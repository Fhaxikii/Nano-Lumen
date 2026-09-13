# core/proactive/intel/ledger.py
"""
慢钟 Welcome Ledger：按"类型×场景×渠道"记长期偏好。
- welcome_score / explicit_mute / frequency / accuracy / capability 五条独立路。
- 分层惩罚冒泡：父级需多子节点一致才更新；按语义决定冒泡深度（不跨语义冒泡）。
- 产品默认先验起步（n=1 稀疏），只有反复反馈的 key 才偏离。
"""
from __future__ import annotations

import datetime
import pathlib
import threading
import time
from typing import Optional

from core.proactive import state as _st
from core.proactive.intel.types import (
    Channel, InterventionType, LedgerEntry, LedgerKey, Scene,
    _FREQ_FLOOR_BY_CHANNEL,
)

_PATH = pathlib.Path("data/proactive_ledger.json")

# ── 产品默认先验 [原型期标定] ──────────────────────────────────
# 生产力类 welcome 默认中高；风险提示更高；不存在情绪类种子（情绪不单独触发）。
_WELCOME_PRIOR = {
    InterventionType.PREPARE: 0.55,
    InterventionType.ORGANIZE: 0.55,
    InterventionType.RECOVER: 0.6,
    InterventionType.RISK_ALERT: 0.7,
    InterventionType.NEXT_STEP: 0.5,
}
_DAILY_CAP_PRIOR = {Channel.POPUP: 1, Channel.CORNER: 3, Channel.PANEL: 6, Channel.SILENT: 99}

# EMA 衰减系数（每次更新对历史的保留）[原型期标定]
_EMA_KEEP = 0.8
# welcome 慢回归中性的速率（每天）[原型期标定]
_WELCOME_REGRESS_PER_DAY = 0.02
# 冒泡：父级 type 需要 ≥2 个不同 scene 或 channel 的同向负证据才更新 [原型期标定]
_BUBBLE_MIN_DISTINCT = 2
_BUBBLE_PARENT_FACTOR = 0.2   # type+channel 父级吃 20%
_BUBBLE_GRAND_FACTOR = 0.05   # type 父级吃 5%


def _today() -> str:
    return str(datetime.date.today())


