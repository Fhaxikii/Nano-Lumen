# core/proactive/intel/affect.py
"""
快钟 Mood + 中钟 Patience 的状态管理与更新。
持久化复用 state.py 的原子写。进程级单例。

铁律：本模块只产出 ①patience（主动闸门用）②tone_hint（注入提示用）。
绝不被工具/OS 执行准入读取。
"""
from __future__ import annotations

import pathlib
import threading
from typing import Optional

from loguru import logger

from core.proactive import state as _st
from core.proactive.intel.types import AffectState

_PATH = pathlib.Path("data/proactive_affect.json")

# 各类反应对情感的冲击幅度 [原型期标定]
SHOCK_DONT_BOTHER = -0.45      # "别烦我" 对 recent_shock 的瞬时压低
VALENCE_POSITIVE = +0.12       # 被采纳 / 致谢
VALENCE_NEG_INTRUSIVE = -0.15  # 被嫌打扰
VALENCE_NEG_WRONG = -0.05      # 猜错（小，且主要走 accuracy 不走这里）
LEARNED_DELTA_STEP = -0.03     # "反复别烦我" 每次慢降 baseline 的步长（负）
LEARNED_DELTA_RECOVER = +0.02  # 正向回升步长


class _AffectStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = AffectState.from_dict(_st._read_json(_PATH))

    def _save(self):
        _st._write_json(_PATH, self._state.to_dict())

    # ── 读 ──
    def patience(self) -> float:
        with self._lock:
            return self._state.patience_current()

    def tone_hint(self) -> str:
        with self._lock:
            return self._state.tone_hint()

    def snapshot(self) -> dict:
        with self._lock:
            self._state.decay()
            return self._state.to_dict()

    def enabled(self) -> bool:
        with self._lock:
            return self._state.proactive_enabled

    # ── 写（由 feedback 路由调用）──
    def apply_shock(self, magnitude: float = SHOCK_DONT_BOTHER):
        with self._lock:
            self._state.decay()
            self._state.recent_shock = min(self._state.recent_shock, magnitude)
            self._state.clamp(); self._save()

    def nudge_valence(self, delta: float):
        with self._lock:
            self._state.decay()
            self._state.valence += delta
            self._state.clamp(); self._save()

    def nudge_learned_delta(self, delta: float):
        with self._lock:
            self._state.decay()
            self._state.learned_delta += delta
            self._state.clamp(); self._save()

    def set_user_mode(self, mode: str):
        if mode not in ("quiet", "balanced", "active"):
            return
        with self._lock:
            self._state.user_mode = mode; self._save()

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._state.proactive_enabled = enabled; self._save()
            logger.info(f"[Affect] proactive_enabled = {enabled}")


_store: Optional[_AffectStore] = None
_store_lock = threading.Lock()


def get_affect() -> _AffectStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = _AffectStore()
    return _store
