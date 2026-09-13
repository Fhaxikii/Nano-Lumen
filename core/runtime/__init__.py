# core/runtime/__init__.py
"""
Nano Runtime Kernel + Thin Task Spine。

═══ 这一层是什么 ═══

把散落在 11+ 处的运行时状态收拢成一个有作用域、有所有者、有统一时钟、
有恢复策略、有唯一权威的内核。**不是一个全局大 FSM**（那会状态组合爆炸），
而是「集中权威 + 分域建模」：Kernel 负责"怎么改、能不能改、如何恢复"，
各领域负责"这个实体有哪些合法转移"。

    core/runtime/
      clock.py         统一时钟（可注入 FakeClock）
      store.py         SQLite schema + 事务边界
      kernel.py        Command 唯一写路径 + revision + 幂等 + 不变量
      task.py          Thin Task Spine（blockers 派生，不是字段）
      outbox.py        action outbox + work lease + fencing token
      reconciler.py    level-triggered 收敛（启动同步 + 周期 tick）
      projection.py    只读派生视图 + 展示回执
      toolbatch.py     ToolBatchSpan：一个工具批次的显式生命周期
      interaction.py   统一"需要用户回应的事"
      waitcond.py      "Nano 在等什么"的唯一权威
      oslease.py       OSActivityLease / AuthorizationLease —— "谁在操作这台电脑"
      attempt.py       ActionAttempt —— "当前这一个动作做到哪了"
      inbox.py         durable inbox —— "用户的话永不丢"
      progress.py      进度总线：ref → "它现在在干什么"
      conversation.py  当前会话的可恢复聊天原文

═══ 接线现状 ═══

本层已经是生产路径的一部分：`app.py` / `core/orchestrator.py` / `memory/manager.py` /
`core/mcp_client.py` / `core/registry.py` / `core/os_layer/*` 等十余处在真引用它。

每个旧字段搬过来时都按三步迁移：**旧字段权威 → 内核权威 → 删旧**，中间用 shadow
比对两边的答案（`toolbatch.py` 里那套 `shadow_*` 就是这个模式的样板）。
📌 一段「看起来还在生效、其实早已断线」的描述，代价在认知 ——
   所以搬完之后要回来改掉描述，而不是留着历史版本。

═══ 边界 ═══

Kernel 只约束 **Canonical Runtime State**。绝不扩张到
`_resp_state.current_text` / spinner 帧 / UI 展开折叠 / scroll position /
临时 Markdown 引用 —— 否则它会变成 UI 状态垃圾桶。

"""
from __future__ import annotations

from core.runtime.clock import SYSTEM_CLOCK, ClockProtocol, FakeClock, SystemClock
from core.runtime.kernel import (
    ANY_REVISION,
    Command,
    CommandResult,
    InvariantViolation,
    KernelError,
    RevisionConflict,
    RuntimeKernel,
    TransitionEvent,
    UnknownCommand,
    get_kernel,
    reset_kernel_for_tests,
)
from core.runtime.outbox import (
    ActionRecord,
    ActionStatus,
    FenceRejected,
)
from core.runtime.projection import (
    Channel,
    Projection,
    ProjectionHub,
    ProjectionReceiptStore,
    RuntimeSnapshot,
    snapshot,
)
from core.runtime.reconciler import (
    ReconcileReport,
    reconcile_on_startup,
    reconcile_tick,
    register_startup_step,
    register_tick_step,
)
from core.runtime.store import RuntimeStore
from core.runtime.toolbatch import (
    AbortReason,
    PathTag,
    SpanStatus,
    coverage,
    coverage_report,
)
from core.runtime.task import (
    Blocker,
    Execution,
    Lifecycle,
    Placement,
    TaskKind,
    TaskRecord,
    TaskView,
    TerminalReason,
    register_blocker_provider,
)

__all__ = [
    # clock
    "ClockProtocol", "SystemClock", "FakeClock", "SYSTEM_CLOCK",
    # store
    "RuntimeStore",
    # kernel
    "RuntimeKernel", "Command", "CommandResult", "TransitionEvent", "ANY_REVISION",
    "KernelError", "UnknownCommand", "RevisionConflict", "InvariantViolation",
    "get_kernel", "reset_kernel_for_tests",
    # task
    "TaskRecord", "TaskView", "Blocker", "Lifecycle", "Placement", "Execution",
    "TaskKind", "TerminalReason", "register_blocker_provider",
    # outbox
    "ActionRecord", "ActionStatus", "FenceRejected",
    # toolbatch
    "SpanStatus", "AbortReason", "PathTag", "coverage", "coverage_report",
    # reconciler
    "ReconcileReport", "reconcile_on_startup", "reconcile_tick",
    "register_startup_step", "register_tick_step",
    # projection
    "RuntimeSnapshot", "snapshot", "Projection", "ProjectionHub",
    "ProjectionReceiptStore", "Channel",
]
