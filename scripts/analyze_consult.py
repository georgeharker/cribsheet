#!/usr/bin/env python3
"""Consult/capture behaviour analyzer — does the agent reach for crib, or grep?

Read-only mining of Claude Code transcripts (`~/.config/claude/projects/*/*.jsonl`)
to measure, over an arbitrary time window:

  - consult rate      — sessions that call cribsheet_lookup/apropos (the read side)
  - first-move        — the FIRST information-seeking tool per session (does it reach
                        for crib first, or grep/read code?) — the "rarely consults in
                        first analysis" pattern, made a number
  - code-search vs consult — the volume ratio that sizes the code-xref opportunity
                        (a grep for CODE is a defensible non-consult; crib can't serve
                        it today — this is the confound-immune signal, see
                        docs/code-symbol-index.md)
  - capture rate      — cribsheet_store/append/edit (the save side)
  - basic-memory      — the predecessor tool, a baseline for "reaches for ANY memory"

Windowing (`--since` / `--until`) lets you A/B behaviour across code changes (e.g.
before/after an instructions rewrite, or before/after a coverage build). Availability
buckets (pre-crib / early / current) keep pre-crib sessions from contaminating the
adoption rate while still using them to size the opportunity.

Per-transcript parse results are cached content-addressed (by path+mtime+size) under
`$XDG_CACHE_HOME/crib/consult_analysis/` — a rebuildable cache (transcripts are the
record), so a windowed re-run only reprocesses new/changed sessions.

    python scripts/analyze_consult.py
    python scripts/analyze_consult.py --since 2026-06-28 --until 2026-07-02
    python scripts/analyze_consult.py --project cribsheet --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TRANSCRIPTS = Path(os.path.expanduser("~/.config/claude/projects"))
CACHE = Path(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) \
    / "crib" / "consult_analysis"
CACHE_SCHEMA = 2   # bump to invalidate all caches on a parser change

SOURCE_EXT = {".py", ".rs", ".lua", ".zsh", ".sh", ".ts", ".tsx", ".js", ".jsx",
              ".go", ".c", ".h", ".cc", ".cpp", ".hpp", ".java", ".rb", ".vim"}
SEARCH_CMD = re.compile(r"\b(rg|grep|egrep|fgrep|ag|ack|ast-grep|sg|find|fd|ctags)\b")
# The 2026-06-28 cribsheet instruction rewrite (eval-organic-memory.md) — the
# adoption boundary the wording change was meant to move.
INSTRUCTIONS_REWRITE = "2026-06-28"


def _classify(tool: str, inp: dict) -> tuple[str, str] | None:
    """Map a tool_use to (kind, detail) for the signals we track, or None."""
    t = tool or ""
    if "cribsheet_lookup" in t or "cribsheet_apropos" in t or "cribsheet_read" in t \
            or "cribsheet_locate" in t:
        return ("crib_consult", (inp.get("project") or ""))
    if "cribsheet_store" in t or "cribsheet_append" in t or "cribsheet_edit" in t:
        return ("crib_save", (inp.get("project") or ""))
    if "basic-memory" in t:
        return ("basic_memory", "")
    if t in ("Grep", "Glob"):
        return ("code_search", t)
    if t == "Bash":
        cmd = (inp.get("command") or "")
        if SEARCH_CMD.search(cmd):
            return ("code_search", "bash")
    if t == "Read":
        fp = (inp.get("file_path") or inp.get("path") or "")
        if Path(fp).suffix.lower() in SOURCE_EXT:
            return ("read_source", "")
    return None


def _parse_transcript(path: Path) -> dict:
    """Extract the ordered event stream + session grouping from one .jsonl."""
    events: list[dict] = []
    session_first: dict[str, str] = {}   # sessionId -> first info-seeking kind
    INFO = {"crib_consult", "code_search", "read_source"}
    for line in path.open(errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = d.get("timestamp")
        sid = d.get("sessionId") or path.stem
        msg = d.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            hit = _classify(b.get("name", ""), b.get("input") or {})
            if not hit:
                continue
            kind, detail = hit
            events.append({"ts": ts, "sid": sid, "kind": kind, "detail": detail})
            if kind in INFO and sid not in session_first:
                session_first[sid] = kind
    return {"schema": CACHE_SCHEMA, "events": events, "first_move": session_first}


def _cache_key(path: Path) -> Path:
    st = path.stat()
    h = hashlib.sha1(f"{path}:{st.st_mtime_ns}:{st.st_size}:{CACHE_SCHEMA}"
                     .encode()).hexdigest()
    return CACHE / f"{h}.json"


def _load(path: Path) -> dict:
    ck = _cache_key(path)
    if ck.exists():
        try:
            return json.loads(ck.read_text())
        except ValueError:
            pass
    data = _parse_transcript(path)
    CACHE.mkdir(parents=True, exist_ok=True)
    ck.write_text(json.dumps(data))
    return data


def _project_of(transcript: Path) -> str:
    """Decode the path-encoded project dir (…/-Users-geohar-Development-cribsheet/…)."""
    name = transcript.parent.name
    return name.rsplit("-", 1)[-1] if "-" in name else name


def _crib_first_available() -> str | None:
    """Earliest commit touching any project's notes in the crib data tree — a proxy
    for 'crib could return something'. None if the data tree isn't a git repo."""
    dd = Path(os.environ.get("CRIB_DATA_DIR",
              os.path.expanduser("~/.local/share/crib")))
    try:
        out = subprocess.run(
            ["git", "-C", str(dd), "log", "--diff-filter=A", "--format=%aI",
             "--", "projects"], capture_output=True, text=True, timeout=15)
        dates = [l for l in out.stdout.splitlines() if l.strip()]
        return dates[-1][:10] if dates else None
    except Exception:  # noqa: BLE001
        return None


