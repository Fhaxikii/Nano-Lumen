# -*- coding: utf-8 -*-
"""故障卡（带修复建议的红色卡）在聊天区清空重画之后仍然在。

故障卡不写进聊天记录，重放画不出它。聊天区在一次运行里会被清空重画：
上下文压缩把对话移出之后的同步（`_sync_evicted_after_turn`）、「重置对话」。
错误并没有因为重画而消失，所以本次运行发出过的故障卡要在重画之后重新画上。
（不做「错误恢复后撤卡」；重启后由健康检查重新探测、重新发卡。）

用法：
  py -3.10 tests\\t_fault_card_redraw.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402

from loguru import logger  # noqa: E402
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_behaviour() -> None:
    print("\n▶ 记住故障卡、重画时重新画出")
    import app as _app
    W = _app.WebUI

    class F:
        pass

    for n in ("_render_chat_event", "_redraw_live_faults"):
        setattr(F, n, getattr(W, n))
    f = F()
    drawn = []
    f.chat_container = object()
    f.scroll_area = None
    f._emitted_chat_keys = set()
    f._live_fault_events = []
    f._draw_chat_event = lambda ev: drawn.append(ev.get("title") or ev.get("body"))

    fault = {"category": "fault", "title": "MCP · playwright 不可用", "dedupe_key": "fp1"}
    f._render_chat_event(fault)
    check(drawn == ["MCP · playwright 不可用"] and f._live_fault_events == [fault],
          "故障卡画出并被记住", str(drawn))
    f._render_chat_event(dict(fault))
    check(len(drawn) == 1 and len(f._live_fault_events) == 1,
          "同一个 dedupe_key 不重复画、不重复记")
    f._render_chat_event({"category": "speech", "body": "你好"})
    check(len(f._live_fault_events) == 1, "普通消息不记")
    drawn.clear()
    f._redraw_live_faults()
    check(drawn == ["MCP · playwright 不可用"], "重画时把记住的故障卡重新画出", str(drawn))


def _func(tree, name):
    return next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)


def t_wiring() -> None:
    print("\n▶ 清空聊天区的两处都在清空之后重画")
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for name in ("_sync_evicted_after_turn", "_do_reset_conversation"):
        fn = _func(tree, name)
        body = ast.unparse(fn) if fn is not None else ""
        i_clear = body.find("chat_container.clear()")
        i_redraw = body.find("_redraw_live_faults()")
        check(fn is not None and 0 <= i_clear < i_redraw, f"{name}：clear() 之后调用 _redraw_live_faults()",
              f"clear={i_clear} redraw={i_redraw}")
    sync = ast.unparse(_func(tree, "_sync_evicted_after_turn"))
    check(sync.find("_replay_durable_conversation()") < sync.find("_redraw_live_faults()"),
          "压缩同步：先重放历史，再把故障卡画到末尾")
    n_clear = src.count("self.chat_container.clear()")
    check(n_clear == 2, "清空聊天区的地方只有这两处（新增清空点时要一并接上重画）", f"n={n_clear}")


if __name__ == "__main__":
    print("=" * 74)
    print("故障卡在聊天区重画后保留")
    print("=" * 74)
    t_behaviour()
    t_wiring()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
