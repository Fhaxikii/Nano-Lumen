# -*- coding: utf-8 -*-
"""进度总线：`ref` → 「它**现在**在干什么」。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么这是一个独立模块，而不是 `longcmd` 里多一个函数                     ║
╚══════════════════════════════════════════════════════════════════════════╝

回看（`_FIRST_RECHECK_SEC` 那一眼）要能看**三种载体**：

  ① OS 长命令 —— stdout（`core/os_layer/longcmd.py`）
  ② MCP 调用  —— 协议原生 `notifications/progress`
  ③ 本地 Skill —— Skill 自己报（`BaseSkill.report_progress`）

而 orchestrator 只该问**一次**。

🔴 上一版它是 `from core.os_layer import longcmd; longcmd.progress(ref)` ——
   于是 MCP 和 Skill 那两条路**必然拿到空**，而且代码上完全看不出
   「这里少了两个载体」，只看得出「这里读了命令的进度」。
   📌 **一个机制如果只有一个接入点，那它可能不是机制，
      只是那一处的实现细节。**
   📌 **三个载体要被同一只眼睛看到，那个「看」的接口就该属于第三方，
      不属于其中任何一个。**

╔══════════════════════════════════════════════════════════════════════════╗
║ 两种接入方式，按「谁拥有那份输出」区分                                   ║
╚══════════════════════════════════════════════════════════════════════════╝

  ① **推**（`report`）——载体自己没有输出缓冲，往总线的环形缓冲里写。
     MCP 的 progress notification、Skill 的 `report_progress` 走这条。

  ② **拉**（`register_provider`）——载体**已经拥有**自己的输出，
     注册一个读函数，别把同一份输出抄第二遍。`longcmd` 走这条。
     📌 **不许为一个已经存在的权威再存一份副本** —— 副本会和权威分叉，
        而且分叉之后没人知道该信哪个。
     ⭐ 与 `core/runtime/task.register_activity_provider` 同一个形状
        （那个是「谁最后活动过」，这个是「它现在在干什么」）。

⚠️ **本模块只回答「现在看得到什么」，不回答「完成了没有」。**
   后者是 `WaitCondition` 的事（`waitcond.py`）。
   📌 两个不同的问题，不许由一个数字/一个接口回答（本项目栽过三次）。
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Optional

# 一条 ref 最多留多少行「它在干什么」。
# ⚠️ 比 `longcmd._MAX_LINES`(300) 小得多是刻意的：这里存的是**状态行**
#    （"[42%] downloading torch"），不是程序输出。进度通知可以每秒几十条，
#    而回看只关心**最近的样子**。
#    📌 丢头不丢尾 —— 与 longcmd 同一条纪律：出问题的证据总在末尾。
_MAX_LINES = 120

# 同时追踪多少个 ref。超了从最老的开始丢。
# ⚠️ 这是**防泄漏的兜底**，不是正常回收路径 —— 正常路径是 `forget(ref)`。
#    📌 一个兜底上限的存在，不免除调用方显式收尾的义务；
#       它只保证「忘了收尾」不会变成无界增长。
_MAX_REFS = 256


class _Track:
    """一个 ref 的进度轨迹。"""

    __slots__ = ("lines", "last_at", "pct")

    def __init__(self) -> None:
        self.lines: Deque[str] = deque(maxlen=_MAX_LINES)
        self.last_at: float = time.time()
        # 最近一次已知百分比（没有就是 None）。单独存是因为回看时
        # 「到哪一步了」比「最后一行文字」更有用。
        self.pct: Optional[float] = None


_lock = threading.Lock()
_tracks: "OrderedDict[str, _Track]" = OrderedDict()
# name → fn(ref) -> str | None
_providers: "dict[str, Callable[[str], Optional[str]]]" = {}


# ══════════════════════════════════════════════════════════════════════════
# ① 推：载体自己没有缓冲
# ══════════════════════════════════════════════════════════════════════════

def report(ref: str, message: str = "", *,
           progress: Optional[float] = None,
           total: Optional[float] = None) -> None:
    """载体报一次进度。**永不抛异常** —— 报进度失败绝不能影响那件事本身。

    ⚠️ 「永不抛」这件事在这里是硬要求，不是随手写的 try：
       调用方是 MCP 的通知回调和 Skill 作者写的代码，
       📌 **一个观测通道的故障，不许变成被观测那件事的故障。**
    """
    try:
        if not ref:
            return
        _pct: Optional[float] = None
        if progress is not None and total:
            try:
                _pct = max(0.0, min(100.0, float(progress) / float(total) * 100.0))
            except Exception:
                _pct = None
        _txt = (message or "").strip()
        if _pct is not None:
            _line = f"[{_pct:.0f}%] {_txt}" if _txt else f"[{_pct:.0f}%]"
        elif progress is not None:
            # 有 progress 没 total —— 协议允许（total 可选）。
            # ⚠️ 这时候**不许算百分比**，那是编数字。
            #    📌 不许为一件还没结束的事记一个结论 —— 分母未知就说不出比例。
            _line = f"[{float(progress):g}] {_txt}" if _txt else f"[{float(progress):g}]"
        else:
            _line = _txt
        if not _line:
            return
        with _lock:
            tr = _tracks.get(ref)
            if tr is None:
                tr = _Track()
                _tracks[ref] = tr
                while len(_tracks) > _MAX_REFS:
                    _tracks.popitem(last=False)
            _tracks.move_to_end(ref)
            # 连续同一行不重复堆（进度通知常常只改数字，文字不变）
            if not tr.lines or tr.lines[-1] != _line:
                tr.lines.append(_line)
            tr.last_at = time.time()
            if _pct is not None:
                tr.pct = _pct
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# ② 拉：载体已经拥有自己的输出
# ══════════════════════════════════════════════════════════════════════════

def register_provider(name: str, fn: Callable[[str], Optional[str]]) -> None:
    """注册一个「按 ref 取进度」的读函数。同名覆盖（幂等，模块可重复导入）。"""
    with _lock:
        _providers[name] = fn


# ══════════════════════════════════════════════════════════════════════════
# 读：回看那一眼
# ══════════════════════════════════════════════════════════════════════════

def tail(ref: str, lines: int = 20) -> str:
    """`ref` 现在的样子。**没有就返回空字符串** —— 调用方据此决定措辞。

    ⚠️ 返回空和返回「(无进度)」是两件事：前者让上层说实话
       （「系统对它的进度一无所知」），后者会被模型当成进度内容。
       📌 **一句「我不知道」必须由知道这件事的那一层说** ——
          总线只报事实，措辞不属于它。
    """
    if not ref:
        return ""
    # 先问 provider（它们拥有权威输出），再看自己的缓冲。
    # ⚠️ 顺序是刻意的：📌 **读权威，不读副本。**
    with _lock:
        _provs = list(_providers.items())
    for _n, _fn in _provs:
        try:
            _out = _fn(ref)
        except Exception:
            _out = None
        if _out:
            return _out
    with _lock:
        tr = _tracks.get(ref)
        if tr is None or not tr.lines:
            return ""
        _sel = list(tr.lines)[-max(1, int(lines)):]
        _age = max(0, int(time.time() - tr.last_at))
    _head = f"(last update {_age}s ago)\n" if _age >= 5 else ""
    return _head + "\n".join(_sel)


def has(ref: str) -> bool:
    """这条 ref 现在能不能看到东西。"""
    return bool(tail(ref, lines=1))


def forget(ref: str) -> None:
    """载体结束了 → 丢掉它的轨迹。

    ⚠️ 只丢总线自己的缓冲；provider 的回收归 provider
      （`longcmd.forget` 有它自己的「只移除已结束的」规则）。
      📌 **一个模块不许代替另一个模块判断「那边结束了没有」。**
    """
    if not ref:
        return
    with _lock:
        _tracks.pop(ref, None)


def live_refs() -> list[str]:
    """当前被追踪的 ref（诊断用）。"""
    with _lock:
        return list(_tracks.keys())


# ══════════════════════════════════════════════════════════════════════════
# 「当前这次调用的 ref」—— 给不方便显式传 ref 的载体用（本地 Skill）
# ══════════════════════════════════════════════════════════════════════════
#
# ⚠️⚠️ **为什么是 ContextVar 而不是挂在实例上**：
#    `registry.skills` 存的是**单例 Skill 实例**（`core/registry.py:290`
#    `skill = self.skills.get(skill_name)`），同一个实例会服务并发的多次调用。
#    把「本次调用的 ref」写成 `self._progress_ref` → 两次并发调用互相覆盖，
#    A 的进度会跑进 B 的轨迹里。
#    📌 **一个单例上的「本次调用」状态，在并发下必然串味** ——
#       这类状态只能挂在「调用」上，不能挂在「对象」上。
#    ⭐ 这和「一个『本轮有效』的状态，必须在每一条进入这一轮的
#       路径上都被设定」是同一族问题的两面：那条管**漏设**，这条管**串味**。
#
# ⚠️ 已知边界（**刻意不修**）：`asyncio.to_thread` 会拷贝上下文，所以
#    Skill 里 `await asyncio.to_thread(...)` 报进度是通的；而裸
#    `threading.Thread(target=...)` **不会**继承，那种写法报不出进度。
#    📌 与其做一个「万能捕获」的全局变量（那就退回串味），
#       不如让一种写法通、并把边界写清楚。
_current_ref: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nano_progress_ref", default="")


def bind(ref: str):
    """把 `ref` 绑到当前执行上下文，返回 token（用 `unbind` 还原）。"""
    return _current_ref.set(ref or "")


def unbind(token) -> None:
    try:
        _current_ref.reset(token)
    except Exception:
        pass


def current_ref() -> str:
    try:
        return _current_ref.get() or ""
    except Exception:
        return ""


def report_here(message: str = "", *,
                progress: Optional[float] = None,
                total: Optional[float] = None) -> None:
    """往「当前这次调用」的轨迹里报一行。没绑 ref 时静默丢弃。

    ⚠️ 没绑 ref 就静默丢弃是对的：Skill 在单元测试里、或者被别的路径直接
       调用时没有 ref，那时候报进度**没有接收方**，不是错误。
       📌 **一个观测通道在没有观测者时应该沉默，而不是报错。**
    """
    _r = current_ref()
    if _r:
        report(_r, message, progress=progress, total=total)
