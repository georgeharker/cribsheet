"""`crib` — one binary, two faces (DESIGN §5).

  crib --mcp            run the MCP stdio server (alias: `crib serve`)
  crib <verb> …         CLI mirroring the MCP tool surface

Verbs are named identically to the MCP tools. Output-producing verbs accept
`--json` for scripting; store/append/edit accept `-` to read content from stdin.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from ast import literal_eval
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import proc
from . import style as _st
from .app import Crib
from .codequery import GRAPH_GROUPINGS


def _read_content(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


_EDITOR_HINT = (
    "# Write the body below. Lines starting with '#' are ignored, and an empty\n"
    "# file aborts.\n"
)


def _from_editor(what: str) -> str:
    """Open `$EDITOR` on a scratch file and return what came back.

    The escape hatch for the worst friction in this surface: a design decision is
    a paragraph or five, and shell-quoting prose is miserable enough that it
    quietly pushes people toward one-line decisions. Only ever reached on a tty
    with the body omitted — a pipeline reads stdin instead."""
    import os
    import tempfile

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as fh:
        fh.write(f"\n{_EDITOR_HINT}# ({what})\n")
        path = Path(fh.name)
    try:
        proc.run([*editor.split(), str(path)], check=True)
        text = path.read_text()
    finally:
        path.unlink(missing_ok=True)
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#")).strip()
    if not body:
        raise SystemExit(f"crib: empty body — {what} aborted")
    return body


def _body(
    value: str | None,
    file: str | None = None,
    *,
    what: str = "body",
    default: str | None = None,
) -> str:
    """Body text for a write verb, from whichever way the caller wants to give it:
    `--file <path>`, `-` (stdin), the positional argument, or — when it's omitted
    at a terminal — `$EDITOR`. Omitted with stdin NOT a tty means a pipeline: read
    stdin, don't try to open an editor into a pipe."""
    if file:
        return Path(file).expanduser().read_text()
    if value == "-":
        return sys.stdin.read()
    if value is not None:
        return value
    if default is not None:
        return default  # optional body (a plan item may be title-only)
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _from_editor(what)
    return sys.stdin.read()


def _split_labels(spec: str | None) -> list[str] | None:
    """Parse a `--keywords a,b,c` spec into a label list.

    ``None`` (flag absent) stays None — "use the config default". An *explicit*
    empty string maps to ``[]`` — "no labels", which disables a default-on index
    (e.g. `keyword_labels=["keywords"]`). The two are distinct: conflating them
    made `--keywords ""` silently fall back to the default, so an eval baseline
    could never turn keyword_index *off* — the `--lift keywords` baseline ran with
    keywords already on, hiding the true lift as a Δ0 null."""
    if spec is None:
        return None
    return [s.strip() for s in spec.split(",") if s.strip()]


def _render_markdown(text: str) -> None:
    """Pretty-print note markdown via llmkit's rich renderer (honouring
    $CRIB_THEME_FILE). Falls back to raw text if the `render` extra
    (llmkit[md]) isn't installed."""
    import os

    try:
        from llmkit.md.render.cli import _load_theme
        from rich.console import Console
        from rich.markdown import Markdown
    except Exception:  # noqa: BLE001 — render extra optional; degrade to raw
        sys.stdout.write(text)
        return
    theme, code_theme = _load_theme(os.environ.get("CRIB_THEME_FILE"))
    Console(theme=theme).print(Markdown(text, code_theme=code_theme))


def _emit_apropos(hits: Any, as_json: bool) -> None:
    """Human view of `apropos`: a locator header per hit, then the matched
    section rendered as markdown. `--json` dumps the raw hits instead."""
    if as_json:
        _emit(hits, True)
        return
    for h in hits:
        loc = (
            f":{h.get('line_start')}-{h.get('line_end')}" if h.get("line_start") else ""
        )
        head = f" — {_st.paint('heading', h['heading'])}" if h.get("heading") else ""
        stale = (_st.paint("warn", "  ⚠︎ stale decision (a dep moved)")
                 if h.get("tainted") else "")
        score = _st.paint("score", f"[{h.get('score', 0.0):.3f}]")
        path = _item_path(h.get("project"), h.get("relpath", ""),
                          h.get("store") or "notes", loc)
        print(f"\n{score} {path}{head}{stale}")
        _render_markdown(h.get("section") or "")
    _emit_rebuilding_note(hits)


def _print_note(text: str, as_json: bool) -> None:
    """`read` output: JSON string when --json, pretty markdown to a tty, else
    raw bytes so pipelines get the file verbatim."""
    if as_json:
        print(json.dumps(text))
    elif sys.stdout.isatty():
        _render_markdown(text)
    else:
        sys.stdout.write(text)


def _emit(obj: Any, as_json: bool) -> None:
    if as_json:

        def default(o):
            return asdict(o) if is_dataclass(o) and not isinstance(o, type) else str(o)

        print(json.dumps(obj, indent=2, default=default))
        return
    _emit_human(obj)


def _emit_human(obj: Any) -> None:
    if isinstance(obj, list):
        for item in obj:
            _emit_human_one(item)
        _emit_rebuilding_note(obj)
    else:
        _emit_human_one(obj)


def _emit_rebuilding_note(hits: Any) -> None:
    """One line when a hit says its project is still being re-embedded after a store
    wipe (embedder change) — otherwise a short result set reads as "that's all there
    is" instead of "the index isn't back yet"."""

    def _flag(h: Any) -> bool:
        return bool(
            h.get("index_rebuilding")
            if isinstance(h, dict)
            else getattr(h, "index_rebuilding", False)
        )

    if isinstance(hits, list) and any(_flag(h) for h in hits):
        print(_st.paint(
            "warn",
            "⚠︎ index rebuilding: this project is still re-embedding "
            "(see `crib status`) — results are incomplete"
        ))


def _emit_human_one(item: Any) -> None:
    from .app import LookupHit

    # Normalize a daemon's dict-shaped lookup hit to the same fields as the
    # in-process LookupHit dataclass so both render identically.
    if isinstance(item, dict) and "score" in item and "snippet" in item:
        item = LookupHit(
            project=item.get("project", ""),
            relpath=item.get("relpath", ""),
            heading=item.get("heading", ""),
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
            score=item.get("score", 0.0),
            line_start=item.get("line_start"),
            line_end=item.get("line_end"),
            index_rebuilding=bool(item.get("index_rebuilding")),
            tainted=bool(item.get("tainted")),
            store=item.get("store") or "notes",
        )
    if isinstance(item, LookupHit):
        loc = f":{item.line_start}-{item.line_end}" if item.line_start else ""
        head = f"  {_st.paint('heading', item.heading)}" if item.heading else ""
        first = item.snippet.splitlines()[0][:100] if item.snippet else ""
        # a stale DECISION says so on the hit itself: the moment you retrieve it is
        # the moment the warning can still change what you do with it
        stale = (
            _st.paint("warn", f"  {_TAINT} stale (a dep moved — `crib design read`)")
            if item.tainted
            else ""
        )
        print(f"{_st.paint('score', f'[{item.score:.3f}]')} "
              f"{_item_path(item.project, item.relpath, item.store, loc)}"
              f"{head}{stale}\n    {first}")
    elif isinstance(item, dict) and ("relpath" in item or "from" in item):
        _emit_write_result(item)
    elif isinstance(item, dict):
        print("  ".join(f"{k}={v}" for k, v in item.items()))
    else:
        print(item)


def _emit_write_result(item: dict) -> None:
    """Echo a write/move result so the target namespace is never silent."""
    if "from" in item:  # move
        f, t = item["from"], item["to"]
        src = _st.paint("path", f"{f['project']}/{f['relpath']}")
        dst = _st.paint("path", f"{t['project']}/{t['relpath']}",
                        _item_link(t["project"], t["relpath"], item.get("store")))
        print(f"moved  {src}  →  {dst}")
    else:  # store/append/edit/forget
        proj, rel = item.get("project", "?"), item.get("relpath", "")
        verb = "removed" if item.get("removed") else "→ stored in"
        link = None if item.get("removed") else _item_link(proj, rel, item.get("store"))
        print(f"{verb}  {_st.paint('path', f'{proj}/{rel}', link)}")
    if item.get("project_created"):
        print(_st.paint("meta", "  (created project "
                        f"'{item.get('project') or item['to']['project']}')"))
    for s in item.get("similar") or []:
        print(
            _st.paint("warn", f"  ⚠︎ similar [{s['score']:.3f}]:") + " "
            + _st.paint("path", s["relpath"])
            + (f" — {_st.paint('heading', s['heading'])}" if s.get("heading") else "")
        )


# ── how a symbol looks, in one place ─────────────────────────────────────────
# Eight emitters used to spell this out themselves, so a change to what a symbol
# renders as meant eight consistent edits. The atoms are shared; `render_symbol`
# composes the shapes that actually differ.


def _sym_name(d: dict) -> str:
    """The symbol's rendered name. `id` where the payload is a graph node (it may be
    cross-project or external), the qualified name everywhere else."""
    return str(
        d.get("fqn") or d.get("id") or d.get("symbol_ref") or d.get("symbol") or ""
    )


_SOURCE_ROOTS: dict[str, Path | None] = {}


def _code_link(d: dict) -> str | None:
    """A `file://` link to a symbol's source file, while styling is on: the
    project's source root (recorded beside its symbol index) joined with the
    symbol's repo-relative `file`. None when either is unknown."""
    proj, rel = d.get("project"), d.get("file")
    if not _st.enabled() or not proj or not rel:
        return None
    if proj not in _SOURCE_ROOTS:
        try:
            from .codeindex import SymbolIndex
            from .paths import Paths

            _SOURCE_ROOTS[proj] = SymbolIndex(
                Paths.resolve().project_dir(proj)).source_root()
        except Exception:  # noqa: BLE001 — a link is a nicety, never a failure
            _SOURCE_ROOTS[proj] = None
    root = _SOURCE_ROOTS[proj]
    return _st.file_link(root / rel) if root else None


_ITEM_PATHS: dict[str, Any] = {}


def _item_link(project: Any, relpath: Any, store: str | None) -> str | None:
    """A `file://` link to an item's file (note, decision, plan, learning ...),
    while styling is on: the project's configured root for the facet's store
    (`resolve_project_paths`), plus the relpath. None when that isn't a file."""
    if not _st.enabled() or not project or not relpath or not store:
        return None
    try:
        from .config import Config
        from .paths import Paths, resolve_project_paths

        if project not in _ITEM_PATHS:
            paths = Paths.resolve()
            _ITEM_PATHS[project] = resolve_project_paths(
                paths, Config.load(paths.config_file), str(project))
        path = _ITEM_PATHS[project].pillar_dir(store) / str(relpath)
        return _st.file_link(path) if path.is_file() else None
    except Exception:  # noqa: BLE001 — a link is a nicety, never a failure
        return None


def _item_path(project: Any, relpath: Any, store: str | None,
               suffix: str = "") -> str:
    """A relpath (plus e.g. `:3-9`) painted as a path, linked when resolvable."""
    return _st.paint("path", f"{relpath or ''}{suffix}",
                     _item_link(project, relpath, store))


def _store_of(args: Any) -> str | None:
    """The store dir of the facet a command names (`crib plan list` -> plans)."""
    from . import facets

    try:
        return facets.by_noun(getattr(args, "cmd", "") or "").store
    except KeyError:
        return None


def _lab(text: str) -> str:
    """`crib status`'s key column (`daemon`, `lsp` ...), padded then painted."""
    return _st.paint("label", f"{text:10}")


def _sym_loc(d: dict, gap: str = "  ") -> str:
    """`file:line` — nothing without a line, because a rolled-up module IS its file
    and an unresolved target has no location to point at."""
    if d.get("external") or not d.get("line"):
        return ""
    return gap + _st.paint("path", f"{d.get('file', '')}:{d['line']}", _code_link(d))


def _sym_tags(d: dict, ascii_mode: bool = False) -> str:
    """The flags a symbol can carry, in a fixed order so two rows line up."""
    out = ""
    if d.get("repeat"):
        out += " ↑" if not ascii_mode else " ^"
    if d.get("external"):
        out += " ·ext"
    if d.get("has_learning"):
        out += " *" if ascii_mode else " ※"
    if d.get("truncated"):
        out += "  …truncated"
    return out


def render_symbol(d: dict, style: str = "inline", ascii_mode: bool = False) -> str:
    """One symbol, rendered for `style`:

    `inline` — name, kind, location (xref, dossier)
    `title`  — name and kind only, for a header that appends its own context
    `hit`    — a ranked-list row (kind column, then name and location)
    `tree`   — a graph row: name, flags, location
    """
    name, kind = _st.paint("name", _sym_name(d)), d.get("kind", "")
    if style == "title":
        return f"{name}  {_st.paint('kind', f'({kind})')}"
    if style == "hit":
        return f"{_st.paint('kind', f'{kind:8}')} {name}{_sym_loc(d)}"
    if style == "tree":
        return f"{name}{_st.paint('meta', _sym_tags(d, ascii_mode))}{_sym_loc(d, '   ')}"
    return f"{name}  {_st.paint('kind', f'({kind})')}{_sym_loc(d)}"


def _emit_code(data: Any, verb: str, as_json: bool) -> None:
    """Human-readable rendering for the code verbs; raw JSON with the global --json."""
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    # implicit-resolution diagnostic (server echoes it on an empty sticky/seeded
    # result — see server._echo_list); render the note, not a blank hit row.
    if (
        isinstance(data, list)
        and len(data) == 1
        and isinstance(data[0], dict)
        and data[0].get("note")
        and "resolved_project" in data[0]
    ):
        print(f"(0 matches) {data[0]['note']}")
        return
    if verb == "code-index":
        if not isinstance(data, dict):
            print(data)
            return
        if data.get("skipped"):
            print(f"{data.get('file', '')}: {data['skipped']}")
            return
        err = (
            f"  (descriptions_error: {data['descriptions_error']})"
            if data.get("descriptions_error")
            else ""
        )
        print(
            f"{data.get('file', '')}: {data.get('symbols', 0)} symbols, "
            f"{data.get('described', 0)} described{err}"
        )
        if data.get("store"):
            print(f"  → {data['store']}")
    elif verb == "code-lookup":
        if not data:
            print("(no matches — is this project code-indexed?)")
            return
        for h in data:
            refs = len(h.get("references") or [])
            cg = f"  {len(h.get('called_by') or [])}←/{len(h.get('calls') or [])}→" + (
                f"/{refs}⇐" if refs else ""
            )
            rank = _st.paint("rank", f"[{h.get('rank', '?')}]")
            print(f"{rank} {render_symbol(h, 'hit')}{_st.paint('meta', cg)}")
            if h.get("description"):
                print(f"      {h['description']}")
            if h.get("learning"):
                _print_learning(h["learning"], "      ", h.get("project"))
    elif verb == "code-xref":
        if not data:
            print("(symbol not found in the symbol_index)")
            return
        for e in data:
            print(render_symbol(e))
            for arrow, key in (("←", "called_by"), ("→", "calls"),
                               ("⇐", "references")):  # ⇐ = referenced by
                for c in e.get(key) or []:
                    print(f"   {_st.paint('meta', arrow)} {c}")
            if e.get("learning"):
                _print_learning(e["learning"], "   ", e.get("project"))


def _emit_status(d: Any, as_json: bool) -> None:
    """Human summary for `crib status`: backend + git lines, live LSP sessions,
    in-flight indexing, then a per-project inventory table."""
    if as_json:
        _emit(d, True)
        return
    print(f"{_lab('store')} {d.get('store')}  embed: {d.get('embed_model')}")
    g = d.get("git") or {}
    if g.get("enabled"):
        parts = [
            g.get("remote") or "no remote",
            "clean" if not g.get("dirty") else f"{g['dirty']} uncommitted",
        ]
        if "ahead" in g:
            parts.append(f"↑{g['ahead']} ↓{g['behind']}")
        print(f"{_lab('git')} " + "  ".join(parts))
        if g.get("last_commit"):
            print(f"{'':10} last: {g['last_commit']}")
    else:
        print(f"{_lab('git')} not enabled (crib memory setup --remote <url>)")
    for s in d.get("lsp_sessions") or []:
        state = "busy" if s.get("busy") else f"idle {s.get('idle_s', 0):.0f}s"
        alive = "" if s.get("alive") else _st.paint("error", "  DEAD")
        print(
            f"{_lab('lsp')} {s.get('server')}  {s.get('root')}  "
            f"pid {s.get('pid')}  {state}{alive}"
        )
    if d.get("reconciling"):
        why = f" ({d['reconcile_reason']})" if d.get("reconcile_reason") else ""
        print(
            f"{_lab('reconcile')} in progress{why}: "
            f"{d.get('reconcile_remaining', '?')} project(s) to go"
        )
    if d.get("index_rebuilding"):
        print(
            f"{_lab('rebuilding')} not re-embedded yet: {', '.join(d['index_rebuilding'])}"
        )
    for proj, sw in (d.get("sweeps") or {}).items():
        print(f"{_lab('sweep')} {proj}: {sw.get('done', 0)}/{sw.get('total', 0)} files")
    for proj, files in (d.get("indexing") or {}).items():
        print(f"{_lab('indexing')} {proj}: {', '.join(files)}")
    _emit_daemon(d)
    projs = d.get("projects") or []
    print(f"{_lab('projects')} {len(projs)}")
    if projs:
        w = max(len(p["project"]) for p in projs)
        for p in projs:
            # stale decisions ride along on the inventory line: an ambient count
            # nobody has to think to ask for (`crib design check` says which)
            stale = (
                _st.paint("warn", f"  ⚠︎ {p['design_tainted']} stale decision(s)")
                if p.get("design_tainted")
                else ""
            )
            print(
                f"  {_st.paint('name', format(p['project'], f'{w}'))}  notes {p['notes']:4}  "
                f"designs {p.get('designs', 0):3}  plans {p.get('plans', 0):3}  "
                f"docs {p['doc_chunks']:4}  symbols {p['symbols']:5}  "
                f"learnings {p['learnings']:3}{stale}"
            )


def _E_ops(d, a):
    _emit_ops(d, a.json)


def _emit_ops(d: Any, as_json: bool) -> None:
    """Human ops view — the queue, made visible: sweeps, generation slots."""
    if as_json:
        print(json.dumps(d, indent=2, default=str))
        return
    from .codestore import format_sweep

    sweeps = d.get("sweeps") or {}
    if sweeps:
        print(_st.paint("header", "sweeps:"))
        for proj, sw in sweeps.items():
            print(f"  {_st.paint('name', proj)}: {format_sweep(sw)}")
    else:
        print(_st.paint("header", "sweeps:") + _st.paint("meta", " (none running)"))
    g = d.get("generation") or {}
    bits = [
        f"in-flight {g.get('inflight', 0)}",
        f"waiting {g.get('waiting', 0)}",
        f"429s {g.get('rate_limited', 0)}",
    ]
    if g.get("cooldown_s"):
        bits.append(f"cooldown {g['cooldown_s']}s")
    print(_st.paint("header", "generation:") + " " + " · ".join(bits))
    for proj, files in (d.get("indexing") or {}).items():
        print(f"  indexing {_st.paint('name', proj)}: {len(files)} file(s) in flight")


def _emit_projects(rows: Any, as_json: bool) -> None:
    """`crib project list`: one project per line, annotated when its notes live in
    a repo — and loudly when that repo isn't on this machine, since such a project
    reads as empty everywhere else."""
    if as_json:
        print(json.dumps(rows, indent=2, default=str))
        return
    for r in rows or []:
        if not isinstance(r, dict):
            print(r)
            continue
        line = _st.paint("name", r.get("project", ""))
        if r.get("store_root"):
            line += _st.paint("meta", f"   in-repo: {r['store_root']}")
        if r.get("unavailable"):
            line += _st.paint("warn", "   ⚠︎ UNAVAILABLE (repo not on this machine)")
        print(line)


def _sweep_progress(d: Any) -> str:
    """`done/total files`, or "starting" before the sweep has counted its files
    (right after it began, both are still None)."""
    if d.get("total") is None:
        return "starting (counting files)"
    return f"{d.get('done') or 0}/{d['total']} files"


def _emit_facet_summary(d: Any) -> None:
    """One line per facet mapping a setup/index answer reports (`facets`)."""
    for noun, f in (d.get("facets") or {}).items():
        how = ", ".join(w for w in ("read-only" if f.get("read_only") else "",
                                   "ids derived" if f.get("ids") == "derived" else "") if w)
        state = (f"{f['items']} item(s) indexed" if "items" in f
                 else f"waiting {f.get('waiting', '')}")
        print(f"  facet {_st.paint('kind', noun)}: "
              f"{_st.paint('path', str(f.get('dir')) + ' ' + str(f.get('glob')))}"
              f"{_st.paint('meta', f' ({how})') if how else ''}: {state}")


