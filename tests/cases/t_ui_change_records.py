# -*- coding: utf-8 -*-
"""用户在界面上对 Nano 环境的改动，怎么让模型知道。

用户在界面上增删、启停 MCP / Skill / 知识库文件时，写成对话里的隐藏系统记录
（`memory.add_system_note`，英文）：它跟着所在的对话一起衰减、重启后仍在；模型只需要知道
它发生过，不主动提起。session log 只记 Nano 自己的操作（英文）。

用法：
  py -3.10 tests\\cases\\t_ui_change_records.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

from core.orchestrator import Orchestrator  # noqa: E402
from core.runtime.clock import FakeClock  # noqa: E402
from core.runtime.kernel import reset_kernel_for_tests  # noqa: E402
from core.runtime.store import RuntimeStore  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402

_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _Provider:
    target_model = "fake"


class _Registry:
    skills: dict = {}

    def get_permanent_manifests(self): return []
    def get_all_manifests(self): return []
    def get_skill_awareness_list(self): return {"official": [], "user": []}
    def is_official_skill(self, n): return False
    def get_skill_source(self, n, include_disabled=False): return None
    def reload_all(self): pass


def make_orch(db_dir: pathlib.Path) -> Orchestrator:
    import core.rag as _rag
    _rag._background_index_started = True   # 不启动知识库后台索引
    db_dir.mkdir(parents=True, exist_ok=True)
    st = RuntimeStore(db_dir / "rt.db")
    _stores.append(st)
    reset_kernel_for_tests(store=st, clock=FakeClock(1_800_000_000.0))
    return Orchestrator(_Provider(), _Registry(), MemoryManager(max_turns=10))


def t_mcp_by_user(tmp: pathlib.Path) -> None:
    print("\n[1] 用户在设置里改 MCP → 对话里的隐藏英文记录，不写 session log")
    o = make_orch(tmp / "a")
    before, log_before = len(o.memory.storage), list(o._session_log)
    o._note_mcp_change("disable", "context7", by="user")
    added = o.memory.storage[before:]
    check(len(added) == 1, "写了一条对话记录", str(len(added)))
    m = added[-1] if added else None
    check(m is not None and m.visible_to_user is False and m.role == "assistant",
          "⭐⭐ 隐藏（不上屏）、模型读得到")
    text = (m.content if m else "") or ""
    check(text.isascii() and "the user disabled" in text and '"context7"' in text,
          "⭐ 英文，写明是用户在设置里做的", text)
    check(o._session_log == log_before, "⭐ 不写 session log（那里只记 Nano 自己的操作）",
          str(o._session_log))


def t_mcp_by_nano(tmp: pathlib.Path) -> None:
    print("\n[2] Nano 自己改 MCP → session log（英文），不另写对话记录")
    o = make_orch(tmp / "b")
    before = len(o.memory.storage)
    o._note_mcp_change("delete", "context7", by="nano")
    check(len(o.memory.storage) == before, "对话里不另写（工具结果本来就在对话里）")
    last = o._session_log[-1] if o._session_log else ""
    check(last.endswith("[MCP removed] context7") and last.isascii(),
          "⭐ session log 一行，英文（原来是中文「Nano删除了 MCP 服务…」）", last)


def t_all_ui_entries_use_system_note() -> None:
    print("\n[3] 界面上的三类改动走同一个出口")
    for mod in ("core.ui_api.skills", "core.ui_api.knowledge"):
        note = S.def_text(mod, "_note")
        check("add_system_note(" in note and "add_message(" not in note
              and "_session_log_append" not in note,
              f"{mod} 的记录走 add_system_note")
    mcp_note = S.def_text("core.ui_api.mcp", "_note")
    check('_note_mcp_change(op, name, by="user")' in mcp_note,
          "core.ui_api.mcp 的记录走 _note_mcp_change(by=\"user\")")


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_ui_change_"))
    try:
        t_mcp_by_user(tmp)
        t_mcp_by_nano(tmp)
        t_all_ui_entries_use_system_note()
    finally:
        for st in _stores:
            try:
                st.close_thread_conn()
            except Exception:
                pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
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
