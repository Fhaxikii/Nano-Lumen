# 06c · Permissions, classifier & audit

**What this page covers**: the three "judgment and recording" blocks of the safety net — the Auto-mode intent-consistency classifier, the append-only audit trail, the sensitive-path deny list, plus the authorization scope and the canary self-check.  
**After reading it you can**: adjust the classifier's scope, extend audit or path policy, and understand why each mechanism has its current shape.  
**Prerequisites**: [06 overview](06-os-automation.md), [06a instructions & risk](06a-instructions-risk.md).  

> Language: [中文](../zh/06c-permissions-audit.md) · English  

---

## The intent-consistency classifier (`cmd_classifier.py`)

The judge for `run_command` under Auto mode. Four verdicts: safe (pass) / low
risk (pass) / **dangerous (dialog)** / undecidable (dialog, conservative).

**"Dangerous" requires all three conditions (absolute design rule)**:

1. It is a `run_command` (enters the judge);
2. ∧ it belongs to a high-risk class (delete / install / modify / kill /
   exfiltrate…);
3. ∧ **it does not match what the user asked for**.

🔴 **Condition ③ is the whole point; without it this is worthless.** With only
①② it degenerates into "delete = dangerous" — something a regex table could do,
and it would block deletions the user explicitly asked for:

```
User "clean up my temp files"   + del /q /f %temp%\*  → matches  → do not block
User "what does this txt say"   + del /q /f %temp%\*  → mismatch → must block
```

⇒ **Danger is not a property of the command; it is a property of the action
deviating from the intent.** The same command, two verdicts.

**What the judge sees — reasoning-blind by design** (following the official
classifier):

- Sees: user messages (intent) + this one tool call (action)
- Blind to: the model's own reasoning (it is the thing being checked; it cannot
  defend itself)
- Blind to: tool outputs (**the injection carrier**; letting them into the judge
  hands the gate to the attacker)

⭐ ③ doubles as **injection defense**: an injection can make the model issue a
dangerous command, but it **cannot change what the user actually said** —
succeeding requires fooling both the user and the judge.

Engineering details: the session cache key **must include an intent
fingerprint** (`_cache_key`, :98) — the same command under a different intent is
a different question; `reset_cache` (:106) on session change. Script commands
get an AST pre-scan first (`prescan_script`, :166): a safe stdlib-only script
passes without asking intent, saving even the model call.

## The audit trail (`audit.py`)

- **Append-only, one JSON per line**: `data/os_audit/os_actions.log`.
- **Read-only actions are logged too** — "every OS action leaves a trace" does
  not tier by risk.
- Deliberately distinct from working memory: working memory is semantic recall
  for the model (filterable, formattable); the audit log is a **tamper-evident
  full operation stream** for after-the-fact tracing.
- Each record carries the risk and upgrade reasons (from
  [06a](06a-instructions-risk.md)'s `reasons`) — answering not just "what was
  done" but "why it was allowed".

## The sensitive-path deny list (`pathpolicy.py`)

**A deny list, not an allow list** — the opposite direction from the sub-agent
toolset, each for good reason:

- Sub-agent toolsets use an allow list: the debts of exclusion grow over time.
- Path policy uses a deny list: the space of legitimate paths is unbounded (the
  user may want to read files anywhere) and cannot be enumerated, but dangerous
  locations are few and enumerable (credentials, private keys, browser
  passwords, `C:\Windows\System32\config\SAM`, etc.).

Read and write policies differ: reads follow the looser KB-side policy but must
still pass the deny list.

## The canary self-check (`canary.py`)

Validates the vision-locating pipeline while idle, under two disciplines:

1. **Targets must be permanently present**: use Windows taskbar standard
   buttons (search / task view / notifications), never "is Notepad open" —
   otherwise "target missing" pollutes the "locate failed" statistics. It
   locates the taskbar window directly (Shell_TrayWnd), not through the
   "guess which window the user means" logic.
2. **Only while idle**: the canary does UIA locating; running it concurrently
   with real tasks reintroduces the just-fixed "self-check stealing foreground
   focus" problem. `should_run()` checks both the interval and idleness.

Consecutive low scores reaching `escalate_after_consecutive_low` (config
default 3) escalate; the decision lives in `run_once()`, and the caller handles it.

## The authorization scope (`safety.py`)

- Session-level "always allow" applies to risk=2 only; **risk=3 always gets its
  own confirmation**.
- Step / replan counters live here too.
- The third "soft stop" once here was deleted (a process-wide flag with no
  reset path); the story is in [06b](06b-execution-pipeline.md). **A safety
  mechanism without a reset path is itself a risk.**

## Hands-on recipes

**Case A: adjust the classifier's scope**
The high-risk class list and the script AST pre-scan live in
`cmd_classifier.py`. The self-check question for any change: does the judgment
still **center on intent consistency**? Judging by command features alone =
degeneration into a regex table. Run `t_cmd_classifier.py` after, focusing on
"deletions the user explicitly asked for" not being blocked.

**Case B: extend audit or path policy**
Audit: keep the two properties (append-only; read-only actions logged too)
(`t_audit_semantics.py`). Paths: only add deny-list entries; before narrowing
toward an allow list, read the rationale at the top of `pathpolicy.py`.

**Case C: change the canary target**
It must satisfy the "permanently present" discipline; `t_shot_prune.py` and the
config keys `canary.interval_seconds` / `success_threshold` /
`escalate_after_consecutive_low` are the tuning entry points.

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_cmd_classifier.py`,
   `t_audit_semantics.py`, `t_os_capability_gate.py`).
2. Real machine, Auto mode: ask "clean up my temp files" and then "read this
   txt" followed by deleting the same directory — the first should pass, the
   second should dialog.
3. After any OS action, check the last line of
   `data/os_audit/os_actions.log`: complete JSON, with risk and reasons.
4. Turn off one permission switch and repeat an operation; the denial reason
   should point at that switch.

---

← Back to [06 overview](06-os-automation.md)