def _emit_project(d: Any, verb: str | None, as_json: bool) -> None:
    """Human summary for `crib project <verb>`."""
    if as_json:
        print(json.dumps(d, indent=2, default=str))
        return
    if not isinstance(d, dict):
        print(d)
        return
    proj = _st.paint("name", d.get("project", ""))
    if verb == "migrate":
        if not d.get("changed") and not d.get("skipped"):
            print(f"{proj}: layout already current — nothing to migrate")
            return
        print(
            f"{proj}: moved {len(d.get('moved') or [])} facet note(s) to the "
            f"sibling pillar stores; requalified "
            f"{len(d.get('refs_rewritten') or [])} citation(s)"
        )
        for s in d.get("skipped") or []:
            print(_st.paint(
                "warn",
                f"  ! collision left in place: {s['from']} → {s['to']} "
                f"(resolve by hand, re-run migrate)"
            ))
        rec = d.get("reconciled") or {}
        print(
            f"  reconcile: {rec.get('changed', 0)} changed, "
            f"{rec.get('removed', 0)} removed"
        )
        return
    if verb in ("adopt", "release"):
        if not d.get("changed"):
            print(d.get("message") or f"{proj}: nothing to do")
            return
        where = ("→ " + d["store"]) if verb == "adopt" else "→ the global store"
        print(
            f"{proj}: moved {d.get('notes_moved', 0)} note(s) + "
            f"{d.get('versions_moved', 0)} version(s) {where}"
        )
        if verb == "adopt":
            print(
                f"  recorded as {d.get('store_root', '')}  "
                f"(commit the notes with this repo)"
            )
        rec = d.get("reconciled") or {}
        print(
            f"  reconcile: {rec.get('changed', 0)} changed, "
            f"{rec.get('removed', 0)} removed  (0/0 = ids unchanged, as expected)"
        )
        return
    if verb == "create":
        if d.get("project_created"):
            print(d.get("message") or f"{proj} created")
        else:
            print(d.get("message") or f"{proj}: already exists — nothing changed")
            return
        if d.get("store"):
            print(f"  store: {d['store']} ({d.get('store_root', '')})")
        return
    if verb == "delete":
        if not d.get("deleted"):
            print(d.get("message") or f"{proj}: nothing to delete")
            return
        print(d.get("message") or f"{proj} deleted")
        if d.get("trash"):
            print(f"  undo: move the tree back from {d['trash']}")
        return
    if verb == "rename":
        print(d.get("message") or f"{proj}: renamed")
        for r in d.get("refs_affected") or []:
            print(_st.paint(
                "warn",
                f"  ! {r.get('referrer')} refs: {r.get('target')} — update its "
                "repo's .crib"
            ))
        return
    if verb == "status":
        state = (_st.paint("ok", "indexed") if d.get("indexed")
                 else _st.paint("warn", "NOT indexed"))
        print(
            f"{proj}: {state} — {d.get('symbols', 0)} symbols "
            f"in {d.get('files', 0)} files"
        )
        kinds = d.get("kinds") or {}
        if kinds:
            print("  " + ", ".join(f"{k}:{n}" for k, n in sorted(kinds.items())))
        if d.get("paths"):
            print(f"  paths: {', '.join(d['paths'])}")
        return
    if verb == "forget":
        print(
            f"{proj}: cleared {d.get('symbols_removed', 0)} symbols"
            + (
                f", {d['learnings_removed']} learnings"
                if d.get("learnings_removed")
                else ""
            )
        )
        return
    if verb == "wait":
        if d.get("running"):
            print(
                f"{proj}: still running — {_sweep_progress(d)}; "
                f"call `crib project wait` again to keep waiting"
            )
        else:
            done = " (already finished)" if d.get("complete") else " (nothing running)"
            print(f"{proj}: not running{done}")
        return
    if verb == "cancel":
        if d.get("cancelled"):
            print(
                f"{proj}: cancelled at {d.get('done_at_cancel')}/{d.get('total')} "
                f"files — completed files remain; re-run `crib project index` "
                f"to resume"
            )
        else:
            print(f"{proj}: nothing running")
        return
    # setup / index
    if d.get("running"):  # a daemon-side sweep still going (joined or started)
        print(f"{proj}: indexing in progress — {_sweep_progress(d)} "
              f"(`crib project wait` to follow it, `crib project status` to look)")
        _emit_facet_summary(d)
        return
    made = "  (created .crib)" if d.get("crib_created") else ""
    docs = f", {d['docs_imported']} docs imported" if d.get("docs_imported") else ""
    fails = (
        f", {d['describes_failed']} desc-failed" if d.get("describes_failed") else ""
    )
    print(
        f"{proj}: indexed {d.get('files_indexed', 0)}/{d.get('files_seen', 0)} files, "
        f"{d.get('symbols', 0)} symbols, {d.get('described', 0)} described"
        f"{fails}{docs}{made}"
    )
    _emit_facet_summary(d)
    errs = d.get("errors") or []
    if errs:
        print(_st.paint("warn",
                        f"  {len(errs)} file(s) errored (first: {errs[0].get('file', '')})"))
    if empty := d.get("empty_extracts") or []:
        servers = ", ".join(sorted({str(e.get("server")) for e in empty}))
        print(_st.paint("warn",
                        f"  {len(empty)} code file(s) got NO symbols from {servers} "
                        f"(first: {empty[0].get('file', '')}): see `crib project doctor`"))


def _emit_code_dossier(d: Any, as_json: bool) -> None:
    """Full single-symbol view: header + description + annotated neighbours + learning."""
    if as_json:
        print(json.dumps(d, indent=2, default=str))
        return
    if not d or not d.get("symbol_ref"):
        print("(symbol not found — is this project code-indexed?)")
        return
    print(render_symbol(d))
    _emit_resolved(d)
    if d.get("signature"):
        print(f"  {_st.paint('meta', d['signature'])}")
    if d.get("description"):
        print(f"  {d['description']}")
    if d.get("learning"):
        _print_learning(d["learning"], "  ", d.get("project"))
    for label, arrow in (("called_by", "←"), ("calls", "→"), ("references", "⇐")):
        rows = d.get(label) or []
        if rows:
            print(f"  {_st.paint('header', f'{label} {arrow}')}")
            for r in rows:
                desc = _st.paint("meta", f"  — {r['description']}") if r.get(
                    "description") else ""
                print(f"     {_st.paint('name', r.get('symbol', ''))}{desc}")


def _print_learning(learning: dict, indent: str, project: Any = None) -> None:
    """Render an attached symbol learning (※) under a code-lookup/xref hit."""
    flag = (_st.paint("warn", "  ⚠︎ stale — body changed since written")
            if learning.get("stale") else "")
    path = _item_path(learning.get("project") or project,
                      learning.get("relpath", ""), "learnings")
    print(f"{indent}{_st.paint('header', '※ note')} ({path}){flag}")
    for line in (learning.get("body") or "").splitlines():
        print(f"{indent}  {line}" if line.strip() else "")


def _emit_code_learning(data: Any, verb: str, as_json: bool) -> None:
    """Confirmation/print for the symbol-learning verbs (append/edit/forget/read)."""
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    sym = _st.paint("name", data.get("symbol", ""))
    rel = _item_path(data.get("project"), data.get("relpath", ""), "learnings")
    if verb == "learning-read":
        if not data.get("found"):
            print(f"(no learning for {sym})")
            return
        print(f"# {sym}  [{rel}]\n{(data.get('body') or '').strip()}")
        return
    if verb == "learning-forget":
        print(f"forgot {sym}  ({rel})")
        return
    if verb == "learning-reaffirm":
        print(f"reaffirmed {sym} (cleared ⚠︎ stale)  → {rel}")
        return
    if verb == "learning-add":
        print(
            f"{'created' if data.get('created') else 'appended'} learning: {sym}  → {rel}"
        )
        return
    print(f"edited learning: {sym}  → {rel}")  # learning-edit


def _emit_code_report(rows: Any, as_json: bool) -> None:
    """Health report for attached learnings (ok/moved/orphan)."""
    if as_json:
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print("(no learnings recorded)")
        return
    icon = {"ok": "·", "moved": "~", "orphan": "✗"}
    role = {"ok": "ok", "moved": "warn", "orphan": "error"}
    for r in rows:
        st = r.get("status", "")
        line = (_st.paint(role.get(st, "meta"), f"{icon.get(st, '?')} {st:7}")
                + f" {_st.paint('name', r.get('symbol', ''))}")
        if st == "moved":
            line += f"   {r.get('file', '')} → {r.get('new_file', '')}"
        elif st == "orphan":
            line += f"   (was {r.get('file', '')})"
        print(line)
    bad = sum(1 for r in rows if r.get("status") != "ok")
    if bad:
        print(
            f"\n{bad} need attention — `crib learning rehome <fqn>` for suggestions, "
            f"or `crib learning forget <fqn>`"
        )


def _emit_code_rehome(data: Any, as_json: bool) -> None:
    """Ranked rehome candidates (no target) or a move confirmation."""
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    if "candidates" in data:
        print(f"rehome {_st.paint('name', data.get('old', ''))} → candidates:")
        cands = data.get("candidates") or []
        if not cands:
            print("  (none — `crib learning forget` if it's truly gone)")
            return
        for c in cands:
            score = c.get("score", "")
            print(f"  {_st.paint('score', f'[{score}]')} "
                  f"{_st.paint('name', _sym_name(c))}   "
                  f"{_st.paint('path', c.get('file', ''), _code_link(c))}")
        print(_st.paint("meta",
                        f"\nconfirm: crib learning rehome {data.get('old', '')} <symbol>"))
        return
    print(
        f"rehomed {_st.paint('name', data.get('old', ''))} → "
        f"{_st.paint('name', data.get('new', ''))}  "
        f"({_item_path(data.get('project'), data.get('relpath', ''), 'learnings')})"
    )


_TAINT = "⚠︎"
# The import tier reads as a QUESTION, not a warning: a proposed entry isn't
# stale, it is un-blessed — a different thing to do about it (`design promote`).
_PROPOSED = "?"


def _facet_flags(row: Any) -> str:
    """The glyphs a design row carries — stale, and/or awaiting promotion."""
    return (f" {_TAINT}" if row.get("tainted") else "") + (
        f" {_PROPOSED}" if row.get("status") == "proposed" else ""
    )


def _tree_glyphs(ascii_mode: bool) -> tuple[str, str, str, str]:
    """(branch, last, vertical, blank) connectors — shared by the code-graph and
    design-tree renderers so the two trees read identically."""
    if ascii_mode:
        return "|-", "`-", "|  ", "   "
    return "├─", "└─", "│  ", "   "


def _emit_design_tree(data: Any, args: Any) -> None:
    """Design dependency tree, in the `code graph` pstree style. `↑` marks a DAG
    node already shown, `⚠︎` a tainted one, `✗` a dangling dep id."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    roots = (data or {}).get("roots") or []
    if not roots:
        print("(no design decisions — `crib design add <title> <body>`)")
        return
    ascii_mode = getattr(args, "ascii", False)
    branch, last, vert, blank = _tree_glyphs(ascii_mode)
    arrow = ">" if ascii_mode else ("▸" if data.get("direction") == "deps" else "◂")
    taint = "!" if ascii_mode else _TAINT
    proj, store = data.get("project"), _store_of(args)

    def label(n: dict) -> str:
        if n.get("missing"):
            return _st.paint("error", f"{n.get('title', '')}  (dangling id)")
        flags = (
            (f" {taint}" if n.get("tainted") else "")
            + (f" {_PROPOSED}" if n.get("status") == "proposed" else "")
            + (" ↑" if n.get("repeat") else "")
        )
        state = (
            f"  [{_st.status('superseded')}]"
            if n.get("status") == "superseded"
            else f"  [{_st.status('proposed')}]"
            if n.get("status") == "proposed"
            else ""
        )
        return (f"{_st.paint('title', n.get('title', ''))}{state}"
                f"{_st.paint('warn', flags)}   "
                f"{_item_path(proj, n.get('relpath', ''), store)}")

    def render(node: dict, prefix: str) -> None:
        kids = node.get("children") or []
        for i, c in enumerate(kids):
            islast = i == len(kids) - 1
            conn = _st.paint("meta", f"{prefix}{last if islast else branch}{arrow}")
            print(f"{conn} {label(c)}")
            render(c, prefix + (blank if islast else vert))

    for root in roots:
        direction = data.get("direction", "")
        print(f"{label(root)}   {_st.paint('meta', f'[{direction}]')}")
        render(root, "")


def _emit_design_check(data: Any, args: Any) -> None:
    """Tainted decisions: the chain that explains each, WHAT changed and when, and
    the verb to run next — a flag with no prescribed follow-up is a dead end."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    rows = (data or {}).get("tainted") or []
    proposed = (data or {}).get("proposed") or []
    proj, store = (data or {}).get("project"), _store_of(args)
    for cyc in (data or {}).get("cycles") or []:
        print(_st.paint("error", f"✗ dependency CYCLE: {' → '.join(cyc)}"))
    for p in proposed:
        print(f"{_st.paint('status.proposed', _PROPOSED)} "
              f"{_st.paint('title', p.get('title', ''))}   "
              f"{_item_path(proj, p.get('relpath', ''), store)}")
        print(_st.paint("meta", f"    → {p.get('next', '')}"))
    if not rows:
        print(_st.paint("ok", f"✓ {data.get('designs', 0)} decision(s), none stale"))
        return
    for r in rows:
        print(f"{_st.paint('warn', _TAINT)} {_st.paint('title', r.get('title', ''))}   "
              f"{_item_path(proj, r.get('relpath', ''), store)}")
        for c in r.get("causes") or []:
            # the date is already inside `reason` when there is one — the bracket
            # carries only the kind, so the line doesn't say it twice
            print(_st.paint("warn",
                            f"    • [{c.get('change_kind', '?')}] {c.get('reason', '')}"))
        if not (r.get("causes") or []):
            for reason in r.get("reasons") or []:
                print(_st.paint("warn", f"    • {reason}"))
        for p in r.get("paths") or []:
            chain = " → ".join(p.get("chain") or [])
            if len(p.get("chain") or []) > 1:
                print(_st.paint("meta", f"      via {chain}"))
        print(_st.paint("meta", f"    → {r.get('next', '')}"))
    print("\n" + _st.paint(
        "warn",
        f"{len(rows)} of {data.get('designs', 0)} decision(s) need re-reading — "
        f"`crib design read <ref>`, then `crib design reaffirm <ref>` "
        f"(taint means a dep moved, not that the decision is wrong)"
    ))


def _emit_facet_hits(hits: Any, args: Any) -> None:
    """`design lookup` / `plan lookup`: a locator line per hit carrying the facet
    state that decides whether to trust it — status, taint, edge counts."""
    if getattr(args, "json", False):
        print(json.dumps(hits, indent=2, default=str))
        return
    if not hits:
        print("(no matches)")
        return
    for h in hits:
        loc = (
            f":{h.get('line_start')}-{h.get('line_end')}" if h.get("line_start") else ""
        )
        st = f"  [{_st.status(h['status'])}]" if h.get("status") else ""
        edges = _st.paint("meta", (
            f"  {h.get('deps', 0)}▸/{h.get('dependents', 0)}◂" if "deps" in h else ""
        ))
        stale = _st.paint("warn", f"  {_TAINT} stale") if h.get("tainted") else ""
        print(
            f"{_st.paint('score', '[{:.3f}]'.format(h.get('score', 0.0)))} "
            f"{_st.paint('title', h.get('title', ''))}{st}{edges}{stale}"
            f"   {_item_path(h.get('project'), h.get('relpath', ''), h.get('store'), loc)}"
        )
        first = (h.get("snippet") or "").splitlines()
        if first:
            print(f"    {first[0][:100]}")
    if any(h.get("tainted") for h in hits):
        print("\n" + _st.paint(
            "warn",
            f"{_TAINT} a stale decision is one whose ground moved and nobody "
            f"re-read — `crib design read <ref>` before relying on it"
        ))


def _emit_design_read(data: Any, args: Any) -> None:
    """A decision's dossier: header, taint, body, then the annotated edges — the
    `code dossier` layout, so the two read the same way."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    if not isinstance(data, dict):
        print(data)
        return
    state = (
        f"  [{_st.status('superseded')}]"
        if data.get("status") == "superseded"
        else f"  [{_st.paint('status.proposed', f'proposed {_PROPOSED}')}]"
        if data.get("status") == "proposed"
        else ""
    )
    flag = _st.paint("warn", f"  {_TAINT} STALE") if data.get("tainted") else ""
    proj = data.get("project")
    print(
        f"{_st.paint('title', data.get('title', ''))}{state}{flag}   "
        f"{_item_path(proj, data.get('relpath', ''), _store_of(args))}"
        f"   {_st.paint('meta', '(updated {})'.format(data.get('updated', '?')))}"
    )
    for c in data.get("causes") or []:
        print(_st.paint("warn", f"  • [{c.get('change_kind', '?')}] {c.get('reason', '')}"))
    for p in data.get("paths") or []:
        if len(p.get("chain") or []) > 1:
            print(_st.paint("meta", f"    via {' → '.join(p['chain'])}"))
    if data.get("next"):
        print(_st.paint("meta", f"  → {data['next']}"))
    print()
    _render_markdown((data.get("body") or "").strip() + "\n")
    _print_edge_rows(data, (("deps", "▸ builds on"), ("dependents", "◂ built on by")),
                     _store_of(args))
    # attribution: where this came FROM, and whether that passage still reads
    # the way it did (`changed`/`missing` are what taints it)
    sources = data.get("sources") or []
    if sources and isinstance(sources[0], dict):
        print("  § drawn from")
        for s in sources:
            mark = {"changed": _TAINT, "missing": "✗"}.get(s.get("state", ""), " ")
            print(f"    {mark} {s.get('label', '')}  [{s.get('state', '')}]")


def _print_edge_rows(data: dict, sections: tuple[tuple[str, str], ...],
                     store: str | None) -> None:
    """A design/plan read's annotated edges: per section, each row's title,
    status, flags and path (linked when it is a file of this facet)."""
    for label, arrow in sections:
        rows = data.get(label) or []
        if not rows:
            continue
        print(f"  {_st.paint('header', arrow)}")
        for r in rows:
            st = f"  [{_st.status(r['status'])}]" if r.get("status") else ""
            flags = _st.paint("warn", _facet_flags(r))
            print(
                f"     {_st.paint('title', r.get('title', ''))}{st}{flags}"
                f"   {_item_path(data.get('project'), r.get('relpath', ''), store)}"
            )


def _emit_design_list(data: Any, args: Any) -> None:
    """Flat decision table: taint, title, edge counts, relpath."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    for cyc in (data or {}).get("cycles") or []:
        print(_st.paint("error", f"✗ dependency CYCLE: {' → '.join(cyc)}"))
    rows = (data or {}).get("designs") or []
    if not rows:
        print(
            "(no decisions"
            + (
                " are stale — ✓)"
                if data.get("filtered")
                else " — `crib design add <title>`)"
            )
        )
        return
    for r in rows:
        mark = (
            _TAINT
            if r.get("tainted")
            else _PROPOSED
            if r.get("status") == "proposed"
            else " "
        )
        mark = _st.paint("warn" if r.get("tainted") else "status.proposed", mark)
        sup = f"  [{_st.status('superseded')}]" if r.get("status") == "superseded" else ""
        cited = f"   §{r['sources']}" if r.get("sources") else ""
        edges = _st.paint("meta", f"{r.get('deps', 0)}▸/{r.get('dependents', 0)}◂{cited}")
        print(
            f"{mark} {_st.paint('title', r.get('title', ''))}{sup}"
            f"   {edges}"
            f"   {_item_path(data.get('project'), r.get('relpath', ''), _store_of(args))}"
        )
    stale, proposed = data.get("tainted", 0), data.get("proposed", 0)
    tail = ""
    if not data.get("filtered"):
        tail = (f", {stale} stale — `crib design check`" if stale else "") + (
            f", {proposed} proposed {_PROPOSED} — `crib design promote <ref>`"
            if proposed
            else ""
        )
    print(f"\n{len(rows)} of {data.get('total', 0)} decision(s){tail}")


_PLAN_GLYPH = {"todo": "·", "in-progress": "▶", "done": "✓", "verified": "✓✓"}


_GROUP_HEADS = {
    "in-progress": "in progress",
    "ready": "ready",
    "blocked": "blocked",
    "done": "done",
}


