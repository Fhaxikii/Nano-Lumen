# -*- coding: utf-8 -*-
"""WaitCondition —— "Nano 在等什么" 的唯一权威。

═══ 为什么需要它（不是重构，是修一类真事故）═══

旧 Suspension 模块是个单表 CRUD，2026-08-04 实际运行中撞出**不死的挂起**：
一句"我还在挂着等 playwright · browser_navigate"永久粘在 UI 上，重启也不消失。
查库确认三条唤醒路径**全堵死**：

| 路径 | 为什么不生效 |
|---|---|
| `due_timers()` 轮询 | SQL 条件含 `timer_at IS NOT NULL`，而它是 `None` → 永远查不到 |
| 用户发消息 | background-only 的挂起只注入、不 resolve（有意设计，但没考虑后台永不回来）|
| 后台完成回调 | **边沿信号**，任务没回来就永远不触发 |

这三条不是三个 bug，是**一个建模错误的三种表现**：
把"等待"建成了"一条记录 + 几个回调"，而不是**一个可以被反复重算的状态**。

═══ 四个刻意的建模选择 ═══

**① `SATISFIED` 与 `CONSUMED` 是两个状态，不是一个。**
   这就是"边沿转状态"（要求落在通用 outbox 上）：
   后台完成时把**结果一起落盘**成 SATISFIED，谁取走谁再标 CONSUMED。
   中间进程崩了，结果还在，下次轮询照样捞得到。
   ⚠️ 合成一个状态 = 回到"消费掉了但 turn 从未发生"那个老形状
   （`resume_suspension` 已经栽过一次）。

**② `fire_at`（到点 = 该去看一眼）与 `expire_at`（截止 = 不等了）是两个字段。**
   旧表只有一个 `timer_at`，于是"定时唤醒"和"等太久了"共用一个含义，
   而 `due_timers()` 又要求它非空 —— **没有定时的等待因此永远查不到**，
   这正是不死挂起的第一条路径。语义相反的两件事不能挤在一个字段里。

   ⚠️⚠️ **本条 2026-08-07 被交叉评审改过一次。**
   第一版写的是"到点 = 条件满足（→ SATISFIED）"，那是错的：

   > 唤醒不能等于完成。Timer 到点只表示"值得继续/检查"。

   犯的是**和旧表同一类错误**，只是压的是另一对含义：
     · 「5 分钟后叫我」        —— 时间本身就是条件，到点确实等于满足
     · 「5 分钟后看看 CI 好没好」—— 到点只是该去看，CI 大概率还没好
   而 ②b 把 `wait_for` 收窄成定时轮询之后，**第二种是唯一用途**。
   所以到点现在进 `DUE_FOR_REVIEW`，由上层看过之后再决定 SATISFY 还是重排。

   📌 判据：**事件只证明世界发生了变化，不证明工作可以继续。**

**③ `orphan_at` 是无条件兜底，与前两者独立。**
   明确要求："即使前两条都失效也不会留下不死记录"。
   所以它不看 kind、不看 wake_on、不管有没有 fire_at —— 到点就回收。
   ⚠️ 收成 `ORPHANED` 而不是 `CANCELLED`：用户没有取消任何东西，
   是我们等的那个东西再也没回来。历史要读得出真相。

**④ 一切都是 level-triggered。**
   没有任何状态依赖"某个回调一定会被调到"。所有推进都来自
   `tick()` 重新扫一遍当前状态 —— 崩溃、漏事件、时钟跳变都能自愈。

═══ 与 Interaction 的分工 ═══

* **Interaction**：Nano 在等**用户**（要人回答）。
* **WaitCondition**：Nano 在等**世界**（定时到点、后台任务回来）。

两者都能阻塞 Task，都通过 `register_blocker_provider` 汇报，互不包含。
⚠️ 别把"等用户回答"塞进这里 —— 那会造出第二个权威，正是三步迁移禁止的。

迁移分三步走：先观测、再切读、最后切写并收口形状；现在本模块是唯一运行时权威。
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
    InvariantViolation,
    KernelError,
    RuntimeKernel,
    TransitionEvent,
)
from core.runtime import task as _task_mod
from core.runtime.task import Blocker, register_blocker_provider


# ══════════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════════

class WaitKind:
    """在等什么。**只描述来源，不决定行为** —— 行为由 wake_on / 各时间字段决定。"""
    TIMER      = "timer"        # 到点
    BACKGROUND = "background"   # 某个后台任务回来
    EXTERNAL   = "external"     # 外部事件（文件出现、端口通了…）
    _ALL = frozenset({TIMER, BACKGROUND, EXTERNAL})


class WaitStatus:
    WAITING   = "WAITING"
    # ⭐ 到点了、该去看一眼了 —— **但还不知道条件成没成**。
    #   这一档是交叉评审加出来的（不变量①：唤醒 ≠ 完成）。
    #   少了它，`fire_at` 到点就会被当成"条件达成"，而定时轮询的场景里
    #   到点几乎总是意味着"还没好，再看看"。
    DUE_FOR_REVIEW = "DUE_FOR_REVIEW"
    SATISFIED = "SATISFIED"    # 条件达成、结果已落盘，**尚未被取走**
    CONSUMED  = "CONSUMED"     # 结果已被取走并用掉（终态）
    EXPIRED   = "EXPIRED"      # 到 `expire_at` 仍未满足（终态）
    ORPHANED  = "ORPHANED"     # 兜底回收：等的东西再也没回来（终态）
    CANCELLED = "CANCELLED"    # 用户/模型主动取消（终态）
    _ALL = frozenset({WAITING, DUE_FOR_REVIEW, SATISFIED, CONSUMED,
                      EXPIRED, ORPHANED, CANCELLED})
    # ⚠️ SATISFIED **算活的**：结果还没被取走，这件事就没完。
    #    把它当终态会让"落盘了但没人消费"的结果被 GC 掉 —— 那就是边沿信号回潮。
    _LIVE = frozenset({WAITING, DUE_FOR_REVIEW, SATISFIED})
    _TERMINAL = frozenset({CONSUMED, EXPIRED, ORPHANED, CANCELLED})


class WakeSource:
    """哪些信号可以让它满足。与旧 `core/suspension.WakeSource` 取值保持一致，
    迁移时不必翻译。

    ⚠️⚠️ **`USER` 是【历史值】：库里读得到，但不许再新写。**

    已定：「**用户发消息不再 resolve 任何挂起**」，
    所以 `wake_on=['user']` 现在等于**一个有效唤醒源都没有** ——
    那条记录只能靠 orphan 兜底回收，中间它是**不死记录**。

    🔴 而早先两处写着这件事已经做完（「工具 schema + **存储校验都拒绝**」），
       **实际只做了一半**：schema 收窄了、resolve 逻辑改了，**存储层照旧接受**。
       📌 **一条声称「两处都改了」的记录，很可能只改了一处。**

    ⭐⭐ **为什么保留在 `_ALL` 里而不是直接删掉**：
       实测日志里出现过 `唤醒源=['user']`，
       库里**存在**这样的历史记录。把它从 `_ALL` 删掉，
       读那些旧记录时不变量会当场红 —— 那是**用「禁止新写」的手段去改「已有数据」**。
    📌 **「这个值合法吗」和「还能不能新写这个值」是两个问题，
       必须用两个集合表达。** 压成一个，就只能在
       「拒绝旧数据」和「放行新写入」之间二选一，两个都错。
    """
    USER       = "user"
    TIMER      = "timer"
    BACKGROUND = "background"
    #: 读取侧：库里合法出现的全部取值（**含历史值**）。不变量用这个。
    _ALL = frozenset({USER, TIMER, BACKGROUND})
    #: 写入侧：**新记录**允许的取值。`_open` 用这个。
    _ACCEPTED_FOR_NEW = frozenset({TIMER, BACKGROUND})


class Resolution:
    """终态原因。与 interaction 的同名概念平行，但取值是这一域自己的。"""
    DONE                  = "DONE"                    # 正常满足并消费
    DEADLINE              = "DEADLINE"                # 到 expire_at
    NEVER_RETURNED        = "NEVER_RETURNED"          # 兜底回收
    USER_CANCELLED        = "USER_CANCELLED"
    INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"
    _ALL = frozenset({DONE, DEADLINE, NEVER_RETURNED,
                      USER_CANCELLED, INTERRUPTED_BY_RESTART})


# 兜底回收的默认年龄。建议 30 分钟。
# ⚠️ 这是**上限不是节奏** —— 正常路径应该早就把它收掉了，
#    走到这条线本身就说明有东西不对，日志要响亮。
# ⭐ 没有活载体的等待（纯定时 / 外部事件）的兜底：多久没消息就认定它再也不回来了。
DEFAULT_ORPHAN_AGE_SEC = 30 * 60
# ══════════════════════════════════════════════════════════════════════════
# 记录
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WaitRecord:
    wait_id: str
    kind: str
    status: str
    wake_on: tuple[str, ...] = ()
    reason: str = ""
    intent: str = "condition_recheck"
    owner_task_id: Optional[str] = None
    owner_turn_id: Optional[str] = None
    bg_ref: Optional[str] = None
    legacy_susp_id: Optional[str] = None
    fire_at: Optional[float] = None
    expire_at: Optional[float] = None
    orphan_at: Optional[float] = None
    result: Optional[dict[str, Any]] = None
    satisfied_at: Optional[float] = None
    satisfied_by: Optional[str] = None
    consumed_at: Optional[float] = None
    resolution: Optional[str] = None
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: Optional[float] = None

    @property
    def is_live(self) -> bool:
        return self.status in WaitStatus._LIVE

    @property
    def awaiting_consumption(self) -> bool:
        """结果已经在库里躺着、等人来取。**轮询就是靠这个捞的。**"""
        return self.status == WaitStatus.SATISFIED


def _row_to_record(row: sqlite3.Row) -> WaitRecord:
    return WaitRecord(
        wait_id=row["wait_id"],
        kind=row["kind"],
        status=row["status"],
        wake_on=tuple(json.loads(row["wake_on"] or "[]")),
        reason=row["reason"] or "",
        intent=row["intent"] or "condition_recheck",
        owner_task_id=row["owner_task_id"],
        owner_turn_id=row["owner_turn_id"],
        bg_ref=row["bg_ref"],
        legacy_susp_id=row["legacy_susp_id"],
        fire_at=row["fire_at"],
        expire_at=row["expire_at"],
        orphan_at=row["orphan_at"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        satisfied_at=row["satisfied_at"],
        satisfied_by=row["satisfied_by"],
        consumed_at=row["consumed_at"],
        resolution=row["resolution"],
        revision=int(row["revision"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
    )


# ══════════════════════════════════════════════════════════════════════════
# 读路径（不经 Command —— 读不改状态）
# ══════════════════════════════════════════════════════════════════════════

def get(kernel: RuntimeKernel, wait_id: str) -> Optional[WaitRecord]:
    with kernel.store.read() as conn:
        row = conn.execute(
            "SELECT * FROM wait_conditions WHERE wait_id=?", (wait_id,)).fetchone()
    return _row_to_record(row) if row else None


def list_live(kernel: RuntimeKernel, owner_task_id: str | None = None, *,
              oldest_first: bool = False) -> list[WaitRecord]:
    """所有还没了结的等待（含已满足但没人取的）。"""
    # ⚠️ 三个活态都要算。漏掉 DUE_FOR_REVIEW 会让"到点待查"的记录
    #    在清单里凭空消失 —— 那正是旧实现"没设定时就查不到"的同款形状。
    sql = ("SELECT * FROM wait_conditions "
           "WHERE status IN ('WAITING','DUE_FOR_REVIEW','SATISFIED')")
    args: list[Any] = []
    if owner_task_id is not None:
        sql += " AND owner_task_id=?"
        args.append(owner_task_id)
    # 默认最新在前，与 interaction 的清单一致。仍需复刻旧 Suspension 展示/处理顺序的
    # 生产调用方必须显式声明 oldest_first，不能为一种排序再养一套兼容 API。
    sql += (" ORDER BY created_at ASC, rowid ASC" if oldest_first
            else " ORDER BY created_at DESC, rowid DESC")
    with kernel.store.read() as conn:
        return [_row_to_record(r) for r in conn.execute(sql, args).fetchall()]


def list_satisfied(kernel: RuntimeKernel) -> list[WaitRecord]:
    """结果已落盘、等人来取的。

    ⭐ **这个查询就是"边沿转状态"的兑现方式**：不需要任何回调还活着，
    重启之后照样捞得到。
    """
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM wait_conditions WHERE status='SATISFIED' "
            "ORDER BY satisfied_at ASC, rowid ASC"
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def list_due_for_review(kernel: RuntimeKernel) -> list[WaitRecord]:
    """到点了、等着被查一眼的那批。

    ⭐ 与 `list_satisfied()` 的区别就是这次改动的全部意义：
      · `SATISFIED`      —— 条件**已经**成了，结果在库里等人取
      · `DUE_FOR_REVIEW` —— **还不知道成没成**，该去看一眼了
    上层拿到这批之后去看，看完再决定 SATISFY 还是重新排一次。
    """
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM wait_conditions WHERE status='DUE_FOR_REVIEW' "
            "ORDER BY fire_at ASC, rowid ASC"
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def due_now(kernel: RuntimeKernel, now: float | None = None) -> list[WaitRecord]:
    """到点该被满足的定时等待。

    ⚠️ 与旧 `due_timers()` 的关键差别：**不再要求 `fire_at IS NOT NULL` 之外的东西**。
    旧实现用同一个 `timer_at` 兼表"定时"和"超时"，导致没设定时的等待
    在任何查询里都不出现 —— 那是不死挂起的第一条路径。
    """
    t = kernel.now() if now is None else now
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM wait_conditions WHERE status='WAITING' "
            "AND fire_at IS NOT NULL AND fire_at <= ? ORDER BY fire_at ASC",
            (t,),
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def list_due_wakeups(kernel: RuntimeKernel,
                     now: float | None = None) -> list[WaitRecord]:
    """需要上层驱动一次唤醒尝试的等待。

    与 `due_now()` 的职责不同：后者只找 WAITING，供状态机把它推进到
    DUE_FOR_REVIEW；这里还必须包含已经 DUE_FOR_REVIEW 的记录。若上一次唤醒
    尝试没能真正起 turn，它仍应在下一次轮询中被捞到，而不是永久失联。
    """
    t = kernel.now() if now is None else now
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM wait_conditions "
            "WHERE status IN ('WAITING','DUE_FOR_REVIEW') "
            "AND fire_at IS NOT NULL AND fire_at <= ? "
            "ORDER BY fire_at ASC, rowid ASC",
            (t,),
        ).fetchall()
    return [_row_to_record(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
# Command 名
# ══════════════════════════════════════════════════════════════════════════

# ⭐ DUE：到点了，该去看一眼 —— **不代表条件满足**（不变量①）。
DUE      = "wait.due"
OPEN     = "wait.open"
SATISFY  = "wait.satisfy"
CONSUME  = "wait.consume"
CANCEL   = "wait.cancel"
EXPIRE   = "wait.expire"
ORPHAN   = "wait.orphan"
# ⭐ [回看设计 2026-08-09] 重排**这一条**的回看时刻。
#    ⚠️ 与「再建一条」的区别是根本性的：一条长任务只该有**一条**等待，
#       它的回看点是这条记录的属性，不是一件新的事。
#    ⭐ 而这也正好朝着已定的目标态走 —— `wait_for` 最终应该
#       退化成「让当前 Task 在某时刻重新可运行」。
RESCHEDULE = "wait.reschedule"
TOUCH    = "wait.touch"


# ══════════════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════════════

def _require(conn: sqlite3.Connection, wid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM wait_conditions WHERE wait_id=?", (wid,)).fetchone()
    if row is None:
        raise KernelError(f"WaitCondition {wid} 不存在")
    return row


def _bump(conn: sqlite3.Connection, wid: str, now: float) -> int:
    conn.execute(
        "UPDATE wait_conditions SET revision=revision+1, updated_at=? WHERE wait_id=?",
        (now, wid))
    return int(conn.execute(
        "SELECT revision FROM wait_conditions WHERE wait_id=?", (wid,)).fetchone()[0])


def _close(conn: sqlite3.Connection, wid: str, status: str,
           resolution: str, now: float) -> int:
    if status not in WaitStatus._TERMINAL:
        raise KernelError(f"{status} 不是终态，不能用 _close")
    if resolution not in Resolution._ALL:
        raise KernelError(f"未知 resolution: {resolution!r}")
    conn.execute(
        "UPDATE wait_conditions SET status=?, resolution=?, closed_at=? WHERE wait_id=?",
        (status, resolution, now, wid))
    return _bump(conn, wid, now)


# ══════════════════════════════════════════════════════════════════════════
# 安装
# ══════════════════════════════════════════════════════════════════════════

def install(kernel: RuntimeKernel) -> None:
    kernel.register_revision_source("wait", "wait_conditions", "wait_id")

    # ── open ─────────────────────────────────────────────────────────────

    @kernel.register(OPEN)
    def _open(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        p = cmd.payload
        kind = p.get("kind", "")
        if kind not in WaitKind._ALL:
            raise KernelError(f"未知 WaitKind: {kind!r}，允许: {sorted(WaitKind._ALL)}")

        wake_on = list(p.get("wake_on") or [])
        # ⚠️⚠️ **写入侧用 `_ACCEPTED_FOR_NEW`，不用 `_ALL`。**
        #    `_ALL` 含历史值 `user`（库里真有那样的旧记录，见 `WakeSource` 的说明），
        #    但**新记录不许再带它** —— 现在 `user` 不 resolve 任何东西，
        #    带着它进库就是一条只能靠 orphan 回收的不死记录。
        #    📌 **「这个值合法吗」和「还能不能新写这个值」是两个问题。**
        bad = [w for w in wake_on if w not in WakeSource._ACCEPTED_FOR_NEW]
        if bad:
            _hist = [w for w in bad if w in WakeSource._ALL]
            _msg = (f"唤醒源 {bad} 不允许用于新记录，"
                    f"允许: {sorted(WakeSource._ACCEPTED_FOR_NEW)}")
            if _hist:
                _msg += (f"（{_hist} 是**历史值** —— 库里读得到，但现在"
                         f"用户消息不再 resolve 任何挂起，所以它已经不是有效唤醒源）")
            raise KernelError(_msg)
        if not wake_on:
            # ⚠️ 没有唤醒源 = 天生的不死记录。直接拒绝，别让它进库。
            # 这正是 2026-08-04 那两条孤儿记录的形状（wake_on 只有 background、
            # 而那个 background 再也没回来）。
            raise KernelError("wait_on 不能为空 —— 没有唤醒源的等待永远不会结束")

        wid = p.get("wait_id") or ("wait_" + uuid.uuid4().hex[:10])

        # ⭐ orphan_at 无条件设置。调用方可以覆盖，但**不能关掉**。
        # "即使前两条都失效也不会留下不死记录"。
        orphan_at = p.get("orphan_at")
        if orphan_at is None:
            orphan_at = ctx.now + float(p.get("orphan_age_sec") or DEFAULT_ORPHAN_AGE_SEC)

        conn.execute(
            """INSERT INTO wait_conditions
               (wait_id, owner_task_id, owner_turn_id, kind, status, wake_on, reason, intent,
                bg_ref, legacy_susp_id, fire_at, expire_at, orphan_at,
                revision, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (wid, p.get("owner_task_id"), p.get("owner_turn_id"), kind,
             WaitStatus.WAITING, json.dumps(wake_on, ensure_ascii=False),
             p.get("reason", ""), p.get("intent") or "condition_recheck",
             p.get("bg_ref"), p.get("legacy_susp_id"),
             p.get("fire_at"), p.get("expire_at"), orphan_at,
             ctx.now, ctx.now),
        )
        return HandlerOutcome(
            data={"wait_id": wid, "kind": kind, "orphan_at": orphan_at},
            revision=1,
            events=[TransitionEvent("wait.opened", "wait", wid, 1,
                                    {"kind": kind, "wake_on": wake_on})],
        )


    @kernel.register(TOUCH)
    def _touch(conn, cmd, ctx) -> HandlerOutcome:
        """心跳：载体还活着 → 把 `orphan_at` 推后。**只动这一个字段。**

        ⚠️ 刻意**不碰** `fire_at` / `status` —— 心跳说的是「它还在跑」，
           不是「该看一眼了」。📌 两件事压进一个命令，将来一定有人拿它去做另一件。
        """
        wid = cmd.payload.get("wait_id") or cmd.subject_id
        row = _require(conn, wid)
        if row["status"] not in (WaitStatus.WAITING, WaitStatus.DUE_FOR_REVIEW):
            return HandlerOutcome(data={"wait_id": wid, "touched": False})
        conn.execute("UPDATE wait_conditions SET orphan_at=? WHERE wait_id=?",
                     (ctx.now + DEFAULT_ORPHAN_AGE_SEC, wid))
        return HandlerOutcome(data={"wait_id": wid, "touched": True})

    @kernel.register(RESCHEDULE)
    def _reschedule(conn, cmd, ctx) -> HandlerOutcome:
        """重排这一条的回看时刻。`DUE_FOR_REVIEW → WAITING`（**重新武装**）。

        ⭐⭐ **这是「回看一眼」能成立的那块拼图**（2026-08-09 定的设计）：
        系统只负责「多久之后开始怀疑」这**第一次**，之后每次回看都由**模型**
        根据看到的东西自己决定下一次 —— 进度 10% 就排远一点，
        95% 就干脆不排（等完成信号），明显坏了就换办法。
        📌 **常量只该承担系统答得出的那个问题**（「多久之后开始怀疑」），
           「这件事还要多久」只有模型能答。

        ⚠️⚠️ **必须把状态推回 `WAITING`**，否则重排不起作用：
           `_due` 对已是 `DUE_FOR_REVIEW` 的是 no-op，
           所以停在那一档的记录再也不会触发下一次回看。
           📌 **「重新武装」不只是改时间，还要把状态退回可触发的那一档。**

        ⚠️ `fire_at=None` 是合法的：意思是「不用再回看了，等完成信号」。
           那时这条等待只剩 `background` 一个源，而 `orphan_at` 仍在兜底。
           📌 **「不再回看」和「这条等待结束了」是两件事** —— 前者只是撤掉一个源。
        """
        p = cmd.payload
        wid = p.get("wait_id") or cmd.subject_id
        row = _require(conn, wid)
        if row["status"] not in (WaitStatus.WAITING, WaitStatus.DUE_FOR_REVIEW):
            raise KernelError(
                f"WaitCondition {wid} 当前是 {row['status']}，不能重排回看"
                f"（只有 WAITING / DUE_FOR_REVIEW 可以）")
        new_fire = p.get("fire_at")
        # ⭐⭐ **兜底回收的期限也跟着推后。**
        #
        # ⚠️ `orphan_at` 在开启时就定死（默认 30 分钟），它防的是
        #    「等的东西再也没回来、没人管了」。而一次成功的回看**恰恰是
        #    「有人管」的证据** —— 模型刚看过、说它还好着。
        # 🔴 不推的话：一个正常的 40 分钟下载会在第 30 分钟被兜底杀掉，
        #    即使 Nano 上一分钟才确认过它没问题。
        # 📌 **一个「兜底回收」的期限，应该在每次「有人确认它还活着」时被推后** ——
        #    否则兜底会杀掉它本来要保护的东西。
        # ⚠️ 但**不取消**兜底（不设成 NULL）：模型也可能忘了再回看，
        #    那时仍然要有人来收。📌 推后 ≠ 取消。
        _orphan = ctx.now + DEFAULT_ORPHAN_AGE_SEC
        conn.execute(
            "UPDATE wait_conditions SET fire_at=?, status=?, orphan_at=? "
            "WHERE wait_id=?",
            (new_fire, WaitStatus.WAITING, _orphan, wid))
        rev = _bump(conn, wid, ctx.now)
        return HandlerOutcome(
            data={"wait_id": wid, "fire_at": new_fire},
            revision=rev,
            events=[TransitionEvent("wait.rescheduled", "wait", wid, rev,
                                    {"fire_at": new_fire})])

    # ── due（到点了，该看一眼）───────────────────────────────────────────

    @kernel.register(DUE)
    def _due(conn, cmd, ctx) -> HandlerOutcome:
        """WAITING → DUE_FOR_REVIEW。**只表示"该查了"，不表示条件成了。**

        ⭐ 这一档是交叉评审加出来的（不变量①：唤醒 ≠ 完成）。
        少了它，`tick()` 到点就直接 SATISFY，等于替上层做了它没资格做的判断 ——
        「5 分钟后看看 CI 好没好」到点时 CI 大概率**还没好**。

        ⚠️ 幂等：定时轮询每跳都会扫到它，直到有人把它推走。重复 DUE 不算错。
        """
        wid = cmd.payload["wait_id"]
        row = _require(conn, wid)
        if row["status"] == WaitStatus.DUE_FOR_REVIEW:
            return HandlerOutcome(data={"wait_id": wid, "already": True},
                                  revision=int(row["revision"]))
        if row["status"] != WaitStatus.WAITING:
            raise KernelError(
                f"WaitCondition {wid} 当前是 {row['status']}，不能转 DUE_FOR_REVIEW")
        conn.execute("UPDATE wait_conditions SET status=? WHERE wait_id=?",
                     (WaitStatus.DUE_FOR_REVIEW, wid))
        rev = _bump(conn, wid, ctx.now)
        return HandlerOutcome(
            data={"wait_id": wid, "already": False}, revision=rev,
            events=[TransitionEvent("wait.due", "wait", wid, rev, {})])

    # ── satisfy ──────────────────────────────────────────────────────────

    @kernel.register(SATISFY)
    def _satisfy(conn, cmd, ctx) -> HandlerOutcome:
        """WAITING → SATISFIED，**结果与状态在同一个事务里落盘**。

        ⭐ 这是本模块的核心：边沿信号（"后台回来了"）在这里变成状态。
        写完之后即使进程立刻死掉，结果也还在库里，`list_satisfied()` 捞得到。

        ⚠️ **不在这里做消费**。满足与消费分开，是因为它们**可能跨进程**：
        满足发生在后台线程，消费发生在下一轮对话。合成一步就等于
        "先标记用掉、再指望那一步真的发生" —— 这个形状已经栽过一次。
        """
        p = cmd.payload
        wid = p["wait_id"]
        row = _require(conn, wid)
        if row["status"] == WaitStatus.SATISFIED:
            # 幂等：同一个信号重复到达（outbox 重试是常态）不算错。
            return HandlerOutcome(data={"wait_id": wid, "already": True},
                                  revision=int(row["revision"]))
        if row["status"] not in (WaitStatus.WAITING, WaitStatus.DUE_FOR_REVIEW):
            raise KernelError(
                f"WaitCondition {wid} 当前是 {row['status']}，不能被满足"
                f"（只有 WAITING / DUE_FOR_REVIEW 可以）")

        conn.execute(
            "UPDATE wait_conditions SET status=?, result_json=?, "
            "satisfied_at=?, satisfied_by=? WHERE wait_id=?",
            (WaitStatus.SATISFIED,
             json.dumps(p.get("result") or {}, ensure_ascii=False, default=str),
             ctx.now, p.get("satisfied_by") or "unknown", wid),
        )
        rev = _bump(conn, wid, ctx.now)
        return HandlerOutcome(
            data={"wait_id": wid, "already": False},
            revision=rev,
            events=[TransitionEvent("wait.satisfied", "wait", wid, rev,
                                    {"by": p.get("satisfied_by")})],
        )

    # ── consume ──────────────────────────────────────────────────────────

    @kernel.register(CONSUME)
    def _consume(conn, cmd, ctx) -> HandlerOutcome:
        """SATISFIED → CONSUMED。**取走结果，且只能取走一次。**

        幂等靠状态 CAS：并发两个消费者，只有一个拿到 `consumed=True`。
        """
        wid = cmd.payload["wait_id"]
        row = _require(conn, wid)
        if row["status"] == WaitStatus.CONSUMED:
            return HandlerOutcome(
                data={"wait_id": wid, "consumed": False,
                      "result": json.loads(row["result_json"] or "{}")},
                revision=int(row["revision"]))
        if row["status"] != WaitStatus.SATISFIED:
            raise KernelError(
                f"WaitCondition {wid} 当前是 {row['status']}，没有可消费的结果")

        conn.execute("UPDATE wait_conditions SET consumed_at=? WHERE wait_id=?",
                     (ctx.now, wid))
        rev = _close(conn, wid, WaitStatus.CONSUMED, Resolution.DONE, ctx.now)
        return HandlerOutcome(
            data={"wait_id": wid, "consumed": True,
                  "result": json.loads(row["result_json"] or "{}")},
            revision=rev,
            events=[TransitionEvent("wait.consumed", "wait", wid, rev, {})],
        )

    # ── cancel / expire / orphan ─────────────────────────────────────────

    def _terminal_handler(status: str, resolution: str, allow_from: frozenset):
        def _h(conn, cmd, ctx) -> HandlerOutcome:
            wid = cmd.payload["wait_id"]
            row = _require(conn, wid)
            if row["status"] == status:
                return HandlerOutcome(data={"wait_id": wid, "already": True},
                                      revision=int(row["revision"]))
            if row["status"] not in allow_from:
                raise KernelError(
                    f"WaitCondition {wid} 当前是 {row['status']}，不能转 {status}")
            rev = _close(conn, wid, status,
                         cmd.payload.get("resolution") or resolution, ctx.now)
            return HandlerOutcome(
                data={"wait_id": wid, "already": False}, revision=rev,
                events=[TransitionEvent(f"wait.{status.lower()}", "wait", wid, rev, {})])
        return _h

    # ⚠️ 取消可以发生在 SATISFIED 上（结果回来了但用户说不要了），
    #    过期/回收只对 WAITING 有意义（已经满足了就不算没等到）。
    kernel.register(CANCEL)(_terminal_handler(
        WaitStatus.CANCELLED, Resolution.USER_CANCELLED,
        frozenset({WaitStatus.WAITING, WaitStatus.SATISFIED})))
    kernel.register(EXPIRE)(_terminal_handler(
        WaitStatus.EXPIRED, Resolution.DEADLINE, frozenset({WaitStatus.WAITING})))
    kernel.register(ORPHAN)(_terminal_handler(
        WaitStatus.ORPHANED, Resolution.NEVER_RETURNED, frozenset({WaitStatus.WAITING})))

    # ── 不变量 ───────────────────────────────────────────────────────────

    def _inv_enums(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT wait_id, status, kind FROM wait_conditions "
            f"WHERE status NOT IN ({','.join('?' * len(WaitStatus._ALL))}) "
            f"   OR kind NOT IN ({','.join('?' * len(WaitKind._ALL))}) LIMIT 1",
            (*sorted(WaitStatus._ALL), *sorted(WaitKind._ALL)),
        ).fetchone()
        if row is not None:
            raise InvariantViolation(
                "wait_enums",
                f"WaitCondition {row['wait_id']} 的 status/kind 越界："
                f"{row['status']}/{row['kind']}")

    def _inv_no_immortal(conn: sqlite3.Connection) -> None:
        """活着的等待必须有兜底回收时间。

        ⭐ 这条不变量**就是不死挂起的结构性防线**：没有 `orphan_at` 的活记录
        在定义上就是不死的，无论上层逻辑写得多小心。
        """
        row = conn.execute(
            "SELECT wait_id FROM wait_conditions "
            "WHERE status IN ('WAITING','DUE_FOR_REVIEW','SATISFIED') "
            "AND orphan_at IS NULL LIMIT 1"
        ).fetchone()
        if row is not None:
            raise InvariantViolation(
                "wait_no_immortal",
                f"WaitCondition {row['wait_id']} 还活着却没有 orphan_at —— "
                f"这是一条不死记录")

    def _inv_consumed_has_result(conn: sqlite3.Connection) -> None:
        """消费过的必须留着当时那份结果。

        ⚠️ 没有它，"消费"就退化成"删掉"，事后无法回答"当时到底拿到了什么"。
        """
        row = conn.execute(
            "SELECT wait_id FROM wait_conditions "
            "WHERE status='CONSUMED' AND (satisfied_at IS NULL OR result_json IS NULL) "
            "LIMIT 1"
        ).fetchone()
        if row is not None:
            raise InvariantViolation(
                "wait_consumed_has_result",
                f"WaitCondition {row['wait_id']} 是 CONSUMED 却没有满足记录/结果")

    def _inv_terminal_closed(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT wait_id, status FROM wait_conditions "
            f"WHERE status IN ({','.join('?' * len(WaitStatus._TERMINAL))}) "
            "AND closed_at IS NULL LIMIT 1",
            tuple(sorted(WaitStatus._TERMINAL)),
        ).fetchone()
        if row is not None:
            raise InvariantViolation(
                "wait_terminal_closed",
                f"WaitCondition {row['wait_id']} 已是 {row['status']} 却没有 closed_at")

    kernel.register_invariant("wait_enums", _inv_enums)
    kernel.register_invariant("wait_no_immortal", _inv_no_immortal)
    kernel.register_invariant("wait_consumed_has_result", _inv_consumed_has_result)
    kernel.register_invariant("wait_terminal_closed", _inv_terminal_closed)

    # ── blocker provider ─────────────────────────────────────────────────
    #
    # ⚠️ **SATISFIED 也算阻塞**：结果回来了但还没人用，这个 Task 仍然没法往下走。
    #    只把 WAITING 算作阻塞，会让 Reconciler 以为它可以推进了。

    def _wait_blockers(conn: sqlite3.Connection, task_id: str) -> list[Blocker]:
        rows = conn.execute(
            "SELECT wait_id, kind, status, reason FROM wait_conditions "
            "WHERE owner_task_id=? AND status IN ('WAITING','DUE_FOR_REVIEW','SATISFIED')",
            (task_id,),
        ).fetchall()
        return [
            Blocker(blocker_kind="wait", blocker_id=r["wait_id"],
                    summary=(r["reason"] or "")[:80],
                    detail={"kind": r["kind"], "status": r["status"]})
            for r in rows
        ]

    register_blocker_provider("wait", _wait_blockers)

    # ⭐ 「这件事最近一次动静」—— 空壳对话回收的新鲜度锚点。
    #    ⚠️ 与 blocker provider 的区别：blocker 只报**还活着的**，
    #       这个连**已经收掉的**也算 —— 因为「刚收掉一条等待」本身就是动静。
    #       📌 一条刚刚结束的东西，是「这件事还在进行」的证据，不是相反。
    #    ⚠️ 取 updated_at / closed_at / created_at 里最大的那个：
    #       三个都可能是最新的一次（改动 / 收尾 / 刚建）。
    def _activity(conn: sqlite3.Connection, task_id: str) -> float:
        r = conn.execute(
            "SELECT MAX(MAX(COALESCE(updated_at,0), COALESCE(closed_at,0)), "
            "           COALESCE(created_at,0)) AS t "
            "FROM wait_conditions WHERE owner_task_id=?", (task_id,)).fetchone()
        return float((r["t"] if r else 0) or 0.0)

    _task_mod.register_activity_provider("waitcond", _activity)

    # ── Reconcile 接线 ───────────────────────────────────────────────────

    from core.runtime import reconciler as _rec

    def _startup(k: RuntimeKernel, report) -> None:
        n = startup_sweep(k)
        if n:
            report.extra["waits_swept"] = n

    def _tick(k: RuntimeKernel, report) -> None:
        stats = tick(k)
        for key, val in stats.items():
            if val:
                report.extra[f"waits_{key}"] = val

    _rec.register_startup_step("wait_startup_sweep", _startup)
    _rec.register_tick_step("wait_tick", _tick)


