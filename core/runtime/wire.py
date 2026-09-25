# -*- coding: utf-8 -*-
"""后端 → 界面事件的可序列化检查。

事件要能跨进程传输，值只能是 JSON 兼容类型：str / int / float / bool / None，
以及由它们组成的 list / tuple / 以字符串为键的 dict。

`non_serializable_paths` 返回不合格值的位置与类型；`warn_if_not_serializable`
在界面消费事件时调用，同一个「事件名 + 位置」每个进程只记一次 WARNING。
"""
from __future__ import annotations

import threading
from typing import Any, Mapping

from loguru import logger

_SCALARS = (str, int, float, bool, type(None))
_warned: set[tuple[str, str]] = set()
_lock = threading.Lock()


def non_serializable_paths(value: Any, path: str = "") -> list[str]:
    """列出 `value` 里不是 JSON 兼容类型的值：`["cards[0].x: Future", ...]`。"""
    if isinstance(value, _SCALARS):
        return []
    if isinstance(value, Mapping):
        out: list[str] = []
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            if not isinstance(k, str):
                out.append(f"{p}: key {type(k).__name__}")
                continue
            out.extend(non_serializable_paths(v, p))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for i, v in enumerate(value):
            out.extend(non_serializable_paths(v, f"{path}[{i}]"))
        return out
    return [f"{path or '<root>'}: {type(value).__name__}"]


def warn_if_not_serializable(event: Mapping[str, Any]) -> list[str]:
    """检查一个事件；新出现的问题记 WARNING。返回全部问题位置。"""
    try:
        bad = non_serializable_paths(event)
    except Exception as e:
        logger.debug(f"[Wire] 检查事件失败: {e}")
        return []
    if bad:
        name = str(event.get("event", "?"))
        with _lock:
            fresh = [b for b in bad if (name, b) not in _warned]
            _warned.update((name, b) for b in fresh)
        if fresh:
            logger.warning(f"[Wire] 事件 {name!r} 含不可序列化的值: {', '.join(fresh)}")
    return bad
