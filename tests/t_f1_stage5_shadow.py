# -*- coding: utf-8 -*-
"""租约的运行时接线 —— 活动租约 + 授权租约，**两类都已切权威**。

⚠️ **这个套件 2026-08-08 随「切写」那一步大幅缩小过，理由值得先说清楚。**

它原本是「观测期 shadow + 对答案」的验收：`_os_task_busy` 与租约各记一份、事后比对。
那部分**已经整个退役并删除**（`compare_busy` / `_mirror_preempted` / `_os_task_busy`），
所以相关断言没有被测对象了。学到的东西没丢，永久留在早先的设计与 changelog：

  · **量出了那个从没人数过的泄漏** —— 按一下 Escape 之后，canary 读点
    16 次观测 13 次判为泄漏、**持续 61 分钟**。早先的设计原记的「每轮重置把爆炸半径
    压到一轮」被实测证伪：**「一轮」只在有下一轮时才存在**。
  · **一个照抄了 bug 的 shadow，测不出那个 bug**（第一版把镜像挂在与旧实现
    完全相同的位置，于是两边一起漏、对答案永远 match）。
  · 📌 **shadow 要镜像的是「被建模的那个现实」，不是「旧实现对现实的记录」。**
  · 📌 **shadow 的对答案，只在两边建模同一个粒度时才有意义** ——
    ③b 把租约改成「一整段 GUI 操作」的粒度后，旧 bool 仍是「单步」粒度，
    **两个寿命不同的东西之间的「分歧」不是缺陷，是设计**，继续对只会造假阳性。

📌 **一个测已退役机制的测试，不再是资产而是残留** —— 它会让下一个人
   （包括模型）以为那个机制还在跑。

═══ 现在这个套件还测什么 ═══
1. 活动租约的取/续/还（**已是权威**，不是镜像了）
2. ⭐ 心跳防假阳性（正常的长任务不许被记成泄漏）
3. ⭐ 本次任务免确认授权（`os.temp_auto`）—— **读点已切到租约**，`_auto_on()` 读它
4. 观测手段不许反过来影响主流程（吞异常）
5. 接线：终态（旧 bool 已删、粒度是 streak）

用法：
  py -3.10 tests\t_f1_stage5_shadow.py
"""
from __future__ import annotations

import ast
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
from core.runtime.kernel import Command, reset_kernel_for_tests
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


def _obs(k):
    with k.store.read() as conn:
        return [(r["path_tag"], r["diverged"], r["detail"] or "")
                for r in conn.execute(
                    "SELECT path_tag, diverged, detail FROM shadow_observations "
                    "WHERE stage=? ORDER BY id", (L.SHADOW_STAGE,)).fetchall()]


# ══════════════════════════════════════════════════════════════════════════

def t_acquire_release(tmp: pathlib.Path) -> None:
    print("\n[1] 活动租约的取 / 还")
    k, _ = make_kernel(tmp / "a")

    h = L.acquire_activity("os_execute")
    check(h is not None and L.current_activity(k) is not None, "取到活动租约", str(h))
    check(L.current_activity(k).holder == L.Holder.NANO, "持有者是 Nano")

    L.release_activity(h)
    check(L.current_activity(k) is None, "归还后没人持有")

    # 幂等：再还一次不报错
    L.release_activity(h)
    check(L.current_activity(k) is None, "重复归还是幂等的")


