# -*- coding: utf-8 -*-
"""「回复这条」的生命周期 + 待审草稿活过重启。

═══ ⑤ 发出消息后自动复位 ═══

用户的理由："模型已经被提醒过一次就够了，而且用户想聊别的时很容易忘记取消。"

复位放在 `handle_query` 的 **`finally`** 里 —— 正常结束、撞预算上限、抛异常，
三条出口都算"这一轮已经消费掉这个指向"。漏掉任何一条它就会粘到下一轮，
而"粘住"正是这个 bug 本身。

⚠️ 连带必须做的两件事，少一件 ⑤ 就是假的：

  1. **所有权收归 orchestrator 一份。** 原来 app 和 orchestrator 各存一份靠调用
     同步。撑不住 ⑤：复位发生在轮次结束，那一刻 UI 完全没参与，它那份会停在旧值。
     这里的双权威不是"可能不同步"，是**注定不同步**。
  2. **`_reply_target` 必须进重画指纹。** 它决定按钮显示「回复这条」还是
     「取消引用」，就是画面的一部分。指纹不含它 → 定时器一直短路 →
     按钮永远停在「取消引用」→ 复位了个寂寞。
     📌 判据：**凡是影响渲染结果的输入，都必须在指纹里。**

═══ ⑥ 点击不要等 1.5 秒 ═══

点 replay / 翻页原来只把指纹置空，真正重画要等下一次 `ui.timer(1.5, …)`。
⚠️ 但**不能直接调重画** —— 此刻正在那张卡片某个按钮的点击回调里，
而重画第一件事是 `card.clear()`，等于处理点击时把这个按钮删掉。
推迟一拍（10ms）。

═══ 待审草稿活过重启 ═══

实测：重启后三张待审卡还在，但**没有 `<>` 按钮**，看不了也部署/丢弃不了。
交互是 PERSISTED（存库），`_pending_skills` 却是纯内存。

⚠️ 这不是少存一个字段，是**耐久性承诺自相矛盾**：审计刻意不设 deadline，
理由是"用户可能真想放三天再看代码"—— 这个理由只有在代码也活到三天后才成立。

📌 判据：**一条持久记录不能指向一个易失的东西。**

用法：
  py -3.10 tests\t_u7_reply_lifecycle.py
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

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


APP = pathlib.Path("app.py").read_text(encoding="utf-8")
ORCH = pathlib.Path("core/orchestrator.py").read_text(encoding="utf-8")
APP_T, ORCH_T = ast.parse(APP), ast.parse(ORCH)


def _func(tree, src, name, owner=None):
    """按名字取函数源码。`owner` 给定时**限定在那个类里找**。

    ⚠️⚠️ 不给 `owner` 时取的是 `ast.walk` 遇到的**第一个**同名函数 ——
       而 `__init__` 这种名字在一个模块里可能有很多个。
    🔴 在 `orchestrator.py` 开头新增了 `_ToolRuntimeView` 类之后，
       它的 `__init__` 就排在了 `Orchestrator.__init__` 前面，于是本文件里
       那条「`_rt_restore_pending_skills` 必须在 `__init__` 末尾被调用」
       **跑去一个不相干的类里找**，从此一直是红的
       （生产代码那一行好好地在 `Orchestrator.__init__` 里）。
    📌 **一条断言如果靠「碰巧是第一个」定位目标，它的正确性就取决于
       别人有没有在它前面加东西。** 加 `owner` 之后它定位的是真正要查的那个。
    """
    for n in ast.walk(tree):
        if owner is not None:
            if not (isinstance(n, ast.ClassDef) and n.name == owner):
                continue
            for sub in n.body:
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name == name):
                    return sub, (ast.get_source_segment(src, sub) or "")
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n, (ast.get_source_segment(src, n) or "")
    return None, ""


def t_reset_in_finally() -> None:
    print("\n[1] ⭐⭐ ⑤ 复位在 handle_query 的 finally 里（三条出口都覆盖）")
    fn, seg = _func(ORCH_T, ORCH, "handle_query")
    check(fn is not None, "前置条件：找得到 handle_query")
    if fn is None:
        return

    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    check(bool(tries), "handle_query 里有带 finally 的 try")

    def _clears(nodes):
        for n in nodes:
            for c in ast.walk(n):
                if (isinstance(c, ast.Assign)
                        and any(isinstance(t, ast.Attribute) and t.attr == "_reply_target"
                                for t in c.targets)
                        and isinstance(c.value, ast.Constant) and c.value.value is None):
                    return True
        return False

    check(any(_clears(t.finalbody) for t in tries),
          "⭐ finally 里把 _reply_target 置 None —— 正常/超预算/异常三条出口都算消费过")

    # ⚠️ 反向前置：证明它不是在别处也被随便清（那样这条断言没有意义）
    body_clears = _clears([n for t in tries for n in t.body])
    check(not body_clears,
          "⚠️ try 主体里【没有】另一处复位（否则 finally 那条只是巧合）")


def t_single_owner() -> None:
    print("\n[2] ⭐ 所有权只有 orchestrator 一份，app 侧是只读视图")
    # app 侧不许再有 `self._reply_target = ...`
    assigns = [n.lineno for n in ast.walk(APP_T)
               if isinstance(n, ast.Assign)
               for t in n.targets
               if isinstance(t, ast.Attribute) and t.attr == "_reply_target"
               and isinstance(t.value, ast.Name) and t.value.id == "self"]
    check(not assigns,
          "⭐ app.py 里没有 `self._reply_target = …`（不存第二份）",
          f"发现于 L{assigns}" if assigns else "")

    # 且它是个 property
    prop = next((n for n in ast.walk(APP_T)
                 if isinstance(n, ast.FunctionDef) and n.name == "_reply_target"), None)
    check(prop is not None, "app.py 里 _reply_target 是个方法/属性")
    check(prop is not None and any(
        isinstance(d, ast.Name) and d.id == "property" for d in prop.decorator_list),
        "⭐ 用 @property 声明成只读视图（写只能走 _set_reply_target）")

    # setter 写的是 agent 那份
    _, seg = _func(APP_T, APP, "_set_reply_target")
    check("self.agent._reply_target" in seg,
          "_set_reply_target 写的是 agent 上那份权威副本")


def t_fingerprint_includes_target() -> None:
    print("\n[3] ⭐⭐ _reply_target 进重画指纹（否则 ⑤ 复位后界面不变）")
    fn, seg = _func(APP_T, APP, "refresh_pinned_interactions")
    check(fn is not None, "前置条件：找得到 refresh_pinned_interactions")
    if fn is None:
        return
    check("snap" in seg and "_pinned_snapshot" in seg,
          "前置条件：这个函数确实用指纹做短路")
    i_snap = seg.index("snap =")
    i_cmp = seg.index("== getattr(self, \"_pinned_snapshot\"")
    tail = seg[i_snap:i_cmp]
    check("_reply_target" in tail,
          "⭐ 指纹在【比较之前】就把 _reply_target 算进去了",
          "指纹片段里没有它" if "_reply_target" not in tail else "")


def t_immediate_redraw() -> None:
    print("\n[4] ⑥ 点击立即重画，且【不是】在回调里直接 clear")
    fn4, seg = _func(APP_T, APP, "_redraw_pinned_now")
    check(bool(seg), "存在 _redraw_pinned_now")
    check("ui.timer" in seg and "once=True" in seg,
          "⭐ 推迟一拍再重画（当前回调所在的按钮会被 card.clear() 删掉）")

    # ⚠️ 这条**必须用 AST**。第一版写成 `"refresh_pinned_interactions(" not in seg`，
    # 被 docstring 里那句"不能在这里直接调 `refresh_pinned_interactions()`"命中，
    # 假失败 —— 正是 那条"检查代码性质要用 AST，不要文本匹配"的现场重演。
    direct = [c.lineno for c in ast.walk(fn4)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
              and c.func.attr == "refresh_pinned_interactions"]
    check(not direct,
          "⚠️ 没有【直接调用】重画（只把函数对象交给 timer）",
          f"直接调用于 L{direct}" if direct else "")

    for caller in ("_set_reply_target", "_flip"):
        src = seg
        if caller == "_flip":
            # _flip 是嵌套函数，从整份源码里找
            i = APP.index("def _flip(step: int):")
            src = APP[i:i + 400]
        else:
            _, src = _func(APP_T, APP, caller)
        check("_redraw_pinned_now" in src, f"{caller} 会触发立即重画")


def t_draft_survives_restart() -> None:
    print("\n[5] ⭐⭐ 待审草稿落盘 + 启动恢复；捞不回来的当场收掉")
    _, seg = _func(ORCH_T, ORCH, "_rt_open_skill_audit")
    check('"draft": orch._get_pending_skill(filename)' in seg,
          "⭐ 登记审计时把【整份载荷】写进 payload（不只是 code —— "
          "部署还要 spec_side_effects / lifecycle / error_context）")

    fn, seg2 = _func(ORCH_T, ORCH, "_rt_restore_pending_skills")
    check(fn is not None, "存在 _rt_restore_pending_skills")
    if fn is None:
        return
    check("_put_pending_skill" in seg2, "有 draft 的 → 放回 _pending_skills")
    check("INTERRUPTED_BY_RESTART" in seg2,
          "⭐ 没 draft 的孤儿 → 收掉，且理由是 INTERRUPTED_BY_RESTART "
          "（不是 USER_CANCELLED —— 用户没取消任何东西）")
    check('draft.get("code")' in seg2,
          "⚠️ 判据是「真有代码」，不是「payload 里有 draft 这个键」")

    # 必须在 __init__ 里被调用，否则永远不生效
    init, iseg = _func(ORCH_T, ORCH, "__init__", owner="Orchestrator")
    check("_rt_restore_pending_skills(self)" in iseg,
          "⭐ __init__ 末尾调用（要等 _pending_skills 建好）")


def main() -> int:
    t_reset_in_finally()
    t_single_owner()
    t_fingerprint_includes_target()
    t_immediate_redraw()
    t_draft_survives_restart()
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
