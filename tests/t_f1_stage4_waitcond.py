# -*- coding: utf-8 -*-
"""WaitCondition 内核模块验收。

═══ 这个套件真正要证明的事 ═══

不是"CRUD 能跑"，而是 **2026-08-04 那条"不死的挂起"在结构上不可能再出现**。

那次事故的三条唤醒路径同时堵死：
  ① `due_timers()` 的 SQL 含 `timer_at IS NOT NULL`，而它是 None → 永远查不到
  ② 用户发消息时，background-only 的挂起只注入、不 resolve
  ③ 后台完成回调是**边沿信号**，任务没回来就永远不触发

所以这里逐条钉：
  · [2] `fire_at` / `expire_at` 语义相反，不许再挤进一个字段（对应 ①）
  · [3] SATISFIED 与 CONSUMED 分离 —— 结果落盘后崩溃也捞得回来（对应 ③）
  · [4] `orphan_at` 无条件兜底，且**不变量**保证活记录必须有它（对应"三条全堵死"）
  · [5] 启动清理只收 background、不碰 timer 和 SATISFIED

⚠️ 每条"某事没发生"的断言前面都先断言前置条件成立 ——
   否则记录压根没建起来也会让断言恒真地绿。

用法：
  py -3.10 tests\t_f1_stage4_waitcond.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护，必须在任何 print 之前

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import Command, KernelError, reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import task as _task
from core.runtime import waitcond as W

BASE_T = 1_700_000_000.0

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path, t: float = BASE_T):
    _task.clear_blocker_providers_for_tests()
    st = RuntimeStore(tmp / "rt.db")
    clock = FakeClock(t)
    return reset_kernel_for_tests(store=st, clock=clock), clock


def _open(k, **kw):
    p = {"kind": W.WaitKind.BACKGROUND, "wake_on": [W.WakeSource.BACKGROUND],
         "reason": "等一个后台任务"}
    p.update(kw)
    return k.submit(Command(kind=W.OPEN, payload=p)).data["wait_id"]


# ══════════════════════════════════════════════════════════════════════════

def t_open_basics(tmp: pathlib.Path) -> None:
    print("\n[1] 建立等待：枚举校验 + 拒绝天生的不死记录")
    k, _ = make_kernel(tmp / "a")

    wid = _open(k)
    rec = W.get(k, wid)
    check(rec is not None and rec.status == W.WaitStatus.WAITING,
          "开出来是 WAITING", rec.status if rec else "None")
    check(rec.is_live, "算活的")
    check(rec.orphan_at is not None,
          "⭐ 自动带上 orphan_at（调用方没给也必须有）", str(rec.orphan_at))

    try:
        _open(k, wake_on=[])
        check(False, "⭐ 空 wake_on 应当被拒绝")
    except KernelError as e:
        check("唤醒源" in str(e),
              "⭐ 空 wake_on 直接拒绝 —— 没有唤醒源的等待天生不死", str(e)[:40])

    try:
        _open(k, wake_on=["telepathy"])
        check(False, "未知唤醒源应当被拒绝")
    except KernelError:
        check(True, "未知唤醒源被拒绝")

    try:
        _open(k, kind="daydream")
        check(False, "未知 kind 应当被拒绝")
    except KernelError:
        check(True, "未知 kind 被拒绝")


def t_fire_vs_expire(tmp: pathlib.Path) -> None:
    """⭐ 对应不死挂起的第一条路径。"""
    print("\n[2] ⭐⭐ fire_at（到点=满足）与 expire_at（截止=没等到）是两件事")
    k, clock = make_kernel(tmp / "b")

    # 没有任何时间字段的纯后台等待 —— 旧实现里这种记录**任何查询都捞不到**
    bg = _open(k)
    check(W.get(k, bg).fire_at is None,
          "前置条件：这条等待确实没有 fire_at（就是旧 bug 的那个形状）")
    check(any(r.wait_id == bg for r in W.list_live(k)),
          "⭐ 它仍然出现在 list_live 里 —— 不再因为没设定时就人间蒸发")

    # 到点 → 满足（好事）
    timer = _open(k, kind=W.WaitKind.TIMER, wake_on=[W.WakeSource.TIMER],
                  fire_at=BASE_T + 60, reason="60 秒后提醒")
    check(not W.due_now(k), "还没到点 → due_now 是空的")
    clock.advance(61)
    due = W.due_now(k)
    check(len(due) == 1 and due[0].wait_id == timer, "到点后 due_now 捞得到")

    W.tick(k)
    # ⚠️ 这两条断言 2026-08-07 改过。原来写的是「到点 → SATISFIED」，
    # 那是把 外部评审 交叉评审指出的错误锁成了"正确行为"：
    #   > 唤醒不能等于完成。Timer 到点只表示"值得继续/检查"。
    # 现在到点进 DUE_FOR_REVIEW，由上层看过之后再决定。
    check(W.get(k, timer).status == W.WaitStatus.DUE_FOR_REVIEW,
          "⭐⭐ 到点的结果是 **DUE_FOR_REVIEW**（该看一眼），**不是** SATISFIED",
          W.get(k, timer).status)
    check(W.get(k, timer).is_live,
          "⚠️ 它仍然算活的 —— 还没查呢，这事没完")
    check(any(r.wait_id == timer for r in W.list_due_for_review(k)),
          "⭐ list_due_for_review 捞得到它（上层靠这个知道该去查什么）")
    check(not any(r.wait_id == timer for r in W.list_satisfied(k)),
          "⭐⭐ **不**出现在 list_satisfied 里 —— 那是「已经成了」的清单")
    # 看过之后确认满足 → 才 SATISFY
    k.submit(Command(kind=W.SATISFY, payload={
        "wait_id": timer, "satisfied_by": "review", "result": {"ok": True}}))
    check(W.get(k, timer).status == W.WaitStatus.SATISFIED,
          "⭐ 查过并确认之后，才由上层推进到 SATISFIED")

    # 到截止 → 过期（坏事）
    k2, clock2 = make_kernel(tmp / "b2")
    dead = k2.submit(Command(kind=W.OPEN, payload={
        "kind": W.WaitKind.EXTERNAL, "wake_on": [W.WakeSource.BACKGROUND],
        "expire_at": BASE_T + 30, "reason": "等一个不会来的东西"})).data["wait_id"]
    clock2.advance(31)
    W.tick(k2)
    check(W.get(k2, dead).status == W.WaitStatus.EXPIRED,
          "⭐ 到截止的结果是 **EXPIRED**（坏事）")
    check(W.get(k2, dead).resolution == W.Resolution.DEADLINE,
          "原因记的是 DEADLINE")

    # 同一跳里既到点又到截止 → 到点优先
    k3, clock3 = make_kernel(tmp / "b3")
    both = k3.submit(Command(kind=W.OPEN, payload={
        "kind": W.WaitKind.TIMER, "wake_on": [W.WakeSource.TIMER],
        "fire_at": BASE_T + 10, "expire_at": BASE_T + 10})).data["wait_id"]
    clock3.advance(11)
    W.tick(k3)
    check(W.get(k3, both).status == W.WaitStatus.DUE_FOR_REVIEW,
          "⭐ 同时到点又到截止 → **先给一次查看机会**，不直接判失败",
          W.get(k3, both).status)


def t_satisfy_consume_split(tmp: pathlib.Path) -> None:
    """⭐ 对应不死挂起的第三条路径：边沿信号。"""
    print("\n[3] ⭐⭐ SATISFIED / CONSUMED 分离 —— 边沿变状态")
    k, _ = make_kernel(tmp / "c")
    wid = _open(k, bg_ref="mcp_toolu_abc")

    k.submit(Command(kind=W.SATISFY, payload={
        "wait_id": wid, "satisfied_by": "playwright",
        "result": {"url": "https://example.com", "ok": True}}))
    rec = W.get(k, wid)
    check(rec.status == W.WaitStatus.SATISFIED, "满足后是 SATISFIED")
    check(rec.result == {"url": "https://example.com", "ok": True},
          "⭐ 结果与状态**同事务落盘**", str(rec.result))
    check(rec.awaiting_consumption, "标记为「等人来取」")
    check(rec.is_live, "⚠️ SATISFIED 仍算活的 —— 结果没被取走这件事就没完")

    found = W.list_satisfied(k)
    check(len(found) == 1 and found[0].wait_id == wid,
          "⭐⭐ list_satisfied 捞得到 —— 这就是「重启后照样能捞」的兑现方式")

    # 消费一次，且只能一次
    r1 = k.submit(Command(kind=W.CONSUME, payload={"wait_id": wid})).data
    check(r1["consumed"] is True and r1["result"]["ok"] is True,
          "第一次消费拿到结果")
    r2 = k.submit(Command(kind=W.CONSUME, payload={"wait_id": wid})).data
    check(r2["consumed"] is False,
          "⭐ 第二次消费拿不到（CAS）—— 同一个结果不会被用两次")
    check(W.get(k, wid).status == W.WaitStatus.CONSUMED, "终态是 CONSUMED")
    check(W.get(k, wid).result is not None,
          "⚠️ 消费之后结果仍留在库里 —— 事后要能回答「当时拿到了什么」")

    # 重复满足是幂等的（outbox 重试是常态）
    k2, _ = make_kernel(tmp / "c2")
    w2 = _open(k2)
    k2.submit(Command(kind=W.SATISFY, payload={"wait_id": w2, "result": {"n": 1}}))
    again = k2.submit(Command(kind=W.SATISFY, payload={"wait_id": w2, "result": {"n": 2}})).data
    check(again.get("already") is True, "⭐ 重复满足幂等（outbox 会重投）")
    check(W.get(k2, w2).result == {"n": 1},
          "⚠️ 且保留**第一次**的结果，后到的不覆盖", str(W.get(k2, w2).result))


def t_orphan_backstop(tmp: pathlib.Path) -> None:
    """⭐ 明确要求："即使前两条都失效也不会留下不死记录"。"""
    print("\n[4] ⭐⭐ 兜底回收：三条路径全堵死也收得掉")
    k, clock = make_kernel(tmp / "d")

    # 完全复刻 2026-08-04 那两条孤儿记录的形状：
    # 只有 background 唤醒源、没有 fire_at、没有 expire_at
    wid = _open(k, wake_on=[W.WakeSource.BACKGROUND],
                reason="running in background: playwright · browser_navigate")
    rec = W.get(k, wid)
    check(rec.fire_at is None and rec.expire_at is None,
          "前置条件：这就是那条不死记录的形状（无 fire_at、无 expire_at）")
    check(list(rec.wake_on) == [W.WakeSource.BACKGROUND],
          "前置条件：唤醒源只有 background")

    clock.advance(W.DEFAULT_ORPHAN_AGE_SEC - 5)
    W.tick(k)
    check(W.get(k, wid).status == W.WaitStatus.WAITING,
          "⚠️ 没到兜底年龄之前不动它（别把还在跑的任务误杀）")

    clock.advance(10)
    stats = W.tick(k)
    check(stats["orphaned"] == 1, "⭐ 到兜底年龄 → 回收", str(stats))
    check(W.get(k, wid).status == W.WaitStatus.ORPHANED,
          "⭐⭐ 终态是 ORPHANED —— 不死记录在这里被终结")
    check(W.get(k, wid).resolution == W.Resolution.NEVER_RETURNED,
          "⚠️ 原因是 NEVER_RETURNED，不是 USER_CANCELLED —— 用户没取消任何东西")
    check(not W.get(k, wid).is_live, "不再是活记录，不会再被注入")

    # 不变量：活着的记录不许没有 orphan_at
    broken = k.check_invariants_now()
    check(not broken, "干净库的不变量全过", str(broken))
    with k.store.write_txn() as conn:  # 绕过 Command 手工造一条不死记录
        conn.execute(
            "INSERT INTO wait_conditions (wait_id,kind,status,wake_on,reason,"
            "orphan_at,revision,created_at,updated_at) VALUES "
            "('w_immortal','background','WAITING','[\"background\"]','',NULL,1,?,?)",
            (BASE_T, BASE_T))
    broken2 = k.check_invariants_now()
    check(any("immortal" in str(b) or "wait_no_immortal" in str(b) for b in broken2),
          "⭐⭐ 不变量抓得住「活着却没有 orphan_at」的记录", str(broken2)[:80])


def t_startup_sweep(tmp: pathlib.Path) -> None:
    print("\n[5] 启动清理：只收 background，不碰 timer / SATISFIED")
    k, _ = make_kernel(tmp / "e")

    bg = _open(k, kind=W.WaitKind.BACKGROUND, wake_on=[W.WakeSource.BACKGROUND])
    tm = _open(k, kind=W.WaitKind.TIMER, wake_on=[W.WakeSource.TIMER],
               fire_at=BASE_T + 3600)
    done = _open(k)
    k.submit(Command(kind=W.SATISFY, payload={"wait_id": done, "result": {"x": 1}}))

    n = W.startup_sweep(k)
    check(n == 1, "只收掉 1 条", str(n))
    check(W.get(k, bg).status == W.WaitStatus.ORPHANED,
          "⭐ 后台等待被收 —— 执行体随上个进程消失了，不可能再回来")
    check(W.get(k, bg).resolution == W.Resolution.INTERRUPTED_BY_RESTART,
          "原因记的是 INTERRUPTED_BY_RESTART")
    check(W.get(k, tm).status == W.WaitStatus.WAITING,
          "⭐⭐ **定时等待不动** —— 到点是纯时间函数，重启后照样成立")
    check(W.get(k, done).status == W.WaitStatus.SATISFIED,
          "⭐⭐ **已满足的不动** —— 结果已落盘，重启不该让它蒸发")


def t_blockers_and_cancel(tmp: pathlib.Path) -> None:
    print("\n[6] Task 阻塞 + 取消")
    k, _ = make_kernel(tmp / "f")
    tid = k.submit(Command(kind=_task.CREATE, payload={
        "kind": _task.TaskKind.BACKGROUND_JOB, "goal_summary": "跑个后台的活"})).data["task_id"]

    wid = _open(k, owner_task_id=tid, reason="等浏览器")
    view = _task.get_task_view(k, tid)
    check(any(b.blocker_id == wid for b in view.blockers),
          "⭐ WAITING 阻塞它的 Task", str([b.blocker_id for b in view.blockers]))

    k.submit(Command(kind=W.SATISFY, payload={"wait_id": wid, "result": {}}))
    view2 = _task.get_task_view(k, tid)
    check(any(b.blocker_id == wid for b in view2.blockers),
          "⭐⭐ SATISFIED **仍然**阻塞 —— 结果没被用掉，这个 Task 还走不了")

    k.submit(Command(kind=W.CONSUME, payload={"wait_id": wid}))
    view3 = _task.get_task_view(k, tid)
    check(not any(b.blocker_id == wid for b in view3.blockers),
          "消费之后才解除阻塞")

    # 取消：WAITING 和 SATISFIED 都能取消，终态不能
    w2 = _open(k)
    k.submit(Command(kind=W.CANCEL, payload={"wait_id": w2}))
    check(W.get(k, w2).status == W.WaitStatus.CANCELLED, "WAITING 可取消")
    w3 = _open(k)
    k.submit(Command(kind=W.SATISFY, payload={"wait_id": w3, "result": {}}))
    k.submit(Command(kind=W.CANCEL, payload={"wait_id": w3}))
    check(W.get(k, w3).status == W.WaitStatus.CANCELLED,
          "⭐ SATISFIED 也可取消（结果回来了但用户说不要了）")
    try:
        k.submit(Command(kind=W.EXPIRE, payload={"wait_id": w3}))
        check(False, "终态不该还能过期")
    except KernelError:
        check(True, "⚠️ 已经终结的不能再被过期/回收（防止状态倒流）")


def main() -> int:
    # ⚠️ `ignore_cleanup_errors` 不能省：Windows 上 SQLite 连接还没释放时
    # rmtree 会抛 NotADirectoryError，**把整份测试结果盖掉** ——
    # 一个纯清理动作不该成为失败源（同 tests/_console.py 那条判据）。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_open_basics(tmp)
        t_fire_vs_expire(tmp)
        t_satisfy_consume_split(tmp)
        t_orphan_backstop(tmp)
        t_startup_sweep(tmp)
        t_blockers_and_cancel(tmp)
    passed = sum(1 for r in _results if r[0])
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
    sys.exit(main())