# ══════════════════════════════════════════════════════════════════════════
# Reconcile 步骤（level-triggered：每次重新算，不依赖任何回调）
# ══════════════════════════════════════════════════════════════════════════

def tick(kernel: RuntimeKernel, now: float | None = None) -> dict[str, int]:
    """每跳重算一次：谁到点了、谁超时了、谁该回收了。

    ⭐ 三件事**互相独立**，任何一件失效都不影响另外两件 ——
    这正是不死挂起（三条路径同时堵死）的结构性解法。

    返回 `{fired, expired, orphaned}`，供 Reconcile 报告统计。
    """
    t = kernel.now() if now is None else now
    stats = {"fired": 0, "expired": 0, "orphaned": 0}

    def _rows(where: str) -> list[sqlite3.Row]:
        with kernel.store.read() as conn:
            return conn.execute(
                f"SELECT wait_id FROM wait_conditions WHERE status='WAITING' AND {where}",
                (t,)).fetchall()

    # ① 到点 → **值得重新评估**（注意：不是"满足"）
    #
    # ⚠️⚠️ 第一版这里直接 `SATISFY` + `result={"reason": "timer fired"}`，
    # 也就是把「到点」当成了「条件达成」。交叉评审一针见血：
    #
    #   > 唤醒不能等于完成。Timer 到点只表示"值得继续/检查"，
    #   > 不能自动把 blocker 标成 satisfied。
    #
    # 这跟批评旧表"把两个含义压进一个字段"是**同一类错误**，只是压的是另一对：
    #   · 「5 分钟后叫我」   —— 时间本身就是条件，到点确实等于满足
    #   · 「5 分钟后看看 CI 好没好」—— 到点只是该去看，**CI 大概率还没好**
    # 第二种才是这个工具现在唯一的用途（②b 已把 wait_for 收窄成定时轮询）。
    #
    # 📌 判据：**事件只证明世界发生了变化，不证明工作可以继续。**
    #    判断"能不能继续"是模型/Task 的事，不是定时器的事。
    #
    # 所以到点只做两件事：把它标成"该看了"、让上层起一轮去看。
    # 结论由那一轮得出 —— 满足就 SATISFY，没满足就再排一次。
    #
    # ⚠️ 目标态是 `ScheduledTrigger → ResumeTask(task_id)`，
    #    由 Task 决定 blocker 到底解没解。Task 现在零接线，先停在这一步。
    for r in _rows("fire_at IS NOT NULL AND fire_at <= ?"):
        try:
            kernel.submit(Command(kind=DUE, subject_id=r["wait_id"],
                                  payload={"wait_id": r["wait_id"]}))
            stats["fired"] += 1
        except Exception as e:      # pragma: no cover
            logger.error(f"[Runtime] 定时等待 {r['wait_id']} 到点处理失败: {e}")

    # ② 到截止仍未满足 → 过期（坏事）
    #
    # ⚠️ 顺序在 ① 之后：同一跳里既到点又到截止时，**到点优先**。
    #    反过来会把一个刚好达成的等待判成失败。
    for r in _rows("expire_at IS NOT NULL AND expire_at <= ?"):
        try:
            kernel.submit(Command(kind=EXPIRE, subject_id=r["wait_id"],
                                  payload={"wait_id": r["wait_id"]}))
            stats["expired"] += 1
        except Exception as e:      # pragma: no cover
            logger.error(f"[Runtime] 等待 {r['wait_id']} 过期处理失败: {e}")

    # ③ 兜底回收 —— 无条件，不看 kind 也不看有没有前两者
    for r in _rows("orphan_at IS NOT NULL AND orphan_at <= ?"):
        try:
            kernel.submit(Command(kind=ORPHAN, subject_id=r["wait_id"],
                                  payload={"wait_id": r["wait_id"]}))
            stats["orphaned"] += 1
            # ⚠️ 响亮：走到这条线说明①②都没收住它，本身就是个信号。
            logger.warning(
                f"[Runtime] 等待 {r['wait_id']} 超过兜底年龄仍未有任何结果 → ORPHANED。"
                f"它等的那个东西再也没回来。")
        except Exception as e:      # pragma: no cover
            logger.error(f"[Runtime] 等待 {r['wait_id']} 兜底回收失败: {e}")

    return stats


