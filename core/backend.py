# -*- coding: utf-8 -*-
"""后端常驻服务的登记与启动：周期心跳与启动时的一次性任务。

由宿主进程在事件循环启动后调用一次 `start_backend_services(...)`。这些任务只依赖后端
（orchestrator、runtime、health、proactive），不依赖任何界面框架。

周期心跳（间隔秒）：
  runtime_reconcile 5 · suspension_poll 5 · capability_probe 15 · budget_health 20 · intel_tick 20 · carrier_heartbeat 30 ·
  cpu_sample 60 · ambient_trail 240 · canary 300
一次性：asyncio 崩溃处理器（立即）· mcp_startup（1.5 秒后）

聊天区的异步产出（不属于任何一轮）也从这里发出：主动开口（`speak`）与故障卡等
（`emit_chat_event`），作为轮外事件发到事件总线（`core.runtime.events`），由界面渲染。
主动智能引擎在这里创建（`get_intel_engine`），「正在回复」由会话调度器的轮次监听设置。
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


_intel_engine: Optional[Any] = None


def get_intel_engine() -> Optional[Any]:
    """主动智能引擎（`start_backend_services` 之后才有）。"""
    return _intel_engine


def emit_chat_event(*, category: str = "speech", body: str = "", title: str = "",
                    lines: list | None = None, hints: list | None = None,
                    intervention_id: Optional[str] = None, dedupe_key: str = "",
                    capabilities: list | None = None) -> None:
    """聊天区的一条异步产出（主动开口 / 故障卡 …）：轮外事件 `chat_message`，由界面渲染。

    界面还没就绪时事件在它的订阅队列里等着（订阅在界面构造时就建好），不丢。
    """
    from core.runtime import events
    events.publish({
        "event": "chat_message", "category": category, "body": body, "title": title,
        "lines": list(lines or []), "hints": list(hints or []),
        "intervention_id": intervention_id, "dedupe_key": dedupe_key,
        # 故障卡涉及的能力。全部恢复（健康登记表发出 RECOVERED）后界面撤掉这张卡。
        "capabilities": list(capabilities or []),
    }, None)


async def speak(content: str, intervention_id: Optional[str] = None) -> None:
    """Nano 主动开口（主动智能引擎、canary 等持有它）。

    不写对话历史（异步写会撕裂 tool_calls / tool_results 事务）；模型侧的知情走
    RecentSystemEvents。
    """
    try:
        from core.health import get_system_events
        get_system_events().add(f"Nano spoke up on its own: {content[:120]}")
    except Exception:
        pass
    emit_chat_event(category="speech", body=content, intervention_id=intervention_id)


def _create_intel_engine(provider: Any) -> Optional[Any]:
    """主动智能 v0：默认 SHADOW（只决策记日志、不真说话）。
    复核 data/proactive_shadow.jsonl 后，把 engine.SHADOW_MODE 改 False 即上线。"""
    global _intel_engine
    if _intel_engine is not None:
        return _intel_engine
    from core.proactive.intel.engine import ProactiveEngine
    from core.session import get_scheduler
    _intel_engine = ProactiveEngine(provider, speak)
    get_scheduler().add_turn_listener(_intel_engine.set_responding)
    return _intel_engine


def start_backend_services(agent: Any, intel_engine: Optional[Any] = None) -> None:
    """登记并启动后端心跳。必须在运行中的事件循环里调用；重复调用无副作用。"""
    agent._push_callback = speak
    if intel_engine is None:
        try:
            intel_engine = _create_intel_engine(agent.provider)
        except Exception as e:
            logger.error(f"[Intel] 主动智能引擎没建起来（不影响其它功能）: {e}")
    try:
        _install_asyncio_crash_handler()
    except Exception as e:
        logger.debug(f"[CrashJournal] asyncio handler 未安装: {e}")

    heartbeat.register("runtime_reconcile", 5, _runtime_reconcile)
    # 到点的定时挂起 → 唤醒轮（只是本地 SQLite 查询；真正的模型调用只在到点时发生）
    from core.session import get_scheduler
    heartbeat.register("suspension_poll", 5, get_scheduler().poll_due)
    heartbeat.register("capability_probe", 15, _capability_probe)
    heartbeat.register("budget_health", 20, _budget_health)
    heartbeat.register("canary", 300, agent.maybe_run_canary)
    # 还在跑的后台载体 → 推后它等待记录的 orphan_at（默认 30 分钟判孤儿）
    from core.runtime import carriers
    heartbeat.register("carrier_heartbeat", 30, carriers.heartbeat)
    if intel_engine is not None:
        # 主动智能引擎（shadow 期 20 秒一跳，多采样决策面；上线后可调回 60 秒）
        heartbeat.register("intel_tick", 20, intel_engine.tick)
    heartbeat.register("cpu_sample", 60, _cpu_sample)
    # 每约 4 分钟把当前现场追加进持久轨迹（跨会话 / 重启留存）
    heartbeat.register("ambient_trail", 240, agent.record_ambient_trail)
    heartbeat.register_once("mcp_startup", 1.5, _mcp_startup)
    heartbeat.start()
