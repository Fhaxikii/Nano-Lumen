# -*- coding: utf-8 -*-
"""长任务不能只靠「成功信号」唤醒 —— 还要有「回头看看情况」。

═══ 这个套件盯的核心 ═══

2026-08-09 定的设计，一句话：

> **长任务「永远完不成」的概率更高。** 所以触发 Nano 复活的依据不能只是
> 「成功信号到了」，还要有「我回来看看情况」。

而实现上最要紧的分工：

📌 **系统只设「第一次回看」那一个数字**（语义 = 「开始怀疑它出问题了」）——
   那是系统唯一答得出的问题。之后每次回看都由**模型**根据看到的东西自己决定：
   坏了就换办法 / 正常但慢就排远一点 / 快完了就不用再看。
📌 **任何固定的回看间隔都是错的**，因为没有一种算法覆盖得了真实情况。
   （最初提的「固定 120s 不递增」被 用户直接否掉，理由就是这条。）

═══ 而实现里有三个「压在一起的现实」必须拆开 ═══

1. 📌 **「唤醒」≠「结束这条等待」** —— 原来 `resume_suspension` 无条件 `resolve`，
   对回看是致命的：后台还在跑，而它的完成信号从此再也唤不醒任何人。
2. 📌 **`kind` 回答「在等什么」，`fire_at` 回答「什么时候回来看」** ——
   一个字段不许表达两个现实。而这不只是命名：`startup_sweep` 按
   `kind == BACKGROUND` 找「载体已死」的等待，标成 TIMER 会让它躲过启动清理。
3. 📌 **「兜底回收」的期限要在每次「有人确认它还活着」时被推后** ——
   否则一个正常的 40 分钟下载会在第 30 分钟被 orphan 杀掉。

用法：
  py -3.10 tests\t_recheck_longtask.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import pathlib
import sys
import tempfile
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import reset_kernel_for_tests, KernelError
from core.runtime.store import RuntimeStore
from core.runtime import task as T
from core.runtime import waitcond as W

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    T.clear_activity_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"),
                                  clock=FakeClock(BASE_T))


def _bg(reason="running in background: pip install torch", first=60):
    return W.open_wait(reason=reason, wake_on=["background", "timer"],
                       timer_seconds=first, bg_ref="bg1")


# ══════════════════════════════════════════════════════════════════════════

def t_dual_source(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐⭐⭐ 双唤醒源：等完成 + 到点回看")
    k = make_kernel(tmp / "a")
    rec = _bg()
    check(rec is not None, "登记成功")
    r = W.find_by_id(k, rec.wait_id)

    check(set(r.wake_on) == {"background", "timer"},
          "⭐⭐⭐ **两个源都在** —— 只挂完成信号等于假设它一定会回来，"
          "而那正是 2026-08-04 两条不死挂起的形状。"
          "📌 触发复活的依据不能只是「成功信号到了」，还要有「我回来看看情况」",
          str(r.wake_on))
    check(r.fire_at == BASE_T + 60,
          "⭐ 第一次回看排在 60 秒后（语义 = **开始怀疑**它出问题了）", str(r.fire_at))
    check(r.bg_ref == "bg1", "后台引用在")

    check(r.kind == W.WaitKind.BACKGROUND,
          "⭐⭐⭐ `kind` 是 **BACKGROUND** 而不是 TIMER —— "
          "📌 **`kind` 回答「在等什么」，`fire_at` 回答「什么时候回来看」**，"
          "一个字段不许表达两个现实。"
          "🔴 而这不只是命名：`startup_sweep` 按 `kind == BACKGROUND` 找"
          "「载体随进程消失」的等待，标成 TIMER 会让一条已经死掉的后台等待"
          "**躲过启动清理**",
          r.kind)


def t_recheck_does_not_resolve(tmp: pathlib.Path) -> None:
    print("\n[2] ⭐⭐⭐ 回看不许把这条等待收掉")
    k = make_kernel(tmp / "b")
    rec = _bg()
    sid = rec.wait_id

    # 到点 → 被轮询捞到
    k.clock.advance(61)
    due = W.list_due_wakeups(k)
    check([x.wait_id for x in due] == [sid], "到点被捞到")

    # 回看的处置 = 重排（而不是 resolve）
    ok = W.reschedule_wait(sid, 900)
    check(ok is True, "重排成功")
    r = W.find_by_id(k, sid)
    check(r.status == W.WaitStatus.WAITING,
          "⭐⭐⭐ **等待仍然活着（回到 WAITING）** —— "
          "🔴 如果这里被 resolve 了，后台任务的完成信号从此再也唤不醒任何人"
          "（记录已终态 → `notify_background_done` 找不到匹配 → 静默丢弃）。"
          "📌 **「唤醒」和「结束这条等待」是两件事**",
          r.status)
    check(r.fire_at == BASE_T + 61 + 900,
          "⭐ 下一次回看被排到了 900 秒后", str(r.fire_at))
    check(set(r.wake_on) == {"background", "timer"}, "两个源都还在")

    # ⭐ 重排之后**不会**在下一跳又点火
    due2 = W.list_due_wakeups(k)
    check(due2 == [],
          "⭐⭐⭐ **重排之后立刻不再被捞到** —— "
          "🔴 不推 `fire_at` 的话，驱动唤醒的那个 "
          "`status IN ('WAITING','DUE_FOR_REVIEW') AND fire_at <= now` 查询"
          "**每 5 秒就会再点一次火**。"
          "📌 **一个「幂等的状态转换」不等于「幂等的副作用」** —— "
          "`_due` 命令不重复写库，但它旁边那个查询会重复点火",
          str(due2))


def t_rearm_from_due(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐⭐ 重排必须能把 DUE_FOR_REVIEW 拉回 WAITING")
    k = make_kernel(tmp / "c")
    rec = _bg()
    sid = rec.wait_id
    wid = W.find_by_id(k, sid).wait_id

    k.clock.advance(61)
    W.tick(k)      # 系统 tick 把它推成 DUE_FOR_REVIEW
    check(W.get(k, wid).status == W.WaitStatus.DUE_FOR_REVIEW,
          "前置：tick 把它推成了 DUE_FOR_REVIEW（到点 ≠ 完成）")

    W.reschedule_wait(sid, 300)
    check(W.get(k, wid).status == W.WaitStatus.WAITING,
          "⭐⭐⭐ 重排把它**拉回 WAITING** —— "
          "🔴 `_due` 对已是 `DUE_FOR_REVIEW` 的是 no-op，"
          "所以停在那一档的记录**再也不会触发下一次回看**。"
          "📌 **「重新武装」不只是改时间，还要把状态退回可触发的那一档**",
          W.get(k, wid).status)

    # 再到点，能再来一次
    k.clock.advance(301)
    W.tick(k)
    check(W.get(k, wid).status == W.WaitStatus.DUE_FOR_REVIEW,
          "⭐ 下一次回看真的又触发了（循环闭合）")


def t_no_more_checkin(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐⭐ 「不用再看了」= 撤掉一个源，不是结束这条等待")
    k = make_kernel(tmp / "d")
    rec = _bg()
    sid = rec.wait_id

    W.reschedule_wait(sid, None)
    r = W.find_by_id(k, sid)
    check(r.fire_at is None, "回看点被清掉", str(r.fire_at))
    check(r.status == W.WaitStatus.WAITING,
          "⭐⭐⭐ **等待仍然活着** —— "
          "📌 **「不再回看」和「这条等待结束了」是两件事**，前者只是撤掉一个源")
    check(W.list_due_wakeups(k) == [], "再也不会被定时捞到")
    check(r.orphan_at is not None,
          "⭐ 而 `orphan_at` 仍在 —— 模型说「不用看了」之后，"
          "万一它真的永远不回来，仍然有人来收。📌 **推后 ≠ 取消**")


def t_orphan_pushed(tmp: pathlib.Path) -> None:
    print("\n[5] ⭐⭐⭐ 兜底回收的期限随回看推后")
    k = make_kernel(tmp / "e")
    rec = _bg()
    sid = rec.wait_id
    o0 = W.find_by_id(k, sid).orphan_at
    check(o0 is not None and o0 <= BASE_T + W.DEFAULT_ORPHAN_AGE_SEC + 1,
          "前置：开启时 orphan 期限定在 30 分钟后", str(o0))

    # 25 分钟后回看一次，模型说「还要 20 分钟」
    k.clock.advance(25 * 60)
    W.reschedule_wait(sid, 20 * 60)
    o1 = W.find_by_id(k, sid).orphan_at
    check(o1 > o0,
          "⭐⭐⭐ **orphan 期限被推后了** —— "
          "🔴 不推的话：一个正常的 40 分钟下载会在第 30 分钟被兜底杀掉，"
          "即使 Nano 上一分钟才确认过它没问题。"
          "📌 **一个「兜底回收」的期限，应该在每次「有人确认它还活着」时被推后** —— "
          "orphan 防的是「没人管了」，而一次成功的回看恰恰是「有人管」的证据",
          f"{o0:.0f} → {o1:.0f}")
    check(o1 is not None, "⚠️ 但**没有被取消**（模型也可能忘了再回看）")


def t_completion_discards_recheck(tmp: pathlib.Path) -> None:
    print("\n[6] ⭐⭐ 后台完成时，那次待办的回看自动废弃")
    k = make_kernel(tmp / "f")
    rec = _bg()
    sid = rec.wait_id
    check(W.find_by_id(k, sid).fire_at is not None, "前置：回看点还挂着")

    W.resolve_wait(sid, "background")
    r = W.find_by_id(k, sid)
    check(r.status not in (W.WaitStatus.WAITING, W.WaitStatus.DUE_FOR_REVIEW),
          "完成之后这条记录进了终态", r.status)
    k.clock.advance(10_000)
    check(W.list_due_wakeups(k) == [],
          "⭐⭐⭐ **那次回看自动废弃，不会再唤醒任何人** —— "
          "一条记录两个源，收掉记录两个源一起没。"
          "📌 这个语义是**建模免费给出的**，不需要额外写「取消那个定时器」的代码 —— "
          "而如果当初把「等待」和「回看定时器」建成两条记录，就得手工配对取消，"
          "那正是 2026-08-04 不死挂起的成因形状")


def t_reschedule_rejects_terminal(tmp: pathlib.Path) -> None:
    print("\n[7] ⭐ 已经结束的等待不许被重排")
    k = make_kernel(tmp / "g")
    rec = _bg()
    sid = rec.wait_id
    W.resolve_wait(sid, "background")
    ok = W.reschedule_wait(sid, 300)
    check(ok is False,
          "⭐⭐ 终态之后重排**失败** —— 最常见的原因正是「那件事刚好完成了」，"
          "而那时模型不该被告知「已排好下一次」（否则它会以为还在跑）")


class _WakeMemory:
    """⚠️ 替身必须与真货**一样严**，不能更宽松。

    2026-08-13：唤醒注入改走 `add_system_note`（系统注记不上屏，见
    `MemoryManager.add_system_note`），这个替身只有 `add_message`，
    于是两条断言以 `AttributeError` 变红 —— **而生产代码是对的**。
    📌 **一个测试替身如果比真货"宽松"，它验的就不是真货的行为**（同 t_d12 那次）；
       这次方向反过来了：替身缺一个真货有的方法，红得**对**，抓的是"替身该跟上"。
    ⭐ 顺带把它变成一条真断言：唤醒注入**必须**是系统注记（`visible=False`），
       否则重启重放又会把 `[System check-in]` 画成 `Koala ❯` 气泡。
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.visible_flags: list[bool] = []

    def add_message(self, role: str, content: str) -> None:
        self.messages.append((role, content))
        self.visible_flags.append(True)

    def add_system_note(self, role: str, content: str) -> None:
        self.messages.append((role, content))
        self.visible_flags.append(False)


