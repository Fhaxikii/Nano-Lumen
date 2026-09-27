# -*- coding: utf-8 -*-
"""用量：每日限额的设置与状态、今日用量、会话 / 本轮 token、货币符号、token 数的格式化。

金额是估算（按厂商表的单价算），与厂商后台的计费可能有出入。
"""
from __future__ import annotations


def _ut():
    from core.usage import usage_tracker
    return usage_tracker


def format_tokens(n: int) -> str:
    """`4.8K` 这种写法（与后端日志、抽屉里的同一套）。"""
    from core.usage import _fmt_tokens
    return _fmt_tokens(int(n or 0))


def currency() -> str:
    """当前厂商的货币符号（深度求索官方标价是人民币；不折算，折算要汇率而汇率不在官方文档里）。"""
    try:
        from core.models import currency_symbol
        return currency_symbol()
    except Exception:
        return "$"


def budget() -> dict:
    """`{"enabled", "soft_cap", "hard_cap", "status", "cost"}`（`status`：ok / soft / hard）。"""
    ut = _ut()
    cfg = ut.load_config()
    return {"enabled": bool(cfg.get("enabled", True)),
            "soft_cap": float(cfg.get("soft_cap_usd", 5) or 0),
            "hard_cap": float(cfg.get("hard_cap_usd", 10) or 0),
            "status": str(ut.cap_status()), "cost": float(ut.today_cost())}


def save_budget(*, enabled: bool, soft_cap: float, hard_cap: float) -> None:
    ut = _ut()
    c = ut.load_config()
    c["enabled"] = bool(enabled)
    c["soft_cap_usd"] = float(soft_cap)
    c["hard_cap_usd"] = float(hard_cap)
    ut.save_config(c)


def today_cost() -> float:
    return float(_ut().today_cost())


def today_tokens() -> int:
    """今日输入 + 输出 token（按 fresh 算，便宜的缓存读不按满价计）。"""
    i, o = _ut().today_input_output()
    return int(i) + int(o)


def session_tokens_total() -> int:
    return int(sum(_ut().session_tokens()))


def reset_session() -> None:
    _ut().reset_session()


def current_turn_tokens() -> str:
    """当前这一轮的 token（格式化好的）。后端随 `final_result` 带来 `turn_usage` 时优先用那个。"""
    return _ut().turn_tokens_fmt()


def current_turn_cache_hit():
    """当前这一轮的缓存命中率（0~1）；量不到为 None（不是 0）。"""
    return _ut().turn_cache_hit()
