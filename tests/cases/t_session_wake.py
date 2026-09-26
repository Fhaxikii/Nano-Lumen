# -*- coding: utf-8 -*-
"""唤醒由后端调度器驱动（S6-6b 业务状态下沉第 4a 步，`core/session.py`）。

逐条对照原界面实现（`_drive_wake` / `_park_wake` / `_wake_now` / `_drain_inbox` 的唤醒分支 /
`_suspension_poll_tick` / `notify_background_done`）的语义：
- 闲 → 拿锁、定型 pill、起唤醒轮（新开气泡）、通知「正在回复」开 / 关、结束后排空。
- 忙 → 进队列（库 + 内存），同一条挂起只排一次；排空时接上，续接原气泡，结束后收掉 inbox 记录。
- 预算到硬上限 → 同样进队列（不静默丢）。
- 后台完成：有匹配的活等待 → background 唤醒并带回结果；没有 → 只收原动作的界面。
- 定时到点 → 轮询驱动 timer 唤醒。
- 「立即执行」：已结束 / 忙时排队 / 闲时起轮。
- 前台空了 → 没安排回看的后台等待排 60 秒回看。
- 排队的用户消息交给呈现方接上，并记下正在处理的 inbox 记录。

测试不碰真实 data/：内核用临时库，预算读数用替身。

用法：
  py -3.10 tests\\cases\\t_session_wake.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core import session as SS  # noqa: E402
from core.runtime import inbox as IB  # noqa: E402
from core.runtime import task as T  # noqa: E402
from core.runtime import waitcond as W  # noqa: E402
from core.runtime.clock import FakeClock  # noqa: E402
from core.runtime.kernel import reset_kernel_for_tests  # noqa: E402
from core.runtime.store import RuntimeStore  # noqa: E402

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=FakeClock(BASE_T))


class _Budget:
    def __init__(self, level="ok"):
        self.level = level

    def __enter__(self):
        import core.usage as U
        self._orig = U.sync_budget_health
        U.sync_budget_health = lambda: self.level
        return self

    def __exit__(self, *a):
        import core.usage as U
        U.sync_budget_health = self._orig


class _Agent:
    def __init__(self):
        self.resumed = []

    def resume_suspension(self, sid, trigger, note=""):
        self.resumed.append((sid, trigger, note))
        return ("source", sid, trigger)


class _Presenter:
    def __init__(self, sched):
        self.sched = sched
        self.calls = []
        self.lock_held_during_run = []

    async def run_wake_turn(self, sid, trigger, continue_bubble, source):
        self.calls.append(("run", sid, trigger, continue_bubble, source))
        self.lock_held_during_run.append(self.sched.lock.locked())
        await asyncio.sleep(0)

    def settle_wake(self, sid, trigger):
        self.calls.append(("settle", sid, trigger))

    def settle_cancelled_handback(self, ref):
        self.calls.append(("cancelled_handback", ref))
        return 1

    def run_parked_user_item(self, item_id, args):
        self.calls.append(("user", item_id, args))


def setup():
    sched = SS.reset_for_tests()
    agent, pres = _Agent(), _Presenter(sched)
    sched.attach(agent, pres)
    turns = []
    sched.add_turn_listener(lambda a: turns.append(a))
    return sched, agent, pres, turns


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0.01)


def _wait(**kw):
    kw.setdefault("reason", "test")
    return W.open_wait(**kw)


def t_idle_wake(tmp):
    print("\n▶ 闲时唤醒")
    make_kernel(tmp / "a")
    sched, agent, pres, turns = setup()
    rec = _wait(wake_on=["timer"], timer_seconds=10)

    async def run():
        with _Budget():
            await sched.drive_wake(rec.wait_id, trigger="timer")
    asyncio.run(run())
    check(pres.calls[:2] == [("settle", rec.wait_id, "timer"),
                             ("run", rec.wait_id, "timer", False, ("source", rec.wait_id, "timer"))],
          "先定型 pill，再起唤醒轮（新开气泡，事件流来自 resume_suspension）", str(pres.calls))
    check(pres.lock_held_during_run == [True], "唤醒轮期间持有锁")
    check(turns == [True, False], "「正在回复」开 → 关", str(turns))
    check(not sched.lock.locked(), "结束后释放锁")


def t_busy_park_and_drain(tmp):
    print("\n▶ 忙时进队列，排空时续接")
    k = make_kernel(tmp / "b")
    sched, agent, pres, turns = setup()
    rec = _wait(wake_on=["background"], bg_ref="bg1")

    async def run():
        with _Budget():
            await sched.lock.acquire()
            await sched.drive_wake(rec.wait_id, trigger="background", note="done: 42")
            await sched.drive_wake(rec.wait_id, trigger="background", note="done: 42")
            parked = list(sched.parked.values())
            pending = IB.pending_count(k)
            check(parked == [("wake", rec.wait_id, "background", "done: 42")],
                  "忙 → 进内存队列（带结果），同一条挂起只排一次", str(parked))
            check(pending == 1, "库里也落了一条唤醒意图", str(pending))
            check(pres.calls == [], "忙时不定型 pill（不提前说「继续」）")
            sched.lock.release()
            await sched.drain()
            await _settle()
    asyncio.run(run())
    check(("run", rec.wait_id, "background", True, ("source", rec.wait_id, "background")) in pres.calls,
          "排空时接上：续接原气泡（触发那一刻前台上有东西）", str(pres.calls))
    check(agent.resumed and agent.resumed[-1][2] == "done: 42", "唤醒带回后台结果")
    check(IB.pending_count(k) == 0 and not sched.parked, "唤醒轮结束后 inbox 记录收掉、队列清空")


def t_budget_hard(tmp):
    print("\n▶ 预算到硬上限")
    make_kernel(tmp / "c")
    sched, agent, pres, turns = setup()
    rec = _wait(wake_on=["timer"], timer_seconds=10)

    async def run():
        with _Budget("hard"):
            await sched.drive_wake(rec.wait_id, trigger="timer")
    asyncio.run(run())
    check(list(sched.parked.values()) == [("wake", rec.wait_id, "timer", "")] and not pres.calls,
          "不起轮，唤醒进队列（不静默丢）", str(list(sched.parked.values())))


def t_background_done(tmp):
    print("\n▶ 后台完成")
    make_kernel(tmp / "d")
    sched, agent, pres, turns = setup()
    rec = _wait(wake_on=["background", "timer"], timer_seconds=60, bg_ref="cmd_1")

    async def run():
        with _Budget():
            await sched.notify_background_done("cmd_1", "finished ok")
            await sched.notify_background_done("cmd_none", "x")
    asyncio.run(run())
    check(("settle", rec.wait_id, "background") in pres.calls
          and agent.resumed[0] == (rec.wait_id, "background", "finished ok"),
          "有匹配的活等待 → background 唤醒并带回结果", str(agent.resumed))
    check(("cancelled_handback", "cmd_none") in pres.calls, "没有匹配的等待 → 只收原动作的界面")


def t_poll_due(tmp):
    print("\n▶ 定时到点")
    k = make_kernel(tmp / "e")
    sched, agent, pres, turns = setup()
    rec = _wait(wake_on=["timer"], timer_seconds=10)

    async def run():
        with _Budget():
            await sched.poll_due()
            n0 = len(agent.resumed)
            k.clock.advance(11)
            await sched.poll_due()
            return n0
    n0 = asyncio.run(run())
    check(n0 == 0, "没到点不唤醒")
    check(agent.resumed == [(rec.wait_id, "timer", "")], "到点 → timer 唤醒", str(agent.resumed))
    check('heartbeat.register("suspension_poll", 5' in S.module_text("core.backend"),
          "由后端心跳 5 秒轮询一次")


def t_wake_now(tmp):
    print("\n▶ 立即执行")
    make_kernel(tmp / "f")
    sched, agent, pres, turns = setup()
    rec = _wait(wake_on=["timer"], timer_seconds=300, intent="scheduled_plan")

    async def run():
        with _Budget():
            r1 = sched.wake_now("no_such_wait")
            await sched.lock.acquire()
            r2 = sched.wake_now(rec.wait_id)
            parked = list(sched.parked.values())
            sched.lock.release()
            sched.parked.clear()
            r3 = sched.wake_now(rec.wait_id)
            await _settle()
            return r1, r2, parked, r3
    r1, r2, parked, r3 = asyncio.run(run())
    check(r1 == "ended", "等待已结束 → ended")
    check(r2 == "parked" and parked == [("wake", rec.wait_id, "manual")], "忙 → 排队", str(parked))
    check(r3 == "started" and ("run", rec.wait_id, "manual", False, ("source", rec.wait_id, "manual")) in pres.calls,
          "闲 → 起手动唤醒轮", str(pres.calls))


def t_drain_idle_rechecks(tmp):
    print("\n▶ 前台空了 → 后台等待排第一次回看")
    k = make_kernel(tmp / "g")
    sched, agent, pres, turns = setup()
    rec = _wait(wake_on=["background"], bg_ref="cmd_x", intent="detached")
    before = W.find_by_id(k, rec.wait_id).fire_at
    asyncio.run(sched.drain())
    after = W.find_by_id(k, rec.wait_id).fire_at
    check(before is None and after is not None and abs(after - (BASE_T + 60)) < 1,
          "排了 60 秒后的回看", f"{before} → {after}")


def t_drain_user_item(tmp):
    print("\n▶ 排队的用户消息交给呈现方")
    k = make_kernel(tmp / "h")
    sched, agent, pres, turns = setup()
    iid = SS.inbox_submit("hello", {})
    sched.parked[iid] = ("hello", "container")
    asyncio.run(sched.drain())
    check(pres.calls == [("user", iid, ("hello", "container"))], "呈现方接上这一条", str(pres.calls))
    check(sched.running_inbox_id == iid, "记下正在处理的 inbox 记录")
    sched.consume_running()
    check(sched.running_inbox_id is None and IB.pending_count(k) == 0, "那一轮结束后收掉")


def t_ui_wiring():
    print("\n▶ 界面只剩呈现")
    app = S.module_text("app")
    for gone in ("def _drive_wake", "def _park_wake", "def _suspension_poll_tick",
                 "def notify_background_done", "def _drain_inbox", "self.pipeline_lock = asyncio.Lock()"):
        check(gone not in app, f"界面不再有 `{gone}`")
    for need in ("def run_wake_turn", "def settle_wake", "def settle_cancelled_handback",
                 "def run_parked_user_item", "_sched.attach(self.agent, self)",
                 "_carriers.set_completion_handler(_sched.notify_background_done)"):
        check(need in app, f"界面实现呈现方 / 登记：`{need}`")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_idle_wake(tmp)
        t_busy_park_and_drain(tmp)
        t_budget_hard(tmp)
        t_background_done(tmp)
        t_poll_due(tmp)
        t_wake_now(tmp)
        t_drain_idle_rechecks(tmp)
        t_drain_user_item(tmp)
        t_ui_wiring()
    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过")
    print("=" * 74)
    if ok != len(_results):
        print("失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  · {name}" + (f"   [{note}]" if note else ""))
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
