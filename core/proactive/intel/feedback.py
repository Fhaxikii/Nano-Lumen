# core/proactive/intel/feedback.py
"""
反馈与学习：归因窗 + 更新路由表。把用户反应路由到对的钟。
- 显式信号(分类结果/按钮)：调用方(UI/orchestrator 的 LLM)给出分类，这里只路由。
- 行为/沉默：engine 调用。
- 严格遵守：Correctness 不动 welcome；被动≠主动欢迎；沉默分三档；反复别烦我压 LearnedDelta。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from loguru import logger

from core.proactive.intel import affect as _affect
from core.proactive.intel.ledger import get_ledger
from core.proactive.intel.types import Channel, InterventionType, LedgerKey, Scene

# 归因窗 [原型期标定]
_STRONG_WINDOW = 30
_WEAK_WINDOW = 5 * 60


class Signal(str, Enum):
    ACCEPTED = "accepted"               # 采纳/致谢/点执行
    ANNOYED_INTRUSIVE = "annoyed"       # 嫌打扰（这次烦）
    WRONG = "wrong"                     # 猜错（内容不对，不烦）
    WRONG_AND_ANNOYED = "wrong_annoyed"
    TOO_FREQUENT = "too_frequent"       # "对的但别老弹"
    MUTE_THIS = "mute_this"             # "以后别提醒这个"
    MUTE_ALL = "mute_all"               # "以后别主动说话"
    DONT_BOTHER_NOW = "dont_bother_now" # "别烦我/现在别说话"
    SILENCE = "silence"
    USED_PASSIVELY = "used_passively"   # 用户主动用了该能力（被动）


@dataclass
class _Shown:
    intervention_id: str
    type: InterventionType
    scene: Scene
    channel: Channel
    ts: float


class _Feedback:
    def __init__(self):
        self._lock = threading.Lock()
        self._shown: list[_Shown] = []
        self._responded: set = set()      # 已收到过显式/行为反馈的 intervention_id
        self._swept: set = set()          # 已按沉默处理过的 intervention_id
        # 跨类连续沉默追踪
        self._silence_streak: list = []   # [(type, ts)]

    def register_shown(self, intervention_id, type_, scene, channel):
        with self._lock:
            self._shown.append(_Shown(intervention_id, type_, scene, channel, time.time()))
            self._shown = self._shown[-50:]

    def _find(self, intervention_id: Optional[str]) -> Optional[_Shown]:
        now = time.time()
        with self._lock:
            cands = [s for s in self._shown if (now - s.ts) <= _WEAK_WINDOW]
        if intervention_id:
            for s in cands:
                if s.intervention_id == intervention_id:
                    return s
        # 不指定则归最近一次（精度优先=主动稀疏，多数无歧义）
        return cands[-1] if cands else None

    def ingest(self, signal: Signal, intervention_id: Optional[str] = None):
        """主入口。把一个反应路由到 affect / ledger。"""
        af = _affect.get_affect()
        led = get_ledger()
        tgt = self._find(intervention_id)

        # 全局级信号（不需要 target）
        if signal == Signal.MUTE_ALL:
            af.set_enabled(False)
            logger.info("[Feedback] 用户关闭主动系统")
            return
        if signal == Signal.DONT_BOTHER_NOW:
            af.apply_shock()  # 压 recent_shock（必回升）
            self._note_cross_type_silence(tgt)  # 也计入"反复别烦"
            return

        if tgt is None:
            # 没有可归因目标：只做极弱全局，不写 Ledger（宁可不学）
            if signal == Signal.SILENCE:
                return
            logger.debug(f"[Feedback] 信号 {signal} 无可归因目标，跳过")
            return

        key = LedgerKey(tgt.type, tgt.scene, tgt.channel)
        if signal != Signal.SILENCE:
            self._responded.add(tgt.intervention_id)  # 收到真反馈，不再当沉默

        if signal == Signal.ACCEPTED:
            led.update_welcome(key, positive=True)
            af.nudge_valence(_affect.VALENCE_POSITIVE)
            af.nudge_learned_delta(_affect.LEARNED_DELTA_RECOVER)
            self._silence_streak.clear()
        elif signal == Signal.ANNOYED_INTRUSIVE:
            led.update_welcome(key, positive=False)
            af.nudge_valence(_affect.VALENCE_NEG_INTRUSIVE)
        elif signal == Signal.WRONG:
            led.update_accuracy(key, correct=False)        # 只动 accuracy，不动 welcome
            af.nudge_valence(_affect.VALENCE_NEG_WRONG)
        elif signal == Signal.WRONG_AND_ANNOYED:
            led.update_accuracy(key, correct=False)
            led.update_welcome(key, positive=False)        # 小幅
            af.nudge_valence(_affect.VALENCE_NEG_INTRUSIVE)
        elif signal == Signal.TOO_FREQUENT:
            led.tighten_frequency(key)                     # welcome 不动，收频率
        elif signal == Signal.MUTE_THIS:
            led.set_explicit_mute(key, True)               # 不自动回升
        elif signal == Signal.USED_PASSIVELY:
            led.bump_capability(key)                        # 抬 capability，不抬 welcome
        elif signal == Signal.SILENCE:
            self._handle_silence(tgt)

    def sweep_silence(self):
        """engine 每 tick 调：把"展示后过了强归因窗仍无任何反馈"的判为沉默。"""
        now = time.time()
        with self._lock:
            due = [s for s in self._shown
                   if (now - s.ts) > _STRONG_WINDOW
                   and s.intervention_id not in self._responded
                   and s.intervention_id not in self._swept]
            for s in due:
                self._swept.add(s.intervention_id)
        for s in due:
            self.ingest(Signal.SILENCE, s.intervention_id)

    # ── 沉默三档 ──
    def _handle_silence(self, tgt: _Shown):
        # 单次沉默：unknown，不动 Patience，不写 Ledger。只计入跨类追踪。
        self._note_cross_type_silence(tgt)

    def _note_cross_type_silence(self, tgt: Optional[_Shown]):
        now = time.time()
        with self._lock:
            if tgt is not None:
                self._silence_streak.append((tgt.type.value, now))
            # 只看一个 active session 窗口内（2-4h）
            self._silence_streak = [(t, ts) for (t, ts) in self._silence_streak
                                    if now - ts < 4 * 3600]
            distinct_types = {t for t, _ in self._silence_streak}
            n = len(self._silence_streak)
        # 跨类连续沉默 ≥3 次覆盖 ≥2 type → 轻压 Patience（recent_shock 小幅）
        if n >= 3 and len(distinct_types) >= 2:
            _affect.get_affect().apply_shock(magnitude=-0.15)
            logger.info("[Feedback] 跨类连续沉默 → 轻压 Patience")
            with self._lock:
                self._silence_streak.clear()


_fb: Optional[_Feedback] = None
_fl = threading.Lock()


def get_feedback() -> _Feedback:
    global _fb
    if _fb is None:
        with _fl:
            if _fb is None:
                _fb = _Feedback()
    return _fb
