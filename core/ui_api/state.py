# -*- coding: utf-8 -*-
"""界面常驻显示的状态快照（`core.snapshots`）：取当前全量、请后端立即重算一份。

快照内容变了由后端以轮外事件 `state_snapshot` 推来；这里给「刚连上时先取一份」和
「界面的操作刚改了状态、想立刻看到」两种情况用。
"""
from __future__ import annotations

from typing import Optional

# 待审卡里交互的种类（快照 `pinned` 的 items[*].kind；目前只放 Skill 代码审计这一种）
AUDIT_KIND = "skill_audit"


def current(name: Optional[str] = None) -> dict:
    """当前快照：给了 `name` 返回那一份，否则 `{名字: 数据}` 全部（没算过的先算一次）。"""
    from core import snapshots
    return snapshots.current(name)


def refresh(name: str) -> bool:
    """立即重算这一份；内容变了就推送。返回是否推送了。"""
    from core import snapshots
    return snapshots.refresh(name)
