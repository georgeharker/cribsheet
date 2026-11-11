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
        shutil.copytree(src, dst, symlinks=True,
                        ignore=shutil.ignore_patterns(".DS_Store"))
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
                    # `.crib` declares the repo's docs; it is often untracked,
                    # and without it the snapshot indexes none of them.
                    crib_file = Path(repo) / ".crib"
                    if crib_file.is_file() and not (target / ".crib").exists():
                        shutil.copy2(crib_file, target / ".crib")
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
    "generation_ledger",
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

# No LLM generation: an endpoint nothing listens on, so every attempt fails
# fast, calls nothing external and writes nothing (the default adapter would
# otherwise drive the Claude CLI from the daemon's background catch-up).
[generate]
adapter = "openai-compatible"
endpoint = "http://127.0.0.1:9/v1"
model = "parity-no-generation"
api_key = "none"
timeout = 2.0

[daemon]
name = "{name}"
port = {port}
grace_period = "30s"
"""

_MARKER = "parity-marker"  # a project only a build's own data dir has


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def live_fingerprint(root: Path) -> dict[str, Any]:
    """File count, newest mtime and a digest of (path, size, mtime) for the LIVE
    data dir: identical before and after a run, or the run touched it."""
    import hashlib

    h = hashlib.sha256()
    count, newest = 0, 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            count += 1
            newest = max(newest, st.st_mtime)
            h.update(f"{p}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return {"files": count, "newest_mtime": newest, "digest": h.hexdigest()}


_ULID = None
# Version-ring entries are named <seq>-<content hash>.md; the hash differs per write.
_VERSION_FILE = __import__("re").compile(r"\b(\d{6})-[0-9a-f]{8}\.md\b")


def _stable_ids(value: Any, seen: dict[str, str]) -> Any:
    """Freshly minted ULIDs differ between builds; name them by first appearance."""
    import re

    global _ULID
    if _ULID is None:
        _ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
    if isinstance(value, dict):
        return {k: _stable_ids(v, seen) for k, v in value.items()}
    if isinstance(value, list):
        return [_stable_ids(v, seen) for v in value]
    if isinstance(value, str):
        value = _VERSION_FILE.sub(r"\1-<hash>.md", value)
        return _ULID.sub(lambda m: seen.setdefault(m.group(0), f"<ID{len(seen)}>"),
                         value)
    return value


def _cli_argv(q: dict[str, Any]) -> list[str] | None:
    """The CLI spelling of one read query (None: no CLI twin)."""
    t, a = q["tool"], q["args"]
    p = ["-p", a["project"]]
    k = ["-k", str(a["k"])] if "k" in a else []
    m = {
        "note_lookup": lambda: ["note", "lookup", *p, *k,
                                *sum((["--tag", x] for x in a.get("tags") or []), []),
                                a["query"]],
        "design_lookup": lambda: ["design", "lookup", *p, *k, a["query"]],
        "plan_lookup": lambda: ["plan", "lookup", *p, *k, a["query"]],
        "design_read": lambda: ["design", "read", *p, a["ref"]],
        "plan_read": lambda: ["plan", "read", *p, a["ref"]],
        "design_tree": lambda: ["design", "tree", *p, a["ref"]],
        "design_check": lambda: ["design", "check", *p],
        "design_list": lambda: ["design", "list", *p],
        "plan_next": lambda: ["plan", "next", *p],
        "plan_list": lambda: ["plan", "list", *p],
        "code_lookup": lambda: ["code", "lookup", *p, *k, a["query"]],
        "code_xref": lambda: ["code", "xref", *p, a["symbol"]],
        "code_dossier": lambda: ["code", "dossier", *p, a["symbol"]],
        "project_status": lambda: ["project", "status", *p],
    }.get(t)
    return m() if m else None


_W = "parity-writes"  # the scratch project the write scenario creates

# The write scenario: (label, argv). `{ref:<label>}` is replaced by the relpath
# the step named <label> returned, so later steps address what earlier ones made.
WRITES: list[tuple[str, list[str]]] = [
    ("n1", ["note", "store", "-p", _W, "--title", "Parity note one", "--tag", "alpha",
            "The first parity note. It mentions widgets and gadgets."]),
    ("n1-append", ["note", "append", "-p", _W, "{ref:n1}",
                   "An appended paragraph about gadgets."]),
    ("n2", ["note", "store", "-p", _W, "--title", "Parity note two",
            "A second note about sprockets, unrelated to widgets."]),
    ("d1", ["design", "add", "-p", _W, "Widgets are keyed by serial",
            "Every widget is identified by its serial number; names are display only."]),
    ("d2", ["design", "add", "-p", _W, "--dep", "{ref:d1}",
            "Gadget lookups go through the widget serial",
            "Because widgets are keyed by serial, gadgets reference them by serial."]),
    ("p1", ["plan", "add", "-p", _W, "--dep", "{ref:d2}", "Index gadgets by serial",
            "Build the gadget index on the widget serial."]),
    ("p2", ["plan", "add", "-p", _W, "--dep", "{ref:p1}", "Backfill old gadgets",
            "Re-key existing gadgets once the index exists."]),
    ("next-1", ["plan", "next", "-p", _W]),
    ("check-1", ["design", "check", "-p", _W]),
    ("d1-edit", ["design", "edit", "-p", _W, "{ref:d1}",
                 "Every widget is identified by its serial number AND batch code."]),
    ("check-2", ["design", "check", "-p", _W]),
    ("next-2", ["plan", "next", "-p", _W]),
    ("d2-reaffirm", ["design", "reaffirm", "-p", _W, "{ref:d2}"]),
    ("check-3", ["design", "check", "-p", _W]),
    ("p1-done", ["plan", "status", "-p", _W, "{ref:p1}", "done"]),
    ("next-3", ["plan", "next", "-p", _W]),
    ("tree", ["design", "tree", "-p", _W, "--dependents", "{ref:d1}"]),
    ("d3", ["design", "add", "-p", _W, "Widgets are keyed by serial and batch",
            "Supersedes the serial-only key."]),
    ("d1-supersede", ["design", "supersede", "-p", _W, "{ref:d1}", "{ref:d3}"]),
    ("list-designs", ["design", "list", "-p", _W]),
    ("list-plans", ["plan", "list", "-p", _W, "--all"]),
    ("n2-edit", ["note", "edit", "-p", _W, "{ref:n2}", "Sprockets, revised."]),
    ("n2-versions", ["note", "versions", "-p", _W, "{ref:n2}"]),
    ("lookup-widgets", ["note", "lookup", "-p", _W, "-k", "5", "widgets"]),
    ("lookup-tag", ["note", "lookup", "-p", _W, "--tag", "alpha", "gadgets"]),
    ("design-lookup", ["design", "lookup", "-p", _W, "serial key"]),
    ("n1-forget", ["note", "forget", "-p", _W, "{ref:n1}"]),
    ("lookup-after-forget", ["note", "lookup", "-p", _W, "widgets"]),
    ("status", ["project", "status", "-p", _W]),
]


def _cli(crib_bin: Path, env: dict[str, str], argv: list[str]) -> Any:
    from crib import proc

    r = proc.run([str(crib_bin), "--json", *argv], env=env, capture_output=True,
                 text=True, timeout=600)
    try:
        data = json.loads(r.stdout) if r.stdout.strip() else None
    except json.JSONDecodeError:
        data = {"stdout": r.stdout.strip()[-2000:]}
    if r.returncode != 0:
        return {"exit": r.returncode, "data": data,
                "stderr": r.stderr.strip().splitlines()[-1:] if r.stderr else []}
    return data


def _wait_idle(crib_bin: Path, env: dict[str, str], projects: list[str],
               timeout: float = 900.0) -> None:
    """Wait until the daemon's background work (the startup reconcile, code
    sweeps, any project job) has finished, so every read sees a settled index."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = _cli(crib_bin, env, ["status"])
        busy = not isinstance(st, dict) or st.get("reconciling") or st.get("sweeps")
        if not busy:
            for proj in projects:
                ps = _cli(crib_bin, env, ["project", "status", "-p", proj])
                jobs = ps.get("jobs") if isinstance(ps, dict) else None
                if any(j.get("status") == "running" or j.get("remaining")
                       for j in jobs or []):
                    busy = True
                    break
        if not busy:
            return
        time.sleep(2)
    raise SystemExit("the parity daemon did not settle within the timeout")


