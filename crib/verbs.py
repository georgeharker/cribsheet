"""Verb templates: each facet's MCP tools, generated from its spec.

One template per verb of an aspect (crib.facets.ASPECTS): its name, its
project-resolution policy, whether a read-only tool set keeps it, a body with
a FIXED signature that works on the facet it is generated for, and a
description written in terms of the facet's noun. `register` turns them into
`<noun>_<verb>` tools for every facet in the registry whose verbs are generated
(facet registry step 2, design 01M7EFXBHJH7FTEM1HK4JYS6JF).

The home facet (notes) reads exactly as its hand-written tools did; the few
sentences that are about notes in particular (what memory is for, where the
other pillars are searched, in-situ docs) have a general wording for every
other facet. tests/test_tool_surface_snapshot.py holds the notes half to the
byte; tests/test_declared_facets.py runs the other half on a declared facet.

Signatures use postponed annotations, resolved by FastMCP against this
module's globals: hence the module-level Context binding below, as in
crib.server.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import facets
from .facets import FacetSpec

if TYPE_CHECKING:
    from fastmcp import Context

    from .app import Crib
else:
    try:
        from fastmcp import Context
    except Exception:  # pragma: no cover — package stays importable without fastmcp
        Context = Any


@dataclass(frozen=True)
class Verb:
    """One verb of an aspect, as a template over facets."""

    verb: str  # the canonical name; a built-in may alias it (FacetSpec.verb_names)
    aspect: str
    policy: str  # crib.server's resolution policy for the tool
    read_only: bool  # kept by a read-only tool set (`--tools notes:ro`)
    describe: Callable[[FacetSpec], str]
    # (crib, spec) -> body; must not touch crib until the body is called (its
    # signature is read with crib=None: FacetView.mcp)
    make: Callable[[Any, FacetSpec], Callable[..., Any]]
    elicit: bool = False
    needs_target: bool = False
    # A declared facet's own fields on this verb: "set" (typed parameters that
    # write them) or "filter" (typed equality filters, plus `where`); "" = none.
    fields: str = ""
    echo: bool = False  # a read that surfaces an implicit resolution


def tool_name(spec: FacetSpec, verb: str) -> str:
    return f"{spec.noun}_{spec.verb_names.get(verb, verb)}"


def _home(f: FacetSpec) -> bool:
    return f.noun == facets.HOME


# ── descriptions ──────────────────────────────────────────────────────────────


def _d_lookup(f: FacetSpec) -> str:
    n = f.noun
    if _home(f):
        head = (
            "Semantic search over memory. Call this FIRST when the user asks\n"
            "about this project — a prior decision, convention, or investigation\n"
            "may already be stored. Returns ranked note sections, each with its\n"
        )
        scope = (
            "Notes only: design decisions, plan\n"
            "items and learnings live in their own pillar stores and never appear\n"
            "here — search them with `design_lookup` / `plan_lookup`, whose hits\n"
            "also carry the facet state.\n"
        )
    else:
        head = f"Semantic search over {f.plural}. Returns ranked {n} sections, each with its\n"
        scope = (
            f"Only {f.plural}: every facet searches its own store, so\n"
            "notes, decisions, plans and other facets never appear here.\n"
        )
    return (
        head + "relpath and the line_start/line_end span of the matching section so\n"
        f"you can jump straight to it (pair with `{tool_name(f, 'locate')}` for the abspath).\n"
        "`tags` filters by frontmatter tag. "
        + scope
        + "`hints`: exact strings you already have in context — symbol names,\n"
        f"flags, error codes, {n} titles you just read. Each runs as its own\n"
        "exact-term route and boosts candidates multiple routes agree on.\n"
        "No hints? Just query. `keyword_labels`/`keyword_weight` (BM25\n"
        "keyword_index) and `summary_labels` (dense summary_index aliases)\n"
        "override which LLM index sets feed retrieval (default from config);\n"
        "`rerank` overrides [retrieve].rerank for this query — mainly for eval\n"
        "sweeps.\n"
        "A hit carrying `index_rebuilding: true` means this project is still being\n"
        "re-embedded after a store wipe (`status` shows the sweep) — the result set\n"
        "is INCOMPLETE, so retry once it clears rather than concluding nothing exists."
    )


def _d_apropos(f: FacetSpec) -> str:
    return (
        f"Like `{tool_name(f, 'lookup')}`, but each hit carries the full matching section's\n"
        "markdown (`section`) instead of a short snippet — for reading the\n"
        "matched sections in full, not just locating them. Carries the same\n"
        "`index_rebuilding` incompleteness flag. `hints`: exact strings you\n"
        "already have in context, each run as its own exact-term route."
    )


def _d_read(f: FacetSpec) -> str:
    n = f.noun
    return (
        f"Read a {n}'s full raw markdown (frontmatter + body) — e.g. to see a\n"
        f"`{tool_name(f, 'lookup')}` hit in full context, or before rewriting the {n} "
        f"with `{tool_name(f, 'edit')}`."
    )


def _d_locate(f: FacetSpec) -> str:
    return (
        f"Get the real on-disk path of a {f.noun} so you can edit it with your own\n"
        f"file tools. After editing, call `{tool_name(f, 'reindex')}(relpath)` to make it "
        "searchable\n"
        "now (the watcher would catch it shortly regardless)."
    )


def _d_add(f: FacetSpec) -> str:
    n = f.noun
    if _home(f):
        head = (
            "Persist a durable fact to memory — a decision, preference,\n"
            "convention, gotcha, or hard-won detail worth recalling in a future\n"
            "session. Assigns an id, writes markdown, indexes it. If a related\n"
        )
    else:
        head = f"Add a {n}. Assigns an id, writes markdown, indexes it. If a related\n"
    return (
        head + f"{n} already exists (check with `{tool_name(f, 'lookup')}`), prefer "
        f"`{tool_name(f, 'append')}`/`{tool_name(f, 'edit')}`\n"
        "over creating a near-duplicate.\n"
        "\n"
        "PICK THE RIGHT PROJECT — REQUIRED. A fact belongs to the project it is ABOUT,\n"
        "which may NOT be the repo you're working in, so a write won't inherit your\n"
        "current project: pass `project=` (the subject's project — often `default` or a\n"
        "tool's own project for cross-cutting knowledge like a CLI/editor/convention),\n"
        "or `project_path=` a path in that repo. If you omit both, you'll be ASKED which\n"
        "project (elicitation). Then tell the user which project it landed in."
    )


def _d_append(f: FacetSpec) -> str:
    return (
        f"Add to an existing {f.noun} (found via `{tool_name(f, 'lookup')}`) — the right "
        "call when new\n"
        "information extends or continues something already remembered, rather than\n"
        f"`{tool_name(f, 'add')}`-ing a near-duplicate. Optionally files it under a new "
        "heading."
    )


def _d_edit(f: FacetSpec) -> str:
    n = f.noun
    return (
        f"Rewrite a {n}'s full content — use when remembered information has\n"
        f"changed, needs correcting, or several {f.plural} should be consolidated (read\n"
        f"it first). Frontmatter (and the {n}'s id/history) is preserved."
    )


def _d_forget(f: FacetSpec) -> str:
    return (
        f"Delete a {f.noun} when its information is obsolete or wrong. Removed from\n"
        "disk and the index, but stashed to the version ring first, so it stays\n"
        "recoverable by id."
    )


def _d_reindex(f: FacetSpec) -> str:
    whole = "the whole project" if _home(f) else f"all of the project's {f.plural}"
    return (
        f"Reindex a {f.noun} (or {whole}). Call after editing a {f.noun} via\n"
        "its raw path. Safe to call redundantly — it no-ops if already current."
    )


def _d_versions(f: FacetSpec) -> str:
    return f"List recoverable prior versions of a {f.noun}."


def _d_restore(f: FacetSpec) -> str:
    return f"Restore a prior version of a {f.noun} (itself undoable)."


def _d_move(f: FacetSpec) -> str:
    return (
        f"Relocate a {f.noun} to another project and/or rename it, preserving its id\n"
        f"and version history (the curation primitive — not {f.verb_names.get('add', 'add')}"
        "-new + forget-old).\n"
        "`to_project` moves it across namespaces; `to_relpath` renames it."
    )


def _d_import(f: FacetSpec) -> str:
    if _home(f):
        head = (
            "Copy the NAMED files into memory as crib-owned notes (snapshot you own:\n"
            "git-synced, editable, versioned). Distinct from a repo's `.crib` docs, which\n"
            "are indexed IN-SITU (source is master, never copied) by `project index`.\n"
        )
    else:
        head = (
            f"Copy the NAMED files in as crib-owned {f.plural} (snapshot you own:\n"
            "git-synced, editable, versioned).\n"
        )
    return (
        head
        + "`paths` must be absolute, or relative to `project_path` (there is no shell\n"
        "cwd here for them to be relative to). An import is ABOUT a repo, so — unlike\n"
        f"a {f.noun} write — it resolves like the repo-scoped `project_*` tools and REQUIRES\n"
        "a source: name `project_path=<the repo dir>` (or `project=`), else it errors\n"
        "rather than quietly filing the copies in the default project."
    )


def _d_distill(f: FacetSpec) -> str:
    return (
        f"LLM-revise a {f.noun} in place: compress, dedupe, normalize — keeping\n"
        "facts/decisions, dropping deliberation, preserving code verbatim.\n"
        "Thrash-guarded (no-op if unchanged); the prior version is recoverable."
    )


def _d_elaborate(f: FacetSpec) -> str:
    return (
        "keyword_index: generate BM25 search terms per section (or whole\n"
        "project), section-addressed under `label` (e.g. `keywords`, `questions`,\n"
        "`phrase`). Skips cached sections unless `overwrite`. Activate via\n"
        "[retrieve].keyword_labels."
    )


def _d_summarize(f: FacetSpec) -> str:
    return (
        "summary_index: generate LLM rephrasings per section (or whole project),\n"
        "embedded as dense alias vectors so paraphrased queries match a section\n"
        "with zero shared tokens. Skips cached sections unless `overwrite`.\n"
        "Activate via [retrieve].summary_labels."
    )


# ── bodies ────────────────────────────────────────────────────────────────────
# Each returns the tool body for one facet. The signature is the wire schema:
# keep it identical across facets.


def _m_lookup(crib: Crib, f: FacetSpec):
    async def body(
        query: str,
        project: str | None = None,
        k: int = 8,
        tags: list[str] | None = None,
        keyword_labels: list[str] | None = None,
        keyword_weight: float | None = None,
        summary_labels: list[str] | None = None,
        summary_weight: float | None = None,
        hints: list[str] | None = None,
        hint_weight: float | None = None,
        rerank: bool | None = None,
        project_path: str | None = None,
        **fields: Any,
    ) -> list[dict[str, Any]]:
        where = fields.pop("where", None)
        return [
            vars(h)
            for h in crib.lookup(
                query,
                project,
                k,
                tags,
                keyword_labels=keyword_labels,
                keyword_weight=keyword_weight,
                summary_labels=summary_labels,
                summary_weight=summary_weight,
                hints=hints,
                hint_weight=hint_weight,
                rerank=rerank,
                store=f.store,
                fields=fields or None,
                where=where,
            )
        ]

    return body


def _m_apropos(crib: Crib, f: FacetSpec):
    async def body(
        query: str,
        project: str | None = None,
        k: int = 8,
        tags: list[str] | None = None,
        hints: list[str] | None = None,
        project_path: str | None = None,
        **fields: Any,
    ) -> list[dict[str, Any]]:
        where = fields.pop("where", None)
        return crib.apropos(
            query, project, k, tags, hints=hints, facet=f.noun,
            fields=fields or None, where=where,
        )

    return body


def _m_read(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str, project: str | None = None, project_path: str | None = None
    ) -> str:
        return crib.read_note(relpath, project, facet=f.noun)

    return body


def _m_locate(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str, project: str | None = None, project_path: str | None = None
    ) -> str:
        return crib.locate(relpath, project, facet=f.noun)

    return body


def _m_add(crib: Crib, f: FacetSpec):
    async def body(
        content: str,
        title: str | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
        project_path: str | None = None,
        ctx: Context | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        return await crib.store_note(
            content, title, project, tags, facet=f.noun, fields=fields or None
        )

    return body


def _m_append(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str,
        content: str,
        heading: str | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.append_note(relpath, content, heading, project, facet=f.noun)

    return body


def _m_edit(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str,
        new_content: str,
        project: str | None = None,
        project_path: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        return await crib.edit_note(
            relpath, new_content, project, facet=f.noun, fields=fields or None
        )

    return body


def _m_forget(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return await crib.forget(relpath, project, facet=f.noun)

    return body


def _m_reindex(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.reindex(relpath, project, facet=f.noun)

    return body


def _m_versions(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str, project: str | None = None, project_path: str | None = None
    ) -> list[dict[str, Any]]:
        return crib.list_versions(relpath, project, facet=f.noun)

    return body


def _m_restore(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str,
        version: str,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.restore(relpath, version, project, facet=f.noun)

    return body


def _m_move(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str,
        to_project: str | None = None,
        to_relpath: str | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.move_note(
            relpath, to_project, to_relpath, project, facet=f.noun
        )

    return body


def _m_import(crib: Crib, f: FacetSpec):
    async def body(
        paths: list[str], project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        from .server import _cwd, _switch_if_created

        return _switch_if_created(
            await crib.import_files(
                paths, project, cwd=_cwd(project_path), facet=f.noun
            )
        )

    return body


def _m_distill(crib: Crib, f: FacetSpec):
    async def body(
        relpath: str, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return await crib.distill(relpath, project, facet=f.noun)

    return body


def _m_elaborate(crib: Crib, f: FacetSpec):
    async def body(
        label: str,
        relpath: str | None = None,
        project: str | None = None,
        overwrite: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.elaborate(
            label, relpath, project, overwrite=overwrite, facet=f.noun
        )

    return body


def _m_summarize(crib: Crib, f: FacetSpec):
    async def body(
        label: str,
        relpath: str | None = None,
        project: str | None = None,
        overwrite: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.summarize(
            label, relpath, project, overwrite=overwrite, facet=f.noun
        )

    return body


# ── the anchored aspect: items named by the code symbol they are about ───────
# Learnings' verbs, generalised: an anchored facet's items are addressed by
# symbol (`symbol`, narrowed by `path`/`scope`/`lang`), not by relpath, and are
# found through the code verbs (code_lookup / code_dossier attach them). Every
# one resolves its project like a read: an item is ABOUT a symbol in the code
# project you are in (the documented exception to the write policy).


def _d_a_add(f: FacetSpec) -> str:
    if f.builtin:
        return (
            "Pin a durable human learning to a code symbol — the 'now I get it',\n"
            "the subtlety, the gotcha you don't want to re-derive next session. Stored\n"
            "as a first-class note in the learnings pillar store keyed to the symbol's\n"
            "fqn, SEPARATE from the regenerable LLM description, so it survives\n"
            "re-indexing and rides git sync (and works on code you can't edit — vendored\n"
            "deps, read-only explorations — where a comment can't go). Appends a dated\n"
            "entry to the symbol's running note. `symbol` is a bare name or dotted\n"
            "fqname already in the symbol_index (code_index the file first). Surfaces\n"
            "back via code_lookup/code_xref. A learning is ABOUT a symbol in the code\n"
            "project you're in, so — unlike a note write — it resolves that project the\n"
            "way the read tools do (a documented exception; pass `project=`/`project_path=`\n"
            "to pin one elsewhere)."
        )
    n = f.noun
    return (
        f"Attach a {n} to a code symbol. Stored as a note in the {f.plural} store\n"
        "keyed to the symbol's fqn, so it survives re-indexing and rides git sync.\n"
        "Appends a dated entry to the symbol's running note. `symbol` is a bare name\n"
        "or dotted fqname already in the symbol_index (code_index the file first).\n"
        f"Surfaces back via code_lookup/code_xref/code_dossier under `{n}`. A {n} is\n"
        "ABOUT a symbol in the code project you're in, so it resolves that project\n"
        "the way the read tools do (pass `project=`/`project_path=` to pin one\n"
        "elsewhere)."
    )


def _d_a_edit(f: FacetSpec) -> str:
    n = f.noun
    return (
        f"Rewrite a symbol's {n} body wholesale (frontmatter preserved) —\n"
        f"the standard edit, scoped to a symbol. Errors if none exists; `{n}_add`\n"
        f"creates. Resolves its project like a read, as `{n}_add` does."
    )


def _d_a_forget(f: FacetSpec) -> str:
    n = f.noun
    return (
        f"Remove a symbol's {n} (stashed to the version ring first, so it's\n"
        "recoverable) — the standard forget, scoped to a symbol. Resolves its project\n"
        f"like a read, as `{n}_add` does."
    )


def _d_a_read(f: FacetSpec) -> str:
    return (
        f"Read a symbol's attached {f.noun} note (frontmatter + body), or found=\n"
        "False if none is written yet. `symbol` is a bare name or dotted fqname."
    )


def _d_a_reaffirm(f: FacetSpec) -> str:
    return (
        f"Clear a {f.noun}'s ⚠︎ stale flag WITHOUT rewriting it — you re-checked the\n"
        "note against the current code and it still holds. Re-snapshots the symbol's\n"
        "content_hash so it reads as fresh again. Use when code_lookup shows a ※ note\n"
        "flagged stale but the understanding is still correct."
    )


def _d_a_migrate(f: FacetSpec) -> str:
    n = f.noun
    return (
        f"Rebind this project's {n} notes to the current symbol identity.\n"
        "\n"
        f"OPTIONAL TIDINESS — the {f.plural} join resolves a note bound to any prior\n"
        "spelling, so an unconverted note keeps working forever; this buys canonical\n"
        "filenames and `symbol_ref` frontmatter. Per record: `noop` (already\n"
        "current), `convert` (rebound, file renamed, old binding kept in\n"
        f"`symbol_was`), `orphan` (binding answers to nothing — `{n}_rehome`\n"
        "repairs it), `collision` (two notes claim one symbol: both kept, said out\n"
        "loud, never merged). Re-running is the resume.\n"
        "\n"
        "DRY RUN unless `apply=True` — it renames files under a git-synced store."
    )


def _d_a_report(f: FacetSpec) -> str:
    n = f.noun
    return (
        f"Health report for attached {f.plural}: each is `ok` | `moved` (fqn resolves\n"
        "but the symbol's file drifted) | `orphan` (fqn no longer resolves — a rename/\n"
        "move/delete left the note dangling). `orphans_only` filters to the actionable\n"
        f"ones. Report-only; drives cleanup via `{n}_rehome` / `{n}_forget`."
    )


def _d_a_rehome(f: FacetSpec) -> str:
    return (
        f"Re-point an orphaned {f.noun} at the symbol it became. Call with just\n"
        "`old_fqn` FIRST to get ranked candidate targets (name/signature/file signals);\n"
        "then call again with the chosen `new_fqn` to move the note (id/history\n"
        "preserved, frontmatter re-snapshotted). Never auto-moves — you pick, because a\n"
        "wrong attach is worse than a dangling one."
    )


def _m_a_add(crib: Crib, f: FacetSpec):
    async def body(
        symbol: str,
        text: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        project_path: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        return await crib.learning_add(
            symbol, text, project=project, path=path, scope=scope, lang=lang,
            facet=f.noun, fields=fields or None,
        )

    return body


def _m_a_edit(crib: Crib, f: FacetSpec):
    async def body(
        symbol: str,
        new_content: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        project_path: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        return await crib.learning_edit(
            symbol, new_content, project=project, path=path, scope=scope, lang=lang,
            facet=f.noun, fields=fields or None,
        )

    return body


def _m_a_forget(crib: Crib, f: FacetSpec):
    async def body(
        symbol: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.learning_forget(
            symbol, path=path, scope=scope, lang=lang, project=project, facet=f.noun
        )

    return body


def _m_a_read(crib: Crib, f: FacetSpec):
    async def body(
        symbol: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return crib.learning_read(
            symbol, path=path, scope=scope, lang=lang, project=project, facet=f.noun
        )

    return body


def _m_a_reaffirm(crib: Crib, f: FacetSpec):
    async def body(
        symbol: str,
        path: str = "",
        scope: str = "",
        lang: str = "",
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.learning_reaffirm(
            symbol, path=path, scope=scope, lang=lang, project=project, facet=f.noun
        )

    return body


def _m_a_migrate(crib: Crib, f: FacetSpec):
    async def body(
        project: str | None = None, project_path: str | None = None, apply: bool = False
    ) -> dict[str, Any]:
        from .server import _cwd

        return await crib.learning_migrate(
            project, cwd=_cwd(project_path), apply=apply, facet=f.noun
        )

    return body


def _m_a_report(crib: Crib, f: FacetSpec):
    async def body(
        project: str | None = None,
        orphans_only: bool = False,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        return crib.learning_report(project, orphans_only=orphans_only, facet=f.noun)

    return body


def _m_a_rehome(crib: Crib, f: FacetSpec):
    async def body(
        old_fqn: str,
        new_fqn: str | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await crib.learning_rehome(old_fqn, new_fqn, project, facet=f.noun)

    return body


# ── a declared facet's own fields ─────────────────────────────────────────────


def _d_list(f: FacetSpec) -> str:
    by = (" by their fields, " + ", ".join(f"`{n}`" for n in f.params) + ","
          if f.params else "")
    return (
        f"List {f.plural}{by} with no search query (in relpath order; `tags` "
        "narrows it to items with any of them). Returns each item's relpath"
        + (" and symbol" if f.has("anchored") else "")
        + ", title, tags" + (" and fields" if f.params else "")
        + ", plus `total` (`k` caps the list)."
    )


def _m_list(crib: Crib, f: FacetSpec):
    async def body(
        project: str | None = None,
        k: int = 50,
        tags: list[str] | None = None,
        project_path: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        where = fields.pop("where", None)
        return crib.list_items(
            project, facet=f.noun, fields=fields or None, where=where, k=k, tags=tags
        )

    return body


def _fields_text(f: FacetSpec, mode: str) -> str:
    """The paragraph a field-bearing verb's description gains."""
    names = ", ".join(f"`{n}` ({t})" for n, t in f.params.items())
    if mode == "set":
        return (
            f"\n\nFields: {names}. Pass any of them to set it"
            " (a date is YYYY-MM-DD); on an edit, the ones you don't pass are left"
            " as they are."
        )
    return (
        f"\n\nFilter by field: {names}. A plain `field=value` is equality;"
        " `where=[{field, op, value}]` takes `== != < <= > >=` for int and date,"
        " `== != re` (a regular expression) for str. An item without the field"
        " matches no condition."
    )


