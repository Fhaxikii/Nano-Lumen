# -*- coding: utf-8 -*-
"""exit 工具的工具卡片必须真的到得了 UI。

═══ 这个套件在验什么 ═══

用户连着三轮报"创建 Skill 时没有工具卡片"，第三轮补了一句关键观察：
**"不是完全不出现，三个 skill 只有一个位置出现了"**。

那一次出现的其实是 **`load_tools` 的卡片**（`$加载能力:create_new_skill√`），
不是 `create_new_skill` 自己的 —— 模型那一轮恰好先调了 `load_tools`。
所以真相是：`create_new_skill` **一次都没发过卡片**，"时有时无"是错觉。

修了两次，第一次修错了半边：

  第一版：`await event_queue.put({...})`  → 实测仍然没有卡片，且**无任何异常**。
  根因：`event_queue` 的排空循环长在 `_run_react_loop` 的**工具批次那一段**
        （`while not batch_task.done(): yield event_queue.get(...)`），
        而 exit 工具在到达那段之前就路由走并 `return` 了。
        事件 put 成功，只是**永远没人取**。

  📌 判据：**「绕过主流程」连带绕过的不只是发，还有取。**
     `event_queue.put` 只在"调用方保证会 drain"的地方成立；
     在生成器自己手里，`yield` 才是无条件到达 UI 的那条路。

═══ 为什么必须用 AST 而不是跑一遍 ═══

跑通一次只能证明"这次有卡片"。这个 bug 的形状是**结构性**的
（放错了通道），所以要钉的是结构：exit 分支里不许出现 `event_queue.put`。

用法：
  py -3.10 tests\t_exit_tool_card.py
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


SRC = module_text("core.orchestrator")
TREE = ast.parse(SRC)


def _func(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _exit_branch(loop):
    """定位 `if exit_call:` 那个分支体。"""
    for n in ast.walk(loop):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                and n.test.id == "exit_call"):
            return n
    return None


def _dict_str_field(d, key):
    """从字面量 dict 里取字符串字段，取不到返回 None。"""
    if not isinstance(d, ast.Dict):
        return None
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v.value if isinstance(v, ast.Constant) else None
    return None


def _queue_puts(node):
    out = []
    for c in ast.walk(node):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "put"
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "event_queue"):
            out.append(c.lineno)
    return out


def t_structure() -> None:
    print("\n[1] ⭐ exit 分支：卡片走 yield，不许走 event_queue.put")
    loop = _func("_run_react_loop")
    check(loop is not None, "前置条件：找得到 _run_react_loop")
    if loop is None:
        return

    br = _exit_branch(loop)
    check(br is not None, "前置条件：找得到 `if exit_call:` 分支")
    if br is None:
        return

    # ⚠️ 先证明"这个分支确实是发卡片的地方"，再断言"它没用错通道"。
    #    少了前置，分支被整体删掉也会让下面那条恒真地绿。
    yields = [n for n in ast.walk(br) if isinstance(n, ast.Yield)]
    ev_names = [_dict_str_field(y.value, "event") for y in yields if y.value is not None]
    check("tool_start" in ev_names,
          "前置条件：分支里【确实 yield 了】tool_start（卡片就在这里发）",
          f"yield 的 event={sorted(set(x for x in ev_names if x))}")
    # ⚠️ tool_end **不再是字面量 yield** —— 它由 `_mk_tool_end()` 产出，
    # 因为要在多个转发点复用且只能发一次（见 [4]）。所以这里改成认那个工厂返回的
    # 字典字面量，而不是认 `yield {...}`。
    # （第一版写成 `"tool_end" in ev_names`，重构后变成假失败。）
    _dicts = [n for n in ast.walk(br) if isinstance(n, ast.Dict)]
    check(any(_dict_str_field(d, "event") == "tool_end" for d in _dicts),
          "前置条件：分支里【确实构造了】tool_end（不发 end 卡片不会定型）")

    puts = _queue_puts(br)
    check(not puts,
          "⭐ 分支里没有任何 event_queue.put —— 这条路径上没人 drain，put 等于丢弃",
          f"发现于 L{puts}" if puts else "")


def t_drain_on_exit_paths() -> None:
    print("\n[2] exit 分支把队列残留也放出去（realtime_callback 的 thinking 同样会卡住）")
    loop = _func("_run_react_loop")
    br = _exit_branch(loop) if loop else None
    if br is None:
        check(False, "前置条件：找得到 `if exit_call:` 分支")
        return

    names = {n.name for n in ast.walk(br)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("_drain_queue" in names,
          "分支内定义了 _drain_queue（非阻塞取干净，不拖慢路由）")

    calls = [c for c in ast.walk(br)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id == "_drain_queue"]
    # ⚠️ 改造前这里要求 ">= 3"：发卡片前 1 次 + 两条 async-for 转发里各 1 次。
    #    cutover 之后**五条分支合并成一个统一转发循环**，各分支自己的排空
    #    搬进了对应的 `_exit_*` 适配器（`_drain_event_queue`）——
    #    所以 runner 里只剩"发卡片前"那 1 次。
    # 📌 断言的**意图**没变（这条路径上的事件不许只进不出），换的是锚点：
    #    数量改成 ">= 1"，同时下面补一条更强的 —— 适配器那边也必须排空。
    check(len(calls) >= 1,
          "⭐ 发卡片前先排空一次（否则状态行顺序会倒过来）",
          f"实际 {len(calls)} 处")


def t_no_stranded_producers() -> None:
    """全局体检：谁往 event_queue 里塞，谁负责被 drain。

    这里不逐条判对错（`_execute_one_tool_call` 塞得最多，但它整个生命周期都在
    批次的 drain 窗口里，是合法的），只钉住一件事：
    **`_run_react_loop` 自己不许再往队列里塞** —— 它是 drain 的宿主，
    在自己手里 put 纯属绕远路，而且一旦落在 drain 窗口外就是静默丢弃。
    """
    print("\n[3] _run_react_loop 自己不再往 event_queue 里塞")
    loop = _func("_run_react_loop")
    if loop is None:
        check(False, "前置条件：找得到 _run_react_loop")
        return
    puts = _queue_puts(loop)
    check(not puts,
          "⭐ 整个函数体内 0 处 event_queue.put（它是 drain 的宿主，应该直接 yield）",
          f"发现于 L{puts}" if puts else "")

    # 前置条件：drain 确实还在（别把 put 和 drain 一起删了还全绿）
    gets = [c.lineno for c in ast.walk(loop)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr in ("get", "get_nowait")
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "event_queue"]
    check(len(gets) >= 2,
          "前置条件：批次那段的 drain 还在（否则上面那条是靠删干净蒙混过关）",
          f"drain 点 L{gets}")


def t_tool_end_before_terminal() -> None:
    """`tool_end` 必须赶在终端事件（final_result / sys_error）**之前**发。

    ═══ 第三次栽在同一张卡片上（2026-08-06）═══

      第 1 版：`event_queue.put` → 没人 drain，卡片完全不出现。
      第 2 版：紧挨 `tool_start` 就发 `tool_end` → 卡片显示 `[✓] 1 tool · 0.0s`。
      第 3 版：挪到"专属流程跑完之后" → **卡片永远停在运行态**：
               金黄色、没有秒数、明细行转圈不停、收起也不出对勾。

    📌 第 3 版的根因在**消费端**：`app.py` 里

            if step.get("event") == "final_result":
                …
                return

        子流程（探索 / 部署 / 重新生成）自己就会 yield 终端事件结束这一轮，
        消费者当场 return —— **排在它后面的任何事件都进不了 UI**。

    📌 判据：**生成器 yield 出去 ≠ 消费者会处理。**
       终端事件是一条单向门，收尾类事件必须在门关上之前发。

    这条测试钉的就是这个顺序，因为它**跑一次是看不出来的**
    （事件确实被 yield 了，只是没人接）。
    """
    print("\n[4] ⭐⭐ tool_end 排在终端事件之前（消费者读到 final_result 就 return）")
    loop = _func("_run_react_loop")
    br = _exit_branch(loop) if loop else None
    if br is None:
        check(False, "前置条件：找得到 `if exit_call:` 分支")
        return

    names = {n.name for n in ast.walk(br)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("_pre" in names,
          "⭐ 有 `_pre(ev)` —— 转发前判断是不是终端事件，是就先补 tool_end")
    check("_mk_tool_end" in names,
          "tool_end 由一个工厂函数产出（保证各处形状一致、只发一次）")

    seg = ast.get_source_segment(SRC, br) or ""
    check("_UI_TERMINAL_EVENTS" in seg,
          "⭐ 用常量表判终端事件（不再在分支里手抄事件名）")

    # ⭐⭐⭐ AST 不变量：常量表必须 == app.py 里真实会 `return` 的那组事件。
    #
    # ⚠️ 这份清单**被猜错过两次**：先只写 final_result（create_new_skill 结束在
    #    skill_preview 上 → 卡片一直转圈），补了 sys_error 之后仍然漏两个。
    #    人记不住，所以让测试去数。
    # 📌 同：**常量表必须能被证明等于真实分发链。**
    import core.orchestrator as _om
    app_src = module_text("app")
    app_tree = ast.parse(app_src)
    real: set[str] = set()
    for n in ast.walk(app_tree):
        if not (isinstance(n, ast.If) and any(isinstance(s, ast.Return) for s in n.body)):
            continue
        for cmp_ in ast.walk(n.test):
            if not isinstance(cmp_, ast.Compare):
                continue
            left, comps = cmp_.left, cmp_.comparators
            # 形状：step.get("event") == "xxx"
            if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                    and left.func.attr == "get" and left.args
                    and isinstance(left.args[0], ast.Constant)
                    and left.args[0].value == "event"
                    and comps and isinstance(comps[0], ast.Constant)):
                real.add(comps[0].value)
    check(bool(real), "前置条件：从 app.py 里确实解析出了终端事件（解析器还有效）",
          f"解析到 {sorted(real)}")
    declared = set(_om._UI_TERMINAL_EVENTS)
    check(real <= declared,
          "⭐⭐ app.py 里每一个会 return 的事件都在 _UI_TERMINAL_EVENTS 里 —— "
          "漏一个，那条路径的卡片就永远转圈",
          f"漏掉：{sorted(real - declared)}" if real - declared else "")
    check(declared <= real,
          "⚠️ 反向：常量表里没有多余项（多写了说明理解已经和现实脱节）",
          f"多余：{sorted(declared - real)}" if declared - real else "")
    check("_tool_end_sent" in seg,
          "⚠️ 有「只发一次」的标记（补发 + 兜底两条路都会走到）")

    # 每一条转发子流程事件的 `yield <var>` 之前都要有 `_pre`
    pre_calls = [c for c in ast.walk(br)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "_pre"]
    # ⚠️ 改造前是 ">= 5"：五条 `elif exit_call.name == ...` 分支各一个 `_pre`。
    #    那条断言其实在替**人**记「五个分支都别忘了」——
    #    而它防不住第六个分支被加进来时忘掉。
    # ⭐ cutover 之后只有**一个**统一转发循环，于是要守的东西变了，也变强了：
    #    「只有一条转发路径」+「那条路径上有 `_pre`」⇒ **任何 exit 工具都不可能
    #    绕过 `_pre`**，包括将来新增的。这比数 5 个调用点强。
    check(len(pre_calls) >= 1,
          "⭐ 统一转发循环里调了 _pre（终端事件之前补 tool_end）",
          f"实际 {len(pre_calls)} 处")
    _fwd_loops = [n for n in ast.walk(br) if isinstance(n, ast.AsyncFor)]
    check(len(_fwd_loops) == 1,
          "⭐⭐ exit 分支里**只有一条** `async for` 转发路径 —— "
          "🔴 改造前是 5 条（按工具名分派出来的），于是「别忘了 _pre」要靠人记 5 次；"
          "现在忘不了，因为**没有第二条路可走**",
          f"实际 {len(_fwd_loops)} 条")
    _orch_src = module_text("core.orchestrator")
    _ot = ast.parse(_orch_src)
    _adapters = {n.name for n in ast.walk(_ot)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name.startswith("_exit_")}
    check(len(_adapters) >= 5,
          "⭐ 五条分支各自的专属逻辑搬进了 `_exit_*` 适配器（公共 plumbing 留在 runner）",
          f"{sorted(_adapters)}")
    _drain_in_adapters = 0
    for n in ast.walk(_ot):
        if (isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("_exit_")
                and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "_drain_event_queue" for c in ast.walk(n))):
            _drain_in_adapters += 1
    check(_drain_in_adapters >= 2,
          "⭐ 改造前在 create_new_skill / answer_open_interaction 两条分支里的排空，"
          "**逐条**搬进了对应适配器 —— 📌 统一转发不等于统一副作用，"
          "哪条原本排空、哪条原本不排，切完必须还是那样",
          f"{_drain_in_adapters} 个适配器里有排空")

    # 兜底：流程没产出终端事件时，末尾仍要补
    check("if not _tool_end_sent:" in seg,
          "⭐ 流程没走终端事件（如 defer 那条）时，末尾兜底补一次")

    # ⚠️ 反向前置：证明消费端确实会在 final_result 处 return，
    #    否则上面整条推理不成立、这些断言就只是形式主义。
    app = module_text("app")
    app_t = ast.parse(app)
    found = False
    for n in ast.walk(app_t):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and isinstance(n.test.comparators[0], ast.Constant)
                and n.test.comparators[0].value == "final_result"
                and any(isinstance(s, ast.Return) for s in n.body)):
            found = True
    check(found,
          "⚠️ 前置条件：app.py 里确实是「收到 final_result 就 return」—— "
          "这是上面所有断言的理由；哪天它不再 return 了，这条测试要重新想")


def main() -> int:
    t_structure()
    t_tool_end_before_terminal()
    t_drain_on_exit_paths()
    t_no_stranded_producers()
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
