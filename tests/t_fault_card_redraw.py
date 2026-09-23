# -*- coding: utf-8 -*-
"""故障卡（带修复建议的红色卡）在聊天区清空重画之后仍然在。

故障卡不写进聊天记录，重放画不出它。聊天区在一次运行里会被清空重画：
上下文压缩把对话移出之后的同步（`_sync_evicted_after_turn`）、「重置对话」。
错误并没有因为重画而消失，所以本次运行发出过的故障卡要在重画之后重新画上。
能力恢复（健康登记表的 RECOVERED：真实调用成功或探针确认能力可用）后撤掉对应的卡；
一张卡合并了多项能力时全部恢复才撤；没有能力清单的卡（上次崩溃记录）不撤。

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


class _El:
    def __init__(self, name):
        self.name, self.deleted = name, False

    def delete(self):
        self.deleted = True


def _fake():
    import contextlib
    import app as _app
    W = _app.WebUI

    class F:
        pass

    for n in ("_render_chat_event", "_redraw_live_faults", "_retire_fault_cards"):
        setattr(F, n, getattr(W, n))
    f = F()
    f.drawn = []
    f.chat_container = object()
    f.scroll_area = None
    f._emitted_chat_keys = set()
    f._live_fault_events = []
    f._fault_card_elements = {}
    f._ui_scope = contextlib.nullcontext

    def _draw(ev):
        el = _El(ev.get("title") or ev.get("body"))
        f.drawn.append(el)
        return el
    f._draw_chat_event = _draw
    return f


def t_behaviour() -> None:
    print("\n▶ 记住故障卡、重画时重新画出")
    f = _fake()
    names = lambda: [e.name for e in f.drawn]  # noqa: E731
    fault = {"category": "fault", "title": "MCP · playwright 不可用", "dedupe_key": "fp1",
             "capabilities": ["mcp.playwright"]}
    f._render_chat_event(fault)
    check(names() == ["MCP · playwright 不可用"] and f._live_fault_events == [fault],
          "故障卡画出并被记住", str(names()))
    f._render_chat_event(dict(fault))
    check(len(f.drawn) == 1 and len(f._live_fault_events) == 1,
          "同一个 dedupe_key 不重复画、不重复记")
    f._render_chat_event({"category": "speech", "body": "你好"})
    check(len(f._live_fault_events) == 1, "普通消息不记")
    f.drawn.clear()
    f._redraw_live_faults()
    check(names() == ["MCP · playwright 不可用"], "重画时把记住的故障卡重新画出", str(names()))


def t_retire() -> None:
    print("\n▶ 能力恢复后撤卡")
    f = _fake()
    one = {"category": "fault", "title": "A 不可用", "dedupe_key": "a:x:gen1", "capabilities": ["cap.a"]}
    two = {"category": "fault", "title": "检测到 2 项能力不可用", "dedupe_key": "b|c",
           "capabilities": ["cap.b", "cap.c"]}
    crash = {"category": "fault", "title": "上次运行没有正常退出", "dedupe_key": "crash:1", "capabilities": []}
    for ev in (one, two, crash):
        f._render_chat_event(ev)
    el_one, el_two, el_crash = f.drawn[:3]

    f._retire_fault_cards({"cap.a"})
    check(el_one.deleted and one not in f._live_fault_events, "单项能力恢复：卡被撤掉、界面元素被删除")
    f._retire_fault_cards({"cap.b"})
    check(not el_two.deleted and two in f._live_fault_events, "合并卡只恢复一部分：保留")
    f._retire_fault_cards({"cap.c"})
    check(el_two.deleted and two not in f._live_fault_events, "合并卡全部恢复：撤掉")
    f._retire_fault_cards({"cap.a", "cap.b", "cap.c", "anything"})
    check(not el_crash.deleted and crash in f._live_fault_events, "没有能力清单的卡（上次崩溃）不撤")

    f.drawn.clear()
    f._redraw_live_faults()
    check([e.name for e in f.drawn] == ["上次运行没有正常退出"], "撤掉的卡不会在重画时回来",
          str([e.name for e in f.drawn]))
    f._retire_fault_cards({"cap.x"})
    check(not f.drawn[0].deleted, "重画后记住的是新画出的元素（撤卡删的是界面上现在那一个）")


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
    tick = ast.unparse(_func(tree, "_health_consumer_tick"))
    check("_recovered.add(" in tick and "self._retire_fault_cards(_recovered)" in tick,
          "健康检查收到 RECOVERED 时调用撤卡")
    check(tick.count("capabilities=") == 2, "两处发故障卡都带上能力清单（单卡 / 合并卡）",
          f"n={tick.count('capabilities=')}")
    n_clear = src.count("self.chat_container.clear()")
    check(n_clear == 2, "清空聊天区的地方只有这两处（新增清空点时要一并接上重画）", f"n={n_clear}")


if __name__ == "__main__":
    print("=" * 74)
    print("故障卡在聊天区重画后保留")
    print("=" * 74)
    t_behaviour()
    t_retire()
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
