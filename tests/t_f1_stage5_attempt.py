# -*- coding: utf-8 -*-
"""`ActionAttempt` —— 「我当前这一个动作做到哪了」。

═══ 这个套件盯的核心只有一条 ═══

**两个维度必须正交，压成一个枚举就会造出假事实。**

`status`（走到哪一步）× `effect_state`（现实被改了没有）。
因为 **GUI 没有 rollback**：Nano 要输入 `abcdef`、已经输入了 `abc` 时用户动手，
那么 `abc` **真的在记事本里了**。把它记成"中断了"就等于宣称"没发生"。

📌那句：**「明确 commit boundary，而不是幻想 GUI 具备数据库 rollback。」**
📌 与本轮所有判据同源：**一个字段不许表达两个现实。**

═══ 第二条：「中断了」和「失败了」语义完全相反 ═══
* **失败** = 可以放心重做
* **中断** = 结果不可信、重做可能**重复副作用**

所以用户接管时必须走 `INTERRUPT` 而不是 `FINISH(ok=False)` ——
后者会把它记成"失败"，而"失败"暗示"没生效"。

用法：
  py -3.10 tests\t_f1_stage5_attempt.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import Command, reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import task as _task
from core.runtime import attempt as A

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    _task.clear_blocker_providers_for_tests()
    clock = FakeClock(BASE_T)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=clock), clock


# ══════════════════════════════════════════════════════════════════════════

def t_commit_boundary(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ `PREPARED → IN_FLIGHT` 那一步就是 commit boundary。"""
    print("\n[1] ⭐⭐⭐ commit boundary：打断发生在提交前还是提交后")
    k, _ = make_kernel(tmp / "a")

    # 提交**前**被打断 → NONE（最好的情况）
    a1 = A.begin(action="type_text", summary="输入 abcdef", turn_id="t1")
    A.interrupt(a1, "用户接管")
    r1 = A.get(k, a1)
    check(r1.status == A.AttemptStatus.INTERRUPTED, "状态是 INTERRUPTED")
    check(r1.effect_state == A.EffectState.NONE,
          "⭐ 还没碰现实就被拦住 → `NONE`（**最好的情况**，可以直接重做）",
          r1.effect_state)
    check(r1.may_have_changed_the_world is False, "「世界可能被改了吗」→ 否")

    # 提交**后**被打断 → PARTIAL_OR_UNKNOWN（GUI 常态）
    k2, _ = make_kernel(tmp / "b")
    a2 = A.begin(action="type_text", summary="输入 abcdef", turn_id="t1")
    A.mark_in_flight_current()
    check(A.get(k2, a2).status == A.AttemptStatus.IN_FLIGHT, "已过 commit boundary")
    A.interrupt(a2, "用户接管")
    r2 = A.get(k2, a2)
    check(r2.effect_state == A.EffectState.PARTIAL_OR_UNKNOWN,
          "⭐⭐⭐ 已经开始碰现实才被打断 → `PARTIAL_OR_UNKNOWN` —— "
          "**GUI 没有 undo，那半截字真的在记事本里了**", r2.effect_state)
    check(r2.may_have_changed_the_world is True, "「世界可能被改了吗」→ 是")

    # ⚠️ 调用方**不许**指定 effect_state（内核按 boundary 推）
    k3, _ = make_kernel(tmp / "c")
    a3 = A.begin(action="click", turn_id="t1")
    A.mark_in_flight_current()
    k3.submit(Command(kind=A.INTERRUPT, payload={
        "attempt_id": a3, "reason": "试图硬塞 NONE",
        "effect_state": A.EffectState.NONE}))     # ← 故意传 NONE
    check(A.get(k3, a3).effect_state == A.EffectState.PARTIAL_OR_UNKNOWN,
          "⭐⭐ 调用方硬传 `NONE` 也**不生效** —— 内核按 commit boundary 推导。"
          "📌 **把约束放在唯一的写入口，比事后校验更强**")


