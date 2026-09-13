# core/runtime/projection.py
"""
Projection —— 只读派生视图 + 展示回执。

═══ Projection 不只是"把状态显示出来" ═══

它必须具备四条，否则它仍然只是散落的 UI 回调：

  1. **可从权威状态完整重建** —— 丢光转移队列也能重画
  2. **有自己的 presentation receipt** —— 且回执不能塞进业务本体
  3. **不得反向写业务事实** —— UI 永远不能授予权限或改变事实状态
  4. **UI 重连后可重放当前视图**

═══ 为什么 presented_at 单独立表 ═══

`health.py` 上栽过一次：`HealthState` 里混了 `presented_at` /
`user_acknowledged_at`，把 **UI 通知生命周期**塞进了**状态权威层**。后果是
"这条故障因为 acknowledged=True 就再也不通知，但它其实已经恢复过又复发了"——
`health.py` 只好又发明一个 `generation` 去绕。

这次一开始就分开：回执进 `projection_receipts`，主键含 **subject_revision**。
于是"内容改了"天然等于"新的一次待展示"，不需要额外的代次概念——
**revision 就是 generation**，一个机制干两件事。

═══ 与 SimpleQueue 的关系 ═══

    SQLite      = 权威
    SimpleQueue = invalidate / refresh hint（只降低 UI 延迟，不负责正确性）
    Projection  = 随时可从当前快照重建

所以消费者的正确写法是：**收到任何事件（甚至不看内容）→ 触发一次 rebuild**。
绝不能写成"根据事件里的 detail 增量改 UI"——那样丢一个事件就永久错位。
`health.py` 的 `_health_consumer_tick` 已经是这个范式（"没有新转移也要重画一次…
漏判也能自愈"），这里把它固化成基类契约。
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from core.runtime import outbox as _outbox
from core.runtime import task as _task
from core.runtime.kernel import RuntimeKernel


# ══════════════════════════════════════════════════════════════════════════
# 展示回执
# ══════════════════════════════════════════════════════════════════════════

class Channel:
    """展示渠道。同一个对象可能要在多个地方分别展示，各自独立记回执。"""
    CHAT = "chat"               # 聊天区卡片（emit_chat）
    MONITOR = "monitor"         # 监控面板
    MODAL = "modal"             # 模态弹窗
    MODEL_CONTEXT = "model"     # 注入给模型的动态段


class ProjectionReceiptStore:
    """"已展示过"的回执。**独立于业务表** —— 见模块头。"""

    def __init__(self, kernel: RuntimeKernel):
        self._kernel = kernel

    def mark_presented(self, subject_kind: str, subject_id: str,
                       subject_revision: int, channel: str) -> bool:
        """记一次展示。返回 True = 这是首次（调用方据此决定要不要真的弹）。

        用 `INSERT ... ON CONFLICT DO NOTHING` + `rowcount` 判首次，
        而不是"先查有没有再插"——两个消费者同时展示同一条时，
        后者必须拿到 False，否则会连发两张卡。让唯一主键来定胜负。
        """
        with self._kernel.store.write_txn() as conn:
            cur = conn.execute(
                """INSERT INTO projection_receipts
                       (subject_kind, subject_id, subject_revision, channel, presented_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (subject_kind, subject_id, int(subject_revision), channel,
                 self._kernel.now()),
            )
            return cur.rowcount > 0

    def was_presented(self, subject_kind: str, subject_id: str,
                      subject_revision: int, channel: str) -> bool:
        with self._kernel.store.read() as conn:
            row = conn.execute(
                """SELECT 1 FROM projection_receipts
                   WHERE subject_kind=? AND subject_id=? AND subject_revision=? AND channel=?""",
                (subject_kind, subject_id, int(subject_revision), channel),
            ).fetchone()
        return row is not None

    def forget(self, subject_kind: str, subject_id: str) -> int:
        """删掉某个对象的全部回执（该对象已终态、进入 retention 回收时用）。

        ⚠️ 这是 retention TTL 的一部分，**不是**"让它重新弹一次"的手段。
        要让它重新弹，正确做法是 bump revision——那才是"内容变了"的语义。
        """
        with self._kernel.store.write_txn() as conn:
            cur = conn.execute(
                "DELETE FROM projection_receipts WHERE subject_kind=? AND subject_id=?",
                (subject_kind, subject_id),
            )
            return cur.rowcount


