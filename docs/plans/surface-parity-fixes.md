# Plan: CLI⇄MCP surface parity fixes

Status: audited 2026-08-05; P1, P2, P3 and P5 APPLIED 2026-08-05 (see the per-item
`DONE` lines). P4 partly applied — the MCP param renames are deliberately deferred.
Original audit: Source: full pairing audit of
`crib/cli.py` (VERBS registry :742-839) vs `crib/server.py` tools vs the shared
`Crib` methods in `crib/app.py`. Parity is structurally good — all 40 MCP tools
are CLI-reachable and almost every pair calls the same `Crib` method — but the
audit found real drift. Each item below is independently landable.

## P1 — behavior bugs

1. **`code_index` uses the wrong project-resolution policy.**
   `project_setup/index/status/forget` use `_source_project` (server.py:77-91)
   specifically so `project_path=/other/repo` can never index into the sticky
   session project — the comment there cites the exact past bug. But
   `code_index` resolves via `_project` (server.py:380), where a set sticky
   project *beats* `project_path` (session.py:87-95). So in a session stuck to
   project A, `code_index(path=/other/repo/f.py, project_path=/other/repo)`
   files symbols under A.
   **Fix:** switch `code_index` to `_source_project`. Add a regression test:
   sticky session on A + `code_index(project_path=B_repo)` → symbols land in B.
   **DONE (2026-08-05).** `code_index` declares the `source` policy; regression test
   `tests/test_project_resolution.py::test_code_index_files_symbols_under_the_path_repo_not_the_sticky_project`.

2. **`--keywords ""` / `--summaries ""` disable-semantics are dead code.**
   `_split_labels` (cli.py:27-38) documents that an explicit empty string maps
   to `[]` to disable a default-on index set (a retrieval eval baseline was
   silently wrong without it). But `_b_lookup` gates on truthiness —
   `if getattr(a, "keywords", None):` (cli.py:731-732) — so `""` is falsy,
   gets dropped, and the config default applies anyway. Same for `--summaries`
   (cli.py:735-736).
   **Fix:** gate on `is not None` and pass through `_split_labels`. Test both
   `""` (→ `[]` in the tool args) and unset (→ absent).
   **DONE (2026-08-05).** `_b_lookup` gates on `is not None`; tests in `tests/test_cli_labels.py`
   cover `""` → `[]`, absent → omitted, and pass-through.

3. **`note apropos` k default: CLI 5 vs MCP 8** (cli.py:470 vs server.py:233;
   `note lookup --render` uses lookup's k=8, so the same rendered view returns
   5 or 8 hits depending on spelling).
   **Fix:** pick 8 (matches lookup and the Crib default) and set the CLI
   default to it. One-line change + doc line in `docs/surface.md`.
   **DONE (2026-08-05).** CLI default is 8, `docs/surface.md` says so, and
   `test_cli_defaults_match_the_declared_mcp_defaults` now guards the whole class.

## P2 — policy inconsistencies (decide once, then enforce)

