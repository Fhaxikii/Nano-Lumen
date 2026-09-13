# core/runtime/kernel.py
"""
Runtime Kernel —— 唯一写路径。

═══ 为什么是 Command 对象 + 注册式 handler，而不是"Kernel 上一堆方法" ═══

目标是"唯一写入口 + 严禁双权威"。如果落成"Kernel 上 20 个方法各自记得校验
revision / 写幂等台账 / 检查不变量"，那**第 21 个方法必然会忘**。

这不是假想——项目吃过一次一模一样的教训：成本闸原计划"下沉到 provider
的四个对外方法"，清点后发现真正发请求的地方有 8 处，其中 3 处在 rag.py 里直接拿
`provider._client.messages.create` 绕过了所有对外方法，而**最贵的那条路（逐页 OCR
最多 100 次 API）恰好在闸外**。结论是"不要在 N 个调用点各加 if，必须收口"。

所以这里收在 `submit()` 一处，固定做六件事，没有旁路：

    1. 查 commands 表 → 幂等（同 command_id 重放直接返回上次结果，不再执行）
    2. BEGIN IMMEDIATE
    3. 校验 expected_revision（乐观并发）
    4. 跑 handler
    5. 校验不变量（跨实体，在同一事务内）
    6. 写 commands 台账 + COMMIT + 推转移事件

以后加命令只写 handler，上面六件白拿。

═══ 关于 revision 的语义 ═══

`expected_revision` 是**乐观并发控制**，不是版本号装饰。三种取值：

    None          创建类命令，或明确不关心当前版本（谨慎使用）
    整数 N        要求目标当前 revision == N，不等则抛 RevisionConflict
    ANY_REVISION  显式声明"我知道我在无脑覆盖"（比 None 更难打错）

既有的不变量要求"revision 不匹配必须拒绝或重新读取"。这里选**拒绝**（抛异常），
因为"重新读取再重试"应该由调用方决定——它可能需要把冲突告诉用户，
Kernel 自己悄悄重试会掩盖并发冲突。

═══ 转移事件队列的定位 ═══

    SQLite      = 权威
    SimpleQueue = invalidate / refresh hint（只降低 UI 延迟，不负责正确性）
    Projection  = 随时可从当前快照重建

**丢光队列不影响正确性**。所以队列用 `queue.SimpleQueue` 而不是
`asyncio.Queue`：写入端可能在事件循环存在之前的普通线程里（同 health.py 的理由）。
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger

from core.runtime.clock import SYSTEM_CLOCK, ClockProtocol
from core.runtime.store import RuntimeStore

# expected_revision 的显式哨兵：比 None 更难打错，且能在日志里区分
# "创建类命令"（None）与"我知道我在覆盖"（ANY_REVISION）。
ANY_REVISION = -1


# ══════════════════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════════════════

class KernelError(RuntimeError):
    """Kernel 层的基类异常。"""


class UnknownCommand(KernelError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"未注册的命令类型: {kind!r}")


class RevisionConflict(KernelError):
    """乐观并发冲突。调用方应重新读取当前状态再决定，不要盲目重试。"""

    def __init__(self, subject: str, expected: int, actual: int | None):
        self.subject, self.expected, self.actual = subject, expected, actual
        super().__init__(
            f"revision 冲突: {subject} 期望 {expected}，实际 {actual}。"
            f"请重新读取当前状态后再决定，不要盲目重试。"
        )


class InvariantViolation(KernelError):
    """跨实体不变量被破坏。事务已回滚。

    这是**代码 bug 的信号**，不是用户输入错误——不变量的意义就是"任何合法命令序列
    都不该让它成立"。所以它不该被吞掉，也不该有 except 分支去"绕过"。
    """

    def __init__(self, name: str, detail: str):
        self.name, self.detail = name, detail
        super().__init__(f"不变量 [{name}] 被破坏: {detail}")


# ══════════════════════════════════════════════════════════════════════════
# Command
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Command:
    """一次状态变更请求。frozen —— handler 不许改它，避免"同一个 command_id
    在重试时携带了不同 payload"这种极难排查的情况。"""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = ""
    expected_revision: Optional[int] = None
    # 目标实体 id。有它才能做 revision 校验；创建类命令留空。
    subject_id: str = ""

    def __post_init__(self):
        if not self.command_id:
            # frozen dataclass 里赋值要绕 object.__setattr__
            object.__setattr__(self, "command_id", "cmd_" + uuid.uuid4().hex[:16])


@dataclass
class CommandResult:
    kind: str
    command_id: str
    data: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False          # True = 幂等命中，本次没有真正执行
    revision: Optional[int] = None  # 执行后目标的新 revision


