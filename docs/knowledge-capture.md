# Knowledge capture & revision (the generation layer)

Status: design. Realizes DESIGN [§7 `distill`](../DESIGN.md) and [§12 automatic
conversation summarization], built on **llmkit's `bridge`** (the `vendor/llmkit`
submodule) rather than the MCP-sampling path §7/§12 originally assumed.

## 1. Goal & the central tension

Durable knowledge — decisions, gotchas, API contracts, hard-won detail — surfaces
during work and then evaporates. The goal is to **ensure it gets captured** into
crib memory without the user hand-authoring every note, and to **revise** existing
notes (compress, dedupe, normalize) on demand.

The tension is the whole design:

- *Ensure capture* → capture broadly, low-friction, or knowledge is lost.
- *Don't pollute `lookup`* (§12) → capture almost nothing, or the curated crib
  sheet drifts toward a chat log and retrieval surfaces noise.

These are reconciled by separating **capture** from **trust**:

> **Capture broadly, into a quarantine tier.** Auto-captured notes land as
> `source: conversation`, down-weighted (or filtered) in `lookup` until a human
> *promotes* them. Capture is frictionless so nothing evaporates; the curated
> index stays clean because unreviewed capture can't outrank it.

Everything below hangs off that.

## 2. Two generation engines

"Summarize / revise text" needs an LLM. Crib has two ways to reach one, picked by
*where the work runs*, not by preference:

| Engine | When available | Used for | Keys |
|---|---|---|---|
| **In-session LLM** — the Claude connected to crib's MCP server | interactive work, **slash commands** | explicit, trusted capture/distill | none (it's the user's session) |
| **Bridge** (`llmkit.bridge`) — crib calls an endpoint itself | **everywhere**, esp. hooks / CLI / daemon (no MCP client) | automatic capture, headless distill | none with the `claude_code` adapter; else configured |

The in-session engine is "MCP sampling done by hand" — the connected model
summarizes and calls a crib MCP tool to store the result. The bridge is the only
option where there is no connected model — a `SessionEnd` hook, a cron job, a plain
CLI invocation. We do **not** keep a separate MCP-sampling code path; one bridge
path is easier to reason about, and the in-session path is just normal tool calls.

### The bridge wrapper — `crib/generate.py`

A thin module over `llmkit.bridge`:

- **Provider** from a new `[generate]` config block. Default `adapter =
  "claude_code"` → generates through the user's existing `claude` login (Agent
  SDK), **no API key in crib**, honoring DESIGN §1's "server holds no keys" ethos.
- **`generate(system, user) -> str`** — `bridge.chat()` streams to a *sink* and
  returns an exit code, so we capture by pointing `content` at a temp file and
  reading it back. (Upstreaming a string-capture sink to llmkit would remove the
  temp file; not required for v1.)
- **Async**: `bridge.chat` is synchronous; in crib's async app it runs via
  `asyncio.to_thread` so it never blocks the event loop.

## 3. Trust tiers (the `source` field)

| `source` | Tier | Origin | `lookup` weight |
|---|---|---|---|
| `manual`, `appended`, `imported` | curated | hand-authored / pulled | full |
| `distilled` | curated | machine-revised in place (§7) | full |
| `conversation` | **quarantine** | auto-captured from a session (§12) | down-weighted / filtered until promoted |

- **Promotion**: `crib promote <relpath>` flips `conversation` → `distilled`/`manual`
  and removes the penalty. The version ring (DESIGN §8) makes a bad auto-write a
  cheap rollback.
- **Lookup weighting**: `RetrieveConfig` gains a `quarantine_weight` (e.g. 0.0 =
  hidden unless `--include-unreviewed`, or a <1.0 multiplier so quarantine can only
  surface when nothing curated matches).

## 4. `distill` — revise a note (DESIGN §7, on the bridge)

On-demand only, **never on write** (that is exactly where a watcher feedback loop
forms). Surface: MCP tool `distill(project?, relpath?)` + CLI `crib distill
[relpath] [-p proj]`. `relpath` given → one note; omitted → whole project
(explicit + bounded — N LLM calls).

Flow per note:

1. Load note. The LLM only ever sees and returns the **body** — `id`, tags,
   `source_path` are held programmatically, so the model can't rewrite identity.
2. `body' = generate(system=distill_prompt, user=body)` (project `distill_prompt`
   from `.cribproject`, else a built-in: *compress, dedupe, normalize, keep
   facts/decisions, drop deliberation, preserve code/commands verbatim*).
3. **Thrash guard**: write only if `hash(body') != hash(body)`.
4. Set `source: distilled`; write through normal `_write_note` → version ring +
   reindex. No special-casing.

