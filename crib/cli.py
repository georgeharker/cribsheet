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
    if isinstance(item, LookupHit):
        loc = f":{item.line_start}-{item.line_end}" if item.line_start else ""
        head = f"  {item.heading}" if item.heading else ""
        first = item.snippet.splitlines()[0][:100] if item.snippet else ""
        print(f"[{item.score:.3f}] {item.relpath}{loc}{head}\n    {first}")
    elif isinstance(item, dict):
        print("  ".join(f"{k}={v}" for k, v in item.items()))
    else:
        print(item)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crib", description="markdown memory")
    p.add_argument("--mcp", action="store_true", help="run the MCP server")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    # transport options (apply to --mcp and `serve`)
    p.add_argument("--http", action="store_true",
                   help="serve MCP over HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    p.add_argument("--port", type=int, default=8787, help="HTTP port")
    sub = p.add_subparsers(dest="cmd")

    def proj(sp):  # shared --project option
        sp.add_argument("-p", "--project")

    sv = sub.add_parser("serve", help="run the MCP server (stdio or --http)")
    sv.add_argument("--http", action="store_true")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)
    sub.add_parser("projects", help="list projects")
    sub.add_parser("info", help="show resolved paths and available backends")

    s = sub.add_parser("lookup", aliases=["search"], help="semantic search")
    s.add_argument("query"); proj(s)
    s.add_argument("-k", type=int, default=8)
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

    s = sub.add_parser("reindex", help="reindex a note or whole project")
    s.add_argument("relpath", nargs="?"); proj(s)

    sub.add_parser("reconcile", help="sweep all projects for offline changes")

    s = sub.add_parser("versions", help="list recoverable versions of a note")
    s.add_argument("relpath"); proj(s)

    s = sub.add_parser("restore", help="restore a prior version of a note")
    s.add_argument("relpath"); s.add_argument("version"); proj(s)

    s = sub.add_parser("import", help="ingest local docs via the nearest .crib")
    proj(s)

    s = sub.add_parser("snapshot", help="git checkpoint of the data tree")
    s.add_argument("-m", "--message")

    s = sub.add_parser("history", help="git history for a note or the tree")
    s.add_argument("relpath", nargs="?")

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
    info = {
        "config_dir": str(paths.config_dir),
        "data_dir": str(paths.data_dir),
        "index_dir": str(paths.index_dir),
        "embed_model": config.embed.model,
        "chroma_mode": config.chroma.mode,
        "default_project": config.default_project,
        "backends": backends,
    }
    if as_json:
        print(json.dumps(info, indent=2))
        return
    for k in ("config_dir", "data_dir", "index_dir", "embed_model",
              "chroma_mode", "default_project"):
        print(f"{k:18} {info[k]}")
    print("backends:")
    for name, ok in backends.items():
        print(f"  {'✓' if ok else '✗'} {name}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.mcp or args.cmd == "serve":
        from .server import main as serve
        transport = "http" if args.http else "stdio"
        serve(transport, args.host, args.port)
        return 0
    if args.cmd is None:
        build_parser().print_help()
        return 1
    if args.cmd == "info":
        cmd_info(args.json)
        return 0

    crib = Crib.open()
    cwd = Path.cwd()
    j = args.json
    try:
        if args.cmd in ("lookup", "search"):
            _emit(crib.lookup(args.query, args.project, args.k, args.tags, cwd=cwd), j)
        elif args.cmd == "read":
            print(crib.read_note(args.relpath, args.project, cwd=cwd), end="")
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
        elif args.cmd == "snapshot":
            print(crib.snapshot(args.message))
        elif args.cmd == "history":
            _emit(crib.history(args.relpath), j)
        elif args.cmd == "projects":
            _emit(crib.projects(), j)
    finally:
        crib.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
