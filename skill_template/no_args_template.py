# skill_template/no_args_template.py
"""
[Nano Skill no-arguments template] Protocol v3.2

Use when the skill needs no user input to run
  - get the current time / get system status / get the local IP

If the skill takes parameters, use with_args_template.py instead.
If this is an official baseline skill that the UI cannot delete/disable,
use no_args_template_official.py.

=====================================================================
Core requirements of protocol v3.2 (missing any one of these means
the skill fails to register / fails hard_validate)
=====================================================================
1. Import: from core.schema import BaseSkill, SkillResult, SkillSpec, ...
2. Class name == get_manifest() name == get_spec() name (all three identical)
3. Must implement get_manifest()  <- follows the Gemini function-declaration shape
4. Must implement get_spec()      <- used by the policy engine and the UI info button
5. run() returns a SkillResult, not a str
6. run() must have a docstring
7. SkillResult.data must contain every key declared in SkillSpec.data_output_keys
   (data_output_keys must not be empty; declare at least one key,
   e.g. "value" / "success")
8. Wrap all blocking IO in asyncio.to_thread()
9. Strip newlines from exceptions: str(e).replace(chr(10), ' ')

[How to write SkillResult.text]
text is the model's ONLY channel for telling the user what happened, so it
must be complete and specific:
  - File operations: must include the absolute path via os.path.abspath()
    x text="Log appended"
    v text=f"Appended to {os.path.abspath(path)}"
  - Queries / data returns: must include the concrete result value
    x text="Query succeeded"
    v text=f"Current time: {formatted_time}"
  - On failure: must state the concrete reason, never just "it failed"

[purpose / not_responsible_for are shown to the user verbatim]
Every skill in the sidebar has an info button that displays get_spec()'s
purpose and not_responsible_for directly to the user. ⚠️ USER-VISIBLE
FIELDS — write them in the language your users read; there is no
translation mechanism. The samples below are English placeholders.
  - purpose: one sentence saying what this tool does, in plain words
  - not_responsible_for: things users might assume it does but it does not

[Enum values (must be uppercase)]
ContextLevel:    NONE / FRAGMENT_OK / GENERATED_OK / FULL_REQUIRED /
                 FILE_PATH_REQUIRED / WEB_REQUIRED
SideEffect:      NONE / FILE_READ / FILE_WRITE / FILE_DELETE / NETWORK /
                 EXTERNAL_API / SHELL / SEND_MESSAGE
PermissionLevel: READONLY / WORKSPACE_WRITE / NETWORK_ALLOWED /
                 EXTERNAL_ACTION / DANGEROUS
Lifecycle:       PERMANENT / TEMPORARY / EXPERIMENTAL
  - PERMANENT: goes in skills/, stays until removed
  - TEMPORARY: goes in skills_temp/, cleared automatically when the
    conversation resets or Nano restarts — use only when the user
    explicitly says "temporary" / "don't save it"; otherwise PERMANENT

[Side-effect / permission consistency, checked by hard_validate]
  NONE/FILE_READ -> starts at READONLY
  FILE_WRITE     -> at least WORKSPACE_WRITE
  NETWORK        -> at least NETWORK_ALLOWED
  EXTERNAL_API/SEND_MESSAGE -> at least EXTERNAL_ACTION
  FILE_DELETE/SHELL         -> at least DANGEROUS
side_effects=[NONE] cannot be combined with any other side effect (mutually
exclusive).

[Extra rule for real-time-data skills]
When the skill returns current time/status or similar live information,
get_manifest()'s description MUST include:
"Call this tool whenever the user asks about [X]; never estimate from
training data."
Otherwise the model will guess from its training data instead of calling
the tool.
=====================================================================
"""

import os
from core.schema import (
    BaseSkill,
    SkillResult,
    SkillSpec,
    ContextLevel,
    SideEffect,
    PermissionLevel,
    Lifecycle,
)


class ExampleNoArgs(BaseSkill):

    def get_manifest(self):
        return {
            "name": "ExampleNoArgs",
            "description": "Call this tool when the user asks about [trigger scenario A] or [trigger scenario B]",
            "parameters": {
                "type": "OBJECT",
                "properties": {},
                "required": [],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="ExampleNoArgs",

            # purpose: the user sees this verbatim in the info popup -- write it in your users' language
            purpose="One sentence describing the duty, e.g.: returns the current server time as a string",

            required_inputs=[],
            optional_inputs=[],

            # Declare which keys run()'s SkillResult.data is guaranteed to contain;
            # the model uses this structured data when summarizing results
            data_output_keys=["value"],

            side_effects=[SideEffect.NONE],
            permission_level=PermissionLevel.READONLY,

            # not_responsible_for: the user sees this verbatim in the info popup
            not_responsible_for=["Takes no parameters", "Reads no files", "Goes online"],

            lifecycle=Lifecycle.PERMANENT,
        )

    async def run(self) -> SkillResult:
        """[What it does] Fires automatically when the user asks about [trigger scenario A] or [trigger scenario B]."""
        try:
            value = "example return value"

            return SkillResult(
                success=True,
                text=f"Done: {value}",
                data={"value": value},
            )
        except Exception as e:
            return SkillResult(
                success=False,
                text=f"Failed: {str(e).replace(chr(10), ' ')}",
                data={"value": None},
            )
