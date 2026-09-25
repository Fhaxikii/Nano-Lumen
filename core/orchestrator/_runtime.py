# core/orchestrator/_runtime.py
"""Orchestrator 对运行时内核（core.runtime）的封装：影子 span、租约、等待、交互（Interaction）的登记与关闭，以及当前代理的归属标签。"""

import contextvars
import re
from loguru import logger


# ══════════════════════════════════════════════════════════════════════════
# ToolBatchSpan 的 shadow 接线
# ══════════════════════════════════════════════════════════════════════════
# 铁律：**shadow 绝不影响真实路径。** 所以这几个包装器把 core.runtime 的 import
# 也放在函数体内并吞掉一切异常 —— 哪怕整个 runtime 包出问题（import 失败、库锁了、
# 磁盘满了），ReAct 主循环的行为一个字节都不变。
#
# 三步迁移观测期的定义：旧字段（`_active_tool_batch_open`）权威，内核只镜像并校验，
# **严禁双写**。所以下面从不写那个字段，只把它的值抄下来对答案。
#
# ⚠️ 2A shadow 已于 2026-08-05 收工（8/8 覆盖、唯一分歧已定性），
# 那份覆盖表已经收工并删除，结论留在本文件下面几段注释里。


class _RT_PATH:
    """覆盖表行号的本地别名，避免在主循环里 import 那个枚举。"""
    NORMAL = "p1_normal"
    ERROR_400 = "p3_error_400"
    OS_EARLY_RETURN = "p4_os_early_ret"
    EXIT_MULTI = "p5_exit_multi"
    BATCH_EXCEPTION = "p6_exception"
    GENERATOR_DROPPED = "p7_gen_dropped"


def _rt_shadow_prepare(orch, round_idx: int, names: list, path_tag: str):
    try:
        from core.runtime import toolbatch as _tb
        return _tb.shadow_prepare(
            getattr(orch, "_rt_turn_id", "") or "rtturn_unknown",
            round_idx, list(names), path_tag,
        )
    except Exception:
        return None


def _rt_shadow_open(orch, span_id, call_ids: list) -> None:
    """把 Span 切到 OPEN。**这一刻起 `_active_tool_batch_open` 派生值变 True。**

    ⚠️ 观测期这里的注释是"必须在 `_active_tool_batch_open = True` 之后调 ——
    这一刻才是旧标志的正确采样点"。切写那一步之后**反过来了**：
    没有旧字段可采样，而这次调用**本身**就是那个状态的来源。
    所以它必须在 `memory.add_tool_calls()` 成功之后调 —— 语义是
    "tool_calls 已经写进 memory 了"，早调一步就成了谎。
    """
    try:
        from core.runtime import toolbatch as _tb
        # legacy_flag=None：没有旧字段可对答案（见 shadow_commit 那处说明）
        _tb.shadow_open(span_id, [str(x) for x in call_ids], None)
        orch._rt_open_span = span_id          # 记住它，供各清理点 abort
    except Exception:
        pass


def _rt_shadow_commit(orch, span_id, result_ids: list, path_tag: str) -> None:
    try:
        from core.runtime import toolbatch as _tb
        # 不再传 legacy_flag：旧字段已删除，`_active_tool_batch_open`
        # 现在是从 Span 派生的属性 —— 把它抄下来"对答案"等于**拿内核跟自己比**，
        # 永远一致，是纯噪音。shadow 对答案机制随观测期一起收工（8/8 覆盖已达成）。
        _tb.shadow_commit(span_id, [str(x) for x in result_ids], path_tag, None)
    except Exception:
        pass
    finally:
        # 正常收尾：无论 shadow 记账成功与否都要摘掉，否则下一个清理点会去 abort
        # 一个已经 COMMITTED 的 span（幂等所以无害，但会污染观测）。
        try:
            if getattr(orch, "_rt_open_span", None) == span_id:
                orch._rt_open_span = None
        except Exception:
            pass


def _rt_abort_open_span(orch, reason: str, path_tag: str = "") -> None:
    """在【旧标志被清理】的任何地方调它，把还开着的 span 一起收掉。

    ⚠️ 这是实测 shadow 第一天抓到的缺口：`_clean_damaged_memory` 会把旧标志置回 False
    （本文件里那个第二清理点，早先没记下来），而 span 留在 OPEN，
    于是下一轮 sweep 把它误报成"批次中途抛异常"。
    **旧标志被清到哪里，span 就要跟到哪里** —— 否则 shadow 比的是两个不同时刻的状态。
    """
    span_id = getattr(orch, "_rt_open_span", None)
    if not span_id:
        return
    try:
        from core.runtime import toolbatch as _tb
        # 同上，不再传 legacy_flag。
        _tb.shadow_abort(span_id, reason, None, path_tag)
    except Exception:
        pass
    finally:
        try:
            orch._rt_open_span = None
        except Exception:
            pass


def _rt_lease_acquire(orch, reason: str):
    """Nano 开始操作电脑前拿活动租约。拿不到返回 `None`。

    ⚠️⚠️ **切读之后这个返回值有意义了，调用方必须看。**
    观测期它纯观测，失败返回 None 也无所谓；现在 `None` 表示
    **机器在别人（用户）手里，这次不该动手** —— 忽略它就等于把被动挂起废掉。
    """
    try:
        from core.runtime import oslease as _ol
        return _ol.acquire_activity(reason)
    except Exception as e:
        # 只有"连模块都 import 不进来"这类才会到这儿；oslease 内部已自行吞过一层。
        logger.warning(f"[OSLease] 拿租约调用异常，按拿不到处理: {e}")
        return None


def _rt_lease_heartbeat(orch) -> None:
    """OS 每走一步续一次期。

    ⚠️ 切读之后不续期的代价升级了：租约一过期 `current_activity()` 立刻不认它，
    canary 会在 GUI 任务跑到一半时跳出来抢前台焦点。观测期这只是"报告不准"。
    """
    try:
        from core.runtime import oslease as _ol
        _ol.heartbeat_activity(getattr(orch, "_rt_os_lease", None))
    except Exception as e:
        logger.debug(f"[OSLease] 心跳失败（忽略）: {e}")


def _rt_lease_release(orch) -> None:
    try:
        from core.runtime import oslease as _ol
        _ol.release_activity(getattr(orch, "_rt_os_lease", None))
        orch._rt_os_lease = None
    except Exception as e:
        logger.debug(f"[OSLease] 归还租约失败（忽略）: {e}")


