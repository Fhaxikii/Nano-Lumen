# core/orchestrator/skill_writer.py
"""Orchestrator 的这一部分：Skill 生成：SkillSpec、代码编写、预览与创建出口。"""

import hashlib
import os
import re
import time

from loguru import logger

from core.orchestrator._runtime import (
    _rt_open_clarification,
    _rt_open_skill_audit,
    _rt_supersede_covered_clarifications,
)
from core.skill_check import validate_skill_code
from core.tools.manifests import _WRITE_SKILL_MANIFEST


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


class SkillWriterMixin:
    """Skill 生成：SkillSpec、代码编写、预览与创建出口。"""

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
        ok, errors = validate_skill_code(code, spec_side_effects=spec_side_effects)

        # ⭐ 撞 max_tokens 被截断 → 代码残缺，协议错误全是**症状不是成因**（实测）
        #
        # 那次生成一个 7 项功能的 Windows 网络配置 Skill，`output=8192`（正好等于上限），
        # 响应中途被切断，`code` 参数是空的。审计窗口于是报出 7 条
        # "缺少 import / 缺少类定义 / 缺少 get_spec() / run() 必须返回 SkillResult …" ——
        # **每一条都对，但每一条都指错了方向**：用户看到的是"模型不懂协议"，
        # 真相是"模型话没说完"。照着这些错误去改需求、改提示词，全是白功。
        #
        # 这与审计失败反馈是同一条判据：**报错要正确且充分。**
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
            # 判据：正确但不充分 = 不合格。
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
                        #    → **正是「双重伤害」的复发路径**
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
                # 它**正确地拒绝了**并引用了协议的 FORBIDDEN 条款与后果。
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
