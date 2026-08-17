// Pi extension: run the `crib` (cribsheet) memory + code-index MCP server via the
// `sharedserver` CLI, inject the reach-for-crib directive into the system prompt, and
// ship the `/crib` recall command.
//
// It is the Pi counterpart of cribsheet's Claude Code and OpenCode plugins, and mirrors
// their behaviour:
//
//   1. Stand-down switch — if a combiner already serves crib (global MCP_COMBINER, or
//      the per-backend MCP_COMBINER_SERVES_CRIBSHEET override, which wins), do NOT
//      launch a standalone backend. The combiner owns crib's lifecycle. Only the launch
//      is gated — the directive and /crib command apply either way, since crib's tools
//      are present via the combiner too.
//   2. Process — on `session_start`, drive `sharedserver use … -- crib --mcp --http …`
//      so one warm crib is running and refcounted (shared across clients). Released on
//      `session_shutdown` when `reason === "quit"` (reload/resume/fork keep the process
//      and re-attach).
//   3. Directive — append the reach-for-crib text to the system prompt via
//      `before_agent_start` (analogue of Claude Code's SessionStart additionalContext
//      and OpenCode's system.transform).
//   4. /crib command — a Pi command mirroring commands/crib.md: shells out to the crib
//      CLI (`note apropos` + `code lookup`) and steers the model to summarise.
//
// MCP registration itself (pointing pi-mcp-adapter at crib) is a single mcp.json entry
// — see mcp.json.example and the README; static, and unnecessary when combiner-served.
// The sharedserver resolution is ported faithfully from plugins/opencode/src/index.ts.

import { spawnSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type {
    AutocompleteItem,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    SessionShutdownEvent,
} from "./pi.js"
import { resolveSharedserver } from "./sharedserver-resolve.js"

const DEFAULT_PORT = 7732
const DEFAULT_HOST = "127.0.0.1"
const DEFAULT_NAME = "cribsheet"
const DEFAULT_GRACE = "1h"
// Floor-only against sharedserver's latest release: cribsheet consumes sharedserver
// rather than shipping it. Kept equal to the sibling plugins' value.
const SHAREDSERVER_MIN_VERSION = "0.6.7"

type LogFn = (level: "info" | "warn" | "error", message: string) => void

// ── the reach-for-crib directive ───────────────────────────────────
// Appended to the system prompt so the agent reaches for crib's tools. Canonical
// source: CLAUDE.md.example at the repo root; a release-time `prepack` copies it to this
// package's root as instructions.txt (see package.json). A dev/unbuilt run without the
// copy falls back to empty and simply injects nothing.
const CRIB_DIRECTIVE: string = (() => {
    try {
        const here = dirname(fileURLToPath(import.meta.url))
        return readFileSync(join(here, "..", "instructions.txt"), "utf8")
    } catch {
        return ""
    }
})()
const DIRECTIVE_MARKER = CRIB_DIRECTIVE.split("\n", 1)[0] ?? ""

// ── env configuration ──────────────────────────────────────────────
// The PI_CRIBSHEET_* namespace mirrors the OpenCode plugin's OPENCODE_CRIBSHEET_* and
// the CC hook's CRIBSHEET_* set — one namespace per client.

function env(name: string): string | undefined {
    const v = process.env[name]
    return v !== undefined && v !== "" ? v : undefined
}

function splitArgs(value: string | undefined): string[] {
    if (!value) return []
    return value.split(/\s+/).filter((s) => s.length > 0)
}

// ── stand-down switch (mirrors the CC hook's combiner_serves) ──────

function truthy(v: string | undefined): boolean {
    if (v == null) return false
    return !["", "0", "false", "no", "off"].includes(v.trim().toLowerCase())
}

/** Does a combiner serve `name`? The per-backend `MCP_COMBINER_SERVES_<NAME>` override
 *  wins over the global `MCP_COMBINER` switch (presence, even empty, counts). These are
 *  the cross-tool switches shared with the CC/OpenCode plugins — NOT PI_-namespaced. */
function combinerServes(name: string): boolean {
    const key = "MCP_COMBINER_SERVES_" + name.toUpperCase().replace(/[-\s]/g, "_")
    if (key in process.env) return truthy(process.env[key])
    return truthy(process.env.MCP_COMBINER)
}

// ── crib command resolution (mirrors the OpenCode plugin) ──────────

type Command = { cmd: string; args: string[] }

function onPath(cmd: string): boolean {
    return spawnSync(cmd, ["--version"], { stdio: "ignore", env: process.env }).status === 0
}

/** Resolve how to invoke crib: env command → `crib` on PATH → `uv run --project
 *  <checkout> crib`. Returns the base command; callers append their own args. */
function resolveCrib(): Command | undefined {
    const command = env("PI_CRIBSHEET_COMMAND")
    if (command) return { cmd: command, args: splitArgs(env("PI_CRIBSHEET_ARGS")) }
    if (onPath("crib")) return { cmd: "crib", args: [] }
    const checkout = env("PI_CRIBSHEET_CHECKOUT")
    if (checkout && existsSync(checkout) && onPath("uv")) {
        return { cmd: "uv", args: ["run", "--project", checkout, "crib"] }
    }
    return undefined
}

// ── sharedserver lifecycle ─────────────────────────────────────────

type Attachment = { binary: string; name: string }
let attachment: Attachment | null = null
let cleanupInstalled = false

function installProcessCleanup() {
    if (cleanupInstalled) return
    cleanupInstalled = true
    process.on("exit", () => detach())
    for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
        process.on(sig, () => {
            detach()
            process.kill(process.pid, sig)
        })
    }
}