# ⚠️ `_rt_lease_compare_busy` 已于切写那一步删除（2026-08-08）。
# 观测期的对答案任务早已完成（量出泄漏频率 + 证实止血有效），而后续改动把租约改成
# **一整段 GUI 操作**的粒度后，旧 bool 仍是**单步**粒度 ——
# 📌 **两个寿命不同的东西之间的"分歧"不是缺陷，是设计**，继续对答案只会造假阳性。
# 📌 **shadow 的对答案，只在两边建模同一个粒度时才有意义。**
def _rt_machine_is_free() -> bool:
    """这台电脑闲着吗（谁在开都算忙，包括 Nano 自己）。

    ⚠️⚠️ **不要换成 `nano_may_touch_os()`** —— 那个在"Nano 自己持有"时返回 True，
    用它做后台自检的闸门，等于允许 canary 在 GUI 自动化跑到一半时去抢前台焦点，
    正是 `canary.py` 设计约束 ② 明令禁止的。两个函数回答的是不同的问题，
    对照表见 `oslease.machine_is_free` 的文档。

    ⚠️ **fail-safe 方向是"不闲"** —— 读不到状态时宁可跳过一次自检。
    """
    try:
        from core.runtime import oslease as _ol
        from core.runtime.kernel import get_kernel
        return _ol.machine_is_free(get_kernel())
    except Exception as e:
        logger.warning(f"[OSLease] 读机器占用状态失败，本次按「忙」跳过自检: {e}")
        return False


def _rt_wait_open(*, reason: str, wake_on: list, timer_seconds=None,
                  bg_ref=None, intent="condition_recheck") -> "object | None":
    """登记一条等待 —— **唯一写入口**，直接进内核。

    ⭐ 取代了切读期那套「写旧库 + 镜像到新库」。切写之后直接返回
       完整 `WaitRecord`，调用方不再通过六态折两态的兼容投影。
    """
    try:
        from core.runtime import waitcond as _wc
        return _wc.open_wait(reason=reason, wake_on=list(wake_on or []),
                              timer_seconds=timer_seconds, bg_ref=bg_ref,
                              intent=intent)
    except Exception as e:
        logger.error(f"[WaitC] 登记等待失败: {e}")
        return None


def _rt_shadow_note(path_tag: str, detail: str = "", diverged: bool = False) -> None:
    try:
        from core.runtime import toolbatch as _tb
        _tb.shadow_note_path(path_tag, detail, diverged)
    except Exception:
        pass


def _rt_has_open_batch(orch) -> tuple[bool, bool]:
    """从 ToolBatchSpan 派生"本轮有没有未完成的工具批次"。

    返回 `(有没有, 可不可信)`。这是 `_active_tool_batch_open` 从**旧字段权威**
    切成**内核权威**的那一步（2A 三步迁移的切读那一步）。

    ⚠️ **fail-safe 方向是 False** —— 理由见 `toolbatch.has_open_span()` 的 docstring：
    错成 True 会删掉合法历史工具对，错成 False 只是留个坏 pair（下轮 400 能修）。
    """
    try:
        from core.runtime import toolbatch as _tb
        return _tb.has_open_span(getattr(orch, "_rt_turn_id", "") or "")
    except Exception as e:
        logger.error(f"[Runtime] 派生 batch 状态失败（按 False 处理）: {e}")
        return False, False


def _rt_sweep_stale_spans(orch, legacy_flag_before_reset) -> None:
    try:
        from core.runtime import toolbatch as _tb
        # ⚠️ **不要 `bool(...)`** —— 那会把 `None` 压成 `False`。
        # 两者含义完全不同：`False` = "旧字段说没有未完成批次"（可对答案，
        # 而且此刻它应该是 True，所以是分歧）；`None` = "旧字段已删除，没得对"。
        # 之前这里写的是 `bool(...)`，于是切写那一步传 None 之后每一轮 sweep
        # 都被记成分歧 —— 一个只存在于观测层的假阳性。
        _tb.sweep_stale_spans(
            getattr(orch, "_rt_turn_id", "") or "rtturn_unknown",
            None if legacy_flag_before_reset is None else bool(legacy_flag_before_reset))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Interaction 接线
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 这一组与上面 2A 那组**纪律相反**，不要照抄。
#
# 2A 是 shadow：旧字段权威，内核只在旁边记账，所以那几个包装器吞掉一切异常是对的。
# 早先是**直接切换**：内核就是权威，没有旧字段兜底。于是分成两类：
#
#     读路径（建动态段）   吞异常 → 最坏是模型这轮看不见待办，下一轮还能看见
#     写路径（开 / 回答）  **不许吞** → 吞掉就等于回到"答案被吞掉"那条老 bug
#
# 尤其 `_rt_answer_interaction`：它失败时必须让上层告诉用户"没记下来，请再说一次"，
# 而不是假装记下了然后继续跑 Explorer —— 后者正是这整个阶段要消灭的形状。
#
# ═══ 为什么澄清态这条不做 shadow（对 2A 三步迁移的有意偏离）═══
#
# 三步迁移（旧权威 → 内核权威 → 删旧）的前提是**两套实现能对答案**。
# 但这里迁的不是一个状态字段，是一条**路由决策**：
#     旧：关键词表判断"这句话算不算回答"（`_NEW_REQUEST_SIGNALS` + 长度>20 启发式）
#     新：模型判断（调不调 `answer_open_interaction`）
# 两者对同一句话会给出不同走向，且**只能有一条真的执行**——没法并行跑完再比。
# 硬做 shadow 只能比"如果走另一条会怎样"的推测，不是事实。
#
# 换来的安全边际是别的：① 这是冷路径（只有 Explorer 提问后才存在）；
# ② 它不参与任何热循环不变量；③ 旧实现本身就有 10 分钟超时会静默作废，
# 也就是说"这条路径偶尔失效"是它的既有行为，不是新引入的风险。


# ══════════════════════════════════════════════════════════════════════════
# 「这条执行链是谁的」—— 授权弹窗要靠它区分 main agent 和 Subagent
# ══════════════════════════════════════════════════════════════════════════
#
# ⭐ 用 `ContextVar` 而不是实例属性：Subagent跑在**自己的 asyncio.Task 里**，
#    `create_task` 在建任务那一刻复制上下文，之后两边各自独立 ——
#    于是「我是 Subagent」这件事天然只作用在它那一支上，main agent 照旧是空。
#    📌 与 `usage._turn_ctx` / `_agent_ctx` 同一个机制、同一个理由：
#       **归属跟着「谁发起的」走。** 实例属性做不到这件事（并发时互相串味），
#       而参数透传要求每一层都记得传 —— 那等于把它交给下一个人的记性。
_agent_scope_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "nano_agent_scope", default="")