def _emit_plan_list(data: Any, args: Any) -> None:
    """The plan as a WORKING SET: in-progress, then ready, then blocked (each
    naming what it waits on), then done — not a graph dump. `⊘` marks a
    derived-blocked item."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    for cyc in (data or {}).get("cycles") or []:
        print(_st.paint("error", f"✗ dependency CYCLE: {' → '.join(cyc)}"))
    items = (data or {}).get("items") or []
    if not items:
        print(
            "(nothing to do — `crib plan add <title>`"
            + (", `--all` shows finished items)" if data.get("hidden") else ")")
        )
        return
    group = None
    for it in items:
        if it.get("group") and it["group"] != group:
            group = it["group"]
            print("\n" + _st.paint("header", f"{_GROUP_HEADS.get(group, group)}:"))
        status = it.get("status", "")
        mark = "⊘" if it.get("blocked") else _PLAN_GLYPH.get(status, "·")
        mark = _st.paint("warn" if it.get("blocked") else f"status.{status}", f"{mark:2}")
        print(
            f"{mark} {_st.paint(f'status.{status}', f'{status:12}')} "
            f"{_st.paint('title', it.get('title', ''))}"
            f"   {_item_path(data.get('project'), it.get('relpath', ''), _store_of(args))}"
        )
        for b in it.get("blocked_by") or []:
            ref = b.get("ref") or b.get("title", "") if isinstance(b, dict) else b
            bst = f" ({b['status']})" if isinstance(b, dict) and b.get("status") else ""
            kind = f"{b['kind']} " if isinstance(b, dict) and b.get("kind") else ""
            print(_st.paint("meta", f"     ← waiting on {kind}{ref}{bst}"))
        if it.get("missing_deps"):
            print(_st.paint("error",
                           f"     ✗ missing dep(s): {', '.join(it['missing_deps'])}"))
        # a finished item whose source moved: reported, never re-opened
        for why in it.get("revisit") or []:
            print(_st.paint("warn", f"     {_TAINT} revisit: {why}"))
    for it in items:  # the per-item loop the ready ones carry
        if it.get("next"):
            print(f"\n→ {it['next']}")
            break
    if data.get("hidden"):
        print(f"\n({data['hidden']} finished item(s) hidden — --all to show)")


def _emit_plan_read(data: Any, args: Any) -> None:
    """A plan item's dossier: header + status, what it's blocked on, the body,
    then the annotated edges — the `design read` layout, adapted to the plan
    facet's derived blocking."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    if not isinstance(data, dict):
        print(data)
        return
    status = data.get("status", "")
    mark = "⊘" if data.get("blocked") else _PLAN_GLYPH.get(status, "·")
    mark = _st.paint("warn" if data.get("blocked") else f"status.{status}", mark)
    flag = _st.paint("warn", f"  {_TAINT} STALE") if data.get("tainted") else ""
    print(
        f"{mark} {_st.paint('title', data.get('title', ''))}  "
        f"[{_st.status(status)}]{flag}   "
        f"{_item_path(data.get('project'), data.get('relpath', ''), _store_of(args))}"
        f"   {_st.paint('meta', '(updated {})'.format(data.get('updated', '?')))}"
    )
    for b in data.get("blocked_by") or []:
        st = f" ({b['status']})" if isinstance(b, dict) and b.get("status") else ""
        kind = f"{b['kind']} " if isinstance(b, dict) and b.get("kind") else ""
        ref = b.get("ref") or b.get("title", "") if isinstance(b, dict) else b
        print(_st.paint("meta", f"  ← waiting on {kind}{ref}{st}"))
    if data.get("missing_deps"):
        print(_st.paint("error", f"  ✗ missing dep(s): {', '.join(data['missing_deps'])}"))
    for why in data.get("revisit") or []:
        print(_st.paint("warn", f"  {_TAINT} revisit: {why}"))
    if data.get("next"):
        print(_st.paint("meta", f"  → {data['next']}"))
    print()
    _render_markdown((data.get("body") or "").strip() + "\n")
    _print_edge_rows(data, (("deps", "▸ waits on"), ("dependents", "◂ blocks")),
                     _store_of(args))
    sources = data.get("sources") or []
    if sources and isinstance(sources[0], dict):
        print("  § drawn from")
        for s in sources:
            mk = {"changed": _TAINT, "missing": "✗"}.get(s.get("state", ""), " ")
            print(f"    {mk} {s.get('label', '')}  [{s.get('state', '')}]")


def _emit_design_import(data: Any, args: Any) -> None:
    """A doc prepared for extraction: the PROCEDURE first (it is the payload —
    the verb wrote nothing and ran no model), then the citable sections and
    whatever already draws on this doc."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    if not isinstance(data, dict):
        print(data)
        return
    path = str(data.get("path", ""))
    print(f"{_st.paint('path', data.get('relpath', ''), _st.file_link(path))}   "
          f"{_st.paint('meta', f'({path})')}\n")
    print(data.get("instruction", "").rstrip())
    existing = data.get("existing") or []
    if existing:
        print("\n" + _st.paint(
            "header",
            f"already drawing on this doc ({len(existing)}) — extend these "
            f"rather than forking:"
        ))
        for e in existing:
            print(f"  · {_st.paint('title', e.get('title', ''))}   "
                  f"{_st.paint('path', e.get('relpath', ''))}")
            for c in e.get("cites") or []:
                print(_st.paint("meta", f"      § {c}"))
    sections = data.get("sections") or []
    print("\n" + _st.paint(
        "header", f"sections ({len(sections)}) — cite the `source` string verbatim:"))
    for s in sections:
        print(f"  {_st.paint('meta', format(s.get('words', 0), '5') + 'w')}  "
              f"{_st.paint('path', s.get('source', ''))}")
        if s.get("preview"):
            print(_st.paint("meta", f"         {s['preview'][:96]}"))


def _emit_design_write(data: Any, args: Any) -> None:
    """Confirmation for the design/plan write verbs — and, crucially, the CAUSAL
    CONSEQUENCES the verb reports: what the write just tainted, or unblocked."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return
    if not isinstance(data, dict):
        print(data)
        return
    proj, store = data.get("project", ""), _store_of(args)

    def tainted(d: dict) -> str:
        return (_st.paint("warn", f"  {_TAINT} now tainted:") + " "
                + _st.paint("title", d.get("title", "")) + "  ("
                + _item_path(proj, d.get("relpath", ""), store) + ")")
    if data.get("removed") is not None:  # forget
        print(f"removed  {_st.paint('path', proj + '/' + data.get('relpath', ''))}")
        for d in data.get("dependents") or []:
            print(tainted(d))
        return
    if data.get("added", 0) > 1:  # a plan batch
        print(f"→ {_st.paint('name', proj)}: added {data['added']} item(s)")
        for it in data.get("items") or []:
            print(f"   · {_st.paint('title', it.get('title', ''))}   "
                  f"{_item_path(proj, it.get('relpath', ''), store)}")
        return
    rel = data.get("relpath", "")
    print(
        f"→ {_st.paint('path', proj + '/' + rel, _item_link(proj, rel, store))}  "
        f"{_st.paint('title', data.get('title', ''))}"
    )
    for key in ("status", "rank", "dep_title", "superseded_by"):
        if data.get(key):
            value = _st.status(data[key]) if key == "status" else data[key]
            print(f"  {_st.paint('meta', key + ':')} {value}")
    if data.get("deps"):
        print(f"  {_st.paint('meta', 'deps:')} {len(data['deps'])}")
    for w in data.get("warnings") or []:
        print(_st.paint("warn", f"  {_TAINT} {w}"))
    for d in data.get("tainted_dependents") or []:
        print(tainted(d))
    for d in data.get("newly_tainted") or []:
        print(tainted(d))
        for via in d.get("via") or []:
            print(_st.paint("meta", f"      via {via}"))
    for u in data.get("unblocked") or []:
        print(_st.paint("ok", "  ✓ unblocked:") + " "
              + _st.paint("title", u.get("title", "")) + "  ("
              + _st.paint("path", u.get("ref", "")) + ")")
    for s in data.get("similar") or []:
        print(
            _st.paint("warn", f"  {_TAINT} similar decision [{s['score']:.3f}]:") + " "
            + _st.paint("path", s["relpath"])
            + (f" — {_st.paint('heading', s['heading'])}" if s.get("heading") else "")
            + "  (append to it rather than forking the graph?)"
        )
    if data.get("missing"):
        print(_st.paint("error", f"  ✗ missing dep(s): {', '.join(data['missing'])}"))
    if data.get("next"):
        print(_st.paint("meta", f"  → {data['next']}"))


def _graph_direction(args: argparse.Namespace) -> str:
    """--references > --callers > default callees."""
    if getattr(args, "references", False):
        return "references"
    return "callers" if getattr(args, "callers", False) else "callees"


def _graph_symbol(args: argparse.Namespace) -> str | None:
    """`--all` (or a bare `code graph`) means the whole project — no root symbol."""
    if getattr(args, "all_symbols", False):
        return None
    if not getattr(args, "symbol", None):
        raise SystemExit(
            "crib code graph: give a SYMBOL, or --all for the whole project"
        )
    return str(args.symbol)


def _graph_shape(args: argparse.Namespace) -> str | None:
    """--edges / --group-by / --all all mean the edge list; otherwise the tree."""
    if (
        getattr(args, "edges", False)
        or getattr(args, "group_by", None)
        or getattr(args, "all_symbols", False)
    ):
        return "edges"
    return None


def _emit_resolved(data: dict) -> None:
    """State what a partial name became — only when it BECAME something, i.e. the
    caller did not type the qualified name. A choice made on the caller's behalf is
    always said out loud."""
    r = data.get("resolved") or {}
    ref = r.get("symbol_ref") or r.get("fqn")
    if ref and r.get("query") != ref:
        proj = f" in {r['project']}" if r.get("project") else ""
        print(_st.paint("meta", f"  ({r['query']!r} → {ref}{proj}, matched on {r.get('via')})"))


def _emit_code_edges(data: dict, args: argparse.Namespace) -> None:
    """Node table + edge list, with the two facts the tree could not state: which
    symbols CONVERGE (in-degree > 1) and where the walk stopped."""
    ascii_mode = getattr(args, "ascii", False)
    arrow = "->" if ascii_mode else "→"
    nodes, edges = data.get("nodes") or [], data.get("edges") or []
    scope = (
        f"{data['root']}  [{data['direction']}, depth {data['depth']}]"
        if data.get("scope") != "project"
        else f"{data.get('project', '')}  [all symbols, {data['direction']}]"
    )
    grouped = data.get("group_by") == "module"
    print(
        f"{_st.paint('name', scope)}  "
        + _st.paint("meta", f"{len(nodes)} {'modules' if grouped else 'nodes'}, "
                    f"{len(edges)} edges")
    )
    _emit_resolved(data)
    indeg: dict[str, int] = {}
    for e in edges:
        if e["from"] != e["to"]:
            indeg[e["to"]] = indeg.get(e["to"], 0) + 1
    print("\n" + _st.paint("header", "nodes:"))
    for n in nodes:
        d = f"{n['depth']}  " if "depth" in n else ""
        extra = f"   ×{n['symbols']}" if grouped else ""
        print(
            f"  {_st.paint('meta', d)}{_st.paint('name', _sym_name(n))}"
            f"{_st.paint('meta', extra)}{_sym_loc(n, '   ')}"
            f"{_st.paint('meta', _sym_tags(n, ascii_mode))}"
        )
    print("\n" + _st.paint("header", "edges:"))
    for e in edges:
        w = _st.paint("meta", f"  ×{e['weight']}") if "weight" in e else ""
        print(f"  {_st.paint('name', e['from'])} {_st.paint('meta', arrow)} "
              f"{_st.paint('name', e['to'])}{w}")
    conv = sorted(((c, i) for i, c in indeg.items() if c > 1), reverse=True)
    if conv:
        print("\n" + _st.paint("header", "convergence (paths landing on one node):"))
        for c, i in conv:
            print(f"  {_st.paint('warn', f'{c}×')}  {_st.paint('name', i)}")


def _emit_code_graph(tree: Any, args: Any) -> None:
    """pstree-style call graph (modeled on zdot's hook graph). `--json` = raw tree.
    `↑` marks a DAG node already shown; `·ext` an edge target outside the index."""
    if getattr(args, "json", False):
        print(json.dumps(tree, indent=2, default=str))
        return
    if not tree:
        print("(symbol not found — is this project code-indexed?)")
        return
    if tree.get("shape") == "edges":
        _emit_code_edges(tree, args)
        return
    direction = _graph_direction(args)
    ascii_mode = getattr(args, "ascii", False)
    arrows = {"callees": (">", "▸"), "callers": ("<", "◂"), "references": ("=", "⇐")}
    arrow = arrows[direction][0 if ascii_mode else 1]
    branch, last, vert, blank = _tree_glyphs(ascii_mode)
    print(
        f"{render_symbol(tree, 'title')}   "
        + _st.paint("meta", f"[{direction}]{_sym_tags(tree, ascii_mode)}")
    )
    _emit_resolved(tree)

    def render(node: dict, prefix: str) -> None:
        kids = node.get("children") or []
        for i, c in enumerate(kids):
            islast = i == len(kids) - 1
            conn = _st.paint("meta", f"{prefix}{last if islast else branch}{arrow}")
            print(f"{conn} {render_symbol(c, 'tree', ascii_mode)}")
            render(c, prefix + (blank if islast else vert))

    render(tree, "")


if TYPE_CHECKING:
    # The daemon client's progress-callback Protocol — structural, fastmcp-free;
    # imported for annotation only so the module stays importable without it.
    from .client import ProgressHandler


def _narrowers(sp: Any) -> None:
    """The axes a caller can narrow an ambiguous symbol on, shared by the verbs that
    resolve one — declared once so they cannot drift apart."""
    sp.add_argument(
        "--path",
        default="",
        help="a trailing run of the file path (state.rs, core/state.rs)",
    )
    sp.add_argument(
        "--scope",
        default="",
        help="a trailing run of the language's own scope (ServerState)",
    )
    sp.add_argument("--lang", default="", help="exact language of the symbol")


def _dep_kind_flags(s: Any) -> None:
    """--follows / --rests-on / --informed-by: a dependency with its kind chosen
    explicitly, overriding the default for its target (repeatable)."""
    for flag, dest, what in (
        ("--follows", "follows", "ordering: blocked until it is done"),
        ("--rests-on", "rests_on", "grounding: blocked while it is stale"),
        ("--informed-by", "informed_by", "context: checked, never blocks"),
    ):
        s.add_argument(flag, action="append", dest=dest, metavar="REF",
                       help=f"a dependency of kind {dest} ({what}; repeatable)")


