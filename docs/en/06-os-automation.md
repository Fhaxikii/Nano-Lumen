# 06 · Desktop automation

**What this page covers**: the execution chain for desktop actions, risk computation, permission switches, and auditing.  
**After reading it you can**: add a desktop action, or modify risk and authorization decisions.  
**Prerequisites**: [02-architecture.md](02-architecture.md), [03-configuration.md](03-configuration.md).

> Language: [中文](../zh/06-os-automation.md) · English

---

## What this layer does

Lets the model operate the machine: run commands, read and write files,
control windows, simulate mouse and keyboard, and understand the screen
through screenshots.

The code lives in `core/os_layer/`. This is the highest-risk part of the
project; read this page in full before changing it.

## Modules

| File | Responsibility |
|---|---|
| `dsl.py` | Action definitions, permission mapping, risk computation |
| `dispatch.py` | Dispatch entry; routes an action to its executor |
| `safety.py` | Safety checks |
| `audit.py` | Audit log |
| `executor_low.py` | Read-only actions |
| `executor_write.py` | Writes through system APIs, no mouse/keyboard involved |
| `executor_action.py` | Mouse, keyboard, and window control |
| `executor_vision.py` | On-screen visual targeting |
| `fileedit.py` | Pure computation for file edits: produces new content and diffs, never writes to disk |
| `filesearch.py` | Recursive search by file name and by content |
| `pathpolicy.py` | Path policy: which locations must never be read |
| `longcmd.py` | Carrier for long-running commands: a command can outlive a single tool call, keep running, and report progress |
| `cmd_classifier.py` | Command danger classification in auto mode |
| `window_binding.py` | Tracks which window is currently being operated on |
| `canary.py` | When idle, self-checks the visual targeting chain against known targets |

## The risk model

Every action has a risk level, an integer. The effective risk is:

```
effective_risk = max(declared risk, static floor, all matching dynamic escalation rules)
```

Using max has two consequences: the model can only raise risk, never lower
it; and when several rules match, the strictest one wins.

Dynamic escalation rules come from `config/os_config.json`; users may add
to them, and they are not hardcoded. ⚠️ The six permission switches' current
values, by contrast, are not there — they live in `data/os_state.json`.
Rules follow the version, switches follow the user; the two must not share a
table. The on-disk location is decided by the single function
`os_state_path()`. A copy of built-in default rules is kept in the code as a
fallback for missing config: `_DEFAULT_UPGRADE_RULES` in
`core/os_layer/dsl.py`.

Typical escalation rules: `launch_app` is not very risky by itself, but when
the target is a system command line tool such as `cmd`, `powershell`, or
`regedit`, it escalates to the top tier; `type_text` is not very risky by
itself, but escalates to the top tier when the foreground window is a
terminal.

## Permission switches

Six switches, defined in `core/os_layer/dsl.py`:

| Constant | UI name |
|---|---|
| `PERM_WORKSPACE_WRITE` | 工作区写入 (Workspace writes) |
| `PERM_WINDOW_CONTROL` | 窗口控制 (Window control) |
| `PERM_MOUSE_KEYBOARD` | 鼠标键盘模拟 (Mouse & keyboard simulation) |
| `PERM_SYSTEM_SETTINGS` | 系统设置 (System settings) |
| `PERM_REGISTRY_WRITE` | 注册表写入 (Registry writes) |
| `PERM_DANGEROUS` | 高危操作总闸 (Dangerous operations master switch) |

The UI names are defined here rather than in the UI layer because the
refusal message sent to the model must say "go turn on switch X" — and the
model reads that sentence. If the two sides each kept their own copy, fixing
one would leave the other lying.

### The master switch's specialness

`PERM_DANGEROUS` is deliberately absent from every action definition. It is
not a capability category; it is a risk tier — it is required automatically
whenever `effective_risk` reaches the top tier.

If the master switch only gated actions statically marked dangerous, actions
escalated by dynamic rules would slip past it, and it would stop being a
master switch.

## Command classification in auto mode

With auto mode on, commands are no longer confirmed one by one.
`cmd_classifier.py` decides whether a command is a read-only query or a
destructive operation; destructive operations are still blocked.

This layer is a net added for auto mode, not a way to remove dialogs from
manual mode. Manual mode is unaffected.

## Visual targeting

`executor_vision.py` turns descriptions like "click this UI element" into
screen coordinates, with two-level fallback: it prefers the system
accessibility API (UI Automation) for real control coordinates; when those
are unavailable (Canvas, image buttons, non-standard controls), it takes a
screenshot and hands it to a multimodal model.

The file still contains an OCR-based locator, but it is off the call path.
It matched words that appeared in the target description rather than the
target itself, and ranked by OCR character confidence rather than match
quality. The knowledge base uses a different OCR implementation and is
unaffected.

The model used for vision calls is set under Settings → Advanced → Vision
and resolved via `model_for_role` in `core/models.py`. Never hardcode a
model name at the call site.

## Adding an action

1. Define it in `core/os_layer/dsl.py`: name, owning tool, required
   permission, static risk floor.
2. Implement it in the matching executor. The executor is chosen by what
   means the action needs: read-only, system-API writes, or mouse/keyboard.
3. If it should be more dangerous under some conditions, add a dynamic
   escalation rule — do not raise the static floor. A raised static floor
   affects every scenario.
4. Confirm it shows up in the audit log.

---

## How to verify you got it right

1. Turn off the relevant permission switch: the action is refused, and the
   refusal says which switch to turn on.
2. Turn it on: the action executes.
3. Construct a scenario that triggers dynamic escalation: risk is raised and
   extra authorization is required.
4. Check the audit log: the action is recorded.
5. Repeat the above in auto mode: destructive operations are still blocked.
6. Run `bash run_tests.sh`.

---

← Back to [README](README.md)
