# core/proactive/intel/salience.py
"""
唤醒层：把现有 activity buffer 的前台事件流聚合成 Episode。
v0 用"硬锚点"切边界（app 大切换 / 标题大变 / 保存 / 长空闲恢复 / 连续输入停止），
软推断（相似度）留默认关，实际运行中看 Shadow 觉得切得不对再开（开关已焊在代码里）。

输出：每次 poll 给 (刚关闭的 episode | None, 离散事件列表, 当前 scene)。
不读 keystroke 内容；窗口标题是合法本地信号。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from core.proactive.activity import get_buffer
from core.proactive.intel.scene import classify_scene
from core.proactive.intel.types import Episode, Scene
from core.self_identity import is_self_pid

# [原型期标定]
_IDLE_GAP_S = 15 * 60         # 多久无键盘算"空闲"（实测：5min 太短，喝杯咖啡就触发 recover，recover 占 52%；抬到 15min 只有真正离开才续上）
_TYPING_STOP_S = 180         # 连续输入后停多久算"停下来了"
_MIN_EPISODE_S = 30          # 太短的 episode 不产出反思类候选
_USE_SOFT_BOUNDARY = False   # v0 关；实际需要时再开

class SalienceTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._cur: Optional[Episode] = None
        self._cur_proc = ""
        self._cur_title = ""
        self._last_save_ts = 0.0
        self._last_seen_keys = 0
        self._typing_was_active = False
        self._was_idle = False   # 上一轮是否处于空闲（用于"回来那一下"只触发一次 recover）

    def poll(self) -> dict:
        """返回 {closed: Episode|None, events: [str], scene: Scene}。"""
        buf = get_buffer()
        snap = buf.snapshot()
        now = snap["now"]
        proc = snap.get("foreground_process", "") or ""
        title = snap.get("foreground_title", "") or ""

        # 空闲秒数（最后一次按键到现在）
        keys = snap.get("keys", [])
        last_key_ts = keys[-1].ts if keys else 0
        idle = now - last_key_ts if last_key_ts else _IDLE_GAP_S + 1

        # scene 只按【应用/标题】判，不再因"当前空闲"就持续判成 idle_recovery
        # （否则人不在时会一直触发 recover）。空闲→活跃的"回来"用 returned 离散事件处理。
        scene = classify_scene(proc, title, 0)
        returned = False
        if idle >= _IDLE_GAP_S:
            self._was_idle = True
        elif self._was_idle:
            returned = True        # 刚从空闲回到活跃 → 只此一下
            self._was_idle = False
        events: list[str] = []
        closed: Optional[Episode] = None

        with self._lock:
            # 排除 Nano 自己窗口：当前现场是切过来之前那个，跳过更新
            if is_self_pid(snap.get("foreground_pid", 0) or 0):
                return {"closed": None, "events": [], "scene": scene}

            # ── 硬锚点边界判定 ──
            boundary, conf = self._is_boundary(proc, title, idle, scene)
            if boundary and self._cur is not None:
                self._cur.end_ts = now
                self._cur.boundary_confidence = conf
                if (now - self._cur.start_ts) >= _MIN_EPISODE_S:
                    closed = self._cur
                self._cur = None

            # ── 新 episode ──
            if self._cur is None:
                self._cur = Episode(scene=scene, app_cluster=proc, start_ts=now)
            else:
                # 同 episode 内，scene 可能因标题变化而更新
                self._cur.scene = scene

            # ── 离散显著事件 ──
            saves = snap.get("saves", [])
            if saves and saves[-1].ts > self._last_save_ts:
                self._last_save_ts = saves[-1].ts
                events.append("save")
                self._cur.salient_events.append("save")

            # 连续输入后停止
            tss = snap.get("typing_session_start")
            if tss and (now - tss) > 5 * 60:
                self._typing_was_active = True
            if self._typing_was_active and idle >= _TYPING_STOP_S:
                events.append("typing_stop")
                self._typing_was_active = False

            # 大段删除（写得多删得多）
            recent = [k for k in keys if now - k.ts < 120]
            chars = sum(1 for k in recent if k.key == "char")
            dels = sum(1 for k in recent if k.key == "backspace")
            if chars >= 80 and dels >= chars * 0.8:
                events.append("large_delete")

            # 刚从空闲回到活跃 → 一次性"回来"事件（recover 候选靠它，不靠 scene）
            if returned:
                events.append("returned")

            self._cur_proc, self._cur_title = proc, title

        return {"closed": closed, "events": events, "scene": scene}

    def _is_boundary(self, proc: str, title: str, idle: float, scene: Scene):
        """返回 (是否边界, boundary_confidence)。纯硬锚点。"""
        if self._cur is None:
            return False, 0.0
        # app 大切换：高置信
        if proc and proc != self._cur_proc:
            return True, 0.9
        # 长空闲：高置信（之后是 idle_recovery）
        if idle >= _IDLE_GAP_S:
            return True, 0.85
        # 标题大变（同 app 内换文档/页面）：中置信
        if title and self._cur_title and not _title_similar(title, self._cur_title):
            if _USE_SOFT_BOUNDARY:
                return True, 0.55
        return False, 0.0


def _title_similar(a: str, b: str) -> bool:
    """粗相似度：共享前若干字符或一方包含另一方。v0 够用。"""
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return True
    if a in b or b in a:
        return True
    # 共同前缀比例
    n = min(len(a), len(b))
    same = sum(1 for i in range(n) if a[i] == b[i])
    return (same / max(len(a), len(b))) > 0.5


_tracker: Optional[SalienceTracker] = None
_tl = threading.Lock()


def get_tracker() -> SalienceTracker:
    global _tracker
    if _tracker is None:
        with _tl:
            if _tracker is None:
                _tracker = SalienceTracker()
    return _tracker
