# -*- coding: utf-8 -*-
"""启动与收尾：日志、崩溃留痕钩子、核心模块加载、启动恢复、创建后端对象、后台服务、
窗口登记、退出。

宿主进程（app.py 的 `__main__`）按下面的顺序调用：
  configure_logging → install_crash_hooks → load_modules → startup_recovery → prepare
  → create(presenter, ...) → （事件循环起来后）start_services / register_window_processes
顺序不能调：启动恢复必须在核心模块加载之后（模块导入时会在运行时内核上登记处理器）。

本模块顶层不导入任何重模块：原生窗口的子进程也会 import app.py，重模块只在主进程里调用这些
函数时才加载。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from loguru import logger

from core.ui_api import _state


def configure_logging() -> None:
    """控制台日志：普通模式只到 INFO；开发者开关 `console_debug` 打开时到 DEBUG。"""
    import sys
    from core import dev_flags
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if dev_flags.enabled("console_debug") else "INFO")


def install_crash_hooks() -> None:
    from core import crash_journal
    crash_journal.install_hooks()


def load_modules() -> None:
    """加载会产生副作用的核心模块（只在主进程）。"""
    import core.orchestrator  # noqa: F401
    import core.provider  # noqa: F401
    import core.registry  # noqa: F401
    import memory.manager  # noqa: F401


def startup_recovery() -> None:
    """运行时的启动恢复：把上个进程留下的脏状态收拾好，被中断的事交给启动呈现，记下这次运行。"""
    from core import startup
    from core.runtime import get_kernel, identity, reconcile_on_startup
    rep = reconcile_on_startup(get_kernel())
    startup.set_interrupted(getattr(rep, "interrupted_details", []) or [])
    if rep.did_anything or any((rep.extra or {}).values()):
        logger.debug(f"[Runtime] 启动恢复：{rep.summary()} extra={rep.extra}")
    identity.record_run(get_kernel(), rep.summary())
    identity.arm_restart_notice(get_kernel())


def prepare() -> None:
    """知识库目录就位、加载 Skill。"""
    from core.ui_api import knowledge, skills
    knowledge.ensure_dir()
    skills.reload_all()


def create(presenter: Any, *, native_window: Optional[Callable[[], Any]] = None) -> None:
    """创建后端对象（记忆、provider、orchestrator）并接好：会话调度器以 `presenter` 为呈现方；
    后台载体的状态变化与上下文衰减以轮外事件告诉界面（`carrier_changed` / `decay_applied`）。

    `native_window`：取原生窗口的函数（截图前要把 Nano 自己的窗口最小化再还原）；浏览器模式为 None。
    """
    from core.orchestrator import Orchestrator
    from core.provider import get_provider
    from core.registry import registry
    from core.runtime import carriers, events, get_kernel
    from core.runtime.conversation import ConversationRepository
    from core.session import get_scheduler
    from memory.manager import MemoryManager

    memory = MemoryManager(max_turns=10,
                           conversation_repository=ConversationRepository(get_kernel().store))
    # 全进程唯一的 provider：改完 key 后 reconfigure 重建的是它，知识库的多模态也用它
    provider = get_provider()
    agent = Orchestrator(provider, registry, memory)
    _state.bind(agent_obj=agent, provider_obj=provider, memory_obj=memory)

    def _carrier_changed(ev: dict) -> None:
        # change：carrier_started / carrier_finished / carrier_promoted
        events.publish({"event": "carrier_changed", "change": ev.get("event") or "",
                        "skill_name": ev.get("skill_name") or "",
                        "rt_task_id": ev.get("rt_task_id") or ""}, None)
    carriers.add_listener(_carrier_changed)
    sched = get_scheduler()
    sched.attach(agent, presenter)
    carriers.set_completion_handler(sched.notify_background_done)
    agent._native_window = native_window


def start_services() -> None:
    """事件循环起来之后：后端心跳与启动时的一次性任务（`core.backend`）。"""
    from core.backend import start_backend_services
    start_backend_services(_state.require_agent())


def register_window_processes(pids: list[int]) -> None:
    """原生窗口所在的进程登记为 Nano 的界面（`core.self_identity`，用来认出 Nano 自己的窗口）。"""
    from core import self_identity
    for pid in pids:
        self_identity.register_window_process(pid)


async def shutdown() -> None:
    from core.ui_api import mcp
    await mcp.shutdown()
