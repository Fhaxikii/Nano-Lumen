# 02 · Architecture

**What this page covers**: the process structure, module layout, and the path a user message travels from input to reply.  
**After reading it you can**: decide which module a change belongs in, and understand the terms used throughout the other pages.  
**Prerequisites**: [01-getting-started.md](01-getting-started.md).

> Language: [中文](../zh/02-architecture.md) · English

---

## Process structure

Nano is a single-process desktop application:

![Nano single-process structure](../../assets/architecture.en.detailed.svg)

The UI layer and the orchestration layer live in the same process and
communicate through async generators passing events.
The UI never calls the model or tools directly; everything goes through the
orchestration layer.

## Modules

### UI layer

| File | Responsibility |
|---|---|
| `app.py` | Window construction, chat area, four side drawers, the settings panel, and all style definitions. See [09-ui.md](09-ui.md) |
| `nano_koala.py` | Koala sprite animation |

### Orchestration layer

`core/orchestrator.py`. Organizes one conversation turn: assembles available
tools, builds the context, drives the model's reasoning-and-tool-call loop,
and pushes progress events to the UI.
It is the largest single file in the project and the confluence point for
most cross-module logic.

### Model access layer

| File | Responsibility |
|---|---|
| `core/provider.py` | Actual communication with model APIs: streaming, tool calls, usage accounting |
| `core/models.py` | The fact table for vendors and models: which vendors exist, which models each has, what each supports; the three internal role slots are resolved here too |
| `core/usage.py` | Usage and cost accounting. Note: context thickness belongs to `core/context/meter.py`; the two are never mixed |

Vendor facts come from `core/models.py` only; model names are not hardcoded
anywhere else.

### Tools and capabilities

| File / dir | Responsibility |
|---|---|
| `core/tools/catalog.py` | The unified tool catalog: the single authority on "what a tool is". Register a tool once and awareness text, scheduling policy, and execution bindings are derived |
| `core/tools/builtin.py` | The single declaration point for all built-in tools |
| `skills/` | Skills — pluggable tools that exist as files. See [04-writing-a-skill.md](04-writing-a-skill.md) |
| `core/registry.py` | Skill discovery, loading, and reloading |
| `core/mcp_client.py` | MCP client. See [05-mcp-servers.md](05-mcp-servers.md) |
| `core/mcp_discovery.py` | The first link of the MCP discovery chain: searches the official MCP Registry |
| `core/reading.py` | Iterative reading: probe-read to locate, deep-read slices, keep notes and discard raw text |
| `core/code_scan.py` | Side-effect scanner |
| `core/temp_exec.py` | Scratch execution channel: run throwaway Python in a child process |
| `core/os_layer/` | Desktop automation. See [06-os-automation.md](06-os-automation.md); file breakdown below |

#### `core/os_layer/` file breakdown

| File | Responsibility |
|---|---|
| `dsl.py` | The OS instruction contract: closed action enum, risk floor with dynamic upgrade, hardcoded state transition table |
| `dispatch.py` | Dispatch entry: validate → pre-locate → authorization gate → route to executor → write audit |
| `executor_low.py` | Read-only executor: screenshot, system info, registry read, window tree, cursor position |
| `executor_write.py` | System-API write actions: windows / volume / processes / file read-write / clipboard / open URL |
| `executor_action.py` | Mouse and keyboard actions plus double-brake e-stop |
| `executor_vision.py` | Vision locator: UIA control tree first, falling back to a multimodal vision model when UIA cannot see it |
| `longcmd.py` | Long commands live past their tool call; stdout is replayable |
| `cmd_classifier.py` | Auto-mode dangerous-command judgment |
| `window_binding.py` | Binds which window Nano is operating |
| `fileedit.py` | The pure-compute half of `edit_file`: computes the diff, never writes to disk |
| `filesearch.py` | `search_files`: filename glob plus content grep, merged into one read-only tool |
| `pathpolicy.py` | Path blacklist for file tools |
| `safety.py` | Authorization scope and step counters |
| `audit.py` | Append-only audit log of every OS action |
| `canary.py` | Idle-time self-test of the vision-locating chain; degrades are reported proactively |

