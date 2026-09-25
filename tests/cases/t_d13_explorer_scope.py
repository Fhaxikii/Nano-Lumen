# -*- coding: utf-8 -*-
"""Skill 探索的作用域污染与相关诊断。

- [1] 探索作用域「manifest ⊆ dispatcher」不变量：探索子循环已整体拆除，本组只确认
  相关方法已不存在（原来守的是什么、为什么退役，见该函数 docstring；终态由
  `t_f4_catalog` 的「没有任何工具带 EXPLORATION binding」守着）。
- [3] 两个诱导源头：load_tools 的返回文案 + 继承来的按需加载广告。
- [4] 按需加载广告确实从 guide 里剥掉了。
- [7] provider：空文本要能诊断，tool_use 要能上报。

用法：
  py -3.10 tests\cases\t_d13_explorer_scope.py
"""
from __future__ import annotations

import ast
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
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

from core.orchestrator import Orchestrator


from core.schema import AgentDecision
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

    def __init__(self):
        self.notools_calls = 0

    async def chat_with_tools_stream(self, *a, **k):
        yield {"type": "done", "decision": AgentDecision("text", content=""),
               "model": self.target_model, "notice": ""}

    async def chat_without_tools_or_call(self, *a, **k):
        self.notools_calls += 1
        return "", self.target_model, None


class _Spec:
    side_effects: list = []

    def __init__(self, name):
        self.name = name


class _Skill:
    def __init__(self, name):
        self.name = name

    def get_spec(self):
        return _Spec(self.name)


_PERMANENT = ["GetDNSList", "GetSystemVolume", "Base64Codec",
              "GetSystemTime", "HashGenerator", "RegexTester"]


class _FakeRegistry:
    """复刻事故当天的 registry：六个 permanent Skill 全部有 manifest。"""

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self.skills = {n: _Skill(n) for n in _PERMANENT}

    def get_permanent_manifests(self):
        return [{"name": n, "description": f"{n} desc",
                 "parameters": {"type": "object", "properties": {}}} for n in _PERMANENT]

    def get_all_manifests(self): return self.get_permanent_manifests()
    def get_skill_awareness_list(self): return {"official": [], "user": []}
    def is_official_skill(self, n): return n in _PERMANENT
    def get_skill_source(self, n, include_disabled=False): return None
    def list_enabled_skills(self): return list(_PERMANENT)
    def list_disabled_skills(self): return []
    async def execute(self, name, params, progress_ref: str = ""): return None


def make_orch(db_dir: pathlib.Path):
    import core.rag as _rag
    _rag._background_index_started = True   # 跳过知识库后台索引（会去加载 bge-m3）
    st = RuntimeStore(db_dir / "rt.db")
    _stores.append(st)
    reset_kernel_for_tests(store=st, clock=FakeClock(1_800_000_000.0))
    o = Orchestrator(_FakeProvider(), _FakeRegistry(), MemoryManager(max_turns=20))
    o._rt_turn_id = "rtturn_test"
    o._tool_pool = {}
    o._core_manifest = []
    o._tool_failures_this_turn = {}
    o._deferred_awareness_text = (
        "\n\n[More Capabilities — Load on Demand]\n"
        "Nano has these capabilities, but their full schemas are not loaded by default.\n"
        "To use one, call load_tools first; then call the loaded tool on the next step.\n"
        "  - os_execute: operate the computer\n"
        "  - create_new_skill: create a reusable new Skill\n"
    )
    return o


def close_all_stores() -> None:
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


# ══════════════════════════════════════════════════════════════════════════
# 1｜⭐ manifest ⊆ dispatcher 不变量（AST）
# ══════════════════════════════════════════════════════════════════════════

