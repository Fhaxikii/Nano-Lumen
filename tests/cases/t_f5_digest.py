# -*- coding: utf-8 -*-
"""L2 结论行的 schema 与提炼契约。

⚠️ **这一步不调模型** —— 它只钉「一条结论行长什么样」和「问什么」。
📌 **先把"摘要什么"钉死，再去摘** —— 反过来的话，第一批 digest 就是按
   一个还没想清楚的形状生成的，而它们**会一直留在库里**。

四条最有价值的不变量：

  ① 🔴 **fail-closed**：校验不过 → `None`，那次交换继续留在 L1。
     📌 一条半残的结论行会被后面的结论行当成上文继续引用，
        错误会**沿着结论链传播**，而且每一步看起来都很正常。

  ② 🔴 **数组必须有上限** —— 否则「固定 schema → 输出体积固定」是假的。

  ③ ⭐ **`chat` 档必须存在**——
     大量交换根本不是任务，硬套 `done` 就是「schema 反过来污染事实」。

  ④ ⭐ **提示词里必须写死"outcome 要自包含、禁止代词"** ——
     这条直接决定 L2 那行结论有多可用。

用法：
  py -3.10 tests\cases\t_f5_digest.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_schema_shape() -> None:
    print("\n[1] ⭐⭐ 四个字段，一个不多")
    from core.context import digest as D
    check(D.FIELDS == ("kind", "status", "outcome", "referents", "open_items"),
          "⭐ 就这五个字段（2026-08-14 拆轴后多了 `kind`）", str(D.FIELDS))
    for banned in ("reasoning", "confidence", "next_step", "tags",
                   "memory_type", "wrong_assumption", "correct_behavior"):
        if banned in D.FIELDS:
            check(False, f"🔴 被明确反对的字段 {banned} 混进来了")
            break
    else:
        check(True, "⭐⭐ 被明确反对的七个字段一个都没混进来 —— "
                    "📌 `reasoning` 会膨胀 / `confidence` 是伪精度 / "
                    "`next_step` 把当时的建议凝固成现在的事实 / "
                    "其余属于语义记忆的 ontology")
    # 🔴🔴 **2026-08-14 拆轴**：这里原来钉的是「`chat` 必须在 STATUS 里」。
    #    那条断言守的东西是对的（不许逼模型给闲聊贴 `done`），
    #    但**守错了地方** —— 实测跑下来几乎所有交换都被判成 `chat`，
    #    连「读完整个 docx 并总结」也是。
    #    诊断：`chat` 与 `done/partial/blocked/failed` **不在同一个轴上**
    #    （前者是类型，后者是结果）。一个枚举混两个轴，模型就得先隐式回答
    #    "这算不算一次尝试"，而提示词里最响的那句正好把它推向 `chat`。
    # 📌 **一条断言可以完全正确地守着一个放错了位置的设计。**
    check("chat" not in D.STATUS,
          "⭐⭐⭐ `chat` **不再**混在 STATUS 里（它是类型，不是结果）",
          str(D.STATUS))
    check(D.KIND == ("talk", "work"), "⭐⭐ 类型独立成 `kind`", str(D.KIND))
    check(set(D.STATUS) >= {"partial", "blocked", "failed"},
          "⚠️ 「尝试过」不会被记成「做过」（partial/blocked/failed 都在）")

    # ⭐⭐⭐ 拆轴的**关键**不是分成两个字段，是第一个字段**根本不问模型**
    class _Ex:
        def __init__(self, msgs):
            self.messages = msgs

    from core.schema import ChatMessage, ToolResultBlock
    _talk = _Ex([ChatMessage(role="user", content="随便聊聊"),
                 ChatMessage(role="assistant", content="好啊")])
    _a = ChatMessage(role="assistant", content="读好了")
    _a.tool_results = [ToolResultBlock(name="t", tool_use_id="u",
                                       content="BODY", is_error=True)]
    _work = _Ex([ChatMessage(role="user", content="读一下"), _a])
    check(D.kind_of(_talk) == "talk" and D.kind_of(_work) == "work",
          "⭐⭐⭐ `kind` 由**代码**从有没有工具调用判定 —— "
          "📌 那是 `ex.messages` 里摆着的事实，不是判断；"
          "问模型 = 把已知事实交给它猜，再花钱验证它猜得对不对")
    check(D.tool_facts(_work) == (1, 1) and D.tool_facts(_talk) == (0, 0),
          "⭐⭐ 工具数/报错数也是**算出来的** —— "
          "📌 能算出来的事实，别让模型去数正文里有几个 ERROR",
          str(D.tool_facts(_work)))
    _d, _e = D.validate({"status": "done", "outcome": "x"}, kind="talk")
    check(_d is not None and _d["status"] is None,
          "⭐⭐⭐ `talk` 的 status **强制为 None**（模型给了也不要）—— "
          "📌 闲聊没有「做成了没有」可答，编一个就是记下了一件没发生的事")
    _d2, _e2 = D.validate({"status": "finished", "outcome": "x"}, kind="work")
    check(_d2 is None, "⚠️ `work` 的坏 status 仍然 fail-closed", str(_e2))


def t_validate_fail_closed() -> None:
    print("\n[2] ⭐⭐⭐ ① fail-closed：校验不过就是 None")
    from core.context import digest as D
    ok, errs = D.validate({"status": "done", "outcome": "把 X 改成了 Y",
                           "referents": ["core/x.py"], "open_items": []})
    check(ok is not None and not errs, "正常的一条能过", str(errs))

    for bad, why in (
        ({"status": "finished", "outcome": "x"}, "status 不在枚举里"),
        ({"status": "done", "outcome": "  "}, "outcome 为空"),
        ({"status": "done", "outcome": "x", "referents": "不是数组"}, "referents 不是数组"),
        ("我不是对象", "根本不是 dict"),
    ):
        d, e = D.validate(bad)
        if d is not None or not e:
            check(False, f"🔴 应该被拒绝却过了：{why}")
            break
    else:
        check(True, "⭐⭐⭐ 四种坏形状全部返回 `None` + 错误说明 —— "
                    "📌 一条半残的结论行会被后面的结论行当成上文继续引用，"
                    "错误沿结论链传播，而且每一步看起来都很正常")

    # ⚠️ 空 digest 的形状本身要能用
    check(D.validate(D.empty())[0] is None,
          "⚠️ `empty()` 只是个空壳，**过不了校验**（outcome 为空）—— "
          "📌 它是给调用方当初始值的，不是一条合法结论")


def t_arrays_are_bounded() -> None:
    print("\n[3] ⭐⭐⭐ ② 数组有上限（否则「固定体积」是假的）")
    from core.context import digest as D
    d, e = D.validate({"status": "done", "outcome": "x",
                       "referents": [f"r{i}" for i in range(50)],
                       "open_items": [f"o{i}" for i in range(50)]})
    check(d is not None, "超上限**不算校验失败**，只截断", str(e))
    check(len(d["referents"]) == D.MAX_REFERENTS
          and len(d["open_items"]) == D.MAX_OPEN_ITEMS,
          "⭐⭐⭐ 被截到上限 —— 🔴 数组无界的话 `referents: 83 个` "
          "照样能让摘要地板长高，「固定 schema → 体积固定」就是假的",
          f"{len(d['referents'])}/{len(d['open_items'])}")
    d2, _ = D.validate({"status": "chat", "outcome": "y",
                        "referents": ["超长" * 200]})
    check(len(d2["referents"][0]) <= D.MAX_ITEM_CHARS, "单项也有长度上限")
    d3, _ = D.validate({"status": "chat", "outcome": "长" * 5000})
    check(len(d3["outcome"]) <= D.MAX_OUTCOME_CHARS, "outcome 有长度上限")


def t_prompt_contract() -> None:
    print("\n[4] ⭐⭐ ④ 提示词把硬要求写死了")
    from core.context import digest as D
    sysmsg, user = D.build_prompt("[user] 那就按刚才那个改\n[nano] 好", [])
    check("JSON only" in sysmsg, "system 说清只要 JSON")
    check("MUST STAND ALONE" in user,
          "⭐⭐⭐ **outcome 必须自包含**写进了提示词 —— "
          "这条直接决定 L2 那行结论有多可用")
    check('"that plan"' in user or "that plan" in user,
          "⭐ 给了**具体反例**（`that plan` / `it` / `the above approach`）—— "
          "📌 抽象禁令模型容易绕过，具体反例不容易")
    check("BAD :" in user and "GOOD:" in user,
          "⭐ 带了 BAD/GOOD 对照（那个 RAG 归档公式的例子）")
    for cap in (str(D.MAX_REFERENTS), str(D.MAX_OPEN_ITEMS), str(D.MAX_OUTCOME_CHARS)):
        if cap not in user:
            check(False, f"🔴 上限 {cap} 没写进提示词")
            break
    else:
        check(True, "⭐⭐ **三个上限都写进了提示词** —— "
                    "📌 与其在事后猜哪个更重要，不如在事前告诉它有多少位置；"
                    "校验那边的截断只是兜底")
    # ⚠️ 2026-08-14 拆轴后**不再有** "most exchanges are not tasks" 那句 ——
    #    它承担的事现在由 `kind` 承担，而且是**代码**判的。
    #    提示词里该有的换成了另一件事：把客观事实告诉它，别让它自己数。
    _, _uw = D.build_prompt("[user] 读一下", [], kind="work", n_tools=3, n_err=2)
    check("3 tool result" in _uw and "2 of them returned an error" in _uw,
          "⭐⭐⭐ `work` 的提示词里带**数出来的事实**（几次调用 / 几次报错）—— "
          "📌 `blocked/failed` 判错，多半是因为我们让它自己去数正文里的 ERROR")
    _, _ut = D.build_prompt("[user] 聊聊", [], kind="talk")
    check("Set it to null" in _ut and "tool result" not in _ut,
          "⭐⭐ `talk` 的提示词**直接要求 status 为 null**，也不喂工具事实")
    check("Simplified Chinese" in _ut or "English" in _ut,
          "⭐⭐ 提示词带**语言指令**（走 `core/i18n.py` 那个唯一出处）—— "
          "📌 结论行同时被模型和用户读，让它跟随用户语言不只是好看："
          "提炼器读中文写英文，等于多做一次有损翻译")
    check("fixed keys" in _ut,
          "⭐⭐ 但明说 `kind`/`status` 是 key **永不翻译** —— "
          "📌 一个被翻译过的枚举值，会在校验那边变成「不在枚举里」")


def t_prior_lines_are_for_reference_only() -> None:
    print("\n[5] ⭐⭐ 上文只给结论行，且只为解指代")
    from core.context import digest as D
    _, user = D.build_prompt("[user] 改一下", [f"第{i}条结论" for i in range(20)])
    check("do NOT summarize these" in user,
          "⭐⭐ 明说**别去总结这些上文** —— "
          "🔴 让它总结上文就退回递归摘要了（摘要的摘要 → 乘性损失、地板单调抬高）")
    check(user.count("第") <= 8 + 1,
          "⭐ 只给最近几条 —— 📌 给多了它会开始「总结总结」", str(user.count("第")))
    check("resolving references only" in user, "说清上文的用途是解指代")

    _, u2 = D.build_prompt("[user] x", [])
    check("Earlier in this same conversation" not in u2,
          "⚠️ 没有上文时不硬塞一段空的")


def t_exchange_text_keeps_user_verbatim() -> None:
    print("\n[6] ⭐⭐⭐ 用户原话不裁，Nano/工具那半可以裁")
    from core.context import digest as D
    from core.context.exchange import split
    from core.schema import ChatMessage as M, ToolResultBlock as TRB

    long_user = "我" * 3000
    msgs = [M(role="user", content=long_user),
            M(role="tool_results", tool_results=[TRB(name="t", tool_use_id="u1",
                                                     content="工" * 5000)]),
            M(role="assistant", content="答" * 5000)]
    txt = D.exchange_text(split(msgs)[0])
    check(long_user in txt,
          "⭐⭐⭐ **用户原话一个字没裁** —— "
          "📌 实测一整段会话里真人打的字只占 0.32%，保它的边际成本极低；"
          "而它是**唯一不可再生**的那部分")
    check("工" * 5000 not in txt and "答" * 5000 not in txt,
          "⭐ 工具结果和 Nano 的话被裁了（那半可再生）")
    check("[tool]" in txt or "[tool " in txt, "工具结果有角色标记")

    # is_error 要在输入里看得见 —— 否则提炼器分不清"尝试过"和"做过"
    m2 = [M(role="user", content="做一下"),
          M(role="tool_results", tool_results=[TRB(name="t", tool_use_id="u2",
                                                   content="炸了", is_error=True)])]
    check("ERROR" in D.exchange_text(split(m2)[0]),
          "⭐⭐ 失败的工具结果在输入里标了 ERROR —— "
          "🔴 提炼器看不见它，就会把「尝试过」写成「做过」")


def t_render_line() -> None:
    print("\n[7] ⭐ 结论行渲染成**一行**")
    from core.context import digest as D
    line = D.render_line({"status": "failed", "outcome": "改 X 失败",
                          "referents": ["core/x.py"], "open_items": ["等用户确认"]})
    check("\n" not in line,
          "⭐ 就一行 —— 📌 它要被几十条一起塞进上下文，每条多一行就是几十行")
    check("(failed)" in line, "非 chat 的状态标出来")
    check(D.render_line({"status": "chat", "outcome": "闲聊了两句"}).startswith("闲聊"),
          "⚠️ `chat` **不标状态** —— 📌 每一条闲聊都挂个 `(chat)` 是纯噪音")
    check(D.render_line({}) == "", "空的渲染成空串，不炸")


def main() -> int:
    t_schema_shape()
    t_validate_fail_closed()
    t_arrays_are_bounded()
    t_prompt_contract()
    t_prior_lines_are_for_reference_only()
    t_exchange_text_keeps_user_verbatim()
    t_render_line()
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
