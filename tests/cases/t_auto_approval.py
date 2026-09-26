# -*- coding: utf-8 -*-
"""Auto 放行判定在后端（S6-6b 业务状态下沉第 2 步）。

- Ask / Auto 的读写走 `dsl.user_auto_mode_on` / `dsl.set_user_auto_mode`（保留 os_state.json 其它字段）。
- OS 动作确认：Auto 开着且危险判定明确放行 → 后端直接放行、不发 `os_action_confirm`，
  结果记 `authorized_by="auto"`；判定拦下 → 照常发确认事件（带拦截类型 mismatch / undecidable，没有 auto 动作），
  弹窗标题与红色说明按类型显示（D49）；
  Auto 关着 → 发确认事件、不调判定。
- 执行确认（临时代码 / Skill / MCP 不可逆）：Auto 下不发 `execution_confirm` 直接执行；
  否则发事件，取消则不执行。
- 缩窗授权：用户选了 Auto → 事件标 `preapproved`（界面不弹授权、缩窗后回复同意）；否则不标。
- 界面不再做任何 Auto 放行判断。

测试不读写真实 data/：Auto 状态用替身函数，os_state.json 用临时目录。

用法：
  py -3.10 tests\\cases\\t_auto_approval.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core.os_layer import dsl  # noqa: E402
from core.runtime import replies as R  # noqa: E402
from core.runtime.clock import FakeClock  # noqa: E402
from core.runtime.kernel import reset_kernel_for_tests  # noqa: E402
from core.runtime.store import RuntimeStore  # noqa: E402
from core.runtime import task as T  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    T.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=FakeClock(1_700_000_000.0))


class _AutoState:
    """替身：把 dsl 的两个 Auto 读数换成可控的值（不读真实 data/os_state.json）。"""

    def __init__(self, user_auto: bool = False, any_auto: bool | None = None):
        self.user_auto = user_auto
        self.any_auto = user_auto if any_auto is None else any_auto

    def __enter__(self):
        self._orig = (dsl.user_auto_mode_on, dsl.auto_authorization_on)
        dsl.user_auto_mode_on = lambda config_path=None: self.user_auto
        dsl.auto_authorization_on = lambda config_path=None: self.any_auto
        return self

    def __exit__(self, *a):
        dsl.user_auto_mode_on, dsl.auto_authorization_on = self._orig


class _Q:
    def __init__(self):
        self.items: list = []

    async def put(self, ev):
        self.items.append(ev)


def make_orch():
    from core.orchestrator import Orchestrator
    return Orchestrator.__new__(Orchestrator)


# ── Ask / Auto 的读写 ─────────────────────────────────────────────────────
def t_user_auto_mode_io(tmp: pathlib.Path) -> None:
    print("\n▶ Ask / Auto 读写走 dsl")
    p = tmp / "os_state.json"
    p.write_text(json.dumps({"allow_file_write": True}), encoding="utf-8")
    check(dsl.user_auto_mode_on(p) is False, "没有 auto_mode 字段 → Ask")
    check(dsl.set_user_auto_mode(True, p) is True, "保存成功返回 True")
    raw = json.loads(p.read_text(encoding="utf-8"))
    check(raw.get("auto_mode") is True and raw.get("allow_file_write") is True,
          "写入 auto_mode，保留其它字段", str(raw))
    check(dsl.user_auto_mode_on(p) is True, "读回 Auto")
    dsl.set_user_auto_mode(False, p)
    check(dsl.user_auto_mode_on(p) is False, "切回 Ask")
    q = tmp / "new" / "os_state.json"
    dsl.set_user_auto_mode(True, q)
    check(dsl.user_auto_mode_on(q) is True, "文件不存在时新建")

    app = S.module_text("app")
    seg = app.split("def _load_global_auto")[1].split("def _toggle_global_auto")[0]
    check("os_dsl.user_auto_mode_on()" in seg and "os_dsl.set_user_auto_mode(" in seg,
          "界面的读 / 存走 dsl，不自己读写文件")


# ── OS 动作确认 ───────────────────────────────────────────────────────────
class _FakeDispatcher:
    def __init__(self):
        self.after: list = []

    async def execute(self, instr):
        yield {"type": "confirm_request", "action": "run_command", "effective_risk": 3,
               "params_summary": "cmd", "reason": "r", "risk_reasons": ["floor=3"],
               "_resolved_instr": instr}

    async def execute_after_confirm(self, instr, confirmed):
        self.after.append(confirmed)
        return {"ok": bool(confirmed), "action": "run_command"}


async def _run_step(o, gate, reply: str | None):
    """跑一次 `_execute_dsl_step`；弹出确认时按 `reply` 回复。返回 (事件, 结果, 判定调用次数, dispatcher)。"""
    calls = []

    async def _gate(action, params, main_model=""):
        calls.append(action)
        return gate

    o._auto_gate_verdict = _gate
    disp = _FakeDispatcher()
    evs, result = [], None
    agen = o._execute_dsl_step({"action": "run_command", "params": {"command": "echo hi"}},
                               disp, None, "m")
    async for ev in agen:
        if "_step_result" in ev:
            result = ev["_step_result"]
            break
        evs.append(ev)
        if ev.get("event") == "os_action_confirm" and reply:
            asyncio.get_running_loop().call_soon(R.resolve, ev["reply_id"], reply)
    else:
        pass
    return evs, result, calls, disp


def t_os_action_confirm(tmp: pathlib.Path) -> None:
    print("\n▶ OS 动作确认：放行判定在后端")
    make_kernel(tmp / "os")
    o = make_orch()

    async def run():
        # ① Auto + 判定放行 → 不发事件，直接执行，记 auto
        with _AutoState(any_auto=True):
            evs, res, calls, disp = await _run_step(o, (True, "", ""), reply=None)
        check(not any(e.get("event") == "os_action_confirm" for e in evs),
              "Auto + 判定放行：不发 os_action_confirm", str([e.get("event") for e in evs]))
        check(disp.after == [True] and (res or {}).get("authorized_by") == "auto",
              "直接执行，authorized_by=auto（与用户亲自同意可区分）", str(res))
        check(calls == ["run_command"], "Auto 开着才调危险判定")

        # ② Auto + 判定拦下 → 照常发确认，带理由，没有 auto 动作；用户取消 → 不执行
        with _AutoState(any_auto=True):
            evs, res, calls, disp = await _run_step(
                o, (False, "mismatch", "这条命令与你的要求对不上"), reply="cancel")
        ev = next((e for e in evs if e.get("event") == "os_action_confirm"), {})
        check(bool(ev), "Auto + 判定拦下：照常发 os_action_confirm")
        check(ev.get("auto_blocked") == "mismatch"
              and not any("拦下" in r for r in ev.get("risk_reasons", [])),
              "事件带拦截类型字段（不塞进风险原因文本）", str(ev.get("auto_blocked")))
        check(ev.get("actions") == ["confirm", "always", "cancel"] and "auto_ok" not in ev,
              "事件没有 auto 动作、不带 auto_ok（界面不做放行判断）", str(ev.get("actions")))
        check(disp.after == [False] and res and res.get("ok") is False
              and res.get("authorized_by") == "user_denied",
              "用户取消 → 不执行，记 user_denied", str(res))

        # ③ Ask → 发确认、不调判定；用户同意 → 执行，记 user_once
        with _AutoState(any_auto=False):
            evs, res, calls, disp = await _run_step(o, (True, "", ""), reply="confirm")
        check(any(e.get("event") == "os_action_confirm" for e in evs) and calls == [],
              "Ask：发确认事件，不调危险判定")
        ev = next((e for e in evs if e.get("event") == "os_action_confirm"), {})
        check(ev.get("auto_blocked") == "", "Ask 下的普通确认不带拦截类型（弹窗照旧）")
        check(disp.after == [True] and res.get("authorized_by") == "user_once",
              "用户同意 → 执行，记 user_once", str(res))

    asyncio.run(run())


# ── 执行确认（临时代码）────────────────────────────────────────────────────
async def _scratch(code: str, reply: str | None):
    from core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._LONG_TASK_HANDBACK_SEC = 30.0
    q = _Q()
    task = asyncio.create_task(Orchestrator._handle_run_scratch_code(
        o, {"code": code, "purpose": "测试写文件"}, "aid", event_queue=q))
    for _ in range(400):
        await asyncio.sleep(0.05)
        for ev in q.items:
            if ev.get("event") == "execution_confirm" and reply and not ev.get("_done"):
                ev["_done"] = True
                R.resolve(ev["reply_id"], reply)
        if task.done():
            break
    return await task, q.items


def t_execution_confirm(tmp: pathlib.Path) -> None:
    print("\n▶ 执行确认：Auto 下后端直接通过")
    make_kernel(tmp / "ex")
    target = (tmp / "out.txt").as_posix()
    code = f"open({target!r}, 'w').write('x')"

    with _AutoState(any_auto=True):
        r, evs = asyncio.run(_scratch(code, reply=None))
    check(not any(e.get("event") == "execution_confirm" for e in evs),
          "Auto：不发 execution_confirm")
    check(pathlib.Path(target).exists() and "successfully" in r, "代码直接执行", r[:60])

    pathlib.Path(target).unlink(missing_ok=True)
    with _AutoState(any_auto=False):
        r, evs = asyncio.run(_scratch(code, reply="cancel"))
    check(any(e.get("event") == "execution_confirm" for e in evs), "Ask：发 execution_confirm")
    check(not pathlib.Path(target).exists() and "NOT run" in r, "取消 → 不执行", r[:60])

    with _AutoState(any_auto=False):
        r, evs = asyncio.run(_scratch(code, reply="confirm"))
    check(pathlib.Path(target).exists(), "同意 → 执行")

    orch = S.module_text("core.orchestrator")
    check(orch.count("auto_skips_confirmation(") == 3,
          "三处执行确认（临时代码 / MCP 不可逆 / Skill 副作用）都经后端放行判定",
          str(orch.count("auto_skips_confirmation(")))


# ── 缩窗授权 ──────────────────────────────────────────────────────────────
async def _mini(o, reply: str):
    q = _Q()
    task = asyncio.create_task(o._handle_set_window_mode({"mode": "mini"}, "aid", event_queue=q))
    for _ in range(100):
        await asyncio.sleep(0.01)
        req = next((e for e in q.items if e.get("event") == "mini_auth_request"), None)
        if req and not req.get("_done"):
            req["_done"] = True
            R.resolve(req["reply_id"], reply)
        if task.done():
            break
    await task
    return q.items


def t_mini_auth(tmp: pathlib.Path) -> None:
    print("\n▶ 缩窗授权：用户选了 Auto → preapproved")
    from core.runtime import oslease as L
    k = make_kernel(tmp / "mini")
    o = make_orch()

    with _AutoState(user_auto=True):
        evs = asyncio.run(_mini(o, "approve"))
    req = next((e for e in evs if e.get("event") == "mini_auth_request"), {})
    check(req.get("preapproved") is True, "用户选了 Auto：事件标 preapproved")
    check(bool(L.gui_session_active(k)) and o._window_mode_now() == "mini",
          "界面缩窗后回复同意 → GUI 任务开始")
    o._gui_task_end("test")

    with _AutoState(user_auto=False, any_auto=False):
        evs = asyncio.run(_mini(o, "reject"))
    req = next((e for e in evs if e.get("event") == "mini_auth_request"), {})
    check(req.get("preapproved") is False, "Ask：不标 preapproved（界面弹授权）")
    check(not L.gui_session_active(k), "拒绝 → 不开始任务")

    app = S.module_text("app")
    seg = app.split('if step.get("event") == "mini_auth_request":')[1][:600]
    check('step.get("preapproved")' in seg and "self._global_auto" not in seg,
          "界面按事件的 preapproved 决定弹不弹，不自己读 Auto")


def t_blocked_kind_and_dialog(tmp: pathlib.Path) -> None:
    print("\n▶ D49：拦截类型与弹窗说明")
    from core.os_layer import cmd_classifier as CC
    o = make_orch()
    o.memory = None
    o.provider = None

    async def verdict(ret=None, exc=None):
        orig = CC.classify

        async def fake(*a, **k):
            if exc:
                raise exc
            return ret
        CC.classify = fake
        try:
            return await o._auto_gate_verdict("run_command", {"command": "x"}, "m")
        finally:
            CC.classify = orig

    check(asyncio.run(verdict((CC.ALLOW, "")))[:2] == (True, ""), "判定安全 → 放行，无拦截类型")
    check(asyncio.run(verdict((CC.BLOCK, "r")))[:2] == (False, "mismatch"), "判定危险 → mismatch")
    check(asyncio.run(verdict((CC.UNKNOWN, "r")))[:2] == (False, "undecidable"), "判不了 → undecidable")
    check(asyncio.run(verdict(exc=RuntimeError("x")))[:2] == (False, "undecidable"),
          "判定器异常 → undecidable（照常弹窗）")

    from app import WebUI
    notes = WebUI._AUTO_BLOCKED_NOTES
    check(notes.get("mismatch") ==
          "原因：Nano所执行的操作被安全模型判定为与你的意图不符的危险指令，请人工二次核查。",
          "mismatch 的说明文字（koala 定稿）")
    check(notes.get("undecidable") ==
          "原因：安全模型无法判断这条命令是否符合你的意图，请人工二次核查。",
          "undecidable 的说明文字（koala 定稿）")
    app = S.module_text("app")
    dlg = app.split("def _show_os_action_confirm_dialog")[1].split("\n    def ")[0]
    check('"Nano操作被系统拦截 · 需要你确认"' in dlg, "被拦截时标题改为「Nano操作被系统拦截 · 需要你确认」")
    check("ui.label(_blocked_note)" in dlg and "var(--nano-danger)" in dlg
          and dlg.index("ui.label(_blocked_note)") < dlg.index("if reason:"),
          "红色说明画在详情区最上方（在 Nano 给的理由之前）")
    check("Nano操作被系统拦截 · {action}" in dlg, "收起后的悬浮条也标明被拦截")
    check("risk_reasons" not in dlg, "风险等级的计算依据继续不显示（只在审计日志）")
    pres = app.split("def _present_os_confirm")[1].split("\n    def ")[0]
    check('auto_blocked=step.get("auto_blocked", "")' in pres, "呈现入口把拦截类型传给弹窗")


def t_ui_has_no_auto_decision() -> None:
    print("\n▶ 界面不做 Auto 放行判断")
    code = "\n".join(l for l in S.module_text("app").splitlines()
                     if not l.strip().startswith("#"))
    check("def _auto_on" not in code and "_auto_on()" not in code, "界面没有 _auto_on")
    check('_reply_cb(step, "auto")' not in code, "界面不回复 auto 动作")
    seg = code.split('if step.get("event") == "execution_confirm":')[1].split('if step.get("event")')[0]
    check("_auto_on" not in seg and "_global_auto" not in seg and "auto_authorization_on" not in seg,
          "execution_confirm 分支只画弹窗")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_user_auto_mode_io(tmp)
        t_os_action_confirm(tmp)
        t_execution_confirm(tmp)
        t_mini_auth(tmp)
        t_blocked_kind_and_dialog(tmp)
        t_ui_has_no_auto_decision()
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
