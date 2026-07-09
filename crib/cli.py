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
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .app import Crib


def _read_content(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


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
        from rich.console import Console
        from rich.markdown import Markdown

        from llmkit.md.render.cli import _load_theme
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
        loc = (f":{h.get('line_start')}-{h.get('line_end')}"
               if h.get("line_start") else "")
        head = f" — {h['heading']}" if h.get("heading") else ""
        print(f"\n[{h.get('score', 0.0):.3f}] {h.get('relpath', '')}{loc}{head}")
        _render_markdown(h.get("section") or "")


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
            return asdict(o) if is_dataclass(o) else str(o)
        print(json.dumps(obj, indent=2, default=default))
        return
    _emit_human(obj)


def _emit_human(obj: Any) -> None:
    if isinstance(obj, list):
        for item in obj:
            _emit_human_one(item)
    else:
        _emit_human_one(obj)


def _emit_human_one(item: Any) -> None:
    from .app import LookupHit
    # Normalize a daemon's dict-shaped lookup hit to the same fields as the
    # in-process LookupHit dataclass so both render identically.
    if isinstance(item, dict) and "score" in item and "snippet" in item:
        item = LookupHit(
            project=item.get("project", ""), relpath=item.get("relpath", ""),
            heading=item.get("heading", ""), title=item.get("title", ""),
            snippet=item.get("snippet", ""), score=item.get("score", 0.0),
            line_start=item.get("line_start"), line_end=item.get("line_end"))
    if isinstance(item, LookupHit):
        loc = f":{item.line_start}-{item.line_end}" if item.line_start else ""
        head = f"  {item.heading}" if item.heading else ""
        first = item.snippet.splitlines()[0][:100] if item.snippet else ""
        print(f"[{item.score:.3f}] {item.relpath}{loc}{head}\n    {first}")
    elif isinstance(item, dict) and ("relpath" in item or "from" in item):
        _emit_write_result(item)
    elif isinstance(item, dict):
        print("  ".join(f"{k}={v}" for k, v in item.items()))
    else:
        print(item)


def _emit_write_result(item: dict) -> None:
    """Echo a write/move result so the target namespace is never silent."""
    if "from" in item:                          # move
        f, t = item["from"], item["to"]
        print(f"moved  {f['project']}/{f['relpath']}  →  {t['project']}/{t['relpath']}")
    else:                                        # store/append/edit/forget
        proj, rel = item.get("project", "?"), item.get("relpath", "")
        verb = "removed" if item.get("removed") else "→ stored in"
        print(f"{verb}  {proj}/{rel}")
    if item.get("created"):
        print(f"  (created project '{item.get('project') or item['to']['project']}')")
    for s in item.get("similar") or []:
        print(f"  ⚠ similar [{s['score']:.3f}]: {s['relpath']}"
              + (f" — {s['heading']}" if s.get("heading") else ""))


def _emit_code(data: Any, verb: str, as_json: bool) -> None:
    """Human-readable rendering for the code verbs; raw JSON with the global --json."""
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    # implicit-resolution diagnostic (server echoes it on an empty sticky/seeded
    # result — see server._echo_list); render the note, not a blank hit row.
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) \
            and data[0].get("note") and "resolved" in data[0]:
        print(f"(0 matches) {data[0]['note']}"); return
    if verb == "code-index":
        if not isinstance(data, dict):
            print(data); return
        if data.get("skipped"):
            print(f"{data.get('file', '')}: {data['skipped']}"); return
        err = (f"  (descriptions_error: {data['descriptions_error']})"
               if data.get("descriptions_error") else "")
        print(f"{data.get('file', '')}: {data.get('symbols', 0)} symbols, "
              f"{data.get('described', 0)} described{err}")
        if data.get("store"):
            print(f"  → {data['store']}")
    elif verb == "code-lookup":
        if not data:
            print("(no matches — is this project code-indexed?)"); return
        for h in data:
            refs = len(h.get('references') or [])
            cg = (f"  {len(h.get('called_by') or [])}←/{len(h.get('calls') or [])}→"
                  + (f"/{refs}⇐" if refs else ""))
            print(f"[{h.get('rank', '?')}] {h.get('kind', ''):8} {h.get('fqname', '')}"
                  f"  {h.get('file', '')}:{h.get('line', '')}{cg}")
            if h.get("description"):
                print(f"      {h['description']}")
            if h.get("learning"):
                _print_learning(h["learning"], "      ")
    elif verb == "code-xref":
        if not data:
            print("(symbol not found in the symbol_index)"); return
        for e in data:
            print(f"{e.get('fqname', '')}  ({e.get('kind', '')})  "
                  f"{e.get('file', '')}:{e.get('line', '')}")
            for c in e.get("called_by") or []:
                print(f"   ← {c}")
            for c in e.get("calls") or []:
                print(f"   → {c}")
            for c in e.get("references") or []:      # ⇐ = referenced by (broader than a call)
                print(f"   ⇐ {c}")
            if e.get("learning"):
                _print_learning(e["learning"], "   ")


