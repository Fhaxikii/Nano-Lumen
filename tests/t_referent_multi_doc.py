# -*- coding: utf-8 -*-
"""句柄解析：一个进程 / 一个窗口里有多个文档时，不把别的文档的路径标为已确认。

覆盖：
- 标题自带完整路径（Notepad++ 默认格式）：名字取文件名，路径直接取自标题，不回退到命令行。
- 只有文件名时：命令行只在进程里只有一个文档时才可信（单窗口、至多一个标签页）。
- 资源管理器：同一 HWND 下多个标签页时只取当前选中的；确定不了则 confirmed=False。

全部使用桩，不依赖桌面。
用法：
  py -3.10 tests\\t_referent_multi_doc.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

from core.proactive import referent as R

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _Patch:
    """临时替换模块属性，退出时恢复。"""

    def __init__(self, obj, **attrs):
        self.obj, self.attrs, self.saved = obj, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)


def t_title_path() -> None:
    print("\n▶ 标题中的完整路径")
    npp = r"C:\Users\me\dirB\same.txt - Notepad++ [Administrator]"
    check(R.title_path("notepad++", npp) == r"C:\Users\me\dirB\same.txt",
          "Notepad++ 标题取出完整路径")
    check(R.parse_title("notepad++", npp)[0] == "same.txt",
          "名字取文件名，不是整条路径（注入只需要短名）")
    check(R.title_path("notepad++", r"*C:\a\b.txt - Notepad++") == r"C:\a\b.txt",
          "未保存标记 * 被去掉")
    check(R.title_path("notepad++", r"C:\a - b\x.txt - Notepad++") == r"C:\a - b\x.txt",
          "路径本身含 ' - ' 时按最后一个分隔切开")
    check(R.title_path("notepad", "same.txt - 记事本") == "",
          "只有文件名的标题不产出路径")
    check(R.title_path("code", "a.py - proj - Visual Studio Code") == "",
          "VS Code 标题不产出路径")
    check(R.title_path("chrome", r"C:\a\b.html - Google Chrome") == "",
          "browser 类不产出路径")
    check(R.title_path("notepad++", "new 1 - Notepad++") == "",
          "未命名文档不产出路径")
    check(R.parse_title("notepad", "same.txt - 记事本")[0] == "same.txt",
          "普通标题的解析不变")


class _ProcRaises:
    def __init__(self, *a, **k):
        raise AssertionError("命令行不应被读取")


def _fake_proc(cmd):
    class _P:
        def __init__(self, *a, **k):
            pass

        def cmdline(self):
            return cmd
    return _P


def t_resolve_file() -> None:
    print("\n▶ 文件：标题路径优先，命令行需单文档")
    import psutil
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "dirA").mkdir()
    (d / "dirB").mkdir()
    fa, fb = d / "dirA" / "same.txt", d / "dirB" / "same.txt"
    fa.write_text("a", encoding="utf-8")
    fb.write_text("b", encoding="utf-8")

    with _Patch(psutil, Process=_ProcRaises):
        r = R.resolve(app="notepad++", pid=1, hwnd=1, title=f"{fb} - Notepad++")
    check(bool(r) and r["confirmed"] and r["path"] == str(fb) and r["name"] == "same.txt",
          "标题路径存在：直接确认，且不读命令行", str(r))

    gone = d / "dirB" / "deleted.txt"
    with _Patch(psutil, Process=_fake_proc(["npp.exe", str(d / "dirA" / "deleted.txt")])):
        r = R.resolve(app="notepad++", pid=1, hwnd=1, title=f"{gone} - Notepad++")
    check(bool(r) and not r["confirmed"] and r["path"] == "",
          "标题路径不存在：未确认，不回退到命令行里的同名文件", str(r))

    # 只有文件名：命令行是 dirA（启动时的第一个文件），当前显示的可能是 dirB 的同名文件
    with _Patch(psutil, Process=_fake_proc(["notepad.exe", str(fa)])), \
         _Patch(R, _single_document=lambda *a, **k: False):
        r = R.resolve(app="notepad", pid=1, hwnd=1, title="same.txt - 记事本")
    check(bool(r) and not r["confirmed"] and r["path"] == "",
          "进程里不止一个文档：同名也不确认", str(r))

    with _Patch(psutil, Process=_fake_proc(["notepad.exe", str(fa)])), \
         _Patch(R, _single_document=lambda *a, **k: True):
        r = R.resolve(app="notepad", pid=1, hwnd=1, title="same.txt - 记事本")
    check(bool(r) and r["confirmed"] and r["path"] == str(fa),
          "只有一个文档：命令行路径照常确认", str(r))

    seen = {}

    def _spy(app, pid, hwnd):
        seen.update(app=app, pid=pid, hwnd=hwnd)
        return True

    with _Patch(psutil, Process=_fake_proc(["notepad.exe", str(fa)])), \
         _Patch(R, _single_document=_spy):
        R.resolve(app="notepad", pid=7, hwnd=77, title="same.txt - 记事本")
    check(seen == {"app": "notepad", "pid": 7, "hwnd": 77},
          "resolve 把 hwnd 传到文档数判定", str(seen))

    with _Patch(psutil, Process=_fake_proc(["notepad.exe", str(d / "dirA" / "other.txt")])), \
         _Patch(R, _single_document=_spy):
        seen.clear()
        R.resolve(app="notepad", pid=7, hwnd=77, title="same.txt - 记事本")
    check(seen == {}, "名字对不上时不做文档数判定（不付 UIA 的代价）")


def t_single_document() -> None:
    print("\n▶ 进程里是否只有一个文档")
    cases = [
        ("winword", [11, 12], None, False, "同一进程两个窗口 -> 否"),
        ("winword", [], None, False, "找不到窗口 -> 否"),
        ("winword", [11], None, True, "非标签页应用、单窗口 -> 是"),
        ("notepad", [11], 0, True, "标签页应用、没有标签栏（旧版记事本）-> 是"),
        ("notepad", [11], 1, True, "标签页应用、一个标签 -> 是"),
        ("notepad", [11], 2, False, "标签页应用、两个标签 -> 否"),
        ("notepad", [11], None, False, "标签页应用、读不出标签 -> 否"),
        ("Notepad", [11], 2, False, "进程名大小写不影响"),
        ("notepad++", [11], 3, False, "Notepad++ 三个标签 -> 否"),
    ]
    for i, (app, wins, tabs, want, label) in enumerate(cases):
        R._multi_doc_procs.clear()
        with _Patch(R, _process_windows=lambda pid, w=wins: list(w),
                    _tab_count=lambda h, t=tabs: t,
                    _proc_key=lambda pid, i=i: (pid, float(i))):
            got = R._single_document(app, 1, 11)
        check(got is want, label, f"got={got}")

    def _boom(pid):
        raise OSError("x")

    R._multi_doc_procs.clear()
    with _Patch(R, _process_windows=_boom, _proc_key=lambda pid: (pid, 0.0)):
        check(R._single_document("winword", 1, 11) is False, "枚举窗口出错 -> 否")
    with _Patch(R, _process_windows=lambda pid: [11], _proc_key=lambda pid: None):
        check(R._single_document("winword", 1, 11) is False, "进程已不存在 -> 否")

    print("\n▶ 见过多个文档的进程，之后只剩一个也不再采用命令行")
    R._multi_doc_procs.clear()
    state = {"tabs": 2}
    with _Patch(R, _process_windows=lambda pid: [11],
                _tab_count=lambda h: state["tabs"],
                _proc_key=lambda pid: (pid, 100.0)):
        check(R._single_document("notepad", 5, 11) is False, "两个标签 -> 否")
        state["tabs"] = 1
        check(R._single_document("notepad", 5, 11) is False,
              "关掉一个后只剩一个标签 -> 仍为否（命令行里可能是被关掉的那个）")
    with _Patch(R, _process_windows=lambda pid: [11],
                _tab_count=lambda h: 1,
                _proc_key=lambda pid: (pid, 200.0)):
        check(R._single_document("notepad", 5, 11) is True,
              "同一 pid 但创建时间不同（进程号复用）-> 不受之前记录影响")
    with _Patch(R, _process_windows=lambda pid: [11, 12],
                _proc_key=lambda pid: (pid, 300.0)):
        R._single_document("winword", 6, 11)
    with _Patch(R, _process_windows=lambda pid: [11],
                _proc_key=lambda pid: (pid, 300.0)):
        check(R._single_document("winword", 6, 11) is False,
              "多窗口同理：见过两个窗口后只剩一个 -> 仍为否")
    R._multi_doc_procs.clear()
    with _Patch(R, _process_windows=lambda pid: [11],
                _tab_count=lambda h: None,
                _proc_key=lambda pid: (pid, 400.0)):
        R._single_document("notepad", 7, 11)
    check((7, 400.0) not in R._multi_doc_procs,
          "读不出标签数时不记为多文档（只是这一次不采用）")
    R._multi_doc_procs.clear()


class _C:
    """UIA 控件桩。"""

    def __init__(self, kind, children=()):
        self.ControlTypeName = kind
        self._ch = list(children)

    def GetChildren(self):
        return self._ch


def t_tab_count() -> None:
    print("\n▶ 标签页计数（UIA 桩）")
    tree_tabs = _C("WindowControl", [
        _C("PaneControl", [
            _C("TabControl", [_C("TabItemControl"), _C("TabItemControl"),
                              _C("ButtonControl"), _C("TabItemControl")]),
        ]),
        _C("DocumentControl"),
    ])
    tree_none = _C("WindowControl", [_C("MenuBarControl"), _C("DocumentControl")])
    fake = types.ModuleType("uiautomation")
    saved = sys.modules.get("uiautomation")
    try:
        sys.modules["uiautomation"] = fake
        fake.ControlFromHandle = lambda h: tree_tabs
        check(R._tab_count(1) == 3, "标签栏里的 TabItem 计数，其他按钮不算")
        fake.ControlFromHandle = lambda h: tree_none
        check(R._tab_count(1) == 0, "没有标签栏 -> 0")

        def _raise(h):
            raise RuntimeError("uia")
        fake.ControlFromHandle = _raise
        check(R._tab_count(1) is None, "UIA 出错 -> None")
        fake.ControlFromHandle = lambda h: None
        check(R._tab_count(1) is None, "取不到窗口 -> None")
    finally:
        if saved is not None:
            sys.modules["uiautomation"] = saved
        else:
            sys.modules.pop("uiautomation", None)


class _Item:
    def __init__(self, hwnd, url, name, tab_hwnd=0):
        self.HWND, self.LocationURL, self.LocationName = hwnd, url, name
        self._tab = tab_hwnd
        item = self

        class _SB:
            def GetWindow(self):
                return item._tab

        class _SP:
            def QueryService(self, sid, iid):
                return _SB()

        class _Ole:
            def QueryInterface(self, iid):
                return _SP()

        self._oleobj_ = _Ole()


class _Windows:
    def __init__(self, items):
        self._items = items
        self.Count = len(items)

    def Item(self, i):
        return self._items[i]


def _shell(items):
    class _Sh:
        def Windows(self):
            return _Windows(items)
    return lambda progid: _Sh()


def t_resolve_dir() -> None:
    print("\n▶ 资源管理器标签页")
    import win32com.client
    a = _Item(500, "file:///C:/Users/me/TEST", "TEST", tab_hwnd=111)
    b = _Item(500, "file:///C:/Users/me/TEST2", "TEST2", tab_hwnd=222)
    other = _Item(600, "file:///C:/Users/me/Other", "Other", tab_hwnd=333)

    with _Patch(win32com.client, Dispatch=_shell([other])):
        r = R.resolve(app="explorer", hwnd=600)
    check(bool(r) and r["confirmed"] and r["path"] == str(pathlib.Path("C:/Users/me/Other")),
          "单标签窗口：行为不变", str(r))

    with _Patch(win32com.client, Dispatch=_shell([a, b, other])), \
         _Patch(R, _active_tab_item=lambda h, items: items[1]):
        r = R.resolve(app="explorer", hwnd=500)
    check(bool(r) and r["confirmed"] and r["name"] == "TEST2",
          "多标签：返回选中的标签，而不是第一个条目", str(r))

    with _Patch(win32com.client, Dispatch=_shell([a, b])), \
         _Patch(R, _active_tab_item=lambda h, items: None,
                _window_text=lambda h: "TEST2"):
        r = R.resolve(app="explorer", hwnd=500)
    check(bool(r) and not r["confirmed"] and r["path"] == "" and r["name"] == "TEST2",
          "多标签且确定不了：只给窗口标题，confirmed=False", str(r))

    got = {}
    with _Patch(win32com.client, Dispatch=_shell([a, b, other])), \
         _Patch(R, _active_tab_item=lambda h, items: got.setdefault("n", len(items)) and items[0]):
        R.resolve(app="explorer", hwnd=500)
    check(got.get("n") == 2, "只把同一 HWND 的条目交给选中判定", str(got))

    with _Patch(win32com.client, Dispatch=_shell([other])):
        check(R.resolve(app="explorer", hwnd=999) is None, "没有匹配的窗口 -> None")


def t_active_tab_item() -> None:
    print("\n▶ 当前标签页判定（z 序第一的 ShellTabWindowClass）")
    import win32gui
    a = _Item(500, "u1", "TEST", tab_hwnd=111)
    b = _Item(500, "u2", "TEST2", tab_hwnd=222)
    with _Patch(win32gui, FindWindowEx=lambda *args: 222):
        check(R._active_tab_item(500, [a, b]) is b, "z 序第一的标签窗口对应的条目被选中")
    with _Patch(win32gui, FindWindowEx=lambda *args: 111):
        check(R._active_tab_item(500, [a, b]) is a, "切换标签后跟着变")
    with _Patch(win32gui, FindWindowEx=lambda *args: 0):
        check(R._active_tab_item(500, [a, b]) is None, "找不到标签窗口 -> None")
    c = _Item(500, "u3", "dup", tab_hwnd=222)
    with _Patch(win32gui, FindWindowEx=lambda *args: 222):
        check(R._active_tab_item(500, [a, b, c]) is None, "多个条目对上同一标签窗口 -> None")
    with _Patch(win32gui, FindWindowEx=lambda *args: 999):
        check(R._active_tab_item(500, [a, b]) is None, "没有条目对上 -> None")


if __name__ == "__main__":
    print("=" * 74)
    print("句柄解析：多文档进程 / 多标签窗口")
    print("=" * 74)
    t_title_path()
    t_resolve_file()
    t_single_document()
    t_tab_count()
    t_resolve_dir()
    t_active_tab_item()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
