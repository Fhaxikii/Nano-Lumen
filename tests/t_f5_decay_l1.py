# -*- coding: utf-8 -*-
"""L0→L1 —— 工具结果换占位符。

═══ 为什么先做这一档 ═══

它是四个箭头里**唯一不过 LLM、不动 UI、不落盘正文**的一个，
换掉的东西（工具输出）**在硬盘上还能重跑**。
⭐ 所以它是验证整套机制（触发点 / 批 / 高低水位 / 不抖）的最佳载体：
   **机制验错了也不丢任何不可再生的东西。**
📌 **把不可测的部分排在最后，先用可测的那一档把骨架钉死。**

═══ 三条会静默出错的红线（本套件重点守这三条）═══

  ① 🔴 **不许破坏 `tool_calls ↔ tool_results` 配对** ——
     只换 `content`，保住 `tool_use_id`。**删一半 = 省 token 最后变成 provider 400。**
  ② 🔴 **`is_error` 必须保留** —— 它是「尝试过」和「做过」的唯一分界。
     丢了它，一次失败的调用会在历史里读起来像成功。
  ③ 🔴 **只降 closed exchange** —— 当前这一轮永远 L0，
     否则可能在 `tool_result` 还没回来时就把它换成占位符。

⚠️ 外加：写侧**整个包在总开关里**（`ladder_enabled`）。
   ⭐ 2026-08-14 整片实测验证通过后已打开；开关的价值在它**关得住什么**，不在它现在是什么值。

用法：
  py -3.10 tests\t_f5_decay_l1.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _fresh():
    from core.runtime.store import RuntimeStore
    from core.runtime.conversation import ConversationRepository
    from core.context.decay_store import DecayStore
    st = RuntimeStore(pathlib.Path(tempfile.mkdtemp(prefix="nanol1_")) / "t.db")
    return st, ConversationRepository(st), DecayStore(st)


def M(role, content="x", **kw):
    from core.schema import ChatMessage
    return ChatMessage(role=role, content=content, **kw)


def TR(uid, body, err=False):
    from core.schema import ToolResultBlock
    return ToolResultBlock(name="t", tool_use_id=uid, content=body, is_error=err)


def TC(uid):
    from core.schema import ToolCall
    return ToolCall(name="t", args={"q": "x"}, tool_use_id=uid, index=0)


def _turn(uid, body, err=False, big=2000):
    """一次带工具往返的交换：user → tool_calls → tool_results → assistant。"""
    return [M("user", "问" * 20),
            M("tool_calls", tool_calls=[TC(uid)]),
            M("tool_results", tool_results=[TR(uid, body or ("料" * big), err)]),
            M("assistant", "答" * 20)]


def t_replace_not_truncate() -> None:
    print("\n[1] ⭐⭐⭐ L1 是「替换」，且不许破坏配对 / 丢 is_error")
    from core.context.exchange import split
    from core.context.decay import apply_l1
    msgs = _turn("u1", "工具原始输出" * 500) + _turn("u2", "失败了", err=True)
    ex = split(msgs)
    check(len(ex) == 2, "前置：两次交换", str(len(ex)))

    _before_ids = [tr.tool_use_id for m in ex[0].messages
                   for tr in (m.tool_results or [])]
    n = apply_l1(ex[0])
    check(n == 1, "换掉 1 条工具结果", str(n))
    _tr = [tr for m in ex[0].messages for tr in (m.tool_results or [])][0]
    check(_tr.tool_use_id == _before_ids[0],
          "⭐⭐⭐ ① `tool_use_id` 原样保留 —— 🔴 删掉配对的一半 = provider 400")
    check(_tr.content.startswith("[Tool output aged out"),
          "⭐ content 被换成占位符")
    check("aged out" in _tr.content and "run the tool again" in _tr.content,
          "⚠️ 占位符说清「怎么拿回来」，不是只说「没了」—— "
          "同 [D10] 图片占位符那条：不许让模型自我否定")

    # ② is_error 必须活下来
    apply_l1(ex[1])
    _tr2 = [tr for m in ex[1].messages for tr in (m.tool_results or [])][0]
    check(_tr2.is_error is True,
          "⭐⭐⭐ ② `is_error` 保住了 —— 🔴 丢了它，一次失败的调用"
          "会在历史里读起来像成功（「尝试过」被读成「做过」）")

    # 幂等：再降一次不该重复包裹
    _again = apply_l1(ex[0])
    check(_again == 0,
          "⭐ 幂等：已经换过的不再换 —— 📌 否则占位符会被套娃，长度反而涨")

    # ⚠️ 不许当成「第二次截断」
    src = module_text("core.context.decay")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "apply_l1"), None)
    _body = "\n".join((ast.get_source_segment(src, x) or "") for x in (fn.body if fn else [])
                      if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)))
    check(fn is not None and "[:" not in _body,
          "⭐⭐ `apply_l1` 里没有切片截断 —— "
          "📌 L1 是**替换**不是二次截断；不管当初是 3K 还是"
          "「原始 50K → 安全阀截成 12K」，到了 L1 都整个换掉")


def t_active_exchange_never_demoted() -> None:
    print("\n[2] ⭐⭐⭐ ③ 当前这一轮永远 L0")
    from core.context.decay import run_l0_to_l1
    from core.context.decay_store import L0
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id

    class _Mem:
        pass
    mem = _Mem()
    mem.storage = []
    # ⚠️ 40 轮 × 3000 字：配额定成 L0=25%×200K=**50K token** 之后，
    #    触发门槛比写这套测试时假设的高得多。
    #    📌 测试数据的规模是配额的函数 —— 配额一改，这些数字就得重算。
    mem.storage = []
    for i in range(40):
        for m in _turn(f"u{i}", None, big=3000):
            repo.append_message(sid, m)
            mem.storage.append(m)

    from core.context.exchange import split
    _last = split(mem.storage)[-1]
    stat = run_l0_to_l1(mem, dec, sid, "anthropic/claude-haiku-4.5")
    check(stat["ran"], "前置：确实触发了（历史够厚）", str(stat))
    check(dec.level_of(sid, _last.start_ordinal) == L0,
          "⭐⭐⭐ 最后一次交换（active）**没有**被降级 —— "
          "🔴 否则可能在 tool_result 还没回来时就把它换成占位符，"
          "Digest 一出生就是陈旧的")
    _tr = [tr for m in _last.messages for tr in (m.tool_results or [])]
    check(_tr and not _tr[0].content.startswith("[Tool output aged out"),
          "⚠️ active 那一轮的工具结果原文还在")
    st.close_thread_conn()


def t_trigger_and_low_water() -> None:
    print("\n[3] ⭐⭐ 触发是「点」，执行是「批」—— 降到低水位，不是刚好不越线")
    from core.context.decay import run_l0_to_l1, LOW_WATER
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id

    class _Mem:
        pass
    mem = _Mem(); mem.storage = []
    for i in range(30):
        for m in _turn(f"u{i}", None, big=3000):
            repo.append_message(sid, m)
            mem.storage.append(m)

    stat = run_l0_to_l1(mem, dec, sid, "anthropic/claude-haiku-4.5")
    check(stat["ran"] and stat["demoted"] > 0, "触发并降了一批", str(stat["demoted"]))
    check(stat["after"] <= stat["quota"] * LOW_WATER + 1,
          "⭐⭐ 降到了**低水位**（配额 × 0.70），不是刚好不越线 —— "
          "📌 处理到刚好不越线 → 下一条又越 → 边界抖动 → UI 分界线刷屏",
          f"after={stat['after']} 目标≤{int(stat['quota']*LOW_WATER)}")

    # 再跑一次不该继续降（已经在低水位以下）—— 这就是「不抖」
    stat2 = run_l0_to_l1(mem, dec, sid, "anthropic/claude-haiku-4.5")
    check(not stat2["ran"],
          "⭐⭐ 紧接着再跑一次 → **不触发** —— 这就是「降到低水位」买到的东西")
    st.close_thread_conn()


def t_no_tool_results_still_advances() -> None:
    """⚠️ 没有工具结果的交换也要标 L1，否则会被永远重新考虑。"""
    print("\n[4] ⭐ 纯文字交换也要推进档位（防死循环）")
    from core.context.decay import run_l0_to_l1
    from core.context.decay_store import L1
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id

    class _Mem:
        pass
    mem = _Mem(); mem.storage = []
    for i in range(50):
        for m in (M("user", "问" * 1000), M("assistant", "答" * 1000)):
            repo.append_message(sid, m)
            mem.storage.append(m)

    stat = run_l0_to_l1(mem, dec, sid, "anthropic/claude-haiku-4.5")
    check(stat["ran"], "前置：触发了", str(stat))
    check(stat["results"] == 0, "⚠️ 一条工具结果都没换（本来就没有）")
    check(stat["demoted"] > 0 and dec.level_of(sid, 1) == L1,
          "⭐⭐ 但档位**推进了** —— 📌 不标 L1 的话它会被永远重新考虑，"
          "而每一轮都不会变小（死循环）", f"demoted={stat['demoted']}")
    # 不许一次把整段历史降完
    from core.context.decay import MAX_PER_RUN
    check(stat["demoted"] <= MAX_PER_RUN,
          "⭐ 一轮有上限 —— 📌 宁可这一轮没降够，下一轮接着降",
          f"{stat['demoted']} ≤ {MAX_PER_RUN}")
    st.close_thread_conn()


def t_off_by_default() -> None:
    print("\n[5] ⭐⭐ 默认关闭 + 治理层故障不许把对话搞挂")
    # ⚠️⚠️ **2026-08-14：这条断言从「默认 False」改成「开关必须真的管用」。**
    #    原来钉的是 `ladder_enabled() is False` —— 那是切换期的安全声明
    #    （「引入一个机制和启用一个机制是两件事」），在**整片实测验证通过之后
    #    就过期了**。「FALSE = 没落地，而目标是真把功能落地，
    #    而不是理论上存在」。
    # 🔴 但那条断言里**真正长期有效的部分**必须留下来：不是"它是 false"，
    #    而是"**写侧整个包在这个开关里**"（下面的 AST 检查）——
    #    📌 一个开关的价值不在它现在是什么值，在它**关得住什么**。
    from core.models import ladder_enabled
    check(isinstance(ladder_enabled(), bool),
          "⚠️ 前置：总开关读得出来且是 bool",
          str(ladder_enabled()))

    src = module_text("core.orchestrator")
    tree = ast.parse(src)
    gated = False
    for n in ast.walk(tree):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Call)
                and isinstance(n.test.func, ast.Name) and n.test.func.id == "_ladder_on"):
            seg = ast.get_source_segment(src, n) or ""
            if "run_l0_to_l1" in seg:
                gated = True
    check(gated, "⭐⭐ 接线点**整个包在开关里** —— 关着时一个字节都不动")

    # 治理层异常必须被吞
    from core.context.decay import run_l0_to_l1
    class _Boom:
        @property
        def storage(self):
            raise RuntimeError("boom")
    stat = run_l0_to_l1(_Boom(), None, "sid", "anthropic/claude-haiku-4.5")
    check(stat["ran"] is False,
          "⭐ 治理层炸了 → 静静返回，**不往上抛** —— "
          "📌 不降级只是上下文厚一点；抛出去就是对话挂了")


def main() -> int:
    t_replace_not_truncate()
    t_active_exchange_never_demoted()
    t_trigger_and_low_water()
    t_no_tool_results_still_advances()
    t_off_by_default()
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
