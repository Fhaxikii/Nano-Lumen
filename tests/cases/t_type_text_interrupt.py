# -*- coding: utf-8 -*-
"""长文本输入（逐字）中途能停下：急停、用户接手、终止。

`ActionExecutor.type_text` 的英文走逐字输入，原来整段一次写完：只在开始前查一次急停，
而且在事件循环里同步执行——打字期间界面事件（终止按钮）都处理不了，用户点了别的窗口，
剩下的字会打进用户正在用的窗口。

这里用一个记录调用的 `pyautogui` 替身（不往真实桌面打字），执行器本身是真的：
分段、段间检查、线程里打字、返回的结果。

用法：
  py -3.10 tests\\cases\\t_type_text_interrupt.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401,E402

from loguru import logger  # noqa: E402
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _FakePyAutoGUI(types.ModuleType):
    """只记录 write 调用；FAILSAFE / PAUSE 供执行器初始化时设置。"""

    def __init__(self):
        super().__init__("pyautogui")
        self.FAILSAFE = True
        self.PAUSE = 0.1
        self.writes: list[tuple[str, dict]] = []
        self.on_write = None

    def write(self, text, interval=0.0, _pause=True):
        self.writes.append((text, {"interval": interval, "_pause": _pause}))
        if self.on_write is not None:
            self.on_write(len(self.writes))


TEXT = "The quick brown fox jumps over the lazy dog. " * 3   # 135 个 ASCII 字符


def _executor(fake):
    sys.modules["pyautogui"] = fake
    from core.os_layer import executor_action as EA
    ex = EA.ActionExecutor()
    ex._focus_target_window = lambda: None      # 不碰真实窗口
    return EA, ex


def t_full_text(fake) -> None:
    print("\n[1] 没有停止信号：分段打完整段")
    EA, ex = _executor(fake)
    EA.set_input_interrupt_probe(lambda: "")
    fake.writes.clear()
    r = asyncio.run(ex.type_text({"text": TEXT}))
    check(r.get("ok") is True and r["data"]["length"] == len(TEXT), "成功，长度正确", str(r))
    check("".join(w[0] for w in fake.writes) == TEXT, "分段拼起来就是原文，一个字都不差")
    check(len(fake.writes) > 1 and all(len(w[0]) <= EA._TYPE_CHUNK for w in fake.writes),
          f"分段输入（每段不超过 {EA._TYPE_CHUNK} 个字符）", str(len(fake.writes)))
    check(all(w[1]["_pause"] is False for w in fake.writes),
          "段与段之间不叠加 pyautogui 的全局停顿")


def t_takeover_midway(fake) -> None:
    print("\n[2] 用户中途接手：当场停下，如实说打了多少")
    EA, ex = _executor(fake)
    state = {"taken": False}
    EA.set_input_interrupt_probe(lambda: "the user took over the computer" if state["taken"] else "")
    fake.writes.clear()
    fake.on_write = lambda n: state.__setitem__("taken", n >= 3)   # 打完第 3 段时用户点了别处
    try:
        r = asyncio.run(ex.type_text({"text": TEXT}))
    finally:
        fake.on_write = None
    typed = sum(len(w[0]) for w in fake.writes)
    check(r.get("ok") is False and len(fake.writes) == 3, "⭐⭐ 第 3 段之后不再往下打",
          f"{len(fake.writes)} 段")
    check(r["data"]["typed"] == typed and r["data"]["length"] == len(TEXT),
          "结果里带着打了多少、一共多少", str(r["data"]))
    check("took over" in r["error"] and f"{typed} of {len(TEXT)}" in r["error"]
          and r["error"].isascii(), "⭐ 给模型的说明是英文，写明原因与进度", r["error"])


def t_estop_midway(fake) -> None:
    print("\n[3] 急停：当场停下（不等整段打完）")
    EA, ex = _executor(fake)
    EA.set_input_interrupt_probe(lambda: "")
    fake.writes.clear()
    fake.on_write = lambda n: EA._GLOBAL_STOP.set() if n == 2 else None
    try:
        r = asyncio.run(ex.type_text({"text": TEXT}))
    finally:
        fake.on_write = None
        EA._GLOBAL_STOP.clear()
    check(r.get("aborted") is True and len(fake.writes) == 2, "⭐⭐ 急停后不再往下打",
          f"{len(fake.writes)} 段")
    check("emergency stop" in r["error"] and r["data"]["typed"] == 2 * EA._TYPE_CHUNK,
          "结果说明是急停，并带着已打的字数", r["error"])


def t_loop_not_blocked(fake) -> None:
    print("\n[4] 打字期间事件循环不被占住（终止按钮等界面事件照常处理）")
    EA, ex = _executor(fake)
    stop = {"asked": False}
    EA.set_input_interrupt_probe(lambda: "the user stopped this turn" if stop["asked"] else "")
    fake.writes.clear()

    async def run():
        async def press_stop_soon():
            # 另一个协程：只有打字让出事件循环，它才有机会运行
            while len(fake.writes) < 2:
                await asyncio.sleep(0)
            stop["asked"] = True
        t = asyncio.ensure_future(press_stop_soon())
        r = await ex.type_text({"text": TEXT})
        await t
        return r
    r = asyncio.run(run())
    check(r.get("ok") is False and "stopped this turn" in r.get("error", ""),
          "⭐⭐ 打字期间另一个协程按下的终止生效了", r.get("error", ""))
    check(len(fake.writes) < len(TEXT) // EA._TYPE_CHUNK, "没有把整段打完", f"{len(fake.writes)} 段")


def t_probe_wiring() -> None:
    print("\n[5] Orchestrator 登记的判据：终止 / 用户接手 / 都没有")
    from core.orchestrator import Orchestrator
    from core.os_layer import executor_action as EA
    import core.proactive.takeover  # noqa: F401  patch_global 要求模块已导入
    from tests._patch import patch_global

    o = object.__new__(Orchestrator)
    o._stop_asked = lambda: False
    undo = patch_global("core.proactive.takeover", "user_holds_machine", lambda now=None: False)
    try:
        check(o._input_interrupt_reason() == "", "都没有 → 继续")
    finally:
        undo()
    undo = patch_global("core.proactive.takeover", "user_holds_machine", lambda now=None: True)
    try:
        check("took over" in o._input_interrupt_reason(), "用户持有机器 → 停，原因写明接手")
    finally:
        undo()
    o._stop_asked = lambda: True
    check("stopped this turn" in o._input_interrupt_reason(), "按了终止 → 停")

    from tests import _src as S
    init = S.def_text("core.orchestrator", "__init__", owner="Orchestrator")
    check("set_input_interrupt_probe(self._input_interrupt_reason)" in init,
          "Orchestrator 初始化时把判据登记给执行器")
    check(callable(EA.set_input_interrupt_probe), "执行器提供登记口")


def main() -> int:
    real = sys.modules.get("pyautogui")
    fake = _FakePyAutoGUI()
    try:
        t_full_text(fake)
        t_takeover_midway(fake)
        t_estop_midway(fake)
        t_loop_not_blocked(fake)
    finally:
        if real is not None:
            sys.modules["pyautogui"] = real
        else:
            sys.modules.pop("pyautogui", None)
        from core.os_layer import executor_action as EA
        EA.set_input_interrupt_probe(None)
    t_probe_wiring()
    ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    if ok == len(_results):
        print(f"结果：{ok}/{len(_results)} 通过")
    else:
        print(f"结果：{ok}/{len(_results)} 通过 —— 失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
