# skills_official/GetSystemTime.py
"""官方基础 Skill：获取本机当前时间。协议 v3.2"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.schema import (
    BaseSkill,
    SkillResult,
    SkillSpec,
    InputDef,
    ContextLevel,
    SideEffect,
    PermissionLevel,
    Lifecycle,
)


_TZ_ALIASES = {
    "utc": "UTC",
    "gmt": "UTC",
    "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "cn": "Asia/Shanghai",
    "japan": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "jp": "Asia/Tokyo",
    "new_york": "America/New_York",
    "ny": "America/New_York",
    "los_angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "london": "Europe/London",
}

_WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


class GetSystemTime(BaseSkill):
    def get_manifest(self):
        return {
            "name": "GetSystemTime",
            "description": "Get this machine's current time, date, weekday, timezone or unix timestamp. Use when the answer depends on what time it is right now.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "Optional IANA timezone, e.g. Asia/Shanghai. Omit to use this machine's local timezone.",
                    }
                },
                "required": [],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="GetSystemTime",
            purpose="获取本机当前时间、日期、星期、时区和 Unix 时间戳。",
            required_inputs=[],
            optional_inputs=[
                InputDef(
                    name="timezone",
                    type="string",
                    description="可选 IANA 时区；不填使用本机时区。",
                ),
            ],
            data_output_keys=[
                "iso", "date", "time", "weekday", "timezone", "utc_offset", "unix_timestamp"
            ],
            side_effects=[SideEffect.NONE],
            permission_level=PermissionLevel.READONLY,
            not_responsible_for=["不联网校时", "不查询节假日", "不计算日期差"],
            lifecycle=Lifecycle.PERMANENT,
        )

    @staticmethod
    def _resolve_tz(tz: str):
        tz = (tz or "").strip()
        if not tz:
            return datetime.now().astimezone().tzinfo, "local"
        key = tz.lower().replace(" ", "_")
        name = _TZ_ALIASES.get(key, tz)
        try:
            return ZoneInfo(name), name
        except ZoneInfoNotFoundError:
            raise ValueError(f"invalid timezone: {tz}")

    async def run(self, timezone: str = "") -> SkillResult:
        try:
            tzinfo, tz_name = self._resolve_tz(timezone)
            now = datetime.now(tzinfo)
            offset = now.strftime("%z")
            offset = f"{offset[:3]}:{offset[3:]}" if offset else ""
            data = {
                "iso": now.isoformat(timespec="seconds"),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "weekday": _WEEKDAY_CN[now.weekday()],
                "weekday_index": now.weekday() + 1,
                "timezone": tz_name,
                "utc_offset": offset,
                "unix_timestamp": int(now.timestamp()),
                "utc_iso": datetime.now(dt_timezone.utc).isoformat(timespec="seconds"),
            }
            return SkillResult(
                success=True,
                text=f"当前时间：{data['date']} {data['time']}（{data['weekday']}，{data['timezone']}）",
                data=data,
            )
        except Exception as e:
            return SkillResult(success=False, text=f"获取时间失败：{e}", data={})
