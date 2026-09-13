# -*- coding: utf-8 -*-
"""`ActionAttempt` —— 「当前这一个动作做到哪了」。

═══ 为什么缺这一层 ═══

Runtime 已经有：
  · **Task**            —— 这件事要不要做
  · **Interaction**     —— 缺用户一个回答
  · **WaitCondition**   —— 缺世界某个条件
  · **OSActivityLease** —— 谁有资格碰鼠标
  · **ToolBatchSpan**   —— 工具协议闭没闭合
**唯独没有「当前这一个 click / type 做到哪了」。**

于是"用户中途接管 → 恢复"这件事一直缺一句最要紧的话：
**恢复后到底该告诉模型什么。** 只能说"环境可能变了"（含糊），
而不能说"你上一个动作是 `type_text('abc')`，**结果不可信，可能已部分生效**"。

═══════════════════════════════════════════════════════════════════════════
⭐⭐ 两个维度，绝对不许压成一个枚举
═══════════════════════════════════════════════════════════════════════════
```
status       PREPARED → IN_FLIGHT → SUCCEEDED / FAILED / INTERRUPTED
effect_state NONE  /  CONFIRMED  /  PARTIAL_OR_UNKNOWN
```
**理由：GUI 没有 rollback。** Nano 要输入 `abcdef`、已经输入了 `abc` 时用户动手，
那么 `abc` **真的在记事本里了**。把它记成"中断了"就等于宣称"没发生" ——
那是**假事实**，比不记更糟。正确的是 `INTERRUPTED` + `PARTIAL_OR_UNKNOWN`。

📌 一句话说清：**明确 commit boundary，而不是幻想 GUI 具备数据库 rollback。**
📌 与本轮所有判据同源：**一个字段不许表达两个现实。**
   （`_os_task_busy` 想同时表达"在跑"和"该不该自检"、
     `!=RUNNING` 想同时表达"不在跑"和"可以调度"，都是同一个问题。）

═══════════════════════════════════════════════════════════════════════════
能做到 / 不能做到 —— 这是物理限制，不是实现上省事
═══════════════════════════════════════════════════════════════════════════
* ✅ 能：**用户信号到达之后，不再允许新的副作用提交。**
* ❌ 不能：把信号到达**之前**已经产生的副作用倒流撤销。

⭐ 所以能做的是**把原子窗口压小**：
  · `type_text` 尽量一次 paste，而不是逐字符六次 SendInput
  · 点击是"先定坐标 → 查 lease → **一次**提交"（`_SendInputClick` 已经这么做了）
  · 文件写可以真做到"写 temp → 查闸 → atomic replace"
**不同 Action 的原子性不同，但 Runtime 模型统一。**

═══════════════════════════════════════════════════════════════════════════
⚠️ 为什么**不**复用 ToolBatchSpan（很容易走错的一步）
═══════════════════════════════════════════════════════════════════════════
它也有 `PREPARED → OPEN → COMMITTED / ABORTED`，乍看就是"动作原子性"。**不是。**
它表达的是「**tool_calls 有没有写进 Memory、tool_results 有没有配回来**」——
Anthropic 工具协议闭没闭合，存在的意义是解决 MemoryManager 与 Runtime SQLite
之间不能事务的问题。
🔴 **`ToolBatch ABORTED` 绝对不能解释成"现实世界没有发生副作用"** ——
那会造出非常严重的假事实。
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
# 两个正交维度
# ══════════════════════════════════════════════════════════════════════════

class AttemptStatus:
    """这一次尝试**走到哪一步**了。⚠️ 它**不回答**"副作用有没有发生"。"""
    PREPARED  = "PREPARED"     # 参数齐了、闸过了，还没作用于现实
    IN_FLIGHT = "IN_FLIGHT"    # 正在作用于现实（这一段不可回滚）
    SUCCEEDED = "SUCCEEDED"    # 执行器明确说成功了（终态）
    FAILED    = "FAILED"       # 执行器明确说失败了（终态）
    INTERRUPTED = "INTERRUPTED"  # 被打断（用户接管 / 急停 / 进程要走了）（终态）
    _ALL = frozenset({PREPARED, IN_FLIGHT, SUCCEEDED, FAILED, INTERRUPTED})
    _TERMINAL = frozenset({SUCCEEDED, FAILED, INTERRUPTED})


class EffectState:
    """现实世界**到底被改了没有**。⚠️ 与 `status` **完全正交**。

    ⭐ 举例说明为什么必须正交：
      · `SUCCEEDED` + `CONFIRMED`          —— 点了，也确实点上了
      · `FAILED` + `NONE`                  —— 定位就失败了，鼠标压根没动
      · `FAILED` + `PARTIAL_OR_UNKNOWN`    —— 点下去了但结果没验上
      · `INTERRUPTED` + `NONE`             —— 还没提交就被拦住（**最好的情况**）
      · `INTERRUPTED` + `PARTIAL_OR_UNKNOWN` —— 输了一半（**GUI 的常态，且不可撤销**）
    """
    NONE = "NONE"                            # 确定什么都没发生
    CONFIRMED = "CONFIRMED"                  # 确定发生了，且是预期的那样
    PARTIAL_OR_UNKNOWN = "PARTIAL_OR_UNKNOWN"  # 可能部分发生 / 无法确认
    _ALL = frozenset({NONE, CONFIRMED, PARTIAL_OR_UNKNOWN})


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    status: str
    effect_state: str
    action: str = ""
    tool_name: str = ""
    summary: str = ""              # 人话：这一步想干什么
    detail: Optional[dict[str, Any]] = None
    owner_task_id: Optional[str] = None    # 归属的 Task；没有归属时为 None
    owner_turn_id: Optional[str] = None
    reason: str = ""               # 终态原因（谁打断的 / 怎么失败的）
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: Optional[float] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in AttemptStatus._TERMINAL

    @property
    def may_have_changed_the_world(self) -> bool:
        """⭐ 恢复时最该问的那个问题。**未知也算「可能变了」** ——
        fail-safe 方向必须偏向"不敢保证没变"，否则模型会拿旧认知往下走。"""
        return self.effect_state != EffectState.NONE


def _row(r: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=r["attempt_id"], status=r["status"],
        effect_state=r["effect_state"], action=r["action"] or "",
        tool_name=r["tool_name"] or "", summary=r["summary"] or "",
        detail=json.loads(r["detail"]) if r["detail"] else None,
        owner_task_id=r["owner_task_id"], owner_turn_id=r["owner_turn_id"],
        reason=r["reason"] or "", revision=int(r["revision"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
        closed_at=r["closed_at"],
    )


# ══════════════════════════════════════════════════════════════════════════
# 读路径
# ══════════════════════════════════════════════════════════════════════════

def get(kernel: RuntimeKernel, attempt_id: str) -> Optional[AttemptRecord]:
    with kernel.store.read() as conn:
        r = conn.execute("SELECT * FROM action_attempts WHERE attempt_id=?",
                         (attempt_id,)).fetchone()
    return _row(r) if r else None


def in_flight(kernel: RuntimeKernel) -> Optional[AttemptRecord]:
    """**当前正在作用于现实的那一个**（最多一个 —— 有唯一索引兜着）。"""
    with kernel.store.read() as conn:
        r = conn.execute(
            "SELECT * FROM action_attempts WHERE status IN (?,?) "
            # ⚠️ `rowid` 次级键：`created_at` 平局时没有它取到的不是最新那条
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (AttemptStatus.PREPARED, AttemptStatus.IN_FLIGHT)).fetchone()
    return _row(r) if r else None


def last_terminal(kernel: RuntimeKernel,
                  turn_id: Optional[str] = None) -> Optional[AttemptRecord]:
    """最近一个**已结束**的尝试 —— 恢复提示要说的就是它。"""
    sql = ("SELECT * FROM action_attempts WHERE status IN (?,?,?) ")
    args: list[Any] = [AttemptStatus.SUCCEEDED, AttemptStatus.FAILED,
                       AttemptStatus.INTERRUPTED]
    if turn_id:
        sql += "AND owner_turn_id=? "
        args.append(turn_id)
    sql += "ORDER BY closed_at DESC LIMIT 1"
    with kernel.store.read() as conn:
        r = conn.execute(sql, tuple(args)).fetchone()
    return _row(r) if r else None


def describe_for_model(rec: Optional[AttemptRecord]) -> str:
    """给模型看的一段事实。**没有记录就返回空串，不编。**

    ⚠️ 措辞规则（与接管状态条的文案同源）：**只陈述当下为真的事**。
    尤其 `PARTIAL_OR_UNKNOWN` **不许说成"失败了"** ——
    那会让模型以为可以放心重做，而现实里 `abc` 已经在记事本里了。
    """
    if rec is None:
        return ""
    what = rec.summary or rec.action or rec.tool_name or "the previous action"
    if rec.status == AttemptStatus.INTERRUPTED:
        head = f"[Interrupted action] {what}"
        why = f" — {rec.reason}" if rec.reason else ""
        if rec.effect_state == EffectState.NONE:
            return (f"{head}{why}\n"
                    "It was stopped BEFORE it touched anything, so the screen is "
                    "unchanged by it. You may simply do it again.")
        return (f"{head}{why}\n"
                "⚠️ It had already started when it was stopped, so it may have "
                "PARTIALLY taken effect — the result is NOT trustworthy and there is "
                "no undo.\n"
                "Do NOT assume it failed and do NOT blindly repeat it. Observe the "
                "current state first, then decide what is actually left to do.")
    if rec.status == AttemptStatus.FAILED:
        if rec.effect_state == EffectState.NONE:
            return f"[Failed action] {what} — nothing was changed."
        return (f"[Failed action] {what}\n"
                "⚠️ It reported failure but may still have changed something. "
                "Verify before retrying.")
    return ""


# ══════════════════════════════════════════════════════════════════════════
# 运行时接线用的薄封装
#
# ⚠️ 全部**吞异常**：记录动作状态是**观测手段**，它坏掉不许影响 OS 执行本身。
#    📌 同 `oslease` 的 shadow 层那条：观测手段不许反过来影响主流程。
# ══════════════════════════════════════════════════════════════════════════

def _tk_label():
    """归属标签。**这里刻意用 `owner_label` 而不是 `ensure_...`** ——
    本模块的归属物是**一轮内**的东西，不许让一件事因为它诞生（会退化成 per-exchange）。
    """
    try:
        from core.runtime import task as _tk
        return _tk.owner_label()
    except Exception:
        return None


def begin(action: str = "", tool_name: str = "", summary: str = "",
          turn_id: Optional[str] = None, detail: Optional[dict] = None) -> Optional[str]:
    """开一条尝试，返回 `attempt_id`。"""
    try:
        from core.runtime.kernel import get_kernel
        return get_kernel().submit(Command(kind=BEGIN, payload={
            "action": action, "tool_name": tool_name, "summary": summary,
            "owner_turn_id": turn_id, "detail": detail or {},
            # 只贴标签，**不创建** —— 见 `task.owner_label()` 的说明
            "owner_task_id": _tk_label(),
        })).data["attempt_id"]
    except Exception as e:
        logger.debug(f"[Attempt] begin 失败（忽略）: {e}")
        return None


def mark_in_flight_current() -> Optional[str]:
    """把**当前那条**未结束的尝试推进到 `IN_FLIGHT`。

    ⭐ **刻意不要求调用方传 `attempt_id`。** 理由：
    这个调用点在 `dispatch`（OS 执行层），而 attempt 是 orchestrator 建的 ——
    把 id 一层层穿下去意味着"每个中间层都得记得传"，
    📌 那正是本轮反复栽的形状（`_os_task_busy` 靠所有调用点记得配对）。
    **表上有唯一索引保证同一时刻最多一条未结束的**，所以"当前那条"是确定的。
    """
    try:
        from core.runtime.kernel import get_kernel
        k = get_kernel()
        cur = in_flight(k)
        if cur is None or cur.status != AttemptStatus.PREPARED:
            return None
        k.submit(Command(kind=IN_FLIGHT_CMD,
                         payload={"attempt_id": cur.attempt_id}))
        return cur.attempt_id
    except Exception as e:
        logger.debug(f"[Attempt] mark_in_flight 失败（忽略）: {e}")
        return None


def finish(attempt_id: Optional[str], ok: bool,
           effect_state: Optional[str] = None, reason: str = "") -> None:
    """执行器给了明确结果。`effect_state` 不给则按 ok 推（失败 → 不确定）。"""
    if not attempt_id:
        return
    try:
        from core.runtime.kernel import get_kernel
        get_kernel().submit(Command(kind=FINISH, payload={
            "attempt_id": attempt_id, "ok": ok,
            "effect_state": effect_state, "reason": reason}))
    except Exception as e:
        logger.debug(f"[Attempt] finish 失败（忽略）: {e}")


def interrupt(attempt_id: Optional[str], reason: str = "") -> None:
    """被打断。⚠️ `effect_state` **由内核按 commit boundary 推导**，
    调用方不许指定 —— 见 `_interrupt` 的说明。"""
    if not attempt_id:
        return
    try:
        from core.runtime.kernel import get_kernel
        get_kernel().submit(Command(kind=INTERRUPT, payload={
            "attempt_id": attempt_id, "reason": reason}))
    except Exception as e:
        logger.debug(f"[Attempt] interrupt 失败（忽略）: {e}")


# ══════════════════════════════════════════════════════════════════════════
# Command 名
# ══════════════════════════════════════════════════════════════════════════

BEGIN     = "attempt.begin"       # 建一条 PREPARED
IN_FLIGHT_CMD = "attempt.in_flight"   # PREPARED → IN_FLIGHT（要开始碰现实了）
FINISH    = "attempt.finish"      # → SUCCEEDED / FAILED
INTERRUPT = "attempt.interrupt"   # → INTERRUPTED（带 effect_state）


def install(kernel: RuntimeKernel) -> None:
    kernel.register_revision_source("attempt", "action_attempts", "attempt_id")

    @kernel.register(BEGIN)
    def _begin(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        """开一条尝试。⚠️ **同一时刻只允许一条未结束的** —— 有唯一索引兜底。

        ⭐ 开新的之前**顺手把上一条未结束的收成 INTERRUPTED**，
        `effect_state` 取 `PARTIAL_OR_UNKNOWN`（保守）——
        因为"上一条没人收尾"本身就说明我们不知道它发生了什么。
        📌 与 `oslease` 同一条设计：**不依赖任何人记得收尾，下一次开新的时顺手收。**
        """
        p = cmd.payload
        prev = conn.execute(
            "SELECT attempt_id FROM action_attempts WHERE status IN (?,?)",
            (AttemptStatus.PREPARED, AttemptStatus.IN_FLIGHT)).fetchall()
        for r in prev:
            conn.execute(
                "UPDATE action_attempts SET status=?, effect_state=?, reason=?, "
                "closed_at=?, revision=revision+1, updated_at=? WHERE attempt_id=?",
                (AttemptStatus.INTERRUPTED, EffectState.PARTIAL_OR_UNKNOWN,
                 "没有收尾就开了下一条 —— 无法确认它做到哪了",
                 ctx.now, ctx.now, r["attempt_id"]))
            logger.warning(f"[Attempt] {r['attempt_id']} 没收尾就被下一条顶掉，"
                           f"按「可能部分生效」收 —— 这类残留说明有调用点漏了收尾")

        aid = p.get("attempt_id") or ("att_" + uuid.uuid4().hex[:10])
        conn.execute(
            """INSERT INTO action_attempts
               (attempt_id, status, effect_state, action, tool_name, summary,
                detail, owner_task_id, owner_turn_id, reason,
                revision, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'',1,?,?)""",
            (aid, AttemptStatus.PREPARED, EffectState.NONE,
             p.get("action", ""), p.get("tool_name", ""), p.get("summary", ""),
             json.dumps(p.get("detail") or {}, ensure_ascii=False, default=str),
             p.get("owner_task_id"), p.get("owner_turn_id"),
             ctx.now, ctx.now))
        return HandlerOutcome(
            data={"attempt_id": aid}, revision=1,
            events=[TransitionEvent("attempt.begun", "attempt", aid, 1, {})])

    @kernel.register(IN_FLIGHT_CMD)
    def _inflight(conn, cmd, ctx) -> HandlerOutcome:
        """要开始碰现实了。**这一刻之后就不能假装没发生。**"""
        aid = cmd.payload["attempt_id"]
        r = conn.execute("SELECT * FROM action_attempts WHERE attempt_id=?",
                         (aid,)).fetchone()
        if r is None:
            raise KernelError(f"Attempt {aid} 不存在")
        if r["status"] != AttemptStatus.PREPARED:
            # 幂等：已经 IN_FLIGHT 就别报错（重放/重试都可能走到）
            return HandlerOutcome(data={"attempt_id": aid, "already": True},
                                  revision=int(r["revision"]))
        conn.execute("UPDATE action_attempts SET status=?, revision=revision+1, "
                     "updated_at=? WHERE attempt_id=?",
                     (AttemptStatus.IN_FLIGHT, ctx.now, aid))
        rev = int(conn.execute("SELECT revision FROM action_attempts WHERE attempt_id=?",
                               (aid,)).fetchone()[0])
        return HandlerOutcome(data={"attempt_id": aid}, revision=rev)

    @kernel.register(FINISH)
    def _finish(conn, cmd, ctx) -> HandlerOutcome:
        """执行器给了明确结果。⚠️ `effect_state` **必须由调用方显式给** ——
        不给就按 `PARTIAL_OR_UNKNOWN`（保守），**绝不默认 NONE**。
        📌 默认成 NONE 就是"没消息当没发生"，那正是最危险的假事实。
        """
        p = cmd.payload
        aid = p["attempt_id"]
        ok = bool(p.get("ok"))
        eff = p.get("effect_state")
        if eff not in EffectState._ALL:
            eff = (EffectState.CONFIRMED if ok else EffectState.PARTIAL_OR_UNKNOWN)
        st = AttemptStatus.SUCCEEDED if ok else AttemptStatus.FAILED
        r = conn.execute("SELECT status, revision FROM action_attempts WHERE attempt_id=?",
                         (aid,)).fetchone()
        if r is None:
            raise KernelError(f"Attempt {aid} 不存在")
        if r["status"] in AttemptStatus._TERMINAL:
            return HandlerOutcome(data={"attempt_id": aid, "already": True},
                                  revision=int(r["revision"]))
        conn.execute(
            "UPDATE action_attempts SET status=?, effect_state=?, reason=?, "
            "closed_at=?, revision=revision+1, updated_at=? WHERE attempt_id=?",
            (st, eff, p.get("reason", ""), ctx.now, ctx.now, aid))
        rev = int(conn.execute("SELECT revision FROM action_attempts WHERE attempt_id=?",
                               (aid,)).fetchone()[0])
        return HandlerOutcome(
            data={"attempt_id": aid, "status": st, "effect_state": eff}, revision=rev,
            events=[TransitionEvent("attempt.finished", "attempt", aid, rev,
                                    {"status": st, "effect_state": eff})])

    @kernel.register(INTERRUPT)
    def _interrupt(conn, cmd, ctx) -> HandlerOutcome:
        """被打断。**`effect_state` 由「打断发生在提交之前还是之后」决定。**

        ⭐ 判据很简单也很硬：
          · 还是 `PREPARED`（没开始碰现实）→ `NONE`（最好的情况）
          · 已经 `IN_FLIGHT`               → `PARTIAL_OR_UNKNOWN`（**不许写 NONE**）
        📌 这就是"明确 commit boundary"落成代码的样子 ——
           `PREPARED → IN_FLIGHT` 那一步就是 commit boundary。
        """
        p = cmd.payload
        aid = p["attempt_id"]
        r = conn.execute("SELECT * FROM action_attempts WHERE attempt_id=?",
                         (aid,)).fetchone()
        if r is None:
            raise KernelError(f"Attempt {aid} 不存在")
        if r["status"] in AttemptStatus._TERMINAL:
            return HandlerOutcome(data={"attempt_id": aid, "already": True},
                                  revision=int(r["revision"]))
        eff = (EffectState.NONE if r["status"] == AttemptStatus.PREPARED
               else EffectState.PARTIAL_OR_UNKNOWN)
        conn.execute(
            "UPDATE action_attempts SET status=?, effect_state=?, reason=?, "
            "closed_at=?, revision=revision+1, updated_at=? WHERE attempt_id=?",
            (AttemptStatus.INTERRUPTED, eff, p.get("reason", "interrupted"),
             ctx.now, ctx.now, aid))
        rev = int(conn.execute("SELECT revision FROM action_attempts WHERE attempt_id=?",
                               (aid,)).fetchone()[0])
        logger.info(f"[Attempt] {aid} 被打断（{p.get('reason','')}）→ effect={eff}")
        return HandlerOutcome(
            data={"attempt_id": aid, "effect_state": eff}, revision=rev,
            events=[TransitionEvent("attempt.interrupted", "attempt", aid, rev,
                                    {"effect_state": eff})])

    # ── 不变量 ────────────────────────────────────────────────────────────

    def _inv_enums(conn) -> None:
        for r in conn.execute("SELECT attempt_id, status, effect_state FROM action_attempts"):
            if r["status"] not in AttemptStatus._ALL:
                raise InvariantViolation(f"{r['attempt_id']} 的 status 非法: {r['status']}")
            if r["effect_state"] not in EffectState._ALL:
                raise InvariantViolation(
                    f"{r['attempt_id']} 的 effect_state 非法: {r['effect_state']}")

    def _inv_closed(conn) -> None:
        r = conn.execute(
            "SELECT attempt_id FROM action_attempts WHERE status IN (?,?,?) "
            "AND closed_at IS NULL LIMIT 1",
            (AttemptStatus.SUCCEEDED, AttemptStatus.FAILED,
             AttemptStatus.INTERRUPTED)).fetchone()
        if r:
            raise InvariantViolation(f"{r['attempt_id']} 已终态但没有 closed_at")

    kernel.register_invariant("attempt_enums", _inv_enums)
    kernel.register_invariant("attempt_terminal_closed", _inv_closed)

    # ⚠️ **想写但写不了的一条不变量，留痕说明为什么**：
    #    「一条**曾经 IN_FLIGHT 过**、又以 INTERRUPTED 结束的记录，
    #      `effect_state` 不许是 NONE」——
    #    因为那等于宣称"它开始动手了，但世界没被改"，GUI 里没人能保证这件事。
    # 🔴 **但表里没有留「曾经 IN_FLIGHT 过」的痕迹**（status 是单值，不是历史），
    #    所以这条查不出来。近似判（比如看 updated_at > created_at）会误伤
    #    "还在 PREPARED 就被打断、中间改过字段"的合法记录 —— **宁可不写，也不写一条会误报的**。
    # 📌 假阳性是本轮反复设防的那种失败（已栽过四次）：它不漏问题，
    #    但会训练出"这个报警不用看"。
    # ⭐ 真正的保证在 `_interrupt` 里：它**按当前 status 推导** effect_state，
    #    根本不接受调用方传 NONE。**把约束放在唯一的写入口，比事后校验更强。**
    # ⚠️ 只有等表里留下「曾经 IN_FLIGHT 过」的痕迹（比如加一列 `first_in_flight_at`），
    #    这条不变量才写得出来。

    # ── 启动收尾 ──────────────────────────────────────────────────────────

    from core.runtime import reconciler as _rec

    # ⚠️ 签名必须是 `(kernel, report)` —— 第一版写成 `(k)`，而
    #    `reconcile_on_startup` 那个循环包着 `except Exception`，于是
    #    **它静默不跑，什么都不报**。测试断言 `extra` 才发现。
    # 📌 与「`reconcile_tick` 压根没人调」是同一个形状：
    #    **一个"注册上去就以为会跑"的东西，最好有一条断言证明它真的跑了。**
    def _startup(k: RuntimeKernel, report) -> None:
        """上个进程留下的未结束尝试，一律收成 `INTERRUPTED`。

        ⚠️ `effect_state` 按 `_interrupt` 的同一条判据推：
        `PREPARED` → `NONE`；`IN_FLIGHT` → `PARTIAL_OR_UNKNOWN`。
        ⭐ 这正是"进程崩在输入一半"那种情况唯一的记录 ——
           重启后模型能知道"上次那一步结果不可信"，而不是一无所知。
        """
        with k.store.read() as conn:
            rows = conn.execute(
                "SELECT attempt_id FROM action_attempts WHERE status IN (?,?)",
                (AttemptStatus.PREPARED, AttemptStatus.IN_FLIGHT)).fetchall()
        n = 0
        for r in rows:
            try:
                k.submit(Command(kind=INTERRUPT, subject_id=r["attempt_id"],
                                 payload={"attempt_id": r["attempt_id"],
                                          "reason": "上个进程结束时它还没收尾"}))
                n += 1
            except Exception as e:      # pragma: no cover
                logger.error(f"[Attempt] 启动收尾 {r['attempt_id']} 失败: {e}")
        if n:
            logger.info(f"[Attempt] 启动收尾 {n} 条未结束的尝试")
            report.extra["attempts_interrupted"] = n

    _rec.register_startup_step("attempt_startup", _startup)
