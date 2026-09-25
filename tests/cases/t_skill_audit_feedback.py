# -*- coding: utf-8 -*-
"""审计失败信息要到模型手里（2026-08-05，实测）。

═══ 这个套件在验什么 ═══

审计窗口拦下一个 Skill、用户点丢弃之后，模型手上曾经什么都没有：

    · 代码从未进 memory（`skill_preview` 的 code 只给弹窗渲染）
    · 校验报错只进 `validation_lbl`，**纯 UI**
    · app 侧那条 `[System record: ...discarded...]` 会被 max_turns=10 切掉

于是用户说"上次写的校验报错，重新写"时，Nano 只能反问
「哪个 Skill 出问题了？」「我需要先检查它的代码看看哪里报错」——
而那个 Skill 压根没部署，`inspect_existing_skill` 查不到。

⚠️ **为什么必须有单测**：这条实测很难复现 —— Haiku 写出不合协议代码的概率本来不高，
碰上了还得正好点丢弃、再隔几轮问回来。等实测撞到才发现回归，代价太大。

用法：
  py -3.10 tests\cases\t_skill_audit_feedback.py
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
from core.runtime.clock import FakeClock
from core.runtime.kernel import reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from memory.manager import MemoryManager

_results: list[tuple[bool, str, str]] = []
_stores: list = []



def _force_legacy_truncation():
    """把 `_truncate_safely` 按**旧的 10 轮硬切**跑一次的上下文管理器。

    ⚠️⚠️ 为什么需要它（2026-08-14 阶梯打开后）：
       `ladder_enabled=true` 时旧截断**按设计退位**成 catastrophic last resort
       （触发点抬到 `max_turns*10` + 响亮报警）—— 见 `memory/manager.py`。
       于是本项目里所有「灌 15 轮 → 看它被切掉」的测试都不再成立。
    🔴 但它们验的东西**没有过期**：
       · `t_f3` 验的是「UI 重放 ≠ 模型投影」
       · `t_skill_audit` 验的是「那条系统记录必须活过截断」
       两者都需要**截断真的发生**才谈得上，截断只是它们的**布景**。
    📌 **一条测试的布景失效时，要换布景，不是删掉那条测试** ——
       删掉的话，等哪天有人把退位改回去，没有任何东西会红。
    """
    import contextlib
    import core.models as _M

    @contextlib.contextmanager
    def _cm():
        _orig = _M.ladder_enabled
        _M.ladder_enabled = lambda: False
        try:
            yield
        finally:
            _M.ladder_enabled = _orig
    return _cm()

def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _FakeProvider:
    target_model = "fake"

    async def generate_skill_spec(self, *a, **k):
        return None


class _FakeRegistry:
    def __init__(self):
        self.skills = {}

    def get_permanent_manifests(self): return []
    def get_all_manifests(self): return []
    def get_skill_awareness_list(self): return {"official": [], "user": []}
    def is_official_skill(self, n): return False
    def get_skill_source(self, n, include_disabled=False): return None
    def reload_all(self): pass
    async def execute(self, name, params, progress_ref: str = ""): return None


def make_orch(db_dir: pathlib.Path) -> Orchestrator:
    import core.rag as _rag
    _rag._background_index_started = True   # 跳过知识库后台索引（会去加载 bge-m3）
    st = RuntimeStore(db_dir / "rt.db")
    _stores.append(st)
    reset_kernel_for_tests(store=st, clock=FakeClock(1_800_000_000.0))
    o = Orchestrator(_FakeProvider(), _FakeRegistry(), MemoryManager(max_turns=10))
    o._rt_turn_id = "rtturn_test"
    return o


def close_all_stores() -> None:
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


_ERRS = [
    "缺少 'from core.schema import ...' 导入",
    "缺少类定义",
    "缺少 get_manifest() 方法",
    "缺少 get_spec() 方法(v3.0 协议要求)",
    "run() 必须返回 SkillResult 而非字符串(v3.0 协议要求)",
]


def _fail(o: Orchestrator, filename="ExtractComputerIP", errors=None, code="x = 1\n"):
    """直接构造一次"审计校验失败"的状态，等价于 _emit_skill_preview 里 not ok 那一支。"""
    import hashlib as _h
    o._last_audit_failure = {
        "filename": filename, "mode": "create",
        "errors": list(errors if errors is not None else _ERRS),
        "code_hash": _h.sha256(code.encode()).hexdigest()[:12],
        "code_lines": len(code.splitlines()),
        "outcome": "awaiting", "at": 1_800_000_000.0,
    }
    o._pending_skill = {"filename": filename, "mode": "create", "code": code,
                        "description": "d", "valid": False, "errors": list(_ERRS),
                        "lifecycle": "permanent", "spec_side_effects": [],
                        "error_context": "", "target_skill": filename,
                        "change_summary": ""}


# ══════════════════════════════════════════════════════════════════════════

def t_default_silent(tmp: pathlib.Path) -> None:
    print("\n[1] 没有失败记录时一个字符都不加")
    o = make_orch(tmp)
    check(o._build_audit_failure_injection() == "", "空状态 → 空串（不浪费 token）")
    o._last_audit_failure = {"filename": ""}
    check(o._build_audit_failure_injection() == "", "残缺状态也不产出（filename 为空）")


def t_injection_content(tmp: pathlib.Path) -> None:
    print("\n[2] 校验报错必须逐条出现在模型看得见的地方")
    o = make_orch(tmp)
    _fail(o)
    inj = o._build_audit_failure_injection()
    check("ExtractComputerIP" in inj, "点名了是哪个 Skill（不用模型反问）")
    missed = [e for e in _ERRS if e not in inj]
    check(not missed, "⭐ 五条校验报错一条不少地传给模型", f"漏了 {missed}" if missed else "5/5")
    # ⚠️ 这条断言第一版写成了 `"awaiting" not in inj`，想验"别把内部枚举名甩给模型"，
    # 但 awaiting 那句人话本身就含 "awaiting the user's decision" —— 断言自己写错了。
    # 改成验真正想验的：**模型看到的是完整句子，不是裸的状态码**。
    check("audit window" in inj, "状态用人话表达（still sitting in the audit window）")
    check("outcome=" not in inj and "code_hash=" in inj,
          "内部字段名不裸奔（code_hash 是给模型对版本的，属于有用信息）")

    # 设计原则 8.5：注入给模型的一律英文（报错原文来自校验器，保持原样）
    _frame = inj
    for e in _ERRS:
        _frame = _frame.replace(e, "")
    _zh = [c for c in _frame if "一" <= c <= "鿿"]
    check(not _zh, "⭐ 框架文字全英文（设计原则 8.5），只有校验器原文保留中文",
          f"残留 {''.join(_zh[:10])}" if _zh else "clean")


def t_discarded_outcome(tmp: pathlib.Path) -> None:
    print("\n[3] 用户点丢弃 → 模型必须知道它【没部署且查不到】")
    o = make_orch(tmp)
    _fail(o)
    res = o.cancel_pending_skill()
    check(res["ok"] and "ExtractComputerIP" in res["msg"], "丢弃返回值正常")
    check(o._last_audit_failure["outcome"] == "discarded", "outcome 落定为 discarded")
    inj = o._build_audit_failure_injection()
    check("NOT deployed" in inj, "明说没有部署")
    check("cannot inspect" in inj, "⭐ 明说查不到 —— 否则它会去 inspect 一个不存在的 Skill")
    check("do not ask them" in inj,
          "⭐ 明说不要反问用户（实测就是在这里反问的）")
    check(o._pending_skill is None, "pending 已清空")


def t_discard_after_pass(tmp: pathlib.Path) -> None:
    print("\n[4] 校验通过但用户仍然丢弃 —— 也不能让模型以为部署了")
    o = make_orch(tmp)
    o._pending_skill = {"filename": "GoodSkill", "mode": "create", "code": "x=1\n",
                        "description": "d", "valid": True, "errors": [],
                        "lifecycle": "permanent", "spec_side_effects": [],
                        "error_context": "", "target_skill": "GoodSkill",
                        "change_summary": ""}
    o.cancel_pending_skill()
    inj = o._build_audit_failure_injection()
    check(o._last_audit_failure is not None, "无报错也要留记录（被丢弃本身就是事实）")
    check("GoodSkill" in inj and "discarded it anyway" in inj, "说明是通过了但被丢弃")
    check("Do not assume it exists" in inj, "⭐ 明说别假设它存在")
    # 纪律：断言"没有报错清单"时，先确认这条路真的走到了
    check("protocol check on these points" not in inj,
          "没有报错时不编造报错清单")


def t_cleared_on_deploy(tmp: pathlib.Path) -> None:
    print("\n[5] 部署成功 → 记录必须作废（否则模型会一直念叨已经修好的事）")
    o = make_orch(tmp)
    _fail(o, filename="Later")
    check(o._build_audit_failure_injection() != "", "前置条件：注入确实存在")
    # 模拟 apply_pending_skill 成功那一段的清理
    _af = o._last_audit_failure
    if isinstance(_af, dict) and _af.get("filename") == "Later":
        o._last_audit_failure = None
    check(o._build_audit_failure_injection() == "", "⭐ 同名部署成功后不再注入")

    # 不同名的部署不该顺手清掉别人的记录
    o2 = make_orch(tmp)
    _fail(o2, filename="AAA")
    _af2 = o2._last_audit_failure
    if isinstance(_af2, dict) and _af2.get("filename") == "BBB":
        o2._last_audit_failure = None
    check("AAA" in o2._build_audit_failure_injection(),
          "部署 BBB 不影响 AAA 的失败记录（按 filename 匹配，不是无脑清空）")


def t_survives_truncation(tmp: pathlib.Path) -> None:
    print("\n[6] ⭐ 核心：它必须活过 max_turns 截断（这才是实测翻车的根因）")
    o = make_orch(tmp)
    _fail(o)
    o.cancel_pending_skill()

    # app 侧那条 memory 记录 —— 实测里它就是这么写的
    o.memory.add_message(
        "assistant",
        "[System record: the user discarded the pending Skill in the UI; it was not deployed.] "
        "已丢弃 Skill 「ExtractComputerIP」")
    check(any("discarded" in (m.content or "") for m in o.memory.storage),
          "前置条件：memory 里确实写过那条系统记录")

    # 灌 15 个用户回合，越过 max_turns=10
    # ⚠️ 显式走**旧的 10 轮硬切**（见 `_force_legacy_truncation` 的说明）——
    #    这条验的是「那条系统记录必须活过截断」，截断只是布景。
    with _force_legacy_truncation():
        for i in range(15):
            o.memory.add_message("user", f"第 {i} 句无关的话")
            o.memory.add_message("assistant", f"回应 {i}")
    check(not any("discarded" in (m.content or "") for m in o.memory.storage),
          "⭐ memory 里那条记录确实被截断掉了（实测现象复现）")

    inj = o._build_audit_failure_injection()
    check("ExtractComputerIP" in inj,
          "⭐⭐ 但动态段仍然知道是哪个 Skill —— 截断影响不到它")
    check(all(e in inj for e in _ERRS),
          "⭐⭐ 校验报错也仍然在（模型能说清哪里不合协议，不必反问）")


def t_reset_clears(tmp: pathlib.Path) -> None:
    print("\n[7] 重置对话要清掉它（语义就是忘掉这段）")
    o = make_orch(tmp)
    _fail(o)
    o._last_spec_errors = ["stale"]
    o.reset_conversation()
    check(o._last_audit_failure is None, "重置后审计失败记录已清")
    check(o._last_spec_errors == [], "SkillSpec 报错也一并清")
    check(o._build_audit_failure_injection() == "", "新对话不带上一段的失败上下文")


def t_spec_errors_not_stale(tmp: pathlib.Path) -> None:
    print("\n[8] SkillSpec 报错不许跨次残留（我自己改动引入过的副作用）")
    o = make_orch(tmp)
    o._last_spec_errors = ["上一次的旧报错"]

    async def _go():
        # provider.generate_skill_spec 返回 None → 走"JSON 解析失败"那条 return None，
        # 它【不】写 _last_spec_errors。入口没清的话旧报错会被当成本次的注给模型。
        return await o._generate_skill_spec("随便什么需求", None)

    r = asyncio.run(_go())
    check(r is None, "前置条件：这次 spec 生成确实失败了")
    check(o._last_spec_errors == [],
          "⭐ 入口已清空 —— 不会把上一次的报错当成本次的注给模型",
          f"残留 {o._last_spec_errors}")


# ══════════════════════════════════════════════════════════════════════════

def t_truncation_reported(tmp: pathlib.Path) -> None:
    """撞 max_tokens 被截断 → 报错必须说"被截断"，而不是列一串协议缺失（实测）。

    那次生成一个 7 项功能的 Windows 网络配置 Skill，`output=8192` 一字不差等于上限，
    响应中途被切断，`code` 参数是空的。审计窗口于是报出 7 条
    "缺少 import / 缺少类定义 / 缺少 get_spec() / run() 必须返回 SkillResult…" ——
    **每一条都对，但每一条都指错方向**：用户看到的是"模型不懂协议"，
    真相是"模型话没说完"。照着这些错误去改需求、改提示词全是白功。

    ⚠️ 这条实测很难强制触发（要正好写一个够大的 Skill 把上限用光），所以必须有单测。
    """
    print("\n[9] 撞 max_tokens 被截断要如实说，不许伪装成协议错误")
    from core.schema import AgentDecision

    o = make_orch(tmp)
    _q = asyncio.Queue()

    def _drive(dec):
        evs = []

        async def _go():
            async for ev in o._emit_skill_preview_from_decision(dec, "fake-model"):
                evs.append(ev)
        asyncio.run(_go())
        return [e for e in evs if e.get("event") == "skill_preview"]

    # ① 截断 + 空代码
    prev = _drive(AgentDecision(
        "call", name="WriteSkill", tool_use_id="t1",
        args={"filename": "Trunc", "code": "", "description": "d"},
        truncated=True))
    check(len(prev) == 1, "产出了审计事件", f"{len(prev)} 个")
    errs = prev[0].get("validation_errors") or [] if prev else []
    check(prev and prev[0].get("validation_ok") is False, "标为未通过校验")
    check(any("截断" in e for e in errs),
          "⭐ 第一条就说明是被截断（不是让用户去看那堆协议缺失）",
          errs[0][:40] if errs else "无")
    check(errs and "截断" in errs[0],
          "⭐ 截断说明排在最前 —— 后面那些协议错误是它的症状")
    check(any("拆小" in e for e in errs),
          "给了可执行的下一步（把 Skill 拆小重新生成）")
    check(any("缺少" in e for e in errs),
          "前置条件：原有的协议错误仍然保留（只是被降级为症状）")

    # ② 同样空代码但【没有】截断 → 不许编造截断说明
    prev2 = _drive(AgentDecision(
        "call", name="WriteSkill", tool_use_id="t2",
        args={"filename": "Empty", "code": "", "description": "d"},
        truncated=False))
    errs2 = prev2[0].get("validation_errors") or [] if prev2 else []
    check(errs2 and not any("截断" in e for e in errs2),
          "⭐ 没截断时不编造截断说明（否则就是把一种错误伪装成另一种）")
    check(any("缺少" in e for e in errs2), "但协议错误照常报")

    # ③ 默认值：老代码构造的 decision 没这个字段也不能崩
    _d = AgentDecision("call", name="WriteSkill", args={})
    check(getattr(_d, "truncated", None) is False,
          "truncated 默认 False（旧构造点不受影响）")


def main() -> int:
    print("=" * 74)
    print("审计失败信息要到模型手里（实测撞到的缺陷）")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_audit_fb_"))
    try:
        for fn in (t_default_silent, t_injection_content, t_discarded_outcome,
                   t_discard_after_pass, t_cleared_on_deploy, t_survives_truncation,
                   t_reset_clears, t_spec_errors_not_stale, t_truncation_reported):
            try:
                fn(tmp)
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
