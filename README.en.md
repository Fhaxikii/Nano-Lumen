# <img src="assets/nano_icon_preview.png" height="34" alt="Nano" align="top"> Nano-Lumen

![Python](https://img.shields.io/badge/Python-3.10-3776AB) ![Platform](https://img.shields.io/badge/Platform-Windows%20Desktop-0078D6) ![License](https://img.shields.io/badge/License-Apache--2.0-brightgreen) ![Release](https://img.shields.io/github/v/release/Fhaxikii/Nano-Lumen)

![Nano-Lumen](assets/nano-banner.png)

**Nano-Lumen v1.97** · A resident general-purpose agent for Windows desktop · [中文](README.md)

`Resident AI Agent` · `Task-level persistent state` · `Local-first privacy` · `Windows desktop`

---

## 💭 If an AI truly lived on your computer, what would it look like?

Today's common AI Agent paradigm is to temporarily move into the computer and complete a task:

> Open a session →
> Pick a workspace →
> Give it a goal →
> It calls a few tools

Nano starts from a different assumption: **a computer should not be merely a set of tools an AI can call — it should be the environment an agent lives in.**

Files, applications, processes, knowledge, the network, external services — they should not be features wired into an AI one by one, but a world an agent can know, use, and act within.

So the question is no longer just:

> **What can an AI do?**

It becomes:

> **What does an AI need to truly live inside a computer for the long term?**

💡 Nano is an exploration of this question.

---

## 📸 Preview

| Dark | Light |
|---|---|
| ![Dark theme](assets/preview/ui-dark.png) | ![Light theme](assets/preview/ui-light.png) |

---

## ⚙️ Core Architecture: Designing the Agent as a Persistent-State System

Around the goal of persistent state, Nano builds a complete state-guarantee layer at the kernel level:

- **SQLite command kernel**: the single write path for all runtime operations, with invariant checks, idempotent accounting, and precise recovery after crashes.
- **End-to-end crash recovery**: write-ahead logging plus a startup reconciler, covering native crashes that `excepthook` cannot catch.
- **Durable inbox**: your messages are persisted the moment they are written and never lost; after a crash, redelivery is announced honestly.
- **Triple fallback for waiting**: timeout / deadline / unconditional orphan reclamation, so tasks never hang indefinitely and the system stays responsive.
- **Persistent memory**: a two-tier stack of working memory and durable semantic memory, supporting active recall.
- **Design perspective**: build the agent as a distributed system with persistent state, not as a one-shot process.
- **Goal-driven**: abandon the traditional tool-driven agent paradigm; give the agent self-evolution, discovery, and autonomous integration instead — its capability ceiling is set by the entire internet ecosystem, not by itself.

> Detailed architecture: [docs/en/02-architecture.md](docs/en/02-architecture.md)

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

- **Full conversation history persisted (SQLite)**: survives restarts, and long conversations are layered-compacted automatically to control cost.
- **Context governance**: conversation content is organized dynamically by context budget, content freshness, and memory levels, keeping continuity while controlling context size and model call cost.
- **Semantic long-term memory**: remembers your preferences and corrections over the long term.
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

![Nano architecture](assets/architecture.en.svg)

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

## Safety

Desktop automation is the highest-risk capability, so Nano layers multiple defenses:

- **Risk takes the maximum**: each action's effective risk = max(declared risk, static floor, dynamic escalation rules); the model can only raise risk, never lower it.
- **Danger master switch**: `PERM_DANGEROUS` is deliberately bound to no specific action; every action reaching the highest risk tier must pass through it.
- **Path deny-list**: explicitly refuses to read credentials, private keys, browser passwords, and other sensitive locations.
- **Explainable denials**: when denied, the model tells you which permission switch to enable.
- **Auto-mode backstop**: even with Auto mode on, destructive commands are still caught by the command classifier.

The six permission switches can be turned off anytime in settings.

Installing or using the software means you have read and agreed to the [User Agreement](docs/en/user-agreement.md).

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
- Testing and contribution guidelines: [docs/en/11-testing.md](docs/en/11-testing.md) and [docs/en/12-contributing.md](docs/en/12-contributing.md)

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

Full **bug report** and **feature request** templates are in the "Reporting Issues" section of [docs/en/12-contributing.md](docs/en/12-contributing.md) — you can fill them in directly.

---

## 🔗 Related Links

- **Developer docs**: [docs/en/README.md](docs/en/README.md)
- **Version history**: [Changelog.txt](Changelog.txt)
- **User Agreement**: [docs/en/user-agreement.md](docs/en/user-agreement.md)
- **Security Policy**: [SECURITY.md](SECURITY.md)

---

## License

This project is licensed under the [**Apache-2.0**](LICENSE) license.

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
- [lebangjames](https://github.com/lebangjames) — data collection, testing and early prototype design

**AI collaborators**

- Claude ([Anthropic](https://www.anthropic.com)) — extensive assistance with parts of the code and debugging
- GPT ([OpenAI](https://openai.com)) — provided early-stage architecture design ideas for the kernel and OS control layer
- GLM ([Zhipu AI](https://www.zhipuai.cn)) — documentation and internationalization