This is the substrate proof: provider config → capture → write-back → index, end to
end, low-risk. Build it first.

## 5. Conversation capture (DESIGN §12) — where the value is

Create *new* knowledge from a session. Two triggers, different engines and tiers:

### 5a. Slash command — explicit, in-session → **curated**

A Claude Code slash command (markdown in `.claude/commands/`) that drives the
*in-session* model:

- `/crib-remember [topic]` → "Summarize what we've established (about *topic*) into
  a durable note — decisions and facts, not transcript — and store it via the crib
  `capture` tool." The connected Claude writes the summary and calls a new MCP tool
  `capture(title, body, tags, project?)` → stored as **curated** (`source: manual`,
  user vouched for it by invoking the command).
- `/crib-distill <relpath>` → invokes the `distill` MCP tool on a note.

No bridge needed — the in-session model does the work and calls tools. This is the
low-friction, high-trust path §12 calls for ("opt-in and explicit").

### 5b. `SessionEnd` hook — automatic, out-of-session → **quarantine**

`SessionEnd` hook → `crib summarize-session` (Claude passes `transcript_path`,
`cwd`, `session_id` on stdin). No connected model, so the **bridge** generates.
The conversation text it works from can come from either of two sources — the
chat-log file (this hook) or a live proxy (§5c); both feed steps 2–5 unchanged:

1. **Resolve project** from `cwd` (`.crib` → project).
2. **Significance gate** (cheap first defense): skip trivially short sessions by a
   turn/length heuristic before spending an LLM call.
3. **Summarize**: `generate(summarize_prompt, transcript)` → extract
   decisions/facts/gotchas; the prompt may return "nothing durable" → skip. Bias to
   residue, drop deliberation.
4. **Merge-aware write** (anti-duplication): `lookup` the nearest existing note(s)
   first; if a close match exists, feed it to the model to **update/merge** rather
   than create a parallel note — crib's own retrieval fighting duplication at write
   time (the feedback loop §6 avoids for `distill`, but §12 *wants* here).
5. **Land in quarantine** (`source: conversation`).

Runs as a one-shot CLI, ideally **detached**, so an LLM call doesn't delay session
teardown.

### Why the split

| | Slash command (5a) | Hook (5b) |
|---|---|---|
| Trigger | explicit (user types it) | automatic (every session end) |
| Engine | in-session LLM via MCP tool | bridge |
| Trust | curated | quarantine |
| Friction | one command | zero |

Explicit intent earns curation; zero-friction automatic capture earns quarantine.
Both ensure capture; neither pollutes.

### 5c. Capture source — chat logs vs proxy

The automatic path (5b) needs the conversation *text*. There are two ways to get
it, and they feed the **same** summarize → merge-aware write → quarantine pipeline
— the engine in §2 is unchanged; this is purely *where the bytes come from*.

| | **Chat logs** (JSONL) | **Proxy** (live interception) |
|---|---|---|
| What | read the harness's transcript file after the fact | an LLM proxy between harness and model endpoint observes traffic |
| Trigger | `SessionEnd` hook hands `transcript_path` | streaming — every request/response |
| Coverage | Claude Code only (one parser per harness) | any client routed through it (Claude Code, zsh-ai, IDEs) — one path |
| Fidelity | the harness's *serialization* — undocumented, version-fragile, may elide thinking / tool results | the canonical on-the-wire record: full system prompt, tool calls + results, thinking |
| Latency | batch, at session end | live — can act mid-session |
| Routing | none — the file is already on disk | model traffic must be pointed at the proxy (`ANTHROPIC_BASE_URL`) |
| Keys | none read | **pass-through**: forwards the user's own auth upstream, holds no key (DESIGN §1) |
| Cost | trivial | a component on every request's critical path — must be transparent, low-latency, **fail-open** |

**Chat logs — the default, ships first.** The JSONL lives under the harness
projects dir — the same place §13 mirrors, so `munge` already resolves it. Zero new
infrastructure, no traffic routing, and it honors "server holds no keys" for free.
Its weaknesses are real: the format is reverse-engineered and breaks with harness
versions, it's Claude-Code-specific, and it only arrives at session boundaries.

