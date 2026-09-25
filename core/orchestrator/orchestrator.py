# core/orchestrator/orchestrator.py
from typing import Optional, Any, NamedTuple
from dataclasses import dataclass, field
from core.schema import AgentDecision, ChatMessage, ToolCall, ToolResultBlock
import hashlib
import os
import re
import asyncio
import contextvars
import threading
import time
import traceback
from core import reading as _READING
from core.skill_check import validate_skill_code  # noqa: E402
from core.tools.manifests import (  # noqa: E402
    BUILTIN_MANIFESTS, _DONT_WAIT_MANIFEST, _NL, _WRITE_SKILL_MANIFEST,
)
from loguru import logger
from core import rag as rag_engine
import pathlib
from core.i18n import language_clause as _language_clause
from core.provider import _DISPATCH_LAYER, CACHE_BREAK_MARKER, CLAUDE_MODEL_MAP, CLAUDE_MODELS, GEMINI_MODELS, GEMINI_MODEL_MAP
from core.memory_store import get_memory_store, EntryType
# 统一工具目录 —— 「一个工具是什么」的唯一权威。
from core.tools import (
    Flow, Preload, Scheduling, ToolCatalog, ToolOrigin, ToolScope,
)
from core.orchestrator._runtime import (
    _RT_PATH,
    _agent_scope_ctx,
    _rt_abort_open_span,
    _rt_answer_interaction,
    _rt_authorization_state,
    _rt_background_jobs,
    _rt_cancel_all_interactions,
    _rt_close_interaction,
    _rt_close_mcp_manage,
    _rt_close_skill_audit,
    _rt_close_skill_manage,
    _rt_has_live_work,
    _rt_has_open_batch,
    _rt_lease_acquire,
    _rt_lease_heartbeat,
    _rt_lease_release,
    _rt_live_interactions,
    _rt_machine_is_free,
    _rt_ongoing_work,
    _rt_open_clarification,
    _rt_open_mcp_manage,
    _rt_open_skill_audit,
    _rt_open_skill_manage,
    _rt_restore_pending_skills,
    _rt_shadow_commit,
    _rt_shadow_note,
    _rt_shadow_open,
    _rt_shadow_prepare,
    _rt_supersede_covered_clarifications,
    _rt_sweep_stale_spans,
    _rt_wait_open,
    current_agent_label,
)
from core.orchestrator._types import (
    StreamDecisionState,
    ToolExecution,
    ToolOutcome,
    _ToolRuntimeView,
)
from core.orchestrator.mcp import McpMixin
from core.orchestrator.screen import _vision_model_for_os, ScreenMixin
from core.orchestrator.file_tools import FileToolsMixin
from core.orchestrator.subagent import SubagentMixin
from core.orchestrator.notes_and_tasks import NotesAndTasksMixin
from core.orchestrator.os_execute import OsExecuteMixin
from core.orchestrator.interaction import InteractionMixin
from core.orchestrator.react_loop import _OS_CAPABILITY_PROMPT, ReactLoopMixin
from core.orchestrator.long_task import LongTaskMixin
from core.orchestrator.os_skill import OsSkillMixin
from core.orchestrator.skill_writer import SkillWriterMixin
from core.orchestrator.skill_lifecycle import SkillLifecycleMixin


def _main_model_is_blind() -> bool:
    """主模型在不在本厂商的 `vision` 白名单里 —— 不在就是"盲"。

    📌 判据用现成的角色槽白名单，不去猜型号名里有没有 "vision"。
    ⚠️ 取不到就当**不盲** —— 退回老路总好过误判后白绕一圈。
    """
    try:
        from core.models import role_pool
        from core.provider import provider as _p
        main = getattr(_p, "target_model", "") or ""
        return bool(main) and main not in role_pool(main, "vision")
    except Exception:
        return False


    # 🪦 `_coerce_confidence` 已删除（2026-08-29）—— **零调用方**，AST 全仓核实。
    #    📌 一个写好但没人调的东西，比没写更坏：没写时缺口是可见的，
    #       写了不接时缺口看起来已经补上了。


# ── Ambient Memory：应用分类 + 标题解析（模块级，注入与轨迹持久化共用）──────────
# app→类别表统一在 core/proactive/app_catalog（scene 分类也读同一张表）。
from core.proactive import app_catalog as _app_catalog          # noqa: F401
from core.proactive import referent as _referent


def _ambient_cat(app: str) -> str:
    """→ `referent.cat`。**搬家留的转发**，判据只有那一处。"""
    return _referent.cat(app)


def _ambient_parse_title(app: str, title: str):
    """→ `referent.parse_title`。**搬家留的转发**。

    ⚠️ 它被搬进 `core/proactive/referent.py` 是因为那里的句柄解析要用它，
       而 `proactive → orchestrator` 会成环。完整推导（含 `office` 为什么
       必须和 `editor` 走同一条）在那边的函数 docstring 里。
    📌 **删/搬一段代码时，长在它身上的「为什么」要跟着搬到新家** ——
       注释掉队比代码掉队更难发现。
    """
    return _referent.parse_title(app, title)


