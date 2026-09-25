# core/os_layer/window_binding.py
"""缺失的那个原语：**Nano 知道自己正在操作哪个窗口。**

═══════════════════════════════════════════════════════════════════════════
为什么需要它（一次真实的数据损坏）
═══════════════════════════════════════════════════════════════════════════
2026-08-07 实测：Nano 要操作它自己打开的 `新建文本文档.txt`，
用户中途把焦点放到了**自己的**另一个记事本上。
`executor_low.get_target_window()` 返回的是「Z-order 最前、且不是 Nano 自己」的窗口，
于是 Nano 对着**用户的**记事本 `Ctrl+A` + 输入，**清掉了用户的内容**。

📌 根因：`get_target_window()` 是一个**「任务开始时」的启发式**
（"操作我正在看的那个窗口"），却被当成**「每一步都重新求值」的权威**用了。
任务中途"最前面的窗口"等于"**谁最后动过**"，包括用户自己的窗口。
⭐ 与运行作用域那条同形：**一个事实只能证明它在被测那一刻成立，不能证明现在仍然成立。**

═══════════════════════════════════════════════════════════════════════════
它同时是挂起期日志（三层防护网第 2 层）的前提
═══════════════════════════════════════════════════════════════════════════
日志记的是 `(hwnd, title, ...)`。恢复后 Nano 读到"焦点去了一个记事本"，
要判断"那是不是我的那个" —— **没有参照物就判断不了**。
所以「日志推断」和「窗口绑定」不是两件竞争的事，是**同一个原语的两个消费者**。

═══════════════════════════════════════════════════════════════════════════
⭐ 绑定怎么建立：**用「我的动作前后的窗口差集」**
═══════════════════════════════════════════════════════════════════════════
Nano 双击一个文件 → 记事本冒出来 → **那个新窗口就是它的目标**。
所以在每个 OS 动作**前后各拍一次顶层窗口快照**，新增的那个就是 Nano 开的。

⚠️ 刻意**不用** `EVENT_OBJECT_CREATE` 钩子：那个事件对**每一个 UI 对象**都发
（菜单项、控件、tooltip 全算），要过滤成"顶层应用窗口"反而更容易出错；
而 `EnumWindows` 差集是确定性的，也不用再装一个全局钩子。

📌 **这也是「谁干的」这个问题在窗口层的答案**（键鼠层用的是 `LLKHF_INJECTED`）：
**夹在自己动作前后出现的窗口，就是自己造成的。** 窗口层没有 injected 那一位，
差集是最接近它的东西。

═══════════════════════════════════════════════════════════════════════════
⚠️⚠️ 绑定的有效期：**从活动租约推导，不靠任何人记得清理**
═══════════════════════════════════════════════════════════════════════════
绑定的寿命应该正好是"一整段 GUI 操作" —— 而那已经有一个现成的东西表示了：
`oslease` 的活动租约。所以绑定**带着 lease_id 一起存**，
读的时候对不上就当没有。

⭐ 这样它**不可能活过自己那一段**，而不需要在任何地方写"记得重置" ——
本轮反复栽的那个坑（`_os_task_busy` 靠所有调用点记得配对、
早期的 `reconcile_tick` 压根没人调）就是这么来的。
📌 **不依赖谁记得清理，让有效性从别的东西推导出来。**
"""
from __future__ import annotations

import ctypes
import threading
from typing import Any, Dict, Optional, Set

from loguru import logger

from core.self_identity import is_self_window

_lock = threading.Lock()
#: `{"lease_id": str, "hwnd": int, "pid": int, "title": str, "proc": str, "sure": bool}`
_bound: Optional[Dict[str, Any]] = None

#: 差集里要忽略的东西：太小的不是应用窗口（任务栏"开始"按钮实测 48x40），
#: 见 `executor_low.get_target_window` 里同款过滤的由来。
_MIN_W, _MIN_H = 200, 150


def _u():
    return ctypes.windll.user32