def _with_fields(body: Any, spec: FacetSpec, mode: str) -> None:
    """Give a body its facet's typed field parameters (crib.fields): the wire
    schema FastMCP builds from `__signature__`, the values arriving in the
    body's `**fields`. Without fields, the catch-all is simply dropped."""
    import inspect

    P = inspect.Parameter
    sig = inspect.signature(body)
    params = [p for p in sig.parameters.values() if p.kind is not P.VAR_KEYWORD]
    notes: dict[str, Any] = {}
    if mode and spec.params:
        for name, t in spec.params.items():
            notes[name] = (int | None) if t == "int" else (str | None)
            params.append(P(name, P.KEYWORD_ONLY, default=None, annotation=notes[name]))
        if mode == "filter":
            notes["where"] = list[dict[str, Any]] | None
            params.append(P("where", P.KEYWORD_ONLY, default=None, annotation=notes["where"]))
    body.__signature__ = sig.replace(parameters=params)
    ann = {k: v for k, v in body.__annotations__.items() if k != "fields"}
    ann.update(notes)
    if hasattr(body, "__annotate__"):
        # Python 3.14+: functools.wraps (crib_tool's wrapping) copies
        # __annotate__, and assigning __annotations__ would set it to None, so
        # the wrapper would lose every annotation. Give it an annotate function.
        def annotate(fmt: int, _ann: dict[str, Any] = ann) -> dict[str, Any]:
            if fmt == 1:  # annotationlib.Format.VALUE
                return dict(_ann)
            raise NotImplementedError

        body.__annotate__ = annotate
    else:
        body.__annotations__ = ann


