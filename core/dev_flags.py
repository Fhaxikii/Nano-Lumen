# core/dev_flags.py
"""开发者开关：统一放在 ``config/dev_flags.json``，随仓库分发。

每个开关的格式::

    "console_debug": {"enabled": false, "description": "..."}

规则：
- 仓库中的版本所有开关必须为 ``"enabled": false``（由测试断言）。开发者只在本地打开。
- 每个进程只在第一次访问时读取一次；修改后重启生效。
- 只有 ``enabled`` 为 JSON 字面量 ``true`` 时开关才算开启。文件不存在、读取失败、
  格式错误、缺少该开关，或 ``enabled`` 为其他任何值（``"true"``、``1`` 等），一律视为关闭。
- 每个处于开启状态的开关都会在首次读取时以 WARNING 级别记录一次，并注明来源文件。

以 ``_`` 开头的键是说明，不是开关。
"""
from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

from loguru import logger

from core.paths import ROOT as _ROOT  # noqa: E402

DEV_FLAGS_PATH = _ROOT / "config" / "dev_flags.json"

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def load_file(path: pathlib.Path | None = None) -> dict[str, Any]:
    """读取并返回开关表（不含以 ``_`` 开头的说明键）。失败时返回空表。"""
    path = path or DEV_FLAGS_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[DevFlags] 无法读取 {path}，全部开关按关闭处理: {e}")
        return {}
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning(f"[DevFlags] {path} 不是合法的 JSON，全部开关按关闭处理: {e}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"[DevFlags] {path} 不是 JSON 对象，全部开关按关闭处理")
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _is_on(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("enabled") is True


def _flags() -> dict[str, Any]:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                data = load_file()
                for name, entry in data.items():
                    if _is_on(entry):
                        logger.warning(f"[DevFlags] 开发者开关 '{name}' 已开启（来源：{DEV_FLAGS_PATH}）")
                _cache = data
    return _cache


def enabled(name: str) -> bool:
    """开关的 ``enabled`` 严格等于 JSON ``true`` 时返回 True，其余情况一律返回 False。"""
    return _is_on(_flags().get(name))


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