# ══════════════════════════════════════════════════════════════════════════
# 三步迁移的历史判据
#
# 判据是："迁移前先问『新旧两套能不能同时跑完再比』。
#                 能 → 走三步迁移；不能（路由/互斥分支）→ 直接切。"
#
# 挂起是**状态存储**不是路由决策：两套可以各记一份、事后对答案，
# 比出来的是**事实**而不是"如果走另一条会怎样"的推测。
# 所以这条**该做 shadow** —— 与澄清路由那次（不能对答案）不同。
#
# 观测期的规矩是「旧权威照常运行、新内核只观察、分歧绝不纠正」。
# 切读、切写并验证之后，双写事实已经消失：继续保留可调用 shadow 会让人误以为
# 仍有第二套账。历史结论保留在这里，运行脚手架全部退役。
# ══════════════════════════════════════════════════════════════════════════


def startup_sweep(kernel: RuntimeKernel) -> int:
    """启动时处理"上个进程死时正在等的东西"。

    ⚠️ **只收 BACKGROUND，不碰 TIMER。** 两者的语义完全不同：
      · 后台任务的执行体随进程一起没了，**不可能**再回来 → 必须收掉；
      · 定时到点是纯时间函数，重启后照样成立 → 让 `tick()` 正常处理它。
    一刀切收掉全部，会把"重启后本该触发的定时提醒"一起抹掉。

    ⚠️ 同样**不碰 SATISFIED**：结果已经落盘了，它正等着被消费 ——
    这恰恰是本模块存在的意义，重启不该让它蒸发。
    """
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT wait_id FROM wait_conditions "
            "WHERE status='WAITING' AND kind=?", (WaitKind.BACKGROUND,)).fetchall()
    n = 0
    for r in rows:
        try:
            kernel.submit(Command(
                kind=ORPHAN, subject_id=r["wait_id"],
                payload={"wait_id": r["wait_id"],
                         "resolution": Resolution.INTERRUPTED_BY_RESTART}))
            n += 1
        except Exception as e:      # pragma: no cover
            logger.error(f"[Runtime] 启动清理后台等待 {r['wait_id']} 失败: {e}")
    if n:
        logger.info(f"[Runtime] 启动清理 {n} 个后台等待（执行体已随上个进程消失）")
    return n


