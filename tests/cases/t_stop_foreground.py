# -*- coding: utf-8 -*-
"""终止时这一轮前台正在执行的一起停（裁决 73，第五次实机 A-4）；抽屉 ■ 真的停进程（D51）。

- 命令：前台等待也听「终止」→ 杀进程、结果记「用户终止」，不交还（不会变成后台载体、不唤醒）。
- MCP / Skill（停不掉）：不交还、不唤醒；发 `tool_still_running`（工具行标「无法中途停止 ·
  后台自行结束中」）；真正结束时发轮外 `tool_finished_late`（收成 ✓ / ✕），并写一条系统事件
  （只进下一轮上下文，要求不主动提起、不解释）。
- `_wait_or_user_speaks` 在用户按了终止时提前返回。
- 抽屉 ■：命令类载体真的停掉进程；执行记录收尾。
- 界面：被终止过的回应期不再续接（终止后的新消息开新气泡）。

用法：
  py -3.10 tests\\cases\\t_stop_foreground.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core.os_layer import longcmd as LC  # noqa: E402
from core.runtime import events as E  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _Stop:
    """替身：「这一轮被终止了吗」。"""

    def __init__(self):
        self.on = False

    def __call__(self):
        return self.on


def t_command_stopped():
    print("\n▶ 前台命令随终止一起停")
    from core.os_layer import executor_write as EW
    W = EW.WriteExecutor
    w = object.__new__(W)
    stop = _Stop()
    LC.set_turn_stop_probe(stop)

    async def run():
        orig = W._FOREGROUND_WAIT_SEC
        W._FOREGROUND_WAIT_SEC = 20
        try:
            async def press():
                await asyncio.sleep(0.8)
                stop.on = True
            asyncio.ensure_future(press())
            t0 = time.time()
            r = await w._run_command_async({"command": "ping -n 30 127.0.0.1"})
            return r, time.time() - t0
        finally:
            W._FOREGROUND_WAIT_SEC = orig
    r, el = asyncio.run(run())
    LC.set_turn_stop_probe(None)
    check(el < 5, "按下终止后前台等待立刻结束（不等满 20 秒）", f"{el:.1f}s")
    check(r.get("ok") is False and "stopped_by_user" in (r.get("error") or "")
          and not (r.get("data") or {}).get("long_running"),
          "结果是「用户终止」，不是 long_running（不会被交还成后台载体）", str(r.get("error"))[:60])
    live = [lc for lc in list(LC._LIVE.values()) if lc.running and "ping -n 30" in lc.display]
    check(not live, "进程已被停掉")


def t_wait_helper():
    print("\n▶ MCP / Skill 的前台等待听「终止」")
    from core.orchestrator import Orchestrator
    stop = _Stop()
    LC.set_turn_stop_probe(stop)

    async def run():
        task = asyncio.ensure_future(asyncio.sleep(30))

        async def press():
            await asyncio.sleep(0.3)
            stop.on = True
        asyncio.ensure_future(press())
        t0 = time.time()
        done = await Orchestrator._wait_or_user_speaks(task, 20)
        task.cancel()
        return done, time.time() - t0
    done, el = asyncio.run(run())
    LC.set_turn_stop_probe(None)
    check(done is False and el < 3, "终止 → 提前返回「没跑完」", f"{el:.1f}s")


def t_finish_after_stop():
    print("\n▶ 停不掉的那一步：后台自行结束，不唤醒")
    from core.orchestrator import Orchestrator
    from core import health as Hm
    o = Orchestrator.__new__(Orchestrator)
    notes = []

    class _Sys:
        def add(self, m):
            notes.append(m)

    class _Q:
        def __init__(self):
            self.items = []

        async def put(self, ev):
            self.items.append(ev)

    async def run():
        E._reset_for_tests()
        bus = E.subscribe()
        q = _Q()

        async def slow_mcp():
            await asyncio.sleep(0.2)
            return ("page fetched", None, False, "fetch")
        orig = Hm.get_system_events
        Hm.get_system_events = lambda: _Sys()
        try:
            txt = await o._finish_after_stop(
                action_id="a1", display="外部服务 fetch", awaitable=asyncio.ensure_future(slow_mcp()),
                outcome=lambda r: (not r[1], str(r[0])), event_queue=q)
            await asyncio.sleep(0.4)
        finally:
            Hm.get_system_events = orig
        out = []
        while not bus.empty():
            out.append(bus.get_nowait())
        E._reset_for_tests()
        return txt, q.items, out
    txt, items, out = asyncio.run(run())
    check(items == [{"event": "tool_still_running", "action_id": "a1"}],
          "先发 tool_still_running（工具行标「后台自行结束中」）", str(items))
    check(out == [(None, {"event": "tool_finished_late", "action_id": "a1", "ok": True})],
          "结束时发轮外 tool_finished_late（收成 ✓）", str(out))
    check(len(notes) == 1 and "do not bring this up on your own" in notes[0]
          and "unless they ask" in notes[0] and "page fetched" in notes[0],
          "系统事件带结果，并要求不主动提起、不解释（除非用户问）", notes[0][:80] if notes else "")
    check("will not be woken" in txt, "这一轮的工具结果说明不会被唤醒")
    orch = S.module_text("core.orchestrator")
    check(orch.count("await self._finish_after_stop(") == 2,
          "MCP 与 Skill 两处都走这条（不交还、不唤醒）", str(orch.count("await self._finish_after_stop(")))
    check("_task.cancel()" in orch.split("if self._stop_asked():")[-1][:400] or
          "前台的 Subagent" in orch, "前台的 Subagent 可以取消，就直接取消")


def t_drawer_stop_kills():
    print("\n▶ 抽屉 ■ 真的停进程（D51）")
    from core.runtime import carriers as C
    stopped = []
    orig = LC.stop
    LC.stop = lambda ref, reason="": stopped.append(ref) or True

    async def run():
        C._reset_for_tests()

        async def forever():
            await asyncio.sleep(60)
        C.start("ping", forever(), "cmd_abc", rt_task_id="rt1")
        C.start("mcp", forever(), "long_x", rt_task_id="rt2")
        await asyncio.sleep(0)
        C.cancel("rt1")
        C.cancel("rt2")
        await asyncio.sleep(0.05)
        C._reset_for_tests()
    try:
        asyncio.run(run())
    finally:
        LC.stop = orig
    check(stopped == ["cmd_abc"], "命令类载体停掉进程；MCP / Skill 类只取消等待（停不掉）", str(stopped))
    osx = S.module_text("core.orchestrator")
    seg = osx.split("async def _await_longcmd")[1][:1200]
    check("except _aio.CancelledError" in seg and "reason=\"stopped by user\"" in seg,
          "被 ■ 取消时执行记录收尾（不再「没收尾就被下一条顶掉」）")


def t_ui():
    print("\n▶ 界面")
    app = S.module_text("app")
    sp = app.split("def start_pipeline_task")[1].split("\n    def ")[0]
    check('not _rs_live.get("stop_clicked")' in sp and 'not _rs_live.get("ui_stopped")' in sp,
          "被终止过的回应期不再续接（终止后的新消息开新气泡）")
    check('_STILL_RUNNING_NOTE = "「无法中途停止 · 后台自行结束中」"' in app,
          "工具行的标注文字（含「」）")
    check('elif _kind == "tool_finished_late":' in app, "轮外 tool_finished_late 收成 ✓ / ✕")


def main() -> int:
    t_command_stopped()
    t_wait_helper()
    t_finish_after_stop()
    t_drawer_stop_kills()
    t_ui()
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
