# core/orchestrator/react_loop.py
"""ReAct 主循环：决策流、循环本体、停止与插话、最终答复流。（`Orchestrator` 的 mixin）"""

import asyncio
import time

from loguru import logger

from core.orchestrator._runtime import (
    _RT_PATH,
    _rt_abort_open_span,
    _rt_lease_acquire,
    _rt_lease_release,
    _rt_shadow_commit,
    _rt_shadow_note,
    _rt_shadow_open,
    _rt_shadow_prepare,
)
from core.orchestrator._types import StreamDecisionState, ToolExecution
from core.orchestrator.screen import _vision_model_for_os
from core.schema import AgentDecision, ToolResultBlock
from core.tools import Flow, ToolScope


# ── WriteSkill 伪工具声明 ────────────────────────────────────────────────
# ⭐⭐ UI 消费循环遇到这些事件就 `return` —— 排在它们后面的任何 yield 都进不了界面。
#
# ⚠️ 这份清单**被猜错过两次**：
#   第一次只写了 `final_result` → `create_new_skill` 的卡片一直转圈
#     （它结束在 `skill_preview` 上）；
#   第二次补了 `sys_error` 仍然漏 `skill_preview` / `user_note_pending`。
#
# 所以不再手抄，而是用 `tests/cases/t_exit_tool_card.py` 的 AST 不变量把它和
# `app.py` 里真实的 `if step.get("event") == X: … return` 绑死 ——
# 以后谁加第五个终端事件，测试会先红。
# 📌 判据：**常量表必须能被证明等于真实分发链，而不是靠人记得同步。**
_UI_TERMINAL_EVENTS = frozenset({
    "final_result",
    "sys_error",
    "skill_preview",
    "user_note_pending",
    # ⭐ 第五个（2026-08-08，无缝对话的协作式中断）。
    #    ⚠️ **它就是上面那句「以后谁加第五个终端事件，测试会先红」的兑现** ——
    #       加完 app 侧那个 `return` 分支就忘了这张表，
    #       `t_exit_tool_card` 的 AST 不变量当场红了。
    #    📌 那条不变量比它的作者预期的更值 —— 它不只防「手抄漏了」，
    #       还防「**加功能的人根本不知道有这张表**」。
    "turn_interrupted",
})


# OS 能力说明（双循环合一后：os_execute 始终注入主 ReAct 循环，OS 是无差别内置能力）。
# "你随时可以用屏幕能力，按需调用，可与其它工具自由组合"，注入到每一轮主决策的 guide 里。
_OS_CAPABILITY_PROMPT = (
    # ⭐⭐ [2026-08-23 拆分] 标题不再只挂 `os_execute`，并且**点名两个工具的分工**。
    #    📌 一个还在宣称自己能点鼠标的注入块，会让模型带着错误的能力预期规划整轮。
    "\n\n[Computer and Screen Capability]\n"
    "Two separate tools, and picking the right one matters:\n"
    "  - os_execute: always available. Files, commands, clipboard, launching apps, reading "
    "system and window state. It does NOT touch the graphical interface.\n"
    "  - computer_use: load it first. Mouse, keyboard, scrolling, dragging, screenshots, "
    "and minimizing/closing/switching windows.\n"
    "Reach for os_execute first: a command or a file write is faster and far more reliable "
    "than driving the screen. Use computer_use when there is genuinely no other way.\n"
    "Call one action at a time, observe the result, then decide the next step. "
    "They may be combined with other tools.\n"

    # ⚠️⚠️ **两条绑定关系，强度不同 —— 拆开说**。
    #    🔴 旧文把 click / typing / screenshot / app launch / window switching
    #       写成**同一条硬规则**，于是「截图前必须先缩小」变成了仪式：
    #       多一次往返、还把自己无谓地缩小了。
    #    📌 「Nano 此刻碍不碍事」只有模型看得见上下文 ——
    #       合我们那条分工：**系统答发生了什么，模型答所以怎么办。**
    "Get out of your own way - two rules, and they are NOT equally strict:\n"
    "  - Mouse, keyboard, or window actions: call set_window_mode('mini') FIRST. Your own "
    "window sits on the screen you are about to operate, and you will hit the wrong thing. "
    "This one is not optional.\n"
    # 🔴 [2026-08-24] 同上，遮罩已删。这是漏掉的第二处。
    "  - Screenshots: shrinking usually helps, because Nano minimizes itself out of the "
    "shot and at full size it would otherwise cover a large part of the screen. But judge it "
    "yourself: if you are not covering what matters (the target app is fullscreen in front, "
    "or on another monitor), just take the shot. Do not shrink as a ritual.\n"
    "Never shrink for pure command-line work, file-only work, KB queries, or memory "
    "queries - and never when the task is to observe or operate Nano's own UI.\n"

    "Use eyes with restraint: call look_at_screen only when the screen state is genuinely uncertain, "
    "or after a key operation when failure would mislead later steps. Do not screenshot before every step. "
    "After one look, reuse that visual understanding for several steps unless the screen meaningfully changes. Nano's own window is excluded by default.\n"

    "KB files are not the same as real computer files. query_local_knowledge, load_full_file, get_file_path, and list_knowledge_files only handle KB files. "
    "For real files on the desktop or arbitrary disk paths, use os_execute list_dir, file_read, or file_write. "
    "Do not call get_file_path for a real desktop file such as 'desktop 1.txt'.\n"

    "Waiting for external changes: if progress depends on something Nano cannot complete now, such as login, download completion, or another person's reply, "
    "use wait_for instead of blocking or pretending to wait in the same turn."
)
# 注：这里【曾经】还有一条 "Soft abort" 规则（用户说不想让 Nano 动电脑 → 模型发
# request_replan(USER_ABORT) → 置进程级软急停标志）。软急停整套已删除，
# 原因见 core/os_layer/safety.py 模块头。用户要停手，靠甩鼠标 failsafe 或 Ctrl+`
# 热键，两者都是瞬发的物理手段，比让模型判断可靠得多。


