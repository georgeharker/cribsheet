"""Old build vs new build: run one query set through two crib builds and diff.

The acceptance gate for the unified-tables work (design: "Unified item and edge
tables in the DuckDB cache"). Each build runs against its OWN copy of a frozen
data snapshot, with its OWN config and index dirs, so neither ever sees the
other's writes (a stage may rewrite frontmatter) and neither touches the live
~/.local/share/crib or ~/.cache/crib.

    # 1. freeze projects (data + their repos' tracked files) once
    python scripts/parity.py snapshot cribsheet svg-mcp un-bien --out /tmp/parity/snap
    # 2. an old build in its own venv (any released version)
    python scripts/parity.py old-venv 0.22.1 --venv ~/.cache/crib-parity/0.22.1
    # 3. run both builds and diff them
    python scripts/parity.py compare /tmp/parity/snap \\
        --a ~/.cache/crib-parity/0.22.1/bin/python --b .venv/bin/python \\
        --work /tmp/parity/run

`compare` with the same interpreter for --a and --b measures the noise floor:
anything that differs there is volatile, not a behaviour change.

Determinism: the hash embedder, no LLM (generation has no providers in the
fresh config), and no code revalidation (snapshots drop symbol_index's
`.source_root`, so code queries read the TOMLs as frozen). Each build is a
separate process started with `python -I` from inside its work dir, so the old
build can only import its own installed crib, never this checkout's.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

# ── snapshot ─────────────────────────────────────────────────────────────────


def _git_archive(repo: Path, dest: Path) -> None:
    """The repo's tracked files at HEAD, unpacked into `dest` (no .git)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crib import proc  # never fork

    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "repo.tar"
        git = shutil.which("git") or "/usr/bin/git"
        proc.run(
            [git, "-C", str(repo), "archive", "--format=tar", "-o", str(tar), "HEAD"],
            check=True,
        )
        with tarfile.open(tar) as t:
            t.extractall(dest, filter="data")


def snapshot(projects: list[str], out: Path, data_root: Path) -> None:
    if out.exists():
        raise SystemExit(f"{out} exists; pick a new directory")
    (out / "data" / "projects").mkdir(parents=True)
    repos: dict[str, str] = {}
    for name in projects:
        src = data_root / "projects" / name
        if not src.is_dir():
            raise SystemExit(f"no project {name!r} under {data_root}")
        dst = out / "data" / "projects" / name
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store"))
        # Frozen code index: without a source root, code queries read the TOMLs
        # and never re-extract (no LSP, no LLM).
        (dst / "symbol_index" / ".source_root").unlink(missing_ok=True)
        sources = dst / "doc-sources.json"
        if sources.exists():
            mapping = json.loads(sources.read_text())
            frozen = {}
            for prefix, repo in mapping.items():
                key = Path(repo).name
                target = out / "repos" / key
                if not target.exists():
                    _git_archive(Path(repo), target)
                repos[key] = repo
                frozen[prefix] = "<SNAP>/repos/" + key
            sources.write_text(json.dumps(frozen, indent=2))
    (out / "manifest.json").write_text(
        json.dumps({"projects": projects, "repos": repos}, indent=2)
    )
    print(f"snapshot of {', '.join(projects)} -> {out}")


# ── the old build ────────────────────────────────────────────────────────────


def old_venv(version: str, venv: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crib import proc

    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv not found on PATH")
    proc.run([uv, "venv", "--python", sys.executable, str(venv)], check=True)
    proc.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            f"cribsheet=={version}",
        ],
        check=True,
    )
    print(f"cribsheet {version} -> {venv}")


# ── queries ──────────────────────────────────────────────────────────────────


def _frontmatter(path: Path) -> dict[str, Any]:
    import yaml

    text = path.read_text(errors="replace")
    if not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except Exception:  # noqa: BLE001 — a broken file just isn't a query source
        return {}


