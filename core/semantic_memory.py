# core/semantic_memory.py
"""
持久语义记忆 — 写入触发逻辑（不是存储层，存储见 memory_store.py /
memory_index.py；这里是"什么时候该写、写之前怎么提纯/脱敏"）。

⚠️ **触发面是主动收紧过的**，比设计时设想的窄：
- task_pattern：只在 OS Skill 部署成功时触发，不是任意 Skill 部署都触发。
  原因：task_pattern 最初的动机就是"OS 任务复现检测"，数据
  处理类 Skill 默认本来就是永久的，已经被现有机制服务到了，task_pattern
  对它的边际价值更低，先不接，避免一次性铺太大面。
- correction：只在"Skill报错后修复成功"这条路径触发（_generate_skill_update
  的 error_context 非空且成功部署），不接"用户在普通对话里随口纠正了什么"
  这种更模糊的触发面——后者需要在顶层路由各处加判断点，范围太散，先不做。
"""
from __future__ import annotations
import re
import uuid
from typing import Any, Optional
from loguru import logger


# ── 敏感信息硬拦截（必须是硬代码规则，不能交给模型判断）──────────────────
_SENSITIVE_PATTERNS = [
    (re.compile(r'(密码|password|pwd|账号|账户|token|令牌|验证码|cookie|凭据|secret|api[_-]?key)\s*[:：=]\s*\S+', re.I),
     '[REDACTED_SENSITIVE_INFORMATION]'),
    (re.compile(r'\b[A-Za-z0-9_\-]{20,}\b'), '[REDACTED_POSSIBLE_SECRET_OR_TOKEN]'),  # 长随机字符串，常见token/key形态
]


