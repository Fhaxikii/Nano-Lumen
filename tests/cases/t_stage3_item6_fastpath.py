# -*- coding: utf-8 -*-
"""早先：删掉 SKILL_CREATE 关键词快路径 + 感知行不再被截断。

═══ 这个套件在验什么 ═══

⑥ 是一条**删除**，而删除最难验 —— "东西没了"很容易假通过。所以这里用 AST
去证明它真的不在源码里，而不是靠 grep 字符串（注释里还留着它的名字做留档）。

⑥ 的两半必须一起成立：
  · 删快路径 → "写个 skill 做 XX" 只剩元工具一个入口
  · `create_new_skill` 的感知行不被截断到 28 字符 → 模型看得清它是什么，
    才不会把一个明确的请求劝退（那是快路径当初被留下的唯一理由）

**只补前者会把"防劝退"这个理由变成真风险。**

用法：
  py -3.10 tests\cases\t_stage3_item6_fastpath.py
"""
from __future__ import annotations

import ast
import inspect
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

import core.orchestrator as orch_mod
from core.orchestrator import Orchestrator
from core.tools.manifests import _CREATE_NEW_SKILL_MANIFEST

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


# ══════════════════════════════════════════════════════════════════════════
# 1｜快路径真的不在了（AST，不是文本匹配）
# ══════════════════════════════════════════════════════════════════════════

def t_fastpath_gone() -> None:
    print("\n[1] ⭐ 关键词快路径已从源码里消失（用 AST 证明，不是 grep）")

    # 检查代码性质用 AST，不用文本匹配：注释与字符串里出现这个名字不代表代码还在用它。
    src = module_text("core.orchestrator")
    tree = ast.parse(src)
    assigned: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned.append(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            assigned.append(n.target.id)
    check(len(assigned) >= 1,"前置条件：AST 确实收集到了赋值（分析有效）", f"{len(assigned)} 个")
    check("_SKILL_CREATE_KEYWORDS" not in assigned,
          "⭐ 没有任何地方【赋值】这个常量了（真的删了）",
          f"命中: {[a for a in assigned if 'SKILL_CREATE' in a]}")

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    check("_SKILL_CREATE_KEYWORDS" not in names,
          "也没有任何地方【引用】它")


def t_single_entry() -> None:
    print("\n[2] Skill 创建只剩一个入口")
    src = inspect.getsource(Orchestrator._handle_query_impl)
    tree = ast.parse(__import__("textwrap").dedent(src))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_run_skill_exploration"
    ]
    check(not calls,
          "⭐ `_handle_query_impl` 里不再直接调 `_run_skill_exploration`",
          f"还剩 {len(calls)} 处")

    # 前置条件：证明这个函数体确实被解析到了，否则空结果会假通过
    check(len(src.splitlines()) > 200,
          "前置条件：确实解析到了 `_handle_query_impl` 的函数体",
          f"{len(src.splitlines())} 行")

    # 元工具那条入口必须还在 —— 删了快路径又没有元工具就等于砍掉功能。
    # ⚠️ 换锚点：cutover 之后 ReAct 的 exit 分支里**没有工具名**了
    #    （那五段 `elif exit_call.name == ...` 正是要删的第二份权威），
    #    入口改由「目录里 create_new_skill 解析到哪个 handler」表达。
    # 📌 断言的意图没变：**那条通路必须还在**；变的是去哪儿证明它还在。
    from core.tools import ToolScope as _TS
    from core.tools.builtin import build_builtin_definitions as _bbd
    import core.orchestrator as _om
    from core.tools.manifests import BUILTIN_MANIFESTS
    _mans = dict(BUILTIN_MANIFESTS)
    _d = {d.name: d for d in _bbd(_mans)}["create_new_skill"]
    _ref = _d.bindings[_TS.MAIN]
    check(bool(_ref), "元工具 create_new_skill 在主决策里解析得到 handler", _ref)
    # ⚠️ 2026-08-13 换锚点（第二次）：探索子循环已整体拆除，
    #    `create_new_skill` 现在**直接进代码生成**。断言的意图仍是
    #    「那条通路必须还在」，只是终点从 Explorer 换成了 SkillWriter。
    # 📌 同上：**变的是去哪儿证明它还在，不是要不要证明。**
    _src = inspect.getsource(getattr(Orchestrator, _ref))
    check("_generate_skill_with_writer" in _src,
          "⭐ 那个 handler 最终确实进代码生成（元工具入口仍然在）")
    check("_run_skill_exploration" not in _src,
          "⚠️ 且**不再**经过已删除的探索子循环（留着就是悬空引用）")


