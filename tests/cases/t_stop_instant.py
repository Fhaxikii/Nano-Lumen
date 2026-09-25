# -*- coding: utf-8 -*-
"""终止按钮的即时反馈（app.py `_stoppable_stream` / `_discard_after_stop` / `_turn_running`）。

- 用户按终止时，包装后的事件流立刻给出一个界面侧的终止事件，不等后端的下一个事件；
  之后继续转发后端事件，直到后端这一轮结束（期间一直持有 pipeline_lock）。
- 后端事件流里的异常原样抛给消费方。
- 终止后到达的、等待回复的确认 / 选择卡自动回「取消」；终止时本轮已显示、仍在等回复的确认也回「取消」
  （不碰 Subagent 的确认）。
- 按过终止后 `_turn_running` 为假（按钮变回发送），尽管锁可能还没释放。
- 终止的事实由后端写进历史，界面不写。

用法：
  py -3.10 tests\\cases\\t_stop_instant.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


_LOG = types.SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                             warning=lambda *a, **k: None)


def _load(name: str):
    import textwrap
    src = textwrap.dedent(S.def_text("app", name, owner="WebUI"))
    ns: dict = {"asyncio": asyncio, "logger": _LOG}
    exec(src, ns)
    return ns[name]


def _view_session_cls():
    """app.py 里真实的 `ViewSession`（回应期状态不是 dict；用 dict 冒充会漏掉 isinstance 之类的差异）。"""
    import ast
    src = S.module_text("app")
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ViewSession")
    ns: dict = {}
    exec("from collections.abc import Mapping\n" + ast.get_source_segment(src, node), ns)
    return ns["ViewSession"]


def t_stream() -> None:
    print("\n▶ 终止立即生效，后端事件继续被消费")
    stoppable = _load("_stoppable_stream")

    async def run():
        release = asyncio.Event()
        backend_done = []

        async def backend():
            yield {"event": "a"}
            await release.wait()           # 模拟一次还没返回的 API 调用
            yield {"event": "turn_interrupted", "stopped": True, "where": "real"}
            backend_done.append(True)

        VS = _view_session_cls()
        rs = VS()
        got = []
        t_stop = None

        async def consume():
            nonlocal t_stop
            async for ev in stoppable(None, backend(), rs):
                got.append((ev.get("event"), ev.get("where"), time.monotonic()))

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        # 走真实的 `_request_stop` / `_turn_running`（self 用替身，回应期用真实 ViewSession）
        request_stop = _load("_request_stop")
        turn_running = _load("_turn_running")
        lock = asyncio.Lock()
        await lock.acquire()
        stops = []
        me = types.SimpleNamespace(
            agent=types.SimpleNamespace(request_stop=lambda src="": stops.append(src)),
            _resp_state=rs, pipeline_lock=lock, _refresh_send_btn=lambda: None,
            _dismiss_confirms_of=lambda *a, **k: None)
        check(turn_running(me) is True, "按终止之前：锁在、按钮是终止")
        t_stop = time.monotonic()
        request_stop(me)
        check(stops and rs.get("stop_clicked") is True, "终止按钮置标志并通知后端", str(stops))
        check(turn_running(me) is False, "按终止之后：锁还在，但按钮立刻变回发送")
        await asyncio.sleep(0.05)
        check([g[0] for g in got] == ["a", "turn_interrupted"] and got[1][1] == "stop button",
              "按终止后立刻得到界面侧终止事件（后端还卡在 API 调用里）", str([g[:2] for g in got]))
        check(got[1][2] - t_stop < 0.05, "延迟小于 50ms", f"{(got[1][2] - t_stop) * 1000:.1f}ms")
        check(not task.done(), "包装器仍在等后端（锁不会提前释放）")
        release.set()
        await asyncio.wait_for(task, 2)
        lock.release()
        check([g[1] for g in got] == [None, "stop button", "real"] and backend_done == [True],
              "后端之后的事件照常转发，后端这一轮完整跑完")

        async def boom():
            yield {"event": "x"}
            raise ValueError("bad")

        err = None
        try:
            async for _ in stoppable(None, boom(), {}):
                pass
        except ValueError as e:
            err = e
        check(isinstance(err, ValueError), "后端异常原样抛给消费方")

        rs2 = _view_session_cls()()
        rs2["stop_clicked"] = True
        seen = []
        async for ev in stoppable(None, backend(), rs2):
            seen.append(ev.get("where"))
            if len(seen) == 1:
                release.set()
        check(seen[0] == "stop button", "开始前就按过终止：第一个就是终止事件", str(seen))

    asyncio.run(run())


def t_discard() -> None:
    print("\n▶ 终止后到达的确认自动取消")
    discard = _load("_discard_after_stop")
    from core.runtime import replies as R
    got = []
    rid = R.register({"confirm": lambda: got.append("confirm"), "cancel": lambda: got.append("cancel")})
    discard(None, {"event": "execution_confirm", "reply_id": rid, "actions": ["confirm", "cancel"]})
    check(got == ["cancel"], "执行确认回「取消」", str(got))
    r2 = R.register({"approve": lambda: got.append("approve"), "reject": lambda: got.append("reject")})
    discard(None, {"event": "mini_auth_request", "reply_id": r2, "actions": ["approve", "reject"]})
    r3 = R.register({"choice": lambda v: got.append("choice"), "dismiss": lambda: got.append("dismiss")})
    discard(None, {"event": "user_choice_request", "cards": [{"reply_id": r3, "actions": ["choice", "dismiss"]}]})
    check(got == ["cancel", "reject", "dismiss"], "缩窗授权回「拒绝」、选择卡回「跳过」", str(got))
    discard(None, {"event": "final_text_delta", "delta": "x"})
    check(True, "普通事件直接丢弃，不报错")
    for r in (rid, r2, r3):
        R.discard(r)


def t_stop_cancels_pending_confirms() -> None:
    print("\n▶ 终止时，本轮还在等回复的确认回「取消」")
    from core.runtime import replies as R
    note = _load("_note_reply_ids")
    discard = _load("_discard_after_stop")
    request_stop = _load("_request_stop")
    rs = _view_session_cls()()
    got = []
    rid = R.register({"confirm": lambda: got.append("confirm"), "cancel": lambda: got.append("cancel")})
    note(rs, {"event": "execution_confirm", "reply_id": rid, "actions": ["confirm", "cancel"]})
    note(rs, {"event": "final_text_delta", "delta": "x"})
    check(rs.get("reply_ids") == [(rid, ["confirm", "cancel"])], "记下了本回应期显示过的确认", str(rs.get("reply_ids")))
    other = R.register({"confirm": lambda: got.append("sub-confirm"), "cancel": lambda: got.append("sub-cancel")})
    me = types.SimpleNamespace(agent=types.SimpleNamespace(request_stop=lambda src="": None),
                               _resp_state=rs, _refresh_send_btn=lambda: None,
                               _dismiss_confirms_of=lambda *a, **k: None)
    me._discard_after_stop = lambda step: discard(me, step)
    request_stop(me)
    check(got == ["cancel"], "按终止：本轮等待中的确认回「取消」（后端不必等到超时）", str(got))
    check(R.resolve(other, "cancel") is True and got[-1] == "sub-cancel",
          "不属于本回应期的确认（如 Subagent 的）不受影响")
    R.discard(rid)
    R.discard(other)


class _Elem:
    def __init__(self):
        self.visible = True
        self.text = ""
        self.closed = False
        self.deleted = False

    def set_visibility(self, v):
        self.visible = v

    def set_text(self, s):
        self.text = s

    def style(self, *_a, **_k):
        return self

    def close(self):
        self.closed = True

    def delete(self):
        self.deleted = True


def t_stop_cleans_ui() -> None:
    print("\n▶ 终止后界面不留残骸：工具行转圈停下、本回应期的确认卡收掉")
    import contextlib
    register = _load("_register_pending_confirm")
    dismiss_of = _load("_dismiss_confirms_of")
    discard = _load("_discard_after_stop")
    VS = _view_session_cls()
    rs, other_rs = VS(), VS()
    me = types.SimpleNamespace(_ui_scope=contextlib.nullcontext, _pending_confirm_dialogs=None)
    d_mine, d_sub, d_other = _Elem(), _Elem(), _Elem()
    chip = _Elem()
    me._confirm_owner = rs
    register(me, d_mine, {"chip": chip})
    me._confirm_owner = None                 # 轮外队列（Subagent）弹出的
    register(me, d_sub, {"chip": None})
    me._confirm_owner = other_rs
    register(me, d_other, {"chip": None})
    dismiss_of(me, rs, "test")
    check(d_mine.closed and chip.deleted, "本回应期的确认卡连同最小化悬浮条一起收掉")
    check(not d_sub.closed and not d_other.closed, "Subagent / 其他回应期的确认卡不动")
    check([e[0] for e in me._pending_confirm_dialogs] == [d_sub, d_other], "登记表里只摘掉本回应期的")

    spin, done = _Elem(), _Elem()
    rs["action_refs"] = {"a1": {"spin": spin, "done": done}}
    settled = []
    me._settle_tool_pill = lambda r: settled.append(r.get("batch_fail_count"))
    discard(me, {"event": "tool_end", "action_id": "a1", "ok": False}, rs)
    check(not spin.visible and done.text == "✕", "终止后到达的 tool_end 仍然停掉那一行的转圈并画上结果")
    check(settled == [1], "工具失败时 pill 重新定型为失败", str(settled))
    rq = S.def_text("app", "_request_stop", owner="WebUI")
    check("self._dismiss_confirms_of(_rs_now" in rq, "终止按钮收掉本回应期的确认卡")
    app = S.module_text("app")
    check("self._confirm_owner = _rs" in app and "self._confirm_owner = None" in app,
          "轮内事件流弹出的确认归本回应期，轮外队列的不归")


def t_wiring() -> None:
    print("\n▶ 接线")
    app = S.module_text("app")
    tr = S.def_text("app", "_turn_running", owner="WebUI")
    check("stop_clicked" in tr and "pipeline_lock.locked()" in tr, "按过终止后 _turn_running 为假（按钮变回发送）")
    rq = S.def_text("app", "_request_stop", owner="WebUI")
    check('_rs_now["stop_clicked"] = True' in rq and "_evt.set()" in rq, "终止按钮置标志并唤醒包装器")
    check("async for step in self._stoppable_stream(_stream, _rs):" in app, "navigate_pipeline 走包装后的事件流")
    check('if _rs.get("ui_stopped"):' in app and "self._discard_after_stop(step, _rs)" in app,
          "界面收尾后到达的事件不再显示")
    code = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))
    check("the user pressed Stop" not in code, "界面不再写终止事实")
    rl = S.def_text("core.orchestrator", "_interject_stop_event", owner="Orchestrator")
    check("the user pressed Stop" in rl and "add_system_note" in rl, "终止事实由后端在真正停下时写")


if __name__ == "__main__":
    print("=" * 74)
    print("终止按钮即时反馈")
    print("=" * 74)
    t_stream()
    t_discard()
    t_stop_cancels_pending_confirms()
    t_stop_cleans_ui()
    t_wiring()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