def current_agent_label() -> str:
    """此刻这条执行链属于哪个 Subagent。**main agent 是空串。**

    ⚠️ 空串必须真的是空串：授权弹窗按它决定要不要画那行来源标识，
       而一个「看起来像 Subagent 其实是 main agent」的标识比不画更坏。
    """
    try:
        return _agent_scope_ctx.get("") or ""
    except Exception:
        return ""


def _rt_ongoing_work(orch) -> str:
    """给模型看的「你手上有哪些一件事在进行」。没有就空串。

    ⚠️ **空串必须真的是空串** —— 一个字符都不许加。
       没有在进行的事时，`task_boundary` 也不注入（见 `_rt_has_live_work`），
       这两处必须**同时**为空，否则会出现「有工具没事实」或「有事实没工具」
       两种半截状态，而模型在半截状态下会开始猜。
       📌 **一个工具和它的事实来源，必须由同一个条件控制。**
    """
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import task as _tk
        return _tk.conversation_tasks_for_model(get_kernel()) or ""
    except Exception:
        return ""


def _rt_background_jobs(orch) -> str:
    """还在跑的后台任务，给模型看。没有就空串。

    ⚠️ 与 `_rt_ongoing_work` 是**两件事，不许合并**：
       · 「一件事」（对话类 Task）—— 模型要**判断它结不结束**（有 `task_boundary`）
       · 「后台任务」—— 模型**不判断它的生死**，它只需要知道「别等」+
         「结束一件事不会停掉它们」
       📌 **一段注入的措辞取决于「要模型拿它做什么决定」** ——
          两段都叫「你手上有什么」，但一段要它表态、一段明确要它别管。
    """
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import task as _tk
        return _tk.background_jobs_for_model(get_kernel()) or ""
    except Exception:
        return ""


# ⭐⭐ **哪些工具会撞上 OS 能力闸** —— 注入里要点名它们。
#
# 🔴 实测 2026-08-20：「工作区写入」关着时，Subagent 如实报告了；而 **main agent 不信它**，
#    原话「Subagent 的报告有问题——它说「工作区写入」禁用了，但我用 `edit_file`
#    可以直接编辑文件」，然后自己又去读了一遍文件、又要了一次授权。
#    📌 **注入告诉了它「哪个开关关了」，没告诉它「这会让你的哪些工具用不了」** ——
#       它手里那个工具叫 `edit_file`，关掉的东西叫「工作区写入」，
#       两个名字之间没有任何东西把它们连起来，于是它判断Subagent搞错了。
#    ⚠️ 代价不只是多一轮：它**推翻了一份正确的报告**，而那会教它以后也别信。
#
# ⚠️ 这张表**必须与「真的会走 OS 层的 handler」一一对应**，
#    而那是可以从代码里数出来的（谁调用了 `_execute_dsl_step`）——
#    `tests/cases/t_os_capability_gate.py` 用 AST 数一遍并比对，多一个少一个都会红。
#    📌 一张手写的表，只有在有东西替你数它的时候才不会过期。
_OS_BACKED_TOOLS: "dict[str, tuple]" = {
    # `os_execute` 覆盖全部 action —— 空元组表示"看这次调的是哪个 action"
    "os_execute": (),
    # `edit_file` 自己不写盘，它算完新内容后**穿过 `file_write` 那条路**
    "edit_file": ("file_write",),
    # OS 类 Skill 的执行计划也走同一层（`_run_os_skill_plan_loop`），
    # 但它不是一个模型可见的工具名 —— 用一句话代替
    "（OS 类 Skill 的步骤）": (),
}


def _rt_authorization_state(orch) -> str:
    """「这台电脑上，你要动手时会遇到什么」—— 给模型的**事实**，不是剧本。

    两件互相独立的事，必须分开说：
      ① **能力开放到哪一档**（设置 → OS 权限那 6 个开关）
      ② **要不要逐个授权**（Auto）

    ⭐⭐ 2026-08-20 定的语义，两者是**上下游**：
         能力未开放 → 连"要不要授权"这个问题都不该被问到（**auto 救不了**）
         能力开放了 → 才轮到 Auto 决定要不要逐个问
       📌 把它们混成一句话，模型就会用"反正开了 auto"去解释一次能力被关的失败。

    ⚠️ 只在**偏离默认**时说话：全开 + 非 auto（= 出厂状态）时返回空串。
       📌 与 压力段同一条纪律：**低于高水位一个字都不说** ——
          一段每轮都在的注入会被模型学会忽略，恰好毁掉"响亮"这件事本身。
    ⚠️ 给的是**状态**，不是「请你怎么表现」——
       📌 同故障卡片那条已验证的机制：给状态，不给剧本。
    """
    try:
        from core.os_layer import dsl as _dsl_a
        _perms = _dsl_a.load_permissions()
        _off = [k for k in _dsl_a.PERMISSION_LABELS if not _perms.get(k, False)]
        _auto = _dsl_a.auto_authorization_on()
    except Exception:
        return ""
    if not _off and not _auto:
        return ""
    _lines = ["[Authorization state - facts about this computer right now]"]
    if _off:
        _names = "、".join(f"「{_dsl_a.PERMISSION_LABELS[k]}」" for k in _off)
        _lines.append(
            f"- The user has turned OFF these capabilities: {_names} "
            f"({', '.join(_off)}). Actions that need them will not run at all - "
            f"this is not a permission prompt you can pass, and Auto does not "
            f"override it. Only the user can re-enable them, in 设置 -> OS 权限. "
            f"If you hit one, say so plainly and name the switch."
        )
        # ⭐⭐⭐ **点名受影响的工具。** 见 `_OS_BACKED_TOOLS` 上方那段留痕：
        #    只说开关名字，模型连不上"我手里这个工具会不会受影响"。
        _hit_tools, _hit_actions = set(), []
        for _a in _dsl_a.ALL_ACTION_NAMES:
            if not (_dsl_a.required_permissions(_a, _dsl_a.action_floor(_a))
                    & set(_off)):
                continue
            _hit_actions.append(_a)
            for _t, _acts in _OS_BACKED_TOOLS.items():
                if not _acts or _a in _acts:
                    _hit_tools.add(_t)
        if _hit_tools:
            _lines.append(
                f"  Concretely, this blocks: {', '.join(sorted(_hit_tools))}"
                f" (for the affected actions: {', '.join(_hit_actions[:8])}"
                f"{' …' if len(_hit_actions) > 8 else ''})."
                f" If a sub-agent reports one of these as blocked, it is telling "
                f"you the truth - do NOT retry it yourself with a different tool."
            )
    if _auto:
        _lines.append(
            "- Auto is ON: for capabilities that ARE enabled, you will not be "
            "asked to confirm each action - authorization is your own call. "
            "Do not tell the user you are 'waiting for their approval'; you are not. "
            "Judge each action as if you were the one accountable for it."
        )
    return "\n".join(_lines)


