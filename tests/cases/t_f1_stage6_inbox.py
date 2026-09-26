# -*- coding: utf-8 -*-
"""durable inbox —— 「用户的话永不丢」。

═══ 这个套件盯的核心只有一条 ═══

**没有任何一条路径会让一句用户的话消失**，除了用户自己显式要求（重置对话）。

它取代的是 `app.py` 里那句
`if self.pipeline_lock.locked(): ui.notify('内核正在处理中，请稍候'); return`
—— 📌 与 完全同形：**闸的出口是失败，队列的出口是稍后处理；
一个只有失败出口的机制，最终一定把成本转嫁给用户去手动重试。**
上一次是让 Nano 撞墙，这次是让用户重新打一遍字。

═══ 第二条：崩在处理中途时，要如实说「你可能已经看过」═══
· 不投 → 真的丢了用户的话
· 投但不说 → 模型可能把同一句当成用户说了两遍，重复动手
📌 所以和 `ActionAttempt` 一样：**投，但说清结果可不可信。**

用法：
  py -3.10 tests\cases\t_f1_stage6_inbox.py
"""
from __future__ import annotations

import ast
import contextlib
import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import def_text as S_def_text
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import (
    Command, KernelError, InvariantViolation, reset_kernel_for_tests)
from core.runtime.store import RuntimeStore
from core.runtime import task as _task
from core.runtime import inbox as I
from core.runtime import reconciler as R

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    _task.clear_blocker_providers_for_tests()
    clock = FakeClock(BASE_T)
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / "rt.db"
    return reset_kernel_for_tests(store=RuntimeStore(db), clock=clock), clock, db


def raw(db: pathlib.Path, sql: str) -> None:
    """绕过 Kernel 直接改库 —— 只用来**造非法状态测不变量**。"""
    c = sqlite3.connect(str(db))
    c.execute(sql)
    c.commit()
    c.close()


# ══════════════════════════════════════════════════════════════════════════

