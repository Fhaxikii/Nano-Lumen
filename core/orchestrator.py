# core/orchestrator.py
from typing import Optional, Any, NamedTuple
from dataclasses import dataclass, field
from core.schema import AgentDecision, ChatMessage, ToolCall, ToolResultBlock
import hashlib
import os
import re
import asyncio
import contextvars
import threading
import time
import traceback
from core import reading as _READING
from loguru import logger
from core import rag as rag_engine
import pathlib
from core.i18n import language_clause as _language_clause
from core.provider import _DISPATCH_LAYER, CACHE_BREAK_MARKER, CLAUDE_MODEL_MAP, CLAUDE_MODELS, GEMINI_MODELS, GEMINI_MODEL_MAP
from core.memory_store import get_memory_store, EntryType
# 统一工具目录 —— 「一个工具是什么」的唯一权威。
# ⚠️ 只 import 类型与目录本身；**内置声明表 `core.tools.builtin` 仍然懒 import**
#    （它要拿本模块里的 `_XXX_MANIFEST` 字面量，模块级 import 会成环）。
from core.tools import (
    Flow, Preload, Scheduling, ToolCatalog, ToolOrigin, ToolScope,
)


# ══════════════════════════════════════════════════════════════════════════
# ReAct 主循环 — 数据结构
# ══════════════════════════════════════════════════════════════════════════

def _main_model_is_blind() -> bool:
    """主模型在不在本厂商的 `vision` 白名单里 —— 不在就是"盲"。

    📌 判据用现成的角色槽白名单，不去猜型号名里有没有 "vision"。
    ⚠️ 取不到就当**不盲** —— 退回老路总好过误判后白绕一圈。
    """
    try:
        from core.models import role_pool
        from core.provider import provider as _p
        main = getattr(_p, "target_model", "") or ""
        return bool(main) and main not in role_pool(main, "vision")
    except Exception:
        return False


def _vision_model_for_os() -> Optional[str]:
    """OS 层视觉定位（computer use 看屏幕）该用哪个模型。

    📌 与 `core/rag.py:_vision_model()` 同一条判据：视觉是独立角色槽，
       `vision_for()` 空串 = 退回主模型（返回 None 让下游沿用默认）。
    ⚠️ 这个参数一直存在，但两个调用点长期传 None ⇒ 视觉恒用主模型。
       在只有 Claude 一家、且全系都有视觉时看不出问题 ——
       换成"某些型号有视觉、某些没有"的厂商时才会卡死。
    """
    try:
        from core.models import vision_for
        from core.provider import provider as _p
        main = getattr(_p, "target_model", "") or ""
        v = vision_for(main)
        return v or None          # None = 沿用主模型
    except Exception:
        return None



@dataclass
class StreamDecisionState:
    """_stream_decision_core 的局部结果容器，避免实例属性在并发场景下被覆盖。"""
    decision: Any = None
    model: str = "UNKNOWN"
    notice: str = ""
    block_started: bool = False
    block_id: Optional[str] = None
    answer_bid: Optional[str] = None


@dataclass
class ToolExecution:
    """单次工具执行结果。"""
    call: ToolCall
    result_text: str
    ok: bool = True
    raw_result: Any = None
    tool_data: Any = None
    error: str = ""


class _ToolRuntimeView:
    """`availability` 判据能看到的运行时事实 —— **只读的窄接口**。

    ⭐ 三个判据都不是新写的，是把**原来散在 `_build_skills_info` 里的三个 if**
       搬到一处：
         · `has_live_work()`        ← `if _rt_has_live_work(self)`（给 `task_boundary`）
         · `is_recheck_round()`     ← `if getattr(self, "_recheck_sid", "")`（给 `set_next_checkin`）
         · `has_open_interaction()` ← 未决交互存在时才注入（给 `answer_open_interaction`）

    📌 判据出处（改造前就写在 `_rt_ongoing_work` 的注释里）：
       **一个工具和它的事实来源，必须由同一个条件控制** ——
       「有工具没事实」或「有事实没工具」两种半截状态都会让**模型开始猜**。
    ⚠️ 三个方法都吞异常返回 False：**算不出来就不给**（多一个工具的代价是模型乱调，
       少一个只是这一轮用不上它）。
    """

    __slots__ = ("_o",)

    def __init__(self, orch):
        self._o = orch

    def has_live_work(self) -> bool:
        try:
            return bool(_rt_has_live_work(self._o))
        except Exception:
            return False

    def is_recheck_round(self) -> bool:
        try:
            return bool(getattr(self._o, "_recheck_sid", ""))
        except Exception:
            return False

    def has_detachable_carrier(self) -> bool:
        """此刻有没有一个**还活着的、可以被交出去/停掉的**慢调用。

        ⭐⭐ [2026-08-22] 它补的是 `is_recheck_round` 够不着的那一半：
           交还发生在一轮的中途，而用户说「把这个放后台」/「把它停掉」
           **必然是下一轮**，那一轮既不是回看轮、也没有中途注入的机会。
        🔴 实测后果（两条）：
           · `dont_wait` 收到「没有交还给你的慢调用」，而那件事明明在跑
           · `stop_background` 压根不在工具表里 → 模型改用 `taskkill` 按**进程名**杀，
             把不相干的同名进程一起杀了
        📌 那条：**一个工具和它的事实来源，必须由同一个条件控制。**
           载体现在能活过一轮了（见 `_handle_query_impl` 里那段），
           工具的出现条件就必须跟着它走 —— 否则又是「有事实没工具」。
        ⚠️ 只认**活着**的：记录点存在不等于对象还在。
        """
        try:
            _c = getattr(self._o, "_detachable_carrier", None) or {}
            _wid = _c.get("wait_id")
            if not _wid:
                return False
            from core.runtime.kernel import get_kernel as _gk_av
            from core.runtime import waitcond as _wc_av
            _r = _wc_av.find_by_id(_gk_av(), _wid)
            return bool(_r is not None and _r.is_live)
        except Exception:
            return False

    def has_open_interaction(self) -> bool:
        try:
            return bool(_rt_live_interactions(self._o))
        except Exception:
            return False

    def has_evicted_history(self) -> bool:
        """这个会话里有没有"已经移出上下文"的交换。

        ⭐ 与 `note_image` 同一条纪律：**一个工具和它的事实来源由同一个条件控制。**
           没有 L3 记录时 system 里一条索引都没有，那时给模型一个
           `recall_conversation`，它只会拿它去猜。
        """
        try:
            from core.context.decay_store import DecayStore, L3
            from core.runtime.kernel import get_kernel
            _sid = self._o.memory.conversation_session_id
            if not _sid:
                return False
            rows = DecayStore(get_kernel().store).active_entries(_sid)
            return any(str(e.get("level")) == L3 and e.get("index_entry")
                       for e in rows.values())
        except Exception:
            return False

    def has_unsummarized_image(self) -> bool:
        """**这一轮**有图、且还没记下来。

        ⚠️⚠️ 读的是**本轮标志** `_turn_image_pending`，**不是 memory 的最后一条**。
           理由（实测栽过两次）写在 `_handle_query_impl` 顶部那段注释里：
           核心工具清单在用户消息进 memory **之前**就算完了，
           而 ReAct 循环里 `storage[-1]` 又会变成 `tool_results`。
           📌 **判据不能挂在「storage 的最后一条是什么」上** ——
              那个位置一轮之内会变好几次，而"这一轮有没有图"只有一个答案。

        ⭐ **同一个判据同时控制三样东西**：`note_image` 在不在工具清单里、
           那段一次性提示注不注入、记完之后两者一起消失。
        📌 那条：**一个工具和它的事实来源，必须由同一个条件控制。**
        ⚠️ 天然一次性：`note_image` 一落，标志翻假。
           用户提醒的「摘要千万不能像 base64 那个 bug 一样每轮注入」——
           防线不是"记得别注入"，是**它没有第二轮可注入**。
        """
        try:
            return bool(getattr(self._o, "_turn_image_pending", False))
        except Exception:
            return False


class ToolOutcome(NamedTuple):
    """工具 handler 的统一返回契约。

    ⭐ AST 实测（提取前）确认：`_execute_one_tool_call` 的 name 分派链**之后**
       只读 `result_text` 一个变量，所以 handler 的完整契约就是
       「产出文本 + 这次算不算失败」。其余（`tool_end` 事件、日志、
       `ToolExecution` 组装、错误分类）一律留在统一 plumbing 里。

    📌 **允许 handler 直接返回 `str`**（绝大多数工具不会失败），
       由 `ToolOutcome.of()` 归一 —— 与项目既有的 `SkillResult.from_raw` 同一范式：
       **一个便利构造器，不是两种并存的契约。**
    """
    text: str
    failed: bool = False

    @staticmethod
    def of(value: Any) -> "ToolOutcome":
        if isinstance(value, ToolOutcome):
            return value
        return ToolOutcome(str(value) if value is not None else "")


# ══════════════════════════════════════════════════════════════════════════
# 显式思考流：用户消息层protocol注入文本（2026-06-21）





# ══════════════════════════════════════════════════════════════════════════
# Skill 开发协议 v3.2 — 注入 system prompt，指导模型生成合规代码
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ **v3.2（2026-08-29）**：两件事一起做了。
#
# ① 🔴 修掉一个一直存在的**版本号自相矛盾**：
#      协议自身（这个常量）自称 v3.1、UI 三处显示 v3.1、模型读到的也是 v3.1，
#      **只有校验器的报错信息和摘要写着 v3.0** —— 同一个协议同时自称两个版本。
#      📌 **一个版本号如果只出现在报错信息里，它多半是所有人都忘了改的那一份。**
#
# ② SkillSpec 删掉了五个 Plan 编排器时代的字段
#    （required_context_level / atomic_action / source_hint /
#      planning_allowed / fail_if）。那个编排器早已删除，字段却留了下来 ——
#    每建一次 Skill 都要模型认真填两个**不产生任何行为差异**的枚举。
#    📌 **删一个契约字段，要么留兼容层，要么把版本号推上去** ——
#       两者都不做，下次有人拿旧模板生成 Skill 会炸得莫名其妙。
#    ⭐ 这次敢真删是因为 已明确「没有用户已经生成的 skill，用户只有我自己」，
#      官方 Skill 和四个模板已一并改干净。
_SKILL_PROTOCOL = '''
[Nano Skill Development Protocol v3.2]

You are Nano SkillWriter. At this stage, SkillSpec has already been generated and validated.
Your task is to generate complete Python Skill code according to the SkillSpec.

[Single Skill Principle]

One Skill corresponds to one task. Do not split the requirement into multiple dependent Skills
that must be called in sequence. If the requirement naturally contains multiple steps — for example
reading a file and then computing something — put all of them into the same Skill's run() method,
in order.

--- Hard Requirements ---
Violating any item means the code is invalid.

1. Required import:
   from core.schema import BaseSkill, SkillResult, SkillSpec, InputDef, ContextLevel, SideEffect, PermissionLevel, Lifecycle

2. The class name, self.name, and SkillSpec.name must be exactly the same.

3. You must implement get_manifest(). Its return value must have this EXACT top-level shape:

   def get_manifest(self) -> Dict[str, Any]:
       return {
           "name": "SkillName",                 # top level, same as the class name
           "description": "what it does",        # top level
           "parameters": {                       # top level
               "type": "object",
               "properties": {...},
               "required": [...],
           },
       }

3.0 ⚠️ FORBIDDEN: do NOT wrap it in an outer envelope. This exact mistake has already
    shipped a broken Skill once, so it is called out explicitly:

      WRONG (OpenAI-style wrapper — the name is nested and will not be found):
        return {"type": "function", "function": {"name": ..., "parameters": ...}}

      WRONG (Gemini-style list):
        return {"function_declarations": [{"name": ...}]}

      RIGHT: "name" / "description" / "parameters" sit at the TOP level, as shown above.

    Why this matters more than it looks: the loader reads the top-level "name" to register
    the tool. If it is nested, the whole manifest is discarded — the Skill still installs,
    still shows READY in the UI, and the file is on disk, but **the model never sees the tool
    and cannot call it**. Nothing appears broken. Get the outer shape right.

3.1 get_manifest().parameters.properties must exactly match get_spec().required_inputs
    and optional_inputs if any. This is mandatory and is the most commonly missed item.

    - Every InputDef.name in required_inputs must appear in parameters.properties.
      The type mapping must be correct, and the name must also be included in parameters.required.
    - If this is missing, Claude will not know the real parameter names. It may invent names,
      causing TypeError: unexpected keyword argument or missing required argument.
      The Skill may look correct in code but fail randomly whenever called.
    - After writing code, self-check:
      compare the name list in get_spec().required_inputs with the key list in
      get_manifest().parameters.properties. They must match exactly in count and spelling.

3.2 All JSON schema type values in get_manifest() must be lowercase.

    Correct:
      "type": "object", "type": "string", "type": "integer", "type": "boolean"

    Wrong:
      "type": "OBJECT", "type": "STRING"

    Uppercase types can cause Claude API 400 errors.
    The top-level parameters type must be exactly "object", not "OBJECT".

4. You must implement get_spec() and return SkillSpec using the exact field names below.
   Wrong field names will crash at runtime.

   def get_spec(self) -> SkillSpec:
       return SkillSpec(
           name="SkillName",            # exactly the same as the class name
           purpose="one-sentence purpose",  # must be purpose, not description
           required_inputs=[...],
           optional_inputs=[],
           data_output_keys=["key1"],
           side_effects=[SideEffect.NONE],
           permission_level=PermissionLevel.READONLY,
           lifecycle=Lifecycle.PERMANENT,
       )

   Forbidden fields:
   - description=
   - context_level=

   These fields do not exist and will cause unexpected keyword argument errors.

5. The core method must be async def run(...), and it must return SkillResult, not str.

6. run() must have a docstring.

7. SkillResult.data must contain every key declared in SkillSpec.data_output_keys.

8. Wrap all synchronous blocking IO with asyncio.to_thread().

8.1 Import self-check:
    If the code uses any module such as asyncio, os, re, json, or pandas, the file must import it at the top.
    After writing code, scan every "module.function()" call inside run() and verify that the corresponding import exists.
    This is especially important for asyncio.to_thread(); if used, import asyncio is mandatory.

8.2 If this Skill can take a long time, report progress with self.report_progress().

    When does this apply? Any Skill that loops over many items, downloads, installs,
    parses large files, or waits on something slow. If run() can plausibly take more
    than about a minute, it applies.

    self.report_progress("parsing file 3 of 40")
    self.report_progress("downloading", progress=42, total=100)   # renders as [42%] downloading

    Why this matters — and it matters more than it looks:
    After a Skill has been running for a while, the runtime hands control back to you
    (Nano) and later wakes you up to LOOK AT how that Skill is doing. That look shows
    exactly what report_progress() reported, and nothing else. A Skill that never calls
    it can only be described as "still running, no idea how it is going" — so when you
    later have to tell the user whether to keep waiting, you will have nothing to say.

    Rules:
    - Report before each slow step, not after. A line that appears only on completion
      is useless during the run.
    - Report what is happening, with a number when a number exists.
      Good: "installing torch (312 MB downloaded)". Bad: "working".
    - Never report a percentage you did not actually compute. If the total is unknown,
      pass progress= without total=, or just describe the step.
    - It is fire-and-forget: no return value, never raises, never blocks.
      Do not wrap it in try/except and do not await it.
    - Inside asyncio.to_thread() it works normally. Inside a bare threading.Thread it
      does not — if you spawn raw threads, report from the coroutine instead.
    - Short Skills (a few seconds) should NOT call it. It would be noise.

9. Clean exception text by removing newlines:
   str(e).replace(chr(10), ' ')

10. SkillResult.text must be a complete natural-language description.
    It must not be empty or vague.

    - On success, text must include the result in a way the user can immediately understand.
      Do not only say "done".
    - On failure, text must include the specific reason.
      Do not only say "failed".
    - User-facing text should follow the user's language when the language is obvious.

--- Critical: SkillResult.text Rules ---

text is the model's only source of information when replying to the user.
If text is unclear, the user will not know what happened.

A. File-operation Skills
   If side_effects includes FILE_WRITE, FILE_READ, or FILE_DELETE,
   text must include the full absolute path.
   Use os.path.abspath() or pathlib.Path.resolve().

   Wrong:
     text="Appended to nano_test.txt"

   Required:
     text=f"Appended to {os.path.abspath(file_path)}"

B. Query/data-return Skills
   If the Skill returns time, numbers, lists, tables, or other values,
   text must include the key result value, not an empty success phrase.

   Wrong:
     text="Query successful"

   Required:
     text=f"Current time: {formatted_time}"

C. Network/API Skills
   If side_effects includes NETWORK or EXTERNAL_API,
   text must include the requested URL or service name, plus the key returned result.

   Wrong:
     text="Request completed"

   Required:
     text=f"Requested {url}, status code {status_code}"

D. Sending/message Skills
   If side_effects includes SEND_MESSAGE,
   text must include the target and a summary of the sent content.

--- Enum Usage Rules ---
Use uppercase enum class attributes exactly as below.

ContextLevel:
  NONE / FRAGMENT_OK / FULL_REQUIRED / FILE_PATH_REQUIRED / GENERATED_OK / WEB_REQUIRED

SideEffect:
  NONE / FILE_READ / FILE_WRITE / FILE_DELETE / NETWORK / EXTERNAL_API / SHELL / SEND_MESSAGE / OS_CONTROL

PermissionLevel:
  READONLY / WORKSPACE_WRITE / NETWORK_ALLOWED / EXTERNAL_ACTION / DANGEROUS

Lifecycle:
  PERMANENT / EXPERIMENTAL

--- Output Requirements ---

Use the WriteSkill tool. All three fields are mandatory:

- filename: class name without .py
- code: complete Python code satisfying all requirements above
- description: one-sentence functional description

--- User-Facing Language Rule ---

A Skill's text has TWO different audiences. Which audience a field serves decides its language - never mix them.

MODEL-FACING (always English, regardless of the user's language):
- get_manifest()["description"]
- every "description" inside get_manifest()["parameters"]
These strings are injected into the tool schema and the awareness list. They are read by
the model when it decides which tool to call - they are never shown to the user.
Write them as trigger conditions in English: what it does, and when to call it.

USER-FACING (follow the language of the user's original Skill request or exploration summary):
- WriteSkill.description
- SkillSpec.purpose            <- this is what the user sees in the Skill detail panel
- SkillSpec.not_responsible_for
- SkillResult.text
- Any status, success, failure, or explanation text returned by run()

Do not force these user-facing strings into English. If the user requested the Skill in Chinese, use Chinese. If the user requested it in English, use English.

Because the two audiences are separate, purpose must stand on its own: the user only ever
sees purpose, never the manifest description. Do not write purpose as a translation of the
description - write it as a plain-language explanation of what this Skill is for.

Do not translate code identifiers, filenames, column names, field names, enum values, or exact business terms confirmed during exploration.

Official built-in Skills are not covered by this rule.

--- Special Rule A: Optional Parameter Descriptions ---

For optional parameters, the description should describe only what the parameter is.
Do not include concrete format examples unless the parameter itself requires a format constraint.

Reason:
Models may treat example values in optional parameter descriptions as default values,
which can cause wrong tool calls.

Wrong:
  "Optional time format, such as HH:mm or yyyy-MM-dd"

Correct:
  "Optional Python strftime format string. Must contain % symbols. If omitted, returns full ISO 8601 datetime."

For strftime or formatting parameters, the description must clearly say that the value must contain % symbols.

--- Special Rule B: Real-Time Data Skill Descriptions ---

If a Skill returns information that the user cannot know from common knowledge, such as current time,
current status, or real-time data, get_manifest().description must include a mandatory-call statement.

Examples:
  "Get the current system time. When the user asks about time or date, this tool must be called. Do not estimate from training data."

  "Query current weather. This tool must be called to obtain real-time data. Do not guess from common knowledge."

Without this statement, the model may answer from training memory instead of calling the tool,
causing incorrect results.

--- Special Rule C: Confirmed Information Must Be Hardcoded; Do Not Invent Structure ---

If the requirement contains "information confirmed during exploration", such as column names,
field names, thresholds, filters, decision standards, formulas, or other concrete values,
these are real business rules already aligned with the user.
The generated code must follow them strictly.

1. Column names and field names

   If the requirement includes a list of involved fields using original column names,
   DataFrame or dictionary access in code must use those exact original names.

   Preserve spelling, case, units, symbols, and parentheses.

   Do not invent a column name that seems more general or business-friendly.

   Example:
   - If the confirmed field is "fyc", do not use "FYC" or "total_fyc".
   - If the confirmed field list does not include "policy_count", do not invent "policy_count".

   If a desired field is not in the confirmed field list, that field has not been verified.
   Do not use it directly. Use confirmed fields instead, or explain in SkillResult.text
   that this part cannot be handled because the field was not confirmed.

2. Thresholds and decision standards

   Concrete thresholds and standards must be written directly as constants or conditions in code.

   Do not turn these concrete values into required_inputs or optional_inputs in SkillSpec,
   even if the code-generation stage sees such parameters in the draft SkillSpec.

   The requirement's concrete values take priority.
   Use them inside run() directly.

   If needed, remove those parameters from the function signature and update get_spec()
   and get_manifest() accordingly, so all three stay consistent.

3. Filters and exclusion conditions

   If the requirement explicitly says to exclude records, such as records where a field equals a certain value,
   the code must actually implement that logic with a filter or mask.

   Do not omit it.
   Do not replace it with a comment.

3.1 Real representations of values and empty values

   Any concrete value used in conditions, such as what counts as "empty" or "qualified",
   must come from the exploration summary.

   Do not assume pandas or Python default behavior.

   Typical mistake:
   The exploration summary says the field "product_type" is displayed as the string "!" when empty,
   but the code uses:

     df["product_type"].isna() | (df["product_type"] == "")

   This assumes pandas-style missing values and does not match the real data representation.
   It may make the condition always false or always true, causing the filter logic to fail silently.

   Correct approach:
   Use the original value confirmed during exploration, for example:

     df["product_type"] == "!"

   If the exploration summary does not provide the real value needed for a condition,
   do not guess based on library defaults. Explain in SkillResult.text that the basis is unclear.

3.2 Return data by default; do not write files by default

   Unless the user explicitly asks to save, export, generate a file, or write to local disk,
   the Skill should return the processed result through SkillResult.data.

   For table-like results, return a list[dict] so the frontend can render it directly.

   Do not call to_excel(), to_csv(), or other file-writing methods by default.

   Reason:
   The user usually only wants to see the result in the conversation.
   Writing a file every time may be unnecessary and will force side_effects to include FILE_WRITE,
   causing an avoidable execution confirmation popup.

   Write a file only when:
   - The user explicitly asks to save or export.
   - The result is too large to return directly, such as thousands of rows.

   If neither condition applies, return:

     SkillResult(data={"output_key": list_of_dicts})

   Do not add an output file path unnecessarily.

4. Final self-check after writing code

   Check both items below:

   a) Column and field names:
      Every column or field name string used in code must appear in the requirement's
      confirmed exploration information.

      If any column name was invented by you and does not appear in the confirmed information,
      it is wrong. Replace it with the real confirmed name.

   b) Condition values:
      For every condition such as:
      - field == value
      - field.isin([...])
      - field.isna()

      Check whether the value or empty-value assumption has original evidence in the confirmed
      exploration information.

      If it was assumed by you, such as assuming empty means NaN or "",
      replace it with the real confirmed value, or clearly state that it cannot be determined.
'''

# ── WriteSkill 伪工具声明 ────────────────────────────────────────────────
# ⭐⭐ UI 消费循环遇到这些事件就 `return` —— 排在它们后面的任何 yield 都进不了界面。
#
# ⚠️ 这份清单**被猜错过两次**：
#   第一次只写了 `final_result` → `create_new_skill` 的卡片一直转圈
#     （它结束在 `skill_preview` 上）；
#   第二次补了 `sys_error` 仍然漏 `skill_preview` / `user_note_pending`。
#
# 所以不再手抄，而是用 `tests/t_exit_tool_card.py` 的 AST 不变量把它和
# `app.py` 里真实的 `if step.get("event") == X: … return` 绑死 ——
# 以后谁加第五个终端事件，测试会先红。
# 📌 同 那条判据：**常量表必须能被证明等于真实分发链，而不是靠人记得同步。**
_UI_TERMINAL_EVENTS = frozenset({
    "final_result",
    "sys_error",
    "skill_preview",
    "user_note_pending",
    # ⭐ 第五个（2026-08-08，无缝对话的协作式中断）。
    #    ⚠️ **它就是上面那句「以后谁加第五个终端事件，测试会先红」的兑现** ——
    #       加完 app 侧那个 `return` 分支就忘了这张表，
    #       `t_exit_tool_card` 的 AST 不变量当场红了。
    #    📌 那条不变量比它的作者预期的更值 —— 它不只防「手抄漏了」，
    #       还防「**加功能的人根本不知道有这张表**」。
    "turn_interrupted",
})

_WRITE_SKILL_MANIFEST = {
    "name": "WriteSkill",
    "description": (
        "Use this tool when the user asks to create, add, or develop a new local Skill. "
        "Generate complete Python Skill code according to Nano Skill Development Protocol v3.2 and output it through this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Skill class name and filename without .py, such as WeatherQuery."
            },
            "code": {
                "type": "string",
                "description": "Complete Python Skill code that follows Nano Skill Development Protocol v3.2."
            },
            "description": {
                "type": "string",
                "description": (
                    "One-sentence user-facing description shown in the audit/confirmation window. "
                    "Use the same language as the user's Skill request."
                )
            }
        },
        "required": ["filename", "code", "description"]
    }
}


# ══════════════════════════════════════════════════════════════════════════
# 探索阶段的工具契约 —— manifest 与 dispatcher 必须是同一份
# ══════════════════════════════════════════════════════════════════════════
#
# 事故背景：2026-08-05 实测死路。清点结果触目惊心：
#
#     探索阶段 manifest        = 12 个工具
#     探索循环真正有 handler   =  6 个
#     **无 handler**           =  6 个  ← 全部是 permanent Skill
#
# 也就是说一半的工具，模型只要调任何一个，就掉进"链尾未处理的 call"兜底。
# 而那次的需求恰好是"日期 + DNS"，`GetSystemTime` 和 `GetDNSList` 就明晃晃
# 摆在它的工具清单里。**这条路根本不需要任何上下文污染就能触发。**
#
# 根因是 `_build_skills_info()` 第一行就 `list(self.registry.get_permanent_manifests())`，
# 无条件把所有已注册 Skill 塞进去 —— 那对主决策循环是对的（它能执行任何 Skill），
# 对探索阶段是错的（它只有六个只读分支）。
#
# ⚠️ `os_execute` 同样**没有 handler**，但探索提示词里曾写着
# "read-only os_execute actions for OS tasks"（已改）。`include_os=True` 时
# 它会进 manifest 然后死在同一个地方。这条路目前实际不可达
# （没有任何调用方传 include_os=True），但代码在那里就是个陷阱。
#
# 下面这个集合是**唯一权威**：
#   - 运行时用它过滤 manifest（`_build_skill_exploration_tools`）
#   - 测试用 AST 解析 `_run_skill_exploration` 的分发链与它比对
#     （`tests/t_d13_explorer_scope.py`），任一侧改了另一侧没跟就红
#
# 加新工具的正确姿势：**先写 handler，再加进这个集合，最后测试自然放行。**










# ── OS Skill 代码生成阶段契约（SkillSpec 生成 + 代码生成都要看到）──
_OS_SKILL_WRITER_CONTRACT = """

[OS Skill Code Contract — Mandatory]

This is an OS Skill. Its run() method must NEVER directly call pyautogui, win32gui, win32api,
subprocess, ctypes, or any other library that can operate the mouse, keyboard, windows,
system settings, or OS directly. That is the responsibility of the OS execution layer, not the Skill.

run() does only one thing:
Given params, which may include os_context describing the current screen or failed step,
decide the next planned OS steps and return a SkillResult.

SkillResult.data must contain "dsl_plan": a JSON array where each item looks like:
{"action": "click", "params": {"target": "OK button"}, "declared_risk": 2, "reason": "submit form"}.

action must be one of this closed set:
screenshot/get_sysinfo/read_registry/read_window_tree/list_windows/
get_cursor_pos/wait/list_dir/click/double_click/right_click/drag/type_text/
hotkey/scroll/win_minimize/win_close/win_switch/launch_app/set_volume/
file_read/file_write/clipboard_read/clipboard_write/open_url/kill_app/
run_command/write_registry/file_delete/file_move/manage_service/
set_env_var/schedule_task/modify_startup/network_config.

Windows hides file extensions by default. When a user-provided filename may omit the real extension,
such as "New Text Document (2)", the first dsl_plan step for delete/move/write operations must be list_dir
on the target directory. Only after confirming the real filename may later steps use the exact full filename.
Do not guess extensions inside run().

Diagnostic tasks:
For requests like "check why X is not working", the first part of dsl_plan must contain only read-only
actions with declared_risk=1. Add risk=2/3 repair actions only after a specific cause is identified.
Do not mix diagnosis and repair from the beginning.

SkillSpec requirements:
- In generated Python code, side_effects must include SideEffect.OS_CONTROL.
- In SkillSpec JSON/dict form, side_effects must include "os_control".
- data_output_keys must include "dsl_plan".
- optional_inputs must include an InputDef named "os_context" with type "string".
- run() should accept os_context: str = "".
  On the first call it is empty. If execution fails and replanning is needed, the system will pass
  a JSON string into os_context containing completed steps, failed step, failure reason, and current state.
  run() must parse this JSON when present; if empty, treat it as initial planning.

Do not implement low-level retry loops, sleep loops, or direct execution retry logic.
The outer system owns execution and retry. run() should be a pure planning function that uses os_context
to decide the next dsl_plan.

"""


# ── [回看设计 2026-08-09] 「下一次什么时候回来看」──────────────────────────
# ⭐⭐ **系统只设第一次回看，之后全归模型** —— 这就是那个「归模型」的落点。
# 📌 常量只该承担系统答得出的那个问题（「多久之后开始怀疑」）；
#    **「这件事还要多久」只有模型能答**，而任何固定数字都覆盖不了真实情况。
#
# ⚠️ **条件注入**：只在「回看轮」出现（那一轮才有可回看的对象）。
#    常驻会让模型在没有任何后台任务时去调它 —— 同 `task_boundary` 那条：
#    📌 一个只在某种状态下才有意义的工具，应该只在那种状态下出现。
#
# ⚠️ `seconds` 省略 / <=0 → **不再回看**（等完成信号）。刻意不用一个魔数表达它：
#    📌 「不再做这件事」不该靠一个特殊数值表达，那种约定迟早被误用。
_SET_NEXT_CHECKIN_MANIFEST = {
    "name": "set_next_checkin",
    "description": (
        "Decide when (or whether) to look at this background job again.\n"
        "Call this once, after you have judged how it is going.\n"
        "  · seconds = a number  -> look again after that many seconds. "
        "Prefer a LONG gap when it looks healthy: if you expect roughly ten more "
        "minutes, ask for ten minutes, not one. Every check-in costs a full turn.\n"
        "  · seconds omitted     -> do NOT look again; the completion signal will "
        "wake you. Use this when it looks nearly done.\n"
        "If you say nothing at all, a long default is used — so calling this is only "
        "needed when you want something different from that.\n\n"
        "On a check-in, decide first, then give at most one user-facing status conclusion. "
        "If this call adds no new fact, do not repeat the same progress both before and after it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "integer",
                "description": (
                    "Seconds until the next check-in. Omit to stop checking in."
                ),
            },
        },
        "required": [],
    },
}


# ── 「这个调用我不等了」──────────────────────────────────────────────
#
# ⭐⭐⭐ **这是「抛后台」的落点，而它的主语是【一次调用】，不是一件 Task。**
#
# 🔴 早先写的是「主语必须是 Task，别做成模型申报某个调用转后台」。
#    2026-08-1x 推翻了它，理由很硬：**没有办法定义「哪一种 Task 值得
#    进后台」** —— 那是个概念，落不了地。而「这个调用我等不等」是模型此刻
#    真的答得出的问题。
#
# ⭐ 与早先那条约定**不冲突**，两者答的是不同的问题：
#      系统答「**谁在跑**」   —— 事实，不需要模型申报（那条约定管的是这个）
#      模型答「**我还等不等**」—— 取决于「下一步依不依赖它」，只有模型知道
#    📌 一个被否掉的申报，否的是它申报的**内容**，不是「模型不许说话」。
#
# ⚠️⚠️ **`next_step` 必填，而且它就是整个判据。**
#    用户的三种情况，分界线全在这一个参数上：
#      ① pip 完才能改代码（有依赖）→ 下一步就是等它 → **填不出别的** → 不该调
#      ② pip 与改代码无关         → 下一步是「改代码」 → 填得出   → 该调
#      ③ 只有 pip 这一件事         → 下一步就是等它     → 填不出   → 不该调
#    📌 **「不等它」只有在「我有别的事要做」时才成立** ——
#       把那个前提做成一个必须写下来的字段，模型就没法含糊过去。
#    ⭐ 这也是实测抓到的滥用（模型逢长任务就 `dont_wait`）的修法：
#       不是在描述里写「别乱用」，是**让它乱用时无话可填**。
#
# ⚠️ 第二条实测教训：模型会把「回头看看它好没好」当成 `next_step` ——
#    那等于什么都没说（它还是在等它），于是 `dont_wait` 白调一次。
#    → 描述里**显式**告诉它：系统会在那件事结束时叫你，你不需要安排去看它。
#    📌 **一个「必须填别的事」的字段，必须同时说清什么不算「别的事」** ——
#       否则模型会用一个语法上合法、语义上等价于「我还是在等」的答案绕过去。
_DONT_WAIT_MANIFEST = {
    "name": "dont_wait",
    "description": (
        # 🔴🔴 **实测 2026-08-20：模型看得见这个工具，却直接跳过它。**
        #    日志：`dont_wait:1070` 在工具表里，而它转头就调了 `edit_file`。
        #    📌 根因是**上一版描述用模型侧的语言说了一件系统侧的事**：
        #       第一句写的是 "Stop waiting for the slow call" ——
        #       可控制权**已经交还给它了**，它主观上根本没在等，
        #       于是这个动作在它看来什么也不改变，自然跳过。
        #    ⭐ 「等 / 不等」的差别只存在于**系统这一侧**（排不排回看、
        #       进不进抽屉），所以描述必须换成**它能据以行动的后果**：
        #       「别在 60 秒后打断我」。那是它自己在乎的事。
        "Hand a slow call that is already running over to the runtime, so it "
        "stops interrupting you about it.\n"
        "\n"
        "By default, after a slow call is handed back to you, the runtime will "
        "pull you back in about a minute just to look at it again, and keep "
        "doing that until it finishes. Call `dont_wait` and that stops: it "
        "moves into the user's task drawer, and you are woken up once, when it "
        "is actually done.\n"
        "\n"
        "Call it BEFORE you start the other work - otherwise you will be "
        "interrupted in the middle of it.\n"
        "\n"
        "Call it ONLY when you genuinely have other work you can do right now "
        "that does NOT depend on that call's result. If your next move is to "
        "wait for it, check it, or use its result, do NOT call this tool - "
        "those periodic look-ins are exactly what you want then.\n"
        "\n"
        "`next_step` must name that other work. These do NOT count as other "
        "work: 'check on it', 'look at it again', 'wait for it', 'see if it "
        "finished', 'report progress to the user'. The runtime tells you when "
        "it is done - you never have to schedule a look."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "next_step": {
                "type": "string",
                "description": (
                    "One short line naming the OTHER work you are going to do while "
                    "it runs - concrete, and independent of that call's result. "
                    "If you cannot name one, do not call this tool."
                ),
            },
        },
        "required": ["next_step"],
    },
}


# ── 「一件事」的边界工具 ──────────────────────────────────────
# ⭐⭐ **为什么必须有这个工具**：Task 的边界是「一个目标达成或放弃」，
#    而那**只有模型知道** —— 三条各自独立的结论都指向这一点
#    （迭代阅读的 scratchpad / 仅本次 MCP / per-task 成本）。
#    代码能给的只有默认值（复用当前那个），说不出「这件事完了」。
#
# ⚠️⚠️ **「继续当前这件事」刻意【不是】一个动作** —— 那是默认。
#    📌 同（`UNRELATED` 不是工具参数，它等于不调用工具）：
#       **默认不需要动作，只有偏离默认才需要动作。**
#       给「继续」一个参数，等于每轮都要模型表态一次，纯烧 token 还多一次犯错机会。
#
# ⚠️ **两个动作分开，不许捆成一个** ——「另开一件事」默认**不结束**旧的。
#    早先的设计原话：「我们的**并不要求 x 和 y 一定相关**」——
#    用户在装库跑着时插一句「先做别的」，那两件事**并存**。
#    📌 捆死之后，模型想表达「并存」就没有说法了。
_TASK_BOUNDARY_MANIFEST = {
    "name": "task_boundary",
    "description": (
        "Declare that a piece of work is finished, or that the user's new request is a "
        "DIFFERENT piece of work.\n"
        "Nano groups long-lived things (timed waits, queued messages, one-off "
        "authorizations, per-task cost) under 'a piece of work' that can span many "
        "turns. Only you can tell when one is done.\n"
        "\n"
        "Call it ONLY when something changes:\n"
        "  · finish  — that work is complete, or the user dropped it\n"
        "  · start   — the user's request is a different piece of work "
        "(the current one is set aside automatically, NOT finished)\n"
        "  · resume  — go back to one that was set aside "
        "(needs its `task_id`)\n"
        "\n"
        "Setting one aside never stops anything: background jobs, timed waits "
        "and agents under it keep running. It only means YOU are not on it now.\n"
        "This tool is about WHICH PIECE OF WORK you are on. It is NOT how you "
        "stop waiting for a slow call - that is `dont_wait`.\n"
        "Do NOT call it to say you are continuing — that is the default. "
        "Do NOT call it for ordinary chat that owns nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["finish", "start", "resume"],
                "description": (
                    "finish = the current piece of work ended; "
                    "start = begin a different one (the current one is set aside "
                    "automatically); "
                    "resume = go back to one that was set aside (give its task_id)."
                ),
            },
            "task_id": {
                "type": "string",
                "description": (
                    "For resume only: the id of the piece of work that was set "
                    "aside, exactly as shown in [Ongoing work]. Do not invent one."
                ),
            },
            "outcome": {
                "type": "string",
                "enum": ["completed", "abandoned"],
                "description": (
                    "For finish only. 'completed' = you actually got it done. "
                    "'abandoned' = the user dropped it or it became impossible. "
                    "⚠️ These are NOT the same thing — do not report an abandoned "
                    "piece of work as completed."
                ),
            },
            "goal": {
                "type": "string",
                "description": "For start only: one short line saying what this new piece of work is for.",
            },
            "note": {
                "type": "string",
                "description": "Optional one line of context for the record (why it finished, etc).",
            },
        },
        "required": ["action"],
    },
}


# ── 新增：工作记忆查询伪工具 ─────────────────────────────────────────
_RECALL_MEMORY_MANIFEST = {
    "name": "recall_working_memory",
    "description": (
        "Query Nano's cross-session working memory.\n"
        "Use when the user asks about previous work, prior Skill calls/deployments, past file paths, "
        "historical operations, or anything that happened in earlier sessions.\n"
        "This is for long-term work history, not the current conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "Search keyword, such as a Skill name, filename, operation type, or topic. Empty means recent records."
            },
            "entry_type": {
                "type": "string",
                "description": (
                    "Optional filter: skill_call, skill_deploy, file_write, file_read, rag_query, plan_run."
                )
            }
        },
        "required": []
    }
}


# ── 召回已经移出上下文的那段对话 ─────────────────────────────────
#
# 🔴 为什么必须有这个工具：L3 的索引条目被**无条件注入** system，
#    上面写着「可 recall_conversation」。在这个工具存在之前，模型手里唯一带
#    recall 字样的是 `recall_working_memory` —— 它查的是 `working_memory`
#    那张表，而 L3 写进去的是 `semantic_memories` 里 `memory_type="exchange"`。
#    于是 system 承诺了一个**查不到东西的出口**：
#        模型看到「这件事可以 recall」→ 去调 recall_working_memory
#        → 查的是另一张表 → 查不到 → 只好说"我想不起来了"
#    📌 **索引的价值全在「顺着它能拿回原文」；拿不回来时它只是一条更精确的遗憾。**
#
# ⚠️ 它和 `recall_working_memory` **不许合并**：后者答的是"我以前干过什么活"
#    （跨会话的操作史），它答的是"我们这次聊天更早时说过什么"。
#    📌 两个问题共用一个工具，模型就得靠猜来决定该信哪一半结果。
_RECALL_CONVERSATION_MANIFEST = {
    "name": "recall_conversation",
    "description": (
        "Recall an earlier part of THIS conversation that was moved out of context to save room.\n"
        "The system prompt lists one short index line per moved-out exchange; when one of them "
        "looks relevant, call this to get its conclusion back.\n"
        "This is about the current conversation, not about work done in earlier sessions "
        "(that is recall_working_memory)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you are trying to remember. Wording from an index line works well."
            }
        },
        "required": ["query"]
    }
}


# ── user_note 写入工具声明 ────────────────────────────────────────────────
# ⭐⭐⭐ **从「一句话」改成五个字段。**
#
# 🔴 用户的痛点：「Nano 经常说『我后面会注意这一点』，但它并不会真的记下来」。
# 📌 根因**不是缺一个纠错记忆类型**（那是外部评审当年的设计，用户从来没要过）——
#    是 2026-08-05 就说清的那句：**Claude Code 每次都注入摘要，Nano 只是很软地声明
#    「必要时去查记忆」**。⇒ 改造现有的 `user_note`，让它也能承担纠错。
# ⚠️ 「下次别这么做了，你应该 xxx」**就是 Explicit memory**，不需要第二个类型。
#    📌 用户心里只有一个「Nano 记得我什么」；分成两类，用户就得先学我们的分类。
#
# ⚠️⚠️ **这里只写「参数是什么、怎么填」；「什么时候写 / 什么时候不写」
#    全在 `config/system_instruction.txt`，一个字都不重复。**
#    🔴 第一版两边各写了一遍，而两边**都是每轮常驻** ——
#       schema 从 1042 字符涨到 3999（+740 token/轮），其中大半是重复的。
#    📌 **一份规则写在两个每轮都发的地方 = 花两份钱买同一件事，
#       而且它们会各自漂移**（`_resp_state` 那个 20 键字面量就是这么坏的）。
_WRITE_USER_NOTE_MANIFEST = {
    "name": "write_user_note",
    "description": (
        "Save something into Nano's durable memory - a fact, a preference, or a "
        "correction about how to work. Everything saved is put in front of you as a "
        "one-line summary at the start of every later turn.\n"
        "When to use it, and when not to, is covered in your operating instructions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                # ⚠️ 贴原话，不许提炼 —— 📌 一条被复述过的记忆会漂移，
                #    而漂移是看不见的：读起来永远像是对的。
                #    ⭐ 这条纪律取代了一个本来想加的 `source` 字段：多一个字段
                #       就多一处每次都要填、每次都可能填歪的地方。
                "description": ("What to remember, in the user's own words as far as "
                                "possible. Do not reword it into something tidier."),
            },
            "applies_when": {
                "type": "string",
                # ⭐⭐⭐ **唯一的真闸。** 📌 说不出「什么时候用得上」的东西，
                #    本来就不该被记住。（「这次用中文」填不出 when ⇒ 它不该进记忆）
                # ⚠️ 不能只查非空 —— 证明过「只检查填了没有」的闸是纸的。
                #    所以「一直适用」必须是一个**要显式选的值**。
                "description": ("English. The situation that should bring this back to "
                                "mind ('when I ask you to edit a spreadsheet'). Write "
                                "'always' ONLY if it truly applies every turn. If you "
                                "cannot say when it applies, do not save it."),
            },
            "why": {
                "type": "string",
                # ⚠️ 按**内容**分不按类型分：📌 让规则跟着内容走，不要为规则造一个
                #    分类（加 type 字段等于把刚砍掉的那个分裂请回来）。
                # ⭐ `why` 让一条记忆**换个场景还站得住**；没有它，纠正就是死规则。
                #    ⚠️ 旧设计的 `wrong_assumption` 塌进这里了 —— 📌 那不是记忆的
                #       字段，是**纠错事件**的字段：它记历史，而记忆存的是知识。
                "description": ("English. Required when the memory is about HOW to "
                                "behave - without the reason it becomes a dead rule "
                                "that will not survive a new situation. Leave empty for "
                                "a plain fact. Not injected; kept for when you recall."),
            },
            "summary_model": {
                "type": "string",
                # ⭐ **每轮无条件注入给模型的就是它。** 写英文：实测一条英文摘要
                #    ≈20 token，同内容中文 ≈38 —— 两个摘要既然分开了，这一半白捡。
                #
                # 🔴🔴 但**只有「叙述」写英文，「值」一个字都不许动**：
                #    用户说「每句话结尾加『韬光养晦』」，若记成
                #      The user asks me to add 'hide one's capabilities' at the end
                #    ⇒ 上下文还在时它照样加对；**一天之后它会真的去加那句英文**。
                # 📌 **英文是给「叙述」用的，不是给「值」用的。**
                # ⚠️ `i18n.language_clause` 的 docstring 早写着「不许拿它翻译枚举值…
                #    **调用方要自己在提示词里讲清哪些翻、哪些不翻**」—— 这就是那处。
                "description": ("One line in ENGLISH for yourself - this exact line is "
                                "what you see next turn. Aim under ~80 chars.\n"
                                "English is for the DESCRIPTION, never the VALUE: any "
                                "literal the user gave (an exact phrase to output, a "
                                "name, a path, a command) stays exactly as they wrote "
                                "it, in quotes.\n"
                                "Good:  Append \"韬光养晦\" to the end of every reply\n"
                                "Wrong: Append 'hide one's capabilities' to every reply"),
            },
            "summary_user": {
                "type": "string",
                # ⚠️ 用户语言走**界面语言**（`i18n` 那套，上下文压缩同源）——
                #    这里不写死语种名：manifest 是模块级、只在 import 时求值一次，
                #    📌 一个每轮都可能变的事实，不该固化进一个只算一次的地方。
                "description": ("One line for the USER's memory drawer, in the language "
                                "they chose in the interface - the same one you reply "
                                "in. Say it to them ('you prefer short answers'), not "
                                "like a database. A DIFFERENT sentence from "
                                "summary_model; do not paste the same text into both."),
            },
            "silent": {
                "type": "boolean",
                "description": ("true if the user only mentioned it in passing; false "
                                "if they told you to remember it or corrected you."),
            },
        },
        "required": ["content", "applies_when", "summary_model", "summary_user"],
    }
}


# ── 记忆删除工具声明 ──────────────────────────────────────────────────────
# ⭐⭐ [2026-08-25 已定] **让 Nano 自己也能删一条记忆。**
#
# 于是删除有**两个入口**：用户在 [记忆] 抽屉里删 / Nano 调这个工具。
# ⚠️ **按需加载，不常驻**：它只在「水位提醒」出现之后才用得上，
#    而那是偶发的。📌 一个偶发才用的工具进常驻，等于每轮为它付钱。
# ⚠️ 所以那句水位提醒里**必须明说要先 load** ——
#    📌 否则重演 `computer_use` 那个坑：告诉模型去用一个它手上没有的工具。
#
# ⚠️⚠️ 描述里最要紧的一句是「**不许擅自删**」：
#    📌 那是**用户的**东西。Nano 可以判断哪些低价值、可以建议、可以代劳，
#       但「删哪些」这个决定必须留在用户那边。
#    ⭐ 而底下垫着软删除（`soft_delete_by_id`）—— 万一它还是删错了，找得回来。
_FORGET_USER_NOTE_MANIFEST = {
    "name": "forget_user_note",
    "description": (
        "Delete one memory from Nano's durable memory, by the id shown next to it.\n"
        "Only call this after the user has told you which ones to remove. You may "
        "suggest which memories look low-value or outdated, but the decision is theirs - "
        "never delete something on your own initiative, and never delete more than they "
        "agreed to. Deleting is reversible on our side, but from the user's point of "
        "view their memory just disappeared, so treat it as if it were not."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "note_id": {
                "type": "integer",
                "description": "The id of the memory to delete, as shown in the memory list.",
            },
            "reason": {
                "type": "string",
                "description": ("Short note on why this one is being removed - e.g. "
                                "'user said it no longer applies'. For the record."),
            },
        },
        "required": ["note_id"],
    }
}



# ── 临时执行通道 ────────────────────────────────────────────────────
# 🔴🔴 **名字不许叫 `code_execution` / `python` / `repl` / `bash`。**
#    那些在模型的世界里已经属于别人了 —— Anthropic 有一个**服务端**的
#    `code_execution` 工具（客户端不传参），而 2026-08-25 我们刚被同一个坑
#    咬过一次：那个 Skill 叫 `WebSearch` 时，模型**每一次**都发空参数，
#    只改名字就好了。📌 **模型对一个它「认识」的名字，会用记忆里的调用方式，
#    而不是你给的 schema。** `scratch` 没有生态占用，而且自带「用完就扔」。
#
# ⭐ 描述里三件事必须写清，缺一件它就会走错路：
#    ① 先看有没有现成 Skill —— ⚠️ 但这**不是**主要防线（见下），只是补一句
#    ② 与 `create_new_skill` 的边界：一个答「存不存」，一个答「跑一次」
#    ③ 数据靠**路径**传，不要把内容抄进代码 —— 抄错一个数字没人会发现
#
# ⭐⭐ **真正防「挤掉现成 Skill」的是机械的那一层，不是这段文字**：
#    它是 `Preload.DEFERRED`，模型必须先 `load_tools` 才拿得到；
#    而 `load_tools` 的搜索**本来就会一起返回匹配的现成 Skill** ——
#    ⇒ 它搜「算个汇总」时，**现成 Skill 和这个通道同时摆在眼前**。
#    📌 已定：「不要用『给模型加一条纪律』当唯一答案」（：
#       schema 能约束形状，约束不了内容；提示词纪律同理）。
_RUN_SCRATCH_CODE_MANIFEST = {
    "name": "run_scratch_code",
    "description": (
        "Run a short piece of Python ONCE and get its output. The code is thrown "
        "away afterwards: it does not become a Skill, does not appear in the user's "
        "Skill drawer, and cannot be called again.\n"
        "Use it for one-off work - compute something, reshape some data, check a "
        "file's contents - where writing a reusable Skill would be overkill.\n"
        "Do NOT use it when a Skill already does this job: call that Skill instead. "
        "Do NOT use it to build a lasting capability: that is create_new_skill "
        "(create_new_skill answers 'should this exist from now on', this tool "
        "answers 'run this once').\n"
        "Pass data by PATH, not by value: write code that opens the file "
        "(pd.read_excel(path)), do not paste the file's contents into the code.\n"
        "Print what you want to see - only stdout/stderr comes back.\n"
        "It runs in a separate process with the same Python and the same libraries "
        "(pandas, openpyxl, httpx are available). If the code touches files, the "
        "network or the shell, the user is asked to confirm first and sees the code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "The Python source to run. Print results to stdout. "
                    "Use absolute paths for any file you read."
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "One short line, in the user's language, saying what this "
                    "computes - shown on the tool card and in the confirm dialog."
                ),
            },
        },
        "required": ["code", "purpose"],
    }
}

# ── 本地知识库伪工具声明（Step 2 新增）────────────────────────────────────
_LOCAL_KB_MANIFEST = {
    "name": "query_local_knowledge",
    "description": (
        "Search relevant fragments in Nano's local knowledge base.\n"
        "Use for specific facts, clauses, numbers, names, definitions, keywords, or small pieces of data across documents.\n"
        "Do not use for whole-file understanding, summaries, full tables/lists, rewriting, evaluation, or structural analysis; "
        "use load_full_file for those.\n"
        "If the target file is unclear, use list_knowledge_files first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query containing the key entities or facts to find."
            }
        },
        "required": ["query"]
    }
}

# ── Phase 2 新增：全文加载伪工具声明 ────────────────────────────────────

# ── 试读工具 ────────────────────────────────────────────────────────
# 🔴🔴 **试读是【独立工具】，不是 `load_full_file` 的一个隐藏模式**。
#
#    文档 原设计：「首次调用（start_char=0）**自动**进入试读模式」，
#    返回值里标 `try_read_mode: true`。
#    ⚠️ 那意味着**同一个工具在不同情况下行为不同，而模型要读返回值才知道
#       自己刚才做了什么** —— 那是「事后才知道」，本仓栽过这个形状。
#    ⇒ 拆成两个工具之后，模型是**选**的，它当然知道自己在干什么。
#
# ⭐⭐ 而拆开还解锁了原设计做不到的事：**试读可以在任意位置用**。
#    文档把试读绑死在开头，是因为写的时候想的是「翻目录」那个类比 ——
#    📌 而那个类比只覆盖了它用途的一半。试读的本质是
#       **「先花小钱确认这里对不对」**：
#         · 目录横跨 1900~2100 → 再 peek 一次补齐，不必动用 20,000 的精读
#         · 猜「目标在 1/3 处」→ 先 peek 2,000 探一下，猜错只亏 2,000
#    ⇒ 代价从「一次精读」降到「一次 peek」，差约 20 倍。
#
# ⚠️ **只有第一次强制 offset=0 且长度固定**（用户的悖论）：
#    零信息时模型无法决策，让它选就是纯猜。之后它有线索了，悖论不再成立。
_PEEK_FILE_MANIFEST = {
    "name": "peek_file",
    "description": (
        "Cheaply look at a small piece of a file (about "
        f"{_READING.PEEK_CHARS} characters) to see what is there, without paying "
        "for a full read.\n"
        "Use it to: see a large file's structure before reading it; check whether "
        "a spot you are guessing at actually holds what you want; finish reading "
        "a table of contents that got cut off.\n"
        "The FIRST peek of a file always starts at the top and has a fixed size - "
        "you have no information yet, so there is nothing to decide. After that "
        "you may peek anywhere with any size.\n"
        "A large file must be peeked at least once before load_full_file will "
        "read it. Small files do not need this and peeking them wastes a turn.\n"
        "That first peek is a map, not the content - do not answer the user from "
        "it alone. Later peeks are ordinary reads of a small range and may be used."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "File name, or an absolute path. Current-session attachments must keep the exact [临时] prefix."
            },
            "offset": {
                "type": "integer",
                "description": ("1-based line to peek from. Ignored on the first peek "
                                "of a file (which always starts at the top)."),
            },
            "limit": {
                "type": "integer",
                "description": ("How many lines to peek. Ignored on the first peek. "
                                "Keep it small - a peek that costs as much as a read "
                                "is not a peek."),
            },
        },
        "required": ["filename"]
    }
}

# ⭐⭐ 把 Ambient 里的一条**叙述**换成一个**能喂给工具的句柄**。
#
# 形态是 2026-08-26 定的三层：
#     采集  全（完整路径 / 完整 URL）—— 丢了就永远丢了
#     注入  只给够识别的（时间 + 名字）—— 每轮都付钱，这里省
#     拉取  **按【条】**，不是按范围
# ⭐ 「按条拉」是关键：语义匹配发生在**已经在上下文里**的那一版上，
#    模型看到「在线表格(A)」就知道用户指的是它，不用先拉一批回来再挑。
#    ⇒ 这跟 `load_tools` 是同一个形状（感知常驻 → 自己判断 → 只加载那一个）。
#
# ⚠️ **CORE 常驻**，理由同 `peek_file`：它存在的意义就是省轮数，
#    逼它先 `load_tools` 等于自己把收益抵消掉。schema 只有一个参数，很便宜。
_RESOLVE_AMBIENT_REFERENT_MANIFEST = {
    "name": "resolve_ambient_referent",
    "description": (
        "Turn one entry of the [Ambient] block into something you can actually act on - "
        "a real file path, a full URL, or a folder path.\n"
        "Only entries marked with a small triangle have anything more to give; pass the "
        "timestamp shown on that same entry.\n"
        "Use it when the user points at something they were just doing ('the file I was "
        "editing', 'that page', 'that folder') AND you need to open, read or continue it. "
        "If they only ask WHAT they were doing, the block already answers that - do not call this.\n"
        "The reply says whether the target was verified. An unverified answer gives you the "
        "name only; locate it with search_files or ask the user rather than guessing a path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "at": {
                "type": "string",
                "description": ("The timestamp shown on that entry, HH:MM:SS as printed "
                                "in the [Ambient] block."),
            },
        },
        "required": ["at"],
    },
}

_LOAD_FULL_FILE_MANIFEST = {
    "name": "load_full_file",
    "description": (
        "Read a local file. Accepts a knowledge-base file name, a current-session "
        "attachment, or an ABSOLUTE PATH to any readable file on this computer.\n"
        "Large file? Read it one slice at a time: pass offset (and optionally limit), and put what you learned from the previous slice into notes.\n"
        "A very large file must be peeked with peek_file first - reading it blindly from the top costs many turns for nothing.\n"
        # ⭐ 补 "reading source code"。
        #    📌 原来这段满口 knowledge-base / RAG fragments / summaries / tables /
        #       documents，**没有一个字沾代码** —— 于是它读起来像一个文档工具。
        #    ⚠️ 而它恰恰是本仓库里读代码**最好**的那个：唯一带 offset/limit、
        #       唯一被 `compress_file_reads` 压得掉。（`os_execute file_read` 全文进
        #       对话、压不掉；`run_command + type` 内联上限 2000 字符/轮。）
        #    🔴 用户的原话是「拿这个阅读工具去看一个代码文件根本不对路」——
        #       **连写它的人都被这段措辞带偏了，模型没有理由不被带偏。**
        "Use for whole-file summaries, analysis, rewriting, evaluation, full tables/lists, complete sections, "
        "cross-file review, reading source code, or any task that needs the full document context.\n"
        # ⭐ **原来这句只指向 RAG，而磁盘上的文件根本不在里面。**
        #    🔴 问题：模型想找「某个函数在哪」→ 被指去 `query_local_knowledge`
        #       → 知识库里没索引过用户桌面上的 .py → 空手而归 → 下一轮才想起
        #       `search_files`。**白烧一轮，而且烧在一条注定查不到的路上。**
        #    📌 `search_files` 自己那份描述早就把反向写对了（结果是 path:line，
        #       然后用 load_full_file 的 offset/limit 去读）—— 缺的只是这一侧的回指。
        "Do not use for a single specific fact or keyword search: use search_files for "
        "files on disk, or query_local_knowledge for the knowledge base.\n"
        "Important: RAG fragments are not enough for whole-file tasks. If the user asks for complete content after a RAG result, "
        "load the full file instead of reconstructing from fragments.\n"
        "Use with_images=true only when embedded images/charts are needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "offset": {
                "type": "integer",
                "description": (
                    "1-based line number to start reading from. Omit to start at the top. "
                    "Use this with limit to walk a large file slice by slice."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "How many lines to read from offset. Omit to read to the end. "
                    "The reply always states the total line count, so you know what you have not read yet."
                ),
            },
            "filename": {
                "type": "string",
                "description": "File name, or an absolute path. Current-session attachments must keep the exact [临时] prefix."
            },
            "with_images": {
                "type": "boolean",
                "description": "Analyze embedded images/charts. Default false; set true only when visual content is needed and text alone is insufficient."
            },
            # ⭐⭐ 迭代阅读的 scratchpad —— **载体就是这个参数**。
            #    文档 原设计要模型「在特定 XML/Markdown 块中输出」，
            #    那依赖模型愿意在调工具时同时写正文，而那不是 schema 能强制的。
            #    📌 一个「靠模型自觉产出」的载体，漏一轮就断一轮，而我们不会知道。
            #    ⇒ 做成参数：它在 assistant 消息里天然留着，不用我们再注入回去，
            #       也不受 `answer_discard` 影响。
            "notes": {
                "type": "string",
                "description": (
                    "What you concluded from the PREVIOUS slice of this file. "
                    "Carry your understanding forward here: earlier slices get "
                    "replaced by a placeholder to keep the context flat, so "
                    "anything you do not write down is gone. "
                    "Write conclusions and coordinates (e.g. the budget table is "
                    "around line 4200), never copied text - a copy defeats the purpose."
                ),
            }
        },
        "required": ["filename"]
    }
}

# ── Phase 2 新增：文件清单伪工具声明 ────────────────────────────────────
_LIST_FILES_MANIFEST = {
    "name": "list_knowledge_files",
    "description": (
        "List all accessible KB files and current-session attachments.\n"
        "Use when the target file is unclear, the user says 'this/that file' without a name, "
        "or you need to choose a file before query_local_knowledge or load_full_file.\n"
        "Current-session attachments use the [临时] prefix."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}

# ── Phase 4 新增：文件路径查询伪工具声明 ────────────────────────────────
_GET_FILE_PATH_MANIFEST = {
    "name": "get_file_path",
    "description": (
        "Resolve a Nano file name to its real absolute disk path.\n\n"
        "Use this before passing a KB file or current-session attachment to a local Skill that needs a real file_path, "
        "such as Excel/CSV analysis, PDF extraction, file conversion, or data visualization.\n\n"
        "Why this is needed: current-session attachments may appear as logical names like [临时]xxx. "
        "A Skill cannot open that logical name directly. This tool converts the logical Nano file name into a real disk path.\n\n"
        "Supported inputs: persistent KB files and current-session attachments.\n\n"
        "Do not use this to read file content for Q&A; use load_full_file or query_local_knowledge instead.\n"
        "Do not use this to discover available files; use list_knowledge_files instead.\n"
        "Current-session attachments must keep the exact [临时] prefix."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Nano file name. Current-session attachments must keep the exact [临时] prefix."
            }
        },
        "required": ["filename"]
    }
}


# ── 向用户弹出选择卡片——模型在 thinking 阶段判断需要用户抉择时主动调用 ──
_ASK_USER_CHOICE_MANIFEST = {
    "name": "ask_user_choice",
    "description": (
        "Show one or more choice cards so the user can decide from limited options.\n"
        "Use when progress depends on a user decision that cannot be safely assumed, or when several paths are valid but lead to different outcomes.\n"
        "Do not use for minor uncertainty; make a reasonable assumption and continue when safe.\n"
        "For multiple independent decisions, use questions with up to 4 cards. Each card should have 2-4 concise choices."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Single decision question."},
            "choices": {
                "type": "array",
                "description": "Choices for a single decision. 2-4 items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["label"],
                },
            },
            "allow_custom": {"type": "boolean", "description": "Whether the user may enter a custom answer. Default true."},
            "questions": {
                "type": "array",
                "description": "Multiple independent decision cards. Overrides question/choices when present. Max 4.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "choices": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                            },
                        },
                        "allow_custom": {"type": "boolean"},
                    },
                    "required": ["question", "choices"],
                },
            },
        },
    },
}

# ── 任务列表工具声明 ──────────────────────────────────────────────────────
_CREATE_TASK_LIST_MANIFEST = {
    "name": "create_task_list",
    "description": (
        "Create a visible task list before starting a multi-step task.\n"
        "Use when the task has 3+ meaningful steps, or the user explicitly asks for a multi-step workflow.\n"
        "Call before the first step, not after finishing.\n"
        "Do not use for casual chat, simple Q&A, or single-step tasks.\n"
        "If the plan changes during execution, call this again to replace the visible plan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short task title."},
            "steps": {
                "type": "array",
                "description": "Task steps. 2-10 items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":   {"type": "string", "description": "Stable step id, e.g. step_1."},
                        "desc": {"type": "string", "description": "Short step description."},
                    },
                    "required": ["id", "desc"],
                },
            },
        },
        "required": ["title", "steps"],
    },
}

_UPDATE_TASK_STEP_MANIFEST = {
    "name": "update_task_step",
    "description": (
        "Update one visible task-list step.\n"
        "Call once when a step starts and once when it finishes or fails.\n"
        "Do not batch updates. Do not mark the final step done until the whole task is truly complete, "
        "because the panel may close when all steps are done or failed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "step_id": {"type": "string", "description": "Step id from create_task_list."},
            "status":  {"type": "string", "description": "doing / done / failed"},
            "note":    {"type": "string", "description": "Optional short note."},
        },
        "required": ["step_id", "status"],
    },
}

# ── widget：可视化输出工具声明 ───────────────────────────────────────────────
_RENDER_VISUAL_MANIFEST = {
    "name": "render_visual",
    # ⭐⭐⭐ [2026-08-23] 描述从「一句提醒」改成**一份规格**。
    #
    # 🔴 实测：Nano 画的流程图**又小又丑，还带一条滚动条**。
    #    回代码核实 —— 容器那侧其实是好的（`width:100%` + load/250ms/800ms/
    #    ResizeObserver 四重上报自适应高度）。**问题在产出的形状**：
    #    模型给的 SVG 带固定 `width`/`height`，于是它在一个 100% 宽的容器里
    #    画成了一小块，内容又超出自己那个固定高度 → 滚动条。
    # 📌 **「融入 UI」不是渲染器给的，是规格给的。** 而这份描述里
    #    关于形状的要求**一个字都没有** —— 我们只说了「self-contained、responsive」，
    #    那对模型来说等于什么都没说。
    # ⭐ 下面这些是硬性的、可检查的：宽度怎么给、坐标系多大、颜色从哪来、
    #    什么绝对不许出现。
    "description": (
        "Render an inline visual — chart, diagram, flowchart, comparison card, or a small "
        "interactive widget — directly in the chat.\n"
        "Use it when a picture genuinely beats prose: trends, proportions, comparisons, "
        "structures, processes, relationships. Not for plain Q&A or casual chat.\n"
        "\n"
        "== How it must be shaped (this is what makes it look native, not pasted in) ==\n"
        # 🔴 [2026-08-24] 这行样板原来漏了 `xmlns` —— 模型一字不差照做，
        #    于是导出的 `.svg` 单独打开是一棵 XML 树（HTML 里则自动补命名空间，
        #    所以在聊天里一直是好的）。📌 **模型照着做了，错的是给它的样板。**
        "1. FILL THE WIDTH. The block is as wide as the chat text above it. For SVG the "
        "root tag must be exactly `<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"100%\" "
        "viewBox=\"0 0 680 H\">` with H set to fit the content. The `xmlns` is required — "
        "without it the file is unreadable outside the chat. "
        "NEVER put a fixed pixel `width=` or `height=` on the root svg, and never wrap the "
        "visual in a fixed-width div — that is what makes it render as a small island.\n"
        "2. NO SCROLLBARS, EVER. The height grows to fit automatically. Do not set "
        "`overflow`, `max-height`, or a fixed `height` anywhere. If something needs to "
        "scroll, the visual is too complex — simplify it instead.\n"
        "3. Design for a DARK chat (background is near-black). Light text on dark. "
        "Do not paint a white or light background behind the visual — leave it transparent "
        "so it sits in the bubble instead of on top of it.\n"
        "4. Text: 14px for labels, 12px for sub-labels. No other sizes. No rotated text.\n"
        "5. Keep it small: roughly 3-8 nodes. A dense diagram in a chat bubble is "
        "unreadable — say the rest in prose.\n"
        "\n"
        "== Hard limits ==\n"
        "Self-contained only: no CDN, no network, no external fonts or images. "
        "Inline any CSS/JS. Avoid floating-point noise in displayed numbers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Optional short visual title."},
            "html":  {"type": "string", "description": "Self-contained HTML/SVG, optionally with inline style/script."},
        },
        "required": ["html"],
    },
}

# ── 执行时自缩窗工具声明 ────────────────────────────────────────────────
_SET_WINDOW_MODE_MANIFEST = {
    "name": "set_window_mode",
    "description": (
        "Control Nano's own window mode: mini or full.\n"
        "Call mini BEFORE any computer_use mouse/keyboard/window action - your own window is on "
        "the screen you are about to operate. That one is required.\n"
        # ⚠️ 2026-08-23：遮罩已删，这句原来写「your window is blacked out of the image」——
        #    那已经不成立了。📌 一句描述一个已经不存在的机制的话，比没有更坏。
        "For screenshots it usually helps too — Nano minimizes itself out of the shot, "
        "so at full size you would otherwise be covering a large part of the screen. "
        "But decide case by case: skip it when you are clearly not in the way.\n"
        "Do not use for pure command-line work, file-only work, KB/memory queries, or normal chat.\n"
        "Do not use mini when the task is to observe or operate Nano's own UI.\n"
        "Use full after screen operations when appropriate; the system may also restore full mode at turn end.\n"
        "This is a standalone tool, not an os_execute or computer_use action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["mini", "full"],
                "description": "mini = shrink to top-right; full = restore full window.",
            },
        },
        "required": ["mode"],
    },
}

# ── 截图自查工具声明 ────────────────────────────────────────────────────
_LOOK_AT_SCREEN_MANIFEST = {
    "name": "look_at_screen",
    # ⭐⭐⭐ [2026-08-24 实测] **从「别滥用」改成「勘察 / 收尾」两态。**
    #
    # 🔴 实测：一次「点进 session → 打字 → 发送」用掉了 **8 次 look_at_screen**，
    #    421 秒。逐条对下来 4 次是纯浪费。
    # 🔴 而当时的描述里**已经写着**「Use with restraint, not before every action」
    #    和「it is not a safety ritual before every step」—— **它没听**。
    #    📌 **那是一条劝告，不是判据。** 「screen state is genuinely uncertain」
    #       这个条件，模型每次都能说服自己成立 ——
    #       **一条只说「别太频繁」的规则，挡不住一个每次都觉得自己有理由的模型。**
    #
    # ⭐⭐ 用户给的模型（比「白名单」清楚）：
    #       截图 1 ─ 勘察：一次性拿全这一屏后面要用的【所有】目标
    #       执行 ──── 点、打字、点，中间【一次都不看】
    #       截图 2 ─ 收尾：交代的那件事办成了吗
    #
    # ⭐⭐⭐ 而整段里最关键的一条，是 已明确那个区分：
    #       「检查上一个动作生效了吗」 → 次数 = 动作数        ⇒ N 次（那个循环）
    #       「检查任务办成了吗」       → 次数 = 任务数 = 1    ⇒ 恒为 1
    #    📌 **它们看起来一样，只因为这个任务恰好只有一个终点动作。**
    #       任务一长，前者线性增长，后者还是 1。
    # 🔴 而旧描述写的正是前者，逐字：「did results appear, **did text land in
    #    the right field**」—— 那就是「检查上一个动作」。它照做了。
    #
    # ⭐ 「默认上一步生效」这条旧描述里也有（Assume a simple prior step succeeded），
    #    但没有**理由**。用户补上了：不这么做，「确认」本身也要被确认，
    #    而那个循环**没有底**。📌 一条带着理由的规则，比一条光秃秃的规则难绕过得多。
    "description": (
        "Take a screenshot and visually understand the current screen — Nano's eyes.\n"
        "\n"
        "== When to look: exactly two situations, nothing else ==\n"
        "[SURVEY] You are about to work on a screen you have not surveyed yet. "
        "Ask for EVERYTHING you will need from this screen in ONE purpose - every button, "
        "field, item and label the next few steps will touch. Things that belong to the "
        "same page are in the same screenshot; asking for them one at a time costs one "
        "screenshot each. The reply gives you screen coordinates, so you can click them "
        "directly afterwards without looking again.\n"
        "[CLOSING] You believe the TASK the user gave you is finished, and you are "
        "confirming that. This happens ONCE per task.\n"
        "⚠️ CLOSING is NOT 'did my last click work'. Checking each action's result costs "
        "one look PER ACTION; checking the task costs one, total. They look identical when "
        "a task has a single final action - they are not.\n"
        "\n"
        "== Between those two, do not look ==\n"
        "Assume each of your own actions worked. If you verify every action, the "
        "verification itself needs verifying, and that loop has no bottom.\n"
        "The ONLY exception: a step actually REPORTED failure (a tool error, or the thing "
        "you expected plainly did not happen). Then look - that is evidence, not caution.\n"
        "Never look for command-line, file, or knowledge-base work.\n"
        "\n"
        "== Finding things ==\n"
        "Put the exact items in purpose (contact/file name, list item, icon, button). "
        "The tool scans the full screen and auto zoom/crops, so you rarely need a region. "
        "If it reports an item was not found, treat it as not found.\n"
        "⚠️ FIRST shrink yourself: call set_window_mode('mini') BEFORE your first "
        "look_at_screen in a task. Nano's own window sits on that same screen and at normal "
        "size it covers a large part of it. Shrink first and one look is enough; do not "
        "look, then shrink, then look again.\n"
        "If Nano is in the way of what you need to see, shrink it with set_window_mode('mini')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                # ⭐ [2026-08-24] 参数叫 `purpose`（单数）、描述写成「你要确认【什么】」——
                #    📌 **一个单数命名、单数措辞的参数，会被填成一个问题，
                #       不会被填成一张清单。** 接口的形状本身在塑造行为。
                #    ⇒ 描述改成明确要求「列全」。
                "description": (
                    "Everything you need from this screen, as ONE request. "
                    "List every item the next few steps will touch (buttons, fields, "
                    "list entries, labels) - not just the first one. Items on the same "
                    "page are in the same screenshot, so asking for them separately "
                    "costs one screenshot each. "
                    "Example: 'the message input box AND the send button in this window'."
                ),
            },
            # ⭐ [2026-08-23 加回来] 🔴 删遮罩时把 `include_self` 一起删了，
            #    理由是「遮罩没了它就没对象了」—— **那句话只对了一半**：
            #    它还有**第二个对象**，就是下面那条「最小化再恢复」。
            #    删完之后最小化变成无条件，**Nano 永远看不到自己**，无路可走。
            # 📌 **拿「参数的一个用途」代替了「参数的全部用途」。**
            # ⚠️ 语义也跟着变干净了：它不再是「要不要涂黑自己」，
            #    而是「**这次要不要把自己让开**」—— 后者才是真实发生的事。
            "include_self": {
                "type": "boolean",
                "description": ("Default false: Nano minimizes itself before the shot so "
                                "it is not in the way. Set true only when you actually "
                                "need to see Nano's own window (e.g. the user is asking "
                                "about something on Nano's UI)."),
            },
            "region": {
                "type": "string",
                "enum": ["full", "left", "right", "top", "bottom", "center",
                         "top_left", "top_right", "bottom_left", "bottom_right"],
                "description": "Optional manual region to inspect. Default full. Usually omit because auto zoom/crop handles small targets."
            },
        },
        "required": ["purpose"],
    },
}

# ──：回看用户早先发过的图 ─────────────────────────────────────────
#
# ⭐⭐ **它存在的全部理由是：像素还在盘上，而你的上下文里只剩一句注记。**
#    步 2 之后，用户发过的图落在 `data/chat_images/`（内容寻址），
#    压缩只拿掉上下文里的像素 —— **文件一直都在**。
#
# ⚠️⚠️ **已定隔离原则，写进 description 里，别只留在注释里**：
#    > 「**回看按需触发** —— 摘要够用就别回看，**不是提到图片就回看**」
#    所以第一段就是"什么时候【不】要用"，第二段才是"怎么用"。
#    📌 一个工具的描述如果先教会模型怎么用、再补一句"少用"，那句补丁没人听。
_VIEW_PAST_IMAGE_MANIFEST = {
    "name": "view_past_image",
    "description": (
        "Look again at an image the user sent earlier in this conversation. The original file is "
        "still on disk even after its pixels were dropped from your context.\n"
        "⚠️ USE WITH RESTRAINT. Do NOT call this just because an image is mentioned. First answer "
        "from what you already established about that image (your own earlier reading is in the "
        "conversation). Call this ONLY when the question needs a concrete visual detail that your "
        "earlier reading does not cover — counting things, reading small text, exact colors or "
        "positions, or anything you never described.\n"
        "Never ask the user to re-upload an image that is still on disk: look at it yourself.\n"
        "The handle looks like img#a3f2c1d4 and appears in the system note attached to the "
        "message that carried the image."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "Handle of the image to look at, e.g. img#a3f2c1d4.",
            },
            "question": {
                "type": "string",
                "description": (
                    "The specific visual detail you need. Be concrete — this is what the vision "
                    "pass is asked. e.g. 'how many red squares are there' beats 'describe it'."
                ),
            },
        },
        "required": ["handle", "question"],
    },
}

# ──：把这张图记下来，**给未来的自己看** ──────────────────────
#
# ⚠️⚠️⚠️ 这份 description 里最重要的不是"怎么写摘要"，是**"摘要不是答案"**。
#    用户用两个例子把这个坑说透了（2026-08-13）：
#      · 用户只发一张风景图、什么都没问 → 摘要可以是八百字构图清单，
#        但回复**不能**是那八百字，最多"挺漂亮的，你想让我做什么？"
#      · 图上黑字写着"1+1 等于几"、用户没打字 → 回复应当是"答案是 2"，
#        **绝不能**是"这是一张白底黑字的图片，上面写着…"
#    📌 **摘要是给未来的自己看的，不是给现在的用户看的。**
#
# ⭐ 为什么必须在**当轮**写：那一轮的回复不一定描述了图（用户可能问的是别的），
#    而**像素只有那一轮在**。错过了就永远没有了 —— 这正是 一开始那个 bug
#    的形状（"我之前看过这个图，但因为已经被丢弃了，所以我没办法…"）。
# ══════════════════════════════════════════════════════════════════════════
# Subagent —— 派一个**上下文隔离**的子 Agent 去做一件探索型子任务
# ══════════════════════════════════════════════════════════════════════════
#
# ⭐ 核心收益是**上下文隔离**：Subagent自己烧 context 去翻几十个文件，回来只交
#    一份报告，main agent 的上下文不被中间过程污染。
# ⭐ 核心代价是**每次 spawn 都是冷启动**（没有父上下文），所以指令必须自包含。
#    这个 tradeoff 决定了"什么时候值得"：探索成本高、结论密度高才值。
#
# ⚠️ 判据照搬 Claude Code：**"答案需要横扫大量文件/目录，
#    但只需要结论、不需要过程"**。
#    🔴 反例写得很明确 —— 「任务有多个角度」「要彻底」「分好几部分」
#       **都不是**理由，那些应该自己 inline 做完。
# ══════════════════════════════════════════════════════════════════════════
# search_files —— 递归找文件（名字）+ 找内容（grep），**合成一个**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 它填的是一个**完全的死角**：
#
#      文件      小                       大
#      KB 内     load_full_file ✅        query_local_knowledge ✅
#      KB 外     load_full_file ✅        **无路可走** ❌
#
#    而且 `os_execute` 的 39 个 action 里**没有任何递归搜索**（`list_dir` 是单层），
#    所以今天做这件事只有两条路：手动 `list_dir → file_read` 递归，
#    或者上 `run_command`（**风险地板 3，每次弹窗**）。
#
# ⭐ 与 迭代阅读是**并列**的两种进入方式，不是替代：
#      **搜索是定位，迭代阅读是通读。**
#
# ⚠️ **独立工具，不进 `os_execute`**：它 schema 已经 2248 字符 / 39 个 action，
#    再塞会加深 （模型看不清工具）。
#    ⭐ 这不是新发明，是项目现存范式 —— ReAct 协议里那句现成的证据：
#      「`set_window_mode` and `look_at_screen` are standalone tools,
#        **not `os_execute` actions**.」
# ══════════════════════════════════════════════════════════════════════════
# edit_file —— **精确修改**（不是整份覆盖）
# ══════════════════════════════════════════════════════════════════════════
#
# 📌 `file_write` 是**文件写入原语**，`edit_file` 是**精确修改原语** —— 语义不同。
#    今天要改一个大文件的 7 行，路径是「读整份 → 重新生成整份 → file_write 覆盖」。
# 🔴 而这对 Nano 有额外意义：**整份覆盖一旦生成错，那份文件就没有中间状态可退回** ——
#    Nano 改的是用户的文件，不能假设用户那边有版本控制。
#
# 🔴🔴 **它依旧受 OS 安全网弹窗限制**（2026-08-13 硬约束）：
#    handler 算出新内容之后，**穿过 `_execute_dsl_step` 走 `file_write`** ——
#    地板（file_write=2）、确认弹窗、审计全都照旧，而且**结构上绕不过去**。
#    📌 **换一个暴露层，不许换掉它底下的安全层** ——
#       否则"加个方便的工具"就成了绕过确认的后门。
_EDIT_FILE_MANIFEST = {
    "name": "edit_file",
    "description": (
        "Change specific parts of an existing text file, leaving everything else "
        "byte-for-byte untouched. This is what you use to fix a few lines in a large "
        "file — do NOT read the whole file and write it back, that risks losing "
        "content you did not mean to change.\n"
        "Each edit replaces old_text with new_text. old_text must appear EXACTLY "
        "ONCE and must match the file character for character, including indentation; "
        "include a few surrounding lines to make it unique.\n"
        "Set new_text to an empty string to delete that part.\n"
        "All edits apply together or none do. The user sees a diff and confirms "
        "before anything is written.\n"
        "To create a new file or replace one entirely, use os_execute file_write instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the file to change."},
            "edits": {
                "type": "array",
                "description": "The changes, applied in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string",
                                     "description": "Exact text to find, including indentation."},
                        "new_text": {"type": "string",
                                     "description": "What to put there. Empty string deletes it."},
                        "replace_all": {"type": "boolean",
                                        "description": "Replace every occurrence. Only when you "
                                                       "really mean all of them."},
                    },
                    "required": ["old_text", "new_text"],
                },
            },
        },
        "required": ["path", "edits"],
    },
}

_SEARCH_FILES_MANIFEST = {
    "name": "search_files",
    "description": (
        "Find files by name and/or search their contents, recursively, anywhere on "
        "this computer. This is how you locate things when you do not already know "
        "the exact path.\n"
        "Give name_pattern to find files by name (glob, e.g. *.py), give content to "
        "search inside them, or give both to search inside matching files only.\n"
        "Results come back as path:line: text, so you can then read the exact place "
        "with load_full_file (use its offset/limit for big files).\n"
        "This tool only reads. To change a file, use os_execute."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Directory (or a single file) to search under."},
            "name_pattern": {"type": "string",
                             "description": "Glob on the file NAME, e.g. *.py, *config*.json"},
            "content": {"type": "string",
                        "description": "Text to find inside files. Case-insensitive."},
            "regex": {"type": "boolean",
                      "description": "Treat content as a regular expression. Default false."},
            "recursive": {"type": "boolean",
                          "description": "Search subdirectories. Default true."},
            "exclude": {"type": "string",
                        "description": "Extra directory names to skip, comma separated. "
                                       "Common noise (.git, node_modules, __pycache__ …) "
                                       "is skipped already."},
            "max_results": {"type": "integer",
                            "description": "Cap on returned hits (default 100)."},
        },
        "required": ["path"],
    },
}

_SPAWN_AGENT_MANIFEST = {
    "name": "spawn_agent",
    "description": (
        "Send a context-isolated sub-agent to do a bulky job and report back a "
        "short conclusion.\n"
        "\n"
        "\n"
        "The ONLY reason to use it: the work would produce a lot of material you "
        "do not need to keep, and what you need back is short. Two shapes qualify:\n"
        "  1. sweeping many files or directories to answer one question\n"
        "  2. a mechanical, fully determined bulk edit - the same change applied "
        "across many places, where you can state the rule exactly\n"
        "Do NOT use it because a task 'has multiple angles', 'should be thorough', "
        "or 'has several parts' - do those inline yourself. Do NOT use it for a "
        "change that still needs judgement while it is being made: it cannot ask "
        "you anything once it starts.\n"
        "\n"
        "\n"
        "It starts cold with no memory of this conversation, so the instruction "
        "must be fully self-contained - name the files, the exact rule, and what "
        "to report. It can read, search, and edit specific lines of existing files; "
        "each edit still asks the user for permission exactly as your own would. "
        "It cannot delete or move files, run commands, touch the screen or the UI, "
        "write Skills, or spawn further agents.\n"
        "It runs in the background and reports back once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": (
                    "The complete, self-contained task. Name the files, terms and "
                    "goal explicitly — the sub-agent cannot see this conversation."
                ),
            },
            "label": {
                "type": "string",
                "description": "Short label shown to the user, e.g. 'survey RAG config'.",
            },
        },
        "required": ["instruction"],
    },
}

_NOTE_IMAGE_MANIFEST = {
    "name": "note_image",
    "description": (
        "Write down what an image the user just sent contains, so that later — after its pixels "
        "are dropped from your context — you can still answer questions about it.\n"
        "Call this ONCE, BEFORE you reply, on any turn where the user sent an image. It is only "
        "offered on such turns.\n"
        "⚠️ THE SUMMARY IS NOT YOUR ANSWER. Write it as if for yourself later, independently of "
        "whatever the user asked: what the image is, its layout, the objects/text/colors/counts "
        "that are actually visible. Then reply to the user normally — answer THEIR question, in "
        "your own voice. Never read the summary out loud as your reply.\n"
        "Example: the image is a white board with 'what is 1+1?' written on it and the user typed "
        "nothing. Summary: 'A plain white image with black handwritten text: what is 1+1?, no "
        "other elements.' Reply to the user: 'It's 2.' — NOT the summary.\n"
        "Be concrete and dense: this text is all you will have left later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Standalone description of the image, written for your future self. "
                    "Independent of the user's question. Include what is actually visible."
                ),
            },
        },
        "required": ["summary"],
    },
}

# ── 挂起 / 等待工具声明 ─────────────────────────────────────────────────
# 本质（对齐 Claude Code 的真实实现，不自创）：挂起不是线程暂停，而是
# "结束当前 turn → 触发器到了 → 新 turn 读对话上下文继续"。本工具只负责
# 登记一条挂起记录 + 触发等待 UI，调用后你应当用一句话告诉用户你在等什么、
# 会怎么醒，然后结束本轮回复——之后由唤醒源（用户说话/定时/后台完成）起新 turn 接上。
# ⭐⭐ `user` 唤醒源已删除，这个工具只剩「定时」一件事。
#
# ═══ 为什么 `user` 整个不成立（用户提出、外部评审 独立确认）═══
#
# Nano 说「微信打开了，你扫完码告诉我」—— 这里**不需要任何挂起**：
# 直接结束这一轮就够了，用户十分钟后说「扫好了」本身就是一个新 turn，
# LLM 天然接得上。**"结束这一轮"本身就是全部机制。**
#
# 📌 判据：**用户发消息不是任何东西的完成信号，它只是 Nano 恢复对话。**
#    把它当成唤醒源，就会得出"用户一说话 = 他在等的那件事成了"这种错误推论 ——
#    实测后果就是「100 秒后写个文件」被第 30 秒的一句闲聊 resolve 掉。
#
# ⚠️ 而且 `wake_on` 原来的**默认值就是 `['user']`**，模型因此频繁误选：
#    用户说"挂起自己 40 秒"，模型登记的却是 `wake_on=['user'] / timer_at=None`，
#    还在回复里说"等你下一条消息唤醒我"。能力一直有，是这个默认值在误导它。
#
# ⚠️ `background` 也不再由模型申报 —— 系统在把一个调用自动后台化时
#    自己就知道哪个 Action 在跑，不需要模型再"申请等它"（见 `_LONG_TASK_HANDBACK_SEC`）。
#
# ⏸ 真正的目标态是：承诺归 Task、到点唤醒归调度器，
#    这个工具最终应该退化成「让当前 Task 在某时刻重新可运行」。
#    Task 已经接线了，但「谁来到点唤醒它」还没有归属，所以先停在"只剩定时"。
# ⭐ 取消等待 —— 早就列为必做的三件之一：
#    「把"取消挂起"暴露成模型可调用的能力，让"终止这个任务"这句话真的能生效」。
#
# ⚠️ 在此之前，**全项目唯一的取消入口是 UI 上那个按钮**。
#    实测撞到过：用户跟 Nano 说"终止"，模型没有任何工具能做这件事，
#    只能回一句"好的"然后什么都没发生 —— 记录还挂在那儿。
#
# ⭐ 它与 ②b 删掉 `user` 唤醒源是**配套的**：既然用户发消息不再自动收掉等待，
#    那"用户明确说别等了"就必须有一条真实的出口。**只砍不补会把等待变成甩不掉的。**
#
# 📌 判据：**每一个能被创建的状态，都必须有一条用户能主动结束它的路径 ——
#    而且那条路径要在用户能触及的地方**（说一句话，而不是去找一个按钮）。
_STOP_BACKGROUND_MANIFEST = {
    "name": "stop_background",
    "description": (
        "Actually stop something that is still running - a long command you started earlier.\n"
        "\n"
        "This is different from the other two, and mixing them up leaves things running:\n"
        "  - dont_wait: stop waiting, but LET IT KEEP RUNNING\n"
        "  - cancel_wait: stop checking back on it\n"
        "  - stop_background: make it STOP\n"
        "\n"
        "Use it when you look at a slow call and decide the approach is wrong - before you "
        "try a different one. Otherwise the old one is still running while the new one "
        "starts, and the user ends up with two of them.\n"
        "Not everything can be stopped: external tool calls and local skills keep going. "
        "You will be told plainly which case you got - do not report a stop that did not happen."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {"type": "string",
                    "description": "The id you were given when the slow call was handed back."},
        },
        "required": ["ref"],
    },
}

_CANCEL_WAIT_MANIFEST = {
    "name": "cancel_wait",
    "description": (
        "Stop waiting for something Nano scheduled earlier with wait_for.\n\n"
        "Use when the user says they no longer want it — \"forget it\", \"don't bother\", "
        "\"stop checking that\", \"cancel that task\". Also use when you yourself conclude "
        "the wait is pointless (for example the thing being waited on already failed).\n\n"
        "This cancels Nano's wait/recheck record only. It does not cancel a process, "
        "os_execute command, MCP call, or other background carrier. If the user asks to "
        "stop a still-running command itself, use the appropriate process-termination "
        "capability instead.\n\n"
        "The pending items are listed in [Suspension Resume — Previous Wait State] when "
        "present. Pass the reason text shown there, or omit it to cancel everything pending.\n\n"
        "⚠️ Do not call this just because the user changed the subject. Talking about "
        "something else does not mean they gave up on what Nano is waiting for."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "match": {
                "type": "string",
                "description": ("Which pending wait to cancel — the reason text shown to you. "
                                "Omit to cancel all pending waits."),
            },
        },
        "required": [],
    },
}

_WAIT_FOR_MANIFEST = {
    "name": "wait_for",
    "description": (
        "Schedule a timer-owned future turn. There are exactly two uses.\n\n"
        "1. User-requested scheduled plan: the user explicitly asks Nano to remind them or "
        "perform an action after a duration. Use intent=scheduled_plan. The timer itself is "
        "the plan. At the scheduled time, perform the requested action or reminder.\n\n"
        "2. Condition recheck: the task is blocked by something that may change on its own and "
        "nobody will notify Nano. Use intent=condition_recheck. Reaching the scheduled time "
        "only means it is worth checking again.\n\n"
        "Do NOT use it for:\n"
        "- Waiting for the user to do something (log in, scan a code, drop a file). "
        "Just say what you need and end the turn. Their next message resumes you naturally; "
        "there is nothing to suspend.\n"
        "- Waiting for a background job Nano itself started. The runtime already tracks it "
        "and will resume you when it finishes.\n"
        "- Anything you can finish right now with the tools you have.\n\n"
        "⚠️ The ‘verify first’ rule applies only to condition_recheck. Nano does no work while waiting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "What Nano is waiting for. Short user-facing text."},
            "timer_seconds": {
                "type": "integer",
                "description": "How many seconds until Nano should check again. Required.",
            },
            "intent": {
                "type": "string",
                "enum": ["scheduled_plan", "condition_recheck"],
                "description": (
                    "scheduled_plan only when the user explicitly asked Nano to run or "
                    "remind something after this duration; the timer itself is their plan. "
                    "condition_recheck when this is merely Nano's own later check of an "
                    "external condition."
                ),
            },
        },
        "required": ["reason", "timer_seconds", "intent"],
    },
}

# ── 内置工具清单（用于工具感知注入，名称→一句话说明）────────────────────

# ── 路由重构 请求修改已有 Skill（元工具，进入确认流而非直接执行）────────
_UPDATE_EXISTING_SKILL_MANIFEST = {
    "name": "update_existing_skill",
    "description": (
        "Request modification of an already deployed Nano Skill. "
        "This is a meta-tool: it starts a confirmation flow and does not modify files immediately.\n\n"
        "Use when:\n"
        "- The user clearly wants to modify, optimize, fix, update, or extend an existing Skill's implementation, rules, thresholds, fields, parameters, or output behavior.\n"
        "- The user says to change 'that Skill' or 'the previous tool', and context clearly points to a recently used or deployed Skill.\n"
        "- An existing Skill is relevant but lacks the behavior the user now wants.\n\n"
        "Do not use when:\n"
        "- The user wants to run a Skill; call that Skill directly.\n"
        "- The user wants to create a new Skill; use create_new_skill.\n"
        "- The user wants to delete, disable, enable, or rename a Skill; use manage_existing_skill.\n"
        "- The user only wants to revise the current reply, copywriting, or one-off output format.\n"
        "- A pending Skill draft is under review; 'change it' usually means the pending draft, not a deployed Skill.\n"
        "- The user only asks about Skill status, capability, or existence; use inspect_existing_skill or answer directly.\n\n"
        "Before calling:\n"
        "- If the target Skill is unclear, skill_name may be omitted only when runtime context can resolve it. Never invent a Skill name.\n"
        "- If the change involves business rules, thresholds, field names, Excel/CSV, or KB files, inspect source/files first and put verified findings into change_summary.\n"
        "- change_summary must describe the durable behavior change, not code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Exact target Skill class name. May be omitted only when context can resolve it."
            },
            "change_summary": {
                "type": "string",
                "description": "Durable behavior/rule/field/threshold/output change requested by the user. Do not write code."
            }
        },
        "required": ["change_summary"]
    }
}

# ── 路由重构收尾：请求删除/禁用/启用已有 Skill（元工具，进入确认流而非直接执行）──
# 取代旧的前置分类器硬路由（SKILL_DELETE/DISABLE/ENABLE 关键词匹配）。
# "关/删/开" 这类裸动词在分类器里会被任意语境误触（如"把这个面板关了吧"
# 被读成禁用 Skill）。改成元工具后由主决策模型结合上下文判断是否真的
# 是在管理 Skill，确认流（_request_management_confirmation）照旧触发。
# ⭐⭐ MCP 的管理元工具 —— **形状照抄 `manage_existing_skill`**。
#
# 📌 为什么照抄而不是另设计:用户对这两件事的心智是同一个
#    （「把那个东西关了 / 删了」），而 Skill 那套已经被实测磨过。
#    ⇒ 同一个心智用两套交互，多出来的复杂度全落在用户身上。
#
# ⚠️ **比 Skill 多一个 `retry`**：MCP 多一个 Skill 没有的状态 —— **连不上**。
#    📌 「连不上」和「被禁用」对用户是两件事：
#       前者该说「再试试」，后者该说「要不要打开」。
#       把它们合成一个操作，就等于让用户去猜现在是哪种。
#
# ⚠️ 删除同样走**自然语言二次确认**（不是弹窗）—— 与 Skill 一致。
#    弹窗只在用户走 UI 按钮删除时出现。
# ⭐⭐ 接入一个新的 MCP —— **它和 `manage_mcp` 是两件事**。
#
# 📌 分开的理由：`manage_mcp` 动的是**已经在**的东西（可逆、低风险）；
#    这一个是**把一个陌生的第三方装进用户的机器**。
#    合成一个工具的话，那四个可逆操作会被这一个不可逆操作的授权成本拖累。
#
# 🔴 **授权弹窗无视 auto 模式**（2026-08-28 定，理由见 handler）。
# ⭐ 发现链第一条：Official MCP Registry。
#    另外三条**不给新工具**（GitHub / 官网用 fetch 或 OpenPageWithBrowser 读，
#    开放世界用 SearchTheWeb）—— 📌 **不给已经能做的事再造一个工具。**
#
# 🔴 `search` 是**子串匹配，不是全文检索**（2026-08-29 实测）：
#      "playwright browser automation" → 0 条
#      "playwright"                    → 2 条
#    ⇒ 说明里必须写死这一条，否则模型按自然语言习惯传一整句，
#      会拿到一个**假的空结果**，然后如实地告诉用户「没有这样的 server」。
#    📌 **一个「没搜到」的结果，必须先确认查询方式对不对，再当成「不存在」。**
_NL = chr(10)
_SEARCH_MCP_REGISTRY_MANIFEST = {
    "name": "search_mcp_registry",
    "description": _NL.join([
        "Search the official MCP registry for servers that provide a capability. "
        "Returns candidates with install shape, version, repo and last-updated date.",
        "",
        "🔴 The query is matched as a SUBSTRING, not as natural language. Use one or "
        "two short keywords (\"playwright\", \"pdf\", \"postgres\"), not a sentence. "
        "\"playwright browser automation\" returns nothing while \"playwright\" returns "
        "results - an empty result from a long query means the query was wrong, not "
        "that no such server exists.",
        "",
        "Use when:",
        "- You already decided a new server is genuinely needed. See connect_mcp first: "
        "if a library you already have would do the job, use that instead.",
        "",
        "Do not use when:",
        "- The server is already configured; manage_mcp handles those.",
        "- You only need to read a web page or a document; that is fetch's job.",
        "",
        "What it does NOT tell you: whether a server is safe or well maintained beyond "
        "its timestamps. The registry is publishing infrastructure that anyone can "
        "publish to - it is not a safety review. Check the repo before proposing "
        "anything to the user.",
    ]),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "One or two short keywords. Substring match, not a sentence.",
            },
            "limit": {
                "type": "integer",
                "description": "How many candidates to return (default 8, max 20).",
            },
        },
        "required": ["query"],
    },
}

# ⭐⭐ **抑制器**（2026-08-29 定形态）
#
# Microsoft Learn / Context7 **不是发现入口，是抑制器** ——
# 它们发生在「找 MCP」之前：先查一下，发现现有 SDK 二十行就能做 → 走
# run_scratch_code → **根本不用装 MCP**。
# 📌 **这条把「接入 MCP」的成功标准从「能找到 MCP」改成了「能不装就不装」** ——
#    一个能自主装插件的能力，最该被评价的是**它有多克制**。
#
# 🔴🔴 **为什么写成提示词里的一条判断，而不是强制流程** —— 2026-08-29：
#   ① 强制会变成**表演性的走流程**：模型明明已经知道没有现成库，还得走一遍过场。
#      ⇒ 让这一步跟**模型自身的强度**挂钩，靠它意识到，而不是靠系统押着走。
#   ② 🔴 更要命的是绑定：强制 = 把整个 MCP 自进化**绑死在这两个 server 上**。
#        现在    它们变强 → 我们受益；它们死了 → 少一条路，直接去网上搜
#        否掉的  它们死了 → Nano 只能跟用户说「这个我做不了」
#      📌 **利用一个东西 ≠ 绑定死一个东西 ——
#         区别在于它消失时，你是「少一条路」还是「没路了」。**
#   ⇒ 所以最后那两句（"judgement call, not a required step" /
#     "or those lookups are unavailable"）**不是客套，是这条设计的一半**：
#     它们明写了「这两个查不了也照样往下走」。
#
# ⚠️ 挂在 `connect_mcp` 的说明里，不挂在主决策提示词里：这个工具是 DEFERRED，
#    模型 load 它的那一刻，正是它开始考虑装 MCP 的那一刻。
#    📌 **抑制器该挂在被抑制的那个动作的说明上，不该挂在所有人都要读的地方** ——
#       挂后者，每一句「你好」都要为它付钱，而它一年用不上几次。
_CONNECT_MCP_MANIFEST = {
    "name": "connect_mcp",
    "description": (
        "Ask the user to approve installing a new MCP server on this machine, "
        "given its configuration JSON.\n\n"
        "The user always sees an approval dialog first - nothing is installed or executed "
        "before they approve. Do not promise the user it is already done.\n\n"
        "Use when:\n"
        "- The user gave you an MCP configuration snippet and wants it added.\n"
        "- You found a server that provides a capability the task needs, and the user agreed to add it.\n\n"
        "Do not use when:\n"
        "- The server is already configured; use manage_mcp to enable or reconnect it.\n"
        "- You are only guessing that some server might help; find out what it actually is first.\n"
        "- The task can be done with code you can already write. Installing a "
        "third-party server is the expensive answer: it downloads and runs "
        "someone else's code on the user's machine, and it stays there. "
        "Check first whether a library that already exists does the job - "
        "microsoft-learn and context7 are there to look that up, and "
        "run_scratch_code can try it. If twenty lines of a library you "
        "already have would do it, do that instead. "
        "This is a judgement call, not a required step: if you already know "
        "no library covers it, or those lookups are unavailable, go ahead "
        "and look for a server.\n\n"
        "purpose_line must state, in the user's own language, WHAT THE USER IS TRYING TO DO "
        "that makes this server necessary. Write the task, not the tool: "
        "\"convert the PDF tables to Excel\", not \"install a PDF MCP\". "
        "If the task is a sub-step you inferred, say the sub-step."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "config_json": {
                "type": "string",
                "description": ("The MCP server configuration, standard ecosystem snippet: "
                                "{\"mcpServers\": {\"name\": {...}}}. Exactly one server."),
            },
            "purpose_line": {
                "type": "string",
                "description": ("One line, in the user's language: what the user is trying to "
                                "do that requires this server."),
            },
            "what_it_does": {
                "type": "string",
                "description": ("One line, in the user's language: what this MCP server itself "
                                "does. Describe the server, not this task."),
            },
        },
        "required": ["config_json", "purpose_line", "what_it_does"],
    },
}

_MANAGE_MCP_MANIFEST = {
    "name": "manage_mcp",
    "description": (
        "Enable, disable, delete or reconnect an MCP server that is already configured on this machine. "
        "This is a meta-tool: delete starts a second confirmation flow and does not remove anything immediately.\n\n"
        "Use when:\n"
        "- The user wants to turn an MCP server off or back on.\n"
        "- The user wants to remove an MCP server they no longer need.\n"
        "- An MCP tool failed because its server is disconnected and it is worth reconnecting.\n\n"
        "Do not use when:\n"
        "- You want to ADD a new MCP server; that is a different flow and needs the user's approval first.\n"
        "- An MCP tool failed because its server is disabled - then ask the user whether to enable it, "
        "rather than enabling it on your own.\n"
        "- The user is talking about a Skill, not an MCP server; use manage_existing_skill.\n"
        "- The target server is unclear and context cannot resolve it; ask which one instead of guessing a name.\n\n"
        "operation must be one of: enable, disable, delete, reconnect. "
        "server_name must be an existing MCP server name exactly as it appears in the configuration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                # 🔴 **约束写进 schema，不写进散文。**
                #    实测 2026-08-28：description 第一句写 "reconnect"，
                #    枚举那句写 "retry" —— 两处自相矛盾，模型填了 reconnect，
                #    然后被 `_op not in _OPS` 挡下。
                #    📌 **一个只存在于散文里的约束，写的人自己都会写岔。**
                #       enum 是机器可读的单一出处，模型侧直接受约束。
                "enum": ["enable", "disable", "delete", "reconnect"],
                "description": "enable / disable / delete / reconnect",
            },
            "server_name": {
                "type": "string",
                "description": "Exact MCP server name as configured, e.g. \"context7\".",
            },
        },
        "required": ["operation", "server_name"],
    },
}

_MANAGE_EXISTING_SKILL_MANIFEST = {
    "name": "manage_existing_skill",
    "description": (
        "Request delete, disable, or enable for an already deployed Nano Skill. "
        "This is a meta-tool: it starts a second confirmation flow and does not change files immediately.\n\n"
        "Use when:\n"
        "- The user clearly wants to delete or remove an existing Skill.\n"
        "- The user clearly wants to disable, stop, or turn off a Skill tool itself, not a UI panel.\n"
        "- The user clearly wants to enable or restore a disabled Skill.\n\n"
        "Do not use when:\n"
        "- The user wants to modify Skill logic, rules, or parameters; use update_existing_skill.\n"
        "- The user wants to run a Skill; call that Skill directly.\n"
        "- The user wants to create a new Skill; use create_new_skill.\n"
        "- The user says close/turn off/shut down about UI panels, drawers, task lists, or windows rather than a Skill tool.\n"
        "- The target Skill is unclear and context cannot resolve it; ask which Skill instead of inventing a name.\n\n"
        "operation must be one of: delete, disable, enable. skill_name must be a real existing Skill class name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "delete / disable / enable",
            },
            "skill_name": {
                "type": "string",
                "description": "Exact target Skill class name.",
            },
        },
        "required": ["operation", "skill_name"],
    },
}

# ── 路由重构：创建新 Skill（元工具，取代旧 SKILL_CREATE 前置分类器硬路由）──
# 旧逻辑：前置分类器判 SKILL_CREATE>=0.75 → 直接进探索阶段。问题：分类器是单选
# 且没有"多步执行任务"这个类，会把"先查时间→等30秒→读文件"这类一次性执行任务
# 误判成 SKILL_CREATE（实测 0.85），劫持进探索阶段（探索阶段不执行工具、不注入
# wait_for/action 工具），导致模型把计划当文字吐出、啥也没真跑。
# 修法（做全做优，对齐 update_existing_skill / manage_existing_skill 元工具范式）：
# 把"创建 Skill"也变成主决策循环的元工具。"创建可复用能力 vs 一次性执行"这个区分
# 本就该由有完整上下文的主决策模型判断，而不是廉价前置分类器。调用本工具 →
# ReAct 退出 → 路由进 _run_skill_exploration（探索→确认→SkillSpec→代码生成，未变）。
_CREATE_NEW_SKILL_MANIFEST = {
    "name": "create_new_skill",
    "description": (
        "Request creation of a new reusable Nano Skill, meaning a durable tool/capability for future repeated use. "
        "This is a meta-tool: it starts the explore-confirm-generate flow and does not write files immediately.\n\n"
        "Use when:\n"
        "- The user clearly wants a reusable tool or Skill, such as 'create a tool/Skill that can...'.\n"
        "- The user describes automation meant for future repeated use, such as 'whenever I say X, do Y' or 'make a tool to organize downloads'.\n"
        "- The intent is to preserve a capability, not just complete the current task once.\n\n"
        "Never use when:\n"
        "- The user only wants Nano to execute a multi-step task now, such as 'first..., then..., then...' or 'read X and tell me Y'. "
        "Use existing tools directly instead: existing Skills, os_execute, KB tools, wait_for, etc.\n"
        "- Multi-step does not mean Skill creation. The key question is whether the user wants this saved for repeated future use.\n"
        "- The user wants to modify an existing Skill; use update_existing_skill.\n"
        "- The user wants to delete, disable, or enable an existing Skill; use manage_existing_skill.\n"
        "- An existing Skill already matches; call it directly or update it if needed.\n\n"
        "requirement must preserve concrete user details such as rules, thresholds, field names, filenames, and exact business logic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # ⚠️⚠️ **两个字段的描述不许重叠。** 2026-08-13 实测第一版就栽在这里：
            #    `requirement` 原文写的是「Complete Skill requirement. Preserve concrete
            #    rules, thresholds, fields, filenames, and user wording.」——
            #    那听起来就是"什么都往这里放"，于是模型**把内容全塞进了 requirement，
            #    `handoff_summary` 留空**（实测两次都是 `handoff=0字符`）。
            # 📌 **两个参数如果描述重叠，模型只会填它先看到的那个** ——
            #    这不是模型不听话，是同一件事被说了两遍。
            "requirement": {
                "type": "string",
                "description": (
                    "One sentence: what durable capability the user wants saved. "
                    "This is the headline only — put every concrete detail "
                    "(rules, thresholds, real field names, filenames) in handoff_summary instead, "
                    "because that is the field the code writer actually reads."
                )
            },
            # ⭐⭐⭐ [2026-08-13] 这两个参数是**探索子循环被拆掉之后**，
            #    它唯一不可替代的产出（向 SkillWriter 的交接）搬到入口来的形态。
            #    见本文件 `_exit_create_new_skill` 的 docstring。
            "handoff_summary": {
                "type": "string",
                "description": (
                    "Self-contained handoff for the Skill writer. It runs as a SEPARATE model call and "
                    "CANNOT see this conversation, your tool results, or the files you just read. "
                    "Anything it needs must be written here.\n\n"
                    "Include:\n"
                    "- The user's concrete rules, thresholds and wording, verbatim.\n"
                    "- Real field/column names, filenames and values you actually looked up — "
                    "use the ORIGINAL identifiers, never translated or guessed ones.\n"
                    "- If you inspected an existing Skill, what differs and why a new one is needed.\n"
                    "- If a rule document disagrees with what the user said, state both and which one was chosen.\n\n"
                    "Be honest about provenance: mark what you actually verified versus what the user told you. "
                    "If the requirement genuinely needs no external lookup (e.g. a date or format tool), "
                    "just say it is self-contained — do NOT invent a verification you did not perform."
                )
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Blocking questions you still need the user to answer. "
                    "Pass an empty array when nothing blocks you.\n"
                    "If this is non-empty, NO code will be generated — the questions are shown to the user "
                    "and remembered until they answer. So do not put non-blocking remarks here, and do not "
                    "claim you are ready while also asking something in your reply."
                )
            }
        },
        # ⚠️ `handoff_summary` / `open_questions` 都是 **required**。
        # 📌 但要说准它保证了什么：schema 只能强制「必须填」，
        #    **强制不了「填的是真的」** —— 模型可以写一句"已确认字段为 xxx"而根本没查。
        #    它真正结构化保证的是**下游 Writer 不再丢失上游已形成的上下文**
        #    （Writer 是另一次模型调用、另一套 system guide，客观上看不见这里的历史）。
        "required": ["requirement", "handoff_summary", "open_questions"]
    }
}

# ── Skill 创建探索阶段·查看已有 Skill 真实实现 ───────────────────────
# 目的：杜绝"只看 description 猜名字像不像就判断能不能复用"。
# description 是一句话摘要，可能和实际代码里的具体规则/阈值/字段不一致
# （比如 description 写"根据分级规则计算等级"，但代码里硬编码的阈值
# 是上一次任务的"5000/3次"，这次任务的阈值是"3000元"——光看 description
# 完全看不出冲突）。这个工具返回真实源代码，让探索阶段能做到
# "证据对比"而不是"印象匹配"。
_INSPECT_EXISTING_SKILL_MANIFEST = {
    "name": "inspect_existing_skill",
    "description": (
        "Inspect the real source code of an existing Skill, including get_spec(), required_inputs, and hardcoded rules, thresholds, fields, and logic in run().\n\n"
        "Use when an existing Skill name or description seems related to the current request. "
        "You must inspect the real code before deciding whether to reuse, update, or replace it. "
        "Do not rely only on the short description; it may omit thresholds/fields or be stale after previous rule changes.\n\n"
        "After inspection, explicitly conclude one of:\n"
        "- Reuse directly: the existing code's rules/fields fully match the current request.\n"
        "- Update existing: the Skill is related but its concrete rules, thresholds, fields, or logic differ; state the exact difference.\n"
        "- Create new: no relevant existing Skill fits.\n\n"
        "Do not claim an existing Skill can satisfy the request without inspecting its source when the match depends on concrete business rules."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill class name to inspect."
            }
        },
        "required": ["skill_name"]
    }
}


# ── 回答未决交互 ─────────────────────────────────────────────
# 取代原来的路由劫持：过去用户回答澄清问题时，代码用关键词表猜"这句是不是回答"
# （命中 `_NEW_REQUEST_SIGNALS` 或者"长度>20 且不含 Skill 词"就判成新话题）。
# 那套启发式两头都会错，而且模型**根本不知道有个问题挂在那里**。
# 现在把判断交给有完整上下文的主决策模型。
#
# ⚠️ relation 只有三个值，**没有 UNRELATED**。
# UNRELATED 是模型判断，不是命令：它对应的正确行为是「不调这个工具」。
# 做成参数只会多一次无意义调用、一个无意义 revision，
# 以及一个"什么也不做"的命令分支）。
_ANSWER_INTERACTION_MANIFEST = {
    "name": "answer_open_interaction",
    "description": (
        "Record the user's reply to an open interaction listed in [Open Interactions], then continue that flow.\n\n"
        "Call when the user's message is a reply to one of the listed open questions, "
        "even if they also adjust the requirement in the same breath.\n\n"
        "Do NOT call when the user's message is about something else. "
        "In that case just handle their new request normally and leave the interaction open; "
        "it stays on screen and can still be answered later.\n\n"
        "relation:\n"
        "- ANSWER: a plain reply to the question.\n"
        "- ANSWER_AND_AMENDMENT: a reply that also changes the original requirement "
        "(e.g. 'use >=6000, and export as CSV instead').\n"
        "- CANCEL: the user no longer wants this at all "
        "(e.g. 'forget it', 'never mind, drop it').\n\n"
        "answer_verbatim must be the user's own wording, copied as-is. "
        "Do not summarize, translate, normalize, or 'clean up' the phrasing — "
        "the downstream flow needs exactly what they said."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "interaction_id": {
                "type": "string",
                "description": "The id shown in [Open Interactions], e.g. int_a1b2c3d4e5."
            },
            "answer_verbatim": {
                "type": "string",
                "description": "The user's reply in their own words, copied verbatim."
            },
            "relation": {
                "type": "string",
                "enum": ["ANSWER", "ANSWER_AND_AMENDMENT", "CANCEL"],
                "description": "How this message relates to the open interaction."
            }
        },
        "required": ["interaction_id", "answer_verbatim", "relation"]
    }
}




# ── OS 层工具声明（供 _handle_os_task 使用）──────────────────
# include_os=True 时追加到 _build_skills_info，普通对话永远看不到。
# ⭐⭐⭐ [2026-08-23] **`os_execute` 拆成两个工具。**
#
# 🔴 拆的两个理由（独立成立，任一条都够）：
#   ① **缓存**：缓存前缀顺序是 `tools → system → messages`，tools 在最前面 ——
#      它一变，后面全废。而 `os_execute` 要常驻（频率最高、且模型忘了 load 就
#      直接调它，白白一次往返）；可它整个 3017 字符里**大半是 GUI 模拟**，
#      而 GUI 频率并不高。
#      ⚠️ 不拆的话，为了让绑定关系自洽，还得把 `look_at_screen` + `set_window_mode`
#         一起搬进常驻 → 回到 Changelog 早期那个「常驻工具膨胀」的老problem。
#   ② **结构自洽**（已明确，比①更硬）：我们一边声明「mini 窗 ⟂ GUI 模拟」
#      是必绑定，一边准备把 GUI 模拟放进常驻、把被它绑定的两个放进按需加载。
#      📌 **一组被声明为「必绑定」的东西，被放进两个不同的可见性层级** ——
#         那不是省字符的问题，是设计本身在自相矛盾，而且它会精确地产生
#         「Nano 用了 GUI 动作却不知道自己还缺两个东西」这种失败。
#
# ⭐ 拆完之后这一簇天然同层（同进同出）：
#       computer_use（鼠标键盘 + 窗口 + 截图）· look_at_screen（眼睛）· set_window_mode（mini 窗）
#   而 `os_execute` 的语义变干净了：**完全不碰图形界面**。
#
# ⚠️⚠️ **enum 一律从 `dsl._ACTIONS` 派生，不许手抄。**
#    📌 这里原来就是**手抄**的，而且已经过期。另一份手抄件（兜底指引）当年漏了 10 个，
#       **而且专挑高频项漏**。**一份手抄的清单，它的过期是静默的。**
#    ✅ **2026-08-24 已解除**：那个写死的常量（拆分时的 36 个快照）已删除，
#       改为真派生 —— enum 从 36 → **38**，补回 `move` 与 `request_user_choice`。
def _os_actions_for(tool_name: str) -> list:
    """从 `dsl._ACTIONS` 派生某个工具的 action enum。**唯一出处。**

    ═══ 判据：不是「`_ACTIONS` 里全部」，是「**已挂载执行器**的那些」═══

    🔴 全抄会把 `read_screen_region` 写进 schema —— 它**没有执行器**
       （只有鼠标键盘档才接 VisionLocator，低档位在校验层就该拒掉它）。
       📌 那种失败最难查：**schema 说有，运行说没有** ——
          模型会反复尝试，而每一次都合法地失败。

    ⭐ 「有没有执行器」的权威出处是**路由表本身**（`dispatch._ROUTE_SPEC`），
       而不是某个 `implemented=True` 之类的声明 ——
       📌 **单一出处必须是「事实本身」，不是「对事实的声明」**：
          声明会和事实脱节，而那正是本项要修的问题。

    ⚠️ 两类合起来才是全集：
         · `ROUTED_ACTIONS`        —— 真的会去操作这台电脑的
         · `CONTROL_FLOW_ACTIONS`  —— 纯信号（`request_replan` / `request_user_choice`），
                                      执行器拿到就交回上层，**不进真正执行**
       🔴 漏掉后者的话，`request_replan` 会从 enum 里消失 —— 而它本来就在。
       📌 **「有执行器」和「能被调用」不是一回事**，控制流动作正好落在缝里。

    ⚠️ 排序固定（按 `_ACTIONS` 的声明序）—— 📌 缓存是内容寻址的，
       同一个工具集必须产生**逐字节相同**的数组，顺序一抖就白白 miss。
    """
    from core.os_layer import dsl as _d
    from core.os_layer.dispatch import ROUTED_ACTIONS as _routed
    _implemented = set(_routed) | set(_d.CONTROL_FLOW_ACTIONS)
    return [n for n, a in _d._ACTIONS.items()
            if a.tool == tool_name and n in _implemented]


_OS_MANIFEST = {
    "name": "os_execute",
    "description": (
        # ⚠️ 拆分后**不再提** mouse/keyboard/screenshots —— 那些在 `computer_use`。
        #    📌 一句描述如果还宣称自己有已经搬走的能力，模型会照着它调，
        #       然后拿到「没有这个 action」。**提示词说错了不报错，只会让它用错工具。**
        "Operate this computer WITHOUT touching the graphical interface: inspect system "
        "state, read/write/move/delete files, run commands, read and write the clipboard, "
        "launch apps, open URLs, read the registry, list windows. One action per call.\n"
        "declared_risk: read-only=1; file/app/system writes=2 (confirmed); high-risk=3 "
        "(run_command, write_registry, file_delete, file_move, kill_app, manage_service, "
        "set_env_var, schedule_task, modify_startup, network_config).\n"
        # ⭐ 「优先命令行、GUI 是最后手段」这句留在**这一侧**：
        #    它是在劝模型**别去**用另一个工具，写在这里才对得上语境。
        "⭐ Prefer this tool over clicking things on screen: run_command, file_write and "
        "launch_app are faster and far more reliable than driving the GUI. Reach for "
        "computer_use only when there is genuinely no other way.\n"
        "For anything that needs the graphical interface — clicking, typing into a window, "
        "screenshots, moving or closing windows — use the computer_use tool instead. "
        "It is not loaded by default; ask for it when you need it.\n"
        # ⭐⭐ file_read 的定位说明。
        #
        # ⚠️ **只能写在这里** —— `dsl.ActionDef.desc` 里那句「大文件请改用
        #    load_full_file 的 offset/limit」写得没错，但 `.desc` 全仓**零消费方**，
        #    模型一个字都看不到；`os_execute` 的 action 是纯 enum，没有逐项描述。
        #    📌 一句只有我们看得见的劝阻，等于没有劝阻。
        #
        # ⚠️ **陈述存在，不下命令**（已定：「不要强制要求它去用，只是告诉它
        #    存在这个办法，具体情况具体判断」）—— 所以这里没有 must / should，
        #    并且明说了 file_read 什么时候仍然是对的。
        #    📌 同「不留任何专门给某个模型的修正动作」：小文件一把梭就是对的，
        #       那时候多一句命令只是噪音。
        # ⭐ 形状照抄上面那句 "Prefer this tool over clicking things on screen"：
        #    **劝模型别去用另一个工具，写在语境这一侧才对得上。**
        "file_read hands you the whole file at once and cannot resume from where it was "
        "cut off; for a long file, load_full_file reads the same path in slices "
        "(offset/limit). Small files, and read-then-write inside a single plan, are fine here.\n"
        "Paths: use %USERPROFILE%\\Desktop|Documents|Downloads; never guess the username. "
        "Before delete/move/write when the extension is uncertain, list_dir first.\n"
        "params.wait_for_result=false: use this for run_command when you already know you "
        "are NOT going to sit and wait for it - because the user asked for something else "
        "in the same breath, or because you have other work lined up that does not need "
        "its output. The command still starts and keeps running; you just get control back "
        "immediately instead of being stuck. Then call dont_wait to put it in the task "
        "drawer. Leave it out when you do need the result - that is the normal case."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # ⭐ 从 `dsl._ACTIONS` 派生，**不手抄**（理由见上面那段）。
            "action": {"type": "string", "enum": _os_actions_for("os_execute")},
            "params": {
                "type": "object",
                "description": (
                    "Parameters for the selected action. Common examples: "
                    "file_write={path,content,mode}; file_read/file_delete={path}; "
                    "file_move={path,dest}; run_command={command,wait_for_result}; "
                    "list_dir={path}; clipboard_write={text}; open_url={url}; "
                    "launch_app={target}."
                ),
            },
            "declared_risk": {"type": "integer",
                              "description": "Risk level: 1 = read-only, 2 = write, 3 = high-risk."},
            "reason": {"type": "string", "description": "One short reason for the action."},
        },
        "required": ["action", "declared_risk"],
    },
}


# ⭐⭐⭐ [2026-08-23] `computer_use` —— 从 `os_execute` 拆出来的**图形界面那一半**。
#
# ⚠️ 它和 `look_at_screen`（眼睛）、`set_window_mode`（mini 窗）是**同一簇**，
#    三个都 DEFERRED、同进同出。拆分之前它们分属两个可见性层级，
#    而我们又声明了它们必绑定 —— 那正是这次要消除的自相矛盾。
_COMPUTER_USE_MANIFEST = {
    "name": "computer_use",
    "description": (
        "Drive the graphical interface of this computer: move and click the mouse, type "
        "with the keyboard, scroll and drag, take screenshots, and minimize/close/switch "
        "windows. One action per call.\n"
        "declared_risk: looking (screenshot, get_cursor_pos) = 1; anything that moves the "
        "mouse, types, or changes a window = 2 (confirmed).\n"
        # 🔴 [2026-08-25 实测] Nano 调了 `screenshot`，拿回一个路径，
        #    然后**去读那个 PNG 文件**，得出「截图是二进制PNG文件，无法直接读取文本内容」。
        #    整轮 `3 tools · 1 failed · 27.3s` 白烧。
        # 📌 **一个叫「截图」的动作，产出却是一个文件路径 —— 而模型调它是为了「看到」。**
        #    它没做错什么：名字承诺了「看」，返回值给的是「存」。
        # ⚠️ 真正让它看到屏幕的是 `look_at_screen`；这个动作只留档（审计凭据）。
        #    两个名字太像、语义相反，必须在描述里说死。
        "WARNING: the `screenshot` action does NOT show you anything. It only writes a "
        "file to disk for the audit record and returns its path - you cannot read that "
        "file, and there is no reason to try. To actually SEE the screen, use "
        "look_at_screen.\n"
        # ⭐ 「用语义 target，不要坐标」这句跟着 click 一起搬过来 —— 它只对这里成立。
        "Click screen elements with params={target:'semantic description'} — never "
        "coordinates; the system locates the element and visually confirms it.\n"
        "type_text enters at the current focus; click the field first if it must be focused.\n"
        # ⚠️⚠️ 两条绑定关系，**强度不同，必须分开说**。
        #    📌 写成同一种强度，模型要么该缩小时不缩，要么为了走流程白缩一次。
        "⚠️ BEFORE your first mouse/keyboard/window action in a task, call "
        "set_window_mode('mini'). Nano's own window sits on the same screen you are about "
        "to operate; at normal size it covers a large part of it and you will click the "
        "wrong thing. This one is not optional.\n"
        # 🔴🔴 [2026-08-24] 这里原来写「Nano 的窗口会被涂黑」—— 遮罩已于 08-23 删除。
        #    ⚠️ 上一轮**声称已经扫过所有提示词**，实际只改了 `set_window_mode` 那一处，
        #       **漏了两处**（本处 + 下面 `_OS_AWARENESS` 那处）。
        #    📌 **一句描述一个已经不存在的机制的话，比没有这句更坏** ——
        #       它不报错，只是让模型按一个假的前提做决定（「反正会被涂黑，那就缩」）。
        #    📌 而这次漏掉的教训更具体：**「扫过了」不是证据，`grep` 的结果才是。**
        "For screenshots the same shrink usually helps — Nano minimizes itself out of the "
        "shot, and at normal size it would otherwise cover a large part of the screen. "
        "But judge it yourself: if Nano is not covering what you need to see (the target "
        "app is fullscreen in front, or it is on another monitor), just take the shot. "
        "Do not shrink as a ritual.\n"
        # ⭐ 反向指引：把模型劝回更可靠的那条路。
        "⭐ Driving the GUI is the last resort. If the same job can be done with a command, "
        "a file write, or launching an app directly, use os_execute instead — it is faster "
        "and does not depend on what happens to be on screen."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _os_actions_for("computer_use")},
            "params": {
                "type": "object",
                "description": (
                    "Parameters for the selected action. Common examples: "
                    "click/double_click/right_click={target:'semantic target description'} "
                    "with no coordinates; type_text={text}; hotkey={keys}; "
                    "scroll={direction,amount}; drag={from_x,from_y,to_x,to_y}; "
                    "screenshot={} ; win_switch/win_minimize/win_close={title}."
                ),
            },
            "declared_risk": {"type": "integer",
                              "description": "Risk level: 1 = looking only, 2 = mouse/keyboard/window."},
            "reason": {"type": "string", "description": "One short reason for the action."},
        },
        "required": ["action", "declared_risk"],
    },
}

# OS 能力说明（双循环合一后：os_execute 始终注入主 ReAct 循环，OS 是无差别内置能力）。
# "你随时可以用屏幕能力，按需调用，可与其它工具自由组合"，注入到每一轮主决策的 guide 里。
_OS_CAPABILITY_PROMPT = (
    # ⭐⭐ [2026-08-23 拆分] 标题不再只挂 `os_execute`，并且**点名两个工具的分工**。
    #    📌 一个还在宣称自己能点鼠标的注入块，会让模型带着错误的能力预期规划整轮。
    "\n\n[Computer and Screen Capability]\n"
    "Two separate tools, and picking the right one matters:\n"
    "  - os_execute: always available. Files, commands, clipboard, launching apps, reading "
    "system and window state. It does NOT touch the graphical interface.\n"
    "  - computer_use: load it first. Mouse, keyboard, scrolling, dragging, screenshots, "
    "and minimizing/closing/switching windows.\n"
    "Reach for os_execute first: a command or a file write is faster and far more reliable "
    "than driving the screen. Use computer_use when there is genuinely no other way.\n"
    "Call one action at a time, observe the result, then decide the next step. "
    "They may be combined with other tools.\n"

    # ⚠️⚠️ **两条绑定关系，强度不同 —— 拆开说**。
    #    🔴 旧文把 click / typing / screenshot / app launch / window switching
    #       写成**同一条硬规则**，于是「截图前必须先缩小」变成了仪式：
    #       多一次往返、还把自己无谓地缩小了。
    #    📌 「Nano 此刻碍不碍事」只有模型看得见上下文 ——
    #       合我们那条分工：**系统答发生了什么，模型答所以怎么办。**
    "Get out of your own way - two rules, and they are NOT equally strict:\n"
    "  - Mouse, keyboard, or window actions: call set_window_mode('mini') FIRST. Your own "
    "window sits on the screen you are about to operate, and you will hit the wrong thing. "
    "This one is not optional.\n"
    # 🔴 [2026-08-24] 同上，遮罩已删。这是漏掉的第二处。
    "  - Screenshots: shrinking usually helps, because Nano minimizes itself out of the "
    "shot and at full size it would otherwise cover a large part of the screen. But judge it "
    "yourself: if you are not covering what matters (the target app is fullscreen in front, "
    "or on another monitor), just take the shot. Do not shrink as a ritual.\n"
    "Never shrink for pure command-line work, file-only work, KB queries, or memory "
    "queries - and never when the task is to observe or operate Nano's own UI.\n"

    "Use eyes with restraint: call look_at_screen only when the screen state is genuinely uncertain, "
    "or after a key operation when failure would mislead later steps. Do not screenshot before every step. "
    "After one look, reuse that visual understanding for several steps unless the screen meaningfully changes. Nano's own window is excluded by default.\n"

    "KB files are not the same as real computer files. query_local_knowledge, load_full_file, get_file_path, and list_knowledge_files only handle KB files. "
    "For real files on the desktop or arbitrary disk paths, use os_execute list_dir, file_read, or file_write. "
    "Do not call get_file_path for a real desktop file such as 'desktop 1.txt'.\n"

    "Waiting for external changes: if progress depends on something Nano cannot complete now, such as login, download completion, or another person's reply, "
    "use wait_for instead of blocking or pretending to wait in the same turn."
)
# 注：这里【曾经】还有一条 "Soft abort" 规则（用户说不想让 Nano 动电脑 → 模型发
# request_replan(USER_ABORT) → 置进程级软急停标志）。软急停整套已删除，
# 原因见 core/os_layer/safety.py 模块头。用户要停手，靠甩鼠标 failsafe 或 Ctrl+`
# 热键，两者都是瞬发的物理手段，比让模型判断可靠得多。


# 不可被本地 Skill 覆盖的保留工具名（一并做了）
# Phase 2 新增 load_full_file 和 list_knowledge_files
_RESERVED_TOOL_NAMES = {
    "WriteSkill",

    "query_local_knowledge",
    "load_full_file",           # Phase 2 新增
    "list_knowledge_files",     # Phase 2 新增
    "get_file_path",            # Phase 4 新增
    "recall_working_memory",    # 新增
    "task_boundary",            # 任务边界
    "set_next_checkin",         # 回看设计
    "write_user_note",          # 新增
    "os_execute",               # OS层新增
    "update_existing_skill",    # 路由重构新增
}


# 全局标志：确保整个进程只跑一次 RAG 初始化，防止多次实例化时重复索引
_rag_init_started = False
_rag_init_lock = __import__('threading').Lock()
# 修复：就绪信号必须是进程级共享的，不能是每个 Orchestrator 实例自己的
# threading.Event()——否则"二次实例化短路"分支会把自己的 Event 立即 set()，
# 而真正的索引在第一个实例的后台线程里还没跑完，导致初始化遮罩瞬间消失。
_rag_ready_event = __import__('threading').Event()


# ══════════════════════════════════════════════════════════════════════════
# ToolBatchSpan 的 shadow 接线
# ══════════════════════════════════════════════════════════════════════════
# 铁律：**shadow 绝不影响真实路径。** 所以这几个包装器把 core.runtime 的 import
# 也放在函数体内并吞掉一切异常 —— 哪怕整个 runtime 包出问题（import 失败、库锁了、
# 磁盘满了），ReAct 主循环的行为一个字节都不变。
#
# 三步迁移观测期的定义：旧字段（`_active_tool_batch_open`）权威，内核只镜像并校验，
# **严禁双写**。所以下面从不写那个字段，只把它的值抄下来对答案。
#
# ⚠️ 2A shadow 已于 2026-08-05 收工（8/8 覆盖、唯一分歧已定性），
# 那份覆盖表已经收工并删除，结论留在本文件下面几段注释里。


class _RT_PATH:
    """覆盖表行号的本地别名，避免在主循环里 import 那个枚举。"""
    NORMAL = "p1_normal"
    ERROR_400 = "p3_error_400"
    OS_EARLY_RETURN = "p4_os_early_ret"
    EXIT_MULTI = "p5_exit_multi"
    BATCH_EXCEPTION = "p6_exception"
    GENERATOR_DROPPED = "p7_gen_dropped"


def _rt_shadow_prepare(orch, round_idx: int, names: list, path_tag: str):
    try:
        from core.runtime import toolbatch as _tb
        return _tb.shadow_prepare(
            getattr(orch, "_rt_turn_id", "") or "rtturn_unknown",
            round_idx, list(names), path_tag,
        )
    except Exception:
        return None


def _rt_shadow_open(orch, span_id, call_ids: list) -> None:
    """把 Span 切到 OPEN。**这一刻起 `_active_tool_batch_open` 派生值变 True。**

    ⚠️ 观测期这里的注释是"必须在 `_active_tool_batch_open = True` 之后调 ——
    这一刻才是旧标志的正确采样点"。切写那一步之后**反过来了**：
    没有旧字段可采样，而这次调用**本身**就是那个状态的来源。
    所以它必须在 `memory.add_tool_calls()` 成功之后调 —— 语义是
    "tool_calls 已经写进 memory 了"，早调一步就成了谎。
    """
    try:
        from core.runtime import toolbatch as _tb
        # legacy_flag=None：没有旧字段可对答案（见 shadow_commit 那处说明）
        _tb.shadow_open(span_id, [str(x) for x in call_ids], None)
        orch._rt_open_span = span_id          # 记住它，供各清理点 abort
    except Exception:
        pass


def _rt_shadow_commit(orch, span_id, result_ids: list, path_tag: str) -> None:
    try:
        from core.runtime import toolbatch as _tb
        # 不再传 legacy_flag：旧字段已删除，`_active_tool_batch_open`
        # 现在是从 Span 派生的属性 —— 把它抄下来"对答案"等于**拿内核跟自己比**，
        # 永远一致，是纯噪音。shadow 对答案机制随观测期一起收工（8/8 覆盖已达成）。
        _tb.shadow_commit(span_id, [str(x) for x in result_ids], path_tag, None)
    except Exception:
        pass
    finally:
        # 正常收尾：无论 shadow 记账成功与否都要摘掉，否则下一个清理点会去 abort
        # 一个已经 COMMITTED 的 span（幂等所以无害，但会污染观测）。
        try:
            if getattr(orch, "_rt_open_span", None) == span_id:
                orch._rt_open_span = None
        except Exception:
            pass


def _rt_abort_open_span(orch, reason: str, path_tag: str = "") -> None:
    """在【旧标志被清理】的任何地方调它，把还开着的 span 一起收掉。

    ⚠️ 这是实测 shadow 第一天抓到的缺口：`_clean_damaged_memory` 会把旧标志置回 False
    （本文件里那个第二清理点，早先没记下来），而 span 留在 OPEN，
    于是下一轮 sweep 把它误报成"批次中途抛异常"。
    **旧标志被清到哪里，span 就要跟到哪里** —— 否则 shadow 比的是两个不同时刻的状态。
    """
    span_id = getattr(orch, "_rt_open_span", None)
    if not span_id:
        return
    try:
        from core.runtime import toolbatch as _tb
        # 同上，不再传 legacy_flag。
        _tb.shadow_abort(span_id, reason, None, path_tag)
    except Exception:
        pass
    finally:
        try:
            orch._rt_open_span = None
        except Exception:
            pass


def _rt_lease_acquire(orch, reason: str):
    """Nano 开始操作电脑前拿活动租约。拿不到返回 `None`。

    ⚠️⚠️ **切读之后这个返回值有意义了，调用方必须看。**
    观测期它纯观测，失败返回 None 也无所谓；现在 `None` 表示
    **机器在别人（用户）手里，这次不该动手** —— 忽略它就等于把被动挂起废掉。
    """
    try:
        from core.runtime import oslease as _ol
        return _ol.acquire_activity(reason)
    except Exception as e:
        # 只有"连模块都 import 不进来"这类才会到这儿；oslease 内部已自行吞过一层。
        logger.warning(f"[OSLease] 拿租约调用异常，按拿不到处理: {e}")
        return None


def _rt_lease_heartbeat(orch) -> None:
    """OS 每走一步续一次期。

    ⚠️ 切读之后不续期的代价升级了：租约一过期 `current_activity()` 立刻不认它，
    canary 会在 GUI 任务跑到一半时跳出来抢前台焦点。观测期这只是"报告不准"。
    """
    try:
        from core.runtime import oslease as _ol
        _ol.heartbeat_activity(getattr(orch, "_rt_os_lease", None))
    except Exception as e:
        logger.debug(f"[OSLease] 心跳失败（忽略）: {e}")


def _rt_lease_release(orch) -> None:
    try:
        from core.runtime import oslease as _ol
        _ol.release_activity(getattr(orch, "_rt_os_lease", None))
        orch._rt_os_lease = None
    except Exception as e:
        logger.debug(f"[OSLease] 归还租约失败（忽略）: {e}")


# ⚠️ `_rt_lease_compare_busy` 已于切写那一步删除（2026-08-08）。
# 观测期的对答案任务早已完成（量出泄漏频率 + 证实止血有效），而后续改动把租约改成
# **一整段 GUI 操作**的粒度后，旧 bool 仍是**单步**粒度 ——
# 📌 **两个寿命不同的东西之间的"分歧"不是缺陷，是设计**，继续对答案只会造假阳性。
# 📌 **shadow 的对答案，只在两边建模同一个粒度时才有意义。**
def _rt_machine_is_free() -> bool:
    """这台电脑闲着吗（谁在开都算忙，包括 Nano 自己）。

    ⚠️⚠️ **不要换成 `nano_may_touch_os()`** —— 那个在"Nano 自己持有"时返回 True，
    用它做后台自检的闸门，等于允许 canary 在 GUI 自动化跑到一半时去抢前台焦点，
    正是 `canary.py` 设计约束 ② 明令禁止的。两个函数回答的是不同的问题，
    对照表见 `oslease.machine_is_free` 的文档。

    ⚠️ **fail-safe 方向是"不闲"** —— 读不到状态时宁可跳过一次自检。
    """
    try:
        from core.runtime import oslease as _ol
        from core.runtime.kernel import get_kernel
        return _ol.machine_is_free(get_kernel())
    except Exception as e:
        logger.warning(f"[OSLease] 读机器占用状态失败，本次按「忙」跳过自检: {e}")
        return False


def _rt_wait_open(*, reason: str, wake_on: list, timer_seconds=None,
                  bg_ref=None, intent="condition_recheck") -> "object | None":
    """登记一条等待 —— **唯一写入口**，直接进内核。

    ⭐ 取代了切读期那套「写旧库 + 镜像到新库」。切写之后直接返回
       完整 `WaitRecord`，调用方不再通过六态折两态的兼容投影。
    """
    try:
        from core.runtime import waitcond as _wc
        return _wc.open_wait(reason=reason, wake_on=list(wake_on or []),
                              timer_seconds=timer_seconds, bg_ref=bg_ref,
                              intent=intent)
    except Exception as e:
        logger.error(f"[WaitC] 登记等待失败: {e}")
        return None


def _rt_shadow_note(path_tag: str, detail: str = "", diverged: bool = False) -> None:
    try:
        from core.runtime import toolbatch as _tb
        _tb.shadow_note_path(path_tag, detail, diverged)
    except Exception:
        pass


def _rt_has_open_batch(orch) -> tuple[bool, bool]:
    """从 ToolBatchSpan 派生"本轮有没有未完成的工具批次"。

    返回 `(有没有, 可不可信)`。这是 `_active_tool_batch_open` 从**旧字段权威**
    切成**内核权威**的那一步（2A 三步迁移的切读那一步）。

    ⚠️ **fail-safe 方向是 False** —— 理由见 `toolbatch.has_open_span()` 的 docstring：
    错成 True 会删掉合法历史工具对，错成 False 只是留个坏 pair（下轮 400 能修）。
    """
    try:
        from core.runtime import toolbatch as _tb
        return _tb.has_open_span(getattr(orch, "_rt_turn_id", "") or "")
    except Exception as e:
        logger.error(f"[Runtime] 派生 batch 状态失败（按 False 处理）: {e}")
        return False, False


def _rt_sweep_stale_spans(orch, legacy_flag_before_reset) -> None:
    try:
        from core.runtime import toolbatch as _tb
        # ⚠️ **不要 `bool(...)`** —— 那会把 `None` 压成 `False`。
        # 两者含义完全不同：`False` = "旧字段说没有未完成批次"（可对答案，
        # 而且此刻它应该是 True，所以是分歧）；`None` = "旧字段已删除，没得对"。
        # 之前这里写的是 `bool(...)`，于是切写那一步传 None 之后每一轮 sweep
        # 都被记成分歧 —— 一个只存在于观测层的假阳性。
        _tb.sweep_stale_spans(
            getattr(orch, "_rt_turn_id", "") or "rtturn_unknown",
            None if legacy_flag_before_reset is None else bool(legacy_flag_before_reset))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Interaction 接线
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ 这一组与上面 2A 那组**纪律相反**，不要照抄。
#
# 2A 是 shadow：旧字段权威，内核只在旁边记账，所以那几个包装器吞掉一切异常是对的。
# 早先是**直接切换**：内核就是权威，没有旧字段兜底。于是分成两类：
#
#     读路径（建动态段）   吞异常 → 最坏是模型这轮看不见待办，下一轮还能看见
#     写路径（开 / 回答）  **不许吞** → 吞掉就等于回到"答案被吞掉"那条老 bug
#
# 尤其 `_rt_answer_interaction`：它失败时必须让上层告诉用户"没记下来，请再说一次"，
# 而不是假装记下了然后继续跑 Explorer —— 后者正是这整个阶段要消灭的形状。
#
# ═══ 为什么澄清态这条不做 shadow（对 2A 三步迁移的有意偏离）═══
#
# 三步迁移（旧权威 → 内核权威 → 删旧）的前提是**两套实现能对答案**。
# 但这里迁的不是一个状态字段，是一条**路由决策**：
#     旧：关键词表判断"这句话算不算回答"（`_NEW_REQUEST_SIGNALS` + 长度>20 启发式）
#     新：模型判断（调不调 `answer_open_interaction`）
# 两者对同一句话会给出不同走向，且**只能有一条真的执行**——没法并行跑完再比。
# 硬做 shadow 只能比"如果走另一条会怎样"的推测，不是事实。
#
# 换来的安全边际是别的：① 这是冷路径（只有 Explorer 提问后才存在）；
# ② 它不参与任何热循环不变量；③ 旧实现本身就有 10 分钟超时会静默作废，
# 也就是说"这条路径偶尔失效"是它的既有行为，不是新引入的风险。


# ══════════════════════════════════════════════════════════════════════════
# 「这条执行链是谁的」—— 授权弹窗要靠它区分 main agent 和 Subagent
# ══════════════════════════════════════════════════════════════════════════
#
# ⭐ 用 `ContextVar` 而不是实例属性：Subagent跑在**自己的 asyncio.Task 里**，
#    `create_task` 在建任务那一刻复制上下文，之后两边各自独立 ——
#    于是「我是 Subagent」这件事天然只作用在它那一支上，main agent 照旧是空。
#    📌 与 `usage._turn_ctx` / `_agent_ctx` 同一个机制、同一个理由：
#       **归属跟着「谁发起的」走。** 实例属性做不到这件事（并发时互相串味），
#       而参数透传要求每一层都记得传 —— 那等于把它交给下一个人的记性。
_agent_scope_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "nano_agent_scope", default="")


def current_agent_label() -> str:
    """此刻这条执行链属于哪个 Subagent。**main agent 是空串。**

    ⚠️ 空串必须真的是空串：授权弹窗按它决定要不要画那行来源标识，
       而一个「看起来像 Subagent 其实是 main agent」的标识比不画更坏。
    """
    try:
        return _agent_scope_ctx.get("") or ""
    except Exception:
        return ""


def _rt_ongoing_work(orch) -> str:
    """给模型看的「你手上有哪些一件事在进行」。没有就空串。

    ⚠️ **空串必须真的是空串** —— 一个字符都不许加。
       没有在进行的事时，`task_boundary` 也不注入（见 `_rt_has_live_work`），
       这两处必须**同时**为空，否则会出现「有工具没事实」或「有事实没工具」
       两种半截状态，而模型在半截状态下会开始猜。
       📌 **一个工具和它的事实来源，必须由同一个条件控制。**
    """
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import task as _tk
        return _tk.conversation_tasks_for_model(get_kernel()) or ""
    except Exception:
        return ""


def _rt_background_jobs(orch) -> str:
    """还在跑的后台任务，给模型看。没有就空串。

    ⚠️ 与 `_rt_ongoing_work` 是**两件事，不许合并**：
       · 「一件事」（对话类 Task）—— 模型要**判断它结不结束**（有 `task_boundary`）
       · 「后台任务」—— 模型**不判断它的生死**，它只需要知道「别等」+
         「结束一件事不会停掉它们」
       📌 **一段注入的措辞取决于「要模型拿它做什么决定」** ——
          两段都叫「你手上有什么」，但一段要它表态、一段明确要它别管。
    """
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import task as _tk
        return _tk.background_jobs_for_model(get_kernel()) or ""
    except Exception:
        return ""


# ⭐⭐ **哪些工具会撞上 OS 能力闸** —— 注入里要点名它们。
#
# 🔴 实测 2026-08-20：「工作区写入」关着时，Subagent 如实报告了；而 **main agent 不信它**，
#    原话「Subagent 的报告有问题——它说「工作区写入」禁用了，但我用 `edit_file`
#    可以直接编辑文件」，然后自己又去读了一遍文件、又要了一次授权。
#    📌 **注入告诉了它「哪个开关关了」，没告诉它「这会让你的哪些工具用不了」** ——
#       它手里那个工具叫 `edit_file`，关掉的东西叫「工作区写入」，
#       两个名字之间没有任何东西把它们连起来，于是它判断Subagent搞错了。
#    ⚠️ 代价不只是多一轮：它**推翻了一份正确的报告**，而那会教它以后也别信。
#
# ⚠️ 这张表**必须与「真的会走 OS 层的 handler」一一对应**，
#    而那是可以从代码里数出来的（谁调用了 `_execute_dsl_step`）——
#    `tests/t_os_capability_gate.py` 用 AST 数一遍并比对，多一个少一个都会红。
#    📌 一张手写的表，只有在有东西替你数它的时候才不会过期。
_OS_BACKED_TOOLS: "dict[str, tuple]" = {
    # `os_execute` 覆盖全部 action —— 空元组表示"看这次调的是哪个 action"
    "os_execute": (),
    # `edit_file` 自己不写盘，它算完新内容后**穿过 `file_write` 那条路**
    "edit_file": ("file_write",),
    # OS 类 Skill 的执行计划也走同一层（`_run_os_skill_plan_loop`），
    # 但它不是一个模型可见的工具名 —— 用一句话代替
    "（OS 类 Skill 的步骤）": (),
}


def _rt_authorization_state(orch) -> str:
    """「这台电脑上，你要动手时会遇到什么」—— 给模型的**事实**，不是剧本。

    两件互相独立的事，必须分开说：
      ① **能力开放到哪一档**（设置 → OS 权限那 6 个开关）
      ② **要不要逐个授权**（Auto）

    ⭐⭐ 2026-08-20 定的语义，两者是**上下游**：
         能力未开放 → 连"要不要授权"这个问题都不该被问到（**auto 救不了**）
         能力开放了 → 才轮到 Auto 决定要不要逐个问
       📌 把它们混成一句话，模型就会用"反正开了 auto"去解释一次能力被关的失败。

    ⚠️ 只在**偏离默认**时说话：全开 + 非 auto（= 出厂状态）时返回空串。
       📌 与 压力段同一条纪律：**低于高水位一个字都不说** ——
          一段每轮都在的注入会被模型学会忽略，恰好毁掉"响亮"这件事本身。
    ⚠️ 给的是**状态**，不是「请你怎么表现」——
       📌 同故障卡片那条已验证的机制：给状态，不给剧本。
    """
    try:
        from core.os_layer import dsl as _dsl_a
        _perms = _dsl_a.load_permissions()
        _off = [k for k in _dsl_a.PERMISSION_LABELS if not _perms.get(k, False)]
        _auto = _dsl_a.auto_authorization_on()
    except Exception:
        return ""
    if not _off and not _auto:
        return ""
    _lines = ["[Authorization state - facts about this computer right now]"]
    if _off:
        _names = "、".join(f"「{_dsl_a.PERMISSION_LABELS[k]}」" for k in _off)
        _lines.append(
            f"- The user has turned OFF these capabilities: {_names} "
            f"({', '.join(_off)}). Actions that need them will not run at all - "
            f"this is not a permission prompt you can pass, and Auto does not "
            f"override it. Only the user can re-enable them, in 设置 -> OS 权限. "
            f"If you hit one, say so plainly and name the switch."
        )
        # ⭐⭐⭐ **点名受影响的工具。** 见 `_OS_BACKED_TOOLS` 上方那段留痕：
        #    只说开关名字，模型连不上"我手里这个工具会不会受影响"。
        _hit_tools, _hit_actions = set(), []
        for _a in _dsl_a.ALL_ACTION_NAMES:
            if not (_dsl_a.required_permissions(_a, _dsl_a.action_floor(_a))
                    & set(_off)):
                continue
            _hit_actions.append(_a)
            for _t, _acts in _OS_BACKED_TOOLS.items():
                if not _acts or _a in _acts:
                    _hit_tools.add(_t)
        if _hit_tools:
            _lines.append(
                f"  Concretely, this blocks: {', '.join(sorted(_hit_tools))}"
                f" (for the affected actions: {', '.join(_hit_actions[:8])}"
                f"{' …' if len(_hit_actions) > 8 else ''})."
                f" If a sub-agent reports one of these as blocked, it is telling "
                f"you the truth - do NOT retry it yourself with a different tool."
            )
    if _auto:
        _lines.append(
            "- Auto is ON: for capabilities that ARE enabled, you will not be "
            "asked to confirm each action - authorization is your own call. "
            "Do not tell the user you are 'waiting for their approval'; you are not. "
            "Judge each action as if you were the one accountable for it."
        )
    return "\n".join(_lines)


def _rt_has_live_work(orch) -> bool:
    """现在有没有一件「在进行的事」（对话类 Task）。

    ⚠️ **fail-safe 方向是「没有」** —— 读不出来就不注入那个工具。
       反过来错的代价是**模型去结束一件不存在的事，然后向用户宣布做完了**，
       而那是个假事实；而少注入一次只是这一轮它没法宣布结束（下一轮还有机会）。
       📌 fail-safe 要朝「少说一句」错，不朝「多断言一件事」错。
    """
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import task as _tk
        # ⭐⭐ **问的就是注入那一句话在不在**，不另写一份等价判断。
        #    📌 一个工具和它的事实来源，必须由同一个条件控制。
        #    🔴 旧写法是 `current_conversation_task() is not None` ——
        #       ④ 之后它在**全部被搁置**时会判 False，
        #       于是模型恰恰在唯一需要 `resume` 的时候拿不到它。
        return _tk.has_live_conversation_work(get_kernel())
    except Exception:
        return False


def _rt_ia_owner(mode: str) -> "str | None":
    """一条 Interaction 该归给谁 —— **由 `mode` 决定，不由调用点决定。**

    · `DEFERRED` —— 定义就是**跨轮待办**（审计连 deadline 都不设，：
      「用户可能真想把代码放几天再看」）。它有权让一件事诞生：
      **有个跨天的待办挂着，就说明有一件事没完。**
    · `INLINE` —— 本轮内问完就走，只贴标签。

    📌 **把「要不要创建」写成 mode 的函数，而不是在每个开交互的地方各写一次** ——
       否则第四处开交互的代码只会去抄最近的一处，抄到哪种全看运气。
       （同 `_UI_TERMINAL_EVENTS` 那条：把规则绑在一个能被检查的地方。）

    ⚠️ 全程吞异常返回 None —— 归属是标签，交互照旧要开得出来。
    """
    try:
        from core.runtime import interaction as _it2
        from core.runtime import task as _tk
        if mode == _it2.Mode.DEFERRED:
            return _tk.ensure_conversation_task("")
        return _tk.owner_label()
    except Exception as e:
        logger.debug(f"[Task] 交互的归属没落上（交互照旧登记）: {e}")
        return None


def _rt_live_interactions(orch) -> list:
    """当前所有未决交互。读路径，失败返回空表。"""
    try:
        from core.runtime.kernel import get_kernel
        from core.runtime import interaction as _it
        return _it.list_live(get_kernel())
    except Exception as e:
        logger.debug(f"[Runtime] 读取未决交互失败（本轮按“无待办”处理）: {e}")
        return []


def _rt_open_clarification(orch, original_requirement: str, last_creation_note: str,
                           prompt_text: str) -> str:
    """创建流程自报未决问题 → 开一个 DEFERRED/PERSISTED 交互。

    ⚠️⚠️ **2026-08-13 迁移（探索子循环已拆）**，三处改动：
      1. 参数 `last_explorer_message` → **`last_creation_note`**。
         内容语义不变（仍是一个混合包），但**生产者已经不是 Explorer 了** ——
         现在是主模型填的 `handoff_summary`。名字留着 explorer 就是给后人留假事实。
      2. **删掉 `include_os`**。它是一条死链：整条调用路径没有任何地方能把它传成 True
         （cutover 时已从 `_build_skill_exploration_tools` 删过一处，
         但签名、这里的参数、checkpoint 写入、两段 prompt 片段都还留着）。
      3. **不再写 `explorer_prompt_version`**。Explorer 都没了，再写这个字段
         就是给未来的人留一个指向不存在物的版本号。
    ⭐ **老记录仍然读得到**（读取方用 `.get()`），只是**新写入不再生产这些字段**。
    📌 **一个字段的名字必须描述它现在是什么，不是它当初由谁产生。**

    ⚠️ 仍然**不要**再拆出 `question` / `confirmed_context` 两个字段：
    名字里不用 `confirmed`，因为这段话里混着"已核对的""当前理解"
    "尚未解决的冲突""向用户提的问题"，叫 confirmed 会诱导后来的人
    把整段当成已验证事实（同：别拿命名冒充语义保证）。

    开失败不抛：问题本身已经流式显示给用户了，对话历史也在。
    最坏结果是这一问退化成普通对话（等于旧实现超时后的行为），不是数据损坏。

    ═══ ⭐ 澄清必须有寿命，而且槽满时要驱逐最旧的（2026-08-05 实测）═══

    定了"deferred ≤ 5"，理由是"上限的意义是逼迫收敛"。
    但**没有任何东西会关掉它们**：RESOLVE 只在 continuation 成功时、
    CANCEL 只在模型主动调、SUPERSEDE 没接线、而澄清**从不设 deadline**
    → `expire_tick` 永远碰不到它。于是僵尸单调累积，
    **上限变成硬停而不是驱动力**：实测 8 小时后槽里躺着 5 条，
    最老的是"dasdjasddd 是什么"，之后每一次澄清登记都被不变量拒绝、
    静默退化成普通对话 —— 用户看不出来，整套机制已经死了。

    两条修法：
    1. **给澄清设 `deadline_at`**（审计仍然不设）。之前写过
       "没设 deadline 的永不过期（积压不是错误）" —— 那句对 **Skill 审计**成立
       （用户可能真想放三天再看代码），对**澄清**不成立：
       一个 8 小时前的"你说的 XX 是什么"早就失效了。**两种 kind 的过期语义不同。**
    2. **槽满时驱逐最旧的，而不是拒绝最新的。** 用户当下问的那个才是真正要的；
       8 小时前那条没人会回来答。驱逐记成 `EXPIRED`，不是静默删。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        _k = get_kernel()

        # ── 槽满 → 驱逐最旧的 ───────────────────────────────────────────
        # 只驱逐澄清类：Skill 审计是用户真要看的代码，不能被一句提问挤掉。
        try:
            _live = [r for r in _it.list_live(_k)
                     if r.slot == _it.Slot.DEFERRED
                     and r.kind == _it.Kind.SKILL_CLARIFICATION]
            while len(_live) >= _it.MAX_DEFERRED:
                _oldest = min(_live, key=lambda r: r.created_at)
                _k.submit(Command(kind=_it.EXPIRE, subject_id=_oldest.interaction_id,
                                  payload={"interaction_id": _oldest.interaction_id}))
                logger.info(
                    f"[Interaction] deferred 槽已满，驱逐最旧的澄清 "
                    f"{_oldest.interaction_id}（开了 "
                    f"{(_k.now() - _oldest.created_at) / 60:.0f} 分钟没人回答）"
                )
                _live = [r for r in _live if r.interaction_id != _oldest.interaction_id]
        except Exception as _e:
            logger.warning(f"[Interaction] 驱逐旧澄清失败（继续尝试登记）: {_e}")

        r = _k.submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.SKILL_CLARIFICATION,
            "mode": _it.Mode.DEFERRED,
            # 归属：DEFERRED 跨轮 → 有权让一件事诞生（见 `_rt_ia_owner`）
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": (prompt_text or "").strip()[:500],
            # 澄清有寿命：过了就是陈旧提问，留着只会挤占槽位并干扰模型。
            # 审计不设 deadline（用户可能真想放几天再审代码）。
            "deadline_at": _k.now() + _CLARIFICATION_TTL_SECONDS,
            "payload": {
                "original_requirement": original_requirement,
                # ⚠️ 见本函数 docstring：只留两个字段，不再写 Explorer 专属的那两个。
                "last_creation_note": last_creation_note,
            },
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] 澄清问题已登记 {iid}（slot={r.data.get('slot')}）")
        return iid
    except Exception as e:
        # ⚠️ 吞异常但**不能静默**：槽位满是运维故障，不是可以悄悄降级的事。
        # 实测就是这么死的 —— 连着五次登记失败，日志里有 ERROR，但用户和模型都不知道。
        logger.error(f"[Interaction] 登记澄清问题失败，本次提问退化成普通对话: {e}")
        try:
            from core.health import get_system_events
            get_system_events().add(
                "A clarification question could not be registered, so it will not persist. "
                "If the user answers it in a later turn, you may have no record of what was asked."
            )
        except Exception:
            pass
        return ""


# 需求文本里的"样板"——每个建 Skill 的请求都会带，对"是不是同一件事"零信息量。
# 不剥掉它们，两个完全无关的需求也会因为共享这些词而拿到虚高的重叠度
# （实测第一版就是这么判不出来的：真正例只有 24%）。
_SKILL_REQ_BOILERPLATE = (
    "写一个skill", "写个skill", "创建一个skill", "创建skill", "新建skill",
    "帮我写一个", "帮我写", "帮我创建", "做一个skill", "做个skill",
    "写一个", "写个", "作用是", "用途是", "功能是", "用于",
    "的skill", "一个skill", "skill", "创建", "新建",
)

# 重叠度阈值。实测真正例 67~86%、假正例 0~25%，取 0.5 两边各留 17 个点余量。
_COVERAGE_THRESHOLD = 0.5


def _strip_skill_boilerplate(s: str | None) -> str:
    """剥掉样板词与标点，只留需求的实质部分，供重叠度比对。"""
    out = (s or "").lower()
    for b in _SKILL_REQ_BOILERPLATE:
        out = out.replace(b, "")
    return re.sub(r"[，。、,.:：「」\"'\s（）()\[\]【】]+", "", out)


def _rt_restore_pending_skills(orch) -> None:
    """启动时把待审草稿从库里捞回内存；捞不回来的交互当场收掉。

    ═══ 为什么需要它（2026-08-06 实测）═══

    用户重启后看到三张待审卡还在，但**没有 `<>` 按钮** —— 代码看不了、
    部署和丢弃也点不了，卡片焊死在界面上。

    交互是 PERSISTED（存库），`_pending_skills` 却是纯内存。重启后卡片读回来了、
    草稿没了 → `_get_pending_skill()` 返回 None → `<>` 不渲染 → **这条交互
    再也没有任何出口**（模型侧同理：它能在 [Open Interactions] 里看到这条，
    以为代码还在，实际上批准也没东西可部署）。

    两半都要做：
      ① 有 `draft` 的 → 放回 `_pending_skills`，恢复成真正可操作的待审；
      ② 没有 `draft` 的 → **当场 CANCEL 掉**。它们是本次修复之前留下的孤儿，
         代码已经永久丢失，留着只会继续骗人。

    ⚠️ ② 用 `INTERRUPTED_BY_RESTART` 而不是 `USER_CANCELLED` —— 用户没取消
       任何东西，是进程重启把草稿弄丢了。历史要读得出真相。
    """
    from core.runtime import interaction as _it
    try:
        recs = _rt_live_interactions(orch)
    except Exception as e:
        logger.warning(f"[Runtime] 待审草稿恢复：读交互失败，跳过（不影响启动）: {e}")
        return

    restored, orphaned = 0, 0
    for r in recs:
        if r.kind != _it.Kind.SKILL_AUDIT:
            continue
        draft = (r.payload or {}).get("draft") if isinstance(r.payload, dict) else None
        if isinstance(draft, dict) and draft.get("filename") and draft.get("code"):
            orch._put_pending_skill(draft)
            restored += 1
            continue
        orphaned += 1
        _rt_close_interaction(r.interaction_id, _it.CANCEL,
                              resolution=_it.Resolution.INTERRUPTED_BY_RESTART)
        logger.warning(
            f"[Runtime] 待审 {r.interaction_id}（{r.artifact_id or '?'}）"
            f"草稿已随上次进程丢失 → 收掉。"
            f"（本次修复之前登记的审计都没存草稿，属于历史遗留）"
        )
    if restored or orphaned:
        logger.info(f"[Runtime] 待审草稿恢复：{restored} 份复原、{orphaned} 条孤儿已收")


def _rt_open_skill_audit(orch, filename: str, description: str, code: str,
                         mode: str, valid: bool, errors: list) -> str:
    """Skill 审计弹窗 → 一条 DEFERRED/PERSISTED Interaction。

    ═══ 为什么审计要进 Interaction ═══

    改造前 `_pending_skill` 是个纯内存 dict + 一段路由劫持 + 一个专用 LLM 分类器
    （`classify_pending_skill_intent`，8 个标签，每条消息多一次 API 往返）。三个问题：

      ① **进程一关就没**。审计弹窗开着时崩溃/关窗，那份待审代码不留任何痕迹，
         重启后没人知道它存在过（`_pending_skill` 在内存里）。
      ② **模型不知道有这件事**。劫持是代码层的，模型看不见"有个 Skill 等着审"，
         于是用户说别的时它也无从判断。
      ③ **专用分类器违反设计原则 3**（少硬路由、判断交给有完整上下文的主决策模型）。
         8 个标签里有一半（EXPLAIN / RISK / COMPLAINT / EXECUTE_BLOCKED）
         本质就是"正常回答用户"，主模型天然会做，不需要先分类。

    ⭐ **artifact 绑定在这里第一次真正用上**：
    审批必须钉在**当时那一版**上。`artifact_id=filename` + `artifact_hash=代码指纹`，
    落地前 `verify_artifact()` 复核。否则用户点"同意"时批准的可能是一个已经被改过的版本。

    ⚠️ 开失败不抛：弹窗已经显示了，用户照样能点按钮（`_pending_skill` 仍在）。
    最坏结果是这次审批没有持久记录 —— 退化成改造前的行为，不是数据损坏。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        _q = (f"待审计的 Skill「{filename}」"
              f"{'（校验未通过：' + '；'.join(errors[:2]) + '）' if not valid else '（校验通过）'}"
              f"：{(description or '').strip()[:120]}")
        r = get_kernel().submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.SKILL_AUDIT,
            "mode": _it.Mode.DEFERRED,
            # 归属：DEFERRED 跨轮 → 有权让一件事诞生（见 `_rt_ia_owner`）
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": _q,
            # ⚠️ 审计**刻意不设 deadline**：用户可能真想把代码放几天再看。
            # 澄清才有 2 小时 TTL —— 两种 kind 的过期语义不同。
            "artifact_kind": "skill",
            "artifact_id": filename,
            "artifact_hash": _it.artifact_hash(code),
            "payload": {
                "filename": filename, "mode": mode,
                "valid": bool(valid), "errors": list(errors or []),
                "code_lines": len((code or "").splitlines()),
                # ⭐⭐ [2026-08-06 实测] 草稿本身必须跟着交互一起落盘。
                #
                # 实测：重启后三张待审卡还在，但**没有 `<>` 按钮**，
                # 既看不了代码也部署/丢弃不了 —— 卡片焊死在界面上。
                #
                # 原因：交互是 PERSISTED（存库），`_pending_skills` 却是**纯内存**。
                # 重启后卡片从库里读回来了，草稿没了 → `_get_pending_skill()` 返回 None
                # → `<>` 不渲染 → 这条交互再也没有任何出口。
                #
                # ⚠️ 这不是"少存了一个字段"，是**耐久性承诺自相矛盾**：
                #    审计**刻意不设 deadline**，理由是"用户可能真想放三天再看代码"
                #。这个理由只有在**代码也活到三天后**时才成立。
                #
                # 📌 判据：**一条持久记录不能指向一个易失的东西。**
                #    要么两个一起持久，要么这条记录本身就不该是持久的。
                #
                # 存整份载荷而不是只存 code：部署要用 spec_side_effects / lifecycle /
                # error_context 等一整套，缺一个就是另一种形式的半残
                #（尤其 spec_side_effects 的 None 与 [] 语义不同，见 `_put_pending_skill`
                #  调用处那段注释——JSON 能如实保留 null，不会退化成 []）。
                "draft": orch._get_pending_skill(filename),
            },
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] Skill 审计已登记 {iid}（{filename}，slot={r.data.get('slot')}）")

        # ⭐⭐ [2026-08-06 实测] 同名的旧待审草稿要被新的取代，不能并存。
        #
        # 实测：三张待审卡同时挂着，**全都叫 `GetComputerIP`**
        #（int_9eeac9e476 / int_e42c7c7f04 / int_f2633f59d5）。
        # 里则是反过来的表现：两张同名 `GetDNSConfig`，部署其中一张
        # **两张一起消失**（18:08:15 同毫秒关掉两条）——用户当时以为是 UI bug。
        #
        # 两个现象是同一个原因：**文件名就是这个产物的身份**。
        # `_pending_skills` 按文件名存，同名后写的直接覆盖先写的；
        # `apply_pending_skill(filename)` 也只认文件名。于是"多张同名卡"
        # 从一开始就是假象 —— 底下**只有一份 payload**，卡片却有三张，
        # 点哪张都是同一份，关一张就该关全部。
        #
        # 📌 判据：**同一个身份不允许有多个未决条目。**
        #    两份同名草稿不可能都部署（只有一个文件位），所以更晚的那份
        #    就是对更早那份的**取代**，而不是"又一个待办"。
        #
        # ⚠️ 用 SUPERSEDE 而不是 CANCEL：用户没有取消任何东西，
        #    是 Nano 自己重写了一版。语义要对得上，否则历史读起来是错的。
        try:
            for _old in _rt_live_interactions(orch):
                if (_old.kind == _it.Kind.SKILL_AUDIT
                        and _old.artifact_id == filename
                        and _old.interaction_id != iid):
                    # ARTIFACT_CHANGED 就是这里的实情：同一个产物被重写了一版。
                    _rt_close_interaction(_old.interaction_id, _it.SUPERSEDE,
                                          resolution=_it.Resolution.ARTIFACT_CHANGED)
                    logger.info(
                        f"[Interaction] 同名旧草稿 {_old.interaction_id}（{filename}）"
                        f"→ SUPERSEDED，被 {iid} 取代（一个文件名只能有一份待审）"
                    )
        except Exception as e:
            # 收不掉不致命：最坏是回到"多张同名卡"那个旧行为，不是数据损坏。
            logger.warning(f"[Interaction] 收拢同名旧草稿失败（不影响本次登记）: {e}")

        # ⭐⭐ [BUG3 · 2026-08-06 实测] 草稿一出现 → 未决澄清的目的已经达成，收掉它。
        #
        # 实测：进澄清 → 回答 → Nano 开始写 Skill → **那张澄清卡片一直挂着**，
        # 直到 Skill 被部署（甚至更久）。
        #
        # 原因是澄清只有两条关闭通路：`answer_open_interaction` 的 RESOLVE，
        # 或者 `_rt_supersede_covered_clarifications` 那个**文本重叠度启发式**。
        # 而模型这次没调工具、直接开了新的创建流程，且用户的回答
        #（"新建一个，SlugName 那个不一样"）跟原需求几乎没有字面重叠 → 启发式没命中。
        #
        # ⭐ 修法不是调那个阈值，是换一个**非启发式的信号**：
        # **审计草稿的存在本身就证明澄清已经被消化了** —— 探索阶段能写出代码，
        # 说明它拿到了它要问的东西。这条判断不依赖任何文本匹配，也不会误伤：
        # 一个还需要用户回答的问题，不可能同时产出一份完整的待审代码。
        #
        # 📌 判据：**优先找"状态本身能证明的事实"，而不是去猜文本像不像。**
        # 启发式该是兜底，不该是唯一通路。
        try:
            _k2 = get_kernel()
            for _c in _it.list_live(_k2):
                if _c.kind != _it.Kind.SKILL_CLARIFICATION:
                    continue
                _k2.submit(Command(kind=_it.SUPERSEDE, subject_id=_c.interaction_id,
                                   payload={"interaction_id": _c.interaction_id,
                                            "superseded_by": iid,
                                            "resolution": _it.Resolution.DONE}))
                logger.info(
                    f"[Interaction] 澄清 {_c.interaction_id} 已被草稿 {filename} 消化"
                    f" → SUPERSEDED（草稿存在即证明问题已解决）"
                )
        except Exception as _e:
            logger.warning(f"[Interaction] 收尾澄清失败（不影响审计）: {_e}")
        return iid
    except Exception as e:
        logger.error(f"[Interaction] 登记 Skill 审计失败，退化为纯内存 pending: {e}")
        return ""


def _rt_open_skill_manage(orch, op: str, skill: str, prompt_text: str,
                          extra: dict | None = None) -> str:
    """Skill 管理/修改确认 → 一条 DEFERRED/PERSISTED Interaction。

    取代 `_pending_action`（四个 op：delete / disable / enable / update_skill）
    加一段路由劫持加一个专用分类器（`classify_pending_action_intent`，6 个标签）。

    ⚠️ **这不是 OS 风险确认** —— 2026-08-06 考古纠正，见 `Kind.SKILL_MANAGE` 的注释。
    真正的 OS 确认是 `execution_confirm` 事件 + 300 秒同步等待，属于清单 ⑧。

    与审计（②b）共享同一套处置语义，所以复用 `answer_open_interaction` 的三个 relation：
        ANSWER               → 确认执行
        ANSWER_AND_AMENDMENT → 改一改再执行（只有 update_skill 用得上）
        CANCEL               → 不做了
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        r = get_kernel().submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.SKILL_MANAGE,
            "mode": _it.Mode.DEFERRED,
            # 归属：DEFERRED 跨轮 → 有权让一件事诞生（见 `_rt_ia_owner`）
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": (prompt_text or "").strip()[:500],
            "artifact_kind": "skill",
            "artifact_id": skill,
            "payload": {"op": op, "skill": skill, **(extra or {})},
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] Skill 管理确认已登记 {iid}（{op} {skill}）")
        return iid
    except Exception as e:
        logger.error(f"[Interaction] 登记 Skill 管理确认失败，退化为纯内存 pending: {e}")
        return ""


def _rt_open_mcp_manage(orch, op: str, server: str, prompt_text: str) -> str:
    """MCP 管理确认 → 一条 DEFERRED/PERSISTED Interaction。

    形状照抄 `_rt_open_skill_manage`，只有三处不同：
        kind           MCP_MANAGE（理由见那个常量的注释）
        artifact_kind  "mcp"
        payload        {"op", "server"} —— 不是 {"op", "skill"}

    ⚠️ **必须登记 Interaction，不能只挂 `_pending_action`** ——
       后者的消费者 `_handle_pending_action` 在 （2026-08-06）就删了，
       现在全仓只有赋值和清空、**没有任何地方读它来执行**。
       📌 只挂它的话，用户回复「确认」之后什么都不会发生 ——
          而那是最坏的一种失败：**用户以为自己批准了，系统以为没这回事。**

    ⭐ 它不会在 UI 上画卡片：`app._CARD_KINDS` 是**白名单**，只有 SKILL_AUDIT 进。
       ⇒ MCP 管理天然走「纯净的自然语言」那条路 —— 正是设计要的。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        r = get_kernel().submit(Command(kind=_it.OPEN, payload={
            "kind": _it.Kind.MCP_MANAGE,
            "mode": _it.Mode.DEFERRED,
            "owner_task_id": _rt_ia_owner(_it.Mode.DEFERRED),
            "durability": _it.Durability.PERSISTED,
            "owner_turn_id": getattr(orch, "_rt_turn_id", "") or None,
            "prompt_text": (prompt_text or "").strip()[:500],
            "artifact_kind": "mcp",
            "artifact_id": server,
            "payload": {"op": op, "server": server},
        }))
        iid = r.data.get("interaction_id", "")
        logger.info(f"[Interaction] MCP 管理确认已登记 {iid}（{op} {server}）")
        return iid
    except Exception as e:
        logger.error(f"[Interaction] 登记 MCP 管理确认失败: {e}")
        return ""


def _rt_close_mcp_manage(server: str, approved: bool, reason: str = "") -> None:
    """MCP 管理确认收尾。按 `artifact_id` 找 —— 同 `_rt_close_skill_manage`。"""
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        for rec in get_kernel().interactions.open_records():
            if rec.kind != _it.Kind.MCP_MANAGE or rec.artifact_id != server:
                continue
            get_kernel().submit(Command(kind=_it.CLOSE, payload={
                "interaction_id": rec.interaction_id,
                "transition": _it.ANSWER if approved else _it.CANCEL,
                "resolution": (_it.Resolution.CONFIRMED if approved
                               else _it.Resolution.CANCELLED),
                "reason": reason or "",
            }))
            return
    except Exception as e:
        logger.warning(f"[Interaction] 关闭 MCP 管理确认失败: {e}")


def _rt_close_skill_manage(skill: str, approved: bool, reason: str = "") -> None:
    """管理确认处置完收尾。按 `artifact_id` 找 —— 同 `_rt_close_skill_audit` 的理由。"""
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        k = get_kernel()
        for rec in _it.list_live(k):
            if rec.kind != _it.Kind.SKILL_MANAGE or rec.artifact_id != skill:
                continue
            k.submit(Command(kind=_it.DECIDE, subject_id=rec.interaction_id,
                             payload={"interaction_id": rec.interaction_id,
                                      "approved": bool(approved),
                                      "resolution": reason or _it.Resolution.DONE}))
            logger.info(
                f"[Interaction] Skill 管理确认 {rec.interaction_id} 已"
                f"{'执行' if approved else '取消'}（{skill}）"
            )
    except Exception as e:
        logger.warning(f"[Interaction] 关闭管理确认交互失败（不影响执行结果）: {e}")


def _rt_close_skill_audit(filename: str, approved: bool, reason: str = "") -> None:
    """UI 按钮或模型工具处置完审计后收尾。按 `artifact_id` 找那条交互。

    ⚠️ 必须能被 **UI 按钮**调用（app.py 的"验证并应用"/"丢弃"直接调 orchestrator 的
    `apply_pending_skill` / `cancel_pending_skill`），否则用户点按钮之后
    交互会一直挂在那里 —— 那就是我们刚修掉的僵尸形状。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        k = get_kernel()
        for rec in _it.list_live(k):
            if rec.kind != _it.Kind.SKILL_AUDIT or rec.artifact_id != filename:
                continue
            k.submit(Command(kind=_it.DECIDE, subject_id=rec.interaction_id,
                             payload={"interaction_id": rec.interaction_id,
                                      "approved": bool(approved),
                                      "resolution": reason or _it.Resolution.DONE}))
            logger.info(
                f"[Interaction] Skill 审计 {rec.interaction_id} 已"
                f"{'批准' if approved else '拒绝'}（{filename}）"
            )
    except Exception as e:
        logger.warning(f"[Interaction] 关闭 Skill 审计交互失败（不影响部署结果）: {e}")


def _rt_supersede_covered_clarifications(orch, new_requirement: str) -> int:
    """开一条新的 Skill 创建流程时，把**已被它涵盖**的未决澄清标成 SUPERSEDED。

    ═══ 为什么要有这个（实测）═══

    Nano 问"你要哪些网络配置？"，用户答"选 7，全部上述内容"，模型**没有**调
    `answer_open_interaction`，而是开了一条新的 `create_new_skill`（需求文本里
    已经包含那个回答）。需求办成了，但那条澄清留在槽里变僵尸。

    ⚠️ **这是个启发式**（按需求文本重叠度判断"是不是同一件事"），而当天已经在
    "靠猜措辞"上翻车两次。这次可以用，理由是**失败代价对称且轻微**：
        误判 → 澄清关早了，用户重问一次
        漏判 → 留个僵尸，2 小时 TTL 收掉
    低风险场景才适合启发式。**不要把这个理由推广到高风险判断上。**

    ═══ 阈值是**量出来的**，不是拍的 ═══

    第一版按原文直接切 3-gram，实测那个例子只得 24%（判不出来）—— 因为原始需求里
    有一堆样板（"写个skill，作用是"）而模型改写后的需求不含它们。
    剥掉样板 + 改 2-gram 后，用 8 组真/假样本量了一遍：

        真正例（同一需求）    67% / 67% / 86%
        假正例（不同需求）    0% / 0% / 0% / 25%

    阈值定 **0.5**，两边各留 17 个百分点余量。

    📌 **纪律**：启发式的阈值必须先量出真假两侧的分布再定。
    直接拍一个数等于把"能不能用"交给运气 —— 第一版的 3-gram 就是这么错的
    （真 30% vs 假 29%，几乎零余量，而当时没量所以不知道）。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
    except Exception:
        return 0
    _new = _strip_skill_boilerplate(new_requirement)
    if len(_new) < 4:
        return 0
    n = 0
    try:
        k = get_kernel()
        for rec in _it.list_live(k):
            if rec.kind != _it.Kind.SKILL_CLARIFICATION:
                continue
            _orig = _strip_skill_boilerplate((rec.payload or {}).get("original_requirement"))
            if len(_orig) < 2:
                continue
            # 2-gram：中文没有空格，按词切不可靠。2 字片段对"查看当前网络配置"
            # 这类短需求区分度最好（3-gram 实测真假两侧几乎重叠，见 docstring）。
            _grams = {_orig[i:i + 2] for i in range(len(_orig) - 1)}
            if not _grams:
                continue
            _hit = sum(1 for g in _grams if g in _new) / len(_grams)
            if _hit < _COVERAGE_THRESHOLD:
                continue
            k.submit(Command(kind=_it.SUPERSEDE, subject_id=rec.interaction_id,
                             payload={"interaction_id": rec.interaction_id,
                                      "resolution": _it.Resolution.FOLLOW_UP_QUESTION}))
            n += 1
            logger.info(
                f"[Interaction] 澄清 {rec.interaction_id} 已被新的创建流程涵盖"
                f"（需求重叠 {_hit:.0%}）→ SUPERSEDED，不再留作僵尸"
            )
    except Exception as e:
        logger.warning(f"[Interaction] 涵盖判定失败（不影响创建流程）: {e}")
    return n


def _rt_answer_interaction(interaction_id: str, answer_verbatim: str, relation: str) -> dict:
    """把用户原话原子落盘。**故意不吞异常** —— 见本节开头。

    返回 kernel 的 result data（含 `retry`：True = 这是失败后的幂等重试）。
    """
    from core.runtime.kernel import get_kernel, Command
    from core.runtime import interaction as _it
    r = get_kernel().submit(Command(
        kind=_it.ANSWER, subject_id=interaction_id,
        payload={"answer_verbatim": answer_verbatim, "relation": relation},
    ))
    return dict(r.data or {})


def _rt_close_interaction(interaction_id: str, kind: str, **payload) -> None:
    """收尾（RESOLVE / CANCEL / SUPERSEDE）。吞异常：

    到这一步用户的答案早已落盘、continuation 也已经跑完，收尾失败最坏是
    一条交互多停留一会儿（下次 Explorer 提问会 SUPERSEDE 它，或用户手动取消）。
    为了这个把已经成功的整轮变成报错，不划算。
    """
    try:
        from core.runtime.kernel import get_kernel, Command
        get_kernel().submit(Command(kind=kind, subject_id=interaction_id,
                                    payload={"interaction_id": interaction_id, **payload}))
    except Exception as e:
        logger.warning(f"[Interaction] 收尾 {kind} 失败（不影响本轮结果）: {e}")


def _rt_cancel_all_interactions(reason: str = "") -> int:
    """重置对话时清场。取代原来的 `self._pending_skill_clarification = None`。"""
    n = 0
    try:
        from core.runtime.kernel import get_kernel, Command
        from core.runtime import interaction as _it
        k = get_kernel()
        for rec in _it.list_live(k):
            try:
                k.submit(Command(kind=_it.CANCEL, subject_id=rec.interaction_id,
                                 payload={"interaction_id": rec.interaction_id,
                                          "resolution": _it.Resolution.USER_CANCELLED}))
                n += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[Interaction] 重置时清理未决交互失败: {e}")
    if n:
        logger.info(f"[Interaction] 重置对话，作废 {n} 个未决交互（{reason}）")
    return n



# 澄清提问的寿命。两小时之后那句"你说的 XX 是什么"基本已经失效，
# 留着只会挤占 deferred 槽位（上限 5）并在动态段里干扰模型。
# ⚠️ 只对**澄清**设，Skill 审计刻意不设 —— 用户可能真想放几天再审代码。
# 实测：8 小时后槽里躺着 5 条陈旧澄清，之后每次登记都被不变量拒绝。
_CLARIFICATION_TTL_SECONDS = 2 * 60 * 60


    # 🪦 `_coerce_confidence` 已删除（2026-08-29）—— **零调用方**，AST 全仓核实。
    #    📌 一个写好但没人调的东西，比没写更坏：没写时缺口是可见的，
    #       写了不接时缺口看起来已经补上了。


def _extract_code_delta(json_acc: str, prev_len: int) -> tuple[str, int]:
    """从 WriteSkill 工具调用的累积 partial_json 里增量提取 code 字段明文。

    Anthropic streaming 工具参数以 JSON 片段方式到达，这里每次接受完整累积串，
    找到 "code": " 后面的 JSON 字符串值，解码转义序列，返回新增的字符 + 新总长度。
    """
    import re as _re
    m = _re.search(r'"code"\s*:\s*"', json_acc)
    if not m:
        return "", prev_len
    rest = json_acc[m.end():]
    decoded = []
    i = 0
    esc = False
    while i < len(rest):
        ch = rest[i]
        if esc:
            esc = False
            if ch == 'n':   decoded.append('\n')
            elif ch == 't': decoded.append('\t')
            elif ch == 'r': decoded.append('\r')
            elif ch == '"': decoded.append('"')
            elif ch == '\\': decoded.append('\\')
            elif ch == '/': decoded.append('/')
            elif ch == 'u':
                hex4 = rest[i+1:i+5]
                if len(hex4) == 4:
                    try: decoded.append(chr(int(hex4, 16)))
                    except ValueError: pass
                    i += 4
            # 其他转义忽略
        elif ch == '\\':
            esc = True
        elif ch == '"':
            break  # code 字段结束
        else:
            decoded.append(ch)
        i += 1
    full = "".join(decoded)
    new_part = full[prev_len:]
    return new_part, len(full)


# ── Ambient Memory：应用分类 + 标题解析（模块级，注入与轨迹持久化共用）──────────
# app→类别表统一在 core/proactive/app_catalog（scene 分类也读同一张表）。
from core.proactive import app_catalog as _app_catalog          # noqa: F401
from core.proactive import referent as _referent


def _ambient_cat(app: str) -> str:
    """→ `referent.cat`。**搬家留的转发**，判据只有那一处。"""
    return _referent.cat(app)


def _ambient_parse_title(app: str, title: str):
    """→ `referent.parse_title`。**搬家留的转发**。

    ⚠️ 它被搬进 `core/proactive/referent.py` 是因为 的句柄解析要用它，
       而 `proactive → orchestrator` 会成环。完整推导（含 `office` 为什么
       必须和 `editor` 走同一条）在那边的函数 docstring 里。
    📌 **删/搬一段代码时，长在它身上的「为什么」要跟着搬到新家** ——
       注释掉队比代码掉队更难发现。
    """
    return _referent.parse_title(app, title)


# ── 按需加载工具（deferred tools，对标 Claude Code 的 ToolSearch）──────────────
# 主决策只常驻【核心工具】+ load_tools；其余能力（操作电脑/网页/技能/可视化…）只在
# 提示词里留"感知"（名字+一句话），完整 schema 默认不注入，模型调 load_tools 才加载。
# 这样一句"你好"不再背 40K 的工具 schema。安全流全在执行时，与此无关。
_LOAD_TOOLS_MANIFEST = {
    "name": "load_tools",
    "description": (
        "Load tools that Nano has but that are not currently in the active tool list.\n"
        "Many capabilities are deferred to reduce token cost: OS control, browser tools, Skills, visuals, task lists, wait/suspension, and more.\n"
        "If a needed capability is listed in the awareness block but its schema is not active, call this tool first; the loaded tool can be used on the next step.\n"
        "Use query to describe the needed capability, or names to request exact tool names.\n"
        "Never say Nano cannot do something only because the tool is not currently loaded."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language description of the needed capability."},
            "names": {"type": "array", "items": {"type": "string"},
                      "description": "Optional exact tool names to load."},
        },
    },
}

# 主决策常驻的核心工具（小、通用、几乎每轮可能用）。其余全部延迟，靠 load_tools 拉。
# Token 压缩：默认只保留「检索/列文件/记忆/写记忆 + load_tools」。
# load_full_file / get_file_path / OS / Skill 管理 / 可视化 / wait_for 等大 schema 全部按需加载，
# 避免一句普通聊天也背 1.6W+ 字符的工具定义。


class Orchestrator:
    def __init__(self, provider, registry, memory, instruction_path="config/system_instruction.txt"):
        self.provider = provider
        self.registry = registry
        self.memory   = memory
        self.instruction_path = instruction_path
        self._system_guide_template = self._load_system_instruction()
        # ⭐ 待审 Skill 载荷：**多槽**，按 filename 索引（2026-08-06）
        #
        # 改造前这里是单个 dict，而 Interaction 那一层按支持 6 条待办。
        # 两层容量不匹配的后果（实测撞出来的）：
        #   CountWords 审计打开 → _pending_skill = {CountWords…}   Interaction A: OPEN
        #   UpperCase  审计打开 → _pending_skill = {UpperCase…}    Interaction B: OPEN
        #                          ↑ 第一份代码正文被【无声覆盖】，而 A 仍自称 OPEN
        # 于是用户说"部署吧"、模型对 A 调工具时，载荷已经不是 A 的了。
        #
        # 现在按 filename 存多份。`_pending_skill`（单数）退化成"最近那条"的
        # **只读兼容视图**（见下面的 property）—— 十几处只关心"有没有待审"的调用点
        # 因此一行都不用改，而需要指定具体哪一条的地方显式传 filename。
        self._pending_skills: dict[str, dict] = {}
        self._pending_skill_order: list[str] = []   # 插入顺序，决定"最近那条"是谁
        self._pending_action: dict | None = None

        # [2026-08-05] `_pending_skill_clarification` 已删除。
        # 澄清态现在是 `core/runtime/interaction.py` 里的一条 Interaction：
        # 跨重启存活、永不静默超时、由模型经 answer_open_interaction 消费。
        # 不要为了"方便"再加回一个内存字段——那会立刻变成双权威。

        # 上一次 OS 视觉定位失败的记录，供 _detect_os_locate_followup 用
        self._last_os_locate_failure: dict | None = None
        # 是否有 OS 任务正在执行（canary 自检只在这个是 False 时才跑）
        # ⚠️ `_os_task_busy` 已于切写那一步删除（2026-08-08）。
        # 权威是 `oslease` 的活动租约 —— 它有主人、有期限、过期自动不算数，
        # 而那个裸 bool 漏写一次 False 就永久停摆且一声不响（实测停了 61 分钟）。
        # 📌 **一个裸 bool 表达不了「谁持有、持有多久、过期算谁的」，所以它防不住任何一种泄漏。**
        self._canary = None  # lazy init，见 maybe_run_canary
        self._push_callback = None  # async (content: str) -> None，由 app.py 注入

        # 本会话是否已经提示过"当前模型不适合写 Skill，建议切换"
        self._model_switch_suggested_this_session: bool = False

        # 本轮已经失败过的 (工具名, 故障类型)，用于"你这轮已经试过这个名字"的升级措辞
        self._tool_failures_this_turn: dict[str, int] = {}

        # Step 4：pending 状态时间戳（用于超时清理）
        self._pending_skill_at: float = 0.0
        self._pending_action_at: float = 0.0

        # RAG 是否本轮真的被调用过（用于 UI HIT 灯）
        self._rag_hit_this_turn: bool = False

        # Phase 2：全文加载是否本轮被调用过（用于 UI FullFile 灯）
        self._full_file_hit_this_turn: bool = False

        # 轻量版 working memory
        self._session_log: list[str] = []
        self._pending_skill_description_cache: str = ""
        # 记录最近一次部署的 Skill 名,用于"这个怎么用"类追问的 target_skill 兜底
        self._last_deployed_skill: str | None = None
        # 记录最近一次调用的 Skill,用于"这个有问题修一下"类模糊路由
        self._last_called_skill: str | None = None
        # 记录最近一次在任意 Skill 相关意图里被点名/解析出的 Skill 名（无论是
        # 调用/查看/启用/禁用/删除哪种动作），用于"刚才那个/为什么被禁用了"
        # 这类不带名字的追问兜底——_last_deployed_skill 和 _last_called_skill
        # 各自只覆盖部署/调用这一种场景，追问别的动作（比如启用/禁用）时会双双
        # 落空，导致明明刚提过名字也照样答"请告诉我你想了解哪个 Skill"。
        self._last_mentioned_skill: str | None = None
        self._notes_written_this_turn: list = []   # 本轮 write_user_note 写入的笔记（供 UI 弹气泡/进抽屉）

        # 跨会话 SQLite working memory
        try:
            self._wm = get_memory_store()
            from datetime import datetime as _dt
            self._wm_session_id = _dt.now().strftime("%Y%m%d_%H%M%S")
        except Exception as _wm_err:
            logger.warning(f"[WorkingMemory] 初始化失败，跳过: {_wm_err}")
            self._wm = None
            self._wm_session_id = ""

        # lazy + 后台初始化，避免阻塞 UI 启动
        # 修复：引用进程级共享 Event，不要每个实例新建自己的
        self._rag_ready = _rag_ready_event
        self._init_rag_async()

        # ReAct 并发信号量（在第一次 asyncio 事件循环里懒初始化）
        self._rag_parallel_sem: asyncio.Semaphore | None = None
        self._tool_parallel_sem: asyncio.Semaphore | None = None
        # ⭐ [⑦ · 切写那一步 · 2026-08-06] `_active_tool_batch_open` **这个字段已经删除**。
        #
        # 它原来是 ReAct batch 事务标记（纯内存 bool），现在降级成从 ToolBatchSpan
        # 派生的**只读属性**（见下面的 property）。三步迁移走完：
        #     A 旧字段权威、内核镜像对答案   ✅ 2026-08-05（8/8 覆盖，1 处分歧已定性）
        #     B 内核权威、旧字段降级为派生   ✅ 本次
        #     C 删掉字段本身与它的每轮重置   ✅ 本次
        #
        # ⭐ **切权威顺手修掉一处真 bug**（观测期的"预判 A"，shadow 已证实）：
        # exit-tool 与其他工具同轮那条路径（`_run_react_loop` 里那段）
        # **从头到尾没碰过旧标志** —— 它确实开了又关了一个批次，但旧标志全程 False。
        # 于是那一轮如果中途抛异常，`_clean_damaged_memory` 会因为标志是 False 而
        # 跳过回滚，留下一个坏 pair。现在状态从 Span 派生，那条路径**终于有标记了**。
        # 这是行为改善，不是重构 —— changelog 要单独写一条。
        # shadow 用的 turn 标识。在 __init__ 里先给个空值，
        # 是为了让 `getattr(orch, "_rt_turn_id", "")` 那种兜底永远不必真的兜底——
        # 属性存在但为空，比属性不存在更容易在日志里看出"谁忘了设它"。
        self._rt_turn_id: str = ""
        # 当前打开着的 Span id。
        # ⚠️ 为什么需要它：旧标志 `_active_tool_batch_open` 有【两个】清理点——
        # 每轮开头的重置，以及 `_clean_damaged_memory` 里那行（7279）。
        # 第二个是早先没记下来的，实测 shadow 第一天就抓到了：span 停在 OPEN 而旧标志
        # 已被它置回 False，于是下一轮 sweep 误报成"批次抛异常"。
        # 把 span id 挂在实例上，任何清理点都能顺手 abort 它。
        self._rt_open_span: str | None = None

        # SkillSpec hard_validate 的报错。降级为直接代码生成时要把它带给模型 ——
        # 只进日志的话，模型会原样再犯（实测 一个会话里犯了三次同类错误）。
        self._last_spec_errors: list[str] = []

        # 最近一次审计窗口校验失败 / 被用户丢弃的 Skill。
        # ⚠️ 必须是**实例状态**而不是 memory 消息：app 侧那条
        # `[System record: ...discarded...]` 会被 max_turns=10 切掉，
        # 而模型往往是在很多轮之后才说"上次写的校验报错，重新写"。
        # 实例状态 + 每轮重建的动态段，天生不受截断影响。
        self._last_audit_failure: dict | None = None
        # 用户在待办卡片上点了「回复这条」→ 下一条消息的指向是显式的。
        # ⚠️ 只是**意图**，不动 Interaction 状态；落盘仍走 answer_open_interaction。
        self._reply_target: dict | None = None
        # ⭐⭐⭐ [2026-08-13 CMD63] 本轮**已被移交**的指向 —— 见 `hand_off_reply_target()`。
        # UI 一按发送就把 `_reply_target` 移到这里（而不是清掉），
        # 因为真正的消费者（模型动态段）比 UI 晚 4 毫秒才来读。
        self._reply_target_turn: dict | None = None

        # ⭐⭐ 重启后把待审草稿捞回来。必须放在 __init__ 最后 ——
        # 它要用 `_put_pending_skill`，也要 `_pending_skills` 已经建好。
        _rt_restore_pending_skills(self)

    # Step 4：pending 状态超时常量（秒）。超过即视为脏数据自动清除。
    # 原值 5*60(5分钟)对"用户读完一段多段落计划/审计代码再回复"这类场景明显
    # 偏短——回复质量越高、内容越详实，用户思考时间越长，越容易撞到这条线。
    # 调到 30 分钟，覆盖绝大多数真实的"认真读完再回"节奏。
    PENDING_TIMEOUT_SECONDS = 30 * 60

    # ── ReAct 主循环常量 ──────────────────────────────────────────────────
    REACT_MAX_ROUNDS = 30         # 单次用户消息最多执行轮数。OS 多步任务（每点一下/
                                  # 输入一次屏幕就占一轮）8 轮远不够，且会卡住缩窗后
                                  # 没机会调 full 恢复。放宽到 30（对齐 Claude Code 的多轮风格）。
    MAX_TOOLS_PER_ROUND = 4       # 单轮最多并发工具数（防止 token 爆炸）

    # 并行安全工具（只读，无副作用，可并发执行）

    # 必须串行执行的工具（有副作用、需要用户交互、或改变系统状态）

    # 触发 ReAct 循环立即退出（进入专属状态机）的工具

    # ⭐⭐⭐ **长任务交还控制权的阈值（秒）** —— 与任务类型无关。
    #
    # 历史：它 8s → 90s（2026-08-09 上午，已定），当时还叫「MCP 自动后台化阈值」。
    # 下面那段解释的就是 8s 为什么错；而**它的名字和覆盖面又错了一次**，见更下方。
    #
    # 🔴 **8 秒落在正常值域里**：一次 browser_navigate、一次大页面 fetch
    #    都可能超过它。于是这个机制在**常见路径上**开火，而在常见路径上它是纯开销：
    #    多两次 LLM 往返（一次说「我放后台了」、一次唤醒后继续）+ 一次用户
    #    不需要的绕路，而且**会把一个 ReAct 链从中间切断**
    #    （给模型的话就是「end this turn and do not call more tools」）。
    # 📌 **一个「防异常」的阈值，如果落在正常值域内，它就不再是保护，
    #    而是变成了主路径** —— 而主路径上的每一条绕路都要付 token 和一次用户困惑。
    #
    # ⭐ 它真正要防的是「几分钟不说话」（一个挂住的连接、一个超大下载），
    #    那和 8 秒不是一个量级。90 秒之后，绝大多数正常调用在前台跑完、
    #    ReAct 链保持完整、一次往返。
    # ⚠️ 提高阈值意味着前台阻塞更久 —— 而那个代价**正好是早先帮我们付掉的**：
    #    durable inbox 之后用户随时能说话，被卡住的只剩「Nano 晚一点开口」。
    #    📌 **一个阈值的合理值，会随着「它防的那件事的代价」变化而变化** ——
    #       inbox 落地时就该重新评估它，当时没有。
    # 🔬 待实测标定（与 `MAX_BACKGROUND_RUNNING` 同类：这类数字推不出来，只能量）。
    #
    # ⭐⭐⭐ **2026-08-09 第二次修正：这个阈值与「任务类型」无关，
    #    而且它的产物【不是】「转后台」，是「把控制权交回模型」。**
    #
    # 🔴 上一版把它做成了 MCP 专属（名字就叫 `_LONG_TASK_HANDBACK_SEC`），
    #    而原话是「从来就没把 mcp 和长命令分开过，我们说的是
    #    『耗时长的任务』，跟任务类型从来就没有关系过。」
    #：**确实从来没有按类型区分的设计** —— `_LONG_TASK_HANDBACK_SEC`
    #    只是 MCP 落地时的一个局部实现，是做回看时按「哪里已经有钩子」
    #    找接入点，才把它当成了机制。
    # 📌 **给一个机制接线时，要按「哪些场景需要它」去找接入点，
    #    不是按「哪里已经有现成的钩子」** —— 后者会让机制的覆盖面
    #    等于历史遗留的形状，而不是等于它的目的。
    #
    # ⭐⭐ **而更深的一层是 用户推翻的「强制后台化」本身**：
    #    现实只有两种情况 ——
    #      ① Nano 主动丢后台（它自己定第一次回看间隔）
    #      ② 没丢，就在前台跑，但保留系统这个回看触发时间
    #    而 ② **压根不需要「强制后台」**：**回看本身就回答了
    #    「为什么这么久」**（根本完不成 / 一切正常再等等）。
    # 📌 **一个纯粹为了「让控制权回到模型手上」的机制，
    #    不该产生用户可见的语义。** 旧实现让 Nano 必须说一句
    #    「我放后台了，回头告诉你」—— 那是实现细节泄漏成了对话内容。
    #    用户该看到的只有「还在跑，我看了一下，情况是 X」。
    # ⭐ 见 `executor_write._FOREGROUND_WAIT_SEC` 上方那段：
    #    2026-08-22 那次建模 把三个宽限期（命令 45 / 外部服务 90 / Subagent 5）
    #    **统一成 5 秒** —— 📌 它们本来就在答同一个问题，
    #    只是当初被当成三个问题分别定了值。
    _LONG_TASK_HANDBACK_SEC = 5.0

    # ⭐ 被交回控制权之后，那个**载体**还能活多久的硬上限。
    #
    # ⚠️ 外部评审 2026-08-09 发现的时间矛盾是真的：MCP 客户端默认 120s 硬超时
    #    < 90s 交还 + 60s 首次回看 = 150s → **回看在数学上不可达**。
    # ⭐ 但正确的解释不是「给 MCP 开个特例」，而是一条通用规则：
    #    📌 **一个操作的期限，在控制权被交回模型之后，
    #       应该由「后台合同」决定，而不再由「前台等待的耐心」决定。**
    #    前台阈值（90s）问的是「我还要不要干等」；
    #    这个期限问的是「这件事本身最多允许跑多久」。两个不同的问题。
    # ⚠️ 它是**载体的硬上限**，不代替模型每次回看后的重排判断；
    #    30 分钟与 WaitCondition 的默认 orphan 兜底同量级。
    _HANDED_BACK_DEADLINE_SEC = 30.0 * 60.0

    # ⭐⭐ **第一次回看的间隔** —— 语义是「**开始怀疑这个长任务出问题了**」。
    #
    # ⚠️ 这是系统唯一该承担的那个数字。已定设计：
    #    系统只设**第一次**，之后每次回看都由**模型**根据看到的东西自己决定下一次
    #    （进度 10% 就排远点、95% 就干脆不排、明显坏了就换办法）。
    # 📌 **常量只该承担系统答得出的那个问题**（「多久之后开始怀疑」）；
    #    「这件事还要多久」只有模型能答 —— 任何固定数字都覆盖不了真实情况。
    # ⚠️ 而 Nano **主动**丢后台的那条路**连这一次都不需要** ——
    #    第一次就是它自己填的（具体任务具体判断）。
    _FIRST_RECHECK_SEC = 60.0

    # ⭐ 每次回看唤醒**之前**，系统先把下一次回看推到这个长兜底上，
    #   然后模型再覆盖它（排近一点 / 干脆不排）。
    # 🔴 **没有这一步会每 5 秒唤醒一次**：驱动唤醒的是那个
    #   `status IN ('WAITING','DUE_FOR_REVIEW') AND fire_at <= now` 查询，
    #   而 `_due` 命令的幂等只保证不重复写库、**不保证不重复点火**。
    #   📌 **一个「幂等的状态转换」不等于「幂等的副作用」。**
    # 📌 而兜底取**长**是有意的：**默认必须是安全的，模型只在偏离默认时才说话** ——
    #    模型这一轮没判断（或者崩了），下一次回看是这个长兜底，不是 5 秒。
    _RECHECK_FALLBACK_SEC = 900.0

    # Phase 2：全文加载工具链最大连续调用轮数（防止模型无限循环）
    # 2026-06-19 路由排查：原来是 3，实测复现"一次任务要依次看4个知识库文件"
    # 时实测卡在轮数上限——跟 EXPLORATION_MAX_FILE_TOOL_CHAIN 当年放宽到 5 时
    # 写的理由（"探索常需要依次查看多个文件，3轮容易卡在边界"）是同一个问题，
    # 当时只改了探索阶段那条路径，主决策这条路径一直没跟着调，这次补齐。
    MAX_FILE_TOOL_CHAIN = 6

    # SKILL_CREATE 探索阶段单独放宽的链上限。
    # 探索常需要依次查看多个文件（说明文件 + 多份数据表），3 轮容易卡在边界。
    EXPLORATION_MAX_FILE_TOOL_CHAIN = 6

    # ── 初始化 ───────────────────────────────────────────────────────────

    def _init_rag_async(self):
        """后台线程初始化 RAG 索引，不阻塞 UI 启动。
        
        全局只跑一次：防止 NiceGUI 多次实例化 Orchestrator 时重复启动索引线程，
        避免多线程同时跑 OCR 导致内存耗尽。
        """
        global _rag_init_started
        with _rag_init_lock:
            if _rag_init_started:
                # 不重复索引，也不在这里 set()——self._rag_ready 现在是
                # 进程级共享 Event，会由第一个实例启动的索引线程在真正
                # 完成后统一 set()。
                return
            _rag_init_started = True

        def _run():
            try:
                # Phase 1 修复：启动时清理历史遗留的临时文件
                try:
                    cleaned = rag_engine.cleanup_stale_temp_files()
                    if cleaned > 0:
                        logger.info(f"[RAG] 启动清理：移除 {cleaned} 个历史遗留临时文件")
                    rag_engine._init_stage_log.append(f"temp_cleaned:{cleaned}")
                except Exception as e:
                    logger.debug(f"[RAG] 启动清理跳过: {e}")

                stats = rag_engine.index_documents()
                _n_err = len(stats.get('errors', []))
                (logger.warning if _n_err else logger.info)(
                    f"[RAG] 知识库就绪 · 新增 {stats['indexed']} · 未变化 {stats['skipped']} · 失败 {_n_err}")
                rag_engine._init_stage_log.append(
                    f"done:{stats['indexed']}:{stats['skipped']}:{len(stats.get('errors', []))}"
                )
            except Exception as e:
                # ⚠️ 这一个 except 曾经吞掉三次根因完全不同的致命失败（2026-08-03 实测）：
                #   ① 旧版 chromadb 建的库与当前版本 schema 不兼容
                #   ② transformers 因 CVE 拒绝加载 .bin 权重（缺 safetensors）
                #   ③ 模型压根没下载成功
                # 三次用户可见表现完全相同：UI 零提示，知识库彻底不可用，只有翻 cmd 才发现。
                # 现在改为登记到 HealthRegistry，由 UI 侧消费者呈现。
                # rag.py 里的具体加载点已按根因分类上报；这里只兜底那些没被具体上报覆盖的。
                logger.error(
                    f"[RAG] 后台初始化失败: {type(e).__name__}: {e}\n{traceback.format_exc()}"
                )
                try:
                    from core.health import Cap, get_health, Status, report_fault
                    _h = get_health()
                    if _h.status_of(Cap.KB_VECTOR_SEARCH) == Status.AVAILABLE and \
                       _h.status_of(Cap.KB_STORE) == Status.AVAILABLE:
                        report_fault(
                            Cap.KB_STORE, "RAG_INIT_FAILED",
                            user_message="知识库初始化失败，检索功能当前不可用。",
                            hint="查看运行日志里的 [RAG] 段落获取详细报错。",
                            hint_en=("Check the [RAG] section of the runtime log for the "
                                     "full error; restarting Nano retries initialisation."),
                            detail=f"{type(e).__name__}: {e}",
                        )
                except Exception:
                    pass
            finally:
                # ⚠️ 语义说明（外部评审推演出来的）：这个 Event 表达的是【初始化流程结束】，
                # 不是【组件可用】——成功失败都会 set。UI 遮罩用它决定"不再挡着"是对的，
                # 但绝不能拿它当 ready。真实可用性一律查 HealthRegistry。
                self._rag_ready.set()

        t = threading.Thread(target=_run, name="rag-init", daemon=True)
        t.start()

    def _load_system_instruction(self) -> str:
        parts = []
        persona_path = os.path.join(os.path.dirname(self.instruction_path), "persona.txt")
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                parts.append(f.read())
        if os.path.exists(self.instruction_path):
            with open(self.instruction_path, "r", encoding="utf-8") as f:
                parts.append(f.read())
        if parts:
            return "\n\n---\n\n".join(parts)
        return (
            "You are Nano, the user's personal assistant.\n"
            "Available tool chain: {skills}.\n"
            "Stay in Nano's persona: talkative but not verbose, sharp, technically tough, a little snarky but not hurtful, and never like customer support.\n"
            "You dislike vague requests and rework, so if something is unclear, point it out directly and give an executable way forward.\n"
            "Do not invent facts, results, or abilities. If uncertain, say what is unclear and how to verify it.\n"
            "Do not use customer-service filler such as 'Received' or 'Need anything else?'."
        )

    def _build_tool_awareness_block(self) -> str:
        """构建当前可用能力边界。Token 压缩版：不重复列出所有内置工具长描述。

        ⑩ 两处改动，其余逐字不变：
          · 常驻集不再抄 `_CORE_TOOL_NAMES`，改问目录（`preload=CORE` 派生）。
            ⭐ 顺带**消灭了那个 `- {"answer_open_interaction"}` 的手工减法** ——
               它要减掉的是「名义常驻但其实是条件注入」的那一个，而现在
               `availability` 直接表达了这件事：没有未决交互它压根不在 eligible 里，
               不需要有人记得在这里减一次。
               📌 **一个需要在下游手工修正的名单，说明它上游描述得不对。**
          · Skill 那两行的描述从 `registry.get_skill_awareness_list()` 的
            **`[:50]` 字符级截断**换成目录里的 `awareness`（按词/句边界压缩）。
            🔴 那个 `[:50]` 是 的**另一半** —— 只修 `[:28]` 不修它，
               不算死透。`get_skill_awareness_list` 随本次改动一起删除。
        ⚠️ 「官方 / 用户」这个维度**仍然问 registry**，没有塞进 ToolDefinition：
           `ToolOrigin` 回答的是「身份权威在哪」（BUILTIN/SKILL/MCP），
           再让它兼答「是不是官方预置」就是**一个字段表达两个现实** ——
           正是这一整轮在反对的东西。
        """
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        _skills = [d for d in _cat.advertised(ToolScope.MAIN, _rtv)
                   if d.origin is ToolOrigin.SKILL]
        cats = {"official": [], "user": []}
        for d in _skills:
            entry = {"name": d.name, "desc": d.awareness}
            try:
                _is_official = self.registry.is_official_skill(d.name)
            except Exception:
                _is_official = False
            cats["official" if _is_official else "user"].append(entry)
        lines = [
            "\n\n[Current Capability Boundary]",
            "Nano has always-on core tools and more deferred capabilities. "
            "If a needed capability is not in the active tool schema, call load_tools first instead of saying Nano cannot do it.",
            "Always-on core: " + " / ".join(sorted(
                m["name"] for m in _cat.core_manifests(ToolScope.MAIN, _rtv))),
        ]
        lines.append(
            "When explaining user-created Skills to the user, localize or paraphrase their descriptions into the user's current language. "
            "Do not translate Skill names, filenames, parameters, field names, or code identifiers. "
            "Official built-in Skills may keep their original fixed descriptions."
        )
        if cats["official"]:
            parts = [f"{s['name']} ({s['desc']})" for s in cats["official"]]
            lines.append("Official Skills (schema loaded on demand; cannot be deleted/disabled): " + " / ".join(parts))
        if cats["user"]:
            parts = [f"{s['name']} ({s['desc']})" for s in cats["user"]]
            lines.append("User Skills (schema loaded on demand): " + " / ".join(parts))
        else:
            lines.append("User Skills: none yet; new reusable Skills can be created when requested.")

        # ⭐⭐ **已配置的 MCP 服务清单** —— 实测 2026-08-28 抓到的缺口。
        #
        # 🔴 问题：用户说「有个 mcp 叫 context7」，Nano 答「这边找不到 / 目前只接入了
        #    time 这一个」—— 而 context7 明明在配置里、还连着。
        #    它说的那句「只接入了 time」不是查出来的，是它**记得自己刚接入过 time**。
        # 📌 根因：模型能看到的只有【已连接 server 的工具】（进 deferred awareness），
        #    **从来没有一份「有哪些 MCP 服务、各自什么状态」的清单**。
        #    ⇒ 于是 `manage_mcp` 直接作废：它要 `server_name`，而模型不知道有哪些名字。
        # ⚠️ 原本清单里的「MCP 感知」做的是**变更感知**（增删改记进 session log），
        #    而这里缺的是**状态感知**（现在有哪些）—— 两件事被混成了一件。
        #    📌 「发生了什么」和「现在是什么」是两份不同的账，缺哪一份都会让模型瞎猜。
        #
        # ⭐ 状态**必须写出来**，不能只列名字：场景「用户手动禁用了某个 MCP」全靠它 ——
        #    模型要能说「它被禁用了，要我打开吗」，而不是「我没有这个能力」。
        # ⚠️ 形状照抄上面 Skill 那两行（名字 + 一句话），每轮成本约几十 token：
        #    MCP 数量天然很少（个位数），而没有它模型就会**编**。
        try:
            from core.mcp_client import MCPManager as _MM_r
            _snap = _MM_r.instance().status_snapshot()
        except Exception:
            _snap = []
        if _snap:
            _ST_WORD = {"connected": "connected", "disabled": "disabled by the user",
                        "failed": "failed to connect", "needs_auth": "needs authentication",
                        "connecting": "connecting", "disconnected": "not connected"}
            # ⚠️ **只给名字 + 状态，不给描述。**
            #    📌 模型选工具靠的是 deferred awareness 里那些【工具】的描述，
            #       server 这一层的描述对「选哪个工具」毫无帮助 ——
            #       它的用途是回答用户「这个 mcp 是干嘛的」，而那是**低频问题**。
            #    ⇒ 每轮都发它，等于为一个偶尔才被问到的问题付固定成本。
            #      （实测带描述 127 token/轮，去掉后 ~40。）
            #    ⭐ 真被问到时，管理页和 `status_snapshot()` 里都有完整描述。
            # ⚠️ `owned_by` 非空的**不列** —— 它是某个 Skill 的内部零件，
            #    模型既管不了它、也从来调不到它的工具
            #    （`expose_tools=False` ⇒ 隐藏 server 的工具连 `_tool_index` 都不进，
            #      只能被那个包装 Skill 通过 `call_hidden()` 走 —— 已回代码核实）。
            #    📌 列一个它既不能用、也不能管的东西，只会让它去"修"一个没坏的东西：
            #       实测 2026-08-28 它就这么干了，还宣布"连上了"。
            _mp = [f"{_m['name']} [{_ST_WORD.get(_m.get('status', ''), _m.get('status', ''))}]"
                   for _m in _snap if not _m.get("owned_by")]
        if _snap and not _mp:
            _snap = []          # 全是零件 ⇒ 对模型而言等于一个都没配
            # 🔴🔴 **「authoritative and complete」这半句不是客套，是这条的命门。**
            #    实测第二轮：清单已经在上下文里、内容也对，模型**照样**花了 5 次工具
            #    调用 68.7 秒去翻 `mcp*.json`，最后加载了用户主目录下的
            #    `.claude.json` —— 那是 **Claude Code 的配置**，不是 Nano 的
            #    （Nano 的在 `config/mcp_servers.json`）。
            #
            # ⭐⭐ 同一个问题的第三次发作，三次的形状一模一样：
            #        ① `.cursor/mcp.json`  → 以为自己是 Cursor
            #        ② 会话记忆            → 「我只接入了 time 这一个」
            #        ③ `.claude.json`      → 以为自己是 Claude Code
            #    📌 **没有一个被声明为权威的来源，模型就会自己找一个像样的。**
            #       而「像样的来源」在一台装着别的 AI 工具的机器上遍地都是。
            #
            # ⚠️ 这不算「专门给某个模型的修正动作」：这份清单**客观上**就是权威且
            #    完整的（直接来自 MCPManager 的活状态），原来只是没把这个事实说出来。
            #    📌 补一句真话 ≠ 打一个补丁。
            # ⚠️ 多花的约 50 token/轮，换掉的是 68 秒 + 5 次工具 + 一个读错文件的答案。
            #    📌 体验 > 成本。
            lines.append(
                "MCP servers on this machine (this list is authoritative and complete "
                "- answer questions about which MCP servers exist or their status "
                "directly from it; never search the disk or read config files for this, "
                "config files belonging to other AI tools are not Nano's). "
                "manage_mcp enables / disables / deletes / retries them; "
                "connect_mcp adds a new one: " + " / ".join(_mp))
        else:
            # ⚠️ 空清单**同样要带权威性** —— 否则模型看到 "none" 的第一反应
            #    就是「不对吧，我去找找配置文件」，于是绕回上面那三次里的任意一次。
            lines.append(
                "MCP servers: none configured on this machine yet (authoritative "
                "- do not look for MCP config files on disk). connect_mcp can add one.")
        return "\n".join(lines)

    def _build_pipeline_context(self) -> list:
        return self.memory.get_full_context()

    @staticmethod
    def _insert_before_cache_break(text: str, extra: str) -> str:
        """把稳定协议插入 CACHE_BREAK_MARKER 前，确保可被 prompt cache 命中。"""
        if not extra:
            return text
        if CACHE_BREAK_MARKER in text:
            return text.replace(CACHE_BREAK_MARKER, extra + CACHE_BREAK_MARKER, 1)
        return text + extra

    # ⭐⭐ Nano 的「我在哪、我在什么机器上」—— 2026-08-28 用户提出。
    #
    # 🔴 起因是 MCP 那次绕路：模型不知道自己的程序目录，于是去翻用户主目录，
    #    读到了 `.claude.json`（Claude Code 的配置）当成自己的。
    #    📌 前后三次发作（.cursor / 会话记忆 / .claude.json）都是同一句话：
    #       **一个不知道自己在哪的程序，会把任何长得像自己配置的文件认成自己的。**
    #
    # ⚠️ 这里是**程序目录**，不是工作目录（用户特意点名）：
    #       cwd    取决于用户从哪儿启动（双击 / 快捷方式 / 命令行各不相同）
    #       __file__ 是这个 .py 文件的真实位置 —— **装在哪就是哪**
    #    ⇒ 「每个人存放路径不一样」恰恰不构成问题：它不依赖 cwd、注册表或启动方式。
    # ⚠️ `core/mcp_client.py` 顶部有同源的一份 `_ROOT`。将来若要建 `core/paths.py`
    #    统一出处，这两处一起收 —— 本次不扩范围。
    # 🔴 若将来打包成 exe（PyInstaller），`__file__` 会指向临时解压目录 `_MEIPASS`，
    #    **不会报错，只会静默指错**。到那天必须在这里加 `sys.frozen` 分支。
    _ENV_BLOCK: str = ""          # 一次会话内不变 ⇒ 只算一次（注册表读取不该每轮跑）

    @classmethod
    def _environment_block(cls) -> str:
        if cls._ENV_BLOCK:
            return cls._ENV_BLOCK
        import sys as _sys, platform as _pf
        try:
            _root = str(pathlib.Path(__file__).resolve().parent.parent)
        except Exception:
            _root = "(unknown)"
        cls._ENV_BLOCK = (
            "\n\n[Environment]\n"
            "Nano itself is running in the following environment:\n"
            f"- Nano's own program directory: {_root} "
            "(this is where Nano's own files live - its code, config and data. "
            "It is NOT the user's working folder; never assume the user's files are here.)\n"
            f"- Platform: {_sys.platform}\n"
            f"- OS Version: {cls._os_version_string()}\n"
        )
        return cls._ENV_BLOCK

    @staticmethod
    def _os_version_string() -> str:
        """真实的 OS 版本串。

        🔴 **不能用 `platform.version()` / `platform.platform()`** —— 它们走
           `GetVersionEx`，被 Windows 的兼容性垫层锁住。实测这台机器：
               platform.version()      = 10.0.19041   ❌
               sys.getwindowsversion() = build 19045   ✅
           📌 差 4 个版本，而且**不报错** —— 最顺手的那两个 API 悄悄给错值。

        🔴 **Windows 11 的注册表仍然写着 "Windows 10 Pro"**（微软没改过这个键）。
           ⚠️ 这条在 Win10 上**永远测不出来** —— 正是「在我机器上好使」
              的标准形态，所以按 build 号硬修正。
        """
        import sys as _sys, platform as _pf
        if _sys.platform != "win32":
            return _pf.platform()          # mac/linux 没有上面那两个坑
        name, build, major, minor = "", 0, 10, 0
        try:
            _wv = _sys.getwindowsversion()
            major, minor, build = _wv.major, _wv.minor, _wv.build
        except Exception:
            pass
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as _k:
                name = str(winreg.QueryValueEx(_k, "ProductName")[0])
                if not build:
                    build = int(winreg.QueryValueEx(_k, "CurrentBuild")[0])
        except Exception:
            pass
        if not name:
            name = f"Windows {_pf.release()}"
        if build >= 22000 and "Windows 10" in name:
            name = name.replace("Windows 10", "Windows 11")
        return f"{name} {major}.{minor}.{build}" if build else name

    # ── 分类器 / 无工具快路径的残骸【已全部删除 · 2026-08-04】────────────────
    # 这里原有四个函数，全部零调用方：
    #   _light_mode_override_note      —— 轻量无工具轮的"本轮没加载工具"说明
    #   _build_text_only_context       —— 给无工具快答用的纯文本上下文
    #   _should_use_no_tool_fast_path  —— 判断是否走无工具快答
    #   _query_requires_full_tools_hard—— 关键词硬拦截表（约 50 个短语 + 组合规则）
    #
    # 它们服务的架构在 v1.1「Token压缩 0703」那轮就被拆掉了：分类器与 fast-path
    # 一起删除，所有消息统一走 ReAct 主循环（理由见 _handle_query_impl 第 3 节）。
    # 函数本身被留了下来，此后一直没有任何调用方。
    #
    # 最后那个尤其不该留：它是一张中文关键词硬路由表（"截图/记住/打开文件/现在几点"…），
    # 正是【原则 3 少硬路由】要消灭的东西，也正是当初做路由重构的直接动机。
    # 留着它的唯一风险是被后来人当成"现行规则"照抄。

    @staticmethod
    def validate_skill_code(code: str, spec_side_effects: list | None = None) -> tuple[bool, list[str]]:
        """代码协议校验。

        新增参数 spec_side_effects:
          如果传入 SkillSpec 声明的 side_effects,额外做 AST 级一致性校验
          (防止模型声明 readonly 但代码里偷偷 file_write)。
          不传则只做协议格式校验(向后兼容)。
        """
        import ast
        errors = []
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, [f"语法错误: {e}"]

        # ── 基础导入校验(v3.2 要求导入 SkillResult) ──────────────────────
        if "from core.schema import" not in code:
            errors.append("缺少 'from core.schema import ...' 导入")
        elif "BaseSkill" not in code:
            errors.append("未导入 BaseSkill")

        if not any(isinstance(n, ast.ClassDef) for n in ast.walk(tree)):
            errors.append("缺少类定义")
        if "def get_manifest" not in code:
            errors.append("缺少 get_manifest() 方法")
        else:
            # ⭐ manifest 的**外层形状**必须能被 registry 认出来（2026-08-05 实测）
            #
            # 实测现象：`ExtractIP` 部署成功、热载成功、UI 显示 READY，
            # 但加载日志里一行 `⚠️ manifest 缺少 name，已跳过`，
            # **manifest 被整条丢弃 → 模型的工具清单里根本没有这个 Skill。**
            # 用户看到它在列表里、状态 READY，Nano 却用不了它 —— 没有任何人被告知。
            #
            # 根因：模型返回了 OpenAI 风格的嵌套外壳
            #   {"type": "function", "function": {"name": ...}}
            # 而 `registry._manifest_name()` 取的是**顶层** `name`。
            # 协议 把内层字段（3.1 参数对应、3.2 类型小写）规定得极细，
            # **却从来没规定过骨架** —— 规定了细节、没规定形状。
            #
            # 这里按 registry 的同一判据静态核一次：取不到名字就在**审计窗口**拦住，
            # 而不是等到加载期只留一行 WARNING。
            _m_err = Orchestrator._check_manifest_shape(tree)
            if _m_err:
                errors.append(_m_err)
        if "def get_spec" not in code:
            errors.append("缺少 get_spec() 方法(v3.2 协议要求)")
        if "async def run" not in code:
            errors.append("缺少 async def run 方法")

        # ── SkillResult 返回校验(v3.2 要求返回 SkillResult 而非 str) ──────
        if "SkillResult" not in code:
            errors.append("run() 必须返回 SkillResult 而非字符串(v3.2 协议要求)")

        # ── run() docstring 校验 ─────────────────────────────────────────
        has_run_docstring = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
                if (node.body and isinstance(node.body[0], ast.Expr) and
                    isinstance(node.body[0].value, ast.Constant) and
                    isinstance(node.body[0].value.value, str)):
                    has_run_docstring = True
                break
        if not has_run_docstring:
            errors.append("run() 方法缺少 docstring")

        # ── 类名 / self.name 一致性 ──────────────────────────────────────
        # ── 类名 / self.name 一致性 ──────────────────────────────────────
        # 修正:BaseSkill.__init__ 已自动设 self.name = self.__class__.__name__。
        # 子类如果没有自定义 __init__,self.name 自动等于类名,无需显式赋值。
        # 只有子类有自定义 __init__ 时,才检查显式赋值是否与类名一致。
        class_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        # 找出有自定义 __init__ 的类
        classes_with_init = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name not in ("BaseSkill",):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        classes_with_init.add(node.name)
                        break
        for cls_name in class_names:
            if cls_name in ("BaseSkill",):
                continue
            if cls_name in classes_with_init:
                # 有自定义 __init__:必须显式赋值且与类名一致
                pattern = f'self.name = "{cls_name}"'
                if pattern not in code:
                    errors.append(
                        f"self.name 与类名 {cls_name} 不一致"
                        f"(自定义 __init__ 时必须显式写 self.name = \"{cls_name}\")"
                    )
            # 没有自定义 __init__:BaseSkill 自动处理,跳过检查

        # ── 保留工具名冲突检测 ────────────────────────────────────────────
        for cls_name in class_names:
            if cls_name in _RESERVED_TOOL_NAMES:
                errors.append(f"类名 {cls_name} 与系统保留工具名冲突，禁止使用")

        # ── AST 副作用一致性校验 ──────────────────────────────────────────
        # 检测代码里的真实副作用 API,与 SkillSpec 声明对比
        # 声明 readonly 但代码含 file_write → 直接拒绝
        # ⭐ spec 缺失时改用**代码自己声明的**那一行（见 `_declared_side_effects_from_code`）。
        # 这样审计窗口与部署时喂的是同一个输入，不会再出现
        # "窗口显示绿色可以部署、点下去说部署失败"（实测 图1）。
        _effective_side_effects = spec_side_effects
        if _effective_side_effects is None:
            _effective_side_effects = Orchestrator._declared_side_effects_from_code(tree)
        if _effective_side_effects is not None:
            spec_side_effects = _effective_side_effects
            ast_warnings = Orchestrator._detect_ast_side_effects(code, tree)
            declared_none = (spec_side_effects == ["none"] or spec_side_effects == [])
            for w in ast_warnings:
                # 只有声明了 none/readonly 但实际有高危操作时才报错
                if declared_none or "none" in spec_side_effects:
                    errors.append(
                        f"AST 副作用不一致:SkillSpec 声明 side_effects={spec_side_effects},"
                        f" 但代码含 {w}。请修改 SkillSpec 声明或移除该操作。"
                    )
                else:
                    # 其他情况作为警告记录到 log,不阻止注册
                    logger.warning(f"[SkillWriter] AST 副作用提示: {w} (已在 spec 中声明,可接受)")

        return len(errors) == 0, errors

    @staticmethod
    def _check_manifest_shape(tree) -> str:
        """静态核一次 `get_manifest` 的**外层形状**，返回错误说明（没问题返回 ""）。

        判据与 `registry._manifest_name()` 完全一致：顶层要有 `name`，
        或者顶层是 `{"function_declarations": [{"name": ...}]}`。
        取不到 → registry 会把整条 manifest 丢掉，而**没有任何人被告知**
        （Skill 照样安装、UI 照样 READY、模型却看不见它）。

        ⚠️ 只在能静态看清时才报错。`get_manifest` 里如果是
        `return self._build()` 这类间接返回，我们看不到字典字面量 ——
        那种情况**放行**，宁可漏判也不要误拦一个写法更复杂但正确的 Skill。
        （漏判的代价回到现状：加载期一行 WARNING；误拦的代价是好 Skill 部署不了。）
        """
        import ast as _ast
        _fn = next(
            (n for n in _ast.walk(tree)
             if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
             and n.name == "get_manifest"),
            None,
        )
        if _fn is None:
            return ""
        _ret = next(
            (n for n in _ast.walk(_fn)
             if isinstance(n, _ast.Return) and isinstance(n.value, _ast.Dict)),
            None,
        )
        if _ret is None:
            return ""       # 看不清 → 放行（见 docstring）
        _keys = [k.value for k in _ret.value.keys
                 if isinstance(k, _ast.Constant) and isinstance(k.value, str)]
        if "name" in _keys:
            return ""
        # 🔴 2026-08-29：`function_declarations`（Gemini 风格）**从放行改成拒绝**。
        #
        # 改造前这里放行它，而 `registry._manifest_name` 也接受它 —— 三方本来是
        # 一致的（提示词说别用，但用了也能工作）。这次删掉 registry 那个分支时，
        # 如果不同时收紧这里，就会变成：**审计说没问题、装上却不工作** ——
        # 比原来那个不一致更坏。
        # 📌 **要收紧一条规则，得把认它的地方一次收完** ——
        #    收一半，等于把「宽松但一致」换成「严格但自相矛盾」。
        # ⚠️ 而收紧本身是对的：这个提示词（见上方 get_manifest 的 WRONG 示例）
        #    早就写着 Gemini 风格是错的，项目也只接 Anthropic 了。
        if "function_declarations" in _keys:
            return (
                "get_manifest() 用了 Gemini 风格的列表外壳 "
                "{\"function_declarations\": [{...}]}，本项目只接受顶层形状 → "
                "**整条 manifest 会被丢弃，Skill 装上了但模型看不见它**。"
                "把 name / description / parameters 三个键放到最外层。"
            )
        if "function" in _keys and "type" in _keys:
            return (
                "get_manifest() 用了 OpenAI 风格的嵌套外壳 "
                "{\"type\":\"function\",\"function\":{...}}，registry 取不到顶层 name → "
                "**整条 manifest 会被丢弃，Skill 装上了但模型看不见它**。"
                "把 name / description / parameters 三个键放到最外层。"
            )
        return (
            f"get_manifest() 的返回里顶层没有 name 键（实际顶层键: {_keys or '空'}）→ "
            "registry 取不到名字，**整条 manifest 会被丢弃，Skill 装上了但模型看不见它**。"
            "name / description / parameters 必须在最外层。"
        )

    @staticmethod
    def _declared_side_effects_from_code(tree) -> list[str] | None:
        """从生成的代码里读出它**自己声明**的 side_effects。

        ═══ 为什么需要它（2026-08-05 实测）═══

        AST 副作用一致性校验的入口条件是 `if spec_side_effects is not None`，
        而两个调用点喂的东西不一样：

            审计窗口 `_emit_skill_preview` → 直接传 SkillSpec 阶段的值。
                                            spec 生成失败时是 **None** → **整个检查被跳过**
                                            → 窗口显示绿色"通过协议 v3.2 全部校验，可以部署"
            `_pending_skill` 落库时       → 存 `spec_side_effects or []` → 变成 **[]**
            部署 `apply_pending_skill`     → 传 `[]` → 检查运行 → declared_none → **拒绝**

        **同一段代码，两处结论相反**：用户看着绿色的"可以部署"点下去，被告知部署失败。
        这正是注释里描述过的那个体验问题的第二个实例（那次只把重名检测提前了）。

        ⚠️ 但**不能简单把 None 归一成 []**：那会误报。SkillSpec 阶段失败不等于
        "这个 Skill 声明了自己没有副作用" —— 生成的代码里那个 `get_spec()`
        可能声明得好好的（`side_effects=[SideEffect.SHELL]`）。拿它跟 `[]` 比就是
        比错了对象。

        **真正的权威是代码自己写的那一行** —— 部署之后生效的就是它，而不是那个
        中途失败的 SkillSpec。所以这里把它读出来，作为 spec 缺失时的取值。
        （同 `lifecycle` 的做法：那个字段本来就是从代码里 `re.search` 出来的。）

        返回 None 表示"代码里也没有可识别的声明"，此时保持旧行为（跳过检查）——
        那种情况下代码本身大概率连协议都不合，会被前面的结构检查先拦下。
        """
        import ast as _ast
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.keyword) or node.arg != "side_effects":
                continue
            val = node.value
            if not isinstance(val, (_ast.List, _ast.Tuple, _ast.Set)):
                continue
            out: list[str] = []
            for el in val.elts:
                # `SideEffect.SHELL` → "shell"
                if isinstance(el, _ast.Attribute):
                    out.append(el.attr.lower())
                # 裸字符串 `"shell"`
                elif isinstance(el, _ast.Constant) and isinstance(el.value, str):
                    out.append(el.value.lower())
            return out
        return None

    @staticmethod
    def _detect_ast_side_effects(code: str, tree) -> list[str]:
        """扫描 AST 检测真实副作用 API。**实现已搬到 `core/code_scan.py`。**

        🔴 搬走的理由：临时执行通道要问的是**同一个问题**
           （「这段代码会不会碰外界」），而两份扫描器迟早会分叉 ——
           📌 分叉的表现不是报错，是「Skill 审计拦得住的东西，临时通道放过去了」
              （或者反过来），**而且没有任何东西会告诉我们它们已经不一致了**。
        ⚠️ 这里保留成一行转发，是为了**调用方一个字都不用改**
           （`_audit_skill_code` 仍然调 `Orchestrator._detect_ast_side_effects`）。
        """
        from core.code_scan import detect_side_effects
        return detect_side_effects(code, tree)

    # ── Skill 审计接口 ────────────────────────────────────────────────────

    def _schedule_semantic_memory_write(self, coro) -> None:
        """语义记忆写入是旁路增强，不能阻塞/拖慢部署这个同步方法本身。
        用 create_task 丢进后台事件循环，调用方不等待结果。拿不到运行中的
        事件循环（极少见，比如纯脚本环境）就静默跳过，不报错。
        """
        try:
            import asyncio
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            logger.debug("[SemanticMemory] 没有运行中的事件循环，跳过本次写入调度")
        except Exception as e:
            logger.warning(f"[SemanticMemory] 调度写入任务失败（不影响部署主流程）: {e}")

    # ── `_active_tool_batch_open`：只读派生属性 ──────────────────────

    @property
    def _active_tool_batch_open(self) -> bool:
        """本轮有没有未完成的工具批次。**从 ToolBatchSpan 派生，没有对应字段了。**

        保留这个名字是为了让两个消费者（400 分支的守卫、`_clean_damaged_memory`
        的回滚判据）读起来跟改造前一样 —— 它们关心的语义没变，只是权威换了地方。

        ⚠️ **故意不提供 setter。** 任何 `self._active_tool_batch_open = X` 都会
        直接抛 AttributeError，而不是静默地写一个没人读的字段 —— 那正是双权威的入口。
        要改这个状态，只能通过 Span 命令（`_rt_shadow_open` / `_rt_shadow_commit`
        / `_rt_abort_open_span`）。

        ⚠️ 读失败时返回 **False**（不回滚），不是 True。见
        `toolbatch.has_open_span()` 里那段关于代价不对称的说明。
        """
        _open, _trusted = _rt_has_open_batch(self)
        if not _trusted:
            # 只在**真的读不出来**时才警告。"库里确实没有 OPEN"是事实、不是故障，
            # 把两者混在一起报警会让这条日志变成噪音然后被忽略。
            logger.warning(
                "[ReAct] batch 状态读取不可信，按【无未完成批次】处理："
                "这一轮如果中途出错，末尾工具对不会被回滚（宁可留坏 pair，不误删历史）"
            )
        return _open

    # ── 多槽待审载荷的读写口 ────────────────────────────────────────

    @property
    def _pending_skill(self) -> dict | None:
        """**最近一条**待审载荷的只读视图。

        存在的意义是让"只关心有没有待审"的十几处调用点不用改
        （`if self._pending_skill:` / 日志 …）。
        ⚠️ **需要指定具体哪一条时不要用它**，要显式传 filename ——
        用它就等于又回到了单槽假设。
        """
        for _fn in reversed(self._pending_skill_order):
            _p = self._pending_skills.get(_fn)
            if _p:
                return _p
        return None

    @_pending_skill.setter
    def _pending_skill(self, value: dict | None) -> None:
        """兼容旧写法。`= None` 表示清空全部；赋一个 dict 表示插入/覆盖同名那条。

        ⚠️ 保留 setter 只为兼容 `reset_conversation()` 之类的整体清空。
        **新代码请直接用 `_put_pending_skill()` / `_drop_pending_skill()`**，
        语义清楚得多。
        """
        if value is None:
            self._pending_skills.clear()
            self._pending_skill_order.clear()
            return
        self._put_pending_skill(value)

    def _put_pending_skill(self, payload: dict) -> None:
        """插入一条待审载荷。同名视为新版本覆盖（那是真的同一个 Skill 重新生成）。"""
        _fn = payload.get("filename") or ""
        if not _fn:
            logger.warning("[Skill] 待审载荷没有 filename，无法多槽存储，已忽略")
            return
        self._pending_skills[_fn] = payload
        if _fn in self._pending_skill_order:
            self._pending_skill_order.remove(_fn)
        self._pending_skill_order.append(_fn)

    def _get_pending_skill(self, filename: str | None = None) -> dict | None:
        """按名取；不给名字就取最近那条（UI 单弹窗时代的默认行为）。"""
        if filename:
            return self._pending_skills.get(filename)
        return self._pending_skill

    def _drop_pending_skill(self, filename: str) -> None:
        self._pending_skills.pop(filename, None)
        if filename in self._pending_skill_order:
            self._pending_skill_order.remove(filename)

    def apply_pending_skill(self, filename: str | None = None) -> dict:
        # 不传 filename = 最近那条（保持 UI 单弹窗时代的默认行为）。
        # 传了就精确取那一条 —— 多条待审并存时必须传，否则会部署错人。
        _sel = self._get_pending_skill(filename)
        if _sel is not None:
            self._active_apply_filename = _sel.get("filename") or ""
        if not _sel:
            return {"ok": False, "msg": "没有待审批的 Skill"}
        filename = _sel["filename"]
        code     = _sel["code"]
        mode     = _sel.get("mode", "create")
        target   = _sel.get("target_skill") or filename
        is_os_skill = "os_control" in (_sel.get("spec_side_effects") or [])
        ok, errors = self.validate_skill_code(
            code, spec_side_effects=_sel.get("spec_side_effects")
        )
        if not ok:
            return {"ok": False, "msg": "代码未通过校验: " + " | ".join(errors)}
        try:
            if mode == "update":
                _error_context = _sel.get("error_context", "")
                result = self.registry.update_skill_file(target, code)
                if result.get("ok"):
                    self._drop_pending_skill(filename)
                    self._pending_skill_at = 0.0
                    # 更新后也写 memory 上下文
                    description = self._pending_skill_description_cache or target
                    self._write_skill_deploy_context(target, description, "update")
                    # 持久语义记忆v3：correction 的v1唯一写入触发点——这次update
                    # 是报错修复（error_context非空）且部署成功，才写。普通的
                    # "用户随口要求改改"不触发（_pending_skill["error_context"]
                    # 默认是空字符串，只有 _generate_skill_update 真正带着报错
                    # 上下文走过来才会非空）。
                    if _error_context:
                        from core.semantic_memory import maybe_write_correction
                        self._schedule_semantic_memory_write(maybe_write_correction(
                            self.provider, None,
                            target, _error_context, description,
                        ))
                return result

            # 所有 Skill 统一部署到 skills/（临时 Skill 概念已废弃）
            lifecycle = "permanent"
            description = _sel.get("description", "")

            # 写 skills/
            os.makedirs("skills", exist_ok=True)
            path = os.path.join("skills", f"{filename}.py")
            # 新建模式撞名会无声覆盖已有永久 Skill——之前只有临时 Skill 撞永久
            # Skill 才有警告（见上面 temporary 分支的 _collision），永久对永久撞名
            # 完全没有检查。实测复现过：探索阶段明确说"新建"，结果给出的做法里用了一个
            # 磁盘上已存在、且和这次需求无关的旧 Skill 同名，点部署就会把旧文件
            # 整个冲掉，用户毫不知情。这里直接拒绝，不静默覆盖。
            if mode != "update" and self.registry.is_official_skill(filename):
                self._drop_pending_skill(filename)
                self._pending_skill_at = 0.0
                return {
                    "ok": False,
                    "msg": (
                        f"部署失败：「{filename}」是官方基础 Skill，不支持同名覆盖。"
                        f"如需扩展，请使用不同名称创建新 Skill。"
                    ),
                }
            if mode != "update" and os.path.exists(path):
                self._drop_pending_skill(filename)
                self._pending_skill_at = 0.0
                return {
                    "ok": False,
                    "msg": (
                        f"部署失败：已存在同名永久 Skill「{filename}」，"
                        f"为避免覆盖现有文件已拒绝部署。如果是想修改这个已有 Skill，"
                        f"请明确说'修改 {filename}'；如果是想新建一个不同的 Skill，"
                        f"请换一个不同的名字重新生成。"
                    ),
                }
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            self.registry.reload_all()
            self._drop_pending_skill(filename)
            self._pending_skill_at = 0.0
            logger.info(f"✔ [Skill] {filename} 已安装并热载")
            # 审计交互收尾。**必须在这里**而不是只在工具路径里 ——
            # 用户点 UI 上的「验证并应用」走的就是这个函数，漏了就留僵尸。
            _rt_close_skill_audit(filename, approved=True)
            self._write_skill_deploy_context(filename, description, "create", lifecycle=lifecycle)
            self._last_deployed_skill = filename
            # 部署成功 → 那条审计失败记录作废。留着会让模型下一轮还在说
            # "上次那版缺 get_spec()"，而它其实已经修好并装上了。
            _af = getattr(self, "_last_audit_failure", None)
            if isinstance(_af, dict) and _af.get("filename") == filename:
                self._last_audit_failure = None
            self._wm_add(
                EntryType.SKILL_DEPLOY, filename,
                "deploy",
                detail=description or "",
                tags=["skill", "deploy"],
            )
            from core.semantic_memory import maybe_write_task_pattern
            self._schedule_semantic_memory_write(maybe_write_task_pattern(
                self.provider, None,
                filename, description,
            ))
            _base_msg = f"Skill 「{filename}」已安装，已自动热载到工具链"
            return {"ok": True, "msg": _base_msg}
        except Exception as e:
            logger.error(f"[Skill] 部署失败: {e}")
            return {"ok": False, "msg": str(e)}

    def _wm_add(self, entry_type: str, subject: str, action: str,
                detail: str = "", tags: list | None = None):
        """安全写入 working memory。异常不影响主流程。"""
        if self._wm is None:
            return
        try:
            self._wm.add(
                entry_type=entry_type,
                subject=subject,
                action=action,
                detail=detail,
                tags=tags or [],
                session_id=self._wm_session_id,
            )
        except Exception as e:
            logger.debug(f"[WorkingMemory] 写入失败(跳过): {e}")

    @staticmethod
    def _looks_chinese(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))

    def _write_skill_deploy_context(self, skill_name: str, description: str,
                                     mode: str = "create", lifecycle: str = "permanent",
                                     name_collision: bool = False):
        """Skill 部署后往 memory 写一条 assistant 消息记录部署上下文。

        解决"这个 Skill 怎么用"被错误路由的根本原因:
        部署后 _pending_skill 清空,模型再收到追问时没有上下文。
        在 memory 里写一条明确的记录,后续追问时模型能从 memory 读到。

        name_collision: Fix A 的延伸——"信息全"不能只给 UI 一个 toast，
        模型自己的 memory 里也要有这件事。否则用户后面说"用回原来的
        {skill_name}"，模型不知道这个名字现在被临时版本遮蔽了，
        又会重复一遍"模型以为的状态 vs 实际状态"不一致的坑。
        """
        use_zh = self._looks_chinese(description)

        if use_zh:
            action_label = "更新" if mode == "update" else "部署"
            context_msg = (
                f"Skill「{skill_name}」已成功{action_label}并热载到工具链。\n"
                f"用途：{description or '无描述'}\n"
                f"使用方式：直接用自然语言描述需求，模型会自动调用；也可以明确说：调用 {skill_name}。\n"
                f"如需修改，说：修改 {skill_name}。如需查看代码，说：查看 {skill_name} 的代码。"
            )
        else:
            action_label = "updated" if mode == "update" else "deployed"
            context_msg = (
                f"Skill \"{skill_name}\" has been successfully {action_label} and hot-loaded into the tool chain.\n"
                f"Purpose: {description or 'no description'}\n"
                f"How to use: describe the need in natural language and the model will call it automatically, "
                f"or explicitly say: call {skill_name}.\n"
                f"To modify it, say: modify {skill_name}. To inspect code, say: inspect {skill_name} code."
            )

        if name_collision:
            if use_zh:
                context_msg += f"\n名字冲突：Skill「{skill_name}」与已有永久 Skill 同名，已覆盖。"
            else:
                context_msg += (
                    f"\nName collision: \"{skill_name}\" has the same name as an existing permanent Skill and has overwritten it."
                )

        self.memory.add_message("assistant", context_msg)
        # Keep session_log in English because it is injected back into the system prompt.
        self._session_log_append(
            f"[Skill {('updated' if mode == 'update' else 'deployed')}] {skill_name}: {description or 'no description'}"
        )

    def _session_log_append(self, entry: str):
        """往 session_log 追加一条记录。自动加时间戳。"""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M")
        self._session_log.append(f"{ts} {entry}")
        if len(self._session_log) > 20:
            self._session_log = self._session_log[-20:]


    def _build_audit_failure_injection(self) -> str:
        """把"上一个 Skill 没能部署，以及具体哪里不合协议"摆到模型面前。

        ═══ 为什么需要它（2026-08-05 实测）═══

        审计窗口拦下一个 Skill、用户点丢弃之后，模型手上什么都没有：
          · 代码从未进 memory（`skill_preview` 的 code 只给弹窗渲染）
          · 校验报错只进 `validation_lbl`，纯 UI
          · app 侧那条 `[System record: ...discarded...]` 被 max_turns=10 切掉了
        于是用户说"上次写的校验报错，重新写"时，它只能反问
        「哪个 Skill 出问题了？」「我需要先检查它的代码看看哪里报错」——
        而那个 Skill 压根没部署，`inspect_existing_skill` 查不到。
        **它不是笨，是真的什么都不知道。**

        ⚠️ 刻意**不写 memory**：memory 会被截断，那正是这条缺陷的一半。
        动态段每轮从实例状态重建，模型隔多少轮回来都还在。
        同 `[Open Interactions]` 的做法。

        没有失败记录时返回空串，一个字符都不加。
        """
        af = getattr(self, "_last_audit_failure", None)
        if not isinstance(af, dict) or not af.get("filename"):
            return ""
        _outcome = af.get("outcome") or "awaiting"
        _errs = af.get("errors") or []
        _fn = af.get("filename")
        _state = {
            "discarded": "The user reviewed it and discarded it. It was NOT deployed and its "
                         "code no longer exists anywhere — you cannot inspect it.",
            "awaiting": "It is still sitting in the audit window awaiting the user's decision.",
        }.get(_outcome, "")

        out = ["\n\n[Last Skill Audit — Not Deployed]",
               f"Skill: {_fn}  (mode={af.get('mode', 'create')}, "
               f"{af.get('code_lines', 0)} lines, code_hash={af.get('code_hash') or 'n/a'})",
               _state]
        if _errs:
            out.append("It failed the v3.x protocol check on these points:")
            out.extend(f"  - {e}" for e in _errs)
            out.append(
                "If the user asks you to rewrite or fix it, you already know which Skill and "
                "which problems — do not ask them, and do not try to inspect it (it was never "
                "deployed). Write a fresh version that fixes exactly the points above."
            )
        else:
            out.append(
                "It passed validation but the user discarded it anyway. Do not assume it exists. "
                "If they ask again, write it fresh; consider asking what they disliked."
            )
        return "\n".join(x for x in out if x)

    def hand_off_reply_target(self) -> str:
        """⭐⭐⭐ [2026-08-13 CMD63] 用户按下发送 → 把「回复这条」的指向**移交给这一轮**。

        ═══ 这个方法存在的理由（一个实测 bug，两行日志就能看完）═══

：

            22:33:39.127  [UI] 引用已发出（int_2f32640844）→ 引用态复位
            22:33:39.131  [TOKEN-PLAN] core_tools=6 (…answer_open_interaction)

        用户点了 replay 引用一张 skill_audit 卡、说「部署这个吧」。
        UI 在**渲染发送行时**就把 `_reply_target` 清了，而
        `_build_open_interactions_injection()` **4 毫秒之后**才去读它 ——
        于是那句「⭐ The user explicitly marked this message as answering
        int_2f32640844 — do not pick a different one」**一个字都没进 prompt**。

        模型只看到一条 `[skill_audit]` 和一句「部署这个吧」，
        于是先 `load_tools` 把 `create_new_skill` 捞回来（22:33:39 是 6 个工具，
        22:33:47 变成 7 个，多出来的正是它），再调它 → 进探索 →
        探索作用域里**根本没有"部署"这个出口** → 伸手拿 `create_new_skill`
        → 内部故障路径 → 澄清待办也跟着不登记。
        ⭐ **用户报的那两个"独立问题"其实是一条链，头在这里。**

        ═══ 为什么当初会清早（这才是要记住的部分）═══

        `handle_query` 的 `finally` 里**本来就有**一处复位（2026-08-06，位置正确）。
        2026-08-09 修「composer 停在引用态」时，app.py 那处注释写的是
        「回查发现…**发送路径上一处都没有**」—— 它没看见 orchestrator 已经有了，
        于是加了**第二处**。而那个 bug 的真因是**显示层没被通知**
        （`_refresh_reply_prompt` 那条自愈线），却被修成了**提前清状态**。

        📌 **一个「发出去就该消失」的状态，不该被清掉，该被【移交】** ——
           清掉会让真正的消费者读空。
        📌 修「显示没跟上」时，动的必须是显示；动状态会把延迟问题变成丢失问题。

        四个诉求这样同时满足：
          · composer 立刻复位（`_reply_target` 确实空了）
          · 模型拿得到指向（读 `_reply_target_turn`）
          · 一次性（`finally` 里连同快照一起清）
          · 单一权威（两个字段都只在 orchestrator 上，UI 只调这个方法）

        返回被移交的 iid（没有就空串），供调用方打日志。
        """
        if self._reply_target is None:
            return ""
        self._reply_target_turn = self._reply_target
        self._reply_target = None
        return (self._reply_target_turn or {}).get("iid") or ""

    def _l3_index_block(self) -> str:
        """已经移出上下文的那些交换，留下的一行行索引。

        ⚠️ **低于一条就一个字都不注入** —— 📌 一段每轮都在、又暂时没有内容的
           标题会被模型学成背景噪音（同压力段那条纪律）。
        ⚠️⚠️ **刻意不判总开关。** `ladder_enabled` 是一个**单向迁移开关**：
           它控制"还产不产生新的衰减"，**不控制"已经产生的算不算数"**。
           关掉它之后，已经降到 L3 的那些交换仍然：hydrate 时被移出、
           UI 仍隐藏、索引仍注入 —— 三处**必须一致**，所以读侧一律不判开关。
           📌 已经发生的降级是**既成事实**，一个 bool 撤不回来 ——
              真要恢复旧行为，清 `exchange_decay` 表（见 `model_config.json`
              的 `_ladder` 说明）。
        ⚠️ 与 `recall_conversation` 的 availability 判据（`has_evicted_history`）
           **共用同一个事实**：有索引 ⇔ 有工具。
        """
        try:
            from core.context.decay import l3_index_lines
            from core.context.decay_store import DecayStore
            from core.runtime.kernel import get_kernel
            _sid = self.memory.conversation_session_id
            if not _sid:
                return ""
            lines_ = l3_index_lines(DecayStore(get_kernel().store), _sid)
        except Exception:
            return ""
        if not lines_:
            return ""
        return (
            "\n\n[Earlier in this conversation - moved out of context]\n"
            "These exchanges are no longer in your context, but they did happen and "
            "they are still recallable with recall_conversation. Use them to know that "
            "something exists, then recall it if the current question needs it. "
            "Do NOT claim you never discussed these." + "\n"
            + "\n".join(f"  {x}" for x in lines_) + "\n"
        )

    def _image_note_request_block(self) -> str:
        """只在「这轮有图且还没记」时出现的一次性提示。

        ⚠️⚠️ **它由 `has_unsummarized_image()` 控制 —— 和 `note_image` 工具是同一个条件。**
           📌 那条：**一个工具和它的事实来源，必须由同一个条件控制**；
              这里连提示词也挂在同一个条件上，于是三者永远同生共死。

        ⚠️ 「摘要千万不能跟 changelog 修复那个 base64 的 bug 一样，一直每轮注入」——
           ⭐ 这段**没有第二轮可注入**：模型一调 `note_image`，条件就为假。
           而万一它不调，下一轮还有图未记 —— 那时提示还在，这是**正确的**重试，
           不是"每轮都塞"。
        """
        try:
            if not self._tool_runtime_view().has_unsummarized_image():
                return ""
        except Exception:
            return ""
        # ⭐ 留痕：这段是一次性的，出现过就该看得见 —— 否则"摘要没生成"时
        #    分不清是【提示没注入】还是【模型没听】。两者的修法完全不同。
        logger.info("[D10] 本轮有图待记摘要 → 注入一次性提示 + 提供 note_image")
        # ⚠️⚠️ 措辞被实测推翻过一次（2026-08-13）：第一版只说"Before you reply, call
        #    note_image once"，**注入了、工具也给了，模型三次全部无视，直接作答**。
        #    根因不是它没读到，是**主循环的纪律在压制它** —— 系统提示里反复讲
        #    「闲聊不调工具 / 第一轮直接出话」，而这条要求它为一件**用户看不见的事**
        #    先调一次工具，正好撞在那条纪律上。
        # 📌 **一条新指令如果和系统反复强化过的纪律冲突，它必须自己说明"我覆盖那条"** ——
        #    否则模型会按更常被强调的那条走，而且不会告诉你它做了取舍。
        return (
            "\n\n[Image just received — one required tool call]\n"
            "The user's latest message carries an image, and its pixels will be dropped from your "
            "context shortly. Call note_image ONCE, first, before you reply.\n"
            "⚠️ This OVERRIDES the usual guidance about answering directly without tools. It is "
            "not optional and it is not a judgement call: an image is present, so this one call "
            "happens. It is the only chance to record what is in it.\n"
            "⚠️ The summary is NOT your answer. Write it for your future self, independently of "
            "what the user asked; then reply to their actual message in your own voice, and never "
            "recite the summary back to them.\n"
        )

    def _execution_scope_block(self, scope: str = "main_react") -> str:
        """每轮的执行作用域声明。极短（约 40 token）。

        ⚠️⚠️ **措辞必须是 "this model request"，不能写 "this turn"。**
        一个用户 turn 内部有**多次** ReAct model request，而 `load_tools` 之后
        `tools_manifest` 会在 turn 中途变化 —— 说 turn 就把粒度说错了一档，
        而这次要明确的粒度恰恰就是 request。

        ⚠️ **刻意不列工具名。** 当前 API manifest 已经是工具的唯一权威来源，
           在这里再列一遍就是第二份名单（正是 花一整轮消灭的东西）。
           所以这段话只讲**规则**，不讲**内容** —— 也因此它在一个 turn 内恒定。

        📌 而这段话之所以敢说 "Only tools attached to this model request are callable"，
           是因为 让它**成为了真的**（`TOOL_NOT_ACTIVE` 那道闸）。
           在 ③ 之前它是一句假话 —— 实测实证：`create_new_skill` 不在本轮 6 个工具里，
           模型凭历史 schema 调它，**照样执行了**。
           📌 **要注入一句话，先让它成为真的。**
        """
        try:
            from core.runtime.identity import current_runtime_id
            _rid = current_runtime_id()
        except Exception:
            _rid = "unknown"
        return (
            "\n\n[Execution Scope]\n"
            f"runtime_id={_rid}\n"
            f"scope={scope}\n"
            "Only tools attached to this model request are callable. "
            "Historical tool activation does not carry forward — an earlier "
            "\"loaded capabilities\" result does not make a tool callable now. "
            "If a capability you need is not attached, call load_tools first, "
            "then use it on the next step.\n"
        )

    def _build_quoted_selection_injection(self) -> str:
        """用户在聊天区**选中了一段文字**，然后右键 replay。

        ⭐ 与 `_build_open_interactions_injection` 是两件事，**刻意分开写**：
           那一段说的是「去答这条待办」（带 interaction_id，有工具要调）；
           这一段说的是「我在指刚才这句话」（没有 id，也没有工具要调）。
           📌 「一个字段不许表达两个现实」—— 合成一段就得靠 `iid` 空不空
              去猜是哪种，而那正是让人猜错的写法。

        ⚠️ 引用的是**聊天区里已有的文字**，所以它一定在上下文里（或者已被
           移出 —— 那更需要这一段，模型自己已经看不到原话了）。
           所以这里把原文**逐字**给它，不做摘要。

        ⚠️ 长度截到 200（UI 侧存的时候就截过）：引用是个指路标，不是重新贴一遍。
        """
        rt = (getattr(self, "_reply_target_turn", None)
              or getattr(self, "_reply_target", None) or {})
        if rt.get("kind") != "selection":
            return ""
        q = (rt.get("q") or "").strip()
        if not q:
            return ""
        return (
            "\n\n[Quoted by the user]\n"
            f'The user selected this text from earlier in this conversation and is '
            f'replying to it:\n"""\n{q}\n"""\n'
            "Their message is about THAT specific passage. Resolve any vague reference "
            "in it (this, that, it, here) against the quote above before anything else in "
            "the conversation. If the quote is something you said, they are pointing at "
            "your own words - do not treat it as a new topic."
        )

    def _build_open_interactions_injection(self) -> str:
        """把未决交互摆到模型面前。

        ⭐ 这是整条改造里**用户体感变化最大**的一块。
        旧实现是路由劫持：代码把下一条用户消息截走，模型自始至终不知道
        有个问题挂在那里。于是"用户回答了但被判成新话题"和"用户问了别的
        但被当成回答"这两种错误，模型都没有机会纠正——它看不见。

        现在改成把状态摆给模型，由它判断。三种走向都是它决定的：
            回答      → 调 answer_open_interaction
            改需求    → 同上，relation=ANSWER_AND_AMENDMENT
            说别的    → 不调工具，正常处理；交互保持 OPEN 留在屏幕上

        落在缓存哨兵之后的动态段：没有待办时返回空串、一个字符都不加。
        """
        recs = _rt_live_interactions(self)
        if not recs:
            return ""
        from core.runtime import interaction as _it

        # ⭐⭐ [2026-08-06 实测] 标注新旧，并把"最新那条"显式指出来。
        #
        # 实测："把 skill 部署吧"（不点名）→ Nano 部署了**队列里第一条**。
        # 那条恰好是最早创建的（前台槽给最先来的那个），而模型是照着这份清单挑的。
        #
        # 理由很硬：真实场景下 Nano 跟用户聊了很多轮，最后一轮刚做完一个 Skill，
        # 用户说"ok 部署吧" —— **指的必然是刚做完那个**。反手部署最早那个非常反直觉。
        #
        # ⚠️ 但**不在代码里硬选**（设计原则 3）：已明确说"问用户"和"部署最新的"
        # 两种都对，取决于人设模板与模型的谨慎程度。代码该做的是**把新旧这个事实
        # 摆清楚**，让模型能自己判断；唯一的硬约束是"别默认挑最旧的"。
        #
        # 顺序仍然保持与 UI 一致（前台优先），因为用户说"第一个"时两边得指同一条。
        _newest_id = max(recs, key=lambda x: x.created_at).interaction_id if recs else ""
        _now = 0.0
        try:
            from core.runtime.kernel import get_kernel as _gk
            _now = _gk().now()
        except Exception:
            pass
        lines = []
        _live_ids: set[str] = set()
        _has_audit = False
        for r in recs:
            if r.kind == _it.Kind.SKILL_AUDIT:
                _has_audit = True
            q = (r.prompt_text or "").strip().replace("\n", " ")
            if len(q) > 200:
                q = q[:200] + "…"
            _age = ""
            if _now and r.created_at:
                _m = max(0, int((_now - r.created_at) / 60))
                _age = f" · created {_m}m ago" if _m else " · created just now"
            _live_ids.add(r.interaction_id)
            _tag = "  ← NEWEST" if r.interaction_id == _newest_id and len(recs) > 1 else ""
            lines.append(f"  - {r.interaction_id} [{r.kind}]{_age}{_tag} {q}")
            if r.status == _it.Status.ANSWERED:
                # 重试入口。没有这行，ANSWERED 就成了没有消费者的永久状态
                # 早先就要求过必须定义谁来触发重试）。
                a = (r.answer_verbatim or "").strip().replace("\n", " ")
                if len(a) > 120:
                    a = a[:120] + "…"
                lines.append(
                    f"      ALREADY ANSWERED but the follow-up failed last time. "
                    f'The user already said: "{a}" — call answer_open_interaction again '
                    f"with the same text to retry. Do NOT ask them to repeat themselves."
                )

        # 「回复这条」的目标：只有它**还在上面这份清单里**才算有效指向。
        # 指向已关闭/已取代的那条 = 把模型逼进死角，理由见下方注释。
        #
        # ⭐⭐⭐ [2026-08-13 CMD63] **先读本轮快照，再退回实时值。**
        #    UI 一按发送就把指向移交到 `_reply_target_turn`（见 `hand_off_reply_target`），
        #    此刻 `_reply_target` 已经是空的 —— 只读它就等于永远读不到用户点的那条，
        #    那正是 CMD63「引用了审计卡却去新建 Skill」的根因。
        # ⚠️ 保留 `or _reply_target` 这一路：不经过 UI 发送路径的调用方
        #    （测试替身、将来别的入口）仍然只设了 `_reply_target`。
        _reply_iid = (
            (getattr(self, "_reply_target_turn", None)
             or getattr(self, "_reply_target", None) or {}).get("iid") or ""
        )
        if _reply_iid and _reply_iid not in _live_ids:
            logger.info(
                f"[Interaction] 「回复这条」目标 {_reply_iid} 已不在未决清单里，"
                f"本轮不注入指向（UI 侧应同步复位）"
            )
            _reply_iid = ""

        return (
            # ⚠️ 这段措辞第一版有两个实测暴露出来的缺口（2026-08-05，）：
            #
            # ① **只提了"answers"，完全没提"取消"。** 用户说"算了不做了"时，
            #    模型在这段里找不到任何适用的指令，于是只回了一句"好的，取消了"、
            #    **没调工具**，待办留在原地。`relation=CANCEL` 明明存在，
            #    但它只写在工具描述里，而模型是照着这段动态段决定要不要调工具的。
            # ② 原文强调 "not blocking" / "do not nag" —— 那是为了防它过度热情，
            #    结果把它压成了**过度冷淡**："just handle their request normally"
            #    成了阻力最小的路。
            #
            # 现在把三种走向**并列写全**，并且明确"关掉它需要调工具，光说一句不算"。
            "\n\n[Open Interactions]\n"
            "Nano is waiting on the user for these. They do not block anything: "
            "the user may reply, may drop the whole thing, or may talk about something else.\n"
            "Three cases, and only the first two involve the tool:\n"
            "1. The message answers one of them (even while also changing the requirement) "
            "→ call answer_open_interaction with relation=ANSWER or ANSWER_AND_AMENDMENT.\n"
            "2. The message says they no longer want it ('forget it', 'never mind', "
            "'drop that skill') → call answer_open_interaction with relation=CANCEL. "
            "**Replying 'okay, cancelled' in text does NOT close it** — the item stays open "
            "and will keep showing up here until the tool records the cancellation.\n"
            "3. The message is about something else entirely → handle it normally, call no tool, "
            "leave the item open. Do not nag them about it.\n"
            # ⭐⭐⭐ [2026-08-06 实测] `[skill_audit]` 的语义必须写出来。
            #
            # 上面那三条是照着**澄清**写的（"Nano 在等你回答问题"）。对审计条目
            # 这个框架是错的：审计不是一个问题，**代码已经写完了在等批准**。
            #
            # 实测复现（19:37）：用户在有真代码的待审卡 int_9eeac9e476 上点了
            # 「回复这条」，然后说「你能不能目前修改这个代码」——
            # 指向是活的、是对的，`answer_open_interaction` 也在工具表里，
            # 模型照样走了 `create_new_skill`，并且原话说
            # **「到现在为止，我们只是澄清了需求，还没有生成任何代码」**。
            #
            # 它不是不听话，是**它看到的那一行只有一个不透明的标签 `[skill_audit]`**，
            # 头部还告诉它"这些是 Nano 在等用户回答的问题"。于是"改这个代码"、
            # "部署这个吧"、"继续"这些话，它认得的唯一出口就是 create_new_skill
            # → 每轮再造一个同名 Skill → 卡片越堆越多 → 死循环
            #   （最后堆了三张同名 GetComputerIP 的待审卡）。
            #
            # 📌 判据：**给模型一个状态标签，不等于给了它这个状态的语义。**
            #    枚举名对写代码的人是自解释的，对模型只是一个陌生字符串。
            + ("\n[About the skill_audit items above]\n"
               "Those are NOT questions — **the code is already written**. Nano finished a "
               "draft and it is sitting in the review window, waiting for the user's call. "
               "So for a skill_audit item:\n"
               "- ANSWER ('deploy it', 'go ahead', 'looks good', 'yes') → the draft is "
               "deployed as-is.\n"
               "- ANSWER_AND_AMENDMENT ('change X', 'use Y instead', 'can you modify this "
               "code', 'make it also return Z') → the draft is rewritten with that change. "
               "**This is how you modify pending code — you do not need to write it again.**\n"
               "- CANCEL ('forget it', 'drop that one') → the draft is discarded.\n"
               "⚠️ Do NOT call create_new_skill for a requirement that already has a "
               "skill_audit item open. That does not act on the existing draft — it produces "
               "a second draft under the same name, and the user ends up with a pile of "
               "near-identical review cards.\n"
               if _has_audit else "")
            # ⭐ 用户点了卡片上的「回复这条」→ 指向是**显式**的，不用猜。
            # 这与下面那段歧义消解互补：那条让模型猜得更准，这条让它根本不必猜。
            #
            # ⚠️⚠️ 必须先确认 `_reply_target` 指的那条**还活着**（`_reply_iid` 已过滤）。
            #
            # 2026-08-06 实测：用户在 17:26 点了澄清
            # int_192a0393ab 的「回复这条」，17:46:13 那条被草稿消化 → SUPERSEDED。
            # 但 `_reply_target` 只有用户手点 ✕ 才清，于是**之后每一轮**都注入
            # 「answering int_192a0393ab … do not pick a different one」，
            # 而下面的清单里根本没有它。
            #
            # 后果比"多一句废话"严重得多：17:58 用户说「部署这个吧」，模型被
            # **点名指向一条不存在的交互 + 明令禁止改挑别的** ——
            # `answer_open_interaction` 无路可走，于是退回 `create_new_skill`，
            # 新建了一个 Skill 而不是部署待审那个（用户报的最恶性那条）。
            #
            # 📌 判据：**指向性的注入必须校验指向的东西还在。**
            #    一个悬空的"必须回答 X"比没有这句话更糟 —— 它把模型逼进死角，
            #    而模型只能从别的工具里找出路。
            #
            # ⚠️ 这只是安全网。真正的修法是 UI 侧发完消息就复位（用户的 BUG5），
            #    两边都要有：目标也可能在选中期间被别人关掉。
            + (f"\n⭐ The user explicitly marked this message as answering "
               f"{_reply_iid} "
               f"— they clicked \"reply to this\" on that card. "
               f"Treat it as the answer to that item; do not pick a different one.\n"
               if _reply_iid else "")
            # ⭐⭐ 歧义消解（2026-08-06 实测）：不点名时该指哪一条。
            # 见上面 `_newest_id` 那段注释里 用户的理由。
            # ⚠️ 上面插了一个 `+ (条件表达式)` 之后，这里**必须继续用 `+`** ——
            # 裸字符串没法跟一个 `+` 表达式的结果做隐式拼接（那是语法错误）。
            + "\nIf several items are open and the user does not say which one "
            "('deploy it', 'go ahead', 'yes'):\n"
            "- The list is ordered newest first, so item 1 is the most recent. "
            "That one (also marked ← NEWEST) is almost always what they mean — "
            "they were just talking about it. Prefer it.\n"
            "- Asking which one is also fine, and is the better choice when the items are "
            "similar or when acting on the wrong one would be hard to undo.\n"
            "- **Never quietly pick the oldest one.** After a long conversation, 'deploy it' "
            "referring to something from twenty turns ago is not a reading a person would make.\n"
            + "\n".join(lines)
        )

    def _build_session_log_injection(self) -> str:
        """Format current-session operation records for system_guide injection.

        Return empty text when there is no session log.
        Limit each entry length to avoid bloating system_guide.
        """
        if not self._session_log:
            return ""
        lines = []
        for entry in self._session_log[-10:]:  
            if len(entry) > 120:
                entry = entry[:117] + "..."
            lines.append(f"  • {entry}")
        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Session Log]\n" + "\n".join(lines)

    def _build_episodic_injection(self) -> str:
        """Inject recent cross-session summaries from working memory into system_guide."""
        if self._wm is None:
            return ""
        try:
            rows = self._wm.get_recent_session_ends(limit=5)
        except Exception:
            return ""
        if not rows:
            return ""
        lines = []
        for r in rows:
            ts_short = r["ts"][5:16] if r.get("ts") else ""  # MM-DD HH:MM
            subject = r.get("subject", "")[:40]
            outcome = r.get("action", "")  # success/failed/chitchat/partial
            outcome_badge = {"success": "✓", "failed": "✗", "partial": "~", "chitchat": "💬"}.get(outcome, "")
            import json as _json
            try:
                skills = _json.loads(r.get("tags", "[]") or "[]")
            except Exception:
                skills = []
            skills_str = f" [Skills: {', '.join(skills)}]" if skills else ""
            lines.append(f"  • [{ts_short}]{(' ' + outcome_badge) if outcome_badge else ''} {subject}{skills_str}")
        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Recent Cross-Session Summaries]\n" + "\n".join(lines)

    # ⭐⭐⭐ [2026-08-25 实测] **把「窗口现在是什么形态」注入给模型。**
    #
    # 🔴 实测：Nano **每一个气泡都要调一次 `set_window_mode('mini')`**。
    #    查下来根因正如猜的那样 —— 全项目 grep「窗口当前是不是 mini」**零命中**，
    #    **从来没有任何地方告诉过模型这件事**。
    # 🔴 而 `look_at_screen` 的描述里那句是硬的：
    #      「call set_window_mode('mini') BEFORE your first look_at_screen
    #        in a task … **This one is not optional.**」
    #    ⇒ 「算不算 in a task」只能它自己猜，而 `wait_for` 之后是新的一轮、
    #      隔了一分钟 —— 猜不出来，按「not optional」就只能再调一次。
    # 📌 **一个「不许省」的强规则，配上一个模型看不见的状态，
    #    结果必然是每次都做。** 那不是它谨慎，是我们没给它判断的依据。
    #
    # ⭐ 这是「注入事实」这个模式的**第五个实例**：
    #      健康态 → 预算态 → 上下文压力 → 记忆索引 → 本条
    #    规律：**给模型注入它自己的状态，它就会自主调整行为。**
    #
    # ⚠️ 权威取 **租约**，不取 UI 的 `_mini_active` ——
    #    `_enter_mini` 那段留痕写着「`_mini_active` 从此**只是投影**，
    #    权威在租约里」。📌 读投影就是给自己造第二个权威。
    # ⚠️ fail-safe 方向：读不到 → 说「full」。
    #    说错成 full → 模型多缩一次窗（幂等，无害）；
    #    说错成 mini → 模型**跳过缩窗直接操作屏幕**，而它正挡着屏幕。
    #    📌 两边代价不对称时，往代价小的那边倒。
    # ⭐⭐⭐ **记忆摘要每轮无条件注入 —— 整个改造的核心。**
    #
    # 🔴 2026-08-05 的原话，根因当时就说对了：
    #    > cc 会在合适的时候召回记忆是因为 **cc 每次都注入了摘要**；
    #    > nano 只是很软地声明了「在 xx 时候必须先查看记忆再回答」。
    #    > **太模糊，nano 没办法先入为主地「预览到」记忆里大概有些什么。**
    # 📌 **一条召回不到的记忆，和一条没写过的记忆，对用户是同一个东西。**
    # ⭐ 沿用 的那句：**索引不是被回忆的，是被塞进来的。**
    # ⭐ 这是「注入事实」模式的第六个实例：
    #    健康态 → 预算态 → 上下文压力 → 记忆索引 →
    #    [窗口形态] → 本条。
    #
    # ═══ ⚠️ 保底（floor），不是配额（quota）—— 2026-08-24 定 ═══
    # 一度建议「N 写成配额，和 L0/L1/L2 共用水位」。用户否掉：
    #    「我没把握，因为如果特别小对这部分是致命的，**因为纠错本身是一等公民**」
    # 这个判断是对的：
    #    L0→L1→L2→L3 是**分辨率阶梯**，每一档都还携带着东西，降档 = 变模糊
    #    记忆     **没有半条**：要么完整生效，要么等于不存在
    # 📌 **配额是给「可以降分辨率的东西」用的。** 记忆降不了分辨率，
    #    进那套机制的唯一结果是「压力一大就被挤掉」——
    #    而压力大 ＝ 对话长 ＝ 用户投入最多的时刻。
    #
    # ⭐⭐ [2026-08-25] **连「保底 40 条」这个数字也拿掉了。**
    #    原话：「我们不数记忆的条数，我们数记忆导致的常驻 token 注入的消耗」。
    #    ⇒ **全部注入，不截断**；约束改成下面那套按 token 水位的提醒。
    # 📌 为什么：**一个「会积累的东西」的清除条件，应该来自它消耗的那个资源** ——
    #    一条 30 字的和一条 300 字的，条数上一样，成本差十倍。
    #    「40 条」是那个资源的**代理变量**，而代理变量在密度变化时就失效
    #    （这条判据是 L3→L4 那格定下来的，这里是它的第二次兑现）。
    # ⚠️ 于是这里**不再有任何常量** —— 阈值全在 `_MEM_WARN_ON/OFF` 那一段，
    #    而且那三个数是**算出来的**（见那段注释），不是拍的。


    # ══════════════════════════════════════════════════════════════════════
    # ⭐⭐⭐ **记忆的自治理：数 token，不数条数。**
    #
    # 已定形状：
    #   · 不设条数上限 —— 全部注入
    #   · 盯的是**这些记忆每轮真实花掉多少 token**
    #   · 越过阈值才注入一句话，让 Nano 在**这一轮自然收尾时**跟用户提一句，
    #     并且**自己先判断**哪些低价值/过时，但**不许擅自删**
    #
    # 📌 为什么数 token 不数条数：**一个「会积累的东西」的清除条件，
    #    应该来自它消耗的那个资源** —— 一条 30 字的和一条 300 字的，
    #    条数上一样，成本差十倍。（这条判据是 L3→L4 那格定下来的。）
    #
    # ═══ 两条线 + 只记「上次提醒时的水位」═══
    # 🔴 已明确两个问题，根子是同一个：**只有一条线的开关会在线附近抖动。**
    #    ① 用户不理会 → 每轮都提 → Nano 当成迫切问题
    #    ② 1200 提示 → 删到 900 → 又写回 1200 → 又提示（鬼畜）
    # ⭐ 修法：触发线与解除线分开，并且**只记上次提醒时的水位**，
    #    要再涨 50% 才提第二次。
    #      提醒条件： 水位 > WARN_ON  且  水位 > last_warned × 1.5
    #      提醒之后： last_warned ← 当前水位
    #      跌破 WARN_OFF： last_warned ← 0（重新武装）
    #
    # ⭐⭐ **我们从不判断用户答了什么。** 提醒的语义是「告诉你一声」，
    #    而告诉一次就够了；再涨一大截是**新的事实**，值得再说一次。
    #    📌 一旦要判断「用户同意还是拒绝」，就得做自然语言理解 ——
    #       而那正是本项目明确否过的方向（「不许让代码去理解『算了别点了』」）。
    #    ⇒ 用户越不在意，提醒频率**指数级变稀**（1200 → 1800 → 2700 → …）。
    #
    # ═══ 阈值是**算出来的**，不是拍的═══
    #   注入格式 `  - {英文 summary_model}  [when: {英文 when}]`
    #   四条真实样例实测（`meter.estimate_text`）：平均 **20.2 token/条**，表头 27。
    #     20 条 ≈  432    40 条 ≈  837    60 条 ≈ 1242    80 条 ≈ 1647
    #   ⇒ WARN_ON 1200 ≈ **58 条**：58 条之前一句不提
    #     忽略一次后 1800 ≈ **88 条**：再攒 30 条才提第二次
    #     WARN_OFF  800 ≈ **38 条**：要真清理到 40 条以内才重新武装
    # ⚠️ 英文摘要 ≈20 token/条，同内容中文 ≈38 —— 强制 `summary_model` 写英文
    #    这一条本身就把水位压掉了一半。
    # ══════════════════════════════════════════════════════════════════════
    _MEM_WARN_ON = 1200          # 触发线（token）≈ 58 条
    _MEM_WARN_OFF = 800          # 解除线（token）≈ 38 条
    _MEM_WARN_GROWTH = 1.5       # 再涨这么多倍才提第二次

    def _mem_water_path(self):
        import pathlib as _pl
        from core.paths import data_path
        return data_path("memory_water.json")

    def _mem_water_read(self) -> float:
        """上次提醒时的水位。读不到当 0（= 未提过）。

        ⚠️ fail-safe 方向是 **0**：读不到就当没提过，最多多提一次；
           反过来（当成提过）会让一个真的涨上去的水位**永远不提**。
        """
        try:
            import json as _j
            return float(_j.loads(self._mem_water_path().read_text("utf-8"))
                         .get("last_warned_at", 0) or 0)
        except Exception:
            return 0.0

    def _mem_water_write(self, v: float) -> None:
        try:
            import json as _j
            _p = self._mem_water_path()
            _p.parent.mkdir(parents=True, exist_ok=True)
            _p.write_text(_j.dumps({"last_warned_at": round(float(v), 1)}), "utf-8")
        except Exception as e:
            logger.debug(f"[O2] 水位留痕失败（不影响本轮）: {e}")

    def _mem_water_notice(self, injected: str) -> str:
        """按注入内容算水位，越线就返回那句提醒；否则空串。

        ⚠️ 算的是**真实注入出去的那段文本**，不是估的条数 ——
           📌 要管一个资源，就得量它本身，不能量它的代理变量。
        ⚠️ 整段吞异常：📌 治理是家务，不该有能力让这一轮失败。
        """
        try:
            if not injected:
                return ""
            from core.context.meter import estimate_text as _est
            _now = float(_est(injected))
            _last = self._mem_water_read()
            if _now < self._MEM_WARN_OFF and _last:
                self._mem_water_write(0)          # 真清理过 → 重新武装
                return ""
            if _now <= self._MEM_WARN_ON or _now <= _last * self._MEM_WARN_GROWTH:
                return ""
            self._mem_water_write(_now)
            logger.info(f"[O2] 记忆注入水位 {_now:.0f} token 越线（上次 {_last:.0f}）→ 提醒一次")
            # ⚠️ **不列候选**：那句提醒本身就是为了省 token，
            #    它自己很长就矛盾了。而 Nano 手上已经有全部摘要，
            #    足够做一轮浅判断；真要确认某条是否过时，再去 recall 全文。
            # ⚠️ 明说要先 load `forget_user_note` —— 📌 否则会重演 computer_use
            #    那个坑：告诉它去用一个它手上没有的工具。
            #
            # 🔴🔴 [2026-08-25 实测] 第一版写的是
            #    「Finish what you are doing first. Then, when this turn **wraps up
            #      naturally**, mention it to them」—— **它一个字都没提。**
            #    日志显示提醒确实注进去了（dyn 7713 → 下一轮 6373），是模型没照做。
            # 📌 根因是改动了用户的措辞：原话是「**你在你的下一条消息当中**，
            #    应该向用户给出建议」，被软化成了「自然收尾时顺带提一句」。
            #    **一条明确的指令被软化成了一条建议，然后它就被当成建议对待了。**
            # ⚠️ 软化的动机是「别劫持当前任务」—— 但那个担心该靠**位置**解决
            #    （答完用户再说），不该靠**削弱语气**解决。两者被混成了一件事。
            # ⚠️ 而对着一句「你好」，「自然收尾时」几乎等于没说：这一轮没什么要收尾的。
            # ⭐ 现在：**这一轮必须说**（明确），但**放在回答之后**（不劫持）。
            #    并且如实告诉它「跳过就没有下一次了」—— 📌 一个不说明代价的指令，
            #    模型没有理由把它排在别的事情前面。
            return (
                f"\n\n[Memory cost - act on this in THIS reply]\n"
                f"The memories above now cost about {_now:.0f} tokens on every single "
                f"turn. Nothing is broken; the user just deserves to know.\n"
                f"Answer the user normally first. Then, at the END OF THAT SAME REPLY, "
                f"add a short paragraph that: says roughly what the memories cost per "
                f"turn; names the ones you think have gone stale or low-value (you can "
                f"see them all above - judge for yourself, do not make the user audit "
                f"the list); and offers to delete those for them.\n"
                f"To actually delete, load forget_user_note first - it is not attached "
                f"by default. Delete only what they agree to; never on your own.\n"
                f"Do NOT skip this and do NOT save it for a later turn: this notice "
                f"appears once and will not come back until the cost grows a lot more. "
                f"If they say the cost is fine, that is a perfectly good answer."
            )
        except Exception as e:
            logger.debug(f"[O2] 水位判断跳过: {e}")
            return ""

    def _build_memory_injection(self) -> str:
        """把已确认记忆的 `summary_model` 一行行塞进动态段。

        ⚠️ 用 `summary_model` **不是** `summary_user` —— 两句话是分开写的：
           📌 一句话同时服务两个受众，最后两边都不合身（这个坑踩过）。
        ⚠️ 带上 `applies_when`：📌 一条不说「什么时候用得上」的记忆，
           模型只能每条都掂量一遍；把作用域摆在旁边，它才能一眼跳过不相关的。
        ⚠️ 整段吞异常并返回空串：📌 记忆是**增益**，读不到不该让这一轮失败。
        """
        try:
            if self._wm is None:
                return ""
            rows = self._wm.get_all_confirmed_notes()
        except Exception as e:
            logger.debug(f"[O2] 读记忆失败（本轮不注入）: {e}")
            return ""
        if not rows:
            return ""
        lines = []
        # ⚠️ **不设条数上限**：📌 要管一个资源就得量它本身 ——
        #    一条 30 字的和一条 300 字的，条数上一样，成本差十倍。
        #    真正的闸是下面那个**按 token 水位**的提醒。
        for r in rows:
            _s = (r.get("summary_model") or r.get("detail") or "").strip()
            if not _s:
                continue
            _w = (r.get("applies_when") or "").strip()
            # 🔴 [2026-08-25 实测] 这一行原来**不带 id** —— 而 `forget_user_note`
            #    要的正是 id。实测里 Nano 判断出「这条已经过时该删」（完全正确），
            #    然后**只能猜**，猜了 1 和 2，两次都 "No memory with id N was found"。
            # 📌 **我们让它做一件事，却没给它做这件事需要的东西** ——
            #    跟 `look_at_screen` 只给散文不给坐标是同一个形状。
            #    ⚠️ 而工具描述里还写着「by the id shown next to it」，
            #       **根本没有任何地方 shown** —— 一句指向空气的描述。
            # ⚠️ 代价：编号约 4 字符 ≈ 1~2 token/条，可以忽略。
            lines.append(f"  - #{r.get('id')} {_s}" + (f"  [when: {_w}]" if _w else ""))
        if not lines:
            return ""
        _block = ("\n\n[What you remember about this user]\n"
                  "These are already in front of you - you do NOT need to look them up.\n"
                  + "\n".join(lines))
        # ⭐ 水位按**真实注入出去的那段文本**算，不按条数估。
        return _block + self._mem_water_notice(_block)

    def _build_window_mode_injection(self) -> str:
        try:
            from core.runtime import oslease as _ol_wm
            from core.runtime.kernel import get_kernel as _gk_wm
            _mini = bool(_ol_wm.gui_session_active(_gk_wm()))
        except Exception:
            _mini = False
        if _mini:
            return ("\n\n[Window] Nano's own window is CURRENTLY MINI (small, "
                    "top-right corner) — you already shrank it. Do NOT call "
                    "set_window_mode('mini') again; it is already done.")
        return ("\n\n[Window] Nano's own window is CURRENTLY FULL SIZE. If you are "
                "about to look at or operate the screen, shrink it first with "
                "set_window_mode('mini').")

    def _build_ambient_injection(self) -> str:
        """Ambient Memory Phase 1: inject a lightweight rule-based activity snapshot.

        Nano can use this to resolve references such as "this", "that thing",
        or "what I was just doing" without extra model calls.
        This uses only context-level signals such as app name, window title,
        activity rhythm, idle time, saves, and background audio. It does not read private content.
        Nano's own window is excluded because the real ambient context is usually the window
        the user was using before switching to Nano.
        """
        try:
            from core.proactive.activity import get_buffer
            from core.os_layer.executor_low import _is_self_window_title
            snap = get_buffer().snapshot()
        except Exception:
            return ""
        now = snap.get("now", time.time())
        windows = snap.get("windows", [])
        keys = snap.get("keys", [])
        saves = snap.get("saves", [])

        def _self(w) -> bool:
            try:
                return _is_self_window_title(getattr(w, "window_title", "") or "")
            except Exception:
                return False

        # Use all focus events as time boundaries, including Nano windows.
        # Nano windows are excluded from reporting, but they still provide accurate duration boundaries.
        all_focus = sorted([w for w in windows if getattr(w, "event", "") == "focus"],
                           key=lambda w: w.ts)
        if not all_focus and not keys:
            return ""

        def _dur(sec: float) -> str:
            m = int(sec // 60)
            return f"about {m} min" if m >= 1 else "less than 1 min"

        dwell: dict = {}
        last_real = None
        app_title: dict = {}
        for i, w in enumerate(all_focus):
            end = all_focus[i + 1].ts if i + 1 < len(all_focus) else now
            dur = max(0.0, end - w.ts)
            if _self(w):
                continue
            app = (w.process_name or "").replace(".exe", "").strip() or "Unknown"
            dwell[app] = dwell.get(app, 0.0) + dur
            app_title[app] = w.window_title or ""
            last_real = w
        cur = last_real
        span_min = int((now - all_focus[0].ts) / 60) if all_focus else 0
        top = sorted(dwell.items(), key=lambda kv: -kv[1])

        char_n = sum(1 for k in keys if getattr(k, "key", "") == "char")
        bs_n = sum(1 for k in keys if getattr(k, "key", "") == "backspace")
        last_act = max([k.ts for k in keys] + [w.ts for w in all_focus] + [0.0])
        idle_sec = now - last_act if last_act else 9999

        # Interpretation layer: app category + title parsing + restrained activity guess.
        _cat = _ambient_cat
        _parse_title = _ambient_parse_title
        cur_app = (cur.process_name or "").replace(".exe", "").strip() if cur else ""
        dom_app = top[0][0] if top else cur_app
        dom_cat = _cat(dom_app)
        f_name, proj, page = _parse_title(cur_app, cur.window_title if cur else "")
        n_switch = sum(1 for w in all_focus if not _self(w))

        dom_url = (snap.get("url_domain") or "").strip()
        guess = ""
        if dom_cat == "editor":
            guess = ("looks like editing/debugging code" if (char_n >= 60 and bs_n * 3 >= char_n)
                     else "looks like writing code" if char_n >= 60 else "looks like reading code")
        elif dom_cat == "browser":
            if dom_url:
                guess = f"looks like browsing {dom_url}"
            else:
                guess = "looks like researching or looking for something" if n_switch >= 4 else "looks like reading a webpage"
        elif dom_cat == "im":
            guess = "looks like chatting or communicating"
        elif dom_cat == "terminal":
            guess = "looks like running commands or debugging"
        if not guess and char_n == 0 and idle_sec > 180:
            guess = "was idle for a while and may have just returned"

        import datetime as _dt2
        parts = []
        if cur:
            since = now - cur.ts
            ago = "was just in" if since < 90 else f"was in about {int(since // 60)} min ago"
            tgt = cur_app
            if f_name:
                tgt += f" ({f_name[:40]})"
            elif page:
                tgt += f" ({page[:40]})"
            lead = f"{ago} \"{tgt}\""
            # ⭐⭐ **「刚才」那一条往往根本不在 trail 里。**
            #    `recent()` 刻意 `exclude_last_sec=600`，而 trail 每 4 分钟才写一次 ——
            #    用户在记事本改完切过来就问，命中的是**实时 buffer**，不是 trail。
            #    📌 只给 trail 加句柄的话，那个最常见的场景一条都拉不到。
            _cur_ref = getattr(cur, "ref", None) or {}
            if _cur_ref.get("path"):
                lead += " ▸" + _dt2.datetime.fromtimestamp(cur.ts).strftime("%H:%M:%S")
            if guess:
                lead += f", {guess}"
            parts.append(lead)
        elif guess:
            parts.append(guess)

        if top:
            def _seg_one(a, s):
                # Browser includes page title; editor includes file name when available.
                fn, pj, pg = _parse_title(a, app_title.get(a, ""))
                detail = fn or pg
                d = f" {detail[:36]}" if detail else ""
                return f"{a}{d} ({_dur(s)})"

            seg = "; ".join(_seg_one(a, s) for a, s in top[:4])
            head = f"over the last about {span_min} min, mainly in" if span_min >= 1 else "recently, mainly in"
            parts.append(f"{head}: {seg}")

        if saves:
            # ⭐ 这里原来直接甩**原始完整窗口标题**，是全局
            #    唯一一处没过 `_parse_title` 的地方 —— 它反而会漏出
            #    `xxx.txt - 记事本` 这种带文件名的串。
            # 📌 **歪打正着 ≠ 可控**：改完 ① 之后，同一个文件会在同一段话里
            #    出现两种写法（`notepad (a.txt)` 和 `saved a file (a.txt - 记事本)`），
            #    而模型没有理由知道它俩是一个东西。⇒ 两条对齐到同一个解析器。
            # ⚠️ 解析不出来时**退回原始标题**，不是退回空 —— 一个不认识的应用，
            #    完整标题仍然比什么都没有强。
            _sv = saves[-1]
            _sf, _, _sp = _parse_title(
                (_sv.process_name or "").replace(".exe", "").strip(),
                _sv.window_title or "")
            st = (_sf or _sp or (_sv.window_title or "")).strip()
            parts.append("recently saved a file" + (f" ({st[:40]})" if st else ""))

        audio = list(snap.get("audio_apps") or [])
        if audio:
            parts.append("background audio: " + ", ".join(audio[:2]))

        # Persistent trail from earlier today, outside the short live buffer.
        try:
            from core.proactive import ambient_trail
            import datetime as _dt
            tr = ambient_trail.recent(hours=12, limit=6)
            segs, prev = [], None
            for r in tr:
                ln = r.get("line", "")
                if ln and ln != prev:
                    # ⭐ 精度提到**秒**：这个时刻同时是拉取时的 id。
                    # 🔴 **绝不能用序号** —— trail 是滚动的（18h/300 条）且每 4 分钟
                    #    新增一条，注入取的是最后 6 条：
                    #      第 N 轮模型看到「#3」→ 第 N+1 轮它调工具要 #3 → 已经不是那条了。
                    #    📌 **一个会在两次调用之间漂的标识，漂了没人知道。** 时刻不漂。
                    hhmm = _dt.datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
                    _mark = " ▸" if (r.get("ref") or {}).get("path") else ""
                    segs.append(f"{hhmm} {ln}{_mark}")
                    prev = ln
            if segs:
                parts.insert(0, "earlier today: " + "; ".join(segs))
        except Exception:
            pass

        if not parts:
            return ""

        # 固定使用说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发数据。
        return "\n\n[Ambient]\n" + "; ".join(parts) + "."

    def _ambient_scene_now(self):
        """当前现场的一行摘要 —— **起返回 `(line, ref)`**。

        ⚠️ `ref` 取的是「停留最久那个应用**最后一条**焦点记录」上的句柄：
           dwell 决定的是「这段时间主要在哪」，而句柄必须落在**具体某一次**
           焦点上。📌 一个按时长聚合出来的对象，不能带一个按次记录的地址 ——
           除非明确说清取的是哪一次。
        """
        try:
            from core.proactive.activity import get_buffer
            from core.os_layer.executor_low import _is_self_window_title
            snap = get_buffer().snapshot()
        except Exception:
            return ("", None)
        now = snap.get("now", time.time())
        windows = snap.get("windows", [])
        keys = snap.get("keys", [])
        all_focus = sorted([w for w in windows if getattr(w, "event", "") == "focus"],
                           key=lambda w: w.ts)
        dwell, titles, refs = {}, {}, {}
        for i, w in enumerate(all_focus):
            end = all_focus[i + 1].ts if i + 1 < len(all_focus) else now
            try:
                if _is_self_window_title(getattr(w, "window_title", "") or ""):
                    continue
            except Exception:
                pass
            app = (w.process_name or "").replace(".exe", "").strip() or "Unknown"
            dwell[app] = dwell.get(app, 0.0) + max(0.0, end - w.ts)
            titles[app] = w.window_title or ""
            if getattr(w, "ref", None):
                refs[app] = w.ref            # 后来的覆盖先前的 = 最后一次
        if not dwell:
            audio = snap.get("audio_apps") or []
            return (("background audio: " + audio[0]), None) if audio else ("", None)
        dom = max(dwell.items(), key=lambda kv: kv[1])[0]
        cat = _ambient_cat(dom)
        fn, pj, pg = _ambient_parse_title(dom, titles.get(dom, ""))
        char_n = sum(1 for k in keys if getattr(k, "key", "") == "char")
        bs_n = sum(1 for k in keys if getattr(k, "key", "") == "backspace")
        act = ""
        if cat == "editor":
            act = "editing code" if (char_n >= 60 and bs_n * 3 >= char_n) else "writing code" if char_n >= 60 else "reading code"
        elif cat == "browser":
            act = "reading a webpage"
        elif cat == "im":
            act = "communicating"
        elif cat == "terminal":
            act = "running commands"
        detail = fn or pg
        line = f"in {dom}"
        if act:
            line += f" {act}"
        if detail:
            line += f" ({detail[:30]})"
        return (line, refs.get(dom))

    def record_ambient_trail(self) -> None:
        """由 app.py 定时器周期调用：把当前现场追加进持久轨迹（Phase 4）。"""
        try:
            from core.proactive import ambient_trail
            line, ref = self._ambient_scene_now()
            if line:
                ambient_trail.append(line, ref)
        except Exception:
            pass

    # ── 按需加载工具：匹配 + 感知文本 ────────────────────────────────────────

    # ⭐ 少数工具的感知行**不许截断**。
    #
    # `_AWARENESS_FULL` 里这几个的共同点：**模型必须在"要不要用它"这个判断上
    # 拿到足够信息**，而 28 字符连一句话都装不下。
    #
    # `create_new_skill` 是最关键的一个 —— ⑥ 删掉关键词快路径之后，
    # "写个 skill 做 XX" **唯一的入口就是它**。而元工具要求模型判断
    # "可复用能力 vs 一次性执行"；如果它连这个工具是干什么的都看不清，
    # 就更容易把一个明确的请求劝退，而劝退之后用户没有替代通道
    # （临时执行通道还不存在）。
    # **这正是快路径当初被留下的唯一理由，所以删它之前必须先把这条补上。**
    #
    # ⚠️ 不是"把所有工具的截断都放宽"（那会让感知块膨胀）——
    # 只对判断成本高的少数几个给全文第一段。的完整修法（`awareness` 字段）
    # 仍然并进，这里是它的**前置局部处置**。


    def record_session_end(self, user_query: str, assistant_reply: str) -> None:
        """每轮 final_result 后由 app.py 调用，把本轮摘要写入 working_memory。

        存储字段：
          subject  = 用户说了什么（60字）
          action   = outcome: success / failed / chitchat
          detail   = 回复摘要 ｜ 操作列表
          tags     = 涉及的 Skill 名列表（artifact 索引）
        """
        if self._wm is None:
            return
        try:
            from core.memory_store import EntryType
            import re as _re

            subject = user_query.strip()[:60] + ("…" if len(user_query.strip()) > 60 else "")

            # ── outcome 推断 ──────────────────────────────────────────────
            ops = [e for e in self._session_log if e]
            has_ops = bool(ops)
            log_text = " ".join(ops)
            log_text_l = log_text.lower()
            if not has_ops:
                outcome = "chitchat"
            elif (
                any(w in log_text_l for w in ("failed", "error", "exception", "cancelled"))
                or any(w in log_text for w in ("失败", "错误"))
            ):
                outcome = "failed"
            elif (
                any(w in log_text_l for w in (
                    "success", "deployed", "updated", "called", "executed",
                    "written", "completed", "created", "hot-loaded"
                ))
                or any(w in log_text for w in ("成功", "已部署", "已调用", "已执行", "已写入", "已完成"))
            ):
                outcome = "success"
            else:
                outcome = "partial"

            # Extract involved Skill names from session_log.
            skill_names = []
            for e in ops:
                for _pat in (
                    r"\[Skill (?:deployed|updated|call|called|deploy|update)\]\s+(\S+)",
                    r"Skill(?:调用|部署|更新)[^\]]*?[：:]\s*(\S+)",
                ):
                    m = _re.search(_pat, e)
                    if m:
                        skill_names.append(m.group(1))
                        break
            skill_names = list(dict.fromkeys(skill_names))

            # ── detail ───────────────────────────────────────────────────
            reply_snippet = assistant_reply.strip()[:80] + ("…" if len(assistant_reply.strip()) > 80 else "")
            ops_str = " | ".join(ops[-3:]) if ops else ""
            detail = reply_snippet + (f" | ops: {ops_str}" if ops_str else "")

            self._wm.add(
                entry_type=EntryType.SESSION_END,
                subject=subject,
                action=outcome,
                detail=detail,
                tags=skill_names or [],
                session_id=self._wm_session_id,
            )
        except Exception as e:
            logger.warning(f"[Episodic] record_session_end 失败: {e}")

    def cancel_pending_skill(self, filename: str | None = None) -> dict:
        # 同 apply：不传就是最近那条，传了就精确丢那一条。
        _sel = self._get_pending_skill(filename)
        name = (_sel or {}).get("filename") or filename or "未知"
        # 丢弃时把 outcome 落定。app 侧那条 `[System record: ...discarded...]` memory 消息
        # 会被 max_turns=10 切掉，而这里是实例状态 + 动态段，不受截断影响 ——
        # 这是"用户点了丢弃，模型下轮还知道"的唯一可靠通道。
        _af = getattr(self, "_last_audit_failure", None)
        if isinstance(_af, dict) and _af.get("filename") == name:
            _af["outcome"] = "discarded"
        elif _sel is not None:
            # 校验通过但用户仍然丢弃（不喜欢这个实现 / 改主意了）。
            # 没有报错可讲，但"被丢弃"这件事本身模型也该知道，否则它会以为部署了。
            self._last_audit_failure = {
                "filename": name, "mode": _sel.get("mode", "create"),
                "errors": [], "code_hash": "", "code_lines": 0,
                "outcome": "discarded", "at": time.time(),
            }
        self._drop_pending_skill(name)
        # ⚠️ 只有全部清空了才重置时间戳 —— 否则还剩别的待审时，
        # `_expire_stale_pending` 会拿一个被清零的时间戳去判超时。
        if not self._pending_skills:
            self._pending_skill_at = 0.0
        # 同上：UI 的「丢弃」按钮走这里，交互必须一起关掉。
        from core.runtime import interaction as _it_r
        _rt_close_skill_audit(name, approved=False,
                              reason=_it_r.Resolution.USER_CANCELLED)
        return {"ok": True, "msg": f"已丢弃 Skill 「{name}」"}

    def reset_conversation(self) -> dict:
        """Step 4：用于 UI"重置当前对话"按钮。

        清空 memory 历史 + 清空所有 pending 状态。Skill 文件、知识库索引、registry 不动。
        """
        had_memory = len(self.memory) > 0
        had_pending_skill = self._pending_skill is not None
        had_pending_action = self._pending_action is not None
        # 这才是唯一的新会话入口。普通 reset() 只清内存投影，不能用于
        # 用户明确要求的「重置对话」，否则重启后旧记录会又回来。
        self.memory.reset_conversation()
        self._pending_skill = None
        self._pending_skill_at = 0.0
        self._pending_action = None
        self._pending_action_at = 0.0
        # 取代 `self._pending_skill_clarification = None`。
        # 未决交互现在在 SQLite 里，清空内存字段清不掉它们。
        _cancelled_interactions = _rt_cancel_all_interactions("重置对话")
        self._rag_hit_this_turn = False
        self._full_file_hit_this_turn = False
        self._session_log = []  # 重置会话 log
        self._pending_skill_description_cache = ""
        self._last_deployed_skill = None
        self._last_called_skill = None
        self._last_mentioned_skill = None
        self._last_os_locate_failure = None
        self._last_skill_error = None     # 结构化错误状态：{skill, error, consumed}
        # 审计失败记录也要清：重置对话的语义是"忘掉这段"，留着它会让新对话
        # 一开口就带着上一段的失败上下文（而用户已经把那段抹掉了）。
        self._last_audit_failure = None
        self._last_spec_errors = []
        # OS 预授权的语义是"【本次对话】始终允许"，所以重置对话必须一起清掉，
        # 否则那个 scope 名不副实（safety.reset() 之前从来没有调用方）。
        if hasattr(self, "_os_safety"):
            try:
                self._os_safety.reset()
            except Exception as _e:
                logger.warning(f"[Reset] 清理 OS 预授权失败（不影响重置）: {_e}")

        logger.info(
            f"[Reset] 对话已重置（memory={had_memory}, "
            f"pending_skill={had_pending_skill}, pending_action={had_pending_action}, "
            f"interactions={_cancelled_interactions}）"
        )
        return {"ok": True, "msg": "已重置当前对话。历史和待处理状态已全部清空。"}

    # ── 工具清单 / 辅助 ─────────────────────────────────────────────────



    @staticmethod
    def _skill_names(skills_info: list) -> list:
        names = []
        for s in skills_info:
            if isinstance(s, dict):
                names.append(s.get('name'))
        return [n for n in names if n]

    async def _emit_skill_preview_from_decision(self, decision, used_model: str, mode: str = "create",
                                                  target_skill: str | None = None, change_summary: str = "",
                                                  spec_side_effects: list | None = None,
                                                  error_context: str = ""):
        filename    = decision.args.get("filename", "UnknownSkill")
        code        = decision.args.get("code", "")
        description = decision.args.get("description", "暂无描述")
        # 把 spec_side_effects 传入,启用 AST 副作用一致性校验
        ok, errors = self.validate_skill_code(code, spec_side_effects=spec_side_effects)

        # ⭐ 撞 max_tokens 被截断 → 代码残缺，协议错误全是**症状不是成因**（实测）
        #
        # 那次生成一个 7 项功能的 Windows 网络配置 Skill，`output=8192`（正好等于上限），
        # 响应中途被切断，`code` 参数是空的。审计窗口于是报出 7 条
        # "缺少 import / 缺少类定义 / 缺少 get_spec() / run() 必须返回 SkillResult …" ——
        # **每一条都对，但每一条都指错了方向**：用户看到的是"模型不懂协议"，
        # 真相是"模型话没说完"。照着这些错误去改需求、改提示词，全是白功。
        #
        # 这与 //审计失败反馈是同一条判据：**报错要正确且充分。**
        # 技术上正确但把人引向错误结论的报错，比没有报错更糟。
        if getattr(decision, "truncated", False):
            _n_lines = len((code or "").splitlines())
            logger.error(
                f"[SkillWriter] ⚠️ 代码生成撞 max_tokens 被截断（拿到 {len(code or '')} 字符 / "
                f"{_n_lines} 行）。下面那些协议错误是截断的症状，不是模型不懂协议。"
            )
            errors = [
                "代码生成中途被截断（撞到单次输出上限），拿到的是残缺内容。"
                "下面列出的协议问题都是截断造成的，不是模型写错了。",
                f"（实际收到 {len(code or '')} 字符 / {_n_lines} 行）",
                "建议：让它把这个 Skill 拆小一些，或分成更少的功能点重新生成。",
            ] + errors
            ok = False

        # 从代码里提取 lifecycle 声明,决定 apply 时写哪个目录
        _lifecycle = "permanent"
        try:
            import re
            _lc_match = re.search(r'lifecycle\s*=\s*Lifecycle\.(\w+)', code)
            if _lc_match:
                _lifecycle = _lc_match.group(1).lower()
        except Exception:
            pass

        # 重名碰撞检测提前到审计窗口生成的这一刻，而不是等用户点了
        # "验证并应用"才在 apply_pending_skill 里拒绝——那样审计窗口已经
        # 显示"通过协议校验，可以部署"的绿色提示，用户点了才被告知不能部署，
        # 体验上是误导的。这里让校验状态本身就如实反映"这个不能部署"。
        if mode != "update":
            _collision_path = os.path.join("skills", f"{filename}.py")
            if os.path.exists(_collision_path):
                ok = False
                errors = errors + [
                    f"已存在同名永久 Skill「{filename}」，部署会覆盖现有文件，已拒绝。"
                    f"如果是想修改这个已有 Skill 请明确说'修改 {filename}'，"
                    f"如果是想新建请换一个不同的名字。"
                ]
        validation_summary = "✔ 代码通过协议 v3.2 全部校验" if ok else ("⚠ 存在问题: " + " | ".join(errors))

        # ── 审计校验失败 → 记下来给模型看（2026-08-05，实测）──────────
        #
        # 改造前 `errors` 只走到 `skill_preview` 事件 → `validation_lbl.set_text()`，
        # **纯 UI**。于是审计窗口拦下一个 Skill、用户点了丢弃之后：
        #   · 那段代码从来没进过 memory（`skill_preview` 的 code 只给弹窗渲染）
        #   · 校验报错一个字都没到模型手里
        #   · 唯一的"已丢弃"痕迹是 app 侧写的一条 memory 消息，而它会被 max_turns=10 切掉
        # 三条叠起来，模型只能问"哪个 Skill 出问题了？""我需要先检查它的代码看看哪里报错"
        # —— 而那个 Skill 压根没部署，它查不到。这不是它笨，是它真的什么都不知道。
        #
        # ⚠️ 放在**实例状态 + 动态段**而不是 memory：memory 会被截断（那正是 A 那条缺陷），
        # 动态段每轮从状态重建，天生不受截断影响。同 `[Open Interactions]` 的做法。
        if not ok:
            self._last_audit_failure = {
                "filename": filename,
                "mode": mode,
                "errors": list(errors),
                # 不存代码本身：模型重写时需要的是"这些协议点我上次漏了"，
                # 不是"我上次那 200 行长什么样"。指纹足够让它确认是不是同一版。
                "code_hash": hashlib.sha256(
                    (code or "").encode("utf-8", "replace")).hexdigest()[:12],
                "code_lines": len((code or "").splitlines()),
                "outcome": "awaiting",   # awaiting → discarded / deployed
                "at": time.time(),
            }
            logger.info(
                f"[SkillWriter] 审计校验失败已登记给模型可见: {filename} "
                f"({len(errors)} 条) —— 用户丢弃后模型仍能说清哪里不合协议"
            )

        # **插入**而不是整体赋值 —— 覆盖会让上一条待审的代码正文
        # 无声消失，而它的 Interaction 仍然自称 OPEN（见 __init__ 里的说明）。
        self._put_pending_skill({
            "mode": mode,
            "target_skill": target_skill or filename,
            "change_summary": change_summary,
            "filename": filename,
            "code": code,
            "description": description,
            "valid": ok,
            "errors": errors,
            "lifecycle": _lifecycle,                    # 新增
            # ⚠️ **保持 None，不要 `or []`**（2026-08-05 实测）。
            # 原来这里写 `spec_side_effects or []`，把"SkillSpec 阶段没产出声明"（None）
            # 悄悄变成了"声明了没有副作用"（[]）。两者语义完全不同，而 AST 一致性校验
            # 正是按这个区分工作的：None → 回退到读代码自己的声明；[] → 直接按"声明无副作用"比对。
            # 于是审计窗口（拿 None，回退读代码，一致 → 放行）与部署（拿 []，比错对象 → 拒绝）
            # 对同一段代码给出相反结论。
            # 两个读取方都能吃 None：下面 `is_os_skill` 有 `or []`，校验则按上面的回退走。
            "spec_side_effects": spec_side_effects,  # 供执行前副作用确认
            "error_context": error_context,             # 持久语义记忆v3:非空=这次update是报错修复,
                                                          # apply成功后据此触发correction写入
            "at": time.time(),   # 每条自带时间戳，超时逐条判
        })
        self._pending_skill_at = time.time()
        # 同时登记成一条 Interaction：跨重启存活 + 模型看得见 + artifact 钉版本。
        # `_pending_skill` 仍是**工作载荷**（代码正文等，那些不该进 SQLite），
        # Interaction 是**待办事实**。两者职责不同，不是双权威。
        self._rt_audit_iid = _rt_open_skill_audit(
            self, filename, description, code, mode, ok, errors)
        yield {
            "event": "skill_preview",
            "filename": filename,
            "code": code,
            "description": description,
            "validation_ok": ok,
            "validation_summary": validation_summary,
            "validation_errors": errors,
            "status": "AWAITING_APPROVAL",
            "log": f"Skill 「{filename}」已{'修改' if mode == 'update' else '生成'}（{'通过校验' if ok else '有问题，请审查'}），等待审批...",
            "model": used_model,
            "current_skill": "SkillWriter"
        }

    async def _generate_skill_spec(self, query: str, status_callback) -> dict | None:
        """SkillWriter 第一段：生成并校验 SkillSpec。

        返回:
          - 通过校验的 spec_dict(含 _validated=True)
          - 或 None(生成失败或校验失败,已 yield 错误事件)

        注意:本方法是 async 普通函数,不是 generator。
        调用方 _generate_skill_with_writer 负责 yield 事件。
        """
        from core.schema import SkillSpec, InputDef

        # ⚠️ 进门先清空。本方法有三条 `return None`（JSON 解析失败 / spec 构建异常 /
        # hard_validate 失败），只有最后一条会写 `_last_spec_errors`。
        # 不在入口清的话，前两条会让**上一次的旧报错**被当成本次的注给模型 ——
        # 那比不给信息更糟：它会去修一个这次根本没犯的错。
        self._last_spec_errors = []

        spec_data = await self.provider.generate_skill_spec(query, status_callback)
        # ⚠️ None 守卫：当前的 provider 实现总返回 dict（`result` 或 `{"error": ...}`），
        # 所以这条现在走不到 —— 但少了它，任何将来返回 None 的路径都会在这里抛
        # AttributeError，**整轮直接崩掉，而不是走调用方设计好的"降级为直接代码生成"**。
        # 失败模式的差别很大：一个是体验降级，一个是内核故障弹窗。
        if not isinstance(spec_data, dict) or spec_data.get("error"):
            if spec_data is None:
                logger.warning("[SkillWriter] provider.generate_skill_spec 返回 None，按生成失败处理")
            return None

        # 把 JSON 转成 SkillSpec 对象做 hard_validate
        try:
            required_inputs = [
                InputDef(
                    name=i.get("name", ""),
                    type=i.get("type", "string"),
                    description=i.get("description", ""),
                )
                for i in (spec_data.get("required_inputs") or [])
            ]
            optional_inputs = [
                InputDef(
                    name=i.get("name", ""),
                    type=i.get("type", "string"),
                    description=i.get("description", ""),
                )
                for i in (spec_data.get("optional_inputs") or [])
            ]
            spec = SkillSpec(
                name=spec_data.get("name", ""),
                purpose=spec_data.get("purpose", ""),
                required_inputs=required_inputs,
                optional_inputs=optional_inputs,
                data_output_keys=spec_data.get("data_output_keys") or [],
                side_effects=spec_data.get("side_effects") or ["none"],
                permission_level=spec_data.get("permission_level", "readonly"),
                not_responsible_for=spec_data.get("not_responsible_for") or [],
                lifecycle=spec_data.get("lifecycle", "permanent"),
            )
        except Exception as e:
            logger.warning(f"[SkillWriter] SkillSpec 构建失败: {e}")
            return None

        ok, errors = spec.hard_validate()
        if not ok:
            logger.warning(f"[SkillWriter] SkillSpec hard_validate 失败: {errors}")
            # ⚠️ **errors 必须交出去，不能只进日志。**
            # 实测 实测：同一个会话里模型连犯三次同类错误
            #   15:05  side_effects 含 'shell'      要求 dangerous，实际 readonly
            #   15:07  side_effects 含 'os_control' 要求 dangerous，实际 external_action
            #   15:11  side_effects 含 'shell'      要求 dangerous，实际 readonly
            # 报错内容每次都写得清清楚楚，但**一个字都没到模型手里** ——
            # 它没有任何途径知道自己错在哪，只能原样再撞一次。
            # 这就是 那条判据：正确但不充分 = 不合格。
            self._last_spec_errors = list(errors or [])
            return None
        self._last_spec_errors = []

        # 把原始 dict 带上 validated 标记返回(code gen 阶段需要 dict 注入 prompt)
        spec_data["_validated"] = True
        spec_data["_spec_obj"] = spec
        return spec_data

    async def _generate_skill_with_writer(self, query: str, base_guide: str, status_callback,
                                          extra_instruction: str = "", mode: str = "create",
                                          target_skill: str | None = None, change_summary: str = "",
                                          error_context: str = ""):
        """两段式 SkillWriter:SkillSpec → hard_validate → 代码生成。

        段一:调 provider.generate_skill_spec 生成 SkillSpec JSON → hard_validate
        段二:把通过校验的 SkillSpec 注入 prompt → 调 chat_with_tools 生成代码

        update 模式(修改已有 Skill)时跳过段一,因为 extra_instruction 里已经有原代码,
        让模型自己更新 SkillSpec 和代码。
        """
        # ── 段一:SkillSpec 生成(仅 create 模式走) ────────────────────────
        spec_data = None
        if mode == "create":
            yield {"event": "thinking",
                   "log": "正在分析需求生成 SkillSpec...",
                   "status": "CORE_THINKING", "model": "SkillSpecGen",
                   "current_skill": "SkillWriter", "rag_hit": False, "full_file_hit": False}

            spec_data = await self._generate_skill_spec(query, status_callback)

            # ⭐⭐⭐ [2026-08-13] hard_validate 失败 → **带着报错重试一次**，再决定降不降级。
            #
            # ═══ 为什么值得加这一次重试 ═══
            #
            # 实测（拆探索那天）触发的是这一条：
            #     SkillSpec hard_validate 失败: Permission mismatch: side_effects
            #     contains 'shell', which requires permission_level='dangerous',
            #     but current is 'external_action'
            # 一个**字段值不匹配**，报错本身已经把正确答案写出来了 —— 而我们
            # 一次重试都没给，直接降级成"不带 SkillSpec 的直接代码生成"。
            # 📌 **这不是安全问题，是每次都在白白掉一档质量**：
            #    通过校验的 SkillSpec 会被注入代码生成阶段，那比让模型凭需求原文猜强得多。
            #
            # ═══ ⚠️ 为什么【不】做成 fail-closed（停下来问用户）═══
            #
            # 一度想照 `create_new_skill` 那两条闸的样子把它也改成停下来。**那是错的。**
            # 📌 **fail-closed 的适用条件是「缺的那件事只有对方能给」** ——
            #    `handoff_summary` / `open_questions` 缺的是用户与主模型该给的东西，
            #    所以停下来问是对的；而 `permission_level 应该是 dangerous`
            #    是**写代码这一方自己该算对的内部细节**，抛给用户是纯噪音，
            #    用户根本无从回答。**缺自己该算对的东西，正确出口是重试，不是提问。**
            #
            # ⚠️ 只在**有具体报错**时重试（`_last_spec_errors` 非空 = 走的是
            #    hard_validate 那条 `return None`）。另外两条（provider 报错 /
            #    SkillSpec 构建异常）**不重试** ——
            #    📌 **没有新信息可给的重试，只是把成本翻倍。**
            if spec_data is None:
                _first_errs = list(getattr(self, "_last_spec_errors", None) or [])
                if _first_errs:
                    logger.info(
                        f"[SkillWriter] SkillSpec 校验失败 → 带着 {len(_first_errs)} 条报错重试一次"
                    )
                    yield {"event": "thinking",
                           "log": "SkillSpec 有几处不合规，带着报错重新生成一次…",
                           "status": "CORE_THINKING", "model": "SkillSpecGen",
                           "current_skill": "SkillWriter",
                           "rag_hit": False, "full_file_hit": False}
                    _retry_query = (
                        f"{query}\n\n"
                        "[Your previous SkillSpec for this same request was REJECTED by the validator]\n"
                        "Exact errors:\n"
                        + "\n".join(f"  - {e}" for e in _first_errs)
                        + "\n\nProduce a corrected SkillSpec for the SAME request. "
                          "Fix exactly these problems and change nothing else.\n"
                          "⚠️ side_effects and permission_level must be consistent: if the Skill "
                          "runs shell commands or controls the OS, it needs "
                          "permission_level='dangerous'. Do NOT silently drop a side effect just "
                          "to satisfy the check — the generated code is scanned by an AST checker "
                          "and a declaration that does not match the code will be rejected later."
                    )
                    spec_data = await self._generate_skill_spec(_retry_query, status_callback)
                    if spec_data is None:
                        # 🔴 **重试也失败 → 必须把「有报错可注入」这件事保住。**
                        #    `_generate_skill_spec` **进门就清空 `_last_spec_errors`**，
                        #    所以如果重试是因为别的原因失败（provider 报错 / 构建异常），
                        #    那份清空会让下面的降级分支拿到空列表 → `spec_injection = ""`
                        #    → **正是 那个「双重伤害」的复发路径**
                        #    （代码生成阶段既没有 spec、也不知道刚才错在哪，
                        #      于是带着同一个违规继续往下走，最后真的部署上去了）。
                        # 📌 **一个「进门先清空」的字段，任何重试都必须先把旧值接住。**
                        _retry_errs = list(getattr(self, "_last_spec_errors", None) or [])
                        self._last_spec_errors = _retry_errs or _first_errs
                        logger.warning("[SkillWriter] SkillSpec 重试后仍失败 → 降级为直接代码生成")
                    else:
                        logger.info("[SkillWriter] ⭐ 重试后 SkillSpec 通过校验（保住了两段式路径）")

            if spec_data is None:
                # 降级:SkillSpec 生成失败,走直接代码生成（容错，不中断用户体验）
                logger.warning("[SkillWriter] SkillSpec 生成失败,降级为直接代码生成")
                yield {"event": "thinking", "log": "SkillSpec 生成失败，已降级为直接代码生成模式",
                       "status": "CORE_THINKING", "model": "SkillSpecGen",
                       "current_skill": "SkillWriter", "rag_hit": False, "full_file_hit": False}
                # ⭐ 降级时把校验报错原样带进代码生成阶段。
                #
                # 原来这里是 `spec_injection = ""` —— 双重伤害：
                #   ① 代码生成阶段连一个 SkillSpec 都没有，只能凭需求原文猜；
                #   ② 刚才那条校验报错也丢了，于是生成的代码**带着同一个违规**继续往下走。
                # 那一次的最终产物就是证据：盘上的 ExtractComputerIP.py 写着
                #   side_effects=[SideEffect.SHELL] + permission_level=PermissionLevel.READONLY
                # 正是 hard_validate 拒了三次的那个组合，最后照样部署了。
                _spec_errs = getattr(self, "_last_spec_errors", None) or []
                if _spec_errs:
                    spec_injection = (
                        "\n\n[SkillSpec Validation Failed — Do Not Repeat These Mistakes]\n"
                        "A SkillSpec was drafted for this request but rejected by the validator. "
                        "The exact errors were:\n"
                        + "\n".join(f"  - {e}" for e in _spec_errs)
                        + "\n"
                        "You are now writing the code directly. The same rules still apply to the "
                        "get_spec() you write inside it — the audit-stage protocol check does not "
                        "re-check permission/side-effect consistency, so an inconsistent pair here "
                        "WILL ship.\n"
                        # ⚠️ 这段原来给了两个"选项"，其中一个写成了
                        # "if the Skill really only reads, do not declare those side effects"。
                        # 实测：模型选了那一个 —— 它把"查看网络配置"理解成只读，
                        # 于是**删掉了声明但保留了 subprocess.run()**，结果撞上 AST 一致性校验
                        # （"声明 side_effects=[]，但代码含 subprocess.run()"）。
                        # 那句措辞暗示了"改声明就能解决"，而声明和代码是被机械比对的。
                        # 现在只给一条路：**先看代码真的做什么，声明必须跟着代码走。**
                        "Rule: the declaration must match what the code actually does. "
                        "An AST scanner compares them mechanically, so you CANNOT fix a mismatch "
                        "by editing the declaration alone.\n"
                        "- If run() calls subprocess / os.system / os.popen, you must declare "
                        "side_effects=[SideEffect.SHELL] AND permission_level='dangerous'. "
                        "Removing the declaration while keeping the call is the one thing that "
                        "will definitely be rejected.\n"
                        "- Only if you can implement it with no shell call at all (pure Python "
                        "stdlib / psutil) may you declare it as read-only — and then there must "
                        "be no subprocess call anywhere in the file."
                    )
                else:
                    spec_injection = ""
            else:
                # 把通过校验的 SkillSpec 序列化注入给代码生成阶段
                import json
                spec_obj = spec_data.pop("_spec_obj", None)
                spec_data.pop("_validated", None)
                spec_data.pop("model", None)
                spec_injection = (
                    f"\n\n[Validated SkillSpec — Generate Code Strictly From This]\n"
                    f"```json\n{json.dumps(spec_data, ensure_ascii=False, indent=2)}\n```\n"
                    f"Every key in data_output_keys={spec_data.get('data_output_keys')} "
                    f"must appear in SkillResult.data returned by run().\n"
                    f"side_effects={spec_data.get('side_effects')} — "
                    f"the generated code's actual behavior must match this declaration, "
                    f"or AST validation will reject registration.\n"
                )
                # 补丁B.2（单向回流的轻量实现）：如果这次 SkillSpec 自己声明了
                # os_control（哪怕这次探索阶段没有打开 include_os，比如用户
                # 直接走普通 SKILL_CREATE 但需求本质是操作系统），说明这其实
                # 是个 OS Skill——不报错、不打回，直接在代码生成阶段补上 OS
                # Skill 的契约说明，让它老老实实生成"规划生成器"而不是直接
                # import pyautogui，而不是任由它生成一个违反契约的普通 Skill。
                if "os_control" in (spec_data.get("side_effects") or []) and "[OS Skill Code Contract" not in extra_instruction:
                    logger.warning("[SkillWriter] SkillSpec 声明 os_control 但未走 OS 探索路径，"
                                   "补注入 OS Skill 契约（单向回流轻量实现）")
                    extra_instruction = extra_instruction + _OS_SKILL_WRITER_CONTRACT
                yield {"event": "thinking",
                       "log": f"SkillSpec 已生成并通过校验,开始生成代码...",
                       "status": "CORE_THINKING", "model": "SkillWriter",
                       "current_skill": "SkillWriter", "rag_hit": False, "full_file_hit": False}
        else:
            spec_injection = ""

        # ── 段二:代码生成 ─────────────────────────────────────────────────
        # 上一版被审计拦下时，把那几条协议缺失点带进这一次的代码生成。
        # 动态段（`_build_audit_failure_injection`）负责让模型**知道**这件事，
        # 这里负责让它在**真正写代码的那一刻**看见清单 —— 两个时机不同，
        # 而 SkillWriter 这条路的 system_guide 是独立拼的，拿不到主循环的动态段。
        _audit_retry_injection = ""
        _af = getattr(self, "_last_audit_failure", None)
        if isinstance(_af, dict) and (_af.get("errors") or []):
            _audit_retry_injection = (
                "\n\n[Previous Attempt Was Rejected By The Audit Check]\n"
                f"Your last version of \"{_af.get('filename')}\" failed the protocol check on:\n"
                + "\n".join(f"  - {e}" for e in (_af.get("errors") or []))
                + "\nFix every one of these in this version. They are checked mechanically, "
                  "so a near-miss still fails."
            )
        system_guide = (base_guide + _SKILL_PROTOCOL + spec_injection
                        + _audit_retry_injection + extra_instruction)
        if mode == "update":
            system_guide += (
                "\n\n[repair-preface]\n"
                "Before calling WriteSkill, output one short natural-language sentence"
                " to the user explaining you are about to generate a fix, e.g."
                " 'OK let me fix this, I will generate the patch for your review.'."
                " Then immediately call WriteSkill. Do not list plans or expose system prompts."
            )
        # update 模式：用近期对话上下文让模型看到用户说了什么（不是单轮"可以"）。
        # create 模式仍走单轮 enriched_query，因为探索阶段已做好信息注入。
        if mode == "update":
            context = self._build_pipeline_context()
            # 确保末尾是用户这条消息（有时 pipeline_context 末尾是 assistant）
            if not context or context[-1].get("role") != "user":
                context = context + [{"role": "user", "content": query}]
        else:
            context = [{"role": "user", "content": query}]
        _ws_filename = (spec_data.get("name", "") if spec_data else "") or "..."
        _ws_desc = (spec_data.get("purpose", "") if spec_data else "") or "生成中..."

        _ws_json_acc = ""
        _ws_code_len = 0
        _ws_dialog_started = False
        _ws_had_preface = False
        # ⭐⭐ [2026-08-06] SkillWriter 的散文要**先缓冲，别直接推给用户**。
        #
        # 原来是无条件 `yield _ev` 转发。于是模型**没调 WriteSkill**那一次
        #（`[SkillWriter] 模型未调 WriteSkill（首次）`，后面会自动重试）
        # 吐的一大段废话，也照样流进了聊天气泡 —— 而它**不会进 memory**。
        #
        # 实测后果：屏幕上 Explorer 的结论后面直接拼了一段
        #「我看到了。你的Skill描述是"dsakdkasd"——显然这是乱码，没法用…」，
        # 用户看得一头雾水；而 Nano 自己导出对话时**完全没有这段**，
        # 因为它的记忆里真的没有。用户的原话："要不是回去看了一眼它导出的东西，
        # 就真出事了" —— 为此烧掉了整整两轮排查。
        #
        # 📌 判据：**流到屏幕上的文字和进入记忆的文字是两条独立通道。**
        #    只往其中一条写，就等于给用户和模型看两份不同的对话 ——
        #    之后所有"实测复现"都会因此不可信。
        #
        # 修法：缓冲。等 `tool_input_delta` 真的来了（= 这次写手确实在产出代码），
        # 才把缓冲的开场白放出去；一直没来就整段丢弃，交给重试。
        _ws_preface_buf: list[str] = []

        async for _ev in self._stream_decision(
            context, [_WRITE_SKILL_MANIFEST], system_guide,
            task_type="code_writing", stage_label="code_generation",
        ):
            if _ev.get("type") == "tool_input_delta":
                # 开窗提前到"第一个 tool_input chunk"（而非第一个 code delta）：
                # 让弹窗+CodeMirror 初始化在 filename/description 还在流的时候就并行
                # 完成，等 code 字段真正开始时 CM 已 ready，代码从第一个字干净流式，
                # 不再因 0.15s 初始化窗口堆积导致"从一半开始"。
                if not _ws_dialog_started:
                    _ws_dialog_started = True
                    # ⭐ 到这一刻才确认"这次写手是真在产出代码" → 放行开场白。
                    if _ws_had_preface:
                        for _p in _ws_preface_buf:
                            yield _p
                    elif mode == "update":
                        # ⚠️ 固定文案，落在"为一句话多跑一次模型不划算"这条豁免里。
                        #
                        # ⚠️⚠️ 但**只在 update 路径发**。这句话原文是"我来处理这次修复"，
                        # 它是为改已有 Skill 写的；create 路径上它有两处错：
                        #   ① 新建不是"修复"，说的是错的事；
                        #   ② Explorer 刚说完"我来直接构建这个 XX"，再来一句纯重复
                        #      —— 实测屏幕上就是两句话硬拼在一起（2026-08-06 看到）。
                        # create 路径本来就有 Explorer 的结论当开场白，不需要补。
                        yield {"event": "final_text",
                               "delta": "好，我来处理这次修复，生成的代码会先进入审计窗口，确认后再部署。\n\n"}
                    _ws_preface_buf.clear()
                    yield {"event": "skill_code_start",
                           "filename": _ws_filename, "description": _ws_desc}
                _ws_json_acc += _ev.get("partial_json", "")
                _new_code, _ws_code_len = _extract_code_delta(_ws_json_acc, _ws_code_len)
                if _new_code:
                    yield {"event": "skill_code_delta", "delta": _new_code,
                           "accumulated_len": _ws_code_len}
            else:
                if _ev.get("event") in ("final_text", "final_text_delta"):
                    _txt = (_ev.get("delta") or _ev.get("content") or "").strip()
                    if _txt:
                        _ws_had_preface = True
                    if not _ws_dialog_started:
                        # 还不知道这次会不会真产出代码 → 先扣着。
                        # 代码开始流之后（`_ws_dialog_started`）写手就不该再说话了，
                        # 万一还说，照常放行（那属于代码后的补充说明，不是失败产物）。
                        _ws_preface_buf.append(_ev)
                        continue
                yield _ev
        decision = self._last_stream_decision
        used_model = self._last_stream_model
        if decision.decision_type == "call" and decision.name == "WriteSkill":
            # AST 副作用一致性校验:把 spec 声明的 side_effects 传进去
            _spec_side_effects = spec_data.get("side_effects") if spec_data else None
            async for step in self._emit_skill_preview_from_decision(
                decision, used_model, mode=mode,
                target_skill=target_skill, change_summary=change_summary,
                spec_side_effects=_spec_side_effects,
                error_context=error_context,
            ):
                yield step
        else:
            # 模型没有调 WriteSkill——自动内部重试一次，而不是抛给用户。
            # 原先抛给用户"再说一次可以"的设计有严重副作用：
            # 用户"可以"进主路由，Haiku 没有状态上下文，会幻觉出 skill_create。
            _first_raw = (decision.content or "").strip()
            logger.warning(f"[SkillWriter] 模型未调 WriteSkill（首次），输出：{_first_raw[:120]}，自动重试...")
            # ⭐ 首次那段散文是**失败的中间产物**，用户不该看到（它也进不了 memory）。
            # 缓冲区里扣着的正是它 —— 直接丢，别放行。
            # ⚠️ `_ws_had_preface` 必须跟着复位：否则重试成功时会因为"上次说过话"
            #    而既不放缓冲（已清空）也不发默认开场白，结果一句话都没有。
            _ws_preface_buf.clear()
            _ws_had_preface = False

            # ── 重试：注入更强硬的多轮指令 ───────────────────────────────
            _retry_context = [
                {"role": "user", "content": query},
                {"role": "assistant", "content": _first_raw or "(no output)"},
                {"role": "user", "content": (
                    "Call the WriteSkill tool now and provide the complete Python Skill code. "
                    "Do not output normal text. Call the tool directly."
                )},
            ]
            _ws_json_acc2 = ""
            _ws_code_len2 = _ws_code_len
            async for _ev2 in self._stream_decision(
                _retry_context, [_WRITE_SKILL_MANIFEST], system_guide,
                task_type="code_writing", stage_label="code_generation_retry",
            ):
                if _ev2.get("type") == "tool_input_delta":
                    # 同主路径：开窗提前到第一个 tool_input chunk
                    if not _ws_dialog_started:
                        _ws_dialog_started = True
                        # ⭐ 与主路径同一套缓冲：确认真在产出代码，才放开场白。
                        # ⚠️ 缓冲区里此刻**只可能**有本次重试说的话 —— 首次那段
                        #    在进入重试前已被丢弃（见上面 `_ws_preface_buf.clear()`）。
                        if _ws_had_preface and _ws_preface_buf:
                            for _p in _ws_preface_buf:
                                yield _p
                        elif mode == "update":
                            # 同主路径：这句只对 update 成立，理由见上面那段注释。
                            yield {"event": "final_text",
                                   "delta": "好，我来处理这次修复，生成的代码会先进入审计窗口，确认后再部署。\n\n"}
                        _ws_preface_buf.clear()
                        yield {"event": "skill_code_start",
                               "filename": _ws_filename, "description": _ws_desc}
                    _ws_json_acc2 += _ev2.get("partial_json", "")
                    _new_code2, _ws_code_len2 = _extract_code_delta(_ws_json_acc2, _ws_code_len2)
                    if _new_code2:
                        yield {"event": "skill_code_delta", "delta": _new_code2,
                               "accumulated_len": _ws_code_len2}
                else:
                    if _ev2.get("event") in ("final_text", "final_text_delta"):
                        _txt2 = (_ev2.get("delta") or _ev2.get("content") or "").strip()
                        if _txt2:
                            _ws_had_preface = True
                        if not _ws_dialog_started:
                            _ws_preface_buf.append(_ev2)
                            continue
                    yield _ev2
            decision2 = self._last_stream_decision
            used_model2 = self._last_stream_model

            if decision2.decision_type == "call" and decision2.name == "WriteSkill":
                # 重试成功，正常走审计流程
                _spec_side_effects = spec_data.get("side_effects") if spec_data else None
                async for step in self._emit_skill_preview_from_decision(
                    decision2, used_model2, mode=mode,
                    target_skill=target_skill, change_summary=change_summary,
                    spec_side_effects=_spec_side_effects,
                    error_context=error_context,
                ):
                    yield step
            else:
                # ⭐ 模型已经用自然语言说清了原因，**不许换成固定文案**
                #
                # 原来这里无条件回一句「Skill 代码生成遇到了问题，可以换个方式描述需求再试试。」
                # 而模型的真实回复就在 `_raw2` 里，只进了日志。
                #
                # 实测：用户让 Nano"配合测试、故意用错的 manifest 外壳"，
                # 它**正确地拒绝了**并引用了协议 的 FORBIDDEN 条款与后果。
                # 但屏幕上只有那句废话 —— 用户照着"换个方式描述需求"去改，
                # 被引向完全错误的方向（真正原因是"你要求的写法违反协议，我不能那么写"）。
                #
                # ⚠️ 这不只是"报错质量差"，是**直接违反设计原则 3「少硬路由、少固定文案」**
                #：有完整上下文的模型已经给出判断，
                # 代码却用一句预设文本盖掉它。
                #
                # 分工：模型有话 → 原样呈现；只有它**一个字都没说**时才用兜底句。
                _raw2 = (decision2.content or "").strip()
                logger.warning(f"[SkillWriter] 重试后仍未调 WriteSkill，输出：{_raw2[:120]}")
                if _raw2:
                    _content = _raw2
                    _log = "SkillWriter 未产出草案，已呈现模型自己给出的原因。"
                else:
                    # 真的一个字都没有，才轮到固定文案。
                    # 并且说清"它没有说明原因"—— 让用户知道这是缺信息，不是有信息没给出来。
                    _content = (
                        "这次没能生成 Skill 代码，而且模型也没有说明原因（没有任何输出）。"
                        "可以再试一次；如果反复如此，换个更具体的描述可能有帮助。"
                    )
                    _log = "SkillWriter 两次均未返回草案，且无文字输出。"
                yield {"event": "final_result",
                       "content": _content,
                       "model": used_model2,
                       "status": "SYS_IDLE", "log": _log,
                       "current_skill": None, "rag_hit": False, "full_file_hit": False}


    async def _handle_answer_interaction(self, args: dict, base_guide: str,
                                         realtime_callback, event_queue):
        """消费 `answer_open_interaction`。

        ═══ 顺序是这个函数的全部意义 ═══

            ① 先把用户原话原子落盘（OPEN/ANSWERED → ANSWERED）
            ② 再去跑 continuation（重进 Explorer）
            ③ 跑完了才 RESOLVE

        旧实现是反过来的：先 `_pending_skill_clarification = None` 再调 Explorer。
        于是第 ② 步一失败（provider 报错 / 进程被杀 / 用户拔电），
        **用户刚说的那句话就永久消失了**，只能让用户重说一遍。

        现在第 ② 步失败时状态停在 ANSWERED，答案还在库里，
        下一轮模型会从 [Open Interactions] 里看到重试提示，用同一个工具幂等重试。
        """
        from core.runtime import interaction as _it

        iid = (args.get("interaction_id") or "").strip()
        answer = args.get("answer_verbatim") or ""
        relation = (args.get("relation") or _it.Relation.ANSWER).strip().upper()
        if relation not in _it.Relation._ALL:
            relation = _it.Relation.ANSWER

        rec = None
        try:
            from core.runtime.kernel import get_kernel
            rec = _it.get(get_kernel(), iid)
        except Exception as e:
            logger.error(f"[Interaction] 读取 {iid} 失败: {e}")

        if rec is None or not rec.is_live:
            # 模型引用了一个不存在或已关闭的交互。**不要静默吞掉**——
            # 静默会让模型以为"回答已被记录"，然后它就不会再提这件事了。
            _why = "已经处理过了" if rec is not None else "不存在"
            # ⚠️ `_why` 给日志看（中文）；交给模型的事实必须英文，
            #    所以**并排**放一份，而不是把中文塞进 tool_result。
            _why_en = "was already handled" if rec is not None else "does not exist"
            logger.warning(f"[Interaction] 模型引用的交互 {iid!r} {_why}")
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"The to-do {iid!r} that was referenced {_why_en}. Nothing was recorded "
                       f"against it. What the user actually said: " + repr(answer) + ". "
                       f"Answer that as a normal request; bring up the stale to-do only if it "
                       f"actually matters to them."
                   ),
                   "log": f"交互 {iid} {_why}。"}
            return

        # ── ① 原子落盘 ──────────────────────────────────────────────────
        try:
            _res = _rt_answer_interaction(iid, answer, relation)
        except Exception as e:
            # 这里**必须**告诉用户，不能假装记下了继续跑。
            logger.error(f"[Interaction] 记录回答失败 {iid}: {e}")
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"Could not save the user's answer to to-do {iid!r} (runtime storage "
                       f"error: {e}). Their answer was NOT recorded. Tell them briefly and ask "
                       f"them to say it once more."
                   ),
                   "log": f"Interaction {iid} 落盘失败"}
            return
        if _res.get("retry"):
            logger.info(f"[Interaction] {iid} 幂等重试（答案未变，不重复写）")

        # ── CANCEL：不需要 continuation ─────────────────────────────────
        if relation == _it.Relation.CANCEL:
            # 审计类要连**工作载荷**一起丢，不能只关交互 ——
            # 留着 `_pending_skill` 会让 UI 上那个弹窗仍然可以点「验证并应用」，
            # 而用户刚说的是"不要了"。走 `cancel_pending_skill()` 复用既有清理
            # （它自己会调 `_rt_close_skill_audit`，所以这里不再重复关）。
            if rec.kind == _it.Kind.SKILL_AUDIT and                     self._get_pending_skill(rec.artifact_id or ""):
                _res_c = self.cancel_pending_skill(rec.artifact_id or "")
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The user cancelled the pending Skill audit for {rec.artifact_id!r}. "
                           f"The draft was discarded; nothing was deployed. Acknowledge briefly "
                           f"and move on - do not re-offer it unless they bring it up."
                       ),
                       "log": f"用户取消了审计 {iid}。"}
                return
            if rec.kind == _it.Kind.MCP_MANAGE:
                # MCP 管理取消。⚠️ 与 Skill 那条**同样**要清 `_pending_action` ——
                #    理由一字不差：留着它会活到 30 分钟超时，期间用户再说「确认」
                #    会误触发一个已经被取消过的操作，而**删 MCP 同样不可逆**。
                _srv_c = (rec.payload or {}).get("server") or rec.artifact_id or ""
                _rt_close_mcp_manage(_srv_c, approved=False,
                                     reason=_it.Resolution.USER_CANCELLED)
                self._pending_action = None
                self._pending_action_at = 0.0
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The user cancelled deleting MCP server {_srv_c!r}. Nothing was deleted; "
                           f"the server is still configured and usable. Acknowledge briefly."
                       ),
                       "log": f"用户取消了 MCP 管理 {iid}。"}
                return
            if rec.kind == _it.Kind.SKILL_MANAGE:
                # 管理类取消：把 `_pending_action` 一起清掉，
                # 否则它会活到 30 分钟超时，期间用户再说"确认"会误触发一个
                # 已经被取消过的操作（删 Skill 是不可逆的，这个错不能犯）。
                _sk_c = (rec.payload or {}).get("skill") or rec.artifact_id or ""
                _rt_close_skill_manage(_sk_c, approved=False,
                                       reason=_it.Resolution.USER_CANCELLED)
                self._pending_action = None
                self._pending_action_at = 0.0
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The user cancelled the pending management operation on Skill {_sk_c!r} "
                           f"(the operation was: {(rec.payload or {}).get('op') or 'unspecified'}). "
                           f"Nothing was changed. Acknowledge briefly."
                       ),
                       "log": f"用户取消了管理确认 {iid}。"}
                return
            _rt_close_interaction(iid, _it.CANCEL,
                                  resolution=_it.Resolution.USER_CANCELLED)
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"The user cancelled the pending to-do {iid!r}. Nothing was done. "
                       f"Acknowledge briefly."
                   ),
                   "log": f"用户取消了交互 {iid}。"}
            return

        # ── ② continuation：按 kind 分派 ────────────────────────────────
        if rec.kind == _it.Kind.SKILL_AUDIT:
            # 用户用**打字**处置待审 Skill（点按钮走的是 UI 那条路）。
            #
            # relation 的映射：
            #   ANSWER               → 同意部署（"可以"/"部署吧"）
            #   ANSWER_AND_AMENDMENT → 要改（"把阈值改成 6000 再部署"）
            #   CANCEL               → 丢弃（已在上面通用分支处理）
            #
            # ⭐ 在这里执行：**用户点同意与代码真正写盘之间，artifact 可能已经变了**
            # （用户在 CodeMirror 里改了几行、或者另一条流程覆盖了 `_pending_skill`）。
            # 所以落地前必须复核指纹，不一致就停手 —— 不能"反正用户说了可以"。
            # **按 rec.artifact_id 精确取**这一条的载荷。
            # 改造前这里读的是"最近那条"再跟 artifact_id 比对，于是两条待审并存时
            # 对老的那条调工具必然比对失败 → 掉进"载荷已失效"分支 → 误报 + 误取消。
            _fn = rec.artifact_id or ""
            _pend = self._get_pending_skill(_fn) or {}
            if not _pend:
                # 待审载荷已经不在了（超时清理 / 被别的流程覆盖 / 重启后只剩交互记录）。
                # 如实说清，不要假装能部署 —— 代码正文没有落盘，重启后确实拿不回来。
                _rt_close_interaction(iid, _it.CANCEL,
                                      resolution=_it.Resolution.INTERRUPTED_BY_RESTART)
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The reviewed draft for {rec.artifact_id!r} is gone. Pending drafts live "
                           f"only in memory, so a restart or a timeout loses them - the code itself "
                           f"was never written to disk and cannot be recovered. Say that plainly, "
                           f"and offer to generate a fresh version for them to review."
                       ),
                       "log": f"审计交互 {iid} 的载荷已失效。"}
                return

            _ok_art, _why = _it.verify_artifact(
                rec, current_revision=None, current_text=_pend.get("code") or "")
            if not _ok_art:
                # artifact 变了 → 这一次批准不放行（它指的是旧版本）。
                #
                # ⚠️⚠️ **但绝不能把这条交互直接关掉。** 第一版就是那么写的
                # （`SUPERSEDE` + ARTIFACT_CHANGED），2026-08-06 实测
                # 当场暴露自相矛盾：
                #   · 卡片消失了，而回复却说"再点一次「验证并应用」即可" ——
                #     那个弹窗的**唯一入口就是刚被关掉的那张卡**；
                #   · 用户改到一半的代码就此**再也拿不回来**；
                #   · 而且 SUPERSEDED **没有任何后继者**，状态本身就是假的。
                #
                # 📌 判据：**拒绝一次操作 ≠ 结束这件事。**
                #    守卫要挡住的是"这次批准指的是旧版本"，不是"这份草稿不要了"。
                #    用户的编辑是合法产出，凭什么因为一次拒绝就被丢掉。
                #
                # 正解：给**当前这一版**重新开一张待审卡。新卡登记时会自动
                # SUPERSEDE 同名旧卡（见 `_rt_open_skill_audit`），于是
                # 「旧的被取代」这个状态第一次真的成立，用户也立刻有了新入口。
                logger.warning(f"[Interaction] 审计 {iid} 的 artifact 已变更，拒绝放行：{_why}")
                _new_iid = ""
                try:
                    _new_iid = _rt_open_skill_audit(
                        self, _fn, _pend.get("description") or "",
                        _pend.get("code") or "", _pend.get("mode", "create"),
                        bool(_pend.get("valid", True)), list(_pend.get("errors") or []),
                    )
                except Exception as _e:
                    # 开不出新卡也不能把旧卡关掉 —— 那样用户就彻底没入口了。
                    # 宁可留一张"批准会被拒"的旧卡，也比留一份够不着的代码强。
                    logger.error(f"[Interaction] 为改动后的 {_fn} 重开待审失败: {_e}")
                # ⚠️ **不要在这里写死一句中文交给用户。**
                #
                # 2026-08-06：固定文案完全不受人格模板影响，占比一高就会
                # 让 Nano 高频人格分裂 —— 满屏都是设定好的语气，中间突然蹦出一句
                #「批准要对得上具体哪一版」。设计原则只给两个豁免：
                #   ① 模型调用本身出问题时的兜底  ② 气泡里的系统级通知/报错
                # 这一条**两个都不占**：模型好好的，而且这是一次正常的业务结果，
                # 不是系统故障。
                #
                # 所以只把**事实**交回去（英文，按"注入给模型的提示词用英文"那条），
                # 由主循环写成 tool_result，让模型用它自己的话说。
                # 📌 同：给足够的事实让它判断怎么说、下一步做什么，
                #    而不是替它把话说死。
                yield {
                    "event": "exit_flow_defer_to_model",
                    "tool_result": (
                        f"Not deployed. The draft changed after this approval was formed, "
                        f"so approving it would have installed a different version than the "
                        f"one the user meant ({_why}). "
                        + (f"The edited version is now pending review as {_new_iid}; "
                           f"it can be deployed once the user confirms this version."
                           if _new_iid else
                           "The user can install this version from the review dialog "
                           "with the apply button.")
                        + " Tell the user what happened and what they can do next. "
                          "Do not repeat the hash values — they mean nothing to them."
                    ),
                }
                return

            if relation == _it.Relation.ANSWER_AND_AMENDMENT:
                # 要改 → 走既有的"带修改意见重新生成"通路，不自己造一条。
                # 交互标 SUPERSEDED：新生成的那一版会开一条**新的**审计交互
                # （同 Explorer 又提新问题的处置：旧的关掉、新的独立，不做回退覆盖）。
                _rt_close_interaction(iid, _it.SUPERSEDE,
                                      resolution=_it.Resolution.FOLLOW_UP_QUESTION)
                logger.info(f"[Interaction] 审计 {iid} → 用户要求修改，重新生成")
                async for step in self._generate_skill_with_writer(
                    _pend.get("description") or _fn, base_guide, realtime_callback,
                    extra_instruction=(
                        "\n\n[User Revision Request For The Pending Draft]\n"
                        f"{answer}\n\n"
                        "Regenerate the Skill applying this. Keep everything else as it was."
                    ),
                    mode=_pend.get("mode", "create"),
                    target_skill=_pend.get("target_skill"),
                ):
                    yield step
                return

            # ANSWER → 同意部署。走既有 apply_pending_skill（它自己会关交互）。
            logger.info(f"[Interaction] 审计 {iid} → 用户同意部署 {_fn}")
            _res = self.apply_pending_skill(_fn)
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"The user approved the pending Skill {_fn!r} and it has been deployed "
                       f"(ok={_res.get('ok')}). Raw system message, as evidence: "
                       + repr(_res.get("msg") or "") + ". Tell them it is in place."
                   ),
                   "log": f"审计 {iid} 经模型工具批准部署。"}
            self.request_skill_refresh() if hasattr(self, "request_skill_refresh") else None
            return

        if rec.kind == _it.Kind.MCP_MANAGE:
            # 用打字确认 MCP 管理操作。只有 delete 会走到这里 ——
            # enable / disable / retry 可逆，在 `_handle_manage_mcp_decision` 里
            # 已经立即执行完了，不进确认流。
            _p_m = rec.payload or {}
            _srv = _p_m.get("server") or rec.artifact_id or ""
            _op_m = (_p_m.get("op") or "").strip()
            logger.info(f"[Interaction] MCP 管理确认 {iid} → 用户确认 {_op_m} {_srv}")
            _rt_close_mcp_manage(_srv, approved=True)
            self._pending_action = None
            self._pending_action_at = 0.0
            try:
                from core.mcp_client import MCPManager as _MM_c
                _ok = await _MM_c.instance().remove_server(_srv)
                _mcp_facts = (
                    f"MCP server {_srv!r} was deleted from the configuration."
                    if _ok else
                    f"Deleting MCP server {_srv!r} did not happen - it was already "
                    f"not in the configuration. Nothing changed.")
                if _ok:
                    # ⭐ 与 Nano 自己动手那条**同一个出口** —— 见 `_note_mcp_change`
                    self._note_mcp_change("delete", _srv, by="nano")
            except Exception as e:
                logger.warning(f"[B3] 删除 MCP {_srv} 失败: {e}")
                _mcp_facts = (f"Deleting MCP server {_srv!r} failed with: {e}. "
                              f"Report the failure as-is; do not soften it.")
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       _mcp_facts
                   ),
                   "log": f"MCP 管理确认 {iid} 已执行。"}
            return

        if rec.kind == _it.Kind.SKILL_MANAGE:
            # 用打字确认 Skill 管理操作（删除 / 禁用 / 启用 / 改写）。
            # CANCEL 已在上面通用分支处理；这里只处理"确认执行"。
            _p = rec.payload or {}
            _op = (_p.get("op") or "").strip()
            _sk = _p.get("skill") or rec.artifact_id or ""
            logger.info(f"[Interaction] 管理确认 {iid} → 用户确认 {_op} {_sk}")

            if _op == "update_skill":
                # 改写确认 → 走既有的生成通路。
                # ANSWER_AND_AMENDMENT 时把用户的补充意见一并带上。
                _rt_close_skill_manage(_sk, approved=True)
                self._pending_action = None
                self._pending_action_at = 0.0
                _extra = ""
                if relation == _it.Relation.ANSWER_AND_AMENDMENT:
                    _extra = ("\n\n[User Additional Requirement On Top Of The Confirmed Plan]\n"
                              f"{answer}")
                async for step in self._generate_skill_update(
                    _p.get("query") or _sk, _sk, base_guide, realtime_callback,
                    change_summary=(_p.get("summary") or "") + _extra,
                ):
                    yield step
                return

            # delete / disable / enable → 走既有的 registry 操作
            if _op not in {"delete", "disable", "enable"}:
                logger.error(f"[Interaction] 管理确认 {iid} 的 op={_op!r} 不认识")
                _rt_close_interaction(iid, _it.CANCEL,
                                      resolution=_it.Resolution.USER_CANCELLED)
                yield {"event": "exit_flow_defer_to_model",
                       "tool_result": (
                           f"The stored management operation {_op!r} is not one of delete / disable "
                           f"/ enable, so nothing was executed. This is an internal gap on our side, "
                           f"not the user's mistake. Ask them to just say what they want, and you "
                           f"will do it again."
                       ),
                       "log": f"管理确认 {iid} op 不识别：{_op}"}
                return

            _rt_close_skill_manage(_sk, approved=True)
            self._pending_action = None
            self._pending_action_at = 0.0
            try:
                if _op == "delete":
                    # ⚠️ 方法名是 `delete_skill_file`，不是 `delete_skill`
                    # （第一版写错了，编译期发现不了 —— 属性访问是运行时解析的）。
                    _r = self.registry.delete_skill_file(_sk)
                elif _op == "disable":
                    _r = self.registry.disable_skill(_sk)
                else:
                    _r = self.registry.enable_skill(_sk)
                _skill_manage_facts = (
                    f"Operation {_op!r} on Skill {_sk!r} completed "
                    f"(ok={(_r or {}).get('ok', True)}). Raw system message, as "
                    f"evidence: " + repr((_r or {}).get("msg") or "") + ".")
            except Exception as e:
                # ⚠️ 如实报错，不要换成"操作遇到了问题"（设计原则 3 的推论）。
                logger.error(f"[Interaction] 管理操作 {_op} {_sk} 失败: {e}")
                _skill_manage_facts = (
                    f"Operation {_op!r} on Skill {_sk!r} failed with: {e}. "
                    f"Report it as-is - do not replace it with a vague "
                    f"\"something went wrong\".")
            self.request_skill_refresh() if hasattr(self, "request_skill_refresh") else None
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       _skill_manage_facts
                   ),
                   "log": f"管理确认 {iid} 执行 {_op}。"}
            return

        if rec.kind != _it.Kind.SKILL_CLARIFICATION:
            # 走到这里说明有人加了新 kind 却没加分支。响亮地记一条，
            # 并且**不要**假装处理完了 —— RESOLVE 会让模型以为事情办了。
            logger.error(
                f"[Interaction] {iid} 的 kind={rec.kind} 没有 continuation 分支 —— "
                f"这是代码缺口，不是用户输入问题"
            )
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       f"Their answer was recorded, but there is no handler for to-do kind "
                       f"{rec.kind!r} - that is an implementation gap on our side, not their "
                       f"mistake. Say so honestly and invite them to restate what they want; "
                       f"you will handle it as a normal request."
                   ),
                   "log": f"kind={rec.kind} 无分支（代码缺口）。"}
            return

        # ══════════════════════════════════════════════════════════════════
        # 创建澄清的续接 —— **2026-08-13：不再重跑子循环，把事实交回主 ReAct**
        # ══════════════════════════════════════════════════════════════════
        #
        # 🔴 旧实现在这里硬编码 `self._run_skill_exploration(...)` —— 探索拆掉之后
        #    这一段必须改，它是整次拆除里**唯一一处不改就会直接崩**的地方。
        #
        # ⭐ 新链条（不发明新机制，用 已有的 `exit_flow_defer_to_model`）：
        #
        #     用户回答 → answer_open_interaction
        #              → 答案先原子落盘（ANSWERED）
        #              → 把 checkpoint + 用户回答作为【事实】交回主 ReAct
        #              → 主模型重新决策：
        #                   还缺信息   → create_new_skill(open_questions=[...])
        #                   已经清楚   → create_new_skill(open_questions=[])
        #                   应该改已有 → update_existing_skill
        #                   现成的够用 → 直接调那个 Skill
        #
        # 📌 **这比"重新造一个 Explorer"干净**：续接需要的从来不是一个子循环，
        #    而是「上次问到哪了」这份领域状态 —— 而那份状态就在 checkpoint 里。
        # ⭐ 而且它顺带解锁了旧实现做不到的两条出口：旧的只能重跑 Explorer
        #    （出口只有 create），现在用户答完之后模型可以改走 update、甚至发现
        #    现成 Skill 就够用。📌 **拆掉一个封闭作用域，出口数量是增加的。**
        #
        # ⚠️⚠️ **收尾时机必须保住原语义**：`OPEN → ANSWERED → (续接成功) → RESOLVED`。
        #    这里**刻意不 RESOLVE** —— 「把事实交回主模型」**不等于**领域工作已经续接成功。
        #    如果紧接着 provider 报错、或模型没接住，状态停在 ANSWERED，
        #    下一轮 `[Open Interactions]` 会带着用户原话让它幂等重试，
        #    **不要求用户重说一遍**。那正是这条状态流当初存在的全部理由。
        # 📌 **判据：一个"已回答"的记录，要等到它引发的领域动作真的产生了新状态，
        #    才算续接完成 —— 把消息递出去不算。**
        #
        # ⭐ 那么谁来把它收成 RESOLVED？—— **产生新状态的那一方**：
        #    · 模型再调 `create_new_skill` 且无未决问题 → `_rt_supersede_covered_clarifications`
        #      按需求重叠度把它标成 SUPERSEDED（既有机制，实测 挣来的）
        #    · 模型再次自报未决问题 → 开一条**新的**澄清，旧的同样被上面那条收掉
        #    · 澄清有 TTL，最坏情况由过期兜底，不会变僵尸
        _p = rec.payload or {}
        _orig = _p.get("original_requirement") or answer
        # ⚠️ 兼容读：老记录里这个字段叫 `last_explorer_message`（见 `_rt_open_clarification`
        #    的迁移说明）。新写入一律是 `last_creation_note`，但老的 Interaction
        #    可能还活在库里，读的时候两个都认。
        _note_prev = (_p.get("last_creation_note")
                      or _p.get("last_explorer_message") or "").strip()

        _facts = (
            f"The user has answered a pending clarification about creating a Skill.\n\n"
            f"[Original requirement]\n{_orig}\n\n"
            + (f"[What you had established last time]\n{_note_prev}\n\n" if _note_prev else "")
            + f"[The user's answer, verbatim]\n{answer}\n\n"
            + "Decide what to do now with this answer. If everything is clear, call "
              "create_new_skill with an empty open_questions. If something still blocks you, "
              "call it again with the remaining questions. If it turns out an existing Skill "
              "should be updated instead, use update_existing_skill; if one already fits, "
              "just call it. Do not ask the user to repeat what they already said above."
        )
        # ⚠️ 字段名是 **`tool_result`**（`_take_defer` 读的就是它），不是 `facts`。
        #    第一版按印象写了 `facts` —— 那样事件会被拦下、内容却是空的，
        #    表现为「模型收到一条空的 tool_result，然后不知道该干嘛」。
        #    📌 又一次：**事件的字段名要回代码核，不能凭印象**（同那次 `_rt_live_interactions`）。
        logger.info(f"[Interaction] 澄清 {iid} 的回答已交回主 ReAct（{len(_facts)} 字符事实）")
        yield {"event": "exit_flow_defer_to_model", "tool_result": _facts,
               "log": f"澄清 {iid} 已回答，交回主决策继续。"}

    async def _stream_with_window_guard(self, context, tools_manifest, system_guide):
        """发不出去时：**紧急回收旧历史 → 重试一次 → 还不行就如实说**。

        ⚠️ **只重试一次。** 📌 一个「失败就回收一点再试」的循环，如果没有次数
           上限，它会在真正装不下的那一天把整段历史磨掉，**然后仍然失败** ——
           而那时用户既没得到答案，也没有了历史。
        🔴 `active_too_big`（这一轮本身就装不下）**根本不重试**：
           📌 回收多少历史都没用，重试一次只是多花一次时间去撞同一堵墙。
        """
        from core.context.guard import ContextWindowExceeded as _CWE

        async def _once():
            async for _e in self.provider.chat_with_tools_stream(
                    context, tools_manifest, system_guide, model_override=None,
                    # ⭐ 缓存断点：前 N 个是无条件核心工具，每轮都一样。
                    #    它后面（条件工具 / `load_tools` 追加的）变了也不伤前缀。
                    stable_tool_count=getattr(self, "_core_stable_n", -1)):
                yield _e

        try:
            async for _e in _once():
                yield _e
            return
        except _CWE as _ex:
            if _ex.kind == "active_too_big":
                logger.error("[Guard] 这一轮输入本身就装不下 —— 不重试（回收也没用）")
                yield {"event": "final_result", "content": _ex.user_message,
                       "status": "SYS_IDLE"}
                return
            _short = max(1, int(_ex.predicted) - int(_ex.limit))
            logger.warning(f"[Guard] 被拒发，缺 {_short:,} token → 紧急回收后重试一次")

        _got = 0
        try:
            from core.context.decay import emergency_reclaim as _er
            from core.context.decay_store import DecayStore as _DS
            from core.runtime.kernel import get_kernel as _gk
            _sid = self.memory.conversation_session_id
            if _sid:
                _got = _er(self.memory, _DS(_gk().store), _sid,
                           self.provider.target_model, need=_short)
        except Exception as _ee:
            logger.warning(f"[Guard] 紧急回收不可用: {_ee}")

        if _got <= 0:
            # ⚠️ 一个 token 都没腾出来 → 别再发一次去撞同一堵墙。
            #    📌 重试的前提是「这次和上次不一样」，而这里没有任何不同。
            from core.context.guard import MSG_STILL_TOO_BIG as _M
            yield {"event": "final_result", "content": _M, "status": "SYS_IDLE"}
            return

        # ⚠️ 回收改的是 `memory.storage`，而 `context` 是这一轮**早就拼好**的
        #    那份快照 —— 不重新取的话，重试发出去的还是原来那么大。
        #    📌 **在一个快照上做完修改，然后把快照原样再发一次，
        #       等于什么都没修。**
        try:
            context = self.memory.get_full_context()
        except Exception:
            pass

        try:
            async for _e in _once():
                yield _e
        except _CWE as _ex2:
            logger.error(f"[Guard] 🔴 回收 {_got:,} token 之后仍然装不下 —— 拒发")
            yield {"event": "final_result", "content": _ex2.user_message,
                   "status": "SYS_IDLE"}

    def _recent_user_messages(self, limit: int = 6) -> list:
        """最近若干条**用户自己说的话**。给危险判定用。

        ⚠️ **只收 role == "user"** —— 模型的话、工具输出一律不进来。
        📌 照官方那句 `reasoning-blind by design`：判定器要核的是
           「动作 vs 用户意图」，而模型的推理正是**被核的对象**，不能自辩；
           工具输出则是**注入的载体**，让它进判定器等于把闸交给攻击者。
        """
        out = []
        try:
            for m in reversed(getattr(self.memory, "storage", []) or []):
                if getattr(m, "role", "") != "user":
                    continue
                c = getattr(m, "content", None)
                if isinstance(c, str) and c.strip():
                    out.append(c.strip())
                if len(out) >= limit:
                    break
        except Exception as e:
            logger.debug(f"[CmdClassifier] 取用户消息失败: {e}")
        return list(reversed(out))

    async def _auto_gate_verdict(self, action: str, params: dict,
                                 main_model: str = "") -> tuple:
        """auto 模式下这一步能不能自动放行。返回 `(auto_ok, gate_by, reason)`。

        ⭐⭐ 四态：
        ```
        A  不是 run_command            → auto_ok=True   零 token
        B  判定：安全                  → auto_ok=True   有 token
        C  判定：危险                  → auto_ok=False  弹窗，说清它要做什么
        D  判定：判不了                → auto_ok=False  弹窗，说清**为什么判不了**
        ```
        ⚠️ C 与 D 必须分开：C 说「它要删/装/改什么」，D 说「我读不了这个脚本」。
           📌 把 D 说成 C 是在冤枉一条可能完全无害的命令，
              而用户点几次之后就会开始无脑点 —— 那时这道闸就白做了。

        🔴 **失败方向一律朝 False（弹窗）倒**：没配判定模型、API 失败、
           取不到用户消息、判定器自己抛异常 —— 全部落 D。
           📌 同 `required_permissions()` 对未知 action 返回总闸那条：
              fail-safe 朝「多要一道」错，不朝「谁都不管」错。
        """
        if action != "run_command":
            return (True, "", "")            # A：不进判定
        try:
            from core.os_layer import cmd_classifier as _cc
            _v, _why = await _cc.classify(
                getattr(self, "provider", None),
                command=str((params or {}).get("command", "")),
                user_messages=self._recent_user_messages(),
                # ⚠️ 主模型 id 决定用**同厂**的哪个判定模型（models.classifier_for）。
                #    三级兜底：本轮实际用的 → orchestrator 上的 → provider 上的。
                #    📌 取不到也没关系：`classifier_for("")` 返回空串 = 不启用 = 落 D。
                main_model=str(main_model
                               or getattr(self, "target_model", "")
                               or getattr(self.provider, "target_model", "")),
            )
        except Exception as e:
            logger.warning(f"[CmdClassifier] 判定异常，按需要确认处理: {e}")
            return (False, "classifier", f"判定器异常（{type(e).__name__}）")
        if _v == _cc.ALLOW:
            return (True, "", "")            # B
        if _v == _cc.BLOCK:
            return (False, "classifier", _why or "这条命令与你的要求对不上")   # C
        return (False, "classifier", _why or "无法判断这条命令是否符合你的要求")  # D

    async def _execute_dsl_step(self, instr: dict, dispatcher, safety, used_model: str):
        """执行一条 DSL 指令，处理两段式确认流程（risk=1 直接放行，risk>=2 弹窗）。

        从原单次执行逻辑里抽出来，供：
          1. _handle_os_task 的"简单任务快路径"（单条 os_execute 调用）
          2. 的 Plan-Execute-Replan 循环（逐条执行 OS Skill 吐出的 dsl_plan）
        两处复用，避免confirm弹窗这套同步等待逻辑写两份。

        yields: 中间 UI 事件（os_action_confirm 等），调用方原样转发给上层。
        最后一定 yield 一个 {"_step_result": result} 哨兵，调用方据此取出
        最终执行结果，自己决定怎么回复用户/要不要继续下一步——这个函数本身
        不 yield final_result，不替上层做决策（实现约束2：只报状态不决策）。
        """
        import asyncio as _asyncio
        confirmed = True
        result = None

        # ⚠️ 每走一步给镜像租约续期。**这条不能省。**
        # 一次 GUI 自动化可能跑几分钟，租约不续就会到期，于是一个**完全正常**的
        # 长任务被记成"泄漏" —— 那是假阳性，而假阳性是 shadow 最坏的一种失败
        #（早先刚栽过：它不漏问题，但会训练出"这个报警不用看"）。
        _rt_lease_heartbeat(self)

        async for ev in dispatcher.execute(instr):
            if ev["type"] == "confirm_request":
                action    = ev["action"]
                risk      = ev["effective_risk"]
                p_summary = ev.get("params_summary", "")
                reason    = ev.get("reason", "")
                annotated = ev.get("annotated_image_path", "")
                resolved_instr = ev.get("_resolved_instr", instr)

                _confirm_ev = _asyncio.Event()
                _user_choice = [None]
                _loop = _asyncio.get_running_loop()

                def _on_confirm():
                    _user_choice[0] = True
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                def _on_always():
                    _user_choice[0] = "always"
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                def _on_cancel():
                    _user_choice[0] = False
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                # ⭐⭐ [B] **auto 走一个【不同的】回调，不是复用 `_on_confirm`。**
                #    🔴 复用的话，「用户亲自点了同意」和「auto 替用户点了」在这一层
                #       完全无法区分 —— 而它们对模型是两件事：
                #       前者是一次真实的人类判断，后者是"这一轮压根没人被问过"。
                #    📌 **一个字段如果要回答「谁批的」，那么两个批准者就必须
                #       走两条能被分辨的路。**
                def _on_auto():
                    _user_choice[0] = "auto"
                    _loop.call_soon_threadsafe(_confirm_ev.set)

                # 对 run_command/file_write，把原始内容传给弹窗用于代码预览
                _raw_params = resolved_instr.get("params", instr.get("params", {}))
                # ⭐ 这个动作是谁发起的 —— **UI 只多画一行，规则一个字不改**
                #    （2026-08-20：「其他的完全复用 main agent 的即可，
                #     本身怎么授权它就怎么授权」）。
                #    📌 而它必须**能被看出来**：一个后台执行体发起的写操作，
                #       如果在弹窗上和你自己那一轮长得一模一样，
                #       用户就没有办法判断"这是我刚让它做的吗"。
                _agent_label = current_agent_label()
                # ⭐⭐ auto 模式下的危险判定（四态）。**只在 auto 开着时才花这个钱** ——
                #    ask permission 模式本来就每条都问，判定一分钱不值。
                _auto_ok, _gate_by, _gate_why = True, "", ""
                try:
                    from core.os_layer import dsl as _dsl_g
                    if _dsl_g.auto_authorization_on():
                        _auto_ok, _gate_by, _gate_why = await self._auto_gate_verdict(
                            action, _raw_params, used_model)
                except Exception as _e_g:
                    # 🔴 连"要不要判"都判不了 → 当作需要确认（fail-safe）
                    logger.warning(f"[CmdClassifier] 闸前置异常: {_e_g}")
                    _auto_ok, _gate_by, _gate_why = False, "classifier", "判定前置异常"
                _reasons = list(ev.get("risk_reasons", []) or [])
                if _gate_why:
                    # ⭐ 复用弹窗现成的「风险原因」那一栏 —— 不新增 UI 通道。
                    #    📌 一个只多一行文字的需求，不该换来一条新的展示管线。
                    _reasons.append(f"[自动放行被拦下] {_gate_why}")
                yield {
                    "event": "os_action_confirm",
                    "action": action, "effective_risk": risk,
                    # ⚠️ **判据是「明确为 True 才放行」，不是「没被拦就放行」** ——
                    #    上游要是漏传了这个键，行为退化成"照常弹窗"，而不是静默执行。
                    "auto_ok": _auto_ok, "gate_by": _gate_by,
                    "risk_reasons": _reasons,
                    "params_summary": p_summary, "reason": reason,
                    "params_raw": _raw_params,
                    "annotated_image_path": annotated,
                    "agent_label": _agent_label,
                    "on_confirm": _on_confirm, "on_always": _on_always,
                    "on_cancel": _on_cancel, "on_auto": _on_auto,
                }

                from core.runtime import inbox as _ib6
                # ⭐⭐ Subagent发起的确认**只认这个弹窗自己的回应**（+ 超时兜底）。
                #    理由见 `wait_confirm_or_user_message` 的 docstring：
                #    📌 一个「取消」的信号，必须来自它要取消的那件事的同一条注意力。
                _oc6 = await _ib6.wait_confirm_or_user_message(
                    _confirm_ev, 300,
                    cancel_on_user_message=not _agent_label)
                if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                    # 🔴 **告诉 UI 把那个弹窗收掉**。
                    #    用户改口说话 / 干等超时，这两条路上**没有人点过按钮**，
                    #    而弹窗只在按钮里 `close()` —— 于是它活了下来，
                    #    屏上写着"待授权"，按钮指向一个已经结束的等待。
                    # 📌 **一个只能被自己的按钮关掉的弹窗，一定会在
                    #    「不是按按钮」的那些结束路径上活下来** ——
                    #    而那些路径恰恰是用户没在看它的时候。
                    try:
                        yield {"event": "confirm_dismiss",
                               "why": str(getattr(_oc6, "value", _oc6))}
                    except Exception:
                        pass
                    # ⭐⭐ 用户没点按钮 —— 要么改口说话了，要么干等超时。
                    #    两者都 **不执行**，但**要告诉模型的话不同**，所以不许压成一个布尔。
                    #    📌 「用户改口说别做了」和「等了五分钟没人管」在结果上都是不执行，
                    #       语义完全不同 —— 同 ActionAttempt 那条：一个字段不许表达两个现实。
                    _user_choice[0] = False

                choice = _user_choice[0]
                confirmed = choice in (True, "always", "auto")

                if not confirmed:
                    await dispatcher.execute_after_confirm(resolved_instr, confirmed=False)
                    # ⚠️ 原来这里恒定是「用户已取消」——**那在两种情况下都是错的**：
                    #    干等超时时没人取消过；用户改口说话时也没"取消"，
                    #    那是**换了个话题**。
                    #    📌 **一句给模型看的话，宁可承认「不知道为什么」，
                    #       也不许替用户编一个用户没做过的动作。**
                    # ⭐ [A] 显式点「拒绝」—— 改造前这里只有"用户已取消"五个字，
                    #    而旁边两条（改口 / 超时）都是完整句。
                    #    📌 三条出口里，**真正的"用户拒绝"反而是说得最不清楚的那条** ——
                    #       而它是唯一一条模型必须停手的。
                    #    ⚠️ 必须明说「不要重试」：这是一次人的判断，不是一次故障。
                    _err6 = ("Denied by the user: they looked at this action and "
                             "said no. This is a decision, not a failure - do NOT "
                             "retry it and do NOT look for a way around it. "
                             "Tell the user plainly that you did not do it, and "
                             "let them decide what happens next.")
                    if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                        _err6 = _ib6.cancelled_by_user_message_note()
                    elif _oc6 == _ib6.ConfirmOutcome.TIMEOUT:
                        _err6 = ("Not executed: the confirmation dialog timed out "
                                 "after 300s with no answer. Nobody cancelled it — "
                                 "the user simply never responded.")
                    result = {"ok": False, "action": action, "effective_risk": risk,
                              "is_control_flow": False, "data": {}, "summary": "",
                              # ⭐ 结构化那一份 —— 消费方不该去解析一段英文
                              "authorized_by": (
                                  "user_denied"
                                  if _oc6 == _ib6.ConfirmOutcome.CONFIRMED
                                  else ("cancelled_by_user_message"
                                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE
                                        else "confirm_timeout")),
                              "error": _err6}
                    break

                result = await dispatcher.execute_after_confirm(resolved_instr, confirmed=True)
                if isinstance(result, dict):
                    result["authorized_by"] = {
                        True: "user_once", "always": "user_always", "auto": "auto",
                    }.get(choice, "user_once")
                # 预授权在执行成功后写入，且按参数范围（scope_key）限定粒度，
                # 防止"允许写A文件"扩散到"允许写B文件"，也防止执行失败仍留下预授权
                # ⚠️⚠️ **`auto` 不许写预授权。** 它是「这一轮不问」，
                #    而预授权是「以后都不问」—— 📌 一个临时的豁免如果顺手落成永久的，
                #    那么关掉 auto 之后它还在，而用户以为自己关掉了。
                if choice == "always" and result.get("ok"):
                    from core.os_layer.dispatch import _derive_auth_scope
                    _scope = _derive_auth_scope(action, resolved_instr.get("params") or {})
                    safety.pre_authorize(action, risk, _scope)
                break

            elif ev["type"] == "result":
                result = ev
                break

        if result is None:
            result = {"ok": False, "error": "调度器无返回"}

        # ⭐⭐⭐ [2026-08-24] **OS 动作产生的图，一律上屏。**
        #
        # 🔴 问题：`computer_use → screenshot` 把图**存进了 os_audit，但一个像素都没给用户看** ——
        #    实测里 Nano 只回了一句「截图保存在 shot_xxx.png」，
        #    用户完全不知道它到底看到了什么。
        # ⭐ 用户的判据（原话）：「**不管是不是凭据、落盘不落盘，这两种 UI 上都要体现**，
        #    不然用户不知道 nano 看到了什么 —— 这跟 pill 显示、可下拉是一个道理」。
        #    📌 **「Nano 此刻看到/在做什么」是一个整体，不该按实现细节（存不存盘）分叉。**
        # ⚠️ 顺带纠正一个不对称：`look_at_screen` **上屏但不落盘**，
        #    `computer_use.screenshot` **落盘但不上屏** ——
        #    📌 两条路都叫「截图」，却各缺对方的一半，而用户要的恰恰是重合的那部分。
        #
        # ⚠️ 走**同一个** `screenshot_preview` 事件（它在 `_CHAT_EVENTS_EPHEMERAL` 里：
        #    重启不回放，2026-08-13 已定）—— 不新增通道。
        # ⚠️ **缩到 1568 再 base64**：原图 2MB 的 PNG 变成 2.7MB 的 data URI 塞进 DOM
        #    是纯浪费，而这里只是给人看一眼。
        # ⚠️ 整段吞异常：它是**展示**，📌 一个用来展示的动作不许成为失败源
        #    （那条，本项目栽过六次）。
        try:
            # 🔴 [2026-08-24] **只上屏「截图动作」产的那张，
            #    定位（locate）那张不上屏。**
            #    第一版把 `locate.screenshot_ref` 也画了出去 ——
            #    结果一轮里四张几乎一模一样的全屏图刷过去。
            #    「虽然这确实是事实，但是 **UI 污染太严重**」。
            #    📌 **真实不等于该显示** —— 一堆长得一样的图不会让用户多知道一件事，
            #       只会把真正要看的那一张淡化掉。
            # ⭐ 分界很清楚：
            #    · `screenshot` 动作   —— Nano **主动看了一眼**，用户该看见  ✅
            #    · locate 的截图   —— 是执行一个点击的**内部过程**，不上屏  ❌
            #      （它仍然落盘进 os_audit，凭据一张没少；确认弹窗里的**标注图**也照旧显示）
            _img_p = ""
            _data = result.get("data") if isinstance(result, dict) else None
            if isinstance(_data, dict):
                _img_p = str(_data.get("path") or "")
            if _img_p and _img_p.lower().endswith(".png") and os.path.exists(_img_p):
                import base64 as _b64
                from PIL import Image as _Im
                import io as _io
                _im = _Im.open(_img_p).convert("RGB")
                if _im.width > 1568:
                    _im = _im.resize((1568, max(1, int(_im.height * 1568 / _im.width))),
                                     _Im.LANCZOS)
                _buf = _io.BytesIO(); _im.save(_buf, "PNG")
                # 🔴 这里第一版写的是 `action` —— **那个名字只在确认分支里绑定**
                #    （`action = ev["action"]`）。而 `screenshot` 是 risk=1、**不弹窗**，
                #    走的正好是没绑定的那条路 → `NameError`，
                #    而它会被下面那个 `except` **吞成一条 debug 日志** ⇒
                #    表现就是「图永远不出现」。
                # 📌 跟启动恢复那条 `self._startup_…` 同一个形状：
                #    **一条被吞掉的 NameError，表现成的是「功能没生效」，
                #    而不是「有人写错了变量」。**
                # ⭐ 改读 `instr` —— 它是入参，**每一条路上都在**。
                yield {"event": "screenshot_preview",
                       "png_b64": _b64.b64encode(_buf.getvalue()).decode(),
                       "purpose": str((instr or {}).get("action") or "screenshot")}
        except Exception as _e_shot:
            logger.debug(f"[OS] 截图上屏跳过（不影响执行）: {_e_shot}")

        yield {"_step_result": result}

    async def _replan_os_skill(self, skill_name: str, original_args: dict,
                                completed_steps: list, failed_instr: dict,
                                failed_result: dict) -> list | None:
        """OS Skill 执行中途定位失败，带着失败上下文重新调用 Skill 的 run()
        拿到更新后的 dsl_plan。

        返回新的 dsl_plan（list[dict]）；Skill 没正确返回则返回 None，
        由调用方决定走熔断话术。
        """
        import json as _json
        os_context = {
            "completed_steps": completed_steps,
            "failed_step": failed_instr,
            "failure_reason": failed_result.get("error", ""),
            "locate_status": failed_result.get("locate_status") or
                             (failed_result.get("data") or {}).get("locate_status", ""),
        }
        replan_args = dict(original_args or {})
        replan_args["os_context"] = _json.dumps(os_context, ensure_ascii=False)
        raw_result = await self.registry.execute(skill_name, replan_args)
        if raw_result is None or not hasattr(raw_result, "data"):
            logger.warning(f"[OS-Replan] Skill「{skill_name}」replan 调用未返回有效 SkillResult")
            return None
        plan = (raw_result.data or {}).get("dsl_plan")
        if not isinstance(plan, list):
            logger.warning(f"[OS-Replan] Skill「{skill_name}」replan 后 dsl_plan 不是合法列表")
            return None
        return plan

    async def _final_answer_or_fallback(
        self, *, facts: str, fallback: str, base_guide: str,
        used_model: str, log: str, status: str = "SYS_IDLE", extra: dict | None = None,
    ):
        """让模型用自己的话说一句收尾，模型不可用时退回 `fallback`。

        ⭐ 提出来的原因：`_run_os_skill_plan_loop` 里有 5 个出口形状完全一样
           —— 先设兜底、跑 `_stream_final_answer`、拿不到就用兜底、最后发终端事件。
           抄 5 遍的代价不是行数，是**5 份各自会漂移的兜底逻辑**。

        ⚠️ `fallback` **保留中文固定文案，这是对的** ——
           它只在模型不可用时才登场，而那时没有任何东西能生成它。
           📌 豁免第④条（模型故障兜底）。**能被模型说的话才归模型**。

        ⚠️ `facts` 用英文（注入模型的文本一律英文），语言由
           `language_clause()` 决定 —— 提示词里不写死任何一种语言。
        """
        content = fallback
        self._last_stream_final_bid = None
        try:
            async for _ev in self._stream_final_answer(
                self._build_pipeline_context(),
                base_guide + f"""

{facts} {_language_clause("your reply")}""",
                task_type="结果总结",
            ):
                yield _ev
            content = self._last_stream_final_text or fallback
        except Exception as _e:
            logger.warning(f"[L12] 收尾话术生成失败，退回兜底: {_e}")
            content = fallback
            self._last_stream_final_bid = None
        self.memory.add_message("assistant", content)
        _ev_out = {"event": "final_result", "content": content, "model": used_model,
                   "status": status, "log": log,
                   "current_skill": None, "rag_hit": False, "full_file_hit": False,
                   "block_id": self._last_stream_final_bid}
        # ⚠️ 用来透传调用点自己的字段（rag_hit / full_file_hit 这类）——
        #    📌 提取公共形状时，**别把调用点的差异一起抹平**：
        #       抹平的那些字段不会报错，只会悄悄变成默认值。
        _ev_out.update(extra or {})
        yield _ev_out

    async def _run_os_skill_plan_loop(self, skill_name: str, initial_plan: list,
                                       original_args: dict, dispatcher, safety,
                                       used_model: str, base_guide: str, realtime_callback):
        """补丁C.2-C.4：Plan-Execute-Replan 循环。仅在 _handle_os_task 内部持有，
        不是全局编排器（补丁C.5：回流主体是"单个 OS Skill 执行中途的视觉纠偏"，
        不是"任务编排"，不会重蹈已删除的多步 Plan 覆辙）。

        逐条执行 initial_plan；遇到 NOT_FOUND/AMBIGUOUS/OCCLUDED 时（执行层
        的三级降级已经在 dispatcher 内部做过，这里收到的已经是降级后仍失败的
        结果）调用 _replan_os_skill 重新规划剩余步骤；step/replan 次数用
        safety 已有的计数器熔断。
        """
        MAX_STEPS = 30
        plan_queue = list(initial_plan or [])
        completed_steps: list = []

        while plan_queue:
            if safety.increment_step() > MAX_STEPS:
                async for _ev in self._final_answer_or_fallback(
                        facts=("The task was stopped because it needs more steps than one run allows. ""Nothing further was attempted. Tell the user, and suggest splitting it ""into a few smaller requests."),
                        # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                        fallback="这个任务步骤太多，超过了单次操作上限，我先停下来——可以拆成几次跟我说。",
                        base_guide=base_guide, used_model=used_model,
                        log="OS Skill：步数熔断。"):
                    yield _ev
                return

            instr = plan_queue.pop(0)
            result = None
            async for ev in self._execute_dsl_step(instr, dispatcher, safety, used_model):
                if "_step_result" in ev:
                    result = ev["_step_result"]
                else:
                    yield ev

            if result.get("is_control_flow") and result.get("data", {}).get("reason") == "USER_ABORT":
                # 软急停已删除，模型不再被教导发这个 reason。这里保留只作兜底：
                # 万一模型自己发了，就当"停掉本次 plan"处理，不再污染会话级状态。
                async for _ev in self._final_answer_or_fallback(
                        facts=("The user asked to stop, so the operation was halted here. ""Acknowledge briefly - do not re-offer to continue unless they ask."),
                        # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                        fallback="好，这次的操作我停在这儿了。",
                        base_guide=base_guide, used_model=used_model,
                        log="OS Skill：模型请求终止本次 plan。"):
                    yield _ev
                return

            if result.get("ok"):
                completed_steps.append(instr)
                continue

            if result.get("aborted") or result.get("error") == "用户已取消":
                async for _ev in self._final_answer_or_fallback(
                        facts=("The user asked to stop mid-operation, so the current step was cut short ""and did not finish. Acknowledge briefly and say the step was left ""incomplete."),
                        # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                        fallback=("好，我停手了——刚才那个操作没做完就中断了。" if result.get("aborted")
                           else "好，这步我不执行了，整个任务也先停在这里。"),
                        base_guide=base_guide, used_model=used_model,
                        log="OS Skill：用户中断。"):
                    yield _ev
                return

            _locate_status = result.get("locate_status") or (result.get("data") or {}).get("locate_status")
            if _locate_status in ("NOT_FOUND", "AMBIGUOUS", "OCCLUDED"):
                if safety.increment_replan() > 3:
                    async for _ev in self._final_answer_or_fallback(
                            facts=(f"The step {instr.get('reason') or instr.get('action')!r} failed after "f"several retries, so the task stopped there. It may be outside what can "f"be done automatically. Tell the user, and offer either that they do "f"this one step by hand, or that they describe the target more precisely."),
                            # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                            fallback=(f"这一步「{instr.get('reason') or instr.get('action')}」我反复试了几次"
                               "都没成功，可能这个任务超出我现在的能力范围了——可以麻烦你手动完成这一步，"
                               "或者换个说法告诉我具体在哪。"),
                            base_guide=base_guide, used_model=used_model,
                            log="OS Skill：连续失败熔断。"):
                        yield _ev
                    return

                yield {"event": "thinking", "log": "这一步没成功，正在重新规划剩余步骤...",
                       "status": "CORE_THINKING", "model": used_model,
                       "current_skill": "OSTask", "rag_hit": False, "full_file_hit": False}
                new_plan = await self._replan_os_skill(
                    skill_name, original_args, completed_steps, instr, result
                )
                if new_plan is None:
                    async for _ev in self._final_answer_or_fallback(
                            facts=("A step failed and no viable replanning was found, so the task stopped ""there. Say so plainly - do not pretend anything is still in progress."),
                            # ⚠️ 兜底保留中文原文 —— 它只在模型不可用时登场（豁免④）
                            fallback="这一步没成功，我也没能重新规划出下一步该怎么做，先停在这里了。",
                            base_guide=base_guide, used_model=used_model,
                            log="OS Skill：replan 失败。"):
                        yield _ev
                    return
                plan_queue = new_plan
                continue

            # 其他失败（权限拒绝/执行异常等）——转写成人话告知，不再继续
            raw_error = result.get("error", "未知原因")
            self._last_stream_final_bid = None
            try:
                async for _ev in self._stream_final_answer(
                    self._build_pipeline_context(),
                    base_guide + (
                        # 🔴 改造前这是一段**中文提示词**，而且写死了「用简洁自然的
                        #    **中文**」—— 用户把界面切成英文，它照样命令模型说中文。
                        #    📌 提示词一律英文；语言由 language_clause() 决定，
                        #       **提示词里不写死任何一种语言**。
                        f"\n\nA system operation failed. Internal error, "
                        f"for your reference only:\n{raw_error}\n\n"
                        "Tell the user briefly and naturally that it did not work. "
                        "Do not copy the internal error text; explain roughly what "
                        "went wrong in terms they can understand. "
                        + _language_clause("your reply")
                    ),
                    task_type="结果总结",
                ):
                    yield _ev
                content = self._last_stream_final_text or f"这次操作没成功（{raw_error}）。"
            except Exception:
                content = "这次操作没成功，具体原因暂时说不清楚，可以再试一次或换个说法。"
                self._last_stream_final_bid = None
            self.memory.add_message("assistant", content)
            yield {"event": "final_result", "content": content, "model": used_model,
                   "status": "SYS_IDLE", "log": f"OS Skill 执行失败：{raw_error}",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False,
                   "block_id": self._last_stream_final_bid}
            return

        # 全部步骤跑完
        content = "好，这个任务我做完了。"
        self._last_stream_final_bid = None
        try:
            async for _ev in self._stream_final_answer(
                self._build_pipeline_context(),
                base_guide + (
                    # ⚠️ 同上：不在提示词里写死语言。
                    f"\n\nThe system operation task is fully complete "
                    f"({len(completed_steps)} steps). "
                    "Tell the user it is done, briefly and naturally; do not "
                    "recite every step. "
                    + _language_clause("your reply")
                ),
                task_type="结果总结",
            ):
                yield _ev
            content = self._last_stream_final_text or "好，这个任务我做完了。"
        except Exception:
            self._last_stream_final_bid = None
        self.memory.add_message("assistant", content)
        yield {"event": "final_result", "content": content, "model": used_model,
               "status": "SYS_IDLE", "log": f"OS Skill 执行完成（共{len(completed_steps)}步）。",
               "current_skill": "OSTask", "rag_hit": False, "full_file_hit": False,
               "block_id": self._last_stream_final_bid}

    def _get_canary(self):
        """懒加载 CanarySelfCheck，复用 VisionLocator/审计目录，避免重复初始化。
        初始化失败时把 self._canary 标记成 False（不是 None），下次直接跳过、
        不重试，避免每次定时器触发都重新尝试一次必然失败的初始化。"""
        if self._canary is not None:
            return self._canary if self._canary else None
        try:
            import json
            import pathlib
            from core.os_layer.canary import CanarySelfCheck
            from core.os_layer.executor_vision import VisionLocator
            from core.os_layer.audit import get_audit_logger
            audit = get_audit_logger()
            vision = VisionLocator(
                provider=self.provider, screenshot_dir=audit.screenshot_dir,
                model_override=None,
            )
            cfg_path = pathlib.Path(__file__).parent.parent / "config" / "os_config.json"
            canary_cfg = {}
            try:
                if cfg_path.exists():
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                    canary_cfg = data.get("canary") or {}
            except Exception:
                pass
            self._canary = CanarySelfCheck(vision, audit.screenshot_dir.parent, canary_cfg)
        except Exception as e:
            logger.warning(f"[Canary] 初始化失败，本次会话跳过自检: {e}")
            self._canary = False
        return self._canary if self._canary else None

    async def maybe_run_canary(self) -> None:
        """Canary self-check entry, periodically called by app.py.

        This method decides whether to run a lightweight self-check and whether
        the result should be escalated to a natural user-facing notice.
        All errors are swallowed so canary checks never affect the main flow.
        """
        try:
            canary = self._get_canary()
            if canary is None:
                return
            # ⚠️⚠️ **对答案已退役（2026-08-07），这里刻意不再调 `_rt_lease_compare_busy`。**
            #
            # 它在观测期的任务已经完成：量出了泄漏频率（16 次观测 13 次泄漏 / 61 分钟）、
            # 证实了止血有效。而后续把租约改成**一整段 GUI 操作**的粒度之后，
            # 旧 bool 仍然是**单步**粒度 —— 两者寿命不同了。
            #
            # 📌 **两个寿命不同的东西之间的"分歧"不是缺陷，是设计。**
            #    继续对答案，只会在每次两步之间的空档报一条"镜像没归还"，
            #    而那正是**假阳性** —— 早先、早先、早先已经各防过一次，
            #    这是第四次。假阳性不漏问题，但会训练出"这个报警不用看"。
            #
            # 📌 更一般的判据：**shadow 的对答案，只在两边建模同一个粒度时才有意义。**
            #    一旦新实现有意做得比旧的更细/更粗，旧的就不再是 oracle，该退役了。

            # ⭐⭐ 这一行就是 `_os_task_busy` 泄漏的**真修**。
            #
            # 旧写法 `canary.should_run(self._os_task_busy)` 的问题不是"这个 bool 会写错"，
            # 是**它没有过期概念** —— 一旦漏了一次 False，canary 就永久停摆且一声不响
            # （实测：按一下 Escape → 61 分钟没跑过，16 次观测 13 次判为泄漏）。
            # 租约靠 TTL 自愈：**不再依赖任何调用点记得配对**。
            #
            # ⚠️ 用的是 `machine_is_free()` 而**不是** `nano_may_touch_os()`
            #    （早先的设计原先写的是后者，是错的，已更正）——
            #    后者在"Nano 自己持有"时返回 True，照它写 canary 会在 GUI 自动化
            #    跑到一半时抢前台焦点，正是 canary 设计约束 ② 明令禁止的那件事。
            #
            # ⭐ 顺带白拿一条旧 bool 给不了的：**用户在用电脑时 canary 也不跑了**。
            #    旧 bool 只知道 Nano 忙不忙，不知道用户正在打字。
            if not canary.should_run(not _rt_machine_is_free()):
                return
            result = await canary.run_once()
            if result.get("escalate"):
                _raw = (
                    f"Screen target-location check: success rate {result['rate']:.0%}; "
                    f"below expectation for {result['consecutive_low']} consecutive checks."
                )
                try:
                    _prompt = (
                        f"You just completed a system self-check and found that screen target-location "
                        f"success rate has stayed low ({result['rate']:.0%}, "
                        f"{result['consecutive_low']} consecutive checks). "
                        f"Tell the user naturally and briefly that if recent screen operations keep failing, "
                        f"they can tell you or try restarting. "
                        f"Do not mention words like system self-check or canary."
                    )
                    content, _ = await self.provider.chat_without_tools(
                        context=[{"role": "user", "content": _prompt}],
                        system_guide="You are Nano, a desktop assistant. Be direct, brief, and natural.",
                    )
                    content = content.strip() if content else _raw
                except Exception:
                    content = _raw
                # 定死的一条：历史/事件记录的所有权只能属于一个层级，调用方和 emitter
                # 不能同时写。这里【曾经】自己 add_message 一次、再 _push_callback 一次，
                # 而 _proactive_push 内部又写一次 —— 同一条告警进历史两次（外部评审推演出来的，
                # 已回代码核实属实）。现在统一交给出口，这里只负责触发。
                logger.warning(f"[Canary] Escalated notice to user: {content}")
                try:
                    from core.health import get_system_events
                    get_system_events().add(f"Screen locate self-check degraded: {content}")
                except Exception:
                    pass
                if self._push_callback:
                    await self._push_callback(content)
        except Exception as e:
            logger.warning(f"[Canary] maybe_run_canary failed, ignored: {e}")

    # _handle_os_task 已删除（OS 双循环合一，0628）：OS 不再有独立循环，
    # os_execute 始终注入主 ReAct 循环，由 _run_react_loop + _execute_one_tool_call
    # 的 os_execute 分支统一执行。

    # ── Skill 生命周期管理 ───────────────────────────────────────────────

    def _recent_skill_events(self, target_skill: str | None) -> list[str]:
        """扫最近 15 条 memory，找和某个 Skill 相关的丢弃/删除/禁用/启用/部署
        记录。从 _skill_not_found_reply 里抽出来，供"Skill 还在但状态有变化"
        （比如已禁用）的分支复用——不能只有"完全找不到"才查历史，"找到了但
        状态变了"同样需要查，否则模型会在"谁/为什么"这类追问上编答案。"""
        _recent_storage = self.memory.storage[-15:]
        _events: list[str] = []
        for _msg in reversed(_recent_storage):
            _body = getattr(_msg, "content", "") or ""
            if target_skill and target_skill not in _body:
                continue
            if any(k in _body for k in ("已丢弃", "已删除", "已禁用", "已安装", "已部署", "已启用")):
                _events.append(_body[:120])
            if len(_events) >= 3:
                break
        return _events

    async def _skill_not_found_reply(self, target_skill: str | None, base_guide: str,
                                       realtime_callback) -> tuple[str, str]:
        """Generate a consistent factual reply when a Skill is not found.

        All branches that need to explain a missing Skill should use the same
        structured fact source, so Nano does not give inconsistent answers
        depending on whether the user tried to run, inspect, or update the Skill.
        """
        _events = self._recent_skill_events(target_skill)

        _on_disk_deleted = (
            target_skill and hasattr(self.registry, "list_deleted_skills")
            and target_skill in self.registry.list_deleted_skills()
        )

        _facts = (
            f"Target Skill name: {target_skill or '(not specified by the user)'}\n"
            f"Current system status: not registered / does not exist\n"
        )
        if _on_disk_deleted:
            _facts += (
                "Confirmed fact from disk backup directory, high confidence: "
                "this Skill did exist before and has been deleted. "
                "Its backup is under the skills/deleted/ directory.\n"
            )
        if _events:
            _facts += "Recent related history, newest first:\n" + "\n".join(f"- {e}" for e in _events)
        elif _on_disk_deleted:
            _facts += "Conversation history does not contain the exact deletion time or operator."
        else:
            _facts += "No recent related history was found. It may never have been created, or the record may be outside visible history."

        _guide = (
            base_guide
            + f"\n\nThe user is asking about or trying to call a Skill, but the Skill is currently not in the system. "
              f"Known facts:\n{_facts}\n\n"
            "Tell the user this fact in 1-3 natural sentences. Use the user's language when obvious. "
            "Do not invent anything that is not in the fact list, especially exact time, operator, audit log, "
            "or any fake source of truth. The fact list contains text snippets only and may not contain timestamps. "
            "Never invent a realistic-looking date or time.\n"
            "If the facts include the disk-backup confirmation, trust it. It is more reliable than conversation history. "
            "Do not say the Skill seems to have never existed just because conversation history did not mention it. "
            "In that case, only say the exact deletion time/operator is unclear, while the deletion itself is confirmed.\n"
            "Only if there is no disk confirmation and no related conversation history, say that the Skill may never have been created "
            "or that no related operation record was found.\n"
            "Finally, briefly tell the user what they can do next. Only use these two options; do not invent UI buttons, "
            "import features, or one-click restore features:\n"
            "  1) Ask Nano to create a new Skill for the same capability, clearly saying this will generate new code rather than restore the old version.\n"
            "  2) If the user is technical and wants the original code, tell them they can manually move the backup file from skills/deleted/ back to skills/."
        )
        try:
            content, model, _ = await self.provider.chat_without_tools_or_call(
                self._build_pipeline_context(), _guide,
                status_callback=realtime_callback,
                model_override=None,
            )
        except Exception:
            content = ""
            model = "BYPASS_RAW"
        content = (content or "").strip()
        if not content:
            content = (
                (f"没有找到 Skill「{target_skill}」。" if target_skill else "你想找的 Skill 我没有匹配上。")
                + "可以说「列出所有 Skill」看看当前有哪些。"
            )
            model = "BYPASS_RAW"
        return content, model

    # 报错跟读检测 —— 用于判断"上一个工具结果疑似报错 + 用户修复回应"
    _ERROR_FOLLOWUP_MAX_LEN = 80   # 放宽到 80，允许用户描述具体怎么修
    _ERROR_FIX_INTENT_WORDS = (
        "修复", "修一下", "修改", "改代码", "改一下", "fix", "update",
        "纠正", "更新代码", "重写", "调整代码", "纠错", "重新生成",
    )
    _ERROR_MARKERS = (
        "失败", "错误", "出错", "崩溃", "异常", "故障",
        "error", "exception", "traceback", "not defined", "nameerror",
        "typeerror", "valueerror", "keyerror", "attributeerror",
    )

    # 短确认词白名单：这些词独立出现时，表示"好，你去修"的意思。
    # 不要用长度做判断，"查天气"和"好的"一样短但含义完全不同。
    _SHORT_ACK_WORDS = frozenset({
        "可以", "好", "好的", "行", "嗯", "ok", "okay", "yeah", "是的", "对",
        "修一下", "改一下", "试试", "再试", "重试", "重新来", "再来",
        "再来一次", "再试一次", "没问题", "帮我修", "帮我改", "你来修",
        "重新写", "重写", "你修", "你改", "那你修", "那修一下",
    })

    def _detect_skill_error_followup(self, query: str) -> tuple[str, str] | None:
        """检测"最近一次工具结果疑似报错 + 用户修复意图"。

        命中条件（三者都满足）：
          1. self.memory.storage 里最近一条 role=="tool" 的消息内容
             包含错误标记（失败/错误/故障/Error等）
             且该条 tool 内容里包含 _last_called_skill 的名字（防止其他工具结果误命中）
          2. self._last_called_skill 存在且该 Skill 仍在 registry 里
          3. query 命中短确认词白名单 OR 包含显式修复动词关键词
             短确认词："好的/可以/修一下" 等明确的同意/授权修复
             修复动词："修/改/fix/纠正/更新/重写"等
             普通短句（"查天气""列文件"）不命中任何一条

        返回 (目标 Skill 名, 最近一次工具报错内容) 用于走 _generate_skill_update，
        不命中返回 None。
        """
        q = query.strip()
        _q_bare = q.rstrip("。！!?？.").strip()
        is_short_ack = q in self._SHORT_ACK_WORDS or _q_bare in self._SHORT_ACK_WORDS
        has_fix_intent = any(w in q.lower() for w in self._ERROR_FIX_INTENT_WORDS)
        if not (is_short_ack or has_fix_intent):
            return None

        # 目标冲突检测：若 query 里明确提到了另一个 Skill，不让 correction 抢
        _se = getattr(self, "_last_skill_error", None)
        _last_error_skill = (_se or {}).get("skill", "")
        if _last_error_skill and not is_short_ack:
            try:
                _all_skill_names = set(self.registry.skills.keys())
                for _sn in _all_skill_names:
                    if _sn != _last_error_skill and _sn.lower() in q.lower():
                        return None  # 用户明确提到了另一个 Skill，不走 correction
            except Exception:
                pass

        # 优先路径：结构化错误状态（精确绑定，不受中间消息干扰）
        if _se and not _se.get("consumed"):
            _se_skill = _se.get("skill", "")
            _se_error = _se.get("error", "")
            if _se_skill:
                try:
                    _all_s = set(self.registry.skills.keys())
                    if _se_skill in _all_s:
                        return _se_skill, _se_error
                except Exception:
                    pass

        # 降级路径：memory 扫描（兼容结构化状态未设置的情况）
        if not self._last_called_skill:
            return None

        try:
            all_skills = set(self.registry.skills.keys())
        except Exception:
            return None
        if self._last_called_skill not in all_skills:
            return None

        # 从后往前找最近一条 role=="tool" 的消息（最多看 20 条）。
        # 不在遇到 user 消息时立刻停止，因为用户可能在崩溃后说过一两句话，
        # 真正的工具报错会被那些 user/assistant 消息隔开，但仍然有效。
        last_tool_content = None
        try:
            for msg in list(reversed(self.memory.storage))[:20]:
                if getattr(msg, "role", None) == "tool":
                    last_tool_content = getattr(msg, "content", "") or ""
                    break
        except Exception:
            return None

        if not last_tool_content:
            return None

        # 验证这条 tool 结果确实来自 _last_called_skill，避免其他工具结果干扰。
        # 错误格式通常是 "工具【SkillName】执行故障: ..."，也兼容 skill 名直接出现的情况。
        _skill_name = self._last_called_skill
        if (f"【{_skill_name}】" not in last_tool_content
                and _skill_name.lower() not in last_tool_content.lower()):
            return None

        lowered = last_tool_content.lower()
        if any(marker.lower() in lowered for marker in self._ERROR_MARKERS):
            return _skill_name, last_tool_content

        return None

    def _detect_os_locate_followup(self, query: str) -> dict | None:
        """检测"上一次 OS 视觉定位失败 + 当前是简短纠正"。和
        _detect_skill_error_followup 同一设计：短句缺信息，靠"上一轮在干嘛"
        的待定状态续接，不依赖无状态的顶层分类器理解上下文。

        命中条件：
          1. self._last_os_locate_failure 存在（上一轮 click 等定位失败过）
          2. 当前 query 很短（复用 _ERROR_FOLLOWUP_MAX_LEN 同一个阈值），
             不像一个新的、独立的实质性请求
        """
        if len(query) > self._ERROR_FOLLOWUP_MAX_LEN:
            return None
        return getattr(self, "_last_os_locate_failure", None)

    async def _generate_skill_update(self, query: str, target_skill: str, base_guide: str, realtime_callback,
                                       error_context: str = ""):
        source = self.registry.get_skill_source(target_skill, include_disabled=False) if hasattr(self.registry, "get_skill_source") else None
        if not source:
            yield {"event": "final_result", "content": f"未找到可修改的已启用 Skill「{target_skill}」。", "model": "LOCAL", "status": "SYS_IDLE", "log": "Skill 修改失败：目标不存在。", "current_skill": None, "rag_hit": False, "full_file_hit": False}
            return

        original_code = source.get("code", "")
        extra = (
            "\n[Skill Update Mode]\n"
            f"Target Skill: {target_skill}\n"
            "The user wants to modify an existing Skill. "
            "You must output complete replacement code. "
            "The filename must remain the target Skill class name. "
            "Do not create a new Skill and do not rename it.\n"
            "Current source code:\n"
            f"{original_code}\n"
        )
        if error_context:
            extra += (
                "\n[Most Recent Execution Result For This Skill — Likely Error To Fix]\n"
                f"{error_context}\n\n"
                "The user's current message may be a short response such as 'okay' or 'fix it', "
                "and it refers to the error above. "
                "Locate and fix the specific cause of that error. "
                "Do not perform unrelated style refactors. "
                "Keep get_spec(), get_manifest(), class name, and self.name unchanged unless the error is directly related to them.\n"
                "[If you also find other real bugs]\n"
                "Because you are already reading the source code, if you find other genuine bugs unrelated to the reported error, "
                "such as hardcoded column names or values that conflict with the exploration-confirmed facts, you may fix them too. "
                "However, the description field must truthfully list all changes. "
                "Do not mention only the error fix while silently changing other behavior, because the user sees the description, not the diff.\n"
            )
            # 持久语义记忆v3：「工具失败修复前」检索注入的窄入口。不暴露成
            # 工具，直接拼进修复prompt；没命中相关历史经验就不加任何内容。
            try:
                from core.semantic_memory import retrieve_correction_hints
                _correction_hint = await retrieve_correction_hints(target_skill, error_context)
                if _correction_hint:
                    extra += f"\n{_correction_hint}\n"
            except Exception as e:
                logger.warning(f"[SemanticMemory] 修复阶段检索注入失败（跳过）: {e}")
        async for step in self._generate_skill_with_writer(
            query,
            base_guide,
            realtime_callback,
            extra_instruction=extra,
            mode="update",
            target_skill=target_skill,
            change_summary=query,
            error_context=error_context,
        ):
            yield step

    # ── _handle_pending_action 已删除（· ②c · 2026-08-06）──────────
    # 117 行的标签分支 + 一个专用分类器 + 一个伪事件 `__pending_new_request__`
    # 用来从生成器里逃逸回常规路由。整套被一条 `skill_manage` Interaction 取代。
    # 处置逻辑现在在 `_handle_answer_interaction` 的 SKILL_MANAGE 分支里。

    def _check_skill_side_effects(self, skill_name: str) -> list[str]:
        """检查 Skill 是否有需要用户确认的副作用。

        返回需要确认的副作用描述列表;空列表表示无需确认(纯只读 Skill)。

        只有以下副作用需要确认(readonly / file_read 不需要):
          - file_write / file_delete / shell / send_message / external_api / network
        """
        # ⭐ 词表已搬到 `core/code_scan.py`，与 临时执行通道共用。
        #    📌 不共用的后果很具体：同一件事，两条路给用户两种说法。
        from core.code_scan import CONFIRM_LABELS as _CONFIRM_NEEDED
        try:
            with self.registry._lock:
                skill = self.registry.skills.get(skill_name)
            if skill is None:
                return []
            spec = skill.get_spec()
            confirmable = []
            for se in (spec.side_effects or []):
                if se in _CONFIRM_NEEDED:
                    confirmable.append(_CONFIRM_NEEDED[se])
            return confirmable
        except NotImplementedError:
            # 旧协议 Skill 未实现 get_spec()，无法判断副作用，必须确认（不能默认放行）
            return ["该 Skill 未提供副作用声明（旧协议），无法判断是否安全，需确认后执行"]
        except Exception:
            return ["该 Skill 副作用检查失败，需确认后执行"]

    def _request_management_confirmation(self, op: str, skill_name: str) -> str:
        self._pending_action = {"op": op, "skill": skill_name}
        self._pending_action_at = time.time()
        action_name = {"delete": "删除", "disable": "禁用", "enable": "启用"}.get(op, op)
        _msg = (f"将要{action_name} Skill「{skill_name}」。"
                f"这是本地文件级操作，请回复确认继续，或回复取消。")
        # 同时登记成 Interaction：跨重启存活 + 模型看得见。
        # `_pending_action` 仍是工作载荷，Interaction 是待办事实 —— 职责不同，不是双权威。
        _rt_open_skill_manage(self, op, skill_name, _msg)
        return _msg


    def _expire_stale_pending(self):
        """Step 4：超过 PENDING_TIMEOUT_SECONDS 仍未处理的 pending 自动清除。"""
        now = time.time()
        # 逐条判超时，不再一刀切。
        # 改造前是"最近那条的时间戳超了就把（唯一的）载荷清掉"；多槽之后
        # 那会变成"最新那条超时 → 把所有待审一起清掉"，包括刚生成两分钟的。
        # 每条自己带 `at`，各算各的。
        for _fn in list(self._pending_skill_order):
            _p = self._pending_skills.get(_fn)
            if not _p:
                self._pending_skill_order.remove(_fn)
                continue
            _at = _p.get("at") or self._pending_skill_at
            if not _at:
                continue
            age = now - _at
            if age > self.PENDING_TIMEOUT_SECONDS:
                self._drop_pending_skill(_fn)
                logger.info(f"[Pending] 超时清除待审 Skill「{_fn}」（{age:.0f}s 未处理）")
                # 交互也要跟着关 —— 否则卡片还挂着，点进去却发现代码没了
                # （那正是 那条误导消息的来源）。
                try:
                    from core.runtime import interaction as _it_e
                    _rt_close_skill_audit(_fn, approved=False,
                                          reason=_it_e.Resolution.DEADLINE)
                except Exception:
                    pass
        if not self._pending_skills:
            self._pending_skill_at = 0.0
        if self._pending_action and self._pending_action_at:
            age = now - self._pending_action_at
            if age > self.PENDING_TIMEOUT_SECONDS:
                op = self._pending_action.get("op", "?")
                skill = self._pending_action.get("skill", "?")
                self._pending_action = None
                self._pending_action_at = 0.0
                logger.info(f"[Pending] 超时清除 pending_action {op}/{skill}（{age:.0f}s 未处理）")



    async def _stream_decision_core(
        self,
        context,
        tools_manifest,
        system_guide,
        *,
        task_type: str,
        stage_label: str,
        state: StreamDecisionState,
        block_id: str | None = None,
        block_started: bool = False,
        emit_done: bool = True,
        event_scope: dict | None = None,
        allow_multi_tools: bool = False,
    ):
        """_stream_decision 的核心实现，结果写入局部 StreamDecisionState 而非实例属性。

        这样多个并发调用之间不会互相覆盖（实例属性在并发场景下有竞态风险）。
        原 _stream_decision 改为调用此方法的 wrapper，旧代码无需修改。
        """
        _scope = event_scope or {}
        _bid = block_id or f"{stage_label}_{time.time_ns()}"
        _started = block_started
        _first_delta = True
        decision = None
        used_model = self.target_model if hasattr(self, "target_model") else None
        notice_text = ""
        _answer_bid: str | None = None

        # ⭐ **在发出请求这一刻，快照本次 request 的 tool contract。**
        #
        # 🔴🔴 判据必须是"这一次 request 实际 attach 了哪些 schema"，
        #    **不能**用 `_core_manifest + _pending_loaded_manifests` 重算 ——
        #    那个方向刚好反了：`_pending_loaded_manifests` 是**中转缓冲区**，
        #    本批执行完就 append 进 `tools_manifest` 然后**立刻清空**
        #    （`orchestrator.py` 那段"按需加载：把本批 load_tools 匹配到的 schema
        #    并入 tools_manifest"）。于是下一步模型真正拿到 `CORE + 刚 load 的`，
        #    而那个缓冲区已经空了 —— 拿它当判据会**把刚刚合法 load 成功的工具拦掉**。
        #
        # ⭐ 而 `tools_manifest` 本身就是那份合同：它在 turn 内累积，且**就是**
        #    发给 provider 的那个列表。所以在这里取一份 frozenset 即可。
        # 📌 **request-local contract —— 不需要 ScopeContext，不需要落库。**
        #
        # ⚠️ 挂在 `decision` 上而不是 `self`：`_stream_decision_core` 刻意改造成
        #    用局部 `StreamDecisionState` 就是为了避免并发时实例属性互相覆盖，
        #    往 `self` 上写等于把那次改造又拆掉。
        #    📌 **一个"属于某次请求"的事实，就该挂在那次请求的产物上。**
        _active_tool_names = frozenset(
            (m.get("name") or "") for m in (tools_manifest or []) if isinstance(m, dict)
        )

        # 重启提示是否还挂着 —— 用来判断这次请求要不要消费它。
        try:
            from core.runtime import identity as _rt_ident
            _had_restart_notice = bool(_rt_ident.pending_restart_notice())
        except Exception:
            _rt_ident, _had_restart_notice = None, False
        _model_really_spoke = False

        # ⭐⭐ 硬窗口守卫的**处置侧**。
        #    provider 只回答「发不发得出去」；**回收历史的权力在这一层** ——
        #    📌 provider 不该碰 memory：一个为了少写一层而拉进来的依赖，
        #       会让「谁有权改什么」这件事从此说不清。
        async for _ev in self._stream_with_window_guard(
            context, tools_manifest, system_guide):
            # ⭐ 消费判定：**"模型确实收到了"，不是"我们拼进了 system_guide"。**
            #    任一真实模型流事件出现 → 它一定读到了 system prompt。
            #    ⚠️ `provider_error` 不算（见下面 done 分支）：API 层 400 / 网络失败时
            #       模型一个字都没看到，这时标成"已说过"= 这次重启永远不会被告知。
            if _ev["type"] in ("notice_delta", "answer_delta", "text_delta",
                               "tool_input_delta"):
                _model_really_spoke = True
            if _ev["type"] == "notice_delta":
                if not _started:
                    _started = True
                    yield {"event": "thought_block_start", "block_id": _bid,
                           "stage_label": stage_label, "status": "CORE_THINKING",
                           "server_start_time": time.time(), **_scope}
                elif _first_delta:
                    yield {"event": "thought_delta", "block_id": _bid, "delta": "\n", **_scope}
                _first_delta = False
                yield {"event": "thought_delta", "block_id": _bid, "delta": _ev["text"], **_scope}

            elif _ev["type"] == "answer_delta":
                # parallel_branch scope 内不把 delta 当最终答案流给前端
                if _scope.get("scope") == "parallel_branch":
                    continue
                if _answer_bid is None:
                    _answer_bid = f"answer_{time.time_ns()}"
                    yield {"event": "final_text_start", "block_id": _answer_bid, **_scope}
                yield {"event": "final_text_delta", "block_id": _answer_bid, "delta": _ev["text"], **_scope}

            elif _ev["type"] == "answer_discard":
                if _answer_bid is not None:
                    yield {"event": "final_text_discard", "block_id": _answer_bid, **_scope}
                    _answer_bid = None

            elif _ev["type"] == "tool_input_delta":
                yield {**_ev, **_scope}

            elif _ev["type"] == "done":
                decision = _ev.get("decision")
                used_model = _ev.get("model", used_model)
                notice_text = _ev.get("notice", "")
                # 旧路径不支持多工具：内部用非流式调用重试，强制单工具
                if (decision is not None
                        and decision.decision_type == "call_many"
                        and not allow_multi_tools):
                    logger.warning(f"[StreamDecision] 旧路径收到 call_many，内部重试单工具")
                    _retry_ctx = list(context) + [{
                        "role": "user",
                        "content": "[System note] This stage allows only one tool call. Choose only the first tool that must run now. Do not call multiple tools in this retry.",
                    }]
                    try:
                        _retry_d, used_model = await self.provider.chat_with_tools(
                            _retry_ctx, tools_manifest, system_guide,
                            # ⚠️ 与主路径同一份 `tools_manifest`，所以用同一个数。
                            stable_tool_count=getattr(self, "_core_stable_n", -1),
                        )
                        if _retry_d.decision_type == "call_many":
                            # 仍返回多工具：强制取第一个
                            _first = _retry_d.tool_calls[0]
                            decision = AgentDecision("call", tool_calls=[_first],
                                                     thinking_blocks=_retry_d.thinking_blocks)
                        elif _retry_d.decision_type in ("call", "text"):
                            decision = _retry_d
                        else:
                            decision = AgentDecision("single_tool_retry", content="")
                    except Exception as _re:
                        logger.error(f"[StreamDecision] 单工具内部重试失败: {_re}")
                        decision = AgentDecision("single_tool_retry", content="")
                if decision is not None:
                    decision.answer_bid = _answer_bid
                if _started and emit_done:
                    yield {"event": "thought_block_done", "block_id": _bid,
                           "full_text": notice_text, "status": "CORE_THINKING",
                           "model": used_model, **_scope}
                    _auto_summary = (notice_text.strip().split('\n')[0])[:10] if notice_text else ""
                    yield {"event": "thought_summary", "block_id": _bid,
                           "summary": _auto_summary, "translation": "", **_scope}

        # ⭐ 把这次 request 的 tool contract 钉在它自己的产物上。
        #    执行层据此判断"这个调用属不属于产生它的那次请求"。
        #    ⚠️ 用 `object.__setattr__` 不必要（AgentDecision 是普通类），直接赋值。
        if decision is not None:
            try:
                decision.active_tool_names = _active_tool_names
            except Exception:
                pass   # 不影响主流程：拿不到就退化成"不校验"（见执行侧的 fail-open 说明）

        # ⭐ 消费重启提示。
        # ⚠️⚠️ `provider_error` **不算模型收到过** —— 它是 `done` 事件里的一个
        #    `decision_type`，不是独立事件类型（`provider.py` 的 400 / 网络失败分支
        #    都是 `yield {"type": "done", "decision": AgentDecision("provider_error", …)}`）。
        #    只看 `done` 会把"请求根本没成功"当成"模型已经被告知过重启"，
        #    于是**这次重启永远不会被说出来**。
        if _had_restart_notice and _rt_ident is not None:
            _dtype = getattr(decision, "decision_type", "") if decision is not None else ""
            if _model_really_spoke or (decision is not None and _dtype != "provider_error"):
                _rt_ident.consume_restart_notice()

        state.decision = decision
        state.model = used_model or "UNKNOWN"
        state.notice = notice_text
        state.block_started = _started
        state.block_id = _bid
        state.answer_bid = _answer_bid

    async def _stream_decision(self, context, tools_manifest, system_guide, *,
                                task_type: str, stage_label: str,
                                block_id: str | None = None,
                                block_started: bool = False,
                                emit_done: bool = True):
        """向后兼容 wrapper。内部委托给 _stream_decision_core，结果同时写入
        实例属性（供旧代码路径使用）和局部 state 对象。

        新代码（ReAct 主循环）直接用 _stream_decision_core + 局部 StreamDecisionState，
        不依赖实例属性，无并发竞态风险。
        """
        state = StreamDecisionState()
        async for ev in self._stream_decision_core(
            context, tools_manifest, system_guide,
            task_type=task_type,
            stage_label=stage_label,
            state=state,
            block_id=block_id,
            block_started=block_started,
            emit_done=emit_done,
        ):
            yield ev

        # 兼容旧调用方（文件工具链、Skill 创建等）通过实例属性读结果
        self._last_stream_decision = state.decision
        self._last_stream_model = state.model
        self._last_stream_notice = state.notice
        self._last_stream_block_started = state.block_started
        self._last_stream_block_id = state.block_id
        self._last_stream_answer_bid = state.answer_bid

    # ══════════════════════════════════════════════════════════════════════
    # ReAct 主循环 — 工具执行层
    # ══════════════════════════════════════════════════════════════════════

    # ⭐⭐⭐ 等待的读写都直接进入 `waitcond`。
    # 切读期的 Authority 门面与观测期的 shadow 映射已经完成使命：继续保留会让后来调用方
    # 误以为仍有两套身份/两套账。历史 `legacy_susp_id` 只是一列反查数据，不是权威。

    def _build_suspension_resume_injection(self, records: list) -> str:
        """把待恢复的挂起记录拼成一段注入文本，让模型唤醒时知道自己在等什么。

        参考 Claude Code：状态本身在对话上下文里，这段只是显式提醒"你之前挂起了，
        现在被唤醒，先核对等的事成了没"，避免模型忽略历史里的 wait_for。
        """
        if not records:
            return ""
        lines = ["\n\n[Suspension Resume — Previous Wait State]"]
        for r in records:
            _src = "/".join(r.wake_on)
            lines.append(f"- Waiting for: {r.reason} (wake sources: {_src})")
        lines.append(
            "You have now been resumed. First verify whether the awaited condition is satisfied:\n"
            "- If satisfied, continue the task.\n"
            "- If not satisfied, you may call wait_for again, or briefly tell the user the current state and what is needed.\n"
            "Do not pretend Nano was working in the background while suspended; Nano was paused until this wake event."
        )
        return "\n".join(lines)

    async def _hand_back_long_task(self, *, display: str, bg_ref: str,
                                    action_id: str, event_queue,
                                    recheck: bool = True,
                                    detachable: bool = True) -> str:
        """一个耗时长的操作跑过阈值 → **把控制权交回模型，操作继续跑。**

        ⭐⭐⭐ **这是「长任务」的公共合同，与任务类型无关**（MCP / OS 命令 /
        以后的Subagent，全走这里）。2026-08-09 定的原则：
        📌 **我们要识别的只有「长任务」，识别任务类型毫无意义** ——
           用户不需要区分，所以系统也不该区分。

        它做三件事，一件都不多：
          ① 开一条**双源等待**（完成信号 + 到点回看）
          ② 出等待 pill（让用户看得见「这件事还挂着」）
          ③ 返回一段**不结束这一轮**的话给模型

        ⚠️⚠️ **第 ③ 件是这次改动的核心。** 旧实现给模型的话是
        「…has been moved to the background. Tell the user… **Then end this turn
        and do not call more tools.**」—— 那句话**在系统层面把 ReAct 链切断**，
        正是 用户最初质疑的那个后果（「会不会在系统层面强制把 react 拆成
        一大堆 turn」）。
        📌 **控制权交回模型 ≠ 这一轮必须结束。** 前者是「你可以继续做别的」，
           后者是「你不许再做事」—— 把它们压成一个，模型就失去了
           「一边等它一边铺路」的能力，而那正是它本来在做的事。

        ⚠️ 登记不上等待时**不许假装挂起了** —— 那会让这个操作永远没人等它。
           📌 一个「我会回来看」的承诺，登记失败时必须撤回，不许留在嘴上。
        """
        # ⭐⭐ `recheck=False` = **这个载体永远不回看**，只等完成信号。
        #
        # 今天唯一走这一档的是**Subagent**。早先的设计原话：agent 是
        # 「**无法被回看的后台任务**」—— 回看意味着把它走过的步骤读回主上下文，
        # 而上下文隔离正是Subagent存在的全部理由。
        # 📌 **一个为了隔离而存在的东西，不能有一条把它的过程灌回来的通路。**
        # ⚠️ 而「不回看」不是这里新造的一档：`set_next_checkin` 的
        #    `seconds` 省略语义逐字就是它（"do NOT look again; the completion
        #    signal will wake you"）。Subagent只是**永久**停在那一档。
        _wake_on = ["background", "timer"] if recheck else ["background"]
        _rec = _rt_wait_open(
            reason=f"still running: {display}",
            wake_on=_wake_on, bg_ref=bg_ref,
            timer_seconds=self._FIRST_RECHECK_SEC if recheck else None,
            # ⚠️ **意图必须显式写，不许由「有没有 timer」反推。**
            #    📌 早先的设计那条逐字同形：「不许由 `timer_at` 猜 pill 是否可控 ——
            #       等待创建者必须显式给 `waiting_intent`」。
            #       timer 的有无是这个意图的**后果**，不是它的定义。
            intent="system_recheck" if recheck else "detached")
        if _rec is None:
            logger.error(f"[LongTask] 交还控制权时登记等待失败（{display[:40]}）"
                         f"—— 不发等待 pill，也不承诺回看")
            return (f"\"{display}\" is still running, but the runtime could not "
                    f"register a follow-up for it. Tell the user it is running and "
                    f"that you cannot promise to report back automatically.")
        await event_queue.put({
            "event": "suspend_waiting",
            "action_id": action_id,
            "suspension_id": _rec.wait_id,
            "reason": f"still running: {display}",
            "wake_on": list(_wake_on),
            "timer_at": _rec.fire_at,
            "timer_seconds": self._FIRST_RECHECK_SEC if recheck else None,
            "waiting_intent": "system_recheck" if recheck else "detached",
            "bg_ref": bg_ref,
        })
        # ⭐⭐ **这一刻是 `dont_wait` 唯一有意义的时刻** —— 在此之前没有
        #    载体可以交出去，在此之后这一轮就结束了。所以它走
        #    `_pending_loaded_manifests`（本轮中途并入 schema 的既有通道），
        #    **不常驻、也不进 deferred 感知块**。
        #    📌 一个只在某种状态下才有意义的工具，应该只在那种状态下出现 ——
        #       这正是 `set_next_checkin` 那条 `availability` 的同一条判据，
        #       只不过它的「那种状态」出现在**一轮的中途**，而
        #       `availability` 是**每轮开头**算一次的，接不住它。
        # ⚠️ Subagent那条 `detachable=False`：它**已经**是不在手头的了，
        #    再给一个「别等它」的工具是让模型对一件已经做完的事再做一次决定。
        if detachable:
            self._detachable_carrier = {
                "wait_id": _rec.wait_id, "bg_ref": bg_ref, "display": display,
                "action_id": action_id,
            }
            try:
                if not hasattr(self, "_pending_loaded_manifests"):
                    self._pending_loaded_manifests = []
                if not any((m or {}).get("name") == "dont_wait"
                           for m in self._pending_loaded_manifests):
                    self._pending_loaded_manifests.append(_DONT_WAIT_MANIFEST)
            except Exception as _e_dw:
                logger.warning(f"[B1] dont_wait 没能注入本轮工具表: {_e_dw}")
        logger.info(f"[LongTask] {display[:40]} 交回控制权"
                    f"（{_rec.wait_id}，"
                    + (f"首次回看 {self._FIRST_RECHECK_SEC:.0f}s 后" if recheck
                       else "**不回看**，只等完成信号") + "）")
        # ⭐⭐ **这一句原来是无条件的「The runtime knows nothing about its
        #    progress right now.」—— 它现在会说谎。**
        #    接上进度总线之后，长命令有 stdout、MCP 有协议原生进度通知，
        #    交还的那一刻往往**已经能看到东西**了。
        #    📌 **一句「我不知道」在它其实知道的时候说出来，比不说更糟** ——
        #       模型会照着它去做一次多余的探查，或者干脆放弃判断。
        # ⚠️ 反过来也一样：没有进度就必须**明说没有**，不许给一个看起来
        #    像进度的空值。📌 一个「进度」字段在没有进度时必须说「没有」。
        _now = ""
        try:
            from core.runtime import progress as _pb0
            _now = _pb0.tail(bg_ref, lines=10)
        except Exception:
            _now = ""
        _now_seg = (f"Here is what it is doing right now:\n{_now}\n" if _now else
                    "⚠️ The runtime cannot see its progress — this carrier does not "
                    "report any. Do NOT claim you can see progress unless you "
                    "actually go and look.\n")
        return (
            f"\"{display}\" is still running — it has not finished yet, and it "
            f"keeps running on its own. It is the command you already started: do NOT "
            f"launch it, retry it, or ask the user whether to launch it again. Its "
            f"completion signal is already registered. Control is back with you only "
            f"for independent work.\n"
            + _now_seg +
            # 🔴 上一版这里写的是「你可以：…/ 继续做不依赖它的步骤 / …」，
            #    而下一段才提 `dont_wait` —— **这两句在打架**：前者已经
            #    准许它直接去做别的，于是它就直接去做了（实测 2026-08-20）。
            #    📌 一段给模型的指引，如果先给了出口再给条件，它只会读到出口。
            f"Decide now, before you do anything else:\n"
            # ⭐⭐ **把那个出口指出来。** schema 在清单里，但模型不会因为
            #    「有个工具」就想到该用它 —— 它需要被告知这一刻有个选择。
            #    ⚠️ 而且**把条件和它一起说**：说出口不说条件，等于邀请它逢长任务
            #       就调（那正是实测抓到的那次滥用）。
            #    📌 一个只在特定条件下才正确的动作，提示它的时候必须连条件一起提。
            + (("(a) If your next move needs this result — do nothing special. "
                "Say one short line to the user if it is worth saying, and the "
                "runtime will bring you back to it in about a minute.\n"
                "(b) If you have OTHER work that does not need this result — "
                "call `dont_wait` FIRST, naming that work, and only then start it. "
                "That hands this call to the runtime: it stops pulling you back to "
                "look at it, it shows up in the user's task drawer, and you get "
                "woken up once when it is actually done.\n"
                "⚠️ Doing that other work WITHOUT calling `dont_wait` first is the "
                "one wrong answer here: you will be interrupted partway through it.\n") if detachable else
               "Carry on with anything that does not depend on it, or end your turn "
               "if there is nothing else to do.\n")
            + "The runtime already owns follow-up. Do not narrate its cadence to the "
              "user; only speak about this task when you have a useful fact or decision.")

    async def resume_suspension(self, suspension_id: str, trigger: str, note: str = ""):
        """定时/后台唤醒入口——被外部触发器（app 的定时器 / 诈尸回调）调用，
        起一个新 turn 带着挂起上下文重新进 ReAct 循环。

        user 唤醒不走这里——用户下次说话时由 _handle_query_impl 顶部自动注入恢复。
        """
        from core.runtime.kernel import get_kernel
        from core.runtime import waitcond as _wc
        rec = _wc.find_by_id(get_kernel(), suspension_id)
        if rec is None or not rec.is_live:
            logger.info(f"[Suspension] resume 跳过：{suspension_id} 不存在或非 active")
            return
        # 预算检查必须在 resolve 之前——这是本方法里唯一的不可逆状态变更。
        #
        # 下面那句 resolve() 是幂等保护（防止同一条被定时+后台重复唤醒），
        # 它有意放在模型调用之前。代价是：一旦后续的 provider 调用失败，
        # 记录已经被标记为已恢复，而 turn 从未发生 —— 这条挂起就【永久丢了】，
        # 用户永远不知道 Nano 找过自己。
        #
        # 这不是假想：实测过一次同形状的事故——代理挂掉时，失败的请求
        # 照样把挂起记录消费掉了。所以这里在动状态之前先问一次预算，
        # 不够就原样退出，记录保持 active，下一次轮询还能再试。
        #
        # ⚠️ 这与"闸必须在 provider 收口"不冲突：判断逻辑仍然只有一份
        # （usage.cap_status），这里只是在会造成不可逆后果的地方提前止损。
        try:
            from core.usage import sync_budget_health
            if sync_budget_health() == "hard":
                logger.warning(
                    f"[Suspension] 预算已达硬上限，跳过唤醒 {suspension_id}（记录保持 active，稍后重试）"
                )
                return
        except Exception as _e:
            # 预算模块异常不该把挂起卡死——放行，真超限的话 provider 层还有一道闸
            logger.warning(f"[Suspension] 预算预检异常，按放行处理: {_e}")

        try:
            from core.usage import usage_tracker as _ut
            _ut.begin_turn()   # 唤醒 turn 也打点，UI 单条 token 一致
        except Exception:
            pass
        # ⭐⭐⭐ [回看设计 2026-08-09] **一次「回看」不许把这条等待 resolve 掉。**
        #
        # 判定：这条等待挂着 `bg_task_ref`（后台还在跑）而唤醒源是**定时** ——
        # 那就是「到点该去看一眼了」，**不是「那件事成了」**。
        # 🔴 原来这里无条件 `resolve` —— 对回看是**致命的**：
        #    后台任务还在跑，而它的完成信号从此再也唤不醒任何人
        #    （记录已终态 → `notify_background_done` 找不到匹配 → 静默丢弃）。
        #
        # 📌 **「唤醒」和「结束这条等待」是两件事** —— 它们此前被压在同一个动作里
        #    （`resolve` 兼做「防重复唤醒」的去重）。回看正是把它们分开的那个场景。
        #    ⭐ 而去重仍然有：重排会把 `fire_at` 推到长兜底上，
        #      所以下一跳轮询不会再点火。**换了一种去重方式，不是取消去重。**
        _is_recheck = bool(rec.bg_ref) and trigger == "timer"
        if _is_recheck:
            # ⚠️ **先把下一次回看排到长兜底，再唤醒模型**（顺序不能反）：
            #    驱动唤醒的是那个 `fire_at <= now` 的查询，
            #    不推的话这一跳唤醒完、下一跳（5 秒后）又会点火。
            #    📌 一个「幂等的状态转换」不等于「幂等的副作用」。
            try:
                from core.runtime import waitcond as _wc_r
                _wc_r.reschedule_wait(suspension_id, self._RECHECK_FALLBACK_SEC)
            except Exception as _e_rs:
                logger.error(f"[Recheck] 🔴 重排下一次回看失败（{suspension_id}）—— "
                             f"这条等待可能会被反复唤醒: {_e_rs}")
            # ⭐ 记住「这一轮在回看哪一条」—— `set_next_checkin` 不需要模型传 id
            #    （回看轮里只有一个对象，让它传 id 只会多一个可以填错的地方）。
            #    📌 **模型不该被要求提供系统已经知道的东西。**
            self._recheck_sid = suspension_id
            logger.info(f"[Recheck] 回看 {suspension_id}（后台仍在跑）→ 起新 turn"
                        f"，下一次回看已先排到 {self._RECHECK_FALLBACK_SEC:.0f}s 后")
        else:
            # ⚠️ 非回看轮要**清掉**它，否则 `set_next_checkin` 会在下一轮
            #    继续被注入、并指向一条已经结束的等待。
            #    📌 一个「本轮有效」的状态，必须在每一条进入这一轮的路径上都被设定 ——
            #       只在需要它的那条路上设，另一条路会带着上一轮的值跑。
            self._recheck_sid = ""
            # 标记恢复（防止同一条被定时+后台重复唤醒）
            _wc.resolve_wait(suspension_id, resolved_by=trigger)
        # ⚠️ 这里原本还会关闭一次观测期的镜像 ——
        #    它关的是**镜像**，而镜像已经就是权威（上一行的
        #    resolve/cancel 直接走内核）。留着是**对同一件事关两次**，
        #    而且它依赖一个**重启就没的内存映射**。
        #    📌 双写拆掉之后，配对的「双关」也必须一起拆 ——
        #       留一半会让人以为还有另一套账。
        logger.info(f"[Suspension] 唤醒 {suspension_id}（trigger={trigger}）→ 起新 turn")

        used_model = "UNKNOWN"
        event_queue = asyncio.Queue()

        async def realtime_callback(m_name: str):
            nonlocal used_model
            used_model = m_name

        # ① 唤醒 turn 的工具清单同样问目录（双循环合一：唤醒也能操作屏幕）。
        # ⚠️ 这里**只**用来拼 `{skills}` 那句系统提示；本 turn 真正的 manifest
        #    由下游 `_run_react_loop` 自己组装 —— 与改造前一致。
        regular_skills = [
            d.manifest for d in
            self._get_tool_catalog().advertised(ToolScope.MAIN,
                                                self._tool_runtime_view())
        ]
        base_guide = self._system_guide_template.format(skills=', '.join(self._skill_names(regular_skills)))
        try:
            base_guide += self._build_tool_awareness_block()
        except Exception:
            pass
        base_guide += _OS_CAPABILITY_PROMPT  # 唤醒 turn 同样注入 OS 使用说明

        system_guide = base_guide + self._build_suspension_resume_injection([rec])
        _session_injection = self._build_session_log_injection()
        if _session_injection:
            system_guide += _session_injection

        # 写一条 user 角色的唤醒提示进 memory，使本轮有"输入"驱动 ReAct
        _trigger_desc = {
            "timer": "(timer fired; automatic wake-up)",
            "background": "(background task completed; automatic wake-up)",
        }.get(trigger, f"({trigger} wake-up)")
        _note_seg = f" Background output: {note}" if note else ""
        if _is_recheck:
            # ⭐⭐⭐ **回看那一眼的全部价值，取决于这段话。**
            #
            # 🔴 系统能告诉它的只有一件事：**那个后台调用还没返回**。
            #    进度、速度、错误 —— 一样都没有（后台载体是 `await _mcp_task`，
            #    里面没有任何进度流）。
            # ⚠️ 所以如果不告诉它「你可以自己去看」，这一轮的必然结果是
            #    「还在跑，我过一会儿再看」—— 花一次完整的模型调用换一句废话。
            # 📌 **「回看一眼」的价值完全取决于那一眼能看到什么** ——
            #    只能看到「还在跑」的话，那个判断系统自己也能做，不需要模型。
            #    「当然要给，不然整个回看设计都是废的。」
            #
            # ⭐ 所以这段话必须做三件事：
            #    ① 如实说清它**现在只知道什么**（别让它以为自己看到了进度）
            #    ② 告诉它**可以怎么去看**（这才是那一眼的内容）
            #    ③ 告诉它**看完要做什么决定** —— 而且三个出口都点明：
            #       换办法 / 排下一次回看 / 不用再看了
            # ⭐⭐⭐ **如果这个载体能给出真实进度，就直接把进度放进来。**
            #
            # 🔴 上一版无条件说「系统对进度一无所知，你自己去看」。对 MCP 是真的
            #    （载体是 `await _mcp_task`，里面没有进度流），但对**长命令是假的**
            #    —— 它的 stdout 就在缓冲里躺着。
            # 📌 **一句「我不知道」在它其实知道的时候说出来，比不说更糟** ——
            #    模型会照着这句话去做一次多余的探查，或者干脆放弃判断。
            # ⭐ 所以这里先问一次载体：有进度就摆在眼前，没有就如实说没有。
            #    📌 **「回看一眼」的价值取决于那一眼能看到什么** ——
            #       所以能给的时候必须给（不给整个设计都是废的）。
            _prog = ""
            try:
                # ⭐⭐⭐ **问总线，不问某一个载体。**
                # 🔴 这里原来是 `from core.os_layer import longcmd; longcmd.progress(...)`
                #    —— 于是 MCP 和本地 Skill 那两条路**必然拿到空**，
                #    而代码上只看得出「这里读了命令的进度」，
                #    看不出「这里少了两个载体」。
                # 📌 **三个载体要被同一只眼睛看到，那个「看」的接口就该属于
                #    第三方，不属于其中任何一个。**
                from core.runtime import progress as _pb3
                _prog = _pb3.tail(rec.bg_ref or "", lines=20)
            except Exception:
                _prog = ""
            _prog_seg = (f"\nHere is what it is doing right now:\n{_prog}\n"
                         if _prog else
                         "\n⚠️ That is ALL the system knows — there is no progress "
                         "information for this one. Do NOT claim you can see progress "
                         "unless you actually go and look.\n")
            self.memory.add_system_note(
                "user",
                "[System check-in] You put this in the background a while ago and it "
                f"has NOT finished yet: {rec.reason}\n"
                + _prog_seg +
                "\n"
                "If you want to judge whether it is healthy or stuck, go look — for example:\n"
                "  · take a screenshot / read the relevant window if it has a UI\n"
                "  · run a quick command to inspect it (log tail, file size, process state)\n"
                "  · ask the same external service for its status\n"
                "\n"
                "Then choose ONE:\n"
                "  · it looks broken (e.g. 3 KB/s for a 3 GB download) → say so and try another way\n"
                "  · it looks fine but slow → tell the user briefly it is still running, "
                "and set the next check-in yourself (a long gap is fine and cheaper — "
                "if you expect ~10 more minutes, ask for ~10 minutes, not 1)\n"
                "  · it looks nearly done → no need to check again; "
                "the completion signal will wake you\n"
                "\n"
                "Decide first, then give at most one user-facing status conclusion. "
                "If scheduling the next check adds no new fact, do not repeat the same "
                "progress both before and after `set_next_checkin`.\n"
                "\n"
                f"Use `set_next_checkin` to choose when (or whether) to look again. "
                f"If you say nothing, the next check-in defaults to "
                f"{self._RECHECK_FALLBACK_SEC:.0f} seconds from now."
            )
        else:
            # ⭐⭐⭐ [2026-08-25 实测] **「核实」和「继续」必须是同一件事。**
            #
            # 🔴 这句话原来是：`Verify whether the task can continue now, then act accordingly.`
            #    用户的观察：**第一条消息那一轮很完美，唤醒之后就变得异常糟糕。**
            #    逐轮对下来，每个唤醒轮只走 1~2 个工具（看一眼屏幕）就结束了，
            #    从外面看就是「疯狂截图、从来不回复」。
            #
            # ⭐ 而 Nano 自己的复盘字字对得上：
            #      「这不是终止任务，所以按照你的指令我应该正常跟他聊天」
            #      「但是我没有这样做，而是调用了多次 look_at_screen…」
            #    ⇒ 它**知道**该聊天（那是用户指令，在历史里），
            #      但它这一轮**被交代的是「Verify」** —— 而它把核实做完了。
            # 📌 **两个指令打架时，离它最近的那个赢。**
            # 📌 **不是模型退化了，是我们把它这一轮的任务换小了** ——
            #    第一轮的驱动是「做完一整件事」，唤醒轮的驱动是「核实一下」。
            # ⚠️ 而且原文**从头到尾没有重述任务是什么**，只说了「你在等什么」
            #    和「能不能继续」—— 任务本身要它自己回头去历史里翻。
            #
            # ⭐ 修法：把「核实」写成**继续任务的第一步**，而不是一件独立的、
            #    做完就能收工的事；并且明说**这一轮什么时候才允许结束**。
            #    📌 一个不说「什么时候算做完」的指令，模型会在第一个自然停顿处停下。
            # ⚠️ 这句话**不止 GUI 走**：长命令、MCP、定时等待的唤醒都走它。
            #    所以措辞只讲「继续你原来那件事」，不提任何屏幕/工具。
            self.memory.add_system_note(
                "user",
                (f"[Scheduled plan is now due] {_trigger_desc}. The user asked for: {rec.reason}. "
                 "Perform the requested reminder or action now."
                 if rec.intent == "scheduled_plan" else
                 f"[System wake-up] {_trigger_desc}. Previous wait reason: {rec.reason}. {_note_seg}"
                 "The thing you were waiting for may have happened. Check it, and then "
                 "CONTINUE THE ORIGINAL TASK in this same turn - checking is only the "
                 "first step, it is not the job. The job is still the task the user gave "
                 "you earlier; re-read it if you need to. End this turn only when that "
                 "task is finished, or when you genuinely have to wait again.")
            )

        self._rag_hit_this_turn = False
        self._full_file_hit_this_turn = False
        _rt_lease_release(self)     # 每轮重置时镜像也归还
        # ⚠️ 这里是 `_run_react_loop` 的【第二个】调用方——
        # 定时/后台唤醒**绕过 `_handle_query_impl`**，所以那边做的两件事必须在这里重做一遍，
        # 否则唤醒 turn 会复用上一轮的 `_rt_turn_id`（冷启动时甚至全都落到 "rtturn_unknown"），
        # 一旦有活 Span 残留就会触发假的不变量违反、且 sweep 永远捞不到它。
        # 顺序同样重要：sweep 必须在旧标志被重置之前。
        self._rt_turn_id = "rtturn_" + __import__("uuid").uuid4().hex[:12]
        # 工具失败计数按轮清零：跨轮保留会让"你这轮已经试过"变成假话。
        self._tool_failures_this_turn = {}
        # 旧字段没了 —— sweep 现在自己从 Span 判断，不需要外部喂旧值。
        _rt_sweep_stale_spans(self, None)
        try:
            async for ev in self._run_react_loop(
                tools_manifest=regular_skills,
                system_guide=system_guide,
                base_guide=base_guide,
                realtime_callback=realtime_callback,
                event_queue=event_queue,
            ):
                yield ev
        except Exception as core_err:
            logger.critical(f"[Suspension] 唤醒 turn 异常: {core_err}\n{traceback.format_exc()}")
            self._clean_damaged_memory()
            yield self._get_generic_error_payload(core_err, None)

    def _ensure_react_sems(self):
        """懒初始化并发信号量（必须在 asyncio 事件循环内调用）。"""
        if self._rag_parallel_sem is None:
            self._rag_parallel_sem = asyncio.Semaphore(2)
        if self._tool_parallel_sem is None:
            self._tool_parallel_sem = asyncio.Semaphore(4)

    # ══════════════════════════════════════════════════════════════════════
    # 工具失败诊断 —— 让模型拿到【正确且充分】的失败信息
    # ══════════════════════════════════════════════════════════════════════
    #
    # ═══ 这不是"安全网"，是在修一条说假话的错误信息 ═══
    #
    # 改造前，模型调一个**不存在**的工具名，收到的是：
    #
    #     "Error: the tool did not return any valid content."
    #
    # 这句话的意思是「工具跑了，但没返回内容」——**和事实正好相反**，
    # 工具压根没被调用（`registry.execute` 找不到就 `return None`，见 registry.py:280，
    # 然后 None 在这里被翻译成了上面那句）。
    #
    # 更糟的是：一个**真实存在**、只是 `run()` 返回了 None 的 Skill，
    # 拿到的是**一模一样的字符串**。两种完全不同的故障连区分都做不到，
    # 所以模型就算想诊断也诊断不出来，只能换参数重试或者再猜一个名字。
    #
    # 也就是说：模型不是"判断失误需要被兜住"，是**在一个假前提上做了在那个前提下
    # 完全合理的判断**。错的是我们。
    #
    # ═══ 判据═══
    #
    #     拿着这条失败消息，一个没有别的上下文的人能不能选出正确的下一步？
    #     不能就不合格 —— **哪怕它技术上没说错**。
    #
    # 所以这里要满足两条独立要求：
    #     正确 = 描述的是真实发生的事（区分"不存在" / "参数不对" / "跑了但失败"）
    #     充分 = 拿着它就能选出下一步（给相近真名字、说清 load_tools 帮不帮得上忙）
    #
    # ⚠️ **第一行必须是人话**：`result_text[:80]` 会作为 `result_summary`
    #    显示在用户可见的工具卡片上（见本文件 `tool_end` 事件），
    #    诊断块只能放在第一行之后。

    class _ToolFailCause:
        UNKNOWN_TOOL = "UNKNOWN_TOOL"       # 名字不存在
        DISABLED_TOOL = "DISABLED_TOOL"     # Skill 存在但被禁用了
        BAD_PARAMS = "BAD_PARAMS"           # 工具存在，参数不合 schema
        TOOL_ERROR = "TOOL_ERROR"           # 工具跑了并自己报了错
        # ⭐ 工具存在、当前健康、当前 eligible —— 只是产生这次调用的那次
        #    API request **没把它的 schema 给模型**（模型凭更早一次请求的记忆调的）。
        # ⚠️ **刻意不复用上面四类**：塞进 UNKNOWN_TOOL 是撒谎（它明明存在）、
        #    塞进 DISABLED_TOOL 也是（它没被禁用）、塞进 TOOL_ERROR 更错（它压根没跑）。
        #    📌 给模型的失败信息必须**正确**且**足够让它选出下一步** ——
        #       而这里正确的下一步是 `load_tools`，与那四类的下一步都不同。
        TOOL_NOT_ACTIVE = "TOOL_NOT_ACTIVE" # 存在且可用，但本次 request 没附它的 schema

    def _known_tool_names(self) -> list[str]:
        """当前进程里**全部合法**的工具名。给"最接近的真名字"用。

        🔴 改造前这里手工拼**四个来源**（registry / `_tool_pool` / `_CORE_TOOL_NAMES`
           / MCP），注释还自陈「四个来源缺一不可」—— 那句话本身就是证据：
           **一份靠"记得把四个都写上"维持的名单，漏一个不会报错，只会让诊断变笨。**
        ⭐ 现在只有一个来源。

        ⚠️ 与改造前有**一处刻意的差异**，留痕：
           改造前拼的是 `registry.list_enabled_skills()`（Skill **实例**字典），
           而目录投影的是 `registry.get_all_manifests()`（**manifest** 列表）。
           两者在正常情况下一致，但 那种「manifest 顶层没有 name、整条被丢弃」
           的 Skill 只会出现在前者里 —— 它**装载成功、UI 显示 READY，而模型
           从来看不见它**。把这种名字放进"你是不是想调这个"的提示里，
           等于劝模型去调一个它拿不到 schema 的东西。
           📌 同 那条：**半可见的能力比不可见更坏。**
        """
        return sorted(n for n in self._get_tool_catalog().names() if n)

    def _describe_tool_failure(self, name: str, cause: str, tool_error: str = "") -> str:
        """产出给模型看的诊断块。**只在失败时调用**——成功时一个字都不加。

        为什么不把 1/2/3 三条排查步骤丢给模型自己走：
        **分发的那一刻代码就已经知道是哪一类了。** 让模型再推一遍，
        等于在"别猜"的地方又开了一次猜的口子。直接把结论递给它。
        """
        import difflib
        # 同一轮内重复撞同一个名字 → 升级措辞，防止它在一轮里连试三个变体
        seen = getattr(self, "_tool_failures_this_turn", None)
        if seen is None:
            seen = self._tool_failures_this_turn = {}
        key = f"{name}::{cause}"
        seen[key] = seen.get(key, 0) + 1
        repeated = seen[key] > 1

        lines = ["", "[Tool Call Diagnostics — internal, not shown to the user]"]

        if cause == self._ToolFailCause.UNKNOWN_TOOL:
            # ⚠️ 必须**忽略大小写**地匹配。`difflib` 直接比 "getdns" 和 "GetDNSList"
            # 相似度只有 0.5 出头，够不到任何合理阈值——而大小写写错恰恰是最常见的
            # 一种错法（一次就修过 21 处 manifest 大小写不一致）。
            # 只按原样匹配的话，最该被提示的那一类反而永远提示不出来。
            known = self._known_tool_names()
            lower_map = {n.lower(): n for n in known}
            exact_ci = lower_map.get(name.lower())
            near = [lower_map[m] for m in difflib.get_close_matches(
                name.lower(), list(lower_map), n=3, cutoff=0.6)]
            lines += [
                "- tool_error: none (the tool was never invoked, so it produced no error of its own)",
                f"- cause: UNKNOWN_TOOL — no tool, Skill, or MCP tool is named \"{name}\".",
            ]
            if exact_ci:
                lines.append(
                    f"- ⭐ The correct name is \"{exact_ci}\" — you only got the capitalization wrong. "
                    f"Tool names are case-sensitive. Retry with the exact spelling."
                )
            elif near:
                lines.append(f"- closest existing names: {', '.join(near)}")
            else:
                lines.append("- no existing name is close to it.")
            lines += [
                # ⚠️ 这条必须说清楚，否则模型会去调 load_tools 然后原地打转：
                # 延迟的 Skill/MCP 即使没 load 也能按名字直接执行（分发与 schema 注入解耦），
                # load_tools 只负责给参数 schema，**不决定工具能不能调**。
                "- load_tools will NOT help here: it only supplies parameter schemas for tools "
                "that already exist. It cannot make a nonexistent name callable.",
                "- Next: call one of the names above if it matches your intent, or tell the user "
                "plainly that this capability does not exist. Do not try another guessed name.",
            ]

        elif cause == self._ToolFailCause.DISABLED_TOOL:
            # ⚠️ Skill 与 MCP 的**下一步不同工具**，所以这句不能合并成一句泛的。
            #    📌 失败信息要同时【正确】且**足够让它选出下一步** ——
            #       而"下一步"在两边是两个不同的工具名。
            _mcp_srv = ""
            try:
                from core.mcp_client import MCPManager as _MM_n
                _mcp_srv = _MM_n.instance().disabled_tool_server(name)
            except Exception:
                _mcp_srv = ""
            if _mcp_srv:
                lines += [
                    "- tool_error: none (the MCP server is disabled, so nothing was invoked)",
                    f"- cause: DISABLED_TOOL — the MCP server \"{_mcp_srv}\" is disabled.",
                    "- The tool name is fine. Do not look for another name and do not "
                    "try to reach the same capability some other way first.",
                    "- Next: tell the user that this MCP server is turned off, and ask "
                    f"whether to enable it (manage_mcp can enable \"{_mcp_srv}\").",
                ]
            else:
                lines += [
                    "- tool_error: none (the Skill exists but is currently disabled, so it was not invoked)",
                    f"- cause: DISABLED_TOOL — the Skill \"{name}\" exists but is disabled.",
                    "- The name is correct. Do not look for another name and do not recreate it.",
                    "- Next: tell the user it is disabled and ask whether to enable it "
                    "(manage_existing_skill can enable it).",
                ]

        elif cause == self._ToolFailCause.BAD_PARAMS:
            lines += [
                f"- tool_error: {tool_error or '(none)'}",
                f"- cause: BAD_PARAMS — \"{name}\" exists; your arguments did not match its schema.",
                "- The name is correct. Do not switch to a different tool.",
                f"- Next: call load_tools(names=[\"{name}\"]) to get the exact parameter schema, "
                "then retry with corrected arguments. Do not guess parameter names.",
            ]

        else:  # TOOL_ERROR
            lines += [
                "- cause: TOOL_ERROR — the tool exists and ran. The error above is its own.",
                "- That message is authoritative. Do not reinterpret it or second-guess it.",
                "- Next: fix the inputs based on that message, or report it to the user. "
                "Do not switch to a different tool name because of this error.",
            ]

        if repeated:
            lines.append(
                f"- ⚠️ You already called \"{name}\" this turn and it failed the same way. "
                "Stop retrying. Explain to the user what is missing instead."
            )
        return "\n".join(lines)

    # ── MCP 接入辅助 ──────────────────────────────────────────────────────
    @staticmethod
    def _is_mcp_tool(name: str) -> bool:
        """MCP 工具名形如 mcp__<server>__<tool>（廉价前缀判断，不需加载 manager）。"""
        return isinstance(name, str) and name.startswith("mcp__")

    @property
    def _mcp_manager(self):
        """懒加载 MCP 管理器单例。MCP 工具属于'Nano 自身能力'，内联分发不进 registry。"""
        mgr = getattr(self, "_mcp_mgr_cache", None)
        if mgr is None:
            from core.mcp_client import get_mcp_manager
            mgr = get_mcp_manager()
            self._mcp_mgr_cache = mgr
        return mgr


    _LOOK_FRACS = {
        "left": (0, 0, 0.5, 1), "right": (0.5, 0, 1, 1),
        "top": (0, 0, 1, 0.5), "bottom": (0, 0.5, 1, 1),
        "center": (0.25, 0.25, 0.75, 0.75),
        "top_left": (0, 0, 0.5, 0.5), "top_right": (0.5, 0, 1, 0.5),
        "bottom_left": (0, 0.5, 0.5, 1), "bottom_right": (0.5, 0.5, 1, 1),
    }

    def _capture_screen_image(self):
        """抓主屏 → 全分辨率 PIL RGB 图，不缩放不裁剪。供两遍放大用。
        ⚠️ 原来这行写着「（masked）」—— 那是遮罩时代的残留，已删（见下）。
        ⛔ 原来有个 mask_rect 参数（把 Nano 自己那块涂黑）——2026-08-23 整个删了，
        理由见 _look_at_screen_impl 里那段。**改名以免下一个人以为它还在遮罩。**
        """
        from PIL import Image, ImageDraw
        im = None
        off_x = off_y = 0
        try:
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                off_x, off_y = mon["left"], mon["top"]
                shot = sct.grab(mon)
                im = Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception:
            try:
                from PIL import ImageGrab
                im = ImageGrab.grab()
            except Exception:
                return None
        if im is None:
            return None
        im = im.convert("RGB")
        # ⛔ [2026-08-23 已定：整个删掉] 这里原来把 Nano 自己那块**涂黑**。
        #
        # 🔴 它是个**早期产物，而且是错的**：那张图**既给模型看、也给用户看**，
        #    是同一张 —— 所以涂黑不是「把自己排除掉」，是**把那块屏幕的信息
        #    对谁都抹掉了**（Nano 窗口下面盖着的东西也一起没了）。
        # 📌 **一个「排除自己」的实现，如果连自己也看不见它排除掉的东西，
        #    那它排除的就不是自己，是那块区域。**
        # ⚠️ 顺带：涂黑还让截图很难看（整块死黑），而它换来的收益是零。
        # ⭐ 「先缩小自己」这件事**照旧要做**，但理由换成真的那个：
        #    **Nano 挡着你要看的东西** —— 而不是「不然会被涂黑三分之一」。
        return im

    @staticmethod
    def _pil_to_png(im, max_w: int = 1568) -> bytes:
        """PIL 图 → PNG bytes。宽于 max_w 才缩；小裁剪会被放大到 max_w（小字变清晰）。"""
        import io
        from PIL import Image
        if im.width != max_w:
            r = max_w / im.width
            im = im.resize((max_w, max(1, int(im.height * r))), Image.LANCZOS)
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

    @staticmethod
    def _parse_loose_json(s: str):
        import json, re
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            pass
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None

    async def _vision_ask(self, png: bytes, prompt: str, system: str) -> str:
        """把一张图 + 提问发给视觉模型，返回文字。Anthropic content 格式。"""
        # ⚠️ 走**视觉槽**而不是主模型 —— 主模型未必有视觉能力
        #    （深度求索只有 flash-vision-exp 有）。
        img_part = self.provider.build_image_part(png, "image/png")
        _vm = _vision_model_for_os()
        context = [{"role": "user", "content": [{"type": "text", "text": prompt}, img_part]}]
        content, _ = await self.provider.chat_without_tools(
            context, system, model_override=_vm)
        return (content or "").strip()

    async def _seed_image_summary_via_vision(self, image_parts: list, query: str):
        """主模型没视觉时：视觉槽先看一眼，结果直接写进 `image_summary`。

        返回**还要发给主模型的 image_parts** —— 成功则 `None`（pixels 不发，
        免得盲主模型读到 `[Unsupported Image]` 然后诚实报告"我看不到"），
        失败则原样返回（退回老路）。
        ⚠️ 降级绝不抛：它不该有能力把主流程带走。
        """
        try:
            from core.runtime.blobs import extract_image_blocks
            pairs = extract_image_blocks(image_parts)
            if not pairs:
                return image_parts
            raw, _mime = pairs[0]
            # ⭐ 带上用户这轮的问题 —— 比通用描述准，且不额外花钱。
            #    但仍要求写成**独立摘要**：这段文字要留给以后的自己，
            #    不能写成"针对这一问的答案"（同 note_image 的那条约束）。
            ask = (
                "Describe this image for your own later reference, independently of any "
                "question: what it is, its layout, and the text / objects / colors / counts "
                "that are actually visible. Be concrete and dense." + _NL
                + "The user's message this turn was: "
                + (query or "").strip()[:400] + _NL
                + "Make sure anything relevant to that is covered, but do NOT answer it here."
            )
            desc = await self._vision_ask(raw, ask, "You are a precise visual describer.")
            if not (desc or "").strip():
                return image_parts
            self.memory.set_image_summary(desc.strip())
            # 摘要已经有了 ⇒ 别再给 note_image（它会拿主模型的盲视覆盖掉这份）
            self._turn_image_pending = False
            # 🔴 **绝不能返回 None** —— 注入块（`if temp_file_hint or image_parts:`）
            #    是模型知道"这一轮来了张图"的**唯一渠道**；置空它，模型的上下文里
            #    一个字都不会提到图，然后它会满机器去找图（实测，52s / 12.7K tok）。
            # ⇒ 把 pixels **换成文字块**，走同一条注入线。
            # 🔴 这里必须用 `short_handle()` —— `ui_images` 存的是**完整 ref**
            #    （`6cd3f9d4….png`），直接当 handle 给模型，`resolve_handle` 的
            #    hex 校验一看见 `.png` 就判死。
            #    📌 **一个有专用构造函数的标识符，永远别自己拼。**
            _handles = []
            try:
                from core.runtime.blobs import short_handle as _sh
                _m = self.memory._last_user_image_message()
                _refs = list(getattr(_m, "ui_images", None) or []) if _m else []
                _handles = [h for h in (_sh(r) for r in _refs) if h]
            except Exception:
                pass
            _hint = (" You can call view_past_image with handle "
                     + str(_handles[0]) + " to look again for details this text omits."
                     ) if _handles else ""
            logger.info(f"[D10] 主模型无视觉 -> 视觉槽代看，摘要 {len(desc)} 字")
            return [{"type": "text",
                     "text": ("[The user attached an image to this message. Your model cannot "
                              "see pixels, so it was read for you by the vision model. "
                              "What it shows:]" + _NL + desc.strip() + _NL + _hint)}]
        except Exception as e:
            # ⚠️ 留痕：静默退回会让这条 bug 长得跟"主模型有视觉"一模一样。
            logger.warning(f"[D10] 视觉槽代看失败，退回原路（pixels 照发）: {e}")
            return image_parts

    async def _emit_shot(self, event_queue, png: bytes, purpose: str):
        """把截到/放大的图推进聊天流（用户看见 Nano 看到了什么）。"""
        if event_queue is None:
            return
        try:
            import base64 as _b64
            await event_queue.put({
                "event": "screenshot_preview",
                "png_b64": _b64.b64encode(png).decode(),
                "purpose": purpose,
            })
        except Exception:
            pass

    def _window_identity_note(self) -> str:
        """⭐ 每次看屏幕都如实告诉模型**它在对着哪个窗口**，以及有没有换过。

        ⚠️⚠️ 这个方法来自一次**真实的数据损坏**：
        Nano 要操作自己打开的 `新建文本文档.txt`，用户中途把焦点放到了**自己的**
        另一个记事本上。`get_target_window()` 返回的是"最前面那个非 Nano 窗口"，
        于是 Nano 对着**用户的**记事本 Ctrl+A + 输入，**把用户的内容清掉了**。

        当时 `look_at_screen` 只返回视觉模型的散文描述，**一个字都没提在看哪个窗口**，
        模型除了在图里认标题之外没有任何手段发现自己换了对象。

        📌 **判据：不要让模型去「记得怀疑」，要把变化本身摆到它眼前。**
        早先的设计 三层防护网第 1 层（"提示词写明挂起前后环境不能默认一致"）
        要求的是模型**自律**；这里给的是**事实**。两者不是重复 ——
        一条是"你应该怀疑"，一条是"这就是变了"。

        ⚠️ 身份用 **hwnd**，不是标题：两个未命名记事本的标题**完全一样**
        （都是"无标题 - 记事本"），靠标题判身份正好在最该分清的场景下失效。

        ⚠️ 比较基准是**本轮**（`_last_fg_window` 每轮重置），与活动租约同一个边界 ——
        跨轮的"变了"没有意义，那本来就是两件事之间。
        """
        try:
            from core.os_layer.executor_low import foreground_identity
            cur = foreground_identity()
        except Exception:
            return ""
        if not cur:
            return ""
        prev = getattr(self, "_last_fg_window", None)
        self._last_fg_window = cur
        _desc = f"\"{cur['title']}\"" + (f" ({cur['proc']})" if cur['proc'] else "")
        head = f"[Foreground window] hwnd={cur['hwnd']} {_desc}"

        # ⭐⭐ 比"前台是谁"重要得多的一件事：**我的目标窗口现在怎么样了。**
        #    前台变了只说明"有别的窗口上来了"；而"目标窗口还在不在、是不是被最小化了、
        #    坐标还有效吗"才是决定下一步动作的信息 —— 而且**截图答不了这些**
        #    （关了/最小化/被挡/在别的显示器，截图看起来全都一样）。
        try:
            from core.os_layer import window_binding as _wb
            _lid = (getattr(self, "_rt_os_lease", None) or ("", 0))[0]
            _tgt = _wb.describe(_lid) or _wb.gone_note(_lid)
            if _tgt:
                head = _tgt + "\n" + head
        except Exception:
            pass

        if prev and prev.get("hwnd") != cur["hwnd"]:
            head += (
                f"\n⚠️ The foreground window CHANGED since your last look "
                f"(was hwnd={prev['hwnd']} \"{prev.get('title','')}\").\n"
                "⚠️ A window that looks like the one you were working on may be a "
                "DIFFERENT window — identical titles are common (e.g. two untitled "
                "Notepad windows). Do NOT assume this is the file you opened.\n"
                "Before any destructive action (select-all, delete, overwrite, save), "
                "confirm this is really your target. If you cannot confirm it, stop and ask."
            )
        return head

    async def _look_at_screen(self, purpose: str, include_self: bool,
                              event_queue=None, region: str = "full") -> str:
        """截屏 + 视觉理解，**并在结果最前面附上前台窗口的真实身份**。

        ⭐ 窗口身份来自 Win32（`GetForegroundWindow`），**不是**问视觉模型 ——
        它是事实而不是推断。视觉模型连"这是哪个窗口"都没被问过，
        更不该由它来回答这种能造成破坏性误操作的问题。
        """
        note = self._window_identity_note()
        body = await self._look_at_screen_impl(purpose, include_self, event_queue, region)
        return f"{note}\n\n{body}" if note else body

    async def _look_at_screen_impl(self, purpose: str, include_self: bool,
                                   event_queue=None, region: str = "full") -> str:
        """截屏 + 视觉理解。默认【自动两遍放大】——整屏看一遍，看不清的小目标由
        视觉模型自己指出大概位置，工具自动裁那块放大到全清晰再看一遍，主模型无需选区域。
        region != full 时为手动覆盖（直接裁那块单遍看）。
        """
        if self.provider is None:
            return "（无法截图自查：视觉模型不可用）"
        SYS = "You are Nano's eyes. Describe only what is actually visible on the screen to help decide the next step. If unclear, say it is unclear."
        # ── 让开自己：**只剩一条路 —— 真的最小化**──
        #
        # ⛔ 这里原来有两条：小窗(mini) → **涂黑**；大窗 → 最小化。
        #    涂黑那条整个删了，因为它是**早期产物而且是错的**：
        #    🔴 那张图**既给模型看、也给用户看**，是同一张 ——
        #       涂黑不是「排除自己」，是**把那块屏幕对谁都抹掉**
        #       （Nano 窗口下面盖着的东西一起没了）。
        #    📌 **一个「排除自己」的实现，如果连自己也看不见它排除掉的东西，
        #       那它排除的就不是自己，是那块区域。**
        #
        # ⭐ 而「最小化再恢复」是**真的让开** —— 它保留下来，且现在无条件走。
        # ⚠️ 顺带删掉了 `include_self` 参数：遮罩没了，它就没有对象了。
        #    📌 一个没有实现的参数留着，模型会拿它当真的用。
        # ⚠️ `include_self=True` 时**什么都不做** —— 那正是「我要看自己」的唯一走法。
        _win = None
        if not include_self:
            try:
                from nicegui import app as _napp
                _win = _napp.native.main_window
                _win.minimize()
                await asyncio.sleep(0.45)
            except Exception:
                _win = None
        try:
            im = await asyncio.to_thread(self._capture_screen_image)
        finally:
            if _win is not None:
                try:
                    _win.restore()
                except Exception:
                    pass
        if im is None:
            return "（截图失败，没看到屏幕）"

        # ── 手动 region 覆盖：直接裁那块、单遍看 ──────────────────────────
        if region in self._LOOK_FRACS:
            fr = self._LOOK_FRACS[region]; w, h = im.size
            crop = im.crop((int(fr[0]*w), int(fr[1]*h), int(fr[2]*w), int(fr[3]*h)))
            png = self._pil_to_png(crop)
            await self._emit_shot(event_queue, png, purpose)
            try:
                ans = await self._vision_ask(
                    png,
                    f"This is a zoomed region of the screen. Need: {purpose}\n"
                    + self._LOOK_ANSWER_RULES, SYS)
                return ans or "（视觉模型没有返回内容）"
            except Exception as e:
                return f"（视觉分析失败：{e}）"

        # ── 自动两遍放大 ────────────────────────────────────────────────
        try:
            overview = self._pil_to_png(im, 1568)
            pass1 = (
                f"This is the current computer screenshot. Need: {purpose}\n"
                + self._LOOK_ANSWER_RULES +
                "If the target may be visible but too small or blurry "
                "(such as a name, filename, list item, or small icon), do not treat it as absent. "
                "Instead set need_zoom and return zoom_bbox for the larger surrounding area to "
                "zoom into; that box should include context and may be larger than the target.\n"
                'Return strict JSON only: {"answer":"...", '
                '"targets":[{"name":"...","box":[x0,y0,x1,y1]}], '
                '"need_zoom":true/false, "zoom_bbox":[x0,y0,x1,y1] or null}. '
                "All boxes use 0-1 normalized coordinates with top-left as (0,0)."
            )
            raw1 = await self._vision_ask(overview, pass1, "You are Nano's eyes. Output only the required JSON.")
            data = self._parse_loose_json(raw1)
            self._look_diag("pass1(整屏)", purpose, raw1, data)
            if not data or not data.get("need_zoom") or not data.get("zoom_bbox"):
                await self._emit_shot(event_queue, overview, purpose)
                _ans = ((data or {}).get("answer") or raw1) or "（视觉模型没有返回内容）"
                return _ans + self._targets_note(data, im.size)

            # 需要放大：从【全分辨率】原图裁 zoom_bbox → 放大 → 第二遍精确看。
            # ★ 它是 LLM 估的、本来就糙，所以【以中心扩张、保证足够大】——
            #   宁可裁大点带上下文，也别裁太紧框歪了漏掉目标（"找错太多次"的根因）。
            # ⚠️ 2026-08-24 由 `bbox` 更名为 `zoom_bbox`：这一版新增了 `targets`
            #    （目标本身的框），📌 两个都叫 box 而语义相反（一个是「放大哪」、
            #    一个是「目标在哪」）—— 不改名迟早有人读错一个。
            x0, y0, x1, y1 = [float(v) for v in data["zoom_bbox"]]
            w, h = im.size
            cxc = (min(x0, x1) + max(x0, x1)) / 2.0
            cyc = (min(y0, y1) + max(y0, y1)) / 2.0
            half_w = max((max(x0, x1) - min(x0, x1)) / 2.0 + 0.06, 0.20)  # 至少 ~40% 宽
            half_h = max((max(y0, y1) - min(y0, y1)) / 2.0 + 0.06, 0.18)  # 至少 ~36% 高
            fx0 = max(0.0, cxc - half_w); fy0 = max(0.0, cyc - half_h)
            fx1 = min(1.0, cxc + half_w); fy1 = min(1.0, cyc + half_h)
            cx0, cy0, cx1, cy1 = int(fx0*w), int(fy0*h), int(fx1*w), int(fy1*h)
            if cx1 - cx0 < 20 or cy1 - cy0 < 20:
                await self._emit_shot(event_queue, overview, purpose)
                return (data.get("answer") or "（看了一眼，没能精确定位）") \
                    + self._targets_note(data, im.size)
            crop = im.crop((cx0, cy0, cx1, cy1))
            zoom_png = self._pil_to_png(crop, 1568)   # 裁剪放大到 1568 → 小字清晰
            await self._emit_shot(event_queue, zoom_png, purpose)
            raw2 = await self._vision_ask(
                zoom_png,
                f"This is a zoomed region from the previous screenshot. Need: {purpose}\n"
                + self._LOOK_ANSWER_RULES +
                "Do not treat similar-looking items as the target. Clearly say whether each "
                "item is present. If this region does not contain it, say so.\n"
                'Return strict JSON only: {"answer":"...", '
                '"targets":[{"name":"...","box":[x0,y0,x1,y1]}]}. '
                "Boxes are 0-1 normalized coordinates of THIS zoomed image.",
                "You are Nano's eyes. Output only the required JSON.",
            )
            d2 = self._parse_loose_json(raw2)
            self._look_diag("pass2(放大)", purpose, raw2, d2)
            # ⚠️ 第二遍的框在**裁剪图**的坐标系里 → 换算回全屏。
            #    偏移 + 缩放，两个都是我们自己算的，精确可逆。
            _note2 = self._targets_note(d2, (cx1 - cx0, cy1 - cy0), offset=(cx0, cy0))
            # ⭐⭐⭐ [2026-08-25 实测] **两遍的答案要一起交出去，不能后一遍盖掉前一遍。**
            #
            # 🔴 问题：这里原来只返回 `_ans2`（放大那一遍），把整屏那一遍的答案**丢掉**。
            #    实测：模型放大的那块**没盖住**屏幕最下面那条新消息，
            #    于是它只拿到「这一块里没有」，得出结论「窗口需要下拉」——
            #    而整屏那一遍其实已经看见了。
            # 📌 **放大是为了看清，不是为了缩小搜索范围。**
            #    一次放大之后「没看到」，只说明**那一块里没有**，
            #    不能当成「屏幕上没有」—— 而旧写法把这两句话变成了同一句。
            # ⚠️ 所以不光要合并，还要**把作用域说出来**：
            #    📌 一个不标明取景范围的观察结果，会被当成对整个画面的断言。
            _ans1 = ((data or {}).get("answer") or "").strip()
            _ans2 = ((d2 or {}).get("answer") or raw2 or "").strip()
            if not _ans2:
                return (_ans1 or "（放大后仍看不清）") + _note2
            _parts = []
            if _ans1:
                _parts.append(f"[Whole screen] {_ans1}")
            _parts.append(f"[Zoomed into region {cx0},{cy0}-{cx1},{cy1}] {_ans2}")
            _parts.append(
                "NOTE: the zoomed view covers only that rectangle. If what you were "
                "looking for is not in it, that is NOT evidence it is absent from the "
                "screen - the whole-screen line above is the wider view. Do not conclude "
                "you need to scroll just because the zoom missed it."
            )
            return "\n".join(_parts) + _note2
        except Exception as e:
            return f"（视觉分析失败：{e}）"

    # ⭐⭐⭐ [2026-08-24] **看完了要把坐标交出来。**
    #
    # 🔴 问题：`look_at_screen` 只返回**散文**（「发送按钮在右侧，是个纸飞机图标」）。
    #    pass1 确实算过一个 box，但那个 box 的语义是「放大哪一块」，用完就丢。
    #    ⇒ Nano **从来没拿到过任何坐标**。它下一步要点，只能
    #      `click(target="发送按钮")` —— 走整条 locate 链再赌一次定位。
    # 🔴 而 Nano 自己复盘时说「我本应直接点击那个坐标」——
    #    📌 **它描述的是一个它没有的能力。** 一个模型对自己能力的自述不能当证据用。
    # ⭐ 真正的账不是「省一次 locate」：
    #      现在：每一次点击都在**重新赌一次定位**（那条链跑了 8 次错了大半）
    #      改后：一次勘察拿全坐标 → 后面几步直接点 → **那条链只被走一次**
    #    ⚠️ 它**不改善单次定位的精度** —— 只把赌的次数从 N 降到 1。
    _LOOK_ANSWER_RULES = (
        "Answer every item asked for in the request - not only the first one. "
        "For each item you can see, give its bounding box so it can be clicked "
        "later without looking again. Be truthful; if something is unclear or "
        "absent, say so instead of guessing.\n"
    )

    # ⭐⭐ [2026-08-24] **给 `look_at_screen` 也加上诊断。**
    #
    # 🔴 实测：坐标那一段（`[Screen coordinates from this look …]`）**一次都没出现**，
    #    而**分不出**是哪一种：
    #      A 模型压根没填 `targets`（没按新 JSON 格式答）
    #      B 填了，但 `_parse_loose_json` 没解析出来 → data 是 None → 返回空串
    # 📌 而这是同一晚**第二次**犯同一个疏漏：locate 那条链偏了六轮之后才加
    #    `_diag`，加完一次就定位了根因；然后改 `look_at_screen` 时**又没给它加**。
    #    📌 **一个只记录结果、不记录输入的流程，出了偏差就只能靠猜**
    #       —— 这句就写在 `_diag` 的注释里，然后没照做。
    # ⚠️ 整段吞异常：它是诊断，📌 一个用来看清楚的东西不许成为失败源。
    @staticmethod
    def _look_diag(tag: str, purpose: str, raw, data) -> None:
        try:
            if data is None:
                _snip = (str(raw) or "")[:220].replace("\n", " ")
                logger.warning(f"[Look-Diag] {tag}「{purpose}」 **JSON 没解析出来**"
                               f"（B 类）| 原文前 220 字：{_snip}")
                return
            _keys = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
            _t = (data or {}).get("targets")
            if not _t:
                _snip = (str(raw) or "")[:220].replace("\n", " ")
                logger.warning(f"[Look-Diag] {tag}「{purpose}」 解析成功但 "
                               f"**targets 为空**（A 类）| keys={_keys} | "
                               f"原文前 220 字：{_snip}")
                return
            logger.info(f"[Look-Diag] {tag}「{purpose}」 keys={_keys} | "
                        f"targets 原始={_t}")
        except Exception:
            pass

    @staticmethod
    def _targets_note(data, size, offset=(0, 0)) -> str:
        """把模型给的 `targets`（归一化框）换算成**屏幕坐标**，附在回答后面。

        ⚠️ `size` 是**那张图对应的屏幕区域**大小，`offset` 是它的左上角 ——
           整屏那一遍 offset=(0,0)；放大那一遍是裁剪区的左上角。
           📌 一个换算函数如果自己去猜「这是第几遍」，它就知道了不该知道的事；
              让调用方把区域交进来，两遍共用同一份实现。
        ⚠️ 失败返回空串：📌 坐标是**附加**信息，拿不到不该让整次「看屏幕」失败。
        """
        try:
            items = (data or {}).get("targets") or []
            if not items:
                return ""
            _w, _h = int(size[0]), int(size[1])
            _ox, _oy = int(offset[0]), int(offset[1])
            lines = []
            for it in items[:8]:
                box = it.get("box")
                if not box or len(box) != 4:
                    continue
                x0b, x1b = sorted((float(box[0]), float(box[2])))
                y0b, y1b = sorted((float(box[1]), float(box[3])))
                cx = _ox + int((x0b + x1b) / 2 * _w)
                cy = _oy + int((y0b + y1b) / 2 * _h)
                lines.append(f"  - {it.get('name') or '?'}: x={cx}, y={cy}")
            if not lines:
                logger.warning("[Look-Diag] targets 有内容但一个 box 都没解析出来")
                return ""
            logger.info(f"[Look-Diag] 换算后屏幕坐标（区域 {_w}x{_h} @ "
                        f"{_ox},{_oy}）：{lines}")
            return ("\n\n[Screen coordinates from this look — click them directly with "
                    "computer_use click(x=…, y=…); do NOT look again just to find them]\n"
                    + "\n".join(lines))
        except Exception:
            return ""


    # ══════════════════════════════════════════════════════════════════════
    # 统一工具目录的接入点
    #
    # ⭐ 这三个方法是 11 个消费点的**共同前置**：目录本身、这一刻的运行时事实、
    #    以及"把两者合起来问"的入口。
    # 📌 **Catalog 只回答"谁处理 / 该不该出现"；"怎么运行"仍是 Runner 的事**
    #    （GUI 租约等待 / health 门控 / 工具卡事件 / 长任务交还 / 错误分类 /
    #     EXIT 路径的同轮拒绝与终端事件收尾，一律留在原处）。
    # ══════════════════════════════════════════════════════════════════════

    def _tool_runtime_view(self) -> "_ToolRuntimeView":
        """这一刻的运行时事实 —— `availability` 判据的唯一输入。

        ⚠️ 刻意做成**窄接口**而不是直接把 orchestrator 传进去：
           一个 predicate 能拿到整个 orchestrator，就迟早有人在里面写副作用。
        """
        return _ToolRuntimeView(self)

    def _get_tool_catalog(self) -> "ToolCatalog":
        """装配工具目录：内置声明（静态）+ Skill / MCP（每次重建）。

        ⚠️ 内置声明只构造一次并缓存 —— 它是静态的，而且构造期不变量
        （manifest 名一致 / awareness 必填 / bindings 非空 / binding 方法真实存在）
        每次重跑纯属浪费。**动态部分每次重建**：Skill 会热加载、MCP 会重连。
        """
        _cat = getattr(self, "_tool_catalog", None)
        if _cat is None:
            from core.tools import ToolCatalog as _TC
            from core.tools.builtin import build_builtin_definitions as _bbd
            _mans = {}
            for _k, _v in globals().items():
                if _k.endswith("_MANIFEST") and isinstance(_v, dict) and _v.get("name"):
                    _mans[_v["name"]] = _v
            _cat = _TC()
            for _d in _bbd(_mans):
                _cat.add_builtin(_d)
            # ⭐ 启动期响亮失败：binding 用方法名字符串写，写错的话**运行时才炸**，
            #    而且表现为「模型调了工具、然后什么都没发生」。
            _cat.assert_bindings_exist(self)
            self._tool_catalog = _cat
        _cat.clear_dynamic()
        try:
            from core.tools import mcp_definitions, skill_definitions
            for _d in skill_definitions(self.registry):
                _cat.add_dynamic(_d)
            _mgr = getattr(self, "_mcp_manager", None)
            if _mgr is not None:
                for _d in mcp_definitions(_mgr):
                    _cat.add_dynamic(_d)
        except Exception as _e:
            logger.warning(f"[F4] 动态工具投影失败（内置仍可用）：{_e}")
        # ⚠️ 被隔离的动态工具要**响亮**（但不致命）—— 一条撞名的用户 Skill /
        #    外部服务不该把整个 Nano 打死，但也绝不能静默消失。
        for _r in _cat.rejected():
            logger.error(f"[F4] 工具被隔离：{_r.name}（{_r.origin.value}）—— {_r.reason}")
        return _cat

    # ══════════════════════════════════════════════════════════════════════
    # 工具 handler —— 从 `_execute_one_tool_call` 的 name 分派链
    #             **机械提取**出来的分支体。
    #
    # ⚠️ **这是纯重构，不是 shadow**：旧的 if/elif 仍然调用它们，行为零变化。
    #    目的是让最终切换那一次只需要把
    #        `if name == A: ... elif name == B: ...`
    #    换成
    #        `handler = catalog.resolve(name, scope); await handler(...)`
    #    而**不必同时重写执行逻辑**。
    #
    # ⭐ 分层（外部评审的核心建议，已核实）：
    #    **Catalog 决定"谁处理"，Flow Runner 决定"怎么运行这一类 handler"。**
    #    所以 handler **只负责产出 result_text**；下面这些统一 plumbing
    #    一律留在 `_execute_one_tool_call` 里，不许被 handler 吞掉：
    #      GUI 租约等待 / health fail-fast / tool_start·tool_end 事件 /
    #      长任务交还 / 错误分类 / 挂起期日志 / ToolExecution 组装。
    #
    # 📌 统一签名 `(args, aid, *, event_queue) -> str`：
    #    AST 实测（提取前）确认 —— 分派链**之后**的代码只读 `result_text` 一个变量，
    #    所以"返回 result_text"就是这批 handler 的完整契约。
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_list_knowledge_files(self, args: dict, aid: str, *,
                                           event_queue, **_ctx) -> str:
        return await asyncio.to_thread(rag_engine.list_knowledge_files_for_agent)

    def _ui_sink(self, event_queue):
        """这次 UI 往返该发到哪条通道。

        🔴🔴 **main agent 和 Subagent 的通道寿命不一样**（实测 2026-08-20 卡死）：
           轮内的 `event_queue` 由 `navigate_pipeline` 的 `async for` 消费，
           **那个循环随本轮结束而退出**。Subagent detach 之后活得比它长 ——
           于是Subagent发的 `os_action_confirm` 落进一个没人读的队列，
           弹窗永远不出现，Subagent在确认闸上干等 300 秒，屏幕上什么都没有。
        📌 **一个跨过了自己那一轮的执行者，不能再用那一轮的通道去要 UI。**
        ⚠️ 这个洞只在**需要 UI 往返**的事件上存在：Subagent此前全是只读工具，
           一次往返都不需要 —— 给它写权限的那一刻才暴露。
           📌 一条只在某种能力出现后才会被走到的路，它的缺陷会**和那个能力
              同一天诞生**，而不是在它被写下的那天。
        ⚠️ 取不到轮外通道时**退回轮内**（fail-safe：最坏退化成今天的行为，
           而不是把授权请求整个丢掉）。
        """
        try:
            if current_agent_label():
                _oob = getattr(self, "_ui_oob_events", None)
                if _oob is not None:
                    return _oob
                logger.error("[OOB] Subagent要弹窗，但轮外通道不存在 —— "
                             "退回轮内通道（本轮结束后它就没人读了）")
        except Exception:
            pass
        return event_queue

    async def _handle_edit_file(self, args: dict, aid: str, *,
                                event_queue, used_model: str = "", **_ctx) -> str:
        """精确修改。**自己不写盘** —— 算完新内容后走 `file_write` 那条路。

        🔴🔴 这个 handler 的**全部安全性**来自最后那一步：
             它把结果交给 `_execute_dsl_step`，于是
             **地板 / 确认弹窗 / 审计 / 路径策略全是 `os_execute` 那一套**。
        📌 2026-08-13 的硬约束：**换一个暴露层，不许换掉它底下的安全层。**
           ⚠️ 如果哪天有人为了"少一次弹窗"把这里改成直接 `Path.write_text`，
              那一刻这个工具就变成了绕过确认的后门 —— 而它看起来只是"简化了一下"。
        """
        from pathlib import Path as _P

        from core.os_layer import fileedit as _fe
        from core.os_layer import pathpolicy as _pp

        _path = (args.get("path") or "").strip()
        _edits = args.get("edits") or []
        if not _path:
            return "[edit_file] 没有给 path。"
        _why = _pp.denied_reason(_path)
        if _why:
            return f"[edit_file] 这个位置不允许操作（{_why}）。"

        _f = _P(_path)
        if not _f.exists():
            # ⚠️ 不许顺手创建 —— 📌 「改一个不存在的文件」几乎总是路径写错了，
            #    而替用户建一个空文件会把一个明显的错误变成一个安静的错误。
            return (f"[edit_file] 文件不存在：{_path}。"
                    f"要新建请用 os_execute 的 file_write。")
        try:
            _orig = await asyncio.to_thread(_f.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            return f"[edit_file] 这个文件不是 UTF-8 文本，无法精确修改：{_path}"
        except Exception as e:
            return f"[edit_file] 读不了：{e}"

        _res = _fe.apply_edits(_orig, _edits)
        if not _res.ok:
            # ⚠️ 失败**什么都没写** —— 全有或全无（见 `fileedit` 模块头）。
            return f"[edit_file] 没有改动：{_res.error}"

        # ── 🔴 交给 OS 层：地板 / 弹窗 / 审计 全在这一步 ──
        # ⭐⭐ **授权卡要看的是「要做什么」，不是「做完长什么样」**
        #    （2026-08-15 定的，参照 Claude Code）：
        #        授权卡 = 行为（接下来要执行什么）
        #        pill 展开 = 结果（做了什么、变了什么 → diff）
        #    📌 这正是 自己写过的那条判据 ——
        #       **「事前授权」和「事后审计」是两个不同的问题，
        #         一个界面回答不了两个** —— 当时写了，接线时却没用上。
        #
        # 🔴 所以这里**不能**把 `_res.content`（整份新文件）当预览：
        #    那既不是"要做什么"（要做的是「把这几行换掉」），
        #    也不是"做了什么" —— 它只是路由到 `file_write` 之后漏出来的实现产物。
        _preview_lines = []
        for _i2, _e2 in enumerate(_edits, 1):
            if not isinstance(_e2, dict):
                continue
            _preview_lines.append(f"# 第 {_i2} 处")
            _preview_lines.append("- " + str(_e2.get("old_text", "")).replace("\n", "\n- "))
            _nt = str(_e2.get("new_text", ""))
            _preview_lines.append("+ " + _nt.replace("\n", "\n+ ") if _nt else "+ （删除）")
            _preview_lines.append("")
        _instr = {"action": "file_write",
                  "params": {"path": str(_f), "content": _res.content,
                             "mode": "overwrite",
                             # ⚠️ 只给授权卡看，不参与执行（executor 只读 path/content/mode）。
                             "_preview": "\n".join(_preview_lines).rstrip()},
                  "reason": f"edit_file：{_res.applied} 处修改 "
                            f"(+{_res.added}/-{_res.removed})"}
        from core.os_layer.dispatch import OSDispatcher
        from core.os_layer.safety import OSSessionSafety
        if not hasattr(self, "_os_safety"):
            self._os_safety = OSSessionSafety()
        _dsp = OSDispatcher(
            session_id=getattr(self, "_session_id", ""),
            m1_mode=False, m2_mode=True, m3_mode=True,
        )
        _os_result = None
        async for _ev in self._execute_dsl_step(_instr, _dsp, self._os_safety,
                                                used_model):
            if "_step_result" in _ev:
                _os_result = _ev["_step_result"]
            else:
                # ⭐ Subagent跨过自己那一轮之后要走轮外通道 —— 见 `_ui_sink`。
                await self._ui_sink(event_queue).put(_ev)

        if not (_os_result or {}).get("ok"):
            _err = (_os_result or {}).get("error") or "被拒绝或取消"
            return f"[edit_file] 写入没有发生：{_err}（文件未改动）"

        self._wm_add(
            EntryType.FILE_READ, str(_f), "file_edited",
            detail=f"+{_res.added}/-{_res.removed}", tags=["file", str(_f)],
        )
        # ⭐ 回给模型的是**摘要 + diff**，不是整份新内容 ——
        #    📌 它刚刚才给出那些改动，把整个文件还给它是纯粹的上下文浪费。
        return (f"[edit_file] 已修改 {_f.name}：{_res.applied} 处，"
                f"+{_res.added}/-{_res.removed} 行。\n{_res.diff}")

    async def _handle_search_files(self, args: dict, aid: str, **_ctx) -> str:
        """找文件/找内容。**只读** —— 写仍然只能走 `os_execute`。

        ⚠️ 丢进线程跑：`os.walk` + 逐行读是**阻塞**的，
           📌 一次在大目录上的搜索能把整个事件循环卡住几秒，
              而那期间 UI 的转圈、pill、终止按钮全部不响应。
        """
        from core.os_layer import filesearch as _fs
        _root = (args.get("path") or "").strip()
        _name = (args.get("name_pattern") or "").strip()
        _content = (args.get("content") or "").strip()
        try:
            _res = await asyncio.to_thread(
                _fs.search, _root,
                name_pattern=_name, content=_content,
                regex=bool(args.get("regex")),
                recursive=bool(args.get("recursive", True)),
                exclude=(args.get("exclude") or ""),
                max_results=int(args.get("max_results") or _fs.MAX_RESULTS),
            )
        except Exception as e:
            logger.warning(f"[Search] search_files 失败: {e}")
            return f"[search_files] 搜索失败：{e}"

        # ⭐⭐ **grep 命中过的文件，视同已试读。**
        #
        # 🔴 实测 2026-08-26 抓到的浪费：模型先撞上试读闸 →
        #    转去 `search_files` 拿到了**精确行号** → 然后又调 `load_full_file`
        #    → **又被拒** → 下一轮才去 peek。**闸白白吃掉了两次工具调用。**
        # 📌 根因是设计缺陷，不是模型笨：
        #      试读的目的 = **「在没有线索时提供线索」**
        #      而此刻它已经有 grep 给的行号了 —— 那正是试读要给的那种线索，
        #      **而且比试读更精准**。
        #    ⇒ 逼它再去试读，是让它花一轮去获取一份**它已经拥有的东西**。
        # ⚠️ 只有**内容命中**（grep）才算：只按文件名找到的（Glob）不算 ——
        #    📌 「知道这个文件存在」和「知道它里面有什么」是两回事，
        #       而试读闸拦的是后者。
        if _content:
            for _h in (_res.get("hits") or []):
                _p = getattr(_h, "path", None) or (
                    _h.get("path") if isinstance(_h, dict) else None)
                if _p:
                    self._peeked[self._peek_key(str(_p))] = "via_search"

        return _fs.render(_res, root=_root, name_pattern=_name, content=_content)

    # ══════════════════════════════════════════════════════════════════
    # 迭代阅读：试读闸 + scratchpad
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐ **闸绑 Task，不绑 turn**（同早先对 scratchpad 的处置）：
    #    任务会被挂起、丢后台、被插队 —— 绑 turn 的话，任务一挂起，
    #    「这个文件试读过了」这件事就没了，模型回来还要再试读一次。
    # ⚠️ 而它**只是一张便签，不是权威状态**：读不出 Task id 就退化成
    #    「按没试读过处理」（fail-safe 方向 = 多问一句，不是多做一步）。
    _peeked: dict = {}

    def _peek_key(self, filename: str) -> str:
        try:
            from core.runtime import task as _tk
            _tid = _tk.ensure_conversation_task("") or "no-task"
        except Exception:
            _tid = "no-task"
        return f"{_tid}::{filename}"

    async def _read_file_text(self, filename: str, with_images: bool,
                              aid: str, event_queue) -> str:
        """把文件读成文本。**两个工具共用这一条路。**

        📌 判据只能有一处：`peek_file` 和 `load_full_file` 拿到的必须是
           **同一份文本**，否则「第 200 行」在两个工具里指的是不同的东西。
        """
        def _rag_progress(msg: str):
            event_queue.put_nowait({"event": "tool_progress", "action_id": aid,
                                    "message": msg})
        async with self._rag_parallel_sem:
            return await asyncio.to_thread(
                rag_engine.load_full_file, filename, with_images, _rag_progress)

    def _ambient_entries(self) -> list:
        """实时 buffer + trail 里**所有带句柄**的条目，统一成 (ts, line, ref)。

        ⚠️ 两个源都要查：`ambient_trail.recent()` 刻意 `exclude_last_sec=600`，
           而 trail 每 4 分钟才写一次 —— **「刚才」那一条几乎总在实时 buffer 里**。
        """
        # 🔴🔴 **收「有 ref」，不是「有 path」**（第一版写成后者，自己把出路堵死了）。
        #
        #    `▸` 标记的判据确实是 **path 非空** —— 拉一条只有名字的回来，
        #    注入里本来就有那个名字，**白烧一轮**（已明确 ②）。
        #    但**查找**不能用同一个判据：模型拿一个没标 ▸ 的时刻来问时，
        #    该听到的是「这条只有名字，路径没确认，你可以 search_files」，
        #    而不是「没有这个条目」。📌 已明确 ④：
        #    **拉到一条没东西的，工具要直说「这条只有这些」，而不是回一个空。**
        # ⚠️ 第一版的后果很阴：`_handle_...` 里那整个 unconfirmed 分支
        #    **永远不会被执行** —— 代码在、测试不写就永远发现不了。
        out = []
        try:
            from core.proactive.activity import get_buffer
            for w in (get_buffer().snapshot().get("windows") or []):
                if getattr(w, "event", "") != "focus":
                    continue
                _r = getattr(w, "ref", None)
                if isinstance(_r, dict) and (_r.get("path") or _r.get("name")):
                    out.append((float(w.ts), w.window_title or "", _r))
        except Exception:
            pass
        try:
            from core.proactive import ambient_trail
            for r in ambient_trail.recent(hours=12, limit=60, exclude_last_sec=0):
                _r = r.get("ref")
                if isinstance(_r, dict) and (_r.get("path") or _r.get("name")):
                    out.append((float(r.get("ts") or 0), r.get("line") or "", _r))
        except Exception:
            pass
        # ⚠️ 两个源会重叠：trail 这里**不排除**最近 10 分钟（否则「刚才」拉不到），
        #    而那段同样躺在实时 buffer 里。⇒ 按 (整秒, path) 去重。
        _seen, _uniq = set(), []
        for ts, line, ref in out:
            _k = (int(ts), str(ref.get("path") or ""), str(ref.get("name") or ""))
            if _k in _seen:
                continue
            _seen.add(_k)
            _uniq.append((ts, line, ref))
        # 🔴 **排序必须给 key** —— `sorted(list_of_tuples)` 在 ts 与 line 都相同时
        #    会接着去比较第三项，而那是个 dict ⇒ **TypeError，当场崩**。
        #    📌 一个"顺手"的默认排序，会在最罕见的输入上变成崩溃点。
        _uniq.sort(key=lambda x: x[0])
        return _uniq

    async def _handle_resolve_ambient_referent(self, args: dict, aid: str,
                                               **_ctx) -> ToolOutcome:
        import datetime as _dt
        _at = str((args or {}).get("at") or "").strip()
        if not _at:
            return ToolOutcome(
                "Missing 'at'. Pass the timestamp printed on the entry you mean, "
                "for example 09:32:14.", failed=True)
        _rows = self._ambient_entries()
        # ⚠️ **可能命中多条**：窗口轮询 2s 一次，同一秒内连续切换会撞在一个时刻上。
        # 🔴 第一版这里 `break` 掉第一条 —— 于是「取那个网页」会静默拿回文件那条，
        #    **而模型不知道自己拿错了**。📌 一个可能指错而不自知的 id，
        #    比没有 id 更糟（同「说清结果可不可信」）。
        # ⇒ 全返回，让它自己挑；挑不出来就该问用户（早先的设计：歧义走 ask_user_choice）。
        _hits = [(ts, line, ref) for ts, line, ref in _rows
                 if _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") == _at]
        _hit = _hits[0] if _hits else None
        if _hit is None:
            # ⚠️ **给出口，不只给一个「没找到」**（同 那条：模型需要的是
            #    一个出口，不是一个名字）。所以这里连「哪些是可取的」一起说。
            _avail = ", ".join(
                _dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")
                for t, _, _ in _rows[-8:]) or "(none right now)"
            return ToolOutcome(
                f"No resolvable entry at {_at}. Only entries marked with the small "
                f"triangle in [Ambient] can be resolved, and you must pass the "
                f"timestamp printed on that same entry.\n"
                f"Resolvable right now: {_avail}", failed=True)

        if len(_hits) > 1:
            _blocks = []
            for _t, _l, _rf in _hits:
                _blocks.append(
                    f"- {_rf.get('kind')}: {_rf.get('name')}\n"
                    f"  {'target' if not _rf.get('confirmed') else 'location'}: "
                    f"{_rf.get('path') if _rf.get('confirmed') else 'not verified'}\n"
                    f"  context: {_l}")
            return ToolOutcome(
                f"[Ambient {_at}] {len(_hits)} different things share this timestamp "
                f"(the user switched windows within the same second):\n"
                + "\n".join(_blocks)
                + "\nPick the one the user means from the context lines. If it is "
                  "genuinely ambiguous, ask them instead of guessing.")

        _ts, _line, _ref = _hit
        _kind = str(_ref.get("kind") or "")
        _name = str(_ref.get("name") or "")
        _path = str(_ref.get("path") or "")
        _ok = bool(_ref.get("confirmed"))
        _what = {"file": "file path", "url": "URL", "dir": "folder path"}.get(_kind, "target")
        _lines = [f"[Ambient {_at}] {_line}".rstrip(),
                  f"kind: {_kind}",
                  f"name: {_name}"]
        if _ok:
            _lines.append(f"{_what}: {_path}")
            _lines.append("verified: yes - this came from the running application "
                          "itself, you can use it directly.")
        else:
            # 📌 **这一支正是这次实测暴露的问题的解法**：它猜桌面猜中了，
            #    而没有任何人知道那是猜的。⇒ 说清结果可不可信。
            _lines.append(f"{_what}: not verified")
            _lines.append("verified: no - the window title gave the name but the real "
                          "location could not be confirmed. Do NOT invent a path: "
                          "locate it with search_files, or ask the user where it is.")
        return ToolOutcome("\n".join(_lines))

    async def _handle_peek_file(self, args: dict, aid: str, *,
                                event_queue, **_ctx) -> str:
        """试读 —— 花小钱看一眼。"""
        filename = args.get("filename", "")
        if not filename:
            return "peek_file was not executed: `filename` is empty."
        text = await self._read_file_text(filename, False, aid, event_queue)
        if not isinstance(text, str) or len(text) < 1:
            return text

        _key = self._peek_key(filename)
        _first = _key not in self._peeked

        if _first:
            # 🔴 **第一次强制从头 + 固定长度**（用户的悖论）：
            #    「你不先看一部分、没拿到这个文件的任何上下文信息，根本无法
            #      决策接下来要看多少、要看哪里。所以试读就算给模型决策，
            #      它也是在没有任何线索的情况下纯猜。」
            #    📌 **试读是决策的前提，所以它自己不能是决策的产物。**
            # ⚠️ 模型传了 offset/limit 也**明确告诉它被忽略了**，不静默吞掉。
            _sl = _READING.slice_lines(
                text, offset=1, budget_chars=_READING.PEEK_CHARS,
                hard_cap_chars=_READING.PEEK_CHARS)
            self._peeked[_key] = True
            _note = ""
            if args.get("offset") or args.get("limit"):
                _note = ("\n[Your offset/limit were ignored: the first peek of a file "
                         "always starts at the top with a fixed size, because at that "
                         "point there is nothing to base a choice on. Peek again to "
                         "choose freely.]")
            _hint = ""
            if _READING.needs_peek(len(text)):
                _hint = ("\n[This is a map, not the content. Do not answer the user "
                         "from this first peek alone - read the part you need with "
                         "load_full_file, or peek another spot first.]")
            return _READING.render(_sl, peek=True, filename=filename) + _note + _hint

        # 之后的 peek：模型自选位置和长度 —— 它已经有线索了，悖论不再成立。
        # ⚠️ 仍然受 `MAX_READ_CHARS` 硬闸约束（安全兜底对所有读取都成立）。
        _sl = _READING.slice_lines(
            text, offset=args.get("offset"), limit=args.get("limit"),
            budget_chars=_READING.PEEK_CHARS,
            hard_cap_chars=_READING.MAX_READ_CHARS)
        self._wm_add(EntryType.FILE_READ, filename, "peek",
                     detail=f"lines {_sl['start']}-{_sl['end']}", tags=["file", filename])
        return _READING.render(_sl, peek=True, filename=filename)

    async def _handle_load_full_file(self, args: dict, aid: str, *,
                                     event_queue, **_ctx) -> str:
        filename = args.get("filename", "")
        with_images = bool(args.get("with_images", False))
        result_text = await self._read_file_text(filename, with_images, aid, event_queue)

        # ── 大文件必须先试读 ─────────────────────────────────────
        # 🔴🔴 **这里是「拒绝」，不是「悄悄给你一个试读」**。
        #    后者零浪费，但它会让**模型以为自己在精读，拿到的却是试读** ——
        #    那正是我们刚把试读拆成独立工具要消灭的东西。
        # 📌 多花一轮，换「模型永远知道自己在干什么」。
        # ⚠️ 而这一轮不是白花：拒绝信息里带着文件规模和下一步该干什么，
        #    要的「失败信息足够选出下一步」在这里是满足的。
        if (isinstance(result_text, str)
                and _READING.needs_peek(len(result_text))
                and self._peek_key(filename) not in self._peeked):
            # ⚠️ **必须标 `failed=True`** —— 实测 2026-08-26 抓到：
            #    handler 返回字符串时 `ToolOutcome.of()` 默认 `failed=False`，
            #    于是工具卡显示 **「加载文件 ✓」**，而它其实**什么都没加载**。
            # 📌 一次没做成的事显示成成功，比不显示更糟：
            #    用户以为读到了、模型也少了一个「这次不算数」的信号。
            #    （同「说清结果可不可信」那条判据。）
            return ToolOutcome(
                f"Not read yet - this file is large ({len(result_text):,} characters, "
                f"{len(result_text.splitlines()):,} lines).\n"
                f"Call peek_file(filename=...) first to see what is in it, then come "
                f"back and read the part you actually need.\n"
                f"If you already know the exact line you want (for example from "
                f"search_files), say so by passing that offset - having located it "
                f"already counts as knowing the file.\n"
                f"Reading it blindly from the top would cost many turns for nothing.",
                failed=True,
            )

        # ── scratchpad：笔记搭在【本次调用的参数】上 ──────────────
        # ⭐⭐ **载体就是工具参数，不用解析正文**。
        #    文档 原设计是「要求模型在特定 XML 或 Markdown 块中输出」——
        #    那依赖**模型愿意在调工具时同时写正文**，而那不是 schema 能强制的。
        #    📌 一个「靠模型自觉产出」的载体，漏一轮就断一轮，而我们不会知道。
        # ⭐ 而 `notes` 是参数：它在 assistant 消息里天然留着，
        #    **不需要我们再注入回去**，也不会被 `answer_discard` 影响。
        _notes_raw = args.get("notes") or ""
        _notes, _cut = _READING.clamp_notes(_notes_raw)
        _note_back = ""
        if _cut:
            # ⚠️ 截断了**必须说** —— 📌 静默截断笔记 = 模型以为自己记住了，
            #    下一轮发现线索没了，而它不知道为什么。
            _note_back = (f"\n[Your notes were cut to {_READING.MAX_NOTES_CHARS} "
                          f"characters. Notes are for conclusions and coordinates, "
                          f"not for copying text - the copy would defeat the point.]")

        if not isinstance(result_text, str):
            return result_text

        _sl = _READING.slice_lines(
            result_text, offset=args.get("offset"), limit=args.get("limit"),
            budget_chars=_READING.READ_STEP_CHARS,
            hard_cap_chars=_READING.MAX_READ_CHARS)
        result_text = _READING.render(_sl, filename=filename) + _note_back

        self._full_file_hit_this_turn = True
        self._wm_add(
            EntryType.FILE_READ, filename, "full_file_read",
            detail=f"lines {_sl['start']}-{_sl['end']} / {_sl['total_lines']}",
            tags=["file", filename],
        )
        return result_text


    # ══════════════════════════════════════════════════════════════════
    # Subagent
    # ══════════════════════════════════════════════════════════════════
    #
    # ⭐⭐ **上下文隔离天然正确，不需要为它写代码**（早先的设计 2026-08-13 预先核实）：
    #    Subagent有自己的 `ctx`，回到主模型的**只有最终报告**，而报告就是一条
    #    `tool_result`。要做的不是"实现隔离"，是**别破坏它** ——
    #    📌 任何"顺手把 Subagent 的中间步骤也塞给 main agent"的好意，都会直接毁掉这个机制
    #       的全部价值。
    #
    # ⭐⭐ **计价也天然并入 main agent**：`usage_tracker` 是进程级单例，`_record_usage`
    #    挂在 provider 层，所以Subagent的 token 自动进同一本账。
    #    ⚠️ 所以**别在 agent 那边另起一个计数器** —— 那会变成两份账。
    #    ⭐ 而归属对不对，靠的是第 0 步那个 `ContextVar`（见 `core/usage.py`）。
    #
    # ⚠️ **Subagent进 那张后台任务抽屉**（早先的设计 2026-08-12 定）：
    #    agent 是「**无法被回看的后台任务**」—— 进 Running/Finished、有终止 `■`、
    #    也计入 `x running task(s)` pill，与普通后台任务唯一差别是
    #    **主体模型不回看它的过程**（一看就丧失隔离的全部价值）。
    #    📌 「用户能不能看过程」和「主体模型能不能看过程」是两件事。

    # Subagent最多走几步。⚠️ 防失控，不是性能考虑 ——
    # 📌 一个没有步数上限的子循环，坏起来是"一直在跑、一直在花钱、没人看得见"。
    _AGENT_MAX_STEPS = 8

    # ⭐⭐ **同时最多几个Subagent**（2026-08-15 并行）。
    #
    # ⚠️ 这个数**不是性能上限，是成本与可观测性的上限**：
    #    每个Subagent都在独立烧 token，而用户在抽屉里一次能看明白的行数有限。
    # 📌 **一个"想开几个开几个"的并发，第一次失控时的表现是账单，
    #    而账单要到第二天才看得见。**
    # ⭐ 而「并发安全」这件事本身已经在第 0 步解决了（`usage_tracker` 的
    #    `RLock` + `ContextVar` 归属）—— 那两条正是为这一刻修的。
    _AGENT_MAX_PARALLEL = 3

    # ⭐⭐⭐ **Subagent干等多久就交还控制权**（2026-08-20，/）。
    #
    # 🔴 改造前 `spawn_agent` 是**同步 await 到底**的，docstring 还写着
    #    「「后台」在这里指的是上下文隔离，不是"不等它"」—— 而早先的设计同一页写的是
    #    agent 是「**无法被回看的后台任务**」。两句话打架，而实现选了前一句。
    #    后果 实测撞到了：**Subagent一跑，整条前台通道就堵死** ——
    #    插话只能进队列，那一段回应期的元信息行冻在写死的 `thinking · 0s`，
    #    等Subagent跑完才一次跳到 84s。
    # 📌 **一个东西如果在抽屉里叫「running task」，它就不该同时霸占前台。**
    #
    # ⭐ 5s 与 `_MCP_BG_THRESHOLD`(8s) / `_LONG_TASK_HANDBACK_SEC`(**5s**) 同族：
    #    它防的是**干等**，不是「异步」本身。所以短Subagent**一个字都不变** ——
    #    5 秒内回来的照旧同步返回 tool_result，用户完全无感。
    # ⚠️ 取更短是因为Subagent几乎不可能在 5 秒内有意义地跑完（它至少两次模型调用），
    #    而它堵住的东西比一条命令贵得多：整个前台。
    _AGENT_HANDBACK_SEC = 5.0

    @staticmethod
    async def _wait_or_user_speaks(task, timeout: float) -> bool:
        """等它跑完，**或者等到用户又说话了**。返回 True = 它跑完了。

        🔴🔴 **实测：插话之后 `queued` 挂了五十多秒。**
           那不是排队态画错了，是**它真的排了那么久** —— 长任务的前台等待是
           90 秒定长，插话只能在队列里干等它到点。
        📌 **一个「我还要不要干等」的阈值，在用户已经开口之后就失去了前提** ——
           它防的是「没有更值得做的事时白等」，而用户说话恰恰说明有了。
        ⭐ 这不是新机制：`run_command` 早就这么做了
           （`executor_write._new_user_input_arrived()` → `await_briefly(stop_when=)`）。
           📌 **一个正确做法已经在代码里存在、却没被推广到同类场景** ——
              本项目第 N 次（`RLock` / `is_readonly` 都是这个形状）。
        ⚠️ 读的是 `inbox.submit_seq()`（收到过多少条用户消息，单调递增）——
           OS 层那条注释写得最准：**这一层只该读它答得出的那个事实**
           （「有新用户输入了」），不去问「要不要中断本轮」（那是 orchestrator 的判断）。
        ⚠️ 读不出来就退化成纯定时等待（fail-safe 方向 = 照旧等），不许因此不等。
        """
        import asyncio as _a
        _seq0 = None
        try:
            from core.runtime import inbox as _ib
            _seq0 = _ib.submit_seq()
        except Exception:
            _seq0 = None
        if _seq0 is None:
            _done, _ = await _a.wait({task}, timeout=timeout)
            return task in _done
        _deadline = _a.get_event_loop().time() + float(timeout)
        while True:
            _left = _deadline - _a.get_event_loop().time()
            # ⚠️⚠️ **先 await 一次，再判到点** —— 顺序反了会丢掉一个隐含语义：
            #    `asyncio.wait({task}, timeout=0)` 也**至少让出一次事件循环**，
            #    于是那个刚 `create_task` 出来的协程有机会跑到第一个 await。
            #    🔴 第一版把「到点就 return」放在前面，`timeout=0` 时那个任务
            #       **一步都没跑过** —— `t_recheck_longtask` 当场抓到（它正是用
            #       `_LONG_TASK_HANDBACK_SEC = 0.0` 驱动慢路径的）。
            #    📌 **把一个 `wait(timeout=T)` 换成自己的轮询循环时，
            #       要连它在 T=0 时的行为一起接管** —— 那不是边界情况，
            #       那是测试驱动慢路径的标准手法。
            _done, _ = await _a.wait({task},
                                     timeout=(0.25 if _left > 0.25
                                              else max(_left, 0.0)))
            if task in _done:
                return True
            if _left <= 0:
                return task.done()
            try:
                from core.runtime import inbox as _ib2
                if _ib2.submit_seq() > _seq0:
                    logger.info("[LongTask] 用户又说话了 → 立刻交还控制权，不等满阈值")
                    return False
            except Exception:
                pass

    def _a4_rec(self, job_id: str) -> dict:
        """取（或建）那个Subagent的记录。"""
        _tr = getattr(self, "_a4_transcripts", None)
        if _tr is None:
            _tr = self._a4_transcripts = {}
        return _tr.setdefault(job_id, {
            "label": "", "instruction": "", "started": 0.0, "ended": 0.0,
            "steps": [], "report": "", "ok": None,
        })

    def _agent_sem(self):
        """并发闸。⚠️ 懒建：`asyncio.Semaphore` 必须绑在**运行中的** loop 上。"""
        _s = getattr(self, "_a4_sem", None)
        if _s is None:
            import asyncio as _a
            _s = self._a4_sem = _a.Semaphore(self._AGENT_MAX_PARALLEL)
        return _s

    def agent_run(self, task_id: str) -> dict:
        """某个Subagent这一次运行的**完整记录**（监控抽屉照着它画）。

        ```
        {"label", "instruction", "started", "ended", "steps": [...], "report", "ok"}
          steps: [(工具名, 参数, 结果文本, 是否失败), ...]
        ```

        ⭐⭐ 它要能**在跑的过程中**被读到，不是跑完才有 ——
           📌 用户要的是**监控**，不是结果：花了多少、走到哪，得在它还在跑时看得见。
        """
        return dict((getattr(self, "_a4_transcripts", None) or {}).get(task_id) or {})

    def agent_transcript(self, task_id: str) -> list:
        """某个Subagent走过的每一步 `(工具名, 参数, 结果文本, 是否失败)`。

        ⭐⭐ **只活在本次运行的内存里，刻意不落盘。**
           📌 这不是省事，是**与抽屉的语义对齐**：已定的 Finished 保留规则
              就是「**本次运行产生的**」（关掉程序就清空）。
              transcript 的寿命如果比抽屉长，那多出来的部分**没有任何入口能看到它** ——
              而一份看不到的记录，只是磁盘上的垃圾。
        ⚠️ 与「主体模型不回看Subagent过程」不冲突：那条掐的是**模型**那一路，
           这里是**用户**那一路（早先的设计 2026-08-12 把这两件事拆开过）。
        """
        return list(self.agent_run(task_id).get("steps") or [])

    _AGENT_SYS = (
        # ⚠️ 开头这句也**不许只说 investigate** —— 它是整段里最先被读到的一句，
        #    只说"调查"会把「机械性批量修改」那一类悄悄框掉。
        #    📌 一段说明的第一句就是它的默认值。
        "You are a sub-agent dispatched by Nano to carry out one specific job "
        "- either finding something out, or making a set of precise, "
        "fully-specified edits - and report back once.\n"
        "You start cold: you cannot see the conversation that sent you, and you "
        "cannot ask questions. Work only from the instruction given.\n"
        # 🔴🔴 **实测 2026-08-20：这三句话把 整个作废了。**
        #    原文是「You can only read. You cannot change anything on this
        #    computer…」—— Subagent照着它回了一份「我的角色被限制为只读权限，
        #    无法完成第 3 步的文件改写」，而它手里**明明有 `edit_file`**。
        #    📌 那一族的最坏形态：能力在、模型看不见 —— 而这次更糟，
        #       **明确告诉了它相反的事**。工具表改了、它读的那份说明没改。
        #    ⚠️ 而它不会报错：一个被告知"我不能"的执行者，会安静地不去做。
        "You can read and search this computer, and you can make precise edits "
        "to existing files with `edit_file`.\n"
        "Every edit you make is shown to the user for approval first, exactly "
        "like Nano's own edits are. That is normal - do not treat it as a "
        "restriction, and do not ask permission in your report instead of just "
        "making the edit.\n"
        "If an edit is denied, or a capability is switched off, say so plainly "
        "in your report and move on with the rest of the task - do NOT retry it, "
        "do NOT look for a way around it, and do NOT abandon the whole job over "
        "one refused step.\n"
        "You cannot delete or move files, run commands, touch the screen or the "
        "user interface, write Skills, or dispatch further sub-agents. If the "
        "task truly needs one of those, report that back and let Nano do it.\n"
        "Investigate with the tools you have, then answer with your findings. "
        "Your answer goes back to Nano, not to the user, so write it as a report: "
        "state what you found, name the concrete files/terms involved, and say "
        "plainly what you could NOT determine. Do not pad it, and do not invent "
        "anything you did not actually read."
    )

    async def _run_agent_loop(self, instruction: str, aid: str, event_queue,
                              job_id: str = "") -> tuple[str, int]:
        """跑一次隔离的 ReAct。返回 `(报告, 用掉的步数)`。**不碰 main agent 的上下文。**"""
        from core.tools.catalog import ToolScope as _TS

        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        # ⭐ 工具集来自 **AGENT 作用域的白名单**（`builtin.D(..., agent=True)`）——
        #    📌 新加的内置工具只声明 MAIN，**天然不在这里**，不需要有人记得排除它。
        _defs = _cat.advertised(_TS.AGENT, _rtv)
        _manifest = [d.manifest for d in _defs]
        if not _manifest:
            return "（Subagent没有任何可用工具，无法调查）", 0

        # ⚠️⚠️ 上下文用 **`ChatMessage.to_dict()`**，不许手搓字典。
        #    🔴 第一版手写了 `{"role": "tool_results", ...}` —— 实测第一次派 Subagent
        #       就 400：`Invalid value for 'messages[2].role': 'tool_results'`。
        #       那个 role 是**项目内部的形状**；`to_dict()` 才负责把它翻成
        #       Anthropic 认的 `user + tool_result blocks`。
        #    📌 **一个已经有规范转换的形状，手搓第二份的代价不是"多写几行"，
        #       是「它在哪一步变形」这件事从此有两个答案。**
        #    ⭐ 走同一条转换，往后 provider 改格式这里自动跟；
        #       而且这正是 `t_f5_decay_l2` 那个假 provider 的教训的反面 ——
        #       那次是假 provider 和调用方**一致地错**，两边出自同一处。
        _msgs: list = [ChatMessage(role="user", content=instruction)]
        _steps = 0
        for _steps in range(1, self._AGENT_MAX_STEPS + 1):
            # ⚠️⚠️ **这里刻意不传 `stable_tool_count`。**
            #    Subagent用的是它自己的 `_manifest`，而 `_core_stable_n` 数的是
            #    主循环那份 `_core_manifest` 的前缀长度 ——
            #    📌 **一个「这份名单的前 N 个」的数字，配错名单就会把断点
            #       打在错的位置上，而且不报错**（缓存照样"工作"，只是命中率变差）。
            #    ⭐ 不传 → 退回旧行为（断点打在最后一个上），对Subagent是正确的：
            #       它的工具集在一次Subagent任务内本来就是固定的。
            _dec, _model = await self.provider.chat_with_tools(
                [m.to_dict() for m in _msgs], _manifest, self._AGENT_SYS)
            _calls = list(getattr(_dec, "tool_calls", None) or [])
            if not _calls:
                return (getattr(_dec, "content", "") or "").strip(), _steps

            _m = ChatMessage(role="tool_calls", content="")
            _m.tool_calls = list(_calls)
            # ⚠️ thinking blocks 必须原样带回（签名不能改写），否则下一轮 400。
            _m.thinking_blocks = list(getattr(_dec, "thinking_blocks", None) or [])
            _msgs.append(_m)
            _results = []
            for c in _calls:
                # ⚠️ **用 AGENT 作用域 resolve** —— 这一行就是隔离的执行侧闸门：
                #    模型哪怕幻觉出 `os_execute`，这里也拿不到 handler。
                #    📌 的教训：给出去的一定要执行得了；反过来
                #       「执行得了却没给出去」是正常的，而这里是第三种：
                #       **既没给出去、也执行不了** —— 那才是真正的隔离。
                _ref = _cat.resolve(c.name, _TS.AGENT, _rtv)
                if _ref is None:
                    _txt, _err = (f"[Tool not available to sub-agents: {c.name}]", True)
                else:
                    try:
                        _out = await getattr(self, _ref)(
                            c.args or {}, aid, event_queue=event_queue,
                            call=c, used_model=_model, gui_waited=False)
                        _oc = ToolOutcome.of(_out)
                        _txt, _err = _oc.text, _oc.failed
                    except Exception as _e:
                        logger.warning(f"[A4] Subagent工具 {c.name} 失败: {_e}")
                        _txt, _err = f"[Tool error] {_e}", True
                _results.append(ToolResultBlock(
                    name=c.name, tool_use_id=c.tool_use_id,
                    content=_txt, is_error=_err))
                # ⭐ 记一步，供抽屉里的 View transcript 用。
                if job_id:
                    _rec = self._a4_rec(job_id)
                    _rec["steps"].append((c.name, dict(c.args or {}), _txt, _err))
            _rm = ChatMessage(role="tool_results", content="")
            _rm.tool_results = _results
            _msgs.append(_rm)

        # ⚠️ 撞上限也要**如实说**，不许假装这是完整结论。
        #    📌 一份没说自己没跑完的报告，会被 main agent 当成定论继续往下推。
        _last = ""
        for _m2 in reversed(_msgs):
            if getattr(_m2, "tool_results", None):
                _last = str(_m2.tool_results[-1].content or "")[:1500]
                break
        return (f"（⚠️ Subagent走满了 {self._AGENT_MAX_STEPS} 步仍未收敛，以下是"
                f"它最后掌握的情况，**不是完整结论**）\n" + _last, _steps)

    async def _handle_spawn_agent(self, args: dict, aid: str, *,
                                  event_queue, used_model: str = "", **_ctx) -> str:
        """派一个 Subagent。**超过 `_AGENT_HANDBACK_SEC` 就把控制权交回 main agent。**

        ⭐⭐ Subagent是「**无法被回看的后台任务**」。三件事同时成立：
          · 进抽屉 / 计入 pill / 有 `■`        —— 它是一件真的在跑的东西
          · main agent **不干等它**（超 5s 就交还）—— 否则一件"后台"的事堵死前台
          · **永远不回看**（`recheck=False`）   —— 回看 = 把它走过的步骤灌回主
            上下文，那正好抵消掉上下文隔离的全部价值

        ⚠️ **快路径（5 秒内跑完）与改造前逐字一致**：报告作为 tool_result 直接回
           main agent。📌 一个阈值防的是「干等」，不该让不干等的那些也改变行为。

        ⚠️ **权威记录的收尾始终在 `_agent_runner` 里**，不交给载体那一层 ——
           理由见下面 `owns_record=False` 那一行。
        """
        _ins = (args.get("instruction") or "").strip()
        _label = (args.get("label") or "").strip() or (_ins[:28] or "子任务调查")
        if not _ins:
            return "[spawn_agent] instruction 是空的，没有可调查的东西。"

        from core.runtime.task import create_background_job, finish_background_job
        _job = None
        try:
            _job = create_background_job(f"Agent · {_label}")
        except Exception as _e:
            # ⚠️ 建不了记录**不阻止Subagent跑** —— 那只是抽屉里少一行。
            #    📌 治理/展示层的故障，不许把能力本身搞掉。
            logger.warning(f"[A4] Subagent的后台任务记录建不了（不影响执行）: {_e}")
        _jid = str(getattr(_job, "task_id", "") or _job or "")
        if _jid:
            import time as _t4
            _rec0 = self._a4_rec(_jid)
            _rec0.update({"label": _label, "instruction": _ins,
                          "started": _t4.time()})

        def _wrap(_report: str, _steps: int) -> str:
            # ⚠️ 报告**原样回给 main agent**，由它转述给用户——
            #    📌 main agent 是协调者；两个声音同时说话会让用户分不清谁在负责。
            #    ⭐ 用户想看原文时看得到：抽屉里那条的 View transcript
            #       与聊天区工具卡展开（那个出口）。
            return (f"[Sub-agent report · {_label}]\n"
                    f"（investigated in {_steps} step(s); this text is the agent's own "
                    f"words — relay it to the user in your own voice, and say so if it "
                    f"reports something it could not determine）\n\n{_report}")

        async def _agent_runner() -> str:
            # ⭐⭐ `begin_agent()` 搬进**这条协程里**（改造前在 handler 体内）。
            #    `create_task` 只在建任务那一刻复制上下文，之后两边各自独立 ——
            #    于是这个归属 ContextVar 只作用在Subagent自己这一支。
            #    🔴 留在 handler 里的话，交还之后 main agent 继续跑的模型调用会**接着**
            #       落在同一个 context 上，被记进这个Subagent的账。
            #    📌 一个用来「归属」的 ContextVar，必须在它所归属的那条执行链里 set。
            _ok, _report, _steps = True, "", 0
            # ⭐ 这一支从此带着「我是Subagent」这个事实往下走 ——
            #    授权弹窗据它画来源标识，确认闸据它决定「用户说话算不算取消」。
            #    ⚠️ 与 `begin_agent()` 放在一起是刻意的：两者都是**归属**，
            #       都必须在它们所归属的那条执行链里 set。
            _agent_scope_ctx.set(_label)
            if _jid:
                try:
                    from core.usage import usage_tracker as _ut4
                    _ut4.begin_agent(_jid)
                except Exception:
                    pass
            _outcome = "completed"
            try:
                # ⭐ 并发闸：多个Subagent可以同时在跑，但不超过 `_AGENT_MAX_PARALLEL`。
                #    ⚠️ 排队时那条**已经在抽屉的 Running 段里**
                #       （`create_background_job` 在闸之前）——
                #       📌 用户该看到「它在排队」，而不是「它不存在」。
                async with self._agent_sem():
                    # ⭐⭐ **拿到 slot 才算「在跑」** —— 在此之前它是 ACTIVE+IDLE，
                    #    抽屉如实显示「排队中」。
                    # 🔴 实测 2026-08-20：抽屉里一个**正在跑**的Subagent一直显示「排队中」。
                    #    根因是 `mark_background_running()` 的唯一调用方是
                    #    `_run_bg_task`，而它的唯一调用方是 `_start_bg_task` ——
                    #    后者在 2026-08-10「系统交还不再写后台 Task 权威记录」之后
                    #    就**零生产调用方**了。于是这行状态**再也没有人写过**，
                    #    每一个真的后台任务都永久停在 IDLE。
                    #    📌 **一条状态如果只有一个写入者，那个写入者一死，
                    #       它就变成一个永远不会改变的谎** —— 而读它的人不会报错。
                    #    ⚠️ 这是同一条死路的第三个受害者（前两个：抽屉那颗 `■`、
                    #       `_bg_tasks` 恒空）。
                    try:
                        from core.runtime.task import mark_background_running as _mbr
                        _mbr(_jid)
                    except Exception as _e_mr:
                        logger.debug(f"[A4] Subagent转 RUNNING 失败（照旧跑）: {_e_mr}")
                    _report, _steps = await self._run_agent_loop(
                        _ins, aid, event_queue, job_id=_jid)
                if not _report:
                    _ok, _report = False, "（Subagent没有给出任何结论）"
                _outcome = "completed" if _ok else "failed"
            except asyncio.CancelledError:
                # ⭐⭐ 用户点了抽屉里那颗 `■`。**`cancelled` 不许并进 `failed`** ——
                #    早先那条实测结论：用户主动停掉不是失败，归进 failed
                #    会让模型和用户都去排查一个不存在的问题。
                _ok, _outcome = False, "cancelled"
                _report = ("（这个Subagent被用户手动终止了，没有给出结论。"
                           "不要自行把它重新派一遍。）")
                if _jid:
                    try:
                        finish_background_job(_job, _outcome, "用户手动终止")
                    except Exception:
                        pass
                    import time as _t6
                    self._a4_rec(_jid).update(
                        {"report": _report, "ok": False, "ended": _t6.time()})
                logger.info(f"[A4] Subagent「{_label}」被用户终止")
                # ⚠️ **必须原样抛上去**：载体那一层靠它区分「被终止」与「失败」，
                #    吞掉就变成一次假的失败（app 侧 `_handback_await` 同款纪律）。
                raise
            except Exception as _e:
                _ok, _outcome = False, "failed"
                _report = f"（Subagent执行失败：{_e}）"
                logger.warning(f"[A4] Subagent失败: {_e}")
            if _job:
                try:
                    finish_background_job(_job, _outcome)
                except Exception:
                    pass
            if _jid:
                import time as _t5
                self._a4_rec(_jid).update(
                    {"report": _report, "ok": _ok, "ended": _t5.time()})
            logger.info(f"[A4] Subagent完成「{_label}」：{_steps} 步 / "
                        f"{'成功' if _ok else '失败'}")
            return _wrap(_report, _steps)

        _task = asyncio.ensure_future(_agent_runner())
        # ⭐ 同两条长任务：用户一开口就立刻交还，不等满 5 秒。
        if await self._wait_or_user_speaks(_task, self._AGENT_HANDBACK_SEC):
            # 快路径：它已经回来了 —— 形状与改造前完全一致，调用方不用改。
            return _task.result()

        # ── 慢路径：交还控制权，Subagent继续跑 ──────────────────────────────────
        # ⚠️ `recheck=False` —— Subagent是唯一走这一档的：**它永远不回看**。
        # ⚠️ `detachable=False` —— 它**已经**不在手头了，再给模型一个
        #    `dont_wait` 是让它对一件已经做完的决定再决定一次。
        #    📌 一个工具只该出现在「它还能改变什么」的时刻。
        # 🔴 **实测第一次就炸在这里**：上一版写的是 `f"agent_{_jid or id(_task):x}"`,
        #    而 `:x` 作用在**整个** `_jid or id(_task)` 上 —— `_jid` 非空时它是 str，
        #    于是 `Unknown format code 'x' for object of type 'str'`。
        #    📌 **一个格式说明符作用的是整个表达式，不是它的某一个分支** ——
        #       而这条路只在「记录建成功了」时走，正好是最常见的那条。
        _agent_ref = "agent_" + (_jid or format(id(_task), "x"))
        _txt = await self._hand_back_long_task(
            display=f"Agent · {_label}", bg_ref=_agent_ref,
            action_id=aid, event_queue=event_queue,
            recheck=False, detachable=False)
        await event_queue.put({
            "event": "long_task_handback",
            "task": _task,
            "bg_task_ref": _agent_ref,
            "display": f"Agent · {_label}",
            # ⭐ Subagent**生来**就有权威记录（`create_background_job` 在最上面），
            #    所以载体这一层只拿它当「那颗 `■` 要终止谁」的钥匙，
            #    **不负责收尾**（`owns_record=False`）。
            #    📌 一条记录只能有一个收尾人 —— 两个都收的表现是
            #       「后收的那个把先收的结论覆盖掉」，而它不会报错。
            "rt_task_id": _jid,
            "owns_record": False,
        })
        return _txt

    async def _handle_query_local_knowledge(self, args: dict, aid: str, *,
                                            event_queue, **_ctx) -> str:
        kb_query = args.get("query", "")

        def _rag_progress_q(msg: str):
            event_queue.put_nowait({"event": "tool_progress", "action_id": aid, "message": msg})
        async with self._rag_parallel_sem:
            result_text = await asyncio.to_thread(
                rag_engine.query_for_agent, kb_query, 6, _rag_progress_q
            )
        self._rag_hit_this_turn = True
        self._wm_add(
            EntryType.RAG_QUERY, kb_query, "rag_search",
            detail=f"returned {len(result_text):,} chars", tags=["rag", kb_query],
        )
        return result_text

    async def _handle_get_file_path(self, args: dict, aid: str, *,
                                    event_queue, **_ctx) -> str:
        filename = args.get("filename", "")
        return await asyncio.to_thread(rag_engine.get_file_path_for_agent, filename)

    async def _handle_recall_conversation(self, args: dict, aid: str, *,
                                          event_queue, **_ctx) -> str:
        """顺着索引条目把那段对话的结论拿回来。

        ⚠️ 返回的是**结论行的完整文本**，不是原文 —— 原文在
           `conversation_messages` 里永远都在，但把它整段搬回上下文，
           正好抵消掉 L3 的全部意义。
        📌 L3 的承诺是「想得起来这件事」，不是「把那一段重新背上」。
        """
        from core.context import bridge as _B
        _q = str(args.get("query") or "").strip()
        if not _q:
            return "[recall_conversation] Provide a query."
        hits = _B.recall(_q, top_k=5)
        if not hits:
            # ⚠️ 空结果**必须说清是哪一种空** —— 不然模型会当成"这件事没发生过"。
            return ("[recall_conversation] Nothing matched. Note this searches only "
                    "exchanges that were moved out of context; if what you are looking "
                    "for is still visible above, just read it there.")
        out = []
        for h in hits:
            _t = str(h.get("canonical_text") or h.get("display_summary") or "").strip()
            if _t:
                out.append("- " + _t)
        if not out:
            return "[recall_conversation] Matched records carried no readable text."
        return "Recalled from earlier in this conversation:\n" + "\n".join(out)

    async def _handle_recall_working_memory(self, args: dict, aid: str, *,
                                            event_queue, **_ctx) -> str:
        kw = args.get("keyword", "")
        etype = args.get("entry_type", "")
        if self._wm is not None:
            return self._wm.format_for_model(keyword=kw, entry_type=etype, limit=15)
        return "[Working Memory] This feature is temporarily unavailable."

    async def _handle_inspect_existing_skill(self, args: dict, aid: str, *,
                                             event_queue, **_ctx) -> str:
        skill_name = args.get("skill_name", "")
        src = (
            self.registry.get_skill_source(skill_name, include_disabled=True)
            if hasattr(self.registry, "get_skill_source") else None
        )
        if src and src.get("code"):
            return f"Real source code of Skill \"{skill_name}\":\n```python\n{src['code']}\n```"
        return f"No Skill named \"{skill_name}\" was found."

    async def _handle_write_user_note(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        # ReAct batch 中不发 user_note_pending terminal 事件（会导致 UI 提前 return，
        # 中断 tool_results 写回，污染 memory）。只写记忆，返回普通 tool_result。
        # 模型在最终回答里自然告知用户"已记住"。
        #
        # ⭐⭐ 五个字段。参数名由 `raw_content` 改为 `content`
        #    （语义没变，名字更贴切）；新增 `applies_when` / `why` /
        #    `summary_model` / `summary_user`，旧的 `display_text` 由后者接手。
        # 🔴 旧实现里 `display_text` **从来没落库** —— 只塞进弹卡事件，弹完就扔，
        #    于是抽屉里一直显示 `detail`（给模型看的那句原文）。
        #    📌 一句话同时服务两个受众，最后两边都不合身。
        # ⚠️ 兼容旧参数名：模型偶尔会照着旧记忆里的形状调用。
        #    📌 兼容是给**过渡期**用的，不是给「两种写法都对」用的 ——
        #       所以只在新名缺失时才回退，且不写进 description。
        content = (args.get("content") or args.get("raw_content") or "").strip()
        applies_when = (args.get("applies_when") or "").strip()
        why = (args.get("why") or "").strip()
        summary_model = (args.get("summary_model") or "").strip()
        summary_user = (args.get("summary_user")
                        or args.get("display_text") or "").strip()

        # ⭐ **闸：说不出「什么时候用得上」就不写。**
        #    📌 这条不是校验参数，是本项目那条判据本身 ——
        #       说不出 when 的东西，本来就不该被记住。
        #    ⚠️ 而且**报错要说清怎么补**：一个只说「不许」的错误，
        #       会让模型换个说法再试一次。
        if content and not applies_when:
            return ("Rejected: applies_when is empty. A memory that cannot say WHEN it "
                    "is relevant will never be usable - it just becomes noise. Either "
                    "give a concrete situation ('when I ask you to edit a spreadsheet'), "
                    "or 'always' if it genuinely applies every turn, or do not save it.")

        if content and self._wm is not None:
            # ReAct 内写为 pending，用户在最终回答后有机会通过 UI 查看/删除。
            # confirmed 状态绕过了用户确认卡片，与产品语义（"写入后用户可删除"）冲突。
            _nid = self._wm.add(
                entry_type=EntryType.USER_NOTE,
                subject="用户笔记", action="记住", detail=content,
                tags=["user_note"], session_id=self._wm_session_id,
                status="pending",
                applies_when=applies_when, why=why,
                summary_model=summary_model or content,
                summary_user=summary_user or content,
            )
            # ReAct batch 内不能发 user_note_pending 终端事件（会截断 tool_results）。
            # 改成攒进本轮列表，final_result 时一并交给 UI 弹气泡 + 进记忆抽屉，不中断循环。
            # ⚠️ 弹卡用的是 **summary_user** —— 那张卡是给用户看的。
            try:
                self._notes_written_this_turn.append(
                    {"note_id": _nid, "display_text": summary_user or content}
                )
            except Exception:
                pass
        return f"User note saved as pending memory: {summary_user or content}"

    async def _handle_run_scratch_code(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> str:
        """跑一段用完就扔的 Python。

        ⭐ 三段：**扫 → （必要时）确认 → 跑**，每一段都复用现成的东西：
          扫   `core.code_scan`（与 Skill 审计**同一份**扫描器）
          确认 `execution_confirm` 事件（与运行有副作用的 Skill **同一个弹窗**）
          跑   `core.temp_exec` → `longcmd`（长任务交还 / 落盘 / 回收全白拿）
        📌 全程没有一样是为这个工具新造的 —— 「直接跟正常 skill 的体感一致」。
        """
        import asyncio as _aio
        from core import temp_exec as _te

        code = (args.get("code") or "").strip()
        purpose = (args.get("purpose") or "").strip()
        if not code:
            return "run_scratch_code was not executed: `code` is empty."

        # ── ① 扫 ──────────────────────────────────────────────────────
        from core import code_scan as _cs
        cats, findings, syn_err = _te.prepare(code)
        if syn_err:
            # ⚠️ 语法错误在**执行前**就返回，不起进程。
            # 📌 让模型拿到 `SyntaxError: invalid syntax (line 3)` 这一句，
            #    比让它拿到一个非零退出码 + 一段 traceback 快得多 ——
            #    要的是「失败信息足够选出下一步」。
            return (f"The code was NOT run: it does not parse.\n"
                    f"SyntaxError: {syn_err}\n"
                    f"Fix the syntax and call the tool again.")

        # ── ② 确认（只在扫出副作用时）──────────────────────────────────
        if cats:
            _confirm_ev = _aio.Event()
            _cancelled = [False]
            _loop = _aio.get_running_loop()

            def _on_confirm():
                _loop.call_soon_threadsafe(_confirm_ev.set)

            def _on_cancel():
                _cancelled[0] = True
                _loop.call_soon_threadsafe(_confirm_ev.set)

            await event_queue.put({
                "event": "execution_confirm",
                # ⚠️ 复用 `skill_name` 这个字段名 —— UI 那边照它渲染标题。
                #    📌 为了一个「其实不是 Skill」而给事件加一个新字段，
                #       会让所有消费方都得处理两种形状。名字略不精确，
                #       换来的是**零改动接进现有弹窗**。
                "skill_name": purpose or "临时代码",
                # ⭐ 传**中文描述**，不传类别键 —— 与 Skill 那条路一字不差。
                "side_effects": _cs.labels_for(cats),
                # ⭐⭐ 代码带进弹窗 —— 让用户知道**自己在授权什么**。
                # 🔴🔴 UI 侧必须以**只读**方式渲染它。
                #    理由不只是保险：**风险类别是 AST 在【这段代码】上扫出来的，
                #    用户一改，授权就和被授权的东西对不上了** —— 他可以把一段
                #    「无副作用」的代码改成写文件的，而结论还挂着旧的。
                #    ⚠️ Skill 那边能改，是因为改完**还会再过一遍审计管线**；
                #       一次性执行**没有第二遍**。
                "preview_code": code,
                "on_confirm": _on_confirm,
                "on_cancel": _on_cancel,
            })
            from core.runtime import inbox as _ib
            _oc = await _ib.wait_confirm_or_user_message(_confirm_ev, 300)
            if _oc != _ib.ConfirmOutcome.CONFIRMED:
                _cancelled[0] = True
            if _cancelled[0]:
                # ⚠️ 同 Skill 那条：**用户改口** vs **干等超时**要分开告诉模型。
                #    📌 两者对「下一步该干什么」的含义完全不同。
                if _oc == _ib.ConfirmOutcome.USER_MESSAGE:
                    return ("The code was NOT run.\n"
                            + _ib.cancelled_by_user_message_note())
                return ("The code was NOT run: the user did not approve it. "
                        "Do not retry the same code - either ask what to change, "
                        "or find another way.")

        # ── ③ 跑 ──────────────────────────────────────────────────────
        try:
            lc = _te.start(code, display=purpose or "scratch code")
        except Exception as e:
            return f"The code could not be started: {type(e).__name__}: {e}"

        from core.os_layer import longcmd as _lc
        _te.cleanup()          # 顺手清过期的临时文件（家务，失败不影响）

        if await _lc.await_briefly(lc, self._LONG_TASK_HANDBACK_SEC,
                                   stop_when=_lc.new_user_input_arrived()):
            res = lc.final_result()
            _lc.forget(lc.ref)
            return self._format_scratch_result(res, cats)

        # ⭐ 超过前台耐心 → 走**同一条**长任务交还合同（与 MCP / run_command 同）。
        #    📌 「我们要识别的只有长任务，跟任务类型从来没有关系过。」
        return await self._hand_back_long_task(
            display=purpose or "临时代码",
            bg_ref=lc.ref, action_id=aid, event_queue=event_queue)

    @staticmethod
    def _format_scratch_result(res: dict, cats: list) -> str:
        """把 `longcmd` 的结果转成给模型的一段话。

        ⚠️ **非零退出码不是「工具坏了」，是「代码报错了」** —— 这两者对模型
           的含义完全不同：前者该换个工具，后者该改代码。
           📌 失败信息必须**正确**，而且要**足够选出下一步**。
        """
        d = res.get("data") or {}
        out = (d.get("output") or "").strip()
        rc = d.get("returncode")
        if res.get("ok"):
            head = "The code ran successfully."
            if not out:
                # ⚠️ 空输出是个真实的坑：模型算完了但忘了 print。
                #    📌 明说「没有输出」比给一个空串强 —— 空串会被读成
                #       「结果就是空的」，而真相是它压根没打印。
                return (head + " It printed nothing, so there is no result to show. "
                        "If you expected a value, add a print() and run it again.")
            return f"{head} Output:\n{out}"
        # 失败：把退出码和输出都给出去
        return (f"The code ran but exited with code {rc} - this is an error in the "
                f"code itself, not a problem with the tool. Output (stdout+stderr):\n"
                f"{out or '(nothing was printed)'}")

    async def _handle_forget_user_note(self, args: dict, aid: str, **_ctx) -> str:
        """删掉一条记忆。**软删除**，底下是 `status='deleted'`。

        ⚠️ 两个入口（用户在抽屉里删 / Nano 调这个）走**同一个** `soft_delete_by_id` ——
           📌 一个动作有两个实现，它们只在「我两次想法相同」的前提下一致。
        ⚠️ 找不到那条 id 时**明说找不到**，不要静默成功 ——
           📌 一个「删了但其实没删」的成功回执，会让模型以为水位降了，
              然后对用户复述一个假的结果。
        """
        try:
            _nid = int(args.get("note_id"))
        except (TypeError, ValueError):
            return ("Rejected: note_id must be the integer id shown next to the memory. "
                    "Look it up in the memory list first.")
        if self._wm is None:
            return "Memory store is unavailable right now; nothing was deleted."
        try:
            _ok = self._wm.soft_delete_by_id(_nid)
        except Exception as e:
            logger.warning(f"[O2] 删除记忆 {_nid} 失败: {e}")
            return f"Could not delete memory #{_nid}: {e}"
        if not _ok:
            return (f"No memory with id {_nid} was found (it may already be deleted). "
                    f"Nothing changed - do not tell the user it was removed.")
        _why = (args.get("reason") or "").strip()
        logger.info(f"[O2] 记忆 #{_nid} 已软删除"
                    + (f"（{_why}）" if _why else ""))
        return (f"Memory #{_nid} deleted. It is gone from what you see each turn and "
                f"from the user's memory drawer.")

    async def _handle_create_task_list(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> str:
        title = args.get("title", "Task")
        steps = args.get("steps", [])
        # 初始化：所有步骤状态为 todo
        task_state = {
            "title": title,
            "steps": [
                {"id": s.get("id", f"step_{i}"), "desc": s.get("desc", ""), "status": "todo", "note": ""}
                for i, s in enumerate(steps)
            ],
        }
        await event_queue.put({
            "event": "task_list_update",
            "task_state": task_state,
        })
        return f"Created task list \"{title}\" with {len(steps)} step(s)."

    async def _handle_update_task_step(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> str:
        step_id = args.get("step_id", "")
        status = args.get("status", "done")
        note = args.get("note", "")
        await event_queue.put({
            "event": "task_step_update",
            "step_id": step_id,
            "status": status,
            "note": note,
        })
        return f"Step \"{step_id}\" was updated to {status}."

    async def _handle_render_visual(self, args: dict, aid: str, *,
                                    event_queue, **_ctx) -> str:
        _viz_html = args.get("html", "") or ""
        _viz_title = args.get("title", "") or ""
        import uuid as _uuid_v
        _viz_token = _uuid_v.uuid4().hex[:12]
        await event_queue.put({
            "event": "visual_render",
            "html": _viz_html,
            "title": _viz_title,
            "token": _viz_token,
        })
        return (
            f"Rendered the visual \"{_viz_title or 'visual'}\" for the user. "
            "Continue with one or two sentences explaining what it shows."
        )

    async def _handle_look_at_screen(self, args: dict, aid: str, *,
                                     event_queue, **_ctx) -> str:
        # 截图自查：截屏 → 视觉模型理解 → 返回描述。默认排除 Nano 自己窗口。
        _purpose = (args.get("purpose") or "").strip() or "understand the current screen state"
        _region = (args.get("region") or "full").strip().lower()
        _include_self = bool(args.get("include_self", False))
        return await self._look_at_screen(_purpose, _include_self, event_queue, _region)

    async def _handle_note_image(self, args: dict, aid: str, *,
                                 event_queue, **_ctx) -> str:
        """把模型当轮写下的图片描述挂到那条 user 消息上。

        ⚠️ 落到**账本 + storage 两处**：storage 让本轮之后的压缩能用上它，
           落盘让重启后 `_restore_image_notes` 还算得出来。
        ⚠️ 写完之后 `has_unsummarized_image()` 立刻为假 → 工具和动态段**一起消失**。
           📌 这就是"不会每轮注入"的**结构性**保证，不是靠谁记得。
        """
        _s = (args.get("summary") or "").strip()
        if not _s:
            return "Nothing was written down — summary was empty. Try again with the actual content."
        _n = self.memory.set_image_summary(_s)
        # ⚠️ 标志无论如何都要落下 —— 哪怕落库失败。
        #    📌 否则「记不下来」会变成「每一轮都再要求它记一次」，
        #       而那正是 已明确不许出现的形状（像 base64 那个 bug 一样每轮注入）。
        self._turn_image_pending = False
        if not _n:
            return ("There is no image on the current turn to write down, so nothing was saved. "
                    "Just answer the user.")
        return ("Noted. This is stored for your future self only — now answer the user's actual "
                "message in your own voice. Do NOT recite the summary back to them.")

    async def _handle_view_past_image(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        """把盘上那张原图重新喂给视觉模型，返回**文字**。

        ⭐ 与 `look_at_screen` 同一条通路（`_vision_ask`），刻意不发明第二套：
           那边早就解决了"图怎么进视觉模型、结果怎么变成 tool_result"。

        ⚠️ **不把像素塞回主上下文** —— 那正好抵消了压缩，而且每次回看都要再付一遍。
           📌 回看的产物是**一句关于那个细节的回答**，不是"把图重新挂回历史"。

        ⚠️ 也**不发 `screenshot_preview` 上屏**：用户此刻正看着自己发的那张图
           （之后它一直在 UI 里），再画一遍纯属重复。
           📌 与 `look_at_screen` 的差别正在这里 —— 那张图用户**没见过**。
        """
        _h = (args.get("handle") or "").strip()
        _q = (args.get("question") or "").strip() or "Describe what is in this image."
        try:
            from core.runtime.blobs import resolve_handle, image_path
        except Exception as _e:
            return f"(Cannot look at past images right now: {_e})"
        _ref = resolve_handle(_h)
        if not _ref:
            # ⚠️ 认不出**必须响亮地说**，不许含糊成"图没了" —— 前者用户能改（换个把手），
            #    后者会让模型转头去要求用户重新上传，正是 一开始那个 bug。
            return (
                f"No stored image matches handle {_h!r}. Handles look like img#a3f2c1d4 and "
                "appear in the system note on the message that carried the image. "
                "Do not guess, and do not tell the user the image is gone — say you could not "
                "resolve that handle."
            )
        _p = image_path(_ref)
        if _p is None:
            # 🔴 真的没了（用户清了 data/chat_images/）。**这时才轮到请用户重发。**
            return (
                "That image was stored earlier but its file is no longer on disk (the image "
                "library may have been cleared). You still saw it at the time — answer from what "
                "you already know if you can. If you truly need the pixels, now it IS correct to "
                "ask the user to send it again, and say why."
            )
        if self.provider is None:
            return "(The vision model is unavailable, so the image cannot be re-examined.)"
        try:
            _png = await asyncio.to_thread(_p.read_bytes)
            _ans = await self._vision_ask(
                _png,
                f"This is an image the user sent earlier in the conversation. Need: {_q}\n"
                "Answer briefly and truthfully. If the detail cannot be determined from the "
                "image, say so plainly instead of guessing — and if it is a count that is only "
                "approximable, say it is approximate.",
                "You are Nano's eyes. Describe only what is actually visible in this image.",
            )
            logger.info(f"[D10] 回看 {_ref[:8]} → {_q[:40]}")
            return _ans or "(The vision model returned nothing.)"
        except Exception as e:
            return f"(Failed to re-examine that image: {e})"

    async def _handle_load_tools(self, args: dict, aid: str, *,
                                 event_queue, **_ctx) -> str:
        # 按需加载工具：匹配全量池 → 暂存，由 ReAct 循环在本批结束后 append 进 tools_manifest
        # ⑤ 匹配问目录 —— 改造前是一张**人工维护的关键词分组表**（`_GROUPS`：
        # "网页/浏览/browser…" → `nm.startswith("browser_")` 之类），加一个新工具
        # 就要记得往表里塞一条，漏了的表现是「load_tools 搜不到它」。
        # ⭐ 现在检索文档由声明**自动**构建（name + awareness + description +
        #    所有 property 名 + **所有 enum 值**），于是
        #    `load_tools(query="file_delete")` 天然精准命中 `os_execute` ——
        #    因为 39 个 action 的 enum 全在它的检索文档里，
        #    **而 awareness 行一个 action 都不用列**。
        # ⚠️ 只在本作用域此刻 eligible 的集合里搜（HIDDEN 的搜不到）——
        #    搜出一个当前不该出现的工具，等于把 eligible 那条不变量从后门破掉。
        _matched = [
            d.manifest for d in self._get_tool_catalog().search(
                args.get("query", ""), args.get("names") or [],
                scope=ToolScope.MAIN, runtime=self._tool_runtime_view())
        ]
        # ⭐⭐⭐ [2026-08-23 实测回归] **必绑定的工具，加载也要绑定。**
        #
        # 🔴 实测症状：模型 `load_tools(computer_use)` 之后，要缩小窗口时填了
        #    `computer_use(action='set_window_mode')` → 「unknown action …
        #    actions must come from the closed enum and cannot be invented」。
        #
        # ⭐ 它不是在乱编：`computer_use` 的描述里明写着「先 call
        #    set_window_mode('mini')」，而 `set_window_mode` 是 DEFERRED、
        #    **这一轮根本没被加载**。手上没有那个工具，它只好拿手上有的那个凑。
        #
        # 📌 **我们声明了「必绑定」，却把绑定的两个放在了不同的加载状态** ——
        #    这正是这次拆分要消除的那个问题（「一组必绑定的东西被放进两个不同的
        #    可见性层级」），而在修它的同一轮里又造了一次。
        # 📌 **一个绑定关系，该由系统保证，不该靠模型记得一起加载。**
        #    靠模型记得 = 又一条「要求每个调用方自觉」的机制，而它漏的时候不报错，
        #    只会让模型编一个不存在的 action。
        #
        # ⚠️ 只补**真正的强绑定**的：`computer_use` 一旦上场，Nano 自己的窗口
        #    必然挡在它要操作的那块屏幕前面 —— 这是**系统事实**，不是偏好。
        #    ⚠️ `look_at_screen` **不在这里**：它和 computer_use 是「常一起出现」，
        #       不是「缺了就干不成」。📌 把「经常一起」当成「必须一起」，
        #       会让按需加载退化成打包加载。
        # ⚠️ 2026-08-23 机械扫了一遍**全部工具描述**（谁提到了谁 + 双方 preload），
        #    发现同形的一共三处 —— 修掉的那处只是其中之一，而
        #    `look_at_screen → set_window_mode` **比它更老**：
        #    📌 **这不是拆分引入的新问题，是拆分让一个既有的坑更容易被踩到。**
        #    ⭐ 筛选判据：那句话是「你**现在就得**去调它」还是「以后可能用到它」——
        #       前者才是真风险（模型此刻要用、手上没有 → 它会编一个）。
        #       像 `os_execute → computer_use`（"load it when you need it"）
        #       就不算，那句话本身就是在叫它去加载。
        _BOUND_WITH = {
            # 「FIRST … This one is not optional」——GUI 操作前必须缩小自己
            "computer_use": ("set_window_mode",),
            # 「FIRST shrink yourself … BEFORE your first look_at_screen」
            "look_at_screen": ("set_window_mode",),
            # 成对：没有表就没有步骤可更新
            "update_task_step": ("create_task_list",),
        }
        _have = {m.get("name") for m in _matched}
        _extra_names = [n for src in _have for n in _BOUND_WITH.get(src, ())
                        if n not in _have]
        if _extra_names:
            _bound = [d.manifest for d in self._get_tool_catalog().search(
                "", _extra_names, scope=ToolScope.MAIN,
                runtime=self._tool_runtime_view())]
            if _bound:
                logger.info(f"[F4] 连带加载 {[m.get('name') for m in _bound]}"
                            f"（被 {sorted(_have & set(_BOUND_WITH))} 强绑定）")
                _matched = _matched + _bound
        if not hasattr(self, "_pending_loaded_manifests"):
            self._pending_loaded_manifests = []
        self._pending_loaded_manifests.extend(_matched)
        if _matched:
            return (
                # ⚠️ 旧文案是 "They can now be called directly."，**没有作用域限定**。
                # 两个方向都假：
                #   ① 不跨轮 —— 每轮开头都会重建 manifest（实测日志：某轮 10 个工具，
                #      下一轮就回到 6 个），但这句话会永久留在对话历史里；
                #   ② 不跨子作用域 —— 主循环加载的工具不进 Skill Explorer 的 manifest。
                # 而它作为普通 tool_result 会写进 MemoryManager，于是一句瞬时状态
                # 变成了模型可以永久引用的"事实"。Explorer 死路事故就是
                # 模型据此再次调用了当前作用域里根本不存在的 create_new_skill。
                #
                # 修在源头比事后清洗历史便宜得多：改这一句，所有作用域一起受益。
                # 见本文件里「运行作用域与事实生命周期」那段说明。
                "Loaded capabilities: " + ", ".join(m.get("name", "") for m in _matched)
                + ". This applies to the current step only. What you can actually call is "
                  "always defined by the tools attached to the current request — not by this "
                  "message. Do not treat this as evidence that these tools are available in a "
                  "later turn or inside a specialized sub-flow. Call the needed tool in the next step."
            )
        return (
            "No matching capability was found. Use names to specify exact tool names, "
            "or describe the task differently and try load_tools again."
        )

    async def _handle_set_next_checkin(self, args: dict, aid: str, *,
                                       event_queue, **_ctx) -> ToolOutcome:
        # ⭐ 模型看完之后自己决定下一次 —— 系统只设过第一次。
        _sec = args.get("seconds")
        try:
            _sec = int(_sec) if _sec is not None else None
        except (TypeError, ValueError):
            _sec = None
        if _sec is not None and _sec <= 0:
            _sec = None      # <=0 当成「不再回看」
        _rk_sid = getattr(self, "_recheck_sid", "") or ""
        if not _rk_sid:
            # ⚠️ 不在回看轮里调它 → **如实说没有对象**，不假装排上了。
            #    📌 同 `task_boundary`：宁可少说一句，不许多断言一件事。
            return ToolOutcome(
                "There is no background job being checked in right "
                "now, so there is nothing to schedule. Do NOT tell "
                "the user you scheduled anything.", True)
        from core.runtime import waitcond as _wc_sn
        _ok_rk = _wc_sn.reschedule_wait(_rk_sid, _sec)
        if _ok_rk:
            return ToolOutcome(
                f"Next check-in set: {_sec} seconds from now."
                if _sec is not None else
                "No further check-ins; the completion signal will wake you. "
                "There is still a long safety net in case it never returns.")
        # ⚠️ 排不上最常见的原因是那件事**刚好完成了** ——
        #    那时它已经终态，重排本来就不该成功。
        return ToolOutcome(
            "Could not set a check-in — that job may have "
            "just finished. Do not assume it is still running.", True)

    def _park_carrier(self, car: dict, next_step: str, why: str = "") -> dict:
        """把一个还在前台上的载体放转入后台。**`dont_wait` 与系统自动转入后台的唯一实现。**

        ⭐ 两个调用方：
           · 模型自己调 `dont_wait`（一句话交代两件事的场景）
           · **用户一插话，系统自动调**（2026-08-22 已定，见下）
           📌 一个已经存在的形状，第二次出现时该复用它 ——
              同一件事有两个实现，它们只在「我两次想法相同」的前提下一致。

        ⚠️ 撤回看复用 `reschedule_wait(None)`，**不另写一套**。
        ⚠️ 抽屉记录交给 UI 侧建：Task 记录要和 carrier 的**生命周期**绑在一起
           （完成时收），而持有 carrier 的是 app。
           📌 一条记录的收尾必须落在拥有它真实终态的地方（逐字同形）。
        """
        try:
            from core.runtime import waitcond as _wc_pk
            _wc_pk.reschedule_wait(car["wait_id"], None)
        except Exception as _e_pk:
            logger.warning(f"[B1] 转入后台时撤回看失败（照旧继续）: {_e_pk}")
        # ⚠️ **一次性** —— 用掉就清。📌 一个「当前是哪个」的记录点如果不清，
        #    下一次会作用在一个早就结束的载体上，而且不报错。
        self._detachable_carrier = None
        logger.info(f"[B1] {why or 'park'}：{car.get('display','')[:40]} 移出手头 "
                    f"→ 进抽屉，下一步「{next_step[:40]}」")
        # ⚠️ **只造事件，不投递** —— 两个调用方的送法本来就不同
        #    （工具走 `event_queue.put`，查询主流程是 async generator 走 `yield`）。
        #    📌 一个函数不该同时决定「做什么」和「怎么把结果送出去」。
        return {
            "event": "carrier_detached",
            "bg_ref": car.get("bg_ref", ""),
            "display": car.get("display", ""),
            "next_step": next_step,
        }

    async def _handle_dont_wait(self, args: dict, aid: str, *,
                                event_queue, **_ctx) -> ToolOutcome:
        """「这个调用我不等了」—— 把它从**手头**移出去。

        ⭐ 三件事，一件都不多：
          ① 撤掉回看（那条等待只剩完成信号）
          ② 让它**进抽屉 + 计入 pill**（它现在真的是一件独立在跑的东西了）
          ③ 告诉模型「完成时我会叫你」，并堵死「把回看当下一步」那条退路

        ⚠️ **不碰那个载体本身。** 「不等它」改的是 Nano 的注意力，
           不是那条命令的生死 —— 与早先的设计那条同源：
           📌 **搁置的是注意力，不是执行体。**
        """
        _next = (args.get("next_step") or "").strip()
        _car = getattr(self, "_detachable_carrier", None) or {}
        if not _car.get("wait_id"):
            # ⭐ **回看轮也是一个合法的时刻**：「看了一眼，还早得很，我不等了」。
            #    那一轮没有经过 `_hand_back_long_task`，所以指向要从回看对象来。
            #    📌 同 `set_next_checkin` 不要模型传 id 的理由：这一轮只有一个
            #       对象，让它传等于多一个可以填错的地方。
            _rk = getattr(self, "_recheck_sid", "") or ""
            if _rk:
                try:
                    from core.runtime.kernel import get_kernel as _gk_dw
                    from core.runtime import waitcond as _wc_dw0
                    _r0 = _wc_dw0.find_by_id(_gk_dw(), _rk)
                    if _r0 is not None and _r0.is_live:
                        _car = {"wait_id": _r0.wait_id,
                                "bg_ref": _r0.bg_ref or "",
                                "display": (_r0.reason or "").replace(
                                    "still running: ", "") or "that job",
                                "action_id": ""}
                except Exception as _e_rk:
                    logger.warning(f"[B1] dont_wait 读回看对象失败: {_e_rk}")
        # ⚠️⚠️ **指向还活着吗** —— 记录点存在不等于对象还在。
        #    🔴 回看轮不走 `_handle_query_impl`，所以 `_detachable_carrier` 有可能
        #       是上一轮留下的；那条等待此刻多半已经终态。
        #       不验的话：`reschedule_wait` 静默返回 False，而模型收到「办好了」
        #       → 它会去做 `next_step`，并且以为有人会叫它回来。
        #    📌 **一个「稍后使用」的指向，使用前必须问一次它指的东西还在不在** ——
        #       否则失败会以「成功」的形状返回。
        if _car.get("wait_id"):
            try:
                from core.runtime.kernel import get_kernel as _gk_lv
                from core.runtime import waitcond as _wc_lv
                _r_lv = _wc_lv.find_by_id(_gk_lv(), _car["wait_id"])
                if _r_lv is None or not _r_lv.is_live:
                    logger.info(f"[B1] dont_wait 的指向已终态（{_car['wait_id']}）—— 当作没有")
                    _car = {}
                    self._detachable_carrier = None
            except Exception as _e_lv:
                logger.warning(f"[B1] dont_wait 校验指向失败: {_e_lv}")
        if not _car.get("wait_id"):
            # ⚠️ 没有可交出去的载体 → **如实说没有**，不假装办成了。
            #    📌 同 `set_next_checkin` / `task_boundary` 那条 fail-safe：
            #       宁可少说一句，不许多断言一件事。
            return ToolOutcome(
                "There is no slow call handed back to you right now, so there is "
                "nothing to stop waiting for. Do NOT tell the user you moved "
                "anything to the background.", True)
        if not _next:
            # 🔴 **这条闸就是「模型滥用 dont_wait」的修法。**
            #    实测抓到过：模型逢长任务就调它，而它其实无事可做 ——
            #    于是「不等」变成了「干等，只是没人管了」。
            #    📌 **「不等它」只有在「我有别的事要做」时才成立** ——
            #       填不出 `next_step`，恰恰证明这次不该调。
            return ToolOutcome(
                "dont_wait needs `next_step`: the other work you are going to do "
                "while it runs. If your next move is to wait for that call or use "
                "its result, do not call this tool at all - just carry on.", True)
        await event_queue.put(self._park_carrier(_car, _next, why="dont_wait"))
        return ToolOutcome(
            f"\"{_car.get('display','that call')}\" is no longer something you are "
            f"waiting on. It keeps running in the background and now shows up in the "
            f"user's task drawer; the runtime will wake you when it finishes.\n"
            f"Go do this now: {_next}\n"
            f"Do NOT schedule or perform a check on it, and do not tell the user you "
            f"will keep an eye on it - you will be woken up. If you finish the work "
            f"above and its result still has not arrived, that is the moment to look "
            f"at it.")

    async def _handle_ask_user_choice(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        # 交互工具：暂停等用户选择。支持一次多张卡片（questions 数组）。
        _raw_qs = args.get("questions")
        if isinstance(_raw_qs, list) and _raw_qs:
            _q_specs = _raw_qs[:4]   # 最多 4 张
        else:
            _q_specs = [{
                "question": args.get("question", "Please choose"),
                "choices": args.get("choices", []),
                "allow_custom": args.get("allow_custom", True),
            }]
        _n = len(_q_specs)
        _UNANSWERED = object()
        _c1_results: list[Any] = [_UNANSWERED] * _n
        _c1_ev = asyncio.Event()
        _c1_loop = asyncio.get_running_loop()

        def _maybe_done():
            if all(r is not _UNANSWERED for r in _c1_results):
                _c1_loop.call_soon_threadsafe(_c1_ev.set)

        def _make_on_choice(i):
            def _h(selected):
                _c1_results[i] = selected
                _maybe_done()
            return _h

        def _make_on_dismiss(i):
            def _h():
                _c1_results[i] = "__dismissed__"
                _maybe_done()
            return _h

        _cards = []
        for _i, _spec in enumerate(_q_specs):
            _cards.append({
                "question": _spec.get("question", "Please choose"),
                "choices": _spec.get("choices", []),
                "allow_custom": _spec.get("allow_custom", True),
                "on_choice": _make_on_choice(_i),
                "on_dismiss": _make_on_dismiss(_i),
            })

        await event_queue.put({"event": "user_choice_request", "cards": _cards})
        from core.runtime import inbox as _ib6
        # ⭐ 选择卡也走双路：用户改口说话时不该继续干等五分钟。
        #    ⚠️ 这一处**不改判据** —— 未答的问题原本就按「用户跳过」处理，
        #       用户说话只是让它**提前**走到那个已有的分支。
        await _ib6.wait_confirm_or_user_message(_c1_ev, 300)

        def _fmt_one(spec, val):
            q = spec.get("question", "Choice")
            if val == "__dismissed__":
                return f"Question \"{q}\": the user dismissed the choice card. You may decide yourself or ask again."
            if val is None or val is _UNANSWERED:
                return f"Question \"{q}\": the user skipped. Choose one option yourself and explain your choice."
            return f"Question \"{q}\": the user chose \"{val}\"."

        if _n == 1:
            _v = _c1_results[0]
            if _v == "__dismissed__":
                return ("The user dismissed the choice card. You may decide how to "
                        "respond: choose an option and explain it, or ask again.")
            if _v is None or _v is _UNANSWERED:
                return ("The user clicked Skip. Choose one of the options yourself, "
                        "continue, and tell the user which one you chose.")
            return f"The user chose: {_v}"
        return "User choices for each card:\n" + "\n".join(
            _fmt_one(_q_specs[_i], _c1_results[_i]) for _i in range(_n)
        )

    async def _handle_set_window_mode(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        # 把窗口切到 mini / full。窗口缩放+布局 reflow 在 UI 层做。
        _wm_mode = (args.get("mode") or "").strip().lower()
        if _wm_mode not in ("mini", "full"):
            _wm_mode = "mini"
        # ⭐⭐⭐ [2026-08-25] **已经是 mini 了还调 mini —— 直接报错，不执行。**
        #
        # 🔴 问题：注入了 `[Window] … CURRENTLY MINI` 之后，Nano **还是**每轮重复调它。
        #    而这个动作**不是幂等的对用户而言**：缩窗前要过一次「临时 auto」授权，
        #    于是**用户被反复弹窗打扰**。
        # 📌 **注入的是事实，事实只能劝；这道闸才是判据。**
        #    今晚已经反复见到同一个形状：一条只靠模型自觉的规则，
        #    挡不住一个每次都觉得自己有理由的模型（`look_at_screen` 那条
        #    「not a safety ritual」写着也没用）。
        # ⭐ 而且报错**要说清现状**（「直接告诉 nano 目前就是在 mini 状态」）——
        #    📌 一个只说「不许」的错误，会让它换个说法再试一次；
        #       说清「已经是了」，它才知道该往下走。
        #
        # ⚠️⚠️ **闸只加在 mini 这一侧，`full` 永远放行。** 两边不对称：
        #      · 多缩一次 mini  → 弹窗打扰用户   ⇒ 该挡
        #      · 多复原一次 full → 什么都不打扰   ⇒ 挡它反而危险：
        #        万一租约状态和真实窗口漂移了，挡住 `full` 会把 Nano **锁死在 mini**。
        #    📌 闸只加在「多做一次会打扰用户」的那一侧；
        #       另一侧多做一次是无害的，而错误地挡住它是有害的。
        #
        # ⚠️ fail-safe 方向：**读不到状态就放行**（与 `_build_window_mode_injection`
        #    同向）。误挡的代价是「Nano 挡着屏幕却缩不了窗」，比多弹一次窗糟得多。
        if _wm_mode == "mini":
            try:
                from core.runtime import oslease as _ol_wg
                from core.runtime.kernel import get_kernel as _gk_wg
                _already_mini = bool(_ol_wg.gui_session_active(_gk_wg()))
            except Exception:
                _already_mini = False
            if _already_mini:
                logger.info("[F8] 已经是 mini 了，拦下这次 set_window_mode('mini')"
                            "（避免重复弹临时授权窗）")
                return ToolOutcome(
                    "Nano's window is ALREADY in mini mode - this call was rejected and "
                    "nothing happened. You do not need to shrink again; the window stays "
                    "mini for the rest of this task. Go straight to what you wanted to do.",
                    True,
                )

        if _wm_mode == "full":
            await event_queue.put({"event": "window_mode", "mode": "full"})
            return "Nano's window has been restored to full mode."
        # mini：缩窗【前】先过一次"临时 auto"授权（全局 auto 已开则 UI 端
        # 直接放行不弹窗）。授权在缩窗前弹 = 那时目标窗口没有瞬态 UI 可丢，
        # 点授权偷焦点也无害；授权后整段连续操作不再弹窗 → 焦点链不断。
        _ev = asyncio.Event()
        _approved = [False]
        _loop = asyncio.get_running_loop()

        def _on_approve():
            _approved[0] = True
            _loop.call_soon_threadsafe(_ev.set)

        def _on_reject():
            _approved[0] = False
            _loop.call_soon_threadsafe(_ev.set)

        await event_queue.put({
            "event": "mini_auth_request",
            "on_approve": _on_approve,
            "on_reject": _on_reject,
        })
        from core.runtime import inbox as _ib6
        # ⭐ 缩窗授权也走双路。⚠️ fail-safe 方向是**不授权** ——
        #    用户打字打断一个「要不要让我操作你的屏幕」，绝不能当成同意。
        if await _ib6.wait_confirm_or_user_message(_ev, 300) != _ib6.ConfirmOutcome.CONFIRMED:
            _approved[0] = False
        if _approved[0]:
            return (
                "Nano has been minimized to the top-right mini window and the user authorized automatic screen operation for this task. "
                "You may now continue operating the user's screen without repeated confirmation prompts. "
                "When finished, call set_window_mode('full') to restore the window; it will also auto-restore at turn end if you do not."
            )
        return (
            "The user rejected this screen-operation authorization or the request timed out. "
            "Do not operate the screen again. Tell the user that authorization is needed to continue, or suggest completing it manually."
        )

    def _file_read_truncation_note(self, action: str, os_result: dict,
                                   result_text: str) -> str:
        """`file_read` 撞上截断闸时，**告诉它还有另一条路**。

        ⭐ 先纠正一个曾经写错的前提：**模型是知道自己被截断的** ——
           `MemoryManager._compress_tool_results_inplace` 已经附了
           `[...truncated; original content was N chars]`。
           📌 所以缺的从来不是「有没有说截断」，而是**说了截断之后没给下一步**：
              它知道被切了、知道总长，却不知道 `file_read` 无法接着读，
              也不知道 `load_full_file` 能用 offset/limit 读同一个路径。
           （同 longcmd 那条判据：**截断可以接受，不说截断了不行** ——
             这里再进一格：说了截断，还得说得出接下来能干什么。）

        ⚠️ **判据只有一处**：阈值直接读 `MemoryManager.MAX_SINGLE_TOOL_RESULT_CHARS`，
           不在这里另写一个数。📌 两处各写一个数，一旦分叉就会出现
           「说没截其实截了」—— **那比不提示更坏**。取不到就宁可不提示。

        ⚠️ 判的是 `len(result_text)` 而**不是** `len(content)`：真正被量的是
           json 包装之后的那一份，中间隔着 `json.dumps`（换行会变成两个字符）。
           📌 量哪一个由「谁会被截」决定，不由「哪个更好拿」决定。

        ⚠️ 措辞里没有 must / should，末句明写 "your call" —— 见 `_OS_MANIFEST`
           里那段同源留痕：**模型拥有阅读主权**。
        """
        if action != "file_read" or not isinstance(os_result, dict):
            return ""
        if not os_result.get("ok"):
            return ""
        try:
            _limit = int(self.memory.MAX_SINGLE_TOOL_RESULT_CHARS)
        except Exception:
            return ""
        if _limit <= 0 or len(result_text) <= _limit:
            return ""
        _data = os_result.get("data") or {}
        _total = len(str(_data.get("content") or ""))
        _path = str(_data.get("path") or "")
        return (
            f"[Note] file_read returned {_total:,} characters, but only about "
            f"{_limit:,} of them reach you here - the tail is cut off, and file_read "
            f"cannot resume from where it stopped. If you need more of this file, "
            f"load_full_file reads the same path in slices (offset/limit)"
            + (f": {_path}" if _path else "")
            + ". Whether that is worth a turn is your call.\n"
        )

    async def _handle_os_execute_readonly(self, args: dict, aid: str, **_ctx):
        """Subagent作用域里的 `os_execute` —— **只读**。

        ⭐⭐ **为什么是一个独立 handler，而不是给 `_handle_os_execute` 加个标志**：
           目录的 `bindings` 本来就是「**不同作用域真的有不同的 handler**」——
           把只读做成 binding，"在Subagent里只能只读"这件事就由**目录解析**保证，
           而不是由"每个调用点记得传 `readonly_only=True`"保证。
           📌 **一个靠「记得传参」维持的安全边界，等于把它交给了下一个人的记性。**
           ⚠️ 而且默认值那一侧永远是危险的那侧：忘了传 = 全权限。

        🔴 双保险，两层各有理由：
           ① 这里先按 `dsl.is_readonly()` 挡一次 —— 为了给模型**一句有用的话**
              （告诉它这个执行者只能读），而不是让它撞一个通用错误。
           ② 底下 `OSDispatcher(readonly_only=True)` 再挡一次 —— 那才是**闸**。
           📌 上面那层是**说明**，下面那层是**执行** ——
              说明可以漏，执行不许漏，所以两层都要有。
        """
        from core.os_layer import dsl as _dsl
        _act = (args or {}).get("action") or ""
        if _act and not _dsl.is_readonly(_act):
            # 🔴🔴 **这句话原来是错的，而且它把Subagent逼进了死路**（实测 2026-08-20）。
            #
            # 原文：「`{_act}` **会改变这台电脑**」—— 对 `file_read` 来说那是**假话**：
            # 它一个字节都不改，它落在只读名单外是因为 `_ACTIONS` 表里
            # `readonly=False`（那张表把「只读」和「要不要授权」写在了同一格：
            # `file_read` 的 `floor=2`，读任意文件确实该确认）。
            # 于是Subagent收到一句它无法反驳、也无法绕开的话，就**放弃了整件事** ——
            # 而它手上明明有 `load_full_file`（v1.47 已确认绝对路径直接放行）。
            #
            # 📌 那条逐字适用：**给模型的失败信息必须同时【正确】且【充分】** ——
            #    这里两条全犯了：说了假因，也没给出口。
            # 📌 那条同源：**模型需要的是一个出口，不是一个名字。**
            #
            # ⚠️ **闸一个字没动**：判据仍然只有 `dsl.is_readonly()` 这一个出处，
            #    下面 `OSDispatcher(readonly_only=True)` 那道真闸照旧。
            #    改的只是**说明**那一层（handler 里这一层的职责本来就是"给一句有用的话"）。
            # ⚠️ 下面这张是**建议**表，不是安全名单 —— 📌 安全名单不许手抄，
            #    而它是从 `readonly_actions()` 派生的；这张表只回答「那你可以改用什么」，
            #    写错了最坏的后果是一句没用的建议，不会放行任何东西。
            _ALT = {
                "file_read": "`load_full_file`（读整份文件，绝对路径可以直接给）"
                             "或 `search_files`（只想在文件里找某段内容时用它）",
                "clipboard_read": "",
                "run_command": "",
            }
            _alt = _ALT.get(_act, "")
            return (f"[os_execute] 这个执行者（Subagent）只能调只读动作，`{_act}` 不在"
                    f"只读名单里。"
                    + (f"\n⭐ 你要做的这件事**有别的路**：改用 {_alt}。\n"
                       if _alt else
                       f"\n可用的只读动作：{', '.join(sorted(_dsl.readonly_actions()))}。\n")
                    + f"如果这件事**必须**动真实文件、或者确实没有替代品，"
                      f"就把你已经查到的东西如实报回给 main agent，由它来做 —— "
                      f"不要因此宣布任务无法完成。")
        return await self._handle_os_execute(
            args, aid,
            call=_ctx.get("call"), used_model=_ctx.get("used_model", ""),
            gui_waited=_ctx.get("gui_waited", 0.0),
            event_queue=_ctx.get("event_queue"),
            _readonly_only=True,
        )

    async def _handle_os_execute(self, args: dict, aid: str, *, call, used_model: str,
                                 gui_waited: float, event_queue, **_ctx):
        """`os_execute` —— 从分派链机械提取，**行为零变化**。

        ⚠️ 它是唯一会**提前返回 `ToolExecution`** 的 handler（两处）：
           ① 机器被别的持有者占着 ② 长命令被交还后台。
           那两处的语义是「这次调用已经自己完成了全部收尾」，所以调用方
           检测到 `ToolExecution` 就直接返回 —— 这正是 Flow Runner 的雏形：
           📌 **Catalog 决定"谁处理"，Runner 决定"怎么运行这一类 handler"。**
        """
        name = call.name
        _gui_waited = gui_waited
        result_text = ""
        _is_failed = False
        # ⚠️ 下面这一整段判据，cutover 之前长在 `_execute_one_tool_call`
        #    的 `elif name == "os_execute":` 分支里。那条分派链已被目录取代，
        #    而**判据讲的是这个 handler 内部的事**，所以随它搬到这里。
        #    📌 删一段代码时，长在它身上的「为什么」要跟着搬到新家 ——
        #       注释掉队比代码掉队更难发现（下面 finally 里那句「理由见上方那段」
        #       指的就是它，搬之前那句已经指向空气了）。
        # ⭐⭐ **租约覆盖一整段 GUI 操作，不是一个动作。**
        #
        # 已经持有就不再拿（幂等），并且**不在本次调用的 finally 里归还** ——
        # 归还点是每轮开头（见 `_handle_query_impl` 的每轮重置）。
        #
        # ⚠️ 这一条是推演验收预期时才发现的，值得写清楚：
        # `os_execute` 是**单步**工具，一次调用只做一个动作。原先每次调用
        # 都 acquire/release，于是 Nano 真正"持有机器"的窗口只有单个动作那一瞬，
        # 而**两步之间那段模型思考时间（好几秒）里没人持有**。
        # 传感器只在 Nano 持有时武装（判据 ③），所以用户的点击**大概率落在空窗里**，
        # 被动挂起等于形同虚设。
        #
        # 📌 建模错在哪：Nano 在一串 GUI 动作中间思考下一步点哪儿的时候，
        #    **鼠标仍然是它的**。每步都还锁等于宣称"我这会儿没在用电脑" ——
        #    那不是事实。**租约的粒度必须匹配「这台电脑归谁用」这件事本身的粒度，
        #    不是匹配代码的调用结构。**
        # ⭐⭐ **只有真正碰鼠标键盘的动作才需要租约。**（2026-08-07 实测修）
        #
        # 实测：让 Nano 用 GUI 打开一个 txt，它实际走了**命令行**，
        # 而这时 用户点桌面**照样**被判成"已让出控制"。原话：
        # 「覆盖的是 GUI 模拟，不是整个 OS 控制能力。
        #   命令行为什么要收到被动挂起传感器的影响」——**对**。
        #
        # 📌 判据：**被动挂起争的是「谁在用鼠标键盘」这一个资源，
        #    不是「谁在用这台电脑」。** 跑一条命令、读一个文件、截一张图
        #    都不与用户争鼠标 —— 用户点桌面不会让 `dir` 的结果失效。
        #    把它们也挡住，是把"互斥资源"的范围放大到了整个 OS 能力。
        #
        # ⚠️ 所以租约**按需**取：第一个鼠标键盘动作才 acquire，
        #    之后整段 GUI streak 一直持有（粒度理由见下）。
        #    纯命令行的一轮**一次都不 acquire**，也就完全不受被动挂起影响。

        try:
            from core.os_layer import dsl as _dsl_mk
            _needs_machine = _dsl_mk.contends_for_machine(
                (args or {}).get("action") or "")
        except Exception:
            # 判不出来就当需要 —— 宁可多要一次租约，也别让 GUI 动作漏过闸
            _needs_machine = True

        # ⚠️ acquire 不在这里做了 —— 统一挪进下面那段"等待"里，
        #    因为"要不要等"和"要不要拿"必须由**同一个判断**决定
        #    （分成两处写就是上一版那个 bug：句柄在手里 → 跳过等待）。

        # ⭐⭐ 被动挂起的**第一道闸**：机器在用户手里就别开工。
        # ⚠️ 只对需要鼠标键盘的动作生效（见上）。
        #
        # 拿不到租约不是故障，是"这台电脑现在归用户"。
        # 在这里挡比在 dispatch 里挡更好 —— **一步都还没做**，
        # 不会留下"做了一半"的现场让模型去猜环境变成什么样了。
        # ⚠️ 话术必须**说清是谁占着**，不能统一成一句"你先操作"：
        #    触发源不一定是用户（也可能是别的程序抢了前台，见 补记 ②），
        #    对一个没动过手的用户说"你先操作"比不说更糟。
        # ⭐⭐⭐ **拿不到就在这一轮里等，不是失败。**
        #
        # 这是本项目里最贵的一个形态错误。第一版做成了"闸"：
        # 拿不到 → 动作失败 → 模型只能"别重试" → **结束这一轮**。
        # 于是用户每发一次「继续」都是新的一轮，进来立刻撞墙、再结束 ——
        # 实测表现为"永久锁死"。
        # 📌 **「闸」和「挂起」在代码里长得像，行为相反：
        #    闸的出口是失败，挂起的出口是等待再继续。**
        #    一个只有失败出口的机制，最终一定把成本转嫁给用户去手动重试。
        #
        # ⚠️ 等待期间**零 LLM 调用、零 token** —— 只是 `asyncio.sleep`。
        #    所以"等 3 分钟"对预算免费，只占一个挂着的 turn。
        # ⚠️ 即时提示**不在这里发**：顶部那条状态条自己轮询租约（1 秒重画），
        #    用户一动手就出现，不依赖这里跑到哪儿。
        # ⚠️⚠️ **判据是「现在还有没有资格」，不是「手里有没有句柄」。**
        #
        # 第一版写的是 `self._rt_os_lease is None` —— 于是"跑到一半被用户抢走"
        # 这种**最常见**的情况整段被跳过：Nano 手里那个句柄还在，
        # 但它指向的租约早已 PREEMPTED。然后 dispatch 那道闸快速失败、
        # 模型结束这一轮 —— 实测 逐条坐实：
        #   20:45:07 用户接管 → 20:45:10 [OS-Dispatch] 让位，不执行 type_text
        #   **全程没有任何「进入等待」的日志。**
        #
        # 📌 **一个"我还持有"的判断，不能拿"我曾经拿到过"来回答。**
        #    与 同形：历史只能证明它曾经成立，不能证明它现在仍然成立。
        # ⚠️ **等待已经在函数最顶上统一做过了**（GUI 模式 → 任何工具前都等），
        #    所以这里只剩"确保手里有租约"。理由见顶部那段：
        #    📌 感知的范围和反应的范围必须一致，而统一在入口做才不会漏掉
        #       非键鼠的那些工具。
        _waited = _gui_waited
        if _needs_machine:
            try:
                from core.runtime import oslease as _ol_w
                from core.runtime.kernel import get_kernel as _gk_w
                # ⚠️ 判据是「现在还有没有资格」，不是「手里有没有句柄」——
                #    句柄可能指向一条已被抢占的租约。
                _may_w, _why_w = _ol_w.nano_may_touch_os(_gk_w())
                if not _may_w:
                    self._rt_os_lease = None
                elif self._rt_os_lease is None:
                    self._rt_os_lease = _rt_lease_acquire(self, "os_execute")
            except Exception as _e_w:
                logger.warning(f"[OSLease] 取租约异常: {_e_w}")
                self._rt_os_lease = None

        # 等到上限还是拿不到 —— 这时候才体面收场
        # ⏸ 调度器落地后，这里应改成「登记一个 continuation」而不是结束。
        # ⚠️⚠️ **2026-08-22 试过一版又拆了**（见 `runtime/scheduler.py` 的墓碑）：
        #    当时建了一类 `DEFERRED_ACTION` Task + 一个 blocker provider + 一个 tick。
        #    已定拆掉 —— **不是因为这个缺口不真**（下面那句
        #    "you will pick this up again when they are done" 至今没人兑现），
        #    而是因为**它不值得一套新机制**。
        #    ⭐ 真要做：用既有的 `set_next_checkin` 在这里排一次回看（几行），
        #       那正是 v1.52 对同类问题给过的答案（「变回手头的活不需要任何新机制」）。
        #    📌 别再造第二套 —— 那条路已经走过了。
        if _needs_machine and self._rt_os_lease is None:
            _who = "the user"
            try:
                from core.runtime import oslease as _ol_r
                from core.runtime.kernel import get_kernel as _gk_r
                _ok_r, _why_r = _ol_r.nano_may_touch_os(_gk_r())
                if not _ok_r:
                    _who = _why_r
            except Exception:
                pass
            logger.info(f"[OSLease] 等满 {_waited:.0f}s 仍拿不到，本轮收场: {_who}")
            # ⭐⭐⭐ 2026-08-22 落地 —— **先排一次回看，再说那句承诺。**
            #
            # 🔴 问题：下面那句 "you will pick this up again when they are done"
            #    是 Nano 对用户的一句**承诺**，而这条路**压根没登记过任何等待** ——
            #    于是没有任何东西会把它叫回来。
            #    v1.63 把【载体】那一半做通了（`_drain_inbox` 队列空 → 扫后台 →
            #    重排回看），但它扫的是 `bg_ref` 非空的记录 ——
            #    **租约这条没有载体，扫不到它。**
            # 📌 **一句模型已经在对用户说、而系统兑现不了的承诺，比没有这句话更坏。**
            #
            # ⭐ 不造新机制（墓碑里写死了）：2026-08-22 试过一套 `DEFERRED_ACTION`
            #    Task + blocker provider + tick，已定拆掉 ——
            #    不是因为缺口不真，而是**它不值得一套新机制**。
            #    这里用的就是 `wait_for` 那个**已经存在的形状**：`_rt_wait_open`
            #    + 纯定时唤醒。📌 一个已经存在的形状，第二次出现时该复用它。
            #
            # ⚠️ 间隔直接用 `_FIRST_RECHECK_SEC`，**不另发明一个数字** ——
            #    📌 它们答的是同一个问题（"过多久回头看一眼"），
            #       而三个宽限期各自演化成 45/90/? 那个问题刚修完。
            _lease_rec = None
            try:
                from core.runtime import waitcond as _wc_lease
                _lease_rec = _rt_wait_open(
                    reason=f"the computer is held by {_who}",
                    wake_on=[_wc_lease.WakeSource.TIMER],
                    timer_seconds=float(self._FIRST_RECHECK_SEC),
                    intent="condition_recheck")
            except Exception as _e_lw:
                logger.warning(f"[OSLease] 排回看失败（照旧收场）: {_e_lw}")
            _picked_up = bool(_lease_rec)
            result_text = (
                f"Waited {_waited:.0f}s but the computer is still held by someone else "
                f"({_who}). This is not an error and not something to diagnose.\n"
                "⚠️ Do NOT retry this action — retrying means grabbing the mouse back "
                "while they are still using it.\n"
                "Stop here. Tell the user briefly and naturally what you observed "
                "(only what the reason above actually says — do not assume it was the "
                "user if it does not say so)"
                + (", and that you will pick this up again when they are done — the "
                   f"runtime will bring you back in about {self._FIRST_RECHECK_SEC:.0f} "
                   "seconds to look again."
                   if _picked_up else
                   # ⚠️⚠️ 排不上就**不许说那句承诺** —— 📌 同 `wait_for` 登记失败
                   #    那处的纪律：宁可承认「不知道」，也不许替用户编一个
                   #    用户没做过的动作；一个写了但永远不生效的声明就是要修的问题。
                   ". ⚠️ Nothing is scheduled, so do NOT say you will check back or "
                   "pick this up later — say plainly that they can ask you again.")
                + " Then end the turn."
            )
            await event_queue.put({
                "event": "tool_end", "action_id": aid,
                "result_summary": result_text[:80], "status": "SYS_IDLE",
                "model": used_model, "current_skill": name, "ok": False,
            })
            # ⭐ 只在**真的排上了**时才给 UI 那个等待 pill。
            #    📌 UI 必须是权威状态的忠实投影：没有权威记录，就不该有 UI 投影。
            #    （`wait_for` 那处抗过这个坑：空 `suspension_id` 会画出一个
            #      永远转圈、按钮点了没用的 pill。）
            if _picked_up:
                await event_queue.put({
                    "event": "suspend_waiting",
                    "action_id": aid,
                    "suspension_id": _lease_rec.wait_id,
                    "reason": f"the computer is held by {_who}",
                    "wake_on": list(_lease_rec.wake_on),
                    "timer_at": _lease_rec.fire_at,
                    "timer_seconds": float(self._FIRST_RECHECK_SEC),
                    "waiting_intent": "condition_recheck",
                })
                logger.info(f"[OSLease] 已排一次回看（{_lease_rec.wait_id}，"
                            f"{self._FIRST_RECHECK_SEC:.0f}s 后）—— [B1] 租约那一半")
            return ToolExecution(call=call, result_text=result_text, ok=False,
                                 error="machine held by another holder")
        import json as _json_os
        from core.os_layer.dispatch import OSDispatcher
        from core.os_layer.safety import OSSessionSafety
        if not hasattr(self, "_os_safety"):
            self._os_safety = OSSessionSafety()
        _os_dispatcher = OSDispatcher(
            session_id=getattr(self, "_session_id", ""),
            m1_mode=False, m2_mode=True, m3_mode=True,
            # ⭐ 只读执行者（Subagent）由 `_handle_os_execute_readonly` 传进来。
            #    ⚠️ 这一层才是**闸** —— 上面那层只是为了给模型一句有用的话。
            readonly_only=bool(_ctx.get("_readonly_only")),
            safety=self._os_safety, provider=self.provider,
            vision_model_override=_vision_model_for_os(),
        )
        _os_result = None
        # ⚠️⚠️ 这个 `try/finally` **只包镜像，不碰 `_os_task_busy`**。
        #
        # 第一版没有它，镜像和旧 bool 挂在**完全相同**的位置 —— 结果实测
        # 测出来的是"两边一致"，而事实上那次泄漏真的发生了：
        # 按一下 Escape 之后 **3 分半**旧 bool 还是 True，只是镜像也一起漏，
        # 所以对答案永远 match。**一个照抄了 bug 的 shadow 测不出那个 bug。**
        #
        # 📌 判据：**shadow 要镜像的是「被建模的那个现实」，不是
        #    「旧实现对现实的记录」。**
        #    早先镜像挂起记录是对的 —— 那里记录本身就是现实；
        #    而这个 bool 是一个**关于现实的断言**（"OS 正在忙"），
        #    镜像必须跟着**真实的 OS 工作**走，两边才有可比性。
        #
        # ⚠️ 旧 bool 依然不动（观测期不切权威）—— 修它是切读那一步的事。
        # ⭐⭐ 动作前后各拍一次顶层窗口快照：**新冒出来的窗口就是这次动作开的。**
        #    这是"谁干的"这个问题在窗口层的答案 —— 键鼠层有 `LLKHF_INJECTED`
        #    可以问系统，窗口层没有，**夹在动作前后的差集**是最接近它的东西。
        #    绑定的有效期挂在活动租约上（见 `window_binding` 模块头），
        #    所以**不需要任何人记得重置**。
        try:
            from core.os_layer import window_binding as _wb
            _wb_before = _wb.snapshot()
        except Exception:
            _wb, _wb_before = None, None

        # ⭐⭐ [ActionAttempt] 开一条尝试。**状态推进到 IN_FLIGHT 不在这里，
        #    在 `dispatch` 真正调执行器之前那一行** —— 因为这中间还有
        #    定位、授权确认（可能等用户点很久），那段时间现实一点没变。
        _att_id = None
        try:
            from core.runtime import attempt as _att_m
            _att_id = _att_m.begin(
                action=(args or {}).get("action") or "",
                tool_name="os_execute",
                summary=self._get_tool_catalog().presentation("os_execute", args or {}),
                turn_id=getattr(self, "_rt_turn_id", None),
                detail={"params": (args or {}).get("params") or {}})
        except Exception:
            _att_id = None
        try:
            async for _os_ev in self._execute_dsl_step(args, _os_dispatcher, self._os_safety, used_model):
                if "_step_result" in _os_ev:
                    _os_result = _os_ev["_step_result"]
                else:
                    await self._ui_sink(event_queue).put(_os_ev)
        finally:
            if _wb is not None and _wb_before is not None:
                try:
                    _wb.bind_if_new(_wb_before,
                                    (getattr(self, "_rt_os_lease", None) or ("", 0))[0])
                except Exception:
                    pass
            # ⚠️⚠️ **这里【不】归还活动租约** —— 归还点在每轮开头。
            #    理由见上方那段：租约覆盖一整段 GUI 操作，不是一个动作；
            #    在这里还了，两步之间就没人持有，被动挂起形同虚设。
            #
            # ⚠️ 这里**曾经**有一行 `self._os_task_busy = False` 的止血，
            # 以及一大段"能不能用 finally"的辨析。切写那一步已把那个 bool 删掉，
            # 但那条辨析值得留：
            # 📌 **同样是「标志没复位」，能不能用 finally 取决于它被谁在什么时候读。**
            #    `_os_task_busy` 唯一用途是抑制 canary → 能用 finally；
            #    `_active_tool_batch_open` 的用途是"异常时告诉
            #    `_clean_damaged_memory` 可以回滚" → **刻意不能**用 finally
            #    （会在外层读到之前清零，等于把机制废掉）。
            pass
        if _os_result is None:
            _os_result = {"ok": False, "error": "step returned no result"}

        # ⭐⭐⭐ [统一长任务 2026-08-09] **长命令走的是和 MCP 完全同一条
        #    交还合同**（`_hand_back_long_task`）。
        #
        # 用户的原则：「我们说的是『耗时长的任务』，跟任务类型从来就没有
        # 关系过……用户需要区分任务吗，我们要识别的就是『长任务』。」
        # 📌 **一个机制如果只有一个接入点，那它可能不是机制，
        #    只是那一处的实现细节。** 这是它的第二个接入点。
        #
        # ⭐ 而这条路上回看**比 MCP 那条有价值得多**：命令的 stdout 里
        #    有 pip 的百分比、下载速度、报错 —— 回看那一眼真的看得见东西。
        _lr = (_os_result.get("data") or {}).get("long_running")
        if _lr and _os_result.get("ok"):
            _lc_ref = (_os_result.get("data") or {}).get("ref", "")
            _lc_disp = (_os_result.get("data") or {}).get("display", "command")
            result_text = await self._hand_back_long_task(
                display=_lc_disp, bg_ref=_lc_ref,
                action_id=aid, event_queue=event_queue)
            # 把「等它结束」这件事做成一个 awaitable 交给后台生产端。
            # ⚠️ 用 `to_thread` 包同步的 `join` —— 读线程已经在跑了，
            #    这里只是「等它」，不是「再跑一次」。
            async def _await_longcmd(_r=_lc_ref, _attempt_id=_att_id):
                import asyncio as _aio
                from core.os_layer import longcmd as _lc2
                _res = await _aio.to_thread(_lc2.join, _r)
                _lc2.forget(_r)
                _out = (_res.get("data") or {}).get("output", "")
                # ⭐ `join` 已经给出这条**同一个**命令的真实终态；
                # 现在才有资格收 ActionAttempt。交还那一刻收会把
                # 「还在跑」伪造成结论；由 UI 的完成文字反推又会把展示
                # 形状误当成事实。完成载体本身同时知道结果与 attempt id，
                # 所以收口必须在这里。
                #
                # ⚠️ 不因 `forget()` 失败而影响账本收尾：它只清观测缓冲，
                # 不是命令是否结束的依据。
                try:
                    from core.runtime import attempt as _att_m3
                    _att_m3.finish(
                        _attempt_id, bool(_res.get("ok")),
                        reason=str(_res.get("error") or "")[:200])
                except Exception:
                    pass
                if _res.get("ok"):
                    return f"command finished successfully:\n{_out[-1500:]}"
                return (f"command finished with a problem "
                        f"({_res.get('error','')}):\n{_out[-1500:]}")
            await event_queue.put({
                "event": "long_task_handback",
                "task": asyncio.ensure_future(_await_longcmd()),
                "bg_task_ref": _lc_ref,
                "display": _lc_disp,
            })
            # ⚠️⚠️ **刻意【不在这里】收尾这条 ActionAttempt。**
            #
            # 🔴 第一版在这里写了 `_att_m2.finish(_att_id, True, ...)`，
            #    被 `t_f1_stage5_attempt` 判红 —— 而它是对的：
            #    **那次尝试根本没有结束，它还在跑。**
            # 📌 **不许为一件还没结束的事记一个结论** ——
            #    而 `ActionAttempt` 的全部意义就是「恢复后能说清
            #    结果可不可信」，被交还的长命令**恰恰是「结果未知」那一格**。
            # ⭐ 所以留在 `IN_FLIGHT` 才是它的诚实状态：
            #    进程真的死了，启动收尾会把它标成中断（那也是真话）。
            # ⭐ 已兑现：attempt id 作为 `_await_longcmd` 的闭包参数
            #    随同一条载体走到 `join()` 的真实结果处才 finish。
            return ToolExecution(call=call, result_text=result_text, ok=True)

        # 用户中断（Ctrl+` 急停 / 甩角 failsafe / 确认弹窗点取消）：
        # 本次动作判失败并告诉模型别再试，但【不】置任何会话级标志——
        # 取消一个动作不等于"从此不许再操作电脑"（软急停已删除，见 safety.py 模块头）。
        _is_abort = (
            (_os_result.get("is_control_flow") and
             _os_result.get("data", {}).get("reason") == "USER_ABORT")
            or _os_result.get("aborted")
            or _os_result.get("error") in ("用户已取消", "user cancelled")
        )

        # ⭐⭐ [ActionAttempt] 收尾。**这里有一个关键分叉：**
        #
        # 如果这一步跑完之后**机器已经不归 Nano 了**（用户中途接管），
        # 那它是**被打断**的，不是"失败"——走 `INTERRUPT`，让内核按
        # commit boundary 判 `effect_state`（已 IN_FLIGHT → PARTIAL_OR_UNKNOWN）。
        #
        # 🔴 走 `FINISH(ok=False)` 会把它记成"失败"，而**"失败"暗示"没生效"** ——
        #    可现实里那半截字已经在记事本里了。**那是假事实。**
        # 📌 「中断了」和「失败了」在结果上长得像，在**语义上完全相反**：
        #    失败 = 可以放心重做；中断 = 结果不可信、重做可能重复副作用。
        try:
            from core.runtime import attempt as _att_m2
            from core.runtime import oslease as _ol_a
            from core.runtime.kernel import get_kernel as _gk_a
            _still_mine, _ = _ol_a.nano_may_touch_os(_gk_a())
            if _is_abort or not _still_mine:
                _att_m2.interrupt(_att_id, "用户接管了这台电脑"
                                  if not _still_mine else "用户中断（急停/取消）")
            else:
                _att_m2.finish(_att_id, bool(_os_result.get("ok")),
                               reason=str(_os_result.get("error") or "")[:200])
        except Exception:
            pass

        if _is_abort:
            _is_failed = True
            result_text = ("The user interrupted this screen action. Do not retry it. "
                           "Respond in text and let the user decide what to do next.")
        else:
            _is_failed = not _os_result.get("ok", False)
            result_text = _json_os.dumps(_os_result, ensure_ascii=False, default=str)
            # ⭐ 见 `_file_read_truncation_note` 的完整推导。
            # ⚠️ **必须前置**：截断切的是尾巴（`content[:limit]`），
            #    附在后面的话，正好在需要它的时候被切掉。
            #    📌 形状同下面的 `_win_note` —— 那条也是前置，同一个理由。
            _fr_note = self._file_read_truncation_note(
                (args or {}).get("action") or "", _os_result, result_text)
            if _fr_note:
                result_text = _fr_note + result_text
            # ⭐ 窗口易主也要在**动作结果**里说，不能只在 look_at_screen 里说 ——
            #    模型完全可以不看屏幕就连着动手，那条路上它同样需要知道对象换了。
            #    （2026-08-07 那次数据损坏正是"看了一眼 → 认错对象 → 直接动手"。）
            _win_note = self._window_identity_note()
            # ⚠️ 门槛从"前台变了"放宽到"目标窗口有任何异常" —— 后者才是决定性的。
            #    只看"前台变了"会漏掉最阴的一种：目标窗口被**最小化**了，
            #    前台没变（还是它自己所在的那个进程/桌面），但它已经不在屏幕上。
            if any(k in _win_note for k in ("CHANGED", "NOT in the foreground",
                                            "MINIMIZED", "is GONE", "MOVED")):
                result_text = _win_note + "\n" + result_text
            # ⭐⭐ 等过就必须说 —— **恢复后不许假装什么都没发生。**
            #    ⚠️ 这是 三层防护网第 1 层的兑现："挂起前后的环境不能默认一致"。
            #    ⭐ 完整版的两半后来都落地了：`ActionAttempt`（说清"上一个动作做到哪了、
            #       结果可不可信"，见下一段）与接管日志（"用户这段时间干了什么"，
            #       `core/proactive/takeover_log.py`）。这里保留的是最小版本：
            #       至少让它知道自己被打断过、等了多久、别拿旧的屏幕认知往下走。
            if _waited >= 1.0:
                # ⭐⭐ [ActionAttempt] 恢复提示不再只说"环境可能变了"（笼统），
                #    而是说清**上一个动作是什么、结果可不可信**。
                #    这正是指出的那个缺口：
                #    「恢复后该告诉模型什么」以前答不上来。
                _att_note = ""
                try:
                    from core.runtime import attempt as _att_m3
                    from core.runtime.kernel import get_kernel as _gk_n
                    _att_note = _att_m3.describe_for_model(
                        _att_m3.last_terminal(
                            _gk_n(), getattr(self, "_rt_turn_id", None)))
                except Exception:
                    _att_note = ""
                # ⚠️⚠️ **这段文案原来最后一句是「Verify the current state
                #    before your next action.」——第 2 层落地时必须改掉。**
                #    那句话把"核实"写成了**无条件义务**，等于绕回早先的设计
                #    纠正过的老路：「用户动过屏幕就必须截图一次」。
                #    📌 判据：**第 2 层是主路径、日志必读；
                #       第 3 层是条件兜底 —— 日志能完成行为还原就不需要截图。**
                #    📌 而真正的判据不是「环境变了没有」，是
                #       **「这个变化影不影响我下一步」** —— 用户往 Nano
                #       正要清空的那个记事本里打了个字，环境确实变了，
                #       但结论是什么都不用做。按"变了就核实"做，这里白烧一次截图。
                result_text = (
                    f"[Resumed after {_waited:.0f}s] The user took control of the "
                    f"computer and you waited for them. The screen may have changed "
                    f"while you were paused — do NOT rely on what you saw before the "
                    f"pause. Read the pause log above and decide whether the change "
                    f"actually affects your next step; if it clearly does not, just "
                    f"continue.\n"
                    + (_att_note + "\n" if _att_note else "")
                ) + result_text
        self._last_called_skill = "os_execute"
        return ToolOutcome(result_text, _is_failed)

    async def _handle_wait_for(self, args: dict, aid: str, *,
                               event_queue, **_ctx) -> ToolOutcome:
        # 挂起/等待：登记一条挂起记录 + 触发等待 UI。
        # 不在这里阻塞——挂起的本质是"结束 turn 等触发"，所以这里只做
        # 登记和发事件，随后模型会用一句话告诉用户在等什么并结束本轮。
        _reason = (args.get("reason") or "").strip() or "external state change"
        # ⭐ 这个工具现在**只剩定时**一种，理由见 `_WAIT_FOR_MANIFEST`。
        #
        # ⚠️ 旧代码：`_wake_on = args.get("wake_on") or ["user"]` ——
        #    schema 的默认值和这里的兜底**双双指向 `user`**，
        #    模型只要不显式写 wake_on 就会拿到一条永远等不到东西的挂起。
        #
        # ⚠️ 老模型可能还会按旧 schema 传 `wake_on` / `bg_task_ref`（缓存里的旧描述、
        #    或历史对话里的旧例子）。**不静默忽略**：明确告诉它这些参数没了，
        #    否则它会以为自己挂了个"等用户"的等待、实际拿到的是定时。
        _legacy = [k for k in ("wake_on", "bg_task_ref") if args.get(k)]
        from core.runtime import waitcond as _wc_wait_for
        _wake_on = [_wc_wait_for.WakeSource.TIMER]
        _bg_ref = None
        # 计时器的数据形状不足以说明用户能不能操纵它：系统自己的回看同样
        # 有 timer。这里是模型理解用户委托后的判断，必须显式写出。
        _plan_intent = args.get("intent")
        _waiting_intent = ("scheduled_timer"
                           if _plan_intent == "scheduled_plan"
                           else "condition_recheck")

        _timer_seconds = args.get("timer_seconds")
        try:
            _timer_seconds = int(_timer_seconds) if _timer_seconds is not None else None
        except (TypeError, ValueError):
            _timer_seconds = None
        if not _timer_seconds or _timer_seconds <= 0:
            # 没有时间的定时等待 = 天生等不到的记录。直接退回，别让它进库。
            return ToolOutcome(
                "wait_for needs timer_seconds (how many seconds until Nano should check "
                "again). It no longer supports waiting on the user or on a background job: "
                "if you need the user to do something, just say so and end your turn — "
                "their next message resumes you. If a background job is running, the "
                "runtime resumes you when it finishes.", True)

        # ⭐⭐⭐ 写点搬到内核 —— **旧库从此不再被写**。
        #    切读那一步已经把 13 个读点 + resolve/cancel 切过来了，只剩这个
        #    `add()` 还写旧库，于是那段时间是**双写**（旧库 + 镜像），
        #    而镜像是 best-effort，所以要靠每次读 `_realign` 兜。
        #    📌 **一个「兜底对齐」机制的正确结局不是被加固，
        #       而是被它兜的那个风险消失。**
        # ⚠️ `session_id` 不再传：WaitCondition 没有这个概念。
        #    📌 迁移时**不许把旧模型里已经没有意义的字段一起搬过来**。
        _rec = _rt_wait_open(
            reason=_reason, wake_on=_wake_on,
            timer_seconds=_timer_seconds, bg_ref=_bg_ref,
            intent=_plan_intent)
        if _rec is None:
            # ⭐⭐ **登记失败 → 立刻如实返回，不发 UI 事件。**（2026-08-12 修）
            #
            # 🔴 旧行为有**两个**后果，而且都是"假的"：
            #   ① 这句诚实的话写了，但**下面的 `result_text` 无条件覆盖了它** ——
            #      模型实际收到的是 "Scheduled a re-check in N seconds…"，
            #      **那是一句假陈述**（什么都没登记）。
            #      📌 与 要修的问题同族：**一个写了但永远不生效的声明。**
            #   ② 更糟的一半（修的时候才查出来）：它**仍然会发 `suspend_waiting` 事件**，
            #      而 `suspension_id` 是空串。UI 侧两条路不对称 ——
            #      `_register_hidden_waiting` 有 `if not suspension_id: return` 保护，
            #      但 **`_make_pill_waiting` 没有**：于是 `scheduled_timer`（用户委托的
            #      定时计划）会画出一个「⏸ 等待中」pill + 倒计时 + [立即执行][取消计划]，
            #      而它注册在**空 key** 上 → 收尾时用真 id 永远找不到它 →
            #      **一个永远转圈的 pill，还带着两颗点了没用的按钮。**
            #
            # ⚠️ 它同时违反两条硬判据：
            #   · **宁可承认"不知道"，也不许替用户编一个用户没做过的动作**（模型侧）
            #   · **UI 必须是权威状态的忠实投影**（用户侧）——
            #     没有权威记录，就不该有对应的 UI 投影。
            # ⭐ 所以修法是「立刻返回」而不是「补一个空 id 判断」：
            #   📌 **不产生那个事件，比让下游各自记得防它更可靠**（下游有两条路，
            #      而它们的保护本来就不一致 —— 那正是这个 bug 能活下来的原因）。
            return ToolOutcome(
                "Could not register that wait, so nothing is pending. "
                "Tell the user plainly instead of implying you will check back.", True)
        _failed = False
        # 发等待事件给 UI（pill 位置渲染成"⏸ 等待中 · 计时器 · 等什么"）
        await event_queue.put({
            "event": "suspend_waiting",
            "action_id": aid,
            # UI 协议字段名本轮不迁移；值已经是唯一的 wait_id。
            "suspension_id": _rec.wait_id if _rec else "",
            "reason": _reason,
            "wake_on": list(_rec.wake_on) if _rec else [],
            "timer_at": _rec.fire_at if _rec else None,
            "timer_seconds": _timer_seconds,
            "waiting_intent": _waiting_intent,
        })
        # ⭐ 只剩定时一种，不再需要那张"源 → 描述"的映射表。
        return ToolOutcome(
            (f"Scheduled the user's plan for {_timer_seconds} seconds from now: \"{_reason}\".\n"
             if _waiting_intent == "scheduled_timer" else
             f"Scheduled a re-check in {_timer_seconds} seconds for: \"{_reason}\".\n")
            + (f"⚠️ You passed {_legacy}, which no longer exist. wait_for is now "
               "timer-only. If you were trying to wait on the user, just say what you "
               "need and end the turn — their next message resumes you. If you were "
               "waiting on a background job, the runtime resumes you when it finishes.\n"
               if _legacy else "")
            + ("Now tell the user briefly and naturally that their plan is scheduled. "
               if _waiting_intent == "scheduled_timer" else
               "Now tell the user briefly and naturally what you are waiting for and "
               "when you will look again, such as \"I'll check again in about 5 minutes\". ")
            + "Then end this turn and do not call more tools.\n"
            + ("⚠️ When you wake up, the time being up does NOT mean the thing you were "
               "waiting for has happened. Check first, then decide."
               if _waiting_intent == "condition_recheck" else
               "⚠️ When you wake up, the requested time has arrived: perform the reminder "
               "or action now."),
            _failed)

    async def _handle_task_boundary(self, args: dict, aid: str, *,
                                    event_queue, **_ctx) -> ToolOutcome:
        # ⭐⭐ 「一件事」的边界 —— **只有模型能划**。
        #    三条各自独立的结论都要求 Task 跨多个 turn（迭代阅读的 scratchpad /
        #    仅本次 MCP / per-task 成本），而它们对「什么时候结束」的要求
        #    是同一句：**那个目标达成或放弃** —— 代码答不了。
        from core.runtime import task as _tkb
        _act = (args.get("action") or "").strip()
        if _act == "finish":
            _oc = (args.get("outcome") or "completed").strip()
            # ⚠️ 「做完了」和「放弃了」**必须分开** —— 早先已定
            #    「用户主动停掉不是失败」，归错会让人去排查一个不存在的问题。
            #    这里同源：**历史要读得出真相**。
            if _oc not in ("completed", "abandoned"):
                _oc = "completed"
            _done = _tkb.finish_conversation_task(_oc, args.get("note") or "")
            if _done:
                return ToolOutcome(
                    f"Marked that piece of work as {_oc}. "
                    f"⚠️ Anything you started under it (background jobs, timed "
                    f"waits) is NOT cancelled by this — cancel those separately "
                    f"if the user wants them stopped.")
            # ⚠️ 没有在进行的事 → **如实说没有**，不假装收了一件。
            #    📌 这正是那条 fail-safe 的兑现：宁可少说一句，
            #       不许多断言一件事（否则模型会向用户宣布一个假的完成）。
            return ToolOutcome(
                "There was no piece of work in progress, so nothing was "
                "closed. Do NOT tell the user you finished something.", True)
        if _act == "start":
            _goal = (args.get("goal") or "").strip()
            if not _goal:
                return ToolOutcome(
                    "task_boundary(start) needs `goal` — one short "
                    "line saying what this new piece of work is for. "
                    "Without it the record is useless later.", True)
            # ⚠️ **默认不结束旧的** —— 早先的设计：「并不要求 x 和 y 一定相关」
            #    ⭐ 2026-08-16 起旧那件事会被**显式搁置**（而不是留在前台），
            #      所以这里的回话也跟着改：要告诉它**旧的去哪了、怎么回去**。
            #      📌 一个工具的回话是模型对世界的唯一观测——
            #         它描述的行为变了，回话不改就是一句假话。
            _prev_before = None
            try:
                from core.runtime.kernel import get_kernel as _gk_tb
                _prev_before = _tkb.current_conversation_task(_gk_tb())
            except Exception:
                pass
            _new = _tkb.start_new_conversation_task(_goal)
            if not _new:
                return ToolOutcome(
                    "Could not start a new piece of work; carry on without it.")
            return ToolOutcome(
                f"Started a new piece of work: {_goal}."
                + (f" The previous one ({_prev_before}) is now PARKED - it is not "
                   f"finished, and anything it started is still running. "
                   f"Use action='resume' with that id to go back to it, or "
                   f"action='finish' after resuming if it is actually done."
                   if _prev_before else ""))
        # ⚠️⚠️ `park` **已退役（2026-08-20，Task 收窄）** —— 见 `task.py` 那段留痕。
        #    模型若照旧写 `park`，撞在这条如实的拒绝上，而不是被静默当成别的动作。
        #    📌 **一个被删掉的枚举值，必须让调用它的人撞到墙** ——
        #       静默忽略会让模型以为自己搁置成功了，然后向用户宣布一件没发生的事。
        if _act == "park":
            return ToolOutcome(
                "There is no 'park' action any more. The current piece of work is "
                "set aside automatically when you start a different one "
                "(action='start'). If you are simply not working on it right now, "
                "do nothing. To stop waiting for a slow CALL, use `dont_wait`.", True)
        if _act == "resume":
            _ok, _msg = _tkb.resume_conversation_task(args.get("task_id") or "")
            return ToolOutcome(_msg, not _ok)
        return ToolOutcome(
            "task_boundary needs action='finish', 'start' or 'resume'. "
            "If you are simply continuing, do not call this tool "
            "at all — continuing is the default.", True)

    async def _handle_stop_background(self, args: dict, aid: str, *,
                                      event_queue=None, **_kw):
        """真的停掉一件正在跑的东西。**不是「不再等它」，是「让它别跑了」。**

        ⚠️ 与另外两个刻意分开 —— 三个不同的意思，不许合并：
            · `dont_wait`   —— 别等了，但**让它继续跑**（转入后台）
            · `cancel_wait` —— 别回看了（等待记录收掉，载体照旧）
            · 本工具        —— **让它停下**
          📌 「答的不是同一个问题，就不合并」。

        🔴 它补的是一个把回看变成空谈的缺口：在此之前模型看到「这个办法坏了」，
           它能做的只有**不再看它** —— 而那件事还在跑。于是它换个新办法，
           **两个进程同时在跑**，而用户以为只有一个。

        ⚠️ **停不掉的要如实说，不许假装成功**：
           · 命令 → 能停（杀整棵进程树）
           · MCP  → 停不了（server 在对端）
           · Skill→ 停不了（`importlib` 进程内执行，Python 没有安全中断手段）
           📌 给模型一个错误的成功信号，比没有这个工具更糟 ——
              它会以为清干净了，然后在一个还在跑的东西上面盖新东西。
        """
        _ref = str(args.get("ref") or "").strip()
        if not _ref:
            return ToolOutcome(
                "stop_background needs `ref` - the id of the thing you want stopped. "
                "It is shown to you when a slow call is handed back to you.", False)
        # 命令这一类：真能停
        if _ref.startswith("cmd_"):
            try:
                from core.os_layer import longcmd as _lc_s
                if _lc_s.stop(_ref, "stopped by the model"):
                    return ToolOutcome(
                        f"Stopped {_ref} (the process and everything it started). "
                        f"It did not finish, so treat its result as unknown - not as failure.",
                        True)
                # ⚠️ 找不到不是错误：它多半刚跑完。
                #    📌 「已经结束了」和「停止失败」对模型的下一步完全不同。
                return ToolOutcome(
                    f"{_ref} was already finished - nothing to stop. "
                    f"Its result should be arriving on its own.", True)
            except Exception as e:
                return ToolOutcome(f"Could not stop {_ref}: {e}", False)
        # 其余：如实说停不掉
        return ToolOutcome(
            f"{_ref} is not something that can be stopped from here. External tool "
            f"calls run on the other side, and local skills run inside this process "
            f"with no safe way to interrupt them. It will keep running until it "
            f"finishes on its own. You can stop waiting for it, but do NOT tell the "
            f"user it has been stopped.", False)

    async def _handle_cancel_wait(self, args: dict, aid: str, *,
                                  event_queue, **_ctx) -> ToolOutcome:
        # 让"别等了"这句话真的能生效。理由见 manifest。
        _match = (args.get("match") or "").strip()
        try:
            from core.runtime.kernel import get_kernel as _get_wait_kernel
            from core.runtime import waitcond as _wc_cancel
            _pending = _wc_cancel.list_live(_get_wait_kernel(), oldest_first=True)
        except Exception as _ce:
            _pending = []
            logger.warning(f"[Suspension] cancel_wait 读取失败: {_ce}")

        if _match:
            # 宽松匹配：模型转述 reason 时常常不是逐字。
            # ⚠️ 匹配不上时**不要静默取消全部** —— 那是把"没听懂"变成"多杀一个"。
            _hit = [r for r in _pending
                    if _match in r.reason or r.reason in _match]
        else:
            _hit = list(_pending)

        if not _pending:
            return ToolOutcome(
                "There is nothing pending right now, so there was nothing to "
                "cancel. Tell the user plainly instead of implying you stopped "
                "something.")
        if not _hit:
            _names = "; ".join((r.reason or "?") for r in _pending)
            return ToolOutcome(
                f"No pending wait matched {_match!r}, so nothing was cancelled. "
                f"Currently pending: {_names}. "
                f"Ask the user which one they meant, or call again with the exact text.", True)
        for _r in _hit:
            _sid = _r.wait_id
            _wc_cancel.cancel_wait(_sid, resolved_by="model-cancel")
            # ⚠️ 这里原本还会关闭一次观测期的镜像 ——
            #    它关的是**镜像**，而镜像已经就是权威（上一行的
            #    resolve/cancel 直接走内核）。留着是**对同一件事关两次**，
            #    而且它依赖一个**重启就没的内存映射**。
            #    📌 双写拆掉之后，配对的「双关」也必须一起拆 ——
            #       留一半会让人以为还有另一套账。
        _done = "; ".join((r.reason or "?") for r in _hit)
        logger.info(f"[Suspension] 模型取消 {len(_hit)} 条等待：{_done}")
        return ToolOutcome(
            f"Cancelled {len(_hit)} pending wait(s): {_done}. "
            f"Nano will not check on those again. Confirm this to the user.")

    # ══════════════════════════════════════════════════════════════════════
    # exit-flow 薄适配器
    #
    # ⭐ 它们存在的唯一理由：**让 EXIT runner 里不再有一张按工具名分派的表。**
    #    改造前那里是 `if exit_call.name == "update_existing_skill": … elif …`，
    #    而那正是这次要删的第二份权威（那条 AST 断言要管的就是它）。
    #
    # 📌 分层没变，仍然是那条：**Catalog 决定"谁处理"，Runner 决定
    #    "怎么运行这一类 handler"。** 所以这五个适配器里**只有各自那一支
    #    原本就在做的事**（构造 `AgentDecision` / 补 requirement / supersede 澄清 /
    #    排空队列），而公共 plumbing —— 同轮拒绝、`tool_start`·`tool_end`、
    #    终端事件前收尾、`exit_flow_defer_to_model` 拦截 —— **一行都没搬**，
    #    仍然长在 runner 里。
    #
    # ⚠️ 统一签名 `(exit_call, decision, *, used_model, base_guide,
    #    realtime_callback, event_queue) -> AsyncIterator[dict]`。
    #    参数**取全集**：每个适配器只用它自己需要的那几个，用不上的不用管 ——
    #    这比让 runner 去猜"这个 handler 要哪几个参数"可靠得多
    #    （猜的那条路只有两种实现：按名字特判，或者反射签名，
    #     前者是我们正在删的东西，后者会把"参数写错"从启动期错误变成运行期错误）。
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _drain_event_queue(event_queue) -> list:
        """把队列里攒着的事件取干净，交给调用处 yield 出去。

        ⭐ 为什么 exit 路径需要它：`event_queue` 的常规排空循环长在**工具批次
        那一段**里，exit 工具在到达那段之前就路由走了。于是这条路径上**任何**
        `event_queue.put` 都是只进不出 —— 不只是工具卡片，`realtime_callback`
        发的 `thinking` 状态事件也一样卡在里面（表现为探索阶段状态行不更新）。

        非阻塞：只取已经在队列里的，不等新的，绝不拖慢路由。
        """
        out = []
        while not event_queue.empty():
            try:
                out.append(event_queue.get_nowait())
            except Exception:
                break
        return out

    def _exit_decision_from(self, exit_call, decision) -> "AgentDecision":
        """把 exit 的 `ToolCall` 还原成 `AgentDecision`（三个适配器都要）。"""
        return AgentDecision(
            "call", name=exit_call.name, args=exit_call.args,
            tool_use_id=exit_call.tool_use_id,
            thinking_blocks=decision.thinking_blocks,
        )

    async def _exit_update_existing_skill(self, exit_call, decision, *, used_model,
                                          base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_update_existing_skill_decision(
            self._exit_decision_from(exit_call, decision),
            self.memory.storage[-1].content if self.memory.storage else "",
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _exit_manage_existing_skill(self, exit_call, decision, *, used_model,
                                          base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_manage_existing_skill_decision(
            self._exit_decision_from(exit_call, decision),
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _exit_connect_mcp(self, exit_call, decision, *, used_model,
                                base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_connect_mcp_decision(
            self._exit_decision_from(exit_call, decision),
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _exit_manage_mcp(self, exit_call, decision, *, used_model,
                               base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_manage_mcp_decision(
            self._exit_decision_from(exit_call, decision),
            used_model, base_guide, realtime_callback
        ):
            yield _ev

    async def _exit_create_new_skill(self, exit_call, decision, *, used_model,
                                     base_guide, realtime_callback, event_queue):
        """创建 Skill 的入口。**2026-08-13：探索子循环已拆，这里直接进代码生成。**

        ═══ 为什么拆掉探索子循环（结论摘要，完整记录）═══

        探索是一个 772+310 行的**第二套 agent runtime**：独立提示词、独立只读工具集、
        独立的单工具 wrapper。它诞生于约 56 天前，当时 Nano 接的还是 Gemini、
        主体是硬路由、**连 ReAct 循环都没有**，文档记载它的目的是
        「让弱模型也能完成复杂任务」。那个前提今天已经不成立：

        · 实测 CMD64/65/66 连续三次证明，**模型在主循环里就把需求问清楚了**，
          压根没进探索（而且问得比设计的场景更细）。
        · 唯一真正进了探索的 CMD67，探索**一个只读工具都没调**就收尾了 ——
          说明隔离作用域本身并没有构成可靠的验证边界。

        📌 **一个为了「规范弱模型」而引入的结构，它的副作用会活得比那个弱模型久。**
           本项目已经为此拆过两次（Skill 编排、硬路由前置分类器），这是第三次。

        ═══ 但探索有一样东西不可替代，必须搬走而不是删掉 ═══

        🔴 **SkillWriter 是另一次模型调用、另一套 system guide，它看不见这里的一切。**
        回代码核实：`_generate_skill_with_writer(query)` → `_generate_skill_spec(query)`
        → `provider.generate_skill_spec(query)`，最后那层**重新拼了一个全新 prompt**
        （`The user wants to create a new local Python Skill: "{query}"`）——
        主循环的对话历史与工具结果**一个字都不在里面**。

        旧世界里，是 Explorer 把 `original_query + [Exploration Conclusion] + 结论`
        拼成 `enriched_query` 交过去的。所以照字面把 Explorer 删掉、直接传 requirement，
        **主循环刚查对的字段名、阈值、规则差异会全部断在这个调用边界前面**。

        ⭐ 于是把那份交接搬到入口：`handoff_summary` 成为必填参数。
        📌 **把结论放进参数，比靠上下文传递可靠** —— 这不是新发明，
           `conclude_exploration` 当年正是从「末行裸 JSON」改成工具参数的，同一条道理。
        ⚠️ 但要说准它保证了什么：schema 只能强制「必须填」，
           **强制不了「填的是真的」**。它解决的是**信息隔离**，不是行为约束。

        ═══ ⚠️ 刻意【不】搬过来的：decision / target_skill ═══

        旧 `conclude_exploration` 有这两个参数，但**它们的前提随作用域一起消失了**：
        Explorer 是封闭作用域，里面没有 `update_existing_skill` 出口，
        所以它必须自己把「探索发现应该更新」带出去。主循环没有这个问题 ——
        **选哪个工具本身就是那个 decision**：

            能直接复用 → 调那个 Skill        应该改 → update_existing_skill
            确实要新建 → create_new_skill

        🔴 而且照搬会**重新造出第二条 Skill 更新通路**：
        `_handle_update_existing_skill_decision` 承担着目标兜底解析 / 存在性校验 /
        规则文件预读 / 用户规则 vs 文件规则冲突检查 / 冲突时直接问用户 /
        无冲突才开修改确认 —— 共 151 行。`create_new_skill(decision="update")`
        会把这一整套全部绕过。
        📌 **一个补丁的参数，不要跟着它保护的东西一起搬家 —— 先问那个补丁的前提还在不在。**
        """
        _args = exit_call.args or {}
        _req = (_args.get("requirement") or "").strip()
        if not _req:
            # 兜底：模型没填 requirement 时用最近一条用户消息
            _req = next(
                (m.content for m in reversed(self.memory.storage)
                 if getattr(m, "role", "") == "user"
                 and isinstance(getattr(m, "content", None), str)
                 and m.content.strip()),
                "",
            )

        # ── open_questions 非空 → 不许进代码生成 ──────────────────────────
        #
        # ⭐ 这条行为原本长在 `conclude_exploration` 上，是实测 挣来的：
        #    模型**一边在正文里问"你要哪个？"、一边调了那个工具**，
        #    于是澄清被绕过、直接开始写代码。做成必填参数之后，
        #    「还有没有问题」变成一个它必须显式回答的事实，而不是我们去猜措辞。
        # ⚠️ 这里同样**不做措辞启发式**（例如"正文以问号结尾就算提问"）——
        #    那种判断会误伤"这样可以吗？我先按这个做"这类确认式表达。只认它自己的声明。
        _open_qs = _args.get("open_questions")
        if isinstance(_open_qs, str):
            # 模型偶尔会把数组写成一整段字符串，按非空即有问题处理
            _open_qs = [_open_qs] if _open_qs.strip() else []
        _open_qs = [str(q).strip() for q in (_open_qs or []) if str(q).strip()]

        _handoff = (_args.get("handoff_summary") or "").strip()

        logger.info(
            f"[Router] create_new_skill 触发（requirement={_req[:50]!r}，"
            f"handoff={len(_handoff)}字符，open_questions={len(_open_qs)}）"
        )

        if _open_qs:
            async for _ev in self._emit_creation_clarification(_req, _handoff, _open_qs):
                yield _ev
            return

        # ⭐⭐⭐ **fail-closed：没有交接包就不许进代码生成。**
        #
        # 🔴 这一条是拆掉探索之后**必须补回来的边界**，2026-08-13 实测当场证实：
        #
        #     07:55:49  [Router] create_new_skill（requirement='部署这个吧'，handoff=0字符）
        #     07:55:52  [SkillWriter] SkillSpec hard_validate 失败 → **降级为直接代码生成**
        #     07:55:59  [SkillWriter] 模型未调 WriteSkill → 一段莫名其妙的追问
        #
        # 用户当时说的是「部署这个吧」（对着一张待审卡），模型误当成新建需求。
        # ⚠️ **模型选错工具这件事，拆不拆探索都会发生**（CMD63 就是同一形状）；
        #    真正变了的是**失败方式**：
        #      · 旧：进 Explorer → 越界重试 → 链尾兜底 → **停下来**，说清楚发生了什么
        #      · 新：直接进 SkillWriter → 拿一句话去生成代码 → 失败得莫名其妙
        #    因为 SkillWriter 内部那条 `SkillSpec 失败 → 降级为直接代码生成` 是 **fail-open**。
        #
        # 📌 **拆掉一个模块时，要连它承担的【失败方向】一起接管** ——
        #    Explorer 的实现可以删，它的 fail-closed 语义不能跟着删。
        #    （这一条外部评审明确要求过，第一版漏做了，实测 20 分钟内就撞上。）
        #
        # ⚠️ 而这**不是**「为了规范模型而加约束」（那条要防的东西）：
        #    它不改控制流、不新增循环、不强制顺序，只是拒绝**在没有需求的情况下**
        #    去写代码 —— 与 `open_questions` 非空时不推进是同一类。
        # 📌 **「我不知道要做什么」的正确出口是问，不是猜着做。**
        if not _handoff:
            logger.warning(
                f"[Router] create_new_skill 没带 handoff_summary → 拒绝进代码生成"
                f"（requirement={_req[:40]!r}）"
            )
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       "create_new_skill was called without a handoff_summary, so no "
                       "code was written and nothing was created. "
                       f"All that came through was: {_req[:60]!r}. "
                       "Ask the user what the Skill should actually do - what it takes "
                       "in and what it produces. If they actually meant to deploy the "
                       "draft that is already pending review, tell them they can just "
                       "say so."),
                   "log": "create_new_skill 缺 handoff_summary，fail-closed 停下。"}
            return

        # ⭐ 开新的创建流程 = 挂着的同需求澄清已被接管（实测）。
        #    模型有时**不调** `answer_open_interaction`，而是直接开一条新的
        #    `create_new_skill`（需求文本里已经包含了那个回答）。需求办成了，
        #    但那条澄清会留在原地变僵尸。所以这里"按真实意图读"：
        #    新流程既然涵盖同一个需求，那条澄清是**被取代**，不是还在等回答。
        _rt_supersede_covered_clarifications(self, _req)

        # ── 证据交接：Writer 收到的是 requirement + handoff ────────────────
        # ⚠️ 段落标题沿用 `[Creation Handoff]`，与 Writer 侧提示词里的术语一致。
        _enriched = f"{_req}\n\n[Creation Handoff]\n{_handoff}" if _handoff else _req

        async for step in self._generate_skill_with_writer(
            _enriched, base_guide, realtime_callback,
        ):
            yield step
            for _q in self._drain_event_queue(event_queue):
                yield _q

    async def _emit_creation_clarification(self, requirement: str, handoff: str,
                                           questions: list[str]):
        """模型自报还有未决问题 → 把问题说给用户，并登记成一条持久 Interaction。

        ⚠️ **这里【不】重跑任何子循环** —— 探索已拆。用户回答之后走的是
        `answer_open_interaction` → 把事实交回主 ReAct → 主模型重新决策
        （可能再调 `create_new_skill`、也可能改调 `update_existing_skill`，
        甚至发现现成 Skill 就够用）。📌 **这比"重新造一个 Explorer"干净**：
        续接本来就不需要一个子循环，它需要的只是「上次问到哪了」这份领域状态。

        ⭐ 登记 Interaction 的理由**不是 UI**（澄清早已不上待办卡了），而是：
        `MemoryManager` 默认只保留约 10 个真实用户回合，所以「隔十几轮 + 重启
        之后回来回答」这件事**靠对话历史接不住**。checkpoint 是那份领域状态的载体。
        """
        _q_text = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        _content = (
            f"要把「{requirement[:40]}」做成 Skill，还有几点需要你确认：\n\n{_q_text}"
            if requirement else f"还有几点需要你确认：\n\n{_q_text}"
        )
        # ⚠️ `_content` 保留 —— 它是**待办条目**的展示文本（界面构件，豁免⑤），
        #    而且登记发生在模型开口之前，拿不到模型的措辞。
        _rt_open_clarification(self, requirement, handoff, _content)
        yield {"event": "exit_flow_defer_to_model",
               "tool_result": (
                   f"Before any code can be written, {len(questions)} question(s) "
                   f"still need the user's answer"
                   + (f" about {requirement[:40]!r}" if requirement else "")
                   + f". Ask them these, keeping them as separate points:\n{_q_text}"),
               "log": f"创建流程自报 {len(questions)} 个未决问题，等用户回答。"}

    async def _exit_answer_open_interaction(self, exit_call, decision, *, used_model,
                                            base_guide, realtime_callback, event_queue):
        async for _ev in self._handle_answer_interaction(
            exit_call.args or {}, base_guide, realtime_callback, event_queue
        ):
            yield _ev
            for _q in self._drain_event_queue(event_queue):
                yield _q

    async def _exit_write_skill(self, exit_call, decision, *, used_model,
                                base_guide, realtime_callback, event_queue):
        # ⚠️ 这条是**幻觉恢复路径**，不是正规通路：`WriteSkill` 的 preload 是
        #    `HIDDEN`，主决策的工具清单里从来没有它（见 `Preload.HIDDEN` 的文档）。
        #    模型偶尔仍会凭记忆调出这个名字，那时它的意图是明确的 —— 转去出预览。
        logger.warning("[ReAct] WriteSkill 出现在主循环，转 _emit_skill_preview")
        async for step in self._emit_skill_preview_from_decision(
            self._exit_decision_from(exit_call, decision), used_model
        ):
            yield step

    async def _execute_one_tool_call(
        self,
        call: ToolCall,
        *,
        used_model: str,
        base_guide: str,
        system_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
        active_tool_names: frozenset | None = None,
    ) -> ToolExecution:
        """执行单个工具调用，把 tool_start/tool_end 事件推进 event_queue。

        不负责写 memory（由 ReAct 主循环统一批量写入）。
        """
        self._ensure_react_sems()
        name = call.name
        args = call.args or {}
        aid = f"react_{call.index}_{time.time_ns()}"
        raw_result = None
        tool_data = None
        # 目录 + 这一刻的运行时事实。工具卡文案、handler 解析都问它。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()

        # ⭐⭐⭐ **在 GUI 模式里，任何工具调用之前都先看机器归谁。**
        #
        # 「只要 mini 窗口存在，不管是 nano 任何行为 ——
        # 命令行、mcp、键鼠模拟、skill 等等，全部受被动挂起的监控，
        # **不管 nano 目前在做什么，只要用户进行操作，一律挂起**。」
        #
        # ⚠️ 第一版只把**感知**换成了全量，**等待**还留在 `os_execute` 里、
        #    而且还额外要求是键鼠动作 —— 用户当场看出来只做了一半。
        #    📌 **感知的范围和反应的范围必须一致**，否则用户看到接管状态条却发现
        #    Nano 照样在动，那正是一开始诊断出的那个"UI 语义不一致"。
        #
        # ⭐ 顺带更正之前一个判断：曾经认为"截图不该被拦，因为恢复后要判断环境
        #    变没变恰恰需要能看"。但 `_look_at_screen` **会最小化/还原自己的窗口**
        #    来避开遮挡 —— 那是**抢焦点**，它确实在跟用户争。所以它也该等。
        #
        # ⚠️ 等待期间零 LLM、零 token（纯 `asyncio.sleep`），只占一个挂着的 turn。
        # ⚠️ 不在 GUI 模式时**完全不查**（一次数据库读都不多花）——
        #    纯命令行 / 纯对话的一轮完全不受影响。
        _gui_waited = 0.0
        try:
            from core.runtime import oslease as _ol_gw
            from core.runtime.kernel import get_kernel as _gk_gw
            _k_gw = _gk_gw()
            if _ol_gw.gui_session_active(_k_gw):
                _may_gw, _why_gw = _ol_gw.nano_may_touch_os(_k_gw)
                if not _may_gw:
                    logger.info(f"[A3] GUI 模式中，机器不归 Nano（{_why_gw}）—— "
                                f"工具 {name!r} 前进入等待（**不结束 turn**）")
                    _h_gw, _gui_waited = await _ol_gw.await_activity_lease(
                        f"before tool {name}")
                    # ⚠️ 只有键鼠动作才真的需要**持有**租约；其余工具等到"没人占着"
                    #    就够了，拿了反而会让 canary 误以为 Nano 在开车。
                    #    📌 「等到可以动」和「持有驾驶权」是两件事。
                    if _h_gw is not None:
                        try:
                            from core.os_layer import dsl as _dsl_gw
                            if name == "os_execute" and _dsl_gw.contends_for_machine(
                                    (args or {}).get("action") or ""):
                                self._rt_os_lease = _h_gw
                            else:
                                _ol_gw.release_activity(_h_gw)
                        except Exception:
                            _ol_gw.release_activity(_h_gw)
                    logger.info(f"[A3] 等了 {_gui_waited:.0f}s，继续执行 {name!r}")
                    # ⭐⭐ 取出这段挂起期的证据日志。
                    #
                    # ⚠️⚠️ **必须在这里取，不能只在 `os_execute` 那一支取** ——
                    #    等待本身是**全量**的（命令行 / MCP / skill / 截图 / 键鼠都会等），
                    #    只在键鼠那一支消费的话，"用户接管期间发生了什么"这条信息
                    #    就取决于 Nano 恢复后**碰巧先调哪个工具**。
                    #    📌 **一份一次性的证据，取它的地方必须和产生它的地方同一个作用域。**
                    #
                    # ⚠️ 日志是**一次性**的：`finish()` 取完即清。所以这里取到就必须
                    #    真的送到模型面前（见函数尾部那段），不能取了不用 —— 那等于丢证据。
                    try:
                        from core.proactive import takeover_log as _tl_gw
                        _sess_gw = _tl_gw.finish()
                        _sum_gw = _tl_gw.summarize(
                            _sess_gw, _tl_gw.sensors_healthy(_sess_gw))
                        if _sum_gw:
                            self._a3_pause_note = _sum_gw
                            logger.info("[A3] 挂起期证据日志已生成，"
                                        f"{len(_sum_gw)} 字符 → 随本次工具结果交给模型")
                    except Exception as _e_tl:
                        logger.debug(f"[A3] 挂起期日志生成失败（不阻断）: {_e_tl}")
        except Exception as _e_gw:
            logger.debug(f"[A3] GUI 模式等待检查失败（放行）: {_e_gw}")

        # ── 能力门控·第二道：执行层 fail-fast ────────────────────────────
        # manifest 门控（第一道）能挡住绝大多数，但挡不住这三种：模型沿用上一轮已加载
        # 的旧 schema、load_tools 动态拉进来的、以及故障恰好发生在本轮 manifest 构建
        # 之后。所以真正动手前再查一次——宁可返回一个结构化的"不可用"，也不让它进到
        # RAG 函数里摔出原生异常。
        try:
            from core.health import get_health
            _blk = get_health().tool_block_reason(name)
        except Exception:
            _blk = None
        if _blk is not None:
            _rt = (
                f"Tool \"{name}\" is currently unavailable: {_blk.user_message} "
                + (f"Recovery hint: {_blk.recovery_hint} " if _blk.recovery_hint else "")
                + "Do not retry this tool. Tell the user plainly what is broken, and continue with "
                  "whatever you can still do."
            )
            logger.warning(f"[Health] 执行层拦截工具【{name}】：{_blk.code}")
            await event_queue.put({
                "event": "tool_start", "action_id": aid,
                "tool_use_id": getattr(call, "tool_use_id", "") or "",
                "action_display": _cat.presentation(name, args),
                "action_input": str(args)[:200],
                "log": f"能力不可用，已拦截【{name}】",
                "status": "TOOL_EXECUTING", "model": used_model, "current_skill": name,
            })
            await event_queue.put({
                "event": "tool_end", "action_id": aid,
                "result_summary": f"能力不可用：{_blk.user_message}"[:80],
                "status": "SYS_IDLE", "model": used_model,
                "current_skill": name, "tool_use_id": call.tool_use_id, "ok": False,
            })
            return ToolExecution(call=call, result_text=_rt, ok=False,
                                 error=f"capability unavailable: {_blk.code}")

        # ══════════════════════════════════════════════════════════════════
        # ⭐⭐⭐ 本次 request 的 tool contract 闸门
        # ══════════════════════════════════════════════════════════════════
        #
        # ═══ 它堵的洞（实测实证 2026-08-13）═══
        #
        #     07:55:39  [TOKEN-PLAN] core_tools=6（不含 create_new_skill，本轮也没 load_tools）
        #     07:55:49  [Router] create_new_skill 触发（requirement='部署这个吧'）  ← 照样执行了
        #
        # 用户对着一张待审卡说「部署这个吧」，模型**凭上一轮的 schema 记忆**调了
        # `create_new_skill`，而 `answer_open_interaction` 就在它手边。
        # ⭐ 这正是 要修的问题本身：**模型把"我曾经能调"当成"我现在能调"。**
        #
        # ═══ ⚠️ 这一条【推翻】了工具目录里的一条既有规定，不是 bug 修复 ═══
        #
        # 原规定（`catalog.py` 模块头 + `t_f4_catalog.py` 模块头两处）：
        #     「deferred 工具『schema 当前没附带但仍可执行』是**正常状态**」
        # **现在改成：主模型的工具调用必须受【产生该调用的那次 API request 的
        # tool contract】约束。** 读到 `catalog.py` 那条的人请看这里，
        # 不要以为 Catalog 被改坏了。
        #
        # ⚠️ 而这不是给正常路径加一轮：`DEFERRED_HEADER` 本来就白纸黑字写着
        #    「call load_tools first … then call the loaded tool **on the next step**」。
        #    所以这是**关掉一条与自己声明矛盾的旁路**。
        #
        # ═══ 五格顺序（少一格就会抢掉别处已经正确的诊断）═══
        #
        #   1. health blocked          → 上面那段（已 return）。已经正确，本闸不许抢
        #   2. Preload.HIDDEN          → 放行到既有内部语义，**绝不能建议 load_tools**
        #                                （HIDDEN 的定义就是"处理得了但刻意不给正式通路"，
        #                                  而 `load_tools` 本来就搜不到它）
        #   3. availability=False      → 放行到 handler，由它说「你要处理的那个对象没了」
        #                                （`eligible ⊆ resolvable` 单向包含正为它设计，
        #                                  失败信息必须**正确**，"不存在"是假话）
        #   4. eligible+DEFERRED+未附  → **本闸拦在这里** → TOOL_NOT_ACTIVE
        #   5. 名字不存在              → 下游既有的 UNKNOWN_TOOL 诊断（含最接近的真名字）
        #
        # ⚠️ **fail-open 是刻意的**：拿不到 `active_tool_names`（老路径、测试替身、
        #    子流程）时**不校验**。📌 一道新加的闸，在信息不全时应当放行 ——
        #    否则它会在自己还没接全的地方制造假故障。
        _active = active_tool_names
        if _active:
            _d6 = _cat.get(name)
            if (_d6 is not None                                  # 存在（否则交给 ⑤）
                    and _d6.preload is Preload.DEFERRED           # 排除 CORE / HIDDEN（②）
                    and _d6.is_eligible(ToolScope.MAIN, _rtv)     # 排除 availability=False（③）
                    and name not in _active):                     # 本次 request 没附它
                logger.warning(
                    f"[F6] 工具【{name}】不在本次 request 的工具表里 → 拒绝执行"
                    f"（本次 attach 了 {len(_active)} 个）"
                )
                _rt6 = (
                    f"Tool \"{name}\" was not executed because it was not active in the "
                    f"tool set attached to this model request.\n"
                    f"[Tool Call Diagnostics]\n"
                    f"- cause: {self._ToolFailCause.TOOL_NOT_ACTIVE} — the tool exists and is "
                    f"currently available, but its schema was not attached to this request.\n"
                    f"- This call came from stale knowledge of an earlier request's tool set, "
                    f"not from the tool contract of the request you are answering now.\n"
                    f"- Next: call load_tools(names=[\"{name}\"]) and then call it on the "
                    f"following step. Loading takes effect from the next step, not this one."
                )
                await event_queue.put({
                    "event": "tool_start", "action_id": aid,
                    "tool_use_id": getattr(call, "tool_use_id", "") or "",
                    "action_display": _cat.presentation(name, args),
                    "action_input": str(args)[:200],
                    "log": f"工具未在本轮工具表中，已拦截【{name}】",
                    "status": "TOOL_EXECUTING", "model": used_model, "current_skill": name,
                })
                await event_queue.put({
                    "event": "tool_end", "action_id": aid,
                    "result_summary": f"未加载：{name}"[:80],
                    "status": "SYS_IDLE", "model": used_model,
                    "current_skill": name, "tool_use_id": call.tool_use_id, "ok": False,
                })
                return ToolExecution(call=call, result_text=_rt6, ok=False,
                                     error=f"tool not active in this request: {name}")

        await event_queue.put({
            "event": "tool_start",
            "action_id": aid,
            # ⭐ 详情按 `tool_use_id` 回账本取（参数与结果都在那边）。
            #    ⚠️ **刻意不带 args** —— 那会让参数有两个来源（live 走事件 /
            #    重放走账本），而两个来源只在"我两次想法相同"时一致。
            "tool_use_id": getattr(call, "tool_use_id", "") or "",
            "action_display": _cat.presentation(name, args),
            "action_input": str(args)[:200],
            "log": f"ReAct: 执行工具【{name}】",
            "status": "TOOL_EXECUTING",
            "model": used_model,
            "current_skill": name,
            "tool_use_id": call.tool_use_id,
        })

        # 统一失败标记。⚠️ 原注释写「内置工具不会设它」——**那句已过期**：
        # 实际有 5 个内置工具会设（set_next_checkin / task_boundary / cancel_wait /
        # wait_for / os_execute），它们的 handler 返回 `ToolOutcome(text, failed)`。
        # 📌 这正是 要修的问题的一个小样本：**注释描述的能力与实现不一致，而且不报错。**
        _is_failed = False
        try:
            # ── 内置伪工具 ───────────────────────────────────────────────
            # 分支体已机械提取成 `_handle_*`（纯重构，行为零变化）。
            # cutover 时这一串 if/elif 会被 `catalog.resolve(name, scope)` 取代。
            # ── 内置工具：**问目录谁来处理** ─────────────────────────────
            #
            # 🔴 改造前这里是 **19 段 `elif name == "..."`** —— 一份按工具名建立的
            #    第二权威。分支体已机械提取成 `_handle_*`（纯重构、行为
            #    零变化），这一步只把「谁处理」这个问题交还给唯一权威。
            #
            # ⚠️ 统一契约是**实测**出来的、不是设计出来的：提取前用 AST 量过，
            #    分派链**之后**只读 `result_text` 一个变量 —— 所以 handler 的完整
            #    契约就是「产出文本 + 这次算不算失败」，`str | ToolOutcome` 两种写法
            #    由 `ToolOutcome.of()` 归一（同 `SkillResult.from_raw` 的既有范式：
            #    **一个便利构造器，不是两种并存的契约**）。
            #
            # ⚠️ 参数**取全集**：`call` / `used_model` / `gui_waited` 只有 `os_execute`
            #    用得上，其余 handler 由 `**_ctx` 收着。为什么不"按需传"——
            #    按需传只有两种实现：按名字特判（正是这次要删的东西），
            #    或反射签名（把"参数写错"从启动期错误变成运行期错误）。
            #
            # ⚠️ **只有内置走这条路**。本地 Skill 与 MCP 的执行体仍是下面两段内联
            #    分支：它们除了 `result_text` 还要回填 `raw_result` / `tool_data`
            #    （Skill 的 `dsl_plan` 转路由就靠它），契约比这批 handler 宽 ——
            #    那次机械提取当时也正是按这条线划的（19/19 = 全部内置）。
            #    📌 目录对这件事**如实描述**：它们的 binding 指向
            #       `_execute_one_tool_call` 本身，因为"谁处理"现在确实是这个函数
            #       （同探索作用域的 binding 指向 `_run_skill_exploration` 那个大循环
            #        —— **不为了好看虚构一层**）。
            _def = _cat.get(name)
            _handler_ref = _cat.resolve(name, ToolScope.MAIN, _rtv)
            if (_def is not None and _def.origin is ToolOrigin.BUILTIN
                    and _handler_ref is not None):
                _out = await getattr(self, _handler_ref)(
                    args, aid, event_queue=event_queue,
                    call=call, used_model=used_model, gui_waited=_gui_waited)
                if isinstance(_out, ToolExecution):
                    # `os_execute` 独有的两处提前返回（机器被占 / 长命令交还）：
                    # 语义是「这次调用已经自己完成了全部收尾」，所以直接 return。
                    return _out
                _outcome = ToolOutcome.of(_out)
                result_text, _is_failed = _outcome.text, _outcome.failed

            elif self._is_mcp_tool(name):
                # ── MCP 工具执行（Nano 自身能力，内联分发，不进 registry）──
                # 照 os_execute/render_visual/wait_for 范式：懒加载 manager、调用、回填
                # result_text。tool_start/tool_end 卡片由本函数头尾自动发，无需手动加。
                _mcp_mgr = self._mcp_manager
                # 在场确认：仅当 server 明确标 destructive（不可逆）时弹确认卡，复用 execution_confirm。
                # 普通读/写不拦——透明度由工具卡片保证（对齐"可逆直接做、不可逆先确认"）。
                _mcp_confirm = _mcp_mgr.confirm_summary(name)
                if _mcp_confirm:
                    _confirm_ev = asyncio.Event()
                    _cancelled = [False]
                    _loop = asyncio.get_running_loop()

                    def _on_confirm():
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    def _on_cancel():
                        _cancelled[0] = True
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    await event_queue.put({
                        "event": "execution_confirm",
                        "skill_name": name,
                        "side_effects": [_mcp_confirm],
                        "on_confirm": _on_confirm,
                        "on_cancel": _on_cancel,
                    })
                    from core.runtime import inbox as _ib6
                    _oc6 = await _ib6.wait_confirm_or_user_message(_confirm_ev, 300)
                    if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                        _cancelled[0] = True
                    if _cancelled[0]:
                        # ⚠️ 两种「没执行」要分开说：**用户改口**和**干等超时**
                        #    在结果上一样，但要告诉模型的话完全不同。
                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                            result_text = (
                                f"External capability \"{name}\" was NOT executed.\n"
                                + _ib6.cancelled_by_user_message_note())
                        else:
                            result_text = (f"External capability \"{name}\" was "
                                           f"cancelled by the user.")
                        await event_queue.put({
                            "event": "tool_end", "action_id": aid,
                            "result_summary": result_text[:80], "status": "SYS_IDLE",
                            "model": used_model, "current_skill": name, "ok": False,
                        })
                        return ToolExecution(call=call, result_text=result_text, ok=False,
                                             error="user cancelled execution")

                # ── 自动后台化（MCP 就是 Nano 的一部分，长调用不特殊对待）──────────
                # 先发起调用，race 一个阈值。阈值内回 = 同步快路径（绝大多数 MCP 是
                # 请求/响应）；超阈值 = 系统【自动】转后台（不需要模型判断哪个工具是
                # "长任务"）：登记 background 挂起 + 把还在跑的调用交给 _start_bg_task
                # 生产端 + 出等待 pill，让模型说句"我去跑着"结束本轮，跑完
                # notify_background_done 自动唤醒续做。复用整套背景唤醒桥。
                # ⭐⭐⭐ **ref 必须在发起调用之前就定下来。**
                #    🔴 上一版是在「超阈值」那条分支里才算 `_bg_ref` ——
                #       那已经是 90 秒之后，而进度订阅必须在**请求发出时**
                #       就带上 `progressToken`，事后补不上。
                #    📌 **一个「出问题时才需要的标识」，如果它同时是
                #       「观测的订阅凭据」，就必须在事情开始时就存在** ——
                #       等到出问题才生成，前 90 秒的进度就永远丢了。
                _bg_ref = f"long_{call.tool_use_id or aid}"
                _bg_display = _cat.presentation(name, args)
                _mcp_task = asyncio.create_task(_mcp_mgr.call(
                    name, args, timeout=self._HANDED_BACK_DEADLINE_SEC,
                    progress_ref=_bg_ref))
                # ⭐ 等它跑完**或者等到用户又说话** —— 见 `_wait_or_user_speaks`。
                _mcp_finished = await self._wait_or_user_speaks(
                    _mcp_task, self._LONG_TASK_HANDBACK_SEC)
                _mcp_done = {_mcp_task} if _mcp_finished else set()
                self._session_log_append(f"[MCP call] {name}")
                self._last_called_skill = name

                if _mcp_task in _mcp_done:
                    # 快路径：阈值内返回 → 这条 ref 再没人看，丢掉轨迹。
                    # ⚠️ 不丢的后果不是崩，是**总线上慢慢堆满已完成的调用** ——
                    #    `_MAX_REFS` 那个兜底会开始丢掉真正活着的那些。
                    #    📌 一个兜底上限的存在，不免除正常路径显式收尾的义务。
                    try:
                        from core.runtime import progress as _pb1
                        _pb1.forget(_bg_ref)
                    except Exception:
                        pass
                    _mcp_text, _mcp_err, _mcp_needs_auth, _mcp_server = _mcp_task.result()
                    result_text = _mcp_text
                    _is_failed = bool(_mcp_err)
                    if _mcp_needs_auth:
                        # 在场授权（OAuth 延后授权）：server 需登录。发事件让 UI 渲染授权卡。
                        await event_queue.put({
                            "event": "mcp_auth_required",
                            "server": _mcp_server,
                            "tool": name,
                        })
                        result_text = (
                            f"External service \"{_mcp_server}\" requires login authorization before use. "
                            "Tell the user in one sentence that authorization is required before this capability can be used. "
                            "They can log in from MCP connections in settings."
                        )
                    if _is_failed:
                        self._last_skill_error = {
                            "skill": name,
                            "error": f"Tool \"{name}\" failed: {result_text}",
                            "consumed": False,
                        }
                else:
                    # ⭐ 慢路径：超阈值 → **交回控制权**（不是「转后台」）。
                    #    公共合同见 `_hand_back_long_task` —— MCP 在这里
                    #    **没有任何特殊之处**，它只是恰好是第一个用上它的载体。
                    result_text = await self._hand_back_long_task(
                        display=_bg_display, bg_ref=_bg_ref,
                        action_id=aid, event_queue=event_queue)

                    # ⭐⭐⭐ **形状转换放在知道形状的这一端。**
                    #
                    # 🔴 上一版直接把 `_mcp_task` 塞进事件，而 app 侧的消费端写的是
                    #    `_txt, _err, _na, _srv = await _t` —— **写死了 MCP 的四元组**。
                    #    于是长命令接进来那一步（它的载体返回一个字符串）
                    #    在完成的那一刻会被解成
                    #    `后台执行失败：too many values to unpack (expected 4)`。
                    #    ⚠️ 而当时的注释写的是「事件名保留 `mcp_background_request`
                    #       —— 改它要同时动 app 侧」——
                    #       📌 **一个「保留旧名字」的决定，必须同时检查那个事件的
                    #          消费端还假设着什么。名字兼容 ≠ 形状兼容。**
                    #
                    # ⭐ 修法：每个载体自己把结果包成**给模型看的文本**，
                    #    消费端只 `await` 出一个字符串。
                    #    📌 **形状转换要发生在知道形状的那一端**，
                    #       不是让公共消费端认识每一种载体。
                    async def _await_mcp(_t=_mcp_task, _n=name, _r=_bg_ref):
                        try:
                            _txt, _err, _na, _srv = await _t
                        except Exception as _e:
                            return (f"External capability \"{_n}\" failed after it was "
                                    f"handed back: {type(_e).__name__}: {_e}")
                        finally:
                            try:
                                from core.runtime import progress as _pb2
                                _pb2.forget(_r)
                            except Exception:
                                pass
                        if _err:
                            return (f"External capability \"{_n}\" finished with an "
                                    f"error:\n{_txt}")
                        return f"External capability \"{_n}\" finished:\n{_txt}"

                    await event_queue.put({
                        "event": "long_task_handback",
                        "task": asyncio.ensure_future(_await_mcp()),
                        "bg_task_ref": _bg_ref,
                        "display": _bg_display,
                    })
            else:
                # 兜底：模型有时把 os_execute 的 action 名（file_write/click/run_command…）
                # 当成独立工具直接调 → registry 里没有 → 会死 fail。这里识别并引导回正道。
                #
                # 🔴🔴 改造前这里是**手抄的 29 个 action**，而 `dsl.py` 实际有 **39 个**
                #    —— 漏了 10 个，且专挑高频项漏：`file_delete` / `file_move` /
                #    `write_registry` / `network_config` / `manage_service` /
                #    `modify_startup` / `schedule_task` / `set_env_var` /
                #    `read_screen_region` / `request_user_choice`。
                #    后果具体：模型把 `file_delete` 当成独立工具调时，兜底**不会**告诉它
                #    "这是 os_execute 的一个 action"，只给一句泛泛的"工具不存在"。
                # 📌 **一份手抄的清单，它的过期是静默的，而且专挑高频项漏。**
                #
                # ⚠️⚠️ **权威取 `dsl.ALL_ACTION_NAMES`，不取 `os_execute` manifest 的
                #    `action.enum`** —— 早先写的是后者，但回代码一核，那个 enum
                #    **本身也是手抄的、也过期了**：它只有 36 个，缺 `move` /
                #    `read_screen_region` / `request_user_choice`，而且 `move` 恰好
                #    是旧 `_OS_ACTIONS` 有、enum 没有的那一个 —— 照早先的设计做会
                #    **让这条兜底反而少认一个 action**。
                #    📌 判据就是 自己那条：**派生要派生到原生权威为止**。
                #       `_ACTIONS` 表在 `dsl.py`，它是执行器真正查的那张表；
                #       manifest 的 enum 只是它的又一份手抄件 ——
                #       **从一份手抄件派生，得到的还是手抄件。**
                #    （enum 少的那 3 个是另一个独立问题：模型 schema 层根本填不了
                #      它们。已单独留痕，不在本次"只切权威"的范围内。）
                try:
                    from core.os_layer import dsl as _dsl_fb
                    _os_actions = _dsl_fb.ALL_ACTION_NAMES
                except Exception:
                    _os_actions = frozenset()
                if name in _os_actions:
                    # ⭐⭐⭐ [2026-08-23 拆分] **必须按归属指路，不能写死 `os_execute`。**
                    #
                    # 🔴 这是整次拆分里**最危险的一处**：它是一条**纠错指引** ——
                    #    模型把 action 当独立工具调时，靠它回到正道。
                    #    拆分后 `click` 属于 `computer_use`，而这句话若仍写死
                    #    `os_execute`，就会把模型**精确地导向错误的工具**，
                    #    然后它拿到「没有这个 action」，再猜一次。
                    # 📌 **一条指错方向的纠错提示，比没有这条提示更坏** ——
                    #    没有时模型会重新想，有错的时它会照着走。
                    # ⚠️ 归属取 `dsl` 的 `tool` 字段（唯一权威），不另维护映射。
                    try:
                        _owner = _dsl_fb._ACTIONS[name].tool
                    except Exception:
                        _owner = "os_execute"
                    _rt = (
                        f"\"{name}\" is not a standalone tool. It is a {_owner} action. "
                        f"Call {_owner}(action='{name}', params={{...}}, declared_risk=...)."
                        + ("" if _owner == "os_execute" else
                           f" {_owner} is not loaded by default - load it first.")
                    )
                    await event_queue.put({
                        "event": "tool_end", "action_id": aid,
                        "result_summary": _rt[:80], "status": "SYS_IDLE",
                        "model": used_model, "current_skill": name, "ok": False,
                    })
                    return ToolExecution(call=call, result_text=_rt, ok=False,
                                         error="os_execute action was incorrectly called as a standalone tool")
                # ── 先判"名字存不存在"，再往下走 ───────────────────
                # 必须在 execute() 之前判：`registry.execute` 对"找不到"和
                # "跑了但返回 None"都返回 None，事后无法区分（见 registry.py:280）。
                #
                # ⚠️ 也必须在**副作用确认之前**判。这个位置一开始放错了，
                # 结果是给一个根本不存在的 Skill 弹副作用确认框，然后
                # `asyncio.wait_for(..., timeout=300)` 干等五分钟——
                # 用户会看到一个"要不要允许 XXX 写文件"的框，而 XXX 压根不存在。
                _enabled_names = []
                try:
                    _enabled_names = self.registry.list_enabled_skills()
                except Exception:
                    pass
                if _enabled_names and name not in _enabled_names:
                    _disabled = []
                    try:
                        _disabled = self.registry.list_disabled_skills()
                    except Exception:
                        pass
                    # ⭐⭐ **MCP 也要能说出「它被禁用了」。**
                    #
                    # 🔴 问题：`list_tool_manifests` 只收 `ST_CONNECTED` 的 server，
                    #    于是一个被禁用的 MCP，它的工具**从目录里彻底消失** ——
                    #    模型调它会被判成 UNKNOWN_TOOL（"这个名字不存在"）。
                    #    而这正是本文件在 `_ToolFailCause` 那里明令避免的那件事：
                    #    **塞进 UNKNOWN_TOOL 是撒谎（它明明存在）。**
                    # ⚠️ MCP 比 Skill 更棘手：Skill 的 manifest 是本地文件，禁用了
                    #    照样读得到；**MCP 的工具清单要连上才知道** ——
                    #    从没连过的 disabled server，我们不可能知道它有哪些工具。
                    # ⭐ 但 MCP 的工具名**自带 server 名** ⇒ 判据下移一层：
                    #    从「这个**工具**被禁用了」下移到「它所属的**服务**被禁用了」。
                    #    📌 答不出精确的那一问时，答一个**同样有用且答得准**的问题，
                    #       比猜一个精确答案强。
                    _mcp_off = ""
                    try:
                        from core.mcp_client import MCPManager as _MM_d
                        _mcp_off = _MM_d.instance().disabled_tool_server(name)
                    except Exception:
                        _mcp_off = ""
                    if _mcp_off:
                        _cause = self._ToolFailCause.DISABLED_TOOL
                        _head = (f"The MCP server \"{_mcp_off}\" is currently disabled, "
                                 f"so \"{name}\" was not run.")
                    elif name in _disabled:
                        _cause = self._ToolFailCause.DISABLED_TOOL
                        _head = f"Skill \"{name}\" exists but is currently disabled, so it was not run."
                    else:
                        _cause = self._ToolFailCause.UNKNOWN_TOOL
                        _head = f"Tool \"{name}\" does not exist. The name is wrong; this is not a transient failure."
                    result_text = _head + "\n" + self._describe_tool_failure(name, _cause)
                    logger.warning(f"[ReAct] 工具调用失败 {_cause}: {name}")
                    await event_queue.put({
                        "event": "tool_end", "action_id": aid,
                        "result_summary": _head[:80], "status": "CORE_THINKING",
                        "model": used_model, "current_skill": name,
                        "tool_use_id": call.tool_use_id, "ok": False,
                    })
                    return ToolExecution(call=call, result_text=result_text, ok=False,
                                         error=f"{_cause}: {name}")

                # 注：延迟的技能/MCP 即使没 load 也能按名字直接执行（registry/MCP 分发与
                # schema 注入解耦），所以这里不拦——load_tools 只为给模型正确的参数 schema。
                # ── 本地 Skill 执行 ──────────────────────────────────────
                # 副作用确认检查
                _side_check = self._check_skill_side_effects(name)
                if _side_check:
                    _confirm_ev = asyncio.Event()
                    _cancelled = [False]
                    _loop = asyncio.get_running_loop()

                    def _on_confirm():
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    def _on_cancel():
                        _cancelled[0] = True
                        _loop.call_soon_threadsafe(_confirm_ev.set)

                    await event_queue.put({
                        "event": "execution_confirm",
                        "skill_name": name,
                        "side_effects": _side_check,
                        "on_confirm": _on_confirm,
                        "on_cancel": _on_cancel,
                    })
                    from core.runtime import inbox as _ib6
                    _oc6 = await _ib6.wait_confirm_or_user_message(_confirm_ev, 300)
                    if _oc6 != _ib6.ConfirmOutcome.CONFIRMED:
                        _cancelled[0] = True

                    if _cancelled[0]:
                        # ⚠️ 同上：用户改口 vs 干等超时，要分开告诉模型。
                        if _oc6 == _ib6.ConfirmOutcome.USER_MESSAGE:
                            result_text = (f"Skill \"{name}\" was NOT executed.\n"
                                           + _ib6.cancelled_by_user_message_note())
                        else:
                            result_text = (f"Skill \"{name}\" execution was "
                                           f"cancelled by the user.")
                        await event_queue.put({
                            "event": "tool_end",
                            "action_id": aid,
                            "result_summary": result_text[:80],
                            "status": "SYS_IDLE",
                            "model": used_model,
                            "current_skill": name,
                            "ok": False,
                        })
                        return ToolExecution(call=call, result_text=result_text, ok=False,
                                             error="user cancelled execution")

                # ⭐⭐⭐ [统一长任务 · 第三类载体] **本地 Skill 走同一条交还合同。**
                #
                # 🔴 上一版这里是 `async with sem: raw_result = await execute(...)`
                #    —— **没有超时，也没有交还**。一个跑很久的 Skill 会把整轮
                #    无限期堵住，比改造前的 `run_command`（至少 30 秒会报错）
                #    还糟：那是「错了」，这是「永远不回来」。
                # 📌 已定原则：**我们要识别的只有「长任务」，
                #    跟任务类型从来没有关系过。** 命令 / MCP / Skill
                #    是同一件事的三个载体，不是三种机制。
                #
                # ⚠️ 阈值用同一个 `_LONG_TASK_HANDBACK_SEC`（**5s**，不是 90s ——
                #    2026-08-22 那次建模 把命令 45 / 外部服务 90 / Subagent 5 统一成 5，
                #    这两处注释忘了跟着改，2026-08-25 排查时才发现）。
                # 📌 一条说 90 的注释配一个写 5 的常量，读注释的人会按 90 去推理，
                #    而按 90 推出来的每一步都是错的 —— **过时的注释比没有注释更贵**。
                #    ⭐ 而**不对称的依据不是任务类型，是「回看那一眼能不能看到
                #       东西」**：命令有 stdout（45s 就交还，因为回看有价值），
                #       Skill 只有它自己调 `report_progress` 时才有 ——
                #       所以给它更长的前台耐心。
                _sk_ref = f"long_{call.tool_use_id or aid}"
                _sk_disp = _cat.presentation(name, args)
                # ⚠️⚠️ **信号量必须手动收放**，不能用 `async with`：
                #    交还之后 Skill **还在跑**，而这个函数要 return ——
                #    `async with` 会在 return 时释放，那没问题；
                #    问题是**不释放**才是错的。前台并发位是「前台等待」的资源，
                #    交还之后这件事已经归后台合同管了。
                #    📌 **一个操作的占位，在控制权被交回模型之后，应该由
                #       「后台合同」决定，而不再由「前台等待的耐心」决定。**
                #       （与 `_HANDED_BACK_DEADLINE_SEC` 同一条判据。）
                await self._tool_parallel_sem.acquire()
                _sem_held = True
                try:
                    _sk_task = asyncio.create_task(
                        self.registry.execute(name, args, progress_ref=_sk_ref))
                    # ⭐ 同 MCP 那条：用户一开口就立刻交还，不等满阈值。
                    _sk_finished = await self._wait_or_user_speaks(
                        _sk_task, self._LONG_TASK_HANDBACK_SEC)
                    if not _sk_finished:
                        # 超阈值 → 交还控制权，Skill 继续跑
                        self._tool_parallel_sem.release()
                        _sem_held = False
                        result_text = await self._hand_back_long_task(
                            display=_sk_disp, bg_ref=_sk_ref,
                            action_id=aid, event_queue=event_queue)

                        async def _await_skill(_t=_sk_task, _n=name, _r=_sk_ref):
                            try:
                                _raw = await _t
                            except Exception as _e:
                                return (f"Skill \"{_n}\" failed after it was handed "
                                        f"back: {type(_e).__name__}: {_e}")
                            finally:
                                try:
                                    from core.runtime import progress as _pb4
                                    _pb4.forget(_r)
                                except Exception:
                                    pass
                            _t2 = str(_raw) if _raw is not None else \
                                f"Skill \"{_n}\" ran but returned no content."
                            return f"Skill \"{_n}\" finished:\n{_t2}"

                        await event_queue.put({
                            "event": "long_task_handback",
                            "task": asyncio.ensure_future(_await_skill()),
                            "bg_task_ref": _sk_ref,
                            "display": _sk_disp,
                            "skill_name": name,
                        })
                        # ⚠️ 刻意**不**收这条 ActionAttempt —— 那次尝试还没结束。
                        #    📌 不许为一件还没结束的事记一个结论。
                        return ToolExecution(call=call, result_text=result_text, ok=True)
                    raw_result = _sk_task.result()
                finally:
                    if _sem_held:
                        self._tool_parallel_sem.release()
                try:
                    from core.runtime import progress as _pb5
                    _pb5.forget(_sk_ref)
                except Exception:
                    pass
                # 走到这里名字一定存在，所以 None 的唯一含义是"跑了但没返回内容"——
                # 这句话现在是真的了。
                result_text = str(raw_result) if raw_result is not None else \
                    f"Tool \"{name}\" ran but returned no content."
                tool_data = getattr(raw_result, "data", None)

                # 记录执行失败状态（供后续修复路由使用）
                _is_failed = False
                if raw_result is None:
                    _is_failed = True  # None 返回明确标为失败
                elif getattr(raw_result, "success", True) is False:
                    _is_failed = True
                if not _is_failed and isinstance(result_text, str) and any(
                    result_text.startswith(m) or m in result_text
                    for m in ("错误：", "【错误】", "【执行失败】", "执行故障", "执行失败")
                ):
                    _is_failed = True

                if _is_failed:
                    self._last_skill_error = {
                        "skill": name,
                        "error": f"Tool \"{name}\" failed: {result_text}",
                        "consumed": False,
                    }
                    # 名字对、跑了、失败了 —— 区分"参数不合 schema"和"执行本身失败"。
                    # `registry.execute` 的参数校验失败会返回以 "Execution failed:" 开头的串
                    # （`registry.py` 里那三处），那三条是同一类：工具在，参数不对。
                    _param_fail = isinstance(result_text, str) and \
                        result_text.startswith("Execution failed:")
                    result_text = result_text + self._describe_tool_failure(
                        name,
                        self._ToolFailCause.BAD_PARAMS if _param_fail
                        else self._ToolFailCause.TOOL_ERROR,
                        tool_error=result_text if _param_fail else "",
                    )

                # OS dsl_plan 检测：Skill 返回了 OS 执行计划，需要转入 OS 执行循环
                _os_plan = (tool_data or {}).get("dsl_plan") if isinstance(tool_data, dict) else None
                if isinstance(_os_plan, list):
                    # 注入特殊标记，ReAct 主循环检测到后转路由
                    result_text = f"__OS_DSL_PLAN__{name}"
                    tool_data = {"dsl_plan": _os_plan, "_skill_name": name, "_skill_args": args}

                self._session_log_append(f"[Skill call] {name}")
                self._wm_add(
                    EntryType.SKILL_CALL, name, "call",
                    detail="", tags=["skill", "call", name],
                )
                self._last_called_skill = name

            ok = not _is_failed
            error = (f"Tool \"{name}\" failed: {result_text[:100]}" if _is_failed else "")

        except Exception as e:
            ok = False
            error = str(e).replace(chr(10), " ")
            result_text = f"Tool \"{name}\" execution error: {error}"
            raw_result = None
            tool_data = None
            self._last_skill_error = {
                "skill": name,
                "error": result_text,
                "consumed": False,
            }
            logger.error(f"[ReAct] 工具【{name}】执行异常: {error}")

        await event_queue.put({
            "event": "tool_end",
            "action_id": aid,
            "result_summary": result_text[:80],
            "status": "CORE_THINKING",
            "model": used_model,
            "current_skill": name,
            "tool_use_id": call.tool_use_id,
            "ok": ok,
        })

        # ⭐⭐ 挂起期证据日志 —— 挂在**工具结果前面**交给模型。
        #
        # ⚠️ 位置刻意在 `tool_end` 事件**之后**：那个事件的 `result_summary` 取
        #    `result_text[:80]`，放前面的话卡片摘要会变成日志开头而不是工具结果。
        #    📌 **给模型看的内容和给用户看的摘要，不该是同一个字符串。**
        #
        # ⚠️ 取完即清：它是一次性的，下一个工具不该再看到同一段。
        _pn = getattr(self, "_a3_pause_note", "")
        if _pn:
            self._a3_pause_note = ""
            result_text = f"{_pn}\n\n---\n{result_text}"

        return ToolExecution(
            call=call,
            result_text=result_text,
            ok=ok,
            raw_result=raw_result,
            tool_data=tool_data,
            error=error,
        )

    async def _execute_tool_batch(
        self,
        calls: list[ToolCall],
        *,
        used_model: str,
        base_guide: str,
        system_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
        active_tool_names: frozenset | None = None,
    ) -> list[ToolExecution]:
        """执行一批工具调用（Claude 单轮决策返回的所有 tool_use）。

        策略：
        - 按 Claude 原始顺序遍历，遇到连续的 parallel 工具段则并发执行，
          遇到 serial 工具时先 flush 前面的 parallel 段再串行执行。
          这样保证执行语义顺序与 Claude 意图一致。
        - 超出 MAX_TOOLS_PER_ROUND 的工具不执行，但必须生成 error tool_result，
          确保 tool_use / tool_result 数量完全对应（Anthropic API 要求）。
        - 结果按原始顺序排列。
        """
        original_calls = list(calls)
        executable_calls = original_calls[:self.MAX_TOOLS_PER_ROUND]
        skipped_calls = original_calls[self.MAX_TOOLS_PER_ROUND:]

        results_by_key: dict[str, ToolExecution] = {}

        def _key(c: ToolCall) -> str:
            return c.tool_use_id or f"tool_{c.name}_{c.index}"

        # 按原始顺序、分 parallel 段并发执行
        parallel_segment: list[ToolCall] = []

        async def flush_parallel():
            nonlocal parallel_segment
            if not parallel_segment:
                return
            tasks = [
                asyncio.create_task(self._execute_one_tool_call(
                    c, used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue, active_tool_names=active_tool_names,
                ))
                for c in parallel_segment
            ]
            for coro in asyncio.as_completed(tasks):
                ex = await coro
                results_by_key[_key(ex.call)] = ex
            parallel_segment = []

        # 并发资格问目录，不再问 `_classify_tool_safety` 那个三值返回。
        #
        # 🔴 那个函数把**两个正交维度压成了一个返回值**：
        #      if EXIT:  return "exit"     ← 先命中就 return
        #      if SERIAL: return "serial"  ← 于是这一行永远走不到
        #    结果是 5 个工具（create_new_skill / update_existing_skill /
        #    manage_existing_skill / WriteSkill / answer_open_interaction）
        #    **同时**登记在 SERIAL 和 EXIT 两张表里，而它们声明的 serial
        #    **永远不生效、且不报错**。
        # 📌 那不是"将来会漏"，是**当时就有一半声明是死的** —— 的直接动机之一。
        #
        # ⭐ 现在 `flow` 答「调用之后去哪」、`scheduling` 答「能不能和别人同批」，
        #    两个维度各自独立。exit 工具的真实调度语义是 **EXCLUSIVE**
        #    （与任何其他工具同轮 → 整批一个都不执行、全部写 error、下轮重选），
        #    **不是** SERIAL —— 写 SERIAL 就是又造一个新的死声明。
        # ⚠️ 走到这里的一定不是 exit 工具（`_run_react_loop` 在更早就摘走了），
        #    所以这里只需要分「能不能并发」：PARALLEL 进并发段，其余串行。
        #    找不到声明的（幻觉名字）落串行 —— 与改造前的兜底同向。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        for call in executable_calls:
            _d = _cat.get(call.name)
            if _d is not None and _d.scheduling is Scheduling.PARALLEL:
                parallel_segment.append(call)
            else:
                await flush_parallel()
                ex = await self._execute_one_tool_call(
                    call, used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue, active_tool_names=active_tool_names,
                )
                results_by_key[_key(ex.call)] = ex

        await flush_parallel()

        # 超限工具必须生成 error tool_result，否则 tool_use/tool_result 不对称
        for c in skipped_calls:
            results_by_key[_key(c)] = ToolExecution(
                call=c,
                result_text=(
                    f"Tool \"{c.name}\" was not executed: the model requested {len(original_calls)} tools this round, "
                    f"which exceeds the per-round system limit of {self.MAX_TOOLS_PER_ROUND}. "
                    "In the next round, decide whether to continue calling tools based on the returned results."
                ),
                ok=False,
                error="tool limit exceeded",
            )

        # 按 Claude 原始顺序排列
        ordered = []
        for call in original_calls:
            k = _key(call)
            ex = results_by_key.get(k) or ToolExecution(
                call=call, result_text="Tool execution result was missing.", ok=False, error="result missing"
            )
            ordered.append(ex)

        return ordered

    def _react_loop_prompt(self) -> str:
        """ReAct protocol injected into system_guide."""
        return (
            "\n\n[ReAct Tool Use Protocol]\n"
            "After each reasoning step, choose one of:\n"
            "1. Answer the user directly.\n"
            "2. Call one tool.\n"
            "3. Call multiple independent tools in the same round.\n\n"

            "When NOT to use tools (important):\n"
            "- For casual conversation, greetings, opinions, explanations, advice, planning, or reasoning about "
            "content already in the conversation, just answer directly (option 1). Do not call any tool.\n"
            "- Only reach for a tool when the task genuinely needs real execution: file access, computer/screen/"
            "browser control, live data (time/weather/news/etc.), memory recall/write, Skills, attachments, waiting, or scheduling.\n"
            "- Do not call memory, knowledge, or note tools just to respond to small talk.\n\n"

            "Dependency rule:\n"
            "- If tool B needs the result of tool A, split them across rounds: call A first, wait for the result, then decide whether to call B.\n"
            "- If tools are independent parallel lookups, reads, or calculations, they may be called in the same round.\n"
            "- After every tool result, reason from the result before calling another tool or giving the final answer.\n\n"

            "Tool boundary discipline:\n"
            "- load_tools is the default way to confirm or activate capabilities that are not currently in the active tool schema.\n"
            "- Preloaded tools and router tool_names are hints, not commands.\n"
            "- Never invent tool names, action names, parameter names, or enum values.\n"
            "- If you are unsure about a tool name, action, parameter, or enum value, call load_tools first instead of guessing.\n"
            "- os_execute may only use actions listed in its schema. set_window_mode and look_at_screen are standalone tools, not os_execute actions.\n"
            "- If a tool call fails due to unknown action, tool not found, invalid enum, unexpected parameter, or missing required parameter, "
            "the next step should inspect/load the correct schema, not guess another name.\n"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 用户手动终止 —— **和「无缝对话的中断」不是同一件事**
    # ══════════════════════════════════════════════════════════════════════
    # 🔴 **早先写过「终止按钮不单独修，等后台任务那一项做完自然消失」——那条是错的。**
    #
    # 📌 **两个机制的出口方向相反**：
    #    · 无缝对话的中断：出口是「**带着新输入继续决策**」→ 它还会接着做事
    #    · 终止：出口是「**停下，别做了**」→ 它不该接着做事
    #    做完前者**不会**自动得到后者 —— 它们答的不是同一个问题。
    #    ⚠️ 这正是本轮反复出现的那个形状：
    #       **拿一个为别的目的定义的机制，去回答一个它答不了的问题。**
    #
    # 三处差别都是硬的：
    #    | | 无缝中断 | 终止 |
    #    |---|---|---|
    #    | 触发 | 用户**打字** | **零输入**（空输入框点一下）|
    #    | 判定 | 过**模型**（它决定怎么办）| **确定性**，不过模型 |
    #    | 出口 | 继续 | 停止 |
    #
    # ⭐⭐ **决定性的论据**：如果想让它停，而唯一的办法是打字告诉它「停」，
    #    那么**模型可能不停** —— 它可能理解成别的意思、或者觉得该先收个尾。
    #    📌 **一个「停止」能力如果依赖被停止的那一方理解你的意思，
    #       它就不是停止能力。**
    #
    # ⭐ 好消息：**检查点已经在正确的位置**（都在「这一轮还没往历史里写任何东西」
    #    之前），所以终止复用它们**零额外风险**。

    def request_stop(self, source: str = "user") -> None:
        """用户点了终止。**只置一个意图，不做任何清理** ——
        真正的停止发生在最近的那个检查点上。

        ⚠️ 为什么不在这里直接动手：**动作与动作之间**才是安全的停止边界
        （早先已认过的物理限制：已经在执行中的那一个动作停不下来）。
        📌 在这里硬砍，就会重新引入悬空的调用记录 → 400。
        """
        self._stop_requested = True
        logger.info(f"[Stop] 收到终止请求（{source}）—— 将在下一个动作边界停下")

    def clear_stop(self) -> None:
        """新一轮开始时清掉。⚠️ **必须清** —— 否则上一次的终止会把下一轮也杀掉。"""
        self._stop_requested = False

    def _stop_asked(self) -> bool:
        return bool(getattr(self, "_stop_requested", False))

    def _user_interjected(self) -> bool:
        """用户在本轮进行中又说话了吗？

        ⚠️ **fail-safe 方向是「没有」** —— 读不出来就当没插话，让本轮正常跑完。
           反过来错的代价是**无故中断一个好好的回答**，那比晚一点响应糟得多。
           📌 与 `inbox.has_work()` 相反（那边错成 True 只是白检查一次），
              **方向由代价决定，不由习惯决定。**
        """
        base = getattr(self, "_turn_input_baseline", None)
        if base is None:
            return False
        try:
            from core.runtime import inbox as _ib_seq
            return _ib_seq.submit_seq() > int(base)
        except Exception:
            return False

    def _interject_stop_event(self, round_idx: int, where: str,
                              stopped: bool = False) -> dict:
        """构造「本轮被用户插话中断」这个事件。

        ⚠️⚠️ **这里刻意不做任何补偿动作，因为不需要** ——
        两个检查点都放在「**这一轮还没往历史里写任何东西**」的位置：
        `stream` 阶段一个字都不写进 memory（写点只有空决策 / 文字答案 /
        `add_tool_calls` 三处），而检查点 B 就在 `add_tool_calls` **之前**。
        📌 **中断点要选在「还没写进历史」的位置 —— 那样根本不需要任何补偿逻辑。**
           选在写入之后就得发明一套收尾（补 `tool_result`、防悬空 `tool_use` 的 400）；
           选在写入之前，那套收尾**根本不存在**。
        ⭐ 所以 用户那个例子里 `GetSystemTime` **压根没被执行** ——
           模型带着两条消息重新决策时，它不会自相矛盾。
        """
        # ⚠️ **两种原因必须分开**，不许压成一个「被中断了」：
        #    · 插话 → 队列里有东西，马上带着新输入重新决策（**还会继续**）
        #    · 终止 → 用户要它**停**，不该有下一段
        #    📌 出口方向相反的两件事，不能共用一个 reason ——
        #       下游要据此决定「接着跑」还是「收尾」。
        if stopped:
            logger.info(f"[Stop] 第{round_idx + 1}轮在「{where}」被用户终止 —— "
                        f"本轮不写历史、不执行工具，且**不接着跑**")
        else:
            logger.info(f"[Interject] 第{round_idx + 1}轮在「{where}」被用户插话中断 —— "
                        f"本轮不写历史、不执行工具，交给下一轮带着两条消息重新决策")
        return {
            "event": "turn_interrupted",
            "reason": "user_stopped" if stopped else "user_interjected",
            "stopped": bool(stopped),
            "where": where,
            "round": round_idx + 1,
            "status": "SYS_IDLE",
            "log": (f"用户终止 → 第{round_idx + 1}轮停止（{where}）" if stopped
                    else f"用户插话 → 第{round_idx + 1}轮中断（{where}）"),
            "current_skill": None,
        }

    async def _run_react_loop(
        self,
        *,
        tools_manifest: list,
        system_guide: str,
        base_guide: str,
        realtime_callback,
        event_queue: asyncio.Queue,
    ):
        """ReAct 主循环。替代 _handle_query_impl 常规路径里的"一次决策 + 巨型分支树"。

        循环结构：
          思考(stream_decision) → 工具行动(批量执行) → 结果写回 memory → 继续思考
          直到模型输出文字答案或超过最大轮数。
        """
        used_model = getattr(self, "target_model", "UNKNOWN")
        tool_data_out = None
        # ReAct 协议是稳定文本，插到 CACHE_BREAK_MARKER 前，避免落入动态区导致缓存失效。
        react_guide = self._insert_before_cache_break(system_guide, self._react_loop_prompt())
        self._pending_loaded_manifests = []   # 按需加载：本轮 load_tools 待并入的 schema

        for round_idx in range(self.REACT_MAX_ROUNDS):
            # ⭐⭐ [协作式中断 · 检查点 A] 动作边界：上一轮的工具往返都已闭合，
            #    历史是干净的 —— 直接停，什么都不用补。
            #    ⚠️ 这一处覆盖的是「ReAct 中途插话」（用户问的那个更复杂情况）。
            if self._stop_asked():
                yield self._interject_stop_event(round_idx, "轮开头", stopped=True)
                return
            if round_idx > 0 and self._user_interjected():
                yield self._interject_stop_event(round_idx, "轮开头")
                return

            # ── 1. 模型思考/决策 ─────────────────────────────────────────
            state = StreamDecisionState()
            context = self._build_pipeline_context()

            try:
                async for ev in self._stream_decision_core(
                    context, tools_manifest, react_guide,
                    task_type="ReAct",
                    stage_label=f"ReAct第{round_idx + 1}轮",
                    state=state,
                    emit_done=True,
                    allow_multi_tools=True,
                ):
                    yield ev
                    # ⭐⭐ [检查点 0] **stream 进行中**也要看 —— 否则模型正在生成
                    #    一段长回答时，点终止要等它写完，那不叫终止。
                    #    ⚠️ 中途 break 是**安全**的，理由和另两个检查点完全一样：
                    #       **stream 阶段一个字都不写进历史**（写点只有空决策 /
                    #       文字答案 / `add_tool_calls` 三处，全在 stream 之后）。
                    #       📌 中断点选在「还没写进历史」的位置 → 不需要补偿逻辑。
                    #    ⭐ 插话也走这一条：没必要等一个**已经不作数**的 stream 跑完
                    #       —— 那纯粹是白烧 token 和时间。
                    if self._stop_asked() or self._user_interjected():
                        _st6 = self._stop_asked()
                        yield self._interject_stop_event(
                            round_idx, "模型生成中", stopped=_st6)
                        return
            except RuntimeError as fatal_err:
                if "API 调用链路全线熔断" in str(fatal_err):
                    self._clean_damaged_memory()
                    yield {"event": "sys_error", "content": "🚨【熔断】API全线枯竭！",
                           "status": "CRITICAL_SYSTEM_HALT", "model": "TOTAL_CRASH",
                           "skills_active": False, "log": "CRITICAL: API链路彻底枯竭",
                           "current_skill": None}
                    return
                raise

            decision = state.decision
            used_model = state.model or used_model

            if decision is None:
                content = "这轮模型没有返回有效决策。"
                self.memory.add_message("assistant", content)
                yield {"event": "final_result", "content": content, "model": used_model,
                       "status": "SYS_IDLE", "log": "ReAct loop：空决策终止。",
                       "current_skill": None, "rag_hit": self._rag_hit_this_turn,
                       "full_file_hit": self._full_file_hit_this_turn}
                return

            # ── 1.5. provider_error：400/异常不当普通回答，清理 memory 并终止 ──
            if decision.decision_type == "provider_error":
                status_code = getattr(decision, "error_status_code", 0)
                if status_code == 400:
                    # 覆盖表第 3 行。⭐ 顺便验预判 B：
                    # provider 调用在【轮首思考阶段】、batch 在【轮中】，所以 400 发生时
                    # 上一轮 batch 早已关闭（置 False 在 raise 之前）→ 这个守卫可能【几乎恒真】，
                    # 即等于没有守卫。先用 shadow 拿证据，不要凭推断提前删它（[L4] 同款纪律）。
                    _rt_shadow_note(_RT_PATH.ERROR_400,
                                    f"open_batch={self._active_tool_batch_open}"
                                    f"（shadow 已证实这里几乎恒为 False，见 [F1] 预判 B）")
                    if not self._active_tool_batch_open:
                        # 当前没有进行中的工具批次，说明是历史坏 pair 导致的 400
                        # 尝试修复历史内存，而不只是清理当前批次
                        repaired = self.memory.repair_invalid_tool_turns()
                        if repaired:
                            logger.warning("[ReAct] 历史坏 tool pair 已修复，下次请求应可恢复。")
                    self._clean_damaged_memory()
                logger.error(f"[ReAct] provider_error status={status_code}: {decision.content}")
                yield {"event": "sys_error", "content": decision.content,
                       "status": "ERROR", "model": used_model,
                       "log": f"provider_error {status_code}",
                       "current_skill": None}
                return

            # ── 2. 文字答案：ReAct 结束 ──────────────────────────────────
            if decision.decision_type in ("text", "web_text") and not decision.tool_calls:
                final_content = decision.content or ""
                self.memory.add_message("assistant", final_content)
                yield {
                    "event": "final_result",
                    "content": final_content,
                    "model": used_model,
                    "status": "SYS_IDLE",
                    "log": f"ReAct loop：完成（第{round_idx + 1}轮）。",
                    "current_skill": None,
                    "rag_hit": self._rag_hit_this_turn,
                    "full_file_hit": self._full_file_hit_this_turn,
                    "tool_data": tool_data_out,
                    "block_id": getattr(decision, "answer_bid", None),
                }
                return

            # ── 3. 工具调用 ──────────────────────────────────────────────
            if decision.is_tool_action:
                # ⭐⭐⭐ [协作式中断 · 检查点 B] **这一处才是 用户那个例子的正解。**
                #
                #    模型已经决定要调工具（`tool_use` 已在 decision 里），
                #    但**还没写进历史、更没执行**。此刻停 → 那次调用彻底不存在。
                #    ⚠️ 位置极其关键：必须在 `add_tool_calls` **之前** ——
                #       `tool_use` 只通过它进历史，所以放在前面就
                #       **不会留下悬空 `tool_use`，零 400 风险**。
                #    📌 **中断点要选在「还没写进历史」的位置** ——
                #       那样根本不需要补偿逻辑（补 `tool_result` 那套压根不存在）。
                #
                #    ⭐ 于是「现在几点？→（正要调 GetSystemTime）→ 算了别调了」
                #      的结果是：**工具没跑**，模型带着两条消息重新决策，不自相矛盾。
                if self._stop_asked():
                    yield self._interject_stop_event(round_idx, "工具执行前",
                                                     stopped=True)
                    return
                if self._user_interjected():
                    yield self._interject_stop_event(round_idx, "工具执行前")
                    return
                calls = decision.tool_calls

                # 这一轮的目录与运行时事实 —— exit 判定、handler 解析都问它。
                _cat = self._get_tool_catalog()
                _rtv = self._tool_runtime_view()

                # 检查是否有需要退出 ReAct 循环的特殊工具。
                # ⭐ 判据从「名字在不在 `_REACT_EXIT_TOOLS` 这张手写表里」换成
                #    「这个工具声明的 `flow` 是不是 EXIT_REACT」——
                #    同一个问题，问的是唯一权威。
                # ⚠️ 用 `resolve(...) is not None` 一起把「此刻 eligible」也验了，
                #    否则可能挑出一个当前不该出现、随后又解析不到 handler 的工具
                #    （那正是 「看得见执行不了」的形态）。
                def _is_exit_call(c) -> bool:
                    _d = _cat.get(c.name)
                    return (_d is not None and _d.flow is Flow.EXIT_REACT
                            and _cat.resolve(c.name, ToolScope.MAIN, _rtv) is not None)

                exit_call = next((c for c in calls if _is_exit_call(c)), None)
                if exit_call:
                    if len(calls) > 1:
                        # exit 工具和其他工具同轮出现：全部写入 memory + error result，让 Claude 下轮重来
                        #
                        # ⚠️ 注意这一段【从头到尾没有碰过 _active_tool_batch_open】。
                        # 但它确实开了又关了一个真实的工具批次（add_tool_calls + add_tool_results）。
                        # 这是 shadow 覆盖表第 5 行，也是我们开工前就预判"必然分歧、且旧实现输"的那条。
                        #  这是开工前就预判过的一条。
                        _sp = _rt_shadow_prepare(
                            self, round_idx, [c.name for c in calls], _RT_PATH.EXIT_MULTI)
                        norm_calls = self.memory.add_tool_calls(calls, thinking_blocks=decision.thinking_blocks)
                        _rt_shadow_open(self, _sp, [c.tool_use_id for c in norm_calls])
                        err_msg = (
                            f"This round contains an exit-flow tool \"{exit_call.name}\". "
                            "It cannot be executed in the same round with other tools. "
                            "In the next round, call only that exit-flow tool, or finish other read-only checks first and call it separately."
                        )
                        self.memory.add_tool_results([
                            ToolResultBlock(
                                name=c.name,
                                tool_use_id=c.tool_use_id,
                                content=err_msg,
                                is_error=True,
                            )
                            for c in norm_calls
                        ])
                        _rt_shadow_commit(self, _sp, [c.tool_use_id for c in norm_calls],
                                          _RT_PATH.EXIT_MULTI)
                        ok_fmt, why_fmt = self.memory.validate_tool_turns()
                        if not ok_fmt:
                            self._rollback_last_tool_batch()
                            raise RuntimeError(f"ReAct exit-multi memory 格式损坏：{why_fmt}")
                        continue  # 让 Claude 下轮重新决策

                    # ⭐ [2026-08-06 实测] exit 工具也要发工具卡片。
                    #
                    # 用户两次报"创建 Skill 时没有工具卡片"。⑥ 删掉关键词快路径之后
                    # 流程确实走主循环了（日志可证），但卡片**仍然没出现** ——
                    # 因为 exit 工具**从设计上就不经过 `_execute_tool_batch`**：
                    # 它们是路由信号，在批次执行【之前】就被摘出来分派了，
                    # 而 `tool_start` / `tool_end` 是在批次执行里发的。
                    #
                    # 所以之前看到的 `加载能力: create_new_skill ✓` 是 **load_tools 的卡片**
                    # （它是普通工具），不是 `create_new_skill` 自己的。
                    #
                    # 📌 判据：**"绕过主流程"必然连带绕过主流程的可观测性。**
                    # 这和 ⑥ 那条快路径是同一个形状 —— 只不过这次绕过的是
                    # 批次执行而不是整个循环。设计原则 4 要求新工具能被卡片显示，
                    # 那就不能有一整类工具天然不发卡片。
                    #
                    # ⚠️⚠️ 必须 `yield`，**绝对不能 `event_queue.put`**。
                    #
                    # 第一版就是写成 `await event_queue.put(...)`，实测
                    # （17:56:48 那轮）**完全没有卡片**，
                    # 而且没有任何异常 —— put 成功了，只是没人取。
                    #
                    # 📌 根因：`event_queue` 的排空循环在**工具批次那一段里**
                    #    （见下方"并发执行工具，同时 drain event_queue"）。
                    #    exit 工具在到达那段之前就路由走并退出循环了，
                    #    于是事件永远躺在队列里。
                    #
                    # 也就是说 —— 「exit 工具绕过批次执行」这件事，
                    # **连带绕过的不只是发卡片，还有取卡片**。第一次只修了发的那半。
                    #
                    # 判据：`event_queue.put` 只在"调用方保证会 drain"的地方成立；
                    #      在生成器自己手里，`yield` 才是无条件到达 UI 的那条路。
                    def _drain_queue():
                        # 实现已提成 `_drain_event_queue`，因为
                        # exit 适配器（`_exit_*`）也要用它 —— 一个闭包拿不出去。
                        return self._drain_event_queue(event_queue)

                    # 先把本轮 LLM 调用期间攒下的事件放出去，再发 exit 卡片，
                    # 否则状态行的顺序会倒过来（卡片先于"模型已就绪"）。
                    for _q in _drain_queue():
                        yield _q

                    _exit_aid = f"exit_{exit_call.tool_use_id or exit_call.name}"
                    yield {
                        "event": "tool_start", "action_id": _exit_aid,
                        "tool_name": exit_call.name,
                        "tool_use_id": getattr(exit_call, "tool_use_id", "") or "",
                        "action_display": _cat.presentation(
                            exit_call.name, exit_call.args or {}),
                        "action_input": str(exit_call.args or {})[:200],
                        "log": f"ReAct: 进入专属流程【{exit_call.name}】",
                        "status": "TOOL_EXECUTING", "model": used_model,
                        "current_skill": exit_call.name,
                        "tool_use_id": exit_call.tool_use_id,
                    }
                    # ⚠️ `tool_end` **不在这里发** —— 见下方 `return` 之前那处。
                    #
                    # 第一版紧挨着 `tool_start` 就发了，结果卡片显示
                    # `[✓] 1 tool · 0.0s`（2026-08-06 当场看出来）。
                    # exit 工具真正的"耗时"是它路由过去之后那整段流程
                    #（探索、写代码、重新生成…），不是路由本身那一瞬。
                    # 📌 一个耗时显示成 0.0s 的卡片，比没有卡片更误导。

                    # ⭐⭐ exit 流程里有些分支不该自己开口。
                    #
                    # 2026-08-06 指出：`_handle_answer_interaction` 的
                    # 拒绝分支直接 `yield final_result` 一句写死的中文。
                    # 那句话**完全不受人格模板影响** —— 用户把 Nano 的人设改成别的，
                    # 满屏都是那个语气，中间突然蹦出一句
                    #「批准要对得上具体哪一版」，等于人格分裂。
                    # 项目里这类固定文案有 **35 处**（已清点，）。
                    #
                    # 机制：handler 想让模型自己说时，yield 一个
                    # `exit_flow_defer_to_model` 事件（带一段**英文事实描述**），
                    # 这里把它拦下来 —— 不给 UI，而是写成 tool_result 喂回记忆，
                    # 然后 `continue` 让 ReAct 循环再跑一轮，由模型用它自己的话说。
                    #
                    # 📌 这与 是同一条：**给出足够的事实，让它自己判断怎么说、
                    #    下一步做什么**，而不是替它把话说死。
                    _defer_to_model: str | None = None

                    # ⭐⭐ [2026-08-06 实测] `tool_end` 必须赶在**终端事件之前**发出。
                    #
                    # 上一版把它挪到"专属流程跑完之后"，想让时长真实。结果三个症状：
                    #   · pill 永远停在运行态（金黄 + 没有秒数，和正常卡片长得不一样）
                    #   · 明细行的转圈**永不停止**
                    #   · 收起后也不出对勾/叉子
                    #
                    # 📌 根因在消费端：`app.py` 里
                    #      `if step.get("event") == "final_result": … return`
                    #    —— 子流程（探索 / 部署 / 重新生成）自己就会 yield 终端事件
                    #    结束这一轮，消费者当场 return，**排在它后面的任何事件都进不了 UI**。
                    #
                    # 判据：**生成器 yield 出去 ≠ 消费者会处理。**
                    #   终端事件是一条单向门 —— 收尾类事件必须在门关上之前发。
                    _tool_end_sent = False

                    def _mk_tool_end():
                        nonlocal _tool_end_sent
                        _tool_end_sent = True
                        return {
                            "event": "tool_end", "action_id": _exit_aid,
                            "result_summary": "已完成" if not _defer_to_model else "需要我来说明",
                            "status": "CORE_THINKING", "model": used_model,
                            "current_skill": exit_call.name,
                            "tool_use_id": exit_call.tool_use_id,
                            "ok": _defer_to_model is None,
                        }

                    def _pre(ev):
                        """要在 `ev` 之前补发的事件。`ev` 是终端事件时补 tool_end。

                        ⚠️ 终端事件有**五个**，见 `_UI_TERMINAL_EVENTS`。
                        漏掉哪个，那条路径的卡片就会永远停在转圈态。
                        """
                        if (not _tool_end_sent and isinstance(ev, dict)
                                and ev.get("event") in _UI_TERMINAL_EVENTS):
                            return [_mk_tool_end()]
                        return []

                    def _take_defer(ev):
                        """返回 True 表示这个事件被拦下（不转发给 UI）。"""
                        nonlocal _defer_to_model
                        if isinstance(ev, dict) and ev.get("event") == "exit_flow_defer_to_model":
                            _defer_to_model = str(ev.get("tool_result") or "").strip() or None
                            return True
                        return False

                    # ⭐⭐ 只有 exit 工具：**问目录谁来处理**，然后退出循环。
                    #
                    # 🔴 这里改造前是 `if exit_call.name == "update_existing_skill":
                    #    elif … elif …` 五段分派 —— 那是**第二份按工具名建立的事实**，
                    #    正是 要消灭的东西（的 AST 断言现在守着它不许回来）。
                    #    每一支各自那点专属逻辑已经**逐字**搬进 `_exit_*` 适配器，
                    #    行为零变化；这里只剩「谁处理」这一个问题，而它由 Catalog 回答。
                    #
                    # ⚠️ 公共 plumbing 一行没动，仍在这里：`_take_defer` 拦截
                    #    `exit_flow_defer_to_model`、`_pre` 在终端事件前补 `tool_end`、
                    #    以及下面那段 defer 的 memory 三步写入。
                    #    📌 **Catalog 决定"谁处理"，Runner 决定"怎么运行"** —— 分层没变。
                    _exit_ref = _cat.resolve(exit_call.name, ToolScope.MAIN, _rtv)
                    if _exit_ref is None:
                        # 走不到：`exit_call` 本来就是从「flow 是 EXIT_REACT 且此刻
                        # eligible」里挑出来的。留一条响亮日志，别静默什么都不做 ——
                        # 静默的表现是「模型调了工具，然后什么都没发生」。
                        logger.error(
                            f"[F4] exit 工具 {exit_call.name!r} 解析不到 handler —— "
                            f"这不该发生（挑它出来的判据和这里是同一个）")
                    else:
                        async for _ev in getattr(self, _exit_ref)(
                            exit_call, decision,
                            used_model=used_model, base_guide=base_guide,
                            realtime_callback=realtime_callback,
                            event_queue=event_queue,
                        ):
                            # ⚠️ `_take_defer` 改造前**只挂在 answer_open_interaction
                            #    那一支**上。这里统一挂 —— 可证等价：全项目只有
                            #    `_handle_answer_interaction` 会 yield 这个事件
                            #    （已 grep 核实，唯一 emitter）。
                            if _take_defer(_ev):
                                continue          # 不转发给 UI，交给模型去说
                            for _e in _pre(_ev):
                                yield _e
                            yield _ev

                    # 流程没走终端事件就结束（比如 defer 那条）→ 这里补上收尾。
                    if not _tool_end_sent:
                        yield _mk_tool_end()

                    if _defer_to_model:
                        # handler 把事实交回来了 → 写成 tool_result，让模型自己组织语言。
                        # ⚠️ 记忆写法必须和 EXIT_MULTI 那段一致（同样的 shadow 三步 +
                        #    格式自检），否则下一轮直接 400：assistant 的 tool_use
                        #    必须紧跟成对的 tool_result。
                        _sp = _rt_shadow_prepare(
                            self, round_idx, [exit_call.name], _RT_PATH.EXIT_MULTI)
                        _norm = self.memory.add_tool_calls(
                            [exit_call], thinking_blocks=decision.thinking_blocks)
                        _rt_shadow_open(self, _sp, [c.tool_use_id for c in _norm])
                        self.memory.add_tool_results([
                            ToolResultBlock(name=c.name, tool_use_id=c.tool_use_id,
                                            content=_defer_to_model, is_error=False)
                            for c in _norm
                        ])
                        _rt_shadow_commit(self, _sp, [c.tool_use_id for c in _norm],
                                          _RT_PATH.EXIT_MULTI)
                        _ok_fmt, _why_fmt = self.memory.validate_tool_turns()
                        if not _ok_fmt:
                            self._rollback_last_tool_batch()
                            raise RuntimeError(f"ReAct exit-defer memory 格式损坏：{_why_fmt}")
                        logger.info(
                            f"[ReAct] exit 流程把措辞交回模型（{exit_call.name}）："
                            f"{_defer_to_model[:80]}"
                        )
                        continue      # ⭐ 不 return —— 让模型用它自己的话回这一轮
                    return

                # 写 assistant turn，拿回归一化后的 calls（id 已补全）
                # shadow：Span 在 memory 写入【之前】登记 PREPARED，
                # 写完之后 CAS 到 OPEN —— 这一档的意义就是区分"崩在写之前"和"崩在写之后"。
                _sp = _rt_shadow_prepare(self, round_idx, [c.name for c in calls], _RT_PATH.NORMAL)
                calls = self.memory.add_tool_calls(calls, thinking_blocks=decision.thinking_blocks)
                # 不再置旧字段 —— `_rt_shadow_open` 已经把 Span 切到 OPEN，
                # 而 `_active_tool_batch_open` 现在就是从那个状态派生的。
                _rt_shadow_open(self, _sp, [c.tool_use_id for c in calls])

                yield {
                    "event": "tool_batch_start",
                    "batch_id": f"batch_{round_idx}_{time.time_ns()}",
                    "count": len(calls),
                    "status": "TOOL_EXECUTING",
                    "model": used_model,
                    "log": f"ReAct第{round_idx + 1}轮：执行 {len(calls)} 个工具。",
                }

                # 并发执行工具，同时 drain event_queue
                batch_task = asyncio.create_task(self._execute_tool_batch(
                    calls,
                    used_model=used_model, base_guide=base_guide,
                    system_guide=system_guide, realtime_callback=realtime_callback,
                    event_queue=event_queue,
                    # ⭐ 把**产生这批调用的那次 request** 的 tool contract 带下去。
                    #    ⚠️ 取自 `decision`，不是重算 —— 见 `_stream_decision_core` 里
                    #       那段关于 `_pending_loaded_manifests` 的说明。
                    active_tool_names=getattr(decision, "active_tool_names", None),
                ))

                while not batch_task.done():
                    try:
                        ev = await asyncio.wait_for(event_queue.get(), timeout=0.05)
                        yield ev
                    except asyncio.TimeoutError:
                        pass

                # 排空队列残留事件
                while not event_queue.empty():
                    yield event_queue.get_nowait()

                executions: list[ToolExecution] = await batch_task

                # 按需加载：把本批 load_tools 匹配到的 schema 并入 tools_manifest，下一轮即可调用
                if self._pending_loaded_manifests:
                    _loaded_names = {(_m.get("name") or "") for _m in self._pending_loaded_manifests}
                    _have = {t.get("name") for t in tools_manifest}
                    for _m in self._pending_loaded_manifests:
                        if _m.get("name") not in _have:
                            tools_manifest.append(_m)
                            _have.add(_m.get("name"))
                    # OS 规范很长，不再每轮常驻；只有真正 load OS 能力后才注入。
                    if _loaded_names & {"os_execute", "look_at_screen", "set_window_mode"}:
                        react_guide = self._insert_before_cache_break(react_guide, _OS_CAPABILITY_PROMPT)
                    self._pending_loaded_manifests = []

                # 收集 tool_data
                batch_tool_data: dict = {}
                for ex in executions:
                    if ex.tool_data is not None:
                        batch_tool_data[ex.call.tool_use_id or ex.call.name] = ex.tool_data
                if batch_tool_data:
                    tool_data_out = batch_tool_data

                # 写 user turn 必须先于 OS plan 检测，确保 tool_calls/tool_results 始终成对
                tool_result_blocks = [
                    ToolResultBlock(
                        name=ex.call.name,
                        tool_use_id=ex.call.tool_use_id or f"tool_{ex.call.name}",
                        content=ex.result_text,
                        is_error=not ex.ok,
                        raw_result=ex.raw_result,
                        tool_data=ex.tool_data,
                    )
                    for ex in executions
                ]
                self.memory.add_tool_results(tool_result_blocks)
                # 同上：`_rt_shadow_commit` 把 Span 切到 COMMITTED，派生值随之变 False。
                _rt_shadow_commit(self, _sp, [b.tool_use_id for b in tool_result_blocks],
                                  _RT_PATH.NORMAL)

                # 格式校验：失败时必须回滚，否则下一轮 Anthropic 大概率 400
                ok_fmt, why_fmt = self.memory.validate_tool_turns()
                if not ok_fmt:
                    logger.error(f"[ReAct] memory 格式校验失败，回滚: {why_fmt}")
                    self._rollback_last_tool_batch()
                    # Span 已 COMMITTED（上面那行），这里补一条观测说明"内核认为一致、
                    # 而 memory 校验说不一致"——这正是不变量 9 该抓的东西。
                    _rt_shadow_note(_RT_PATH.NORMAL, f"validate_tool_turns 失败: {why_fmt}",
                                    diverged=True)
                    raise RuntimeError(f"ReAct memory 格式损坏，已回滚：{why_fmt}")

                # OS dsl_plan 检测（在 tool_results 已写入之后）
                os_plan_ex = next(
                    (ex for ex in executions if isinstance(ex.result_text, str)
                     and ex.result_text.startswith("__OS_DSL_PLAN__")),
                    None
                )
                if os_plan_ex and isinstance(os_plan_ex.tool_data, dict):
                    _plan_data = os_plan_ex.tool_data
                    _skill_name = _plan_data.get("_skill_name", "")
                    _os_plan = _plan_data.get("dsl_plan", [])
                    _skill_args = _plan_data.get("_skill_args", {})
                    from core.os_layer.dispatch import OSDispatcher
                    from core.os_layer.safety import OSSessionSafety
                    if not hasattr(self, "_os_safety"):
                        self._os_safety = OSSessionSafety()
                    _os_dispatcher = OSDispatcher(
                        session_id=getattr(self, "_session_id", ""),
                        m1_mode=False, m2_mode=True, m3_mode=True,
                        safety=self._os_safety, provider=self.provider,
                        vision_model_override=_vision_model_for_os(),
                    )
                    _rt_shadow_note(_RT_PATH.OS_EARLY_RETURN,
                                    f"skill={_skill_name} steps={len(_os_plan)}")
                    self._rt_os_lease = _rt_lease_acquire(self, "os_skill_plan")
                    try:
                        async for step in self._run_os_skill_plan_loop(
                            _skill_name, _os_plan, _skill_args,
                            _os_dispatcher, self._os_safety, used_model, base_guide, realtime_callback
                        ):
                            yield step
                    finally:
                        _rt_lease_release(self)
                    return

                yield {
                    "event": "tool_batch_end",
                    "count": len(executions),
                    "status": "CORE_THINKING",
                    "model": used_model,
                    "log": "工具结果已回填，继续 ReAct 思考。",
                }

                continue  # 进入下一轮思考

            # ── 4. 其他类型兜底 ──────────────────────────────────────────
            content = decision.content or f"无法处理的决策类型：{decision.decision_type}"
            self.memory.add_message("assistant", content)
            yield {
                "event": "final_result",
                "content": content,
                "model": used_model,
                "status": "SYS_IDLE",
                "log": "ReAct loop：兜底终止。",
                "current_skill": None,
                "rag_hit": self._rag_hit_this_turn,
                "full_file_hit": self._full_file_hit_this_turn,
            }
            return

        # 超过最大轮数
        # 走到这里说明循环正常结束，理论上没有活 span；兜底收一次，
        # 免得极端路径把它漏在 OPEN 让下一轮误报。幂等，无副作用。
        _rt_abort_open_span(self, "LOOP_EXHAUSTED", _RT_PATH.GENERATOR_DROPPED)
        async for _ev in self._final_answer_or_fallback(
                facts=("The loop hit its round limit after "
                       f"{self.REACT_MAX_ROUNDS} rounds of thinking and tool "
                       "calls, so it stopped there rather than keep spinning. "
                       "Whatever was in progress is unfinished. Tell the user "
                       "that, and suggest narrowing the request or splitting it."),
                # ⚠️ 兜底保留中文原文 —— 模型不可用时才登场（豁免④）
                fallback=f"已连续思考/调用工具 {self.REACT_MAX_ROUNDS} 轮，为避免陷入循环，先停在这里。",
                base_guide=base_guide, used_model=used_model,
                log=f"ReAct loop：达到最大轮数 {self.REACT_MAX_ROUNDS}。",
                extra={"current_skill": None, "rag_hit": self._rag_hit_this_turn, "full_file_hit": self._full_file_hit_this_turn}):
            yield _ev

    async def _stream_final_answer(self, context, system_guide, *, task_type: str):
        """统一的"流式最终答案"调用 helper——跟 _stream_decision 同一类设计，
        所有"不调工具、直接生成最终回答（结果总结/失败转写等收尾文案）"的调用点
        都走这个，不再各自手写"调 chat_without_tools 拿完整字符串"。

        跟思考流的 _stream_decision 不同：这里不需要 think protocol 注入（最终
        答案本来就不需要思考标签），直接复用 chat_without_tools_stream 的
        text_delta/done 两段式事件，翻译成前端的 final_text_start/
        final_text_delta，结果通过实例属性回传：
          self._last_stream_final_text  : str        完整最终文本
          self._last_stream_final_model : str
          self._last_stream_final_bid   : str | None  有内容到达才会有 bid；
            空回答（从未触发任何 delta）时是 None，调用方据此判断——app.py
            那边 final_result 事件如果带的 block_id 是 None，会走老的
            "没有流式内容，直接整段渲染"分支，不强求每个调用点都必须流式化。
        """
        bid = f"final_{time.time_ns()}"
        started = False
        full_text = ""
        used_model = self.target_model if hasattr(self, "target_model") else None
        async for _ev in self.provider.chat_without_tools_stream(
            context, system_guide, model_override=None,
        ):
            if _ev["type"] == "text_delta":
                if not started:
                    started = True
                    yield {"event": "final_text_start", "block_id": bid}
                yield {"event": "final_text_delta", "block_id": bid, "delta": _ev["text"]}
            elif _ev["type"] == "done":
                full_text = _ev["text"]
                used_model = _ev["model"]
        self._last_stream_final_text = full_text
        self._last_stream_final_model = used_model
        self._last_stream_final_bid = bid if started else None


    # ── 主流程 ───────────────────────────────────────────────────────────

    async def handle_query(self, query: str, image_parts: list | None = None, temp_file_hint: str | None = None):
        """公开入口：统一思考块（per-turn 单块）+ 行动卡片 action_id 透传。
        内部委托给 _handle_query_impl，通过 generator 过滤层处理：
        - 所有 thought_block_start/delta/done/summary 统一到同一 block_id
        - 重复 thought_block_start 静默丢弃（同一 turn 只开一次 UI 块）
        - thought_block_done/summary 缓冲到 terminal 事件之前才 flush
          （思考块视觉上一直开着直到答案出来前才收尾，类 Claude Code 感觉）
        """
        _TERMINAL = frozenset({"final_result", "user_note_pending", "skill_preview"})
        _turn_bid = f"turn_{time.time_ns()}"
        _block_open = False
        _pending_done: dict | None = None
        _pending_summary: dict | None = None

        # 预算硬上限的降级出口。放在这里而不是各个 provider 调用点，
        # 是因为这是【整个用户驱动路径的唯一收口】——ReAct 循环第几轮撞上限都走到这。
        # UI 入口（start_pipeline_task）那道闸只拦"发之前就已超限"，
        # 拦不住"这一轮跑到一半跨过阈值"，而那恰恰是最常见的情形。
        from core.usage import BudgetExceeded
        from core.provider import ProviderNotConfigured
        try:
            async for event in self._handle_query_impl(query, image_parts, temp_file_hint):
                ev = event.get("event")

                if ev == "thought_block_start":
                    if not _block_open:
                        _block_open = True
                        yield {**event, "block_id": _turn_bid, "stage_label": "Nano"}
                    continue  # 重复 start 静默丢弃

                if ev == "thought_delta":
                    yield {**event, "block_id": _turn_bid}
                    continue

                if ev == "thought_block_done":
                    _pending_done = {**event, "block_id": _turn_bid}
                    continue  # 缓冲，等 terminal 之前 flush

                if ev == "thought_summary":
                    _pending_summary = {**event, "block_id": _turn_bid}
                    continue  # 同上，最后一次 summary 胜出

                # terminal 事件之前 flush 思考块收尾
                if ev in _TERMINAL and _pending_done:
                    yield _pending_done
                    _pending_done = None
                    if _pending_summary:
                        yield _pending_summary
                        _pending_summary = None

                yield event

            # generator 正常结束时 flush 残余（防御性兜底）
            if _pending_done:
                yield _pending_done
            if _pending_summary:
                yield _pending_summary
        except ProviderNotConfigured as e:
            # 未配置 API Key。这条路径的存在本身就是为了"用户能先把界面打开再配"，
            # 所以这里必须给一句指路的话，而不是让 UI 转圈或弹内核异常。
            logger.warning(f"[Provider] 未配置 API Key，本轮无法调用模型: {e}")
            yield {"event": "final_result",
                   "content": ("我还没拿到 API Key，暂时没法思考。\n\n"
                               "点右上角三个点打开设置，在「通用 → 环境配置」里填一下就行，保存后立刻生效，不用重启。"),
                   "model": "NOT_CONFIGURED", "status": "SYS_IDLE",
                   "log": "尚未配置 API Key。",
                   "current_skill": None,
                   "rag_hit": False, "full_file_hit": False}
        except BudgetExceeded as e:
            # 硬上限撞在半路：给用户一句能看懂的话，而不是"内核异常"。
            # 不写 memory 的 assistant 轮次——这一轮模型其实没说话，
            # 把系统话术塞进对话历史会让下一轮的模型以为那是自己说的。
            _msg = (
                f"今天的用量已经到上限了（${e.cost:.2f} / ${e.cap:.2f}），我先停一下。\n\n"
                f"点右上角三个点打开设置，在「通用」里能调高上限；不改的话，用量每天 0 点自动重置。"
            )
            try:
                from core.health import get_system_events
                get_system_events().add(
                    f"Daily budget cap reached (${e.cost:.2f} / ${e.cap:.2f}); the turn was stopped partway."
                )
            except Exception:
                pass
            logger.warning(f"[Budget] turn 中途撞上硬上限: {e}")
            yield {"event": "final_result", "content": _msg,
                   "model": "BUDGET_CAPPED", "status": "SYS_IDLE",
                   "log": "今日用量已达上限，本轮已停止。",
                   "current_skill": None,
                   "rag_hit": False, "full_file_hit": False}
        finally:
            # ⭐ 「回复这条」是**一次性**的：这一轮用过就复位。
            #
            # 用户的理由（2026-08-06）："模型已经被提醒过一次就够了，
            # 而且用户想聊别的时很容易忘记取消。"
            #
            # ⚠️ 放在 `finally` 而不是正常收尾处：这一轮**不管是正常结束、
            #    撞预算上限、还是抛异常，指向都已经被这一轮消费掉了**。
            #    漏掉任何一条出口，它就会粘到下一轮 —— 那正是这个 bug 本身。
            #
            # 📌 所有权：`_reply_target` 只有 orchestrator 一个权威副本。
            #    UI 通过 `_set_reply_target()` 写、直接读 `agent._reply_target`，
            #    不再自己存一份（双权威必然不同步 —— 这里尤其明显：
            #    复位发生在轮次结束，而那时 UI 根本没参与）。
            #
            # ⭐⭐⭐ [2026-08-13 CMD63] **两个字段都要清。**
            #    `_reply_target_turn` 是 UI 按发送时移交过来的本轮快照
            #    （见 `hand_off_reply_target`）—— 正常路径下真正带着 iid 的是它，
            #    漏清它 = 指向粘到下一轮，也就是这个 bug 的原始形态。
            #    `_reply_target` 仍要清：有可能压根没经过 UI 发送路径。
            _rt_consumed = (self._reply_target_turn or self._reply_target or {}).get("iid")
            if _rt_consumed:
                logger.info(
                    f"[Interaction] 「回复这条」{_rt_consumed} 已被本轮消费 → 自动复位"
                )
            self._reply_target = None
            self._reply_target_turn = None

    async def _handle_query_impl(self, query: str, image_parts: list | None = None, temp_file_hint: str | None = None):
        current_running_skill = None
        used_model = "UNKNOWN"
        event_queue = asyncio.Queue()

        # ⭐⭐⭐ 「这一轮有图、还没记下来」—— **本轮标志，不看 memory。**
        #
        # 🔴 实测连栽两次，两次都是**顺序**问题，而且两次的顺序还不一样：
        #   ① 第一版把判据写成 `memory.storage[-1] 有 ui_images`。
        #      但**核心工具清单在本函数上方就算完了**（`[TOKEN-PLAN]` 那行），
        #      那时 `add_message("user", query)` 都还没执行 —— storage 里根本没有这条。
        #      → `note_image` 被算进 deferred，模型只能先 `load_tools` 去捞。
        #   ② 就算捞到了，ReAct 循环里 `storage[-1]` 已经变成 `tool_results` 了，
        #      判据又变假 → 下一轮它从清单里消失 → 模型说「note_image 工具不可用」。
        #      ⚠️ 而 UI 上那个对钩是 **load_tools 成功**的对钩 ——
        #      **pill 和那句话都没撒谎，它们说的是两件事。**
        #
        # 📌 **判据不能挂在「storage 的最后一条是什么」上** —— 那个位置在一轮之内
        #    会变好几次，而"这一轮有没有图"从头到尾只有一个答案。
        # 📌 更一般的那条：**一条顺序断言只保护它写下的那个顺序。**
        #    为约束①补了测试，然后在②③上各摔了一次。
        self._turn_image_pending = bool(image_parts)

        # 🔴🔴 [2026-08-22 实测] **这里原来是无条件 `= None`，那是个真 bug。**
        #
        # 症状：Skill 交还之后，用户下一轮说「把这个放后台」，`dont_wait` 回
        #      「There is no slow call handed back to you right now」——
        #      而那件事**明明还在跑**。（Nano 随后还照着说了「已经在后台了」。）
        #
        # ⭐ 根因：**「可以被交出去的载体」这个记录点只活一轮，
        #    而「把这个放后台」这句话必然发生在下一轮。**
        #    ⚠️ 与载体类型无关 —— 命令/MCP/Skill 走同一个
        #       `_hand_back_long_task`，这是**轮级状态**的问题。
        #       实测里命令那两次成功，只是因为模型**当轮就调了** `dont_wait`。
        #
        # ⭐⭐ 而当初清空它的那个理由，**已经被一个更精确的东西接管了**：
        #    旧理由：「不清的话，下一轮 `dont_wait` 会作用在一条早就结束的等待上」
        #    现状：`_handle_dont_wait` 里已经有 liveness 校验
        #            （`find_by_id` + `is_live`），它**逐条问「那条等待还活着吗」**。
        # 📌 **一个防御措施的理由被另一个更精确的措施接管之后，它自己就该移除** ——
        #    两个都留着，粗的那个会把细的那个的精确性整个抹掉。
        # 📌 更一般的那条（本项目反复出现）：**别用近似物回答一个能精确回答的问题。**
        #    「是不是本轮」是近似物；「那条等待还活着吗」才是那个问题本身。
        #
        # ⚠️ 所以这里**只在指向已经死掉时才清**，而不是每轮都清。
        #    ⚠️ 校验放在这里是**顺手清账**，不是防线 —— 真正的防线仍然是
        #       `_handle_dont_wait` 里那一处（它在**用之前**问）。
        #       📌 记录点的清理和使用点的校验，是两件事，都要有。
        _car_prev = getattr(self, "_detachable_carrier", None) or {}
        if _car_prev.get("wait_id"):
            try:
                from core.runtime.kernel import get_kernel as _gk_pc
                from core.runtime import waitcond as _wc_pc
                _r_pc = _wc_pc.find_by_id(_gk_pc(), _car_prev["wait_id"])
                if _r_pc is None or not _r_pc.is_live:
                    logger.info(f"[B1] 上一轮的可交载体已终态"
                                f"（{_car_prev['wait_id']}）→ 清掉")
                    self._detachable_carrier = None
                else:
                    # ⭐⭐⭐ [2026-08-22] **用户一开口，还在前台上的载体自动转入后台。**
                    #
                    # 🔴 这一步（前台 → 后台）原本设计成「用户插话／我改主意 → `dont_wait`」——
                    #    即**由模型决定**。实测 73 证明这条走不通：
                    #    **模型不会自己调**，除非用户点名工具名，而真人不会那么说话。
                    #    于是用户说「把这个放后台」，Nano 嘴上答应、实际什么都没做。
                    #
                    # ⭐ 用户的推理（这才是关键，不是"改成自动"这么简单）：
                    #    「用户说了一句话，nano 此刻要处理的**不是**『用户交代了什么任务』
                    #      （我们猜不出），而是**『回应用户这句话本身』**」
                    #    → 于是「有没有需要我转移注意力的事」在这个情形下**恒为真**。
                    # 📌 **一个恒为真的判断，不该拿去问模型。**
                    #    这不是系统替模型做判断，是系统把一个不需要判断的东西
                    #    从模型的待办里拿走 —— 合乎老分工：
                    #    **系统答发生了什么，模型答所以怎么办**；而这件事没有「所以怎么办」。
                    #
                    # ⚠️ 只在**用户消息**这条路触发（本函数就是用户消息入口）；
                    #    回看轮不走这里，所以场景 #1（只有一件长任务）和 #3（有依赖）的
                    #    「留在前台」语义完好 —— 没人插话就什么都不变。
                    # ⚠️ 已经在后台的一个字都不动：`_detachable_carrier` 只存
                    #    **还在前台上**的那个，转入后台时就被清了（天然幂等）。
                    # ⚠️ `next_step` 填的是**真话** —— 回应用户确实就是下一步要做的事，
                    #    不是为了过闸编一个理由。
                    _pk_ev = self._park_carrier(
                        _car_prev, "回应用户刚说的话", why="用户插话")
                    yield _pk_ev
            except Exception as _e_pc:
                # ⚠️ 查不动就**留着** —— 使用点还会再查一次。
                #    📌 「不知道」该表现成「交给下一道关」，不是「当它死了」。
                logger.warning(f"[B1] 校验上一轮可交载体失败（留着，使用点再查）: {_e_pc}")
        else:
            self._detachable_carrier = None

        # 本轮 token 计数打点（与记账同一异步流，供 UI 单条显示取差值，避免时序竞争）
        try:
            from core.usage import usage_tracker as _ut
            _ut.begin_turn()
        except Exception:
            pass

        # 本轮 write_user_note 写入的笔记（react 内不发终端事件，攒到 final_result 交 UI）
        self._notes_written_this_turn = []

        # 每轮开始：RAG / FullFile hit 标记清零
        self._rag_hit_this_turn = False
        self._full_file_hit_this_turn = False  # Phase 2

        async def realtime_callback(m_name: str):
            nonlocal used_model
            used_model = m_name
            await event_queue.put({"event": "thinking", "log": f"模型已就绪，正在驱动 [{m_name}] 分析意图...", "status": "CORE_THINKING", "model": m_name, "current_skill": current_running_skill})

        yield {"event": "thinking", "log": "正在进行 LLM 语义路由...", "status": "CORE_THINKING", "model": "CONNECTING", "current_skill": None, "rag_hit": False, "full_file_hit": False}

        # OS 双循环合一：os_execute 始终注入主 ReAct 循环（include_os=True）。
        # OS 不再是被前置分类器硬路由到 _handle_os_task 的"特殊任务"，而是和查知识库、
        # 调 Skill 同级的无差别内置能力。这样 wait_for / render_visual / os_execute 等
        # 在同一个循环里自由组合，彻底消除"OS 任务里够不到挂起"这类双循环漏洞。
        # ⭐⭐ ① 工具清单问目录。
        #    改造前是 `_build_skills_info(...)` 那 11 个 `include_*` 开关的合力结果，
        #    「这一轮给哪些工具」要靠读懂 11 个布尔的组合才能答；
        #    现在是 `advertised(scope, runtime)` —— **有 binding + 此刻 available +
        #    不是 HIDDEN**，三条规则，没有开关。
        _cat = self._get_tool_catalog()
        _rtv = self._tool_runtime_view()
        _advertised = _cat.advertised(ToolScope.MAIN, _rtv)

        # ── 能力门控·第一道：manifest 层不给坏掉的工具 ──────────────────────
        # 光在 system prompt 里说"知识库不可用"不够——工具 schema 还摆在模型面前，
        # 它照样会调（外部评审推演出来、且已回代码核实：自然语言约束替代不了执行层约束）。
        # 只摘 UNAVAILABLE 的；DEGRADED 保留工具，限制写在能力边界文本里由模型自己权衡。
        #
        # ⚠️⚠️ **它刻意留在消费点，不进 Catalog，也不做成 `availability`。**
        #    📌 **这不是第二张名单，它是一层正交的临时遮罩**：
        #       Catalog 答「有哪些工具」，health 答「哪些暂时不能用」。
        #    🔴 塞进 `availability` 会出一个具体的错：坏掉的工具 `resolve()` 返回
        #       None → 调用方走 UNKNOWN_TOOL 诊断 → 模型收到「这个工具不存在」。
        #       **而那是假话。** 违反 「给模型的失败信息必须正确」——
        #       "不存在"和"坏了"是两种故障，共用一个出口就是撒谎，
        #       而那道执行层 fail-fast 存在的全部意义正是说出**正确**的那一句。
        #    还有一条实际理由：health 状态**在一轮之内会变**（探针恢复），
        #    而 Catalog 是按轮构建的，塞进去会让两者的生命周期绑死。
        #    ⚠️ 所以后人看到这里请**不要**把它"优化"进 Catalog —— 它没有违反
        #       「只有一张名单」，它根本不在回答同一个问题。
        try:
            from core.health import get_health
            _blocked = get_health().blocked_tools()
        except Exception:
            _blocked = set()
        if _blocked:
            _before = len(_advertised)
            _advertised = [d for d in _advertised if d.name not in _blocked]
            logger.warning(
                f"[Health] 能力门控：本轮下架 {_before - len(_advertised)} 个工具 "
                f"({', '.join(sorted(_blocked))})"
            )

        # ── 按需加载：拆"核心常驻" vs "延迟加载" ─────────────────────────────
        # Token 压缩：不让普通主循环常驻全部内置工具。默认只给极小核心集，
        # 其余只发一行感知，模型需要时先 load_tools。
        # ⚠️ `_tool_pool` 保留：它是 load_tools 真正 append 进 tools_manifest 的
        #    schema 来源（`_pending_loaded_manifests`），不是第二份名单 ——
        #    它的内容现在**整个来自** `advertised`。
        regular_skills = [d.manifest for d in _advertised]
        self._tool_pool = {m.get("name"): m for m in regular_skills if m.get("name")}
        # ⭐⭐⭐ [2026-08-23] **核心集排成「无条件在前、带条件在后」。**
        #
        # 📌 缓存前缀顺序是 `tools → system → messages`，tools 在最前面 ——
        #    它一变，后面全废。而 `availability` 带条件的核心工具
        #    （`dont_wait` / `stop_background` / `set_next_checkin`）
        #    **在轮与轮之间进出**，改的正是这个最脆弱的位置。
        # ⚠️ 这个副作用是 2026-08-22 加 `_when_has_carrier` 时**没想到的** ——
        #    当时只顾着 「工具和事实来源必须由同一个条件控制」，
        #    没意识到那个条件同时决定了缓存前缀稳不稳。
        #    📌 **一个判据在它自己的维度上正确，不代表它在别的维度上无害。**
        #
        # ⭐ 于是分成两段，断点打在中间（与 `_cached_system` 的 stable⟂dynamic 同形）：
        #        [ 无条件核心 ] ⟂ [ 带条件核心 ] + [ load_tools 追加的 ]
        # ⚠️ **顺序在这里被建立，`_to_anthropic_tools` 依赖它** —— 别在别处重排。
        from core.tools.catalog import ALWAYS as _ALWAYS_AV
        _core_always = [d for d in _advertised
                        if d.preload is Preload.CORE and d.availability is _ALWAYS_AV]
        _core_cond = [d for d in _advertised
                      if d.preload is Preload.CORE and d.availability is not _ALWAYS_AV]
        self._core_manifest = [d.manifest for d in _core_always + _core_cond]
        # ⭐ 稳定段长度 —— 发请求时告诉 provider 断点该打在哪。
        #    ⚠️ 它必须跟着 `_core_manifest` 一起算，**不能在别处重新数** ——
        #       📌 一个「这份名单的前 N 个」的数字，和那份名单必须同源，
        #          否则两边各自演化，断点就会打在错的位置上（而且不报错）。
        self._core_stable_n = len(_core_always)
        _deferred = [d for d in _advertised if d.preload is Preload.DEFERRED]
        # ③ 感知块由目录渲染 —— `[:28]` 字符级截断（根因）随旧实现删除。
        # ⚠️ 这里传的是**过完 health 门控**的那批，所以「坏掉的工具」也不会出现在
        #    感知块里 —— 与 manifest 层保持同一个口径（说得见的都真能用）。
        self._deferred_awareness_text = (
            _cat.DEFERRED_HEADER + "\n".join(f"  - {d.name}: {d.awareness}"
                                             for d in _deferred)
        ) if _deferred else ""
        try:
            logger.debug(
                f"[TOKEN-PLAN] core_tools={len(self._core_manifest)} "
                f"({','.join(m.get('name','') for m in self._core_manifest)}); "
                f"deferred_tools={len(_deferred)}"
            )
        except Exception:
            pass
        base_guide = self._system_guide_template.format(skills=', '.join(self._skill_names(regular_skills)))
        # ⚠️ 这里**曾经**有一行 `self._os_task_busy = False` 的每轮重置。
        # 已删 —— 租约靠 TTL 自愈，不需要"每轮兜底"这种依赖下一轮才生效的止血。
        # 📌 实测证伪过那个前提：「每轮重置把爆炸半径压到一轮」——
        #    「一轮」只在**有下一轮**时才存在，用户不说话它就一直挂着（实测 61 分钟）。
        # ⭐ 前台窗口身份的比较基准每轮清零 —— 跨轮的"窗口变了"没有意义，
        #    那本来就是两件事之间。与活动租约同一个边界（见 `_window_identity_note`）。
        self._last_fg_window = None
        # ⭐ 活动租约也在每轮开头兜底归还。
        #    它的正常释放点是"一整段 GUI 操作结束"，而"结束"最可靠的判据就是
        #    **下一轮开始了** —— 所以这里是它真正的边界，不是 os_execute 的 finally。
        #    理由见 `os_execute` 分支里那段注释。TTL 只是二道兜底。
        _rt_lease_release(self)
        # 同理重置 ReAct batch 事务标记。2026-08-04 补：
        # 它原本【只有】置位与正常路径的复位，没有任何跨轮兜底——
        # 一旦 tool_calls 已写、tool_results 未写完时抛异常（`_run_react_loop`
        # 5781→5847 之间），它就会带着 True 活到后面的轮次，造成两件事：
        #   ① `_clean_damaged_memory` 在【后来一次无关的失败】里误判成"批次未关闭"
        #      → 回滚掉合法的历史工具对（而那个守卫存在的目的恰恰是防止误删）；
        #   ② 400 分支（5665）会跳过 `repair_invalid_tool_turns`，
        #      历史坏 pair 修不掉 → 下一轮继续 400。
        #
        # ⚠️ 为什么不是在那两个调用点外面包 try/finally：
        # 这个标志的用途就是"异常发生时告诉 _clean_damaged_memory 可以安全回滚"。
        # finally 会在外层错误处理读到它【之前】清零，回滚被跳过 —— 等于把这个机制废掉。
        # 所以正确做法是照 _os_task_busy 的范式做【每轮重置】：轮内语义不变，跨轮不残留。
        #
        # ⚠️ 顺序很重要：**必须在重置之前把旧标志的值抄给 shadow**。
        # 因为"上一轮遗留 True"正是覆盖表第 6/7 行（批次抛异常 / generator 被丢弃）
        # 唯一的观测窗口——重置之后就永远看不到它了。
        self._rt_turn_id = "rtturn_" + __import__("uuid").uuid4().hex[:12]
        # 工具失败计数按轮清零：跨轮保留会让"你这轮已经试过"变成假话。
        self._tool_failures_this_turn = {}
        # 每轮开头只需要 sweep 掉上一轮的残留 span，没有旧字段可重置了。
        _rt_sweep_stale_spans(self, None)

        # User profile injection
        try:
            import json as _json, pathlib as _pl
            from core.paths import data_path as _data_path
            _prof = _data_path("user_profile.json")
            if _prof.exists():
                _p = _json.loads(_prof.read_text(encoding="utf-8"))

                _not_set = "not set"
                _nickname = _p.get("nickname") or _not_set

                _bday = _p.get("birthday")
                _birthday_label = f'{int(_bday.split("-")[0])}/{int(_bday.split("-")[1])}' if _bday else _not_set

                _region = _p.get("region", {}).get("display", _not_set)
                _identity = _p.get("identity", _not_set)
                
                base_guide += (
                    f"\n\n[User Profile — From Settings, Do Not Duplicate Into Memory]\n"
                    f"Preferred name: {_nickname}\n"
                    f"Birthday: {_birthday_label}\n"
                    f"Region: {_region}\n"
                    f"Identity/context: {_identity}\n"
                    f"These fields are maintained by the user in settings. "
                    f"If the user asks Nano to remember name, birthday, region, identity, or similar profile fields, "
                    f"respond based on the information above: if already set, say Nano already knows it; "
                    f"if not set, suggest filling it in settings. "
                    f"Do not write these fields again into conversation memory."
                )
        except Exception:
            pass

        # 工具感知注入——让模型知道自己当前真实拥有哪些能力
        try:
            base_guide += self._build_tool_awareness_block()
        except Exception:
            pass

        # 按需加载：把延迟工具的"感知"（名字+一句话+load_tools 用法）注入
        try:
            if getattr(self, "_deferred_awareness_text", ""):
                base_guide += self._deferred_awareness_text
        except Exception:
            pass

        # OS_CAPABILITY_PROMPT 很长，且普通聊天不需要。
        # 现在改为：只有 load_tools 真正加载 os_execute / look_at_screen / set_window_mode 后，
        # 在 _run_react_loop 内按需注入，避免每句普通消息都背 OS 操作手册。

        # 用户唤醒（打底源）——用户发来新消息时，若存在 active 挂起记录，
        # 把"你之前在等什么"注入 guide，并把这些记录标记恢复（resolved_by=user）。
        # 这样模型会带着上下文重新决策"等的事成了没"，没成可以再 wait_for。
        # 标记恢复后，store 里这些记录不再 active，app 侧基于 store 轮询的定时器
        # 自然不会再为它们触发（不留孤儿定时器）。
        # ⭐⭐ [无缝对话 · 措辞] 续接段：告诉它「你这一段会和上一段拼在一个气泡里」。
        #
        # ⚠️⚠️ **注入的是事实，不是表演指令。**
        #    ❌「请假装这是一次连续对话」→ 那是**演出脚本**，模型会批量生产过渡词，
        #      而且可能**编造连续性**（声称自己做过没做的事）。
        #    ✅「你上一段和这一段会显示在同一个气泡里」→ 这是它**推导不出来**的事实。
        #    📌 与故障卡片验证过的那条同源：
        #       **给模型注入它自己的状态，模型就会自主调整行为 —— 给状态，不给剧本。**
        #
        # ⭐ 而且**只注入它推导不出来的那一点**：它已经能在对话历史里看到自己
        #    上一段回答了，它唯一看不到的是**呈现方式**。
        #    📌 只注入模型无法从上下文推导出来的那一点 ——
        #       其余全是噪音，而噪音会训练它忽略注入。
        #
        # ⚠️ **走这条动态段通道，绝不拼进用户原话** ——
        #    📌 用户的原话必须保持原样（同早先`answer_verbatim` 那条纪律）；
        #       拼进去之后模型就分不清哪句是用户说的、哪句是系统说的。
        try:
            _seam_part = int(getattr(self, "_seam_continuation_part", 0) or 0)
            if _seam_part >= 2:
                # ⚠️⚠️ **这段文案 2026-08-08 改过一次，原因值得记。**
                #    第一版写的是「你**上一段回答**和这一段会显示在同一个气泡里」——
                #    那在「执行分段」时代是真的，但改成**协作式中断**之后
                #    **上一段被撤回了、屏幕上不存在** → 那句话变成了**假话**。
                #    📌 **一个描述「当前呈现方式」的注入，在呈现方式变了之后
                #       必须一起改，否则它就是在给模型讲一个不存在的现实。**
                #
                # ⭐ 现在说的是真事：用户在你上一次回答成型**之前**又补了一句，
                #    所以你会看到**连续多条 user 消息**，而**只应该给一个回答**。
                #    这也顺带解决了 用户要的「标记第一条/第二条」——
                #    模型知道后面那条更新、前面那条可能已被推翻。
                base_guide += (
                    "\n\n[Interjected] The user sent another message while you were "
                    "still working on the previous one, so your earlier attempt was "
                    "discarded before it produced anything — nothing was executed and "
                    "nothing was shown to them. You will therefore see several "
                    "consecutive user messages: the LATER ones are more recent and may "
                    "revise or cancel the earlier ones. Give ONE single answer that "
                    "addresses their combined intent. Do NOT answer them one by one and "
                    "do NOT contradict yourself."
                )
                if _seam_part > 2:
                    base_guide += (
                        f" They have interjected {_seam_part - 1} time(s) so far."
                    )
                # ⚠️⚠️ **还有哪些东西在跑，必须告诉它** ——
                #    否则用户说「算了这个不做了」时，模型**不知道有什么可取消**。
                #    📌 代码判「有没有新消息 → 中断本轮」（确定性）；
                #       模型判「那些后台任务要不要取消」（需要理解意图）。
                #       —— 「模型判断 vs 代码判据必须分开」的又一实例。
                #    ⭐ 而这条注入是那个分工的**前提**：没有它，模型没有可判的材料。
                try:
                    from core.runtime.kernel import get_kernel as _get_bg_wait_kernel
                    from core.runtime import waitcond as _wc_bg
                    _bg = [r for r in _wc_bg.list_live(_get_bg_wait_kernel(), oldest_first=True)
                           if _wc_bg.WakeSource.BACKGROUND in r.wake_on]
                    if _bg:
                        _bg_lines = "; ".join(
                            (r.reason or "background work")[:80] for r in _bg[:5])
                        base_guide += (
                            f"\n[Still running] {len(_bg)} background item(s) are still "
                            f"running and were NOT affected by the interruption: "
                            f"{_bg_lines}. They keep going unless you explicitly cancel "
                            f"them. If the user's new message means they should stop, "
                            f"cancel them yourself — the interruption did not."
                        )
                except Exception:
                    pass
            # ⚠️ 即读即清：它只对**这一次**调用有效。
            self._seam_continuation_part = 0
        except Exception:
            pass

        try:
            from core.runtime.kernel import get_kernel as _get_active_wait_kernel
            from core.runtime import waitcond as _wc_active
            _active_susp = _wc_active.list_live(
                _get_active_wait_kernel(), oldest_first=True)
            if _active_susp:
                base_guide += self._build_suspension_resume_injection(_active_susp)
                # ⭐⭐ **用户发消息不再 resolve 任何挂起。**
                #
                # 旧代码只豁免 background-only，于是 `wake_on=['timer']` 的挂起
                # 会被**任何一句无关的话**收掉。实测场景：
                #   用户「100 秒后在桌面写个文件」→ Nano 设定时
                #   → 用户第 30 秒说「今天天气不错」→ **那个承诺就没了**。
                # 旧代码的辩解是"注入回去让模型自己再 wait_for 一次"，
                # 等于把一个**确定的承诺**换成了一次**模型判断**。
                #
                # 📌 判据（用户提出、外部评审 独立确认）：
                #    **「本轮 Nano 不再暂停」与「未来的承诺已经解除」是两件事。**
                #    用户发消息只证明前者。
                #
                # 现在：注入**保留**（模型需要知道自己在等什么），resolve **删除**。
                # 定时到点由轮询收；用户真想取消，说出来 —— 模型调 `cancel_wait`（F 项）。
                # ⚠️ 这也让 `resolved_by="user"` 这个值从此不再产生。
                logger.info(
                    f"[Suspension] 用户唤醒：注入 {len(_active_susp)} 条挂起上下文"
                    f"（**不 resolve** —— 用户说话不代表他等的事成了）")
                # 观测期曾在这里重读新旧两边做对答案；双写已消失，继续比较只会
                # 拿权威和自己比，制造一条永远为真的观测噪音。
        except Exception as _se:
            logger.warning(f"[Suspension] 用户唤醒注入失败（跳过）: {_se}")

        # "信息真实"：模型训练分布里大量"我这就去处理/你等我一会"这类
        # agent式表述——在 Claude Code 等真的会在后台持续工作的 agent 里
        # 是真的，但这套框架严格单轮同步：本轮回复结束后这次"我"就不存在
        # 了，之后什么都不会发生，除非用户发下一条消息触发全新一轮。
        # 放在 base_guide 源头，会传播到下游所有 guide 变体
        # (exploration_guide / 各阶段 system_guide 都是 base_guide + ... 拼出来的)。
        base_guide = base_guide + (
            "\n\n[Conversation Boundary — Do Not Promise Later Work]\n"
            "After each reply ends, this turn stops completely. Nano will not keep working in the background unless the user sends another message "
            "or a wait/background mechanism has explicitly been set up.\n"
            "- Do not say things like 'I'll do it now', 'wait a moment', 'I'll handle it later', or 'I'll fix it for you' if the action will not actually happen in this turn.\n"
            "- If a tool can complete the task now, use it now. If not, clearly tell the user what they need to say or do next.\n"
            "- Nano cannot directly rename/delete/disable/enable Skill files through normal text. For those requests, use the Skill management flow "
            "and explain that confirmation is required before anything changes.\n"
            "- For Skill replacement, mention both creating the new Skill and what will happen to the old one. Do not say 'I'll delete the old one' "
            "unless the system is actually entering the confirmed management flow."
        )

        # Token 压缩 Stage2：动态数据块（session_log/episodic/ambient/tone）的【固定使用说明】
        # 从每轮全价重发的动态区，挪到这里的稳定前缀（缓存 0.1x，只付一次）。哨兵之后只留
        # 真正每轮变化的【数据本身】。模型看到的信息完全一致，只是换了缓存分区，零体验变化。
        base_guide = base_guide + (
            "\n\n[Live Context Blocks — How To Use Them]\n"
            "Below the boundary, when available, you may see these live blocks. Read them when relevant, "
            "but never invent details beyond what they contain:\n"
            "- [Session Log]: concrete actions Nano took THIS session (file paths, Skill names, deploy/call "
            "status, generated outputs). Trust them for those facts.\n"
            "- [Recent Cross-Session Summaries]: hint-level memory of past sessions. For exact paths, Skill "
            "names, deployments, deletions, or call records, call recall_working_memory instead of relying on the summary.\n"
            # ⭐⭐ **把「不许调工具」的范围收回到它本来该管的那一件事上。**
            #
            # 🔴 原文一句话罩住了两种完全不同的请求：
            #      「我刚才在干嘛」        → 这个块**就是**答案，再调工具纯属浪费（原意）
            #      「把我刚才那个文件读了」 → 这个块**给不出**答案，它只有名字没有句柄
            #    而禁令是无差别的 ⇒ 第二种请求被一起挡住，模型要么拿窗口标题里的
            #    相对文件名去硬试，要么直接回一句「我看不到路径」。
            # 📌 **一条按「话题」划的禁令，挡住的是「意图」** —— 两句话都在说
            #    「刚才那个」，但一句要的是叙述，另一句要的是能喂给工具的参数。
            #
            # ⚠️ **刻意【不】在这里指向 `resolve_ambient_referent`** —— 那个工具还不存在。
            #    📌 同 `os_execute` 描述里那条教训：**schema 说有、运行说没有，
            #       是最难查的一类失败 —— 模型会反复尝试，而每次都合法地失败。**
            #    ⇒ 2026-08-26 落地，出口已接上（`resolve_ambient_referent`）。
            #
            # ⭐⭐ 注入的是**互动感版**（时间 + 名字），完整路径/URL 留在 trail 里
            #    按需取 —— 这一行是**每轮无条件注入**的，每多一个字都乘以轮数。
            #    📌 三层各管一件事：**采集要全**（丢了就永远丢了）、
            #       **注入要省**（每轮都付钱）、**拉取按条**（不是按范围：
            #       语义匹配发生在已经在上下文里的那一版上）。
            "- [Ambient]: what the user was doing right before switching to Nano (app, window title, activity "
            "rhythm, idle, background audio). Use it to resolve references like 'this', 'that', or 'what I was just "
            "doing'. If the user asks what they were just doing, answer directly from this block — do NOT call tools "
            "or memory for that, the answer is already here, and do not ask back. That rule covers only telling them "
            "what they were doing. Acting on one of those things is different: the block gives names, not handles - a "
            "window title is not a file path. An entry marked with a small triangle can be turned into a real file "
            "path, URL or folder by calling resolve_ambient_referent with the timestamp printed on that same entry; "
            "entries without the mark hold nothing beyond what you already see. Never invent a path for something you "
            "only saw the name of. Weave it in naturally only when "
            "helpful; do not recite app names as a checklist, do not mention it every reply, do not over-explain or "
            "sound like surveillance, and add no privacy disclaimers. It reads no private content.\n"
            "- [Tone]: affects the flavor/style of your wording ONLY — never answer quality, completeness, or "
            "accuracy. Do not talk about your own mood unless asked. On a serious task, prioritize completing it "
            "clearly and correctly. If the tone is negative or terse, stay concise and task-focused; never mock, "
            "blame, guilt-trip, or self-pity.\n"
            "- [Capability Health]: components that are currently broken or degraded. UNAVAILABLE means the related "
            "tools are withheld this turn — do not claim you will use them, and if the user asks for that capability, "
            "say plainly what is wrong. DEGRADED means still usable with reduced quality or coverage — mention it only "
            "when it actually affects the answer. Never invent a workaround you do not have.\n"
            "- [Recent System Events]: things that happened inside this process — component failures, recoveries, "
            "background completions. **They are not things you said or did.** Do not bring them up on your own; use "
            "them only when the user asks what just happened."
        )

        # ── 缓存分界 ──────────────────────────────────────────────────────────
        # 到此为止的 base_guide 是【每轮一致的稳定前缀】（persona / 工具说明 / OS 能力 /
        # 对话边界等），插入哨兵；下游追加的 session_log/episodic/ambient 等每轮变动
        # 的注入都落在哨兵之后。provider._cached_system 据此只缓存稳定前缀，动态内容
        # 不再打穿缓存。base_guide 直接当 system_guide 用的路径，哨兵后为空，无副作用。
        # ⚠️ Environment 块进**哨兵之前** —— 它一次会话内不变，属于稳定前缀，
        #    落在哨兵之后会白白打穿 prompt cache。
        #    📌 对照：MCP 清单是**动态**的（状态会变），必须在哨兵之后。
        base_guide = base_guide + self._environment_block() + CACHE_BREAK_MARKER

        # Step 4：pending 超时检查
        self._expire_stale_pending()

        # 1. 待确认管理动作优先
        # ── 1. 这里【曾经】是 Skill 管理/修改确认的路由劫持 ──────────────────
        # [2026-08-06 删除] 原来是
        #     if self._pending_action: -> _handle_pending_action()  （117 行 + 一个专用分类器）
        # 每条消息先跑 `classify_pending_action_intent` 分成 6 个标签，再按标签分支，
        # 其中 NEW_REQUEST 那一支还要靠一个伪事件 `__pending_new_request__`
        # 从生成器里"逃逸"回常规路由 —— 那个 break-then-continue 的控制流本身就很脆。
        #
        # ⚠️ 顺带纠正一处长期误记：`_pending_action` 的四个 op
        # （delete / disable / enable / update_skill）**全是 Skill 生命周期操作，
        # 与 OS 无关**。早先的设计曾把它记成"OS 风险确认"，因为字段名太泛。
        # 真正的 OS 确认是 `execution_confirm` + 300 秒同步等待（清单 ⑧）。
        #
        # 现在它是一条 `skill_manage` Interaction，用户下一条消息走正常路由，
        # 模型从 `[Open Interactions]` 看到它自己判断。

        # ── 2. 这里【曾经】是 Skill 审计的路由劫持 ──────────────────────────
        # [2026-08-06 删除] 原来是
        #     if self._pending_skill: -> _handle_pending_skill()  （约 90 行 + 一个专用分类器）
        # 那条路把用户的下一条消息整条截走，先跑 `classify_pending_skill_intent`
        # （**每条消息多一次 API 往返**）分成 8 个标签，再按标签分支。
        #
        # 三个问题一起没了：
        #   ① 8 个标签里有一半（EXPLAIN / RISK / COMPLAINT / EXECUTE_BLOCKED）
        #      本质就是"正常回答用户" —— 主模型天然会做，不需要先分类（设计原则 3）；
        #   ② 模型看不见"有个 Skill 等着审"，用户说别的时它无从判断；
        #   ③ `_pending_skill` 是纯内存 dict，崩溃/关窗后那份待审代码不留痕迹。
        #
        # 现在审计是一条 `skill_audit` Interaction（artifact 钉版本，），
        # 用户的下一条消息走**完全正常的路由**，模型从 `[Open Interactions]` 看到它，
        # 自己决定调不调 `answer_open_interaction`。UI 按钮那条路一字未改。

        # ── 2.5 这里【曾经】是 Skill 澄清的路由劫持 ────────────────────────────
        # [2026-08-05 删除] 整段约 50 行没了，没有替代分支——
        # 澄清态现在是一条 Interaction，用户的下一条消息**走完全正常的路由**，
        # 由主决策模型看着 [Open Interactions] 决定调不调 answer_open_interaction。
        #
        # 被删掉的三样东西，各自是一个独立的 bug：
        #   ① `_CLARIFY_TIMEOUT = 600` —— 10 分钟没回应就静默作废。用户去开个会
        #      回来，答案就没人接了，而且**没有任何提示**说它作废了。
        #      新实现里澄清永不过期（deadline_at 留空），积压不是错误。
        #   ② `_abort_words` / `_NEW_REQUEST_SIGNALS` 两张关键词表 + 一条
        #      "长度>20 且不含 Skill 词就算新话题"的启发式。两个方向都会错：
        #      "删除那一列的空行" 命中"删除"被判成新请求；
        #      "帮我查一下今天的天气" 里的"查一下"同理。反过来，一句
        #      "那个文件在 D 盘" 只要超过 20 字又不含表里的词，也会被判成新话题。
        #   ③ **模型全程看不见这件事**。劫持发生在进模型之前，所以上面两类误判
        #      模型一次纠错机会都没有——它连"有个问题挂着"都不知道。
        #
        # ⚠️ 顺带修掉一个从来没被记录过的形状：旧代码在调 Explorer **之前**
        # 就把 `_pending_skill_clarification = None` 了。于是 Explorer 那一步
        # 只要失败（provider 报错、进程被杀），用户刚说的答案就彻底消失，
        # 只能让用户重说一遍。现在是先落盘 ANSWERED 再跑 Explorer，失败可重试。

        # ── 这里【曾经】是"OS 层软急停前置检查" ──────────────────────────────
        # 关键词硬匹配 + LLM 语义兜底两层，命中就置进程级 _aborted。整套软急停已删除
        # （原因见 core/os_layer/safety.py 模块头：有甩鼠标和 Ctrl+` 两种瞬发物理手段在，
        # 第三种没人用，而且它自己带着"一旦触发这个进程再也不能操作电脑"的坏性质）。
        #
        # 顺带修掉的 token 浪费：这段的进入条件是 hasattr(self, "_os_safety")，
        # 而 _os_safety 一旦因为某次 os_execute 被懒创建就永久存在——于是本进程
        # 之后【每一条】用户消息都要多跑一次 classify_os_abort_intent 的独立 LLM 往返。
        # v1.1 那轮删顶层分类器时漏掉了这一处。

        # 2.8 OS 定位失败后的简短纠正 → 把纠正并进 query，落到主 ReAct 循环续接。
        # 双循环合一后不再有独立 OS 循环，os_execute 始终在主循环里，模型看着
        # 对话历史（含上次定位失败）+ 用户纠正，自然重试，无需特殊路由。
        _os_followup = self._detect_os_locate_followup(query)
        if _os_followup:
            self._last_os_locate_failure = None
            logger.info(f"[Router] OS 定位失败后的简短纠正 → 合并 query 落主循环: {query}")
            query = (
                f"{_os_followup['original_query']}"
                f"（用户纠正/补充：{query}）"
            )
            # 不 return，继续往下走常规分类 → 主 ReAct 循环（含 os_execute）

        # ── 3a. 这里【曾经】是 SKILL_CREATE 关键词快路径 ────────────────────
        # [2026-08-06 删除] 整段约 45 行 + `_SKILL_CREATE_KEYWORDS`
        # 那张 13 条的关键词表一起没了。
        # （常量名写在这里是故意的 —— 早先的设计按名字引用它，有人 grep 时该找到这块墓碑，
        #   而不是一无所获然后以为自己记错了。）
        #
        # 它最早是分类器的补丁（SKILL_CREATE 阈值 0.75 偏高，混进 OS 相关词会被稀释）。
        # 但 v1.1 已经把 `classify_primary_intent` 整个删了，此后它保护的是一个
        # **不存在的分类器**。后来留着它的理由换成了"防模型把明确的建 Skill 请求劝退"。
        #
        # 删掉的两条实测证据（08-06，都不是推测）：
        #
        # **证据 1 —— 同一个功能有两种可见性。**
        # 「写一个skill 作用是提取dns」命中关键词 → **整个 ReAct 主循环被跳过**
        # → 没有任何 tool_use → 屏幕上没有工具卡片。
        # 紧接着「再写一个，作用是提取ip地址」没命中 → 走主循环 →
        # 正常出现 `加载能力: create_new_skill ✓`。
        # 用户那句话里有没有"写一个 skill"，决定了用户能不能看见 Nano 在做什么。
        # 违反了那条约定（新工具必须能被工具卡片显示、且正确出现在感知清单里）。
        #
        # **证据 2 —— 它一直在替 那条 bug 挡枪。**
        # 三份日志里"快路径入口从不越界、元工具入口每次都越界"，
        # 当时判断成"快路径运气好"。真实原因是它的 `messages` 只有 1 条 ——
        # 没有 `load_tools` 回执可污染。**所以那个 bug 才活了那么久没被定位。**
        #
        # 📌 判据：**一条绕过主流程的快路径，会同时绕过主流程的可见性与诊断。**
        # 省下的两次往返，代价是同一个功能有两条行为不同的路，
        # 而其中一条永远不产生可观测记录。
        #
        # ⚠️ 删它的前置条件已满足：`create_new_skill` 的感知行不再被截断到 28 字符
        # （见 `_AWARENESS_FULL`）。那是"防劝退"这个理由的正面解法 ——
        # 让模型看清工具是什么，而不是绕开它自己的判断。

        # 2.7 Skill 报错后的简短回应 → 直接走修复通道，绕过顶层分类器
        # 必须在 OS 急停和 SKILL_CREATE 强关键词之后检测，防止
        # "别动屏幕了" / "写一个skill..." 等语句被错误拦截到修复路由。
        # 短确认词（好的/可以/修一下）+ 显式修复动词 + 工具报错状态三条件同时满足才触发。
        _error_followup = self._detect_skill_error_followup(query)
        if _error_followup:
            _error_fix_target, _error_content = _error_followup
            self.memory.add_message("user", query)
            logger.info(f"[Router] 检测到 Skill 报错后的简短回应 → 走修复通道: {_error_fix_target}")
            _got_skill_preview = False
            async for step in self._generate_skill_update(
                query, _error_fix_target, base_guide, realtime_callback,
                error_context=_error_content
            ):
                if step.get("event") == "skill_preview":
                    _got_skill_preview = True
                yield step
            # 只有成功产出 skill_preview 才标记消费，失败时保留可重试
            if _got_skill_preview:
                if getattr(self, "_last_skill_error", None) and not self._last_skill_error.get("consumed"):
                    self._last_skill_error["consumed"] = True
            return

        # 3. 顶层语义分类【已删除 · 残骸清理 2026-08-04】
        # 原来每条消息先跑一次分类器判 NO_TOOLS/REACT 决定走不走 fast-path。实测两点：
        #   ① 分类器每条一次、~770 fresh token，且中转下这么小的前缀无法缓存（<4096 门槛）；
        #   ② fast-path 的 system 前缀（~2.4K）同样够不到缓存门槛，每条全价重发。
        # 合并做法：删分类器 + 删 fast-path，所有消息统一走 ReAct 主循环——它带核心工具、
        # 稳定前缀+工具 >4096 会命中缓存（0.1x），闲聊时模型按 ReAct 协议直接出话、不调工具。
        #
        # 那次只删了分类器本身，把 `primary_intent="DIRECT_ANSWER"` / `primary_conf=0.0`
        # 两个常量和一整套"防御性降级"判断留了下来。既然分类器已经不存在，
        # 那些判断永远进不去（`0.0 >= 0.55` 恒假），一并删除：
        #   - SKILL_CREATE / SKILL_UPDATE / SKILL_DELETE|DISABLE|ENABLE 三段降级分支
        #     （它们的意图已由 create_new_skill / update_existing_skill /
        #      manage_existing_skill 三个元工具在主循环里承担）
        #   - SKILL_OTHER_THRESHOLD / OS_TASK_THRESHOLD 两个只服务于上述分支的阈值
        #   - registered_skill_names —— **零引用，但每条消息都要跑一次
        #     `get_all_manifests()` 建一个 set**，纯浪费
        #   - _classifier_says_simple —— 恒为 False，在 or 链里等于不存在
        #
        # OS 也不再有前置硬路由：os_execute 始终注入主 ReAct 循环（见上方 include_os=True），
        # OS 任务和普通任务走同一条循环、同一套工具集。这消除了"OS 任务被分流到独立循环、
        # 够不到 wait_for/render_visual 等其它能力"的双循环漏洞
        # （0627 os_execute replay bug、够不到屏幕都源于此）。


        # 4. 常规路径
        self.memory.add_message("user", query)

        # ⭐ 用户发的图 → Nano 自存一份，引用落进刚写的这条 user 消息。
        #
        # ⚠️⚠️ **位置有两个硬约束，两个都踩过：**
        #   ① 必须在 `_last_user_msg.content = _parts`（把 content 换成带 base64
        #      的 list）**之前** —— 否则 `attach_user_images` 里的 `update_message`
        #      会把整张图的 base64 写进权威账本。
        #   ② 🔴 **必须在 `_image_note_request_block()` 之前** ——
        #      原来它排在 system_guide 组装之后，于是那一刻 `ui_images` 还是空的，
        #      `has_unsummarized_image()` 恒 False，**那段「顺手记一份摘要」的提示
        #      永远不会出现**。实测实测：图答对了，`image_summary` 却是 None。
        #      ⚠️ 而它**一声不响** —— 没有报错、没有告警，只是摘要永远不生成。
        #      📌 原来的测试钉住了约束 ①，然后在约束 ② 上栽了 ——
        #         **一条顺序断言只保护它写下的那个顺序。**（已补断言）
        # 📌 收口在 memory 那一侧，不在这里拆 base64：将来加粘贴通道 / 拖拽 /
        #    Skill 产图，都不用再想起这件事。
        if image_parts:
            self.memory.attach_user_images(image_parts)
            # ⚠️ 顺序要紧：**先** attach（登记 handle / 落盘），**再**决定
            #    pixels 发不发给主模型 —— 回看能力挂在 handle 上，
            #    绝不能因为"这个主模型看不了图"而丢掉它。
            if _main_model_is_blind():
                yield {"event": "thinking", "log": "正在用视觉模型识图...",
                       "status": "CORE_THINKING", "current_skill": None,
                       "rag_hit": False, "full_file_hit": False}
                image_parts = await self._seed_image_summary_via_vision(image_parts, query)

        # ⭐ [2026-08-22] 引用指向跟着这条消息落盘 —— 见 `attach_reply_quote`。
        # ⚠️ 读的是 `_reply_target_turn`（UI 按发送时移交过来的本轮快照），
        #    退回 `_reply_target` 兼容还没移交的路径 —— 与
        #    `_build_quoted_selection_block()` 读的是**同一个来源**。
        #    📌 展示给用户的那份和喂给模型的那份，必须来自同一个事实，
        #       否则总有一天它们会说两件事。
        try:
            _rq = ((getattr(self, "_reply_target_turn", None)
                    or getattr(self, "_reply_target", None) or {}).get("q") or "")
            if _rq:
                self.memory.attach_reply_quote(_rq)
        except Exception as _e_rq:
            logger.debug(f"[Reply] 引用落盘跳过: {_e_rq}")

        # ⭐⭐⭐ [无缝对话 · 协作式中断] 记下本轮开始时「收到过多少条用户消息」。
        #
        # 之后 ReAct 循环在**动作边界**比对这个基线 —— 变大了就说明用户插话了，
        # 于是**停止本轮**，让下一轮带着**两条消息**重新决策。
        #
        # 📌 **无缝对话的本质是「让模型在决定之前看到全部输入」**，
        #    不是「让两次输出看起来连续」—— 后者在第二条撤回第一条时必然矛盾
        #    （用户的反例：「现在几点？」→ thinking →「算了不要调用工具了」
        #      → 执行分段会先查了时间、再说「好那我不查了」）。
        #
        # ⚠️ **基线用计数器，不用「有没有事件」** —— 队列里早就存在的消息
        #    不该让刚开始的这一轮立刻自我中断。
        #    📌 用「事件」表达「有新东西」时必须定基线，否则它表达的是「曾经有过东西」。
        try:
            from core.runtime import inbox as _ib_seq
            self._turn_input_baseline = _ib_seq.submit_seq()
        except Exception:
            self._turn_input_baseline = None

        # ⭐⭐ **新一轮开始就清掉终止意图。**
        # ⚠️ 不清的后果：上一次的终止会把**下一轮也杀掉** ——
        #    用户点了停、然后重新问一句，结果那句话一进来就被上次的终止干掉，
        #    表现为「Nano 再也不回话了」。
        #    📌 一个「一次性的意图」必须有明确的清除点，
        #       否则它会从「这一次」悄悄变成「从此以后」。
        #       （形状与那个泄漏的忙标志同族：**没人负责清 = 永久生效**。）
        self.clear_stop()

        # Token hygiene: remove historical image/base64 blocks before building ReAct context.
        # Current-turn images are not in memory yet, so this does not affect current image understanding.
        # The placeholder keeps the fact that the user previously sent image(s), but drops the heavy payload.
        try:
            _compressed_imgs = self.memory.compress_image_blocks()
            if _compressed_imgs:
                logger.debug(f"[Memory] compressed {_compressed_imgs} historical image block(s) before ReAct context build")
        except Exception as _img_compact_err:
            logger.debug(f"[Memory] historical image compression skipped: {_img_compact_err}")

        # ⭐⭐ 同一个位置、同一条理由：把**已经读过的旧文件切片**换成占位符。
        #    这是「恒定上下文厚度」唯一的落地点 —— 那条状态机
        #    「读入原始数据 → 提炼进 scratchpad → **丢弃原始数据** → 带着它进入下一轮」。
        # 📌 没有它，`notes` 参数只是多存了一份东西，原文照样在上下文里堆着 ——
        #    **只做一半（写了 notes 却不丢旧切片），得到的是「原文照样堆着再加一份笔记」，比不做还贵。**
        # ⚠️ 与图片压缩挂在同一处不是巧合：两者是**同一件事的两个实例**
        #    （把历史里的重载荷换成占位符），所以它们的时机也必须一样：
        #    **在构建 ReAct 上下文之前，且在本轮内容进 memory 之前。**
        try:
            _compressed_reads = self.memory.compress_file_reads()
            if _compressed_reads:
                logger.debug(f"[A2] 压掉 {_compressed_reads} 段旧的文件切片（保留每份文件最后一段）")
        except Exception as _read_compact_err:
            logger.debug(f"[A2] 文件切片压缩跳过: {_read_compact_err}")

        # 🔴 **这里刻意不带 `model`**（2026-08-14 实测）：此刻 `used_model` 还是
        #    `"UNKNOWN"` —— 真实模型名要等 provider 回报、由 `realtime_callback` 填。
        #    带上去的结果就是监控卡「当前模型」在发送途中闪成 UNKNOWN，回复后才恢复。
        # 📌 **一个「我还不知道」被渲染成一个具体值，比不显示更糟** ——
        #    同监控卡那条（量不到显示 `--` 不是 0%）。
        #    ⚠️ 消费端也加了守卫（`app.py` 收到 UNKNOWN 一律忽略），因为带 model 的
        #    yield 点有几十处，漏一个就复现 —— 📌 **防御要放在收口处，不是每个发出点。**
        yield {"event": "thinking", "log": "正在初始化寻址...", "status": "CORE_THINKING", "current_skill": None, "rag_hit": False, "full_file_hit": False}

        # 快路径分流：简单任务不注入额外规则，节省 token
        _registered_count = len(self._skill_names(self.registry.get_all_manifests()))
        _CHAIN_WORDS = {"先", "再", "然后", "接着", "之后", "最后", "并且", "同时", "分别"}
        _is_short_simple = len(query.strip()) < 15 and not any(w in query for w in _CHAIN_WORDS)
        # 原本这里还有第三个条件 _classifier_says_simple（要求 primary_conf >= 0.6），
        # 分类器删掉后 primary_conf 恒为 0.0，它恒为 False，在 or 链里等于不存在。移除。
        _force_fast_path = (
            _registered_count < 1
            or _is_short_simple
        )
        system_guide = base_guide
        if not _force_fast_path:
            logger.debug(f"[Router] 慢路径: skills={_registered_count}, query_len={len(query.strip())}")

        # 未决交互：让模型看见"有个问题挂在那里"。
        # 放在最前面是有意的——它决定这一轮该不该走 answer_open_interaction，
        # 属于路由级信息，比 session_log/episodic 那些背景资料优先级高。
        _interaction_injection = self._build_open_interactions_injection()
        if _interaction_injection:
            system_guide = system_guide + _interaction_injection
        # ⭐ 选中文字的引用。**独立于未决交互那段** —— 没有待办时它也要出现，
        #    而那一段在没有待办时会直接返回空串。
        #    📌 两件事共用一个入口的话，「没有待办」会顺手把「用户指了一句话」也吃掉。
        _quote_injection = self._build_quoted_selection_injection()
        if _quote_injection:
            system_guide = system_guide + _quote_injection
        # ⭐ 进行中的「一件事」：让模型看见它在替什么东西负责。
        # ⚠️ 这是「边界由模型判」的**前提** —— 不给它看，`task_boundary` 就是个
        #    要它凭空猜的工具。同 「终止事实要传给模型」：
        #    📌 **要模型做判断，就得先让它看见判断所需的事实。**
        # ⭐ 紧跟未决交互之后是有意的：两段都是「有东西挂在你名下」的路由级信息，
        #    而 `task_boundary` 的可见性正是由这段话决定的。
        _work_injection = _rt_ongoing_work(self)
        if _work_injection:
            system_guide = system_guide + "\n\n" + _work_injection
        # ⭐ 还在跑的后台任务。
        # ⚠️ **必须现在就接上，不许「先写好等以后用」** ——
        #    核时刚抓到 `mcp_client.awareness_lines` 的教训：
        #    它写得完全正确、docstring 还写明「给 xxx 用」，而**零调用方**，
        #    于是「MCP 按需加载」这件事看起来做了、实际一直没做。
        # 📌 **一个写好但没人调的函数，比没写更坏** ——
        #    没写时缺口是可见的，写了不接时缺口**看起来已经补上了**。
        _bg_injection = _rt_background_jobs(self)
        if _bg_injection:
            system_guide = system_guide + "\n\n" + _bg_injection
        # ⭐ [A/B] 授权与能力的现状。⚠️ **必须现在就接上，不许「先写好等以后用」**
        #    —— `mcp_client.awareness_lines()` 那笔账的形状（写好、零调用方、
        #    于是缺口看起来已经补上了）。
        _auth_injection = _rt_authorization_state(self)
        if _auth_injection:
            system_guide = system_guide + "\n\n" + _auth_injection
        # 上一个没能部署的 Skill + 它的校验报错（2026-08-05 实测）。
        # 放动态段而不是 memory：memory 会被 max_turns=10 截断，
        # 而用户往往隔很多轮才说"上次写的校验报错，重新写"。
        _audit_injection = self._build_audit_failure_injection()
        if _audit_injection:
            system_guide = system_guide + _audit_injection
        # 注入 session_log,让模型知道本次会话做过什么
        _session_injection = self._build_session_log_injection()
        if _session_injection:
            system_guide = system_guide + _session_injection
        # Episodic: 注入跨会话历史摘要
        _episodic_injection = self._build_episodic_injection()
        if _episodic_injection:
            system_guide = system_guide + _episodic_injection
        # Ambient Memory：注入"当前工作现场"，让 Nano 默认知道用户刚才在干嘛
        _ambient_injection = self._build_ambient_injection()
        if _ambient_injection:
            system_guide = system_guide + _ambient_injection
        # ⭐ [2026-08-25] 窗口形态（mini / full）—— 见 `_build_window_mode_injection`。
        #    ⚠️ **必须现在就接上**，不许「先写好等以后用」（`awareness_lines()` 那笔账）。
        # ⭐⭐⭐ 记忆摘要无条件注入 —— 见 `_build_memory_injection`。
        #    📌 这一行就是 用户那句「cc 每次都注入了摘要」的落地。
        system_guide = system_guide + self._build_memory_injection()
        system_guide = system_guide + self._build_window_mode_injection()
        # 情感系统：Mood 染语气注入被动路径（设计 /）。落在缓存哨兵之后的动态段，
        # 不污染稳定前缀缓存。情感只染语气，绝不降质量、不碰功能（铁律）。
        try:
            from core.proactive.intel.affect import get_affect as _get_affect
            _tone = _get_affect().tone_hint()
            # 固定说明已挪到稳定前缀 [Live Context Blocks]（Stage2），这里只发变化的 tone 值。
            system_guide = system_guide + f"\n\n[Tone] Current tone: {_tone}."
        except Exception:
            pass

        # ── 能力健康 + 系统事件注入（两者都落在缓存哨兵之后的动态段）────────
        # 为什么在动态段而不是稳定前缀：正常情况下这两块都是空串，一个字符都不加；
        # 出故障时才有内容，而那时 manifest 也变了、缓存本来就得重建。放稳定前缀会
        # 让"降级"（不摘工具、不改 manifest）白白打穿缓存。
        #
        # 两者分工：能力健康记【状态】（坏着就一直在），系统事件记【事件】
        # （刚才发生过什么）。一个防模型去调坏工具，一个让用户追问"刚才怎么了"能答上来。
        # 都不写进 MemoryManager —— 后台线程在 tool_calls/tool_results 之间插一条
        # assistant 会撕裂工具事务，代价是当轮硬崩 + 已执行结果被回滚吃掉 + 下一轮 400。
        try:
            from core.health import get_health, get_system_events
            _cap_notice = get_health().render_capability_notice()
            if _cap_notice:
                system_guide = system_guide + _cap_notice
            _sys_events = get_system_events().render()
            if _sys_events:
                system_guide = system_guide + _sys_events
        except Exception as _h_err:
            logger.debug(f"[Health] 注入跳过: {_h_err}")

        # ⭐ 运行作用域 —— 两块，顺序固定，都落在缓存哨兵之后的动态段。
        #
        #   Health current block
        #   RecentSystemEvents          ← 上面那段
        #   [Runtime Restarted]         ← ② 一次性，消费后消失
        #   [Execution Scope]           ← ④ 每轮
        #
        # ⚠️ ② **不塞进 `RecentSystemEvents`**：它的表头写着「除非用户问起否则别用」，
        #    而重启提示恰恰是"没问也必须影响判断"。理由详见 `runtime/identity.py`。
        # ⚠️ ④ 刻意**不重复工具列表** —— 当前 API manifest 已经是工具的权威来源，
        #    再列一遍就是第二份名单。它只说"规则"，所以在一个 turn 内是恒定的，
        #    和 Health 同一处注入即可。
        try:
            from core.runtime import identity as _rt_ident
            _restart_notice = _rt_ident.pending_restart_notice()
            if _restart_notice:
                system_guide = system_guide + _restart_notice
            system_guide = system_guide + self._execution_scope_block()
            system_guide = system_guide + self._image_note_request_block()
            # ⭐⭐⭐ L3 索引条目 —— **无条件注入**，不是"需要时再检索"。
            #
            # 🔴 这一条解掉的是 用户当年那个悖论：
            #    「大模型真的忘记某个东西之后，它不就把『我记过这个东西、
            #      我应该去看笔记』这件事也忘了吗？」
            # ⭐ 参照 `MEMORY.md`：**索引不是被回忆起来的，是被塞进来的。**
            #    只做召回端不够 —— 等发现该记的时候，可能已经没得记了。
            system_guide = system_guide + self._l3_index_block()
            # 上下文压力对模型可见。⚠️ 低于高水位返回空串（见 budget.py 的纪律）。
            try:
                from core.context.budget import pressure_block as _pb
                system_guide = system_guide + _pb(self.provider.target_model)
            except Exception:
                pass
        except Exception as _sc_err:
            logger.debug(f"[F6] 作用域注入跳过: {_sc_err}")

        context = self._build_pipeline_context()

        # 显式思考流：主决策特有的"用户上传文件提示 / 图片"注入——这两个是
        # 用户当轮输入的一部分，只在主决策入口有；protocol 的注入由下面
        # _stream_decision 内部统一处理（_inject_think_protocol），所以这里
        # 只追加 temp_file_hint/image_parts，不在这里加 protocol。
        # 注：只追加到发给模型的 context 副本，不污染 self.memory（原始 query
        # 在这之前已干净存好）。
        import copy
        context = copy.deepcopy(context)
        if temp_file_hint or image_parts:
            for i in range(len(context) - 1, -1, -1):
                item = context[i]
                role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
                if role == "user" and isinstance(item, dict):
                    parts = list(item.get("parts", []))
                    if temp_file_hint:
                        parts.append({"text": temp_file_hint})
                    if image_parts:
                        parts.extend(image_parts)
                    item["parts"] = parts
                    break

        # ── 所有消息统一走 ReAct 主循环（fast-path 已于 0703 合并删除）──────────
        # 闲聊/无工具需求的消息：模型按 ReAct 协议（见 _react_loop_prompt "直接回答"
        # + "闲聊不调工具"约束）第一轮直接出话、不产生 tool_use，渲染与旧 fast-path 一致；
        # 需要工具时正常进多轮。稳定前缀+核心工具 >4096 触发缓存，成本反而更低。

        # ── ReAct 主循环替代旧"一次决策+巨型分支树" ─────────────────────────
        # _run_react_loop 内部自行调用 _stream_decision_core，多轮迭代直到
        # 模型输出最终文字答案或达到最大轮次。
        # temp_file_hint / image_parts 的注入：在第一轮 _stream_decision_core
        # 调用时，context 已经含有它们（通过上面的 context = copy.deepcopy + 注入）。
        # _run_react_loop 第一轮调用 _build_pipeline_context() 会拿到注入后的
        # memory（因为 add_message("user", query) 已经在上面执行），
        # 但 temp_file_hint 不在 memory 里——需要显式传给第一轮。
        # 解决方式：把 temp_file_hint/image_parts 注入到刚写入 memory 的 user 消息上。
        # （这里直接修改 memory.storage 最后一条——user 消息刚被写入，是安全的）
        if temp_file_hint or image_parts:
            _last_user_msg = self.memory.storage[-1] if self.memory.storage else None
            if _last_user_msg is not None and _last_user_msg.role == "user":
                # 构造 list 格式 content，provider/to_dict 会按 Anthropic 多模态格式序列化
                _parts: list = [{"type": "text", "text": _last_user_msg.content or ""}]
                if temp_file_hint:
                    _parts.append({"type": "text", "text": temp_file_hint})
                if image_parts:
                    _parts.extend(image_parts)
                _last_user_msg.content = _parts

        try:
            async for _react_ev in self._run_react_loop(
                tools_manifest=list(self._core_manifest),   # 核心常驻 + load_tools；其余按需加载
                system_guide=system_guide,
                base_guide=base_guide,
                realtime_callback=realtime_callback,
                event_queue=event_queue,
            ):
                yield _react_ev
        except RuntimeError as fatal_err:
            if "API 调用链路全线熔断" in str(fatal_err):
                self._clean_damaged_memory()
                yield {"event": "sys_error", "content": "🚨【熔断】总线API全线枯竭！",
                       "status": "CRITICAL_SYSTEM_HALT", "model": "TOTAL_CRASH",
                       "skills_active": False, "log": "CRITICAL: 底层 API 链路彻底枯竭",
                       "current_skill": None}
                return
            raise fatal_err
        except Exception as core_err:
            logger.critical(f"[ReAct] 主循环异常: {core_err}\n{traceback.format_exc()}")
            self._clean_damaged_memory()
            yield self._get_generic_error_payload(core_err, current_running_skill)
            return
        finally:
            # Always compress image payloads after the turn attempt.
            # This also runs when the API fails before normal completion.
            # User text remains, and the placeholder preserves that images were sent.
            try:
                _compressed_imgs = self.memory.compress_image_blocks()
                if _compressed_imgs:
                    logger.debug(f"[Memory] compressed {_compressed_imgs} image block(s) after ReAct turn")
            except Exception as _img_compact_err:
                logger.debug(f"[Memory] post-turn image compression skipped: {_img_compact_err}")

            # ⭐ L0→L1：把老交换的工具结果换成占位符。
            #
            # ⚠️⚠️ **默认关闭**（`data/model_config.json` 的 `_settings.ladder_enabled`）。
            #    这是第一个**真的从模型上下文里拿走东西**的动作，必须由 用户显式打开。
            #    📌 **引入一个机制和启用一个机制是两件事** —— 一起做，出了问题
            #       分不清是机制的锅还是接线的锅（同「立架子和搬家具是两件事」）。
            #
            # ⚠️ 放在**这一轮结束之后**（和图片压缩同一个 `finally`）：
            #    衰减只碰 **closed exchange**，而"当前这一轮"到这里才算关闭。
            #    📌 在轮内降级 = 可能在 tool_result 还没回来时就把它换成占位符。
            try:
                from core.models import ladder_enabled as _ladder_on
                if _ladder_on():
                    from core.context.decay import run_l0_to_l1 as _run_l1
                    from core.context.decay_store import DecayStore as _DS
                    from core.runtime.kernel import get_kernel as _gk
                    _sid = self.memory.conversation_session_id
                    if _sid:
                        _dstore = _DS(_gk().store)
                        _run_l1(self.memory, _dstore, _sid, self.provider.target_model)
                        # ⚠️ L1→L2 **过提炼器**（要花钱、要等秒级），所以排在 L0→L1
                        #    之后：先把免费的那一档降完，可能就不需要走这一档了。
                        #    📌 **贵的动作永远排在便宜的动作后面** ——
                        #       不然你会为一件本来不必做的事付钱。
                        from core.context.decay import run_l1_to_l2 as _run_l2
                        await _run_l2(self.memory, _dstore, self.provider, _sid,
                                      self.provider.target_model)
                        # L2→L3：交接给语义记忆 + 生成索引条目 + 改档。
                        # ⚠️ 不过 LLM（结论行已经在库里），但它是**唯一用户有感**的箭头。
                        from core.context.decay import run_l2_to_l3 as _run_l3
                        _run_l3(self.memory, _dstore, _sid, self.provider.target_model)
                        # L3→L4：索引条目过期。⚠️ **不删语义记忆** ——
                        # 📌 遗忘 = 不再自动想起，不等于抹掉。
                        from core.context.decay import run_l3_to_l4 as _run_l4
                        _run_l4(self.memory, _dstore, _sid, self.provider.target_model)
                        # 🔴🔴 **最后把投影按权威重建一次** —— 四档只改
                        #    `exchange_decay`（权威），**没有一个会去动 `storage`**。
                        #    不做这一步的后果/抓到的最大那条）：
                        #
                        #        exchange_decay 说：这段已经 L2 / 已经移出去了
                        #        storage（真正发给模型的）说：原文还背在身上
                        #
                        #    —— 日志说省了、其实没省；L3 更怪：**要等重启才真的忘掉**。
                        # ⭐ 用的就是重启那条路径的同一个函数，所以 live 与 hydrate
                        #    **不可能漂开**（📌 各写一遍的话，它们只在"我两次都想对了"
                        #    的前提下相等）。
                        from core.context.decay import rebuild_projection as _reproj
                        _reproj(self.memory, _dstore, _sid)
                        # ⚠️ 投影已经服从权威了，**这时候才轮到 UI**。
                        #    📌 UI 与模型必须看到同一件事：模型侧移出去了而聊天区还显示，
                        #       用户就会接着问"你刚才说的那个"。
                        _cb = getattr(self, "_on_decay_applied", None)
                        if callable(_cb):
                            try:
                                _cb()
                            except Exception as _ui_err:
                                logger.debug(f"[Decay] 通知 UI 同步失败: {_ui_err}")
            except Exception as _decay_err:
                # ⚠️ 治理层的故障**绝不许**把对话搞挂 —— 不降级只是上下文厚一点。
                logger.warning(f"[Decay] L0→L1 跳过（不影响对话）: {_decay_err}")

        logger.debug(f"[ReAct] 常规路径完成")

    async def _handle_search_mcp_registry(self, args: dict, aid: str, *,
                                           event_queue=None, used_model: str = "",
                                           **_ctx) -> str:
        """查 Official MCP Registry。**只读，不改任何东西。**

        ⚠️ 返回的是给模型的**候选清单**，不是结论 —— 判断留给模型和用户。
           早先定过：`source = OFFICIAL_MCP_REGISTRY` 不等于 `trust = SAFE`。
        ⚠️ 查不到 / 查不通 一律**如实说是哪一种**，不合并成「没有」——
           📌 「读不懂」「连不上」「确实没有」是三件事，压成一件会让模型
              向用户报告一个假的空结果。
        """
        from core.mcp_discovery import search_registry
        _ok, _txt = await search_registry(args.get("query") or "",
                                          args.get("limit") or 8)
        return _txt

    async def _handle_manage_mcp_decision(
        self,
        decision,
        used_model: str,
        base_guide: str,
        realtime_callback,
    ):
        """`manage_mcp` 元工具 —— **形状照抄 `_handle_manage_existing_skill_decision`**。

        📌 用户对「关掉那个东西」的心智，在 Skill 和 MCP 上是同一个 ——
           两套交互只会把多出来的复杂度落在用户身上。

        ⚠️ 与 Skill 的三处**必要**差异（不是随意偏离）：
          ① 多一个 `retry` —— MCP 多一个 Skill 没有的状态：**连不上**。
             📌 「连不上」和「被禁用」对用户是两件事：前者说「再试试」，
                后者说「要不要打开」。合成一个操作等于让用户猜现在是哪种。
          ② `enable` / `disable` / `retry` **立即生效、不走二次确认** ——
             它们可逆，而 Skill 那边同样只对 delete 要确认。
             🔴 `delete` 会改配置文件、**不可逆** ⇒ 与 Skill 一致走
                自然语言二次确认（`_request_mcp_management_confirmation`），
                **不弹窗**。弹窗只在用户走 UI 按钮删除时出现。
          ③ 存在性校验查的是**配置**而不是"已连接" ——
             📌 一个被禁用、或正连不上的 server **依然存在**；
                拿"连上了没有"当"存不存在"，就会对着一个真实存在的东西说它不存在。
        """
        _args = decision.args or {}
        _op = (_args.get("operation") or "").strip().lower()
        _srv = (_args.get("server_name") or "").strip()

        # ⚠️ 同义词兜底 —— **不是给某个模型打的补丁**。
        #    「重连 / retry / restart」对人和模型都指同一件事；一个只认其中一个
        #    拼法的接口，是接口自己的问题。
        #    📌 与「不留专门给某个模型的修正动作」不冲突：那条禁的是迁就某个模型的
        #       **怪癖**，而这里任何人、任何模型都会在这几个词之间摇摆。
        #    ⚠️ 只收**真同义**的；`connect` / `start` / `stop` 故意不收 ——
        #       它们指向别的东西（connect_mcp 是新增，不是重连），猜错比不猜更坏。
        _SYNONYM = {
            "retry": "reconnect", "restart": "reconnect", "re-connect": "reconnect",
            "on": "enable", "turn_on": "enable", "activate": "enable",
            "off": "disable", "turn_off": "disable", "deactivate": "disable",
            "remove": "delete", "uninstall": "delete",
        }
        _op = _SYNONYM.get(_op, _op)
        _OPS = {"enable", "disable", "delete", "reconnect"}

        # ⭐⭐ **这个 handler 一句中文都不自己说。**
        #
        # 全部出口走 `exit_flow_defer_to_model`：只把**英文事实**交回主 ReAct，
        # 由模型用它自己的话（用户当前的语言 + 当前的人格）说出来。
        #
        # 🔴 第一版另造了个 `_bail`，每个出口写死一句中文，2026-08-28
        #    当场抓到其中一句。而这个机制 **2026-08-06 就存在了** ——
        #    照抄了 Skill 管理那套的**形状**，却没查那个形状本身对不对。
        #    📌 **照抄一个形状之前，先确认那个形状本身是对的。**
        #
        # 🔴🔴 走 defer 的路径**绝对不能自己 `add_tool_call`** ——
        #    拦截处（`if _defer_to_model:`）会无条件写一次。写两次 →
        #    tool_use / tool_result 配对损坏 → 下一轮直接 400。
        #    ⇒ 所以原来那句「始终先写 tool_call」**整条删掉**，不是挪位置。
        #    （既有两处正确用法所在的函数里，`add_tool_call` 出现 **0 次** —— 已核。）
        def _defer(facts: str, log: str):
            return {"event": "exit_flow_defer_to_model", "tool_result": facts, "log": log}

        if _op not in _OPS:
            yield _defer(
                f"manage_mcp did NOT run: operation {_op!r} is not one of the four valid "
                f"operations (enable, disable, delete, reconnect). Nothing was changed. "
                f"If you can tell which one the user meant, call manage_mcp again with that "
                f"exact value; otherwise ask the user which one they want.",
                "manage_mcp operation 非法，措辞交回模型。")
            return

        try:
            from core.mcp_client import MCPManager as _MM
            _mgr = _MM.instance()
        except Exception as e:
            yield _defer(
                f"manage_mcp could not run: the MCP subsystem is unavailable ({e}). "
                f"Nothing was changed. Tell the user this did not go through; do not retry.",
                "manage_mcp: MCPManager 不可用，措辞交回模型。")
            return

        # ── 存在性校验：查【配置】，不查"连上了没有" ──────────────────
        # ⚠️ `owned_by` 非空的**不算在内** —— 它不是用户的外接能力，是某个 Skill
        #    的内部零件。列进来的话，模型会拿它当一个可管理的候选去猜。
        _all = getattr(_mgr, "servers", {}) or {}
        _known = [n for n, _s in _all.items()
                  if not str(getattr(_s, "owned_by", "") or "").strip()]

        # 🔴 撞到零件：四个动作全拒。
        #    ⚠️ 但**不能说"没有这个 server"** —— 那是撒谎，它确实存在，
        #       而且模型下一步会去"创建"或"重新接入"一个已经在的东西。
        #    📌 拒绝要给真实理由，否则模型只会换个姿势再试一次。
        _target = _all.get(_srv)
        _owner = str(getattr(_target, "owned_by", "") or "").strip() if _target else ""
        if _owner:
            yield _defer(
                f"manage_mcp will not act on {_srv!r}: it is not a user-managed MCP server, "
                f"it is an internal component of the Skill {_owner!r}. It starts on demand and "
                f"stops on its own; showing as 'not connected' is normal, not a failure. "
                f"Nothing was changed and nothing needs to be. If the user is having trouble "
                f"with what {_owner} does, talk about that Skill - not about this component.",
                f"manage_mcp 拒绝对内部零件「{_srv}」动手（属于 {_owner}）。")
            return

        if not _srv or _srv not in _known:
            # ⭐ 把**配置里真实存在的名字**一起交回去 —— 模型据此能自己改名重试，
            #    不必回头问用户「你说的是哪个」。
            yield _defer(
                f"manage_mcp did NOT run: there is no MCP server named {_srv!r}. "
                f"The servers that actually exist are: {_known}. Nothing was changed. "
                f"If one of those is clearly what the user meant, call manage_mcp again with "
                f"that exact name; otherwise ask the user which one.",
                "manage_mcp 目标不存在，措辞交回模型。")
            return

        # ── delete：不可逆 ⇒ 自然语言二次确认（与 Skill 一致，不弹窗）──
        if _op == "delete":
            # ⚠️ 返回值（那句固定中文）**故意不再使用** —— 只留它的副作用：
            #    把待确认登记成 Interaction（PERSISTED，跨重启存活 + 模型看得见）。
            #    ⚠️ 待办条目自身的展示文本目前仍是固定中文，属 的范围，本次不动。
            self._request_mcp_management_confirmation(_op, _srv)
            yield _defer(
                f"Deleting MCP server {_srv!r} is NOT done yet - it needs the user's explicit "
                f"confirmation first, because it edits the local configuration and cannot be "
                f"undone. A pending confirmation has been registered. Ask the user to confirm "
                f"or cancel, in your own words. Do not call manage_mcp again for this.",
                f"等待用户确认 delete MCP「{_srv}」，措辞交回模型。")
            return

        # ── enable / disable / reconnect：可逆，立即执行 ────────────────
        try:
            if _op == "enable":
                await _mgr.set_enabled(_srv, True)
                _facts = (f"MCP server {_srv!r} is now enabled and is connecting. "
                          f"Its tools become usable once the connection is up.")
            elif _op == "disable":
                await _mgr.set_enabled(_srv, False)
                _facts = (f"MCP server {_srv!r} is now disabled. Its tools are unavailable "
                          f"until it is enabled again.")
            else:
                # ⚠️ 内部 API 仍叫 `retry_server` —— 对模型暴露的动词是 `reconnect`。
                #    📌 内部命名和对外契约不必一致；**对外那个要贴用户的说法**。
                await _mgr.retry_server(_srv)
                _facts = f"A reconnect was triggered for MCP server {_srv!r}."
        except Exception as e:
            logger.warning(f"[B3] manage_mcp {_op} {_srv} 失败: {e}")
            yield _defer(
                f"manage_mcp {_op} on {_srv!r} failed: {type(e).__name__}: {e}. "
                f"Nothing was changed. Tell the user what failed, in your own words.",
                f"manage_mcp {_op} 失败，措辞交回模型。")
            return

        # ⭐ 感知：Nano 自己动的手，也要落进本次会话的事实账
        #    📌 与用户手点 UI 那条**同一个出口** —— 见 `_note_mcp_change`。
        self._note_mcp_change(_op, _srv, by="nano")
        yield _defer(
            _facts + " This already succeeded - do not call manage_mcp again for it. "
                     "Just tell the user, in your own words.",
            f"manage_mcp {_op}「{_srv}」完成，措辞交回模型。")

    async def _handle_connect_mcp_decision(
        self, decision, used_model: str, base_guide: str, realtime_callback,
    ):
        """`connect_mcp` —— 接入一个新 MCP，**必须先经用户批准**。

        ⚠️⚠️ **这个弹窗无视 auto 模式**。理由不是
           「MCP 特殊」或「频率低」——那种理由是**例外**，而例外会被下一个人
           问「那为什么别的不例外」。真正的理由是**它不在 auto 管辖的维度上**：

               能力开关 答「这项能力开不开放」
               auto      答「开放了的，要不要逐个授权」
               这个弹窗 答「**这个第三方是什么东西**」   ← 第三个维度

           📌 **auto 从来没有承诺「不给你看东西」，它承诺的是「不用你逐个点同意」。**
           而接入 MCP 时缺的不是授权，是**信息**：用户说「帮我接入 X」时，
           动作与意图完全对齐（命令分类器会放行），但 **X 是什么，用户不知道**。
        🔴 而且早先的设计写死了一条不能豁免的：对 stdio，「连上」本身就是在本机
           执行第三方代码 ⇒ 这个弹窗是**执行第三方代码前的最后一道**，
           auto 豁免它 = 那道就不存在了。

        ⭐ 流程严格是：**只解析 → 弹窗 → 用户批准 → 才写配置、才启动**。
           `preview_server_json` 一行第三方代码都不执行，一个字都不写盘。
        """
        _args = decision.args or {}
        _cfg_txt = (_args.get("config_json") or "").strip()
        _purpose = (_args.get("purpose_line") or "").strip()
        _what = (_args.get("what_it_does") or "").strip()

        # ⚠️ **不写 `add_tool_call`** —— 本 handler 所有出口都走
        #    `exit_flow_defer_to_model`，拦截处会无条件写一次。写两次 →
        #    tool_use / tool_result 配对损坏 → 下一轮 400。（同 manage_mcp 那条）
        def _defer(facts: str, log: str):
            return {"event": "exit_flow_defer_to_model", "tool_result": facts, "log": log}

        try:
            from core.mcp_client import MCPManager as _MM
            _mgr = _MM.instance()
        except Exception as e:
            yield _defer(
                f"connect_mcp could not run: the MCP subsystem is unavailable ({e}). "
                f"Nothing was added. Tell the user this did not go through.",
                "connect_mcp: 管理器不可用，措辞交回模型。")
            return

        _ok, _info = _mgr.preview_server_json(_cfg_txt)
        if not _ok:
            yield _defer(
                f"connect_mcp did NOT run: that MCP configuration could not be parsed - {_info}. "
                f"Nothing was added and the user was not asked anything. "
                f"Explain the problem to the user, or ask them for a corrected config.",
                "connect_mcp: 配置无效，措辞交回模型。")
            return

        # ⭐ 弹窗事件。**刻意用一个新事件名**而不是复用 os_action_confirm /
        #    execution_confirm —— 那两个都会被 `_auto_on()` 自动放行，
        #    而这一类**不许被 auto 豁免**。
        #    📌 复用一个「会被豁免」的通道去表达「不许豁免」，是自相矛盾的接线。
        # ⚠️ 等待走**既有合同** `inbox.wait_confirm_or_user_message(event, timeout)` ——
        #    📌 它同时处理三种结局：用户点了、用户改口说别的、干等超时。
        from core.runtime import inbox as _ib
        _confirm_ev = asyncio.Event()
        _approved = {"v": False}

        def _on_ok():
            _approved["v"] = True
            _confirm_ev.set()

        def _on_no():
            _approved["v"] = False
            _confirm_ev.set()

        yield {"event": "mcp_connect_confirm",
               "info": _info, "purpose_line": _purpose, "what_it_does": _what,
               "on_confirm": _on_ok, "on_cancel": _on_no}

        _oc = await _ib.wait_confirm_or_user_message(_confirm_ev, 300)
        if _oc != _ib.ConfirmOutcome.CONFIRMED or not _approved["v"]:
            # 🔴 三种"没接入"**分开交给模型** —— 📌 对模型下一步的含义完全不同：
            #    用户明确拒绝 → 别再提；改口说别的 → 先答那件事；超时 → 可以再问一次。
            # ⚠️ 上一版这三种区别只进了 `log`（用户和模型都看不到），
            #    模型收到的 tool_result 是 `MCP not installed: ConfirmOutcome.TIMEOUT`
            #    这样的 enum repr。📌 **注释里写着的设计，不等于落地了的设计** ——
            #    这次是把那条注释真正接上。
            _nm = _info.get("name", "")
            _facts = {
                _ib.ConfirmOutcome.USER_MESSAGE: (
                    f"MCP server {_nm!r} was NOT added: the user said something else while the "
                    f"approval dialog was open, so it was dismissed. Nothing was installed. "
                    f"Answer what they just said first; only come back to this if they ask."),
                _ib.ConfirmOutcome.TIMEOUT: (
                    f"MCP server {_nm!r} was NOT added: the approval dialog timed out with no "
                    f"answer. Nothing was installed. You may offer once more, briefly."),
            }.get(_oc, (
                f"MCP server {_nm!r} was NOT added: the user declined it. Nothing was installed. "
                f"Accept that and move on - do not offer it again unless they bring it up."))
            yield _defer(_facts, f"connect_mcp 未接入（{_oc}），措辞交回模型。")
            return

        # ── 批准之后才动真格：写配置 → 连接 → 取工具清单 → 生成说明 ──
        _ok2, _name = _mgr.add_server_from_json(_cfg_txt)
        if not _ok2:
            yield _defer(
                f"connect_mcp failed while writing the configuration: {_name}. "
                f"The user had already approved, but nothing was installed. "
                f"Tell them what failed.",
                "connect_mcp: 写配置失败，措辞交回模型。")
            return
        _srv = _info["name"]
        try:
            await _mgr.retry_server(_srv)
        except Exception as e:
            logger.warning(f"[B3] 接入后连接 {_srv} 失败: {e}")

        self._note_mcp_change("add", _srv, by="nano")
        _tools = []
        try:
            _s = _mgr.servers.get(_srv)
            _tools = [t.get("name", "") for t in (getattr(_s, "tools", []) or [])]
        except Exception:
            pass

        # ⭐ tooltip：**优先问 server 自己**，模型写的那句只是兜底。
        #    📌 早先的设计：「这个机制其实能同时覆盖 ①②③」——
        #       让「模型凭任务上下文写简述」退化成拿不到工具清单时的兜底。
        _desc = await self._mcp_tooltip(_srv, _tools, fallback=_what)
        if _desc:
            _mgr.set_description(_srv, _desc)

        # ⭐⭐ **回核** —— 拿真实能力对照授权时说的话。
        #
        # 早先定的链条是三段，前半段只做了前两段：
        #   授权前只读元数据 ✅（preview_server_json）→ 授权后启动取 tools/list ✅
        #   → **拿真实能力回核授权前的描述** ← 🔴 这一段一直缺
        # 缺的后果很具体：用户批准的是 A，装进来的可能是 B，**没有任何东西会发现**。
        #
        # ⭐ 而它几乎不要额外成本：对照用的两样**都已经在手上** ——
        #   `_what`   授权时模型写的那句（弹窗上给用户看过）
        #   `_tools`  tools/list 回来的真实工具名
        #   📌 **回核不需要新机制，只需要把「说过的」和「拿到的」放进同一句话** ——
        #      它们原本分别躺在两个变量里，谁也不认识谁。
        # ⚠️ 判断交给模型而不是写规则：「宣称的能力」和「工具名列表」之间是**语义**
        #    关系。说「读取网页」的 server 提供 fetch_url / get_page 都算相符，
        #    提供 send_email 就不相符 —— 这条线没有算法边界。
        #    而这一轮本来就要 defer 回模型说话 ⇒ **判断塞在同一轮里，零额外调用**。
        #
        # ⚠️ 「装上了」和「连上了」仍然分开说 ——
        #    📌 说成一件事的话，用户会以为能用了，然后发现工具还是不在。
        _claimed = (_what or '').strip()
        _recheck = ''
        if _tools and _claimed:
            _recheck = (
                " Before approving, you told the user this server would: "
                + repr(_claimed)
                + ". What it actually exposes is: " + ", ".join(_tools[:30])
                + ". Check those against each other. If they broadly match, just say "
                "what it can do. If they do NOT match - it does something else, or "
                "far more than described - say so plainly to the user and tell them "
                "they can remove it with manage_mcp. Do not quietly move on."
            )
        elif _tools and not _claimed:
            # ⚠️ 没有宣称过 ⇒ **没得对照**，说清是「没得核」而不是「核过了」。
            #    📌 「没核」和「核过没问题」压成一件事，就等于把一次没做的检查
            #       报告成通过了。
            _recheck = (" There was no description given when this was approved, "
                        "so there is nothing to check it against. Tell the user what "
                        "it actually exposes.")
        yield _defer(
            f"MCP server {_srv!r} was added and approved by the user. "
            + (f"It connected and exposes {len(_tools)} tools: "
               + ", ".join(_tools[:30]) + ". "
               if _tools else
               "It is installed but has not connected yet, so its tools are not "
               "available yet and there is nothing to verify against what was "
               "promised; a reconnect can be tried later. ")
            + _recheck
            + " This already succeeded - do not call connect_mcp again for it. "
              "Tell the user, in your own words.",
            f"connect_mcp 已接入「{_srv}」，措辞交回模型。")

    async def _mcp_tooltip(self, server: str, tool_names: list, *, fallback: str = "") -> str:
        """一句话说明这个 MCP 是干什么的。**先问 server 自己，模型写的只是兜底。**

        ⚠️ 生态标准的 `mcpServers` snippet 里**没有 description 字段** ——
           command/args/env/url 全都不含人话描述。而 `tools/list` 回来的
           每个工具都带 name + description，**数据本来就在手上**。
        ⭐ 所以顺序是：拿真实工具清单 → 让便宜模型压成一句 → 存下来。
           拿不到清单（连不上 / 描述为空）才退回模型凭任务上下文写的那句。
           📌 早先的设计：这个机制**同时覆盖**官方内置、Nano 自主接入、用户手动粘贴三种来源。

        ⚠️ 语言跟随用户界面语言（`language_clause`）—— 与 digest 同一条路。
           📌 而它一旦生成就**存下来不再变**：切换语言后旧内容保持原样，
              这是 已经定过的规矩，不为它单开机制。
        """
        if not tool_names:
            return (fallback or "").strip()[:200]
        try:
            from core.models import distiller_for
            from core.i18n import language_clause
            _mdl = distiller_for(getattr(self, "target_model", "") or "") or None
            _sys = ("You write a one-line description of what an MCP server does, "
                    "based on the names of the tools it exposes. "
                    "One sentence, no more than 25 words, no marketing words. "
                    + language_clause("the description"))
            _usr = f"MCP server: {server}\nIts tools: " + ", ".join(tool_names[:30])
            _txt, _ = await self.provider.chat_without_tools(
                [{"role": "user", "content": _usr}], _sys,
                model_override=_mdl, max_tokens=120)
            _txt = " ".join(str(_txt or "").split())
            return _txt[:200] or (fallback or "").strip()[:200]
        except Exception as e:
            logger.warning(f"[B3] tooltip 生成失败，退回模型写的那句: {e}")
            return (fallback or "").strip()[:200]

    def _request_mcp_management_confirmation(self, op: str, server: str) -> str:
        """MCP 删除的自然语言二次确认 —— 形状照抄 Skill 那条。

        ⚠️ 同样登记成 Interaction：**跨重启存活 + 模型看得见**。
           📌 只挂 `_pending_action` 的话，重启后那个待确认会蒸发，
              而用户以为自己还欠一句「确认」。
        ⚠️ 文案里**不预先解释这个 MCP 是干什么的** —— 2026-08-28：
           用户主动要删，说明他知道那是什么；真不确定时会问，而那时 Nano 答得出来。
           📌 **数据要有，不代表要预先展示**；替用户预设一个他不会有的困惑，
              只会让每一次确认都变长。
        """
        self._pending_action = {"op": op, "mcp": server}
        self._pending_action_at = time.time()
        _name = {"delete": "删除"}.get(op, op)
        _msg = (f"将要{_name} MCP 服务「{server}」。"
                f"这会改动本地配置且不可撤销，请回复确认继续，或回复取消。")
        try:
            _rt_open_mcp_manage(self, op, server, _msg)
        except Exception as e:
            logger.warning(f"[B3] MCP 管理待办登记失败（不影响本次确认）: {e}")
        return _msg

    def _note_mcp_change(self, op: str, server: str, *, by: str) -> None:
        """MCP 被增删改这件事，**两条来路共用的唯一出口**。

        🔴 `by` 有两个取值，而它们**不能合并**：
             "nano"  Nano 自己调 `manage_mcp` 动的
             "user"  用户在设置里手点的
           📌 对模型来说这是两件事：前者是它自己做的（它知道），
              后者是**环境变了**（它必须被告知，否则下一轮还当那个工具在）。
              ——同 OS 授权那条判据：「用户亲自点了同意」和「auto 替用户点了」
                对模型是两件事，复用同一个回调这个区别就消失了。

        ⚠️ 写进 session log 而不是只发个事件：📌 事件是**当轮**的，
           而「这个 MCP 已经没了」是**后续每一轮**都要知道的事实。
        """
        _verb = {"enable": "启用", "disable": "停用", "delete": "删除",
                 "retry": "重连", "add": "接入"}.get(op, op)
        _who = "Nano" if by == "nano" else "用户"
        try:
            self._session_log_append(f"{_who}{_verb}了 MCP 服务「{server}」")
        except Exception as e:
            logger.warning(f"[B3] MCP 变更未能记入 session log: {e}")

    async def _handle_manage_existing_skill_decision(
        self,
        decision,
        used_model: str,
        base_guide: str,
        realtime_callback,
    ):
        """manage_existing_skill 元工具处理逻辑（删除/禁用/启用）。

        取代旧前置分类器硬路由。模型在主决策里判断用户确实要管理某个 Skill
        时才调用本工具，目标存在性校验后挂 pending_action，走原有二次确认流。
        """
        _args = decision.args or {}
        _op = (_args.get("operation") or "").strip().lower()
        _skill = (_args.get("skill_name") or "").strip()

        _OP_VALID = {"delete", "disable", "enable"}

        # ⚠️ 同义词兜底 —— 同 `manage_mcp` 那条：禁的是迁就某个模型的**怪癖**，
        #    而 "remove / turn off / activate" 对任何人任何模型都是真同义词。
        _op = {"remove": "delete", "uninstall": "delete", "del": "delete",
               "off": "disable", "turn_off": "disable", "deactivate": "disable",
               "on": "enable", "turn_on": "enable", "activate": "enable"}.get(_op, _op)

        # ⭐⭐ 措辞全部交给模型（`exit_flow_defer_to_model`）——
        #    2026-08-28 在 MCP 那边抓到同样的问题，这条线一起清。
        # 🔴 **注意 `add_tool_call` 的位置**：走 defer 的分支绝不能自己写
        #    （拦截处会写一次，写两次 → tool_use/tool_result 配对损坏 → 400）；
        #    而仍走 `final_result` 的那一个分支**必须自己写**。
        #    ⇒ 所以原来函数开头那句「始终先写 tool_call」被拆散到具体分支里。
        #    📌 「始终先写」在只有一种出口时是对的，多一种出口它就成了 bug。
        def _defer(facts: str, log: str):
            return {"event": "exit_flow_defer_to_model", "tool_result": facts, "log": log}

        # operation 非法
        if _op not in _OP_VALID:
            yield _defer(
                f"manage_existing_skill did NOT run: operation {_op!r} is not one of "
                f"delete, disable, enable. Nothing was changed. If you can tell which one "
                f"the user meant, call it again with that value; otherwise ask them.",
                "manage_existing_skill operation 非法，措辞交回模型。")
            return

        # 目标未指定
        if not _skill:
            yield _defer(
                "manage_existing_skill did NOT run: no Skill name was given, so there is "
                "nothing to act on. Nothing was changed. Ask the user which Skill they mean "
                "(you can list the Skills you already know about).",
                "manage_existing_skill 目标未确定，措辞交回模型。")
            return

        # 存在性校验：disable/delete 必须是已注册 Skill；enable 目标可能在 disabled 目录
        _known = set(self.registry.skills.keys())
        try:
            if hasattr(self.registry, "list_disabled_skills"):
                _known |= set(self.registry.list_disabled_skills())
        except Exception:
            pass
        if _skill not in _known:
            # ⚠️ 这一支**保持原样、不改 defer** —— 它已经是模型生成的措辞
            #    （`_skill_not_found_reply` 会查磁盘备份目录和最近事件，
            #      再让模型据实说），**本来就不违反固定文案那条**。
            #    📌 本次范围是「把写死的文案改掉」，不是「把所有出口统一成 defer」。
            #    ⭐ 若将来真要统一：把它的事实收集拆成纯函数交给 defer，
            #       能顺带省掉那一次单独的模型调用。本次不扩范围。
            self.memory.add_tool_call(
                decision.name, _args, tool_use_id=decision.tool_use_id,
                thinking_blocks=getattr(decision, "thinking_blocks", None),
            )
            _nf_content, _nf_model = await self._skill_not_found_reply(_skill, base_guide, realtime_callback)
            self.memory.add_tool_result(decision.name, f"Target Skill '{_skill}' does not exist.")
            self.memory.add_message("assistant", _nf_content)
            yield {"event": "final_result", "content": _nf_content, "model": _nf_model,
                   "status": "SYS_IDLE", "log": "manage_existing_skill 目标不存在，已事实核查。",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False}
            return

        # 挂 pending_action，走原有二次确认流
        # ⚠️ 返回值（那句「将要删除 Skill「xxx」…」）**故意不再使用** ——
        #    只留它的副作用：设 `_pending_action` + 登记 Interaction。
        #    📌 2026-08-28 专门问过这句是不是固定文案：**是**，现在不再出现在气泡里。
        #    ⚠️ 待办条目自身的展示文本仍是那句固定中文，属 范围，本次不动。
        self._request_management_confirmation(_op, _skill)
        _WHAT = {"delete": "Deleting", "disable": "Disabling", "enable": "Enabling"}[_op]
        yield _defer(
            f"{_WHAT} the Skill {_skill!r} is NOT done yet - it needs the user's explicit "
            f"confirmation first, because it is a local file-level operation. A pending "
            f"confirmation has been registered. Ask the user to confirm or cancel, in your "
            f"own words. Do not call manage_existing_skill again for this.",
            f"等待用户确认 {_op} Skill「{_skill}」，措辞交回模型。")

    async def _handle_update_existing_skill_decision(
        self,
        decision,
        query: str,
        used_model: str,
        base_guide: str,
        realtime_callback,
    ):
        """update_existing_skill 元工具的共用处理逻辑。
        主入口和文件链尾均调用此方法，保证两条路径保护一致：
        - 目标兜底（last_called → last_deployed → 唯一）
        - 存在性校验（调用 _skill_not_found_reply 事实核查）
        - 规则文件预读 + 冲突检测（冲突时不挂 pending_action，直接问用户）
        - 无冲突时挂起 pending_action 等待确认
        """
        _upd_args = decision.args or {}
        _upd_skill = (_upd_args.get("skill_name") or "").strip()
        _upd_summary = (_upd_args.get("change_summary") or "").strip()

        # target 兜底解析
        if not _upd_skill:
            _all_skills = list(self.registry.skills.keys())
            if self._last_called_skill and self._last_called_skill in _all_skills:
                _upd_skill = self._last_called_skill
                logger.info(f"[UpdateSkill] skill_name 为空，兜底 → 最近调用: {_upd_skill}")
            elif self._last_deployed_skill and self._last_deployed_skill in _all_skills:
                _upd_skill = self._last_deployed_skill
                logger.info(f"[UpdateSkill] skill_name 为空，兜底 → 最近部署: {_upd_skill}")
            elif len(_all_skills) == 1:
                _upd_skill = _all_skills[0]
                logger.info(f"[UpdateSkill] skill_name 为空，兜底 → 唯一 Skill: {_upd_skill}")

        if not _upd_skill:
            self.memory.add_tool_call(decision.name, _upd_args, tool_use_id=decision.tool_use_id, thinking_blocks=getattr(decision, "thinking_blocks", None))
            yield {"event": "exit_flow_defer_to_model",
                   "tool_result": (
                       "The user seems to want to modify an existing Skill, but which "
                       "one could not be determined - the name was not given and there "
                       "is more than one candidate. Nothing was changed. Ask them which "
                       "Skill they mean; you can list the ones you know about."),
                   "log": "等待用户指定要修改的 Skill。"}
            return

        # 存在性校验
        if _upd_skill not in self.registry.skills:
            _nf_content, _nf_model = await self._skill_not_found_reply(_upd_skill, base_guide, realtime_callback)
            self.memory.add_tool_call(decision.name, _upd_args, tool_use_id=decision.tool_use_id, thinking_blocks=getattr(decision, "thinking_blocks", None))
            self.memory.add_tool_result(decision.name, f"Target Skill '{_upd_skill}' does not exist.")
            self.memory.add_message("assistant", _nf_content)
            yield {"event": "final_result", "content": _nf_content, "model": _nf_model,
                   "status": "SYS_IDLE", "log": "update_existing_skill 目标不存在，已事实核查。",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False}
            return

        # 规则文件预读
        _upd_rule_ctx = ""
        try:
            _kb_files = rag_engine.list_knowledge_files()
            _rule_kws = ("规则", "标准", "政策", "定义", "规定")
            _rule_cands = [
                f["filename"] for f in _kb_files
                if isinstance(f, dict) and any(k in f.get("filename", "") for k in _rule_kws)
            ][:2]
            _rule_texts = []
            for _rfn in _rule_cands:
                try:
                    _rtxt = rag_engine.load_full_file(_rfn)
                    if _rtxt:
                        _rule_texts.append(f"[{_rfn}]\n{_rtxt[:2000]}")
                except Exception:
                    pass
            if _rule_texts:
                _upd_rule_ctx = (
                    "\n\n[Possibly Related Rule Documents From KB — Already Loaded]\n"
                    + "\n\n".join(_rule_texts)
                )
        except Exception:
            _upd_rule_ctx = ""

        # 生成修改摘要：有规则文件时额外做冲突核查
        _upd_summary_resp = _upd_summary
        _upd_model = used_model
        if _upd_rule_ctx:
            _summary_guide = (
                base_guide
                + f"\n\nThe user wants to modify the deployed Skill \"{_upd_skill}\".\n"
                + f"Original user request: \"{query}\"\n"
                + f"Model-interpreted change summary: \"{_upd_summary}\"\n"
                + _upd_rule_ctx
                + "\n\nIn one sentence, no more than 60 Chinese characters or 40 English words, "
                  "confirm how you plan to change this Skill's code logic.\n"
                  "Important: if the concrete numbers or concepts in change_summary conflict with the KB rule documents, "
                  "you must explicitly state the difference and ask the user which standard to use. "
                  "Do not decide by yourself.\n"
                  "If there is no conflict, directly restate the change_summary.\n"
                  "Use the user's language when obvious."
            )
            try:
                _upd_summary_resp, _upd_model, _ = await self.provider.chat_without_tools_or_call(
                    self._build_pipeline_context(), _summary_guide,
                    status_callback=realtime_callback, model_override=None,
                )
                _upd_summary_resp = (_upd_summary_resp or "").strip() or _upd_summary
            except Exception:
                _upd_summary_resp = _upd_summary
                _upd_model = "BYPASS_RAW"

        if not _upd_summary_resp:
            _upd_summary_resp = f"修改「{_upd_skill}」（具体改动来自用户原话）"

        # 冲突检测：无论冲突来自"handler 读到的规则文件"还是"文件链已写入 change_summary"，
        # 都需要检测——所以放在 _upd_rule_ctx 条件之外，始终扫描最终摘要
        # "选择"单字太泛（"文件选择功能"等正常摘要会误触发），只保留明确问用户二选一的短语
        _CLARIFY_MARKERS = (
             "请问", "按哪个", "选哪个", "哪个标准", "冲突", "不一致", "您希望", "还是按", "以哪个为准",
             "which standard", "which rule", "which one", "conflict", "inconsistent", "different from",
             "do you want", "should this use", "or the"
             )
        _upd_need_clarify = any(x in _upd_summary_resp for x in _CLARIFY_MARKERS)

        self.memory.add_tool_call(decision.name, _upd_args, tool_use_id=decision.tool_use_id, thinking_blocks=getattr(decision, "thinking_blocks", None))

        if _upd_need_clarify:
            # 规则冲突：不挂 pending_action，直接把澄清问题抛给用户
            self.memory.add_tool_result(decision.name, f"Rule conflict requires user clarification. Target: {_upd_skill}")
            self.memory.add_message("assistant", _upd_summary_resp)
            yield {"event": "final_result", "content": _upd_summary_resp, "model": _upd_model,
                   "status": "SYS_IDLE", "log": "发现规则冲突，等待用户选择标准。",
                   "current_skill": None, "rag_hit": False, "full_file_hit": False}
            return

        # 无冲突：把确认摘要写进 query，确保 SkillWriter 收到的是用户已确认的具体描述
        # 而不只是原始模糊 query（如"按文档改一下"）
        _confirmed_query = (
            f"{query}\n\n"
            f"[change_summary from tool/file chain]\n{_upd_summary}\n\n"
            f"[summary shown to the user and waiting for confirmation]\n{_upd_summary_resp}"
        ).strip()
        self.memory.add_tool_result(decision.name, f"Entered update confirmation flow. Target: {_upd_skill}")
        self._pending_action = {
            "op": "update_skill", "skill": _upd_skill,
            "query": _confirmed_query, "summary": _upd_summary_resp,
        }
        self._pending_action_at = time.time()
        _upd_confirm_content = f"我打算这样修改 Skill「{_upd_skill}」：{_upd_summary_resp}。确认按这个改吗？"
        # 见 `_request_management_confirmation` 里同一处的理由。
        _rt_open_skill_manage(self, "update_skill", _upd_skill, _upd_confirm_content,
                              extra={"summary": _upd_summary_resp})
        # ⚠️ `_upd_confirm_content` 上面已被 `_rt_open_skill_manage` 用作待办条目
        #    的展示文本（界面构件，豁免⑤），所以变量保留；这里只把**气泡**交给模型。
        yield {"event": "exit_flow_defer_to_model",
               "tool_result": (
                   f"A change to Skill {_upd_skill!r} is prepared but NOT applied "
                   f"yet - it needs the user's go-ahead first. What the change "
                   f"does, as summarised: " + repr(_upd_summary_resp) + ". "
                   f"Put that to them in your own words and ask them to confirm "
                   f"or cancel."),
               "log": "等待用户确认 Skill 的改法。"}

    def _rollback_last_tool_batch(self):
        """回滚最后一对 tool_calls / tool_results（ReAct 格式校验失败时调用）。"""
        last = self.memory.peek_last()
        if last and last.role == "tool_results":
            popped = self.memory.pop_last()
            logger.warning(f"[防御] 回滚孤儿 tool_results: role='{popped.role}'")
        last = self.memory.peek_last()
        if last and last.role == "tool_calls":
            popped = self.memory.pop_last()
            logger.warning(f"[防御] 回滚 tool_calls: role='{popped.role}'")

    def _clean_damaged_memory(self):
        """更安全的回滚：只清理本轮未完成的 tool batch，不误删已合法完成的工具对。

        判断依据：`_active_tool_batch_open` 标记。
        - True：tool_calls 已写，tool_results 未完成（mid-batch 异常），安全回滚
        - False：batch 已正常关闭，末尾工具对是合法历史，不能删
        """
        last = self.memory.peek_last()
        if last is None:
            return
        # 孤儿 user / 旧格式 tool_call：始终安全回滚
        if last.role in ("user", "tool_call"):
            popped = self.memory.pop_last()
            if popped:
                logger.warning(f"[防御] 已回滚受损记忆节点: role='{popped.role}'")
            return
        # 新格式 tool_calls / tool_results：只在 batch 事务未关闭时回滚
        if last.role in ("tool_calls", "tool_results"):
            if self._active_tool_batch_open:
                self._rollback_last_tool_batch()
                # ⚠️ 这是旧标志的【第二个】清理点（第一个是每轮开头的重置）。
                # 早先的设计的清理覆盖矩阵原先只记了"没有每轮重置"，没记这一处。
                # span 必须跟着一起收掉——否则它停在 OPEN、旧标志已 False，
                # 下一轮 sweep 会把它误报成"批次中途抛异常"（实测第一天就撞到了）。
                _rt_abort_open_span(self, "MEMORY_ROLLBACK", _RT_PATH.BATCH_EXCEPTION)
            else:
                logger.warning("[防御] 末尾是合法工具对，batch 已完成，跳过回滚，避免误删历史")



    def _get_generic_error_payload(self, err: Exception, skill: str) -> dict:
        return {
            "event": "sys_error",
            "content": f"抱歉，模型调度遭遇未预期中断。\n[异常报告]: {str(err) or '未知链路断开'}。\n当前请求已降级熔断。",
            "status": "ERROR",
            "model": "CRITICAL_OFFLINE",
            "log": f"指令熔断: {str(err)[:40]}",
            "current_skill": skill
        }

