# core/orchestrator/skill_lifecycle.py
"""待审 Skill 的保存 / 部署 / 取消，以及管理、更新已有 Skill 的决策。（`Orchestrator` 的 mixin）"""

import asyncio
import os
import time

from loguru import logger

from core import rag as rag_engine
from core.memory_store import EntryType
from core.orchestrator._runtime import _rt_close_skill_audit, _rt_open_skill_manage
from core.skill_check import validate_skill_code


class SkillLifecycleMixin:
    """待审 Skill 的保存 / 部署 / 取消，以及管理、更新已有 Skill 的决策。"""

    # Step 4：pending 状态超时常量（秒）。超过即视为脏数据自动清除。
    # 原值 5*60(5分钟)对"用户读完一段多段落计划/审计代码再回复"这类场景明显
    # 偏短——回复质量越高、内容越详实，用户思考时间越长，越容易撞到这条线。
    # 调到 30 分钟，覆盖绝大多数真实的"认真读完再回"节奏。
    PENDING_TIMEOUT_SECONDS = 30 * 60

    def _schedule_semantic_memory_write(self, coro) -> None:
        """语义记忆写入是旁路增强，不能阻塞/拖慢部署这个同步方法本身。
        用 create_task 丢进后台事件循环，调用方不等待结果。拿不到运行中的
        事件循环（极少见，比如纯脚本环境）就静默跳过，不报错。
        """
        try:
            import asyncio
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            logger.debug("[SemanticMemory] 没有运行中的事件循环，跳过本次写入调度")
        except Exception as e:
            logger.warning(f"[SemanticMemory] 调度写入任务失败（不影响部署主流程）: {e}")

    @property
    def _pending_skill(self) -> dict | None:
        """**最近一条**待审载荷的只读视图。

        存在的意义是让"只关心有没有待审"的十几处调用点不用改
        （`if self._pending_skill:` / 日志 …）。
        ⚠️ **需要指定具体哪一条时不要用它**，要显式传 filename ——
        用它就等于又回到了单槽假设。
        """
        for _fn in reversed(self._pending_skill_order):
            _p = self._pending_skills.get(_fn)
            if _p:
                return _p
        return None

    @_pending_skill.setter
    def _pending_skill(self, value: dict | None) -> None:
        """兼容旧写法。`= None` 表示清空全部；赋一个 dict 表示插入/覆盖同名那条。

        ⚠️ 保留 setter 只为兼容 `reset_conversation()` 之类的整体清空。
        **新代码请直接用 `_put_pending_skill()` / `_drop_pending_skill()`**，
        语义清楚得多。
        """
        if value is None:
            self._pending_skills.clear()
            self._pending_skill_order.clear()
            return
        self._put_pending_skill(value)

    def _put_pending_skill(self, payload: dict) -> None:
        """插入一条待审载荷。同名视为新版本覆盖（那是真的同一个 Skill 重新生成）。"""
        _fn = payload.get("filename") or ""
        if not _fn:
            logger.warning("[Skill] 待审载荷没有 filename，无法多槽存储，已忽略")
            return
        self._pending_skills[_fn] = payload
        if _fn in self._pending_skill_order:
            self._pending_skill_order.remove(_fn)
        self._pending_skill_order.append(_fn)

    def _get_pending_skill(self, filename: str | None = None) -> dict | None:
        """按名取；不给名字就取最近那条（UI 单弹窗时代的默认行为）。"""
        if filename:
            return self._pending_skills.get(filename)
        return self._pending_skill

    def _drop_pending_skill(self, filename: str) -> None:
        self._pending_skills.pop(filename, None)
        if filename in self._pending_skill_order:
            self._pending_skill_order.remove(filename)

    def apply_pending_skill(self, filename: str | None = None) -> dict:
        # 不传 filename = 最近那条（保持 UI 单弹窗时代的默认行为）。
        # 传了就精确取那一条 —— 多条待审并存时必须传，否则会部署错人。
        _sel = self._get_pending_skill(filename)
        if _sel is not None:
            self._active_apply_filename = _sel.get("filename") or ""
        if not _sel:
            return {"ok": False, "msg": "没有待审批的 Skill"}
        filename = _sel["filename"]
        code     = _sel["code"]
        mode     = _sel.get("mode", "create")
        target   = _sel.get("target_skill") or filename
        is_os_skill = "os_control" in (_sel.get("spec_side_effects") or [])
        ok, errors = validate_skill_code(
            code, spec_side_effects=_sel.get("spec_side_effects")
        )
        if not ok:
            return {"ok": False, "msg": "代码未通过校验: " + " | ".join(errors)}
        try:
            if mode == "update":
                _error_context = _sel.get("error_context", "")
                result = self.registry.update_skill_file(target, code)
                if result.get("ok"):
                    self._drop_pending_skill(filename)
                    self._pending_skill_at = 0.0
                    # 更新后也写 memory 上下文
                    description = self._pending_skill_description_cache or target
                    self._write_skill_deploy_context(target, description, "update")
                    # 持久语义记忆v3：correction 的v1唯一写入触发点——这次update
                    # 是报错修复（error_context非空）且部署成功，才写。普通的
                    # "用户随口要求改改"不触发（_pending_skill["error_context"]
                    # 默认是空字符串，只有 _generate_skill_update 真正带着报错
                    # 上下文走过来才会非空）。
                    if _error_context:
                        from core.semantic_memory import maybe_write_correction
                        self._schedule_semantic_memory_write(maybe_write_correction(
                            self.provider, None,
                            target, _error_context, description,
                        ))
                return result

            # 所有 Skill 统一部署到 skills/（临时 Skill 概念已废弃）
            lifecycle = "permanent"
            description = _sel.get("description", "")

            # 写 skills/
            os.makedirs("skills", exist_ok=True)
            path = os.path.join("skills", f"{filename}.py")
            # 新建模式撞名会无声覆盖已有永久 Skill——之前只有临时 Skill 撞永久
            # Skill 才有警告（见上面 temporary 分支的 _collision），永久对永久撞名
            # 完全没有检查。实测复现过：探索阶段明确说"新建"，结果给出的做法里用了一个
            # 磁盘上已存在、且和这次需求无关的旧 Skill 同名，点部署就会把旧文件
            # 整个冲掉，用户毫不知情。这里直接拒绝，不静默覆盖。
            if mode != "update" and self.registry.is_official_skill(filename):
                self._drop_pending_skill(filename)
                self._pending_skill_at = 0.0
                return {
                    "ok": False,
                    "msg": (
                        f"部署失败：「{filename}」是官方基础 Skill，不支持同名覆盖。"
                        f"如需扩展，请使用不同名称创建新 Skill。"
                    ),
                }
            if mode != "update" and os.path.exists(path):
                self._drop_pending_skill(filename)
                self._pending_skill_at = 0.0
                return {
                    "ok": False,
                    "msg": (
                        f"部署失败：已存在同名永久 Skill「{filename}」，"
                        f"为避免覆盖现有文件已拒绝部署。如果是想修改这个已有 Skill，"
                        f"请明确说'修改 {filename}'；如果是想新建一个不同的 Skill，"
                        f"请换一个不同的名字重新生成。"
                    ),
                }
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            self.registry.reload_all()
            self._drop_pending_skill(filename)
            self._pending_skill_at = 0.0
            logger.info(f"✔ [Skill] {filename} 已安装并热载")
            # 审计交互收尾。**必须在这里**而不是只在工具路径里 ——
            # 用户点 UI 上的「验证并应用」走的就是这个函数，漏了就留僵尸。
            _rt_close_skill_audit(filename, approved=True)
            self._write_skill_deploy_context(filename, description, "create", lifecycle=lifecycle)
            self._last_deployed_skill = filename
            # 部署成功 → 那条审计失败记录作废。留着会让模型下一轮还在说
            # "上次那版缺 get_spec()"，而它其实已经修好并装上了。
            _af = getattr(self, "_last_audit_failure", None)
            if isinstance(_af, dict) and _af.get("filename") == filename:
                self._last_audit_failure = None
            self._wm_add(
                EntryType.SKILL_DEPLOY, filename,
                "deploy",
                detail=description or "",
                tags=["skill", "deploy"],
            )
            from core.semantic_memory import maybe_write_task_pattern
            self._schedule_semantic_memory_write(maybe_write_task_pattern(
                self.provider, None,
                filename, description,
            ))
            _base_msg = f"Skill 「{filename}」已安装，已自动热载到工具链"
            return {"ok": True, "msg": _base_msg}
        except Exception as e:
            logger.error(f"[Skill] 部署失败: {e}")
            return {"ok": False, "msg": str(e)}

    def _write_skill_deploy_context(self, skill_name: str, description: str,
                                     mode: str = "create", lifecycle: str = "permanent",
                                     name_collision: bool = False):
        """Skill 部署后往 memory 写一条 assistant 消息记录部署上下文。

        解决"这个 Skill 怎么用"被错误路由的根本原因:
        部署后 _pending_skill 清空,模型再收到追问时没有上下文。
        在 memory 里写一条明确的记录,后续追问时模型能从 memory 读到。

        name_collision: Fix A 的延伸——"信息全"不能只给 UI 一个 toast，
        模型自己的 memory 里也要有这件事。否则用户后面说"用回原来的
        {skill_name}"，模型不知道这个名字现在被临时版本遮蔽了，
        又会重复一遍"模型以为的状态 vs 实际状态"不一致的坑。
        """
        use_zh = self._looks_chinese(description)

        if use_zh:
            action_label = "更新" if mode == "update" else "部署"
            context_msg = (
                f"Skill「{skill_name}」已成功{action_label}并热载到工具链。\n"
                f"用途：{description or '无描述'}\n"
                f"使用方式：直接用自然语言描述需求，模型会自动调用；也可以明确说：调用 {skill_name}。\n"
                f"如需修改，说：修改 {skill_name}。如需查看代码，说：查看 {skill_name} 的代码。"
            )
        else:
            action_label = "updated" if mode == "update" else "deployed"
            context_msg = (
                f"Skill \"{skill_name}\" has been successfully {action_label} and hot-loaded into the tool chain.\n"
                f"Purpose: {description or 'no description'}\n"
                f"How to use: describe the need in natural language and the model will call it automatically, "
                f"or explicitly say: call {skill_name}.\n"
                f"To modify it, say: modify {skill_name}. To inspect code, say: inspect {skill_name} code."
            )

        if name_collision:
            if use_zh:
                context_msg += f"\n名字冲突：Skill「{skill_name}」与已有永久 Skill 同名，已覆盖。"
            else:
                context_msg += (
                    f"\nName collision: \"{skill_name}\" has the same name as an existing permanent Skill and has overwritten it."
                )

        self.memory.add_message("assistant", context_msg)
        # Keep session_log in English because it is injected back into the system prompt.
        self._session_log_append(
            f"[Skill {('updated' if mode == 'update' else 'deployed')}] {skill_name}: {description or 'no description'}"
        )

    def cancel_pending_skill(self, filename: str | None = None) -> dict:
        # 同 apply：不传就是最近那条，传了就精确丢那一条。
        _sel = self._get_pending_skill(filename)
        name = (_sel or {}).get("filename") or filename or "未知"
        # 丢弃时把 outcome 落定。app 侧那条 `[System record: ...discarded...]` memory 消息
        # 会被 max_turns=10 切掉，而这里是实例状态 + 动态段，不受截断影响 ——
        # 这是"用户点了丢弃，模型下轮还知道"的唯一可靠通道。
        _af = getattr(self, "_last_audit_failure", None)
        if isinstance(_af, dict) and _af.get("filename") == name:
            _af["outcome"] = "discarded"
        elif _sel is not None:
            # 校验通过但用户仍然丢弃（不喜欢这个实现 / 改主意了）。
            # 没有报错可讲，但"被丢弃"这件事本身模型也该知道，否则它会以为部署了。
            self._last_audit_failure = {
                "filename": name, "mode": _sel.get("mode", "create"),
                "errors": [], "code_hash": "", "code_lines": 0,
                "outcome": "discarded", "at": time.time(),
            }
        self._drop_pending_skill(name)
        # ⚠️ 只有全部清空了才重置时间戳 —— 否则还剩别的待审时，
        # `_expire_stale_pending` 会拿一个被清零的时间戳去判超时。
        if not self._pending_skills:
            self._pending_skill_at = 0.0
        # 同上：UI 的「丢弃」按钮走这里，交互必须一起关掉。
        from core.runtime import interaction as _it_r
        _rt_close_skill_audit(name, approved=False,
                              reason=_it_r.Resolution.USER_CANCELLED)
        return {"ok": True, "msg": f"已丢弃 Skill 「{name}」"}

    def _recent_skill_events(self, target_skill: str | None) -> list[str]:
        """扫最近 15 条 memory，找和某个 Skill 相关的丢弃/删除/禁用/启用/部署
        记录。从 _skill_not_found_reply 里抽出来，供"Skill 还在但状态有变化"
        （比如已禁用）的分支复用——不能只有"完全找不到"才查历史，"找到了但
        状态变了"同样需要查，否则模型会在"谁/为什么"这类追问上编答案。"""
        _recent_storage = self.memory.storage[-15:]
        _events: list[str] = []
        for _msg in reversed(_recent_storage):
            _body = getattr(_msg, "content", "") or ""
            if target_skill and target_skill not in _body:
                continue
            if any(k in _body for k in ("已丢弃", "已删除", "已禁用", "已安装", "已部署", "已启用")):
                _events.append(_body[:120])
            if len(_events) >= 3:
                break
        return _events

    async def _skill_not_found_reply(self, target_skill: str | None, base_guide: str,
                                       realtime_callback) -> tuple[str, str]:
        """Generate a consistent factual reply when a Skill is not found.

        All branches that need to explain a missing Skill should use the same
        structured fact source, so Nano does not give inconsistent answers
        depending on whether the user tried to run, inspect, or update the Skill.
        """
        _events = self._recent_skill_events(target_skill)

        _on_disk_deleted = (
            target_skill and hasattr(self.registry, "list_deleted_skills")
            and target_skill in self.registry.list_deleted_skills()
        )

        _facts = (
            f"Target Skill name: {target_skill or '(not specified by the user)'}\n"
            f"Current system status: not registered / does not exist\n"
        )
        if _on_disk_deleted:
            _facts += (
                "Confirmed fact from disk backup directory, high confidence: "
                "this Skill did exist before and has been deleted. "
                "Its backup is under the skills/deleted/ directory.\n"
            )
        if _events:
            _facts += "Recent related history, newest first:\n" + "\n".join(f"- {e}" for e in _events)
        elif _on_disk_deleted:
            _facts += "Conversation history does not contain the exact deletion time or operator."
        else:
            _facts += "No recent related history was found. It may never have been created, or the record may be outside visible history."

        _guide = (
            base_guide
            + f"\n\nThe user is asking about or trying to call a Skill, but the Skill is currently not in the system. "
              f"Known facts:\n{_facts}\n\n"
            "Tell the user this fact in 1-3 natural sentences. Use the user's language when obvious. "
            "Do not invent anything that is not in the fact list, especially exact time, operator, audit log, "
            "or any fake source of truth. The fact list contains text snippets only and may not contain timestamps. "
            "Never invent a realistic-looking date or time.\n"
            "If the facts include the disk-backup confirmation, trust it. It is more reliable than conversation history. "
            "Do not say the Skill seems to have never existed just because conversation history did not mention it. "
            "In that case, only say the exact deletion time/operator is unclear, while the deletion itself is confirmed.\n"
            "Only if there is no disk confirmation and no related conversation history, say that the Skill may never have been created "
            "or that no related operation record was found.\n"
            "Finally, briefly tell the user what they can do next. Only use these two options; do not invent UI buttons, "
            "import features, or one-click restore features:\n"
            "  1) Ask Nano to create a new Skill for the same capability, clearly saying this will generate new code rather than restore the old version.\n"
            "  2) If the user is technical and wants the original code, tell them they can manually move the backup file from skills/deleted/ back to skills/."
        )
        try:
            content, model, _ = await self.provider.chat_without_tools_or_call(
                self._build_pipeline_context(), _guide,
                status_callback=realtime_callback,
                model_override=None,
            )
        except Exception:
            content = ""
            model = "BYPASS_RAW"
        content = (content or "").strip()
        if not content:
            content = (
                (f"没有找到 Skill「{target_skill}」。" if target_skill else "你想找的 Skill 我没有匹配上。")
                + "可以说「列出所有 Skill」看看当前有哪些。"
            )
            model = "BYPASS_RAW"
        return content, model

    # 报错跟读检测 —— 用于判断"上一个工具结果疑似报错 + 用户修复回应"
    _ERROR_FOLLOWUP_MAX_LEN = 80   # 放宽到 80，允许用户描述具体怎么修

    _ERROR_FIX_INTENT_WORDS = (
        "修复", "修一下", "修改", "改代码", "改一下", "fix", "update",
        "纠正", "更新代码", "重写", "调整代码", "纠错", "重新生成",
    )

    _ERROR_MARKERS = (
        "失败", "错误", "出错", "崩溃", "异常", "故障",
        "error", "exception", "traceback", "not defined", "nameerror",
        "typeerror", "valueerror", "keyerror", "attributeerror",
    )

    # 短确认词白名单：这些词独立出现时，表示"好，你去修"的意思。
    # 不要用长度做判断，"查天气"和"好的"一样短但含义完全不同。
    _SHORT_ACK_WORDS = frozenset({
        "可以", "好", "好的", "行", "嗯", "ok", "okay", "yeah", "是的", "对",
        "修一下", "改一下", "试试", "再试", "重试", "重新来", "再来",
        "再来一次", "再试一次", "没问题", "帮我修", "帮我改", "你来修",
        "重新写", "重写", "你修", "你改", "那你修", "那修一下",
    })

    def _detect_skill_error_followup(self, query: str) -> tuple[str, str] | None:
        """检测"最近一次工具结果疑似报错 + 用户修复意图"。

        命中条件（三者都满足）：
          1. self.memory.storage 里最近一条 role=="tool" 的消息内容
             包含错误标记（失败/错误/故障/Error等）
             且该条 tool 内容里包含 _last_called_skill 的名字（防止其他工具结果误命中）
          2. self._last_called_skill 存在且该 Skill 仍在 registry 里
          3. query 命中短确认词白名单 OR 包含显式修复动词关键词
             短确认词："好的/可以/修一下" 等明确的同意/授权修复
             修复动词："修/改/fix/纠正/更新/重写"等
             普通短句（"查天气""列文件"）不命中任何一条

        返回 (目标 Skill 名, 最近一次工具报错内容) 用于走 _generate_skill_update，
        不命中返回 None。
        """
        q = query.strip()
        _q_bare = q.rstrip("。！!?？.").strip()
        is_short_ack = q in self._SHORT_ACK_WORDS or _q_bare in self._SHORT_ACK_WORDS
        has_fix_intent = any(w in q.lower() for w in self._ERROR_FIX_INTENT_WORDS)
        if not (is_short_ack or has_fix_intent):
            return None

        # 目标冲突检测：若 query 里明确提到了另一个 Skill，不让 correction 抢
        _se = getattr(self, "_last_skill_error", None)
        _last_error_skill = (_se or {}).get("skill", "")
        if _last_error_skill and not is_short_ack:
            try:
                _all_skill_names = set(self.registry.skills.keys())
                for _sn in _all_skill_names:
                    if _sn != _last_error_skill and _sn.lower() in q.lower():
                        return None  # 用户明确提到了另一个 Skill，不走 correction
            except Exception:
                pass

        # 优先路径：结构化错误状态（精确绑定，不受中间消息干扰）
        if _se and not _se.get("consumed"):
            _se_skill = _se.get("skill", "")
            _se_error = _se.get("error", "")
            if _se_skill:
                try:
                    _all_s = set(self.registry.skills.keys())
                    if _se_skill in _all_s:
                        return _se_skill, _se_error
                except Exception:
                    pass

        # 降级路径：memory 扫描（兼容结构化状态未设置的情况）
        if not self._last_called_skill:
            return None

        try:
            all_skills = set(self.registry.skills.keys())
        except Exception:
            return None
        if self._last_called_skill not in all_skills:
            return None

        # 从后往前找最近一条 role=="tool" 的消息（最多看 20 条）。
        # 不在遇到 user 消息时立刻停止，因为用户可能在崩溃后说过一两句话，
        # 真正的工具报错会被那些 user/assistant 消息隔开，但仍然有效。
        last_tool_content = None
        try:
            for msg in list(reversed(self.memory.storage))[:20]:
                if getattr(msg, "role", None) == "tool":
                    last_tool_content = getattr(msg, "content", "") or ""
                    break
        except Exception:
            return None

        if not last_tool_content:
            return None

        # 验证这条 tool 结果确实来自 _last_called_skill，避免其他工具结果干扰。
        # 错误格式通常是 "工具【SkillName】执行故障: ..."，也兼容 skill 名直接出现的情况。
        _skill_name = self._last_called_skill
        if (f"【{_skill_name}】" not in last_tool_content
                and _skill_name.lower() not in last_tool_content.lower()):
            return None

        lowered = last_tool_content.lower()
        if any(marker.lower() in lowered for marker in self._ERROR_MARKERS):
            return _skill_name, last_tool_content

        return None

    def _request_management_confirmation(self, op: str, skill_name: str) -> str:
        self._pending_action = {"op": op, "skill": skill_name}
        self._pending_action_at = time.time()
        action_name = {"delete": "删除", "disable": "禁用", "enable": "启用"}.get(op, op)
        _msg = (f"将要{action_name} Skill「{skill_name}」。"
                f"这是本地文件级操作，请回复确认继续，或回复取消。")
        # 同时登记成 Interaction：跨重启存活 + 模型看得见。
        # `_pending_action` 仍是工作载荷，Interaction 是待办事实 —— 职责不同，不是双权威。
        _rt_open_skill_manage(self, op, skill_name, _msg)
        return _msg

    def _expire_stale_pending(self):
        """Step 4：超过 PENDING_TIMEOUT_SECONDS 仍未处理的 pending 自动清除。"""
        now = time.time()
        # 逐条判超时，不再一刀切。
        # 改造前是"最近那条的时间戳超了就把（唯一的）载荷清掉"；多槽之后
        # 那会变成"最新那条超时 → 把所有待审一起清掉"，包括刚生成两分钟的。
        # 每条自己带 `at`，各算各的。
        for _fn in list(self._pending_skill_order):
            _p = self._pending_skills.get(_fn)
            if not _p:
                self._pending_skill_order.remove(_fn)
                continue
            _at = _p.get("at") or self._pending_skill_at
            if not _at:
                continue
            age = now - _at
            if age > self.PENDING_TIMEOUT_SECONDS:
                self._drop_pending_skill(_fn)
                logger.info(f"[Pending] 超时清除待审 Skill「{_fn}」（{age:.0f}s 未处理）")
                # 交互也要跟着关 —— 否则卡片还挂着，点进去却发现代码没了
                # （那正是误导消息的来源）。
                try:
                    from core.runtime import interaction as _it_e
                    _rt_close_skill_audit(_fn, approved=False,
                                          reason=_it_e.Resolution.DEADLINE)
                except Exception:
                    pass
        if not self._pending_skills:
            self._pending_skill_at = 0.0
        if self._pending_action and self._pending_action_at:
            age = now - self._pending_action_at
            if age > self.PENDING_TIMEOUT_SECONDS:
                op = self._pending_action.get("op", "?")
                skill = self._pending_action.get("skill", "?")
                self._pending_action = None
                self._pending_action_at = 0.0
                logger.info(f"[Pending] 超时清除 pending_action {op}/{skill}（{age:.0f}s 未处理）")

    async def _handle_inspect_existing_skill(self, args: dict, aid: str, *,
                                             event_queue, **_ctx) -> str:
        skill_name = args.get("skill_name", "")
        src = (
            self.registry.get_skill_source(skill_name, include_disabled=True)
            if hasattr(self.registry, "get_skill_source") else None
        )
        if src and src.get("code"):
            return f"Real source code of Skill \"{skill_name}\":\n```python\n{src['code']}\n```"
        return f"No Skill named \"{skill_name}\" was found."

    async def _exit_update_existing_skill(self, exit_call, decision, *, used_model,
                                          base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_update_existing_skill_decision(
            self._exit_decision_from(exit_call, decision),
            self.memory.storage[-1].content if self.memory.storage else "",
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _exit_manage_existing_skill(self, exit_call, decision, *, used_model,
                                          base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_manage_existing_skill_decision(
            self._exit_decision_from(exit_call, decision),
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _handle_manage_existing_skill_decision(
        self,
        decision,
        used_model: str,
        base_guide: str,
        realtime_callback,
    ):
        """manage_existing_skill 元工具处理逻辑（删除/禁用/启用）。

        取代旧前置分类器硬路由。模型在主决策里判断用户确实要管理某个 Skill
        时才调用本工具，目标存在性校验后挂 pending_action，走原有二次确认流。
        """
        _args = decision.args or {}
        _op = (_args.get("operation") or "").strip().lower()
        _skill = (_args.get("skill_name") or "").strip()

        _OP_VALID = {"delete", "disable", "enable"}

        # ⚠️ 同义词兜底 —— 同 `manage_mcp` 那条：禁的是迁就某个模型的**怪癖**，
        #    而 "remove / turn off / activate" 对任何人任何模型都是真同义词。
        _op = {"remove": "delete", "uninstall": "delete", "del": "delete",
               "off": "disable", "turn_off": "disable", "deactivate": "disable",
               "on": "enable", "turn_on": "enable", "activate": "enable"}.get(_op, _op)

        # ⭐⭐ 措辞全部交给模型（`exit_flow_defer_to_model`）——
        #    2026-08-28 在 MCP 那边抓到同样的问题，这条线一起清。
        # 🔴 **注意 `add_tool_call` 的位置**：走 defer 的分支绝不能自己写
        #    （拦截处会写一次，写两次 → tool_use/tool_result 配对损坏 → 400）；
        #    而仍走 `final_result` 的那一个分支**必须自己写**。
        #    ⇒ 所以原来函数开头那句「始终先写 tool_call」被拆散到具体分支里。
        #    📌 「始终先写」在只有一种出口时是对的，多一种出口它就成了 bug。
        def _defer(facts: str, log: str):
            return {"event": "exit_flow_defer_to_model", "tool_result": facts, "log": log}

        # operation 非法
        if _op not in _OP_VALID:
            yield _defer(
                f"manage_existing_skill did NOT run: operation {_op!r} is not one of "
                f"delete, disable, enable. Nothing was changed. If you can tell which one "
                f"the user meant, call it again with that value; otherwise ask them.",
                "manage_existing_skill operation 非法，措辞交回模型。")
            return

        # 目标未指定
        if not _skill:
            yield _defer(
                "manage_existing_skill did NOT run: no Skill name was given, so there is "
                "nothing to act on. Nothing was changed. Ask the user which Skill they mean "
                "(you can list the Skills you already know about).",
                "manage_existing_skill 目标未确定，措辞交回模型。")
            return

        # 存在性校验：disable/delete 必须是已注册 Skill；enable 目标可能在 disabled 目录
        _known = set(self.registry.skills.keys())
        try:
            if hasattr(self.registry, "list_disabled_skills"):
                _known |= set(self.registry.list_disabled_skills())
        except Exception:
            pass
        if _skill not in _known:
            # ⚠️ 这一支**保持原样、不改 defer** —— 它已经是模型生成的措辞
            #    （`_skill_not_found_reply` 会查磁盘备份目录和最近事件，
            #      再让模型据实说），**本来就不违反固定文案那条**。
            #    📌 本次范围是「把写死的文案改掉」，不是「把所有出口统一成 defer」。
            #    ⭐ 若将来真要统一：把它的事实收集拆成纯函数交给 defer，
            #       能顺带省掉那一次单独的模型调用。本次不扩范围。
            self.memory.add_tool_call(
                decision.name, _args, tool_use_id=decision.tool_use_id,
                thinking_blocks=getattr(decision, "thinking_blocks", None),
            )
            _nf_content, _nf_model = await self._skill_not_found_reply(_skill, base_guide, realtime_callback)
            self.memory.add_tool_result(decision.name, f"Target Skill '{_skill}' does not exist.")
            self.memory.add_message("assistant", _nf_content)
            yield {"event": "final_result", "content": _nf_content, "model": _nf_model,
                   "status": "SYS_IDLE", "log": "manage_existing_skill 目标不存在，已事实核查。",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False}
            return

        # 挂 pending_action，走原有二次确认流
        # ⚠️ 返回值（那句「将要删除 Skill「xxx」…」）**故意不再使用** ——
        #    只留它的副作用：设 `_pending_action` + 登记 Interaction。
        #    📌 2026-08-28 专门问过这句是不是固定文案：**是**，现在不再出现在气泡里。
        #    ⚠️ 待办条目自身的展示文本仍是那句固定中文，属 范围，本次不动。
        self._request_management_confirmation(_op, _skill)
        _WHAT = {"delete": "Deleting", "disable": "Disabling", "enable": "Enabling"}[_op]
        yield _defer(
            f"{_WHAT} the Skill {_skill!r} is NOT done yet - it needs the user's explicit "
            f"confirmation first, because it is a local file-level operation. A pending "
            f"confirmation has been registered. Ask the user to confirm or cancel, in your "
            f"own words. Do not call manage_existing_skill again for this.",
            f"等待用户确认 {_op} Skill「{_skill}」，措辞交回模型。")

    async def _handle_update_existing_skill_decision(
        self,
        decision,
        query: str,
        used_model: str,
        base_guide: str,
        realtime_callback,
    ):
        """update_existing_skill 元工具的共用处理逻辑。
        主入口和文件链尾均调用此方法，保证两条路径保护一致：
        - 目标兜底（last_called → last_deployed → 唯一）
        - 存在性校验（调用 _skill_not_found_reply 事实核查）
        - 规则文件预读 + 冲突检测（冲突时不挂 pending_action，直接问用户）
        - 无冲突时挂起 pending_action 等待确认
        """
        _upd_args = decision.args or {}
        _upd_skill = (_upd_args.get("skill_name") or "").strip()
        _upd_summary = (_upd_args.get("change_summary") or "").strip()

        # target 兜底解析
        if not _upd_skill:
            _all_skills = list(self.registry.skills.keys())
            if self._last_called_skill and self._last_called_skill in _all_skills:
                _upd_skill = self._last_called_skill
                logger.info(f"[UpdateSkill] skill_name 为空，兜底 → 最近调用: {_upd_skill}")
            elif self._last_deployed_skill and self._last_deployed_skill in _all_skills:
                _upd_skill = self._last_deployed_skill
                logger.info(f"[UpdateSkill] skill_name 为空，兜底 → 最近部署: {_upd_skill}")
            elif len(_all_skills) == 1:
                _upd_skill = _all_skills[0]
                logger.info(f"[UpdateSkill] skill_name 为空，兜底 → 唯一 Skill: {_upd_skill}")

        if not _upd_skill:
            self.memory.add_tool_call(decision.name, _upd_args, tool_use_id=decision.tool_use_id, thinking_blocks=getattr(decision, "thinking_blocks", None))
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       "The user seems to want to modify an existing Skill, but which "
                       "one could not be determined - the name was not given and there "
                       "is more than one candidate. Nothing was changed. Ask them which "
                       "Skill they mean; you can list the ones you know about."),
                   "log": "等待用户指定要修改的 Skill。"}
            return

        # 存在性校验
        if _upd_skill not in self.registry.skills:
            _nf_content, _nf_model = await self._skill_not_found_reply(_upd_skill, base_guide, realtime_callback)
            self.memory.add_tool_call(decision.name, _upd_args, tool_use_id=decision.tool_use_id, thinking_blocks=getattr(decision, "thinking_blocks", None))
            self.memory.add_tool_result(decision.name, f"Target Skill '{_upd_skill}' does not exist.")
            self.memory.add_message("assistant", _nf_content)
            yield {"event": "final_result", "content": _nf_content, "model": _nf_model,
                   "status": "SYS_IDLE", "log": "update_existing_skill 目标不存在，已事实核查。",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False}
            return

        # 规则文件预读
        _upd_rule_ctx = ""
        try:
            _kb_files = rag_engine.list_knowledge_files()
            _rule_kws = ("规则", "标准", "政策", "定义", "规定")
            _rule_cands = [
                f["filename"] for f in _kb_files
                if isinstance(f, dict) and any(k in f.get("filename", "") for k in _rule_kws)
            ][:2]
            _rule_texts = []
            for _rfn in _rule_cands:
                try:
                    _rtxt = rag_engine.load_full_file(_rfn)
                    if _rtxt:
                        _rule_texts.append(f"[{_rfn}]\n{_rtxt[:2000]}")
                except Exception:
                    pass
            if _rule_texts:
                _upd_rule_ctx = (
                    "\n\n[Possibly Related Rule Documents From KB — Already Loaded]\n"
                    + "\n\n".join(_rule_texts)
                )
        except Exception:
            _upd_rule_ctx = ""

        # 生成修改摘要：有规则文件时额外做冲突核查
        _upd_summary_resp = _upd_summary
        _upd_model = used_model
        if _upd_rule_ctx:
            _summary_guide = (
                base_guide
                + f"\n\nThe user wants to modify the deployed Skill \"{_upd_skill}\".\n"
                + f"Original user request: \"{query}\"\n"
                + f"Model-interpreted change summary: \"{_upd_summary}\"\n"
                + _upd_rule_ctx
                + "\n\nIn one sentence, no more than 60 Chinese characters or 40 English words, "
                  "confirm how you plan to change this Skill's code logic.\n"
                  "Important: if the concrete numbers or concepts in change_summary conflict with the KB rule documents, "
                  "you must explicitly state the difference and ask the user which standard to use. "
                  "Do not decide by yourself.\n"
                  "If there is no conflict, directly restate the change_summary.\n"
                  "Use the user's language when obvious."
            )
            try:
                _upd_summary_resp, _upd_model, _ = await self.provider.chat_without_tools_or_call(
                    self._build_pipeline_context(), _summary_guide,
                    status_callback=realtime_callback, model_override=None,
                )
                _upd_summary_resp = (_upd_summary_resp or "").strip() or _upd_summary
            except Exception:
                _upd_summary_resp = _upd_summary
                _upd_model = "BYPASS_RAW"

        if not _upd_summary_resp:
            _upd_summary_resp = f"修改「{_upd_skill}」（具体改动来自用户原话）"

        # 冲突检测：无论冲突来自"handler 读到的规则文件"还是"文件链已写入 change_summary"，
        # 都需要检测——所以放在 _upd_rule_ctx 条件之外，始终扫描最终摘要
        # "选择"单字太泛（"文件选择功能"等正常摘要会误触发），只保留明确问用户二选一的短语
        _CLARIFY_MARKERS = (
             "请问", "按哪个", "选哪个", "哪个标准", "冲突", "不一致", "您希望", "还是按", "以哪个为准",
             "which standard", "which rule", "which one", "conflict", "inconsistent", "different from",
             "do you want", "should this use", "or the"
             )
        _upd_need_clarify = any(x in _upd_summary_resp for x in _CLARIFY_MARKERS)

        self.memory.add_tool_call(decision.name, _upd_args, tool_use_id=decision.tool_use_id, thinking_blocks=getattr(decision, "thinking_blocks", None))

        if _upd_need_clarify:
            # 规则冲突：不挂 pending_action，直接把澄清问题抛给用户
            self.memory.add_tool_result(decision.name, f"Rule conflict requires user clarification. Target: {_upd_skill}")
            self.memory.add_message("assistant", _upd_summary_resp)
            yield {"event": "final_result", "content": _upd_summary_resp, "model": _upd_model,
                   "status": "SYS_IDLE", "log": "发现规则冲突，等待用户选择标准。",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False}
            return

        # 无冲突：把确认摘要写进 query，确保 SkillWriter 收到的是用户已确认的具体描述
        # 而不只是原始模糊 query（如"按文档改一下"）
        _confirmed_query = (
            f"{query}\n\n"
            f"[change_summary from tool/file chain]\n{_upd_summary}\n\n"
            f"[summary shown to the user and waiting for confirmation]\n{_upd_summary_resp}"
        ).strip()
        self.memory.add_tool_result(decision.name, f"Entered update confirmation flow. Target: {_upd_skill}")
        self._pending_action = {
            "op": "update_skill", "skill": _upd_skill,
            "query": _confirmed_query, "summary": _upd_summary_resp,
        }
        self._pending_action_at = time.time()
        _upd_confirm_content = f"我打算这样修改 Skill「{_upd_skill}」：{_upd_summary_resp}。确认按这个改吗？"
        # 见 `_request_management_confirmation` 里同一处的理由。
        _rt_open_skill_manage(self, "update_skill", _upd_skill, _upd_confirm_content,
                              extra={"summary": _upd_summary_resp})
        # ⚠️ `_upd_confirm_content` 上面已被 `_rt_open_skill_manage` 用作待办条目
        #    的展示文本（界面构件，豁免⑤），所以变量保留；这里只把**气泡**交给模型。
        yield {"event": "exit_flow_defer_to_model",
               "tool_result": (
                   f"A change to Skill {_upd_skill!r} is prepared but NOT applied "
                   f"yet - it needs the user's go-ahead first. What the change "
                   f"does, as summarised: " + repr(_upd_summary_resp) + ". "
                   f"Put that to them in your own words and ask them to confirm "
                   f"or cancel."),
               "log": "等待用户确认 Skill 的改法。"}
