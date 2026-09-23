# -*- coding: utf-8 -*-
"""窗口身份如实上报 —— 防"对着错误的窗口动手"。

═══ 这个套件来自一次真实的数据损坏 ═══

2026-08-07 实测：Nano 要操作它自己打开的 `新建文本文档.txt`，
用户中途把焦点放到了**自己的**另一个记事本上。于是 Nano 对着**用户的**记事本
`Ctrl+A` + 输入，**把用户的内容清掉了**。

根因不在被动挂起，在**目标窗口的选法**：
`executor_low.get_target_window()` 返回的是「Z-order 最前、且不是 Nano 自己」的窗口 ——
它是一个**「任务开始时」的启发式**（"操作我正在看的窗口"），
却被当成**「每一步都重新求值」的权威**在用。任务中途"最前面的窗口"
等于"谁最后动过"，**包括用户自己的窗口**。

📌 与 同形：**一个事实只能证明它在被测那一刻成立，不能证明现在仍然成立。**

而当时 `look_at_screen` 只返回视觉模型的散文描述，**一个字都没提在看哪个窗口** ——
模型除了在图里认标题之外，没有任何手段发现自己换了对象。

═══ 这一版修的是什么（以及刻意没修什么）═══
✅ **把"我在对着哪个窗口"变成事实**：`look_at_screen` 与 `os_execute` 的结果里
   带上 Win32 取的前台窗口身份，换过就明确警告。
📌 **判据：不要让模型去「记得怀疑」，把变化本身摆到它眼前。**
   早先的设计 三层防护网第 1 层要的是模型**自律**，这里给的是**事实** —— 两者不重复。
✅ **后来做了**：绑定窗口句柄，绑定期从活动租约推导（见 `core/os_layer/window_binding.py`）。
   本套件守的是它之前那一步：先让静默的错对象变成看得见的事实。

用法：
  py -3.10 tests\t_window_identity.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


class _FakeOrch:
    """只借 `_window_identity_note` 这一个方法来测，不构造真 Orchestrator。"""
    from core.orchestrator import Orchestrator as _O
    _window_identity_note = _O._window_identity_note

    def __init__(self):
        self._last_fg_window = None


def t_identity_uses_hwnd() -> None:
    print("\n[1] ⭐⭐ 身份用 hwnd，不用标题")
    from core.os_layer import executor_low as EL

    saved = EL.foreground_identity
    try:
        # 两个**标题完全相同**的记事本 —— 这正是最该分清、而标题最没用的场景
        seq = [
            {"hwnd": 111, "pid": 1, "proc": "notepad.exe", "title": "无标题 - 记事本"},
            {"hwnd": 222, "pid": 2, "proc": "notepad.exe", "title": "无标题 - 记事本"},
        ]
        box = {"i": 0}

        def fake():
            v = seq[min(box["i"], len(seq) - 1)]
            box["i"] += 1
            return v

        EL.foreground_identity = fake
        o = _FakeOrch()
        n1 = o._window_identity_note()
        check("hwnd=111" in n1, "第一次：报出 hwnd", n1.splitlines()[0][:60])
        check("CHANGED" not in n1, "第一次没有「变了」（没有比较基准）")

        n2 = o._window_identity_note()
        check("CHANGED" in n2,
              "⭐⭐ 换到另一个**同名**记事本 → 判定为**变了**。"
              "靠标题比的话这里会说「没变」，而那正是造成数据损坏的那一步")
        check("hwnd=111" in n2 and "hwnd=222" in n2,
              "⚠️ 新旧 hwnd 都写出来了 —— 模型能自己核对，不用信我们的结论")
    finally:
        EL.foreground_identity = saved


def t_warning_is_actionable() -> None:
    print("\n[2] 警告必须可执行，不能只说「变了」")
    from core.os_layer import executor_low as EL

    saved = EL.foreground_identity
    try:
        seq = [{"hwnd": 1, "pid": 1, "proc": "notepad.exe", "title": "a"},
               {"hwnd": 2, "pid": 2, "proc": "notepad.exe", "title": "b"}]
        box = {"i": 0}
        EL.foreground_identity = lambda: (seq[min(box["i"], 1)], box.__setitem__("i", box["i"] + 1))[0]
        o = _FakeOrch()
        o._window_identity_note()
        n = o._window_identity_note()

        check("identical titles" in n,
              "⭐ 明说了「同名窗口很常见」—— 否则模型会觉得标题一样就是同一个")
        check("Do NOT assume" in n, "明说了不许假设这是它打开的那个文件")
        for word in ("select-all", "delete", "overwrite", "save"):
            check(word in n, f"点名了破坏性动作：{word}")
        check("stop and ask" in n,
              "⭐ 给了兜底出路（确认不了就停下来问）—— "
              "只说「小心」而不给动作，等于什么都没说")
    finally:
        EL.foreground_identity = saved


def t_scope_is_the_turn() -> None:
    print("\n[3] 比较基准的边界 = 本轮（与活动租约同一个边界）")
    src = module_text("core.orchestrator")
    oc = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("self._last_fg_window = None" in oc, "每轮开头清零")

    i_reset = oc.find("self._last_fg_window = None")
    i_lease = oc.find("_rt_lease_release(self)", i_reset)
    check(0 < i_lease - i_reset < 400,
          "⭐ 清零点与活动租约的每轮归还点在一起 —— "
          "跨轮的「窗口变了」没有意义，那本来就是两件事之间", f"距 {i_lease - i_reset} 字符")


def t_wiring() -> None:
    print("\n[4] 接线：两条会动手的路都带上身份")
    src = module_text("core.orchestrator")
    tree = ast.parse(src)

    def _seg(name):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return ast.get_source_segment(src, n) or ""
        return ""

    look = _seg("_look_at_screen")
    check("_window_identity_note()" in look,
          "⭐ `look_at_screen` 的结果带身份")
    check("_look_at_screen_impl" in look,
          "⚠️ 用包一层的写法 —— 原函数有 5 个返回点，逐个改必然漏一个")

    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("_win_note = self._window_identity_note()" in body,
          "⭐⭐ `os_execute` 的**动作结果**里也带 —— "
          "模型可以不看屏幕就连着动手，那条路上它同样需要知道对象换了")

    # 身份必须来自 Win32，不能问视觉模型
    note = _seg("_window_identity_note")
    check("foreground_identity" in note,
          "⭐ 身份取自 Win32（`GetForegroundWindow`），是**事实**不是推断")
    check("_vision_ask" not in note,
          "⚠️⚠️ **不许**用视觉模型判身份 —— 它连这个问题都没被问过，"
          "而答错的后果是破坏性误操作")

    low = module_text("core.os_layer.executor_low")
    check("启发式" in low and "get_target_window" in low,
          "⭐ 根因函数上留了痕：它是「任务开始时」的启发式，不是每步可重问的权威")


def main() -> int:
    t_identity_uses_hwnd()
    t_warning_is_actionable()
    t_scope_is_the_turn()
    t_wiring()
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
