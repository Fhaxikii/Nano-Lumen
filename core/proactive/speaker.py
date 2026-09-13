# core/proactive/speaker.py
"""
主动开口调度器。
负责：冷却检查 → 触发决策 → 调 Claude 生成内容 → fallback 模板 → push。
"""
from __future__ import annotations
import asyncio
import time
from typing import Callable, Optional, Awaitable
from loguru import logger

from core.proactive import state as st
from core.proactive.activity import get_buffer
from core.proactive.triggers import (
    generate_candidates, detect_holiday, is_do_not_disturb
)

# ══════════════════════════════════════════════════════════════════════════
# 🔴 这里曾经有一套「Claude API 不可用时的固定文案兜底」——2026-08-22 整个删掉
# ══════════════════════════════════════════════════════════════════════════
#
# 原来是两张表（`_FALLBACK` 日常触发 / `_HOLIDAY_FALLBACK` 节日）+ `_get_fallback()`，
# 在 `_generate()` 里当 `chat_without_tools` 抛异常时随机挑一句顶上。
#
# ⚠️⚠️ **它本身是一个悖论，而这个理由比 i18n 那条更硬：**
#
#   API 调不通（断网 / 超时 / 额度 / 服务故障）意味着 **Nano 此刻恰恰不能思考**。
#   而这时候蹦出一句「还没睡？」，给用户的体感是**Nano 还活着、还在关心我** ——
#   📌 **一句在「它已经不能思考」时发出的话，是在谎报它的状态。**
#      而气泡里的每一句话，用户都读成「Nano 在对我说」。
#
#   ⭐ 反过来想才对：主动开口这件事，**它的价值全部来自「这句话是它想出来的」**。
#      一句从表里随机抽的话，既不是它想的，也不知道此刻发生了什么 ——
#      它保住的只有「有话出现」这个形式，而形式本来就不是这里的目的。
#
# ⚠️ 第二个理由（界面 i18n）：**兜底文案的 i18n 成本是乘法，不是加法。**
#    · 每加一个主动开口触发点，就得**为它多写一条兜底文案**
#    · 而每条兜底文案又要**乘上支持的语言数**
#    · 还得再写一层「按用户当前选的语言挑对应那条」的分发逻辑
#    📌 一句本来就不该出现的话，却要为它建一整套多语言分发 ——
#       **复杂度全花在维持一个错误行为上。**
#
#    ⭐ 正解是反过来的：**把用户当前选的语言注入模型，让模型按那个语言生成。**
#       语言从此只是 prompt 里的一个事实，加语言 = 加一行映射，
#       **不需要为任何一句话准备译文**。见下面 `_generate()` 里的语言注入。
#
# ⚠️ **节日祝福这条线为什么在代码里找不到**（2026-08-22 拆除 `_HOLIDAY_FALLBACK` 时记）：
#    节日祝福那一类**不能靠翻译解决** —— 各国的节日本身就不一样
#    （春节 / 端午对英文用户没有意义，而圣诞对中文用户是另一种分量），
#    UI 里可选的省份/地区也和节日表对不上。
#    📌 **这不是「同一句话换种语言」，是「换一批要不要说的事」** ——
#       所以它属于界面 i18n 之外的一个独立问题，届时单独想办法。
#    ⚠️ 之所以记在这里而不是别处：拆掉 `_HOLIDAY_FALLBACK` 之后，
#       节日这条线在代码里就**没有任何痕迹**了 —— 这段话是它唯一的落点。
#
# ⭐ 而「不说话」这条路**本来就是通的**：`_fire()` 里早就写着
#    `content = await self._generate(...)` / `if not content: return`。
#    🔴 也就是说这个正确的出口一直存在，只是 `_generate()` 从来不走它 ——
#       📌 **一个 fallback 的存在，会让它上游那条正确的退路永远跑不到。**
#
# 📌 判据（可推广）：**fail-safe 的方向是「少说一句」，不是「凑一句」。**
#    同那条「读不到权威时什么都不收——多留一个转圈的 pill，
#    好过谎报一个 ✓」。
#
# ⚠️ 唯一的行为变化：断网时主动开口**静默跳过**（日志里有 warning）。
#    下一个触发点还会再来，它本来就是尽力而为的东西。


