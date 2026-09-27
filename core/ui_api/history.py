# -*- coding: utf-8 -*-
"""对话历史：重放用的消息、已经移出上下文的交换、历史图片、导出、上下文用量。"""
from __future__ import annotations

from typing import Any

from core.ui_api import _state


def _memory():
    if _state.memory is None:
        raise RuntimeError("后端还没启动（ui_api.boot 未执行）")
    return _state.memory


def messages() -> list[dict]:
    """当前会话的全部消息（被动重放用，不喂模型）。

    每条是落盘时的那个形状（`core.runtime.conversation._message_payload`：`content`、`tool_calls`、
    `tool_results`、`visible_to_user`、`reply_quote`、`render_kind`、`ui_images` …），另加
    `role`、`ordinal`（会话里的序号）与 `created_at`（落盘时刻，重算工具阶段耗时用）。
    """
    from core.runtime.conversation import _message_payload
    out = []
    for m in _memory().conversation_messages():
        d = dict(_message_payload(m))
        d["role"] = m.role
        d["ordinal"] = getattr(m, "_conversation_ordinal", None)
        d["created_at"] = getattr(m, "_conversation_created_at", None)
        out.append(d)
    return out


def moved_out_exchanges() -> list[dict]:
    """已经移出上下文的交换（老 → 新）：`[{"ordinal", "end_ordinal", "index_entry", "recallable"}]`。

    两档都在里面（这个列表回答「哪些消息不该出现在聊天区」，两档答案相同）；`recallable` 区分它们：
    为真 = 索引还在注入，Nano 还想得起这条线索；为假 = 记忆还在库里，但不再自动想起。
    读的是 `active_entries`（陈旧的派生物不算数）。
    """
    from core.context.decay_store import L3, L4, DecayStore
    from core.runtime.kernel import get_kernel
    sid = _memory().conversation_session_id
    if not sid:
        return []
    rows = DecayStore(get_kernel().store).active_entries(sid)
    out = []
    for o, e in sorted(rows.items()):
        lv = str(e.get("level"))
        if lv in (L3, L4) and e.get("index_entry"):
            out.append({"ordinal": int(o), "end_ordinal": int(e.get("end_ordinal", o)),
                        "index_entry": str(e.get("index_entry")), "recallable": lv == L3})
    return out


def image_uri(ref: str) -> str:
    """用户发过的一张图的 `data:` URI（图库里已经没有了为空串）。"""
    from core.runtime.blobs import image_data_uri
    return image_data_uri(ref) or ""


def export(dest_dir: str) -> dict:
    """把库里全部会话导出到 `dest_dir` 下的新文件夹：`{"dir", "sessions", "messages", "images", ...}`。"""
    from core.runtime.export import export_all
    from core.runtime.kernel import get_kernel
    return dict(export_all(dest_dir, get_kernel()))


def context_budget() -> dict[str, Any]:
    """当前主模型的上下文用量：`{"known", "used", "window", "ratio", "level", "model", ...}`。"""
    from core.context.budget import snapshot
    return dict(snapshot(getattr(_state.provider, "target_model", "") or ""))