# ── graph facets (links + status): decisions, plans ──────────────────────────
# One template per verb; a facet's aspects (and its statuses' roles) decide
# which verbs it gets and, for add / list, which parameters. The built-ins'
# descriptions are data (crib/wording.toml), held to the byte by the surface
# snapshot. Bodies call the built-in's own Crib method for now; a declared
# graph facet gets generic bodies in facet registry step 7, and no verbs before.


def _graph(f: FacetSpec) -> bool:
    return f.has("links") and f.has("status")


def _gd(verb: str, generic: Callable[[FacetSpec], str]):
    def describe(f: FacetSpec) -> str:
        from . import wording

        own = wording.mcp(f.noun, verb) if f.builtin else None
        return own if own is not None else generic(f)

    return describe


# THE engine operation behind each graph verb (and the transitions' park /
# decline / promote): the Crib graph_* method it calls, bound to the facet.
# One operation per verb for every graph facet; where a verb depends on a
# trait (add: ordered, read/list: progress, append: requires_body), the
# operation branches on it in the engine (Designs.graph_*). crib's own
# facets' design_*/plan_* names are aliases of these.
GRAPH_OPS: dict[str, str] = {
    "add": "graph_add",
    "read": "graph_read",
    "list": "graph_list",
    "append": "graph_append",
    "edit": "graph_edit",
    "lookup": "graph_lookup",
    "export": "graph_export",
    "import": "graph_import",  # the `extract` verb's procedure
    "dep_add": "graph_dep_add",
    "dep_remove": "graph_dep_remove",
    "forget": "graph_forget",
    "reaffirm": "graph_reaffirm",
    "graph": "graph_graph",
    "check": "graph_check",
    "tree": "graph_tree",
    "supersede": "graph_supersede",
    "promote": "graph_promote",
    "status": "graph_status",
    "next": "graph_next",
    "review": "graph_review",
    "review_apply": "graph_review_apply",
    "move": "graph_move",
    "park": "graph_park",
    "decline": "graph_decline",
}


