"""Characterization harness for the store restructure (see todos / CodeStore work).

Freeze a project at a git SHA, check it out (with submodules) into an isolated dir,
index it STRUCTURAL-ONLY (LLM describe stubbed → deterministic) into a private data
dir, and diff the on-disk symbol_index. The symbol_index TOMLs ARE the snapshot — no
serializer needed; we just target a fresh CRIB_DATA_DIR per run and diff the dirs.

    python scripts/snapshot_harness.py idempotency <repo> <sha>

`idempotency` indexes the SAME frozen checkout TWICE and diffs the two indexes, so a
zero-source-change diff reveals the VOLATILE FLOOR (what to ignore when comparing a
real restructure). Run before the restructure so we know signal from noise.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _git(*args: str, cwd: str | Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def checkout(repo: str, sha: str, dest: Path) -> Path:
    """Clone `repo` at `sha` into `dest` with submodules — the frozen input."""
    _git("clone", "--recurse-submodules", "--quiet", str(repo), str(dest))
    _git("checkout", "--quiet", sha, cwd=dest)
    _git("submodule", "update", "--init", "--recursive", "--quiet", cwd=dest)
    return dest


def index(checkout_dir: Path, data_root: Path) -> tuple[str, Path]:
    """Index `checkout_dir` structural-only into an isolated data dir; return
    (project, symbol_index_dir). Each call is a fresh process-env + fresh Crib."""
    for k, sub in (("CRIB_DATA_DIR", "data"), ("CRIB_INDEX_DIR", "index"),
                   ("CRIB_CONFIG_DIR", "config")):
        os.environ[k] = str(data_root / sub)
    # structural-only: stub the LLM describe so images are deterministic
    import crib.codeindex as ci
    ci.describe_file = lambda *a, **k: {}          # type: ignore[assignment]
    ci.describe_symbols = lambda *a, **k: {}       # type: ignore[assignment]
    from crib.app import Crib
    from crib.codeindex import SymbolIndex
    from crib.config import Config
    from crib.paths import Paths
    from crib.store import InMemoryStore
    crib = Crib(Paths.resolve().ensure(), Config(), InMemoryStore())
    try:
        res = asyncio.run(crib.project_index(cwd=checkout_dir))
        proj = res["project"]
        return proj, SymbolIndex(crib.paths.project_dir(proj)).root
    finally:
        crib.close()


def _load(sym_dir: Path) -> dict[str, dict]:
    from crib.codeindex import _parse
    return {p.stem: _parse(p.read_text()) for p in sorted(sym_dir.glob("*.toml"))}


def diff_indexes(a: dict[str, dict], b: dict[str, dict],
                 ignore: set[str]) -> dict[str, list]:
    """Compare two {slug: entry} indexes, ignoring `ignore` fields. Report added /
    removed symbols and, per common symbol, the fields that differ."""
    out: dict[str, list] = {"added": sorted(set(b) - set(a)),
                            "removed": sorted(set(a) - set(b)), "changed": []}
    for slug in sorted(set(a) & set(b)):
        ea, eb = a[slug], b[slug]
        fields = (set(ea) | set(eb)) - ignore
        d = {f: (ea.get(f), eb.get(f)) for f in sorted(fields) if ea.get(f) != eb.get(f)}
        if d:
            out["changed"].append((slug, d))
    return out


def idempotency(repo: str, sha: str) -> int:
    ignore_none: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="crib-snap-") as tmp:
        base = Path(tmp)
        co = checkout(repo, sha, base / "src")
        proj, dir_a = index(co, base / "run_a")
        # index the SAME checkout a second time into a fresh data dir
        _, dir_b = index(co, base / "run_b")
        a, b = _load(dir_a), _load(dir_b)
        print(f"project={proj}  symbols: run_a={len(a)} run_b={len(b)}")

        # 1) RAW diff — ignore nothing, to SEE the full volatile floor
        raw = diff_indexes(a, b, ignore_none)
        # tally which fields churned across all changed symbols
        churn: dict[str, int] = {}
        for _slug, d in raw["changed"]:
            for f in d:
                churn[f] = churn.get(f, 0) + 1
        print(f"\nRAW (ignore nothing): +{len(raw['added'])} "
              f"-{len(raw['removed'])} ~{len(raw['changed'])} changed")
        for f, n in sorted(churn.items(), key=lambda x: -x[1]):
            print(f"    field churned: {f:16} in {n} symbols")
        for slug, d in raw["changed"][:3]:
            print(f"    e.g. {slug}:")
            for f, (va, vb) in d.items():
                print(f"        {f}: {va!r}  !=  {vb!r}")

        # 2) diff ignoring the empirically-volatile fields → should be EMPTY
        volatile = set(churn) if not raw["added"] and not raw["removed"] else set()
        clean = diff_indexes(a, b, volatile)
        stable = not clean["added"] and not clean["removed"] and not clean["changed"]
        print(f"\nIgnoring {sorted(volatile)}: "
              f"{'STABLE ✓ (idempotent modulo those fields)' if stable else 'STILL DIFFERS ✗'}")
        if not stable:
            print(f"    +{len(clean['added'])} -{len(clean['removed'])} "
                  f"~{len(clean['changed'])}")
            for slug, d in clean["changed"][:5]:
                print(f"    {slug}: {list(d)}")
        return 0 if stable else 1


def capture(repo: str, sha: str, golden: Path) -> int:
    """Freeze a project's index at `sha` into `golden/` (the pre-restructure oracle):
    a copy of the symbol_index TOMLs + a meta line. Run on the CURRENT code before a
    restructure; `compare` later re-indexes the same SHA and diffs against it."""
    import shutil
    golden = Path(golden)
    with tempfile.TemporaryDirectory(prefix="crib-snap-") as tmp:
        co = checkout(repo, sha, Path(tmp) / "src")
        proj, sym_dir = index(co, Path(tmp) / "run")
        if golden.exists():
            shutil.rmtree(golden)
        shutil.copytree(sym_dir, golden / "symbol_index")
        (golden / "meta").write_text(f"{proj}\n{repo}\n{sha}\n")
        print(f"captured {proj} @ {sha} → {golden}  ({len(_load(golden/'symbol_index'))} symbols)")
    return 0


def compare(golden: Path) -> int:
    """Re-index the golden's SHA with the CURRENT code and diff against the frozen
    index. Floor is ∅ (idempotency-proven), so any diff is a real change."""
    golden = Path(golden)
    proj, repo, sha = (golden / "meta").read_text().splitlines()[:3]
    with tempfile.TemporaryDirectory(prefix="crib-snap-") as tmp:
        co = checkout(repo, sha, Path(tmp) / "src")
        _, sym_dir = index(co, Path(tmp) / "run")
        old, new = _load(golden / "symbol_index"), _load(sym_dir)
        d = diff_indexes(old, new, set())
        n = len(d["added"]) + len(d["removed"]) + len(d["changed"])
        print(f"{proj} @ {sha}: golden={len(old)} now={len(new)}  "
              f"+{len(d['added'])} -{len(d['removed'])} ~{len(d['changed'])}")
        for slug in d["added"][:10]:
            print(f"    + {slug}")
        for slug in d["removed"][:10]:
            print(f"    - {slug}")
        for slug, fields in d["changed"][:20]:
            print(f"    ~ {slug}: {', '.join(fields)}")
        print("IDENTICAL ✓" if n == 0 else f"{n} differences — inspect above")
        return 0 if n == 0 else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "idempotency":
        sys.exit(idempotency(a[1], a[2]))
    if len(a) == 4 and a[0] == "capture":
        sys.exit(capture(a[1], a[2], Path(a[3])))
    if len(a) == 2 and a[0] == "compare":
        sys.exit(compare(Path(a[1])))
    print(__doc__)
    print("usage:\n  idempotency <repo> <sha>\n  capture <repo> <sha> <golden_dir>\n"
          "  compare <golden_dir>")
    sys.exit(2)
