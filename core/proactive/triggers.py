# core/proactive/triggers.py
"""
从 activity buffer 快照生成触发候选（candidate）。
只负责"条件是否成立"，不判断冷却，不调 Claude。

每个 detect_* 函数返回 dict | None：
  {
    "trigger_id": str,
    "category":   "companion" | "holiday",
    "priority":   int,          # 越小越高
    "cooldown":   int,          # 自身冷却秒数
    "ttl":        int,          # candidate 有效期秒数（超过则丢弃）
    "context":    dict,         # 传给 Claude 的场景描述
    "detected_at": float,
  }
"""
from __future__ import annotations
import time
from datetime import date, datetime
from typing import Optional

from loguru import logger

# 工作应用白名单（周五下午触发点用）
_WORK_APPS = {
    "winword.exe", "excel.exe", "powerpnt.exe", "wps.exe", "wpp.exe", "et.exe",
    "code.exe", "pycharm64.exe", "idea64.exe", "devenv.exe",
    "chrome.exe", "msedge.exe", "firefox.exe",
    "wxwork.exe", "feishu.exe", "dingtalk.exe", "teams.exe",
}


def _is_work_app(process_name: str) -> bool:
    return process_name.lower() in _WORK_APPS


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


# ── 陪伴类触发检测 ─────────────────────────────────────────────────────────────

def detect_late_night(snap: dict) -> Optional[dict]:
    """异常时段：23:00后持续使用 或 4:00前唤醒。"""
    hour = datetime.fromtimestamp(snap["now"]).hour
    last_msg = _last_nano_event(snap, "user_message")

    if hour >= 23:
        # 23点后有用户操作
        if last_msg and (snap["now"] - last_msg) < 3600:
            return _make_companion("late_night", priority=6, cooldown=24*3600, ttl=3600,
                                   context={"hour": hour, "type": "still_up"})

    if hour < 4:
        # 凌晨4点前唤醒
        recent_keys = [k for k in snap["keys"] if snap["now"] - k.ts < 300]
        if recent_keys:
            return _make_companion("late_night", priority=6, cooldown=24*3600, ttl=1800,
                                   context={"hour": hour, "type": "early_wake"})
    return None


def detect_mealtime_skip(snap: dict) -> Optional[dict]:
    """饭点没停：连续操作35min+且在饭点窗口内。"""
    now_dt = datetime.fromtimestamp(snap["now"])
    h, m = now_dt.hour, now_dt.minute
    in_lunch  = (12, 0) <= (h, m) <= (13, 30)
    in_dinner = (19, 0) <= (h, m) <= (20, 30)
    if not (in_lunch or in_dinner):
        return None

    # 窗口内是否持续活跃35min（没有超过10min的空档）
    meal_label = "lunch" if in_lunch else "dinner"
    window_start = snap["now"] - 35 * 60
    recent_keys = [k for k in snap["keys"] if k.ts >= window_start]
    if len(recent_keys) < 10:
        return None
    # 检查有没有10min空档
    times = [k.ts for k in recent_keys]
    for i in range(1, len(times)):
        if times[i] - times[i-1] > 600:
            return None

    return _make_companion("mealtime_skip", priority=7, cooldown=8*3600, ttl=60*60,
                           context={"meal": meal_label, "hour": h})


def detect_friday_afternoon(snap: dict) -> Optional[dict]:
    """周五下午：工作应用失焦+低活跃90s+。"""
    now_dt = datetime.fromtimestamp(snap["now"])
    if now_dt.weekday() != 4:  # 0=周一
        return None
    if not (15 <= now_dt.hour <= 18):
        return None

    # 过去60分钟内有工作应用活跃
    cutoff = snap["now"] - 3600
    had_work = any(
        _is_work_app(w.process_name) for w in snap["windows"]
        if w.ts >= cutoff and w.event == "focus"
    )
    if not had_work:
        return None

    # 当前工作应用已失焦90秒+
    recent_keys = [k for k in snap["keys"] if snap["now"] - k.ts < 90]
    if recent_keys:
        return None

    return _make_companion("friday_afternoon", priority=8, cooldown=7*24*3600, ttl=3*3600,
                           context={"hour": now_dt.hour})


