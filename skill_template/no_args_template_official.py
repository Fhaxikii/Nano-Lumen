# skill_template/no_args_template_official.py
"""
[Nano Skill no-arguments template -- official baseline] Protocol v3.2

Use for tools that take no parameters AND count as official baseline skills
  - get the current time / get the local IP / system status queries

The protocol is identical to the regular template (no_args_template.py);
the ONLY difference is [where the file lives, not the lifecycle value]:

  Regular skill -> put it in skills/          (can be disabled/deleted in the UI)
  Official skill -> put it in skills/official/  (the UI buttons exist, but the
                   backend refuses to disable/delete)

In code, lifecycle is still written as Lifecycle.PERMANENT. "Official" is
decided entirely by the file's directory; reload_all() scans
skills/official/ last, so it wins on name collisions.

Workflow:
  1. Write the skill following this template
  2. Place the .py file into skills/official/ by hand (not via Nano's UI deploy)
  3. Restart Nano; the startup log lists the official skills: [official: ['XXX']]

For the full protocol spec see the header comment of no_args_template.py;
not repeated here.
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


class ExampleNoArgsOfficial(BaseSkill):

    def get_manifest(self):
        return {
            "name": "ExampleNoArgsOfficial",
            "description": "Call this tool when the user asks about [trigger scenario A] or [trigger scenario B]",
            "parameters": {
                "type": "OBJECT",
                "properties": {},
                "required": [],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="ExampleNoArgsOfficial",

            # purpose: the user sees this verbatim in the info popup -- write it in your users' language
            purpose="One sentence describing the duty, e.g.: returns the current server time as a string",

            required_inputs=[],
            optional_inputs=[],
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
