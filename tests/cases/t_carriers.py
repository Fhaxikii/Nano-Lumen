# -*- coding: utf-8 -*-
"""后台载体由后端持有（S6-6b 业务状态下沉第 3 步，`core/runtime/carriers.py`）。

- 起跑 → 结束时把结果交给完成回调（唤醒等待方）；失败记 failed，结果写「后台执行失败：…」。
- 用户按 `■`（`cancel`）→ 记 cancelled，给模型的话说明是用户停的；不是用户停的另有一句。
- 权威记录：`rt_task_id` 有值且 `owns_record=True` 的由载体收尾；`owns_record=False`（Subagent）不收。
- `dont_wait`（`promote`）→ 建权威记录并转 RUNNING，幂等；找不到载体（已结束）不建。
- 监听者收到 carrier_started / carrier_promoted / carrier_finished（界面据此刷新）。
- 后端交还事件可序列化：不再带 asyncio 对象（原来的 `[Wire]` WARNING 来源）。

用法：
  py -3.10 tests\\cases\\t_carriers.py
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

from core.runtime import carriers as C  # noqa: E402
from core.runtime import task as T  # noqa: E402
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


class _Env:
    """每个场景一套干净的载体表 + 记录完成通知与监听事件。"""

    def __init__(self):
        C._reset_for_tests()
        self.done: list = []
        self.events: list = []

        async def _done(ref, hint):
            self.done.append((ref, hint))
        C.set_completion_handler(_done)
        C.add_listener(lambda ev: self.events.append(ev))


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0.01)


def t_outcomes(tmp: pathlib.Path) -> None:
    print("\n▶ 三种结局")
    k = make_kernel(tmp / "a")

    async def run():
        env = _Env()

        async def ok():
            return "command finished successfully"
        C.start("pip install", ok(), "cmd_ok", skill_name="SkillA")
        await _settle()
        check(env.done == [("cmd_ok", "command finished successfully")],
              "完成 → 完成回调收到 ref 与结果", str(env.done))
        kinds = [e["event"] for e in env.events]
        check(kinds == ["carrier_started", "carrier_finished"], "监听者收到起跑与结束", str(kinds))
        check(env.events[-1].get("skill_name") == "SkillA" and not C.skill_running("SkillA"),
              "结束后该 Skill 不再算在跑")

        env = _Env()

        async def boom():
            raise RuntimeError("disk full")
        rid = T.create_background_job("会失败的")
        C.start("会失败的", boom(), "cmd_bad", rt_task_id=rid)
        await _settle()
        check(env.done and env.done[0][1] == "后台执行失败：RuntimeError: disk full",
              "失败 → 结果写「后台执行失败：…」", str(env.done))
        check(T.get_task(k, rid).terminal_reason == T.TerminalReason.FAILED,
              "有权威记录的载体失败 → 记录收成 FAILED")

        env = _Env()
        rid2 = T.create_background_job("用户会停掉的")

        async def slow():
            await asyncio.sleep(60)
        C.start("用户会停掉的", slow(), "cmd_stop", rt_task_id=rid2)
        await asyncio.sleep(0)
        check(C.cancel(rid2) is True, "cancel 找到在跑的载体")
        await _settle()
        check(env.done and "the user manually stopped this" in env.done[0][1],
              "用户按停 → 给模型的话说明是用户停的", env.done[0][1][:60] if env.done else "")
        check(T.get_task(k, rid2).terminal_reason == T.TerminalReason.CANCELLED,
              "记录收成 CANCELLED（不并进 FAILED）")
        check(C.cancel(rid2) is False, "已结束的再按 cancel → False")

        env = _Env()
        t = asyncio.ensure_future(slow())
        C.start("系统取消的", t, "cmd_sys")
        await asyncio.sleep(0)
        t.cancel()
        await _settle()
        check(env.done and "it was not the user" in env.done[0][1],
              "不是用户停的 → 如实说不知道为什么停了")

    asyncio.run(run())
    C._reset_for_tests()


def t_record_ownership(tmp: pathlib.Path) -> None:
    print("\n▶ 权威记录只有一个收尾人")
    k = make_kernel(tmp / "b")

    async def run():
        _Env()
        rid = T.create_background_job("Agent · 查东西")

        async def ok():
            return "report"
        C.start("Agent · 查东西", ok(), "agent_x", rt_task_id=rid, owns_record=False)
        await _settle()
        check(T.get_task(k, rid).terminal_reason is None,
              "owns_record=False（Subagent）→ 载体不收它的记录（由 Subagent 自己收）",
              str(T.get_task(k, rid).terminal_reason))

        _Env()
        before = len(T.live_background_jobs(k))
        C.start("系统交还", ok(), "cmd_h")
        check(len(T.live_background_jobs(k)) == before, "系统交还（无 rt_task_id）不建用户可见的后台任务")
        await _settle()

    asyncio.run(run())
    C._reset_for_tests()


def t_promote(tmp: pathlib.Path) -> None:
    print("\n▶ dont_wait → 进抽屉")
    k = make_kernel(tmp / "c")

    async def run():
        env = _Env()

        async def slow():
            await asyncio.sleep(60)
        C.start("pip install big", slow(), "cmd_p")
        await asyncio.sleep(0)
        check(C.promote("cmd_p", "pip install big") is True, "promote 成功")
        live = T.live_background_jobs(k)
        check(len(live) == 1 and live[0].task_id not in {r.task_id for r in T.queued_background_jobs(k)},
              "建了权威记录且是 RUNNING（不显示成排队中）")
        check(C.promote("cmd_p", "pip install big") is True and len(T.live_background_jobs(k)) == 1,
              "幂等：再 promote 不建第二条")
        check(C.promote("cmd_missing", "x") is False, "找不到载体（多半刚完成）→ 不建记录")
        check(any(e["event"] == "carrier_promoted" for e in env.events), "监听者收到 carrier_promoted")
        rid = C.snapshot()[0]["rt_task_id"]
        C.cancel(rid)
        await _settle()
        check(T.get_task(k, rid).terminal_reason == T.TerminalReason.CANCELLED,
              "promote 补上的记录由载体收尾（现读表里的 id）")

    asyncio.run(run())
    C._reset_for_tests()


def t_wire_and_wiring() -> None:
    print("\n▶ 交还事件可序列化；界面不再持有载体")
    orch = "\n".join(l for l in S.module_text("core.orchestrator").splitlines()
                     if not l.strip().startswith("#"))
    check('"task":' not in orch and "'task':" not in orch,
          "后端事件里不再放 asyncio Task（原来的 [Wire] WARNING 来源）")
    check(orch.count("_carriers.start(") == 4, "四处交还（长命令 / MCP / Skill / Subagent）都由后端登记载体",
          str(orch.count("_carriers.start(")))
    from core.runtime.wire import non_serializable_paths
    ev = {"event": "long_task_handback", "bg_task_ref": "cmd_1", "display": "pip", "skill_name": ""}
    check(non_serializable_paths(ev) == [], "交还事件的字段都可序列化")
    app = S.module_text("app")
    for gone in ("_handed_back_carriers", "_start_handed_back_carrier", "_promote_carrier_to_background",
                 "def _cancel_carrier", "_is_handed_back_skill_running", "_handback_await"):
        check(gone not in app, f"界面不再有 `{gone}`")
    check("_carriers.set_completion_handler(_sched.notify_background_done)" in app
          and "_carriers.add_listener(self._on_carrier_change)" in app,
          "完成回调登记为后端调度器的 notify_background_done；界面只登记状态监听")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_outcomes(tmp)
        t_record_ownership(tmp)
        t_promote(tmp)
        t_wire_and_wiring()
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