### Storage and memory

| File / dir | Responsibility |
|---|---|
| `memory/manager.py` | **In-memory message projection of the current conversation**: ChatMessage list, exchange-aligned truncation, image compression, system notes. Cleared on reset; persistence is delegated to `core/runtime/` |
| `core/runtime/conversation.py` | The authoritative ledger of what was actually said |
| `core/runtime/store.py` | The SQLite persistence layer of the runtime, four tables: tasks / runtime_actions / commands / interactions |
| `core/memory_store.py` | Working Memory: cross-session persistent operation log, recallable by the model |
| `core/semantic_memory.py` | When to write a semantic memory and how to distill/redact before write |
| `core/memory_index.py` | Vector recall for semantic memory, never sharing a collection with the knowledge base |
| `core/runtime/blobs.py` | Content-addressed gallery of images you sent |
| `core/context/` | Context governance: metering, budget, layered decay, digesting. See [07-memory-and-context.md](07-memory-and-context.md) |
| `core/rag.py` | Knowledge-base ingest and retrieval. See [08-knowledge-base.md](08-knowledge-base.md) |

#### `core/runtime/` file breakdown

Nano has no "session" concept — a Task is the unit of state ownership, and
this layer is the runtime machinery built around it.

| File | Responsibility |
|---|---|
| `task.py` | The Task spine: a state-owning unit that spans turns, can be parked, and can go to the background |
| `kernel.py` | The single write entry: every state change goes through `submit()`, where validation and idempotency are enforced in one place |
| `reconciler.py` | Level-triggered convergence: re-derive what to do from current state, never from edge notifications |
| `waitcond.py` | The single authority on "what Nano is waiting for" |
| `interaction.py` | Uniform "needs a user answer" surface: one table, one tool |
| `inbox.py` | User messages are never lost: queued while busy, delivered when the kernel frees up |
| `outbox.py` | Transactional outbox: closes the crash window between state change and external action |
| `oslease.py` | "Who is currently operating this machine" lease |
| `attempt.py` | Where exactly the current action got to |
| `toolbatch.py` | Explicit four-state lifecycle of a tool batch |
| `clock.py` | Injectable unified clock |
| `identity.py` | Process identity `runtime_id`: distinguishes "said by this process" from "stale memory" |
| `projection.py` | Read-only derived UI view: fully rebuildable from authoritative state; the UI never writes business facts back |
| `progress.py` | Progress bus across long commands / MCP / skills: "what is it doing right now" |
| `scheduler.py` | Decides, after a restart, which half-finished jobs should be redone |
| `export.py` | Export the full chat history |

#### `core/context/` file breakdown

Full story in [the 07 series](07-memory-and-context.md); listed here by job:

| File | Responsibility |
|---|---|
| `meter.py` | Context metering: how much of the window the current context fills |
| `budget.py` | Budget water level: how close we are to the high watermark |
| `guard.py` | Hard window guard: the deterministic backstop under the heuristics; forces safe degradation before overflow |
| `exchange.py` | "One exchange" — the unit the decay ladder operates on |
| `decay.py` | Decay execution: L0→L1 pure rewrite, L1→L2 through the distiller |
| `decay_store.py` | The decay ledger: which L level a segment currently projects to |
| `digest.py` | The fixed schema of L2 conclusion lines and the distillation prompt |
| `bridge.py` | The L3 handoff: ship a conclusion to semantic memory and verify it is actually retrievable; failure means it stays at L2 |

### Proactive intelligence (`core/proactive/`)

| File / dir | Responsibility |
|---|---|
| `activity.py` | Real-time behavior telemetry; records only, never judges |
| `hooks.py` | System event hooks: keyboard listener plus foreground window polling |
| `triggers.py` | Builds trigger candidates from a behavior snapshot |
| `speaker.py` | The proactive-speech scheduler: cooldown → content generation → fallback → push |
| `intel/` | The proactive engine: L0 hard safety / L1 calendar ritual / L2 state inference; affect only colors tone |
| `takeover.py` | User-takeover lease: the instant the user moves the mouse or types, Nano yields |
| `referent.py` | Ambient referent resolution: "the file I was just editing" → real path |
| `ambient_trail.py` | Work-context trail: one-line spoken summaries persisted by time band, surviving restarts |
| `app_catalog.py` | The single source of truth for app classification |
| `state.py` | Atomic persistence of the proactive system's state |

