# @geohar/opencode-cribsheet

An [OpenCode](https://opencode.ai) plugin that brings **`crib`** (cribsheet —
persistent cross-session markdown memory + a code symbol index) to OpenCode: it
starts crib (supervised by
[`sharedserver`](https://github.com/georgeharker/sharedserver)), registers its
HTTP MCP endpoint, injects the reach-for-crib directive, and ships the `/crib`
recall command — and **stands down** when a combiner already serves crib.

It is the OpenCode counterpart of cribsheet's Claude Code plugin, and mirrors its
behaviour.

## What it does

1. **Stand-down switch.** If a combiner already serves crib, the plugin neither
   registers a standalone MCP entry nor launches a backend (the combiner owns
   crib's lifecycle):
   - `MCP_COMBINER=1` — global switch.
   - `MCP_COMBINER_SERVES_CRIBSHEET=0|1` — per-backend override (wins).

   The directive and `/crib` command are injected **either way** — crib's tools
   are present via the combiner too.
2. **Register.** Otherwise it injects `cribsheet` into OpenCode's `mcp` config as
   a `type: "remote"` endpoint (default `http://127.0.0.1:7732/mcp`). A user-
   defined entry is left untouched.
3. **Run one warm crib.** It drives `sharedserver`:
   ```
   sharedserver use cribsheet --pid <opencode-pid> --grace-period 1h \
       -- crib --mcp --http --host 127.0.0.1 --port 7732
   ```
   `sharedserver` refcounts by PID, so one crib process is shared across clients
   (OpenCode, Claude Code, Neovim) and outlives any single one. `unuse` on exit.
4. **Reach-for-crib directive.** Appended to the system prompt each session via
   `experimental.chat.system.transform` — the analogue of the Claude Code plugin's
   SessionStart `additionalContext`.
5. **`/crib` command.** Registered in OpenCode's `command` config, mirroring
   cribsheet's `commands/crib.md`: recalls memory (`crib note apropos`) and the
   code index (`crib code lookup`) for a topic, then summarizes.

## Install

Add it to your `opencode.json` `plugin` list:

```json
{
  "plugin": [
    "@geohar/opencode-cribsheet@latest"
  ]
}
```

With options (all optional — defaults shown):

```json
{
  "plugin": [
    ["@geohar/opencode-cribsheet@latest", {
      "port": 7732,
      "host": "127.0.0.1",
      "gracePeriod": "1h",
      "manage": true,
      "register": true,
      "instructions": true,
      "command": true,
      "notify": true
    }]
  ]
}
```

## Options

| option | default | meaning |
|---|---|---|
| `mcpName` | `"cribsheet"` | key under OpenCode's `mcp` config |
| `url` | `http://127.0.0.1:<port>/mcp` | MCP URL to register |
| `register` | `true` | register the endpoint with OpenCode |
| `instructions` | `true` | inject the reach-for-crib directive into the system prompt |
| `command` | `true` | register the `/crib` command |
| `manage` | `true` | launch/attach crib via sharedserver (`false` → register only) |
| `binary` | auto | path to `sharedserver` (`$SHAREDSERVER_BIN` also honoured) |
| `lockdir` | — | override `SHAREDSERVER_LOCKDIR` |
| `name` | `"cribsheet"` | sharedserver instance name |
| `gracePeriod` | `"1h"` | sharedserver grace period |
| `logFile` | — | capture crib output (`sharedserver --log-file`) |
| `crib` | `crib` on PATH | override the crib command (`$OPENCODE_CRIBSHEET_COMMAND`) |
| `checkout` | — | cribsheet checkout for `uv run --project` (`$OPENCODE_CRIBSHEET_CHECKOUT`) |
| `port` | `7732` | HTTP port crib serves on |
| `host` | `127.0.0.1` | HTTP host crib binds |
| `notify` | `true` | show TUI toasts for attach/health outcomes |

## Requirements

- `curl` and [`uv`](https://docs.astral.sh/uv/) — **nothing else**. The plugin fetches
  [`sharedserver`](https://github.com/georgeharker/sharedserver) and `crib` itself on
  first use if they are not already present, so no Rust toolchain is needed and
  cribsheet need not be installed by hand.
- To supply your own instead: anything on `PATH`, the `binary` option, or
  `$SHAREDSERVER_BIN` / `$CRIB_BIN` is used as-is and never silently replaced. A
  `checkout` + `uv` also works for a dev tree.

## Relationship to the other plugins

crib behind a **combiner** is served through
[`mcp-combiner`](https://github.com/georgeharker/mcp-companion); set
`MCP_COMBINER=1` (or run `mcp-combiner env-disable`) and this plugin stands down.
The Claude Code counterpart ships in the
[cribsheet](https://github.com/georgeharker/cribsheet) repo's marketplace.
