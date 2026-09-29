# -*- coding: utf-8 -*-
"""MCP 服务管理（设置里的 MCP 连接页）：状态列表、重连、移除、粘 JSON 添加、启用开关。

用户在界面上改了 MCP，对模型来说是「环境变了」（和 Nano 自己调 `manage_mcp` 不是一回事），
所以每次改动都经 `_note_mcp_change(by="user")` 写成对话里的隐藏系统记录，让模型知道发生过。
"""
from __future__ import annotations

from loguru import logger

from core.ui_api import _state


def _mgr():
    from core.mcp_client import get_mcp_manager
    return get_mcp_manager()


def _note(op: str, name: str) -> None:
    try:
        _state.require_agent()._note_mcp_change(op, name, by="user")
    except Exception as e:
        logger.warning(f"[B3] 用户的 MCP 变更未能写进对话记录: {e}")


def servers() -> list[dict]:
    """用户管理的 server 的状态（Skill 自带的内部零件不列）。"""
    return [dict(x) for x in _mgr().status_snapshot() if not x.get("owned_by")]


async def retry(name: str) -> None:
    await _mgr().retry_server(name)
    _note("retry", name)


async def remove(name: str) -> bool:
    ok = bool(await _mgr().remove_server(name))
    if ok:
        _note("delete", name)
    return ok


def add_from_json(text: str) -> dict:
    """粘贴的 JSON 配置 → 写进配置。返回 `{"ok", "msg"}`（成功时 `msg` 是 server 名）。"""
    ok, msg = _mgr().add_server_from_json(text)
    if ok:
        _note("add", msg)
    return {"ok": bool(ok), "msg": msg}


async def apply_switches(desired: dict) -> int:
    """按开关的期望值启用 / 停用，然后连接所有已启用的。返回实际改了几个。"""
    mgr = _mgr()
    changed = 0
    for name, on in (desired or {}).items():
        s = mgr.servers.get(name)
        if s is not None and bool(s.enabled) != bool(on):
            await mgr.set_enabled(name, bool(on))
            _note("enable" if on else "disable", name)
            changed += 1
    await mgr.connect_enabled()
    return changed


async def shutdown() -> None:
    await _mgr().shutdown()