### Others

| File | Responsibility |
|---|---|
| `core/health.py` | Capability health registry and self-healing probes: when a capability is unavailable, it decides how the outside world is told |
| `core/crash_journal.py` | Crash breadcrumbs: dangerous operations write a breadcrumb first, so a dead process can be diagnosed at next launch |
| `core/i18n.py` | The single source of truth for "what language is currently in use" |
| `core/schema.py` | Data structures and enums of the skill protocol and internal messages |
| `core/rag.py` | Knowledge base: ingest/parsing, BM25 plus vector dual-path retrieval, reranking, health self-healing |

## The path of one message

```
User input
   │
   ├─ UI layer collects: text, images, temp files, references
   │
   ├─ Written to conversation history (memory projection + runtime ledger)
   │
   ├─ Orchestration layer assembles this turn's context
   │     · system prompt and persona
   │     · available tool catalog (built-in + skills + MCP, loaded on demand)
   │     · conversation history (the context-governed version)
   │
   ├─ Reasoning and tool-call loop
   │     model output → if it contains tool calls → execute → feed results
   │     back → reason again; the loop ends when the model produces final text
   │
   ├─ Events keep streaming to the UI along the way (tool cards, status, logs)
   │
   └─ Final reply is written to conversation history and rendered
```

Tools are not all resident by default. The model sees a slim capability
catalog; the full definition of a tool is fetched via a loader tool only when
its parameters are needed. This keeps the fixed overhead of every turn low.

## Concepts you need to understand

**Skill**
A tool that exists as a single Python file. Drop it into `skills/` and it is
discovered and loaded, no framework changes needed.

**Tool card**
The collapsible UI block that shows one tool call: tool name, arguments,
result.

**Risk level**
How dangerous a desktop automation action is, as an integer. It decides
whether an action requires user authorization. The computation is described
in [06-os-automation.md](06-os-automation.md).

**Anything bound to a Task must carry its own fallback**
Anything whose lifecycle is attached to a Task (the scratchpad, "this turn
only" authorizations, per-task counters, the in-progress display) must NOT
treat the Task's end as its only invalidation condition. It needs a second,
independent fallback: a TTL, a count cap, or startup-time cleanup.

The reason: a Task's end depends on the model remembering to close it out,
and models forget. What forgetting breaks differs per item — an undeleted
scratchpad just leaves a lump in context, an uncleared counter just makes a
number meaningless, but **a "this turn only" authorization that never expires
is a security problem**.

`os.temp_auto` and `os.gui_session` in `core/runtime/oslease.py` are the
positive examples: each has two independent exits — an explicit revoke and a
startup-time sweep — and neither depends on the Task being properly closed.

**Context layers**
In long conversations, older content is progressively distilled and
compressed instead of being truncated, to control how much is sent to the
model each turn. Layers are numbered L0 to L4; L0 is verbatim text and larger
numbers mean more condensed. See [07-memory-and-context.md](07-memory-and-context.md).

---

## How to verify you understood it

Structural understanding cannot be verified directly. The test is: take a
concrete problem and say which file it belongs in. For example:

- "The message text shown when a tool call fails" → UI layer, `app.py`
- "An action that should require authorization doesn't pop the dialog" →
  risk computation in `core/os_layer/dsl.py`
- "The model list didn't update after switching vendors" → `core/models.py`
  and the dropdown refresh in `app.py`
- "An image the user sent is gone from the UI after restart" → `core/runtime/blobs.py`
- "A pending wait sticks on screen forever, even after restart" →
  `core/runtime/waitcond.py` and `reconciler.py`
- "The model says 'I never had that capability' after an MCP failure" →
  `core/health.py`

If you can locate the file for questions like these, this page has done its
job.

---

← Back to [README](README.md)
