# -*- coding: utf-8 -*-
"""界面接口背后的后端对象（由 `boot` 创建与登记）。"""
from __future__ import annotations

from typing import Any, Optional

agent: Optional[Any] = None       # core.orchestrator.Orchestrator
provider: Optional[Any] = None    # core.provider 的当前 provider
memory: Optional[Any] = None      # memory.manager.MemoryManager


def bind(*, agent_obj: Any = None, provider_obj: Any = None, memory_obj: Any = None) -> None:
    global agent, provider, memory
    if agent_obj is not None:
        agent = agent_obj
    if provider_obj is not None:
        provider = provider_obj
    if memory_obj is not None:
        memory = memory_obj


def require_agent() -> Any:
    if agent is None:
        raise RuntimeError("后端还没启动（ui_api.boot 未执行）")
    return agent
