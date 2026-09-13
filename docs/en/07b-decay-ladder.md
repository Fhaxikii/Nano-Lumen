# 07b · The decay ladder

**What this page covers**: for each of the five arrows (L0→L1→L2→L3→L4): trigger, cost, product, and red lines — plus the Digest contract.  
**After reading it you can**: change the decay policy of one level, tune batch sizes and caps, or modify the Digest schema / distillation prompt.  
**Prerequisites**: [07 overview](07-memory-and-context.md), [07a data & projection](07a-memory-data.md).  

> Language: [中文](../zh/07b-decay-ladder.md) · English  

---

## The five arrows at a glance

Implementation: `core/context/decay.py`; called in order by the orchestrator in
each round's `finally` (see [07 overview](07-memory-and-context.md)).

| Arrow | Function | LLM? | Driven by | Product |
|---|---|---|---|---|
| L0→L1 | `run_l0_to_l1`  | no | until L0 usage drops below target, or `MAX_PER_RUN` demotions this round | tool-result bodies → placeholders |
| L1→L2 | `run_l1_to_l2`  | **yes, one call each** | L1 usage over the model's quota (`_quota.L1` × window) | Digest lines |
| L2→L3 | `run_l2_to_l3`  | no (digest already stored) | exchanges meeting the L3 condition | semantic memory + index entry |
| L3→L4 | `run_l3_to_l4`  | no | index entry expiry | index entry removed (**semantic memory kept**) |

## Three red lines (learn before touching any arrow)

1. **Never break `tool_calls ↔ tool_results` pairing**. Decay replaces
   `content` only; `tool_use_id` and order stay intact — drop one side and the
   saved tokens turn into a provider 400.
2. **`is_error` must survive**. It is the only boundary between "tried" and
   "did"; lose it and a failed tool call reads as a success in history.
3. **Only closed exchanges decay**. The current round is always L0 — otherwise a
   placeholder may replace a `tool_result` that has not come back yet.

## L0→L1: aging (the free tier)

Old exchanges' tool-result bodies are replaced whole by a placeholder:

```
[Tool output aged out of context to save room. ...about {n:,} characters...
If you need the actual content, run the tool again — do not tell the user the
result is lost.]
```

Three points:

- **L1 is "replacement", not "a second truncation"**. The existing per-item
  safety valve (`_compress_tool_results_inplace`, runs before `_append`) is
  admission — truncating only when one result would blow up the request on its
  own. L1 is aging — history is not worth carrying in full every turn. The two
  are orthogonal and never fight: whether the original 50K was valve-truncated
  to 12K or not, at L1 it is replaced whole.
- **`MAX_PER_RUN = 40` (decay.py:54) is not about performance; it prevents
  runaway**: a "demote until target" loop, if the target can never be met for
  some other reason, would decay the entire history in one round. Better to
  fall short this round and continue next round.
- Hence one old claim must be corrected: `conversation_messages` stores the
  **normalized history after the safety valve**, not "every raw byte the tools
  returned".

## L1→L2: distillation (the paid tier)

Trigger: L1 usage exceeds the model's quota (`_quota.L1` × window from
`data/model_config.json`, via `quota_of` / `window_of`). Each demotion is one
LLM call, so it runs as a batch — but **the batch caps call count, not success
judgment**.

Key constants (decay.py :54-73):

| Constant | Value | Why |
|---|---|---|
| `MAX_DISTILL_PER_RUN` | 6 | a separate cap for the expensive tier; guards the "uncatchable backlog" — budget full all day, next morning dozens of exchanges queued. `MAX_PER_RUN` prevents runaway; this prevents bills. When falling behind, it **must warn loudly**: a quietly falling-behind queue lives forever as "why is the context always over". |
| `MAX_PRIOR_LINES` | 8 | how many **earlier** low-resolution lines the distiller sees (`_prior_for`) |
| `_DISTILL_MAX_TOKENS` | 2000 | output cap for the distillation call — the number must actually reach the provider, or the schema caps are decoration |

### The Digest contract — `core/context/digest.py`

