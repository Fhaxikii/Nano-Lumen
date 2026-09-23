# -*- coding: utf-8 -*-
"""上屏的东西，重启后还在不在 —— 必须是个**被回答过**的问题。

═══ 这个套件在验什么（2026-08-13 实测，用户）═══

🔴 Nano 画了一张流程图，重启后 **图没了，描述那张图的话还在** ——
   气泡里只剩 `[✓] used 2 tools` 和「就是这样——三个盒子从上到下」，
   它在描述一张不存在的图。

⭐ 根因不是"没存"：`render_visual` 的 `html`/`title` 就在工具调用的 `args` 里，
   而 args 是逐字落盘的。**是重放侧没去读那一格。**

📌 **落盘完整 ≠ 重放完整**，两者之间隔着一段手写的投影代码。

用户当场把问题问大了一圈：
  > 「未来新加某些输出内容（类型）然后重启后不存在 —— 这种 bug 也挺隐蔽的」

⭐ 它的形状是：**直播链是一长串 `if step.get("event") == X`，
   重放链是另一段手写代码，两条链之间没有任何东西强迫它们对齐。**
   加事件只需要改直播那条；重放那条不报错、不警告，只是安静地少画一样东西，
   而且要等到有人重启并恰好回看那一段才会发现。

═══ 为什么必须用 AST，而不是"重启一次看看" ═══

重启一次只能证明**这一次**这张图在。这个 bug 是**结构性的**（两条链各写各的），
所以要钉的是结构：**直播链里的每一个事件名，都必须被四张分类表之一认领。**
新加一个事件 → 谁都没认领 → 这条测试当场红，逼人回答"它重启后还在不在"。

📌 同 `_UI_TERMINAL_EVENTS` / 那条判据：
   **常量表必须能被证明等于真实分发链，而不是靠人记得同步。**

用法：
  py -3.10 tests\t_replay_chat_events.py
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
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


APP_SRC = module_text("app")
APP_TREE = ast.parse(APP_SRC)


def _event_names(test_node) -> list[str]:
    """从 `step.get("event") == "x"` / `step.get("event") in ("a","b")` 里取事件名。"""
    out: list[str] = []
    for c in ast.walk(test_node):
        if not isinstance(c, ast.Compare):
            continue
        left = c.left
        if not (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == "get" and left.args
                and isinstance(left.args[0], ast.Constant)
                and left.args[0].value == "event"):
            continue
        for cp in c.comparators:
            if isinstance(cp, ast.Constant) and isinstance(cp.value, str):
                out.append(cp.value)
            elif isinstance(cp, (ast.Tuple, ast.List, ast.Set)):
                out += [e.value for e in cp.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return out


def _consume_loop():
    """UI 的事件消费循环：`async for step in _stream:`。"""
    for n in ast.walk(APP_TREE):
        if (isinstance(n, ast.AsyncFor) and isinstance(n.target, ast.Name)
                and n.target.id == "step"):
            return n
    return None


def _dispatched_events() -> set[str]:
    out: set[str] = set()
    loop = _consume_loop()
    if loop is None:
        return out
    for stmt in loop.body:
        if isinstance(stmt, ast.If):
            out.update(_event_names(stmt.test))
    return out


def t_every_event_is_classified() -> None:
    print("\n[1] ⭐⭐⭐ 直播链里的每个事件都被认领：重启后还在不在，必须有人答过")
    loop = _consume_loop()
    check(loop is not None, "前置条件：找得到 UI 事件消费循环 `async for step in _stream`")
    if loop is None:
        return

    real = _dispatched_events()
    # ⚠️ 先证明解析器还有效 —— 少了这条，分发链被重构成别的形状时
    #    `real` 会变成空集，下面那条"全被认领"会**恒真地绿**。
    check(len(real) >= 20,
          "前置条件：确实解析出了成规模的事件分发（解析器没失效）",
          f"解析到 {len(real)} 个")

    import app as _app
    declared = (_app._CHAT_EVENTS_REPLAYED | _app._CHAT_EVENTS_EPHEMERAL
                | _app._CHAT_EVENTS_INTERACTION | _app._CHAT_EVENTS_NOT_IN_CHAT)
    missing = real - declared
    check(not missing,
          "⭐⭐⭐ 每个上屏事件都在四张分类表之一里 —— "
          "漏一个，就是「新加的输出重启后安静消失」那个 bug 的下一次",
          f"没人认领：{sorted(missing)}" if missing else f"{len(real)} 个全部有归属")

    extra = declared - real
    check(not extra,
          "⚠️ 反向：分类表里没有多余项（多写了说明这份理解已经和现实脱节）",
          f"多余：{sorted(extra)}" if extra else "")


def t_categories_are_disjoint() -> None:
    print("\n[2] 四张表互不重叠（一个事件只能有一个归属）")
    import app as _app
    tables = {
        "REPLAYED": _app._CHAT_EVENTS_REPLAYED,
        "EPHEMERAL": _app._CHAT_EVENTS_EPHEMERAL,
        "INTERACTION": _app._CHAT_EVENTS_INTERACTION,
        "NOT_IN_CHAT": _app._CHAT_EVENTS_NOT_IN_CHAT,
    }
    names = list(tables)
    dupes = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ov = tables[names[i]] & tables[names[j]]
            if ov:
                dupes.append(f"{names[i]}∩{names[j]}={sorted(ov)}")
    check(not dupes, "⭐ 无事件同时落在两张表里", "; ".join(dupes))


def t_visual_replay_path() -> None:
    """`visual_render` 被声明成 REPLAYED —— 那就必须真的有人去画它。

    ⚠️ 这条是上面那张表的**兑现检查**：把事件填进 REPLAYED 只是一句声明，
       声明不会让图出现。所以这里钉的是「重放代码里确实有那条路径」。
    """
    print("\n[3] ⭐⭐ 声明成 REPLAYED 的可视化，重放侧确实有画它的代码")
    import app as _app
    check("visual_render" in _app._CHAT_EVENTS_REPLAYED,
          "前置条件：visual_render 声明为「重启后能重建」")

    fn = None
    for n in ast.walk(APP_TREE):
        if isinstance(n, ast.FunctionDef) and n.name == "_replay_visual_artifacts":
            fn = n
    check(fn is not None, "⭐ 存在 `_replay_visual_artifacts`（重放侧的画图入口）")

    # 它必须真的被 `_replay_durable_conversation` 调到 —— 写了没人调等于没写。
    caller = None
    for n in ast.walk(APP_TREE):
        if isinstance(n, ast.FunctionDef) and n.name == "_replay_durable_conversation":
            caller = n
    check(caller is not None, "前置条件：找得到 `_replay_durable_conversation`")
    if caller is not None:
        called = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "_replay_visual_artifacts"
                     for c in ast.walk(caller))
        check(called,
              "⭐⭐ 重放主循环里确实调用了它 —— 📌 写了没人调，和没写一模一样")

    if fn is not None:
        seg = ast.get_source_segment(APP_SRC, fn) or ""
        check("_REPLAYABLE_VISUAL_TOOL" in seg,
              "⚠️ 用常量认工具名，不在重放代码里手抄字符串")
        check("is_error" in seg,
              "⚠️ 跳过 is_error 的那次 —— 闸（健康闸 / TOOL_NOT_ACTIVE）拦下时 "
              "handler 没跑过，事件从未发出，用户当时就没看见")


def t_visual_tool_name_still_exists() -> None:
    """工具改了名而重放没跟着改 → 表现正好是这次这个 bug（安静地少画一张图）。"""
    print("\n[4] ⭐ `render_visual` 这个名字在工具目录里还真的存在")
    import app as _app
    try:
        from core.tools.builtin import BUILTIN_TOOLS  # type: ignore
        names = {getattr(d, "name", None) for d in BUILTIN_TOOLS}
    except Exception:
        # 目录的导出名可能变；退回到源码认领（仍然能抓到"改名"这件事）
        src = module_text("core.tools.builtin")
        names = set()
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "D" and n.args
                    and isinstance(n.args[0], ast.Constant)):
                names.add(n.args[0].value)
    check(bool(names), "前置条件：读得到内置工具名清单", f"{len(names)} 个")
    check(_app._REPLAYABLE_VISUAL_TOOL in names,
          "⭐ `_REPLAYABLE_VISUAL_TOOL` 指向一个真实存在的工具 —— "
          "它对不上时，重放会安静地少画一张图（正是本次的 bug 形状）",
          f"找的是 {_app._REPLAYABLE_VISUAL_TOOL!r}")


def t_args_are_durable() -> None:
    """整条修复的地基：工具调用的 `args` 真的被逐字落盘了。

    ⚠️ 没有这条，前面所有断言都建立在一个**没验证过的前提**上 ——
       万一哪天 args 改成只存摘要，重放照样画不出图，而上面全绿。
    """
    print("\n[5] ⚠️ 地基：工具调用的 args 逐字进落盘（重放能拿到 html 的唯一理由）")
    src = module_text("core.runtime.conversation")
    tree = ast.parse(src)
    found_ser, found_de = False, False
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if (isinstance(k, ast.Constant) and k.value == "args"
                        and isinstance(v, ast.Attribute) and v.attr == "args"):
                    found_ser = True
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "ToolCall"):
            found_de = True
    check(found_ser, "⭐ 序列化侧把 `call.args` 原样写进落盘 payload")
    check(found_de, "⭐ 反序列化侧把它还原成 ToolCall（args 回得来）")


def main() -> int:
    t_every_event_is_classified()
    t_categories_are_disjoint()
    t_visual_replay_path()
    t_visual_tool_name_still_exists()
    t_args_are_durable()
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
