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
    make: Callable[[Any, FacetSpec], Callable[..., Any]]  # (crib, spec) -> body
    elicit: bool = False
    needs_target: bool = False


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
    ) -> list[dict[str, Any]]:
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
    ) -> list[dict[str, Any]]:
        return crib.apropos(query, project, k, tags, hints=hints, facet=f.noun)

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
    ) -> dict[str, Any]:
        return await crib.store_note(content, title, project, tags, facet=f.noun)

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
    ) -> dict[str, Any]:
        return await crib.edit_note(relpath, new_content, project, facet=f.noun)

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


# ── the templates, in registration order ─────────────────────────────────────

TEMPLATES: tuple[Verb, ...] = (
    Verb("lookup", "core", "read", True, _d_lookup, _m_lookup),
    Verb("apropos", "core", "read", True, _d_apropos, _m_apropos),
    Verb("read", "core", "read", True, _d_read, _m_read),
    Verb("locate", "core", "read", True, _d_locate, _m_locate),
    Verb("add", "core", "write", False, _d_add, _m_add, elicit=True),
    Verb("append", "core", "write", False, _d_append, _m_append),
    Verb("edit", "core", "write", False, _d_edit, _m_edit),
    Verb("forget", "core", "write", False, _d_forget, _m_forget),
    Verb("reindex", "core", "read", False, _d_reindex, _m_reindex),
    Verb("versions", "core", "read", True, _d_versions, _m_versions),
    Verb("restore", "core", "read", False, _d_restore, _m_restore),
    Verb("distill", "enriched", "read", False, _d_distill, _m_distill),
    Verb("elaborate", "enriched", "read", False, _d_elaborate, _m_elaborate),
    Verb("summarize", "enriched", "read", False, _d_summarize, _m_summarize),
    Verb("import", "core", "source", False, _d_import, _m_import, needs_target=True),
    Verb("move", "core", "write", False, _d_move, _m_move),
)


def generated(spec: FacetSpec) -> bool:
    """Whether this facet's verbs come from the templates here. A facet with
    links keeps its hand-written family until the graph engine (step 6); an
    anchored one gets its own templates (step 2f)."""
    return not (spec.has("links") or spec.has("anchored"))


def verbs_of(spec: FacetSpec) -> list[Verb]:
    return [t for t in TEMPLATES if spec.has(t.aspect)] if generated(spec) else []


def register(crib: Crib, crib_tool: Callable[..., Callable[[Any], Any]]) -> list[str]:
    """Register every generated facet's tools through crib.server's
    `crib_tool`; the tool names, in order."""
    names = []
    for spec in facets.FACETS:
        for t in verbs_of(spec):
            name = tool_name(spec, t.verb)
            body: Any = t.make(crib, spec)  # a plain function: FastMCP reads these
            body.__name__ = body.__qualname__ = name
            body.__doc__ = t.describe(spec)
            crib_tool(t.policy, elicit=t.elicit, needs_target=t.needs_target)(body)
            names.append(name)
    return names
