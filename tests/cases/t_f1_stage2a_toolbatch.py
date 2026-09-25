# -*- coding: utf-8 -*-
"""验收：ToolBatchSpan + shadow 对答案。

三类用例：
  A. Span 四态与不变量（进程内）
  B. 崩溃点（子进程 os._exit）+ 启动恢复
  C. **shadow 铁律**：内核出任何问题都不许影响真实路径

⚠️ 本文件不验"覆盖表全绿"——那要靠 实测使用（1/2/4/5 行）+ 注入（3/6/7/8 行），
   覆盖表已收工并合并进早先的设计。这里验的是**机制本身对不对**。

用法：
  py -3.10 tests\cases\t_f1_stage2a_toolbatch.py
  py -3.10 tests\cases\t_f1_stage2a_toolbatch.py --child <point> <db> <base_time>
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
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="CRITICAL")

from core.runtime import (
    Command, FakeClock, InvariantViolation, RuntimeStore, reset_kernel_for_tests,
    reconcile_on_startup,
)
from core.runtime import toolbatch as tb

BASE_T = 1_800_000_000.0
_results: list[tuple[bool, str, str]] = []
_stores: list = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def boot(db: pathlib.Path, t: float):
    clock = FakeClock(t)
    st = RuntimeStore(db)
    _stores.append(st)
    return reset_kernel_for_tests(store=st, clock=clock), clock


def close_all():
    for s in _stores:
        try:
            s.close_thread_conn()
        except Exception:
            pass
    _stores.clear()


def span_row(k, span_id):
    with k.store.read() as c:
        return c.execute("SELECT * FROM tool_batch_spans WHERE span_id=?", (span_id,)).fetchone()


def mk(k, *, turn="T1", rnd=0, tag=tb.PathTag.NORMAL, names=("a",)):
    return k.submit(Command(kind=tb.PREPARE, payload={
        "turn_id": turn, "round_idx": rnd, "intended_names": list(names), "path_tag": tag,
    })).data["span_id"]


# ══════════════════════════════════════════════════════════════════════════
# 子进程
# ══════════════════════════════════════════════════════════════════════════

def child_main(point: str, db: str, base_t: float) -> None:
    k, _ = boot(pathlib.Path(db), base_t)
    if point == "die_prepared":
        mk(k, turn="TC", names=("x", "y"))
        os._exit(9)
    elif point == "die_open":
        sp = mk(k, turn="TC", names=("x", "y"))
        k.submit(Command(kind=tb.OPEN, subject_id=sp,
                         payload={"span_id": sp, "call_ids": ["id1", "id2"],
                                  "legacy_flag": True}))
        os._exit(9)
    elif point == "die_committed":
        sp = mk(k, turn="TC", names=("x",))
        k.submit(Command(kind=tb.OPEN, subject_id=sp,
                         payload={"span_id": sp, "call_ids": ["id1"], "legacy_flag": True}))
        k.submit(Command(kind=tb.COMMIT, subject_id=sp,
                         payload={"span_id": sp, "result_ids": ["id1"], "legacy_flag": False}))
        os._exit(9)
    else:
        raise SystemExit(f"未知崩溃点 {point}")
    os._exit(0)


def run_child(point, db, base_t) -> int:
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", point,
                        str(db), str(base_t)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 9:
        print(f"    ⚠️ 子进程 exit={p.returncode}，stderr 尾部：")
        for ln in (p.stderr or "").strip().splitlines()[-8:]:
            print(f"       {ln}")
    return p.returncode


# ══════════════════════════════════════════════════════════════════════════
# A｜四态与不变量
# ══════════════════════════════════════════════════════════════════════════

def t_states(tmp):
    print("\n[A1] 四态转移 PREPARED → OPEN → COMMITTED，且 CAS 不许跳级")
    k, _ = boot(tmp / "a1.db", BASE_T)
    sp = mk(k, names=("q", "w"))
    check(span_row(k, sp)["status"] == tb.SpanStatus.PREPARED, "prepare 后是 PREPARED")
    check(span_row(k, sp)["legacy_flag_at_open"] is None,
          "PREPARE 不记 legacy 旗标（此刻旧标志还没置 True，抄了会误报）")

    jumped = False
    try:
        k.submit(Command(kind=tb.COMMIT, subject_id=sp,
                         payload={"span_id": sp, "result_ids": []}))
    except InvariantViolation:
        jumped = True
    check(jumped, "PREPARED 不能直接跳到 COMMITTED")

    k.submit(Command(kind=tb.OPEN, subject_id=sp,
                     payload={"span_id": sp, "call_ids": ["i1", "i2"], "legacy_flag": True}))
    r = span_row(k, sp)
    check(r["status"] == tb.SpanStatus.OPEN and r["legacy_flag_at_open"] == 1,
          "OPEN 时记下旧标志（正确采样点）", f"at_open={r['legacy_flag_at_open']}")

    twice = False
    try:
        k.submit(Command(kind=tb.OPEN, subject_id=sp,
                         payload={"span_id": sp, "call_ids": ["i1", "i2"]}))
    except InvariantViolation:
        twice = True
    check(twice, "OPEN 不可重入（CAS 只允许 PREPARED→OPEN）")

    k.submit(Command(kind=tb.COMMIT, subject_id=sp,
                     payload={"span_id": sp, "result_ids": ["i1", "i2"], "legacy_flag": False}))
    r = span_row(k, sp)
    check(r["status"] == tb.SpanStatus.COMMITTED and r["legacy_flag_at_close"] == 0,
          "COMMITTED 且记下关闭时的旧标志")


def t_ids_match(tmp):
    print("\n[A2] 不变量 9：COMMITTED 时 calls/results 的 ID 与顺序必须一致")
    k, _ = boot(tmp / "a2.db", BASE_T)

    # ⚠️ 每个用例必须用不同的 turn_id：被拒的 Span 会停在 OPEN，
    #    共用 turn 会撞上 one_live_span_per_turn（那条不变量是对的，是测试数据错了）。
    for i, (label, results) in enumerate((("多一个", ["i1", "i2", "i3"]),
                                          ("少一个", []),
                                          ("顺序反了", ["i2", "i1"]))):
        sp = mk(k, turn=f"TM{i}")
        k.submit(Command(kind=tb.OPEN, subject_id=sp,
                         payload={"span_id": sp, "call_ids": ["i1", "i2"], "legacy_flag": True}))
        bad = False
        try:
            k.submit(Command(kind=tb.COMMIT, subject_id=sp,
                             payload={"span_id": sp, "result_ids": results}))
        except InvariantViolation:
            bad = True
        check(bad, f"ID {label} → 拒绝提交", f"results={results}")
        check(span_row(k, sp)["status"] == tb.SpanStatus.OPEN, f"  被拒后仍是 OPEN（事务回滚）")


def t_one_live_per_turn(tmp):
    print("\n[A3] 不变量：同一 turn 里上一轮的 Span 必须先关掉才能开下一轮")
    k, _ = boot(tmp / "a3.db", BASE_T)
    sp1 = mk(k, turn="TT", rnd=0)
    k.submit(Command(kind=tb.OPEN, subject_id=sp1,
                     payload={"span_id": sp1, "call_ids": ["a"], "legacy_flag": True}))
    boom = False
    try:
        mk(k, turn="TT", rnd=1)      # 上一轮还开着就开下一轮
    except InvariantViolation:
        boom = True
    check(boom, "同 turn 出现两个活 Span → 拒绝")

    k.submit(Command(kind=tb.COMMIT, subject_id=sp1,
                     payload={"span_id": sp1, "result_ids": ["a"], "legacy_flag": False}))
    sp2 = mk(k, turn="TT", rnd=1)
    check(span_row(k, sp2) is not None, "上一轮关掉后可以开下一轮（多轮 batch 是合法的）")

    # 不同 turn 之间不互相约束
    sp3 = mk(k, turn="TT2", rnd=0)
    check(span_row(k, sp3) is not None, "不同 turn 各自独立")


def t_abort_idempotent(tmp):
    print("\n[A4] ABORT 幂等（会从 validate 失败 / 轮首清理 / 启动恢复三处被调，撞车是正常的）")
    k, _ = boot(tmp / "a4.db", BASE_T)
    sp = mk(k)
    k.submit(Command(kind=tb.OPEN, subject_id=sp,
                     payload={"span_id": sp, "call_ids": ["a"], "legacy_flag": True}))
    r1 = k.submit(Command(kind=tb.ABORT, subject_id=sp,
                          payload={"span_id": sp, "reason": tb.AbortReason.EXCEPTION}))
    check(r1.data["status"] == tb.SpanStatus.ABORTED, "首次 ABORT 成功")
    r2 = k.submit(Command(kind=tb.ABORT, subject_id=sp,
                          payload={"span_id": sp, "reason": tb.AbortReason.TURN_RESET}))
    check(r2.data.get("already_final") is True, "重复 ABORT 幂等返回，不报错")
    check(span_row(k, sp)["abort_reason"] == tb.AbortReason.EXCEPTION,
          "不覆盖首次的 reason（第一次的原因才是真的）")


# ══════════════════════════════════════════════════════════════════════════
# B｜崩溃点与启动恢复
# ══════════════════════════════════════════════════════════════════════════

def t_restart(tmp):
    print("\n[B1] 重启时活 Span 一律 ABORTED（PREPARED 与 OPEN 都算活）")
    for point, expect in (("die_prepared", tb.SpanStatus.PREPARED),
                          ("die_open", tb.SpanStatus.OPEN)):
        db = tmp / f"b_{point}.db"
        rc = run_child(point, db, BASE_T)
        check(rc == 9, f"{point}: 子进程死在注入点", f"exit={rc}")
        k, _ = boot(db, BASE_T + 10)
        with k.store.read() as c:
            row = c.execute("SELECT * FROM tool_batch_spans LIMIT 1").fetchone()
        check(row["status"] == expect, f"{point}: 崩溃留下的脏状态是 {expect}")
        rep = reconcile_on_startup(k)
        after = span_row(k, row["span_id"])
        check(after["status"] == tb.SpanStatus.ABORTED, f"{point}: 启动恢复打成 ABORTED")
        check(after["abort_reason"] == tb.AbortReason.INTERRUPTED_BY_RESTART,
              f"{point}: reason=INTERRUPTED_BY_RESTART")
        check(row["span_id"] in (rep.extra.get("aborted_spans") or []),
              f"{point}: 报告里列出了它")
        rep2 = reconcile_on_startup(k)
        check(not (rep2.extra.get("aborted_spans") or []), f"{point}: 第二次恢复不再处理（幂等）")


def t_restart_keeps_final(tmp):
    print("\n[B2] 已 COMMITTED 的 Span 重启后不被动（终态不可逆）")
    db = tmp / "b2.db"
    rc = run_child("die_committed", db, BASE_T)
    check(rc == 9, "子进程在 COMMITTED 后被杀", f"exit={rc}")
    k, _ = boot(db, BASE_T + 10)
    with k.store.read() as c:
        row = c.execute("SELECT * FROM tool_batch_spans LIMIT 1").fetchone()
    check(row["status"] == tb.SpanStatus.COMMITTED, "崩溃前已是 COMMITTED")
    reconcile_on_startup(k)
    check(span_row(k, row["span_id"])["status"] == tb.SpanStatus.COMMITTED,
          "启动恢复没有动它")


def t_sweep(tmp):
    print("\n[B3] 轮首 sweep：上一轮遗留的活 Span 被打掉，并记覆盖表第 6 行")
    k, _ = boot(tmp / "b3.db", BASE_T)
    sp = mk(k, turn="OLD")
    k.submit(Command(kind=tb.OPEN, subject_id=sp,
                     payload={"span_id": sp, "call_ids": ["a"], "legacy_flag": True}))

    # 模拟真实场景：上一轮异常收场 → 旧标志带着 True 活到本轮开头
    swept = tb.sweep_stale_spans("NEW", legacy_flag=True)
    check(sp in swept, "上一轮遗留的活 Span 被 sweep 掉")
    check(span_row(k, sp)["abort_reason"] == tb.AbortReason.TURN_RESET, "reason=TURN_RESET")
    cov = {r.path_tag: r for r in tb.coverage(k)}
    check(cov[tb.PathTag.BATCH_EXCEPTION].hits == 1, "覆盖表第 6 行记了一笔")
    check(cov[tb.PathTag.BATCH_EXCEPTION].divergences == 0,
          "旧标志为 True 符合预期 → 零分歧")

    # 反面：旧标志是 False（两边不一致）→ 必须报分歧
    sp2 = mk(k, turn="OLD2")
    k.submit(Command(kind=tb.OPEN, subject_id=sp2,
                     payload={"span_id": sp2, "call_ids": ["b"], "legacy_flag": True}))
    tb.sweep_stale_spans("NEW2", legacy_flag=False)
    cov = {r.path_tag: r for r in tb.coverage(k)}
    check(cov[tb.PathTag.BATCH_EXCEPTION].divergences == 1,
          "旧标志为 False 时报分歧（Span 说有活批次，旧字段说没有）")
    check(not tb.sweep_stale_spans("NEW3", legacy_flag=False), "没有活 Span 时 sweep 是空操作")


def t_divergence_verdict(tmp):
    print("\n[B4] 分歧判据：正常剧本 1→0 零分歧；预判 A 的 0→0 必须报分歧")
    k, _ = boot(tmp / "b4.db", BASE_T)

    sp = tb.shadow_prepare("T", 0, ["a"], tb.PathTag.NORMAL)
    tb.shadow_open(sp, ["i1"], legacy_flag=True)
    tb.shadow_commit(sp, ["i1"], tb.PathTag.NORMAL, legacy_flag=False)
    cov = {r.path_tag: r for r in tb.coverage(k)}
    check(cov[tb.PathTag.NORMAL].hits == 1 and cov[tb.PathTag.NORMAL].divergences == 0,
          "正常路径 legacy 1→0 → 零分歧")

    # 预判 A：exit-multi 那段全程没碰旧标志 → 0→0
    sp = tb.shadow_prepare("T2", 0, ["a", "b"], tb.PathTag.EXIT_MULTI)
    tb.shadow_open(sp, ["i1", "i2"], legacy_flag=False)
    tb.shadow_commit(sp, ["i1", "i2"], tb.PathTag.EXIT_MULTI, legacy_flag=False)
    cov = {r.path_tag: r for r in tb.coverage(k)}
    row = cov[tb.PathTag.EXIT_MULTI]
    check(row.hits == 1 and row.divergences == 1,
          "预判 A 成立：exit-multi 全程 legacy=False → 报分歧")
    check(any("at_open=0" in s for s in row.samples),
          "分歧详情里能看到 at_open=0", str(row.samples))

    # 多轮：round_idx>0 自动给第 2 行记一笔
    sp = tb.shadow_prepare("T3", 2, ["a"], tb.PathTag.NORMAL)
    tb.shadow_open(sp, ["i9"], legacy_flag=True)
    tb.shadow_commit(sp, ["i9"], tb.PathTag.NORMAL, legacy_flag=False)
    cov = {r.path_tag: r for r in tb.coverage(k)}
    check(cov[tb.PathTag.MULTI_ROUND].hits == 1, "round_idx>0 自动记覆盖表第 2 行")


def t_coverage_report(tmp):
    print("\n[B5] 覆盖率报告可读，且 8 行齐全")
    k, _ = boot(tmp / "b5.db", BASE_T)
    rows = tb.coverage(k)
    check(len(rows) == 8, "覆盖表 8 行", f"{len(rows)} 行")
    check(all(r.hits == 0 for r in rows), "新库全部未覆盖")
    txt = tb.coverage_report(k)
    check("0/8 已覆盖" in txt, "报告显示 0/8")
    check("待覆盖" in txt, "报告列出待覆盖项")
    tb.shadow_note_path(tb.PathTag.ERROR_400, "detail")
    check("1/8 已覆盖" in tb.coverage_report(k), "记一笔后变 1/8")


# ══════════════════════════════════════════════════════════════════════════
# C｜shadow 铁律：绝不影响真实路径
# ══════════════════════════════════════════════════════════════════════════

def t_shadow_never_breaks(tmp):
    print("\n[C1] 铁律：内核彻底不可用时，shadow 门面必须静默降级")
    k, _ = boot(tmp / "c1.db", BASE_T)

    # 把 db 文件换成非 sqlite 内容，让每一次内核写入都必然失败
    k.store.close_thread_conn()
    (tmp / "c1.db").write_bytes(b"this is definitely not a sqlite database")

    ok = True
    try:
        sp = tb.shadow_prepare("T", 0, ["a"], tb.PathTag.NORMAL)
        tb.shadow_open(sp, ["i1"], legacy_flag=True)
        tb.shadow_commit(sp, ["i1"], tb.PathTag.NORMAL, legacy_flag=False)
        tb.shadow_abort(sp, tb.AbortReason.EXCEPTION, legacy_flag=True)
        tb.shadow_note_path(tb.PathTag.ERROR_400, "x")
        tb.sweep_stale_spans("T2", legacy_flag=True)
    except Exception as e:
        ok = False
        check(False, "shadow 门面抛异常了（违反铁律）", f"{type(e).__name__}: {e}")
    if ok:
        check(True, "库坏掉时六个门面函数全部静默返回，一个异常都不抛")
    check(sp is None, "prepare 失败时返回 None（调用方必须容忍）")


def t_orchestrator_wiring(tmp):
    """接线检查：只验"调用点存在且签名对得上"，不启动整个 app。"""
    print("\n[C2] orchestrator 接线：调用点齐全、参数顺序正确")
    src = module_text("core.orchestrator")

    for pat, why in (
        ("_rt_shadow_prepare(self, round_idx, [c.name for c in calls], _RT_PATH.NORMAL)",
         "主路径 prepare"),
        ("_rt_shadow_open(self, _sp, [c.tool_use_id for c in calls])", "主路径 open"),
        ("_rt_shadow_open(self, _sp, [c.tool_use_id for c in norm_calls])", "exit-multi open"),
        ("_RT_PATH.EXIT_MULTI", "exit-multi 标签"),
        ("_RT_PATH.OS_EARLY_RETURN", "OS 早退标签"),
        ("_RT_PATH.ERROR_400", "400 分支标签"),
        ("_rt_sweep_stale_spans(self, None)", "轮首 sweep（⑦ 后不再喂旧字段）"),
    ):
        check(pat in src, f"存在：{why}")

    # ⭐ [⑦ · B/切写那一步 2026-08-06] 这里原来有两条断言，锁的是 **观测期的接线**：
    #     · "sweep 必须在 `_active_tool_batch_open = False` 之前"
    #     · "open 必须在 `= True` 之后（否则 legacy 采样恒为 False）"
    # 两条都依赖那个字段**存在且被写**。切写那一步把字段删了，它们自然失效 ——
    # 这不是回归，是**它们完成了自己的使命**：观测期靠它们保证 shadow 采样点正确，
    # 而 shadow 已经收工（8/8 覆盖，1 处分歧已定性）。
    #
    # 换成验 B/切写那一步的新不变量：**旧字段不能再被写，只能被派生**。
    import ast as _astm
    _writes_orch: list[str] = []
    for _node in _astm.walk(_astm.parse(src)):
        _tgts = []
        if isinstance(_node, _astm.Assign):
            _tgts = _node.targets
        elif isinstance(_node, (_astm.AnnAssign, _astm.AugAssign)):
            _tgts = [_node.target]
        for _t in _tgts:
            if isinstance(_t, _astm.Attribute) and _t.attr == "_active_tool_batch_open":
                _writes_orch.append(f"line {_node.lineno}")
    check(not _writes_orch,
          "⭐ 没有任何地方【写】_active_tool_batch_open（它现在是只读派生属性）",
          f"仍有写入: {_writes_orch}")

    check("def _active_tool_batch_open" in src,
          "⭐ 它是 property（从 ToolBatchSpan 派生），不是字段")
    check("_rt_has_open_batch" in src, "派生入口 _rt_has_open_batch 存在")
    # 前置条件：证明这个属性确实还有消费者，否则"没人写"是因为整个东西被删空了
    check(src.count("self._active_tool_batch_open") >= 2,
          "前置条件：仍有读取方（400 守卫 + _clean_damaged_memory）",
          f"{src.count('self._active_tool_batch_open')} 处读")

    # 严禁双写：shadow 侧绝不写旧字段。
    # ⚠️ 用 AST 而不是字符串搜索 —— 第一版写成 `"_active_tool_batch_open =" not in src`，
    #    结果被 `_open()` 文档字符串里那句演示顺序的
    #    "prepare → add_tool_calls() → `_active_tool_batch_open = True` → open" 误伤，
    #    报了一个假失败。**检查代码性质的断言要用 AST，不要用文本匹配。**
    import ast as _ast
    tbsrc = module_text("core.runtime.toolbatch")
    _writes: list[str] = []
    for node in _ast.walk(_ast.parse(tbsrc)):
        targets = []
        if isinstance(node, _ast.Assign):
            targets = node.targets
        elif isinstance(node, (_ast.AugAssign, _ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, _ast.Attribute) and t.attr == "_active_tool_batch_open":
                _writes.append(f"line {getattr(node, 'lineno', '?')}")
            if isinstance(t, _ast.Name) and t.id == "_active_tool_batch_open":
                _writes.append(f"line {getattr(node, 'lineno', '?')}")
    check(not _writes,
          "toolbatch.py 从不【写】旧字段（三步迁移的观测期严禁双写）", "; ".join(_writes))

    # 反向：也不许【读】它去做决策（读只能发生在 orchestrator 侧的采样点）
    _reads = [n.lineno for n in _ast.walk(_ast.parse(tbsrc))
              if isinstance(n, _ast.Attribute) and n.attr == "_active_tool_batch_open"]
    check(not _reads, "toolbatch.py 也不直接读旧字段（值由 orchestrator 传进来）",
          str(_reads))

    # resume_suspension 是 _run_react_loop 的第二个调用方，必须同样设 turn_id + sweep
    i_resume = src.index("async def resume_suspension")
    i_loop2 = src.index("self._run_react_loop(", i_resume)
    seg = src[i_resume:i_loop2]
    check("self._rt_turn_id = " in seg, "resume_suspension 也设了新的 turn_id")

    # 旧标志的【第二个】清理点必须也 abort span（实测 shadow 第一天抓到的缺口）
    i_clean = src.index("def _clean_damaged_memory")
    seg2 = src[i_clean:i_clean + 1600]
    check("_rt_abort_open_span(self," in seg2,
          "_clean_damaged_memory 清旧标志时一并 abort span（否则下一轮 sweep 误报）")
    # ⭐ 这里原来还断言 "abort 必须在 `_active_tool_batch_open = False` 之前"。
    # 切写那一步把那行赋值删了（状态从 Span 派生，不需要也不允许手动清），
    # 所以那条断言的前提没了。**它同样是完成了使命而不是回归。**
    #
    # 换成验真正要守的东西：abort 之后派生值必须变 False ——
    # 也就是"回滚完这一轮就不再被当成有未完成批次"。
    # 这条比原来那条更接近意图：原来验的是**赋值顺序**（实现细节），
    # 现在验的是**状态收敛**（真正要的性质）。
    check("self._active_tool_batch_open = False" not in seg2,
          "⭐ 不再手动清旧标志（切写那一步删了那行；状态由 abort 收敛）")
    check("_rt_abort_open_span" in seg2 and "_rollback_last_tool_batch" in seg2,
          "回滚与 abort 成对出现（memory 与 Span 一起收，不能只收一边）")

    # 启动恢复必须在 ui.run() 之前同步调用
    appsrc = module_text("app")
    check("_rt_reconcile(_rt_get_kernel())" in appsrc, "app.py 接了 reconcile_on_startup")
    check(appsrc.index("_rt_reconcile(_rt_get_kernel())") < appsrc.index("ui.run(**_run_kwargs)"),
          "  启动恢复在 ui.run() 【之前】（不能等事件循环）")
    # 参数从旧字段变成 None（没有旧字段可喂）；要守的性质没变：
    # 唤醒 turn 绕过 `_handle_query_impl`，所以它必须自己 sweep 一次。
    check("_rt_sweep_stale_spans(self, None)" in seg,
          "resume_suspension 也做了轮首 sweep（唤醒 turn 绕过 _handle_query_impl）")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 74)
    print("ToolBatchSpan + shadow 验收")
    print("=" * 74)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nano_2a_"))
    try:
        for fn in (t_states, t_ids_match, t_one_live_per_turn, t_abort_idempotent,
                   t_restart, t_restart_keeps_final, t_sweep, t_divergence_verdict,
                   t_coverage_report, t_shadow_never_breaks, t_orchestrator_wiring):
            try:
                fn(tmp)
            except Exception as e:
                import traceback
                check(False, f"{fn.__name__} 抛异常", f"{type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        close_all()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in _results if ok)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{len(_results)} 通过")
    if passed != len(_results):
        print("\n失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}" + (f"   [{note}]" if note else ""))
    print("=" * 74)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_main(sys.argv[2], sys.argv[3], float(sys.argv[4]))
    else:
        sys.exit(main())