def _ref_of(result: Any) -> str | None:
    if isinstance(result, dict):
        for key in ("relpath", "ref"):
            if isinstance(result.get(key), str):
                return result[key]
        for v in result.values():
            if (r := _ref_of(v)) is not None:
                return r
    return None


def _run_build(label: str, python: Path, snap: Path, work: Path, queries_file: Path,
               queries: list[dict[str, Any]], cli: bool, writes: bool) -> dict[str, Any]:
    from crib import proc

    root = work / label
    if root.exists():
        shutil.rmtree(root)
    data = root / "data"
    shutil.copytree(snap / "data", data, symlinks=True)
    if (snap / "repos").exists():  # this build's own frozen repo copies
        shutil.copytree(snap / "repos", root / "repos", symlinks=True)
    for f in data.glob("projects/*/doc-sources.json"):
        f.write_text(f.read_text().replace("<SNAP>", str(root)))
    marker = data / "projects" / _MARKER / "notes"
    marker.mkdir(parents=True)
    (marker / "marker.md").write_text(
        "---\ntitle: parity marker\n---\n\nOnly a parity build's data dir has this.\n")
    name = f"crib-parity-{label}-{os.getpid()}"
    (root / "config").mkdir()
    (root / "config" / "config.toml").write_text(
        _CONFIG.format(name=name, port=_free_port()))
    (root / "index").mkdir()
    bindir = python.parent
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CRIB_")},
        "CRIB_CONFIG_DIR": str(root / "config"),
        "CRIB_DATA_DIR": str(data),
        "CRIB_INDEX_DIR": str(root / "index"),
        # the daemon sharedserver launches is the bare `crib` on PATH: this build's
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PARITY_WORK": str(root),
        "PARITY_PROJECTS": json.dumps(
            json.loads((snap / "manifest.json").read_text())["projects"]),
    }
    script = root / "parity_probe.py"
    shutil.copy(Path(__file__), script)
    out = root / "results.json"
    r = proc.run([str(python), "-I", str(script), "_probe", str(queries_file),
                  str(out)], env=env, cwd=root)
    if r.returncode != 0:
        raise SystemExit(f"{label} build failed (exit {r.returncode})")
    result: dict[str, Any] = {"mcp": json.loads(out.read_text())}
    if not (cli or writes):
        return result
    crib_bin = bindir / "crib"
    try:
        listing = json.dumps(_cli(crib_bin, env, ["project", "list"]))
        if _MARKER not in listing:
            raise SystemExit(
                f"{label}: the CLI's daemon cannot see {_MARKER}; it is not reading "
                f"this build's data dir. Aborting before any query or write.")
        _wait_idle(crib_bin, env, json.loads(env["PARITY_PROJECTS"]))
        if cli:
            # Reads in parallel against the one daemon (MCP over HTTP serves
            # concurrent calls); order is preserved by map.
            from concurrent.futures import ThreadPoolExecutor

            def one(q: dict[str, Any]) -> Any:
                argv = _cli_argv(q)
                return _normalize(_cli(crib_bin, env, argv), str(root)) if argv else None

            with ThreadPoolExecutor(max_workers=8) as pool:
                result["cli"] = list(pool.map(one, queries))
        if writes:
            refs: dict[str, str] = {}
            seen: dict[str, str] = {}
            steps = []
            for step, argv in WRITES:
                argv = [
                    refs.get(a[5:-1], a) if a.startswith("{ref:") else a for a in argv]
                res = _cli(crib_bin, env, argv)
                if (ref := _ref_of(res)) and step not in refs:
                    refs[step] = ref
                steps.append(_stable_ids(_normalize(res, str(root)), seen))
            result["writes"] = steps
    finally:
        sharedserver = shutil.which("sharedserver")
        if sharedserver:
            proc.run([sharedserver, "admin", "stop", name], capture_output=True)
    return result


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


