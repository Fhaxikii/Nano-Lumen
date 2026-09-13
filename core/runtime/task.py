# core/runtime/task.py
"""
Thin Task Spine —— Nano 没有 session，用 Task 作为状态归属单位。

═══ 为什么第一版只做"脊柱" ═══

一个 Task 可以跨多个 turn、被挂起、被丢后台、被新指令插队、拥有自己的
子资源与生命周期。已确认由它界定生命周期的东西有十类（迭代阅读的 scratchpad / 仅本次 MCP /
临时执行通道产物 / OS 预授权 scope / temp_auto 与 mini 窗 / [临时] 附件 / 挂起归属 /
Subagent / running tasks UI / per-task 成本记账）。

但**这一版刻意不做任务智能编排**（继续/插队/后台化/派 Subagent 的决策是另一件事）。
现在只给下游一根可以挂 `owner_task_id` 的脊柱。

═══ 正交区域，不是一个 status ═══

    lifecycle : ACTIVE | TERMINAL
    placement : FOREGROUND | BACKGROUND
    execution : IDLE | RUNNING | PAUSED

三个正交才能表达"前台等授权""后台跑但不阻塞聊天""因用户介入而暂停""被插队但未取消"。
塞进一个枚举必然长成 `BACKGROUND_WITH_PENDING_APPROVAL_AND_PASSIVE_PAUSE`。

═══ ⭐ blockers 是派生的，不是字段 ═══

`tasks` 表**刻意没有 blockers 列**。Approval 与 Wait 通过 `owner_task_id` 指向 Task，
Task 再存一份就是三处双权威，会出现"Approval 已取消但 Task.blockers 仍含它"。

但 Approval / Wait 的表是后来才建的。所以这里做成**注册式 blocker provider**：
建完 Approval 表就 `register_blocker_provider(...)` 插进来，`task.py` 一行不用改。
这样派生路径**现在就存在且被测试覆盖**，不是留个 TODO 等以后补。
（同 `health.py` 的 `register_probe`：运行时登记，避免 task ← approval ← task 的循环依赖。）

═══ 不变量 18：终态不可逆 ═══

TERMINAL 不许回 ACTIVE。要继续就新建 Task 挂 `parent_task_id`。
否则历史、成本记账、副作用归属会混乱——尤其 per-task 成本记账是 NCOA
"按总执行代价择优"的量化基础 —— 一个能反复复活的 Task 记不出账。
"""
from __future__ import annotations

import time

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

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
# 枚举（裸字符串会拼错，一律走这里）
# ══════════════════════════════════════════════════════════════════════════

class Lifecycle:
    ACTIVE = "ACTIVE"
    TERMINAL = "TERMINAL"
    _ALL = frozenset({ACTIVE, TERMINAL})


class Placement:
    FOREGROUND = "FOREGROUND"
    BACKGROUND = "BACKGROUND"
    _ALL = frozenset({FOREGROUND, BACKGROUND})


class Execution:
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    _ALL = frozenset({IDLE, RUNNING, PAUSED})


# ══════════════════════════════════════════════════════════════════════════
# execution slots —— 前台 1 + 后台 N
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ **前台那个 1 不是配置项，是定义**：一个用户一次只在跟一件事对话。
#    所以它是常量而不是设置项 —— 把它做成可配的，等于允许一种没有意义的状态。
MAX_FOREGROUND_RUNNING = 1

# 🔬 **后台并发上限 —— 这个数字是有理由的估计，不是实测出来的，需要标定。**
#    为什么不是「越大越好」：后台任务**每完成一个就唤醒一次模型**
#    （`notify_background_done` → 新 turn）。所以 N 不只是内存/句柄上限，
#    **它是一个成本乘数** —— 同时跑 20 个下载，完成时就是 20 次模型调用。
#    ⚠️ 取 3 的依据：① 现实里同时需要的后台事很少（装个库 + 下个模型 + 跑个脚本）；
#       ② 比它大的时候，用户已经不可能在界面上跟得住了（`x running task(s)`
#          抽屉一屏就那么高）；③ 超了不失败、只是排队，所以取小的代价很轻。
#    📌 **一个「上限」的取值理由，必须说清「取小了会怎样」** ——
#       这里取小只是晚一点跑，取大则是把成本闸让开一个口子。
#    ⚠️ 这个数是初始值，还需要按实际体验标定（与 `USER_HOLD_SEC = 20` 同类）。
MAX_BACKGROUND_RUNNING = 3


class TaskKind:
    """Task 的种类。"GUI Task 不可后台化"这条硬约束要靠它判。"""
    CONVERSATION = "conversation"   # 普通用户驱动的一轮对话
    GUI_AUTOMATION = "gui"          # 操作屏幕（不可后台化）
    BACKGROUND_JOB = "background"    # 后台长任务
    AGENT = "agent"                 # Subagent
    _ALL = frozenset({CONVERSATION, GUI_AUTOMATION, BACKGROUND_JOB, AGENT})


class TerminalReason:
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"
    EXPIRED = "EXPIRED"


