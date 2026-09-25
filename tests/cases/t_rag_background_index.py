# -*- coding: utf-8 -*-
"""知识库启动时的后台索引：进程内只启动一次，所有调用方拿到同一个完成信号。

NiceGUI 会多次实例化 Orchestrator。每次都起索引线程会让多个线程同时跑 OCR、耗尽内存；
完成信号如果是每个调用方自己的，第二次调用的短路分支会立即 set()，初始化遮罩提前消失。
完成信号表达的是「初始化流程结束」，成功、失败都要 set，否则遮罩永远不撤。

用法：
  py -3.10 tests\\cases\\t_rag_background_index.py
"""
from __future__ import annotations

import ast
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import find_def, module_text  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _reset(R) -> None:
    R._background_index_started = False
    R._background_index_done = threading.Event()


def t_once_and_shared() -> None:
    print("\n▶ 调用两次：只起一个索引线程，两次拿到同一个信号")
    from core import rag as R
    saved = (R.index_documents, R.cleanup_stale_temp_files)
    gate = threading.Event()
    calls = []

    def fake_index(*a, **k):
        calls.append(1)
        gate.wait(10)
        return {"indexed": 0, "skipped": 0, "errors": []}

    R.index_documents, R.cleanup_stale_temp_files = fake_index, lambda: 0
    _reset(R)
    try:
        ev1 = R.start_background_index()
        ev2 = R.start_background_index()
        check(ev1 is ev2, "两次调用拿到同一个 Event")
        check(not ev1.is_set(), "索引还在跑时信号未 set（第二次调用没有提前 set）")
        gate.set()
        check(ev1.wait(10), "索引结束后信号 set")
        check(len(calls) == 1, "索引只跑了一次", str(len(calls)))
    finally:
        gate.set()
        R.index_documents, R.cleanup_stale_temp_files = saved
        _reset(R)


def t_failure_still_signals() -> None:
    print("\n▶ 索引抛异常：信号照样 set")
    from core import rag as R
    saved = (R.index_documents, R.cleanup_stale_temp_files)

    def boom(*a, **k):
        raise RuntimeError("simulated index failure")

    R.index_documents, R.cleanup_stale_temp_files = boom, lambda: 0
    _reset(R)
    try:
        ev = R.start_background_index()
        check(ev.wait(10), "失败时信号也 set（遮罩不会永远挡着）")
    finally:
        R.index_documents, R.cleanup_stale_temp_files = saved
        _reset(R)


def t_orchestrator_wiring() -> None:
    print("\n▶ Orchestrator 只调用一句，不再持有自己的启动状态")
    init = ast.unparse(find_def("core.orchestrator", "__init__", owner="Orchestrator"))
    check("self._rag_ready = rag_engine.start_background_index()" in init,
          "__init__ 把共享信号挂到 _rag_ready 上")
    tree = ast.parse(module_text("core.orchestrator"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    check(not names & {"_rag_init_started", "_rag_init_lock", "_rag_ready_event"},
          "orchestrator 里没有模块级的 RAG 启动状态")


if __name__ == "__main__":
    print("=" * 74)
    print("知识库后台索引只启动一次")
    print("=" * 74)
    t_once_and_shared()
    t_failure_still_signals()
    t_orchestrator_wiring()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
