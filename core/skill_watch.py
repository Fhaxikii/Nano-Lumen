# -*- coding: utf-8 -*-
"""Skill 目录热重载：监听 `skills/` 下 .py 的增删改移，合并成一次 `registry.reload_all()`。

- 监听线程（watchdog）只做标记（`request_reload`），真正的重载由后端心跳在事件循环里做
  （`reload_tick`，0.5 秒一次），做完发一个轮外事件 `skills_reloaded`，界面据此刷新 Skill 列表。
- SkillWriter / 审计部署自己写文件时先 `suppress(秒)`，避免同一次写入再触发一次重载。
- 管理操作（删除 / 禁用 / 启用）完成后直接 `request_reload()`，不等监听线程。
"""
from __future__ import annotations

import pathlib
import threading
import time
from typing import Optional

from loguru import logger

_lock = threading.Lock()
_requested = False
_suppress_until = 0.0
_observer = None


def request_reload(why: str = "") -> None:
    """标记需要重载（线程安全；多次标记合并成一次）。"""
    global _requested
    with _lock:
        _requested = True
    if why:
        logger.info(f"✨ {why}，标记等待重载 Skill")


def suppress(seconds: float) -> None:
    """接下来 `seconds` 秒内的文件变更不触发重载（自己写文件的一方调用）。"""
    global _suppress_until
    _suppress_until = time.time() + float(seconds)


def suppressed() -> bool:
    return time.time() < _suppress_until


def reload_tick() -> bool:
    """有标记就重载一次并通知界面。返回是否重载了。"""
    global _requested
    with _lock:
        if not _requested:
            return False
        _requested = False
    try:
        from core.registry import registry
        registry.reload_all()
        logger.info("🔄 已重载 Skill")
    except Exception as e:
        logger.error(f"[Skill] 重载失败: {e}")
        return False
    try:
        from core.runtime import events
        events.publish({"event": "skills_reloaded"}, None)
    except Exception:
        pass
    return True


def _make_handler():
    from watchdog.events import FileSystemEventHandler

    class _SkillWatcher(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.src_path.endswith(".py"):
                return
            if suppressed():
                logger.info(f"✨ 技能代码变更由 SkillWriter 触发，已抑制二次热重载: {event.src_path}")
                return
            request_reload(f"技能代码变更: {event.src_path}")

        def on_deleted(self, event):
            # 删除 / 禁用把 .py 移出 skills/，on_modified 收不到。
            if event.is_directory or not str(event.src_path).endswith(".py"):
                return
            request_reload(f"技能文件删除/移出: {event.src_path}")

        def on_moved(self, event):
            _src = str(getattr(event, "src_path", ""))
            _dst = str(getattr(event, "dest_path", ""))
            if event.is_directory or not (_src.endswith(".py") or _dst.endswith(".py")):
                return
            request_reload(f"技能文件移动: {_src} → {_dst}")

        def on_created(self, event):
            # 启用（从 skills/disabled 移回 skills/）时源在监听树外，watchdog 报 on_created。
            if event.is_directory or not str(event.src_path).endswith(".py"):
                return
            if suppressed():
                return
            request_reload(f"技能文件新增/启用: {event.src_path}")

    return _SkillWatcher()


def start_watching(path: Optional[pathlib.Path] = None) -> None:
    """开始监听 skills/（重复调用无副作用）。"""
    global _observer
    if _observer is not None:
        return
    from watchdog.observers import Observer
    if path is None:
        from core.paths import ROOT
        path = ROOT / "skills"
    obs = Observer()
    obs.schedule(_make_handler(), path=str(path), recursive=False)
    obs.daemon = True
    obs.start()
    _observer = obs


def _reset_for_tests() -> None:
    global _requested, _suppress_until
    _requested = False
    _suppress_until = 0.0
