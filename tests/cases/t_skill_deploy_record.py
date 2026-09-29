# -*- coding: utf-8 -*-
"""Skill 草稿进审计、部署 / 更新之后写进对话的记录。

草稿进审计（`_emit_skill_preview_from_decision`）：写代码的过程与审计卡都不在对话历史里，
不记一笔的话模型读历史会以为还没写，用户说「部署吧」时去重写或答「没有待审计的 Skill」。

部署 / 更新（`_write_skill_deploy_context`）。两者都是系统陈述的事实，给模型看：
- 走隐藏的系统记录（`visible_to_user=False`），不是普通 assistant 消息——否则模型以为
  自己已经告诉过用户、这一轮不再开口，重放时还会被画成 Nano 的气泡；
- 英文（注入模型的文本一律英文），写明是系统写的；
- 同名覆盖永久 Skill 时写明。

用法：
  py -3.10 tests\\cases\\t_skill_deploy_record.py
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


def t_deploy_record(tmp: pathlib.Path) -> None:
    print("\n[1] 部署记录是隐藏的英文系统记录")
    o = make_orch(tmp / "a")
    before = len(o.memory.storage)
    # 中文描述：旧实现按描述语言写一段中文 assistant 消息
    o._write_skill_deploy_context("ReadClipboardAndCountWords", "读取剪贴板里的文字并统计字数", "create")
    added = o.memory.storage[before:]
    check(len(added) == 1, "写了一条", str(len(added)))
    m = added[-1] if added else None
    check(m is not None and m.role == "assistant", "角色是 assistant（模型能读到）")
    check(m is not None and getattr(m, "visible_to_user", True) is False,
          "⭐⭐ 不对用户可见（系统记录，重放不画成 Nano 的气泡）")
    text = (m.content if m else "") or ""
    frame = text.replace("读取剪贴板里的文字并统计字数", "")
    check(frame.isascii(), "⭐ 除了引用的描述原文，全部是英文", frame[:120])
    check("not something you said" in text and text.startswith("[System record"),
          "⭐⭐ 写明这是系统写的、不是模型说过的话", text[:80])
    check("ReadClipboardAndCountWords" in text and "读取剪贴板" in text, "带上名字与用途")
    check("[Skill deployed] ReadClipboardAndCountWords" in "\n".join(o._session_log),
          "session log 照旧记一行（英文）")


def t_update_and_collision(tmp: pathlib.Path) -> None:
    print("\n[2] 更新与同名覆盖")
    o = make_orch(tmp / "b")
    o._write_skill_deploy_context("GetDNS", "show DNS servers", "update", name_collision=True)
    text = o.memory.storage[-1].content
    check("has been updated" in text, "更新写 updated")
    check("Name collision" in text and "overwritten" in text, "同名覆盖写明")
    check(o.memory.storage[-1].visible_to_user is False, "同样不对用户可见")


def t_draft_record(tmp: pathlib.Path) -> None:
    print("\n[3] 草稿写好、进入审计：对话里记一笔（隐藏、英文）")
    import asyncio
    import types
    o = make_orch(tmp / "c")
    o._put_pending_skill = lambda d: None      # 草稿的工作载荷与本用例无关
    before = len(o.memory.storage)
    dec = types.SimpleNamespace(args={"filename": "CountClipboardWords", "code": "x = 1\n",
                                      "description": "count the words on the clipboard"})

    async def run():
        return [e async for e in o._emit_skill_preview_from_decision(dec, "fake")]
    events = asyncio.run(run())
    check(any(e.get("event") == "skill_preview" for e in events), "前提：审计卡照常发出")
    added = o.memory.storage[before:]
    m = added[-1] if added else None
    text = (m.content if m else "") or ""
    check(m is not None and m.visible_to_user is False and m.role == "assistant",
          "⭐⭐ 写了一条隐藏的系统记录（模型读得到，聊天区不显示）", str(len(added)))
    check(text.isascii() and '"CountClipboardWords"' in text and "review window" in text,
          "⭐ 英文，写明草稿名、在审计窗口等用户决定", text[:120])
    check("answer_open_interaction" in text and "do not write it again" in text,
          "⭐ 告诉模型用 answer_open_interaction 处理、不要重写")
    check("did NOT pass validation" in text, "校验没通过时如实写明（这段代码不合协议）")


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_deploy_record_"))
    try:
        t_deploy_record(tmp)
        t_update_and_collision(tmp)
        t_draft_record(tmp)
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