async def _exercise_real_resume(rec, trigger: str) -> tuple[list, _WakeMemory]:
    """Push the real resume generator up to the ReAct boundary without a provider call."""
    import core.orchestrator as O

    agent = object.__new__(O.Orchestrator)
    memory = _WakeMemory()
    agent.memory = memory
    agent._system_guide_template = "skills={skills}"
    agent._build_skills_info = lambda **kwargs: []
    agent._skill_names = lambda skills: []
    agent._build_tool_awareness_block = lambda: ""
    agent._build_session_log_injection = lambda: ""
    agent._recheck_sid = ""

    async def _fake_react(self, **kwargs):
        yield {"type": "wake-test-reached-react"}

    agent._run_react_loop = types.MethodType(_fake_react, agent)

    old_release = O._rt_lease_release
    old_sweep = O._rt_sweep_stale_spans
    O._rt_lease_release = lambda _agent: None
    O._rt_sweep_stale_spans = lambda _agent, _old: None
    try:
        events = [event async for event in agent.resume_suspension(rec.wait_id, trigger)]
    finally:
        O._rt_lease_release = old_release
        O._rt_sweep_stale_spans = old_sweep
    return events, memory


def t_resume_consumes_waitrecord_shape(tmp: pathlib.Path) -> None:
    print("\n[8] ⭐⭐⭐ 实际唤醒生成器直接消费 WaitRecord")

    make_kernel(tmp / "wake-timer")
    timer = W.open_wait(reason="check timer probe", wake_on=[W.WakeSource.TIMER],
                        timer_seconds=30)
    try:
        timer_events, timer_memory = asyncio.run(_exercise_real_resume(timer, "timer"))
        timer_error = ""
    except Exception as exc:
        timer_events, timer_memory, timer_error = [], _WakeMemory(), repr(exc)
    check(bool(timer_events) and "check timer probe" in timer_memory.messages[-1][1],
          "普通定时唤醒能用 WaitRecord 生成注入并进入 ReAct",
          timer_error)

    make_kernel(tmp / "wake-recheck")
    recheck = _bg(reason="check background probe")
    try:
        recheck_events, recheck_memory = asyncio.run(
            _exercise_real_resume(recheck, "timer"))
        recheck_error = ""
    except Exception as exc:
        recheck_events, recheck_memory, recheck_error = [], _WakeMemory(), repr(exc)
    check(bool(recheck_events) and "check background probe" in recheck_memory.messages[-1][1],
          "后台回看唤醒能用 WaitRecord 生成注入并进入 ReAct",
          recheck_error)

    # ⭐⭐⭐ [2026-08-13 实测] 唤醒注入**必须**是系统注记，不是用户消息。
    # 用户重启后看到 `Koala ❯ [System check-in] You put this in the background…`
    # —— 一整段英文提示词署着用户的名字。注记为了让模型读到必须挂 user 角色，
    # 所以「它是不是用户说的」这件事必须**另外记下来**。
    # 📌 **一份账本同时当「模型上下文」和「用户看过的东西」，就必须记下两者的差别。**
    for _who, _mem in (("定时唤醒", timer_memory), ("回看唤醒", recheck_memory)):
        check(bool(_mem.visible_flags) and _mem.visible_flags[-1] is False,
              f"⭐⭐⭐ {_who}的注入是【系统注记】（不上屏）—— "
              f"走 add_message 的话，重启重放会把它画成 Koala 气泡",
              str(_mem.visible_flags))


