# core/orchestrator/notes_and_tasks.py
"""Orchestrator 的这一部分：回忆、用户笔记、任务清单与可视化工具，以及会话结束记录。"""

from loguru import logger

from core.memory_store import EntryType


class NotesAndTasksMixin:
    """回忆、用户笔记、任务清单与可视化工具，以及会话结束记录。"""

    def record_session_end(self, user_query: str, assistant_reply: str) -> None:
        """每轮 final_result 后由 app.py 调用，把本轮摘要写入 working_memory。

        存储字段：
          subject  = 用户说了什么（60字）
          action   = outcome: success / failed / chitchat
          detail   = 回复摘要 ｜ 操作列表
          tags     = 涉及的 Skill 名列表（artifact 索引）
        """
        if self._wm is None:
            return
        try:
            from core.memory_store import EntryType
            import re as _re

            subject = user_query.strip()[:60] + ("…" if len(user_query.strip()) > 60 else "")

            # ── outcome 推断 ──────────────────────────────────────────────
            ops = [e for e in self._session_log if e]
            has_ops = bool(ops)
            log_text = " ".join(ops)
            log_text_l = log_text.lower()
            if not has_ops:
                outcome = "chitchat"
            elif (
                any(w in log_text_l for w in ("failed", "error", "exception", "cancelled"))
                or any(w in log_text for w in ("失败", "错误"))
            ):
                outcome = "failed"
            elif (
                any(w in log_text_l for w in (
                    "success", "deployed", "updated", "called", "executed",
                    "written", "completed", "created", "hot-loaded"
                ))
                or any(w in log_text for w in ("成功", "已部署", "已调用", "已执行", "已写入", "已完成"))
            ):
                outcome = "success"
            else:
                outcome = "partial"

            # Extract involved Skill names from session_log.
            skill_names = []
            for e in ops:
                for _pat in (
                    r"\[Skill (?:deployed|updated|call|called|deploy|update)\]\s+(\S+)",
                    r"Skill(?:调用|部署|更新)[^\]]*?[：:]\s*(\S+)",
                ):
                    m = _re.search(_pat, e)
                    if m:
                        skill_names.append(m.group(1))
                        break
            skill_names = list(dict.fromkeys(skill_names))

            # ── detail ───────────────────────────────────────────────────
            reply_snippet = assistant_reply.strip()[:80] + ("…" if len(assistant_reply.strip()) > 80 else "")
            ops_str = " | ".join(ops[-3:]) if ops else ""
            detail = reply_snippet + (f" | ops: {ops_str}" if ops_str else "")

            self._wm.add(
                entry_type=EntryType.SESSION_END,
                subject=subject,
                action=outcome,
                detail=detail,
                tags=skill_names or [],
                session_id=self._wm_session_id,
            )
        except Exception as e:
            logger.warning(f"[Episodic] record_session_end 失败: {e}")

    async def _handle_recall_conversation(self, args: dict, aid: str, *,
                                          event_queue, **_ctx) -> str:
        """顺着索引条目把那段对话的结论拿回来。

        ⚠️ 返回的是**结论行的完整文本**，不是原文 —— 原文在
           `conversation_messages` 里永远都在，但把它整段搬回上下文，
           正好抵消掉 L3 的全部意义。
        📌 L3 的承诺是「想得起来这件事」，不是「把那一段重新背上」。
        """
        from core.context import bridge as _B
        _q = str(args.get("query") or "").strip()
        if not _q:
            return "[recall_conversation] Provide a query."
        hits = _B.recall(_q, top_k=5)
        if not hits:
            # ⚠️ 空结果**必须说清是哪一种空** —— 不然模型会当成"这件事没发生过"。
            return ("[recall_conversation] Nothing matched. Note this searches only "
                    "exchanges that were moved out of context; if what you are looking "
                    "for is still visible above, just read it there.")
        out = []
        for h in hits:
            _t = str(h.get("canonical_text") or h.get("display_summary") or "").strip()
            if _t:
                out.append("- " + _t)
        if not out:
            return "[recall_conversation] Matched records carried no readable text."
        return "Recalled from earlier in this conversation:\n" + "\n".join(out)

    async def _handle_recall_working_memory(self, args: dict, aid: str, *,
                                            event_queue, **_ctx) -> str:
        kw = args.get("keyword", "")
        etype = args.get("entry_type", "")
        if self._wm is not None:
            return self._wm.format_for_model(keyword=kw, entry_type=etype, limit=15)
        return "[Working Memory] This feature is temporarily unavailable."

    async def _handle_write_user_note(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        # ReAct batch 中不发 user_note_pending terminal 事件（会导致 UI 提前 return，
        # 中断 tool_results 写回，污染 memory）。只写记忆，返回普通 tool_result。
        # 模型在最终回答里自然告知用户"已记住"。
        #
        # ⭐⭐ 五个字段。参数名由 `raw_content` 改为 `content`
        #    （语义没变，名字更贴切）；新增 `applies_when` / `why` /
        #    `summary_model` / `summary_user`，旧的 `display_text` 由后者接手。
        # 🔴 旧实现里 `display_text` **从来没落库** —— 只塞进弹卡事件，弹完就扔，
        #    于是抽屉里一直显示 `detail`（给模型看的那句原文）。
        #    📌 一句话同时服务两个受众，最后两边都不合身。
        # ⚠️ 兼容旧参数名：模型偶尔会照着旧记忆里的形状调用。
        #    📌 兼容是给**过渡期**用的，不是给「两种写法都对」用的 ——
        #       所以只在新名缺失时才回退，且不写进 description。
        content = (args.get("content") or args.get("raw_content") or "").strip()
        applies_when = (args.get("applies_when") or "").strip()
        why = (args.get("why") or "").strip()
        summary_model = (args.get("summary_model") or "").strip()
        summary_user = (args.get("summary_user")
                        or args.get("display_text") or "").strip()

        # ⭐ **闸：说不出「什么时候用得上」就不写。**
        #    📌 这条不是校验参数，是本项目那条判据本身 ——
        #       说不出 when 的东西，本来就不该被记住。
        #    ⚠️ 而且**报错要说清怎么补**：一个只说「不许」的错误，
        #       会让模型换个说法再试一次。
        if content and not applies_when:
            return ("Rejected: applies_when is empty. A memory that cannot say WHEN it "
                    "is relevant will never be usable - it just becomes noise. Either "
                    "give a concrete situation ('when I ask you to edit a spreadsheet'), "
                    "or 'always' if it genuinely applies every turn, or do not save it.")

        if content and self._wm is not None:
            # ReAct 内写为 pending，用户在最终回答后有机会通过 UI 查看/删除。
            # confirmed 状态绕过了用户确认卡片，与产品语义（"写入后用户可删除"）冲突。
            _nid = self._wm.add(
                entry_type=EntryType.USER_NOTE,
                subject="用户笔记", action="记住", detail=content,
                tags=["user_note"], session_id=self._wm_session_id,
                status="pending",
                applies_when=applies_when, why=why,
                summary_model=summary_model or content,
                summary_user=summary_user or content,
            )
            # ReAct batch 内不能发 user_note_pending 终端事件（会截断 tool_results）。
            # 改成攒进本轮列表，final_result 时一并交给 UI 弹气泡 + 进记忆抽屉，不中断循环。
            # ⚠️ 弹卡用的是 **summary_user** —— 那张卡是给用户看的。
            try:
                self._notes_written_this_turn.append(
                    {"note_id": _nid, "display_text": summary_user or content}
                )
            except Exception:
                pass
        return f"User note saved as pending memory: {summary_user or content}"

    async def _handle_forget_user_note(self, args: dict, aid: str, **_ctx) -> str:
        """删掉一条记忆。**软删除**，底下是 `status='deleted'`。

        ⚠️ 两个入口（用户在抽屉里删 / Nano 调这个）走**同一个** `soft_delete_by_id` ——
           📌 一个动作有两个实现，它们只在「我两次想法相同」的前提下一致。
        ⚠️ 找不到那条 id 时**明说找不到**，不要静默成功 ——
           📌 一个「删了但其实没删」的成功回执，会让模型以为水位降了，
              然后对用户复述一个假的结果。
        """
        try:
            _nid = int(args.get("note_id"))
        except (TypeError, ValueError):
            return ("Rejected: note_id must be the integer id shown next to the memory. "
                    "Look it up in the memory list first.")
        if self._wm is None:
            return "Memory store is unavailable right now; nothing was deleted."
        try:
            _ok = self._wm.soft_delete_by_id(_nid)
        except Exception as e:
            logger.warning(f"[O2] 删除记忆 {_nid} 失败: {e}")
            return f"Could not delete memory #{_nid}: {e}"
        if not _ok:
            return (f"No memory with id {_nid} was found (it may already be deleted). "
                    f"Nothing changed - do not tell the user it was removed.")
        _why = (args.get("reason") or "").strip()
        logger.info(f"[O2] 记忆 #{_nid} 已软删除"
                    + (f"（{_why}）" if _why else ""))
        return (f"Memory #{_nid} deleted. It is gone from what you see each turn and "
                f"from the user's memory drawer.")

    async def _handle_create_task_list(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> str:
        title = args.get("title", "Task")
        steps = args.get("steps", [])
        # 初始化：所有步骤状态为 todo
        task_state = {
            "title": title,
            "steps": [
                {"id": s.get("id", f"step_{i}"), "desc": s.get("desc", ""), "status": "todo", "note": ""}
                for i, s in enumerate(steps)
            ],
        }
        await event_queue.put({
            "event": "task_list_update",
            "task_state": task_state,
        })
        return f"Created task list \"{title}\" with {len(steps)} step(s)."

    async def _handle_update_task_step(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> str:
        step_id = args.get("step_id", "")
        status = args.get("status", "done")
        note = args.get("note", "")
        await event_queue.put({
            "event": "task_step_update",
            "step_id": step_id,
            "status": status,
            "note": note,
        })
        return f"Step \"{step_id}\" was updated to {status}."

    async def _handle_render_visual(self, args: dict, aid: str, *,
                                    event_queue, **_ctx) -> str:
        _viz_html = args.get("html", "") or ""
        _viz_title = args.get("title", "") or ""
        import uuid as _uuid_v
        _viz_token = _uuid_v.uuid4().hex[:12]
        await event_queue.put({
            "event": "visual_render",
            "html": _viz_html,
            "title": _viz_title,
            "token": _viz_token,
        })
        return (
            f"Rendered the visual \"{_viz_title or 'visual'}\" for the user. "
            "Continue with one or two sentences explaining what it shows."
        )
