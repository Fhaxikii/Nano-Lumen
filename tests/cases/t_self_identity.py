# -*- coding: utf-8 -*-
"""「哪个窗口是 Nano 自己」的统一判据（core/self_identity.py）。

- 身份按进程 id / 窗口句柄判断：本进程 + 窗口壳登记的界面进程 / 窗口。
- 本进程的子进程**不**自动算 Nano 自己（Nano 替用户启动的程序也是它的子进程）。
- 标题不参与判断：core 里不再有按标题识别自己的代码。
- app.py 在启动时按 NiceGUI 原生窗口子进程的 target 名登记界面进程。

用法：
  py -3.10 tests\\cases\\t_self_identity.py
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import re
import subprocess
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_pids() -> None:
    print("\n▶ 进程判据")
    from core import self_identity as SI
    SI.reset()
    check(SI.self_pids() == frozenset({os.getpid()}), "未登记时只有本进程")
    check(SI.is_self_pid(os.getpid()), "本进程是 Nano 自己")
    check(not SI.is_self_pid(0), "pid 0 不是")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        check(not SI.is_self_pid(child.pid),
              "本进程的子进程不自动算 Nano 自己（Nano 替用户启动的程序也是子进程）", str(child.pid))
        SI.register_window_process(child.pid)
        check(SI.is_self_pid(child.pid), "登记为界面进程后算 Nano 自己")
        SI.unregister_window_process(child.pid)
        check(not SI.is_self_pid(child.pid), "撤销登记后不再算")
    finally:
        child.kill()
        child.wait(10)
    SI.reset(window_pids=[123456])
    check(SI.self_pids() == frozenset({os.getpid(), 123456}), "reset 可以重新登记")
    SI.reset()


def t_windows() -> None:
    print("\n▶ 窗口判据")
    from core import self_identity as SI
    SI.reset()
    u = ctypes.windll.user32
    u.CreateWindowExW.restype = ctypes.c_void_p
    u.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    u.DestroyWindow.argtypes = [ctypes.c_void_p]
    # 本进程建一个不显示的顶层窗口，标题故意不含关键词
    own = u.CreateWindowExW(0, "STATIC", "unrelated title", 0, 0, 0, 10, 10, None, None, None, None)
    try:
        check(bool(own), "建出了本进程的窗口")
        check(SI.window_pid(own) == os.getpid(), "window_pid 取到所属进程")
        check(SI.is_self_window(own), "本进程的窗口是 Nano 自己（与标题无关）")
    finally:
        if own:
            u.DestroyWindow(own)
    u.GetShellWindow.restype = ctypes.c_void_p
    shell = u.GetShellWindow()
    if shell:
        check(not SI.is_self_window(shell), "其他进程的窗口不是（桌面 shell 窗口）")
        SI.register_window(shell)
        check(SI.is_self_window(shell), "登记过的窗口句柄算 Nano 自己")
        SI.reset()
        check(not SI.is_self_window(shell), "reset 后不再算")
    check(not SI.is_self_window(0), "句柄 0 不是")


def t_no_title_judgement() -> None:
    print("\n▶ core 里不再按标题识别自己")
    hits: list[str] = []
    pat = re.compile(r"""_is_self_window_title|_SELF_WINDOW_TITLE_MARKERS|_SELF_TITLES|_own_pids\b""")
    for f in S.module_files("core"):
        if f.suffix != ".py":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8-sig").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{f.relative_to(ROOT).as_posix()}:{i}")
    check(not hits, "旧的标题 / 进程树判据已全部移除", ", ".join(hits[:5]))

    users = {"core.os_layer.executor_low", "core.os_layer.window_binding", "core.proactive.takeover",
             "core.proactive.intel.salience", "core.proactive.activity", "core.orchestrator"}
    missing = [m for m in sorted(users) if "self_identity" not in S.module_text(m)]
    check(not missing, "各调用方都改用 core.self_identity", ", ".join(missing))


def t_app_registers_native_window() -> None:
    print("\n▶ app.py 登记原生窗口进程")
    src = S.def_text("app", "_register_native_window_process")
    check("_nicegui_app.on_startup(_register_native_window_process)" in S.module_text("app"),
          "启动时调用登记")
    from core import self_identity as SI
    SI.reset()

    def _open_window():
        pass

    def _other():
        pass

    fake_mp = types.SimpleNamespace(active_children=lambda: [
        types.SimpleNamespace(_target=_other, pid=111),
        types.SimpleNamespace(_target=_open_window, pid=222),
    ])
    ns: dict = {"logger": types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)}
    exec(src, ns)
    import multiprocessing as real
    sys.modules["multiprocessing"] = fake_mp
    try:
        ns["_register_native_window_process"]()
    finally:
        sys.modules["multiprocessing"] = real
    check(SI.is_self_pid(222) and not SI.is_self_pid(111),
          "只登记 target 为 _open_window 的子进程", str(sorted(SI.self_pids())))
    SI.reset()


if __name__ == "__main__":
    print("=" * 74)
    print("统一自我身份")
    print("=" * 74)
    t_pids()
    t_windows()
    t_no_title_judgement()
    t_app_registers_native_window()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
