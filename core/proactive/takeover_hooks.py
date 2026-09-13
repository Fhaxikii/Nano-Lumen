# core/proactive/takeover_hooks.py
"""被动挂起 · 传感器 —— Windows 低级钩子，只喂 `takeover.on_user_signal`。

判定逻辑全在 `takeover.py`；这里只负责"把事件从操作系统里拿出来"，
所以这个文件几乎不可单元测试（要真的按键盘），判定那边才是测试的落点。

═══════════════════════════════════════════════════════════════════════════
为什么不复用 `hooks.py` 里现成的 `keyboard` 库钩子和 2 秒窗口轮询
═══════════════════════════════════════════════════════════════════════════
**① `keyboard` 库拿不到"这个事件是不是程序合成的"。**
   Windows 在 `KBDLLHOOKSTRUCT.flags` 里给了 `LLKHF_INJECTED`，
   但 `keyboard` 的 `KeyboardEvent` 没有透出来。
   ⭐ 这一位是本模块存在的**首要理由**：Nano 自己用 SendInput 打的字
   也会走同一个钩子 —— 不过滤就是"Nano 一打字就把自己的锁抢走"，当场自锁死。

**② 2 秒轮询达不到"瞬发"。**（这条事先就点名了）
   前台窗口易主改用 `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`，事件驱动、毫秒级。
   ⚠️ `hooks.py` 那个轮询**不删** —— 它喂的是 `ActivityBuffer`（十分钟环形缓冲，
   要的是"这段时间用户在干嘛"，2 秒粒度足够），和这里要的东西不是一回事。

═══════════════════════════════════════════════════════════════════════════
⚠️ 低级钩子的两条硬约束（写错了会静默失效，不报错）
═══════════════════════════════════════════════════════════════════════════
**① 回调必须极快。** Windows 有 `LowLevelHooksTimeout`（默认 300ms），
   超时**直接把钩子摘掉**且不通知你 —— 表现就是"用了一会儿就不灵了"。
   所以回调里只做 `put_nowait`，判定和数据库全部甩给工作线程。
**② 装钩子的线程必须跑消息循环**（`GetMessage`），否则回调永远不会被调用。
   `SetWinEventHook` 的 `WINEVENT_OUTOFCONTEXT` 同样依赖它，所以两者共用一个线程。
📌 还有一条 Python 特有的：`WINFUNCTYPE` 包出来的回调**必须自己持有引用**，
   被 GC 掉之后 Windows 回调到一段释放了的内存 —— 进程直接崩。下面用模块级变量兜住。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time

from loguru import logger

from core.proactive import takeover
from core.proactive import takeover_log

# ── Win32 常量 ────────────────────────────────────────────────────────────

_WH_KEYBOARD_LL = 13
_WH_MOUSE_LL = 14

_WM_KEYDOWN = 0x0100
_WM_SYSKEYDOWN = 0x0104
_WM_LBUTTONDOWN = 0x0201
_WM_RBUTTONDOWN = 0x0204
_WM_MBUTTONDOWN = 0x0207
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEHWHEEL = 0x020E

_LLKHF_INJECTED = 0x10
_LLMHF_INJECTED = 0x01

_EVENT_SYSTEM_FOREGROUND = 0x0003
# 窗口生命周期 —— 只喂挂起期日志，不参与接管判定。
_EVENT_SYSTEM_MINIMIZESTART = 0x0016
_EVENT_SYSTEM_MINIMIZEEND = 0x0017
_EVENT_OBJECT_CREATE = 0x8000
_EVENT_OBJECT_DESTROY = 0x8001
_EVENT_OBJECT_LOCATIONCHANGE = 0x800B
_WINEVENT_OUTOFCONTEXT = 0x0000

_CLICKS = frozenset({_WM_LBUTTONDOWN, _WM_RBUTTONDOWN, _WM_MBUTTONDOWN})
_SCROLLS = frozenset({_WM_MOUSEWHEEL, _WM_MOUSEHWHEEL})


# ── ctypes 原型 ───────────────────────────────────────────────────────────
# ⚠️ **这一段不是可选的。** ctypes 对没声明原型的 Win32 函数默认按 `c_int` 处理，
#    64 位下有两处会当场坏掉，而且坏法都不明显：
#      · `CallNextHookEx` 的 lParam 是个指针，塞进 c_int → `OverflowError`
#        **在回调里抛** → Python 打一句 "Exception ignored" 就吞了，
#        钩子看着"已就绪"、实际每个事件都炸。（探针里就是这么现形的。）
#      · `SetWindowsHookExW` / `SetWinEventHook` 返回的是**句柄**，
#        按 c_int 接会被**截断成低 32 位** —— 非零，所以"装上了"的检查照样通过，
#        但拿去 Unhook 就是个野句柄。
# 📌 一句话：默认原型的错法是**静默的**，所以宁可全部显式声明。
_LRESULT = ctypes.c_ssize_t     # LONG_PTR，不是 c_long

_user32 = ctypes.windll.user32
_user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
_user32.CallNextHookEx.restype = _LRESULT
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                      wt.HINSTANCE, wt.DWORD]
_user32.SetWindowsHookExW.restype = wt.HANDLE
_user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE,
                                    ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD]
_user32.SetWinEventHook.restype = wt.HANDLE
_user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, ctypes.c_uint, ctypes.c_uint]
_user32.GetMessageW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.WindowFromPoint.argtypes = [wt.POINT]
_user32.WindowFromPoint.restype = wt.HWND
_user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_user32.GetAncestor.restype = wt.HWND
_user32.GetForegroundWindow.restype = wt.HWND

_GA_ROOT = 2


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


# ── 回调 → 队列 → 工作线程 ────────────────────────────────────────────────

_started = False
#: `(kind, detail, injected, point_or_None)` —— point 是**事件自带的**光标位置。
#: ⚠️ 不能在工作线程里读"当前光标位置"：那时候光标早就移开了，
#:    解出来的落点会是另一个窗口。**落点必须跟着事件走。**
_signals: "queue.SimpleQueue[tuple[str, str, bool, object]]" = queue.SimpleQueue()

# ⚠️ 见模块头第 3 条：这些必须活到进程结束
_kb_cb = None
_ms_cb = None
_we_cb = None
_kb_handle = None
_ms_handle = None
_we_handle = None


def _win_title(hwnd: int) -> str:
    """窗口标题，取不到就空串。⚠️ 用 `c_void_p` 包句柄 —— 大句柄不包会抛
    `OverflowError`，而这里外层有 `except`，那个事件就会**悄悄丢掉**。
    （同一个坑本模块的 ctypes 原型上踩过一次。）"""
    try:
        h = ctypes.c_void_p(int(hwnd))
        n = _user32.GetWindowTextLengthW(h)
        if n <= 0:
            return ""
        b = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(h, b, n + 1)
        return (b.value or "")[:40]
    except Exception:
        return ""


def _top_level_at(pt) -> int:
    """光标位置下的**顶层**窗口。`WindowFromPoint` 给的是子控件，要往上走到根。"""
    try:
        h = _user32.WindowFromPoint(pt)
        if not h:
            return 0
        root = _user32.GetAncestor(h, _GA_ROOT)
        return int(root or h)
    except Exception:
        return 0


def _worker_loop() -> None:
    """把队列里的信号交给判定层。**判定和数据库都在这里，不在钩子回调里。**"""
    while True:
        try:
            kind, detail, injected, pt = _signals.get()
            # 落点解析放在这里而不是回调里：`WindowFromPoint` 便宜，但低级钩子的
            # 300ms 超时预算不值得为它冒险（超时会**静默摘掉钩子**，见模块头）。
            if pt is not None:
                hwnd = _top_level_at(pt)          # 鼠标类：按事件自带坐标
            else:
                hwnd = int(_user32.GetForegroundWindow() or 0)   # 键盘类：按前台
            takeover.on_user_signal(kind, detail, injected=injected, hwnd=hwnd)
        except Exception as e:      # pragma: no cover - 守护线程不许死
            logger.debug(f"[Takeover-Hooks] 处理信号失败（忽略）: {e}")


def _emit(kind: str, detail: str, injected: bool, pt=None) -> None:
    try:
        _signals.put_nowait((kind, detail, injected, pt))
    except Exception:
        pass    # 队列出问题也绝不能拖住钩子回调


def _hook_thread() -> None:
    global _kb_cb, _ms_cb, _we_cb, _kb_handle, _ms_handle, _we_handle
    user32 = _user32
    kernel32 = ctypes.windll.kernel32

    # 回调的返回值也是 LRESULT —— 用 c_long 在 64 位下会截断
    hook_proc = ctypes.WINFUNCTYPE(
        _LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

    def _on_key(n_code, w_param, l_param):
        if n_code >= 0 and w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
            st = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            _emit(takeover.KEY, "", bool(st.flags & _LLKHF_INJECTED))
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_mouse(n_code, w_param, l_param):
        if n_code >= 0 and (w_param in _CLICKS or w_param in _SCROLLS):
            st = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            kind = takeover.SCROLL if w_param in _SCROLLS else takeover.CLICK
            # ⭐ 把**事件自带的**坐标带上：判定要知道这一下打在哪个窗口。
            #    `wt.POINT` 是值类型，这里复制一份 —— `st` 指向的内存回调返回后就不保证有效。
            _emit(kind, "", bool(st.flags & _LLMHF_INJECTED),
                  wt.POINT(st.pt.x, st.pt.y))
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    winevent_proc = ctypes.WINFUNCTYPE(
        None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

    def _on_foreground(h_hook, event, hwnd, id_obj, id_child, thread_id, ts):
        # ⚠️ 焦点切换**没有** injected 标志可用：Nano 的 `_focus_target_window()`
        #    自己也会把目标窗口提到前台，看上去和用户点标题栏一模一样。
        #    这里按发起线程判：是本进程干的就不算用户接管。
        #    📌 这和键鼠的 `LLKHF_INJECTED` 是同一个思路（问系统"谁干的"），
        #       不是"Nano 提前举手说我要动了"那种要所有调用点配合的写法。
        try:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == kernel32.GetCurrentProcessId():
                return
        except Exception:
            pass
        _emit(takeover.FOCUS_CHANGE, "", False)

    # ══════════════════════════════════════════════════════════════════════
    # 窗口生命周期传感器 —— 只服务挂起期日志，**不碰租约**
    # ══════════════════════════════════════════════════════════════════════
    # 规格里那四行（"我的窗口还在吗 / 被最小化还是被挡住 / 我记的坐标还有效吗"）
    # 只能靠这一族事件回答，而它们**都不是用户输入** ——
    # 📌 **所以绝对不许走 `on_user_signal`**：一个程序自己弹出的窗口
    #    不该被算成"用户拿走了电脑"（那正是 `FOCUS_CHANGE` 被踢出
    #    `TAKEOVER_TRIGGERS` 的同一个理由）。这里直接写日志，绕过判定层。
    #
    # ⚠️⚠️ **三层过滤，缺一条就会把 cmd 和缓冲一起淹掉**：
    #   ① **没在录就立刻返回** —— 不在挂起期这些事件毫无用处，
    #      而这一条让平时的开销近乎为零（Nano 绝大多数时间不在挂起）。
    #   ② **只要顶层窗口本身**（`id_obj == OBJID_WINDOW` 且 `id_child == 0`）——
    #      不加这条，`EVENT_OBJECT_CREATE` 会为**每一个子控件**报一次。
    #   ③ **`LOCATIONCHANGE` 按 hwnd 节流** —— 拖一次窗口能报上千条。
    #      日志侧只留一个"动过"的标记（几何只记净差），但回调本身也得省下来。
    #      📌 **"下游会聚合"不是"上游可以随便发"的理由。**
    _OBJID_WINDOW = 0
    _loc_last: dict = {}

    def _on_winevent(h_hook, event, hwnd, id_obj, id_child, thread_id, ts):
        try:
            if not takeover_log.is_recording():
                return
            if id_obj != _OBJID_WINDOW or id_child != 0 or not hwnd:
                return
            h = int(hwnd)
            if event == _EVENT_OBJECT_LOCATIONCHANGE:
                now = time.time()
                if now - _loc_last.get(h, 0.0) < 1.0:
                    return
                _loc_last[h] = now
                # ⚠️ 同 `created` 那条过滤：**看不见的窗口移动与 Nano 无关**
                #    （任务栏按钮、shell 瞬时窗口都会报）。探针里这一条
                #    把"涉及 13 个窗口"降到 5 个。
                if not user32.IsWindowVisible(ctypes.c_void_p(h)) or not _win_title(h):
                    return
                kind = "moved"
            elif event == _EVENT_OBJECT_CREATE:
                # ⚠️⚠️ **探针实测抓到的**：开**一个**记事本报出 11 条 `created`
                #    （IME、隐藏辅助窗口……），摘要于是说"10 newly opened" ——
                #    模型会以为用户开了 10 个程序。
                #    📌 **一个用户看不见的窗口，既挡不住 Nano，也不可能是用户开的。**
                #    所以要求「可见 + 有标题」。这条过滤放在**上游**，
                #    因为下游只看得到 hwnd，判不出这些。
                if not user32.IsWindowVisible(ctypes.c_void_p(h)):
                    return
                if not _win_title(h):
                    return
                kind = "created"
            elif event == _EVENT_OBJECT_DESTROY:
                # ⚠️ 销毁时**取不到**可见性和标题（窗口已经没了），所以这一格
                #    过滤不了 —— 交给日志侧按"这个 hwnd 本段里出现过吗"判。
                kind = "destroyed"
            elif event == _EVENT_SYSTEM_MINIMIZESTART:
                kind = "minimized"
            elif event == _EVENT_SYSTEM_MINIMIZEEND:
                kind = "restored"
            else:
                return
            # ⚠️ 已销毁的窗口取不到标题，所以 `destroyed` 那条只有 hwnd ——
            #    这恰好是 hwnd 必须入库的又一个理由：**窗口没了以后，
            #    能指认它的只剩句柄。**
            title = "" if kind == "destroyed" else _win_title(h)
            takeover_log.note(kind, hwnd=h, title=title, detail=kind)
        except Exception:
            pass

    _kb_cb = hook_proc(_on_key)
    _ms_cb = hook_proc(_on_mouse)
    _we_cb = winevent_proc(_on_foreground)
    _wl_cb = winevent_proc(_on_winevent)

    _kb_handle = user32.SetWindowsHookExW(_WH_KEYBOARD_LL, _kb_cb, None, 0)
    _ms_handle = user32.SetWindowsHookExW(_WH_MOUSE_LL, _ms_cb, None, 0)
    _we_handle = user32.SetWinEventHook(
        _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None,
        _we_cb, 0, 0, _WINEVENT_OUTOFCONTEXT)
    # ⚠️ 分成两个 range 注册：`OBJECT_*` 和 `SYSTEM_MINIMIZE*` 在事件号上
    #    **不连续**，用一个大 range 会顺带订阅中间几十种无关事件。
    _wl_handle = user32.SetWinEventHook(
        _EVENT_SYSTEM_MINIMIZESTART, _EVENT_SYSTEM_MINIMIZEEND, None,
        _wl_cb, 0, 0, _WINEVENT_OUTOFCONTEXT)
    _wo_handle = user32.SetWinEventHook(
        _EVENT_OBJECT_CREATE, _EVENT_OBJECT_LOCATIONCHANGE, None,
        _wl_cb, 0, 0, _WINEVENT_OUTOFCONTEXT)
    _all = (("键盘", _kb_handle), ("鼠标", _ms_handle), ("前台窗口", _we_handle),
            ("最小化/还原", _wl_handle), ("窗口生死/几何", _wo_handle))
    ok = [n for n, h in _all if h]
    bad = [n for n, h in _all if not h]
    if bad:
        # ⚠️ 装不上要说出来。悄悄失败的传感器 = 被动挂起看着在、其实从不触发，
        #    正是 `_os_task_busy` 那类"一声不响"的翻版。
        logger.warning(f"[Takeover-Hooks] 这些钩子没装上，被动挂起会漏信号: {'、'.join(bad)}")
    if ok:
        logger.info(f"[Takeover-Hooks] 已就绪: {'、'.join(ok)}")

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def start_takeover_hooks() -> None:
    """在 NiceGUI event loop 起来之后调。失败不影响主流程。"""
    global _started
    if _started:
        return
    import platform
    if platform.system() != "Windows":
        logger.info("[Takeover-Hooks] 非 Windows，跳过（被动挂起暂只支持 Windows）")
        return
    _started = True
    threading.Thread(target=_worker_loop, daemon=True,
                     name="takeover-worker").start()
    threading.Thread(target=_hook_thread, daemon=True,
                     name="takeover-hooks").start()
