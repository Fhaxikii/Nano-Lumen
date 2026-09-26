# core/orchestrator/orchestrator.py
"""`Orchestrator`：组合各职责的 mixin（同目录各模块），持有共享状态（`__init__`）
与几个共用的小方法（working memory 写入、会话日志、重置对话、受损记忆回滚）。
"""

import asyncio

from loguru import logger

from core import rag as rag_engine
from core.memory_store import get_memory_store
from core.orchestrator._runtime import (
    _RT_PATH,
    _rt_abort_open_span,
    _rt_cancel_all_interactions,
    _rt_has_open_batch,
    _rt_restore_pending_skills,
)
from core.orchestrator.file_tools import FileToolsMixin
from core.orchestrator.interaction import InteractionMixin
from core.orchestrator.long_task import LongTaskMixin
from core.orchestrator.mcp import McpMixin
from core.orchestrator.notes_and_tasks import NotesAndTasksMixin
from core.orchestrator.os_execute import OsExecuteMixin
from core.orchestrator.os_skill import OsSkillMixin
from core.orchestrator.prompt_context import PromptContextMixin
from core.orchestrator.react_loop import ReactLoopMixin
from core.orchestrator.screen import ScreenMixin
from core.orchestrator.skill_lifecycle import SkillLifecycleMixin
from core.orchestrator.skill_writer import SkillWriterMixin
from core.orchestrator.subagent import SubagentMixin
from core.orchestrator.tool_dispatch import ToolDispatchMixin
from core.orchestrator.turn import TurnMixin