def _rt_has_live_work(orch) -> bool:
    """现在有没有一件「在进行的事」（对话类 Task）。

    ⚠️ **fail-safe 方向是「没有」** —— 读不出来就不注入那个工具。
       反过来错的代价是**模型去结束一件不存在的事，然后向用户宣布做完了**，
       而那是个假事实；而少注入一次只是这一轮它没法宣布结束（下一轮还有机会）。
       📌 fail-safe 要朝「少说一句」错，不朝「多断言一件事」错。
    """
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import task as _tk
        # ⭐⭐ **问的就是注入那一句话在不在**，不另写一份等价判断。
        #    📌 一个工具和它的事实来源，必须由同一个条件控制。
        #    🔴 旧写法是 `current_conversation_task() is not None` ——
        #       ④ 之后它在**全部被搁置**时会判 False，
        #       于是模型恰恰在唯一需要 `resume` 的时候拿不到它。
        return _tk.has_live_conversation_work(get_kernel())
    except Exception:
        return False


def _rt_ia_owner(mode: str) -> "str | None":
    """一条 Interaction 该归给谁 —— **由 `mode` 决定，不由调用点决定。**

    · `DEFERRED` —— 定义就是**跨轮待办**（审计连 deadline 都不设，：
      「用户可能真想把代码放几天再看」）。它有权让一件事诞生：
      **有个跨天的待办挂着，就说明有一件事没完。**
    · `INLINE` —— 本轮内问完就走，只贴标签。

    📌 **把「要不要创建」写成 mode 的函数，而不是在每个开交互的地方各写一次** ——
       否则第四处开交互的代码只会去抄最近的一处，抄到哪种全看运气。
       （同 `_UI_TERMINAL_EVENTS` 那条：把规则绑在一个能被检查的地方。）

    ⚠️ 全程吞异常返回 None —— 归属是标签，交互照旧要开得出来。
    """
    try:
        from core.runtime import interaction as _it2
        from core.runtime import task as _tk
        if mode == _it2.Mode.DEFERRED:
            return _tk.ensure_conversation_task("")
        return _tk.owner_label()
    except Exception as e:
        logger.debug(f"[Task] 交互的归属没落上（交互照旧登记）: {e}")
        return None


def _rt_live_interactions(orch) -> list:
    """当前所有未决交互。读路径，失败返回空表。"""
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import interaction as _it
        return _it.list_live(get_kernel())
    except Exception as e:
        logger.debug(f"[Runtime] 读取未决交互失败（本轮按“无待办”处理）: {e}")
        return []


