# -*- coding: utf-8 -*-
"""**真的把它跑一遍** ——那五个的回归。

═══ 这个套件为什么必须存在 ═══

🔴🔴 `tests/t_f5_bridge_l3.py` 有 28 项、全绿，而 `bridge.hand_off()` 里写着
   `from core.memory_store import get_store` —— **那个名字根本不存在**
   （真名 `get_memory_store`）。于是整条 L2→L3→L4 是死的：每次 ImportError
   被 except 吞掉、返回 None、所有交换永远卡在 L2。

它能活下来，是因为那 28 项里验 `hand_off` 的四项**全是 `ast.parse` 源码结构
检查**（调用先后、关键字参数、`return None` 的位置）——
**没有一项真的调用过这个函数。**

📌 当初把断言从「读源码文本」搬去「读 AST」，是为了躲开
   *「断言被自己写的注释喂红」* 那个坑；结果换来一个更糟的形状：
   **断言读的是代码的【形状】，而不是代码的【行为】** ——
   形状可以完全正确，而它一跑就炸。
📌 **一个从不执行被测代码的套件，证明的是「我把它摆成了我理解的样子」。**

⚠️ 所以本文件的纪律是**反过来的**：
   **不许出现 `ast.parse` / `read_text`。** 每一项都必须真的调用。

═══ 五条必守项 ═══
  ① L2 在账本里是 L2，但 `storage` 里还是 L1 原文 —— 压缩收益是假的
  ② L2 厚度只算结论行，漏掉逐字保留的用户原话 —— 系统性低估
  ③ L3 commit 后没从 `storage` 移除 —— 要等重启才真的忘掉
  ④ `bridge` 调不存在的 `get_store` —— L3 整条腿跑不通
  ⑤ 索引写着"可 recall"，而模型手里没有能查到它的工具 —— 假能力

用法：
  py -3.10 tests\t_f5_live.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _fresh():
    """临时 runtime 库 + repo + decay。⚠️：测试不许读生产库。"""
    from core.runtime.store import RuntimeStore
    from core.runtime.conversation import ConversationRepository
    from core.context.decay_store import DecayStore
    st = RuntimeStore(pathlib.Path(tempfile.mkdtemp(prefix="nanolive_")) / "t.db")
    return st, ConversationRepository(st), DecayStore(st)


def _fresh_memory_store():
    """把**全局**语义记忆库换成临时库。⚠️ 同。"""
    from core import memory_store as MS
    MS._store = MS.WorkingMemoryStore(
        pathlib.Path(tempfile.mkdtemp(prefix="nanomem_")) / "m.db")
    return MS._store


def M(role, content="x", **kw):
    from core.schema import ChatMessage
    return ChatMessage(role=role, content=content, **kw)


def TR(name="t", content="RESULT-BODY " * 40, is_error=False):
    from core.schema import ToolResultBlock
    return ToolResultBlock(name=name, tool_use_id="tu_1", content=content,
                           is_error=is_error)


class _Mem:
    """`rebuild_projection` 只需要 `.storage`。"""
    def __init__(self, msgs):
        self.storage = list(msgs)


_D = {"status": "done", "outcome": "把 rag.py 的检索阈值从 0.5 调到 0.35",
      "referents": ["core/rag.py"], "open_items": ["等实测标定"]}


def _seed(repo, sid, *, big_user=False):
    """两次交换：第一次带工具结果，第二次是当前轮（active，永远不碰）。"""
    u1 = M("user", ("需求很长。" * 800) if big_user else "改一下检索阈值")
    a1 = M("assistant", "好的，我改了")
    a1.tool_results = [TR()]
    repo.append_message(sid, u1)
    repo.append_message(sid, a1)
    repo.append_message(sid, M("user", "现在这一轮"))
    repo.append_message(sid, M("assistant", "在做"))
    return repo.load_messages(sid)


# ══════════════════════════════════════════════════════════════════
def t_bridge_actually_runs() -> None:
    print("\n[1] 🔴🔴 ④ **真的调一次 `hand_off`** —— 这一项就是那个 bug 的回归")
    from core.context import bridge as B
    store = _fresh_memory_store()

    mid = B.hand_off(_D, "把阈值调低点", source_ref="s:1")
    check(bool(mid), "⭐⭐⭐ `hand_off` 真的**返回了 memory_id** —— "
                     "🔴 修之前这里恒为 None（ImportError 被 except 吞掉），"
                     "而 28 项静态断言全绿", str(mid))
    if mid:
        row = store.get_semantic_memory(mid)
        check(row is not None, "⭐⭐ 语义记忆**真的落库了**（不是只返回了个 id）")
        check(bool(row) and row.get("memory_type") == "exchange",
              "⭐ 类型是 exchange（不污染 task_pattern / correction）",
              str(row.get("memory_type") if row else None))
        _t = str((row or {}).get("canonical_text") or "")
        check("阈值" in _t and "rag.py" in _t,
              "⭐ 载体里同时有用户原话和结论（能被检索到）")

    # ⚠️ 回滚：交接成功但后续失败时，不许留孤儿
    if mid:
        B.rollback(mid)
        _after = store.get_semantic_memory(mid)
        # ⚠️ `soft_delete_semantic_memory` 写的是 `deleted_at`（不是 `enabled`）——
        #    所有检索路径都过滤 `deleted_at IS NULL`，所以这就是"撤掉了"。
        _dead = _after is None or bool(_after.get("deleted_at"))
        check(_dead, "⭐⭐ `rollback` 真的把那条撤掉了 —— "
                     "📌 fail-closed 只保证「没提交的不算数」，"
                     "不会替你收拾「已经写下去的」", str(bool(_after)))

    check("recall_conversation" in B.index_line(_D),
          "⭐⭐⭐ ⑤ 索引条目**指名道姓**说得出那个工具 —— "
          "🔴 原来写的是泛指的「可 recall」，而模型手里唯一带 recall 字样的是 "
          "`recall_working_memory`，查的是另一张表", B.index_line(_D))


def t_l2_is_really_projected() -> None:
    print("\n[2] 🔴🔴 ① L2 **真的进了 storage**（不是只在表里改个标签）")
    from core.context.decay import project_exchange, rebuild_projection
    from core.context.exchange import split
    import json
    st, repo, decay = _fresh()
    sid = repo.current_session().session_id
    msgs = _seed(repo, sid)
    ex0 = split(msgs)[0]

    decay.record(sid, ex0.start_ordinal, ex0.end_ordinal, level="L2", digest=_D)
    ent = decay.get(sid, ex0.start_ordinal)
    out = project_exchange(ex0, ent)
    check(len(out) == 2, "⭐⭐⭐ L2 投影 = **两条**（用户原话 + 结论行）",
          f"{len(out)} 条")
    check(bool(out) and getattr(out[0], "role", "") == "user",
          "⭐ 第一条是用户原话（逐字保留）")
    check(len(out) == 2 and "0.35" in str(out[1].content),
          "⭐⭐ 第二条是**结论行**，不是 Nano 原回复",
          str(out[-1].content)[:60])

    # 端到端：整段投影重建
    mem = _Mem(msgs)
    rebuild_projection(mem, decay, sid)
    _joined = " ".join(str(getattr(m, "content", "")) for m in mem.storage)
    check("好的，我改了" not in _joined,
          "⭐⭐⭐ 重建之后 Nano 那半**真的不在 storage 里了** —— "
          "🔴 修之前它原样留着，日志说省了、其实没省")
    check("现在这一轮" in _joined, "⚠️ active 那一轮一个字没动")

    # 幂等
    _n1 = len(mem.storage)
    rebuild_projection(mem, decay, sid)
    check(len(mem.storage) == _n1,
          "⭐⭐ 连跑两次结果相同（幂等）—— "
          "📌 live 每轮都会调它，不幂等就会每轮多插一条结论行", f"{_n1}/{len(mem.storage)}")


def t_l3_evicts_live() -> None:
    print("\n[3] 🔴🔴 ③ L3 **当场**从 storage 移出（不用等重启）")
    from core.context.decay import project_exchange, rebuild_projection
    from core.context.exchange import split
    st, repo, decay = _fresh()
    sid = repo.current_session().session_id
    msgs = _seed(repo, sid)
    ex0 = split(msgs)[0]

    decay.record(sid, ex0.start_ordinal, ex0.end_ordinal, level="L3",
                 digest=_D, index_entry="〔改阈值，可 recall_conversation〕")
    check(project_exchange(ex0, decay.get(sid, ex0.start_ordinal)) == [],
          "⭐⭐⭐ L3 投影 = **空**（内容不进上下文）")

    mem = _Mem(msgs)
    rebuild_projection(mem, decay, sid)
    _joined = " ".join(str(getattr(m, "content", "")) for m in mem.storage)
    check("改一下检索阈值" not in _joined,
          "⭐⭐⭐ 用户那句也移出去了 —— L3 移的是**整次交换**")
    check(len(mem.storage) == 2, "⚠️ 只剩 active 那一轮", str(len(mem.storage)))

    # L4 同样不进上下文
    decay.record(sid, ex0.start_ordinal, ex0.end_ordinal, level="L4",
                 digest=_D, index_entry="〔改阈值〕")
    check(project_exchange(ex0, decay.get(sid, ex0.start_ordinal)) == [],
          "⭐ L4 也不进上下文（L4 = 索引也过期，不是「回来了」）")


def t_stale_falls_back_to_raw() -> None:
    print("\n[4] ⭐⭐ 陈旧的派生物 → 投影退回原文（fail-safe 方向）")
    from core.context.decay import project_exchange
    from core.context.exchange import split
    st, repo, decay = _fresh()
    sid = repo.current_session().session_id
    msgs = _seed(repo, sid)
    ex0 = split(msgs)[0]
    decay.record(sid, ex0.start_ordinal, ex0.end_ordinal, level="L3",
                 digest=_D, index_entry="x")
    ent = dict(decay.get(sid, ex0.start_ordinal))
    ent["source_hash"] = "变过了"
    check(decay.is_stale(ent), "前置：这条确实算陈旧")
    out = project_exchange(ex0, ent, decay.is_stale(ent))
    check(len(out) == len(ex0.messages),
          "⭐⭐ 陈旧 → **原文**，不是空 —— "
          "📌 猜「它降过级了」会少发内容给模型；猜「还是原文」最多多花点 token",
          f"{len(out)}/{len(ex0.messages)}")

    # 统一出口：陈旧的不许出现在 active_entries 里
    check(ex0.start_ordinal in decay.load_session(sid),
          "前置：load_session 里有这条")
    _act = decay.active_entries(sid)
    check(isinstance(_act, dict), "⭐ `active_entries` 是唯一的消费出口（存在且可调）")


def t_l2_footprint_counts_user_words() -> None:
    print("\n[5] 🔴 ② L2 厚度**必须算上逐字保留的用户原话**")
    from core.context.decay import l2_footprint, _line_tokens
    from core.context.exchange import split
    import json
    st, repo, decay = _fresh()
    sid = repo.current_session().session_id
    msgs = _seed(repo, sid, big_user=True)     # 用户粘了一大段
    ex0 = split(msgs)[0]
    decay.record(sid, ex0.start_ordinal, ex0.end_ordinal, level="L2", digest=_D)
    ent = decay.get(sid, ex0.start_ordinal)

    _line_only = _line_tokens(_D)
    _real = l2_footprint(ex0, ent)
    check(_real > _line_only * 5,
          "⭐⭐⭐ 大段用户原话时，真实占用**远大于**结论行 —— "
          "🔴 原来只算结论行，一条 20K 的用户需求会被报成 ≈150 token，"
          "**系统性往低估方向错**", f"结论行 {_line_only} → 实际 {_real}")


def t_l1_persists_before_projecting() -> None:
    print("\n[6] ⭐⭐ L1：落盘失败**不许**留下「投影降了 / 权威没降」")
    from core.context.decay import run_l0_to_l1
    from core.context.exchange import split
    st, repo, decay = _fresh()
    sid = repo.current_session().session_id
    msgs = _seed(repo, sid)

    class _Refuse:
        """一个永远写不进去的衰减表。"""
        def __init__(self, real): self._r = real
        def record(self, *a, **k): return False
        def __getattr__(self, n): return getattr(self._r, n)

    mem = _Mem(msgs)
    _before = str(split(mem.storage)[0].messages[1].tool_results[0].content)
    run_l0_to_l1(mem, _Refuse(decay), sid, "anthropic/claude-haiku-4.5")
    _after = str(split(mem.storage)[0].messages[1].tool_results[0].content)
    check(_before == _after,
          "⭐⭐⭐ `record` 失败时**工具结果没被换掉** —— "
          "🔴 修之前是 `apply_l1 → record` 且不判返回值：一次 SQLite 失败就会留下"
          "「storage=L1 / authority=L0」，而 storage 已经变小 → 下一轮不会再重试，"
          "**只有一条 warning，行为继续跑**")


def t_system_note_is_not_user_speech() -> None:
    print("\n[7] ⭐⭐ 系统注记不许在提炼输入里被标成 `[user]`")
    from core.context import digest as D
    from core.context.exchange import split
    msgs = [M("user", "真人说的话"),
            M("user", "[System check-in] you put this in background",
              visible_to_user=False),
            M("assistant", "回复")]
    ex = split(msgs)[0]
    txt = D.exchange_text(ex)
    check("[user] 真人说的话" in txt, "⭐ 真人那句仍标 [user]")
    check("[user] [System check-in]" not in txt,
          "⭐⭐⭐ 系统注记**没有**被标成 [user] —— "
          "🔴 否则提炼器会写下「用户亲口说：This message originally carried 1 image…」")
    check("[system note]" in txt,
          "⚠️ 但也**没有删掉它** —— 删了提炼器会看不懂上下文")


def t_recall_is_reachable_by_model() -> None:
    print("\n[8] 🔴 ⑤ `recall_conversation` **模型真的够得到**")
    import core.orchestrator as O
    from core.tools.builtin import build_builtin_definitions

    _m = getattr(O, "_RECALL_CONVERSATION_MANIFEST", None)
    check(isinstance(_m, dict) and _m.get("name") == "recall_conversation",
          "⭐⭐ manifest 存在（`*_MANIFEST` 全局会被自动收集）")
    check(hasattr(O.NanoAgent if hasattr(O, "NanoAgent") else O.Orchestrator
                  if hasattr(O, "Orchestrator") else object,
                  "_handle_recall_conversation")
          or any("_handle_recall_conversation" in n for n in dir(O)),
          "⭐⭐ handler 方法真实存在（binding 写错的话运行时才炸）")

    _mans = {v["name"]: v for k, v in vars(O).items()
             if k.endswith("_MANIFEST") and isinstance(v, dict) and v.get("name")}
    defs = {d.name: d for d in build_builtin_definitions(_mans)}
    check("recall_conversation" in defs, "⭐⭐⭐ **注册进了工具目录**")
    _d = defs.get("recall_conversation")
    check(_d is not None and _d.preload.name == "CORE",
          "⭐⭐ preload=CORE —— 📌 索引被无条件注入 system，"
          "而 DEFERRED 要模型先 load_tools 才看得见它",
          str(_d.preload if _d else None))

    class _RT:
        def __init__(self, v): self.v = v
        def has_evicted_history(self): return self.v
        def has_live_work(self): return False
        def has_open_interaction(self): return False
        def is_recheck_round(self): return False
        def has_unsummarized_image(self): return False

    check(_d is not None and not _d.availability(_RT(False)),
          "⭐ 没有 L3 记录时它**不出现**（有工具没事实 = 让模型猜）")
    check(_d is not None and _d.availability(_RT(True)),
          "⭐⭐⭐ 有 L3 记录时它**出现** —— "
          "🔴 这正是原来缺的那一腿：system 承诺了一个不存在的出口")


def t_no_second_eviction_rule() -> None:
    print("\n[9] ⭐⭐ L3 索引不许有「固定 60 条」这第二套淘汰规则")
    import inspect
    from core.context.decay import l3_index_lines
    _p = inspect.signature(l3_index_lines).parameters
    check("limit" not in _p,
          "⭐⭐⭐ 签名里**没有** `limit` —— "
          "🔴 原来 `limit=60` 与 L3 token 配额是两套上限：第 61 条以前的记录"
          "行为上已经等于 L4，权威却还说它是 L3，**而且没有任何记录**。"
          "📌 同一件事有两个淘汰规则时，输的那个会无声地赢",
          str(list(_p)))

    # ⚠️ 用一个只回答「现在还算数的记录是哪些」的桩 —— 这一项验的是
    #    **不再有第二套上限**，不是 `active_entries` 的陈旧判定（那是第 4 项）。
    class _Stub:
        def active_entries(self, _sid):
            return {i: {"level": "L3", "index_entry": f"〔条目{i}〕"}
                    for i in range(80)}

    check(len(l3_index_lines(_Stub(), "s")) == 80,
          "⭐⭐ 80 条 L3 就返回 80 条（该少注入就让它真的降 L4，那条路径有日志有权威）",
          str(len(l3_index_lines(_Stub(), "s"))))


def t_distill_max_tokens_reaches_provider() -> None:
    print("\n[10] ⭐ `max_tokens` 真的传得下去（原来被 `**_` 吃掉）")
    import inspect
    from core.provider import ClaudeProvider as _P
    for _n in ("chat_without_tools", "chat_without_tools_or_call"):
        _f = getattr(_P, _n, None)
        check(_f is not None and "max_tokens" in inspect.signature(_f).parameters,
              f"⭐⭐ `{_n}` 签名里有 `max_tokens` —— "
              f"📌 **一个被 `**_` 吞掉的参数比没有这个参数更糟**："
              f"没有的话调用方会去查，被吞掉的话调用方以为它生效了")

    from core.context.decay import _DISTILL_MAX_TOKENS
    from core.context.digest import MAX_OUTCOME_CHARS, MAX_REFERENTS, MAX_ITEM_CHARS
    _worst = MAX_OUTCOME_CHARS + (MAX_REFERENTS + 4) * MAX_ITEM_CHARS
    check(_DISTILL_MAX_TOKENS > _worst,
          "⭐⭐ 上限比 schema 的最坏情况大 —— "
          "🔴 接通之后 700 会把一条**完全合法**的结论行截断 → JSON 不完整 → "
          "白花一次调用继续留 L1，而日志只说「提炼失败」",
          f"schema 最坏 ≈{_worst} 字 / 上限 {_DISTILL_MAX_TOKENS}")


def t_distill_payload_survives_the_provider() -> None:
    print("\n[12] 🔴🔴 提炼的输入**真的到得了 provider**（不是「我调了它」）")
    import asyncio
    from core.context.decay import _distill_one
    from core.provider import ClaudeProvider
    from core.context.exchange import split

    seen = {}

    class _Probe:
        """不是"接受一切"的桩 —— 它按 **真 provider 的规矩** 检查收到的东西。"""
        async def chat_without_tools(self, context, system_guide, *a, **kw):
            seen["ctx"] = context
            seen["sys"] = system_guide
            seen["max_tokens"] = kw.get("max_tokens")
            # ⭐ 用**真的那个** `_merge_context`，而不是自己想象它要什么
            seen["merged"] = ClaudeProvider._merge_context(context)
            return '{"status":"chat","outcome":"x","referents":[],"open_items":[]}', "m"

    ex = split([M("user", "把检索阈值调低"), M("assistant", "改好了")])[0]
    asyncio.get_event_loop().run_until_complete(
        _distill_one(_Probe(), ex, ["上一条结论"], "anthropic/claude-haiku-4.5"))

    _merged = seen.get("merged") or []
    check(bool(_merged) and all("content" in m for m in _merged),
          "⭐⭐⭐ 过完 `_merge_context` 之后每条消息都还有 `content` —— "
          "🔴 原来传的是 `{'role':..,'parts':[..]}`（Gemini 形状），"
          "`_merge_context` 读的是 `msg['content']` → **整段提炼输入凭空消失**，"
          "实测表现是 `fresh=40 tokens`、三次提炼全败在「outcome 为空」",
          str([sorted(m) for m in _merged]))
    _text = " ".join(str(m.get("content")) for m in _merged)
    check("把检索阈值调低" in _text,
          "⭐⭐⭐ 用户原话**真的在发出去的那份里**（不是只在我构造的对象里）")
    check("上一条结论" in _text or "上一条结论" in str(seen.get("sys") or ""),
          "⭐ 低分辨率上文也在")
    # ⚠️ schema 契约在 **user** 那半（system 只有一句角色声明）—— 第一版断言
    #    写的是"system > 200 字"，那是原以为的分工，不是代码的分工。
    check(len(str(seen.get("sys") or "")) > 60 and "JSON only" in str(seen.get("sys")),
          "⭐ 系统提示词非空且说清了「只输出 JSON」",
          f"{len(str(seen.get('sys') or ''))} 字")
    check(all(k in _text for k in ("status", "outcome", "referents", "open_items")),
          "⭐⭐ **四个字段的契约真的在发出去的那份里** —— "
          "📌 契约丢了模型照样会答，只是答一个自己编的 schema")
    check(seen.get("max_tokens"),
          "⭐ `max_tokens` 确实传出去了", str(seen.get("max_tokens")))


def t_source_forbids_static_only() -> None:
    print("\n[11] ⭐ 本文件自己的纪律：**不许只读源码**")
    # ⚠️⚠️ **不许用"源码里搜不到 ast.parse"来验这件事** —— 第一版就是那么写的，
    #    然后被这条断言**自己的说明文字**喂红了（正文里就有 "ast.parse" 这几个字）。
    #    🔴 这是本项目同一形状的第五次（源码换行 / 关键词缺席 / docstring / 注释 / 这次）。
    #    📌 **只要断言读的是文本，对代码的【解释】就会参与判定。**
    # ⭐ 改成查这个模块的命名空间：没 import 过就没有这个名字，文字喂不进来。
    check("ast" not in globals(),
          "⭐⭐⭐ 本模块**从没 import 过 `ast`** —— "
          "📌 断言读代码的【形状】时，形状可以完全正确而它一跑就炸；"
          "那个 `get_store` 就是这么活过 28 项断言的")
    check("pathlib" in globals(), "（对照）确实在查命名空间，不是恒真")


def main() -> int:
    for fn in (t_bridge_actually_runs, t_l2_is_really_projected, t_l3_evicts_live,
               t_stale_falls_back_to_raw, t_l2_footprint_counts_user_words,
               t_l1_persists_before_projecting, t_system_note_is_not_user_speech,
               t_recall_is_reachable_by_model, t_no_second_eviction_rule,
               t_distill_max_tokens_reaches_provider, t_distill_payload_survives_the_provider,
               t_source_forbids_static_only):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f"{fn.__name__} 抛异常", str(e))
    bad = [r for r in _results if not r[0]]
    # ⚠️ 汇总必须是 `run_tests.sh` 认得的两种格式之一（`X/Y 通过`），
    #    否则它会报「无汇总」并整体判失败 —— 📌 一个跑绿了但汇报格式不对的套件，
    #    在全量里和红的没有区别。
    print(f"\n{'=' * 60}\n结果：{len(_results) - len(bad)}/{len(_results)} 通过")
    for _, n, note in bad:
        print(f"  FAIL  {n}" + (f"   [{note}]" if note else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
