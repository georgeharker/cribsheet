// Narrow, local typing for the slice of Pi's extension API this plugin uses.
//
// Pi (badlogic/pi-mono, earendil-works/pi) ships its ExtensionAPI types with the
// harness rather than as a standalone npm package we can depend on, so we declare
// exactly the surface we touch — three lifecycle events, `registerCommand`, `exec`,
// and `sendMessage`. Kept deliberately minimal: a wider mirror would rot against a
// moving upstream. Signatures follow the published extension docs
// (https://pi.dev/docs/latest/extensions).

export type SessionStartReason = "startup" | "reload" | "new" | "resume" | "fork"
export type SessionShutdownReason = "quit" | "reload" | "new" | "resume" | "fork"

export type SessionStartEvent = {
    reason: SessionStartReason
    previousSessionFile?: string
}
export type SessionShutdownEvent = {
    reason: SessionShutdownReason
    targetSessionFile?: string
}
export type BeforeAgentStartEvent = { systemPrompt: string }
export type BeforeAgentStartResult = { systemPrompt?: string } | void

/** Fired after a tool finishes. We read only `toolName` (to spot crib plan mutations). */
export type ToolExecutionEndEvent = {
    toolName: string
    args?: unknown
    result?: unknown
    isError?: boolean
}
/** Fired at the end of an agent turn — our natural coalescing point for re-snapshots. */
export type TurnEndEvent = Record<string, never>

export type ExtensionContext = {
    cwd: string
    mode: "tui" | "rpc" | "json" | "print"
    hasUI: boolean
    signal?: AbortSignal
    ui?: {
        notify?: (message: string, level?: "info" | "warn" | "error") => void
    }
}

/** Context handed to a command handler. Superset of ExtensionContext in practice; we
 *  only read `signal`, `ui`, and `hasUI`. */
export type ExtensionCommandContext = ExtensionContext

export type AutocompleteItem = { value: string; label?: string }

export type ExecResult = {
    stdout: string
    stderr: string
    code: number
    killed: boolean
}
export type ExecOptions = {
    signal?: AbortSignal
    timeout?: number
    cwd?: string
    env?: NodeJS.ProcessEnv
}

/** An LLM-visible message injected from a command. `display:true` shows it in the TUI;
 *  `{triggerTurn:true, deliverAs:"steer"}` makes the model act on it this turn. */
export type SendMessage = {
    customType: string
    content: string
    display?: boolean
    details?: Record<string, unknown>
}
export type SendMessageOptions = {
    triggerTurn?: boolean
    deliverAs?: "steer" | "followUp"
}

export type CommandSpec = {
    description: string
    handler: (args: string, ctx: ExtensionCommandContext) => void | Promise<void>
    getArgumentCompletions?: (prefix: string) => AutocompleteItem[] | null
}

export interface ExtensionAPI {
    on(event: "session_start", handler: (event: SessionStartEvent, ctx: ExtensionContext) => void | Promise<void>): void
    on(
        event: "session_shutdown",
        handler: (event: SessionShutdownEvent, ctx: ExtensionContext) => void | Promise<void>,
    ): void
    on(
        event: "before_agent_start",
        handler: (
            event: BeforeAgentStartEvent,
            ctx: ExtensionContext,
        ) => BeforeAgentStartResult | Promise<BeforeAgentStartResult>,
    ): void
    on(
        event: "tool_execution_end",
        handler: (event: ToolExecutionEndEvent, ctx: ExtensionContext) => void | Promise<void>,
    ): void
    on(event: "turn_end", handler: (event: TurnEndEvent, ctx: ExtensionContext) => void | Promise<void>): void
    registerCommand(name: string, spec: CommandSpec): void
    exec(command: string, args: string[], options?: ExecOptions): Promise<ExecResult>
    sendMessage(message: SendMessage, options?: SendMessageOptions): void
    /** Persist a custom session entry; emits `entry_appended` (forwarded over RPC, but NOT
     *  delivered to in-process extension `on` handlers). For RPC consumers (pi-acp). */
    appendEntry(customType: string, data?: unknown): string
    /** Shared in-process pub/sub bus across all extensions (how `subagents:*` crosses
     *  extensions). The channel for in-process consumers like pi-plan's TUI. Not RPC-forwarded. */
    events: {
        on(channel: string, handler: (data: unknown) => void): () => void
        emit(channel: string, data?: unknown): void
    }
}