def _get_kernel():
    """⚠️ 内联 import —— `get_kernel` 是单例取值器，模块级 import 会固化一个
    尚未初始化的引用（与 `oslease` 里同一条理由）。"""
    from core.runtime.kernel import get_kernel
    return get_kernel()


# ══════════════════════════════════════════════════════════════════════════
# 正式调用入口
# ══════════════════════════════════════════════════════════════════════════

def open_wait(*, reason: str, wake_on: list[str],
              timer_seconds: Optional[int] = None,
              bg_ref: Optional[str] = None,
              owner_task_id: Optional[str] = None,
              intent: str = "condition_recheck") -> Optional[WaitRecord]:
    """登记一条等待。返回完整 `WaitRecord`（失败返回 None）。

    ⭐ 先搬写点、再单独更换返回形状，避免一次迁移同时动两个变量。
    现在下游直接读取完整六态，`wait_id` 是唯一新身份；历史反查字段只读不写。
    📌 **兼容字段该在被兼容的东西消失时留空，而不是继续伪造。**
    """
    try:
        k = _get_kernel()
        clean = [w for w in (wake_on or []) if w in WakeSource._ACCEPTED_FOR_NEW]
        # ⭐ `detached` =「不在手头」：这条等待**只等完成信号，不排回看**。
        #    它与 `system_recheck` 的差别不是「有没有 timer」（那是后果），
        #    而是**Nano 此刻还等不等它** —— 前者等（所以要定期看一眼），
        #    后者不等（所以看它没有意义，完成时叫醒即可）。
        #    📌 意图必须是被写下的事实：与「不许由 `timer_at` 猜」逐字同形。
        if intent not in {"scheduled_plan", "condition_recheck",
                          "system_recheck", "detached"}:
            logger.warning(f"[WaitC] 拒绝登记：未知等待意图 {intent!r}")
            return None
        if not clean:
            logger.warning(f"[WaitC] 拒绝登记：没有有效唤醒源（给的是 {wake_on}）—— "
                           f"没有唤醒源的等待永远不会结束")
            return None
        fire_at = None
        if timer_seconds and int(timer_seconds) > 0:
            fire_at = k.now() + float(timer_seconds)
        # ⚠️ 同上：`bg_ref` 优先 —— `kind` 说「在等什么」，`fire_at` 说「何时回看」。
        kind = (WaitKind.BACKGROUND if bg_ref
                else WaitKind.TIMER if fire_at is not None
                else WaitKind.EXTERNAL)
        # ⚠️ 归属：等待是「跨多个 turn」的归属物 → 有权让一件事诞生。
        #    调用方没给就自己去问，别把这条纪律留给每个调用点记。
        if owner_task_id is None:
            try:
                from core.runtime import task as _tk
                # 🔴🔴 **这里曾经传 `reason`，2026-08-22 改成空串。**
                #
                #   `reason` 描述的是**这条等待**（例："still running: ping 127.0.0.1 -n 60"），
                #   而 `goal_summary` 的语义是**用户手上这件事**。
                #   📌 又一个「一个字段表达两个现实」——「等什么」被当成了「这件事是什么」。
                #
                # ⚠️ 实际后果（2026-08-22 撞到）：等待被满足之后，
                #    那条 conversation Task 仍然 ACTIVE，`goal_summary` 逐字写着
                #    「still running: ping …」，而它**每轮都被注入模型**。
                #    于是**用户重置对话之后，Nano 还在说「那条命令已经在后台运行了」**
                #    —— 而 ping 早就跑完了。对话重置管不着它（那是 runtime Task）。
                #
                # ⭐ 空串是**诚实**的：这条 Task 是等待顺带催生的，
                #    系统并不知道「用户这件事」叫什么名字。
                #    而空 `goal` 本来就会被下游过滤掉（不进重启提醒、不误导模型）。
                owner_task_id = _tk.ensure_conversation_task("")
            except Exception as _e:
                logger.debug(f"[WaitC] 归属没落上（等待照旧登记）: {_e}")
        r = k.submit(Command(kind=OPEN, payload={
            "kind": kind,
            "wake_on": clean,
            "reason": reason or "",
            "intent": intent,
            "bg_ref": bg_ref,
            "fire_at": fire_at,
            "owner_task_id": owner_task_id,
        }))
        wid = r.data.get("wait_id")
        rec = get(k, wid)
        if rec is None:
            return None
        logger.info(f"[WaitC] 登记等待 {wid}: 等 {reason!r} / 唤醒源={clean} / "
                    f"fire_at={fire_at}")
        return rec
    except Exception as e:
        logger.error(f"[WaitC] 登记等待失败: {e}")
        return None