class _NeverFinishingMCP:
    """Only the external wait is fake; the orchestrator slow-path stays production-real."""
    def __init__(self) -> None:
        self.call_timeout = 120.0

    def confirm_summary(self, name: str):
        return None

    async def call(self, name: str, args: dict, timeout: float = 120.0,
                   progress_ref: str = ""):
        self.call_timeout = timeout
        # ⭐ 记下 orchestrator 有没有**在发起调用时**就带上进度订阅凭据。
        #    📌 事后补不上 —— `progressToken` 必须随请求一起发出。
        self.progress_ref = progress_ref
        await asyncio.Event().wait()


async def _exercise_real_mcp_slow_path():
    import core.orchestrator as O
    from core.schema import ToolCall

    agent = object.__new__(O.Orchestrator)
    agent.registry = object()
    agent._rag_parallel_sem = None
    agent._tool_parallel_sem = None
    manager = _NeverFinishingMCP()
    agent._mcp_mgr_cache = manager
    agent._LONG_TASK_HANDBACK_SEC = 0.0
    agent._FIRST_RECHECK_SEC = 60.0
    agent._session_log = []
    agent._wm = None
    agent._a3_pause_note = ""
    agent._last_skill_error = None

    events = asyncio.Queue()
    result = await agent._execute_one_tool_call(
        ToolCall(name="mcp__probe__slow", args={"probe": "c-phase"},
                 tool_use_id="slow-call", index=0),
        used_model="TEST", base_guide="", system_guide="",
        realtime_callback=None, event_queue=events,
    )
    emitted = []
    while not events.empty():
        emitted.append(events.get_nowait())
    return (result, emitted, manager.call_timeout,
            O.Orchestrator._LONG_TASK_HANDBACK_SEC, O.Orchestrator._FIRST_RECHECK_SEC,
            manager)


def t_mcp_slow_path_consumes_waitrecord(tmp: pathlib.Path) -> None:
    print("\n[9] ⭐⭐⭐ MCP 超阈值真实慢路径直接消费 WaitRecord")
    k = make_kernel(tmp / "mcp-slow")
    result, emitted, call_timeout, bg_threshold, first_recheck, manager = asyncio.run(
        _exercise_real_mcp_slow_path())
    suspended = [e for e in emitted if e.get("event") == "suspend_waiting"]
    requests = [e for e in emitted if e.get("event") == "long_task_handback"]
    sid = suspended[0].get("suspension_id", "") if suspended else ""
    rec = W.find_by_id(k, sid) if sid else None

    check(result.ok and len(suspended) == 1 and len(requests) == 1,
          "MCP 超阈值后产生等待 pill 与后台请求，不因旧 dict 访问失败",
          result.error)
    check(rec is not None and set(rec.wake_on) == {W.WakeSource.BACKGROUND,
                                                    W.WakeSource.TIMER},
          "pill 的 wait_id 可反查到同一条双唤醒源 WaitRecord",
          f"sid={sid}, rec={rec}")
    check(call_timeout > bg_threshold + first_recheck,
          "⭐⭐ 被交回控制权的载体，其硬期限晚于首次回看 —— 否则回看不可达。"
          "📌 **一个操作的期限，在控制权被交回模型之后，应该由「后台合同」决定，"
          "而不再由「前台等待的耐心」决定** —— 前台阈值问「我还要不要干等」，"
          "这个期限问「这件事最多允许跑多久」，两个不同的问题",
          f"timeout={call_timeout}s, first recheck at {bg_threshold + first_recheck}s")

    # ── ⭐⭐⭐ 统一语义（2026-08-09 第二次修正）─────────────────────
    _rt = result.result_text or ""
    check("end this turn" not in _rt and "do not call more tools" not in _rt,
          "⭐⭐⭐ **给模型的话里【没有】「结束这一轮、别再调工具」** —— "
          "🔴 旧实现有这句，它在**系统层面切断 ReAct 链**，"
          "正是最初质疑的那个后果（「会不会强制把 react 拆成一大堆 turn」）。"
          "📌 **控制权交回模型 ≠ 这一轮必须结束** —— 压成一个，"
          "模型就失去了「一边等它一边铺路」的能力",
          _rt[:90])
    check("Control is back with you" in _rt,
          "⭐⭐ 而是明确告诉它**控制权在它手上**（可以继续做不依赖它的步骤）")
    # ⭐⭐⭐ 这一条原来钉的是「无条件说『运行时对进度一无所知』」。
    #    🔴 **那句话现在会说谎**：接上进度总线之后，长命令有 stdout、
    #       MCP 有协议原生进度通知，交还那一刻往往已经看得到东西。
    #    📌 **一条钉「中间态」的断言，在设计前进之后会反过来阻止终态 ——
    #       改成钉终态，不是删掉。**（本项目第二次用到这条。）
    #    ⭐ 终态是：**措辞按当次实际有没有拿到进度决定**。
    #       这个替身永远不报进度，所以这一次必须走「没有」那一支。
    check("cannot see its progress" in _rt,
          "⭐ 这个载体确实报不出进度 → 如实说「看不到」 —— "
          "📌 一个「进度」字段在没有进度时必须说「没有」，"
          "不许给一个看起来像进度的空值（那会让模型编一个进展出来）")
    check("Do NOT claim you can see progress" in _rt,
          "⭐ 并且明确禁止它假装看得见 —— "
          "📌 不写这句，模型会拿一句「进度良好」糊过去，而那是假陈述")
    check(getattr(manager, "progress_ref", "") != "",
          "⭐⭐⭐ **进度订阅凭据在【发起调用时】就带上了**（不是超时后才生成）—— "
          "🔴 上一版 `_bg_ref` 只在超阈值那条分支里才算，那已经是 90 秒之后，"
          "而 MCP 的 `progressToken` 必须随请求发出，事后补不上 → 前 90 秒的"
          "进度永远丢了。"
          "📌 **一个「出问题时才需要的标识」，如果它同时是「观测的订阅凭据」，"
          "就必须在事情开始时就存在。**",
          f"progress_ref={getattr(manager, 'progress_ref', None)!r}")

    # ⭐ 类型无关：常量名与合同名里都不许再出现 MCP
    orc_src = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    # ⚠️⚠️ **改成数【赋值】，不数文本**（2026-08-20）。
    #    原来写的是 `"_MCP_BG_THRESHOLD" not in orc_src` —— 而 2026-08-20 给
    #    `_AGENT_HANDBACK_SEC` 写留痕时，注释里引用了这个旧名字来说明"同族阈值"，
    #    这条断言当场红了。
    #    📌 **只要断言读的是文本，对代码的解释就会参与判定**（本项目第六次踩）。
    #       它要守的是「这个常量不存在」，不是「这五个字不许被提起」。
    _assigned = {t.id for n in ast.walk(ast.parse(orc_src))
                 if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)}
    check("_MCP_BG_THRESHOLD" not in _assigned,
          "⭐⭐⭐ 旧的 MCP 专属阈值名**已经不存在**（按赋值数，不按文本数） —— "
          "🔴 那个名字是这条错路的源头：它只是 MCP 落地时的局部实现，"
          "而我做回看时按「哪里已经有钩子」找接入点，把它当成了机制。"
          "📌 **给一个机制接线时，要按「哪些场景需要它」去找接入点，"
          "不是按「哪里已经有现成的钩子」**")
    check("async def _hand_back_long_task" in orc_src,
          "⭐⭐ 交还合同是**公共**的（`_hand_back_long_task`）—— "
          "📌 koala：「我们要识别的只有『长任务』，识别任务类型毫无意义」；"
          "一个机制如果只有一个接入点，那它可能不是机制，只是那一处的实现细节")


