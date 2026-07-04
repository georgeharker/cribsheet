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
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

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
    p = argparse.ArgumentParser(prog="crib", description="markdown memory")
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

    def proj(sp):  # shared --project option
        sp.add_argument("-p", "--project")

    sv = sub.add_parser("serve", help="run the MCP server (stdio or --http)")
    sv.add_argument("--http", action="store_true")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sub.add_parser("projects", help="list projects")
    sub.add_parser("info", help="show resolved paths and available backends")

    s = sub.add_parser("lookup", aliases=["search"], help="semantic search")
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

    s = sub.add_parser("apropos", aliases=["a"],
                       help="semantic search, rendering each full matched section "
                            "(alias for `search --render`, fewer hits)")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--tag", action="append", dest="tags")

    s = sub.add_parser("code-lookup",
                       help="find a code symbol by concept OR name (hybrid dense+kw)")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=8)

    s = sub.add_parser("code-xref",
                       help="a symbol's callers/callees/references from the symbol_index")
    s.add_argument("symbol"); proj(s)

    s = sub.add_parser("code-dossier",
                       help="everything about one symbol (+ neighbour descriptions)")
    s.add_argument("symbol"); proj(s)

    s = sub.add_parser("code-graph",
                       help="pstree-style call graph around a symbol (recursive)")
    s.add_argument("symbol"); proj(s)
    s.add_argument("--callers", action="store_true",
                   help="what CALLS the symbol (default: what it calls)")
    s.add_argument("--references", action="store_true",
                   help="everywhere the symbol is REFERENCED (broader than calls)")
    s.add_argument("--depth", type=int, default=6)
    s.add_argument("--ascii", action="store_true", help="ASCII glyphs, no box-drawing")

    s = sub.add_parser("code-index",
                       help="index a source file: symbols + call graph + descriptions")
    s.add_argument("path"); proj(s)

    s = sub.add_parser("code-append",
                       help="attach a durable learning to a code symbol ('-' reads stdin)")
    s.add_argument("symbol"); s.add_argument("text"); proj(s)

    s = sub.add_parser("code-edit",
                       help="rewrite a symbol's learning body ('-' reads stdin)")
    s.add_argument("symbol"); s.add_argument("text"); proj(s)

    s = sub.add_parser("code-forget",
                       help="remove a symbol's learning (recoverable; works on orphans)")
    s.add_argument("symbol"); proj(s)

    s = sub.add_parser("code-read", help="print a symbol's attached learning")
    s.add_argument("symbol"); proj(s)

    s = sub.add_parser("code-reaffirm",
                       help="clear a learning's ⚠ stale flag without rewriting it")
    s.add_argument("symbol"); proj(s)

    s = sub.add_parser("code-learnings",
                       help="health report for attached learnings (ok/moved/orphan)")
    proj(s)
    s.add_argument("--orphans", action="store_true",
                   help="only the actionable ones (moved/orphan)")

    s = sub.add_parser("code-rehome",
                       help="re-point an orphaned learning (no target = ranked suggestions)")
    s.add_argument("old"); s.add_argument("new", nargs="?"); proj(s)

    s = sub.add_parser("read", help="print a note's raw markdown")
    s.add_argument("relpath"); proj(s)

    s = sub.add_parser("locate", help="print a note's on-disk path")
    s.add_argument("relpath"); proj(s)

    s = sub.add_parser("store", help="create a new note ('-' reads stdin)")
    s.add_argument("content"); proj(s)
    s.add_argument("--title")
    s.add_argument("--tag", action="append", dest="tags")

    s = sub.add_parser("append", help="append to a note ('-' reads stdin)")
    s.add_argument("relpath"); s.add_argument("content"); proj(s)
    s.add_argument("--heading")

    s = sub.add_parser("edit", help="replace a note's content (stdin by default)")
    s.add_argument("relpath"); s.add_argument("content", nargs="?", default="-"); proj(s)

    s = sub.add_parser("forget", help="delete a note (recoverable via the ring)")
    s.add_argument("relpath"); proj(s)

    s = sub.add_parser("move", help="move/rename a note across projects (keeps id)")
    s.add_argument("relpath"); proj(s)
    s.add_argument("--to-project", dest="to_project")
    s.add_argument("--to-relpath", dest="to_relpath")

    s = sub.add_parser("reindex", help="reindex a note or whole project")
    s.add_argument("relpath", nargs="?"); proj(s)

    sub.add_parser("reconcile", help="sweep all projects for offline changes")

    s = sub.add_parser("versions", help="list recoverable versions of a note")
    s.add_argument("relpath"); proj(s)

    s = sub.add_parser("restore", help="restore a prior version of a note")
    s.add_argument("relpath"); s.add_argument("version"); proj(s)

    s = sub.add_parser("import", help="ingest local docs via the nearest .crib")
    proj(s)

    s = sub.add_parser("import-memory",
                       help="mirror Claude Code's harness memory into a crib project")
    proj(s)

    s = sub.add_parser("distill",
                       help="LLM-revise a note in place (compress/dedupe/normalize)")
    s.add_argument("relpath"); proj(s)

    s = sub.add_parser("elaborate",
                       help="keyword_index: generate BM25 search terms per section "
                            "(keywords/questions/phrase/…) for a note or project")
    s.add_argument("label")
    s.add_argument("relpath", nargs="?"); proj(s)
    s.add_argument("--overwrite", action="store_true",
                   help="regenerate even if it already exists")

    s = sub.add_parser("summarize",
                       help="summary_index: generate dense alias rephrasings per "
                            "section for a note or project")
    s.add_argument("label")
    s.add_argument("relpath", nargs="?"); proj(s)
    s.add_argument("--overwrite", action="store_true",
                   help="regenerate even if it already exists")

    s = sub.add_parser("snapshot", help="git checkpoint of the data tree")
    s.add_argument("-m", "--message")

    s = sub.add_parser("setup",
                       help="join the shared note repo on this machine "
                            "(set remote + frontmatter merge driver, then pull)")
    s.add_argument("--remote", help="git remote URL to join (prompted if omitted)")

    s = sub.add_parser("sync",
                       help="share notes via git: commit + pull + push, then reindex")
    s.add_argument("-m", "--message")
    s.add_argument("--remote", help="bootstrap: git init + set origin to this URL")
    sub.add_parser("push", help="push local note commits to the remote")
    sub.add_parser("pull", help="pull notes from the remote, then reindex")

    s = sub.add_parser("history", help="git history for a note or the tree")
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