def _dep_kind_arg(s: Any) -> None:
    s.add_argument("--as", dest="kind", default=None,
                   choices=("follows", "rests_on", "informed_by"),
                   help="the dependency's kind (default: by what it points at)")


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    p = argparse.ArgumentParser(prog="crib", description="markdown memory")
    p.add_argument("--version", action="version", version=f"crib {__version__}")
    p.add_argument("--mcp", action="store_true", help="run the MCP server")
    p.add_argument(
        "--tools",
        default=None,
        help="comma-separated tool SETS to expose on this deployment "
        "(notes,designs,plans,code,learnings,projects,memory; `system` — "
        "status/ops/reconcile — always rides); default: all. Overrides "
        "config.toml [tools].expose",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--root",
        metavar="DIR",
        help="CONFINED MODE: keep everything (config, store, index, notes) under "
        "DIR and write nothing outside it; no git sync, no shared store, "
        "watchers only inside DIR, a checkout's own .crib never read or "
        "written (projects are named). Same as $CRIB_ROOT",
    )
    p.add_argument(
        "--store",
        metavar="DIR",
        help="put the STORE (index dir: the DuckDB file and caches) at DIR; with "
        "--root it is the one thing kept outside the root (a disposable cache, "
        "rebuilt from the root's files). Same as $CRIB_INDEX_DIR",
    )
    p.add_argument(
        "--writable",
        action="append",
        metavar="PROJECT",
        help="only these projects are writable for THIS process (repeatable; "
        "everything else is read-only); for `serve`/--mcp and --no-daemon "
        "verbs. A daemon-attached call follows the daemon's own setting. "
        "Same as $CRIB_WRITABLE or `writable = [...]` in config.toml",
    )
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="style human output by role: auto (a colour terminal; honours "
        "NO_COLOR / FORCE_COLOR), always, or never",
    )
    # transport options (apply to --mcp and `serve`; also pick the daemon the CLI
    # attaches to). Default to None so config `[daemon]` (host/port) wins unless
    # the user overrides on the command line.
    p.add_argument(
        "--http", action="store_true", help="serve MCP over HTTP instead of stdio"
    )
    p.add_argument("--host", default=None, help="HTTP host (bind, or daemon to attach)")
    p.add_argument(
        "--port", type=int, default=None, help="HTTP port (bind, or daemon to attach)"
    )
    # CLI verbs attach to the warm daemon by default; --no-daemon runs in-process.
    p.add_argument(
        "--no-daemon",
        action="store_true",
        help="run the verb in-process instead of via the daemon",
    )
    sub = p.add_subparsers(dest="cmd")

    def proj(sp):  # shared project selectors
        sp.add_argument("-p", "--project")  # by NAME
        sp.add_argument(
            "-P",
            "--project-path",  # by PATH (resolve .crib from here
            dest="project_path",
        )  # instead of the actual cwd)

    sv = sub.add_parser("serve", help="run the MCP server (stdio or --http)")
    sv.add_argument("--http", action="store_true")
    sv.add_argument("--tools", dest="tools", default=None)  # same set as --mcp
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sub.add_parser("info", help="show resolved paths and available backends")
    sub.add_parser(
        "status",
        help="health summary: projects (notes/docs/code/"
        "learnings), git sync, LSP sessions, indexing",
    )
    sub.add_parser(
        "ops",
        help="live ops: running sweeps (done/total/failed), in-flight files, "
        "generation slot occupancy (in-flight/waiting/429s/cooldown)",
    )

    # `crib project <verb>` — whole-project lifecycle (superset of code + notes)
    pj = sub.add_parser(
        "project", help="onboard/index a whole repo (setup/index/status/forget)"
    )
    pjsub = pj.add_subparsers(dest="project_verb")
    for _v, _h in (
        ("setup", "ensure .crib + import docs + index all code"),
        ("index", "(re)index the repo's code + in-situ docs from its .crib"),
        ("status", "is it indexed? counts, kinds, .crib paths"),
        ("doctor", "what needs attention (report-only), with the fix for each"),
        ("wait", "bounded-block on a running index job (join primitive)"),
        ("cancel", "cancel a running index sweep"),
    ):
        _sp = pjsub.add_parser(_v, help=_h)
        if _v in ("setup", "index"):
            _sp.add_argument(
                "--budget",
                type=float,
                default=None,
                dest="budget_s",
                help="soft deadline; unfinished files are deferred",
            )
        if _v == "index":
            _sp.add_argument(
                "--phased", action=argparse.BooleanOptionalAction, default=None,
                help="index in phases under the one --budget: changed files' "
                "structure, then edges re-resolved (dangling calls reported), "
                "then descriptions (default: on with --root)",
            )
        if _v == "wait":
            # the polling contract: `project index` returns a running handle and
            # the CLI emitter follows it to completion — no --wait-s there; the
            # BLOCKING join is wait's own verb (scripts/terminals).
            _sp.add_argument(
                "--wait-s",
                type=float,
                default=None,
                dest="wait_s",
                help="bound this WAIT (the sweep keeps running "
                "daemon-side); re-invoke to re-join",
            )
        proj(_sp)
    for _v, _h in (
        (
            "adopt",
            "move this project's notes INTO the repo (needs `store:` in its .crib)",
        ),
        ("release", "move an adopted project's notes back to the global store"),
        (
            "migrate",
            "move legacy notes/{design,plans,code-learnings} "
            "into the sibling pillar stores (idempotent; every "
            "full reindex also self-heals)",
        ),
    ):
        _sp = pjsub.add_parser(_v, help=_h)
        proj(_sp)
    _sp = pjsub.add_parser(
        "create",
        help="explicitly create a project (skeleton + birth manifest; "
        "--store <rel> declares in-repo residency at birth)",
    )
    _sp.add_argument("project", nargs="?", help="the new project's name")
    _sp.add_argument(
        "--store",
        metavar="RELPATH",
        help="create the project IN-REPO at this repo-relative store path "
        "(writes .crib + the machine stub; anchor with -P/--project-path)",
    )
    _sp.add_argument(
        "--transient", action="store_true",
        help="a project for one job: never synced, no version ring, deleted "
        "without .trash (and by the daemon once --expires passes)",
    )
    _sp.add_argument(
        "--expires", metavar="WHEN",
        help="with --transient: an ISO time, or a duration like 30m / 2h / 1d",
    )
    # -P anchors --store at a repo, not the project; -p NAMES it, same fact as
    # the positional (separate dest, merged + conflict-guarded in the build).
    _sp.add_argument(
        "-P",
        "--project-path",
        dest="project_path",
        help="anchor --store at this repo (needed with --store; never the "
        "daemon's cwd)",
    )
    _sp.add_argument(
        "-p",
        "--project",
        dest="project_flag",
        default=None,
        help="the new project's name (selector spelling)",
    )
    _sp = pjsub.add_parser(
        "delete",
        help="fenced removal: moves the tree to .trash (default "
        "project, running sweeps and in-repo stores are fenced)",
    )
    _sp.add_argument(
        "project",
        nargs="?",
        help="the project to delete (explicit; positional or -p; never the "
        "session default)",
    )
    _sp.add_argument(
        "-p",
        "--project",
        dest="project_flag",
        default=None,
        help="the project to delete (selector spelling, same fact)",
    )
    _sp.add_argument(
        "--force",
        action="store_true",
        help="accepted for compatibility; NO-OP — delete never removes "
        "anything from a user repo (its notes stay; release them first if "
        "they should follow the namespace)",
    )
    _sp.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation (for scripts; piped stdin "
        "refuses without it)",
    )
    _sp = pjsub.add_parser(
        "rename",
        help="rename a project namespace (chunk ids churn → full re-embed; "
        "note ids and ring history survive)",
    )
    _sp.add_argument("project", help="the current name")
    _sp.add_argument("to", help="the new name")
    # NO -p/-P selectors: both positionals name the namespace, and rename never
    # anchors at a repo (the -p clash with the positional would be the same
    # order-dependence create had).
    _sp = pjsub.add_parser(
        "relocate",
        help="the repo moved and/or was renamed: re-point code index, "
        "doc-sources and its own .crib at the new path — identity kept; "
        "the sources/<repo>/ mount prefix re-homes by default",
    )
    _sp.add_argument("project", help="the (current) project name")
    _sp.add_argument(
        "-P",
        "--project-path",
        dest="project_path",
        help="the repo's NEW dir (a moved repo carries its own .crib)",
    )
    _sp.add_argument(
        "--source",
        help="the mount segment to re-point, when several in-situ sources are dead",
    )
    _sp.add_argument(
        "--keep-prefix",
        action="store_true",
        dest="keep_prefix",
        help="identity-keeping only: do NOT re-home the sources/<repo>/ mount "
        "prefix (citations keep spelling the old dir name; the docs rehash "
        "is skipped so no double-mount)",
    )
    _pf = pjsub.add_parser(
        "forget", help="clear the code index (keeps learnings/notes)"
    )
    proj(_pf)
    _pf.add_argument(
        "--with-learnings",
        action="store_true",
        help="also drop attached learnings (default: keep them)",
    )
    _sp = pjsub.add_parser(
        "lookup", help="one search across notes, decisions, plans and learnings"
    )
    _sp.add_argument("query")
    _sp.add_argument("-k", type=int, default=8)
    _sp.add_argument("--kind", action="append", dest="kinds",
                     help="note/design/plan/learning (repeatable)")
    _sp.add_argument("--status", action="append",
                     help="only decisions/plans in this status (repeatable)")
    proj(_sp)
    _sp = pjsub.add_parser(
        "items", help="every item across facets, no query (notes, decisions, plans ...)"
    )
    _sp.add_argument("-k", type=int, default=200)
    _sp.add_argument("--kind", action="append", dest="kinds",
                     help="only this facet: note/design/plan/learning/... (repeatable)")
    _sp.add_argument("--tag", action="append", dest="tags",
                     help="only items with this tag (repeatable: any)")
    proj(_sp)
    _sp = pjsub.add_parser(
        "related",
        help="one item and what surrounds it: links both ways, nearest of every kind",
    )
    _sp.add_argument("ref", help="an id, relpath, decision/plan title, or symbol")
    _sp.add_argument("-k", type=int, default=3, help="nearest items per kind")
    proj(_sp)
    pjsub.add_parser("list", help="list projects (separate memory namespaces)")
    pjsub.add_parser("reconcile", help="sweep all projects for offline changes")

    # noun groups mirroring `project`: note / code / learning (verbs nest under them)
    n_note = sub.add_parser("note", help="memory notes: search, read, write, share")
    notesub = n_note.add_subparsers(dest="note_verb", required=True)
    n_code = sub.add_parser("code", help="code symbol index: search + navigate")
    codesub = n_code.add_subparsers(dest="code_verb", required=True)
    n_learn = sub.add_parser(
        "learning", help="durable learnings attached to code symbols"
    )
    learnsub = n_learn.add_subparsers(dest="learning_verb", required=True)
    # Every generated facet's subcommands (notes' and learnings' under their
    # nouns, a declared facet's under its own): the CLI half of crib.verbs'
    # templates (_FACET_CLI, _ANCHORED_CLI).

    s = notesub.add_parser(
        "import-memory", help="mirror Claude Code's harness memory into a crib project"
    )
    proj(s)

    s = codesub.add_parser(
        "lookup", help="find a code symbol by concept OR name (hybrid dense+kw)"
    )
    s.add_argument("query")
    proj(s)
    s.add_argument("-k", type=int, default=8)

    s = codesub.add_parser(
        "xref", help="a symbol's callers/callees/references from the symbol_index"
    )
    s.add_argument("symbol")
    proj(s)
    _narrowers(s)

    s = codesub.add_parser(
        "dossier", help="everything about one symbol (+ neighbour descriptions)"
    )
    s.add_argument("symbol")
    proj(s)
    _narrowers(s)

    s = codesub.add_parser(
        "graph",
        help="call graph around a symbol — pstree, edge list, or whole-project export",
    )
    s.add_argument("symbol", nargs="?", help="omit with --all for the whole project")
    proj(s)
    s.add_argument(
        "--callers",
        action="store_true",
        help="what CALLS the symbol (default: what it calls)",
    )
    s.add_argument(
        "--references",
        action="store_true",
        help="everywhere the symbol is REFERENCED (broader than calls)",
    )
    s.add_argument("--depth", type=int, default=6)
    s.add_argument("--ascii", action="store_true", help="ASCII glyphs, no box-drawing")
    s.add_argument(
        "--edges",
        action="store_true",
        help="deduplicated node+edge subgraph instead of a tree "
        "(convergence stays visible; feeds a layout tool)",
    )
    s.add_argument(
        "--group-by",
        choices=list(GRAPH_GROUPINGS),
        default=None,
        dest="group_by",
        help="roll symbol edges up onto one axis: file, dir or scope",
    )
    s.add_argument(
        "--group-depth",
        type=int,
        default=0,
        dest="group_depth",
        help="how coarse: dirs/scope segments to keep (0 = all)",
    )
    s.add_argument(
        "--all",
        action="store_true",
        dest="all_symbols",
        help="the WHOLE project: every symbol, every edge, no depth bound",
    )
    s.add_argument(
        "--under",
        default="",
        help="whole-project only: export just the files under this path "
        "prefix (boundary-crossing edges keep truncated frontier "
        "nodes)",
    )
    s.add_argument(
        "--exclude",
        default="",
        help="whole-project only: drop files under this path prefix before export",
    )
    _narrowers(s)

    s = codesub.add_parser(
        "index", help="index a source file: symbols + call graph + descriptions"
    )
    s.add_argument("path")
    proj(s)

    s = codesub.add_parser(
        "convert",
        help="convert the symbol store to the current shape in place "
        "(no LSP/LLM; dry run without --apply)",
    )
    s.add_argument("--apply", action="store_true")
    proj(s)

    # `crib design <verb>` / `crib plan <verb>` — decisions and work items, both
    # notes with a dependency graph (crib/designs.py). The FACET is the interface:
    # read/edit/lookup have their own verbs here, because only they speak edges.
    # Bare `crib design` / `crib plan` mean `list`, as bare `crib project` means
    # `status` — the orienting read is what you want when you type just the noun.
    n_design = sub.add_parser(
        "design", help="design decisions + their dependency graph"
    )
    designsub = n_design.add_subparsers(dest="design_verb")
    n_plan = sub.add_parser("plan", help="persistent, resumable plan items")
    plansub = n_plan.add_subparsers(dest="plan_verb")
    # Every generated facet's subcommands (notes', learnings', decisions' and
    # plans' under their nouns, a declared facet's under its own): the CLI half
    # of crib.verbs' templates (_FACET_CLI, _ANCHORED_CLI, _GRAPH_CLI).
    _facet_parsers(sub, {"note": notesub, "learning": learnsub,
                         "design": designsub, "plan": plansub}, proj)

    # `crib memory <verb>` — the whole memory store's git lifecycle. These act on
    # the entire data tree (every project's notes + learnings), not a note or a
    # project, so they live under their own top-level noun, over `GitBacking`.
    n_memory = sub.add_parser(
        "memory", help="the memory store's git lifecycle: snapshot + sync"
    )
    memsub = n_memory.add_subparsers(dest="memory_verb", required=True)

    s = memsub.add_parser("snapshot", help="git checkpoint of the whole data tree")
    s.add_argument("-m", "--message")

    s = memsub.add_parser(
        "setup",
        help="join the shared memory repo on this machine "
        "(set remote + frontmatter merge driver, then pull)",
    )
    s.add_argument("--remote", help="git remote URL to join (prompted if omitted)")

    s = memsub.add_parser(
        "sync", help="share memory via git: commit + pull + push, then reindex"
    )
    s.add_argument("-m", "--message")
    s.add_argument("--remote", help="bootstrap: git init + set origin to this URL")
    memsub.add_parser("push", help="push local commits to the remote")
    memsub.add_parser("pull", help="pull from the remote, then reindex")

    s = memsub.add_parser("history", help="git history for the tree (or a note)")
    s.add_argument("relpath", nargs="?")

    # internal: invoked by git as the cribnote merge driver (DESIGN §14). No
    # help= → kept out of the listed commands (still a valid hidden subcommand).
    n_bundle = sub.add_parser(
        "bundle",
        help="self-contained, disposable crib roots handed to a sandbox "
        "(open one with `crib --root DIR`)",
    )
    bsub = n_bundle.add_subparsers(dest="bundle_cmd", required=True)
    _bc = bsub.add_parser(
        "create", help="write a bundle: a writable overlay copy + read-only context")
    _bc.add_argument("dir", type=Path, help="the bundle directory (new or empty)")
    _bc.add_argument("--overlay", required=True, metavar="BASE:NAME",
                     help="copy project BASE as the writable project BASE@NAME")
    _bc.add_argument("--context", action="append", default=[], metavar="PROJECT",
                     help="a project to include read-only (repeatable)")
    _bc.add_argument("--context-to", type=Path, metavar="DIR",
                     help="where the context copies go (default DIR/context); "
                     "the sandbox mounts it read-only")
    _bc.add_argument("--context-mount", metavar="PATH",
                     help="where --context-to appears inside the sandbox")
    _bc.add_argument("--expires", default="7d", metavar="WHEN",
                     help="ISO time or duration (30m/2h/7d); default 7d")
    _bc.add_argument("--no-vectors", dest="vectors", action="store_false",
                     help="skip building the bundle's store and vector file "
                     "(a sandbox then re-embeds with the model in its image)")
    _be = bsub.add_parser("expire", help="set a bundle's expiry (also inside it)")
    _be.add_argument("dir", type=Path, nargs="?",
                     help="the bundle (default: the --root you're confined to)")
    _be.add_argument("--expires", required=True, metavar="WHEN")
    bsub.add_parser("list", help="registered bundles and their expiry")
    _bd = bsub.add_parser("delete", help="remove a registered bundle now")
    _bd.add_argument("dir", type=Path)
    n_facets = sub.add_parser(
        "facets", help="the facet registry: show it, conform the store to it"
    )
    facetsub = n_facets.add_subparsers(dest="facets_cmd", required=True)
    _fsh = facetsub.add_parser(
        "show", aliases=["list"],
        help="every facet (built-in and declared) and its state in this store"
    )
    _fsh.add_argument("--toml", action="store_true",
                      help="print the effective registry as a facets file instead")
    _fi = facetsub.add_parser(
        "init", help="write a starter system facets file (examples, all commented)"
    )
    _fi.add_argument("--force", action="store_true",
                     help="overwrite an existing facets file")
    facetsub.add_parser(
        "instructions",
        help="the agent-facing guidance on this machine's declared facets "
        "(markdown; nothing without any) — what the plugins add to their directive",
    )
    _fs = facetsub.add_parser(
        "sync",
        help="conform the store to the declared facets (a plan unless --apply)",
    )
    _fs.add_argument("--apply", action="store_true",
                     help="carry the plan out; without it nothing is changed")
    _fs.add_argument("--drop", action="store_true",
                     help="also drop the facets gone from the facets file (their "
                     "index rows; your files are never touched)")
    _fs.add_argument("--shared", action="store_true",
                     help="on a shared store (the crib DuckDB server): confirm the "
                     "change is meant for every machine using it")
    n_gen = sub.add_parser(
        "generate", help="set up and check LLM generation (descriptions, summaries)"
    )
    gensub = n_gen.add_subparsers(dest="generate_cmd", required=True)
    _gs = gensub.add_parser(
        "setup",
        help="detect the providers this machine has, pick one, write the config",
    )
    _gs.add_argument("--provider", help="use this detected provider (skips the prompt)")
    _gs.add_argument("--model", help="the model to use (skips the prompt)")
    _gs.add_argument("--mode", choices=("on", "explicit", "off"),
                     help="when crib may generate (see [generate] mode)")
    _gs.add_argument("--yes", action="store_true",
                     help="write without asking (defaults for anything not given)")
    gensub.add_parser("test", help="one small call per purpose, to check the setup")

    n_store = sub.add_parser("store", help="the vector store (DuckDB)")
    storesub = n_store.add_subparsers(dest="store_cmd", required=True)
    storesub.add_parser(
        "install-extensions",
        help="download the shared-mode DuckDB extensions now (containers, offline)",
    )

    s = sub.add_parser("merge-driver")
    s.add_argument("base")  # %O ancestor
    s.add_argument("current")  # %A ours / output file
    s.add_argument("other")  # %B theirs
    s.add_argument("pathname", nargs="?")  # %P (informational)

    return p


def cmd_info(as_json: bool) -> None:
    import importlib.util
    import shutil

    from .config import Config
    from .paths import Paths

    paths = Paths.resolve()
    config = Config.load(paths.config_file)
    backends = {
        "duckdb": importlib.util.find_spec("duckdb") is not None,
        "fastembed": importlib.util.find_spec("fastembed") is not None,
        "sentence_transformers": importlib.util.find_spec("sentence_transformers")
        is not None,
        "fastmcp": importlib.util.find_spec("fastmcp") is not None,
        "watchdog": importlib.util.find_spec("watchdog") is not None,
        "sharedserver": shutil.which("sharedserver") is not None,
    }
    d = config.daemon
    info = {
        "config_dir": str(paths.config_dir),
        "data_dir": str(paths.data_dir),
        "index_dir": str(paths.index_dir),
        "embed_model": config.embed.model,
        "chunk": {
            "window_words": config.chunk.window_words,
            "overlap_ratio": config.chunk.overlap_ratio,
            "overlap_words": config.chunk.overlap_words,
        },
        "retrieve": {
            "hybrid": config.retrieve.hybrid,
            "rerank": config.retrieve.rerank,
            "rerank_model": config.retrieve.rerank_model,
        },
        "store": config.store.backend
        + (f" ({config.duckdb.mode})" if config.store.backend == "duckdb" else ""),
        "default_project": config.default_project,
        "daemon": {
            "enabled": d.enabled,
            "name": d.name,
            "endpoint": f"http://{d.host}:{d.port}/mcp",
            "grace_period": d.grace_period,
        },
        "backends": backends,
    }
    if as_json:
        print(json.dumps(info, indent=2))
        return
    for k in (
        "config_dir",
        "data_dir",
        "index_dir",
        "embed_model",
        "store",
        "default_project",
    ):
        print(f"{k:18} {info[k]}")
    ck = config.chunk
    print(
        f"{'chunk':18} {ck.window_words}w window, "
        f"{ck.overlap_words}w overlap ({ck.overlap_ratio:.0%})"
    )
    rt = config.retrieve
    rr = f" + rerank ({rt.rerank_model.split('/')[-1]})" if rt.rerank else ""
    print(
        f"{'retrieve':18} {'hybrid (dense+BM25 score blend)' if rt.hybrid else 'dense only'}{rr}"
    )
    print(
        f"{'daemon':18} {'on' if d.enabled else 'off'}  "
        f"http://{d.host}:{d.port}/mcp  ({d.name}, grace {d.grace_period})"
    )
    print("backends:")
    for name, ok in backends.items():
        print(f"  {'✓' if ok else '✗'} {name}")


# ── Verb registry (one row per CLI verb) ──────────────────────────────────────
# Collapses what used to be three hand-maintained if-chains (the daemon arg-mapper,
# the in-process dispatcher, and the emitter switch) into a single table. The daemon
# and in-process paths share the SAME logical call dict (`build`) and emitter; they
# differ only in three mechanical ways the dispatchers apply: the daemon sends
# `project_path=<cwd-str>` + calls the MCP `tool`, while in-process sends `cwd=<Path>`
# + calls the Crib `method` (== tool unless overridden) and wraps async ones in
# `asyncio.run`. Content args read stdin here (client-side) via `build`, since the
# daemon has none. Special verbs (git, project, serve/info/merge-driver) are handled
# outside the registry; `search`/`a`/`lookup --render` normalize to a canonical verb.
#
# Each row also DECLARES its MCP twin's wire signature (`mcp`) and the server-side
# project-resolution policy (`policy`) it must carry, making this table the single
# source of truth for the whole surface: tests/test_surface_parity.py walks it
# against FastMCP's introspected schemas, so a param, a default (the CLI/MCP
# `apropos k` split was 5 vs 8) or a policy can't drift on one face only.
def _E_export(d: Any, a: Any) -> None:
    """`design/plan export`: --out writes the file (prints a summary), else the
    markdown goes to stdout verbatim. `--json` always emits the full dict (the
    markdown is inside it) for scripted consumption.

    Two shapes arrive here, on purpose: the DAEMON path returns the MCP tool's
    bare markdown string, the in-process path returns the full dict. Handle both
    — the summary line (count/tainted) is best-effort and only exists for the
    dict shape."""
    if isinstance(d, str):  # daemon path: the MCP tool returns the markdown
        if a.json:
            _emit({"markdown": d}, True)
            return
        if a.out:
            a.out.write_text(d)
            print(f"wrote {_st.paint('path', a.out, _st.file_link(Path(a.out).resolve()))} "
                  f"({len(d.splitlines())} lines)")
        else:
            print(d, end="")
        return
    if a.json:
        _emit(d, True)
        return
    md = d["markdown"]
    if a.out:
        a.out.write_text(md)
        print(
            f"wrote {_st.paint('path', a.out, _st.file_link(Path(a.out).resolve()))} — "
            f"{d['count']} {d['facet']}s, "
            f"{d['tainted']} tainted, {len(md.splitlines())} lines"
        )
    else:
        print(md, end="")


@dataclass(frozen=True)
class Verb:
    tool: str  # MCP tool name (daemon path)
    build: Callable[[Any], dict[str, Any]]  # parsed args → logical call params
    emit: Callable[[Any, Any], None]  # (result, parsed args) → stdout
    method: str = ""  # Crib method (in-process); "" ⇒ tool
    is_async: bool = False  # in-process wraps in asyncio.run
    wants_cwd: bool = True  # append project_path / cwd
    mcp: str | None = None  # MCP params: "query k=8 …" ("" = none)
    policy: str = ""  # server.TOOL_POLICY declaration
    # Extra kwargs for the in-process Crib method only (a generated facet's own
    # `facet=`); the daemon path calls the facet's own tool instead.
    inproc: tuple[tuple[str, Any], ...] = ()
    # A declared facet's field parameters: top-level on the MCP tool, folded
    # into the Crib method's `fields` dict on the in-process path.
    fields: tuple[str, ...] = ()

    def crib_method(self) -> str:
        return self.method or self.tool

    def mcp_params(self) -> dict[str, Any]:
        """The declared `mcp` signature as {param: default}, `...` when required —
        the shape `tests/test_surface_parity.py` compares against the tool schema."""
        out: dict[str, Any] = {}
        for token in (self.mcp or "").split():
            name, eq, default = token.partition("=")
            out[name] = literal_eval(default) if eq else ...
        return out


# emit adapters — normalize every emitter to the same (data, args) signature
def _E(d, a):
    _emit(d, a.json)  # generic


def _E_raw(d, a):
    print(d)  # verbatim (locate/snapshot)


def _E_note(d, a):
    _print_note(d, a.json)


def _E_apropos(d, a):
    _emit_apropos(d, a.json)