**Proxy — may be more effective, later.** Because each Anthropic request carries the
*entire prior message list*, the proxy never has to stitch streaming deltas into a
transcript: the **last (longest) request of a session already *is* the full
conversation** — keep the latest message array per session key and summarize that;
only the final assistant turn needs assembling from the response stream. That yields
a richer, canonical record than any harness serialization, captures *every* client
routed through it uniformly (not just Claude Code), and lets capture fire on a live
boundary marker instead of waiting for `SessionEnd`. The costs are operational:
traffic must be routed to the proxy, the proxy sits on the critical path of every
call (so it must forward transparently and **fail open** — never block, delay, or
mutate the user's actual request), and session correlation is fuzzier without an
explicit end signal (§9).

**They compose.** The daemon (DESIGN §10.2 — the MCP server is already a
long-running process) can host the proxy as a second listener; whichever source is
configured produces the same `(project, transcript)` pair that 5b's steps 2–5
consume. Start on chat logs; add the proxy when its fidelity and cross-harness reach
earn the routing cost.

## 6. Config

```toml
# global config.toml — generation provider for distill + headless summarize
[generate]
adapter = "claude_code"        # default: the user's claude login, no key
# adapter = "openai-compatible"; model = "qwen2.5:7b"
# endpoint = "http://localhost:11434/v1"; api_key_env = "OPENAI_API_KEY"
max_tokens = 2048
temperature = 0.2

# optional: a cheaper/local model just for auto-summarization, so SessionEnd
# capture doesn't spend the user's claude quota every session. Falls back to
# [generate] if absent.
[generate.summarize]
adapter = "openai-compatible"
model = "qwen2.5:7b"
endpoint = "http://localhost:11434/v1"

# optional: capture conversations live via an LLM proxy (§5c) instead of — or
# alongside — the SessionEnd chat-log reader. Pass-through: it forwards the user's
# own auth upstream and holds no key. Must fail open (never block the real call).
[capture.proxy]
enabled = false
listen   = "127.0.0.1:7733"
upstream = "https://api.anthropic.com"
```

Per-project `distill_prompt` / `summarize_prompt` live in `.cribproject`
(`distill_prompt` already exists in `ProjectConfig`).

## 7. New surface (summary)

- **MCP tools**: `distill(project?, relpath?)`, `capture(title, body, tags?,
  project?)`, `promote(relpath, project?)`.
- **CLI**: `crib distill [relpath]`, `crib summarize-session` (hook entry),
  `crib promote <relpath>`, `crib proxy` (optional capture-proxy listener, §5c).
- **Slash commands** (`.claude/commands/`): `crib-remember`, `crib-distill`.
- **Hook**: `SessionEnd` → `crib summarize-session` (wired in `settings.json`).
- **Config**: `GenerateConfig` (`crib/config.py`), `quarantine_weight`
  (`RetrieveConfig`), `CaptureConfig` (the `[capture.proxy]` block).
- **Module**: `crib/generate.py` (bridge wrapper); `crib/proxy.py` (optional —
  the pass-through capture proxy, §5c).

## 8. Build order

1. **`crib/generate.py` + `[generate]` config** — the bridge substrate
   (`claude_code` default, temp-file capture, async wrap).
2. **`distill`** (MCP + CLI) — proves the substrate, ships §7.
3. **Quarantine tier** — `source: conversation`, `quarantine_weight` in `lookup`,
   `capture` + `promote` tools.
4. **`/crib-remember` slash command** — explicit in-session capture (no bridge).
5. **`summarize-session` + `SessionEnd` hook** — automatic capture from **chat
   logs**: transcript parse, significance gate, merge-aware write, detached run.
6. **Capture proxy** (`crib proxy`, §5c) — *optional, additive*: the same pipeline
   fed by a live, harness-agnostic source. Only worth it once the chat-log path is
   solid and its fidelity/coverage limits bite.

Each phase is independently useful; capture value arrives at phase 3–4. Phase 6 is a
second *source* for phase 5's pipeline, not a rewrite of it.

## 9. Open questions

- **Capture source** (§5c): chat logs first; is the proxy worth its routing/critical-path
  cost, or does cross-harness reach not matter for a single-user tool? Lean
  chat-logs-first, proxy when fidelity bites.
- **Transcript shape**: parse Claude Code's JSONL directly, or have the hook hand a
  pre-extracted payload? (JSONL lives under the harness projects dir — same place
  §13 mirrors; `munge` resolves it.) The proxy sidesteps this entirely — the wire
  message list *is* the transcript.
- **Proxy session correlation**: with no `SessionEnd` signal, how is a session keyed
  and its end detected? Candidates: a client-supplied header, a fingerprint of the
  message-array prefix, or an idle timeout on the latest request per key.
- **Significance gate**: pure heuristic (length/turns) vs a cheap LLM yes/no
  pre-check. Start heuristic.
- **Quarantine in `lookup`**: hard filter (`--include-unreviewed` to see) vs soft
  down-weight (surfaces only when nothing curated competes). Lean soft.
- **Merge vs append on near-duplicate**: when 5b finds a close note, update it
  in place (loses the per-session provenance) or append a dated section? Lean
  append-section so provenance survives and the version ring stays meaningful.
