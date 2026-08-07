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
from ast import literal_eval
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .app import Crib


def _read_content(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


_EDITOR_HINT = (
    "# Write the body below. Lines starting with '#' are ignored, and an empty\n"
    "# file aborts.\n")


def _from_editor(what: str) -> str:
    """Open `$EDITOR` on a scratch file and return what came back.

    The escape hatch for the worst friction in this surface: a design decision is
    a paragraph or five, and shell-quoting prose is miserable enough that it
    quietly pushes people toward one-line decisions. Only ever reached on a tty
    with the body omitted — a pipeline reads stdin instead."""
    import os
    import subprocess
    import tempfile
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as fh:
        fh.write(f"\n{_EDITOR_HINT}# ({what})\n")
        path = Path(fh.name)
    try:
        subprocess.run([*editor.split(), str(path)], check=True)
        text = path.read_text()
    finally:
        path.unlink(missing_ok=True)
    body = "\n".join(ln for ln in text.splitlines()
                     if not ln.startswith("#")).strip()
    if not body:
        raise SystemExit(f"crib: empty body — {what} aborted")
    return body


def _body(value: str | None, file: str | None = None, *,
          what: str = "body", default: str | None = None) -> str:
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
        return default              # optional body (a plan item may be title-only)
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
        stale = "  ⚠︎ stale decision (a dep moved)" if h.get("tainted") else ""
        print(f"\n[{h.get('score', 0.0):.3f}] {h.get('relpath', '')}{loc}{head}{stale}")
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
            return asdict(o) if is_dataclass(o) else str(o)
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
        return bool(h.get("index_rebuilding") if isinstance(h, dict)
                    else getattr(h, "index_rebuilding", False))
    if isinstance(hits, list) and any(_flag(h) for h in hits):
        print("⚠︎ index rebuilding: this project is still re-embedding "
              "(see `crib status`) — results are incomplete")


def _emit_human_one(item: Any) -> None:
    from .app import LookupHit
    # Normalize a daemon's dict-shaped lookup hit to the same fields as the
    # in-process LookupHit dataclass so both render identically.
    if isinstance(item, dict) and "score" in item and "snippet" in item:
        item = LookupHit(
            project=item.get("project", ""), relpath=item.get("relpath", ""),
            heading=item.get("heading", ""), title=item.get("title", ""),
            snippet=item.get("snippet", ""), score=item.get("score", 0.0),
            line_start=item.get("line_start"), line_end=item.get("line_end"),
            index_rebuilding=bool(item.get("index_rebuilding")),
            tainted=bool(item.get("tainted")))
    if isinstance(item, LookupHit):
        loc = f":{item.line_start}-{item.line_end}" if item.line_start else ""
        head = f"  {item.heading}" if item.heading else ""
        first = item.snippet.splitlines()[0][:100] if item.snippet else ""
        # a stale DECISION says so on the hit itself: the moment you retrieve it is
        # the moment the warning can still change what you do with it
        stale = f"  {_TAINT} stale (a dep moved — `crib design read`)" if item.tainted else ""
        print(f"[{item.score:.3f}] {item.relpath}{loc}{head}{stale}\n    {first}")
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
        print(f"  ⚠︎ similar [{s['score']:.3f}]: {s['relpath']}"
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
        print(f"{'git':10} not enabled (crib memory setup --remote <url>)")
    for s in d.get("lsp_sessions") or []:
        state = "busy" if s.get("busy") else f"idle {s.get('idle_s', 0):.0f}s"
        alive = "" if s.get("alive") else "  DEAD"
        print(f"{'lsp':10} {s.get('server')}  {s.get('root')}  "
              f"pid {s.get('pid')}  {state}{alive}")
    if d.get("reconciling"):
        why = f" ({d['reconcile_reason']})" if d.get("reconcile_reason") else ""
        print(f"{'reconcile':10} in progress{why}: "
              f"{d.get('reconcile_remaining', '?')} project(s) to go")
    if d.get("index_rebuilding"):
        print(f"{'rebuilding':10} not re-embedded yet: "
              f"{', '.join(d['index_rebuilding'])}")
    for proj, sw in (d.get("sweeps") or {}).items():
        print(f"{'sweep':10} {proj}: {sw.get('done', 0)}/{sw.get('total', 0)} files")
    for proj, files in (d.get("indexing") or {}).items():
        print(f"{'indexing':10} {proj}: {', '.join(files)}")
    projs = d.get("projects") or []
    print(f"{'projects':10} {len(projs)}")
    if projs:
        w = max(len(p["project"]) for p in projs)
        for p in projs:
            # stale decisions ride along on the inventory line: an ambient count
            # nobody has to think to ask for (`crib design check` says which)
            stale = (f"  ⚠︎ {p['design_tainted']} stale decision(s)"
                     if p.get("design_tainted") else "")
            print(f"  {p['project']:{w}}  notes {p['notes']:4}  "
                  f"designs {p.get('designs', 0):3}  plans {p.get('plans', 0):3}  "
                  f"docs {p['doc_chunks']:4}  symbols {p['symbols']:5}  "
                  f"learnings {p['learnings']:3}{stale}")


def _emit_projects(rows: Any, as_json: bool) -> None:
    """`crib project list`: one project per line, annotated when its notes live in
    a repo — and loudly when that repo isn't on this machine, since such a project
    reads as empty everywhere else."""
    if as_json:
        print(json.dumps(rows, indent=2, default=str)); return
    for r in rows or []:
        if not isinstance(r, dict):
            print(r); continue
        line = r.get("project", "")
        if r.get("store_root"):
            line += f"   in-repo: {r['store_root']}"
        if r.get("unavailable"):
            line += "   ⚠︎ UNAVAILABLE (repo not on this machine)"
        print(line)


def _emit_project(d: Any, verb: str | None, as_json: bool) -> None:
    """Human summary for `crib project <verb>`."""
    if as_json:
        print(json.dumps(d, indent=2, default=str)); return
    if not isinstance(d, dict):
        print(d); return
    proj = d.get("project", "")
    if verb == "migrate":
        if not d.get("changed") and not d.get("skipped"):
            print(f"{proj}: layout already current — nothing to migrate"); return
        print(f"{proj}: moved {len(d.get('moved') or [])} facet note(s) to the "
              f"sibling pillar stores; requalified "
              f"{len(d.get('refs_rewritten') or [])} citation(s)")
        for s in d.get("skipped") or []:
            print(f"  ! collision left in place: {s['from']} → {s['to']} "
                  f"(resolve by hand, re-run migrate)")
        rec = d.get("reconciled") or {}
        print(f"  reconcile: {rec.get('changed', 0)} changed, "
              f"{rec.get('removed', 0)} removed")
        return
    if verb in ("adopt", "release"):
        if not d.get("changed"):
            print(d.get("message") or f"{proj}: nothing to do"); return
        where = ("→ " + d["store"]) if verb == "adopt" else "→ the global store"
        print(f"{proj}: moved {d.get('notes_moved', 0)} note(s) + "
              f"{d.get('versions_moved', 0)} version(s) {where}")
        if verb == "adopt":
            print(f"  recorded as {d.get('store_root', '')}  "
                  f"(commit the notes with this repo)")
        rec = d.get("reconciled") or {}
        print(f"  reconcile: {rec.get('changed', 0)} changed, "
              f"{rec.get('removed', 0)} removed  (0/0 = ids unchanged, as expected)")
        return
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
    """Render an attached symbol learning (※) under a code-lookup/xref hit."""
    flag = "  ⚠︎ stale — body changed since written" if learning.get("stale") else ""
    print(f"{indent}※ note ({learning.get('relpath', '')}){flag}")
    for line in (learning.get("body") or "").splitlines():
        print(f"{indent}  {line}" if line.strip() else "")


def _emit_code_learning(data: Any, verb: str, as_json: bool) -> None:
    """Confirmation/print for the symbol-learning verbs (append/edit/forget/read)."""
    if as_json:
        print(json.dumps(data, indent=2, default=str)); return
    sym, rel = data.get("symbol", ""), data.get("relpath", "")
    if verb == "learning-read":
        if not data.get("found"):
            print(f"(no learning for {sym})"); return
        print(f"# {sym}  [{rel}]\n{(data.get('body') or '').strip()}"); return
    if verb == "learning-forget":
        print(f"forgot {sym}  ({rel})"); return
    if verb == "learning-reaffirm":
        print(f"reaffirmed {sym} (cleared ⚠︎ stale)  → {rel}"); return
    if verb == "learning-add":
        print(f"{'created' if data.get('created') else 'appended'} learning: {sym}  → {rel}")
        return
    print(f"edited learning: {sym}  → {rel}")   # learning-edit


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
        print(f"\n{bad} need attention — `crib learning rehome <fqn>` for suggestions, "
              f"or `crib learning forget <fqn>`")


def _emit_code_rehome(data: Any, as_json: bool) -> None:
    """Ranked rehome candidates (no target) or a move confirmation."""
    if as_json:
        print(json.dumps(data, indent=2, default=str)); return
    if "candidates" in data:
        print(f"rehome {data.get('old', '')} → candidates:")
        cands = data.get("candidates") or []
        if not cands:
            print("  (none — `crib learning forget` if it's truly gone)"); return
        for c in cands:
            print(f"  [{c.get('score', '')}] {c.get('fqname', '')}   {c.get('file', '')}")
        print(f"\nconfirm: crib learning rehome {data.get('old', '')} <fqname>")
        return
    print(f"rehomed {data.get('old', '')} → {data.get('new', '')}  ({data.get('relpath', '')})")


_TAINT = "⚠︎"
# The import tier reads as a QUESTION, not a warning: a proposed entry isn't
# stale, it is un-blessed — a different thing to do about it (`design promote`).
_PROPOSED = "?"


def _facet_flags(row: Any) -> str:
    """The glyphs a design row carries — stale, and/or awaiting promotion."""
    return ((f" {_TAINT}" if row.get("tainted") else "")
            + (f" {_PROPOSED}" if row.get("status") == "proposed" else ""))


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
        print(json.dumps(data, indent=2, default=str)); return
    roots = (data or {}).get("roots") or []
    if not roots:
        print("(no design decisions — `crib design add <title> <body>`)"); return
    ascii_mode = getattr(args, "ascii", False)
    branch, last, vert, blank = _tree_glyphs(ascii_mode)
    arrow = ">" if ascii_mode else ("▸" if data.get("direction") == "deps" else "◂")
    taint = "!" if ascii_mode else _TAINT

    def label(n: dict) -> str:
        if n.get("missing"):
            return f"{n.get('title', '')}  (dangling id)"
        flags = ((f" {taint}" if n.get("tainted") else "")
                 + (f" {_PROPOSED}" if n.get("status") == "proposed" else "")
                 + (" ↑" if n.get("repeat") else ""))
        state = ("  [superseded]" if n.get("status") == "superseded"
                 else "  [proposed]" if n.get("status") == "proposed" else "")
        return f"{n.get('title', '')}{state}{flags}   {n.get('relpath', '')}"

    def render(node: dict, prefix: str) -> None:
        kids = node.get("children") or []
        for i, c in enumerate(kids):
            islast = i == len(kids) - 1
            print(f"{prefix}{last if islast else branch}{arrow} {label(c)}")
            render(c, prefix + (blank if islast else vert))

    for root in roots:
        print(f"{label(root)}   [{data.get('direction', '')}]")
        render(root, "")


def _emit_design_check(data: Any, args: Any) -> None:
    """Tainted decisions: the chain that explains each, WHAT changed and when, and
    the verb to run next — a flag with no prescribed follow-up is a dead end."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str)); return
    rows = (data or {}).get("tainted") or []
    proposed = (data or {}).get("proposed") or []
    for cyc in (data or {}).get("cycles") or []:
        print(f"✗ dependency CYCLE: {' → '.join(cyc)}")
    for p in proposed:
        print(f"{_PROPOSED} {p.get('title', '')}   {p.get('relpath', '')}")
        print(f"    → {p.get('next', '')}")
    if not rows:
        print(f"✓ {data.get('designs', 0)} decision(s), none stale"); return
    for r in rows:
        print(f"{_TAINT} {r.get('title', '')}   {r.get('relpath', '')}")
        for c in r.get("causes") or []:
            # the date is already inside `reason` when there is one — the bracket
            # carries only the kind, so the line doesn't say it twice
            print(f"    • [{c.get('change_kind', '?')}] {c.get('reason', '')}")
        if not (r.get("causes") or []):
            for reason in r.get("reasons") or []:
                print(f"    • {reason}")
        for p in r.get("paths") or []:
            chain = " → ".join(p.get("chain") or [])
            if len(p.get("chain") or []) > 1:
                print(f"      via {chain}")
        print(f"    → {r.get('next', '')}")
    print(f"\n{len(rows)} of {data.get('designs', 0)} decision(s) need re-reading — "
          f"`crib design read <ref>`, then `crib design reaffirm <ref>` "
          f"(taint means a dep moved, not that the decision is wrong)")


def _emit_facet_hits(hits: Any, args: Any) -> None:
    """`design lookup` / `plan lookup`: a locator line per hit carrying the facet
    state that decides whether to trust it — status, taint, edge counts."""
    if getattr(args, "json", False):
        print(json.dumps(hits, indent=2, default=str)); return
    if not hits:
        print("(no matches)"); return
    for h in hits:
        loc = (f":{h.get('line_start')}-{h.get('line_end')}"
               if h.get("line_start") else "")
        st = f"  [{h['status']}]" if h.get("status") else ""
        edges = (f"  {h.get('deps', 0)}▸/{h.get('dependents', 0)}◂"
                 if "deps" in h else "")
        stale = f"  {_TAINT} stale" if h.get("tainted") else ""
        print(f"[{h.get('score', 0.0):.3f}] {h.get('title', '')}{st}{edges}{stale}"
              f"   {h.get('relpath', '')}{loc}")
        first = (h.get("snippet") or "").splitlines()
        if first:
            print(f"    {first[0][:100]}")
    if any(h.get("tainted") for h in hits):
        print(f"\n{_TAINT} a stale decision is one whose ground moved and nobody "
              f"re-read — `crib design read <ref>` before relying on it")


def _emit_design_read(data: Any, args: Any) -> None:
    """A decision's dossier: header, taint, body, then the annotated edges — the
    `code dossier` layout, so the two read the same way."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str)); return
    if not isinstance(data, dict):
        print(data); return
    state = ("  [superseded]" if data.get("status") == "superseded"
             else f"  [proposed {_PROPOSED}]" if data.get("status") == "proposed"
             else "")
    flag = f"  {_TAINT} STALE" if data.get("tainted") else ""
    print(f"{data.get('title', '')}{state}{flag}   {data.get('relpath', '')}"
          f"   (updated {data.get('updated', '?')})")
    for c in data.get("causes") or []:
        print(f"  • [{c.get('change_kind', '?')}] {c.get('reason', '')}")
    for p in data.get("paths") or []:
        if len(p.get("chain") or []) > 1:
            print(f"    via {' → '.join(p['chain'])}")
    if data.get("next"):
        print(f"  → {data['next']}")
    print()
    _render_markdown((data.get("body") or "").strip() + "\n")
    for label, arrow in (("deps", "▸ builds on"), ("dependents", "◂ built on by")):
        rows = data.get(label) or []
        if not rows:
            continue
        print(f"  {arrow}")
        for r in rows:
            st = f"  [{r['status']}]" if r.get("status") else ""
            print(f"     {r.get('title', '')}{st}{_facet_flags(r)}"
                  f"   {r.get('relpath', '')}")
    # attribution: where this came FROM, and whether that passage still reads
    # the way it did (`changed`/`missing` are what taints it)
    sources = data.get("sources") or []
    if sources and isinstance(sources[0], dict):
        print("  § drawn from")
        for s in sources:
            mark = {"changed": _TAINT, "missing": "✗"}.get(s.get("state", ""), " ")
            print(f"    {mark} {s.get('label', '')}  [{s.get('state', '')}]")


def _emit_design_list(data: Any, args: Any) -> None:
    """Flat decision table: taint, title, edge counts, relpath."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str)); return
    for cyc in (data or {}).get("cycles") or []:
        print(f"✗ dependency CYCLE: {' → '.join(cyc)}")
    rows = (data or {}).get("designs") or []
    if not rows:
        print("(no decisions"
              + (" are stale — ✓)" if data.get("filtered")
                 else " — `crib design add <title>`)"))
        return
    for r in rows:
        mark = (_TAINT if r.get("tainted")
                else _PROPOSED if r.get("status") == "proposed" else " ")
        sup = "  [superseded]" if r.get("status") == "superseded" else ""
        cited = f"   §{r['sources']}" if r.get("sources") else ""
        print(f"{mark} {r.get('title', '')}{sup}"
              f"   {r.get('deps', 0)}▸/{r.get('dependents', 0)}◂{cited}"
              f"   {r.get('relpath', '')}")
    stale, proposed = data.get("tainted", 0), data.get("proposed", 0)
    tail = ""
    if not data.get("filtered"):
        tail = ((f", {stale} stale — `crib design check`" if stale else "")
                + (f", {proposed} proposed {_PROPOSED} — `crib design promote <ref>`"
                   if proposed else ""))
    print(f"\n{len(rows)} of {data.get('total', 0)} decision(s){tail}")


_PLAN_GLYPH = {"todo": "·", "in-progress": "▶", "done": "✓", "verified": "✓✓"}


_GROUP_HEADS = {"in-progress": "in progress", "ready": "ready",
                "blocked": "blocked", "done": "done"}


def _emit_plan_list(data: Any, args: Any) -> None:
    """The plan as a WORKING SET: in-progress, then ready, then blocked (each
    naming what it waits on), then done — not a graph dump. `⊘` marks a
    derived-blocked item."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str)); return
    for cyc in (data or {}).get("cycles") or []:
        print(f"✗ dependency CYCLE: {' → '.join(cyc)}")
    items = (data or {}).get("items") or []
    if not items:
        print("(nothing to do — `crib plan add <title>`"
              + (", `--all` shows finished items)" if data.get("hidden") else ")"))
        return
    group = None
    for it in items:
        if it.get("group") and it["group"] != group:
            group = it["group"]
            print(f"\n{_GROUP_HEADS.get(group, group)}:")
        mark = "⊘" if it.get("blocked") else _PLAN_GLYPH.get(it.get("status", ""), "·")
        print(f"{mark:2} {it.get('status', ''):12} {it.get('title', '')}"
              f"   {it.get('relpath', '')}")
        for b in it.get("blocked_by") or []:
            ref = b.get("ref") or b.get("title", "") if isinstance(b, dict) else b
            st = f" ({b['status']})" if isinstance(b, dict) and b.get("status") else ""
            kind = f"{b['kind']} " if isinstance(b, dict) and b.get("kind") else ""
            print(f"     ← waiting on {kind}{ref}{st}")
        if it.get("missing_deps"):
            print(f"     ✗ missing dep(s): {', '.join(it['missing_deps'])}")
        # a finished item whose source moved: reported, never re-opened
        for why in it.get("revisit") or []:
            print(f"     {_TAINT} revisit: {why}")
    for it in items:                      # the per-item loop the ready ones carry
        if it.get("next"):
            print(f"\n→ {it['next']}")
            break
    if data.get("hidden"):
        print(f"\n({data['hidden']} finished item(s) hidden — --all to show)")


def _emit_design_import(data: Any, args: Any) -> None:
    """A doc prepared for extraction: the PROCEDURE first (it is the payload —
    the verb wrote nothing and ran no model), then the citable sections and
    whatever already draws on this doc."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str)); return
    if not isinstance(data, dict):
        print(data); return
    print(f"{data.get('relpath', '')}   ({data.get('path', '')})\n")
    print(data.get("instruction", "").rstrip())
    existing = data.get("existing") or []
    if existing:
        print(f"\nalready drawing on this doc ({len(existing)}) — extend these "
              f"rather than forking:")
        for e in existing:
            print(f"  · {e.get('title', '')}   {e.get('relpath', '')}")
            for c in e.get("cites") or []:
                print(f"      § {c}")
    sections = data.get("sections") or []
    print(f"\nsections ({len(sections)}) — cite the `source` string verbatim:")
    for s in sections:
        print(f"  {s.get('words', 0):5}w  {s.get('source', '')}")
        if s.get("preview"):
            print(f"         {s['preview'][:96]}")


def _emit_design_write(data: Any, args: Any) -> None:
    """Confirmation for the design/plan write verbs — and, crucially, the CAUSAL
    CONSEQUENCES the verb reports: what the write just tainted, or unblocked."""
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str)); return
    if not isinstance(data, dict):
        print(data); return
    if data.get("removed") is not None:          # forget
        print(f"removed  {data.get('project', '')}/{data.get('relpath', '')}")
        for d in data.get("dependents") or []:
            print(f"  {_TAINT} now tainted: {d.get('title', '')}  ({d.get('relpath', '')})")
        return
    if data.get("added", 0) > 1:                 # a plan batch
        print(f"→ {data.get('project', '')}: added {data['added']} item(s)")
        for it in data.get("items") or []:
            print(f"   · {it.get('title', '')}   {it.get('relpath', '')}")
        return
    print(f"→ {data.get('project', '')}/{data.get('relpath', '')}  "
          f"{data.get('title', '')}")
    for key in ("status", "rank", "dep_title", "superseded_by"):
        if data.get(key):
            print(f"  {key}: {data[key]}")
    if data.get("deps"):
        print(f"  deps: {len(data['deps'])}")
    for w in data.get("warnings") or []:
        print(f"  {_TAINT} {w}")
    for d in data.get("tainted_dependents") or []:
        print(f"  {_TAINT} now tainted: {d.get('title', '')}  ({d.get('relpath', '')})")
    for d in data.get("newly_tainted") or []:
        print(f"  {_TAINT} now tainted: {d.get('title', '')}  ({d.get('relpath', '')})")
        for via in d.get("via") or []:
            print(f"      via {via}")
    for u in data.get("unblocked") or []:
        print(f"  ✓ unblocked: {u.get('title', '')}  ({u.get('ref', '')})")
    for s in data.get("similar") or []:
        print(f"  {_TAINT} similar decision [{s['score']:.3f}]: {s['relpath']}"
              + (f" — {s['heading']}" if s.get("heading") else "")
              + "  (append to it rather than forking the graph?)")
    if data.get("missing"):
        print(f"  ✗ missing dep(s): {', '.join(data['missing'])}")
    if data.get("next"):
        print(f"  → {data['next']}")


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
    branch, last, vert, blank = _tree_glyphs(ascii_mode)
    pin = " *" if ascii_mode else " ※"          # step 3: node carries a learning
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
                   ("index", "(re)index the repo's code + in-situ docs from its .crib"),
                   ("status", "is it indexed? counts, kinds, .crib paths")):
        _sp = pjsub.add_parser(_v, help=_h)
        proj(_sp)
    for _v, _h in (("adopt", "move this project's notes INTO the repo "
                             "(needs `store:` in its .crib)"),
                   ("release", "move an adopted project's notes back to the "
                               "global store"),
                   ("migrate", "move legacy notes/{design,plans,code-learnings} "
                               "into the sibling pillar stores (idempotent; every "
                               "full reindex also self-heals)")):
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
                            "(alias for `search --render`)")
    s.add_argument("query"); proj(s)
    # k matches `lookup`/the MCP tool/the Crib default (8) — `note lookup --render`
    # routes HERE, so a different default made the same rendered view return 5 or 8
    # hits depending on how you spelled it.
    s.add_argument("-k", type=int, default=8)
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
                       help="clear a learning's ⚠︎ stale flag without rewriting it")
    s.add_argument("symbol"); proj(s)

    s = learnsub.add_parser("report",
                       help="health report for attached learnings (ok/moved/orphan)")
    proj(s)
    s.add_argument("--orphans", action="store_true",
                   help="only the actionable ones (moved/orphan)")

    s = learnsub.add_parser("rehome",
                       help="re-point an orphaned learning (no target = ranked suggestions)")
    s.add_argument("old"); s.add_argument("new", nargs="?"); proj(s)

    # `crib design <verb>` / `crib plan <verb>` — decisions and work items, both
    # notes with a dependency graph (crib/designs.py). The FACET is the interface:
    # read/edit/lookup have their own verbs here, because only they speak edges.
    # Bare `crib design` / `crib plan` mean `list`, as bare `crib project` means
    # `status` — the orienting read is what you want when you type just the noun.
    n_design = sub.add_parser("design",
                              help="design decisions + their dependency graph")
    designsub = n_design.add_subparsers(dest="design_verb")
    n_plan = sub.add_parser("plan", help="persistent, resumable plan items")
    plansub = n_plan.add_subparsers(dest="plan_verb")

    # the three ways to give a body: positional, `-` for stdin, `--file`; omitted
    # at a terminal opens $EDITOR (shell-quoting prose is the worst friction here)
    def body_args(sp, what: str) -> None:
        sp.add_argument("content", nargs="?",
                        help=f"{what} ('-' reads stdin; omitted opens $EDITOR)")
        sp.add_argument("--file", dest="file", help=f"read the {what} from a file")

    # `--source doc#heading` — where a decision/item came FROM. Attribution edges
    # check (a changed section taints) but never gate, so they hang off the same
    # authoring verbs rather than getting a facet of their own. A source names a
    # SECTION: a bare doc is refused (unless the doc has no headings at all).
    def source_arg(sp) -> None:
        sp.add_argument("--source", action="append", dest="sources",
                        metavar="DOC#HEADING",
                        help="doc SECTION this was drawn from (repeatable)")

    s = designsub.add_parser("add",
                       help="record a design decision (body: arg, '-', --file or $EDITOR)")
    s.add_argument("title"); body_args(s, "the decision, why, what was rejected")
    proj(s)
    s.add_argument("--dep", action="append", dest="deps",
                   help="a decision this one builds on (repeatable)")
    source_arg(s)
    s.add_argument("--proposed", action="store_true",
                   help="land it in the import tier (taints nothing until promoted)")

    s = designsub.add_parser("read",
                       help="a decision's dossier: body + annotated edges + taint")
    s.add_argument("ref"); proj(s)

    s = designsub.add_parser("edit",
                       help="rewrite a decision; lists what the change tainted")
    s.add_argument("ref"); body_args(s, "the new body"); proj(s); source_arg(s)

    s = designsub.add_parser("append",
                       help="extend a decision; lists what the change tainted")
    s.add_argument("ref"); body_args(s, "the text to append"); proj(s)

    s = designsub.add_parser("lookup", aliases=["search"],
                       help="semantic search over DECISIONS (hits flag stale ones)")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=8)

    s = designsub.add_parser("list",
                       help="every decision as a table (--tainted filters to stale)")
    proj(s)
    s.add_argument("--tainted", action="store_true",
                   help="only decisions that need re-reading")

    for _v, _h in (("dep-add", "declare that a decision builds on another"),
                   ("dep-remove", "drop a dependency edge between decisions")):
        _s = designsub.add_parser(_v, help=_h)
        _s.add_argument("ref"); _s.add_argument("dep_ref", metavar="dep"); proj(_s)

    s = designsub.add_parser("forget",
                       help="delete a decision (refuses while dependents exist)")
    s.add_argument("ref"); proj(s)
    s.add_argument("--force", action="store_true",
                   help="delete anyway, leaving dependents tainted")

    s = designsub.add_parser("check",
                       help="which decisions are stale w.r.t. what they build on")
    s.add_argument("ref", nargs="?"); proj(s)

    s = designsub.add_parser("reaffirm",
                       help="re-record a decision's dep hashes (you re-read it)")
    s.add_argument("ref"); proj(s)

    s = designsub.add_parser("tree",
                       help="dependency tree around a decision (taint-flagged)")
    s.add_argument("ref", nargs="?"); proj(s)
    s.add_argument("--dependents", action="store_true",
                   help="what builds ON this (default: what this builds on)")
    s.add_argument("--depth", type=int, default=6)
    s.add_argument("--ascii", action="store_true", help="ASCII glyphs, no box-drawing")

    s = designsub.add_parser("supersede",
                       help="mark a decision superseded + taint its dependents")
    s.add_argument("ref"); s.add_argument("by", nargs="?"); proj(s)

    s = designsub.add_parser("promote",
                       help="proposed → active (an extracted decision, confirmed)")
    s.add_argument("ref"); proj(s)

    for _sub, _what in ((designsub, "decisions"), (plansub, "actionable work")):
        _s = _sub.add_parser("import",
                       help=f"prepare a doc for extraction into {_what} (writes nothing)")
        _s.add_argument("doc", help="note relpath, or a repo path indexed in situ")
        proj(_s)

    s = plansub.add_parser("add",
                       help="add plan item(s) — body optional, `--item` adds more")
    s.add_argument("title"); body_args(s, "the item's detail (optional)"); proj(s)
    s.add_argument("--dep", action="append", dest="deps",
                   help="an item this one must follow (repeatable)")
    source_arg(s)
    s.add_argument("--item", action="append", dest="extra_items", metavar="TITLE",
                   help="another title-only item, added after this one (repeatable)")
    s.add_argument("--after"); s.add_argument("--before")

    s = plansub.add_parser("lookup", aliases=["search"],
                       help="semantic search over PLAN ITEMS")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=8)

    s = plansub.add_parser("status",
                       help="set an item's status (todo/in-progress/done/verified)")
    s.add_argument("ref"); s.add_argument("status"); proj(s)

    for _v, _h in (("dep-add", "declare that an item must follow another"),
                   ("dep-remove", "drop a must-precede edge between items")):
        _s = plansub.add_parser(_v, help=_h)
        _s.add_argument("ref"); _s.add_argument("dep_ref", metavar="dep"); proj(_s)

    s = plansub.add_parser("forget",
                       help="delete a plan item (refuses while dependents exist)")
    s.add_argument("ref"); proj(s)
    s.add_argument("--force", action="store_true",
                   help="delete anyway, leaving dependents tainted")

    s = plansub.add_parser("move", help="re-order an item (rank only; deps untouched)")
    s.add_argument("ref"); proj(s)
    s.add_argument("--after"); s.add_argument("--before")

    s = plansub.add_parser("list", help="the plan in execution order (topo + rank)")
    proj(s)
    s.add_argument("--all", action="store_true", help="include done/verified items")

    s = plansub.add_parser("next", help="actionable items now (todo, deps satisfied)")
    proj(s)
    s.add_argument("-k", type=int, default=5)

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
#
# Each row also DECLARES its MCP twin's wire signature (`mcp`) and the server-side
# project-resolution policy (`policy`) it must carry, making this table the single
# source of truth for the whole surface: tests/test_surface_parity.py walks it
# against FastMCP's introspected schemas, so a param, a default (the CLI/MCP
# `apropos k` split was 5 vs 8) or a policy can't drift on one face only.
@dataclass(frozen=True)
class Verb:
    tool: str                                   # MCP tool name (daemon path)
    build: Callable[[Any], dict[str, Any]]      # parsed args → logical call params
    emit: Callable[[Any, Any], None]            # (result, parsed args) → stdout
    method: str = ""                            # Crib method (in-process); "" ⇒ tool
    is_async: bool = False                      # in-process wraps in asyncio.run
    wants_cwd: bool = True                       # append project_path / cwd
    mcp: str | None = None                      # MCP params: "query k=8 …" ("" = none)
    policy: str = ""                            # server.TOOL_POLICY declaration

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
def _E(d, a): _emit(d, a.json)                                   # generic
def _E_raw(d, a): print(d)                                      # verbatim (locate/snapshot)
def _E_note(d, a): _print_note(d, a.json)
def _E_apropos(d, a): _emit_apropos(d, a.json)
def _E_status(d, a): _emit_status(d, a.json)
def _E_dossier(d, a): _emit_code_dossier(d, a.json)
def _E_report(d, a): _emit_code_report(d, a.json)
def _E_rehome(d, a): _emit_code_rehome(d, a.json)
def _E_graph(d, a): _emit_code_graph(d, a)
def _E_dtree(d, a): _emit_design_tree(d, a)
def _E_dcheck(d, a): _emit_design_check(d, a)
def _E_dread(d, a): _emit_design_read(d, a)
def _E_dlist(d, a): _emit_design_list(d, a)
def _E_facet(d, a): _emit_facet_hits(d, a)
def _E_plans(d, a): _emit_plan_list(d, a)
def _E_dwrite(d, a): _emit_design_write(d, a)
def _E_dimport(d, a): _emit_design_import(d, a)
def _E_projects(d, a): _emit_projects(d, a.json)
def _E_code(verb): return lambda d, a: _emit_code(d, verb, a.json)
def _E_learning(verb): return lambda d, a: _emit_code_learning(d, verb, a.json)
def _E_project(verb): return lambda d, a: _emit_project(d, verb, a.json)


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
    return call


def _plan_items(a: Any) -> list[dict[str, Any]] | None:
    """`crib plan add <title> [body] --item T --item T` → the batch form.

    Only built when `--item` is actually used; otherwise the call stays the plain
    single-item shape (so nothing about the common case changes). The extra items
    are title-only, which is the normal shape for a plan item anyway."""
    extra = getattr(a, "extra_items", None)
    if not extra:
        return None
    first = {"title": a.title, "deps": a.deps,
             "content": _body(a.content, a.file, default=""),
             "sources": getattr(a, "sources", None)}
    return [first, *({"title": t} for t in extra)]


def _proj_of(a: Any) -> str | None:
    """`-p/--project` when the sub-parser defines it — bare `crib project` selects no
    sub-verb, so its Namespace carries neither selector."""
    return getattr(a, "project", None)


_PROJ = "project=None project_path=None"          # the two selectors, on most tools

VERBS: dict[str, Verb] = {
    # notes: search / read
    "note lookup": Verb("lookup", _b_lookup, _E, policy="read",
                        mcp=f"query {_PROJ} k=8 tags=None keyword_labels=None "
                            "keyword_weight=None summary_labels=None summary_weight=None"),
    "note apropos": Verb("apropos", lambda a: {"query": a.query, "project": a.project,
                                          "k": a.k, "tags": a.tags}, _E_apropos,
                    policy="read", mcp=f"query {_PROJ} k=8 tags=None"),
    "note read": Verb("read", lambda a: {"relpath": a.relpath, "project": a.project},
                 _E_note, method="read_note", policy="read", mcp=f"relpath {_PROJ}"),
    "note locate": Verb("locate", lambda a: {"relpath": a.relpath, "project": a.project},
                   _E_raw, policy="read", mcp=f"relpath {_PROJ}"),
    # notes: write
    "note store": Verb("store", lambda a: {"content": _read_content(a.content),
                                      "title": a.title, "project": a.project,
                                      "tags": a.tags}, _E, method="store_note",
                  is_async=True, policy="write",
                  mcp=f"content {_PROJ} title=None tags=None"),
    "note append": Verb("append", lambda a: {"relpath": a.relpath,
                                        "content": _read_content(a.content),
                                        "heading": a.heading, "project": a.project},
                   _E, method="append_note", is_async=True, policy="write",
                   mcp=f"relpath content {_PROJ} heading=None"),
    "note edit": Verb("edit", lambda a: {"relpath": a.relpath,
                                    "new_content": _read_content(a.content),
                                    "project": a.project}, _E,
                 method="edit_note", is_async=True, policy="write",
                 mcp=f"relpath new_content {_PROJ}"),
    "note forget": Verb("forget", lambda a: {"relpath": a.relpath, "project": a.project},
                   _E, is_async=True, policy="write", mcp=f"relpath {_PROJ}"),
    "note move": Verb("move", lambda a: {"relpath": a.relpath, "to_project": a.to_project,
                                    "to_relpath": a.to_relpath, "project": a.project},
                 _E, method="move_note", is_async=True, policy="write",
                 mcp=f"relpath {_PROJ} to_project=None to_relpath=None"),
    "note reindex": Verb("reindex", lambda a: {"relpath": a.relpath, "project": a.project},
                    _E, is_async=True, policy="read", mcp=f"relpath=None {_PROJ}"),
    "project reconcile": Verb("reconcile", lambda a: {}, _E, method="reconcile_all",
                      is_async=True, wants_cwd=False, policy="none", mcp=""),
    "note versions": Verb("versions", lambda a: {"relpath": a.relpath, "project": a.project},
                     _E, method="list_versions", policy="read", mcp=f"relpath {_PROJ}"),
    "note restore": Verb("restore", lambda a: {"relpath": a.relpath, "version": a.version,
                                          "project": a.project}, _E, is_async=True,
                    policy="read", mcp=f"relpath version {_PROJ}"),
    "note import": Verb("import", lambda a: {"paths": a.paths, "project": a.project},
                   _E, method="import_files", is_async=True, policy="source",
                   mcp=f"paths {_PROJ}"),
    "note import-memory": Verb("import_memory", lambda a: {"project": a.project}, _E,
                          method="import_claude_memory", is_async=True,
                          policy="source", mcp=_PROJ),
    "note distill": Verb("distill", lambda a: {"relpath": a.relpath, "project": a.project},
                    _E, is_async=True, policy="read", mcp=f"relpath {_PROJ}"),
    "note elaborate": Verb("elaborate", lambda a: {"label": a.label, "relpath": a.relpath,
                                              "project": a.project,
                                              "overwrite": a.overwrite}, _E,
                      is_async=True, policy="read",
                      mcp=f"label relpath=None {_PROJ} overwrite=False"),
    "note summarize": Verb("summarize", lambda a: {"label": a.label, "relpath": a.relpath,
                                              "project": a.project,
                                              "overwrite": a.overwrite}, _E,
                      is_async=True, policy="read",
                      mcp=f"label relpath=None {_PROJ} overwrite=False"),
    "memory snapshot": Verb("snapshot", lambda a: {"message": a.message}, _E_raw,
                     wants_cwd=False, policy="none", mcp="message=None"),
    "memory history": Verb("history", lambda a: {"relpath": a.relpath}, _E,
                     wants_cwd=False, policy="none", mcp="relpath=None"),
    "project list": Verb("projects", lambda a: {}, _E_projects,
                         method="project_list", wants_cwd=False,
                         policy="none", mcp=""),
    "project use": Verb("use_project", lambda a: {"project": a.project}, _E,
                        method="use_project", wants_cwd=False, policy="session",
                        mcp="project"),
    "project current": Verb("current_project", lambda a: {}, _E,
                            method="current_project", policy="session",
                            mcp="project_path=None"),
    "status": Verb("status", lambda a: {}, _E_status, wants_cwd=False,
                   policy="none", mcp=""),
    # project lifecycle (whole repo) — repo-scoped, hence the `source` policy
    "project setup": Verb("project_setup", lambda a: {"project": _proj_of(a)},
                          _E_project("setup"), is_async=True, policy="source",
                          mcp=_PROJ),
    "project index": Verb("project_index", lambda a: {"project": _proj_of(a)},
                          _E_project("index"), is_async=True, policy="source",
                          mcp=f"{_PROJ} budget_s=None"),
    "project status": Verb("project_status", lambda a: {"project": _proj_of(a)},
                           _E_project("status"), policy="source", mcp=_PROJ),
    "project forget": Verb("project_forget",
                           lambda a: {"project": _proj_of(a),
                                      "with_learnings": getattr(a, "with_learnings",
                                                                False)},
                           _E_project("forget"), policy="source",
                           mcp=f"{_PROJ} with_learnings=False"),
    # in-repo storage: move a project's notes into / out of the repo that owns them
    "project adopt": Verb("project_adopt", lambda a: {"project": _proj_of(a)},
                          _E_project("adopt"), is_async=True, policy="source",
                          mcp=_PROJ),
    "project release": Verb("project_release", lambda a: {"project": _proj_of(a)},
                            _E_project("release"), is_async=True, policy="source",
                            mcp=_PROJ),
    "project migrate": Verb("project_migrate", lambda a: {"project": _proj_of(a)},
                            _E_project("migrate"), is_async=True, policy="source",
                            mcp=_PROJ),
    # code index
    "code lookup": Verb("code_lookup", lambda a: {"query": a.query,
                                                 "project": a.project, "k": a.k},
                        _E_code("code-lookup"), policy="read",
                        mcp=f"query {_PROJ} k=8"),
    "code xref": Verb("code_xref", lambda a: {"symbol": a.symbol, "project": a.project},
                      _E_code("code-xref"), policy="read", mcp=f"symbol {_PROJ}"),
    "code dossier": Verb("code_dossier", lambda a: {"symbol": a.symbol,
                                                   "project": a.project}, _E_dossier,
                         policy="read", mcp=f"symbol {_PROJ}"),
    "code graph": Verb("code_graph", lambda a: {"symbol": a.symbol,
                                               "direction": _graph_direction(a),
                                               "depth": a.depth, "project": a.project},
                       _E_graph, policy="read",
                       mcp=f"symbol {_PROJ} direction='callees' depth=6"),
    "code index": Verb("code_index",
                       lambda a: {"path": str(Path(a.path).expanduser().resolve()),
                                  "project": a.project},
                       _E_code("code-index"), is_async=True, policy="source",
                       mcp=f"path {_PROJ}"),
    # code learnings — `read` policy BY INTENT (a learning is about a symbol in the
    # project you're in); see the exception block above `_write_project` in server.py
    "learning add": Verb("learning_add", lambda a: {"symbol": a.symbol,
                                                 "text": _read_content(a.text),
                                                 "project": a.project},
                        _E_learning("learning-add"), is_async=True, policy="read",
                        mcp=f"symbol text {_PROJ}"),
    "learning edit": Verb("learning_edit", lambda a: {"symbol": a.symbol,
                                             "new_content": _read_content(a.text),
                                             "project": a.project},
                      _E_learning("learning-edit"), is_async=True, policy="read",
                      mcp=f"symbol new_content {_PROJ}"),
    "learning forget": Verb("learning_forget", lambda a: {"symbol": a.symbol,
                                                 "project": a.project},
                        _E_learning("learning-forget"), is_async=True, policy="read",
                        mcp=f"symbol {_PROJ}"),
    "learning read": Verb("learning_read", lambda a: {"symbol": a.symbol, "project": a.project},
                      _E_learning("learning-read"), policy="read", mcp=f"symbol {_PROJ}"),
    "learning reaffirm": Verb("learning_reaffirm", lambda a: {"symbol": a.symbol,
                                                     "project": a.project},
                          _E_learning("learning-reaffirm"), is_async=True, policy="read",
                          mcp=f"symbol {_PROJ}"),
    "learning report": Verb("learning_report", lambda a: {"project": a.project,
                                                       "orphans_only": a.orphans},
                           _E_report, policy="read",
                           mcp=f"{_PROJ} orphans_only=False"),
    "learning rehome": Verb("learning_rehome", lambda a: {"old_fqn": a.old, "new_fqn": a.new,
                                                 "project": a.project}, _E_rehome,
                        is_async=True, policy="read",
                        mcp=f"old_fqn {_PROJ} new_fqn=None"),
    # design decisions + plan items (crib/designs.py). The two `add` verbs CREATE a
    # durable fact → `write` policy (name the project); every other verb is keyed by
    # a ref that only resolves inside one project → `read`, the learnings exception.
    # The FACET carries its own read/write/search verbs (`design read/edit/append/
    # lookup/list`) — the note verbs can't speak edges, so they aren't the way in.
    "design add": Verb("design_add", lambda a: {"title": a.title,
                                                "content": _body(
                                                    a.content, a.file,
                                                    what="the decision + rationale"),
                                                "deps": a.deps, "project": a.project,
                                                "sources": a.sources,
                                                "proposed": a.proposed},
                       _E_dwrite, is_async=True, policy="write",
                       mcp=f"title content deps=None {_PROJ} sources=None "
                           "proposed=False"),
    "design read": Verb("design_read", lambda a: {"ref": a.ref,
                                                  "project": a.project},
                        _E_dread, policy="read", mcp=f"ref {_PROJ}"),
    "design edit": Verb("design_edit",
                        lambda a: {"ref": a.ref,
                                   "new_content": _body(a.content, a.file,
                                                        what="the new body"),
                                   "project": a.project, "sources": a.sources},
                        _E_dwrite, is_async=True, policy="read",
                        mcp=f"ref new_content {_PROJ} sources=None"),
    "design append": Verb("design_append",
                          lambda a: {"ref": a.ref,
                                     "content": _body(a.content, a.file,
                                                      what="the text to append"),
                                     "project": a.project},
                          _E_dwrite, is_async=True, policy="read",
                          mcp=f"ref content {_PROJ}"),
    "design lookup": Verb("design_lookup", lambda a: {"query": a.query, "k": a.k,
                                                      "project": a.project},
                          _E_facet, policy="read", mcp=f"query {_PROJ} k=8"),
    "design list": Verb("design_list",
                        lambda a: {"tainted": getattr(a, "tainted", False),
                                   "project": _proj_of(a)},
                        _E_dlist, policy="read", mcp=f"{_PROJ} tainted=False"),
    "design dep-add": Verb("design_dep_add", lambda a: {"ref": a.ref,
                                                        "dep_ref": a.dep_ref,
                                                        "project": a.project},
                           _E_dwrite, is_async=True, policy="read",
                           mcp=f"ref dep_ref {_PROJ}"),
    "design dep-remove": Verb("design_dep_remove", lambda a: {"ref": a.ref,
                                                              "dep_ref": a.dep_ref,
                                                              "project": a.project},
                              _E_dwrite, is_async=True, policy="read",
                              mcp=f"ref dep_ref {_PROJ}"),
    "design forget": Verb("design_forget", lambda a: {"ref": a.ref, "force": a.force,
                                                      "project": a.project},
                          _E_dwrite, is_async=True, policy="read",
                          mcp=f"ref {_PROJ} force=False"),
    "design check": Verb("design_check", lambda a: {"ref": a.ref, "project": a.project},
                         _E_dcheck, policy="read", mcp=f"{_PROJ} ref=None"),
    "design reaffirm": Verb("design_reaffirm", lambda a: {"ref": a.ref,
                                                          "project": a.project},
                            _E_dwrite, is_async=True, policy="read",
                            mcp=f"ref {_PROJ}"),
    "design tree": Verb("design_tree",
                        lambda a: {"ref": a.ref, "project": a.project,
                                   "direction": ("dependents" if a.dependents
                                                 else "deps"), "depth": a.depth},
                        _E_dtree, policy="read",
                        mcp=f"{_PROJ} ref=None direction='deps' depth=6"),
    "design supersede": Verb("design_supersede", lambda a: {"ref": a.ref,
                                                            "by_ref": a.by,
                                                            "project": a.project},
                             _E_dwrite, is_async=True, policy="read",
                             mcp=f"ref {_PROJ} by_ref=None"),
    # source attribution + the import tier: `import` prepares a doc (and writes
    # nothing), `promote` is the human act that ends the quarantine. Both are
    # keyed inside one project, so both take the `read` policy like the rest.
    "design promote": Verb("design_promote", lambda a: {"ref": a.ref,
                                                        "project": a.project},
                           _E_dwrite, is_async=True, policy="read",
                           mcp=f"ref {_PROJ}"),
    "design import": Verb("design_import", lambda a: {"relpath": a.doc,
                                                      "project": a.project},
                          _E_dimport, policy="read", mcp=f"relpath {_PROJ}"),
    "plan import": Verb("plan_import", lambda a: {"relpath": a.doc,
                                                  "project": a.project},
                        _E_dimport, policy="read", mcp=f"relpath {_PROJ}"),
    "plan add": Verb("plan_add", lambda a: {"title": a.title,
                                            # a plan item's body is OPTIONAL —
                                            # title-only is a normal whole item, so
                                            # an omitted body must not open $EDITOR
                                            "content": _body(a.content, a.file,
                                                             default=""),
                                            "deps": a.deps, "after": a.after,
                                            "before": a.before,
                                            "items": _plan_items(a),
                                            "project": a.project,
                                            "sources": a.sources},
                     _E_dwrite, is_async=True, policy="write",
                     mcp=f"title=None content='' {_PROJ} deps=None after=None "
                         "before=None items=None sources=None"),
    "plan lookup": Verb("plan_lookup", lambda a: {"query": a.query, "k": a.k,
                                                  "project": a.project},
                        _E_facet, policy="read", mcp=f"query {_PROJ} k=8"),
    "plan status": Verb("plan_status", lambda a: {"ref": a.ref, "status": a.status,
                                                  "project": a.project},
                        _E_dwrite, is_async=True, policy="read",
                        mcp=f"ref status {_PROJ}"),
    "plan dep-add": Verb("plan_dep_add", lambda a: {"ref": a.ref, "dep_ref": a.dep_ref,
                                                    "project": a.project},
                         _E_dwrite, is_async=True, policy="read",
                         mcp=f"ref dep_ref {_PROJ}"),
    "plan dep-remove": Verb("plan_dep_remove", lambda a: {"ref": a.ref,
                                                          "dep_ref": a.dep_ref,
                                                          "project": a.project},
                            _E_dwrite, is_async=True, policy="read",
                            mcp=f"ref dep_ref {_PROJ}"),
    "plan forget": Verb("plan_forget", lambda a: {"ref": a.ref, "force": a.force,
                                                  "project": a.project},
                        _E_dwrite, is_async=True, policy="read",
                        mcp=f"ref {_PROJ} force=False"),
    "plan move": Verb("plan_move", lambda a: {"ref": a.ref, "after": a.after,
                                              "before": a.before,
                                              "project": a.project},
                      _E_dwrite, is_async=True, policy="read",
                      mcp=f"ref {_PROJ} after=None before=None"),
    "plan list": Verb("plan_list", lambda a: {"all": getattr(a, "all", False),
                                              "project": _proj_of(a)},
                      _E_plans, policy="read", mcp=f"{_PROJ} all=False"),
    "plan next": Verb("plan_next", lambda a: {"k": a.k, "project": a.project},
                      _E_plans, policy="read", mcp=f"{_PROJ} k=5"),
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
        return None                          # unresolvable project: not our error
    if not pp.in_repo:
        return None
    return (f"crib memory: notes for {proj} live in {pp.store_root}; commit them "
            f"with that repo's git. (The global memory store no longer carries "
            f"them — `crib project release {proj}` moves them back if you want "
            f"crib to sync them again.)")


def _run_git(args: Any, cfg: Any) -> int:
    """Share notes via git. Runs git client-side (the user's terminal owns auth);
    a pull that changes files then triggers a reindex through the daemon (or
    in-process). Not an MCP tool — pushing notes is outward-facing + interactive."""
    from .gitbacking import GitBacking
    from .paths import Paths

    verb = getattr(args, "memory_verb", None)        # `crib memory setup/sync/push/pull`
    # setup runs on a fresh machine where the data dir may not exist yet
    paths = Paths.resolve().ensure() if verb == "setup" else Paths.resolve()
    if (refusal := _in_repo_guard(paths, cfg, _cwd_of(args))) is not None:
        print(refusal, file=sys.stderr)
        return 1
    git = GitBacking(paths.data_dir)

    if verb == "setup":
        remote = getattr(args, "remote", None) or git.current_remote() or _prompt_remote()
        if not remote:
            print("crib memory setup: no remote given (pass --remote <url>)",
                  file=sys.stderr)
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
                return client.call("project_reconcile", {})
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
    # a noun with no verb (`crib note`) → point at its subcommands. `design`/`plan`
    # are NOT here: a bare one is a real command (`list`), per _BARE_NOUN_DEFAULT.
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