def _rt_open_clarification(orch, original_requirement: str, last_creation_note: str,
                           prompt_text: str) -> str:
    """创建流程自报未决问题 → 开一个 DEFERRED/PERSISTED 交互。

    ⚠️⚠️ **2026-08-13 迁移（探索子循环已拆）**，三处改动：
      1. 参数 `last_explorer_message` → **`last_creation_note`**。
         内容语义不变（仍是一个混合包），但**生产者已经不是 Explorer 了** ——
         现在是主模型填的 `handoff_summary`。名字留着 explorer 就是给后人留假事实。
      2. **删掉 `include_os`**。它是一条死链：整条调用路径没有任何地方能把它传成 True
         （cutover 时已从 `_build_skill_exploration_tools` 删过一处，
         但签名、这里的参数、checkpoint 写入、两段 prompt 片段都还留着）。
      3. **不再写 `explorer_prompt_version`**。Explorer 都没了，再写这个字段
         就是给未来的人留一个指向不存在物的版本号。
    ⭐ **老记录仍然读得到**（读取方用 `.get()`），只是**新写入不再生产这些字段**。
    📌 **一个字段的名字必须描述它现在是什么，不是它当初由谁产生。**

    ⚠️ 仍然**不要**再拆出 `question` / `confirmed_context` 两个字段：
    名字里不用 `confirmed`，因为这段话里混着"已核对的""当前理解"
    "尚未解决的冲突""向用户提的问题"，叫 confirmed 会诱导后来的人
    把整段当成已验证事实（同：别拿命名冒充语义保证）。

    开失败不抛：问题本身已经流式显示给用户了，对话历史也在。
    最坏结果是这一问退化成普通对话（等于旧实现超时后的行为），不是数据损坏。

    ═══ ⭐ 澄清必须有寿命，而且槽满时要驱逐最旧的（2026-08-05 实测）═══

    定了"deferred ≤ 5"，理由是"上限的意义是逼迫收敛"。
    但**没有任何东西会关掉它们**：RESOLVE 只在 continuation 成功时、
    CANCEL 只在模型主动调、SUPERSEDE 没接线、而澄清**从不设 deadline**
    → `expire_tick` 永远碰不到它。于是僵尸单调累积，
    **上限变成硬停而不是驱动力**：实测 8 小时后槽里躺着 5 条，
    最老的是"dasdjasddd 是什么"，之后每一次澄清登记都被不变量拒绝、
    静默退化成普通对话 —— 用户看不出来，整套机制已经死了。

    两条修法：
    1. **给澄清设 `deadline_at`**（审计仍然不设）。之前写过
       "没设 deadline 的永不过期（积压不是错误）" —— 那句对 **Skill 审计**成立
       （用户可能真想放三天再看代码），对**澄清**不成立：
       一个 8 小时前的"你说的 XX 是什么"早就失效了。**两种 kind 的过期语义不同。**
    2. **槽满时驱逐最旧的，而不是拒绝最新的。** 用户当下问的那个才是真正要的；
       8 小时前那条没人会回来答。驱逐记成 `EXPIRED`，不是静默删。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        _k = get_kernel()

        # ── 槽满 → 驱逐最旧的 ───────────────────────────────────────────
        # 只驱逐澄清类：Skill 审计是用户真要看的代码，不能被一句提问挤掉。
        try:
            _live = [r for r in _it.list_live(_k)
                     if r.slot == _it.Slot.DEFERRED
                     and r.kind == _it.Kind.SKILL_CLARIFICATION]
            while len(_live) >= _it.MAX_DEFERRED:
                _oldest = min(_live, key=lambda r: r.created_at)
                _k.submit(Command(kind=_it.EXPIRE, subject_id=_oldest.interaction_id,
                                  payload={"interaction_id": _oldest.interaction_id}))
                logger.info(
                    f"[Interaction] deferred 槽已满，驱逐最旧的澄清 "
                    f"{_oldest.interaction_id}（开了 "
                    f"{(_k.now() - _oldest.created_at) / 60:.0f} 分钟没人回答）"
                )
                _live = [r for r in _live if r.interaction_id != _oldest.interaction_id]
        except Exception as _e:
            logger.warning(f"[Interaction] 驱逐旧澄清失败（继续尝试登记）: {_e}")

        r = _k.submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.SKILL_CLARIFICATION,
            "mode": _it.Mode.DEFERRED,
            # 归属：DEFERRED 跨轮 → 有权让一件事诞生（见 `_rt_ia_owner`）
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": (prompt_text or "").strip()[:500],
            # 澄清有寿命：过了就是陈旧提问，留着只会挤占槽位并干扰模型。
            # 审计不设 deadline（用户可能真想放几天再审代码）。
            "deadline_at": _k.now() + _CLARIFICATION_TTL_SECONDS,
            "payload": {
                "original_requirement": original_requirement,
                # ⚠️ 见本函数 docstring：只留两个字段，不再写 Explorer 专属的那两个。
                "last_creation_note": last_creation_note,
            },
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] 澄清问题已登记 {iid}（slot={r.data.get('slot')}）")
        return iid
    except Exception as e:
        # ⚠️ 吞异常但**不能静默**：槽位满是运维故障，不是可以悄悄降级的事。
        # 实测就是这么死的 —— 连着五次登记失败，日志里有 ERROR，但用户和模型都不知道。
        logger.error(f"[Interaction] 登记澄清问题失败，本次提问退化成普通对话: {e}")
        try:
            from core.health import get_system_events
            get_system_events().add(
                "A clarification question could not be registered, so it will not persist. "
                "If the user answers it in a later turn, you may have no record of what was asked."
            )
        except Exception:
            pass
        return ""


# 需求文本里的"样板"——每个建 Skill 的请求都会带，对"是不是同一件事"零信息量。
# 不剥掉它们，两个完全无关的需求也会因为共享这些词而拿到虚高的重叠度
# （实测第一版就是这么判不出来的：真正例只有 24%）。
_SKILL_REQ_BOILERPLATE = (
    "写一个skill", "写个skill", "创建一个skill", "创建skill", "新建skill",
    "帮我写一个", "帮我写", "帮我创建", "做一个skill", "做个skill",
    "写一个", "写个", "作用是", "用途是", "功能是", "用于",
    "的skill", "一个skill", "skill", "创建", "新建",
)


# 重叠度阈值。实测真正例 67~86%、假正例 0~25%，取 0.5 两边各留 17 个点余量。
_COVERAGE_THRESHOLD = 0.5


def _strip_skill_boilerplate(s: str | None) -> str:
    """剥掉样板词与标点，只留需求的实质部分，供重叠度比对。"""
    out = (s or "").lower()
    for b in _SKILL_REQ_BOILERPLATE:
        out = out.replace(b, "")
    return re.sub(r"[，。、,.:：「」\"'\s（）()\[\]【】]+", "", out)


def _rt_restore_pending_skills(orch) -> None:
    """启动时把待审草稿从库里捞回内存；捞不回来的交互当场收掉。

    ═══ 为什么需要它（2026-08-06 实测）═══

    用户重启后看到三张待审卡还在，但**没有 `<>` 按钮** —— 代码看不了、
    部署和丢弃也点不了，卡片焊死在界面上。

    交互是 PERSISTED（存库），`_pending_skills` 却是纯内存。重启后卡片读回来了、
    草稿没了 → `_get_pending_skill()` 返回 None → `<>` 不渲染 → **这条交互
    再也没有任何出口**（模型侧同理：它能在 [Open Interactions] 里看到这条，
    以为代码还在，实际上批准也没东西可部署）。

    两半都要做：
      ① 有 `draft` 的 → 放回 `_pending_skills`，恢复成真正可操作的待审；
      ② 没有 `draft` 的 → **当场 CANCEL 掉**。它们是本次修复之前留下的孤儿，
         代码已经永久丢失，留着只会继续骗人。

    ⚠️ ② 用 `INTERRUPTED_BY_RESTART` 而不是 `USER_CANCELLED` —— 用户没取消
       任何东西，是进程重启把草稿弄丢了。历史要读得出真相。
    """
    from core.runtime import interaction as _it
    try:
        recs = _rt_live_interactions(orch)
    except Exception as e:
        logger.warning(f"[Runtime] 待审草稿恢复：读交互失败，跳过（不影响启动）: {e}")
        return

    restored, orphaned = 0, 0
    for r in recs:
        if r.kind != _it.Kind.SKILL_AUDIT:
            continue
        draft = (r.payload or {}).get("draft") if isinstance(r.payload, dict) else None
        if isinstance(draft, dict) and draft.get("filename") and draft.get("code"):
            orch._put_pending_skill(draft)
            restored += 1
            continue
        orphaned += 1
        _rt_close_interaction(r.interaction_id, _it.CANCEL,
                              resolution=_it.Resolution.INTERRUPTED_BY_RESTART)
        logger.warning(
            f"[Runtime] 待审 {r.interaction_id}（{r.artifact_id or '?'}）"
            f"草稿已随上次进程丢失 → 收掉。"
            f"（本次修复之前登记的审计都没存草稿，属于历史遗留）"
        )
    if restored or orphaned:
        logger.info(f"[Runtime] 待审草稿恢复：{restored} 份复原、{orphaned} 条孤儿已收")


def _rt_open_skill_audit(orch, filename: str, description: str, code: str,
                         mode: str, valid: bool, errors: list) -> str:
    """Skill 审计弹窗 → 一条 DEFERRED/PERSISTED Interaction。

    ═══ 为什么审计要进 Interaction ═══

    改造前 `_pending_skill` 是个纯内存 dict + 一段路由劫持 + 一个专用 LLM 分类器
    （`classify_pending_skill_intent`，8 个标签，每条消息多一次 API 往返）。三个问题：

      ① **进程一关就没**。审计弹窗开着时崩溃/关窗，那份待审代码不留任何痕迹，
         重启后没人知道它存在过（`_pending_skill` 在内存里）。
      ② **模型不知道有这件事**。劫持是代码层的，模型看不见"有个 Skill 等着审"，
         于是用户说别的时它也无从判断。
      ③ **专用分类器违反设计原则 3**（少硬路由、判断交给有完整上下文的主决策模型）。
         8 个标签里有一半（EXPLAIN / RISK / COMPLAINT / EXECUTE_BLOCKED）
         本质就是"正常回答用户"，主模型天然会做，不需要先分类。

    ⭐ **artifact 绑定在这里第一次真正用上**：
    审批必须钉在**当时那一版**上。`artifact_id=filename` + `artifact_hash=代码指纹`，
    落地前 `verify_artifact()` 复核。否则用户点"同意"时批准的可能是一个已经被改过的版本。

    ⚠️ 开失败不抛：弹窗已经显示了，用户照样能点按钮（`_pending_skill` 仍在）。
    最坏结果是这次审批没有持久记录 —— 退化成改造前的行为，不是数据损坏。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        _q = (f"待审计的 Skill「{filename}」"
              f"{'（校验未通过：' + '；'.join(errors[:2]) + '）' if not valid else '（校验通过）'}"
              f"：{(description or '').strip()[:120]}")
        r = get_kernel().submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.SKILL_AUDIT,
            "mode": _it.Mode.DEFERRED,
            # 归属：DEFERRED 跨轮 → 有权让一件事诞生（见 `_rt_ia_owner`）
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": _q,
            # ⚠️ 审计**刻意不设 deadline**：用户可能真想把代码放几天再看。
            # 澄清才有 2 小时 TTL —— 两种 kind 的过期语义不同。
            "artifact_kind": "skill",
            "artifact_id": filename,
            "artifact_hash": _it.artifact_hash(code),
            "payload": {
                "filename": filename, "mode": mode,
                "valid": bool(valid), "errors": list(errors or []),
                "code_lines": len((code or "").splitlines()),
                # ⭐⭐ [2026-08-06 实测] 草稿本身必须跟着交互一起落盘。
                #
                # 实测：重启后三张待审卡还在，但**没有 `<>` 按钮**，
                # 既看不了代码也部署/丢弃不了 —— 卡片焊死在界面上。
                #
                # 原因：交互是 PERSISTED（存库），`_pending_skills` 却是**纯内存**。
                # 重启后卡片从库里读回来了，草稿没了 → `_get_pending_skill()` 返回 None
                # → `<>` 不渲染 → 这条交互再也没有任何出口。
                #
                # ⚠️ 这不是"少存了一个字段"，是**耐久性承诺自相矛盾**：
                #    审计**刻意不设 deadline**，理由是"用户可能真想放三天再看代码"
                #。这个理由只有在**代码也活到三天后**时才成立。
                #
                # 📌 判据：**一条持久记录不能指向一个易失的东西。**
                #    要么两个一起持久，要么这条记录本身就不该是持久的。
                #
                # 存整份载荷而不是只存 code：部署要用 spec_side_effects / lifecycle /
                # error_context 等一整套，缺一个就是另一种形式的半残
                #（尤其 spec_side_effects 的 None 与 [] 语义不同，见 `_put_pending_skill`
                #  调用处那段注释——JSON 能如实保留 null，不会退化成 []）。
                "draft": orch._get_pending_skill(filename),
            },
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] Skill 审计已登记 {iid}（{filename}，slot={r.data.get('slot')}）")

        # ⭐⭐ [2026-08-06 实测] 同名的旧待审草稿要被新的取代，不能并存。
        #
        # 实测：三张待审卡同时挂着，**全都叫 `GetComputerIP`**
        #（int_9eeac9e476 / int_e42c7c7f04 / int_f2633f59d5）。
        # 里则是反过来的表现：两张同名 `GetDNSConfig`，部署其中一张
        # **两张一起消失**（18:08:15 同毫秒关掉两条）——用户当时以为是 UI bug。
        #
        # 两个现象是同一个原因：**文件名就是这个产物的身份**。
        # `_pending_skills` 按文件名存，同名后写的直接覆盖先写的；
        # `apply_pending_skill(filename)` 也只认文件名。于是"多张同名卡"
        # 从一开始就是假象 —— 底下**只有一份 payload**，卡片却有三张，
        # 点哪张都是同一份，关一张就该关全部。
        #
        # 📌 判据：**同一个身份不允许有多个未决条目。**
        #    两份同名草稿不可能都部署（只有一个文件位），所以更晚的那份
        #    就是对更早那份的**取代**，而不是"又一个待办"。
        #
        # ⚠️ 用 SUPERSEDE 而不是 CANCEL：用户没有取消任何东西，
        #    是 Nano 自己重写了一版。语义要对得上，否则历史读起来是错的。
        try:
            for _old in _rt_live_interactions(orch):
                if (_old.kind == _it.Kind.SKILL_AUDIT
                        and _old.artifact_id == filename
                        and _old.interaction_id != iid):
                    # ARTIFACT_CHANGED 就是这里的实情：同一个产物被重写了一版。
                    _rt_close_interaction(_old.interaction_id, _it.SUPERSEDE,
                                          resolution=_it.Resolution.ARTIFACT_CHANGED)
                    logger.info(
                        f"[Interaction] 同名旧草稿 {_old.interaction_id}（{filename}）"
                        f"→ SUPERSEDED，被 {iid} 取代（一个文件名只能有一份待审）"
                    )
        except Exception as e:
            # 收不掉不致命：最坏是回到"多张同名卡"那个旧行为，不是数据损坏。
            logger.warning(f"[Interaction] 收拢同名旧草稿失败（不影响本次登记）: {e}")

        # ⭐⭐ [BUG3 · 2026-08-06 实测] 草稿一出现 → 未决澄清的目的已经达成，收掉它。
        #
        # 实测：进澄清 → 回答 → Nano 开始写 Skill → **那张澄清卡片一直挂着**，
        # 直到 Skill 被部署（甚至更久）。
        #
        # 原因是澄清只有两条关闭通路：`answer_open_interaction` 的 RESOLVE，
        # 或者 `_rt_supersede_covered_clarifications` 那个**文本重叠度启发式**。
        # 而模型这次没调工具、直接开了新的创建流程，且用户的回答
        #（"新建一个，SlugName 那个不一样"）跟原需求几乎没有字面重叠 → 启发式没命中。
        #
        # ⭐ 修法不是调那个阈值，是换一个**非启发式的信号**：
        # **审计草稿的存在本身就证明澄清已经被消化了** —— 探索阶段能写出代码，
        # 说明它拿到了它要问的东西。这条判断不依赖任何文本匹配，也不会误伤：
        # 一个还需要用户回答的问题，不可能同时产出一份完整的待审代码。
        #
        # 📌 判据：**优先找"状态本身能证明的事实"，而不是去猜文本像不像。**
        # 启发式该是兜底，不该是唯一通路。
        try:
            _k2 = get_kernel()
            for _c in _it.list_live(_k2):
                if _c.kind != _it.Kind.SKILL_CLARIFICATION:
                    continue
                _k2.submit(Command(kind=_it.SUPERSEDE, subject_id=_c.interaction_id,
                                   payload={"interaction_id": _c.interaction_id,
                                            "superseded_by": iid,
                                            "resolution": _it.Resolution.DONE}))
                logger.info(
                    f"[Interaction] 澄清 {_c.interaction_id} 已被草稿 {filename} 消化"
                    f" → SUPERSEDED（草稿存在即证明问题已解决）"
                )
        except Exception as _e:
            logger.warning(f"[Interaction] 收尾澄清失败（不影响审计）: {_e}")
        return iid
    except Exception as e:
        logger.error(f"[Interaction] 登记 Skill 审计失败，退化为纯内存 pending: {e}")
        return ""


