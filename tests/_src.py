# -*- coding: utf-8 -*-
"""测试读取产品源码的唯一入口。

- `module_text("core.orchestrator")`：按模块名读源码。名字对应的是包（目录）时，
  把包内全部 `.py` 按相对路径排序后拼接返回 —— 模块拆成包之后，基于全文的
  `in` 判断与 `ast.walk` 查找不需要改。
- `module_tree(...)`：上面文本的 AST。
- `find_def(module, name, owner=None)`：找函数 / 方法定义。`owner` 给出时在该类及其
  在本模块 / 包里定义的基类（mixin）里找。找不到或找到多个同名定义都抛
  `LookupError`，**不返回 None**：返回 None 的查找配上 `if fn is not None:`，
  函数被改名或挪走时整组断言会被静默跳过。
- `def_text(...)`：该定义的原文（含注释）。

测试只读被测代码本身；本模块只接受产品模块名，不读其他文件。
"""
from __future__ import annotations

import ast
import functools
import pathlib
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_path(dotted: str) -> pathlib.Path:
    """`core.orchestrator` → `core/orchestrator.py` 或 `core/orchestrator/`。"""
    base = ROOT.joinpath(*dotted.split("."))
    f = base.with_suffix(".py")
    if f.is_file():
        return f
    if base.is_dir() and any(base.glob("*.py")):
        return base
    raise FileNotFoundError(f"no module or package named {dotted!r} under {ROOT}")


def module_files(dotted: str) -> list[pathlib.Path]:
    """模块对应的源文件：单文件模块返回它本身，包返回包内全部 `.py`（排序）。

    给逐文件做静态分析的测试用（例如检查每个文件的导入）。找不到模块时抛错，
    不返回空列表 —— 空列表会让逐文件检查静默地什么都不查。
    """
    p = module_path(dotted)
    if p.is_file():
        return [p]
    return sorted(p.rglob("*.py"), key=lambda x: x.relative_to(p).as_posix())


@functools.lru_cache(maxsize=None)
def module_text(dotted: str) -> str:
    p = module_path(dotted)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    parts = []
    for f in sorted(p.rglob("*.py"), key=lambda x: x.relative_to(p).as_posix()):
        parts.append(f.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


@functools.lru_cache(maxsize=None)
def module_tree(dotted: str) -> ast.Module:
    return ast.parse(module_text(dotted))


def class_and_bases(tree: ast.Module, owner: str) -> list[ast.ClassDef]:
    """`owner` 类，加上它在同一模块 / 包里能找到定义的全部基类（递归）。

    方法可以由基类（mixin）提供：「挂在 Orchestrator 上的方法」包括继承来的。
    基类不在本模块 / 包里（例如 `NamedTuple`）的忽略。
    """
    classes: dict[str, list[ast.ClassDef]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            classes.setdefault(n.name, []).append(n)
    out: list[ast.ClassDef] = []
    todo = [owner]
    seen: set[str] = set()
    while todo:
        cname = todo.pop(0)
        if cname in seen:
            continue
        seen.add(cname)
        for c in classes.get(cname, []):
            out.append(c)
            for b in c.bases:
                bname = b.id if isinstance(b, ast.Name) else (
                    b.attr if isinstance(b, ast.Attribute) else None)
                if bname:
                    todo.append(bname)
    return out


def find_def(dotted: str, name: str, owner: Optional[str] = None) -> ast.AST:
    """按名字找函数 / 方法定义；`owner` 给出时只在该类及其基类里找。"""
    tree = module_tree(dotted)
    scopes = [tree] if owner is None else class_and_bases(tree, owner)
    if owner is not None and not scopes:
        raise LookupError(f"class {owner!r} not found in {dotted}")
    hits = []
    for scope in scopes:
        nodes = ast.walk(scope) if owner is None else ast.iter_child_nodes(scope)
        hits.extend(n for n in nodes if isinstance(n, _DEF_TYPES) and n.name == name)
    if not hits:
        where = f"{owner}." if owner else ""
        raise LookupError(f"def {where}{name} not found in {dotted}")
    if len(hits) > 1:
        raise LookupError(f"def {name} is defined {len(hits)} times in {dotted}; pass owner=")
    return hits[0]


def def_text(dotted: str, name: str, owner: Optional[str] = None) -> str:
    node = find_def(dotted, name, owner)
    seg = ast.get_source_segment(module_text(dotted), node)
    if not seg:
        raise LookupError(f"no source segment for {name} in {dotted}")
    return seg
