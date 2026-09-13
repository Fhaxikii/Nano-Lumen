# core/proactive/intel/safety_l0.py
"""
L0 硬安全：事实无歧义 + 有明确动作 + severity 可测。
状态机 + alert_id 去重，防"硬安全"变新噪音源。可穿透静音，但严格受限。

v0 可轮询的 severity：电量将尽、磁盘将满（psutil 直接拿）。
事件驱动型（保存失败/应用崩溃/卡死）提供 raise_alert() 入口——有 sensor 时即插即用，
不留半成品：没有 sensor 时这些就是不触发，而不是"待实现"。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from core.proactive.intel.types import AlertState, L0Alert

# [原型期标定]
_BATTERY_CRIT = 5          # %
_DISK_CRIT_GB = 1.0        # 剩余 GB
_PENETRATE_DEDUP_S = 24 * 3600   # 同 alert_id 24h 最多穿透一次


def _make_alert_id(alert_type: str, resource: str, severity_band: str) -> str:
    return f"{alert_type}|{resource}|{severity_band}"


class _L0:
    def __init__(self):
        self._lock = threading.Lock()
        self._alerts: dict[str, L0Alert] = {}

    # ── 轮询型探测（v0 可用）──
    def poll(self) -> list[L0Alert]:
        """返回本轮"可穿透"的新/升级警报（已过去重）。"""
        out = []
        for a in self._detect_battery() + self._detect_disk():
            if a and self._should_penetrate(a):
                out.append(a)
        return out

    def _detect_battery(self) -> list[Optional[L0Alert]]:
        try:
            import psutil
            b = psutil.sensors_battery()
            if b is not None and not b.power_plugged and b.percent <= _BATTERY_CRIT:
                aid = _make_alert_id("battery_low", "device", "critical")
                return [L0Alert(alert_id=aid, alert_type="battery_low", severity_band="critical")]
        except Exception:
            pass
        return []

    def _detect_disk(self) -> list[Optional[L0Alert]]:
        try:
            import psutil, os
            drive = os.environ.get("SystemDrive", "C:") + "\\"
            free_gb = psutil.disk_usage(drive).free / (1024 ** 3)
            if free_gb < _DISK_CRIT_GB:
                aid = _make_alert_id("disk_low", "system_drive", "below_1GB")
                return [L0Alert(alert_id=aid, alert_type="disk_low", severity_band="below_1GB")]
        except Exception:
            pass
        return []

    # ── 事件驱动入口（保存失败/崩溃/卡死，有 sensor 时调用）──
    def raise_alert(self, alert_type: str, resource: str, severity_band: str = "critical") -> Optional[L0Alert]:
        aid = _make_alert_id(alert_type, resource, severity_band)
        a = L0Alert(alert_id=aid, alert_type=alert_type, severity_band=severity_band)
        if self._should_penetrate(a):
            return a
        return None

    def _should_penetrate(self, a: L0Alert) -> bool:
        with self._lock:
            old = self._alerts.get(a.alert_id)
            now = time.time()
            if old is not None:
                if old.state == AlertState.RESOLVED:
                    pass  # resolved 后可重触发
                elif (now - old.last_penetrated_at) < _PENETRATE_DEDUP_S:
                    return False  # 24h 内已穿透过，不重复
            a.state = AlertState.NOTIFIED
            a.last_penetrated_at = now
            self._alerts[a.alert_id] = a
            logger.info(f"[L0] 穿透警报: {a.alert_id}")
            return True

    def resolve(self, alert_id: str):
        with self._lock:
            if alert_id in self._alerts:
                self._alerts[alert_id].state = AlertState.RESOLVED


_l0: Optional[_L0] = None
_l0lock = threading.Lock()


def get_l0() -> _L0:
    global _l0
    if _l0 is None:
        with _l0lock:
            if _l0 is None:
                _l0 = _L0()
    return _l0
