# 🐨 Nano-Lumen

![Nano-Lumen](assets/nano-banner.png)

**Nano-Lumen v1.96** · A resident general-purpose agent for Windows desktop · [中文](README.md)

`Resident agent` · `Cross-session persistent state` · `Local-first privacy` · `Windows desktop`

---

Sessions end. Processes crash. Machines reboot. Nano is different — **it never starts from zero.**

Nano is not another agent shell. It treats the agent as a **system that lives on your machine with persistent state**: state survives across sessions, crashes are recoverable, memory is not reset.

---

## The Core Difference: Session-based vs Continuous

Most mainstream agent products (Claude Code, Cursor, etc.) are **session-based**:

- When a session ends, working state, context, and memory are cleared.
- Starting a new session means re-explaining background and prior context.

Nano is **continuous**, defining the agent's persistence at the architectural level:

- State persists and is recoverable across sessions.
- After a crash, reboot, or power loss, it recovers automatically and honestly asks "should that task continue?"
- It remembers the files you asked it to find and the preferences you've expressed, across sessions.

---

## Core Architecture: Treating the Agent as a Persistent State System

To deliver persistent state, Nano builds a complete state-guarantee layer into the kernel:

- **SQLite command kernel**: the single write path for all runtime operations, with invariant checks, idempotent accounting, and precise recovery after crashes.
- **End-to-end crash recovery**: write-ahead logging plus a startup reconciler that also covers native crashes that `excepthook` cannot catch.
- **Durable inbox**: your messages are persisted the moment they're written and are never lost; on crash repost, it tells you honestly.
- **Triple fallback for waiting**: timeout / deadline / unconditional orphan reclamation, so tasks never hang indefinitely and the system stays responsive.
- **Cross-session memory**: a two-tier stack of working memory and durable semantic memory supporting active recall.

> Design view: **build the agent as a distributed system with persistent state, not as a one-shot process.**

Detailed architecture: [docs/en/02-architecture.md](docs/en/02-architecture.md)

---

## Privacy & Trust

For a desktop agent, privacy is the core of trust. Nano applies three principles at the architecture level: **local-first, minimal collection, restrained behavior**.

**Local-first**

OCR, vector search, memory storage, and the interface all run locally. The only data that leaves the machine is the model API request you yourself configured.

**Minimal collection**

- **Ambient trails** store only a one-line summary of "app + what it's doing" (e.g. "Chrome is browsing a page"), never raw events or plaintext content, and are deleted after 18 hours.
- **The behavior ledger** uses only closed-set category labels (e.g. "file operation", "network access") and forbids storing any semantically sensitive information.

**No telemetry**

Nano itself never collects usage statistics or behavioral data.

---

## Capabilities

**💬 Conversation & Models**

- **Multiple vendors and models**: built-in Anthropic Claude and DeepSeek support, plus relay API support.
- **Streaming replies, visible process**: tool-call cards, usage stats, and more are visible in real time.

**🧰 Tools & Skills**

- **Agent execution engine**: ReAct tool loop, single-file skill system, and standard MCP (Model Context Protocol) support over local stdio or remote HTTP, connecting to any standards-compliant third-party MCP server.
- **30+ built-in tools**: file read/search/edit, RAG retrieval, task lists, background jobs & wait/wake, image/screen, OS automation, MCP & skill management, sub-agents, and more — all discovered and injected on demand to keep per-turn context cost low.
- **Skill self-authoring**: Nano can create, modify, and audit single-file skills from your request, with source viewing, backup-on-update, disable/enable, and delete-to-archive.
- **MCP auto-discovery & management**: auto-searches the official MCP Registry and connects/manages MCP servers on a single command; persistent connections with automatic reconnect and unified management.

**🧠 Memory & Context**

- **Full conversation history persisted** in SQLite; survives restarts, and long conversations are layered-compacted automatically to control cost.
- **Context governance**: conversation content is organized dynamically by context budget, content freshness, and memory levels, keeping continuity while controlling context size and model call cost.
- **Semantic long-term memory**: remembers your preferences and corrections across sessions.
- **Session data export**: exports all sessions to Markdown, raw JSON, and image copies; export respects the user-visibility boundary, so history that no longer appears in the UI keeps a complete data outlet.

**📚 Knowledge Base (RAG)**

- Local embedding models (BGE-M3 + reranker), fully offline retrieval.

**🤖 Proactive Intelligence (experimental, in shadow observation)**

- A three-layer proactive engine (hard safety / routine trigger / state inference) plus an emotion model is implemented, currently in **shadow observation**: it records decisions without speaking, and never messages you unprompted. Emotion only affects tone and the proactive gate, never the underlying functions.

**🎛️ System Operation & Safety**

