# core/runtime/reconciler.py
"""
Reconciler —— level-triggered 收敛。

═══ 为什么是 level-triggered 而不是 edge callback ═══

"不死的挂起"是 edge 丢失的活体标本：`notify_background_done` 是
一次性回调，`_drive_wake` 因为内核忙而早退之后，那个信号就永远没了；而轮询只查
`due_timers()`（SQL 含 `timer_at IS NOT NULL`），捞不到 background-only 的记录。
三条唤醒路径全堵死，记录被注入进每一轮上下文，用户看到一句永远刷不掉的
"我还在挂着等…"，重启也不消失。

level-triggered 的意思是：**每次都重新读当前状态推导该做什么，不依赖"有没有收到通知"。**
所以 `list_ready_tasks()` 是现算的、`reclaim_expired` 是扫表的——丢多少事件都无所谓。

═══ 两段驱动，且第一段不能等事件循环 ═══

    启动一次性  reconcile_on_startup()   在 ui.run() 之前【同步】跑
    周期性      reconcile_tick()         ui.timer(5.0, ...)

第一段为什么不能挂 timer：`WebUI()` 构造早于 `ui.run()`，而 RAG 初始化线程在
`WebUI()` 里就启动了。如果启动恢复要等事件循环，那么"上一次崩溃留下的 RUNNING Task"
在这段窗口里是可见的脏状态，而这段窗口恰好是最容易再出事的地方（同 health.py 与
crash_journal 的理由）。所以它是纯同步函数，`__main__` 里直接调。

═══ 重启恢复对照表 ═══

    RUNNING Turn           → INTERRUPTED_BY_RESTART
    ACTIVE 的 Task（**一律**）→ TERMINAL + INTERRUPTED_BY_RESTART     ← 2026-08-22 改
        （原来豁免 CONVERSATION。⚠️ 终止的是【执行】，不是【这件事】——
          重启后由 Nano 主动问用户要不要重做，见 scheduler.startup_resume_notice）
                             （GUI / 后台任务 / Subagent；对话类**豁免**，它的载体是用户）
    OPEN ToolBatch         → ABORTED + 修复对应 memory pair      ← toolbatch
    OS AuthorizationLease  → REVOKED                            ← oslease
    后台任务无恢复凭据      → ORPHANED / FAILED                   ← waitcond
    已满足但未消费的 Wait   → READY，重新调度                     ← waitcond
    已过期 Wait            → EXPIRED                            ← waitcond

⚠️ **不要把重启前的裸 `active` 直接恢复成"仍在正常运行"。** 这正是现有
`SuspensionStore` 在启动时把所有 active 记录重新提示出来、导致不死记录重启后仍存在的原因。

本模块只做第一行（Task 的 RUNNING）。其余行由各领域模块用 `register_startup_step`
自己插进来 —— 注册机制现在就有且被测试覆盖。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from core.runtime import outbox as _outbox
from core.runtime import task as _task
from core.runtime.kernel import Command, RuntimeKernel


@dataclass
class ReconcileReport:
    """一次 Reconcile 做了什么。给日志、监控卡和测试断言用。"""
    interrupted_tasks: list[str] = field(default_factory=list)
    # ⭐ 被终止的那些活**是什么** —— `[{task_id, kind, goal}]`。
    #    ⚠️ 只有 id 是不够的：重启后要由 Nano 问用户「要不要重做 X」，
    #       而 `task_id` 对用户毫无意义。
    #       📌 **一份「谁被中断了」的报告，如果只给得出 id，
    #          它服务不了任何面向用户的出口。**
    #    ⚠️ 与 `interrupted_tasks` 并存而不是取代它：那个是给日志/断言数数的，
    #       这个是给「说人话」用的 —— 答的不是同一个问题，不合并。
    interrupted_details: list[dict] = field(default_factory=list)
    reclaimed_actions: list[str] = field(default_factory=list)
    ready_tasks: list[str] = field(default_factory=list)
    broken_invariants: list[str] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def did_anything(self) -> bool:
        return bool(self.interrupted_tasks or self.reclaimed_actions)

    def summary(self) -> str:
        parts = []
        if self.interrupted_tasks:
            parts.append(f"中断恢复 {len(self.interrupted_tasks)} 个 Task")
        if self.reclaimed_actions:
            parts.append(f"回收 {len(self.reclaimed_actions)} 条过期 lease")
        if self.ready_tasks:
            parts.append(f"{len(self.ready_tasks)} 个 Task 可推进")
        if self.broken_invariants:
            parts.append(f"⚠️ {len(self.broken_invariants)} 条不变量被破坏")
        # ⭐⭐ 各领域登记的启动步骤只能往 `extra` 里写，而这行摘要原来**完全无视它** ——
        #    于是 attempt / waitcond / oslease / toolbatch / interaction / inbox
        #    六个步骤干了多少活，人在启动日志里一个字都看不到，
        #    只会读到一句「无需处理」。
        # 🔴 这是一条**撒谎的日志**，而且是最坏的那种：**它恰好否定了那个
        #    「注册上去就以为会跑」的东西真的跑了**。
        #    那个「启动步骤签名写错 → 异常被吞 → 静默不跑」的 bug，
        #    正是靠断言 report 内容才发现的；而这行摘要作为人类唯一会读的出口，
        #    却把那份证据丢掉了。
        # 📌 **一个扩展机制要求插件往报告里写，那份报告的摘要就必须认得它们写的东西**
        #    —— 否则扩展点在自己被迫填写的报告里是二等公民。
        # ⚠️ 只报非零项：0 是常态（大多数启动没有遗留），全都列出来会淹掉真信号。
        for k in sorted(self.extra):
            v = self.extra[k]
            if isinstance(v, (int, float)) and not v:
                continue
            parts.append(f"{k}={v}")
        return "；".join(parts) or "无需处理"


# ── 各领域模块的插入点 ────────────────────────────────────────────────────
# (kernel, report) -> None
StartupStep = Callable[[RuntimeKernel, ReconcileReport], None]
TickStep = Callable[[RuntimeKernel, ReconcileReport], None]

_startup_steps: dict[str, StartupStep] = {}
_tick_steps: dict[str, TickStep] = {}


def register_startup_step(name: str, fn: StartupStep) -> None:
    """各领域模块在自己的 install() 里登记重启恢复步骤。
    运行时登记而不是写死在这里——否则 reconciler 会反向依赖那些领域模块。"""
    _startup_steps[name] = fn


def register_tick_step(name: str, fn: TickStep) -> None:
    _tick_steps[name] = fn


def clear_steps_for_tests() -> None:
    _startup_steps.clear()
    _tick_steps.clear()


# ══════════════════════════════════════════════════════════════════════════
# 启动恢复
# ══════════════════════════════════════════════════════════════════════════

def reconcile_on_startup(kernel: RuntimeKernel) -> ReconcileReport:
    """在 `ui.run()` 之前同步调用一次。幂等 —— 连跑两次结果相同。"""
    report = ReconcileReport()

    # ── 1. RUNNING Task → 中断恢复 ────────────────────────────────────────
    # 上一个进程死的时候正在跑的 Task。**不能当成"仍在正常运行"**。
    #
    # ⚠️ 这里的处置是把 execution 打回 IDLE 并解绑 turn，**而不是把 Task 终止**。
    # 理由：Task 的语义是"一件事"，进程死了不代表这件事该放弃——
    # 它可能还要被恢复继续做（那正是 Task 取代 session 的意义）。
    # 真正"被重启打断"的是 **turn**，不是 Task。所以终止的是 turn 绑定，
    # 并把 INTERRUPTED_BY_RESTART 记在事件里供上层决定要不要续。
    #
    # ⚠️⚠️ **2026-08-09 更正：上面那段理由【只对可重建的那一类成立】。**
    #    它写于「唯一接线的 Task 类是 GUI」的时候，而当时**没有任何一类需要终止**，
    #    所以「一律打回 IDLE」看起来对。第二类出现时它就是错的 —— 见下面第 1b 步。
    # 📌 **一段「对当时唯一的那一类正确」的处置逻辑，会在第二类出现时变成错的，
    #    而它不会报错** —— 它只是安静地留下一条不死记录。
    with kernel.store.read() as conn:
        rows = conn.execute(
            "SELECT task_id, revision, current_turn_id FROM tasks WHERE lifecycle=? AND execution=?",
            (_task.Lifecycle.ACTIVE, _task.Execution.RUNNING),
        ).fetchall()
    for row in rows:
        tid = row["task_id"]
        try:
            kernel.submit(Command(
                kind=_task.SET_EXECUTION,
                subject_id=tid,
                payload={"task_id": tid, "execution": _task.Execution.IDLE,
                         "note": "INTERRUPTED_BY_RESTART"},
                command_id=f"cmd_startup_interrupt_{tid}_{int(row['revision'])}",
                # ⚠️ command_id 里带 revision：同一个 Task 在同一 revision 上只会被
                # 恢复一次（幂等），但如果它之后又跑起来又崩了，revision 变了，
                # 新的一次恢复仍然能执行。这正是 health.py 里 generation 解决的同一个问题。
            ))
            kernel.submit(Command(
                kind=_task.SET_TURN, subject_id=tid,
                payload={"task_id": tid, "turn_id": None},
                command_id=f"cmd_startup_unbind_{tid}_{int(row['revision'])}",
            ))
            report.interrupted_tasks.append(tid)
            logger.warning(
                f"[Runtime] Task {tid} 上次进程死时正在 RUNNING → 已打回 IDLE 并解绑 turn "
                f"(turn={row['current_turn_id']})"
            )
        except Exception as e:
            logger.error(f"[Runtime] 恢复 Task {tid} 失败: {e}")

    # ── 1b. ⭐⭐⭐ 执行载体不可重建的 Task → **终止**（2026-08-09 新增）─────
    #
    # 🔴 **这一步补的是一个实测出来的既存缺陷**：GUI Task
    #    在重启后**不死** —— 实测过程与结果：
    #      `open_gui_session()` → Task ACTIVE / execution=IDLE
    #      → 换新内核（模拟重启）→ `reconcile_on_startup` + `startup_release_all`
    #      → **租约收掉了 ✅，Task 仍然 ACTIVE、terminal_reason=None** ❌
    #
    #    为什么第 1 步捞不到它：`open_gui_session` **不置 `execution=RUNNING`**，
    #    而第 1 步的 SQL 只查 `execution=RUNNING`。
    #    📌 **一个「只查某个状态」的恢复步骤，捞不到从来不进那个状态的东西。**
    #
    #    而它为什么再也没人能收：`_finish_gui_task` 的唯一入口是
    #    `close_gui_session`，那需要「当前 GUI 会话」，而**会话租约已经被收掉了**。
    # 📌📌 **一条记录的收尾路径，不许依赖另一条会先它一步消失的记录** ——
    #    这正是 2026-08-04 那两条不死挂起的形状：收尾能力和被收尾对象一起消失了。
    #
    # ═══ 判据：该不该终止，看**执行载体能不能被重建** ═══
    #   · `CONVERSATION` —— **能**。载体是用户，用户会回来接着说那件事。
    #   · `GUI_AUTOMATION` —— **不能**。载体是「那一段正在点鼠标的代码」+ 当时的屏幕状态，
    #      进程一死两样都没了（`startup_release_all` 的 docstring 已经这么判过租约）。
    #   · `BACKGROUND_JOB` / `AGENT` —— **不能**。载体是内存里的 coroutine / 子进程。
    # 📌 **「进程死了这件事该不该放弃」的答案，取决于它的执行载体能不能被重建** ——
    #    不取决于「这件事重要不重要」，也不取决于「它是不是一条记录」。
    #    ⭐ 这与 `oslease.startup_release_all` / `waitcond.startup_sweep` 是**同一条判据**
    #       的第三次应用：**收什么取决于「它的执行体还在不在」。**
    #
    # ⚠️ **fail-safe 方向：名单列的是「可重建的」，其余一律终止。**
    #    所以将来新增一类 Task 时，**默认行为是终止** —— 而那是安全的方向：
    #    · 多终止一个：只丢掉分组（`owner_task_id` 是标签、不主宰生命周期 —— 今天刚立的判据），
    #      它名下的东西照旧按自己的条件失效。
    #    · 少终止一个：**一条不死记录** + 它的 blockers 永远挂在模型眼前。
    #    📌 两边不对称时，名单要写「豁免谁」，不写「惩罚谁」。
    #
    # ═══ 🔴🔴 2026-08-22 改：**豁免名单清空，一律终止** ═══
    #
    # 上面那条「CONVERSATION 能重建，因为载体是用户，用户会回来接着说」——
    # 判据本身没错，但**它答的是「技术上能不能续」，而真正决定这件事的
    # 是「用户还想不想要」** —— 而那个问题，代码没有资格代答。
    #
    # ⚠️⚠️ **这里绕过了一整类做不好的判断，留痕值得读：**
    #
    #   我们真正想要的是分清三种情况：
    #     ① 运行中崩溃 → 该接上   ② 非运行中崩溃 → 不用管   ③ 正常关闭 → 不用管
    #   而它们**在本机上分不开**：
    #     · `crash_journal` 的 breadcrumb 残留，也可能只是用户在那一步中间关了软件
    #     · excepthook 三兄弟只说明「有异常」，不说明进程死了
    #     · 唯一确定的 `sys.excepthook` **恰好抓不到本项目已知的 segfault**
    #       （`crash_journal` 模块头自己写着这三个钩子一个都抓不到）
    #     · 而 `data/crash_journal.json` **至今不存在**（`record_fatal` 90 天零次）
    #
    #   🔴 第一版的处置是：既然分不开，就按「关闭 = 用户放弃本次协同」一律终止。
    #      **这一步是错的** —— 它只是把一个不可靠的推断（崩溃检测）
    #      换成了**另一个同样不可靠的推断**（关闭 = 放弃）。
    #      反例一句话就够：**误触了关闭按钮呢？**
    #
    #   ⭐ 正解是**根本不推断**：
    #      终止执行（这是事实：执行载体随进程消失，技术上确实续不了），
    #      但**把「要不要重新做」交给 Nano 去问用户**。
    #      重启提示（`identity.pending_restart_notice`）里会带上这批被终止的活，
    #      Nano 主动开口问一句 —— 见 `scheduler.startup_resume_notice()`。
    #
    #   📌 **判据（可推广）：当两个推断都不可靠时，不要挑一个更顺眼的，
    #      而要把事实原样交给能处理歧义的那一层。**
    #      这里恰好有这样一层：Nano 问一句、用户答一句。
    #      ⚠️ 第一版之所以没想到，是因为默认了「系统必须自己决定」——
    #         而这个系统里**一直有一个能替它决定的人**。
    #
    #   📌 而且这样天然不需要区分崩溃与否：**温和提醒本身是无害的**
    #      （它只是一段话，不是强制继续），所以「问错了」的代价接近零，
    #      而「猜错了」的代价是丢掉用户真正想接着做的事。
    #
    # ⚠️⚠️ **终止 ≠ 闭嘴。** 这两件事第一版被绑在了一起。
    #    Task 确实该终止（执行体没了），但那不代表这件事不该被提起。
    _RESUMABLE_KINDS: frozenset = frozenset()
    # ⚠️ `NOT IN ()` 在 SQLite 里是语法错误（空括号），所以豁免名单为空时
    #    必须走另一条 SQL。📌 一个「名单可能为空」的查询，空集是它的**正常输入**，
    #    不是边界情况 —— 而拼字符串的写法会在那一刻直接抛 syntax error。
    with kernel.store.read() as conn:
        if _RESUMABLE_KINDS:
            dead_rows = conn.execute(
                "SELECT task_id, revision, kind, goal_summary FROM tasks "
                "WHERE lifecycle=? AND kind NOT IN ({}) ORDER BY created_at ASC, rowid ASC".format(
                    ",".join("?" * len(_RESUMABLE_KINDS))),
                (_task.Lifecycle.ACTIVE, *sorted(_RESUMABLE_KINDS)),
            ).fetchall()
        else:
            dead_rows = conn.execute(
                "SELECT task_id, revision, kind, goal_summary FROM tasks "
                "WHERE lifecycle=? ORDER BY created_at ASC, rowid ASC",
                (_task.Lifecycle.ACTIVE,),
            ).fetchall()
    for row in dead_rows:
        tid = row["task_id"]
        try:
            kernel.submit(Command(
                kind=_task.TERMINATE, subject_id=tid,
                payload={"task_id": tid,
                         # ⚠️ key 必须是 `reason` —— `_terminate` 读的是这个。
                         #    写成 `terminal_reason` 会被静默忽略、然后默认成
                         #    COMPLETED，于是「被重启打断」在历史里长成「做完了」。
                         "reason": _task.TerminalReason.INTERRUPTED_BY_RESTART,
                         # ⚠️ 措辞跟着当前判据走：现在的理由**不是**「载体没了」，
                         #    而是「上一个进程结束 = 这次协同结束」。
                         #    📌 一条 note 是给将来查日志的人看的，
                         #       写着旧理由等于把已经改掉的判据留在现场。
                         "note": "执行载体随上个进程消失；是否重做由 Nano 问用户"},
                # 幂等：同一个 Task 在同一 revision 上只终止一次。
                command_id=f"cmd_startup_kill_{tid}_{int(row['revision'])}",
            ))
            report.interrupted_tasks.append(tid)
            report.interrupted_details.append({
                "task_id": tid,
                "kind": row["kind"],
                "goal": (row["goal_summary"] or "").strip(),
            })
            logger.warning(
                f"[Runtime] Task {tid}（{row['kind']}）随上个进程结束 → "
                f"已终止为 INTERRUPTED_BY_RESTART"
                f"{'：' + (row['goal_summary'] or '')[:40] if row['goal_summary'] else ''}")
        except Exception as e:
            logger.error(f"[Runtime] 终止不可恢复 Task {tid} 失败: {e}")

    # ── 2. lease 过期的 action 打回 PENDING ──────────────────────────────
    _reclaim(kernel, report, tag="startup")

    # ── 3. 各领域模块登记的恢复步骤 ──────────────────────────────────────
    for name, step in list(_startup_steps.items()):
        try:
            step(kernel, report)
        except Exception as e:
            # 一个步骤失败不能让整个启动恢复停下——其余步骤仍然有价值。
            logger.error(f"[Runtime] 启动恢复步骤 {name!r} 失败（继续其余步骤）: {e}")

    # ── 4. 恢复完自检一次不变量 ──────────────────────────────────────────
    # 只报告不拦截。真发现违反说明有代码 bug 或有人手工改过库，要响亮。
    report.broken_invariants = kernel.check_invariants_now()

    logger.debug(f"[Runtime] 启动恢复完成：{report.summary()}")
    return report


# ══════════════════════════════════════════════════════════════════════════
# 周期收敛
# ══════════════════════════════════════════════════════════════════════════

def reconcile_tick(kernel: RuntimeKernel) -> ReconcileReport:
    """周期调用（`ui.timer(5.0, ...)`）。廉价：几条索引扫描，不加载任何模型。

    ⚠️ 它**只推导与回收，不启动业务动作**。"发现 Task READY 之后去启动一个模型 turn"
    是调度器的事，Reconciler 只把它登记成 outbox 里的一条 PENDING。
    这个分工要解决的是崩溃窗口：**先落成事实，再由 worker 认领执行。**
    """
    report = ReconcileReport()
    _reclaim(kernel, report, tag="tick")

    # level-triggered：每次重新算，不管有没有收到过通知
    try:
        report.ready_tasks = [v.record.task_id for v in _task.list_ready_tasks(kernel)]
    except Exception as e:
        logger.error(f"[Runtime] 推导 ready tasks 失败: {e}")

    for name, step in list(_tick_steps.items()):
        try:
            step(kernel, report)
        except Exception as e:
            logger.error(f"[Runtime] tick 步骤 {name!r} 失败（继续其余）: {e}")

    if report.did_anything:
        logger.info(f"[Runtime] tick：{report.summary()}")
    return report


def _reclaim(kernel: RuntimeKernel, report: ReconcileReport, *, tag: str) -> None:
    """回收过期 lease。command_id 带上时间桶，让它在同一秒内幂等、跨秒可重跑。

    ⚠️ 不能用固定 command_id（那样只会执行一次，之后永远命中幂等台账）；
    也不能每次用随机 id（那样 commands 表会无限增长）。
    按整秒取桶是个折中：同一秒重复调用幂等，下一秒重新生效。
    """
    bucket = int(kernel.now())
    try:
        res = kernel.submit(Command(
            kind=_outbox.RECLAIM_EXPIRED,
            payload={},
            command_id=f"cmd_reclaim_{tag}_{bucket}",
        ))
        report.reclaimed_actions = list(res.data.get("reclaimed") or [])
    except Exception as e:
        logger.error(f"[Runtime] 回收过期 lease 失败: {e}")
