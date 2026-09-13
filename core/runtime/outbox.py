# core/runtime/outbox.py
"""
Action Outbox —— transactional outbox + work lease。

═══ 为什么必须有这个东西 ═══

设计时一度打算连这个一起砍掉（理由是"不做持久 event log"）。方向对，但**砍多了
一刀**：不做 event sourcing 没问题，漏掉的是
**"状态 → 外部动作"之间的崩溃窗口**。

Reconciler 发现某个 Task READY，要去启动一个模型 turn。两种顺序都错：

    先启动 turn，再写 RUNNING   → 崩在中间 → RUNNING 没写进去 → 重启后又启动一次 → 【重复执行】
    先写 RUNNING，再启动 turn   → 崩在中间 → turn 从未启动 → 重启后以为有人在跑 → 【永久卡住】

⭐ **这两个 bug 在 Nano 里都真实发生过，不是假想**：

  1. 「先写后做」：`resume_suspension` 里 `_suspension_store.resolve()` 在**任何模型调用
     之前**执行（有意为之，防重复唤醒）。代价是 provider 抛错时**记录已被消费而 turn 从未
     发生 → 这条挂起永久丢失，用户永远不知道 Nano 找过自己**。实测撞到过一次。
  2. 「边沿信号」：那条"不死的挂起"—— `notify_background_done` 是一次性回调，
     `_drive_wake` 早退之后信号就没了，而轮询只查 `due_timers()`（要求 `timer_at IS NOT NULL`），
     捞不到 background-only 的记录。三条唤醒路径全堵死。

  原定的修法是"给挂起加 `bg_done_at`/`bg_result` 字段 + 轮询多查一类"——
  **那本质就是一个手搓的、只服务一个场景的 mini-outbox**。这里通用地做一次，
  上层各处直接落在它上面，不再各自单独实现一份。

═══ 可以立刻用的原则 ═══

    任何可能被闸挡住的唤醒信号，都必须是【状态型】的。
    边沿型信号放在闸后面必然丢失。

outbox 就是"把边沿转成状态"的物理实现：信号先落盘成一行 PENDING，
之后**每一次** Reconciler 轮询都能重新看到它。所以现在的两个早退点
（内核忙 / 预算超限）、以及以后新增的任何早退点，**全部自动获得重试能力**。

═══ fencing token 为什么不能只靠 expires_at（不变量 15）═══

    Worker A 拿到 lease → 卡住（GC 暂停 / 系统休眠 / 断点）
    lease 过期 → Worker B 重新认领 → 开始执行
    Worker A 醒了 → 提交它的旧结果

只有 `lease_until` 挡不住第三步——A 提交时不知道自己已经被取代了。
所以每次认领 `fence += 1`，提交时必须带上认领时拿到的 fence，不匹配一律拒绝。

单进程单用户下这不是空谈：MCP 自动后台化那个 `_mcp_task` 就是"lease 超时后旧 worker
仍能返回结果"的现成场景（`_MCP_BG_THRESHOLD = 8.0` 之后调用还在跑），
Subagent会让它变成常态。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from core.runtime.kernel import (
    Command,
    HandlerContext,
    HandlerOutcome,
    KernelError,
    RuntimeKernel,
    TransitionEvent,
)


class ActionStatus:
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    FAILED = "FAILED"
    _ALL = frozenset({PENDING, CLAIMED, DONE, FAILED})
    _TERMINAL = frozenset({DONE, FAILED})


# 默认 lease 时长。取 120s 的理由：一次模型 turn 的量级是几秒到几十秒
# （见 cmd_log 里 [TOKEN-USAGE] 的间隔），120s 足够覆盖慢的一轮而不至于
# 让真崩掉的 worker 占着 slot 太久。各 kind 可以在 enqueue 时覆盖。
DEFAULT_LEASE_SECONDS = 120.0


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    idempotency_key: str
    kind: str
    status: str
    target_task_id: Optional[str] = None
    claimed_by: Optional[str] = None
    lease_until: Optional[float] = None
    fence: int = 0
    attempt_no: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    last_error: Optional[str] = None
    created_at: float = 0.0
    completed_at: Optional[float] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ActionRecord":
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        return cls(
            action_id=row["action_id"], idempotency_key=row["idempotency_key"],
            kind=row["kind"], status=row["status"],
            target_task_id=row["target_task_id"], claimed_by=row["claimed_by"],
            lease_until=row["lease_until"], fence=int(row["fence"]),
            attempt_no=int(row["attempt_no"]), payload=payload,
            last_error=row["last_error"], created_at=float(row["created_at"]),
            completed_at=row["completed_at"],
        )


class FenceRejected(KernelError):
    """提交时 fence 不匹配 —— 你的 lease 已经被别人接管了，结果作废。

    ⚠️ 这是**正常运行的一部分**，不是 bug：worker 卡住超过 lease 就该被取代。
    调用方看到它应该安静放弃，不要重试（重试会再抢一次 lease，可能造成活锁）。
    """

    def __init__(self, action_id: str, expected: int, actual: int):
        self.action_id, self.expected, self.actual = action_id, expected, actual
        super().__init__(
            f"action {action_id} 的 fence 已从 {expected} 变成 {actual}"
            f"（lease 已被接管），本次提交作废。"
        )


# ══════════════════════════════════════════════════════════════════════════
# 命令
# ══════════════════════════════════════════════════════════════════════════

ENQUEUE = "action.enqueue"
CLAIM = "action.claim"
COMPLETE = "action.complete"
FAIL = "action.fail"
RECLAIM_EXPIRED = "action.reclaim_expired"


def install(kernel: RuntimeKernel) -> None:

    @kernel.register(ENQUEUE)
    def _enqueue(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        """登记一条待执行动作。**按 idempotency_key 去重。**

        ⚠️ 用 `INSERT ... ON CONFLICT DO NOTHING` 而不是"先 SELECT 再 INSERT"。
        后者在并发下会出事：两个任务同时读到"没有"，然后各插一条。
        让 `ux_actions_idem` 这个 UNIQUE 索引来定胜负——数据库层面只有一个能成功。
        """
        p = cmd.payload
        idem = p.get("idempotency_key") or ""
        if not idem:
            raise KernelError("action.enqueue 必须给 idempotency_key（它是去重的唯一依据）")
        kind = p.get("kind") or ""
        if not kind:
            raise KernelError("action.enqueue 必须给 kind")

        action_id = "act_" + uuid.uuid4().hex[:12]
        conn.execute(
            """INSERT INTO runtime_actions
                   (action_id, idempotency_key, kind, target_task_id, status,
                    fence, attempt_no, payload, created_at)
               VALUES (?,?,?,?,?,0,0,?,?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (action_id, idem, kind, p.get("target_task_id"), ActionStatus.PENDING,
             json.dumps(p.get("payload") or {}, ensure_ascii=False, default=str), ctx.now),
        )
        row = conn.execute(
            "SELECT * FROM runtime_actions WHERE idempotency_key=?", (idem,)
        ).fetchone()
        rec = ActionRecord.from_row(row)
        created = (rec.action_id == action_id)

        events = []
        if created:
            events.append(TransitionEvent("action.enqueued", "action", rec.action_id,
                                          detail={"kind": kind, "idem": idem}))
        return HandlerOutcome(
            data={"action_id": rec.action_id, "created": created, "status": rec.status},
            events=events,
        )

    @kernel.register(CLAIM)
    def _claim(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        """原子认领一条可执行的 action。返回 `(action, fence)`，没有可认领的则 action=None。

        可认领 = `PENDING`，**或** `CLAIMED` 但 lease 已过期（原 worker 大概率死了）。
        后者会 `fence += 1`，于是原 worker 若复活，它的提交会被 `FenceRejected` 挡掉。
        """
        p = cmd.payload
        worker = p.get("worker_id") or ""
        if not worker:
            raise KernelError("action.claim 必须给 worker_id（fence 拒绝时要能说清是谁被取代了）")
        lease = float(p.get("lease_seconds") or DEFAULT_LEASE_SECONDS)
        kinds = p.get("kinds") or []

        # ⚠️ 整段在 Kernel 的写事务里（BEGIN IMMEDIATE 已持写锁），
        # 所以"选一行 + 改它"是原子的，不需要额外的 SELECT ... FOR UPDATE（SQLite 也没有）。
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            sql = f"""SELECT * FROM runtime_actions
                      WHERE kind IN ({placeholders})
                        AND (status=? OR (status=? AND lease_until IS NOT NULL AND lease_until < ?))
                      ORDER BY created_at ASC, rowid ASC LIMIT 1"""
            args: tuple = (*kinds, ActionStatus.PENDING, ActionStatus.CLAIMED, ctx.now)
        else:
            sql = """SELECT * FROM runtime_actions
                     WHERE (status=? OR (status=? AND lease_until IS NOT NULL AND lease_until < ?))
                     ORDER BY created_at ASC, rowid ASC LIMIT 1"""
            args = (ActionStatus.PENDING, ActionStatus.CLAIMED, ctx.now)

        row = conn.execute(sql, args).fetchone()
        if row is None:
            return HandlerOutcome(data={"action": None})

        rec = ActionRecord.from_row(row)
        was_stolen = (rec.status == ActionStatus.CLAIMED)
        new_fence = rec.fence + 1
        conn.execute(
            """UPDATE runtime_actions
               SET status=?, claimed_by=?, lease_until=?, fence=?, attempt_no=attempt_no+1
               WHERE action_id=?""",
            (ActionStatus.CLAIMED, worker, ctx.now + lease, new_fence, rec.action_id),
        )
        if was_stolen:
            logger.warning(
                f"[Runtime] action {rec.action_id} 的 lease 已过期，"
                f"从 {rec.claimed_by!r} 接管给 {worker!r}（fence {rec.fence}→{new_fence}）"
            )
        return HandlerOutcome(
            data={"action": {"action_id": rec.action_id, "kind": rec.kind,
                             "target_task_id": rec.target_task_id,
                             "payload": rec.payload,
                             "attempt_no": rec.attempt_no + 1},
                  "fence": new_fence, "stolen_from": rec.claimed_by if was_stolen else None},
            events=[TransitionEvent("action.claimed", "action", rec.action_id,
                                    detail={"worker": worker, "fence": new_fence})],
        )

    def _finish(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext,
                status: str) -> HandlerOutcome:
        p = cmd.payload
        action_id = cmd.subject_id or p.get("action_id") or ""
        fence = p.get("fence")
        if fence is None:
            raise KernelError(
                f"{cmd.kind} 必须带 fence —— 不带就无法拒绝被取代的旧 worker 的提交（不变量 15）"
            )
        row = conn.execute(
            "SELECT * FROM runtime_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise KernelError(f"action 不存在: {action_id}")
        rec = ActionRecord.from_row(row)
        if rec.fence != int(fence):
            raise FenceRejected(action_id, int(fence), rec.fence)
        if rec.status in ActionStatus._TERMINAL:
            # 幂等友好：已终态就直接返回，不报错（可能是重放）
            return HandlerOutcome(data={"action_id": action_id, "status": rec.status,
                                        "already_final": True})
        conn.execute(
            "UPDATE runtime_actions SET status=?, completed_at=?, last_error=? WHERE action_id=?",
            (status, ctx.now, p.get("error"), action_id),
        )
        return HandlerOutcome(
            data={"action_id": action_id, "status": status},
            events=[TransitionEvent(f"action.{status.lower()}", "action", action_id,
                                    detail={"error": p.get("error")})],
        )

    @kernel.register(COMPLETE)
    def _complete(conn, cmd, ctx) -> HandlerOutcome:
        return _finish(conn, cmd, ctx, ActionStatus.DONE)

    @kernel.register(FAIL)
    def _fail(conn, cmd, ctx) -> HandlerOutcome:
        return _finish(conn, cmd, ctx, ActionStatus.FAILED)

    @kernel.register(RECLAIM_EXPIRED)
    def _reclaim(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        """把 lease 过期的 CLAIMED 打回 PENDING。由 Reconciler 周期调用。

        ⚠️ 这里**不**碰 fence —— fence 在真正被重新 claim 时才递增。
        原因：打回 PENDING 只是"让它重新可认领"，此时旧 worker 若复活提交，
        它的 fence 仍然等于当前值，**应该被接受**（它确实完成了工作，只是慢）。
        只有当另一个 worker 真的接管了，旧结果才该作废。
        这个区分很细但很重要：过早递增 fence 会白扔掉本来有效的工作成果。
        """
        rows = conn.execute(
            """SELECT action_id FROM runtime_actions
               WHERE status=? AND lease_until IS NOT NULL AND lease_until < ?""",
            (ActionStatus.CLAIMED, ctx.now),
        ).fetchall()
        ids = [r["action_id"] for r in rows]
        if ids:
            conn.execute(
                f"""UPDATE runtime_actions SET status=?, claimed_by=NULL, lease_until=NULL
                    WHERE action_id IN ({','.join('?' * len(ids))})""",
                (ActionStatus.PENDING, *ids),
            )
            logger.info(f"[Runtime] {len(ids)} 条 action 的 lease 已过期，打回 PENDING")
        return HandlerOutcome(
            data={"reclaimed": ids},
            events=[TransitionEvent("action.reclaimed", "action", aid) for aid in ids],
        )


# ══════════════════════════════════════════════════════════════════════════
# 读取 API
# ══════════════════════════════════════════════════════════════════════════

def get_action(kernel: RuntimeKernel, action_id: str) -> Optional[ActionRecord]:
    with kernel.store.read() as conn:
        row = conn.execute(
            "SELECT * FROM runtime_actions WHERE action_id=?", (action_id,)
        ).fetchone()
    return ActionRecord.from_row(row) if row else None


def find_by_idempotency_key(kernel: RuntimeKernel, key: str) -> Optional[ActionRecord]:
    with kernel.store.read() as conn:
        row = conn.execute(
            "SELECT * FROM runtime_actions WHERE idempotency_key=?", (key,)
        ).fetchone()
    return ActionRecord.from_row(row) if row else None


def pending_count(kernel: RuntimeKernel) -> int:
    with kernel.store.read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runtime_actions WHERE status IN (?,?)",
            (ActionStatus.PENDING, ActionStatus.CLAIMED),
        ).fetchone()
    return int(row["n"])


def list_open_actions(kernel: RuntimeKernel, limit: int = 100) -> list[ActionRecord]:
    with kernel.store.read() as conn:
        rows = conn.execute(
            """SELECT * FROM runtime_actions WHERE status IN (?,?)
               ORDER BY created_at ASC, rowid ASC LIMIT ?""",
            (ActionStatus.PENDING, ActionStatus.CLAIMED, limit),
        ).fetchall()
    return [ActionRecord.from_row(r) for r in rows]