def _emit_daemon(d: dict[str, Any]) -> None:
    """status's daemon and generation state, one line per thing that is going on."""
    dm = d.get("daemon") or {}
    if dm:
        log = f"  log {dm['log_file']}" if dm.get("log_file") else ""
        print(f"{_lab('daemon')} up {_secs(dm.get('uptime_s', 0))}{log}")
    for j in dm.get("background") or []:
        detail = f"  {j['detail']}" if j.get("detail") else ""
        print(f"{_lab('running')} {j['job']} for {_secs(j.get('running_s', 0))}{detail}")
    queued = {k: v for k, v in (dm.get("queues") or {}).items() if v}
    if queued:
        print(f"{_lab('queued')} " + ", ".join(f"{k.replace('_', ' ')} {v}"
                                             for k, v in queued.items()))
    notes = (dm.get("watchers") or {}).get("notes") or {}
    if notes.get("state") and notes["state"] != "healthy":
        lat = (f" ({notes['last_latency_s']}s)"
               if notes.get("last_latency_s") is not None else "")
        print(f"{_lab('watching')} {notes['state']}{lat}: reads still refresh; a "
              f"catch-up sweep runs while degraded")
    gen = d.get("generation") or {}
    if gen:
        pending = gen.get("pending_descriptions") or {}
        waiting = (": descriptions waiting in "
                   + ", ".join(f"{p} {n}" for p, n in pending.items())
                   if pending else "")
        print(f"{_lab('generate')} {gen.get('mode', '?')}{waiting}")