def find_by_id(kernel: RuntimeKernel, reference: str) -> Optional[WaitRecord]:
    """按当前 `wait_id` 或历史 `legacy_susp_id` 读取完整记录。

    `get()` 始终是严格的新 ID 查询；历史兼容只集中在这一处。先查当前 ID，
    避免调用方继续把所有身份都理解成「旧 suspension id」。
    """
    if not reference:
        return None
    rec = get(kernel, reference)
    if rec is not None:
        return rec
    with kernel.store.read() as conn:
        row = conn.execute(
            "SELECT * FROM wait_conditions WHERE legacy_susp_id=? LIMIT 1",
            (reference,),
        ).fetchone()
    return _row_to_record(row) if row else None


def resolve_wait(reference: str, resolved_by: str = "") -> bool:
    """把一条活等待正常收尾；不存在或已终态时幂等返回 False。

    保持 SATISFY → CONSUME 两步：结果先持久化，再被消费。合成一步会重新打开
    「状态已结束但触发的 turn 从未发生」那类崩溃窗口。
    """
    try:
        k = _get_kernel()
        rec = find_by_id(k, reference)
        if rec is None or not rec.is_live:
            return False
        k.submit(Command(kind=SATISFY, subject_id=rec.wait_id, payload={
            "wait_id": rec.wait_id,
            "satisfied_by": resolved_by or "unknown",
            "result": {"resolved_by": resolved_by},
        }))
        k.submit(Command(kind=CONSUME, subject_id=rec.wait_id,
                         payload={"wait_id": rec.wait_id}))
        return True
    except Exception as e:
        logger.error(f"[WaitC] 收掉 {reference} 失败: {e}")
        return False


