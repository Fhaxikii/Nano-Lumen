# core/proactive/intel/candidate.py
"""
候选生成（种子能力）：把 (episode/事件, scene) 映射到生产力候选。
**硬编码 eligibility，不硬编码人格剧本**——候选只带"目标 + 物料外壳"，最终措辞由
engine 用 LLM 按当时语气渲染。情绪类不单独成候选（情绪只是渲染时的附着层）。
"""
from __future__ import annotations

import uuid

from core.proactive.intel.types import (
    AuthLevel, InterventionCandidate, InterventionType, RiskLevel, Scene,
)

# 种子 eligibility 表：(触发条件) → 候选。utility 是显式收益先验 [原型期标定]。
# goal 是给 LLM 的"要达成什么"，不是成品话术。fallback 是 LLM 不可用时的中性兜底。


def _mk(t: InterventionType, scene: Scene, utility: float, goal: str, fallback: str,
        risk=RiskLevel.READONLY, auth=AuthLevel.L0_NONE, timing=0.6) -> InterventionCandidate:
    return InterventionCandidate(
        type=t, scene=scene, utility=utility, timing_score=timing,
        risk_level=risk, required_auth=auth,
        proposed_action=goal, fallback_message=fallback,
        context_key_str=f"{t.value}|{scene.value}", intervention_id=uuid.uuid4().hex[:12],
    )


def generate(poll: dict) -> list[InterventionCandidate]:
    """poll = salience.poll() 的返回。产出 0..N 个候选。"""
    out: list[InterventionCandidate] = []
    scene: Scene = poll.get("scene", Scene.UNKNOWN)
    events = poll.get("events", []) or []
    closed = poll.get("closed")

    # 1) 刚从空闲【回到】电脑前（一次性 returned 事件，不是"一直空闲"）→ 恢复
    if "returned" in events:
        out.append(_mk(InterventionType.RECOVER, scene, 0.6,
                       goal="The user just returned to the computer. Suggest resuming the unfinished work from before they left. Suggest only; do not take action automatically.",
                       fallback="要接着刚才那个继续吗？"))

    # 2) 保存了东西（写作/编码场景）→ 整理/收尾
    if "save" in events and scene in (Scene.WRITING, Scene.CODING):
        out.append(_mk(InterventionType.ORGANIZE, scene, 0.5,
                       goal="The user just saved the thing they were working on. Suggest preparing a read-only organization pass, such as a checklist or change summary.",
                       fallback="要我顺手整理一版清单吗？"))

    # 3) 高强度输入后停下 → 下一步（写作/编码）
    if "typing_stop" in events and scene in (Scene.WRITING, Scene.CODING):
        out.append(_mk(InterventionType.NEXT_STEP, scene, 0.45,
                       goal="The user just ended a high-intensity typing session. Suggest one concrete next step, such as drafting talking points, running tests, or looking up supporting material. Suggest only.",
                       fallback="歇一下？要的话我可以帮你弄下一步。", timing=0.5))

    # 4) 一段工作结束（episode 关闭、置信够）→ 整理收尾
    if closed is not None and closed.boundary_confidence >= 0.8 and \
            closed.scene in (Scene.WRITING, Scene.CODING):
        out.append(_mk(InterventionType.ORGANIZE, closed.scene, 0.5,
                       goal="The user just ended this work episode. Suggest preparing a read-only summary or checklist of what they just did.",
                       fallback="刚那段我帮你理个摘要？"))

    # 注：large_delete（卡住）不产出"安慰"候选——情绪不单独成主动原因；
    # 若要帮也必须是生产力形态（如查资料），且 utility 低，v0 先不种这条以免噪音。
    return out
