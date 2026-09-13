# 10 · Built-in tools & the tool catalog

**What this page covers**: the body of the tool system — the `ToolDefinition` unified registry, the four dimensions (awareness / scheduling / flow / preload), the on-demand loading mechanism (core-resident vs load_tools) with its trade-offs, the two kinds of copy (card vs intent), execution scopes, and "when to choose a built-in tool over MCP or a Skill".  
**After reading it you can**: add a built-in tool, tune its awareness/scheduling/preload attributes, or decide whether a new capability should be built-in, MCP, or a Skill.  
**Prerequisites**: [02-architecture.md](02-architecture.md), [04-writing-a-skill.md](04-writing-a-skill.md), [05-mcp-servers.md](05-mcp-servers.md).  

> Language: [中文](../zh/10-builtin-tools.md) · English  

---

## Choosing among the three capability carriers

The criterion for where a new capability belongs: **how close it is to Nano's
core runtime, and how much runtime state it needs**:

| Carrier | Fits | Does not fit |
|---|---|---|
| **Built-in tool** | needs Nano's runtime state (tasks, interactions, decay, windows, OS); needs fine-grained card/intent copy; participates in scheduling and flow control | purely external capabilities (online services → MCP), pure text processing (scriptable → Skill) |
| **MCP server** | standard-protocol external services (web fetch, browser, docs search); already exists in the ecosystem | deep reliance on runtime state; fine-grained UI cards |
| **Skill** | pure text processing, scriptable, small personalized capabilities | needs runtime facts (task/interaction/decay state); OS actions with side effects |

The core sentence: **a built-in tool can read Nano's runtime view
(`ToolRuntimeView`); MCP and Skills cannot**. Any capability whose very
presence depends on Nano's current state (recheck rounds, recall, wait
management) can only be a built-in tool.

## The unified registry: `ToolDefinition`

`core/tools/builtin.py` is the **single place** built-in tools are declared
(all `D(...)` entries live here). Before the rework, one tool's facts were scattered
across **11 places** — missing one failed silently in its own way. The registry
gathers them into one definition:

```
ToolDefinition (constructed by D(), builtin.py）
├── manifest      the schema the model sees (the single authority)
├── awareness     the one-line awareness the model sees (required, enforced at construction)
├── card          tool-card copy — answers "what Nano is doing"
├── intent        decision copy — answers "what Nano intends" (not the same sentence as card)
├── detail        tool-card detail blocks (a list of DetailBlock)
├── scheduling    SERIAL / PARALLEL_SAFE (may it share a round)
├── flow          CONTINUE / EXCLUSIVE (does it own the round)
├── preload       CORE (always resident) / DEFERRED (waits for load_tools)
├── availability  a runtime-availability predicate over ToolRuntimeView
├── handler       the MAIN-scope execution function
└── agent / agent_handler  enters the Sub-agent scope? (default: no)
```

## On-demand loading: core-resident vs load_tools

**Mechanism** (`core/orchestrator.py`, ):

- Only a **tiny core set** is resident by default; every other tool leaves just
  a one-line "awareness" (name + one sentence) in the prompt — full schemas are
  not injected until the model calls `load_tools(query=...)`.
- `_tool_pool` keeps all gated manifests and is the schema source that
  `load_tools` actually appends — it is not a second list; its content comes
  entirely from `advertised`.

**Pros**: the regular main loop does not carry every tool schema every turn,
keeping per-turn token cost low; the core set is small and stable, so the
cache prefix (tools come first) hits well.

**Cons and edges**: the model must "know what it wants" before `load_tools` —
a poorly written awareness line means it never thinks to load; one extra
load round-trip. Non-dynamic loading (everything resident) is the mirror
image: no load latency, but every turn carries all schemas and the cache
prefix churns every turn.

### ⭐ The two-segment split of the core set (cache-hit critical)

The core set is ordered "**unconditional first, conditional last**" with the
break between:

```
[ unconditional core ] ⟂ [ conditional core ] + [ appended by load_tools ]
```