def cancel_wait(reference: str, resolved_by: str = "") -> bool:
    """主动取消一条活等待；保留 CANCELLED，而不是伪装成正常完成。"""
    try:
        k = _get_kernel()
        rec = find_by_id(k, reference)
        if rec is None or not rec.is_live:
            return False
        k.submit(Command(kind=CANCEL, subject_id=rec.wait_id, payload={
            "wait_id": rec.wait_id,
            "reason": resolved_by or "cancelled",
        }))
        return True
    except Exception as e:
        logger.error(f"[WaitC] 取消 {reference} 失败: {e}")
        return False


def parked_without_recheck() -> list:
    """后台、且**没有安排回看**的那些等待（`fire_at IS NULL` 且还活着）。

    ⭐⭐ [2026-08-22] 建模原图里最容易被漏掉的那条支线的数据源：

        next_step 做完／前台空了／用户否决了前台那件事
                             ▼
                  👁 视线转向后台「那件事好了没」

    🔴 代码此前把「转入后台」做成了**单向门**：`reschedule_wait(None)` 之后
       只剩完成唤醒，再也没人回头看它。而原图从来不是这么画的。
    ⭐ **「转入后台」取消的是「在我做别的事的时候被拽回来」，不是「以后都不看了」。**
       不回看的理由是**注意力被别的事占着** —— 而那个理由会过期：手一空就该恢复。
    📌 **一个基于「此刻注意力在别处」的暂停，不该做成永久取消。**

    ⚠️ 只认 `bg_ref` 非空的：那是「后台」的判据（有载体在自动跑）。
       没有载体的等待（纯定时器）不属于后台，不该被这条拉回来。
    """
    try:
        k = _get_kernel()
        with k.store.read() as conn:
            rows = conn.execute(
                "SELECT wait_id FROM wait_conditions "
                "WHERE fire_at IS NULL AND bg_ref IS NOT NULL AND bg_ref != '' "
                "AND status IN (?,?)",
                (WaitStatus.WAITING, WaitStatus.DUE_FOR_REVIEW)).fetchall()
        return [r["wait_id"] for r in rows]
    except Exception as e:
        logger.debug(f"[WaitC] 列后台没回看的失败（忽略）: {e}")
        return []