def build_queries(snap: Path, per_kind: int = 15) -> list[dict[str, Any]]:
    """The same deterministic query set for every build, drawn from the snapshot."""
    import tomllib

    projects = json.loads((snap / "manifest.json").read_text())["projects"]
    qs: list[dict[str, Any]] = []

    def add(tool: str, **args: Any) -> None:
        qs.append({"tool": tool, "args": args})

    for p in projects:
        root = snap / "data" / "projects" / p
        notes = sorted((root / "notes").rglob("*.md"))[:per_kind]
        for f in notes:
            fm = _frontmatter(f)
            add("note_lookup", query=str(fm.get("title") or f.stem), project=p, k=8)
            for tag in (fm.get("tags") or [])[:1]:
                add(
                    "note_lookup",
                    query=str(fm.get("title") or f.stem),
                    project=p,
                    k=8,
                    tags=[tag],
                )
        for pillar, lookup, read in (
            ("design", "design_lookup", "design_read"),
            ("plans", "plan_lookup", "plan_read"),
        ):
            files = sorted((root / pillar).glob("*.md"))
            for f in files[:per_kind]:
                fm = _frontmatter(f)
                add(lookup, query=str(fm.get("title") or f.stem), project=p, k=8)
                add(read, ref=f.name, project=p)
        for f in sorted((root / "design").glob("*.md"))[:5]:
            add("design_tree", ref=f.name, project=p)
        add("design_check", project=p)
        add("design_list", project=p)
        add("plan_next", project=p)
        add("plan_list", project=p)
        syms = sorted((root / "symbol_index").glob("*.toml"))[:per_kind]
        for f in syms:
            try:
                rec = tomllib.loads(f.read_text())
            except (tomllib.TOMLDecodeError, OSError):
                continue
            fq = str(rec.get("fqn") or rec.get("name") or "")
            if not fq:
                continue
            add(
                "code_lookup",
                query=str(rec.get("description") or fq)[:120],
                project=p,
                k=8,
            )
            add("code_xref", symbol=fq, project=p)
            add("code_dossier", symbol=fq, project=p)
        add("project_status", project=p)
    return qs


# ── probe: runs INSIDE one build's interpreter ───────────────────────────────

_VOLATILE_KEYS = {
    "crib_version",
    "elapsed",
    "elapsed_s",
    "took",
    "took_ms",
    "duration_ms",
    "generated_at",
    "now",
    "timestamp",
    "uptime",
    "pid",
    "file_mtime",
    "mtime",
    "store_stats",
    "embed_cache",
    "lsp_sessions",
}


def _normalize(value: Any, work: str) -> Any:
    if isinstance(value, dict):
        return {
            k: _normalize(v, work)
            for k, v in sorted(value.items())
            if k not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_normalize(v, work) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 4)
    if isinstance(value, str):
        return value.replace(work, "<WORK>")
    return value


async def _probe_async(queries: list[dict[str, Any]], work: str) -> list[Any]:
    from fastmcp import Client

    from crib.app import Crib
    from crib.server import build_server

    crib = Crib.open()
    try:
        await crib.reconcile_all()
        for p in json.loads(os.environ["PARITY_PROJECTS"]):
            with contextlib.suppress(Exception):  # a project without docs
                await crib.index_docs_insitu(p)
        mcp = build_server(crib)
        out: list[Any] = []
        async with Client(mcp) as c:
            for q in queries:
                try:
                    res = await c.call_tool(q["tool"], q["args"], raise_on_error=False)
                    data = getattr(res, "structured_content", None)
                    if data is None:
                        data = [getattr(b, "text", None) for b in res.content]
                    if getattr(res, "is_error", False):
                        data = {"error": data}
                except Exception as e:  # noqa: BLE001 — recorded, compared
                    data = {"exception": type(e).__name__, "message": str(e)}
                out.append(_normalize(data, work))
        return out
    finally:
        crib.close()


def probe(queries_file: Path, out_file: Path) -> None:
    queries = json.loads(queries_file.read_text())
    work = os.environ["PARITY_WORK"]
    results = asyncio.run(_probe_async(queries, work))
    out_file.write_text(json.dumps(results, indent=1, sort_keys=True))