Five schema fields: `kind / status / outcome / referents / open_items`.
**This module does not call the model**; it defines what a digest line looks
like and what to ask. The call lives in decay.py.

- **Why a fixed schema**: output size is decided by the schema, not input
  length. This kills generation-loss from "digesting digests" (symptom: the
  digest floor rising 400k→600k→800k→amnesia).
- **`kind` is decided by code, not the model** (`kind_of`): any tool call/result
  in the exchange → `work`, else `talk`. Pure-advice exchanges are deliberately
  `talk` — there is no "did it succeed" to answer; forcing `done` is exactly
  "the schema polluting the facts".
- **`tool_facts` is counted by code** (calls, errors) — objective facts fed to
  the distiller. If it can be counted, don't make the model count it.
- **Caps (enforced by `validate`)**: `outcome ≤ 400 chars`, `referents ≤ 6×80`,
  `open_items ≤ 4×80`.
- **The user's words are kept verbatim**. L2 compresses "one half": the user's
  message stays as-is; only the Nano/tool/intermediate half becomes the digest.
  This resolved a self-contradiction — "L2 = one exchange into one line" and
  "user words kept verbatim" conflict literally; split in two, the
  non-regenerable user words are truly kept (measured: human-typed text is
  0.32% of a session).
- **It does not reuse the `semantic_memories` schema**. That table shapes "what
  Nano learned" (long-term knowledge lifecycle); this is "what happened in this
  exchange". Forcing it makes the model bend ordinary conversation into lessons.
  L2 stands alone; the handover (bridge) does an explicit mapping.

## L2→L3: handover (fail-closed)

`bridge.py` runs an immutable order: **produce recallable content → persist →
verify it is actually retrievable → generate the L3 index → only then commit
L3**. Any step failing → stay at L2.

- **Direction is decided by who pays for failure**: staying at L2 costs some
  extra context (cheap); the reverse failure loses content permanently and
  silently (irreversible). Hence it must run before eviction.
- **Two legs**: SQLite `semantic_memories` is the **authority** — failure there
  fails everything; the vector store `memory_index` is **associative recall** —
  failure only degrades, but must warn loudly: a quietly degraded recall leg
  makes people believe recall was always this weak.
- **`memory_type = "exchange"`, separate from `task_pattern` / `correction`**.
  Forcing it into an old type would pollute the type-filtered recall leg,
  silently.

## L3→L4: index expiry

Only the index entry is removed; **semantic memory is not deleted**.
Forgetting = no longer recalled automatically, not erasure — the user can still
ask about it; the memory stays in the store.

## Hands-on recipes

**Case A: tune batch sizes and caps**
Constants block at decay.py :54-73. Touch `MAX_PER_RUN` only for runaway
protection; think about the bill before touching `MAX_DISTILL_PER_RUN`. After
changing, verify on a real machine with `_quota_override` (in
`data/model_config.json` under `_settings`), and set it back to `null` after —
while active, every quota read warns loudly, on purpose.

**Case B: change the Digest schema**
`digest.py`: `FIELDS` + `empty()` + `validate()` + `render_line()` + the prompt
constants — five places, one change. Note: **`is_stale` checks only the source
hash, not the digest shape** — digests already stored under the old schema will
not rebuild automatically; new fields need lenient defaults, and changing a
field's meaning should consider voiding old entries.

**Case C: change the distillation prompt**
The prompt constants live in `digest.py`. After changing, use `quota_override`
to force a batch of demotions and inspect digests: `kind` not bent by the model
(it is pinned by code), `outcome` not truncated, user words intact.

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_f5_decay_l1.py`, `t_f5_decay_l2.py`,
   `t_f5_digest.py`, `t_f5_bridge_l3.py`).
2. Real long conversation + `_quota_override` to accelerate; watch logs and the
   monitor drawer arrow by arrow.
3. Restart and confirm decay state restores (see [07a](07a-memory-data.md)).
4. Self-check the three red lines: pairing intact, `is_error` kept, only closed
   exchanges touched.

---

← Back to [07 overview](07-memory-and-context.md)
