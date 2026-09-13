# 04 · Writing a skill

**What this page covers**: the skill protocol, directory conventions, a
complete walkthrough, and how to deploy and debug.
**After reading it you can**: add a tool the model can call, and verify it
loads and runs correctly.
**Prerequisites**: [02-architecture.md](02-architecture.md).

> Language: [中文](../zh/04-writing-a-skill.md) · English

---

## What a skill is

A skill is a single Python file implementing one tool the model can call.
Drop the file into `skills/` and it is discovered and loaded — no framework
changes needed.

This is the most direct way to add a capability to the project, and the
lowest-risk entry point for outside contributors.

## Directory conventions

| Directory | Meaning |
|---|---|
| `skills/` | Regular skills. Can be disabled and deleted in the UI |
| `skills/official/` | Official baseline skills. The UI button exists but the backend refuses to disable or delete them |
| `skills/disabled/` | Skills disabled by the user |
| `skills/deleted/` | Skills deleted by the user |

"Official" is decided entirely by which directory the file is in; there is no
field for it. At load time `skills/official/` is scanned last and wins on
name collisions.

## Templates

The repo ships four templates in [`skill_template/`](../../skill_template/):

| File | For |
|---|---|
| `no_args_template.py` | Regular skill, no arguments |
| `with_args_template.py` | Regular skill, with arguments |
| `no_args_template_official.py` | Official skill, no arguments |
| `with_args_template_official.py` | Official skill, with arguments |

The full protocol description sits at the top of
[`skill_template/no_args_template.py`](../../skill_template/no_args_template.py). The template is authoritative; do not
copy snippets from this page.

## The three required methods

Using [`skill_template/no_args_template_official.py`](../../skill_template/no_args_template_official.py) as the example:

### `get_manifest()`

The tool declaration the model sees: name, description, parameter schema.
The description decides when the model will call the tool, so it should
describe triggering situations, not implementation details.

### `get_spec()`

Returns a `SkillSpec` declaring the skill's nature. Key fields:

| Field | Meaning | Values from |
|---|---|---|
| `purpose` | One-line responsibility | free text |
| `required_inputs` / `optional_inputs` | Input definitions | `InputDef` |
| `data_output_keys` | Which keys may appear in the output dict | free text |
| `side_effects` | Side-effect types | `SideEffect` in `core/schema.py` |
| `permission_level` | Required permission tier | `PermissionLevel` in `core/schema.py` |
| `not_responsible_for` | What it explicitly does not do | free text |
| `lifecycle` | Lifecycle | `Lifecycle` in `core/schema.py` |

`side_effects` values include `NONE`, `FILE_READ`, `FILE_WRITE`,
`FILE_DELETE`, `NETWORK`, `EXTERNAL_API`, `SHELL`, `SEND_MESSAGE`,
`OS_CONTROL`.

`permission_level` values include `READONLY`, `WORKSPACE_WRITE`,
`NETWORK_ALLOWED`, `EXTERNAL_ACTION`, `DANGEROUS`.

These two fields are not documentation — they participate in runtime
decisions. Declaring looser than reality bypasses gates; declaring stricter
than reality gets the skill refused.

### `run()`

An async method returning a `SkillResult` with three fields:

- `success`: boolean.
- `text`: one-line result for the model.
- `data`: structured output; keys must match `data_output_keys`.

On failure, do not raise. Return `success=False` and explain why in `text`.
An exception aborts the whole turn; a failure result lets the model adapt.

## Language of user-facing text

Two kinds of text live in a skill, and their readers differ:

- **Read by the model** (`get_manifest()` descriptions, `SkillResult.text`):
  English.
- **Read by the user** (`purpose`, `not_responsible_for`, display names in
  the UI): whatever language is written into the skill is what the user
  sees — there is no translation mechanism. Write them in the language your
  users read; the sample values in the `skill_template/` templates are
  English placeholders.

The criterion is "who reads this text", not "which file is it in".

## Comments

Users can read skill source directly in the UI (the "view source" action on
every tool in the tool panel). Keep only necessary interface documentation in
skill files — no development logs, debugging notes, or historical incident
notes. That content is development history, not user-visible source.

Match the comment density of the existing files under `skills/official/`.

## Deployment

Regular skills deploy through the UI. Official skills must be placed into
`skills/official/` by hand, followed by a restart; the startup log lists the
official skills that loaded.

## Debugging

1. After launch, check the log for the skill being loaded. Load failures
   record the reason.
2. Confirm it appears in the tool panel on the left with status `READY`.
3. Trigger it through conversation and expand the tool card to see the
   actual arguments and result.

Skill discovery and loading live in `core/registry.py`; protocol validation
in `core/schema.py`.

---

## How to verify you got it right

1. The skill appears in the startup log with no load errors.
2. The tool panel shows it as `READY`.
3. One real call: the tool card shows the expected arguments and result.
4. Force a failure (e.g. an invalid argument): the result is a failure
   result, not an exception abort.
5. Run `bash run_tests.sh`.