def _emit_status(d: Any, as_json: bool) -> None:
    """Human summary for `crib status`: backend + git lines, live LSP sessions,
    in-flight indexing, then a per-project inventory table."""
    if as_json:
        _emit(d, True)
        return
    print(f"{'store':10} {d.get('store')}  embed: {d.get('embed_model')}")
    g = d.get("git") or {}
    if g.get("enabled"):
        parts = [g.get("remote") or "no remote",
                 "clean" if not g.get("dirty") else f"{g['dirty']} uncommitted"]
        if "ahead" in g:
            parts.append(f"↑{g['ahead']} ↓{g['behind']}")
        print(f"{'git':10} " + "  ".join(parts))
        if g.get("last_commit"):
            print(f"{'':10} last: {g['last_commit']}")
    else:
        print(f"{'git':10} not enabled (crib setup --remote <url>)")
    for s in d.get("lsp_sessions") or []:
        state = "busy" if s.get("busy") else f"idle {s.get('idle_s', 0):.0f}s"
        alive = "" if s.get("alive") else "  DEAD"
        print(f"{'lsp':10} {s.get('server')}  {s.get('root')}  "
              f"pid {s.get('pid')}  {state}{alive}")
    for proj, sw in (d.get("sweeps") or {}).items():
        print(f"{'sweep':10} {proj}: {sw.get('done', 0)}/{sw.get('total', 0)} files")
    for proj, files in (d.get("indexing") or {}).items():
        print(f"{'indexing':10} {proj}: {', '.join(files)}")
    projs = d.get("projects") or []
    print(f"{'projects':10} {len(projs)}")
    if projs:
        w = max(len(p["project"]) for p in projs)
        for p in projs:
            print(f"  {p['project']:{w}}  notes {p['notes']:4}  "
                  f"docs {p['doc_chunks']:4}  symbols {p['symbols']:5}  "
                  f"learnings {p['learnings']:3}")


def _emit_project(d: Any, verb: str | None, as_json: bool) -> None:
    """Human summary for `crib project <verb>`."""
    if as_json:
        print(json.dumps(d, indent=2, default=str)); return
    if not isinstance(d, dict):
        print(d); return
    proj = d.get("project", "")
    if verb == "status":
        state = "indexed" if d.get("indexed") else "NOT indexed"
        print(f"{proj}: {state} — {d.get('symbols', 0)} symbols "
              f"in {d.get('files', 0)} files")
        kinds = d.get("kinds") or {}
        if kinds:
            print("  " + ", ".join(f"{k}:{n}" for k, n in sorted(kinds.items())))
        if d.get("paths"):
            print(f"  paths: {', '.join(d['paths'])}")
        return
    if verb == "forget":
        print(f"{proj}: cleared {d.get('symbols_removed', 0)} symbols"
              + (f", {d['learnings_removed']} learnings" if d.get("learnings_removed") else ""))
        return
    # setup / index
    made = "  (created .crib)" if d.get("crib_created") else ""
    docs = f", {d['docs_imported']} docs imported" if d.get("docs_imported") else ""
    print(f"{proj}: indexed {d.get('files_indexed', 0)}/{d.get('files_seen', 0)} files, "
          f"{d.get('symbols', 0)} symbols, {d.get('described', 0)} described{docs}{made}")
    errs = d.get("errors") or []
    if errs:
        print(f"  {len(errs)} file(s) errored (first: {errs[0].get('file', '')})")


def _emit_code_dossier(d: Any, as_json: bool) -> None:
    """Full single-symbol view: header + description + annotated neighbours + learning."""
    if as_json:
        print(json.dumps(d, indent=2, default=str)); return
    if not d or not d.get("fqname"):
        print("(symbol not found — is this project code-indexed?)"); return
    print(f"{d['fqname']}  ({d.get('kind', '')})  {d.get('file', '')}:{d.get('line', '')}")
    if d.get("signature"):
        print(f"  {d['signature']}")
    if d.get("description"):
        print(f"  {d['description']}")
    if d.get("learning"):
        _print_learning(d["learning"], "  ")
    for label, arrow in (("called_by", "←"), ("calls", "→"), ("references", "⇐")):
        rows = d.get(label) or []
        if rows:
            print(f"  {label} {arrow}")
            for r in rows:
                desc = f"  — {r['description']}" if r.get("description") else ""
                print(f"     {r.get('symbol', '')}{desc}")


def _print_learning(learning: dict, indent: str) -> None:
    """Render an attached symbol learning (📌) under a code-lookup/xref hit."""
    flag = "  ⚠ stale — body changed since written" if learning.get("stale") else ""
    print(f"{indent}📌 note ({learning.get('relpath', '')}){flag}")
    for line in (learning.get("body") or "").splitlines():
        print(f"{indent}  {line}" if line.strip() else "")


def _emit_code_learning(data: Any, verb: str, as_json: bool) -> None:
    """Confirmation/print for the symbol-learning verbs (append/edit/forget/read)."""
    if as_json:
        print(json.dumps(data, indent=2, default=str)); return
    sym, rel = data.get("symbol", ""), data.get("relpath", "")
    if verb == "code-read":
        if not data.get("found"):
            print(f"(no learning for {sym})"); return
        print(f"# {sym}  [{rel}]\n{(data.get('body') or '').strip()}"); return
    if verb == "code-forget":
        print(f"forgot {sym}  ({rel})"); return
    if verb == "code-reaffirm":
        print(f"reaffirmed {sym} (cleared ⚠ stale)  → {rel}"); return
    if verb == "code-append":
        print(f"{'created' if data.get('created') else 'appended'} learning: {sym}  → {rel}")
        return
    print(f"edited learning: {sym}  → {rel}")   # code-edit


