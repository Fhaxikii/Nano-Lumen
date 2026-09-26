# -*- coding: utf-8 -*-
"""后端 → 界面的事件总线（S6-6b 业务状态下沉第 5 步，`core/runtime/events.py`）。

- 发布 / 订阅：每个订阅者收到 `(turn_id, event)`；没有订阅者时丢弃不报错。
- 一轮的后端事件流由 `pump_turn` 在一个 task 里完整迭代（后端生成器用 ContextVar），
  最后发 `turn_end`；生成器抛异常时 `turn_end` 带 `error`。
- 轮外出口 `OUT_OF_TURN`（Subagent 跨过它那一轮之后用）：事件不带轮 id。
- 进总线时检查可序列化（不合格的值记 WARNING，事件照发）。
- 界面：常驻消费者按轮 id 分发；`_turn_events(turn_id)` 读到 `turn_end` 为止、出错时抛；
  轮外事件交给 `_handle_out_of_turn`（真的弹出授权弹窗）。

用法：
  py -3.10 tests\\cases\\t_events.py
"""
from __future__ import annotations

import asyncio
import contextvars
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core.runtime import events as E  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


_cv = contextvars.ContextVar("cv", default=None)


def t_bus():
    print("\n▶ 发布 / 订阅 / 泵")
    E._reset_for_tests()
    E.publish({"event": "x"}, "t0")
    check(True, "没有订阅者时发布不报错")

    async def run():
        q = E.subscribe()

        async def gen():
            _cv.set("mine")
            yield {"event": "a"}
            await asyncio.sleep(0)
            yield {"event": "b", "cv": _cv.get()}

        await E.pump_turn("t1", gen())

        async def bad():
            yield {"event": "c"}
            raise ValueError("boom")

        await E.pump_turn("t2", bad())
        await E.OUT_OF_TURN.put({"event": "os_action_confirm"})
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        E.unsubscribe(q)
        return out
    out = asyncio.run(run())
    t1 = [e for tid, e in out if tid == "t1"]
    check([e["event"] for e in t1] == ["a", "b", E.TURN_END], "一轮的事件按序发出，最后 turn_end",
          str([e["event"] for e in t1]))
    check(t1[1].get("cv") == "mine", "整个生成器在一个 task 里迭代（ContextVar 跨步可见）")
    t2 = [e for tid, e in out if tid == "t2"]
    check(t2[-1]["event"] == E.TURN_END and "ValueError: boom" in t2[-1].get("error", ""),
          "后端出错 → turn_end 带 error", str(t2[-1]))
    check((None, {"event": "os_action_confirm"}) in out, "轮外出口：不带轮 id")

    msgs = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        E._reset_for_tests()

        async def run2():
            q = E.subscribe()
            E.publish({"event": "evt_unique_bus", "task": object()}, "t3")
            return q.qsize()
        n = asyncio.run(run2())
    finally:
        logger.remove(hid)
    check(n == 1 and any("evt_unique_bus" in m for m in msgs), "不可序列化的值记 WARNING，事件照发")
    E._reset_for_tests()


def t_ui_router():
    print("\n▶ 界面按轮 id 分发")
    from app import WebUI

    class _Host:
        def __init__(self):
            self._turn_queues = {}
            self.presented = []
            self._confirm_owner = "rs"

        def _present_os_confirm(self, ev):
            self.presented.append((ev.get("event"), self._confirm_owner))

        def _dismiss_pending_confirms(self, why=""):
            self.presented.append(("dismiss", why))

    for name in ("_turn_queue", "_event_router", "_turn_events", "_handle_out_of_turn"):
        setattr(_Host, name, getattr(WebUI, name))

    async def run():
        E._reset_for_tests()
        h = _Host()
        h._events_q = E.subscribe()
        router = asyncio.ensure_future(h._event_router())

        async def gen():
            yield {"event": "tool_start"}
            yield {"event": "final_result"}
        await E.pump_turn("tA", gen())
        await E.OUT_OF_TURN.put({"event": "os_action_confirm", "reply_id": "r", "actions": []})
        await asyncio.sleep(0.01)
        got = [ev["event"] async for ev in h._turn_events("tA")]

        async def bad():
            yield {"event": "tool_start"}
            raise RuntimeError("后端炸了")
        await E.pump_turn("tB", bad())
        await asyncio.sleep(0.01)
        err = None
        try:
            async for _ in h._turn_events("tB"):
                pass
        except RuntimeError as e:
            err = str(e)
        router.cancel()
        E._reset_for_tests()
        return h, got, err
    h, got, err = asyncio.run(run())
    check(got == ["tool_start", "final_result"], "这一轮的事件读到 turn_end 为止", str(got))
    check(err and "后端炸了" in err, "后端那一轮出错 → 渲染方收到异常（界面照旧显示故障）", str(err))
    check(h.presented == [("os_action_confirm", None)] and h._confirm_owner == "rs",
          "轮外授权请求直接弹窗（不属于任何回应期，弹完恢复原归属）", str(h.presented))
    check(h._turn_queues == {}, "读完的轮不留队列")

    app = S.module_text("app")
    check("_oob_events" not in app and "_drain_oob_events" not in app and "handle_query(" not in app,
          "界面不再有轮外队列与轮询，不再自己调后端 handle_query")
    check("self._events_q = _events.subscribe()" in app, "订阅在界面构造时建好（界面起来前的事件不丢）")


def main() -> int:
    t_bus()
    t_ui_router()
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
