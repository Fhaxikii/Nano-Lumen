# 07d · Long-term memory

**What this page covers**: the three carriers of cross-session memory — semantic memory (what was learned), working memory (what was done), and L3 index entries (which conversation discussed what) — their write triggers, recall paths, and red lines.  
**After reading it you can**: add a memory type, adjust redaction rules, or change a recall path without polluting existing mechanisms.  
**Prerequisites**: [07 overview](07-memory-and-context.md), [07a data & projection](07a-memory-data.md), [07b the decay ladder](07b-decay-ladder.md).  

> Language: [中文](../zh/07d-long-term-memory.md) · English  

---

## Three memories, three lifecycles

| Carrier | Stored in | Remembers | Written by |
|---|---|---|---|
| Semantic memory `semantic_memories` | SQLite (authority) + vector store (association) | long-term knowledge: task patterns, corrections, conversation conclusions | trigger code + bridge (L3) |
| Working memory `working_memory` | `data/nano_memory.db` | an operations event ledger: "what was done" | code instrumentation + a model pseudo-tool |
| L3 index entries | the system dynamic segment | signposts: "which conversation discussed what" | the five-arrow pipeline (L2→L3) |

They **do not substitute for each other**: deleting the vector store only weakens
association; deleting semantic memory is real amnesia; working memory is an
event log, not a knowledge base.

## Semantic memory: what to write, and when

`core/semantic_memory.py`. The **trigger surface is deliberately narrow**,
narrower than originally designed:

- **`task_pattern`**: only when an OS Skill deployment succeeds. The original
  motivation was "OS task reproduction"; data-processing Skills are permanent by
  nature and already served, so their marginal value here is low.
- **`correction`**: only on the "Skill errored, then was fixed" path. Casual
  corrections in ordinary conversation are not wired — that would require
  judgment points all over the top-level routing.
- **`exchange`**: written by `bridge.py` at L2→L3 (see
  [07b the decay ladder](07b-decay-ladder.md)); never reused from the two above.

Two universal disciplines:

- **Dedupe before writing**: `find_semantic_candidates` (SQLite candidates by
  type) then a vector search; on a hit, **update confidence instead of
  creating a new row** -177). Otherwise the same task pattern multiplies
  with usage.
- **Redaction is a hard code rule, never the model's judgment**:
  `redact_sensitive`  hard-blocks passwords/tokens/long random strings via
  the `_SENSITIVE_PATTERNS` regexes . Prefer false positives (replace with
  `[REDACTED_*]`): what enters long-term memory lives a long time, and a missed
  redaction is irreversible.

## Working memory: `core/memory_store.py`

Deliberately distinct from `memory/manager.py` (conversation history, cleared on
reset): this stores **cross-session operation events**, permanently.

- **Two-way writing**: code instrumentation (`add`) plus the model reading
  actively through the `recall_working_memory` pseudo-tool
  (`search` → `format_for_model`/:291).
- ⚠️ **One fixed lesson** (~:252): deleted memories used to come back through
  recall — `search` did not filter status. A `user_note`'s pending state shows
  only in the UI and is excluded from model recall; "deleted" must be excluded
  the same way. **What enters the model's view must leave it upon deletion.**

## Two recall legs

Same source as the bridge in [07b](07b-decay-ladder.md): **SQLite is the
authority; the vector store is association**.

| Leg | Function | On failure |
|---|---|---|
| SQLite by type | `find_semantic_candidates` (memory_store side) | whole path fails |
| Vector association | `memory_index.search` , chroma + the shared `_load_embedder`) | degrade + warn loudly |

Three read entries: `retrieve_task_pattern_hint` ,
`retrieve_correction_hints` , `bridge.recall`  — each bound to its own
`memory_type`. **The wrong type makes a recall leg return irrelevant results,
silently.**

## Hands-on recipes

**Case A: add a new memory_type**
1. Schema/fields in `memory_store.add_semantic_memory` .
2. Write trigger: copy `maybe_write_task_pattern`'s trio — narrow trigger,
   dedupe, redaction.
3. Recall leg: copy `retrieve_*_hint`, **bound to the new type**; never reuse an
   old type's search.
4. If produced by the ladder: add an explicit mapping in `bridge.py` (see
   `MEMORY_TYPE`).
5. Test: follow `t_f5_bridge_l3.py`.

**Case B: change redaction**
`_SENSITIVE_PATTERNS` in `semantic_memory.py`. Principle: **hard regexes, prefer
false positives**; do not introduce "let the model judge sensitivity" —
redacted content lives long, and a missed redaction is irreversible.

**Case C: improve recall quality**
`search` in `memory_index.py` , default top_k 5) and the embedding (shared
`_load_embedder`, see [07c](07c-meter-budget-guard.md)). Changing the embedding
model = re-embedding the whole store — a big-window project, never a drive-by.

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_f5_bridge_l3.py`).
2. Real machine: deploy an OS Skill → a `task_pattern` row appears in
   `semantic_memories` with redacted fields; deploying a similar one again
   updates the row instead of adding one.
3. After deleting a memory, `recall_working_memory` no longer returns it.
4. After the ladder reaches L3: the vector store can recall that digest from a
   natural-language query, and the index entry appears in the system dynamic
   segment.

---

← Back to [07 overview](07-memory-and-context.md)
