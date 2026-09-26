# -*- coding: utf-8 -*-
"""后端周期任务（core/heartbeat.py）与后端心跳登记（core/backend.py）。

- 调度语义：周期任务启动即跑一次、之后按间隔重复；一次性任务延迟后跑一次；异常不中断后续周期；
  同名登记覆盖；stop 取消全部。
- 登记清单：原来由界面定时器驱动的后端心跳全部在 core.backend 登记，间隔不变；
  app.py 不再用 ui.timer 驱动它们，只在启动时调用 start_backend_services。

用法：
  py -3.10 tests\\cases\\t_backend_heartbeat.py
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core import heartbeat as H  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_scheduler() -> None:
    print("\n▶ 调度语义")
    H.reset_for_tests()
    hits = {"p": 0, "a": 0, "o": 0, "e": 0}

    def periodic():
        hits["p"] += 1

    async def periodic_async():
        hits["a"] += 1

    def once():
        hits["o"] += 1

    def boom():
        hits["e"] += 1
        raise RuntimeError("x")

    async def run():
        H.register("p", 0.05, periodic)
        H.register("a", 0.05, periodic_async)
        H.register_once("o", 0.08, once)
        H.register("e", 0.05, boom)
        H.start()
        await asyncio.sleep(0.01)
        check(hits["p"] == 1 and hits["a"] == 1, "周期任务启动即跑一次（同步与 async 都支持）", str(hits))
        check(hits["o"] == 0, "一次性任务在延迟之前不跑")
        await asyncio.sleep(0.2)
        check(hits["p"] >= 3 and hits["a"] >= 3, "之后按间隔重复", str(hits))
        check(hits["o"] == 1, "一次性任务只跑一次")
        check(hits["e"] >= 3, "抛异常不中断后续周期", str(hits["e"]))
        st = H.status()
        check(st["e"]["errors"] == st["e"]["runs"] and st["p"]["errors"] == 0, "status 记录运行与出错次数")
        n = hits["p"]
        H.register("p", 10, periodic)          # 覆盖：旧任务取消，新任务立即跑一次
        await asyncio.sleep(0.12)
        check(hits["p"] == n + 1, "同名登记覆盖旧任务", f"{n} -> {hits['p']}")
        H.stop()
        m = dict(hits)
        await asyncio.sleep(0.12)
        check(hits == m, "stop 后不再运行")

    asyncio.run(run())
    H.reset_for_tests()


EXPECTED = {
    "runtime_reconcile": 5, "suspension_poll": 5, "capability_probe": 15, "budget_health": 20, "canary": 300,
    "intel_tick": 20, "cpu_sample": 60, "ambient_trail": 240,
    # 后台载体心跳（原挂在抽屉 2 秒刷新里，S6-6b 第 3 步移入后端；orphan 判定是 30 分钟）
    "carrier_heartbeat": 30,
}


def t_backend_registration() -> None:
    print("\n▶ 后端心跳登记")
    from core import backend as B
    H.reset_for_tests()

    class _Agent:
        async def maybe_run_canary(self):
            pass

        def record_ambient_trail(self):
            pass

    class _Intel:
        async def tick(self):
            pass

    real_start = H.start
    H.start = lambda: None          # 只检查登记，不真的跑（不碰真实库和 MCP 配置）
    try:
        asyncio.run(_register(B, _Agent(), _Intel()))
    finally:
        H.start = real_start
    st = H.status()
    got = {n: v["interval"] for n, v in st.items() if not v["once"]}
    check(got == EXPECTED, "周期心跳与间隔和原界面定时器一致", str(got))
    check(st.get("mcp_startup", {}).get("once") is True, "MCP 启动是一次性任务")
    H.reset_for_tests()
    H.start = lambda: None
    try:
        asyncio.run(_register(B, _Agent(), None))
    finally:
        H.start = real_start
    check("intel_tick" not in H.status(), "没有主动智能引擎时不登记 intel_tick")
    H.reset_for_tests()


async def _register(B, agent, intel):
    B.start_backend_services(agent, intel)


def t_app_no_longer_drives_them() -> None:
    print("\n▶ 界面不再驱动后端心跳")
    src = S.module_text("app")
    tree = ast.parse(src)
    cbs = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "timer"
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "ui" and len(n.args) >= 2):
            cbs.append(ast.unparse(n.args[1]))
    gone = ["_budget_health_tick", "_capability_probe_tick", "_install_loop_handler", "_maybe_run_canary",
            "_intel_tick", "_cpu_sample", "_ambient_trail_tick", "_runtime_reconcile_tick", "_mcp_startup"]
    left = [g for g in gone if any(g in c for c in cbs)]
    check(not left, "这些回调不再挂在 ui.timer 上", ", ".join(left))
    check(len(cbs) == 26, "app.py 剩 26 个 ui.timer（原 34，去 9 加 1 个用量警示刷新）", str(len(cbs)))
    check("_nicegui_app.on_startup(lambda: _start_backend_services(gui))" in src,
          "启动时调用 start_backend_services")
    check("def _capability_probe_tick" not in src and "def _budget_health_tick" not in src,
          "两个探针方法已从界面类移出")
    bsrc = S.module_text("core.backend")
    check("nicegui" not in bsrc and "from app" not in bsrc, "core.backend 不依赖界面")


if __name__ == "__main__":
    print("=" * 74)
    print("后端周期任务")
    print("=" * 74)
    t_scheduler()
    t_backend_registration()
    t_app_no_longer_drives_them()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
