// The crib PLAN SOURCE: surfaces cribsheet's plan graph onto pi's in-process bus as
// `plan:snapshot` events, so a plan sidebar (pi-plan TUI, or the pi-acp ACP adapter over
// RPC) can render it. cribsheet is one PLAN source among many; it publishes the shared
// wire format with `ns:"cribsheet"` — only this plugin knows crib's CLI/schema.
//
// Snapshot-based (not delta): each emit carries the WHOLE current cribsheet plan; consumers
// replace their `cribsheet` slice. Prime on session_start; re-snapshot after a crib plan
// MUTATION tool completes (coalesced to turn_end). A missed trigger self-heals on the next
// snapshot — the trigger is a cache-invalidation ping, not the data.
//
// CHANNEL: emits on the shared in-process `pi.events` bus (`plan:snapshot`), which is how an
// in-process consumer (pi-plan's TUI) receives it. `appendEntry` is NOT used here: pi-acp
// runs its own bundled extension that re-emits this bus event as an `acp:plan` custom entry
// to cross RPC (entry_appended is forwarded over RPC, but is NOT delivered to in-process
// extension `on` handlers).

import type { ExtensionAPI } from "./pi.js"

/** One item in the shared plan wire format. Rich fields (`kind`, `tainted`, `deps`) are
 *  populated by cribsheet; a simpler source may omit them. */
export interface PlanItem {
    /** crib's namespaced id, `<kind>:<slug>` (already the wire id scheme). */
    id: string
    /** display text (the wire field is `title`). */
    title: string
    /** kind passed through verbatim from crib (plan|design|note). */
    kind: string
    status: string | null
    /** must-precede refs (this item depends on these), by node id. */
    deps: string[]
    tainted?: boolean
    ulid?: string
    updated?: string
}

/** A full-replace snapshot on the `plan:snapshot` channel. `ns` attributes the source;
 *  `seq` lets a consumer drop out-of-order arrivals. */
export interface PlanSnapshot {
    ns: "cribsheet"
    seq: number
    project?: string
    items: PlanItem[]
}

// ── crib `plan graph --json` shapes ────────────────────────────────
interface CribNode {
    id: string
    ulid?: string
    name?: string
    kind?: string
    status?: string | null
    updated?: string
    tainted?: boolean
}
interface CribEdge {
    from: string
    to: string
    kind: string
}
interface CribGraph {
    project?: string
    nodes?: CribNode[]
    edges?: CribEdge[]
}

/** Map a crib `plan graph` payload into a `source=plan` snapshot. Edge `from -> to`
 *  (kind `dep`) means `from` depends on `to`, so `to` lands in `from`'s `deps`. */
export function graphToSnapshot(graph: CribGraph, seq: number): PlanSnapshot {
    const depsByNode = new Map<string, string[]>()
    for (const e of graph.edges ?? []) {
        if (!e || e.kind !== "dep" || typeof e.from !== "string" || typeof e.to !== "string") continue
        const arr = depsByNode.get(e.from)
        if (arr) arr.push(e.to)
        else depsByNode.set(e.from, [e.to])
    }
    const items: PlanItem[] = (graph.nodes ?? [])
        .filter((n): n is CribNode => !!n && typeof n.id === "string")
        .map((n) => ({
            id: n.id,
            title: typeof n.name === "string" ? n.name : n.id,
            kind: typeof n.kind === "string" ? n.kind : "plan",
            status: n.status ?? null,
            deps: depsByNode.get(n.id) ?? [],
            tainted: n.tainted,
            ulid: n.ulid,
            updated: n.updated,
        }))
    return { ns: "cribsheet", seq, project: graph.project, items }
}

// ── crib plan MUTATION tools (reads are ignored) ───────────────────
// Tools arrive combiner/client-PREFIXED (e.g. cribsheet_plan_add, or the longer
// mcp__…-combiner__cribsheet_plan_add), so match on the TAIL, with a separator
// boundary so `plan_add` can't spuriously match e.g. `explain_add`.
const CRIB_PLAN_MUTATION_VERBS = [
    "plan_dep_remove",
    "plan_dep_add",
    "plan_reaffirm",
    "plan_import",
    "plan_status",
    "plan_forget",
    "plan_move",
    "plan_add",
] as const

export function isCribPlanMutation(toolName: string): boolean {
    for (const verb of CRIB_PLAN_MUTATION_VERBS) {
        if (toolName === verb) return true
        if (toolName.endsWith(verb)) {
            const boundary = toolName[toolName.length - verb.length - 1]
            if (boundary === "_" || boundary === ":" || boundary === "/") return true
        }
    }
    return false
}

/** How to invoke crib — mirrors index.ts's resolveCrib() shape. */
export type CribCommand = { cmd: string; args: string[] }

export interface PlanSourceOptions {
    /** Resolve the crib CLI (env command → `crib` on PATH → `uv run … crib`). */
    resolveCrib: () => CribCommand | undefined
    /** Pull timeout in ms (default 15000). */
    timeoutMs?: number
}

/**
 * Wire the crib plan source into the extension: prime on `session_start`, mark dirty when
 * a crib plan-mutation tool completes, and re-snapshot at `turn_end` if dirty. Emits via
 * `pi.events.emit("plan:snapshot", snapshot)` on the shared in-process bus (for pi-plan's TUI).
 */
export function installCribPlanSource(pi: ExtensionAPI, opts: PlanSourceOptions): void {
    const timeout = opts.timeoutMs ?? 15_000
    let seq = 0
    let dirty = false
    let lastCwd: string | undefined
    // Guard against overlapping pulls (a burst of turn_end + a slow crib).
    let pulling = false

    const pull = async (cwd: string, signal?: AbortSignal): Promise<void> => {
        if (pulling) return
        const crib = opts.resolveCrib()
        if (!crib) return
        pulling = true
        try {
            // Global `--json` goes BEFORE the subcommand. Run crib IN the session cwd
            // and select the project with `-P .` — the CLI resolves it to the absolute
            // client path, so the daemon anchors on the right project (not `default`).
            const res = await pi.exec(crib.cmd, [...crib.args, "--json", "plan", "graph", "-P", "."], {
                signal,
                timeout,
                cwd,
            })
            if (res.code !== 0) return
            let graph: CribGraph
            try {
                graph = JSON.parse(res.stdout) as CribGraph
            } catch {
                return
            }
            pi.events.emit("plan:snapshot", graphToSnapshot(graph, seq++))
        } catch {
            // best effort; a dropped snapshot self-heals on the next trigger
        } finally {
            pulling = false
        }
    }

    pi.on("session_start", (_event, ctx) => {
        lastCwd = ctx.cwd
        // Prime: surface whatever plan already exists (incl. edits from before this session).
        void pull(ctx.cwd, ctx.signal)
    })

    pi.on("tool_execution_end", (event) => {
        if (isCribPlanMutation(event.toolName)) dirty = true
    })

    // Coalesce a turn's worth of mutations into one re-snapshot after they've all landed.
    pi.on("turn_end", (_event, ctx) => {
        if (!dirty) return
        dirty = false
        void pull(ctx.cwd ?? lastCwd ?? process.cwd(), ctx.signal)
    })
}
