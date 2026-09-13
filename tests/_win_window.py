# -*- coding: utf-8 -*-
"""自建窗口子进程，供 `t_window_binding.py` 使用。

`window_binding.snapshot()` 只认「够大、可见、不是 Nano 自己」的真实顶层窗口。
用系统记事本当目标时，窗口的归属进程在不同 Windows 版本上不可靠：
经典桌面下 Popen 起的进程就是持窗进程，Win11 打包版记事本却把它挂在
另一个进程名下，导致测试里按 pid 归窗的辅助逻辑失效。

这里改为自建一个**确定由子进程自己拥有**的真实 Win32 顶层窗口：
pid 归属、可见性、最小化/还原、标题都完全可控，任何 Windows 上都成立。
mock 出来的假 hwnd 证明不了真实 Win32 语义，所以这里用的是真窗口。
"""
from __future__ import annotations

import argparse
import sys

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _proc(hwnd, msg, wp, lp):
    """默认窗口过程：未处理的消息交给 DefWindowProcW。"""
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


# 声明 DefWindowProcW 的参数类型，否则 ctypes 把 wParam/lParam 当 c_int 截断，
# 高 32 位溢出的消息（鼠标/滚轮输出大坐标时）会抛 OverflowError，窗口过程直接崩。
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_long


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="无标题 - Notepad")
    ap.add_argument("--x", type=int, default=80)
    ap.add_argument("--y", type=int, default=80)
    ap.add_argument("--w", type=int, default=420)
    ap.add_argument("--h", type=int, default=320)
    args = ap.parse_args()

    w, h = args.w, args.h
    if w < 200 or h < 150:  # window_binding.snapshot 有最小尺寸过滤
        w, h = 420, 320

    WC = "NanoWinTestWnd"
    WndProc = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    wndproc = WndProc(_proc)

    cls = _WNDCLASSEXW()
    cls.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    cls.style = 0
    cls.lpfnWndProc = wndproc
    cls.cbClsExtra = 0
    cls.cbWndExtra = 0
    cls.hInstance = kernel32.GetModuleHandleW(None)
    cls.hbrBackground = wintypes.HANDLE(5)  # COLOR_WINDOW
    cls.lpszClassName = WC
    atom = user32.RegisterClassExW(ctypes.byref(cls))

    hwnd = user32.CreateWindowExW(
        0, WC, args.title,
        0x00CF0000,  # WS_OVERLAPPEDWINDOW
        args.x, args.y, w, h,
        None, None, kernel32.GetModuleHandleW(None), None,
    )
    if not hwnd:
        return 2
    user32.ShowWindow(hwnd, 5)   # SW_SHOW
    user32.UpdateWindow(hwnd)

    # 跑 Windows 消息循环，直到父进程终止我们
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
