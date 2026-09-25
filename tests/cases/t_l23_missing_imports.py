# -*- coding: utf-8 -*-
"""两处「不崩但一直是错的」缺失导入 + 一个常驻的作用域检查。

背景：这两处都不会崩 —— 它们外面都包着 `except Exception`，所以只是**静默降级**。

🔴 `core/os_layer/executor_low.py`：唯一的 `import ctypes` 写在函数体里，
   而 `_own_pid()` / `_pid_and_proc()` / `_H()` 三个模块级函数都用 `ctypes.`。
   实测 `_own_pid()` **恒为 -1**、`_pid_and_proc()` **恒为 `(0, "")`**。
   后果不是「少一个 PID」——`is_self_window()` 在 2026-08-07 被专门重写过，
   把主键**从标题换成 PID**，而那条路是 `if pid and pid == _own_pid()` →
   **永远进不去**，每次都落回它想废掉的标题判据。
   📌 **一次「把主键从 A 换成 B」的重写，如果 B 那条路上有一个静默返回哨兵值的
      缺陷，那次重写等于没发生 —— 而且看起来发生了。**
   `_own_pid` / `_pid_and_proc` / `is_self_window` 已移除，自身窗口判据统一到
   `core.self_identity`（见 `t_self_identity`）；这里只保留 `_H` 与模块级 `import ctypes` 的检查。

🔴 `core/rag.py:878`：`Status` / `Severity` 没导入（同文件 724 行只导了
   `get_health, Cap`）→ 向量库损坏并被自动清理这件事**用户永远看不到**。
   📌 **一条「出事时才走」的路径上的错误，只会在出事的时候暴露 ——
      也就是最不该再出错的时候。**

⭐ 最后一组是**常驻的 AST 作用域检查**，覆盖全库，防这一整类。
   ⚠️ 它自己的第一版是坏的（用 `ast.walk` 收集绑定 → 把内层函数的局部名算成
      模块级 → **对这一整类 bug 完全免疫**）。
   📌 **遍历 AST 找作用域信息时，`ast.walk` 几乎总是错的** —— 它拉平了
      唯一重要的那个维度。
   📌 **一个查错工具必须先证明它能抓到已知的那个错**，否则它只是让人安心。
"""
import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests._src import module_files, module_text  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(cond, label, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}" + (f"   [{detail}]" if detail else ""))
    else:
        _failed.append(f"{label}   [{detail}]")
        print(f"  FAIL  {label}" + (f"   [{detail}]" if detail else ""))


# ══════════════════════════════════════════════════════════════════════════
# 作用域检查：**按 scope 逐层收集**，不许 ast.walk
# ══════════════════════════════════════════════════════════════════════════
import builtins as _builtins

# ⚠️⚠️ 这里必须用 `dir(_builtins)`，**不能用 `dir(__builtins__)`**。
# 🔴 `__builtins__` 在 `__main__` 里是**模块**、被别的模块 import 时是**dict** ——
#    于是直接跑这个文件时一切正常，而**被别的测试 import 复用时**
#    `dir()` 返回的是 dict 的方法名，`Exception` / `dict` / `input`
#    全都变成「未定义名」假阳性。
# 📌 **一个只在「直接运行」下正确的实现，在被复用的那一刻才暴露** ——
#    而工具类代码的宿命就是被复用。
# ⭐ 这一处是 `t_ui_edge_l25_l27.py` 复用 `undefined_names()` 时暴露的，
#    也就是说：**这个检查器本身第三次被自己的用户抓到问题**。
# 🔴🔴 **`self` / `cls` 曾经在这张表里，2026-08-22 摘掉了。**
#    它们本来就不需要豁免：`_own_bindings` **收函数参数**，
#    所以方法里的 `self` 一直是绑得上的。豁免它们唯一的效果是
#    **让这个检查器对「模块级/函数外用了 self」完全瞎掉**。
#
#    ⚠️ 而那正是它这天漏掉的那个真 bug：一段**模块级**的启动恢复代码里写了
#       `self._startup_interrupted = …` → 运行时 `NameError: self`，
#       被外面那层 `except` 吞成一句「启动恢复失败（不影响启动）」。
#       📌 **一条被吞掉的 NameError，表现成的是「功能失败」，
#          而不是「有人写错了变量」** —— 这正是这个检查器该拦住的那一类。
#
#    ✅ 摘掉之后全库（app.py + core/**）**假阳性 0 条**，
#       而合成样本 `self._foo = 2`（模块级）能被抓到。
#    📌 判据：**一条豁免必须能说出「不豁免会假报什么」。**
#       说不出来的豁免，实际作用只是让检查器少看一块地方。
_BUILTINS = set(dir(_builtins)) | {
    "__file__", "__name__", "__doc__", "__package__",
    "WindowsError", "reveal_type",
}