def _emit_code_report(rows: Any, as_json: bool) -> None:
    """Health report for attached learnings (ok/moved/orphan)."""
    if as_json:
        print(json.dumps(rows, indent=2, default=str)); return
    if not rows:
        print("(no learnings recorded)"); return
    icon = {"ok": "·", "moved": "~", "orphan": "✗"}
    for r in rows:
        st = r.get("status", "")
        line = f"{icon.get(st, '?')} {st:7} {r.get('symbol', '')}"
        if st == "moved":
            line += f"   {r.get('file', '')} → {r.get('new_file', '')}"
        elif st == "orphan":
            line += f"   (was {r.get('file', '')})"
        print(line)
    bad = sum(1 for r in rows if r.get("status") != "ok")
    if bad:
        print(f"\n{bad} need attention — `crib code-rehome <fqn>` for suggestions, "
              f"or `crib code-forget <fqn>`")


def _emit_code_rehome(data: Any, as_json: bool) -> None:
    """Ranked rehome candidates (no target) or a move confirmation."""
    if as_json:
        print(json.dumps(data, indent=2, default=str)); return
    if "candidates" in data:
        print(f"rehome {data.get('old', '')} → candidates:")
        cands = data.get("candidates") or []
        if not cands:
            print("  (none — `crib code-forget` if it's truly gone)"); return
        for c in cands:
            print(f"  [{c.get('score', '')}] {c.get('fqname', '')}   {c.get('file', '')}")
        print(f"\nconfirm: crib code-rehome {data.get('old', '')} <fqname>")
        return
    print(f"rehomed {data.get('old', '')} → {data.get('new', '')}  ({data.get('relpath', '')})")


def _graph_direction(args: Any) -> str:
    """--references > --callers > default callees."""
    if getattr(args, "references", False):
        return "references"
    return "callers" if getattr(args, "callers", False) else "callees"