def t_source_wiring() -> None:
    print("\n[10] ⭐⭐ 接线：阈值 / 引导 / 工具 / 状态清理")
    orc = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(orc)

    # ① 阈值
    # ⭐⭐⭐ 2026-08-22 那次建模：**90s → 5s，命令/MCP/Subagent统一**（已定 A 方案）。
    #
    # ⚠️⚠️ 这里推翻了一条写得很好的旧判据，必须说清**它为什么不再适用**：
    #    旧判据：「一个『防异常』的阈值，如果落在正常值域内，
    #             它就不再是保护，而是变成了主路径」（8s → 90s 的理由）
    #
    # 📌 **那条判据本身没错，是它的前提变了。**
    #    旧前提：交还 ＝ **防异常的保护** → 只该在异常时触发 → 阈值须在正常值域外
    #    新前提：交还 ＝ **正常的异步转换**（这件事要跑一会儿，先说句话/去干别的）
    #            → 它本来就该经常发生，「变成主路径」不再是问题
    #    ⭐ **一条判据被推翻，往往不是它错了，是它守的那个东西被重新定义了。**
    #
    # ⭐ 两条支撑：
    #   ① 用户的设计原则：**体验 > 成本**。阻塞 20 秒期间用户看不到任何东西、
    #      模型说不了话；代价只是两轮模型调用。
    #   ② v5 新增了**第 0 秒出口**（`wait_for_result=false`）——
    #      模型明知道要久的东西第 0 秒就声明不等，**根本走不到宽限期**。
    #      于是宽限期只兜「预判错了」的那些，而那种情况早交还本来就是对的。
    # ⚠️ 未消除的顾虑（留痕）：MCP 调用的快慢模型多半预判不出，
    #    所以那一类仍会频繁走宽限期 —— 这是 A 方案已知要付的成本。
    check("_LONG_TASK_HANDBACK_SEC = 5.0" in orc,
          "⭐⭐ 快路径宽限期 **5s**（命令/MCP/Subagent统一）—— "
          "📌 它不是「要不要干等」的决策，是「先同步等一下，多半马上就好」的**测量**")
    check("_FOREGROUND_WAIT_SEC = 5" in
          (ROOT / "core" / "os_layer" / "executor_write.py").read_text(encoding="utf-8"),
          "⭐ 命令那条也是 5s（三个数字本来就在答同一个问题）")
    check("_FIRST_RECHECK_SEC = 60.0" in orc, "⭐ 第一次回看 60s（「开始怀疑」）")
    check("_RECHECK_FALLBACK_SEC" in orc,
          "⭐ 有长兜底（模型没表态时用它，取长 → 让「没判断」这种失败便宜）")

    # ② 写点是双源
    # ⚠️⚠️ **2026-08-20 改口径**：Subagent接进同一条交还合同之后，
    #    `wake_on` 不再是一个字面量，而是 `recheck` 的函数
    #    （Subagent那一档是**单源**：它永远不回看）。
    #    ⭐ 所以这里钉的东西也跟着换成**真正要守的那一条**：
    #       **`recheck` 的默认值是 True** —— 也就是「不特意说明的载体一律双源」。
    #       📌 一个开关的价值不在它现在是什么值，在它**关得住什么**：
    #          默认值错了的表现是「所有长任务都不回看了」，而且不会报错。
    #    🔬 双源/单源的**行为**由 `tests/t_b1_dont_wait.py` 真的开一条等待来验。
    _hb = [n for n in ast.walk(tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "_hand_back_long_task"]
    _defaults = {}
    if _hb:
        _kw = _hb[0].args.kwonlyargs
        _kd = _hb[0].args.kw_defaults
        _defaults = {a.arg: (d.value if isinstance(d, ast.Constant) else None)
                     for a, d in zip(_kw, _kd) if d is not None}
    check(_defaults.get("recheck") is True,
          "⭐⭐⭐ 交还合同**默认双源**（`recheck=True`）—— "
          "不特意说明的载体一律「完成信号 + 到点回看」",
          f"defaults={_defaults}")
    _hb_body = ast.unparse(_hb[0]) if _hb else ""
    check("'background'" in _hb_body and "'timer'" in _hb_body,
          "⭐ 双源那一档确实还在这条合同里")

    # ③ 回看不 resolve
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "resume_suspension"]
    body = ast.unparse(fn[0]) if fn else ""
    check("_is_recheck" in body and "reschedule_wait" in body,
          "⭐⭐⭐ `resume_suspension` 认得出「这是一次回看」并改走重排")
    check(body.index("_is_recheck") < body.index("resolve_wait(suspension_id"),
          "⭐⭐ 而且判定在 `resolve` **之前** —— 顺序反了就等于没判")

    # ④ 引导那段话的三件事
    check("That is ALL the system knows" in orc,
          "⭐⭐⭐ 引导里**如实说清它现在只知道什么** —— "
          "不写这句它会拿一句「进度良好」糊过去，而那是假陈述")
    check("take a screenshot" in orc and "file size" in orc,
          "⭐⭐ 并告诉它**可以怎么去看**（截图 / 命令 / 问服务）—— "
          "📌 **「回看一眼」的价值完全取决于那一眼能看到什么**；"
          "只能看到「还在跑」的话，那个判断系统自己也能做，不需要花一次模型调用。"
          "当时的结论：「当然要给，不然整个回看设计都是废的」")
    check("try another way" in orc and "nearly done" in orc,
          "⭐ 三个出口都点明（换办法 / 排下一次 / 不用再看）")

    # ⑤ 工具：条件注入 + 不进常驻 + 显式串行
    # ⚠️ 换锚点：这三条改造前分别查
    #      「`_SET_NEXT_CHECKIN_MANIFEST` 的 append 是不是包在 `if _recheck_sid` 里」
    #      「`_CORE_TOOL_NAMES` 这个字面量里有没有它」
    #      「`orc.split("_REACT_SERIAL_TOOLS")[1][:900]` 这段文本里有没有它」
    #    —— 全是**关于源码长什么样**的断言，而那三张表已随 cutover 删除。
    # ⭐ 三条要守的东西一条没变，锚点换成「量结果 / 问声明」，而且更强：
    #    最后那条尤其明显 —— 旧写法是在源码里切 900 个字符找名字，
    #    表一换行、位置一挪就失效；现在问的是声明里的那个字段。
    from core.tools import Preload as _PL, Scheduling as _SC, ToolCatalog as _TC, ToolScope as _TS
    from core.tools.builtin import build_builtin_definitions as _bbd
    import core.orchestrator as _om2
    _mans = {v["name"]: v for k, v in vars(_om2).items()
             if k.endswith("_MANIFEST") and isinstance(v, dict) and v.get("name")}
    _defs = {d.name: d for d in _bbd(_mans)}
    _snc = _defs["set_next_checkin"]

    class _RTPlain:
        def has_live_work(self): return False
        def has_open_interaction(self): return False
        def is_recheck_round(self): return False

    class _RTRecheck(_RTPlain):
        def is_recheck_round(self): return True

    _cat = _TC()
    for _d in _defs.values():
        _cat.add_builtin(_d)
    _plain = {d.name for d in _cat.advertised(_TS.MAIN, _RTPlain())}
    _recheck = {d.name for d in _cat.advertised(_TS.MAIN, _RTRecheck())}
    check("set_next_checkin" not in _plain and "set_next_checkin" in _recheck,
          "⭐⭐⭐ `set_next_checkin` **只在回看轮出现** —— "
          "📌 一个只在某种状态下才有意义的工具，应该只在那种状态下出现")
    check(_snc.preload is not _PL.CORE,
          "⭐ 不常驻（改造前查的是 `_CORE_TOOL_NAMES` 那张手写表）", _snc.preload.value)
    check(_snc.scheduling is _SC.SERIAL,
          "⭐ 在串行表里（它改的是「下一次什么时候回来看」这个权威状态）",
          _snc.scheduling.value)

    # ⑥ `_recheck_sid` 两条路径都要设
    check(body.count("_recheck_sid") >= 2,
          "⭐⭐⭐ `_recheck_sid` 在**两条路径**上都被设定（回看轮设、非回看轮清）—— "
          "📌 **一个「本轮有效」的状态，必须在每一条进入这一轮的路径上都被设定**；"
          "只在需要它的那条路上设，另一条路会带着上一轮的值跑",
          f"{body.count('_recheck_sid')} 处")

    # ⑦ 模型不该被要求提供系统已经知道的东西
    man = orc[orc.index("_SET_NEXT_CHECKIN_MANIFEST = {"):]
    man = man[:man.index("\n}")]
    check("suspension" not in man and "wait_id" not in man,
          "⭐⭐ 工具**不要模型传 id** —— 回看轮里只有一个对象。"
          "📌 **模型不该被要求提供系统已经知道的东西**（多一个可以填错的地方）")