def _in_window(ts: str | None, since: str | None, until: str | None) -> bool:
    if ts is None:
        return since is None and until is None
    day = ts[:10]
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def _bucket(ts: str | None, crib_first: str | None) -> str:
    if ts is None:
        return "unknown"
    day = ts[:10]
    if crib_first and day < crib_first:
        return "pre-crib"
    if day < INSTRUCTIONS_REWRITE:
        return "early"
    return "current"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="ISO date (YYYY-MM-DD); include events on/after")
    ap.add_argument("--until", help="ISO date (YYYY-MM-DD); include events on/before")
    ap.add_argument("--project", help="substring filter on the transcript's project dir")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--rebuild", action="store_true", help="ignore the parse cache")
    args = ap.parse_args(argv)

    if not TRANSCRIPTS.exists():
        print(f"error: no transcripts at {TRANSCRIPTS}", file=sys.stderr)
        return 2
    if args.rebuild and CACHE.exists():
        for f in CACHE.glob("*.json"):
            f.unlink()

    crib_first = _crib_first_available()
    files = sorted(TRANSCRIPTS.glob("*/*.jsonl"))
    if args.project:
        files = [f for f in files if args.project in f.parent.name]

    # Aggregate, split by availability bucket.
    from collections import defaultdict, Counter
    buckets = ["pre-crib", "early", "current", "unknown"]
    agg: dict[str, Counter] = {b: Counter() for b in buckets}
    sessions: dict[str, set] = {b: set() for b in buckets}
    consult_sessions: dict[str, set] = {b: set() for b in buckets}
    save_sessions: dict[str, set] = {b: set() for b in buckets}
    first_move: dict[str, Counter] = {b: Counter() for b in buckets}
    n_files = 0

    for f in files:
        data = _parse_transcript(f) if args.rebuild else _load(f)
        proj = _project_of(f)
        used = False
        # session -> its events (windowed)
        for e in data["events"]:
            if not _in_window(e["ts"], args.since, args.until):
                continue
            used = True
            b = _bucket(e["ts"], crib_first)
            agg[b][e["kind"]] += 1
            sessions[b].add(e["sid"])
            if e["kind"] == "crib_consult":
                consult_sessions[b].add(e["sid"])
            if e["kind"] == "crib_save":
                save_sessions[b].add(e["sid"])
        if used:
            n_files += 1
        # first-move: the session's EARLIEST windowed info-seeking event
        fm_best: dict[str, tuple[str, str]] = {}   # sid -> (ts, kind)
        for e in data["events"]:
            if e["kind"] in ("crib_consult", "code_search", "read_source") \
                    and _in_window(e["ts"], args.since, args.until):
                ts = e["ts"] or ""
                if e["sid"] not in fm_best or ts < fm_best[e["sid"]][0]:
                    fm_best[e["sid"]] = (ts, e["kind"])
        for sid, (ts, kind) in fm_best.items():
            first_move[_bucket(ts or None, crib_first)][kind] += 1

    _report(agg, sessions, consult_sessions, save_sessions, first_move,
            crib_first, args, n_files)
    return 0


