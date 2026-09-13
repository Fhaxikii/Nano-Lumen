# 07 · Memory and context

**What this page covers**: how conversation history is stored, how it is  
progressively compressed as it grows, and how it is recalled.
**After reading it you can**: locate problems with context bloat or lost  
memory, or modify the compression strategy.
**Prerequisites**: [02-architecture.md](02-architecture.md).  

> Language: [中文](../zh/07-memory-and-context.md) · English

---

## The problem

The longer the conversation, the more goes to the model each turn; cost and
latency rise with it until the context window ceiling is hit.

This project's approach: the older the content, the harder it is compressed
— but never dropped outright. It is distilled step by step into shorter
forms, with a path kept to retrieve the original.

## Three ledgers

Understanding this layer starts with separating three stores. Their order of
authority is fixed:

| Data | Location | Role |
|---|---|---|
| Conversation verbatim | SQLite | The single authority on historical fact — what the user actually said |
| Decay ledger | `core/context/decay_store.py` | Which layer each stretch of conversation is currently in |
| In-memory projection | `storage` in `memory/manager.py` | Temporary form; rebuildable from the two above at any time |

On any disagreement, the verbatim record wins. Pay for one more distillation
rather than treat a stale summary as fact.

The in-memory projection is intentionally different from what is on disk:
in memory, images are replaced by placeholders, and after a restart the
descriptions are recomputed from records. Therefore any check of "did the
content change" must be computed from the verbatim record on disk; computing
it from the projection would declare the content changed on every restart
and re-distill forever.

## Layers

Layers are labeled L0 to L4, written as strings rather than numbers so they
read directly in logs and the database, and so adding a layer later does not
change the meaning of existing data.

| Layer | Meaning |
|---|---|
| L0 | Verbatim. The current turn is always L0 |
| L1 | Aged. Content still exists but is no longer carried in full every turn |
| L2 | Conclusion lines. Structured results after model distillation |
| L3 | Handed off to semantic memory, and verified to be retrievable |
| L4 | Index only |

Only finished turns are demoted; the turn in progress never is.

The cost differs sharply between layers: L0→L1 is a pure rewrite, no model
call; every single L1→L2 step is a model call, so it has its own cap.

## Modules

| File | Responsibility |
|---|---|
| `core/context/meter.py` | Metering: how thick the context currently is |
| `core/context/budget.py` | Budgeting: how far from the watermark |
| `core/context/exchange.py` | Defines "one exchange" — the minimal unit of layering |
| `core/context/decay_store.py` | The decay ledger |
| `core/context/decay.py` | Executes demotions |
| `core/context/digest.py` | Conclusion-line structure and the distillation contract |
| `core/context/bridge.py` | Hands conclusion lines off to semantic memory |
| `core/context/guard.py` | Hard window guard |

`guard.py` is the deterministic backstop: every layer before it is heuristic
and can fail; near the ceiling it simply blocks, no judgment involved.

## Semantic memory

`core/semantic_memory.py` and `core/memory_index.py` handle long-term
cross-session memory. Conclusion lines demoted to L3 are handed off here,
and the handoff verifies they can actually be retrieved — "wrote it, counts
as done" is not assumed.

## Images

Images do not enter layered compression; they take a separate path. They are
stored on disk under `data/chat_images/`, content-addressed, the file name
being the content hash. The conversation keeps only the reference and a
text description; the original is fetched by reference when needed.

The only entry point for storing them is `attach_user_images` in
`memory/manager.py`, which also registers the reference. Bypassing it
produces images that entered the conversation but can never be viewed again
in later turns.

## When modifying

Most defects in this layer do not surface as errors but as "something
quietly disappeared" or "paying twice". Before shipping a change, confirm:

- Checksums are computed over the authoritative data, not the projection.
- Demotion touches only finished turns.
- Every path to disk goes through the same entry point.

---

## How to verify you got it right

1. Have a long conversation; the context usage in the monitor drawer drops
   as layering takes effect.
2. After a restart, layer state is restored correctly — nothing falls back
   to verbatim wholesale.
3. Ask about a fact that appeared only in early conversation: still answered
   correctly.
4. No duplicate distillation: the same stretch of conversation must not
   trigger model calls repeatedly in the log.
5. Run `bash run_tests.sh`.
