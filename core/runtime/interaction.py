# core/runtime/interaction.py
"""
Interaction —— 统一"需要用户回应的事"。

═══ 它替掉了什么 ═══

改造前，"等用户回应"这件事在 orchestrator 里有三条各写各的路由劫持：

    _pending_skill                Skill 审计（同意/拒绝）
    _pending_action               OS 风险确认
    _pending_skill_clarification  探索澄清（Explorer 提问）

它们各自把下一条用户消息**从主循环里截走**。三条路径共享同一个致命形状：

    ① 先把 pending 清掉
    ② 再去干活（调 Explorer / 装 Skill / 执行操作）
    ③ 干活失败 → 用户的回答已经不存在了，只能让用户重说一遍

而且因为是"劫持"，模型根本不知道有这么一件事在等着 —— 用户如果说的是别的事，
代码只能按"回答"去解析。

这一版把它们收成一张表 + 一个工具（`answer_open_interaction`）。
关键性质：**先原子落盘用户原话，再去干活**。干活失败，答案还在，下轮可幂等重试。

═══ 两个正交维度 ═══

    mode        INLINE   本轮必须回应，不回应就卡住      （风险确认）
                DEFERRED 可以不理，Nano 继续做别的        （Skill 审计 / 澄清）
    durability  EPHEMERAL  不跨重启
                PERSISTED  跨重启存活

压成一个 kind 就会长出 `INLINE_EPHEMERAL_OS_RISK_CONFIRM` 这种名字，
而且"重启后怎么办"会散落在各处 if 里。

⚠️ EPHEMERAL 的也写进这张表，靠**启动时 purge** 实现"不跨重启"（见 `startup_purge`）。
   多一次 SQLite 写换来的是：模型动态段只有一个数据源，不需要同时问内存和库。

═══ 状态流 ═══

    OPEN
      → 原子保存 answer_verbatim + relation
    ANSWERED
      → 同一轮内调 Explorer / 执行
    RESOLVED

    Explorer 又提了一个新问题：
        旧的 → RESOLVED，**新建一个 OPEN**（不要 ANSWERED → OPEN 回退覆盖）
        理由：旧问题确实已经被回答了。新问题是一个新的交互事实，
        有自己的 id、revision 和展示回执。

    失败 / 崩溃：
        停在 ANSWERED，**答案不丢**，下轮用同一个工具幂等重试。

刻意**不建** `CONTINUATION_PENDING`：既然 continuation 在同一调用栈内立即执行，
它和 ANSWERED 没有可观察的业务差别，只会多一个无意义状态。

⚠️ 但"可重试"必须有明确的消费者，否则 ANSWERED 会变成没人管的永久状态。
   消费者定死为：**下一轮模型**。领域层用 `needs_retry_ids()` 只回答事实（谁要重试），
   措辞由 orchestrator 的 `_build_open_interactions_injection()` 唯一产出（英文）。

═══ relation 只有三个值，没有 UNRELATED ═══

    ANSWER                 就是在回答
    ANSWER_AND_AMENDMENT   回答 + 顺带改需求
    CANCEL                 用户不想要了

`UNRELATED` 是**模型判断**，不是 **Kernel 命令** —— 它对应的正确行为是
"不要调这个工具，正常处理用户的新请求，Interaction 保持 OPEN"。
把它做成工具参数只会产生一次无意义调用、一个无意义 revision，
以及一个"什么也不做"的命令分支。
📌 **一个正确行为是「什么都不做」的选项，不该做成一条命令。**

═══ artifact 绑定 ═══

审批必须钉在**当时那一版**上。只记 `artifact_id` 的话，用户点"同意"时批准的
可能是一个已经被改过的 Skill —— 这是审批语义里最严重的一类错误。
所以开 Interaction 时连 `artifact_revision + artifact_hash` 一起钉死，
落地前用 `verify_artifact()` 复核；不一致就 SUPERSEDE，不允许放行。
"""
from __future__ import annotations

import hashlib
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

class Mode:
    INLINE = "INLINE"
    DEFERRED = "DEFERRED"
    _ALL = frozenset({INLINE, DEFERRED})


class Durability:
    EPHEMERAL = "EPHEMERAL"
    PERSISTED = "PERSISTED"
    _ALL = frozenset({EPHEMERAL, PERSISTED})


