# core/proactive/takeover.py
"""被动挂起 · 感知层 —— 把「用户有后果的动作」翻译成一条 `Holder.USER` 活动租约。

这里只做**判定 + 接线**，钩子的安装在 `hooks.py`。

═══════════════════════════════════════════════════════════════════════════
为什么是"用户持有租约"，而不是"给 Nano 发一个暂停信号"
═══════════════════════════════════════════════════════════════════════════
硬要求是"这个过程一定要瞬发，不然毫无意义"。
"发信号让 Nano 停"做不到瞬发 —— 信号要有人收、有人判、有人传到执行器。
**让用户直接持有那把锁**才是瞬发的：Nano 下一步想动手时自己拿不到，
不需要任何人通知它。租约本来就是互斥的，这件事不用另外实现。

⭐ 所以被动挂起不是"打断 Nano"，是**用户把这台电脑拿回去了**。

═══════════════════════════════════════════════════════════════════════════
三条判据（都是事先定好的，不是这里现编的）
═══════════════════════════════════════════════════════════════════════════
⓪ **⭐⭐「有后果」不是信号种类的属性，是「信号落在哪个窗口」的属性。**（2026-08-07 追加）

   第一版只按**动作是什么**判，实际运行当场死锁：用户滚 Nano **自己的窗口**
   （为了看它做到哪了）→ 判成"用户拿走了电脑" → Nano 每一步都碰壁。
   更糟的是**打字跟 Nano 说"继续"，那次按键又给那条阻止它干活的租约续了期** ——
   想让它继续就得说话，说话就把它继续不了的原因延长 20 秒。**结构性死锁。**

   正确的判据要看**打在谁身上**：

   | 落在哪 | 滚轮 | 点击 / 按键 |
   |---|---|---|
   | **Nano 自己的窗口** | 不算 | **不算** —— 用户在**跟它说话**，不是拿走电脑 |
   | **Nano 的目标窗口**（`window_binding`）| **算** | **算** |
   | 第三方窗口 | **不算** | 算 |

   逐格理由：
   * **Nano 自己的窗口**：它不属于 Nano 正在自动化的那个环境。
     ⚠️ 唯一的实际后果是"前台被抢走了"，而那件事**已经由窗口绑定兜住** ——
     `_focus_target_window()` 会把绑定的目标提回前台。**能自己修的事不该停机。**
   * **目标窗口**：这是**最该停**的一格。用户在动 Nano 正在操作的东西 ——
     滚一下它记的坐标就全废了，打个字内容就变了。
   * **第三方窗口 + 滚轮**：滚别的窗口不影响 Nano 的目标；而且 Win10+ 默认
     "滚动非活动窗口"，滚轮**压根不换焦点**，对 Nano 零影响。
     ⭐ 这正好回答"滚轮=打断颗粒太粗"那个疑问 ——
     粗的不是滚轮，是**按种类判而不是按对象判**。
   * **第三方窗口 + 点击/按键**：抢走了前台。Nano 若继续就会 `SetForegroundWindow`
     把焦点抢回来 —— **在用户正打字的时候**。这才是被动挂起本来要防的那件事。

① **只认"有后果的动作"，不认"有活动的迹象"。**
   点击 / 按键 / 前台窗口切换 / 拖拽 算；**纯鼠标移动不算**。
   理由（已定）：光标动了但什么都没点，窗口层级、焦点、控件状态一个都没变，
   Nano 的坐标和目标全部仍然有效 —— 为一件不产生后果的事挂起纯粹是自找的抖动。
   ⚠️ 顺带说明"点击途中用户也在动鼠标"那个真实风险为什么**不归这里管**：
   它早就在更低的层解决了（`executor_action._SendInputClick` 用原子 SendInput
   提交整个 move+down+up，Windows 保证不被插队），当年还**明确否决过**
   "检测用户是否在动鼠标再决定要不要中止"这个解法。别在这里重新实现一遍。

② **防抖 = 租约的 TTL，不是另一个计时器。**
   用户每做一个有后果的动作就把租约续到 `now + USER_HOLD_SEC`。
   于是那个鬼畜场景（动一下停一下再动一下 → 挂起-恢复反复横跳）
   自动没了：中间那些动作只是**续期**，不产生任何状态翻转。
   "用户停下来了"的定义因此就是"租约到期"，不需要单独维护。

③ **传感器只在 Nano 持有机器时武装。**
   用户在场是 Nano 的**常态**，不是异常。Nano 没在操作电脑的时候，
   用户点什么都与它无关 —— 那时候记租约既无意义又会让 Nano 之后拿不到锁
   （用户随手一点就锁住 20 秒，Nano 变成"想干活先等你消停"，荒谬）。

═══════════════════════════════════════════════════════════════════════════
接管之后 Nano 在哪两处碰壁（2026-08-07 已接线）
═══════════════════════════════════════════════════════════════════════════
① **开工前**：`os_execute` 拿不到活动租约 → 一步都不做就让位。
   ⭐ 这道闸比第二道好，因为**没有"做了一半"的现场**要模型去猜。
② **跑到一半**：`dispatch.execute()` 对鼠标键盘动作查 `nano_may_touch_os()`。
   多步计划中途被接管只能一步一步碰壁，这道闸兜住那种情况。
   ⚠️⚠️ **这里原先写着「只拦鼠标键盘，不拦截图/读窗口」—— 已作废两次：**
   · 范围：作用域已改成「在 GUI 模式就全量监控」，不再按动作种类分。
   · 论据：原来的理由是「恢复后判断环境变没变恰恰需要能看」——
     那是**闸时代**的产物。出口是「失败」时拦截图 = 永久拿不到那张图；
     出口改成「等待」后，拦 = **稍后再做**，担心自动消失。
     📌 **机制的出口从「失败」变成「等待」时，一大批「不能拦 X」的论据会自动失效。**
     而更直接的一点是：**两个时刻压根不可能重合** ——
     「为判断环境而截图」在挂起**结束之后**，拦截在用户**正在动**的时候。
   ⭐ 截图现在也等，真实理由是另一条：`_look_at_screen` 会最小化/还原自己的窗口
     来避开遮挡 —— 那是**抢焦点**，它确实在跟用户争。

⚠️ 两道闸都**只在有别的持有者时**才拒绝。租约系统整个挂了 → 没人持有 → 放行，
   最坏退化成"没有被动挂起"，而不是把 OS 能力锁死。

三层防护网都已落地：
   · 第 1 层：提示词写明挂起前后的环境不能默认一致（见 `orchestrator` 的挂起提示）。
   · 第 2 层：接管期间记一次性证据日志（`takeover_log.py`）。
   · 第 3 层：恢复时把那份日志**挂在工具结果前面**交给模型，让它先读再决定
     要不要重新截图（见 `orchestrator` 里取用 `_a3_pause_note` 那一处）。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from loguru import logger

from core.proactive import takeover_log

# ── 判定表 ────────────────────────────────────────────────────────────────

CLICK = "click"                  # 左/中/右键按下
KEY = "key"                      # 实体按键
FOCUS_CHANGE = "focus_change"    # 前台窗口易主
SCROLL = "scroll"                # 滚轮
DRAG = "drag"                    # 按住键的移动（= 有后果的移动）
MOVE = "move"                    # ⚠️ 刻意列出来但**不在**下面那张表里

#: 会改变环境、因而值得让 Nano 停手的动作。**改这张表前先回读上面判据 ①。**
#:
#: ⭐ `SCROLL` 是原清单（点击/键盘/焦点切换/最小化-还原/拖拽）之外补的一条，
#:    理由与判据 ① 完全同源：**滚一下，页面上每个元素的坐标就全变了** ——
#:    Nano 刚定位好的那个按钮不在原地了，这是货真价实的"有后果"。
#:    它恰好也是"移动 vs 滚动"分界的最好例子：光标划过去什么都没变，滚轮转一下全变了。
#:
#: 📌 `DRAG` 不需要单独的传感器：拖拽必然以一次按下开始，那一下已经算 `CLICK` 了。
#:    列在这里是为了让判定表读起来和原清单的措辞对得上，以及留给未来真要区分时用。
CONSEQUENTIAL = frozenset({CLICK, KEY, FOCUS_CHANGE, SCROLL, DRAG})

#: ⭐⭐ **能证明「用户接管了这台电脑」的信号 —— 比 `CONSEQUENTIAL` 窄。**
#:
#: `FOCUS_CHANGE` 刻意**不在**这里。2026-08-07 实测探针的结果：
#:   · 点**一下**记事本 → 产生 **3 条** focus_change，
#:     其中一条落点是 `explorer.exe ''`（**空标题的 shell 瞬时窗口**）——
#:     那不是任何人点的东西。每一条都会抢/续租约。
#:   · 而 Nano 自己的 `look_at_screen`（最小化/还原自身）和 `set_window_mode`
#:     **都会把前台交给一个非 Nano 的窗口** —— 在系统层与"用户点了那个窗口"
#:     **完全无法区分**。于是 Nano 的截图动作会把机器从自己手里抢走。
#:
#: 📌 **判据：焦点切换是「后果」，不是「动作」。**
#:    用户接管由**硬件输入**证明（点击/按键/滚轮，`injected=False` = 真实人手）；
#:    焦点变了只证明**有东西**动了窗口，而 Nano 自己就是最频繁的那个。
#:    这与另一条同源：**不能把程序抢焦点归因成用户动手** ——
#:    那条约束当初记下了，却在第一版实现里让它成了触发源。
#:
#: ⚠️ **覆盖面不会因此变窄**：Alt+Tab 是按键，点别的窗口是点击，两条都还在。
#: ⚠️ `FOCUS_CHANGE` 仍留在 `CONSEQUENTIAL` 里 —— 它是**环境变化**的一部分，
#:    第 2 层的挂起期日志需要它（"焦点去了哪儿"是还原用户行为的主要线索）。
#:    **两张表的用途不同，不许合并。**
TAKEOVER_TRIGGERS = frozenset({CLICK, KEY, SCROLL, DRAG})

#: 用户停手多久之后 Nano 才能把机器拿回去。**从最后一次动手起算。**
#:
#: ⚠️⚠️ **这 12 秒只是第一次体感调整，还没最终确定**（2026-08-26）：
#:    「20 绝对是过长，但是 12 是否舒适我并不确定」。
#:    ⇒ 下一个人看到这个数时**不要当成已标定的结论**；它还欠一次真实使用后的复核。
#:
#: 🔴 **20 的来源是一个错误的类比**（原注释逐字保留在下面）：
#:      「参考 `ActivityBuffer._TYPING_GAP = 30s`（同类参数），这里取小一些」
#:    而那个 30 答的是**「一段输入算不算同一次」**，这个答的是
#:    **「让 Nano 干等多久」** —— 📌 **两个问题不同，一个的答案推不出另一个的。**
#:    实际体感反馈：「20 秒内大部分时间都在傻等着」。
#: ⭐ 12 不是又一次猜测，是**用真实体感替掉那个类比**；本来想砍得更狠，
#:    但**越小风险越高**（Nano 可能在用户还没真停手时抢回鼠标），所以先走一档。
#:
#: ⚠️ **它是纯等待，没有任何缓冲叠加在上面**（2026-08-26 核实）：
#:      `_COALESCE_SEC = 0.5`   只是 0.5s 内不重复提交内核命令（省 IO），
#:                              **不影响到期时间**
#:      `app._TAKEOVER_SETTLE_SEC = 2.0`  在这 12 秒**里面**，只决定接管状态条
#:                              什么时候从静态文案切成倒计时（0~2s 静态 → 之后倒数）
#:    ⇒ 用户实际等的就是 12 秒整。
#: ⚠️ 调这个数时**不要顺手也调那 2 秒**：它答的是「人打字的自然间隙有多长」，
#:    而那个事实没变。📌 **一个参数变了，不代表跟它有关的参数都要跟着变 ——
#:    要问的是「它答的那个问题变没变」。**
USER_HOLD_SEC = 12.0


class TakeoverResult:
    """`on_user_signal` 的返回值。**每一种都要能被测到**，所以不用裸字符串常量散落各处。"""
    IGNORED_NOT_CONSEQUENTIAL = "ignored_not_consequential"
    IGNORED_NANO_IDLE = "ignored_nano_idle"
    IGNORED_SELF_INPUT = "ignored_self_input"
    IGNORED_OWN_UI = "ignored_own_ui"            # 落在 Nano 自己的窗口上
    IGNORED_ELSEWHERE = "ignored_elsewhere"      # 滚轮落在与 Nano 无关的窗口上
    IGNORED_NOT_A_TAKEOVER = "ignored_not_a_takeover"   # 是环境变化，但不证明用户动手
    TAKEN = "taken"
    RENEWED = "renewed"
    ERROR = "error"


def _describe_hwnd(hwnd: Optional[int]) -> str:
    """`hwnd=… pid=… proc 标题` —— 只给日志和租约 reason 用，取不到就返回空串。

    ⚠️ 用 `c_void_p` 包 hwnd：不包的话大句柄会抛 `OverflowError`，
    而这里外面有 `except`，那个窗口就会**悄悄变成"未知落点"** ——
    诊断路径上的静默失败最难查（同一个坑在 `takeover_hooks` 踩过一次）。
    """
    if not hwnd:
        return ""
    try:
        import ctypes
        u = ctypes.windll.user32
        h = ctypes.c_void_p(int(hwnd))
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        n = u.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        proc = ""
        try:
            import psutil
            proc = psutil.Process(pid.value).name()
        except Exception:
            pass
        return f"hwnd={int(hwnd)} pid={pid.value} {proc} {(b.value or '')[:30]!r}"
    except Exception:
        return f"hwnd={hwnd}"


def _target_class(hwnd: Optional[int], lease_id: str) -> str:
    """这个 hwnd 属于哪一格：`own` / `target` / `other` / `unknown`。

    ⚠️ **`unknown` 必须按 `other` 之外的方式处理**：拿不到落点时，
    我们不知道用户打在哪 —— 那就退回"按种类判"的老行为（保守，宁可停手），
    因为拿不到落点的原因可能正是"窗口刚被销毁"这类真的出事了的情况。
    """
    if not hwnd:
        return "unknown"
    try:
        import ctypes
        u = ctypes.windll.user32
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        from core.self_identity import is_self_pid
        if is_self_pid(int(pid.value)):
            return "own"
    except Exception:
        return "unknown"
    try:
        from core.os_layer import window_binding as _wb
        b = _wb.bound(lease_id)
        if b and int(b["hwnd"]) == int(hwnd):
            return "target"
    except Exception:
        pass
    return "other"


# 内核提交不是免费的；用户按住一个键会连发。同一种信号在这个窗口内只提交一次。
# ⚠️ 这**不是**防抖（防抖是 TTL，见判据 ②），这只是省 IO —— 两者不要混。
_COALESCE_SEC = 0.5
_lock = threading.Lock()
_last_submit_at: float = 0.0

# Nano 有没有正在进行的一轮（由 orchestrator 在每轮开始 / 结束时设置）。
# GUI 任务可以跨多轮；两轮之间 Nano 不动，用户用电脑不算接管。
_turn_active = threading.Event()


def set_turn_active(on: bool) -> None:
    if on:
        _turn_active.set()
    else:
        _turn_active.clear()


def turn_active() -> bool:
    return _turn_active.is_set()


def _reset_for_test() -> None:
    """测试用：清掉合并窗口。生产不调。"""
    global _last_submit_at
    with _lock:
        _last_submit_at = 0.0


def on_user_signal(kind: str, detail: str = "", *, injected: bool = False,
                   hwnd: Optional[int] = None, now: Optional[float] = None) -> str:
    """收到一个用户输入信号。返回 `TakeoverResult` 之一。

    :param injected: 这个事件是不是**程序合成的**（Windows 的 `LLKHF_INJECTED`）。
        ⭐ 这个参数是本模块最关键的一处：**Nano 自己的 SendInput 也会被钩子看见**。
        不过滤的话，Nano 打一个字就把自己的租约抢走，下一步立刻碰壁 —— 自锁死。
        📌 特意**没有**用"Nano 动手前后设一个自抑制标志"那种写法：
        那正是 `_os_task_busy` 栽过的形状（靠所有调用点都记得配对），
        新增一个发送输入的地方就会静默破掉。**让操作系统告诉我们是谁发的**，
        新增调用点也不会漏。
    """
    global _last_submit_at
    _r = _on_user_signal_impl(kind, detail, injected=injected, hwnd=hwnd, now=now)
    _trace(kind, injected, hwnd, _r)
    _feed_log(kind, injected, hwnd, _r)
    return _r


def _hwnd_facts(hwnd) -> tuple:
    """`(title, proc)` —— 取不到就空串。⚠️ 与 `_describe_hwnd` 同样用 `c_void_p`
    包句柄（大句柄不包会抛 `OverflowError` 并被外层 `except` 悄悄吞掉）。"""
    if not hwnd:
        return "", ""
    try:
        import ctypes
        u = ctypes.windll.user32
        h = ctypes.c_void_p(int(hwnd))
        n = u.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        proc = ""
        try:
            import psutil
            proc = psutil.Process(pid.value).name()
        except Exception:
            pass
        return (b.value or "")[:40], proc
    except Exception:
        return "", ""


def _feed_log(kind: str, injected: bool, hwnd, verdict: str) -> None:
    """把这个信号喂给挂起期证据日志。

    ⚠️⚠️ **这里记的范围比「接管判定」宽，而且是故意的**：
    判定问的是"用户是不是在接管"（`TAKEOVER_TRIGGERS`，不含焦点切换）；
    日志问的是"环境发生了什么变化"（`CONSEQUENTIAL`，**含**焦点切换）。
    📌 两张表本来就是为两个不同问题定义的 —— 这正是本轮栽过六次的那个形状
    （拿一个为别的目的定义的近似物去回答另一个问题）。这里**分开用**。

    ⚠️ `injected`（Nano 自己发的）不记：这份日志答的是"**用户**做了什么"。
    """
    try:
        if injected or kind not in CONSEQUENTIAL:
            return
        # ⭐ 起录时刻挂在「瞬发 / 状态条出现」那一刻，不挂在挂起真正生效时 ——
        #    Nano 可能还在收手，而用户在那段时间的操作恰好可能破坏那个动作。
        #    📌 **证据窗口要覆盖「可能出事的那一刻」，不是「我开始处理的那一刻」。**
        if verdict in (TakeoverResult.TAKEN, TakeoverResult.RENEWED):
            tgt_h, tgt_t = 0, ""
            try:
                from core.os_layer import window_binding
                b = window_binding.snapshot_bound_for_log()
                if b:
                    tgt_h, tgt_t = b
            except Exception:
                pass
            takeover_log.start(tgt_h, tgt_t)
        if not takeover_log.is_recording():
            return
        title, proc = _hwnd_facts(hwnd)
        takeover_log.note("focus" if kind == FOCUS_CHANGE else "input",
                          hwnd=hwnd or 0, title=title, proc=proc, detail=kind)
    except Exception:
        pass


# ── 诊断留痕 ──────────────────────────────────────────────────────────────
# ⚠️⚠️ **这一段是因为「狂点二十下、日志里什么都没有」才加的。**
#
# 原来只有 TAKEN / RENEWED 打日志，**被忽略的信号一条都不留痕**。于是
# "状态条不出现"这一个症状对应三种完全不同的原因，而当时一种都区分不了：
#   ① 钩子压根没收到（低级钩子被 Windows 静默摘掉 —— 见 takeover_hooks 模块头）
#   ② 收到了但被判成忽略（own？unknown？）
#   ③ 判成接管了，但状态条没重画（事件循环被堵住）
#
# 📌 **判据：一个只记「成功路径」的日志，没法回答「为什么没发生」。**
#    而"为什么没发生"恰恰是最难靠人眼观察的那一类问题 ——
#    用户能描述"它没出现"，但描述不出"它在哪一步没出现"。
# 📌 **不要让用户去描述观察不到的东西，让机器把它记下来。**
#
# ⚠️ 点击/焦点**每条都记**（频率低）；按键/滚轮会连发，**按秒聚合**免得淹掉 cmd。
_trace_lock = threading.Lock()
_trace_bucket: dict = {}
_trace_flushed_at: float = 0.0
_TRACE_FLUSH_SEC = 3.0


def _trace(kind: str, injected: bool, hwnd, verdict: str) -> None:
    global _trace_flushed_at
    try:
        if kind in (CLICK, FOCUS_CHANGE):
            logger.debug(f"[Takeover-Trace] {kind} injected={injected} "
                        f"verdict={verdict} 落点={_describe_hwnd(hwnd) or '未知'}")
            return
        now = time.time()
        with _trace_lock:
            key = (kind, injected, verdict)
            _trace_bucket[key] = _trace_bucket.get(key, 0) + 1
            if now - _trace_flushed_at < _TRACE_FLUSH_SEC:
                return
            _trace_flushed_at = now
            snap = dict(_trace_bucket)
            _trace_bucket.clear()
        if snap:
            parts = [f"{k[0]}/{'inj' if k[1] else 'real'}→{k[2]}×{v}"
                     for k, v in sorted(snap.items(), key=lambda x: str(x[0]))]
            logger.debug(f"[Takeover-Trace] 近 {_TRACE_FLUSH_SEC:.0f}s 聚合: "
                        + "  ".join(parts))
    except Exception:
        pass


def _on_user_signal_impl(kind: str, detail: str = "", *, injected: bool = False,
                         hwnd: Optional[int] = None,
                         now: Optional[float] = None) -> str:
    global _last_submit_at
    if injected:
        return TakeoverResult.IGNORED_SELF_INPUT
    if kind not in CONSEQUENTIAL:
        return TakeoverResult.IGNORED_NOT_CONSEQUENTIAL
    # ⭐⭐ 环境变化 ≠ 用户接管。`FOCUS_CHANGE` 属前者不属后者 ——
    #    理由与实测数据见 `TAKEOVER_TRIGGERS` 上方那段。
    if kind not in TAKEOVER_TRIGGERS:
        return TakeoverResult.IGNORED_NOT_A_TAKEOVER

    t = time.time() if now is None else now
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import oslease

        kernel = get_kernel()

        # ⭐⭐⭐ 判据 ③（2026-08-07 重写）：武装条件是「**在 GUI 模式**」，
        #     不是「Nano 此刻握着鼠标」。
        #
        # 🔴 旧写法是 `current_activity() is None → 忽略`。实测（cmd_log/44）灾难性：
        #    点 17 下、跨 17.6 秒，**全部** `ignored_nano_idle`，第 18 下才命中。
        #    因为活动租约只在**键鼠动作那一瞬**才被持有，而一个 GUI 任务里
        #    大部分时间在思考 / 截图 / 跑命令 —— 武装窗口只占一小片，命中纯靠运气。
        #    这就是那句"完全找不到规律"的来源。
        #
        # 📌📌 **当初把「Nano 此刻握着鼠标」当成了「Nano 此刻在做 GUI 任务」。
        #    前者瞬时断续，后者持续。** 判据 ③ 的**意图**一直是后者
        #    （"用户在场是常态，Nano 闲着时别锁"），实现成了前者。
        #
        # ⭐ 现在挂在 `gui_session` 上（= mini 窗开着，这是定下的作用域）：
        #    只要在 GUI 模式，**Nano 在做什么都不重要** —— 命令行、MCP、skill、
        #    键鼠，一律监控。**同时也不用再判断动作种类了**，复杂度反而降了。
        if not oslease.gui_session_active(kernel):
            return TakeoverResult.IGNORED_NANO_IDLE
        # 并且要有正在进行的一轮：GUI 任务跨轮，两轮之间 Nano 不动，用户用电脑不算接管
        # （否则下一条消息也得等完用户持有的那段倒计时）。
        if not _turn_active.is_set():
            return TakeoverResult.IGNORED_NANO_IDLE

        cur = oslease.current_activity(kernel)

        # ⭐⭐ 判据 ⓪：按**落在哪个窗口**判，不是按动作种类判。见模块头那张表。
        #    ⚠️ 这一格要在"续期"之前判：否则用户跟 Nano 说话会**给那条阻止它干活的
        #    租约续期**，形成"想让它继续就得说话、说话就延长阻塞"的死锁（实际撞过）。
        # ⚠️ `cur` 现在**可以是 None** —— GUI 模式中、Nano 正好在两个动作之间
        #    （思考 / 跑命令 / 等 MCP）。那正是最需要被覆盖的时段，
        #    也正是旧写法漏掉的 17 次点击所在的时段。
        _cls = _target_class(hwnd, cur.lease_id if cur else "")
        if _cls == "own":
            # 用户在跟 Nano 说话，不是把电脑拿回去。
            # 前台被抢走这件事由窗口绑定兜（`_focus_target_window` 会提回来）——
            # 📌 **能自己修的事不该停机。**
            return TakeoverResult.IGNORED_OWN_UI
        if _cls == "other" and kind == SCROLL:
            # 滚别的窗口不影响 Nano 的目标；Win10+ 默认"滚动非活动窗口"，
            # 滚轮压根不换焦点。⭐ 这就是"滚轮颗粒太粗"那个疑问的真正答案。
            return TakeoverResult.IGNORED_ELSEWHERE

        if cur is not None and cur.holder == oslease.Holder.USER:
            # ⚠️⚠️ **续期不许"不确定就续"。**（2026-08-07 实测后修）
            #
            # 原来落点解析失败（`_cls == "unknown"`）会掉到这里 → 续期。
            # 于是**任何解析不出落点的信号都把租约再推 20 秒，连滚轮也算**。
            # 实测后果：19:34:08 接管的一条 20s 租约，到 19:34:49 仍"未过期" ——
            # 用户明明停手了，状态条不消失、Nano 一直拿不到机器。
            #
            # 📌 判据：**首次接管保守是对的，续期保守是错的。**
            #    首次"不确定就停手"最多让 Nano 等 20 秒；
            #    续期"不确定就再锁 20 秒"会**无限自我延长** ——
            #    同一个"宁可保守"的直觉，在两个位置上后果完全相反。
            if _cls == "unknown":
                return TakeoverResult.IGNORED_ELSEWHERE
            # 判据 ②：续期即防抖。合并窗口只是省 IO。
            with _lock:
                if t - _last_submit_at < _COALESCE_SEC:
                    return TakeoverResult.RENEWED
                _last_submit_at = t
            kernel.submit(oslease.Command(kind=oslease.HEARTBEAT, payload={
                "lease_id": cur.lease_id, "fence": cur.fence,
                "ttl_sec": USER_HOLD_SEC}))
            # ⭐ 续期也要留痕。上一版只记首次接管，于是"到底谁在续期"查不出来 ——
            #    而那恰恰是"为什么一直锁着"唯一需要的证据。
            #    📌 **卡住的原因通常在续期路径上，不在触发路径上。**
            logger.info(f"[Takeover] 续期（{kind}，落点 {_describe_hwnd(hwnd) or '未知'}，"
                        f"分类={_cls}）→ USER 再持有 {USER_HOLD_SEC:.0f}s")
            return TakeoverResult.RENEWED

        if cur is not None and cur.holder != oslease.Holder.NANO:
            # 机器在第三方手里（既不是 Nano 也不是 USER），不该由用户信号去动它
            return TakeoverResult.IGNORED_NANO_IDLE
        # 到这里只剩两种：`cur is None`（Nano 在动作之间）或 `cur.holder == NANO`。
        # ⭐ 两种都要接管 —— 因为**在 GUI 模式里，"Nano 此刻没握着鼠标"
        #   不等于"用户可以随便动"**：它马上就要动手了。
        #   这一格就是那 17 次被漏掉的点击。

        with _lock:
            _last_submit_at = t
        # ⭐ 把**落点**一起记下来。cmd_log/38 那次接管只记了 "user click"，
        #    结果"到底点在哪个窗口"查不出来 —— 而那正是唯一需要的证据。
        #    📌 判据：**诊断信息要在事发那一刻记，不能等出问题了再回来加。**
        #       主流程加了一堆"把事实摆到模型眼前"，却漏了诊断路径本身。
        _where = _describe_hwnd(hwnd)
        kernel.submit(oslease.Command(kind=oslease.ACQUIRE, payload={
            "holder": oslease.Holder.USER,
            "ttl_sec": USER_HOLD_SEC,
            "reason": f"user {kind}" + (f" @ {_where}" if _where else "")
                      + (f": {detail}" if detail else ""),
            # 用户当然可以从 Nano 手里把机器拿走 —— 这不是异常，是这台电脑的主人回来了
            "preempt": True,
            "preempt_reason": f"user took the machine back ({kind} @ {_where or '?'})",
        }))
        logger.info(f"[Takeover] 用户接管（{kind}，落点 {_where or '未知'}，"
                    f"分类={_cls}）→ 机器交给 USER {USER_HOLD_SEC:.0f}s")
        return TakeoverResult.TAKEN

    except Exception as e:
        # ⚠️ 感知层永远不许把主流程搞崩 —— 它是"观察用户"的，不是关键路径。
        logger.debug(f"[Takeover] 处理用户信号失败（忽略）: {e}")
        return TakeoverResult.ERROR


def user_holds_machine(now: Optional[float] = None) -> bool:
    """现在是不是用户占着机器。**读失败按"没占着"处理** —— 与
    `nano_may_touch_os()` 的 fail-safe 方向一致（那边失败按"Nano 不能动"），
    两者说的是同一件事的两面，不能一个宽一个严。"""
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import oslease
        cur = oslease.current_activity(get_kernel(), now)
        return cur is not None and cur.holder == oslease.Holder.USER
    except Exception:
        return False
