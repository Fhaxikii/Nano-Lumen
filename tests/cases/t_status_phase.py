# -*- coding: utf-8 -*-
"""状态行分开说实话：connecting（到流建立为止）/ thinking 系列（裁决 74）。

- provider 在流建立（响应头已到）时发 `stream_open`；react_loop 每次调用模型前发
  `model_request_start`，把 `stream_open` 转成 `model_stream_open`。
- 界面：新回复一开始、以及每次新调用前显示 `connecting · Ns`；流建立后才进入 thinking 系列，
  thinking 系列的阶段从流建立算起，显示的秒数仍是整段回复的总用时。

用法：
  py -3.10 tests\\cases\\t_status_phase.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _Lbl:
    def __init__(self):
        self.text = ""

    def set_text(self, t):
        self.text = t


def t_timer_wording():
    print("\n▶ 状态行措辞")
    from app import WebUI
    import contextlib

    class _Host:
        def _ui_scope(self):
            return contextlib.nullcontext()
    _Host._resp_status_timer = WebUI._resp_status_timer

    async def run():
        st = {"running": True, "start_time": time.time() - 3, "status_lbl": _Lbl(),
              "spin_lbl": _Lbl(), "model_phase": "connecting", "phase_start": None}
        t = asyncio.ensure_future(_Host()._resp_status_timer(st))
        await asyncio.sleep(0.3)
        a = st["status_lbl"].text
        st["model_phase"], st["phase_start"] = "thinking", time.time()
        await asyncio.sleep(0.3)
        b = st["status_lbl"].text
        st["phase_start"] = time.time() - 25          # 流建立后已思考 25 秒
        await asyncio.sleep(0.3)
        c = st["status_lbl"].text
        st["model_phase"] = "connecting"              # 工具之后的下一次调用
        await asyncio.sleep(0.3)
        d = st["status_lbl"].text
        st["running"] = False
        await t
        return a, b, c, d
    a, b, c, d = asyncio.run(run())
    check(a.startswith("connecting · 3"), "流建立之前说 connecting（秒数是整段总用时）", a)
    check(b.startswith("thinking · 3"), "流建立后进入 thinking（阶段从流建立算起）", b)
    check(c.startswith("thinking more · "), "thinking 系列按流建立后的时长推进", c)
    check(d.startswith("connecting · "), "下一次调用开始时回到 connecting", d)


def t_wiring():
    print("\n▶ 接线")
    prov = S.module_text("core.provider")
    check('yield {"type": "stream_open"}' in prov, "provider 在流建立时发 stream_open")
    rl = S.module_text("core.orchestrator")
    i = rl.index('yield {"event": "model_request_start", **_scope}')
    check(i < rl.index("async for _ev in self._stream_with_window_guard(", i),
          "每次调用模型之前发 model_request_start")
    check('yield {"event": "model_stream_open", **_scope}' in rl, "stream_open → model_stream_open")
    app = S.module_text("app")
    check(app.count("ui.label('connecting · 0s')") == 2 and "ui.label('thinking · 0s')" not in app,
          "新回复与唤醒轮的初始状态都是 connecting")
    check('_rs["model_phase"] = "connecting"' in app and '_rs["model_phase"] = "thinking"' in app,
          "界面按两个事件切换阶段")


def main() -> int:
    t_timer_wording()
    t_wiring()
    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过")
    print("=" * 74)
    if ok != len(_results):
        print("失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  · {name}" + (f"   [{note}]" if note else ""))
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