class Status:
    OPEN = "OPEN"            # 等用户
    ANSWERED = "ANSWERED"    # 答案已落盘，continuation 待执行/可重试
    RESOLVED = "RESOLVED"    # 问答类完成
    APPROVED = "APPROVED"    # 审批类通过
    REJECTED = "REJECTED"    # 审批类拒绝
    CANCELLED = "CANCELLED"  # 用户取消 / 重启作废
    EXPIRED = "EXPIRED"      # 过了 deadline
    SUPERSEDED = "SUPERSEDED"  # 被新版本取代（artifact 变了 / 提了新问题）

    # "还活着"= 仍然占槽位、仍然要出现在模型动态段里
    _LIVE = frozenset({OPEN, ANSWERED})
    _TERMINAL = frozenset({RESOLVED, APPROVED, REJECTED, CANCELLED, EXPIRED, SUPERSEDED})
    _ALL = _LIVE | _TERMINAL


class Relation:
    """用户这句话与 Interaction 的关系。**只有三个** —— 见模块头。"""
    ANSWER = "ANSWER"
    ANSWER_AND_AMENDMENT = "ANSWER_AND_AMENDMENT"
    CANCEL = "CANCEL"
    _ALL = frozenset({ANSWER, ANSWER_AND_AMENDMENT, CANCEL})


class Kind:
    """领域种类。决定 payload 的形状与 UI 用哪个卡片渲染。"""
    SKILL_CLARIFICATION = "skill_clarification"   # Explorer 提问（DEFERRED/PERSISTED）
    SKILL_AUDIT = "skill_audit"                   # Skill 代码审计（DEFERRED/PERSISTED）
    # Skill 生命周期操作确认：删除 / 禁用 / 启用 / 修改方案（DEFERRED/PERSISTED）
    # ⚠️ 它取代的 `_pending_action` **与 OS 无关**：那个字段名太泛
    #    （"action" 既像 OS 动作也像管理动作），很容易被误读成 "OS 风险确认"。
    #    四个 op 全是 Skill 生命周期操作。
    SKILL_MANAGE = "skill_manage"
    SKILL_SIDE_EFFECT = "skill_side_effect"       # 副作用确认（INLINE/EPHEMERAL）
    OS_RISK = "os_risk"                           # 操作电脑风险确认（INLINE/EPHEMERAL）
    # MCP 生命周期操作确认：只有 delete 需要确认（DEFERRED/PERSISTED）
    # ⚠️ **不复用 SKILL_MANAGE**：本类的 docstring 说得很清楚 ——
    #    kind「决定 payload 的形状与 UI 用哪个卡片渲染」，而 MCP 的 payload
    #    带的是 server 不是 skill。挤进同一个 kind 就得在消费端写
    #    `if payload.get("target_kind") == "mcp"` 这种分流 ——
    #    📌 那正是「一份账本被两个人读」的形状。
    # ⭐ enable / disable / retry **不进这里**：它们可逆，立即执行、无需确认。
    #    只有 delete 改配置且不可撤销。
    MCP_MANAGE = "mcp_manage"
    _ALL = frozenset({SKILL_CLARIFICATION, SKILL_AUDIT, SKILL_MANAGE,
                      SKILL_SIDE_EFFECT, OS_RISK, MCP_MANAGE})


class Slot:
    FOREGROUND = "foreground"   # 置顶卡片，最多 1（数据库唯一索引保证）
    DEFERRED = "deferred"       # 队列，最多 5（不变量保证）
    _ALL = frozenset({FOREGROUND, DEFERRED})


class Resolution:
    DONE = "DONE"
    USER_CANCELLED = "USER_CANCELLED"
    INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"
    ARTIFACT_CHANGED = "ARTIFACT_CHANGED"
    FOLLOW_UP_QUESTION = "FOLLOW_UP_QUESTION"
    DEADLINE = "DEADLINE"


# 槽位上限。foreground 由唯一索引兜底，deferred 由不变量兜底。
MAX_DEFERRED = 5