# ══════════════════════════════════════════════════════════════════════════
# 记录与视图
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TaskRecord:
    """`tasks` 表一行的直读映射。**不含 blockers** —— 见模块头。"""
    task_id: str
    kind: str
    lifecycle: str
    placement: str
    execution: str
    goal_summary: str = ""
    parent_task_id: Optional[str] = None
    current_turn_id: Optional[str] = None
    terminal_reason: Optional[str] = None
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle == Lifecycle.TERMINAL

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRecord":
        return cls(
            task_id=row["task_id"], kind=row["kind"],
            lifecycle=row["lifecycle"], placement=row["placement"],
            execution=row["execution"], goal_summary=row["goal_summary"] or "",
            parent_task_id=row["parent_task_id"], current_turn_id=row["current_turn_id"],
            terminal_reason=row["terminal_reason"], revision=int(row["revision"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )


@dataclass(frozen=True)
class Blocker:
    """一个正在挡住 Task 的东西。由 provider 产出，Task 本体不存它。"""
    blocker_kind: str       # "approval" | "wait" | ...（由各自的模块定义）
    blocker_id: str
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskView:
    """给 Projection / 模型上下文用的派生视图。**blockers 在这里，不在 TaskRecord。**"""
    record: TaskRecord
    blockers: tuple[Blocker, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return bool(self.blockers)

    @property
    def is_ready(self) -> bool:
        """可以尝试推进：活着、没被挡、**当前是 IDLE**。
        ⚠️ 这是 Reconciler 的判据来源，**level-triggered**——
        它只看当前状态，不依赖任何"有没有收到通知"。

        ⚠️⚠️ **2026-08-07 修正：从 `!= RUNNING` 改成 `== IDLE`。**

        原写法把 `PAUSED` 也算 ready（`PAUSED != RUNNING`）。现在没炸，
        **只因为真正的 Task 调度器还没接起来**（生产里 0 个 Task）。
        一旦后台任务体系接上，它会和被动挂起**正面冲突**：

            用户动手 → Task 转 PAUSED（不许动电脑）
            → Reconciler：不是 RUNNING、又没 blocker → READY
            → 把 Nano 又叫起来 → 继续抢用户的鼠标

        📌 **`PAUSED` 的语义就是"明确不许调度"**，它不是"暂时没在跑"。
        把"不在跑"和"不许跑"压进同一个判断，就是今天反复栽的那个形状：
        **一个表达式同时承担两个不同的现实。**

        （回代码核实成立。它还没炸不等于它是对的 ——
        这类"等基建接上才会显形"的错，正是趁现在改最便宜。）
        """
        r = self.record
        return (r.lifecycle == Lifecycle.ACTIVE
                and r.execution == Execution.IDLE
                and not self.blockers)


# ══════════════════════════════════════════════════════════════════════════
# blocker provider 注册（Approval / Wait 的插入点）
# ══════════════════════════════════════════════════════════════════════════

# (conn, task_id) -> list[Blocker]
BlockerProvider = Callable[[sqlite3.Connection, str], list[Blocker]]

_blocker_providers: dict[str, BlockerProvider] = {}


def register_blocker_provider(name: str, fn: BlockerProvider) -> None:
    """Approval / WaitCondition 在自己的 install() 里调这个。
    重复注册直接覆盖是允许的（同名 = 同一个领域重新接线），与 kernel.register 不同。"""
    _blocker_providers[name] = fn


def clear_blocker_providers_for_tests() -> None:
    _blocker_providers.clear()


# ══════════════════════════════════════════════════════════════════════════
# activity provider —— 「这件事最近一次动静是什么时候」
# ══════════════════════════════════════════════════════════════════════════
# ⚠️⚠️ **为什么不能只看 `tasks.updated_at`（这是被测试判红逼出来的第三版）**：
#    关掉一条等待 **不会 touch `tasks` 那一行**（等待自己带 `owner_task_id`，
#    收它只动 `wait_conditions`）。于是：
#
#      10:00 建 Task + 挂一条 3 小时的定时      tasks.updated_at = 10:00
#      13:00 定时到点、等待被收                 tasks.updated_at **还是 10:00**
#      13:00 tick 扫到：没有 blocker + 已过 TTL → **当场收掉**
#
#    而 13:00 正是 Nano **刚被唤醒、准备接着做那件事**的时刻 ——
#    收掉它等于在最不该的一刻把「你正在做什么」从模型眼前拿走。
# 📌 **一个「多久没动静」的判断，量的必须是【这件事】的动静，
#    不是【那一行记录】的动静。** 子树里的活动也是活动。
#
# ⭐ 用 provider 注册（同 `register_blocker_provider` / `health.register_probe`）：
#    `task.py` 不许知道 `wait_conditions` / `interactions` 这些表的存在，
#    否则脊柱就反向依赖了挂在它上面的东西。

# (conn, task_id) -> 最近一次动静的时间戳（没有就 0.0）
ActivityProvider = Callable[[sqlite3.Connection, str], float]

_activity_providers: dict[str, ActivityProvider] = {}


def register_activity_provider(name: str, fn: ActivityProvider) -> None:
    """挂在 Task 上的领域（waitcond / interaction / …）在自己的 install() 里调。"""
    _activity_providers[name] = fn


def clear_activity_providers_for_tests() -> None:
    _activity_providers.clear()


def last_activity_at(kernel: RuntimeKernel, rec: "TaskRecord") -> float:
    """这件事最近一次动静的时间戳 —— 取「记录本身」与「子树」里最晚的那个。

    ⚠️ provider 抛异常时**当成「刚刚动过」**（返回 now），不是当成 0 ——
       📌 读不出动静时要**偏向不回收**：多留一条空壳只是噪音，
          错收一条正在用的会把上下文从模型眼前拿走。
    """
    newest = float(rec.updated_at or 0.0)
    if not _activity_providers:
        return newest
    with kernel.store.read() as conn:
        for name, fn in _activity_providers.items():
            try:
                newest = max(newest, float(fn(conn, rec.task_id) or 0.0))
            except Exception as e:
                logger.warning(f"[Task] activity provider {name!r} 抛异常，"
                               f"{rec.task_id} 本次按「刚动过」处理（偏向不回收）: {e}")
                return kernel.now()
    return newest

def _collect_blockers(conn: sqlite3.Connection, task_id: str) -> tuple[Blocker, ...]:
    out: list[Blocker] = []
    for name, fn in _blocker_providers.items():
        try:
            out.extend(fn(conn, task_id))
        except Exception as e:
            # provider 挂了不能让整个视图读不出来。但也不能静默——
            # blockers 少算会让 Reconciler 误判 Task 可推进（比多算危险）。
            from loguru import logger
            logger.error(
                f"[Runtime] blocker provider {name!r} 抛异常，"
                f"task {task_id} 的 blockers 可能不完整: {e}"
            )
    return tuple(out)


# ══════════════════════════════════════════════════════════════════════════
# 读取 API（不经 Kernel —— 读不需要走唯一写路径）
# ══════════════════════════════════════════════════════════════════════════

def get_task(kernel: RuntimeKernel, task_id: str) -> Optional[TaskRecord]:
    with kernel.store.read() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return TaskRecord.from_row(row) if row else None


def get_task_view(kernel: RuntimeKernel, task_id: str) -> Optional[TaskView]:
    with kernel.store.read() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        rec = TaskRecord.from_row(row)
        return TaskView(record=rec, blockers=_collect_blockers(conn, task_id))


def list_active_tasks(kernel: RuntimeKernel) -> list[TaskRecord]:
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE lifecycle=? ORDER BY created_at ASC, rowid ASC",
            (Lifecycle.ACTIVE,),
        ).fetchall()
    return [TaskRecord.from_row(r) for r in rows]


def list_ready_tasks(kernel: RuntimeKernel) -> list[TaskView]:
    """Reconciler 用。level-triggered：每次重新算，不依赖任何事件。

    ⚠️ 这里的 `execution=IDLE` 必须和 `is_ready` 保持一致 ——
    两处写法分家过一次（SQL 用 `!=RUNNING`、属性也用 `!=RUNNING`，
    于是 `PAUSED` 在两边都被误判为可推进）。理由见 `is_ready`。
    """
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE lifecycle=? AND execution=? "
            "ORDER BY created_at ASC, rowid ASC",
            (Lifecycle.ACTIVE, Execution.IDLE),
        ).fetchall()
        out = []
        for r in rows:
            rec = TaskRecord.from_row(r)
            view = TaskView(record=rec, blockers=_collect_blockers(conn, rec.task_id))
            if view.is_ready:
                out.append(view)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 命令 handler
# ══════════════════════════════════════════════════════════════════════════

CREATE = "task.create"
SET_EXECUTION = "task.set_execution"
SET_PLACEMENT = "task.set_placement"
SET_TURN = "task.set_turn"
TERMINATE = "task.terminate"


def _touch(conn: sqlite3.Connection, task_id: str, now: float) -> int:
    """bump revision + updated_at，返回新 revision。
    所有改 task 的 handler 都必须走这里——**revision 单调递增是不变量 14 的物理基础**。"""
    conn.execute(
        "UPDATE tasks SET revision=revision+1, updated_at=? WHERE task_id=?",
        (now, task_id),
    )
    row = conn.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return int(row["revision"])


def _require(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise KernelError(f"Task 不存在: {task_id}")
    return row


def _assert_not_terminal(row: sqlite3.Row, what: str) -> None:
    """不变量 18 的执行点。放在 handler 里而不是全局不变量检查里，
    是因为它需要知道"这次命令想干什么"才能给出可读的错误。"""
    if row["lifecycle"] == Lifecycle.TERMINAL:
        raise InvariantViolation(
            "terminal_immutable",
            f"Task {row['task_id']} 已是 TERMINAL（{row['terminal_reason']}），"
            f"不允许{what}。要继续请新建 Task 并设 parent_task_id。",
        )


def install(kernel: RuntimeKernel) -> None:
    """把本领域的命令与不变量装进 Kernel。由 get_kernel() 调用。"""
    kernel.register_revision_source("task", "tasks", "task_id")

    @kernel.register(CREATE)
    def _create(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        p = cmd.payload
        kind = p.get("kind", TaskKind.CONVERSATION)
        if kind not in TaskKind._ALL:
            raise KernelError(f"未知 Task kind: {kind!r}，允许: {sorted(TaskKind._ALL)}")
        placement = p.get("placement", Placement.FOREGROUND)
        if placement not in Placement._ALL:
            raise KernelError(f"未知 placement: {placement!r}")

        task_id = p.get("task_id") or ("task_" + uuid.uuid4().hex[:12])
        parent = p.get("parent_task_id")
        if parent is not None:
            # 父 Task 必须存在。允许父已终止——"从一个失败的 Task 派生重试"是合法的，
            # 也正是不变量 18 要求的替代路径。
            _require(conn, parent)

        conn.execute(
            """INSERT INTO tasks (task_id, parent_task_id, kind, goal_summary,
                                  lifecycle, placement, execution, current_turn_id,
                                  revision, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
            (task_id, parent, kind, p.get("goal_summary", ""),
             Lifecycle.ACTIVE, placement, Execution.IDLE,
             p.get("current_turn_id"), ctx.now, ctx.now),
        )
        return HandlerOutcome(
            data={"task_id": task_id},
            revision=1,
            events=[TransitionEvent("task.created", "task", task_id, 1,
                                    {"kind": kind, "placement": placement})],
        )

    @kernel.register(SET_EXECUTION)
    def _set_execution(conn, cmd, ctx) -> HandlerOutcome:
        task_id = cmd.subject_id or cmd.payload.get("task_id", "")
        target = cmd.payload.get("execution", "")
        if target not in Execution._ALL:
            raise KernelError(f"未知 execution: {target!r}，允许: {sorted(Execution._ALL)}")
        row = _require(conn, task_id)
        _assert_not_terminal(row, f"改 execution 为 {target}")
        prev = row["execution"]
        conn.execute("UPDATE tasks SET execution=? WHERE task_id=?", (target, task_id))
        rev = _touch(conn, task_id, ctx.now)
        return HandlerOutcome(
            data={"task_id": task_id, "execution": target, "previous": prev},
            revision=rev,
            events=[TransitionEvent("task.execution_changed", "task", task_id, rev,
                                    {"from": prev, "to": target})],
        )

    @kernel.register(SET_PLACEMENT)
    def _set_placement(conn, cmd, ctx) -> HandlerOutcome:
        task_id = cmd.subject_id or cmd.payload.get("task_id", "")
        target = cmd.payload.get("placement", "")
        if target not in Placement._ALL:
            raise KernelError(f"未知 placement: {target!r}")
        row = _require(conn, task_id)
        _assert_not_terminal(row, f"改 placement 为 {target}")
        # ⭐⭐⭐ **「GUI Task 不可后台化」这条硬约束 —— 2026-08-08 加上。**
        #
        # ⚠️ 这里原来写着「以后会加，现在不加 —— 那条依赖 OSActivityLease，
        #    **提前加会变成一个没有对应执行层的空规则**」。
        #    ⭐ 现在执行层齐了：`OSActivityLease` 已是权威，
        #      而 GUI 自动化已经成为**第一个真实的 Task**
        #      （`open_gui_session` 建、`close_gui_session` 收）。所以现在该加了。
        #    📌 那条注释本身是个好范例：**别在一条规则的执行层存在之前就写下它** ——
        #       否则它是一条没人能违反、也没人能兑现的空话。
        #
        # 🔴 **为什么它必须是硬约束，而不是「碰巧成立的事实」**：
        #    那条的原话是「改绑 Task 后必须保证 Task 生命周期与 mini 窗
        #    **严格同步**，否则**用户再也无法从屏幕判断授权是否仍在**」。
        #    一个 GUI Task 被丢到后台 → mini 窗还开着但那件事「在后台跑」，
        #    于是**屏幕上的信号和真实状态脱钩** ——
        #    而 mini 窗恰恰是「Nano 正在自主操作」唯一的用户可见信号。
        #    📌 同上下文治理那条：**UI 必须是权威状态的忠实投影。**
        if (row["kind"] == TaskKind.GUI_AUTOMATION
                and target == Placement.BACKGROUND):
            raise KernelError(
                f"GUI 自动化 Task {task_id} 不可后台化"
                f"（它的生命周期必须与 mini 窗严格同步，"
                f"丢后台会让屏幕上的信号和真实状态脱钩）："
                f"它的用户可见信号是 mini 窗，后台化会让屏幕与真实状态脱钩。"
                f"要放到后台，请先结束这个 GUI 任务、另建一个后台 Task。")
        prev = row["placement"]
        conn.execute("UPDATE tasks SET placement=? WHERE task_id=?", (target, task_id))
        rev = _touch(conn, task_id, ctx.now)
        return HandlerOutcome(
            data={"task_id": task_id, "placement": target, "previous": prev},
            revision=rev,
            events=[TransitionEvent("task.placement_changed", "task", task_id, rev,
                                    {"from": prev, "to": target})],
        )

    @kernel.register(SET_TURN)
    def _set_turn(conn, cmd, ctx) -> HandlerOutcome:
        task_id = cmd.subject_id or cmd.payload.get("task_id", "")
        turn_id = cmd.payload.get("turn_id")
        row = _require(conn, task_id)
        _assert_not_terminal(row, "绑定新的 turn")
        conn.execute("UPDATE tasks SET current_turn_id=? WHERE task_id=?", (turn_id, task_id))
        rev = _touch(conn, task_id, ctx.now)
        return HandlerOutcome(
            data={"task_id": task_id, "turn_id": turn_id}, revision=rev,
            events=[TransitionEvent("task.turn_bound", "task", task_id, rev, {"turn_id": turn_id})],
        )

    @kernel.register(TERMINATE)
    def _terminate(conn, cmd, ctx) -> HandlerOutcome:
        task_id = cmd.subject_id or cmd.payload.get("task_id", "")
        reason = cmd.payload.get("reason", TerminalReason.COMPLETED)
        row = _require(conn, task_id)
        if row["lifecycle"] == Lifecycle.TERMINAL:
            # 幂等友好：重复终止不算错（可能是 Reconciler 与业务路径撞了），
            # 但也不改 reason —— 第一次的原因才是真的。
            return HandlerOutcome(
                data={"task_id": task_id, "already_terminal": True,
                      "reason": row["terminal_reason"]},
                revision=int(row["revision"]),
            )
        conn.execute(
            "UPDATE tasks SET lifecycle=?, execution=?, terminal_reason=? WHERE task_id=?",
            (Lifecycle.TERMINAL, Execution.IDLE, reason, task_id),
        )
        rev = _touch(conn, task_id, ctx.now)
        return HandlerOutcome(
            data={"task_id": task_id, "reason": reason}, revision=rev,
            events=[TransitionEvent("task.terminated", "task", task_id, rev, {"reason": reason})],
        )

    # ── 不变量 ───────────────────────────────────────────────────────────

    def _inv_terminal_not_running(conn: sqlite3.Connection) -> None:
        """不变量 4 的第一条：终态 Task 不该还是 RUNNING。
        （"终态不得拥有 OPEN Approval / PENDING Wait / active lease"由各自的模块补齐。）"""
        row = conn.execute(
            "SELECT task_id FROM tasks WHERE lifecycle=? AND execution=? LIMIT 1",
            (Lifecycle.TERMINAL, Execution.RUNNING),
        ).fetchone()
        if row is not None:
            raise InvariantViolation("terminal_not_running",
                                     f"Task {row['task_id']} 已 TERMINAL 但 execution 仍是 RUNNING")

    def _inv_slot_limits(conn: sqlite3.Connection) -> None:
        """execution slots：**前台 1 + 后台 N**，结构约束。

        ⚠️⚠️ **为什么这条不变量必须存在，而不是「靠 `pipeline_lock` 就行了」**：
           `pipeline_lock` 是**内存里的 asyncio.Lock** —— 它一重启就没了，
           而且它只管前台那一路，对后台**一无所知**（实测：后台并发
           `asyncio.create_task` **完全没有上限**，想起多少起多少）。
        📌 **一个只存在于内存里的限制，不是这个系统的限制，
           它只是这一次进程运行的限制。**

        ⭐ 这里是**绊线，不是闸**：真正排队的机制在 `app` 的信号量那边
           （出口是「稍后跑」）。这条不变量的作用是——万一哪天有人绕过那个
           机制，事务当场回滚而不是安静地超载。
           📌 **机制负责让它不发生，不变量负责让它发生时被发现。**
           （同 Interaction 槽位：`UNIQUE` 索引 + 不变量两层。）

        ⚠️ 只数 `RUNNING`，**不数 IDLE** —— 排队中的后台任务是 ACTIVE+IDLE，
           那是它的诚实状态（`CREATE` 恒定写 IDLE，本来就是「还没跑」）。
           📌 **「在排队」和「在跑」必须是两个状态，不能都叫「活着」** ——
              压成一个之后，上限就再也数不清了。
        """
        rows = conn.execute(
            "SELECT placement, COUNT(*) AS n FROM tasks "
            "WHERE lifecycle=? AND execution=? GROUP BY placement",
            (Lifecycle.ACTIVE, Execution.RUNNING)).fetchall()
        counts = {r["placement"]: int(r["n"]) for r in rows}
        fg = counts.get(Placement.FOREGROUND, 0)
        bg = counts.get(Placement.BACKGROUND, 0)
        if fg > MAX_FOREGROUND_RUNNING:
            raise InvariantViolation(
                "slot_limits",
                f"同时有 {fg} 个前台 Task 在 RUNNING，上限 {MAX_FOREGROUND_RUNNING}"
                f" —— 前台那个 1 不是配置项，是定义：一个用户一次只在跟一件事对话")
        if bg > MAX_BACKGROUND_RUNNING:
            raise InvariantViolation(
                "slot_limits",
                f"同时有 {bg} 个后台 Task 在 RUNNING，上限 {MAX_BACKGROUND_RUNNING}"
                f" —— 排队中的应该停在 IDLE，不该被置成 RUNNING")

    def _inv_one_foreground_conversation(conn: sqlite3.Connection) -> None:
        """**至多一个「在前台的一件事」**（对话类）。

        ⭐ 这条不变量是 `current_conversation_task` 改用 `placement` 之后的**配套**：
           那个查询现在问的是「谁在前台」，而一个「谁」的问题，只有在答案唯一时
           才有意义。📌 **一个读侧假定唯一的字段，必须有人保证它唯一** ——
           否则 `LIMIT 1` 只是在两个都对的答案里随便挑一个，而且永远不报错。

        ⚠️ **只管 `CONVERSATION`。** GUI 自动化 Task 也是 FOREGROUND
           （`oslease` 建它时就写死了），后台任务是 BACKGROUND ——
           📌 一个限制的作用域，必须和它要守的那个读侧查询**逐字相同**，
              否则它不是拦得太松就是拦到了不相干的人。

        ⚠️ 与 `_inv_slot_limits` **不重复**：那条只数 `RUNNING`（它守的是执行槽），
           而搁置/回接发生在 `IDLE` 上 —— 两件事全程碰不到面。
           📌 **「几个在跑」和「哪个在前台」是两个问题**，
              一条只数 RUNNING 的不变量，对「两个都 IDLE 但都自称前台」一无所知。
        """
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE kind=? AND lifecycle=? AND placement=?",
            (TaskKind.CONVERSATION, Lifecycle.ACTIVE, Placement.FOREGROUND)).fetchone()
        n = int(row["n"]) if row else 0
        if n > 1:
            raise InvariantViolation(
                "one_foreground_conversation",
                f"同时有 {n} 件对话类 Task 在前台 —— 上限是 1。"
                f"「并存」的正确表示是【一个在前台 + 其余退居后台】"
                f"（`_park_current_for_new` 在开新事时自动做，"
                f"`resume_conversation_task` 负责回去），"
                f"不是两个都自称在前台：多出来的那个既回不去、也 finish 不掉。")

    def _inv_enums(conn: sqlite3.Connection) -> None:
        """枚举值合法性。看起来多余（handler 都校验了），但它挡的是
        **绕过 Kernel 的直接 UPDATE**——shadow 模式和手工修库时真的会发生。"""
        bad = conn.execute(
            f"""SELECT task_id, lifecycle, placement, execution FROM tasks
                WHERE lifecycle NOT IN ({','.join('?' * len(Lifecycle._ALL))})
                   OR placement NOT IN ({','.join('?' * len(Placement._ALL))})
                   OR execution NOT IN ({','.join('?' * len(Execution._ALL))})
                LIMIT 1""",
            (*sorted(Lifecycle._ALL), *sorted(Placement._ALL), *sorted(Execution._ALL)),
        ).fetchone()
        if bad is not None:
            raise InvariantViolation(
                "task_enums",
                f"Task {bad['task_id']} 有非法枚举值: "
                f"lifecycle={bad['lifecycle']} placement={bad['placement']} execution={bad['execution']}",
            )

    kernel.register_invariant("terminal_not_running", _inv_terminal_not_running)
    kernel.register_invariant("task_enums", _inv_enums)
    kernel.register_invariant("slot_limits", _inv_slot_limits)
    kernel.register_invariant("one_foreground_conversation",
                              _inv_one_foreground_conversation)


# ══════════════════════════════════════════════════════════════════════════
# 对话类 Task —— 「一件事」的边界由模型判，代码给默认
# ══════════════════════════════════════════════════════════════════════════
# ⚠️⚠️ **这一层的设计经过一次被推翻的做法 + 一次十项核查**，结论如下。
#    最要紧的三条前提：
#
# ① **它必须跨多个 turn。** 三条各自独立的结论都要求这一点：
#    · 迭代阅读的 scratchpad：绑 Task 而**不绑 Final Answer**，因为
#      「任务会被挂起、丢后台、被插队，**『一轮 = 一次 Final Answer』不再成立**」
#    · 「仅本次」的 MCP 授权：「**一个 turn 不能代表任务完成**」
#    · per-task 成本：要「总执行代价」，而 per-turn 已经有了 ——
#      若 Task = 一次交换，那这条需求等于没满足
#    🔴 所以**「Task = 一次交换」被明文否掉**。而上下文治理的 L2 降级单位是
#       「一次交换」，那是**另一个粒度**，不经过 Task。
#
# ② **懒创建：第一个「归属物」出现时才建。**
#    ⚠️ 这条与 ① **不冲突** —— 上一版把「懒创建」和「per-exchange 边界」
#    捆在一起提，所以被一起否掉了；但被否的只是后者。
#    简单问答（问个时间、聊两句）**压根不建** —— 没有任何跨 turn 的东西需要主人。
#    📌 **一个只为「给别人当主人」而存在的实体，应该在第一个孩子出现时才诞生。**
#
# ③ **边界由模型判，代码给默认。** 默认 = **复用当前那个 ACTIVE 的**。
#    ⚠️ 默认会在一处错：用户在长任务进行中提一个**完全无关**的请求
#    （判据是「不要求 x 和 y 相关」）→ 两件事被归进同一个。
#    ⭐ 但代价被限制在「分组不好看」（`owner_task_id` 是归属标签、不是生命周期主宰），
#    而模型可以显式开一个新的。
#    📌 **代码给一个「够用的默认」，模型只在默认不对时才需要说话** ——
#       这比「每次都问模型」和「永远不问模型」都好（同那条：
#       「继续」不是一个动作，所以不需要一个命令）。
#
# ⚠️⚠️ **而最要紧的一条约束**：
# 📌 **不许把「这件事结束了」当成任何东西【唯一】的失效条件。**
#    每一样绑它的东西都必须自带独立兜底（TTL / 数量上限 / 启动收尾）——
#    因为**它的结束依赖模型判断，而模型会忘**（甚至压根没机会说话：进程崩了）。


def _get_kernel() -> RuntimeKernel:
    """⚠️ 内联 import —— `get_kernel` 是单例取值器（同 oslease / waitcond 的理由）。"""
    from core.runtime.kernel import get_kernel
    return get_kernel()


def current_conversation_task(kernel: RuntimeKernel) -> Optional[str]:
    """当前那个「一件事」的 id（对话类）。没有就 None。

    ⚠️ **只认 `CONVERSATION` 那一档** —— GUI / 后台 / Subagent各有自己的边界，
       它们不该被当成「当前这件事」。
       📌 一个「当前是什么」的查询必须限定在同一类里，
          否则一次 GUI 任务会把对话类的归属抢走。

    ═══ ⚠️⚠️ 2026-08-16：加上 `placement=FOREGROUND`。这一条改的是「谁说了算」 ═══

    原来这里只有 `ORDER BY created_at DESC` —— **「当前是哪件事」是从创建时间
    【推】出来的，不是被【写】下来的。** 只有一个候选时它永远是对的，所以它一直
    没出问题；而「搁置 X → 先做 Y → 回头接上 X」一旦成立，它就必然错：
    **X 永远比 Y 老，接不回去。**

    📌 **一个「现在是哪个」的问题，答案必须是一条被显式写下的事实，
       不能从一个碰巧单调的字段里推** —— 推导在只有一个候选时永远正确，
       于是它的错误要等到第二个候选出现才暴露，而那时它已经被到处依赖了。
    ⭐ 与 `waiting_intent` 那条**逐字同形**：明写过「不许由 `timer_at` 猜 pill
       是否可控 —— 等待创建者必须显式给 `waiting_intent`」。
    ⭐ 而要写的地方**本来就有**：`placement`。它此前是**零调用方**
       （`SET_PLACEMENT` 建好了、注册了、没人 submit 过）——
       📌 **一个建好了却没人写也没人读的字段，和没有它是一样的**（同 `is_readonly`）。

    🔴 **顺带修掉一个今天就存在的缺陷**：`task_boundary(action='start')` 走
       `finish_current=False`，会建**第二个** FOREGROUND ACTIVE。于是旧那件事当场
       变成一条**回不去、也结束不掉**的记录 —— `finish` 收的是新的那个，而旧的只能
       等 `sweep_stale_conversations` 的 2 小时 TTL；它若名下有 blocker 则**永远不收**，
       并在 `[Ongoing work]` 里一直报下去。（现在 `start` 会显式把旧的搁置。）

    ⭐ 排序保留不动：不变量已保证至多一个 FOREGROUND，排序只是它的兜底 ——
       📌 机制负责让它不发生，不变量负责让它发生时被发现，而读侧仍要有确定的答案。
    """
    with kernel.store.read() as conn:
        r = conn.execute(
            "SELECT task_id FROM tasks WHERE kind=? AND lifecycle=? AND placement=? "
            # ⚠️ `rowid` 是次级键：`created_at` 平局时（同一毫秒 / FakeClock）
            #    没有它取到的就不是最新那个。见本文件末尾那段判据。
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (TaskKind.CONVERSATION, Lifecycle.ACTIVE, Placement.FOREGROUND)).fetchone()
    return r["task_id"] if r else None


# ⚠️⚠️ `parked_conversation_tasks()` **已删除（2026-08-20 Task 收窄）**。
#
# 它唯一的消费者是抽屉里那段「搁置 N」。而那一段与 `park` 工具一起退役了：
# 📌 **抽屉回答的是「什么正在动」，而被退居后台的一件事【没有任何执行体】** ——
#    把它画在一个「什么在跑」的面板里，就是在告诉用户「有人在推进它」。
# ⭐ `placement=BACKGROUND` 从此只是**内部指针的另一半**（「它不是当前那件」），
#    不再有任何用户可见语义。模型侧仍在 `conversation_tasks_for_model()` 里
#    看得到它，措辞明写 `[PARKED - nothing is working on it]`。


def ensure_conversation_task(goal_summary: str = "") -> Optional[str]:
    """要挂一个归属物了 → 保证有一个「一件事」可挂。**这就是懒创建的入口。**

    ⚠️ **调用时机是「第一个归属物即将产生」**，不是「用户发了消息」。
       后者会把它退化成 per-exchange，而那已被三条独立的结论否掉。
    ⭐ 已经有 ACTIVE 的就复用（那正是「跨多个 turn」的实现方式）。
    """
    try:
        k = _get_kernel()
        tid = current_conversation_task(k)
        if tid:
            return tid
        tid = k.submit(Command(kind=CREATE, payload={
            "kind": TaskKind.CONVERSATION,
            "placement": Placement.FOREGROUND,
            "goal_summary": (goal_summary or "")[:200],
        })).data["task_id"]
        logger.info(f"[Task] 新开一件事 {tid}"
                    f"{'：' + goal_summary[:60] if goal_summary else ''}")
        return tid
    except Exception as e:
        # ⚠️ 建不出来就返回 None，**不阻断调用方** ——
        #    📌 接线一个新实体时，它的失败不该让已经能用的东西停摆
        #       （同 GUI Task）。归属物照旧能创建，只是 owner 为空。
        logger.warning(f"[Task] 新开一件事失败（归属物照旧创建，owner 留空）: {e}")
        return None


def finish_conversation_task(outcome: str = "",
                             note: str = "") -> Optional[str]:
    """模型说「这件事完了」。返回被收掉的 task_id（没有就 None）。

    ⚠️ `outcome` 必须能区分**做完了**和**放弃了** ——
       已定：「`CANCELLED` 必须是独立状态，不许塞进 `FAILED`」，
       理由是**用户主动停掉不是失败**，归进失败会让模型和用户都去排查一个
       不存在的问题。这里同源：**「做完了」和「不做了」在历史上必须分得开。**
    """
    try:
        k = _get_kernel()
        tid = current_conversation_task(k)
        if not tid:
            logger.debug("[Task] 没有正在进行的事，无需收尾")
            return None
        reason = (TerminalReason.CANCELLED if outcome == "abandoned"
                  else TerminalReason.COMPLETED)
        # ⚠️⚠️ payload 键名是 **`reason`**，不是 `terminal_reason`（见 `_terminate`）。
        #    🔴 第一版传的是后者 —— 于是它被**静默忽略**、走了默认值 `COMPLETED`，
        #       「放弃」被记成了「做完了」，而**没有任何报错**。
        #    📌 **一个「多传的键被忽略、缺的键取默认」的接口，写错键名是静默失败** ——
        #       而这里静默失败的后果正是那条要防的：
        #       **「不做了」被记成「做完了」，历史读不出真相。**
        k.submit(Command(kind=TERMINATE, subject_id=tid, payload={
            "task_id": tid, "reason": reason,
            "note": note or "模型声明这件事结束了",
        }))
        logger.info(f"[Task] 一件事收尾 {tid} → {reason}"
                    f"{'（' + note[:50] + '）' if note else ''}")
        return tid
    except Exception as e:
        # ⚠️ 收不掉只是留一条 ACTIVE 记录 —— **不影响任何失效条件**，
        #    因为每样绑它的东西都自带兜底（那条判据的直接回报）。
        logger.warning(f"[Task] 收尾失败（不影响归属物的失效）: {e}")
        return None


def start_new_conversation_task(goal_summary: str,
                                finish_current: bool = False) -> Optional[str]:
    """模型说「这是另一件事」。

    ⚠️⚠️ **默认不结束当前那件事**（`finish_current=False`）。
       判据原话：「**并不要求 x 和 y 一定相关**」——
       用户在装库跑着的时候插一句「先做别的」，那**两件事并存**，
       旧的没完。
       📌 **「开一件新的」和「结束旧的」是两个动作，不该被一个调用捆死** ——
          捆死之后，模型想表达「并存」就没有说法了。
    """
    try:
        if finish_current:
            finish_conversation_task("completed", "开始另一件事前收尾")
        else:
            # ⭐⭐⭐ **2026-08-16：不结束旧的 ≠ 让旧的留在前台。**
            #
            # 🔴 原来这里什么都不做，直接建第二个 FOREGROUND。后果不是「并存」，
            #    是**旧那件事当场失联**：`current_conversation_task` 取最新的一个，
            #    于是旧的既不是「当前」（回不去）、`finish` 也收不到它（收的是新的）。
            #    它只剩 `sweep_stale_conversations` 的 2 小时 TTL；而**名下有 blocker
            #    的那些连 TTL 都不收**，会在 `[Ongoing work]` 里一直报下去。
            #
            # ⭐ 「并存」这个语义是对的（判据：「并不要求 x 和 y 一定相关」），
            #    错的是它的表示方式 —— 并存必须表示成 **一个在前台 + 其余被搁置**，
            #    而不是**两个都自称在前台**。
            # 📌 **「两件事并存」和「两件事都在前台」是不同的两句话** ——
            #    把前者实现成后者，多出来的那一个不会报错，它只是安静地够不着了。
            _park_current_for_new(goal_summary)
        k = _get_kernel()
        tid = k.submit(Command(kind=CREATE, payload={
            "kind": TaskKind.CONVERSATION,
            "placement": Placement.FOREGROUND,
            "goal_summary": (goal_summary or "")[:200],
        })).data["task_id"]
        logger.info(f"[Task] 另开一件事 {tid}：{goal_summary[:60]}")
        return tid
    except Exception as e:
        logger.warning(f"[Task] 另开一件事失败: {e}")
        return None


def _move_placement(task_id: str, placement: str, why: str) -> bool:
    """把一件事挪到前台 / 后台。**唯一写 `placement` 的地方。**"""
    if not task_id:
        return False
    try:
        _get_kernel().submit(Command(kind=SET_PLACEMENT, subject_id=task_id, payload={
            "task_id": task_id, "placement": placement}))
        logger.info(f"[Task] {task_id} → {placement}（{why}）")
        return True
    except Exception as e:
        # ⚠️ 失败**不阻断调用方**：搁置没成只是这件事还在前台，
        #    比「因为挪不动就把用户的请求做失败」好得多。
        #    ⭐ 但必须是 warning 而不是 debug —— 它会让下一步的不变量成为
        #      唯一的防线，而那时报出来的是**另一个**症状。
        logger.warning(f"[Task] {task_id} 挪到 {placement} 失败（照旧继续）: {e}")
        return False


def _park_current_for_new(goal_summary: str = "") -> Optional[str]:
    """开新的一件事之前，把现在前台那件搁到后台。返回被搁置的 id。"""
    try:
        tid = current_conversation_task(_get_kernel())
    except Exception:
        return None
    if not tid:
        return None
    return tid if _move_placement(
        tid, Placement.BACKGROUND,
        f"另开「{(goal_summary or '')[:30]}」时自动搁置") else None


# ⚠️⚠️ `park_conversation_task()` **已删除（2026-08-20 Task 收窄）**。
#
# 🔴 它是「把一整件 Task 丢后台」那个概念的落地形态，2026-08 中旬推翻了它：
#    **没有办法定义「哪一种 Task 值得进后台」** —— 于是「抛后台」的主语从
#    Task 换成了**一次调用**（见 `dont_wait` 工具）。
#
# ⭐ 而「退居」本身**没有被删，只是不再是一个可申报的动作**：
#    `_park_current_for_new()` 在 `task_boundary(start)` 里自动做掉它。
#    📌 判据取自 `_TASK_BOUNDARY_MANIFEST` 自己写过的那句：
#       **默认不需要动作，只有偏离默认才需要动作。**
#       「放下当前这件」是开新事的默认副作用；
#       「回到旧的那件」才是偏离默认 —— 所以 `resume` 留着，`park` 不留。


def resume_conversation_task(task_id: str) -> tuple[bool, str]:
    """回头接上一件被搁置的事。返回 `(成功, 说明)`。

    ⚠️⚠️ **顺序只有一种是合法的：先把现任搁置，再把目标提到前台。**
       📌 一个「至多一个」的不变量，决定了交换两者的顺序 ——
          **必须先腾位再入座**，反过来那一瞬间会有两个前台，
          而不变量是**逐事务**检查的，它会当场回滚，于是这次交接一半都做不成。

    ⚠️ 目标必须**真的存在且还活着**才动手 —— 校验在前、副作用在后：
       📌 一个先把现任搁置、然后才发现目标不存在的实现，
          会把用户从一件好好的事上挪走，去接一件不存在的事。
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return False, "resume needs the task id of the parked piece of work."
    try:
        k = _get_kernel()
        rec = get_task(k, task_id)
    except Exception as e:
        logger.warning(f"[Task] 回接 {task_id} 失败（读不到）: {e}")
        return False, "Could not read that piece of work."
    if rec is None:
        return False, f"No piece of work with id {task_id}."
    if rec.kind != TaskKind.CONVERSATION:
        return False, (f"{task_id} is not a conversation-level piece of work "
                       f"(it is {rec.kind}); it cannot be resumed this way.")
    if rec.lifecycle != Lifecycle.ACTIVE:
        # ⚠️ 如实说它已经结束了 —— 不许静默当成功，
        #    否则模型会向用户宣布它「接着做」一件已经收掉的事。
        return False, (f"{task_id} already ended ({rec.terminal_reason or 'terminal'}); "
                       f"there is nothing to resume. Do NOT tell the user you resumed it.")
    if rec.placement == Placement.FOREGROUND:
        return True, f"{task_id} was already the one in front; nothing changed."
    _prev = current_conversation_task(k)
    if _prev and _prev != task_id:
        _move_placement(_prev, Placement.BACKGROUND, f"回接 {task_id} 时让位")
    if not _move_placement(task_id, Placement.FOREGROUND, "模型回头接上"):
        return False, "Could not bring that piece of work back to the front."
    return True, (f"Back on: {rec.goal_summary or task_id}."
                  + (f" The previous one ({_prev}) is now parked." if _prev
                     and _prev != task_id else ""))


# ══════════════════════════════════════════════════════════════════════════
# 后台任务成为 Task —— `app._bg_tasks` 那个内存 dict 的权威版
# ══════════════════════════════════════════════════════════════════════════
# ⚠️⚠️ **这一层【不加列、不加表、不删行】。三个「不」各有理由。**
#
# ① **不加列**：`_bg_tasks` 里那五样，逐个问「它在崩溃之后还有意义吗」：
#      · `display`        → 有 → `goal_summary` ✅ 已有
#      · `started_at`     → 有 → `created_at` ✅ 已有
#      · `suspension_ref` → **没有**：它的用途是任务完成时唤醒那条挂起，
#                            而进程一死那个 coroutine 就没了，永远不会有「完成」
#                            （这正是启动恢复要终止它的原因）
#      · UI 元素引用       → **没有**：DOM 随窗口消失（那是 L7 ViewSession 的事）
#      · asyncio.Task 句柄 → **没有**：进程内的东西
#    📌 **一个「要不要加列」的问题，先问「这条信息在崩溃之后还有意义吗」** ——
#       只在进程内有意义的东西放进权威库，只会让人以为它跨重启可信。
#
# ② **不删行**（这一条是回头核对约定之后改的）：
#    第一版写了个 `prune_finished_background_jobs()` 直接 `DELETE`。回查发现
#    **本项目没有任何模块删过状态行** —— 全都是关到终态（唯一的 `DELETE` 在
#    `projection.py`，删的是投影回执，不是状态）。而且它还绕过了内核的唯一写路径。
#    改成**读侧封顶**：`finished_background_jobs(limit=...)`。
#    ⚠️ 但那条判据仍然要被回答：
#       📌 **任何列表/配额落地时必须回答「谁来把它降下去」**（
#          「**Nano 没有 session 概念**，Finished 必须有条数上限」）。
#    **答案是三层，明写出来而不是留空**：
#       · UI 只读最近 N 条（本函数的 limit）—— 单调堆积在**界面上**不会发生
#       · 用户手动 [Clear] —— 属 L5（x running task(s) 抽屉）
#       · 表本身**刻意不清**：这些行是「上个进程崩的时候在跑什么」的**唯一记录**，
#         删掉就把崩溃取证一起删了。量级算过：每行约 200 字节、每天十个后台任务
#         → **不到 1 MB/年**。最终兜底沿用上下文治理 L4 定的同一招
#         （超大文件体积阈值），到那时一起做。
#    📌 **「谁来把它降下去」的合法答案里包括「刻意没有人，因为代价已经算过」** ——
#       不合法的是**没算就没答**。
#
# ③ **不加表**：后台任务的状态就是那一行 Task，没有第二份现实。
#
# 于是三件要求的事有了数据源：
#    ① Finished 段（4238 行）—— TERMINAL 的行
#    ② 「崩溃恢复后能翻看上个进程在跑什么」（4252 行）—— INTERRUPTED_BY_RESTART 的行
#    ③ 「跑完了 / 崩了 / 被你停了」三种必须分开（4277-4284 行）—— terminal_reason
#
# 🔴 **③ 最要紧，因为 Nano 自己正犯着在同类工具上观察到、并写成要求的那个问题**：
#    app._run_bg_task 把异常压成 f"执行失败：{e}"，然后走**和成功完全一样**的
#    路径 —— 于是「跑完了但没输出」/「崩了」/「被停了」在下游表象一致。
#    原话是：「**要不是在对话里说了一句，模型永远不会知道。**」
# 📌 **一个能分辨三种结局的系统，和一个能描述其中一种的系统，
#    差的不是细节，是「历史能不能读出真相」。**

# 后台任务的三种结局 -> Task 终态原因。
# 🔴 **cancelled 独立，不许塞进 failed** —— 已定：
#    **用户主动停掉不是失败**，归错会让模型和用户去排查一个不存在的问题。
_BG_OUTCOME_TO_REASON = {
    "completed": TerminalReason.COMPLETED,
    "failed":    TerminalReason.FAILED,
    "cancelled": TerminalReason.CANCELLED,
}

# ══════════════════════════════════════════════════════════════════════════
# Finished 段留多久 —— **「本次运行」，不是「最近 N 条」**（2026-08-15 定）
# ══════════════════════════════════════════════════════════════════════════
#
# 早先只写了「必须有条数上限，否则单调堆积」（任何列表/配额
# 落地时必须回答"谁来把它降下去"）。实测同类工具之后有了更好的答案：
#
#   > 那类工具关掉程序后 Finished 全部消失。这种做法 + 一个极大值保护更好。
#   > 因为单纯硬编码 limit 数字**没有任何语义**；而那种做法起码能表示
#   > 「这次打开 Nano 产生的任务」，而不是往里面无脑塞一大堆不知道
#   > 什么时候发生的垃圾。
#
# 📌 **一个上限如果只是个数字，用户没法预测哪一条会消失；
#    如果它对应一个真实边界（一次运行），用户就知道自己在看什么。**
# ⭐ 硬上限仍然保留，但它退位成**防失控**（一直不关程序的情况），
#    不再是主要机制 —— 主要机制是那条时间线。
#
# ⚠️⚠️ **`INTERRUPTED_BY_RESTART` 那些行不会因此消失**，这一点很关键：
#    专门要求过 Finished 要能答出「**上个进程崩的时候在跑什么**」。
#    而那些行**正是本次启动的清扫把它们标成终态的** —— 它们的 `updated_at`
#    落在本次运行窗口内，所以照样在列表里。
#    📌 **「本次运行产生的」不等于「本次运行发生的」** ——
#       前者包含"本次运行才认定下来的历史"，而那恰好就是崩溃恢复要给的东西。
#
# ⚠️⚠️ 起点取自 **`kernel.started_at`（内核自己的时钟）**，不是模块级的
#    `time.time()`。第一版写的正是后者，于是这里拿墙钟去和 FakeClock 写下的
#    `updated_at` 比大小 —— `t_f1_stage7_bgjob` 的崩溃取证那条当场红了。
#    📌 **一个模块级的 `time.time()` 是一个看不见的全局依赖** ——
#       在一个已经有时钟抽象的系统里，它等于给同一件事造了第二个时间源，
#       而两个时间源只在"恰好都是真实时间"时相等。

# 防失控用的硬上限。⚠️ 大到正常一天用不到、小到不会把抽屉拖垮。
# 📌 它现在只是安全带，不是保留策略本身 —— 别再把它当"留多少条"来读。
_BG_FINISHED_MAX = 200


def create_background_job(display: str,
                          parent_task_id: Optional[str] = None) -> Optional[str]:
    """一个后台任务真的跑起来了 -> 建一条权威记录。返回 task_id（失败 None）。

    ⚠️ parent_task_id 传的是**那件事**（对话类 Task）—— 后台任务是
       「某件事的一部分」，不是凭空长出来的。
       但它**不需要主人才能存在**：拿不到就留空，照旧建。
       📌 归属是标签，不是前提条件。
    ⚠️ placement=BACKGROUND 是**定义**而不是选项 —— 它就是那个「被丢到后台」的东西。
    """
    try:
        k = _get_kernel()
        payload = {
            "kind": TaskKind.BACKGROUND_JOB,
            "placement": Placement.BACKGROUND,
            "goal_summary": (display or "")[:200],
        }
        if parent_task_id:
            payload["parent_task_id"] = parent_task_id
        tid = k.submit(Command(kind=CREATE, payload=payload)).data["task_id"]
        logger.info(f"[Task] 后台任务 {tid} 起来了：{(display or '')[:60]}")
        return tid
    except Exception as e:
        # ⚠️ 建不出来 -> None，**后台任务照旧跑**。
        #    📌 观测一件事的失败，不许把那件事本身弄坏。
        logger.warning(f"[Task] 后台任务建记录失败（任务照旧跑，无权威记录）: {e}")
        return None


def finish_background_job(task_id: Optional[str], outcome: str,
                          note: str = "") -> bool:
    """后台任务结束了。outcome 属于 completed / failed / cancelled。

    ⚠️ 未知 outcome **不静默兜底成 COMPLETED**，而是记 FAILED 并响亮报错。
       📌 兜底方向要朝「看得见」错，不朝「看起来一切正常」错 ——
          错成 COMPLETED 是**造一个假事实**，错成 FAILED 只是多一次排查。
    """
    if not task_id:
        return False
    try:
        reason = _BG_OUTCOME_TO_REASON.get(outcome)
        if reason is None:
            logger.error(
                f"[Task] 后台任务 {task_id} 的结局 {outcome!r} 不认识 -> 记为 FAILED"
                f"（**不默认成 COMPLETED**，那会造一个假事实）。"
                f"允许: {sorted(_BG_OUTCOME_TO_REASON)}")
            reason = TerminalReason.FAILED
        _get_kernel().submit(Command(kind=TERMINATE, subject_id=task_id, payload={
            "task_id": task_id,
            # ⚠️ key 是 reason —— 写成 terminal_reason 会被静默忽略、
            #    然后默认成 COMPLETED。这个坑 2026-08-08 踩过，两个模块同时中招。
            "reason": reason,
            "note": note or "",
        }))
        logger.info(f"[Task] 后台任务 {task_id} 结束 -> {reason}"
                    f"{'（' + note[:60] + '）' if note else ''}")
        return True
    except Exception as e:
        logger.warning(f"[Task] 后台任务 {task_id} 收尾失败: {e}")
        return False


def live_background_jobs(kernel: RuntimeKernel) -> list[TaskRecord]:
    """还在跑的后台任务。**x running task(s) 那个数字的唯一数据源。**"""
    return [r for r in list_active_tasks(kernel) if r.kind == TaskKind.BACKGROUND_JOB]


def finished_background_jobs(kernel: RuntimeKernel,
                             limit: int = _BG_FINISHED_MAX,
                             since: float | None = None) -> list[TaskRecord]:
    """已结束的后台任务，**新的在前**。Finished 段的数据源。

    它能答出旧实现答不出的那个问题：**上个进程崩的时候在跑什么**
    （那些行的 terminal_reason 是 INTERRUPTED_BY_RESTART）——
    旧实现是内存 dict，重启后连「它曾经存在」都不知道。

    ⚠️ 二级键 rowid：同毫秒结束 / FakeClock 下 updated_at 会打平，
       只按它排序时顺序不稳定（这个坑扫过 11 处）。
    """
    # ⚠️ `since=None` → 本次运行开始那一刻（见 `_PROCESS_START` 上面那段）。
    #    显式传 0 可以要"全部历史"（导出、排查用），但**UI 不该那么调**。
    _since = float(kernel.started_at) if since is None else float(since)
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE kind=? AND lifecycle=? AND updated_at>=? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (TaskKind.BACKGROUND_JOB, Lifecycle.TERMINAL, _since, int(limit))).fetchall()
    return [TaskRecord.from_row(r) for r in rows]


def running_background_count(kernel: RuntimeKernel) -> int:
    """现在真正在跑的后台任务数（**不含排队中的**）。"""
    with kernel.store.read() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE kind=? AND lifecycle=? AND execution=?",
            (TaskKind.BACKGROUND_JOB, Lifecycle.ACTIVE, Execution.RUNNING)).fetchone()
    return int(r["n"]) if r else 0


def queued_background_jobs(kernel: RuntimeKernel) -> list[TaskRecord]:
    """在排队等 slot 的后台任务（ACTIVE + IDLE）。

    ⭐ 它们**不是**「卡住了」，是「还没轮到」—— 出口是稍后跑，不是失败。
       📌 闸的出口是失败，队列的出口是稍后处理（这条判据第四次用到）。
    """
    return [r for r in live_background_jobs(kernel)
            if r.execution == Execution.IDLE]


def mark_background_running(task_id: Optional[str]) -> bool:
    """拿到 slot 了 → 把这条后台任务从「排队」推进到「在跑」。

    ⚠️ 这一步必须在**真的要开始跑之前**发生，而不是创建时 ——
       否则「排队中」和「在跑」就分不开，上限也就数不清了。
    """
    if not task_id:
        return False
    try:
        _get_kernel().submit(Command(kind=SET_EXECUTION, subject_id=task_id, payload={
            "task_id": task_id, "execution": Execution.RUNNING}))
        return True
    except InvariantViolation as e:
        # 🔴🔴 **这一条是 bug 信号，不是普通失败，所以它是 ERROR。**
        #    真正拦住并发的机制是 `app` 那个信号量；这条不变量只是绊线。
        #    它响 = **机制和账本对不上**（信号量放行了、库里却已经满了），
        #    而那只有几种成因：信号量上限和常量分家了 / 有人绕过 `_start_bg_task` /
        #    上一批 RUNNING 没被启动恢复收掉。**每一种都得查。**
        # 📌 **一个「本来永不该响」的检查响了，它的日志级别应该反映
        #    「这说明另一个地方坏了」，而不是「这次没做成」。**
        logger.error(f"[Task] 🔴 后台 slot 不变量被触发 —— 信号量与账本对不上，"
                     f"请查上面三种成因: {e}")
        return False
    except Exception as e:
        # ⚠️ 其它失败只是记录不准，**不阻断那个任务真的去跑** ——
        #    但仍要 warning，因为它让 slot 计数偏小（偏向「多跑」）。
        logger.warning(f"[Task] 后台任务 {task_id} 转 RUNNING 失败（照旧跑，slot 计数会偏小）: {e}")
        return False


def background_jobs_for_model(kernel: RuntimeKernel) -> str:
    """给模型看的一段话：现在有哪些后台任务在跑。空的就返回空串。

    ⚠️ **只报还在跑的。** 已结束的结果本来就会通过 background 唤醒或诈尸气泡
       送到它面前，动态段里再报一遍等于同一件事说两次 —— 而模型会当成两件事。
       📌 **一件已经通过别的通道告诉过它的事，不该再进每轮注入。**
    ⚠️ 措辞里那句「结束一件事不会停掉它们」是必须的 ——
       明确要求过：终止 / 收尾**都不许连带杀掉后台任务**，
       要停得由 Nano 自己下一次行动去取消。
    """
    try:
        rows = live_background_jobs(kernel)
        if not rows:
            return ""
        # ⚠️ **排队中的要标出来。** 一个还没轮到的任务和一个真在跑的任务，
        #    对模型是两个不同的事实（「它已经在动了」vs「它还没开始」）——
        #    而如果它把排队的当成在跑的，就会去汇报一个还没发生的进展。
        #    📌 **一段注入里，凡是两种状态会导致模型说不同的话，就必须分开标。**
        lines = [f"  . {r.task_id}: {r.goal_summary or '(no goal recorded)'}"
                 + ("" if r.execution == Execution.RUNNING
                    else "  [queued - not started yet]")
                 for r in rows]
        return ("[Background jobs] These are still running on their own:" + chr(10)
                + chr(10).join(lines) + chr(10)
                + "You do NOT need to wait for them; you will be told when one "
                  "finishes. Ending a piece of work does NOT stop them - "
                  "if the user wants one stopped, cancel it explicitly.")
    except Exception as e:
        logger.debug(f"[Task] 生成后台任务列表失败: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════
# 空壳对话 Task 的回收 —— 懒创建的【对称面】
# ══════════════════════════════════════════════════════════════════════════
# 🔴 **实测证据**：整份日志里**没有一行 `[Task] 新开一件事`**，
#    却有一行 `[Task] 一件事收尾 task_d093a2b6ccd6 -> COMPLETED` ——
#    那个 Task 是**更早一次运行留下来的**，跨了至少一次重启还活着。
#    复现出的后果：一件三天前的事、名下什么都没有，`[Ongoing work]` 里照旧报
#    「You currently have these things in progress: ... 等提醒」→
#    **Nano 会跟用户说它还在办一件早就没了的事。**
#
# 📌 **懒创建的对称面**：一个「只为给别人当主人而存在」的实体，
#    在最后一个孩子消失之后就没有存在理由了。
#    当时建了它的**诞生条件**，没建它的**反面** —— 这一层就是那个反面。
#
# ═══ ⚠️ 这个判据走过两版错的，两版都是被既存断言逼出来的，值得记下来 ═══
#
# **第一版（错）：放在启动恢复里，「名下空了就收」。**
#   被 `t_f1_stage7_guitask` 和 `t_f1_stage1_runtime` [7] 同时判红，而它们是对的：
#   🔴 **「名下空了」≠「没有在进行的事」** —— 一个死在 `RUNNING` 里的 Task，
#      之所以没有任何 blocker，**正是因为它当时在干活、而不是在等**。
#      那样收等于顺手推翻了 reconciler step 1 那条带推理的性质
#      （「被重启打断的是 turn，不是这件事本身」）。
#
# **第二版（还是错）：加一条「刚被打断的宽限一次」。**
#   被「第二次启动恢复不再处理它（幂等）」判红 ——
#   🔴 因为它让结果取决于**启动了几次**，而不是取决于**状态**。
#   📌 **那是 edge-triggered 思维溜了进来** ——
#      本项目所有回收都是 level-triggered：只看当前状态该不该收。
#
# **第三版（本层）：TTL + tick。**
#   📌 **区分「刚发生」和「早就没了」要用时间，不用「第几次启动」** ——
#      后者不是状态，它是历史。
#   ⭐ 时间是**真正的状态**：它对启动次数、对进程边界、对调用顺序全都免疫。
#   ⭐ TTL 取 2 小时，与澄清交互的 `_CLARIFICATION_TTL_SECONDS` 同值 ——
#      那一条当初选 2 小时的理由（「过了就是陈旧提问」）在这里逐字成立。
#      📌 一个新 TTL 应该先找项目里已有的同语义先例，而不是另发明一个数。
#
# ⚠️ **第四版：新鲜度的锚点从 `tasks.updated_at` 换成 `last_activity_at()`。**
#    被本层自己的测试判红逼出来的：关掉一条等待**不会 touch `tasks` 那一行**，
#    于是「13:00 三小时的定时刚到点、Nano 正要接着做」会被误判成「早就没动静了」，
#    在最不该的一刻被收掉。
#    📌 **一个「多久没动静」的判断，量的必须是【这件事】的动静，
#       不是【那一行记录】的动静** —— 子树里的活动也是活动。
# ⚠️ 已知的一格小限制：一场持续两小时、期间**一个等待/交互都没有**的纯聊天，
#    它的空壳 Task 会被收掉。代价只是模型那一轮没法说「这件事完了」，
#    而它名下本来也没有任何东西。等前台 slot 把每轮 `SET_TURN` 接上之后自动消失。
_STALE_CONV_TTL_SECONDS = 2 * 60 * 60


def sweep_stale_conversations(kernel: RuntimeKernel) -> int:
    """收掉「名下空了、且已经放了很久」的对话类 Task。返回收了几条。

    ⚠️ 两个条件**必须同时**满足，少一个都会出错：
      · **名下空了** —— 有 blocker 就说明还有东西需要它当主人
      · **放了很久** —— 刚刚还在动的，不许当成过去的事（见上面第一版那个错）
    """
    now = kernel.now()
    stale: list[TaskRecord] = []
    try:
        for rec in list_active_tasks(kernel):
            if rec.kind != TaskKind.CONVERSATION:
                continue
            if rec.execution != Execution.IDLE:
                continue      # 在跑 / PAUSED 的一律不碰
            # ⚠️ 用 `last_activity_at` 而不是 `rec.updated_at` —— 见那个函数的说明：
            #    关掉一条等待不会 touch `tasks` 这一行，只看它会把
            #    「刚被唤醒、正要接着做」误判成「三天前的僵尸」。
            if (now - last_activity_at(kernel, rec)) < _STALE_CONV_TTL_SECONDS:
                continue      # 还新鲜
            v = get_task_view(kernel, rec.task_id)
            if v is not None and not v.blockers:
                stale.append(rec)
    except Exception as e:
        logger.warning(f"[Task] 扫描空壳对话失败（本次不收）: {e}")
        return 0
    n = 0
    for rec in stale:
        try:
            _get_kernel().submit(Command(
                kind=TERMINATE, subject_id=rec.task_id,
                payload={"task_id": rec.task_id,
                         # ⚠️ 不是 COMPLETED（我们并不知道那件事成了没）、
                         #    不是 CANCELLED（没人取消过它）、
                         #    不是 INTERRUPTED_BY_RESTART（它没被打断）。
                         # 📌 **终态原因是一句事实陈述，不许拿一个「差不多」的值凑** ——
                         #    历史读得出真相的前提是每个值只表达它自己那件事。
                         "reason": TerminalReason.EXPIRED,
                         "note": "名下已无归属物，且长时间没有动静"},
                command_id=f"cmd_stale_conv_{rec.task_id}_{int(rec.revision)}"))
            n += 1
            logger.info(
                f"[Task] 空壳对话 {rec.task_id} 收成 EXPIRED"
                f"{'：' + rec.goal_summary[:40] if rec.goal_summary else ''}")
        except Exception as e:
            logger.warning(f"[Task] 收空壳对话 {rec.task_id} 失败: {e}")
    return n


def install_reconcile(kernel: RuntimeKernel) -> None:
    """把本模块的 tick 步骤登记到 Reconciler。

    ⚠️ 单独一个函数、而不是塞进 `install()`：`reconciler` 会 import `task`，
       在 `install()` 里顶层 import 它就是循环依赖 —— 所以用局部 import，
       并且由调用方在内核起好之后调（同 `oslease` 的做法）。
    """
    from core.runtime import reconciler as _rec

    def _tick(k: RuntimeKernel, report) -> None:
        n = sweep_stale_conversations(k)
        if n:
            report.extra["stale_conversations_closed"] = n

    _rec.register_tick_step("task_stale_conversations", _tick)


def owner_label() -> Optional[str]:
    """给一个**只贴标签、不创建**的归属物用：现在这件事的 id，没有就 None。

    ⚠️⚠️ **它和 `ensure_conversation_task` 的区别是「会不会让一件事诞生」，
       而那个区别是这一层最要紧的一条边界。**

    · `ensure_conversation_task()` —— 归属物**跨多个 turn**才有意义
      （等待：定义就是「结束这一轮、等触发再回来」）。它有权让一件事诞生。
    · `owner_label()` —— 归属物是**一轮内**的东西
      （排队消息、工具批次、单次动作尝试、一次确认卡）。它只借用已有的那件事。

    🔴 **为什么这条边界不能松**：如果「排队消息进库」也能创建，
       那用户每发一条消息就诞生一件事 —— 那就是 per-exchange，
       而 per-exchange 已被**三条各自独立的结论**否掉
       （scratchpad /「一个 turn 不能代表任务完成」/ per-task 成本）。
       📌 **懒创建的「懒」，全部靠「谁有权触发创建」这张名单守住** ——
          名单一放宽，懒创建就静默退化成 per-exchange，而且不会有任何报错。

    ⭐ 返回 None 完全正常（简单问答压根没有在进行的事），调用方不许因此失败。
    """
    try:
        return current_conversation_task(_get_kernel())
    except Exception:
        return None


def has_live_conversation_work(kernel: RuntimeKernel) -> bool:
    """现在有没有「一件事」值得给模型看 —— **`task_boundary` 的门禁。**

    ⚠️⚠️ **它必须与 `conversation_tasks_for_model()` 是【同一个】条件，
       所以这里直接问那个函数，而不是另写一份等价判断。**
       📌 改造前 `_rt_ongoing_work` 的注释就写着：
          **一个工具和它的事实来源，必须由同一个条件控制** ——
          否则会出现「有工具没事实」或「有事实没工具」两种半截状态，
          而模型在半截状态下会开始猜。
       ⭐ 写成「另写一个等价判断」的版本在今天就已经不等价了：
          注入侧会把「名下空、又不在推进」的那些滤掉，而旧门禁只问
          `current_conversation_task is not None` —— 两者早就能各说各话。

    🔴 **而「至多一个在前台的一件事」让这个缺口从「罕见」变成「必然」**：
       全部被搁置时 `current_conversation_task()` 是 None → 旧门禁判 False →
       `task_boundary` 不注入 → **模型再也回接不了那件事**。
       📌 **一个「回去的动作」，不能由「已经不在那里」来决定给不给** ——
          那正好在唯一需要它的时刻把它拿走。
    """
    return bool(conversation_tasks_for_model(kernel))


def conversation_tasks_for_model(kernel: RuntimeKernel) -> str:
    """给模型看的一段话：你手上有哪些「一件事」在进行。空的就返回空串。

    ⭐ 这是**「边界由模型判」的前提** —— 它得知道现在有哪些，才能说
       「这件事完了」或「这是另一件事」。
       📌 与「终止事实要传给模型」同源：**要模型做判断，
          就得先让它看见判断所需的事实。**
    ⚠️ 只报**有实际影响**的（有活着的归属物、或正在推进）——
       一个没有任何影响的 ACTIVE 记录报出来只是噪音。
       📌 **一个「看起来还没结束」的东西，如果它已经没有任何影响，
          那么把它藏起来比把它杀掉更诚实** ——
          「结束」是个事实声明，而我们并不知道用户会不会回来继续那件事。
    """
    try:
        rows = [r for r in list_active_tasks(kernel)
                if r.kind == TaskKind.CONVERSATION]
        if not rows:
            return ""
        lines = []
        _parked = 0
        for r in rows:
            v = get_task_view(kernel, r.task_id)
            blockers = list(v.blockers) if v else []
            _is_parked = (r.placement == Placement.BACKGROUND)
            # ═══ ⭐⭐⭐ 2026-08-16：这里原来有一条过滤，**已经移除**。═══
            #
            # 原文是「没有归属物、也没在推进 → 不报」，理由写的是
            # 「一个没有任何影响的 ACTIVE 记录报出来只是噪音」。
            #
            # 🔴 **回头看，那条过滤是在替一个 bug 兜底，而那个 bug 今天修掉了。**
            #    它要藏的那些「多余的 ACTIVE 对话记录」，来源只有一个：
            #    `task_boundary(start)` 直接建第二个 FOREGROUND，把旧那件事
            #    变成**回不去也 finish 不掉**的孤儿。过滤让它们不再刷屏，
            #    但它们照旧躺在库里，也照旧只能等 2 小时 TTL。
            #
            # ⭐ 而那条落地之后，**每一条 ACTIVE 对话 Task 都有明确身份**：
            #    要么在前台（至多一个、不变量守着），要么被显式搁置。
            #    孤儿这个类别**不存在了**，于是那条过滤没有了要藏的对象 ——
            #    它只会开始藏**正常的东西**：本套件抓到的就是它把
            #    **前台那件**藏了（两条并存时前台那条恰好符合它的条件），
            #    同时把搁置的报了出来。那正好是反的。
            #
            # 📌 **一条「把没影响的藏起来」的过滤，往往是在掩盖「它们本来就不该
            #    存在」** —— 根因修掉之后它必须跟着移除，否则它藏的会变成正常的东西，
            #    而且**它不会报错，只会让人看不见**。
            # ⚠️ 顺带一句它原来的判据仍然成立、只是不再适用：
            #    「一个看起来还没结束、但已经没有任何影响的东西，藏起来比杀掉更诚实」——
            #    对**没有身份**的记录是对的；而现在没有无身份的记录了。
            b = f"，被 {len(blockers)} 件事挡着" if blockers else ""
            # ⭐⭐⭐ [2026-08-22] **`goal_summary` 空着时，问 blocker 它在等什么。**
            #
            # 🔴 问题经过（两个 bug，一个根）：
            #   ① 老写法把等待的 `reason` 直接**复制**进 `goal_summary`
            #      （`ensure_conversation_task(reason)`）。
            #      实测：等待早就满足了、ping 早跑完了，那条 Task 仍 ACTIVE，
            #      `goal_summary` 逐字写着「still running: ping …」并**每轮注入模型**
            #      —— 用户重置对话之后，Nano 还在说那条命令在后台跑。
            #      📌 **把一个会过期的事实复制成永久的，就是造一个会说谎的字段。**
            #   ② 改成空串之后（那一步是对的），事实段变成 `(没写目标)` ——
            #      模型看得见「有一件事」，却**不知道那是什么事**，
            #      于是它判断不了该不该收。
            #
            # ⭐ 两个都修的办法只有一个：**别复制，去读。**
            #    `Blocker.summary` 就是那条等待自己的 `reason`，而 blocker
            #    **只从还活着的等待里来**（见 `_wait_blockers` 的 SQL 条件）——
            #    等待一结束它自动消失，**结构上不可能变陈旧**。
            # 📌 **一个复制来的事实需要有人负责让它过期；一个读出来的事实不需要。**
            #
            # ⚠️ 只在 `goal_summary` 真的为空时才用它 —— 用户自己开的一件事
            #    有真名字，那个名字比「在等什么」更贴切（它们答的是两个问题：
            #    「这是什么事」vs「它卡在哪」）。📌 一个字段不许表达两个现实。
            _goal = r.goal_summary or ""
            if not _goal and blockers:
                _named = "；".join(
                    (bk.summary or "").strip() for bk in blockers
                    if (bk.summary or "").strip())
                if _named:
                    _goal = f"(等: {_named})"[:200]
            if _is_parked:
                _parked += 1
                # ⚠️ 措辞必须说清「没有人在推进它」——
                #    📌 `placement=BACKGROUND` 只表示「不在用户眼前」，
                #       而模型看到 "background" 会默认「它自己在跑」，
                #       然后向用户汇报一个根本没有发生的进展。
                lines.append(f"  · {r.task_id}: {_goal or '(没写目标)'}{b}"
                             f"   [PARKED - set aside, nothing is working on it]")
            else:
                lines.append(f"  · {r.task_id}: "
                             f"{_goal or '(没写目标)'}{b}   [in front]")
        if not lines:
            return ""
        _tail = ("\nIf one of them is finished (or the user dropped it), say so via "
                 "the task tool. If the user's new request is a DIFFERENT thing, "
                 "start a new one — the old one keeps running unless you finish it.")
        if _parked:
            # ⭐ 只在**真的有搁置的**时候才讲回接怎么用 ——
            #    📌 只注入模型无法从上下文推导出来的那一点，其余全是噪音，
            #       而噪音会训练它忽略注入（那条判据逐字沿用）。
            _tail += ("\nA PARKED one is waiting for you and will not move on its own. "
                      "When you are ready to go back to it, use the task tool with "
                      "action='resume' and its id above. Anything it started "
                      "(background jobs, timed waits) kept running the whole time.")
        return ("[Ongoing work] You currently have these things in progress:\n"
                + "\n".join(lines) + _tail)
    except Exception as e:
        logger.debug(f"[Task] 生成进行中列表失败: {e}")
        return ""