def t_never_lost(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐⭐ 核心：一句话都不许丢")
    k, _, _ = make_kernel(tmp / "a")

    check(I.pending_count(k) == 0, "初始队列为空")
    check(I.has_work(k) is False, "初始没有活干")

    a = I.submit_user_message("先帮我装个库")
    b = I.submit_user_message("算了先做别的")
    check(a and b and I.pending_count(k) == 2, "两条都收下了")
    check(I.has_work(k) is True, "有活干")

    # ⚠️ 顺序是语义的一部分：先说 A 再说 B，倒过来处理会得出相反的结论
    items = I.list_pending(k)
    check([x.body for x in items] == ["先帮我装个库", "算了先做别的"],
          "⭐ 按到达顺序（顺序是语义的一部分 —— 倒过来处理结论会相反）",
          str([x.body for x in items]))

    it = I.claim_next("turn_1")
    check(it is not None and it.item_id == a, "认领最早那条", str(it and it.item_id))
    check(it.status == I.ItemStatus.CLAIMED, "状态是 CLAIMED")
    check(it.delivery_count == 1,
          "⭐ 认领即投递计数 +1 —— **必须在「交出去之前」加**，"
          "加在「处理成功之后」永远数不到那次崩溃")
    check(I.pending_count(k) == 1, "队列里还剩一条")

    I.consume(it.item_id)
    check(I.get(k, a).status == I.ItemStatus.CONSUMED, "消费后是终态")
    check(I.get(k, a).closed_at is not None, "终态有 closed_at")
    check(I.current_claimed(k) is None, "没有在处理中的了")

    # 幂等
    I.consume(it.item_id)
    check(I.get(k, a).status == I.ItemStatus.CONSUMED,
          "⚠️ 重复消费是幂等的（不抛异常 —— 重复收尾是正常运行的一部分，"
          "为它抛异常会让 finally 很难写）")


def t_release_not_drop(tmp: pathlib.Path) -> None:
    print("\n[2] ⭐⭐ 处理失败的出口是「回队列」，不是「丢掉」")
    k, _, _ = make_kernel(tmp / "b")

    a = I.submit_user_message("这条会处理失败")
    it = I.claim_next("turn_1")
    I.release(it.item_id, reason="模型调用失败")

    r = I.get(k, a)
    check(r.status == I.ItemStatus.PENDING, "⭐ 退回 PENDING，没有丢")
    check(r.owner_turn_id is None, "⚠️ 同时清掉了 owner_turn_id（不再属于那一轮）")
    check(r.closed_at is None,
          "⚠️⚠️ **也清掉了 closed_at** —— 留着的话这条记录会同时声称"
          "「我还在排队」和「我已经结束了」。📌 一个字段不许表达两个现实")
    check(r.delivery_count == 1, "投递计数**不回退**（它记的是投过几次，不是成功几次）")

    it2 = I.claim_next("turn_2")
    check(it2 is not None and it2.item_id == a, "能被再次认领")
    check(it2.delivery_count == 2, "第二次投递 → 计数 2")


def t_crash_redelivery(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐⭐ 崩在处理中途：投，但如实说「你可能已经看过」")
    k, _, _ = make_kernel(tmp / "c")

    a = I.submit_user_message("把桌面那个文件删了")
    it = I.claim_next("turn_1")
    check(it.maybe_seen_before is False,
          "第一次投递 → 不说「可能看过」（没必要制造疑虑）")
    check(I.describe_for_model(it) == "",
          "⭐ 首次投递前缀是空串 —— **一句用户的话最好的呈现方式就是它本身**")

    # 进程崩了：CLAIMED 还挂着 → 启动收尾
    rep = R.reconcile_on_startup(k)
    check(rep.extra.get("inbox_released") == 1,
          "⭐ 启动收尾退回了 1 条", str(rep.extra))
    check(I.get(k, a).status == I.ItemStatus.PENDING, "退回了队列，没有丢")

    it2 = I.claim_next("turn_2")
    check(it2.delivery_count == 2 and it2.maybe_seen_before is True,
          "重投 → maybe_seen_before")
    note = I.describe_for_model(it2)
    check("may have been shown to you before" in note, "如实告知可能重复")
    check("do NOT act again" in note,
          "⭐⭐ 并且明确「如果已经做过就别再做」—— 与 `ActionAttempt` 同源："
          "**说清结果可不可信，而不是假装没发生或悄悄丢掉**")

    # ⭐ 无条件写 report：0 条也要写
    k2, _, _ = make_kernel(tmp / "c2")
    rep2 = R.reconcile_on_startup(k2)
    check("inbox_released" in rep2.extra and rep2.extra["inbox_released"] == 0,
          "⭐⭐ 一条都没有时**也**往 report 写 0 —— 只在 n>0 时写的话，"
          "「没有遗留」和「压根没跑」长得一样，而那正是"
          "「启动步骤签名写错、静默不跑」的 bug 能藏住的原因")


def t_summary_reports_extra(tmp: pathlib.Path) -> None:
    print("\n[4] 🔴 那行启动摘要原来无视 report.extra（撒谎的日志）")
    k, _, _ = make_kernel(tmp / "d")
    I.submit_user_message("一句话")
    I.claim_next("turn_1")
    rep = R.reconcile_on_startup(k)
    s = rep.summary()
    check("inbox_released=1" in s,
          "⭐⭐ 摘要里报出了扩展步骤做的事。**原来它只认两个内建步骤**，"
          "于是六个靠 `extra` 登记的启动步骤在人类唯一会读的那行里"
          "一个字都不出现，只会看到「无需处理」", s)
    check(s != "无需处理",
          "🔴 而那正是最坏的一种撒谎：**它恰好否定了那个"
          "「注册上去就以为会跑」的东西真的跑了**")

    # ⚠️ 但 0 值不许污染摘要（0 是常态，全列出来会淹掉真信号）
    k2, _, _ = make_kernel(tmp / "d2")
    rep2 = R.reconcile_on_startup(k2)
    check(rep2.summary() == "无需处理",
          "⚠️ 全是 0 时仍然说「无需处理」—— 0 是常态，列出来会淹掉真信号",
          rep2.summary())


def t_discard_only_by_user(tmp: pathlib.Path) -> None:
    print("\n[5] ⚠️ 唯一允许丢弃的路径：用户显式重置对话")
    k, _, _ = make_kernel(tmp / "e")

    I.submit_user_message("一")
    I.submit_user_message("二")
    I.submit_user_message("三")
    n = I.discard_all_pending("用户重置对话")
    check(n == 3 and I.pending_count(k) == 0, "三条都被丢弃", str(n))
    check(all(I.get(k, x.item_id).status == I.ItemStatus.DISCARDED
              for x in I.list_pending(k)) or I.pending_count(k) == 0,
          "⭐ 记成 DISCARDED 而不是删掉 —— "
          "📌 **用户自己决定丢，和系统悄悄丢，是两件事**，账上要能分开")


def t_kinds_not_merged(tmp: pathlib.Path) -> None:
    print("\n[6] ⚠️ 两种 kind 不合并（触发的处理路径不同）")
    k, _, _ = make_kernel(tmp / "f")

    a = I.submit_user_message("用户说的话")
    b = I.submit_wake_intent("susp_1", "timer")
    check(I.get(k, a).kind == I.ItemKind.USER_MESSAGE, "用户消息")
    rb = I.get(k, b)
    check(rb.kind == I.ItemKind.WAKE_INTENT, "唤醒意图")
    check(rb.body == "" and rb.detail == {"suspension_id": "susp_1",
                                          "trigger": "timer"},
          "⭐ 唤醒意图没有 body，靠 detail 带 suspension_id",
          str(rb.detail))
    check(I.ItemKind.USER_MESSAGE != I.ItemKind.WAKE_INTENT,
          "📌 与 `CONSEQUENTIAL` vs `TAKEOVER_TRIGGERS` 同一判据："
          "**答的不是同一个问题，就不合并**")


def t_write_gate(tmp: pathlib.Path) -> None:
    print("\n[7] ⭐ 约束放在唯一的写入口")
    k, _, _ = make_kernel(tmp / "g")

    for body, label in (("", "空串"), ("   ", "全空白"), ("\n\t", "只有空白字符")):
        try:
            k.submit(Command(kind=I.SUBMIT,
                             payload={"kind": "user_message", "body": body}))
            check(False, f"{label}的消息应该被挡住")
        except KernelError:
            check(True, f"⭐ {label}的消息被挡在写入口 —— "
                        f"进去会造出一条空气泡 + 给模型一句空话")

    try:
        k.submit(Command(kind=I.SUBMIT, payload={"kind": "怪东西", "body": "x"}))
        check(False, "非法 kind 应该被挡住")
    except InvariantViolation:
        check(False, "⚠️ 参数非法不该抛 InvariantViolation")
    except KernelError as e:
        check(True, "⭐ 非法 kind 抛 `KernelError` 而**不是** `InvariantViolation` —— "
                    "后者的语义是「任何合法命令序列都不该让它成立」，是代码 bug 的信号。"
                    "📌 拿它报参数错误 = 把调用方的错误误标成数据损坏", str(e)[:40])

    # ⚠️ 唤醒意图允许空 body（它的信息在 detail 里）
    check(I.submit_wake_intent("s1", "manual") is not None,
          "⚠️ 而唤醒意图允许空 body —— 校验是**按 kind** 分的，不是一刀切")

    # 认领空队列
    k2, _, _ = make_kernel(tmp / "g2")
    check(I.claim_next("t") is None, "空队列认领返回 None（不抛）")


def t_invariants(tmp: pathlib.Path) -> None:
    print("\n[8] ⚠️ 三条不变量真的会响（造非法状态验证）")

    k, _, db = make_kernel(tmp / "h")
    I.submit_user_message("x")
    raw(db, "UPDATE inbox_items SET closed_at=1.0")
    check("inbox_terminal_closed" in k.check_invariants_now(),
          "⭐ 非终态却有 closed_at → 响（这一半才是关键，"
          "它兜住 `release` 忘了清 closed_at 的情况）")

    k2, _, db2 = make_kernel(tmp / "i")
    I.submit_user_message("y")
    raw(db2, "UPDATE inbox_items SET status='CONSUMED'")
    check("inbox_terminal_closed" in k2.check_invariants_now(),
          "终态没有 closed_at → 响")

    k3, _, db3 = make_kernel(tmp / "j")
    I.submit_user_message("z")
    raw(db3, "UPDATE inbox_items SET status='CONSUMED', closed_at=1.0, "
             "delivery_count=0")
    check("inbox_consumed_was_delivered" in k3.check_invariants_now(),
          "⭐⭐ 标了 CONSUMED 但从未投递过 → 响。"
          "这是**最坏的一种丢消息**：我们声称把没给模型看过的话处理掉了，"
          "而账上是干净的")

    k4, _, db4 = make_kernel(tmp / "k")
    I.submit_user_message("w")
    raw(db4, "UPDATE inbox_items SET status='怪状态'")
    check("inbox_enums" in k4.check_invariants_now(), "状态枚举越界 → 响")

    # 反向前置：正常状态下三条都不响
    k5, _, _ = make_kernel(tmp / "l")
    a = I.submit_user_message("正常")
    it = I.claim_next("t1")
    I.consume(it.item_id)
    I.submit_user_message("排队中")
    bad = k5.check_invariants_now()
    check(not bad,
          "反向前置：正常流程下三条**都不响**（否则上面只证明了「总是响」）",
          str(bad))


def t_one_claimed_only(tmp: pathlib.Path) -> None:
    print("\n[9] ⚠️ 同一时刻最多一条 CLAIMED（唯一索引兜底）")
    k, _, db = make_kernel(tmp / "m")
    I.submit_user_message("一")
    I.submit_user_message("二")
    a = I.claim_next("t1")
    b = I.claim_next("t2")
    check(a is not None and b is None,
          "⭐ 已有一条在处理时，再认领拿不到（唯一索引挡住了）",
          f"a={a and a.item_id} b={b and b.item_id}")
    check(I.pending_count(k) == 1,
          "⚠️ 而且那条**仍在队列里**，没有因为认领失败而消失")
    # 索引真的存在
    c = sqlite3.connect(str(db))
    idx = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inbox_items'")]
    c.close()
    check("ux_inbox_one_claimed" in idx,
          "⚠️ 唯一索引存在 —— **不是为了并发安全**（asyncio 单线程），"
          "而是让「哪一条正在被处理」有唯一答案，崩溃恢复才知道退回哪一条", str(idx))


def t_targeted_claim(tmp: pathlib.Path) -> None:
    print("\n[11] ⭐⭐ 指定认领：取哪一条做，就把哪一条标成在做")
    k, _, _ = make_kernel(tmp / "n")

    # 场景：上个进程遗留一条（被启动收尾退回队列），排在最前面；
    #       这一次用户又说了一句。UI 侧从自己的内存队列里取的是**后来那条**。
    old = I.submit_user_message("上个进程遗留的")
    new = I.submit_user_message("这一次用户说的")

    got = k.submit(Command(kind=I.CLAIM, payload={"item_id": new})).data
    check(got["item_id"] == new,
          "⭐⭐ 指定 id → 认领的就是**那一条**，不是「最早那条」。"
          "否则跑的是 A、标记消费的是 B，**两条都被记错，而库里看起来完全正常**",
          str(got))
    check(I.get(k, old).status == I.ItemStatus.PENDING,
          "⚠️ 遗留那条**没被动过**，仍在队列里等")

    busy = k.submit(Command(kind=I.CLAIM, payload={})).data
    check(busy["item_id"] is None and busy.get("busy") is True,
          "⭐ 已有一条在处理 → **明确返回 None + busy**，"
          "而不是让唯一索引抛裸 `sqlite3.IntegrityError`。"
          "📌 索引是兜底（纵深防御），显式判断才是主路径", str(busy))

    I.consume(new)
    after = k.submit(Command(kind=I.CLAIM, payload={})).data
    check(after["item_id"] == old,
          "⭐ 腾出位置后，不指定 id 时拿到的是遗留那条（按 rowid 顺序）")

    I.release(old)
    miss = k.submit(Command(kind=I.CLAIM, payload={"item_id": "不存在"})).data
    check(miss["item_id"] is None, "指定一个不存在的 id → None（不抛）")

    # 指定一条已经是终态的 → 也拿不到
    I.consume(old)
    dead = k.submit(Command(kind=I.CLAIM, payload={"item_id": old})).data
    check(dead["item_id"] is None,
          "⚠️ 指定一条已终态的 → None（`AND status=PENDING` 挡住了，"
          "不会把已处理完的又拉回来）")


def t_app_wiring() -> None:
    print("\n[12] app.py 接线（源码不变量）")
    src = module_text("app")
    live = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    check("内核正在处理中，请稍候...', type='warning')" not in live,
          "⭐⭐⭐ 那句丢消息的 `notify + return` **已经不在代码里了**"
          "（注释里留着历史）")
    check("内核正在处理中" in src,
          "⚠️ 但注释里留着原文 —— 📌 删的是「还在运行的东西」，"
          "不是「关于它的记忆」")
    check("_rt_inbox_submit(effective_query" in live,
          "⭐ 每条消息都落库 —— **闲着也落**（`item_id` 是这句话的唯一身份，"
          "而崩溃可能发生在任何时刻）")
    check("_rt_inbox_busy" in live and "self._rt_inbox_parked[" in live,
          "忙时进内存队列，不起 pipeline")
    check("await self._drain_inbox()" in live,
          "轮结束后排空队列")

    # ⚠️ 排空必须在 `async with pipeline_lock` **之外**
    seg = live.split("async def _safe_execute_pipeline")[1].split("async def ")[0]
    lock_i = seg.index("async with self.pipeline_lock")
    drain_i = seg.index("await self._drain_inbox()")
    fin_i = seg.index("finally:")
    check(drain_i > fin_i,
          "⭐⭐ 排空写在 finally **之后**（= 锁已释放）。写在锁作用域里的话，"
          "被排空起的那一轮会立刻撞上还没释放的锁 → 又被判成「忙」→ "
          "又进队列 → **永远没人处理**。"
          "📌 一个「等锁释放后再做」的动作，不能写在还持有锁的作用域里")
    check(seg[drain_i - 400:drain_i].count("async with self.pipeline_lock") == 0,
          "⚠️ 且它前面没有重新进入锁", "")

    check("_ib.discard_all_pending" in live,
          "⭐ 重置对话 = 用户显式丢弃（唯一允许丢的路径）")
    check("payload={\"item_id\": item_id}" in live,
          "⭐ 认领传的是**具体那一条**的 id")
    check("库负责「不丢」" in src,
          "⚠️ 两层分工留了痕：库负责不丢、内存负责接得上")


def t_dual_wait(tmp: pathlib.Path) -> None:
    print("\n[13] ⭐⭐⭐ 用户一句话能解除正在阻塞 turn 的 INLINE 确认")
    import asyncio
    k, _, _ = make_kernel(tmp / "o")

    async def run():
        # ① 用户点了按钮
        ev = asyncio.Event()
        ev.set()
        check(await I.wait_confirm_or_user_message(ev, 1.0)
              == I.ConfirmOutcome.CONFIRMED, "点了按钮 → CONFIRMED")

        # ② ⚠️⚠️ 队列里**早就存在**的消息不许取消一个刚弹出的确认
        I.submit_user_message("这条在弹窗之前就发了")
        check(await I.wait_confirm_or_user_message(asyncio.Event(), 0.3)
              == I.ConfirmOutcome.TIMEOUT,
              "⭐⭐ 陈旧消息**不会**误取消 —— 基线用计数器而不是事件状态。"
              "📌 用「事件」表达「有新东西」时必须定基线，"
              "否则它表达的是「曾经有过东西」")

        # ③ 等待期间来了新消息 → 确定性取消
        ev3 = asyncio.Event()

        async def later():
            await asyncio.sleep(0.05)
            I.submit_user_message("算了别点了")

        t = asyncio.ensure_future(later())
        r = await I.wait_confirm_or_user_message(ev3, 5.0)
        await t
        check(r == I.ConfirmOutcome.USER_MESSAGE,
              "⭐⭐⭐ 等待期间用户说话 → USER_MESSAGE（**不必干等满 300 秒**）。"
              "📌 一句「没丢」的保证，如果它的兑现时机取决于一个正在等它的东西，"
              "那它实际上就是丢了")

        # ④ 谁都没来
        check(await I.wait_confirm_or_user_message(asyncio.Event(), 0.2)
              == I.ConfirmOutcome.TIMEOUT, "干等 → TIMEOUT")

        # ⑤ ⭐⭐⭐ **后台执行体（Subagent）的确认：用户说话【不算】取消。**
        #
        # 🔴 这两条的差别是本项目里一个真实的不对称：
        #      main agent —— 弹窗是**这一轮**的产物，用户下一句话就是对它的回应
        #              → 「改口 = 取消」成立（③ 验的就是它，必须保住）
        #      Subagent —— 弹窗是一个**后台执行体**的产物，
        #              用户下一句话**跟它没有任何关系**
        #    共用同一条判据时会静默地坏：Subagent 在批量改文件，用户随口问一句
        #    「刚才那个 pip 装完没」→ 那次写入被当成「用户取消」丢掉，
        #    而用户的体感是**自己什么都没做，它就失败了**。
        # 📌 **一个「取消」的信号，必须来自它要取消的那件事的同一条注意力。**
        # ⭐ 定死的措辞：**Subagent 只接受这个弹窗自己的回应。**
        _ev5 = asyncio.Event()

        async def _later5():
            await asyncio.sleep(0.05)
            I.submit_user_message("现在几点？")     # ← 一句完全不相干的话

        _t5 = asyncio.ensure_future(_later5())
        _r5 = await I.wait_confirm_or_user_message(
            _ev5, 0.4, cancel_on_user_message=False)
        await _t5
        check(_r5 == I.ConfirmOutcome.TIMEOUT,
              "⭐⭐⭐ **Subagent的确认不被新用户消息取消**（这里走到超时）—— "
              "🔴 若判成 USER_MESSAGE，用户就在毫不知情的情况下否掉了一个"
              "后台动作，而且不会有任何提示",
              str(_r5))

        # ⑥ ⚠️ 但它**照旧认自己那个按钮** —— 不是「关掉了双路等待」，
        #    是「把另一路换掉了」。📌 一个开关如果顺手让主路也失效，
        #    那它关掉的东西比它声称的多。
        _ev6 = asyncio.Event()
        _ev6.set()
        check(await I.wait_confirm_or_user_message(
                  _ev6, 1.0, cancel_on_user_message=False)
              == I.ConfirmOutcome.CONFIRMED,
              "⭐⭐ Subagent点了确认照样 CONFIRMED（超时那道兜底也还在）—— "
              "📌 沉默不等于成功，所以超时必须留着")

        # ⚠️ 三种结果必须互不相同（不许压成布尔）
        check(len({I.ConfirmOutcome.CONFIRMED, I.ConfirmOutcome.USER_MESSAGE,
                   I.ConfirmOutcome.TIMEOUT}) == 3,
              "⚠️ 三种结果互不相同 —— 📌 「用户改口说别做了」和「等了五分钟"
              "没人管」在结果上都是不执行，语义完全不同")

        # ⚠️ 坏掉的 event 要退化成「只等确认」，不能变成新的失败路径
        class _Bad:
            async def wait(self):
                raise RuntimeError("坏了")

        r2 = await I.wait_confirm_or_user_message(_Bad(), 0.2)
        check(r2 in (I.ConfirmOutcome.TIMEOUT, I.ConfirmOutcome.CONFIRMED),
              "⚠️ 异常时退化成一个明确结果 —— 📌 这个机制是**为了少堵用户**，"
              "不该反过来变成一条新的失败路径", str(r2))

    asyncio.run(run())

    note = I.cancelled_by_user_message_note()
    check("was NOT executed" in note, "给模型的说明：明确说没执行")
    check("do not assume it means they approved or rejected" in note,
          "⭐ 并且明确「别自己猜那是同意还是拒绝」—— "
          "📌 用户打字是**换了个话题**，不是对那个弹窗投票")


def t_dual_wait_wiring() -> None:
    print("\n[14] 五个确认等待点都接上了")
    src = module_text("core.orchestrator")
    live = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    # ⭐ 2026-08-26：第 6 处是临时执行通道（`run_scratch_code`）——
    #    它走的是**与 Skill 完全一样**的那条确认路（同一个事件、同一套词表、
    #    同样区分「用户改口 / 干等超时」），所以它出现在这张名单里是**对的**。
    # 📌 这条断言的价值正在这里：**新增一条确认路时它会红**，
    #    逼人回答「你这条为什么不走公共合同」。
    # ⭐ 2026-08-28：第 7 处是 **MCP 接入授权**（`connect_mcp`）。
    #    ⚠️ 名单里原有的那个「MCP」指的是 **MCP 工具调用**的确认，
    #       与这一条（**接入一个新 MCP**）不是同一件事 —— 别看名字像就以为重复了。
    #    ⭐⭐ 而这条断言又一次兑现了它自己写的承诺：新增确认路时它红了，
    #       逼着回答「这一条为什么不走公共合同」——
    #       答案是**它走了**（同一个事件、同一套三态：确认 / 改口 / 超时）。
    #    📌 第一版还差点自己写个 while-sleep 等待，那会漏掉「用户改口」那一态。
    check(live.count("wait_confirm_or_user_message") == 7,
          "⭐ 七处全接（OS 风险确认 / 选择卡 / 缩窗授权 / MCP 调用 / Skill / 临时执行 / MCP 接入）",
          str(live.count("wait_confirm_or_user_message")))
    import re
    for pat, what in ((r"wait_for\(_confirm_ev\.wait\(\)", "OS/MCP/Skill 确认"),
                      (r"wait_for\(_c1_ev\.wait\(\)", "选择卡"),
                      (r"wait_for\(_ev\.wait\(\)", "缩窗授权")):
        check(not re.search(pat, live), f"⚠️ {what} 的旧单路写法已无残留")

    check("cancelled_by_user_message_note" in live,
          "⭐ 取消原因如实告诉模型")
    # ⚠️ 那条恒定的「用户已取消」不许再无条件用
    seg = live.split("execute_after_confirm(resolved_instr, confirmed=False)")[1][:900]
    check("_err6" in seg and "timed out" in seg,
          "⭐⭐ OS 那处不再恒定报「用户已取消」—— "
          "📌 **宁可承认「不知道为什么」，也不许替用户编一个用户没做过的动作**"
          "（超时时没人取消过；用户改口时那是换了话题）")

    ib = module_text("core.runtime.inbox")
    check("不许让代码去理解「算了别点了」" in ib,
          "⚠️ 职责切分留了痕：代码判「有没有新消息」，模型判「那句话什么意思」")
    check("不依赖模型判断的确定性 Cancel" in ib,
          "⭐ 与「不依赖模型判断的确定性 Cancel」那条要求的对应关系写清了")
    check("_note_arrival()" in ib and
          ib.index("_note_arrival()") > ib.index("def submit_user_message"),
          "⚠️ 叫醒动作在**落库成功之后** —— 先保证不丢，再叫醒别人")


def t_wake_intent_wiring() -> None:
    print("\n[15] ⭐⭐ 最后一个还在丢用户意图的地方也接上了")
    import re
    src = module_text("app")
    live = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    check(live.count("内核正在处理中，请稍候") == 0,
          "⭐⭐⭐ `pipeline_lock` 的使用点里**再没有一处**会丢掉用户意图"
          "（发消息 / 手动继续 两条都进队列了）",
          str(live.count("内核正在处理中，请稍候")))
    check("内核正在处理中" in src,
          "⚠️ 但注释里留着原文 —— 📌 删的是「还在运行的东西」，不是「关于它的记忆」")

    check("_rt_inbox_submit_wake" in live,
          "⭐ 手动「继续」被拒时改成落库 + 进队列")
    check('args[0] == "wake"' in live,
          "⭐ 排空认得两种项：用户消息起新 turn / 唤醒意图走 `_drive_wake`。"
          "📌 这正是 `ItemKind` 刻意分两种的原因")

    # ⚠️⚠️ 每一个持锁点后面都必须有排空
    n_lock = len(re.findall(r"async with self\.pipeline_lock", live))
    n_drain = live.count("await self._drain_inbox()")
    check(n_lock == n_drain == 2,
          "⭐⭐ **两个持锁点都跟着排空**。漏一处的后果是：在那条路径跑的时候"
          "进队列的消息会一直排着，直到下一次有别的 turn 结束 —— "
          "没有下一次就是永远。"
          "📌 **一个「锁释放后要做的动作」，必须挂在每一个持有那把锁的地方**",
          f"lock={n_lock} drain={n_drain}")
    for m in re.finditer(r"async with self\.pipeline_lock", live):
        seg = live[m.end():m.end() + 6000]
        check("await self._drain_inbox()" in seg,
              "⚠️ 这个持锁点后面有排空", "")


def t_seam_wiring() -> None:
    print("\n[16] ⭐⭐⭐ [无缝对话] 插话切开回应期；无插话才续接原气泡")
    src = module_text("app")
    live = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    # ⚠️ 构造形式从 dict 字面量换成了 `ViewSession(...)` 关键字实参，
    #    这里两种都认 —— 📌 它守的是**回应期记不记得容器本体**，
    #    不是「用不用 dict 写」。
    #    一条抓的是**语法**而不是**性质**的断言，会在重构时变成假阳性。
    check('"container": loading_container' in live
          or 'container=loading_container' in live,
          "⭐ `_resp_state` 记住容器本体 —— 归属从「本轮」变成「本**回应期**」。"
          "📌 一个叫「本轮」的状态，在语义变成「一段可含多轮」之后必须重新划归属")
    check("_live_box.move(self.chat_container)" not in live,
          "⭐⭐⭐ 插话时**不移动旧 Nano 气泡** —— 插话前已经发生的工具事实必须留在用户新消息上方")
    check("_handoff_response_epoch" in live,
          "⭐⭐ 忙时不是拼接 DOM，而是显式交接 predecessor/successor 回应期")
    check('args[0] == "cont"' in live and "_safe_execute_pipeline(_q, _box" in live,
          "⭐ 排空时喂进**已经存在的**那个气泡，不新建")

    # ⚠️ 用量不许在续接时被清零（token 计数器统计整段）
    check("if not _seam_cont:" in live and
          live.count("usage_tracker.reset_session()") == 1,
          "⭐⭐ 续接时**不重置用量** —— token 计数器只有一个、在末尾，"
          "那意味着它统计的是**整段回应期**",
          str(live.count("usage_tracker.reset_session()")))

    # ⚠️⚠️ 最要紧的一条：收尾条件
    #
    # 🔴 这两条原来是文本匹配 `"_seam_more = bool" in live` + `live.split(...)[1][:1200]`，
    #    而代码里实际写的是 `_seam_more = (bool(...) or _waiting_for_carrier)` ——
    #    **多了一个括号就匹配不上**，于是 `split()[1]` 直接 IndexError，
    #    整个测试文件挂掉（rc=1）而不是给出一条红。
    # 📌 **一条断言如果匹配的是「表达式怎么排版」，那它验的是格式，不是行为。**
    # 📌 而**用 `split()[1]` 取范围的断言，锚点一变就是崩，不是失败** ——
    #    崩掉比失败更糟：它会掩盖掉这个文件里后面所有本来会跑的断言。
    # ⭐ 改成按 AST 认「那个分支」，同时把断言加强成
    #    「三件事在 else 支里、且**不在** if 支里」。
    _mod = ast.parse(module_text("app"))
    _np = next((n for n in ast.walk(_mod)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "navigate_pipeline"), None)
    _sm_if = None
    for _n in ast.walk(_np) if _np is not None else ():
        if (isinstance(_n, ast.If) and isinstance(_n.test, ast.Name)
                and _n.test.id == "_seam_more"):
            _sm_if = _n
            break
    check(_sm_if is not None,
          "⭐⭐⭐ 收尾条件从「这一轮结束」变成「这一轮结束 **且** 队列空」"
          "（`if _seam_more:` 这个分支存在）。"
          "📌 一个「结束时才做的事」，在「一段可以包含多轮」之后，"
          "判据必须从「这一轮完了吗」换成「整段完了吗」")
    _assigned = {t2.id for _n in (ast.walk(_np) if _np is not None else ())
                 if isinstance(_n, ast.Assign)
                 for t2 in _n.targets if isinstance(t2, ast.Name)}
    check("_seam_more" in _assigned, "`_seam_more` 在本函数里被赋值")
    if _sm_if is not None:
        _body = "\n".join(ast.unparse(s) for s in _sm_if.body)
        _orelse = "\n".join(ast.unparse(s) for s in _sm_if.orelse)
        check("turn_tokens_fmt" in _orelse
              and "set_visibility(False)" in _orelse
              and "display:inline-block" in _orelse,
              "⚠️ 藏转圈 / 显 ✦ / 写 token 三件事**都**在「队列空」那一支里 —— "
              "漏一件就会出现「转圈停了但没有统计」这种半截状态")
        check("turn_tokens_fmt" not in _body
              and "set_visibility(False)" not in _body,
              "⭐⭐ 而「队列里还有」那一支**一件都不做** —— "
              "📌 这条比上面那条更要紧：上面验的是「该做的做了」，"
              "这条验的是「不该做的没做」，"
              "而假收尾正是从「不该做的做了」来的")

    # ⚠️⚠️ 续接是「复用」还是「新开」——**由那个元素现在有没有内容决定**
    check("_prev_has_text = bool" in live,
          "⭐⭐⭐ 判据是**看现在的状态**（上一段有没有内容），"
          "不是追踪「从哪条路来的」。"
          "📌 别追踪「我是从哪来的」，直接问「现在是什么样」")
    # ⚠️ [2026-08-22] 模型正文统一走 `nano_md()`（它给正文打**可回复**标记，
    #    见 `app.nano_md` 的说明）—— 断言跟着**语义**走：
    #    「新建了一个正文元素」，不是「用哪个构造函数建的」。
    #    📌 一条钉住实现细节的断言，会被一次正确的重构打红。
    check('_rs["content_md"] = nano_md(' in live,
          "⭐ 上一段**有内容**时新开一个元素 —— `final_result` 是整体覆盖，"
          "复用会把上一段答案擦掉")
    check('_rs["content_md"].set_content("")' in live,
          "⭐⭐ 上一段**是空的**（被撤回）时**复用它**。"
          "🔴 原来无条件新开，造出了实测那个「`nano ❯` 与文字对不齐」——"
          "被擦空的元素带着 `min-height:1em`，**空着也占一行**，"
          "新段落落在它下面就低了一行。📌 **擦掉内容 ≠ 移除元素**")
    check("擦掉内容 ≠ 移除元素" in src,
          "⚠️ 那个 bug 的判据留了痕")
    check("执行分段」时代的产物" in src,
          "⚠️ 并写清了「无条件新开」本身是被推翻的那个设计的遗留")

    # ⚠️ 兜底：找不到活着的回应期时退化成旧行为，不是不处理
    check("找不到活着的回应期" in src or "退回「排队」那套" in src,
          "⚠️ 兜底留痕：📌 fail-safe 方向是**退化成旧行为，不退化成不处理** —— "
          "别让「无缝」变成一条新的失败路径")
    for dead in ("_last_reply_inner_col", "_last_reply_status_lbl",
                 "_last_reply_start_time", "_last_reply_end_time",
                 "_last_reply_tok_str", '"msg_start_time"', '"tok_str"'):
        check(dead not in live,
              f"⭐ 旧‘上一条回复’异步 DOM 快照已退役：{dead}")


def t_seam_epoch_handoff() -> None:
    """用真 NiceGUI 顺序 + 真实 helper 验证回应期切片。"""
    print("\n[17] ⭐⭐⭐ [无缝] predecessor 留在原位，future output 归 successor")
    from nicegui import ui
    from app import WebUI

    box = ui.column()
    with box:
        old_user = ui.label("老用户")
        old_nano = ui.column()
        with old_nano:
            old_meta = ui.label("old thinking")
        new_user = ui.label("新用户")
        new_nano = ui.label("新nano")

    def order():
        names = {id(old_user): "老用户", id(old_nano): "老nano",
                 id(new_user): "新用户", id(new_nano): "新nano"}
        return [names[id(c)] for c in box.default_slot.children]

    check(order() == ["老用户", "老nano", "新用户", "新nano"],
          "前置：两段回应已经按真实时间顺序创建", str(order()))

    old = {"container": old_nano, "meta_row": old_meta, "running": True}
    new = {"container": new_nano, "meta_row": None, "running": True}
    gui = object.__new__(WebUI)
    gui._ui_scope = contextlib.nullcontext
    gui.chat_container = box
    gui._resp_state = new
    gui._waiting_pills = {
        "live": {"done": False, "resp_state": old},
        "done": {"done": True, "resp_state": old},
        "other": {"done": False, "resp_state": {}},
    }
    gui._handoff_response_epoch(old, new)

    check(order() == ["老用户", "老nano", "新用户", "新nano"],
          "⭐⭐⭐ 交接后 DOM 时间线不动：旧事实 / 新插话 / 新回应", str(order()))
    check(gui._resp_state is new,
          "⭐⭐ 当前回应权威保持指向 successor，不再还原成 predecessor")
    check(gui._waiting_pills["live"]["resp_state"] is new,
          "⭐⭐ 活等待只迁移**未来输出归属**，完成后续接新回应")
    check(gui._waiting_pills["done"]["resp_state"] is old,
          "⚠️ 已结束等待不迁移，历史归属不被篡改")
    check(gui._waiting_pills["other"]["resp_state"] is not new,
          "⚠️ 不属于 predecessor 的等待不被顺手搬走")
    check(old_meta.is_deleted,
          "⭐⭐ predecessor 的活动元信息行被收掉，不留下永久转圈")

    # successor 尚未开始输出时又来一条更新：空占位要折叠，不能留下空气泡。
    with box:
        third_user = ui.label("第三条用户")
        latest_nano = ui.label("最终nano")
    new["pending_epoch"] = True
    latest = {"container": latest_nano, "meta_row": None,
              "running": True, "pending_epoch": True}
    gui._handoff_response_epoch(new, latest)
    names2 = {id(old_user): "老用户", id(old_nano): "老nano",
              id(new_user): "新用户", id(third_user): "第三条用户",
              id(latest_nano): "最终nano"}
    order2 = [names2[id(c)] for c in box.default_slot.children]
    check(order2 == ["老用户", "老nano", "新用户", "第三条用户", "最终nano"],
          "⭐⭐ 连续插话折叠尚未启动的空 successor，只保留一个最终 Nano 气泡",
          str(order2))


def t_seam_cmd47_fix() -> None:
    print("\n[18] 🔴 插话交接的源码不变量")
    src = module_text("app")
    live = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    tree = ast.parse(src)
    start = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "start_pipeline_task")
    busy = next(n for n in ast.walk(start)
                if isinstance(n, ast.If) and ast.unparse(n.test) == "_rt_inbox_busy")
    busy_body = ast.unparse(busy)

    check("_seam_live = getattr(self, \"_resp_state\", None) if _rt_inbox_busy" in live,
          "⭐⭐⭐ 在**忙判断那一行**就把活着的回应期快照下来。"
          "📌 一个「要不要做 X」的判断，和「不做 X」的那个分支之间，"
          "不许有任何会改变 X 前提的代码")
    check("self._handoff_response_epoch(_rs_live, self._resp_state)" in busy_body,
          "⭐⭐ 忙分支把快照与新 state 显式交给回应期交接函数")
    check("self._resp_state = _rs_live" not in busy_body,
          "⭐⭐ 不再把全局当前回应还原成 predecessor")
    check(".move(" not in busy_body and
          "self.chat_container.remove(loading_container)" not in busy_body,
          "⭐⭐ 不再删除新用户消息下方的 successor Nano 气泡")


def t_seam_note() -> None:
    print("\n[19] ⭐⭐ [无缝 · 措辞] 注入的是「事实」，不是「表演指令」")
    orc = module_text("core.orchestrator")
    app = module_text("app")
    live = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))

    # ⚠️⚠️ **这几条 2026-08-08 改过**：第一版验的是「[Same bubble] …你上一段回答
    #    和这一段会显示在同一个气泡里」。改成**协作式中断**之后**上一段被撤回了**，
    #    那句话变成了**假话** → 文案换成 `[Interjected]`。
    #    📌 **一个描述「当前呈现方式」的注入，在呈现方式变了之后必须一起改，
    #       否则它就是在给模型讲一个不存在的现实。**
    check("[Interjected]" in live, "⭐ 被插话的那一轮会收到说明")
    check("your earlier attempt was" in live and "discarded" in live,
          "⭐⭐ 说的是**真事**：上一次尝试**被丢弃了、什么都没执行、什么都没显示** ——"
          "而不是「你上一段回答还在气泡里」那句已经不成立的话")
    check("nothing was executed" in live,
          "⭐ 明确「什么都没执行」—— 这正是检查点放在 `add_tool_calls` **之前**"
          "才敢说的话（工具压根没跑，不是「可能部分跑了」）")
    check("the LATER ones are more recent" in live,
          "⭐ 兑现了「标记第一条/第二条」这个要求 —— 后面那条更新，"
          "可能**撤回或修改**前面那条")
    check("ONE single answer" in live and "do NOT contradict yourself" in live,
          "⭐⭐ 给的是**可验证的具体约束**（只给一个回答、别自相矛盾），"
          "而不是「自然一点」那种没法判定的要求。"
          "⚠️ 「别自相矛盾」直指实测撞到的那个反例")
    check("[Still running]" in live,
          "⭐⭐ 还有哪些东西在跑**也告诉它** —— 否则用户说「算了这个不做了」时，"
          "模型**不知道有什么可取消**。"
          "📌 代码判「有没有新消息」（确定性），模型判「要不要取消」（理解意图）——"
          "而这条注入是那个分工的**前提**")
    check("the interruption did not" in live,
          "⚠️ 并且明说**中断没有替它取消** —— "
          "📌 中断的作用域是「这一轮的决策与输出」，不是「这一轮启动过的一切」")

    # ⚠️⚠️ 不许出现表演指令。**只扫注入块本身** ——
    #    扫整个文件会撞到别处提示词里的同名词（第一版就这么误报了）。
    #    📌 一条源码断言的范围必须窄到只包含它要管的那段，
    #       否则它报的是"这个文件里有这个词"，不是"我关心的那段有这个词"。
    _blk = live[live.index("[Interjected]"):]
    _blk = _blk[:_blk.index("_active_susp")]
    for bad in ("pretend", "act as if", "假装", "make it look"):
        check(bad.lower() not in _blk.lower(),
              f"⚠️ 注入块里不含表演指令「{bad}」—— 📌 **给状态，不给剧本**："
              f"演出脚本会让它批量生产过渡词，还可能**编造连续性**"
              f"（声称做过没做的事）")

    check("if _seam_part >= 2:" in live,
          "⭐ **只在第 2 段起注入** —— 首段一个字都不加（那时没有「上一段」）")
    check("if _seam_part > 2:" in live,
          "⚠️ 段号只在第 3 段起才报（两段时说「part 2」是噪音）")
    check("self._seam_continuation_part = 0" in live,
          "⚠️ 即读即清 —— 它只对**这一次**调用有效")

    # ⚠️ 绝不拼进用户原话
    check("_seam_part" not in live.split("handle_query")[0][-3000:] or
          "base_guide +=" in live,
          "⭐⭐ 走 `base_guide` 动态段通道，**不拼进用户原话**。"
          "📌 用户的原话必须保持原样（同 `answer_verbatim` 的纪律）——"
          "拼进去模型就分不清哪句是用户说的、哪句是系统说的")

    alive = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))
    check("self._seam_part = 1" in alive,
          "⭐ 首段把段计数归 1")
    check('self.agent._seam_continuation_part = self._seam_part' in alive,
          "⭐ 续接时把段号交给 orchestrator")


