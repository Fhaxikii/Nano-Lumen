# -*- coding: utf-8 -*-
"""内置工具的 manifest 清单与 builtin 声明一一对应。

manifest 在 `core/tools/manifests.py`，其余事实在 `core/tools/builtin.py`，
由 `BUILTIN_MANIFESTS` 这份显式清单连起来。会静默出错的两种情况：
  · 写了 `_XXX_MANIFEST` 却没加进清单 → builtin 里没有它的声明时，这个工具悄悄不存在；
  · 清单里有、builtin 没有声明 → schema 写好了，模型永远拿不到。
builtin 声明了而清单里没有的，`build_builtin_definitions` 会直接抛 KeyError（响亮）。

用法：
  py -3.10 tests\\cases\\t_tool_manifests.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import find_def  # noqa: E402

from loguru import logger  # noqa: E402
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_list_matches_declarations() -> None:
    print("\n▶ 清单 ↔ 模块里的 manifest ↔ builtin 声明")
    from core.tools import manifests as M
    from core.tools.builtin import build_builtin_definitions

    in_module = {v["name"] for k, v in vars(M).items()
                 if k.endswith("_MANIFEST") and isinstance(v, dict)}
    listed = set(M.BUILTIN_MANIFESTS)
    check(bool(listed), "前置：清单非空", str(len(listed)))
    check(in_module == listed, "模块里每个 manifest 都在清单里",
          str(sorted(in_module ^ listed)))
    check(all(M.BUILTIN_MANIFESTS[n]["name"] == n for n in listed),
          "清单的键就是 manifest 自己的 name")
    declared = {d.name for d in build_builtin_definitions(M.BUILTIN_MANIFESTS)}
    check(declared == listed, "清单里每个工具在 builtin 里都有声明",
          str(sorted(declared ^ listed)))


def t_catalog_uses_the_list() -> None:
    print("\n▶ 工具目录装配读的是清单，不是按名字后缀扫描")
    body = ast.unparse(find_def("core.orchestrator", "_get_tool_catalog", owner="Orchestrator"))
    check("BUILTIN_MANIFESTS" in body, "_get_tool_catalog 传入 BUILTIN_MANIFESTS")
    check("globals()" not in body, "没有 globals() 扫描")


if __name__ == "__main__":
    print("=" * 74)
    print("内置工具 manifest 清单")
    print("=" * 74)
    t_list_matches_declarations()
    t_catalog_uses_the_list()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
