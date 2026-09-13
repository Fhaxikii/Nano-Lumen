# 09a · Startup, window & theme

**What this page covers**: how the process goes from cold start to a usable window — tray residence, the native window, sync-init ordering, the mini window / passive-suspension binding, theming, and SkillWatcher.  
**After reading it you can**: safely change startup order, window behavior, theme switching, or skill hot-reload without introducing "writes UI during startup" failures.  
**Prerequisites**: [09 overview](09-ui.md).  

> Language: [中文](../zh/09a-startup-window.md) · English  

---

## Startup order (a chain that must not be shuffled)

```
process start
→ CrashJournal hooks installed (log: "breadcrumb primary, hooks secondary")
→ Runtime Kernel ready (47 commands · nano_runtime.db)
→ reconciler startup recovery ("should that task continue?" is asked here)
→ skills loaded (registry, the six official skills)
→ WebUI() constructed
→ [synchronous init]   ⚠️ must run synchronously before ui.run(); never on a ui.timer
→ ui.run() 
→ WebView2 native window
→ _on_browser_connect (client connect; re-applies theme; conversation state kept)
```

**Known timing traps (memorize these)**:

1. The **RAG init thread** (a plain `threading.Thread`) starts during WebUI
   construction, before `ui.run()` — the textbook victim of the "background
   threads must explicitly enter the client context to write UI" rule
   (comment at app.py）.
2. **Cleanup left by the previous process must run synchronously before
   `ui.run()`** (comment: not on a ui.timer) — making it async means the
   user may act before cleanup finishes.
3. **Recovery notices (`arm_restart_notice`) attach before WebUI** — restart
   notices depend on the runtime identity (`rt_*`); late means lost.

## Tray and "close means hide"

- System tray (pystray): **clicking close hides to tray, it does not
  exit** — the user-facing half of "resident" (since Changelog v1.11).
- Real exit goes through the tray menu → `_quit`. A PR that makes
  close = exit reverses the resident positioning and will not pass.

## The native window layer

- pywebview + WebView2 rendering; window APIs like
  `nano_set_min_size`  go through a compatibility shim.
- **Per-Monitor DPI awareness must be declared** (log `[OS-Coord]`) — it
  determines the coordinate system of visual locating (see the
  coordinate-consistency check in 06b).

## Mini window & passive suspension (a three-way binding)

⭐ **mini window open = GUI mode = full passive-suspension monitoring — three
behaviors, one state** :

- mini open → GUI mode → Nano's passive suspension starts monitoring (Nano
  restrains itself while the user types in other windows).
- mini closed → GUI mode off → monitoring stops.

Change any branch and check the other two — they are three projections of the
same "the user is working elsewhere" state; split them and you get
half-activated states like "window open but monitoring off".

## Theming

- `_apply_theme_visuals`  is the single entry; after a theme switch the
  **current view is re-applied** (on client reconnect too — theme re-applied,
  conversation state untouched; see `_on_browser_connect`).
- Styles lean on CSS variables (`var(--nano-fg)` etc.) — new components should
  consume them instead of hardcoding colors, so theme switching follows
  automatically.
- After UI changes, walk the area under **both themes** (overview checklist
  item 2).

## SkillWatcher 

File watcher (watchdog) implementing the "detect" half of skill hot-reload;
the reload itself is `core/registry.py`'s `reload_all` (see
[04-writing-a-skill.md](04-writing-a-skill.md)). It runs on the watcher
thread — feedback into the UI (e.g. refreshing the skill list) likewise goes
through the client context.

## Hands-on recipes

**Case A: add initialization to the startup chain**
1. Three questions first: must it be synchronous (may the user act before it
   finishes)? Does it write UI (which thread; is the client up)? Does it depend
   on runtime/kernel (which comes first)?
2. Synchronous, no UI → the sync-init block before `ui.run()`.
3. Async / heavy UI → a `ui.timer` or after the client-connect callback.

**Case B: change window behavior**
Distinguish the two paths "hide to tray" and "exit"; closing stays resident.
DPI-related changes must re-verify visual locating (06b coordinate
consistency).

**Case C: add/change theme variables**
Define them in `_apply_theme_visuals`, consume them in components; verify both
themes (including easy-to-miss corners like tool cards and code blocks).

## How to verify you got it right

1. `bash run_tests.sh` (startup-related: `t_f1_stage7_bgjob.py`,
   `t_replay_chat_events.py`).
2. One cold start (no history) + one warm start (history and a previous
   abnormal exit): the warm start should ask "should that task continue?".
3. Check startup log order: CrashJournal → Kernel → reconciler → skills →
   ui.run (a wrong order usually shows up as odd behavior, not errors).
4. One round of mini-window open/close: monitoring start/stop and window form
   agree across all three states.

---

← Back to [09 overview](09-ui.md)
