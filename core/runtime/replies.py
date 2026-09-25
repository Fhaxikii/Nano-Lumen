# -*- coding: utf-8 -*-
"""确认类事件的回复登记表：事件里只放 `reply_id` 与可选动作名，回调留在后端。

后端发确认类事件（授权弹窗、选择卡等）时，把各个按钮对应的回调登记在这里，
拿到一个 `reply_id` 放进事件；界面收到事件后只需按 `reply_id` + 动作名回复，
不直接持有 Python 可调用对象，事件因此可以序列化、跨进程传输。

- `register` / `pending`：登记一组 `{动作名: 回调}`，返回 `reply_id`。
- `resolve`：按 `reply_id` + 动作名调用对应回调；未知或已撤销的 id 返回 False，不抛错
  （等待结束后才到达的回复属于正常情况）。
- `discard`：等待结束后撤销登记。`pending` 上下文管理器在退出时自动撤销。

回调在调用 `resolve` 的线程里执行，锁外调用；现有回调都用 `call_soon_threadsafe`
唤醒等待方，可以从任意线程回复。
"""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from loguru import logger

_lock = threading.Lock()
_pending: dict[str, dict[str, Callable[..., Any]]] = {}


def register(handlers: Mapping[str, Callable[..., Any]]) -> str:
    """登记一组回调，返回新的 `reply_id`。"""
    if not handlers:
        raise ValueError("handlers must not be empty")
    for name, fn in handlers.items():
        if not callable(fn):
            raise TypeError(f"handler {name!r} is not callable")
    reply_id = uuid.uuid4().hex
    with _lock:
        _pending[reply_id] = dict(handlers)
    return reply_id


def resolve(reply_id: str, action: str, *args: Any) -> bool:
    """调用 `reply_id` 下名为 `action` 的回调。找到并调用返回 True。"""
    with _lock:
        handlers = _pending.get(reply_id)
        fn = handlers.get(action) if handlers else None
    if handlers is None:
        logger.debug(f"[Replies] 回复的 id 已结束或不存在，忽略: {reply_id} / {action}")
        return False
    if fn is None:
        logger.warning(f"[Replies] 未登记的动作 {action!r}（id={reply_id}，"
                       f"可用: {sorted(handlers)}）")
        return False
    fn(*args)
    return True


def discard(reply_id: str) -> None:
    """撤销登记；之后对该 id 的回复一律忽略。"""
    with _lock:
        _pending.pop(reply_id, None)


def actions_of(reply_id: str) -> list[str]:
    """该 id 下登记的动作名（已撤销则为空）。"""
    with _lock:
        return sorted(_pending.get(reply_id, ()))


@contextmanager
def pending(handlers: Mapping[str, Callable[..., Any]]) -> Iterator[str]:
    """`register` + 退出时 `discard`。"""
    reply_id = register(handlers)
    try:
        yield reply_id
    finally:
        discard(reply_id)


def reply_callback(event: Mapping[str, Any], action: str) -> Callable[..., Any] | None:
    """界面侧：把事件里的 `reply_id` + 动作名包成一个可调用对象。

    事件没有 `reply_id`，或该动作不在事件声明的 `actions` 里时返回 None。
    """
    reply_id = event.get("reply_id")
    if not reply_id or action not in (event.get("actions") or ()):
        return None
    return lambda *args: resolve(reply_id, action, *args)
