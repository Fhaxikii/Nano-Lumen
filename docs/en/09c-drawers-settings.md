# 09c · Drawers, monitor & settings

**What this page covers**: everything outside the chat area — the right drawer (one drawer, many panels), the health-driven monitor cards, the context ring, the six settings pages, and data export.  
**After reading it you can**: add a panel or a setting, adjust monitor display policy, without re-breaking the "multiple drawers fighting over a layout slot" structure that was already fixed.  
**Prerequisites**: [09 overview](09-ui.md).  

> Language: [中文](../zh/09c-drawers-settings.md) · English  

---

## The right drawer: one drawer, many panels

⭐ **The structure was deliberately changed** (comment): there used to be
three independent `ui.right_drawer`s fighting for the same layout slot —
NiceGUI/Quasar's q-layout assumes one drawer per side; with several right
drawers, hide()/toggle() switching made the layout's `padding-right` order-
dependent, and whitespace drifted ("open monitor first, then switch to
another"). **The fix: exactly one `right_drawer`, with multiple content panels
toggling visibility inside** — eliminating the "several drawers fighting for a
slot" premise at the root.

Panel inventory (`WebUI.__init__`, ~:1050):

| Panel | Variable | Contents |
|---|---|---|
| Monitor | `monitor_panel` | health cards + context usage |
| Knowledge base | `kb_panel` | KB files and state |
| Memory | `memory_panel` | pending/confirmed memory cards |
| Background tasks | `tasks_panel` | running tasks + finished records (Clear only hides, never deletes) |
| Sub-agent | `agent_panel` | doppelgänger monitoring (no side button; which one is watched is `_agent_watch`) |

**The right way to add a panel**: one more panel + a nav row inside the
existing `right_drawer` — **never mount a second `ui.right_drawer`**.

The chat area also has a `_task_pill` ("x running task(s)") — the chat-side
entry to the drawer, same source as the drawer badge (`_tasks_badge_label`).

## Monitor cards: availability first, tell the truth

- **Chat area (what the user sees): only "unavailable" gets a card; degraded
  goes to the monitor panel only** (comment) — the goal is a monitor card
  that tells the truth, not stuffing cards into chat.
- Color tiers : **the first three are availability, the last three are
  activity state** — fault states override activity display
  (`_refresh_health_card`, redraws from the HealthRegistry). Something
  like RAG ("degraded but usable") only shows its degraded tier in the panel.
- Card refreshes are **passively triggered** (health events / after operations),
  not a polling flood.

## The context ring: one number, two outlets

- **The monitor card's "context" and the little ring at the input box's
  bottom-right are the same number** — both read `budget.snapshot` .
  Two outlets disagreeing = consumers fetching independently, a bug.
- `_refresh_context_card`  / `_refresh_context_ring` ; ring
  discipline: **unmeasurable / inaccurate → an empty gray ring, never 0%**
  (the monitor-card discipline again: "don't know" must not be drawn as "fine").

## The six settings pages (7104-10700)

| Page | Builder | Contents |
|---|---|---|
| OS Permissions | `_build_settings_permissions`  | the six permission switches (enforcement via 06a perms) |
| Profile | `_build_settings_profile`  | persona |
| Cost caps | `_build_settings_cost_cap`  | soft/hard caps; the hard cap stops requests |
| MCP connections | `_build_settings_mcp`  | server list, states, authorization-card entry |
| Advanced | `_build_settings_advanced`  | vision model, proactivity tier (reserved, no effect yet) |
| General | `_build_settings_general`  | language / usage caps / environment config / **data export** |

Common rule: after a config is persisted, **affected displays refresh
immediately, no restart** 's "persist + refresh monitor card").

## Data export

Entry: Settings → General → Export data . Two semantics already stated
in the UI copy:

- **It exports all conversations, including reset ones** ("reset" means
  abandoned, not deleted — same semantics as 07a's export).
- The export runs `core/runtime/export.py`: Markdown + raw JSON + image copies,
  filtering `visible_to_user=False`.

## Hands-on recipes

**Case A: add a panel or a setting**
Panel: one more panel + nav row + visibility toggle inside the existing
`right_drawer`; never a second drawer. Setting: put it in the matching settings
builder, wire a "persist + refresh immediately" callback; if the new switch
constrains OS actions, wire its enforcement into 06a's perms (the dead-switch
lesson).

**Case B: adjust monitor display**
First classify: availability or activity? The fault-overrides-activity priority
holds; chat only accepts "unavailable" level, everything else lives in the
panel. Run `t_u7_card_layout.py`.

**Case C: add a monitor outlet (a new ring or badge)**
It must consume the same authoritative number (`budget.snapshot` or
HealthRegistry); never fetch independently (two outlets disagreeing = a bug).
Unmeasurable renders as "unknown", not zero.

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_u7_card_layout.py`, `t_l5_bg_drawer.py`,
   `t_d10_user_images.py`).
2. Drawer switching order test: open monitor → KB → memory → tasks in
   sequence; layout padding must not drift (the old multi-drawer disease).
3. Walk the new panel/setting under both themes.
4. Monitor card and ring agree during a long conversation; both show "unknown"
   when the model is unmeasurable.

---

← Back to [09 overview](09-ui.md)