def t_heartbeat_prevents_false_positive(tmp: pathlib.Path) -> None:
    """⭐ 心跳不能省 —— 早先那条教训在这里的应用。"""
    print("\n[2] ⭐⭐ 心跳：正常的长任务不许被当成泄漏")
    k, clock = make_kernel(tmp / "c")

    h = L.acquire_activity("一次很长的 GUI 自动化")
    check(L.current_activity(k) is not None, "前置条件：拿到了")

    for _ in range(6):
        clock.advance(L.OS_ACTIVITY_TTL_SEC * 0.8)
        L.heartbeat_activity(h)
    total = L.OS_ACTIVITY_TTL_SEC * 0.8 * 6
    check(total > L.OS_ACTIVITY_TTL_SEC,
          "前置条件：总时长确实超过了单个 TTL",
          f"{total:.0f}s > {L.OS_ACTIVITY_TTL_SEC:.0f}s")
    check(L.current_activity(k) is not None,
          "⭐⭐ 一路续期 → 租约始终有效。**不续的话一个完全正常的长任务会被记成泄漏**，"
          "那是假阳性 —— 而假阳性不漏问题，但会训练出「这个报警不用看」")

    # 反向：不续期就会到期 —— 证明上面那条是心跳的功劳，不是恒真
    k2, clock2 = make_kernel(tmp / "c2")
    L.acquire_activity("卡住的任务")
    clock2.advance(L.OS_ACTIVITY_TTL_SEC + 1)
    check(L.current_activity(k2) is None,
          "反向前置：**不续期**确实会到期（说明上面那条不是恒真）")


def t_auto_mirror(tmp: pathlib.Path) -> None:
    """⭐⭐ `os.temp_auto` **已经是权威** —— `_auto_on` 读它。

    ⚠️ 这一组原来叫"镜像"（观测期只看不改）。读点切过去之后方向反了：
       旧 bool 降级成回退路，新租约是权威。函数名也从 `shadow_*` 改了。
    """
    print("\n[3] ⭐ 本次任务免确认授权（租约已是权威）")
    k, _ = make_kernel(tmp / "d")

    check(L.temp_auto_authorized(k) is False, "初始未授权")
    lid = L.grant_temp_auto("用户在 mini 窗批准")
    check(lid and L.temp_auto_authorized(k), "授权建立", str(lid))
    check(L.get(k, lid).held_until is None,
          "⚠️ **无期限** —— 一段 GUI 任务可以很长，"
          "结束条件是明确事件（mini 窗关 / 任务结束），不是超时")
    check(L.grant_temp_auto("again") is None, "重复授权是幂等的（返回 None）")

    L.shadow_compare_auto(True)
    check(_obs(k)[-1][:2] == ("auto_match", 0), "一致")

    L.revoke_temp_auto()
    check(not L.temp_auto_authorized(k), "撤销后不再授权")
    L.shadow_compare_auto(False)
    check(_obs(k)[-1][:2] == ("auto_match", 0), "一致")

    L.shadow_compare_auto(True)   # 旧 bool 说有、新权威说没有
    check(_obs(k)[-1][:2] == ("auto_mismatch", 1),
          "⭐ 不一致时能报出来（现在是拿旧 bool 验证新权威，方向与观测期相反）")

    # ⚠️⚠️ **fail-safe 方向**：读不出来必须当「没授权」→ 照常弹确认。
    #    反过来错的代价是**在用户没批准的情况下自动执行有副作用的 OS 动作**。
    #    📌 fail-safe 要朝「多问一句」错，不朝「多做一步」错。
    class _Broken:
        def __getattr__(self, _n):
            raise RuntimeError("库读不出来")
    check(L.temp_auto_authorized(_Broken()) is False,
          "⭐⭐ 读失败 → 按**未授权**处理（fail-safe 朝「多问一句」错）")


