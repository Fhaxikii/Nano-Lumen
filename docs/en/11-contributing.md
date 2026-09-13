# 11 · Contributing

**What this page covers**: what to know before submitting a PR, including what we do not accept.  
**After reading it you can**: judge whether your change is likely to be accepted, and run the pre-submission checklist.  
**Prerequisites**: whichever page matches your change.

> Language: [中文](../zh/11-contributing.md) · English

---

## Reporting issues

Before filing an issue, please confirm:

- You have read the "common startup problems" section of [01-getting-started.md](01-getting-started.md).
- You have checked [Changelog.txt](../../Changelog.txt) to make sure it is not a known or already-fixed issue.
- You have read the "Known Limits" section of the README — the limitations listed there
  (such as proactive intelligence not yet being enabled, or DeepSeek not being deeply
  tested) are out of scope for issue reports.

### Bug report template

```
---
name: Bug report
about: Report a reproducible problem
title: "[Bug] brief description"
labels: bug
---

## Environment

- Nano version: <!-- e.g. v1.96 -->
- Operating system: <!-- e.g. Windows 11 22H2 -->
- Python version: <!-- e.g. 3.10.11 -->
- Model vendor / model: <!-- e.g. Anthropic / claude-sonnet-5 -->

## Problem description

<!-- One sentence on what happened -->

## Steps to reproduce

1.
2.
3.

## Expected behavior

<!-- What should happen -->

## Actual behavior

<!-- What actually happened; screenshots welcome -->

## Logs

<!-- Key excerpts from the console or logs under data/ (mask sensitive info) -->

## Already checked

- [ ] Confirmed it is not a limitation listed under "Known Limits"
- [ ] Checked Changelog.txt, not a known issue
- [ ] Reproduced with a minimal configuration (e.g. only one vendor key)
```

### Feature request template

```
---
name: Feature request
about: Suggest an idea for Nano
title: "[Feature] brief suggestion"
labels: enhancement
---

## Problem you want to solve

<!-- The use case; why the current capabilities are not enough -->

## Proposed solution

<!-- What you want Nano to do -->

## Alternatives

<!-- Other approaches you considered -->

## Notes

<!-- Whether you'd be willing to help implement or provide a test environment -->
```

---

## Read this first: what we do not accept

Saying it up front beats rejecting it afterwards. A finished PR that gets
rejected is wasted work for the contributor and wasted review time for the
maintainer.

### Explicitly not accepted

**Changes to the core safety gates**
Meaning the desktop automation risk computation, the permission switches,
and the dangerous-command classification in auto mode. If these fail, the
user's machine executes what it should not. If you believe a decision is
wrong, open an issue describing the concrete scenario; do not change it
directly.

**Paid dependencies**
Any library, service, or model that requires payment to use.

**Services requiring an account**
Including anything requiring an API key. The user's own model-vendor key is
the sole exception — it is a prerequisite of using this project at all.

The same three conditions apply to built-in MCP servers: free, no
registration, no key — hard requirements.

### This list is not exhaustive

It grows with what actually happens. If your change is not on the list but
you are unsure, ask in an issue first — cheaper than discussing a finished
PR.

## Before submitting

### 1. Check the scope

One PR does one thing. Keep unrelated changes out, including drive-by
formatting.

### 2. Run the tests

```
bash run_tests.sh
```

The output must be `OK 真·全量 0 失败`. Known environment-dependent failures
are in [10-testing.md](10-testing.md).

### 3. Add a test

If the change introduces new behavior, add a test. The criteria are in
"what to test" in [10-testing.md](10-testing.md).

### 4. Checklist

- [ ] No hardcoded model names. Models resolve through the matching
      functions in `core/models.py`.
- [ ] No hardcoded colors or font sizes. Use CSS variables.
- [ ] Model-visible text is in English; user-visible text follows the
      user's language.
- [ ] New config items have defaults; a missing config does not break
      startup.
- [ ] If a data write path was touched, all write entry points go through
      the same function.
- [ ] If the change affects a permanently-mounted UI element, it gets
      refreshed.
- [ ] No time-consuming blocking calls before UI elements are created.

## Conventions you will meet in comments

This project's comments are dense, and mostly explain "why it is done this
way". Three things you may find confusing:

**`内部设计文档` (internal design documents)** — planning and decision
documents from development, **not shipped with the repo**. Wherever one is
referenced, the conclusion itself is in the comment; you do not need the
document. Its mention only marks where the statement came from.

**Bracketed codenames like `[L5]` `[F4]` `[D13]`** — numbers of internal
work items, pointing to documents that are likewise not shipped. They are
labels and do not affect what the comment says.

📌 Neither of these is a prerequisite for understanding the code. When you
see one, reading the sentence after it is enough.

## Code style

- Follow the style of the file you are changing: naming, comment density,
  indentation conventions.
- Comments explain "why it is done this way", not "what this line does".
- Users can read skill source in the UI, so files under `skills/` carry
  interface documentation only — no development history.

## Commit messages

Say what changed and why. If it fixes a defect, describe the defect's
symptoms, not just an issue number.

## Documentation

If your change alters how a module is used, update the matching page under
`docs/`. A passage describing a mechanism that no longer exists is worse
than no passage: readers will follow it, and the error will not surface
immediately.

---

## How to verify you got it right

1. `bash run_tests.sh` passes.
2. Every checklist item confirmed.
3. Re-walk [01-getting-started.md](01-getting-started.md) in a clean
   environment; first launch still works.
4. If the change touches the UI, verify under both themes.

---

← Back to [README](README.md)
