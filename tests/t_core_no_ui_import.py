# -*- coding: utf-8 -*-
"""后端（core / memory / skills）不导入 UI 框架。

前端会整体迁移（NiceGUI + pywebview → 另一套前端），后端里任何一处直接导入 UI 框架，
迁移时都会在运行到那一行时才失败。后端需要窗口能力时由 app.py 注入回调
（例：`Orchestrator._native_window`，看屏幕前用它最小化自己）。

用法：
  py -3.10 tests\\t_core_no_ui_import.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import find_def, module_text  # noqa: E402

_UI = {"nicegui", "webview"}
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_no_ui_imports() -> None:
    print("\n▶ 后端模块里没有 UI 框架的 import（含函数内的延迟 import）")
    files = []
    for d in ("core", "memory", "skills"):
        files += sorted((ROOT / d).rglob("*.py"))
    hits = []
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods = [n.module]
            if any(m.split(".")[0] in _UI for m in mods):
                hits.append(f"{f.relative_to(ROOT)}:{n.lineno}")
    check(not hits, "没有 nicegui / webview 的 import", ", ".join(hits))
    check(len(files) > 50, "确实扫描到了后端代码（扫描器没有失效）", f"n={len(files)}")


def t_window_is_injected() -> None:
    print("\n▶ 看屏幕时让开自己的窗口：用注入的回调")
    impl = ast.unparse(find_def("core.orchestrator", "_look_at_screen_impl", owner="Orchestrator"))
    check("self._native_window" in impl and ".minimize()" in impl and ".restore()" in impl,
          "_look_at_screen_impl 通过 _native_window 最小化并恢复")
    app_src = module_text("app")
    check("self.agent._native_window = " in app_src, "app.py 注入了 _native_window")


if __name__ == "__main__":
    print("=" * 74)
    print("后端不导入 UI 框架")
    print("=" * 74)
    t_no_ui_imports()
    t_window_is_injected()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
