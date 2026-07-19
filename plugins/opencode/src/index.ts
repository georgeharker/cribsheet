// OpenCode plugin: run the `crib` (cribsheet) memory + code-index MCP server via
// the `sharedserver` CLI, register its HTTP endpoint with OpenCode, inject the
// reach-for-crib directive, and ship the `/crib` recall command.
//
// This is the OpenCode counterpart of cribsheet's Claude Code plugin (a
// SessionStart shell hook + additionalContext + a /crib command). It mirrors that
// plugin's behaviour:
//
//   1. Stand-down switch — if a combiner already serves crib (global MCP_COMBINER,
//      or the per-backend MCP_COMBINER_SERVES_CRIBSHEET override, which wins), do
//      NOT register a standalone entry and do NOT launch a backend. The combiner
//      owns crib's lifecycle. (Only the MCP registration + launch are gated — the
//      directive and /crib command are injected either way, since crib's tools are
//      present via the combiner too.)
//   2. Registration — inject a `type: "remote"` entry into OpenCode's `mcp` config
//      via the `config` hook.
//   3. Process — drive `sharedserver use … -- crib --mcp --http …` so one warm crib
//      is running and refcounted (shared across clients). `unuse` on exit.
//   4. Directive — append the reach-for-crib text to the system prompt each session
//      via `experimental.chat.system.transform` (the analogue of Claude Code's
//      SessionStart additionalContext).
//   5. /crib command — register an OpenCode command mirroring commands/crib.md.

import { spawnSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type { Plugin } from "@opencode-ai/plugin"

type Options = {
    // ── Registration ──────────────────────────────────────────────
    /** Key under OpenCode's `mcp` config. Default `"cribsheet"`. */
    mcpName?: string
    /** Explicit MCP URL to register. Default `http://127.0.0.1:<port>/mcp`. */
    url?: string
    /** Register the MCP endpoint with OpenCode. Default `true`. */
    register?: boolean
    /** Inject the reach-for-crib directive into the system prompt. Default `true`. */
    instructions?: boolean
    /** Register the `/crib` command. Default `true`. */
    command?: boolean

    // ── Process management ────────────────────────────────────────
    /** Launch/attach crib via sharedserver. Default `true`. */
    manage?: boolean
    /** Explicit path to the `sharedserver` binary. */
    binary?: string
    /** Override SHAREDSERVER_LOCKDIR for child invocations. */
    lockdir?: string
    /** sharedserver instance name. Default `"cribsheet"`. */
    name?: string
    /** sharedserver grace period, e.g. "30m", "1h". Default `"1h"`. */
    gracePeriod?: string
    /** Capture crib's stdout/stderr to this path (sharedserver `--log-file`). */
    logFile?: string

    // ── crib invocation ───────────────────────────────────────────
    /** Override the crib command (else `crib` on PATH, or a checkout). */
    crib?: string
    /** Extra args passed to crib before the serve args. */
    args?: string[]
    /** Path to a cribsheet checkout for `uv run --project <checkout> crib`. */
    checkout?: string
    /** HTTP port crib serves on. Default `7732`. */
    port?: number
    /** HTTP host crib binds. Default `127.0.0.1`. */
    host?: string

    /** Show TUI toasts for attach/health outcomes. Default `true`. */
    notify?: boolean
}

type LogFn = (level: "info" | "warn" | "error", message: string) => void
type ToastFn = (variant: "success" | "warning" | "error", message: string) => void
type OcClient = Parameters<Plugin>[0]["client"]

const DEFAULT_PORT = 7732
const DEFAULT_HOST = "127.0.0.1"
const DEFAULT_NAME = "cribsheet"
const DEFAULT_GRACE = "1h"

// ── the reach-for-crib directive ───────────────────────────────────
// Appended to the system prompt so the agent reaches for crib's tools (the analogue
// of the Claude Code plugin's SessionStart additionalContext). Canonical source:
// CLAUDE.md.example at the repo root (plugins/claude/instructions.txt symlinks it). A
// release-time `prepack` copies that file to this package's root as instructions.txt
// (see package.json `prepack`/`files`); we read the copy ONCE here so the published
// npm package is self-contained without duplicating the text in source. A dev/unbuilt
// run (no copy present) falls back to an empty string and simply injects nothing.
const CRIB_DIRECTIVE: string = (() => {
    try {
        // dist/index.js lives in dist/; the packed copy ships at the package root.
        const here = dirname(fileURLToPath(import.meta.url))
        return readFileSync(join(here, "..", "instructions.txt"), "utf8")
    } catch {
        return ""
    }
})()

// ── the /crib command template (mirrors commands/crib.md) ──────────
const CRIB_COMMAND_TEMPLATE = `Consult crib first — recall memory + the code index for "$ARGUMENTS" before reasoning from scratch.

If "$ARGUMENTS" is empty, ask the user what to recall and stop.

Memory — semantic recall, full matched sections:

!\`crib note apropos "$ARGUMENTS"\`

Code index — symbols by concept or name (skip if the topic is clearly not about code; a "project not indexed" reply just means there's nothing to add here):

!\`crib code lookup "$ARGUMENTS"\`

Then summarize what crib already knows about "$ARGUMENTS": lead with the notes' answer, fold in any relevant code symbols, and cite note/symbol names so the user can \`crib note read\` / \`crib code dossier\` to go deeper. If nothing relevant came back, say so plainly rather than guessing — that's a signal the knowledge isn't captured yet.`

// ── stand-down switch (mirrors the CC hook's combiner_serves) ──────

function truthy(v: string | undefined): boolean {
    if (v == null) return false
    return !["", "0", "false", "no", "off"].includes(v.trim().toLowerCase())
}

/** Does a combiner serve `name`? The per-backend `MCP_COMBINER_SERVES_<NAME>`
 *  override wins over the global `MCP_COMBINER` switch (presence, even empty,
 *  counts as an override — matching the CC hook's `+set` test). */
function combinerServes(name: string, env: NodeJS.ProcessEnv): boolean {
    const key = "MCP_COMBINER_SERVES_" + name.toUpperCase().replace(/[-\s]/g, "_")
    if (key in env) return truthy(env[key])
    return truthy(env.MCP_COMBINER)
}

// ── sharedserver binary resolution (ported) ────────────────────────

const CANDIDATE_BINARIES = [
    "sharedserver",
    join(homedir(), ".cargo", "bin", "sharedserver"),
    join(homedir(), ".local", "bin", "sharedserver"),
    "/usr/local/bin/sharedserver",
    "/opt/homebrew/bin/sharedserver",
]

function resolveBinary(override: string | undefined, env: NodeJS.ProcessEnv): string | undefined {
    const candidates = [override, env.SHAREDSERVER_BIN, ...CANDIDATE_BINARIES].filter(
        (v): v is string => typeof v === "string" && v.length > 0,
    )
    for (const candidate of candidates) {
        if (candidate.includes("/")) {
            if (existsSync(candidate)) return candidate
            continue
        }
        const probe = spawnSync(candidate, ["--version"], { stdio: "ignore", env })
        if (probe.status === 0) return candidate
    }
    return undefined
}

function onPath(cmd: string, env: NodeJS.ProcessEnv): boolean {
    return spawnSync(cmd, ["--version"], { stdio: "ignore", env }).status === 0
}

// ── crib command resolution (mirrors the CC hook: `crib` on PATH) ──

type Command = { cmd: string; args: string[] }

function splitArgs(value: string | undefined): string[] {
    if (!value) return []
    return value.split(/\s+/).filter((s) => s.length > 0)
}

/** Resolve how to invoke crib: explicit option → env command → `crib` on PATH →
 *  `uv run --project <checkout> crib`. */
function resolveCrib(opts: Options, env: NodeJS.ProcessEnv): Command | undefined {
    const extra = opts.args ?? []
    if (opts.crib) return { cmd: opts.crib, args: extra }
    if (env.OPENCODE_CRIBSHEET_COMMAND) {
        return {
            cmd: env.OPENCODE_CRIBSHEET_COMMAND,
            args: [...splitArgs(env.OPENCODE_CRIBSHEET_ARGS), ...extra],
        }
    }
    if (onPath("crib", env)) return { cmd: "crib", args: extra }
    const checkout = opts.checkout ?? env.OPENCODE_CRIBSHEET_CHECKOUT
    if (checkout && onPath("uv", env)) {
        return { cmd: "uv", args: ["run", "--project", checkout, "crib", ...extra] }
    }
    return undefined
}

// ── sharedserver lifecycle (ported) ────────────────────────────────

type PreState = "active" | "grace" | "stopped" | "unknown"

function preCheck(binary: string, name: string, env: NodeJS.ProcessEnv): PreState {
    const result = spawnSync(binary, ["check", name], { stdio: "ignore", env })
    switch (result.status) {
        case 0: return "active"
        case 1: return "grace"
        case 2: return "stopped"
        default: return "unknown"
    }
}

type ServerInfo = { pid?: number; state?: string }

function readServerInfo(binary: string, name: string, env: NodeJS.ProcessEnv): ServerInfo | undefined {
    const result = spawnSync(binary, ["info", name, "--json"], { env })
    if (result.status !== 0) return undefined
    try {
        return JSON.parse(result.stdout.toString()) as ServerInfo
    } catch {
        return undefined
    }
}

function isPidAlive(pid: number): boolean {
    try {
        process.kill(pid, 0)
        return true
    } catch {
        return false
    }
}

type Attached = { binary: string; name: string; env: NodeJS.ProcessEnv }

const attached: Attached[] = []
let cleanupInstalled = false

function installCleanup() {
    if (cleanupInstalled) return
    cleanupInstalled = true

    const drain = () => {
        while (attached.length) {
            const s = attached.pop()!
            spawnSync(s.binary, ["unuse", s.name, "--pid", String(process.pid)], {
                stdio: "ignore",
                env: s.env,
            })
        }
    }

    process.on("exit", drain)
    for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
        process.on(sig, () => {
            drain()
            process.kill(process.pid, sig)
        })
    }
}