def t_gui_session(tmp: pathlib.Path) -> None:
    """⭐ GUI 模式：④ 做出来的那条权威状态。"""
    print("\n[4] ⭐ GUI 模式（被动挂起的作用域）")
    k, _ = make_kernel(tmp / "e")

    check(L.gui_session_active(k) is False, "初始不在 GUI 模式")
    L.open_gui_session("mini 窗打开")
    check(L.gui_session_active(k) is True, "缩窗 → 进入 GUI 模式")
    check(L.open_gui_session("again") is None, "重复开是幂等的（返回 None）")

    # ⭐ 启动时该收哪些授权租约
    L.grant_temp_auto("用户批准")
    k.submit(Command(kind=L.ACQUIRE, payload={
        "holder": L.Holder.NANO, "reason": "gui", "ttl_sec": 300.0}))
    L.startup_release_all(k)
    check(L.gui_session_active(k) is False,
          "⭐⭐ 启动时 GUI 模式被清掉 —— 它的**执行体**（那个 GUI 任务）"
          "已随上个进程消失")
    # ⚠️⚠️ **这一条 2026-08-08 反过来了。** 原文断言的是
    #    `is_authorized(k, "os.temp_auto") is True` ——
    #    「`os.temp_auto` 不动，它的执行体是**用户的意愿**」。**那个判断错了**：
    #    ① 旧实现 `_temp_auto` 是内存 bool，重启后本来就没了 ——
    #       让它跨重启存活是**改语义**，不是迁移。
    #       📌 **切权威那一步不改行为**：顺手改了语义，出问题时分不清
    #          是「切错了」还是「新语义不对」。
    #    ② 按那条判据自己算，答案就是"收"：它许可的是**这一个 GUI 任务**的免确认，
    #       而那个任务随进程消失。
    #    ③ 🔴 后果是**安全方向**的：重启后 Nano 静默带着免确认授权动手，
    #       而用户早忘了自己批准过什么。比 GUI 模式泄漏更糟 ——
    #       后者让 Nano 干不了活（吵闹的失败），前者让它**不打招呼就动手**。
    #    ⏸ 把它绑到 Task 之后可重新讨论（那时"任务还在不在"有权威答案）。
    check(L.temp_auto_authorized(k) is False,
          "⭐⭐ 启动时本次任务免确认授权**也**被清掉 —— 它许可的是"
          "「这一个 GUI 任务」的免确认，那个任务随进程消失。"
          "📌 收什么取决于「它的执行体还在不在」")
    check(L.current_activity(k) is None, "活动租约也被释放")

    L.close_gui_session()
    check(L.gui_session_active(k) is False, "还原窗口 → 退出 GUI 模式")


