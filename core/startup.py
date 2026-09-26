# -*- coding: utf-8 -*-
"""启动时的呈现、健康状态消费、初始化进度：后端按顺序发事件，界面只渲染（S6-6b 第 7 步）。

- `present_startup(agent)`：一次性，按顺序发出
    ① 上次运行的崩溃留痕（故障卡）
    ② 关软件时还排在队列里的用户消息（`unsent_message`，只呈现、不执行，呈现后出队）
    ③ 「我重启前还挂着在等 …」（有活着的等待时）
    ④ 上个进程留下的活 → 由模型生成一句询问要不要接着做（生成失败就不说）
  顺序的理由：系统陈述事实在前、Nano 开口在后；「你上次还有话没说完」在「要不要接着做」之前。
- `health_tick()`：周期 drain 健康登记表的状态转移 → 系统事件（给模型）、故障卡
  （只有「不可用」出卡，多项合并成一张）、`faults_recovered`（界面撤卡）。
- `watch_init_progress(agent)`：把 RAG 初始化的阶段日志与就绪信号转成事件
  （`init_facts` / `init_stage` / `init_ready`），初始化遮罩据此推进。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from loguru import logger

_interrupted: list = []


def set_interrupted(details: list) -> None:
    """启动恢复认定的「上个进程被中断的活」（`reconcile_on_startup` 的 interrupted_details）。"""
    global _interrupted
    _interrupted = list(details or [])


def _publish(ev: dict) -> None:
    from core.runtime import events
    events.publish(ev, None)


# ── 启动呈现 ──────────────────────────────────────────────────────────────
def _present_crash_journal() -> None:
    """上一个进程的崩溃留痕（segfault / os._exit 靠 breadcrumb 留下）。"""
    try:
        from core import crash_journal
        recs = crash_journal.startup_scan()
    except Exception as e:
        logger.debug(f"[CrashJournal] 启动扫描跳过: {e}")
        return
    if not recs:
        return
    from core.backend import emit_chat_event
    emit_chat_event(
        category="fault",
        title="上次运行没有正常退出",
        lines=[r.get("summary", "") for r in recs[:5]],
        hints=["这是上一个进程的记录，当前这次的能力状态以本次重新探测为准。"],
        dedupe_key="crash:" + ",".join(r.get("id", "") for r in recs[:5]),
    )
    try:
        from core.health import get_system_events
        for r in recs[:5]:
            get_system_events().add(r.get("summary", ""))
        crash_journal.mark_presented([r.get("id", "") for r in recs])
    except Exception:
        pass


def _present_unsent() -> None:
    """关软件时还排在队列里的用户消息 —— 呈现，不执行（关闭软件 = 默认放弃这次协同）。

    呈现后出队：留着 PENDING 的话，下一次排空会把它当成要执行的消息。
    不进模型上下文、不写聊天记录（它说明的是上次关闭时的状态）。
    唤醒意图不呈现（不是用户打的字），只丢弃；上个进程没干完的活由 ④ 问。
    """
    try:
        from core.runtime import inbox as _ib
        items = _ib.list_unfinished(limit=50)
    except Exception as e:
        logger.warning(f"[L14] 读未处理队列失败（跳过）: {e}")
        return
    shown = 0
    for it in items or []:
        try:
            if getattr(it, "kind", "") != _ib.ItemKind.USER_MESSAGE:
                _ib.discard(it.item_id, "重启后丢弃（非用户消息）")
                continue
            _d = it.detail or {}
            _publish({"event": "unsent_message", "text": it.body or "",
                      "had_image": bool(_d.get("had_image"))})
            _ib.discard(it.item_id, "重启后已呈现给用户")
            shown += 1
        except Exception as e:
            logger.warning(f"[L14] 呈现 {getattr(it, 'item_id', '?')} 失败: {e}")
    if shown:
        logger.info(f"[L14] 重启后呈现了 {shown} 条未处理的消息（未执行，已出队）")


async def _present_live_waits() -> None:
    """重启后还活着的等待：告诉用户 Nano 仍记得在等什么（定时源由轮询接管，
    用户源在用户下次说话时由 orchestrator 恢复）。"""
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import waitcond as _wc
        left = _wc.list_live(get_kernel(), oldest_first=True)
    except Exception:
        left = []
    if left:
        from core.backend import speak
        txt = "、".join(r.reason for r in left)
        await speak(f"（我重启前还挂着在等：{txt}。需要的话直接跟我说一声就能接着来。）")


async def _present_resume_offer(agent: Any) -> None:
    """上个进程留下的活：把事实交给模型，由它开口问一句（生成失败就不说）。

    不推断「是崩了还是正常关的」：把事实说出来，让用户答。气泡里的话由模型生成。
    """
    details = list(_interrupted or [])
    if not details:
        return
    try:
        from core.runtime.scheduler import startup_resume_notice
        facts = startup_resume_notice(details)
        if not facts:
            return
        from core.i18n import language_clause
        content, _ = await agent.provider.chat_without_tools(
            context=[{"role": "user", "content": facts}],
            system_guide=("You are Nano. Speak in your own voice, 1-2 short sentences.\n"
                          + language_clause("your line") + "\n"),
        )
        content = (content or "").strip()
        if not content:
            return
        from core.backend import speak
        await speak(content)
        logger.info(f"[B1] 重启后已就 {len(details)} 件未完成的活开口询问")
    except Exception as e:
        logger.warning(f"[B1] 重启后询问生成失败 → 本次不开口: {e}")


async def present_startup(agent: Any) -> None:
    """启动呈现，按固定顺序（见模块说明）。每一段失败都不挡后面的。"""
    for step in (_present_crash_journal, _present_unsent):
        try:
            step()
        except Exception as e:
            logger.warning(f"[Startup] {step.__name__} 失败: {e}")
    try:
        await _present_live_waits()
    except Exception as e:
        logger.warning(f"[Startup] 活着的等待提示失败: {e}")
    await _present_resume_offer(agent)


# ── 健康登记表消费 ────────────────────────────────────────────────────────
def health_tick() -> None:
    """drain 健康登记表的状态转移（登记 + 轮询：故障可能发生在事件循环存在之前）。"""
    try:
        from core.health import get_health, get_system_events, Transition, Status, Severity
    except Exception:
        return
    h = get_health()
    trans = h.drain_transitions()
    if not trans:
        return
    sysev = get_system_events()
    to_card, recovered = [], []
    for t in trans:
        st = t.state
        label = st.snapshot().get("label", st.capability)
        # 给模型：所有转移都记（含 degraded 与 recovered）
        if t.kind == Transition.RECOVERED:
            recovered.append(st.capability)
            sysev.add(f"{label} recovered and is available again.")
        elif st.status == Status.DEGRADED:
            sysev.add(f"{label} degraded: {st.user_message}")
        else:
            sysev.add(f"{label} became unavailable: {st.user_message}")
        # 给用户：只有「不可用」出卡；降级只进监控面板（让监控卡说真话，不往聊天区塞）
        if t.kind in (Transition.OPENED, Transition.UPDATED) and \
                st.status == Status.UNAVAILABLE and st.presented_at is None:
            if h.mark_presented(st.capability, st.generation):
                to_card.append(st)
    if recovered:
        _publish({"event": "faults_recovered", "capabilities": recovered})
    if not to_card:
        return
    from core.backend import emit_chat_event
    # 多个组件同时失败时归并成一张卡
    to_card.sort(key=lambda s: Severity.rank(s.severity), reverse=True)
    if len(to_card) == 1:
        s = to_card[0]
        emit_chat_event(category="fault",
                        title=f"{s.snapshot().get('label', s.capability)} 不可用",
                        lines=[s.user_message], hints=[s.recovery_hint],
                        dedupe_key=s.fingerprint, capabilities=[s.capability])
    else:
        emit_chat_event(category="fault",
                        title=f"检测到 {len(to_card)} 项能力不可用",
                        lines=[f"{s.snapshot().get('label', s.capability)}：{s.user_message}"
                               for s in to_card],
                        hints=[s.recovery_hint for s in to_card],
                        dedupe_key="|".join(s.fingerprint for s in to_card),
                        capabilities=[s.capability for s in to_card])


# ── 初始化进度 ────────────────────────────────────────────────────────────
async def watch_init_progress(agent: Any, poll: float = 0.15) -> None:
    """RAG 初始化（后台线程写阶段日志）→ 事件：先发一次 `init_facts`（启动时已能读到的
    真实数据），之后每出现一个新阶段发 `init_stage`，就绪时发 `init_ready`。"""
    try:
        from core import rag as rag_engine
        from core.registry import registry
    except Exception as e:
        logger.warning(f"[Init] 初始化进度无法读取: {e}")
        _publish({"event": "init_ready"})
        return
    try:
        skills = len(registry.get_all_manifests())
    except Exception:
        skills = 0
    try:
        chunks = rag_engine.get_stats().get("total_chunks", 0)
    except Exception:
        chunks = 0
    _publish({"event": "init_facts", "skill_count": skills,
              "proxy": bool(os.getenv("AI_PROXY") or os.getenv("HTTPS_PROXY")),
              "chunk_count": chunks})
    ready = getattr(agent, "_rag_ready", None)
    sent = 0
    while True:
        try:
            stages = list(rag_engine.get_init_stage_log())
        except Exception:
            stages = []
        for st in stages[sent:]:
            _publish({"event": "init_stage", "stage": st})
        sent = max(sent, len(stages))
        if ready is None or ready.is_set():
            break
        await asyncio.sleep(poll)
    # 就绪前最后写进去的阶段（如 `done:…`）再补发一次
    try:
        for st in list(rag_engine.get_init_stage_log())[sent:]:
            _publish({"event": "init_stage", "stage": st})
    except Exception:
        pass
    _publish({"event": "init_ready"})