def t_interrupted_is_not_failed(tmp: pathlib.Path) -> None:
    """⭐⭐ 「中断」和「失败」在语义上完全相反。"""
    print("\n[2] ⭐⭐ 「中断了」≠「失败了」")
    k, _ = make_kernel(tmp / "d")

    a_int = A.begin(action="type_text", summary="输入 abc", turn_id="t1")
    A.mark_in_flight_current()
    A.interrupt(a_int, "用户接管了这台电脑")
    t_int = A.describe_for_model(A.get(k, a_int))

    k2, _ = make_kernel(tmp / "e")
    a_fail = A.begin(action="click", summary="点击按钮", turn_id="t1")
    A.finish(a_fail, ok=False, effect_state=A.EffectState.NONE, reason="定位失败")
    t_fail = A.describe_for_model(A.get(k2, a_fail))

    check("Do NOT assume it failed" in t_int,
          "⭐⭐ 中断的话里明确说了**别当成失败** —— "
          "失败暗示「没生效」，而这里可能已经生效了")
    check("no undo" in t_int, "⭐ 明确说了**没有 undo**")
    check("blindly repeat" in t_int,
          "⭐ 明确说了**别盲目重做** —— 重做可能重复副作用")
    check("Observe the current state first" in t_int,
          "⭐ 给了可执行的下一步（先看现场），不是只报状态")
    check("nothing was changed" in t_fail,
          "⭐ 而真正的「失败且没生效」就直说什么都没改", t_fail)
    check(t_int != t_fail,
          "⭐⭐ 两句话**必须不同** —— 这就是这一层实体存在的全部理由")


def t_no_silent_none(tmp: pathlib.Path) -> None:
    """⚠️ 不许「没消息当没发生」。"""
    print("\n[3] ⚠️ `effect_state` 不给时的默认方向")
    k, _ = make_kernel(tmp / "f")
    a = A.begin(action="click", turn_id="t1")
    A.mark_in_flight_current()
    A.finish(a, ok=False)          # 不传 effect_state
    check(A.get(k, a).effect_state == A.EffectState.PARTIAL_OR_UNKNOWN,
          "⭐⭐ 失败且没给 effect → 按 `PARTIAL_OR_UNKNOWN`（保守），**绝不默认 NONE**。"
          "📌 默认成 NONE 就是「没消息当没发生」，那是最危险的假事实")

    k2, _ = make_kernel(tmp / "g")
    a2 = A.begin(action="click", turn_id="t1")
    A.mark_in_flight_current()
    A.finish(a2, ok=True)
    check(A.get(k2, a2).effect_state == A.EffectState.CONFIRMED,
          "反证：成功且没给 effect → `CONFIRMED`（不是一律保守）")


