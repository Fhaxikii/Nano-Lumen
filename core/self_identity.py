# -*- coding: utf-8 -*-
"""Nano 自己的进程与窗口：「哪个窗口是 Nano 自己」的唯一判据。

身份按操作系统给的事实判断（进程 id、窗口句柄），不看标题：标题可以被用户随意命名
（文件名、控制台标题、文件夹名都可能含 "nano"）。

- 本进程（后端）总是算 Nano 自己。
- 界面窗口所在的进程由窗口壳登记（`register_window_process`）；界面窗口句柄已知时
  也可以直接登记（`register_window`）。
- **不包含本进程的全部子进程**：Nano 替用户启动的程序（`launch_app`、`run_command`
  拉起的 GUI 程序等）也是本进程的子进程，它们是用户的程序，不是 Nano 的界面。
"""
from __future__ import annotations

import ctypes
import os
import threading
from typing import Iterable

_lock = threading.Lock()
_window_pids: set[int] = set()
_windows: set[int] = set()


def register_window_process(pid: int) -> None:
    """登记界面窗口所在的进程。"""
    if pid and int(pid) > 0:
        with _lock:
            _window_pids.add(int(pid))


def register_window(hwnd: int) -> None:
    """登记界面窗口句柄。"""
    if hwnd:
        with _lock:
            _windows.add(int(hwnd))


def unregister_window_process(pid: int) -> None:
    with _lock:
        _window_pids.discard(int(pid))


def reset(window_pids: Iterable[int] = (), windows: Iterable[int] = ()) -> None:
    """清空并重新登记（界面重建、测试用）。"""
    with _lock:
        _window_pids.clear()
        _windows.clear()
        _window_pids.update(int(p) for p in window_pids if p)
        _windows.update(int(h) for h in windows if h)


def self_pids() -> frozenset:
    """本进程 + 已登记的界面进程。"""
    with _lock:
        return frozenset({os.getpid(), *_window_pids})


def is_self_pid(pid: int) -> bool:
    return bool(pid) and int(pid) in self_pids()


def window_pid(hwnd: int) -> int:
    """窗口所属进程 id；取不到返回 0。"""
    try:
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def is_self_window(hwnd: int) -> bool:
    """窗口是否属于 Nano：句柄已登记，或所属进程是 Nano 的进程。"""
    if not hwnd:
        return False
    with _lock:
        if int(hwnd) in _windows:
            return True
    return is_self_pid(window_pid(hwnd))
