# -*- coding: utf-8 -*-
"""bridge 交接 + L2→L3 + 索引条目无条件注入。

═══ 这一层最重要的一条：**提交顺序** ═══

    产生可召回内容 → 持久化 → 验证确实检索得到 → 生成索引条目 → 最后才 commit L3
    任何一步失败 → **继续留在 L2**

🔴 绝不能「内容先移出 → 再写语义记忆 → bridge 失败 → 内容没了、召回也没有」。
📌 **fail-closed 的方向由「失败时谁受损」决定**：
   这里失败的代价是"多背一会儿上下文"（便宜、可逆），
   反方向失败的代价是"内容永久消失且没人知道"（不可逆）。

其余三条：
  ② **新的 `memory_type="exchange"`**，不复用 `task_pattern`/`correction` ——
     硬塞会让现有那两条召回腿开始返回对话摘要（一个正在工作的机制被污染）。
  ③ **索引条目无条件注入 system** —— 不是"需要时再检索"。
     🔴 它解掉的是 用户那个悖论：「真忘了之后，不就把『我该去看笔记』也忘了吗」。
  ④ **两条腿一 fail-closed 一 best-effort**：SQLite 写不进去就整体失败；
     向量腿失败只降级，但**必须响亮**。

用法：
  py -3.10 tests\t_f5_bridge_l3.py
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

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


_D = {"status": "done", "outcome": "把 RAG 归档公式改成乘法版",
      "referents": ["core/rag.py"], "open_items": ["等实测标定"]}


def t_index_line() -> None:
    print("\n[1] ⭐⭐ 索引条目：一行、带定位、有上限")
    from core.context import bridge as B
    line = B.index_line(_D, 1786600000)
    check("\n" not in line, "⭐ 就一行（它要几十条一起进 system）")
    check("core/rag.py" in line,
          "⭐⭐ **带定位信息** —— 📌 索引条目写砸 = 那条记忆等于不存在")
    check("recall" in line, "明说可以 recall")
    _long = dict(_D, outcome="长" * 500)
    check(len(B.index_line(_long)) <= B.MAX_INDEX_CHARS + 20,
          "⭐ 有长度上限 —— 📌 一个「小到可以一直背着」的东西，"
          "一旦不小了它就不再是索引而是负担", str(len(B.index_line(_long))))


def t_canonical_keeps_user_words() -> None:
    print("\n[2] ⭐⭐ 交接正文带上用户原话")
    from core.context import bridge as B
    t = B.canonical_text(_D, "就按乘法版改吧，别再改了")
    check("就按乘法版改吧" in t,
          "⭐⭐ **用户原话在里面** —— 📌 它是唯一不可再生的那部分，"
          "到了这一层再丢就真的没有了")
    check("core/rag.py" in t and "等实测标定" in t, "结论/涉及/未决都在")
    check(len(B.canonical_text(_D, "长" * 5000)) < 2000,
          "⚠️ 但要截断 —— 这是「能被检索到的载体」，不是原文备份"
          "（原文备份在 conversation_messages，永远都在）")


def t_memory_type_is_new() -> None:
    print("\n[3] ⭐⭐⭐ ② 用新的 `exchange` 类型，不污染现有两条召回腿")
    from core.context import bridge as B
    check(B.MEMORY_TYPE == "exchange", "类型是 exchange", B.MEMORY_TYPE)
    check(B.MEMORY_TYPE not in ("task_pattern", "correction"),
          "⭐⭐⭐ **没有复用** `task_pattern` / `correction` —— "
          "🔴 硬塞会让 `semantic_memory.py` 里那条按 task_pattern 检索的召回腿"
          "**开始返回对话摘要**，一个正在正常工作的机制被污染，而且不报错")

    src = pathlib.Path("core/context/bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "hand_off"), None)
    seg = (ast.get_source_segment(src, fn) or "") if fn else ""
    # ⚠️⚠️ **走 AST 查关键字参数，不看源码文本。**
    #    第一版写成「源码里不许出现 wrong_assumption」，可 `hand_off` 的注释
    #    正在解释"为什么不填它" —— 于是断言被那段解释喂红了。
    #    🔴 **这是同一个形状的第四次**（源码换行 / 关键词缺席 / docstring / 注释）。
    #    📌 **只要断言读的是文本，对代码的【解释】就会参与判定。**
    _call = next((c for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                  and c.func.attr == "add_semantic_memory"), None)
    _kw = {k.arg for k in (_call.keywords if _call else [])}
    check(_call is not None and not (_kw & {"wrong_assumption", "correct_behavior",
                                            "correction_subtype"}),
          "⭐⭐ 交接时**不填** `wrong_assumption` / `correct_behavior` / "
          "`correction_subtype` —— 📌 一次普通对话不是一次「纠错」，"
          "硬填就是让 schema 编事实", str(sorted(_kw)))
    check("condition_text=" in seg,
          "⚠️ `condition_text` 显式给了值（那张表的规矩：不许真空）")

    # recall 也必须限定类型
    fr = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "recall"), None)
    segr = (ast.get_source_segment(src, fr) or "") if fr else ""
    check("memory_type=MEMORY_TYPE" in segr,
          "⭐ `recall` 限定只搜 exchange —— "
          "📌 不限定会把另外两类一起捞出来，而它们回答的是另外两个问题")


def t_commit_order_is_fail_closed() -> None:
    print("\n[4] ⭐⭐⭐ ① 提交顺序：验证通过才 commit，失败一律留 L2")
    src = pathlib.Path("core/context/decay.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "run_l2_to_l3"), None)
    check(fn is not None, "找得到 `run_l2_to_l3`")
    seg = (ast.get_source_segment(src, fn) or "") if fn else ""

    i_hand = seg.find("hand_off(")
    i_line = seg.find("index_line(")
    i_rec = seg.find("level=L3")
    check(0 < i_hand < i_line < i_rec,
          "⭐⭐⭐ 顺序是 **交接 → 生成索引 → 最后才 commit L3** —— "
          "🔴 反过来就是「内容没了、召回也没有」",
          f"hand_off@{i_hand} < index_line@{i_line} < commit@{i_rec}")
    check(seg.count("stat[\"failed\"] += 1") >= 4,
          "⭐⭐ 每一步失败都有各自的出口（都 `continue`，保持 L2）",
          f"{seg.count('stat[chr(34)failed]')}")
    # ⚠️ 只降 closed + 不碰 active
    check("exchanges[:-1]" in seg, "⚠️ active exchange 排除在外")
    # ⚠️ 陈旧的 L2 不许交接
    # ⚠️ 2026-08-14 起改成走 `active_entries`（陈旧的**根本不在返回值里**），
    #    比原来在这里各判一次 `is_stale` 更强：
    #    📌 「每处都有 stale 逻辑」不等于「stale 被统一消费」——
    #       前者是三份各自正确的判断，后者才是一个权威。
    check("is_stale" in seg or "active_entries" in seg,
          "⭐⭐ 陈旧的 L2 **不许交接**（现在由唯一的读出口 `active_entries` 保证）—— "
          "📌 拿一个过期 digest 去生成永久的语义记忆，错误就固化了")
    # ⚠️ 行为侧的证据在 `t_f5_live.py` —— 那里真的调 `project_exchange`。


def t_bridge_two_legs() -> None:
    print("\n[5] ⭐⭐ ④ 两条腿：SQLite fail-closed / 向量 best-effort 但响亮")
    src = pathlib.Path("core/context/bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "hand_off"), None)
    seg = (ast.get_source_segment(src, fn) or "") if fn else ""

    _sq = seg.find("add_semantic_memory")
    _rb = seg.find("get_semantic_memory")
    _vec = seg.find("memory_index")
    check(0 < _sq < _rb < _vec,
          "⭐ 顺序：写 SQLite → **回读校验** → 才动向量库",
          f"{_sq}<{_rb}<{_vec}")
    check("return None" in seg[_sq:_vec],
          "⭐⭐⭐ SQLite 那两步失败 → `return None`（整体失败，留 L2）")
    check("return None" not in seg[_vec:],
          "⭐⭐ 向量腿失败**不返回 None** —— "
          "📌 L3 索引条目本身就是一条召回路径，它不依赖向量；"
          "向量挂了只是联想召回变弱，不是这段记忆没了")
    check("warning" in seg[_vec:],
          "⚠️ 但**必须响亮** —— 📌 一个安静降级的召回腿，"
          "会让人以为召回率天生就这样")
    check("检索不到自己" in seg,
          "⭐⭐ 连「写进去了但搜不到自己」这种情况也告警 —— "
          "📌 「写成功」和「找得到」是两件事")


def t_index_injected_unconditionally() -> None:
    print("\n[6] ⭐⭐⭐ ③ 索引条目**无条件注入** system")
    src = pathlib.Path("core/orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_l3_index_block"), None)
    check(fn is not None, "存在 `_l3_index_block`")
    seg = (ast.get_source_segment(src, fn) or "") if fn else ""
    check("_l3_index_block()" in src,
          "⭐⭐ 它确实被拼进 system_guide —— 📌 写了没人调，和没写一模一样")
    check("Do NOT claim you never" in seg,
          "⭐⭐⭐ 明令**不许说「我们从没讨论过」** —— "
          "同 [D10] 图片占位符那条：不许让模型自我否定")
    check("recall it if the current question needs it" in seg,
          "⭐ 说清用法：先知道存在，需要时再 recall")
    check('if not lines_:' in seg and 'return ""' in seg,
          "⚠️ 一条都没有时**一个字都不注入** —— "
          "📌 每轮都在、又暂时没内容的标题会被模型学成背景噪音")

    # ⚠️⚠️ 它不该判开关（`ladder_enabled` 是**单向迁移开关**，读侧一律不判）。
    #    🔴 第一版写的是 `"ladder_enabled" not in seg` —— 2026-08-14 给这个函数
    #       补了一段解释"为什么刻意不判开关"的 docstring，这条断言**被那段说明
    #       自己喂红了**。这是本项目同一形状的第五次。
    #    📌 **只要断言读的是文本，对代码的【解释】就会参与判定。**
    #    ⭐ 改成走 AST 找**调用**：注释怎么写都不会产生一个 Call 节点。
    _calls = {getattr(c.func, "id", "") or getattr(c.func, "attr", "")
              for c in ast.walk(fn) if isinstance(c, ast.Call)} if fn else set()
    check("ladder_enabled" not in _calls,
          "⚠️ 不重复判总开关（查的是**调用**不是文本）—— "
          "📌 读侧多加一个条件，就多一个「模型侧、UI 侧、投影侧三个答案」的机会",
          str(sorted(x for x in _calls if x)))


def main() -> int:
    t_index_line()
    t_canonical_keeps_user_words()
    t_memory_type_is_new()
    t_commit_order_is_fail_closed()
    t_bridge_two_legs()
    t_index_injected_unconditionally()
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
