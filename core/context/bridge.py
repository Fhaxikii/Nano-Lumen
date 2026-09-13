"""L3 交接 —— 把一条 L2 结论行**交给语义记忆**，并**验证真的拿得回来**。

═══ 🔴 为什么它必须排在 eviction【之前】 ═══

绝不能：

    L2 内容先移出上下文  →  再写语义记忆  →  bridge 失败
    →  **内容没了，召回也没有**

必须：

    产生可召回内容 → 持久化 → **验证确实检索得到** → 生成 L3 索引 → 最后才 commit L3
    任何一步失败 → **继续留在 L2**

📌 **fail-closed 的方向由「失败时谁受损」决定** ——
   这里失败的代价是"多背一会儿上下文"（便宜），
   而反方向失败的代价是"内容永久消失且没人知道"（不可逆）。

═══ ⚠️ 用新的 `memory_type = "exchange"`，不复用现有两种 ═══

立这一条时回代码核实过：`semantic_memories` 当时只有两种类型在用 ——
`task_pattern`（任务模式）与 `correction`（纠错）。

🔴 **一次对话交换的结论两者都不是。** 硬塞进 `task_pattern` 会让
`semantic_memory.py` 里那条按 `memory_type="task_pattern"` 检索的召回腿
**开始返回对话摘要** —— 一个正在正常工作的机制被污染了，而且不会报错。

📌 与本层另一处同源：**schema/类型会反过来污染事实**；
   两个不同的问题就该有两个不同的类型，交接处做**显式映射**，不是共用。

═══ ⚠️ 两条腿，一条 fail-closed 一条 best-effort ═══

    SQLite (`semantic_memories`)   **权威** —— 写不进去就整个失败
    向量库 (`memory_index`)        **联想召回** —— 失败只降级，响亮告警

📌 理由：L3 索引条目本身就是一条召回路径（它无条件进 system 动态段），
   它不依赖向量。向量挂了只是"联想召回变弱"，不是"这段记忆没了"。
⚠️ 但**必须告警** —— 一个安静降级的召回腿，会让人以为召回率天生就这样。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from loguru import logger

# ⭐ 新类型。⚠️ 不复用 `task_pattern` / `correction`（见模块头）。
MEMORY_TYPE = "exchange"

# L3 索引条目的长度上限。⚠️ 它会**无条件进 system 动态段**，几十条一起 ——
# 📌 一个"小到可以一直背着"的东西，一旦不小了，它就不再是索引而是负担。
MAX_INDEX_CHARS = 110

# 🔴 索引条目里那句"可 recall"必须**指名道姓**。
#    原来写的是泛指的「可 recall」，而模型手里唯一带 recall 字样的工具是
#    `recall_working_memory` —— 它查的是 `working_memory` 那张表，
#    **根本不是** `semantic_memories` 里 `memory_type="exchange"` 的这些。
#    于是形成一个非常隐蔽的**假能力**：
#        system 说「这件事可以 recall」→ 模型去调 recall_working_memory
#        → 查的是另一张表 → 查不到 → 模型只好说"我想不起来了"
#    📌 **一句提示词承诺的能力，必须能用它自己给出的名字调到。**
RECALL_TOOL = "recall_conversation"


def canonical_text(digest: dict, user_said: str) -> str:
    """写进语义记忆的正文。**结论 + 用户原话**。

    ⚠️ 带上用户原话是刻意的：📌 实测一整段会话里真人打的字只占 0.32%，
       而它是**唯一不可再生**的部分 —— 到了这一层再丢，就真的没有了。
    ⚠️ 但要截断：这是"能被向量检索到"的载体，不是原文备份
       （原文备份在 `conversation_messages`，永远都在）。
    """
    _o = (digest.get("outcome") or "").strip()
    _r = "、".join(digest.get("referents") or [])
    _u = (user_said or "").strip()[:600]
    parts = [f"结论：{_o}"]
    if _r:
        parts.append(f"涉及：{_r}")
    if digest.get("open_items"):
        parts.append("未决：" + "；".join(digest["open_items"]))
    if _u:
        parts.append(f"用户当时说：{_u}")
    return "\n".join(parts)


def index_line(digest: dict, when: float | None = None) -> str:
    """L3 索引条目 —— **一行**，会被无条件注入 system 动态段。

    形如：`〔08-03 定了 RAG 归档公式（乘法版），可 recall〕`

    ⚠️⚠️ **措辞质量直接决定召回率** —— 这不是修辞，是机制：
       索引是 Nano **唯一**知道"这儿曾经有东西"的途径
       （参照 `MEMORY.md`：索引不是被回忆起来的，是被塞进来的）。
       📌 **索引条目写砸 = 那条记忆等于不存在。**
    ⚠️ 所以它必须带**定位信息**，不许留代词 —— 这一条已经在
       `digest.outcome` 的硬要求里堵过一次，这里只是别把它丢掉。
    """
    _d = time.strftime("%m-%d", time.localtime(when or time.time()))
    _o = (digest.get("outcome") or "").strip()
    _r = (digest.get("referents") or [])
    _tail = f"（{_r[0]}）" if _r else ""
    _body = f"{_o}{_tail}"
    # ⚠️ 上限量的是**整行**，不是正文 —— 📌 一个只管正文的上限，会在外面每加一个
    #    固定后缀时静默变松（`RECALL_TOOL` 这次就把整行从 ~110 顶到 140）。
    #    而这一行是**每轮无条件注入 system 的**，它有多少条就乘多少倍。
    _fixed = len(f"〔{_d} ，可 {RECALL_TOOL}〕")
    _room = max(16, MAX_INDEX_CHARS - _fixed)
    if len(_body) > _room:
        _body = _body[:_room - 1] + "…"
    return f"〔{_d} {_body}，可 {RECALL_TOOL}〕"


def hand_off(digest: dict, user_said: str, *, source_ref: str = "") -> str | None:
    """把一条结论交给语义记忆，**并验证拿得回来**。成功返回 `memory_id`，失败返回 `None`。

    ⚠️ **返回 None 时调用方必须让那次交换继续留在 L2** —— 见模块头。
    """
    _text = canonical_text(digest, user_said)
    if not _text.strip():
        logger.warning("[Bridge] 结论为空，拒绝交接（该交换继续留在 L2）")
        return None

    mid = f"ex_{uuid.uuid4().hex[:16]}"
    # ── ① 写权威（SQLite）—— 失败即整体失败 ──
    # 🔴🔴 这里原来写的是 `from core.memory_store import get_store` —— **那个名字
    #    不存在**（真名 `get_memory_store`）。于是 `hand_off` 每次都 ImportError →
    #    被 except 吞掉 → 返回 None → 所有交换永远卡在 L2，**L3/L4 整条腿是死的**。
    #
    # ⚠️ 它为什么能活过 28 项测试：`t_f5_bridge_l3.py` 里验 `hand_off` 的四项
    #    **全是 `ast.parse` 源码结构检查**（调用先后、关键字参数），
    #    **没有一项真的调用过这个函数**。
    # 📌 断言从「读源码文本」改成「读 AST」，是为了躲开
    #    「断言被自己的注释喂红」；结果换来一个更糟的形状：
    #    **断言读的是代码的【形状】，不是代码的【行为】** ——
    #    形状可以完全正确，而它一跑就炸。
    # ⭐ 所以本次同时补了 `t_f5_bridge_live.py`：**真的调一次。**
    from core.memory_store import get_memory_store
    try:
        store = get_memory_store()
        store.add_semantic_memory(
            memory_id=mid, memory_type=MEMORY_TYPE,
            canonical_text=_text,
            display_summary=(digest.get("outcome") or "")[:120],
            # ⚠️ 显式映射，**不是** 把 L2 的字段硬塞进语义记忆的 ontology：
            #    `task_intent` 这里记的是"这次交换讲了什么"，
            #    而 `wrong_assumption` / `correct_behavior` 一律留空 ——
            #    📌 一次普通对话不是一次"纠错"，硬填就是让 schema 编事实。
            task_intent=(digest.get("outcome") or "")[:200],
            target_entity=(digest.get("referents") or [""])[0][:120],
            condition_text="来自对话历史的交换结论",   # 不许真空（该表的规矩）
            confidence=0.3,
            source_event_ids=[source_ref] if source_ref else [],
        )
    except Exception as e:
        logger.warning(f"[Bridge] 写语义记忆失败 → 该交换继续留在 L2: {e}")
        return None

    # ── ② 验证真的读得回来（权威侧）——「写进去了」和「拿得回来」是两件事 ──
    try:
        if get_memory_store().get_semantic_memory(mid) is None:
            logger.error(f"[Bridge] 🔴 刚写的 {mid} 读不回来 → 继续留在 L2")
            return None
    except Exception as e:
        logger.warning(f"[Bridge] 回读校验失败 → 继续留在 L2: {e}")
        return None

    # ── ③ 向量腿：best-effort，失败只降级但**必须响亮** ──
    try:
        from core import memory_index
        memory_index.upsert(mid, _text, MEMORY_TYPE)
        _hit = any(h.get("id") == mid for h in
                   memory_index.search(_text[:200], memory_type=MEMORY_TYPE, top_k=5))
        if not _hit:
            # ⚠️ 不算失败（索引条目那条腿还在），但**不许安静** ——
            #    📌 一个安静降级的召回腿，会让人以为召回率天生就这样。
            logger.warning(f"[Bridge] ⚠️ {mid} 写进向量库后**检索不到自己** —— "
                           f"联想召回这条腿失效，L3 索引条目仍然有效")
    except Exception as e:
        logger.warning(f"[Bridge] 向量腿失败（不影响交接）: {e}")

    logger.info(f"[Bridge] 交接完成 {mid} ← {(digest.get('outcome') or '')[:40]}")
    return mid


def rollback(memory_id: str) -> None:
    """把一次**没走完**的交接撤掉。永不抛。

    ⚠️ 用在这个窗口：`hand_off` 成功了，但后面的 `index_line` / `decay.record`
       失败 → 那次交换保持 L2，下一轮会**重新交接一次**。
       不撤的话，每失败一轮就在语义记忆里多一条**没人引用的孤儿**，
       而它们内容几乎相同 —— 召回时会互相稀释。
    📌 **fail-closed 只保证「没提交的不算数」，不会替你收拾「已经写下去的」。**
    """
    try:
        from core.memory_store import get_memory_store
        get_memory_store().soft_delete_semantic_memory(memory_id)
    except Exception as e:
        logger.debug(f"[Bridge] 回滚 {memory_id} 失败（留下一条孤儿）: {e}")
    try:
        from core import memory_index
        memory_index.delete(memory_id)
    except Exception:
        pass


def recall(query: str, top_k: int = 5) -> list[dict]:
    """按语义找回被移出上下文的交换。**只搜 `exchange` 这一类。**

    ⚠️ 限定类型是刻意的：不限定的话它会把 `task_pattern` / `correction`
       一起捞出来，📌 而那两类回答的是**另外两个问题**。
    """
    hits = []
    try:
        from core import memory_index
        hits = memory_index.search(query, memory_type=MEMORY_TYPE, top_k=top_k) or []
    except Exception as e:
        logger.debug(f"[Bridge] 向量召回失败，走兜底: {e}")
    if hits:
        return hits

    # 🔴 **不许只有向量这一条腿。** `hand_off` 里已经写明向量是 best-effort
    #    （挂了只是联想召回变弱）—— 那么 `recall` 就不能把它当唯一入口，
    #    否则"best-effort"实际上变成了"单点故障"。
    # ⚠️ 2026-08-14 实际运行中发现：某些机器上 `memory_index.search` 因为 torch 版本
    #    直接抛异常，于是 `recall_conversation` **恒返回空** —— 而 system 里的
    #    索引条目仍然在告诉模型"这件事可以召回"。
    # 📌 **一个 best-effort 的组件，不能是某条承诺的唯一实现。**
    try:
        from core.memory_store import get_memory_store
        rows = get_memory_store().list_semantic_memories(
            memory_type=MEMORY_TYPE, include_disabled=False, limit=200) or []
        _q = [w for w in str(query).split() if w] or [str(query)]
        scored = []
        for r in rows:
            _t = str(r.get("canonical_text") or "")
            _n = sum(1 for w in _q if w and w in _t)
            # ⚠️ 逐字匹配不到时用**字符重合**兜一层：中文查询往往整句进来，
            #    切不出词。宁可召回得糙，也别让这条腿也交白卷。
            if not _n:
                _n = sum(1 for ch in set(str(query)) if len(ch.strip()) and ch in _t)
                _n = _n / max(1, len(set(str(query))))
            if _n:
                scored.append((_n, r))
        scored.sort(key=lambda t: -t[0])
        out = [r for _, r in scored[:top_k]]
        if out:
            logger.info(f"[Bridge] 向量召回不可用 → 走 SQLite 兜底，命中 {len(out)} 条")
        return out
    except Exception as e:
        logger.warning(f"[Bridge] recall 两条腿都失败: {e}")
        return []
