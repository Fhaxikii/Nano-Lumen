# -*- coding: utf-8 -*-
"""工具失败时，模型拿到的信息必须【正确且充分】。

═══ 这不是"安全网"，是在修一条说假话的错误信息 ═══

改造前，模型调一个不存在的工具名，收到的是：

    "Error: the tool did not return any valid content."

意思是「工具跑了但没返回内容」——**和事实正好相反**（工具压根没被调用）。
而且一个真实存在、只是 run() 返回 None 的 Skill 拿到的是**同一个字符串**，
两种故障连区分都做不到。模型不是判断失误，是**在假前提上做了合理判断**。

═══ 本套件套用的判据═══

    拿着这条失败消息，一个没有别的上下文的人能不能选出正确的下一步？
    不能就不合格 —— 哪怕它技术上没说错。

所以每个用例都问两件事：**说的是不是真的**，以及**够不够选出下一步**。

用法：
  py -3.10 tests\cases\t_d12_tool_failure_info.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前

from loguru import logger
logger.remove()

import core.orchestrator as orch_mod
from core.orchestrator import Orchestrator
from core.schema import AgentDecision, ToolCall
from core.runtime.clock import FakeClock
from core.runtime.kernel import reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from memory.manager import MemoryManager

_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# ══════════════════════════════════════════════════════════════════════════
# 假件
# ══════════════════════════════════════════════════════════════════════════

class _FakeProvider:
    target_model = "fake-model"

    async def chat_with_tools_stream(self, *a, **k):
        yield {"type": "done", "decision": AgentDecision("text", content=""),
               "model": self.target_model, "notice": ""}


class _Result:
    """模仿 SkillResult：有 success / data，str() 是给模型看的文本。"""

    def __init__(self, text: str, success: bool = True, data=None):
        self._t, self.success, self.data = text, success, data

    def __str__(self):
        return self._t


class _FakeRegistry:
    """只实现 orchestrator 会碰到的那几个方法。

    ⚠️ `_lock` 和 `skills` 不能省：`_check_skill_side_effects` 会用它们，
    而它的 except 分支是**fail-safe**——查不出副作用就返回"需确认"，
    于是测试会撞上 `asyncio.wait_for(..., timeout=300)` 干等五分钟。
    生产代码那样写是对的（宁可多问一次），假件必须配合它。
    """

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self.skills: dict = {}          # 空 → 副作用检查直接返回 []
        self.enabled = ["GetDNSList", "GetSystemTime", "Base64Codec"]
        self.disabled = ["OldReportTool"]
        self.behaviour: dict = {}

    def list_enabled_skills(self): return list(self.enabled)
    def list_disabled_skills(self): return list(self.disabled)

    # ⚠️ 这个替身以前 `list_enabled_skills` 返回三个 Skill、
    #    而 `get_all_manifests()` 返回空 —— **它自己跟自己不一致**。
    #    改造前没暴露，是因为 `_known_tool_names()` 拼的是前者；
    #    现在候选池来自统一工具目录（投影 manifest），这个替身就得像真的
    #    registry 一样两边同源。
    # 📌 一个测试替身如果比真货"宽松"，它验的就不是真货的行为。
    def _man(self, n):
        return {"name": n, "description": f"{n} for tests",
                "parameters": {"type": "object", "properties": {}}}

    def get_permanent_manifests(self): return [self._man(n) for n in self.enabled]
    def get_all_manifests(self): return [self._man(n) for n in self.enabled]
    def is_official_skill(self, n): return False
    def get_skill_source(self, n, include_disabled=False): return None

    async def execute(self, name, params, progress_ref: str = ""):
        if name not in self.enabled:
            return None                       # registry.py:280 的真实行为
        return self.behaviour.get(name, _Result("ok"))


def make_orch(db_dir: pathlib.Path):
    import core.rag as _rag
    _rag._background_index_started = True   # 跳过知识库后台索引（会去加载 bge-m3）
    st = RuntimeStore(db_dir / "rt.db")
    _stores.append(st)
    reset_kernel_for_tests(store=st, clock=FakeClock(1_800_000_000.0))
    reg = _FakeRegistry()
    o = Orchestrator(_FakeProvider(), reg, MemoryManager(max_turns=10))
    o._rt_turn_id = "rtturn_test"
    o._tool_pool = {"os_execute": {}, "look_at_screen": {}, "render_visual": {}}
    o._core_manifest = []
    o._tool_failures_this_turn = {}
    o._ensure_react_sems()
    return o, reg


def close_all_stores() -> None:
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


def run_tool(o, name: str, args: dict | None = None):
    """跑一次**真实的** `_execute_one_tool_call`，返回 ToolExecution + 用户可见卡片摘要。

    不 patch 分发本身 —— 验的就是分发尾部那段接线（：
    绕过被测代码的验证等于没验）。
    """
    q: asyncio.Queue = asyncio.Queue()
    call = ToolCall(name=name, args=args or {}, tool_use_id="tu_1")

    async def _go():
        return await o._execute_one_tool_call(
            call, used_model="fake-model", base_guide="", system_guide="",
            realtime_callback=None, event_queue=q,
        )

    res = asyncio.run(_go())
    summary = ""
    while not q.empty():
        ev = q.get_nowait()
        if ev.get("event") == "tool_end":
            summary = ev.get("result_summary", "")
    return res, summary


# ══════════════════════════════════════════════════════════════════════════

def t_unknown_tool(tmp: pathlib.Path) -> None:
    print("\n[1] 不存在的工具名：旧文案是假的，新文案必须说真话并给出下一步")
    o, reg = make_orch(tmp)
    res, card = run_tool(o, "getdns")
    txt = res.result_text

    # ── 正确 ────────────────────────────────────────────────────────────
    check("did not return any valid content" not in txt,
          "旧的假消息已消失（它把'没被调用'说成了'跑了但没内容'）")
    check("does not exist" in txt, "明说这个名字不存在")
    check("UNKNOWN_TOOL" in txt, "带上故障分类，模型不用自己推")
    check("tool_error: none" in txt,
          "如实说明工具本身没有报错——因为它根本没被调用")
    check(res.ok is False, "标记为失败")

    # ── 充分 ────────────────────────────────────────────────────────────
    check("GetDNSList" in txt,
          "⭐ 给出最接近的【真】名字 —— 这一条直接掐死'猜名字'")
    check("load_tools will NOT help" in txt,
          "说清 load_tools 帮不上忙（它只给参数 schema，不决定工具能不能调）")
    check("Do not try another guessed name" in txt, "明确禁止再猜一个")

    # ── 用户可见的卡片不能被诊断块淹掉 ──────────────────────────────────
    check("Tool Call Diagnostics" not in card,
          "用户卡片上看不到内部诊断块", card)
    check("does not exist" in card, "用户卡片第一行就是人话", card)


def t_disabled_tool(tmp: pathlib.Path) -> None:
    print("\n[2] 存在但被禁用：和'不存在'是两件事，建议也不一样")
    o, reg = make_orch(tmp)
    res, _ = run_tool(o, "OldReportTool")
    txt = res.result_text
    check("DISABLED_TOOL" in txt, "分类为 DISABLED_TOOL 而不是 UNKNOWN_TOOL")
    check("does not exist" not in txt, "不能说它不存在——那是假话")
    check("do not recreate it" in txt.lower(), "明确阻止模型去重新造一个")
    check("manage_existing_skill" in txt, "给出真正可行的下一步：启用它")


def t_bad_params(tmp: pathlib.Path) -> None:
    print("\n[3] 参数不合 schema：名字是对的，别换工具")
    o, reg = make_orch(tmp)
    reg.behaviour["GetSystemTime"] = _Result(
        "Execution failed: missing required parameter: fmt", success=False)
    res, _ = run_tool(o, "GetSystemTime")
    txt = res.result_text
    check("BAD_PARAMS" in txt, "分类为 BAD_PARAMS")
    check("missing required parameter: fmt" in txt, "工具自己的报错原样保留在最前")
    check("The name is correct" in txt, "明说名字没错，避免它去换工具")
    check('load_tools(names=["GetSystemTime"])' in txt,
          "这一类 load_tools **确实**有用（要参数 schema），所以给出确切调法")
    check("Do not guess parameter names" in txt, "禁止猜参数名")


def t_tool_error(tmp: pathlib.Path) -> None:
    print("\n[4] 工具跑了并报错：以工具报错为权威")
    o, reg = make_orch(tmp)
    reg.behaviour["GetDNSList"] = _Result("错误：网络适配器未启用", success=False)
    res, card = run_tool(o, "GetDNSList")
    txt = res.result_text
    check("TOOL_ERROR" in txt, "分类为 TOOL_ERROR")
    check("错误：网络适配器未启用" in txt, "工具原始报错保留")
    check("authoritative" in txt, "明说工具报错是权威，别自己重新解释")
    check("Do not switch to a different tool name" in txt,
          "禁止因为执行失败就去换个工具名试（这是真实发生过的行为）")
    check(card.startswith("错误：网络适配器未启用"),
          "用户卡片上是工具原话，不是诊断块", card)


def t_none_return_now_true(tmp: pathlib.Path) -> None:
    print("\n[5] 真的返回 None 的工具：那句话现在是真的了")
    o, reg = make_orch(tmp)

    async def _none(name, params):
        return None
    # 名字在 enabled 里，但 execute 返回 None —— 这是"跑了但没内容"的真实情况
    reg.enabled.append("EmptyTool")
    reg.behaviour["EmptyTool"] = None
    orig = reg.execute

    # ⚠️ 签名要跟着真接口走（`progress_ref` 是 2026-08-09 长任务统一加的）。
    #    📌 **一次接口扩参会打到所有替身，包括这种「赋值替换」的** ——
    #       而按 `async def execute` 搜索抓不到它，只有按 `\.execute\s*=` 才行。
    async def _exec(name, params, progress_ref: str = ""):
        if name == "EmptyTool":
            return None
        return await orig(name, params, progress_ref)
    reg.execute = _exec

    res, _ = run_tool(o, "EmptyTool")
    txt = res.result_text
    check("ran but returned no content" in txt,
          "这句话只在真的'跑了但没返回'时出现")
    check("UNKNOWN_TOOL" not in txt, "不会被误判成名字不存在")
    check("TOOL_ERROR" in txt, "归类为执行失败，工具存在")


def t_repeat_guard(tmp: pathlib.Path) -> None:
    print("\n[6] 同一轮重复撞同一个名字 → 升级措辞，防止连试三个变体")
    o, reg = make_orch(tmp)
    res1, _ = run_tool(o, "getdns")
    check("already called" not in res1.result_text, "第一次不升级")
    res2, _ = run_tool(o, "getdns")
    check("already called" in res2.result_text, "第二次明说'你这轮已经试过了'")
    check("Stop retrying" in res2.result_text, "要求停止重试")

    # 换个名字不该被算成重复
    res3, _ = run_tool(o, "getdnslist2")
    check("already called" not in res3.result_text, "不同名字不算重复")

    # 轮次切换后清零 —— 跨轮保留会让"你这轮已经试过"变成假话
    o._tool_failures_this_turn = {}
    res4, _ = run_tool(o, "getdns")
    check("already called" not in res4.result_text, "新一轮清零")


def t_known_names_universe(tmp: pathlib.Path) -> None:
    # 标题从「四个来源」改成「唯一权威」：候选池不再手工拼
    # registry / _tool_pool / _CORE_TOOL_NAMES / MCP 四份，而是问统一工具目录。
    # ⭐ 断言本身一条没减 —— 要守的东西没变（内置、Skill、load_tools 都要在，
    #    禁用的不许在），变的只是"去问谁"。
    print("\n[7] '最接近的真名字'的候选池 —— 现在来自唯一权威")
    o, reg = make_orch(tmp)
    names = o._known_tool_names()
    check("GetDNSList" in names, "包含已注册 Skill")
    check("os_execute" in names, "包含延迟工具池（模型猜错的经常是内置工具）")
    check("query_local_knowledge" in names, "包含核心工具")
    check("load_tools" in names, "包含 load_tools 本身")
    check("OldReportTool" not in names,
          "**不**包含已禁用的 Skill —— 推荐一个用不了的名字等于给假建议")


def t_success_adds_nothing(tmp: pathlib.Path) -> None:
    print("\n[8] 成功时一个字都不加")
    o, reg = make_orch(tmp)
    reg.behaviour["Base64Codec"] = _Result("bmFubw==")
    res, card = run_tool(o, "Base64Codec")
    check(res.result_text == "bmFubw==", "成功结果原样返回，无任何附加", res.result_text)
    check(res.ok is True, "标记为成功")
    check("Diagnostics" not in card, "卡片上也没有诊断块")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("[D12] 工具失败信息：正确 + 充分")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_d12_"))
    try:
        for fn in (t_unknown_tool, t_disabled_tool, t_bad_params, t_tool_error,
                   t_none_return_now_true, t_repeat_guard, t_known_names_universe,
                   t_success_adds_nothing):
            d = tmp / fn.__name__
            d.mkdir(parents=True, exist_ok=True)
            try:
                fn(d)
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
    finally:
        close_all_stores()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 74)
    if passed == total:
        print(f"结果：{passed}/{total} 通过")
    else:
        print(f"结果：{passed}/{total} 通过 —— 失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