def t_source_invariants() -> None:
    print("\n[10] 源码不变量")
    src = module_text("core.runtime.inbox")
    rec = module_text("core.runtime.reconciler")
    ker = module_text("core.runtime.kernel")
    sto = module_text("core.runtime.store")

    check("_inbox.install(k)" in ker, "已接到 Kernel 上")
    check(ker.count("_inbox.install(k)") == 2,
          "两条建 kernel 的路径都装了（生产 + 测试）", str(ker.count("_inbox.install(k)")))
    # ⚠️⚠️ **不写死当前版本号。** 第一版写的是 `_SCHEMA_VERSION = 7`，
    #    下一次加表/加列（v8）就把这条无辜地撞红了 ——
    #    而它想保的性质根本不是「现在是第 7 版」。
    #    📌 **一条断言要保的是「那件事发生过」，不是「此刻的计数器等于某个值」** ——
    #       后者会被任何无关的推进撞红，于是训练出「改一下数字就行」的习惯，
    #       而那正好废掉了断言。
    import re as _re
    _m = _re.search(r"_SCHEMA_VERSION = (\d+)", sto)
    check(_m and int(_m.group(1)) >= 7,
          "⭐ schema 版本 ≥ v7（inbox 那一版之后就不许回退）",
          _m.group(1) if _m else "?")
    check("v7: 加 inbox_items" in sto,
          "⭐ v7 那条版本注释**留着** —— 📌 版本注释是一份只增不减的账，"
          "它回答「哪一版加了什么」，而那正是迁移出问题时唯一能查的东西")

    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("delivery_count=delivery_count+1" in body,
          "⭐ 投递计数在 claim（交出去之前）加")
    check("closed_at=NULL" in body,
          "⭐ release 明确清 closed_at")
    check("闸的出口是失败，队列的出口是稍后处理" in src,
          "⚠️ 判据留痕：与 [A3] 同形（上一次是让 Nano 撞墙，这次是让用户重打一遍字）")
    # ⚠️ 锚点原来钉的是**外部文档里的一句原话**（"并不要求 x 和 y 一定相关"）。
    #    那句话删掉之后断言就红了 —— 而它守的那条约束本身**一个字没少**。
    #    📌 锚点要挑「因果上必须在那儿」的东西：约束的**内容**必须在，
    #       它当初被谁用什么措辞说出来的，不是。
    check("不要求相关" in src and "对当前任务的修正" in src,
          "⚠️ 那条硬约束留了痕（inbox 里的东西"
          "不许被假设成「对当前任务的修正」）")
    check("可重放性取决于" in src,
          "⚠️ 写清了为什么这里能退回重投、而 ActionAttempt 必须记成 INTERRUPTED")

    check("for k in sorted(self.extra)" in rec,
          "⭐ 启动摘要现在会报 extra")
    check("扩展点在自己被迫填写的报告里是二等公民" in rec,
          "⚠️ 那条撒谎日志的判据留了痕")


