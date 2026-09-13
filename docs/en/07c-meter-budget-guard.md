# 07c · Metering, budget & guard

**What this page covers**: the context "gauges" — how much is used right now (meter), how far from the watermarks (budget), and whether this request can be sent at all (guard), plus how quotas are configured and tuned.  
**After reading it you can**: adjust watermarks, add or change a model's window, modify the output reserve and the backstop, without breaking the three ledgers.  
**Prerequisites**: [07 overview](07-memory-and-context.md), [07a data & projection](07a-memory-data.md).  

> Language: [中文](../zh/07c-meter-budget-guard.md) · English  

---

## Three modules, three roles

| Module | Answers exactly one question | Never does |
|---|---|---|
| `core/context/meter.py` | how much of the window is used now | **never used for billing** (billing uses the true values in `core/usage.py`) |
| `core/context/budget.py` | should compaction start | **triggers nothing** — it speaks to the human (monitor card) and the model (pressure block) |
| `core/context/guard.py` | can this request be sent at all | **no semantic deletion** — only safe degradation or outright failure |

Mixing meter with billing produces "the bill and the budget disagree" — each
side is correct on its own; the hardest class of bug to find.

## Metering: anchor + snapshot delta

**Why not "write a token estimator"**: context is not additive — every turn
reassembles it. The `input_tokens` of request N is the **entire** input of that
request; summing them counts the same history many times (measured: three turns
summed to 38,355 while the truth was 13,400). And the truth is already in hand
every turn: `gross = input + cache_read + cache_creation` is Anthropic's own
definition of that request's full input (cache hits are still input, just
cheaper).

⭐ The answer is **anchor + delta**, where the delta is not an event ledger but
the difference of two snapshots:

```
predicted = anchor.actual + (estimate(now) − anchor.estimate)
```

This **automatically absorbs** history edits, tool-result truncation, image
placeholdering, the dynamic segment of system appearing/disappearing, schemas
added by `load_tools` — no hooks anywhere.

Key locations (`core/context/meter.py`):

| Location | Role |
|---|---|
| `ContextMeter`  / `_Anchor`  | the meter and the anchor |
| `estimate_request`  / `estimate_text`  | pre-send estimation |
| `normalize_prompt_input`  / `_PROMPT_INPUT_FIELDS`  | per-vendor usage normalization |
| `_sample` , `_SAMPLE_MAX=5000`, oldest dropped when full) | predicted-vs-actual distribution (`data/context_samples.jsonl`) — a **distribution**, not a ledger |
| `last_known`  / `forget_conversation_size`  | "last known" across restarts, and its invalidation |

⚠️ The provider reshapes the payload three more times before sending (drop
messages to prevent 400 / split stable-dynamic / manifest→input_schema) —
**the delta seen in Memory ≠ the delta actually sent**, so the anchor must
calibrate on the response, not at assembly time.

## Budget: three watermarks

`core/context/budget.py-26`:

| Watermark | Value | Audience |
|---|---|---|
| NOTICE | 0.50 | monitor card only; does not disturb the model |
| HIGH | 0.70 | injected to the model (pressure block) |
| CRITICAL | 0.85 | injected to the model, firmer wording |

- **The unit is a percentage of the model's own window, never absolute tokens**:
  the same absolute threshold means "just warming up" on a 1M model and "compact
  now" on a 200K model. Any threshold reused across models must be relative.
- **This layer only observes; it never acts.** The three levels deliberately do
  not trigger anything yet: measure the real distribution first, then set
  thresholds (the meter's samples are that distribution). `level_for` 
  classifies, `snapshot`  builds the snapshot, `pressure_block` 
  renders the pressure block for the model.

## Guard: the deterministic backstop

`core/context/guard.py`. The ladder is heuristic ("when to start forgetting");
the guard is the request-validity invariant ("can this request be sent at all")
— **a heuristic must sit on a deterministic backstop**.

- `admissible_input` : `window × (1 − OUTPUT_RESERVE)`, reserving room for output.
- `preflight` : fast screening before the request, **never raises**.
  `predicted is None` → pass — "I don't know" must not be treated as "over the
  limit", or the first message after every restart gets blocked.
- `in_red_zone` : eligibility for emergency decay / precise metering.
- **Two kinds of overflow must be distinguished** (`classify`): solvable by
  recycling old context → emergency decay; **this turn itself does not fit** →
  tell the user outright (`ContextWindowExceeded`). Counter-example: the
  user says "apply that plan we discussed" with the key constraint 30 turns
  back — the guard says "oldest, delete", the API succeeds, and Nano **confidently
  operates the real computer under the wrong constraint**. Window overflow may
  fail this request; it may not cause uncontrolled semantic deletion.

## Quotas and tuning

- Quotas are fractions of **each model's own window**, configured in
  `data/model_config.json` under `_quota` (summing to ~50%; the rest goes to
  system/tool-table floor noise, the current turn, bursts, and output).
  Read via `quota_of` (`core/models.py`); fallback `_FALLBACK_QUOTA`
  (L0 0.15 / L1 0.20 / L2 0.15).
- ⚠️ These four numbers are **uncalibrated** — the meter's samples are
  accumulating the distribution needed to set them.
- **Accelerated testing**: `_settings.quota_override` like
  `{"L0":0.03,"L1":0.02,...}` runs the whole ladder in a dozen turns; while
  active every quota read warns loudly, and it must be set back to `null`.

## Hands-on recipes

**Case A: adjust watermarks** — the three constants at `budget.py-26`. Look
at the distribution in `data/context_samples.jsonl` first; a threshold without
distribution data is a guess.

**Case B: add or change a model's window** — only the `window` field of the
vendor table in `data/model_config.json`. Guard, budget, and decay all follow
proportionally; no code changes.

**Case C: change the output reserve** — `OUTPUT_RESERVE` in `guard.py`. Smaller
means more usable input but a higher chance of a deterministic 400. This is a
hard risk; be careful.

**Case D: add a vendor's usage fields** — extend `_PROMPT_INPUT_FIELDS` and
`normalize_prompt_input` in `meter.py`; otherwise that vendor's usage
normalization fails and the anchor degrades (`_warn_missing_fields` in logs).

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_f5_budget.py`, `t_f5_guard.py`,
   `t_f5_live.py`).
2. Real long conversation: the monitor card's watermark should change color
   through 0.50/0.70/0.85 as the conversation grows.
3. **First turn after a restart**: the guard must not block it (last-known +
   pass-through logic).
4. Billing check: the meter's numbers are decision references only; real charges
   are what the vendor's console says.

---

← Back to [07 overview](07-memory-and-context.md)
