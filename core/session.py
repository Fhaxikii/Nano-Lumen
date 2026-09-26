# -*- coding: utf-8 -*-
"""会话调度器：谁在什么时候起一轮（S6-6b 业务状态下沉）。

一次只能有一轮在跑（`lock`）。一轮起不来的时候（忙 / 预算到硬上限）进队列，
上一轮结束后由 `drain` 接上 —— 闸的出口是失败，队列的出口是稍后处理。

在这里决定的：
  · 用户消息（`submit_user_message`）：闲 → 立刻起一轮；忙且当前回应期还在跑 →
    插话，切开回应期，排在当前轮之后续接同一个气泡；忙但接不上 → 排队。
    回应期的段号（`seam_part`，第几段）也在这里记，交给 orchestrator 写进提示。
  · 唤醒轮：定时到点（`poll_due`，由后端心跳调用）、后台载体完成
    （`notify_background_done`，载体表的完成回调）、用户点「立即执行」（`wake_now`）
    与「取消计划」（`cancel_wait`）。
  · 排队与排空：`parked`（内存侧，只放数据）+ durable inbox（库侧，负责「不丢」）。
    附件字节只在内存里：重启后队列里那条保留文字、丢掉附件（已知缺口）。
  · 前台空了 → 给没安排回看的后台等待排第一次回看（60 秒）。

每一轮的后端事件流由这里泵出（`events.pump_turn`），带着轮 id 发到事件总线
（`core.runtime.events`）；锁一直持有到后端这一轮真正结束（界面先收尾不影响）。

呈现方（界面）只负责画：用户轮（`render_user_turn`）与唤醒轮（`run_wake_turn`）的
气泡——它从总线读自己那一轮（按 `turn_id`）的事件渲染——以及等待 pill 的定型
（`settle_wake` / `settle_cancelled_handback`）。
每一轮开始 / 结束通知 `add_turn_listener` 登记的监听者（主动智能的「正在回复」等）。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Optional, Protocol

from loguru import logger

_FIRST_RECHECK_SEC = 60.0


# ── durable inbox 门面 ────────────────────────────────────────────────────
# 全部吞异常，退化方向朝「照旧干活」：队列的目的是不丢，不是多一道挡住用户的闸。
def inbox_submit(body: str, detail: dict | None = None) -> str | None:
    try:
        from core.runtime import inbox as _ib
        return _ib.submit_user_message(body, detail)
    except Exception as e:
        logger.error(f"[Inbox] 落库失败（不阻断，照旧处理这条）: {e}")
        return None


def inbox_claim(item_id: str | None) -> None:
    """把**这一条**标成「正在处理」（取哪一条去做，就标哪一条；`mem_` 开头的没落库）。"""
    if not item_id or item_id.startswith("mem_"):
        return
    try:
        from core.runtime import inbox as _ib
        from core.runtime.kernel import get_kernel, Command
        get_kernel().submit(Command(kind=_ib.CLAIM, payload={"item_id": item_id}))
    except Exception as e:
        logger.debug(f"[Inbox] 认领失败（忽略）: {e}")


def inbox_consume(item_id: str | None) -> None:
    if not item_id:
        return
    try:
        from core.runtime import inbox as _ib
        _ib.consume(item_id)
    except Exception as e:
        logger.debug(f"[Inbox] 标记已消费失败（忽略）: {e}")


def inbox_submit_wake(suspension_id: str, trigger: str) -> str | None:
    """收下一个「继续」意图（与用户消息分开的 kind：它恢复一个已有挂起，不起新话题）。"""
    try:
        from core.runtime import inbox as _ib
        return _ib.submit_wake_intent(suspension_id, trigger)
    except Exception as e:
        logger.error(f"[Inbox] 唤醒意图落库失败（不阻断）: {e}")
        return None


def inbox_discard(item_id: str | None, reason: str) -> None:
    if not item_id or item_id.startswith(("mem_", "memwake_")):
        return
    try:
        from core.runtime import inbox as _ib
        _ib.discard(item_id, reason)
    except Exception as e:
        logger.debug(f"[Inbox] 丢弃失败（忽略）: {e}")


def inbox_pending() -> int:
    try:
        from core.runtime import inbox as _ib
        from core.runtime.kernel import get_kernel
        return _ib.pending_count(get_kernel())
    except Exception:
        return 0


async def _with_turn_usage(source):
    """给这一轮的 `final_result` 带上本轮用量（`turn_usage`：token 数与缓存命中率）。

    用量按轮归属（`core.usage` 的上下文变量，由这一轮的后端在开头打点）。这个包装与后端
    在同一个任务里迭代，读到的就是这一轮；界面在别的协程里，读不到这个归属。
    """
    async for ev in source:
        if isinstance(ev, dict) and ev.get("event") == "final_result" and "turn_usage" not in ev:
            try:
                from core.usage import usage_tracker as _ut
                _tid = _ut.context_turn()
                if _tid:
                    ev = {**ev, "turn_usage": {"tokens": _ut.turn_tokens(_tid),
                                               "cache_hit": _ut.turn_cache_hit(_tid)}}
            except Exception as e:
                logger.debug(f"[Turn] 本轮用量没附上（界面退回读当前轮）: {e}")
        yield ev


class Presenter(Protocol):
    """界面（呈现方）要实现的接口。"""

    async def run_wake_turn(self, suspension_id: str, trigger: str,
                            continue_bubble: bool, turn_id: str) -> None:
        """画一个唤醒轮：续接当前气泡或新开一个，渲染 `turn_id` 那一轮的事件。"""

    def settle_wake(self, suspension_id: str, trigger: str) -> None:
        """唤醒轮拿到锁、即将开始：把等待 pill 定型（background 还要收掉原动作的转圈）。"""

    def settle_cancelled_handback(self, bg_ref: str) -> int:
        """载体完成但等待已取消：只收原动作的转圈，返回收了几条。"""

    async def render_user_turn(self, key: str, payload: dict, continuation: bool,
                               turn_id: str) -> None:
        """画一个用户轮：`key` 是提交时返回的那条消息的标识（界面据此找到它的占位）；
        `continuation` 为真时续接当前气泡（插话之后的下一段）；渲染 `turn_id` 那一轮的事件。
        锁由调度器持有。"""


class TurnScheduler:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.parked: dict = {}                 # item_id → 排队项（dict 保序 = 入队顺序）
        self.running_inbox_id: Optional[str] = None
        self.seam_part = 1                     # 当前回应期的第几段（插话一次 +1）
        self.epoch = 0                         # 当前回应期的编号（新开一段回应期 +1，续接不变）
        self.presenter: Optional[Presenter] = None
        self.agent: Any = None                 # 提供 resume_suspension / memory
        self._turn_listeners: list[Callable[[bool], None]] = []
        # 终止时仍在跑、停不掉的载体：ref → 显示名（结束时结果只记入历史，不唤醒）
        self._stopped_refs: dict[str, str] = {}
        try:
            from core.runtime import carriers as _car
            _car.set_epoch_provider(lambda: self.epoch)
        except Exception as e:
            logger.debug(f"[Turn] 载体回应期来源登记失败（终止时无法按回应期停载体）: {e}")

    # ── 登记 ──────────────────────────────────────────────────────────────
    def attach(self, agent: Any, presenter: Presenter) -> None:
        self.agent = agent
        self.presenter = presenter

    def add_turn_listener(self, fn: Callable[[bool], None]) -> None:
        if fn not in self._turn_listeners:
            self._turn_listeners.append(fn)

    def turn_state(self, active: bool) -> None:
        """一轮开始（True）/ 结束（False）：通知监听者。持锁的一方调用。"""
        for fn in list(self._turn_listeners):
            try:
                fn(active)
            except Exception as e:
                logger.debug(f"[Turn] 监听者出错（忽略）: {e}")

    def busy(self) -> bool:
        return self.lock.locked()

    # ── 用户消息 ────────────────────────────────────────────────────────────
    def submit_user_message(self, text: str, *, image_bytes: bytes | None = None,
                            image_mime: str = "image/jpeg", temp_hint: str | None = None,
                            can_continue: bool = False) -> tuple[str, str]:
        """收下一条用户消息，决定它怎么跑。返回 `(key, mode)`：

          · "run"    —— 闲：立刻起一轮（新回应期，段号归 1）
          · "cont"   —— 忙且当前回应期还在跑（界面给 `can_continue`）：插话切开回应期，
                        当前轮结束后续接同一个气泡
          · "queued" —— 忙但接不上：排队，当前轮结束后起新一轮

        无论忙不忙都先落库：`item_id` 是这句话在系统里的唯一身份，崩溃可能发生在任何时刻。
        落库失败（`item_id` 为空）照样干活，用内存 key。
        """
        busy = self.lock.locked()
        item_id = inbox_submit(text, {"had_image": bool(image_bytes),
                                      "temp_hint": bool(temp_hint)})
        payload = {"text": text, "image_bytes": image_bytes, "image_mime": image_mime,
                   "temp_hint": temp_hint}
        if busy:
            if can_continue:
                key = item_id or f"cont_{id(payload)}"
                self.parked[key] = ("cont", payload)
                logger.info(f"[Seam] 内核忙 → 切开回应期（{key}）：predecessor 留在原位，后续写入 successor")
                return key, "cont"
            key = item_id or f"mem_{id(payload)}"
            self.parked[key] = ("user", payload)
            logger.info(f"[Inbox] 内核忙 → 这条进队列（{key}），当前轮结束后自动接上")
            return key, "queued"
        key = item_id or f"mem_{id(payload)}"
        # 认领失败（库挂了）也照样起轮：队列是为了不丢，不是多一道挡住用户的闸。
        inbox_claim(item_id)
        self.running_inbox_id = item_id
        self._set_seam_part(1)
        self.epoch += 1
        asyncio.ensure_future(self.run_user_turn(key, payload, continuation=False))
        return key, "run"

    def _set_seam_part(self, n: int) -> None:
        """回应期的第几段；第 1 段 orchestrator 不加说明（交给它的是 0）。"""
        self.seam_part = n
        try:
            self.agent._seam_continuation_part = 0 if n <= 1 else n
        except Exception:
            pass

    def _user_source(self, payload: dict):
        """用户轮的后端事件流：`agent.handle_query`（图片先转成模型的图片 part）。"""
        _parts = None
        if payload.get("image_bytes"):
            _parts = [self.agent.provider.build_image_part(
                payload["image_bytes"], payload.get("image_mime") or "image/jpeg")]
        return self.agent.handle_query(payload.get("text", ""), image_parts=_parts,
                                       temp_file_hint=payload.get("temp_hint"))

    async def _run_turn(self, source, render) -> bool:
        """泵出这一轮的后端事件（发到总线），同时让呈现方渲染；两边都结束才返回。
        返回呈现方是否正常结束。"""
        from core.runtime import events as _events
        turn_id = uuid.uuid4().hex[:12]
        pump = asyncio.ensure_future(_events.pump_turn(turn_id, _with_turn_usage(source)))
        ok = False
        try:
            if self.presenter is not None:
                await render(turn_id)
                ok = True
            else:
                logger.error(f"[Turn] 没有登记呈现方，第 {turn_id} 轮只在后端跑（界面看不到）")
        finally:
            # 界面先收尾（终止、插话）不影响后端：这一轮真正结束才放锁。
            await pump
        return ok

    async def run_user_turn(self, key: str, payload: dict, continuation: bool) -> None:
        """持锁跑一个用户轮（由呈现方画），结束时收掉 inbox 记录，锁释放后排空。"""
        async with self.lock:
            self.turn_state(True)
            self._activity_event("user_message")
            self._clear_stop()
            try:
                source = self._user_source(payload)
                if await self._run_turn(source, lambda tid: self.presenter.render_user_turn(
                        key, payload, continuation, tid)):
                    self._activity_event("nano_responded")
            except Exception as e:
                logger.error(f"[Turn] 用户轮异常: {e}")
            finally:
                self.turn_state(False)
                self.consume_running()
                self._after_turn()
        # 锁已释放：这一轮跑的时候进来的消息 / 唤醒要在这里接上。
        try:
            await self.drain()
        except Exception as e:
            logger.error(f"[Inbox] 排空队列失败: {e}")

    # ── 唤醒 ──────────────────────────────────────────────────────────────
    def park_wake(self, suspension_id: str, trigger: str, note: str, why: str) -> None:
        """唤醒起不来 → 排进队列（库 + 内存），锁 / 预算恢复后由排空接上。

        同一条挂起只排一次：预算硬上限可能连续多轮都满，每次重排都写一行就是泄漏。
        """
        for _v in self.parked.values():
            if (isinstance(_v, tuple) and len(_v) > 1
                    and _v[0] == "wake" and _v[1] == suspension_id):
                logger.debug(f"[Inbox] {suspension_id} 的唤醒已在队列里，不重复排（{why}）")
                return
        try:
            _wid = inbox_submit_wake(suspension_id, trigger)
            self.parked[_wid or f"memwake_{suspension_id}"] = ("wake", suspension_id, trigger, note)
            logger.info(f"[Inbox] {why} → {trigger} 唤醒进队列（{suspension_id}）")
        except Exception as e:
            # 连队列都进不去 → 响亮报错：丢掉的是「后台任务已经完成」这个事实。
            logger.error(f"[Suspension] 🔴 {trigger} 唤醒既起不来也进不了队列 "
                         f"（{suspension_id}，{why}）—— 这个完成通知丢了: {e}")

    async def drive_wake(self, suspension_id: str, trigger: str, note: str = "",
                         inbox_item_id: str | None = None) -> None:
        """唤醒轮的唯一出口：不管从哪条路返回，排队时认领的那条 inbox 记录都要收掉。"""
        ran = False
        try:
            # inbox_item_id 有值 ⟺ 触发那一刻前台上有东西（忙才会进队列）。
            ran = await self._drive_wake_inner(suspension_id, trigger, note,
                                               busy_at_trigger=bool(inbox_item_id))
        finally:
            if inbox_item_id:
                inbox_consume(inbox_item_id)
        if not ran:
            return
        # 锁已释放、这一条已收掉：唤醒轮跑的时候进来的消息 / 唤醒要在这里接上。
        # 必须在收掉之后：库里同一时刻只能有一条「正在处理」，先排空的话下一条认领不上。
        try:
            await self.drain()
        except Exception as e:
            logger.error(f"[Inbox] 唤醒轮后排空队列失败: {e}")

    async def _drive_wake_inner(self, suspension_id: str, trigger: str,
                                note: str = "", busy_at_trigger: bool = False) -> bool:
        """起一个唤醒轮；返回是否真的起了（进队列时为假）。"""
        if self.lock.locked():
            # 后台等待的 fire_at 为空，定时轮询永远轮不到它：忙时必须进队列，不能静默返回。
            self.park_wake(suspension_id, trigger, note, "内核忙")
            return False
        try:
            from core.usage import sync_budget_health
            if sync_budget_health() == "hard":
                logger.warning(f"[Suspension] 预算已达硬上限，本次不唤醒 "
                               f"{suspension_id}（记录保持 active，唤醒进队列）")
                self.park_wake(suspension_id, trigger, note, "预算已达硬上限")
                return False
        except Exception:
            pass
        async with self.lock:
            self.turn_state(True)
            self._clear_stop()
            if not busy_at_trigger:
                self.epoch += 1              # 不续接原气泡 → 新的一段回应期
            try:
                p = self.presenter
                if p is not None:
                    # 真正拿到锁、即将起唤醒轮时才定型 pill（内核忙时不提前定型）。
                    p.settle_wake(suspension_id, trigger)
                source = self.agent.resume_suspension(suspension_id, trigger, note=note)
                # 同一段回应期（触发那一刻前台上有东西）→ 续接进原气泡。
                if await self._run_turn(source, lambda tid: p.run_wake_turn(
                        suspension_id, trigger, bool(busy_at_trigger), tid)):
                    self._activity_event("nano_responded")
            except Exception as e:
                logger.error(f"[Suspension] 唤醒轮异常: {e}")
            finally:
                self.turn_state(False)
                self._after_turn()
        return True

    # ── 终止 ──────────────────────────────────────────────────────────────
    def _clear_stop(self) -> None:
        """新一轮开始：清掉上一次的终止意图（不清的话上一次的终止会把这一轮也停掉）。"""
        try:
            self.agent.clear_stop()
        except Exception:
            pass

    def _after_turn(self) -> None:
        """一轮结束（持锁）：这一轮被用户终止过 → 回应期到此为止。"""
        try:
            stopped = bool(self.agent._stop_asked())
        except Exception:
            stopped = False
        if stopped:
            try:
                self._settle_stopped_epoch()
            except Exception as e:
                logger.error(f"[Stop] 终止后收尾回应期失败: {e}")

    def _settle_stopped_epoch(self) -> None:
        """用户终止了这一轮：这段回应期不再继续（裁决 73）。

        · 排着的续接段改成新的一轮：它们是用户说的话，照常处理，但不再接进被终止的回应期。
        · 这段回应期里交还、仍在 Nano 手头（没进抽屉）的载体属于前台，一并停下，
          不再唤醒 Nano：命令进程终止；停不掉的（MCP / Skill）自行结束，结果只记入历史；
          已经完成、唤醒在排队的，结果记入历史，不起唤醒轮。
          抽屉里的后台任务不受影响。
        """
        for k, v in list(self.parked.items()):
            if isinstance(v, tuple) and len(v) == 2 and v[0] == "cont":
                self.parked[k] = ("user", v[1])        # 原位替换，排队顺序不变
                logger.info(f"[Stop] 排着的续接段 {k} 改为新的一轮（不接进被终止的回应期）")

        from core.runtime import carriers as _car
        from core.runtime import waitcond as _wc
        from core.runtime.kernel import get_kernel
        running, finished = _car.on_hand_of_epoch(self.epoch)
        refs = set(running) | set(finished)
        if not refs:
            return
        try:
            _live = _wc.list_live(get_kernel(), oldest_first=True)
        except Exception:
            _live = []
        for r in _live:
            if r.bg_ref in refs:
                _wc.cancel_wait(r.wait_id, "user stopped the turn")

        for ref, display in running.items():
            self._stopped_refs[ref] = display
            if ref.startswith("cmd_"):
                try:
                    from core.os_layer import longcmd as _lc
                    _lc.stop(ref, "stopped by the user (Stop)")
                    logger.info(f"[Stop] 手头的命令 {ref} 随这一轮一起停下")
                except Exception as e:
                    logger.warning(f"[Stop] 停止手头的命令 {ref} 失败: {e}")
            else:
                logger.info(f"[Stop] 手头的「{display[:40]}」停不掉 → 自行结束，结果只记入历史（不唤醒）")

        for k, v in list(self.parked.items()):
            if not (isinstance(v, tuple) and v and v[0] == "wake"):
                continue
            try:
                rec = _wc.find_by_id(get_kernel(), v[1])
            except Exception:
                rec = None
            ref = getattr(rec, "bg_ref", None)
            if ref not in finished:
                continue
            self.parked.pop(k, None)
            inbox_discard(k, "用户终止了这段回应期，唤醒不再发生")
            self._note_finished_after_stop(finished[ref], v[3] if len(v) > 3 else "")
            if self.presenter is not None:
                try:
                    self.presenter.settle_cancelled_handback(ref)
                except Exception as e:
                    logger.debug(f"[Stop] 收原动作界面失败: {e}")
            logger.info(f"[Stop] {ref} 已完成、唤醒在排队 → 结果记入历史，不起唤醒轮")

    @staticmethod
    def _note_finished_after_stop(display: str, result: str, *, terminated: bool = False) -> None:
        """终止波及的那件事的结局：写一条系统事件（只在下一轮进上下文），不唤醒 Nano。"""
        what = ("It was terminated together with the turn" if terminated
                else "It has ended")
        try:
            from core.health import get_system_events
            get_system_events().add(
                f"The user stopped a turn while \"{display}\" was still in hand. {what}; "
                f"its result: {str(result)[:300]}. Because the user stopped it on purpose, "
                f"do not bring this up on your own and do not explain it to the user "
                f"unless they ask about it.")
        except Exception:
            pass

    async def notify_background_done(self, ref: str, result_hint: str | None = None) -> None:
        """后台载体（ref）完成 → 唤醒等它的挂起；等待已取消则只收原动作的界面。"""
        _stopped_display = self._stopped_refs.pop(ref, None)
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import waitcond as _wc
            _active = _wc.list_live(get_kernel(), oldest_first=True)
            _hit = [r for r in _active
                    if _wc.WakeSource.BACKGROUND in r.wake_on and r.bg_ref == ref]
        except Exception:
            _hit = []
        if not _hit:
            if _stopped_display is not None:
                # 终止时还在手头、停不掉（或命令已被终止）的那件事结束了：结果只记入历史。
                self._note_finished_after_stop(_stopped_display, result_hint or "",
                                               terminated=ref.startswith("cmd_"))
            _settled = 0
            if self.presenter is not None:
                try:
                    _settled = self.presenter.settle_cancelled_handback(ref)
                except Exception as e:
                    logger.debug(f"[Suspension] 收原动作界面失败: {e}")
            if _settled:
                logger.info(f"[Suspension] background 完成 ref={ref}；等待已取消，"
                            f"仅收 {_settled} 条原始动作 UI，不唤醒 Nano")
            else:
                logger.info(f"[Suspension] background 完成 ref={ref}，但无匹配 active 挂起（可能已被其它源唤醒）")
            return
        for r in _hit:
            await self.drive_wake(r.wait_id, trigger="background", note=(result_hint or ""))

    async def poll_due(self) -> None:
        """到点的定时挂起 → 驱动唤醒（后端心跳周期调用）。

        副作用驱动查询：DUE_FOR_REVIEW 也要捞到，否则一次唤醒尝试失败就永久失联。
        """
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import waitcond as _wc
            due = _wc.list_due_wakeups(get_kernel())
        except Exception as e:
            logger.warning(f"[Suspension] 轮询失败: {e}")
            return
        for rec in due:
            if rec.wait_id:
                await self.drive_wake(rec.wait_id, trigger="timer")

    def wake_now(self, suspension_id: str) -> str:
        """用户点「立即执行」。返回 "ended"（等待已结束）/ "parked"（忙，已排队）/ "started"。"""
        try:
            from core.runtime.kernel import get_kernel
            from core.runtime import waitcond as _wc
            rec = _wc.find_by_id(get_kernel(), suspension_id)
        except Exception:
            rec = None
        if rec is None or not rec.is_live:
            return "ended"
        if self.lock.locked():
            _wid = inbox_submit_wake(suspension_id, "manual")
            self.parked[_wid or f"memwake_{suspension_id}"] = ("wake", suspension_id, "manual")
            logger.info(f"[Inbox] 内核忙 → 手动继续进队列（{suspension_id}）")
            return "parked"
        asyncio.ensure_future(self.drive_wake(suspension_id, trigger="manual"))
        return "started"

    def cancel_wait(self, suspension_id: str) -> bool:
        """用户点「取消计划」：取消那条等待，并在对话里留一条系统记录。"""
        from core.runtime import waitcond as _wc
        ok = _wc.cancel_wait(suspension_id)
        if ok:
            try:
                self.agent.memory.add_system_note(
                    "assistant", "[System record: the user cancelled the pending wait above; "
                                 "Nano will not continue waiting.]")
            except Exception:
                pass
        return ok

    # ── 排空 ──────────────────────────────────────────────────────────────
    async def drain(self) -> None:
        """当前轮结束 → 接上排队的下一条。一次只处理一条，由新那一轮的收尾再次调用
        （新轮是另起的 task，这里不等它；每次都重新看「还有没有」，而不是维护计数）。"""
        if self.lock.locked():
            return
        if not self.parked:
            # 前台空了 → 视线转向后台：没安排回看的后台等待排第一次回看，
            # 之后由模型自己 `set_next_checkin`。
            try:
                from core.runtime import waitcond as _wc
                for _wid in _wc.parked_without_recheck():
                    _wc.reschedule_wait(_wid, _FIRST_RECHECK_SEC)
                    logger.info(f"[B1] 前台空了 → 视线转回后台（{_wid}，60s 后看一眼）")
            except Exception as e:
                logger.debug(f"[B1] 转视线回后台失败（完成唤醒仍在）: {e}")
            return
        item_id = next(iter(self.parked))
        args = self.parked.pop(item_id)
        logger.info(f"[Inbox] 上一轮结束 → 接上排队的那条（{item_id}），"
                    f"队列里还剩 {len(self.parked)} 条")
        inbox_claim(item_id)
        self.running_inbox_id = None if item_id.startswith(("mem_", "memwake_")) else item_id
        if isinstance(args, tuple) and args and args[0] == "wake":
            # 唤醒这一条的收尾交给它自己那条路（drive_wake 的 finally）：谁起的轮，谁收。
            self.running_inbox_id = None
            _sid = args[1]
            _trig = args[2] if len(args) > 2 else "manual"
            _note = args[3] if len(args) > 3 else ""
            asyncio.ensure_future(self.drive_wake(_sid, trigger=_trig, note=_note,
                                                  inbox_item_id=item_id))
            return
        if isinstance(args, tuple) and len(args) == 2 and args[0] in ("cont", "user"):
            _cont = args[0] == "cont"
            if _cont:
                # 插话之后的下一段：段号 +1，orchestrator 据此说明「这一段和上一段拼在同一个气泡里」。
                self._set_seam_part(self.seam_part + 1)
                logger.info(f"[Seam] 续接当前回应期（第 {self.seam_part} 段）—— 同一个 nano 气泡，不新建")
            else:
                self._set_seam_part(1)
            asyncio.ensure_future(self.run_user_turn(item_id, args[1], continuation=_cont))
            return
        logger.error(f"[Inbox] 认不出的排队项 {item_id}（仍在库里）: {type(args).__name__}")

    def consume_running(self) -> None:
        """用户消息那一轮结束：收掉它对应的 inbox 记录。"""
        inbox_consume(self.running_inbox_id)
        self.running_inbox_id = None

    def discard_parked(self) -> None:
        """重置对话：用户显式丢掉排队项（库里记 DISCARDED，由调用方负责）。"""
        self.parked.clear()

    def pending_user_items(self) -> int:
        """队列里还有几条用户消息（续接或排队）没跑。"""
        return sum(1 for v in self.parked.values()
                   if isinstance(v, tuple) and v and v[0] in ("cont", "user"))

    @staticmethod
    def _activity_event(ev: str) -> None:
        try:
            from core.proactive.activity import get_buffer
            get_buffer().on_nano_event(ev)
        except Exception:
            pass


_scheduler: Optional[TurnScheduler] = None


def get_scheduler() -> TurnScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = TurnScheduler()
    return _scheduler


def reset_for_tests() -> TurnScheduler:
    global _scheduler
    _scheduler = TurnScheduler()
    return _scheduler