// ── health checks (ported) ─────────────────────────────────────────

function scheduleProcessHealthCheck(
    binary: string, name: string, env: NodeJS.ProcessEnv,
    log: LogFn, toast: ToastFn, delayMs: number,
) {
    setTimeout(() => {
        const info = readServerInfo(binary, name, env)
        if (!info) {
            log("warn", `${name}: process health check returned no data`)
            return
        }
        if (info.state && info.state !== "active") {
            const msg = `${name}: not active after start (state: ${info.state})`
            log("error", msg); toast("error", msg); return
        }
        if (info.pid && !isPidAlive(info.pid)) {
            const msg = `${name}: PID ${info.pid} died shortly after start`
            log("error", msg); toast("error", msg); return
        }
        log("info", `${name}: process healthy (pid=${info.pid}, state=${info.state})`)
    }, delayMs).unref()
}

function scheduleMcpHealthCheck(
    client: OcClient, mcpName: string, log: LogFn, toast: ToastFn, delayMs: number,
) {
    setTimeout(() => {
        client.mcp
            .status()
            .then((res) => {
                const st = res.data?.[mcpName]
                if (!st) {
                    log("warn", `${mcpName}: not present in OpenCode mcp status yet`)
                    return
                }
                switch (st.status) {
                    case "connected":
                        log("info", `${mcpName}: connected`)
                        toast("success", `${mcpName}: connected`)
                        break
                    case "failed":
                        toast("error", `${mcpName}: failed — ${st.error ?? "unknown error"}`)
                        break
                    case "needs_auth":
                    case "needs_client_registration":
                        toast("warning", `${mcpName}: ${st.status}`)
                        break
                    default:
                        log("info", `${mcpName}: status ${st.status}`)
                }
            })
            .catch((err: unknown) => {
                log("warn", `${mcpName}: mcp status check failed: ${err instanceof Error ? err.message : String(err)}`)
            })
    }, delayMs).unref()
}

// ── plugin ─────────────────────────────────────────────────────────