# ── compare ──────────────────────────────────────────────────────────────────

_CONFIG = """\
[embed]
model = "hash"

[store]
backend = "duckdb"

[duckdb]
mode = "embedded"
"""


def _run_build(
    label: str, python: Path, snap: Path, work: Path, queries_file: Path
) -> Path:
    from crib import proc

    root = work / label
    if root.exists():
        shutil.rmtree(root)
    data = root / "data"
    shutil.copytree(snap / "data", data)
    # each build's own frozen repo copies, with paths pointing at them
    shutil.copytree(snap / "repos", root / "repos") if (
        snap / "repos"
    ).exists() else None
    for f in data.glob("projects/*/doc-sources.json"):
        f.write_text(f.read_text().replace("<SNAP>", str(root)))
    (root / "config").mkdir()
    (root / "config" / "config.toml").write_text(_CONFIG)
    (root / "index").mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CRIB_")},
        "CRIB_CONFIG_DIR": str(root / "config"),
        "CRIB_DATA_DIR": str(data),
        "CRIB_INDEX_DIR": str(root / "index"),
        "PARITY_WORK": str(root),
        "PARITY_PROJECTS": json.dumps(
            json.loads((snap / "manifest.json").read_text())["projects"]
        ),
    }
    script = root / "parity_probe.py"
    shutil.copy(Path(__file__), script)
    out = root / "results.json"
    r = proc.run(
        [str(python), "-I", str(script), "_probe", str(queries_file), str(out)],
        env=env,
        cwd=root,
    )
    if r.returncode != 0:
        raise SystemExit(f"{label} build failed (exit {r.returncode})")
    return out


def _first_diff(a: Any, b: Any, path: str = "$") -> str | None:
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: only in {'b' if k not in a else 'a'}"
            d = _first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r:.80} vs {b!r:.80}"


def compare(snap: Path, a: Path, b: Path, work: Path) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    work.mkdir(parents=True, exist_ok=True)
    queries = build_queries(snap)
    qfile = work / "queries.json"
    qfile.write_text(json.dumps(queries, indent=1))
    ra = json.loads(_run_build("a", a.absolute(), snap, work, qfile).read_text())
    rb = json.loads(_run_build("b", b.absolute(), snap, work, qfile).read_text())
    diffs = []
    for q, x, y in zip(queries, ra, rb):
        d = _first_diff(x, y)
        if d:
            diffs.append({"tool": q["tool"], "args": q["args"], "first_diff": d})
    by_tool: dict[str, list[int]] = {}
    for q in queries:
        by_tool.setdefault(q["tool"], [0, 0])[0] += 1
    for d in diffs:
        by_tool[d["tool"]][1] += 1
    (work / "diffs.json").write_text(json.dumps(diffs, indent=1))
    print(f"{len(queries)} queries, {len(diffs)} differ  (details: {work}/diffs.json)")
    for tool, (n, bad) in sorted(by_tool.items()):
        print(f"  {tool:<16} {n:>4} queries  {bad:>4} differ")
    return 1 if diffs else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("projects", nargs="+")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument(
        "--data-root",
        type=Path,
        default=Path(
            os.environ.get("CRIB_DATA_DIR", "~/.local/share/crib")
        ).expanduser(),
    )
    v = sub.add_parser("old-venv")
    v.add_argument("version")
    v.add_argument("--venv", type=Path, required=True)
    c = sub.add_parser("compare")
    c.add_argument("snap", type=Path)
    c.add_argument("--a", type=Path, required=True)
    c.add_argument("--b", type=Path, required=True)
    c.add_argument("--work", type=Path, required=True)
    p = sub.add_parser("_probe")  # internal: runs inside one build
    p.add_argument("queries", type=Path)
    p.add_argument("out", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snapshot(args.projects, args.out, args.data_root)
    elif args.cmd == "old-venv":
        old_venv(args.version, args.venv.expanduser())
    elif args.cmd == "compare":
        return compare(args.snap, args.a.expanduser(), args.b.expanduser(), args.work)
    else:
        probe(args.queries, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