def _emit_code_graph(tree: Any, args: Any) -> None:
    """pstree-style call graph (modeled on zdot's hook graph). `--json` = raw tree.
    `↑` marks a DAG node already shown; `·ext` an edge target outside the index."""
    if getattr(args, "json", False):
        print(json.dumps(tree, indent=2, default=str)); return
    if not tree:
        print("(symbol not found — is this project code-indexed?)"); return
    direction = _graph_direction(args)
    ascii_mode = getattr(args, "ascii", False)
    arrows = {"callees": (">", "▸"), "callers": ("<", "◂"), "references": ("=", "⇐")}
    arrow = arrows[direction][0 if ascii_mode else 1]
    if ascii_mode:
        branch, last, vert, blank = "|-", "`-", "|  ", "   "
    else:
        branch, last, vert, blank = "├─", "└─", "│  ", "   "
    pin = " *" if ascii_mode else " 📌"          # step 3: node carries a learning
    print(f"{tree['fqname']}  ({tree.get('kind', '')})   [{direction}]"
          f"{pin if tree.get('has_learning') else ''}")

    def render(node: dict, prefix: str) -> None:
        kids = node.get("children") or []
        for i, c in enumerate(kids):
            islast = i == len(kids) - 1
            conn = last if islast else branch
            tag = " ↑" if c.get("repeat") else (" ·ext" if c.get("external") else "")
            if c.get("has_learning"):
                tag += pin
            loc = (f"   {c.get('file', '')}:{c.get('line', '')}"
                   if c.get("line") and not c.get("external") else "")
            print(f"{prefix}{conn}{arrow} {c.get('fqname', '')}{tag}{loc}")
            render(c, prefix + (blank if islast else vert))

    render(tree, "")


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    p = argparse.ArgumentParser(prog="crib", description="markdown memory")
    p.add_argument("--version", action="version", version=f"crib {__version__}")
    p.add_argument("--mcp", action="store_true", help="run the MCP server")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    # transport options (apply to --mcp and `serve`; also pick the daemon the CLI
    # attaches to). Default to None so config `[daemon]` (host/port) wins unless
    # the user overrides on the command line.
    p.add_argument("--http", action="store_true",
                   help="serve MCP over HTTP instead of stdio")
    p.add_argument("--host", default=None, help="HTTP host (bind, or daemon to attach)")
    p.add_argument("--port", type=int, default=None,
                   help="HTTP port (bind, or daemon to attach)")
    # CLI verbs attach to the warm daemon by default; --no-daemon runs in-process.
    p.add_argument("--no-daemon", action="store_true",
                   help="run the verb in-process instead of via the daemon")
    sub = p.add_subparsers(dest="cmd")

    def proj(sp):  # shared project selectors
        sp.add_argument("-p", "--project")            # by NAME
        sp.add_argument("-P", "--project-path",       # by PATH (resolve .crib from here
                        dest="project_path")          # instead of the actual cwd)

    sv = sub.add_parser("serve", help="run the MCP server (stdio or --http)")
    sv.add_argument("--http", action="store_true")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sub.add_parser("info", help="show resolved paths and available backends")
    sub.add_parser("status", help="health summary: projects (notes/docs/code/"
                                  "learnings), git sync, LSP sessions, indexing")

    # `crib project <verb>` — whole-project lifecycle (superset of code + notes)
    pj = sub.add_parser("project", help="onboard/index a whole repo (setup/index/"
                                        "status/forget)")
    pjsub = pj.add_subparsers(dest="project_verb")
    for _v, _h in (("setup", "ensure .crib + import docs + index all code"),
                   ("index", "(re)index the repo's code from its .crib"),
                   ("status", "is it indexed? counts, kinds, .crib paths")):
        _sp = pjsub.add_parser(_v, help=_h)
        proj(_sp)
    _pf = pjsub.add_parser("forget", help="clear the code index (keeps learnings/notes)")
    proj(_pf)
    _pf.add_argument("--with-learnings", action="store_true",
                     help="also drop attached learnings (default: keep them)")
    pjsub.add_parser("list", help="list projects (separate memory namespaces)")
    _pu = pjsub.add_parser("use", help="set this session's current project")
    _pu.add_argument("project")
    _pc = pjsub.add_parser("current", help="show this session's current project")
    proj(_pc)
    pjsub.add_parser("reconcile", help="sweep all projects for offline changes")

    # noun groups mirroring `project`: note / code / learning (verbs nest under them)
    n_note = sub.add_parser("note", help="memory notes: search, read, write, share")
    notesub = n_note.add_subparsers(dest="note_verb", required=True)
    n_code = sub.add_parser("code", help="code symbol index: search + navigate")
    codesub = n_code.add_subparsers(dest="code_verb", required=True)
    n_learn = sub.add_parser("learning",
                             help="durable learnings attached to code symbols")
    learnsub = n_learn.add_subparsers(dest="learning_verb", required=True)

    s = notesub.add_parser("lookup", aliases=["search"], help="semantic search")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--tag", action="append", dest="tags")
    s.add_argument("--keywords",
                   help="comma-separated keyword_index labels to fold into BM25 "
                        "for this query (overrides [retrieve].keyword_labels)")
    s.add_argument("--keyword-weight", type=float, default=None, dest="keyword_weight",
                   help="weight of keyword_index tokens vs body in BM25 "
                        "(overrides [retrieve].keyword_weight)")
    s.add_argument("--summaries",
                   help="comma-separated summary_index labels to fold in as dense "
                        "alias vectors (overrides [retrieve].summary_labels)")
    s.add_argument("--summary-weight", type=float, default=None, dest="summary_weight",
                   help="RRF fusion weight of the summary alias ranking "
                        "(overrides [retrieve].summary_weight)")
    s.add_argument("-a", "--render", action="store_true",
                   help="render each matched section as markdown (like `apropos`) "
                        "instead of compact locator lines")

    s = notesub.add_parser("apropos", aliases=["a"],
                       help="semantic search, rendering each full matched section "
                            "(alias for `search --render`, fewer hits)")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--tag", action="append", dest="tags")

    s = codesub.add_parser("lookup",
                       help="find a code symbol by concept OR name (hybrid dense+kw)")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=8)

    s = codesub.add_parser("xref",
                       help="a symbol's callers/callees/references from the symbol_index")
    s.add_argument("symbol"); proj(s)

    s = codesub.add_parser("dossier",
                       help="everything about one symbol (+ neighbour descriptions)")
    s.add_argument("symbol"); proj(s)

    s = codesub.add_parser("graph",
                       help="pstree-style call graph around a symbol (recursive)")
    s.add_argument("symbol"); proj(s)
    s.add_argument("--callers", action="store_true",
                   help="what CALLS the symbol (default: what it calls)")
    s.add_argument("--references", action="store_true",
                   help="everywhere the symbol is REFERENCED (broader than calls)")
    s.add_argument("--depth", type=int, default=6)
    s.add_argument("--ascii", action="store_true", help="ASCII glyphs, no box-drawing")

    s = codesub.add_parser("index",
                       help="index a source file: symbols + call graph + descriptions")
    s.add_argument("path"); proj(s)

    s = learnsub.add_parser("add",
                       help="attach a durable learning to a code symbol ('-' reads stdin)")
    s.add_argument("symbol"); s.add_argument("text"); proj(s)

    s = learnsub.add_parser("edit",
                       help="rewrite a symbol's learning body ('-' reads stdin)")
    s.add_argument("symbol"); s.add_argument("text"); proj(s)

    s = learnsub.add_parser("forget",
                       help="remove a symbol's learning (recoverable; works on orphans)")
    s.add_argument("symbol"); proj(s)

    s = learnsub.add_parser("read", help="print a symbol's attached learning")
    s.add_argument("symbol"); proj(s)

    s = learnsub.add_parser("reaffirm",
                       help="clear a learning's ⚠ stale flag without rewriting it")
    s.add_argument("symbol"); proj(s)

    s = learnsub.add_parser("report",
                       help="health report for attached learnings (ok/moved/orphan)")
    proj(s)
    s.add_argument("--orphans", action="store_true",
                   help="only the actionable ones (moved/orphan)")

    s = learnsub.add_parser("rehome",
                       help="re-point an orphaned learning (no target = ranked suggestions)")
    s.add_argument("old"); s.add_argument("new", nargs="?"); proj(s)

    s = notesub.add_parser("read", help="print a note's raw markdown")
    s.add_argument("relpath"); proj(s)

    s = notesub.add_parser("locate", help="print a note's on-disk path")
    s.add_argument("relpath"); proj(s)

    s = notesub.add_parser("store", help="create a new note ('-' reads stdin)")
    s.add_argument("content"); proj(s)
    s.add_argument("--title")
    s.add_argument("--tag", action="append", dest="tags")

    s = notesub.add_parser("append", help="append to a note ('-' reads stdin)")
    s.add_argument("relpath"); s.add_argument("content"); proj(s)
    s.add_argument("--heading")

    s = notesub.add_parser("edit", help="replace a note's content (stdin by default)")
    s.add_argument("relpath"); s.add_argument("content", nargs="?", default="-"); proj(s)

    s = notesub.add_parser("forget", help="delete a note (recoverable via the ring)")
    s.add_argument("relpath"); proj(s)

    s = notesub.add_parser("move", help="move/rename a note across projects (keeps id)")
    s.add_argument("relpath"); proj(s)
    s.add_argument("--to-project", dest="to_project")
    s.add_argument("--to-relpath", dest="to_relpath")

    s = notesub.add_parser("reindex", help="reindex a note or whole project")
    s.add_argument("relpath", nargs="?"); proj(s)


    s = notesub.add_parser("versions", help="list recoverable versions of a note")
    s.add_argument("relpath"); proj(s)

    s = notesub.add_parser("restore", help="restore a prior version of a note")
    s.add_argument("relpath"); s.add_argument("version"); proj(s)

    s = notesub.add_parser("import",
                       help="copy explicit files into memory as crib-owned notes")
    s.add_argument("paths", nargs="+", help="files to copy into memory")
    proj(s)

    s = notesub.add_parser("import-memory",
                       help="mirror Claude Code's harness memory into a crib project")
    proj(s)

    s = notesub.add_parser("distill",
                       help="LLM-revise a note in place (compress/dedupe/normalize)")
    s.add_argument("relpath"); proj(s)

    # elaborate (keyword_index/BM25) and summarize (summary_index/dense aliases)
    # share one arg shape — label + optional note + --overwrite — differing only in
    # which section-index they populate; the two prompts/dispatch split downstream.
    for _v, _h in (
        ("elaborate", "keyword_index: generate BM25 search terms per section "
                      "(keywords/questions/phrase/…) for a note or project"),
        ("summarize", "summary_index: generate dense alias rephrasings per "
                      "section for a note or project")):
        _s = notesub.add_parser(_v, help=_h)
        _s.add_argument("label")
        _s.add_argument("relpath", nargs="?"); proj(_s)
        _s.add_argument("--overwrite", action="store_true",
                        help="regenerate even if it already exists")

    # `crib memory <verb>` — the whole memory store's git lifecycle. These act on
    # the entire data tree (every project's notes + learnings), not a note or a
    # project, so they live under their own top-level noun, over `GitBacking`.
    n_memory = sub.add_parser("memory",
                              help="the memory store's git lifecycle: snapshot + sync")
    memsub = n_memory.add_subparsers(dest="memory_verb", required=True)

    s = memsub.add_parser("snapshot", help="git checkpoint of the whole data tree")
    s.add_argument("-m", "--message")

    s = memsub.add_parser("setup",
                       help="join the shared memory repo on this machine "
                            "(set remote + frontmatter merge driver, then pull)")
    s.add_argument("--remote", help="git remote URL to join (prompted if omitted)")

    s = memsub.add_parser("sync",
                       help="share memory via git: commit + pull + push, then reindex")
    s.add_argument("-m", "--message")
    s.add_argument("--remote", help="bootstrap: git init + set origin to this URL")
    memsub.add_parser("push", help="push local commits to the remote")
    memsub.add_parser("pull", help="pull from the remote, then reindex")

    s = memsub.add_parser("history", help="git history for the tree (or a note)")
    s.add_argument("relpath", nargs="?")

    # internal: invoked by git as the cribnote merge driver (DESIGN §14). No
    # help= → kept out of the listed commands (still a valid hidden subcommand).
    s = sub.add_parser("merge-driver")
    s.add_argument("base")        # %O ancestor
    s.add_argument("current")     # %A ours / output file
    s.add_argument("other")       # %B theirs
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
        "chromadb": importlib.util.find_spec("chromadb") is not None,
        "fastembed": importlib.util.find_spec("fastembed") is not None,
        "sentence_transformers":
            importlib.util.find_spec("sentence_transformers") is not None,
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
            "hybrid": config.retrieve.hybrid, "rrf_k": config.retrieve.rrf_k,
            "rerank": config.retrieve.rerank, "rerank_model": config.retrieve.rerank_model,
        },
        "chroma_mode": config.chroma.mode,
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
    for k in ("config_dir", "data_dir", "index_dir", "embed_model",
              "chroma_mode", "default_project"):
        print(f"{k:18} {info[k]}")
    ck = config.chunk
    print(f"{'chunk':18} {ck.window_words}w window, "
          f"{ck.overlap_words}w overlap ({ck.overlap_ratio:.0%})")
    rt = config.retrieve
    rr = f" + rerank ({rt.rerank_model.split('/')[-1]})" if rt.rerank else ""
    print(f"{'retrieve':18} {'hybrid (dense+BM25, RRF)' if rt.hybrid else 'dense only'}{rr}")
    print(f"{'daemon':18} {'on' if d.enabled else 'off'}  "
          f"http://{d.host}:{d.port}/mcp  ({d.name}, grace {d.grace_period})")
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
@dataclass(frozen=True)
class Verb:
    tool: str                                   # MCP tool name (daemon path)
    build: Callable[[Any], dict[str, Any]]      # parsed args → logical call params
    emit: Callable[[Any, Any], None]            # (result, parsed args) → stdout
    method: str = ""                            # Crib method (in-process); "" ⇒ tool
    is_async: bool = False                      # in-process wraps in asyncio.run
    wants_cwd: bool = True                       # append project_path / cwd

    def crib_method(self) -> str:
        return self.method or self.tool


