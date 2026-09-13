# -*- coding: utf-8 -*-
"""OSActivityLease / AuthorizationLease 验收。

═══ 这个套件要证明什么 ═══

不是"租约能拿能还"，而是 **`_os_task_busy` 那类泄漏在结构上不可能再出现**。

现状（已核代码）：`orchestrator.py` 的 `os_execute` 分支置 `_os_task_busy = True`
之后**没有 `try/finally`**（`_run_os_skill_plan_loop` 那处有）。生成器被丢弃或抛异常
就永久卡 True → `canary.should_run(...)` 恒假 → **视觉自检从此不跑，且一声不响**。
目前靠"每轮重置"把爆炸半径压到一轮 —— 止血，不是修好。

**一个裸 bool 表达不了"谁持有、持有多久、过期算谁的"，所以它防不住任何一种泄漏。**

所以这里逐条钉：
  · [2] 过期是**推导**的，不需要任何人记得去清（对应泄漏）
  · [3] fence —— 卡死的持有者回来时安静作废（GUI 场景：不能和用户抢鼠标）
  · [4] 互斥有**数据库级**保证，不只靠不变量
  · [5] activity 与 authorization 规则**相反**，重启处置也相反
  · [6] 被动挂起 = 用户拿走活动租约

⚠️ 每条"某事不会发生"的断言前面都先断言前置条件成立。

用法：
  py -3.10 tests\t_f1_stage5_oslease.py
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
from core.runtime import oslease as L

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    _task.clear_blocker_providers_for_tests()
    clock = FakeClock(BASE_T)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=clock), clock


def _acq(k, **kw):
    p = {"holder": L.Holder.NANO, "reason": "跑一段 GUI 自动化"}
    p.update(kw)
    return k.submit(Command(kind=L.ACQUIRE, payload=p)).data


# ══════════════════════════════════════════════════════════════════════════

def t_acquire_and_mutex(tmp: pathlib.Path) -> None:
    print("\n[1] 拿租约 + 互斥")
    k, _ = make_kernel(tmp / "a")

    d = _acq(k)
    rec = L.get(k, d["lease_id"])
    check(rec is not None and rec.is_held, "拿到了，状态 HELD")
    check(rec.held_until is not None,
          "⭐ 活动租约**必须**有期限（没期限 = 永远不会自己松开的裸 bool）")
    check(d["fence"] == 1, "初始 fence = 1")

    cur = L.current_activity(k)
    check(cur is not None and cur.lease_id == d["lease_id"], "current_activity 读得到")
    ok, why = L.nano_may_touch_os(k)
    check(ok, "Nano 自己持有时可以操作", why)

    # 互斥
    try:
        _acq(k, holder=L.Holder.USER)
        check(False, "⭐ 已有人持有时不该给")
    except KernelError as e:
        check("已被" in str(e), "⭐ 互斥生效：未过期时拿不到第二个", str(e)[:36])

    # 显式抢占才给
    d2 = _acq(k, holder=L.Holder.USER, preempt=True, preempt_reason="用户动了鼠标")
    check(L.get(k, d["lease_id"]).status == L.LeaseStatus.PREEMPTED,
          "⭐ 显式 preempt=True 时旧的被打成 PREEMPTED")
    check(L.get(k, d2["lease_id"]).is_held, "新持有者拿到了")

    ok2, why2 = L.nano_may_touch_os(k)
    check(not ok2, "⭐⭐ 用户持有时 Nano **不能**操作 —— 被动挂起就是这个语义", why2)


def t_expiry_is_derived(tmp: pathlib.Path) -> None:
    """⭐ 对应 `_os_task_busy` 泄漏。"""
    print("\n[2] ⭐⭐ 过期是推导的，不需要任何人记得去清")
    k, clock = make_kernel(tmp / "b")

    d = _acq(k, ttl_sec=60)
    check(L.current_activity(k) is not None, "前置条件：现在确实持有")

    clock.advance(61)
    # ⚠️ 注意：这里**没有跑任何 tick、没有任何人调 release**
    check(L.current_activity(k) is None,
          "⭐⭐ 到期后 current_activity 立刻不算它 —— **没跑 tick、没人 release**")
    ok, _ = L.nano_may_touch_os(k)
    check(ok, "⭐ 于是新的操作可以进行（裸 bool 时代这里会永久卡住）")

    # 而且下一个 acquire 会顺手把它收掉，不用等 tick
    d2 = _acq(k)
    check(L.get(k, d["lease_id"]).status == L.LeaseStatus.EXPIRED,
          "⭐ 旧的被 acquire 顺手收成 EXPIRED（不用等 tick 到来）",
          L.get(k, d["lease_id"]).status)
    check(L.get(k, d2["lease_id"]).is_held, "新的正常拿到")

    # tick 也能收（两条路都有）
    k2, clock2 = make_kernel(tmp / "b2")
    d3 = _acq(k2, ttl_sec=30)
    clock2.advance(31)
    n = L.expire_tick(k2)
    check(n == 1 and L.get(k2, d3["lease_id"]).status == L.LeaseStatus.EXPIRED,
          "⭐ tick 也会回收（level-triggered，两条路互不依赖）", str(n))


def t_fence(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐ fence：被接管的持有者安静作废")
    k, _ = make_kernel(tmp / "c")

    d = _acq(k)
    f_old = d["fence"]
    # 心跳能续
    k.submit(Command(kind=L.HEARTBEAT, payload={"lease_id": d["lease_id"], "fence": f_old}))
    check(L.get(k, d["lease_id"]).is_held, "自己的心跳能续期")
    check(L.get(k, d["lease_id"]).fence == f_old,
          "⚠️ 心跳**不 bump fence** —— 它证明「我还活着」，不是「我重新拿了一次」",
          str(L.get(k, d["lease_id"]).fence))

    # 被抢占后，旧持有者拿旧 fence 回来
    k.submit(Command(kind=L.PREEMPT, payload={"by": L.Holder.USER, "reason": "用户动手"}))
    check(L.get(k, d["lease_id"]).status == L.LeaseStatus.PREEMPTED, "已被抢占")
    try:
        k.submit(Command(kind=L.HEARTBEAT,
                         payload={"lease_id": d["lease_id"], "fence": f_old}))
        check(False, "被抢占后不该还能续期")
    except KernelError:
        check(True, "⭐ 被抢占后旧持有者续不了期（它已经不是持有者了）")

    # release 用旧 fence：不抛，安静忽略
    k2, _ = make_kernel(tmp / "c2")
    d2 = _acq(k2)
    k2.submit(Command(kind=L.PREEMPT, payload={"by": L.Holder.USER}))
    r = k2.submit(Command(kind=L.RELEASE,
                          payload={"lease_id": d2["lease_id"], "fence": 99})).data
    check(r.get("already") or r.get("stale"),
          "⭐⭐ 归还时 fence 对不上**不抛异常**，安静忽略 —— "
          "被抢占是正常运行的一部分，为它抛异常会让 finally 很难写", str(r))


def t_db_level_mutex(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐ 互斥有数据库级保证，不只靠不变量")
    k, _ = make_kernel(tmp / "d")
    _acq(k)
    broken = k.check_invariants_now()
    check(not broken, "干净库不变量全过", str(broken))

    # 绕过 Command 手工插第二条 HELD 活动租约 → 唯一索引应当拦住
    import sqlite3
    hit = False
    try:
        with k.store.write_txn() as conn:
            conn.execute(
                "INSERT INTO os_leases (lease_id,kind,status,holder,fence,scope,reason,"
                "held_until,payload,revision,created_at,updated_at) VALUES "
                "('l_dup','activity','HELD','nano',1,'','',?,'{}',1,?,?)",
                (BASE_T + 999, BASE_T, BASE_T))
    except sqlite3.IntegrityError:
        hit = True
    check(hit,
          "⭐⭐ **数据库唯一索引**直接拦住第二个 HELD 活动租约 —— "
          "竞态下「两个都以为自己在开车」是最危险的，不能只靠事后不变量")

    # ⚠️ 这里本来还想手工造一条"两个 HELD 活动租约"来验不变量，
    #    结果**造不出来** —— 唯一索引在 INSERT 和 UPDATE 两条路上都拦住了。
    #    这是好消息（保证比预期强），但也意味着 `single_activity` 那条不变量
    #    在正常库上永远不会触发。它留着是**纵深防御**：
    #    索引哪天被误删、或者迁移时漏建，不变量还能响一声。
    #
    # 换验另一条能造出来的：活动租约没有 held_until（= 永远不会自己松开）。
    k2, _ = make_kernel(tmp / "d2")
    check(not k2.check_invariants_now(), "前置条件：新库干净")
    with k2.store.write_txn() as conn:
        conn.execute(
            "INSERT INTO os_leases (lease_id,kind,status,holder,fence,scope,reason,"
            "held_until,payload,revision,created_at,updated_at) VALUES "
            "('l_nodl','activity','HELD','nano',1,'','',NULL,'{}',1,?,?)",
            (BASE_T, BASE_T))
    broken2 = k2.check_invariants_now()
    check(any("has_deadline" in str(b) for b in broken2),
          "⭐⭐ 不变量抓得住「活动租约没有期限」—— 那就是裸 bool 泄漏在库里的形状",
          str(broken2)[:70])


def t_authorization_is_different(tmp: pathlib.Path) -> None:
    print("\n[5] ⭐⭐ 授权租约的规则与活动租约【相反】")
    k, clock = make_kernel(tmp / "e")

    a1 = k.submit(Command(kind=L.GRANT, payload={
        "scope": "os.click", "ttl_sec": 300, "reason": "本次任务免确认"})).data
    a2 = k.submit(Command(kind=L.GRANT, payload={
        "scope": "os.type", "ttl_sec": 300})).data
    check(len(L.active_authorizations(k)) == 2,
          "⭐ 授权**可以并存多条**（活动租约互斥，这里相反）")
    check(L.is_authorized(k, "os.click") and L.is_authorized(k, "os.type"),
          "各自 scope 都命中")
    check(not L.is_authorized(k, "os.delete"), "没授权的 scope 不命中")

    # 无期限授权是允许的（活动租约则被不变量禁止）
    a3 = k.submit(Command(kind=L.GRANT, payload={
        "scope": "os.scroll", "ttl_sec": None})).data
    check(L.get(k, a3["lease_id"]).held_until is None,
          "⭐ 授权**允许**无期限（用户有意给的长期许可）")
    check(not k.check_invariants_now(),
          "⚠️ 且不违反不变量 —— 而同样无期限的**活动**租约会违反（见 [4]）")

    clock.advance(301)
    check(len(L.active_authorizations(k)) == 1,
          "到期的授权自动不算，剩下那条无期限的", str(len(L.active_authorizations(k))))

    # 撤销
    k.submit(Command(kind=L.REVOKE, payload={"scope": "os.scroll"}))
    check(not L.active_authorizations(k), "按 scope 撤销生效")


def t_startup_asymmetry(tmp: pathlib.Path) -> None:
    print("\n[6] ⭐⭐ 重启处置：活动租约释放，授权租约不动")
    k, _ = make_kernel(tmp / "f")
    act = _acq(k)
    auth = k.submit(Command(kind=L.GRANT, payload={
        "scope": "os.click", "ttl_sec": None, "reason": "用户给的长期许可"})).data

    n = L.startup_release_all(k)
    check(n == 1, "只释放 1 条", str(n))
    check(L.get(k, act["lease_id"]).status == L.LeaseStatus.EXPIRED,
          "⭐ 活动租约被释放 —— 持有者（上个进程里那段点鼠标的代码）已经不存在了")
    check(L.get(k, auth["lease_id"]).is_held,
          "⭐⭐ 授权租约**不动** —— 用户给的许可不随进程生死")

    # 📌 与 waitcond.startup_sweep 同一条判据
    check(L.current_activity(k) is None and L.is_authorized(k, "os.click"),
          "⚠️ 判据：收什么取决于「它的执行体还在不在」，不是「它是不是一条记录」")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_acquire_and_mutex(tmp)
        t_expiry_is_derived(tmp)
        t_fence(tmp)
        t_db_level_mutex(tmp)
        t_authorization_is_different(tmp)
        t_startup_asymmetry(tmp)
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