# ══════════════════════════════════════════════════════════════════════════
# 记录
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class InteractionRecord:
    interaction_id: str
    kind: str
    mode: str
    durability: str
    status: str
    prompt_text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    slot: Optional[str] = None
    owner_task_id: Optional[str] = None
    owner_turn_id: Optional[str] = None
    artifact_kind: Optional[str] = None
    artifact_id: Optional[str] = None
    artifact_revision: Optional[int] = None
    artifact_hash: Optional[str] = None
    answer_verbatim: Optional[str] = None
    relation: Optional[str] = None
    resolution: Optional[str] = None
    superseded_by: Optional[str] = None
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    answered_at: Optional[float] = None
    closed_at: Optional[float] = None
    deadline_at: Optional[float] = None

    @property
    def is_live(self) -> bool:
        return self.status in Status._LIVE

    @property
    def needs_retry(self) -> bool:
        """答案已落盘但 continuation 没跑成。下轮模型要负责重试（见模块头）。"""
        return self.status == Status.ANSWERED

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "InteractionRecord":
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        return cls(
            interaction_id=row["interaction_id"], kind=row["kind"],
            mode=row["mode"], durability=row["durability"], status=row["status"],
            prompt_text=row["prompt_text"] or "", payload=payload, slot=row["slot"],
            owner_task_id=row["owner_task_id"], owner_turn_id=row["owner_turn_id"],
            artifact_kind=row["artifact_kind"], artifact_id=row["artifact_id"],
            artifact_revision=(None if row["artifact_revision"] is None
                               else int(row["artifact_revision"])),
            artifact_hash=row["artifact_hash"],
            answer_verbatim=row["answer_verbatim"], relation=row["relation"],
            resolution=row["resolution"], superseded_by=row["superseded_by"],
            revision=int(row["revision"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            answered_at=row["answered_at"], closed_at=row["closed_at"],
            deadline_at=row["deadline_at"],
        )


def artifact_hash(text: str) -> str:
    """artifact 内容指纹。用 sha256 前 16 位 —— 够防"改了但 revision 没动"，
    不需要抗碰撞攻击（这不是安全边界，是一致性校验）。"""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════
# 读取 API（不经 Kernel）
# ══════════════════════════════════════════════════════════════════════════

def get(kernel: RuntimeKernel, interaction_id: str) -> Optional[InteractionRecord]:
    with kernel.store.read() as conn:
        row = conn.execute("SELECT * FROM interactions WHERE interaction_id=?",
                           (interaction_id,)).fetchone()
    return InteractionRecord.from_row(row) if row else None


def list_live(kernel: RuntimeKernel, kind: str | None = None) -> list[InteractionRecord]:
    """所有还活着的 Interaction。**这是模型动态段与 UI 卡片的唯一来源。**

    ⭐ **排序：最新在前**（2026-08-06 实测之后改的）。

    原来是"foreground 在前，然后按创建时间升序"，于是第 1 条恰好是**最早**那个。
    实测后果：用户说"把 skill 部署吧"（不点名），模型照着清单挑了第 1 条 ——
    也就是最老那个。理由：真实场景下 Nano 跟用户聊了很多轮，最后一轮刚做完一个
    Skill，用户说"ok 部署吧"，指的必然是刚做完那个；反手部署最早那个非常反直觉。

    所以顺序改成 `1 = 最新 / 2 = 次新 / 3 = 最旧`，新的进来占住第 1 位、旧的往后推。
    这样"第一个"这个说法在 UI 与模型两侧都等于"最新那个" —— 也就是用户的自然指代。

    ⚠️ **UI 与模型必须共用这一个排序**（这条原来的理由仍然成立）：
    否则用户说"第一个"时，用户看到的和模型理解的不是同一条。
    ⚠️ **foreground 槽不再影响顺序**：它是容量记账（1 前台 + 5 队列），
    不是展示优先级。把两件事绑在一起正是这个 bug 的来源。
    """
    sql = ("SELECT * FROM interactions WHERE status IN ('OPEN','ANSWERED')")
    args: list[Any] = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    # ⚠️ 平局用 `rowid` 兜底，**不要用 interaction_id** —— 那是随机十六进制，
    # 排出来的顺序是任意的。`rowid` 是真正的插入顺序，后插入的必然更新。
    # 这不只是测试问题：FakeClock 下三条的 `created_at` 完全相同，
    # 而生产里同一秒创建两条也会平局，那时"最新在前"就不成立了。
    sql += " ORDER BY created_at DESC, rowid DESC"
    with kernel.store.read() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [InteractionRecord.from_row(r) for r in rows]


def foreground(kernel: RuntimeKernel) -> Optional[InteractionRecord]:
    """当前置顶卡片。最多一个（唯一索引保证）。"""
    with kernel.store.read() as conn:
        row = conn.execute(
            "SELECT * FROM interactions WHERE slot='foreground' "
            "AND status IN ('OPEN','ANSWERED') LIMIT 1"
        ).fetchone()
    return InteractionRecord.from_row(row) if row else None


def slot_usage(kernel: RuntimeKernel) -> dict[str, int]:
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT slot, COUNT(*) n FROM interactions "
            "WHERE status IN ('OPEN','ANSWERED') AND slot IS NOT NULL GROUP BY slot"
        ).fetchall()
    return {r["slot"]: int(r["n"]) for r in rows}


def verify_artifact(rec: InteractionRecord, current_revision: int | None,
                    current_text: str | None) -> tuple[bool, str]:
    """审批复核的执行点：落地前确认审批钉的还是不是同一版。

    返回 `(是否一致, 原因)`。**不一致时调用方必须停手**，不能"反正用户点了同意"。
    """
    if rec.artifact_id is None:
        return True, ""
    if current_revision is None and current_text is None:
        return False, f"artifact {rec.artifact_id} 已不存在"
    if rec.artifact_revision is not None and current_revision is not None \
            and int(current_revision) != int(rec.artifact_revision):
        return False, (f"artifact {rec.artifact_id} 的 revision 从 "
                       f"{rec.artifact_revision} 变成了 {current_revision}")
    if rec.artifact_hash and current_text is not None:
        now_hash = artifact_hash(current_text)
        if now_hash != rec.artifact_hash:
            return False, (f"artifact {rec.artifact_id} 内容已变更 "
                           f"（{rec.artifact_hash} → {now_hash}）")
    return True, ""


def needs_retry_ids(kernel: RuntimeKernel) -> list[str]:
    """哪些交互停在 ANSWERED（答案已落盘、continuation 没跑成）。

    ═══ 这里【曾经】是 `pending_retry_hint()`，直接产出一段注入文本 ═══

    删掉它的两个理由：

    1. **双权威。** orchestrator 的 `_build_open_interactions_injection()` 已经在产出
       同一件事（含 `ALREADY ANSWERED ... Do NOT ask them to repeat themselves` 那两行）。
       两个函数各写一份"重试提示"，迟早会漂移成两种说法 —— 提示词也适用
       "严禁双权威"，不只状态适用。
    2. **它是中文的，而它的用途就是注入给模型**（违反设计原则 8.5）。
       写的时候它读起来像"给人看的说明"，但判据是
       **这段字符串最终会不会出现在发给模型的 request 里**。

    所以领域层只负责回答事实（谁需要重试），措辞留给唯一的那个注入点。
    """
    return [r.interaction_id for r in list_live(kernel) if r.needs_retry]


# ══════════════════════════════════════════════════════════════════════════
# 命令
# ══════════════════════════════════════════════════════════════════════════

OPEN = "interaction.open"
ANSWER = "interaction.answer"
RESOLVE = "interaction.resolve"
DECIDE = "interaction.decide"
CANCEL = "interaction.cancel"
SUPERSEDE = "interaction.supersede"
EXPIRE = "interaction.expire"


def _touch(conn: sqlite3.Connection, iid: str, now: float) -> int:
    conn.execute("UPDATE interactions SET revision=revision+1, updated_at=? "
                 "WHERE interaction_id=?", (now, iid))
    row = conn.execute("SELECT revision FROM interactions WHERE interaction_id=?",
                       (iid,)).fetchone()
    return int(row["revision"])


def _require(conn: sqlite3.Connection, iid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM interactions WHERE interaction_id=?", (iid,)).fetchone()
    if row is None:
        raise KernelError(f"Interaction 不存在: {iid}")
    return row


def _assert_live(row: sqlite3.Row, what: str) -> None:
    """终态不可逆。与 Task 的不变量 18 同一条理由：
    一个能反复复活的审批，其"用户批准过"这件事就不再是可信记录。"""
    if row["status"] not in Status._LIVE:
        raise InvariantViolation(
            "interaction_terminal_immutable",
            f"Interaction {row['interaction_id']} 已是终态 {row['status']}"
            f"（{row['resolution']}），不允许{what}。要继续请新开一个。",
        )


def _close(conn: sqlite3.Connection, iid: str, status: str,
           resolution: str, now: float, superseded_by: str | None = None) -> None:
    """进终态：清空 slot 并落 closed_at。

    ⚠️ **slot 必须置 NULL**，不能只靠"status 不在 LIVE 里"来腾位置 ——
    前台唯一索引的条件是 `slot='foreground' AND status IN (...)`，
    status 变了索引确实会放行；但 `slot_usage()` 之类的读取端如果漏写条件就会算错。
    显式清空让"占位"只有一种表示法。
    """
    conn.execute(
        "UPDATE interactions SET status=?, resolution=?, slot=NULL, "
        "closed_at=?, superseded_by=? WHERE interaction_id=?",
        (status, resolution, now, superseded_by, iid),
    )


def install(kernel: RuntimeKernel) -> None:
    kernel.register_revision_source("interaction", "interactions", "interaction_id")

    # ── open ─────────────────────────────────────────────────────────────

    @kernel.register(OPEN)
    def _open(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        p = cmd.payload
        kind = p.get("kind", "")
        if kind not in Kind._ALL:
            raise KernelError(f"未知 Interaction kind: {kind!r}，允许: {sorted(Kind._ALL)}")
        mode = p.get("mode", Mode.DEFERRED)
        durability = p.get("durability", Durability.PERSISTED)
        if mode not in Mode._ALL:
            raise KernelError(f"未知 mode: {mode!r}")
        if durability not in Durability._ALL:
            raise KernelError(f"未知 durability: {durability!r}")

        iid = p.get("interaction_id") or ("int_" + uuid.uuid4().hex[:10])

        # ── 槽位分配 ─────────────────────────────────────────────────────
        # INLINE 不占槽：它是模态对话框，不是卡片。占槽会让一次风险确认
        # 白白挤掉一个 Skill 审计卡片。
        slot = p.get("slot", "__auto__")
        if slot == "__auto__":
            if mode == Mode.INLINE:
                slot = None
            else:
                busy = conn.execute(
                    "SELECT 1 FROM interactions WHERE slot='foreground' "
                    "AND status IN ('OPEN','ANSWERED') LIMIT 1"
                ).fetchone()
                slot = Slot.DEFERRED if busy else Slot.FOREGROUND
        if slot is not None and slot not in Slot._ALL:
            raise KernelError(f"未知 slot: {slot!r}")

        conn.execute(
            """INSERT INTO interactions
               (interaction_id, owner_task_id, owner_turn_id, kind, mode, durability,
                status, slot, prompt_text, payload,
                artifact_kind, artifact_id, artifact_revision, artifact_hash,
                revision, created_at, updated_at, deadline_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
            (iid, p.get("owner_task_id"), p.get("owner_turn_id"), kind, mode, durability,
             Status.OPEN, slot, p.get("prompt_text", ""),
             json.dumps(p.get("payload") or {}, ensure_ascii=False, default=str),
             p.get("artifact_kind"), p.get("artifact_id"),
             p.get("artifact_revision"), p.get("artifact_hash"),
             ctx.now, ctx.now, p.get("deadline_at")),
        )
        return HandlerOutcome(
            data={"interaction_id": iid, "slot": slot, "kind": kind, "mode": mode},
            revision=1,
            events=[TransitionEvent("interaction.opened", "interaction", iid, 1,
                                    {"kind": kind, "mode": mode, "slot": slot})],
        )

    # ── answer ───────────────────────────────────────────────────────────

    @kernel.register(ANSWER)
    def _answer(conn, cmd, ctx) -> HandlerOutcome:
        """OPEN → ANSWERED，**原子落盘用户原话**。

        这一步是本模块的核心修复点：改造前的三条路径都是"先清 pending 再干活"，
        干活失败答案就没了。现在答案先落盘，continuation 由调用方在**这条命令返回之后**
        执行；失败就停在 ANSWERED，下轮重试。

        幂等：
            OPEN     + 回答     → 落盘，返回 retry=False（调用方该去执行 continuation）
            ANSWERED + 同一回答 → 不重复写、不 bump revision，返回 retry=True
            ANSWERED + 新回答   → 覆盖（用户又说了一遍且改了口，最新的才是真的）
        """
        iid = cmd.subject_id or cmd.payload.get("interaction_id", "")
        answer = cmd.payload.get("answer_verbatim", "")
        relation = cmd.payload.get("relation", Relation.ANSWER)
        if relation not in Relation._ALL:
            raise KernelError(
                f"未知 relation: {relation!r}，允许: {sorted(Relation._ALL)}。"
                f"（UNRELATED 不是命令 —— 它的含义是【不要调这个工具】。）"
            )
        row = _require(conn, iid)
        _assert_live(row, "回答")

        if row["status"] == Status.ANSWERED and (row["answer_verbatim"] or "") == answer:
            # 同答案重放：这是"上次 continuation 失败、模型按提示重试"的正常路径，
            # 不是错误。不 bump revision —— 什么都没变，UI 不需要重画。
            return HandlerOutcome(
                data={"interaction_id": iid, "retry": True,
                      "relation": row["relation"], "answer_verbatim": answer},
                revision=int(row["revision"]),
            )

        was_answered = row["status"] == Status.ANSWERED
        conn.execute(
            "UPDATE interactions SET status=?, answer_verbatim=?, relation=?, answered_at=? "
            "WHERE interaction_id=?",
            (Status.ANSWERED, answer, relation, ctx.now, iid),
        )
        rev = _touch(conn, iid, ctx.now)
        return HandlerOutcome(
            data={"interaction_id": iid, "retry": False, "relation": relation,
                  "answer_verbatim": answer, "overwrote_previous_answer": was_answered},
            revision=rev,
            events=[TransitionEvent("interaction.answered", "interaction", iid, rev,
                                    {"relation": relation})],
        )

    # ── resolve / decide / cancel / supersede / expire ───────────────────

    @kernel.register(RESOLVE)
    def _resolve(conn, cmd, ctx) -> HandlerOutcome:
        """continuation 成功。ANSWERED → RESOLVED。

        也允许从 OPEN 直接 RESOLVE：Explorer 这一轮自己想明白了、不再需要用户回答。
        """
        iid = cmd.subject_id or cmd.payload.get("interaction_id", "")
        row = _require(conn, iid)
        _assert_live(row, "标记完成")
        reason = cmd.payload.get("resolution", Resolution.DONE)
        _close(conn, iid, Status.RESOLVED, reason, ctx.now)
        rev = _touch(conn, iid, ctx.now)
        return HandlerOutcome(
            data={"interaction_id": iid, "resolution": reason}, revision=rev,
            events=[TransitionEvent("interaction.resolved", "interaction", iid, rev,
                                    {"resolution": reason})],
        )

    @kernel.register(DECIDE)
    def _decide(conn, cmd, ctx) -> HandlerOutcome:
        """审批类终态：APPROVED / REJECTED。

        ⚠️ 这条命令**只记录用户的决定**，不代表可以落地。
        落地前调用方必须再跑一次 `verify_artifact()` ——
        用户点同意与代码真正写盘之间，artifact 可能已经变了。
        """
        iid = cmd.subject_id or cmd.payload.get("interaction_id", "")
        approved = bool(cmd.payload.get("approved", False))
        row = _require(conn, iid)
        _assert_live(row, "做审批决定")
        status = Status.APPROVED if approved else Status.REJECTED
        _close(conn, iid, status, cmd.payload.get("resolution", Resolution.DONE), ctx.now)
        rev = _touch(conn, iid, ctx.now)
        return HandlerOutcome(
            data={"interaction_id": iid, "status": status,
                  "artifact_id": row["artifact_id"],
                  "artifact_revision": row["artifact_revision"],
                  "artifact_hash": row["artifact_hash"]},
            revision=rev,
            events=[TransitionEvent("interaction.decided", "interaction", iid, rev,
                                    {"status": status})],
        )

    @kernel.register(CANCEL)
    def _cancel(conn, cmd, ctx) -> HandlerOutcome:
        iid = cmd.subject_id or cmd.payload.get("interaction_id", "")
        row = _require(conn, iid)
        if row["status"] not in Status._LIVE:
            # 幂等友好：重复取消不算错（重启 purge 与用户点击可能撞上）。
            return HandlerOutcome(
                data={"interaction_id": iid, "already_closed": True,
                      "status": row["status"]},
                revision=int(row["revision"]),
            )
        reason = cmd.payload.get("resolution", Resolution.USER_CANCELLED)
        _close(conn, iid, Status.CANCELLED, reason, ctx.now)
        rev = _touch(conn, iid, ctx.now)
        return HandlerOutcome(
            data={"interaction_id": iid, "resolution": reason}, revision=rev,
            events=[TransitionEvent("interaction.cancelled", "interaction", iid, rev,
                                    {"resolution": reason})],
        )

    @kernel.register(SUPERSEDE)
    def _supersede(conn, cmd, ctx) -> HandlerOutcome:
        """被新版本取代。两种来源：
        ① artifact 变了 —— 旧审批作废，必须重新问
        ② Explorer 又提了新问题 —— 旧的 RESOLVED，这里只用于 artifact 那一路
        """
        iid = cmd.subject_id or cmd.payload.get("interaction_id", "")
        row = _require(conn, iid)
        _assert_live(row, "标记为被取代")
        reason = cmd.payload.get("resolution", Resolution.ARTIFACT_CHANGED)
        _close(conn, iid, Status.SUPERSEDED, reason, ctx.now,
               superseded_by=cmd.payload.get("superseded_by"))
        rev = _touch(conn, iid, ctx.now)
        return HandlerOutcome(
            data={"interaction_id": iid, "resolution": reason,
                  "superseded_by": cmd.payload.get("superseded_by")},
            revision=rev,
            events=[TransitionEvent("interaction.superseded", "interaction", iid, rev,
                                    {"resolution": reason})],
        )

    @kernel.register(EXPIRE)
    def _expire(conn, cmd, ctx) -> HandlerOutcome:
        iid = cmd.subject_id or cmd.payload.get("interaction_id", "")
        row = _require(conn, iid)
        if row["status"] not in Status._LIVE:
            return HandlerOutcome(data={"interaction_id": iid, "already_closed": True},
                                  revision=int(row["revision"]))
        _close(conn, iid, Status.EXPIRED, Resolution.DEADLINE, ctx.now)
        rev = _touch(conn, iid, ctx.now)
        return HandlerOutcome(
            data={"interaction_id": iid}, revision=rev,
            events=[TransitionEvent("interaction.expired", "interaction", iid, rev, {})],
        )

    # ── 不变量 ───────────────────────────────────────────────────────────

    def _inv_deferred_cap(conn: sqlite3.Connection) -> None:
        """DEFERRED 槽最多 5 个。

        为什么是不变量而不是"打开时查一下"：查了再插在并发下会双双通过
        （同 runtime_actions 那条教训）。这里在事务内校验，超了整体回滚。
        foreground 那一个走唯一索引，本函数只兜 deferred。
        """
        n = conn.execute(
            "SELECT COUNT(*) c FROM interactions "
            "WHERE slot='deferred' AND status IN ('OPEN','ANSWERED')"
        ).fetchone()["c"]
        if int(n) > MAX_DEFERRED:
            raise InvariantViolation(
                "interaction_deferred_cap",
                f"DEFERRED 槽有 {n} 个未决交互，上限 {MAX_DEFERRED}。"
                f"（上限的意义是逼迫收敛：积压 6 个待办说明前面的没被处理。）",
            )

    def _inv_foreground_cap(conn: sqlite3.Connection) -> None:
        """与唯一索引重复，是**故意的**。索引挡的是本进程的写，
        不变量还能在 `check_invariants_now()` 里体检出手工改库/旧版本遗留的脏数据。"""
        n = conn.execute(
            "SELECT COUNT(*) c FROM interactions "
            "WHERE slot='foreground' AND status IN ('OPEN','ANSWERED')"
        ).fetchone()["c"]
        if int(n) > 1:
            raise InvariantViolation("interaction_foreground_cap",
                                     f"前台槽有 {n} 个未决交互，上限 1")

    def _inv_enums(conn: sqlite3.Connection) -> None:
        bad = conn.execute(
            f"""SELECT interaction_id, kind, mode, durability, status FROM interactions
                WHERE mode       NOT IN ({','.join('?' * len(Mode._ALL))})
                   OR durability NOT IN ({','.join('?' * len(Durability._ALL))})
                   OR status     NOT IN ({','.join('?' * len(Status._ALL))})
                LIMIT 1""",
            (*sorted(Mode._ALL), *sorted(Durability._ALL), *sorted(Status._ALL)),
        ).fetchone()
        if bad is not None:
            raise InvariantViolation(
                "interaction_enums",
                f"Interaction {bad['interaction_id']} 有非法枚举值: "
                f"mode={bad['mode']} durability={bad['durability']} status={bad['status']}",
            )

    def _inv_closed_holds_no_slot(conn: sqlite3.Connection) -> None:
        """终态不许还占着槽。见 `_close()` 的注释：占位只能有一种表示法。"""
        row = conn.execute(
            "SELECT interaction_id, status, slot FROM interactions "
            "WHERE slot IS NOT NULL AND status NOT IN ('OPEN','ANSWERED') LIMIT 1"
        ).fetchone()
        if row is not None:
            raise InvariantViolation(
                "interaction_closed_holds_no_slot",
                f"Interaction {row['interaction_id']} 已是 {row['status']} "
                f"却仍占着 {row['slot']} 槽",
            )

    kernel.register_invariant("interaction_deferred_cap", _inv_deferred_cap)
    kernel.register_invariant("interaction_foreground_cap", _inv_foreground_cap)
    kernel.register_invariant("interaction_enums", _inv_enums)
    kernel.register_invariant("interaction_closed_holds_no_slot", _inv_closed_holds_no_slot)

    # ── blocker provider（插入点在这里，task.py 一行都不用改）──────────
    #
    # 语义澄清：DEFERRED "不阻塞 Nano"，但它**确实阻塞它自己那个 Task**。
    # 两者不矛盾 —— 前者说的是"用户能不能继续聊别的"，后者说的是
    # "这个 Task 能不能被 Reconciler 推进"。Task 级 blocker 就该包含 DEFERRED。

    def _interaction_blockers(conn: sqlite3.Connection, task_id: str) -> list[Blocker]:
        rows = conn.execute(
            "SELECT interaction_id, kind, mode, status, prompt_text FROM interactions "
            "WHERE owner_task_id=? AND status IN ('OPEN','ANSWERED')",
            (task_id,),
        ).fetchall()
        return [
            Blocker(blocker_kind="interaction", blocker_id=r["interaction_id"],
                    summary=(r["prompt_text"] or "")[:80],
                    detail={"kind": r["kind"], "mode": r["mode"], "status": r["status"]})
            for r in rows
        ]

    register_blocker_provider("interaction", _interaction_blockers)

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
            "FROM interactions WHERE owner_task_id=?", (task_id,)).fetchone()
        return float((r["t"] if r else 0) or 0.0)

    _task_mod.register_activity_provider("interaction", _activity)

    # ── Reconcile 接线 ───────────────────────────────────────────────────
    from core.runtime import reconciler as _rec

    def _startup(k: RuntimeKernel, report) -> None:
        n = startup_purge(k)
        if n:
            report.extra["interactions_purged"] = n

    def _tick(k: RuntimeKernel, report) -> None:
        n = expire_tick(k)
        if n:
            report.extra["interactions_expired"] = n

    _rec.register_startup_step("interaction_purge", _startup)
    _rec.register_tick_step("interaction_expire", _tick)


# ══════════════════════════════════════════════════════════════════════════
# Reconcile 步骤
# ══════════════════════════════════════════════════════════════════════════

def startup_purge(kernel: RuntimeKernel) -> int:
    """启动时作废所有 EPHEMERAL 的活跃交互 —— 这就是 EPHEMERAL 的物理实现。

    为什么不是"根本不存"：存了才能让模型动态段只有一个数据源。
    代价只是每次风险确认多一次 SQLite 写，而收益是不用维护
    "内存里还有几个 + 库里还有几个"这种必然会不同步的双份账。

    ⚠️ 走 Kernel 命令而不是直接 UPDATE。一条 UPDATE 更快，但会绕过幂等台账、
    不变量校验和转移事件 —— 那正是"唯一写路径"要禁止的旁路。
    """
    from core.runtime.kernel import Command as _Cmd
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT interaction_id FROM interactions "
            "WHERE durability=? AND status IN ('OPEN','ANSWERED')",
            (Durability.EPHEMERAL,),
        ).fetchall()
    n = 0
    for r in rows:
        try:
            kernel.submit(_Cmd(kind=CANCEL, subject_id=r["interaction_id"],
                               payload={"interaction_id": r["interaction_id"],
                                        "resolution": Resolution.INTERRUPTED_BY_RESTART}))
            n += 1
        except Exception as e:      # pragma: no cover
            logger.error(f"[Runtime] 作废 EPHEMERAL 交互 {r['interaction_id']} 失败: {e}")
    if n:
        logger.info(f"[Runtime] 启动作废 {n} 个 EPHEMERAL 交互（重启即失效）")
    return n


def expire_tick(kernel: RuntimeKernel) -> int:
    """level-triggered：每次重新算谁过期了，不依赖任何定时器回调。

    没有 deadline_at 的交互**永远不过期** —— 没设期限就是真的没有期限，
    Skill 审计放三天再处理完全合法。
    📌 **积压不是错误状态**：这一层只负责「到点的算过期」，
       「多久算太久」是调用方设 `deadline_at` 时的决定，不该由这里替它兜。
    """
    from core.runtime.kernel import Command as _Cmd
    now = kernel.now()
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT interaction_id FROM interactions "
            "WHERE deadline_at IS NOT NULL AND deadline_at <= ? "
            "AND status IN ('OPEN','ANSWERED')",
            (now,),
        ).fetchall()
    n = 0
    for r in rows:
        try:
            kernel.submit(_Cmd(kind=EXPIRE, subject_id=r["interaction_id"],
                               payload={"interaction_id": r["interaction_id"]}))
            n += 1
        except Exception as e:      # pragma: no cover
            logger.error(f"[Runtime] 过期交互 {r['interaction_id']} 失败: {e}")
    return n