# ── 主调度器 ──────────────────────────────────────────────────────────────────

class ProactiveSpeaker:
    def __init__(self, provider, push_callback: Callable[[str], Awaitable[None]]):
        """
        provider: Orchestrator 的 provider（用于调 Claude 生成内容）
        push_callback: async (content: str) -> None，即 app._proactive_push
        """
        self._provider = provider
        self._push = push_callback
        self._firing = False          # 防止并发重入
        self._is_responding = False   # 由 app.py 维护，主流程响应中时置 True
        self._user_typing = False     # 由 app.py 维护
        self._startup_ts = time.time()
        self._startup_holiday_checked = False

    def set_responding(self, v: bool):
        self._is_responding = v

    def set_user_typing(self, v: bool):
        self._user_typing = v

    # ── 中断保护检查（节日和陪伴类都要过）────────────────────────────────────

    def _interruption_ok(self, snap: dict) -> bool:
        if self._is_responding:
            return False
        if self._user_typing:
            return False
        if is_do_not_disturb(snap):
            return False
        # 用户5分钟内刚说过话
        buf = get_buffer()
        last_msg = buf.last_user_message_ts()
        if last_msg and (time.time() - last_msg) < 300:
            return False
        return True

    # ── 启动时节日检测（60秒后调用一次）──────────────────────────────────────

    async def check_holiday_on_startup(self):
        if self._startup_holiday_checked:
            return
        self._startup_holiday_checked = True
        await asyncio.sleep(60)
        await self._try_holiday()

    async def _try_holiday(self):
        buf = get_buffer()
        snap = buf.snapshot()
        profile = st.load_user_profile()
        candidate = detect_holiday(profile)
        if candidate is None:
            return

        holiday_key = candidate["trigger_id"]
        s = st.load()

        if st.holiday_already_fired(s, holiday_key):
            return

        if st.holiday_skip_count(s, holiday_key) >= 2:
            # 今天已跳过2次，不再重试
            return

        if not self._interruption_ok(snap):
            st.mark_holiday_skipped(s, holiday_key, reason="interruption")
            st.save(s)
            # 30分钟后再试一次
            asyncio.create_task(self._retry_holiday_later(holiday_key, delay=1800))
            return

        await self._fire(candidate, s)

    async def _retry_holiday_later(self, holiday_key: str, delay: int):
        await asyncio.sleep(delay)
        await self._try_holiday()

    # ── 5分钟轮询入口 ─────────────────────────────────────────────────────────

    async def maybe_speak(self):
        if self._firing:
            return
        if time.time() - self._startup_ts < 60:
            return

        buf = get_buffer()
        snap = buf.snapshot()
        profile = st.load_user_profile()
        s = st.load()

        if not self._interruption_ok(snap):
            return

        if st.in_rejection_silence(s):
            return

        candidates = generate_candidates(snap, buf, profile)
        if not candidates:
            return

        if not st.global_cooldown_ok(s):
            return

        if not st.daily_count_ok(s):
            return

        for c in candidates:
            if st.trigger_cooldown_ok(s, c["trigger_id"], c["cooldown"]):
                await self._fire(c, s)
                return

    # ── 实际触发 ──────────────────────────────────────────────────────────────

    async def _fire(self, candidate: dict, s: dict):
        self._firing = True
        trigger_id = candidate["trigger_id"]
        category   = candidate["category"]
        context    = candidate["context"]

        try:
            content = await self._generate(trigger_id, context)
            if not content:
                return

            await self._push(content)

            # 写状态
            if category == "holiday":
                st.mark_holiday_fired(s, trigger_id)
            else:
                st.mark_companion_fired(s)
                st.mark_trigger_fired(s, trigger_id)
                st.increment_daily_count(s)
                if trigger_id == "nano_rejection":
                    st.mark_rejection_silence(s)

            st.save(s)
            logger.info(f"[Proactive] fired: {trigger_id}")

        except Exception as e:
            logger.warning(f"[Proactive] fire 异常: {e}")
        finally:
            self._firing = False

    async def _generate(self, trigger_id: str, context: dict) -> Optional[str]:
        prompt = _build_prompt(trigger_id, context)
        try:
            # ⭐ 语言走 `i18n.language_clause()` —— 它的 docstring 原话就是
            #    **「所有需要指定语言的提示词都该用它」**。
            #    🔴 原来这里写的是「Output Chinese unless the user's current language
            #       is known to be different」，那正是 `language_clause` 造出来要收掉的
            #       两句之一（另一句在 app.py）——📌 它们各自说得通，合在一起就说不清
            #       Nano 到底按什么规则决定语言。
            #    ⭐ 而语言的**唯一出处**是设置→通用→语言那个下拉
            #       （`ui.select(on_change=set_lang)` → `i18n.current_lang()`），
            #       所以这是个**已知值**，模型不需要猜。
            #    ⭐ 这也是拆掉固定文案兜底之后语言这件事的正解：
            #       加一门语言 = 往 `LANGS` 加一行，**不需要为任何一句话准备译文**。
            try:
                from core.i18n import language_clause as _lang_clause
                _lang_line = _lang_clause("your line") + "\n"
            except Exception:
                _lang_line = ""      # 取不到就不说 —— 别自己编一句语言指令
            content, _ = await self._provider.chat_without_tools(
                context=[{"role": "user", "content": prompt}],
                system_guide=(
                    "You are Nano, a desktop companion assistant. Say it naturally in 1-2 short sentences. "
                    "Do not explain the reason, do not give feature tips, and keep Nano's usual personality.\n"
                    + _lang_line
                ),
            )
            return content.strip() if content else None
        except Exception as e:
            # ⚠️ **什么都不说**（见上面那段：拿固定文案顶上等于谎报它还在思考）。
            #    `_fire()` 里的 `if not content: return` 会接住它。
            logger.warning(f"[Proactive] 生成失败 → 本次不开口（不使用固定文案兜底）: {e}")
            return None


