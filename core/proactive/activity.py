# core/proactive/activity.py
"""
实时行为元数据采集。
只记录，不判断是否触发——触发判断交给 triggers.py。

采集的信号：
- 键盘事件（按键时间戳、Ctrl+S、Ctrl+Z、Backspace 频率）
- 前台窗口/进程切换
- CPU 采样
- 保存+关闭事件（Ctrl+S 后绑定 process_id）
- Nano 内部信号（用户否定、发送消息时间戳）
"""
from __future__ import annotations
import collections
import time
import threading
import psutil
from dataclasses import dataclass, field
from typing import Deque, Optional
from loguru import logger

# ring buffer 保留最近事件的时间窗口（秒）
_BUFFER_WINDOW = 600  # 10 分钟内的事件


@dataclass
class KeyEvent:
    ts: float
    key: str  # "char" | "backspace" | "ctrl_s" | "ctrl_z"


@dataclass
class WindowEvent:
    ts: float
    process_name: str
    window_title: str
    pid: int
    event: str  # "focus" | "close"
    # ⭐⭐ 2026-08-26 加的两个字段。
    #
    # `hwnd` —— ⚠️ **explorer 必须靠它，pid 靠不住**：实测两个资源管理器窗口
    #    pid 可以不同（4872 / 691932），但默认设置下它们**共用一个 explorer 进程**。
    #    📌 一个「有时候能区分」的标识，等于不能区分。
    #
    # `ref`  —— 这一刻窗口里那个**用户的东西**（文件路径 / 完整 URL / 目录），
    #    形状见 `core/proactive/referent.py`。拿不到就是 None，**绝不猜**。
    #    ⚠️ 它在**采集时**填，不是等用户开口再去解析 ——
    #    📌 「按需解析」这个范式**只在变化慢的对象上成立**：
    #         文件  编辑器通常还开着     慢 ⇒ 成立
    #         URL   翻页 = 一次点击      快 ⇒ **不成立**（等他开口，页面早翻走了）
    #       ⇒ **变化快的必须在采集时留快照。**（2026-08-26 定）
    hwnd: int = 0
    ref: Optional[dict] = None


@dataclass
class SaveEvent:
    ts: float
    pid: int
    process_name: str
    window_title: str


@dataclass
class CpuSample:
    ts: float
    percent: float
    top_process: str
    top_process_is_nano: bool


@dataclass
class NanoInternalEvent:
    ts: float
    event: str  # "user_message" | "user_rejection" | "nano_responded"


