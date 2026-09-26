# -*- coding: utf-8 -*-
"""后台载体：被交还后继续跑的长任务（长命令、MCP 调用、本地 Skill、Subagent）。

一次调用跑过前台耐心阈值后，控制权交回模型，那个调用本身作为「载体」继续跑；
完成（或被终止、失败）时通知等待它的挂起，由唤醒让 Nano 带着结果继续。

载体的语义由 `rt_task_id` 区分：
  · None —— 系统交还：当前工作仍依赖它的结果，不进后台任务抽屉，没有权威 Task 记录。
  · 有值 —— 已不在 Nano 手头（Subagent 生来如此，或模型调了 `dont_wait`）：
    进抽屉、计入 `x running task(s)`、可以用 `■` 终止。

- `start`：登记并起跑一个载体（`asyncio.Task` 句柄保存在表里：终止的前提，也防止被 GC）。
- `promote`：`dont_wait` → 给载体建权威记录，进抽屉；不碰载体本身。
- `cancel`：用户按 `■` 终止（先记「用户按的」，再取消）。
- `heartbeat`：给还在跑的载体推后等待记录的 `orphan_at`（由后端心跳周期调用）。
- 完成通知交给 `set_completion_handler` 登记的回调（`(ref, result_hint)`）；
  状态变化（起跑 / 结束 / 进抽屉）通知 `add_listener` 登记的监听者（界面刷新用）。

一条权威记录只有一个收尾人：`owns_record=False` 的载体（Subagent）由它自己的协程收尾，
这里不再收，避免用更粗的结论覆盖。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

_carriers: dict[str, dict] = {}
_completion_handler: Optional[Callable[[str, str], Awaitable[None]]] = None
_listeners: list[Callable[[dict], None]] = []

USER_STOPPED_NOTE = (
    "[System record: the user manually stopped this from the task drawer. It did not "
    "fail and it did not finish - the user decided to stop it. Do NOT restart it on "
    "your own; tell them plainly that it was stopped and let them decide what happens next.]")
UNKNOWN_STOP_NOTE = (
    "[System record: this stopped before it returned a result, and nobody asked for that - "
    "it was not the user. Do NOT assume it succeeded, and say plainly that you do not know "
    "why it stopped.]")


def set_completion_handler(fn: Optional[Callable[[str, str], Awaitable[None]]]) -> None:
    """登记载体结束时的通知回调：`await fn(suspension_ref, result_hint)`。"""
    global _completion_handler
    _completion_handler = fn


def add_listener(fn: Callable[[dict], None]) -> None:
    """登记状态变化监听：`fn({"event": "carrier_started" | "carrier_finished" |
    "carrier_promoted", "skill_name": ..., "display": ..., "rt_task_id": ...})`。"""
    if fn not in _listeners:
        _listeners.append(fn)


def remove_listener(fn: Callable[[dict], None]) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def _notify(ev: dict) -> None:
    for fn in list(_listeners):
        try:
            fn(ev)
        except Exception as e:
            logger.debug(f"[B1] 载体监听者出错（忽略）: {e}")


def start(display: str, awaitable: Awaitable[Any], suspension_ref: str, *,
          skill_name: str = "", rt_task_id: Optional[str] = None,
          owns_record: bool = True) -> str:
    """登记并起跑一个载体，返回 carrier_id。必须在运行中的事件循环里调用。

    `awaitable` 的结果是给模型看的一段话；抛异常记为失败，结果写成「后台执行失败：…」。
    """
    carrier_id = uuid.uuid4().hex[:8]

    async def _run():
        _outcome, _note = "completed", ""
        try:
            result = await awaitable
        except asyncio.CancelledError:
            # 用户点了 `■`（或进程收尾）。仍要通知等待方，避免活的等待记录挂在已死的载体上。
            # cancelled 不并进 failed：用户主动停掉不是失败。
            _outcome = "cancelled"
            if (_carriers.get(carrier_id) or {}).get("cancelled_by_user"):
                _note, result = "用户手动终止", USER_STOPPED_NOTE
            else:
                _note, result = "载体在返回结果前终止", UNKNOWN_STOP_NOTE
        except Exception as e:
            _outcome, _note = "failed", f"{type(e).__name__}: {e}"[:160]
            result = f"后台执行失败：{type(e).__name__}: {e}"
        finally:
            _meta = _carriers.pop(carrier_id, None) or {}
            # 从表里现读 id：`dont_wait` 是在载体起跑之后才补上它的。
            _rt = _meta.get("rt_task_id") or rt_task_id
            if _rt and _meta.get("owns_record", owns_record):
                try:
                    from core.runtime import task as _rt_task
                    _rt_task.finish_background_job(_rt, _outcome, _note)
                except Exception as _e_fc:
                    logger.warning(f"[B1] 载体 {carrier_id} 收权威记录失败: {_e_fc}")
            _notify({"event": "carrier_finished", "skill_name": skill_name,
                     "display": display, "rt_task_id": _rt or "", "outcome": _outcome})
        # 这一步也要接住 CancelledError：cancel() 可能在协程不在 await 点时到达，
        # 于是下一个 await（这个通知）再抛一次。权威记录已在上面同步落好，
        # 最坏只丢一次通知，等待那边有 orphan 兜底。
        try:
            if _completion_handler is not None:
                await _completion_handler(suspension_ref, result)
            else:
                logger.warning(f"[B1] 载体 {carrier_id} 结束但没有登记完成通知回调（ref={suspension_ref}）")
        except asyncio.CancelledError:
            logger.warning(
                f"[B1] 载体 {carrier_id} 的收尾通知被第二次取消打断 —— "
                f"权威记录已落（{_outcome}），等待那边靠 orphan 兜底回收")

    aio = asyncio.ensure_future(_run())
    _carriers[carrier_id] = {"display": display, "aio": aio,
                             "suspension_ref": suspension_ref,
                             "skill_name": skill_name,
                             "rt_task_id": rt_task_id,
                             "owns_record": owns_record}
    _notify({"event": "carrier_started", "skill_name": skill_name,
             "display": display, "rt_task_id": rt_task_id or ""})
    return carrier_id


def promote(bg_ref: str, display: str) -> bool:
    """`dont_wait`：给这个载体建一条权威记录，让它进抽屉。载体本身照旧在跑。"""
    _hit = next(((cid, m) for cid, m in _carriers.items()
                 if (m or {}).get("suspension_ref") == bg_ref), None)
    if _hit is None:
        # 最常见的原因是它刚好跑完了 —— 那时不该再建一条 Running。
        logger.info(f"[B1] dont_wait 找不到载体 {bg_ref}（多半刚完成）—— 不建记录")
        return False
    _cid, _meta = _hit
    if _meta.get("rt_task_id"):
        return True                          # 幂等：同一条别建两次
    try:
        from core.runtime import task as _rt_task
        _tid = _rt_task.create_background_job(
            display or _meta.get("display") or "后台任务", _rt_task.owner_label())
    except Exception as e:
        # 记录建不上，那个调用照旧在后台跑、照旧会唤醒 Nano。
        logger.warning(f"[B1] dont_wait 建后台任务记录失败（载体照旧跑）: {e}")
        return False
    _meta["rt_task_id"] = str(getattr(_tid, "task_id", "") or _tid or "")
    # 它此刻就在跑：不 mark 的话抽屉会把一个正在跑的东西显示成「排队中」。
    try:
        _rt_task.mark_background_running(_meta["rt_task_id"])
    except Exception as _e_mr:
        logger.debug(f"[B1] 载体转 RUNNING 失败（照旧跑）: {_e_mr}")
    logger.info(f"[B1] {(display or '')[:40]} 进抽屉 "
                f"（carrier={_cid} task={_meta['rt_task_id']}）")
    _notify({"event": "carrier_promoted", "skill_name": _meta.get("skill_name", ""),
             "display": display, "rt_task_id": _meta["rt_task_id"]})
    return True


def cancel(rt_task_id: str) -> bool:
    """终止一条已不在手头的载体（Subagent / 被 `dont_wait` 的调用）。

    收尾不在这里做：`cancel()` 之后 `_run` 拿到 CancelledError，在那里记 cancelled 并通知等待方。
    先落「用户按的」标记再取消，否则取消分支可能先跑到、读到「没人按过停」。
    """
    for _cid, _meta in list(_carriers.items()):
        if (_meta or {}).get("rt_task_id") != rt_task_id:
            continue
        _aio = (_meta or {}).get("aio")
        if _aio is None or _aio.done():
            return False
        _meta["cancelled_by_user"] = True
        _aio.cancel()
        logger.info(f"[B1] 用户终止载体 {_cid}（task={rt_task_id}）")
        # 取消的只是 Nano 这边的等待；命令进程要另外停掉（D51）。
        # MCP（服务在对端）/ Skill（进程内执行）停不掉，如实记日志。
        _ref = str(_meta.get("suspension_ref") or "")
        if _ref.startswith("cmd_"):
            try:
                from core.os_layer import longcmd as _lc
                if _lc.stop(_ref, "stopped by user from the task drawer"):
                    logger.info(f"[B1] 载体 {_ref} 的命令进程已停止")
                else:
                    logger.info(f"[B1] 载体 {_ref} 的命令已经不在了（多半刚跑完）")
            except Exception as e:
                logger.warning(f"[B1] 停止载体 {_ref} 的命令失败（等待已取消）: {e}")
        elif _ref:
            logger.info(f"[B1] 载体 {_ref} 属于停不掉的一类（MCP / Skill）—— 只取消了等待，它可能仍在运行")
        return True
    return False


def heartbeat() -> None:
    """还在跑的载体 → 推后它那条等待记录的 `orphan_at`（载体还活着 = 它还会回来）。"""
    try:
        from core.runtime import waitcond as _wc
    except Exception:
        return
    for _m in list(_carriers.values()):
        _aio = _m.get("aio")
        if _aio is None or _aio.done():
            continue
        _ref = str(_m.get("suspension_ref") or "")
        if _ref:
            try:
                _wc.touch_by_bg_ref(_ref)
            except Exception as e:
                logger.debug(f"[B1] 载体心跳跳过: {e}")


def skill_running(name: str) -> bool:
    """该 Skill 是否仍有一个没结束的载体。"""
    return any(m.get("skill_name") == name for m in _carriers.values())


def running_skill_names() -> set:
    return {m.get("skill_name") for m in _carriers.values() if m.get("skill_name")}


def snapshot() -> list[dict]:
    """当前载体的展示信息（不含 asyncio 对象）。"""
    return [{"carrier_id": cid, "display": m.get("display", ""),
             "suspension_ref": m.get("suspension_ref", ""),
             "skill_name": m.get("skill_name", ""), "rt_task_id": m.get("rt_task_id") or ""}
            for cid, m in _carriers.items()]


def _reset_for_tests() -> None:
    global _completion_handler
    _carriers.clear()
    _listeners.clear()
    _completion_handler = None