def _rt_open_skill_manage(orch, op: str, skill: str, prompt_text: str,
                          extra: dict | None = None) -> str:
    """Skill 管理/修改确认 → 一条 DEFERRED/PERSISTED Interaction。

    取代 `_pending_action`（四个 op：delete / disable / enable / update_skill）
    加一段路由劫持加一个专用分类器（`classify_pending_action_intent`，6 个标签）。

    ⚠️ **这不是 OS 风险确认** —— 2026-08-06 考古纠正，见 `Kind.SKILL_MANAGE` 的注释。
    真正的 OS 确认是 `execution_confirm` 事件 + 300 秒同步等待，属于清单 ⑧。

    与审计（②b）共享同一套处置语义，所以复用 `answer_open_interaction` 的三个 relation：
        ANSWER               → 确认执行
        ANSWER_AND_AMENDMENT → 改一改再执行（只有 update_skill 用得上）
        CANCEL               → 不做了
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        r = get_kernel().submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.SKILL_MANAGE,
            "mode": _it.Mode.DEFERRED,
            # 归属：DEFERRED 跨轮 → 有权让一件事诞生（见 `_rt_ia_owner`）
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": (prompt_text or "").strip()[:500],
            "artifact_kind": "skill",
            "artifact_id": skill,
            "payload": {"op": op, "skill": skill, **(extra or {})},
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] Skill 管理确认已登记 {iid}（{op} {skill}）")
        return iid
    except Exception as e:
        logger.error(f"[Interaction] 登记 Skill 管理确认失败，退化为纯内存 pending: {e}")
        return ""


def _rt_open_mcp_manage(orch, op: str, server: str, prompt_text: str) -> str:
    """MCP 管理确认 → 一条 DEFERRED/PERSISTED Interaction。

    形状照抄 `_rt_open_skill_manage`，只有三处不同：
        kind           MCP_MANAGE（理由见那个常量的注释）
        artifact_kind  "mcp"
        payload        {"op", "server"} —— 不是 {"op", "skill"}

    ⚠️ **必须登记 Interaction，不能只挂 `_pending_action`** ——
       后者的消费者 `_handle_pending_action` 在 （2026-08-06）就删了，
       现在全仓只有赋值和清空、**没有任何地方读它来执行**。
       📌 只挂它的话，用户回复「确认」之后什么都不会发生 ——
          而那是最坏的一种失败：**用户以为自己批准了，系统以为没这回事。**

    ⭐ 它不会在 UI 上画卡片：`app._CARD_KINDS` 是**白名单**，只有 SKILL_AUDIT 进。
       ⇒ MCP 管理天然走「纯净的自然语言」那条路 —— 正是设计要的。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        r = get_kernel().submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.MCP_MANAGE,
            "mode": _it.Mode.DEFERRED,
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": (prompt_text or "").strip()[:500],
            "artifact_kind": "mcp",
            "artifact_id": server,
            "payload": {"op": op, "server": server},
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] MCP 管理确认已登记 {iid}（{op} {server}）")
        return iid
    except Exception as e:
        logger.error(f"[Interaction] 登记 MCP 管理确认失败: {e}")
        return ""


