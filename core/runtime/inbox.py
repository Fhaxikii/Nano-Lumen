# -*- coding: utf-8 -*-
"""durable inbox —— 「用户的话永不丢」。

═══════════════════════════════════════════════════════════════════════════
它取代的那一行
═══════════════════════════════════════════════════════════════════════════
```python
if self.pipeline_lock.locked():
    ui.notify('内核正在处理中，请稍候...'); return   # ← 用户打的字就这么没了
```
**一轮任务跑起来后，用户连消息都发不出去**，而且没有终止按钮。

📌 **这与「闸 vs 挂起」是同一个形状**：
   **闸的出口是失败，队列的出口是稍后处理。**
   **一个只有失败出口的机制，最终一定把成本转嫁给用户去手动重试。**
   上一次是让 Nano 撞墙（拿不到租约 → 动作失败 → 结束 turn），
   这次是让用户重新打一遍字。

⭐ 而 `pipeline_lock` 的五个使用点里，**已经有一个是对的** ——
   `_drive_wake` 那处写着「内核忙：稍后由 poller 再尝试（记录仍 active）」。
   📌 **同一个「内核忙」被处理成了四种语义，而只有「稍后重试」适用于用户意图。**

═══════════════════════════════════════════════════════════════════════════
⚠️ 范围边界（写死，免得被当成没做完）
═══════════════════════════════════════════════════════════════════════════
本项**只保证「不丢 + 当前轮结束后自动接上」**，**不做「当前 turn 中途看到它」**。

后者是「无缝对话」，而它的载体是 Subagent —— Subagent 与「把任务抛到后台」
是它的两个前置能力，本模块不承担那一步。

⚠️ **一条直接影响本模块建模的硬约束**：新来的这句话与手头这件事**不要求相关**。

所以 inbox 里的东西**不许被假设成「对当前任务的修正」**。
它是一条**独立的用户意图**，可能与手头的事毫无关系。
📌 这也正是「回答 + **独立**新请求」被后置到 durable inbox 的原因 ——
   Interaction 那一层只支持「回答 + 同一 Skill 的 amendment」。

═══════════════════════════════════════════════════════════════════════════
⭐ 状态机：为什么需要 CLAIMED
═══════════════════════════════════════════════════════════════════════════
```
PENDING ──claim──▶ CLAIMED ──consume──▶ CONSUMED
   ▲                   │
   └── 启动收尾退回 ────┘        （任何状态都可 discard → DISCARDED）
```
⚠️ `CLAIMED` **不是为了并发安全**（asyncio 单线程，压根没有竞争）。
   它存在的唯一理由是：**让「哪一条正在被处理」有唯一答案** ——
   进程崩在「已取出、还没消费完」之间时，重启才知道该退回哪一条。
   📌 没有它，崩溃恢复只能在「全部重投」和「全部丢掉」之间二选一，两个都错。

⭐⭐ **`delivery_count` 是这里最要紧的一个字段，理由与 `ActionAttempt` 完全同源。**
   崩在「已经把这条话交给模型看了、但还没标 CONSUMED」之间时，
   重启退回 PENDING 会**再投一次**。那到底该不该投？
   · 不投 → 可能真的丢了用户的话（最该避免的）
   · 投但不说 → 模型可能把同一句话当成用户说了两遍，重复动手
   📌 所以答案和 `ActionAttempt` 一样：**投，但如实说「这条你可能已经看过」。**
      **不假装没发生过，也不悄悄丢掉。**

⚠️ **为什么退回 PENDING 是安全的（而 `ActionAttempt` 必须记成 INTERRUPTED）**：
   一条用户消息是**信息**，不是**副作用**。重新给模型看一遍最坏是浪费一次调用；
   而重放一次 `type_text` 会真的在记事本里多打一遍字。
   📌 **可重放性取决于「它改变的是世界还是上下文」。**
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from core.runtime.kernel import (
    Command,
    HandlerOutcome,
    InvariantViolation,
    KernelError,
    RuntimeKernel,
    TransitionEvent,
)


# ══════════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════════

class ItemStatus:
    PENDING   = "PENDING"      # 已收下，还没人处理
    CLAIMED   = "CLAIMED"      # 正在被这一轮处理
    CONSUMED  = "CONSUMED"     # 已交给模型并处理完（终态）
    DISCARDED = "DISCARDED"    # 用户撤回 / 重置对话（终态）
    _ALL = frozenset({PENDING, CLAIMED, CONSUMED, DISCARDED})
    _TERMINAL = frozenset({CONSUMED, DISCARDED})


class ItemKind:
    """⚠️ 刻意分两种，不合并成一个 `kind='message'`。

    它们**触发的处理路径不同**：用户消息要起一轮新 turn 并把原话喂进去；
    唤醒意图要走 `resume_suspension` 那条路（恢复一个已有的挂起）。
    📌 与 `CONSEQUENTIAL` vs `TAKEOVER_TRIGGERS` 同一个判据：
       **答的不是同一个问题，就不合并。**
    """
    USER_MESSAGE = "user_message"   # 用户打的字
    WAKE_INTENT  = "wake_intent"    # 「继续」/ 定时到点 / 后台完成
    _ALL = frozenset({USER_MESSAGE, WAKE_INTENT})


# 命令
SUBMIT  = "inbox.submit"
CLAIM   = "inbox.claim"
CONSUME = "inbox.consume"
RELEASE = "inbox.release"     # 处理失败 → 退回 PENDING（可重试）
DISCARD = "inbox.discard"


# ══════════════════════════════════════════════════════════════════════════
# 记录
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class InboxItem:
    item_id: str
    status: str
    kind: str
    body: str = ""
    detail: Optional[dict[str, Any]] = None
    delivery_count: int = 0
    owner_task_id: Optional[str] = None
    owner_turn_id: Optional[str] = None
    reason: str = ""
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: Optional[float] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ItemStatus._TERMINAL

    @property
    def maybe_seen_before(self) -> bool:
        """⭐ 这条可能已经被模型看过了（崩溃后重投）。**要如实告诉模型。**"""
        return self.delivery_count > 1


def _row(r: sqlite3.Row) -> InboxItem:
    return InboxItem(
        item_id=r["item_id"], status=r["status"], kind=r["kind"],
        body=r["body"] or "",
        detail=json.loads(r["detail"]) if r["detail"] else None,
        delivery_count=int(r["delivery_count"]),
        owner_task_id=r["owner_task_id"], owner_turn_id=r["owner_turn_id"],
        reason=r["reason"] or "", revision=int(r["revision"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
        closed_at=r["closed_at"],
    )


# ══════════════════════════════════════════════════════════════════════════
# 读路径
# ══════════════════════════════════════════════════════════════════════════

def get(kernel: RuntimeKernel, item_id: str) -> Optional[InboxItem]:
    with kernel.store.read() as conn:
        r = conn.execute("SELECT * FROM inbox_items WHERE item_id=?",
                         (item_id,)).fetchone()
    return _row(r) if r else None


def pending_count(kernel: RuntimeKernel) -> int:
    """⭐ UI 用它显示「排队中 N 条」。"""
    with kernel.store.read() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE status=?",
            (ItemStatus.PENDING,)).fetchone()[0])


def list_pending(kernel: RuntimeKernel, limit: int = 50) -> list[InboxItem]:
    """按**到达顺序**。⚠️ 顺序是语义的一部分 —— 用户先说 A 再说 B，
    倒过来处理会得出完全不同的结论（「装个库」→「算了先做别的」反过来读意思全变）。

    ⚠️⚠️ **用 rowid 排，不用 created_at。** 这是测试抓出来的一个真 bug：
       原写法是 `ORDER BY created_at, item_id`，而**同一时刻提交的两条
       `created_at` 相等** → 退化成按 `item_id`（随机 hex）排 → 顺序是乱的。
       📌 **到达顺序应该由「到达」本身决定，不该由时钟决定** ——
          时钟会相等、会被 NTP 拨回、在测试里还是假的（FakeClock 就是这么
          把它暴露出来的）；而 `rowid` 是 SQLite 记下的**真实插入次序**，
          白拿、单调、不会骗人。
    """
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM inbox_items WHERE status=? ORDER BY rowid "
            "LIMIT ?", (ItemStatus.PENDING, limit)).fetchall()
    return [_row(r) for r in rows]


def current_claimed(kernel: RuntimeKernel) -> Optional[InboxItem]:
    with kernel.store.read() as conn:
        r = conn.execute("SELECT * FROM inbox_items WHERE status=? LIMIT 1",
                         (ItemStatus.CLAIMED,)).fetchone()
    return _row(r) if r else None


def has_work(kernel: RuntimeKernel) -> bool:
    """还有没有该处理的东西。

    ⚠️ **fail-safe 方向是「有」** —— 读不出来时宁可多驱动一次（最坏是空跑一轮），
    也不要漏掉用户的话。
    📌 与 `nano_may_touch_os()` 相反：那边错成 True 会在用户打字时抢鼠标；
       这边错成 True 只是白检查一次。**方向由代价决定，不由习惯决定。**
    """
    try:
        return pending_count(kernel) > 0
    except Exception as e:
        logger.warning(f"[Inbox] 读 pending 失败，按「有」处理: {e}")
        return True


# ══════════════════════════════════════════════════════════════════════════
# Kernel 接线（handler 签名是 `(conn, cmd, ctx)`，用 `@kernel.register` 装饰）
# ══════════════════════════════════════════════════════════════════════════

def install(kernel: RuntimeKernel) -> None:
    kernel.register_revision_source("inbox", "inbox_items", "item_id")

    @kernel.register(SUBMIT)
    def _submit(conn, cmd, ctx) -> HandlerOutcome:
        """收下一条。**这是唯一的入口，所以校验放这里。**

        📌 与 `ActionAttempt` 同一条：**把约束放在唯一的写入口，比事后校验更强。**
        """
        p = cmd.payload or {}
        kind = p.get("kind") or ItemKind.USER_MESSAGE
        if kind not in ItemKind._ALL:
            # ⚠️ 用 KernelError 而不是 InvariantViolation —— 后者的语义是
            #    「任何合法命令序列都不该让它成立」，是**代码 bug 的信号**。
            #    命令参数非法是另一回事。
            #    📌 拿 InvariantViolation 报参数错误 = 把调用方的错误
            #       误标成数据损坏，会让人去查一个不存在的问题。
            raise KernelError(f"未知的 inbox kind: {kind!r}")
        body = p.get("body") or ""
        if kind == ItemKind.USER_MESSAGE and not body.strip():
            # ⚠️ 空消息不许入库：它会让 UI 出现一条空气泡、让模型收到一句空话。
            raise KernelError("user_message 的 body 不许为空")
        iid = p.get("item_id") or f"inbox_{uuid.uuid4().hex[:10]}"
        now = ctx.now
        conn.execute(
            "INSERT INTO inbox_items (item_id, status, kind, body, detail, "
            "delivery_count, owner_task_id, owner_turn_id, reason, revision, "
            "created_at, updated_at) VALUES (?,?,?,?,?,0,?,NULL,'',1,?,?)",
            (iid, ItemStatus.PENDING, kind, body,
             json.dumps(p.get("detail"), ensure_ascii=False)
             if p.get("detail") is not None else None,
             p.get("owner_task_id"), now, now))
        return HandlerOutcome(
            data={"item_id": iid},
            events=[TransitionEvent(kind="inbox.submitted", subject_kind="inbox",
                                    subject_id=iid, detail={"kind": kind})])

    @kernel.register(CLAIM)
    def _claim(conn, cmd, ctx) -> HandlerOutcome:
        """取出最早那条 PENDING 并认领。没有就返回 `item_id=None`。

        ⚠️⚠️ **认领即 `delivery_count += 1`** —— 这个数记的是「投递过几次」，
           不是「成功处理过几次」。
           📌 **投递次数必须在「交出去之前」加，不能在「处理成功之后」加** ——
              后者永远数不到那次崩溃，而那正是唯一需要它的场合。
        """
        p = cmd.payload or {}
        # ⚠️ 已经有一条在处理中 → 现在没什么可认领的，**明确返回 None**。
        #    唯一索引会兜住这件事，但它抛的是裸 `sqlite3.IntegrityError`
        #    —— 那是「数据库救了我们一命」，不是「这个函数回答了问题」。
        #    📌 **索引是兜底（纵深防御），显式判断才是主路径** ——
        #       同 `oslease` 那条「互斥有数据库级唯一索引兜底，不只靠不变量」。
        if conn.execute("SELECT 1 FROM inbox_items WHERE status=? LIMIT 1",
                        (ItemStatus.CLAIMED,)).fetchone():
            return HandlerOutcome(data={"item_id": None, "busy": True})
        want = p.get("item_id")
        if want:
            # ⭐⭐ **指定认领** —— 这个分支不是可选的便利，它修一个真 bug：
            #    调用方（UI 侧）从自己的内存队列里取出**某一条**去跑，
            #    而「认领最早那条」可能认领的是**另一条** ——
            #    比如上个进程遗留、被启动收尾退回队列的那条还排在前面。
            #    于是**跑的是 A、标记消费的是 B，两条都被记错了**。
            #    📌 **「取哪一条去做」和「把哪一条标成在做」必须是同一条** ——
            #       否则账和事实分家，而这种错在库里看起来完全正常。
            r = conn.execute(
                "SELECT item_id FROM inbox_items WHERE item_id=? AND status=?",
                (want, ItemStatus.PENDING)).fetchone()
        else:
            # ⚠️ `ORDER BY rowid` = 真实到达次序。**不要用 `created_at`** ——
            #    同一时刻提交的两条时间戳相等，会退化成按随机 id 排。
            #    见 `list_pending` 的说明。
            r = conn.execute(
                "SELECT item_id FROM inbox_items WHERE status=? ORDER BY rowid LIMIT 1",
                (ItemStatus.PENDING,)).fetchone()
        if r is None:
            return HandlerOutcome(data={"item_id": None})
        iid = r["item_id"]
        conn.execute(
            "UPDATE inbox_items SET status=?, delivery_count=delivery_count+1, "
            "owner_turn_id=?, revision=revision+1, updated_at=? WHERE item_id=?",
            (ItemStatus.CLAIMED, p.get("owner_turn_id"), ctx.now, iid))
        return HandlerOutcome(
            data={"item_id": iid},
            events=[TransitionEvent(kind="inbox.claimed", subject_kind="inbox",
                                    subject_id=iid)])

    def _close(conn, cmd, ctx, status: str, ev: str) -> HandlerOutcome:
        p = cmd.payload or {}
        iid = p.get("item_id") or cmd.subject_id
        if not iid:
            raise KernelError(f"{cmd.kind} 缺 item_id")
        r = conn.execute("SELECT status FROM inbox_items WHERE item_id=?",
                         (iid,)).fetchone()
        if r is None:
            raise KernelError(f"inbox item 不存在: {iid}")
        if r["status"] in ItemStatus._TERMINAL:
            # 幂等：已终态就不动。⚠️ **不抛异常** —— 同 `release_activity` 的判据：
            #    重复收尾是正常运行的一部分，为它抛异常会让 finally 很难写。
            return HandlerOutcome(data={"item_id": iid, "already": r["status"]})
        conn.execute(
            "UPDATE inbox_items SET status=?, reason=?, revision=revision+1, "
            "updated_at=?, closed_at=? WHERE item_id=?",
            (status, p.get("reason") or "", ctx.now, ctx.now, iid))
        return HandlerOutcome(
            data={"item_id": iid},
            events=[TransitionEvent(kind=ev, subject_kind="inbox", subject_id=iid)])

    @kernel.register(CONSUME)
    def _consume(conn, cmd, ctx) -> HandlerOutcome:
        return _close(conn, cmd, ctx, ItemStatus.CONSUMED, "inbox.consumed")

    @kernel.register(DISCARD)
    def _discard(conn, cmd, ctx) -> HandlerOutcome:
        return _close(conn, cmd, ctx, ItemStatus.DISCARDED, "inbox.discarded")

    @kernel.register(RELEASE)
    def _release(conn, cmd, ctx) -> HandlerOutcome:
        """处理失败 / 崩溃退回 → 回到 PENDING，等下一次再试。

        ⚠️ **这是本模块存在意义的直接体现**：处理一条用户消息失败时，
           出口是「回队列」而不是「丢掉」。
           📌 闸的出口是失败，队列的出口是稍后处理。
        ⚠️ 必须同时清 `owner_turn_id` 和 `closed_at` ——
           留着 `closed_at` 会让这条记录同时声称「我还在排队」和「我已经结束了」。
           📌 **一个字段不许表达两个现实**（有条不变量兜住这个）。
        """
        p = cmd.payload or {}
        iid = p.get("item_id") or cmd.subject_id
        r = conn.execute("SELECT status FROM inbox_items WHERE item_id=?",
                         (iid,)).fetchone()
        if r is None:
            raise KernelError(f"inbox item 不存在: {iid}")
        if r["status"] in ItemStatus._TERMINAL:
            return HandlerOutcome(data={"item_id": iid, "already": r["status"]})
        conn.execute(
            "UPDATE inbox_items SET status=?, owner_turn_id=NULL, closed_at=NULL, "
            "reason=?, revision=revision+1, updated_at=? WHERE item_id=?",
            (ItemStatus.PENDING, p.get("reason") or "", ctx.now, iid))
        return HandlerOutcome(
            data={"item_id": iid},
            events=[TransitionEvent(kind="inbox.released", subject_kind="inbox",
                                    subject_id=iid)])

    # ── 不变量 ────────────────────────────────────────────────────────────

    def _inv_enums(conn: sqlite3.Connection) -> None:
        for r in conn.execute("SELECT item_id, status, kind FROM inbox_items"):
            if r["status"] not in ItemStatus._ALL:
                raise InvariantViolation("inbox_enums",
                    f"{r['item_id']} 状态非法: {r['status']}")
            if r["kind"] not in ItemKind._ALL:
                raise InvariantViolation("inbox_enums",
                    f"{r['item_id']} kind 非法: {r['kind']}")

    def _inv_terminal_closed(conn: sqlite3.Connection) -> None:
        """终态必须有 `closed_at`；**非终态必须没有**。

        ⚠️ 第二半才是关键 —— `release` 把一条从 CLAIMED 退回 PENDING 时
           如果忘了清 `closed_at`，那条记录就同时声称「我还在排队」和「我已结束」。
           📌 **一个字段不许表达两个现实。**
        """
        for r in conn.execute("SELECT item_id, status, closed_at FROM inbox_items"):
            term = r["status"] in ItemStatus._TERMINAL
            if term and r["closed_at"] is None:
                raise InvariantViolation("inbox_terminal_closed",
                    f"{r['item_id']} 终态但没有 closed_at")
            if not term and r["closed_at"] is not None:
                raise InvariantViolation("inbox_terminal_closed",
                    f"{r['item_id']} 非终态（{r['status']}）却有 closed_at")

    def _inv_consumed_was_delivered(conn: sqlite3.Connection) -> None:
        """`CONSUMED` 必须至少被投递过一次。

        一条从没投递过就被标成「已处理完」的记录，意味着我们
        **声称把没给模型看过的话处理掉了** —— 那是最坏的一种「丢消息」，
        因为它在账上是干净的。
        ⚠️ `DISCARDED` 不受这条约束：用户可以撤回一条从没被处理的消息。
        """
        r = conn.execute(
            "SELECT item_id FROM inbox_items WHERE status=? AND delivery_count<1 "
            "LIMIT 1", (ItemStatus.CONSUMED,)).fetchone()
        if r:
            raise InvariantViolation("inbox_consumed_was_delivered",
                f"{r['item_id']} 标了 CONSUMED 但从未投递过")

    kernel.register_invariant("inbox_enums", _inv_enums)
    kernel.register_invariant("inbox_terminal_closed", _inv_terminal_closed)
    kernel.register_invariant("inbox_consumed_was_delivered",
                              _inv_consumed_was_delivered)

    # ── 启动收尾 ──────────────────────────────────────────────────────────

    def _startup(k: RuntimeKernel, report) -> None:
        """把上个进程遗留的 `CLAIMED` 退回 `PENDING` —— **一条都不丢。**

        ⚠️⚠️ 签名必须是 `(k, report)`，且 `report` 是 `ReconcileReport`
           （用 `report.extra[...]`，**不是** dict）。这里栽过一次：
           写成 `(k)` 时异常被 `reconcile_on_startup` 的 `except` 吞掉，
           **静默不跑** —— 与「注册上去就以为会跑」的 `reconcile_tick` 同形。
        ⭐ 所以这里**无条件**写 `report.extra`（哪怕 0 条）——
           只在 n>0 时写的话，「一条都没有」和「压根没跑」在 report 里长得一样，
           而那正是当初那个 bug 能藏住的原因。
        """
        n = 0
        with k.store.read() as conn:
            rows = conn.execute(
                "SELECT item_id, delivery_count FROM inbox_items WHERE status=?",
                (ItemStatus.CLAIMED,)).fetchall()
        for r in rows:
            try:
                k.submit(Command(
                    kind=RELEASE, subject_id=r["item_id"],
                    payload={"item_id": r["item_id"],
                             "reason": "上个进程崩在处理中途，退回队列重投"}))
                n += 1
                logger.warning(
                    f"[Inbox] {r['item_id']} 退回队列"
                    f"（已投递 {r['delivery_count']} 次）—— 重投时会告知模型"
                    f"「这条可能已经看过」")
            except Exception as e:      # pragma: no cover
                logger.error(f"[Inbox] 退回 {r['item_id']} 失败: {e}")
        report.extra["inbox_released"] = n
        if n:
            logger.info(f"[Inbox] 启动退回 {n} 条被中断的消息（一条都没丢）")

    from core.runtime import reconciler as _rec
    _rec.register_startup_step("inbox_startup", _startup)


# ══════════════════════════════════════════════════════════════════════════
# 薄封装（给 app.py / orchestrator 用）
# ══════════════════════════════════════════════════════════════════════════

def _k() -> RuntimeKernel:
    from core.runtime.kernel import get_kernel
    return get_kernel()


def _tk_label():
    """归属标签。**这里刻意用 `owner_label` 而不是 `ensure_...`** ——
    本模块的归属物是**一轮内**的东西，不许让一件事因为它诞生（会退化成 per-exchange）。
    """
    try:
        from core.runtime import task as _tk
        return _tk.owner_label()
    except Exception:
        return None


def submit_user_message(body: str, detail: Optional[dict] = None) -> Optional[str]:
    """收下用户一句话。**返回 item_id；失败返回 None。**

    🔴 **调用方绝不能把 None 当成「可以丢掉这句话」** ——
       落库失败时正确做法是照旧直接起一轮（退化成旧行为），不是静默丢弃。
    """
    try:
        iid = _k().submit(Command(kind=SUBMIT, payload={
            "kind": ItemKind.USER_MESSAGE, "body": body, "detail": detail,
            # 只贴标签，**不创建** —— 见 `task.owner_label()` 的说明
            "owner_task_id": _tk_label(),
        })).data["item_id"]
        # ⭐ 通知正在等确认的那个 turn：用户改口说话了。
        # ⚠️ 放在**落库成功之后** —— 先保证不丢，再叫醒别人。
        #    📌 顺序反了的话，可能叫醒了一个去处理并不存在的消息的等待点。
        _note_arrival()
        return iid
    except Exception as e:
        logger.error(f"[Inbox] 收下用户消息失败: {e}")
        return None


def submit_wake_intent(suspension_id: str, trigger: str) -> Optional[str]:
    try:
        return _k().submit(Command(kind=SUBMIT, payload={
            "kind": ItemKind.WAKE_INTENT, "body": "",
            "detail": {"suspension_id": suspension_id, "trigger": trigger},
            # 只贴标签，**不创建** —— 见 `task.owner_label()` 的说明
            "owner_task_id": _tk_label(),
        })).data["item_id"]
    except Exception as e:
        logger.error(f"[Inbox] 收下唤醒意图失败: {e}")
        return None


def claim_next(owner_turn_id: Optional[str] = None) -> Optional[InboxItem]:
    """认领最早那条。没有就 None。"""
    try:
        iid = _k().submit(Command(kind=CLAIM, payload={
            "owner_turn_id": owner_turn_id})).data.get("item_id")
        return get(_k(), iid) if iid else None
    except Exception as e:
        logger.error(f"[Inbox] 认领失败: {e}")
        return None


def consume(item_id: str, reason: str = "") -> None:
    try:
        _k().submit(Command(kind=CONSUME, subject_id=item_id,
                            payload={"item_id": item_id, "reason": reason}))
    except Exception as e:
        logger.error(f"[Inbox] 标记已消费失败 {item_id}: {e}")


def release(item_id: str, reason: str = "") -> None:
    try:
        _k().submit(Command(kind=RELEASE, subject_id=item_id,
                            payload={"item_id": item_id, "reason": reason}))
    except Exception as e:
        logger.error(f"[Inbox] 退回队列失败 {item_id}: {e}")


def list_unfinished(limit: int = 50) -> list[InboxItem]:
    """重启后**还没被处理完**的全集：PENDING ∪ CLAIMED。

    ⭐⭐ 必须**两个状态都要**：
       · `PENDING`  —— 排着队还没轮到
       · `CLAIMED`  —— 上一个进程认领了、**但没来得及 consume 就死了**
    🔴 只取 PENDING 会漏掉「正在处理那一条」，而那恰恰是用户最在意的一条
       （用户刚发完、看着 Nano 开始动，然后软件关了）。
       📌 **「还没做完」不等于「还没开始」** —— 一个按状态取的清单，
          漏掉中间态就等于漏掉了最要紧的那个。
    ⚠️ 只读，不改状态。丢弃由调用方在**呈现之后**显式做（见 `discard`）。
    """
    out = list(list_pending(_k(), limit=limit))
    try:
        cur = current_claimed(_k())
        if cur is not None and all(i.item_id != cur.item_id for i in out):
            out.insert(0, cur)      # 它比排队的更早，放最前
    except Exception as e:
        logger.warning(f"[Inbox] 读 CLAIMED 失败（只呈现 PENDING）: {e}")
    return out


def discard(item_id: str, reason: str = "") -> None:
    """丢弃一条。

    ⚠️ 与 `discard_all_pending` 一样，这是**允许丢用户消息**的路径，
       所以调用点必须先把它**呈现给用户**（定的是「呈现，不执行」）。
       📌 呈现完再丢，和悄悄丢，是两件事。
    🔴 而且**必须丢**：留着 PENDING 的话，下一次 `_drain_inbox` 会把它捡起来
       真的执行 —— 那正是被否掉的那一支。
    """
    try:
        _k().submit(Command(kind=DISCARD, subject_id=item_id,
                            payload={"item_id": item_id,
                                     "reason": reason or "重启后已呈现给用户"}))
    except Exception as e:
        logger.error(f"[Inbox] 丢弃 {item_id} 失败: {e}")


def discard_all_pending(reason: str = "") -> int:
    """重置对话时清空队列。返回清掉几条。

    ⚠️ 这是**唯一**允许丢弃用户消息的路径，因为它是用户**显式**要求的
    （点了「重置对话」）。📌 **用户自己决定丢，和系统悄悄丢，是两件事。**
    """
    n = 0
    for it in list_pending(_k(), limit=10000):
        try:
            _k().submit(Command(kind=DISCARD, subject_id=it.item_id,
                                payload={"item_id": it.item_id,
                                         "reason": reason or "用户重置对话"}))
            n += 1
        except Exception as e:
            logger.error(f"[Inbox] 丢弃 {it.item_id} 失败: {e}")
    if n:
        logger.info(f"[Inbox] 用户重置对话，清掉 {n} 条排队消息")
    return n


# ══════════════════════════════════════════════════════════════════════════
# 让用户的一句话能解除一个正在阻塞 turn 的 INLINE 确认
# ══════════════════════════════════════════════════════════════════════════
# 🔴🔴 **durable inbox 只解掉了一半，这一半不补上就是个死锁形状的缺口。**
#
# 只有 durable inbox 时的现状：
#   1. Nano 弹出一个 OS 风险确认，turn 卡在 `await wait_for(ev.wait(), 300)`
#   2. 用户不想点那个按钮，直接打字「算了别点了」
#   3. durable inbox 把这句话**收下了、没丢** ✅
#   4. 但队列的消费时机是「**当前轮结束之后**」——
#      而当前轮正卡在等这个确认，**它不会结束**
#   5. → 用户的话要等满 **300 秒**超时才被处理
#
# 📌 **一句「没丢」的保证，如果它的兑现时机取决于一个正在等它的东西，
#    那它实际上就是丢了。**（形状与那个「闸」一样：出口存在，但走不到。）
# ⭐ 而用户面对一个不想要的确认弹窗，**最自然的反应就是打字**，不是去找取消按钮。
#
# ═══ 职责怎么切（这一条最要紧）═══
# 📌 **不许让代码去理解「算了别点了」，也不许让模型来决定「那个已经在等的动作要不要执行」。**
#    · 前者是自然语言理解 —— 代码做不了，硬做就是关键词表（`_pending_skill_clarification`
#      那张中文关键词表已经因此被废掉过一次）
#    · 后者是**安全属性** —— 不能等模型想明白；而且模型此刻压根没被调用
#
# 所以切成两半：
#   **代码判据（确定性、不看内容）**：等待期间来了新的用户消息 →
#       这个 INLINE 确认以「被用户的新消息取消」结束，**那个动作不执行**。
#   **模型判断（下一轮）**：那句话到底什么意思 →
#       下一轮模型读到「你刚才那个确认被取消了 + 用户的原话」，自己决定怎么办。
#
# ⭐ 这正好兑现了
#    **「不依赖模型判断的确定性 Cancel」** —— 用户打字**就是**那个确定性取消。
# ⭐ fail-safe 方向也很清楚：**不执行**。
#    📌 用户打字打断一个确认弹窗，绝大多数情况就是「别做」；
#       而且**「没做」可以重做，「做了」可能不可逆**。
_submit_seq: int = 0
_arrival_ev: "Optional[Any]" = None      # asyncio.Event，懒建
# 🔴 这个 Event 是在**哪个事件循环**上建的。见 `_get_arrival_ev` 的说明 ——
#    不记它的话，循环一换，等待它的那条路会永远炸，而后果是**判错不是报错**。
_arrival_ev_loop: "Optional[Any]" = None


def submit_seq() -> int:
    """收到过多少条用户消息（单调递增）。**基线用它，不用事件的状态。**"""
    return _submit_seq


def _note_arrival() -> None:
    global _submit_seq
    _submit_seq += 1
    ev = _arrival_ev
    if ev is not None:
        try:
            ev.set()
        except Exception:
            pass


def _get_arrival_ev():
    """「有新用户消息到了」的信号事件。**按当前事件循环缓存。**

    🔴🔴 **原来是一个永久缓存的模块级 Event，2026-08-26 修。**
       `asyncio.Event` 在 3.10 里会在**第一次 await 时绑定当时的事件循环**，
       之后换一个循环再 `await ev.wait()` 就抛
       `RuntimeError: ... is bound to a different event loop`。
    ⚠️ 而那个异常的后果**不是报错，是判错**：等待它的那个 task 会
       **带着异常「完成」**，于是 `asyncio.wait` 把它算进 `done`，
       调用方读到「用户插话了」——
       ⇒ **事件循环换过之后，每一次确认弹窗都会被判成「用户改口取消」，
          而用户实际点的「确认执行」被丢掉。** 而且全程静默（异常被 asyncio
          吞成 "Task exception was never retrieved"）。
    📌 **一个「有没有新消息」的判断，在循环换掉之后变成了永远为真。**
    ⭐ 修法两层：这里换成按循环缓存（去掉**成因**），
       `wait_confirm_or_user_message` 那边再查一次 `exception()`（兜住**症状**）——
       📌 因为「循环换了」只是**一种**让它炸的原因，而下一种我们还不知道。
    ⚠️ 换新 Event 时**不继承旧的 set 状态**，这是对的：基线是
       `_submit_seq`，事件只负责唤醒。（同函数上方那条「用事件表达『有新东西』
       时必须定基线」。）
    """
    global _arrival_ev, _arrival_ev_loop
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _arrival_ev is None or _arrival_ev_loop is not loop:
        _arrival_ev = asyncio.Event()
        _arrival_ev_loop = loop
    return _arrival_ev


class ConfirmOutcome:
    """`wait_confirm_or_user_message` 的结果。**三种，不许压成布尔。**

    📌 「用户点了取消」和「用户改口说别做了」和「等了五分钟没人管」
       在结果上都是"不执行"，但**要告诉模型的话完全不同** ——
       同 `ActionAttempt` 那条：一个字段不许表达两个现实。
    """
    CONFIRMED = "confirmed"          # 用户点了按钮（同意或拒绝，看 on_confirm 侧的记录）
    USER_MESSAGE = "user_message"    # 用户没点，改口说话了 → 确定性取消
    TIMEOUT = "timeout"              # 干等到超时


async def wait_confirm_or_user_message(confirm_ev, timeout: float, *,
                                       cancel_on_user_message: bool = True) -> str:
    """等确认按钮**或**一条新的用户消息，谁先来算谁。返回 `ConfirmOutcome` 之一。

    ⚠️⚠️ **`cancel_on_user_message=False` 是给【后台执行体】的**（Subagent 那一路）。
       🔴 「用户说了新话」这个信号，对 main agent 和对 Subagent 的含义**完全不同**：
            main agent —— 弹窗是**这一轮**的产物，用户下一句话就是对它的回应
                    → 「改口 = 取消」成立，而且不该逼用户去点一下拒绝
                      （这正是本函数存在的理由：为了少堵用户）
            Subagent —— 弹窗是一个**后台执行体**的产物，
                    用户下一句话**跟它没有任何关系**
       于是共用同一条判据时会这样坏（而且**静默**）：
            Subagent在批量改文件 → 第 1 处弹窗挂着
            用户随口问「刚才那个 pip 装完没」→ 那次写入被当成「用户取消」丢掉
            Subagent要么以为自己没权限而放弃整件事，要么重试 → 又被取消（活锁）
            而用户的体感是：**自己什么都没做，它就失败了。**
       📌 **一个「取消」的信号，必须来自它要取消的那件事的同一条注意力。**
       ⭐ 定死的措辞：**Subagent 只接受这个弹窗自己的回应**，
          不论是确认还是拒绝。
       ⚠️ 超时那道兜底**保留** —— 📌 沉默不等于成功（等待类任务的通用纪律）。
       ⚠️ 这个不对称是 Subagent detach **新引入**的：在那之前 Subagent 是同步的，
          用户说话确实还在同一条注意力上。
          📌 一条判据的前提变了，它就得重新问一遍，而不是继承下来。

    ⚠️⚠️ **基线用计数器，不用事件的 set/clear 状态。**
       只看事件的话，队列里**早就存在**的消息会让每个刚弹出的确认**立刻**被取消。
       📌 **用「事件」表达「有新东西」时必须定基线，否则它表达的是「曾经有过东西」。**
       （同一个坑：`_os_task_busy` 那个裸 bool 表达的也是"曾经"而不是"此刻"。）

    ⚠️ 任何异常都退化成「就按原来那样只等确认」——
       📌 这个机制是**为了少堵用户**，不该反过来变成一条新的失败路径。
    """
    import asyncio
    if not cancel_on_user_message:
        # ⭐ 只认这个弹窗自己的回应（+ 超时）。**不看 inbox。**
        try:
            await asyncio.wait_for(confirm_ev.wait(), timeout=timeout)
            return ConfirmOutcome.CONFIRMED
        except asyncio.TimeoutError:
            return ConfirmOutcome.TIMEOUT
        except Exception as e:
            logger.warning(f"[Inbox] 后台确认等待失败，按超时处理: {e}")
            return ConfirmOutcome.TIMEOUT
    try:
        base = _submit_seq
        ev = _get_arrival_ev()

        async def _new_msg() -> None:
            while _submit_seq <= base:
                ev.clear()
                await ev.wait()

        t_confirm = asyncio.ensure_future(confirm_ev.wait())
        t_msg = asyncio.ensure_future(_new_msg())
        try:
            done, _ = await asyncio.wait(
                {t_confirm, t_msg}, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t_confirm, t_msg):
                if not t.done():
                    t.cancel()
        if t_confirm in done:
            return ConfirmOutcome.CONFIRMED
        # 🔴🔴 **「完成」不等于「有结果」** —— 一个带着异常结束的 task 同样在
        #    `done` 里。不查这一下的后果是：等待新消息那条路只要炸了
        #    （比如 Event 绑在别的事件循环上），就会被读成「用户插话了」，
        #    而用户实际点的「确认执行」**被丢掉**。
        # 📌 **fail-safe 方向**：判不出来时**不取消** —— 错误地取消会丢掉
        #    用户已经做出的决定；多等一会儿只是慢一点。
        if t_msg in done and t_msg.exception() is not None:
            logger.warning(
                f"[Inbox] 等待新消息那条路失败了（{t_msg.exception()!r}）"
                f"—— **不当成「用户插话」**，退化成只等确认。"
            )
            try:
                await asyncio.wait_for(confirm_ev.wait(), timeout=timeout)
                return ConfirmOutcome.CONFIRMED
            except asyncio.TimeoutError:
                return ConfirmOutcome.TIMEOUT
        if t_msg in done:
            logger.info("[Inbox] 用户在确认弹窗挂着时说话了 → "
                        "这个确认按「被新消息取消」处理（不执行那个动作）")
            return ConfirmOutcome.USER_MESSAGE
        return ConfirmOutcome.TIMEOUT
    except Exception as e:
        logger.warning(f"[Inbox] 双路等待失败，退化成只等确认: {e}")
        try:
            await asyncio.wait_for(confirm_ev.wait(), timeout=timeout)
            return ConfirmOutcome.CONFIRMED
        except Exception:
            return ConfirmOutcome.TIMEOUT


def cancelled_by_user_message_note() -> str:
    """给模型看的一句话。⚠️ 必须说清**三件事**：没执行、为什么、以及别自己猜原因。"""
    return ("[Cancelled by the user speaking up] You were waiting for the user to "
            "confirm an action. Instead of answering the dialog, they sent a new "
            "message — so the action was NOT executed. Read their new message and "
            "decide what to do; do not assume it means they approved or rejected "
            "anything in particular.")


def describe_for_model(item: InboxItem) -> str:
    """把一条 inbox 项变成给模型看的前缀。**没有可说的就返回空串，不编。**

    ⭐ 只有两种情况需要说话：**排过队**（用户不是刚说的）和**可能重投**。
    📌 其余情况一个字都不加 —— 一句用户的话最好的呈现方式就是它本身。
    """
    bits = []
    if item.maybe_seen_before:
        bits.append(
            f"[This message may have been shown to you before] It was delivered "
            f"{item.delivery_count} times because the process restarted while it "
            f"was being handled. If you already acted on it, do NOT act again — "
            f"just confirm with the user.")
    return "\n".join(bits)