def _title(hwnd: int) -> str:
    try:
        u = _u()
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def _pid_of(hwnd: int) -> int:
    try:
        pid = ctypes.c_ulong()
        _u().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _proc_of(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _rect(hwnd: int):
    class R(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    try:
        r = R()
        if not _u().GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.l, r.t, r.r - r.l, r.b - r.t)
    except Exception:
        return None


def snapshot() -> Set[int]:
    """当前所有"够大的、可见的、不是 Nano 自己"的顶层窗口 hwnd。"""
    out: Set[int] = set()
    try:
        u = _u()
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _lp):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                t = _title(hwnd)
                if not t or is_self_window(int(hwnd)):
                    return True
                rc = _rect(hwnd)
                if not rc or rc[2] < _MIN_W or rc[3] < _MIN_H:
                    return True
                out.add(int(hwnd))
            except Exception:
                pass
            return True

        u.EnumWindows(EnumProc(cb), 0)
    except Exception as e:
        logger.debug(f"[WinBind] 枚举窗口失败: {e}")
    return out


def bind_if_new(before: Set[int], lease_id: str) -> Optional[Dict[str, Any]]:
    """动作跑完后调：**新冒出来的窗口就是刚才那个动作开的**，绑定它。

    没有新窗口 → 保持原绑定不动（返回当前绑定）。

    ⚠️ **多个新窗口时**（一个动作同时开出好几个）绑前台那个，并标 `sure=False`。
    不猜死也不放弃 —— 但要让模型知道这个绑定没那么确定。
    📌 宁可给一个标着"不确定"的答案，也别给一个假装确定的答案。
    """
    global _bound
    try:
        after = snapshot()
        new = after - before
        if not new:
            return bound(lease_id)
        fg = int(_u().GetForegroundWindow() or 0)
        hwnd = fg if fg in new else sorted(new)[0]
        pid = _pid_of(hwnd)
        rec = {"lease_id": lease_id, "hwnd": hwnd, "pid": pid,
               "title": _title(hwnd), "proc": _proc_of(pid),
               "sure": len(new) == 1, "rect": _rect(hwnd)}
        with _lock:
            _bound = rec
        logger.info(f"[WinBind] 绑定我打开的窗口 hwnd={hwnd} {rec['title']!r} "
                    f"({rec['proc']}){'' if rec['sure'] else ' ⚠️同时出现多个新窗口，不确定'}")
        return rec
    except Exception as e:
        logger.debug(f"[WinBind] 绑定失败（忽略）: {e}")
        return bound(lease_id)


def bound(lease_id: str) -> Optional[Dict[str, Any]]:
    """当前绑定。⚠️ **`lease_id` 对不上就当没有** —— 见模块头「有效期」那段。

    ⭐ 顺手也检查窗口还在不在：句柄失效的绑定不算绑定
    （`IsWindow` 为假 = 窗口已销毁）。**同「过期是推导出来的」一个道理。**
    """
    with _lock:
        b = _bound
    if not b or not lease_id or b.get("lease_id") != lease_id:
        return None
    try:
        if not _u().IsWindow(b["hwnd"]):
            return None
    except Exception:
        return None
    return b


def snapshot_bound_for_log() -> Optional[tuple]:
    """`(hwnd, title)` —— **不校验 lease_id**，只给日志/上报用。取不到返回 None。

    ⚠️⚠️ **为什么可以不校验，而 `bound()` 必须校验**：
    📌 **「能不能拿它当目标动手」和「能不能提它的名字」是两个不同的权限。**
    `bound()` 的 lease_id 校验是**安全属性** —— 上一个任务留下的句柄被当成
    这一次的目标，就是那次清空用户记事本的形状。
    而挂起日志只需要**指认**"这个 hwnd 是你的目标窗口"好让模型
    对上号，它不会据此动手；这里要是也强行要 lease_id，日志就只能说
    "焦点去了某个记事本"，答不出"那不是我的那个" —— 反而丢掉了它存在的理由。

    ⚠️ 仍然检查 `IsWindow`：**已销毁的窗口不该被当成"你的目标还在"**。
    （窗口已关这个事实由 `takeover_log` 那边的 `destroyed` 事件表达，不靠这里。）
    """
    with _lock:
        b = _bound
    if not b:
        return None
    try:
        if not _u().IsWindow(b["hwnd"]):
            return None
    except Exception:
        return None
    return int(b["hwnd"]), str(b.get("title") or "")


def clear() -> None:
    """只给测试和显式收尾用。⚠️ 生产**不需要**调 —— 有效期靠 lease_id 推导。"""
    global _bound
    with _lock:
        _bound = None


