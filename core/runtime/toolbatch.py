# core/runtime/toolbatch.py
"""
ToolBatchSpan —— 工具批次的显式生命周期。
用它验证内核，同时给 `_active_tool_batch_open` 做 shadow 对答案。

═══ 为什么拿这个当内核的第一个真实用户 ═══

外部评审建议把这一步拆开、**只先做 ToolBatchSpan**：它 UI 与产品语义最少，
适合做基础设施试金石（能一次验到 command 唯一写入口 / revision / 崩溃恢复 /
四态转移 / invariant / shadow 比对 / FakeClock / Reconciler 全套），
而完整 OS lease 要连带 fencing / 心跳 / mini 投影 / 被动挂起一起想，
提前迁只会先做出一个半成品 lease，之后再重构一次。

═══ ⚠️ 四态，不是两态 ═══

    PREPARED → OPEN → COMMITTED | ABORTED

旧实现是一个布尔值：`True` 表示"tool_calls 已写、tool_results 未写"。
四态多出来的那一档 `PREPARED` 是**必须的**，因为显式 Span 只解决"身份与生命周期"，
**不自动创造跨存储原子性** —— `MemoryManager.storage` 是纯内存 `list`，
与 Runtime SQLite 不是同一个事务资源，而 `validate_tool_turns` 要求
`tool_calls`/`tool_results` 严格相邻且 ID 列表完全一致。

    PREPARED  即将往 memory 写 tool_calls（还没写）
    OPEN      已经写进 memory 了

这一档的唯一意义就是区分"崩在写之前"和"崩在写之后"。

⚠️ **"PREPARED 时检查 memory 有没有 calls，没有则 ABORT"未必一直是真检查。**
`MemoryManager.storage` 是内存投影：只要重启恢复跑到这一步时它还是空的，
这个检查就**恒为"没有"→ 恒 ABORT**。结论恰好正确，但它不是在真的检查。
等对话历史能在这一步之前重放回来，它才变成真检查，那时语义会变。
**别默认它一直在生效。**

═══ shadow 期的铁律：绝不影响真实路径 ═══

对外只暴露 `shadow_*` 那几个函数，它们**吞掉一切异常**。
Span 记错、库锁了、磁盘满了——最坏结果是日志多一行，Nano 的行为一个字节都不变。
这是三步迁移第一步的定义：**旧字段权威，内核只镜像并校验，严禁双写。**
所以本模块从不读也不写 `_active_tool_batch_open`，只把它的值抄下来对答案。
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
    get_kernel,
)

STAGE = "2A"


class SpanStatus:
    PREPARED = "PREPARED"
    OPEN = "OPEN"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    _ALL = frozenset({PREPARED, OPEN, COMMITTED, ABORTED})
    _LIVE = frozenset({PREPARED, OPEN})
    _FINAL = frozenset({COMMITTED, ABORTED})


class AbortReason:
    VALIDATE_FAILED = "VALIDATE_FAILED"          # memory 的 tool pair 校验没过
    EXCEPTION = "EXCEPTION"                      # 批次执行中途抛异常
    TURN_RESET = "TURN_RESET"                    # 上一轮留下的活 Span，被本轮开头发现
    INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"


class PathTag:
    """对应 shadow 覆盖表的行号（表已收工）。
    字符串裸写会拼错，走这里。"""
    NORMAL = "p1_normal"                 # 正常提交
    MULTI_ROUND = "p2_multi_round"       # 单 turn 内多轮（由 round_idx>0 自动判定）
    ERROR_400 = "p3_error_400"           # 400 分支读旧标志
    OS_EARLY_RETURN = "p4_os_early_ret"  # OS dsl_plan 早退
    EXIT_MULTI = "p5_exit_multi"         # exit-tool 同轮多工具（预判会分歧）
    BATCH_EXCEPTION = "p6_exception"     # 批次中途抛异常
    GENERATOR_DROPPED = "p7_gen_dropped" # generator 被丢弃
    RESTART_OPEN = "p8_restart_open"     # 重启时处于 OPEN
    _ALL = (NORMAL, MULTI_ROUND, ERROR_400, OS_EARLY_RETURN,
            EXIT_MULTI, BATCH_EXCEPTION, GENERATOR_DROPPED, RESTART_OPEN)


# ══════════════════════════════════════════════════════════════════════════
# 命令
# ══════════════════════════════════════════════════════════════════════════

PREPARE = "toolbatch.prepare"
OPEN = "toolbatch.open"
COMMIT = "toolbatch.commit"
ABORT = "toolbatch.abort"
OBSERVE = "shadow.observe"


def _live_span_row(conn: sqlite3.Connection, span_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tool_batch_spans WHERE span_id=?", (span_id,)).fetchone()
    if row is None:
        raise KernelError(f"span 不存在: {span_id}")
    return row


def install(kernel: RuntimeKernel) -> None:

    @kernel.register(PREPARE)
    def _prepare(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        p = cmd.payload
        span_id = p.get("span_id") or ("span_" + uuid.uuid4().hex[:12])
        # ⚠️ 这里【不】记 legacy_flag_at_open。
        # PREPARE 发生在 `_active_tool_batch_open = True` 之前（那一行紧跟
        # `memory.add_tool_calls()`），此刻抄下来的必然是 False，会让每条正常路径
        # 都误报分歧。旧标志的采样点必须是 OPEN —— 见 _open() 的注释。
        conn.execute(
            """INSERT INTO tool_batch_spans
                   (span_id, owner_task_id, turn_id, round_idx, status,
                    intended_names, path_tag, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (span_id, p.get("owner_task_id"), p.get("turn_id"), int(p.get("round_idx") or 0),
             SpanStatus.PREPARED,
             json.dumps(list(p.get("intended_names") or []), ensure_ascii=False),
             p.get("path_tag"), ctx.now),
        )
        return HandlerOutcome(
            data={"span_id": span_id},
            events=[TransitionEvent("toolbatch.prepared", "span", span_id,
                                    detail={"path_tag": p.get("path_tag")})],
        )

    @kernel.register(OPEN)
    def _open(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        """CAS PREPARED → OPEN，并记下 memory 里实际的 tool_use_id 列表。

        ⚠️ call_ids 只能在这一步拿到，不能在 PREPARE 时：`memory.add_tool_calls()`
        会给缺 id 的 call 现场生成 id 并返回归一化结果，调用方在它之前拿不到最终 id。
        这是观测期的一处已知妥协——切权威时可以把 id 生成提前到 Kernel，
        那时 PREPARED 就能带上 call_ids，恢复检查也随之变成真检查。

        ⭐ **旧标志的采样点在这里，不在 PREPARE。** 主循环里的顺序是：
            prepare → memory.add_tool_calls() → `_active_tool_batch_open = True` → open
        所以只有 OPEN 这一刻抄下来的值才反映"批次已打开"这个事实。
        （第一版把它放在 PREPARE，结果每条正常路径都误报分歧——写测试时抓到的。）
        """
        span_id = cmd.subject_id or cmd.payload.get("span_id", "")
        row = _live_span_row(conn, span_id)
        if row["status"] != SpanStatus.PREPARED:
            raise InvariantViolation(
                "span_cas_open",
                f"span {span_id} 当前是 {row['status']}，只有 PREPARED 才能转 OPEN",
            )
        _legacy = cmd.payload.get("legacy_flag")
        conn.execute(
            """UPDATE tool_batch_spans
               SET status=?, call_ids=?, opened_at=?, legacy_flag_at_open=?
               WHERE span_id=?""",
            (SpanStatus.OPEN,
             json.dumps(list(cmd.payload.get("call_ids") or []), ensure_ascii=False),
             ctx.now, None if _legacy is None else int(bool(_legacy)), span_id),
        )
        return HandlerOutcome(
            data={"span_id": span_id, "status": SpanStatus.OPEN},
            events=[TransitionEvent("toolbatch.opened", "span", span_id)],
        )

    @kernel.register(COMMIT)
    def _commit(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        span_id = cmd.subject_id or cmd.payload.get("span_id", "")
        row = _live_span_row(conn, span_id)
        if row["status"] != SpanStatus.OPEN:
            raise InvariantViolation(
                "span_cas_commit",
                f"span {span_id} 当前是 {row['status']}，只有 OPEN 才能转 COMMITTED",
            )
        call_ids = json.loads(row["call_ids"] or "[]")
        result_ids = list(cmd.payload.get("result_ids") or [])
        # ⭐ 这条校验是 `validate_tool_turns` 在内核侧的镜像（不变量 9）：
        # COMMITTED 时 calls/results 的 ID 与顺序必须一致。
        # 放在这里而不是全局不变量里，是因为它需要知道"这次提交带来的 result_ids"。
        if call_ids != result_ids:
            raise InvariantViolation(
                "span_ids_match",
                f"span {span_id} 的 call_ids 与 result_ids 不一致：\n"
                f"  calls  ={call_ids}\n  results={result_ids}",
            )
        _legacy = cmd.payload.get("legacy_flag")
        conn.execute(
            """UPDATE tool_batch_spans
               SET status=?, result_ids=?, closed_at=?, legacy_flag_at_close=?
               WHERE span_id=?""",
            (SpanStatus.COMMITTED, json.dumps(result_ids, ensure_ascii=False), ctx.now,
             None if _legacy is None else int(bool(_legacy)), span_id),
        )
        return HandlerOutcome(
            data={"span_id": span_id, "status": SpanStatus.COMMITTED, "n": len(result_ids)},
            events=[TransitionEvent("toolbatch.committed", "span", span_id)],
        )

    @kernel.register(ABORT)
    def _abort(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        span_id = cmd.subject_id or cmd.payload.get("span_id", "")
        row = _live_span_row(conn, span_id)
        if row["status"] in SpanStatus._FINAL:
            # 幂等：已终态就原样返回。ABORT 会从多处被调（validate 失败 / 轮首清理 /
            # 启动恢复），撞车是正常的，不该报错。
            return HandlerOutcome(data={"span_id": span_id, "status": row["status"],
                                        "already_final": True})
        _legacy = cmd.payload.get("legacy_flag")
        conn.execute(
            """UPDATE tool_batch_spans
               SET status=?, abort_reason=?, closed_at=?, legacy_flag_at_close=?
               WHERE span_id=?""",
            (SpanStatus.ABORTED, cmd.payload.get("reason") or AbortReason.EXCEPTION,
             ctx.now, None if _legacy is None else int(bool(_legacy)), span_id),
        )
        return HandlerOutcome(
            data={"span_id": span_id, "status": SpanStatus.ABORTED,
                  "reason": cmd.payload.get("reason")},
            events=[TransitionEvent("toolbatch.aborted", "span", span_id,
                                    detail={"reason": cmd.payload.get("reason")})],
        )

    @kernel.register(OBSERVE)
    def _observe(conn: sqlite3.Connection, cmd: Command, ctx: HandlerContext) -> HandlerOutcome:
        p = cmd.payload
        conn.execute(
            """INSERT INTO shadow_observations (stage, path_tag, diverged, detail, observed_at)
               VALUES (?,?,?,?,?)""",
            (p.get("stage") or STAGE, p.get("path_tag") or "?",
             1 if p.get("diverged") else 0, p.get("detail"), ctx.now),
        )
        return HandlerOutcome(data={"ok": True})

    # ── 不变量 ───────────────────────────────────────────────────────────

    def _inv_one_live_span_per_turn(conn: sqlite3.Connection) -> None:
        """同一个 turn 里最多一个活 Span。

        ⚠️ 这条**不是**"同时只能有一个批次"——ReAct 一个 turn 会跑多轮，每轮一个 Span。
        它守的是"**上一轮的 Span 必须先关掉，才能开下一轮**"。
        旧实现靠一个布尔值天然满足（置 True 两次没人发现），这里让它变成可检测的。
        """
        row = conn.execute(
            """SELECT turn_id, COUNT(*) AS n FROM tool_batch_spans
               WHERE status IN (?,?) AND turn_id IS NOT NULL
               GROUP BY turn_id HAVING n > 1 LIMIT 1""",
            (SpanStatus.PREPARED, SpanStatus.OPEN),
        ).fetchone()
        if row is not None:
            raise InvariantViolation(
                "one_live_span_per_turn",
                f"turn {row['turn_id']} 同时有 {row['n']} 个活 Span（上一轮没关就开了下一轮）",
            )

    kernel.register_invariant("one_live_span_per_turn", _inv_one_live_span_per_turn)

    # ── 启动恢复步骤 ─────────────────────────────────────────────────────

    def _startup_abort_live_spans(k: RuntimeKernel, report) -> None:
        """重启后把所有活 Span 打成 ABORTED。

        ⚠️ 这里**不修 memory 的 tool pair**（重启恢复表里的"+ 修复对应 memory pair"）。
        原因：`MemoryManager.storage` 是内存投影，这一步跑到时它还是空的，没有 pair 可修。
        等对话历史能在这一步之前重放回来，要补上"按 call_ids 找到那对孤儿 tool_calls 并删掉"。
        **现在不写占位代码**——写一个永远进不去的分支比不写更容易误导后来人。
        """
        with k.store.read() as conn:
            rows = conn.execute(
                "SELECT span_id, status, turn_id FROM tool_batch_spans WHERE status IN (?,?)",
                (SpanStatus.PREPARED, SpanStatus.OPEN),
            ).fetchall()
        for r in rows:
            try:
                k.submit(Command(
                    kind=ABORT, subject_id=r["span_id"],
                    payload={"span_id": r["span_id"],
                             "reason": AbortReason.INTERRUPTED_BY_RESTART},
                    command_id=f"cmd_span_restart_abort_{r['span_id']}",
                ))
                report.extra.setdefault("aborted_spans", []).append(r["span_id"])
                logger.warning(
                    f"[Runtime] span {r['span_id']} 上次进程死时处于 {r['status']} → 已 ABORTED"
                    f"（turn={r['turn_id']}）"
                )
                _record(k, PathTag.RESTART_OPEN, diverged=False,
                        detail=f"restart abort from {r['status']}")
            except Exception as e:
                logger.error(f"[Runtime] 恢复 span {r['span_id']} 失败: {e}")

    from core.runtime import reconciler as _rec
    _rec.register_startup_step("toolbatch_abort_live", _startup_abort_live_spans)


# ══════════════════════════════════════════════════════════════════════════
# shadow 门面 —— 唯一对 orchestrator 暴露的东西，吞掉一切异常
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠️ 铁律：**shadow 绝不影响真实路径。** 所以下面每个函数都是
# try/except Exception → logger.debug → return。Span 记错顶多日志多一行。
# 三步迁移第一步的定义就是"旧字段权威，内核只镜像并校验，严禁双写"。

def _record(kernel: RuntimeKernel, path_tag: str, *, diverged: bool, detail: str = "") -> None:
    try:
        kernel.submit(Command(kind=OBSERVE,
                              payload={"stage": STAGE, "path_tag": path_tag,
                                       "diverged": diverged, "detail": detail[:500]}))
        if diverged:
            logger.warning(f"[Shadow] ⚠️ 分歧 [{path_tag}] {detail}")
    except Exception as e:
        logger.debug(f"[Shadow] 记录观测失败（忽略）: {e}")


def _tk_label():
    """归属标签。**这里刻意用 `owner_label` 而不是 `ensure_...`** ——
    本模块的归属物是**一轮内**的东西，不许让一件事因为它诞生（会退化成 per-exchange）。
    """
    try:
        from core.runtime import task as _tk
        return _tk.owner_label()
    except Exception:
        return None


def shadow_prepare(turn_id: str, round_idx: int, intended_names: list[str],
                   path_tag: str) -> Optional[str]:
    """返回 span_id；失败返回 None（调用方必须容忍 None）。

    ⚠️ 不收 legacy_flag —— 此刻旧标志还没被置 True，抄下来只会误报。见 `_open()`。
    """
    try:
        k = get_kernel()
        res = k.submit(Command(kind=PREPARE, payload={
            "turn_id": turn_id, "round_idx": round_idx,
            "intended_names": intended_names, "path_tag": path_tag,
            # 只贴标签，**不创建** —— 见 `task.owner_label()` 的说明
            "owner_task_id": _tk_label(),
        }))
        return res.data.get("span_id")
    except Exception as e:
        logger.debug(f"[Shadow] prepare 失败（忽略）: {e}")
        return None


def shadow_open(span_id: Optional[str], call_ids: list[str],
                legacy_flag: bool | None = None) -> None:
    """CAS 到 OPEN，**并在此刻抄下旧标志的值**（正确的采样点）。"""
    if not span_id:
        return
    try:
        get_kernel().submit(Command(kind=OPEN, subject_id=span_id,
                                    payload={"span_id": span_id, "call_ids": call_ids,
                                             "legacy_flag": legacy_flag}))
    except Exception as e:
        logger.debug(f"[Shadow] open 失败（忽略）: {e}")


def shadow_commit(span_id: Optional[str], result_ids: list[str],
                  path_tag: str, legacy_flag: bool | None) -> None:
    """提交 Span，并**在这里对答案**。

    ⭐ 分歧判据：Span 走到 COMMITTED 意味着"这一轮确实开了又关了一个工具批次"。
    如果此刻旧字段不是 `False`（正常关闭）或它在打开时不是 `True`，就是分歧。

    已知的一处：`orchestrator.py` 里 exit-tool 同轮多工具那段**从头到尾没碰过
    旧字段**，所以那条路径上 `legacy_flag` 在打开与关闭时都会是 `False` →
    Span 记了一次完整批次而旧字段全程静默 → **必然分歧，且是旧实现漏了**。
    """
    if not span_id:
        return
    try:
        k = get_kernel()
        k.submit(Command(kind=COMMIT, subject_id=span_id,
                         payload={"span_id": span_id, "result_ids": result_ids,
                                  "legacy_flag": legacy_flag}))
        with k.store.read() as conn:
            row = conn.execute(
                "SELECT legacy_flag_at_open, legacy_flag_at_close, round_idx "
                "FROM tool_batch_spans WHERE span_id=?", (span_id,)
            ).fetchone()
        at_open = row["legacy_flag_at_open"]
        at_close = row["legacy_flag_at_close"]
        # 观测期：旧字段的正确剧本是"打开时 True、关闭时 False"，偏离就是分歧。
        # ⭐ 两列都是 NULL 表示**旧字段已删除、没有可对答案的对象**。
        # 这时判分歧是错的 —— 那不是"两边不一致"，是"只剩一边"。
        # （拿派生值去对答案等于内核跟自己比，永远一致，纯噪音；所以 facade 已经改成传 None。）
        if at_open is None and at_close is None:
            diverged = False
            detail = ""
        else:
            diverged = not (at_open == 1 and at_close == 0)
            detail = f"legacy at_open={at_open} at_close={at_close}（正确剧本是 1→0）"
        _record(k, path_tag, diverged=diverged, detail=detail if diverged else "")
        # 多轮：round_idx>0 自动给覆盖表第 2 行记一笔
        if row["round_idx"] and int(row["round_idx"]) > 0:
            _record(k, PathTag.MULTI_ROUND, diverged=diverged, detail=detail if diverged else "")
    except Exception as e:
        logger.debug(f"[Shadow] commit 失败（忽略）: {e}")


def shadow_abort(span_id: Optional[str], reason: str,
                 legacy_flag: bool | None, path_tag: str = "") -> None:
    if not span_id:
        return
    try:
        k = get_kernel()
        k.submit(Command(kind=ABORT, subject_id=span_id,
                         payload={"span_id": span_id, "reason": reason,
                                  "legacy_flag": legacy_flag}))
        if path_tag:
            _record(k, path_tag, diverged=False, detail=f"aborted: {reason}")
    except Exception as e:
        logger.debug(f"[Shadow] abort 失败（忽略）: {e}")


def shadow_note_path(path_tag: str, detail: str = "", diverged: bool = False) -> None:
    """给没有 Span 的路径记一笔覆盖（如 400 分支只是读了一下旧标志）。"""
    try:
        _record(get_kernel(), path_tag, diverged=diverged, detail=detail)
    except Exception as e:
        logger.debug(f"[Shadow] note 失败（忽略）: {e}")


def sweep_stale_spans(turn_id_now: str, legacy_flag: bool | None) -> list[str]:
    """轮首调用：把**上一轮遗留的活 Span** 打成 ABORTED 并记覆盖。

    这就是覆盖表第 6/7 行（批次中途抛异常 / generator 被丢弃）的探测器 ——
    那两种情况下 Span 会停在 OPEN，而旧实现是"标志带着 True 活到下一轮的重置"。
    ⭐ 两边都靠"下一轮开头清理"兜底，所以这里正好能对上答案。

    ⚠️ **`legacy_flag` 现在恒为 None** —— 旧字段已经删除。
    参数保留是因为 shadow 观测记录里那一列还有历史数据，而且观测期那批用例
    仍然会显式传 True/False 来验"对答案"这件事本身。
    `None` 的含义是"**没有旧字段可对**"，此时不再判分歧（对着一个不存在的东西
    报"不一致"是没有意义的噪音）。
    """
    out: list[str] = []
    try:
        k = get_kernel()
        with k.store.read() as conn:
            rows = conn.execute(
                """SELECT span_id, status, turn_id FROM tool_batch_spans
                   WHERE status IN (?,?) AND (turn_id IS NULL OR turn_id != ?)""",
                (SpanStatus.PREPARED, SpanStatus.OPEN, turn_id_now),
            ).fetchall()
        for r in rows:
            k.submit(Command(kind=ABORT, subject_id=r["span_id"],
                             payload={"span_id": r["span_id"],
                                      "reason": AbortReason.TURN_RESET,
                                      "legacy_flag": legacy_flag}))
            out.append(r["span_id"])
            # 观测期：旧字段此刻【应该】是 True（上一轮没正常收尾），是 False 就是分歧。
            # 旧字段删除之后 legacy_flag 恒为 None → 没有可对的对象，不判分歧。
            _record(k, PathTag.BATCH_EXCEPTION,
                    diverged=(legacy_flag is False),
                    detail=(f"上一轮遗留 {r['status']} span；"
                            + ("旧字段已删除（⑦），不再对答案"
                               if legacy_flag is None
                               else f"轮首旧标志={legacy_flag}（预期 True）")))
            logger.warning(
                f"[Shadow] 上一轮遗留活 Span {r['span_id']}（{r['status']}）→ ABORTED；"
                f"轮首旧标志={legacy_flag}"
            )
    except Exception as e:
        logger.debug(f"[Shadow] sweep 失败（忽略）: {e}")
    return out


# ══════════════════════════════════════════════════════════════════════════
# 覆盖率报告
# ══════════════════════════════════════════════════════════════════════════

_PATH_LABEL = {
    PathTag.NORMAL: "1 正常提交",
    PathTag.MULTI_ROUND: "2 单 turn 多轮 batch",
    PathTag.ERROR_400: "3 400 分支读旧标志",
    PathTag.OS_EARLY_RETURN: "4 OS dsl_plan 早退",
    PathTag.EXIT_MULTI: "5 exit-tool 同轮多工具（预判会分歧）",
    PathTag.BATCH_EXCEPTION: "6 批次中途抛异常/标志跨轮残留",
    PathTag.GENERATOR_DROPPED: "7 generator 被丢弃",
    PathTag.RESTART_OPEN: "8 重启时处于 OPEN",
}


@dataclass
class CoverageRow:
    path_tag: str
    label: str
    hits: int = 0
    divergences: int = 0
    samples: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.hits == 0:
            return "未覆盖"
        return "有分歧" if self.divergences else "零分歧"


def has_open_span(turn_id: str) -> tuple[bool, bool]:
    """当前这一 turn 里有没有**未关闭**的工具批次。

    返回 `(有没有, 这个答案可不可信)`。

    ═══ 为什么要把"可不可信"一起返回 ═══

    这是三步迁移的第二步：`_active_tool_batch_open` 从**权威**降级成
    从这里派生的只读属性。而它唯一的严肃消费者是 `_clean_damaged_memory`：

        True  → 回滚末尾工具对（tool_calls 已写、tool_results 未完成）
        False → **不回滚**（那是合法历史，删了就是数据损失）

    两个方向的代价**极不对称**：错成 True 会删掉用户合法的历史工具对；
    错成 False 只是留下一个坏 pair，下一轮 400 时 `repair_invalid_tool_turns` 能收。
    所以读失败时必须 **fail-safe 到 False**。

    但"因为库读不出来所以答 False"和"库里确实没有 OPEN 所以答 False"是两件事 ——
    前者是故障，后者是事实。**只回一个 bool 会把故障伪装成事实**，
    那正是这几天反复在修的形状。所以第二个返回值把它们分开，调用方可以按需要报警。

    ⚠️ 只看**当前 turn**：跨 turn 的残留 span 由 `sweep_stale_spans()` 负责收，
    不该让上一轮的残留影响这一轮的回滚判断。
    """
    if not turn_id:
        return False, False
    try:
        k = get_kernel()
        with k.store.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM tool_batch_spans "
                "WHERE turn_id=? AND status IN (?,?) LIMIT 1",
                (turn_id, SpanStatus.PREPARED, SpanStatus.OPEN),
            ).fetchone()
        return (row is not None), True
    except Exception as e:
        logger.error(
            f"[Runtime] 读取 OPEN span 失败（按【没有未完成批次】处理，"
            f"这会跳过回滚而不是误删历史）: {e}"
        )
        return False, False


def coverage(kernel: RuntimeKernel | None = None) -> list[CoverageRow]:
    k = kernel or get_kernel()
    rows: list[CoverageRow] = []
    with k.store.read() as conn:
        for tag in PathTag._ALL:
            r = conn.execute(
                """SELECT COUNT(*) AS n, SUM(diverged) AS d FROM shadow_observations
                   WHERE stage=? AND path_tag=?""", (STAGE, tag)
            ).fetchone()
            samples = [x["detail"] for x in conn.execute(
                """SELECT detail FROM shadow_observations
                   WHERE stage=? AND path_tag=? AND diverged=1 AND detail IS NOT NULL
                   ORDER BY id DESC LIMIT 2""", (STAGE, tag)
            ).fetchall()]
            rows.append(CoverageRow(tag, _PATH_LABEL.get(tag, tag),
                                    int(r["n"] or 0), int(r["d"] or 0), samples))
    return rows


def coverage_report(kernel: RuntimeKernel | None = None) -> str:
    rows = coverage(kernel)
    done = sum(1 for r in rows if r.hits)
    lines = [
        "",
        f"shadow 覆盖表   {done}/{len(rows)} 已覆盖",
        "─" * 72,
        f"{'状态':<8}{'命中':>5}{'分歧':>5}  路径",
        "─" * 72,
    ]
    for r in rows:
        lines.append(f"{r.state:<8}{r.hits:>5}{r.divergences:>5}  {r.label}")
        for s in r.samples:
            # ⚠️ 只用 GBK 装得下的字符。这份报告可能被打到 Windows 控制台里，
            # 而 Windows 控制台默认 GBK —— 用 `↳`(U+21B3) 这类字符会直接抛 UnicodeEncodeError，
            # 让一个纯展示函数变成崩溃点。第一版就踩了这个，冒烟测试时抓到的。
            lines.append(f"{'':>18}  -> {s}")
    lines.append("─" * 72)
    blocked = [r for r in rows if not r.hits]
    if blocked:
        lines.append("待覆盖：" + "、".join(r.label for r in blocked))
    else:
        div = [r for r in rows if r.divergences]
        lines.append("✅ 全部覆盖。" + ("待定性分歧：" + "、".join(r.label for r in div)
                                       if div else "零分歧，可以切权威。"))
    return "\n".join(lines)
