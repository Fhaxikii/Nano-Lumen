# core/proactive/intel/shadow.py
"""
Shadow Mode 日志：记录每次主动决策（含"想说但忍住了"），供实际使用中调参 + 可解释。
只存枚举/分值/reason_code，不存窗口标题全文/文件名/任何语义化敏感标签。
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Optional

from loguru import logger

from core.proactive.intel.types import ShadowLogEntry

_LOG_PATH = pathlib.Path("data/proactive_shadow.jsonl")
_MAX_LINES = 5000  # [原型期标定] 滚动上限，超了截断保留最近


def log(entry: ShadowLogEntry) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": round(entry.ts, 1),
            "type": entry.candidate_type,
            "scene": entry.scene,
            "scores": {k: round(v, 3) for k, v in (entry.score_by_channel or {}).items()},
            "chosen": entry.chosen_channel,
            "reason": entry.reason_code,
        }
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_truncate()
    except Exception as e:
        logger.debug(f"[Shadow] 写日志失败（忽略）: {e}")


def quick(candidate_type: str, scene: str, reason_code: str,
          scores: Optional[dict] = None, chosen: Optional[str] = None) -> None:
    log(ShadowLogEntry(ts=time.time(), candidate_type=candidate_type, scene=scene,
                       score_by_channel=scores or {}, chosen_channel=chosen, reason_code=reason_code))


def recent(n: int = 100) -> list:
    try:
        if not _LOG_PATH.exists():
            return []
        lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def stats(window_n: int = 300) -> dict:
    """粗略趋势指标，供可解释面板。"""
    rows = recent(window_n)
    fired = [r for r in rows if r.get("chosen")]
    held = [r for r in rows if not r.get("chosen")]
    return {"total": len(rows), "fired": len(fired), "held": len(held)}


def _maybe_truncate() -> None:
    try:
        lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_LINES:
            _LOG_PATH.write_text("\n".join(lines[-_MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass
