# core/orchestrator/turn.py
"""Orchestrator 的这一部分：一轮用户消息的入口：handle_query 与其实现。"""

import asyncio
import time
import traceback

from loguru import logger

from core.orchestrator._runtime import (
    _rt_authorization_state,
    _rt_background_jobs,
    _rt_lease_release,
    _rt_ongoing_work,
    _rt_sweep_stale_spans,
)
from core.provider import CACHE_BREAK_MARKER
from core.tools import Preload, ToolScope


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


class TurnMixin:
    """一轮用户消息的入口：handle_query 与其实现。"""

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