_RAW_PRINT = {"locate", "snapshot"}  # printed verbatim (read handled separately)


def _resolve_serve_endpoint(args: Any) -> tuple[str, int]:
    """Bind address for `serve`/`--mcp`: explicit flags win, else `[daemon]`."""
    from .config import Config
    from .paths import Paths

    cfg = Config.load(Paths.resolve().config_file)
    return (args.host or cfg.daemon.host, args.port or cfg.daemon.port)


def _verb_call(args: Any) -> tuple[str, dict[str, Any]]:
    """Map a parsed CLI verb to an (mcp_tool, arguments) pair for the daemon.

    Content args read stdin here (client-side) — the daemon has none; `cwd` is
    sent so the daemon resolves `.crib`/project relative to the caller."""
    cwd = str(Path.cwd())
    v = args.cmd
    if v in ("lookup", "search", "apropos", "a"):
        # `apropos`/`a`, or `search --render`, take the section-rendering path
        # (the apropos tool returns full sections); plain `search` gets locators.
        tool = "apropos" if (v in ("apropos", "a") or getattr(args, "render", False)) \
            else "lookup"
        call = {"query": args.query, "project": args.project,
                "k": args.k, "tags": args.tags, "cwd": cwd}
        if tool == "lookup" and getattr(args, "keywords", None):
            call["keyword_labels"] = _split_labels(args.keywords)
        if tool == "lookup" and getattr(args, "keyword_weight", None) is not None:
            call["keyword_weight"] = args.keyword_weight
        if tool == "lookup" and getattr(args, "summaries", None):
            call["summary_labels"] = _split_labels(args.summaries)
        if tool == "lookup" and getattr(args, "summary_weight", None) is not None:
            call["summary_weight"] = args.summary_weight
        return tool, call
    if v == "read":
        return "read", {"relpath": args.relpath, "project": args.project, "cwd": cwd}
    if v == "locate":
        return "locate", {"relpath": args.relpath, "project": args.project, "cwd": cwd}
    if v == "store":
        return "store", {"content": _read_content(args.content), "title": args.title,
                         "project": args.project, "tags": args.tags, "cwd": cwd}
    if v == "append":
        return "append", {"relpath": args.relpath,
                          "content": _read_content(args.content),
                          "heading": args.heading, "project": args.project, "cwd": cwd}
    if v == "edit":
        return "edit", {"relpath": args.relpath,
                        "new_content": _read_content(args.content),
                        "project": args.project, "cwd": cwd}
    if v == "forget":
        return "forget", {"relpath": args.relpath, "project": args.project, "cwd": cwd}
    if v == "move":
        return "move", {"relpath": args.relpath, "to_project": args.to_project,
                        "to_relpath": args.to_relpath, "project": args.project,
                        "cwd": cwd}
    if v == "reindex":
        return "reindex", {"relpath": args.relpath, "project": args.project, "cwd": cwd}
    if v == "reconcile":
        return "reconcile", {}
    if v == "versions":
        return "versions", {"relpath": args.relpath, "project": args.project, "cwd": cwd}
    if v == "restore":
        return "restore", {"relpath": args.relpath, "version": args.version,
                           "project": args.project, "cwd": cwd}
    if v == "import":
        return "import", {"project": args.project, "cwd": cwd}
    if v == "import-memory":
        return "import_memory", {"project": args.project, "cwd": cwd}
    if v == "distill":
        return "distill", {"relpath": args.relpath, "project": args.project, "cwd": cwd}
    if v in ("elaborate", "summarize"):
        return v, {"label": args.label, "relpath": args.relpath,
                   "project": args.project, "overwrite": args.overwrite,
                   "cwd": cwd}
    if v == "snapshot":
        return "snapshot", {"message": args.message}
    if v == "history":
        return "history", {"relpath": args.relpath}
    if v == "projects":
        return "projects", {}
    if v == "code-lookup":
        return "code_lookup", {"query": args.query, "project": args.project,
                               "k": args.k, "cwd": cwd}
    if v == "code-xref":
        return "code_xref", {"symbol": args.symbol, "project": args.project, "cwd": cwd}
    if v == "code-dossier":
        return "code_dossier", {"symbol": args.symbol, "project": args.project, "cwd": cwd}
    if v == "code-graph":
        return "code_graph", {"symbol": args.symbol,
                              "direction": _graph_direction(args),
                              "depth": args.depth, "project": args.project, "cwd": cwd}
    if v == "code-index":
        # resolve the path client-side (the daemon's cwd differs from the caller's)
        return "code_index", {"path": str(Path(args.path).expanduser().resolve()),
                              "project": args.project, "cwd": cwd}
    if v == "code-append":
        return "code_append", {"symbol": args.symbol, "text": _read_content(args.text),
                               "project": args.project, "cwd": cwd}
    if v == "code-edit":
        return "code_edit", {"symbol": args.symbol,
                             "new_content": _read_content(args.text),
                             "project": args.project, "cwd": cwd}
    if v == "code-forget":
        return "code_forget", {"symbol": args.symbol, "project": args.project, "cwd": cwd}
    if v == "code-read":
        return "code_read", {"symbol": args.symbol, "project": args.project, "cwd": cwd}
    if v == "code-reaffirm":
        return "code_reaffirm", {"symbol": args.symbol, "project": args.project, "cwd": cwd}
    if v == "code-learnings":
        return "code_learnings", {"project": args.project, "orphans_only": args.orphans,
                                  "cwd": cwd}
    if v == "code-rehome":
        return "code_rehome", {"old_fqn": args.old, "new_fqn": args.new,
                               "project": args.project, "cwd": cwd}
    raise SystemExit(f"crib: unknown verb {v!r}")