# ══════════════════════════════════════════════════════════════════════════
# 转移事件
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class TransitionEvent:
    """状态转移通知。只是 refresh hint，不是权威。"""
    kind: str                       # 如 "task.created" / "action.claimed"
    subject_kind: str
    subject_id: str
    revision: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


# handler 签名：(conn, cmd, ctx) -> HandlerOutcome
@dataclass
class HandlerOutcome:
    data: dict[str, Any] = field(default_factory=dict)
    revision: Optional[int] = None
    events: list[TransitionEvent] = field(default_factory=list)


HandlerFn = Callable[[sqlite3.Connection, Command, "HandlerContext"], HandlerOutcome]


@dataclass
class HandlerContext:
    """handler 能拿到的东西。刻意很窄——handler 不该拿到 kernel 本体，
    否则它可能在事务里再调 submit()，撞上"事务不可嵌套"。"""
    now: float
    clock: ClockProtocol


# ══════════════════════════════════════════════════════════════════════════
# 不变量
# ══════════════════════════════════════════════════════════════════════════

# 不变量检查器签名：(conn) -> None，违反则抛 InvariantViolation
InvariantFn = Callable[[sqlite3.Connection], None]


# ══════════════════════════════════════════════════════════════════════════
# RuntimeKernel
# ══════════════════════════════════════════════════════════════════════════

