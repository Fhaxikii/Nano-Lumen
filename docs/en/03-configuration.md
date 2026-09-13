# 03 · Configuration

**What this page covers**: where every config file lives, what each one governs, and how to add a new config item.  
**After reading it you can**: locate the config behind a behavior, or add configuration for a new feature.  
**Prerequisites**: [02-architecture.md](02-architecture.md).

> Language: [中文](../zh/03-configuration.md) · English

---

## Where configuration lives

Configuration is split across three places, partitioned by "who this data
follows":

| Location | Contents | Follows |
|---|---|---|
| `.env` | Secrets, endpoints, proxy | The user and the machine |
| `config/` | Behavior rules, persona, system instruction | The version |
| `data/` | Runtime state and user choices | The user |

The split has a concrete consequence: on upgrade, `config/` may be
overwritten; `data/` and `.env` must not be.

## `.env`

Project root; not under version control.

Every item here can also be filled in through the UI (Settings → General →
Environment). Saving in the UI writes straight back to this file — the two
are the same data. Hand-editing works too.

| Key | Meaning | Editable in UI |
|---|---|---|
| `NANO_API_VENDOR` | Vendor id; see `vendors()` in `core/models.py` for values | yes |
| `NANO_API_RELAY_API_KEY` | API key. RELAY in the name is legacy; the key is stored here whether or not a relay is used | yes |
| `NANO_API_RELAY_BASE_URL` | Relay URL. Empty means the vendor's official endpoint | yes |
| `HTTP_PROXY` / `HTTPS_PROXY` | HTTP proxy. Filling it in the UI writes both | yes |
| `HF_HUB_OFFLINE` | Whether the local retrieval models load offline. Written by `_setup_rag_models.py` after a successful download | no |

### About `HF_HUB_OFFLINE`

Do not add this key by hand ahead of time. If it is `1` before the model
cache exists, loading goes straight into offline mode and the models can
never be downloaded. The only correct moment to write it is after a
successful download, and `_setup_rag_models.py` does that itself.

Conversely, if it is not set after the models are downloaded, there is a
risk: loading will go online to fetch again, and an interrupted fetch rewrites
the local cache's references and writes negative-cache entries. The result:
the weight files are still on disk but can never be loaded again. The record
of that incident is at the top of `core/rag.py`.

`HF_ENDPOINT` (the model-download mirror) is set by `install.ps1` as a
user-level environment variable and is not written to `.env`.

### Escape hatches

The following keys never appear in a default `.env` and have no UI control.
They override default behavior; add them by hand only when actually needed:

| Key | Meaning |
|---|---|
| `NANO_MODEL` | Force the main model, bypassing the UI selection |
| `NANO_RELAY_DEFAULT_MODEL` | Force the default model |
| `NANO_ENABLE_CLAUDE_THINKING` | When `0`, thinking parameters are not sent |
| `NANO_THINKING_BUDGET` | Thinking budget cap; only applies to models that take an explicit budget |
| `TESSERACT_CMD` | Path to the OCR executable. When unset, PATH and the default install locations are searched |

`ANTHROPIC_API_KEY` is deprecated. Older versions chose which key to store
the secret in based on whether a relay URL was filled; everything is
unified into `NANO_API_RELAY_API_KEY` now. The code still reads it only for
backward compatibility; do not write it in new configs.

### Clearing a key

Clearing a field in the UI deletes the whole line rather than commenting it
out. Keeping the value in a comment means the UI shows empty while the file
still has it — and packaging or sharing the file carries the secret out with
it.

## `config/`

| File | Contents |
|---|---|
| `os_config.json` | Desktop automation: risk escalation rules, limits, self-check parameters |
| `mcp_servers.json` | MCP server list |
| `persona.txt` | Persona |
| `system_instruction.txt` | System instruction |

The risk escalation rules in `os_config.json` are data, not code, and users
may add to them. Matching and merging logic lives in `core/os_layer/dsl.py`;
the rules themselves are not hardcoded.

## `data/`

Produced at runtime; never hand-edit, never commit to version control.

| File | Contents |
|---|---|
| `model_config.json` | Fact table of vendors and models |
| `throttle_config.json` | Choices the user made in the UI |
| `usage.json` / `usage_config.json` | Usage statistics and quota settings |
| `user_profile.json` | User's personal information |
| `indexed_hashes.json` | Knowledge-base ingest records, for incremental indexing |
| `parse_reports.json` | File parsing results |
| `os_state.json` | The six desktop-automation permission switches and auto mode |
| `context_last_known.json` | Context state snapshot |
| `memory_water.json` | Memory watermark |
| `proactive_affect.json` / `proactive_ledger.json` | Proactive behavior state and records |
| `breadcrumbs.json` | Session trail |
| `models_cache.json` | Cache of model lists returned by vendor APIs |

`data/` also holds directories: `knowledge/` stores knowledge-base files and
`chat_images/` stores images that appeared in conversations, both
content-addressed.

### Why the two tables are separate

`model_config.json` records vendor facts: which models a vendor has, what
each supports. It follows version updates.

`throttle_config.json` records user choices: which main model, which model
per role, which tier the token counter shows. It follows the user.

Mixing them means an upgrade either loses user choices or cannot update
vendor facts.

The same criterion applies to desktop automation:
`config/os_config.json` holds version facts (risk escalation rules, limits,
self-check parameters); `data/os_state.json` holds user state (the six
permission switches and auto mode).

The consequence is heavier here than for the model table. If the permission
switches lived in `config/`, they would ship with the version — sending a
used machine's switches (including the dangerous-operations master switch)
to a new user means every gate arrives open. The on-disk location is decided
by the single function `os_state_path()` in `core/os_layer/dsl.py`; both the
read and the write side go through it.

## Adding a config item

1. Decide who the data follows; that decides `.env`, `config/`, or `data/`.
2. Provide a default when reading; a missing config must not break startup.
3. If it needs a UI control, add it to the settings panel in `app.py` and
   make it take effect immediately on save, no restart required.
4. If the same data is read or written from several places, converge on one
   function that every entry point calls. With multiple write paths, one
   violator is enough to make the rule meaningless.

---

## How to verify you got it right

1. Delete the config item and start: defaults apply, no errors.
2. Change it in the UI: the behavior changes immediately, no restart.
3. Restart: the setting survives.
4. Run `bash run_tests.sh`.
