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


# ── 节日记录 ──────────────────────────────────────────────────────────────────

def holiday_already_fired(state: dict, holiday_key: str) -> bool:
    return state.get("holidays_fired", {}).get(holiday_key) is not None


def mark_holiday_fired(state: dict, holiday_key: str) -> None:
    state.setdefault("holidays_fired", {})[holiday_key] = {
        "fired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