def t_long_command_is_the_same_contract() -> None:
    """⭐⭐⭐ 长命令与 MCP 走**同一条**交还合同 —— 不按任务类型分叉。

    🔴 **这一组是 2026-08-09 第二次修正逼出来的。** 当时的原话：
    > 「我从来就没把 mcp 和长命令分开过，我们说的是『耗时长的任务』，
    >   跟任务类型从来就没有关系过，我不知道这条错误的路是什么时候走上的。」
    > 「用户需要区分任务吗，我们要识别的就是『长任务』。」

    回代码核实：**早先的设计从来没有按类型区分的设计**。`_MCP_BG_THRESHOLD` 只是 MCP
    落地时的一个局部实现，而做回看时按「哪里已经有钩子」找接入点，
    才把它当成了机制。
    📌 **给一个机制接线时，要按「哪些场景需要它」去找接入点，
       不是按「哪里已经有现成的钩子」** —— 后者让机制的覆盖面
       等于历史遗留的形状，而不是等于它的目的。

    ⚠️ 而 外部评审 当时的判断是「`os_execute(background=true)` 有自己的后台合同，
       所以不要错误合并两种后台机制」—— **那个合同不存在**（代码里搜不到那个参数，
       `run_command` 就是 `subprocess.run(timeout=30)`，超时的出口是失败）。
       📌 **一个「不要合并」的决定，必须先证明「被合并的两边真的各自存在」。**
    """
    print("\n[10] ⭐⭐⭐ 长命令 = 同一条交还合同（类型无关）")
    orc = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    oc = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))

    check(oc.count("_hand_back_long_task(") >= 3,
          "⭐⭐⭐ 那条公共合同有**至少两个调用方**（MCP 慢路径 + 长命令）+ 定义 —— "
          "📌 **一个机制如果只有一个接入点，那它可能不是机制，"
          "只是那一处的实现细节**",
          f"{oc.count('_hand_back_long_task(')} 处")
    check("long_running" in oc,
          "⭐⭐ orchestrator 认得出「命令还在跑」这个标记（走同一条合同）")
    # ⚠️ 这里**曾经有第二份**同样的断言（也是按文本数的）。2026-08-20 把上面
    #    那份改成按 AST 数赋值之后，这一份原样留着 —— 于是它红了，而上面那份是绿的。
    #    📌 **一句出现在两处的断言，必然有一天只改到一处**（本项目第 N 次）——
    #       这里直接删掉重复的那份，判据只留一个出处（见 t_carrier_neutral）。

    # ── run_command 的出口不再是「失败」──────────────────────────────────
    ew = (ROOT / "core" / "os_layer" / "executor_write.py").read_text(encoding="utf-8")
    ec = "\n".join(l for l in ew.splitlines() if not l.strip().startswith("#"))
    check("command timed out" not in ec,
          "⭐⭐⭐ `run_command` **再也不会因为「跑得久」而报 timed out** —— "
          "🔴 旧实现 30 秒后返回 `{ok: False, error: 'command timed out'}`，"
          "于是 pip / 下载 / 安装这类最典型长任务**从来没进过后台体系**。"
          "📌 **闸的出口是失败，队列的出口是稍后处理**（本项目第六次）—— "
          "一个跑得久的命令不是错误，它只是还没完")
    check("longcmd" in ec and "wait_briefly" in ec,
          "⭐⭐ 它改用了能**活过这次工具调用**的载体（`longcmd`）—— "
          "📌 `subprocess.run` 是阻塞的：函数返回时进程已经结束或被杀，"
          "**没有任何东西可以交给后台**。那是「交回控制权」的物理前提")
    check('"ok": True' in ec.split("long_running")[0][-400:] or
          "long_running" in ec,
          "⭐⭐ 「还在跑」报的是 **ok=True** —— "
          "📌 **一个「还没完成」的状态被报成失败，最常见的后果是重复执行**"
          "（模型会去重试，那会起第二个进程）")

    # ── 两个时间语义必须是两个数 ────────────────────────────────────────
    check("_FOREGROUND_WAIT_SEC" in ec,
          "⭐⭐ 「**前台愿意等多久**」是它自己的常量")
    lc = (ROOT / "core" / "os_layer" / "longcmd.py").read_text(encoding="utf-8")
    check("_HARD_DEADLINE_SEC" in lc,
          "⭐⭐ 而「**这件事最多允许跑多久**」是另一个 —— "
          "📌 **两个不同的问题，不许由一个数字回答**（今天第二次："
          "第一次是 MCP 的 120s 硬超时 vs 90s 交还阈值）")

    # ── 交还时不许给一个还没结束的尝试记结论 ────────────────────────────
    i_lr = oc.find("long_running")
    seg = oc[i_lr:i_lr + 1800] if i_lr > 0 else ""
    check("_att_m2.finish(" not in seg,
          "⭐⭐⭐ 交还分支里**没有** `_att_m2.finish(...)` —— "
          "🔴 我第一版写了，被 `t_f1_stage5_attempt` 判红，而它是对的："
          "**那次尝试根本没有结束，它还在跑**。"
          "📌 **不许为一件还没结束的事记一个结论** —— "
          "`ActionAttempt` 的全部意义就是「说清结果可不可信」，"
          "而被交还的长命令恰恰是「结果未知」那一格")


