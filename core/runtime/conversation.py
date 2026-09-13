"""当前会话的可恢复聊天原文。

这里保存的是 ChatMessage 的权威、可逆形状；MemoryManager.storage 只是当前
会话中受上下文窗口限制的内存投影。Task 不参与本模块：Task 是一件工作的归属，
会话是用户显式「重置对话」之前的一段聊天原文，两者不能互相替代。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from core.runtime.store import RuntimeStore
from core.schema import ChatMessage, ToolCall, ToolResultBlock


@dataclass(frozen=True)
class ConversationSession:
    session_id: str
    created_at: float
    closed_at: float | None = None
    close_reason: str | None = None


def _strip_persisted_images(message: ChatMessage) -> Any:
    """⚠️⚠️ **权威账本不存像素** —— 像素在 `data/chat_images/` 的图库里。

    🔴 为什么必须收口在这里，而不是"每个调用点记得别写"：
       `MemoryManager` 里凡是改了消息又要落盘的地方（`attach_user_images` /
       `set_image_summary` / 将来任何一个）都会 `update_message(msg)`，
       而那一刻 `msg.content` **正是带 base64 的那份**（orchestrator 在
       ReAct 之前把 image_parts 拼了进去）。少写一处 = 一整张图的 base64
       进 SQLite，payload 从两百字节涨到几百 K，而且**图在库里已经有一份了**。
       📌 同一个错在两天里差点犯了两次 —— 所以修的是**唯一的出口**，不是调用点。

    ⚠️ **只有确认像素已经安全落进图库（`ui_images` 非空）才允许拿掉。**
       📌 fail-safe 方向：宁可账本里多一份 base64，不可两边都没有。
    """
    content = message.content
    if not message.ui_images or not isinstance(content, list):
        return content
    kept = [b for b in content
            if not (isinstance(b, dict) and b.get("type") in ("image", "image_url"))]
    if not kept:
        return ""
    return kept


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    """Return the reversible ChatMessage representation, never provider API shape."""
    return {
        "content": _strip_persisted_images(message),
        # ⭐ [2026-08-13] 可见性跟着原文一起落盘 —— 见 `ChatMessage.visible_to_user`。
        # ⚠️ 只在为 False 时写，让**老行与普通消息的 payload 一个字节都不变**
        #    （读回时缺这个键就是 True）。📌 **新增一个可选事实，不该改写既有事实的形状。**
        **({} if message.visible_to_user else {"visible_to_user": False}),
        # ⭐ 用户发过的图（Nano 自存那份的引用）。同上：**为空时不写**。
        **({"ui_images": list(message.ui_images)} if message.ui_images else {}),
        **({"image_summary": message.image_summary} if message.image_summary else {}),
        # ⭐ [2026-08-22] 引用指向跟着消息落盘。同上：**为空时不写**，
        #    老行与普通消息的 payload 一个字节都不变。
        **({"reply_quote": message.reply_quote} if message.reply_quote else {}),
        # ⭐ [2026-08-22] 系统事件（`sys_error`）跟着消息落盘。同上：**为空时不写**，
        #    老行与普通消息的 payload 一个字节都不变。
        **({"render_kind": message.render_kind}
           if getattr(message, "render_kind", "") else {}),
        "name": message.name,
        "args": message.args,
        "tool_use_id": message.tool_use_id,
        "thinking_blocks": message.thinking_blocks,
        "tool_calls": [
            {"name": call.name, "args": call.args, "tool_use_id": call.tool_use_id,
             "index": call.index}
            for call in message.tool_calls
        ],
        "tool_results": [
            {"name": result.name, "tool_use_id": result.tool_use_id,
             "content": result.content, "is_error": result.is_error,
             "raw_result": result.raw_result, "tool_data": result.tool_data}
            for result in message.tool_results
        ],
    }


def _message_from_payload(role: str, payload: dict[str, Any], ordinal: int | None = None,
                          created_at: float | None = None) -> ChatMessage:
    message = ChatMessage(
        role=role,
        content=payload.get("content", ""),
        name=payload.get("name"),
        args=payload.get("args") or {},
        tool_use_id=payload.get("tool_use_id"),
        thinking_blocks=payload.get("thinking_blocks") or [],
        tool_calls=[ToolCall(**call) for call in payload.get("tool_calls") or []],
        tool_results=[ToolResultBlock(**result) for result in payload.get("tool_results") or []],
        # 缺这个键 = 老行 / 普通消息 → True（见 `_message_payload` 的注释）。
        #
        # ⚠️ **刻意不给老行做正文猜测式回填**（写过一版，否掉了，那是对的）：
        #    08-13 之前落盘的注记确实没有这一列，但那些全是测试数据，
        #    「重置对话」一下就没了。为一次性的旧数据留一个**永久**的前缀启发式，
        #    正好是本文件上一段刚否掉的那件事。
        # 📌 **一个只为一次性需求存在的启发式，会永远留在代码里** ——
        #    而它每多活一天，就多一分被后人当成"判据"照抄的机会。
        visible_to_user=payload.get("visible_to_user", True),
        ui_images=payload.get("ui_images") or [],
        image_summary=payload.get("image_summary") or "",
        reply_quote=payload.get("reply_quote") or "",
        render_kind=payload.get("render_kind") or "",
    )
    if ordinal is not None:
        setattr(message, "_conversation_ordinal", ordinal)
    # 把落盘时刻带出来，供 UI 被动重放时重算「工具阶段耗时」（不喂 provider）。
    if created_at is not None:
        setattr(message, "_conversation_created_at", created_at)
    return message


class ConversationRepository:
    """Transactional repository for the one current durable conversation."""

    def __init__(self, store: RuntimeStore):
        self._store = store

    @property
    def store(self):
        """这个仓储绑的那个 store。

        ⚠️ 加它是因为踩了一次：衰减重放里写了 `get_kernel().store`，
           而调用方（`MemoryManager`）**明明已经绑定了自己的仓储**。
           于是测试里用临时库、重放却去读生产库 —— 什么都没发生，**且不报错**。
        📌 **一个对象已经知道自己连的是谁时，别再去问全局要一遍** ——
           那等于给同一件事造了第二个权威，而它们只在"默认情况"下相等。
        """
        return self._store

    @staticmethod
    def _session(row: Any) -> ConversationSession:
        return ConversationSession(
            session_id=str(row["session_id"]),
            created_at=float(row["created_at"]),
            closed_at=float(row["closed_at"]) if row["closed_at"] is not None else None,
            close_reason=row["close_reason"],
        )

    def current_session(self) -> ConversationSession:
        with self._store.write_txn() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_sessions WHERE is_current=1"
            ).fetchone()
            if row is not None:
                return self._session(row)
            now = time.time()
            session = ConversationSession(uuid.uuid4().hex, now)
            conn.execute(
                "INSERT INTO conversation_sessions(session_id,is_current,created_at) VALUES(?,?,?)",
                (session.session_id, 1, now),
            )
            return session

    def append_message(self, session_id: str, message: ChatMessage) -> int:
        """Append exactly one message and return its session-local ordinal."""
        payload = json.dumps(_message_payload(message), ensure_ascii=False, default=str,
                             separators=(",", ":"))
        with self._store.write_txn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversation_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"未知 conversation session: {session_id}")
            ordinal = int(conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM conversation_messages WHERE session_id=?",
                (session_id,),
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO conversation_messages(message_id,session_id,ordinal,role,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, session_id, ordinal, message.role, payload, time.time()),
            )
            setattr(message, "_conversation_ordinal", ordinal)
            return ordinal

    def update_message(self, session_id: str, message: ChatMessage) -> None:
        """Persist a deliberate in-memory rewrite of an already appended message."""
        ordinal = getattr(message, "_conversation_ordinal", None)
        if not isinstance(ordinal, int):
            raise ValueError("消息没有 durable ordinal，不能原地更新")
        payload = json.dumps(_message_payload(message), ensure_ascii=False, default=str,
                             separators=(",", ":"))
        with self._store.write_txn() as conn:
            updated = conn.execute(
                "UPDATE conversation_messages SET role=?,payload_json=? WHERE session_id=? AND ordinal=?",
                (message.role, payload, session_id, ordinal),
            ).rowcount
            if updated != 1:
                raise ValueError(f"未找到 conversation message: {session_id}/{ordinal}")

    def load_messages(self, session_id: str) -> list[ChatMessage]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT ordinal,role,payload_json,created_at FROM conversation_messages "
                "WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        return [_message_from_payload(str(row["role"]), json.loads(row["payload_json"]),
                                      int(row["ordinal"]),
                                      float(row["created_at"]) if row["created_at"] is not None else None)
                for row in rows]

    def reset_current_session(self, reason: str = "用户重置对话") -> ConversationSession:
        """Close the old current session and create the only new current session."""
        with self._store.write_txn() as conn:
            now = time.time()
            conn.execute(
                "UPDATE conversation_sessions SET is_current=0,closed_at=?,close_reason=? "
                "WHERE is_current=1",
                (now, reason),
            )
            session = ConversationSession(uuid.uuid4().hex, now)
            conn.execute(
                "INSERT INTO conversation_sessions(session_id,is_current,created_at) VALUES(?,?,?)",
                (session.session_id, 1, now),
            )
            return session
