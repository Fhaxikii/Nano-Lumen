# core/proactive/state.py
"""
主动开口系统的状态持久化。
原子写入（tmp → rename），崩溃不损坏 JSON。
"""
from __future__ import annotations
import json
import os
import pathlib
from core.paths import data_dir, data_path
import time
from datetime import date
from typing import Any

_STATE_PATH = data_path("c2_state.json")
_USER_PROFILE_PATH = data_path("user_profile.json")


def _read_json(path: pathlib.Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        broken = path.with_suffix(".broken.json")
        try:
            path.rename(broken)
        except Exception:
            pass
    return {}


def _write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_f = open(tmp, "a")
        tmp_f.flush()
        os.fsync(tmp_f.fileno())
        tmp_f.close()
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ── 状态读写 ──────────────────────────────────────────────────────────────────

def load() -> dict:
    return _read_json(_STATE_PATH)


def save(state: dict) -> None:
    _write_json(_STATE_PATH, state)


def load_user_profile() -> dict:
    return _read_json(_USER_PROFILE_PATH)


def save_user_profile(profile: dict) -> None:
    _write_json(_USER_PROFILE_PATH, profile)


# ── 全局冷却 ──────────────────────────────────────────────────────────────────

GLOBAL_COOLDOWN_SEC = 3 * 3600  # 陪伴类相邻触发最短间隔


def global_cooldown_ok(state: dict) -> bool:
    last = float(state.get("last_companion_ts", 0))
    return (time.time() - last) >= GLOBAL_COOLDOWN_SEC


def mark_companion_fired(state: dict) -> None:
    state["last_companion_ts"] = time.time()


# ── 每日上限 ──────────────────────────────────────────────────────────────────

DAILY_COMPANION_MAX = 3


def daily_count_ok(state: dict) -> bool:
    today = str(date.today())
    counts = state.setdefault("daily_counts", {})
    return counts.get(today, {}).get("companion_total", 0) < DAILY_COMPANION_MAX


def increment_daily_count(state: dict) -> None:
    today = str(date.today())
    counts = state.setdefault("daily_counts", {})
    day = counts.setdefault(today, {"companion_total": 0, "holiday_total": 0})
    day["companion_total"] += 1
    # 只保留最近 7 天，防止文件无限增长
    if len(counts) > 7:
        oldest = sorted(counts.keys())[0]
        counts.pop(oldest, None)


# ── 触发点自身冷却 ─────────────────────────────────────────────────────────────

def trigger_cooldown_ok(state: dict, trigger_id: str, cooldown_sec: int) -> bool:
    last = float(state.get("trigger_last", {}).get(trigger_id, 0))
    return (time.time() - last) >= cooldown_sec


def mark_trigger_fired(state: dict, trigger_id: str) -> None:
    state.setdefault("trigger_last", {})[trigger_id] = time.time()


# ── 节日记录 ──────────────────────────────────────────────────────────────────

def holiday_already_fired(state: dict, holiday_key: str) -> bool:
    return state.get("holidays_fired", {}).get(holiday_key) is not None


def mark_holiday_fired(state: dict, holiday_key: str) -> None:
    state.setdefault("holidays_fired", {})[holiday_key] = {
        "fired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def holiday_skip_count(state: dict, holiday_key: str) -> int:
    return state.get("holiday_skipped", {}).get(holiday_key, {}).get("count", 0)


def mark_holiday_skipped(state: dict, holiday_key: str, reason: str) -> None:
    skipped = state.setdefault("holiday_skipped", {})
    entry = skipped.setdefault(holiday_key, {"count": 0})
    entry["count"] += 1
    entry["last_reason"] = reason
    entry["last_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


# ── 连续否定后静默 ─────────────────────────────────────────────────────────────

REJECTION_SILENCE_SEC = 2 * 3600


def in_rejection_silence(state: dict) -> bool:
    last = float(state.get("last_rejection_silence_ts", 0))
    return (time.time() - last) < REJECTION_SILENCE_SEC


def mark_rejection_silence(state: dict) -> None:
    state["last_rejection_silence_ts"] = time.time()