function detach() {
    if (!attachment) return
    const { binary, name } = attachment
    attachment = null
    spawnSync(binary, ["unuse", name, "--pid", String(process.pid)], { stdio: "ignore", env: process.env })
}

// ── the extension ──────────────────────────────────────────────────

export default function cribsheet(pi: ExtensionAPI): void {
    const notify = env("PI_CRIBSHEET_NOTIFY") !== "false"
    const wantInstructions = env("PI_CRIBSHEET_INSTRUCTIONS") !== "false"
    const wantCommand = env("PI_CRIBSHEET_COMMAND_ENABLE") !== "false"
    const manage = env("PI_CRIBSHEET_MANAGE") !== "false"
    const name = env("PI_CRIBSHEET_NAME") ?? DEFAULT_NAME
    const served = combinerServes(name)

    // ── directive: appended every turn (dup-guarded across turns) ──
    pi.on("before_agent_start", (event) => {
        if (!wantInstructions || !CRIB_DIRECTIVE) return
        if (DIRECTIVE_MARKER && event.systemPrompt.includes(DIRECTIVE_MARKER)) return
        return { systemPrompt: `${event.systemPrompt}\n\n${CRIB_DIRECTIVE}` }
    })

    // ── /crib command: recall memory + code index, then steer the model ──
    if (wantCommand) {
        pi.registerCommand("crib", {
            description: "Consult crib (recall memory + code index); verb: system-prompt shows the directive",
            getArgumentCompletions: (prefix) => completeVerbs(prefix),
            handler: async (args, ctx) => {
                const arg = args.trim()
                if (arg === "system-prompt") {
                    showDirective(ctx, "cribsheet", CRIB_DIRECTIVE, wantInstructions)
                    return
                }
                const topic = arg
                if (!topic) {
                    ctx.ui?.notify?.("crib: give me a topic (e.g. /crib auth flow), or /crib system-prompt", "warn")
                    return
                }
                const crib = resolveCrib()
                if (!crib) {
                    ctx.ui?.notify?.("crib: CLI not found; install cribsheet or set $PI_CRIBSHEET_COMMAND", "error")
                    return
                }
                const run = async (sub: string[]) => {
                    try {
                        const r = await pi.exec(crib.cmd, [...crib.args, ...sub, topic], {
                            signal: ctx.signal,
                            timeout: 30_000,
                        })
                        return r.stdout.trim() || r.stderr.trim()
                    } catch (e) {
                        return `(crib ${sub.join(" ")} failed: ${e instanceof Error ? e.message : String(e)})`
                    }
                }
                const [memory, code] = await Promise.all([run(["note", "apropos"]), run(["code", "lookup"])])
                const content =
                    `Consult crib first for "${topic}" — recalled below. Summarise what crib already knows: ` +
                    "lead with the notes' answer, fold in any relevant code symbols, and cite note/symbol names " +
                    "so the user can `crib note read` / `crib code dossier` to go deeper. If nothing relevant came " +
                    "back, say so plainly rather than guessing.\n\n" +
                    `Memory — semantic recall (crib note apropos):\n${memory}\n\n` +
                    `Code index — symbols by concept or name (crib code lookup):\n${code}`
                pi.sendMessage(
                    { customType: "crib-recall", content, display: true, details: { topic } },
                    { triggerTurn: true, deliverAs: "steer" },
                )
            },
        })
    }

    // Combiner-served or manage=false: nothing to launch (crib runs elsewhere). The
    // directive + command above still apply.
    if (served || !manage) return

    // ── process: launch on session_start, release on session_shutdown("quit") ──
    pi.on("session_start", (_event, ctx) => {
        if (attachment) return

        const log = makeLog(ctx, notify)
        const binary = resolveSharedserver(
            {
                label: "cribsheet",
                minVersion: SHAREDSERVER_MIN_VERSION,
                installerUrl:
                    "https://github.com/georgeharker/sharedserver/releases/latest/download/sharedserver-installer.sh",
            },
            env("SHAREDSERVER_BIN"),
            process.env,
            log,
        )
        if (!binary) {
            log("error", "sharedserver binary not found; set $SHAREDSERVER_BIN, or PI_CRIBSHEET_MANAGE=false")
            return
        }

        const crib = resolveCrib()
        if (!crib) {
            log(
                "error",
                "crib command not found; install cribsheet (so `crib` is on PATH), or set " +
                    "$PI_CRIBSHEET_COMMAND / $PI_CRIBSHEET_CHECKOUT",
            )
            return
        }

        const port = env("PI_CRIBSHEET_PORT") ?? String(DEFAULT_PORT)
        const host = env("PI_CRIBSHEET_HOST") ?? DEFAULT_HOST
        const grace = env("PI_CRIBSHEET_GRACE") ?? DEFAULT_GRACE

        // Assemble: crib [extra] --mcp --http --host <host> --port <port>
        const serve = ["--mcp", "--http", "--host", host, "--port", port]
        const useArgs = [
            "use",
            name,
            "--pid",
            String(process.pid),
            "--grace-period",
            grace,
            "--metadata",
            `pi-${process.pid}`,
        ]
        const logFile = env("PI_CRIBSHEET_LOG")
        if (logFile && logFile !== "none") useArgs.push("--log-file", logFile)
        useArgs.push("--", crib.cmd, ...crib.args, ...serve)

        installProcessCleanup()
        const result = spawnSync(binary, useArgs, { stdio: "pipe", env: process.env })
        if (result.error) {
            log("error", `${name}: failed to spawn sharedserver (${result.error.message})`)
            return
        }
        if (result.status !== 0) {
            const stderr = result.stderr?.toString().trim()
            log("error", `${name}: sharedserver use exited ${result.status}${stderr ? ` (${stderr})` : ""}`)
            return
        }

        attachment = { binary, name }
        log("info", `crib "${name}" attached on ${host}:${port} (${crib.cmd} ${crib.args.join(" ")})`)
    })

    pi.on("session_shutdown", (event: SessionShutdownEvent) => {
        if (event.reason === "quit") detach()
    })
}

