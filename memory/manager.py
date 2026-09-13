# memory/manager.py
from typing import List, Optional, Dict, Any, TYPE_CHECKING

from loguru import logger

from core.schema import ChatMessage, ToolCall, ToolResultBlock

if TYPE_CHECKING:
    from core.runtime.conversation import ConversationRepository


class MemoryManager:
    """对话历史管理器。

    设计要点：
    - 存储层只放 ChatMessage 对象，不做格式转换。
    - 输出层 (get_full_context) 调用 ChatMessage.to_dict()，保证 tool_call ↔ tool 配对正确。
    - 截断按"对话回合"对齐，永远不切散 function_call ↔ function_response 配对。
    - reset() 用于"重置当前对话"按钮。
    """

    # 单个工具结果上限。原 30_000 字符会让代码修改 / load_full_file 类任务
    # 在多轮 ReAct 里迅速膨胀到 70K~100K tokens；先压到 12K，
    # 保留关键内容，同时避免历史 tool_result 反复重传。
    MAX_SINGLE_TOOL_RESULT_CHARS = 12_000

    def __init__(self, max_turns: int = 10,
                 conversation_repository: "ConversationRepository | None" = None):
        self.storage: List[ChatMessage] = []
        self.max_turns = max_turns
        # 只有真实应用传入仓储才持久化；测试/独立调用仍可使用纯内存。
        self._conversation_repository = conversation_repository
        self._conversation_session_id: str | None = None
        if conversation_repository is not None:
            session = conversation_repository.current_session()
            self._conversation_session_id = session.session_id
            self._hydrate_current_session()

    def _append(self, msg: ChatMessage) -> None:
        """先写权威账本，再更新可丢弃的内存投影。"""
        if self._conversation_repository is not None and self._conversation_session_id:
            self._conversation_repository.append_message(self._conversation_session_id, msg)
        self.storage.append(msg)
        self._truncate_safely()

    def _hydrate_current_session(self) -> None:
        """从当前会话重建可注入上下文，不让半截工具事务跨重启污染 provider。"""
        if self._conversation_repository is None or not self._conversation_session_id:
            return
        raw = self._conversation_repository.load_messages(self._conversation_session_id)
        restored: List[ChatMessage] = []
        i = 0
        while i < len(raw):
            msg = raw[i]
            if msg.role == "tool_calls":
                nxt = raw[i + 1] if i + 1 < len(raw) else None
                call_ids = [tc.tool_use_id for tc in msg.tool_calls]
                result_ids = ([tr.tool_use_id for tr in nxt.tool_results]
                              if nxt is not None and nxt.role == "tool_results" else [])
                if nxt is not None and nxt.role == "tool_results" and call_ids == result_ids:
                    restored.extend((msg, nxt))
                    i += 2
                    continue
                i += 1
                continue
            if msg.role == "tool_results":
                # 只能由上一分支与 tool_calls 成对加入；孤立结果绝不能进 API context。
                i += 1
                continue
            if msg.role == "tool_call":
                nxt = raw[i + 1] if i + 1 < len(raw) else None
                if (nxt is not None and nxt.role == "tool"
                        and nxt.tool_use_id == msg.tool_use_id):
                    restored.extend((msg, nxt))
                    i += 2
                    continue
                i += 1
                continue
            if msg.role == "tool":
                i += 1
                continue
            restored.append(msg)
            i += 1
        self.storage = restored
        self._restore_image_notes()
        # 🔴 **把落盘的衰减档位重放回来** —— 不做的话每次重启都会
        #    「hydrate 原始历史 → 全部恢复 L0 → 又衰减一遍」，
        #    而 L3 那些更糟：内容回到上下文，但 decay 表说它已经降过了，
        #    于是**永远不会再被移除**，同时 UI 按 level 把它藏着
        #    → **UI 说「已移出记忆」，模型手里还拿着**。
        # 📌 建了「衰减权威」这张表却不把它重放回投影，等于只做了一半。
        self._replay_decay_levels()
        self._truncate_safely()

    def _replay_decay_levels(self) -> None:
        """重启后按 `exchange_decay` 重建投影。

        ⚠️⚠️ **刻意不判 `ladder_enabled`。** 它是一个**单向迁移开关**：
           控制"还产不产生新的衰减"，不控制"已经产生的算不算数"。
           这里如果判了开关，关掉之后就会变成：
               投影：把原文全放回来（当它没降过）
               UI / L3 索引：仍按表把那几轮藏着、仍注入索引
           —— **三处对同一件事三个答案**，比不可逆糟糕得多。
        📌 已经发生的降级是**既成事实**，一个 bool 撤不回来。要真正恢复旧行为，
           清 `exchange_decay` 表（原文一直在 `conversation_messages` 里）——
           见 `data/model_config.json` 的 `_ladder` 说明。
        """
        try:
            if not self._conversation_session_id:
                return
            from core.context.decay import rebuild_projection
            from core.context.decay_store import DecayStore
            # ⚠️ 用**本仓储自己的** store，不是 `get_kernel().store` ——
            #    📌 一个对象已经知道自己连的是谁时，别再去问全局要一遍。
            rebuild_projection(self, DecayStore(self._conversation_repository.store),
                               self._conversation_session_id)
        except Exception as e:
            logger.debug(f"[Memory] 重放衰减档位跳过: {e}")

    def _restore_image_notes(self) -> None:
        """重启后，按 `ui_images` **重新算出**那句「你当时确实看过这些图」。

        ⭐ 这是「历史不许被上下文改写」的另一半：`compress_image_blocks` 不再把
           占位符写进落盘正文了，于是重启后模型本该什么都不知道 ——
           **除非我们把它算回来**。而算回来的信息源是 `ui_images`（用户历史），
           不是被压过的 content（模型投影）。

        📌 步 1 的保证（模型不许说"我从没见过图片"）一点没丢，
           但它现在是**推导出来的**，不是**写进用户历史里的**。
        ⚠️ 像素**不重新喂给模型** —— 那是步 3「回看工具」的事，
           而且无条件重灌会让每次重启都付一遍视觉 token。
        """
        for msg in self.storage:
            if msg.role != "user" or not getattr(msg, "ui_images", None):
                continue
            body = msg.content if isinstance(msg.content, str) else ""
            if "[System note" in body:      # 幂等：同一条别贴两次
                continue
            note = self._image_note_for(msg)
            msg.content = (body + "\n\n" + note) if body.strip() else note

    @property
    def conversation_session_id(self) -> str | None:
        """Current durable session identity, if this manager is persistence-bound."""
        return self._conversation_session_id

    @property
    def conversation_repository(self):
        """这个 manager 绑的那本**落盘账本**。

        ⭐ 加它是因为踩过一次：`app` 侧写 `self.memory.conversation_repository`
           取原文，而真名是私有的 `_conversation_repository` ——
           `AttributeError` 被 `except` 吞掉，工具卡展开显示
           「（这次调用没有可展示的参数或结果）」。
        📌 **那句兜底文案读起来像个正常答案，而不是像一个 bug** ——
           这恰恰是最难查的那类静默：一个界面在"如实地报告一件错的事"。
        ⚠️ 与 `conversation_session_id` 成对：**要读原文的人两样都需要**，
           只给一半等于逼调用方去碰私有属性。
        """
        return self._conversation_repository

    def reset_conversation(self) -> str | None:
        """The only durable new-session operation: explicit user conversation reset."""
        if self._conversation_repository is None:
            self.reset()
            return None
        session = self._conversation_repository.reset_current_session()
        self._conversation_session_id = session.session_id
        self.storage = []
        return session.session_id

    def conversation_messages(self) -> List[ChatMessage]:
        """Return the full durable current conversation for passive UI projection.

        This intentionally bypasses ``storage``: that list is a bounded model context,
        while the UI must faithfully show the whole current session until context decay moves a
        complete exchange out of visible context.
        """
        if self._conversation_repository is None or not self._conversation_session_id:
            return list(self.storage)
        return self._conversation_repository.load_messages(self._conversation_session_id)

    # ── 写入 ─────────────────────────────────────────────────────────────

    def add_message(
        self,
        role: str,
        content: str = "",
        name: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        tool_use_id: Optional[str] = None,
        thinking_blocks: Optional[List[Dict[str, Any]]] = None,
    ):
        """追加一条消息。**这个入口写的都是用户看得见的东西。**

        ⚠️ 系统注记（`[System check-in]` / `[System record: …]` 这一类
        「给模型读、用户从没在屏幕上见过」的消息）**必须走 `add_system_note`**，
        不要在这里加个参数了事 —— 理由见那个方法的 docstring。
        """
        msg = ChatMessage(
            role=role, content=content, name=name, args=args,
            tool_use_id=tool_use_id, thinking_blocks=thinking_blocks,
        )
        self._append(msg)

    def add_system_note(self, role: str, content: str):
        """⭐⭐⭐ 写一条**给模型读、但用户从没在屏幕上见过**的消息。

        ═══ 为什么需要一个单独的入口（2026-08-13 实际运行中发现）═══

        重启 Nano 之后，聊天区里冒出一个用户名气泡，
        内容是 `[System check-in] You put this in the background a while ago…`
        —— 一整段英文系统提示词，署着用户的名字。

        这类注记为了让模型读到，**必须**以 `user` / `assistant` 角色进上下文
        （provider 只认这两种角色）。把上下文原样落盘是对的，
        而 UI 重放时把整本账本当聊天记录逐条画出来，于是它们全部现形。

        ═══ 为什么是「换个入口」而不是「加个参数」═══

        加参数的话，**下一个写系统注记的人会去抄最近的一处**，抄到哪种全看运气；
        漏传一次就是一次静默泄露，而且要等到有人重启才会被看见。

        📌 **把「要不要显示」写成入口的性质，而不是每个调用点的一个参数。**
           （同 `_rt_ia_owner` 那条：把规则绑在一个能被检查的地方。）
        ⭐ 于是它可以被一条 AST 断言守住：**正文以 `[System` 开头的 `add_message`
           一律判违规** —— 那种写法只可能是漏用了本方法。
        """
        msg = ChatMessage(role=role, content=content, visible_to_user=False)
        self._append(msg)

    def add_ui_only_record(self, content: str, render_kind: str) -> None:
        """⭐⭐⭐ 写一条**用户看得见、但模型从没见过**的记录。**`add_system_note` 的镜像。**

        ═══ 为什么需要它（2026-08-22 实际运行中发现）═══

        API 欠费时，聊天区出现一张红色 `System Error` 卡片（402）。
        **重启之后那张卡片消失了** —— 「你好」下面空空如也，
        而当时真的发生过一件事。
        📌 **对话记录在说谎**，而这直接违反唯一那条持久化原则：
           **UI 必须是真实的反馈。**

        ⚠️ 一度考虑把它折叠/加「当时」的修饰，理由是「它是当时的系统状态」。
           这个想法被否掉了，理由是：
           📌 **一条在对话流里的记录不对「现在」做任何断言 ——
              它的位置已经把时间说清楚了**（上面的都在它之前发生）。
           会过期的是**活的状态指示器**（pill / 抽屉），不是历史里的一条记录。
        → 所以：**原样重画**，不折叠、不加修饰。

        ═══ 为什么只进账本、不进 storage ═══

        ⭐ 两个受众**本来就读两个地方**（`conversation_messages()` 的注释原话：
           "intentionally bypasses storage"）：
             · 模型 → `storage`（有界上下文）
             · UI 重放 → 账本（`_conversation_repository`）
        ⭐ 而这条记录的事实是：**那一轮根本没到达模型**（请求本身失败了）。
        📌 **一条从没到达模型的消息，不该在它的历史里显示成它说过的话** ——
           否则重启后模型会看见自己"说"了一串 402 报错，然后为此道歉。
        ⭐ 于是不需要新增 `visible_to_model` 那条轴：**分流点已经存在**，
           只要不写 `storage` 即可。

        ⚠️ 与 `add_system_note` 严格互补，两个入口都**把「给谁看」写成入口的性质**，
           而不是某个调用点的参数 —— 📌 加参数的话，下一个人会去抄最近的一处，
           抄到哪种全看运气（那正是 `add_system_note` 当初被单独立出来的理由）。
        """
        if self._conversation_repository is None or not self._conversation_session_id:
            return
        msg = ChatMessage(role="assistant", content=content,
                          render_kind=render_kind)
        # ⚠️ **只写账本，不碰 `self.storage`** —— 这一行就是本方法存在的全部理由。
        self._conversation_repository.append_message(
            self._conversation_session_id, msg)

    def add_tool_call(
        self,
        name: str,
        args: Dict[str, Any],
        tool_use_id: Optional[str] = None,
        thinking_blocks: Optional[List[Dict[str, Any]]] = None,
    ):
        """单工具调用（旧格式，向后兼容）。内部委托给 add_tool_calls。"""
        self.add_tool_calls(
            [ToolCall(name=name, args=args or {}, tool_use_id=tool_use_id, index=0)],
            thinking_blocks=thinking_blocks,
        )

    def add_tool_result(self, name: str, content: str, tool_use_id: Optional[str] = None):
        """单工具结果（旧格式，向后兼容）。内部委托给 add_tool_results。

        tool_use_id 优先用传入值；传入为 None 时，从最近的 tool_calls/tool_call 记录里找。
        """
        if not tool_use_id:
            for msg in reversed(self.storage):
                if getattr(msg, "role", None) == "tool_calls":
                    for tc in reversed(getattr(msg, "tool_calls", []) or []):
                        if tc.name == name:
                            tool_use_id = tc.tool_use_id
                            break
                elif getattr(msg, "role", None) == "tool_call" and getattr(msg, "name", None) == name:
                    tool_use_id = msg.tool_use_id
                if tool_use_id:
                    break
        self.add_tool_results([
            ToolResultBlock(
                name=name,
                tool_use_id=tool_use_id or f"tool_{name}",
                content=content,
            )
        ])

    def add_tool_calls(
        self,
        tool_calls: List[ToolCall],
        thinking_blocks: Optional[List[Dict[str, Any]]] = None,
    ):
        """批量写入多工具调用（ReAct 新格式）。一次写入生成一条 assistant 消息。

        所有 tool_use 和 thinking_blocks 必须在同一条 assistant 消息里，
        Anthropic API 才能正确关联 tool_result。
        写入前归一化空 tool_use_id，确保 validate_tool_turns 比对一致。
        """
        import uuid as _uuid
        from dataclasses import replace as _replace
        normalized = []
        for i, c in enumerate(tool_calls):
            if not c.tool_use_id:
                c = _replace(c, tool_use_id=f"tool_{c.name}_{i}_{_uuid.uuid4().hex[:8]}")
            normalized.append(c)
        msg = ChatMessage(
            role="tool_calls",
            tool_calls=normalized,
            thinking_blocks=thinking_blocks or [],
        )
        self._append(msg)
        return normalized

    def add_tool_results(self, tool_results: List[ToolResultBlock]):
        """批量写入多工具结果（ReAct 新格式）。一次写入生成一条 user 消息。

        必须与前面的 add_tool_calls 一一对应，tool_use_id 顺序必须一致。
        """
        self._compress_tool_results_inplace(tool_results)
        msg = ChatMessage(
            role="tool_results",
            tool_results=tool_results,
        )
        self._append(msg)

    def attach_reply_quote(self, quote: str) -> bool:
        """[2026-08-22] 把「这句话在回复什么」挂到最后一条 user 消息上。

        🔴 问题（实际运行中发现）：重启之后，引用回复的消息**变回普通消息** ——
           `↳` 和引用横幅都没了。因为那个指向只活在内存里
           （`_reply_target` → `_reply_target_turn`），发出去就被本轮的
           prompt 消费掉，**从来没跟着消息落过盘**。
        📌 **「这句话在回答哪一条」对模型是当轮的事，对用户是永久的事** ——
           两个受众的时效不同，就不能共用同一个内存指针。

        ⚠️ 形状照抄 `attach_user_images`（同一件事：**UI 侧事实进账本**）——
           📌 一个已经存在的形状，第二次出现时该复用它。
        ⚠️ 同样倒着找而不是写 `storage[-1]`：
           📌 一个"此刻恰好成立"的前提，会在别人挪动调用点时静默失效。
        ⚠️ 同样全程吞异常：留一份引用是附加价值，
           它的失败绝不许升级成"消息发不出去"。
        """
        try:
            _q = (quote or "").strip()
            if not _q:
                return False
            msg = next((m for m in reversed(self.storage) if m.role == "user"), None)
            if msg is None:
                return False
            msg.reply_quote = _q[:200]
            if self._conversation_repository is not None and self._conversation_session_id:
                self._conversation_repository.update_message(self._conversation_session_id, msg)
            return True
        except Exception as e:
            logger.debug(f"[Reply] 引用落盘失败（不影响发送）: {e}")
            return False

    def attach_user_images(self, image_parts: list) -> int:
        """把这一轮用户发的图存进 Nano 自己的图库，引用挂到最后一条 user 消息上。

        ⚠️⚠️ **这是「图片进入账本」的唯一收口点。**
        📌 与成本闸下沉到 provider、上下文计量收口在 provider 同一条经验：
           **别在十个上游各补一次** —— 收在账本这一侧，任何将来新增的图片入口
           （粘贴通道、拖拽、Skill 产出）都自动被覆盖。

        ⚠️ 全程吞异常并返回 0：留一份历史是**附加价值**，
           它的失败绝不许升级成"消息发不出去"。
        """
        try:
            from core.runtime.blobs import extract_image_blocks, put_image
            pairs = extract_image_blocks(image_parts)
            if not pairs:
                return 0
            # ⚠️ 这里的调用点确实紧跟 `add_message("user", …)`，所以 `storage[-1]`
            #    此刻是对的 —— **但仍然不写 `storage[-1]`**：
            #    📌 一个"此刻恰好成立"的前提，会在别人挪动调用点时静默失效，
            #       而失效的表现是"图片默默不留档"。倒着找一条 user 消息不花钱。
            msg = next((m for m in reversed(self.storage) if m.role == "user"), None)
            if msg is None:
                return 0
            refs = [r for r in (put_image(raw, mime) for raw, mime in pairs) if r]
            if not refs:
                return 0
            msg.ui_images = list(dict.fromkeys([*msg.ui_images, *refs]))
            if self._conversation_repository is not None and self._conversation_session_id:
                self._conversation_repository.update_message(self._conversation_session_id, msg)
            return len(refs)
        except Exception as e:
            logger.warning(f"用户图片留档失败（不影响本轮对话）: {e}")
            return 0

    def _last_user_image_message(self):
        """最近一条**带图的用户消息** —— 倒着找，不看 `storage[-1]`。

        🔴🔴 `storage[-1]` 在这个项目里已经坑了四次（全是图片留档这一格）：
             它在一轮之内会变成 user → tool_calls → tool_results → assistant，
             而"最近那张图挂在哪条消息上"从头到尾只有一个答案。
        📌 **别问"最后一条是什么"，直接问"我要的那条在哪"。**
        ⚠️ 倒着找而不是正着找：同一轮对话里可能有多张图，要的永远是最近那张。
        """
        for msg in reversed(self.storage):
            if msg.role == "user" and getattr(msg, "ui_images", None):
                return msg
        return None

    def set_image_summary(self, summary: str) -> int:
        """把摘要挂到最近一条带图的 user 消息上（storage + 落盘）。

        返回 0 表示当前没有对象 —— 📌 **宁可少说一句，不许多断言一件事**
        （同 `set_next_checkin` 那条：不在回看轮里就如实说没有对象）。
        """
        try:
            msg = self._last_user_image_message()
            if msg is None:
                return 0
            msg.image_summary = (summary or "").strip()
            if self._conversation_repository is not None and self._conversation_session_id:
                self._conversation_repository.update_message(self._conversation_session_id, msg)
            return len(msg.ui_images)
        except Exception as e:
            logger.warning(f"图片摘要留档失败: {e}")
            return 0

    def _compress_tool_results_inplace(self, tool_results: List[ToolResultBlock]):
        """单个 tool_result 超过字符上限时就地截断，防止单个大结果撑爆上下文。"""
        limit = self.MAX_SINGLE_TOOL_RESULT_CHARS
        for tr in tool_results:
            if isinstance(tr.content, str) and len(tr.content) > limit:
                original_len = len(tr.content)
                tr.content = tr.content[:limit] + f"\n[...truncated; original content was {original_len:,} chars]"

    @staticmethod
    def _is_image_block(block: Any) -> bool:
        """Return True for known multimodal image block shapes."""
        if not isinstance(block, dict):
            return False

        # Anthropic-style: {"type": "image", "source": {...}}
        if block.get("type") in {"image", "image_url"}:
            return True

        # Gemini/app-style variants, kept defensive.
        if "inline_data" in block or "image" in block:
            return True

        source = block.get("source")
        if isinstance(source, dict):
            if source.get("type") == "base64":
                return True
            if source.get("data") and (source.get("media_type") or source.get("mime_type")):
                return True

        return False

    # 占位文本。这段措辞是刻意的，别随手改回"数据已省略"那种写法。
    #
    # 2026-08-04 实际运行中的 bug：用户发图 → Nano 分析 → 追问图里的细节 →
    # Nano 答"图片数据已经被系统省略了，我看不到原图，你需要重新上传"。
    # 直接问它本人看到了什么，它原样贴出了旧占位符并说"**系统标签**明确说
    # image data omitted…，理论上看不到"。
    #
    # 关键事实：**它自己那轮的分析根本没被删**——本方法只动 user 轮的 image block，
    # assistant 轮一个字没碰，而对话窗口是 10 轮、用户是紧接着追问的。
    # 所以失败的不是"信息没了"，是这句占位符**同时做错了三件事**：
    #   ① 只说"没了"，不说"曾经有过什么、你当时看过"；
    #   ② 用 "omitted to reduce token cost" 这种"已被拿走"的措辞，读起来像"你无能为力"；
    #   ③ 它被拼进 user 消息正文，没有任何标记说明这是系统加的。
    # 于是模型顺势给出"我看不到，请重新上传"——哪怕它的分析就在下一条。
    #
    # 现在改成：明确标注这是系统注记、陈述"你当时确实看过"、指出结论在哪、
    # 并直接禁止那句"我从没见过图片"。
    # ⭐ 2026-08-13：现在它还带上**把手**和**回看阶梯**。
    _IMAGE_PLACEHOLDER = (
        "[System note — inserted by Nano's context manager, not written by the user]\n"
        "This message originally carried {n} image(s). You DID see them at the time and "
        "your own reading of them is in your reply that follows. Only the raw pixels were "
        "dropped from history to save tokens.\n"
        "Therefore: do not say you never saw an image, and do not ask the user to re-upload "
        "just because the pixels are gone. Answer from what you already established about "
        "them.{handles}"
    )

    # ⭐⭐⭐ 回看阶梯。**这段的每一句都是定下来的隔离原则**：
    #   > 「**回看按需触发** —— 摘要够用就别回看，**不是提到图片就回看**」
    # 📌 所以措辞必须先说"够用就别看"，再说"怎么看" —— 顺序反了模型会默认先看。
    _IMAGE_REVIEW_HINT = (
        "\nThe original file(s) are still on disk: {handles}. "
        "If — and only if — the question needs a visual detail that is NOT already covered "
        "above, look at the original again: load_tools(names=[\"view_past_image\"]) first, "
        "then call view_past_image with that handle. "
        "Do not look again just because an image is mentioned, and never ask the user to "
        "re-upload something that is still on disk."
    )
    # ⚠️ 那句 `load_tools(...) first` 是实际运行中加的（2026-08-13）：不写的话模型会
    #    直接调 `view_past_image` → 撞 `TOOL_NOT_ACTIVE` 闸 → 用户先看到
    #    一个红 ✗ 和 `1 failed`，然后它才去 load、再调一次。
    # ⭐ 闸拦得对（工具确实没激活），**要修的是别让它撞** ——
    #    📌 一次「按设计失败」的重试，在用户眼里和一次真故障长得一模一样。

    def _image_note_for(self, msg) -> str:
        """这条消息的图片系统注记。**压缩和 hydrate 共用同一个生成器。**

        ⚠️ 两处各写一份是这个项目栽过的形状（见 `_UI_TERMINAL_EVENTS`）：
           📌 **同一句话有两个作者，它们迟早说得不一样。**
        """
        _refs = list(getattr(msg, "ui_images", None) or [])
        _n = len(_refs) or self._count_image_blocks(msg)

        # ⭐⭐ 摘要 —— 模型**在还看得见像素的那一轮**自己写下的。
        # ⚠️ 它和把手、和这条注记一起活、一起死：消息掉出窗口，它们全都跟着走。
        #    明确否掉了任何"让图活过上下文压缩"的想法 ——
        #    **连文本都随 UI 上下文走了，专门给一张界面上都不在的图做持久记忆，
        #    一点必要都没有。**
        _sum = (getattr(msg, "image_summary", "") or "").strip()
        _mid = (f"\nWhat you wrote down about it at the time: {_sum}" if _sum else "")

        _tail = ""
        if _refs:
            try:
                from core.runtime.blobs import short_handle
                _hs = [h for h in (short_handle(r) for r in _refs) if h]
                if _hs:
                    _tail = self._IMAGE_REVIEW_HINT.format(handles=", ".join(_hs))
            except Exception:
                _tail = ""
        if not _tail:
            # 没有落盘引用（老消息）→ 退回步 1 那句：承认看过，但别承诺能回看。
            # 📌 **兜底不许承诺一件做不到的事** —— 那比不提更糟。
            _tail = (
                "\nAsk for the image again only if the question needs a visual detail you "
                "never described — and if so, say exactly which detail you need to look at."
            )
        return self._IMAGE_PLACEHOLDER.format(n=_n, handles=_mid + _tail)

    @staticmethod
    def _count_image_blocks(msg) -> int:
        c = msg.content
        if not isinstance(c, list):
            return 1
        return sum(1 for b in c if MemoryManager._is_image_block(b)) or 1

    def compress_image_blocks(self) -> int:
        """Replace historical image/base64 blocks with a small text placeholder.

        This is a context-size optimization only. It should be called after the current
        turn has had a chance to use the image, or before building context to remove
        older images. It preserves user text and removes only image payloads.

        ⚠️⚠️ **只动 `self.storage`（模型上下文投影），【绝不】写回权威账本。**

        🔴 改造前这里调 `update_message()`，于是「省 token」这个纯上下文动作
           **把一段英文系统注记写进了用户那条消息的落盘正文** ——
           重放时它会**原样出现在用户自己的气泡里**。
           那正是之前修过的那个泄露 bug，只是换了一扇门进来：上次是整条系统消息
           （`visible_to_user` 挡住了），这次是**粘在真实用户消息尾巴上的系统注记**，
           而 `visible_to_user` 对它无能为力（那条消息确实是用户发的）。
           ⭐ 幸好没等到它发作：修复落地后还没有人发过图，落盘里 0 条中招。

        📌 **判据：省 token 是模型侧的事，它不该有权改写"用户说过什么"。**
        ⭐ 所以两侧现在各管各的：
             storage（会丢弃/会被截断） → 模型上下文，可以压
             落盘账本 + `ui_images`     → 用户历史，谁都不许动
           重启后由 `_hydrate_current_session` 依据 `ui_images` **重新生成**占位符，
           步 1「模型不许自我否定」的保证一点没丢 —— 它现在是**算出来的**，
           不是**写进历史里的**。
        """
        compressed = 0

        for msg in self.storage:
            if msg.role != "user" or not isinstance(msg.content, list):
                continue

            img_count = sum(1 for b in msg.content if self._is_image_block(b))
            if img_count <= 0:
                continue

            text_parts = []
            for b in msg.content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    text_parts.append(str(b.get("text")))
                elif b.get("text"):
                    text_parts.append(str(b.get("text")))

            combined = "\n".join(p.strip() for p in text_parts if p and p.strip()).strip()
            placeholder = self._image_note_for(msg)
            # 占位符放在用户原文【之后】并空一行：先读到用户说了什么，再读到系统注记。
            # 反过来会让模型先撞上一大段系统文字，容易把它当成用户的话。
            msg.content = (combined + "\n\n" + placeholder) if combined else placeholder
            compressed += img_count

        return compressed

    # ══════════════════════════════════════════════════════════════════
    # 迭代阅读：把**已经读过的旧切片**换成占位符
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐⭐ **这是「恒定上下文厚度」唯一的落地点**（那条状态机：
    #    「读入原始数据 → 提炼进 scratchpad → **丢弃原始数据** → 带着它进入下一轮」）。
    #    没有它，`notes` 参数只是多存了一份东西，原文照样在上下文里堆着 ——
    #    📌 **这套流程只做一半（提炼了却不丢原文），得到的是「原文照样堆着、
    #       再多一份笔记」，比不做还贵。**
    #
    # 🔴 **只能改写，不能删**：`tool_use` / `tool_result` 必须严格配对，
    #    删了直接 400（memory 层已有三道防线在守这件事）。
    #    ⭐ 范式直接抄同文件的 `compress_image_blocks()` —— 坑一原文点名的那个。
    #
    # ⚠️⚠️ **只动 `self.storage`（模型上下文投影），绝不写回权威账本。**
    #    📌 判据抄自 `compress_image_blocks` 的验尸：**省 token 是模型侧的事，
    #       它不该有权改写「历史上发生过什么」。** 用户在 UI 里回看那一轮时，
    #       看到的仍然是当时真实的工具结果。
    #
    # ⚠️ **保留最后一次**：模型此刻正在用的那一段不能动 ——
    #    📌 丢掉它等于让模型「刚读完就忘」，那不是压缩，是失忆。
    def compress_file_reads(self) -> int:
        """把同一个文件的**旧**读取结果换成占位符。返回压缩了几段。

        ⭐ 判据是「**同一个文件的、不是最后一次的**」：
          · 不同文件互不影响 —— 交替读两份文档时，两边各自保留最后一段
          · 最后一次保留 —— 那是模型当前的工作面
        """
        import re as _re
        # 头部形如：[Lines 1-198 of 11,174 lines / 526,203 chars · <文件名>]
        _HEAD = _re.compile(
            r"^\[(?:Lines|PEEK) ([\d,]+)-([\d,]+) of [\d,]+ lines / [\d,]+ chars"
            r"(?: · (.+?))?\]")

        # 先扫一遍：每个文件最后一次出现在哪
        hits = []          # (msg_idx, block_idx, filename, head)
        for mi, msg in enumerate(self.storage):
            if getattr(msg, "role", "") != "tool_results":
                continue
            for bi, tr in enumerate(getattr(msg, "tool_results", None) or []):
                body = getattr(tr, "content", None)
                if not isinstance(body, str):
                    continue
                m = _HEAD.match(body)
                if not m:
                    continue
                hits.append((mi, bi, (m.group(3) or "").strip(), m.group(0)))

        if len(hits) < 2:
            return 0
        last_of: dict = {}
        for k, (mi, bi, fn, _h) in enumerate(hits):
            last_of[fn] = k

        compressed = 0
        for k, (mi, bi, fn, head) in enumerate(hits):
            if last_of.get(fn) == k:
                continue          # 最后一次：模型正在用，不动
            tr = self.storage[mi].tool_results[bi]
            # 🔴 **不能用 `startswith("[dropped")`** —— 占位符前面还留着那个头
            #    （`[Lines ...]`），所以那个判断**永远为假**。
            #    ⚠️ 后果不是重复压缩（内容已经是占位符了），而是**返回值在撒谎**：
            #       每一轮都报「压掉 N 段」，而实际一段都没动。
            #    📌 **一个只在返回值上错的 bug 最难发现：功能是对的，
            #       而所有基于它的判断（日志、计数、上层决策）都是错的。**
            if not isinstance(tr.content, str) or "[dropped to keep" in tr.content:
                continue
            _orig = len(tr.content)
            # ⚠️ 占位符**如实说清三件事**：读过哪一段 / 内容没了 / 想要就再读一次。
            #    📌 只说「已省略」而不说「怎么拿回来」，模型会以为那段永远没了，
            #       于是要么放弃，要么凭记忆编 —— 后者更糟。
            tr.content = (
                f"{head}\n[dropped to keep the context flat - "
                f"{_orig:,} characters of this slice are no longer here. "
                f"Your notes are what carried forward. "
                f"Read it again with the same offset if you truly need the raw text.]"
            )
            compressed += 1
        return compressed

    def repair_invalid_tool_turns(self) -> bool:
        """修复 storage 中损坏的 tool_calls/tool_results 对。

        适用于历史内存已污染（provider 400 之前就存在坏 pair）的场景，
        _clean_damaged_memory 只处理"当前写入中"的失败，这个函数处理"历史已有"的坏 pair。
        从后往前扫描，删除不合法的 tool_calls/tool_results 对。
        返回 True 表示执行了修复，False 表示内存原本就合法。
        """
        ok, _ = self.validate_tool_turns()
        if ok:
            return False
        msgs = self.storage
        i = len(msgs) - 1
        repaired = False
        while i >= 0:
            msg = msgs[i]
            if getattr(msg, "role", None) == "tool_calls":
                nxt = msgs[i + 1] if i + 1 < len(msgs) else None
                if nxt is None or getattr(nxt, "role", None) != "tool_results":
                    # 孤立 tool_calls，直接删
                    msgs.pop(i)
                    repaired = True
                else:
                    call_ids = [tc.tool_use_id for tc in (msg.tool_calls or [])]
                    result_ids = [tr.tool_use_id for tr in (nxt.tool_results or [])]
                    if call_ids != result_ids:
                        # ID 不匹配的 pair，删除两条
                        msgs.pop(i + 1)
                        msgs.pop(i)
                        repaired = True
            i -= 1
        return repaired

    def validate_tool_turns(self) -> tuple[bool, str]:
        """校验 memory 里 tool_calls/tool_results 格式是否合法。

        每条 tool_calls 后面必须紧跟一条 tool_results，且 tool_use_id 列表完全对应。
        旧格式的 tool_call/tool 对不在这里校验（历史兼容）。
        """
        msgs = list(self.storage)
        for i, msg in enumerate(msgs):
            if getattr(msg, "role", None) == "tool_calls":
                if i + 1 >= len(msgs):
                    return False, "tool_calls is missing the matching tool_results message"
                nxt = msgs[i + 1]
                if getattr(nxt, "role", None) != "tool_results":
                    return False, f"tool_calls[{i}] is followed by role={getattr(nxt,'role',None)}, but must be followed by tool_results"
                call_ids = [tc.tool_use_id for tc in (msg.tool_calls or [])]
                result_ids = [tr.tool_use_id for tr in (nxt.tool_results or [])]
                if call_ids != result_ids:
                    return False, f"tool_use_id mismatch: calls={call_ids} results={result_ids}"
        return True, "OK"

    # ── 读取 ─────────────────────────────────────────────────────────────

    def get_full_context(self) -> List[dict]:
        """返回 Anthropic Messages API 格式的上下文列表。"""
        return [m.to_dict() for m in self.storage]

    # ── 截断 ─────────────────────────────────────────────────────────────

    def _truncate_safely(self):
        """按对话回合截断，永远不切散工具调用配对。

        策略：
        1. 软上限：max_turns * 2 条消息（一回合粗略算 2 条：user + assistant）。
        2. 实际工具回合可能消息更多，软上限只是触发点。
        3. 触发后找安全切点：必须落在 role="user"（真实用户输入）的开头。
        4. tool_calls / tool_results / tool_call / tool 这些内部消息不作为切点。
        """
        # ⭐⭐⭐ 衰减阶梯开着时，**它是唯一的正常治理权威**。
        #
        # 🔴 两套机制并存的后果：
        #      衰减阶梯：正准备把一个 Exchange L0→L1→L2
        #      旧截断：不用麻烦了，已经直接删掉了。
        #    ⚠️ **而且不会报错。**
        #
        # ⭐ 所以旧的 10 轮硬切退位成 **catastrophic last resort**：
        #    阶梯开着 → 它只在「阶梯已经失效、上下文仍在失控增长」时才出手，
        #    而且**必须响亮报警**，不能继续像以前那样静默切。
        # 📌 **一个兜底如果和正常机制一样安静，你永远不知道正常机制什么时候死的。**
        _ladder = False
        try:
            from core.models import ladder_enabled as _le
            _ladder = _le()
        except Exception:
            pass

        soft_limit = self.max_turns * 2
        if _ladder:
            # ══════════════════════════════════════════════════════════
            # 🔴🔴 阶梯开着时，这道兜底必须**和阶梯量同一样东西**
            # ══════════════════════════════════════════════════════════
            #
            # 原来是「条数 > max_turns*10」。而阶梯的判据是 **token 厚度**
            # （L0 层 > 25% × 窗口）。两把尺子量不同的东西，于是：
            #
            #     一段很长但每条都很短的会话（连着几十句「嗯」「继续」、
            #     跑测试时的短问短答）→ **条数到 115，token 远不到 50K**
            #     → 阶梯**正确地什么都不做** → 这道兜底却报警说
            #       「阶梯没能跟上」，然后**硬切掉一批阶梯有意留着的历史**。
            #
            # 🔴 危害不是日志吵：它**绕过阶梯直接删 storage**，
            #    而阶梯本来会把那些历史降到 L1/L2（可召回、可重建），不是删掉。
            # 📌 **一个兜底如果用另一把尺子判断「主机制失灵了没有」，
            #    它迟早会在主机制完全正常时开火** —— 而它开火的方式，
            #    恰恰是主机制刻意避免的那种（硬删 vs 降级）。
            # ⚠️ 2026-08-16 实际运行中撞到，那条 ERROR 里
            #    「应当回头检查配额/触发策略」在这种场景下是**误导**：
            #    配额没问题，是这道守卫问错了问题。
            #
            # ⭐ 改成：**按 token 量**，阈值取「窗口 × 阶梯全部配额之和」的
            #    一个明显倍数 —— 到那儿才叫「阶梯确实没跟上」。
            _tok_limit = 0
            try:
                from core.models import quota_of, window_of
                _w = int(window_of(self._last_model_id or "") or 0)
                _q = quota_of(self._last_model_id or "") or {}
                _sum_q = sum(float(v or 0) for v in _q.values())
                if _w > 0 and _sum_q > 0:
                    # ⚠️ 1.5 倍不是调参，是**量级**：正常阶梯下永远到不了
                    #    （阶梯会把每一层压回它自己的配额以内）。
                    _tok_limit = int(_w * _sum_q * 1.5)
            except Exception:
                pass

            if _tok_limit <= 0:
                # ⚠️ 算不出窗口/配额（未知模型）→ **退回条数**，但阈值放得很松。
                #    📌 fail-safe 方向：宁可晚切，不可在阶梯正常时误切 ——
                #       误切删掉的是不可重建的历史。
                soft_limit = self.max_turns * 40
                if len(self.storage) > soft_limit:
                    logger.error(
                        f"[Memory] 🔴 阶梯开着但算不出 token 上限（未知模型），"
                        f"退回条数判据：{len(self.storage)} 条 > {soft_limit} "
                        f"—— 触发最后安全截断。")
                if len(self.storage) <= soft_limit:
                    return
            else:
                _now_tok = 0
                try:
                    from core.context.decay import estimate_tokens
                    _now_tok = estimate_tokens(self.storage)
                except Exception:
                    pass
                if _now_tok <= _tok_limit:
                    return
                logger.error(
                    f"[Memory] 🔴 阶梯开着，上下文仍涨到 {_now_tok:,} token "
                    f"(> {_tok_limit:,}，= 窗口×全部配额×1.5) —— **触发最后安全截断**。"
                    f"⚠️ 这一次是真的没跟上（判据与阶梯同为 token），"
                    f"该查的是配额/触发策略，而不是提高这个上限。")
                # 落到下面走同一套「按交换切」的逻辑。

        if not _ladder and len(self.storage) <= soft_limit:
            return

        # ⭐ 切点问「一次交换」那一层要，**不再自己算**。
        #
        # 原来这里是 `[i for i, m in enumerate(self.storage) if m.role == "user"]`
        # —— 规则完全一样，但它是这个概念在项目里的**第一处**实现，
        # 另外两处在 `app._replay_durable_conversation` 和搜索的 DOM 扫描里。
        # 📌 **一个概念只要还有第二处实现，它迟早会有第二种含义** ——
        #    而 L2 的定义就是「一次交换压成一条结论行」，
        #    单位漂了，阶梯就压错东西。
        # ⚠️ 行为一个字节都没变（有测试逐位对拍旧算法）。
        try:
            from core.context.exchange import user_cut_points
            user_positions = user_cut_points(self.storage)
        except Exception:
            # 兜底：观测层/视图层的故障不许让截断失效 —— 不截断 = 上下文无限涨。
            user_positions = [i for i, m in enumerate(self.storage) if m.role == "user"]
        if len(user_positions) <= self.max_turns:
            return

        cut_index = user_positions[-self.max_turns]
        if cut_index <= 0:
            return
        self.storage = self.storage[cut_index:]

    def pop_last(self) -> Optional[ChatMessage]:
        """弹出最后一条消息。orchestrator 的 _clean_damaged_memory 会用。"""
        if not self.storage:
            return None
        return self.storage.pop()

    def peek_last(self) -> Optional[ChatMessage]:
        """看一眼最后一条消息但不弹出。"""
        return self.storage[-1] if self.storage else None

    # ── 重置 ─────────────────────────────────────────────────────────────

    def reset(self):
        """清空所有对话历史。用于"重置当前对话"按钮。"""
        self.storage = []

    def __len__(self) -> int:
        return len(self.storage)