# ══════════════════════════════════════════════════════════════════════════
# 快照（Projection 的唯一输入）
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RuntimeSnapshot:
    """某一时刻的完整 Canonical Runtime State。

    ⭐ **rebuild contract 的核心**：Projection 只能从这个对象派生，
    **不许自己去查 SQLite、也不许读转移事件的 detail**。
    这样"丢光队列也能重建"就是结构上成立的，不靠调用方自觉。
    """
    at: float
    active_tasks: tuple[_task.TaskRecord, ...]
    ready_task_ids: tuple[str, ...]
    open_actions: tuple[_outbox.ActionRecord, ...]

    @property
    def pending_action_count(self) -> int:
        return len(self.open_actions)

    @property
    def foreground_task(self) -> Optional[_task.TaskRecord]:
        """当前前台 Task。不变量 1（最多一个前台 Task 持有 OS lease）的读取侧起点。"""
        for t in self.active_tasks:
            if t.placement == _task.Placement.FOREGROUND:
                return t
        return None


def snapshot(kernel: RuntimeKernel) -> RuntimeSnapshot:
    """读一次完整快照。

    ⚠️ 三次查询不在同一事务里 —— WAL 下每次读各自看到一致快照，但三者之间
    可能有微小偏移（比如 Task 已终止而 action 还开着）。**这是可接受的**：
    Projection 是"随时可重建"的，下一次 tick 就自动纠正；
    而为了完全一致去开读事务会跟写事务抢锁，代价大于收益。
    ⭐ 但**判据不能建在快照的内部一致性上** —— 那属于不变量，要在 Kernel 的写事务里查。
    """
    return RuntimeSnapshot(
        at=kernel.now(),
        active_tasks=tuple(_task.list_active_tasks(kernel)),
        ready_task_ids=tuple(v.record.task_id for v in _task.list_ready_tasks(kernel)),
        open_actions=tuple(_outbox.list_open_actions(kernel)),
    )


# ══════════════════════════════════════════════════════════════════════════
# Projection 基类
# ══════════════════════════════════════════════════════════════════════════

class Projection(ABC):
    """所有派生视图的基类。

    子类只实现 `rebuild(snapshot)`。**不要**实现"处理某个事件"的方法——
    那会诱导增量更新，而增量更新在丢事件时会永久错位。
    """

    name: str = "projection"

    @abstractmethod
    def rebuild(self, snap: RuntimeSnapshot) -> None:
        """从快照完整重建。必须幂等，必须能在任意时刻被调用（含 UI 刚重连时）。"""

    def on_refresh_hint(self, snap: RuntimeSnapshot) -> None:
        """收到任何转移事件时调用。默认就是无脑 rebuild —— 这是刻意的。

        ⚠️ 不许在子类里改成"根据事件类型做增量更新"。
        `health.py` 的实践已经证明这个范式是对的：它的消费者"没有新转移也要重画一次"，
        注释写的理由是"万一哪条路径漏了健康判断，1 秒内会被这里纠正回来（自愈，不靠调用方自觉）"。
        """
        self.rebuild(snap)


class ProjectionHub:
    """一组 Projection 的驱动器。UI 侧只需要调 `tick()`。"""

    def __init__(self, kernel: RuntimeKernel):
        self._kernel = kernel
        self._projections: list[Projection] = []

    def add(self, p: Projection) -> None:
        self._projections.append(p)

    def tick(self) -> None:
        """drain 事件 → 取一次快照 → 全部 rebuild。

        ⭐ 注意 drain 的结果**被刻意丢弃**（只用它的"有没有"）。
        这不是省事，是那条不变量的落地：队列只是 refresh hint，
        内容一律从快照来。真丢了事件，下面那个"无条件也重画"兜住。
        """
        events = self._kernel.drain_transitions()
        if not self._projections:
            return
        snap = snapshot(self._kernel)
        for p in self._projections:
            try:
                if events:
                    p.on_refresh_hint(snap)
                else:
                    p.rebuild(snap)
            except Exception as e:
                # 一个 projection 画崩了不能连累其它的（更不能连累内核）
                logger.warning(f"[Runtime] projection {p.name!r} 重建失败: {e}")