def t_never_two_open(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐ 同一时刻最多一条未结束的（数据库唯一索引兜底）")
    k, _ = make_kernel(tmp / "h")
    a1 = A.begin(action="click", turn_id="t1")
    A.mark_in_flight_current()
    a2 = A.begin(action="click2", turn_id="t1")   # 上一条没收尾
    r1 = A.get(k, a1)
    check(r1.status == A.AttemptStatus.INTERRUPTED,
          "⭐ 开新的时**顺手把上一条收成 INTERRUPTED** —— "
          "📌 同 `oslease`：不依赖任何人记得收尾")
    check(r1.effect_state == A.EffectState.PARTIAL_OR_UNKNOWN,
          "⚠️ 而且按**保守**收 —— 「没人收尾」本身就说明我们不知道它做到哪了")
    cur = A.in_flight(k)
    check(cur is not None and cur.attempt_id == a2, "当前未结束的只有新那条")


def t_startup_sweep(tmp: pathlib.Path) -> None:
    """⭐ 进程崩在输入一半 —— 重启后模型能知道「上次那步结果不可信」。"""
    print("\n[5] ⭐ 启动收尾：崩在半路也留得下事实")
    k, _ = make_kernel(tmp / "i")
    a = A.begin(action="type_text", summary="输入 abcdef", turn_id="t1")
    A.mark_in_flight_current()
    # 模拟进程直接消失：什么都不收，重新起一个内核指向同一个库
    k2 = reset_kernel_for_tests(store=RuntimeStore(tmp / "i" / "rt.db"),
                                clock=FakeClock(BASE_T + 100))
    from core.runtime.reconciler import reconcile_on_startup
    rep = reconcile_on_startup(k2)
    r = A.get(k2, a)
    check(r.status == A.AttemptStatus.INTERRUPTED,
          "⭐ 启动时收成 INTERRUPTED", str(rep))
    check(r.effect_state == A.EffectState.PARTIAL_OR_UNKNOWN,
          "⭐⭐ 而且是 `PARTIAL_OR_UNKNOWN` —— **崩在输入一半，那半截字还在**。"
          "重启后模型知道「上次那步结果不可信」，而不是一无所知")


def t_wiring() -> None:
    print("\n[6] 接线：commit boundary 在真正调执行器之前")
    dsp = (ROOT / "core" / "os_layer" / "dispatch.py").read_text(encoding="utf-8")
    dc = "\n".join(l for l in dsp.splitlines() if not l.strip().startswith("#"))
    i_mark = dc.find("mark_in_flight_current()")
    i_call = dc.find("result = await fn(params)")
    check(0 < i_mark < i_call,
          "⭐⭐⭐ `IN_FLIGHT` 标在 `await fn(params)` **之前** —— "
          "那一行就是 commit boundary，之后副作用可能真的发生",
          f"mark@{i_mark} < call@{i_call}")
    # ⚠️ 不许标得太早：定位/授权确认那段现实一点没变（确认弹窗可能等用户很久）
    i_locate = dc.find("await self._vision.locate(")
    check(i_locate < 0 or i_locate < i_mark,
          "⚠️ 标在**定位之后** —— 定位和授权确认期间现实一点没变；"
          "标早了会把「等用户点确认」也算成「正在改世界」")

    orc = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    oc = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))
    check("_att_m.begin(" in oc, "⭐ `os_execute` 开尝试")
    check("_att_m2.interrupt(" in oc and "_att_m2.finish(" in oc,
          "⭐⭐ 收尾分两条路 —— 用户接管走 `interrupt`，其余走 `finish`")
    i_still = oc.find("_still_mine")
    i_fin = oc.find("_att_m2.finish(")
    check(0 < i_still < i_fin,
          "⭐⭐ **先判「机器还归不归 Nano」再决定走哪条** —— "
          "📌 用户接管时若走 `finish(ok=False)`，会把它记成「失败」，"
          "而「失败」暗示「没生效」，那是假事实",
          f"still_mine@{i_still} < finish@{i_fin}")
    check("describe_for_model(" in oc,
          "⭐ 恢复提示用的是 `describe_for_model()`，不再是笼统那句「环境可能变了」")

    # ⚠️ 不许复用 ToolBatchSpan
    att = (ROOT / "core" / "runtime" / "attempt.py").read_text(encoding="utf-8")
    check("ToolBatchSpan" in att and "绝对不能解释成" in att,
          "⭐ 模块头留痕：为什么**不能**复用 `ToolBatchSpan` —— "
          "它表达的是工具协议闭没闭合，不是现实有没有被改")


def t_handed_back_long_command_finishes_only_on_real_result() -> None:
    """交还不是完成；join 返回的真实结果才是 attempt 的收口点。"""
    print("\n[7] 被交还的长命令在真实完成时收尾 ActionAttempt")
    source = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    waiter = next((node for node in ast.walk(tree)
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "_await_longcmd"),
                  None)
    body = ast.unparse(waiter) if waiter else ""

    check(waiter is not None, "交还长命令的完成协程存在")
    check("await _aio.to_thread(_lc2.join, _r)" in body,
          "它以 longcmd.join 的真实返回作为完成边界")
    finish_calls = [node for node in ast.walk(waiter or ast.Pass())
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "finish"]
    finish_call = next((node for node in finish_calls
                        if node.args and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "_attempt_id"), None)
    check(finish_call is not None and "_res.get('ok')" in ast.unparse(finish_call),
          "🔴→✅ 只在 join 已返回后，按真实 ok 收尾 ActionAttempt")
    check(body.find("await _aio.to_thread(_lc2.join, _r)")
          < body.find(".finish("),
          "finish 排在真实完成之后，而非交还点")

    # 交还点本身仍不得收尾；否则把「仍在跑」记成既成事实。
    handback = source[source.find("if _lr and _os_result.get(\"ok\"):"):]
    handback = handback[:handback.find("# 用户中断")]
    before_waiter = handback[:handback.find("async def _await_longcmd")]
    check(".finish(" not in before_waiter,
          "⭐⭐⭐ 交还点仍保持 IN_FLIGHT；不许为未结束的事记结论")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_commit_boundary(tmp)
        t_interrupted_is_not_failed(tmp)
        t_no_silent_none(tmp)
        t_never_two_open(tmp)
        t_startup_sweep(tmp)
    t_wiring()
    t_handed_back_long_command_finishes_only_on_real_result()
    passed = sum(1 for r in _results if r[0])
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
