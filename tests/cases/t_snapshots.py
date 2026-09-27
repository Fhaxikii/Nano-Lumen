# -*- coding: utf-8 -*-
"""界面常驻显示的后端状态快照（S6-6b C 类，`core/snapshots.py`）。

- 后端按周期重算；内容指纹变了才发轮外事件 `state_snapshot`，没变不发；
  以 `_` 开头的键不进指纹；算失败 / 发失败本跳不算数，下一跳重来。
- `current()` 给新连上的界面取全量（没算过的先算）。
- 各份快照的内容：接管阶段（六格 → 三种阶段）、GUI 任务与临时授权、后台任务（枚举转字符串、
  Subagent 步数、排队 id）、监控面板可用性、还活着的等待（带计算时刻）、预算。
- 界面：收到快照按名字重画；七块渲染不再自己读后端；排队 / 定时器接线；
  等待 pill 只按「比它晚算出来的」快照收尾。

测试不碰真实 data/：内核用临时库，其余读数用替身。

用法：
  py -3.10 tests\\cases\\t_snapshots.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402
from tests._patch import patch_global  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core import snapshots as SN  # noqa: E402
from core.runtime import events as EV  # noqa: E402
from core.runtime import task as T  # noqa: E402
from core.runtime import waitcond as W  # noqa: E402
from core.runtime.clock import FakeClock  # noqa: E402
from core.runtime.kernel import reset_kernel_for_tests  # noqa: E402
from core.runtime.store import RuntimeStore  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _Pub:
    """把 `events.publish` 换成记录。"""

    def __init__(self, fail=False):
        self.got, self.fail = [], fail

    def __call__(self, ev, turn_id=None):
        if self.fail:
            raise RuntimeError("bus down")
        self.got.append(ev)

    def __enter__(self):
        self._orig = EV.publish
        EV.publish = self
        return self

    def __exit__(self, *a):
        EV.publish = self._orig


def t_push_on_change() -> None:
    print("\n▶ 内容变了才推")
    SN._reset_for_tests()
    box = {"v": 1, "_at": 0.0}
    SN.register("x", lambda: dict(box))
    with _Pub() as pub:
        a = SN.tick("x")
        b = SN.tick("x")
        box["_at"] = 99.0
        c = SN.tick("x")
        box["v"] = 2
        d = SN.tick("x")
    check(a and not b, "第一次推，内容没变不推", f"{a} {b}")
    check(not c, "只有 `_` 开头的键变了不推")
    check(SN.current("x").get("_at") == 99.0, "但 `current()` 拿得到最新的那一份（新的 `_at`）")
    check(d and len(pub.got) == 2 and pub.got[-1] == {"event": "state_snapshot", "name": "x",
                                                      "data": {"v": 2, "_at": 99.0}},
          "内容变了再推一次，事件形状 {event, name, data}", str(pub.got[-1:]))

    def _boom():
        raise ValueError("db locked")
    SN.register("y", _boom)
    with _Pub() as pub2:
        e = SN.tick("y")
    check(not e and not pub2.got, "算失败本跳不发")

    SN.register("z", lambda: {"v": 1})
    with _Pub(fail=True):
        f1 = SN.tick("z")
    with _Pub() as pub3:
        f2 = SN.tick("z")
    check(not f1 and f2 and len(pub3.got) == 1, "发送失败不记指纹，下一跳重发")

    SN._reset_for_tests()
    SN.register("q", lambda: {"v": 7})
    with _Pub():
        cur = SN.current()
    check(cur == {"q": {"v": 7}}, "`current()` 没算过的先算一次", str(cur))
    SN._reset_for_tests()


def t_takeover_phase() -> None:
    print("\n▶ 接管阶段")
    H = 12.0
    now = 1000.0
    check(SN.takeover_phase(now + H - 0.5, False, H, now) == {"phase": "yielding"},
          "还在收手 + 刚动过 → yielding")
    check(SN.takeover_phase(now + H - 0.5, True, H, now) == {"phase": "paused"},
          "已停下 + 刚动过 → paused")
    a = SN.takeover_phase(now + H - 5, True, H, now)
    b = SN.takeover_phase(now + H - 5, False, H, now)
    check(a == b == {"phase": "countdown", "seconds": 7}, "停手 ≥2 秒 → 倒计时，两格相同", str(a))
    check(SN.takeover_phase(now + 0.3, True, H, now)["seconds"] == 1, "向上取整，不显示 0 秒")
    check(SN.takeover_phase(now + H - 1.9, True, H, now)["phase"] == "paused"
          and SN.takeover_phase(now + H - 2.1, True, H, now)["phase"] == "countdown",
          "2 秒边界")


def t_session_state() -> None:
    print("\n▶ session 快照：接管 / GUI 任务 / 临时授权")
    import core.runtime.kernel  # noqa: F401
    from core.runtime import oslease as OL
    lease = types.SimpleNamespace(holder=OL.Holder.USER, held_until=time.time() + 100)
    restores = [patch_global("core.runtime.kernel", "get_kernel", lambda: None),
                patch_global("core.runtime.oslease", "current_activity", lambda k, now=None: lease),
                patch_global("core.runtime.oslease", "is_parked", lambda: True),
                patch_global("core.runtime.oslease", "gui_session_active", lambda k, now=None: True),
                patch_global("core.runtime.oslease", "temp_auto_authorized", lambda: True)]
    try:
        s1 = SN.session_state()
        lease.holder = OL.Holder.NANO
        s2 = SN.session_state()
    finally:
        for r in restores:
            r()
    check(s1["takeover"] == {"phase": "paused"} and s1["gui_session"] and s1["temp_auto"],
          "用户占着 → 给阶段；GUI 任务与临时授权照实", str(s1))
    check(s2["takeover"] is None, "不是用户占着 → 没有接管")

    def _boom(*a, **k):
        raise RuntimeError("x")
    restores = [patch_global("core.runtime.kernel", "get_kernel", lambda: None),
                patch_global("core.runtime.oslease", "current_activity", _boom),
                patch_global("core.runtime.oslease", "gui_session_active", _boom),
                patch_global("core.runtime.oslease", "temp_auto_authorized", _boom)]
    try:
        s3 = SN.session_state()
    finally:
        for r in restores:
            r()
    check(s3 == {"takeover": None, "gui_session": False, "temp_auto": False},
          "读失败按「没有」处理（不留假警报、不误报授权）", str(s3))


def t_tasks_state() -> None:
    print("\n▶ tasks 快照")
    import enum

    class _E(enum.Enum):
        RUNNING = "RUNNING"

    def _rec(tid, **kw):
        d = dict(task_id=tid, goal_summary="Agent · x", terminal_reason=None, kind=_E.RUNNING,
                 placement="background", created_at=1.0, updated_at=2.0, execution=_E.RUNNING)
        d.update(kw)
        return types.SimpleNamespace(**d)

    import core.runtime.kernel  # noqa: F401
    import core.runtime.task  # noqa: F401
    restores = [patch_global("core.runtime.kernel", "get_kernel", lambda: None),
                patch_global("core.runtime.task", "live_background_jobs",
                             lambda k: [_rec("t1"), _rec("t2")]),
                patch_global("core.runtime.task", "finished_background_jobs",
                             lambda k: [_rec("t0", terminal_reason="DONE")]),
                patch_global("core.runtime.task", "queued_background_jobs",
                             lambda k: [_rec("t2")])]
    agent = types.SimpleNamespace(agent_transcript=lambda tid: [1, 2, 3] if tid == "t1" else [])
    try:
        d = SN.tasks_state(agent)
    finally:
        for r in restores:
            r()
    import json
    check(json.loads(json.dumps(d)) == d, "可序列化")
    check([r["task_id"] for r in d["running"]] == ["t1", "t2"] and d["queued_ids"] == ["t2"],
          "在跑与排队分开给", str(d["queued_ids"]))
    check(d["running"][0]["steps"] == 3 and "steps" not in d["finished"][0],
          "在跑的带 Subagent 步数（抽屉按步数重画）")
    check(d["running"][0]["execution"] == "RUNNING" and d["finished"][0]["terminal_reason"] == "DONE",
          "枚举转成字符串")


def t_waits_state(tmp: pathlib.Path) -> None:
    print("\n▶ waits 快照")
    T.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=FakeClock(1_700_000_000.0))
    rec = W.open_wait(reason="t", wake_on=["background"], bg_ref="b1")
    before = time.time()
    d = SN.waits_state()
    check(d["live"] == [rec.wait_id] and d["_at"] >= before, "列出还活着的等待 + 计算时刻", str(d))
    W.cancel_wait(rec.wait_id)
    check(SN.waits_state()["live"] == [], "取消后不在里面")


def t_health_budget() -> None:
    print("\n▶ health / budget 快照")
    from core import health as HM

    class _St:
        def __init__(self, cap, status):
            self.capability, self.status = cap, status

    class _Reg:
        def card_status(self, card):
            return {"rag": _St("rag", HM.Status.UNAVAILABLE),
                    "net": _St("net", HM.Status.RECOVERING)}.get(card)

        def problems(self):
            return [_St("tesseract_x", HM.Status.DEGRADED)]
    r1 = patch_global("core.health", "get_health", lambda: _Reg())
    try:
        h = SN.health_state()
    finally:
        r1()
    check(h["cards"] == {"rag": "FAULT", "full_file": "", "net": "RECOVERING"},
          "有专属卡片的三项给档位", str(h["cards"]))
    check(h["env"] == {"count": 1, "worst": "DEGRADED"}, "没有专属卡片的归环境卡", str(h["env"]))

    from core.usage import usage_tracker as UT
    saved = {n: UT.__dict__.get(n) for n in ("load_config", "cap_status", "today_cost")}
    UT.load_config = lambda: {"soft_cap_usd": 5.0, "hard_cap_usd": 10.0}
    UT.cap_status = lambda: "soft"
    UT.today_cost = lambda: 6.123456
    try:
        b = SN.budget_state()
    finally:
        for n, v in saved.items():
            if v is None:
                UT.__dict__.pop(n, None)
            else:
                setattr(UT, n, v)
    check(b == {"status": "soft", "cost": 6.1235, "soft_cap": 5.0, "hard_cap": 10.0},
          "预算档位、花费、两条上限", str(b))


def _code(fn_name: str) -> str:
    """函数的代码本身（去掉 docstring 与注释），检查「还读不读后端」时不被历史说明命中。"""
    import ast
    node = ast.parse(S.def_text("app", fn_name, owner="WebUI").strip())
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and body
                and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            n.body = body[1:] or [ast.Pass()]
    return ast.unparse(node)


def t_ui_side() -> None:
    print("\n▶ 界面：照快照画")
    import app as A
    ui = object.__new__(A.WebUI)
    ui._snap = {"tasks": {"running": [{"task_id": "t1", "updated_at": 5.0, "steps": 2}],
                          "finished": [{"task_id": "f_old", "updated_at": 1.0},
                                       {"task_id": "f_new", "updated_at": 9.0}],
                          "queued_ids": ["t1"]}}
    ui._bg_finished_hidden_before = 3.0
    run, fin = ui._bg_snapshot()
    check(run[0].task_id == "t1" and run[0].steps == 2 and ui._bg_queued_ids == {"t1"},
          "抽屉照快照：在跑、步数、排队 id")
    check([r.task_id for r in fin] == ["f_new"], "Clear 只藏这一刻之前结束的（界面这边）")

    settled = []
    ui._pill_settle_words = lambda sid, t, c: (t, c)
    ui._settle_waiting_pill = lambda sid, txt, color=None: settled.append(sid)
    now = time.time()
    ui._waiting_pills = {"w_live": {"created": now - 50}, "w_done": {"created": now - 50},
                         "w_new": {"created": now + 5}}
    ui._snap["waits"] = {"live": ["w_live"], "_at": now}
    ui._settle_all_waiting_pills()
    check(settled == ["w_done"],
          "只收不在活着名单里、且早于快照登记的 pill（刚登记的不按旧快照判）", str(settled))
    settled.clear()
    ui._snap["waits"] = {"live": []}
    ui._settle_all_waiting_pills()
    check(not settled, "没有带计算时刻的快照 → 一条都不收（不谎报完成）")

    src = S.module_text("app")
    live_src = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    ap = S.def_text("app", "_apply_snapshot", owner="WebUI")
    for n in ("pinned", "tasks", "session", "net", "health", "waits", "budget"):
        check(f'"{n}":' in ap, f"快照 {n} 有对应的重画")
    check('elif _kind == "state_snapshot":' in src, "轮外事件 state_snapshot 进 `_apply_snapshot`")
    check("ui.timer(0.05, self._load_snapshots, once=True)" in src, "界面起来先取一份全量")
    for fn, bad in (("refresh_pinned_interactions", ("list_live", "get_kernel", "_get_pending_skill")),
                    ("_bg_snapshot", ("get_kernel", "live_background_jobs")),
                    ("_refresh_tasks_panel", ("agent_transcript",)),
                    ("_refresh_takeover_bar", ("current_activity", "oslease")),
                    ("_refresh_auto_chip", ("temp_auto_authorized", "oslease")),
                    ("_sync_window_with_gui_task", ("gui_session_active",)),
                    ("_update_net_status", ("web_status", "get_mcp_manager")),
                    ("_refresh_monitor_health", ("get_health",)),
                    ("_card_is_faulted", ("get_health",)),
                    ("_update_cost_warning", ("usage_tracker.",))):
        body = _code(fn)
        hit = [b for b in bad if b in body]
        check(not hit, f"{fn} 不再自己读后端", ", ".join(hit))
    for gone in ("ui.timer(1.5, self.refresh_pinned_interactions)",
                 "ui.timer(2.0, self._refresh_tasks_panel)", "ui.timer(3, self._update_net_status)",
                 "ui.timer(20.0, self._update_cost_warning)", "ui.timer(5, _suspension_tick)",
                 "ui.timer(1, _takeover_bar_tick)"):
        check(gone not in live_src, f"不再有 {gone}")


def main() -> int:
    t_push_on_change()
    t_takeover_phase()
    t_session_state()
    t_tasks_state()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        t_waits_state(pathlib.Path(d))
    t_health_budget()
    t_ui_side()
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