def graph_op(f: FacetSpec, verb: str) -> str:
    """The Crib method serving `verb` on graph facet `f` (GRAPH_OPS): one
    graph_* operation per verb, for every graph facet (the engine branches on
    the facet's traits, not its callers)."""
    return GRAPH_OPS[verb]


def inproc_method(f: FacetSpec, verb: str) -> tuple[str, tuple[tuple[str, Any], ...]]:
    """The Crib method a graph verb runs on IN-PROCESS (the CLI's `--no-daemon`
    path), and the keyword arguments it adds: the same engine operation the
    MCP tool's body calls (GRAPH_OPS), never a `<noun>_<verb>` name Crib
    doesn't have. crib's own facets keep their named methods (design_read ...)."""
    if f.builtin:
        return f"{f.noun}_{f.verb_names.get(verb, verb)}", ()
    own = (("facet", f.noun),)
    if verb in f.transitions:
        to = f.transitions[verb]
        role = f.role(to)
        if role == "paused":
            return GRAPH_OPS["park"], (*own, ("to", to))  # type: ignore[return-value]
        if role == "dropped":
            return GRAPH_OPS["decline"], (*own, ("to", to))  # type: ignore[return-value]
        return "graph_transition", (*own, ("verb", verb))
    return graph_op(f, "import" if verb == "extract" else verb), own


