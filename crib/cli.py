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
    """Parse a `--elaborations a,b,c` spec into a label list (None if unset)."""
    if not spec:
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