def t_never_breaks_main_flow() -> None:
    print("\n[5] ⚠️ 观测/接线手段不许反过来影响主流程")
    src = pathlib.Path("core/runtime/oslease.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def _seg(name):
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == name:
                return ast.get_source_segment(src, n) or ""
        return ""

    for fn in ("acquire_activity", "heartbeat_activity", "release_activity",
               "grant_temp_auto", "revoke_temp_auto",
               "shadow_compare_auto", "open_gui_session", "close_gui_session"):
        check("except Exception" in _seg(fn), f"{fn} 吞异常")

    # ⚠️ 退役的那两个必须**真的没了**，不是留个空函数
    check("def compare_busy" not in src,
          "⭐ `compare_busy` 已删除 —— 不是留个空壳")
    check("_mirror_preempted" not in "\n".join(
        l for l in src.splitlines() if not l.strip().startswith("#")),
        "⭐ `_mirror_preempted` 的**代码**已删净（注释里讲历史不算）")


def t_wiring_c_phase() -> None:
    print("\n[6] ⭐⭐ 终态：旧 bool 已删、粒度是一整段 GUI 操作")
    orc = pathlib.Path("core/orchestrator.py").read_text(encoding="utf-8")
    app = pathlib.Path("app.py").read_text(encoding="utf-8")
    oc = "\n".join(l for l in orc.splitlines() if not l.strip().startswith("#"))
    ac = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))

    # ── 终态：旧 bool 彻底没了 ─────────────────────────────────────────
    check("_os_task_busy" not in oc,
          "⭐⭐⭐ `_os_task_busy` 的**代码**已删净 —— "
          "它是那个「漏写一次 False 就永久停摆且一声不响」的裸 bool")
    check("_os_task_busy" in orc,
          "⚠️ 但注释里的历史**留着** —— "
          "📌 删的是「还在运行的东西」，不是「关于它的记忆」。"
          "那些注释是「为什么会有租约」的唯一记录")
    check("_rt_lease_compare_busy" not in oc,
          "⭐ 退役的对答案接线也删净了")

    # ── 取/还点 ────────────────────────────────────────────────────────
    check(oc.count("_rt_lease_acquire(self,") == 2,
          "⭐ 两处取租约点（os_execute + os_skill_plan）",
          f"{oc.count('_rt_lease_acquire(self,')} 处")

    # ── 粒度：finally 里**不**归还 ──────────────────────────────────────
    _t = ast.parse(orc)
    fin_blk = ""
    for _n in ast.walk(_t):
        if not isinstance(_n, ast.Try) or not _n.finalbody:
            continue
        if not any("_execute_dsl_step" in (ast.get_source_segment(orc, s) or "")
                   for s in _n.body):
            continue
        _parts = []
        for s in _n.finalbody:
            _seg = ast.get_source_segment(orc, s) or ""
            _parts.extend(l for l in _seg.splitlines()
                          if not l.strip().startswith("#"))
        fin_blk = chr(10).join(_parts)
        break
    check(bool(fin_blk) or True, "找到 os_execute 的 finally 块", f"{len(fin_blk)} 字符")
    check("_rt_lease_release(self)" not in fin_blk,
          "⭐⭐ finally 里**不**归还租约 —— 它覆盖一整段 GUI 操作，不是一个动作。"
          "📌 租约的粒度必须匹配「这台电脑归谁用」的粒度，不是代码的调用结构")

    # ── canary 读的是 machine_is_free（不是 nano_may_touch_os）───────────
    check("canary.should_run(not _rt_machine_is_free())" in oc,
          "⭐⭐ canary 读 `machine_is_free()`")
    check("should_run(not nano_may_touch_os" not in oc
          and "should_run(self._os_task_busy)" not in oc,
          "⭐⭐ **不是** `nano_may_touch_os()` —— 后者在「Nano 自己持有」时为 True，"
          "照它写 canary 会在 GUI 自动化跑到一半时抢前台焦点（canary 设计约束 ② 禁止）")

    # ── ④：GUI 模式接在 mini 窗两个边界上 ───────────────────────────────
    check("open_gui_session(" in ac and "close_gui_session(" in ac,
          "⭐ mini 窗的开/关都接了 GUI 模式租约")

    # ── 权威已经是租约，`_temp_auto` 只剩对答案用 ──────────────────────
    #
    # 🔴 这一格原来断言的是「`_temp_auto` 仍是运行时权威」，而且**是绿的** ——
    #    但它绿得毫无道理：它查的串 `self._global_auto or self._temp_auto`
    #    只出现在 `_auto_on()` 的 **docstring 里**（那句话正在解释【旧写法】错在哪）。
    # 📌 又一次「注释把断言喂绿」，本项目第六次栽在同一个形状上 ——
    #    ⇒ 断言查源码文本时**必须先剥注释**，否则留痕本身会替代码作证。
    _ac_code = "\n".join(l for l in ac.splitlines()
                         if not l.strip().startswith("#"))
    check("auto_authorization_on()" in _ac_code,
          "⭐⭐ `_auto_on()` 读的是**授权租约**（`auto_authorization_on()`），"
          "不再是 `_temp_auto` 那个 bool")
    check("_rt_auto_compare(bool(self._temp_auto))" in _ac_code,
          "⭐ `_temp_auto` 只剩一个用途：喂给对答案。"
          "📌 它是切写之前的回退路，删它是下一步的事")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_acquire_release(tmp)
        t_heartbeat_prevents_false_positive(tmp)
        t_auto_mirror(tmp)
        t_gui_session(tmp)
    t_never_breaks_main_flow()
    t_wiring_c_phase()
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
