# -*- coding: utf-8 -*-
"""后端 → 界面的事件总线：所有给界面的事件走这一条（S6-6b 业务状态下沉第 5 步）。

每个事件带一个 `turn_id`：
  · 某一轮的事件（轮内事件流，由调度器 `core.session` 泵出）—— 该轮的 id；
  · 轮外事件（Subagent 跨过它那一轮之后的授权请求等）—— None。
界面订阅一次（`subscribe`），按 `turn_id` 分发；终止按钮按轮 id 丢弃后续事件。

每一轮结束时调度器发一条 `{"event": "turn_end"}`（后端生成器抛异常时带 `error`），
界面据此知道这一轮的事件流到头了。

事件在进入总线时检查可序列化（`core.runtime.wire`）：界面将来在另一个进程，
事件里只能是数据。NiceGUI 期传输是进程内队列；传输层 6c 换（S6-Q1）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from loguru import logger

TURN_END = "turn_end"

_subscribers: list[asyncio.Queue] = []


def subscribe() -> asyncio.Queue:
    """订阅全部事件；队列里是 `(turn_id, event)`。"""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


def publish(event: dict, turn_id: Optional[str] = None) -> None:
    """发一个事件。没有订阅者时丢弃并记一条日志（界面还没起来 / 已关闭）。"""
    try:
        from core.runtime.wire import warn_if_not_serializable
        warn_if_not_serializable(event or {})
    except Exception:
        pass
    if not _subscribers:
        logger.debug(f"[Events] 没有订阅者，丢弃事件 {(event or {}).get('event', '?')}（turn={turn_id}）")
        return
    for q in list(_subscribers):
        q.put_nowait((turn_id, event))


class _OutOfTurn:
    """轮外出口：和轮内 `event_queue` 同一个 `put` 接口，事件发到总线、不带轮 id。"""

    async def put(self, event: dict) -> None:
        publish(event, None)

    def put_nowait(self, event: dict) -> None:
        publish(event, None)


OUT_OF_TURN = _OutOfTurn()


async def pump_turn(turn_id: str, source: Any) -> None:
    """把一轮的后端事件流完整迭代（一个 task 里：后端生成器用 ContextVar，不能拆开推进），
    逐个发到总线，最后发 `turn_end`。"""
    err = None
    try:
        async for ev in source:
            publish(ev, turn_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.error(f"[Events] 第 {turn_id} 轮的事件流异常: {err}")
    finally:
        end = {"event": TURN_END}
        if err:
            end["error"] = err
        publish(end, turn_id)


def _reset_for_tests() -> None:
    _subscribers.clear()
