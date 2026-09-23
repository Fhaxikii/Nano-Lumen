# -*- coding: utf-8 -*-
"""L1→L2 —— **第一次真的调模型删东西**。

═══ 契约 ═══

    derive → validate → persist → activate
    **每个 Exchange 各自成败**，任何一步失败 → **继续留在 L1**

🔴 反例（外部评审 明确点名，本套件重点守）：一批 20 个里 17 成 3 败，
   **不许**整批标 L2 —— 那 3 个的原文会被当成"已经压好了"而丢掉，
   换来的却是三条不存在的结论。
📌 **一个批次只有一个成功位，等于把最差的那个成员的失败藏进平均值里。**

其余三条：
  ② **提炼器同厂**；配不到 → 退回主模型自己提炼（**宁可贵，不许失能**）
  ③ **active exchange 永远不碰**
  ④ **已生成的结论行是下一条的低分辨率上文**，而**不给原文**

用法：
  py -3.10 tests\t_f5_decay_l2.py
"""
from __future__ import annotations

import asyncio
import ast
import json
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


def M(role, content="x", **kw):
    from core.schema import ChatMessage
    return ChatMessage(role=role, content=content, **kw)


def _fresh():
    from core.runtime.store import RuntimeStore
    from core.runtime.conversation import ConversationRepository
    from core.context.decay_store import DecayStore
    st = RuntimeStore(pathlib.Path(tempfile.mkdtemp(prefix="nanol2_")) / "t.db")
    return st, ConversationRepository(st), DecayStore(st)