- **Controlled operation**: system actions are risk-graded with six independent permission switches and a classifier-backed Auto mode that judges dangerous commands by whether the action matches your intent, blocking high-risk actions that fail intent-consistency checks.
- **Trajectory awareness**: records only a summary of "which app and what you're doing" (expiring in ~18 hours); the behavior ledger uses closed categories with no sensitive semantics, and no telemetry by default.
- **Restricted-scope sub-agents**: spawns sub-agents with restricted scope for parallel exploration or execution, isolated via tool and runtime allowlists.
- **Native window**: a native windowed interface built on NiceGUI + WebView2.
- **Auditable action trail**: OS actions (including read-only ones) are written one by one to an append-only audit log; related screenshots are reclaimed by the configured retention policy; idle-time checks verify the visual-locating (UIA) pipeline stays healthy.

---

## Installation & Quick Start

### Requirements

| Item | Requirement |
|---|---|
| OS | Windows 10 or later |
| Python | **3.10** (version-sensitive; newer versions untested) |
| Disk | ~6 GB (including ~2.3 GB local embedding model) |
| Memory | 8 GB or more recommended |

### Install

Run `install.bat` for a one-shot install. The script detects and silently installs Python 3.10.11, then sets up all Python dependencies, local embedding models, Node, OCR, and WebView2; it only asks you to install something manually if a download fails.

```
install.bat
```

Download the local embedding model (optional, for the knowledge base; supports resume):

```
py -3.10 _setup_rag_models.py
```

### Start

```
start.bat
```

### First-run configuration

If no key is configured on first launch, the environment-configuration page pops up automatically. You can also open Settings anytime via the three colored dots in the top-left corner. Choose a model vendor and enter your API Key:

- **Vendor**: Anthropic or DeepSeek
- **API Key**: the key for your chosen vendor

Config is written to `.env` in the project root. Full configuration reference: [docs/en/03-configuration.md](docs/en/03-configuration.md). Getting started and troubleshooting: [docs/en/01-getting-started.md](docs/en/01-getting-started.md).

---

## Skills & Extensibility

Skills are Nano's plugin mechanism: one skill = one Python file.

```
skills/
├── official/          # Built-in skills shipped with the project
│   ├── GetSystemTime.py
│   ├── Base64Codec.py
│   ├── HashGenerator.py
│   ├── RegexTester.py
│   ├── SearchTheWeb.py
│   └── ...
├── disabled/          # Disabled skills
├── deleted/           # Deleted skills
└── skills/ root       # Skills written by Nano live here
```

Dropping a skill file into the `skills/` root auto-loads it, no framework changes needed. You can also simply **ask Nano to write and deploy a new skill, or modify an existing one** (`create_new_skill` / `update_existing_skill`) without writing code yourself.

- Writing a skill: [docs/en/04-writing-a-skill.md](docs/en/04-writing-a-skill.md)
- **MCP**: standard protocol via the official `mcp` SDK, supporting local stdio and remote HTTP, connecting to any standards-compliant MCP server. See [docs/en/05-mcp-servers.md](docs/en/05-mcp-servers.md)

---

## Architecture Overview

```
┌─────────────────────────────┐
│ Window layer pywebview/WebView2 │
└──────────────┬──────────────┘
┌──────────────▼──────────────┐
│ UI layer app.py (NiceGUI)     │
└──────────────┬──────────────┘
┌──────────────▼──────────────┐
│ Orchestration core/orchestrator.py │  ReAct main loop
└──┬──────────┬──────────┬────┘
   │          │          │
┌──▼───┐ ┌───▼────┐ ┌───▼─────┐
│Models │ │Tools   │ │State/    │
│layer  │ │layer   │ │memory    │
│provider│ │tools   │ │memory    │
│models │ │skills  │ │context   │
│       │ │os_layer│ │rag       │
│       │ │mcp     │ │runtime   │
└──────┘ └────────┘ └─────────┘
```

The UI layer never calls models or tools directly; everything goes through the orchestrator. Full details: [docs/en/02-architecture.md](docs/en/02-architecture.md)

---

## Directory Structure

```
Nano-Lumen/
├── app.py                    # UI layer
├── nano_koala.py             # Sprite animation
├── core/                     # Core logic
│   ├── orchestrator.py       # Orchestration / ReAct main loop
│   ├── provider.py           # Model API integration
│   ├── models.py             # Vendor / model tables
│   ├── rag.py                # Knowledge-base retrieval
│   ├── mcp_client.py         # MCP client
│   ├── os_layer/             # Desktop automation
│   ├── context/              # Context metering and decay
│   ├── proactive/            # Proactive intelligence
│   ├── runtime/              # Task scheduling / session persistence
│   └── …                     # Other modules: docs/en/02-architecture.md
├── memory/                   # Conversation history access
├── skills/                   # Skills (plugins)
├── config/                   # Behavior rules / persona / system instructions
├── data/                     # Runtime data
├── docs/                     # Developer docs
├── tests/                    # Test suite
├── assets/                   # Icons / fonts / assets
├── static/                   # Frontend static resources
├── requirements_cpu.txt      # Python dependencies
├── install.bat / install.ps1 # One-shot install
└── start.bat                 # Launch entry
```