class ReactLoopMixin:
    """ReAct 主循环：决策流、循环本体、停止与插话、最终答复流。"""

    # ── ReAct 主循环常量 ──────────────────────────────────────────────────
    REACT_MAX_ROUNDS = 30         # 单次用户消息最多执行轮数。OS 多步任务（每点一下/

                                  # 输入一次屏幕就占一轮）8 轮远不够，且会卡住缩窗后
                                  # 没机会调 full 恢复。放宽到 30（对齐 Claude Code 的多轮风格）。
    MAX_TOOLS_PER_ROUND = 4       # 单轮最多并发工具数（防止 token 爆炸）

    async def _stream_with_window_guard(self, context, tools_manifest, system_guide):
        """发不出去时：**紧急回收旧历史 → 重试一次 → 还不行就如实说**。

        ⚠️ **只重试一次。** 📌 一个「失败就回收一点再试」的循环，如果没有次数
           上限，它会在真正装不下的那一天把整段历史磨掉，**然后仍然失败** ——
           而那时用户既没得到答案，也没有了历史。
        🔴 `active_too_big`（这一轮本身就装不下）**根本不重试**：
           📌 回收多少历史都没用，重试一次只是多花一次时间去撞同一堵墙。
        """
        from core.context.guard import ContextWindowExceeded as _CWE

        async def _once():
            async for _e in self.provider.chat_with_tools_stream(
                    context, tools_manifest, system_guide, model_override=None,
                    # ⭐ 缓存断点：前 N 个是无条件核心工具，每轮都一样。
                    #    它后面（条件工具 / `load_tools` 追加的）变了也不伤前缀。
                    stable_tool_count=getattr(self, "_core_stable_n", -1)):
                yield _e

        try:
            async for _e in _once():
                yield _e
            return
        except _CWE as _ex:
            if _ex.kind == "active_too_big":
                logger.error("[Guard] 这一轮输入本身就装不下 —— 不重试（回收也没用）")
                yield {"event": "final_result", "content": _ex.user_message,
                       "status": "SYS_IDLE"}
                return
            _short = max(1, int(_ex.predicted) - int(_ex.limit))
            logger.warning(f"[Guard] 被拒发，缺 {_short:,} token → 紧急回收后重试一次")

        _got = 0
        try:
            from core.context.decay import emergency_reclaim as _er
            from core.context.decay_store import DecayStore as _DS
            from core.runtime.kernel import get_kernel as _gk
            _sid = self.memory.conversation_session_id
            if _sid:
                _got = _er(self.memory, _DS(_gk().store), _sid,
                           self.provider.target_model, need=_short)
        except Exception as _ee:
            logger.warning(f"[Guard] 紧急回收不可用: {_ee}")

        if _got <= 0:
            # ⚠️ 一个 token 都没腾出来 → 别再发一次去撞同一堵墙。
            #    📌 重试的前提是「这次和上次不一样」，而这里没有任何不同。
            from core.context.guard import MSG_STILL_TOO_BIG as _M
            yield {"event": "final_result", "content": _M, "status": "SYS_IDLE"}
            return

        # ⚠️ 回收改的是 `memory.storage`，而 `context` 是这一轮**早就拼好**的
        #    那份快照 —— 不重新取的话，重试发出去的还是原来那么大。
        #    📌 **在一个快照上做完修改，然后把快照原样再发一次，
        #       等于什么都没修。**
        try:
            context = self.memory.get_full_context()
        except Exception:
            pass

        try:
            async for _e in _once():
                yield _e
        except _CWE as _ex2:
            logger.error(f"[Guard] 🔴 回收 {_got:,} token 之后仍然装不下 —— 拒发")
            yield {"event": "final_result", "content": _ex2.user_message,
                   "status": "SYS_IDLE"}

    async def _stream_decision_core(
        self,
        context,
        tools_manifest,
        system_guide,
        *,
        task_type: str,
        stage_label: str,
        state: StreamDecisionState,
        block_id: str | None = None,
        block_started: bool = False,
        emit_done: bool = True,
        event_scope: dict | None = None,
        allow_multi_tools: bool = False,
    ):
        """_stream_decision 的核心实现，结果写入局部 StreamDecisionState 而非实例属性。

        这样多个并发调用之间不会互相覆盖（实例属性在并发场景下有竞态风险）。
        原 _stream_decision 改为调用此方法的 wrapper，旧代码无需修改。
        """
        _scope = event_scope or {}
        _bid = block_id or f"{stage_label}_{time.time_ns()}"
        _started = block_started
        _first_delta = True
        decision = None
        used_model = self.target_model if hasattr(self, "target_model") else None
        notice_text = ""
        _answer_bid: str | None = None

        # ⭐ **在发出请求这一刻，快照本次 request 的 tool contract。**
        #
        # 🔴🔴 判据必须是"这一次 request 实际 attach 了哪些 schema"，
        #    **不能**用 `_core_manifest + _pending_loaded_manifests` 重算 ——
        #    那个方向刚好反了：`_pending_loaded_manifests` 是**中转缓冲区**，
        #    本批执行完就 append 进 `tools_manifest` 然后**立刻清空**
        #    （本文件 `_run_react_loop` 里那段"按需加载：把本批 load_tools 匹配到的 schema
        #    并入 tools_manifest"）。于是下一步模型真正拿到 `CORE + 刚 load 的`，
        #    而那个缓冲区已经空了 —— 拿它当判据会**把刚刚合法 load 成功的工具拦掉**。
        #
        # ⭐ 而 `tools_manifest` 本身就是那份合同：它在 turn 内累积，且**就是**
        #    发给 provider 的那个列表。所以在这里取一份 frozenset 即可。
        # 📌 **request-local contract —— 不需要 ScopeContext，不需要落库。**
        #
        # ⚠️ 挂在 `decision` 上而不是 `self`：`_stream_decision_core` 刻意改造成
        #    用局部 `StreamDecisionState` 就是为了避免并发时实例属性互相覆盖，
        #    往 `self` 上写等于把那次改造又拆掉。
        #    📌 **一个"属于某次请求"的事实，就该挂在那次请求的产物上。**
        _active_tool_names = frozenset(
            (m.get("name") or "") for m in (tools_manifest or []) if isinstance(m, dict)
        )

        # 重启提示是否还挂着 —— 用来判断这次请求要不要消费它。
        try:
            from core.runtime import identity as _rt_ident
            _had_restart_notice = bool(_rt_ident.pending_restart_notice())
        except Exception:
            _rt_ident, _had_restart_notice = None, False
        _model_really_spoke = False

        # ⭐⭐ 硬窗口守卫的**处置侧**。
        #    provider 只回答「发不发得出去」；**回收历史的权力在这一层** ——
        #    📌 provider 不该碰 memory：一个为了少写一层而拉进来的依赖，
        #       会让「谁有权改什么」这件事从此说不清。
        async for _ev in self._stream_with_window_guard(
            context, tools_manifest, system_guide):
            # ⭐ 消费判定：**"模型确实收到了"，不是"我们拼进了 system_guide"。**
            #    任一真实模型流事件出现 → 它一定读到了 system prompt。
            #    ⚠️ `provider_error` 不算（见下面 done 分支）：API 层 400 / 网络失败时
            #       模型一个字都没看到，这时标成"已说过"= 这次重启永远不会被告知。
            if _ev["type"] in ("notice_delta", "answer_delta", "text_delta",
                               "tool_input_delta"):
                _model_really_spoke = True
            if _ev["type"] == "notice_delta":
                if not _started:
                    _started = True
                    yield {"event": "thought_block_start", "block_id": _bid,
                           "stage_label": stage_label, "status": "CORE_THINKING",
                           "server_start_time": time.time(), **_scope}
                elif _first_delta:
                    yield {"event": "thought_delta", "block_id": _bid, "delta": "\n", **_scope}
                _first_delta = False
                yield {"event": "thought_delta", "block_id": _bid, "delta": _ev["text"], **_scope}

            elif _ev["type"] == "answer_delta":
                # parallel_branch scope 内不把 delta 当最终答案流给前端
                if _scope.get("scope") == "parallel_branch":
                    continue
                if _answer_bid is None:
                    _answer_bid = f"answer_{time.time_ns()}"
                    yield {"event": "final_text_start", "block_id": _answer_bid, **_scope}
                yield {"event": "final_text_delta", "block_id": _answer_bid, "delta": _ev["text"], **_scope}

            elif _ev["type"] == "answer_discard":
                if _answer_bid is not None:
                    yield {"event": "final_text_discard", "block_id": _answer_bid, **_scope}
                    _answer_bid = None

            elif _ev["type"] == "tool_input_delta":
                yield {**_ev, **_scope}

            elif _ev["type"] == "done":
                decision = _ev.get("decision")
                used_model = _ev.get("model", used_model)
                notice_text = _ev.get("notice", "")
                # 旧路径不支持多工具：内部用非流式调用重试，强制单工具
                if (decision is not None
                        and decision.decision_type == "call_many"
                        and not allow_multi_tools):
                    logger.warning(f"[StreamDecision] 旧路径收到 call_many，内部重试单工具")
                    _retry_ctx = list(context) + [{
                        "role": "user",
                        "content": "[System note] This stage allows only one tool call. Choose only the first tool that must run now. Do not call multiple tools in this retry.",
                    }]
                    try:
                        _retry_d, used_model = await self.provider.chat_with_tools(
                            _retry_ctx, tools_manifest, system_guide,
                            # ⚠️ 与主路径同一份 `tools_manifest`，所以用同一个数。
                            stable_tool_count=getattr(self, "_core_stable_n", -1),
                        )
                        if _retry_d.decision_type == "call_many":
                            # 仍返回多工具：强制取第一个
                            _first = _retry_d.tool_calls[0]
                            decision = AgentDecision("call", tool_calls=[_first],
                                                     thinking_blocks=_retry_d.thinking_blocks)
                        elif _retry_d.decision_type in ("call", "text"):
                            decision = _retry_d
                        else:
                            decision = AgentDecision("single_tool_retry", content="")
                    except Exception as _re:
                        logger.error(f"[StreamDecision] 单工具内部重试失败: {_re}")
                        decision = AgentDecision("single_tool_retry", content="")
                if decision is not None:
                    decision.answer_bid = _answer_bid
                if _started and emit_done:
                    yield {"event": "thought_block_done", "block_id": _bid,
                           "full_text": notice_text, "status": "CORE_THINKING",
                           "model": used_model, **_scope}
                    _auto_summary = (notice_text.strip().split('\n')[0])[:10] if notice_text else ""
                    yield {"event": "thought_summary", "block_id": _bid,
                           "summary": _auto_summary, "translation": "", **_scope}

        # ⭐ 把这次 request 的 tool contract 钉在它自己的产物上。
        #    执行层据此判断"这个调用属不属于产生它的那次请求"。
        #    ⚠️ 用 `object.__setattr__` 不必要（AgentDecision 是普通类），直接赋值。
        if decision is not None:
            try:
                decision.active_tool_names = _active_tool_names
            except Exception:
                pass   # 不影响主流程：拿不到就退化成"不校验"（见执行侧的 fail-open 说明）

        # ⭐ 消费重启提示。
        # ⚠️⚠️ `provider_error` **不算模型收到过** —— 它是 `done` 事件里的一个
        #    `decision_type`，不是独立事件类型（`provider.py` 的 400 / 网络失败分支
        #    都是 `yield {"type": "done", "decision": AgentDecision("provider_error", …)}`）。
        #    只看 `done` 会把"请求根本没成功"当成"模型已经被告知过重启"，
        #    于是**这次重启永远不会被说出来**。
        if _had_restart_notice and _rt_ident is not None:
            _dtype = getattr(decision, "decision_type", "") if decision is not None else ""
            if _model_really_spoke or (decision is not None and _dtype != "provider_error"):
                _rt_ident.consume_restart_notice()

        state.decision = decision
        state.model = used_model or "UNKNOWN"
        state.notice = notice_text
        state.block_started = _started
        state.block_id = _bid
        state.answer_bid = _answer_bid

    async def _stream_decision(self, context, tools_manifest, system_guide, *,
                                task_type: str, stage_label: str,
                                block_id: str | None = None,
                                block_started: bool = False,
                                emit_done: bool = True):
        """向后兼容 wrapper。内部委托给 _stream_decision_core，结果同时写入
        实例属性（供旧代码路径使用）和局部 state 对象。

        新代码（ReAct 主循环）直接用 _stream_decision_core + 局部 StreamDecisionState，
        不依赖实例属性，无并发竞态风险。
        """
        state = StreamDecisionState()
        async for ev in self._stream_decision_core(
            context, tools_manifest, system_guide,
            task_type=task_type,
            stage_label=stage_label,
            state=state,
            block_id=block_id,
            block_started=block_started,
            emit_done=emit_done,
        ):
            yield ev

        # 兼容旧调用方（文件工具链、Skill 创建等）通过实例属性读结果
        self._last_stream_decision = state.decision
        self._last_stream_model = state.model
        self._last_stream_notice = state.notice
        self._last_stream_block_started = state.block_started
        self._last_stream_block_id = state.block_id
        self._last_stream_answer_bid = state.answer_bid

    def _ensure_react_sems(self):
        """懒初始化并发信号量（必须在 asyncio 事件循环内调用）。"""
        if self._rag_parallel_sem is None:
            self._rag_parallel_sem = asyncio.Semaphore(2)
        if self._tool_parallel_sem is None:
            self._tool_parallel_sem = asyncio.Semaphore(4)

    def _react_loop_prompt(self) -> str:
        """ReAct protocol injected into system_guide."""
        return (
            "\n\n[ReAct Tool Use Protocol]\n"
            "After each reasoning step, choose one of:\n"
            "1. Answer the user directly.\n"
            "2. Call one tool.\n"
            "3. Call multiple independent tools in the same round.\n\n"

            "When NOT to use tools (important):\n"
            "- For casual conversation, greetings, opinions, explanations, advice, planning, or reasoning about "
            "content already in the conversation, just answer directly (option 1). Do not call any tool.\n"
            "- Only reach for a tool when the task genuinely needs real execution: file access, computer/screen/"
            "browser control, live data (time/weather/news/etc.), memory recall/write, Skills, attachments, waiting, or scheduling.\n"
            "- Do not call memory, knowledge, or note tools just to respond to small talk.\n\n"

            "Dependency rule:\n"
            "- If tool B needs the result of tool A, split them across rounds: call A first, wait for the result, then decide whether to call B.\n"
            "- If tools are independent parallel lookups, reads, or calculations, they may be called in the same round.\n"
            "- After every tool result, reason from the result before calling another tool or giving the final answer.\n\n"

            "Tool boundary discipline:\n"
            "- load_tools is the default way to confirm or activate capabilities that are not currently in the active tool schema.\n"
            "- Preloaded tools and router tool_names are hints, not commands.\n"
            "- Never invent tool names, action names, parameter names, or enum values.\n"
            "- If you are unsure about a tool name, action, parameter, or enum value, call load_tools first instead of guessing.\n"
            "- os_execute may only use actions listed in its schema. set_window_mode and look_at_screen are standalone tools, not os_execute actions.\n"
            "- If a tool call fails due to unknown action, tool not found, invalid enum, unexpected parameter, or missing required parameter, "
            "the next step should inspect/load the correct schema, not guess another name.\n"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 用户手动终止 —— **和「无缝对话的中断」不是同一件事**
    # ══════════════════════════════════════════════════════════════════════
    # 🔴 **早先写过「终止按钮不单独修，等后台任务那一项做完自然消失」——那条是错的。**
    #
    # 📌 **两个机制的出口方向相反**：
    #    · 无缝对话的中断：出口是「**带着新输入继续决策**」→ 它还会接着做事
    #    · 终止：出口是「**停下，别做了**」→ 它不该接着做事
    #    做完前者**不会**自动得到后者 —— 它们答的不是同一个问题。
    #    ⚠️ 这正是本轮反复出现的那个形状：
    #       **拿一个为别的目的定义的机制，去回答一个它答不了的问题。**
    #
    # 三处差别都是硬的：
    #    | | 无缝中断 | 终止 |
    #    |---|---|---|
    #    | 触发 | 用户**打字** | **零输入**（空输入框点一下）|
    #    | 判定 | 过**模型**（它决定怎么办）| **确定性**，不过模型 |
    #    | 出口 | 继续 | 停止 |
    #
    # ⭐⭐ **决定性的论据**：如果想让它停，而唯一的办法是打字告诉它「停」，
    #    那么**模型可能不停** —— 它可能理解成别的意思、或者觉得该先收个尾。
    #    📌 **一个「停止」能力如果依赖被停止的那一方理解你的意思，
    #       它就不是停止能力。**
    #
    # ⭐ 好消息：**检查点已经在正确的位置**（都在「这一轮还没往历史里写任何东西」
    #    之前），所以终止复用它们**零额外风险**。

    def request_stop(self, source: str = "user") -> None:
        """用户点了终止。**只置一个意图，不做任何清理** ——
        真正的停止发生在最近的那个检查点上。

        ⚠️ 为什么不在这里直接动手：**动作与动作之间**才是安全的停止边界
        （早先已认过的物理限制：已经在执行中的那一个动作停不下来）。
        📌 在这里硬砍，就会重新引入悬空的调用记录 → 400。
        """
        self._stop_requested = True
        logger.info(f"[Stop] 收到终止请求（{source}）—— 将在下一个动作边界停下")

    def clear_stop(self) -> None:
        """新一轮开始时清掉。⚠️ **必须清** —— 否则上一次的终止会把下一轮也杀掉。"""
        self._stop_requested = False

    def _stop_asked(self) -> bool:
        return bool(getattr(self, "_stop_requested", False))

    def _user_interjected(self) -> bool:
        """用户在本轮进行中又说话了吗？

        ⚠️ **fail-safe 方向是「没有」** —— 读不出来就当没插话，让本轮正常跑完。
           反过来错的代价是**无故中断一个好好的回答**，那比晚一点响应糟得多。
           📌 与 `inbox.has_work()` 相反（那边错成 True 只是白检查一次），
              **方向由代价决定，不由习惯决定。**
        """
        base = getattr(self, "_turn_input_baseline", None)
        if base is None:
            return False
        try:
            from core.runtime import inbox as _ib_seq
            return _ib_seq.submit_seq() > int(base)
        except Exception:
            return False

    def _interject_stop_event(self, round_idx: int, where: str,
                              stopped: bool = False) -> dict:
        """构造「本轮被用户插话中断」这个事件。

        ⚠️⚠️ **这里刻意不做任何补偿动作，因为不需要** ——
        两个检查点都放在「**这一轮还没往历史里写任何东西**」的位置：
        `stream` 阶段一个字都不写进 memory（写点只有空决策 / 文字答案 /
        `add_tool_calls` 三处），而检查点 B 就在 `add_tool_calls` **之前**。
        📌 **中断点要选在「还没写进历史」的位置 —— 那样根本不需要任何补偿逻辑。**
           选在写入之后就得发明一套收尾（补 `tool_result`、防悬空 `tool_use` 的 400）；
           选在写入之前，那套收尾**根本不存在**。
        ⭐ 所以 用户那个例子里 `GetSystemTime` **压根没被执行** ——
           模型带着两条消息重新决策时，它不会自相矛盾。
        """
        # ⚠️ **两种原因必须分开**，不许压成一个「被中断了」：
        #    · 插话 → 队列里有东西，马上带着新输入重新决策（**还会继续**）
        #    · 终止 → 用户要它**停**，不该有下一段
        #    📌 出口方向相反的两件事，不能共用一个 reason ——
        #       下游要据此决定「接着跑」还是「收尾」。
        if stopped:
            logger.info(f"[Stop] 第{round_idx + 1}轮在「{where}」被用户终止 —— "
                        f"本轮不写历史、不执行工具，且**不接着跑**")
        else:
            logger.info(f"[Interject] 第{round_idx + 1}轮在「{where}」被用户插话中断 —— "
                        f"本轮不写历史、不执行工具，交给下一轮带着两条消息重新决策")
        return {
            "event": "turn_interrupted",
            "reason": "user_stopped" if stopped else "user_interjected",
            "stopped": bool(stopped),
            "where": where,
            "round": round_idx + 1,
            "status": "SYS_IDLE",
            "log": (f"用户终止 → 第{round_idx + 1}轮停止（{where}）" if stopped
                    else f"用户插话 → 第{round_idx + 1}轮中断（{where}）"),
            "current_skill": None,
        }

    async def _run_react_loop(
        self,
        *,
        tools_manifest: list,
        system_guide: str,
        base_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
    ):
        """ReAct 主循环。替代 _handle_query_impl 常规路径里的"一次决策 + 巨型分支树"。

        循环结构：
          思考(stream_decision) → 工具行动(批量执行) → 结果写回 memory → 继续思考
          直到模型输出文字答案或超过最大轮数。
        """
        used_model = getattr(self, "target_model", "UNKNOWN")
        tool_data_out = None
        # ReAct 协议是稳定文本，插到 CACHE_BREAK_MARKER 前，避免落入动态区导致缓存失效。
        react_guide = self._insert_before_cache_break(system_guide, self._react_loop_prompt())
        self._pending_loaded_manifests = []   # 按需加载：本轮 load_tools 待并入的 schema

        for round_idx in range(self.REACT_MAX_ROUNDS):
            # ⭐⭐ [协作式中断 · 检查点 A] 动作边界：上一轮的工具往返都已闭合，
            #    历史是干净的 —— 直接停，什么都不用补。
            #    ⚠️ 这一处覆盖的是「ReAct 中途插话」（用户问的那个更复杂情况）。
            if self._stop_asked():
                yield self._interject_stop_event(round_idx, "轮开头", stopped=True)
                return
            if round_idx > 0 and self._user_interjected():
                yield self._interject_stop_event(round_idx, "轮开头")
                return

            # ── 1. 模型思考/决策 ─────────────────────────────────────────
            state = StreamDecisionState()
            context = self._build_pipeline_context()

            try:
                async for ev in self._stream_decision_core(
                    context, tools_manifest, react_guide,
                    task_type="ReAct",
                    stage_label=f"ReAct第{round_idx + 1}轮",
                    state=state,
                    emit_done=True,
                    allow_multi_tools=True,
                ):
                    yield ev
                    # ⭐⭐ [检查点 0] **stream 进行中**也要看 —— 否则模型正在生成
                    #    一段长回答时，点终止要等它写完，那不叫终止。
                    #    ⚠️ 中途 break 是**安全**的，理由和另两个检查点完全一样：
                    #       **stream 阶段一个字都不写进历史**（写点只有空决策 /
                    #       文字答案 / `add_tool_calls` 三处，全在 stream 之后）。
                    #       📌 中断点选在「还没写进历史」的位置 → 不需要补偿逻辑。
                    #    ⭐ 插话也走这一条：没必要等一个**已经不作数**的 stream 跑完
                    #       —— 那纯粹是白烧 token 和时间。
                    if self._stop_asked() or self._user_interjected():
                        _st6 = self._stop_asked()
                        yield self._interject_stop_event(
                            round_idx, "模型生成中", stopped=_st6)
                        return
            except RuntimeError as fatal_err:
                if "API 调用链路全线熔断" in str(fatal_err):
                    self._clean_damaged_memory()
                    yield {"event": "sys_error", "content": "🚨【熔断】API全线枯竭！",
                           "status": "CRITICAL_SYSTEM_HALT", "model": "TOTAL_CRASH",
                           "skills_active": False, "log": "CRITICAL: API链路彻底枯竭",
                           "current_skill": None}
                    return
                raise

            decision = state.decision
            used_model = state.model or used_model

            if decision is None:
                content = "这轮模型没有返回有效决策。"
                self.memory.add_message("assistant", content)
                yield {"event": "final_result", "content": content, "model": used_model,
                       "status": "SYS_IDLE", "log": "ReAct loop：空决策终止。",
                       "current_skill": None, "rag_hit": self._rag_hit_this_turn,
                       "full_file_hit": self._full_file_hit_this_turn}
                return

            # ── 1.5. provider_error：400/异常不当普通回答，清理 memory 并终止 ──
            if decision.decision_type == "provider_error":
                status_code = getattr(decision, "error_status_code", 0)
                if status_code == 400:
                    # 覆盖表第 3 行。⭐ 顺便验预判 B：
                    # provider 调用在【轮首思考阶段】、batch 在【轮中】，所以 400 发生时
                    # 上一轮 batch 早已关闭（置 False 在 raise 之前）→ 这个守卫可能【几乎恒真】，
                    # 即等于没有守卫。先用 shadow 拿证据，不要凭推断提前删它（[L4] 同款纪律）。
                    _rt_shadow_note(_RT_PATH.ERROR_400,
                                    f"open_batch={self._active_tool_batch_open}"
                                    f"（shadow 已证实这里几乎恒为 False，见 [F1] 预判 B）")
                    if not self._active_tool_batch_open:
                        # 当前没有进行中的工具批次，说明是历史坏 pair 导致的 400
                        # 尝试修复历史内存，而不只是清理当前批次
                        repaired = self.memory.repair_invalid_tool_turns()
                        if repaired:
                            logger.warning("[ReAct] 历史坏 tool pair 已修复，下次请求应可恢复。")
                    self._clean_damaged_memory()
                logger.error(f"[ReAct] provider_error status={status_code}: {decision.content}")
                yield {"event": "sys_error", "content": decision.content,
                       "status": "ERROR", "model": used_model,
                       "log": f"provider_error {status_code}",
                       "current_skill": None}
                return

            # ── 2. 文字答案：ReAct 结束 ──────────────────────────────────
            if decision.decision_type in ("text", "web_text") and not decision.tool_calls:
                final_content = decision.content or ""
                self.memory.add_message("assistant", final_content)
                yield {
                    "event": "final_result",
                    "content": final_content,
                    "model": used_model,
                    "status": "SYS_IDLE",
                    "log": f"ReAct loop：完成（第{round_idx + 1}轮）。",
                    "current_skill": None,
                    "rag_hit": self._rag_hit_this_turn,
                    "full_file_hit": self._full_file_hit_this_turn,
                    "tool_data": tool_data_out,
                    "block_id": getattr(decision, "answer_bid", None),
                }
                return

            # ── 3. 工具调用 ──────────────────────────────────────────────
            if decision.is_tool_action:
                # ⭐⭐⭐ [协作式中断 · 检查点 B] **这一处才是 用户那个例子的正解。**
                #
                #    模型已经决定要调工具（`tool_use` 已在 decision 里），
                #    但**还没写进历史、更没执行**。此刻停 → 那次调用彻底不存在。
                #    ⚠️ 位置极其关键：必须在 `add_tool_calls` **之前** ——
                #       `tool_use` 只通过它进历史，所以放在前面就
                #       **不会留下悬空 `tool_use`，零 400 风险**。
                #    📌 **中断点要选在「还没写进历史」的位置** ——
                #       那样根本不需要补偿逻辑（补 `tool_result` 那套压根不存在）。
                #
                #    ⭐ 于是「现在几点？→（正要调 GetSystemTime）→ 算了别调了」
                #      的结果是：**工具没跑**，模型带着两条消息重新决策，不自相矛盾。
                if self._stop_asked():
                    yield self._interject_stop_event(round_idx, "工具执行前",
                                                     stopped=True)
                    return
                if self._user_interjected():
                    yield self._interject_stop_event(round_idx, "工具执行前")
                    return
                calls = decision.tool_calls

                # 这一轮的目录与运行时事实 —— exit 判定、handler 解析都问它。
                _cat = self._get_tool_catalog()
                _rtv = self._tool_runtime_view()

                # 检查是否有需要退出 ReAct 循环的特殊工具。
                # ⭐ 判据从「名字在不在 `_REACT_EXIT_TOOLS` 这张手写表里」换成
                #    「这个工具声明的 `flow` 是不是 EXIT_REACT」——
                #    同一个问题，问的是唯一权威。
                # ⚠️ 用 `resolve(...) is not None` 一起把「此刻 eligible」也验了，
                #    否则可能挑出一个当前不该出现、随后又解析不到 handler 的工具
                #    （那正是 「看得见执行不了」的形态）。
                def _is_exit_call(c) -> bool:
                    _d = _cat.get(c.name)
                    return (_d is not None and _d.flow is Flow.EXIT_REACT
                            and _cat.resolve(c.name, ToolScope.MAIN, _rtv) is not None)

                exit_call = next((c for c in calls if _is_exit_call(c)), None)
                if exit_call:
                    if len(calls) > 1:
                        # exit 工具和其他工具同轮出现：全部写入 memory + error result，让 Claude 下轮重来
                        #
                        # ⚠️ 注意这一段【从头到尾没有碰过 _active_tool_batch_open】。
                        # 但它确实开了又关了一个真实的工具批次（add_tool_calls + add_tool_results）。
                        # 这是 shadow 覆盖表第 5 行，也是我们开工前就预判"必然分歧、且旧实现输"的那条。
                        #  这是开工前就预判过的一条。
                        _sp = _rt_shadow_prepare(
                            self, round_idx, [c.name for c in calls], _RT_PATH.EXIT_MULTI)
                        norm_calls = self.memory.add_tool_calls(calls, thinking_blocks=decision.thinking_blocks)
                        _rt_shadow_open(self, _sp, [c.tool_use_id for c in norm_calls])
                        err_msg = (
                            f"This round contains an exit-flow tool \"{exit_call.name}\". "
                            "It cannot be executed in the same round with other tools. "
                            "In the next round, call only that exit-flow tool, or finish other read-only checks first and call it separately."
                        )
                        self.memory.add_tool_results([
                            ToolResultBlock(
                                name=c.name,
                                tool_use_id=c.tool_use_id,
                                content=err_msg,
                                is_error=True,
                            )
                            for c in norm_calls
                        ])
                        _rt_shadow_commit(self, _sp, [c.tool_use_id for c in norm_calls],
                                          _RT_PATH.EXIT_MULTI)
                        ok_fmt, why_fmt = self.memory.validate_tool_turns()
                        if not ok_fmt:
                            self._rollback_last_tool_batch()
                            raise RuntimeError(f"ReAct exit-multi memory 格式损坏：{why_fmt}")
                        continue  # 让 Claude 下轮重新决策

                    # ⭐ [2026-08-06 实测] exit 工具也要发工具卡片。
                    #
                    # 用户两次报"创建 Skill 时没有工具卡片"。⑥ 删掉关键词快路径之后
                    # 流程确实走主循环了（日志可证），但卡片**仍然没出现** ——
                    # 因为 exit 工具**从设计上就不经过 `_execute_tool_batch`**：
                    # 它们是路由信号，在批次执行【之前】就被摘出来分派了，
                    # 而 `tool_start` / `tool_end` 是在批次执行里发的。
                    #
                    # 所以之前看到的 `加载能力: create_new_skill ✓` 是 **load_tools 的卡片**
                    # （它是普通工具），不是 `create_new_skill` 自己的。
                    #
                    # 📌 判据：**"绕过主流程"必然连带绕过主流程的可观测性。**
                    # 这和 ⑥ 那条快路径是同一个形状 —— 只不过这次绕过的是
                    # 批次执行而不是整个循环。设计原则 4 要求新工具能被卡片显示，
                    # 那就不能有一整类工具天然不发卡片。
                    #
                    # ⚠️⚠️ 必须 `yield`，**绝对不能 `event_queue.put`**。
                    #
                    # 第一版就是写成 `await event_queue.put(...)`，实测
                    # （17:56:48 那轮）**完全没有卡片**，
                    # 而且没有任何异常 —— put 成功了，只是没人取。
                    #
                    # 📌 根因：`event_queue` 的排空循环在**工具批次那一段里**
                    #    （见下方"并发执行工具，同时 drain event_queue"）。
                    #    exit 工具在到达那段之前就路由走并退出循环了，
                    #    于是事件永远躺在队列里。
                    #
                    # 也就是说 —— 「exit 工具绕过批次执行」这件事，
                    # **连带绕过的不只是发卡片，还有取卡片**。第一次只修了发的那半。
                    #
                    # 判据：`event_queue.put` 只在"调用方保证会 drain"的地方成立；
                    #      在生成器自己手里，`yield` 才是无条件到达 UI 的那条路。
                    def _drain_queue():
                        # 实现已提成 `_drain_event_queue`，因为
                        # exit 适配器（`_exit_*`）也要用它 —— 一个闭包拿不出去。
                        return self._drain_event_queue(event_queue)

                    # 先把本轮 LLM 调用期间攒下的事件放出去，再发 exit 卡片，
                    # 否则状态行的顺序会倒过来（卡片先于"模型已就绪"）。
                    for _q in _drain_queue():
                        yield _q

                    _exit_aid = f"exit_{exit_call.tool_use_id or exit_call.name}"
                    yield {
                        "event": "tool_start", "action_id": _exit_aid,
                        "tool_name": exit_call.name,
                        "tool_use_id": getattr(exit_call, "tool_use_id", "") or "",
                        "action_display": _cat.presentation(
                            exit_call.name, exit_call.args or {}),
                        "action_input": str(exit_call.args or {})[:200],
                        "log": f"ReAct: 进入专属流程【{exit_call.name}】",
                        "status": "TOOL_EXECUTING", "model": used_model,
                        "current_skill": exit_call.name,
                        "tool_use_id": exit_call.tool_use_id,
                    }
                    # ⚠️ `tool_end` **不在这里发** —— 见下方 `return` 之前那处。
                    #
                    # 第一版紧挨着 `tool_start` 就发了，结果卡片显示
                    # `[✓] 1 tool · 0.0s`（2026-08-06 当场看出来）。
                    # exit 工具真正的"耗时"是它路由过去之后那整段流程
                    #（探索、写代码、重新生成…），不是路由本身那一瞬。
                    # 📌 一个耗时显示成 0.0s 的卡片，比没有卡片更误导。

                    # ⭐⭐ exit 流程里有些分支不该自己开口。
                    #
                    # 2026-08-06 指出：`_handle_answer_interaction` 的
                    # 拒绝分支直接 `yield final_result` 一句写死的中文。
                    # 那句话**完全不受人格模板影响** —— 用户把 Nano 的人设改成别的，
                    # 满屏都是那个语气，中间突然蹦出一句
                    #「批准要对得上具体哪一版」，等于人格分裂。
                    # 项目里这类固定文案有 **35 处**（已清点，）。
                    #
                    # 机制：handler 想让模型自己说时，yield 一个
                    # `exit_flow_defer_to_model` 事件（带一段**英文事实描述**），
                    # 这里把它拦下来 —— 不给 UI，而是写成 tool_result 喂回记忆，
                    # 然后 `continue` 让 ReAct 循环再跑一轮，由模型用它自己的话说。
                    #
                    # 📌 同一条判据：**给出足够的事实，让它自己判断怎么说、
                    #    下一步做什么**，而不是替它把话说死。
                    _defer_to_model: str | None = None

                    # ⭐⭐ [2026-08-06 实测] `tool_end` 必须赶在**终端事件之前**发出。
                    #
                    # 上一版把它挪到"专属流程跑完之后"，想让时长真实。结果三个症状：
                    #   · pill 永远停在运行态（金黄 + 没有秒数，和正常卡片长得不一样）
                    #   · 明细行的转圈**永不停止**
                    #   · 收起后也不出对勾/叉子
                    #
                    # 📌 根因在消费端：`app.py` 里
                    #      `if step.get("event") == "final_result": … return`
                    #    —— 子流程（探索 / 部署 / 重新生成）自己就会 yield 终端事件
                    #    结束这一轮，消费者当场 return，**排在它后面的任何事件都进不了 UI**。
                    #
                    # 判据：**生成器 yield 出去 ≠ 消费者会处理。**
                    #   终端事件是一条单向门 —— 收尾类事件必须在门关上之前发。
                    _tool_end_sent = False

                    def _mk_tool_end():
                        nonlocal _tool_end_sent
                        _tool_end_sent = True
                        return {
                            "event": "tool_end", "action_id": _exit_aid,
                            "result_summary": "已完成" if not _defer_to_model else "需要我来说明",
                            "status": "CORE_THINKING", "model": used_model,
                            "current_skill": exit_call.name,
                            "tool_use_id": exit_call.tool_use_id,
                            "ok": _defer_to_model is None,
                        }

                    def _pre(ev):
                        """要在 `ev` 之前补发的事件。`ev` 是终端事件时补 tool_end。

                        ⚠️ 终端事件有**五个**，见 `_UI_TERMINAL_EVENTS`。
                        漏掉哪个，那条路径的卡片就会永远停在转圈态。
                        """
                        if (not _tool_end_sent and isinstance(ev, dict)
                                and ev.get("event") in _UI_TERMINAL_EVENTS):
                            return [_mk_tool_end()]
                        return []

                    def _take_defer(ev):
                        """返回 True 表示这个事件被拦下（不转发给 UI）。"""
                        nonlocal _defer_to_model
                        if isinstance(ev, dict) and ev.get("event") == "exit_flow_defer_to_model":
                            _defer_to_model = str(ev.get("tool_result") or "").strip() or None
                            return True
                        return False

                    # ⭐⭐ 只有 exit 工具：**问目录谁来处理**，然后退出循环。
                    #
                    # 🔴 这里改造前是 `if exit_call.name == "update_existing_skill":
                    #    elif … elif …` 五段分派 —— 那是**第二份按工具名建立的事实**，
                    #    正是 要消灭的东西（的 AST 断言现在守着它不许回来）。
                    #    每一支各自那点专属逻辑已经**逐字**搬进 `_exit_*` 适配器，
                    #    行为零变化；这里只剩「谁处理」这一个问题，而它由 Catalog 回答。
                    #
                    # ⚠️ 公共 plumbing 一行没动，仍在这里：`_take_defer` 拦截
                    #    `exit_flow_defer_to_model`、`_pre` 在终端事件前补 `tool_end`、
                    #    以及下面那段 defer 的 memory 三步写入。
                    #    📌 **Catalog 决定"谁处理"，Runner 决定"怎么运行"** —— 分层没变。
                    _exit_ref = _cat.resolve(exit_call.name, ToolScope.MAIN, _rtv)
                    if _exit_ref is None:
                        # 走不到：`exit_call` 本来就是从「flow 是 EXIT_REACT 且此刻
                        # eligible」里挑出来的。留一条响亮日志，别静默什么都不做 ——
                        # 静默的表现是「模型调了工具，然后什么都没发生」。
                        logger.error(
                            f"[F4] exit 工具 {exit_call.name!r} 解析不到 handler —— "
                            f"这不该发生（挑它出来的判据和这里是同一个）")
                    else:
                        async for _ev in getattr(self, _exit_ref)(
                            exit_call, decision,
                            used_model=used_model, base_guide=base_guide,
                            realtime_callback=realtime_callback,
                            event_queue=event_queue,
                        ):
                            # ⚠️ `_take_defer` 改造前**只挂在 answer_open_interaction
                            #    那一支**上。这里统一挂 —— 可证等价：全项目只有
                            #    `_handle_answer_interaction` 会 yield 这个事件
                            #    （已 grep 核实，唯一 emitter）。
                            if _take_defer(_ev):
                                continue          # 不转发给 UI，交给模型去说
                            for _e in _pre(_ev):
                                yield _e
                            yield _ev

                    # 流程没走终端事件就结束（比如 defer 那条）→ 这里补上收尾。
                    if not _tool_end_sent:
                        yield _mk_tool_end()

                    if _defer_to_model:
                        # handler 把事实交回来了 → 写成 tool_result，让模型自己组织语言。
                        # ⚠️ 记忆写法必须和 EXIT_MULTI 那段一致（同样的 shadow 三步 +
                        #    格式自检），否则下一轮直接 400：assistant 的 tool_use
                        #    必须紧跟成对的 tool_result。
                        _sp = _rt_shadow_prepare(
                            self, round_idx, [exit_call.name], _RT_PATH.EXIT_MULTI)
                        _norm = self.memory.add_tool_calls(
                            [exit_call], thinking_blocks=decision.thinking_blocks)
                        _rt_shadow_open(self, _sp, [c.tool_use_id for c in _norm])
                        self.memory.add_tool_results([
                            ToolResultBlock(name=c.name, tool_use_id=c.tool_use_id,
                                            content=_defer_to_model, is_error=False)
                            for c in _norm
                        ])
                        _rt_shadow_commit(self, _sp, [c.tool_use_id for c in _norm],
                                          _RT_PATH.EXIT_MULTI)
                        _ok_fmt, _why_fmt = self.memory.validate_tool_turns()
                        if not _ok_fmt:
                            self._rollback_last_tool_batch()
                            raise RuntimeError(f"ReAct exit-defer memory 格式损坏：{_why_fmt}")
                        logger.info(
                            f"[ReAct] exit 流程把措辞交回模型（{exit_call.name}）："
                            f"{_defer_to_model[:80]}"
                        )
                        continue      # ⭐ 不 return —— 让模型用它自己的话回这一轮
                    return

                # 写 assistant turn，拿回归一化后的 calls（id 已补全）
                # shadow：Span 在 memory 写入【之前】登记 PREPARED，
                # 写完之后 CAS 到 OPEN —— 这一档的意义就是区分"崩在写之前"和"崩在写之后"。
                _sp = _rt_shadow_prepare(self, round_idx, [c.name for c in calls], _RT_PATH.NORMAL)
                calls = self.memory.add_tool_calls(calls, thinking_blocks=decision.thinking_blocks)
                # 不再置旧字段 —— `_rt_shadow_open` 已经把 Span 切到 OPEN，
                # 而 `_active_tool_batch_open` 现在就是从那个状态派生的。
                _rt_shadow_open(self, _sp, [c.tool_use_id for c in calls])

                yield {
                    "event": "tool_batch_start",
                    "batch_id": f"batch_{round_idx}_{time.time_ns()}",
                    "count": len(calls),
                    "status": "TOOL_EXECUTING",
                    "model": used_model,
                    "log": f"ReAct第{round_idx + 1}轮：执行 {len(calls)} 个工具。",
                }

                # 并发执行工具，同时 drain event_queue
                batch_task = asyncio.create_task(self._execute_tool_batch(
                    calls,
                    used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue,
                    # ⭐ 把**产生这批调用的那次 request** 的 tool contract 带下去。
                    #    ⚠️ 取自 `decision`，不是重算 —— 见 `_stream_decision_core` 里
                    #       那段关于 `_pending_loaded_manifests` 的说明。
                    active_tool_names=getattr(decision, "active_tool_names", None),
                ))

                while not batch_task.done():
                    try:
                        ev = await asyncio.wait_for(event_queue.get(), timeout=0.05)
                        yield ev
                    except asyncio.TimeoutError:
                        pass

                # 排空队列残留事件
                while not event_queue.empty():
                    yield event_queue.get_nowait()

                executions: list[ToolExecution] = await batch_task

                # 按需加载：把本批 load_tools 匹配到的 schema 并入 tools_manifest，下一轮即可调用
                if self._pending_loaded_manifests:
                    _loaded_names = {(_m.get("name") or "") for _m in self._pending_loaded_manifests}
                    _have = {t.get("name") for t in tools_manifest}
                    for _m in self._pending_loaded_manifests:
                        if _m.get("name") not in _have:
                            tools_manifest.append(_m)
                            _have.add(_m.get("name"))
                    # OS 规范很长，不再每轮常驻；只有真正 load OS 能力后才注入。
                    if _loaded_names & {"os_execute", "look_at_screen", "set_window_mode"}:
                        react_guide = self._insert_before_cache_break(react_guide, _OS_CAPABILITY_PROMPT)
                    self._pending_loaded_manifests = []

                # 收集 tool_data
                batch_tool_data: dict = {}
                for ex in executions:
                    if ex.tool_data is not None:
                        batch_tool_data[ex.call.tool_use_id or ex.call.name] = ex.tool_data
                if batch_tool_data:
                    tool_data_out = batch_tool_data

                # 写 user turn 必须先于 OS plan 检测，确保 tool_calls/tool_results 始终成对
                tool_result_blocks = [
                    ToolResultBlock(
                        name=ex.call.name,
                        tool_use_id=ex.call.tool_use_id or f"tool_{ex.call.name}",
                        content=ex.result_text,
                        is_error=not ex.ok,
                        raw_result=ex.raw_result,
                        tool_data=ex.tool_data,
                    )
                    for ex in executions
                ]
                self.memory.add_tool_results(tool_result_blocks)
                # 同上：`_rt_shadow_commit` 把 Span 切到 COMMITTED，派生值随之变 False。
                _rt_shadow_commit(self, _sp, [b.tool_use_id for b in tool_result_blocks],
                                  _RT_PATH.NORMAL)

                # 格式校验：失败时必须回滚，否则下一轮 Anthropic 大概率 400
                ok_fmt, why_fmt = self.memory.validate_tool_turns()
                if not ok_fmt:
                    logger.error(f"[ReAct] memory 格式校验失败，回滚: {why_fmt}")
                    self._rollback_last_tool_batch()
                    # Span 已 COMMITTED（上面那行），这里补一条观测说明"内核认为一致、
                    # 而 memory 校验说不一致"——这正是不变量 9 该抓的东西。
                    _rt_shadow_note(_RT_PATH.NORMAL, f"validate_tool_turns 失败: {why_fmt}",
                                    diverged=True)
                    raise RuntimeError(f"ReAct memory 格式损坏，已回滚：{why_fmt}")

                # OS dsl_plan 检测（在 tool_results 已写入之后）
                os_plan_ex = next(
                    (ex for ex in executions if isinstance(ex.result_text, str)
                     and ex.result_text.startswith("__OS_DSL_PLAN__")),
                    None
                )
                if os_plan_ex and isinstance(os_plan_ex.tool_data, dict):
                    _plan_data = os_plan_ex.tool_data
                    _skill_name = _plan_data.get("_skill_name", "")
                    _os_plan = _plan_data.get("dsl_plan", [])
                    _skill_args = _plan_data.get("_skill_args", {})
                    from core.os_layer.dispatch import OSDispatcher
                    from core.os_layer.safety import OSSessionSafety
                    if not hasattr(self, "_os_safety"):
                        self._os_safety = OSSessionSafety()
                    _os_dispatcher = OSDispatcher(
                        session_id=getattr(self, "_session_id", ""),
                        m1_mode=False, m2_mode=True, m3_mode=True,
                        safety=self._os_safety, provider=self.provider,
                        vision_model_override=_vision_model_for_os(),
                    )
                    _rt_shadow_note(_RT_PATH.OS_EARLY_RETURN,
                                    f"skill={_skill_name} steps={len(_os_plan)}")
                    self._rt_os_lease = _rt_lease_acquire(self, "os_skill_plan")
                    try:
                        async for step in self._run_os_skill_plan_loop(
                            _skill_name, _os_plan, _skill_args,
                            _os_dispatcher, self._os_safety, used_model, base_guide, realtime_callback
                        ):
                            yield step
                    finally:
                        _rt_lease_release(self)
                    return

                yield {
                    "event": "tool_batch_end",
                    "count": len(executions),
                    "status": "CORE_THINKING",
                    "model": used_model,
                    "log": "工具结果已回填，继续 ReAct 思考。",
                }

                continue  # 进入下一轮思考

            # ── 4. 其他类型兜底 ──────────────────────────────────────────
            content = decision.content or f"无法处理的决策类型：{decision.decision_type}"
            self.memory.add_message("assistant", content)
            yield {
                "event": "final_result",
                "content": content,
                "model": used_model,
                "status": "SYS_IDLE",
                "log": "ReAct loop：兜底终止。",
                "current_skill": None,
                "rag_hit": self._rag_hit_this_turn,
                "full_file_hit": self._full_file_hit_this_turn,
            }
            return

        # 超过最大轮数
        # 走到这里说明循环正常结束，理论上没有活 span；兜底收一次，
        # 免得极端路径把它漏在 OPEN 让下一轮误报。幂等，无副作用。
        _rt_abort_open_span(self, "LOOP_EXHAUSTED", _RT_PATH.GENERATOR_DROPPED)
        async for _ev in self._final_answer_or_fallback(
                facts=("The loop hit its round limit after "
                       f"{self.REACT_MAX_ROUNDS} rounds of thinking and tool "
                       "calls, so it stopped there rather than keep spinning. "
                       "Whatever was in progress is unfinished. Tell the user "
                       "that, and suggest narrowing the request or splitting it."),
                # ⚠️ 兜底保留中文原文 —— 模型不可用时才登场（豁免④）
                fallback=f"已连续思考/调用工具 {self.REACT_MAX_ROUNDS} 轮，为避免陷入循环，先停在这里。",
                base_guide=base_guide, used_model=used_model,
                log=f"ReAct loop：达到最大轮数 {self.REACT_MAX_ROUNDS}。",
                extra={"current_skill": None, "rag_hit": self._rag_hit_this_turn, "full_file_hit": self._full_file_hit_this_turn}):
            yield _ev

    async def _stream_final_answer(self, context, system_guide, *, task_type: str):
        """统一的"流式最终答案"调用 helper——跟 _stream_decision 同一类设计，
        所有"不调工具、直接生成最终回答（结果总结/失败转写等收尾文案）"的调用点
        都走这个，不再各自手写"调 chat_without_tools 拿完整字符串"。

        直接复用 chat_without_tools_stream 的
        text_delta/done 两段式事件，翻译成前端的 final_text_start/
        final_text_delta，结果通过实例属性回传：
          self._last_stream_final_text  : str        完整最终文本
          self._last_stream_final_model : str
          self._last_stream_final_bid   : str | None  有内容到达才会有 bid；
            空回答（从未触发任何 delta）时是 None，调用方据此判断——app.py
            那边 final_result 事件如果带的 block_id 是 None，会走老的
            "没有流式内容，直接整段渲染"分支，不强求每个调用点都必须流式化。
        """
        bid = f"final_{time.time_ns()}"
        started = False
        full_text = ""
        used_model = self.target_model if hasattr(self, "target_model") else None
        async for _ev in self.provider.chat_without_tools_stream(
            context, system_guide, model_override=None,
        ):
            if _ev["type"] == "text_delta":
                if not started:
                    started = True
                    yield {"event": "final_text_start", "block_id": bid}
                yield {"event": "final_text_delta", "block_id": bid, "delta": _ev["text"]}
            elif _ev["type"] == "done":
                full_text = _ev["text"]
                used_model = _ev["model"]
        self._last_stream_final_text = full_text
        self._last_stream_final_model = used_model
        self._last_stream_final_bid = bid if started else None
