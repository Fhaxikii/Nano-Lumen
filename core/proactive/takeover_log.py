# -*- coding: utf-8 -*-
"""挂起期一次性日志 —— 「你被打断的这段时间里，世界发生了什么」。

═══════════════════════════════════════════════════════════════════════════
它要回答的问题（倒推出来的，不是先想字段再想用途）
═══════════════════════════════════════════════════════════════════════════
| 恢复后必须能答 | 靠什么 |
|---|---|
| 目标窗口还在吗 | 窗口创建 / 销毁（带 hwnd）|
| 不在前台了？现在前台是谁 | **完整焦点序列** A→B→C，不是"变了" |
| 被最小化还是被挡住 | 最小化 / 还原 |
| 记下的坐标还有效吗 | 移动 / 改大小 / 滚动 |
| 它的内容可能被改过吗 | **输入落在哪个 hwnd**（不只是"按了键"）|
| 用户是不是已经替它做了 | 同上 + 焦点序列 |

═══════════════════════════════════════════════════════════════════════════
⭐⭐ 三条设计判据（都是事先定好的，不是这里现编的）
═══════════════════════════════════════════════════════════════════════════
**① 摘要的体积与「涉及了几个窗口」成正比，不与「发生了多少事件」成正比。**
   曾担心"日志膨胀到 LLM 读不了"并自评风险 1% —— **照"记原始事件"做的话它是常态**：
   10 分钟里打字 3000 条、滚轮 ~350 条（实测速率）、点击 200 条，
   加上窗口移动事件**拖一次窗口就上千条**。
   所以这里**按窗口聚合**、焦点路线压掉重复、几何只记净差。
   ⭐ 更要紧的是**以「我的目标窗口」为中心组织，不按时间线平铺** ——
   Nano 只需要三件：自己的窗口怎么了 / 现在前台是谁 / 什么可能挡路。
   其余不管多少条都是噪音。

**② 日志必须自陈完整性 —— 否则「没把握还原」是感觉而不是事实。**
   环形缓冲会**静默**丢弃更早的事件。接管 15 分钟，开头那段就没了，
   而模型看不出来 → 它会拿残缺序列当完整的去还原，得出一个**自信的错结论**。
   ⚠️ 但目的是**给模型真实信息，不是给它畏手畏脚的理由**：
   真正决定"变化影不影响我继续"的极大概率是**尾部**（Nano 面对的是终态）。
   ⭐ 而环形缓冲按时间剪 —— **只会丢头，永远不会丢尾**。这条要直接说给模型。

**③ 🔴 真正该触发截图的是「传感器静默失效」，不是「头部被截断」。**
   Windows 的 `LowLevelHooksTimeout` 超时会**直接摘掉钩子且不通知**。
   钩子一死事件就不进日志，而**一份空日志读起来像"什么都没发生"** ——
   这是最坏的错误结论，形状与 `_os_task_busy` 泄漏、canary 停摆完全一样：
   **不报错、不留痕、看起来还很正常。**
   ⭐ 检测白拿：`hooks.py` 那个 2 秒窗口轮询是**独立的第二个传感器** ——
   轮询看到焦点变过、而事件钩子一条都没报 → 钩子是死的。用现成冗余交叉校验。

═══════════════════════════════════════════════════════════════════════════
⚠️ 起录时刻挂在「瞬发 / 接管状态条出现」，不挂在「真正发起挂起」
═══════════════════════════════════════════════════════════════════════════
因为 Nano 可能还在收手（不可阻断的动作没做完），而**用户在那段时间的操作
恰好可能破坏那个动作** —— 证据必须覆盖那一刻。
📌 **证据窗口要覆盖「可能出事的那一刻」，不是「我开始处理的那一刻」。**

═══════════════════════════════════════════════════════════════════════════
⚠️ 为什么新开一个模块，不直接扩 `ActivityBuffer`
═══════════════════════════════════════════════════════════════════════════
`ActivityBuffer` 的语义是「**最近十分钟用户在干嘛**」，喂的是主动开口
（`_build_ambient_injection`）—— 它要的是"氛围"，2 秒粒度够，也**不需要 hwnd**。
这里要的是「**这一段挂起期间，我的目标窗口出了什么事**」—— 需要 hwnd、
需要精确到单次输入落点、而且**用完就丢**（一次性）。
📌 **两个不同的问题不要挤进同一个缓冲**，否则一方的粒度需求会绑死另一方
（本轮已经栽过六次"拿一个为别的目的定义的东西去回答另一个问题"）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

#: 一段挂起期最多保留多少条原始事件。**超了丢头不丢尾** ——
#: 尾部才是决定性的（Nano 面对的是终态）。
MAX_EVENTS = 400


@dataclass
class Ev:
    """一条原始事件。⚠️ **hwnd 是必需的** —— 两个未命名记事本标题完全一样，
    靠标题判身份正好在最该分清的场景下失效。"""
    at: float
    kind: str                 # input / focus / created / destroyed / minimized
                              # / restored / moved
    hwnd: int = 0
    title: str = ""
    proc: str = ""
    detail: str = ""


@dataclass
class Session:
    """一段挂起期的证据。"""
    started_at: float
    target_hwnd: int = 0              # Nano 那个目标窗口（来自 window_binding）
    target_title: str = ""
    events: list[Ev] = field(default_factory=list)
    dropped: int = 0                  # 因超上限被丢掉的**头部**条数
    first_kept_at: float = 0.0        # 现存最早那条的时刻（用于自陈完整性）


_lock = threading.Lock()
_cur: Optional[Session] = None


# ══════════════════════════════════════════════════════════════════════════
# 记录侧
# ══════════════════════════════════════════════════════════════════════════

def start(target_hwnd: int = 0, target_title: str = "") -> None:
    """开始录一段。**在瞬发那一刻调**（用户接管、接管状态条出现），不是挂起真正生效时。

    ⚠️ 幂等：同一段挂起期内重复调不重开 —— 用户连点会触发多次接管/续期，
    但那是**同一段**挂起。重开会把前面的证据丢掉，而**导致挂起的那次操作
    恰恰是最该留下的**（它可能就是破坏了 Nano 动作的那一下）。
    """
    global _cur, _poll_focus_seen
    with _lock:
        if _cur is not None:
            # 已经在录了 —— 只补上目标窗口（首次接管时可能还没绑定）
            if target_hwnd and not _cur.target_hwnd:
                _cur.target_hwnd, _cur.target_title = target_hwnd, target_title
            return
        # ⚠️ 健康计数器在 **start** 归零，不在 `finish` —— 因为 `sensors_healthy()`
        #    是在 `finish()` **之后**拿着取出来的 Session 调的。
        _poll_focus_seen = 0
        now = time.time()
        _cur = Session(started_at=now, target_hwnd=target_hwnd,
                       target_title=target_title, first_kept_at=now)
    logger.info(f"[TakeoverLog] 开始记录挂起期证据（目标窗口 hwnd={target_hwnd}）")


def note(kind: str, hwnd: int = 0, title: str = "", proc: str = "",
         detail: str = "") -> None:
    """记一条。**没在录就直接丢** —— 不在挂起期的事件与这份一次性日志无关。"""
    with _lock:
        if _cur is None:
            return
        h = int(hwnd or 0)
        # ⚠️⚠️ `destroyed` 的上游过滤不掉（窗口已经没了，取不到可见性/标题），
        #    所以在这里按「本段里见过这个 hwnd 吗」判。
        #    ⭐ 这条自动跟上游的 `created` 过滤对齐：那些隐藏辅助窗口的 create
        #      被拦了，于是它们的 destroy 在这里也见不到 hwnd → 一并丢掉。
        #    ⚠️ **但目标窗口必须无条件留** —— 它可能整段挂起里什么都没发生、
        #      最后被关掉，那恰恰是最要紧的一条（「我的目标窗口还在吗」）。
        if kind == "destroyed" and h != _cur.target_hwnd:
            if not any(e.hwnd == h for e in _cur.events):
                return
        _cur.events.append(Ev(time.time(), kind, h, title, proc, detail))
        # ⚠️ 超上限**丢头**：尾部才是决定性的。
        if len(_cur.events) > MAX_EVENTS:
            over = len(_cur.events) - MAX_EVENTS
            del _cur.events[:over]
            _cur.dropped += over
            _cur.first_kept_at = _cur.events[0].at


def finish() -> Optional[Session]:
    """结束这一段并取出证据。**取完就清** —— 它是一次性的。"""
    global _cur
    with _lock:
        s, _cur = _cur, None
    if s is not None:
        logger.info(f"[TakeoverLog] 挂起期结束：{len(s.events)} 条事件"
                    f"（丢头 {s.dropped} 条）")
    return s


def is_recording() -> bool:
    with _lock:
        return _cur is not None


# ══════════════════════════════════════════════════════════════════════════
# 🔴 传感器健康交叉校验 —— 判据 ③
# ══════════════════════════════════════════════════════════════════════════
# `hooks.py` 的 2 秒窗口轮询是一个**完全独立的第二传感器**：它靠
# `GetForegroundWindow()` 主动问，不经过任何钩子。所以：
#
#   轮询说"焦点变过" 而 事件钩子一条 focus 都没报  →  **钩子是死的**
#
# ⭐ 这条检测**不新装任何东西**，纯粹是把已有的冗余用起来。
# 📌 一个自己会静默死掉的传感器，必须有个**独立的第二来源**能证伪它 ——
#    否则它死了之后交出的空数据，读起来和"真的什么都没发生"一模一样。
#
# ⚠️ 只在"轮询报了、钩子没报"这一个方向上判死。反方向（钩子报了轮询没报）
#    是**正常的**：焦点在 2 秒内变过去又变回来，轮询天然看不见。
#    📌 **交叉校验要判的是「该有却没有」，不是「两边不一致」。**
_poll_focus_seen: int = 0


def note_poll_focus() -> None:
    """独立的第二传感器（`hooks.py` 的 2s 轮询）看到了一次焦点变化。"""
    global _poll_focus_seen
    with _lock:
        if _cur is not None:
            _poll_focus_seen += 1


def sensors_healthy(s: Optional[Session] = None) -> bool:
    """事件钩子在这段挂起期里还活着吗。"""
    if s is None:
        return True
    if _poll_focus_seen <= 0:
        return True          # 第二传感器也没看到变化 → 没有证据说钩子死了
    return any(e.kind == "focus" for e in s.events)


def _reset_for_test() -> None:
    global _cur, _poll_focus_seen
    with _lock:
        _cur = None
        _poll_focus_seen = 0


# ══════════════════════════════════════════════════════════════════════════
# 摘要侧 —— 以「我的目标窗口」为中心
# ══════════════════════════════════════════════════════════════════════════

def _fmt_win(hwnd: int, title: str, proc: str, is_target: bool) -> str:
    tag = " ← YOUR TARGET" if is_target else ""
    t = f'"{title}"' if title else "(no title)"
    return f"hwnd={hwnd} {t}{(' ' + proc) if proc else ''}{tag}"


def summarize(s: Optional[Session], sensors_healthy: bool = True) -> str:
    """把一段挂起期变成给模型看的一段话。**没有证据就返回空串，不编。**

    ⚠️ 组织方式是**以目标窗口为中心**，不是时间线平铺 —— 见模块头判据 ①。
    """
    if s is None:
        return ""
    now = time.time()
    span = now - s.started_at
    tgt = s.target_hwnd

    # ── 按窗口聚合 ────────────────────────────────────────────────────
    per: dict[int, dict[str, Any]] = {}
    focus_seq: list[int] = []
    for e in s.events:
        d = per.setdefault(e.hwnd, {"title": e.title, "proc": e.proc,
                                    "input": 0, "flags": set()})
        if e.title and not d["title"]:
            d["title"] = e.title
        if e.proc and not d["proc"]:
            d["proc"] = e.proc
        if e.kind == "input":
            d["input"] += 1
        else:
            d["flags"].add(e.kind)
        if e.kind == "focus":
            # ⭐ 焦点路线压掉连续重复：A↔B 反复 5 次不该占 10 行
            if not focus_seq or focus_seq[-1] != e.hwnd:
                focus_seq.append(e.hwnd)

    out: list[str] = [f"[What happened while you were paused] {span:.0f}s"]

    # ── ① 目标窗口怎么了（最重要，放最前）─────────────────────────
    if tgt:
        d = per.get(tgt)
        if d is None:
            out.append(f"· YOUR TARGET WINDOW (hwnd={tgt} \"{s.target_title}\"): "
                       f"nothing happened to it.")
        else:
            # ⭐⭐ **终态和「期间发生过什么」必须分开说。**
            #
            # ⚠️⚠️ 探针实测抓到的问题：原来是一个平铺列表，输出成
            #    `it was CLOSED; it was MINIMIZED; it was restored; it MOVED`——
            #    真实时序却是 moved→minimized→restored→moved→destroyed。
            #    两个后果：
            #      · 读起来像"先关了然后又最小化又还原"，**时序完全乱了**；
            #      · `minimized` 和 `restored` 同时出现时，模型**分不出终态**。
            #    📌 **这是又一次「一个字段表达两个现实」**：
            #       "这一段里它经历过什么" 和 "它现在什么样" 是两件事。
            #    ⭐ 而 Nano 需要的**首先**是终态（它接下来要对着现在的世界动手），
            #      "经历过什么"只用来判断「我记的东西还能不能用」。
            _last_state = ""
            for e in s.events:
                if e.hwnd != tgt:
                    continue
                if e.kind == "destroyed":
                    _last_state = "GONE — it was closed"
                elif e.kind == "minimized":
                    _last_state = "MINIMIZED"
                elif e.kind in ("restored", "created"):
                    _last_state = "visible"
            now_line = (f"now {_last_state}" if _last_state
                        else "still there as far as this log shows")
            out.append(f"· YOUR TARGET WINDOW (hwnd={tgt} "
                       f"\"{d['title'] or s.target_title}\"): {now_line}")

            # 期间变更 —— 只报"让你记的东西失效"的那几件
            bits = []
            if d["input"]:
                bits.append(f"the user typed/clicked in it {d['input']} time(s), "
                            f"so its CONTENT may have changed")
            if "moved" in d["flags"]:
                bits.append("it moved or was resized, so remembered "
                            "coordinates are stale")
            if "minimized" in d["flags"] and _last_state != "MINIMIZED":
                bits.append("it was minimized and then restored")
            if bits:
                out.append("  During the pause: " + "; ".join(bits) + ".")
    else:
        out.append("· You had no bound target window during this pause.")

    # ── ② 焦点去哪了（终态最重要）──────────────────────────────────────
    if focus_seq:
        names = []
        for h in focus_seq[-4:]:
            d = per.get(h, {})
            names.append(_fmt_win(h, d.get("title", ""), d.get("proc", ""), h == tgt))
        route = "  →  ".join(names)
        more = f"（earlier hops omitted）" if len(focus_seq) > 4 else ""
        out.append(f"· Foreground moved: {route} {more}".rstrip())
        out.append(f"· Foreground ENDED on: "
                   f"{_fmt_win(focus_seq[-1], per.get(focus_seq[-1],{}).get('title',''), per.get(focus_seq[-1],{}).get('proc',''), focus_seq[-1]==tgt)}")

    # ── ③ 别的窗口（只报数量和是否新建，不铺细节）────────────────────────
    others = [h for h in per if h and h != tgt]
    if others:
        created = [h for h in others if "created" in per[h]["flags"]]
        typed = [h for h in others if per[h]["input"]]
        line = f"· {len(others)} other window(s) involved"
        if created:
            line += f"; {len(created)} newly opened"
        if typed:
            line += f"; the user typed in {len(typed)} of them"
        out.append(line + ".")

    # ── ④ 完整性自陈 ────────────────────────────────────────────────────
    # ⚠️ 目的是给真实信息，**不是给它畏手畏脚的理由** —— 所以同时说清尾部完整。
    if s.dropped:
        out.append(f"⚠️ Coverage: this log keeps the LAST {len(s.events)} events "
                   f"(from {time.strftime('%H:%M:%S', time.localtime(s.first_kept_at))}). "
                   f"{s.dropped} earlier event(s) were dropped — the pause actually began "
                   f"at {time.strftime('%H:%M:%S', time.localtime(s.started_at))}.\n"
                   f"  ⭐ The TAIL is intact (the ring buffer only ever drops the head), "
                   f"and the tail is what determines the current state.")
    else:
        out.append("· Coverage: complete — every event during the pause is included.")

    # ── ⑤ 🔴 传感器健康：空日志与「什么都没发生」不是一回事 ────────────────
    if not sensors_healthy:
        out.append("🔴 SENSOR FAILURE: the input hooks stopped reporting during this "
                   "pause, so this log is NOT trustworthy — an empty log here does NOT "
                   "mean nothing happened.\n"
                   "  → Take a screenshot and verify the screen yourself before acting.")
    elif not s.events:
        out.append("· No events recorded, and the sensors were healthy — "
                   "so nothing observable actually happened.")

    # ── ⑥ 怎么用这份日志（第 2/3 层的关系）────────────────────────────────
    out.append("")
    out.append("How to use this: reconstruct what the user did from the above. "
               "If you can tell that it does NOT affect your next step, just continue — "
               "do NOT take a screenshot. Only if you cannot reconstruct it (or the log "
               "says it is untrustworthy) should you look at the screen.\n"
               "⚠️ If you do take a screenshot, read it TOGETHER with this log rather "
               "than judging from the image alone — the best case is that the two agree.")
    return "\n".join(out)
