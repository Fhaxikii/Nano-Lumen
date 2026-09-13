# 06 · Desktop automation (Overview)

**What this page covers**: the map of the OS execution layer — the full path of one instruction from the model to the real screen, the core of the risk model, and how the three sub-pages divide the work.  
**After reading it you can**: tell which sub-page your change belongs in, and understand why this layer is shaped the way it is.  
**Prerequisites**: [02-architecture.md](02-architecture.md).  

> Language: [中文](../zh/06-os-automation.md) · English  

---

## What this layer does

Desktop automation is Nano's highest-risk capability: it operates **not a
sandbox but the real computer**. The layer's shape is pinned by two hard
constraints (`core/os_layer/dsl.py`):

1. **The ACTION enum is closed** — the model only picks from registered
   actions and cannot invent instructions, reinforced by the dynamic upgrade
   rule table.
2. **The state transition table is hardcoded** — no room left for the model to
   improvise.

⭐ The enum was defined completely on day one: write and mouse/keyboard actions
were all registered from the start, gated by a `stage` field marking "from
which permission tier this becomes executable" — opening a new tier only raises
`max_stage`, **the contract never changes**.

## The execution pipeline (six steps)

One DSL instruction enters `core/os_layer/dispatch.py` and walks a fixed order:

```
1. validate_instruction   validate + compute effective risk + tier gate
2. control-flow signals return to the upper layer immediately
3. locate first   actions with a semantic target (click/type_text) get
                  VisionLocator to resolve coordinates + an annotated
                  screenshot, then the confirm dialog (the user sees what will
                  be clicked)
4. authorization   risk=1 passes; risk=2 checks pre-authorization or suspends
                   for confirmation; risk=3 always suspends
5. execute   route to the matching executor with the emergency-stop listener attached
6. audit.record   append to the audit log
```

## The risk model in one line

```
risk = max( declared risk, static floor, dynamic upgrade rules hit )
```

The model **can only raise risk, never lower it**; every upgrade reason goes
into the audit log. Why max, how floors are set, how to write upgrade rules —
see [06a](06a-instructions-risk.md).

## The safety nets

- **Two emergency stops**: mouse-fling failsafe + the Ctrl+` hotkey, both in
  `EmergencyStop` (`executor_action.py`). A third "soft stop" (keyword
  detection) was deleted — its `_aborted` flag was process-wide with no reset,
  one trigger left the process permanently crippled (the story in 06c).
- **Auto-mode backstop**: even with Auto mode on, destructive commands are still
  caught by the command classifier ([06c](06c-permissions-audit.md)).
- **Explainable denials**: when denied, the model tells you which switch to open.

## Module map

| Module | One line | Read in |
|---|---|---|
| `dsl.py` | instruction contract: enum, risk, transition table | [06a](06a-instructions-risk.md) |
| `dispatch.py` | six-step pipeline entry | [06b](06b-execution-pipeline.md) |
| `executor_vision.py` | visual locating: UIA → multimodal two-tier fallback | [06b](06b-execution-pipeline.md) |
| `executor_action.py` | mouse/keyboard + dual emergency stop | [06b](06b-execution-pipeline.md) |
| `executor_write.py` / `executor_low.py` | system-API writes / read-only atomics | [06b](06b-execution-pipeline.md) |
| `longcmd.py` | long commands outliving their tool call, with progress | [06b](06b-execution-pipeline.md) |
| `window_binding.py` / `filesearch.py` / `fileedit.py` | window identity, file search, file edit | [06b](06b-execution-pipeline.md) |
| `safety.py` | authorization scope, step counters | [06c](06c-permissions-audit.md) |
| `cmd_classifier.py` | Auto-mode intent-consistency classifier | [06c](06c-permissions-audit.md) |
| `audit.py` / `pathpolicy.py` | append-only audit, sensitive-path deny list | [06c](06c-permissions-audit.md) |
| `canary.py` | idle-time vision-pipeline self-check | [06c](06c-permissions-audit.md) |

## Three iron rules

1. **The model cannot invent instructions or transitions** — closed enum,
   hardcoded table.
2. **Risk only rises** — max semantics: no declaration beats a floor or an
   upgrade rule.
3. **Report status, never decide** — dispatch only yields execution results;
   "whether to replan" belongs to `_handle_os_task` upstream.

---

## How to verify you got it right

This page is a map. Verification lives at the end of each sub-page; full
regression `bash run_tests.sh` (this layer's dedicated tests:
`t_os_layer_primitives.py`, `t_cmd_classifier.py`, `t_os_capability_gate.py`,
`t_window_binding.py`, `t_audit_semantics.py`, etc.).

---

← Back to [README](README.md)