def t_stop_button() -> None:
    print("\n[20] ⭐⭐⭐ [B1] 终止按钮 —— 它**不是**无缝对话的一部分")
    orc = module_text("core.orchestrator")
    app = module_text("app")
    olive = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))
    alive = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))

    # ── 意图的生命周期 ────────────────────────────────────────────────────
    from core.orchestrator import Orchestrator

    class _Bare(Orchestrator):
        def __init__(self):
            pass

    o = _Bare()
    check(o._stop_asked() is False, "初始没有终止意图")
    o.request_stop("test")
    check(o._stop_asked() is True, "请求后置上了")
    o.clear_stop()
    check(o._stop_asked() is False,
          "⭐⭐ 新一轮开始时**必须清掉** —— 不清的后果是"
          "「上一次的终止把下一轮也杀掉」，表现为**Nano 再也不回话了**。"
          "📌 一个「一次性的意图」必须有明确的清除点，"
          "否则它会从「这一次」悄悄变成「从此以后」")
    check("self.clear_stop()" in olive,
          "⚠️ 而且清除点真的接在 `handle_query` 里（不是只有个方法没人调）")

    # ── 三个检查点都认终止 ────────────────────────────────────────────────
    check(olive.count("self._stop_asked()") >= 4,
          "⭐ 三个检查点（模型生成中 / 轮开头 / 工具执行前）都认终止",
          str(olive.count("self._stop_asked()")))
    check("模型生成中" in orc,
          "⭐⭐ **stream 进行中也检查** —— 否则模型正在写一段长回答时点终止"
          "要等它写完，那不叫终止")
    check("stream 阶段一个字都不写进历史" in orc,
          "⚠️ 并留痕了它为什么安全：**stream 阶段不写历史** → "
          "📌 中断点选在「还没写进历史」的位置 → 不需要补偿逻辑")

    # ── 两种原因必须分开 ──────────────────────────────────────────────────
    check('"user_stopped" if stopped else "user_interjected"' in olive,
          "⭐⭐⭐ **两种原因分开**，不许压成一个「被中断了」。"
          "📌 **出口方向相反的两件事，不能共用一个 reason** —— "
          "插话要「接着跑」，终止要「收尾」")
    check('if step.get("stopped"):' in alive,
          "⭐ app 侧据此走两条不同的收尾路径")

    # ── 收尾差异 ──────────────────────────────────────────────────────────
    # ⚠️⚠️ **切片范围由代码结构定，不由字符数定。**
    #    原来这里是 `[:2200]` —— 2026-08-09 给终止分支加了一段注释之后，
    #    要找的那句话被挤到 2200 之外，测试红了一格。
    # 🔴 这是同一个问题的**第二种表现**：上一次是窗口**太宽**（2500 字符跨进了下一个分支，
    #    断言被隔壁的代码骗过），这次是**太窄**。两次的根因是同一个：
    # 📌 **用字符数划定断言范围，等于赌那段代码以后不会变长也不会变短** ——
    #    而注释一加就变。范围必须由**结构**划定（AST，或一个明确的结束标记）。
    # ⚠️ 而**结束标记不能用注释** —— `alive` 是刻意滤掉注释行的
    #    （文本匹配会被注释和 docstring 打中）。第一版拿注释当右边界，
    #    在 `alive` 里它压根不存在，于是前置断言当场红了。
    #    📌 **一个「防止被注释骗到」的过滤器，也会让注释没法当锚点** ——
    #       同一个设计的两面，用它的时候得记住另一面。
    # ⭐ 所以用 **AST**：直接取那个 `if` 的 body，边界由语法树给。
    import ast as _ast
    _seg_src = None
    for _n in _ast.walk(_ast.parse(app)):
        if isinstance(_n, _ast.If) and "stopped" in _ast.unparse(_n.test) \
                and "step" in _ast.unparse(_n.test):
            _seg_src = "\n".join(_ast.unparse(s) for s in _n.body)
            break
    check(_seg_src is not None and len(_seg_src) > 500,
          "前置：AST 真的取到了终止分支的 body（边界由语法树定，不由字符数定）",
          f"{len(_seg_src or '')} 字符")
    seg = _seg_src or ""
    check("已终止" in seg and "spin_lbl" in seg and "_el" in seg,
          "⭐ 终止**要收尾**（写统计、停转圈、显示「已终止」）—— "
          "因为**没有下一段**；而插话时刻意不收尾")
    _stop_ev_src = S_def_text("core.orchestrator", "_interject_stop_event", owner="Orchestrator")
    check("[System record: the user pressed Stop" in _stop_ev_src
          and "[System record: the user pressed Stop" not in seg,
          "⭐⭐ **终止这个事实传给了模型**——由后端在真正停下时写进历史"
          "（界面点击即收尾，那时后端可能还没停下，所以不由界面写）")
    check("running unless I cancel" in _stop_ev_src,
          "⭐ 并且明说**已经启动的后台工作还在跑，除非它自己去取消** —— "
          "📌 中断的作用域是「这一轮的决策与输出」，不是「这一轮启动过的一切」")
    check("不清队列" in app,
          "⚠️ 终止**不清 inbox 队列** —— "
          "📌 「停止当前这一轮」和「丢掉我说过的话」是两件事")

    # ── 按钮的双身份 ──────────────────────────────────────────────────────
    check("_on_send_or_stop" in alive, "⭐ 按钮走同一个入口，身份现场判定")
    check("icon=stop" in alive and "icon=arrow_upward" in alive,
          "⭐ 两个图标都在（空+在跑=终止 / 否则=发送）")
    check("ui.timer(0.4, self._refresh_send_btn)" in alive,
          "⭐⭐ **level-triggered 刷新** —— 不靠「输入时记得改图标」那种配对写法。"
          "📌 本轮反复栽的都是「靠所有调用点都记得同步」的东西，"
          "漏一处就永久错位")

    # ⚠️ 决定「做什么」时不许看渲染状态
    # ⚠️⚠️ **用 AST 取「去掉 docstring 的函数体」** ——
    #    第一版直接切字符串，结果被**docstring 骗了**：那段 docstring 里
    #    正好**提到**了 `_send_btn_is_stop`（用来解释为什么不看它）。
    #    📌 又一次「断言的范围包含了不该包含的东西」——
    #       上一轮刚在「不许有表演指令」那条上栽过同一个形状。
    #       **一条源码断言的范围必须窄到只包含它要管的那段。**
    import ast as _ast
    _tree = _ast.parse(app)
    _body_src = ""
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.FunctionDef) and _n.name == "_on_send_or_stop":
            _stmts = _n.body[1:] if (_n.body and isinstance(_n.body[0], _ast.Expr)
                                     and isinstance(_n.body[0].value, _ast.Constant)
                                     and isinstance(_n.body[0].value.value, str)
                                     ) else _n.body
            _body_src = "\n".join(_ast.get_source_segment(app, s) or ""
                                  for s in _stmts)
    check(bool(_body_src), "前置：AST 取到了函数体")
    seg2 = _body_src
    check("_send_btn_is_stop" not in seg2,
          "⭐⭐ 判定时**不看 `_send_btn_is_stop`**（那是渲染状态，有 0.4s 滞后）。"
          "📌 用「界面现在长什么样」决定「该做什么」= 让 UI 反过来当权威，"
          "而判据是**UI 必须是权威状态的忠实投影，不是权威本身**")
    check("self._turn_running()" in seg2,
          "⚠️ 而是现场问「有没有一轮在跑」（`pipeline_lock` 是那个事实的权威）")

    # ── ⭐⭐⭐ 终止**不许**碰后台任务和Subagent（2026-08-08 特意强调）────────
    # 📌 **一条被口头强调的约束，最好的归宿是一条断言** ——
    #    否则它只活在那次对话里，下一个改这段代码的人看不到。
    # ⚠️⚠️ **取范围用「起点 + 明确的终点标记」，不用「起点 + 固定字符数」。**
    #    固定字符数第一版取 2500 字符，**越过了终止分支**、撞到后面
    #    `final_result` 里的 `_timer_task.cancel()` → 误报。
    #    📌 这是断言范围问题在三轮内的**第三次**
    #       （扫整个文件 → 被 docstring 骗 → 窗口越界）。
    #       **一条源码断言的范围必须由代码结构决定，不由字符数决定** ——
    #       字符数是个**与语义无关的代理物**，而本轮反复栽的正是这个形状。
    # ⚠️ 在**原文**上定界（终点标记是条注释，`alive` 已经把注释剥掉了），
    #    定完界再剥注释 —— 两步不能倒。
    _raw = app.split('if step.get("stopped"):')[1]
    _raw = _raw[:_raw.index("插话：不收尾元信息行")]
    _stopseg = "\n".join(l for l in _raw.splitlines()
                         if not l.strip().startswith("#"))
    for _bad, _why in ((".cancel()", "取消协程"),
                       ("_bg_tasks.clear()", "清空后台任务表"),
                       ("_bg_tasks.pop", "摘掉后台任务")):
        check(_bad not in _stopseg,
              f"⭐⭐ 终止路径里没有「{_why}」这种动作 —— "
              f"📌 **中断的作用域是「这一轮的决策与输出」，"
              f"不是「这一轮启动过的一切」。**"
              f"后台任务和Subagent的执行体是它们自己，不随终止死",
              _bad)
    check("_run_bg_task" not in _stopseg and "notify_background_done" not in _stopseg,
          "⚠️ 也没有去动后台任务的完成通路")
    # ⭐ 而且必须**明确告诉模型**它们还活着（否则它以为都停了）
    check("still" in _stop_ev_src and "running unless I cancel" in _stop_ev_src,
          "⭐⭐ 并且**明说它们还在跑、除非它自己去取消** —— "
          "⚠️ 只是「没杀掉」不够：模型若以为都停了，就不会去取消该取消的。"
          "📌 状态变化必须让需要知道的人知道")

    # ── 那条被推翻的规定的留痕 ────────────────────────────────────────────
    # ⚠️ 锚点钉的是**那条被推翻的规定说了什么**，不是它的编号 ——
    #    编号随清理消失，而「说了什么」是这段留痕存在的全部理由。
    check("终止按钮不单独修" in orc and "那条是错的" in orc,
          "🔴 早先那条「终止按钮不单独修、等后台任务做完自然消失」**被推翻并留痕**")
    check("一个「停止」能力如果依赖被停止的那一方理解你的意思" in orc,
          "⭐⭐ 决定性论据留了痕：**它就不是停止能力**")


