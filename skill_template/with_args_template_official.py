# skill_template/with_args_template_official.py
"""
[Nano Skill with-arguments template -- official baseline] Protocol v3.2

Use for tools that take parameters AND count as official baseline skills
  - read a given Excel file / search by keyword / standard data conversion

The protocol is identical to the regular template (with_args_template.py);
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

For the full protocol spec see the header comments of no_args_template.py
and with_args_template.py; not repeated here.
"""

import os
from core.schema import (
    BaseSkill,
    SkillResult,
    SkillSpec,
    InputDef,
    ContextLevel,
    SideEffect,
    PermissionLevel,
    Lifecycle,
)


class ExampleWithArgsOfficial(BaseSkill):

    def get_manifest(self):
        return {
            "name": "ExampleWithArgsOfficial",
            "description": "Call this tool when the user needs [scenario]; requires [argument description]",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "The search keyword provided by the user",
                    },
                },
                "required": ["query"],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="ExampleWithArgsOfficial",

            # purpose: the user sees this verbatim in the info popup -- write it in your users' language
            purpose="One sentence describing the duty, e.g.: searches by keyword and returns matching lines",

            required_inputs=[
                InputDef(
                    name="query",
                    type="string",
                    description="The search keyword; must not be empty",
                ),
            ],
            optional_inputs=[],

            data_output_keys=["matched_count", "result"],

            side_effects=[SideEffect.NONE],
            permission_level=PermissionLevel.READONLY,

            # not_responsible_for: the user sees this verbatim in the info popup
            not_responsible_for=["Reads no files", "Goes online", "Modifies no source data"],

            lifecycle=Lifecycle.PERMANENT,
        )

    async def run(self, query: str) -> SkillResult:
        """[What it does] Fires when the user wants to query [scenario A] or [scenario B].

        Args:
            query: The search keyword; must not be empty
        """
        try:
            if not isinstance(query, str) or not query.strip():
                return SkillResult(
                    success=False,
                    text="Failed: the query parameter must not be empty",
                    data={"matched_count": 0, "result": None},
                )

            result = f"Processed result for '{query}'"
            matched_count = 1

            return SkillResult(
                success=True,
                text=f"Found {matched_count} result(s): {result}",
                data={
                    "matched_count": matched_count,
                    "result": result,
                },
            )
        except Exception as e:
            return SkillResult(
                success=False,
                text=f"Failed: {str(e).replace(chr(10), ' ')}",
                data={"matched_count": 0, "result": None},
            )