def _rt_close_mcp_manage(server: str, approved: bool, reason: str = "") -> None:
    """MCP 管理确认收尾。按 `artifact_id` 找 —— 同 `_rt_close_skill_manage`。"""
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        for rec in get_kernel().interactions.open_records():
            if rec.kind != _it.Kind.MCP_MANAGE or rec.artifact_id != server:
                continue
            get_kernel().submit(Command(kind=_it.CLOSE, payload={
                "interaction_id": rec.interaction_id,
                "transition": _it.ANSWER if approved else _it.CANCEL,
                "resolution": (_it.Resolution.CONFIRMED if approved
                               else _it.Resolution.CANCELLED),
                "reason": reason or "",
            }))
            return
    except Exception as e:
        logger.warning(f"[Interaction] 关闭 MCP 管理确认失败: {e}")


def _rt_close_skill_manage(skill: str, approved: bool, reason: str = "") -> None:
    """管理确认处置完收尾。按 `artifact_id` 找 —— 同 `_rt_close_skill_audit` 的理由。"""
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        k = get_kernel()
        for rec in _it.list_live(k):
            if rec.kind != _it.Kind.SKILL_MANAGE or rec.artifact_id != skill:
                continue
            k.submit(Command(kind=_it.DECIDE, subject_id=rec.interaction_id,
                             payload={"interaction_id": rec.interaction_id,
                                      "approved": bool(approved),
                                      "resolution": reason or _it.Resolution.DONE}))
            logger.info(
                f"[Interaction] Skill 管理确认 {rec.interaction_id} 已"
                f"{'执行' if approved else '取消'}（{skill}）"
            )
    except Exception as e:
        logger.warning(f"[Interaction] 关闭管理确认交互失败（不影响执行结果）: {e}")


