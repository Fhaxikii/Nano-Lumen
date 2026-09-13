# -*- coding: utf-8 -*-
"""硬窗口守卫。

═══ 它和阶梯回答的是两个不同的问题 ═══

    阶梯   = 什么时候该开始逐渐遗忘 = **启发式**（配额是拍的，还没标定）
    Guard  = 这次请求到底能不能发   = **硬边界**

📌 **一个启发式的机制，底下必须垫一个确定性的兜底。**

三条最有价值的不变量：

  ① 🔴 **区分两种超限**：可回收 vs **这一轮本身装不下**。
     把后者当前者，就会一路删历史、删到没得删、最后仍然失败，
     ⚠️ 却已经把用户的历史毁了。

  ② ⚠️ **上限不是 `input < window`** —— 要给本轮输出留空间。
     📌 一个刚好塞满输入的请求，等于没有回答。

  ③ ⚠️ **`predicted is None` 要放行** —— 一个「我不知道」不该被当成「超了」，
     那会在每次重启后第一句话就拦人，而那时上下文通常恰恰最短。

用法：
  py -3.10 tests\t_f5_guard.py
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


HAIKU = "anthropic/claude-haiku-4.5"     # 200K


def t_output_reserve() -> None:
    print("\n[1] ⭐⭐ ② 上限要给输出留空间")
    from core.context import guard as G
    lim = G.admissible_input(200_000)
    check(lim < 200_000,
          "⭐⭐ 允许上限**小于**窗口 —— "
          "📌 即使接口愿意收，Nano 自己也得有地方生成；"
          "一个刚好塞满输入的请求，等于没有回答", f"{lim}/200000")
    check(0 < G.OUTPUT_RESERVE < 0.2, "预留比例是个合理的小数", str(G.OUTPUT_RESERVE))
    check(G.RED_ZONE > 0.85,
          "⭐ 红区比 CRITICAL(0.85) 更高 —— "
          "📌 Guard 是最后一道闸，正常情况下阶梯早就动过了；"
          "它频繁触发本身就是「配额失准」的信号", str(G.RED_ZONE))


def t_preflight() -> None:
    print("\n[2] ⭐⭐ 预检：不知道就放行，超了就拦")
    from core.context import guard as G
    check(G.preflight(HAIKU, None)["ok"],
          "⭐⭐⭐ ③ `predicted is None` → **放行** —— "
          "📌 一个「我不知道」不该被当成「超了」："
          "那会在每次重启后第一句话就拦人，而那时上下文通常恰恰最短")
    check(G.preflight(HAIKU, 50_000)["ok"], "远低于上限 → 放行")
    r = G.preflight(HAIKU, 199_000)
    check(not r["ok"] and r["reason"] == "over_limit", "超了 → 拦下", str(r["reason"]))
    check(G.preflight("nobody/xx", 10)["ok"], "未知模型也不炸")

    check(not G.in_red_zone(HAIKU, 100_000), "半满不算红区")
    check(G.in_red_zone(HAIKU, 190_000), "接近满 → 红区")
    check(not G.in_red_zone(HAIKU, None), "不知道 → 不算红区")


def t_classify_two_kinds() -> None:
    print("\n[3] ⭐⭐⭐ ① 区分「可回收」和「这一轮本身装不下」")
    from core.context import guard as G
    # 20 万窗口；预测 19.5 万，其中 15 万是历史 → 回收历史能解决
    check(G.classify(HAIKU, 195_000, history_tokens=150_000) == "recoverable",
          "⭐ 历史占大头 → **可回收**")
    # 预测 19.5 万，历史只有 5 千 → 这一轮本身就装不下
    check(G.classify(HAIKU, 195_000, history_tokens=5_000) == "active_too_big",
          "⭐⭐⭐ 历史全删也不够 → **active_too_big** —— "
          "🔴 把它当成「可回收」，就会一路删历史、删到没得删、"
          "最后仍然失败，**却已经把用户的历史毁了**")
    check(G.classify(HAIKU, 50_000, 40_000) == "ok", "没超 → ok")
    check(G.classify(HAIKU, None, 0) == "ok", "不知道 → ok（不拦）")


def t_messages_distinguish() -> None:
    print("\n[4] ⭐⭐ 两种超限的文案必须不一样")
    from core.context import guard as G
    a, b = G.MSG_ACTIVE_TOO_BIG, G.MSG_STILL_TOO_BIG
    check(a != b, "⭐ 两条文案不同")
    check("这一次的输入本身" in a and "拆小" in a,
          "⭐⭐ 「这一轮装不下」那条**告诉用户去拆分本次输入** —— "
          "📌 一条只说「太长了」的错误，会让用户去删历史（没用）"
          "而不是拆分本次输入")
    check("已经尽量收缩了较早的历史" in b,
          "⭐ 「回收完仍不够」那条说明 Nano 已经尽力了")
    for m in (a, b):
        check("切换到" in m, "两条都给了出路（换更大窗口的模型）")
        break


def t_no_semantic_deletion() -> None:
    """🔴 Guard 只能强制安全降级，不能强制语义删除。"""
    print("\n[5] ⭐⭐⭐ Guard 自己不删任何东西")
    src = pathlib.Path("core/context/guard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 本模块只做判断，不该出现任何写/删动作
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in ("record", "delete", "pop", "remove",
                               "add_semantic_memory", "apply_l1"):
                bad.append(n.func.attr)
    check(not bad,
          "⭐⭐⭐ 守卫模块里**没有任何写/删调用** —— "
          "📌 窗口溢出允许导致本次请求失败，**不允许导致不受控的语义删除**。"
          "⚠️ 尤其 Nano 是能操作真实电脑的 Agent，"
          "「按错误约束自信地执行」比「请求失败」危险得多", str(bad))
    # ⚠️ 也不该自己去调 _truncate_safely
    check("_truncate_safely" not in src,
          "⭐⭐ 不复用旧的 10 轮硬切 —— "
          "📌 那会绕过整个上下文治理层：它不知道 L1/L2/digest/source_hash 是什么")


def t_guard_really_blocks() -> None:
    print("\n[6] ⭐⭐⭐ 硬窗口守卫**真的拒发**（不再是 shadow）")
    import asyncio
    from core.context.guard import ContextWindowExceeded as CWE
    from core.orchestrator import Orchestrator

    # ── 这一轮本身就装不下 → **不重试**，直接把话说清 ──
    class _P1:
        target_model = "anthropic/claude-haiku-4.5"
        calls = 0

        async def chat_with_tools_stream(self, *a, **k):
            _P1.calls += 1
            raise CWE("active_too_big", "这一次的输入本身就超过了…",
                      predicted=999_999, limit=188_000)
            yield {}      # noqa: 让它是异步生成器

    class _H1:
        provider = _P1()
        memory = None

    async def _run(host):
        out = []
        async for e in Orchestrator._stream_with_window_guard.__get__(
                host, type(host))([], [], ""):
            out.append(e)
        return out

    _out = asyncio.run(_run(_H1()))
    check(_P1.calls == 1,
          "⭐⭐⭐ `active_too_big` **只试了一次，没有重试** —— "
          "📌 回收多少历史都没用，重试只是多花一次时间去撞同一堵墙",
          f"{_P1.calls} 次")
    check(len(_out) == 1 and _out[0]["event"] == "final_result"
          and "输入本身" in _out[0]["content"],
          "⭐⭐⭐ 而且**如实告诉用户是哪一种超限** —— "
          "🔴 一条只说「太长了」的错误，会让用户去删历史（没用）"
          "而不是拆分本次输入",
          str(_out[0].get("content", ""))[:24])

    # ── 一个 token 都回收不出来 → 也不重试 ──
    class _P2:
        target_model = "anthropic/claude-haiku-4.5"
        calls = 0

        async def chat_with_tools_stream(self, *a, **k):
            _P2.calls += 1
            raise CWE("still_too_big", "上下文已经接近硬上限…",
                      predicted=200_000, limit=188_000)
            yield {}

    class _M2:
        conversation_session_id = None      # 拿不到会话 → 回收不了

    class _H2:
        provider = _P2()
        memory = _M2()

    _out2 = asyncio.run(_run(_H2()))
    check(_P2.calls == 1,
          "⭐⭐⭐ 回收不出东西时**不再发一次** —— "
          "📌 重试的前提是「这次和上次不一样」，而这里没有任何不同",
          f"{_P2.calls} 次")
    check(_out2 and _out2[-1]["event"] == "final_result",
          "⚠️ 仍然给用户一句话，不是静默失败")


def t_emergency_reclaim_never_touches_active() -> None:
    print("\n[7] ⭐⭐⭐ 紧急回收**只降已关闭的交换**，当前这一轮一个字不碰")
    import pathlib as _pl
    import tempfile
    from core.context.decay import emergency_reclaim
    from core.context.decay_store import DecayStore
    from core.runtime.conversation import ConversationRepository
    from core.runtime.store import RuntimeStore
    from core.schema import ChatMessage, ToolResultBlock

    st = RuntimeStore(_pl.Path(tempfile.mkdtemp(prefix="nanoq7_")) / "t.db")
    repo = ConversationRepository(st)
    sid = repo.current_session().session_id
    storage = []
    for i in range(4):
        _u = ChatMessage(role="user", content=f"第{i}问")
        _a = ChatMessage(role="assistant", content="答")
        _a.tool_results = [ToolResultBlock(name="t", tool_use_id=f"tu{i}",
                                           content="X" * 4000)]
        for m in (_u, _a):
            repo.append_message(sid, m)
            storage.append(m)

    class _Mem:
        pass
    mem = _Mem()
    mem.storage = storage

    _active_before = storage[-1].tool_results[0].content
    got = emergency_reclaim(mem, DecayStore(st), sid,
                            "anthropic/claude-haiku-4.5", need=1_000)
    check(got > 0, "⭐ 真的腾出了 token", f"{got}")
    check(storage[-1].tool_results[0].content == _active_before,
          "⭐⭐⭐ **最后一次交换（active）一个字都没动** —— "
          "🔴 用户刚打的那句话和本轮工具结果是**不可再生**的输入；"
          "为了发得出去而删掉它，等于用「我做不到」换成「我假装你没说过」。"
          "📌 **装不下的时候，正确的失败方式是拒绝，不是偷偷少装一点**")
    check(storage[1].tool_results[0].content.startswith("[Tool output aged out"),
          "⭐⭐ 而最老的那次**确实被换成了占位符**（不是「一个都没降」蒙的）")
    st.close_thread_conn()


def main() -> int:
    t_output_reserve()
    t_preflight()
    t_classify_two_kinds()
    t_messages_distinguish()
    t_no_semantic_deletion()
    t_guard_really_blocks()
    t_emergency_reclaim_never_touches_active()
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