Why: the cache-prefix order is `tools → system → messages` — tools come first,
so if they change, everything after is invalidated. The conditional core tools
(`dont_wait` / `stop_background` / `set_next_checkin`) **enter and exit
between turns**, touching exactly the most fragile position.
`_core_stable_n` (the unconditional segment's length) and `_core_manifest`
**must be computed together** — never recount elsewhere, or the break lands in
the wrong place, silently.

### availability: a tool and its facts share one condition

Runtime-availability predicates live over `ToolRuntimeView`
(builtin.py-138). Typical ones:

- `_when_has_carrier`: a slow call is running (recheck round ∨ handed back from
  last round) — the two sources cover different moments; with only the first,
  when the user says "put it in the background" the tool is not in the table.
- `_when_has_evicted_history`: only when an exchange has decayed to L3 does
  "recall" appear — **the condition and the index injection are the same
  thing**: index exists ⇔ tool exists.
- `_when_image_needs_summary`: naturally one-shot — once the summary is
  written, the condition turns false.

📌 **Iron rule: a tool and its source of facts must be gated by the same
condition.** "Tool without facts" or "facts without tool" both make the model
guess.

## Two kinds of copy: card ≠ intent

- **card**: answers "what Nano **is doing**" (e.g. "won't wait for it; moving
  on to: research").
- **intent**: answers "what Nano **intends to do**", shown in decision
  confirmations.
- The wording is deliberately different; the old line-by-line comparison table
  only covered cards, so intents slipped outside its scope and silently fell
  back to card copy, changing what users saw.
- Cards should surface **fields meaningful to the user** (like `next_step`),
  not function names — a tool card answers "what is being done", not "which
  function was called".

## Execution scopes (ToolScope, catalog.py）

`MAIN` / `EXPLORATION` (no tools anymore) / `SKILL_WRITER` / `OS_LOOP` /
`AGENT`.

- **AGENT (Sub-agent) has zero tools by default; allowlisted one by one**: a
  new tool declaring only `MAIN` is naturally absent from Sub-agents.
  Exclusion ("just remove X") makes every new tool owe an entry, silently;
  an allowlist only requires remembering what you want.
- Sub-agents can use **another handler** (currently only a read-only
  `os_execute`) — the scope-to-handler mapping is guaranteed by the catalog,
  not by callers remembering to pass the right parameter.

## Five historical lessons (pre-registry inconsistencies, recorded as the correct side)

1. Five tools' `serial` declarations were dead (listed in both SERIAL and EXIT
   tables; EXIT is checked first and returns) → recorded as the real
   `EXCLUSIVE`.
2. Five tools had no awareness → written by hand (required, enforced).
3. `cancel_wait` was in no scheduling table, correct only by fallback →
   recorded explicitly as SERIAL.
4. `os_execute`'s awareness was truncated at 28 chars mid-word → a full
   hand-written sentence, not listing all 39 actions (they reach the search
   docs automatically via the manifest enum).
5. 🔴 **`_BUILTIN_TOOLS_AWARENESS` was a dead table**: twenty hand-written good
   descriptions never reached the model, which only ever saw `[]`
   fragments — "the correct answer written down" and "the wrong answer in use"
   coexisted, unaware of each other. 📌 **A well-written thing nobody calls is
   worse than nothing written**: it creates the illusion that it was handled.

## Hands-on recipes

**Case A: add a built-in tool**
1. Register via `D(...)`: think through the six dimensions — awareness as a
   full sentence; card answers "doing", intent answers "intending";
   scheduling by parallelism; preload by residency (default DEFERRED); if
   availability depends on runtime state, **share the condition with its facts**.
2. Implement the handler + route (MAIN required; Sub-agent needs `agent=True`
   plus a permissions think-through).
3. If it has "facts" (like a new one-shot notice), confirm the injection
   condition shares the tool's condition.
4. Tests: `tests/t_f4_catalog.py` (registry shape) plus scope tests.

**Case B: adjust the core set**
Before changing `preload`, think about the cache prefix: is the tool stable
across turns? Conditional core tools entering and exiting break the cache —
that is why the two-segment split exists. After changing, verify
`_core_stable_n` and `_core_manifest` stay same-sourced.

**Case C: decide where a new capability goes**
Use the table at the top; if unsure, ask — "does its availability depend on
Nano's runtime state?" Yes → built-in. No → prefer MCP (external service) or
Skill (scriptable).

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_f4_catalog.py`,
   `t_f1_stage2a_toolbatch.py`).
2. New tool's visibility in the monitor drawer's dynamic awareness: resident
   tools every turn, conditional ones by condition, deferred only after
   load_tools.
3. Check card and intent copy separately: card answers "doing", the
   confirmation scene answers "intending".
4. Run one long-conversation round and confirm cache hits did not degrade
   (provider logs' cache_read).

---

← Back to [README](README.md)