4. **Write-project policy applied inconsistently in MCP.** The rule
   (server.py:94-109): note writes must NAME the target (`project` or
   `project_path`) — enforced for `note_store/append/edit/forget/move` via
   `write_tool`. But:
   - `note_import` / `note_import_memory` (server.py:579, 590) are writes
     registered as plain tools; with no args they silently fall through to the
     config default project — exactly what `_write_project` forbids.
   - `learning_add/edit/forget` are writes using the sticky read policy
     `_project` (server.py:516, 524, 531).
   **Decision to record (design note once the design facet exists):** imports
   are *source-driven* (cwd/repo) and learnings are *about the current code
   project*, so sticky/inferred resolution is arguably correct for both — but
   it is an undocumented exception.
   **Fix:** keep behavior for learnings; for the two imports require
   `project_path` (they're meaningless without a source repo anyway); add one
   comment block at `_write_project` naming the exception classes, and a
   docstring line on each excepted tool.
   **DONE (2026-08-05).** Learnings declare `read` (documented at each tool); the imports declare
   `source` + `needs_target`, so they error — and say so in the wire schema's
   `anyOf` — instead of falling through
   (`test_imports_refuse_to_fall_through_to_the_default_project`).

5. **`project_use` / `project_current` logic duplicated in server.py.**
   server.py:632-642 re-implements `Crib.use_project` (app.py:1827-1834);
   server.py:645-650 re-implements `Crib.current_project` (app.py:1836-1845).
   Currently line-for-line identical — pure drift risk.
   **Fix:** delete the inline copies; call the Crib methods (the CLI path
   already does).
   **DONE (2026-08-05).** Both tools call `Crib.use_project` / `Crib.current_project`; the
   project-name validation moved into `Crib.use_project` with the copy.

## P3 — stale text after the noun-verb rename (drift that already bit)

All of these reference commands/tools that no longer exist. Sweep and fix in
one commit; then grep-guard (see below).

| site | says | should say |
|---|---|---|
| cli.py:323-324 | `crib code-rehome` / `crib code-forget` | `crib learning rehome` / `crib learning forget` |
| cli.py:335 | `crib code-forget` | `crib learning forget` |
| cli.py:338 | `crib code-rehome {old} <fqname>` | `crib learning rehome {old} <fqname>` |
| cli.py:206 | `crib setup --remote <url>` | `crib memory setup --remote <url>` |
| cli.py:936 | `crib note setup:` (error prefix) | `crib memory setup:` |
| server.py:253 | "call `reindex(relpath)`" | `note_reindex` |
| server.py:523 | "code_append creates" | `learning_add` |
| server.py:555 | "code_rehome / code_forget" | `learning_rehome` / `learning_forget` |

**Grep-guard:** add a tiny test that greps `crib/*.py` docstrings/strings for
the retired names (`code-rehome`, `code-forget`, `code_append` outside app.py,
`crib setup`, `crib note setup`) so the next rename can't silently drift.

**DONE (2026-08-05).** All eight table rows swept, plus two the audit missed (`gitbacking.py`'s
`crib setup` / `crib sync` messages). The guard is
`tests/test_surface_parity.py::test_no_retired_command_names_in_user_facing_strings`
(retired command forms, any string literal) and `::test_no_retired_tool_names_in_strings`
(retired tool/method names — strict, now that P4.7 unified the vocabulary).

## P4 — naming/param drift (fix opportunistically; keep back-compat where MCP-visible)

6. **DEFERRED — same-role parameter names differ:** `learning_add(text)` vs
   `learning_edit(new_content)`; `note_store(content)` vs
   `note_edit(new_content)`; CLI `--orphans` vs MCP `orphans_only`; CLI
   `old/new` vs MCP `old_fqn/new_fqn`. Convention to adopt: creation takes
   `content`, replacement takes `new_content`, learning's dated-append takes
   `text` — OR unify on `content` everywhere. Renaming MCP params is a
   client-visible break; if unifying, accept both for one release (keyword
   alias) and note it in the tool docstring.
   **Deferred (2026-08-05)** — client-visible, so not bundled with the internal
   sweep. `tests/test_surface_parity.py` pins the current names, so the rename
   becomes a deliberate registry edit rather than silent drift.
7. **Three vocabularies for the learning facet:** surface `learning_*`, Crib
   methods `code_append/code_edit/…/code_learnings` (app.py:1270-1304), CLI
   emitters keyed `"code-append"` etc. (cli.py:287-341). Rename the Crib
   methods to `learning_*` (internal, safe) and the emitter keys to match;
   `learning_report`'s backing `code_learnings` is the worst offender.
   **DONE (2026-08-05).** `Crib.code_append/edit/forget/reaffirm/read/rehome/learnings` →
   `learning_add/edit/forget/reaffirm/read/rehome/report`, CLI emitter keys
   likewise, and the two `code_append first` error strings in `learnings.py`.
8. **DEFERRED — "memory" means two things:** `memory_*` = store git lifecycle;
   `note_import_memory` = Claude harness memory. Cheap fix: docstring
   cross-references disambiguating both. Costlier (optional): rename
   `note_import_memory` → `note_mirror_claude` or fold under a `mirror` noun —
   defer unless something else forces a break.

## Non-issues (audited, deliberate — do not "fix")

- `memory setup/sync/push/pull` CLI-only: git auth must run in the user's
  terminal (cli.py:921-924). `snapshot`/`history` (local-only) correctly stay
  MCP-exposed.
- `crib serve`, `crib info`, `crib merge-driver` CLI-only: transport/plumbing.
- MCP-only `project_index(budget_s)` + progress reporting: agent-facing
  affordances; CLI runs foreground. (Optional nicety: `--budget-s` flag.)
- MCP result markers (`project_source`, `resolved` echoes) absent on the
  in-process CLI path: the daemon path carries them and the CLI renders them
  (cli.py:146-148); in-process is the escape hatch.

## P5 — structural enforcement (approved 2026-08-05; do after P1-P3)

Rationale: the `code_index` bug (P1.1) happened because the resolution-policy
choice is one invisible helper call inside each tool body. Make it declarative
and mechanically checked so the class of bug can't recur silently.

9. **Declarative resolution policy.** The server has three legitimate
   resolution policies: `read` (`_project`: explicit → sticky session → cwd
   seed), `write` (`_write_project`: must name the target), `source`
   (`_source_project`: explicit → project_path; repo-scoped, sticky must
   never win). Refactor tool registration so each tool *declares* its policy
   at the decorator (extend the existing `write_tool` pattern into e.g.
   `crib_tool(resolution="read"|"write"|"source")`), the decorator wires the
   resolver, and no tool body calls a resolver directly. Documented
   exceptions get their declaration + a docstring line: `learning_add/edit/
   forget` = `read` by intent (learnings are about the current code project);
   `note_import`/`note_import_memory` = `source` (they're about a repo — this
   also fixes their current silent fall-through to the default project, P2.4).
   A table test asserts every registered tool carries an explicit policy.
   **DONE (2026-08-05).** `@crib_tool(resolution=…)` registers every tool and wires the resolver;
   bodies receive an already-resolved `project`. Two policies joined the audit's
   three: `session` (project_use/project_current — they ARE the session pointer)
   and `none` (no project args). `server.TOOL_POLICY` records all 40;
   `test_every_mcp_tool_declares_a_resolution_policy` and
   `test_no_tool_body_picks_a_resolver_itself` enforce it.
10. **Registry-driven parity test.** Extend `cli.py`'s `VERBS` registry
    entries with the MCP-visible parameter names/defaults and the resolution
    policy, making it the single source of truth for the surface. One test
    walks the registry against FastMCP's introspected tool schemas: tool
    exists, same param names, same defaults, policy declaration matches.
    This mechanically catches the whole audit class (apropos k=5/8, param
    drift, one-surface-only verbs) forever. Update `docs/surface.md` from the
    registry (or note it as generated).
    **DONE (2026-08-05).** `Verb` grew `mcp=` (the MCP signature, e.g. `"query project=None k=8 …"`)
    and `policy=`; the four `project setup/index/status/forget` verbs moved from
    the synthetic `_project_verb` into VERBS, so all 40 tools are registry rows.
    `tests/test_surface_parity.py` walks it against FastMCP's introspected schemas
    (tool exists, param names, defaults, policy) plus a CLI-defaults check that
    catches the P1.3 class. `docs/surface.md` documents the policies and the
    parity check; it is still hand-written.

## Ordering

P1.1 → P1.2 → P1.3 (each independent, test-first), then P3 (mechanical sweep +
grep-guard), then P2.5 (deletion), P2.4 (docs + import guard), P4 as time
allows. When the design facet (docs/plans/design-plan-tracking.md) lands,
record the pairing convention and the P2.4 exceptions as design notes with
deps — this audit is the manual run of what `design_check` should automate.