class ActivityBuffer:
    """线程安全的行为元数据 ring buffer。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._keys:    Deque[KeyEvent]          = collections.deque()
        self._windows: Deque[WindowEvent]        = collections.deque()
        self._saves:   Deque[SaveEvent]          = collections.deque()
        self._cpu:     Deque[CpuSample]          = collections.deque()
        self._nano:    Deque[NanoInternalEvent]  = collections.deque()

        # 当前打字 session（连续输入段）
        self._typing_session_start: Optional[float] = None
        self._typing_session_consumed: bool = False  # 是否已被某个触发点消费
        self._TYPING_GAP = 30  # 超过30秒无输入则结束当前 session

        # 当前前台窗口
        self.foreground_pid: int = 0
        self.foreground_process: str = ""
        self.foreground_title: str = ""

        # 当前正在【出声】的后台程序（前台焦点之外的活动，如后台放歌）。由 hooks 音频轮询写入。
        self.audio_apps: list = []

        # 当前前台窗口的 hwnd 与句柄。
        self.foreground_hwnd: int = 0
        self.foreground_ref: Optional[dict] = None

        # 当前浏览器地址栏域名（UIA best-effort 读，**只存域名不存完整 URL**）。
        # 🪦 2026-08-27：抓完整 URL 那一版做过、也跑通过，最后被否掉 ——
        #    理由是**跨机器不可靠**（实测 8 拍里 5 拍读不到），
        #    而「一个『在我机器上好使』的功能，比没有这个功能更坏」。
        #    ⇒ 浏览器停留在**互动感**层：页面名字够了，网址不抓。
        self.foreground_url_domain: str = ""

    def _prune(self, buf: Deque, cutoff: float):
        while buf and buf[0].ts < cutoff:
            buf.popleft()

    def _prune_all(self):
        cutoff = time.time() - _BUFFER_WINDOW
        for buf in (self._keys, self._windows, self._saves, self._cpu, self._nano):
            self._prune(buf, cutoff)

    # ── 写入接口（由 app.py 的各事件钩子调用）────────────────────────────────

    def on_key(self, key: str):
        now = time.time()
        with self._lock:
            self._keys.append(KeyEvent(ts=now, key=key))
            self._update_typing_session(now)
            self._prune(self._keys, now - _BUFFER_WINDOW)

    def on_window_focus(self, pid: int, process_name: str, title: str,
                        hwnd: int = 0, ref: Optional[dict] = None):
        now = time.time()
        with self._lock:
            self.foreground_pid = pid
            self.foreground_process = process_name
            self.foreground_title = title
            self.foreground_hwnd = hwnd
            self.foreground_ref = ref
            self._windows.append(WindowEvent(
                ts=now, process_name=process_name,
                window_title=title, pid=pid, event="focus",
                hwnd=hwnd, ref=ref,
            ))
            self._prune(self._windows, now - _BUFFER_WINDOW)

    def on_window_close(self, pid: int, process_name: str, title: str):
        now = time.time()
        with self._lock:
            self._windows.append(WindowEvent(
                ts=now, process_name=process_name,
                window_title=title, pid=pid, event="close"
            ))
            self._prune(self._windows, now - _BUFFER_WINDOW)

    def on_save(self, pid: int, process_name: str, title: str):
        now = time.time()
        with self._lock:
            self._saves.append(SaveEvent(
                ts=now, pid=pid,
                process_name=process_name, window_title=title
            ))
            self._prune(self._saves, now - _BUFFER_WINDOW)

    def on_cpu_sample(self, percent: float):
        now = time.time()
        top_proc = ""
        is_nano = False
        try:
            procs = sorted(psutil.process_iter(["name", "cpu_percent"]),
                           key=lambda p: p.info.get("cpu_percent") or 0,
                           reverse=True)
            if procs:
                top_proc = procs[0].info.get("name", "")
                from core.self_identity import is_self_pid
                is_nano = is_self_pid(procs[0].pid)
        except Exception:
            pass
        with self._lock:
            self._cpu.append(CpuSample(
                ts=now, percent=percent,
                top_process=top_proc, top_process_is_nano=is_nano
            ))
            self._prune(self._cpu, now - _BUFFER_WINDOW)

    def on_nano_event(self, event: str):
        now = time.time()
        with self._lock:
            self._nano.append(NanoInternalEvent(ts=now, event=event))
            self._prune(self._nano, now - _BUFFER_WINDOW)

    # ── 打字 session 管理 ─────────────────────────────────────────────────────

    def _update_typing_session(self, now: float):
        if self._typing_session_start is None:
            self._typing_session_start = now
            self._typing_session_consumed = False
        else:
            last_key_ts = self._keys[-2].ts if len(self._keys) >= 2 else 0
            if now - last_key_ts > self._TYPING_GAP:
                # 间隔太长，开新 session
                self._typing_session_start = now
                self._typing_session_consumed = False

    def consume_typing_session(self, by: str) -> bool:
        """标记当前 typing session 已被某触发点消费，返回是否成功（未消费时才能消费）。"""
        with self._lock:
            if self._typing_session_consumed:
                return False
            self._typing_session_consumed = True
            logger.debug(f"[Proactive] typing session consumed by: {by}")
            return True

    # ── 只读快照（供 triggers.py 使用）──────────────────────────────────────

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._prune_all()
            return {
                "now": now,
                "keys": list(self._keys),
                "windows": list(self._windows),
                "saves": list(self._saves),
                "cpu": list(self._cpu),
                "nano": list(self._nano),
                "typing_session_start": self._typing_session_start,
                "typing_session_consumed": self._typing_session_consumed,
                "foreground_pid": self.foreground_pid,
                "foreground_process": self.foreground_process,
                "foreground_title": self.foreground_title,
                "foreground_hwnd": self.foreground_hwnd,
                "foreground_ref": self.foreground_ref,
                "audio_apps": list(self.audio_apps),
                "url_domain": self.foreground_url_domain,
            }

    def set_audio_apps(self, apps: list):
        with self._lock:
            self.audio_apps = list(apps or [])

    def set_url_domain(self, domain: str):
        """存前台浏览器的**域名**。

        🪦 这个函数在 2026-08-26~27 之间短暂叫过 `set_browser_url`，存完整 URL
           并回填到句柄上。那一版实际也跑通过，最后被否掉 —— 理由见
           `referent.browser_domain` 的墓碑（**跨机器不可靠**）。
           ⇒ 回到原样：只存域名，不碰任何 ref。
        """
        with self._lock:
            self.foreground_url_domain = (domain or "").strip()

    def last_user_message_ts(self) -> float:
        with self._lock:
            for ev in reversed(self._nano):
                if ev.event == "user_message":
                    return ev.ts
        return 0.0

    def last_nano_responded_ts(self) -> float:
        with self._lock:
            for ev in reversed(self._nano):
                if ev.event == "nano_responded":
                    return ev.ts
        return 0.0


# 进程级单例
_buffer = ActivityBuffer()


def get_buffer() -> ActivityBuffer:
    return _buffer
