# 07 · Memory & Context (Overview)

**What this page covers**: the map of this layer — the three ledgers, the exchange unit, the five-arrow pipeline, and how the four sub-pages divide the work.  
**After reading it you can**: tell which sub-page your change belongs in, and carry the right vocabulary into it.  
**Prerequisites**: [02-architecture.md](02-architecture.md).  

> Language: [中文](../zh/07-memory-and-context.md) · English  

---

## What this layer does

Longer conversations mean more content per turn, rising cost and latency, and
eventually the context window. Two principles govern everything here; the rest
of the design follows from them:

- **Authority and projection are separate**: facts live in exactly one place;
  everything else is a rebuildable projection.
- **Forgetting is not deletion**: decay only means "no longer recalled
  automatically"; the original text and semantic memory both remain.

## The three ledgers (authority order is fixed)

| Ledger | Where | What it holds |
|---|---|---|
| Conversation record | SQLite `conversation_messages` | the single authority on facts — what the user actually said |
| Decay ledger | `core/context/decay_store.py` (`exchange_decay` table) | which decay level each stretch of history is at, and what was derived from it |
| In-memory projection | `storage` in `memory/manager.py` | the transient shape actually sent to the model; rebuildable at any time |

On disagreement, **the conversation record always wins**: paying once more for a
digest beats trusting a stale one. Source hashes and content checks must be
computed over the **persisted ledger** — the projection changes by definition;
using it as the baseline builds a comparison that can never succeed. See
[07a](07a-memory-data.md).

## The unit: one exchange

The smallest unit of decay is "one exchange" (user message → Nano's closing
reply), not a Task: Tasks span multiple turns and are created lazily, so simple
Q&A has no Task at all — grouping by Task would let that whole class of
exchanges bypass decay. In `core/context/exchange.py` it is a **pure function +
read-only view** — a rule for how to group messages, with no storage of its own.

## The five-arrow pipeline

At the end of every round (inside `finally`, once the current exchange is
closed), if the master switch `_settings.ladder_enabled` is true, the
orchestrator runs, in order (search `run_l0_to_l1` in `core/orchestrator.py`):

```
L0→L1  pure rewrite, no model call (free tier, always first)
L1→L2  through the digest step (paid, deliberately after the free tier)
L2→L3  hand over to semantic memory and verify recall (the only user-visible arrow)
L3→L4  index entry expires (semantic memory is never deleted)
──────
rebuild_projection  rebuild the projection from the ledgers (same function as hydration on restart)
notify UI  chat area and model see the same thing
```

Trigger conditions, cost, and red lines per arrow: [07b](07b-decay-ladder.md).

## Sub-page map

| Sub-page | Direction of change |
|---|---|
| [07a · Data & projection](07a-memory-data.md) | the exchange unit, decay ledger, projection rebuild, hydration |
| [07b · The decay ladder](07b-decay-ladder.md) | the five arrows, digest schema, handover verification |
| [07c · Metering, budget & guard](07c-meter-budget-guard.md) | how usage is measured, watermarks, the hard backstop, quota tuning |
| [07d · Long-term memory](07d-long-term-memory.md) | semantic memory, working memory, recall, sensitive-data redaction |

## Images bypass the ladder

Images take a separate path: persisted under `data/chat_images/`,
content-addressed (file name = content hash); the conversation keeps only a
reference and a text note. The single entry point for storing them is
`attach_user_images` in `memory/manager.py`, which also registers the
reference — bypassing it means the image enters the conversation but can never
be viewed again later.

## Three iron rules

Learn these before changing anything here. Most defects in this layer do not
raise errors; they manifest as "something quietly disappeared" or "paying
twice":

1. **Checksums are computed over the persisted ledger**, not the projection.
2. **Only closed exchanges decay**; the current round is always L0.
3. **Forgetting = no longer recalled automatically**; semantic memory is never deleted.

---

## How to verify you got it right

This page is a map and contains no logic to change. Verification lives at the
end of each sub-page; full regression is still `bash run_tests.sh` (this
layer's dedicated tests are the nine `tests/t_f5_*.py` files).

---

← Back to [README](README.md)
