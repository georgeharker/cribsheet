# @geohar/pi-cribsheet

A [Pi](https://pi.dev) extension that makes **cribsheet** (the `crib` memory +
code-index MCP server) available to Pi: it starts `crib` (supervised by
[`sharedserver`](https://github.com/georgeharker/sharedserver)), injects the
reach-for-crib directive into the system prompt, and ships the `/crib` recall command.

It is the Pi counterpart of cribsheet's
[Claude Code](https://github.com/georgeharker/cribsheet/tree/main/plugins/claude) and
[OpenCode](https://github.com/georgeharker/cribsheet/tree/main/plugins/opencode)
plugins, and shares the same `crib` server and `sharedserver` instance — so Pi, Claude
Code, OpenCode, and Neovim all talk to one refcounted process.

## How it fits together

Pi has no MCP of its own. Two pieces give it crib:

1. **[`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter)** — the Pi package that
   speaks MCP. It reads its own `mcp.json` and connects to `crib` over HTTP. **Install
   it too** (`pi install npm:pi-mcp-adapter`).
2. **This extension** — the process + directive + command half:
   - **Run crib** on `session_start` via `sharedserver use … -- crib --mcp --http …`,
     refcounted and shared across clients; released on `session_shutdown` when
     `reason === "quit"`.
   - **Inject the directive** on `before_agent_start` (analogue of CC's
     `additionalContext` and OpenCode's `system.transform`).
   - **`/crib <topic>`** — shells out to the crib CLI (`note apropos` + `code lookup`)
     and steers the model to summarise, mirroring `commands/crib.md`.

### Stand-down when combiner-served

If a **combiner** already serves crib (the global `MCP_COMBINER` switch, or the
per-backend `MCP_COMBINER_SERVES_CRIBSHEET` override, which wins), the extension does
**not** launch a standalone backend — the combiner owns crib's lifecycle. The directive
and `/crib` command still apply, since crib's tools are present via the combiner too. In
that setup you register the *combiner* with pi-mcp-adapter (see the mcp-companion Pi
extension), not crib directly.

## Install

```sh
# build
npm --prefix plugins/pi install && npm --prefix plugins/pi run build
# install into Pi (symlink the package dir; uses "main": dist/index.js)
ln -sfn "$PWD/plugins/pi" ~/.pi/agent/extensions/cribsheet
# MCP transport (skip the mcp.json when crib is combiner-served)
pi install npm:pi-mcp-adapter
cp plugins/pi/mcp.json.example ~/.config/mcp/mcp.json   # standalone only
```

Build-free live dev: `pi -e ./plugins/pi/src/index.ts`.

## Configuration

`PI_CRIBSHEET_*` env namespace (mirrors the OpenCode plugin's `OPENCODE_CRIBSHEET_*`):

| Variable | Default | Effect |
|----------|---------|--------|
| `PI_CRIBSHEET_PORT` | `7732` | HTTP port crib serves on. |
| `PI_CRIBSHEET_HOST` | `127.0.0.1` | HTTP host crib binds. |
| `PI_CRIBSHEET_COMMAND` / `_ARGS` | *(auto: `crib` on PATH)* | Override the crib invocation. |
| `PI_CRIBSHEET_CHECKOUT` | — | Checkout for `uv run --project <checkout> crib`. |
| `PI_CRIBSHEET_NAME` | `cribsheet` | `sharedserver` instance name. |
| `PI_CRIBSHEET_GRACE` | `1h` | `sharedserver` grace period. |
| `PI_CRIBSHEET_LOG` | — | Capture crib's stdout/stderr (`sharedserver --log-file`); `"none"`/unset disables. |
| `PI_CRIBSHEET_MANAGE` | `true` | `false` → don't launch (assume crib runs elsewhere). |
| `PI_CRIBSHEET_INSTRUCTIONS` | `true` | `false` → don't inject the directive. |
| `PI_CRIBSHEET_COMMAND_ENABLE` | `true` | `false` → don't register `/crib`. |
| `PI_CRIBSHEET_NOTIFY` | `true` | `false` → don't surface messages via the Pi UI. |
| `MCP_COMBINER` / `MCP_COMBINER_SERVES_CRIBSHEET` | — | Combiner serves crib → don't launch a standalone backend. |
| `SHAREDSERVER_BIN` / `SHAREDSERVER_LOCKDIR` | *(auto)* | sharedserver binary / lock dir. |

## Development

```sh
npm install && npm run typecheck && npm run build
```

`src/sharedserver-resolve.ts` is vendored byte-identical from
[`georgeharker/sharedserver`](https://github.com/georgeharker/sharedserver) via
`scripts/sync-vendored.sh` — edit upstream, re-sync here.

## License

MIT © George Harker