def t_manifest_dispatch_invariant(tmp: pathlib.Path) -> None:
    """[1] ⏸ **已退役**（2026-08-13）：探索子循环整体拆除，本组失去对象。

    ═══ 这里曾经守的是什么（保留下来，因为它是本项目最贵的一课之一）═══

    事故当天的清点：

        探索阶段 manifest      = 12 个工具
        真正有 handler 的      =  6 个
        **无 handler 的**      =  6 个（全部是 permanent Skill）

    模型调那 6 个里的任何一个，都会掉进「链尾未处理的 call」→ 强制总结 → 死路，
    **而且不需要任何上下文污染**。那次的需求恰好是"日期 + DNS"，
    `GetSystemTime` / `GetDNSList` 就明晃晃摆在它的工具清单里。

    本组用 AST 解析 `_run_skill_exploration` 的真实分发链，与
    `_EXPLORATION_DISPATCH_TOOLS` 逐项比对 —— 任一侧改了另一侧没跟，就红。

    ═══ 为什么现在不需要它了 ═══

    这条不变量守的是「**第二个作用域里，声明与实现对不上**」。
    2026-08-13 拆掉探索子循环之后，**没有第二个作用域了** ——
    于是这类事故在结构上不可能再发生，而不是"被守住了"。

    ⭐ 判据的升级链值得单记一笔：
         靠人记注释 → 靠 AST 比对两张名单 → 靠「只有一张名单」
                   → **现在是「只有一个作用域」**
       📌 **每一步都是把同一条约束换成更难违反的形式，而不是新增一条规则。**

    ⚠️ 终态由 `tests/cases/t_f4_catalog.py` 的
       「`eligible(EXPLORATION)` 是空的 / 没有任何工具带 EXPLORATION binding」守着。
    📌 **一个测已退役机制的测试，不再是资产而是残留** —— 它会让人以为那机制还在跑。
    """
    print("")
    print("[1] ⏸ [D13] 作用域不变量已退役（探索子循环拆除，见 docstring）")
    import core.orchestrator as _o
    check(not hasattr(_o.Orchestrator, "_run_skill_exploration"),
          "⭐⭐ `_run_skill_exploration` 确实已不存在（1500 行子循环整体拆除）")
    check(not hasattr(_o.Orchestrator, "_build_skill_exploration_tools"),
          "⭐ 探索工具清单构造器也已不存在")
    check(not hasattr(_o.Orchestrator, "_execute_file_tool_chain"),
          "⭐ 探索专属的文件工具链也已不存在")


# ══════════════════════════════════════════════════════════════════════════
# 3｜作用域污染的两个源头
# ══════════════════════════════════════════════════════════════════════════

