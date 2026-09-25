# -*- coding: utf-8 -*-
"""GUI 任务的生命周期与窗口形态分开。

- GUI 任务 = GUI 会话租约 + 临时免确认授权，由后端开始 / 结束；窗口形态（mini / full）只是呈现。
- 开始：set_window_mode('mini') 且用户授权（先开会话再授权）。
- 结束：set_window_mode('full')、急停（整个任务期间监听 Ctrl+`）、重置对话、空闲兜底；
  **不随轮结束，也不随终止按钮 / 插话结束**。
- 用户手动放大：只改窗口形态，任务与授权保留，下一个工具结果附一次说明；
  之后再缩回 mini 不重新授权。
- 界面：mini 窗开关不碰租约；GUI 任务结束而窗口仍是 mini 时由 1 秒 tick 恢复。
- 鼠标 / 键盘 / 窗口动作只能在 GUI 任务内执行；逐次点击授权的标注图已删除。

用法：
  py -3.10 tests\\cases\\t_gui_task_lifecycle.py
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

import types as _types  # noqa: E402


class _FakeKeyboard:
    """替身 keyboard 模块：不装真实的全局键盘钩子（测试不碰真实桌面）。"""
    def __init__(self):
        self.hotkeys: dict = {}
        self._n = 0

    def add_hotkey(self, combo, cb):
        self._n += 1
        self.hotkeys[self._n] = (combo, cb)
        return self._n

    def remove_hotkey(self, handle):
        self.hotkeys.pop(handle, None)


FAKE_KB = _FakeKeyboard()
sys.modules["keyboard"] = _types.SimpleNamespace(add_hotkey=FAKE_KB.add_hotkey,
                                                 remove_hotkey=FAKE_KB.remove_hotkey)

from core.runtime.clock import FakeClock  # noqa: E402
from core.runtime.kernel import reset_kernel_for_tests  # noqa: E402
from core.runtime.store import RuntimeStore  # noqa: E402
from core.runtime import oslease as L  # noqa: E402
from core.runtime import task as T  # noqa: E402
from core.runtime import replies as R  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=FakeClock(1_700_000_000.0))


def make_orch():
    from core.orchestrator import Orchestrator
    return Orchestrator.__new__(Orchestrator)


class _Q:
    def __init__(self):
        self.items: list = []

    async def put(self, ev):
        self.items.append(ev)


def _active(k) -> tuple[bool, bool]:
    return bool(L.gui_session_active(k)), bool(L.temp_auto_authorized(k))


def t_begin_end(tmp: pathlib.Path) -> None:
    print("\n▶ 开始 / 结束")
    k = make_kernel(tmp / "a")
    o = make_orch()
    check(_active(k) == (False, False), "初始没有 GUI 任务")
    o._gui_task_begin("test")
    check(_active(k) == (True, True), "开始：GUI 会话与临时授权都在")
    check(L.current_gui_task_id() is not None, "GUI 会话建了 Task（授权可绑定到它）")
    o._set_window_mode("mini")
    o._gui_task_end("test")
    check(_active(k) == (False, False), "结束：会话与授权都收回")
    check(o._window_mode_now() == "full", "结束后窗口形态记为 full")
    o._gui_task_end("again")
    check(_active(k) == (False, False), "结束是幂等的")


async def _call_mode(o, mode: str, reply: str | None = None):
    q = _Q()
    task = asyncio.create_task(o._handle_set_window_mode({"mode": mode}, "aid", event_queue=q))
    for _ in range(100):
        await asyncio.sleep(0.01)
        req = next((e for e in q.items if e.get("event") == "mini_auth_request"), None)
        if req and reply and not req.get("_done"):
            req["_done"] = True
            R.resolve(req["reply_id"], reply)
        if task.done():
            break
    res = await task
    text = getattr(res, "text", None) or getattr(res, "content", None) or str(res)
    return text, q.items


def t_set_window_mode(tmp: pathlib.Path) -> None:
    print("\n▶ set_window_mode 与用户放大")
    k = make_kernel(tmp / "b")
    o = make_orch()

    async def run():
        txt, evs = await _call_mode(o, "mini", reply="reject")
        check(any(e.get("event") == "mini_auth_request" for e in evs), "没有任务时缩窗先请求授权")
        check(_active(k) == (False, False) and o._window_mode_now() == "full", "拒绝：不开始任务")

        txt, evs = await _call_mode(o, "mini", reply="approve")
        check(_active(k) == (True, True) and o._window_mode_now() == "mini", "同意：任务开始，窗口 mini")
        check("set_window_mode('full')" in txt and "revokes" in txt,
              "授权结果提醒：完成后调 full，它结束任务并收回授权", txt[:80])

        txt, evs = await _call_mode(o, "mini")
        check("ALREADY" in txt and not evs, "已经是 mini：拒绝，不发任何事件")

        o.notify_window_enlarged_by_user()
        check(o._window_mode_now() == "full" and _active(k) == (True, True),
              "用户放大：窗口 full，任务与授权保留")
        note = o._take_window_note()
        check("[Window]" in note and "respect" in note and "never a reason" in note,
              "附一次说明：尊重放大、看屏幕不是缩窗理由", note[:60])
        check(o._take_window_note() == "", "说明只附一次")
        inj = o._build_window_mode_injection()
        check("enlarged" in inj and "No new authorization" in inj, "每轮注入：任务进行中但用户放大了")

        txt, evs = await _call_mode(o, "mini")
        check(not any(e.get("event") == "mini_auth_request" for e in evs)
              and any(e.get("event") == "window_mode" and e.get("mode") == "mini" for e in evs),
              "放大后再缩：同一任务，不重新授权，直接缩回")
        check("one short sentence why" in txt, "要求在循环里说一句缩窗理由", txt[:80])
        check(o._window_mode_now() == "mini" and _active(k) == (True, True), "仍是同一个任务")
        inj = o._build_window_mode_injection()
        check("CURRENTLY MINI" in inj, "每轮注入：mini")

        txt, evs = await _call_mode(o, "full")
        check(_active(k) == (False, False)
              and any(e.get("event") == "window_mode" and e.get("mode") == "full" for e in evs),
              "full：结束任务并恢复窗口")
        inj = o._build_window_mode_injection()
        check("CURRENTLY FULL SIZE" in inj and "does not require it" in inj,
              "每轮注入：full 且没有任务；看屏幕不需要缩窗")

    asyncio.run(run())


def t_turns_and_stops(tmp: pathlib.Path) -> None:
    print("\n▶ 轮结束 / 停止 / 挂起")
    k = make_kernel(tmp / "c")
    o = make_orch()

    async def feed(events):
        for e in events:
            yield e

    async def drain(events):
        async for _ in o._gui_task_track_turn(feed(events)):
            pass

    o._gui_task_begin("test")
    asyncio.run(drain([{"event": "final_result"}]))
    check(_active(k) == (True, True), "轮正常结束不结束 GUI 任务（任务可跨轮）")
    check(o._turn_running is False and o._last_turn_suspended is False, "记录：轮已结束、未挂起")

    asyncio.run(drain([{"event": "suspend_waiting"}]))
    check(o._last_turn_suspended is True and _active(k) == (True, True), "以挂起结束：记录挂起，任务保留")

    asyncio.run(drain([{"event": "turn_interrupted", "stopped": False}]))
    check(_active(k) == (True, True), "插话中断不结束任务")
    asyncio.run(drain([{"event": "turn_interrupted", "stopped": True}]))
    check(_active(k) == (True, True), "终止按钮也不结束任务（只有急停结束）")
    o._gui_task_end("cleanup")

    seen = []

    async def watch():
        async for _ in o._gui_task_track_turn(feed([{"event": "x"}])):
            seen.append(o._turn_running)
    asyncio.run(watch())
    check(seen == [True] and o._turn_running is False, "轮进行中标记正确")


def t_idle(tmp: pathlib.Path) -> None:
    print("\n▶ 空闲兜底")
    k = make_kernel(tmp / "d")
    o = make_orch()
    idle = o._GUI_TASK_IDLE_SEC
    check(idle == 15 * 60, "空闲时长 15 分钟", str(idle))

    o._gui_task_begin("test")
    t0 = o._gui_last_activity
    o._turn_running = False
    o._last_turn_suspended = False
    check(o._gui_task_idle_check(now=t0 + idle - 1) is False and _active(k)[0], "不满 15 分钟不结束")
    o._turn_running = True
    check(o._gui_task_idle_check(now=t0 + idle + 1) is False and _active(k)[0], "轮进行中不结束")
    o._turn_running = False
    o._last_turn_suspended = True
    o._live_suspension_exists = lambda: True
    check(o._gui_task_idle_check(now=t0 + idle + 1) is False and _active(k)[0],
          "上一轮以挂起结束且挂起仍在：不结束（唤醒后接着做）")
    o._live_suspension_exists = lambda: False
    o._set_window_mode("mini")
    check(o._gui_task_idle_check(now=t0 + idle + 1) is True, "满 15 分钟、没有轮、没有挂起：结束")
    check(_active(k) == (False, False) and o._window_mode_now() == "full", "结束后授权收回、窗口形态 full")
    check(o._gui_task_idle_check(now=t0 + 10 * idle) is False, "没有任务时什么都不做")

    o._gui_task_begin("again")
    o._gui_task_touch()
    t1 = o._gui_last_activity
    check(o._gui_task_idle_check(now=t1 + idle - 5) is False, "屏幕活动重新起算")
    o._gui_task_end("cleanup")


def t_emergency_stop(tmp: pathlib.Path) -> None:
    print(chr(10) + "▶ 急停（Ctrl+`）在整个 GUI 任务期间有效")
    from core.os_layer import executor_action as EA
    k = make_kernel(tmp / "f")
    o = make_orch()
    stops = []
    o.request_stop = lambda source="": stops.append(source)
    FAKE_KB.hotkeys.clear()
    check(not EA.global_hotkey_armed(), "任务外不监听")
    o._gui_task_begin("test")
    check(EA.global_hotkey_armed() and len(FAKE_KB.hotkeys) == 1
          and list(FAKE_KB.hotkeys.values())[0][0] == "ctrl+`", "GUI 任务开始：注册 Ctrl+` 热键")
    o._gui_task_touch()
    check(EA.global_hotkey_armed(), "两个动作之间（思考 / 等 API）热键仍在")
    est = EA.EmergencyStop()
    est.start_listening()
    check(not est.is_stopped(), "按下之前动作不中止")
    _, cb = list(FAKE_KB.hotkeys.values())[0]
    cb()                                   # 模拟按下 Ctrl+`（不在事件循环里：直接回调）
    check(est.is_stopped(), "按下：正在执行的动作看到中止标志")
    check(len(stops) == 1 and "emergency" in stops[0], "按下：终止本轮", str(stops))
    check(_active(k) == (False, False) and o._window_mode_now() == "full", "按下：结束 GUI 任务（收回授权）")
    check(not EA.global_hotkey_armed() and not FAKE_KB.hotkeys, "任务结束：热键注销")
    est2 = EA.EmergencyStop()
    check(est2.is_stopped(), "中止标志保留到下一次任务开始（让还在执行的动作能看到）")
    o._gui_task_begin("again")
    check(not est2.is_stopped(), "新的 GUI 任务开始时清掉中止标志")
    o._gui_task_end("cleanup")
    check(not FAKE_KB.hotkeys, "正常结束也注销热键")


def t_batch_and_end_tool(tmp: pathlib.Path) -> None:
    print(chr(10) + "▶ 一次回复连发多步 computer_use；失败即停；end_screen_task")
    from core.orchestrator._types import ToolExecution
    from core.schema import ToolCall
    k = make_kernel(tmp / "g")
    o = make_orch()
    ran = []
    fail_on = {"c2"}

    async def fake_one(call, **_kw):
        ran.append(call.tool_use_id)
        return ToolExecution(call=call, result_text="ok", ok=call.tool_use_id not in fail_on)

    o._execute_one_tool_call = fake_one
    o._get_tool_catalog = lambda: {}
    o._tool_runtime_view = lambda: None
    calls = [ToolCall(name="computer_use", args={}, tool_use_id=f"c{i}", index=i) for i in (1, 2, 3, 4)]
    res = asyncio.run(o._execute_tool_batch(calls, used_model="m", base_guide="", system_guide="",
                                             realtime_callback=None, event_queue=_Q()))
    check(ran == ["c1", "c2"], "第 2 步失败后，第 3、4 步不执行", str(ran))
    check([r.call.tool_use_id for r in res] == ["c1", "c2", "c3", "c4"]
          and not res[2].ok and "skipped" in res[2].error and "Re-plan" in res[2].result_text,
          "被跳过的步骤也有结果（tool_use / tool_result 一一对应），并说明要重新规划")
    ran.clear()
    fail_on.clear()
    asyncio.run(o._execute_tool_batch(calls, used_model="m", base_guide="", system_guide="",
                                       realtime_callback=None, event_queue=_Q()))
    check(ran == ["c1", "c2", "c3", "c4"], "全部成功时按顺序全部执行")
    from core.orchestrator.react_loop import _OS_CAPABILITY_PROMPT as _P
    check("send those computer_use calls together in one reply - up to 4" in _P
          and "the rest of that batch are skipped" in _P,
          "系统提示允许一次看清后连发多步（上限 4，失败即停）")

    o2 = make_orch()
    o2._gui_task_begin("test")
    o2._set_window_mode("mini")
    q = _Q()
    txt = asyncio.run(o2._handle_end_screen_task({}, "aid", event_queue=q))
    check(_active(k) == (False, False) and "revoked" in txt
          and any(e.get("event") == "window_mode" and e.get("mode") == "full" for e in q.items),
          "end_screen_task：收回 Temp Auto、恢复窗口")
    txt2 = asyncio.run(o2._handle_end_screen_task({}, "aid", event_queue=_Q()))
    check("no screen-operation task" in txt2, "没有任务时什么都不做")
    from core.tools import manifests as M
    from core.tools.builtin import build_builtin_definitions
    defs = {d.name: d for d in build_builtin_definitions(M.BUILTIN_MANIFESTS)}
    d = defs.get("end_screen_task")
    check(d is not None and str(d.preload).endswith("CORE"), "end_screen_task 常驻（不进 load_tools）",
          str(getattr(d, "preload", None)))
    check(len(M._END_SCREEN_TASK_MANIFEST["description"]) < 250 and not M._END_SCREEN_TASK_MANIFEST["parameters"]["properties"],
          "说明简短、没有参数（只能退出）")


def t_locate_wording() -> None:
    print(chr(10) + "▶ 定位口径（D17 / D18）")
    from core.tools import manifests as M
    cu = M.BUILTIN_MANIFESTS["computer_use"]["description"]
    check("read_window_tree" in cu and "plusButton" in cu and "'加'" in cu,
          "computer_use 说明：先读控件树，用真实名字 / id 当 target（界面语言可能不同）")
    scr = S.def_text("core.orchestrator", "_format_look_targets", owner="Orchestrator") \
        if "_format_look_targets" in S.module_text("core.orchestrator") else ""
    src = S.module_text("core.orchestrator")
    check("click them directly with" not in src and "click(target='<name>')" in src,
          "look_at_screen 只列目标名字，不再给「直接点这些坐标」")
    vis = S.def_text("core.os_layer.executor_vision", "_locate_uia", owner="VisionLocator")
    check("AutomationId" in vis and "_aid_hit" in vis, "UIA 定位认 AutomationId")
    low = S.def_text("core.os_layer.executor_low", "_read_window_tree_sync", owner="LowLevelExecutor")
    check("get_target_window()" in low and '"id"' in low and "_WINDOW_TREE_MAX_NODES" in low,
          "read_window_tree 读目标窗口（排除 Nano 自己）、带 id、有节点上限")
    del scr


def t_chip_wiring() -> None:
    print(chr(10) + "▶ Temp Auto 芯片")
    chip = S.def_text("app", "_refresh_auto_chip", owner="WebUI")
    check("_rt_auto_authorized()" in chip and "'Temp Auto'" in chip and "--nano-info" in chip,
          "临时授权存在时显示 Temp Auto（读授权租约，换颜色）")
    check("no-parent-event" in chip and "_auto_chip_state" in chip,
          "Temp Auto 期间点击不展开；状态没变时不重画")
    menu = S.def_text("app", "_open_auto_menu", owner="WebUI")
    check("if _rt_auto_authorized():" in menu, "菜单入口在 Temp Auto 期间直接返回")
    check("gui._refresh_auto_chip()" in S.module_text("app"), "1 秒 tick 刷新芯片")


def t_screen_action_gate(tmp: pathlib.Path) -> None:
    print("\n▶ 鼠标 / 键盘 / 窗口动作只能在 GUI 任务内")
    k = make_kernel(tmp / "e")
    o = make_orch()
    calls: list = []

    class _Disp:
        async def execute(self, instr):
            calls.append(instr.get("action"))
            yield {"type": "result", "ok": True, "action": instr.get("action"), "data": {},
                   "summary": "done", "error": ""}

    async def step(action):
        res = None
        async for ev in o._execute_dsl_step({"action": action, "params": {}}, _Disp(), None, "m"):
            if "_step_result" in ev:
                res = ev["_step_result"]
        return res

    r = asyncio.run(step("click"))
    check(r and r["ok"] is False and "no_screen_task" in r["error"] and "set_window_mode('mini')" in r["error"]
          and calls == [], "没有 GUI 任务：click 不执行，告诉模型先缩窗开始任务", (r or {}).get("error", "")[:50])
    for a in ("drag", "type_text", "hotkey", "win_close"):
        calls.clear()
        r = asyncio.run(step(a))
        check(r["ok"] is False and calls == [], f"没有 GUI 任务：{a} 同样不执行")
    calls.clear()
    asyncio.run(step("screenshot"))
    check(calls == ["screenshot"], "只读动作（screenshot）不需要任务")
    o._gui_task_begin("test")
    calls.clear()
    asyncio.run(step("click"))
    check(calls == ["click"], "GUI 任务内：click 交给执行层")
    o._gui_task_end("cleanup")

    hits = [m for m in ("core.os_layer.dispatch", "core.orchestrator", "app")
            if "annotated_image_path" in S.module_text(m) or "_annotate_screenshot" in S.module_text(m)]
    check(not hits, "逐次点击授权的标注图已删除（生成、事件字段、界面渲染）", ", ".join(hits))


def t_wiring() -> None:
    print("\n▶ 接线")
    osrc = S.module_text("core.orchestrator")
    check("self._gui_task_end(\"conversation reset\")" in S.def_text("core.orchestrator", "reset_conversation", owner="Orchestrator"),
          "重置对话结束 GUI 任务")
    check(osrc.count("self._gui_task_end(\"emergency stop\")") == 3,
          "急停结束 GUI 任务：热键回调 + 动作返回 aborted 的两条路（os_execute / os_skill）")
    check("_gui_task_track_turn(self._run_react_loop(" in S.def_text("core.orchestrator", "_handle_query_impl", owner="Orchestrator"),
          "普通轮经过 _gui_task_track_turn")
    check(osrc.count("_gui_task_track_turn(self._run_react_loop(") == 2, "唤醒轮也经过 _gui_task_track_turn")
    check("self._take_window_note()" in osrc, "工具结果附窗口说明")
    check('register_tick_step("gui_task_idle"' in osrc and "self._install_gui_task_idle_tick()" in osrc,
          "空闲兜底登记在 runtime reconcile 周期 tick 上")

    from core.tools import manifests as M
    by = {m["name"]: m["description"] for m in (M._SET_WINDOW_MODE_MANIFEST, M._LOOK_AT_SCREEN_MANIFEST)}
    swm = by["set_window_mode"]
    check("revokes the temporary automatic authorization" in swm and "15 idle minutes" in swm,
          "set_window_mode 说明：full 结束任务、收回授权，忘了调的后果")
    check("does NOT end with the turn" in swm, "说明：任务不随轮结束")
    check("FIRST shrink yourself" not in by["look_at_screen"], "look_at_screen 说明不再要求先缩窗")

    app = "\n".join(l for l in S.module_text("app").splitlines() if not l.strip().startswith("#"))
    enter = S.def_text("app", "_enter_mini", owner="WebUI")
    exit_ = S.def_text("app", "_exit_mini", owner="WebUI")
    check("gui_session" not in enter and "gui_session" not in exit_ and "temp_auto" not in exit_,
          "界面的 mini 开关只改窗口形态")
    check("_sync_window_with_gui_task()" in app, "1 秒 tick 在 GUI 任务结束后恢复窗口")
    check("_exit_mini_if_active" not in app, "零调用方的旧兜底已删除")


if __name__ == "__main__":
    print("=" * 74)
    print("GUI 任务生命周期")
    print("=" * 74)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_begin_end(tmp)
        t_set_window_mode(tmp)
        t_turns_and_stops(tmp)
        t_idle(tmp)
        t_screen_action_gate(tmp)
        t_emergency_stop(tmp)
        t_batch_and_end_tool(tmp)
    t_wiring()
    t_locate_wording()
    t_chip_wiring()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