def _rt_close_skill_audit(filename: str, approved: bool, reason: str = "") -> None:
    """UI 按钮或模型工具处置完审计后收尾。按 `artifact_id` 找那条交互。

    ⚠️ 必须能被 **UI 按钮**调用（app.py 的"验证并应用"/"丢弃"直接调 orchestrator 的
    `apply_pending_skill` / `cancel_pending_skill`），否则用户点按钮之后
    交互会一直挂在那里 —— 那就是我们刚修掉的僵尸形状。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        k = get_kernel()
        for rec in _it.list_live(k):
            if rec.kind != _it.Kind.SKILL_AUDIT or rec.artifact_id != filename:
                continue
            k.submit(Command(kind=_it.DECIDE, subject_id=rec.interaction_id,
                             payload={"interaction_id": rec.interaction_id,
                                      "approved": bool(approved),
                                      "resolution": reason or _it.Resolution.DONE}))
            logger.info(
                f"[Interaction] Skill 审计 {rec.interaction_id} 已"
                f"{'批准' if approved else '拒绝'}（{filename}）"
            )
    except Exception as e:
        logger.warning(f"[Interaction] 关闭 Skill 审计交互失败（不影响部署结果）: {e}")


def _rt_supersede_covered_clarifications(orch, new_requirement: str) -> int:
    """开一条新的 Skill 创建流程时，把**已被它涵盖**的未决澄清标成 SUPERSEDED。

    ═══ 为什么要有这个（实测）═══

    Nano 问"你要哪些网络配置？"，用户答"选 7，全部上述内容"，模型**没有**调
    `answer_open_interaction`，而是开了一条新的 `create_new_skill`（需求文本里
    已经包含那个回答）。需求办成了，但那条澄清留在槽里变僵尸。

    ⚠️ **这是个启发式**（按需求文本重叠度判断"是不是同一件事"），而当天已经在
    "靠猜措辞"上翻车两次。这次可以用，理由是**失败代价对称且轻微**：
        误判 → 澄清关早了，用户重问一次
        漏判 → 留个僵尸，2 小时 TTL 收掉
    低风险场景才适合启发式。**不要把这个理由推广到高风险判断上。**

    ═══ 阈值是**量出来的**，不是拍的 ═══

    第一版按原文直接切 3-gram，实测那个例子只得 24%（判不出来）—— 因为原始需求里
    有一堆样板（"写个skill，作用是"）而模型改写后的需求不含它们。
    剥掉样板 + 改 2-gram 后，用 8 组真/假样本量了一遍：

        真正例（同一需求）    67% / 67% / 86%
        假正例（不同需求）    0% / 0% / 0% / 25%

    阈值定 **0.5**，两边各留 17 个百分点余量。

    📌 **纪律**：启发式的阈值必须先量出真假两侧的分布再定。
    直接拍一个数等于把"能不能用"交给运气 —— 第一版的 3-gram 就是这么错的
    （真 30% vs 假 29%，几乎零余量，而当时没量所以不知道）。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
    except Exception:
        return 0
    _new = _strip_skill_boilerplate(new_requirement)
    if len(_new) < 4:
        return 0
    n = 0
    try:
        k = get_kernel()
        for rec in _it.list_live(k):
            if rec.kind != _it.Kind.SKILL_CLARIFICATION:
                continue
            _orig = _strip_skill_boilerplate((rec.payload or {}).get("original_requirement"))
            if len(_orig) < 2:
                continue
            # 2-gram：中文没有空格，按词切不可靠。2 字片段对"查看当前网络配置"
            # 这类短需求区分度最好（3-gram 实测真假两侧几乎重叠，见 docstring）。
            _grams = {_orig[i:i + 2] for i in range(len(_orig) - 1)}
            if not _grams:
                continue
            _hit = sum(1 for g in _grams if g in _new) / len(_grams)
            if _hit < _COVERAGE_THRESHOLD:
                continue
            k.submit(Command(kind=_it.SUPERSEDE, subject_id=rec.interaction_id,
                             payload={"interaction_id": rec.interaction_id,
                                      "resolution": _it.Resolution.FOLLOW_UP_QUESTION}))
            n += 1
            logger.info(
                f"[Interaction] 澄清 {rec.interaction_id} 已被新的创建流程涵盖"
                f"（需求重叠 {_hit:.0%}）→ SUPERSEDED，不再留作僵尸"
            )
    except Exception as e:
        logger.warning(f"[Interaction] 涵盖判定失败（不影响创建流程）: {e}")
    return n


def _rt_answer_interaction(interaction_id: str, answer_verbatim: str, relation: str) -> dict:
    """把用户原话原子落盘。**故意不吞异常** —— 见本节开头。

    返回 kernel 的 result data（含 `retry`：True = 这是失败后的幂等重试）。
    """
    from core.runtime.kernel import get_kernel, Command
    from core.runtime import interaction as _it
    r = get_kernel().submit(Command(
        kind=_it.ANSWER, subject_id=interaction_id,
        payload={"answer_verbatim": answer_verbatim, "relation": relation},
    ))
    return dict(r.data or {})


def _rt_close_interaction(interaction_id: str, kind: str, **payload) -> None:
    """收尾（RESOLVE / CANCEL / SUPERSEDE）。吞异常：

    到这一步用户的答案早已落盘、continuation 也已经跑完，收尾失败最坏是
    一条交互多停留一会儿（下次 Explorer 提问会 SUPERSEDE 它，或用户手动取消）。
    为了这个把已经成功的整轮变成报错，不划算。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        get_kernel().submit(Command(kind=kind, subject_id=interaction_id,
                                    payload={"interaction_id": interaction_id, **payload}))
    except Exception as e:
        logger.warning(f"[Interaction] 收尾 {kind} 失败（不影响本轮结果）: {e}")


def _rt_cancel_all_interactions(reason: str = "") -> int:
    """重置对话时清场。取代原来的 `self._pending_skill_clarification = None`。"""
    n = 0
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        k = get_kernel()
        for rec in _it.list_live(k):
            try:
                k.submit(Command(kind=_it.CANCEL, subject_id=rec.interaction_id,
                                 payload={"interaction_id": rec.interaction_id,
                                          "resolution": _it.Resolution.USER_CANCELLED}))
                n += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[Interaction] 重置时清理未决交互失败: {e}")
    if n:
        logger.info(f"[Interaction] 重置对话，作废 {n} 个未决交互（{reason}）")
    return n


# 澄清提问的寿命。两小时之后那句"你说的 XX 是什么"基本已经失效，
# 留着只会挤占 deferred 槽位（上限 5）并在动态段里干扰模型。
# ⚠️ 只对**澄清**设，Skill 审计刻意不设 —— 用户可能真想放几天再审代码。
# 实测：8 小时后槽里躺着 5 条陈旧澄清，之后每次登记都被不变量拒绝。
_CLARIFICATION_TTL_SECONDS = 2 * 60 * 60
