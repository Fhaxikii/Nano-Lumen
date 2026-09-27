# -*- coding: utf-8 -*-
"""用户笔记（`write_user_note` 写的记忆）：待确认的保留 / 删除，已记住的查看与删除。"""
from __future__ import annotations


def _store():
    from core.memory_store import get_memory_store
    return get_memory_store()


def pending() -> list[dict]:
    """待确认的笔记（`id`、`ts`、`subject`、`action`、`detail`、`tags` …）。"""
    return [dict(r) for r in _store().get_pending_notes()]


def confirmed() -> list[dict]:
    """全部已记住的笔记（抽屉「全部记忆」）。"""
    return [dict(r) for r in _store().get_all_confirmed_notes()]


def confirm(note_id: int) -> bool:
    return bool(_store().confirm(note_id))


def confirm_all() -> None:
    _store().confirm_all_pending()


def delete_pending(note_id: int) -> None:
    """删除一条待确认的笔记。"""
    _store().delete_by_id(note_id)


def forget(note_id: int) -> None:
    """删除一条已记住的笔记（软删除）。"""
    _store().soft_delete_by_id(note_id)