class Orchestrator(McpMixin, ScreenMixin, FileToolsMixin, SubagentMixin, NotesAndTasksMixin, OsExecuteMixin, InteractionMixin, ReactLoopMixin, LongTaskMixin, OsSkillMixin, SkillWriterMixin, SkillLifecycleMixin):
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
        self._push_callback = None  # async (content: str) -> None，由 app.py 注入
        # () -> Nano 主窗口对象（有 minimize / restore），由 app.py 注入；core 不直接依赖 UI 框架。
        # 看屏幕前用它把自己最小化让开；没有注入时（测试、无界面运行）不让开。
        self._native_window = None

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

    # ── 初始化 ───────────────────────────────────────────────────────────

    def _load_system_instruction(self) -> str:
        parts = []
        persona_path = os.path.join(os.path.dirname(self.instruction_path), "persona.txt")
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                parts.append(f.read())
        if os.path.exists(self.instruction_path):
            with open(self.instruction_path, "r", encoding="utf-8") as f:
                parts.append(f.read())
        if parts:
            return "\n\n---\n\n".join(parts)
        return (
            "You are Nano, the user's personal assistant.\n"
            "Available tool chain: {skills}.\n"
            "Stay in Nano's persona: talkative but not verbose, sharp, technically tough, a little snarky but not hurtful, and never like customer support.\n"
            "You dislike vague requests and rework, so if something is unclear, point it out directly and give an executable way forward.\n"
            "Do not invent facts, results, or abilities. If uncertain, say what is unclear and how to verify it.\n"
            "Do not use customer-service filler such as 'Received' or 'Need anything else?'."
        )

    def _build_tool_awareness_block(self) -> str:
        """构建当前可用能力边界。Token 压缩版：不重复列出所有内置工具长描述。

        ⑩ 两处改动，其余逐字不变：
          · 常驻集不再抄 `_CORE_TOOL_NAMES`，改问目录（`preload=CORE` 派生）。
            ⭐ 顺带**消灭了那个 `- {"answer_open_interaction"}` 的手工减法** ——
               它要减掉的是「名义常驻但其实是条件注入」的那一个，而现在
               `availability` 直接表达了这件事：没有未决交互它压根不在 eligible 里，
               不需要有人记得在这里减一次。
               📌 **一个需要在下游手工修正的名单，说明它上游描述得不对。**
          · Skill 那两行的描述从 `registry.get_skill_awareness_list()` 的
            **`[:50]` 字符级截断**换成目录里的 `awareness`（按词/句边界压缩）。
            🔴 那个 `[:50]` 是 `[:28]` 截断问题的**另一半** —— 只修 `[:28]` 不修它，
               不算死透。`get_skill_awareness_list` 随本次改动一起删除。
        ⚠️ 「官方 / 用户」这个维度**仍然问 registry**，没有塞进 ToolDefinition：
           `ToolOrigin` 回答的是「身份权威在哪」（BUILTIN/SKILL/MCP），
           再让它兼答「是不是官方预置」就是**一个字段表达两个现实** ——
           正是这一整轮在反对的东西。
        """
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        _skills = [d for d in _cat.advertised(ToolScope.MAIN, _rtv)
                   if d.origin is ToolOrigin.SKILL]
        cats = {"official": [], "user": []}
        for d in _skills:
            entry = {"name": d.name, "desc": d.awareness}
            try:
                _is_official = self.registry.is_official_skill(d.name)
            except Exception:
                _is_official = False
            cats["official" if _is_official else "user"].append(entry)
        lines = [
            "\n\n[Current Capability Boundary]",
            "Nano has always-on core tools and more deferred capabilities. "
            "If a needed capability is not in the active tool schema, call load_tools first instead of saying Nano cannot do it.",
            "Always-on core: " + " / ".join(sorted(
                m["name"] for m in _cat.core_manifests(ToolScope.MAIN, _rtv))),
        ]
        lines.append(
            "When explaining user-created Skills to the user, localize or paraphrase their descriptions into the user's current language. "
            "Do not translate Skill names, filenames, parameters, field names, or code identifiers. "
            "Official built-in Skills may keep their original fixed descriptions."
        )
        if cats["official"]:
            parts = [f"{s['name']} ({s['desc']})" for s in cats["official"]]
            lines.append("Official Skills (schema loaded on demand; cannot be deleted/disabled): " + " / ".join(parts))
        if cats["user"]:
            parts = [f"{s['name']} ({s['desc']})" for s in cats["user"]]
            lines.append("User Skills (schema loaded on demand): " + " / ".join(parts))
        else:
            lines.append("User Skills: none yet; new reusable Skills can be created when requested.")

        # ⭐⭐ **已配置的 MCP 服务清单** —— 实测 2026-08-28 抓到的缺口。
        #
        # 🔴 问题：用户说「有个 mcp 叫 context7」，Nano 答「这边找不到 / 目前只接入了
        #    time 这一个」—— 而 context7 明明在配置里、还连着。
        #    它说的那句「只接入了 time」不是查出来的，是它**记得自己刚接入过 time**。
        # 📌 根因：模型能看到的只有【已连接 server 的工具】（进 deferred awareness），
        #    **从来没有一份「有哪些 MCP 服务、各自什么状态」的清单**。
        #    ⇒ 于是 `manage_mcp` 直接作废：它要 `server_name`，而模型不知道有哪些名字。
        # ⚠️ 原本清单里的「MCP 感知」做的是**变更感知**（增删改记进 session log），
        #    而这里缺的是**状态感知**（现在有哪些）—— 两件事被混成了一件。
        #    📌 「发生了什么」和「现在是什么」是两份不同的账，缺哪一份都会让模型瞎猜。
        #
        # ⭐ 状态**必须写出来**，不能只列名字：场景「用户手动禁用了某个 MCP」全靠它 ——
        #    模型要能说「它被禁用了，要我打开吗」，而不是「我没有这个能力」。
        # ⚠️ 形状照抄上面 Skill 那两行（名字 + 一句话），每轮成本约几十 token：
        #    MCP 数量天然很少（个位数），而没有它模型就会**编**。
        try:
            from core.mcp_client import MCPManager as _MM_r
            _snap = _MM_r.instance().status_snapshot()
        except Exception:
            _snap = []
        if _snap:
            _ST_WORD = {"connected": "connected", "disabled": "disabled by the user",
                        "failed": "failed to connect", "needs_auth": "needs authentication",
                        "connecting": "connecting", "disconnected": "not connected"}
            # ⚠️ **只给名字 + 状态，不给描述。**
            #    📌 模型选工具靠的是 deferred awareness 里那些【工具】的描述，
            #       server 这一层的描述对「选哪个工具」毫无帮助 ——
            #       它的用途是回答用户「这个 mcp 是干嘛的」，而那是**低频问题**。
            #    ⇒ 每轮都发它，等于为一个偶尔才被问到的问题付固定成本。
            #      （实测带描述 127 token/轮，去掉后 ~40。）
            #    ⭐ 真被问到时，管理页和 `status_snapshot()` 里都有完整描述。
            # ⚠️ `owned_by` 非空的**不列** —— 它是某个 Skill 的内部零件，
            #    模型既管不了它、也从来调不到它的工具
            #    （`expose_tools=False` ⇒ 隐藏 server 的工具连 `_tool_index` 都不进，
            #      只能被那个包装 Skill 通过 `call_hidden()` 走 —— 已回代码核实）。
            #    📌 列一个它既不能用、也不能管的东西，只会让它去"修"一个没坏的东西：
            #       实测 2026-08-28 它就这么干了，还宣布"连上了"。
            _mp = [f"{_m['name']} [{_ST_WORD.get(_m.get('status', ''), _m.get('status', ''))}]"
                   for _m in _snap if not _m.get("owned_by")]
        if _snap and not _mp:
            _snap = []          # 全是零件 ⇒ 对模型而言等于一个都没配
            # 🔴🔴 **「authoritative and complete」这半句不是客套，是这条的命门。**
            #    实测第二轮：清单已经在上下文里、内容也对，模型**照样**花了 5 次工具
            #    调用 68.7 秒去翻 `mcp*.json`，最后加载了用户主目录下的
            #    `.claude.json` —— 那是 **Claude Code 的配置**，不是 Nano 的
            #    （Nano 的在 `config/mcp_servers.json`）。
            #
            # ⭐⭐ 同一个问题的第三次发作，三次的形状一模一样：
            #        ① `.cursor/mcp.json`  → 以为自己是 Cursor
            #        ② 会话记忆            → 「我只接入了 time 这一个」
            #        ③ `.claude.json`      → 以为自己是 Claude Code
            #    📌 **没有一个被声明为权威的来源，模型就会自己找一个像样的。**
            #       而「像样的来源」在一台装着别的 AI 工具的机器上遍地都是。
            #
            # ⚠️ 这不算「专门给某个模型的修正动作」：这份清单**客观上**就是权威且
            #    完整的（直接来自 MCPManager 的活状态），原来只是没把这个事实说出来。
            #    📌 补一句真话 ≠ 打一个补丁。
            # ⚠️ 多花的约 50 token/轮，换掉的是 68 秒 + 5 次工具 + 一个读错文件的答案。
            #    📌 体验 > 成本。
            lines.append(
                "MCP servers on this machine (this list is authoritative and complete "
                "- answer questions about which MCP servers exist or their status "
                "directly from it; never search the disk or read config files for this, "
                "config files belonging to other AI tools are not Nano's). "
                "manage_mcp enables / disables / deletes / retries them; "
                "connect_mcp adds a new one: " + " / ".join(_mp))
        else:
            # ⚠️ 空清单**同样要带权威性** —— 否则模型看到 "none" 的第一反应
            #    就是「不对吧，我去找找配置文件」，于是绕回上面那三次里的任意一次。
            lines.append(
                "MCP servers: none configured on this machine yet (authoritative "
                "- do not look for MCP config files on disk). connect_mcp can add one.")
        return "\n".join(lines)

    def _build_pipeline_context(self) -> list:
        return self.memory.get_full_context()

    @staticmethod
    def _insert_before_cache_break(text: str, extra: str) -> str:
        """把稳定协议插入 CACHE_BREAK_MARKER 前，确保可被 prompt cache 命中。"""
        if not extra:
            return text
        if CACHE_BREAK_MARKER in text:
            return text.replace(CACHE_BREAK_MARKER, extra + CACHE_BREAK_MARKER, 1)
        return text + extra

    # ⭐⭐ Nano 的「我在哪、我在什么机器上」—— 2026-08-28 用户提出。
    #
    # 🔴 起因是 MCP 那次绕路：模型不知道自己的程序目录，于是去翻用户主目录，
    #    读到了 `.claude.json`（Claude Code 的配置）当成自己的。
    #    📌 前后三次发作（.cursor / 会话记忆 / .claude.json）都是同一句话：
    #       **一个不知道自己在哪的程序，会把任何长得像自己配置的文件认成自己的。**
    #
    # ⚠️ 这里是**程序目录**，不是工作目录（用户特意点名）：
    #       cwd    取决于用户从哪儿启动（双击 / 快捷方式 / 命令行各不相同）
    #       项目根（`core.paths.ROOT`）由代码文件位置推出 —— **装在哪就是哪**
    #    ⇒ 「每个人存放路径不一样」恰恰不构成问题：它不依赖 cwd、注册表或启动方式。
    # 🔴 若将来打包成 exe（PyInstaller），`__file__` 会指向临时解压目录 `_MEIPASS`，
    #    **不会报错，只会静默指错**。到那天必须在 `core/paths.py` 加 `sys.frozen` 分支。
    _ENV_BLOCK: str = ""          # 一次会话内不变 ⇒ 只算一次（注册表读取不该每轮跑）

    @classmethod
    def _environment_block(cls) -> str:
        if cls._ENV_BLOCK:
            return cls._ENV_BLOCK
        import sys as _sys, platform as _pf
        try:
            from core.paths import ROOT as _ROOT
            _root = str(_ROOT)
        except Exception:
            _root = "(unknown)"
        cls._ENV_BLOCK = (
            "\n\n[Environment]\n"
            "Nano itself is running in the following environment:\n"
            f"- Nano's own program directory: {_root} "
            "(this is where Nano's own files live - its code, config and data. "
            "It is NOT the user's working folder; never assume the user's files are here.)\n"
            f"- Platform: {_sys.platform}\n"
            f"- OS Version: {cls._os_version_string()}\n"
        )
        return cls._ENV_BLOCK

    @staticmethod
    def _os_version_string() -> str:
        """真实的 OS 版本串。

        🔴 **不能用 `platform.version()` / `platform.platform()`** —— 它们走
           `GetVersionEx`，被 Windows 的兼容性垫层锁住。实测这台机器：
               platform.version()      = 10.0.19041   ❌
               sys.getwindowsversion() = build 19045   ✅
           📌 差 4 个版本，而且**不报错** —— 最顺手的那两个 API 悄悄给错值。

        🔴 **Windows 11 的注册表仍然写着 "Windows 10 Pro"**（微软没改过这个键）。
           ⚠️ 这条在 Win10 上**永远测不出来** —— 正是「在我机器上好使」
              的标准形态，所以按 build 号硬修正。
        """
        import sys as _sys, platform as _pf
        if _sys.platform != "win32":
            return _pf.platform()          # mac/linux 没有上面那两个坑
        name, build, major, minor = "", 0, 10, 0
        try:
            _wv = _sys.getwindowsversion()
            major, minor, build = _wv.major, _wv.minor, _wv.build
        except Exception:
            pass
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as _k:
                name = str(winreg.QueryValueEx(_k, "ProductName")[0])
                if not build:
                    build = int(winreg.QueryValueEx(_k, "CurrentBuild")[0])
        except Exception:
            pass
        if not name:
            name = f"Windows {_pf.release()}"
        if build >= 22000 and "Windows 10" in name:
            name = name.replace("Windows 10", "Windows 11")
        return f"{name} {major}.{minor}.{build}" if build else name

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

    @staticmethod
    def _looks_chinese(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))

    def _session_log_append(self, entry: str):
        """往 session_log 追加一条记录。自动加时间戳。"""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M")
        self._session_log.append(f"{ts} {entry}")
        if len(self._session_log) > 20:
            self._session_log = self._session_log[-20:]


    def _build_audit_failure_injection(self) -> str:
        """把"上一个 Skill 没能部署，以及具体哪里不合协议"摆到模型面前。

        ═══ 为什么需要它（2026-08-05 实测）═══

        审计窗口拦下一个 Skill、用户点丢弃之后，模型手上什么都没有：
          · 代码从未进 memory（`skill_preview` 的 code 只给弹窗渲染）
          · 校验报错只进 `validation_lbl`，纯 UI
          · app 侧那条 `[System record: ...discarded...]` 被 max_turns=10 切掉了
        于是用户说"上次写的校验报错，重新写"时，它只能反问
        「哪个 Skill 出问题了？」「我需要先检查它的代码看看哪里报错」——
        而那个 Skill 压根没部署，`inspect_existing_skill` 查不到。
        **它不是笨，是真的什么都不知道。**

        ⚠️ 刻意**不写 memory**：memory 会被截断，那正是这条缺陷的一半。
        动态段每轮从实例状态重建，模型隔多少轮回来都还在。
        同 `[Open Interactions]` 的做法。

        没有失败记录时返回空串，一个字符都不加。
        """
        af = getattr(self, "_last_audit_failure", None)
        if not isinstance(af, dict) or not af.get("filename"):
            return ""
        _outcome = af.get("outcome") or "awaiting"
        _errs = af.get("errors") or []
        _fn = af.get("filename")
        _state = {
            "discarded": "The user reviewed it and discarded it. It was NOT deployed and its "
                         "code no longer exists anywhere — you cannot inspect it.",
            "awaiting": "It is still sitting in the audit window awaiting the user's decision.",
        }.get(_outcome, "")

        out = ["\n\n[Last Skill Audit — Not Deployed]",
               f"Skill: {_fn}  (mode={af.get('mode', 'create')}, "
               f"{af.get('code_lines', 0)} lines, code_hash={af.get('code_hash') or 'n/a'})",
               _state]
        if _errs:
            out.append("It failed the v3.x protocol check on these points:")
            out.extend(f"  - {e}" for e in _errs)
            out.append(
                "If the user asks you to rewrite or fix it, you already know which Skill and "
                "which problems — do not ask them, and do not try to inspect it (it was never "
                "deployed). Write a fresh version that fixes exactly the points above."
            )
        else:
            out.append(
                "It passed validation but the user discarded it anyway. Do not assume it exists. "
                "If they ask again, write it fresh; consider asking what they disliked."
            )
        return "\n".join(x for x in out if x)

    def _l3_index_block(self) -> str:
        """已经移出上下文的那些交换，留下的一行行索引。

        ⚠️ **低于一条就一个字都不注入** —— 📌 一段每轮都在、又暂时没有内容的
           标题会被模型学成背景噪音（同压力段那条纪律）。
        ⚠️⚠️ **刻意不判总开关。** `ladder_enabled` 是一个**单向迁移开关**：
           它控制"还产不产生新的衰减"，**不控制"已经产生的算不算数"**。
           关掉它之后，已经降到 L3 的那些交换仍然：hydrate 时被移出、
           UI 仍隐藏、索引仍注入 —— 三处**必须一致**，所以读侧一律不判开关。
           📌 已经发生的降级是**既成事实**，一个 bool 撤不回来 ——
              真要恢复旧行为，清 `exchange_decay` 表（见 `model_config.json`
              的 `_ladder` 说明）。
        ⚠️ 与 `recall_conversation` 的 availability 判据（`has_evicted_history`）
           **共用同一个事实**：有索引 ⇔ 有工具。
        """
        try:
            from core.context.decay import l3_index_lines
            from core.context.decay_store import DecayStore
            from core.runtime.kernel import get_kernel
            _sid = self.memory.conversation_session_id
            if not _sid:
                return ""
            lines_ = l3_index_lines(DecayStore(get_kernel().store), _sid)
        except Exception:
            return ""
        if not lines_:
            return ""
        return (
            "\n\n[Earlier in this conversation - moved out of context]\n"
            "These exchanges are no longer in your context, but they did happen and "
            "they are still recallable with recall_conversation. Use them to know that "
            "something exists, then recall it if the current question needs it. "
            "Do NOT claim you never discussed these." + "\n"
            + "\n".join(f"  {x}" for x in lines_) + "\n"
        )

    def _image_note_request_block(self) -> str:
        """只在「这轮有图且还没记」时出现的一次性提示。

        ⚠️⚠️ **它由 `has_unsummarized_image()` 控制 —— 和 `note_image` 工具是同一个条件。**
           📌 那条：**一个工具和它的事实来源，必须由同一个条件控制**；
              这里连提示词也挂在同一个条件上，于是三者永远同生共死。

        ⚠️ 「摘要千万不能跟 changelog 修复那个 base64 的 bug 一样，一直每轮注入」——
           ⭐ 这段**没有第二轮可注入**：模型一调 `note_image`，条件就为假。
           而万一它不调，下一轮还有图未记 —— 那时提示还在，这是**正确的**重试，
           不是"每轮都塞"。
        """
        try:
            if not self._tool_runtime_view().has_unsummarized_image():
                return ""
        except Exception:
            return ""
        # ⭐ 留痕：这段是一次性的，出现过就该看得见 —— 否则"摘要没生成"时
        #    分不清是【提示没注入】还是【模型没听】。两者的修法完全不同。
        logger.info("[D10] 本轮有图待记摘要 → 注入一次性提示 + 提供 note_image")
        # ⚠️⚠️ 措辞被实测推翻过一次（2026-08-13）：第一版只说"Before you reply, call
        #    note_image once"，**注入了、工具也给了，模型三次全部无视，直接作答**。
        #    根因不是它没读到，是**主循环的纪律在压制它** —— 系统提示里反复讲
        #    「闲聊不调工具 / 第一轮直接出话」，而这条要求它为一件**用户看不见的事**
        #    先调一次工具，正好撞在那条纪律上。
        # 📌 **一条新指令如果和系统反复强化过的纪律冲突，它必须自己说明"我覆盖那条"** ——
        #    否则模型会按更常被强调的那条走，而且不会告诉你它做了取舍。
        return (
            "\n\n[Image just received — one required tool call]\n"
            "The user's latest message carries an image, and its pixels will be dropped from your "
            "context shortly. Call note_image ONCE, first, before you reply.\n"
            "⚠️ This OVERRIDES the usual guidance about answering directly without tools. It is "
            "not optional and it is not a judgement call: an image is present, so this one call "
            "happens. It is the only chance to record what is in it.\n"
            "⚠️ The summary is NOT your answer. Write it for your future self, independently of "
            "what the user asked; then reply to their actual message in your own voice, and never "
            "recite the summary back to them.\n"
        )

    def _execution_scope_block(self, scope: str = "main_react") -> str:
        """每轮的执行作用域声明。极短（约 40 token）。

        ⚠️⚠️ **措辞必须是 "this model request"，不能写 "this turn"。**
        一个用户 turn 内部有**多次** ReAct model request，而 `load_tools` 之后
        `tools_manifest` 会在 turn 中途变化 —— 说 turn 就把粒度说错了一档，
        而这次要明确的粒度恰恰就是 request。

        ⚠️ **刻意不列工具名。** 当前 API manifest 已经是工具的唯一权威来源，
           在这里再列一遍就是第二份名单（正是 花一整轮消灭的东西）。
           所以这段话只讲**规则**，不讲**内容** —— 也因此它在一个 turn 内恒定。

        📌 而这段话之所以敢说 "Only tools attached to this model request are callable"，
           是因为 让它**成为了真的**（`TOOL_NOT_ACTIVE` 那道闸）。
           在 ③ 之前它是一句假话 —— 实测实证：`create_new_skill` 不在本轮 6 个工具里，
           模型凭历史 schema 调它，**照样执行了**。
           📌 **要注入一句话，先让它成为真的。**
        """
        try:
            from core.runtime.identity import current_runtime_id
            _rid = current_runtime_id()
        except Exception:
            _rid = "unknown"
        return (
            "\n\n[Execution Scope]\n"
            f"runtime_id={_rid}\n"
            f"scope={scope}\n"
            "Only tools attached to this model request are callable. "
            "Historical tool activation does not carry forward — an earlier "
            "\"loaded capabilities\" result does not make a tool callable now. "
            "If a capability you need is not attached, call load_tools first, "
            "then use it on the next step.\n"
        )

    def _build_quoted_selection_injection(self) -> str:
        """用户在聊天区**选中了一段文字**，然后右键 replay。

        ⭐ 与 `_build_open_interactions_injection` 是两件事，**刻意分开写**：
           那一段说的是「去答这条待办」（带 interaction_id，有工具要调）；
           这一段说的是「我在指刚才这句话」（没有 id，也没有工具要调）。
           📌 「一个字段不许表达两个现实」—— 合成一段就得靠 `iid` 空不空
              去猜是哪种，而那正是让人猜错的写法。

        ⚠️ 引用的是**聊天区里已有的文字**，所以它一定在上下文里（或者已被
           移出 —— 那更需要这一段，模型自己已经看不到原话了）。
           所以这里把原文**逐字**给它，不做摘要。

        ⚠️ 长度截到 200（UI 侧存的时候就截过）：引用是个指路标，不是重新贴一遍。
        """
        rt = (getattr(self, "_reply_target_turn", None)
              or getattr(self, "_reply_target", None) or {})
        if rt.get("kind") != "selection":
            return ""
        q = (rt.get("q") or "").strip()
        if not q:
            return ""
        return (
            "\n\n[Quoted by the user]\n"
            f'The user selected this text from earlier in this conversation and is '
            f'replying to it:\n"""\n{q}\n"""\n'
            "Their message is about THAT specific passage. Resolve any vague reference "
            "in it (this, that, it, here) against the quote above before anything else in "
            "the conversation. If the quote is something you said, they are pointing at "
            "your own words - do not treat it as a new topic."
        )

    def _build_open_interactions_injection(self) -> str:
        """把未决交互摆到模型面前。

        ⭐ 这是整条改造里**用户体感变化最大**的一块。
        旧实现是路由劫持：代码把下一条用户消息截走，模型自始至终不知道
        有个问题挂在那里。于是"用户回答了但被判成新话题"和"用户问了别的
        但被当成回答"这两种错误，模型都没有机会纠正——它看不见。

        现在改成把状态摆给模型，由它判断。三种走向都是它决定的：
            回答      → 调 answer_open_interaction
            改需求    → 同上，relation=ANSWER_AND_AMENDMENT
            说别的    → 不调工具，正常处理；交互保持 OPEN 留在屏幕上

        落在缓存哨兵之后的动态段：没有待办时返回空串、一个字符都不加。
        """
        recs = _rt_live_interactions(self)
        if not recs:
            return ""
        from core.runtime import interaction as _it

        # ⭐⭐ [2026-08-06 实测] 标注新旧，并把"最新那条"显式指出来。
        #
        # 实测："把 skill 部署吧"（不点名）→ Nano 部署了**队列里第一条**。
        # 那条恰好是最早创建的（前台槽给最先来的那个），而模型是照着这份清单挑的。
        #
        # 理由很硬：真实场景下 Nano 跟用户聊了很多轮，最后一轮刚做完一个 Skill，
        # 用户说"ok 部署吧" —— **指的必然是刚做完那个**。反手部署最早那个非常反直觉。
        #
        # ⚠️ 但**不在代码里硬选**（设计原则 3）：已明确说"问用户"和"部署最新的"
        # 两种都对，取决于人设模板与模型的谨慎程度。代码该做的是**把新旧这个事实
        # 摆清楚**，让模型能自己判断；唯一的硬约束是"别默认挑最旧的"。
        #
        # 顺序仍然保持与 UI 一致（前台优先），因为用户说"第一个"时两边得指同一条。
        _newest_id = max(recs, key=lambda x: x.created_at).interaction_id if recs else ""
        _now = 0.0
        try:
            from core.runtime.kernel import get_kernel as _gk
            _now = _gk().now()
        except Exception:
            pass
        lines = []
        _live_ids: set[str] = set()
        _has_audit = False
        for r in recs:
            if r.kind == _it.Kind.SKILL_AUDIT:
                _has_audit = True
            q = (r.prompt_text or "").strip().replace("\n", " ")
            if len(q) > 200:
                q = q[:200] + "…"
            _age = ""
            if _now and r.created_at:
                _m = max(0, int((_now - r.created_at) / 60))
                _age = f" · created {_m}m ago" if _m else " · created just now"
            _live_ids.add(r.interaction_id)
            _tag = "  ← NEWEST" if r.interaction_id == _newest_id and len(recs) > 1 else ""
            lines.append(f"  - {r.interaction_id} [{r.kind}]{_age}{_tag} {q}")
            if r.status == _it.Status.ANSWERED:
                # 重试入口。没有这行，ANSWERED 就成了没有消费者的永久状态
                # 早先就要求过必须定义谁来触发重试）。
                a = (r.answer_verbatim or "").strip().replace("\n", " ")
                if len(a) > 120:
                    a = a[:120] + "…"
                lines.append(
                    f"      ALREADY ANSWERED but the follow-up failed last time. "
                    f'The user already said: "{a}" — call answer_open_interaction again '
                    f"with the same text to retry. Do NOT ask them to repeat themselves."
                )

        # 「回复这条」的目标：只有它**还在上面这份清单里**才算有效指向。
        # 指向已关闭/已取代的那条 = 把模型逼进死角，理由见下方注释。
        #
        # ⭐⭐⭐ [2026-08-13 CMD63] **先读本轮快照，再退回实时值。**
        #    UI 一按发送就把指向移交到 `_reply_target_turn`（见 `hand_off_reply_target`），
        #    此刻 `_reply_target` 已经是空的 —— 只读它就等于永远读不到用户点的那条，
        #    那正是 CMD63「引用了审计卡却去新建 Skill」的根因。
        # ⚠️ 保留 `or _reply_target` 这一路：不经过 UI 发送路径的调用方
        #    （测试替身、将来别的入口）仍然只设了 `_reply_target`。
        _reply_iid = (
            (getattr(self, "_reply_target_turn", None)
             or getattr(self, "_reply_target", None) or {}).get("iid") or ""
        )
        if _reply_iid and _reply_iid not in _live_ids:
            logger.info(
                f"[Interaction] 「回复这条」目标 {_reply_iid} 已不在未决清单里，"
                f"本轮不注入指向（UI 侧应同步复位）"
            )
            _reply_iid = ""

        return (
            # ⚠️ 这段措辞第一版有两个实测暴露出来的缺口（2026-08-05，）：
            #
            # ① **只提了"answers"，完全没提"取消"。** 用户说"算了不做了"时，
            #    模型在这段里找不到任何适用的指令，于是只回了一句"好的，取消了"、
            #    **没调工具**，待办留在原地。`relation=CANCEL` 明明存在，
            #    但它只写在工具描述里，而模型是照着这段动态段决定要不要调工具的。
            # ② 原文强调 "not blocking" / "do not nag" —— 那是为了防它过度热情，
            #    结果把它压成了**过度冷淡**："just handle their request normally"
            #    成了阻力最小的路。
            #
            # 现在把三种走向**并列写全**，并且明确"关掉它需要调工具，光说一句不算"。
            "\n\n[Open Interactions]\n"
            "Nano is waiting on the user for these. They do not block anything: "
            "the user may reply, may drop the whole thing, or may talk about something else.\n"
            "Three cases, and only the first two involve the tool:\n"
            "1. The message answers one of them (even while also changing the requirement) "
            "→ call answer_open_interaction with relation=ANSWER or ANSWER_AND_AMENDMENT.\n"
            "2. The message says they no longer want it ('forget it', 'never mind', "
            "'drop that skill') → call answer_open_interaction with relation=CANCEL. "
            "**Replying 'okay, cancelled' in text does NOT close it** — the item stays open "
            "and will keep showing up here until the tool records the cancellation.\n"
            "3. The message is about something else entirely → handle it normally, call no tool, "
            "leave the item open. Do not nag them about it.\n"
            # ⭐⭐⭐ [2026-08-06 实测] `[skill_audit]` 的语义必须写出来。
            #
            # 上面那三条是照着**澄清**写的（"Nano 在等你回答问题"）。对审计条目
            # 这个框架是错的：审计不是一个问题，**代码已经写完了在等批准**。
            #
            # 实测复现（19:37）：用户在有真代码的待审卡 int_9eeac9e476 上点了
            # 「回复这条」，然后说「你能不能目前修改这个代码」——
            # 指向是活的、是对的，`answer_open_interaction` 也在工具表里，
            # 模型照样走了 `create_new_skill`，并且原话说
            # **「到现在为止，我们只是澄清了需求，还没有生成任何代码」**。
            #
            # 它不是不听话，是**它看到的那一行只有一个不透明的标签 `[skill_audit]`**，
            # 头部还告诉它"这些是 Nano 在等用户回答的问题"。于是"改这个代码"、
            # "部署这个吧"、"继续"这些话，它认得的唯一出口就是 create_new_skill
            # → 每轮再造一个同名 Skill → 卡片越堆越多 → 死循环
            #   （最后堆了三张同名 GetComputerIP 的待审卡）。
            #
            # 📌 判据：**给模型一个状态标签，不等于给了它这个状态的语义。**
            #    枚举名对写代码的人是自解释的，对模型只是一个陌生字符串。
            + ("\n[About the skill_audit items above]\n"
               "Those are NOT questions — **the code is already written**. Nano finished a "
               "draft and it is sitting in the review window, waiting for the user's call. "
               "So for a skill_audit item:\n"
               "- ANSWER ('deploy it', 'go ahead', 'looks good', 'yes') → the draft is "
               "deployed as-is.\n"
               "- ANSWER_AND_AMENDMENT ('change X', 'use Y instead', 'can you modify this "
               "code', 'make it also return Z') → the draft is rewritten with that change. "
               "**This is how you modify pending code — you do not need to write it again.**\n"
               "- CANCEL ('forget it', 'drop that one') → the draft is discarded.\n"
               "⚠️ Do NOT call create_new_skill for a requirement that already has a "
               "skill_audit item open. That does not act on the existing draft — it produces "
               "a second draft under the same name, and the user ends up with a pile of "
               "near-identical review cards.\n"
               if _has_audit else "")
            # ⭐ 用户点了卡片上的「回复这条」→ 指向是**显式**的，不用猜。
            # 这与下面那段歧义消解互补：那条让模型猜得更准，这条让它根本不必猜。
            #
            # ⚠️⚠️ 必须先确认 `_reply_target` 指的那条**还活着**（`_reply_iid` 已过滤）。
            #
            # 2026-08-06 实测：用户在 17:26 点了澄清
            # int_192a0393ab 的「回复这条」，17:46:13 那条被草稿消化 → SUPERSEDED。
            # 但 `_reply_target` 只有用户手点 ✕ 才清，于是**之后每一轮**都注入
            # 「answering int_192a0393ab … do not pick a different one」，
            # 而下面的清单里根本没有它。
            #
            # 后果比"多一句废话"严重得多：17:58 用户说「部署这个吧」，模型被
            # **点名指向一条不存在的交互 + 明令禁止改挑别的** ——
            # `answer_open_interaction` 无路可走，于是退回 `create_new_skill`，
            # 新建了一个 Skill 而不是部署待审那个（用户报的最恶性那条）。
            #
            # 📌 判据：**指向性的注入必须校验指向的东西还在。**
            #    一个悬空的"必须回答 X"比没有这句话更糟 —— 它把模型逼进死角，
            #    而模型只能从别的工具里找出路。
            #
            # ⚠️ 这只是安全网。真正的修法是 UI 侧发完消息就复位（用户的 BUG5），
            #    两边都要有：目标也可能在选中期间被别人关掉。
            + (f"\n⭐ The user explicitly marked this message as answering "
               f"{_reply_iid} "
               f"— they clicked \"reply to this\" on that card. "
               f"Treat it as the answer to that item; do not pick a different one.\n"
               if _reply_iid else "")
            # ⭐⭐ 歧义消解（2026-08-06 实测）：不点名时该指哪一条。
            # 见上面 `_newest_id` 那段注释里用户的理由。
            # ⚠️ 上面插了一个 `+ (条件表达式)` 之后，这里**必须继续用 `+`** ——
            # 裸字符串没法跟一个 `+` 表达式的结果做隐式拼接（那是语法错误）。
            + "\nIf several items are open and the user does not say which one "
            "('deploy it', 'go ahead', 'yes'):\n"
            "- The list is ordered newest first, so item 1 is the most recent. "
            "That one (also marked ← NEWEST) is almost always what they mean — "
            "they were just talking about it. Prefer it.\n"
            "- Asking which one is also fine, and is the better choice when the items are "
            "similar or when acting on the wrong one would be hard to undo.\n"
            "- **Never quietly pick the oldest one.** After a long conversation, 'deploy it' "
            "referring to something from twenty turns ago is not a reading a person would make.\n"
            + "\n".join(lines)
        )

    def _build_session_log_injection(self) -> str:
        """Format current-session operation records for system_guide injection.

        Return empty text when there is no session log.
        Limit each entry length to avoid bloating system_guide.
        """
        if not self._session_log:
            return ""
        lines = []
        for entry in self._session_log[-10:]:  
            if len(entry) > 120:
                entry = entry[:117] + "..."
            lines.append(f"  • {entry}")
        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Session Log]\n" + "\n".join(lines)

    def _build_episodic_injection(self) -> str:
        """Inject recent cross-session summaries from working memory into system_guide."""
        if self._wm is None:
            return ""
        try:
            rows = self._wm.get_recent_session_ends(limit=5)
        except Exception:
            return ""
        if not rows:
            return ""
        lines = []
        for r in rows:
            ts_short = r["ts"][5:16] if r.get("ts") else ""  # MM-DD HH:MM
            subject = r.get("subject", "")[:40]
            outcome = r.get("action", "")  # success/failed/chitchat/partial
            outcome_badge = {"success": "✓", "failed": "✗", "partial": "~", "chitchat": "💬"}.get(outcome, "")
            import json as _json
            try:
                skills = _json.loads(r.get("tags", "[]") or "[]")
            except Exception:
                skills = []
            skills_str = f" [Skills: {', '.join(skills)}]" if skills else ""
            lines.append(f"  • [{ts_short}]{(' ' + outcome_badge) if outcome_badge else ''} {subject}{skills_str}")
        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Recent Cross-Session Summaries]\n" + "\n".join(lines)

    # ⭐⭐⭐ [2026-08-25 实测] **把「窗口现在是什么形态」注入给模型。**
    #
    # 🔴 实测：Nano **每一个气泡都要调一次 `set_window_mode('mini')`**。
    #    查下来根因正如猜的那样 —— 全项目 grep「窗口当前是不是 mini」**零命中**，
    #    **从来没有任何地方告诉过模型这件事**。
    # 🔴 而 `look_at_screen` 的描述里那句是硬的：
    #      「call set_window_mode('mini') BEFORE your first look_at_screen
    #        in a task … **This one is not optional.**」
    #    ⇒ 「算不算 in a task」只能它自己猜，而 `wait_for` 之后是新的一轮、
    #      隔了一分钟 —— 猜不出来，按「not optional」就只能再调一次。
    # 📌 **一个「不许省」的强规则，配上一个模型看不见的状态，
    #    结果必然是每次都做。** 那不是它谨慎，是我们没给它判断的依据。
    #
    # ⭐ 这是「注入事实」这个模式的**第五个实例**：
    #      健康态 → 预算态 → 上下文压力 → 记忆索引 → 本条
    #    规律：**给模型注入它自己的状态，它就会自主调整行为。**
    #
    # ⚠️ 权威取 **租约**，不取 UI 的 `_mini_active` ——
    #    `_enter_mini` 那段留痕写着「`_mini_active` 从此**只是投影**，
    #    权威在租约里」。📌 读投影就是给自己造第二个权威。
    # ⚠️ fail-safe 方向：读不到 → 说「full」。
    #    说错成 full → 模型多缩一次窗（幂等，无害）；
    #    说错成 mini → 模型**跳过缩窗直接操作屏幕**，而它正挡着屏幕。
    #    📌 两边代价不对称时，往代价小的那边倒。
    # ⭐⭐⭐ **记忆摘要每轮无条件注入 —— 整个改造的核心。**
    #
    # 🔴 2026-08-05 的原话，根因当时就说对了：
    #    > cc 会在合适的时候召回记忆是因为 **cc 每次都注入了摘要**；
    #    > nano 只是很软地声明了「在 xx 时候必须先查看记忆再回答」。
    #    > **太模糊，nano 没办法先入为主地「预览到」记忆里大概有些什么。**
    # 📌 **一条召回不到的记忆，和一条没写过的记忆，对用户是同一个东西。**
    # ⭐ 沿用这句：**索引不是被回忆的，是被塞进来的。**
    # ⭐ 这是「注入事实」模式的第六个实例：
    #    健康态 → 预算态 → 上下文压力 → 记忆索引 →
    #    [窗口形态] → 本条。
    #
    # ═══ ⚠️ 保底（floor），不是配额（quota）—— 2026-08-24 定 ═══
    # 一度建议「N 写成配额，和 L0/L1/L2 共用水位」。用户否掉：
    #    「我没把握，因为如果特别小对这部分是致命的，**因为纠错本身是一等公民**」
    # 这个判断是对的：
    #    L0→L1→L2→L3 是**分辨率阶梯**，每一档都还携带着东西，降档 = 变模糊
    #    记忆     **没有半条**：要么完整生效，要么等于不存在
    # 📌 **配额是给「可以降分辨率的东西」用的。** 记忆降不了分辨率，
    #    进那套机制的唯一结果是「压力一大就被挤掉」——
    #    而压力大 ＝ 对话长 ＝ 用户投入最多的时刻。
    #
    # ⭐⭐ [2026-08-25] **连「保底 40 条」这个数字也拿掉了。**
    #    原话：「我们不数记忆的条数，我们数记忆导致的常驻 token 注入的消耗」。
    #    ⇒ **全部注入，不截断**；约束改成下面那套按 token 水位的提醒。
    # 📌 为什么：**一个「会积累的东西」的清除条件，应该来自它消耗的那个资源** ——
    #    一条 30 字的和一条 300 字的，条数上一样，成本差十倍。
    #    「40 条」是那个资源的**代理变量**，而代理变量在密度变化时就失效
    #    （这条判据是 L3→L4 那格定下来的，这里是它的第二次兑现）。
    # ⚠️ 于是这里**不再有任何常量** —— 阈值全在 `_MEM_WARN_ON/OFF` 那一段，
    #    而且那三个数是**算出来的**（见那段注释），不是拍的。


    # ══════════════════════════════════════════════════════════════════════
    # ⭐⭐⭐ **记忆的自治理：数 token，不数条数。**
    #
    # 已定形状：
    #   · 不设条数上限 —— 全部注入
    #   · 盯的是**这些记忆每轮真实花掉多少 token**
    #   · 越过阈值才注入一句话，让 Nano 在**这一轮自然收尾时**跟用户提一句，
    #     并且**自己先判断**哪些低价值/过时，但**不许擅自删**
    #
    # 📌 为什么数 token 不数条数：**一个「会积累的东西」的清除条件，
    #    应该来自它消耗的那个资源** —— 一条 30 字的和一条 300 字的，
    #    条数上一样，成本差十倍。（这条判据是 L3→L4 那格定下来的。）
    #
    # ═══ 两条线 + 只记「上次提醒时的水位」═══
    # 🔴 已明确两个问题，根子是同一个：**只有一条线的开关会在线附近抖动。**
    #    ① 用户不理会 → 每轮都提 → Nano 当成迫切问题
    #    ② 1200 提示 → 删到 900 → 又写回 1200 → 又提示（鬼畜）
    # ⭐ 修法：触发线与解除线分开，并且**只记上次提醒时的水位**，
    #    要再涨 50% 才提第二次。
    #      提醒条件： 水位 > WARN_ON  且  水位 > last_warned × 1.5
    #      提醒之后： last_warned ← 当前水位
    #      跌破 WARN_OFF： last_warned ← 0（重新武装）
    #
    # ⭐⭐ **我们从不判断用户答了什么。** 提醒的语义是「告诉你一声」，
    #    而告诉一次就够了；再涨一大截是**新的事实**，值得再说一次。
    #    📌 一旦要判断「用户同意还是拒绝」，就得做自然语言理解 ——
    #       而那正是本项目明确否过的方向（「不许让代码去理解『算了别点了』」）。
    #    ⇒ 用户越不在意，提醒频率**指数级变稀**（1200 → 1800 → 2700 → …）。
    #
    # ═══ 阈值是**算出来的**，不是拍的═══
    #   注入格式 `  - {英文 summary_model}  [when: {英文 when}]`
    #   四条真实样例实测（`meter.estimate_text`）：平均 **20.2 token/条**，表头 27。
    #     20 条 ≈  432    40 条 ≈  837    60 条 ≈ 1242    80 条 ≈ 1647
    #   ⇒ WARN_ON 1200 ≈ **58 条**：58 条之前一句不提
    #     忽略一次后 1800 ≈ **88 条**：再攒 30 条才提第二次
    #     WARN_OFF  800 ≈ **38 条**：要真清理到 40 条以内才重新武装
    # ⚠️ 英文摘要 ≈20 token/条，同内容中文 ≈38 —— 强制 `summary_model` 写英文
    #    这一条本身就把水位压掉了一半。
    # ══════════════════════════════════════════════════════════════════════
    _MEM_WARN_ON = 1200          # 触发线（token）≈ 58 条
    _MEM_WARN_OFF = 800          # 解除线（token）≈ 38 条
    _MEM_WARN_GROWTH = 1.5       # 再涨这么多倍才提第二次

    def _mem_water_path(self):
        import pathlib as _pl
        from core.paths import data_path
        return data_path("memory_water.json")

    def _mem_water_read(self) -> float:
        """上次提醒时的水位。读不到当 0（= 未提过）。

        ⚠️ fail-safe 方向是 **0**：读不到就当没提过，最多多提一次；
           反过来（当成提过）会让一个真的涨上去的水位**永远不提**。
        """
        try:
            import json as _j
            return float(_j.loads(self._mem_water_path().read_text("utf-8"))
                         .get("last_warned_at", 0) or 0)
        except Exception:
            return 0.0

    def _mem_water_write(self, v: float) -> None:
        try:
            import json as _j
            _p = self._mem_water_path()
            _p.parent.mkdir(parents=True, exist_ok=True)
            _p.write_text(_j.dumps({"last_warned_at": round(float(v), 1)}), "utf-8")
        except Exception as e:
            logger.debug(f"[O2] 水位留痕失败（不影响本轮）: {e}")

    def _mem_water_notice(self, injected: str) -> str:
        """按注入内容算水位，越线就返回那句提醒；否则空串。

        ⚠️ 算的是**真实注入出去的那段文本**，不是估的条数 ——
           📌 要管一个资源，就得量它本身，不能量它的代理变量。
        ⚠️ 整段吞异常：📌 治理是家务，不该有能力让这一轮失败。
        """
        try:
            if not injected:
                return ""
            from core.context.meter import estimate_text as _est
            _now = float(_est(injected))
            _last = self._mem_water_read()
            if _now < self._MEM_WARN_OFF and _last:
                self._mem_water_write(0)          # 真清理过 → 重新武装
                return ""
            if _now <= self._MEM_WARN_ON or _now <= _last * self._MEM_WARN_GROWTH:
                return ""
            self._mem_water_write(_now)
            logger.info(f"[O2] 记忆注入水位 {_now:.0f} token 越线（上次 {_last:.0f}）→ 提醒一次")
            # ⚠️ **不列候选**：那句提醒本身就是为了省 token，
            #    它自己很长就矛盾了。而 Nano 手上已经有全部摘要，
            #    足够做一轮浅判断；真要确认某条是否过时，再去 recall 全文。
            # ⚠️ 明说要先 load `forget_user_note` —— 📌 否则会重演 computer_use
            #    那个坑：告诉它去用一个它手上没有的工具。
            #
            # 🔴🔴 [2026-08-25 实测] 第一版写的是
            #    「Finish what you are doing first. Then, when this turn **wraps up
            #      naturally**, mention it to them」—— **它一个字都没提。**
            #    日志显示提醒确实注进去了（dyn 7713 → 下一轮 6373），是模型没照做。
            # 📌 根因是改动了用户的措辞：原话是「**你在你的下一条消息当中**，
            #    应该向用户给出建议」，被软化成了「自然收尾时顺带提一句」。
            #    **一条明确的指令被软化成了一条建议，然后它就被当成建议对待了。**
            # ⚠️ 软化的动机是「别劫持当前任务」—— 但那个担心该靠**位置**解决
            #    （答完用户再说），不该靠**削弱语气**解决。两者被混成了一件事。
            # ⚠️ 而对着一句「你好」，「自然收尾时」几乎等于没说：这一轮没什么要收尾的。
            # ⭐ 现在：**这一轮必须说**（明确），但**放在回答之后**（不劫持）。
            #    并且如实告诉它「跳过就没有下一次了」—— 📌 一个不说明代价的指令，
            #    模型没有理由把它排在别的事情前面。
            return (
                f"\n\n[Memory cost - act on this in THIS reply]\n"
                f"The memories above now cost about {_now:.0f} tokens on every single "
                f"turn. Nothing is broken; the user just deserves to know.\n"
                f"Answer the user normally first. Then, at the END OF THAT SAME REPLY, "
                f"add a short paragraph that: says roughly what the memories cost per "
                f"turn; names the ones you think have gone stale or low-value (you can "
                f"see them all above - judge for yourself, do not make the user audit "
                f"the list); and offers to delete those for them.\n"
                f"To actually delete, load forget_user_note first - it is not attached "
                f"by default. Delete only what they agree to; never on your own.\n"
                f"Do NOT skip this and do NOT save it for a later turn: this notice "
                f"appears once and will not come back until the cost grows a lot more. "
                f"If they say the cost is fine, that is a perfectly good answer."
            )
        except Exception as e:
            logger.debug(f"[O2] 水位判断跳过: {e}")
            return ""

    def _build_memory_injection(self) -> str:
        """把已确认记忆的 `summary_model` 一行行塞进动态段。

        ⚠️ 用 `summary_model` **不是** `summary_user` —— 两句话是分开写的：
           📌 一句话同时服务两个受众，最后两边都不合身（这个坑踩过）。
        ⚠️ 带上 `applies_when`：📌 一条不说「什么时候用得上」的记忆，
           模型只能每条都掂量一遍；把作用域摆在旁边，它才能一眼跳过不相关的。
        ⚠️ 整段吞异常并返回空串：📌 记忆是**增益**，读不到不该让这一轮失败。
        """
        try:
            if self._wm is None:
                return ""
            rows = self._wm.get_all_confirmed_notes()
        except Exception as e:
            logger.debug(f"[O2] 读记忆失败（本轮不注入）: {e}")
            return ""
        if not rows:
            return ""
        lines = []
        # ⚠️ **不设条数上限**：📌 要管一个资源就得量它本身 ——
        #    一条 30 字的和一条 300 字的，条数上一样，成本差十倍。
        #    真正的闸是下面那个**按 token 水位**的提醒。
        for r in rows:
            _s = (r.get("summary_model") or r.get("detail") or "").strip()
            if not _s:
                continue
            _w = (r.get("applies_when") or "").strip()
            # 🔴 [2026-08-25 实测] 这一行原来**不带 id** —— 而 `forget_user_note`
            #    要的正是 id。实测里 Nano 判断出「这条已经过时该删」（完全正确），
            #    然后**只能猜**，猜了 1 和 2，两次都 "No memory with id N was found"。
            # 📌 **我们让它做一件事，却没给它做这件事需要的东西** ——
            #    跟 `look_at_screen` 只给散文不给坐标是同一个形状。
            #    ⚠️ 而工具描述里还写着「by the id shown next to it」，
            #       **根本没有任何地方 shown** —— 一句指向空气的描述。
            # ⚠️ 代价：编号约 4 字符 ≈ 1~2 token/条，可以忽略。
            lines.append(f"  - #{r.get('id')} {_s}" + (f"  [when: {_w}]" if _w else ""))
        if not lines:
            return ""
        _block = ("\n\n[What you remember about this user]\n"
                  "These are already in front of you - you do NOT need to look them up.\n"
                  + "\n".join(lines))
        # ⭐ 水位按**真实注入出去的那段文本**算，不按条数估。
        return _block + self._mem_water_notice(_block)

    def _build_window_mode_injection(self) -> str:
        try:
            from core.runtime import oslease as _ol_wm
            from core.runtime.kernel import get_kernel as _gk_wm
            _mini = bool(_ol_wm.gui_session_active(_gk_wm()))
        except Exception:
            _mini = False
        if _mini:
            return ("\n\n[Window] Nano's own window is CURRENTLY MINI (small, "
                    "top-right corner) — you already shrank it. Do NOT call "
                    "set_window_mode('mini') again; it is already done.")
        return ("\n\n[Window] Nano's own window is CURRENTLY FULL SIZE. If you are "
                "about to look at or operate the screen, shrink it first with "
                "set_window_mode('mini').")

    def _build_ambient_injection(self) -> str:
        """Ambient Memory Phase 1: inject a lightweight rule-based activity snapshot.

        Nano can use this to resolve references such as "this", "that thing",
        or "what I was just doing" without extra model calls.
        This uses only context-level signals such as app name, window title,
        activity rhythm, idle time, saves, and background audio. It does not read private content.
        Nano's own window is excluded because the real ambient context is usually the window
        the user was using before switching to Nano.
        """
        try:
            from core.proactive.activity import get_buffer
            from core.os_layer.executor_low import _is_self_window_title
            snap = get_buffer().snapshot()
        except Exception:
            return ""
        now = snap.get("now", time.time())
        windows = snap.get("windows", [])
        keys = snap.get("keys", [])
        saves = snap.get("saves", [])

        def _self(w) -> bool:
            try:
                return _is_self_window_title(getattr(w, "window_title", "") or "")
            except Exception:
                return False

        # Use all focus events as time boundaries, including Nano windows.
        # Nano windows are excluded from reporting, but they still provide accurate duration boundaries.
        all_focus = sorted([w for w in windows if getattr(w, "event", "") == "focus"],
                           key=lambda w: w.ts)
        if not all_focus and not keys:
            return ""

        def _dur(sec: float) -> str:
            m = int(sec // 60)
            return f"about {m} min" if m >= 1 else "less than 1 min"

        dwell: dict = {}
        last_real = None
        app_title: dict = {}
        for i, w in enumerate(all_focus):
            end = all_focus[i + 1].ts if i + 1 < len(all_focus) else now
            dur = max(0.0, end - w.ts)
            if _self(w):
                continue
            app = (w.process_name or "").replace(".exe", "").strip() or "Unknown"
            dwell[app] = dwell.get(app, 0.0) + dur
            app_title[app] = w.window_title or ""
            last_real = w
        cur = last_real
        span_min = int((now - all_focus[0].ts) / 60) if all_focus else 0
        top = sorted(dwell.items(), key=lambda kv: -kv[1])

        char_n = sum(1 for k in keys if getattr(k, "key", "") == "char")
        bs_n = sum(1 for k in keys if getattr(k, "key", "") == "backspace")
        last_act = max([k.ts for k in keys] + [w.ts for w in all_focus] + [0.0])
        idle_sec = now - last_act if last_act else 9999

        # Interpretation layer: app category + title parsing + restrained activity guess.
        _cat = _ambient_cat
        _parse_title = _ambient_parse_title
        cur_app = (cur.process_name or "").replace(".exe", "").strip() if cur else ""
        dom_app = top[0][0] if top else cur_app
        dom_cat = _cat(dom_app)
        f_name, proj, page = _parse_title(cur_app, cur.window_title if cur else "")
        n_switch = sum(1 for w in all_focus if not _self(w))

        dom_url = (snap.get("url_domain") or "").strip()
        guess = ""
        if dom_cat == "editor":
            guess = ("looks like editing/debugging code" if (char_n >= 60 and bs_n * 3 >= char_n)
                     else "looks like writing code" if char_n >= 60 else "looks like reading code")
        elif dom_cat == "browser":
            if dom_url:
                guess = f"looks like browsing {dom_url}"
            else:
                guess = "looks like researching or looking for something" if n_switch >= 4 else "looks like reading a webpage"
        elif dom_cat == "im":
            guess = "looks like chatting or communicating"
        elif dom_cat == "terminal":
            guess = "looks like running commands or debugging"
        if not guess and char_n == 0 and idle_sec > 180:
            guess = "was idle for a while and may have just returned"

        import datetime as _dt2
        parts = []
        if cur:
            since = now - cur.ts
            ago = "was just in" if since < 90 else f"was in about {int(since // 60)} min ago"
            tgt = cur_app
            if f_name:
                tgt += f" ({f_name[:40]})"
            elif page:
                tgt += f" ({page[:40]})"
            lead = f"{ago} \"{tgt}\""
            # ⭐⭐ **「刚才」那一条往往根本不在 trail 里。**
            #    `recent()` 刻意 `exclude_last_sec=600`，而 trail 每 4 分钟才写一次 ——
            #    用户在记事本改完切过来就问，命中的是**实时 buffer**，不是 trail。
            #    📌 只给 trail 加句柄的话，那个最常见的场景一条都拉不到。
            _cur_ref = getattr(cur, "ref", None) or {}
            if _cur_ref.get("path"):
                lead += " ▸" + _dt2.datetime.fromtimestamp(cur.ts).strftime("%H:%M:%S")
            if guess:
                lead += f", {guess}"
            parts.append(lead)
        elif guess:
            parts.append(guess)

        if top:
            def _seg_one(a, s):
                # Browser includes page title; editor includes file name when available.
                fn, pj, pg = _parse_title(a, app_title.get(a, ""))
                detail = fn or pg
                d = f" {detail[:36]}" if detail else ""
                return f"{a}{d} ({_dur(s)})"

            seg = "; ".join(_seg_one(a, s) for a, s in top[:4])
            head = f"over the last about {span_min} min, mainly in" if span_min >= 1 else "recently, mainly in"
            parts.append(f"{head}: {seg}")

        if saves:
            # ⭐ 这里原来直接甩**原始完整窗口标题**，是全局
            #    唯一一处没过 `_parse_title` 的地方 —— 它反而会漏出
            #    `xxx.txt - 记事本` 这种带文件名的串。
            # 📌 **歪打正着 ≠ 可控**：改完 ① 之后，同一个文件会在同一段话里
            #    出现两种写法（`notepad (a.txt)` 和 `saved a file (a.txt - 记事本)`），
            #    而模型没有理由知道它俩是一个东西。⇒ 两条对齐到同一个解析器。
            # ⚠️ 解析不出来时**退回原始标题**，不是退回空 —— 一个不认识的应用，
            #    完整标题仍然比什么都没有强。
            _sv = saves[-1]
            _sf, _, _sp = _parse_title(
                (_sv.process_name or "").replace(".exe", "").strip(),
                _sv.window_title or "")
            st = (_sf or _sp or (_sv.window_title or "")).strip()
            parts.append("recently saved a file" + (f" ({st[:40]})" if st else ""))

        audio = list(snap.get("audio_apps") or [])
        if audio:
            parts.append("background audio: " + ", ".join(audio[:2]))

        # Persistent trail from earlier today, outside the short live buffer.
        try:
            from core.proactive import ambient_trail
            import datetime as _dt
            tr = ambient_trail.recent(hours=12, limit=6)
            segs, prev = [], None
            for r in tr:
                ln = r.get("line", "")
                if ln and ln != prev:
                    # ⭐ 精度提到**秒**：这个时刻同时是拉取时的 id。
                    # 🔴 **绝不能用序号** —— trail 是滚动的（18h/300 条）且每 4 分钟
                    #    新增一条，注入取的是最后 6 条：
                    #      第 N 轮模型看到「#3」→ 第 N+1 轮它调工具要 #3 → 已经不是那条了。
                    #    📌 **一个会在两次调用之间漂的标识，漂了没人知道。** 时刻不漂。
                    hhmm = _dt.datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
                    _mark = " ▸" if (r.get("ref") or {}).get("path") else ""
                    segs.append(f"{hhmm} {ln}{_mark}")
                    prev = ln
            if segs:
                parts.insert(0, "earlier today: " + "; ".join(segs))
        except Exception:
            pass

        if not parts:
            return ""

        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Ambient]\n" + "; ".join(parts) + "."

    def _ambient_scene_now(self):
        """当前现场的一行摘要 —— **起返回 `(line, ref)`**。

        ⚠️ `ref` 取的是「停留最久那个应用**最后一条**焦点记录」上的句柄：
           dwell 决定的是「这段时间主要在哪」，而句柄必须落在**具体某一次**
           焦点上。📌 一个按时长聚合出来的对象，不能带一个按次记录的地址 ——
           除非明确说清取的是哪一次。
        """
        try:
            from core.proactive.activity import get_buffer
            from core.os_layer.executor_low import _is_self_window_title
            snap = get_buffer().snapshot()
        except Exception:
            return ("", None)
        now = snap.get("now", time.time())
        windows = snap.get("windows", [])
        keys = snap.get("keys", [])
        all_focus = sorted([w for w in windows if getattr(w, "event", "") == "focus"],
                           key=lambda w: w.ts)
        dwell, titles, refs = {}, {}, {}
        for i, w in enumerate(all_focus):
            end = all_focus[i + 1].ts if i + 1 < len(all_focus) else now
            try:
                if _is_self_window_title(getattr(w, "window_title", "") or ""):
                    continue
            except Exception:
                pass
            app = (w.process_name or "").replace(".exe", "").strip() or "Unknown"
            dwell[app] = dwell.get(app, 0.0) + max(0.0, end - w.ts)
            titles[app] = w.window_title or ""
            if getattr(w, "ref", None):
                refs[app] = w.ref            # 后来的覆盖先前的 = 最后一次
        if not dwell:
            audio = snap.get("audio_apps") or []
            return (("background audio: " + audio[0]), None) if audio else ("", None)
        dom = max(dwell.items(), key=lambda kv: kv[1])[0]
        cat = _ambient_cat(dom)
        fn, pj, pg = _ambient_parse_title(dom, titles.get(dom, ""))
        char_n = sum(1 for k in keys if getattr(k, "key", "") == "char")
        bs_n = sum(1 for k in keys if getattr(k, "key", "") == "backspace")
        act = ""
        if cat == "editor":
            act = "editing code" if (char_n >= 60 and bs_n * 3 >= char_n) else "writing code" if char_n >= 60 else "reading code"
        elif cat == "browser":
            act = "reading a webpage"
        elif cat == "im":
            act = "communicating"
        elif cat == "terminal":
            act = "running commands"
        detail = fn or pg
        line = f"in {dom}"
        if act:
            line += f" {act}"
        if detail:
            line += f" ({detail[:30]})"
        return (line, refs.get(dom))

    def record_ambient_trail(self) -> None:
        """由 app.py 定时器周期调用：把当前现场追加进持久轨迹（Phase 4）。"""
        try:
            from core.proactive import ambient_trail
            line, ref = self._ambient_scene_now()
            if line:
                ambient_trail.append(line, ref)
        except Exception:
            pass

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

        logger.info(
            f"[Reset] 对话已重置（memory={had_memory}, "
            f"pending_skill={had_pending_skill}, pending_action={had_pending_action}, "
            f"interactions={_cancelled_interactions}）"
        )
        return {"ok": True, "msg": "已重置当前对话。历史和待处理状态已全部清空。"}

    # ── 工具清单 / 辅助 ─────────────────────────────────────────────────


    # _handle_os_task 已删除（OS 双循环合一，0628）：OS 不再有独立循环，
    # os_execute 始终注入主 ReAct 循环，由 _run_react_loop + _execute_one_tool_call
    # 的 os_execute 分支统一执行。

    # ── _handle_pending_action 已删除（· ②c · 2026-08-06）──────────
    # 117 行的标签分支 + 一个专用分类器 + 一个伪事件 `__pending_new_request__`
    # 用来从生成器里逃逸回常规路由。整套被一条 `skill_manage` Interaction 取代。
    # 处置逻辑现在在 `_handle_answer_interaction` 的 SKILL_MANAGE 分支里。

    # ══════════════════════════════════════════════════════════════════════
    # ReAct 主循环 — 工具执行层
    # ══════════════════════════════════════════════════════════════════════

    # ⭐⭐⭐ 等待的读写都直接进入 `waitcond`。
    # 切读期的 Authority 门面与观测期的 shadow 映射已经完成使命：继续保留会让后来调用方
    # 误以为仍有两套身份/两套账。历史 `legacy_susp_id` 只是一列反查数据，不是权威。

    def _build_suspension_resume_injection(self, records: list) -> str:
        """把待恢复的挂起记录拼成一段注入文本，让模型唤醒时知道自己在等什么。

        参考 Claude Code：状态本身在对话上下文里，这段只是显式提醒"你之前挂起了，
        现在被唤醒，先核对等的事成了没"，避免模型忽略历史里的 wait_for。
        """
        if not records:
            return ""
        lines = ["\n\n[Suspension Resume — Previous Wait State]"]
        for r in records:
            _src = "/".join(r.wake_on)
            lines.append(f"- Waiting for: {r.reason} (wake sources: {_src})")
        lines.append(
            "You have now been resumed. First verify whether the awaited condition is satisfied:\n"
            "- If satisfied, continue the task.\n"
            "- If not satisfied, you may call wait_for again, or briefly tell the user the current state and what is needed.\n"
            "Do not pretend Nano was working in the background while suspended; Nano was paused until this wake event."
        )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 工具失败诊断 —— 让模型拿到【正确且充分】的失败信息
    # ══════════════════════════════════════════════════════════════════════
    #
    # ═══ 这不是"安全网"，是在修一条说假话的错误信息 ═══
    #
    # 改造前，模型调一个**不存在**的工具名，收到的是：
    #
    #     "Error: the tool did not return any valid content."
    #
    # 这句话的意思是「工具跑了，但没返回内容」——**和事实正好相反**，
    # 工具压根没被调用（`registry.execute` 找不到就 `return None`，见 registry.py:280，
    # 然后 None 在这里被翻译成了上面那句）。
    #
    # 更糟的是：一个**真实存在**、只是 `run()` 返回了 None 的 Skill，
    # 拿到的是**一模一样的字符串**。两种完全不同的故障连区分都做不到，
    # 所以模型就算想诊断也诊断不出来，只能换参数重试或者再猜一个名字。
    #
    # 也就是说：模型不是"判断失误需要被兜住"，是**在一个假前提上做了在那个前提下
    # 完全合理的判断**。错的是我们。
    #
    # ═══ 判据═══
    #
    #     拿着这条失败消息，一个没有别的上下文的人能不能选出正确的下一步？
    #     不能就不合格 —— **哪怕它技术上没说错**。
    #
    # 所以这里要满足两条独立要求：
    #     正确 = 描述的是真实发生的事（区分"不存在" / "参数不对" / "跑了但失败"）
    #     充分 = 拿着它就能选出下一步（给相近真名字、说清 load_tools 帮不帮得上忙）
    #
    # ⚠️ **第一行必须是人话**：`result_text[:80]` 会作为 `result_summary`
    #    显示在用户可见的工具卡片上（见本文件 `tool_end` 事件），
    #    诊断块只能放在第一行之后。

    class _ToolFailCause:
        UNKNOWN_TOOL = "UNKNOWN_TOOL"       # 名字不存在
        DISABLED_TOOL = "DISABLED_TOOL"     # Skill 存在但被禁用了
        BAD_PARAMS = "BAD_PARAMS"           # 工具存在，参数不合 schema
        TOOL_ERROR = "TOOL_ERROR"           # 工具跑了并自己报了错
        # ⭐ 工具存在、当前健康、当前 eligible —— 只是产生这次调用的那次
        #    API request **没把它的 schema 给模型**（模型凭更早一次请求的记忆调的）。
        # ⚠️ **刻意不复用上面四类**：塞进 UNKNOWN_TOOL 是撒谎（它明明存在）、
        #    塞进 DISABLED_TOOL 也是（它没被禁用）、塞进 TOOL_ERROR 更错（它压根没跑）。
        #    📌 给模型的失败信息必须**正确**且**足够让它选出下一步** ——
        #       而这里正确的下一步是 `load_tools`，与那四类的下一步都不同。
        TOOL_NOT_ACTIVE = "TOOL_NOT_ACTIVE" # 存在且可用，但本次 request 没附它的 schema

    def _known_tool_names(self) -> list[str]:
        """当前进程里**全部合法**的工具名。给"最接近的真名字"用。

        🔴 改造前这里手工拼**四个来源**（registry / `_tool_pool` / `_CORE_TOOL_NAMES`
           / MCP），注释还自陈「四个来源缺一不可」—— 那句话本身就是证据：
           **一份靠"记得把四个都写上"维持的名单，漏一个不会报错，只会让诊断变笨。**
        ⭐ 现在只有一个来源。

        ⚠️ 与改造前有**一处刻意的差异**，留痕：
           改造前拼的是 `registry.list_enabled_skills()`（Skill **实例**字典），
           而目录投影的是 `registry.get_all_manifests()`（**manifest** 列表）。
           两者在正常情况下一致，但那种「manifest 顶层没有 name、整条被丢弃」
           的 Skill 只会出现在前者里 —— 它**装载成功、UI 显示 READY，而模型
           从来看不见它**。把这种名字放进"你是不是想调这个"的提示里，
           等于劝模型去调一个它拿不到 schema 的东西。
           📌 **半可见的能力比不可见更坏。**
        """
        return sorted(n for n in self._get_tool_catalog().names() if n)

    def _describe_tool_failure(self, name: str, cause: str, tool_error: str = "") -> str:
        """产出给模型看的诊断块。**只在失败时调用**——成功时一个字都不加。

        为什么不把 1/2/3 三条排查步骤丢给模型自己走：
        **分发的那一刻代码就已经知道是哪一类了。** 让模型再推一遍，
        等于在"别猜"的地方又开了一次猜的口子。直接把结论递给它。
        """
        import difflib
        # 同一轮内重复撞同一个名字 → 升级措辞，防止它在一轮里连试三个变体
        seen = getattr(self, "_tool_failures_this_turn", None)
        if seen is None:
            seen = self._tool_failures_this_turn = {}
        key = f"{name}::{cause}"
        seen[key] = seen.get(key, 0) + 1
        repeated = seen[key] > 1

        lines = ["", "[Tool Call Diagnostics — internal, not shown to the user]"]

        if cause == self._ToolFailCause.UNKNOWN_TOOL:
            # ⚠️ 必须**忽略大小写**地匹配。`difflib` 直接比 "getdns" 和 "GetDNSList"
            # 相似度只有 0.5 出头，够不到任何合理阈值——而大小写写错恰恰是最常见的
            # 一种错法（一次就修过 21 处 manifest 大小写不一致）。
            # 只按原样匹配的话，最该被提示的那一类反而永远提示不出来。
            known = self._known_tool_names()
            lower_map = {n.lower(): n for n in known}
            exact_ci = lower_map.get(name.lower())
            near = [lower_map[m] for m in difflib.get_close_matches(
                name.lower(), list(lower_map), n=3, cutoff=0.6)]
            lines += [
                "- tool_error: none (the tool was never invoked, so it produced no error of its own)",
                f"- cause: UNKNOWN_TOOL — no tool, Skill, or MCP tool is named \"{name}\".",
            ]
            if exact_ci:
                lines.append(
                    f"- ⭐ The correct name is \"{exact_ci}\" — you only got the capitalization wrong. "
                    f"Tool names are case-sensitive. Retry with the exact spelling."
                )
            elif near:
                lines.append(f"- closest existing names: {', '.join(near)}")
            else:
                lines.append("- no existing name is close to it.")
            lines += [
                # ⚠️ 这条必须说清楚，否则模型会去调 load_tools 然后原地打转：
                # 延迟的 Skill/MCP 即使没 load 也能按名字直接执行（分发与 schema 注入解耦），
                # load_tools 只负责给参数 schema，**不决定工具能不能调**。
                "- load_tools will NOT help here: it only supplies parameter schemas for tools "
                "that already exist. It cannot make a nonexistent name callable.",
                "- Next: call one of the names above if it matches your intent, or tell the user "
                "plainly that this capability does not exist. Do not try another guessed name.",
            ]

        elif cause == self._ToolFailCause.DISABLED_TOOL:
            # ⚠️ Skill 与 MCP 的**下一步不同工具**，所以这句不能合并成一句泛的。
            #    📌 失败信息要同时【正确】且**足够让它选出下一步** ——
            #       而"下一步"在两边是两个不同的工具名。
            _mcp_srv = ""
            try:
                from core.mcp_client import MCPManager as _MM_n
                _mcp_srv = _MM_n.instance().disabled_tool_server(name)
            except Exception:
                _mcp_srv = ""
            if _mcp_srv:
                lines += [
                    "- tool_error: none (the MCP server is disabled, so nothing was invoked)",
                    f"- cause: DISABLED_TOOL — the MCP server \"{_mcp_srv}\" is disabled.",
                    "- The tool name is fine. Do not look for another name and do not "
                    "try to reach the same capability some other way first.",
                    "- Next: tell the user that this MCP server is turned off, and ask "
                    f"whether to enable it (manage_mcp can enable \"{_mcp_srv}\").",
                ]
            else:
                lines += [
                    "- tool_error: none (the Skill exists but is currently disabled, so it was not invoked)",
                    f"- cause: DISABLED_TOOL — the Skill \"{name}\" exists but is disabled.",
                    "- The name is correct. Do not look for another name and do not recreate it.",
                    "- Next: tell the user it is disabled and ask whether to enable it "
                    "(manage_existing_skill can enable it).",
                ]

        elif cause == self._ToolFailCause.BAD_PARAMS:
            lines += [
                f"- tool_error: {tool_error or '(none)'}",
                f"- cause: BAD_PARAMS — \"{name}\" exists; your arguments did not match its schema.",
                "- The name is correct. Do not switch to a different tool.",
                f"- Next: call load_tools(names=[\"{name}\"]) to get the exact parameter schema, "
                "then retry with corrected arguments. Do not guess parameter names.",
            ]

        else:  # TOOL_ERROR
            lines += [
                "- cause: TOOL_ERROR — the tool exists and ran. The error above is its own.",
                "- That message is authoritative. Do not reinterpret it or second-guess it.",
                "- Next: fix the inputs based on that message, or report it to the user. "
                "Do not switch to a different tool name because of this error.",
            ]

        if repeated:
            lines.append(
                f"- ⚠️ You already called \"{name}\" this turn and it failed the same way. "
                "Stop retrying. Explain to the user what is missing instead."
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 统一工具目录的接入点
    #
    # ⭐ 这三个方法是 11 个消费点的**共同前置**：目录本身、这一刻的运行时事实、
    #    以及"把两者合起来问"的入口。
    # 📌 **Catalog 只回答"谁处理 / 该不该出现"；"怎么运行"仍是 Runner 的事**
    #    （GUI 租约等待 / health 门控 / 工具卡事件 / 长任务交还 / 错误分类 /
    #     EXIT 路径的同轮拒绝与终端事件收尾，一律留在原处）。
    # ══════════════════════════════════════════════════════════════════════

    def _tool_runtime_view(self) -> "_ToolRuntimeView":
        """这一刻的运行时事实 —— `availability` 判据的唯一输入。

        ⚠️ 刻意做成**窄接口**而不是直接把 orchestrator 传进去：
           一个 predicate 能拿到整个 orchestrator，就迟早有人在里面写副作用。
        """
        return _ToolRuntimeView(self)

    def _get_tool_catalog(self) -> "ToolCatalog":
        """装配工具目录：内置声明（静态）+ Skill / MCP（每次重建）。

        ⚠️ 内置声明只构造一次并缓存 —— 它是静态的，而且构造期不变量
        （manifest 名一致 / awareness 必填 / bindings 非空 / binding 方法真实存在）
        每次重跑纯属浪费。**动态部分每次重建**：Skill 会热加载、MCP 会重连。
        """
        _cat = getattr(self, "_tool_catalog", None)
        if _cat is None:
            from core.tools import ToolCatalog as _TC
            from core.tools.builtin import build_builtin_definitions as _bbd
            _cat = _TC()
            for _d in _bbd(BUILTIN_MANIFESTS):
                _cat.add_builtin(_d)
            # ⭐ 启动期响亮失败：binding 用方法名字符串写，写错的话**运行时才炸**，
            #    而且表现为「模型调了工具、然后什么都没发生」。
            _cat.assert_bindings_exist(self)
            self._tool_catalog = _cat
        _cat.clear_dynamic()
        try:
            from core.tools import mcp_definitions, skill_definitions
            for _d in skill_definitions(self.registry):
                _cat.add_dynamic(_d)
            _mgr = getattr(self, "_mcp_manager", None)
            if _mgr is not None:
                for _d in mcp_definitions(_mgr):
                    _cat.add_dynamic(_d)
        except Exception as _e:
            logger.warning(f"[F4] 动态工具投影失败（内置仍可用）：{_e}")
        # ⚠️ 被隔离的动态工具要**响亮**（但不致命）—— 一条撞名的用户 Skill /
        #    外部服务不该把整个 Nano 打死，但也绝不能静默消失。
        for _r in _cat.rejected():
            logger.error(f"[F4] 工具被隔离：{_r.name}（{_r.origin.value}）—— {_r.reason}")
        return _cat

    # ══════════════════════════════════════════════════════════════════════
    # 工具 handler —— 从 `_execute_one_tool_call` 的 name 分派链
    #             **机械提取**出来的分支体。
    #
    # ⚠️ **这是纯重构，不是 shadow**：旧的 if/elif 仍然调用它们，行为零变化。
    #    目的是让最终切换那一次只需要把
    #        `if name == A: ... elif name == B: ...`
    #    换成
    #        `handler = catalog.resolve(name, scope); await handler(...)`
    #    而**不必同时重写执行逻辑**。
    #
    # ⭐ 分层（外部评审的核心建议，已核实）：
    #    **Catalog 决定"谁处理"，Flow Runner 决定"怎么运行这一类 handler"。**
    #    所以 handler **只负责产出 result_text**；下面这些统一 plumbing
    #    一律留在 `_execute_one_tool_call` 里，不许被 handler 吞掉：
    #      GUI 租约等待 / health fail-fast / tool_start·tool_end 事件 /
    #      长任务交还 / 错误分类 / 挂起期日志 / ToolExecution 组装。
    #
    # 📌 统一签名 `(args, aid, *, event_queue) -> str`：
    #    AST 实测（提取前）确认 —— 分派链**之后**的代码只读 `result_text` 一个变量，
    #    所以"返回 result_text"就是这批 handler 的完整契约。
    # ══════════════════════════════════════════════════════════════════════

    def _ui_sink(self, event_queue):
        """这次 UI 往返该发到哪条通道。

        🔴🔴 **main agent 和 Subagent 的通道寿命不一样**（实测 2026-08-20 卡死）：
           轮内的 `event_queue` 由 `navigate_pipeline` 的 `async for` 消费，
           **那个循环随本轮结束而退出**。Subagent detach 之后活得比它长 ——
           于是Subagent发的 `os_action_confirm` 落进一个没人读的队列，
           弹窗永远不出现，Subagent在确认闸上干等 300 秒，屏幕上什么都没有。
        📌 **一个跨过了自己那一轮的执行者，不能再用那一轮的通道去要 UI。**
        ⚠️ 这个洞只在**需要 UI 往返**的事件上存在：Subagent此前全是只读工具，
           一次往返都不需要 —— 给它写权限的那一刻才暴露。
           📌 一条只在某种能力出现后才会被走到的路，它的缺陷会**和那个能力
              同一天诞生**，而不是在它被写下的那天。
        ⚠️ 取不到轮外通道时**退回轮内**（fail-safe：最坏退化成今天的行为，
           而不是把授权请求整个丢掉）。
        """
        try:
            if current_agent_label():
                _oob = getattr(self, "_ui_oob_events", None)
                if _oob is not None:
                    return _oob
                logger.error("[OOB] Subagent要弹窗，但轮外通道不存在 —— "
                             "退回轮内通道（本轮结束后它就没人读了）")
        except Exception:
            pass
        return event_queue

    async def _handle_load_tools(self, args: dict, aid: str, *,
                                 event_queue, **_ctx) -> str:
        # 按需加载工具：匹配全量池 → 暂存，由 ReAct 循环在本批结束后 append 进 tools_manifest
        # ⑤ 匹配问目录 —— 改造前是一张**人工维护的关键词分组表**（`_GROUPS`：
        # "网页/浏览/browser…" → `nm.startswith("browser_")` 之类），加一个新工具
        # 就要记得往表里塞一条，漏了的表现是「load_tools 搜不到它」。
        # ⭐ 现在检索文档由声明**自动**构建（name + awareness + description +
        #    所有 property 名 + **所有 enum 值**），于是
        #    `load_tools(query="file_delete")` 天然精准命中 `os_execute` ——
        #    因为 39 个 action 的 enum 全在它的检索文档里，
        #    **而 awareness 行一个 action 都不用列**。
        # ⚠️ 只在本作用域此刻 eligible 的集合里搜（HIDDEN 的搜不到）——
        #    搜出一个当前不该出现的工具，等于把 eligible 那条不变量从后门破掉。
        _matched = [
            d.manifest for d in self._get_tool_catalog().search(
                args.get("query", ""), args.get("names") or [],
                scope=ToolScope.MAIN, runtime=self._tool_runtime_view())
        ]
        # ⭐⭐⭐ [2026-08-23 实测回归] **必绑定的工具，加载也要绑定。**
        #
        # 🔴 实测症状：模型 `load_tools(computer_use)` 之后，要缩小窗口时填了
        #    `computer_use(action='set_window_mode')` → 「unknown action …
        #    actions must come from the closed enum and cannot be invented」。
        #
        # ⭐ 它不是在乱编：`computer_use` 的描述里明写着「先 call
        #    set_window_mode('mini')」，而 `set_window_mode` 是 DEFERRED、
        #    **这一轮根本没被加载**。手上没有那个工具，它只好拿手上有的那个凑。
        #
        # 📌 **我们声明了「必绑定」，却把绑定的两个放在了不同的加载状态** ——
        #    这正是这次拆分要消除的那个问题（「一组必绑定的东西被放进两个不同的
        #    可见性层级」），而在修它的同一轮里又造了一次。
        # 📌 **一个绑定关系，该由系统保证，不该靠模型记得一起加载。**
        #    靠模型记得 = 又一条「要求每个调用方自觉」的机制，而它漏的时候不报错，
        #    只会让模型编一个不存在的 action。
        #
        # ⚠️ 只补**真正的强绑定**的：`computer_use` 一旦上场，Nano 自己的窗口
        #    必然挡在它要操作的那块屏幕前面 —— 这是**系统事实**，不是偏好。
        #    ⚠️ `look_at_screen` **不在这里**：它和 computer_use 是「常一起出现」，
        #       不是「缺了就干不成」。📌 把「经常一起」当成「必须一起」，
        #       会让按需加载退化成打包加载。
        # ⚠️ 2026-08-23 机械扫了一遍**全部工具描述**（谁提到了谁 + 双方 preload），
        #    发现同形的一共三处 —— 修掉的那处只是其中之一，而
        #    `look_at_screen → set_window_mode` **比它更老**：
        #    📌 **这不是拆分引入的新问题，是拆分让一个既有的坑更容易被踩到。**
        #    ⭐ 筛选判据：那句话是「你**现在就得**去调它」还是「以后可能用到它」——
        #       前者才是真风险（模型此刻要用、手上没有 → 它会编一个）。
        #       像 `os_execute → computer_use`（"load it when you need it"）
        #       就不算，那句话本身就是在叫它去加载。
        _BOUND_WITH = {
            # 「FIRST … This one is not optional」——GUI 操作前必须缩小自己
            "computer_use": ("set_window_mode",),
            # 「FIRST shrink yourself … BEFORE your first look_at_screen」
            "look_at_screen": ("set_window_mode",),
            # 成对：没有表就没有步骤可更新
            "update_task_step": ("create_task_list",),
        }
        _have = {m.get("name") for m in _matched}
        _extra_names = [n for src in _have for n in _BOUND_WITH.get(src, ())
                        if n not in _have]
        if _extra_names:
            _bound = [d.manifest for d in self._get_tool_catalog().search(
                "", _extra_names, scope=ToolScope.MAIN,
                runtime=self._tool_runtime_view())]
            if _bound:
                logger.info(f"[F4] 连带加载 {[m.get('name') for m in _bound]}"
                            f"（被 {sorted(_have & set(_BOUND_WITH))} 强绑定）")
                _matched = _matched + _bound
        if not hasattr(self, "_pending_loaded_manifests"):
            self._pending_loaded_manifests = []
        self._pending_loaded_manifests.extend(_matched)
        if _matched:
            return (
                # ⚠️ 旧文案是 "They can now be called directly."，**没有作用域限定**。
                # 两个方向都假：
                #   ① 不跨轮 —— 每轮开头都会重建 manifest（实测日志：某轮 10 个工具，
                #      下一轮就回到 6 个），但这句话会永久留在对话历史里；
                #   ② 不跨子作用域 —— 主循环加载的工具不进 Skill Explorer 的 manifest。
                # 而它作为普通 tool_result 会写进 MemoryManager，于是一句瞬时状态
                # 变成了模型可以永久引用的"事实"。Explorer 死路事故就是
                # 模型据此再次调用了当前作用域里根本不存在的 create_new_skill。
                #
                # 修在源头比事后清洗历史便宜得多：改这一句，所有作用域一起受益。
                # 见本文件里「运行作用域与事实生命周期」那段说明。
                "Loaded capabilities: " + ", ".join(m.get("name", "") for m in _matched)
                + ". This applies to the current step only. What you can actually call is "
                  "always defined by the tools attached to the current request — not by this "
                  "message. Do not treat this as evidence that these tools are available in a "
                  "later turn or inside a specialized sub-flow. Call the needed tool in the next step."
            )
        return (
            "No matching capability was found. Use names to specify exact tool names, "
            "or describe the task differently and try load_tools again."
        )

    # ══════════════════════════════════════════════════════════════════════
    # exit-flow 薄适配器
    #
    # ⭐ 它们存在的唯一理由：**让 EXIT runner 里不再有一张按工具名分派的表。**
    #    改造前那里是 `if exit_call.name == "update_existing_skill": … elif …`，
    #    而那正是这次要删的第二份权威（那条 AST 断言要管的就是它）。
    #
    # 📌 分层没变，仍然是那条：**Catalog 决定"谁处理"，Runner 决定
    #    "怎么运行这一类 handler"。** 所以这五个适配器里**只有各自那一支
    #    原本就在做的事**（构造 `AgentDecision` / 补 requirement / supersede 澄清 /
    #    排空队列），而公共 plumbing —— 同轮拒绝、`tool_start`·`tool_end`、
    #    终端事件前收尾、`exit_flow_defer_to_model` 拦截 —— **一行都没搬**，
    #    仍然长在 runner 里。
    #
    # ⚠️ 统一签名 `(exit_call, decision, *, used_model, base_guide,
    #    realtime_callback, event_queue) -> AsyncIterator[dict]`。
    #    参数**取全集**：每个适配器只用它自己需要的那几个，用不上的不用管 ——
    #    这比让 runner 去猜"这个 handler 要哪几个参数"可靠得多
    #    （猜的那条路只有两种实现：按名字特判，或者反射签名，
    #     前者是我们正在删的东西，后者会把"参数写错"从启动期错误变成运行期错误）。
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _drain_event_queue(event_queue) -> list:
        """把队列里攒着的事件取干净，交给调用处 yield 出去。

        ⭐ 为什么 exit 路径需要它：`event_queue` 的常规排空循环长在**工具批次
        那一段**里，exit 工具在到达那段之前就路由走了。于是这条路径上**任何**
        `event_queue.put` 都是只进不出 —— 不只是工具卡片，`realtime_callback`
        发的 `thinking` 状态事件也一样卡在里面（表现为探索阶段状态行不更新）。

        非阻塞：只取已经在队列里的，不等新的，绝不拖慢路由。
        """
        out = []
        while not event_queue.empty():
            try:
                out.append(event_queue.get_nowait())
            except Exception:
                break
        return out

    def _exit_decision_from(self, exit_call, decision) -> "AgentDecision":
        """把 exit 的 `ToolCall` 还原成 `AgentDecision`（三个适配器都要）。"""
        return AgentDecision(
            "call", name=exit_call.name, args=exit_call.args,
            tool_use_id=exit_call.tool_use_id,
            thinking_blocks=decision.thinking_blocks,
        )

    async def _execute_one_tool_call(
        self,
        call: ToolCall,
        *,
        used_model: str,
        base_guide: str,
        system_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
        active_tool_names: frozenset | None = None,
    ) -> ToolExecution:
        """执行单个工具调用，把 tool_start/tool_end 事件推进 event_queue。

        不负责写 memory（由 ReAct 主循环统一批量写入）。
        """
        self._ensure_react_sems()
        name = call.name
        args = call.args or {}
        aid = f"react_{call.index}_{time.time_ns()}"
        raw_result = None
        tool_data = None
        # 目录 + 这一刻的运行时事实。工具卡文案、handler 解析都问它。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()

        # ⭐⭐⭐ **在 GUI 模式里，任何工具调用之前都先看机器归谁。**
        #
        # 「只要 mini 窗口存在，不管是 nano 任何行为 ——
        # 命令行、mcp、键鼠模拟、skill 等等，全部受被动挂起的监控，
        # **不管 nano 目前在做什么，只要用户进行操作，一律挂起**。」
        #
        # ⚠️ 第一版只把**感知**换成了全量，**等待**还留在 `os_execute` 里、
        #    而且还额外要求是键鼠动作 —— 用户当场看出来只做了一半。
        #    📌 **感知的范围和反应的范围必须一致**，否则用户看到接管状态条却发现
        #    Nano 照样在动，那正是一开始诊断出的那个"UI 语义不一致"。
        #
        # ⭐ 顺带更正之前一个判断：曾经认为"截图不该被拦，因为恢复后要判断环境
        #    变没变恰恰需要能看"。但 `_look_at_screen` **会最小化/还原自己的窗口**
        #    来避开遮挡 —— 那是**抢焦点**，它确实在跟用户争。所以它也该等。
        #
        # ⚠️ 等待期间零 LLM、零 token（纯 `asyncio.sleep`），只占一个挂着的 turn。
        # ⚠️ 不在 GUI 模式时**完全不查**（一次数据库读都不多花）——
        #    纯命令行 / 纯对话的一轮完全不受影响。
        _gui_waited = 0.0
        try:
            from core.runtime import oslease as _ol_gw
            from core.runtime.kernel import get_kernel as _gk_gw
            _k_gw = _gk_gw()
            if _ol_gw.gui_session_active(_k_gw):
                _may_gw, _why_gw = _ol_gw.nano_may_touch_os(_k_gw)
                if not _may_gw:
                    logger.info(f"[A3] GUI 模式中，机器不归 Nano（{_why_gw}）—— "
                                f"工具 {name!r} 前进入等待（**不结束 turn**）")
                    _h_gw, _gui_waited = await _ol_gw.await_activity_lease(
                        f"before tool {name}")
                    # ⚠️ 只有键鼠动作才真的需要**持有**租约；其余工具等到"没人占着"
                    #    就够了，拿了反而会让 canary 误以为 Nano 在开车。
                    #    📌 「等到可以动」和「持有驾驶权」是两件事。
                    if _h_gw is not None:
                        try:
                            from core.os_layer import dsl as _dsl_gw
                            if name == "os_execute" and _dsl_gw.contends_for_machine(
                                    (args or {}).get("action") or ""):
                                self._rt_os_lease = _h_gw
                            else:
                                _ol_gw.release_activity(_h_gw)
                        except Exception:
                            _ol_gw.release_activity(_h_gw)
                    logger.info(f"[A3] 等了 {_gui_waited:.0f}s，继续执行 {name!r}")
                    # ⭐⭐ 取出这段挂起期的证据日志。
                    #
                    # ⚠️⚠️ **必须在这里取，不能只在 `os_execute` 那一支取** ——
                    #    等待本身是**全量**的（命令行 / MCP / skill / 截图 / 键鼠都会等），
                    #    只在键鼠那一支消费的话，"用户接管期间发生了什么"这条信息
                    #    就取决于 Nano 恢复后**碰巧先调哪个工具**。
                    #    📌 **一份一次性的证据，取它的地方必须和产生它的地方同一个作用域。**
                    #
                    # ⚠️ 日志是**一次性**的：`finish()` 取完即清。所以这里取到就必须
                    #    真的送到模型面前（见函数尾部那段），不能取了不用 —— 那等于丢证据。
                    try:
                        from core.proactive import takeover_log as _tl_gw
                        _sess_gw = _tl_gw.finish()
                        _sum_gw = _tl_gw.summarize(
                            _sess_gw, _tl_gw.sensors_healthy(_sess_gw))
                        if _sum_gw:
                            self._a3_pause_note = _sum_gw
                            logger.info("[A3] 挂起期证据日志已生成，"
                                        f"{len(_sum_gw)} 字符 → 随本次工具结果交给模型")
                    except Exception as _e_tl:
                        logger.debug(f"[A3] 挂起期日志生成失败（不阻断）: {_e_tl}")
        except Exception as _e_gw:
            logger.debug(f"[A3] GUI 模式等待检查失败（放行）: {_e_gw}")

        # ── 能力门控·第二道：执行层 fail-fast ────────────────────────────
        # manifest 门控（第一道）能挡住绝大多数，但挡不住这三种：模型沿用上一轮已加载
        # 的旧 schema、load_tools 动态拉进来的、以及故障恰好发生在本轮 manifest 构建
        # 之后。所以真正动手前再查一次——宁可返回一个结构化的"不可用"，也不让它进到
        # RAG 函数里摔出原生异常。
        try:
            from core.health import get_health
            _blk = get_health().tool_block_reason(name)
        except Exception:
            _blk = None
        if _blk is not None:
            _rt = (
                f"Tool \"{name}\" is currently unavailable: {_blk.user_message} "
                + (f"Recovery hint: {_blk.recovery_hint} " if _blk.recovery_hint else "")
                + "Do not retry this tool. Tell the user plainly what is broken, and continue with "
                  "whatever you can still do."
            )
            logger.warning(f"[Health] 执行层拦截工具【{name}】：{_blk.code}")
            await event_queue.put({
                "event": "tool_start", "action_id": aid,
                "tool_use_id": getattr(call, "tool_use_id", "") or "",
                "action_display": _cat.presentation(name, args),
                "action_input": str(args)[:200],
                "log": f"能力不可用，已拦截【{name}】",
                "status": "TOOL_EXECUTING", "model": used_model, "current_skill": name,
            })
            await event_queue.put({
                "event": "tool_end", "action_id": aid,
                "result_summary": f"能力不可用：{_blk.user_message}"[:80],
                "status": "SYS_IDLE", "model": used_model,
                "current_skill": name, "tool_use_id": call.tool_use_id, "ok": False,
            })
            return ToolExecution(call=call, result_text=_rt, ok=False,
                                 error=f"capability unavailable: {_blk.code}")

        # ══════════════════════════════════════════════════════════════════
        # ⭐⭐⭐ 本次 request 的 tool contract 闸门
        # ══════════════════════════════════════════════════════════════════
        #
        # ═══ 它堵的洞（实测实证 2026-08-13）═══
        #
        #     07:55:39  [TOKEN-PLAN] core_tools=6（不含 create_new_skill，本轮也没 load_tools）
        #     07:55:49  [Router] create_new_skill 触发（requirement='部署这个吧'）  ← 照样执行了
        #
        # 用户对着一张待审卡说「部署这个吧」，模型**凭上一轮的 schema 记忆**调了
        # `create_new_skill`，而 `answer_open_interaction` 就在它手边。
        # ⭐ 这正是 要修的问题本身：**模型把"我曾经能调"当成"我现在能调"。**
        #
        # ═══ ⚠️ 这一条【推翻】了工具目录里的一条既有规定，不是 bug 修复 ═══
        #
        # 原规定（`catalog.py` 模块头 + `t_f4_catalog.py` 模块头两处）：
        #     「deferred 工具『schema 当前没附带但仍可执行』是**正常状态**」
        # **现在改成：主模型的工具调用必须受【产生该调用的那次 API request 的
        # tool contract】约束。** 读到 `catalog.py` 那条的人请看这里，
        # 不要以为 Catalog 被改坏了。
        #
        # ⚠️ 而这不是给正常路径加一轮：`DEFERRED_HEADER` 本来就白纸黑字写着
        #    「call load_tools first … then call the loaded tool **on the next step**」。
        #    所以这是**关掉一条与自己声明矛盾的旁路**。
        #
        # ═══ 五格顺序（少一格就会抢掉别处已经正确的诊断）═══
        #
        #   1. health blocked          → 上面那段（已 return）。已经正确，本闸不许抢
        #   2. Preload.HIDDEN          → 放行到既有内部语义，**绝不能建议 load_tools**
        #                                （HIDDEN 的定义就是"处理得了但刻意不给正式通路"，
        #                                  而 `load_tools` 本来就搜不到它）
        #   3. availability=False      → 放行到 handler，由它说「你要处理的那个对象没了」
        #                                （`eligible ⊆ resolvable` 单向包含正为它设计，
        #                                  失败信息必须**正确**，"不存在"是假话）
        #   4. eligible+DEFERRED+未附  → **本闸拦在这里** → TOOL_NOT_ACTIVE
        #   5. 名字不存在              → 下游既有的 UNKNOWN_TOOL 诊断（含最接近的真名字）
        #
        # ⚠️ **fail-open 是刻意的**：拿不到 `active_tool_names`（老路径、测试替身、
        #    子流程）时**不校验**。📌 一道新加的闸，在信息不全时应当放行 ——
        #    否则它会在自己还没接全的地方制造假故障。
        _active = active_tool_names
        if _active:
            _d6 = _cat.get(name)
            if (_d6 is not None                                  # 存在（否则交给 ⑤）
                    and _d6.preload is Preload.DEFERRED           # 排除 CORE / HIDDEN（②）
                    and _d6.is_eligible(ToolScope.MAIN, _rtv)     # 排除 availability=False（③）
                    and name not in _active):                     # 本次 request 没附它
                logger.warning(
                    f"[F6] 工具【{name}】不在本次 request 的工具表里 → 拒绝执行"
                    f"（本次 attach 了 {len(_active)} 个）"
                )
                _rt6 = (
                    f"Tool \"{name}\" was not executed because it was not active in the "
                    f"tool set attached to this model request.\n"
                    f"[Tool Call Diagnostics]\n"
                    f"- cause: {self._ToolFailCause.TOOL_NOT_ACTIVE} — the tool exists and is "
                    f"currently available, but its schema was not attached to this request.\n"
                    f"- This call came from stale knowledge of an earlier request's tool set, "
                    f"not from the tool contract of the request you are answering now.\n"
                    f"- Next: call load_tools(names=[\"{name}\"]) and then call it on the "
                    f"following step. Loading takes effect from the next step, not this one."
                )
                await event_queue.put({
                    "event": "tool_start", "action_id": aid,
                    "tool_use_id": getattr(call, "tool_use_id", "") or "",
                    "action_display": _cat.presentation(name, args),
                    "action_input": str(args)[:200],
                    "log": f"工具未在本轮工具表中，已拦截【{name}】",
                    "status": "TOOL_EXECUTING", "model": used_model, "current_skill": name,
                })
                await event_queue.put({
                    "event": "tool_end", "action_id": aid,
                    "result_summary": f"未加载：{name}"[:80],
                    "status": "SYS_IDLE", "model": used_model,
                    "current_skill": name, "tool_use_id": call.tool_use_id, "ok": False,
                })
                return ToolExecution(call=call, result_text=_rt6, ok=False,
                                     error=f"tool not active in this request: {name}")

        await event_queue.put({
            "event": "tool_start",
            "action_id": aid,
            # ⭐ 详情按 `tool_use_id` 回账本取（参数与结果都在那边）。
            #    ⚠️ **刻意不带 args** —— 那会让参数有两个来源（live 走事件 /
            #    重放走账本），而两个来源只在"我两次想法相同"时一致。
            "tool_use_id": getattr(call, "tool_use_id", "") or "",
            "action_display": _cat.presentation(name, args),
            "action_input": str(args)[:200],
            "log": f"ReAct: 执行工具【{name}】",
            "status": "TOOL_EXECUTING",
            "model": used_model,
            "current_skill": name,
            "tool_use_id": call.tool_use_id,
        })

        # 统一失败标记。⚠️ 原注释写「内置工具不会设它」——**那句已过期**：
        # 实际有 5 个内置工具会设（set_next_checkin / task_boundary / cancel_wait /
        # wait_for / os_execute），它们的 handler 返回 `ToolOutcome(text, failed)`。
        # 📌 这正是 要修的问题的一个小样本：**注释描述的能力与实现不一致，而且不报错。**
        _is_failed = False
        try:
            # ── 内置伪工具 ───────────────────────────────────────────────
            # 分支体已机械提取成 `_handle_*`（纯重构，行为零变化）。
            # cutover 时这一串 if/elif 会被 `catalog.resolve(name, scope)` 取代。
            # ── 内置工具：**问目录谁来处理** ─────────────────────────────
            #
            # 🔴 改造前这里是 **19 段 `elif name == "..."`** —— 一份按工具名建立的
            #    第二权威。分支体已机械提取成 `_handle_*`（纯重构、行为
            #    零变化），这一步只把「谁处理」这个问题交还给唯一权威。
            #
            # ⚠️ 统一契约是**实测**出来的、不是设计出来的：提取前用 AST 量过，
            #    分派链**之后**只读 `result_text` 一个变量 —— 所以 handler 的完整
            #    契约就是「产出文本 + 这次算不算失败」，`str | ToolOutcome` 两种写法
            #    由 `ToolOutcome.of()` 归一（同 `SkillResult.from_raw` 的既有范式：
            #    **一个便利构造器，不是两种并存的契约**）。
            #
            # ⚠️ 参数**取全集**：`call` / `used_model` / `gui_waited` 只有 `os_execute`
            #    用得上，其余 handler 由 `**_ctx` 收着。为什么不"按需传"——
            #    按需传只有两种实现：按名字特判（正是这次要删的东西），
            #    或反射签名（把"参数写错"从启动期错误变成运行期错误）。
            #
            # ⚠️ **只有内置走这条路**。本地 Skill 与 MCP 的执行体仍是下面两段内联
            #    分支：它们除了 `result_text` 还要回填 `raw_result` / `tool_data`
            #    （Skill 的 `dsl_plan` 转路由就靠它），契约比这批 handler 宽 ——
            #    那次机械提取当时也正是按这条线划的（19/19 = 全部内置）。
            #    📌 目录对这件事**如实描述**：它们的 binding 指向
            #       `_execute_one_tool_call` 本身，因为"谁处理"现在确实是这个函数
            #       （同探索作用域的 binding 指向 `_run_skill_exploration` 那个大循环
            #        —— **不为了好看虚构一层**）。
            _def = _cat.get(name)
            _handler_ref = _cat.resolve(name, ToolScope.MAIN, _rtv)
            if (_def is not None and _def.origin is ToolOrigin.BUILTIN
                    and _handler_ref is not None):
                _out = await getattr(self, _handler_ref)(
                    args, aid, event_queue=event_queue,
                    call=call, used_model=used_model, gui_waited=_gui_waited)
                if isinstance(_out, ToolExecution):
                    # `os_execute` 独有的两处提前返回（机器被占 / 长命令交还）：
                    # 语义是「这次调用已经自己完成了全部收尾」，所以直接 return。
                    return _out
                _outcome = ToolOutcome.of(_out)
                result_text, _is_failed = _outcome.text, _outcome.failed

            elif self._is_mcp_tool(name):
                # ── MCP 工具执行（Nano 自身能力，内联分发，不进 registry）──
                # 照 os_execute/render_visual/wait_for 范式：懒加载 manager、调用、回填
                # result_text。tool_start/tool_end 卡片由本函数头尾自动发，无需手动加。
                _mcp_mgr = self._mcp_manager
                # 在场确认：仅当 server 明确标 destructive（不可逆）时弹确认卡，复用 execution_confirm。
                # 普通读/写不拦——透明度由工具卡片保证（对齐"可逆直接做、不可逆先确认"）。
                _mcp_confirm = _mcp_mgr.confirm_summary(name)
                if _mcp_confirm:
                    _confirm_ev = asyncio.Event()
                    _cancelled = [False]
                    _loop = asyncio.get_running_loop()

                    def _on_confirm():
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    def _on_cancel():
                        _cancelled[0] = True
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    await event_queue.put({
                        "event": "execution_confirm",
                        "skill_name": name,
                        "side_effects": [_mcp_confirm],
                        "on_confirm": _on_confirm,
                        "on_cancel": _on_cancel,
                    })
                    from core.runtime import inbox as _ib6
                    _oc6 = await _ib6.wait_confirm_or_user_message(_confirm_ev, 300)
                    if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                        _cancelled[0] = True
                    if _cancelled[0]:
                        # ⚠️ 两种「没执行」要分开说：**用户改口**和**干等超时**
                        #    在结果上一样，但要告诉模型的话完全不同。
                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                            result_text = (
                                f"External capability \"{name}\" was NOT executed.\n"
                                + _ib6.cancelled_by_user_message_note())
                        else:
                            result_text = (f"External capability \"{name}\" was "
                                           f"cancelled by the user.")
                        await event_queue.put({
                            "event": "tool_end", "action_id": aid,
                            "result_summary": result_text[:80], "status": "SYS_IDLE",
                            "model": used_model, "current_skill": name, "ok": False,
                        })
                        return ToolExecution(call=call, result_text=result_text, ok=False,
                                             error="user cancelled execution")

                # ── 自动后台化（MCP 就是 Nano 的一部分，长调用不特殊对待）──────────
                # 先发起调用，race 一个阈值。阈值内回 = 同步快路径（绝大多数 MCP 是
                # 请求/响应）；超阈值 = 系统【自动】转后台（不需要模型判断哪个工具是
                # "长任务"）：登记 background 挂起 + 把还在跑的调用交给 _start_bg_task
                # 生产端 + 出等待 pill，让模型说句"我去跑着"结束本轮，跑完
                # notify_background_done 自动唤醒续做。复用整套背景唤醒桥。
                # ⭐⭐⭐ **ref 必须在发起调用之前就定下来。**
                #    🔴 上一版是在「超阈值」那条分支里才算 `_bg_ref` ——
                #       那已经是 90 秒之后，而进度订阅必须在**请求发出时**
                #       就带上 `progressToken`，事后补不上。
                #    📌 **一个「出问题时才需要的标识」，如果它同时是
                #       「观测的订阅凭据」，就必须在事情开始时就存在** ——
                #       等到出问题才生成，前 90 秒的进度就永远丢了。
                _bg_ref = f"long_{call.tool_use_id or aid}"
                _bg_display = _cat.presentation(name, args)
                _mcp_task = asyncio.create_task(_mcp_mgr.call(
                    name, args, timeout=self._HANDED_BACK_DEADLINE_SEC,
                    progress_ref=_bg_ref))
                # ⭐ 等它跑完**或者等到用户又说话** —— 见 `_wait_or_user_speaks`。
                _mcp_finished = await self._wait_or_user_speaks(
                    _mcp_task, self._LONG_TASK_HANDBACK_SEC)
                _mcp_done = {_mcp_task} if _mcp_finished else set()
                self._session_log_append(f"[MCP call] {name}")
                self._last_called_skill = name

                if _mcp_task in _mcp_done:
                    # 快路径：阈值内返回 → 这条 ref 再没人看，丢掉轨迹。
                    # ⚠️ 不丢的后果不是崩，是**总线上慢慢堆满已完成的调用** ——
                    #    `_MAX_REFS` 那个兜底会开始丢掉真正活着的那些。
                    #    📌 一个兜底上限的存在，不免除正常路径显式收尾的义务。
                    try:
                        from core.runtime import progress as _pb1
                        _pb1.forget(_bg_ref)
                    except Exception:
                        pass
                    _mcp_text, _mcp_err, _mcp_needs_auth, _mcp_server = _mcp_task.result()
                    result_text = _mcp_text
                    _is_failed = bool(_mcp_err)
                    if _mcp_needs_auth:
                        # 在场授权（OAuth 延后授权）：server 需登录。发事件让 UI 渲染授权卡。
                        await event_queue.put({
                            "event": "mcp_auth_required",
                            "server": _mcp_server,
                            "tool": name,
                        })
                        result_text = (
                            f"External service \"{_mcp_server}\" requires login authorization before use. "
                            "Tell the user in one sentence that authorization is required before this capability can be used. "
                            "They can log in from MCP connections in settings."
                        )
                    if _is_failed:
                        self._last_skill_error = {
                            "skill": name,
                            "error": f"Tool \"{name}\" failed: {result_text}",
                            "consumed": False,
                        }
                else:
                    # ⭐ 慢路径：超阈值 → **交回控制权**（不是「转后台」）。
                    #    公共合同见 `_hand_back_long_task` —— MCP 在这里
                    #    **没有任何特殊之处**，它只是恰好是第一个用上它的载体。
                    result_text = await self._hand_back_long_task(
                        display=_bg_display, bg_ref=_bg_ref,
                        action_id=aid, event_queue=event_queue)

                    # ⭐⭐⭐ **形状转换放在知道形状的这一端。**
                    #
                    # 🔴 上一版直接把 `_mcp_task` 塞进事件，而 app 侧的消费端写的是
                    #    `_txt, _err, _na, _srv = await _t` —— **写死了 MCP 的四元组**。
                    #    于是长命令接进来那一步（它的载体返回一个字符串）
                    #    在完成的那一刻会被解成
                    #    `后台执行失败：too many values to unpack (expected 4)`。
                    #    ⚠️ 而当时的注释写的是「事件名保留 `mcp_background_request`
                    #       —— 改它要同时动 app 侧」——
                    #       📌 **一个「保留旧名字」的决定，必须同时检查那个事件的
                    #          消费端还假设着什么。名字兼容 ≠ 形状兼容。**
                    #
                    # ⭐ 修法：每个载体自己把结果包成**给模型看的文本**，
                    #    消费端只 `await` 出一个字符串。
                    #    📌 **形状转换要发生在知道形状的那一端**，
                    #       不是让公共消费端认识每一种载体。
                    async def _await_mcp(_t=_mcp_task, _n=name, _r=_bg_ref):
                        try:
                            _txt, _err, _na, _srv = await _t
                        except Exception as _e:
                            return (f"External capability \"{_n}\" failed after it was "
                                    f"handed back: {type(_e).__name__}: {_e}")
                        finally:
                            try:
                                from core.runtime import progress as _pb2
                                _pb2.forget(_r)
                            except Exception:
                                pass
                        if _err:
                            return (f"External capability \"{_n}\" finished with an "
                                    f"error:\n{_txt}")
                        return f"External capability \"{_n}\" finished:\n{_txt}"

                    await event_queue.put({
                        "event": "long_task_handback",
                        "task": asyncio.ensure_future(_await_mcp()),
                        "bg_task_ref": _bg_ref,
                        "display": _bg_display,
                    })
            else:
                # 兜底：模型有时把 os_execute 的 action 名（file_write/click/run_command…）
                # 当成独立工具直接调 → registry 里没有 → 会死 fail。这里识别并引导回正道。
                #
                # 🔴🔴 改造前这里是**手抄的 29 个 action**，而 `dsl.py` 实际有 **39 个**
                #    —— 漏了 10 个，且专挑高频项漏：`file_delete` / `file_move` /
                #    `write_registry` / `network_config` / `manage_service` /
                #    `modify_startup` / `schedule_task` / `set_env_var` /
                #    `read_screen_region` / `request_user_choice`。
                #    后果具体：模型把 `file_delete` 当成独立工具调时，兜底**不会**告诉它
                #    "这是 os_execute 的一个 action"，只给一句泛泛的"工具不存在"。
                # 📌 **一份手抄的清单，它的过期是静默的，而且专挑高频项漏。**
                #
                # ⚠️⚠️ **权威取 `dsl.ALL_ACTION_NAMES`，不取 `os_execute` manifest 的
                #    `action.enum`** —— 早先写的是后者，但回代码一核，那个 enum
                #    **本身也是手抄的、也过期了**：它只有 36 个，缺 `move` /
                #    `read_screen_region` / `request_user_choice`，而且 `move` 恰好
                #    是旧 `_OS_ACTIONS` 有、enum 没有的那一个 —— 照早先的设计做会
                #    **让这条兜底反而少认一个 action**。
                #    📌 判据就是 自己那条：**派生要派生到原生权威为止**。
                #       `_ACTIONS` 表在 `dsl.py`，它是执行器真正查的那张表；
                #       manifest 的 enum 只是它的又一份手抄件 ——
                #       **从一份手抄件派生，得到的还是手抄件。**
                #    （enum 少的那 3 个是另一个独立问题：模型 schema 层根本填不了
                #      它们。已单独留痕，不在本次"只切权威"的范围内。）
                try:
                    from core.os_layer import dsl as _dsl_fb
                    _os_actions = _dsl_fb.ALL_ACTION_NAMES
                except Exception:
                    _os_actions = frozenset()
                if name in _os_actions:
                    # ⭐⭐⭐ [2026-08-23 拆分] **必须按归属指路，不能写死 `os_execute`。**
                    #
                    # 🔴 这是整次拆分里**最危险的一处**：它是一条**纠错指引** ——
                    #    模型把 action 当独立工具调时，靠它回到正道。
                    #    拆分后 `click` 属于 `computer_use`，而这句话若仍写死
                    #    `os_execute`，就会把模型**精确地导向错误的工具**，
                    #    然后它拿到「没有这个 action」，再猜一次。
                    # 📌 **一条指错方向的纠错提示，比没有这条提示更坏** ——
                    #    没有时模型会重新想，有错的时它会照着走。
                    # ⚠️ 归属取 `dsl` 的 `tool` 字段（唯一权威），不另维护映射。
                    try:
                        _owner = _dsl_fb._ACTIONS[name].tool
                    except Exception:
                        _owner = "os_execute"
                    _rt = (
                        f"\"{name}\" is not a standalone tool. It is a {_owner} action. "
                        f"Call {_owner}(action='{name}', params={{...}}, declared_risk=...)."
                        + ("" if _owner == "os_execute" else
                           f" {_owner} is not loaded by default - load it first.")
                    )
                    await event_queue.put({
                        "event": "tool_end", "action_id": aid,
                        "result_summary": _rt[:80], "status": "SYS_IDLE",
                        "model": used_model, "current_skill": name, "ok": False,
                    })
                    return ToolExecution(call=call, result_text=_rt, ok=False,
                                         error="os_execute action was incorrectly called as a standalone tool")
                # ── 先判"名字存不存在"，再往下走 ───────────────────
                # 必须在 execute() 之前判：`registry.execute` 对"找不到"和
                # "跑了但返回 None"都返回 None，事后无法区分（见 registry.py:280）。
                #
                # ⚠️ 也必须在**副作用确认之前**判。这个位置一开始放错了，
                # 结果是给一个根本不存在的 Skill 弹副作用确认框，然后
                # `asyncio.wait_for(..., timeout=300)` 干等五分钟——
                # 用户会看到一个"要不要允许 XXX 写文件"的框，而 XXX 压根不存在。
                _enabled_names = []
                try:
                    _enabled_names = self.registry.list_enabled_skills()
                except Exception:
                    pass
                if _enabled_names and name not in _enabled_names:
                    _disabled = []
                    try:
                        _disabled = self.registry.list_disabled_skills()
                    except Exception:
                        pass
                    # ⭐⭐ **MCP 也要能说出「它被禁用了」。**
                    #
                    # 🔴 问题：`list_tool_manifests` 只收 `ST_CONNECTED` 的 server，
                    #    于是一个被禁用的 MCP，它的工具**从目录里彻底消失** ——
                    #    模型调它会被判成 UNKNOWN_TOOL（"这个名字不存在"）。
                    #    而这正是本文件在 `_ToolFailCause` 那里明令避免的那件事：
                    #    **塞进 UNKNOWN_TOOL 是撒谎（它明明存在）。**
                    # ⚠️ MCP 比 Skill 更棘手：Skill 的 manifest 是本地文件，禁用了
                    #    照样读得到；**MCP 的工具清单要连上才知道** ——
                    #    从没连过的 disabled server，我们不可能知道它有哪些工具。
                    # ⭐ 但 MCP 的工具名**自带 server 名** ⇒ 判据下移一层：
                    #    从「这个**工具**被禁用了」下移到「它所属的**服务**被禁用了」。
                    #    📌 答不出精确的那一问时，答一个**同样有用且答得准**的问题，
                    #       比猜一个精确答案强。
                    _mcp_off = ""
                    try:
                        from core.mcp_client import MCPManager as _MM_d
                        _mcp_off = _MM_d.instance().disabled_tool_server(name)
                    except Exception:
                        _mcp_off = ""
                    if _mcp_off:
                        _cause = self._ToolFailCause.DISABLED_TOOL
                        _head = (f"The MCP server \"{_mcp_off}\" is currently disabled, "
                                 f"so \"{name}\" was not run.")
                    elif name in _disabled:
                        _cause = self._ToolFailCause.DISABLED_TOOL
                        _head = f"Skill \"{name}\" exists but is currently disabled, so it was not run."
                    else:
                        _cause = self._ToolFailCause.UNKNOWN_TOOL
                        _head = f"Tool \"{name}\" does not exist. The name is wrong; this is not a transient failure."
                    result_text = _head + "\n" + self._describe_tool_failure(name, _cause)
                    logger.warning(f"[ReAct] 工具调用失败 {_cause}: {name}")
                    await event_queue.put({
                        "event": "tool_end", "action_id": aid,
                        "result_summary": _head[:80], "status": "CORE_THINKING",
                        "model": used_model, "current_skill": name,
                        "tool_use_id": call.tool_use_id, "ok": False,
                    })
                    return ToolExecution(call=call, result_text=result_text, ok=False,
                                         error=f"{_cause}: {name}")

                # 注：延迟的技能/MCP 即使没 load 也能按名字直接执行（registry/MCP 分发与
                # schema 注入解耦），所以这里不拦——load_tools 只为给模型正确的参数 schema。
                # ── 本地 Skill 执行 ──────────────────────────────────────
                # 副作用确认检查
                _side_check = self._check_skill_side_effects(name)
                if _side_check:
                    _confirm_ev = asyncio.Event()
                    _cancelled = [False]
                    _loop = asyncio.get_running_loop()

                    def _on_confirm():
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    def _on_cancel():
                        _cancelled[0] = True
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    await event_queue.put({
                        "event": "execution_confirm",
                        "skill_name": name,
                        "side_effects": _side_check,
                        "on_confirm": _on_confirm,
                        "on_cancel": _on_cancel,
                    })
                    from core.runtime import inbox as _ib6
                    _oc6 = await _ib6.wait_confirm_or_user_message(_confirm_ev, 300)
                    if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                        _cancelled[0] = True

                    if _cancelled[0]:
                        # ⚠️ 同上：用户改口 vs 干等超时，要分开告诉模型。
                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                            result_text = (f"Skill \"{name}\" was NOT executed.\n"
                                           + _ib6.cancelled_by_user_message_note())
                        else:
                            result_text = (f"Skill \"{name}\" execution was "
                                           f"cancelled by the user.")
                        await event_queue.put({
                            "event": "tool_end",
                            "action_id": aid,
                            "result_summary": result_text[:80],
                            "status": "SYS_IDLE",
                            "model": used_model,
                            "current_skill": name,
                            "ok": False,
                        })
                        return ToolExecution(call=call, result_text=result_text, ok=False,
                                             error="user cancelled execution")

                # ⭐⭐⭐ [统一长任务 · 第三类载体] **本地 Skill 走同一条交还合同。**
                #
                # 🔴 上一版这里是 `async with sem: raw_result = await execute(...)`
                #    —— **没有超时，也没有交还**。一个跑很久的 Skill 会把整轮
                #    无限期堵住，比改造前的 `run_command`（至少 30 秒会报错）
                #    还糟：那是「错了」，这是「永远不回来」。
                # 📌 已定原则：**我们要识别的只有「长任务」，
                #    跟任务类型从来没有关系过。** 命令 / MCP / Skill
                #    是同一件事的三个载体，不是三种机制。
                #
                # ⚠️ 阈值用同一个 `_LONG_TASK_HANDBACK_SEC`（**5s**，不是 90s ——
                #    2026-08-22 那次建模 把命令 45 / 外部服务 90 / Subagent 5 统一成 5，
                #    这两处注释忘了跟着改，2026-08-25 排查时才发现）。
                # 📌 一条说 90 的注释配一个写 5 的常量，读注释的人会按 90 去推理，
                #    而按 90 推出来的每一步都是错的 —— **过时的注释比没有注释更贵**。
                #    ⭐ 而**不对称的依据不是任务类型，是「回看那一眼能不能看到
                #       东西」**：命令有 stdout（45s 就交还，因为回看有价值），
                #       Skill 只有它自己调 `report_progress` 时才有 ——
                #       所以给它更长的前台耐心。
                _sk_ref = f"long_{call.tool_use_id or aid}"
                _sk_disp = _cat.presentation(name, args)
                # ⚠️⚠️ **信号量必须手动收放**，不能用 `async with`：
                #    交还之后 Skill **还在跑**，而这个函数要 return ——
                #    `async with` 会在 return 时释放，那没问题；
                #    问题是**不释放**才是错的。前台并发位是「前台等待」的资源，
                #    交还之后这件事已经归后台合同管了。
                #    📌 **一个操作的占位，在控制权被交回模型之后，应该由
                #       「后台合同」决定，而不再由「前台等待的耐心」决定。**
                #       （与 `_HANDED_BACK_DEADLINE_SEC` 同一条判据。）
                await self._tool_parallel_sem.acquire()
                _sem_held = True
                try:
                    _sk_task = asyncio.create_task(
                        self.registry.execute(name, args, progress_ref=_sk_ref))
                    # ⭐ 同 MCP 那条：用户一开口就立刻交还，不等满阈值。
                    _sk_finished = await self._wait_or_user_speaks(
                        _sk_task, self._LONG_TASK_HANDBACK_SEC)
                    if not _sk_finished:
                        # 超阈值 → 交还控制权，Skill 继续跑
                        self._tool_parallel_sem.release()
                        _sem_held = False
                        result_text = await self._hand_back_long_task(
                            display=_sk_disp, bg_ref=_sk_ref,
                            action_id=aid, event_queue=event_queue)

                        async def _await_skill(_t=_sk_task, _n=name, _r=_sk_ref):
                            try:
                                _raw = await _t
                            except Exception as _e:
                                return (f"Skill \"{_n}\" failed after it was handed "
                                        f"back: {type(_e).__name__}: {_e}")
                            finally:
                                try:
                                    from core.runtime import progress as _pb4
                                    _pb4.forget(_r)
                                except Exception:
                                    pass
                            _t2 = str(_raw) if _raw is not None else \
                                f"Skill \"{_n}\" ran but returned no content."
                            return f"Skill \"{_n}\" finished:\n{_t2}"

                        await event_queue.put({
                            "event": "long_task_handback",
                            "task": asyncio.ensure_future(_await_skill()),
                            "bg_task_ref": _sk_ref,
                            "display": _sk_disp,
                            "skill_name": name,
                        })
                        # ⚠️ 刻意**不**收这条 ActionAttempt —— 那次尝试还没结束。
                        #    📌 不许为一件还没结束的事记一个结论。
                        return ToolExecution(call=call, result_text=result_text, ok=True)
                    raw_result = _sk_task.result()
                finally:
                    if _sem_held:
                        self._tool_parallel_sem.release()
                try:
                    from core.runtime import progress as _pb5
                    _pb5.forget(_sk_ref)
                except Exception:
                    pass
                # 走到这里名字一定存在，所以 None 的唯一含义是"跑了但没返回内容"——
                # 这句话现在是真的了。
                result_text = str(raw_result) if raw_result is not None else \
                    f"Tool \"{name}\" ran but returned no content."
                tool_data = getattr(raw_result, "data", None)

                # 记录执行失败状态（供后续修复路由使用）
                _is_failed = False
                if raw_result is None:
                    _is_failed = True  # None 返回明确标为失败
                elif getattr(raw_result, "success", True) is False:
                    _is_failed = True
                if not _is_failed and isinstance(result_text, str) and any(
                    result_text.startswith(m) or m in result_text
                    for m in ("错误：", "【错误】", "【执行失败】", "执行故障", "执行失败")
                ):
                    _is_failed = True

                if _is_failed:
                    self._last_skill_error = {
                        "skill": name,
                        "error": f"Tool \"{name}\" failed: {result_text}",
                        "consumed": False,
                    }
                    # 名字对、跑了、失败了 —— 区分"参数不合 schema"和"执行本身失败"。
                    # `registry.execute` 的参数校验失败会返回以 "Execution failed:" 开头的串
                    # （`registry.py` 里那三处），那三条是同一类：工具在，参数不对。
                    _param_fail = isinstance(result_text, str) and \
                        result_text.startswith("Execution failed:")
                    result_text = result_text + self._describe_tool_failure(
                        name,
                        self._ToolFailCause.BAD_PARAMS if _param_fail
                        else self._ToolFailCause.TOOL_ERROR,
                        tool_error=result_text if _param_fail else "",
                    )

                # OS dsl_plan 检测：Skill 返回了 OS 执行计划，需要转入 OS 执行循环
                _os_plan = (tool_data or {}).get("dsl_plan") if isinstance(tool_data, dict) else None
                if isinstance(_os_plan, list):
                    # 注入特殊标记，ReAct 主循环检测到后转路由
                    result_text = f"__OS_DSL_PLAN__{name}"
                    tool_data = {"dsl_plan": _os_plan, "_skill_name": name, "_skill_args": args}

                self._session_log_append(f"[Skill call] {name}")
                self._wm_add(
                    EntryType.SKILL_CALL, name, "call",
                    detail="", tags=["skill", "call", name],
                )
                self._last_called_skill = name

            ok = not _is_failed
            error = (f"Tool \"{name}\" failed: {result_text[:100]}" if _is_failed else "")

        except Exception as e:
            ok = False
            error = str(e).replace(chr(10), " ")
            result_text = f"Tool \"{name}\" execution error: {error}"
            raw_result = None
            tool_data = None
            self._last_skill_error = {
                "skill": name,
                "error": result_text,
                "consumed": False,
            }
            logger.error(f"[ReAct] 工具【{name}】执行异常: {error}")

        await event_queue.put({
            "event": "tool_end",
            "action_id": aid,
            "result_summary": result_text[:80],
            "status": "CORE_THINKING",
            "model": used_model,
            "current_skill": name,
            "tool_use_id": call.tool_use_id,
            "ok": ok,
        })

        # ⭐⭐ 挂起期证据日志 —— 挂在**工具结果前面**交给模型。
        #
        # ⚠️ 位置刻意在 `tool_end` 事件**之后**：那个事件的 `result_summary` 取
        #    `result_text[:80]`，放前面的话卡片摘要会变成日志开头而不是工具结果。
        #    📌 **给模型看的内容和给用户看的摘要，不该是同一个字符串。**
        #
        # ⚠️ 取完即清：它是一次性的，下一个工具不该再看到同一段。
        _pn = getattr(self, "_a3_pause_note", "")
        if _pn:
            self._a3_pause_note = ""
            result_text = f"{_pn}\n\n---\n{result_text}"

        return ToolExecution(
            call=call,
            result_text=result_text,
            ok=ok,
            raw_result=raw_result,
            tool_data=tool_data,
            error=error,
        )

    async def _execute_tool_batch(
        self,
        calls: list[ToolCall],
        *,
        used_model: str,
        base_guide: str,
        system_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
        active_tool_names: frozenset | None = None,
    ) -> list[ToolExecution]:
        """执行一批工具调用（Claude 单轮决策返回的所有 tool_use）。

        策略：
        - 按 Claude 原始顺序遍历，遇到连续的 parallel 工具段则并发执行，
          遇到 serial 工具时先 flush 前面的 parallel 段再串行执行。
          这样保证执行语义顺序与 Claude 意图一致。
        - 超出 MAX_TOOLS_PER_ROUND 的工具不执行，但必须生成 error tool_result，
          确保 tool_use / tool_result 数量完全对应（Anthropic API 要求）。
        - 结果按原始顺序排列。
        """
        original_calls = list(calls)
        executable_calls = original_calls[:self.MAX_TOOLS_PER_ROUND]
        skipped_calls = original_calls[self.MAX_TOOLS_PER_ROUND:]

        results_by_key: dict[str, ToolExecution] = {}

        def _key(c: ToolCall) -> str:
            return c.tool_use_id or f"tool_{c.name}_{c.index}"

        # 按原始顺序、分 parallel 段并发执行
        parallel_segment: list[ToolCall] = []

        async def flush_parallel():
            nonlocal parallel_segment
            if not parallel_segment:
                return
            tasks = [
                asyncio.create_task(self._execute_one_tool_call(
                    c, used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue, active_tool_names=active_tool_names,
                ))
                for c in parallel_segment
            ]
            for coro in asyncio.as_completed(tasks):
                ex = await coro
                results_by_key[_key(ex.call)] = ex
            parallel_segment = []

        # 并发资格问目录，不再问 `_classify_tool_safety` 那个三值返回。
        #
        # 🔴 那个函数把**两个正交维度压成了一个返回值**：
        #      if EXIT:  return "exit"     ← 先命中就 return
        #      if SERIAL: return "serial"  ← 于是这一行永远走不到
        #    结果是 5 个工具（create_new_skill / update_existing_skill /
        #    manage_existing_skill / WriteSkill / answer_open_interaction）
        #    **同时**登记在 SERIAL 和 EXIT 两张表里，而它们声明的 serial
        #    **永远不生效、且不报错**。
        # 📌 那不是"将来会漏"，是**当时就有一半声明是死的** —— 的直接动机之一。
        #
        # ⭐ 现在 `flow` 答「调用之后去哪」、`scheduling` 答「能不能和别人同批」，
        #    两个维度各自独立。exit 工具的真实调度语义是 **EXCLUSIVE**
        #    （与任何其他工具同轮 → 整批一个都不执行、全部写 error、下轮重选），
        #    **不是** SERIAL —— 写 SERIAL 就是又造一个新的死声明。
        # ⚠️ 走到这里的一定不是 exit 工具（`_run_react_loop` 在更早就摘走了），
        #    所以这里只需要分「能不能并发」：PARALLEL 进并发段，其余串行。
        #    找不到声明的（幻觉名字）落串行 —— 与改造前的兜底同向。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        for call in executable_calls:
            _d = _cat.get(call.name)
            if _d is not None and _d.scheduling is Scheduling.PARALLEL:
                parallel_segment.append(call)
            else:
                await flush_parallel()
                ex = await self._execute_one_tool_call(
                    call, used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue, active_tool_names=active_tool_names,
                )
                results_by_key[_key(ex.call)] = ex

        await flush_parallel()

        # 超限工具必须生成 error tool_result，否则 tool_use/tool_result 不对称
        for c in skipped_calls:
            results_by_key[_key(c)] = ToolExecution(
                call=c,
                result_text=(
                    f"Tool \"{c.name}\" was not executed: the model requested {len(original_calls)} tools this round, "
                    f"which exceeds the per-round system limit of {self.MAX_TOOLS_PER_ROUND}. "
                    "In the next round, decide whether to continue calling tools based on the returned results."
                ),
                ok=False,
                error="tool limit exceeded",
            )

        # 按 Claude 原始顺序排列
        ordered = []
        for call in original_calls:
            k = _key(call)
            ex = results_by_key.get(k) or ToolExecution(
                call=call, result_text="Tool execution result was missing.", ok=False, error="result missing"
            )
            ordered.append(ex)

        return ordered

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

    # ── 主流程 ───────────────────────────────────────────────────────────

    async def handle_query(self, query: str, image_parts: list | None = None, temp_file_hint: str | None = None):
        """公开入口：统一思考块（per-turn 单块）+ 行动卡片 action_id 透传。
        内部委托给 _handle_query_impl，通过 generator 过滤层处理：
        - 所有 thought_block_start/delta/done/summary 统一到同一 block_id
        - 重复 thought_block_start 静默丢弃（同一 turn 只开一次 UI 块）
        - thought_block_done/summary 缓冲到 terminal 事件之前才 flush
          （思考块视觉上一直开着直到答案出来前才收尾，类 Claude Code 感觉）
        """
        _TERMINAL = frozenset({"final_result", "user_note_pending", "skill_preview"})
        _turn_bid = f"turn_{time.time_ns()}"
        _block_open = False
        _pending_done: dict | None = None
        _pending_summary: dict | None = None

        # 预算硬上限的降级出口。放在这里而不是各个 provider 调用点，
        # 是因为这是【整个用户驱动路径的唯一收口】——ReAct 循环第几轮撞上限都走到这。
        # UI 入口（start_pipeline_task）那道闸只拦"发之前就已超限"，
        # 拦不住"这一轮跑到一半跨过阈值"，而那恰恰是最常见的情形。
        from core.usage import BudgetExceeded
        from core.provider import ProviderNotConfigured
        try:
            async for event in self._handle_query_impl(query, image_parts, temp_file_hint):
                ev = event.get("event")

                if ev == "thought_block_start":
                    if not _block_open:
                        _block_open = True
                        yield {**event, "block_id": _turn_bid, "stage_label": "Nano"}
                    continue  # 重复 start 静默丢弃

                if ev == "thought_delta":
                    yield {**event, "block_id": _turn_bid}
                    continue

                if ev == "thought_block_done":
                    _pending_done = {**event, "block_id": _turn_bid}
                    continue  # 缓冲，等 terminal 之前 flush

                if ev == "thought_summary":
                    _pending_summary = {**event, "block_id": _turn_bid}
                    continue  # 同上，最后一次 summary 胜出

                # terminal 事件之前 flush 思考块收尾
                if ev in _TERMINAL and _pending_done:
                    yield _pending_done
                    _pending_done = None
                    if _pending_summary:
                        yield _pending_summary
                        _pending_summary = None

                yield event

            # generator 正常结束时 flush 残余（防御性兜底）
            if _pending_done:
                yield _pending_done
            if _pending_summary:
                yield _pending_summary
        except ProviderNotConfigured as e:
            # 未配置 API Key。这条路径的存在本身就是为了"用户能先把界面打开再配"，
            # 所以这里必须给一句指路的话，而不是让 UI 转圈或弹内核异常。
            logger.warning(f"[Provider] 未配置 API Key，本轮无法调用模型: {e}")
            yield {"event": "final_result",
                   "content": ("我还没拿到 API Key，暂时没法思考。\n\n"
                               "点右上角三个点打开设置，在「通用 → 环境配置」里填一下就行，保存后立刻生效，不用重启。"),
                   "model": "NOT_CONFIGURED", "status": "SYS_IDLE",
                   "log": "尚未配置 API Key。",
                   "current_skill": None,
                   "rag_hit": False, "full_file_hit": False}
        except BudgetExceeded as e:
            # 硬上限撞在半路：给用户一句能看懂的话，而不是"内核异常"。
            # 不写 memory 的 assistant 轮次——这一轮模型其实没说话，
            # 把系统话术塞进对话历史会让下一轮的模型以为那是自己说的。
            _msg = (
                f"今天的用量已经到上限了（${e.cost:.2f} / ${e.cap:.2f}），我先停一下。\n\n"
                f"点右上角三个点打开设置，在「通用」里能调高上限；不改的话，用量每天 0 点自动重置。"
            )
            try:
                from core.health import get_system_events
                get_system_events().add(
                    f"Daily budget cap reached (${e.cost:.2f} / ${e.cap:.2f}); the turn was stopped partway."
                )
            except Exception:
                pass
            logger.warning(f"[Budget] turn 中途撞上硬上限: {e}")
            yield {"event": "final_result", "content": _msg,
                   "model": "BUDGET_CAPPED", "status": "SYS_IDLE",
                   "log": "今日用量已达上限，本轮已停止。",
                   "current_skill": None,
                   "rag_hit": False, "full_file_hit": False}
        finally:
            # ⭐ 「回复这条」是**一次性**的：这一轮用过就复位。
            #
            # 用户的理由（2026-08-06）："模型已经被提醒过一次就够了，
            # 而且用户想聊别的时很容易忘记取消。"
            #
            # ⚠️ 放在 `finally` 而不是正常收尾处：这一轮**不管是正常结束、
            #    撞预算上限、还是抛异常，指向都已经被这一轮消费掉了**。
            #    漏掉任何一条出口，它就会粘到下一轮 —— 那正是这个 bug 本身。
            #
            # 📌 所有权：`_reply_target` 只有 orchestrator 一个权威副本。
            #    UI 通过 `_set_reply_target()` 写、直接读 `agent._reply_target`，
            #    不再自己存一份（双权威必然不同步 —— 这里尤其明显：
            #    复位发生在轮次结束，而那时 UI 根本没参与）。
            #
            # ⭐⭐⭐ [2026-08-13 CMD63] **两个字段都要清。**
            #    `_reply_target_turn` 是 UI 按发送时移交过来的本轮快照
            #    （见 `hand_off_reply_target`）—— 正常路径下真正带着 iid 的是它，
            #    漏清它 = 指向粘到下一轮，也就是这个 bug 的原始形态。
            #    `_reply_target` 仍要清：有可能压根没经过 UI 发送路径。
            _rt_consumed = (self._reply_target_turn or self._reply_target or {}).get("iid")
            if _rt_consumed:
                logger.info(
                    f"[Interaction] 「回复这条」{_rt_consumed} 已被本轮消费 → 自动复位"
                )
            self._reply_target = None
            self._reply_target_turn = None

    async def _handle_query_impl(self, query: str, image_parts: list | None = None, temp_file_hint: str | None = None):
        current_running_skill = None
        used_model = "UNKNOWN"
        event_queue = asyncio.Queue()

        # ⭐⭐⭐ 「这一轮有图、还没记下来」—— **本轮标志，不看 memory。**
        #
        # 🔴 实测连栽两次，两次都是**顺序**问题，而且两次的顺序还不一样：
        #   ① 第一版把判据写成 `memory.storage[-1] 有 ui_images`。
        #      但**核心工具清单在本函数上方就算完了**（`[TOKEN-PLAN]` 那行），
        #      那时 `add_message("user", query)` 都还没执行 —— storage 里根本没有这条。
        #      → `note_image` 被算进 deferred，模型只能先 `load_tools` 去捞。
        #   ② 就算捞到了，ReAct 循环里 `storage[-1]` 已经变成 `tool_results` 了，
        #      判据又变假 → 下一轮它从清单里消失 → 模型说「note_image 工具不可用」。
        #      ⚠️ 而 UI 上那个对钩是 **load_tools 成功**的对钩 ——
        #      **pill 和那句话都没撒谎，它们说的是两件事。**
        #
        # 📌 **判据不能挂在「storage 的最后一条是什么」上** —— 那个位置在一轮之内
        #    会变好几次，而"这一轮有没有图"从头到尾只有一个答案。
        # 📌 更一般的那条：**一条顺序断言只保护它写下的那个顺序。**
        #    为约束①补了测试，然后在②③上各摔了一次。
        self._turn_image_pending = bool(image_parts)

        # 🔴🔴 [2026-08-22 实测] **这里原来是无条件 `= None`，那是个真 bug。**
        #
        # 症状：Skill 交还之后，用户下一轮说「把这个放后台」，`dont_wait` 回
        #      「There is no slow call handed back to you right now」——
        #      而那件事**明明还在跑**。（Nano 随后还照着说了「已经在后台了」。）
        #
        # ⭐ 根因：**「可以被交出去的载体」这个记录点只活一轮，
        #    而「把这个放后台」这句话必然发生在下一轮。**
        #    ⚠️ 与载体类型无关 —— 命令/MCP/Skill 走同一个
        #       `_hand_back_long_task`，这是**轮级状态**的问题。
        #       实测里命令那两次成功，只是因为模型**当轮就调了** `dont_wait`。
        #
        # ⭐⭐ 而当初清空它的那个理由，**已经被一个更精确的东西接管了**：
        #    旧理由：「不清的话，下一轮 `dont_wait` 会作用在一条早就结束的等待上」
        #    现状：`_handle_dont_wait` 里已经有 liveness 校验
        #            （`find_by_id` + `is_live`），它**逐条问「那条等待还活着吗」**。
        # 📌 **一个防御措施的理由被另一个更精确的措施接管之后，它自己就该移除** ——
        #    两个都留着，粗的那个会把细的那个的精确性整个抹掉。
        # 📌 更一般的那条（本项目反复出现）：**别用近似物回答一个能精确回答的问题。**
        #    「是不是本轮」是近似物；「那条等待还活着吗」才是那个问题本身。
        #
        # ⚠️ 所以这里**只在指向已经死掉时才清**，而不是每轮都清。
        #    ⚠️ 校验放在这里是**顺手清账**，不是防线 —— 真正的防线仍然是
        #       `_handle_dont_wait` 里那一处（它在**用之前**问）。
        #       📌 记录点的清理和使用点的校验，是两件事，都要有。
        _car_prev = getattr(self, "_detachable_carrier", None) or {}
        if _car_prev.get("wait_id"):
            try:
                from core.runtime.kernel import get_kernel as _gk_pc
                from core.runtime import waitcond as _wc_pc
                _r_pc = _wc_pc.find_by_id(_gk_pc(), _car_prev["wait_id"])
                if _r_pc is None or not _r_pc.is_live:
                    logger.info(f"[B1] 上一轮的可交载体已终态"
                                f"（{_car_prev['wait_id']}）→ 清掉")
                    self._detachable_carrier = None
                else:
                    # ⭐⭐⭐ [2026-08-22] **用户一开口，还在前台上的载体自动转入后台。**
                    #
                    # 🔴 这一步（前台 → 后台）原本设计成「用户插话／我改主意 → `dont_wait`」——
                    #    即**由模型决定**。实测 73 证明这条走不通：
                    #    **模型不会自己调**，除非用户点名工具名，而真人不会那么说话。
                    #    于是用户说「把这个放后台」，Nano 嘴上答应、实际什么都没做。
                    #
                    # ⭐ 用户的推理（这才是关键，不是"改成自动"这么简单）：
                    #    「用户说了一句话，nano 此刻要处理的**不是**『用户交代了什么任务』
                    #      （我们猜不出），而是**『回应用户这句话本身』**」
                    #    → 于是「有没有需要我转移注意力的事」在这个情形下**恒为真**。
                    # 📌 **一个恒为真的判断，不该拿去问模型。**
                    #    这不是系统替模型做判断，是系统把一个不需要判断的东西
                    #    从模型的待办里拿走 —— 合乎老分工：
                    #    **系统答发生了什么，模型答所以怎么办**；而这件事没有「所以怎么办」。
                    #
                    # ⚠️ 只在**用户消息**这条路触发（本函数就是用户消息入口）；
                    #    回看轮不走这里，所以场景 #1（只有一件长任务）和 #3（有依赖）的
                    #    「留在前台」语义完好 —— 没人插话就什么都不变。
                    # ⚠️ 已经在后台的一个字都不动：`_detachable_carrier` 只存
                    #    **还在前台上**的那个，转入后台时就被清了（天然幂等）。
                    # ⚠️ `next_step` 填的是**真话** —— 回应用户确实就是下一步要做的事，
                    #    不是为了过闸编一个理由。
                    _pk_ev = self._park_carrier(
                        _car_prev, "回应用户刚说的话", why="用户插话")
                    yield _pk_ev
            except Exception as _e_pc:
                # ⚠️ 查不动就**留着** —— 使用点还会再查一次。
                #    📌 「不知道」该表现成「交给下一道关」，不是「当它死了」。
                logger.warning(f"[B1] 校验上一轮可交载体失败（留着，使用点再查）: {_e_pc}")
        else:
            self._detachable_carrier = None

        # 本轮 token 计数打点（与记账同一异步流，供 UI 单条显示取差值，避免时序竞争）
        try:
            from core.usage import usage_tracker as _ut
            _ut.begin_turn()
        except Exception:
            pass

        # 本轮 write_user_note 写入的笔记（react 内不发终端事件，攒到 final_result 交 UI）
        self._notes_written_this_turn = []

        # 每轮开始：RAG / FullFile hit 标记清零
        self._rag_hit_this_turn = False
        self._full_file_hit_this_turn = False  # Phase 2

        async def realtime_callback(m_name: str):
            nonlocal used_model
            used_model = m_name
            await event_queue.put({"event": "thinking", "log": f"模型已就绪，正在驱动 [{m_name}] 分析意图...", "status": "CORE_THINKING", "model": m_name, "current_skill": current_running_skill})

        yield {"event": "thinking", "log": "正在进行 LLM 语义路由...", "status": "CORE_THINKING", "model": "CONNECTING", "current_skill": None, "rag_hit": False, "full_file_hit": False}

        # OS 双循环合一：os_execute 始终注入主 ReAct 循环（include_os=True）。
        # OS 不再是被前置分类器硬路由到 _handle_os_task 的"特殊任务"，而是和查知识库、
        # 调 Skill 同级的无差别内置能力。这样 wait_for / render_visual / os_execute 等
        # 在同一个循环里自由组合，彻底消除"OS 任务里够不到挂起"这类双循环漏洞。
        # ⭐⭐ ① 工具清单问目录。
        #    改造前是 `_build_skills_info(...)` 那 11 个 `include_*` 开关的合力结果，
        #    「这一轮给哪些工具」要靠读懂 11 个布尔的组合才能答；
        #    现在是 `advertised(scope, runtime)` —— **有 binding + 此刻 available +
        #    不是 HIDDEN**，三条规则，没有开关。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        _advertised = _cat.advertised(ToolScope.MAIN, _rtv)

        # ── 能力门控·第一道：manifest 层不给坏掉的工具 ──────────────────────
        # 光在 system prompt 里说"知识库不可用"不够——工具 schema 还摆在模型面前，
        # 它照样会调（外部评审推演出来、且已回代码核实：自然语言约束替代不了执行层约束）。
        # 只摘 UNAVAILABLE 的；DEGRADED 保留工具，限制写在能力边界文本里由模型自己权衡。
        #
        # ⚠️⚠️ **它刻意留在消费点，不进 Catalog，也不做成 `availability`。**
        #    📌 **这不是第二张名单，它是一层正交的临时遮罩**：
        #       Catalog 答「有哪些工具」，health 答「哪些暂时不能用」。
        #    🔴 塞进 `availability` 会出一个具体的错：坏掉的工具 `resolve()` 返回
        #       None → 调用方走 UNKNOWN_TOOL 诊断 → 模型收到「这个工具不存在」。
        #       **而那是假话。** 违反 「给模型的失败信息必须正确」——
        #       "不存在"和"坏了"是两种故障，共用一个出口就是撒谎，
        #       而那道执行层 fail-fast 存在的全部意义正是说出**正确**的那一句。
        #    还有一条实际理由：health 状态**在一轮之内会变**（探针恢复），
        #    而 Catalog 是按轮构建的，塞进去会让两者的生命周期绑死。
        #    ⚠️ 所以后人看到这里请**不要**把它"优化"进 Catalog —— 它没有违反
        #       「只有一张名单」，它根本不在回答同一个问题。
        try:
            from core.health import get_health
            _blocked = get_health().blocked_tools()
        except Exception:
            _blocked = set()
        if _blocked:
            _before = len(_advertised)
            _advertised = [d for d in _advertised if d.name not in _blocked]
            logger.warning(
                f"[Health] 能力门控：本轮下架 {_before - len(_advertised)} 个工具 "
                f"({', '.join(sorted(_blocked))})"
            )

        # ── 按需加载：拆"核心常驻" vs "延迟加载" ─────────────────────────────
        # Token 压缩：不让普通主循环常驻全部内置工具。默认只给极小核心集，
        # 其余只发一行感知，模型需要时先 load_tools。
        # ⚠️ `_tool_pool` 保留：它是 load_tools 真正 append 进 tools_manifest 的
        #    schema 来源（`_pending_loaded_manifests`），不是第二份名单 ——
        #    它的内容现在**整个来自** `advertised`。
        regular_skills = [d.manifest for d in _advertised]
        self._tool_pool = {m.get("name"): m for m in regular_skills if m.get("name")}
        # ⭐⭐⭐ [2026-08-23] **核心集排成「无条件在前、带条件在后」。**
        #
        # 📌 缓存前缀顺序是 `tools → system → messages`，tools 在最前面 ——
        #    它一变，后面全废。而 `availability` 带条件的核心工具
        #    （`dont_wait` / `stop_background` / `set_next_checkin`）
        #    **在轮与轮之间进出**，改的正是这个最脆弱的位置。
        # ⚠️ 这个副作用是 2026-08-22 加 `_when_has_carrier` 时**没想到的** ——
        #    当时只顾着 「工具和事实来源必须由同一个条件控制」，
        #    没意识到那个条件同时决定了缓存前缀稳不稳。
        #    📌 **一个判据在它自己的维度上正确，不代表它在别的维度上无害。**
        #
        # ⭐ 于是分成两段，断点打在中间（与 `_cached_system` 的 stable⟂dynamic 同形）：
        #        [ 无条件核心 ] ⟂ [ 带条件核心 ] + [ load_tools 追加的 ]
        # ⚠️ **顺序在这里被建立，`_to_anthropic_tools` 依赖它** —— 别在别处重排。
        from core.tools.catalog import ALWAYS as _ALWAYS_AV
        _core_always = [d for d in _advertised
                        if d.preload is Preload.CORE and d.availability is _ALWAYS_AV]
        _core_cond = [d for d in _advertised
                      if d.preload is Preload.CORE and d.availability is not _ALWAYS_AV]
        self._core_manifest = [d.manifest for d in _core_always + _core_cond]
        # ⭐ 稳定段长度 —— 发请求时告诉 provider 断点该打在哪。
        #    ⚠️ 它必须跟着 `_core_manifest` 一起算，**不能在别处重新数** ——
        #       📌 一个「这份名单的前 N 个」的数字，和那份名单必须同源，
        #          否则两边各自演化，断点就会打在错的位置上（而且不报错）。
        self._core_stable_n = len(_core_always)
        _deferred = [d for d in _advertised if d.preload is Preload.DEFERRED]
        # ③ 感知块由目录渲染 —— `[:28]` 字符级截断（根因）随旧实现删除。
        # ⚠️ 这里传的是**过完 health 门控**的那批，所以「坏掉的工具」也不会出现在
        #    感知块里 —— 与 manifest 层保持同一个口径（说得见的都真能用）。
        self._deferred_awareness_text = (
            _cat.DEFERRED_HEADER + "\n".join(f"  - {d.name}: {d.awareness}"
                                             for d in _deferred)
        ) if _deferred else ""
        try:
            logger.debug(
                f"[TOKEN-PLAN] core_tools={len(self._core_manifest)} "
                f"({','.join(m.get('name','') for m in self._core_manifest)}); "
                f"deferred_tools={len(_deferred)}"
            )
        except Exception:
            pass
        base_guide = self._system_guide_template.format(skills=', '.join(self._skill_names(regular_skills)))
        # ⚠️ 这里**曾经**有一行 `self._os_task_busy = False` 的每轮重置。
        # 已删 —— 租约靠 TTL 自愈，不需要"每轮兜底"这种依赖下一轮才生效的止血。
        # 📌 实测证伪过那个前提：「每轮重置把爆炸半径压到一轮」——
        #    「一轮」只在**有下一轮**时才存在，用户不说话它就一直挂着（实测 61 分钟）。
        # ⭐ 前台窗口身份的比较基准每轮清零 —— 跨轮的"窗口变了"没有意义，
        #    那本来就是两件事之间。与活动租约同一个边界（见 `_window_identity_note`）。
        self._last_fg_window = None
        # ⭐ 活动租约也在每轮开头兜底归还。
        #    它的正常释放点是"一整段 GUI 操作结束"，而"结束"最可靠的判据就是
        #    **下一轮开始了** —— 所以这里是它真正的边界，不是 os_execute 的 finally。
        #    理由见 `os_execute` 分支里那段注释。TTL 只是二道兜底。
        _rt_lease_release(self)
        # 同理重置 ReAct batch 事务标记。2026-08-04 补：
        # 它原本【只有】置位与正常路径的复位，没有任何跨轮兜底——
        # 一旦 tool_calls 已写、tool_results 未写完时抛异常（`_run_react_loop`
        # 5781→5847 之间），它就会带着 True 活到后面的轮次，造成两件事：
        #   ① `_clean_damaged_memory` 在【后来一次无关的失败】里误判成"批次未关闭"
        #      → 回滚掉合法的历史工具对（而那个守卫存在的目的恰恰是防止误删）；
        #   ② 400 分支（5665）会跳过 `repair_invalid_tool_turns`，
        #      历史坏 pair 修不掉 → 下一轮继续 400。
        #
        # ⚠️ 为什么不是在那两个调用点外面包 try/finally：
        # 这个标志的用途就是"异常发生时告诉 _clean_damaged_memory 可以安全回滚"。
        # finally 会在外层错误处理读到它【之前】清零，回滚被跳过 —— 等于把这个机制废掉。
        # 所以正确做法是照 _os_task_busy 的范式做【每轮重置】：轮内语义不变，跨轮不残留。
        #
        # ⚠️ 顺序很重要：**必须在重置之前把旧标志的值抄给 shadow**。
        # 因为"上一轮遗留 True"正是覆盖表第 6/7 行（批次抛异常 / generator 被丢弃）
        # 唯一的观测窗口——重置之后就永远看不到它了。
        self._rt_turn_id = "rtturn_" + __import__("uuid").uuid4().hex[:12]
        # 工具失败计数按轮清零：跨轮保留会让"你这轮已经试过"变成假话。
        self._tool_failures_this_turn = {}
        # 每轮开头只需要 sweep 掉上一轮的残留 span，没有旧字段可重置了。
        _rt_sweep_stale_spans(self, None)

        # User profile injection
        try:
            import json as _json, pathlib as _pl
            from core.paths import data_path as _data_path
            _prof = _data_path("user_profile.json")
            if _prof.exists():
                _p = _json.loads(_prof.read_text(encoding="utf-8"))

                _not_set = "not set"
                _nickname = _p.get("nickname") or _not_set

                _bday = _p.get("birthday")
                _birthday_label = f'{int(_bday.split("-")[0])}/{int(_bday.split("-")[1])}' if _bday else _not_set

                _region = _p.get("region", {}).get("display", _not_set)
                _identity = _p.get("identity", _not_set)
                
                base_guide += (
                    f"\n\n[User Profile — From Settings, Do Not Duplicate Into Memory]\n"
                    f"Preferred name: {_nickname}\n"
                    f"Birthday: {_birthday_label}\n"
                    f"Region: {_region}\n"
                    f"Identity/context: {_identity}\n"
                    f"These fields are maintained by the user in settings. "
                    f"If the user asks Nano to remember name, birthday, region, identity, or similar profile fields, "
                    f"respond based on the information above: if already set, say Nano already knows it; "
                    f"if not set, suggest filling it in settings. "
                    f"Do not write these fields again into conversation memory."
                )
        except Exception:
            pass

        # 工具感知注入——让模型知道自己当前真实拥有哪些能力
        try:
            base_guide += self._build_tool_awareness_block()
        except Exception:
            pass

        # 按需加载：把延迟工具的"感知"（名字+一句话+load_tools 用法）注入
        try:
            if getattr(self, "_deferred_awareness_text", ""):
                base_guide += self._deferred_awareness_text
        except Exception:
            pass

        # OS_CAPABILITY_PROMPT 很长，且普通聊天不需要。
        # 现在改为：只有 load_tools 真正加载 os_execute / look_at_screen / set_window_mode 后，
        # 在 _run_react_loop 内按需注入，避免每句普通消息都背 OS 操作手册。

        # 用户唤醒（打底源）——用户发来新消息时，若存在 active 挂起记录，
        # 把"你之前在等什么"注入 guide，并把这些记录标记恢复（resolved_by=user）。
        # 这样模型会带着上下文重新决策"等的事成了没"，没成可以再 wait_for。
        # 标记恢复后，store 里这些记录不再 active，app 侧基于 store 轮询的定时器
        # 自然不会再为它们触发（不留孤儿定时器）。
        # ⭐⭐ [无缝对话 · 措辞] 续接段：告诉它「你这一段会和上一段拼在一个气泡里」。
        #
        # ⚠️⚠️ **注入的是事实，不是表演指令。**
        #    ❌「请假装这是一次连续对话」→ 那是**演出脚本**，模型会批量生产过渡词，
        #      而且可能**编造连续性**（声称自己做过没做的事）。
        #    ✅「你上一段和这一段会显示在同一个气泡里」→ 这是它**推导不出来**的事实。
        #    📌 与故障卡片验证过的那条同源：
        #       **给模型注入它自己的状态，模型就会自主调整行为 —— 给状态，不给剧本。**
        #
        # ⭐ 而且**只注入它推导不出来的那一点**：它已经能在对话历史里看到自己
        #    上一段回答了，它唯一看不到的是**呈现方式**。
        #    📌 只注入模型无法从上下文推导出来的那一点 ——
        #       其余全是噪音，而噪音会训练它忽略注入。
        #
        # ⚠️ **走这条动态段通道，绝不拼进用户原话** ——
        #    📌 用户的原话必须保持原样（同早先`answer_verbatim` 那条纪律）；
        #       拼进去之后模型就分不清哪句是用户说的、哪句是系统说的。
        try:
            _seam_part = int(getattr(self, "_seam_continuation_part", 0) or 0)
            if _seam_part >= 2:
                # ⚠️⚠️ **这段文案 2026-08-08 改过一次，原因值得记。**
                #    第一版写的是「你**上一段回答**和这一段会显示在同一个气泡里」——
                #    那在「执行分段」时代是真的，但改成**协作式中断**之后
                #    **上一段被撤回了、屏幕上不存在** → 那句话变成了**假话**。
                #    📌 **一个描述「当前呈现方式」的注入，在呈现方式变了之后
                #       必须一起改，否则它就是在给模型讲一个不存在的现实。**
                #
                # ⭐ 现在说的是真事：用户在你上一次回答成型**之前**又补了一句，
                #    所以你会看到**连续多条 user 消息**，而**只应该给一个回答**。
                #    这也顺带解决了 用户要的「标记第一条/第二条」——
                #    模型知道后面那条更新、前面那条可能已被推翻。
                base_guide += (
                    "\n\n[Interjected] The user sent another message while you were "
                    "still working on the previous one, so your earlier attempt was "
                    "discarded before it produced anything — nothing was executed and "
                    "nothing was shown to them. You will therefore see several "
                    "consecutive user messages: the LATER ones are more recent and may "
                    "revise or cancel the earlier ones. Give ONE single answer that "
                    "addresses their combined intent. Do NOT answer them one by one and "
                    "do NOT contradict yourself."
                )
                if _seam_part > 2:
                    base_guide += (
                        f" They have interjected {_seam_part - 1} time(s) so far."
                    )
                # ⚠️⚠️ **还有哪些东西在跑，必须告诉它** ——
                #    否则用户说「算了这个不做了」时，模型**不知道有什么可取消**。
                #    📌 代码判「有没有新消息 → 中断本轮」（确定性）；
                #       模型判「那些后台任务要不要取消」（需要理解意图）。
                #       —— 「模型判断 vs 代码判据必须分开」的又一实例。
                #    ⭐ 而这条注入是那个分工的**前提**：没有它，模型没有可判的材料。
                try:
                    from core.runtime.kernel import get_kernel as _get_bg_wait_kernel
                    from core.runtime import waitcond as _wc_bg
                    _bg = [r for r in _wc_bg.list_live(_get_bg_wait_kernel(), oldest_first=True)
                           if _wc_bg.WakeSource.BACKGROUND in r.wake_on]
                    if _bg:
                        _bg_lines = "; ".join(
                            (r.reason or "background work")[:80] for r in _bg[:5])
                        base_guide += (
                            f"\n[Still running] {len(_bg)} background item(s) are still "
                            f"running and were NOT affected by the interruption: "
                            f"{_bg_lines}. They keep going unless you explicitly cancel "
                            f"them. If the user's new message means they should stop, "
                            f"cancel them yourself — the interruption did not."
                        )
                except Exception:
                    pass
            # ⚠️ 即读即清：它只对**这一次**调用有效。
            self._seam_continuation_part = 0
        except Exception:
            pass

        try:
            from core.runtime.kernel import get_kernel as _get_active_wait_kernel
            from core.runtime import waitcond as _wc_active
            _active_susp = _wc_active.list_live(
                _get_active_wait_kernel(), oldest_first=True)
            if _active_susp:
                base_guide += self._build_suspension_resume_injection(_active_susp)
                # ⭐⭐ **用户发消息不再 resolve 任何挂起。**
                #
                # 旧代码只豁免 background-only，于是 `wake_on=['timer']` 的挂起
                # 会被**任何一句无关的话**收掉。实测场景：
                #   用户「100 秒后在桌面写个文件」→ Nano 设定时
                #   → 用户第 30 秒说「今天天气不错」→ **那个承诺就没了**。
                # 旧代码的辩解是"注入回去让模型自己再 wait_for 一次"，
                # 等于把一个**确定的承诺**换成了一次**模型判断**。
                #
                # 📌 判据（用户提出、外部评审独立确认）：
                #    **「本轮 Nano 不再暂停」与「未来的承诺已经解除」是两件事。**
                #    用户发消息只证明前者。
                #
                # 现在：注入**保留**（模型需要知道自己在等什么），resolve **删除**。
                # 定时到点由轮询收；用户真想取消，说出来 —— 模型调 `cancel_wait`（F 项）。
                # ⚠️ 这也让 `resolved_by="user"` 这个值从此不再产生。
                logger.info(
                    f"[Suspension] 用户唤醒：注入 {len(_active_susp)} 条挂起上下文"
                    f"（**不 resolve** —— 用户说话不代表他等的事成了）")
                # 观测期曾在这里重读新旧两边做对答案；双写已消失，继续比较只会
                # 拿权威和自己比，制造一条永远为真的观测噪音。
        except Exception as _se:
            logger.warning(f"[Suspension] 用户唤醒注入失败（跳过）: {_se}")

        # "信息真实"：模型训练分布里大量"我这就去处理/你等我一会"这类
        # agent式表述——在 Claude Code 等真的会在后台持续工作的 agent 里
        # 是真的，但这套框架严格单轮同步：本轮回复结束后这次"我"就不存在
        # 了，之后什么都不会发生，除非用户发下一条消息触发全新一轮。
        # 放在 base_guide 源头，会传播到下游所有 guide 变体
        # (exploration_guide / 各阶段 system_guide 都是 base_guide + ... 拼出来的)。
        base_guide = base_guide + (
            "\n\n[Conversation Boundary — Do Not Promise Later Work]\n"
            "After each reply ends, this turn stops completely. Nano will not keep working in the background unless the user sends another message "
            "or a wait/background mechanism has explicitly been set up.\n"
            "- Do not say things like 'I'll do it now', 'wait a moment', 'I'll handle it later', or 'I'll fix it for you' if the action will not actually happen in this turn.\n"
            "- If a tool can complete the task now, use it now. If not, clearly tell the user what they need to say or do next.\n"
            "- Nano cannot directly rename/delete/disable/enable Skill files through normal text. For those requests, use the Skill management flow "
            "and explain that confirmation is required before anything changes.\n"
            "- For Skill replacement, mention both creating the new Skill and what will happen to the old one. Do not say 'I'll delete the old one' "
            "unless the system is actually entering the confirmed management flow."
        )

        # Token 压缩 Stage2：动态数据块（session_log/episodic/ambient/tone）的【固定使用说明】
        # 从每轮全价重发的动态区，挪到这里的稳定前缀（缓存 0.1x，只付一次）。哨兵之后只留
        # 真正每轮变化的【数据本身】。模型看到的信息完全一致，只是换了缓存分区，零体验变化。
        base_guide = base_guide + (
            "\n\n[Live Context Blocks — How To Use Them]\n"
            "Below the boundary, when available, you may see these live blocks. Read them when relevant, "
            "but never invent details beyond what they contain:\n"
            "- [Session Log]: concrete actions Nano took THIS session (file paths, Skill names, deploy/call "
            "status, generated outputs). Trust them for those facts.\n"
            "- [Recent Cross-Session Summaries]: hint-level memory of past sessions. For exact paths, Skill "
            "names, deployments, deletions, or call records, call recall_working_memory instead of relying on the summary.\n"
            # ⭐⭐ **把「不许调工具」的范围收回到它本来该管的那一件事上。**
            #
            # 🔴 原文一句话罩住了两种完全不同的请求：
            #      「我刚才在干嘛」        → 这个块**就是**答案，再调工具纯属浪费（原意）
            #      「把我刚才那个文件读了」 → 这个块**给不出**答案，它只有名字没有句柄
            #    而禁令是无差别的 ⇒ 第二种请求被一起挡住，模型要么拿窗口标题里的
            #    相对文件名去硬试，要么直接回一句「我看不到路径」。
            # 📌 **一条按「话题」划的禁令，挡住的是「意图」** —— 两句话都在说
            #    「刚才那个」，但一句要的是叙述，另一句要的是能喂给工具的参数。
            #
            # ⚠️ **刻意【不】在这里指向 `resolve_ambient_referent`** —— 那个工具还不存在。
            #    📌 同 `os_execute` 描述里那条教训：**schema 说有、运行说没有，
            #       是最难查的一类失败 —— 模型会反复尝试，而每次都合法地失败。**
            #    ⇒ 2026-08-26 落地，出口已接上（`resolve_ambient_referent`）。
            #
            # ⭐⭐ 注入的是**互动感版**（时间 + 名字），完整路径/URL 留在 trail 里
            #    按需取 —— 这一行是**每轮无条件注入**的，每多一个字都乘以轮数。
            #    📌 三层各管一件事：**采集要全**（丢了就永远丢了）、
            #       **注入要省**（每轮都付钱）、**拉取按条**（不是按范围：
            #       语义匹配发生在已经在上下文里的那一版上）。
            "- [Ambient]: what the user was doing right before switching to Nano (app, window title, activity "
            "rhythm, idle, background audio). Use it to resolve references like 'this', 'that', or 'what I was just "
            "doing'. If the user asks what they were just doing, answer directly from this block — do NOT call tools "
            "or memory for that, the answer is already here, and do not ask back. That rule covers only telling them "
            "what they were doing. Acting on one of those things is different: the block gives names, not handles - a "
            "window title is not a file path. An entry marked with a small triangle can be turned into a real file "
            "path, URL or folder by calling resolve_ambient_referent with the timestamp printed on that same entry; "
            "entries without the mark hold nothing beyond what you already see. Never invent a path for something you "
            "only saw the name of. Weave it in naturally only when "
            "helpful; do not recite app names as a checklist, do not mention it every reply, do not over-explain or "
            "sound like surveillance, and add no privacy disclaimers. It reads no private content.\n"
            "- [Tone]: affects the flavor/style of your wording ONLY — never answer quality, completeness, or "
            "accuracy. Do not talk about your own mood unless asked. On a serious task, prioritize completing it "
            "clearly and correctly. If the tone is negative or terse, stay concise and task-focused; never mock, "
            "blame, guilt-trip, or self-pity.\n"
            "- [Capability Health]: components that are currently broken or degraded. UNAVAILABLE means the related "
            "tools are withheld this turn — do not claim you will use them, and if the user asks for that capability, "
            "say plainly what is wrong. DEGRADED means still usable with reduced quality or coverage — mention it only "
            "when it actually affects the answer. Never invent a workaround you do not have.\n"
            "- [Recent System Events]: things that happened inside this process — component failures, recoveries, "
            "background completions. **They are not things you said or did.** Do not bring them up on your own; use "
            "them only when the user asks what just happened."
        )

        # ── 缓存分界 ──────────────────────────────────────────────────────────
        # 到此为止的 base_guide 是【每轮一致的稳定前缀】（persona / 工具说明 / OS 能力 /
        # 对话边界等），插入哨兵；下游追加的 session_log/episodic/ambient 等每轮变动
        # 的注入都落在哨兵之后。provider._cached_system 据此只缓存稳定前缀，动态内容
        # 不再打穿缓存。base_guide 直接当 system_guide 用的路径，哨兵后为空，无副作用。
        # ⚠️ Environment 块进**哨兵之前** —— 它一次会话内不变，属于稳定前缀，
        #    落在哨兵之后会白白打穿 prompt cache。
        #    📌 对照：MCP 清单是**动态**的（状态会变），必须在哨兵之后。
        base_guide = base_guide + self._environment_block() + CACHE_BREAK_MARKER

        # Step 4：pending 超时检查
        self._expire_stale_pending()

        # 1. 待确认管理动作优先
        # ── 1. 这里【曾经】是 Skill 管理/修改确认的路由劫持 ──────────────────
        # [2026-08-06 删除] 原来是
        #     if self._pending_action: -> _handle_pending_action()  （117 行 + 一个专用分类器）
        # 每条消息先跑 `classify_pending_action_intent` 分成 6 个标签，再按标签分支，
        # 其中 NEW_REQUEST 那一支还要靠一个伪事件 `__pending_new_request__`
        # 从生成器里"逃逸"回常规路由 —— 那个 break-then-continue 的控制流本身就很脆。
        #
        # ⚠️ 顺带纠正一处长期误记：`_pending_action` 的四个 op
        # （delete / disable / enable / update_skill）**全是 Skill 生命周期操作，
        # 与 OS 无关**。早先的设计曾把它记成"OS 风险确认"，因为字段名太泛。
        # 真正的 OS 确认是 `execution_confirm` + 300 秒同步等待（清单 ⑧）。
        #
        # 现在它是一条 `skill_manage` Interaction，用户下一条消息走正常路由，
        # 模型从 `[Open Interactions]` 看到它自己判断。

        # ── 2. 这里【曾经】是 Skill 审计的路由劫持 ──────────────────────────
        # [2026-08-06 删除] 原来是
        #     if self._pending_skill: -> _handle_pending_skill()  （约 90 行 + 一个专用分类器）
        # 那条路把用户的下一条消息整条截走，先跑 `classify_pending_skill_intent`
        # （**每条消息多一次 API 往返**）分成 8 个标签，再按标签分支。
        #
        # 三个问题一起没了：
        #   ① 8 个标签里有一半（EXPLAIN / RISK / COMPLAINT / EXECUTE_BLOCKED）
        #      本质就是"正常回答用户" —— 主模型天然会做，不需要先分类（设计原则 3）；
        #   ② 模型看不见"有个 Skill 等着审"，用户说别的时它无从判断；
        #   ③ `_pending_skill` 是纯内存 dict，崩溃/关窗后那份待审代码不留痕迹。
        #
        # 现在审计是一条 `skill_audit` Interaction（artifact 钉版本，），
        # 用户的下一条消息走**完全正常的路由**，模型从 `[Open Interactions]` 看到它，
        # 自己决定调不调 `answer_open_interaction`。UI 按钮那条路一字未改。

        # ── 2.5 这里【曾经】是 Skill 澄清的路由劫持 ────────────────────────────
        # [2026-08-05 删除] 整段约 50 行没了，没有替代分支——
        # 澄清态现在是一条 Interaction，用户的下一条消息**走完全正常的路由**，
        # 由主决策模型看着 [Open Interactions] 决定调不调 answer_open_interaction。
        #
        # 被删掉的三样东西，各自是一个独立的 bug：
        #   ① `_CLARIFY_TIMEOUT = 600` —— 10 分钟没回应就静默作废。用户去开个会
        #      回来，答案就没人接了，而且**没有任何提示**说它作废了。
        #      新实现里澄清永不过期（deadline_at 留空），积压不是错误。
        #   ② `_abort_words` / `_NEW_REQUEST_SIGNALS` 两张关键词表 + 一条
        #      "长度>20 且不含 Skill 词就算新话题"的启发式。两个方向都会错：
        #      "删除那一列的空行" 命中"删除"被判成新请求；
        #      "帮我查一下今天的天气" 里的"查一下"同理。反过来，一句
        #      "那个文件在 D 盘" 只要超过 20 字又不含表里的词，也会被判成新话题。
        #   ③ **模型全程看不见这件事**。劫持发生在进模型之前，所以上面两类误判
        #      模型一次纠错机会都没有——它连"有个问题挂着"都不知道。
        #
        # ⚠️ 顺带修掉一个从来没被记录过的形状：旧代码在调 Explorer **之前**
        # 就把 `_pending_skill_clarification = None` 了。于是 Explorer 那一步
        # 只要失败（provider 报错、进程被杀），用户刚说的答案就彻底消失，
        # 只能让用户重说一遍。现在是先落盘 ANSWERED 再跑 Explorer，失败可重试。

        # ── 这里【曾经】是"OS 层软急停前置检查" ──────────────────────────────
        # 关键词硬匹配 + LLM 语义兜底两层，命中就置进程级 _aborted。整套软急停已删除
        # （原因见 core/os_layer/safety.py 模块头：有甩鼠标和 Ctrl+` 两种瞬发物理手段在，
        # 第三种没人用，而且它自己带着"一旦触发这个进程再也不能操作电脑"的坏性质）。
        #
        # 顺带修掉的 token 浪费：这段的进入条件是 hasattr(self, "_os_safety")，
        # 而 _os_safety 一旦因为某次 os_execute 被懒创建就永久存在——于是本进程
        # 之后【每一条】用户消息都要多跑一次 classify_os_abort_intent 的独立 LLM 往返。
        # v1.1 那轮删顶层分类器时漏掉了这一处。

        # 2.8 OS 定位失败后的简短纠正 → 把纠正并进 query，落到主 ReAct 循环续接。
        # 双循环合一后不再有独立 OS 循环，os_execute 始终在主循环里，模型看着
        # 对话历史（含上次定位失败）+ 用户纠正，自然重试，无需特殊路由。
        _os_followup = self._detect_os_locate_followup(query)
        if _os_followup:
            self._last_os_locate_failure = None
            logger.info(f"[Router] OS 定位失败后的简短纠正 → 合并 query 落主循环: {query}")
            query = (
                f"{_os_followup['original_query']}"
                f"（用户纠正/补充：{query}）"
            )
            # 不 return，继续往下走常规分类 → 主 ReAct 循环（含 os_execute）

        # ── 3a. 这里【曾经】是 SKILL_CREATE 关键词快路径 ────────────────────
        # [2026-08-06 删除] 整段约 45 行 + `_SKILL_CREATE_KEYWORDS`
        # 那张 13 条的关键词表一起没了。
        # （常量名写在这里是故意的 —— 早先的设计按名字引用它，有人 grep 时该找到这块墓碑，
        #   而不是一无所获然后以为自己记错了。）
        #
        # 它最早是分类器的补丁（SKILL_CREATE 阈值 0.75 偏高，混进 OS 相关词会被稀释）。
        # 但 v1.1 已经把 `classify_primary_intent` 整个删了，此后它保护的是一个
        # **不存在的分类器**。后来留着它的理由换成了"防模型把明确的建 Skill 请求劝退"。
        #
        # 删掉的两条实测证据（08-06，都不是推测）：
        #
        # **证据 1 —— 同一个功能有两种可见性。**
        # 「写一个skill 作用是提取dns」命中关键词 → **整个 ReAct 主循环被跳过**
        # → 没有任何 tool_use → 屏幕上没有工具卡片。
        # 紧接着「再写一个，作用是提取ip地址」没命中 → 走主循环 →
        # 正常出现 `加载能力: create_new_skill ✓`。
        # 用户那句话里有没有"写一个 skill"，决定了用户能不能看见 Nano 在做什么。
        # 违反了那条约定（新工具必须能被工具卡片显示、且正确出现在感知清单里）。
        #
        # **证据 2 —— 它一直在替元工具入口越界那个 bug 挡枪。**
        # 三份日志里"快路径入口从不越界、元工具入口每次都越界"，
        # 当时判断成"快路径运气好"。真实原因是它的 `messages` 只有 1 条 ——
        # 没有 `load_tools` 回执可污染。**所以那个 bug 才活了那么久没被定位。**
        #
        # 📌 判据：**一条绕过主流程的快路径，会同时绕过主流程的可见性与诊断。**
        # 省下的两次往返，代价是同一个功能有两条行为不同的路，
        # 而其中一条永远不产生可观测记录。
        #
        # ⚠️ 删它的前置条件已满足：`create_new_skill` 的感知行不再被截断到 28 字符
        # （见 `core/tools/builtin.py` 里 `create_new_skill` 的 awareness）。那是"防劝退"这个理由的正面解法 ——
        # 让模型看清工具是什么，而不是绕开它自己的判断。

        # 2.7 Skill 报错后的简短回应 → 直接走修复通道，绕过顶层分类器
        # 必须在 OS 急停和 SKILL_CREATE 强关键词之后检测，防止
        # "别动屏幕了" / "写一个skill..." 等语句被错误拦截到修复路由。
        # 短确认词（好的/可以/修一下）+ 显式修复动词 + 工具报错状态三条件同时满足才触发。
        _error_followup = self._detect_skill_error_followup(query)
        if _error_followup:
            _error_fix_target, _error_content = _error_followup
            self.memory.add_message("user", query)
            logger.info(f"[Router] 检测到 Skill 报错后的简短回应 → 走修复通道: {_error_fix_target}")
            _got_skill_preview = False
            async for step in self._generate_skill_update(
                query, _error_fix_target, base_guide, realtime_callback,
                error_context=_error_content
            ):
                if step.get("event") == "skill_preview":
                    _got_skill_preview = True
                yield step
            # 只有成功产出 skill_preview 才标记消费，失败时保留可重试
            if _got_skill_preview:
                if getattr(self, "_last_skill_error", None) and not self._last_skill_error.get("consumed"):
                    self._last_skill_error["consumed"] = True
            return

        # 3. 顶层语义分类【已删除 · 残骸清理 2026-08-04】
        # 原来每条消息先跑一次分类器判 NO_TOOLS/REACT 决定走不走 fast-path。实测两点：
        #   ① 分类器每条一次、~770 fresh token，且中转下这么小的前缀无法缓存（<4096 门槛）；
        #   ② fast-path 的 system 前缀（~2.4K）同样够不到缓存门槛，每条全价重发。
        # 合并做法：删分类器 + 删 fast-path，所有消息统一走 ReAct 主循环——它带核心工具、
        # 稳定前缀+工具 >4096 会命中缓存（0.1x），闲聊时模型按 ReAct 协议直接出话、不调工具。
        #
        # 那次只删了分类器本身，把 `primary_intent="DIRECT_ANSWER"` / `primary_conf=0.0`
        # 两个常量和一整套"防御性降级"判断留了下来。既然分类器已经不存在，
        # 那些判断永远进不去（`0.0 >= 0.55` 恒假），一并删除：
        #   - SKILL_CREATE / SKILL_UPDATE / SKILL_DELETE|DISABLE|ENABLE 三段降级分支
        #     （它们的意图已由 create_new_skill / update_existing_skill /
        #      manage_existing_skill 三个元工具在主循环里承担）
        #   - SKILL_OTHER_THRESHOLD / OS_TASK_THRESHOLD 两个只服务于上述分支的阈值
        #   - registered_skill_names —— **零引用，但每条消息都要跑一次
        #     `get_all_manifests()` 建一个 set**，纯浪费
        #   - _classifier_says_simple —— 恒为 False，在 or 链里等于不存在
        #
        # OS 也不再有前置硬路由：os_execute 始终注入主 ReAct 循环（见上方 include_os=True），
        # OS 任务和普通任务走同一条循环、同一套工具集。这消除了"OS 任务被分流到独立循环、
        # 够不到 wait_for/render_visual 等其它能力"的双循环漏洞
        # （0627 os_execute replay bug、够不到屏幕都源于此）。


        # 4. 常规路径
        self.memory.add_message("user", query)

        # ⭐ 用户发的图 → Nano 自存一份，引用落进刚写的这条 user 消息。
        #
        # ⚠️⚠️ **位置有两个硬约束，两个都踩过：**
        #   ① 必须在 `_last_user_msg.content = _parts`（把 content 换成带 base64
        #      的 list）**之前** —— 否则 `attach_user_images` 里的 `update_message`
        #      会把整张图的 base64 写进权威账本。
        #   ② 🔴 **必须在 `_image_note_request_block()` 之前** ——
        #      原来它排在 system_guide 组装之后，于是那一刻 `ui_images` 还是空的，
        #      `has_unsummarized_image()` 恒 False，**那段「顺手记一份摘要」的提示
        #      永远不会出现**。实测实测：图答对了，`image_summary` 却是 None。
        #      ⚠️ 而它**一声不响** —— 没有报错、没有告警，只是摘要永远不生成。
        #      📌 原来的测试钉住了约束 ①，然后在约束 ② 上栽了 ——
        #         **一条顺序断言只保护它写下的那个顺序。**（已补断言）
        # 📌 收口在 memory 那一侧，不在这里拆 base64：将来加粘贴通道 / 拖拽 /
        #    Skill 产图，都不用再想起这件事。
        if image_parts:
            self.memory.attach_user_images(image_parts)
            # ⚠️ 顺序要紧：**先** attach（登记 handle / 落盘），**再**决定
            #    pixels 发不发给主模型 —— 回看能力挂在 handle 上，
            #    绝不能因为"这个主模型看不了图"而丢掉它。
            if _main_model_is_blind():
                yield {"event": "thinking", "log": "正在用视觉模型识图...",
                       "status": "CORE_THINKING", "current_skill": None,
                       "rag_hit": False, "full_file_hit": False}
                image_parts = await self._seed_image_summary_via_vision(image_parts, query)

        # ⭐ [2026-08-22] 引用指向跟着这条消息落盘 —— 见 `attach_reply_quote`。
        # ⚠️ 读的是 `_reply_target_turn`（UI 按发送时移交过来的本轮快照），
        #    退回 `_reply_target` 兼容还没移交的路径 —— 与
        #    `_build_quoted_selection_block()` 读的是**同一个来源**。
        #    📌 展示给用户的那份和喂给模型的那份，必须来自同一个事实，
        #       否则总有一天它们会说两件事。
        try:
            _rq = ((getattr(self, "_reply_target_turn", None)
                    or getattr(self, "_reply_target", None) or {}).get("q") or "")
            if _rq:
                self.memory.attach_reply_quote(_rq)
        except Exception as _e_rq:
            logger.debug(f"[Reply] 引用落盘跳过: {_e_rq}")

        # ⭐⭐⭐ [无缝对话 · 协作式中断] 记下本轮开始时「收到过多少条用户消息」。
        #
        # 之后 ReAct 循环在**动作边界**比对这个基线 —— 变大了就说明用户插话了，
        # 于是**停止本轮**，让下一轮带着**两条消息**重新决策。
        #
        # 📌 **无缝对话的本质是「让模型在决定之前看到全部输入」**，
        #    不是「让两次输出看起来连续」—— 后者在第二条撤回第一条时必然矛盾
        #    （用户的反例：「现在几点？」→ thinking →「算了不要调用工具了」
        #      → 执行分段会先查了时间、再说「好那我不查了」）。
        #
        # ⚠️ **基线用计数器，不用「有没有事件」** —— 队列里早就存在的消息
        #    不该让刚开始的这一轮立刻自我中断。
        #    📌 用「事件」表达「有新东西」时必须定基线，否则它表达的是「曾经有过东西」。
        try:
            from core.runtime import inbox as _ib_seq
            self._turn_input_baseline = _ib_seq.submit_seq()
        except Exception:
            self._turn_input_baseline = None

        # ⭐⭐ **新一轮开始就清掉终止意图。**
        # ⚠️ 不清的后果：上一次的终止会把**下一轮也杀掉** ——
        #    用户点了停、然后重新问一句，结果那句话一进来就被上次的终止干掉，
        #    表现为「Nano 再也不回话了」。
        #    📌 一个「一次性的意图」必须有明确的清除点，
        #       否则它会从「这一次」悄悄变成「从此以后」。
        #       （形状与那个泄漏的忙标志同族：**没人负责清 = 永久生效**。）
        self.clear_stop()

        # Token hygiene: remove historical image/base64 blocks before building ReAct context.
        # Current-turn images are not in memory yet, so this does not affect current image understanding.
        # The placeholder keeps the fact that the user previously sent image(s), but drops the heavy payload.
        try:
            _compressed_imgs = self.memory.compress_image_blocks()
            if _compressed_imgs:
                logger.debug(f"[Memory] compressed {_compressed_imgs} historical image block(s) before ReAct context build")
        except Exception as _img_compact_err:
            logger.debug(f"[Memory] historical image compression skipped: {_img_compact_err}")

        # ⭐⭐ 同一个位置、同一条理由：把**已经读过的旧文件切片**换成占位符。
        #    这是「恒定上下文厚度」唯一的落地点 —— 那条状态机
        #    「读入原始数据 → 提炼进 scratchpad → **丢弃原始数据** → 带着它进入下一轮」。
        # 📌 没有它，`notes` 参数只是多存了一份东西，原文照样在上下文里堆着 ——
        #    **只做一半（写了 notes 却不丢旧切片），得到的是「原文照样堆着再加一份笔记」，比不做还贵。**
        # ⚠️ 与图片压缩挂在同一处不是巧合：两者是**同一件事的两个实例**
        #    （把历史里的重载荷换成占位符），所以它们的时机也必须一样：
        #    **在构建 ReAct 上下文之前，且在本轮内容进 memory 之前。**
        try:
            _compressed_reads = self.memory.compress_file_reads()
            if _compressed_reads:
                logger.debug(f"[A2] 压掉 {_compressed_reads} 段旧的文件切片（保留每份文件最后一段）")
        except Exception as _read_compact_err:
            logger.debug(f"[A2] 文件切片压缩跳过: {_read_compact_err}")

        # 🔴 **这里刻意不带 `model`**（2026-08-14 实测）：此刻 `used_model` 还是
        #    `"UNKNOWN"` —— 真实模型名要等 provider 回报、由 `realtime_callback` 填。
        #    带上去的结果就是监控卡「当前模型」在发送途中闪成 UNKNOWN，回复后才恢复。
        # 📌 **一个「我还不知道」被渲染成一个具体值，比不显示更糟** ——
        #    同监控卡那条（量不到显示 `--` 不是 0%）。
        #    ⚠️ 消费端也加了守卫（`app.py` 收到 UNKNOWN 一律忽略），因为带 model 的
        #    yield 点有几十处，漏一个就复现 —— 📌 **防御要放在收口处，不是每个发出点。**
        yield {"event": "thinking", "log": "正在初始化寻址...", "status": "CORE_THINKING", "current_skill": None, "rag_hit": False, "full_file_hit": False}

        # 快路径分流：简单任务不注入额外规则，节省 token
        _registered_count = len(self._skill_names(self.registry.get_all_manifests()))
        _CHAIN_WORDS = {"先", "再", "然后", "接着", "之后", "最后", "并且", "同时", "分别"}
        _is_short_simple = len(query.strip()) < 15 and not any(w in query for w in _CHAIN_WORDS)
        # 原本这里还有第三个条件 _classifier_says_simple（要求 primary_conf >= 0.6），
        # 分类器删掉后 primary_conf 恒为 0.0，它恒为 False，在 or 链里等于不存在。移除。
        _force_fast_path = (
            _registered_count < 1
            or _is_short_simple
        )
        system_guide = base_guide
        if not _force_fast_path:
            logger.debug(f"[Router] 慢路径: skills={_registered_count}, query_len={len(query.strip())}")

        # 未决交互：让模型看见"有个问题挂在那里"。
        # 放在最前面是有意的——它决定这一轮该不该走 answer_open_interaction，
        # 属于路由级信息，比 session_log/episodic 那些背景资料优先级高。
        _interaction_injection = self._build_open_interactions_injection()
        if _interaction_injection:
            system_guide = system_guide + _interaction_injection
        # ⭐ 选中文字的引用。**独立于未决交互那段** —— 没有待办时它也要出现，
        #    而那一段在没有待办时会直接返回空串。
        #    📌 两件事共用一个入口的话，「没有待办」会顺手把「用户指了一句话」也吃掉。
        _quote_injection = self._build_quoted_selection_injection()
        if _quote_injection:
            system_guide = system_guide + _quote_injection
        # ⭐ 进行中的「一件事」：让模型看见它在替什么东西负责。
        # ⚠️ 这是「边界由模型判」的**前提** —— 不给它看，`task_boundary` 就是个
        #    要它凭空猜的工具。同 「终止事实要传给模型」：
        #    📌 **要模型做判断，就得先让它看见判断所需的事实。**
        # ⭐ 紧跟未决交互之后是有意的：两段都是「有东西挂在你名下」的路由级信息，
        #    而 `task_boundary` 的可见性正是由这段话决定的。
        _work_injection = _rt_ongoing_work(self)
        if _work_injection:
            system_guide = system_guide + "\n\n" + _work_injection
        # ⭐ 还在跑的后台任务。
        # ⚠️ **必须现在就接上，不许「先写好等以后用」** ——
        #    核时刚抓到 `mcp_client.awareness_lines` 的教训：
        #    它写得完全正确、docstring 还写明「给 xxx 用」，而**零调用方**，
        #    于是「MCP 按需加载」这件事看起来做了、实际一直没做。
        # 📌 **一个写好但没人调的函数，比没写更坏** ——
        #    没写时缺口是可见的，写了不接时缺口**看起来已经补上了**。
        _bg_injection = _rt_background_jobs(self)
        if _bg_injection:
            system_guide = system_guide + "\n\n" + _bg_injection
        # ⭐ [A/B] 授权与能力的现状。⚠️ **必须现在就接上，不许「先写好等以后用」**
        #    —— `mcp_client.awareness_lines()` 那笔账的形状（写好、零调用方、
        #    于是缺口看起来已经补上了）。
        _auth_injection = _rt_authorization_state(self)
        if _auth_injection:
            system_guide = system_guide + "\n\n" + _auth_injection
        # 上一个没能部署的 Skill + 它的校验报错（2026-08-05 实测）。
        # 放动态段而不是 memory：memory 会被 max_turns=10 截断，
        # 而用户往往隔很多轮才说"上次写的校验报错，重新写"。
        _audit_injection = self._build_audit_failure_injection()
        if _audit_injection:
            system_guide = system_guide + _audit_injection
        # 注入 session_log,让模型知道本次会话做过什么
        _session_injection = self._build_session_log_injection()
        if _session_injection:
            system_guide = system_guide + _session_injection
        # Episodic: 注入跨会话历史摘要
        _episodic_injection = self._build_episodic_injection()
        if _episodic_injection:
            system_guide = system_guide + _episodic_injection
        # Ambient Memory：注入"当前工作现场"，让 Nano 默认知道用户刚才在干嘛
        _ambient_injection = self._build_ambient_injection()
        if _ambient_injection:
            system_guide = system_guide + _ambient_injection
        # ⭐ [2026-08-25] 窗口形态（mini / full）—— 见 `_build_window_mode_injection`。
        #    ⚠️ **必须现在就接上**，不许「先写好等以后用」（`awareness_lines()` 那笔账）。
        # ⭐⭐⭐ 记忆摘要无条件注入 —— 见 `_build_memory_injection`。
        #    📌 这一行就是 用户那句「cc 每次都注入了摘要」的落地。
        system_guide = system_guide + self._build_memory_injection()
        system_guide = system_guide + self._build_window_mode_injection()
        # 情感系统：Mood 染语气注入被动路径（设计 /）。落在缓存哨兵之后的动态段，
        # 不污染稳定前缀缓存。情感只染语气，绝不降质量、不碰功能（铁律）。
        try:
            from core.proactive.intel.affect import get_affect as _get_affect
            _tone = _get_affect().tone_hint()
            # 固定说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发变化的 tone 值。
            system_guide = system_guide + f"\n\n[Tone] Current tone: {_tone}."
        except Exception:
            pass

        # ── 能力健康 + 系统事件注入（两者都落在缓存哨兵之后的动态段）────────
        # 为什么在动态段而不是稳定前缀：正常情况下这两块都是空串，一个字符都不加；
        # 出故障时才有内容，而那时 manifest 也变了、缓存本来就得重建。放稳定前缀会
        # 让"降级"（不摘工具、不改 manifest）白白打穿缓存。
        #
        # 两者分工：能力健康记【状态】（坏着就一直在），系统事件记【事件】
        # （刚才发生过什么）。一个防模型去调坏工具，一个让用户追问"刚才怎么了"能答上来。
        # 都不写进 MemoryManager —— 后台线程在 tool_calls/tool_results 之间插一条
        # assistant 会撕裂工具事务，代价是当轮硬崩 + 已执行结果被回滚吃掉 + 下一轮 400。
        try:
            from core.health import get_health, get_system_events
            _cap_notice = get_health().render_capability_notice()
            if _cap_notice:
                system_guide = system_guide + _cap_notice
            _sys_events = get_system_events().render()
            if _sys_events:
                system_guide = system_guide + _sys_events
        except Exception as _h_err:
            logger.debug(f"[Health] 注入跳过: {_h_err}")

        # ⭐ 运行作用域 —— 两块，顺序固定，都落在缓存哨兵之后的动态段。
        #
        #   Health current block
        #   RecentSystemEvents          ← 上面那段
        #   [Runtime Restarted]         ← ② 一次性，消费后消失
        #   [Execution Scope]           ← ④ 每轮
        #
        # ⚠️ ② **不塞进 `RecentSystemEvents`**：它的表头写着「除非用户问起否则别用」，
        #    而重启提示恰恰是"没问也必须影响判断"。理由详见 `runtime/identity.py`。
        # ⚠️ ④ 刻意**不重复工具列表** —— 当前 API manifest 已经是工具的权威来源，
        #    再列一遍就是第二份名单。它只说"规则"，所以在一个 turn 内是恒定的，
        #    和 Health 同一处注入即可。
        try:
            from core.runtime import identity as _rt_ident
            _restart_notice = _rt_ident.pending_restart_notice()
            if _restart_notice:
                system_guide = system_guide + _restart_notice
            system_guide = system_guide + self._execution_scope_block()
            system_guide = system_guide + self._image_note_request_block()
            # ⭐⭐⭐ L3 索引条目 —— **无条件注入**，不是"需要时再检索"。
            #
            # 🔴 这一条解掉的是 用户当年那个悖论：
            #    「大模型真的忘记某个东西之后，它不就把『我记过这个东西、
            #      我应该去看笔记』这件事也忘了吗？」
            # ⭐ 参照 `MEMORY.md`：**索引不是被回忆起来的，是被塞进来的。**
            #    只做召回端不够 —— 等发现该记的时候，可能已经没得记了。
            system_guide = system_guide + self._l3_index_block()
            # 上下文压力对模型可见。⚠️ 低于高水位返回空串（见 budget.py 的纪律）。
            try:
                from core.context.budget import pressure_block as _pb
                system_guide = system_guide + _pb(self.provider.target_model)
            except Exception:
                pass
        except Exception as _sc_err:
            logger.debug(f"[F6] 作用域注入跳过: {_sc_err}")

        context = self._build_pipeline_context()

        # 主决策特有的"用户上传文件提示 / 图片"注入——这两个是
        # 用户当轮输入的一部分，只在主决策入口有。
        # 注：只追加到发给模型的 context 副本，不污染 self.memory（原始 query
        # 在这之前已干净存好）。
        import copy
        context = copy.deepcopy(context)
        if temp_file_hint or image_parts:
            for i in range(len(context) - 1, -1, -1):
                item = context[i]
                role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
                if role == "user" and isinstance(item, dict):
                    parts = list(item.get("parts", []))
                    if temp_file_hint:
                        parts.append({"text": temp_file_hint})
                    if image_parts:
                        parts.extend(image_parts)
                    item["parts"] = parts
                    break

        # ── 所有消息统一走 ReAct 主循环（fast-path 已于 0703 合并删除）──────────
        # 闲聊/无工具需求的消息：模型按 ReAct 协议（见 _react_loop_prompt "直接回答"
        # + "闲聊不调工具"约束）第一轮直接出话、不产生 tool_use，渲染与旧 fast-path 一致；
        # 需要工具时正常进多轮。稳定前缀+核心工具 >4096 触发缓存，成本反而更低。

        # ── ReAct 主循环替代旧"一次决策+巨型分支树" ─────────────────────────
        # _run_react_loop 内部自行调用 _stream_decision_core，多轮迭代直到
        # 模型输出最终文字答案或达到最大轮次。
        # temp_file_hint / image_parts 的注入：在第一轮 _stream_decision_core
        # 调用时，context 已经含有它们（通过上面的 context = copy.deepcopy + 注入）。
        # _run_react_loop 第一轮调用 _build_pipeline_context() 会拿到注入后的
        # memory（因为 add_message("user", query) 已经在上面执行），
        # 但 temp_file_hint 不在 memory 里——需要显式传给第一轮。
        # 解决方式：把 temp_file_hint/image_parts 注入到刚写入 memory 的 user 消息上。
        # （这里直接修改 memory.storage 最后一条——user 消息刚被写入，是安全的）
        if temp_file_hint or image_parts:
            _last_user_msg = self.memory.storage[-1] if self.memory.storage else None
            if _last_user_msg is not None and _last_user_msg.role == "user":
                # 构造 list 格式 content，provider/to_dict 会按 Anthropic 多模态格式序列化
                _parts: list = [{"type": "text", "text": _last_user_msg.content or ""}]
                if temp_file_hint:
                    _parts.append({"type": "text", "text": temp_file_hint})
                if image_parts:
                    _parts.extend(image_parts)
                _last_user_msg.content = _parts

        try:
            async for _react_ev in self._run_react_loop(
                tools_manifest=list(self._core_manifest),   # 核心常驻 + load_tools；其余按需加载
                system_guide=system_guide,
                base_guide=base_guide,
                realtime_callback=realtime_callback,
                event_queue=event_queue,
            ):
                yield _react_ev
        except RuntimeError as fatal_err:
            if "API 调用链路全线熔断" in str(fatal_err):
                self._clean_damaged_memory()
                yield {"event": "sys_error", "content": "🚨【熔断】总线API全线枯竭！",
                       "status": "CRITICAL_SYSTEM_HALT", "model": "TOTAL_CRASH",
                       "skills_active": False, "log": "CRITICAL: 底层 API 链路彻底枯竭",
                       "current_skill": None}
                return
            raise fatal_err
        except Exception as core_err:
            logger.critical(f"[ReAct] 主循环异常: {core_err}\n{traceback.format_exc()}")
            self._clean_damaged_memory()
            yield self._get_generic_error_payload(core_err, current_running_skill)
            return
        finally:
            # Always compress image payloads after the turn attempt.
            # This also runs when the API fails before normal completion.
            # User text remains, and the placeholder preserves that images were sent.
            try:
                _compressed_imgs = self.memory.compress_image_blocks()
                if _compressed_imgs:
                    logger.debug(f"[Memory] compressed {_compressed_imgs} image block(s) after ReAct turn")
            except Exception as _img_compact_err:
                logger.debug(f"[Memory] post-turn image compression skipped: {_img_compact_err}")

            # ⭐ L0→L1：把老交换的工具结果换成占位符。
            #
            # ⚠️⚠️ **默认关闭**（`data/model_config.json` 的 `_settings.ladder_enabled`）。
            #    这是第一个**真的从模型上下文里拿走东西**的动作，必须由 用户显式打开。
            #    📌 **引入一个机制和启用一个机制是两件事** —— 一起做，出了问题
            #       分不清是机制的锅还是接线的锅（同「立架子和搬家具是两件事」）。
            #
            # ⚠️ 放在**这一轮结束之后**（和图片压缩同一个 `finally`）：
            #    衰减只碰 **closed exchange**，而"当前这一轮"到这里才算关闭。
            #    📌 在轮内降级 = 可能在 tool_result 还没回来时就把它换成占位符。
            try:
                from core.models import ladder_enabled as _ladder_on
                if _ladder_on():
                    from core.context.decay import run_l0_to_l1 as _run_l1
                    from core.context.decay_store import DecayStore as _DS
                    from core.runtime.kernel import get_kernel as _gk
                    _sid = self.memory.conversation_session_id
                    if _sid:
                        _dstore = _DS(_gk().store)
                        _run_l1(self.memory, _dstore, _sid, self.provider.target_model)
                        # ⚠️ L1→L2 **过提炼器**（要花钱、要等秒级），所以排在 L0→L1
                        #    之后：先把免费的那一档降完，可能就不需要走这一档了。
                        #    📌 **贵的动作永远排在便宜的动作后面** ——
                        #       不然你会为一件本来不必做的事付钱。
                        from core.context.decay import run_l1_to_l2 as _run_l2
                        await _run_l2(self.memory, _dstore, self.provider, _sid,
                                      self.provider.target_model)
                        # L2→L3：交接给语义记忆 + 生成索引条目 + 改档。
                        # ⚠️ 不过 LLM（结论行已经在库里），但它是**唯一用户有感**的箭头。
                        from core.context.decay import run_l2_to_l3 as _run_l3
                        _run_l3(self.memory, _dstore, _sid, self.provider.target_model)
                        # L3→L4：索引条目过期。⚠️ **不删语义记忆** ——
                        # 📌 遗忘 = 不再自动想起，不等于抹掉。
                        from core.context.decay import run_l3_to_l4 as _run_l4
                        _run_l4(self.memory, _dstore, _sid, self.provider.target_model)
                        # 🔴🔴 **最后把投影按权威重建一次** —— 四档只改
                        #    `exchange_decay`（权威），**没有一个会去动 `storage`**。
                        #    不做这一步的后果/抓到的最大那条）：
                        #
                        #        exchange_decay 说：这段已经 L2 / 已经移出去了
                        #        storage（真正发给模型的）说：原文还背在身上
                        #
                        #    —— 日志说省了、其实没省；L3 更怪：**要等重启才真的忘掉**。
                        # ⭐ 用的就是重启那条路径的同一个函数，所以 live 与 hydrate
                        #    **不可能漂开**（📌 各写一遍的话，它们只在"我两次都想对了"
                        #    的前提下相等）。
                        from core.context.decay import rebuild_projection as _reproj
                        _reproj(self.memory, _dstore, _sid)
                        # ⚠️ 投影已经服从权威了，**这时候才轮到 UI**。
                        #    📌 UI 与模型必须看到同一件事：模型侧移出去了而聊天区还显示，
                        #       用户就会接着问"你刚才说的那个"。
                        _cb = getattr(self, "_on_decay_applied", None)
                        if callable(_cb):
                            try:
                                _cb()
                            except Exception as _ui_err:
                                logger.debug(f"[Decay] 通知 UI 同步失败: {_ui_err}")
            except Exception as _decay_err:
                # ⚠️ 治理层的故障**绝不许**把对话搞挂 —— 不降级只是上下文厚一点。
                logger.warning(f"[Decay] L0→L1 跳过（不影响对话）: {_decay_err}")

        logger.debug(f"[ReAct] 常规路径完成")

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

