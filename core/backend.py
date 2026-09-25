# -*- coding: utf-8 -*-
"""后端常驻服务的登记与启动：周期心跳与启动时的一次性任务。

由宿主进程在事件循环启动后调用一次 `start_backend_services(...)`。这些任务只依赖后端
（orchestrator、runtime、health、proactive），不依赖任何界面框架。

周期心跳（间隔秒）：
  runtime_reconcile 5 · capability_probe 15 · budget_health 20 · intel_tick 20 ·
  cpu_sample 60 · ambient_trail 240 · canary 300
一次性：asyncio 崩溃处理器（立即）· mcp_startup（1.5 秒后）
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from loguru import logger

from core import heartbeat


def _runtime_reconcile() -> None:
    from core.runtime import get_kernel, reconcile_tick
    rep = reconcile_tick(get_kernel())
    # 只在真做了事的时候记日志，否则 5 秒一条
    if rep.did_anything or rep.extra:
        logger.info(f"[Runtime] tick：{rep.summary()} extra={rep.extra}")


def _capability_probe() -> None:
    """跑到点的能力探针；恢复的能力写进系统事件（由界面告诉用户）。"""
    from core.health import get_capability_spec, get_health, get_system_events
    for cap in get_health().run_due_probes():
        spec = get_capability_spec(cap)
        label = spec.label if spec else cap
        get_system_events().add(f"Capability recovered: {label}.")
        logger.info(f"[Health] 探针恢复：{label}")


def _budget_health() -> None:
    """把预算状态同步进健康登记表（按日重置后，故障卡能在没有模型调用时恢复）。"""
    from core.usage import sync_budget_health
    sync_budget_health()


def _cpu_sample() -> None:
    import psutil
    from core.proactive.activity import get_buffer
    get_buffer().on_cpu_sample(psutil.cpu_percent(interval=None))


async def _mcp_startup() -> None:
    """加载 MCP 配置并连接已启用的 server（各自在 worker task 里异步连接）。"""
    from core.mcp_client import get_mcp_manager
    mgr = get_mcp_manager()
    if mgr.available:
        mgr.load_config()
        await mgr.connect_enabled()


def _install_asyncio_crash_handler() -> None:
    from core import crash_journal
    crash_journal.install_asyncio_handler(asyncio.get_running_loop())


def start_backend_services(agent: Any, intel_engine: Optional[Any] = None) -> None:
    """登记并启动后端心跳。必须在运行中的事件循环里调用；重复调用无副作用。"""
    try:
        _install_asyncio_crash_handler()
    except Exception as e:
        logger.debug(f"[CrashJournal] asyncio handler 未安装: {e}")

    heartbeat.register("runtime_reconcile", 5, _runtime_reconcile)
    heartbeat.register("capability_probe", 15, _capability_probe)
    heartbeat.register("budget_health", 20, _budget_health)
    heartbeat.register("canary", 300, agent.maybe_run_canary)
    if intel_engine is not None:
        # 主动智能引擎（shadow 期 20 秒一跳，多采样决策面；上线后可调回 60 秒）
        heartbeat.register("intel_tick", 20, intel_engine.tick)
    heartbeat.register("cpu_sample", 60, _cpu_sample)
    # 每约 4 分钟把当前现场追加进持久轨迹（跨会话 / 重启留存）
    heartbeat.register("ambient_trail", 240, agent.record_ambient_trail)
    heartbeat.register_once("mcp_startup", 1.5, _mcp_startup)
    heartbeat.start()