def target_state(lease_id: str) -> Optional[Dict[str, Any]]:
    """绑定的那个窗口**现在怎么样了**。这是本模块真正的产出。

    ⭐⭐ 它回答的正是**截图答不了**的那个问题：「我刚才打开的记事本去哪了？」
    截图里看不到它，可能是**关了 / 最小化了 / 被挡住了 / 在别的显示器上** ——
    这四种的后续动作完全不同（重开 / 还原 / 提到前台 / 移过来），
    而截图**一个都分不出来**。这里靠 Win32 直接给答案：

      · `IsWindow`            → 还在吗（假 = 已关闭）
      · `IsIconic`            → 最小化了吗
      · `GetForegroundWindow` → 是不是前台（不是 = 被挡/被抢焦点）
      · `GetWindowRect`       → 几何变了吗（坐标还有效吗）
    """
    b = bound(lease_id)
    if not b:
        return None
    try:
        u = _u()
        hwnd = b["hwnd"]
        fg = int(u.GetForegroundWindow() or 0)
        rc = _rect(hwnd)
        return {
            "hwnd": hwnd, "title": _title(hwnd) or b["title"],
            "proc": b.get("proc", ""), "sure": b.get("sure", True),
            "alive": True,
            "minimized": bool(u.IsIconic(hwnd)),
            "foreground": fg == hwnd,
            "fg_hwnd": fg, "fg_title": _title(fg) if fg else "",
            "moved": bool(rc and b.get("rect") and rc != b["rect"]),
            "rect": rc,
        }
    except Exception as e:
        logger.debug(f"[WinBind] 读目标窗口状态失败: {e}")
        return None


def describe(lease_id: str) -> str:
    """给模型看的一段话。没有绑定就返回空串（**不编**）。

    ⚠️ 措辞要**说清后续动作**，不能只报状态 —— "被最小化了"和"该还原它"
    对模型是两件事，只说前者等于把判断留给它猜。
    """
    st = target_state(lease_id)
    if not st:
        return ""
    head = (f"[Your target window] hwnd={st['hwnd']} \"{st['title']}\""
            + (f" ({st['proc']})" if st["proc"] else ""))
    if not st["sure"]:
        head += "\n⚠️ Low confidence: several windows appeared at once when this was bound."
    if st["minimized"]:
        head += ("\n⚠️ It is MINIMIZED — it did not disappear. Restore it before acting; "
                 "do not reopen the file (that would create a second window).")
    elif not st["foreground"]:
        head += (f"\n⚠️ It is NOT in the foreground. The foreground is hwnd={st['fg_hwnd']} "
                 f"\"{st['fg_title']}\" — a DIFFERENT window, even if it looks the same "
                 f"(identical titles are common).\n"
                 "⚠️ Anything you do now would land on that other window. Bring YOUR target "
                 "to the front first, or stop and ask. Never run select-all / delete / "
                 "overwrite / save while this warning is present.")
    # ⚠️ 最小化时**不报** MOVED：最小化会把 rect 改成屏幕外坐标，技术上确实"变了"，
    #    但真正的问题是"它被最小化了"。两条一起说会稀释主信息 ——
    #    📌 报警的价值取决于**读的人能不能一眼看出该干什么**，不取决于它有多全。
    if st["moved"] and not st["minimized"]:
        head += ("\n⚠️ It MOVED or was RESIZED — any coordinates you remember are stale. "
                 "Re-locate before clicking.")
    return head


def gone_note(lease_id: str, last_known: Optional[Dict[str, Any]] = None) -> str:
    """绑定的窗口**已经不在了**时说的话。

    ⚠️ 与"最小化"必须分开说：**关掉了要重开，最小化了要还原** ——
    截图对这两种情况看起来完全一样（都是屏幕上没有它）。
    """
    with _lock:
        b = last_known or _bound
    if not b or (lease_id and b.get("lease_id") != lease_id):
        return ""
    try:
        if _u().IsWindow(b["hwnd"]):
            return ""
    except Exception:
        return ""
    return (f"[Your target window] hwnd={b['hwnd']} \"{b.get('title','')}\" "
            f"is GONE — the window was CLOSED (not minimized, not covered). "
            f"If you still need it, open it again.")