def _call(crib: Crib, f: FacetSpec, verb: str):
    """The engine operation for `verb` on facet `f`, bound to that facet."""
    import functools

    crib._require_conformed(f.noun)  # a pending facet refuses, never reads as empty
    return functools.partial(getattr(crib, graph_op(f, verb)), facet=f.noun)


def _g_read(crib, f):
    async def body(
        ref: str, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return _call(crib, f, "read")(ref, project)

    return body


def _g_edit(crib, f):
    async def body(
        ref: str,
        new_content: str,
        project: str | None = None,
        sources: list[str] | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "edit")(ref, new_content, project, sources)

    return body


def _g_append(crib, f):
    async def body(
        ref: str,
        content: str,
        project: str | None = None,
        project_path: str | None = None,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "append")(ref, content, project, sources=sources)

    return body


def _g_lookup(crib, f):
    async def body(
        query: str,
        project: str | None = None,
        k: int = 8,
        hints: list[str] | None = None,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        return _call(crib, f, "lookup")(query, project, k, hints=hints)

    return body


def _g_forget(crib, f):
    async def body(
        ref: str,
        force: bool = False,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "forget")(ref, force, project)

    return body


def _g_dep_add(crib, f):
    async def body(
        ref: str,
        dep_ref: str,
        project: str | None = None,
        project_path: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "dep_add")(ref, dep_ref, project, kind=kind)

    return body


def _g_dep_remove(crib, f):
    async def body(
        ref: str,
        dep_ref: str,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "dep_remove")(ref, dep_ref, project)

    return body


def _g_reaffirm(crib, f):
    async def body(
        ref: str, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return await _call(crib, f, "reaffirm")(ref, project)

    return body


def _g_graph(crib, f):
    async def body(
        project: str | None = None,
        project_path: str | None = None,
        sources: bool = False,
    ) -> dict[str, Any]:
        from .server import _cwd

        return _call(crib, f, "graph")(project, cwd=_cwd(project_path), sources=sources)

    return body


def _g_extract(crib, f):
    async def body(
        relpath: str, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return _call(crib, f, "import")(relpath, project)

    return body


def _g_export(crib, f):
    async def body(
        project: str | None = None, project_path: str | None = None
    ) -> str:
        return _call(crib, f, "export")(project)["markdown"]

    return body


def _g_check(crib, f):
    async def body(
        ref: str | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return _call(crib, f, "check")(ref, project)

    return body


def _g_tree(crib, f):
    async def body(
        ref: str | None = None,
        direction: str = "deps",
        depth: int = 6,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return _call(crib, f, "tree")(ref, direction, depth, project)

    return body


def _g_supersede(crib, f):
    async def body(
        ref: str,
        by_ref: str | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "supersede")(ref, by_ref, project)

    return body


def _g_add(crib, f):
    if f.has("ordered"):  # batches and placement in the order
        async def ordered(
            title: str | None = None,
            content: str = "",
            deps: list[str] | None = None,
            after: str | None = None,
            before: str | None = None,
            items: list[dict[str, Any]] | None = None,
            project: str | None = None,
            sources: list[str] | None = None,
            project_path: str | None = None,
            follows: list[str] | None = None,
            rests_on: list[str] | None = None,
            informed_by: list[str] | None = None,
        ) -> dict[str, Any]:
            return await _call(crib, f, "add")(
                title, content, deps, project, sources, None, follows, rests_on,
                informed_by, after=after, before=before, items=items,
            )

        if not f.with_role("gated"):
            return ordered

        async def ordered_gated(  # an ordered facet with an import tier
            title: str | None = None,
            content: str = "",
            deps: list[str] | None = None,
            after: str | None = None,
            before: str | None = None,
            items: list[dict[str, Any]] | None = None,
            project: str | None = None,
            sources: list[str] | None = None,
            proposed: bool = False,
            project_path: str | None = None,
            follows: list[str] | None = None,
            rests_on: list[str] | None = None,
            informed_by: list[str] | None = None,
        ) -> dict[str, Any]:
            return await _call(crib, f, "add")(
                title, content, deps, project, sources, None, follows, rests_on,
                informed_by, after=after, before=before, items=items, proposed=proposed,
            )

        return ordered_gated

    if not f.with_role("gated"):  # nothing to propose: no import tier
        async def plain(
            title: str,
            content: str,
            deps: list[str] | None = None,
            project: str | None = None,
            sources: list[str] | None = None,
            project_path: str | None = None,
            follows: list[str] | None = None,
            rests_on: list[str] | None = None,
            informed_by: list[str] | None = None,
        ) -> dict[str, Any]:
            return await _call(crib, f, "add")(
                title, content, deps, project, sources, None, follows, rests_on,
                informed_by,
            )

        return plain

    async def body(
        title: str,
        content: str,
        deps: list[str] | None = None,
        project: str | None = None,
        sources: list[str] | None = None,
        proposed: bool = False,
        project_path: str | None = None,
        follows: list[str] | None = None,
        rests_on: list[str] | None = None,
        informed_by: list[str] | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "add")(
            title, content, deps, project, sources, None, follows, rests_on,
            informed_by, proposed=proposed,
        )

    return body


def _g_list(crib, f):
    if f.has("progress") and f.report_taint:  # its working set, or just the stale
        async def working_list(
            all: bool = False,
            tainted: bool = False,
            project: str | None = None,
            project_path: str | None = None,
        ) -> dict[str, Any]:
            return _call(crib, f, "list")(project, all=all, tainted=tainted)

        return working_list

    if f.report_taint:  # the stale ones, on request
        async def tainted_list(
            tainted: bool = False,
            project: str | None = None,
            project_path: str | None = None,
        ) -> dict[str, Any]:
            return _call(crib, f, "list")(project, tainted=tainted)

        return tainted_list

    async def body(
        all: bool = False, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return _call(crib, f, "list")(project, all=all)

    return body


def _g_status(crib, f):
    async def body(
        ref: str,
        status: str,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "status")(ref, status, project)

    return body


def _g_next(crib, f):
    async def body(
        k: int = 5, project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return _call(crib, f, "next")(k, project)

    return body


def _g_review(crib, f):
    async def body(
        project: str | None = None, project_path: str | None = None
    ) -> dict[str, Any]:
        return await _call(crib, f, "review")(project)

    return body


def _g_review_apply(crib, f):
    async def body(
        batch: list[dict],
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "review_apply")(project, batch)

    return body


def _g_move(crib, f):
    async def body(
        ref: str,
        after: str | None = None,
        before: str | None = None,
        project: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return await _call(crib, f, "move")(ref, after, before, project)

    return body


# A transition's body, by the role of the status it moves to.
def _t_move(verb: str):
    """A declared facet's transition to any role but paused/dropped: the engine
    decides by where the item is (Designs.graph_transition)."""
    def make(crib, f):
        async def body(
            ref: str, project: str | None = None, project_path: str | None = None
        ) -> dict[str, Any]:
            crib._require_conformed(f.noun)
            return await crib.graph_transition(ref, verb, project, facet=f.noun)

        return body

    return make


def _t_ungate(verb: str):
    def make(crib, f):
        async def body(
            ref: str, project: str | None = None, project_path: str | None = None
        ) -> dict[str, Any]:
            return await _call(crib, f, "promote")(ref, project, to=f.transitions[verb])

        return body

    return make


def _t_pause(verb: str):
    def make(crib, f):
        async def body(
            ref: str,
            triggers: list[str] | None = None,
            why: str | None = None,
            project: str | None = None,
            project_path: str | None = None,
        ) -> dict[str, Any]:
            return await _call(crib, f, "park")(ref, project, triggers, why,
                                                to=f.transitions[verb])

        return body

    return make


def _t_drop(verb: str):
    def make(crib, f):
        async def body(
            ref: str,
            why: str,
            triggers: list[str] | None = None,
            project: str | None = None,
            project_path: str | None = None,
        ) -> dict[str, Any]:
            return await _call(crib, f, "decline")(ref, why, project, triggers,
                                                   to=f.transitions[verb])

        return body

    return make


def _gen(text: str) -> Callable[[FacetSpec], str]:
    """General wording for a declared graph facet's verb (`{n}` its noun,
    `{p}` its plural)."""
    return lambda f: text.format(n=f.noun, p=f.plural)


def _ordered_graph(f: FacetSpec) -> bool:
    return f.has("ordered")


def _transition_verbs(f: FacetSpec) -> list[Verb]:
    """One verb per transition in the spec, its body by the target's role."""
    out = []
    for verb, to in f.transitions.items():
        role = f.role(to)
        if role == "paused":
            make, policy, text = _t_pause(verb), "write", "Pause a {n} (it moves to %s), with what should bring it back."
        elif role == "dropped":
            make, policy, text = _t_drop(verb), "write", "Drop a {n} (it moves to %s), saying why, and what would reopen it."
        else:
            # crib's own decisions keep promote (proposed -> active, only);
            # a declared facet's transitions move from wherever the item is
            make = _t_ungate(verb) if f.builtin else _t_move(verb)
            policy, text = "read", "Move a {n} to %s."
        out.append(Verb(verb, "status", policy, False, _gd(verb, _gen(text % to)), make))
    return out


GRAPH_TEMPLATES: tuple[Verb, ...] = (
    Verb("add", "core", "write", False, _gd("add", _gen("Add a {n}, with what it depends on.")), _g_add),
    Verb("read", "core", "read", True, _gd("read", _gen("Read a {n}: its body, status, dependencies and what rests on it.")), _g_read),
    Verb("edit", "core", "read", False, _gd("edit", _gen("Rewrite a {n}'s body; returns what that change made stale.")), _g_edit),
    Verb("append", "core", "read", False, _gd("append", _gen("Add to a {n}'s body; returns what that change made stale.")), _g_append),
    Verb("lookup", "core", "read", True, _gd("lookup", _gen("Semantic search over {p}.")), _g_lookup),
    Verb("list", "core", "read", True, _gd("list", _gen("List {p}, grouped by status.")), _g_list),
    Verb("export", "sources", "read", True, _gd("export", _gen("All {p} as one markdown document, in dependency order.")), _g_export),
    Verb("dep_add", "links", "read", False, _gd("dep_add", _gen("Make a {n} depend on another item.")), _g_dep_add),
    Verb("dep_remove", "links", "read", False, _gd("dep_remove", _gen("Remove one of a {n}'s dependencies.")), _g_dep_remove),
    Verb("forget", "core", "read", False, _gd("forget", _gen("Delete a {n} (recoverable from its version ring).")), _g_forget),
    Verb("check", "links", "read", True, _gd("check", _gen("Which {p} are stale, and why.")), _g_check),
    Verb("reaffirm", "links", "read", False, _gd("reaffirm", _gen("Mark a stale {n} as re-read and still holding.")), _g_reaffirm),
    Verb("extract", "sources", "read", False, _gd("extract", _gen("A doc's citable sections and the procedure for drawing {p} from it.")), _g_extract),
    Verb("tree", "links", "read", True, _gd("tree", _gen("A {n}'s dependencies (or dependents) as a tree.")), _g_tree),
    Verb("graph", "links", "read", True, _gd("graph", _gen("The {p} and their links, as nodes and edges.")), _g_graph, echo=True),
    Verb("supersede", "supersede", "read", False, _gd("supersede", _gen("Replace a {n} with another; what rested on it goes stale.")), _g_supersede),
    Verb("status", "progress", "read", False, _gd("status", _gen("Set a {n}'s status.")), _g_status),
    Verb("review", "progress", "read", True, _gd("review", _gen("Review the open {p} against what they depend on.")), _g_review),
    Verb("review_apply", "progress", "write", False, _gd("review_apply", _gen("Apply a reviewed batch of {n} status changes.")), _g_review_apply),
    Verb("move", "ordered", "read", False, _gd("move", _gen("Reorder a {n}.")), _g_move),
    Verb("next", "progress", "read", True, _gd("next", _gen("The {p} ready to work on now.")), _g_next),
)


# ── the templates, in registration order ─────────────────────────────────────

TEMPLATES: tuple[Verb, ...] = (
    Verb("lookup", "core", "read", True, _d_lookup, _m_lookup, fields="filter"),
    Verb("apropos", "core", "read", True, _d_apropos, _m_apropos, fields="filter"),
    Verb("read", "core", "read", True, _d_read, _m_read),
    Verb("locate", "core", "read", True, _d_locate, _m_locate),
    Verb("add", "core", "write", False, _d_add, _m_add, elicit=True, fields="set"),
    Verb("append", "core", "write", False, _d_append, _m_append),
    Verb("edit", "core", "write", False, _d_edit, _m_edit, fields="set"),
    Verb("forget", "core", "write", False, _d_forget, _m_forget),
    Verb("reindex", "core", "read", False, _d_reindex, _m_reindex),
    Verb("versions", "core", "read", True, _d_versions, _m_versions),
    Verb("restore", "core", "read", False, _d_restore, _m_restore),
    Verb("distill", "enriched", "read", False, _d_distill, _m_distill),
    Verb("elaborate", "enriched", "read", False, _d_elaborate, _m_elaborate),
    Verb("summarize", "enriched", "read", False, _d_summarize, _m_summarize),
    Verb("import", "core", "source", False, _d_import, _m_import, needs_target=True),
    Verb("move", "core", "write", False, _d_move, _m_move),
    Verb("list", "core", "read", True, _d_list, _m_list, fields="filter"),
    # an anchored facet's family, in place of the relpath-addressed core verbs
    Verb("add", "anchored", "read", False, _d_a_add, _m_a_add, fields="set"),
    Verb("edit", "anchored", "read", False, _d_a_edit, _m_a_edit, fields="set"),
    Verb("forget", "anchored", "read", False, _d_a_forget, _m_a_forget),
    Verb("read", "anchored", "read", True, _d_a_read, _m_a_read),
    Verb("reaffirm", "anchored", "read", False, _d_a_reaffirm, _m_a_reaffirm),
    Verb("migrate", "anchored", "read", False, _d_a_migrate, _m_a_migrate),
    Verb("report", "anchored", "read", True, _d_a_report, _m_a_report),
    Verb("rehome", "anchored", "read", False, _d_a_rehome, _m_a_rehome),
    Verb("list", "anchored", "read", True, _d_list, _m_list, fields="filter"),
)


# Each family's templates by verb (facets.VERB_CATALOGUE says which a facet has).
TEMPLATES_BY_FAMILY: dict[str, dict[str, Verb]] = {
    "note": {t.verb: t for t in TEMPLATES if t.aspect != "anchored"},
    "anchored": {t.verb: t for t in TEMPLATES if t.aspect == "anchored"},
    "graph": {t.verb: t for t in GRAPH_TEMPLATES},
}


def verbs_of(spec: FacetSpec) -> list[Verb]:
    """The templates a facet gets, in registration order: one per verb the
    catalogue gives it (facets.verb_names), a graph facet's transitions last."""
    table = TEMPLATES_BY_FAMILY[facets.family(spec.aspects)]
    moves = {t.verb: t for t in _transition_verbs(spec)} if _graph(spec) else {}
    return [moves[v] if v in moves else table[v] for v in facets.verb_names(spec)]


@dataclass(frozen=True)
class FacetView:
    """One facet, resolved once from its spec: the verbs it has (their
    templates, in registration order) and what each is called. Everything that
    serves a facet (crib.server's registration, read-only sets and tool sets,
    crib.cli's commands) reads this rather than re-deriving it."""

    spec: FacetSpec
    verbs: tuple[Verb, ...]

    def verb(self, verb: str) -> Verb | None:
        return next((t for t in self.verbs if t.verb == verb), None)

    def tool(self, verb: str) -> str:
        """The MCP tool name of `verb` (a built-in may alias it: note_store)."""
        return tool_name(self.spec, verb)

    def cli(self, verb: str) -> str:
        """Its CLI subcommand word."""
        return self.spec.verb_names.get(verb, verb).replace("_", "-")

    def command(self, verb: str) -> str:
        """Its full CLI command (`crib plan status`)."""
        return f"crib {self.spec.noun} {self.cli(verb)}"

    def status_for(self, role: str) -> str | None:
        """This facet's (first) status playing `role`, or None."""
        return next(iter(self.spec.with_role(role)), None)

    def describe(self, verb: str) -> str:
        """`verb`'s tool description: its template's wording, then the facet's
        own (`about`, and `[<noun>.when]` for this verb), then its fields'
        parameters or filters."""
        t = self.verb(verb)
        if t is None:
            raise KeyError(f"{self.spec.noun} has no verb {verb!r}")
        doc = t.describe(self.spec)
        own = [w for w in (self.spec.about, self.spec.when.get(verb, "")) if w]
        if own:  # between the template's text and its trailing newlines
            body = doc.rstrip("\n")
            doc = body + "\n\n" + "\n\n".join(own) + doc[len(body):]
        if t.fields and self.spec.params and t.verb != "list":
            doc += _fields_text(self.spec, t.fields)
        elif t.verb == "list" and t.fields:  # the field-filtering list
            doc += _fields_text(self.spec, "filter")
        return doc

    def mcp(self, verb: str) -> str:
        """`verb`'s MCP wire signature, `name` / `name=default` tokens (the
        shape crib.cli's VERBS rows declare and tests/test_surface_parity.py
        holds against the served schema), read off the body itself: its
        parameters are described once. `ctx` (FastMCP's injected context)
        isn't on the wire."""
        import inspect

        t = self.verb(verb)
        if t is None:
            raise KeyError(f"{self.spec.noun} has no verb {verb!r}")
        body = t.make(None, self.spec)  # a body touches crib only when called
        _with_fields(body, self.spec, t.fields)
        return " ".join(
            p.name if p.default is inspect.Parameter.empty else f"{p.name}={p.default!r}"
            for p in inspect.signature(body).parameters.values() if p.name != "ctx")

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(self.tool(t.verb) for t in self.verbs)

    @property
    def read_only_tools(self) -> frozenset[str]:
        """The tools a read-only tool set (`--tools notes:ro`) keeps."""
        return frozenset(self.tool(t.verb) for t in self.verbs if t.read_only)


_VIEWS: dict[int, FacetView] = {}


def view(spec: FacetSpec) -> FacetView:
    """The resolved view of `spec` (cached per spec object)."""
    v = _VIEWS.get(id(spec))
    if v is None or v.spec is not spec:
        v = _VIEWS[id(spec)] = FacetView(spec, tuple(verbs_of(spec)))
    return v


def register(crib: Crib, crib_tool: Callable[..., Callable[[Any], Any]]) -> list[str]:
    """Register every generated facet's tools through crib.server's
    `crib_tool`; the tool names, in order."""
    import sys

    names = []
    for spec in facets.FACETS:
        v = view(spec)
        for t in v.verbs:
            name = v.tool(t.verb)
            if name in names:
                # the registry's checks make names unique; never let FastMCP
                # replace a tool silently if one slips through
                print(f"[crib] facet {spec.noun!r}: tool {name} is already "
                      "registered; skipping this one", file=sys.stderr)
                continue
            body: Any = t.make(crib, spec)  # a plain function: FastMCP reads these
            body.__name__ = body.__qualname__ = name
            _with_fields(body, spec, t.fields)
            body.__doc__ = v.describe(t.verb)
            crib_tool(t.policy, echo=t.echo, elicit=t.elicit,
                      needs_target=t.needs_target)(body)
            names.append(name)
    return names
