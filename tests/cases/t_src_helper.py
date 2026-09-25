# -*- coding: utf-8 -*-
"""测试读取产品源码的 helper（tests/_src.py）。

- 单文件模块与包（目录）都能按模块名读取；包 = 包内全部 `.py` 拼接。
- 查找定义找不到、或同名定义不止一个时抛错，不返回 None；给出 owner 时也在它的基类（mixin）里找。
- 测试文件不再按路径读产品源码（`(ROOT / "x.py").read_text(...)`、
  `Path("x.py").read_text(...)`）：模块拆成包之后按路径读会失效，而按模块名读不会。

用法：
  py -3.10 tests\\cases\\t_src_helper.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _raises(fn, *a, **k) -> str:
    try:
        fn(*a, **k)
    except (LookupError, FileNotFoundError) as e:
        return type(e).__name__
    return ""


def t_module_text() -> None:
    print("\n▶ 按模块名读取")
    check(S.module_text("core.paths") == (ROOT / "core" / "paths.py").read_text(encoding="utf-8"),
          "单文件模块：原文")
    pkg = S.module_text("core.runtime")
    check("class RuntimeKernel" in pkg and "def acquire_activity" in pkg,
          "包：包内各文件拼在一起（kernel.py 与 oslease.py 的内容都在）")
    files = S.module_files("core.runtime")
    check(len(files) > 10 and all(f.suffix == ".py" for f in files), "包：module_files 列出包内文件",
          f"n={len(files)}")
    check(S.module_files("core.paths") == [ROOT / "core" / "paths.py"], "单文件模块：module_files 就是它本身")
    check(_raises(S.module_text, "core.no_such_module") == "FileNotFoundError", "不存在的模块：抛错")


def t_find_def() -> None:
    print("\n▶ 查找定义")
    n = S.find_def("core.runtime", "acquire_activity")
    check(n.name == "acquire_activity", "在包里找到函数（不管它在哪个文件）")
    check(_raises(S.find_def, "core.runtime", "no_such_function") == "LookupError",
          "找不到：抛 LookupError，不返回 None")
    check(_raises(S.find_def, "core.runtime", "__init__") == "LookupError",
          "同名定义不止一个：抛 LookupError，要求给 owner")
    m = S.find_def("core.runtime", "__init__", owner="RuntimeKernel")
    check(m.name == "__init__", "给出 owner 后只在该类里找")
    check(_raises(S.find_def, "core.runtime", "x", owner="NoSuchClass") == "LookupError", "类不存在：抛错")
    # 方法由基类（mixin）提供时，按主类查也要找得到
    mcp = S.find_def("core.orchestrator", "_handle_connect_mcp_decision", owner="Orchestrator")
    check(mcp.name == "_handle_connect_mcp_decision", "owner 是主类时，沿基类（mixin）找到方法")
    check(S.find_def("core.orchestrator", "_handle_connect_mcp_decision", owner="McpMixin") is mcp,
          "直接按 mixin 查是同一个定义")
    check(_raises(S.find_def, "core.orchestrator", "_no_such_method", owner="Orchestrator") == "LookupError",
          "沿基类也找不到：抛错")
    txt = S.def_text("core.runtime", "acquire_activity")
    check(txt.startswith("def acquire_activity") and "#" in txt, "def_text 返回原文（含注释）")


_PATH_READ = re.compile(
    r'(?:\(\s*ROOT(?:\s*/\s*"[^"]+")+\s*\)|(?:pathlib\.)?Path\(\s*"[^"]+\.py"\s*\))\.read_text\(')


def t_no_path_reads_in_tests() -> None:
    print("\n▶ 测试不再按路径读产品源码")
    hits = []
    for f in sorted((ROOT / "tests" / "cases").glob("t_*.py")):
        if f.name == pathlib.Path(__file__).name:
            continue    # 本文件要拿按路径读的原文做对照
        for i, line in enumerate(f.read_text(encoding="utf-8-sig").splitlines(), 1):
            m = _PATH_READ.search(line)
            if m and ".py" in m.group(0):
                hits.append(f"{f.name}:{i}")
    check(not hits, "没有 (ROOT / \"x.py\").read_text / Path(\"x.py\").read_text", ", ".join(hits[:8]))


def t_no_silent_lookup_helpers() -> None:
    print("\n▶ 用例里自写的「按名字找定义」辅助函数，找不到时不返回空值")
    import ast
    hits = []
    for f in sorted((ROOT / "tests" / "cases").glob("t_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("t_"):
                continue
            src = ast.unparse(fn)
            if not (("ast.walk" in src or "iter_child_nodes" in src or ".body" in src)
                    and "FunctionDef" in src and ".name ==" in src):
                continue
            last = fn.body[-1]
            if isinstance(last, ast.Return):
                v = last.value
                vals = v.elts if isinstance(v, ast.Tuple) else [v]
                if all(x is None or (isinstance(x, ast.Constant) and x.value in ("", None)) for x in vals):
                    hits.append(f"{f.name}:{fn.name}")
    check(not hits, "找不到时抛错（返回空值会让「X 不在代码里」恒真）", ", ".join(hits))


def t_patch_global() -> None:
    print("\n▶ 替换包内模块级名字：所有持有它的模块一起换")
    import ast
    from tests._patch import patch_global
    import core.orchestrator as O
    import core.orchestrator.orchestrator as M
    real = O._rt_wait_open
    marker = object()
    undo = patch_global("core.orchestrator", "_rt_wait_open", marker)
    check(O._rt_wait_open is marker and M._rt_wait_open is marker,
          "包顶层与实际调用方所在的子模块都换掉了")
    undo()
    check(O._rt_wait_open is real and M._rt_wait_open is real, "还原")
    check(_raises(patch_global, "core.orchestrator", "_no_such_name", 1) == "LookupError",
          "名字不存在：抛错，不悄悄什么都不换")

    # 用例里不许直接给 core.orchestrator 的模块属性赋值（调用方在子模块里查名字，会落空）
    hits = []
    for f in sorted((ROOT / "tests" / "cases").glob("t_*.py")):
        t = ast.parse(f.read_text(encoding="utf-8-sig"))
        aliases = set()
        for n in ast.walk(t):
            if isinstance(n, ast.Import):
                aliases |= {a.asname for a in n.names if a.name == "core.orchestrator" and a.asname}
            elif isinstance(n, ast.ImportFrom) and n.module == "core":
                aliases |= {a.asname or a.name for a in n.names if a.name == "orchestrator"}
        for n in ast.walk(t):
            tgts = n.targets if isinstance(n, ast.Assign) else ([n.target] if isinstance(n, ast.AugAssign) else [])
            for tg in tgts:
                if isinstance(tg, ast.Attribute) and isinstance(tg.value, ast.Name) and tg.value.id in aliases:
                    hits.append(f"{f.name}:{n.lineno}")
    check(not hits, "用例不直接给 core.orchestrator 的模块属性赋值（用 patch_global）", ", ".join(hits[:5]))


if __name__ == "__main__":
    print("=" * 74)
    print("测试源码 helper")
    print("=" * 74)
    t_module_text()
    t_find_def()
    t_no_path_reads_in_tests()
    t_no_silent_lookup_helpers()
    t_patch_global()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
