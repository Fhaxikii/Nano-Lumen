# -*- coding: utf-8 -*-
"""界面常驻显示的后端状态：后端按周期算快照，内容变了才推给界面（S6-6b C 类）。

这几块状态以后端为权威：待审卡（`pinned`）、后台任务抽屉（`tasks`）、接管状态条与
GUI 任务 / 临时授权（`session`）、联网状态（`net`）、监控面板的可用性（`health`）、
还活着的等待（`waits`，界面据此收等待 pill）、当日预算（`budget`）。以前界面各自定时读取重画；
现在后端按同样的周期读库，算出一份可序列化的快照，与上一次的内容指纹不同才作为
轮外事件 `state_snapshot`（`{"name", "data"}`）发出，界面收到就照着画。

收敛方式仍是 level-triggered：每一跳都从权威重算，不靠「改状态的地方记得通知」；
丢一个事件只是晚一个周期，下一次内容再变照样会推。界面（重新）连上时用 `current()`
取当前全量。界面的操作改了状态、想立刻看到时调 `refresh(name)`（立即重算一次）。

快照里只放数据；界面上的措辞由界面按字段决定（接管状态条给阶段与秒数，不给句子）。
以 `_` 开头的顶层键不进指纹（例如 `_at` = 这份快照的计算时刻）：它们随内容一起送达，
但只有它们变了不会触发推送。
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Callable, Optional

from loguru import logger

# 周期（秒），沿用各自原来在界面上的节奏
PERIODS = {"pinned": 1.5, "tasks": 2.0, "session": 1.0, "net": 3.0,
           "health": 1.0, "waits": 5.0, "budget": 20.0}

# 用户停手多久才算真停下（接管状态条从「刚动过」切到倒计时）。不显示给用户：
# 人打字的自然间隙经常超过 0.2 秒，没有这段的话状态条会在两种说法之间来回闪。
TAKEOVER_SETTLE_SEC = 2.0

_producers: dict[str, Callable[[], dict]] = {}
_last: dict[str, tuple[str, dict]] = {}


def register(name: str, producer: Callable[[], dict]) -> None:
    _producers[name] = producer


def tick(name: str) -> bool:
    """重算一次 `name` 的快照；内容变了就发事件。返回是否发了。读失败本跳不发。"""
    fn = _producers.get(name)
    if fn is None:
        return False
    try:
        data = fn()
    except Exception as e:
        logger.debug(f"[Snapshot] {name} 本跳没算出来（下一跳再试）: {e}")
        return False
    fp = json.dumps({k: v for k, v in data.items() if not str(k).startswith("_")},
                    sort_keys=True, ensure_ascii=False, default=str)
    prev = _last.get(name)
    if prev is not None and prev[0] == fp:
        _last[name] = (fp, data)       # 内容没变：记下最新一份（`current()` 取得到新的 `_at`），不推
        return False
    _last[name] = (fp, data)
    try:
        from core.runtime import events as _ev
        _ev.publish({"event": "state_snapshot", "name": name, "data": data}, None)
    except Exception as e:
        # 发不出去 → 忘掉这次的指纹，下一跳重发
        _last.pop(name, None)
        logger.debug(f"[Snapshot] {name} 发送失败（下一跳重发）: {e}")
        return False
    return True


def refresh(name: str) -> bool:
    """立即重算（界面的操作刚改了状态时用）。"""
    return tick(name)


def current(name: Optional[str] = None) -> dict:
    """当前快照（没算过的先算一次）。给了 `name` 返回那一份，否则返回全部。"""
    names = [name] if name else list(_producers)
    for n in names:
        if n not in _last:
            tick(n)
    if name:
        return (_last.get(name) or ("", {}))[1]
    return {n: _last[n][1] for n in names if n in _last}


def install(agent: Any) -> None:
    """登记各份快照的来源（需要 orchestrator 的那两份绑上 agent）。"""
    register("pinned", lambda: pinned_state(agent))
    register("tasks", lambda: tasks_state(agent))
    register("session", session_state)
    register("net", net_state)
    register("health", health_state)
    register("waits", waits_state)
    register("budget", budget_state)


# ── 待审卡 ────────────────────────────────────────────────────────────────
def pinned_state(agent: Any) -> dict:
    """未决的 Skill 代码审计交互（最新在前）+ 当前的引用回复目标。

    待办卡只放 `_CARD_KINDS`（Skill 代码审计）：它承载代码块、折叠、看源码与部署 / 丢弃
    动作，这些没法用一句话替代。澄清 / 管理确认只会复述气泡里的话；副作用与 OS 风险确认
    本该是当轮弹窗，不该出现在跨重启的待办卡里。这只是显示范围：交互记录一个字不动，
    模型侧 `[Open Interactions]` 照旧列出全部。

    引用回复的目标已经关闭（不在未决交互里）→ 在这里清掉：它唯一的撤销入口长在那张
    卡片上，卡片随交互关闭而消失，不清的话之后每一轮都会给模型注入一个已不存在的目标。
    判据用全部未决交互，不是只用上卡的那一种（显示范围不等于「还在不在」）。
    """
    from core.runtime.kernel import get_kernel
    from core.runtime import interaction as _it
    live = _it.list_live(get_kernel())
    rt = getattr(agent, "_reply_target", None) or None
    if rt and rt.get("iid") and rt["iid"] not in {r.interaction_id for r in live}:
        logger.info(f"[UI] 「回复这条」目标 {rt['iid']} 已关闭，自动取消引用")
        try:
            agent._reply_target = None
        except Exception:
            pass
        rt = None
    _CARD_KINDS = (_it.Kind.SKILL_AUDIT,)
    items = []
    for r in live:
        if r.kind not in _CARD_KINDS:
            continue
        try:
            has_pending = bool(agent._get_pending_skill(r.artifact_id or ""))
        except Exception:
            has_pending = False
        items.append({
            "interaction_id": r.interaction_id, "kind": r.kind, "status": r.status,
            "revision": r.revision, "prompt_text": r.prompt_text or "",
            "artifact_id": r.artifact_id or "", "needs_retry": bool(r.needs_retry),
            "has_pending_skill": has_pending,
        })
    return {"items": items, "reply_target": dict(rt) if rt else None}


# ── 后台任务抽屉 ──────────────────────────────────────────────────────────
_TASK_FIELDS = ("task_id", "goal_summary", "terminal_reason", "kind", "placement",
                "created_at", "updated_at", "execution")


def _task_dict(rec, agent: Any, with_steps: bool) -> dict:
    d = {f: getattr(rec, f, None) for f in _TASK_FIELDS}
    for f in ("terminal_reason", "kind", "placement", "execution"):
        v = d.get(f)
        d[f] = str(getattr(v, "value", v) or "")
    d["created_at"] = float(d.get("created_at") or 0)
    d["updated_at"] = float(d.get("updated_at") or 0)
    if with_steps:
        # Subagent 的步数：跑到第 N 步时 `updated_at` 不一定变，但抽屉要跟着重画
        try:
            d["steps"] = len(agent.agent_transcript(d.get("task_id") or ""))
        except Exception:
            d["steps"] = 0
    return d


def tasks_state(agent: Any) -> dict:
    """在跑（含排队）与本次运行里已结束的后台任务；排队的另列 id。"""
    from core.runtime.kernel import get_kernel
    from core.runtime.task import (live_background_jobs, finished_background_jobs,
                                   queued_background_jobs)
    k = get_kernel()
    return {
        "running": [_task_dict(r, agent, True) for r in live_background_jobs(k)],
        "finished": [_task_dict(r, agent, False) for r in finished_background_jobs(k)],
        "queued_ids": sorted(str(getattr(r, "task_id", "") or "")
                             for r in queued_background_jobs(k)),
    }


# ── 接管状态条 / GUI 任务 / 临时授权 ───────────────────────────────────────
def takeover_phase(held_until: float, parked: bool, hold_sec: float,
                   now: Optional[float] = None) -> dict:
    """用户接管时状态条该处于哪一格。

    | Nano      | 用户          | 阶段                     |
    |-----------|---------------|--------------------------|
    | 还在收手  | 刚动过        | `yielding`               |
    | 已停下    | 刚动过        | `paused`                 |
    | 任一      | 停手 ≥ 2 秒   | `countdown`（带剩余秒数）|
    | 任一      | 租约到期      | 不显示（`takeover` 为空）|

    倒计时从 `held_until` 推导，不自己数：用户再动一下，`held_until` 跳到
    now + hold，自动回到「刚动过」那一格。剩余 > hold - 2 ⟺ 距上次操作不足 2 秒。
    秒数向上取整，不显示 0 秒。
    """
    now = time.time() if now is None else now
    remain = max(0.0, float(held_until or 0) - now)
    if remain > hold_sec - TAKEOVER_SETTLE_SEC:
        return {"phase": "paused" if parked else "yielding"}
    return {"phase": "countdown", "seconds": max(1, int(math.ceil(remain)))}


def session_state() -> dict:
    """用户接管（有则给阶段）、GUI 任务是否进行中、临时授权（Temp Auto）是否有效。
    读失败按「没有」处理：观测坏了不该留一条假警报，也不该误报授权。"""
    from core.runtime.kernel import get_kernel
    from core.runtime import oslease as _ol
    k = get_kernel()
    takeover = None
    try:
        cur = _ol.current_activity(k)
        if cur is not None and cur.holder == _ol.Holder.USER:
            try:
                from core.proactive.takeover import USER_HOLD_SEC as _hold
                _parked = _ol.is_parked()
            except Exception:
                # 与 USER_HOLD_SEC 一起改
                _hold, _parked = 12.0, True
            takeover = takeover_phase(cur.held_until, _parked, _hold)
    except Exception:
        takeover = None
    try:
        gui = bool(_ol.gui_session_active(k))
    except Exception:
        gui = False
    try:
        temp_auto = bool(_ol.temp_auto_authorized())
    except Exception:
        temp_auto = False
    return {"takeover": takeover, "gui_session": gui, "temp_auto": temp_auto}


# ── 联网状态 ──────────────────────────────────────────────────────────────
def net_state() -> dict:
    """找（SearchTheWeb Skill）与读（声明 web.fetch 的已连接 MCP）合成的联网状态。"""
    try:
        from core.mcp_client import get_mcp_manager
        return {"state": str(get_mcp_manager().web_status())}
    except Exception:
        return {"state": "OFFLINE"}


# ── 监控面板的可用性 ──────────────────────────────────────────────────────
_CARDS = ("rag", "full_file", "net")


def health_state() -> dict:
    """有专属卡片的三项（知识库 / 全文加载 / 联网）的可用性档位，以及没有专属卡片的
    问题项（归到环境卡）的数量与最坏档位。档位：`FAULT` / `RECOVERING` / `DEGRADED`，
    没问题为空串。"""
    from core.health import get_capability_spec, get_health, Status
    h = get_health()

    def _level(st) -> str:
        if st is None:
            return ""
        if st.status == Status.UNAVAILABLE:
            return "FAULT"
        if st.status == Status.RECOVERING:
            return "RECOVERING"
        return "DEGRADED"

    orphans = []
    for s in h.problems():
        spec = get_capability_spec(s.capability)
        if spec is None or spec.monitor_card == "environment":
            orphans.append(s)
    worst = ("" if not orphans else
             "FAULT" if any(s.status == Status.UNAVAILABLE for s in orphans) else "DEGRADED")
    return {"cards": {c: _level(h.card_status(c)) for c in _CARDS},
            "env": {"count": len(orphans), "worst": worst}}


# ── 还活着的等待 ──────────────────────────────────────────────────────────
def waits_state() -> dict:
    """还活着的等待的 id。界面的等待 pill 只在对应等待真的不在这里时才收尾
    （完成必须来自权威，不能由「用户发消息了」之类的事推断）。

    `_at` 是计算时刻：比它晚登记的 pill 不能按这份快照判「已结束」（那时它的等待
    可能还没被这份快照看见）。"""
    from core.runtime.kernel import get_kernel
    from core.runtime import waitcond as _wc
    now = time.time()
    return {"live": sorted(r.wait_id for r in _wc.list_live(get_kernel(), oldest_first=True)),
            "_at": now}


# ── 当日预算 ──────────────────────────────────────────────────────────────
def budget_state() -> dict:
    """预算档位（`ok` / `soft` / `hard`）、当日花费与两条上限（美元）。"""
    from core.usage import usage_tracker as _ut
    cfg = _ut.load_config()
    return {"status": str(_ut.cap_status()), "cost": round(float(_ut.today_cost()), 4),
            "soft_cap": float(cfg.get("soft_cap_usd", 0) or 0),
            "hard_cap": float(cfg.get("hard_cap_usd", 0) or 0)}


def _reset_for_tests() -> None:
    _producers.clear()
    _last.clear()
