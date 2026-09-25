# -*- coding: utf-8 -*-
"""OSActivityLease / AuthorizationLease —— 「谁正在操作这台电脑」。

═══ 这一项要解决的两类事故（都已在代码里坐实）═══

**① `_os_task_busy` 泄漏 → 视觉自检永久停摆，而且一声不响。**
`orchestrator.py` 的 `os_execute` 分支置 `_os_task_busy = True` 之后**没有
`try/finally`**（另一处 `_run_os_skill_plan_loop` 有）。生成器被中途丢弃或抛异常时
标志卡在 True → `canary.should_run(self._os_task_busy)` 恒假 →
**视觉定位自检从此不再跑**。目前靠"每轮重置"把爆炸半径压到一轮，
但那是止血不是修好：**一个裸 bool 没法表达"谁持有、持有多久、过期了算谁的"**。

**② `_temp_auto` 是个没有主人的授权。**
它现在是 `app.py` 上一个裸 bool，语义是"本次任务临时 auto"，
失效条件写在 UI 里（`任何 mini 窗关 → _temp_auto = False`）。
于是"这次授权到底还有效吗"这个问题，**答案取决于一个窗口的开关状态**。

═══ 三个刻意的建模选择 ═══

**① 租约有主人、有期限、有 fence。**
`_os_task_busy = True` 回答不了"谁开的、什么时候该过期、过期后谁能接管"。
租约三件事都能回答，而且**过期是被动推导的**（`held_until <= now`），
不依赖任何人记得去清 —— 这正是 `_os_task_busy` 泄漏的解法。

**② fence 与 outbox 用同一套语义**（见 `outbox.FenceRejected` 的注释）：
每次接管 `fence += 1`，旧持有者带着旧 fence 回来时**安静作废**。
⚠️ 这不是异常处理，是**正常运行的一部分** —— 卡住超过期限就该被取代。
GUI 自动化尤其需要它：一个卡死的 OS 任务不能永久占着鼠标键盘。

**③ 授权租约与活动租约是两个东西，不许合并。**
  · `OSActivityLease`   ——「**我正在操作**这台电脑」（互斥，同时只能有一个）
  · `AuthorizationLease`——「**我被允许**做某类操作而不再逐次确认」（可并存多条）
两者生命周期完全不同：一次 GUI 任务跑完，活动租约就该还；
但用户给的"这次别再问我了"可能要跨好几次任务。
⚠️ 现状把后者绑在 mini 窗的开关上，就是这两件事被压成一件的后果。

═══ 与 WaitCondition / Interaction 的分界 ═══

* **Interaction**：Nano 在等**用户回答**。
* **WaitCondition**：Nano 在等**世界**（到点、后台回来）。
* **OSActivityLease**：**没有人在等** —— 它表示"这台电脑此刻归谁用"。

⚠️ 有一条结论在这里直接生效：**被动挂起（用户动了鼠标 → Nano 让位）
归本模块，不归 Suspension。** 它不是"等一件事发生"，是**一个租约被别人占着**。
往 suspension 里塞它就是重犯那一整条的错误。

═══ 当前状态（2026-08-07，**这一段才是 current truth**）═══

⚠️⚠️ **这里原来写的是「本模块刻意不接线，零现有调用方」—— 那是 ① 项刚写完时的话，
   现在早就不成立了，删掉。** 核实成立：
   同一个文件里同时留着迁移前的历史说明和迁移后的现状说明，
   **过两周谁也分不清哪句是现行规则**。

**活动租约（activity）已经是权威**：
  · `maybe_run_canary` 读 `machine_is_free()`（不是 `nano_may_touch_os()`，见那两个函数的对照表）
  · `dispatch.execute()` 对鼠标键盘动作读 `nano_may_touch_os()`
  · `os_execute` 开工前必须拿到租约，拿不到就让位
  · Nano 的 acquire **不带 preempt** —— 抢占权只属于用户
  · 粒度是**一整段 GUI 操作**（每轮开头归还），不是一个动作
  · `_os_task_busy` 仍在维护但**只写不读**，纯粹是验证期的回退路，之后会删

**授权租约（authorization）也已经是权威**（2026-08-08，④ 的另一半）：
  · `os.gui_session` —— 「Nano 在做 GUI 任务吗」，被动挂起的**作用域**
  · `os.temp_auto`   —— 「用户许可了本次任务免逐个确认吗」，`_auto_on()` 读它
  · 两者**寿命常常相同但不许合并**（今天就已分岔：`_global_auto` 开着时会
    缩窗但不发 `temp_auto`）—— 📌 **答的是不是同一个问题才是合并的判据。**
  · 两者都**在启动时收掉**（执行体是那个 GUI 任务，随进程消失）
  · `_temp_auto` / `_os_task_busy` 仍在写但**只写不读**，是验证期的回退路，之后会删

⚠️ **上面 ②「`_temp_auto` 是个没有主人的授权」那段讲的是它被换掉之前的问题** ——
   保留是因为它是「为什么要有授权租约」的唯一记录，但**那已经是历史，不是现状**。

📌 判据：**模块头必须只写现状。** 历史要留，但要明确标成历史，
   否则它会被下一个读代码的人（包括模型）当成现行规则去模仿。
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
    HandlerContext,
    HandlerOutcome,
    InvariantViolation,
    KernelError,
    RuntimeKernel,
    TransitionEvent,
)


# ══════════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════════

class LeaseKind:
    ACTIVITY      = "activity"       # 持有者正在操作这台电脑（互斥）
    AUTHORIZATION = "authorization"  # 持有者被允许做某类操作（可并存）
    _ALL = frozenset({ACTIVITY, AUTHORIZATION})


class LeaseStatus:
    HELD      = "HELD"
    RELEASED  = "RELEASED"    # 正常归还（终态）
    PREEMPTED = "PREEMPTED"   # 被抢占 —— 用户动手了 / 别人接管了（终态）
    EXPIRED   = "EXPIRED"     # 到期没人续（终态）
    REVOKED   = "REVOKED"     # 用户/系统撤销（终态）
    _ALL = frozenset({HELD, RELEASED, PREEMPTED, EXPIRED, REVOKED})
    _TERMINAL = frozenset({RELEASED, PREEMPTED, EXPIRED, REVOKED})


class Holder:
    """谁持有。**用户也是一个合法持有者** —— 这是被动挂起的建模基础：
    用户动鼠标不是"打断了 Nano"，是**用户拿走了这台电脑的活动租约**。"""
    NANO = "nano"
    USER = "user"
    _ALL = frozenset({NANO, USER})


# 活动租约的默认时长。⚠️ 这是**兜底不是节奏** —— 正常路径应该主动归还，
# 走到过期说明持有者没能收尾（就是 `_os_task_busy` 泄漏那个形状）。
DEFAULT_ACTIVITY_TTL_SEC = 120.0
# 心跳续期的建议间隔。长任务要自己续，续不上就该被抢。
DEFAULT_HEARTBEAT_SEC = 30.0


class LeasePreempted(KernelError):
    """带着过期/被接管的 fence 回来提交 —— 你的租约已经不是你的了。

    ⚠️ 与 `outbox.FenceRejected` 同源语义：**这是正常运行的一部分，不是 bug。**
    调用方应当安静停手（GUI 场景下尤其重要：继续点下去就是在和用户抢鼠标），
    **不要重试** —— 重试等于再抢一次，会和用户拉锯。
    """

    def __init__(self, lease_id: str, expected: int, actual: int):
        self.lease_id, self.expected, self.actual = lease_id, expected, actual
        super().__init__(
            f"lease {lease_id} 的 fence 已从 {expected} 变成 {actual}"
            f"（已被接管或过期重发），本次操作作废。")


# ══════════════════════════════════════════════════════════════════════════
# 记录
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LeaseRecord:
    lease_id: str
    kind: str
    status: str
    holder: str
    fence: int = 0
    scope: str = ""                 # authorization 用：授权范围（动作类/风险级）
    reason: str = ""
    owner_task_id: Optional[str] = None
    owner_turn_id: Optional[str] = None
    held_until: Optional[float] = None
    payload: Optional[dict[str, Any]] = None
    resolution: Optional[str] = None
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: Optional[float] = None

    @property
    def is_held(self) -> bool:
        return self.status == LeaseStatus.HELD

    def is_expired(self, now: float) -> bool:
        """⚠️ 过期是**推导**出来的，不是某个人记得去写的状态。
        这正是 `_os_task_busy` 那个裸 bool 做不到的事。"""
        return (self.status == LeaseStatus.HELD
                and self.held_until is not None and self.held_until <= now)


def _row_to_record(row: sqlite3.Row) -> LeaseRecord:
    return LeaseRecord(
        lease_id=row["lease_id"], kind=row["kind"], status=row["status"],
        holder=row["holder"], fence=int(row["fence"]),
        scope=row["scope"] or "", reason=row["reason"] or "",
        owner_task_id=row["owner_task_id"], owner_turn_id=row["owner_turn_id"],
        held_until=row["held_until"],
        payload=json.loads(row["payload"]) if row["payload"] else None,
        resolution=row["resolution"], revision=int(row["revision"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
        closed_at=row["closed_at"],
    )


# ══════════════════════════════════════════════════════════════════════════
# 读路径
# ══════════════════════════════════════════════════════════════════════════

def get(kernel: RuntimeKernel, lease_id: str) -> Optional[LeaseRecord]:
    with kernel.store.read() as conn:
        row = conn.execute("SELECT * FROM os_leases WHERE lease_id=?",
                           (lease_id,)).fetchone()
    return _row_to_record(row) if row else None


def current_activity(kernel: RuntimeKernel, now: float | None = None) -> Optional[LeaseRecord]:
    """当前谁在操作这台电脑。**已过期的不算** —— 这就是取代 `_os_task_busy` 的读法。

    ⭐ 关键差别：`_os_task_busy` 泄漏后永远是 True；这里过期即自动不算数，
    **不需要任何人记得去清**。
    """
    t = kernel.now() if now is None else now
    with kernel.store.read() as conn:
        row = conn.execute(
            "SELECT * FROM os_leases WHERE kind=? AND status='HELD' "
            "AND (held_until IS NULL OR held_until > ?) LIMIT 1",
            (LeaseKind.ACTIVITY, t)).fetchone()
    return _row_to_record(row) if row else None


def nano_may_touch_os(kernel: RuntimeKernel, now: float | None = None) -> tuple[bool, str]:
    """Nano 现在能不能动这台电脑？返回 `(能不能, 为什么)`。

    ⚠️ **fail-safe 方向是"不能"**：读失败时宁可让 Nano 停手。
    与 `toolbatch.has_open_span()` 那条相反 —— 那边错成 True 会删掉合法历史，
    这边错成 True 会**在用户正打字时抢鼠标**，后果严重得多。
    """
    try:
        cur = current_activity(kernel, now)
    except Exception as e:
        logger.warning(f"[OSLease] 读租约失败，按「不能操作」处理: {e}")
        return False, "lease state unreadable"
    if cur is None:
        return True, "no one holds the machine"
    if cur.holder == Holder.NANO:
        return True, f"nano already holds it ({cur.lease_id})"
    return False, f"held by {cur.holder}: {cur.reason or 'user is using the machine'}"


#: 「这一轮是 GUI 模式」这个事实的 scope。
#:
#: ⭐⭐ **它就是被动挂起的作用域**（2026-08-07 定）：
#:   `该任务类型 ⟺ mini 窗口存在 ⟺ 被动挂起生效`，三者绑定。
#:   只要它在，Nano 的**任何**行为（命令行 / MCP / 键鼠 / skill）都受监控。
#:
#: ⚠️ **为什么必须是租约而不是 app.py 上的 `_mini_active` 那个裸 bool**：
#:   实测（cmd_log/44）传感器的武装条件原先是"Nano 持有活动租约"，
#:   而那只在键鼠动作那一瞬成立 —— 点 17 下、跨 17.6 秒，
#:   **全部** `ignored_nano_idle`，第 18 下才命中。
#:   📌 **当初把「Nano 此刻握着鼠标」当成了「Nano 此刻在做 GUI 任务」** ——
#:      前者瞬时断续，后者持续。作用域必须挂在后者上。
#:
#: ⚠️ 用 authorization 而不是 activity：它**可并存、允许跨动作、重启不动**，
#:   正是"一段任务模式"该有的生命周期；activity 是互斥的"谁在开车"，语义不同。
#: `owner_task_id` 由 Task 侧填入，"绑 Task"由此自然达成（
#:   而**不需要**等 Task 先落地）。
GUI_SESSION_SCOPE = "os.gui_session"


def gui_session_active(kernel: RuntimeKernel, now: float | None = None) -> bool:
    """这一轮是不是 GUI 模式（= mini 窗开着 = 被动挂起生效）。

    ⚠️ **fail-safe 方向是"不是"**：读不到就当没在 GUI 模式 ——
    宁可漏掉监控，也不要因为读库失败而把用户的每一次点击都当成接管
    （那会让 Nano 在一次数据库抖动之后彻底动不了）。
    """
    try:
        return is_authorized(kernel, GUI_SESSION_SCOPE, now)
    except Exception as e:
        logger.warning(f"[OSLease] 读 GUI 模式失败，按「不在 GUI 模式」处理: {e}")
        return False


def machine_is_free(kernel: RuntimeKernel, now: float | None = None) -> bool:
    """这台电脑现在**没人在开**吗（谁都算，包括 Nano 自己）。

    ⚠️⚠️ **这不是 `nano_may_touch_os()`，两者回答的是不同的问题，别混用。**

    | | Nano 持有 | 用户持有 | 无人持有 |
    |---|---|---|---|
    | `nano_may_touch_os()`「我能动手吗」| **True**（本来就是我的）| False | True |
    | `machine_is_free()`「机器闲着吗」| **False** | False | True |

    分歧就在第一列。**后台自检类的东西必须用这一个** ——
    它们要的是"没人在用屏幕"，而 Nano 自己正在跑 GUI 自动化时机器**不闲**。

    📌 早先写的是「`should_run` 改读 `nano_may_touch_os()`」，**那句是错的**：
    照它写，Nano 一边做 GUI 自动化、canary 一边去抢前台焦点做 UIA 定位 ——
    正是 `canary.py` 设计约束 ② 白纸黑字禁止的那件事
    （原文：「会重新引入『自检抢占前台焦点干扰正在执行的真实任务』这个刚修过的同类问题」）。
    已更正。

    ⭐ 顺带一个旧 bool 给不了的好处：**用户持有时它也返回 False**。
    `_os_task_busy` 只知道 Nano 忙不忙，不知道用户正在用电脑 ——
    所以旧实现下，用户打字时 canary 照样会跳出来抢焦点。
    """
    return current_activity(kernel, now) is None


def active_authorizations(kernel: RuntimeKernel, now: float | None = None) -> list[LeaseRecord]:
    """当前还有效的授权。过期的自动不算 —— 取代 `_temp_auto` 那个裸 bool。"""
    t = kernel.now() if now is None else now
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM os_leases WHERE kind=? AND status='HELD' "
            "AND (held_until IS NULL OR held_until > ?) "
            "ORDER BY created_at DESC, rowid DESC",
            (LeaseKind.AUTHORIZATION, t)).fetchall()
    return [_row_to_record(r) for r in rows]


def is_authorized(kernel: RuntimeKernel, scope: str, now: float | None = None) -> bool:
    """某个范围现在被授权了吗。

    ⚠️ 空 scope 的授权表示"全局"，会命中一切 —— 这是刻意的（对应现在的 `_global_auto`），
    但**建立**那种授权时要格外小心，所以 `grant` 里有专门的警告。
    """
    for r in active_authorizations(kernel, now):
        if not r.scope or r.scope == scope:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# Command 名
# ══════════════════════════════════════════════════════════════════════════

ACQUIRE   = "oslease.acquire"     # 拿活动租约
HEARTBEAT = "oslease.heartbeat"   # 续期（带 fence）
RELEASE   = "oslease.release"     # 主动归还（带 fence）
PREEMPT   = "oslease.preempt"     # 抢占（用户动手 / 别人接管）
GRANT     = "oslease.grant"       # 发一条授权
REVOKE    = "oslease.revoke"      # 撤销授权
EXPIRE    = "oslease.expire"      # 到期回收（由 tick 推动）


# ══════════════════════════════════════════════════════════════════════════
# 内部
# ══════════════════════════════════════════════════════════════════════════

def _require(conn: sqlite3.Connection, lid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM os_leases WHERE lease_id=?", (lid,)).fetchone()
    if row is None:
        raise KernelError(f"Lease {lid} 不存在")
    return row


def _bump(conn: sqlite3.Connection, lid: str, now: float) -> int:
    conn.execute("UPDATE os_leases SET revision=revision+1, updated_at=? WHERE lease_id=?",
                 (now, lid))
    return int(conn.execute("SELECT revision FROM os_leases WHERE lease_id=?",
                            (lid,)).fetchone()[0])


def _close(conn: sqlite3.Connection, lid: str, status: str,
           resolution: str, now: float) -> int:
    if status not in LeaseStatus._TERMINAL:
        raise KernelError(f"{status} 不是终态")
    conn.execute("UPDATE os_leases SET status=?, resolution=?, closed_at=? WHERE lease_id=?",
                 (status, resolution, now, lid))
    return _bump(conn, lid, now)


def _check_fence(row: sqlite3.Row, given: int | None) -> None:
    """带 fence 的操作必须对得上。`None` 表示调用方不关心（只允许在系统内部路径用）。"""
    if given is None:
        return
    if int(row["fence"]) != int(given):
        raise LeasePreempted(row["lease_id"], int(given), int(row["fence"]))


# ══════════════════════════════════════════════════════════════════════════
# 安装
# ══════════════════════════════════════════════════════════════════════════

def install(kernel: RuntimeKernel) -> None:
    kernel.register_revision_source("oslease", "os_leases", "lease_id")

    # ── acquire ──────────────────────────────────────────────────────────

    @kernel.register(ACQUIRE)
    def _acquire(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        """拿活动租约。**互斥**：已经有人持有且没过期时不给。

        ⚠️ 过期的旧租约在这里**顺手收掉**，而不是等 tick —— 否则
        "上一次任务崩了没还锁"会让下一次任务在 tick 到来之前一直拿不到，
        表现就是 `_os_task_busy` 泄漏的同款症状。
        """
        p = cmd.payload
        holder = p.get("holder", Holder.NANO)
        if holder not in Holder._ALL:
            raise KernelError(f"未知 holder: {holder!r}，允许: {sorted(Holder._ALL)}")

        cur = conn.execute(
            "SELECT * FROM os_leases WHERE kind=? AND status='HELD' LIMIT 1",
            (LeaseKind.ACTIVITY,)).fetchone()
        if cur is not None:
            _expired = (cur["held_until"] is not None and cur["held_until"] <= ctx.now)
            if not _expired:
                if not p.get("preempt"):
                    raise KernelError(
                        f"活动租约已被 {cur['holder']} 持有（{cur['lease_id']}），"
                        f"未过期。要接管请显式传 preempt=True。")
                # 显式抢占：把旧的打成 PREEMPTED
                _close(conn, cur["lease_id"], LeaseStatus.PREEMPTED,
                       p.get("preempt_reason") or f"preempted by {holder}", ctx.now)
            else:
                # ⭐ 到期未还 —— 这就是 `_os_task_busy` 泄漏那个形状，收掉并响亮记一笔
                _close(conn, cur["lease_id"], LeaseStatus.EXPIRED,
                       "not released before deadline", ctx.now)
                logger.warning(
                    f"[OSLease] 活动租约 {cur['lease_id']}（{cur['holder']}）到期未归还，"
                    f"已回收。持有者没能收尾 —— 这正是裸 bool 时代会永久卡死的那种情况。")

        lid = p.get("lease_id") or ("lease_" + uuid.uuid4().hex[:10])
        ttl = p.get("ttl_sec")
        ttl = DEFAULT_ACTIVITY_TTL_SEC if ttl is None else float(ttl)
        conn.execute(
            """INSERT INTO os_leases
               (lease_id, kind, status, holder, fence, scope, reason,
                owner_task_id, owner_turn_id, held_until, payload,
                revision, created_at, updated_at)
               VALUES (?,?,?,?,1,?,?,?,?,?,?,1,?,?)""",
            (lid, LeaseKind.ACTIVITY, LeaseStatus.HELD, holder,
             p.get("scope", ""), p.get("reason", ""),
             p.get("owner_task_id"), p.get("owner_turn_id"),
             ctx.now + ttl,
             json.dumps(p.get("payload") or {}, ensure_ascii=False, default=str),
             ctx.now, ctx.now),
        )
        return HandlerOutcome(
            data={"lease_id": lid, "fence": 1, "holder": holder,
                  "held_until": ctx.now + ttl},
            revision=1,
            events=[TransitionEvent("oslease.acquired", "oslease", lid, 1,
                                    {"holder": holder})],
        )

    # ── heartbeat ────────────────────────────────────────────────────────

    @kernel.register(HEARTBEAT)
    def _heartbeat(conn, cmd, ctx) -> HandlerOutcome:
        """续期。长任务必须自己续 —— **续不上就该被抢**。

        ⚠️ 心跳**不 bump fence**：它证明"我还活着"，不是"我重新拿了一次"。
        bump 的话，持有者自己的下一次提交就会被自己挤掉。
        """
        p = cmd.payload
        lid = p["lease_id"]
        row = _require(conn, lid)
        _check_fence(row, p.get("fence"))
        if row["status"] != LeaseStatus.HELD:
            raise KernelError(f"Lease {lid} 当前是 {row['status']}，不能续期")
        ttl = p.get("ttl_sec")
        ttl = DEFAULT_ACTIVITY_TTL_SEC if ttl is None else float(ttl)
        conn.execute("UPDATE os_leases SET held_until=? WHERE lease_id=?",
                     (ctx.now + ttl, lid))
        rev = _bump(conn, lid, ctx.now)
        return HandlerOutcome(data={"lease_id": lid, "held_until": ctx.now + ttl},
                              revision=rev)

    # ── release ──────────────────────────────────────────────────────────

    @kernel.register(RELEASE)
    def _release(conn, cmd, ctx) -> HandlerOutcome:
        """主动归还。幂等：已经不是 HELD 就当已还。

        ⚠️ **fence 不匹配时不抛**，只当作"你那份早就不是你的了"安静返回。
        理由见 `LeasePreempted`：被抢占是正常运行的一部分，而"归还"这个动作
        本身没有副作用 —— 为它抛异常只会让调用方的 finally 变得难写。
        """
        p = cmd.payload
        lid = p["lease_id"]
        row = _require(conn, lid)
        if row["status"] != LeaseStatus.HELD:
            return HandlerOutcome(data={"lease_id": lid, "already": True},
                                  revision=int(row["revision"]))
        given = p.get("fence")
        if given is not None and int(row["fence"]) != int(given):
            logger.info(f"[OSLease] {lid} 的 fence 已变（{given}→{row['fence']}），"
                        f"这次归还忽略 —— 它已经不属于调用方了")
            return HandlerOutcome(data={"lease_id": lid, "stale": True},
                                  revision=int(row["revision"]))
        rev = _close(conn, lid, LeaseStatus.RELEASED,
                     p.get("resolution") or "done", ctx.now)
        return HandlerOutcome(
            data={"lease_id": lid, "already": False}, revision=rev,
            events=[TransitionEvent("oslease.released", "oslease", lid, rev, {})])

    # ── preempt ──────────────────────────────────────────────────────────

    @kernel.register(PREEMPT)
    def _preempt(conn, cmd, ctx) -> HandlerOutcome:
        """抢占当前活动租约。**被动挂起就走这条。**

        ⭐ 用户动了鼠标/键盘 → 不是"打断了 Nano"，是**用户把这台电脑拿回去了**。
        Nano 手里那份立刻变成非 HELD，它下一次续期/动手就会被挡住 ——
        **这才是"瞬发"的正确实现**：不需要通知到 Nano，它自己碰壁就停。

        ⚠️ **挡住它的是「状态不再是 HELD」，不是 fence 不匹配。**
        `_close` 只 bump revision、**不动 fence**，所以 `_check_fence` 会照样放行，
        真正拒绝发生在下一行的状态检查（`HEARTBEAT` 里）。
        写 `except LeasePreempted` 想接住抢占的人会接了个空 —— 别按异常类型认。

        📌 被动挂起的实际接线**不走这条命令**，走 `ACQUIRE(holder=USER, preempt=True)`：
        光把 Nano 的租约打掉还不够 —— 那样 `current_activity()` 变 None，
        `nano_may_touch_os()` 反而会说"没人占着，可以动"。
        **用户必须真的持有它**，`Holder.USER` 就是为此存在的。
        这条 `PREEMPT` 留给"接管但不占有"的场景（如运维强制回收）。
        """
        p = cmd.payload
        by = p.get("by", Holder.USER)
        if by not in Holder._ALL:
            raise KernelError(f"未知 holder: {by!r}")
        cur = conn.execute(
            "SELECT * FROM os_leases WHERE kind=? AND status='HELD' LIMIT 1",
            (LeaseKind.ACTIVITY,)).fetchone()
        if cur is None:
            return HandlerOutcome(data={"preempted": None, "note": "no active lease"},
                                  revision=0)
        rev = _close(conn, cur["lease_id"], LeaseStatus.PREEMPTED,
                     p.get("reason") or f"preempted by {by}", ctx.now)
        logger.info(f"[OSLease] 活动租约 {cur['lease_id']}（{cur['holder']}）被 {by} 抢占")
        return HandlerOutcome(
            data={"preempted": cur["lease_id"], "prev_holder": cur["holder"]},
            revision=rev,
            events=[TransitionEvent("oslease.preempted", "oslease", cur["lease_id"], rev,
                                    {"by": by})])

    # ── grant / revoke（授权租约）────────────────────────────────────────

    @kernel.register(GRANT)
    def _grant(conn, cmd, ctx) -> HandlerOutcome:
        """发一条授权。**可以并存多条**，与活动租约的互斥性完全不同。

        ⚠️ `ttl_sec=None` 表示不过期。这是**危险的默认**，所以必须显式传 None ——
        现状 `_temp_auto` 的失效条件是"mini 窗关掉"，一个 UI 事件；
        换成时间之后至少它有个自己的寿命，不再取决于某个窗口开着没有。
        """
        p = cmd.payload
        scope = p.get("scope", "")
        lid = p.get("lease_id") or ("auth_" + uuid.uuid4().hex[:10])
        ttl = p.get("ttl_sec", 0)
        held_until = None if ttl is None else ctx.now + float(ttl or 0)
        if not scope:
            logger.warning(
                "[OSLease] 发了一条**空 scope** 的授权 —— 它会命中一切操作。"
                "这对应旧的 `_global_auto`，确认这是本意。")
        conn.execute(
            """INSERT INTO os_leases
               (lease_id, kind, status, holder, fence, scope, reason,
                owner_task_id, owner_turn_id, held_until, payload,
                revision, created_at, updated_at)
               VALUES (?,?,?,?,1,?,?,?,?,?,?,1,?,?)""",
            (lid, LeaseKind.AUTHORIZATION, LeaseStatus.HELD, Holder.NANO,
             scope, p.get("reason", ""), p.get("owner_task_id"),
             p.get("owner_turn_id"), held_until,
             json.dumps(p.get("payload") or {}, ensure_ascii=False, default=str),
             ctx.now, ctx.now),
        )
        return HandlerOutcome(
            data={"lease_id": lid, "scope": scope, "held_until": held_until},
            revision=1,
            events=[TransitionEvent("oslease.granted", "oslease", lid, 1,
                                    {"scope": scope})])

    @kernel.register(REVOKE)
    def _revoke(conn, cmd, ctx) -> HandlerOutcome:
        """撤销授权。给 `lease_id` 撤一条；给 `scope` 撤该范围全部；都不给撤全部。"""
        p = cmd.payload
        if p.get("lease_id"):
            rows = [_require(conn, p["lease_id"])]
        elif p.get("scope"):
            rows = conn.execute(
                "SELECT * FROM os_leases WHERE kind=? AND status='HELD' AND scope=?",
                (LeaseKind.AUTHORIZATION, p["scope"])).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM os_leases WHERE kind=? AND status='HELD'",
                (LeaseKind.AUTHORIZATION,)).fetchall()
        n, rev = 0, 0
        for row in rows:
            if row["status"] != LeaseStatus.HELD:
                continue
            rev = _close(conn, row["lease_id"], LeaseStatus.REVOKED,
                         p.get("reason") or "revoked", ctx.now)
            n += 1
        return HandlerOutcome(data={"revoked": n}, revision=rev or 0)

    # ── expire（由 tick 推动）────────────────────────────────────────────

    @kernel.register(EXPIRE)
    def _expire(conn, cmd, ctx) -> HandlerOutcome:
        lid = cmd.payload["lease_id"]
        row = _require(conn, lid)
        if row["status"] != LeaseStatus.HELD:
            return HandlerOutcome(data={"lease_id": lid, "already": True},
                                  revision=int(row["revision"]))
        rev = _close(conn, lid, LeaseStatus.EXPIRED, "deadline passed", ctx.now)
        return HandlerOutcome(
            data={"lease_id": lid}, revision=rev,
            events=[TransitionEvent("oslease.expired", "oslease", lid, rev, {})])

    # ── 不变量 ───────────────────────────────────────────────────────────

    def _inv_single_activity(conn: sqlite3.Connection) -> None:
        """**同时只能有一个人在操作这台电脑。**

        ⭐ 这条是本模块存在的核心理由：裸 bool 表达不了"谁"，
        自然也就防不住"两个东西都以为自己在开车"。
        """
        n = conn.execute(
            "SELECT COUNT(*) FROM os_leases WHERE kind=? AND status='HELD'",
            (LeaseKind.ACTIVITY,)).fetchone()[0]
        if n > 1:
            raise InvariantViolation(
                "oslease_single_activity",
                f"同时有 {n} 个活动租约处于 HELD —— 互斥被破坏了")

    def _inv_enums(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT lease_id, kind, status, holder FROM os_leases "
            f"WHERE kind NOT IN ({','.join('?' * len(LeaseKind._ALL))}) "
            f"   OR status NOT IN ({','.join('?' * len(LeaseStatus._ALL))}) "
            f"   OR holder NOT IN ({','.join('?' * len(Holder._ALL))}) LIMIT 1",
            (*sorted(LeaseKind._ALL), *sorted(LeaseStatus._ALL), *sorted(Holder._ALL)),
        ).fetchone()
        if row is not None:
            raise InvariantViolation(
                "oslease_enums",
                f"Lease {row['lease_id']} 的枚举越界："
                f"{row['kind']}/{row['status']}/{row['holder']}")

    def _inv_activity_has_deadline(conn: sqlite3.Connection) -> None:
        """活动租约**必须**有期限。

        ⭐ 没有期限的活动租约 = 一个永远不会自己松开的 `_os_task_busy`。
        这条不变量就是那个泄漏在库层面的防线。
        （授权租约允许无期限 —— 那是用户有意给的长期许可。）
        """
        row = conn.execute(
            "SELECT lease_id FROM os_leases WHERE kind=? AND status='HELD' "
            "AND held_until IS NULL LIMIT 1", (LeaseKind.ACTIVITY,)).fetchone()
        if row is not None:
            raise InvariantViolation(
                "oslease_activity_has_deadline",
                f"活动租约 {row['lease_id']} 没有 held_until —— "
                f"它永远不会自己松开，和裸 bool 泄漏是同一个东西")

    def _inv_terminal_closed(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT lease_id, status FROM os_leases "
            f"WHERE status IN ({','.join('?' * len(LeaseStatus._TERMINAL))}) "
            "AND closed_at IS NULL LIMIT 1",
            tuple(sorted(LeaseStatus._TERMINAL))).fetchone()
        if row is not None:
            raise InvariantViolation(
                "oslease_terminal_closed",
                f"Lease {row['lease_id']} 已是 {row['status']} 却没有 closed_at")

    kernel.register_invariant("oslease_single_activity", _inv_single_activity)
    kernel.register_invariant("oslease_enums", _inv_enums)
    kernel.register_invariant("oslease_activity_has_deadline", _inv_activity_has_deadline)
    kernel.register_invariant("oslease_terminal_closed", _inv_terminal_closed)

    # ── Reconcile 接线 ───────────────────────────────────────────────────

    from core.runtime import reconciler as _rec

    def _startup(k: RuntimeKernel, report) -> None:
        n = startup_release_all(k)
        if n:
            report.extra["leases_released_on_start"] = n

    def _tick(k: RuntimeKernel, report) -> None:
        n = expire_tick(k)
        if n:
            report.extra["leases_expired"] = n

    _rec.register_startup_step("oslease_startup", _startup)
    _rec.register_tick_step("oslease_expire", _tick)


# ══════════════════════════════════════════════════════════════════════════
# Reconcile 步骤
# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# 活动租约的运行时接线（先 shadow 观测 → **现在内核已是权威**）
#
# 判据是：「新旧两套能不能同时跑完再比？」
# `_os_task_busy` / `_temp_auto` 都是**状态标志**，两套各记一份、事后对答案 →
# 走三步迁移。第一步（旧权威 + 镜像）已完成，并且量出了泄漏的真实频率
# （按一下 Escape → 16 次观测 13 次泄漏、持续 61 分钟）。
#
# ⭐ **第二步改了什么**：`maybe_run_canary` 和 `dispatch` 现在读租约，不再读旧 bool。
#    旧 bool 仍然维护（最后一步才删），`compare_busy` 继续对答案 —— 但含义反过来了：
#    **现在分歧意味着旧 bool 错了**，那正是可以删掉它的信号。
#
# ⚠️⚠️ **心跳不能省。** 活动租约有 TTL，而一次 GUI 自动化可能跑几分钟。
#    观测期不续期只是记成假阳性；**权威切换之后不续期会让 Nano 自己把机器丢掉** ——
#    租约一过期，`current_activity()` 立刻不认它，canary 就会在 GUI 任务跑到
#    一半时跳出来抢前台焦点。代价从"报告不准"升级成"真的干扰任务"。
# ══════════════════════════════════════════════════════════════════════════

SHADOW_STAGE = "5A"


def shadow_note(path_tag: str, detail: str = "", diverged: bool = False) -> None:
    """记一条 5A 观测。失败静默 —— 观测手段不许反过来影响主流程。"""
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime.toolbatch import OBSERVE
        get_kernel().submit(Command(
            kind=OBSERVE,
            payload={"stage": SHADOW_STAGE, "path_tag": path_tag,
                     "diverged": diverged, "detail": detail[:500]}))
        if diverged:
            logger.warning(f"[Shadow] ⚠️ 分歧 [{path_tag}] {detail}")
    except Exception as e:
        logger.debug(f"[Shadow] 记录观测失败（忽略）: {e}")


# GUI 自动化可能跑很久，给足余量 —— 到期才有"确实卡住了"的含义。
# ⚠️ 这不是"允许 Nano 占 5 分钟"，是"5 分钟没心跳就当它死了"。
#    正常任务每一步都续期（见 `_execute_dsl_step`），根本碰不到这个上限。
OS_ACTIVITY_TTL_SEC = 300.0


def acquire_activity(reason: str = "") -> Optional[tuple[str, int]]:
    """Nano 开始操作电脑前拿活动租约。返回 `(lease_id, fence)`；**拿不到返回 None**。

    ⚠️⚠️ **权威切换时在这里去掉了 `preempt=True`，这是那一步最要紧的一处改动。**

    观测期它必须带 `preempt=True`，因为旧 `_os_task_busy` 有两处置位点、
    没有任何互斥，镜像若坚持互斥就会**抛在主流程里** —— 等于"为了观察这件事
    把这件事弄坏了"。观测期的规矩是如实反映旧行为，不是纠正它。

    **切成权威之后反过来：互斥必须真的生效。** 留着 `preempt=True` 的话，
    用户刚把电脑拿回去（`Holder.USER`），Nano 下一个 OS 动作就**一声不响地抢回来** ——
    那正是被动挂起要消灭的行为。⭐ 抢占权只属于用户，不属于 Nano。

    📌 **拿不到不是异常，是正常运行的一部分**（同 `LeasePreempted` 的语义），
    所以这里返回 `None` 而不是抛 —— 调用方据此**安静停手**。
    """
    try:
        from core.runtime.kernel import get_kernel
        r = get_kernel().submit(Command(kind=ACQUIRE, payload={
            "holder": Holder.NANO, "reason": reason or "os activity",
            "ttl_sec": OS_ACTIVITY_TTL_SEC,
        })).data
        return r["lease_id"], r["fence"]
    except KernelError as e:
        # 互斥挡住了 —— 机器在别人（多半是用户）手里
        logger.info(f"[OSLease] Nano 拿不到活动租约，本次不动手: {e}")
        return None
    except Exception as e:
        logger.warning(f"[OSLease] 拿活动租约异常: {e}")
        return None


# ⚠️ `_mirror_preempted` 已删除（2026-08-08）。它是观测期「对答案」用的 ——
#    用来把「用户接管的后果」和「泄漏 / 接线漏了」分开记，避免假阳性。
#    对答案退役后它就没有读者了。


#: 轮内等待的上限。⚠️ 这**不是**"用户最多能占多久"（用户想占多久都行），
#: 是"一轮 turn 最多为此挂多久"。到点就体面收场，把控制权还给用户。
#: ⚠️ 调度器落地后，这个分支应改成「登记一个 continuation」而不是收场。
AWAIT_MACHINE_CAP_SEC = 180.0
AWAIT_MACHINE_POLL_SEC = 1.0


#: Nano 此刻是不是**真的停住在等** —— 区别于"还在收手"（不可阻断的动作没做完）。
#:
#: ⭐ **由 `await_activity_lease` 自己维护，不要求任何调用方配合。**
#:    📌 这是本轮反复栽的那个坑的反面：`_os_task_busy` 靠所有调用点记得配对、
#:    早期的 `reconcile_tick` 压根没人调 —— **让做那件事的函数自己记录状态，
#:    比要求每个调用方都记得设标志可靠得多。**
#:
#: ⚠️ 它是**给 UI 用的**（接管状态条要分"已让出控制/我这步做完就停"和"已暂停控制"），
#:    不是给决策用的 —— 决策一律读租约。
_parked: bool = False


def is_parked() -> bool:
    """Nano 是不是正停在等待里。见 `_parked` 的说明。"""
    return _parked


async def await_activity_lease(reason: str = "",
                               cap_sec: float = AWAIT_MACHINE_CAP_SEC,
                               poll_sec: float = AWAIT_MACHINE_POLL_SEC):
    """**在这一轮里等**，直到机器空出来并拿到活动租约。

    返回 `(handle, waited_sec)`；等到上限还没拿到则 `handle is None`。

    ═══ 为什么必须是"等"，不是"失败" ═══
    实测三轮后发现（这是本项目里代价最大的一个形态错误）：

    > 没有被动挂起的情况下，一旦被用户操作打断，**Nano 的 turn 就彻底结束了**。
    > 有了被动挂起是**全流程自动**的：用户占用 → 瞬间挂起并告知 →
    > 用户彻底停手 → **Nano 自动从挂起中恢复** → 继续。
    > **整个都没脱离这轮 turn。**

    原先做的是"闸"：拿不到 → 动作失败 → 模型只能"别重试" → **结束这一轮**。
    于是用户每发一次「继续」都是新的一轮，进来立刻撞墙、再结束 ——
    表现为"永久锁死"。
    📌 **判据：「闸」和「挂起」在代码里长得像，行为相反。**
    **闸的出口是失败，挂起的出口是等待再继续。**
    **一个只有失败出口的机制，最终一定把成本转嫁给用户去手动重试。**

    ⚠️ **用 `acquire` 本身当测试，不要"先查 `nano_may_touch_os()` 再拿"** ——
    那两步之间用户完全可以再动一次手，于是查到"可以"、拿的时候却被抢。
    📌 同 `projection` 那条契约的另一面：**判断和动作之间的任何间隙都是竞态**，
    能合并成一次原子操作就别拆开。

    ⚠️ 等待期间**不做任何 OS 动作**，只是 `asyncio.sleep` —— 没有 LLM 调用、
    没有 token 成本。所以"等 3 分钟"对预算是免费的，只占一个挂着的 turn。
    """
    global _parked
    import asyncio
    import time as _t
    t0 = _t.time()
    try:
        while True:
            h = acquire_activity(reason)
            if h is not None:
                return h, _t.time() - t0
            # ⚠️ 只有**确认拿不到之后**才算 parked：第一次就拿到的话
            #    Nano 压根没停过，不该让 UI 说"已暂停控制"。
            _parked = True
            if _t.time() - t0 >= cap_sec:
                logger.info(f"[OSLease] 等了 {cap_sec:.0f}s 机器仍不空，本轮放弃等待")
                return None, _t.time() - t0
            await asyncio.sleep(poll_sec)
    finally:
        # ⭐ `finally` 在这里是安全的：`_parked` 只被 UI 读来选文案，
        #   不参与任何决策 —— 对比 `_active_tool_batch_open` 那个**刻意不能**用
        #   finally 的标志（它要在外层错误处理读到之后才清）。
        #   📌 同样是"标志要不要在 finally 里复位"，答案取决于**它被谁在什么时候读**。
        _parked = False


def heartbeat_activity(handle: Optional[tuple[str, int]]) -> None:
    """OS 每走一步续一次期。**不续就会把正常的长任务记成分歧。**

    ⭐ 这里同时是**用户接管的探测点**：续期时发现自己那条租约已经不是 HELD，
    就说明这中间有人（`Holder.USER`）把机器拿走了。**只记一条观测，不改行为** ——
    真正的停手由两道闸和轮内 await 负责。
    """
    if not handle:
        return
    try:
        from core.runtime.kernel import get_kernel
        get_kernel().submit(Command(kind=HEARTBEAT, payload={
            "lease_id": handle[0], "fence": handle[1],
            "ttl_sec": OS_ACTIVITY_TTL_SEC}))
    except LeasePreempted:
        shadow_note("mirror_preempted", f"{handle[0]} 的 fence 已失效 → 有人接管了机器")
    except Exception as e:
        # ⚠️ 抢占实际走的是「状态已不是 HELD」那条路（`_close` 只 bump revision、
        #    **不动 fence**），所以它多半落在这里而不是上面的 `LeasePreempted` ——
        #    **别靠异常类型认，靠文案认。**
        if "不能续期" in str(e):
            shadow_note("mirror_preempted", f"{handle[0]} 已不是 HELD → 有人接管了机器")
        else:
            logger.debug(f"[OSLease] 心跳失败（忽略）: {e}")


def release_activity(handle: Optional[tuple[str, int]]) -> None:
    """归还活动租约。幂等；fence 不匹配时安静忽略（被抢占是正常运行的一部分）。"""
    if not handle:
        return
    try:
        from core.runtime.kernel import get_kernel
        get_kernel().submit(Command(kind=RELEASE, payload={
            "lease_id": handle[0], "fence": handle[1]}))
    except Exception as e:
        logger.debug(f"[OSLease] 归还租约失败（忽略）: {e}")


# ⚠️ `compare_busy` 已删除（2026-08-08）。
#
# 它在观测期的任务已经完成：**量出了那个从没人数过的泄漏**
# —— 按一下 Escape 之后，canary 读点 16 次观测 13 次判为泄漏、持续 61 分钟。
# 而 ③b 把租约改成**一整段 GUI 操作**的粒度后，旧 bool 仍是**单步**粒度：
# 📌 **两个寿命不同的东西之间的「分歧」不是缺陷，是设计。**
# 📌 **shadow 的对答案，只在两边建模同一个粒度时才有意义** ——
#    一旦新实现有意做得比旧的更细/更粗，旧的就不再是 oracle，该退役了。
#
# ⚠️ `shadow_*_temp_auto` 那三个**留着** —— `_temp_auto` 仍是运行时权威，
#    那条 shadow 还活着（④ 的另一半）。
#    📌 删的标准是「**还有没有人读它**」，不是「名字里有没有 shadow」。
# ══════════════════════════════════════════════════════════════════════════
# GUI 自动化任务 —— **第一个真实的 Task**
# ══════════════════════════════════════════════════════════════════════════
# ⭐⭐⭐ **为什么从这一类开始接线，而不是从「对话」开始。**
#
# `tasks` 表在真库里长期是 **0 行**：状态机、命令、不变量、启动恢复全都有，
# 但**生产侧从来没有人建过一个**。而「一件事」这个抽象要立起来，
# 第一个真实用例的**边界必须清楚**。
#
# 📌 **给一个新实体接线，要从「边界最清楚」的那一类开始，
#    不是从「最常见」的那一类开始。**
#    最常见的那类（对话）边界最模糊（一句话算一件事？一个话题算一件事？），
#    而**模糊的边界会让这个实体从第一天就不可信** ——
#    之后所有 `owner_task_id` 都继承那份不可信。
#
# ⭐ GUI 自动化任务的边界是**零歧义**的：`open_gui_session` 开始、
#    `close_gui_session` 结束，而这两个事件**已经是权威租约**，
#    **不需要模型判断**。
#
# ⭐⭐ 它同时兑现欠着的那一条：
#    「`_temp_auto` 由『绑 mini 窗』改为**显式绑 Task**」。
#    于是「**mini 窗关 = 授权失效**」这个**用 UI 形态当授权作用域锚点**的
#    方向错误，被换成「**Task 结束 = 授权失效**」。
#
# ⚠️⚠️ **但 `owner_task_id` 只是「归属标签」，不是「生命周期主宰」。**
#    每一类被拥有的东西**仍然保留自己独立的失效条件**
#    （活动租约有 TTL、wait_condition 有 deadline、GUI 会话靠启动收尾）。
#    📌 理由：对话类 Task 的边界最终要靠模型判断，而**模型会判错** ——
#       让 `owner_task_id` 决定谁该死，就等于把「分组判错」升级成「租约泄漏」。
#       这样一来，**边界判错的代价被限制在「分组不好看」**。
#    ⭐ 这是把已有纪律延伸：**过期是推导出来的，不依赖任何人记得收。**

GUI_TASK_KIND = "gui"          # = task.TaskKind.GUI_AUTOMATION


def current_gui_task_id() -> Optional[str]:
    """当前那个 GUI 自动化 Task 的 id（没有就 None）。

    ⚠️ **从 `os.gui_session` 那条租约上读，不另存一份。**
       📌 一个「谁拥有它」的答案只该有一个来源 —— 再存一份内存副本，
          就又造出一个「两个东西都以为自己知道」的局面（本轮栽过的那类）。
    """
    try:
        k = _get_kernel()
        with k.store.read() as conn:
            r = conn.execute(
                "SELECT owner_task_id FROM os_leases "
                "WHERE kind=? AND status='HELD' AND scope=? LIMIT 1",
                (LeaseKind.AUTHORIZATION, GUI_SESSION_SCOPE)).fetchone()
        return (r["owner_task_id"] or None) if r else None
    except Exception as e:
        logger.debug(f"[Task] 读当前 GUI Task 失败: {e}")
        return None


def _create_gui_task(reason: str) -> Optional[str]:
    """建一个 GUI 自动化 Task。失败返回 None（**不阻断缩窗**）。

    ⚠️ 吞异常的方向：建不出 Task 就退化成「没有 Task」——
       GUI 模式本身照旧工作（它的权威是那条租约，不是 Task）。
       📌 **接线一个新实体时，它的失败不该让已经能用的东西停摆。**
    """
    try:
        from core.runtime import task as _tk
        return _get_kernel().submit(Command(kind=_tk.CREATE, payload={
            "kind": _tk.TaskKind.GUI_AUTOMATION,
            # ⭐ GUI Task **永远是前台** —— 见下面那条硬约束。
            "placement": _tk.Placement.FOREGROUND,
            "goal_summary": (reason or "操作用户的屏幕")[:200],
        })).data["task_id"]
    except Exception as e:
        logger.warning(f"[Task] 建 GUI Task 失败（GUI 模式照旧工作）: {e}")
        return None


def _finish_gui_task(task_id: Optional[str], reason: str = "") -> None:
    """GUI 任务结束 → 收成终态。幂等。"""
    if not task_id:
        return
    try:
        from core.runtime import task as _tk
        _get_kernel().submit(Command(kind=_tk.TERMINATE, subject_id=task_id, payload={
            "task_id": task_id,
            # ⚠️ 键名是 `reason`（`terminal_reason` 会被静默忽略走默认值）
            "reason": _tk.TerminalReason.COMPLETED,
            "note": reason or "mini 窗还原，GUI 任务结束",
        }))
        logger.info(f"[Task] GUI Task {task_id} 已收尾")
    except Exception as e:
        # ⚠️ 收不掉只是留一条 ACTIVE 的记录 —— **不影响任何失效条件**
        #    （租约照旧被 `close_gui_session` 撤、启动收尾照旧兜底）。
        #    这正是「归属标签不当生命周期主宰」买来的容错。
        logger.warning(f"[Task] 收尾 GUI Task {task_id} 失败（不影响租约）: {e}")


def open_gui_session(reason: str = "") -> Optional[str]:
    """进入 GUI 模式（mini 窗打开时调）。幂等：已经开着就返回现有那条。

    ⚠️ **不设 TTL**：一段 GUI 任务可以很长，而它的结束条件是"mini 窗关掉"这个
    **明确事件**，不是超时。这与活动租约相反（那个必须有 `held_until`）——
    📌 两种租约的规则本来就相反，这正是当初不许合并它们的理由。
    """
    try:
        from core.runtime.kernel import get_kernel
        k = get_kernel()
        if is_authorized(k, GUI_SESSION_SCOPE):
            return None            # 已经在 GUI 模式，不重复开
        # ⭐⭐ 先建那个 Task —— 它就是「这一次操作屏幕」这件事。
        _tid = _create_gui_task(reason)
        lid = k.submit(Command(kind=GRANT, payload={
            "scope": GUI_SESSION_SCOPE, "ttl_sec": None,
            "reason": reason or "GUI 任务进行中（mini 窗已开）",
            "owner_task_id": _tid,
        })).data["lease_id"]
        logger.info(f"[OSLease] 进入 GUI 模式 {lid}（Task={_tid}）"
                    f"—— 被动挂起从此全量监控")
        return lid
    except Exception as e:
        logger.warning(f"[OSLease] 开 GUI 模式失败: {e}")
        return None


def close_gui_session() -> None:
    """退出 GUI 模式（mini 窗关闭时调）。幂等。

    ⭐ 顺带把那个 GUI Task 收成终态。
    ⚠️ **必须先读 task_id 再撤租约** —— 撤完就读不到了（它挂在租约上）。
       📌 一个「顺带收尾」的动作，取信息的顺序不能晚于毁掉信息的动作。
    """
    try:
        from core.runtime.kernel import get_kernel
        _tid = current_gui_task_id()
        get_kernel().submit(Command(kind=REVOKE,
                                    payload={"scope": GUI_SESSION_SCOPE}))
        _finish_gui_task(_tid)
        logger.info("[OSLease] 退出 GUI 模式 —— 被动挂起停止监控")
    except Exception as e:
        logger.warning(f"[OSLease] 关 GUI 模式失败: {e}")


# ══════════════════════════════════════════════════════════════════════════
# 本次任务临时免确认 —— 权威从裸 bool 换成授权租约
# ══════════════════════════════════════════════════════════════════════════
# 它换掉的是 `app.py` 上的 `self._temp_auto`。旧实现的问题**不是"这个 bool 会写错"**，
# 而是：
#   🔴 **它的失效条件写在 UI 里** —— `任何 mini 窗关 → _temp_auto = False`。
#      一条"用户还授权着吗"的判断，答案取决于**某个窗口开着没有**。
#      📌 那个被点名的方向错误：**用 UI 形态当授权作用域的锚点。**
#   🔴 **它没有主人**：谁给的、给了什么范围、什么时候该收，一个字段答不了。
#
# ⚠️⚠️ **和 `os.gui_session` 寿命常常相同，但绝不许合并。** 它们答两个不同的问题：
#   · `os.gui_session`：**Nano 现在在做 GUI 任务吗**（被动挂起的作用域）
#   · `os.temp_auto`：**用户许可了免确认吗**（OS 确认弹不弹）
# ⭐ 而且**今天就已经分岔**：`_global_auto` 开着时，`mini_auth_request` 走
#    "直接缩窗、不弹授权"那条路 —— 于是 `os.gui_session` 开着而 `os.temp_auto` 没有。
#    `window_mode: mini` 那条路同理。
# 📌 与「`CONSEQUENTIAL` vs `TAKEOVER_TRIGGERS`」「活动租约 vs 授权租约」同一个判据：
#    **寿命恰好相同不是合并的理由；答的是不是同一个问题才是。**
TEMP_AUTO_SCOPE = "os.temp_auto"


def _get_kernel() -> RuntimeKernel:
    """⚠️ 内联 import —— 与本模块其余函数一致。`get_kernel` 是个单例取值器，
    模块级 import 会在 import 期固化一个尚未初始化的引用。"""
    from core.runtime.kernel import get_kernel
    return get_kernel()


def temp_auto_authorized(kernel: Optional[RuntimeKernel] = None) -> bool:
    """用户是否授权了「本次任务免逐个确认」。

    ⚠️⚠️ **fail-safe 方向是「没授权」。** 读不出来就当没授权 → 照常弹确认。
       反过来错的代价是**在用户没批准的情况下自动执行有副作用的 OS 动作** ——
       📌 与 `nano_may_touch_os()` 同一个方向判据：
          **fail-safe 要朝「多问一句」错，不朝「多做一步」错。**
    """
    try:
        k = kernel if kernel is not None else _get_kernel()
        return is_authorized(k, TEMP_AUTO_SCOPE)
    except Exception as e:
        logger.debug(f"[OSLease] 读临时授权失败，按未授权处理: {e}")
        return False


def grant_temp_auto(reason: str = "") -> Optional[str]:
    """用户在 mini 授权弹窗里点了"同意"。幂等：已授权就不重复发。

    ⚠️ **无期限**（`ttl_sec=None`）—— 一段 GUI 任务可以很长，它的结束条件是
    **明确事件**（mini 窗关 / 任务结束），不是超时。同 `os.gui_session`。
    ⭐ 「绑到 Task」**已兑现**（见下面 `current_gui_task_id()`）。
    ⚠️ 这里原来写着「等 Task 真正接线再填」—— 那句话在下面这段代码
       落地的同一刻就过期了，而**它就贴在做完那件事的代码上面**。
       📌 **一条「以后再做」的注释，必须和做它的那次改动一起删** ——
          否则它会让下一个人以为这里还是个缺口，
          而这正是最难发现的那种半遗忘：**事做完了，说它没做的那句话还在。**
    """
    try:
        k = _get_kernel()
        if is_authorized(k, TEMP_AUTO_SCOPE):
            return None
        # ⭐⭐⭐ **「绑到 Task」的兑现**：绑到那个 GUI Task 上。
        #    原文：「`_temp_auto` 由『绑 mini 窗』改为**显式绑 Task**」——
        #    于是「mini 窗关 = 授权失效」这个**用 UI 形态当授权作用域锚点**的
        #    方向错误，换成了「**Task 结束 = 授权失效**」。
        #    ⚠️ 但它仍然由 `revoke_temp_auto` / 启动收尾**独立**失效 ——
        #       📌 归属标签不当生命周期主宰（见本节头部那段）。
        _tid = current_gui_task_id()
        lid = k.submit(Command(kind=GRANT, payload={
            "scope": TEMP_AUTO_SCOPE, "ttl_sec": None,
            "reason": reason or "本次任务临时免确认（用户在 mini 授权弹窗里同意）",
            "owner_task_id": _tid,
        })).data["lease_id"]
        logger.info(f"[OSLease] 本次任务免确认授权 {lid}（Task={_tid}）")
        return lid
    except Exception as e:
        # ⚠️ 这里吞异常的代价是"授权没发出去" → `temp_auto_authorized()` 返回 False
        #    → 照常弹确认。**朝安全方向退化**，所以可以吞。
        logger.warning(f"[OSLease] 发临时授权失败（将照常弹确认）: {e}")
        return None


def revoke_temp_auto() -> None:
    """mini 窗关 = 本次任务结束 = 授权收回。幂等。

    ⚠️ **只在真收掉了东西时才留痕。** 无条件打"已收回"的话，
    `_global_auto` 那条路（缩窗但从不发这条授权）每次关 mini 都会打一行 ——
    📌 **一条在什么都没做时也说自己做了的日志，比没有日志更糟**：
       排查时要靠日志还原行为，而这种行会让"授权到底发过没有"变得不可读。
    """
    try:
        k = _get_kernel()
        had = is_authorized(k, TEMP_AUTO_SCOPE)
        k.submit(Command(kind=REVOKE, payload={"scope": TEMP_AUTO_SCOPE}))
        if had:
            logger.info("[OSLease] 本次任务免确认授权已收回")
    except Exception as e:
        # 🔴 这一条吞掉的代价方向**相反**：收不回来 = 授权继续有效 = 不该免确认时免了。
        #    所以它必须 `warning` 级别留痕，不能像 grant 那样安静吞。
        logger.warning(f"[OSLease] 收回临时授权失败（授权可能仍有效！）: {e}")


def expire_tick(kernel: RuntimeKernel, now: float | None = None) -> int:
    """到期回收。level-triggered，每跳重算。"""
    t = kernel.now() if now is None else now
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT lease_id, kind, holder FROM os_leases WHERE status='HELD' "
            "AND held_until IS NOT NULL AND held_until <= ?", (t,)).fetchall()
    n = 0
    for r in rows:
        try:
            kernel.submit(Command(kind=EXPIRE, subject_id=r["lease_id"],
                                  payload={"lease_id": r["lease_id"]}))
            n += 1
            if r["kind"] == LeaseKind.ACTIVITY:
                # ⚠️ 活动租约走到过期，说明持有者没能收尾 —— 响亮
                logger.warning(
                    f"[OSLease] 活动租约 {r['lease_id']}（{r['holder']}）到期回收。"
                    f"正常路径应当主动归还，走到这里说明有东西没收好。")
        except Exception as e:      # pragma: no cover
            logger.error(f"[OSLease] 回收 {r['lease_id']} 失败: {e}")
    return n


def startup_release_all(kernel: RuntimeKernel) -> int:
    """启动时释放所有**活动**租约。

    ⚠️ 活动租约的持有者是"上一个进程里那段正在点鼠标的代码"，它已经不存在了，
    所以租约必须还 —— 否则新进程永远拿不到，表现为"Nano 再也不肯操作电脑"。

    ⚠️ **授权租约不动**：那是用户给的许可，不随进程生死。
    （这与 `waitcond.startup_sweep` 只收 background 是同一条判据：
      **收什么取决于"它的执行体还在不在"，不是"它是不是一条记录"**。）
    """
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT lease_id FROM os_leases WHERE kind=? AND status='HELD'",
            (LeaseKind.ACTIVITY,)).fetchall()
    n = 0
    for r in rows:
        try:
            kernel.submit(Command(
                kind=EXPIRE, subject_id=r["lease_id"],
                payload={"lease_id": r["lease_id"]}))
            n += 1
        except Exception as e:      # pragma: no cover
            logger.error(f"[OSLease] 启动释放 {r['lease_id']} 失败: {e}")
    # ⭐⭐ 有**两条**授权租约必须在启动时收掉：`os.gui_session` 和 `os.temp_auto`。
    #
    # ⚠️ 看着像破例（上面刚说"授权租约不动"），其实是**严格遵守那条判据**：
    #    「**收什么取决于「它的执行体还在不在」，不是「它是不是一条记录」**」。
    #    · `os.gui_session` 是**"Nano 正在做一个 GUI 任务"这个事实** ——
    #      执行体就是那个任务，进程一死它就不存在了
    #    · `os.temp_auto` 授权的是**那一个 GUI 任务里的免确认** ——
    #      执行体同样是那个任务。任务没了，"本次任务免确认"就无所指。
    #
    # ⚠️⚠️ **这里更正一处写错的判断（原注释和早先的设计都说错了）**：
    #    原文写的是「`os.temp_auto` 这类是用户给的许可 —— 执行体是用户的意愿，
    #    进程死了它还在」。**那条错了**，三个理由：
    #    ① **行为不符**：旧实现 `_temp_auto` 是内存 bool，重启后本来就没了。
    #       让它跨重启存活是**改语义**，不是迁移。
    #       📌 **切权威那一步只切权威，不改行为** —— 顺手改了语义，出问题时你分不清
    #          是"切错了"还是"新语义不对"。
    #    ② **按那条判据自己算，答案就是"收"**：它许可的是"这一个 GUI 任务"的
    #       免确认，而那个任务随进程消失。
    #    ③ 🔴 **不收的后果是安全方向的**：重启后 Nano **静默地**带着免确认授权，
    #       而用户早就忘了自己批准过什么。这比 GUI 模式泄漏更糟 ——
    #       后者让 Nano 干不了活（吵闹的失败），前者让它**不打招呼就动手**。
    #    ⚠️ 等授权也绑到 Task 之后可以重新讨论：那时候"任务还在不在"
    #       有权威答案，可以由被恢复的 Task 重新授予。
    # 🔴 GUI 模式不收的后果也很重：留一条永远开着的 GUI 模式 → 被动挂起
    #    **全量监控永久生效** → 用户平时点任何东西都在抢租约 → Nano 再也拿不到机器。
    #    📌 **它们被存成 authorization 只是因为生命周期规则合适（无 TTL、可并存），
    #       语义上是「会话/模式」和「本次任务的许可」，都不是「长期许可」**
    #       —— 别被存储位置带跑。
    for _scope, _what in ((GUI_SESSION_SCOPE, "GUI 模式"),
                          (TEMP_AUTO_SCOPE, "本次任务免确认授权")):
        try:
            with kernel.store.read() as conn:
                g = conn.execute(
                    "SELECT lease_id FROM os_leases "
                    "WHERE kind=? AND status='HELD' AND scope=?",
                    (LeaseKind.AUTHORIZATION, _scope)).fetchall()
            if g:
                kernel.submit(Command(kind=REVOKE, payload={"scope": _scope}))
                logger.info(f"[OSLease] 启动清掉 {len(g)} 条遗留的{_what}"
                            f"（那个 GUI 任务已随上个进程消失）")
                n += len(g)
        except Exception as e:      # pragma: no cover
            logger.error(f"[OSLease] 启动清{_what}失败: {e}")
    if n:
        logger.info(f"[OSLease] 启动共释放 {n} 条（持有者已随上个进程消失）")
    return n
