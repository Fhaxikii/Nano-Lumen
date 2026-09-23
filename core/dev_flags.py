# core/dev_flags.py
"""开发者开关：从一个不进仓库的本地文件读取。

文件：``data/dev_flags.json``（已被 ``.gitignore`` 排除）。示例::

    {"console_debug": true}

规则：
- 每个进程只在第一次访问时读取一次；修改后重启生效。
- 只有值为 JSON 字面量 ``true`` 时开关才算开启。文件不存在、读取失败、
  格式错误、缺少该键，或值为其他任何内容（``"true"``、``1`` 等），一律视为关闭。
  发行版不包含此文件，因此对最终用户所有开关都是关闭的。
- 每个处于开启状态的开关都会在首次读取时以 WARNING 级别记录一次，并注明来源文件，
  避免开发者开关在不知情的情况下保持开启。

已定义的开关：
- ``console_debug``：控制台日志级别由 INFO 改为 DEBUG。
"""
from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

from loguru import logger

DEV_FLAGS_PATH = pathlib.Path(__file__).parent.parent / "data" / "dev_flags.json"

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    try:
        raw = DEV_FLAGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[DevFlags] 无法读取 {DEV_FLAGS_PATH}，全部开关按关闭处理: {e}")
        return {}
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning(f"[DevFlags] {DEV_FLAGS_PATH} 不是合法的 JSON，全部开关按关闭处理: {e}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"[DevFlags] {DEV_FLAGS_PATH} 不是 JSON 对象，全部开关按关闭处理")
        return {}
    return data


def _flags() -> dict[str, Any]:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                data = _load()
                for name, value in data.items():
                    if value is True:
                        logger.warning(f"[DevFlags] 开发者开关 '{name}' 已开启（来源：{DEV_FLAGS_PATH}）")
                _cache = data
    return _cache


def enabled(name: str) -> bool:
    """开关的值严格等于 JSON ``true`` 时返回 True，其余情况一律返回 False。"""
    return _flags().get(name) is True


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