# emit adapters — normalize every emitter to the same (data, args) signature
def _E(d, a): _emit(d, a.json)                                   # generic
def _E_raw(d, a): print(d)                                      # verbatim (locate/snapshot)
def _E_note(d, a): _print_note(d, a.json)
def _E_apropos(d, a): _emit_apropos(d, a.json)
def _E_status(d, a): _emit_status(d, a.json)
def _E_dossier(d, a): _emit_code_dossier(d, a.json)
def _E_report(d, a): _emit_code_report(d, a.json)
def _E_rehome(d, a): _emit_code_rehome(d, a.json)
def _E_graph(d, a): _emit_code_graph(d, a)
def _E_code(verb): return lambda d, a: _emit_code(d, verb, a.json)
def _E_learning(verb): return lambda d, a: _emit_code_learning(d, verb, a.json)


def _b_lookup(a: Any) -> dict[str, Any]:
    """`lookup` call params — the keyword/summary label + weight overrides fold in
    only when given (absent ⇒ the method/[retrieve] default applies)."""
    call = {"query": a.query, "project": a.project, "k": a.k, "tags": a.tags}
    if getattr(a, "keywords", None):
        call["keyword_labels"] = _split_labels(a.keywords)
    if getattr(a, "keyword_weight", None) is not None:
        call["keyword_weight"] = a.keyword_weight
    if getattr(a, "summaries", None):
        call["summary_labels"] = _split_labels(a.summaries)
    if getattr(a, "summary_weight", None) is not None:
        call["summary_weight"] = a.summary_weight
    return call