def _report(agg, sessions, consult_sessions, save_sessions, first_move,
            crib_first, args, n_files) -> None:
    order = ["pre-crib", "early", "current"]
    if args.json:
        out = {"crib_first_available": crib_first, "window": [args.since, args.until],
               "transcripts": n_files, "buckets": {}}
        for b in order + ["unknown"]:
            s = len(sessions[b])
            out["buckets"][b] = {
                "sessions": s,
                "events": dict(agg[b]),
                "consult_sessions": len(consult_sessions[b]),
                "save_sessions": len(save_sessions[b]),
                "first_move": dict(first_move[b]),
            }
        print(json.dumps(out, indent=2))
        return

    win = f"{args.since or '…'} → {args.until or '…'}"
    print(f"consult analysis   window={win}   crib first available={crib_first}   "
          f"transcripts touched={n_files}")
    print("=" * 78)
    hdr = f"{'bucket':<9}{'sess':>5}{'consult':>8}{'grep/rd':>8}{'save':>6}" \
          f"{'basicmem':>9}   first-move (consult/code/read)"
    print(hdr); print("-" * 78)
    for b in order:
        s = len(sessions[b])
        if not s:
            continue
        a = agg[b]
        code = a.get("code_search", 0) + a.get("read_source", 0)
        fm = first_move[b]
        fmstr = f"{fm.get('crib_consult',0)}/{fm.get('code_search',0)}/{fm.get('read_source',0)}"
        print(f"{b:<9}{s:>5}{a.get('crib_consult',0):>8}{code:>8}"
              f"{a.get('crib_save',0):>6}{a.get('basic_memory',0):>9}   {fmstr}")
    print("-" * 78)
    # Headline signals on the 'current' bucket (adoption-relevant regime).
    cur = agg["current"]; cs = len(sessions["current"])
    if cs:
        consult = cur.get("crib_consult", 0)
        code = cur.get("code_search", 0) + cur.get("read_source", 0)
        fm = first_move["current"]; fmn = sum(fm.values())
        print(f"CURRENT regime (crib populated, post-instructions-rewrite):")
        print(f"  sessions with a consult : {len(consult_sessions['current'])}/{cs} "
              f"({100*len(consult_sessions['current'])//max(cs,1)}%)")
        print(f"  first-move = consult    : {fm.get('crib_consult',0)}/{fmn} "
              f"({100*fm.get('crib_consult',0)//max(fmn,1)}%)   "
              f"← the 'reaches for crib first' rate")
        print(f"  consult : code-seek     : {consult} : {code}   "
              f"← code-seek volume sizes the code-xref opportunity")
    print("\nNote: a code grep is a DEFENSIBLE non-consult (crib has no code index yet);"
          "\nthat volume is the confound-immune size of the code-xref build's payoff.")


if __name__ == "__main__":
    raise SystemExit(main())
