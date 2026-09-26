# core/proactive/triggers.py
"""
节日 / 用户生日检测，以及勿扰场景判定（主动智能引擎的 L1 层与守卫使用）。
"""
from __future__ import annotations
import time
from datetime import date, datetime
from typing import Optional

# ── 节日检测 ──────────────────────────────────────────────────────────────────

def _get_lunar_date(d: date) -> Optional[tuple]:
    """返回 (lunar_month, lunar_day)，失败返回 None。"""
    try:
        from zhdate import ZhDate
        zh = ZhDate.from_datetime(datetime(d.year, d.month, d.day))
        return (zh.lunar_month, zh.lunar_day)
    except Exception:
        return None


_SOLAR_HOLIDAYS = {
    (1,  1):  ("元旦",    "new_year"),
    (5,  1):  ("劳动节",  "labor_day"),
    (6,  1):  ("儿童节",  "childrens_day"),
    (10, 1):  ("国庆节",  "national_day"),
    (12, 25): ("圣诞节",  "christmas"),
}

_LUNAR_HOLIDAYS = {
    (1,  1):  ("春节",   "spring_festival"),
    (1,  15): ("元宵节", "lantern_festival"),
    (5,  5):  ("端午节", "dragon_boat"),
    (7,  7):  ("七夕",   "qixi"),
    (8,  15): ("中秋节", "mid_autumn"),
}


def detect_holiday(user_profile: dict) -> Optional[dict]:
    today = date.today()
    year = today.year

    # 固定节日
    key = (today.month, today.day)
    if key in _SOLAR_HOLIDAYS:
        name, hid = _SOLAR_HOLIDAYS[key]
        return _make_holiday(name, f"{hid}_{year}")

    # 农历节日
    lunar = _get_lunar_date(today)
    if lunar and lunar in _LUNAR_HOLIDAYS:
        name, hid = _LUNAR_HOLIDAYS[lunar]
        return _make_holiday(name, f"{hid}_{year}")

    # 生日
    birthday = user_profile.get("birthday")  # "MM-DD"
    if birthday:
        try:
            m, d_day = birthday.split("-")
            if today.month == int(m) and today.day == int(d_day):
                return _make_holiday("用户生日", f"birthday_{year}")
        except Exception:
            pass

    return None


def _make_holiday(name: str, holiday_key: str) -> dict:
    return {
        "trigger_id":   holiday_key,
        "category":     "holiday",
        "priority":     0,
        "cooldown":     0,  # 节日不走常规冷却
        "ttl":          86400,  # 当天有效
        "context":      {"holiday_name": name},
        "detected_at":  time.time(),
    }


# ── 勿扰场景检测 ──────────────────────────────────────────────────────────────

_FULLSCREEN_APPS = {
    "vlc.exe", "potplayer64.exe", "mpv.exe",        # 视频播放
    "zoom.exe", "teams.exe", "feishu.exe",           # 会议
    "obs64.exe", "obs32.exe",                        # 直播/录屏
}


def is_do_not_disturb(snap: dict) -> bool:
    """检测当前是否处于勿扰场景（全屏视频/会议/录屏等）。"""
    proc = snap["foreground_process"].lower()
    if proc in _FULLSCREEN_APPS:
        return True
    return False