def t_wake_never_silently_dropped() -> None:
    """⭐⭐⭐ [2026-08-09 实测] 唤醒在内核忙时**不许被静默丢掉**。

    🔴 **实测现象**：让 Nano 用 playwright 打开一个网页 → 超 8 秒自动转后台 →
       网页 0.4 秒后就打开了，但 Nano **永远停在「等着看结果」**。
       pill 甚至显示绿色的「▶ 后台完成，继续」，而那一轮再也没有下文。

    ⚠️⚠️ **根因是一句兑现不了的注释。** `_drive_wake` 在内核忙时静默 `return`，
       注释写着「稍后由 poller 再尝试（记录仍 active）」——
       而 poller（`_suspension_poll_tick`）走的是 `due_timers()`，SQL 条件是
       `fire_at IS NOT NULL AND fire_at <= now`。
       **后台等待的 `fire_at` 是 `None`，它永远不会被轮到。**

    ⚠️ 而 MCP 自动后台化**必然**撞上：调用是在**一轮进行中**被转后台的，
       完成时那一轮还握着锁。所以这不是偶发竞态，是**每次都会发生**。

    📌 **一句「稍后由 X 再试」的注释，必须能指出 X 真的会再试。**
    📌 而更贵的一条：此前把这一处判成「本来就是对的」，**依据正是那句注释本身**。
       **判断一处「本来就是对的」，不能只读它的注释说它交给了谁，
       要去看那个「谁」是不是真的会接。**

    ⭐ 修法用的是**已经在代码里的那条正确做法**（`_wake_now` 内核忙时把唤醒意图
       落进 durable inbox）——
       📌 **一个正确做法已经在代码里存在、却没被推广到同类场景，
          缺的不是想法，是一致性。**
       ⚠️ 讽刺的是这条判据当初就是从 `_wake_now` 那一处立的，
          而它当时把 `_drive_wake` 当成了**正面例子**。
    """
    print("\n[11] ⭐⭐⭐ 唤醒在内核忙时进队列，不许静默丢掉")
    app = module_text("app")
    tree = ast.parse(app)

    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_drive_wake_inner":
            fn = n
    check(fn is not None, "前置：找到 `_drive_wake`")
    body = ast.unparse(fn) if fn else ""

    # ① 忙的时候必须落队列，不许只是 return
    # ⚠️ 2026-08-09 收成门面  之后，这两条改成断言"两条早退路径共用它"
    check("_park_wake" in body,
          "⭐⭐⭐ 内核忙时**把唤醒意图落进 durable inbox** —— "
          "🔴 这一格红了就回到了那个「后台完成了但 Nano 永远不说话」的老问题")
    pw = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_park_wake":
            pw = n
    pwb = ast.unparse(pw) if pw else ""
    check(pw is not None, "前置：找到门面 `_park_wake`")

    check(body.count("_park_wake") >= 2,
          "⭐⭐⭐ **两条早退路径（内核忙 / 预算硬上限）共用同一个出口** —— "
          "🔴 cmd53 我只修了「忙」那条，「预算」那条原样留着静默 return，"
          "对 background 又是同一个静默丢失（只是触发原因换了）。"
          "📌 **同一种失败要走同一个出口，否则「补齐所有出口」这件事永远做不完**",
          f"{body.count('_park_wake')} 处")
    check("_rt_inbox_submit_wake" in pwb and "_rt_inbox_parked" in pwb,
          "⭐ 门面里真的落进 durable inbox + 排空表")
    check("_v[1] == suspension_id" in pwb.replace(chr(39), chr(34)).replace(chr(34)+chr(34),chr(34)) or "suspension_id" in pwb,
          "⭐⭐ 门面**对同一条挂起去重** —— 预算硬上限不是瞬时状态，可能连续多轮都满，"
          "每次重排都写一条 durable inbox 行就是泄漏。"
          "📌 一个「稍后再试」的队列必须对同一件事去重，否则「稍后」的次数会变成行数")

    # ② note 必须一起排进去
    # ⚠️ 这条的目标 2026-08-09 从 `_drive_wake` 移到了门面 `_park_wake` ——
    #    📌 **一条按位置定位的断言，在代码被抽成函数之后要跟着搬**，
    #       否则它会红在一个其实更好的实现上。
    check('"wake", suspension_id, trigger, note' in pwb.replace("'", '"'),
          "⭐⭐ 排队项带上了 `note`（后台结果）—— "
          "📌 **一个「稍后再处理」的队列，必须把「处理它需要的东西」一起排进去**，"
          "只排一个 id 等于把上下文丢在原地")

    # ③ 连队列都进不去时要响亮
    check("logger.error" in body,
          "⭐ 连队列都进不去时**响亮报错** —— 这条路径丢掉的是"
          "「后台任务已经完成」这个事实，而它不会有第二次机会")

    # ④ 排空侧要认 4 元组，且仍兼容 3 元组
    dr = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_drain_inbox":
            dr = n
    dbody = ast.unparse(dr) if dr else ""
    check("len(args) > 3" in dbody,
          "⭐⭐ 排空侧认第四项 `note`，且用 `len(args)` 做兼容 —— "
          "📌 队列里可能同时存在旧格式的项（历史入队 / 手动继续那条路），"
          "所以解包必须容错，不能硬拆")

    # ⑤ 那个「提前定型」不许再出现在 notify_background_done 里
    nb = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "notify_background_done":
            nb = n
    nbody = ast.unparse(nb) if nb else ""
    check("_settle_waiting_pill" not in nbody,
          "⭐⭐⭐ `notify_background_done` **不再自己定型 pill** —— "
          "`_drive_wake` 内部早就把定型挪到了「拿到锁之后」，"
          "但那次改动只改了它自己那条路径。"
          "📌 **一个「等拿到锁再定型」的修法，如果只改了其中一条调用路径，"
          "另一条路径上的 UI 仍然在说谎** —— 而它说的还是「完成了，继续」，"
          "比什么都不说更糟")

    # ⑥ 反向：poller 只轮定时这件事本身要留痕（免得有人以为它什么都轮）
    check("due_timers" in app,
          "⭐ poller 走的是 `due_timers()` —— 它**只轮定时**，"
          "这就是为什么 background 不能指望它")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_never_lost(tmp)
        t_release_not_drop(tmp)
        t_crash_redelivery(tmp)
        t_summary_reports_extra(tmp)
        t_discard_only_by_user(tmp)
        t_kinds_not_merged(tmp)
        t_write_gate(tmp)
        t_invariants(tmp)
        t_one_claimed_only(tmp)
        t_targeted_claim(tmp)
        t_app_wiring()
        t_dual_wait(tmp)
        t_dual_wait_wiring()
        t_wake_intent_wiring()
        t_seam_wiring()
        t_seam_epoch_handoff()
        t_seam_cmd47_fix()
        t_seam_note()
        t_stop_button()
        t_source_invariants()
        t_wake_never_silently_dropped()
        t_l14_present_not_execute(tmp)

    ok = sum(1 for r in _results if r[0])
    print("\n" + "=" * 74)
    print(f"结果：{ok}/{len(_results)} 通过" +
          ("" if ok == len(_results) else " —— 失败项："))
    for good, name, note in _results:
        if not good:
            print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


