# -*- coding: utf-8 -*-
"""用户消息由后端调度器决定怎么跑（S6-6b 业务状态下沉第 4b 步，`core/session.py`）。

逐条对照原界面实现（`start_pipeline_task` 的收尾 / `_safe_execute_pipeline` 的持锁 /
`_drain_inbox` 的用户分支）的语义：
- 闲 → 落库、认领、起一轮（新回应期，段号 1，orchestrator 收到 0）；轮结束收掉 inbox 记录。
- 忙且界面还接得上 → 插话：排在当前轮之后，续接同一个气泡，段号 +1（每插一次再 +1）。
- 忙但接不上 → 排队：之后起新一轮（段号回到 1）。
- 排队项只放数据（文字、附件字节、附件提示），不带界面对象。
- 落库失败照样干活（内存 key）；呈现方出错也要放锁、收记录、接着排空。
- 用户消息与唤醒意图进同一个队列，按入队顺序接上。

测试不碰真实 data/：内核用临时库，预算读数用替身。

用法：
  py -3.10 tests\\cases\\t_session_user.py
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

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=FakeClock(1_700_000_000.0))


class _Budget:
    def __enter__(self):
        import core.usage as U
        self._orig = U.sync_budget_health
        U.sync_budget_health = lambda: "ok"
        return self

    def __exit__(self, *a):
        import core.usage as U
        U.sync_budget_health = self._orig


class _Provider:
    @staticmethod
    def build_image_part(b, mime):
        return {"image": len(b), "mime": mime}


class _Agent:
    _seam_continuation_part = -1
    provider = _Provider()

    def __init__(self):
        self.queries = []

    def resume_suspension(self, sid, trigger, note=""):
        async def _gen():
            yield {"event": "final_result"}
        return _gen()

    def handle_query(self, text, image_parts=None, temp_file_hint=None):
        self.queries.append((text, image_parts, temp_file_hint))

        async def _gen():
            yield {"event": "final_result", "text": text}
        return _gen()


class _Presenter:
    def __init__(self, sched, agent, hold: asyncio.Event | None = None, fail=False):
        self.sched, self.agent = sched, agent
        self.calls = []
        self.hold = hold
        self.fail = fail

    async def render_user_turn(self, key, payload, continuation, turn_id):
        self.calls.append(("user", key, payload.get("text"), continuation,
                           self.sched.seam_part, self.agent._seam_continuation_part,
                           self.sched.lock.locked()))
        if self.hold is not None:
            await self.hold.wait()
        if self.fail:
            raise RuntimeError("界面炸了")

    async def run_wake_turn(self, sid, trigger, continue_bubble, turn_id):
        self.calls.append(("wake", sid, trigger, continue_bubble))

    def settle_wake(self, sid, trigger):
        pass

    def settle_cancelled_handback(self, ref):
        return 0


def setup(**kw):
    sched = SS.reset_for_tests()
    agent = _Agent()
    pres = _Presenter(sched, agent, **kw)
    sched.attach(agent, pres)
    turns = []
    sched.add_turn_listener(lambda a: turns.append(a))
    return sched, agent, pres, turns


async def _settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0.01)


def t_idle(tmp):
    print("\n▶ 闲时发送")
    k = make_kernel(tmp / "a")
    sched, agent, pres, turns = setup()

    async def run():
        key, mode = sched.submit_user_message("你好", image_bytes=b"img", temp_hint="hint")
        check(mode == "run", "闲 → 立刻起一轮", mode)
        check(IB.pending_count(k) == 0, "落库并认领（不算排队中）")
        await _settle()
        return key
    key = asyncio.run(run())
    check(pres.calls and pres.calls[0][:4] == ("user", key, "你好", False),
          "呈现方收到这一条（新回应期，不续接）", str(pres.calls))
    check(pres.calls[0][4:] == (1, 0, True), "段号 1、orchestrator 收到 0、期间持锁", str(pres.calls[0][4:]))
    check(turns == [True, False] and not sched.lock.locked(), "「正在回复」开 → 关，锁已释放")
    check(agent.queries == [("你好", [{"image": 3, "mime": "image/jpeg"}], "hint")],
          "后端事件流由调度器建：文字、图片 part、附件提示交给 handle_query", str(agent.queries))
    check(sched.running_inbox_id is None, "轮结束收掉 inbox 记录")


def t_interject_and_queue(tmp):
    print("\n▶ 忙时：插话续接 / 排队")
    k = make_kernel(tmp / "b")
    hold = asyncio.Event
    sched, agent, pres, turns = setup()

    async def run():
        gate = asyncio.Event()
        pres.hold = gate
        k1, m1 = sched.submit_user_message("第一句")
        await _settle(3)
        k2, m2 = sched.submit_user_message("等等，改一下", can_continue=True)
        k3, m3 = sched.submit_user_message("再补一句", can_continue=True)
        k4, m4 = sched.submit_user_message("排队的那句", can_continue=False)
        parked = list(sched.parked.values())
        check((m1, m2, m3, m4) == ("run", "cont", "cont", "queued"), "闲→run，忙→cont / queued",
              str((m1, m2, m3, m4)))
        check(all(isinstance(v, tuple) and isinstance(v[1], dict) and set(v[1]) ==
                  {"text", "image_bytes", "image_mime", "temp_hint"} for v in parked),
              "排队项只放数据，不带界面对象", str([v[0] for v in parked]))
        check(IB.pending_count(k) == 3, "三条都落了库（排队中）", str(IB.pending_count(k)))
        pres.hold = None
        gate.set()
        await _settle(40)
    asyncio.run(run())
    seq = [(c[2], c[3], c[4], c[5]) for c in pres.calls]
    check(seq == [("第一句", False, 1, 0), ("等等，改一下", True, 2, 2),
                  ("再补一句", True, 3, 3), ("排队的那句", False, 1, 0)],
          "按入队顺序接上；插话续接且段号逐次 +1，排队的那条回到新回应期", str(seq))
    check(IB.pending_count(k) == 0 and not sched.parked, "全部跑完，队列与库里都清空")


def t_inbox_down(tmp):
    print("\n▶ 落库失败照样干活")
    make_kernel(tmp / "c")
    sched, agent, pres, turns = setup()
    orig = SS.inbox_submit
    SS.inbox_submit = lambda body, detail=None: None
    try:
        async def run():
            key, mode = sched.submit_user_message("库挂了")
            await _settle()
            return key, mode
        key, mode = asyncio.run(run())
    finally:
        SS.inbox_submit = orig
    check(mode == "run" and key.startswith("mem_") and pres.calls and pres.calls[0][2] == "库挂了",
          "内存 key 照样起轮", key)


def t_presenter_error(tmp):
    print("\n▶ 呈现方出错")
    k = make_kernel(tmp / "d")
    sched, agent, pres, turns = setup(fail=True)

    async def run():
        sched.submit_user_message("会炸的")
        await _settle()
        # 出错之后下一条照样能跑
        pres.fail = False
        sched.submit_user_message("下一条")
        await _settle()
    asyncio.run(run())
    check(not sched.lock.locked() and turns[-1] is False, "出错也放锁、「正在回复」关掉")
    check(IB.pending_count(k) == 0 and [c[2] for c in pres.calls] == ["会炸的", "下一条"],
          "记录收掉，下一条照常起轮")


def t_mixed_queue(tmp):
    print("\n▶ 用户消息与唤醒同一个队列")
    make_kernel(tmp / "e")
    sched, agent, pres, turns = setup()
    rec = W.open_wait(reason="t", wake_on=["background"], bg_ref="bg1")

    async def run():
        with _Budget():
            gate = asyncio.Event()
            pres.hold = gate
            sched.submit_user_message("先跑的")
            await _settle(3)
            await sched.drive_wake(rec.wait_id, trigger="background", note="done")
            sched.submit_user_message("后来的", can_continue=False)
            pres.hold = None
            gate.set()
            await _settle(40)
    asyncio.run(run())
    kinds = [(c[0], c[2] if c[0] == "user" else c[2]) for c in pres.calls]
    check(kinds == [("user", "先跑的"), ("wake", "background"), ("user", "后来的")],
          "按入队顺序：唤醒排在后来的消息前面", str(kinds))


def t_ui_wiring():
    print("\n▶ 界面只剩呈现")
    app = S.module_text("app")
    code = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))
    sp = code.split("def start_pipeline_task")[1].split("\n    def ")[0]
    check("get_scheduler().submit_user_message(" in sp, "发送交给调度器决定")
    check("self._rt_inbox_parked[" not in code and "asyncio.create_task(self._safe_execute_pipeline" not in code,
          "界面不再自己排队、不再自己起轮")
    se = code.split("async def _safe_execute_pipeline")[1].split("\n    async def ")[0].split("\n    def ")[0]
    check("async with" not in se and ".lock" not in se and "drain(" not in se and "turn_state" not in se,
          "`_safe_execute_pipeline` 只渲染：不持锁、不排空")
    check("def render_user_turn" in app and "def run_parked_user_item" not in app, "呈现方接口换成 render_user_turn")
    check("_seam_part" not in code, "段号在调度器（`seam_part`），界面不再记")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_idle(tmp)
        t_interject_and_queue(tmp)
        t_inbox_down(tmp)
        t_presenter_error(tmp)
        t_mixed_queue(tmp)
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
