# core/proactive/intel/engine.py
"""
主动智能主引擎，把下面各层串联起来。v0 默认 SHADOW_MODE：
只决策+记日志，不真说话——先攒实际运行数据，复核 shadow 日志后把 SHADOW_MODE 改 False 即上线。

三层：L0 硬安全(不受情感闸门，穿透静音) / L1 日历仪式(不受情感压制，服从静音+守卫) /
L2 状态推断(全套情感闸门)。情绪只在【渲染语气】和【主动闸门】出现，绝不碰硬功能。
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable, Optional

from loguru import logger
from core.i18n import language_clause as _lc_engine

from core.proactive import state as _st
from core.proactive.activity import get_buffer
from core.proactive.triggers import detect_holiday, is_do_not_disturb
from core.proactive.intel import candidate as _cand
from core.proactive.intel import feedback as _fbmod
from core.proactive.intel import shadow as _shadow
from core.proactive.intel.affect import get_affect
from core.proactive.intel.ledger import get_ledger
from core.proactive.intel.safety_l0 import get_l0
from core.proactive.intel.salience import get_tracker
from core.proactive.intel.scorer import score as _score
from core.proactive.intel.types import Channel, LedgerKey, Scene

# ── 上线开关 ──────────────────────────────────────────────────────────────
# v0 先跑 Shadow：复核 data/proactive_shadow.jsonl 觉得决策靠谱后，改成 False 上线。
SHADOW_MODE = True

# 守卫 [原型期标定]
_RECENT_USER_MSG_S = 300        # 用户 5min 内说过话不主动
_GLOBAL_DAILY_CAP = 8           # 安全天花板：一天最多主动次数（跨所有类）


def _today() -> str:
    import datetime
    return str(datetime.date.today())


class ProactiveEngine:
    def __init__(self, provider, push_callback: Callable[[str], Awaitable[None]]):
        self._provider = provider
        self._push = push_callback
        self._is_responding = False
        self._user_typing = False
        self._last_fire_ts = 0.0
        self._firing = False

    def set_responding(self, v: bool): self._is_responding = v
    def set_user_typing(self, v: bool): self._user_typing = v

    # ── 供 orchestrator 注入语气（被动路径也染色；用户明确下令时调用方降一档）──
    def tone_hint(self) -> str:
        return get_affect().tone_hint()

    # ── 反馈入口（UI/orchestrator 调）──
    def feedback(self, signal: "_fbmod.Signal", intervention_id: Optional[str] = None):
        _fbmod.get_feedback().ingest(signal, intervention_id)

    # ── 主循环 tick（app 定时器每 ~60s 调一次）──
    async def tick(self):
        if self._firing:
            return
        self._firing = True
        try:
            _fbmod.get_feedback().sweep_silence()   # 先结算沉默
            await self._tick_l0()                    # L0 不受情感/守卫的情感部分约束
            af = get_affect()
            if not af.enabled():
                return                               # "以后别主动说话" → 只剩 L0
            await self._tick_l1()                    # 日历仪式
            await self._tick_l2()                    # 状态推断
        except Exception as e:
            logger.warning(f"[Engine] tick 异常: {e}")
        finally:
            self._firing = False

    # ── L0 硬安全 ──
    async def _tick_l0(self):
        for alert in get_l0().poll():
            reason = f"L0_{alert.alert_type}"
            if SHADOW_MODE:
                _shadow.quick("L0", "system", reason, chosen="penetrate")
                continue
            msg = {
                "battery_low": "电量快没了，记得插电。",
                "disk_low": "系统盘快满了（不足 1GB），要清一下吗？",
            }.get(alert.alert_type, "系统有个情况要你看一下。")
            await self._push(msg)
            self._last_fire_ts = time.time()

    # ── L1 日历/仪式 ──
    async def _tick_l1(self):
        buf = get_buffer(); snap = buf.snapshot()
        if is_do_not_disturb(snap):
            return
        profile = _st.load_user_profile()
        c = detect_holiday(profile)
        if not c:
            return
        s = _st.load()
        hid = c["trigger_id"]
        if _st.holiday_already_fired(s, hid):
            return
        if SHADOW_MODE:
            _shadow.quick("L1", "calendar", f"L1_{hid}", chosen="would_fire")
            return
        name = c["context"].get("holiday_name", "今天")
        await self._push(await self._render_l1(name))
        _st.mark_holiday_fired(s, hid); _st.save(s)
        self._last_fire_ts = time.time()

    # ── L2 状态推断（主体）──
    async def _tick_l2(self):
        buf = get_buffer(); snap = buf.snapshot()
        # 守卫（物理礼貌，与情感无关）
        if self._is_responding or self._user_typing or is_do_not_disturb(snap):
            return
        last_msg = buf.last_user_message_ts()
        if last_msg and (time.time() - last_msg) < _RECENT_USER_MSG_S:
            return
        # 全局每日上限（安全天花板）
        if self._global_count_today() >= _GLOBAL_DAILY_CAP:
            return

        poll = get_tracker().poll()
        cands = _cand.generate(poll)
        if not cands:
            return

        ledger = get_ledger()
        fired = False
        for cand in cands:
            decision = _score(cand)
            ch = decision["chosen"]
            _shadow.quick(cand.type.value, cand.scene.value, decision["reason"],
                          scores=decision["scores"], chosen=(ch.value if ch else None))
            if SHADOW_MODE:
                # 模拟真实限流：记一次展示，让后续 tick 的 FreqAllowance 反映节流
                # （否则 record_shown 不被调用，日志会每个 tick 都 fired，失真）
                if not fired and ch is not None and ch != Channel.SILENT:
                    ledger.record_shown(LedgerKey(cand.type, cand.scene, ch), ch)
                    fired = True
                continue
            if fired:
                continue
            if ch is None or ch == Channel.SILENT:
                continue
            # 同一 tick 只发一个 visible
            content = await self._render_l2(cand)
            if not content:
                continue
            await self._push(content, cand.intervention_id)
            key = LedgerKey(cand.type, cand.scene, ch)
            ledger.record_shown(key, ch)
            _fbmod.get_feedback().register_shown(cand.intervention_id, cand.type, cand.scene, ch)
            self._bump_global_count()
            self._last_fire_ts = time.time()
            fired = True

    # ── 渲染（情绪在此落地）──
    async def _render_l2(self, cand) -> Optional[str]:
        tone = get_affect().tone_hint()
        serious = cand.scene in (Scene.CODING, Scene.WRITING)
        spice_note = (
        "The user is doing serious work, so keep the spice restrained and make the useful point first."
        if serious else 
        "You may show a little more personality."
        )
        guide = (
            "You are Nano, a desktop assistant with a strong personality. You are about to proactively say one line to the user.\n"
            "Hard requirements:\n"
            "1) This is a productivity intervention, not casual emotional checking. Say one short, natural, useful line.\n"
            "2) Do not explain how you know this, do not report what you observed, and never talk about your own mood.\n"
            "3) Let the current tone affect the flavor of the sentence only. It must not reduce usefulness or dominate the message.\n"
            "4) Negative tone means a little sharper, flatter, or shorter; never insult, blame, guilt-trip, or self-pity.\n"
            "5) This is only a suggestion. Do not say you are about to do it; action happens only after the user agrees.\n"
            f"Current tone: {tone}. {spice_note}\n"
            f"Goal for this intervention: {cand.proposed_action}\n"
            + _lc_engine("the final one-line message") + " "
            + "Do not use quotation marks, prefixes, or suffixes."
        )
        try:
            content, _ = await self._provider.chat_without_tools(
                context=[{"role": "user", "content": "Write the one line according to the goal."}],
                system_guide=guide,
            )
            content = (content or "").strip()
            return content or cand.fallback_message
        except Exception as e:
            logger.debug(f"[Engine] L2 渲染失败，用 fallback: {e}")
            return cand.fallback_message

    async def _render_l1(self, holiday_name: str) -> str:
        tone = get_affect().tone_hint()
        guide = (
            f"You are Nano. Today is {holiday_name}. Proactively say one timely line to the user.\n"
            f"Current tone: {tone}. Let the tone show naturally, but do not explain, do not mention your own mood, and do not ramble. "
            + _lc_engine("the final one-line message")
        )
        try:
            content, _ = await self._provider.chat_without_tools(
                context=[{"role": "user", "content": "Write the timely one-line message."}],
                system_guide=guide,
            )
            return (content or "").strip() or f"{holiday_name}快乐。"
        except Exception:
            return f"{holiday_name}快乐。"

    # ── 全局每日计数（安全天花板）──
    def _global_count_today(self) -> int:
        s = _st.load()
        return s.get("intel_daily", {}).get(_today(), 0)

    def _bump_global_count(self):
        s = _st.load()
        d = s.setdefault("intel_daily", {})
        d[_today()] = d.get(_today(), 0) + 1
        # 只留最近 7 天
        if len(d) > 7:
            for k in sorted(d.keys())[:-7]:
                d.pop(k, None)
        _st.save(s)