---

## Safety

Desktop automation is the highest-risk capability, so Nano layers multiple defenses:

- **Risk takes the maximum**: each action's effective risk = max(declared risk, static floor, dynamic escalation rules); the model can only raise risk, never lower it.
- **Danger master switch**: `PERM_DANGEROUS` is deliberately bound to no specific action; every action reaching the highest risk tier must pass through it.
- **Path deny-list**: explicitly refuses to read credentials, private keys, browser passwords, and other sensitive locations.
- **Explainable denials**: when denied, the model tells you which permission switch to enable.
- **Auto-mode backstop**: even with Auto mode on, destructive commands are still caught by the command classifier.

The six permission switches can be turned off anytime in settings. See [docs/en/06-os-automation.md](docs/en/06-os-automation.md)

---

## Why the Name "Nano"

The name comes from GNU nano — a classic terminal editor. For many of us, nano was the editor we first wrote Python in.

Before GUIs, you had to learn the machine's language to operate a computer; the GUI freed people from memorizing command lines.

The next interaction paradigm we believe in: **the machine learns to understand human speech.**

This is not a finished product. It is the start of a very long road.

---

## Known Limits

This section states only current, concrete limitations. It makes no promise of unimplemented features and sets no release dates. Contributions are welcome.

**Not yet implemented / current limits:**

- Windows 10 or later only; no cross-platform version.
- The proactive engine is in shadow observation and exposes no proactive interaction, and **cannot be toggled on by users**; the "proactivity" control in the UI is a placeholder with **no effect yet**. It will be enabled by changing a code constant once shadow logs are validated and reliability is confirmed.
- The koala animation is sprite-based and bound to the state machine — **not true skeletal animation** — and currently looks somewhat dated.
- All development-time testing used the Claude API; DeepSeek tool-call stability and token-cache hit rate are **not deeply tested**.
- I18N is incomplete: language switching currently only sets the model's preferred output language; **it does not localize the UI**.
- NiceGUI cannot host an embedded browser.

---

## Development & Testing

- The test suite lives in `tests/` and ships with the repo; run the full regression via `run_tests.sh`.

Single case:

```
py -3.10 tests\t_d12_tool_failure_info.py
```

Full suite (requires bash):

```
bash run_tests.sh
```

- Developer docs are in `docs/` (both `en/` and `zh/`, with a `docs/GLOSSARY.md` glossary).
- Testing and contribution guidelines: [docs/en/10-testing.md](docs/en/10-testing.md) and [docs/en/11-contributing.md](docs/en/11-contributing.md)

---

## Reporting Issues

Before filing an issue, please confirm:

- You have read the "common startup problems" section of [docs/en/01-getting-started.md](docs/en/01-getting-started.md).
- You have checked the "Known Limits" section — the current limitations listed there are out of scope for issue reports.

When reporting a bug, please include:

- The Nano version number.
- Your operating system and Python version.
- Reproduction steps, expected behavior, and actual behavior.
- Relevant logs (from the `data/` directory or console output).

Full **bug report** and **feature request** templates are in the "Reporting Issues" section of [docs/en/11-contributing.md](docs/en/11-contributing.md) — you can fill them in directly.

---

## Documentation

- Entry point: [docs/en/README.md](docs/en/README.md)
- Version history: [Changelog.txt](Changelog.txt)

---

## License

This project is licensed under the **Apache-2.0** license.

---

## Acknowledgments
*Nano would not exist without the inspiration and support of these open-source projects, services, and contributors:*

- **Anthropic**: Nano draws heavily on Claude Code's design paradigm. See [Anthropic Engineering](https://www.anthropic.com/engineering)
- **BAAI**: the bge-m3 embedding model and bge-reranker-v2-m3 reranker
- **NiceGUI, pywebview (WebView2), CodeMirror**: desktop UI and interaction
- **SQLite, ChromaDB, Tesseract**: storage, retrieval, and OCR foundations
- **Model Context Protocol (MCP)**: standardizing a plugin-style tool ecosystem
- ***Designing Data-Intensive Applications* by Martin Kleppmann**: the theoretical source of "treating the agent as a system with persistent state"
- **GNU nano**: the origin of the name

**Authors & Contributors**

- [Koala](https://github.com/Fhaxikii) — project author and lead developer
- [lebangjames](https://github.com/lebangjames) — data collection / testing and early prototype design

**AI collaborators**

- Claude ([Anthropic](https://www.anthropic.com)) — extensive assistance with parts of the code and debugging
- GPT ([OpenAI](https://openai.com)) — kernel and OS-control-layer architecture design
- GLM ([Zhipu AI](https://www.zhipuai.cn)) — documentation and internationalization