def _own_bindings(node) -> set:
    """**只收这一层作用域自己的绑定**，不下钻到嵌套函数/类里。

    ⚠️⚠️ 收的方式是「**所有 Store 上下文的 `Name`**」，而不是按语句类型一个个
       枚举（Assign / For / With / ExceptHandler / 推导式 / 海象 …）。
    🔴 第二版就是枚举的，结果漏了**推导式变量**和**嵌套在 `Try` 里的
       `except ... as e`**（`ExceptHandler` 不是 `ast.stmt`，被我的过滤条件丢了）
       → 全库报出一堆 `m` / `i` / `k` / `e` 这样的假阳性。
    📌 **一个「列出所有情况」的实现，它的缺陷永远是「少列了一种」** ——
       而 Python 已经把「这是不是一次绑定」标在了 `ctx` 上，
       直接问它就不会漏。
    📌 与本项目那条老纪律同形：**别去枚举「从哪来的」，直接问「现在是什么」。**
    ⚠️ 推导式在 Py3 里其实有自己的作用域，这里刻意把它的变量算进外层 ——
       方向是**宁可漏报不要假报**：一个只会喊狼来了的检查器会被人关掉。
    """
    out: set = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        for x in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                  + [a.vararg, a.kwarg]):
            if x is not None:
                out.add(x.arg)

    def rec(n, top=False):
        # ⚠️ 嵌套作用域：只记它的名字，不进去
        if not top and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
            out.add(n.name)
            return
        if not top and isinstance(n, ast.Lambda):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                out.add((al.asname or al.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        for c in ast.iter_child_nodes(n):
            rec(c)

    rec(node, top=True)
    return out


def _scopes(tree):
    """产出 (node, 这一层自己的绑定)，模块在最外。"""
    yield tree, _own_bindings(tree)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n, _own_bindings(n)


def _own_loads(node) -> set:
    """**只收这一层自己读的名字**，不下钻嵌套函数（它们有自己的形参）。"""
    out: set = set()
    _inner = {n for c in ast.iter_child_nodes(node) for n in ast.walk(c)
              if False}
    del _inner

    def rec(n, top=False):
        if not top and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda, ast.ClassDef)):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.add(n.id)
        for c in ast.iter_child_nodes(n):
            rec(c)

    rec(node, top=True)
    return out