def _prompt_text(module: str) -> str:
    """把一个模块里**所有字符串常量**拼起来。

    ⚠️ 为什么不能直接 `in src`：源码里的注释会打中。
    第一版就是这么写的，而且**恰好被留档注释本身打中**——
    那段注释原文是 `# ⚠️ 旧文案是 "They can now be called directly."`。
    这已经是 第三次了：**留档写得越认真，文本匹配越会失败。**
    注释不进 AST，字符串常量进；这里要验的正是"模型会读到什么"，
    所以字符串常量才是正确的取样面。

    ⚠️⚠️ **2026-08-13 第四次踩到，而且形态是新的：docstring 也是字符串常量。**
    上面那段说「注释不进 AST、字符串常量进」是对的，但它漏了一半 ——
    **docstring 恰恰是写留档说明的地方**。拆掉探索子循环之后，
    「`conclude_exploration` 已删除」这条断言就被
    `_exit_create_new_skill` 里**解释它为什么被删的那段 docstring** 打红了。
    📌 **判据升级：要取样「模型会读到什么」，必须同时排除注释【和 docstring】** ——
       两者都是写给人看的，区别只是一个进 AST、一个不进。
       而"进不进 AST"是实现细节，不是判据。
    """
    tree = ast.parse(module_text(module))
    # ⚠️ 按**节点身份**排除，不能按内容排除 —— 同一段文字完全可能真的是提示词。
    _docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = getattr(n, "body", None)
            if (b and isinstance(b[0], ast.Expr)
                    and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                _docs.add(id(b[0].value))
    return "\n".join(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and id(n) not in _docs
    )


def t_scope_pollution(tmp: pathlib.Path) -> None:
    print("\n[3] 两个诱导源头：load_tools 的返回文案 + 继承来的按需加载广告")
    # 模型读到的文字分两处：orchestrator 里的提示词 + core/tools/manifests.py 里的工具说明
    prompts = _prompt_text("core.orchestrator") + "\n" + _prompt_text("core.tools.manifests")
    src = module_text("core.orchestrator")

    # 前置条件：证明取样面真的有内容
    check(len(prompts) > 20000, "前置条件：AST 取到了大量提示词字符串",
          f"{len(prompts)} 字符")

    # ① load_tools 返回文案不再声称一个不存在的持久能力
    check("They can now be called directly." not in prompts,
          "⭐ 旧文案「They can now be called directly.」已从提示词里删除"
          "（它跨轮跨作用域都是假的）")
    check("This applies to the current step only" in prompts, "新文案明确限定在当前步")
    check("always defined by the tools attached to the current request" in prompts,
          "并指明真正的权威是当前请求附带的工具表")
    check("inside a specialized sub-flow" in prompts, "明说不适用于专用子流程")

    # ② 探索 guide 不继承 [More Capabilities — Load on Demand]
    # ⏸ 这里原有三条断言，验的是 `_EXPLORATION_SCOPE_NOTICE`（探索作用域声明）：
    #    「父作用域的工具激活不继承」「这里没有 load_tools」「存在该常量」。
    #    2026-08-13 探索子循环拆除后它们失去对象 —— **而且是好事**：
    #    那三句话本来就是在向模型解释"你现在处在一个不一样的作用域里"，
    #    📌 **一个需要向模型解释自己存在的作用域，本身就是复杂度的来源。**
    check("_EXPLORATION_SCOPE_NOTICE" not in src,
          "⏸ 探索作用域声明常量已随子循环删除（不再需要向模型解释第二个作用域）")
    # ⚠️ 这条断言原来验的是"明说 proceed 要靠输出决策 JSON 而不是调工具"。
    # 那个协议本身就是 bug 的根源 —— 证明模型在 manifest 已清干净、
    # scope notice 已加上的情况下**照样调工具**，因为它需要的是一个出口而不是一句禁令。
    # 现在协议改成了出口工具，断言随之反转：验的是"出口存在且被指名"。
    check("conclude_exploration" not in prompts,
          "⏸ 出口工具 `conclude_exploration` 也已删除 —— 它的职责（证据交接 / "
          "未决问题阻断）搬到了 `create_new_skill` 的必填参数上")
    check("call no tool" in prompts,
          "另一条出口（有问题就纯文本提问、不调工具）也写清了")

    # ③ 提示词不再邀请模型调 os_execute
    check("read-only os_execute actions for OS tasks" not in prompts,
          "空回复重试的提示词不再邀请调 os_execute（它没有 handler）")


def t_guide_strips_ad(tmp: pathlib.Path) -> None:
    print("\n[4] 真的把按需加载广告从探索 guide 里剥掉了")
    o = make_orch(tmp)
    base = "PERSONA..." + o._deferred_awareness_text + "...TAIL"
    # 复刻 _run_skill_exploration 里的剥离逻辑
    adv = o._deferred_awareness_text
    scoped = base.replace(adv, "") if adv and adv in base else base
    check("[More Capabilities — Load on Demand]" in base, "前置条件：base_guide 里确实有这段广告")
    check("[More Capabilities — Load on Demand]" not in scoped, "剥离后不见了")
    check("create_new_skill" not in scoped,
          "⭐ 广告里列的 create_new_skill 也随之消失（它是事故里被调用的那个名字）")
    check(scoped.startswith("PERSONA") and scoped.endswith("TAIL"),
          "只剥掉那一段，前后内容不受影响", scoped)


# ══════════════════════════════════════════════════════════════════════════
# 5｜越界工具调用 → 定向重试一次
# ══════════════════════════════════════════════════════════════════════════





# ══════════════════════════════════════════════════════════════════════════
# 7｜provider 的诊断缺口
# ══════════════════════════════════════════════════════════════════════════



def t_provider_diagnostics(tmp: pathlib.Path) -> None:
    print("\n[7] provider：空文本要能诊断，tool_use 要能上报")
    src = module_text("core.provider")
    check("stop_reason=" in src and "blocks=" in src,
          "空文本时会打出 stop_reason 与 block 类型（事故当天这两个都没有）")
    check("if not text.strip():" in src, "只在没提取到文本时打，正常路径不加噪音")
    check('getattr(b, "type", "") == "tool_use"' in src,
          "会识别模型在无工具请求里返回的 tool_use")

    # AST 验证：成功路径不再无条件 return None（：这种检查用 AST）
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "chat_without_tools_or_call"), None)
    check(fn is not None, "前置条件：AST 找到了 chat_without_tools_or_call")
    if fn is not None:
        rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        # 至少要有一个 return 的第三项不是常量 None
        nonconst = 0
        for r in rets:
            if isinstance(r.value, ast.Tuple) and len(r.value.elts) == 3:
                third = r.value.elts[2]
                if not (isinstance(third, ast.Constant) and third.value is None):
                    nonconst += 1
        check(nonconst >= 1,
              "⭐ 第三个返回值不再永远是 None —— 它原来是死代码，"
              "调用方的 _stray_call_notice 永远拿不到东西",
              f"{nonconst} 处非 None")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("[D13] Skill Explorer 执行作用域污染 + 工具契约不一致")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_d13_"))
    try:
        # ⚠️ 2026-08-13：四组已随探索子循环退役（`t_explorer_builder` /
        #    `t_out_of_scope_retry` / `t_retry_gives_up_once` / `t_conclude_exit_tool`），
        #    历史与理由合并进 `t_manifest_dispatch_invariant` 的 docstring。
        #    ⭐ 留下来的三组**与探索无关、仍然活着**：
        #      · `t_scope_pollution`   —— load_tools 回执文案（主循环的，那条诱因是真的）
        #      · `t_guide_strips_ad`   —— 按需加载广告的剥离逻辑
        #      · `t_provider_diagnostics` —— provider 的空文本诊断与 tool_use 上报
        for fn in (t_manifest_dispatch_invariant, t_scope_pollution,
                   t_guide_strips_ad, t_provider_diagnostics):
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
