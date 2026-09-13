# core/proactive/intel/scorer.py
"""
打分：每个候选按 channel 分别试算，硬否决任一零闸，
选最高且过线者；某渠道频率到顶只降级不杀候选。LLM 不在这层（这层是 policy engine 本地算）。
"""
from __future__ import annotations

from typing import Optional

from core.proactive.intel.affect import get_affect
from core.proactive.intel.ledger import get_ledger
from core.proactive.intel.types import (
    Channel, InterventionCandidate, LedgerKey, RiskLevel, Scene,
)

# [原型期标定]
_INTERRUPTION_COST = {Channel.POPUP: 0.50, Channel.CORNER: 0.20, Channel.PANEL: 0.05}
_RISK_COST = {RiskLevel.READONLY: 0.0, RiskLevel.SIDE_LIGHT: 0.15, RiskLevel.SIDE_HEAVY: 0.4}
_THETA_VALUE = 0.05     # 净值下限
_THETA_GATE = 0.08      # 资格下限
# 渠道由高到低试算
_CHANNELS = [Channel.POPUP, Channel.CORNER, Channel.PANEL]


def score(cand: InterventionCandidate) -> dict:
    """返回 {chosen: Channel|None, scores: {ch_value:score}, reason: str}。"""
    affect = get_affect()
    ledger = get_ledger()
    patience = affect.patience()
    scores: dict[str, float] = {}

    # Patience=0（含 disabled / 显式静音冲击）→ 禁一切 visible，只可 silent
    if patience <= 0.0:
        return {"chosen": None, "scores": {}, "reason": "patience_zero"}

    best_ch: Optional[Channel] = None
    best_score = -1.0
    any_veto_reason = "below_threshold"

    for ch in _CHANNELS:
        key = LedgerKey(cand.type, cand.scene, ch)
        e = ledger.get(key)
        welcome = 0.0 if (e.explicit_mute or e.welcome_score <= 0.0) else e.welcome_score
        freq = e.freq_allowance(ch)
        timing = cand.timing_score

        # 硬否决：任一零闸 → 该渠道出局（但不杀整个候选，继续看更低打扰渠道）
        if welcome <= 0.0:
            any_veto_reason = "welcome_zero_or_mute"
            continue
        if freq <= 0.0:
            any_veto_reason = "freq_capped"
            continue

        elig = patience * welcome * freq * timing
        value = cand.utility - _INTERRUPTION_COST[ch] - _RISK_COST.get(cand.risk_level, 0.0)
        s = value * elig if value > 0 else (value)  # value<=0 直接负，不会过线
        scores[ch.value] = round(s, 4)

        if value > _THETA_VALUE and elig > _THETA_GATE and s > best_score:
            best_score = s
            best_ch = ch

    if best_ch is None:
        return {"chosen": None, "scores": scores, "reason": any_veto_reason}
    return {"chosen": best_ch, "scores": scores, "reason": f"fired_{best_ch.value}"}
