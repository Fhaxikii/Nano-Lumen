# 06b · The execution pipeline

**What this page covers**: how a validated instruction reaches the real screen — locate-first, authorization gates, the four executors, emergency stops, long commands, and audit writes.  
**After reading it you can**: add an executor route, adjust confirmation timing, or wire up a long command without breaking the "report status, never decide" boundary.  
**Prerequisites**: [06 overview](06-os-automation.md), [06a instructions & risk](06a-instructions-risk.md).  

> Language: [中文](../zh/06b-execution-pipeline.md) · English  

---

## The six-step pipeline (`core/os_layer/dispatch.py`)

| Step | What happens | On failure / special case |
|---|---|---|
| 1 validate | `validate_instruction`: checks + effective risk + tier gate (see [06a](06a-instructions-risk.md)) | return status |
| 2 control flow | control signals from upstream (e.g. terminate) | return to upstream |
| 3 locate first | actions with a `target` (click/type_text) go through VisionLocator for coordinates + an annotated screenshot | locate failure **returns status; no dialog** |
| 4 authorize | risk=1 passes; risk=2 checks pre-authorization or suspends; risk=3 **always** suspends | user denies → return status |
| 5 execute | route to the executor with the emergency-stop listener attached | exception → status, no retry (replan belongs upstream) |
| 6 audit | `audit.record` persists (with risk and upgrade reasons) | — |

Two boundaries (dispatch's implementation constraints):

- **Report status, never decide**: it only yields execution results; "whether to
  replan" is decided by `_handle_os_task` upstream.
- **Authorization and execution are separate**: `safety.py` only answers "may
  this happen", never "how to do it".

## Locate first: see it, then confirm

VisionLocator (`executor_vision.py`) turns a semantic description like "click
that button" into screen coordinates with a **two-tier fallback, cheapest
first**:

1. **UIA** (uiautomation): read the control tree, match Name/ControlType, get
   real coordinates — fast and precise.
2. **Multimodal vision**: when UIA cannot (Canvas / image buttons / non-standard
   controls), a screenshot goes to the vision model; the model is configured in
   Settings → Advanced → Vision, never hardcoded here.

Two details:

- The **first locate runs a one-time coordinate-consistency check** (is DPI
  awareness effective); a mismatch warns "click coordinates may be
  systematically offset" — checked once, never again.
- After locating, an **annotated screenshot** is generated before the confirm
  dialog: the user sees *what will be clicked*, not an abstract description.

## Authorization gates and emergency stops

- risk=2's "pre-authorization" comes from `safety.py`'s session-level "always
  allow"; **risk=3 ignores it and always gets its own dialog**.
- During execution, the **dual emergency stops** are armed: mouse-fling
  failsafe + the Ctrl+` hotkey (`EmergencyStop`, executor_action.py). A third
  "soft stop" (keyword detection setting `_aborted`) was deleted — the flag was
  process-wide with no reset, so one trigger permanently crippled the process.
  **A safety mechanism without a reset path is itself a risk.**

## Four executors + long commands

| Executor | Scope | Representative actions |
|---|---|---|
| `executor_low.py` | read-only atomics, zero writes / zero mouse | screenshot / get_sysinfo / read_registry / read_window_tree |
| `executor_write.py` | system-API writes (no mouse/keyboard) | win_minimize / win_close / set_volume / launch_app |
| `executor_action.py` | mouse/keyboard + emergency stop | click / drag / type_text / hotkey / scroll |
| `executor_vision.py` | visual locating (shared by steps 3 and 5) | locate |

Supporting components:

- **`longcmd.py`**: lets a command **outlive its tool call** and **stay
  observable** (`LiveCommand`: running / elapsed / `tail(20)`; output spilled to
  disk to prevent flooding). Origin: a challenge — if long `os_execute` commands
  did not enter the same wait/review mechanism as MCP backgrounding, downloads,
  pip, and installs would run uncontrolled.
- **`window_binding.py`**: window identity binding — ensuring actions land on
  "the window from back then".
- **`filesearch.py` / `fileedit.py`**: atomic implementations of file search and edit.

## Hands-on recipes

**Case A: add an executor route**
Mount the action → executor mapping in `dispatch.py`'s route table; the executor
only yields results. A missing mount fails at execution with "not mounted" —
choosing the right `stage` for a new action (see the `read_screen_region`
example in [06a](06a-instructions-risk.md)) exposes this class of error at
validation instead.

**Case B: adjust confirmation timing**
The dialog is a `yield {"event":"os_action_confirm"}` suspension consumed
upstream. When changing it, keep two invariants: risk=3 always dialogs; a
locate failure shows no dialog (there is nothing to display).

**Case C: wiring a long command**
Any command that can outlive its tool call (downloads, pip, installs) goes
through `longcmd`, sharing the same wait/review mechanism as MCP backgrounding —
never a bespoke side-channel for one command.

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_os_layer_primitives.py`,
   `t_window_binding.py`, `t_recheck_longtask.py`).
2. Real machine: trigger one instruction at each of risk=1/2/3 and confirm the
   pass / pre-authorize / always-dialog paths.
3. Trigger an emergency stop (fling the mouse or Ctrl+`); the running action
   must terminate **and later tasks must still work**.
4. Locate a deliberately wrong target: it must return a status — no dialog, no hang.

---

← Back to [06 overview](06-os-automation.md)