def _secs(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else (
        f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s")


def _E_status(d, a):
    _emit_status(d, a.json)


def _E_dossier(d, a):
    _emit_code_dossier(d, a.json)


def _E_report(d, a):
    _emit_code_report(d, a.json)


def _E_rehome(d, a):
    _emit_code_rehome(d, a.json)


def _E_graph(d, a):
    _emit_code_graph(d, a)


def _E_dtree(d, a):
    _emit_design_tree(d, a)


def _E_dcheck(d, a):
    _emit_design_check(d, a)


def _E_dread(d, a):
    _emit_design_read(d, a)


def _E_dlist(d, a):
    _emit_design_list(d, a)


def _E_facet(d, a):
    _emit_facet_hits(d, a)


def _E_plans(d, a):
    _emit_plan_list(d, a)


def _E_pread(d, a):
    _emit_plan_read(d, a)


def _E_dwrite(d, a):
    _emit_design_write(d, a)


def _E_dimport(d, a):
    _emit_design_import(d, a)


def _E_projects(d, a):
    _emit_projects(d, a.json)


def _E_code(verb):
    return lambda d, a: _emit_code(d, verb, a.json)


def _E_learning(verb):
    return lambda d, a: _emit_code_learning(d, verb, a.json)


def _emit_doctor(d: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(d, indent=2, default=str))
        return
    if d.get("healthy"):
        print(f"{_st.paint('name', d['project'])}: "
              + _st.paint("ok", "nothing needs attention"))
        return
    print(f"{_st.paint('name', d['project'])}: "
          + _st.paint("warn", ", ".join(f"{n} {c}" for c, n in d["counts"].items())))
    for f in d["findings"]:
        what = (f.get("item") or " ~ ".join(f.get("items", []))
                or ", ".join(f.get("files", [])) or str(f.get("count", "")))
        extra = f.get("dep") or f.get("source") or f.get("symbol") or ""
        sim = f" ({f['similarity']})" if "similarity" in f else ""
        print(f"  - {_st.paint('warn', f['check'])}: {what}"
              f"{' -> ' + extra if extra else ''}{_st.paint('meta', sim)}")
        print(f"      {_st.paint('meta', 'fix:')} {f['fix']}")


def _emit_lookup_all(rows: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, default=str))
        return
    for r in rows:
        st = f" [{_st.status(r['status'])}]" if r.get("status") else ""
        where = _item_path(r.get("project"), r["relpath"], r.get("store")) + (
            f" :: {_st.paint('heading', r['heading'])}" if r.get("heading") else "")
        print(f"{_st.paint('score', format(r['score'], '.3f'))}  "
              f"{_st.paint('kind', format(r['kind'], '<8'))} "
              f"{_st.paint('title', r.get('title') or '')}{st}")
        print(f"         {where}")


def _emit_related(d: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(d, indent=2, default=str))
        return

    def line(r: dict[str, Any]) -> str:
        st = f" [{_st.status(r['status'])}]" if r.get("status") else ""
        what = _st.paint("title", r.get("title") or r.get("ref", ""))
        return f"{_st.paint('kind', r.get('kind', '?') + ':')} {what}{st}"

    it = d["item"]
    print(f"{line(it)}  "
          f"({_item_path(it.get('project') or d.get('project'), it['relpath'], it.get('store'))})")
    for label, rows in (("links out", d["links_out"]), ("links in", d["links_in"])):
        if rows:
            print(f"  {_st.paint('header', label + ':')}")
            for r in rows:
                print(f"    {_st.paint('meta', format(r['link'], '<14'))} {line(r)}")
    for kind, rows in d["similar"].items():
        print(f"  {_st.paint('header', f'nearest {kind}s:')}")
        for r in rows:
            print(f"    {_st.paint('score', format(r['similarity'], '.3f'))}  {line(r)}")


def _E_project(verb):
    return lambda d, a: _emit_project(d, verb, a.json)


def _b_lookup(a: Any) -> dict[str, Any]:
    """`lookup` call params — the keyword/summary label + weight overrides fold in
    only when given (absent ⇒ the method/[retrieve] default applies).

    Gated on `is not None`, NOT truthiness: `--keywords ""` is an explicit "no
    labels" that must reach `_split_labels` (→ `[]`) to turn a default-on index set
    OFF. A truthiness gate dropped it, so the flag silently fell back to the config
    default — the disable-semantics `_split_labels` documents were dead code."""
    call = {"query": a.query, "project": a.project, "k": a.k, "tags": a.tags}
    if getattr(a, "keywords", None) is not None:
        call["keyword_labels"] = _split_labels(a.keywords)
    if getattr(a, "keyword_weight", None) is not None:
        call["keyword_weight"] = a.keyword_weight
    if getattr(a, "summaries", None) is not None:
        call["summary_labels"] = _split_labels(a.summaries)
    if getattr(a, "summary_weight", None) is not None:
        call["summary_weight"] = a.summary_weight
    if getattr(a, "hints", None):  # absent ⇒ None; --hint accumulates
        call["hints"] = a.hints
    if getattr(a, "hint_weight", None) is not None:
        call["hint_weight"] = a.hint_weight
    if getattr(a, "rerank", None) is not None:
        call["rerank"] = a.rerank
    return call


def _plan_items(a: Any) -> list[dict[str, Any]] | None:
    """`crib plan add <title> [body] --item T --item T` → the batch form.

    Only built when `--item` is actually used; otherwise the call stays the plain
    single-item shape (so nothing about the common case changes). The extra items
    are title-only, which is the normal shape for a plan item anyway."""
    extra = getattr(a, "extra_items", None)
    if not extra:
        return None
    first = {
        "title": a.title,
        "deps": a.deps,
        "content": _body(a.content, a.file, default=""),
        "sources": getattr(a, "sources", None),
    }
    return [first, *({"title": t} for t in extra)]


def _proj_of(a: Any) -> str | None:
    """`-p/--project` when the sub-parser defines it — bare `crib project` selects no
    sub-verb, so its Namespace carries neither selector."""
    return getattr(a, "project", None)


_PROJ = "project=None project_path=None"  # the two selectors, on most tools


# ── generated facets' CLI ─────────────────────────────────────────────────────
# The CLI half of crib.verbs' templates, keyed by the same canonical verb names
# (tests/test_declared_facets.py checks the two halves cover the same verbs):
# per verb, the subcommand's help and arguments, and the VERBS row's call
# params, emitter, Crib method and MCP signature. The notes subcommands read
# exactly as their hand-written parsers did (tests/test_tool_surface_snapshot.py).


@dataclass(frozen=True)
class _FacetCli:
    help: Callable[[Any], str]  # (spec) -> the subcommand's one-line help
    args: Callable[[Any, Any, Any], None]  # (subparser, proj, spec)
    build: Callable[[Any], dict[str, Any]]
    emit: Callable[[Any, Any], None]
    method: str
    is_async: bool
    aliases: tuple[str, ...] = ()


def _home(f: Any) -> bool:
    from . import facets

    return f.noun == facets.HOME


def _a_lookup(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("query")
    proj(s)
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--tag", action="append", dest="tags")
    s.add_argument(
        "--keywords",
        help="comma-separated keyword_index labels to fold into BM25 "
        "for this query (overrides [retrieve].keyword_labels)",
    )
    s.add_argument(
        "--keyword-weight",
        type=float,
        default=None,
        dest="keyword_weight",
        help="weight of keyword_index tokens vs body in BM25 "
        "(overrides [retrieve].keyword_weight)",
    )
    s.add_argument(
        "--summaries",
        help="comma-separated summary_index labels to fold in as dense "
        "alias vectors (overrides [retrieve].summary_labels)",
    )
    s.add_argument(
        "--summary-weight",
        type=float,
        default=None,
        dest="summary_weight",
        help="trust scale of summary alias vectors vs body dense "
        "(overrides [retrieve].summary_weight)",
    )
    s.add_argument(
        "--hint",
        action="append",
        dest="hints",
        help="exact strings you already know (symbol, flag, error code, title) — "
        "each runs as its own exact-term route and boosts candidates multiple "
        "routes agree on; repeatable",
    )
    s.add_argument(
        "--hint-weight",
        type=float,
        default=None,
        dest="hint_weight",
        help="weight of the hint agreement bonus (overrides [retrieve].hint_weight)",
    )
    r = s.add_mutually_exclusive_group()
    r.add_argument(
        "--rerank",
        dest="rerank",
        action="store_true",
        default=None,
        help="force the cross-encoder rerank for this query",
    )
    r.add_argument(
        "--no-rerank",
        dest="rerank",
        action="store_false",
        help="skip the cross-encoder rerank for this query",
    )
    s.add_argument(
        "-a",
        "--render",
        action="store_true",
        help="render each matched section as markdown (like `apropos`) "
        "instead of compact locator lines",
    )


def _a_apropos(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("query")
    proj(s)
    # k matches `lookup`/the MCP tool/the Crib default (8) — `note lookup --render`
    # routes HERE, so a different default made the same rendered view return 5 or 8
    # hits depending on how you spelled it.
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--tag", action="append", dest="tags")
    s.add_argument(
        "--hint",
        action="append",
        dest="hints",
        help="exact strings you already know — each runs as its own "
        "exact-term route; repeatable",
    )


def _a_relpath(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("relpath")
    proj(s)


def _a_add(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("content")
    proj(s)
    s.add_argument("--title")
    s.add_argument("--tag", action="append", dest="tags")


def _a_append(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("relpath")
    s.add_argument("content")
    proj(s)
    s.add_argument("--heading")


def _a_edit(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("relpath")
    s.add_argument("content", nargs="?", default="-")
    proj(s)


def _a_move(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("relpath")
    proj(s)
    s.add_argument("--to-project", dest="to_project")
    s.add_argument("--to-relpath", dest="to_relpath")


def _a_reindex(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("relpath", nargs="?")
    proj(s)


def _a_restore(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("relpath")
    s.add_argument("version")
    proj(s)


def _a_import(s: Any, proj: Any, f: Any) -> None:
    s.add_argument(
        "paths", nargs="+",
        help="files to copy into memory" if _home(f) else "files to copy in",
    )
    proj(s)


def _a_index(s: Any, proj: Any, f: Any) -> None:
    # elaborate (keyword_index/BM25) and summarize (summary_index/dense aliases)
    # share one arg shape — label + optional item + --overwrite — differing only
    # in which section-index they populate.
    s.add_argument("label")
    s.add_argument("relpath", nargs="?")
    proj(s)
    s.add_argument(
        "--overwrite",
        action="store_true",
        help="regenerate even if it already exists",
    )


def _b_index(a: Any) -> dict[str, Any]:
    return {
        "label": a.label,
        "relpath": a.relpath,
        "project": a.project,
        "overwrite": a.overwrite,
    }


def _b_relpath(a: Any) -> dict[str, Any]:
    return {"relpath": a.relpath, "project": a.project}


def _a_list(s: Any, proj: Any, f: Any) -> None:
    proj(s)
    s.add_argument("-k", type=int, default=50)
    s.add_argument("--tag", action="append", dest="tags",
                   help="only items with this tag (a file's `type` counts; repeatable: any)")


def _emit_items(d: Any, args: Any) -> None:
    """`<noun> list` / `project items`: one line per item, its title then its
    (linked) path, with its kind, status, symbol, tags and fields; then how
    many of how many."""
    if getattr(args, "json", False) or not isinstance(d, dict):
        _emit(d, bool(getattr(args, "json", False)))
        return
    items = d.get("items") or []
    if not items:
        print("(nothing listed)")
    for it in items:
        kind = it.get("kind")
        store = it.get("store") or _store_of(args)
        head = _st.paint("kind", f"{kind:8} ") if kind else ""
        st = f"  [{_st.status(it['status'])}]" if it.get("status") else ""
        sym = f"  {_st.paint('name', it['symbol'])}" if it.get("symbol") else ""
        print(f"{head}{_st.paint('title', it.get('title') or '')}{st}{sym}   "
              f"{_item_path(d.get('project'), it.get('relpath', ''), store)}")
        known = {"kind", "store", "relpath", "title", "tags", "status", "symbol"}
        extra = [f"{k}={v}" for k, v in it.items() if k not in known and v is not None]
        tags = ", ".join(it.get("tags") or [])
        meta = "  ".join(x for x in (f"#{tags}" if tags else "", *extra) if x)
        if meta:
            print(_st.paint("meta", f"    {meta}"))
    if d.get("pending"):
        print(_st.paint("warn", f"not listed (facet not conformed yet): "
                                f"{', '.join(d['pending'])}: `crib facets sync`"))
    shown, total = len(items), d.get("total", len(items))
    if total > shown:
        print(_st.paint("meta", f"\n{shown} of {total} shown (-k to show more)"))


_LIST_CLI = _FacetCli(
    lambda f: (f"list {f.plural} by their fields (no search query)" if f.params
               else f"list {f.plural} (no search query)"),
    _a_list,
    lambda a: {"project": a.project, "k": a.k, "tags": a.tags},
    _emit_items, "list_items", False, 
)

_FACET_CLI: dict[str, _FacetCli] = {
    "list": _LIST_CLI,
    "lookup": _FacetCli(
        lambda f: "semantic search", _a_lookup, _b_lookup, _E, "lookup", False,
        
        aliases=("search",),
    ),
    "apropos": _FacetCli(
        lambda f: "semantic search, rendering each full matched section "
        "(alias for `search --render`)",
        _a_apropos,
        lambda a: {
            "query": a.query,
            "project": a.project,
            "k": a.k,
            "tags": a.tags,
            "hints": a.hints,
        },
        _E_apropos, "apropos", False, 
        aliases=("a",),
    ),
    "read": _FacetCli(
        lambda f: f"print a {f.noun}'s raw markdown",
        _a_relpath, _b_relpath, _E_note, "read_note", False, 
    ),
    "locate": _FacetCli(
        lambda f: f"print a {f.noun}'s on-disk path",
        _a_relpath, _b_relpath, _E_raw, "locate", False, 
    ),
    "add": _FacetCli(
        lambda f: f"create a new {f.noun} ('-' reads stdin)",
        _a_add,
        lambda a: {
            "content": _read_content(a.content),
            "title": a.title,
            "project": a.project,
            "tags": a.tags,
        },
        _E, "store_note", True, 
    ),
    "append": _FacetCli(
        lambda f: f"append to a {f.noun} ('-' reads stdin)",
        _a_append,
        lambda a: {
            "relpath": a.relpath,
            "content": _read_content(a.content),
            "heading": a.heading,
            "project": a.project,
        },
        _E, "append_note", True, 
    ),
    "edit": _FacetCli(
        lambda f: f"replace a {f.noun}'s content (stdin by default)",
        _a_edit,
        lambda a: {
            "relpath": a.relpath,
            "new_content": _read_content(a.content),
            "project": a.project,
        },
        _E, "edit_note", True, 
    ),
    "forget": _FacetCli(
        lambda f: f"delete a {f.noun} (recoverable via the ring)",
        _a_relpath, _b_relpath, _E, "forget", True, 
    ),
    "move": _FacetCli(
        lambda f: f"move/rename a {f.noun} across projects (keeps id)",
        _a_move,
        lambda a: {
            "relpath": a.relpath,
            "to_project": a.to_project,
            "to_relpath": a.to_relpath,
            "project": a.project,
        },
        _E, "move_note", True, 
    ),
    "reindex": _FacetCli(
        lambda f: "reindex a note or whole project" if _home(f)
        else f"reindex a {f.noun} or all of the project's {f.plural}",
        _a_reindex, _b_relpath, _E, "reindex", True, 
    ),
    "versions": _FacetCli(
        lambda f: f"list recoverable versions of a {f.noun}",
        _a_relpath, _b_relpath, _E, "list_versions", False, 
    ),
    "restore": _FacetCli(
        lambda f: f"restore a prior version of a {f.noun}",
        _a_restore,
        lambda a: {"relpath": a.relpath, "version": a.version, "project": a.project},
        _E, "restore", True, 
    ),
    "import": _FacetCli(
        lambda f: "copy explicit files into memory as crib-owned notes" if _home(f)
        else f"copy explicit files in as crib-owned {f.plural}",
        _a_import,
        lambda a: {"paths": a.paths, "project": a.project},
        _E, "import_files", True, 
    ),
    "distill": _FacetCli(
        lambda f: f"LLM-revise a {f.noun} in place (compress/dedupe/normalize)",
        _a_relpath, _b_relpath, _E, "distill", True, 
    ),
    "elaborate": _FacetCli(
        lambda f: "keyword_index: generate BM25 search terms per section "
        f"(keywords/questions/phrase/…) for a {f.noun} or project",
        _a_index, _b_index, _E, "elaborate", True,
        
    ),
    "summarize": _FacetCli(
        lambda f: "summary_index: generate dense alias rephrasings per "
        f"section for a {f.noun} or project",
        _a_index, _b_index, _E, "summarize", True,
        
    ),
}

# The CLI's order of a facet's subcommands (`crib note -h`), as the notes
# parsers were registered.
def _cli_order(family: str, after: tuple[tuple[str, str], ...] = ()) -> tuple[str, ...]:
    """A verb family's subcommand order in `--help`: the catalogue's order
    (facets.VERB_CATALOGUE, the same one the MCP tools register in), with
    `after` placing a verb right after another, in turn: the few places the
    CLI's help has always grouped differently. Derived, not hand-kept, so a
    new catalogue verb gets its command without touching this module."""
    from .facets import VERB_CATALOGUE

    order = [d.verb for d in VERB_CATALOGUE[family]]
    for verb, anchor in after:
        order.remove(verb)
        order.insert(order.index(anchor) + 1, verb)
    return tuple(order)


_FACET_CLI_ORDER = _cli_order("note", (("move", "forget"), ("import", "restore")))


def _a_symbol(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("symbol")
    proj(s)
    _narrowers(s)


def _a_symbol_text(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("symbol")
    s.add_argument("text")
    proj(s)
    _narrowers(s)


def _a_migrate(s: Any, proj: Any, f: Any) -> None:
    proj(s)
    s.add_argument(
        "--apply",
        action="store_true",
        help="write the plan out; without it nothing is changed",
    )


def _a_report(s: Any, proj: Any, f: Any) -> None:
    proj(s)
    s.add_argument(
        "--orphans", action="store_true", help="only the actionable ones (moved/orphan)"
    )


def _a_rehome(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("old")
    s.add_argument("new", nargs="?")
    proj(s)


def _b_symbol(a: Any) -> dict[str, Any]:
    return {
        "symbol": a.symbol,
        "path": getattr(a, "path", ""),
        "scope": getattr(a, "scope", ""),
        "lang": getattr(a, "lang", ""),
        "project": a.project,
    }


def _E_anchored(verb: str) -> Callable[[Any, Any], None]:
    """The learning emitter for a verb (`learning-add`), whatever the facet:
    its label names the verb's shape, not the noun."""
    return _E_learning(f"learning-{verb}")


_ANCHORED_CLI: dict[str, _FacetCli] = {
    "list": _LIST_CLI,
    "add": _FacetCli(
        lambda f: "attach a durable learning to a code symbol ('-' reads stdin)"
        if f.builtin else f"attach a {f.noun} to a code symbol ('-' reads stdin)",
        _a_symbol_text,
        lambda a: {
            "symbol": a.symbol,
            "text": _read_content(a.text),
            "project": a.project,
            "path": getattr(a, "path", ""),
            "scope": getattr(a, "scope", ""),
            "lang": getattr(a, "lang", ""),
        },
        _E_anchored("add"), "learning_add", True,
        
    ),
    "edit": _FacetCli(
        lambda f: f"rewrite a symbol's {f.noun} body ('-' reads stdin)",
        _a_symbol_text,
        lambda a: {
            "symbol": a.symbol,
            "new_content": _read_content(a.text),
            "project": a.project,
            "path": getattr(a, "path", ""),
            "scope": getattr(a, "scope", ""),
            "lang": getattr(a, "lang", ""),
        },
        _E_anchored("edit"), "learning_edit", True,
        
    ),
    "forget": _FacetCli(
        lambda f: f"remove a symbol's {f.noun} (recoverable; works on orphans)",
        _a_symbol, _b_symbol, _E_anchored("forget"), "learning_forget", True,
        
    ),
    "read": _FacetCli(
        lambda f: f"print a symbol's attached {f.noun}",
        _a_symbol, _b_symbol, _E_anchored("read"), "learning_read", False,
        
    ),
    "reaffirm": _FacetCli(
        lambda f: f"clear a {f.noun}'s ⚠︎ stale flag without rewriting it",
        _a_symbol, _b_symbol, _E_anchored("reaffirm"), "learning_reaffirm", True,
        
    ),
    "migrate": _FacetCli(
        lambda f: f"re-attach {f.plural} to the current symbol identity "
        "(dry run unless --apply)",
        _a_migrate,
        lambda a: {"project": a.project, "apply": a.apply},
        _E, "learning_migrate", True, 
    ),
    "report": _FacetCli(
        lambda f: f"health report for attached {f.plural} (ok/moved/orphan)",
        _a_report,
        lambda a: {"project": a.project, "orphans_only": a.orphans},
        _E_report, "learning_report", False, 
    ),
    "rehome": _FacetCli(
        lambda f: f"re-point an orphaned {f.noun} (no target = ranked suggestions)",
        _a_rehome,
        lambda a: {"old_fqn": a.old, "new_fqn": a.new, "project": a.project},
        _E_rehome, "learning_rehome", True, 
    ),
}

# The CLI's order of an anchored facet's subcommands, as learnings' were.
_ANCHORED_CLI_ORDER = _cli_order("anchored")


# ── graph facets' CLI (decisions, plans): the CLI half of crib.verbs'
# GRAPH_TEMPLATES ─────────────────────────────────────────────────────────────
# Per verb: the subcommand's help and arguments, and the VERBS row's call
# params, emitter and MCP signature, each a function of the facet's spec (its
# aspects decide add's and list's options, whether it reports taint decides the
# emitters). The built-ins read exactly as their hand-written parsers did (the
# surface snapshot holds every subcommand's --help); their wording is data,
# keyed (noun, what). A declared graph facet gets the general wording.

def _gt(spec: Any, key: str, generic: str) -> str:
    """A built-in's own CLI wording for `key` (crib/wording.toml), else the
    general one."""
    from . import wording

    own = wording.cli(spec.noun, key) if spec.builtin else None
    return own if own is not None else generic.format(n=spec.noun, p=spec.plural)


def _g_body_args(sp: Any, what: str) -> None:
    """The three ways to give a body: positional, `-` for stdin, `--file`;
    omitted at a terminal opens $EDITOR."""
    sp.add_argument("content", nargs="?",
                    help=f"{what} ('-' reads stdin; omitted opens $EDITOR)")
    sp.add_argument("--file", dest="file", help=f"read the {what} from a file")


def _g_source_arg(sp: Any) -> None:
    """`--source doc#heading`: the doc SECTION an item was drawn from."""
    sp.add_argument("--source", action="append", dest="sources", metavar="DOC#HEADING",
                    help="doc SECTION this was drawn from (repeatable)")


@dataclass(frozen=True)
class _GraphCli:
    help: Callable[[Any], str]
    args: Callable[[Any, Any, Any], None]  # (subparser, proj, spec)
    build: Callable[[Any], Callable[[Any], dict[str, Any]]]  # spec -> build
    emit: Callable[[Any], Callable[[Any, Any], None]]  # spec -> emitter
    is_async: bool
    parser: bool = True  # False: an MCP-only row (no subcommand)


def _ga_add(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("title")
    _g_body_args(s, _gt(f, "add.body", "the {n}'s body"))
    proj(s)
    s.add_argument("--dep", action="append", dest="deps",
                   help=_gt(f, "add.dep", "a dependency of the default kind for its "
                            "target (repeatable)"))
    _dep_kind_flags(s)
    _g_source_arg(s)
    if f.with_role("gated"):
        s.add_argument("--proposed", action="store_true",
                       help="land it in the import tier (taints nothing until promoted)")
    if f.has("ordered"):
        s.add_argument("--item", action="append", dest="extra_items", metavar="TITLE",
                       help="another title-only item, added after this one (repeatable)")
        s.add_argument("--after")
        s.add_argument("--before")


def _gb_add(f: Any) -> Callable[[Any], dict[str, Any]]:
    links = ("follows", "rests_on", "informed_by")
    if f.has("ordered"):
        return lambda a: {
            "title": a.title,
            # a body is OPTIONAL here — title-only is a normal whole item,
            # so an omitted body must not open $EDITOR
            "content": _body(a.content, a.file, default=""),
            "deps": a.deps,
            "after": a.after,
            "before": a.before,
            "items": _plan_items(a),
            "project": a.project,
            "sources": a.sources,
            **{k: getattr(a, k) for k in links},
        }
    gated = bool(f.with_role("gated"))
    return lambda a: {
        "title": a.title,
        "content": _body(a.content, a.file, what="the decision + rationale"),
        "deps": a.deps,
        "project": a.project,
        "sources": a.sources,
        **({"proposed": getattr(a, "proposed", False)} if gated else {}),
        **{k: getattr(a, k) for k in links},
    }


def _ga_ref(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref")
    proj(s)


def _ga_ref_opt(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref", nargs="?")
    proj(s)


def _ga_export(s: Any, proj: Any, f: Any) -> None:
    proj(s)
    s.add_argument("--out", type=Path, default=None,
                   help=_gt(f, "export.out", "write the markdown here instead of stdout"))


def _ga_edit(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref")
    _g_body_args(s, "the new body")
    proj(s)
    _g_source_arg(s)


def _ga_append(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref")
    _g_body_args(s, "the text to append")
    proj(s)
    s.add_argument("--source", action="append", default=[],
                   help="ADD a doc-section citation (doc.md#Heading; repeatable; "
                   "existing citations keep their capture-time hashes)")


def _ga_lookup(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("query")
    proj(s)
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--hint", action="append", dest="hints",
                   help=_gt(f, "lookup.hint", "exact strings you already know — each runs "
                            "as its own exact-term route; repeatable"))


def _ga_list(s: Any, proj: Any, f: Any) -> None:
    proj(s)
    if f.report_taint:
        s.add_argument("--tainted", action="store_true",
                       help="only decisions that need re-reading")
    else:
        s.add_argument("--all", action="store_true", help="include done/verified items")


def _ga_dep(with_kind: bool):
    def args(s: Any, proj: Any, f: Any) -> None:
        s.add_argument("ref")
        s.add_argument("dep_ref", metavar="dep")
        proj(s)
        if with_kind:
            _dep_kind_arg(s)

    return args


def _ga_forget(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref")
    proj(s)
    s.add_argument("--force", action="store_true",
                   help="delete anyway, leaving dependents tainted")


def _ga_tree(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref", nargs="?")
    proj(s)
    s.add_argument("--dependents", action="store_true",
                   help="what builds ON this (default: what this builds on)")
    s.add_argument("--depth", type=int, default=6)
    s.add_argument("--ascii", action="store_true", help="ASCII glyphs, no box-drawing")


def _ga_graph(s: Any, proj: Any, f: Any) -> None:
    proj(s)
    s.add_argument("--sources", action="store_true",
                   help="add doc-section attribution nodes + `source` edges")


def _ga_supersede(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref")
    s.add_argument("by", nargs="?")
    proj(s)


def _ga_extract(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("doc", help="note relpath, or a repo path indexed in situ")
    proj(s)


def _ga_status(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref")
    s.add_argument("status")
    proj(s)


def _ga_move(s: Any, proj: Any, f: Any) -> None:
    s.add_argument("ref")
    proj(s)
    s.add_argument("--after")
    s.add_argument("--before")


def _ga_next(s: Any, proj: Any, f: Any) -> None:
    proj(s)
    s.add_argument("-k", type=int, default=5)


def _const(x: Any) -> Callable[[Any], Any]:
    return lambda f: x


def _ref_build(a: Any) -> dict[str, Any]:
    return {"ref": a.ref, "project": a.project}


_GRAPH_CLI: dict[str, _GraphCli] = {
    "add": _GraphCli(lambda f: _gt(f, "add", "add a {n}"), _ga_add, _gb_add,
                     _const(_E_dwrite), True),
    "read": _GraphCli(lambda f: _gt(f, "read", "a {n}'s dossier"), _ga_ref,
                      _const(_ref_build),
                      lambda f: _E_dread if f.report_taint else _E_pread, False),
    "edit": _GraphCli(lambda f: _gt(f, "edit", "rewrite a {n}; lists what the change tainted"),
                      _ga_edit,
                      _const(lambda a: {"ref": a.ref,
                                        "new_content": _body(a.content, a.file,
                                                             what="the new body"),
                                        "project": a.project, "sources": a.sources}),
                      _const(_E_dwrite), True),
    "append": _GraphCli(lambda f: _gt(f, "append", "extend a {n}; lists what the change "
                                      "tainted"), _ga_append,
                        _const(lambda a: {"ref": a.ref,
                                          "content": _body(a.content, a.file,
                                                           what="the text to append"),
                                          "project": a.project, "sources": a.source or None}),
                        _const(_E_dwrite), True),
    "lookup": _GraphCli(lambda f: _gt(f, "lookup", "semantic search over {p}"), _ga_lookup,
                        _const(lambda a: {"query": a.query, "k": a.k, "project": a.project,
                                          "hints": a.hints}),
                        _const(_E_facet), False),
    "list": _GraphCli(lambda f: _gt(f, "list", "the {p}"), _ga_list,
                      lambda f: (lambda a: {"tainted": getattr(a, "tainted", False),
                                            "project": _proj_of(a)})
                      if f.report_taint else
                      (lambda a: {"all": getattr(a, "all", False), "project": _proj_of(a)}),
                      lambda f: _E_dlist if f.report_taint else _E_plans, False),
    "export": _GraphCli(lambda f: _gt(f, "export", "all {p} as one markdown doc"),
                        _ga_export, _const(lambda a: {"project": _proj_of(a)}),
                        _const(_E_export), False),
    "dep_add": _GraphCli(lambda f: _gt(f, "dep_add", "make a {n} depend on another"),
                         _ga_dep(True),
                         _const(lambda a: {"ref": a.ref, "dep_ref": a.dep_ref,
                                           "project": a.project, "kind": a.kind}),
                         _const(_E_dwrite), True),
    "dep_remove": _GraphCli(lambda f: _gt(f, "dep_remove", "drop one of a {n}'s "
                                          "dependencies"), _ga_dep(False),
                            _const(lambda a: {"ref": a.ref, "dep_ref": a.dep_ref,
                                              "project": a.project}),
                            _const(_E_dwrite), True),
    "forget": _GraphCli(lambda f: _gt(f, "forget", "delete a {n} (refuses while dependents "
                                      "exist)"), _ga_forget,
                        _const(lambda a: {"ref": a.ref, "force": a.force,
                                          "project": a.project}),
                        _const(_E_dwrite), True),
    "check": _GraphCli(lambda f: _gt(f, "check", "which {p} are stale"), _ga_ref_opt,
                       _const(_ref_build), _const(_E_dcheck), False),
    "reaffirm": _GraphCli(lambda f: _gt(f, "reaffirm", "re-record a {n}'s dep hashes (you "
                                        "re-read it)"), _ga_ref, _const(_ref_build),
                          lambda f: _E_dwrite if f.report_taint else _E, True),
    "tree": _GraphCli(lambda f: _gt(f, "tree", "dependency tree around a {n}"), _ga_tree,
                      _const(lambda a: {"ref": a.ref, "project": a.project,
                                        "direction": ("dependents" if a.dependents
                                                      else "deps"),
                                        "depth": a.depth}),
                      _const(_E_dtree), False),
    "graph": _GraphCli(lambda f: _gt(f, "graph", "the {p} as {{nodes, edges}}"), _ga_graph,
                       _const(lambda a: {"project": a.project, "sources": a.sources}),
                       _const(_E), False),
    "supersede": _GraphCli(lambda f: _gt(f, "supersede", "mark a {n} superseded"),
                           _ga_supersede,
                           _const(lambda a: {"ref": a.ref, "by_ref": a.by,
                                             "project": a.project}),
                           _const(_E_dwrite), True),
    "extract": _GraphCli(lambda f: _gt(f, "extract", "prepare a doc for extraction into "
                                       "{p} (writes nothing)"), _ga_extract,
                         _const(lambda a: {"relpath": a.doc, "project": a.project}),
                         _const(_E_dimport), False),
    "status": _GraphCli(lambda f: _gt(f, "status", "set a {n}'s status"), _ga_status,
                        _const(lambda a: {"ref": a.ref, "status": a.status,
                                          "project": a.project}),
                        _const(_E_dwrite), True),
    "move": _GraphCli(lambda f: _gt(f, "move", "re-order a {n}"), _ga_move,
                      _const(lambda a: {"ref": a.ref, "after": a.after,
                                        "before": a.before, "project": a.project}),
                      _const(_E_dwrite), True),
    "next": _GraphCli(lambda f: _gt(f, "next", "the {p} ready now"), _ga_next,
                      _const(lambda a: {"k": a.k, "project": a.project}),
                      _const(_E_plans), False),
    "review": _GraphCli(lambda f: "", _ga_ref, _const(lambda a: {"project": a.project}),
                        _const(_E), True, parser=False),
    "review_apply": _GraphCli(lambda f: "", _ga_ref,
                              _const(lambda a: {
                                  "batch": json.loads(Path(a.batch).read_text()),
                                  "project": _proj_of(a)}),
                              _const(_E_dwrite), True, 
                              parser=False),
}


def _transition_cli(spec: Any, verb: str) -> _GraphCli:
    """A status transition's CLI, by the role of the status it moves to."""
    role = spec.role(spec.transitions[verb])
    if role == "paused":
        def args(s: Any, proj: Any, f: Any) -> None:
            s.add_argument("ref")
            s.add_argument("--trigger", action="append", dest="triggers",
                           help=_gt(f, f"{verb}.trigger", "ref to watch; repeatable"))
            s.add_argument("--why", help=_gt(f, f"{verb}.why", "why"))
            proj(s)

        return _GraphCli(lambda f: _gt(f, verb, "pause a {n}"), args,
                         _const(lambda a: {"ref": a.ref, "triggers": a.triggers or None,
                                           "why": a.why, "project": a.project}),
                         _const(_E_dwrite), True)
    if role == "dropped":
        def dargs(s: Any, proj: Any, f: Any) -> None:
            s.add_argument("ref")
            s.add_argument("--why", required=True, help=_gt(f, f"{verb}.why", "why"))
            s.add_argument("--trigger", action="append", dest="triggers",
                           help=_gt(f, f"{verb}.trigger", "ref to watch; repeatable"))
            proj(s)

        return _GraphCli(lambda f: _gt(f, verb, "drop a {n}, saying why"), dargs,
                         _const(lambda a: {"ref": a.ref, "why": a.why, "project": a.project,
                                           "triggers": a.triggers or None}),
                         _const(_E_dwrite), True)
    return _GraphCli(lambda f: _gt(f, verb, f"move a {{n}} to {spec.transitions[verb]}"),
                     _ga_ref, _const(_ref_build), _const(_E_dwrite), True)


# A graph facet's subcommand order (`crib design -h`); transitions follow.
_GRAPH_CLI_ORDER = _cli_order(
    "graph", (("extract", "supersede"), ("review", "next"), ("review_apply", "review")))


def _graph_cli(spec: Any, verb: str) -> _GraphCli:
    return _transition_cli(spec, verb) if verb in spec.transitions else _GRAPH_CLI[verb]


def _cli_word(spec: Any, verb: str) -> str:
    from . import verbs

    return verbs.view(spec).cli(verb)


def _facets_mod() -> Any:
    from . import facets

    return facets


def _cli_of(t: Any) -> _FacetCli:
    """A template's CLI half: the anchored family has its own table (its verbs
    share names with core ones)."""
    return (_ANCHORED_CLI if t.aspect == "anchored" else _FACET_CLI)[t.verb]


def _facet_cli_verbs(spec: Any) -> list[Any]:
    from . import verbs

    # The CLI's own order (its --help has always listed them so); a verb the
    # order doesn't name still gets its command, last, never silently none.
    by = {t.verb: t for t in verbs.view(spec).verbs}
    if spec.has("links"):  # a graph facet: its order, then its transitions
        order: tuple[str, ...] = (*_GRAPH_CLI_ORDER, *spec.transitions)
    else:
        order = _ANCHORED_CLI_ORDER if spec.has("anchored") else _FACET_CLI_ORDER
    return [by[v] for v in order if v in by] + [t for v, t in by.items() if v not in order]


def _facet_parsers(sub: Any, nouns: dict[str, Any], proj: Any) -> None:
    """Each generated facet's subcommands: a built-in's under its existing
    noun parser (`nouns`), a declared facet's under its own."""
    for spec in _facets_mod().FACETS:
        if not _facet_cli_verbs(spec):
            continue
        fsub = nouns.get(spec.noun)
        if fsub is None:
            # A declared facet's commands are tried on a scratch parser first:
            # one that can't be built (a name clash the registry's checks
            # missed) is skipped with a warning, never every `crib` command.
            why = (f"its name is crib's own `{spec.noun}` command"
                   if spec.noun in sub.choices else None)
            if why is None:
                try:
                    scratch = argparse.ArgumentParser(add_help=False).add_subparsers()
                    _facet_verb_parsers(
                        scratch.add_parser(spec.noun).add_subparsers(), spec, proj)
                except (argparse.ArgumentError, ValueError, TypeError) as e:
                    why = str(e)
            if why is not None:
                print(f"[crib] facet {spec.noun!r} has no CLI commands: {why}",
                      file=sys.stderr)
                continue
            what = ("symbol-anchored" if spec.has("anchored") else "search, read, write")
            what = spec.about.splitlines()[0] if spec.about else what
            n = sub.add_parser(spec.noun, help=f"{spec.plural}: {what}")
            fsub = n.add_subparsers(dest=f"{spec.noun}_verb", required=True)
        _facet_verb_parsers(fsub, spec, proj)


def _facet_verb_parsers(fsub: Any, spec: Any, proj: Any) -> None:
    """One facet's verb subcommands under its noun's subparsers."""
    for t in _facet_cli_verbs(spec):
        if spec.has("links"):
            g = _graph_cli(spec, t.verb)
            if not g.parser:
                continue  # an MCP-only row
            sp = fsub.add_parser(
                _cli_word(spec, t.verb),
                aliases=["search"] if t.verb == "lookup" else [],
                help=g.help(spec),
            )
            g.args(sp, proj, spec)
            continue
        c = _cli_of(t)
        sp = fsub.add_parser(
            _cli_word(spec, t.verb), aliases=list(c.aliases), help=c.help(spec)
        )
        c.args(sp, proj, spec)
        if t.fields and spec.params:
            _field_options(sp, spec, t.fields)


def _field_options(sp: Any, spec: Any, mode: str) -> None:
    """A declared facet's field options: `--<field>` (typed), and `--where` on
    the filtering verbs."""
    for name, t in spec.params.items():
        sp.add_argument(
            f"--{name.replace('_', '-')}",
            dest=name,
            type=int if t == "int" else str,
            default=None,
            help=f"{t} field" + (" (YYYY-MM-DD)" if t == "date" else "")
            + (" to set" if mode == "set" else " equals"),
        )
    if mode == "filter":
        sp.add_argument(
            "--where",
            action="append",
            dest="where",
            metavar="EXPR",
            help="FIELD OP VALUE, e.g. 'severity>=3' or 'title re ^Fix' "
            "(int/date: == != < <= > >=; str: == != re); repeatable",
        )


def _with_field_args(build: Callable[[Any], dict[str, Any]], spec: Any, mode: str):
    """A row's call params plus the facet's field options (and `--where`)."""
    from .fields import parse_expr

    def b(a: Any) -> dict[str, Any]:
        call = build(a)
        for name in spec.params:
            if (v := getattr(a, name, None)) is not None:
                call[name] = v
        if mode == "filter" and getattr(a, "where", None):
            call["where"] = [parse_expr(e) for e in a.where]
        return call

    return b


def _facet_rows() -> dict[str, Verb]:
    """Each generated facet's VERBS rows. The policy is the MCP template's; a
    facet other than notes carries itself to the in-process Crib method
    (`inproc`), the daemon path calls its own `<noun>_<verb>` tool."""
    from . import facets, verbs

    rows: dict[str, Verb] = {}
    for spec in facets.FACETS:
        v = verbs.view(spec)
        for t in _facet_cli_verbs(spec):
            if spec.has("links"):
                g = _graph_cli(spec, t.verb)
                word = _cli_word(spec, t.verb)
                method, inproc_kw = verbs.inproc_method(spec, t.verb)
                rows[f"{spec.noun} {word}"] = Verb(
                    t.verb, g.build(spec), g.emit(spec),
                    method=method, inproc=inproc_kw,
                    is_async=g.is_async, policy=t.policy, mcp=v.mcp(t.verb),
                )
                continue
            c = _cli_of(t)
            inproc: tuple[tuple[str, Any], ...] = ()
            if not spec.builtin:
                inproc = (("store", spec.store),) if t.verb == "lookup" else (
                    ("facet", spec.noun),)
            build, names = c.build, ()
            if t.fields and spec.params:
                build = _with_field_args(c.build, spec, t.fields)
                names = tuple(spec.params)
            mcp = v.mcp(t.verb)  # the MCP twin's signature: its body's
            rows[f"{spec.noun} {_cli_word(spec, t.verb)}"] = Verb(
                t.verb, build, c.emit, method=c.method, is_async=c.is_async,
                policy=t.policy, mcp=mcp, inproc=inproc, fields=names,
            )
    return rows


# Deletion is ALWAYS human-confirmed (MCP mode elicits; the CLI prompts in its
# terminal, `--yes` for scripts) and the CLI routes it IN-PROCESS below — the
# confirmation must be a human act, never a tool-call parameter an agent could
# supply. Routing detail in main()'s `project delete` branch.
def _proj_named(a: Any, verb: str, optional: bool = False) -> str | None:
    """The lifecycle verbs' project name: the positional OR the `-p` selector
    spelling, same fact, both allowed — but never silently arbitrated. If both
    are given and DIFFER, that's a typo, not a choice; say so and refuse. (The
    dests are separate, so there's no order-dependence to launder through.)

    `optional=True` (create) returns None when NEITHER is given: the core
    derives the name from the repo's `.crib`/repo dir — the MCP surface can
    do that, so the CLI must not block it (nargs="?" already says optional)."""
    from .errors import CribUserError

    pos, flag = getattr(a, "project", None), getattr(a, "project_flag", None)
    if pos is not None and flag is not None and pos != flag:
        raise CribUserError(
            f"crib project {verb}: {pos!r} (positional) and {flag!r} (-p) name "
            "different projects — give one"
        )
    name = pos if pos is not None else flag
    if name is None and not optional:
        raise CribUserError(
            f"crib project {verb}: pass the project name (positional or -p)"
        )
    return name


def _proj_name_required(a: Any, verb: str) -> str:
    """The strict form of `_proj_named` — a name is REQUIRED and is a `str`
    (the checker-visible guarantee the delete path needs; delete never
    resolves a session default or a cwd)."""
    from .errors import CribUserError

    name = _proj_named(a, verb, optional=False)
    if name is None:  # unreachable: strict mode raises above
        raise CribUserError(
            f"crib project {verb}: pass the project name (positional or -p)"
        )
    return name


_DELETE_VERB = Verb(
    "project_delete",
    lambda a: {
        "project": _proj_name_required(a, "delete"),
        "force": getattr(a, "force", False),
    },
    _E_project("delete"),
    is_async=True,
    policy="write",
    wants_cwd=False,  # deletion never resolves from a path (MCP tool agrees)
    mcp="project force=False",
)

VERBS: dict[str, Verb] = {
    "project reconcile": Verb(
        "reconcile",
        lambda a: {},
        _E,
        method="reconcile_all",
        is_async=True,
        wants_cwd=False,
        policy="none",
        mcp="",
    ),
    "note import-memory": Verb(
        "import_memory",
        lambda a: {"project": a.project},
        _E,
        method="import_claude_memory",
        is_async=True,
        policy="source",
        mcp=_PROJ,
    ),
    "memory snapshot": Verb(
        "snapshot",
        lambda a: {"message": a.message},
        _E_raw,
        wants_cwd=False,
        policy="none",
        mcp="message=None",
    ),
    "memory history": Verb(
        "history",
        lambda a: {"relpath": a.relpath},
        _E,
        wants_cwd=False,
        policy="none",
        mcp="relpath=None",
    ),
    "project list": Verb(
        "projects",
        lambda a: {},
        _E_projects,
        method="project_list",
        wants_cwd=False,
        policy="none",
        mcp="",
    ),
    "status": Verb(
        "status", lambda a: {}, _E_status, wants_cwd=False, policy="none", mcp=""
    ),
    "ops": Verb("ops", lambda a: {}, _E_ops, wants_cwd=False, policy="none", mcp=""),
    # project lifecycle (whole repo) — repo-scoped, hence the `source` policy
    "project setup": Verb(
        "project_setup",
        lambda a: {"project": _proj_of(a)},
        _E_project("setup"),
        is_async=True,
        policy="source",
        mcp=_PROJ,
    ),
    "project index": Verb(
        "project_index",
        # the polling contract: the daemon face returns a RUNNING HANDLE at
        # once (no wait_s — a blocking call can't survive proxy caps); the CLI
        # emitter follows it to completion (a terminal may block) via
        # project_wait when the handle says running.
        lambda a: {"project": _proj_of(a), "budget_s": getattr(a, "budget_s", None),
                   "phased": getattr(a, "phased", None)},
        _E_project("index"),
        is_async=True,
        policy="source",
        mcp=f"{_PROJ} budget_s=None phased=None",
    ),
    "project wait": Verb(
        "project_wait",
        lambda a: {"project": _proj_of(a), "wait_s": getattr(a, "wait_s", None)},
        _E_project("wait"),
        is_async=True,
        policy="none",
        mcp=f"{_PROJ} wait_s=30.0",
    ),
    "project cancel": Verb(
        "project_cancel",
        lambda a: {"project": _proj_of(a)},
        _E_project("cancel"),
        is_async=True,
        policy="source",
        mcp=f"{_PROJ}",
    ),
    "project status": Verb(
        "project_status",
        lambda a: {"project": _proj_of(a)},
        _E_project("status"),
        policy="source",
        mcp=_PROJ,
    ),
    "project doctor": Verb(
        "project_doctor",
        lambda a: {"project": _proj_of(a)},
        lambda d, a: _emit_doctor(d, a.json),
        policy="source",
        mcp=_PROJ,
    ),
    "project lookup": Verb(
        "project_lookup",
        lambda a: {"query": a.query, "k": a.k, "kinds": a.kinds,
                   "status": a.status, "project": _proj_of(a)},
        lambda d, a: _emit_lookup_all(d, a.json),
        method="lookup_all",
        policy="source",
        mcp=f"query k=8 kinds=None status=None {_PROJ}",
    ),
    "project items": Verb(
        "project_items",
        lambda a: {"kinds": a.kinds, "k": a.k, "tags": a.tags,
                   "project": _proj_of(a)},
        _emit_items,
        policy="source",
        mcp=f"kinds=None k=200 tags=None {_PROJ}",
    ),
    "project related": Verb(
        "project_related",
        lambda a: {"ref": a.ref, "k": a.k, "project": _proj_of(a)},
        lambda d, a: _emit_related(d, a.json),
        policy="source",
        mcp=f"ref k=3 {_PROJ}",
    ),
    "project forget": Verb(
        "project_forget",
        lambda a: {
            "project": _proj_of(a),
            "with_learnings": getattr(a, "with_learnings", False),
        },
        _E_project("forget"),
        policy="source",
        mcp=f"{_PROJ} with_learnings=False",
    ),
    # in-repo storage: move a project's notes into / out of the repo that owns them
    "project adopt": Verb(
        "project_adopt",
        lambda a: {"project": _proj_of(a)},
        _E_project("adopt"),
        is_async=True,
        policy="source",
        mcp=_PROJ,
    ),
    "project release": Verb(
        "project_release",
        lambda a: {"project": _proj_of(a)},
        _E_project("release"),
        is_async=True,
        policy="source",
        mcp=_PROJ,
    ),
    "project migrate": Verb(
        "project_migrate",
        lambda a: {"project": _proj_of(a)},
        _E_project("migrate"),
        is_async=True,
        policy="source",
        mcp=_PROJ,
    ),
    "project create": Verb(
        "project_create",
        lambda a: {
            # OPTIONAL name: no positional and no -p means the core derives it
            # from the repo's .crib/repo dir (with --store) — the MCP surface
            # can do that, so the CLI must not block it.
            "project": _proj_named(a, "create", optional=True),
            "store": getattr(a, "store", None),
            "transient": getattr(a, "transient", False),
            "expires": getattr(a, "expires", None),
        },
        _E_project("create"),
        is_async=True,
        # matches the server's "none" (create does its own naming: no
        # session-default fallback may point a repo-bound store at `default`)
        policy="none",
        mcp="project=None store=None project_path=None transient=False expires=None",
    ),
    "project rename": Verb(
        "project_rename",
        lambda a: {"project": a.project, "to": a.to},
        _E_project("rename"),
        is_async=True,
        policy="write",
        wants_cwd=False,  # the MCP tool takes no project_path
        mcp="project to",
    ),
    "project relocate": Verb(
        "project_relocate",
        lambda a: {
            "project": a.project,
            "project_path": a.project_path,
            "source": getattr(a, "source", None),
            "keep_prefix": getattr(a, "keep_prefix", False),
        },
        _E,  # the report is structured; json-aware print
        is_async=True,
        policy="none",
        wants_cwd=False,  # the new dir is named, never inferred from cwd
        mcp=_PROJ + " source=None keep_prefix=False",
    ),
    "project delete": _DELETE_VERB,
    # code index
    "code lookup": Verb(
        "code_lookup",
        lambda a: {"query": a.query, "project": a.project, "k": a.k},
        _E_code("code-lookup"),
        policy="read",
        mcp=f"query {_PROJ} k=8",
    ),
    "code xref": Verb(
        "code_xref",
        lambda a: {
            "symbol": a.symbol,
            "path": getattr(a, "path", ""),
            "scope": getattr(a, "scope", ""),
            "lang": getattr(a, "lang", ""),
            "project": a.project,
        },
        _E_code("code-xref"),
        policy="read",
        mcp=f'symbol path="" scope="" lang="" {_PROJ}',
    ),
    "code dossier": Verb(
        "code_dossier",
        lambda a: {
            "symbol": a.symbol,
            "project": a.project,
            "path": a.path,
            "scope": a.scope,
            "lang": a.lang,
        },
        _E_dossier,
        policy="read",
        mcp=f"symbol {_PROJ} path='' scope='' lang=''",
    ),
    "code graph": Verb(
        "code_graph",
        lambda a: {
            "symbol": _graph_symbol(a),
            "direction": _graph_direction(a),
            "depth": a.depth,
            "project": a.project,
            "shape": _graph_shape(a),
            "group_by": a.group_by,
            "group_depth": a.group_depth,
            "path": a.path,
            "scope": a.scope,
            "lang": a.lang,
            "under": a.under,
            "exclude": a.exclude,
        },
        _E_graph,
        policy="read",
        mcp=f"symbol=None {_PROJ} direction='callees' depth=6 "
        "shape=None group_by=None group_depth=0 "
        "path='' scope='' lang='' under='' exclude=''",
    ),
    "code index": Verb(
        "code_index",
        lambda a: {
            "path": str(Path(a.path).expanduser().resolve()),
            "project": a.project,
        },
        _E_code("code-index"),
        is_async=True,
        policy="source",
        mcp=f"path {_PROJ}",
    ),
    "code convert": Verb(
        "code_convert",
        lambda a: {"project": a.project, "apply": a.apply},
        _E,
        is_async=True,
        policy="write",
        mcp=f"{_PROJ} apply=False",
    ),
}


# Verb.tool historically doubled as BOTH the MCP tool name and the Crib method (they
# matched). After the noun-verb rename they diverge: the MCP tool is the nested key
# underscored (`note lookup` → `note_lookup`), the Crib method stays the old tool name
# (or the explicit `method=`). Split them here so no row needs editing (Verb is frozen).
VERBS.update(_facet_rows())
VERBS = {
    _key: replace(
        _v, method=_v.method or _v.tool, tool=_key.replace(" ", "_").replace("-", "_")
    )
    for _key, _v in VERBS.items()
}


def _cwd_of(args: Any) -> Path:
    """The caller's project anchor: -P/--project-path overrides the actual cwd.

    Resolve to an absolute path client-side: the daemon anchors a relative path
    against its own cwd, so an unresolved `-P .` would silently select `default`."""
    return (
        Path(args.project_path).expanduser().resolve()
        if getattr(args, "project_path", None)
        else Path.cwd()
    )


def _resolve_verb(args: Any) -> tuple[Verb, dict[str, Any]]:
    """Map parsed args to a (Verb, call-params) pair. `crib <noun> <verb>` keys the
    registry by "<noun> <verb>"; a bare top-level verb (status) keys by its name.
    Normalizes the note aliases (`search`→lookup, `a`→apropos) and routes
    `note lookup --render` to the apropos section-rendering path."""
    noun = args.cmd
    sub = getattr(args, f"{noun}_verb", None)
    if sub is None:  # flat top-level verb (status)
        entry = VERBS[noun]
        return entry, entry.build(args)
    sub = {"search": "lookup", "a": "apropos"}.get(sub, sub)
    if sub == "lookup" and getattr(args, "render", False) and f"{noun} apropos" in VERBS:
        sub = "apropos"
    entry = VERBS[f"{noun} {sub}"]
    return entry, entry.build(args)


# A bare noun means its ORIENTING READ — what you wanted when you typed just the
# noun. `crib project` → status (the precedent); `crib design`/`crib plan` → list.
_BARE_NOUN_DEFAULT = {"project": "status", "design": "list", "plan": "list"}


def _dispatch(args: Any) -> tuple[Verb, dict[str, Any]]:
    """Everything goes through the registry, with bare nouns defaulted per
    `_BARE_NOUN_DEFAULT` (`crib design` = `crib design list`)."""
    default = _BARE_NOUN_DEFAULT.get(args.cmd)
    if default and getattr(args, f"{args.cmd}_verb", None) is None:
        setattr(args, f"{args.cmd}_verb", default)
    return _resolve_verb(args)


def _resolve_tool_spec(args: Any) -> str | list[str] | None:
    """The tool-set exposure for serve: the --tools flag WINS over config.toml
    `[tools].expose`; neither = everything. (Validated at serve startup.)"""
    from .config import Config
    from .paths import Paths

    spec = getattr(args, "tools", None)
    if spec is not None and str(spec).strip():
        return str(spec)
    cfg = Config.load(Paths.resolve().config_file)
    return cfg.tools.expose or None


def _resolve_serve_endpoint(args: Any) -> tuple[str, int]:
    """Bind address for `serve`/`--mcp`: explicit flags win, else `[daemon]`."""
    from .config import Config
    from .paths import Paths

    cfg = Config.load(Paths.resolve().config_file)
    return (args.host or cfg.daemon.host, args.port or cfg.daemon.port)


def _unlinked_path_message(args: Any, entry: Verb, cfg: Any) -> str | None:
    """The advisory text when an explicit -P/--project-path names a directory that
    no `.crib` anchors (else None) — the call answers from the default project,
    which is that path's DOCUMENTED fallback but almost always a missing `.crib`
    rather than intent (see DESIGN: 'An explicit selector never loses'). A bare cwd
    (no -P) is deliberate, so this is gated on `args.project_path` being set."""
    if not (entry.wants_cwd and args.project_path):
        return None
    if args.cmd == "project" and args.project_verb == "create":
        # A fresh repo without .crib is create's INTENDED input (at-birth
        # residency writes the .crib) — the "answering from 'default'" advisory
        # would be not just noise but wrong.
        return None
    from .config import CribLink

    path = _cwd_of(args)
    if CribLink.find(path) is not None:  # a `.crib` decides the project — no surprise
        return None
    return (
        f"crib: -P {args.project_path} is not linked to a crib project (no `.crib` "
        f"in {path} or a parent) — answering from '{cfg.default_project}'. Did you "
        f"mean `-p <name>`, or to link this repo: "
        f"`crib project setup -P {args.project_path}`?"
    )


def _apply_unlinked_advisory(args: Any, msg: str | None, data: Any) -> Any:
    """Surface the unlinked-path advisory coherently across output modes. Under
    `--json` a stderr-only warning is HAZARDOUS: a stdout-parsing consumer reads a
    legit-looking empty result (`items: []`) and never learns it answered from the
    default project. So put the message IN the payload (`unlinked_project_path`) for
    `--json`, and on stderr for a human. Also drops the daemon's own copy of the key
    so the CLI is its single owner — daemon and in-process paths then behave
    identically (the in-process path never sees the server key)."""
    if isinstance(data, dict):
        data.pop("unlinked_project_path", None)  # the CLI owns this, not the daemon
        if msg and args.json:
            data["unlinked_project_path"] = msg
    if msg:
        print(msg, file=sys.stderr)
    return data


# Verbs whose Crib call runs a project-wide code sweep — the only ones a progress
# ticker makes sense for (both go through _index_project_code).
_SWEEP_TOOLS = frozenset({"project_index", "project_setup"})


class _SweepTicker:
    """One stderr line, redrawn in place while a sweep runs.

    TTY-gated on purpose: piped or `--json` output (a stdout consumer, a script)
    sees nothing from us, so machine-readable streams stay clean. stderr, not
    stdout, for the same reason — the human channel, trivially 2>/dev/null'd."""

    def __init__(self) -> None:
        self._width = 0

    def draw(self, text: str) -> None:
        if not sys.stderr.isatty():
            return
        sys.stderr.write("\r" + text + " " * max(self._width - len(text), 0))
        sys.stderr.flush()
        self._width = len(text)

    def done(self) -> None:
        """Erase the line so the verb's own summary starts on a clean row."""
        if self._width and sys.stderr.isatty():
            sys.stderr.write("\r" + " " * self._width + "\r")
            sys.stderr.flush()
            self._width = 0


def _daemon_sweep_progress(ticker: _SweepTicker) -> "ProgressHandler":
    """A progress callback for DaemonClient: render the daemon's message as it
    streams in — already shaped `<proj>: 42/317 files (13%) · rate · eta`
    server-side by `format_sweep`, so the CLI ticker and the MCP surface show
    the SAME line for the same sweep."""

    async def _on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        if message:
            ticker.draw(message)

    return _on_progress


def _sweep_watcher(
    crib: Crib, named: str | None, ticker: _SweepTicker, stop: threading.Event
) -> None:
    """In-process ticker feed: poll the sweep counter (the same dict `status`
    reads) once a second and redraw. `before` scopes us to the sweep THIS call
    started — another project's concurrent sweep is never ours to report."""
    from .codestore import format_sweep

    before = set(crib.code.sweeps)
    while not stop.wait(1.0):
        with crib.code.indexing_lock:
            sweeps = {p: dict(v) for p, v in crib.code.sweeps.items()}
        pick = (
            named
            if named in sweeps
            else next((p for p in sweeps if p not in before), None)
        )
        if pick and sweeps[pick].get("total"):
            ticker.draw(f"{pick}: {format_sweep(sweeps[pick])}")


def _run_daemon(args: Any, cfg: Any) -> None:
    """Run a verb via the warm daemon: build the call, ship the caller's cwd as
    `project_path`, call the MCP tool, and emit — all off one registry row."""
    from .client import DaemonClient
    from .errors import CribUserError

    entry, call = _dispatch(args)
    if entry.wants_cwd:
        call["project_path"] = str(_cwd_of(args))
    msg = _unlinked_path_message(args, entry, cfg)
    ticker = _SweepTicker()
    handler = _daemon_sweep_progress(ticker) if entry.tool in _SWEEP_TOOLS else None
    with DaemonClient(cfg.daemon) as client:
        try:
            data = client.call(entry.tool, call, progress_handler=handler)
        except Exception as e:  # noqa: BLE001 — only the unknown-tool case is ours
            if f"Unknown tool: {entry.tool!r}" not in str(e) and \
                    f"Unknown tool: {entry.tool}" not in str(e):
                raise
            # This process and the daemon resolve the facet registry each for
            # themselves: a facet declared (or synced) after the daemon started,
            # or a different $CRIB_FACETS_FILE here, has commands the daemon
            # doesn't serve.
            raise CribUserError(
                f"the running crib daemon has no `{entry.tool}` tool: it read its "
                "facets file before this facet was declared or synced (or a "
                "different one), or its `--tools` don't expose it. Restart the "
                "daemon, or run this command with `crib --no-daemon ...`") from e
    ticker.done()
    entry.emit(_apply_unlinked_advisory(args, msg, data), args)


def _in_repo_guard(paths: Any, cfg: Any, cwd: Path) -> str | None:
    """Refusal message when the project you're standing in keeps its notes in its
    OWN repo, else None.

    `crib memory setup/sync/push/pull` act on the whole global data tree, which by
    construction no longer contains an adopted project's notes — so running them
    here would look like it synced those notes and silently wouldn't. Worse, a
    user who "fixed" that by also committing them from the repo would end up with
    two git histories of one file. Refuse and name the owner."""
    from .config import resolve_project
    from .paths import resolve_project_paths

    try:
        proj = resolve_project(cfg, None, cwd)
        pp = resolve_project_paths(paths, cfg, proj)
    except ValueError:
        return None  # unresolvable project: not our error
    if not pp.in_repo:
        return None
    return (
        f"crib memory: notes for {proj} live in {pp.store_root}; commit them "
        f"with that repo's git. (The global memory store no longer carries "
        f"them — `crib project release {proj}` moves them back if you want "
        f"crib to sync them again.)"
    )


def _run_git(args: Any, cfg: Any) -> int:
    """Share notes via git. Runs git client-side (the user's terminal owns auth);
    a pull that changes files then triggers a reindex through the daemon (or
    in-process). Not an MCP tool — pushing notes is outward-facing + interactive."""
    from .gitbacking import GitBacking
    from .paths import Paths

    # the memory noun always defines memory_verb (required subparser), and
    # main() dispatches here only for the four git verbs
    verb = args.memory_verb  # `crib memory setup/sync/push/pull`
    # setup runs on a fresh machine where the data dir may not exist yet
    paths = Paths.resolve().ensure() if verb == "setup" else Paths.resolve()
    if paths.root is not None:
        print(f"crib memory {verb}: refused in confined mode (--root): the root "
              "is disposable and never git-synced", file=sys.stderr)
        return 1
    if (refusal := _in_repo_guard(paths, cfg, _cwd_of(args))) is not None:
        print(refusal, file=sys.stderr)
        return 1
    git = GitBacking(paths.data_dir)

    if verb == "setup":
        # the setup parser always defines --remote; verb is matched above
        remote = args.remote or git.current_remote() or _prompt_remote()
        if not remote:
            print(
                "crib memory setup: no remote given (pass --remote <url>)",
                file=sys.stderr,
            )
            return 1
        print(f"joining {remote} …")
        res = git.setup(remote)
    elif verb == "sync":
        if args.remote:  # the sync parser always defines it
            print(git.init(args.remote))
        res = git.sync(args.message)
    elif verb == "push":
        res = git.push()
    else:
        res = git.pull()

    print(res.message)
    if res.conflicts:
        return 1
    if res.changed:  # a pull rewrote notes → index must follow
        print("reindexing pulled changes…")
        print(f"  {_reconcile(cfg)}")
    return 0 if res.ok else 1


def _prompt_remote() -> str | None:
    """Ask for the remote URL when `setup` is run interactively without one."""
    if not sys.stdin.isatty():
        return None
    try:
        return input("Remote URL to join (git): ").strip() or None
    except EOFError:
        return None


def _reconcile(cfg: Any) -> Any:
    """Run reconcile via the warm daemon if available, else in-process."""
    if cfg.daemon.enabled:
        from . import sharedserver

        if sharedserver.available():
            from .client import DaemonClient

            with DaemonClient(cfg.daemon) as client:
                return client.call("project_reconcile", {})
    crib = Crib.open()
    try:
        return asyncio.run(crib.reconcile_all())
    finally:
        crib.close()


def _confirm_delete_terminal(args: Any, proj: str) -> bool:
    """The CLI's half of ALWAYS-CONFIRMED deletion (MCP mode elicits; the CLI
    asks in its terminal). Piped stdin refuses without --yes — a script that
    can't confirm must not delete."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print(
            f"crib: refusing to delete {proj!r} without confirmation — stdin is "
            "not a terminal; pass --yes (or answer the prompt interactively)",
            file=sys.stderr,
        )
        return False
    ans = input(
        f"Delete crib project '{proj}'? The tree moves to .trash (one-level "
        "undo); an in-repo store needs --force and is removed from the repo. "
        "[y/N] "
    )
    return ans.strip().lower() in ("y", "yes")


def _daemon_is_up(cfg: Any) -> bool:
    """Is a crib daemon ACTUALLY listening right now — a socket probe of the
    configured host/port, NOT `sharedserver.available()` (which only checks
    the binary is on PATH; attaching via DaemonClient would then SPAWN one,
    whose startup reconcile races the destructive verb this guards)."""
    import socket

    if not getattr(cfg, "daemon", None) or not cfg.daemon.enabled:
        return False
    try:
        with socket.create_connection((cfg.daemon.host, cfg.daemon.port), timeout=0.3):
            return True
    except OSError:
        return False


def _daemon_sweep_fence(cfg: Any, proj: str) -> None:
    """Pre-flight for an in-process destructive verb while a daemon runs: the
    daemon's warm state (running sweep) is invisible to this process — ask IT
    whether anything is in flight for `proj` before deleting.

    ATTACHED daemons only — `_daemon_is_up`'s socket probe never spawns one
    (DaemonClient.__enter__ would), and when none is up there is nothing to
    race. Uses `status` (which carries `sweeps` + `reconciling`), not
    `project_status` (which has neither)."""
    if not _daemon_is_up(cfg):
        return
    from .client import DaemonClient

    try:
        with DaemonClient(cfg.daemon) as client:
            st = client.call("status", {})
    except Exception:  # noqa: BLE001 — daemon went down mid-probe ⇒ cannot race
        return
    if (
        (st.get("sweeps") or {}).get(proj)
        or st.get("reconciling")
        or (st.get("indexing") or {}).get(proj)
    ):
        raise SystemExit(
            f"crib: the daemon is indexing ({proj!r} sweep or a background "
            "reconcile) — `crib project cancel` / wait for it to finish first"
        )


def _run_inprocess(args: Any, cfg: Any) -> None:
    """Run a verb in-process against a Crib instance: same registry row as the
    daemon path, but call the Crib `method` with `cwd=<Path>` and wrap async ones
    in `asyncio.run` (the daemon does this awaiting server-side)."""
    entry, call = _dispatch(args)
    if entry.wants_cwd:
        call["cwd"] = _cwd_of(args)
    msg = _unlinked_path_message(args, entry, cfg)
    crib = Crib.open()
    ticker = _SweepTicker()
    stop = threading.Event()
    watcher: threading.Thread | None = None
    try:
        if entry.tool in _SWEEP_TOOLS:
            watcher = threading.Thread(
                target=_sweep_watcher,
                args=(crib, _proj_of(args), ticker, stop),
                daemon=True,
            )
            watcher.start()
        if (facet := dict(entry.inproc).get("facet")) is not None:
            crib._require_conformed(facet)  # as the daemon's tool would refuse
        method = getattr(crib, entry.crib_method())
        call.update(entry.inproc)
        if entry.fields:
            given = {f: call.pop(f) for f in entry.fields if f in call}
            if given:
                call["fields"] = given
        data = asyncio.run(method(**call)) if entry.is_async else method(**call)
        if watcher is not None:
            stop.set()
            watcher.join(timeout=2.0)
        ticker.done()
        entry.emit(_apply_unlinked_advisory(args, msg, data), args)
    finally:
        stop.set()
        crib.close()


def _user_errors() -> tuple[type[BaseException], ...]:
    """What to print as a message rather than a traceback: the `CribUserError` base
    (in-process) and `ToolError` (the daemon path's wrapper for the very same
    conditions — see `errors.py`). Two entries, one idea; a new user-error subclass
    needs no change here."""
    from .errors import CribUserError

    out: tuple[type[BaseException], ...] = (CribUserError,)
    try:
        from fastmcp.exceptions import ToolError
    except ImportError:  # fastmcp absent ⇒ no daemon path
        return out
    return (*out, ToolError)


def _bundle_cmd(args: Any) -> int:
    """`crib bundle create|expire|list|delete`: local, trusted-side admin (except
    `expire`, which also works inside a confined bundle on its own root)."""
    from . import bundle as bundles
    from .config import Config
    from .errors import CribUserError
    from .paths import Paths

    paths = Paths.resolve()
    cmd = args.bundle_cmd
    if cmd == "expire":
        target = args.dir or paths.root
        if target is None:
            raise CribUserError("crib bundle expire: name the bundle directory")
        out: Any = bundles.expire(target, args.expires)
        human = f"{out['bundle']}: expires {out['expires']}"
    elif paths.root is not None:
        raise CribUserError(
            f"crib bundle {cmd}: run on the trusted side, not in confined mode")
    elif cmd == "create":
        cfg = Config.load(paths.ensure().config_file)
        out = bundles.create(paths, cfg, args.dir, args.overlay, args.context,
                             args.context_to, args.context_mount, args.expires,
                             vectors=args.vectors)
        files = ", ".join(f"{p} {n}" for p, n in out["files"].items())
        human = (f"bundle {out['bundle']}\n  overlay {out['overlay']} (writable, from "
                 f"{out['base']})\n  context {', '.join(out['context']) or '-'}"
                 + (f" at {out['context_dir']}" if out["context_dir"] else "")
                 + f"\n  files: {files}\n  vectors: {out['vectors']}\n"
                 f"  expires {out['expires']}\n"
                 f"  open it with: crib --root {out['bundle']} [--store SCRATCH]")
    elif cmd == "list":
        out = bundles.listing(paths)
        human = "\n".join(
            f"{r['state']:10} {r['bundle']}  {r.get('overlay', '')}  "
            f"expires {r.get('expires') or '-'}" for r in out) or "(no bundles)"
    else:  # delete
        out = bundles.delete(paths, args.dir)
        human = f"deleted {out['bundle']}" + (
            f" (+ {', '.join(out['removed'][1:])})" if len(out["removed"]) > 1 else "")
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(human)
    return 0


def _facets_cmd(args: Any) -> int:
    """`crib facets show|init|sync`: local, against this machine's store."""
    from . import facets

    if args.facets_cmd == "list":  # an alias of `show`
        args.facets_cmd = "show"

    if args.facets_cmd == "init":
        f = facets.system_file()
        if f.exists() and not args.force:
            print(f"{f} already exists: edit it, or re-run with --force to replace it")
            return 1
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(facets.starter())
        print(f"wrote {f}: uncomment an example, then `crib facets sync --apply`")
        return 0
    if args.facets_cmd == "instructions":
        print(facets.instructions(), end="")  # the registry only: no store opened
        return 0
    if args.facets_cmd == "show" and args.toml:
        print(facets.to_toml(facets.FACETS), end="")
        return 0
    crib = Crib.open()
    plan = {p["facet"]: p for p in crib.store.facet_plan()}
    if args.facets_cmd == "show":
        if facets.LOAD_ERROR:
            print(f"facets file ignored: {facets.LOAD_ERROR}")
        for spec in facets.FACETS:
            origin = "built-in" if spec.builtin else "declared"
            state = plan.get(spec.noun, {}).get("status", "current")
            print(f"{spec.noun}  ({origin}, {state})")
            print(f"  plural {spec.plural}, store {spec.store}, aspects "
                  + ", ".join(sorted(spec.aspects)))
            if spec.params:
                print("  fields " + ", ".join(f"{n} ({t})" for n, t in spec.params.items()))
            if spec.disabled:
                print("  disabled " + ", ".join(sorted(spec.disabled)))
            if spec.flags:
                print("  flags " + ", ".join(
                    k if v is True else f"{k}={v}" for k, v in spec.flags.items()))
        for noun, p in plan.items():
            if p["status"] == "removed":
                print(f"{noun}  (removed from the facets file; still in the store)")
        print(f"facets file: {facets.system_file()}")
        return 0
    if facets.LOAD_ERROR:
        print(f"refusing: the facets file didn't load ({facets.LOAD_ERROR}). Fix it "
              "first: until then every declared facet would look removed")
        return 1
    removed = [p for p in plan.values() if p["status"] == "removed"]
    todo = [p for p in plan.values() if p["status"] in ("new", "changed")
            or (p["status"] == "removed" and args.drop)]
    for p in removed:
        if not args.drop:
            print(f"  kept     {p['facet']}: gone from the facets file; its index rows "
                  "stay until `--drop` (your files are never touched)")
    if not todo:
        print("the store is conformed to every declared facet: nothing to do")
        return 0
    for p in todo:
        print(f"  {p['status']:8} {p['facet']}: {p['detail']}")
    shared = getattr(crib.store, "SHARED", False)
    if shared:
        print("this store is SHARED (the crib DuckDB server): the change applies to "
              "every machine using it, whose facets files must say the same")
    if not args.apply:
        print("dry run: nothing changed (re-run with --apply"
              + (", and --shared" if shared and todo else "") + ")")
        return 0
    if shared and not args.shared:
        print("refusing: a shared store changes for every machine; re-run with "
              "--apply --shared once their facets files agree")
        return 1
    crib.store.facet_sync(drop=args.drop, shared=args.shared)
    # a facet whose store moved leaves its files in the old directories (unless
    # another facet now has that store): in crib's data dir, and in each project
    # adopted into a repo (`store:`)
    for p in todo:
        old = p.get("was_store")
        spec = facets.get(p["facet"])
        if old and spec is not None and old != spec.store and facets.by_store(old) is None:
            roots = [crib.paths.projects_dir / n for n in crib.projects()]
            roots += [pp.data_root for n in crib.projects()
                      if (pp := crib.project_paths(n)).in_repo and pp.available]
            left = sorted({d for r in roots if (d := r / old).is_dir()
                           and any(d.rglob("*.md"))})
            for d in left:
                print(f"  note: {p['facet']} items are still in {d}: move them to "
                      f"{d.parent / spec.store} (the store is now {spec.store!r})")
    rebuilt = [p["facet"] for p in todo if p["status"] in ("new", "changed")]
    crib = Crib.open()  # now serving them
    files = 0
    for noun in rebuilt:
        for proj in crib.projects():
            if crib._facet_store(noun).root(proj).exists():
                files += (asyncio.run(crib.reindex(None, proj, facet=noun)))["files"]
    print(f"applied; re-read {files} file(s) of {', '.join(rebuilt) or 'no facet'}")
    print("restart the crib daemon" + (" on every machine sharing this store"
          if shared else "") + " so it serves the change")
    return 0


def _generate_cmd(args: Any) -> int:
    from . import gensetup
    from .config import Config
    from .paths import Paths

    paths = Paths.resolve()
    if args.generate_cmd == "setup":
        return gensetup.run_setup(
            paths, gensetup.detect(), ask=input, say=print, assume_yes=args.yes,
            pick=args.provider, model=args.model, mode=args.mode)
    cfg = Config.load(paths.config_file).generate
    print(f"generation mode: {cfg.resolved_mode()}"
          + (f" · providers: {cfg.config} (" + ", ".join(
              f"{k} {getattr(cfg, k)}"
              + (" from $CRIB_GENERATE_" + k.upper() if k in cfg.from_env else "")
              for k in ("profile", "provider") if getattr(cfg, k)) + ")"
             if cfg.config else " · no models config: the inline [generate] provider"))
    rows = gensetup.test_purposes(cfg)
    for r in rows:
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"  {mark} {r['purpose']:<10} {r['seconds']:>5.1f}s"
              + (f"  {r['error']}" if r.get("error") else ""))
    if args.json:
        print(json.dumps(rows, indent=2))
    return 0 if all(r["ok"] for r in rows) else 1


def main(argv: list[str] | None = None) -> int:
    import sys as _sys

    args = build_parser().parse_args(list(argv if argv is not None else _sys.argv[1:]))
    if args.root:
        import os as _os

        from .paths import ROOT_ENV

        _os.environ[ROOT_ENV] = str(Path(args.root).expanduser().resolve())
    if args.store:
        import os as _os

        _os.environ["CRIB_INDEX_DIR"] = str(Path(args.store).expanduser().resolve())
    if args.writable:
        import os as _os

        from .access import WRITABLE_ENV

        _os.environ[WRITABLE_ENV] = ",".join(args.writable)
    # --json is for machines: never styled, whatever the terminal
    _st.configure("never" if args.json else args.color)

    if args.mcp or args.cmd == "serve":
        host, port = _resolve_serve_endpoint(args)
        from .server import main as serve

        tools = _resolve_tool_spec(args)
        transport = "http" if args.http else "stdio"
        serve(transport, host, port, tools)
        return 0
    if args.cmd is None:
        build_parser().print_help()
        return 1
    if args.cmd == "info":
        cmd_info(args.json)
        return 0
    if args.cmd == "bundle":
        from .errors import CribUserError

        try:
            return _bundle_cmd(args)
        except CribUserError as e:
            print(f"crib bundle: {e}", file=sys.stderr)
            return 2
    if args.cmd == "facets":
        # Local: it reads and conforms this machine's store, never through the
        # daemon (which serves the facets it was started with).
        return _facets_cmd(args)
    if args.cmd == "generate":
        # Local and interactive: it reads and writes this machine's config files
        # and talks to the providers directly, never through the daemon.
        return _generate_cmd(args)
    if args.cmd == "store" and args.store_cmd == "install-extensions":
        # Runs locally (no daemon, no store file): it only fills crib's own
        # extension directory, which every crib DuckDB connection reads.
        from .paths import Paths
        from .store_quack import install_extensions

        ext = Paths.resolve().duckdb_extension_dir
        names = install_extensions(ext)
        print(f"installed {', '.join(names)} into {ext}")
        return 0
    if args.cmd == "merge-driver":
        # git invokes this per-file during a merge — stay light, no config/daemon
        from .merge import run_driver

        return run_driver(args.base, args.current, args.other)
    # a noun with no verb (`crib note`) → point at its subcommands. `design`/`plan`
    # are NOT here: a bare one is a real command (`list`), per _BARE_NOUN_DEFAULT.
    if (
        args.cmd in ("note", "code", "learning")
        and getattr(args, f"{args.cmd}_verb", None) is None
    ):
        print(
            f"crib {args.cmd}: choose a subcommand (try `crib {args.cmd} --help`)",
            file=sys.stderr,
        )
        return 2

    from .config import Config
    from .paths import Paths

    cfg = Config.load(Paths.resolve().config_file)
    if args.cmd == "memory" and getattr(args, "memory_verb", None) in (
        "setup",
        "sync",
        "push",
        "pull",
    ):
        return _run_git(args, cfg)
    if args.cmd == "project" and args.project_verb == "delete":
        # Deletion is ALWAYS human-confirmed: MCP mode elicits (the tool refuses
        # without an affirmation); the CLI confirms HERE, in the terminal
        # (`--yes` for scripts, piped stdin without it refuses). The verb runs
        # in-process even when a daemon runs — the tool-level elicitation is
        # not the CLI's confirmation — with a pre-flight fence against the
        # daemon's in-flight sweep (attached daemons only: the fence must
        # never SPAWN one, whose startup reconcile would race the delete).
        # Inside the try: a fenced refusal (default project, missing name,
        # conflict) is a user error with the same formatting as every other
        # refusal — never a raw traceback, and --json gets the error payload.
        try:
            proj = _proj_name_required(args, "delete")
            _daemon_sweep_fence(cfg, proj)
            if not _confirm_delete_terminal(args, proj):
                return 2
            _run_inprocess(args, cfg)
            if _daemon_is_up(cfg):
                # a warm daemon holds per-project state this process cannot
                # clean (code-watcher roots, mirror snapshot, warm caches):
                # say so rather than leave it silent
                print(
                    f"crib: a running daemon still holds warm state for "
                    f"{proj!r} (watchers/caches) — restart the daemon to "
                    "drop it (its index tiers were already cleaned here)",
                    file=sys.stderr,
                )
            return 0
        except _user_errors() as e:
            msg = str(e)
            print(f"crib: {msg}", file=sys.stderr)
            if getattr(args, "json", False):
                print(json.dumps({"error": msg}))
            return 2
    try:
        from .paths import confined_root

        # confined mode (--root) runs verbs IN-PROCESS: a sandbox has no warm
        # daemon (and no sharedserver) to attach to, and must not reach one
        if cfg.daemon.enabled and not args.no_daemon and confined_root() is None:
            from . import sharedserver

            if not sharedserver.available():
                print(
                    "crib: daemon mode requires the 'sharedserver' binary on PATH "
                    "(install it, set [daemon].enabled = false, or pass --no-daemon)",
                    file=sys.stderr,
                )
                return 1
            _run_daemon(args, cfg)
        else:
            _run_inprocess(args, cfg)
    except _user_errors() as e:
        # An ambiguous symbol is an ordinary, expected answer (≈12% of bare names),
        # not a crash — a traceback buries the candidate list under stack frames.
        # Safe to swallow on the daemon path too: those frames are fastmcp client
        # plumbing, and the real stack is in the daemon's log, not here.
        msg = (
            str(e).split(": ", 1)[-1]
            if str(e).startswith("Error calling tool")
            else str(e)
        )
        print(f"crib: {msg}", file=sys.stderr)
        # Under --json a stderr-only refusal leaves stdout EMPTY, so a stdout-parsing
        # consumer gets an unparseable blank with no reason. Emit the error as JSON
        # too (stderr stays the human channel; exit code is still 2).
        if getattr(args, "json", False):
            print(json.dumps({"error": msg}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