class Orchestrator(
    McpMixin,
    ScreenMixin,
    FileToolsMixin,
    SubagentMixin,
    NotesAndTasksMixin,
    OsExecuteMixin,
    InteractionMixin,
    ReactLoopMixin,
    LongTaskMixin,
    OsSkillMixin,
    SkillWriterMixin,
    SkillLifecycleMixin,
    ToolDispatchMixin,
    PromptContextMixin,
    TurnMixin,
):
    def __init__(self, provider, registry, memory, instruction_path="config/system_instruction.txt"):
        self.provider = provider
        self.registry = registry
        self.memory   = memory
        self.instruction_path = instruction_path
        self._system_guide_template = self._load_system_instruction()
        # ⭐ 待审 Skill 载荷：**多槽**，按 filename 索引（2026-08-06）
        #
        # 改造前这里是单个 dict，而 Interaction 那一层按支持 6 条待办。
        # 两层容量不匹配的后果（实测撞出来的）：
        #   CountWords 审计打开 → _pending_skill = {CountWords…}   Interaction A: OPEN
        #   UpperCase  审计打开 → _pending_skill = {UpperCase…}    Interaction B: OPEN
        #                          ↑ 第一份代码正文被【无声覆盖】，而 A 仍自称 OPEN
        # 于是用户说"部署吧"、模型对 A 调工具时，载荷已经不是 A 的了。
        #
        # 现在按 filename 存多份。`_pending_skill`（单数）退化成"最近那条"的
        # **只读兼容视图**（见下面的 property）—— 十几处只关心"有没有待审"的调用点
        # 因此一行都不用改，而需要指定具体哪一条的地方显式传 filename。
        self._pending_skills: dict[str, dict] = {}
        self._pending_skill_order: list[str] = []   # 插入顺序，决定"最近那条"是谁
        self._pending_action: dict | None = None

        # [2026-08-05] `_pending_skill_clarification` 已删除。
        # 澄清态现在是 `core/runtime/interaction.py` 里的一条 Interaction：
        # 跨重启存活、永不静默超时、由模型经 answer_open_interaction 消费。
        # 不要为了"方便"再加回一个内存字段——那会立刻变成双权威。

        # 上一次 OS 视觉定位失败的记录，供 _detect_os_locate_followup 用
        self._last_os_locate_failure: dict | None = None
        # 是否有 OS 任务正在执行（canary 自检只在这个是 False 时才跑）
        # ⚠️ `_os_task_busy` 已于切写那一步删除（2026-08-08）。
        # 权威是 `oslease` 的活动租约 —— 它有主人、有期限、过期自动不算数，
        # 而那个裸 bool 漏写一次 False 就永久停摆且一声不响（实测停了 61 分钟）。
        # 📌 **一个裸 bool 表达不了「谁持有、持有多久、过期算谁的」，所以它防不住任何一种泄漏。**
        self._canary = None  # lazy init，见 maybe_run_canary
        self._push_callback = None  # async (content: str) -> None，由后端 start_backend_services 注入（core.backend.speak）
        # () -> Nano 主窗口对象（有 minimize / restore），由 app.py 注入；core 不直接依赖 UI 框架。
        # 看屏幕前用它把自己最小化让开；没有注入时（测试、无界面运行）不让开。
        self._native_window = None
        # GUI 任务的空闲兜底挂在 runtime reconcile 的周期 tick 上。
        self._install_gui_task_idle_tick()

        # 本会话是否已经提示过"当前模型不适合写 Skill，建议切换"
        self._model_switch_suggested_this_session: bool = False

        # 本轮已经失败过的 (工具名, 故障类型)，用于"你这轮已经试过这个名字"的升级措辞
        self._tool_failures_this_turn: dict[str, int] = {}

        # Step 4：pending 状态时间戳（用于超时清理）
        self._pending_skill_at: float = 0.0
        self._pending_action_at: float = 0.0

        # RAG 是否本轮真的被调用过（用于 UI HIT 灯）
        self._rag_hit_this_turn: bool = False

        # Phase 2：全文加载是否本轮被调用过（用于 UI FullFile 灯）
        self._full_file_hit_this_turn: bool = False

        # 轻量版 working memory
        self._session_log: list[str] = []
        self._pending_skill_description_cache: str = ""
        # 记录最近一次部署的 Skill 名,用于"这个怎么用"类追问的 target_skill 兜底
        self._last_deployed_skill: str | None = None
        # 记录最近一次调用的 Skill,用于"这个有问题修一下"类模糊路由
        self._last_called_skill: str | None = None
        # 记录最近一次在任意 Skill 相关意图里被点名/解析出的 Skill 名（无论是
        # 调用/查看/启用/禁用/删除哪种动作），用于"刚才那个/为什么被禁用了"
        # 这类不带名字的追问兜底——_last_deployed_skill 和 _last_called_skill
        # 各自只覆盖部署/调用这一种场景，追问别的动作（比如启用/禁用）时会双双
        # 落空，导致明明刚提过名字也照样答"请告诉我你想了解哪个 Skill"。
        self._last_mentioned_skill: str | None = None
        self._notes_written_this_turn: list = []   # 本轮 write_user_note 写入的笔记（供 UI 弹气泡/进抽屉）

        # 跨会话 SQLite working memory
        try:
            self._wm = get_memory_store()
            from datetime import datetime as _dt
            self._wm_session_id = _dt.now().strftime("%Y%m%d_%H%M%S")
        except Exception as _wm_err:
            logger.warning(f"[WorkingMemory] 初始化失败，跳过: {_wm_err}")
            self._wm = None
            self._wm_session_id = ""

        # 知识库后台索引（进程内只启动一次，不阻塞 UI 启动）；
        # _rag_ready 是进程级共享的「初始化流程结束」信号
        self._rag_ready = rag_engine.start_background_index()

        # ReAct 并发信号量（在第一次 asyncio 事件循环里懒初始化）
        self._rag_parallel_sem: asyncio.Semaphore | None = None
        self._tool_parallel_sem: asyncio.Semaphore | None = None
        # ⭐ [⑦ · 切写那一步 · 2026-08-06] `_active_tool_batch_open` **这个字段已经删除**。
        #
        # 它原来是 ReAct batch 事务标记（纯内存 bool），现在降级成从 ToolBatchSpan
        # 派生的**只读属性**（见下面的 property）。三步迁移走完：
        #     A 旧字段权威、内核镜像对答案   ✅ 2026-08-05（8/8 覆盖，1 处分歧已定性）
        #     B 内核权威、旧字段降级为派生   ✅ 本次
        #     C 删掉字段本身与它的每轮重置   ✅ 本次
        #
        # ⭐ **切权威顺手修掉一处真 bug**（观测期的"预判 A"，shadow 已证实）：
        # exit-tool 与其他工具同轮那条路径（`_run_react_loop` 里那段）
        # **从头到尾没碰过旧标志** —— 它确实开了又关了一个批次，但旧标志全程 False。
        # 于是那一轮如果中途抛异常，`_clean_damaged_memory` 会因为标志是 False 而
        # 跳过回滚，留下一个坏 pair。现在状态从 Span 派生，那条路径**终于有标记了**。
        # 这是行为改善，不是重构 —— changelog 要单独写一条。
        # shadow 用的 turn 标识。在 __init__ 里先给个空值，
        # 是为了让 `getattr(orch, "_rt_turn_id", "")` 那种兜底永远不必真的兜底——
        # 属性存在但为空，比属性不存在更容易在日志里看出"谁忘了设它"。
        self._rt_turn_id: str = ""
        # 当前打开着的 Span id。
        # ⚠️ 为什么需要它：旧标志 `_active_tool_batch_open` 有【两个】清理点——
        # 每轮开头的重置，以及 `_clean_damaged_memory` 里那行（7279）。
        # 第二个是早先没记下来的，实测 shadow 第一天就抓到了：span 停在 OPEN 而旧标志
        # 已被它置回 False，于是下一轮 sweep 误报成"批次抛异常"。
        # 把 span id 挂在实例上，任何清理点都能顺手 abort 它。
        self._rt_open_span: str | None = None

        # SkillSpec hard_validate 的报错。降级为直接代码生成时要把它带给模型 ——
        # 只进日志的话，模型会原样再犯（实测 一个会话里犯了三次同类错误）。
        self._last_spec_errors: list[str] = []

        # 最近一次审计窗口校验失败 / 被用户丢弃的 Skill。
        # ⚠️ 必须是**实例状态**而不是 memory 消息：app 侧那条
        # `[System record: ...discarded...]` 会被 max_turns=10 切掉，
        # 而模型往往是在很多轮之后才说"上次写的校验报错，重新写"。
        # 实例状态 + 每轮重建的动态段，天生不受截断影响。
        self._last_audit_failure: dict | None = None
        # 用户在待办卡片上点了「回复这条」→ 下一条消息的指向是显式的。
        # ⚠️ 只是**意图**，不动 Interaction 状态；落盘仍走 answer_open_interaction。
        self._reply_target: dict | None = None
        # ⭐⭐⭐ [2026-08-13 CMD63] 本轮**已被移交**的指向 —— 见 `hand_off_reply_target()`。
        # UI 一按发送就把 `_reply_target` 移到这里（而不是清掉），
        # 因为真正的消费者（模型动态段）比 UI 晚 4 毫秒才来读。
        self._reply_target_turn: dict | None = None

        # ⭐⭐ 重启后把待审草稿捞回来。必须放在 __init__ 最后 ——
        # 它要用 `_put_pending_skill`，也要 `_pending_skills` 已经建好。
        _rt_restore_pending_skills(self)

    # ── 分类器 / 无工具快路径的残骸【已全部删除 · 2026-08-04】────────────────
    # 这里原有四个函数，全部零调用方：
    #   _light_mode_override_note      —— 轻量无工具轮的"本轮没加载工具"说明
    #   _build_text_only_context       —— 给无工具快答用的纯文本上下文
    #   _should_use_no_tool_fast_path  —— 判断是否走无工具快答
    #   _query_requires_full_tools_hard—— 关键词硬拦截表（约 50 个短语 + 组合规则）
    #
    # 它们服务的架构在 v1.1「Token压缩 0703」那轮就被拆掉了：分类器与 fast-path
    # 一起删除，所有消息统一走 ReAct 主循环（理由见 _handle_query_impl 第 3 节）。
    # 函数本身被留了下来，此后一直没有任何调用方。
    #
    # 最后那个尤其不该留：它是一张中文关键词硬路由表（"截图/记住/打开文件/现在几点"…），
    # 正是【原则 3 少硬路由】要消灭的东西，也正是当初做路由重构的直接动机。
    # 留着它的唯一风险是被后来人当成"现行规则"照抄。

    # ── `_active_tool_batch_open`：只读派生属性 ──────────────────────

    @property
    def _active_tool_batch_open(self) -> bool:
        """本轮有没有未完成的工具批次。**从 ToolBatchSpan 派生，没有对应字段了。**

        保留这个名字是为了让两个消费者（400 分支的守卫、`_clean_damaged_memory`
        的回滚判据）读起来跟改造前一样 —— 它们关心的语义没变，只是权威换了地方。

        ⚠️ **故意不提供 setter。** 任何 `self._active_tool_batch_open = X` 都会
        直接抛 AttributeError，而不是静默地写一个没人读的字段 —— 那正是双权威的入口。
        要改这个状态，只能通过 Span 命令（`_rt_shadow_open` / `_rt_shadow_commit`
        / `_rt_abort_open_span`）。

        ⚠️ 读失败时返回 **False**（不回滚），不是 True。见
        `toolbatch.has_open_span()` 里那段关于代价不对称的说明。
        """
        _open, _trusted = _rt_has_open_batch(self)
        if not _trusted:
            # 只在**真的读不出来**时才警告。"库里确实没有 OPEN"是事实、不是故障，
            # 把两者混在一起报警会让这条日志变成噪音然后被忽略。
            logger.warning(
                "[ReAct] batch 状态读取不可信，按【无未完成批次】处理："
                "这一轮如果中途出错，末尾工具对不会被回滚（宁可留坏 pair，不误删历史）"
            )
        return _open

    def _wm_add(self, entry_type: str, subject: str, action: str,
                detail: str = "", tags: list | None = None):
        """安全写入 working memory。异常不影响主流程。"""
        if self._wm is None:
            return
        try:
            self._wm.add(
                entry_type=entry_type,
                subject=subject,
                action=action,
                detail=detail,
                tags=tags or [],
                session_id=self._wm_session_id,
            )
        except Exception as e:
            logger.debug(f"[WorkingMemory] 写入失败(跳过): {e}")

    def _session_log_append(self, entry: str):
        """往 session_log 追加一条记录。自动加时间戳。"""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M")
        self._session_log.append(f"{ts} {entry}")
        if len(self._session_log) > 20:
            self._session_log = self._session_log[-20:]


    def reset_conversation(self) -> dict:
        """Step 4：用于 UI"重置当前对话"按钮。

        清空 memory 历史 + 清空所有 pending 状态。Skill 文件、知识库索引、registry 不动。
        """
        had_memory = len(self.memory) > 0
        had_pending_skill = self._pending_skill is not None
        had_pending_action = self._pending_action is not None
        # 这才是唯一的新会话入口。普通 reset() 只清内存投影，不能用于
        # 用户明确要求的「重置对话」，否则重启后旧记录会又回来。
        self.memory.reset_conversation()
        self._pending_skill = None
        self._pending_skill_at = 0.0
        self._pending_action = None
        self._pending_action_at = 0.0
        # 取代 `self._pending_skill_clarification = None`。
        # 未决交互现在在 SQLite 里，清空内存字段清不掉它们。
        _cancelled_interactions = _rt_cancel_all_interactions("重置对话")
        self._rag_hit_this_turn = False
        self._full_file_hit_this_turn = False
        self._session_log = []  # 重置会话 log
        self._pending_skill_description_cache = ""
        self._last_deployed_skill = None
        self._last_called_skill = None
        self._last_mentioned_skill = None
        self._last_os_locate_failure = None
        self._last_skill_error = None     # 结构化错误状态：{skill, error, consumed}
        # 审计失败记录也要清：重置对话的语义是"忘掉这段"，留着它会让新对话
        # 一开口就带着上一段的失败上下文（而用户已经把那段抹掉了）。
        self._last_audit_failure = None
        self._last_spec_errors = []
        # OS 预授权的语义是"【本次对话】始终允许"，所以重置对话必须一起清掉，
        # 否则那个 scope 名不副实（safety.reset() 之前从来没有调用方）。
        if hasattr(self, "_os_safety"):
            try:
                self._os_safety.reset()
            except Exception as _e:
                logger.warning(f"[Reset] 清理 OS 预授权失败（不影响重置）: {_e}")
        # 重置对话结束进行中的 GUI 任务（收回临时授权；界面随后恢复窗口）。
        self._gui_task_end("conversation reset")

        logger.info(
            f"[Reset] 对话已重置（memory={had_memory}, "
            f"pending_skill={had_pending_skill}, pending_action={had_pending_action}, "
            f"interactions={_cancelled_interactions}）"
        )
        return {"ok": True, "msg": "已重置当前对话。历史和待处理状态已全部清空。"}

    # _handle_os_task 已删除（OS 双循环合一，0628）：OS 不再有独立循环，
    # os_execute 始终注入主 ReAct 循环，由 _run_react_loop + _execute_one_tool_call
    # 的 os_execute 分支统一执行。

    # ── _handle_pending_action 已删除（· ②c · 2026-08-06）──────────
    # 117 行的标签分支 + 一个专用分类器 + 一个伪事件 `__pending_new_request__`
    # 用来从生成器里逃逸回常规路由。整套被一条 `skill_manage` Interaction 取代。
    # 处置逻辑现在在 `_handle_answer_interaction` 的 SKILL_MANAGE 分支里。

    def _rollback_last_tool_batch(self):
        """回滚最后一对 tool_calls / tool_results（ReAct 格式校验失败时调用）。"""
        last = self.memory.peek_last()
        if last and last.role == "tool_results":
            popped = self.memory.pop_last()
            logger.warning(f"[防御] 回滚孤儿 tool_results: role='{popped.role}'")
        last = self.memory.peek_last()
        if last and last.role == "tool_calls":
            popped = self.memory.pop_last()
            logger.warning(f"[防御] 回滚 tool_calls: role='{popped.role}'")

    def _clean_damaged_memory(self):
        """更安全的回滚：只清理本轮未完成的 tool batch，不误删已合法完成的工具对。

        判断依据：`_active_tool_batch_open` 标记。
        - True：tool_calls 已写，tool_results 未完成（mid-batch 异常），安全回滚
        - False：batch 已正常关闭，末尾工具对是合法历史，不能删
        """
        last = self.memory.peek_last()
        if last is None:
            return
        # 孤儿 user / 旧格式 tool_call：始终安全回滚
        if last.role in ("user", "tool_call"):
            popped = self.memory.pop_last()
            if popped:
                logger.warning(f"[防御] 已回滚受损记忆节点: role='{popped.role}'")
            return
        # 新格式 tool_calls / tool_results：只在 batch 事务未关闭时回滚
        if last.role in ("tool_calls", "tool_results"):
            if self._active_tool_batch_open:
                self._rollback_last_tool_batch()
                # ⚠️ 这是旧标志的【第二个】清理点（第一个是每轮开头的重置）。
                # 早先的设计的清理覆盖矩阵原先只记了"没有每轮重置"，没记这一处。
                # span 必须跟着一起收掉——否则它停在 OPEN、旧标志已 False，
                # 下一轮 sweep 会把它误报成"批次中途抛异常"（实测第一天就撞到了）。
                _rt_abort_open_span(self, "MEMORY_ROLLBACK", _RT_PATH.BATCH_EXCEPTION)
            else:
                logger.warning("[防御] 末尾是合法工具对，batch 已完成，跳过回滚，避免误删历史")


    def _get_generic_error_payload(self, err: Exception, skill: str) -> dict:
        return {
            "event": "sys_error",
            "content": f"抱歉，模型调度遭遇未预期中断。\n[异常报告]: {str(err) or '未知链路断开'}。\n当前请求已降级熔断。",
            "status": "ERROR",
            "model": "CRITICAL_OFFLINE",
            "log": f"指令熔断: {str(err)[:40]}",
            "current_skill": skill
        }