def _run_daemon(args: Any, cfg: Any) -> None:
    from .client import DaemonClient

    tool, call_args = _verb_call(args)
    with DaemonClient(cfg.daemon) as client:
        data = client.call(tool, call_args)
    if args.cmd == "read":
        _print_note(data, args.json)
    elif tool == "apropos":            # apropos verb, or `search --render`
        _emit_apropos(data, args.json)
    elif args.cmd == "code-graph":
        _emit_code_graph(data, args)
    elif args.cmd == "code-dossier":
        _emit_code_dossier(data, args.json)
    elif args.cmd in ("code-lookup", "code-xref", "code-index"):
        _emit_code(data, args.cmd, args.json)
    elif args.cmd in ("code-append", "code-edit", "code-forget", "code-read",
                      "code-reaffirm"):
        _emit_code_learning(data, args.cmd, args.json)
    elif args.cmd == "code-learnings":
        _emit_code_report(data, args.json)
    elif args.cmd == "code-rehome":
        _emit_code_rehome(data, args.json)
    elif args.cmd in _RAW_PRINT:
        print(data)
    else:
        _emit(data, args.json)


def _run_git(args: Any, cfg: Any) -> int:
    """Share notes via git. Runs git client-side (the user's terminal owns auth);
    a pull that changes files then triggers a reindex through the daemon (or
    in-process). Not an MCP tool — pushing notes is outward-facing + interactive."""
    from .gitbacking import GitBacking
    from .paths import Paths

    # setup runs on a fresh machine where the data dir may not exist yet
    paths = Paths.resolve().ensure() if args.cmd == "setup" else Paths.resolve()
    git = GitBacking(paths.data_dir)

    if args.cmd == "setup":
        remote = getattr(args, "remote", None) or git.current_remote() or _prompt_remote()
        if not remote:
            print("crib setup: no remote given (pass --remote <url>)", file=sys.stderr)
            return 1
        print(f"joining {remote} …")
        res = git.setup(remote)
    elif args.cmd == "sync":
        if getattr(args, "remote", None):
            print(git.init(args.remote))
        res = git.sync(args.message)
    elif args.cmd == "push":
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
    crib = Crib.open()
    cwd = Path.cwd()
    j = args.json
    try:
        if args.cmd in ("lookup", "search") and not getattr(args, "render", False):
            _emit(crib.lookup(args.query, args.project, args.k, args.tags, cwd=cwd,
                              keyword_labels=_split_labels(
                                  getattr(args, "keywords", None)),
                              keyword_weight=getattr(args, "keyword_weight", None),
                              summary_labels=_split_labels(
                                  getattr(args, "summaries", None)),
                              summary_weight=getattr(args, "summary_weight", None)), j)
        elif args.cmd in ("lookup", "search", "apropos", "a"):
            _emit_apropos(
                crib.apropos(args.query, args.project, args.k, args.tags, cwd=cwd), j)
        elif args.cmd == "read":
            _print_note(crib.read_note(args.relpath, args.project, cwd=cwd), j)
        elif args.cmd == "locate":
            print(crib.locate(args.relpath, args.project, cwd=cwd))
        elif args.cmd == "store":
            _emit(asyncio.run(crib.store_note(
                _read_content(args.content), args.title, args.project,
                args.tags, cwd=cwd)), j)
        elif args.cmd == "append":
            _emit(asyncio.run(crib.append_note(
                args.relpath, _read_content(args.content), args.heading,
                args.project, cwd=cwd)), j)
        elif args.cmd == "edit":
            _emit(asyncio.run(crib.edit_note(
                args.relpath, _read_content(args.content), args.project, cwd=cwd)), j)
        elif args.cmd == "forget":
            _emit(asyncio.run(crib.forget(args.relpath, args.project, cwd=cwd)), j)
        elif args.cmd == "move":
            _emit(asyncio.run(crib.move_note(
                args.relpath, args.to_project, args.to_relpath,
                args.project, cwd=cwd)), j)
        elif args.cmd == "reindex":
            _emit(asyncio.run(crib.reindex(args.relpath, args.project, cwd=cwd)), j)
        elif args.cmd == "reconcile":
            _emit(asyncio.run(crib.reconcile_all()), j)
        elif args.cmd == "versions":
            _emit(crib.list_versions(args.relpath, args.project, cwd=cwd), j)
        elif args.cmd == "restore":
            _emit(asyncio.run(crib.restore(
                args.relpath, args.version, args.project, cwd=cwd)), j)
        elif args.cmd == "import":
            _emit(asyncio.run(crib.import_docs(args.project, cwd=cwd)), j)
        elif args.cmd == "import-memory":
            _emit(asyncio.run(crib.import_claude_memory(args.project, cwd=cwd)), j)
        elif args.cmd == "distill":
            _emit(asyncio.run(crib.distill(args.relpath, args.project, cwd=cwd)), j)
        elif args.cmd == "elaborate":
            _emit(asyncio.run(crib.elaborate(
                args.label, args.relpath, args.project, cwd=cwd,
                overwrite=args.overwrite)), j)
        elif args.cmd == "summarize":
            _emit(asyncio.run(crib.summarize(
                args.label, args.relpath, args.project, cwd=cwd,
                overwrite=args.overwrite)), j)
        elif args.cmd == "snapshot":
            print(crib.snapshot(args.message))
        elif args.cmd == "history":
            _emit(crib.history(args.relpath), j)
        elif args.cmd == "projects":
            _emit(crib.projects(), j)
        elif args.cmd == "code-lookup":
            _emit_code(crib.code_lookup(args.query, args.project, args.k, cwd=cwd),
                       "code-lookup", j)
        elif args.cmd == "code-xref":
            _emit_code(crib.code_xref(args.symbol, args.project, cwd=cwd),
                       "code-xref", j)
        elif args.cmd == "code-dossier":
            _emit_code_dossier(crib.code_dossier(args.symbol, args.project, cwd=cwd), j)
        elif args.cmd == "code-index":
            _emit_code(asyncio.run(crib.code_index(
                str(Path(args.path).expanduser().resolve()), args.project, cwd=cwd)),
                "code-index", j)
        elif args.cmd == "code-graph":
            _emit_code_graph(crib.code_graph(
                args.symbol, _graph_direction(args),
                args.depth, args.project, cwd=cwd), args)
        elif args.cmd == "code-append":
            _emit_code_learning(asyncio.run(crib.code_append(
                args.symbol, _read_content(args.text), args.project, cwd=cwd)),
                "code-append", j)
        elif args.cmd == "code-edit":
            _emit_code_learning(asyncio.run(crib.code_edit(
                args.symbol, _read_content(args.text), args.project, cwd=cwd)),
                "code-edit", j)
        elif args.cmd == "code-forget":
            _emit_code_learning(asyncio.run(crib.code_forget(
                args.symbol, args.project, cwd=cwd)), "code-forget", j)
        elif args.cmd == "code-read":
            _emit_code_learning(crib.code_read(args.symbol, args.project, cwd=cwd),
                                "code-read", j)
        elif args.cmd == "code-reaffirm":
            _emit_code_learning(asyncio.run(crib.code_reaffirm(
                args.symbol, args.project, cwd=cwd)), "code-reaffirm", j)
        elif args.cmd == "code-learnings":
            _emit_code_report(crib.code_learnings(
                args.project, cwd=cwd, orphans_only=args.orphans), j)
        elif args.cmd == "code-rehome":
            _emit_code_rehome(asyncio.run(crib.code_rehome(
                args.old, args.new, args.project, cwd=cwd)), j)
    finally:
        crib.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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

    from .config import Config
    from .paths import Paths

    cfg = Config.load(Paths.resolve().config_file)
    if args.cmd in ("setup", "sync", "push", "pull"):
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
