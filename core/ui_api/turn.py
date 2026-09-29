# -*- coding: utf-8 -*-
"""轮次：发消息、终止、引用回复、确认的回复、后端事件的订阅、唤醒 / 取消等待、重置对话。

后端这一轮的事件由会话调度器泵到事件总线（`core.runtime.events`）；界面订阅一次，按轮 id 分发。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from core.runtime.events import TURN_END  # noqa: F401  （界面据此判断一轮的事件流结束）
from core.ui_api import _state


def _sched():
    from core.session import get_scheduler
    return get_scheduler()


# ── 事件与确认 ────────────────────────────────────────────────────────────
def subscribe_events():
    """订阅后端事件总线（界面启动时一次；起来之前发出的事件在队列里等着）。"""
    from core.runtime import events
    return events.subscribe()


def reply(reply_id: str, action: str, *args: Any) -> bool:
    """回复一个等待中的确认 / 选择（`reply_id` 来自事件）。返回这一回复是否被接收。"""
    from core.runtime.replies import resolve
    return bool(resolve(reply_id, action, *args))


def reply_handler(event: Mapping[str, Any], action: str) -> Optional[Callable[..., Any]]:
    """把事件里的 `reply_id` 与一个动作名包成按钮用的回调；事件没声明这个动作时为 None。"""
    from core.runtime.replies import reply_callback
    return reply_callback(event, action)


def check_event(event: Any) -> None:
    """事件的可序列化检查（问题只记日志）。"""
    from core.runtime.wire import warn_if_not_serializable
    warn_if_not_serializable(event)


# ── 发消息 / 终止 ─────────────────────────────────────────────────────────
def submit(text: str, *, image_bytes: Optional[bytes] = None, image_mime: str = "image/jpeg",
           temp_hint: Optional[str] = None, can_continue: bool = False,
           reply_target: Optional[dict] = None) -> tuple[str, str]:
    """收下一条用户消息。返回 `(key, mode)`，mode：`run` 立刻起一轮 / `cont` 插话续接当前回应期 /
    `queued` 排队。只有附件、没有文字时，交给模型的是一句「请处理上传的内容」（带语言偏好）。
    `reply_target` 是这条消息的引用（`take_reply_target()` 取到的），随消息排队。
    补上说明的消息标成「只发附件」，界面（包括重放）显示「（附件已发送）」而不是那句说明。"""
    attachment_only = not (text or "").strip()
    if attachment_only:
        from core.i18n import language_clause
        text = "Please process the uploaded content. " + language_clause("your reply")
    return _sched().submit_user_message(text, image_bytes=image_bytes, image_mime=image_mime,
                                        temp_hint=temp_hint, can_continue=can_continue,
                                        reply_target=reply_target,
                                        attachment_only=attachment_only)


def busy() -> bool:
    """后端现在有没有一轮在跑（持有调度锁）。"""
    return _sched().busy()


def has_queued() -> bool:
    """调度器的队列里还有没有东西（用户消息或唤醒）。"""
    return bool(_sched().parked)


def request_stop(source: str = "user") -> None:
    _state.require_agent().request_stop(source)


# ── 引用回复 ──────────────────────────────────────────────────────────────
def reply_target() -> Optional[dict]:
    """下一条消息在引用什么：`{"iid", "q", "kind"}` 或 None（权威副本在 orchestrator）。"""
    rt = getattr(_state.require_agent(), "_reply_target", None)
    return dict(rt) if rt else None


def set_reply_target(target: Optional[dict]) -> None:
    _state.require_agent()._reply_target = dict(target) if target else None


def take_reply_target() -> Optional[dict]:
    """发出消息时取走引用（界面侧随即复位），交给 `submit(reply_target=…)` 随这条消息走。"""
    return _state.require_agent().take_reply_target()


# ── 对话里的界面记录 / 重置 ──────────────────────────────────────────────
def record_ui_error(text: str) -> None:
    """一张只给用户看的系统错误卡（落盘，重放时照样显示；不进模型上下文）。"""
    _state.require_agent().memory.add_ui_only_record(text, "sys_error")


def reset_conversation() -> dict:
    """用户显式重置对话：丢弃排队的消息、开新会话、作废上下文厚度的记录、清空本轮临时附件。
    返回 `{"msg", "discarded"}`。有一轮在跑时不重置（`{"busy": True}`）。"""
    if busy():
        return {"busy": True, "msg": "", "discarded": 0}
    discarded = 0
    try:
        from core.runtime import inbox
        discarded = int(inbox.discard_all_pending("用户重置对话") or 0)
    except Exception:
        pass
    _sched().discard_parked()
    result = _state.require_agent().reset_conversation() or {}
    try:
        from core.context.meter import forget_conversation_size, get_meter
        forget_conversation_size()
        get_meter()._anchor = None      # 本次运行的锚也作废（历史真的没了）
    except Exception:
        pass
    try:
        from core import rag
        rag.clear_temp_knowledge()
    except Exception:
        pass
    return {"busy": False, "msg": str(result.get("msg") or ""), "discarded": discarded}


# ── 等待 ──────────────────────────────────────────────────────────────────
def wait_outcome(wait_id: str) -> Optional[dict]:
    """一条等待的终态：`{"status", "satisfied_by"}`；查不到记录为 None。"""
    from core.runtime import waitcond
    from core.runtime.kernel import get_kernel
    rec = waitcond.find_by_id(get_kernel(), wait_id)
    if rec is None:
        return None
    return {"status": str(rec.status), "satisfied_by": str(rec.satisfied_by or "")}


def wake_now(wait_id: str) -> str:
    """用户点「立即执行」：`ended`（等待已结束）/ `parked`（忙，已排队）/ `started`。"""
    return _sched().wake_now(wait_id)


def cancel_wait(wait_id: str) -> bool:
    return bool(_sched().cancel_wait(wait_id))