VERBS: dict[str, Verb] = {
    # notes: search / read
    "note lookup": Verb("lookup", _b_lookup, _E),
    "note apropos": Verb("apropos", lambda a: {"query": a.query, "project": a.project,
                                          "k": a.k, "tags": a.tags}, _E_apropos),
    "note read": Verb("read", lambda a: {"relpath": a.relpath, "project": a.project},
                 _E_note, method="read_note"),
    "note locate": Verb("locate", lambda a: {"relpath": a.relpath, "project": a.project},
                   _E_raw),
    # notes: write
    "note store": Verb("store", lambda a: {"content": _read_content(a.content),
                                      "title": a.title, "project": a.project,
                                      "tags": a.tags}, _E, method="store_note",
                  is_async=True),
    "note append": Verb("append", lambda a: {"relpath": a.relpath,
                                        "content": _read_content(a.content),
                                        "heading": a.heading, "project": a.project},
                   _E, method="append_note", is_async=True),
    "note edit": Verb("edit", lambda a: {"relpath": a.relpath,
                                    "new_content": _read_content(a.content),
                                    "project": a.project}, _E,
                 method="edit_note", is_async=True),
    "note forget": Verb("forget", lambda a: {"relpath": a.relpath, "project": a.project},
                   _E, is_async=True),
    "note move": Verb("move", lambda a: {"relpath": a.relpath, "to_project": a.to_project,
                                    "to_relpath": a.to_relpath, "project": a.project},
                 _E, method="move_note", is_async=True),
    "note reindex": Verb("reindex", lambda a: {"relpath": a.relpath, "project": a.project},
                    _E, is_async=True),
    "project reconcile": Verb("reconcile", lambda a: {}, _E, method="reconcile_all",
                      is_async=True, wants_cwd=False),
    "note versions": Verb("versions", lambda a: {"relpath": a.relpath, "project": a.project},
                     _E, method="list_versions"),
    "note restore": Verb("restore", lambda a: {"relpath": a.relpath, "version": a.version,
                                          "project": a.project}, _E, is_async=True),
    "note import": Verb("import", lambda a: {"paths": a.paths, "project": a.project},
                   _E, method="import_files", is_async=True),
    "note import-memory": Verb("import_memory", lambda a: {"project": a.project}, _E,
                          method="import_claude_memory", is_async=True),
    "note distill": Verb("distill", lambda a: {"relpath": a.relpath, "project": a.project},
                    _E, is_async=True),
    "note elaborate": Verb("elaborate", lambda a: {"label": a.label, "relpath": a.relpath,
                                              "project": a.project,
                                              "overwrite": a.overwrite}, _E,
                      is_async=True),
    "note summarize": Verb("summarize", lambda a: {"label": a.label, "relpath": a.relpath,
                                              "project": a.project,
                                              "overwrite": a.overwrite}, _E,
                      is_async=True),
    "memory snapshot": Verb("snapshot", lambda a: {"message": a.message}, _E_raw,
                     wants_cwd=False),
    "memory history": Verb("history", lambda a: {"relpath": a.relpath}, _E, wants_cwd=False),
    "project list": Verb("projects", lambda a: {}, _E, wants_cwd=False),
    "project use": Verb("use_project", lambda a: {"project": a.project}, _E,
                        method="use_project", wants_cwd=False),
    "project current": Verb("current_project", lambda a: {}, _E,
                            method="current_project"),
    "status": Verb("status", lambda a: {}, _E_status, wants_cwd=False),
    # code index
    "code lookup": Verb("code_lookup", lambda a: {"query": a.query,
                                                 "project": a.project, "k": a.k},
                        _E_code("code-lookup")),
    "code xref": Verb("code_xref", lambda a: {"symbol": a.symbol, "project": a.project},
                      _E_code("code-xref")),
    "code dossier": Verb("code_dossier", lambda a: {"symbol": a.symbol,
                                                   "project": a.project}, _E_dossier),
    "code graph": Verb("code_graph", lambda a: {"symbol": a.symbol,
                                               "direction": _graph_direction(a),
                                               "depth": a.depth, "project": a.project},
                       _E_graph),
    "code index": Verb("code_index",
                       lambda a: {"path": str(Path(a.path).expanduser().resolve()),
                                  "project": a.project},
                       _E_code("code-index"), is_async=True),
    # code learnings
    "learning add": Verb("code_append", lambda a: {"symbol": a.symbol,
                                                 "text": _read_content(a.text),
                                                 "project": a.project},
                        _E_learning("code-append"), is_async=True),
    "learning edit": Verb("code_edit", lambda a: {"symbol": a.symbol,
                                             "new_content": _read_content(a.text),
                                             "project": a.project},
                      _E_learning("code-edit"), is_async=True),
    "learning forget": Verb("code_forget", lambda a: {"symbol": a.symbol,
                                                 "project": a.project},
                        _E_learning("code-forget"), is_async=True),
    "learning read": Verb("code_read", lambda a: {"symbol": a.symbol, "project": a.project},
                      _E_learning("code-read")),
    "learning reaffirm": Verb("code_reaffirm", lambda a: {"symbol": a.symbol,
                                                     "project": a.project},
                          _E_learning("code-reaffirm"), is_async=True),
    "learning report": Verb("code_learnings", lambda a: {"project": a.project,
                                                       "orphans_only": a.orphans},
                           _E_report),
    "learning rehome": Verb("code_rehome", lambda a: {"old_fqn": a.old, "new_fqn": a.new,
                                                 "project": a.project}, _E_rehome,
                        is_async=True),
}