# ══════════════════════════════════════════════════════════════════════════
# 3｜感知行：判断成本高的工具不许被截断
# ══════════════════════════════════════════════════════════════════════════

def t_awareness_not_truncated() -> None:
    print("\n[3] ⭐ create_new_skill 的感知行不再被截断到 28 字符")
    # ⚠️ 换锚点：`_build_deferred_awareness` 和它的 `[:28]` 已随 cutover 删除，
    #    感知行现在是 `ToolDefinition.awareness`（必填、人工写、**不许由截断得到**）。
    # 📌 断言的意图一条没变：模型必须看得清这个工具是什么 ——
    #    ⑥ 删掉关键词快路径之后，「写个 skill 做 XX」唯一的入口就是它。
    from core.tools.builtin import build_builtin_definitions as _bbd
    import core.orchestrator as _om
    from core.tools.manifests import BUILTIN_MANIFESTS
    _mans = dict(BUILTIN_MANIFESTS)
    defs = {d.name: d for d in _bbd(_mans)}

    _body = defs["create_new_skill"].awareness
    check(bool(_body), "感知块里有 create_new_skill 这一行")
    check(len(_body) > 100,
          "⭐ 它的描述远长于 28 字符（模型看得清这工具是什么）",
          f"{len(_body)} 字符")
    check("reusable" in _body or "meta-tool" in _body,
          "⭐ 关键判据词进去了（reusable / meta-tool）—— "
          "这正是模型判断'可复用能力 vs 一次性执行'要用的信息")
    check("\n" not in _body,
          "⚠️ 感知行是**一行** —— 它进的是 system prompt 的清单，不是正文")


def t_awareness_set_is_deliberate() -> None:
    print("\n[4] 放宽是例外不是默认（感知块不许膨胀）")
    # ⚠️ 换锚点：`_AWARENESS_FULL` / `_AWARENESS_FULL_MAX` 已随 cutover 删除。
    #    改造前"哪几个工具放宽"是一张**白名单**；现在每个工具的 awareness 各自
    #    人工写死，所以要守的东西变成了：**放宽的仍然只有那几个，其余仍然很短**。
    # ⭐ 顺带比旧断言强一点：旧的只验名单长度，新的直接量**每个工具真实的那一行**。
    from core.tools.builtin import build_builtin_definitions as _bbd
    import core.orchestrator as _om
    from core.tools.manifests import BUILTIN_MANIFESTS
    _mans = dict(BUILTIN_MANIFESTS)
    defs = {d.name: d for d in _bbd(_mans)}

    _long = sorted(n for n, d in defs.items() if len(d.awareness) > 120)
    check("create_new_skill" in _long, "create_new_skill 属于放宽的那几个（⑥ 的安全网）")
    check(len(_long) <= 5, "放宽的很少 —— 例外不是默认", f"{len(_long)} 个: {_long}")
    _max = max(len(d.awareness) for d in defs.values())
    check(_max <= 400,
          "但仍有上限 —— 不是把整份 description 塞进感知块", f"最长 {_max} 字符")
    _short_ok = sum(1 for d in defs.values() if len(d.awareness) <= 90)
    check(_short_ok >= len(defs) - 5,
          "⭐ 绝大多数工具的感知行仍然是一句短话（感知块不膨胀）",
          f"{_short_ok}/{len(defs)} 个 ≤ 90 字符")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("删关键词快路径 + 感知行不截断")
    print("=" * 74)
    for fn in (t_fastpath_gone, t_single_entry,
               t_awareness_not_truncated, t_awareness_set_is_deliberate):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")

    passed = sum(1 for ok, _, _ in _results if ok)
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
