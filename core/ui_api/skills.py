# -*- coding: utf-8 -*-
"""Skill：侧边栏列表、详情、源码、启用 / 禁用 / 删除，待审草稿的查看、校验、部署与丢弃。

界面上直接操作 Skill 绕过了对话，所以每次操作成功都往对话里记一条系统记录，并写明
来源是界面（不是 Nano 在对话里做的），否则模型被问起时会以为是自己做的、或者根本不知道。
"""
from __future__ import annotations

from typing import Optional

from core.ui_api import _state


def _registry():
    from core.registry import registry
    return registry


def _note(text: str) -> None:
    try:
        _state.require_agent().memory.add_system_note("assistant", text)
    except Exception:
        pass


# ── 列表与详情 ────────────────────────────────────────────────────────────
def list_skills() -> dict:
    """`{"enabled": [{"name", "official", "os"}], "disabled": [name, ...]}`（启用的按注册顺序）。"""
    reg = _registry()
    enabled = [{"name": n, "official": bool(reg.is_official_skill(n)), "os": bool(reg.is_os_skill(n))}
               for n in list(reg.skills)]
    try:
        disabled = list(reg.list_disabled_skills())
    except Exception:
        disabled = []
    return {"enabled": enabled, "disabled": disabled}


def skill_info(name: str) -> Optional[dict]:
    """`{"description", "purpose", "not_responsible_for"}`；没有这个 Skill 返回 None。"""
    obj = _registry().skills.get(name)
    if obj is None:
        return None
    try:
        manifest = obj.get_manifest() or {}
    except Exception:
        manifest = {}
    purpose, not_resp = "", []
    try:
        spec = obj.get_spec()
        purpose = getattr(spec, "purpose", "") or ""
        not_resp = [str(x) for x in (getattr(spec, "not_responsible_for", None) or [])]
    except Exception:
        pass
    return {"description": manifest.get("description", "") or "", "purpose": purpose,
            "not_responsible_for": not_resp}


def skill_source(name: str) -> Optional[str]:
    """源码（含已禁用的）；没有返回 None。"""
    src = _registry().get_skill_source(name, include_disabled=True)
    return (src or {}).get("code") if src else None


# ── 启用 / 禁用 / 删除 ────────────────────────────────────────────────────
def disable(name: str) -> dict:
    """移到 skills/disabled/。返回 `{"ok", "msg"}`。"""
    r = _registry().disable_skill(name) or {}
    if r.get("ok"):
        _note(f'[System record: the user disabled Skill "{name}" from the UI sidebar; this was '
              f'not executed in the current chat.] {r.get("msg") or f"Skill {name!r} has been disabled"}')
    return {"ok": bool(r.get("ok")), "msg": r.get("msg") or ""}


def enable(name: str) -> dict:
    r = _registry().enable_skill(name) or {}
    if r.get("ok"):
        _note(f'[System record: the user enabled Skill "{name}" from the UI sidebar; this was '
              f'not executed in the current chat.] {r.get("msg") or f"Skill {name!r} has been enabled"}')
    return {"ok": bool(r.get("ok")), "msg": r.get("msg") or ""}


def delete(name: str) -> dict:
    """移到 skills/deleted/ 备份目录，并从当前工具链移除。"""
    r = _registry().delete_skill_file(name) or {}
    if r.get("ok"):
        _note(f'[System record: the user deleted Skill "{name}" from the UI sidebar; this was '
              f'not executed in the current chat.] {r.get("msg") or f"Skill {name!r} has been deleted"}')
    return {"ok": bool(r.get("ok")), "msg": r.get("msg") or ""}


# ── 待审草稿 ──────────────────────────────────────────────────────────────
def pending_draft(filename: Optional[str] = None) -> Optional[dict]:
    """一份待审草稿（不给名字取最近那份）：`{"filename", "code", "description", "valid", "errors"}`。"""
    p = _state.require_agent()._get_pending_skill(filename)
    if not p:
        return None
    return {"filename": p.get("filename") or filename or "", "code": p.get("code", "") or "",
            "description": p.get("description", "") or "", "valid": bool(p.get("valid", True)),
            "errors": [str(e) for e in (p.get("errors") or [])]}


def update_draft_code(filename: Optional[str], code: str) -> bool:
    """把用户在编辑器里改过的代码写回**这一份**草稿（多份并存时写错人，改动就落到别的草稿上）。"""
    p = _state.require_agent()._get_pending_skill(filename)
    if p is None:
        return False
    p["code"] = code
    return True


def validate_code(code: str) -> dict:
    """`{"ok", "errors"}`（Skill 协议校验）。"""
    from core.skill_check import validate_skill_code
    ok, errors = validate_skill_code(code)
    return {"ok": bool(ok), "errors": [str(e) for e in (errors or [])]}


def apply_draft(filename: Optional[str], code: str) -> dict:
    """部署这一份草稿（先写回编辑器里的代码）。返回 `{"ok", "msg"}`；成功时 `msg` 第一段是给用户看的结论。"""
    update_draft_code(filename, code)
    from core import skill_watch
    skill_watch.suppress(2.5)          # 部署写文件，别让目录监听再重载一次
    r = _state.require_agent().apply_pending_skill(filename) or {}
    if r.get("ok"):
        _note("[System record: a pending Skill was deployed from the UI.] "
              + str(r.get("msg") or "").split("\n\n")[0])
    return {"ok": bool(r.get("ok")), "msg": r.get("msg") or ""}


def discard_draft(filename: Optional[str]) -> dict:
    """丢弃这一份草稿（不部署）。"""
    r = _state.require_agent().cancel_pending_skill(filename) or {}
    _note("[System record: the user discarded the pending Skill in the UI; it was not deployed.] "
          + str(r.get("msg") or ""))
    return {"ok": bool(r.get("ok", True)), "msg": r.get("msg") or ""}


def reload_all() -> None:
    _registry().reload_all()