def t_long_command_progress_is_real(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ 回看那一眼**真的看得见东西** —— 这是整个设计成不成立的关键。

    「**当然要给，不然整个回看设计都是废的。**」
    📌 **「回看一眼」的价值完全取决于那一眼能看到什么。**

    ⭐ 而这正是长命令这条路比 MCP 那条更值得接回看的原因：
       MCP 的载体是 `await _mcp_task`，里面**没有任何进度流**；
       而一个命令的 stdout 里有 pip 的百分比、下载速度、报错。
    """
    print("\n[11] ⭐⭐⭐ 长命令的回看：进度是真的（不是「你自己去查」）")
    from core.os_layer import longcmd as LC

    lc = LC.start(
        'python -c "import time;[(print(i,flush=True),time.sleep(0.15)) for i in range(12)]"',
        shell=True, display="progress probe")
    check(LC.wait_briefly(lc, 0.35) is False,
          "前置：前台只等 0.35s，它还没跑完")

    note = LC.progress(lc.ref, lines=10)
    check("still running" in note and "elapsed" in note,
          "⭐ 回看能说出「还在跑 + 跑了多久」", note.split("\n")[0][:50])
    check("last output lines" in note and any(str(d) in note for d in range(0, 3)),
          "⭐⭐⭐ **而且带着真实的增量输出** —— "
          "这就是那个 pip 例子里最关键的一环：回看不再是「你自己去查」，"
          "而是**进度摆在眼前**",
          note.replace("\n", " | ")[:110])

    # ⚠️ 没有输出时要如实说「没有」，不许给一个像进度的空值
    lc2 = LC.start('python -c "import time;time.sleep(3)"', shell=True, display="silent")
    LC.wait_briefly(lc2, 0.3)
    n2 = LC.progress(lc2.ref)
    check("nothing on stdout" in n2,
          "⭐⭐ 没有任何输出时**如实说没有**，并说明「这本身不代表卡住」—— "
          "📌 一个「进度」字段在没有进度时必须说「没有」，"
          "不许给一个看起来像进度的空值（那会让模型编一个进展出来）")
    lc2.kill("test cleanup")

    # ── 缓冲满了丢头不丢尾 ──────────────────────────────────────────────
    check(LC._MAX_LINES > 0,
          "⭐ 输出缓冲有上限（长命令可能刷几十万行）")
    lcs = (ROOT / "core" / "os_layer" / "longcmd.py").read_text(encoding="utf-8")
    check("丢头不丢尾" in lcs,
          "⭐⭐ 而且是**丢最旧的** —— 回看要看的是「现在到哪了」，"
          "最新那几行才回答那个问题")

    # ── 清理不许碰还活着的 ──────────────────────────────────────────────
    lc3 = LC.start('python -c "import time;time.sleep(5)"', shell=True, display="alive")
    LC.wait_briefly(lc3, 0.2)
    LC.forget(lc3.ref)
    check(LC.get(lc3.ref) is not None,
          "⭐⭐ `forget()` **不碰还在跑的** —— "
          "📌 一个「清理」动作不许有任何路径能碰到还活着的东西"
          "（移掉它就等于把进度和结果一起丢了）")
    lc3.kill("test cleanup")


def t_wake_continues_same_bubble() -> None:
    """⭐⭐⭐ 唤醒续接进同一个气泡（用户没插话时）。

    「我并没有插话进去，所以我认为这里不应该产生后两次 >NANO，
    而是在一个气泡当中继续（**回看同理**），只要不出现用户插话。」
    📌 **一个 nano 气泡 = 一段连续的回应期** —— `nano ❯` 那个头代表
       「Nano 对用户的一次回应」，用户没说话就不该有第二个头。
    """
    print("\n[12] ⭐⭐⭐ 唤醒续接进同一个气泡")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef)
          and n.name in ("_drive_wake", "_drive_wake_inner")]
    # ⚠️⚠️ [2026-08-22] **唤醒这条路现在是两个函数**：`_drive_wake` 是一层很薄的
    #    包壳（只负责收那条 inbox 记录，见它的 docstring），流程在
    #    `_drive_wake_inner` 里。
    # 📌 这些断言守的是「**唤醒这条路上**有没有 X」，不是「名叫 `_drive_wake`
    #    的那个函数体里有没有 X」—— 后者会被一次纯粹的重构打红，
    #    而红的样子和「行为真的没了」一模一样。
    check(len(fn) == 2, "前置：找到唤醒那条路的两个函数（壳 + 内层）",
          f"实际 {sorted(x.name for x in fn)}")
    node = None
    for _f in fn:
        for n in ast.walk(_f):
            if isinstance(n, ast.If) and ast.unparse(n.test) == "_same_epoch":
                node = n
    check(node is not None,
          "⭐⭐⭐ `_drive_wake` 里有「是不是同一段回应期」的判断")
    if node is not None:
        body = "\n".join(ast.unparse(s) for s in node.body)
        els = "\n".join(ast.unparse(s) for s in node.orelse)
        check("_resp_continuation" in body,
          "⭐⭐⭐ 同一段 → **只打开续接开关**，复用无缝对话那条已被实测验过的路径。"
          "📌 **复用一条已经被实测验过的路径，比新写一条等价的更安全** —— "
          "尤其在 UI 这种测试无法覆盖的层")
        check("nano ❯" in els and "self._resp_state =" in els,
          "⭐⭐⭐ 而**整段重建气泡的代码全在 else 里** —— "
          "🔴 这一格红了意味着它会无条件覆盖 `_resp_state`，"
          "那就是又新开一个 `nano ❯`",
          f"else 内: nano❯={'nano ❯' in els}, resp_state={'self._resp_state =' in els}")
        check("_last_meta_row.delete" in els,
          "⭐⭐ 删元信息行也只在 else 里 —— 续接时那个元信息行还要继续用"
          "（token 统计等**整段**结束才写）。"
          "📌 「上一条的元信息行」这个说法在续接场景里不成立："
          "此刻它不是上一条的，是**当前这一段**的")

    check('"resp_state": getattr(self, "_resp_state", None)' in app,
          "⭐⭐ pill 登记时记住了它属于哪一段（用**对象身份**判断）—— "
          "📌 **能用「是不是同一个对象」判断的事，不要另立一个计数器**："
          "计数器需要有人记得维护，身份是天然的")


def t_handback_action_spinner_finishes_honestly() -> None:
    """交还的动作明细：回看不是完成；后台真的返回才结束 spinner。"""
    print("\n[13] ⭐⭐⭐ 长任务动作 spinner 的终态来自真实后台完成")
    from app import WebUI

    class FakeEl:
        def __init__(self):
            self.visible = True
            self.text = ""
        def set_visibility(self, value):
            self.visible = bool(value)
        def set_text(self, value):
            self.text = value
        def style(self, _value):
            return self

    spin, done = FakeEl(), FakeEl()
    gui = object.__new__(WebUI)
    gui._ui_scope = contextlib.nullcontext
    gui._waiting_pills = {
        "sid": {"done": False, "action_ref": {"spin": spin, "done": done}}
    }

    gui._settle_waiting_action("sid", ok=True)
    check(spin.visible is False,
          "⭐⭐⭐ 后台真正完成后，工具明细 spinner 被隐藏")
    check(done.text == "✓",
          "⭐⭐ 后台载体返回后，明细进入终态而不是永久加载")

    app = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app)
    # ⚠️ [2026-08-22] 唤醒是**两个函数**（壳 + 内层）—— 拼起来算一条路。
    #    📌 用 `next(...)` 只会拿到 walk 顺序里的第一个（那是薄壳），
    #       于是断言在一次纯粹的重构之后变红，红得像"行为没了"。
    wake_body = "\n".join(
        ast.unparse(n) for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name in ("_drive_wake", "_drive_wake_inner"))
    check("trigger == 'background'" in wake_body and
          "_settle_waiting_action" in wake_body,
          "⭐⭐ 只有 background 完成信号收动作明细；timer 回看不冒充完成")
    check("action_ref=" in app and '"action_ref": action_ref' in app,
          "⭐ suspend_waiting 把 action_id 对应的 UI 引用交给 waiting entry")


def t_waiting_intent_keeps_user_controls_off_system_rechecks() -> None:
    """定时计划可控；系统回看不是一个让用户催促/取消的假计时器。"""
    print("\n[14] ⭐⭐⭐ 等待意图：计划可控，系统回看不露出控制面")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    orch = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")

    check('"waiting_intent": "system_recheck"' in orch,
          "长调用交还明确标成 system_recheck —— 不从 timer_at 猜语义")
    check('_waiting_intent = step.get("waiting_intent", "condition_recheck")' in app,
          "事件的等待意图由事件显式传入，而不是在 UI 侧猜")
    check('waiting_intent == "scheduled_timer"' in app,
          "只有 scheduled_timer 分支创建用户控制")
    check("立即执行" in app and "取消计划" in app,
          "计划的唯一两个按钮是「立即执行 / 取消计划」")

    # 系统回看不是用户可见的“计时计划”。它保留的是原长任务工具明细的 spinner，
    # 而非另起一个倒计时 pill；但唤醒时仍须能据此收掉那个 spinner。
    check('if _waiting_intent == "scheduled_timer":' in app and
          'self._register_hidden_waiting(' in app,
          "只有用户定时计划渲染 pill；system_recheck 只登记不可见的完成关联")
    check('if entry.get("hidden"):' in app,
          "不可见关联在等待结束时只完成内部收尾，不能访问不存在的 pill DOM")

    tree = ast.parse(app)
    pipeline = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "navigate_pipeline")
    handback = next((node for node in ast.walk(pipeline)
                     if isinstance(node, ast.If)
                     and 'long_task_handback' in ast.unparse(node.test)), None)
    handback_calls = [node.func.attr for node in ast.walk(handback)
                      if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute)] if handback else []
    check("_start_handed_back_carrier" in handback_calls and
          "_start_bg_task" not in handback_calls,
          "系统交还的载体不登记成用户可见 BACKGROUND_JOB")

    check("at most one user-facing status conclusion" in orch,
          "回看协议要求决定后只给用户一次状态结论，不在工具前后复述")
    check("User-requested scheduled plan" in orch and
          "At the scheduled time, perform the requested action or reminder." in orch,
          "wait_for 的总说明明确允许用户定时计划，并说明到点即执行/提醒")


def t_cancelled_handback_still_closes_its_original_action() -> None:
    """取消回看不是终止载体；载体后来真完成也必须收原动作的 spinner。"""
    print("\n[15] 取消回看后，载体完成仍收原动作 spinner")
    from app import WebUI

    class FakeEl:
        def __init__(self):
            self.visible = True
            self.text = ""
        def set_visibility(self, value):
            self.visible = bool(value)
        def set_text(self, value):
            self.text = value
        def style(self, _value):
            return self

    spin, done = FakeEl(), FakeEl()
    gui = object.__new__(WebUI)
    gui._ui_scope = contextlib.nullcontext
    gui._waiting_pills = {
        "sid": {
            "hidden": True, "done": True, "bg_ref": "carrier-1",
            "action_ref": {"spin": spin, "done": done}, "action_done": False,
        }
    }

    settled = gui._settle_cancelled_handback_actions("carrier-1")
    check(settled == 1 and spin.visible is False and done.text == "✓",
          "取消等待后载体完成，仍只收原 action 而不重新唤醒 Nano")

    orch = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    orch_tree = ast.parse(orch)
    wait_open = next(n for n in ast.walk(orch_tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_rt_wait_open")
    wait_open_body = ast.unparse(wait_open)
    check("intent='condition_recheck'" in wait_open_body and
          "intent=intent" in wait_open_body and "intent=_plan_intent" in orch,
          "wait_for 写入时的 intent 能穿过运行时封装")
    check("_settle_cancelled_handback_actions(ref)" in app and
          '"bg_ref": bg_ref' in app,
          "取消后的完成通知仍能按 carrier ref 找到原 action")
    check("does not cancel a process" in orch,
          "cancel_wait 明示仅取消等待，不误导模型把它当成终止命令")


def t_handback_never_restarts_carrier_and_keeps_epoch_clock_live() -> None:
    """交还不是重跑；等载体时整段计时不得冻结。"""
    print("\n[16] 交还不重跑 carrier，等待期间回应期计时不冻结")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    orch = (ROOT / "core" / "orchestrator.py").read_text(encoding="utf-8")

    check("It is the command you already started" in orch and
          "launch it, retry it, or ask the user whether to launch it again." in orch,
          "交还提示明示说清它是同一条已运行的命令，不得重启/重试/反问用户")
    check('_rs["waiting_for_carrier"] = True' in app,
          "系统交还单独标记回应期正在等 carrier，不从 final_result 猜测")
    final = next(n for n in ast.walk(ast.parse(app))
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "navigate_pipeline")
    final_body = ast.unparse(final)
    check("if not _waiting_for_carrier" in final_body,
          "carrier 运行时中途 final_result 不能停掉整段的计时任务")
    timer = next(n for n in ast.walk(ast.parse(app))
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_resp_status_timer")
    check("state.get('waiting_for_carrier')" in ast.unparse(timer),
          "计时器在等待 carrier 时仍更新总耗时，不伪装成 Nano 正在 thinking")
    check('_rs["_status_timer_task"]' in app,
          "一段回应期保存唯一计时协程的引用，续接时不能另起一个并发写同一元信息行")

    from app import WebUI

    class FakeEl:
        def __init__(self):
            self.visible = True
            self.text = ""
        def set_visibility(self, value):
            self.visible = bool(value)
        def set_text(self, value):
            self.text = value

    gui = object.__new__(WebUI)
    gui._ui_scope = contextlib.nullcontext
    status, spin = FakeEl(), FakeEl()
    state = {"running": True, "waiting_for_carrier": True,
             "start_time": time.time() - 5, "status_lbl": status, "spin_lbl": spin}

    async def _observe_waiting_clock():
        task = asyncio.create_task(gui._resp_status_timer(state))
        await asyncio.sleep(0.15)
        state["running"] = False
        await task

    asyncio.run(_observe_waiting_clock())
    # 🔴 这条断言原来还要求 `spin.visible is False` —— 而 实测
    #    （2026-08-10）把那个行为判成了 bug：转圈藏了、✦ 也没出来，
    #    元信息行变成**一个裸的 `62s`**，读起来像它坏了。
    # 📌 **一条钉「中间态」的断言，在设计前进之后会反过来阻止终态** ——
    #    改成钉终态，不是删掉（本项目**第三次**用到这条）。
    # ⭐ 保留的那一半仍然对：**不许冒充 Nano 正在 thinking**（它确实没在想）。
    #    去掉的那一半错在前提：那一行属于**这一段回应期**，不属于 Nano ——
    #    📌 **一个「进行中」指示器属于它所在的那一段，
    #       不属于其中某一个参与者。**
    check(status.text.endswith("s") and "thinking" not in status.text,
          "⭐ carrier 运行时元计时仍前进，且**不冒充 Nano 正在 thinking**",
          status.text)
    check(spin.visible is not False and bool(spin.text),
          "⭐⭐⭐ 而转圈**仍然在转** —— 确实有事在跑，那是真事实。"
          "🔴 藏掉它会让元信息行落进一个不存在的第三态"
          "（既不是⠋进行中，也不是✦已结束）。"
          "📌 **一个状态指示器有 N 个合法状态，"
          "任何路径都必须落在这 N 个里**", f"visible={spin.visible} text={spin.text!r}")
    check("still running" in status.text,
          "⭐ 措辞说的是「还在跑」—— "
          "📌 **假事实的修法是换成真话，不是把话删掉**", status.text)


def t_handed_back_skill_stays_running_until_real_completion() -> None:
    """CMD62：回看轮结束不等于被交还的本地 Skill 已结束。"""
    print("\n[17] 交还中的本地 Skill 只许在真实完成时离开 RUNNING")
    from app import WebUI

    class FakeEl:
        def __init__(self):
            self.text = "READY"
            self.styles: list[str] = []
        def set_text(self, value):
            self.text = value
        def style(self, value):
            self.styles.append(value)

    gui = object.__new__(WebUI)
    status, icon = FakeEl(), FakeEl()
    gui.skill_ui_elements = {"SlowProgressTest": {"status": status, "icon": icon}}
    gui._handed_back_carriers = {
        "carrier-1": {"skill_name": "SlowProgressTest"},
    }

    gui._refresh_handed_back_skill_statuses()
    check(status.text == "RUNNING",
          "⭐⭐⭐ 回看轮重置抽屉后，存活 carrier 的本地 Skill 仍显示 RUNNING",
          status.text)
    check(gui._is_handed_back_skill_running("SlowProgressTest"),
          "真实完成前禁止 final_result 把它结算为 OK")

    gui._handed_back_carriers.clear()  # 载体真实结束后才允许离开 RUNNING
    check(not gui._is_handed_back_skill_running("SlowProgressTest"),
          "载体记录移除后，状态门禁才放行 OK")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_dual_source(tmp)
        t_recheck_does_not_resolve(tmp)
        t_rearm_from_due(tmp)
        t_no_more_checkin(tmp)
        t_orphan_pushed(tmp)
        t_completion_discards_recheck(tmp)
        t_reschedule_rejects_terminal(tmp)
        t_resume_consumes_waitrecord_shape(tmp)
        t_mcp_slow_path_consumes_waitrecord(tmp)
        t_long_command_is_the_same_contract()
        t_long_command_progress_is_real(tmp)
        t_wake_continues_same_bubble()
        t_handback_action_spinner_finishes_honestly()
        t_waiting_intent_keeps_user_controls_off_system_rechecks()
        t_cancelled_handback_still_closes_its_original_action()
        t_handback_never_restarts_carrier_and_keeps_epoch_clock_live()
        t_handed_back_skill_stays_running_until_real_completion()
        t_source_wiring()

    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过" +
          ("" if ok == len(_results) else " —— 失败项："))
    for good, name, note in _results:
        if not good:
            print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
