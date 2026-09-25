# -*- coding: utf-8 -*-
"""「一次交换」—— 阶梯的单位。

═══ 这个套件在验什么 ═══

L2 的定义是「**一次交换**压成一条结论行」。而在这之前，「一次交换」在代码里
**没有任何载体**，被临时算过三遍（`_truncate_safely` / 重放 / 搜索的 DOM 扫描）。

📌 **没有单位就没有阶梯。**

三条最有价值的不变量：
  ① **切法与旧 `_truncate_safely` 逐位一致** —— 收编一个已有实现时，
     唯一能证明"没改坏"的方式是**拿旧算法对拍**，不是"看起来一样"。
  ② **前导残段不许并进后面那次交换** —— 并进去会让那次交换的「用户那句」
     名不副实，而 L2 结论行、L3 索引条目正是靠那句话写摘要的。
  ③ **系统注记算进交换体积** —— 「用户看不看得见」和「它占不占上下文」
     是两个问题，`visible_to_user` 只回答前一个。

用法：
  py -3.10 tests\cases\t_f5_exchange.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def M(role, content="x", **kw):
    from core.schema import ChatMessage
    return ChatMessage(role=role, content=content, **kw)


def t_basic_split() -> None:
    print("\n[1] ⭐ 基本切分：每遇到一条 user 就开一次新交换")
    from core.context.exchange import split

    msgs = [M("user", "问1"), M("assistant", "答1"),
            M("user", "问2"), M("tool_calls"), M("tool_results"), M("assistant", "答2"),
            M("user", "问3")]
    ex = split(msgs)
    check(len(ex) == 3, "切出 3 次交换", f"{len(ex)}")
    check([len(e.messages) for e in ex] == [2, 4, 1],
          "⭐ 工具往返归属于**发起它的那次交换**（不自成一段）",
          str([len(e.messages) for e in ex]))
    check([e.user_message.content for e in ex] == ["问1", "问2", "问3"],
          "每次交换的「用户那句」都对得上")
    check([e.index for e in ex] == [0, 1, 2] and ex[1].start == 2 and ex[1].end == 6,
          "序号与下标区间正确", f"{ex[1].start}..{ex[1].end}")
    check(split([]) == [], "空列表 → 空结果（不炸）")


def t_orphan_head() -> None:
    """⚠️ 历史被截断后，开头可能不是 user。"""
    print("\n[2] ⭐⭐ 前导残段：**不许**并进后面那次交换")
    from core.context.exchange import split

    msgs = [M("assistant", "上一轮的尾巴"), M("tool_results"),
            M("user", "新问题"), M("assistant", "新回答")]
    ex = split(msgs)
    check(len(ex) == 2, "切出 2 段（残段 + 一次完整交换）", f"{len(ex)}")
    check(ex[0].is_orphan and ex[0].user_message is None,
          "⭐ 第一段是 orphan（没有用户开头）")
    check(not ex[1].is_orphan and ex[1].user_message.content == "新问题",
          "⭐⭐ 后面那次交换的「用户那句」**没有被残段污染** —— "
          "🔴 并进去的话它就名不副实了，而 L2 结论行 / L3 索引条目"
          "正是靠那句话写摘要的")
    check(len(ex[0].messages) == 2 and len(ex[1].messages) == 2,
          "两段各自完整，没有消息丢失")

    # 全是非 user 的一串 → 整个是一个 orphan
    only = split([M("assistant"), M("assistant")])
    check(len(only) == 1 and only[0].is_orphan, "整串都没有 user → 一个 orphan 段")


def t_equivalence_with_legacy_truncate() -> None:
    """⭐⭐⭐ 收编一个已有实现时，唯一能证明"没改坏"的是**拿旧算法对拍**。"""
    print("\n[3] ⭐⭐⭐ 切点与旧 `_truncate_safely` 的算法**逐位一致**")
    from core.context.exchange import user_cut_points

    def legacy(msgs):
        return [i for i, m in enumerate(msgs) if m.role == "user"]

    rnd = random.Random(20260814)
    roles = ["user", "assistant", "tool_calls", "tool_results", "tool", "model"]
    bad = 0
    for _ in range(400):
        msgs = [M(rnd.choice(roles)) for _ in range(rnd.randint(0, 24))]
        if user_cut_points(msgs) != legacy(msgs):
            bad += 1
    check(bad == 0, "⭐⭐⭐ 400 组**普通消息**序列，新旧切点完全相同",
          f"{bad} 组不一致")

    # ⚠️⚠️ **但对系统注记，新旧【刻意】不同** —— 旧算法把它也当切点，
    #    于是截断会切在一条用户从没说过的消息上。那本来就是 bug，只是没人看得见。
    # 📌 收编一个旧实现时，如果发现旧的那个是错的，
    #    **要的是「修正 + 说清差异」，不是「为了对拍绿而把错抄过来」。**
    _sn = [M("user", "真问题"), M("assistant", "答"),
           M("user", "[System check-in]", visible_to_user=False)]
    check(legacy(_sn) == [0, 2] and user_cut_points(_sn) == [0],
          "⭐⭐⭐ 系统注记那一格：旧算法切两刀、新的只切一刀 —— "
          "**这个差异是修正，不是回归**",
          f"旧={legacy(_sn)} 新={user_cut_points(_sn)}")

    # ⚠️ 前置：证明这个对拍不是恒真（旧算法确实会产出非空切点）
    _m = [M("assistant"), M("user"), M("assistant"), M("user")]
    check(legacy(_m) == [1, 3] == user_cut_points(_m),
          "⚠️ 前置：对拍有真实内容（不是两边都返回空）")

    # ⭐ 真正接线了吗
    src = module_text("memory.manager")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "_truncate_safely"), None)
    check(fn is not None, "前置条件：找得到 `_truncate_safely`")
    if fn is not None:
        called = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                     and c.func.id == "user_cut_points" for c in ast.walk(fn))
        check(called, "⭐⭐ `_truncate_safely` **真的改成问这一层要切点了** —— "
                      "📌 写了没人用，这个单位就还是三份")


def t_purity_and_no_kernel() -> None:
    """：一次交换**不进 Kernel**。"""
    print("\n[4] ⭐⭐ 纯视图：不持有状态、不进 Kernel")
    src = module_text("core.context.exchange")
    tree = ast.parse(src)

    # 不许 import kernel / store / sqlite —— 它只是"怎么看这堆数据"
    bad_imports = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + ([n.module] if isinstance(n, ast.ImportFrom) else [])
            for x in names:
                if x and any(k in x for k in ("kernel", "store", "sqlite", "runtime.task")):
                    bad_imports.append(x)
    check(not bad_imports,
          "⭐⭐ 没有 import kernel / store / sqlite —— "
          "📌 一次交换**不进 Kernel**（否则 Kernel 变成 UI 状态垃圾桶）。"
          "一个只是「怎么看这堆数据」的概念不该拥有自己的存储", str(bad_imports))

    # 没有模块级可变状态（那会变成第二个权威）
    mutable = [t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name) and not t.id.startswith("_")]
    check(not mutable,
          "⭐ 没有模块级可变状态 —— 📌 存了就要回答「它和消息表哪个是权威」，"
          "而那个问题不该存在", str(mutable))

    # 纯函数：不改入参
    from core.context.exchange import split
    msgs = [M("user"), M("assistant")]
    before = list(msgs)
    split(msgs)
    check(msgs == before, "⚠️ `split()` 不改入参")


def t_system_notes_counted() -> None:
    """🔴🔴 **这条测试原本钉住的是一个 bug。**（2026-08-14 改写）

    第一版断言：「系统注记以 user 角色进上下文 → 它**确实**开了一次新交换」。
    **那是错的**，外部评审 复查抓出、回代码核实成立：

        `[System check-in]` / `[System wake-up]` / `[Scheduled plan is now due]`
        为了让模型读到，**必须**以 `user` 角色进上下文（provider 只认 user/assistant），
        但**用户从没开过口**。把它当成新交换 →
            交换边界错位 → L2 摘错一整段 → L3 删错一整段
        ⚠️ 而且全程不 crash、不报错。

    📌 **一条断言只能证明「实现和我当时的理解一致」，证明不了那个理解是对的。**
       ⚠️ 更糟的是：**测试绿着，会让人以为这件事已经被想过了。**
    ⭐ 判别所需的事实一直都在（`visible_to_user`，那次泄露修出来的列）——
       📌 **一个为 A 问题引入的字段，往往正好是 B 问题缺的那个判据。**
    """
    print("\n[5] ⭐⭐⭐ 系统注记**不开新交换**（原断言钉住的是 bug，已改写）")
    from core.context.exchange import split
    msgs = [M("user", "问"),
            M("user", "[System check-in] ...", visible_to_user=False),
            M("assistant", "答")]
    ex = split(msgs)
    check(len(ex) == 1,
          "⭐⭐⭐ 系统注记**不**开新交换 —— 🔴 开了就会让 L2/L3 摘错删错整整一段，"
          "而且不报错", f"切出 {len(ex)} 段")
    check(len(ex[0].messages) == 3,
          "⭐ 但它**仍然算进这次交换** —— 📌 「算不算数」和「开不开新段」是两个问题："
          "`visible_to_user` 只回答前一个（它占上下文，衰减要算它的体积）")

    _o = split([M("user", "[System wake-up]", visible_to_user=False), M("assistant", "醒了")])
    check(_o[0].is_orphan and _o[0].user_message is None,
          "⭐⭐ 以系统注记开头 → `is_orphan` —— 📌 判据必须与 `_opens_exchange` 同一个"
          "（我改切分时一度忘了改这里：**一个概念只能有一个定义，包括它的每一处消费点**）")

    _n = split([M("user", "问1"), M("assistant", "答"), M("user", "问2")])
    check(len(_n) == 2, "⚠️ [L5] 反向：真用户消息照常开新交换（上面不是靠「永远不开」蒙的）")

    _e = split([M("user", "一二三"), M("assistant", "四五")])[0]
    check(_e.text_len() == 5, "text_len 粗略体积可用", str(_e.text_len()))


def main() -> int:
    t_basic_split()
    t_orphan_head()
    t_equivalence_with_legacy_truncate()
    t_purity_and_no_kernel()
    t_system_notes_counted()
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