def _build_prompt(trigger_id: str, context: dict) -> str:
    templates = {
        "late_night": lambda c: (
            f"It is {c.get('hour')} AM. The user is still awake and still using the computer."
            if c.get("type") == "still_up" else
            f"It is {c.get('hour')} AM. The user just woke or opened the computer."
        ),
        "mealtime_skip": lambda c: (
            f"It is {'lunch' if c.get('meal') == 'lunch' else 'dinner'} time, around {c.get('hour')}:00. "
            "The user has been working continuously for over 35 minutes without stopping to eat."
        ),
        "friday_afternoon": lambda c: (
            f"It is Friday afternoon, around {c.get('hour')}:00. "
            "The user just left a work app and entered a low-activity state."
        ),
        "typing_burst_stop": lambda c: (
            f"The user had a high-intensity typing session for {c.get('duration_min')} minutes and then suddenly stopped."
        ),
        "large_delete": lambda c: (
            f"The user wrote {c.get('chars_written')} characters, then deleted {c.get('chars_deleted')} characters within 30 seconds, almost all of it."
        ),
        "save_and_close": lambda c: (
            f"The user just saved {c.get('app', 'a file')} and closed it within 3 seconds."
        ),
        "nano_rejection": lambda c: (
            f"The user rejected Nano's answers {c.get('count')} times within the last 10 minutes and expressed dissatisfaction. "
            "Nano should lower its presence instead of apologizing or continuing to explain."
        ),
        "nano_idle": lambda c: (
            f"Nano has quietly been with the user for {c.get('idle_hours')} hours, and the user has not proactively spoken to Nano."
        ),
    }

    if "holiday_name" in context:
        return f"Today is {context['holiday_name']}. Say one timely line in Nano's own style."

    fn = templates.get(trigger_id)
    if fn:
        return fn(context)
    return f"Trigger: {trigger_id}. Context: {context}. Say one natural line."