class _FakeProvider:
    """假提炼器。`script` 决定第 i 次调用返回什么。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []          # [(model_override, user_prompt)]
        self.target_model = "anthropic/claude-haiku-4.5"

    async def chat_without_tools(self, context, system_guide, status_callback=None,
                                 model_override=None, **kw):
        # 🔴🔴 **按真 provider 的规矩收货，不是"照单全收"。**
        #
        # 2026-08-14 实测现场：这里原本读的是 `context[0]["parts"][0]["text"]`，
        # 而 `_distill_one` 也正好传的是 `{"role":..,"parts":[..]}`（Gemini 形状）。
        # 两边**一致**，于是 22 项全绿；可真的 `ClaudeProvider._merge_context`
        # 读的是 `msg["content"]` —— 上线一跑，整段提炼输入凭空消失，
        # 日志是 `fresh=40 tokens`、三次提炼全败在「outcome 为空」。
        #
        # 📌 **假 provider 和调用方出自同一处，所以它们一致；
        #    而它们一致这件事，什么都不证明** —— 它证的是两次想法相同。
        # ⭐ 所以这里**过一遍真的那个 `_merge_context`**：形状错了当场就炸。
        from core.provider import ClaudeProvider
        merged = ClaudeProvider._merge_context(context)
        assert merged and all("content" in m for m in merged), \
            f"提炼消息形状不对（真 provider 会把它变成空的）: {context!r}"
        _txt = merged[0]["content"]
        if not isinstance(_txt, str):
            _txt = " ".join(str(b.get("text", "")) for b in _txt if isinstance(b, dict))
        self.calls.append((model_override, _txt))
        i = len(self.calls) - 1
        out = self.script[i] if i < len(self.script) else self.script[-1]
        if isinstance(out, Exception):
            raise out
        return out, model_override or self.target_model


def TR(name="t", content="RESULT", is_error=False):
    from core.schema import ToolResultBlock
    return ToolResultBlock(name=name, tool_use_id="tu", content=content,
                           is_error=is_error)


def _seed(repo, dec, sid, n, chars=2600):
    """造 n 次已经在 L1 的交换。"""
    from core.context.decay_store import L1
    storage = []
    for i in range(n):
        # ⚠️ 带上工具结果 —— L1 的交换**按定义**是有过工具结果的那些，
        #    而 2026-08-14 拆轴之后 `kind` 由「有没有工具调用」判定：
        #    没有工具结果的交换会被判成 `talk`，而 `talk` 的 `status` 是 null，
        #    于是本套件里那个「坏 status」的 fixture **根本不会被判错**。
        # 📌 造数据时省掉的那部分，会在某条断言里以「它怎么不红了」的形式回来。
        _a = M("assistant", "答" * 200)
        _a.tool_results = [TR(content="RESULT " * 50)]
        msgs = [M("user", f"第{i}个问题" + "话" * chars), _a]
        for m in msgs:
            repo.append_message(sid, m)
            storage.append(m)
    from core.context.exchange import split
    for e in split(storage)[:-1]:
        dec.record(sid, e.start_ordinal, e.end_ordinal, level=L1)

    class _Mem:
        pass
    mem = _Mem(); mem.storage = storage
    return mem


_GOOD = json.dumps({"status": "done", "outcome": "把 X 改成了 Y（按 08-03 定的公式）",
                    "referents": ["core/x.py"], "open_items": []}, ensure_ascii=False)
_BAD_SCHEMA = json.dumps({"status": "finished", "outcome": "x"}, ensure_ascii=False)
_NOT_JSON = "我觉得这次对话讲的是……"


def t_per_exchange_success() -> None:
    print("\n[1] ⭐⭐⭐ 每个 Exchange 各自成败，失败的**继续留在 L1**")
    from core.context.decay import run_l1_to_l2
    from core.context.decay_store import L1, L2
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id
    mem = _seed(repo, dec, sid, 30)

    # 第 3 次调用返回坏 schema，第 4 次直接不是 JSON，第 5 次抛异常
    #
    # ⚠️⚠️ 三个失败点**必须落在 `MAX_DISTILL_PER_RUN` 以内**。
    #    2026-08-14 给昂贵那一级单独设了上限（原来跟着 `MAX_PER_RUN=40`），
    #    这一版原本把失败放在第 3/5/7 次 —— 第 7 次**根本轮不到**，
    #    于是本项红了，而红的原因和它要证的那件事毫无关系。
    # 📌 **测试数据的规模是配额的函数** —— 这次是第二个自变量：批量上限。
    from core.context.decay import MAX_DISTILL_PER_RUN
    check(MAX_DISTILL_PER_RUN >= 5,
          "⚠️ 前置：昂贵那一级的上限装得下这三个失败点",
          str(MAX_DISTILL_PER_RUN))
    script = [_GOOD] * 30
    script[2] = _BAD_SCHEMA
    script[3] = _NOT_JSON
    script[4] = RuntimeError("提炼服务挂了")
    prov = _FakeProvider(script)

    stat = asyncio.get_event_loop().run_until_complete(
        run_l1_to_l2(mem, dec, prov, sid, "anthropic/claude-haiku-4.5"))
    check(stat["ran"] and stat["tried"] >= 5, "触发并试了若干次", str(stat))
    check(stat["failed"] == 3,
          "⭐⭐⭐ **恰好 3 个失败**（坏 schema / 非 JSON / 调用抛异常）", str(stat["failed"]))
    check(stat["ok"] == stat["tried"] - 3, "其余全部成功", f"{stat['ok']}/{stat['tried']}")

    from core.context.exchange import split
    ex = split(mem.storage)[:-1]
    lv = [dec.level_of(sid, e.start_ordinal) for e in ex[:stat["tried"]]]
    check(lv[2] == L1 and lv[3] == L1 and lv[4] == L1,
          "⭐⭐⭐ 三个失败的**仍然是 L1** —— "
          "🔴 若整批标 L2，它们的原文会被当成「已经压好了」而丢掉，"
          "换来的却是三条不存在的结论",
          f"第3/4/5 个 = {lv[2]}/{lv[3]}/{lv[4]}")
    check(lv[0] == L2 and lv[1] == L2,
          "⚠️ 反向：成功的那些确实进了 L2（不是「一个都没降」蒙的）")
    st.close_thread_conn()


def t_distiller_same_vendor_and_fallback() -> None:
    print("\n[2] ⭐⭐ 提炼器同厂 + 配不到就退回主模型（宁可贵，不许失能）")
    from core.context.decay import run_l1_to_l2
    from core.models import distiller_for, vendor_of
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id
    mem = _seed(repo, dec, sid, 20)
    prov = _FakeProvider([_GOOD] * 20)
    asyncio.get_event_loop().run_until_complete(
        run_l1_to_l2(mem, dec, prov, sid, "anthropic/claude-haiku-4.5"))
    _used = {m for m, _ in prov.calls}
    check(_used and all(vendor_of(m) == "anthropic" for m in _used),
          "⭐⭐ 用的提炼模型与主模型**同厂** —— "
          "📌 硬约束不是建议：跨厂就意味着用户没有那把 key", str(_used))

    # 未知厂商 → 退回主模型自己提炼，**不是不提炼**
    st2, repo2, dec2 = _fresh()
    sid2 = repo2.current_session().session_id
    # ⚠️ 未知模型走的是 `_FALLBACK_QUOTA`（L1=0.20×200K=40K），
    #    比 anthropic 那档（0.15）更高 → 要更多数据才触发。
    #    📌 兜底配额和真配额不是一个数，测试数据得按**实际走到的那个**算。
    mem2 = _seed(repo2, dec2, sid2, 40)
    prov2 = _FakeProvider([_GOOD] * 20)
    check(distiller_for("nobody/xx") == "", "前置：未知厂商没有专门的提炼器")
    stat = asyncio.get_event_loop().run_until_complete(
        run_l1_to_l2(mem2, dec2, prov2, sid2, "nobody/xx"))
    check(stat["ok"] > 0 and all(m == "nobody/xx" for m, _ in prov2.calls),
          "⭐⭐⭐ 退回**主模型自己提炼** —— "
          "🔴 不是「不提炼」：不提炼 = 上下文治理失效 = 迟早撞窗口。"
          "📌 **宁可贵，不许失能**", f"{stat['ok']} 条成功")
    st.close_thread_conn(); st2.close_thread_conn()


def t_prior_lines_are_fed_forward() -> None:
    print("\n[3] ⭐⭐ 已生成的结论行 = 下一条的低分辨率上文")
    from core.context.decay import run_l1_to_l2
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id
    mem = _seed(repo, dec, sid, 20)
    prov = _FakeProvider([_GOOD] * 20)
    asyncio.get_event_loop().run_until_complete(
        run_l1_to_l2(mem, dec, prov, sid, "anthropic/claude-haiku-4.5"))
    check(len(prov.calls) >= 3, "前置：至少调了三次", str(len(prov.calls)))
    _first, _third = prov.calls[0][1], prov.calls[2][1]
    check("Earlier in this same conversation" not in _first,
          "⭐ 第一次调用没有上文（前面还没有结论行）")
    check("Earlier in this same conversation" in _third,
          "⭐⭐ 第三次调用**带上了前面生成的结论行** —— "
          "📌 用「低分辨率但够定位」的上文换线性成本")
    check("把 X 改成了 Y" in _third, "上文里确实是前面那条结论")
    check("第0个问题" not in _third and "第1个问题" not in _third,
          "⭐⭐⭐ **上文里没有前面那些交换的原文**（只有它们的结论行）—— "
          "🔴 给原文就是 O(n²)，会变回「反复重读」，成本优势全没")
    st.close_thread_conn()


def t_active_never_touched() -> None:
    print("\n[4] ⭐⭐ active exchange 永远不碰")
    from core.context.decay import run_l1_to_l2
    from core.context.decay_store import L1
    st, repo, dec = _fresh()
    sid = repo.current_session().session_id
    mem = _seed(repo, dec, sid, 25)
    from core.context.exchange import split
    _last = split(mem.storage)[-1]
    prov = _FakeProvider([_GOOD] * 40)
    asyncio.get_event_loop().run_until_complete(
        run_l1_to_l2(mem, dec, prov, sid, "anthropic/claude-haiku-4.5"))
    # ⚠️ 它从没被 `record` 过 → `level_of` 走 fail-safe 返回 **L0**（不是 L1）。
    #    📌 要钉的不变量是「**没被提炼**」，写成 `== L1` 是把实现细节当成了不变量。
    check(dec.level_of(sid, _last.start_ordinal) != "L2",
          "⭐⭐ 最后一次交换没有被提炼 —— "
          "🔴 它还没关闭，摘出来的结论一出生就可能是陈旧的"
          "（tool_result 还没回来时摘成「正在执行 X」，几秒后其实是「X 失败」）")
    _texts = [t for _, t in prov.calls]
    check(not any(_last.user_message.content[:20] in t for t in _texts),
          "⚠️ active 那一轮的内容压根没被送去提炼")
    st.close_thread_conn()


def t_no_repair_no_silent_pass() -> None:
    print("\n[5] ⭐⭐ 坏输出不修补、不放行")
    from core.context.decay import _parse_digest
    check(_parse_digest("```json\n" + _GOOD + "\n```") is not None,
          "⭐ 认 ```json 围栏（模型经常那么写）")
    check(_parse_digest(_NOT_JSON) is None, "非 JSON → None")
    check(_parse_digest(_BAD_SCHEMA) is None, "schema 不过 → None")
    check(_parse_digest('{"status":"done","outcome":"x",}') is None,
          "⭐⭐ 尾逗号这种**差一点点**的也一律 None —— "
          "📌 **不做任何修补**（补引号、猜字段）："
          "一个被我们猜着修好的 digest，错在哪永远查不出来")

    src = module_text("core.context.decay")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_l1_to_l2"), None)
    seg = (ast.get_source_segment(src, fn) or "") if fn else ""
    check("continue" in seg and "level=L2" in seg,
          "⚠️ 失败路径用 `continue`（保持原级别），成功路径才 `level=L2`")


def t_ordering_cheap_before_expensive() -> None:
    print("\n[6] ⭐ 便宜的那一档排在贵的前面")
    src = module_text("core.orchestrator")
    i1, i2 = src.find("_run_l1(self.memory"), src.find("await _run_l2(")
    check(i1 > 0 and i2 > i1,
          "⭐ L0→L1（免费）排在 L1→L2（要花钱）**之前** —— "
          "📌 **贵的动作永远排在便宜的动作后面**：不然你会为一件"
          "本来不必做的事付钱", f"L1@{i1} < L2@{i2}")
    check("ladder_enabled" in src or "_ladder_on" in src,
          "⚠️ 两者都在总开关里（默认关闭）")


def main() -> int:
    t_per_exchange_success()
    t_distiller_same_vendor_and_fallback()
    t_prior_lines_are_fed_forward()
    t_active_never_touched()
    t_no_repair_no_silent_pass()
    t_ordering_cheap_before_expensive()
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
