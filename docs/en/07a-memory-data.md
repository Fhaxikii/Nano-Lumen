# 07a · Data & projection

**What this page covers**: the decay ladder's **unit** (the exchange), the **decay ledger** (`exchange_decay`), and **projection rebuild** (`rebuild_projection`) — how the books are kept, what identity means, and how the projection is rebuilt from authority.  
**After reading it you can**: change the exchange grouping rules, add ledger fields, or modify hydration/rebuild without breaking the three-ledger authority order.  
**Prerequisites**: [07 overview](07-memory-and-context.md).  

> Language: [中文](../zh/07a-memory-data.md) · English  

---

## The exchange: unit of the ladder

Defined in `core/context/exchange.py`. **One exchange = the user's message plus
everything up to the next user message.**

```
Exchange                     # dataclass, read-only view （在 exchange.py 中按函数名搜索）
├── index          which exchange this is (from 0)
├── start / end    [closed, open) range in the message list
├── messages       the messages themselves
├── user_message   the opening user message; may be None (see "orphan head")
├── is_orphan      a stretch with no user opening (leftover after truncation)
└── start_ordinal  its starting ordinal in the persisted ledger — its stable identity
```

Four properties you must know:

1. **System notes are included**. Messages with `visible_to_user=False` are part
   of the model's context; decay must count their size. "Visible to the user"
   and "occupies the context" are two different questions.
2. **Identity is derived, not stored**. `start_ordinal` comes from
   `_conversation_ordinal` attached to each message, and both paths attach it:
   `append_message()` (live) and `_message_from_payload()` (hydration). A view
   needs no id — but things hung on the view do.
3. **One concept, one definition**. `user_message`'s predicate must be identical
   to `_opens_exchange` — there was an incident here: the split rule changed
   without changing this property, and an "orphan head starting with a system
   note" was misread as having a user opening. When changing the split rule,
   change both.
4. **It deliberately stays out of the Kernel and has no storage**.
   `split(messages)` and `user_cut_points(messages)` are pure functions
   (exchange.py / :190). Store it and you must answer "who is authoritative,
   this table or the message table" — a question that should not exist.
   Historically this concept was computed inline three times in three shapes —
   the signal that it deserved to become a real entity.

## The decay ledger: `exchange_decay`

Implementation: `core/context/decay_store.py` (class `DecayStore`). Levels
are strings `L0/L1/L2/L3/L4` : readable at a glance in logs and SQLite, and
adding a level (a future L5) never shifts the meaning of existing data.

| Method | Line | Purpose |
|---|---|---|
| `source_hash(session_id, start, end)` | :76 | content hash over the **persisted text** |
| `record(...)` | :95 | record one level migration (and its derivatives) |
| `get` / `load_session` / `active_entries` | :126/:137/:149 | one entry / whole session / unexpired entries |
| `is_stale(entry)` | :176 | hash comparison: source changed → entry void |
| `level_of(session_id, start)` | :202 | current level of one stretch |

**Iron rule: `source_hash` hashes the persisted ledger, not the projection**
(`_hash_rows`, reads the original text straight from SQLite). Why: the
projection is deliberately different from the ledger — images become
placeholders and notes are recomputed on restart, both projection-only. Hash the
projection and every restart produces new hashes, every Digest is judged stale,
and everything is **re-digested infinitely (costing money forever)** — while the
symptom looks exactly like "the content really did change".

## Projection rebuild: `rebuild_projection`

`core/context/decay.py`. The single place where the three-ledger promise is
honored:

```
conversation_messages (facts) + exchange_decay (levels)
        ↓  project_exchange, stretch by stretch
MemoryManager.storage (projection)
```

- **Exactly two call sites**: ① after hydration
  (`MemoryManager._hydrate_current_session` — restart / session switch, ending in
  `app.py`'s `_replay_durable_conversation`); ② after the five arrows each round
  (live). Missing the former loses decay state on restart; missing the latter
  once caused a big hole: `run_l1_to_l2` / `run_l2_to_l3` only touch authority,
  not the projection, so "the table says L2/L3 while the model still carries the
  original text" — it only took effect after a restart.
- **Live and hydration run the same code**, the only reliable way they cannot
  drift — written separately, they are equal only under "I got it right twice".
- **Idempotent and never raises**: the same state rebuilt N times gives the same
  result; a governance failure must never take the conversation down.

## Hands-on recipes

**Case A: add a ledger column** (say, "how much this digest cost")
1. Extend the INSERT and table schema in `DecayStore.record` (~decay_store.py）.
2. Check the three readers (`get` / `load_session` / `active_entries`) for the new column.
3. Old databases: SQLite needs `ALTER TABLE ... ADD COLUMN`, next to the create
   logic in `__init__`.
4. Test: `tests/t_f5_decay_store.py`.

**Case B: change the split rule** (e.g. make a class of system notes open an exchange)
1. Change only `_opens_exchange`（在 exchange.py 中按函数名搜索）.
2. **Check `Exchange.user_message`** (predicates must share one source, property 3).
3. Confirm downstream: `decay.py` and `rebuild_projection` group via `split()`
   and follow automatically, but existing `exchange_decay` rows may have
   `start_ordinal` values that no longer match the new split — `is_stale` catches
   part of it; run `tests/t_f5_exchange.py` and restart once on a real machine
   to verify hydration.

**Case C: change hydration/rebuild behavior**
Change only `rebuild_projection` / `project_exchange` — live and hydration pick
it up together. Do not write a second projection logic inside `app.py`'s
`_replay_durable_conversation`.

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_f5_exchange.py`, `t_f5_decay_store.py`).
2. Trigger decay in a long conversation, then **restart Nano**: decay levels
   should restore exactly; nothing falls back to L0 original text.
3. Trigger two rebuilds from the same state: results must be identical (idempotent).
4. The monitor drawer's context usage should match what the logs claim was saved —
   a mismatch means the projection is not obeying authority.

---

← Back to [07 overview](07-memory-and-context.md)