# Verb.tool historically doubled as BOTH the MCP tool name and the Crib method (they
# matched). After the noun-verb rename they diverge: the MCP tool is the nested key
# underscored (`note lookup` → `note_lookup`), the Crib method stays the old tool name
# (or the explicit `method=`). Split them here so no row needs editing (Verb is frozen).
VERBS = {
    _key: replace(_v, method=_v.method or _v.tool,
                  tool=_key.replace(" ", "_").replace("-", "_"))
    for _key, _v in VERBS.items()
}


def _cwd_of(args: Any) -> Path:
    """The caller's project anchor: -P/--project-path overrides the actual cwd."""
    return (Path(args.project_path).expanduser()
            if getattr(args, "project_path", None) else Path.cwd())


def _resolve_verb(args: Any) -> tuple[Verb, dict[str, Any]]:
    """Map parsed args to a (Verb, call-params) pair. `crib <noun> <verb>` keys the
    registry by "<noun> <verb>"; a bare top-level verb (status) keys by its name.
    Normalizes the note aliases (`search`→lookup, `a`→apropos) and routes
    `note lookup --render` to the apropos section-rendering path."""
    noun = args.cmd
    sub = getattr(args, f"{noun}_verb", None)
    if sub is None:                                    # flat top-level verb (status)
        entry = VERBS[noun]
        return entry, entry.build(args)
    sub = {"search": "lookup", "a": "apropos"}.get(sub, sub)
    if noun == "note" and sub == "lookup" and getattr(args, "render", False):
        sub = "apropos"
    entry = VERBS[f"{noun} {sub}"]
    return entry, entry.build(args)


def _dispatch(args: Any) -> tuple[Verb, dict[str, Any]]:
    """Route `project setup/index/status/forget` (and bare `project`) through the
    synthetic _project_verb; everything else — incl. `project list`/`reconcile` —
    through the registry."""
    if args.cmd == "project" and getattr(args, "project_verb", None) in (
            None, "setup", "index", "status", "forget"):
        return _project_verb(args)
    return _resolve_verb(args)


