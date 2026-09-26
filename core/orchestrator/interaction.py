# core/orchestrator/interaction.py
"""回答待办交互、请用户选择、回复指向的移交。（`Orchestrator` 的 mixin）"""

import asyncio
from typing import Any

from loguru import logger

from core.orchestrator._runtime import (
    _rt_answer_interaction,
    _rt_close_interaction,
    _rt_close_mcp_manage,
    _rt_close_skill_manage,
    _rt_open_skill_audit,
)


class InteractionMixin:
    """回答待办交互、请用户选择、回复指向的移交。"""

    def hand_off_reply_target(self) -> str:
        """⭐⭐⭐ [2026-08-13 CMD63] 用户按下发送 → 把「回复这条」的指向**移交给这一轮**。

        ═══ 这个方法存在的理由（一个实测 bug，两行日志就能看完）═══

：

            22:33:39.127  [UI] 引用已发出（int_2f32640844）→ 引用态复位
            22:33:39.131  [TOKEN-PLAN] core_tools=6 (…answer_open_interaction)

        用户点了 replay 引用一张 skill_audit 卡、说「部署这个吧」。
        UI 在**渲染发送行时**就把 `_reply_target` 清了，而
        `_build_open_interactions_injection()` **4 毫秒之后**才去读它 ——
        于是那句「⭐ The user explicitly marked this message as answering
        int_2f32640844 — do not pick a different one」**一个字都没进 prompt**。

        模型只看到一条 `[skill_audit]` 和一句「部署这个吧」，
        于是先 `load_tools` 把 `create_new_skill` 捞回来（22:33:39 是 6 个工具，
        22:33:47 变成 7 个，多出来的正是它），再调它 → 进探索 →
        探索作用域里**根本没有"部署"这个出口** → 伸手拿 `create_new_skill`
        → 内部故障路径 → 澄清待办也跟着不登记。
        ⭐ **用户报的那两个"独立问题"其实是一条链，头在这里。**

        ═══ 为什么当初会清早（这才是要记住的部分）═══

        `handle_query` 的 `finally` 里**本来就有**一处复位（2026-08-06，位置正确）。
        2026-08-09 修「composer 停在引用态」时，app.py 那处注释写的是
        「回查发现…**发送路径上一处都没有**」—— 它没看见 orchestrator 已经有了，
        于是加了**第二处**。而那个 bug 的真因是**显示层没被通知**
        （`_refresh_reply_prompt` 那条自愈线），却被修成了**提前清状态**。

        📌 **一个「发出去就该消失」的状态，不该被清掉，该被【移交】** ——
           清掉会让真正的消费者读空。
        📌 修「显示没跟上」时，动的必须是显示；动状态会把延迟问题变成丢失问题。

        四个诉求这样同时满足：
          · composer 立刻复位（`_reply_target` 确实空了）
          · 模型拿得到指向（读 `_reply_target_turn`）
          · 一次性（`finally` 里连同快照一起清）
          · 单一权威（两个字段都只在 orchestrator 上，UI 只调这个方法）

        返回被移交的 iid（没有就空串），供调用方打日志。
        """
        if self._reply_target is None:
            return ""
        self._reply_target_turn = self._reply_target
        self._reply_target = None
        return (self._reply_target_turn or {}).get("iid") or ""

    async def _handle_answer_interaction(self, args: dict, base_guide: str,
                                         realtime_callback, event_queue):
        """消费 `answer_open_interaction`。

        ═══ 顺序是这个函数的全部意义 ═══

            ① 先把用户原话原子落盘（OPEN/ANSWERED → ANSWERED）
            ② 再去跑 continuation（重进 Explorer）
            ③ 跑完了才 RESOLVE

        旧实现是反过来的：先 `_pending_skill_clarification = None` 再调 Explorer。
        于是第 ② 步一失败（provider 报错 / 进程被杀 / 用户拔电），
        **用户刚说的那句话就永久消失了**，只能让用户重说一遍。

        现在第 ② 步失败时状态停在 ANSWERED，答案还在库里，
        下一轮模型会从 [Open Interactions] 里看到重试提示，用同一个工具幂等重试。
        """
        from core.runtime import interaction as _it

        iid = (args.get("interaction_id") or "").strip()
        answer = args.get("answer_verbatim") or ""
        relation = (args.get("relation") or _it.Relation.ANSWER).strip().upper()
        if relation not in _it.Relation._ALL:
            relation = _it.Relation.ANSWER

        rec = None
        try:
            from core.runtime.kernel import get_kernel
            rec = _it.get(get_kernel(), iid)
        except Exception as e:
            logger.error(f"[Interaction] 读取 {iid} 失败: {e}")

        if rec is None or not rec.is_live:
            # 模型引用了一个不存在或已关闭的交互。**不要静默吞掉**——
            # 静默会让模型以为"回答已被记录"，然后它就不会再提这件事了。
            _why = "已经处理过了" if rec is not None else "不存在"
            # ⚠️ `_why` 给日志看（中文）；交给模型的事实必须英文，
            #    所以**并排**放一份，而不是把中文塞进 tool_result。
            _why_en = "was already handled" if rec is not None else "does not exist"
            logger.warning(f"[Interaction] 模型引用的交互 {iid!r} {_why}")
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"The to-do {iid!r} that was referenced {_why_en}. Nothing was recorded "
                       f"against it. What the user actually said: " + repr(answer) + ". "
                       f"Answer that as a normal request; bring up the stale to-do only if it "
                       f"actually matters to them."
                   ),
                   "log": f"交互 {iid} {_why}。"}
            return

        # ── ① 原子落盘 ──────────────────────────────────────────────────
        try:
            _res = _rt_answer_interaction(iid, answer, relation)
        except Exception as e:
            # 这里**必须**告诉用户，不能假装记下了继续跑。
            logger.error(f"[Interaction] 记录回答失败 {iid}: {e}")
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"Could not save the user's answer to to-do {iid!r} (runtime storage "
                       f"error: {e}). Their answer was NOT recorded. Tell them briefly and ask "
                       f"them to say it once more."
                   ),
                   "log": f"Interaction {iid} 落盘失败"}
            return
        if _res.get("retry"):
            logger.info(f"[Interaction] {iid} 幂等重试（答案未变，不重复写）")

        # ── CANCEL：不需要 continuation ─────────────────────────────────
        if relation == _it.Relation.CANCEL:
            # 审计类要连**工作载荷**一起丢，不能只关交互 ——
            # 留着 `_pending_skill` 会让 UI 上那个弹窗仍然可以点「验证并应用」，
            # 而用户刚说的是"不要了"。走 `cancel_pending_skill()` 复用既有清理
            # （它自己会调 `_rt_close_skill_audit`，所以这里不再重复关）。
            if rec.kind == _it.Kind.SKILL_AUDIT and                     self._get_pending_skill(rec.artifact_id or ""):
                _res_c = self.cancel_pending_skill(rec.artifact_id or "")
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The user cancelled the pending Skill audit for {rec.artifact_id!r}. "
                           f"The draft was discarded; nothing was deployed. Acknowledge briefly "
                           f"and move on - do not re-offer it unless they bring it up."
                       ),
                       "log": f"用户取消了审计 {iid}。"}
                return
            if rec.kind == _it.Kind.MCP_MANAGE:
                # MCP 管理取消。⚠️ 与 Skill 那条**同样**要清 `_pending_action` ——
                #    理由一字不差：留着它会活到 30 分钟超时，期间用户再说「确认」
                #    会误触发一个已经被取消过的操作，而**删 MCP 同样不可逆**。
                _srv_c = (rec.payload or {}).get("server") or rec.artifact_id or ""
                _rt_close_mcp_manage(_srv_c, approved=False,
                                     reason=_it.Resolution.USER_CANCELLED)
                self._pending_action = None
                self._pending_action_at = 0.0
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The user cancelled deleting MCP server {_srv_c!r}. Nothing was deleted; "
                           f"the server is still configured and usable. Acknowledge briefly."
                       ),
                       "log": f"用户取消了 MCP 管理 {iid}。"}
                return
            if rec.kind == _it.Kind.SKILL_MANAGE:
                # 管理类取消：把 `_pending_action` 一起清掉，
                # 否则它会活到 30 分钟超时，期间用户再说"确认"会误触发一个
                # 已经被取消过的操作（删 Skill 是不可逆的，这个错不能犯）。
                _sk_c = (rec.payload or {}).get("skill") or rec.artifact_id or ""
                _rt_close_skill_manage(_sk_c, approved=False,
                                       reason=_it.Resolution.USER_CANCELLED)
                self._pending_action = None
                self._pending_action_at = 0.0
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The user cancelled the pending management operation on Skill {_sk_c!r} "
                           f"(the operation was: {(rec.payload or {}).get('op') or 'unspecified'}). "
                           f"Nothing was changed. Acknowledge briefly."
                       ),
                       "log": f"用户取消了管理确认 {iid}。"}
                return
            _rt_close_interaction(iid, _it.CANCEL,
                                  resolution=_it.Resolution.USER_CANCELLED)
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"The user cancelled the pending to-do {iid!r}. Nothing was done. "
                       f"Acknowledge briefly."
                   ),
                   "log": f"用户取消了交互 {iid}。"}
            return

        # ── ② continuation：按 kind 分派 ────────────────────────────────
        if rec.kind == _it.Kind.SKILL_AUDIT:
            # 用户用**打字**处置待审 Skill（点按钮走的是 UI 那条路）。
            #
            # relation 的映射：
            #   ANSWER               → 同意部署（"可以"/"部署吧"）
            #   ANSWER_AND_AMENDMENT → 要改（"把阈值改成 6000 再部署"）
            #   CANCEL               → 丢弃（已在上面通用分支处理）
            #
            # ⭐ 在这里执行：**用户点同意与代码真正写盘之间，artifact 可能已经变了**
            # （用户在 CodeMirror 里改了几行、或者另一条流程覆盖了 `_pending_skill`）。
            # 所以落地前必须复核指纹，不一致就停手 —— 不能"反正用户说了可以"。
            # **按 rec.artifact_id 精确取**这一条的载荷。
            # 改造前这里读的是"最近那条"再跟 artifact_id 比对，于是两条待审并存时
            # 对老的那条调工具必然比对失败 → 掉进"载荷已失效"分支 → 误报 + 误取消。
            _fn = rec.artifact_id or ""
            _pend = self._get_pending_skill(_fn) or {}
            if not _pend:
                # 待审载荷已经不在了（超时清理 / 被别的流程覆盖 / 重启后只剩交互记录）。
                # 如实说清，不要假装能部署 —— 代码正文没有落盘，重启后确实拿不回来。
                _rt_close_interaction(iid, _it.CANCEL,
                                      resolution=_it.Resolution.INTERRUPTED_BY_RESTART)
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The reviewed draft for {rec.artifact_id!r} is gone. Pending drafts live "
                           f"only in memory, so a restart or a timeout loses them - the code itself "
                           f"was never written to disk and cannot be recovered. Say that plainly, "
                           f"and offer to generate a fresh version for them to review."
                       ),
                       "log": f"审计交互 {iid} 的载荷已失效。"}
                return

            _ok_art, _why = _it.verify_artifact(
                rec, current_revision=None, current_text=_pend.get("code") or "")
            if not _ok_art:
                # artifact 变了 → 这一次批准不放行（它指的是旧版本）。
                #
                # ⚠️⚠️ **但绝不能把这条交互直接关掉。** 第一版就是那么写的
                # （`SUPERSEDE` + ARTIFACT_CHANGED），2026-08-06 实测
                # 当场暴露自相矛盾：
                #   · 卡片消失了，而回复却说"再点一次「验证并应用」即可" ——
                #     那个弹窗的**唯一入口就是刚被关掉的那张卡**；
                #   · 用户改到一半的代码就此**再也拿不回来**；
                #   · 而且 SUPERSEDED **没有任何后继者**，状态本身就是假的。
                #
                # 📌 判据：**拒绝一次操作 ≠ 结束这件事。**
                #    守卫要挡住的是"这次批准指的是旧版本"，不是"这份草稿不要了"。
                #    用户的编辑是合法产出，凭什么因为一次拒绝就被丢掉。
                #
                # 正解：给**当前这一版**重新开一张待审卡。新卡登记时会自动
                # SUPERSEDE 同名旧卡（见 `_rt_open_skill_audit`），于是
                # 「旧的被取代」这个状态第一次真的成立，用户也立刻有了新入口。
                logger.warning(f"[Interaction] 审计 {iid} 的 artifact 已变更，拒绝放行：{_why}")
                _new_iid = ""
                try:
                    _new_iid = _rt_open_skill_audit(
                        self, _fn, _pend.get("description") or "",
                        _pend.get("code") or "", _pend.get("mode", "create"),
                        bool(_pend.get("valid", True)), list(_pend.get("errors") or []),
                    )
                except Exception as _e:
                    # 开不出新卡也不能把旧卡关掉 —— 那样用户就彻底没入口了。
                    # 宁可留一张"批准会被拒"的旧卡，也比留一份够不着的代码强。
                    logger.error(f"[Interaction] 为改动后的 {_fn} 重开待审失败: {_e}")
                # ⚠️ **不要在这里写死一句中文交给用户。**
                #
                # 2026-08-06：固定文案完全不受人格模板影响，占比一高就会
                # 让 Nano 高频人格分裂 —— 满屏都是设定好的语气，中间突然蹦出一句
                #「批准要对得上具体哪一版」。设计原则只给两个豁免：
                #   ① 模型调用本身出问题时的兜底  ② 气泡里的系统级通知/报错
                # 这一条**两个都不占**：模型好好的，而且这是一次正常的业务结果，
                # 不是系统故障。
                #
                # 所以只把**事实**交回去（英文，按"注入给模型的提示词用英文"那条），
                # 由主循环写成 tool_result，让模型用它自己的话说。
                # 📌 同：给足够的事实让它判断怎么说、下一步做什么，
                #    而不是替它把话说死。
                yield {
                    "event": "exit_flow_defer_to_model",
                    "tool_result": (
                        f"Not deployed. The draft changed after this approval was formed, "
                        f"so approving it would have installed a different version than the "
                        f"one the user meant ({_why}). "
                        + (f"The edited version is now pending review as {_new_iid}; "
                           f"it can be deployed once the user confirms this version."
                           if _new_iid else
                           "The user can install this version from the review dialog "
                           "with the apply button.")
                        + " Tell the user what happened and what they can do next. "
                          "Do not repeat the hash values — they mean nothing to them."
                    ),
                }
                return

            if relation == _it.Relation.ANSWER_AND_AMENDMENT:
                # 要改 → 走既有的"带修改意见重新生成"通路，不自己造一条。
                # 交互标 SUPERSEDED：新生成的那一版会开一条**新的**审计交互
                # （同 Explorer 又提新问题的处置：旧的关掉、新的独立，不做回退覆盖）。
                _rt_close_interaction(iid, _it.SUPERSEDE,
                                      resolution=_it.Resolution.FOLLOW_UP_QUESTION)
                logger.info(f"[Interaction] 审计 {iid} → 用户要求修改，重新生成")
                async for step in self._generate_skill_with_writer(
                    _pend.get("description") or _fn, base_guide, realtime_callback,
                    extra_instruction=(
                        "\n\n[User Revision Request For The Pending Draft]\n"
                        f"{answer}\n\n"
                        "Regenerate the Skill applying this. Keep everything else as it was."
                    ),
                    mode=_pend.get("mode", "create"),
                    target_skill=_pend.get("target_skill"),
                ):
                    yield step
                return

            # ANSWER → 同意部署。走既有 apply_pending_skill（它自己会关交互）。
            logger.info(f"[Interaction] 审计 {iid} → 用户同意部署 {_fn}")
            _res = self.apply_pending_skill(_fn)
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"The user approved the pending Skill {_fn!r} and it has been deployed "
                       f"(ok={_res.get('ok')}). Raw system message, as evidence: "
                       + repr(_res.get("msg") or "") + ". Tell them it is in place."
                   ),
                   "log": f"审计 {iid} 经模型工具批准部署。"}
            from core import skill_watch as _skw
            _skw.request_reload("审计经模型工具批准部署")
            return

        if rec.kind == _it.Kind.MCP_MANAGE:
            # 用打字确认 MCP 管理操作。只有 delete 会走到这里 ——
            # enable / disable / retry 可逆，在 `_handle_manage_mcp_decision` 里
            # 已经立即执行完了，不进确认流。
            _p_m = rec.payload or {}
            _srv = _p_m.get("server") or rec.artifact_id or ""
            _op_m = (_p_m.get("op") or "").strip()
            logger.info(f"[Interaction] MCP 管理确认 {iid} → 用户确认 {_op_m} {_srv}")
            _rt_close_mcp_manage(_srv, approved=True)
            self._pending_action = None
            self._pending_action_at = 0.0
            try:
                from core.mcp_client import MCPManager as _MM_c
                _ok = await _MM_c.instance().remove_server(_srv)
                _mcp_facts = (
                    f"MCP server {_srv!r} was deleted from the configuration."
                    if _ok else
                    f"Deleting MCP server {_srv!r} did not happen - it was already "
                    f"not in the configuration. Nothing changed.")
                if _ok:
                    # ⭐ 与 Nano 自己动手那条**同一个出口** —— 见 `_note_mcp_change`
                    self._note_mcp_change("delete", _srv, by="nano")
            except Exception as e:
                logger.warning(f"[B3] 删除 MCP {_srv} 失败: {e}")
                _mcp_facts = (f"Deleting MCP server {_srv!r} failed with: {e}. "
                              f"Report the failure as-is; do not soften it.")
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       _mcp_facts
                   ),
                   "log": f"MCP 管理确认 {iid} 已执行。"}
            return

        if rec.kind == _it.Kind.SKILL_MANAGE:
            # 用打字确认 Skill 管理操作（删除 / 禁用 / 启用 / 改写）。
            # CANCEL 已在上面通用分支处理；这里只处理"确认执行"。
            _p = rec.payload or {}
            _op = (_p.get("op") or "").strip()
            _sk = _p.get("skill") or rec.artifact_id or ""
            logger.info(f"[Interaction] 管理确认 {iid} → 用户确认 {_op} {_sk}")

            if _op == "update_skill":
                # 改写确认 → 走既有的生成通路。
                # ANSWER_AND_AMENDMENT 时把用户的补充意见一并带上。
                _rt_close_skill_manage(_sk, approved=True)
                self._pending_action = None
                self._pending_action_at = 0.0
                _extra = ""
                if relation == _it.Relation.ANSWER_AND_AMENDMENT:
                    _extra = ("\n\n[User Additional Requirement On Top Of The Confirmed Plan]\n"
                              f"{answer}")
                async for step in self._generate_skill_update(
                    _p.get("query") or _sk, _sk, base_guide, realtime_callback,
                    change_summary=(_p.get("summary") or "") + _extra,
                ):
                    yield step
                return

            # delete / disable / enable → 走既有的 registry 操作
            if _op not in {"delete", "disable", "enable"}:
                logger.error(f"[Interaction] 管理确认 {iid} 的 op={_op!r} 不认识")
                _rt_close_interaction(iid, _it.CANCEL,
                                      resolution=_it.Resolution.USER_CANCELLED)
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The stored management operation {_op!r} is not one of delete / disable "
                           f"/ enable, so nothing was executed. This is an internal gap on our side, "
                           f"not the user's mistake. Ask them to just say what they want, and you "
                           f"will do it again."
                       ),
                       "log": f"管理确认 {iid} op 不识别：{_op}"}
                return

            _rt_close_skill_manage(_sk, approved=True)
            self._pending_action = None
            self._pending_action_at = 0.0
            try:
                if _op == "delete":
                    # ⚠️ 方法名是 `delete_skill_file`，不是 `delete_skill`
                    # （第一版写错了，编译期发现不了 —— 属性访问是运行时解析的）。
                    _r = self.registry.delete_skill_file(_sk)
                elif _op == "disable":
                    _r = self.registry.disable_skill(_sk)
                else:
                    _r = self.registry.enable_skill(_sk)
                _skill_manage_facts = (
                    f"Operation {_op!r} on Skill {_sk!r} completed "
                    f"(ok={(_r or {}).get('ok', True)}). Raw system message, as "
                    f"evidence: " + repr((_r or {}).get("msg") or "") + ".")
            except Exception as e:
                # ⚠️ 如实报错，不要换成"操作遇到了问题"（设计原则 3 的推论）。
                logger.error(f"[Interaction] 管理操作 {_op} {_sk} 失败: {e}")
                _skill_manage_facts = (
                    f"Operation {_op!r} on Skill {_sk!r} failed with: {e}. "
                    f"Report it as-is - do not replace it with a vague "
                    f"\"something went wrong\".")
            from core import skill_watch as _skw
            _skw.request_reload(f"Skill 管理操作 {_op}")
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       _skill_manage_facts
                   ),
                   "log": f"管理确认 {iid} 执行 {_op}。"}
            return

        if rec.kind != _it.Kind.SKILL_CLARIFICATION:
            # 走到这里说明有人加了新 kind 却没加分支。响亮地记一条，
            # 并且**不要**假装处理完了 —— RESOLVE 会让模型以为事情办了。
            logger.error(
                f"[Interaction] {iid} 的 kind={rec.kind} 没有 continuation 分支 —— "
                f"这是代码缺口，不是用户输入问题"
            )
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"Their answer was recorded, but there is no handler for to-do kind "
                       f"{rec.kind!r} - that is an implementation gap on our side, not their "
                       f"mistake. Say so honestly and invite them to restate what they want; "
                       f"you will handle it as a normal request."
                   ),
                   "log": f"kind={rec.kind} 无分支（代码缺口）。"}
            return

        # ══════════════════════════════════════════════════════════════════
        # 创建澄清的续接 —— **2026-08-13：不再重跑子循环，把事实交回主 ReAct**
        # ══════════════════════════════════════════════════════════════════
        #
        # 🔴 旧实现在这里硬编码 `self._run_skill_exploration(...)` —— 探索拆掉之后
        #    这一段必须改，它是整次拆除里**唯一一处不改就会直接崩**的地方。
        #
        # ⭐ 新链条（不发明新机制，用 已有的 `exit_flow_defer_to_model`）：
        #
        #     用户回答 → answer_open_interaction
        #              → 答案先原子落盘（ANSWERED）
        #              → 把 checkpoint + 用户回答作为【事实】交回主 ReAct
        #              → 主模型重新决策：
        #                   还缺信息   → create_new_skill(open_questions=[...])
        #                   已经清楚   → create_new_skill(open_questions=[])
        #                   应该改已有 → update_existing_skill
        #                   现成的够用 → 直接调那个 Skill
        #
        # 📌 **这比"重新造一个 Explorer"干净**：续接需要的从来不是一个子循环，
        #    而是「上次问到哪了」这份领域状态 —— 而那份状态就在 checkpoint 里。
        # ⭐ 而且它顺带解锁了旧实现做不到的两条出口：旧的只能重跑 Explorer
        #    （出口只有 create），现在用户答完之后模型可以改走 update、甚至发现
        #    现成 Skill 就够用。📌 **拆掉一个封闭作用域，出口数量是增加的。**
        #
        # ⚠️⚠️ **收尾时机必须保住原语义**：`OPEN → ANSWERED → (续接成功) → RESOLVED`。
        #    这里**刻意不 RESOLVE** —— 「把事实交回主模型」**不等于**领域工作已经续接成功。
        #    如果紧接着 provider 报错、或模型没接住，状态停在 ANSWERED，
        #    下一轮 `[Open Interactions]` 会带着用户原话让它幂等重试，
        #    **不要求用户重说一遍**。那正是这条状态流当初存在的全部理由。
        # 📌 **判据：一个"已回答"的记录，要等到它引发的领域动作真的产生了新状态，
        #    才算续接完成 —— 把消息递出去不算。**
        #
        # ⭐ 那么谁来把它收成 RESOLVED？—— **产生新状态的那一方**：
        #    · 模型再调 `create_new_skill` 且无未决问题 → `_rt_supersede_covered_clarifications`
        #      按需求重叠度把它标成 SUPERSEDED（既有机制，实测 挣来的）
        #    · 模型再次自报未决问题 → 开一条**新的**澄清，旧的同样被上面那条收掉
        #    · 澄清有 TTL，最坏情况由过期兜底，不会变僵尸
        _p = rec.payload or {}
        _orig = _p.get("original_requirement") or answer
        # ⚠️ 兼容读：老记录里这个字段叫 `last_explorer_message`（见 `_rt_open_clarification`
        #    的迁移说明）。新写入一律是 `last_creation_note`，但老的 Interaction
        #    可能还活在库里，读的时候两个都认。
        _note_prev = (_p.get("last_creation_note")
                      or _p.get("last_explorer_message") or "").strip()

        _facts = (
            f"The user has answered a pending clarification about creating a Skill.\n\n"
            f"[Original requirement]\n{_orig}\n\n"
            + (f"[What you had established last time]\n{_note_prev}\n\n" if _note_prev else "")
            + f"[The user's answer, verbatim]\n{answer}\n\n"
            + "Decide what to do now with this answer. If everything is clear, call "
              "create_new_skill with an empty open_questions. If something still blocks you, "
              "call it again with the remaining questions. If it turns out an existing Skill "
              "should be updated instead, use update_existing_skill; if one already fits, "
              "just call it. Do not ask the user to repeat what they already said above."
        )
        # ⚠️ 字段名是 **`tool_result`**（`_take_defer` 读的就是它），不是 `facts`。
        #    第一版按印象写了 `facts` —— 那样事件会被拦下、内容却是空的，
        #    表现为「模型收到一条空的 tool_result，然后不知道该干嘛」。
        #    📌 又一次：**事件的字段名要回代码核，不能凭印象**（同那次 `_rt_live_interactions`）。
        logger.info(f"[Interaction] 澄清 {iid} 的回答已交回主 ReAct（{len(_facts)} 字符事实）")
        yield {"event": "exit_flow_defer_to_model", "tool_result": _facts,
               "log": f"澄清 {iid} 已回答，交回主决策继续。"}

    async def _handle_ask_user_choice(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        # 交互工具：暂停等用户选择。支持一次多张卡片（questions 数组）。
        _raw_qs = args.get("questions")
        if isinstance(_raw_qs, list) and _raw_qs:
            _q_specs = _raw_qs[:4]   # 最多 4 张
        else:
            _q_specs = [{
                "question": args.get("question", "Please choose"),
                "choices": args.get("choices", []),
                "allow_custom": args.get("allow_custom", True),
            }]
        _n = len(_q_specs)
        _UNANSWERED = object()
        _c1_results: list[Any] = [_UNANSWERED] * _n
        _c1_ev = asyncio.Event()
        _c1_loop = asyncio.get_running_loop()

        def _maybe_done():
            if all(r is not _UNANSWERED for r in _c1_results):
                _c1_loop.call_soon_threadsafe(_c1_ev.set)

        def _make_on_choice(i):
            def _h(selected):
                _c1_results[i] = selected
                _maybe_done()
            return _h

        def _make_on_dismiss(i):
            def _h():
                _c1_results[i] = "__dismissed__"
                _maybe_done()
            return _h

        from core.runtime import replies as _replies
        _cards = []
        _rids = []
        for _i, _spec in enumerate(_q_specs):
            _rid = _replies.register({"choice": _make_on_choice(_i),
                                      "dismiss": _make_on_dismiss(_i)})
            _rids.append(_rid)
            _cards.append({
                "question": _spec.get("question", "Please choose"),
                "choices": _spec.get("choices", []),
                "allow_custom": _spec.get("allow_custom", True),
                "reply_id": _rid, "actions": ["choice", "dismiss"],
            })

        await event_queue.put({"event": "user_choice_request", "cards": _cards})
        from core.runtime import inbox as _ib6
        # ⭐ 选择卡也走双路：用户改口说话时不该继续干等五分钟。
        #    ⚠️ 这一处**不改判据** —— 未答的问题原本就按「用户跳过」处理，
        #       用户说话只是让它**提前**走到那个已有的分支。
        try:
            await _ib6.wait_confirm_or_user_message(_c1_ev, 300)
        finally:
            for _rid in _rids:
                _replies.discard(_rid)

        def _fmt_one(spec, val):
            q = spec.get("question", "Choice")
            if val == "__dismissed__":
                return f"Question \"{q}\": the user dismissed the choice card. You may decide yourself or ask again."
            if val is None or val is _UNANSWERED:
                return f"Question \"{q}\": the user skipped. Choose one option yourself and explain your choice."
            return f"Question \"{q}\": the user chose \"{val}\"."

        if _n == 1:
            _v = _c1_results[0]
            if _v == "__dismissed__":
                return ("The user dismissed the choice card. You may decide how to "
                        "respond: choose an option and explain it, or ask again.")
            if _v is None or _v is _UNANSWERED:
                return ("The user clicked Skip. Choose one of the options yourself, "
                        "continue, and tell the user which one you chose.")
            return f"The user chose: {_v}"
        return "User choices for each card:\n" + "\n".join(
            _fmt_one(_q_specs[_i], _c1_results[_i]) for _i in range(_n)
        )

    async def _exit_answer_open_interaction(self, exit_call, decision, *, used_model,
                                            base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_answer_interaction(
            exit_call.args or {}, base_guide, realtime_callback, event_queue
        ):
            yield _ev
            for _q in self._drain_event_queue(event_queue):
                yield _q
