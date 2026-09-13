# skill_template/with_args_template.py
"""
[Nano Skill with-arguments template] Protocol v3.2

Use when the skill needs user-provided parameters to run
  - read an Excel file at a given path / search by keyword / call an API
    for specific data

If the skill needs no parameters at all, use no_args_template.py.
If this is an official baseline skill that the UI cannot delete/disable,
use with_args_template_official.py.

The full protocol spec (enum values, how to write text, side-effect /
permission consistency, the info-button fields) is documented in the header
comment of no_args_template.py and is not repeated here.

=====================================================================
Extra points for skills with arguments
=====================================================================

[Choosing required_context_level]
Takes a file-path argument (e.g. file_path)   -> FILE_PATH_REQUIRED
Takes full file content (e.g. full_text)       -> FULL_REQUIRED
Plain user-typed strings/numbers suffice       -> NONE (most common)

[What goes in source_hint]
An InputDef's source_hint says where the parameter usually comes from:
  "user"           -> typed directly by the user (most common)
  "get_file_path"  -> a disk path obtained via the get_file_path tool
  "load_full_file" -> text returned by the full-file loader tool

[get_manifest parameter types]
Types in the Gemini function declaration are uppercase strings:
  STRING / INTEGER / NUMBER / BOOLEAN / ARRAY / OBJECT

[run() signature]
Parameter names must match the keys under get_manifest -> parameters ->
properties EXACTLY, otherwise arguments passed by the model are silently
lost to the name mismatch.
=====================================================================
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


class ExampleWithArgs(BaseSkill):

    def get_manifest(self):
        return {
            "name": "ExampleWithArgs",
            "description": "Call this tool when the user needs [scenario]; requires [argument description]",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {
                        "type": "STRING",
                        "description": "The search keyword provided by the user",
                    },
                    # Add further parameters here; names must match the run() signature:
                    # "limit": {"type": "INTEGER", "description": "Maximum number of results"},
                    # "file_path": {"type": "STRING", "description": "Absolute disk path of the target file"},
                },
                "required": ["query"],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="ExampleWithArgs",

            # purpose: the user sees this verbatim in the info popup -- write it in your users' language
            purpose="One sentence describing the duty, e.g.: searches by keyword and returns matching lines",

            required_inputs=[
                InputDef(
                    name="query",
                    type="string",
                    description="The search keyword; must not be empty",
                ),
                # Example of a file-path parameter:
                # InputDef(
                #     name="file_path",
                #     type="file_path",
                #     source_hint="get_file_path",
                #     description="Absolute disk path of the target file, provided by the get_file_path tool",
                # ),
            ],
            optional_inputs=[
                # Optional parameters go here; run() must give them defaults
                # InputDef(name="limit", type="integer", source_hint="user",
                #          description="Maximum number of results, default 10"),
            ],

            # Declare which keys run()'s SkillResult.data is guaranteed to contain
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
