# -*- coding: utf-8 -*-
"""终止之后回应期到此为止；排着的消息、手头的载体、本轮用量各归各位。

复现的实测场景（cmd_log/18）：一条命令交还后在手头跑完、唤醒进了队列；用户插话（续接段排队），
随后按终止，又发了一条（排队）。修之前：唤醒轮继承了上一轮的终止标志、续接段接进被终止的气泡、
排队那条渲染到被清空的旧元素上，两句「现在几点」都看不到回答，库里那条还报「从未投递」。

- 调度器：每一轮开始清终止标志；被终止的一轮结束时，排着的续接段改成新的一轮；
  这段回应期里交还、还在手头的载体一并处理（命令终止，停不掉的自行结束，已完成的唤醒不起轮），
  结果以系统事件记入历史；抽屉里的、别的回应期的载体不受影响。
- 唤醒轮后先收掉 inbox 记录再排空（库里同一时刻只能有一条「正在处理」）。
- 本轮用量随 `final_result` 带出（`turn_usage`），界面不自己按上下文去取。
- 界面：每条消息的气泡按 key 取回；排队不再清空气泡、不再占用 `_resp_state`；
  终止作用在后端正在跑的那一轮；被终止的回应期不再被唤醒轮续接。
- 工具行：交回措辞但已经做成的管理操作（Skill 禁用 / 启用、MCP 管理）标 ✓。
- 其它：running task 挂进最新回复的元信息行；高危确认倒计时 3 秒；知识库读文件展开环境变量。

测试不碰真实 data/：内核用临时库，用量只动内存计数。

用法：
  py -3.10 tests\\cases\\t_stop_epoch.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402
from tests._patch import patch_global  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core import session as SS  # noqa: E402
from core.os_layer import longcmd as _LC  # noqa: F401,E402
from core.runtime import carriers as CAR  # noqa: E402
from core.runtime import inbox as IB  # noqa: E402
from core.runtime import task as T  # noqa: E402
from core.runtime import waitcond as W  # noqa: E402
from core.runtime.clock import FakeClock  # noqa: E402
from core.runtime.kernel import get_kernel, reset_kernel_for_tests  # noqa: E402
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
    def __enter__(self):
        import core.usage as U
        self._orig = U.sync_budget_health
        U.sync_budget_health = lambda: "ok"
        return self

    def __exit__(self, *a):
        import core.usage as U
        U.sync_budget_health = self._orig


class _Agent:
    """handle_query 按消息文字跑一段预设剧本；终止标志与真实 orchestrator 同名同义。"""

    def __init__(self):
        self.stop = False
        self.cleared = 0
        self.resumed = []
        self.plans = {}
        self.stop_at_start = []

    def clear_stop(self):
        self.stop = False
        self.cleared += 1

    def _stop_asked(self):
        return self.stop

    def resume_suspension(self, sid, trigger, note=""):
        self.resumed.append((sid, trigger))

        async def _gen():
            yield {"event": "final_result", "sid": sid}
        return _gen()

    def handle_query(self, text, image_parts=None, temp_file_hint=None):
        plan = self.plans.get(text)

        async def _gen():
            self.stop_at_start.append((text, self.stop))
            if plan is not None:
                async for ev in plan():
                    yield ev
            yield {"event": "final_result", "text": text}
        return _gen()


class _Presenter:
    def __init__(self):
        self.calls = []

    async def run_wake_turn(self, sid, trigger, continue_bubble, turn_id):
        self.calls.append(("wake", sid, trigger, continue_bubble))

    def settle_wake(self, sid, trigger):
        pass

    def settle_cancelled_handback(self, ref):
        self.calls.append(("cancelled_handback", ref))
        return 1

    async def render_user_turn(self, key, payload, continuation, turn_id):
        self.calls.append(("user", payload.get("text"), continuation))


def setup():
    CAR._reset_for_tests()
    sched = SS.reset_for_tests()
    agent, pres = _Agent(), _Presenter()
    sched.attach(agent, pres)
    CAR.set_completion_handler(sched.notify_background_done)
    return sched, agent, pres


async def _settle(n=30):
    for _ in range(n):
        await asyncio.sleep(0.01)


def _events_text() -> str:
    from core.health import get_system_events
    return get_system_events().render()


def t_measured_scenario(tmp):
    print("\n▶ 实测场景：交还的命令在手头跑完 → 插话 → 终止 → 再发一条")
    make_kernel(tmp / "a")
    sched, agent, pres = setup()
    from core.health import get_system_events
    get_system_events().clear()
    stopped_cmds = []
    restore = patch_global("core.os_layer.longcmd", "stop",
                           lambda ref, why="": stopped_cmds.append(ref) or True)
    gate = None
    keys = {}

    async def plan_first():
        nonlocal gate
        gate = asyncio.Event()

        async def _done():
            return "folder sizes: Windows 30 GB"

        async def _running():
            await gate.wait()
            return "ping output"
        W.open_wait(reason="still running: size", wake_on=["background", "timer"],
                    timer_seconds=60, bg_ref="cmd_done")
        CAR.start("计算文件夹大小", _done(), "cmd_done")
        W.open_wait(reason="still running: mcp", wake_on=["background"], bg_ref="mcp_run")
        CAR.start("外部服务 · 打开网页", _running(), "mcp_run")
        await asyncio.sleep(0.05)              # 第一个载体跑完 → 忙 → 唤醒进队列
        yield {"event": "thinking"}
        keys["cont"] = sched.submit_user_message("现在几点", can_continue=True)
        agent.stop = True                      # 用户按了终止
        keys["queued"] = sched.submit_user_message("现在几点 2", can_continue=False)
        yield {"event": "thinking"}

    agent.plans["把文件夹大小算出来"] = plan_first

    async def run():
        with _Budget():
            sched.submit_user_message("把文件夹大小算出来")
            await _settle(60)
            woken_before_release = list(agent.resumed)
            gate.set()                         # 停不掉的那个载体现在自己结束
            await _settle(30)
            return woken_before_release
    try:
        woken = asyncio.run(run())
    finally:
        restore()

    check(keys["cont"][1] == "cont" and keys["queued"][1] == "queued",
          "插话进续接段、终止后那条进队列", str(keys))
    check(not woken and not agent.resumed, "已完成的交还命令不再起唤醒轮（不会冒出汇报）", str(agent.resumed))
    users = [c for c in pres.calls if c[0] == "user"]
    check([u[1] for u in users] == ["把文件夹大小算出来", "现在几点", "现在几点 2"],
          "三条用户消息都渲染了，顺序不变", str(users))
    check(len(users) == 3 and users[1][2] is False,
          "终止前排着的续接段改成新的一轮（不续接被终止的气泡）", str(users))
    check(("现在几点", False) in agent.stop_at_start and ("现在几点 2", False) in agent.stop_at_start,
          "下一轮开始时终止标志已清", str(agent.stop_at_start))
    check(("cancelled_handback", "cmd_done") in pres.calls, "已完成那个载体的转圈由界面收掉")
    check("mcp_run" not in stopped_cmds and not agent.resumed,
          "停不掉的载体不被当成命令去停，结束时也不唤醒", str(stopped_cmds))
    ev = _events_text()
    check("folder sizes: Windows 30 GB" in ev and "ping output" in ev,
          "两件事的结果都以系统事件记入历史", ev[-300:])
    check(ev.count("do not bring this up on your own") >= 2, "系统事件带「不主动提起」的要求")
    live = [r for r in W.list_live(get_kernel()) if r.bg_ref in ("cmd_done", "mcp_run")]
    check(not live, "两条等待都已取消", str([(r.bg_ref, r.status) for r in live]))


def t_running_command_terminated(tmp):
    print("\n▶ 终止时手头还在跑的命令被终止；抽屉里的、别的回应期的不受影响")
    make_kernel(tmp / "b")
    sched, agent, pres = setup()
    stopped_cmds = []
    restore = patch_global("core.os_layer.longcmd", "stop",
                           lambda ref, why="": stopped_cmds.append(ref) or True)
    hold = {}

    def _forever(name):
        hold[name] = asyncio.Event()

        async def _c():
            await hold[name].wait()
            return f"{name} finished"
        return _c()

    async def plan_old():
        W.open_wait(reason="old", wake_on=["background"], bg_ref="cmd_old")
        CAR.start("旧回应期的命令", _forever("cmd_old"), "cmd_old")
        yield {"event": "thinking"}

    async def plan_new():
        W.open_wait(reason="new", wake_on=["background"], bg_ref="cmd_new")
        CAR.start("这一轮的命令", _forever("cmd_new"), "cmd_new")
        W.open_wait(reason="drawer", wake_on=["background"], bg_ref="cmd_drawer")
        CAR.start("抽屉里的命令", _forever("cmd_drawer"), "cmd_drawer", rt_task_id="task_x",
                  owns_record=False)
        yield {"event": "thinking"}
        agent.stop = True

    agent.plans.update({"old": plan_old, "new": plan_new})

    async def run():
        with _Budget():
            sched.submit_user_message("old")
            await _settle()
            sched.submit_user_message("new")
            await _settle()
            snap = {m["suspension_ref"] for m in CAR.snapshot()}
            for e in hold.values():
                e.set()
            await _settle()
            return snap
    try:
        snap = asyncio.run(run())
    finally:
        restore()
    check(stopped_cmds == ["cmd_new"], "只终止这一轮交还的命令", str(stopped_cmds))
    check({"cmd_old", "cmd_drawer"} <= snap, "别的回应期、抽屉里的载体照旧在跑", str(snap))
    check(len(agent.resumed) == 2, "旧回应期与抽屉里的完成照常唤醒；被终止那个不唤醒",
          str(agent.resumed))
    check("the turn" in _events_text() and "terminated together with the turn" in _events_text(),
          "被终止命令的结局记入历史")


def t_wake_consumes_before_drain(tmp):
    print("\n▶ 唤醒轮先收掉自己的 inbox 记录再排空（下一条认领得上）")
    k = make_kernel(tmp / "c")
    sched, agent, pres = setup()

    async def run():
        with _Budget():
            await sched.lock.acquire()
            rec = W.open_wait(reason="t", wake_on=["background"], bg_ref="bg1")
            await sched.drive_wake(rec.wait_id, trigger="background")      # 忙 → 进队列
            key, mode = sched.submit_user_message("排着的那条")               # 忙 → 进队列
            sched.lock.release()
            await sched.drain()
            await _settle()
            return key, mode
    key, mode = asyncio.run(run())
    it = IB.get(k, key)
    check(mode == "queued", "用户消息进队列", mode)
    check(it is not None and it.status == IB.ItemStatus.CONSUMED and it.delivery_count >= 1,
          "排在唤醒之后的那条被认领、投递、收掉",
          f"{getattr(it, 'status', None)} / {getattr(it, 'delivery_count', None)}")


def t_turn_usage():
    print("\n▶ 本轮用量随 final_result 带出")
    from core.usage import usage_tracker as UT

    async def backend():
        UT.begin_turn()
        with UT._lock:
            UT._attribute(1234)
            UT._attribute_cache(30, 100)
        yield {"event": "final_result", "content": "hi"}

    async def run():
        out = []
        async for ev in SS._with_turn_usage(backend()):
            out.append(ev)
        return out
    out = asyncio.run(run())
    tu = out[0].get("turn_usage") or {}
    check(tu.get("tokens") == 1234 and abs((tu.get("cache_hit") or 0) - 0.3) < 1e-9,
          "final_result 带本轮 token 与缓存命中率", str(tu))
    app = S.module_text("app")
    check('step.get("turn_usage")' in app and "self._turn_tok_suffix(_tok_str, _tu)" in app,
          "界面状态行用后端带来的本轮用量")


def t_ui_wiring():
    print("\n▶ 界面接线")
    app = S.module_text("app")
    check("_rt_inbox_mark_queued" not in app and "_queued_views" not in app,
          "排队不再清空那条消息的气泡")
    check("self._user_views[_key] = _view" in app, "每条消息的回应期按 key 记下")
    check("self._resp_state = _seam_live" in app, "排队的那条不占用当前回应期")
    src = S.def_text("app", "_request_stop", owner="WebUI")
    check("self._live_view()" in src, "终止作用在后端正在跑的那一轮")
    src = S.def_text("app", "run_wake_turn", owner="WebUI")
    check('not _live_rs.get("stop_clicked")' in src and "self._new_reply_view()" in src,
          "被终止的回应期不再被唤醒轮续接")
    src = S.def_text("app", "_sync_task_pill", owner="WebUI")
    check("'✳'" not in src and "_last_meta_row" in src and "_task_pill_sep" in src,
          "running task 接在最新回复计时器后面（· 分隔，无图标）")
    check("countdown = [3]" in app and "确认执行 (3)" in app and "countdown = [5]" not in app,
          "高危确认倒计时 3 秒")


def t_defer_ok():
    print("\n▶ 交回措辞但已做成的管理操作标 ✓")
    rl = S.module_text("core.orchestrator")
    check('"ok": _defer_to_model is None or _defer_ok' in rl, "tool_end 的成败认 handler 标的 ok")
    sk = S.def_text("core.orchestrator", "_handle_manage_existing_skill_decision",
                    owner="Orchestrator")
    check("ok=_done" in sk, "Skill 禁用 / 启用成功时标 ok")
    mc = S.module_text("core.orchestrator")
    check('措辞交回模型。", ok=True)' in mc, "MCP 管理 / 接入成功时标 ok")


def t_rag_expandvars(tmp):
    print("\n▶ 知识库读文件展开环境变量")
    from core import rag as R
    d = tmp / "ev"
    d.mkdir(parents=True, exist_ok=True)
    (d / "f.txt").write_text("x", encoding="utf-8")
    os.environ["NANO_T_EPOCH_DIR"] = str(d)
    try:
        p, _src = R._resolve_file_path("%NANO_T_EPOCH_DIR%" + os.sep + "f.txt")
    finally:
        os.environ.pop("NANO_T_EPOCH_DIR", None)
    check(p is not None and p.name == "f.txt", "%VAR% 形式的路径能找到文件", str(p))


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = pathlib.Path(td)
        t_measured_scenario(tmp)
        t_running_command_terminated(tmp)
        t_wake_consumes_before_drain(tmp)
        t_turn_usage()
        t_ui_wiring()
        t_defer_ok()
        t_rag_expandvars(tmp)
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