def _intended(d: dict[str, Any], intended: list[dict[str, str]]) -> str | None:
    """The reason, when this difference is one the change MEANS to make."""
    for rule in intended:
        if rule.get("phase", d["phase"]) != d["phase"]:
            continue
        if rule.get("tool", d.get("tool")) != d.get("tool"):
            continue
        if rule.get("step", d.get("step")) != d.get("step"):
            continue
        if d["first_diff"].startswith(rule.get("path_prefix", "")):
            return rule["reason"]
    return None


def compare(snap: Path, a: Path, b: Path, work: Path, cli: bool, writes: bool,
            live: Path, intended: list[dict[str, str]] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    work.mkdir(parents=True, exist_ok=True)
    before = live_fingerprint(live)
    queries = build_queries(snap)
    qfile = work / "queries.json"
    qfile.write_text(json.dumps(queries, indent=1))
    ra = _run_build("a", a.absolute(), snap, work, qfile, queries, cli, writes)
    rb = _run_build("b", b.absolute(), snap, work, qfile, queries, cli, writes)
    after = live_fingerprint(live)
    diffs: list[dict[str, Any]] = []
    tally: dict[str, list[int]] = {}
    for phase in ("mcp", "cli"):
        if phase not in ra:
            continue
        for q, x, y in zip(queries, ra[phase], rb[phase]):
            if phase == "cli" and x is None and y is None:
                continue
            key = f"{phase}:{q['tool']}"
            tally.setdefault(key, [0, 0])[0] += 1
            if d := _first_diff(x, y):
                tally[key][1] += 1
                diffs.append({"phase": phase, "tool": q["tool"], "args": q["args"],
                              "first_diff": d})
    if "writes" in ra:
        for (step, argv), x, y in zip(WRITES, ra["writes"], rb["writes"]):
            tally.setdefault("writes", [0, 0])[0] += 1
            if d := _first_diff(x, y):
                tally["writes"][1] += 1
                diffs.append({"phase": "writes", "step": step, "argv": argv,
                              "first_diff": d})
        (work / "writes-a.json").write_text(json.dumps(ra["writes"], indent=1))
        (work / "writes-b.json").write_text(json.dumps(rb["writes"], indent=1))
    expected = [d for d in diffs if (r := _intended(d, intended or []))
                and not d.update(intended=r)]
    diffs = [d for d in diffs if "intended" not in d]
    (work / "diffs.json").write_text(json.dumps(diffs, indent=1))
    (work / "intended.json").write_text(json.dumps(expected, indent=1))
    n = sum(v[0] for v in tally.values())
    print(f"{n} comparisons, {len(diffs)} differ, {len(expected)} intended  "
          f"(details: {work}/diffs.json)")
    for key, (total, bad) in sorted(tally.items()):
        if bad:
            print(f"  {key:<22} {total:>4}  {bad:>4} differ")
    for d in expected:
        print(f"  intended: {d.get('tool') or d.get('step')}: {d['first_diff']}"
              f"  ({d['intended']})")
    untouched = before == after
    print(f"live data dir {live}: {'untouched' if untouched else 'CHANGED during the run'}")
    if not untouched:
        print(f"  before {before}\n  after  {after}")
    return 1 if diffs or not untouched else 0


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
    c.add_argument("--cli", action="store_true",
                   help="also replay the queries as `crib --json` CLI calls, each "
                   "build through its own private daemon")
    c.add_argument("--writes", action="store_true",
                   help="also run the write scenario in a scratch project")
    c.add_argument("--intended", type=Path, default=None,
                   help="JSON list of differences the change means to make: "
                   "{phase, tool|step, path_prefix, reason}")
    c.add_argument("--live", type=Path,
                   default=Path(os.environ.get("CRIB_DATA_DIR",
                                               "~/.local/share/crib")).expanduser(),
                   help="the live data dir that must stay untouched")
    p = sub.add_parser("_probe")  # internal: runs inside one build
    p.add_argument("queries", type=Path)
    p.add_argument("out", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snapshot(args.projects, args.out, args.data_root)
    elif args.cmd == "old-venv":
        old_venv(args.version, args.venv.expanduser())
    elif args.cmd == "compare":
        return compare(args.snap, args.a.expanduser(), args.b.expanduser(), args.work,
                       args.cli, args.writes, args.live,
                       json.loads(args.intended.read_text()) if args.intended else None)
    else:
        probe(args.queries, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