def _project_verb(args: Any) -> tuple[Verb, dict[str, Any]]:
    """`crib project <sub>` as a synthetic Verb — the four sub-verbs share one
    shape (differing only in tool/async), and one emitter keyed by the sub-verb."""
    pv = getattr(args, "project_verb", None) or "status"
    tool = {"setup": "project_setup", "index": "project_index",
            "status": "project_status", "forget": "project_forget"}[pv]
    call: dict[str, Any] = {"project": args.project}
    if pv == "forget":
        call["with_learnings"] = getattr(args, "with_learnings", False)
    emit = lambda d, a: _emit_project(d, pv, a.json)     # noqa: E731
    return Verb(tool, lambda a: call, emit, is_async=pv in ("setup", "index")), call


def _resolve_serve_endpoint(args: Any) -> tuple[str, int]:
    """Bind address for `serve`/`--mcp`: explicit flags win, else `[daemon]`."""
    from .config import Config
    from .paths import Paths

    cfg = Config.load(Paths.resolve().config_file)
    return (args.host or cfg.daemon.host, args.port or cfg.daemon.port)


def _run_daemon(args: Any, cfg: Any) -> None:
    """Run a verb via the warm daemon: build the call, ship the caller's cwd as
    `project_path`, call the MCP tool, and emit — all off one registry row."""
    from .client import DaemonClient

    entry, call = _dispatch(args)
    if entry.wants_cwd:
        call["project_path"] = str(_cwd_of(args))
    with DaemonClient(cfg.daemon) as client:
        data = client.call(entry.tool, call)
    entry.emit(data, args)


def _run_git(args: Any, cfg: Any) -> int:
    """Share notes via git. Runs git client-side (the user's terminal owns auth);
    a pull that changes files then triggers a reindex through the daemon (or
    in-process). Not an MCP tool — pushing notes is outward-facing + interactive."""
    from .gitbacking import GitBacking
    from .paths import Paths

    verb = getattr(args, "memory_verb", None)        # `crib memory setup/sync/push/pull`
    # setup runs on a fresh machine where the data dir may not exist yet
    paths = Paths.resolve().ensure() if verb == "setup" else Paths.resolve()
    git = GitBacking(paths.data_dir)

    if verb == "setup":
        remote = getattr(args, "remote", None) or git.current_remote() or _prompt_remote()
        if not remote:
            print("crib note setup: no remote given (pass --remote <url>)", file=sys.stderr)
            return 1
        print(f"joining {remote} …")
        res = git.setup(remote)
    elif verb == "sync":
        if getattr(args, "remote", None):
            print(git.init(args.remote))
        res = git.sync(args.message)
    elif verb == "push":
        res = git.push()
    else:
        res = git.pull()

    print(res.message)
    if res.conflicts:
        return 1
    if res.changed:                       # a pull rewrote notes → index must follow
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
                return client.call("reconcile", {})
    crib = Crib.open()
    try:
        return asyncio.run(crib.reconcile_all())
    finally:
        crib.close()


def _run_inprocess(args: Any) -> None:
    """Run a verb in-process against a Crib instance: same registry row as the
    daemon path, but call the Crib `method` with `cwd=<Path>` and wrap async ones
    in `asyncio.run` (the daemon does this awaiting server-side)."""
    entry, call = _dispatch(args)
    if entry.wants_cwd:
        call["cwd"] = _cwd_of(args)
    crib = Crib.open()
    try:
        method = getattr(crib, entry.crib_method())
        data = asyncio.run(method(**call)) if entry.is_async else method(**call)
        entry.emit(data, args)
    finally:
        crib.close()


def main(argv: list[str] | None = None) -> int:
    import sys as _sys
    args = build_parser().parse_args(
        list(argv if argv is not None else _sys.argv[1:]))

    if args.mcp or args.cmd == "serve":
        host, port = _resolve_serve_endpoint(args)
        from .server import main as serve
        transport = "http" if args.http else "stdio"
        serve(transport, host, port)
        return 0
    if args.cmd is None:
        build_parser().print_help()
        return 1
    if args.cmd == "info":
        cmd_info(args.json)
        return 0
    if args.cmd == "merge-driver":
        # git invokes this per-file during a merge — stay light, no config/daemon
        from .merge import run_driver
        return run_driver(args.base, args.current, args.other)
    # a noun with no verb (`crib note`) → point at its subcommands
    if args.cmd in ("note", "code", "learning") and \
            getattr(args, f"{args.cmd}_verb", None) is None:
        print(f"crib {args.cmd}: choose a subcommand (try `crib {args.cmd} --help`)",
              file=sys.stderr)
        return 2

    from .config import Config
    from .paths import Paths

    cfg = Config.load(Paths.resolve().config_file)
    if args.cmd == "memory" and getattr(args, "memory_verb", None) in (
            "setup", "sync", "push", "pull"):
        return _run_git(args, cfg)
    if cfg.daemon.enabled and not args.no_daemon:
        from . import sharedserver
        if not sharedserver.available():
            print("crib: daemon mode requires the 'sharedserver' binary on PATH "
                  "(install it, set [daemon].enabled = false, or pass --no-daemon)",
                  file=sys.stderr)
            return 1
        _run_daemon(args, cfg)
    else:
        _run_inprocess(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