def touch_by_bg_ref(bg_ref: str) -> bool:
    """载体还活着 → 把这条等待的兜底回收期限推后。**后台那条的心跳。**

    ⭐⭐ 前台那条的心跳是**回看**（见 `_reschedule` 里那段：
       「一次成功的回看恰恰是『有人管』的证据」），而 `dont_wait` 之后
       `fire_at=None` —— **后台的东西没有回看**，于是没有任何东西去推 `orphan_at`。
    🔴 后果：一个装 35 分钟的安装包，30 分钟被 `ORPHANED`，
       35 分钟真装完时完成通知落到一条**已终态**的记录上 → **结果丢失**。
    ⭐ 所以后台那条的「有人管」证据换成另一个事实：**载体还在跑**。
       📌 `orphan_at` 的定义是「我们等的那个东西**再也没回来**」，
          而载体还活着恰恰证明它还会回来 ——
          **「多久没消息」是近似物，「载体还在不在」才是能精确回答的那个问题。**

    ⚠️ **只推后，不取消**（同 `_reschedule` 那条纪律）：载体一死就不再推，
       兜底照常到点。📌 推后 ≠ 取消。
    ⚠️ 静默失败：找不到 / 已终态都返回 False，**不许把心跳的失败变成载体的失败**。
    """
    if not bg_ref:
        return False
    try:
        k = _get_kernel()
        with k.store.read() as conn:
            row = conn.execute(
                "SELECT wait_id FROM wait_conditions WHERE bg_ref=? AND status IN (?,?) "
                "LIMIT 1", (bg_ref, WaitStatus.WAITING, WaitStatus.DUE_FOR_REVIEW)).fetchone()
        if row is None:
            return False
        k.submit(Command(kind=TOUCH, subject_id=row["wait_id"], payload={
            "wait_id": row["wait_id"]}))
        return True
    except Exception as e:
        logger.debug(f"[WaitC] 心跳推后失败（忽略）: {e}")
        return False


def reschedule_wait(suspension_id: str, seconds: Optional[float]) -> bool:
    """把这一条的下一次回看排到 `seconds` 秒之后。`None` = 不再回看。

    ⭐ 这是「模型自己决定下一次回看」的落点。
    ⚠️ 返回 False 只表示没排上（记录已终态 / 找不到），**不该阻断调用方** ——
       最坏情况是这条等待只剩完成信号 + `orphan_at` 兜底。
    """
    try:
        k = _get_kernel()
        rec = find_by_id(k, suspension_id)
        if rec is None:
            logger.debug(f"[WaitC] 重排回看：找不到 {suspension_id}")
            return False
        fire_at = None if seconds is None else k.now() + float(seconds)
        k.submit(Command(kind=RESCHEDULE, subject_id=rec.wait_id, payload={
            "wait_id": rec.wait_id, "fire_at": fire_at}))
        logger.info(f"[WaitC] {rec.wait_id} 下一次回看 → "
                    f"{('%.0fs 后' % seconds) if seconds is not None else '不再回看'}")
        return True
    except Exception as e:
        logger.warning(f"[WaitC] 重排回看失败（{suspension_id}）: {e}")
        return False

