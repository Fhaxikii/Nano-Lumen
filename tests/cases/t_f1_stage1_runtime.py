# -*- coding: utf-8 -*-
"""验收测试。

两类用例：
  · 进程内 幂等 / revision / 不变量 / blockers 派生 / Projection 重建
  · 子进程崩溃 在真实转移点 os._exit，父进程验证重启后的 Reconcile 结果

⚠️ 全程 FakeClock，零 sleep。lease 过期靠"父进程 FakeClock 起点晚于子进程"实现——
   lease_until 是落盘的绝对时间戳，注入时钟就能让"已经过期"成为确定事实。

用法：
  python t_stage1.py
  python t_stage1.py --child <crash_point> <db> <base_time>    （父进程内部调用）
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]   # 不写死绝对路径，换机器/改目录名都能跑
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="ERROR")   # 测试期只看错误

from core.runtime import (
    ANY_REVISION, Command, Execution, FakeClock, InvariantViolation, Lifecycle,
    Placement, Projection, ProjectionHub, ProjectionReceiptStore, RevisionConflict,
    RuntimeStore, TaskKind, TerminalReason, reconcile_on_startup, reconcile_tick,
    register_blocker_provider, reset_kernel_for_tests, snapshot,
)
from core.runtime import outbox as ob
from core.runtime import task as tk
from core.runtime.projection import Channel

BASE_T = 1_800_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


_stores: list = []


def boot(db: pathlib.Path, t: float):
    """建一个独立的 Kernel（独立 db + FakeClock）。"""
    tk.clear_blocker_providers_for_tests()
    clock = FakeClock(t)
    st = RuntimeStore(db)
    _stores.append(st)
    k = reset_kernel_for_tests(store=st, clock=clock)
    return k, clock


def close_all_stores() -> None:
    """Windows 上不关连接就删不掉 tempdir（WinError 32）。"""
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


# ══════════════════════════════════════════════════════════════════════════
# 子进程崩溃注入
# ══════════════════════════════════════════════════════════════════════════

def _arm_crash(conn: sqlite3.Connection, pattern: str) -> None:
    """在下一条匹配 pattern 的 SQL 执行时立刻杀掉本进程。

    用 sqlite3 的 trace callback：它在语句真正执行【之前】触发，
    所以能精确切在"INSERT INTO commands 之前"和"COMMIT 之前"这两个点上。
    """
    up = pattern.upper()

    def _trace(sql: str):
        if up in (sql or "").upper():
            sys.stderr.flush()
            os._exit(9)

    conn.set_trace_callback(_trace)


def child_main(point: str, db: str, base_t: float) -> None:
    k, clock = boot(pathlib.Path(db), base_t)

    if point == "in_handler":
        # 崩在 handler 已跑完、commands 台账还没写之前 → 整个事务应回滚
        _arm_crash(k.store.connect(), "INSERT INTO commands")
        k.submit(Command(kind=tk.CREATE, command_id="cid_A",
                         payload={"kind": TaskKind.CONVERSATION, "task_id": "T_A"}))

    elif point == "before_commit":
        # 崩在 commands 已写、COMMIT 之前 → 业务变更与台账必须一起回滚
        _arm_crash(k.store.connect(), "COMMIT")
        k.submit(Command(kind=tk.CREATE, command_id="cid_B",
                         payload={"kind": TaskKind.CONVERSATION, "task_id": "T_B"}))

    elif point == "after_commit":
        k.submit(Command(kind=tk.CREATE, command_id="cid_C",
                         payload={"kind": TaskKind.CONVERSATION, "task_id": "T_C"}))
        os._exit(9)

    elif point == "after_enqueue":
        k.submit(Command(kind=ob.ENQUEUE, command_id="cid_D",
                         payload={"kind": "resume_task", "idempotency_key": "IDEM_D"}))
        os._exit(9)

    elif point == "after_claim":
        k.submit(Command(kind=ob.ENQUEUE, command_id="cid_E1",
                         payload={"kind": "resume_task", "idempotency_key": "IDEM_E"}))
        k.submit(Command(kind=ob.CLAIM, command_id="cid_E2",
                         payload={"worker_id": "W_child", "lease_seconds": 60}))
        os._exit(9)

    elif point == "task_running":
        k.submit(Command(kind=tk.CREATE, command_id="cid_F1",
                         payload={"kind": TaskKind.CONVERSATION, "task_id": "T_F"}))
        k.submit(Command(kind=tk.SET_TURN, subject_id="T_F", command_id="cid_F2",
                         payload={"turn_id": "turn_zombie"}))
        k.submit(Command(kind=tk.SET_EXECUTION, subject_id="T_F", command_id="cid_F3",
                         payload={"execution": Execution.RUNNING}))
        os._exit(9)

    else:
        raise SystemExit(f"未知崩溃点: {point}")

    os._exit(0)   # 不该走到这里（前两个点应该已经被 trace 杀掉）


def run_child(point: str, db: pathlib.Path, base_t: float) -> int:
    p = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--child", point, str(db), str(base_t)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p.returncode != 9:
        # 子进程没死在预期的注入点 —— 把它的 stderr 打出来，否则这类失败无从排查
        print(f"    ⚠️ 子进程 exit={p.returncode}，stderr 尾部：")
        for line in (p.stderr or "").strip().splitlines()[-8:]:
            print(f"       {line}")
    return p.returncode


# ══════════════════════════════════════════════════════════════════════════
# 1–3｜命令幂等与事务原子性
# ══════════════════════════════════════════════════════════════════════════

def t_crash_before_ledger(tmp: pathlib.Path) -> None:
    print("\n[1] 崩在 commands 台账写入之前 → 命令未生效，重放可正常执行一次")
    db = tmp / "c1.db"
    rc = run_child("in_handler", db, BASE_T)
    check(rc == 9, "子进程确实死在注入点", f"exit={rc}")

    k, _ = boot(db, BASE_T + 1)
    check(tk.get_task(k, "T_A") is None, "Task 未被创建（事务已回滚）")
    with k.store.read() as c:
        n = c.execute("SELECT COUNT(*) n FROM commands WHERE command_id='cid_A'").fetchone()["n"]
    check(n == 0, "commands 台账无残留")

    res = k.submit(Command(kind=tk.CREATE, command_id="cid_A",
                           payload={"kind": TaskKind.CONVERSATION, "task_id": "T_A"}))
    check(not res.replayed, "重放同 command_id 被当成首次执行")
    check(tk.get_task(k, "T_A") is not None, "重放后 Task 存在")


def t_crash_before_commit(tmp: pathlib.Path) -> None:
    print("\n[2] 崩在 commands 已写、COMMIT 之前 → 台账与业务变更一起回滚（同一事务）")
    db = tmp / "c2.db"
    rc = run_child("before_commit", db, BASE_T)
    check(rc == 9, "子进程确实死在 COMMIT 之前", f"exit={rc}")

    k, _ = boot(db, BASE_T + 1)
    t = tk.get_task(k, "T_B")
    with k.store.read() as c:
        n = c.execute("SELECT COUNT(*) n FROM commands WHERE command_id='cid_B'").fetchone()["n"]
    check(t is None and n == 0,
          "Task 与台账【同时】不存在（证明二者在同一事务）",
          f"task={'有' if t else '无'} ledger={n}")


def t_crash_after_commit(tmp: pathlib.Path) -> None:
    print("\n[3] COMMIT 之后立刻崩 → 命令已生效，重放不重复产生副作用")
    db = tmp / "c3.db"
    rc = run_child("after_commit", db, BASE_T)
    check(rc == 9, "子进程在 COMMIT 后被杀", f"exit={rc}")

    k, _ = boot(db, BASE_T + 1)
    t = tk.get_task(k, "T_C")
    check(t is not None and t.revision == 1, "Task 已持久化且 revision=1")

    res = k.submit(Command(kind=tk.CREATE, command_id="cid_C",
                           payload={"kind": TaskKind.CONVERSATION, "task_id": "T_C"}))
    check(res.replayed, "重放命中幂等台账（replayed=True）")
    with k.store.read() as c:
        n = c.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]
    check(n == 1, "没有产生第二个 Task", f"tasks={n}")


# ══════════════════════════════════════════════════════════════════════════
# 4｜outbox 去重
# ══════════════════════════════════════════════════════════════════════════

def t_outbox_idem(tmp: pathlib.Path) -> None:
    print("\n[4] enqueue 之后崩 → 同 idempotency_key 不产生第二条")
    db = tmp / "c4.db"
    rc = run_child("after_enqueue", db, BASE_T)
    check(rc == 9, "子进程在 enqueue 后被杀", f"exit={rc}")

    k, _ = boot(db, BASE_T + 1)
    a = ob.find_by_idempotency_key(k, "IDEM_D")
    check(a is not None and a.status == ob.ActionStatus.PENDING, "action 存活且为 PENDING")

    # 换一个 command_id（模拟 Reconciler 下一轮重新推导），但 idem key 相同
    res = k.submit(Command(kind=ob.ENQUEUE, command_id="cid_D_again",
                           payload={"kind": "resume_task", "idempotency_key": "IDEM_D"}))
    check(res.data.get("created") is False, "第二次 enqueue 被 UNIQUE 索引挡掉（created=False）")
    with k.store.read() as c:
        n = c.execute("SELECT COUNT(*) n FROM runtime_actions").fetchone()["n"]
    check(n == 1, "库里只有一条 action", f"actions={n}")


# ══════════════════════════════════════════════════════════════════════════
# 5–6｜lease 回收与 fencing
# ══════════════════════════════════════════════════════════════════════════

def t_lease_reclaim_and_fence(tmp: pathlib.Path) -> None:
    print("\n[5+6] CLAIMED 后崩 → lease 过期回收 → 重认领 fence 递增 → 旧 fence 提交被拒")
    db = tmp / "c5.db"
    rc = run_child("after_claim", db, BASE_T)
    check(rc == 9, "子进程在 claim 后被杀", f"exit={rc}")

    # 父进程时钟起点 = 子进程 + 61s > lease 60s，所以 lease 已确定过期（零 sleep）
    k, clock = boot(db, BASE_T + 61)
    a = ob.find_by_idempotency_key(k, "IDEM_E")
    check(a is not None and a.status == ob.ActionStatus.CLAIMED and a.fence == 1,
          "崩溃前的状态是 CLAIMED / fence=1 / attempt=1",
          f"status={a.status} fence={a.fence} attempt={a.attempt_no}")

    rep = reconcile_on_startup(k)
    check(a.action_id in rep.reclaimed_actions, "启动恢复把过期 lease 打回 PENDING")
    a2 = ob.get_action(k, a.action_id)
    check(a2.status == ob.ActionStatus.PENDING and a2.claimed_by is None, "claimed_by 已清空")
    check(a2.fence == 1, "打回 PENDING 时 fence 不变（旧 worker 若复活其提交仍有效）",
          f"fence={a2.fence}")

    res = k.submit(Command(kind=ob.CLAIM, payload={"worker_id": "W_parent", "lease_seconds": 60}))
    new_fence = res.data["fence"]
    check(new_fence == 2, "重认领后 fence 递增到 2", f"fence={new_fence}")
    check(res.data["action"]["attempt_no"] == 2, "attempt_no 累加到 2")

    # 旧 worker 复活，拿着 fence=1 提交 → 必须被拒
    rejected = False
    try:
        k.submit(Command(kind=ob.COMPLETE, subject_id=a.action_id,
                         payload={"action_id": a.action_id, "fence": 1}))
    except ob.FenceRejected:
        rejected = True
    check(rejected, "旧 fence=1 的提交被 FenceRejected 拒绝（不变量 15）")
    check(ob.get_action(k, a.action_id).status == ob.ActionStatus.CLAIMED,
          "被拒的提交没有改动状态")

    r2 = k.submit(Command(kind=ob.COMPLETE, subject_id=a.action_id,
                          payload={"action_id": a.action_id, "fence": new_fence}))
    check(r2.data["status"] == ob.ActionStatus.DONE, "当前 fence 的提交正常完成")

    miss = False
    try:
        k.submit(Command(kind=ob.COMPLETE, payload={"action_id": a.action_id}))
    except Exception as e:
        miss = "fence" in str(e)
    check(miss, "不带 fence 的提交被直接拒绝（不许绕过不变量 15）")


# ══════════════════════════════════════════════════════════════════════════
# 7｜重启中断恢复
# ══════════════════════════════════════════════════════════════════════════

def t_startup_interrupt(tmp: pathlib.Path) -> None:
    print("\n[7] Task 在 RUNNING 时崩 → 打回 IDLE + 解绑 turn，且恢复幂等")
    db = tmp / "c7.db"
    rc = run_child("task_running", db, BASE_T)
    check(rc == 9, "子进程在 RUNNING 状态被杀", f"exit={rc}")

    k, _ = boot(db, BASE_T + 5)
    before = tk.get_task(k, "T_F")
    check(before.execution == Execution.RUNNING and before.current_turn_id == "turn_zombie",
          "崩溃留下的脏状态确实是 RUNNING + 绑着 turn",
          f"exec={before.execution} turn={before.current_turn_id}")

    rep = reconcile_on_startup(k)
    check("T_F" in rep.interrupted_tasks, "报告里列出了被中断的 Task")
    after = tk.get_task(k, "T_F")
    check(after.current_turn_id is None, "turn 已解绑")
    # 🔴 2026-08-22 已定**推翻了这里原来守的那条**：
    #    旧断言是「Task 仍是 ACTIVE（被重启打断的是 turn，不是这件事本身）」。
    #    新语义：**关闭软件 = 用户默认放弃这次协同**，所以它不该活过重启。
    #    📌 「技术上能不能重建」和「用户想不想让它重建」是两个问题，这里听后者。
    #    ⭐ 而且实测证据支持它更准确：本项目 90 天 `record_fatal` 零次、
    #       已知崩溃形态是「拉不起来」（那时没有 task 在跑）——
    #       所以启动时捞到的 ACTIVE Task 几乎必然来自**用户主动关掉了软件**。
    check(after.lifecycle == Lifecycle.TERMINAL,
          "⭐⭐ Task 已终止（关闭软件即视为放弃本次协同）", str(after.lifecycle))
    check(after.terminal_reason == "INTERRUPTED_BY_RESTART",
          "⭐ 终止理由是「被重启打断」，不是「做完了」", str(after.terminal_reason))
    check(not rep.broken_invariants, "恢复后不变量自检通过", str(rep.broken_invariants))

    rev_after = after.revision
    rep2 = reconcile_on_startup(k)
    check(not rep2.interrupted_tasks, "第二次启动恢复不再处理它（幂等）")
    check(tk.get_task(k, "T_F").revision == rev_after, "revision 没有被白bump", f"rev={rev_after}")


# ══════════════════════════════════════════════════════════════════════════
# 8｜Projection 从快照重建（丢光事件也对）
# ══════════════════════════════════════════════════════════════════════════

class _Spy(Projection):
    name = "spy"

    def __init__(self):
        self.rebuilds = 0
        self.tasks = 0
        self.actions = 0

    def rebuild(self, snap) -> None:
        self.rebuilds += 1
        self.tasks = len(snap.active_tasks)
        self.actions = snap.pending_action_count


def t_projection_rebuild(tmp: pathlib.Path) -> None:
    print("\n[8] 丢光转移队列后 Projection 仍能从 SQLite 重建（不变量 17）")
    k, _ = boot(tmp / "c8.db", BASE_T)
    k.submit(Command(kind=tk.CREATE, payload={"kind": TaskKind.CONVERSATION, "task_id": "P1"}))
    # ⚠️ **必须显式建成 BACKGROUND（2026-08-16，④ 之后）。**
    #    本用例要的只是「库里有两条 Task」，两条都在前台是它**顺手**构造的；
    #    而 `one_foreground_conversation` 不变量现在管这件事：
    #    「两件事并存」的合法表示是**一个在前台 + 其余被搁置**。
    # ⭐ 这次红是不变量在**正确工作** —— 它抓到的正是它被建出来要抓的形状
    #    （📌 机制负责让它不发生，不变量负责让它发生时被发现）。
    k.submit(Command(kind=tk.CREATE, payload={"kind": TaskKind.CONVERSATION, "task_id": "P2",
                                              "placement": Placement.BACKGROUND}))
    k.submit(Command(kind=ob.ENQUEUE, payload={"kind": "x", "idempotency_key": "IK1"}))

    dropped = k.drain_transitions()
    check(len(dropped) >= 3, "先把事件全部丢弃", f"丢了 {len(dropped)} 条")
    check(not k.drain_transitions(), "队列已空")

    hub = ProjectionHub(k)
    spy = _Spy()
    hub.add(spy)
    hub.tick()
    check(spy.rebuilds == 1, "队列为空时依然重画了一次（自愈，不靠调用方自觉）")
    check(spy.tasks == 2 and spy.actions == 1,
          "重建出的内容与库内一致", f"tasks={spy.tasks} actions={spy.actions}")

    snap = snapshot(k)
    check(snap.foreground_task is not None, "快照能派生出前台 Task")

    # 展示回执：主键含 revision，所以 bump 之后应该重新可展示
    rc_store = ProjectionReceiptStore(k)
    t = tk.get_task(k, "P1")
    check(rc_store.mark_presented("task", "P1", t.revision, Channel.CHAT), "首次展示返回 True")
    check(not rc_store.mark_presented("task", "P1", t.revision, Channel.CHAT),
          "同 revision 重复展示返回 False（去重）")
    k.submit(Command(kind=tk.SET_PLACEMENT, subject_id="P1",
                     payload={"placement": Placement.BACKGROUND}))
    t2 = tk.get_task(k, "P1")
    check(rc_store.mark_presented("task", "P1", t2.revision, Channel.CHAT),
          "revision 变了之后重新可展示（revision 就是 generation）", f"rev {t.revision}→{t2.revision}")
    check(rc_store.mark_presented("task", "P1", t2.revision, Channel.MONITOR),
          "不同渠道各自独立记回执")


# ══════════════════════════════════════════════════════════════════════════
# 9｜终态不可逆 + revision 冲突 + 不变量回滚
# ══════════════════════════════════════════════════════════════════════════

def t_invariants(tmp: pathlib.Path) -> None:
    print("\n[9] 终态不可逆 / revision 冲突 / 不变量违反时整体回滚")
    k, _ = boot(tmp / "c9.db", BASE_T)
    k.submit(Command(kind=tk.CREATE, payload={"kind": TaskKind.CONVERSATION, "task_id": "Z"}))
    k.submit(Command(kind=tk.TERMINATE, subject_id="Z",
                     payload={"reason": TerminalReason.COMPLETED}))
    z = tk.get_task(k, "Z")
    check(z.lifecycle == Lifecycle.TERMINAL, "Task 已终态")

    blocked = False
    try:
        k.submit(Command(kind=tk.SET_EXECUTION, subject_id="Z",
                         payload={"execution": Execution.RUNNING}))
    except InvariantViolation:
        blocked = True
    check(blocked, "TERMINAL Task 拒绝改 execution（不变量 18）")
    check(tk.get_task(k, "Z").revision == z.revision, "被拒的命令没有 bump revision")

    r = k.submit(Command(kind=tk.TERMINATE, subject_id="Z",
                         payload={"reason": TerminalReason.CANCELLED}))
    check(r.data.get("already_terminal") is True, "重复终止幂等返回")
    check(tk.get_task(k, "Z").terminal_reason == TerminalReason.COMPLETED,
          "重复终止不覆盖首次的 reason")

    # revision 冲突
    k.submit(Command(kind=tk.CREATE, payload={"kind": TaskKind.CONVERSATION, "task_id": "R"}))
    cur = tk.get_task(k, "R").revision
    ok_rev = k.submit(Command(kind=tk.SET_EXECUTION, subject_id="R",
                              expected_revision=cur,
                              payload={"execution": Execution.RUNNING}))
    check(ok_rev.revision == cur + 1, "expected_revision 匹配时正常执行并递增")
    conflict = False
    try:
        k.submit(Command(kind=tk.SET_EXECUTION, subject_id="R", expected_revision=cur,
                         payload={"execution": Execution.IDLE}))
    except RevisionConflict:
        conflict = True
    check(conflict, "过期的 expected_revision 被拒（乐观并发）")

    any_ok = k.submit(Command(kind=tk.SET_EXECUTION, subject_id="R",
                              expected_revision=ANY_REVISION,
                              payload={"execution": Execution.IDLE}))
    check(any_ok.revision is not None, "ANY_REVISION 显式绕过版本校验")

    # 自定义不变量：违反时必须整体回滚
    def _never(conn):
        raise InvariantViolation("always_fail", "测试用：永远失败")

    k.register_invariant("always_fail", _never)
    n_before = len(tk.list_active_tasks(k))
    boom = False
    try:
        k.submit(Command(kind=tk.CREATE, payload={"kind": TaskKind.CONVERSATION, "task_id": "NOPE"}))
    except InvariantViolation:
        boom = True
    check(boom, "不变量违反时命令被拒")
    check(tk.get_task(k, "NOPE") is None, "违反不变量的写入已整体回滚")
    check(len(tk.list_active_tasks(k)) == n_before, "库内 Task 数量未变")
    check("always_fail" in k.check_invariants_now(), "check_invariants_now 能报告违反项")


# ══════════════════════════════════════════════════════════════════════════
# 10｜blockers 派生 + FakeClock 驱动
# ══════════════════════════════════════════════════════════════════════════

def t_blockers_and_clock(tmp: pathlib.Path) -> None:
    print("\n[10] blockers 查询派生（不是字段）+ FakeClock 驱动 lease，零 sleep")
    k, clock = boot(tmp / "c10.db", BASE_T)

    with k.store.read() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
    check("blockers" not in cols, "tasks 表【没有】blockers 列 —— blockers 是派生的，不是字段",
          f"{len(cols)} 列")

    k.submit(Command(kind=tk.CREATE, payload={"kind": TaskKind.CONVERSATION, "task_id": "B1"}))
    v = tk.get_task_view(k, "B1")
    check(v is not None and not v.blockers and v.is_ready, "无 provider 时 Task 可推进")

    held = {"on": True}

    def _fake_provider(conn, task_id):
        if task_id == "B1" and held["on"]:
            return [tk.Blocker("approval", "ap_1", "等用户点应用")]
        return []

    register_blocker_provider("test_approval", _fake_provider)
    v = tk.get_task_view(k, "B1")
    check(len(v.blockers) == 1 and v.blockers[0].blocker_id == "ap_1",
          "provider 注册后 blockers 立刻派生出来（task.py 一行未改）")
    check(not v.is_ready, "有 blocker 时 is_ready=False")
    check("B1" not in [x.record.task_id for x in tk.list_ready_tasks(k)],
          "list_ready_tasks 排除被挡住的 Task")

    held["on"] = False
    check(tk.get_task_view(k, "B1").is_ready,
          "blocker 消失后立刻可推进（派生的，不需要谁去清字段）")

    # ── ⭐⭐ PAUSED 不许被调度（2026-08-07 修，指出）───────────────
    # 原判据是 `execution != RUNNING`，于是 **PAUSED 也算 ready**。
    # 它一直没炸，只因为真正的 Task 调度器还没接起来（生产里 0 个 Task）——
    # 而这类"等基建接上才会显形"的错，趁现在改最便宜。
    # 📌 `PAUSED` 的语义是"**明确不许调度**"，不是"暂时没在跑"。
    #    把"不在跑"和"不许跑"压进同一个判断，就是"一个表达式承担两个现实"。
    k.submit(Command(kind=tk.SET_EXECUTION,
                     payload={"task_id": "B1", "execution": tk.Execution.PAUSED}))
    v_p = tk.get_task_view(k, "B1")
    check(v_p.record.execution == tk.Execution.PAUSED, "前提：Task 已转 PAUSED")
    check(not v_p.is_ready,
          "⭐⭐ PAUSED 的 Task **不是** ready —— 否则被动挂起刚让 Nano 停手，"
          "Reconciler 立刻又把它叫起来去抢用户的鼠标")
    check("B1" not in [x.record.task_id for x in tk.list_ready_tasks(k)],
          "⭐⭐ `list_ready_tasks` 也排除 PAUSED —— "
          "⚠️ SQL 和 `is_ready` 必须同步，它们分家过一次（两边都写成 !=RUNNING）")

    # 反证：转回 IDLE 必须重新可推进，否则上面只证明了"永远不 ready"
    k.submit(Command(kind=tk.SET_EXECUTION,
                     payload={"task_id": "B1", "execution": tk.Execution.IDLE}))
    check(tk.get_task_view(k, "B1").is_ready,
          "反证：回到 IDLE 立刻又可推进")

    # FakeClock：lease 过期完全由时钟推进决定
    k.submit(Command(kind=ob.ENQUEUE, payload={"kind": "y", "idempotency_key": "IKC"}))
    k.submit(Command(kind=ob.CLAIM, payload={"worker_id": "W1", "lease_seconds": 30}))
    rep = reconcile_tick(k)
    check(not rep.reclaimed_actions, "lease 未到期时不回收")
    clock.advance(31)
    rep = reconcile_tick(k)
    check(len(rep.reclaimed_actions) == 1, "时钟推进 31s 后回收（零 sleep）")

    # 同一秒内 reclaim 幂等，跨秒可重跑
    rep_a = reconcile_tick(k)
    clock.advance(2)
    k.submit(Command(kind=ob.CLAIM, payload={"worker_id": "W2", "lease_seconds": 5}))
    clock.advance(6)
    rep_b = reconcile_tick(k)
    check(len(rep_b.reclaimed_actions) == 1, "跨秒后 reclaim 能再次生效（command_id 按秒分桶）")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("Runtime Kernel + Thin Task Spine 验收")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_rt_"))
    try:
        for fn in (t_crash_before_ledger, t_crash_before_commit, t_crash_after_commit,
                   t_outbox_idem, t_lease_reclaim_and_fence, t_startup_interrupt,
                   t_projection_rebuild, t_invariants, t_blockers_and_clock):
            try:
                fn(tmp)
            except Exception as e:
                import traceback
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        close_all_stores()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{total} 通过")
    if passed != total:
        print("\n失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}" + (f"   [{note}]" if note else ""))
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_main(sys.argv[2], sys.argv[3], float(sys.argv[4]))
    else:
        sys.exit(main())
