# -*- coding: utf-8 -*-
"""静态查 UnboundLocalError：嵌套函数遮蔽了外层名字，且"先读后赋值"。

═══ 为什么要有这个测试 ═══

2026-08-06 实测真崩过一次：

    def _make_minimizable(self, ..., chip: bool = True):
        def _minimize():
            if not chip:                      # ← 读
                return
            with ui.row()... as chip:         # ← 赋值，就是这一行把 chip 变成了局部名
                ...

    UnboundLocalError: local variable 'chip' referenced before assignment

这一崩把「最小化」整个动作打断，还连累实测测试把"代码改动存活不过最小化"
误判成 CodeMirror 同步问题 —— 查错了一整轮。

⚠️ 这类 bug **运行时才炸，而且只炸在那条分支上**，普通冒烟测试摸不到；
   环境里又没有 pyflakes。所以这里手写一个窄检查。

═══ 判据（故意收窄，宁可漏不可吵） ═══

只有同时满足才报：
  1. 内层函数**自己**给某名字赋了值（不下钻更深的嵌套）；
  2. 这个名字在**外层函数**里也是局部名（参数 / 外层自己的赋值）；
  3. 内层对它的**最早一次读，行号早于最早一次赋值**。

第 3 条是关键。少了它会有 150+ 条噪音 —— 内层拿 `e`/`p`/`raw` 当临时变量、
恰好和外层重名，先赋后读，完全无害。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前
from tests._src import module_files, module_text  # noqa: E402

TARGETS = ["app", "core.orchestrator", "core.runtime.interaction",
           "core.runtime.toolbatch", "core.runtime.kernel"]

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _params(fn):
    a = fn.args
    out = {x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
    for x in (a.vararg, a.kwarg):
        if x:
            out.add(x.arg)
    return out


# 推导式在 Python 3 里**自带一层作用域** —— `[f(c) for c in xs]` 里的 `c`
# 既不是外层的局部名，也不会泄漏出去。不建模它就会误报，例如
# `orchestrator.py` 的 `flush_parallel()`：`c` 读在 L7853、`for c in` 在 L7857，
# 行号上"先读后赋值"，但那完全合法。
_COMP = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _comp_targets(node):
    """推导式自己绑定的名字（含嵌套解包 `for a, b in ...`）。"""
    out = set()
    for gen in node.generators:
        for n in ast.walk(gen.target):
            if isinstance(n, ast.Name):
                out.add(n.id)
    return out


def _own_names(fn, want_store):
    """fn **直接拥有**的 Store（或 Load）名字 → {name: 最早行号}。

    跳过更深的函数/类（它们有自己的作用域），并在推导式子树里屏蔽掉
    该推导式自己的目标名。
    """
    out = {}

    def note(name, lineno, masked):
        if name in masked:
            return
        if name not in out or lineno < out[name]:
            out[name] = lineno

    def walk(n, masked):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, _SCOPE):
                continue
            sub = masked | _comp_targets(c) if isinstance(c, _COMP) else masked
            if isinstance(c, ast.Name):
                if isinstance(c.ctx, ast.Store) == want_store:
                    note(c.id, c.lineno, sub)
            elif want_store and isinstance(c, ast.ExceptHandler) and c.name:
                note(c.name, c.lineno, sub)
            walk(c, sub)

    walk(fn, frozenset())
    if want_store:
        # global/nonlocal 声明过的不算局部名
        for n in ast.walk(fn):
            if isinstance(n, (ast.Global, ast.Nonlocal)):
                for nm in n.names:
                    out.pop(nm, None)
    return out


def _own_stores(fn):
    return _own_names(fn, want_store=True)


def _own_loads(fn):
    return _own_names(fn, want_store=False)


def scan(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    hits = []

    def visit(node, stack):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stores, loads = _own_stores(c), _own_loads(c)
                for outer in stack:
                    outer_local = _params(outer) | set(_own_stores(outer))
                    for nm, sl in stores.items():
                        if nm not in outer_local:
                            continue
                        rl = loads.get(nm)
                        if rl is not None and rl < sl:
                            hits.append((rl, outer.name, c.name, nm, sl))
                visit(c, stack + [c])
            else:
                visit(c, stack if not isinstance(c, ast.ClassDef) else stack)

    visit(tree, [])
    return sorted(set(hits))


def t_no_shadow() -> None:
    print("\n[1] 闭包遮蔽外层名字且【先读后赋值】→ UnboundLocalError")
    for mod in TARGETS:
      for p in module_files(mod):
        rel = p.relative_to(ROOT).as_posix()
        hits = scan(str(p))
        if hits:
            for rl, o, i, nm, sl in hits:
                check(False, f"{rel}: {o}() -> {i}() 的 '{nm}'",
                      f"读于 L{rl}、赋值于 L{sl} —— 走到那条分支必崩，"
                      f"把【赋值处】改个名（不要动读的那处）")
        else:
            check(True, f"{rel}: 无先读后赋值的遮蔽")


def t_selfcheck() -> None:
    """⚠️ 断言"扫不出问题"之前，必须先证明这个扫描器**扫得出**问题。
    否则 scan() 恒返回空也能让上面全绿。用真实的崩溃形状喂它。"""
    print("\n[2] ⭐ 扫描器自检（前置条件：它对已知的崩溃形状必须报警）")
    import tempfile
    bad = (
        "def outer(chip=True):\n"
        "    def inner():\n"
        "        if not chip:\n"
        "            return\n"
        "        with open('x') as chip:\n"
        "            pass\n"
        "    return inner\n"
    )
    good = (
        "def outer(chip=True):\n"
        "    def inner():\n"
        "        if not chip:\n"
        "            return\n"
        "        with open('x') as chip_el:\n"
        "            print(chip_el)\n"
        "    return inner\n"
    )
    # 推导式的合法形状 —— 第一版扫描器在这里误报过（orchestrator flush_parallel）
    comp = (
        "def outer(items):\n"
        "    def inner():\n"
        "        return [\n"
        "            str(c)\n"
        "            for c in items\n"
        "        ]\n"
        "    return inner\n"
    )
    with tempfile.TemporaryDirectory() as d:
        def _scan(name, text):
            p = os.path.join(d, name)
            pathlib.Path(p).write_text(text, encoding="utf-8")
            return scan(p)
        hb, hg, hc = _scan("bad.py", bad), _scan("good.py", good), _scan("comp.py", comp)
    check(len(hb) == 1 and hb[0][3] == "chip",
          "对那个真实崩溃形状报警", f"命中={hb}")
    check(hg == [],
          "⭐ 改名后【不再】报警（证明修复方式有效，且不是恒报警）", f"命中={hg}")
    check(hc == [],
          "⭐ 推导式的 `for c in` 读在赋值行之前【不报警】—— 那是独立作用域",
          f"命中={hc}")


def main() -> int:
    t_selfcheck()      # 自检放前面：它挂了，下面的绿色就没有意义
    t_no_shadow()
    passed = sum(1 for r in _results if r[0])
    total = len(_results)
    print("\n" + "=" * 74)
    if passed == total:
        print(f"结果：{passed}/{total} 通过")
    else:
        print(f"结果：{passed}/{total} 通过 —— 失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