// ── helpers ────────────────────────────────────────────────────────

// Slash-command verbs. `system-prompt` shows the directive this extension injects — the
// show-command pattern from pi-custom-system-prompt, since `before_agent_start`
// injections are per-turn and never appear in Pi's own `/system-prompt` (base only).
const COMMAND_VERBS = ["system-prompt"]
function completeVerbs(prefix: string): AutocompleteItem[] | null {
    const p = prefix.trim()
    const matches = COMMAND_VERBS.filter((v) => v.startsWith(p))
    return matches.length ? matches.map((v) => ({ value: v, label: v })) : null
}

const SHOW_LIMIT = 1600
function showDirective(ctx: ExtensionCommandContext, label: string, directive: string, enabled: boolean): void {
    if (!directive) {
        ctx.ui?.notify?.(`${label}: no directive bundled (instructions.txt missing)`, "warn")
        return
    }
    const head = enabled
        ? `${label} directive — injected into the system prompt on every turn (before_agent_start):`
        : `${label} directive — injection is DISABLED this session; it would be:`
    const body =
        directive.length > SHOW_LIMIT
            ? `${directive.slice(0, SHOW_LIMIT)}\n\n… (${directive.length} chars total)`
            : directive
    ctx.ui?.notify?.(`${head}\n\n${body}`, "info")
}

function makeLog(ctx: ExtensionContext, notify: boolean): LogFn {
    return (level, message) => {
        const line = `cribsheet: ${message}`
        if (notify && ctx.hasUI && ctx.ui?.notify) {
            ctx.ui.notify(line, level === "error" ? "error" : level === "warn" ? "warn" : "info")
        } else if (level === "error" || level === "warn") {
            process.stderr.write(`${line}\n`)
        }
    }
}
