# 09b · Chat area & event stream

**What this page covers**: the heart of the UI — how the `agent.handle_query` event stream is consumed into the chat area: the unified text stream, tool cards, response epochs and the continuation seam, scroll policy, and conversation replay.  
**After reading it you can**: add a new event rendering, adjust streaming behavior or tool-card layout without breaking the epoch/seam race-prevention structure.  
**Prerequisites**: [09 overview](09-ui.md).  

> Language: [中文](../zh/09b-chat-events.md) · English  

---

## The consumption skeleton

The chat produces nothing itself; it consumes the orchestrator's event stream
(~:3615):

```python
_stream = self.agent.handle_query(query, image_parts=..., temp_file_hint=...)
async for step in _stream:
    # dispatch rendering by step["event"]
```

**The unified text stream** (this section's soul):
`thought_block_start / thought_block_done / thought_summary` no longer create
separate collapsible blocks — **thinking text and the final answer flow into the
same markdown element in the same font and style**, "thinking and answer as one
continuous flow". `thought_delta` appends piece by piece. Separate blocks for
"the thinking process" were tried and rejected once; don't go back.

## Response epochs and the continuation seam

One "response epoch" owns one set of rendering state `_rs` (current text,
spinner, the metadata row, etc.). **Key mechanics**:

- **One epoch, one status timer**: a carrier completion may **continue the same
  epoch** — before continuing, the old timer must be cancelled and a new one
  started (the `_status_timer_task` cancellation around :3610), or two tasks
  race to render one metadata row.
- **The continuation seam**: when a continuation happens, the preparation logic
  clears `content_md`, resets `current_text`, re-shows the spinner — and on
  failure **degrades to overwriting the same element**, never spawns a second
  one. Log lines prefixed `[Seam]` are this.
- The `waiting_for_carrier` flag distinguishes "waiting for carrier" from
  "generating".

Self-check question when changing streaming behavior: **could the same content
end up with two rendering carriers in a continuation scenario?** If yes, the
seam is broken.

## Tool cards and process visibility

- Tool calls unfold as **cards** in the chat area (layout guarded by
  `t_u7_card_layout` / `t_u8_tool_detail`).
- **Routing and internal step info never enter the chat** — log events go to
  the monitor panel only (comment at :4405). The criterion: "the user needs to
  see the process" goes to chat; "debug info" goes to the monitor.
- Memory actions ("Nano remembered something") go through a **unified bubble +
  pending-confirmation card in the memory drawer** , not a flood of
  detail in chat.

## Scroll policy

`_pin_chat_bottom` / `_scroll_chat_to_bottom` : **stay glued to
the bottom** without yanking the user back while they scroll up through
history (the v1.95 fix). Scroll updates from streaming appends must respect
"the user is currently reading upward".

## Conversation replay

`_replay_durable_conversation` : repaints the persisted ledger as
bubbles after restart / session switch. Two disciplines:

1. **It only paints** — rendering the durable conversation's projection, not
   participating in decay/projection rebuild (that is `rebuild_projection`'s
   territory in 07a; replay reads its result).
2. **Group by role into bubbles, filtering `visible_to_user=False`** — system
   notes the user never saw must not appear after replay (exports share the
   same discipline, see 07a).

## Hands-on recipes

**Case A: render a new event type**
Add a branch in the dispatch. First answer: chat area or monitor panel (criterion
above)? For chat, consider the unified text stream (no separate container for
the new event); run `t_u3_chat_surface.py` after.

**Case B: adjust streaming / continuation behavior**
Before touching `_rs` logic, restate epoch ownership: who owns the timer, who
cancels whom at the seam. When adding a state flag, spell out its value on both
sides of the seam.

**Case C: change scrolling**
Invariant: "stick to bottom without disturbing"; any "force to bottom" needs
the user sending a message as its trigger.

**Case D: change replay**
Touch rendering only, never the projection; verify both fresh and old sessions;
for history with images, verify image references still resolve (the gallery
path in 07a).

## How to verify you got it right

1. `bash run_tests.sh` (this page: `t_u3_chat_surface.py`,
   `t_u7_reply_lifecycle.py`, `t_u8_tool_detail.py`, `t_exit_tool_card.py`).
2. Real machine: one long answer with tool calls — thinking and answer flow
   into one bubble; tool cards expand properly; scrolling up is not yanked back.
3. Continuation scenario (trigger a carrier-then-continue): no duplicated or
   racing metadata rows.
4. Replay after restart: complete bubbles, no system-note leakage, images
   viewable.

---

← Back to [09 overview](09-ui.md)
