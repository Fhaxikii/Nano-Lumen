# core/proactive/hooks.py
"""
系统事件钩子：键盘监听 + 前台窗口轮询。
只采集元数据（按键类型、窗口切换），不记录内容。

启动方式：
    from core.proactive.hooks import start_hooks
    start_hooks()   # 在 NiceGUI event loop 启动后调用

CPU 采样不在这里——由 app.py 里的 ui.timer 每分钟调一次
activity.on_cpu_sample()，保持在 async 上下文。
"""
from __future__ import annotations
import ctypes
import threading
import time
from typing import Optional

import psutil
from loguru import logger

from core.proactive.activity import get_buffer
from core.proactive import referent as _referent

_started = False
_POLL_INTERVAL = 2.0   # 窗口轮询间隔（秒）


# ── 键盘钩子（keyboard 库，后台线程）─────────────────────────────────────────

def _start_keyboard_hook():
    try:
        import keyboard as kb

        def _on_key(event):
            buf = get_buffer()
            name = event.name or ""
            if name == "backspace":
                buf.on_key("backspace")
            elif name in ("ctrl+s",):
                buf.on_key("ctrl_s")
            elif name in ("ctrl+z",):
                buf.on_key("ctrl_z")
            elif len(name) == 1:
                buf.on_key("char")
            # 其余功能键忽略

        kb.on_press(_on_key)
        logger.debug("[Proactive-Hooks] 键盘钩子已启动")
    except Exception as e:
        logger.warning(f"[Proactive-Hooks] 键盘钩子启动失败（不影响主流程）: {e}")


# ── 前台窗口轮询（ctypes + psutil）────────────────────────────────────────────

def _get_foreground_info() -> Optional[tuple[int, str, str, int]]:
    """返回 (pid, process_name, title, hwnd) 或 None。

    ⚠️ 2026-08-26 补上 `hwnd` —— 它一直被取到（`GetForegroundWindow()`），
       只是没往外传。📌 explorer 的所有窗口可能共用一个 pid，**只有 hwnd 能
       区分是哪个窗口**，而「刚才那个文件夹」正需要它。
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        proc = psutil.Process(pid.value)
        return (pid.value, proc.name(), title, int(hwnd))
    except Exception:
        return None


def _get_audio_apps() -> list:
    """当前正在【出声】的程序（前台焦点之外的活动，如后台放歌），带上它的窗口标题
    （音乐 app 的标题常含歌名/歌手）。用 Windows 音频会话：State==Active 且有进程 = 在播放。"""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return []
    # pid → 窗口标题：后台 app 也能拿到（标题里常有歌名/视频名）
    pid_title = {}
    try:
        import pygetwindow as gw
        user32 = ctypes.windll.user32
        for w in gw.getAllWindows():
            t = (w.title or "").strip()
            if not t:
                continue
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(w._hWnd, ctypes.byref(pid))
            if pid.value and pid.value not in pid_title:
                pid_title[pid.value] = t
    except Exception:
        pass
    out = []
    try:
        for s in AudioUtilities.GetAllSessions():
            try:
                if getattr(s, "State", 0) == 1 and s.Process:   # 1 = AudioSessionStateActive
                    nm = (s.Process.name() or "").replace(".exe", "").strip()
                    low = nm.lower()
                    if not nm or low in ("python", "python3", "pythonw"):
                        continue
                    title = pid_title.get(s.Process.pid, "")
                    disp = f"{nm}（{title[:50]}）" if title else nm
                    if disp not in out:
                        out.append(disp)
            except Exception:
                continue
    except Exception:
        return []
    return out


# ⚠️ 2026-08-26：浏览器进程名、`_domain_of`、地址栏读取**全部搬进
#    `core/proactive/referent.py`** —— 本文件原来自己抄了一份同样的集合，
#    而那边解析句柄时也要用。📌 一份清单出现两处，迟早会分叉。
#    ⭐ 地址栏读取（`referent.browser_domain()`）**只交出域名** ——
#       抓完整 URL 那一版 2026-08-27 被否掉，墓碑在那个函数的 docstring 里。


def _window_poll_loop():
    buf = get_buffer()
    last_pid: int = 0
    last_title: str = ""
    _tick = 0

    while True:
        try:
            info = _get_foreground_info()
            if info:
                pid, name, title, hwnd = info
                if pid != last_pid or title != last_title:
                    # ⭐⭐ **在焦点变化那一刻解析句柄**（2026-08-26 定）。
                    #    📌 采集时不解析，等用户开口再去查，拿到的就是**那时候**的
                    #       状态而不是他说的那个 —— 尤其浏览器，翻页只要一次点击。
                    #    ⚠️ 这里只做**便宜**的那一半（cmdline / Shell COM）；
                    #       地址栏 UIA 贵，仍走下面 6s 的慢轮询再回填。
                    _ref = None
                    try:
                        _ref = _referent.resolve(
                            app=(name or "").replace(".exe", ""),
                            pid=pid, hwnd=hwnd, title=title)
                    except Exception as _e_r:
                        logger.debug(f"句柄解析失败: {_e_r}")
                    buf.on_window_focus(pid, name, title, hwnd=hwnd, ref=_ref)
                    last_pid = pid
                    last_title = title
                    # 🔴 这一行是**免费的第二传感器**：本轮询靠
                    # `GetForegroundWindow()` 主动问，**不经过任何钩子**。
                    # 低级钩子被 Windows 静默摘掉时（`LowLevelHooksTimeout`），
                    # 只有它还能看到焦点变过 → 用来证伪一份"空得可疑"的挂起日志。
                    try:
                        from core.proactive import takeover_log
                        takeover_log.note_poll_focus()
                    except Exception:
                        pass
        except Exception:
            pass
        # 后台音频 + 浏览器域名每 ~6s 查一次（比窗口轮询贵，不用每 2s 都查）
        if _tick % 3 == 0:
            try:
                buf.set_audio_apps(_get_audio_apps())
            except Exception:
                pass
            try:
                fg_proc = (info[1] if info else "").replace(".exe", "").lower()
                _fg_hwnd = int(info[3]) if info else 0
                if fg_proc in _referent.browser_procs():
                    # ⭐ 保留的唯一改动：**按 hwnd 读**，不用 `GetForegroundControl()`
                    #    （后者实测在这台机器上根本读不到）。完整 URL 不出那个函数。
                    buf.set_url_domain(_referent.browser_domain(hwnd=_fg_hwnd))
                else:
                    buf.set_url_domain("")
            except Exception:
                pass
        _tick += 1
        time.sleep(_POLL_INTERVAL)


def _start_window_poll():
    t = threading.Thread(target=_window_poll_loop, daemon=True, name="proactive-win-poll")
    t.start()
    logger.debug("[Proactive-Hooks] 窗口轮询线程已启动")


# ── 公开入口 ──────────────────────────────────────────────────────────────────

def start_hooks():
    global _started
    if _started:
        return
    _started = True
    _start_keyboard_hook()
    _start_window_poll()
    # 被动挂起的传感器是另一套（要 injected 标志 + 毫秒级焦点事件，这里两样都给不了）。
    # 单独一个模块，理由写在 `takeover_hooks.py` 的模块头。
    try:
        from core.proactive.takeover_hooks import start_takeover_hooks
        start_takeover_hooks()
    except Exception as e:
        logger.warning(f"[Proactive-Hooks] 被动挂起传感器启动失败（不影响主流程）: {e}")
    logger.debug("[Proactive-Hooks] 全部钩子已就绪")
