# -*- coding: utf-8 -*-
"""租约成为权威 · Nano 碰壁停手。

三步迁移的第二步：**新的成权威，旧的还留着**。这个套件盯四件事：

**① `machine_is_free()` ≠ `nano_may_touch_os()`，混用会出事。**
   ⭐ 早先的设计原先写的是「`should_run` 改读 `nano_may_touch_os`」——**那句是错的**。
   后者在「Nano 自己持有」时返回 True，照它写，canary 会在 GUI 自动化跑到一半时
   去抢前台焦点，正是 `canary.py` 设计约束 ② 白纸黑字禁止的那件事
   （原文：「会重新引入『自检抢占前台焦点干扰正在执行的真实任务』这个刚修过的同类问题」）。
   这条错误是**回读来源文件**（canary.py 的设计约束）时抓到的，不是读早先的设计抓到的。

**② Nano 不许抢回用户手里的机器。**
   观测期 `acquire` 必须带 `preempt=True`（旧 bool 无互斥，坚持互斥会抛在主流程里）。
   切读之后必须去掉 —— 留着的话用户刚拿回电脑，Nano 下一个动作就一声不响抢回来，
   被动挂起等于白做。⭐ **抢占权只属于用户，不属于 Nano。**

**③ 两道闸都只在"有别的持有者"时才拒绝。**
   租约系统整个挂了 → 没人持有 → 放行，最坏退化成"没有被动挂起"，
   **不会把 OS 能力锁死**。⚠️ 这条是 fail-safe 方向的安全边界，必须钉死。

**④ 泄漏在结构上不可能再复现。**
   直接构造那个故障形状（拿了不还）——旧 bool 会永久卡住，租约到点自愈。

**⑤ 租约的粒度 = 一整段 GUI 操作，不是一次工具调用。**
   ⭐ 这条是**推演验收预期时才发现的**：`os_execute` 是单步工具，
   原先每次调用都 acquire/release，于是 Nano 真正持有机器的窗口只有单个动作那一瞬，
   **两步之间那段模型思考时间里没人持有** —— 而传感器只在 Nano 持有时武装，
   用户的点击大概率落在空窗里，被动挂起形同虚设。
   📌 **租约的粒度必须匹配「这台电脑归谁用」这件事的粒度，不是匹配代码的调用结构。**

用法：
  py -3.10 tests\cases\t_f1_stage5_bphase.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import Command, reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import task as _task
from core.runtime import oslease as L
from core.proactive import takeover as T
# 这些用例测的是「一轮进行中」的接管感知（两轮之间不监控，见 t_f1_stage5_takeover 的反例）
T.set_turn_active(True)

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path):
    _task.clear_blocker_providers_for_tests()
    T._reset_for_test()
    clock = FakeClock(BASE_T)
    return reset_kernel_for_tests(store=RuntimeStore(tmp / "rt.db"), clock=clock), clock


def _hold(k, holder, ttl=300.0, preempt=False):
    return k.submit(Command(kind=L.ACQUIRE, payload={
        "holder": holder, "reason": f"{holder} test", "ttl_sec": ttl,
        "preempt": preempt})).data


def _gui(k):
    """开 GUI 模式。⚠️ 2026-08-07 起传感器的武装条件是**在 GUI 模式**，
    不再是「Nano 持有活动租约」—— 理由见 `t_f1_stage5_takeover` 第 [11] 组
    （实测 17 次点击全部被漏掉）。凡是要触发接管的用例都得先开它。"""
    L.open_gui_session("test: gui task")


# ══════════════════════════════════════════════════════════════════════════

def t_two_predicates(tmp: pathlib.Path) -> None:
    """⭐⭐ 本套件最重要的一组：两个读法回答的不是同一个问题。"""
    print("\n[1] ⭐⭐ machine_is_free() 与 nano_may_touch_os() 必须分开")
    k, _ = make_kernel(tmp / "a")

    # 无人持有
    check(L.machine_is_free(k) is True, "无人持有 → 机器闲着")
    check(L.nano_may_touch_os(k)[0] is True, "无人持有 → Nano 能动手")

    # Nano 持有 —— ⭐ 分歧就在这一格
    _hold(k, L.Holder.NANO)
    check(L.machine_is_free(k) is False,
          "⭐⭐ Nano 自己持有 → 机器**不闲**（canary 不许跑）")
    check(L.nano_may_touch_os(k)[0] is True,
          "⭐⭐ 同一时刻 Nano **能**动手（本来就是它的）—— 两个答案相反")

    # 用户持有
    k2, _ = make_kernel(tmp / "a2")
    _hold(k2, L.Holder.USER, ttl=20.0)
    check(L.machine_is_free(k2) is False, "用户持有 → 机器不闲")
    check(L.nano_may_touch_os(k2)[0] is False, "用户持有 → Nano 不能动手")
    check(L.machine_is_free(k2) is False and L.nano_may_touch_os(k2)[0] is False,
          "⭐ 这一格两者一致 —— 所以只测这一格的话，混用永远测不出来")

    # 过期
    k3, clock3 = make_kernel(tmp / "a3")
    _hold(k3, L.Holder.NANO, ttl=10.0)
    clock3.advance(11)
    check(L.machine_is_free(k3) is True,
          "⭐ 到期即算闲 —— 这就是取代裸 bool 的那条性质（不需要任何人 release）")


def t_nano_cannot_grab_back(tmp: pathlib.Path) -> None:
    """⭐⭐ 切读那一步最要紧的一处改动。"""
    print("\n[2] ⭐⭐ 抢占权只属于用户，不属于 Nano")
    k, _ = make_kernel(tmp / "b")
    _gui(k)
    _hold(k, L.Holder.NANO)
    T.on_user_signal(T.CLICK, hwnd=0xF00D)      # 用户接管
    check(L.current_activity(k).holder == L.Holder.USER, "前提：用户拿走了机器")

    h = L.acquire_activity("nano tries again")
    check(h is None,
          "⭐⭐ Nano 再去拿 → **拿不到**（观测期那个 `preempt=True` 已去掉）")
    check(L.current_activity(k).holder == L.Holder.USER,
          "⚠️ 而且用户那条**没被动过** —— 拿不到不等于悄悄抢了一半")

    # 反证：换成用户来拿，就必须拿得到
    k2, _ = make_kernel(tmp / "b2")
    _gui(k2)
    _hold(k2, L.Holder.NANO)
    check(T.on_user_signal(T.CLICK, hwnd=0xF00D) == T.TakeoverResult.TAKEN,
          "反证：**用户**从 Nano 手里拿 —— 拿得到。"
          "否则上面只证明了「谁都拿不到」")

    # AST：acquire_activity 里不许再出现 preempt
    src = module_text("core.runtime.oslease")
    tree = ast.parse(src)
    seg = ""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "acquire_activity":
            seg = ast.get_source_segment(src, n) or ""
    body = "\n".join(l for l in seg.splitlines() if not l.strip().startswith("#"))
    check('"preempt"' not in body and "'preempt'" not in body,
          "⭐ AST：`acquire_activity` 的**代码里**不再传 preempt（注释里讲历史不算）")


def t_gates_only_block_others(tmp: pathlib.Path) -> None:
    """⚠️ fail-safe 的安全边界：坏掉时退化，不是锁死。"""
    print("\n[3] ⚠️ 两道闸只在「有别的持有者」时拒绝")
    k, _ = make_kernel(tmp / "c")

    check(L.nano_may_touch_os(k)[0] is True,
          "⭐ 租约系统里空空如也（等价于「整个没在用」）→ **放行**")
    check(L.acquire_activity("normal work") is not None,
          "⭐ 也拿得到租约 —— 所以租约层挂掉最坏是退回「没有被动挂起」，不是锁死 OS")

    ok, why = L.nano_may_touch_os(k)
    check(ok is True and "nano" in why.lower(), "自己持有时继续放行", why)

    # 唯一会拒的：读不出来
    saved = L.current_activity
    try:
        L.current_activity = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db locked"))
        ok2, why2 = L.nano_may_touch_os(k)
        check(ok2 is False and "unreadable" in why2,
              "⚠️ 读不出状态 → fail-safe 说「不能」（宁可停手，也不在用户打字时抢鼠标）")
    finally:
        L.current_activity = saved


def t_leak_shape_cannot_recur(tmp: pathlib.Path) -> None:
    """⭐ 直接构造那个故障形状。"""
    print("\n[4] ⭐ 那个 61 分钟的泄漏形状，在结构上不可能再出现")
    k, clock = make_kernel(tmp / "d")

    h = L.acquire_activity("os_execute")
    check(h is not None and L.machine_is_free(k) is False, "开工，机器忙")

    # 模拟泄漏：生成器被丢弃，谁都没有 release、没有心跳
    del h
    check(L.machine_is_free(k) is False, "刚泄漏时确实还是忙（TTL 还没到）")

    clock.advance(L.OS_ACTIVITY_TTL_SEC + 1)
    check(L.machine_is_free(k) is True,
          "⭐⭐ 到期自愈 —— canary 恢复运行。旧 bool 在这个场景下会**永久**卡 True")

    # 而且下一次 acquire 顺手把它收成 EXPIRED，不必等任何 tick
    h2 = L.acquire_activity("next task")
    check(h2 is not None, "下一次任务照常拿得到租约")
    rows = []
    with k.store.read() as conn:
        rows = [r["status"] for r in conn.execute(
            "SELECT status FROM os_leases WHERE kind='activity' ORDER BY created_at")]
    check(L.LeaseStatus.EXPIRED in rows,
          "⭐ 泄漏那条被收成 EXPIRED（留了痕，不是悄悄消失）", str(rows))


def t_streak_granularity(tmp: pathlib.Path) -> None:
    """⭐⭐ 租约覆盖一整段 GUI 操作，不是一个动作。"""
    print("\n[5] ⭐⭐ 粒度：租约的寿命 = 一段 GUI 操作，不是一次工具调用")
    k, _ = make_kernel(tmp / "e")
    _gui(k)

    # 模拟 Nano 连做三步：第一步拿租约，后两步幂等复用
    h1 = L.acquire_activity("os_execute")
    check(h1 is not None, "第 1 步拿到租约")
    lid = L.current_activity(k).lease_id

    for step in (2, 3):
        # 生产代码的写法：已经持有就不再拿
        held = L.current_activity(k)
        if held is None or held.holder != L.Holder.NANO:
            L.acquire_activity("os_execute")
        L.heartbeat_activity(h1)
        check(L.current_activity(k).lease_id == lid,
              f"第 {step} 步仍是**同一条**租约（没有还了再拿）")

    # ⭐ 关键：两步之间用户点一下，必须被抓到
    check(T.on_user_signal(T.CLICK, hwnd=0xF00D) == T.TakeoverResult.TAKEN,
          "⭐⭐ 步与步之间用户点击 → **抓到了**。"
          "每步都归还的话这里是 ignored_nano_idle，被动挂起形同虚设")

    # 反证：如果 Nano 真的还了锁，同一个点击就抓不到 —— 证明上面那条测的是粒度
    k2, _ = make_kernel(tmp / "e2")
    h2 = L.acquire_activity("os_execute")
    L.release_activity(h2)                      # 模拟"每步都还"的旧写法
    # ⚠️ 这里刻意**不开** GUI 模式 —— 反证的是"没武装就抓不到"
    check(T.on_user_signal(T.CLICK, hwnd=0xF00D) == T.TakeoverResult.IGNORED_NANO_IDLE,
          "反证：还了锁之后同一个点击**抓不到** —— "
          "所以上面那条确实在测粒度，不是测「点击总能抓到」")


def t_wiring() -> None:
    print("\n[6] 接线核对（AST / 源码，不靠猜）")
    orc = module_text("core.orchestrator")
    dsp = module_text("core.os_layer.dispatch")
    oc = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))
    dc = "\n".join(l for l in dsp.splitlines() if not l.strip().startswith("#"))

    check("canary.should_run(not _rt_machine_is_free())" in oc,
          "⭐⭐ canary 读租约")
    check("self._rt_os_lease is None" in oc,
          "⭐⭐ 闸一：`os_execute` 检查了 acquire 的返回值 —— "
          "不检查的话被动挂起完全不生效")
    check("nano_may_touch_os" in dc,
          "⭐⭐ 闸二：dispatch 里有中途碰壁的闸")
    check("machine_not_available" in dc,
          "⚠️ 错误码与 `permission_denied` 分开 —— "
          "「机器暂时不归你」和「你没被授权」是两件事，混了模型会去排查权限")

    # 闸二只拦鼠标键盘
    i = dc.find("nano_may_touch_os")
    head = dc[max(0, i - 600):i]
    # ⚠️ 这条 2026-08-07 **反过来了**：作用域已改成「在 GUI 模式就全量监控」，
    #    不再按动作种类分。而原来的理由（"恢复后判断环境变没变恰恰需要能看"）
    #    是**闸时代**的产物 —— 出口改成「等待」后，"拦"= 稍后再做，那个担心不存在了。
    # 📌 **机制的出口从「失败」变成「等待」时，一大批「不能拦 X」的论据会自动失效。**
    check("contends_for_machine" in head or "contends_for_machine" in dc,
          "⭐ 闸二用的是显式集合而不是 `_is_mk` 代理"
          "（那三个等级分的是**能力范围**，不是「争不争鼠标」）")

    # ── 三步迁移的终点（原来这里断言的是**中间态**）─────────────────────────
    # ⚠️⚠️ 这条原文是：
    #     `oc.count("self._os_task_busy = True") == 2` ——
    #     「切写之前旧 bool 仍在维护 —— 只切读点会把它变成没人维护的僵尸」
    # 那在**只切了读点**的时候是对的：读点已切到租约、写点还留着，才能随时回退。
    # 但**切写那一步把那个 bool 删了**，于是这条断言开始要求"它还在" ——
    # 📌 **一条测中间态的断言，在迁移完成后会反过来阻止终态。**
    #    这就是为什么三步迁移的第三步必须**连测试一起收尾**，
    #    而不只是删产品代码（本轮 `t_f1_stage5_shadow.py` 已按此重写过一次，
    #    这个文件当时漏了 —— 所以那次"全绿"其实是 1123/1124，不是 1124/1124）。
    check("self._os_task_busy" not in oc,
          "⭐ 终态：旧 bool 已删净 —— 那个「漏写一次就永久停摆且一声不响」"
          "的裸 bool 不再存在（互斥现在由租约的 TTL 自愈）")
    # ⭐ 这一对断言故意打在**两个不同的字符串**上，而这正好把要保的性质说清了：
    #    `oc` 是**剥掉注释后**的源码（上面第 248 行），`orc` 是原文。
    #    于是「代码里没有、注释里有」可以被**直接测出来**，不是靠人自觉。
    #    📌 删的是「还在运行的东西」，不是「关于它的记忆」——
    #       那些注释是「为什么会有租约」的唯一记录。
    check("_os_task_busy" in orc,
          "⚠️ 注释里的历史留着（`oc` 里没有 / `orc` 里有 = 正是想要的形状）")

    # ── 免确认授权也切到租约了 ──────────────────────────
    apc = module_text("app")
    apl = chr(10).join(l for l in apc.splitlines() if not l.strip().startswith("#"))
    _dsl = module_text("core.os_layer.dsl")
    _aao = _dsl.split("def auto_authorization_on")[1].split("def ")[0]
    check("temp_auto_authorized()" in _aao and "user_auto_mode_on(" in _aao,
          "⭐⭐ Auto 的判断（`dsl.auto_authorization_on`）= 用户选的 Auto 或临时授权租约")
    check("def _auto_on" not in apl,
          "⭐ 界面不再自己判断 Auto（放行判定在后端）")
    check("_temp_auto" not in apl,
          "旧 bool `_temp_auto` 已删除：临时授权只以授权租约为准")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_two_predicates(tmp)
        t_nano_cannot_grab_back(tmp)
        t_gates_only_block_others(tmp)
        t_leak_shape_cannot_recur(tmp)
        t_streak_granularity(tmp)
    t_wiring()
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
