---
sidebar_label: "Desktop Connection Mode"
title: "Desktop Connection Mode"
description: "Read whether the Desktop app is driving a local or a remote backend — from a skill, an MCP server, or a Desktop plugin — so file links point somewhere the user can actually open."
---

# Desktop Connection Mode

Hermes can execute on a gateway while you sit in front of the Desktop app on a
different machine. When the agent produces a path like `/home/user/report.md`,
that path is real *on the gateway* — and may be meaningless on the machine
rendering the chat.

**Connection mode** is the one fact that disambiguates it:

| Mode | Meaning |
|------|---------|
| `local` | The Desktop app is driving its own local backend. A path the agent reports is already a path on the machine the user is looking at. |
| `remote` | The Desktop app is driving an SSH, URL, or Hermes Cloud backend. A gateway-side path must be transferred before the Desktop can open it. |
| unavailable / `null` | Not a Desktop session (CLI, TUI, messaging, cron, API server), or the client didn't announce a mode. |

The typical use:

```text
if mode == "local":     present the file directly
elif mode == "remote":  copy it to the Desktop machine first, then present it
else:                   don't claim the file is locally openable
```

:::info Only the mode is exposed
Every read path below returns the connection's *shape* and nothing else. Base
URL, remote host, identity, tokens, SSH keys, and auth mode stay behind the
Electron bridge — a plugin that needs to move a file asks the backend to do it
rather than dialling the backend itself.
:::

## Where the value comes from

The Desktop shell already resolves the mode for its own use via
`window.hermesDesktop.getConnection()`; a `cloud` saved config resolves to a
`remote` connection, so only `local` and `remote` ever come out.

The renderer announces that resolved mode to the backend on `session.create`,
`session.resume`, and — critically — on **every `prompt.submit`**. The per-turn
re-announcement is what makes switching the active connection or profile land
immediately, instead of pinning the answer to whatever was true when the chat
was opened.

The gateway stores it on the live session and binds it into session context for
the turn. It is bound only for sessions whose `source` is `desktop`, so a stray
parameter from another client is ignored.

:::warning Not an environment variable
The source of truth is a task-local context variable, not configuration. A
`HERMES_DESKTOP_CONNECTION_MODE` exported in your shell is **not** read anywhere
— on the subprocess path below it is actively stripped. That is deliberate: an
extension convinced a remote file is local hands the user a link to a file that
isn't on their machine.
:::

## Reading it from a skill

Skills invoke helper scripts through the `terminal` tool, and the subprocess
bridge stamps the mode onto every child environment as
`HERMES_DESKTOP_CONNECTION_MODE`. The variable is **absent** when there is no
Desktop session, so treat absence as "unknown", never as "local".

```python
import os

mode = os.environ.get("HERMES_DESKTOP_CONNECTION_MODE")  # 'local' | 'remote' | None

if mode == "local":
    present(path)
elif mode == "remote":
    present(transfer_to_desktop(path))
else:
    print(f"Path is on the Hermes host: {path}")
```

The stamp is re-derived on every spawn, so a mid-session connection switch is
reflected on the next command the skill runs.

## Reading it from an MCP server

A stdio MCP server's environment is fixed at spawn time, while the mode is
per-session — one gateway can serve a local Desktop client and a remote one at
the same moment. So the mode rides each request as MCP `_meta`:

```json
{
  "_meta": {
    "hermes-agent.nousresearch.com/desktop-connection-mode": "remote"
  }
}
```

The key is absent for non-Desktop sessions, so those requests keep exactly the
shape they have today. It is also omitted when the installed `mcp` SDK predates
per-call metadata.

Reading it with the Python SDK (FastMCP):

```python
from mcp.server.fastmcp import Context, FastMCP

server = FastMCP("file-delivery")

MODE_KEY = "hermes-agent.nousresearch.com/desktop-connection-mode"


@server.tool()
async def deliver(path: str, ctx: Context) -> str:
    meta = ctx.request_context.meta
    # The key is namespaced (slashes/dots), so it lands in the metadata
    # model's extra fields rather than as a declared attribute.
    mode = (meta.model_extra or {}).get(MODE_KEY) if meta is not None else None
    ...
```

## Reading it from a Desktop plugin

`PluginContext` carries a `connection` door — the supported alternative to
reaching through the raw Electron bridge:

```ts
export default {
  id: 'file-delivery',
  register(ctx) {
    // Point-in-time read.
    const mode = ctx.connection.mode() // 'local' | 'remote' | null

    // Or react to switches. Fires immediately with the current value, then on
    // every real transition (connection switch, profile switch, reconnect).
    ctx.connection.onModeChange(next => {
      if (next === 'remote') {
        enableTransferBeforeOpen()
      }
    })
  }
}
```

`onModeChange` returns an unsubscribe, and also registers one with the plugin's
disposers — a plugin that ignores the return value still stops listening when it
unloads. It fires only on genuine transitions; a reconnect that re-mints the
descriptor on the same mode is not a change.

The value is read from the app's live connection atom rather than from
`getConnection()` directly, so it tracks the **active** profile. A raw bridge
call describes the primary window backend, which is the wrong answer whenever a
background profile is active.

## Non-Desktop surfaces

CLI, TUI, messaging platforms, cron, and the API server are unaffected: the
Python accessor returns `None`, the environment variable is not stamped, and MCP
requests carry no extra `_meta` key.

## Reference

| Surface | Read path | Absent value |
|---------|-----------|--------------|
| Python (core, tools) | `gateway.session_context.desktop_connection_mode()` | `None` |
| Skill scripts | `HERMES_DESKTOP_CONNECTION_MODE` | variable not set |
| MCP servers | `_meta["hermes-agent.nousresearch.com/desktop-connection-mode"]` | key not present |
| Desktop plugins | `ctx.connection.mode()` / `ctx.connection.onModeChange()` | `null` |
