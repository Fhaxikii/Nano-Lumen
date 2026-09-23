# -*- coding: utf-8 -*-
"""被动挂起 · 感知层 —— 用户接管判定 + 抢占接线。

对应早先的设计。这个套件盯三件事，其余都是附带：

**① 判定的是「有后果的动作」，不是「有活动的迹象」。**
   纯鼠标移动不算 —— 早先已定，理由是光标动了但窗口层级/焦点/控件状态一个没变，
   Nano 的坐标全部仍然有效。为不产生后果的事挂起就是自找抖动。

**② `injected` 过滤不是可选项。**
   Nano 自己 SendInput 打的字也会经过同一个钩子。不过滤 = Nano 一打字就把
   自己的锁抢走、下一步立刻碰壁 —— **自锁死**。
   ⭐ 特意没用"动手前后设个自抑制标志"那种写法：那正是 `_os_task_busy` 栽过的形状
   （靠所有调用点都记得配对），新增一个发送输入的地方就静默破掉。

**③ 传感器只在 Nano 持有机器时武装。**
   用户在场是 Nano 的**常态**。Nano 闲着的时候用户随手一点就锁住 20 秒的话，
   Nano 就变成"想干活先等你消停"，荒谬。

═══ 还有一条是"不制造假阳性" ═══
被动挂起一接线，旧的 `compare_busy` 就会把「用户持有」记成「泄漏」。
那是**设计内的差异**被算成缺陷 —— 假阳性是 shadow 最坏的一种失败
（早先栽过一次，早先自己又栽过一次）。所以第 [5] 组专门测这个。

用法：
  py -3.10 tests\t_f1_stage5_takeover.py
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
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import Command, reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import task as _task
from core.runtime import oslease as L
from core.proactive import takeover as T
from core.proactive.takeover import TakeoverResult as R

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


def _obs(k):
    with k.store.read() as conn:
        return [(r["path_tag"], r["diverged"], r["detail"] or "")
                for r in conn.execute(
                    "SELECT path_tag, diverged, detail FROM shadow_observations "
                    "WHERE stage=? ORDER BY id", (L.SHADOW_STAGE,)).fetchall()]


def _nano_holds(k, reason: str = "gui"):
    """Nano 正在做一段 GUI 任务：**GUI 模式开着** + 它此刻握着鼠标。

    ⚠️ 2026-08-07 起必须同时开 GUI 模式 —— 传感器的武装条件从
    「Nano 持有活动租约」换成了「在 GUI 模式」。
    🔴 换的理由是实测灾难：活动租约只在**键鼠动作那一瞬**被持有，
       用户点 17 下跨 17.6 秒**全部** `ignored_nano_idle`，第 18 下才命中。
    📌 **「Nano 此刻握着鼠标」≠「Nano 此刻在做 GUI 任务」** ——
       前者瞬时断续，后者持续。武装必须挂在后者上。
    """
    L.open_gui_session("test: gui task")
    return k.submit(Command(kind=L.ACQUIRE, payload={
        "holder": L.Holder.NANO, "reason": reason, "ttl_sec": 300.0})).data


def _gui_mode_only(k):
    """只开 GUI 模式，**Nano 此刻不握鼠标** —— 就是被漏掉的那 17 次点击所在的时段
    （Nano 在思考 / 跑命令 / 等 MCP）。"""
    L.open_gui_session("test: gui task, between actions")


# ══════════════════════════════════════════════════════════════════════════

def t_consequential_table(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐ 判定表：有后果的动作 vs 有活动的迹象")
    k, _ = make_kernel(tmp / "a")
    _nano_holds(k)

    for kind in (T.CLICK, T.KEY, T.FOCUS_CHANGE, T.SCROLL, T.DRAG):
        check(kind in T.CONSEQUENTIAL, f"{kind} 算「有后果」")

    check(T.MOVE not in T.CONSEQUENTIAL,
          "⭐⭐ 纯鼠标移动**不算** —— 坐标/焦点/层级一个都没变")
    check(T.on_user_signal(T.MOVE) == R.IGNORED_NOT_CONSEQUENTIAL,
          "⭐ 移动信号进来直接被判掉，不碰内核")
    check(L.current_activity(k).holder == L.Holder.NANO,
          "⚠️ 而且租约**没被动过** —— 判掉不等于走了一遍再回滚")

    check(T.SCROLL in T.CONSEQUENTIAL,
          "⭐ 滚轮算 —— 滚一下页面上每个元素的坐标全变了，这是清单之外补的一条")


def t_injected_filter(tmp: pathlib.Path) -> None:
    """⭐⭐ 不过滤就是自锁死。"""
    print("\n[2] ⭐⭐ injected 过滤：Nano 不能被自己的输入抢占")
    k, _ = make_kernel(tmp / "b")
    d = _nano_holds(k, "nano typing")

    check(T.on_user_signal(T.KEY, injected=True) == R.IGNORED_SELF_INPUT,
          "⭐⭐ 程序合成的按键被忽略（Nano 自己 SendInput 打的字）")
    cur = L.current_activity(k)
    check(cur is not None and cur.holder == L.Holder.NANO and cur.lease_id == d["lease_id"],
          "⚠️ Nano 的租约原封不动 —— 不过滤的话这里已经自锁死了")

    check(T.on_user_signal(T.CLICK, injected=True) == R.IGNORED_SELF_INPUT,
          "⭐ 合成的点击同样忽略（`_SendInputClick` 发出去的那些）")

    # 反证：同一个信号只要不是合成的，就必须真的抢
    check(T.on_user_signal(T.KEY, injected=False) == R.TAKEN,
          "反证：同一个 KEY 信号，非合成时**确实**会抢 —— "
          "否则上面两条只证明了「什么都没发生」")


def t_only_armed_when_nano_holds(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐ 传感器只在 Nano 持有机器时武装")
    k, _ = make_kernel(tmp / "c")

    check(L.current_activity(k) is None, "前提：没人持有机器")
    check(T.on_user_signal(T.CLICK) == R.IGNORED_NANO_IDLE,
          "⭐ Nano 闲着时用户点鼠标 → 什么都不做")
    check(L.current_activity(k) is None,
          "⚠️ **没有**凭空造出一条 USER 租约 —— "
          "否则用户随手一点就锁住 20 秒，Nano 变成「想干活先等你消停」")

    _nano_holds(k)
    check(T.on_user_signal(T.CLICK) == R.TAKEN,
          "反证：Nano 一持有，同一个点击立刻生效")


def t_takeover_and_renew(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐ 接管 = 用户真的持有；防抖 = 续期，不是第二个计时器")
    k, clock = make_kernel(tmp / "d")
    d = _nano_holds(k)

    check(T.on_user_signal(T.CLICK, "left") == R.TAKEN, "用户点击 → 接管")
    cur = L.current_activity(k)
    check(cur is not None and cur.holder == L.Holder.USER,
          "⭐⭐ 用户**真的持有**了机器（不是把 Nano 的打掉就完事）", str(cur.holder))
    check(L.get(k, d["lease_id"]).status == L.LeaseStatus.PREEMPTED,
          "Nano 那条被打成 PREEMPTED")
    check(abs(cur.held_until - (BASE_T + T.USER_HOLD_SEC)) < 1e-6,
          f"USER 租约的期限是 now + {T.USER_HOLD_SEC:.0f}s", str(cur.held_until))

    ok, why = L.nano_may_touch_os(k)
    check(ok is False and "user" in why,
          "⭐⭐ `nano_may_touch_os()` 因此说「不能」—— "
          "这就是「瞬发」：不用通知 Nano，它自己碰壁")

    # 防抖 = 续期
    lid = cur.lease_id
    # ⚠️ **推进量从 `USER_HOLD_SEC` 推，不写死** —— 这里原来是 `advance(15)`，
    #    在 HOLD=20 时是「还没到期」，而 2026-08-26 HOLD 调到 12 之后
    #    15 秒**已经过期**了：那个信号就不再是「续期」，而是一次全新接管，
    #    于是这两条断言一起红。
    # 📌 与本文件上面那条同形：**一个从参数派生出来的测试值，必须跟着参数走。**
    clock.advance(T.USER_HOLD_SEC - 5.0)
    T._reset_for_test()          # 跳过合并窗口（那只是省 IO，不是防抖）
    # ⚠️ 这里 2026-08-07 加了 `hwnd=` —— 原来不传，靠的是"落点未知也续期"那个**漏洞**。
    #    那个漏洞已修（见 `on_user_signal` 里 `_cls == "unknown"` 那段）：
    #    **首次接管保守是对的，续期保守是错的** —— 后者可以无限自我延长。
    #    📌 一个测试如果依赖某个漏洞才能过，修掉漏洞时它必须跟着改，
    #       而它变红正是它在起作用的证据。
    _OTHER_WIN = 0xF00D          # 随便一个非本进程的句柄 → 分类为 other
    check(T.on_user_signal(T.KEY, hwnd=_OTHER_WIN) == R.RENEWED,
          "用户继续操作（落点是第三方窗口）→ 续期")
    cur2 = L.current_activity(k)
    check(cur2.lease_id == lid,
          "⭐⭐ 还是**同一条**租约 —— 没有「挂起→恢复→再挂起」的翻转，"
          "实测点出来的第二个极端场景在这里被消掉")
    # ⚠️ 同上：这里的 `15` 也是写死的派生值，跟着推进量一起改成从 HOLD 推。
    check(abs(cur2.held_until
              - (BASE_T + (T.USER_HOLD_SEC - 5.0) + T.USER_HOLD_SEC)) < 1e-6,
          "期限被推后了（从最后一次动手起算，再给满 HOLD）", str(cur2.held_until))

    # 用户停手 → 到期即自动交还，不需要任何人 release
    clock.advance(T.USER_HOLD_SEC + 1)
    check(L.current_activity(k) is None,
          "⭐ 用户停手超过 TTL → 租约自动不算数（「停下来了」的定义就是到期）")
    ok2, _ = L.nano_may_touch_os(k)
    check(ok2 is True, "Nano 可以把机器拿回去了 —— 全程没有人调 release")

    # ── ⭐⭐ 续期不许"不确定就续" ────────────────────────────────────────
    # 实测：19:29:26 接管的一条 **20s** 租约，到 19:31:12 才被回收 ——
    # **106 秒**。用户那段时间没操作，是"落点未知也续期"把它一路推后的。
    # 📌 **首次接管保守是对的，续期保守是错的**：
    #    首次"不确定就停手"最多让 Nano 等 20 秒；
    #    续期"不确定就再锁 20 秒"**可以无限自我延长** ——
    #    同一个"宁可保守"的直觉，在两个位置上后果完全相反。
    k9, clock9 = make_kernel(tmp / "d9")
    _nano_holds(k9)
    T.on_user_signal(T.CLICK, hwnd=_OTHER_WIN)
    _u0 = L.current_activity(k9).held_until
    clock9.advance(10)
    T._reset_for_test()
    check(T.on_user_signal(T.KEY, hwnd=None) == R.IGNORED_ELSEWHERE,
          "⭐⭐ 落点**未知**的信号**不续期** —— 否则一条 20s 的租约能被无限推后")
    check(L.current_activity(k9).held_until == _u0,
          "⚠️ 期限**一点没动**（不是「少推了一点」）", str(L.current_activity(k9).held_until))
    # 反证：同一时刻换成落点已知，就必须续上
    check(T.on_user_signal(T.KEY, hwnd=_OTHER_WIN) == R.RENEWED
          and L.current_activity(k9).held_until > _u0,
          "反证：落点已知的同一个信号**确实**会续期 —— "
          "上面那条不是把续期整个关掉了")


def t_no_false_positive_in_shadow(tmp: pathlib.Path) -> None:
    """⚠️ 这一组原本测「shadow 对答案不许把设计内的差异记成缺陷」。

    **切写那一步已把对答案（`compare_busy`）整个删掉**，所以那些断言没有被测对象了。
    学到的东西没丢，永久留在早先的设计与 changelog 里：
      📌 **两个寿命不同的东西之间的「分歧」不是缺陷，是设计。**
      📌 **shadow 的对答案，只在两边建模同一个粒度时才有意义** ——
         一旦新实现有意做得比旧的更细/更粗，旧的就不再是 oracle，该退役了。
    ⚠️ 保留这个空壳只为留一条线索：**这里曾经有一组测试，它是被【机制退役】
       而不是被【删需求】拿掉的。** 直接删掉函数会让人以为从来没测过假阳性。
    📌 一个测已退役机制的测试，不再是资产而是残留 —— 它会让人以为那机制还在跑。
    """
    print("\n[5] ⏸ shadow 对答案已随切写那一步移除（原假阳性用例的留痕见下方 docstring）")
    check(True, "⏸ 对答案已删除 —— 判据已留档，不再需要运行时断言")

def t_holder_user_is_first_class(tmp: pathlib.Path) -> None:
    print("\n[6] 用户是合法持有者")
    k, _ = make_kernel(tmp / "f")
    _nano_holds(k)
    T.on_user_signal(T.CLICK)

    check(T.user_holds_machine() is True, "`user_holds_machine()` 说是")
    cur = L.current_activity(k)
    check(cur.reason.startswith("user "),
          "租约上写着是谁、因为什么拿走的 —— 裸 bool 表达不了这个", cur.reason)

    # 互斥仍然成立：用户持有期间不可能再冒出第二条活动租约
    try:
        k.submit(Command(kind=L.ACQUIRE, payload={
            "holder": L.Holder.NANO, "reason": "sneak in"}))
        sneaked = True
    except Exception:
        sneaked = False
    check(sneaked is False,
          "⭐ 用户持有期间 Nano 不加 preempt 抢不进来（互斥对用户一样生效）")


def t_judge_by_target_not_by_kind(tmp: pathlib.Path) -> None:
    """⭐⭐ 实测死锁修复：按「打在谁身上」判，不是按「动作是什么」判。

    第一版按种类判，实测当场死锁：滚 Nano **自己的窗口**（为了看进度）
    → 判成"用户拿走了电脑" → Nano 每步碰壁。而**打字说「继续」又给那条
    阻止它干活的租约续了期** —— 想让它继续就得说话，说话就延长阻塞。
    """
    print("\n[8] ⭐⭐ 按「落在哪个窗口」判，不是按「动作种类」判")
    k, _ = make_kernel(tmp / "h")
    _nano_holds(k)

    import ctypes
    OWN = 0xBEEF
    TARGET = 0xCAFE
    OTHER = 0xF00D

    saved_cls = T._target_class
    try:
        T._target_class = lambda h, lid: {OWN: "own", TARGET: "target",
                                          OTHER: "other"}.get(h, "unknown")

        # ── Nano 自己的窗口：滚轮和按键都不算 ────────────────────────────
        check(T.on_user_signal(T.SCROLL, hwnd=OWN) == R.IGNORED_OWN_UI,
              "⭐⭐ 滚 Nano 自己的窗口 → **不算接管**（用户在看进度）")
        check(T.on_user_signal(T.KEY, hwnd=OWN) == R.IGNORED_OWN_UI,
              "⭐⭐ 在 Nano 窗口里打字 → **不算接管**（用户在跟它说话）")
        cur = L.current_activity(k)
        check(cur is not None and cur.holder == L.Holder.NANO,
              "⚠️⚠️ 租约仍在 Nano 手里 —— **这一条就是死锁的解**："
              "说「继续」不再给阻塞它的租约续期")

        # ── 目标窗口：最该停的一格 ──────────────────────────────────────
        T._reset_for_test()
        check(T.on_user_signal(T.SCROLL, hwnd=TARGET) == R.TAKEN,
              "⭐⭐ 滚**我正在操作的那个**窗口 → **算** —— 它记的坐标全废了")

        k2, _ = make_kernel(tmp / "h2")
        _nano_holds(k2)
        check(T.on_user_signal(T.KEY, hwnd=TARGET) == R.TAKEN,
              "⭐ 在目标窗口里打字 → 算（内容变了）")

        # ── 第三方窗口：滚轮不算、点击算 ────────────────────────────────
        k3, _ = make_kernel(tmp / "h3")
        _nano_holds(k3)
        check(T.on_user_signal(T.SCROLL, hwnd=OTHER) == R.IGNORED_ELSEWHERE,
              "⭐⭐ 滚**别人家**的窗口 → 不算。Win10+ 默认滚非活动窗口，"
              "压根不换焦点，对 Nano 零影响 —— 这才是「滚轮颗粒太粗」的正解")
        check(L.current_activity(k3).holder == L.Holder.NANO, "⚠️ 租约没被动过")

        check(T.on_user_signal(T.CLICK, hwnd=OTHER) == R.TAKEN,
              "⭐ 点**别人家**的窗口 → **算**：抢走了前台，"
              "Nano 若继续就会在用户打字时把焦点抢回来")

        # ── ⭐⭐ 焦点切换是「后果」不是「动作」，不许当接管信号 ──────────
        # 实测探针实测：用户点**一下**记事本产生 **3 条** focus_change，
        # 其中一条落点是 `explorer.exe ''`（空标题的 shell 瞬时窗口）——
        # 没有任何人点过它。而 Nano 自己的 look_at_screen 最小化/还原也会产生同款事件，
        # 在系统层与"用户点了那个窗口"完全无法区分 → 它的截图动作会自己抢走机器。
        k5, _ = make_kernel(tmp / "h5")
        _nano_holds(k5)
        check(T.on_user_signal(T.FOCUS_CHANGE, hwnd=OTHER) == R.IGNORED_NOT_A_TAKEOVER,
              "⭐⭐ 前台易主（哪怕落在第三方窗口）→ **不算接管**")
        check(L.current_activity(k5).holder == L.Holder.NANO,
              "⚠️ 租约没被动过 —— 否则 Nano 一截图就把机器从自己手里抢走")
        check(T.FOCUS_CHANGE in T.CONSEQUENTIAL and T.FOCUS_CHANGE not in T.TAKEOVER_TRIGGERS,
              "⭐ 它**仍是**环境变化（[A3] 第 2 层日志要用），但**不是**接管证据 —— "
              "两张表用途不同，不许合并")
        # 反证：同一个落点换成点击就必须接管，否则上面只证明了"这个窗口豁免"
        check(T.on_user_signal(T.CLICK, hwnd=OTHER) == R.TAKEN,
              "反证：同一个第三方窗口，**点击**照样接管 —— "
              "去掉的是焦点这个信号，不是那个窗口的资格")

        # ── 拿不到落点时保守（宁可停手）──────────────────────────────
        k4, _ = make_kernel(tmp / "h4")
        _nano_holds(k4)
        check(T.on_user_signal(T.SCROLL, hwnd=None) == R.TAKEN,
              "⚠️ 落点未知 → 退回按种类判（保守）。"
              "拿不到落点的原因可能正是「窗口刚被销毁」这类真出事了的情况")
    finally:
        T._target_class = saved_cls


def t_armed_by_gui_mode_not_by_mouse(tmp: pathlib.Path) -> None:
    """⭐⭐⭐ 实测定案的那条：武装条件是「在 GUI 模式」，不是「Nano 握着鼠标」。

    🔴 旧写法的实测后果：用户点 **17 下、跨 17.6 秒**，
       **全部** `ignored_nano_idle`，第 18 下才命中；接管状态条随后 0.8 秒就出来了。
       **19 秒的延迟里 18.8 秒是"接管压根没生效"，只有 0.8 秒是 UI。**
    📌 **「Nano 此刻握着鼠标」≠「Nano 此刻在做 GUI 任务」** ——
       前者瞬时断续（只在键鼠动作那一瞬），后者持续。
       判据的**意图**一直是后者，**实现**成了前者。
    """
    print("\n[11] ⭐⭐⭐ 武装条件 = 在 GUI 模式（不是握着鼠标）")
    OTHER = 0xF00D

    # ① 不在 GUI 模式 —— 用户日常用电脑，一律不管
    k1, _ = make_kernel(tmp / "g1")
    check(L.gui_session_active(k1) is False, "前提：不在 GUI 模式")
    check(T.on_user_signal(T.CLICK, hwnd=OTHER) == R.IGNORED_NANO_IDLE,
          "⭐ 不在 GUI 模式 → 忽略（用户在场是常态，不是异常）")
    check(L.current_activity(k1) is None,
          "⚠️ 没有凭空造租约 —— 否则用户平时点任何东西都在抢机器")

    # ② ⭐⭐ GUI 模式 + Nano **没**握鼠标 —— 就是被漏掉的那 17 次
    k2, _ = make_kernel(tmp / "g2")
    _gui_mode_only(k2)
    check(L.current_activity(k2) is None,
          "前提：在 GUI 模式，但 Nano 此刻**不握鼠标**（在思考/跑命令/等 MCP）")
    check(T.on_user_signal(T.CLICK, hwnd=OTHER) == R.TAKEN,
          "⭐⭐⭐ **第一下就命中** —— 旧写法在这一格连点 17 下都是 ignored_nano_idle")
    _c2 = L.current_activity(k2)
    check(_c2 is not None and _c2.holder == L.Holder.USER,
          "⭐ 用户拿到机器（哪怕 Nano 此刻没握着）—— "
          "因为它**马上就要动手了**，等它动手再拦就晚了")

    # ③ GUI 模式 + Nano 握着鼠标 —— 照旧接管
    k3, _ = make_kernel(tmp / "g3")
    _nano_holds(k3)
    check(T.on_user_signal(T.CLICK, hwnd=OTHER) == R.TAKEN,
          "GUI 模式 + Nano 握着鼠标 → 照旧接管")

    # ④ 反证：GUI 模式关掉之后立刻不再武装
    k4, _ = make_kernel(tmp / "g4")
    _gui_mode_only(k4)
    check(T.on_user_signal(T.CLICK, hwnd=OTHER) == R.TAKEN, "GUI 模式开着 → 接管")
    L.close_gui_session()
    T._reset_for_test()
    check(L.gui_session_active(k4) is False, "mini 窗关 → 退出 GUI 模式")
    k4.submit(Command(kind=L.ACQUIRE, payload={
        "holder": L.Holder.NANO, "reason": "still holding", "ttl_sec": 300.0,
        "preempt": True}))
    check(T.on_user_signal(T.CLICK, hwnd=OTHER) == R.IGNORED_NANO_IDLE,
          "⭐⭐ 反证：**哪怕 Nano 仍握着鼠标**，只要退出 GUI 模式就不再监控 —— "
          "证明武装条件真的换成了 GUI 模式，不是两个条件的或")

    # ⑤ GUI 模式是权威状态，不是 UI 标志
    src = module_text("app")
    ac = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("open_gui_session(" in ac and "close_gui_session(" in ac,
          "⭐ mini 窗的两个边界都接了 GUI 模式租约")
    i_open, i_mini = ac.find("open_gui_session("), ac.find("self._mini_active = True")
    check(0 < i_mini and 0 < i_open and abs(i_open - i_mini) < 600,
          "⚠️ 开 GUI 模式紧挨着 `_mini_active = True` —— 两者必须同生同死",
          f"距 {abs(i_open - i_mini)} 字符")
    tsrc = module_text("core.proactive.takeover")
    tc = "\n".join(l for l in tsrc.splitlines() if not l.strip().startswith("#"))
    check("gui_session_active(" in tc,
          "⭐⭐ 判定层读的是 GUI 模式")
    check("current_activity(kernel)\n        if cur is None:" not in tc,
          "⚠️ 旧的「没握鼠标就忽略」那条已经不在了")


def t_never_breaks_main_flow(tmp: pathlib.Path) -> None:
    """感知层是观察用户的，不是关键路径。"""
    print("\n[7] 感知层不许把主流程搞崩")
    k, _ = make_kernel(tmp / "g")
    _nano_holds(k)          # 前提：Nano 持有，所以「什么都没发生」不能靠没武装来解释

    # ⚠️ 不能用「把内核单例设成 None」来模拟坏掉 —— `get_kernel()` 会当场懒建一个新的，
    #    测出来的是「新内核里没人持有」，跟"读失败"完全是两回事。直接让读租约抛。
    saved = L.current_activity

    def _boom(*a, **kw):
        raise RuntimeError("database is locked")

    try:
        L.current_activity = _boom
        r = T.on_user_signal(T.CLICK)
        check(r == R.ERROR,
              "⭐ 读租约抛异常时返回 ERROR 并安静收场，不把异常甩进钩子线程", r)
        check(T.user_holds_machine() is False,
              "⚠️ 读不到时按「用户没占着」处理 —— 与 `nano_may_touch_os()` "
              "读不到按「Nano 不能动」是同一件事的两面，不能一个宽一个严")
    finally:
        L.current_activity = saved

    check(T.on_user_signal(T.CLICK) in (R.TAKEN, R.RENEWED),
          "反证：内核恢复后同一个信号立刻正常工作 —— "
          "上面那条不是「这个函数永远什么都不做」")


def t_wiring() -> None:
    """AST：常量表和真实分发链必须对得上（不变量式断言）。"""
    print("\n[8] 接线核对（AST，不靠文本匹配）")
    src = module_text("core.proactive.takeover_hooks")
    tree = ast.parse(src)

    # 钩子回调里出现的 takeover.XXX 常量，必须都在 CONSEQUENTIAL 里
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "takeover" and n.attr.isupper()}
    emitted = {a for a in used if a in {"CLICK", "KEY", "FOCUS_CHANGE", "SCROLL", "DRAG", "MOVE"}}
    check(emitted and emitted <= {c.upper() for c in T.CONSEQUENTIAL},
          "⭐ 钩子发出的每一种信号都在 `CONSEQUENTIAL` 表里", str(sorted(emitted)))
    check("MOVE" not in emitted,
          "⭐⭐ 钩子**根本不发** MOVE —— 判定表里排除它还不够，"
          "传感器层就不该产生它（省掉每秒几百次无用调用）")

    # injected 必须真的被传下去，而不是收了不用
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_emit"]
    check(len(calls) >= 3, f"三种信号都有发出点", str(len(calls)))
    check(all(len(c.args) >= 3 for c in calls),
          "⚠️ 每个 `_emit` 都带 injected 参数 —— 少传一个就是一路漏过滤",
          str([len(c.args) for c in calls]))
    # ⭐ 鼠标类必须**额外**带落点：判定要按"打在哪个窗口"判（见判据 ⓪）。
    #   ⚠️ 落点必须是**事件自带的**坐标 —— 工作线程里读"当前光标位置"时
    #      光标早移开了，解出来会是另一个窗口。
    _mouse_emits = [c for c in calls if len(c.args) >= 4]
    check(len(_mouse_emits) >= 1,
          "⭐⭐ 鼠标类 `_emit` 带第 4 个参数（落点）—— 死锁修复的接线点",
          f"{len(_mouse_emits)}/{len(calls)}")
    check(any("st.pt" in (ast.get_source_segment(src, c) or "") for c in _mouse_emits),
          "⭐⭐ 落点取自**事件结构体**（`st.pt`），不是当前光标位置")
    wsrc = ast.get_source_segment(src, next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_worker_loop")) or ""
    check("hwnd=hwnd" in wsrc,
          "⭐ 工作线程把落点解析成 hwnd 后传给判定层")
    check("GetForegroundWindow" in wsrc,
          "⚠️ 键盘类没有坐标，用前台窗口当落点（键盘事件本来就发给前台）")

    # 判定层：injected 必须是第一道闸（在读内核之前）
    # ⚠️ 2026-08-07 起判定逻辑在 `_on_user_signal_impl` 里，`on_user_signal` 只是
    #    「调判定 + 记诊断留痕」的薄壳。拆开的理由：**每一条信号（包括被忽略的）
    #    都必须留痕** —— 只记成功路径的日志没法回答"为什么它没发生"。
    tsrc = module_text("core.proactive.takeover")
    ttree = ast.parse(tsrc)
    fn = next(n for n in ast.walk(ttree)
              if isinstance(n, ast.FunctionDef) and n.name == "_on_user_signal_impl")
    first_ifs = [s for s in fn.body if isinstance(s, ast.If)][:2]
    check(any(isinstance(s.test, ast.Name) and s.test.id == "injected" for s in first_ifs),
          "⭐ `injected` 是判定函数里的第一道闸 —— 排在任何数据库读取之前")
    # ⭐ 而且薄壳必须**无条件**留痕，不能只在某些分支记
    shell = next(n for n in ast.walk(ttree)
                 if isinstance(n, ast.FunctionDef) and n.name == "on_user_signal")
    check(not [s for s in shell.body if isinstance(s, ast.If)],
          "⭐⭐ 留痕的那层**没有任何分支** —— 每一条信号都留痕，"
          "包括被忽略的。只记成功路径的日志答不了「为什么它没发生」")

    # 启动接线
    hsrc = module_text("core.proactive.hooks")
    htree = ast.parse(hsrc)
    start = next(n for n in ast.walk(htree)
                 if isinstance(n, ast.FunctionDef) and n.name == "start_hooks")
    names = {n.func.id for n in ast.walk(start)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("start_takeover_hooks" in names,
          "⭐ `start_hooks()` 里真的调了 `start_takeover_hooks()` —— "
          "`reconcile_tick` 当初就是漏了这一步，整层白写")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = pathlib.Path(d)
        t_consequential_table(tmp)
        t_injected_filter(tmp)
        t_only_armed_when_nano_holds(tmp)
        t_takeover_and_renew(tmp)
        t_no_false_positive_in_shadow(tmp)
        t_holder_user_is_first_class(tmp)
        t_judge_by_target_not_by_kind(tmp)
        t_armed_by_gui_mode_not_by_mouse(tmp)
        t_never_breaks_main_flow(tmp)
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