def detect_typing_burst_stop(snap: dict, buffer) -> Optional[dict]:
    """高强度打字10min后突然停止，且该session未被其他触发点消费。"""
    if snap["typing_session_consumed"]:
        return None
    session_start = snap["typing_session_start"]
    if not session_start:
        return None
    session_len = snap["now"] - session_start
    if session_len < 10 * 60:
        return None

    # 最近3min没有键盘输入（停止了）
    recent = [k for k in snap["keys"] if snap["now"] - k.ts < 180]
    if recent:
        return None

    return _make_companion("typing_burst_stop", priority=3, cooldown=6*3600, ttl=3*60,
                           context={"duration_min": round(session_len / 60)})


def detect_large_delete(snap: dict, buffer) -> Optional[dict]:
    """写了大段又全删：输入>80字后30s内删除80%+，优先级高于typing_burst_stop。"""
    if snap["typing_session_consumed"]:
        return None

    # 过去2分钟的按键
    cutoff = snap["now"] - 120
    recent = [k for k in snap["keys"] if k.ts >= cutoff]
    chars   = sum(1 for k in recent if k.key == "char")
    deletes = sum(1 for k in recent if k.key == "backspace")

    if chars < 80:
        return None
    if deletes < chars * 0.8:
        return None

    # 消费session，让typing_burst_stop不会再触发
    buffer.consume_typing_session("large_delete")

    return _make_companion("large_delete", priority=1, cooldown=4*3600, ttl=2*60,
                           context={"chars_written": chars, "chars_deleted": deletes})


def detect_save_and_close(snap: dict) -> Optional[dict]:
    """保存后立刻关闭同一窗口（同一process_id，3秒内）。"""
    if not snap["saves"]:
        return None
    last_save = snap["saves"][-1]
    if snap["now"] - last_save.ts > 60:
        return None

    # 找3秒内相同pid的close事件
    for w in reversed(snap["windows"]):
        if w.event != "close":
            continue
        if w.ts < last_save.ts:
            break
        if (w.ts - last_save.ts) <= 3 and w.pid == last_save.pid:
            return _make_companion("save_and_close", priority=4, cooldown=4*3600, ttl=60,
                                   context={"app": last_save.process_name})
    return None


def detect_nano_rejection(snap: dict) -> Optional[dict]:
    """短时间内用户连续3次否定Nano回答。"""
    cutoff = snap["now"] - 600  # 10分钟内
    rejections = [e for e in snap["nano"]
                  if e.event == "user_rejection" and e.ts >= cutoff]
    if len(rejections) < 3:
        return None
    return _make_companion("nano_rejection", priority=2, cooldown=8*3600, ttl=5*60,
                           context={"count": len(rejections)})


def detect_nano_idle(snap: dict) -> Optional[dict]:
    """Nano陪坐5小时无用户主动消息。"""
    last_msg = _last_nano_event(snap, "user_message")
    if last_msg is None:
        return None
    idle_sec = snap["now"] - last_msg
    if idle_sec < 5 * 3600:
        return None
    return _make_companion("nano_idle", priority=9, cooldown=12*3600, ttl=None,
                           context={"idle_hours": round(idle_sec / 3600, 1)})


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


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _last_nano_event(snap: dict, event: str) -> Optional[float]:
    for e in reversed(snap["nano"]):
        if e.event == event:
            return e.ts
    return None


def _make_companion(trigger_id: str, priority: int, cooldown: int,
                    ttl: Optional[int], context: dict) -> dict:
    return {
        "trigger_id":   trigger_id,
        "category":     "companion",
        "priority":     priority,
        "cooldown":     cooldown,
        "ttl":          ttl,
        "context":      context,
        "detected_at":  time.time(),
    }


# ── 主入口：生成本轮所有候选 ─────────────────────────────────────────────────

def generate_candidates(snap: dict, buffer, user_profile: dict) -> list[dict]:
    """
    按优先级顺序检测所有触发点，返回候选列表（已过 TTL 的丢弃）。
    large_delete 和 typing_burst_stop 互斥：large_delete 优先。
    """
    candidates = []
    now = snap["now"]

    detectors = [
        lambda: detect_large_delete(snap, buffer),       # 必须先于 typing_burst_stop
        lambda: detect_typing_burst_stop(snap, buffer),
        lambda: detect_save_and_close(snap),
        lambda: detect_late_night(snap),
        lambda: detect_mealtime_skip(snap),
        lambda: detect_friday_afternoon(snap),
        lambda: detect_nano_idle(snap),
    ]

    for fn in detectors:
        result = fn()
        if result is None:
            continue
        ttl = result.get("ttl")
        if ttl is not None and (now - result["detected_at"]) > ttl:
            continue
        candidates.append(result)

    return sorted(candidates, key=lambda c: c["priority"])
