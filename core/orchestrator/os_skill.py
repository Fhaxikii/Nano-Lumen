# core/orchestrator/os_skill.py
"""OS Skill 的计划执行（DSL 步骤、重规划、兜底答复）与 canary 自检。（`Orchestrator` 的 mixin）"""

import os

from loguru import logger

from core.i18n import language_clause as _language_clause
from core.orchestrator._runtime import (
    _rt_lease_heartbeat,
    _rt_machine_is_free,
    current_agent_label,
)


class OsSkillMixin:
    """OS Skill 的计划执行（DSL 步骤、重规划、兜底答复）与 canary 自检。"""

    def _recent_user_messages(self, limit: int = 6) -> list:
        """最近若干条**用户自己说的话**。给危险判定用。

        ⚠️ **只收 role == "user"** —— 模型的话、工具输出一律不进来。
        📌 照官方那句 `reasoning-blind by design`：判定器要核的是
           「动作 vs 用户意图」，而模型的推理正是**被核的对象**，不能自辩；
           工具输出则是**注入的载体**，让它进判定器等于把闸交给攻击者。
        """
        out = []
        try:
            for m in reversed(getattr(self.memory, "storage", []) or []):
                if getattr(m, "role", "") != "user":
                    continue
                c = getattr(m, "content", None)
                if isinstance(c, str) and c.strip():
                    out.append(c.strip())
                if len(out) >= limit:
                    break
        except Exception as e:
            logger.debug(f"[CmdClassifier] 取用户消息失败: {e}")
        return list(reversed(out))

    async def _auto_gate_verdict(self, action: str, params: dict,
                                 main_model: str = "") -> tuple:
        """auto 模式下这一步能不能自动放行。返回 `(auto_ok, gate_by, reason)`。

        ⭐⭐ 四态：
        ```
        A  不是 run_command            → auto_ok=True   零 token
        B  判定：安全                  → auto_ok=True   有 token
        C  判定：危险                  → auto_ok=False  弹窗，说清它要做什么
        D  判定：判不了                → auto_ok=False  弹窗，说清**为什么判不了**
        ```
        ⚠️ C 与 D 必须分开：C 说「它要删/装/改什么」，D 说「我读不了这个脚本」。
           📌 把 D 说成 C 是在冤枉一条可能完全无害的命令，
              而用户点几次之后就会开始无脑点 —— 那时这道闸就白做了。

        🔴 **失败方向一律朝 False（弹窗）倒**：没配判定模型、API 失败、
           取不到用户消息、判定器自己抛异常 —— 全部落 D。
           📌 同 `required_permissions()` 对未知 action 返回总闸那条：
              fail-safe 朝「多要一道」错，不朝「谁都不管」错。
        """
        if action != "run_command":
            return (True, "", "")            # A：不进判定
        try:
            from core.os_layer import cmd_classifier as _cc
            _v, _why = await _cc.classify(
                getattr(self, "provider", None),
                command=str((params or {}).get("command", "")),
                user_messages=self._recent_user_messages(),
                # ⚠️ 主模型 id 决定用**同厂**的哪个判定模型（models.classifier_for）。
                #    三级兜底：本轮实际用的 → orchestrator 上的 → provider 上的。
                #    📌 取不到也没关系：`classifier_for("")` 返回空串 = 不启用 = 落 D。
                main_model=str(main_model
                               or getattr(self, "target_model", "")
                               or getattr(self.provider, "target_model", "")),
            )
        except Exception as e:
            logger.warning(f"[CmdClassifier] 判定异常，按需要确认处理: {e}")
            return (False, "classifier", f"判定器异常（{type(e).__name__}）")
        if _v == _cc.ALLOW:
            return (True, "", "")            # B
        if _v == _cc.BLOCK:
            return (False, "classifier", _why or "这条命令与你的要求对不上")   # C
        return (False, "classifier", _why or "无法判断这条命令是否符合你的要求")  # D

    async def _execute_dsl_step(self, instr: dict, dispatcher, safety, used_model: str):
        """执行一条 DSL 指令，处理两段式确认流程（risk=1 直接放行，risk>=2 弹窗）。

        从原单次执行逻辑里抽出来，供：
          1. _handle_os_task 的"简单任务快路径"（单条 os_execute 调用）
          2. 的 Plan-Execute-Replan 循环（逐条执行 OS Skill 吐出的 dsl_plan）
        两处复用，避免confirm弹窗这套同步等待逻辑写两份。

        yields: 中间 UI 事件（os_action_confirm 等），调用方原样转发给上层。
        最后一定 yield 一个 {"_step_result": result} 哨兵，调用方据此取出
        最终执行结果，自己决定怎么回复用户/要不要继续下一步——这个函数本身
        不 yield final_result，不替上层做决策（实现约束2：只报状态不决策）。
        """
        import asyncio as _asyncio
        confirmed = True
        result = None

        # ⚠️ 每走一步给镜像租约续期。**这条不能省。**
        # 一次 GUI 自动化可能跑几分钟，租约不续就会到期，于是一个**完全正常**的
        # 长任务被记成"泄漏" —— 那是假阳性，而假阳性是 shadow 最坏的一种失败
        #（早先刚栽过：它不漏问题，但会训练出"这个报警不用看"）。
        _rt_lease_heartbeat(self)

        async for ev in dispatcher.execute(instr):
            if ev["type"] == "confirm_request":
                action    = ev["action"]
                risk      = ev["effective_risk"]
                p_summary = ev.get("params_summary", "")
                reason    = ev.get("reason", "")
                annotated = ev.get("annotated_image_path", "")
                resolved_instr = ev.get("_resolved_instr", instr)

                _confirm_ev = _asyncio.Event()
                _user_choice = [None]
                _loop = _asyncio.get_running_loop()

                def _on_confirm():
                    _user_choice[0] = True
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                def _on_always():
                    _user_choice[0] = "always"
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                def _on_cancel():
                    _user_choice[0] = False
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                # ⭐⭐ [B] **auto 走一个【不同的】回调，不是复用 `_on_confirm`。**
                #    🔴 复用的话，「用户亲自点了同意」和「auto 替用户点了」在这一层
                #       完全无法区分 —— 而它们对模型是两件事：
                #       前者是一次真实的人类判断，后者是"这一轮压根没人被问过"。
                #    📌 **一个字段如果要回答「谁批的」，那么两个批准者就必须
                #       走两条能被分辨的路。**
                def _on_auto():
                    _user_choice[0] = "auto"
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                # 对 run_command/file_write，把原始内容传给弹窗用于代码预览
                _raw_params = resolved_instr.get("params", instr.get("params", {}))
                # ⭐ 这个动作是谁发起的 —— **UI 只多画一行，规则一个字不改**
                #    （2026-08-20：「其他的完全复用 main agent 的即可，
                #     本身怎么授权它就怎么授权」）。
                #    📌 而它必须**能被看出来**：一个后台执行体发起的写操作，
                #       如果在弹窗上和你自己那一轮长得一模一样，
                #       用户就没有办法判断"这是我刚让它做的吗"。
                _agent_label = current_agent_label()
                # ⭐⭐ auto 模式下的危险判定（四态）。**只在 auto 开着时才花这个钱** ——
                #    ask permission 模式本来就每条都问，判定一分钱不值。
                _auto_ok, _gate_by, _gate_why = True, "", ""
                try:
                    from core.os_layer import dsl as _dsl_g
                    if _dsl_g.auto_authorization_on():
                        _auto_ok, _gate_by, _gate_why = await self._auto_gate_verdict(
                            action, _raw_params, used_model)
                except Exception as _e_g:
                    # 🔴 连"要不要判"都判不了 → 当作需要确认（fail-safe）
                    logger.warning(f"[CmdClassifier] 闸前置异常: {_e_g}")
                    _auto_ok, _gate_by, _gate_why = False, "classifier", "判定前置异常"
                _reasons = list(ev.get("risk_reasons", []) or [])
                if _gate_why:
                    # ⭐ 复用弹窗现成的「风险原因」那一栏 —— 不新增 UI 通道。
                    #    📌 一个只多一行文字的需求，不该换来一条新的展示管线。
                    _reasons.append(f"[自动放行被拦下] {_gate_why}")
                yield {
                    "event": "os_action_confirm",
                    "action": action, "effective_risk": risk,
                    # ⚠️ **判据是「明确为 True 才放行」，不是「没被拦就放行」** ——
                    #    上游要是漏传了这个键，行为退化成"照常弹窗"，而不是静默执行。
                    "auto_ok": _auto_ok, "gate_by": _gate_by,
                    "risk_reasons": _reasons,
                    "params_summary": p_summary, "reason": reason,
                    "params_raw": _raw_params,
                    "annotated_image_path": annotated,
                    "agent_label": _agent_label,
                    "on_confirm": _on_confirm, "on_always": _on_always,
                    "on_cancel": _on_cancel, "on_auto": _on_auto,
                }

                from core.runtime import inbox as _ib6
                # ⭐⭐ Subagent发起的确认**只认这个弹窗自己的回应**（+ 超时兜底）。
                #    理由见 `wait_confirm_or_user_message` 的 docstring：
                #    📌 一个「取消」的信号，必须来自它要取消的那件事的同一条注意力。
                _oc6 = await _ib6.wait_confirm_or_user_message(
                    _confirm_ev, 300,
                    cancel_on_user_message=not _agent_label)
                if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                    # 🔴 **告诉 UI 把那个弹窗收掉**。
                    #    用户改口说话 / 干等超时，这两条路上**没有人点过按钮**，
                    #    而弹窗只在按钮里 `close()` —— 于是它活了下来，
                    #    屏上写着"待授权"，按钮指向一个已经结束的等待。
                    # 📌 **一个只能被自己的按钮关掉的弹窗，一定会在
                    #    「不是按按钮」的那些结束路径上活下来** ——
                    #    而那些路径恰恰是用户没在看它的时候。
                    try:
                        yield {"event": "confirm_dismiss",
                               "why": str(getattr(_oc6, "value", _oc6))}
                    except Exception:
                        pass
                    # ⭐⭐ 用户没点按钮 —— 要么改口说话了，要么干等超时。
                    #    两者都 **不执行**，但**要告诉模型的话不同**，所以不许压成一个布尔。
                    #    📌 「用户改口说别做了」和「等了五分钟没人管」在结果上都是不执行，
                    #       语义完全不同 —— 同 ActionAttempt 那条：一个字段不许表达两个现实。
                    _user_choice[0] = False

                choice = _user_choice[0]
                confirmed = choice in (True, "always", "auto")

                if not confirmed:
                    await dispatcher.execute_after_confirm(resolved_instr, confirmed=False)
                    # ⚠️ 原来这里恒定是「用户已取消」——**那在两种情况下都是错的**：
                    #    干等超时时没人取消过；用户改口说话时也没"取消"，
                    #    那是**换了个话题**。
                    #    📌 **一句给模型看的话，宁可承认「不知道为什么」，
                    #       也不许替用户编一个用户没做过的动作。**
                    # ⭐ [A] 显式点「拒绝」—— 改造前这里只有"用户已取消"五个字，
                    #    而旁边两条（改口 / 超时）都是完整句。
                    #    📌 三条出口里，**真正的"用户拒绝"反而是说得最不清楚的那条** ——
                    #       而它是唯一一条模型必须停手的。
                    #    ⚠️ 必须明说「不要重试」：这是一次人的判断，不是一次故障。
                    _err6 = ("Denied by the user: they looked at this action and "
                             "said no. This is a decision, not a failure - do NOT "
                             "retry it and do NOT look for a way around it. "
                             "Tell the user plainly that you did not do it, and "
                             "let them decide what happens next.")
                    if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                        _err6 = _ib6.cancelled_by_user_message_note()
                    elif _oc6 == _ib6.ConfirmOutcome.TIMEOUT:
                        _err6 = ("Not executed: the confirmation dialog timed out "
                                 "after 300s with no answer. Nobody cancelled it — "
                                 "the user simply never responded.")
                    result = {"ok": False, "action": action, "effective_risk": risk,
                              "is_control_flow": False, "data": {}, "summary": "",
                              # ⭐ 结构化那一份 —— 消费方不该去解析一段英文
                              "authorized_by": (
                                  "user_denied"
                                  if _oc6 == _ib6.ConfirmOutcome.CONFIRMED
                                  else ("cancelled_by_user_message"
                                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE
                                        else "confirm_timeout")),
                              "error": _err6}
                    break

                result = await dispatcher.execute_after_confirm(resolved_instr, confirmed=True)
                if isinstance(result, dict):
                    result["authorized_by"] = {
                        True: "user_once", "always": "user_always", "auto": "auto",
                    }.get(choice, "user_once")
                # 预授权在执行成功后写入，且按参数范围（scope_key）限定粒度，
                # 防止"允许写A文件"扩散到"允许写B文件"，也防止执行失败仍留下预授权
                # ⚠️⚠️ **`auto` 不许写预授权。** 它是「这一轮不问」，
                #    而预授权是「以后都不问」—— 📌 一个临时的豁免如果顺手落成永久的，
                #    那么关掉 auto 之后它还在，而用户以为自己关掉了。
                if choice == "always" and result.get("ok"):
                    from core.os_layer.dispatch import _derive_auth_scope
                    _scope = _derive_auth_scope(action, resolved_instr.get("params") or {})
                    safety.pre_authorize(action, risk, _scope)
                break

            elif ev["type"] == "result":
                result = ev
                break

        if result is None:
            result = {"ok": False, "error": "调度器无返回"}

        # ⭐⭐⭐ [2026-08-24] **OS 动作产生的图，一律上屏。**
        #
        # 🔴 问题：`computer_use → screenshot` 把图**存进了 os_audit，但一个像素都没给用户看** ——
        #    实测里 Nano 只回了一句「截图保存在 shot_xxx.png」，
        #    用户完全不知道它到底看到了什么。
        # ⭐ 用户的判据（原话）：「**不管是不是凭据、落盘不落盘，这两种 UI 上都要体现**，
        #    不然用户不知道 nano 看到了什么 —— 这跟 pill 显示、可下拉是一个道理」。
        #    📌 **「Nano 此刻看到/在做什么」是一个整体，不该按实现细节（存不存盘）分叉。**
        # ⚠️ 顺带纠正一个不对称：`look_at_screen` **上屏但不落盘**，
        #    `computer_use.screenshot` **落盘但不上屏** ——
        #    📌 两条路都叫「截图」，却各缺对方的一半，而用户要的恰恰是重合的那部分。
        #
        # ⚠️ 走**同一个** `screenshot_preview` 事件（它在 `_CHAT_EVENTS_EPHEMERAL` 里：
        #    重启不回放，2026-08-13 已定）—— 不新增通道。
        # ⚠️ **缩到 1568 再 base64**：原图 2MB 的 PNG 变成 2.7MB 的 data URI 塞进 DOM
        #    是纯浪费，而这里只是给人看一眼。
        # ⚠️ 整段吞异常：它是**展示**，📌 一个用来展示的动作不许成为失败源
        #    （那条，本项目栽过六次）。
        try:
            # 🔴 [2026-08-24] **只上屏「截图动作」产的那张，
            #    定位（locate）那张不上屏。**
            #    第一版把 `locate.screenshot_ref` 也画了出去 ——
            #    结果一轮里四张几乎一模一样的全屏图刷过去。
            #    「虽然这确实是事实，但是 **UI 污染太严重**」。
            #    📌 **真实不等于该显示** —— 一堆长得一样的图不会让用户多知道一件事，
            #       只会把真正要看的那一张淡化掉。
            # ⭐ 分界很清楚：
            #    · `screenshot` 动作   —— Nano **主动看了一眼**，用户该看见  ✅
            #    · locate 的截图   —— 是执行一个点击的**内部过程**，不上屏  ❌
            #      （它仍然落盘进 os_audit，凭据一张没少；确认弹窗里的**标注图**也照旧显示）
            _img_p = ""
            _data = result.get("data") if isinstance(result, dict) else None
            if isinstance(_data, dict):
                _img_p = str(_data.get("path") or "")
            if _img_p and _img_p.lower().endswith(".png") and os.path.exists(_img_p):
                import base64 as _b64
                from PIL import Image as _Im
                import io as _io
                _im = _Im.open(_img_p).convert("RGB")
                if _im.width > 1568:
                    _im = _im.resize((1568, max(1, int(_im.height * 1568 / _im.width))),
                                     _Im.LANCZOS)
                _buf = _io.BytesIO(); _im.save(_buf, "PNG")
                # 🔴 这里第一版写的是 `action` —— **那个名字只在确认分支里绑定**
                #    （`action = ev["action"]`）。而 `screenshot` 是 risk=1、**不弹窗**，
                #    走的正好是没绑定的那条路 → `NameError`，
                #    而它会被下面那个 `except` **吞成一条 debug 日志** ⇒
                #    表现就是「图永远不出现」。
                # 📌 跟启动恢复那条 `self._startup_…` 同一个形状：
                #    **一条被吞掉的 NameError，表现成的是「功能没生效」，
                #    而不是「有人写错了变量」。**
                # ⭐ 改读 `instr` —— 它是入参，**每一条路上都在**。
                yield {"event": "screenshot_preview",
                       "png_b64": _b64.b64encode(_buf.getvalue()).decode(),
                       "purpose": str((instr or {}).get("action") or "screenshot")}
        except Exception as _e_shot:
            logger.debug(f"[OS] 截图上屏跳过（不影响执行）: {_e_shot}")

        yield {"_step_result": result}

    async def _replan_os_skill(self, skill_name: str, original_args: dict,
                                completed_steps: list, failed_instr: dict,
                                failed_result: dict) -> list | None:
        """OS Skill 执行中途定位失败，带着失败上下文重新调用 Skill 的 run()
        拿到更新后的 dsl_plan。

        返回新的 dsl_plan（list[dict]）；Skill 没正确返回则返回 None，
        由调用方决定走熔断话术。
        """
        import json as _json
        os_context = {
            "completed_steps": completed_steps,
            "failed_step": failed_instr,
            "failure_reason": failed_result.get("error", ""),
            "locate_status": failed_result.get("locate_status") or
                             (failed_result.get("data") or {}).get("locate_status", ""),
        }
        replan_args = dict(original_args or {})
        replan_args["os_context"] = _json.dumps(os_context, ensure_ascii=False)
        raw_result = await self.registry.execute(skill_name, replan_args)
        if raw_result is None or not hasattr(raw_result, "data"):
            logger.warning(f"[OS-Replan] Skill「{skill_name}」replan 调用未返回有效 SkillResult")
            return None
        plan = (raw_result.data or {}).get("dsl_plan")
        if not isinstance(plan, list):
            logger.warning(f"[OS-Replan] Skill「{skill_name}」replan 后 dsl_plan 不是合法列表")
            return None
        return plan

    async def _final_answer_or_fallback(
        self, *, facts: str, fallback: str, base_guide: str,
        used_model: str, log: str, status: str = "SYS_IDLE", extra: dict | None = None,
    ):
        """让模型用自己的话说一句收尾，模型不可用时退回 `fallback`。

        ⭐ 提出来的原因：`_run_os_skill_plan_loop` 里有 5 个出口形状完全一样
           —— 先设兜底、跑 `_stream_final_answer`、拿不到就用兜底、最后发终端事件。
           抄 5 遍的代价不是行数，是**5 份各自会漂移的兜底逻辑**。

        ⚠️ `fallback` **保留中文固定文案，这是对的** ——
           它只在模型不可用时才登场，而那时没有任何东西能生成它。
           📌 豁免第④条（模型故障兜底）。**能被模型说的话才归模型**。

        ⚠️ `facts` 用英文（注入模型的文本一律英文），语言由
           `language_clause()` 决定 —— 提示词里不写死任何一种语言。
        """
        content = fallback
        self._last_stream_final_bid = None
        try:
            async for _ev in self._stream_final_answer(
                self._build_pipeline_context(),
                base_guide + f"""

{facts} {_language_clause("your reply")}""",
                task_type="结果总结",
            ):
                yield _ev
            content = self._last_stream_final_text or fallback
        except Exception as _e:
            logger.warning(f"[L12] 收尾话术生成失败，退回兜底: {_e}")
            content = fallback
            self._last_stream_final_bid = None
        self.memory.add_message("assistant", content)
        _ev_out = {"event": "final_result", "content": content, "model": used_model,
                   "status": status, "log": log,
                   "current_skill": None, "rag_hit": False, "full_file_hit": False,
                   "block_id": self._last_stream_final_bid}
        # ⚠️ 用来透传调用点自己的字段（rag_hit / full_file_hit 这类）——
        #    📌 提取公共形状时，**别把调用点的差异一起抹平**：
        #       抹平的那些字段不会报错，只会悄悄变成默认值。
        _ev_out.update(extra or {})
        yield _ev_out

    async def _run_os_skill_plan_loop(self, skill_name: str, initial_plan: list,
                                       original_args: dict, dispatcher, safety,
                                       used_model: str, base_guide: str, realtime_callback):
        """补丁C.2-C.4：Plan-Execute-Replan 循环。仅在 _handle_os_task 内部持有，
        不是全局编排器（补丁C.5：回流主体是"单个 OS Skill 执行中途的视觉纠偏"，
        不是"任务编排"，不会重蹈已删除的多步 Plan 覆辙）。

        逐条执行 initial_plan；遇到 NOT_FOUND/AMBIGUOUS/OCCLUDED 时（执行层
        的三级降级已经在 dispatcher 内部做过，这里收到的已经是降级后仍失败的
        结果）调用 _replan_os_skill 重新规划剩余步骤；step/replan 次数用
        safety 已有的计数器熔断。
        """
        MAX_STEPS = 30
        plan_queue = list(initial_plan or [])
        completed_steps: list = []

        while plan_queue:
            if safety.increment_step() > MAX_STEPS:
                async for _ev in self._final_answer_or_fallback(
                        facts=("The task was stopped because it needs more steps than one run allows. ""Nothing further was attempted. Tell the user, and suggest splitting it ""into a few smaller requests."),
                        # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                        fallback="这个任务步骤太多，超过了单次操作上限，我先停下来——可以拆成几次跟我说。",
                        base_guide=base_guide, used_model=used_model,
                        log="OS Skill：步数熔断。"):
                    yield _ev
                return

            instr = plan_queue.pop(0)
            result = None
            async for ev in self._execute_dsl_step(instr, dispatcher, safety, used_model):
                if "_step_result" in ev:
                    result = ev["_step_result"]
                else:
                    yield ev

            if result.get("is_control_flow") and result.get("data", {}).get("reason") == "USER_ABORT":
                # 软急停已删除，模型不再被教导发这个 reason。这里保留只作兜底：
                # 万一模型自己发了，就当"停掉本次 plan"处理，不再污染会话级状态。
                async for _ev in self._final_answer_or_fallback(
                        facts=("The user asked to stop, so the operation was halted here. ""Acknowledge briefly - do not re-offer to continue unless they ask."),
                        # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                        fallback="好，这次的操作我停在这儿了。",
                        base_guide=base_guide, used_model=used_model,
                        log="OS Skill：模型请求终止本次 plan。"):
                    yield _ev
                return

            if result.get("ok"):
                completed_steps.append(instr)
                continue

            if result.get("aborted") or result.get("error") == "用户已取消":
                async for _ev in self._final_answer_or_fallback(
                        facts=("The user asked to stop mid-operation, so the current step was cut short ""and did not finish. Acknowledge briefly and say the step was left ""incomplete."),
                        # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                        fallback=("好，我停手了——刚才那个操作没做完就中断了。" if result.get("aborted")
                           else "好，这步我不执行了，整个任务也先停在这里。"),
                        base_guide=base_guide, used_model=used_model,
                        log="OS Skill：用户中断。"):
                    yield _ev
                return

            _locate_status = result.get("locate_status") or (result.get("data") or {}).get("locate_status")
            if _locate_status in ("NOT_FOUND", "AMBIGUOUS", "OCCLUDED"):
                if safety.increment_replan() > 3:
                    async for _ev in self._final_answer_or_fallback(
                            facts=(f"The step {instr.get('reason') or instr.get('action')!r} failed after "f"several retries, so the task stopped there. It may be outside what can "f"be done automatically. Tell the user, and offer either that they do "f"this one step by hand, or that they describe the target more precisely."),
                            # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                            fallback=(f"这一步「{instr.get('reason') or instr.get('action')}」我反复试了几次"
                               "都没成功，可能这个任务超出我现在的能力范围了——可以麻烦你手动完成这一步，"
                               "或者换个说法告诉我具体在哪。"),
                            base_guide=base_guide, used_model=used_model,
                            log="OS Skill：连续失败熔断。"):
                        yield _ev
                    return

                yield {"event": "thinking", "log": "这一步没成功，正在重新规划剩余步骤...",
                       "status": "CORE_THINKING", "model": used_model,
                       "current_skill": "OSTask", "rag_hit": False, "full_file_hit": False}
                new_plan = await self._replan_os_skill(
                    skill_name, original_args, completed_steps, instr, result
                )
                if new_plan is None:
                    async for _ev in self._final_answer_or_fallback(
                            facts=("A step failed and no viable replanning was found, so the task stopped ""there. Say so plainly - do not pretend anything is still in progress."),
                            # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                            fallback="这一步没成功，我也没能重新规划出下一步该怎么做，先停在这里了。",
                            base_guide=base_guide, used_model=used_model,
                            log="OS Skill：replan 失败。"):
                        yield _ev
                    return
                plan_queue = new_plan
                continue

            # 其他失败（权限拒绝/执行异常等）——转写成人话告知，不再继续
            raw_error = result.get("error", "未知原因")
            self._last_stream_final_bid = None
            try:
                async for _ev in self._stream_final_answer(
                    self._build_pipeline_context(),
                    base_guide + (
                        # 🔴 改造前这是一段**中文提示词**，而且写死了「用简洁自然的
                        #    **中文**」—— 用户把界面切成英文，它照样命令模型说中文。
                        #    📌 提示词一律英文；语言由 language_clause() 决定，
                        #       **提示词里不写死任何一种语言**。
                        f"\n\nA system operation failed. Internal error, "
                        f"for your reference only:\n{raw_error}\n\n"
                        "Tell the user briefly and naturally that it did not work. "
                        "Do not copy the internal error text; explain roughly what "
                        "went wrong in terms they can understand. "
                        + _language_clause("your reply")
                    ),
                    task_type="结果总结",
                ):
                    yield _ev
                content = self._last_stream_final_text or f"这次操作没成功（{raw_error}）。"
            except Exception:
                content = "这次操作没成功，具体原因暂时说不清楚，可以再试一次或换个说法。"
                self._last_stream_final_bid = None
            self.memory.add_message("assistant", content)
            yield {"event": "final_result", "content": content, "model": used_model,
                   "status": "SYS_IDLE", "log": f"OS Skill 执行失败：{raw_error}",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False,
                   "block_id": self._last_stream_final_bid}
            return

        # 全部步骤跑完
        content = "好，这个任务我做完了。"
        self._last_stream_final_bid = None
        try:
            async for _ev in self._stream_final_answer(
                self._build_pipeline_context(),
                base_guide + (
                    # ⚠️ 同上：不在提示词里写死语言。
                    f"\n\nThe system operation task is fully complete "
                    f"({len(completed_steps)} steps). "
                    "Tell the user it is done, briefly and naturally; do not "
                    "recite every step. "
                    + _language_clause("your reply")
                ),
                task_type="结果总结",
            ):
                yield _ev
            content = self._last_stream_final_text or "好，这个任务我做完了。"
        except Exception:
            self._last_stream_final_bid = None
        self.memory.add_message("assistant", content)
        yield {"event": "final_result", "content": content, "model": used_model,
               "status": "SYS_IDLE", "log": f"OS Skill 执行完成（共{len(completed_steps)}步）。",
               "current_skill": "OSTask", "rag_hit": False, "full_file_hit": False,
               "block_id": self._last_stream_final_bid}

    def _get_canary(self):
        """懒加载 CanarySelfCheck，复用 VisionLocator/审计目录，避免重复初始化。
        初始化失败时把 self._canary 标记成 False（不是 None），下次直接跳过、
        不重试，避免每次定时器触发都重新尝试一次必然失败的初始化。"""
        if self._canary is not None:
            return self._canary if self._canary else None
        try:
            import json
            import pathlib
            from core.os_layer.canary import CanarySelfCheck
            from core.os_layer.executor_vision import VisionLocator
            from core.os_layer.audit import get_audit_logger
            audit = get_audit_logger()
            vision = VisionLocator(
                provider=self.provider, screenshot_dir=audit.screenshot_dir,
                model_override=None,
            )
            from core.paths import ROOT as _ROOT
            cfg_path = _ROOT / "config" / "os_config.json"
            canary_cfg = {}
            try:
                if cfg_path.exists():
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                    canary_cfg = data.get("canary") or {}
            except Exception:
                pass
            self._canary = CanarySelfCheck(vision, audit.screenshot_dir.parent, canary_cfg)
        except Exception as e:
            logger.warning(f"[Canary] 初始化失败，本次会话跳过自检: {e}")
            self._canary = False
        return self._canary if self._canary else None

    async def maybe_run_canary(self) -> None:
        """Canary self-check entry, periodically called by app.py.

        This method decides whether to run a lightweight self-check and whether
        the result should be escalated to a natural user-facing notice.
        All errors are swallowed so canary checks never affect the main flow.
        """
        try:
            canary = self._get_canary()
            if canary is None:
                return
            # ⚠️⚠️ **对答案已退役（2026-08-07），这里刻意不再调 `_rt_lease_compare_busy`。**
            #
            # 它在观测期的任务已经完成：量出了泄漏频率（16 次观测 13 次泄漏 / 61 分钟）、
            # 证实了止血有效。而后续把租约改成**一整段 GUI 操作**的粒度之后，
            # 旧 bool 仍然是**单步**粒度 —— 两者寿命不同了。
            #
            # 📌 **两个寿命不同的东西之间的"分歧"不是缺陷，是设计。**
            #    继续对答案，只会在每次两步之间的空档报一条"镜像没归还"，
            #    而那正是**假阳性** —— 早先、早先、早先已经各防过一次，
            #    这是第四次。假阳性不漏问题，但会训练出"这个报警不用看"。
            #
            # 📌 更一般的判据：**shadow 的对答案，只在两边建模同一个粒度时才有意义。**
            #    一旦新实现有意做得比旧的更细/更粗，旧的就不再是 oracle，该退役了。

            # ⭐⭐ 这一行就是 `_os_task_busy` 泄漏的**真修**。
            #
            # 旧写法 `canary.should_run(self._os_task_busy)` 的问题不是"这个 bool 会写错"，
            # 是**它没有过期概念** —— 一旦漏了一次 False，canary 就永久停摆且一声不响
            # （实测：按一下 Escape → 61 分钟没跑过，16 次观测 13 次判为泄漏）。
            # 租约靠 TTL 自愈：**不再依赖任何调用点记得配对**。
            #
            # ⚠️ 用的是 `machine_is_free()` 而**不是** `nano_may_touch_os()`
            #    （早先的设计原先写的是后者，是错的，已更正）——
            #    后者在"Nano 自己持有"时返回 True，照它写 canary 会在 GUI 自动化
            #    跑到一半时抢前台焦点，正是 canary 设计约束 ② 明令禁止的那件事。
            #
            # ⭐ 顺带白拿一条旧 bool 给不了的：**用户在用电脑时 canary 也不跑了**。
            #    旧 bool 只知道 Nano 忙不忙，不知道用户正在打字。
            if not canary.should_run(not _rt_machine_is_free()):
                return
            result = await canary.run_once()
            if result.get("escalate"):
                _raw = (
                    f"Screen target-location check: success rate {result['rate']:.0%}; "
                    f"below expectation for {result['consecutive_low']} consecutive checks."
                )
                try:
                    _prompt = (
                        f"You just completed a system self-check and found that screen target-location "
                        f"success rate has stayed low ({result['rate']:.0%}, "
                        f"{result['consecutive_low']} consecutive checks). "
                        f"Tell the user naturally and briefly that if recent screen operations keep failing, "
                        f"they can tell you or try restarting. "
                        f"Do not mention words like system self-check or canary."
                    )
                    content, _ = await self.provider.chat_without_tools(
                        context=[{"role": "user", "content": _prompt}],
                        system_guide="You are Nano, a desktop assistant. Be direct, brief, and natural.",
                    )
                    content = content.strip() if content else _raw
                except Exception:
                    content = _raw
                # 定死的一条：历史/事件记录的所有权只能属于一个层级，调用方和 emitter
                # 不能同时写。这里【曾经】自己 add_message 一次、再 _push_callback 一次，
                # 而 _proactive_push 内部又写一次 —— 同一条告警进历史两次（外部评审推演出来的，
                # 已回代码核实属实）。现在统一交给出口，这里只负责触发。
                logger.warning(f"[Canary] Escalated notice to user: {content}")
                try:
                    from core.health import get_system_events
                    get_system_events().add(f"Screen locate self-check degraded: {content}")
                except Exception:
                    pass
                if self._push_callback:
                    await self._push_callback(content)
        except Exception as e:
            logger.warning(f"[Canary] maybe_run_canary failed, ignored: {e}")

    def _detect_os_locate_followup(self, query: str) -> dict | None:
        """检测"上一次 OS 视觉定位失败 + 当前是简短纠正"。和
        _detect_skill_error_followup 同一设计：短句缺信息，靠"上一轮在干嘛"
        的待定状态续接，不依赖无状态的顶层分类器理解上下文。

        命中条件：
          1. self._last_os_locate_failure 存在（上一轮 click 等定位失败过）
          2. 当前 query 很短（复用 _ERROR_FOLLOWUP_MAX_LEN 同一个阈值），
             不像一个新的、独立的实质性请求
        """
        if len(query) > self._ERROR_FOLLOWUP_MAX_LEN:
            return None
        return getattr(self, "_last_os_locate_failure", None)
