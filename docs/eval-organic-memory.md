# Exploration task: how well does the LLM save notes to cribsheet *unprompted*?

> **Brief for a fresh Claude Code session.** Self-contained — assumes no memory of
> the session that wrote it. Goal is measurement + recommendations, **not** code
> changes to cribsheet. Read the whole brief before starting.

## The question

cribsheet is meant to replace basic-memory as long-term, cross-session, cross-agent
memory. Its value depends on the LLM **organically** persisting durable facts —
deciding to `store`/`append`/`edit` on its own because the MCP instructions told it
to — rather than only when a human says "remember this." Measure that organic
behaviour and recommend how to improve it.

The single metric that matters: **of all cribsheet writes, what fraction were
model-initiated (unforced) vs explicitly user-requested?** Plus: when the LLM
*should* have saved something durable, how often did it?

## Critical confound — isolate cribsheet from the *other* memory system

This environment has **two independent memory mechanisms**. Do not conflate them:

1. **Harness file-based memory** — the `MEMORY.md` + per-fact markdown files under
   `~/.config/claude/projects/<proj>/memory/`, driven by the system-prompt "Memory"
   instructions and written with the plain `Write` tool. This is NOT cribsheet.
2. **cribsheet MCP** — tools named `cribsheet_store` / `cribsheet_append` /
   `cribsheet_edit` / `cribsheet_lookup` / `cribsheet_apropos` (here, surfaced via
   the mcp-combiner as `mcp__plugin_claude-mcp-combiner_mcp-combiner__cribsheet_*`).

Only #2 counts. A `Write` to `…/memory/foo.md` is the harness system, not cribsheet.
Report on both if useful, but the headline metric is cribsheet specifically.

**Also note (DESIGN §13):** cribsheet can now *mirror* harness memory into a crib
project (`crib import-memory` + the daemon's live mirror), so the cribsheet store may
contain notes that originated in the harness system. These carry
`source: claude_memory` frontmatter and live under `notes/claude-memory/`. Exclude
them when counting *organic cribsheet writes* — they're ingested, not model-authored
via the `store`/`append` tools. The provenance tag makes them easy to filter.

Note: the cribsheet MCP `instructions` block was **rewritten on 2026-06-28** (see
`crib/server.py`, `build_server`). Transcripts before that date reflect the OLD
instructions; after, the new ones. Segment your analysis by that boundary, and treat
pre-change data as the baseline the rewrite was meant to improve.

## Evidence sources (in priority order)

1. **Claude Code session transcripts** — `~/.config/claude/projects/*/*.jsonl`.
   The gold source: each is a full session with `tool_use` blocks in context, so you
   can see *both* the cribsheet call and the surrounding turns that triggered it.
   - Parse JSONL; find assistant `tool_use` blocks whose tool name matches
     `cribsheet_(store|append|edit|lookup|apropos|...)`.
   - For each, capture the preceding user message(s) to classify **prompted vs
     organic** (see rubric below).
   - These files are large — stream/iterate, don't slurp. Sample if needed and say so.

2. **cribsheet's own accumulated state** — run the CLI (it attaches to the warm
   daemon): `crib projects`, then per project inspect notes and `crib history`
   (git log of the data tree) and `crib versions <relpath>`. Cross-reference note
   creation/edit timestamps with session times. Distinguish `source: manual`
   (LLM/user-written) from `source: imported` frontmatter.

3. **The MCP instructions** — the `instructions=` string in `crib/server.py`. Judge
   its clarity against observed behaviour: does it actually elicit organic saves?

4. **basic-memory comparison (optional)** — basic-memory was just disabled
   (`~/.config/secrets/mcpservers.json`, now `"disabled": true`). Its accumulated
   notes live under `$XDG_DATA_HOME/basic-memory`. Did the *old* tool get used
   organically more or less than cribsheet? A rough baseline, not a controlled one.

## Prompted-vs-organic rubric

Classify each cribsheet write:

- **Prompted** — the user (or a slash command / hook) explicitly asked to persist:
  "remember…", "save a note", "store this", "/remember", etc., in the turns
  immediately before the call.
- **Organic** — the model decided on its own: no save request in recent context; the
  call follows the LLM establishing a durable fact, decision, or convention.
- **Ambiguous** — borderline; count separately, don't force it.

Also flag **missed opportunities**: turns where a durable fact was clearly
established (a decision, a gotcha, a convention) but no cribsheet write followed.
These are the denominator for "did it save when it should have."

## Metrics to produce

- Total cribsheet writes; split organic / prompted / ambiguous.
- **Organic-save rate** = organic / (organic + prompted).
- Sessions with ≥1 organic cribsheet write / total sessions touching the project.
- **Consult rate** — `lookup`/`apropos` calls before answering a project question
  (the read side of the instruction: "consult before answering from memory").
- **Hygiene** — did it prefer `append`/`edit` on an existing note over near-duplicate
  `store`s (the instruction says to)? Count duplicate-ish stores.
- Qualitative: are organically-saved notes *good* (durable facts) or noise?
- Segment all of the above by the 2026-06-28 instruction-rewrite boundary.

## Deliverables

1. A short metrics report answering the headline question, with the numbers above
   and the pre/post-rewrite split.
2. Concrete findings on instruction effectiveness (what worked, what didn't).
3. Recommendations, ranked, e.g.: instruction wording tweaks; tool-description
   sharpening; whether a *hook* (harness-level, on stop/precompact) should nudge or
   automate saves rather than relying on model volition; whether `store` needs a
   lower-friction affordance.
4. (Optional, valuable) a small reusable transcript-analysis script committed under
   `scripts/` or `tests/`, so this eval can be re-run after instruction changes —
   turning a one-off into a regression check on organic-memory behaviour.

## Constraints

- **Read-only on user data.** Transcripts and notes are the user's; analyse, don't
  mutate. Do not `store` test notes into real projects (use a throwaway `--project`
  if you must exercise the write path).
- The daemon may be warm; `crib --no-daemon …` forces in-process if you need
  isolation. `--json` on read verbs gives machine-parseable output.
- Don't change cribsheet behaviour in this task — measurement only. File follow-up
  dev as recommendations.
- Watch sample bias: heavy dev sessions on cribsheet itself will over-represent
  cribsheet tool use vs a normal project. Note which projects the transcripts cover.