const CribsheetPlugin: Plugin = async ({ client }, options) => {
    const opts = (options ?? {}) as Options
    const notify = opts.notify !== false

    const log: LogFn = (level, message) => {
        client.app.log({ body: { service: "cribsheet", level, message } }).catch(() => {})
    }
    const toast: ToastFn = (variant, message) => {
        if (!notify) return
        setTimeout(() => {
            client.tui.showToast({ body: { title: "cribsheet", message, variant } }).catch(() => {})
        }, 1500).unref()
    }

    const env: NodeJS.ProcessEnv = { ...process.env }
    if (opts.lockdir) env.SHAREDSERVER_LOCKDIR = opts.lockdir

    const port = opts.port ?? DEFAULT_PORT
    const host = opts.host ?? DEFAULT_HOST
    const mcpName = opts.mcpName ?? DEFAULT_NAME
    const name = opts.name ?? DEFAULT_NAME
    const register = opts.register !== false
    const wantInstructions = opts.instructions !== false
    const wantCommand = opts.command !== false
    const url = opts.url ?? `http://127.0.0.1:${port}/mcp`

    const served = combinerServes(mcpName, env)

    // The config hook: MCP registration (gated by the stand-down switch) plus the
    // /crib command (always — crib's tools are present via the combiner too).
    const configHook = async (cfg: {
        mcp?: Record<string, unknown>
        command?: Record<string, unknown>
    }) => {
        if (register) {
            cfg.mcp ??= {}
            if (cfg.mcp[mcpName]) {
                log("info", `mcp "${mcpName}" already configured by the user; leaving as-is`)
            } else if (served) {
                log("info", `a combiner serves "${mcpName}"; not registering a standalone entry`)
            } else {
                cfg.mcp[mcpName] = { type: "remote", url, enabled: true }
                log("info", `registered mcp "${mcpName}" → ${url}`)
            }
        }
        if (wantCommand) {
            cfg.command ??= {}
            if (cfg.command.crib) {
                log("info", `command "/crib" already configured by the user; leaving as-is`)
            } else {
                cfg.command.crib = {
                    template: CRIB_COMMAND_TEMPLATE,
                    description: "Consult crib first — recall memory + the code index for a topic",
                }
            }
        }
    }

    // The directive: appended to the system prompt each session (analogue of the CC
    // plugin's SessionStart additionalContext). Injected regardless of `served`.
    const systemHook = async (_input: unknown, output: { system: string[] }) => {
        if (!wantInstructions || !CRIB_DIRECTIVE) return
        output.system.push(CRIB_DIRECTIVE)
    }

    const hooks = {
        config: configHook,
        "experimental.chat.system.transform": systemHook,
    }

    // The process half — skipped when combiner-served or manage=false.
    if (served) {
        log("info", `a combiner serves "${mcpName}"; not launching a standalone backend`)
        return hooks
    }
    const manage = opts.manage !== false
    if (!manage) {
        log("info", `manage=false; registering ${url} only (assuming crib is started elsewhere)`)
        scheduleMcpHealthCheck(client, mcpName, log, toast, 5000)
        return hooks
    }

    const binary = resolveBinary(opts.binary, env)
    if (!binary) {
        const msg = "sharedserver binary not found; set `binary`/`$SHAREDSERVER_BIN`, or use manage:false"
        log("error", msg); toast("error", msg); return hooks
    }
    const crib = resolveCrib(opts, env)
    if (!crib) {
        const msg =
            "crib command not found; install cribsheet (so `crib` is on PATH), set " +
            "`crib`/`checkout`, or $OPENCODE_CRIBSHEET_COMMAND / $OPENCODE_CRIBSHEET_CHECKOUT"
        log("error", msg); toast("error", msg); return hooks
    }

    // Assemble: crib [extra args] --mcp --http --host <host> --port <port>
    const serve = ["--mcp", "--http", "--host", host, "--port", String(port)]
    const wrapped: Command = { cmd: crib.cmd, args: [...crib.args, ...serve] }

    const useArgs = [
        "use", name,
        "--pid", String(process.pid),
        "--grace-period", opts.gracePeriod ?? DEFAULT_GRACE,
        "--metadata", `opencode-${process.pid}`,
    ]
    if (opts.logFile) useArgs.push("--log-file", opts.logFile)
    useArgs.push("--", wrapped.cmd, ...wrapped.args)

    installCleanup()
    const pre = preCheck(binary, name, env)
    const result = spawnSync(binary, useArgs, { stdio: "pipe", env })

    if (result.error) {
        const msg = `${name}: failed to spawn sharedserver (${result.error.message})`
        log("error", msg); toast("error", msg); return hooks
    }
    if (result.status !== 0) {
        const stderr = result.stderr?.toString().trim()
        const msg = `${name}: sharedserver use exited ${result.status}${stderr ? ` (${stderr})` : ""}`
        log("error", msg); toast("error", msg); return hooks
    }

    attached.push({ binary, name, env })
    if (pre === "stopped" || pre === "unknown") {
        log("info", `started crib "${name}" (${wrapped.cmd} ${wrapped.args.join(" ")})`)
    } else {
        log("info", `attached to running crib "${name}" (was ${pre})`)
    }

    scheduleProcessHealthCheck(binary, name, env, log, toast, 2500)
    scheduleMcpHealthCheck(client, mcpName, log, toast, 5000)
    return hooks
}

export default CribsheetPlugin
