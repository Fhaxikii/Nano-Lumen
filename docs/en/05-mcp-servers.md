# 05 · Connecting an MCP server

**What this page covers**: how MCP is integrated in this project, the config
shape, the authorization flow, and the project's constraints on it.
**After reading it you can**: add a new MCP server, or modify how existing
ones are integrated.
**Prerequisites**: [02-architecture.md](02-architecture.md),
[03-configuration.md](03-configuration.md).

> Language: [中文](../zh/05-mcp-servers.md) · English

---

## What MCP is

Model Context Protocol — an open protocol for exposing external tools to a
model. This project integrates as a client to protocol-compliant servers,
extending its capabilities without changing its own code.

The protocol itself is out of scope here; consult its official specification.

## Configuration

The server list lives in `config/mcp_servers.json`, using the ecosystem's
usual `mcpServers` shape. A JSON block copied from a server's README will
usually paste in as-is.

Two types:

**Local process (stdio)**
```json
{
  "type": "stdio",
  "command": "python",
  "args": ["-m", "mcp_server_fetch"],
  "enabled": true
}
```

**Remote service (http)**
```json
{
  "type": "http",
  "url": "https://example.com/mcp",
  "headers": {},
  "enabled": true
}
```

`type` may be omitted; it is inferred from the presence of `command` or
`url`. Values support `${VAR}` environment-variable expansion.

## Built-in servers

Five are built in, all marked `builtin: true`:

| Name | Type | Purpose |
|---|---|---|
| `fetch` | stdio | Web page fetching; Python, no Node needed |
| `playwright` | stdio | Browser automation over the accessibility tree and DOM; needs Node |
| `playwright-headless` | stdio | Same, headless |
| `microsoft-learn` | http | Microsoft official documentation search |
| `context7` | http | Open-source library documentation search |

The criteria for what gets built in are under "Our constraints on MCP" below.

## Authorization

Servers that need authorization use "authorization at first use": pasting
the config only registers it, no login prompt up front. The authorization
card appears in the conversation the first time the server is actually used.

The reason: at paste time the user has no intent to use the server yet, so
asking for login then inverts the cause-and-effect between authorization and
use.

The add flow is implemented in `add_server_from_json` in
`core/mcp_client.py`. Before authorization it does exactly three things:
parse the JSON, extract identity, extract the declared capabilities.

The authorization dialog for connecting external capabilities ignores auto
mode — it appears even when auto mode is on. The reason: this step introduces
an external dependency that did not exist before, a change the user must
explicitly know about.

## Connection states

A server is in exactly one of these states, and the UI shows it truthfully:

connected, connecting, connection failed, needs login, disabled, not
connected, reconnecting.

"Intentionally not connected" and "cannot connect" are different things:
the former is normal, the latter is a fault. Showing both as "not connected"
sends users to fix something that isn't broken.

401/403-class errors are classified as "needs authorization", which stops
automatic reconnection — no point hammering a server with requests destined
to fail. The classification logic is in `core/mcp_client.py`.

## Our constraints on MCP

A new built-in server must satisfy all of:

- Free.
- No account registration.
- No API key.

Servers that don't qualify can still be added by users; they just won't be
built in. The same three conditions apply to PRs — see
[11-contributing.md](11-contributing.md).

## Adding a built-in server

1. Confirm it meets the three constraints above.
2. Add an entry in `config/mcp_servers.json`, marked `builtin: true`.
3. If it provides a general capability, fill in the `provides` field for
   capability discovery.
4. If it depends on Node or another runtime, note that in
   [01-getting-started.md](01-getting-started.md).

---

## How to verify you got it right

1. After launch, the server appears under Settings → MCP Connections with
   the correct state.
2. Trigger one of its tool calls; the tool card shows the expected result.
3. Deliberately break the `command` or `url`: the state shows
   "connection failed", not "not connected".
4. For an authorization-requiring server: no login prompt at paste time, the
   prompt appears at first use.
5. Run `bash run_tests.sh`.