class _Ledger:
    def __init__(self):
        self._lock = threading.Lock()
        raw = _st._read_json(_PATH) or {}
        self._entries: dict[str, LedgerEntry] = {}
        for k, v in (raw.get("entries", {}) or {}).items():
            try:
                self._entries[k] = LedgerEntry(**{kk: vv for kk, vv in v.items()
                                                  if kk in LedgerEntry.__dataclass_fields__})
            except Exception:
                pass
        # 负证据子节点记录（用于多子一致冒泡判断）：{type_value: {child_str: ts}}
        self._neg_children: dict[str, dict] = raw.get("neg_children", {}) or {}

    def _save(self):
        data = {
            "entries": {k: vars(e) for k, e in self._entries.items()},
            "neg_children": self._neg_children,
        }
        _st._write_json(_PATH, data)

    def _default_entry(self, key: LedgerKey) -> LedgerEntry:
        return LedgerEntry(
            welcome_score=_WELCOME_PRIOR.get(key.type, 0.5),
            min_interval=_FREQ_FLOOR_BY_CHANNEL.get(key.channel, 0),
            daily_cap=_DAILY_CAP_PRIOR.get(key.channel, 3),
        )

    def get(self, key: LedgerKey) -> LedgerEntry:
        with self._lock:
            ks = key.as_str()
            e = self._entries.get(ks)
            if e is None:
                e = self._default_entry(key)
                self._entries[ks] = e
            self._regress(e)
            return e

    def _regress(self, e: LedgerEntry):
        """welcome 慢回归中性 0.5（按上次展示时间粗略折算；explicit_mute 不回归）。"""
        if e.explicit_mute:
            return
        if e.last_shown_at <= 0:
            return
        days = max(0.0, (time.time() - e.last_shown_at) / 86400.0)
        if days <= 0:
            return
        pull = min(_WELCOME_REGRESS_PER_DAY * days, abs(e.welcome_score - 0.5))
        e.welcome_score += pull if e.welcome_score < 0.5 else -pull

    # ── 展示登记（影响 frequency 余量）──
    def record_shown(self, key: LedgerKey, channel: Channel):
        with self._lock:
            e = self._entries.setdefault(key.as_str(), self._default_entry(key))
            e.last_shown_at = time.time()
            if e.today_date != _today():
                e.today_date = _today(); e.today_count = 0
            e.today_count += 1
            self._save()

    # ── 五条独立更新路（由 feedback 路由调用）──
    def update_welcome(self, key: LedgerKey, positive: bool):
        with self._lock:
            e = self._entries.setdefault(key.as_str(), self._default_entry(key))
            if positive:
                e.accept_ema = _EMA_KEEP * e.accept_ema + (1 - _EMA_KEEP)
                e.welcome_score = min(1.0, e.welcome_score + 0.08)
            else:
                e.annoy_ema = _EMA_KEEP * e.annoy_ema + (1 - _EMA_KEEP)
                e.welcome_score = max(0.0, e.welcome_score - 0.08)
                self._bubble_negative(key)
            self._save()

    def set_explicit_mute(self, key: LedgerKey, muted: bool = True):
        with self._lock:
            e = self._entries.setdefault(key.as_str(), self._default_entry(key))
            e.explicit_mute = muted
            self._save()

    def tighten_frequency(self, key: LedgerKey):
        """"对的但别老弹"：welcome 不动，收紧频率（地板内、可回升）。"""
        with self._lock:
            e = self._entries.setdefault(key.as_str(), self._default_entry(key))
            floor = _FREQ_FLOOR_BY_CHANNEL.get(key.channel, 0)
            e.min_interval = max(e.min_interval, floor) * 1.5 + 600
            e.daily_cap = max(1, e.daily_cap - 1)
            self._save()

    def update_accuracy(self, key: LedgerKey, correct: bool):
        """"这个不准"：只调 accuracy_prior，不动 welcome。"""
        with self._lock:
            e = self._entries.setdefault(key.as_str(), self._default_entry(key))
            e.accuracy_prior = max(0.0, min(1.0,
                e.accuracy_prior + (0.06 if correct else -0.08)))
            self._save()

    def bump_capability(self, key: LedgerKey):
        """被动使用某能力：抬 capability，不抬 proactive welcome。"""
        with self._lock:
            e = self._entries.setdefault(key.as_str(), self._default_entry(key))
            e.capability_confidence = min(1.0, e.capability_confidence + 0.1)
            self._save()

    def _bubble_negative(self, key: LedgerKey):
        """多子一致才冒泡：同 type 下 ≥2 个不同 (scene 或 channel) 有负证据，
        才轻减父级（type+channel 吃 20%，type 吃 5%）。仅 welcome 语义冒泡。"""
        t = key.type.value
        children = self._neg_children.setdefault(t, {})
        children[f"{key.scene.value}|{key.channel.value}"] = time.time()
        distinct_scene = {c.split("|")[0] for c in children}
        distinct_chan = {c.split("|")[1] for c in children}
        if len(distinct_scene) >= _BUBBLE_MIN_DISTINCT or len(distinct_chan) >= _BUBBLE_MIN_DISTINCT:
            # 冒泡到 type+channel 与 type
            pk1 = LedgerKey(key.type, Scene.UNKNOWN, key.channel).as_str()
            pk2 = LedgerKey(key.type, Scene.UNKNOWN, Channel.SILENT).as_str()
            for ks, factor in ((pk1, _BUBBLE_PARENT_FACTOR), (pk2, _BUBBLE_GRAND_FACTOR)):
                pe = self._entries.setdefault(ks, LedgerEntry(welcome_score=_WELCOME_PRIOR.get(key.type, 0.5)))
                pe.welcome_score = max(0.0, pe.welcome_score - 0.08 * factor)

    def explain(self) -> dict:
        """供可解释面板：哪些类被降权 / 被静音。"""
        with self._lock:
            low = {k: round(e.welcome_score, 2) for k, e in self._entries.items()
                   if e.welcome_score < 0.4 or e.explicit_mute}
            return low

    def explain_readable(self, cap: int = 2) -> dict:
        """供下拉面板：返回 {'items': [人话标签...], 'total': N}（封顶 cap 条 + 计数）。"""
        low = self.explain()
        _type_cn = {"prepare": "预备", "organize": "整理", "recover": "恢复",
                    "risk_alert": "风险提示", "next_step": "下一步"}
        _scene_cn = {"writing": "写作", "coding": "写代码", "browsing": "浏览",
                     "meeting": "会议", "media": "看片/听歌", "file_management": "文件",
                     "idle_recovery": "回到电脑", "unknown": ""}
        items = []
        for k in low:
            parts = k.split("|")
            t = _type_cn.get(parts[0], parts[0]) if parts else k
            sc = _scene_cn.get(parts[1], "") if len(parts) > 1 else ""
            items.append(f"{sc}{('·' if sc else '')}{t}".strip("·"))
        # 去重保序
        seen, uniq = set(), []
        for it in items:
            if it not in seen:
                seen.add(it); uniq.append(it)
        return {"items": uniq[:cap], "total": len(uniq)}

    def reset(self):
        """全部恢复默认：清掉学到的好恶/静音/频率收紧（回归产品先验）。"""
        with self._lock:
            self._entries.clear()
            self._neg_children.clear()
            self._save()


_ledger: Optional[_Ledger] = None
_ll = threading.Lock()


def get_ledger() -> _Ledger:
    global _ledger
    if _ledger is None:
        with _ll:
            if _ledger is None:
                _ledger = _Ledger()
    return _ledger