def undefined_names(path: pathlib.Path) -> list:
    """返回 [(scope_name, missing_name)]。**闭包链按嵌套关系逐层向外找。**"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mod_b = _own_bindings(tree)
    bad = []
    # 记录每个函数节点的祖先链绑定
    parents: dict = {}

    def descend(node, chain):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parents[ch] = chain
                descend(ch, chain + [_own_bindings(ch)])
            elif isinstance(ch, ast.ClassDef):
                descend(ch, chain)
            else:
                descend(ch, chain)

    descend(tree, [mod_b])
    for node, own in _scopes(tree):
        if node is tree:
            visible = set(mod_b)
            name = "<module>"
        else:
            visible = set(own)
            for b in parents.get(node, [mod_b]):
                visible |= b
            name = node.name
        for nm in sorted(_own_loads(node) - visible - _BUILTINS):
            bad.append((name, nm))
    return bad


# ══════════════════════════════════════════════════════════════════════════
def t_executor_low_ctypes() -> None:
    print("\n[1] `executor_low` 的 `_H` 可用、`ctypes` 在模块级导入")
    from core.os_layer import executor_low as E

    check(E._H(30803936) is not None,
          "⭐ `_H()` 不再抛 `NameError`（大 hwnd 转句柄那条路真的能走）")

    _tree = ast.parse(module_text("core.os_layer.executor_low"))
    _mod_imports = {al.asname or al.name for st in _tree.body
                    if isinstance(st, ast.Import) for al in st.names}
    check("ctypes" in _mod_imports,
          "`import ctypes` 在**模块级**（不是某个函数体里）")


def t_rag_health_report() -> None:
    print("\n[3] 🔴 `rag.py` 那条「向量库已自愈」的上报能真的落下去")
    _src = module_text("core.rag")
    _tree = ast.parse(_src)
    _fn = next(n for n in ast.walk(_tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and "CHROMA_HEALED_PENDING_RESTART" in ast.unparse(n))
    # ⚠️ 这条断言原来写死了 `"from core.health import Status, Severity"` ——
    #    等发现 `get_health` 也缺、把导入改成三个名字之后，它就红了。
    # 📌 **一条断言不该写死「那一行长什么样」，要写死「那些名字在不在」** ——
    #    否则每次补一个名字都要回来改断言，而它验的其实一直是同一件事。
    _imported = {al.asname or al.name
                 for n in ast.walk(_fn) if isinstance(n, ast.ImportFrom)
                 and (n.module or "").endswith("health") for al in n.names}
    check({"get_health", "Status", "Severity"} <= _imported,
          "⭐ `get_health` / `Status` / `Severity` **三个都**在用到它们的那个"
          "作用域里被导入 —— "
          "🔴 修之前只有 724 行的 `get_health, Cap`，这条上报每次都抛 "
          "`NameError` 被外层 except 吞掉 → "
          "**向量库损坏并被自动清理这件事用户永远看不到**"
          "（那句 `user_message` 是写给 UI 的），"
          "Nano 也不知道 `KB_STORE` 处于「已清理、等重启重建」这一档。"
          "📌 **一条「出事时才走」的路径上的错误，只会在出事的时候暴露 —— "
          "也就是最不该再出错的时候**")

    from core.health import Status, Severity, Cap  # noqa: F401
    check(hasattr(Status, "RECOVERING") and hasattr(Severity, "WARNING"),
          "而这两个枚举成员真的存在（不是名字对了值不对）")


def t_checker_catches_known_bug() -> None:
    print("\n[4] ⭐⭐⭐ 先证明这个检查器能抓到**已知的那个错**")
    import tempfile

    _bad = '''
import asyncio

def _own_pid():
    try:
        return int(ctypes.windll.kernel32.GetCurrentProcessId())
    except Exception:
        return -1

def _window_title(h):
    import ctypes
    return ctypes.windll.user32
'''
    _p = pathlib.Path(tempfile.mkdtemp()) / "bad.py"
    _p.write_text(_bad, encoding="utf-8")
    _bad_hits = undefined_names(_p)
    check(any(n == "ctypes" for _, n in _bad_hits),
          "⭐⭐⭐ 在**修复前的那个形状**上，检查器确实报出 `ctypes` —— "
          "📌 **一个查错工具必须先证明它能抓到已知的那个错**，"
          "否则它只是让人安心。"
          "⚠️ 我这个工具的第一版就是坏的：用 `ast.walk` 收集绑定 → "
          "把函数体里的 `import ctypes` 算成模块级 → "
          "**对这一整类 bug 完全免疫**，正好是它要抓的那一类。"
          "📌 **遍历 AST 找作用域信息时，`ast.walk` 几乎总是错的** —— "
          "它拉平了唯一重要的那个维度", str(_bad_hits))

    _good = '''
import ctypes

def _own_pid():
    try:
        return int(ctypes.windll.kernel32.GetCurrentProcessId())
    except Exception:
        return -1
'''
    _p2 = pathlib.Path(tempfile.mkdtemp()) / "good.py"
    _p2.write_text(_good, encoding="utf-8")
    check(not [x for x in undefined_names(_p2) if x[1] == "ctypes"],
          "⚠️ 而修好的形状不再报（没有假阳性 —— "
          "一个只会喊狼来了的检查器会被人关掉）")

    # 闭包：内层读外层的名字不算未定义
    _clo = '''
def outer():
    x = 1
    def inner():
        return x
    return inner()
'''
    _p3 = pathlib.Path(tempfile.mkdtemp()) / "clo.py"
    _p3.write_text(_clo, encoding="utf-8")
    check(not undefined_names(_p3),
          "⭐ 闭包读外层变量不算未定义（否则全库都是假阳性）")


def t_no_undefined_in_hot_modules() -> None:
    print("\n[5] ⭐ 常驻检查：核心模块里没有未定义名")
    _targets = [p.relative_to(ROOT).as_posix() for m in (
        "core.os_layer.executor_low", "core.os_layer.executor_write",
        "core.os_layer.longcmd", "core.rag", "core.runtime.progress", "core.registry",
        "core.mcp_client", "core.schema") for p in module_files(m)]
    _all = []
    for rel in _targets:
        _hits = [h for h in undefined_names(ROOT / rel)
                 # 装饰器里那个同名函数是 Python 的合法前向引用形状
                 if h[1] != h[0]]
        if _hits:
            _all.append((rel, _hits))
        check(not _hits, f"{rel} 干净", str(_hits[:3]) if _hits else "")
    check(not _all,
          "⭐ 全部干净 —— 📌 **这一整类缺陷的共同点是「不崩」**，"
          "所以它只能靠这种检查发现，靠跑一遍是发现不了的")


if __name__ == "__main__":
    print("=" * 74)
    print("两处静默的缺失导入 + 常驻作用域检查")
    print("=" * 74)
    for _t in (t_executor_low_ctypes,
               t_rag_health_report, t_checker_catches_known_bug,
               t_no_undefined_in_hot_modules):
        _t()
    print("\n" + "=" * 74)
    print(f"结果: {_passed} passed, {len(_failed)} failed")
    print("=" * 74)
    if _failed:
        for _f in _failed:
            print("  !!", _f)
        sys.exit(1)
