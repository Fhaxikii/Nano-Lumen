# -*- coding: utf-8 -*-
"""轮内 await —— 把「闸」改成真正的「挂起」。

═══ 这个套件存在的理由是一个形态错误 ═══

第一版做成了**闸**：拿不到租约 → 动作失败 → 模型只能"别重试" → **结束这一轮**。
于是用户每发一次「继续」都是新的一轮，进来立刻撞墙、再结束 ——
实测表现为"**永久锁死**"。

已明确正确形态（原话转述）：
> 没有被动挂起的情况下，一旦被用户操作打断，**Nano 的 turn 就彻底结束了**。
> 有了被动挂起是**全流程自动**的：用户占用 → 瞬间挂起并告知 →
> 用户彻底停手 → **Nano 自动从挂起中恢复** → 继续。
> **整个都没脱离这轮 turn。**

📌📌 **判据：「闸」和「挂起」在代码里长得像，行为相反。**
   **闸的出口是失败，挂起的出口是等待再继续。**
   **一个只有失败出口的机制，最终一定把成本转嫁给用户去手动重试。**

📌 顺带解掉一个误判过的担心：曾说"续期没有上限 = 可以被永久锁住"——
   **如果 Nano 是在等而不是在失败，无限续期是对的**：用户在用电脑，它就该等。
   上限只是为了别把一轮永远挂着。

用法：
  py -3.10 tests\t_f1_stage5_await.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

from core.runtime.kernel import Command, reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import task as _task
from core.runtime import oslease as L

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def fresh():
    """⚠️ 用**真实时钟**，不用 FakeClock —— 被测的东西是 `asyncio.sleep` 的等待，
    假时钟推不动它。所以这个套件的 TTL 都取秒级小值。"""
    _task.clear_blocker_providers_for_tests()
    return reset_kernel_for_tests(
        store=RuntimeStore(pathlib.Path(tempfile.mkdtemp()) / "rt.db"))


# ══════════════════════════════════════════════════════════════════════════

async def t_waits_then_resumes() -> None:
    """⭐⭐ 核心：用户放手后 Nano **自己**拿到机器，不需要任何人来叫它。"""
    print("\n[1] ⭐⭐ 等 → 自己恢复（出口是继续，不是失败）")
    k = fresh()
    k.submit(Command(kind=L.ACQUIRE, payload={
        "holder": L.Holder.USER, "reason": "user is typing", "ttl_sec": 2.0}))
    check(L.acquire_activity("nano") is None, "前提：用户持有，Nano 拿不到")

    t0 = time.time()
    h, waited = await L.await_activity_lease("nano", cap_sec=15, poll_sec=0.2)
    el = time.time() - t0

    check(h is not None,
          "⭐⭐ 等到用户的租约到期后**拿到了** —— 这就是「挂起」的出口")
    check(L.current_activity(k).holder == L.Holder.NANO,
          "⭐ 机器回到 Nano 手里")
    check(1.5 <= el <= 6.0,
          "⚠️ 等待时长与用户租约的 TTL 相符（不是立刻返回，也没死等）",
          f"实测 {el:.1f}s / TTL 2s")
    check(abs(waited - el) < 1.0,
          "⭐ 返回的 waited 是**真实等了多久** —— 恢复提示要用它告诉模型",
          f"waited={waited:.1f} 实测={el:.1f}")


async def t_cap_is_graceful() -> None:
    print("\n[2] 上限：等不到就体面收场，不是无限挂着")
    k = fresh()
    k.submit(Command(kind=L.ACQUIRE, payload={
        "holder": L.Holder.USER, "reason": "user busy", "ttl_sec": 999.0}))
    t0 = time.time()
    h, waited = await L.await_activity_lease("nano", cap_sec=1.5, poll_sec=0.2)
    check(h is None, "⭐ 到上限仍拿不到 → 返回 None（调用方据此收场）")
    check(1.0 <= time.time() - t0 <= 4.0, "⚠️ 确实在上限附近返回，没有一直等下去",
          f"{time.time()-t0:.1f}s / cap 1.5s")
    check(L.current_activity(k).holder == L.Holder.USER,
          "⚠️ **没有**因为等不下去就去抢用户的租约")


async def t_no_llm_no_token() -> None:
    """⚠️ 等待必须是纯 sleep —— 否则"等 3 分钟"会变成一笔账。"""
    print("\n[3] ⚠️ 等待期间零 LLM、零 token")
    src = (ROOT / "core" / "runtime" / "oslease.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    seg = ""
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "await_activity_lease":
            seg = ast.get_source_segment(src, n) or ""
    body = "\n".join(l for l in seg.splitlines() if not l.strip().startswith("#"))
    check("asyncio.sleep" in body, "⭐ 用 `asyncio.sleep` 等（不阻塞事件循环）")
    for bad in ("provider", "chat_with_tools", "_vision_ask", "usage_tracker"):
        check(bad not in body, f"⚠️ 等待里没有 {bad}（等待不该产生任何成本）")
    check("acquire_activity(" in body,
          "⭐⭐ **用 acquire 本身当测试**，不是「先查 nano_may_touch_os 再拿」—— "
          "那两步之间用户完全可以再动一次手")


def t_bar_text_six_cases() -> None:
    """⭐⭐ 接管状态条的文案：六格穷举 → 三句话。

    起因：原文案「已让出控制」在 Nano **还在收手**（不可阻断的动作没做完）时
    **断言了一件没发生的事** —— 又是这一整轮反复栽的同一个坑。
    📌 **UI 上的每一句话都必须是「当下为真」的陈述，不能是「即将为真」。**
    """
    print("\n[4] ⭐⭐ 接管状态条的文案：六格穷举 → 三句话")
    import time as _time
    import app as A
    from core.proactive.takeover import USER_HOLD_SEC as HOLD

    class _H:
        def __init__(self, remain):
            self.holder, self.reason = "user", "user click"
            self.held_until = _time.time() + remain

    gui = object.__new__(A.WebUI)

    def _txt(remain, parked):
        L._parked = parked
        try:
            return gui._takeover_text(_H(remain))
        finally:
            L._parked = False

    # 格 1 / 2：用户刚动过（剩余 > HOLD-2），靠 parked 区分
    t1 = _txt(HOLD - 0.5, False)
    t2 = _txt(HOLD - 0.5, True)
    check("我手上这一步做完就停" in t1,
          "⭐ 格1 还在收手 + 刚动过 → 「我手上这一步做完就停」（**不谎称已停**）", t1)
    check("已暂停控制" in t2, "⭐ 格2 已停下 + 刚动过 → 「已暂停控制」", t2)
    check(t1 != t2,
          "⭐⭐ 这两格**必须不同** —— 「已让出控制」在 Nano 还在收手时是假话，"
          "而那正是这一整轮反复栽的坑")

    # 格 3 / 4：停手 ≥2s → 倒计时，**两格共用**
    # ⚠️ 剩余时间**从 HOLD 推，不写死** —— 2026-08-26 `USER_HOLD_SEC`
    #    从 20 调到 12 时，这里原来写死的 `15.0` 当场落到了「刚动过」那一档
    #    （15 > 12-2），于是三条断言一起红。
    # 📌 **一个从参数派生出来的测试值，必须跟着参数走** ——
    #    写死它等于把「这个参数当时是多少」偷偷编进了测试。
    _mid = HOLD - 5.0            # 停手已超 2 秒，且还没到期
    t3 = _txt(_mid, True)
    t4 = _txt(_mid, False)
    check(f"{int(_mid)} 秒内你不再操作" in t3, "格3 已停下 + 停手 → 倒计时", t3)
    check(t3 == t4,
          "⭐⭐⭐ 格3 与格4 **共用同一句** —— 倒计时说的是「条件+后果」，"
          "不声称 Nano 停了（格4 里它确实没停），所以两格都为真。"
          "📌 **状态数和文案数不必一一对应**")
    check("我会继续行动" in t3 and "已停" not in t3,
          "⚠️ 倒计时文案里**不出现「已停」** —— 否则格4 就成了假话")

    # ⭐ 倒计时是**推导**的：用户再动一下自动弹回，不需要额外状态
    check("秒内你不再操作" in _txt(_mid, True)
          and "已暂停控制" in _txt(HOLD, True),
          "⭐⭐ 用户又动了（held_until 跳回 now+HOLD）→ **自动**弹回「刚动过」那一档 —— "
          "不需要记「上次停手时间」，也不可能与真实状态漂移")

    # 2 秒子缓冲的边界
    check("我会继续行动" in _txt(HOLD - 2.1, True), "刚过 2 秒 → 进倒计时")
    check("已暂停控制" in _txt(HOLD - 1.9, True), "还没到 2 秒 → 仍是「刚动过」")

    # 快到期时不许显示 0 秒（显示 0 却还没醒会让人困惑）
    check("1 秒内" in _txt(0.3, True),
          "⚠️ 向上取整 —— 不会显示「0 秒内」却还没醒", _txt(0.3, True))

    # `parked` 必须由 await 自己维护，不靠调用方
    src = (ROOT / "core" / "runtime" / "oslease.py").read_text(encoding="utf-8")
    tree2 = ast.parse(src)
    seg = ""
    for n in ast.walk(tree2):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "await_activity_lease":
            seg = ast.get_source_segment(src, n) or ""
    check("_parked = True" in seg and "_parked = False" in seg,
          "⭐⭐ `parked` 由 `await_activity_lease` **自己**维护 —— "
          "📌 让做那件事的函数自己记录状态，比要求每个调用方都记得设标志可靠得多"
          "（`_os_task_busy` 就是栽在后者上）")
    check("finally:" in seg,
          "⚠️ 用 finally 复位。**这里安全**，因为 `_parked` 只被 UI 读来选文案、"
          "不参与任何决策 —— 对比 `_active_tool_batch_open` 刻意不能用 finally。"
          "📌 同样是「标志要不要在 finally 里复位」，答案取决于它被谁在什么时候读")


def t_wiring() -> None:
    print("\n[5] 接线：出口是「等」而不是「失败」，且**范围与感知一致**")
    orc = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    oc = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))
    tree = ast.parse(orc)

    check("await_activity_lease(" in oc, "⭐⭐ 真的调了轮内等待")
    # ⚠️⚠️ 这条**曾经**比的是两段代码在文件里的**字符位置**（`i_await < i_bail`）。
    #    🔴 把 `os_execute` 的分支体提取成 `_handle_os_execute` 之后，
    #       "收场"那句被搬到了文件更靠前的位置，而"等待"仍在 `_execute_one_tool_call`
    #       里 —— 位置反了，这条从那时起就一直红着（而**执行顺序一点没变**）。
    # 📌 **文件里的先后 ≠ 运行时的先后。** 一条讲执行顺序的断言，绑在文本偏移上
    #    是错的锚点；换成沿调用链看：等待发生在**分派之前**（在
    #    `_execute_one_tool_call` 的开头），而收场发生在 handler **内部**，
    #    所以运行时必然是先等、后收场。
    _own = {}
    for _n in ast.walk(tree):
        if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for _ln in range(_n.lineno, (_n.end_lineno or _n.lineno) + 1):
                _own.setdefault(_ln, _n.name)
    _await_ln = next(i + 1 for i, l in enumerate(orc.splitlines())
                     if "await_activity_lease(" in l and not l.strip().startswith("#"))
    _bail_ln = next(i + 1 for i, l in enumerate(orc.splitlines())
                    if "machine held by another holder" in l)
    check(_own.get(_await_ln) == "_execute_one_tool_call",
          "⭐⭐ 等待发生在**分派之前**（`_execute_one_tool_call` 的开头，"
          "任何工具分支之前）—— 范围是全量的，不只是键鼠动作",
          f"在 {_own.get(_await_ln)} 里")
    check(_own.get(_bail_ln) == "_handle_os_execute",
          "⭐⭐ 而「机器归别人、收场」在 `os_execute` 的 handler **内部** —— "
          "运行时必然先等、等不到才收场。📌 顺序反了就还是闸",
          f"在 {_own.get(_bail_ln)} 里")

    # ⭐⭐⭐ 等待必须在**所有工具**的入口，不能只在 os_execute 分支里。
    # 用户当场指出第一版只做了一半：感知换成全量了，反应还只覆盖键鼠动作。
    # 📌 **感知的范围和反应的范围必须一致** —— 否则用户看到接管状态条却发现 Nano
    #    照样在动，那正是最初诊断出的"UI 语义不一致"。
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_execute_one_tool_call")
    seg = ast.get_source_segment(orc, fn) or ""
    body = "\n".join(l for l in seg.splitlines() if not l.strip().startswith("#"))
    i_fn_await = body.find("await_activity_lease(")
    i_first_branch = body.find('elif name ==')
    if i_first_branch < 0:
        i_first_branch = body.find('if name ==')
    check(0 < i_fn_await < i_first_branch,
          "⭐⭐⭐ 等待在**任何工具分支之前** —— 命令行 / MCP / skill / 截图全覆盖",
          f"await@{i_fn_await} < 第一个分支@{i_first_branch}")
    check("gui_session_active(" in body,
          "⭐⭐ 入口那道等待的条件是「在 GUI 模式」，不是「这个动作碰不碰鼠标」")
    # ⚠️ 反向：不在 GUI 模式时不许等（纯命令行的一轮完全不受影响）
    i_gui = body.find("gui_session_active(")
    check(0 < i_gui < i_fn_await,
          "⚠️ 先判 GUI 模式、再决定要不要等 —— 顺序反了就是每轮都查",
          f"gui@{i_gui} < await@{i_fn_await}")
    check("_waited" in oc and "Resumed after" in oc,
          "⭐ 等过之后会告诉模型「你被打断过、等了多久、别信旧的屏幕认知」")
    # ⚠️ 只匹配**单行内**的片段：这句话在源码里被 f-string 拼接切成了两行，
    #    按完整句子 grep 会红 —— 而运行时字符串是对的。
    # 📌 同"用字符距离取范围"一个问题：**测的是源码排版，不是行为。**
    check("do NOT rely on what you saw" in orc,
          "⚠️ 这是三层防护网第 1 层的兑现（环境不能默认一致）")

    # 🪦 这条原本是「`ActionAttempt` 还没做」的待办哨兵，兜底还去读一份
    #    **不随仓库发行**的内部文档 —— 靠 `or` 短路才一直没炸。
    # ⚠️ 两边都已经过期：`core/runtime/attempt.py` 早就落地，有自己的套件，
    #    orchestrator 里 5 处接线。
    # 📌 **一个「还没做」的哨兵，做完之后必须翻面成「已经接上了」** ——
    #    否则它会一直以「待办」的样子挂着，而没有人会回来核实。
    check("ActionAttempt" in orc,
          "⭐ `ActionAttempt`（说清上一个动作做到哪了）已经接进 orchestrator")


async def _amain() -> None:
    await t_waits_then_resumes()
    await t_cap_is_graceful()
    await t_no_llm_no_token()


def main() -> int:
    asyncio.run(_amain())
    t_bar_text_six_cases()
    t_wiring()
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