def t_l14_present_not_execute(tmp: pathlib.Path) -> None:
    """关软件时排队中的消息：**呈现，不执行**。

        inbox 的立意：用户的话永不丢
        关软件那条：关闭软件 = 用户默认放弃这次协同（一律 TERMINAL）
               ├─ 重新【呈现】→ 两条都满足 ✅
               └─ 自动【执行】→ 违反后一条 ❌
    📌 **「不丢」和「替用户做决定」是两件事。**
    """
    import ast as _ast
    import inspect as _i
    print("\n[L14] 关软件时排队中的消息：呈现，不执行")
    k, _, _ = make_kernel(tmp / "l14")

    # ① 🔴 `list_unfinished` 必须 PENDING ∪ CLAIMED
    a = I.submit_user_message("这条排着队")
    b = I.submit_user_message("这条也排着")
    claimed = I.claim_next()          # 模拟「上个进程认领了但没 consume 就死了」
    got = {x.item_id for x in I.list_unfinished()}
    check(claimed is not None, "先造一个 CLAIMED 出来")
    check(got == {a, b},
          "🔴🔴 `list_unfinished` = PENDING ∪ CLAIMED —— "
          "📌 **「还没做完」不等于「还没开始」**：只取 PENDING 会漏掉"
          "「正在处理那一条」，而那恰恰是用户最在意的一条", str(got))
    check(claimed.item_id in got, "⭐ 被认领的那条确实在里面")

    # ② ⭐ 顺序：CLAIMED 比排队的更早，必须排最前
    check(I.list_unfinished()[0].item_id == claimed.item_id,
          "⭐ 认领中的那条排在最前（它比排队的更早发生）")

    # ③ 呈现完必须 discard —— 否则下一次 drain 会真的执行它
    I.discard(a, "重启后已呈现给用户")
    check(I.get(k, a).status == I.ItemStatus.DISCARDED,
          "⭐⭐ `discard` 记成 DISCARDED（不是删掉）—— 账上分得开")
    check(a not in {x.item_id for x in I.list_unfinished()},
          "🔴🔴 丢弃后不再出现在未完成清单里 —— "
          "📌 **「不执行」不是靠没人去执行它，是靠它不再处于可被执行的状态**")

    # ④ 接线（AST，只看会执行的代码）
    src = module_text("app")
    fn = _ast.parse(src)
    _m = next((n for n in _ast.walk(fn)
               if isinstance(n, _ast.AsyncFunctionDef)
               and n.name == "_startup_present_unsent"), None)
    check(_m is not None, "⭐ `_startup_present_unsent` 存在")
    if _m is not None:
        body = _ast.unparse(_m)
        check("add_system_note" not in body and "add_message" not in body,
              "🔴 **没有**任何一条写进模型上下文的路径 —— 模型看见一条没人处理的"
              "用户请求会去做，「呈现」和「执行」的界限就没了")
        check("add_ui_only_record" not in body,
              "只呈现一次、不写进聊天记录：它说明的是「上次关闭时的状态」，"
              "重启后或所在上下文被压缩移出后都不再有意义")
        check("discard" in body,
              "⭐⭐ 呈现之后**真的丢弃** —— 留着 PENDING 的话下次 drain 会执行它")
        check("WAKE_INTENT" in body or "USER_MESSAGE" in body,
              "⚠️ 按 kind 分流：`WAKE_INTENT` 不呈现（那不是用户打的字，"
              "「上个进程有没干完的活」归 [B1] `_startup_resume_offer` 问）")
        check("render_unsent_user_card" in body, "呈现时画出这张卡")

    # ⑤ 重放：旧版本写进记录的 inbox_unsent 一律跳过，不再画出来
    check(src.count("def render_unsent_user_card") == 1, "渲染器只有一份")
    _rp = next((n for n in _ast.walk(fn)
                if isinstance(n, _ast.FunctionDef) and n.name == "_replay_durable_conversation"), None)
    _rp_src = _ast.unparse(_rp) if _rp is not None else ""
    check("inbox_unsent" in _rp_src and "render_unsent_user_card" not in _rp_src,
          "重放认得旧的 `inbox_unsent` 记录并跳过，不画这张卡")

    # ⑥ ⚠️ 顺序：呈现排在「要不要接着做」之前
    _i_present = src.index("self._startup_present_unsent, once=True")
    _i_offer = src.index("self._startup_resume_offer, once=True")
    check(_i_present < _i_offer,
          "⚠️ 呈现排在 `_startup_resume_offer` **之前** —— "
          "📌 「你上次还有话没说完」该出现在「要不要接着做那件活」之前："
          "前者是事实回放，后者是基于事实的提问")

    # ⑦ ⚠️ 附件：明说失效，不去捞
    check("已失效" in src,
          "⚠️ 附件**明说已失效**（不去图库捞）—— "
          "📌 一个「看起来还在、点下去才发现没了」的附件，比明说没了更坏")


if __name__ == "__main__":
    sys.exit(main())
