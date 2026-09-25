# -*- coding: utf-8 -*-
"""· Interaction 表 + revision 绑定（清单第 ① 项）。

验的是早先的**核心承诺**：用户的回答不会再被吞掉。

改造前三条路由劫持（`_pending_skill` / `_pending_action` /
`_pending_skill_clarification`）共享同一个形状：先清 pending，再去干活；
干活失败，答案就没了。本套件里 的子进程崩溃用例就是在钉这一条 ——
answer 提交之后立刻 `os._exit(9)`，重启后原话必须还在，且状态停在 ANSWERED 可重试。

用法：
  py -3.10 tests\cases\t_f1_stage3_interaction.py
  py -3.10 tests\cases\t_f1_stage3_interaction.py --child <point> <db> <base_time>
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import (
    ANY_REVISION,
    Command,
    InvariantViolation,
    KernelError,
    RevisionConflict,
    reset_kernel_for_tests,
)
from core.runtime.store import RuntimeStore
from core.runtime import interaction as I
from core.runtime import task as tk
from core.runtime import reconciler as rec

BASE_T = 1_800_000_000.0

_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def boot(db: pathlib.Path, base_t: float = BASE_T):
    clock = FakeClock(base_t)
    st = RuntimeStore(db)
    _stores.append(st)
    k = reset_kernel_for_tests(store=st, clock=clock)
    return k, clock


def close_all_stores() -> None:
    for st in _stores:
        try:
            st.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


def open_one(k, **kw) -> str:
    p = {"kind": I.Kind.SKILL_CLARIFICATION, "prompt_text": "问题?"}
    p.update(kw)
    return k.submit(Command(kind=I.OPEN, payload=p)).data["interaction_id"]


# ══════════════════════════════════════════════════════════════════════════
# 1｜schema 与槽位分配
# ══════════════════════════════════════════════════════════════════════════

def t_schema(tmp: pathlib.Path) -> None:
    print("\n[1] schema v3 与槽位分配")
    k, _ = boot(tmp / "s.db")

    with k.store.read() as conn:
        ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    # ⚠️ 断言 `>= 3` 而不是 `== 3`：schema 版本**只增不减**，以后加表就会往上走
    #（早先加 wait_conditions 升到了 v4）。写死等号等于"每加一张表就有一条无关测试变红"，
    # 那种红是噪音，久了会训练出"红了先改测试"的坏习惯。
    # 这条要守的是「interactions 是在 v3 引入的、且库不比它旧」。
    check(ver >= 3, "schema 版本不低于引入 interactions 的 v3", f"user_version={ver}")
    check("interactions" in tables, "interactions 表已建立")
    check("ux_inter_foreground" in idx, "前台槽唯一索引存在（不靠先查再插）")

    # 第一个 DEFERRED 拿前台，第二个开始排队
    a = open_one(k)
    b = open_one(k)
    ra, rb = I.get(k, a), I.get(k, b)
    check(ra.slot == I.Slot.FOREGROUND, "第一个 DEFERRED 自动占前台槽")
    check(rb.slot == I.Slot.DEFERRED, "第二个排进 deferred 队列")

    # INLINE 不占槽 —— 它是模态对话框，占槽会白白挤掉一张卡片
    c = open_one(k, kind=I.Kind.OS_RISK, mode=I.Mode.INLINE,
                 durability=I.Durability.EPHEMERAL)
    check(I.get(k, c).slot is None, "INLINE 不占槽位（模态对话框不是卡片）")

    check(I.foreground(k).interaction_id == a, "foreground() 取到的是占前台槽那条")

    # ⭐ 这条断言原来写的是 `order[0] == a`（"前台排在最前"）。
    # **2026-08-06 实测推翻了那个设计**：前台槽给的是最先来的那个，
    # 于是清单第 1 条恰好是最老的，模型照着挑就部署了最老那个 Skill。
    # 理由：聊了很多轮之后用户说"部署吧"，指的必然是刚做完那个。
    # 现在改成**最新在前**（`1 = 最新 / 2 = 次新 / 3 = 最旧`），
    # 并且 foreground 槽**不再影响展示顺序** —— 它只是容量记账。
    # 把"容量"和"展示优先级"绑在一起正是那个 bug 的来源。
    order = [r.interaction_id for r in I.list_live(k)]
    check(order[0] == c, "⭐ list_live 最新在前（c 是最后开的）", str(order))
    check(order[-1] == a, "最早那条排在最后", str(order))
    check(order == [c, b, a], "完整顺序 = 创建时间倒序", str(order))


# ══════════════════════════════════════════════════════════════════════════
# 2｜槽位上限
# ══════════════════════════════════════════════════════════════════════════

def t_slot_caps(tmp: pathlib.Path) -> None:
    print("\n[2] 槽位上限：deferred ≤ 5 靠不变量，foreground ≤ 1 靠唯一索引")
    k, _ = boot(tmp / "c.db")

    ids = [open_one(k) for _ in range(6)]     # 1 前台 + 5 队列 = 刚好满
    usage = I.slot_usage(k)
    check(usage.get(I.Slot.FOREGROUND) == 1 and usage.get(I.Slot.DEFERRED) == 5,
          "1 前台 + 5 队列时仍然合法", str(usage))
    check(k.check_invariants_now() == [], "满槽状态下不变量全部通过")

    # 第 7 个必须被拒
    raised = ""
    try:
        open_one(k)
    except InvariantViolation as e:
        raised = e.name
    check(raised == "interaction_deferred_cap", "第 7 个被 deferred 上限拒绝", raised)

    # ⭐ 纪律：断言"某件事没发生"时，必须同时断言前置条件确实发生过。
    # 只验"总数还是 6"的话，如果 open 因为别的原因根本没跑到 INSERT，用例会假通过。
    with k.store.read() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM interactions").fetchone()["c"]
        cmds = conn.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"]
    check(total == 6, "被拒的那条已整体回滚，没留下半条记录", f"总数={total}")
    check(cmds == 6, "前置条件成立：前 6 条命令确实执行并记账了", f"commands={cmds}")

    # 关掉一个就能再开一个 —— 上限是"未决"的上限，不是历史总量的上限
    k.submit(Command(kind=I.RESOLVE, subject_id=ids[1]))
    again = ""
    try:
        open_one(k)
        again = "ok"
    except InvariantViolation:
        again = "still blocked"
    check(again == "ok", "关掉一个后腾出名额（上限约束的是未决数量）")

    # 强行塞第二个前台 → 唯一索引拦下
    raised2 = ""
    try:
        open_one(k, slot=I.Slot.FOREGROUND)
    except (sqlite3.IntegrityError, InvariantViolation) as e:
        raised2 = type(e).__name__
    check(raised2 in ("IntegrityError", "InvariantViolation"),
          "第二个前台卡片被数据库/不变量拦下", raised2)
    check(len([r for r in I.list_live(k) if r.slot == I.Slot.FOREGROUND]) == 1,
          "前台仍然只有一个")


# ══════════════════════════════════════════════════════════════════════════
# 3｜answer：原子落盘 + 幂等重试
# ══════════════════════════════════════════════════════════════════════════

def t_answer(tmp: pathlib.Path) -> None:
    print("\n[3] answer：先落盘用户原话，失败可幂等重试")
    k, _ = boot(tmp / "a.db")
    iid = open_one(k, payload={"original_requirement": "导出大额消费"})

    r = k.submit(Command(kind=I.ANSWER, subject_id=iid,
                         payload={"answer_verbatim": "用 >=6000", "relation": I.Relation.ANSWER}))
    rec1 = I.get(k, iid)
    check(rec1.status == I.Status.ANSWERED, "OPEN → ANSWERED")
    check(rec1.answer_verbatim == "用 >=6000", "用户原话一字不改地存下来")
    check(rec1.revision == 2, "revision 递增", f"rev={rec1.revision}")
    check(r.data["retry"] is False, "首次回答：retry=False（调用方该去跑 continuation）")

    # 同一答案再来一次 = 上次 continuation 失败、模型按提示重试。不是错误。
    r2 = k.submit(Command(kind=I.ANSWER, subject_id=iid,
                          payload={"answer_verbatim": "用 >=6000", "relation": I.Relation.ANSWER}))
    rec2 = I.get(k, iid)
    check(r2.data["retry"] is True, "同答案重放被识别为重试")
    check(rec2.revision == 2, "重试不 bump revision（什么都没变，UI 不必重画）",
          f"rev={rec2.revision}")

    # 改口 → 覆盖，最新的才是真的
    r3 = k.submit(Command(kind=I.ANSWER, subject_id=iid,
                          payload={"answer_verbatim": "算了用 >4000", "relation": I.Relation.ANSWER}))
    rec3 = I.get(k, iid)
    check(rec3.answer_verbatim == "算了用 >4000", "改口时覆盖旧答案")
    check(r3.data["overwrote_previous_answer"] is True, "覆盖行为被显式报告")
    check(rec3.revision == 3, "覆盖 bump revision", f"rev={rec3.revision}")

    # UNRELATED 不是命令）
    bad = ""
    try:
        k.submit(Command(kind=I.ANSWER, subject_id=iid,
                         payload={"answer_verbatim": "查下天气", "relation": "UNRELATED"}))
    except KernelError as e:
        bad = str(e)
    check("UNRELATED" in bad and "不要调这个工具" in bad,
          "UNRELATED 被拒，且错误信息直说它的含义是【不调工具】")

    check(I.needs_retry_ids(k) == [iid],
          "ANSWERED 被识别为待重试（保证它不是没人管的永久状态）")
    check(I.get(k, iid).needs_retry is True, "记录自己也暴露 needs_retry")

    # 终态之后不许再回答
    k.submit(Command(kind=I.RESOLVE, subject_id=iid))
    inv = ""
    try:
        k.submit(Command(kind=I.ANSWER, subject_id=iid,
                         payload={"answer_verbatim": "再改一次"}))
    except InvariantViolation as e:
        inv = e.name
    check(inv == "interaction_terminal_immutable", "终态不可逆：RESOLVED 之后不能再回答", inv)
    check(I.get(k, iid).slot is None, "进终态时槽位被显式清空")
    check(k.check_invariants_now() == [], "closed_holds_no_slot 不变量通过")


# ══════════════════════════════════════════════════════════════════════════
# 4｜revision 绑定（清单 ① 的另一半）
# ══════════════════════════════════════════════════════════════════════════

def t_revision_binding(tmp: pathlib.Path) -> None:
    print("\n[4] revision 绑定：expected_revision 必须查对表")
    k, _ = boot(tmp / "r.db")
    iid = open_one(k)

    ok = k.submit(Command(kind=I.ANSWER, subject_id=iid, expected_revision=1,
                          payload={"answer_verbatim": "对的版本"}))
    check(ok.revision == 2, "expected_revision 正确时放行")

    conflict = None
    try:
        k.submit(Command(kind=I.ANSWER, subject_id=iid, expected_revision=1,
                         payload={"answer_verbatim": "过期的版本"}))
    except RevisionConflict as e:
        conflict = e
    check(conflict is not None, "版本过期被拒")
    # ⭐ 这条是早先补的内核缺口的回归测试：
    # 修之前 `_assert_revision` 把 `SELECT revision FROM tasks` 写死了，
    # 于是 interaction 命令会去 tasks 表查一个不存在的 id，拿到 actual=None，
    # 抛出一条**指向 Task 的**冲突信息 —— 排查的人会被带到完全无关的地方。
    check(conflict is not None and conflict.actual == 2,
          "冲突信息里的 actual 来自 interactions 表，不是 tasks",
          f"actual={getattr(conflict, 'actual', None)}")
    check(I.get(k, iid).answer_verbatim == "对的版本", "被拒的写入没有生效")

    forced = k.submit(Command(kind=I.ANSWER, subject_id=iid, expected_revision=ANY_REVISION,
                              payload={"answer_verbatim": "我知道我在覆盖"}))
    check(forced.revision == 3, "ANY_REVISION 显式绕过版本校验")

    # 领域没登记 revision 来源时，必须响亮失败而不是静默退回 tasks 表
    k.register_revision_source("orphan", "interactions", "interaction_id")
    src_missing = ""

    @k.register("orphan2.noop")
    def _noop(conn, cmd, ctx):
        from core.runtime.kernel import HandlerOutcome
        return HandlerOutcome(data={})

    try:
        k.submit(Command(kind="orphan2.noop", subject_id="x", expected_revision=1))
    except KernelError as e:
        src_missing = str(e)
    check("register_revision_source" in src_missing,
          "未登记 revision 来源的领域会被明确拒绝，并给出修法")

    # Task 那一路没被改坏
    k.submit(Command(kind=tk.CREATE, payload={"task_id": "T1"}))
    tc = None
    try:
        k.submit(Command(kind=tk.SET_TURN, subject_id="T1", expected_revision=99,
                         payload={"turn_id": "x"}))
    except RevisionConflict as e:
        tc = e
    check(tc is not None and tc.actual == 1, "Task 的 revision 校验仍然正常",
          f"actual={getattr(tc, 'actual', None)}")


# ══════════════════════════════════════════════════════════════════════════
# 5｜artifact 绑定
# ══════════════════════════════════════════════════════════════════════════

def t_artifact(tmp: pathlib.Path) -> None:
    print("\n[5] artifact 绑定：审批钉在当时那一版上")
    k, _ = boot(tmp / "art.db")
    code_v1 = "def run():\n    return 1\n"
    iid = open_one(k, kind=I.Kind.SKILL_AUDIT, artifact_kind="skill",
                   artifact_id="sk_export", artifact_revision=3,
                   artifact_hash=I.artifact_hash(code_v1))
    rec = I.get(k, iid)

    ok, why = I.verify_artifact(rec, 3, code_v1)
    check(ok and why == "", "同一版：校验通过")

    ok2, why2 = I.verify_artifact(rec, 4, code_v1)
    check(not ok2 and "revision" in why2, "revision 变了：拒绝放行", why2)

    ok3, why3 = I.verify_artifact(rec, 3, code_v1 + "    os.remove('/')\n")
    check(not ok3 and "内容已变更" in why3,
          "revision 没动但内容被改：靠 hash 抓出来", why3)

    ok4, why4 = I.verify_artifact(rec, None, None)
    check(not ok4 and "已不存在" in why4, "artifact 消失：拒绝放行", why4)

    d = k.submit(Command(kind=I.DECIDE, subject_id=iid, payload={"approved": True}))
    check(I.get(k, iid).status == I.Status.APPROVED, "DECIDE 记录审批结果")
    check(d.data["artifact_hash"] == rec.artifact_hash,
          "审批结果里带回 artifact 指纹（落地前还要再核一次）")

    # artifact 变了 → SUPERSEDE，旧审批作废
    iid2 = open_one(k, kind=I.Kind.SKILL_AUDIT, artifact_id="sk_b", artifact_revision=1,
                    artifact_hash=I.artifact_hash("old"))
    new_id = open_one(k, kind=I.Kind.SKILL_AUDIT, artifact_id="sk_b", artifact_revision=2,
                      artifact_hash=I.artifact_hash("new"))
    k.submit(Command(kind=I.SUPERSEDE, subject_id=iid2,
                     payload={"superseded_by": new_id}))
    sup = I.get(k, iid2)
    check(sup.status == I.Status.SUPERSEDED and sup.superseded_by == new_id,
          "旧版本被标记 SUPERSEDED 并指向新交互")
    check(sup.slot is None, "被取代的交互腾出槽位")


# ══════════════════════════════════════════════════════════════════════════
# 6｜blocker provider
# ══════════════════════════════════════════════════════════════════════════

def t_blockers(tmp: pathlib.Path) -> None:
    print("\n[6] blockers 由 Interaction 派生，tasks 表仍然没有 blockers 列")
    k, _ = boot(tmp / "b.db")
    k.submit(Command(kind=tk.CREATE, payload={"task_id": "T_x"}))

    view0 = tk.get_task_view(k, "T_x")
    check(view0.is_ready, "没有交互时 Task 可推进")

    iid = open_one(k, owner_task_id="T_x", prompt_text="要用哪个阈值?")
    view1 = tk.get_task_view(k, "T_x")
    check(len(view1.blockers) == 1, "OPEN 交互成为 Task 的 blocker")
    check(view1.blockers[0].blocker_kind == "interaction", "blocker 类型正确")
    check(view1.blockers[0].summary.startswith("要用哪个阈值"), "blocker 带得上问题摘要")
    check(not view1.is_ready, "被挡住的 Task 不 ready")
    check([v.record.task_id for v in tk.list_ready_tasks(k)] == [],
          "list_ready_tasks 排除被挡住的 Task")

    # ⚠️ DEFERRED"不阻塞 Nano"与"阻塞它自己那个 Task"不矛盾：
    # 前者说的是用户能不能继续聊别的，后者说的是 Reconciler 能不能推进这个 Task。
    check(I.get(k, iid).mode == I.Mode.DEFERRED, "前置条件：这是个 DEFERRED 交互")

    k.submit(Command(kind=I.ANSWER, subject_id=iid, payload={"answer_verbatim": "6000"}))
    check(len(tk.get_task_view(k, "T_x").blockers) == 1,
          "ANSWERED 仍然算 blocker（continuation 还没跑完）")

    k.submit(Command(kind=I.RESOLVE, subject_id=iid))
    check(tk.get_task_view(k, "T_x").is_ready, "RESOLVED 之后 Task 恢复可推进")

    with k.store.read() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    check("blockers" not in cols, "tasks 表始终没有 blockers 列 —— blockers 一直是派生的")


# ══════════════════════════════════════════════════════════════════════════
# 7｜Reconcile：EPHEMERAL 不跨重启、deadline 过期
# ══════════════════════════════════════════════════════════════════════════

def t_reconcile(tmp: pathlib.Path) -> None:
    print("\n[7] Reconcile：EPHEMERAL 重启作废 · deadline 过期")
    db = tmp / "rec.db"
    k, clock = boot(db)

    eph = open_one(k, kind=I.Kind.OS_RISK, mode=I.Mode.INLINE,
                   durability=I.Durability.EPHEMERAL, prompt_text="要删这个文件吗?")
    per = open_one(k, kind=I.Kind.SKILL_AUDIT, durability=I.Durability.PERSISTED)
    check(len(I.list_live(k)) == 2, "前置条件：两个交互都活着")

    report = rec.reconcile_on_startup(k)
    check(report.extra.get("interactions_purged") == 1, "启动作废了 1 个 EPHEMERAL",
          str(report.extra))
    check(I.get(k, eph).status == I.Status.CANCELLED, "EPHEMERAL 被作废")
    check(I.get(k, eph).resolution == I.Resolution.INTERRUPTED_BY_RESTART,
          "作废原因写明是重启")
    check(I.get(k, per).status == I.Status.OPEN, "PERSISTED 跨重启存活")

    # 幂等：连跑两次结果相同
    r2 = rec.reconcile_on_startup(k)
    check(r2.extra.get("interactions_purged") is None, "再跑一次不再作废任何东西（幂等）")

    # deadline：没设的永不过期
    dl = open_one(k, deadline_at=clock.now() + 100.0)
    check(rec.reconcile_tick(k).extra.get("interactions_expired") is None,
          "未到期时不过期")
    clock.advance(101.0)
    r3 = rec.reconcile_tick(k)
    check(r3.extra.get("interactions_expired") == 1, "到期后被 EXPIRE", str(r3.extra))
    check(I.get(k, dl).status == I.Status.EXPIRED, "状态是 EXPIRED")
    check(I.get(k, per).status == I.Status.OPEN,
          "没设 deadline 的审计永不过期（积压不是错误）")
    check(k.check_invariants_now() == [], "Reconcile 之后不变量全通过")


# ══════════════════════════════════════════════════════════════════════════
# 8｜子进程崩溃：答案不会再被吞掉（早先的核心承诺）
# ══════════════════════════════════════════════════════════════════════════

def child_main(point: str, db: str, base_t: float) -> None:
    k, _ = boot(pathlib.Path(db), base_t)

    if point == "answer_then_die":
        # 模拟改造前那条 bug 链的现场：答案刚记下，continuation 还没跑，进程没了。
        iid = k.submit(Command(
            kind=I.OPEN, command_id="cid_open",
            payload={"kind": I.Kind.SKILL_CLARIFICATION, "interaction_id": "int_fixed",
                     "durability": I.Durability.PERSISTED,
                     "prompt_text": "阈值用哪个?",
                     "payload": {"original_requirement": "导出大额消费",
                                 "last_explorer_message": "文件已找到，阈值待定"}},
        )).data["interaction_id"]
        k.submit(Command(kind=I.ANSWER, subject_id=iid, command_id="cid_ans",
                         payload={"answer_verbatim": "用 >=6000，另外导出成 CSV",
                                  "relation": I.Relation.ANSWER_AND_AMENDMENT}))
        os._exit(9)

    elif point == "die_before_commit":
        # 崩在 COMMIT 之前 → 答案必须整体回滚，状态停在 OPEN（而不是半个 ANSWERED）
        k.submit(Command(
            kind=I.OPEN, command_id="cid_open2",
            payload={"kind": I.Kind.SKILL_CLARIFICATION, "interaction_id": "int_rb",
                     "prompt_text": "阈值?"}))
        conn = k.store.connect()

        def _trace(sql: str):
            if "COMMIT" in (sql or "").upper():
                sys.stderr.flush()
                os._exit(9)
        conn.set_trace_callback(_trace)
        k.submit(Command(kind=I.ANSWER, subject_id="int_rb", command_id="cid_ans2",
                         payload={"answer_verbatim": "这句话不该存活"}))
        os._exit(0)

    else:
        raise SystemExit(f"未知崩溃点: {point}")

    os._exit(0)


def run_child(point: str, db: pathlib.Path, base_t: float) -> int:
    p = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--child", point, str(db), str(base_t)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p.returncode != 9:
        print(f"    子进程 exit={p.returncode}，stderr 尾部：")
        for line in (p.stderr or "").strip().splitlines()[-8:]:
            print(f"       {line}")
    return p.returncode


def t_crash_answer_survives(tmp: pathlib.Path) -> None:
    print("\n[8] 子进程崩溃：答案落盘后被强杀，重启必须还在")
    db = tmp / "crash.db"
    rc = run_child("answer_then_die", db, BASE_T)
    check(rc == 9, "子进程在 answer 之后被强杀", f"exit={rc}")

    k, _ = boot(db)
    r = I.get(k, "int_fixed")
    check(r is not None, "交互记录存活")
    check(r.status == I.Status.ANSWERED, "状态停在 ANSWERED（可重试），不是丢失")
    check(r.answer_verbatim == "用 >=6000，另外导出成 CSV",
          "⭐ 用户原话一字不差地活了下来 —— 这就是 Interaction 那一层要修的那条 bug")
    check(r.relation == I.Relation.ANSWER_AND_AMENDMENT, "relation 一并存活")
    check(r.payload.get("original_requirement") == "导出大额消费",
          "领域 payload（原始需求）存活，不需要用户重说")

    # 重启后 Reconcile 不会把 PERSISTED 的答案清掉
    rec.reconcile_on_startup(k)
    r2 = I.get(k, "int_fixed")
    check(r2.status == I.Status.ANSWERED, "启动 Reconcile 不动 PERSISTED 的 ANSWERED")
    check(I.needs_retry_ids(k) == ["int_fixed"],
          "重启后它仍被识别为待重试（ANSWERED 有明确消费者）")

    # 重放同一个 command_id → 幂等台账拦下
    replay = k.submit(Command(kind=I.ANSWER, subject_id="int_fixed", command_id="cid_ans",
                              payload={"answer_verbatim": "完全不同的话"}))
    check(replay.replayed is True, "同 command_id 重放被幂等台账拦下")
    check(I.get(k, "int_fixed").answer_verbatim == "用 >=6000，另外导出成 CSV",
          "重放没有污染已存的原话")


def t_crash_rollback(tmp: pathlib.Path) -> None:
    print("\n[9] 崩在 COMMIT 之前：答案必须整体回滚，不留半个状态")
    db = tmp / "crash2.db"
    rc = run_child("die_before_commit", db, BASE_T)
    check(rc == 9, "子进程在 COMMIT 前被强杀", f"exit={rc}")

    k, _ = boot(db)
    r = I.get(k, "int_rb")
    # 纪律：先断言前置条件确实发生过，否则"什么都没存"可能只是因为进程根本没跑起来
    check(r is not None, "前置条件：OPEN 命令在崩溃前已经成功提交")
    check(r is not None and r.status == I.Status.OPEN, "状态仍是 OPEN，没有半个 ANSWERED")
    check(r is not None and r.answer_verbatim is None, "回滚掉的答案没有落盘")
    with k.store.read() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM commands WHERE command_id='cid_ans2'"
                         ).fetchone()["c"]
    check(n == 0, "命令台账也一并回滚（业务变更与台账同事务）")
    check(k.check_invariants_now() == [], "崩溃恢复后不变量全通过")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("Interaction 表 + revision 绑定")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_f1s3_"))
    try:
        for fn in (t_schema, t_slot_caps, t_answer, t_revision_binding, t_artifact,
                   t_blockers, t_reconcile, t_crash_answer_survives, t_crash_rollback):
            try:
                fn(tmp)
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
    finally:
        close_all_stores()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 74)
    if passed == total:
        print(f"结果：{passed}/{total} 通过")
    else:
        print(f"结果：{passed}/{total} 通过 —— 失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_main(sys.argv[2], sys.argv[3], float(sys.argv[4]))
    else:
        sys.exit(main())