def redact_sensitive(text: str) -> str:
    """硬代码正则脱敏，不依赖模型判断"这是不是敏感信息"。
    宁可误伤（把不敏感的长字符串也脱敏掉），不能漏判。
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── 检索注入：窄入口，不做成通用工具 ────────────────────────────────────
#    不能挂成伪工具让模型自由调用，只能在固定入口主动查、注入 guide。
#    这两个函数都不调用 LLM，纯向量检索 + 格式化，开销小，适合内联在
#    请求路径里同步调用，不需要像写入那样丢后台。

async def retrieve_task_pattern_hint(query_text: str, top_k: int = 2) -> str:
    """OS多步任务规划前调用。返回一段可以直接拼进 system guide 的提示文本；
    没有命中相关记忆时返回空字符串，调用方不应该因为这个返回空而改变行为。
    """
    try:
        from core import memory_index
        hits = memory_index.search(query_text, memory_type="task_pattern",
                                    top_k=top_k, min_similarity=0.55)
        if not hits:
            return ""
        lines = ["[Historical Task Pattern Hints] Reference only. Do not decide for the user."]
        for h in hits:
            skill_note = f", related Skill: {h['skill_ref']}" if h.get("skill_ref") else ""
            lines.append(
                f"- Similar task handled before: {h.get('display_summary') or h.get('canonical_text')} "
                f"(seen {h.get('hit_count', 1)} time(s){skill_note})"
            )
        lines.append(
            "If the current task is highly similar to one of these records, you may briefly suggest "
            "saving it as a permanent Skill for future reuse. This must be a suggestion only; "
            "do not decide or promote it on the user's behalf."
        )
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[SemanticMemory] retrieve_task_pattern_hint failed; returning empty hint: {e}")
        return ""


async def retrieve_correction_hints(target_skill: str, error_context: str, top_k: int = 2) -> str:
    """工具失败修复前调用。返回一段可以拼进修复prompt的历史纠正经验提示。"""
    try:
        from core import memory_index
        query_text = f"{target_skill} {error_context}"[:500]
        hits = memory_index.search(query_text, memory_type="correction",
                                    top_k=top_k, min_similarity=0.55)
        if not hits:
            return ""
        lines = ["[Historical Correction Hints] Reference only. Do not copy blindly without checking the current context."]
        for h in hits:
            lines.append(
                f"- Condition: {h.get('condition_text', '')}; "
                f"previous wrong assumption: {h.get('wrong_assumption', '')}; "
                f"correct behavior: {h.get('correct_behavior', '')}"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[SemanticMemory] retrieve_correction_hints failed; returning empty hint: {e}")
        return ""


# ── 写入触发：task_pattern（仅 OS Skill 部署成功）──────────────────────────

async def maybe_write_task_pattern(provider, model_override: str,
                                   skill_name: str, description: str) -> None:
    """OS任务完成后调用。失败/异常静默吞掉，绝不影响主流程本身
    （这是个旁路增强，不是关键路径）。
    """
    try:
        from core.memory_store import get_memory_store
        from core import memory_index

        description = redact_sensitive(description or "")

        extraction_prompt = (
            "Extract three fields from the OS automation Skill description below. "
            "Return strict JSON only, with no markdown and no extra text:\n"
            '{"task_intent": "...", "target_entity": "...", "action_outline": "..."}\n\n'
            "Rules:\n"
            "- task_intent: the core intent of the task, as a short verb phrase.\n"
            "- target_entity: the stable category of the operated target, not this run's specific parameter. "
            "For example, use 'sales report' instead of 'May sales report', and 'login flow for a system' "
            "instead of a specific login on a specific date.\n"
            "- action_outline: a short outline of the operation steps, no more than 50 Chinese characters or 80 English words.\n"
            "- If the description contains accounts, passwords, tokens, keys, or other sensitive information, "
            "never include it in the output. Use '[REDACTED]' instead.\n"
            "- Preserve exact Skill names, file names, field names, and business terms when needed.\n\n"
            f"Skill name: {skill_name}\nDescription: {description}"
        )
        content, _ = await provider.chat_without_tools(
            [{"role": "user", "content": extraction_prompt}],
            "You are a precise information extraction assistant. Output JSON only.",
            model_override=model_override,
        )
        import json as _json
        try:
            _jm = re.search(r'\{.*\}', content or "", re.S)
            data = _json.loads(_jm.group()) if _jm else {}
        except Exception:
            data = {}

        task_intent = redact_sensitive(data.get("task_intent") or skill_name)
        target_entity = redact_sensitive(data.get("target_entity") or "")
        action_outline = redact_sensitive(data.get("action_outline") or description[:100])

        canonical_text = f"{task_intent} {target_entity} {action_outline}".strip()
        if not canonical_text:
            return

        store = get_memory_store()
        # 锚点匹配：同memory_type内找候选，不直接全库向量检索（先粗筛省算力）
        candidates = store.find_semantic_candidates("task_pattern", limit=30)
        existing_match = None
        if candidates:
            hits = memory_index.search(canonical_text, memory_type="task_pattern",
                                        top_k=1, min_similarity=0.80)
            if hits:
                existing_match = hits[0]

        if existing_match:
            new_status = "pattern" if existing_match["memory_status"] == "implicit" else existing_match["memory_status"]
            new_confidence = min(1.0, existing_match["confidence"] + 0.25)
            store.bump_semantic_memory(existing_match["id"], confidence=new_confidence,
                                       memory_status=new_status)
            logger.info(f"[SemanticMemory] task_pattern matched existing memory {existing_match['id']}; "
                       f"hit_count+1, status={new_status}")
        else:
            mid = _new_id("tp")
            store.add_semantic_memory(
                memory_id=mid, memory_type="task_pattern",
                canonical_text=canonical_text,
                display_summary=f"{task_intent} ({target_entity})" if target_entity else task_intent,
                task_intent=task_intent, target_entity=target_entity,
                action_outline=action_outline, skill_ref=skill_name,
                condition_text="current task context",
            )
            memory_index.upsert(mid, canonical_text, "task_pattern")
            logger.info(f"[SemanticMemory] wrote new task_pattern {mid}: {canonical_text[:60]}")
    except Exception as e:
        logger.warning(f"[SemanticMemory] maybe_write_task_pattern failed; ignored: {e}")


# ── 写入触发：correction（仅 Skill 报错修复成功）───────────────────────────

async def maybe_write_correction(provider, model_override: str,
                                 target_skill: str, error_context: str,
                                 fix_description: str) -> None:
    """Skill报错→修复成功后调用。失败/异常静默吞掉。"""
    try:
        from core.memory_store import get_memory_store
        from core import memory_index

        error_context = redact_sensitive(error_context or "")
        fix_description = redact_sensitive(fix_description or "")

        extraction_prompt = (
            "Extract three fields from this Skill failure-and-fix description. "
            "Return strict JSON only, with no markdown and no extra text:\n"
            '{"wrong_assumption": "...", "correct_behavior": "...", "condition": "..."}\n\n'
            "Rules:\n"
            "- wrong_assumption: the incorrect approach or assumption. Stay close to the source; do not over-paraphrase.\n"
            "- correct_behavior: the correct approach. Stay close to the source.\n"
            "- condition: the limited scenario where this correction applies. This must never be an empty string. "
            "If the source does not explicitly say this applies globally or by default, provide a scoped condition, "
            "such as 'when processing XX spreadsheets' or 'when calling XX Skill'. "
            "Only write 'general rule, no limitation' when the user explicitly stated a global rule.\n"
            "- If the description contains accounts, passwords, tokens, keys, or other sensitive information, "
            "never include it in the output. Use '[REDACTED]' instead.\n"
            "- Preserve exact Skill names, file names, field names, and business terms when needed.\n\n"
            f"Target Skill: {target_skill}\nError context: {error_context}\nFix description: {fix_description}"
        )
        content, _ = await provider.chat_without_tools(
            [{"role": "user", "content": extraction_prompt}],
            "You are a precise information extraction assistant. Output JSON only.",
            model_override=model_override,
        )
        import json as _json
        try:
            _jm = re.search(r'\{.*\}', content or "", re.S)
            data = _json.loads(_jm.group()) if _jm else {}
        except Exception:
            data = {}

        wrong_assumption = redact_sensitive(data.get("wrong_assumption") or "")
        correct_behavior = redact_sensitive(data.get("correct_behavior") or "")
        condition_text = redact_sensitive(data.get("condition") or f"when calling {target_skill}")
        if not condition_text.strip():
            condition_text = f"when calling {target_skill}"  # fallback: condition must never be empty

        if not wrong_assumption or not correct_behavior:
            return

        canonical_text = f"{wrong_assumption} -> {correct_behavior} ({condition_text})"

        store = get_memory_store()
        candidates = store.find_semantic_candidates(
            "correction", condition_text=condition_text, limit=30
        )
        existing_match = None
        if candidates:
            hits = memory_index.search(canonical_text, memory_type="correction",
                                        top_k=1, min_similarity=0.80)
            # 只在锚点(condition)也匹配的情况下才算同一条
            if hits and hits[0].get("condition_text") == condition_text:
                existing_match = hits[0]

        if existing_match:
            new_confidence = min(1.0, existing_match["confidence"] + 0.25)
            new_status = "pattern" if existing_match["memory_status"] == "implicit" else existing_match["memory_status"]
            store.bump_semantic_memory(existing_match["id"], confidence=new_confidence,
                                       memory_status=new_status)
            logger.info(f"[SemanticMemory] correction matched existing memory {existing_match['id']}")
        else:
            mid = _new_id("cor")
            store.add_semantic_memory(
                memory_id=mid, memory_type="correction",
                canonical_text=canonical_text,
                display_summary=f"{condition_text}: {correct_behavior}",
                correction_subtype="failure_fix",
                wrong_assumption=wrong_assumption, correct_behavior=correct_behavior,
                condition_text=condition_text, skill_ref=target_skill,
            )
            memory_index.upsert(mid, canonical_text, "correction")
            logger.info(f"[SemanticMemory] wrote new correction {mid}: {canonical_text[:60]}")
    except Exception as e:
        logger.warning(f"[SemanticMemory] maybe_write_correction failed; ignored: {e}")