class RuntimeKernel:
    """唯一写路径。

    ⚠️ 它只约束 **Canonical Runtime State**。
    绝不扩张到 `_resp_state.current_text` / spinner 帧 / UI 展开折叠 / scroll position
    / 临时 Markdown 引用 —— 否则 Kernel 会变成 UI 状态垃圾桶。
    """

    def __init__(self, store: RuntimeStore | None = None,
                 clock: ClockProtocol | None = None):
        self._store = store or RuntimeStore()
        self._clock = clock or SYSTEM_CLOCK
        self._handlers: dict[str, HandlerFn] = {}
        self._invariants: list[tuple[str, InvariantFn]] = []
        self._rev_sources: dict[str, tuple[str, str]] = {}
        self._submit_lock = threading.RLock()
        self._transitions: "queue.SimpleQueue[TransitionEvent]" = queue.SimpleQueue()
        # ⭐ 「本次运行」的起点。**用内核自己的时钟**，不是 `time.time()`。
        #    🔴 第一版在 `task.py` 里写了模块级 `_PROCESS_START = time.time()`，
        #       于是 `finished_background_jobs` 拿墙钟去和**FakeClock 写下的
        #       `updated_at`** 比大小 —— 崩溃取证那条测试当场红了（对的）。
        #    📌 **一个模块级的 `time.time()` 是一个看不见的全局依赖** ——
        #       它在有时钟抽象的系统里，等于给同一件事造了第二个时间源。
        self._started_at = self._clock.now()

    @property
    def store(self) -> RuntimeStore:
        return self._store

    @property
    def clock(self) -> ClockProtocol:
        return self._clock

    def now(self) -> float:
        return self._clock.now()

    @property
    def started_at(self) -> float:
        """这个内核实例诞生的时刻 = **「本次运行」的起点**（抽屉的 Finished 段用）。"""
        return self._started_at

    # ── 注册 ─────────────────────────────────────────────────────────────

    def register(self, kind: str) -> Callable[[HandlerFn], HandlerFn]:
        """装饰器形式注册命令 handler。

        重复注册直接抛错而不是覆盖——两个模块注册同名命令一定是 bug，
        静默覆盖会让"到底哪个生效"取决于 import 顺序。
        """
        def _deco(fn: HandlerFn) -> HandlerFn:
            if kind in self._handlers:
                raise KernelError(f"命令 {kind!r} 已被注册，不允许覆盖（检查是否重名）")
            self._handlers[kind] = fn
            return fn
        return _deco

    def register_invariant(self, name: str, fn: InvariantFn) -> None:
        """登记跨实体不变量。在**每个命令提交前**、同一事务内检查。

        刻意做成运行时登记而不是写死在这里：各领域模块会各自加自己的不变量
        （Approval ≤ 5、GUI Task 不可后台化…），写死在 kernel 里会让它反向依赖
        那些领域模块。与 `health.py` 的 `register_probe` 同一范式。
        """
        self._invariants.append((name, fn))

    def register_revision_source(self, subject_kind: str, table: str, pk_column: str) -> None:
        """告诉 Kernel "这类 subject 的 revision 去哪张表读"。

        ⚠️ 这里补的是一个**真缺口**：`_assert_revision` 原来把
        `SELECT revision FROM tasks` 写死了。只有 Task 一种带 revision 的实体时，
        写死看起来无害；但一加上 `interactions`，任何带 `expected_revision` 的
        interaction 命令都会去 `tasks` 里查一个不存在的 id，拿到 `actual=None`，
        然后抛一条**指向错误对象的** RevisionConflict —— 报错信息会把人引到 Task 上。

        `subject_kind` 默认取命令 kind 的第一段（`interaction.answer` → `interaction`）。
        这不是新发明的约定：现有五个领域（task / action / toolbatch / shadow / interaction）
        本来就都是 `<领域>.<动词>`。
        """
        self._rev_sources[subject_kind] = (table, pk_column)

    def registered_commands(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    # ── 提交（唯一写路径）────────────────────────────────────────────────

    def submit(self, cmd: Command) -> CommandResult:
        """执行一条命令。六步固定流程见模块头。

        ⚠️ 进程内用 RLock 串行化。理由不是 SQLite 需要（它自己有文件锁），
        而是**不变量检查需要**：两个命令并发时，各自事务内看到的快照都合法，
        但合并结果可能违反不变量（经典的 write-skew）。
        SQLite 的默认隔离级别挡不住这个，串行化最省心且这里吞吐量根本不是瓶颈。
        """
        handler = self._handlers.get(cmd.kind)
        if handler is None:
            raise UnknownCommand(cmd.kind)

        with self._submit_lock:
            # ── 1. 幂等（不变量 14）───────────────────────────────────────
            # 在事务外先查一次是快路径；真正的保证在事务内的 INSERT（主键冲突）。
            replayed = self._lookup_command(cmd.command_id)
            if replayed is not None:
                logger.debug(f"[Runtime] 命令 {cmd.command_id} 已执行过，返回上次结果（不重复执行）")
                return CommandResult(kind=cmd.kind, command_id=cmd.command_id,
                                     data=replayed, replayed=True)

            now = self._clock.now()
            ctx = HandlerContext(now=now, clock=self._clock)

            with self._store.write_txn() as conn:
                # ── 1b. 事务内再查一次 ────────────────────────────────────
                # 两个线程同时进来时，RLock 已经挡住了；但**跨进程**（将来的
                # 子进程、或用户开了两个 Nano）挡不住，所以事务内必须再确认一次。
                row = conn.execute(
                    "SELECT result_json FROM commands WHERE command_id=?",
                    (cmd.command_id,),
                ).fetchone()
                if row is not None:
                    return CommandResult(kind=cmd.kind, command_id=cmd.command_id,
                                         data=json.loads(row["result_json"]), replayed=True)

                # ── 3. revision 校验 ─────────────────────────────────────
                if cmd.expected_revision is not None and cmd.expected_revision != ANY_REVISION:
                    self._assert_revision(conn, cmd)   # noqa: E501  (见 register_revision_source)

                # ── 4. handler ───────────────────────────────────────────
                outcome = handler(conn, cmd, ctx)

                # ── 5. 不变量（同一事务内，违反则整体回滚）────────────────
                for name, check in self._invariants:
                    check(conn)  # 抛 InvariantViolation → write_txn 自动 ROLLBACK

                # ── 6. 幂等台账 + COMMIT ─────────────────────────────────
                # ⚠️ 必须与业务变更在同一事务。分开写会造成
                # "命令已记但状态没改"或"状态改了但命令没记（重放会再改一次）"。
                # 验收表第 2 项专门验这条。
                conn.execute(
                    "INSERT INTO commands (command_id, kind, applied_at, result_json) VALUES (?,?,?,?)",
                    (cmd.command_id, cmd.kind, now,
                     json.dumps(outcome.data, ensure_ascii=False, default=str)),
                )

            # ── 事务已提交，再推事件 ──────────────────────────────────────
            # 顺序很重要：先 COMMIT 再推。反过来的话，消费者可能在事务还没提交时
            # 就去读 SQLite，读到旧值。丢事件无害（不变量 17），读到旧值有害。
            for ev in outcome.events:
                if not ev.ts:
                    ev.ts = now
                self._transitions.put(ev)

            return CommandResult(kind=cmd.kind, command_id=cmd.command_id,
                                 data=outcome.data, revision=outcome.revision)

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _lookup_command(self, command_id: str) -> Optional[dict[str, Any]]:
        with self._store.read() as conn:
            row = conn.execute(
                "SELECT result_json FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["result_json"])
        except Exception:
            return {}

    def _assert_revision(self, conn: sqlite3.Connection, cmd: Command) -> None:
        if not cmd.subject_id:
            raise KernelError(
                f"命令 {cmd.kind!r} 声明了 expected_revision={cmd.expected_revision} "
                f"但没给 subject_id —— 无法校验版本。"
            )
        subject_kind = cmd.kind.split(".", 1)[0]
        src = self._rev_sources.get(subject_kind)
        if src is None:
            # 宁可响亮地失败：静默退回 tasks 表会抛一条指向错误对象的 RevisionConflict。
            raise KernelError(
                f"命令 {cmd.kind!r} 用了 expected_revision，但领域 {subject_kind!r} "
                f"没有登记 revision 来源。请在该领域的 install() 里调 "
                f"kernel.register_revision_source({subject_kind!r}, <表名>, <主键列>)。"
            )
        table, pk = src
        row = conn.execute(
            f"SELECT revision FROM {table} WHERE {pk}=?", (cmd.subject_id,)
        ).fetchone()
        actual = int(row["revision"]) if row is not None else None
        if actual != cmd.expected_revision:
            raise RevisionConflict(cmd.subject_id, int(cmd.expected_revision), actual)

    # ── 读取端 ───────────────────────────────────────────────────────────

    def drain_transitions(self, limit: int = 128) -> list[TransitionEvent]:
        """一次取完（同 health.py：多个变更要能被归并成一次重画，不是连发 N 次）。"""
        out: list[TransitionEvent] = []
        for _ in range(limit):
            try:
                out.append(self._transitions.get_nowait())
            except queue.Empty:
                break
        return out

    def check_invariants_now(self) -> list[str]:
        """在事务外主动跑一遍全部不变量，返回违反项的名字。

        给两个地方用：① 启动 Reconcile 之后自检 ② shadow 期的对比日志。
        不抛异常——它的用途是"报告"，不是"拦截"。
        """
        broken: list[str] = []
        with self._store.read() as conn:
            for name, check in self._invariants:
                try:
                    check(conn)
                except InvariantViolation as e:
                    broken.append(name)
                    logger.error(f"[Runtime] 不变量自检失败 [{name}]: {e.detail}")
                except Exception as e:      # pragma: no cover
                    logger.error(f"[Runtime] 不变量 [{name}] 检查本身抛异常: {e}")
        return broken


# ══════════════════════════════════════════════════════════════════════════
# 进程级单例
# ══════════════════════════════════════════════════════════════════════════
# 双重检查加锁 —— 与 provider.get_provider() / rag 三个懒加载单例同一范式。
# 那三个被实测抓出过并发初始化竞态，这里同样会被
# UI 线程与后台线程访问，不能写成裸的 `if x is None`。

_kernel: Optional[RuntimeKernel] = None
_kernel_lock = threading.Lock()


def get_kernel() -> RuntimeKernel:
    global _kernel
    if _kernel is not None:
        return _kernel
    with _kernel_lock:
        if _kernel is None:
            k = RuntimeKernel()
            # 领域模块在这里接线。放在单例构造内而不是模块顶层 import，
            # 是为了避免 kernel ← task ← kernel 的循环 import。
            from core.runtime import task as _task
            from core.runtime import outbox as _outbox
            from core.runtime import toolbatch as _toolbatch
            from core.runtime import interaction as _interaction
            from core.runtime import waitcond as _waitcond
            from core.runtime import oslease as _oslease
            from core.runtime import attempt as _attempt
            from core.runtime import inbox as _inbox
            _task.install(k)
            _outbox.install(k)
            _toolbatch.install(k)
            _interaction.install(k)
            _waitcond.install(k)
            _oslease.install(k)
            _attempt.install(k)
            _inbox.install(k)
            # ⚠️ tick 步骤单独登记：`reconciler` 会 import `task`，
            #    在 `task.install()` 里顶层 import 它就是循环依赖（同 oslease 的做法）。
            _task.install_reconcile(k)
            _kernel = k
            logger.info(
                f"[Runtime] Kernel 就绪 · {len(k.registered_commands())} 个命令 "
                f"· db={k.store.path.name}"
            )
    return _kernel


def reset_kernel_for_tests(store: RuntimeStore | None = None,
                           clock: ClockProtocol | None = None) -> RuntimeKernel:
    """仅供测试：换掉进程单例。生产代码不要调。"""
    global _kernel
    with _kernel_lock:
        k = RuntimeKernel(store=store, clock=clock)
        from core.runtime import task as _task
        from core.runtime import outbox as _outbox
        from core.runtime import toolbatch as _toolbatch
        from core.runtime import interaction as _interaction
        from core.runtime import waitcond as _waitcond
        from core.runtime import oslease as _oslease
        from core.runtime import attempt as _attempt
        from core.runtime import inbox as _inbox
        from core.runtime import reconciler as _rec
        _rec.clear_steps_for_tests()
        _task.install(k)
        _outbox.install(k)
        _toolbatch.install(k)
        _interaction.install(k)
        _waitcond.install(k)
        _oslease.install(k)
        _attempt.install(k)
        _inbox.install(k)
        # ⚠️ tick 步骤单独登记：`reconciler` 会 import `task`，
        #    在 `task.install()` 里顶层 import 它就是循环依赖（同 oslease 的做法）。
        _task.install_reconcile(k)
        _kernel = k
    return k
