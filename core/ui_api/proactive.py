# -*- coding: utf-8 -*-
"""主动智能：用户选的主动程度、学到的偏好（可解释、可恢复默认）、对一次主动开口的反馈，
以及感知钩子（键鼠 / 窗口 / 保存）的启动。"""
from __future__ import annotations

# 界面上一次主动开口下面那几个反馈按钮对应的信号名（`core.proactive.intel.feedback.Signal`）
FEEDBACK_SIGNALS = ("ACCEPTED", "WRONG", "MUTE_THIS", "ANNOYED_INTRUSIVE")


def effort_mode() -> str:
    """`quiet` / `balanced` / `proactive` 这类用户模式（取不到为 `balanced`）。"""
    from core.proactive.intel.affect import get_affect
    return str(get_affect().snapshot().get("user_mode", "balanced"))


def set_effort_mode(mode: str) -> None:
    from core.proactive.intel.affect import get_affect
    get_affect().set_user_mode(mode)


def learned_preferences(cap: int = 2) -> dict:
    """学到的「这类别再提」：`{"items": [人话标签, ...], "total": N}`（最多列 cap 条）。"""
    from core.proactive.intel.ledger import get_ledger
    info = get_ledger().explain_readable(cap=cap) or {}
    return {"items": [str(x) for x in info.get("items") or []], "total": int(info.get("total") or 0)}


def reset_preferences() -> None:
    from core.proactive.intel.ledger import get_ledger
    get_ledger().reset()


def feedback(signal: str, intervention_id: str) -> None:
    """用户对一次主动开口的反馈（`signal` 取 `FEEDBACK_SIGNALS` 里的名字）。"""
    from core.backend import get_intel_engine
    from core.proactive.intel.feedback import Signal
    eng = get_intel_engine()
    if eng is not None:
        eng.feedback(Signal[signal], intervention_id)


def start_hooks() -> None:
    """启动感知钩子（键鼠 / 窗口 / 保存）。"""
    from core.proactive.hooks import start_hooks as _start
    _start()
