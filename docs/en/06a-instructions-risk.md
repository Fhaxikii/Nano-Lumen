# 06a · Instructions & risk

**What this page covers**: the static contract of an OS instruction — the `ActionDef` registry, how the six capability switches are actually enforced, the max-of-three-sources risk computation, how to write dynamic upgrade rules, and the tiers & state transition table.  
**After reading it you can**: add a new action, add dynamic upgrade rules, or open a new execution tier without breaking the "model can only raise risk" invariant.  
**Prerequisites**: [06 overview](06-os-automation.md).  

> Language: [中文](../zh/06a-instructions-risk.md) · English  

---

## ActionDef: the static profile of one instruction

Every action is registered in `_ACTIONS` (`core/os_layer/dsl.py`, :154):

```
ActionDef
├── name       instruction name
├── floor      risk floor (1-3)
├── readonly   read-only?
├── stage      executable from which tier: 1=readonly / 2=writes / 3=mouse & keyboard
├── perms      which capability switches (Settings → OS Permissions) must be on
└── tool       owning tool (OS / COMPUTER_USE)
```

Two designs that are easy to misread:

- **`readonly` and `stage` are not the same thing**. `read_screen_region` is
  read-only but `stage=3` — it needs VisionLocator, which low tiers do not
  mount; better rejected at validation than failing at execution with "not
  mounted".
- **An unknown action is treated as highest risk**. `action_floor` (:276)
  returns 3 for anything not in the dictionary — fail-closed: what the registry
  does not know is handled as most dangerous, not most optimistic.

## Why the six capability switches actually work

The `perms` field patches a hole found on 2026-08-20: of the 6 switches, **only
`allow_mouse_keyboard` had an enforcement point**; the other five (workspace
write / window control / system settings / registry write / **dangerous master
switch**) had **zero reads** anywhere. A user turning off the dangerous master
switch still got `run_command` and `file_delete` — the switch was persisted,
grayed out in the UI, **looked fully effective**.

📌 **A dead switch is more dangerous than no switch** — users relax around it.
⚠️ And it fails open. The fix: every ActionDef declares the switches it needs,
and dispatch checks each one during validation.

## Risk: max of three sources

`compute_effective_risk` (dsl.py:365):

```
risk = max( declared_risk, action_floor, upgrade_to of matched rules )
reasons = [why each raise happened]   ← all go to the audit log
```

The model's declared risk is a **declaration**; floors and upgrade rules are
**objective facts**. max semantics guarantee the model can only raise risk,
never lower it — that is the implementation of "the model cannot invent a risk
level".

## Writing dynamic upgrade rules

Built-ins live in `_DEFAULT_UPGRADE_RULES` (:315); users can add more under
`dynamic_upgrade_rules` in `config/os_config.json` (no code change needed).
Rule shape:

```json
{
  "action": "launch_app",
  "condition": {"target_in": ["cmd", "powershell", "regedit", "diskpart"]},
  "upgrade_to": 3,
  "reason": "launching a system command-line tool"
}
```

`condition` supports three predicates: `target_in` (target on a list),
`foreground_window_in` / `foreground_window_matches` (front window is a
terminal or an IDE's executable context). The most typical built-in: **typing
text into a terminal → upgrade to 3** — typing into cmd is arbitrary command
execution.

## Tiers and M1/M2/M3

- `stage`: 1=readonly / 2=writes / 3=mouse & keyboard.
- `M1/M2/M3_ALLOWED_ACTIONS` (:226-228) are **derived from `_ACTIONS`** — never
  hand-copied: a hand-copied list once had 29 entries against the real 39,
  missing exactly the high-frequency ones.
- `m1_mode` → `max_stage=1` (iron rule: read-only only; click and friends are
  rejected).

## The state transition table

`_STATE_TRANSITIONS` (:568) is hardcoded: a task's legal state progressions are
exactly the ones in the table; the model cannot invent paths.
`request_replan` is another branch of the same state machine.

## Hands-on recipes

**Case A: add a new action**
1. Register in `_ACTIONS`: name / floor / readonly / stage / **perms** (think
   about which switches it truly needs) / tool.
2. Mount the route in the matching executor (`executor_low` / `executor_write` /
   `executor_action` / `executor_vision`).
3. Read-only sets derive via `readonly_actions()` — **never hand-copy action
   lists elsewhere**.
4. Tests: add a case to `t_os_layer_primitives.py`; if capability switches are
   involved, update `t_os_capability_gate.py`.

**Case B: add a dynamic upgrade rule**
Prefer `config/os_config.json` (user-extensible, no code change); only rules
that must apply to everyone go into `_DEFAULT_UPGRADE_RULES`. After writing,
run a real instruction that matches and check the audit log shows the upgrade
reason.

**Case C: open a new execution tier**
Raise `max_stage` — that is the payoff of "enum defined once, completely".
Before opening, confirm every `perms` switch involved in that tier has an
enforcement point (see the dead-switch lesson above).

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_os_layer_primitives.py`,
   `t_os_capability_gate.py`).
2. Turn off one perms switch and confirm the actions depending on it are denied
   with an **explainable reason**.
3. Declare risk=1 on an instruction matching an upgrade rule; the audit log
   should show the raise to 3 and its reason.
4. A misspelled action name should be treated as risk=3, not reported as
   "does not exist".

---

← Back to [06 overview](06-os-automation.md)
