#!/usr/bin/env python3
"""Retrieval-quality eval for cribsheet — MRR and recall@k over labeled queries.

Drives the ``crib`` CLI (``crib --json lookup``), deliberately NOT the MCP path:
the measurement substrate must not share the combiner's fragility (see
docs/retrieval-and-adoption.md §4.5 — the combiner dropped tools mid-call while the
daemon stayed healthy). Seed a project first (``crib import`` via the repo's
``.crib``) so the cases have something to match.

    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --k 8 --recall-k 3
    python scripts/eval_retrieval.py --cases other.json --bar-mrr 0.7 --bar-recall 0.9

Exit codes (so it doubles as a regression gate):
    0  all quality bars met
    1  ran fine, but a bar was unmet (a regression)
    2  environment not ready (no ``crib`` on PATH, lookup failed, or the project
       returned zero hits for every case — i.e. unseeded)
"""

from __future__ import annotations

import argparse
import atexit
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_CASES = Path(__file__).resolve().parent / "eval_retrieval.cases.json"


def _labels(spec: str | None) -> list[str] | None:
    """`--keywords a,b` → ["a","b"]; `""` → [] ("no labels", turning a default-on
    index OFF); None → None ("use the config default"). Mirrors `cli._split_labels`
    — the empty-vs-absent distinction is what lets a lift baseline disable an index,
    and collapsing the two reports every lift as a Δ0 null."""
    if spec is None:
        return None
    return [s.strip() for s in spec.split(",") if s.strip()]


class _DaemonSession:
    """ONE event loop and ONE open connection, held for the whole run.

    Two layers of per-call cost are being removed here, and they are different sizes.
    The big one was a `crib` SUBPROCESS per query — ~1.4s of CPU in Python imports
    against a retrieval that is a rounding error beside it. The small one is that
    `DaemonClient.call` is itself per-call: it opens a fastmcp `Client` and spins an
    `asyncio.run` every time. Keeping both open across the run removes the second.

    This is not micro-optimisation for its own sake. At the subprocess cost the
    n=1876 gold set was a ~66-minute pass, so in practice it never ran and a
    31-phrasing hand set — with about 2 effective samples for the effects being
    measured — stood in as the retrieval gate."""

    def __init__(self, cfg: Any) -> None:
        import asyncio

        from fastmcp import Client

        from crib.client import DaemonClient
        # DaemonClient owns the LIFECYCLE: sharedserver.use() starts the daemon if it
        # isn't up. Keep it entered for the whole run so the daemon isn't reaped
        # mid-sweep, and let its own `call` make the first request — that path waits
        # for readiness, which a raw Client would not.
        self._dc = DaemonClient(cfg)
        self._dc.__enter__()
        self._loop = asyncio.new_event_loop()
        self._client = Client(self._dc.url)
        self._entered = False

    def _ensure(self) -> None:
        if not self._entered:
            self._loop.run_until_complete(self._client.__aenter__())
            self._entered = True

    def call_ready(self, tool: str, args: dict[str, Any]) -> Any:
        """First call — goes through DaemonClient so it waits for readiness."""
        return self._dc.call(tool, args)

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        from crib.client import _data
        self._ensure()
        args = {k: v for k, v in args.items() if v is not None}
        return _data(self._loop.run_until_complete(
            self._client.call_tool(tool, args)))

    def close(self) -> None:
        try:
            if self._entered:
                self._loop.run_until_complete(
                    self._client.__aexit__(None, None, None))
        except Exception:
            pass
        finally:
            try:
                self._loop.close()
            finally:
                self._dc.__exit__(None, None, None)


_SESSION: Any = None
_SESSION_TRIED = False


def _session() -> Any:
    """The shared session, or None when there is no reachable daemon (or no
    importable `crib`) — callers then fall back to one subprocess per query."""
    global _SESSION, _SESSION_TRIED
    if _SESSION_TRIED:
        return _SESSION
    _SESSION_TRIED = True
    try:
        from crib.config import Config
        from crib.paths import Paths
        _SESSION = _DaemonSession(Config.load(Paths.resolve().config_file).daemon)
        atexit.register(_SESSION.close)
    except Exception:
        _SESSION = None
    return _SESSION


def run_lookup(query: str, project: str, k: int, crib: str,
               no_daemon: bool = False,
               keywords: str | None = None,
               keyword_weight: float | None = None,
               summaries: str | None = None,
               summary_weight: float | None = None) -> list[dict[str, Any]]:
    """One lookup → its ranked hits (top-first).

    ``keywords``/``keyword_weight`` drive BM25 keyword_index; ``summaries``/
    ``summary_weight`` the dense summary_index aliases — the lift knobs (§3).

    Served by the shared daemon connection when there is one; `--no-daemon` (and an
    unreachable daemon) fall back to one `crib --json lookup` subprocess per query.
    Both paths build the SAME call, so the numbers are comparable across them."""
    client = None if no_daemon else _session()
    if client is not None:
        call: dict[str, Any] = {"query": query, "project": project, "k": k,
                                "tags": None}
        if keywords is not None:
            call["keyword_labels"] = _labels(keywords)
        if keyword_weight is not None:
            call["keyword_weight"] = keyword_weight
        if summaries is not None:
            call["summary_labels"] = _labels(summaries)
        if summary_weight is not None:
            call["summary_weight"] = summary_weight
        try:
            return client.call("note_lookup", call)
        except Exception as e:
            raise RuntimeError(f"daemon lookup failed: {e}") from e
    cmd = [crib, *(["--no-daemon"] if no_daemon else []),
           "--json", "note", "lookup", query, "-p", project, "-k", str(k)]
    if keywords is not None:
        cmd += ["--keywords", keywords]
    if keyword_weight is not None:
        cmd += ["--keyword-weight", str(keyword_weight)]
    if summaries is not None:
        cmd += ["--summaries", summaries]
    if summary_weight is not None:
        cmd += ["--summary-weight", str(summary_weight)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"`crib lookup` failed ({proc.returncode}): {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"non-JSON lookup output: {e}: {proc.stdout[:200]!r}")


def _rel_match(relpath: str, expect: str | list[str]) -> bool:
    """Does ``relpath`` match the expected target? ``expect`` may be a single
    relpath fragment or a list of them (any-match) — some queries genuinely have
    more than one right answer, and a too-narrow label reads a legitimate hit as a
    regression."""
    exps = expect if isinstance(expect, list) else [expect]
    return any(e in relpath for e in exps)


def rank_of(hits: list[dict[str, Any]], expect: str | list[str],
            expect_heading: str | None) -> int | None:
    """1-based rank of the first hit matching ``expect`` (relpath substring/list) and,
    if given, ``expect_heading`` (case-insensitive substring of the section heading).
    None if no hit matches within the returned list."""
    eh = expect_heading.lower() if expect_heading else None
    for i, h in enumerate(hits, start=1):
        if _rel_match(h.get("relpath", ""), expect):
            if eh is None or eh in h.get("heading", "").lower():
                return i
    return None


def load_needs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize to a list of information-needs, each with one target and a list
    of phrasings. Accepts the grouped ``needs`` form or the flat ``cases`` form
    (each flat case → a one-phrasing need)."""
    if "needs" in spec:
        return spec["needs"]
    needs = []
    for c in spec.get("cases", []):
        needs.append(
            {
                "id": c.get("id") or c["expect"],
                "expect": c["expect"],
                "expect_heading": c.get("expect_heading"),
                "project": c.get("project"),
                "queries": [c["query"]],
            }
        )
    return needs


def evaluate(spec: dict[str, Any], k: int, recall_k: int, crib: str,
             no_daemon: bool = False,
             keywords: str | None = None,
             keyword_weight: float | None = None,
             summaries: str | None = None,
             summary_weight: float | None = None) -> list[dict[str, Any]]:
    """One row per (need, phrasing) — so a need with 3 phrasings yields 3 rows."""
    default_project = spec.get("project", "default")
    rows: list[dict[str, Any]] = []
    for need in load_needs(spec):
        project = need.get("project") or default_project
        for query in need["queries"]:
            hits = run_lookup(query, project, k, crib, no_daemon, keywords,
                              keyword_weight, summaries, summary_weight)
            rank = rank_of(hits, need["expect"], need.get("expect_heading"))
            score = next(
                (h.get("score") for h in hits if _rel_match(h.get("relpath", ""), need["expect"])),
                None,
            )
            rows.append(
                {
                    "need": need.get("id") or need["expect"],
                    "query": query,
                    "expect": need["expect"],
                    "heading": need.get("expect_heading"),
                    "rank": rank,
                    "score": score,
                    "n_hits": len(hits),
                    "rr": (1.0 / rank) if rank else 0.0,
                    "hit": bool(rank and rank <= recall_k),
                }
            )
    return rows


def seeded_check(spec: dict[str, Any], k: int, crib: str,
                 no_daemon: bool) -> str | None:
    """None if the store holds content for the projects under test, else the reason.

    ASKS THE STORE, rather than inferring emptiness from query results. `status`
    reports per-project `notes`/`designs`/`plans`/`doc_chunks` in ONE call, so an
    unseeded run is named precisely ("project 'x' holds no content") instead of being
    guessed at from misses — and a genuinely hard query set can never be mistaken for
    an empty store, which a results-based probe cannot promise.

    Falls back to probing a few real lookups when there is no daemon to ask (that
    path has no cheap oracle, so it infers: a seeded store hits on at least one of
    five phrasings, and five consecutive misses is not a corpus, it is an empty
    store)."""
    projects = {need.get("project") or spec.get("project", "default")
                for need in load_needs(spec)}
    session = None if no_daemon else _session()
    if session is not None:
        try:
            rows = session.call_ready("status", {}).get("projects") or []
        except Exception as e:
            return f"cannot reach the crib daemon: {e}"
        counts = {r.get("project"): r for r in rows}
        for proj in sorted(projects):
            row = counts.get(proj)
            if row is None:
                return (f"project {proj!r} is not in the store "
                        f"(known: {', '.join(sorted(filter(None, counts))) or 'none'})")
            held = sum(int(row.get(f) or 0)
                       for f in ("notes", "designs", "plans", "doc_chunks"))
            if held == 0:
                return f"project {proj!r} holds no content (`crib import` to seed it)"
        return None

    seen = 0
    for need in load_needs(spec):
        project = need.get("project") or spec.get("project", "default")
        for query in need["queries"]:
            if run_lookup(query, project, k, crib, no_daemon):
                return None
            seen += 1
            if seen >= 5:
                return "the first 5 queries returned 0 hits — is the project seeded?"
    return None                      # fewer than 5 phrasings: nothing to conclude


def report(rows: list[dict[str, Any]], recall_k: int) -> tuple[float, float]:
    mrr = sum(r["rr"] for r in rows) / len(rows)
    recall = sum(r["hit"] for r in rows) / len(rows)

    by_need: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_need.setdefault(r.get("need", r["expect"]), []).append(r)

    print(f"{'rank':>4} {'score':>6}  {'need':<15} query")
    print("-" * 92)
    for need, rs in by_need.items():
        for r in rs:
            rank = str(r["rank"]) if r["rank"] else "—"
            score = f"{r['score']:.3f}" if r["score"] is not None else "—"
            mark = " " if r["hit"] else "✗"
            q = r["query"] if len(r["query"]) <= 52 else r["query"][:49] + "…"
            print(f"{rank:>4}{mark}{score:>6}  {need:<15} {q}")
    print("-" * 92)

    # Per-need robustness across phrasings: the point of multi-phrasing coverage —
    # a need is only as findable as its *weakest* phrasing.
    print("per-need (phrasings hit / total, worst rank across phrasings):")
    fully_robust = 0
    for need, rs in by_need.items():
        hit = sum(x["hit"] for x in rs)
        ranks = [x["rank"] for x in rs if x["rank"]]
        worst = max(ranks) if len(ranks) == len(rs) else "—"
        if all(x["rank"] == 1 for x in rs):
            fully_robust += 1
        flag = "" if hit == len(rs) else "   ⚠︎ weak phrasing"
        print(f"  {need:<15} {hit}/{len(rs)}   worst={worst}{flag}")
    print("-" * 92)
    print(
        f"MRR = {mrr:.3f}    recall@{recall_k} = {recall:.3f}    "
        f"needs all-rank-1 = {fully_robust}/{len(by_need)}    "
        f"(phrasings n={len(rows)}, needs={len(by_need)})"
    )
    return mrr, recall


def _run_lift(spec: dict[str, Any], args: Any) -> int:
    """Measure index lift: run the full set with no LLM index (baseline), then
    with `--lift` keyword labels and/or `--lift-summaries` summary labels, and
    print MRR/recall for each plus the delta and rank moves."""
    kw = args.lift          # keyword_index labels ("" baseline forced below)
    sm = args.lift_summaries  # summary_index labels
    try:
        base = evaluate(spec, args.k, args.recall_k, args.crib, args.no_daemon,
                        keywords="", summaries="")
        withl = evaluate(spec, args.k, args.recall_k, args.crib, args.no_daemon,
                         keywords=(kw if kw is not None else ""),
                         keyword_weight=args.elab_weight,
                         summaries=(sm if sm is not None else ""),
                         summary_weight=args.summary_weight)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if all(r["n_hits"] == 0 for r in base):
        print("error: every query returned 0 hits — is the project seeded?", file=sys.stderr)
        return 2

    def agg(rows: list[dict[str, Any]]) -> tuple[float, float]:
        return (sum(r["rr"] for r in rows) / len(rows),
                sum(r["hit"] for r in rows) / len(rows))

    bm, br = agg(base)
    wm, wr = agg(withl)
    rk = f"recall@{args.recall_k}"
    parts = []
    if kw is not None:
        parts.append(f"kw={kw}" + (f"@w={args.elab_weight}" if args.elab_weight is not None else ""))
    if sm is not None:
        parts.append(f"sum={sm}" + (f"@w={args.summary_weight}" if args.summary_weight is not None else ""))
    label_col = " ".join(parts) or "index"
    print(f"index lift — baseline vs {label_col}  (n={len(base)} phrasings)")
    print("-" * 62)
    print(f"{'set':<28}{'MRR':>8}{rk:>14}")
    print(f"{'baseline (none)':<28}{bm:>8.3f}{br:>14.3f}")
    print(f"{label_col:<28}{wm:>8.3f}{wr:>14.3f}")
    print(f"{'Δ':<28}{wm - bm:>+8.3f}{wr - br:>+14.3f}")
    print("-" * 62)
    base_rank = {(r["need"], r["query"]): r["rank"] for r in base}
    moves = [(r["need"], r["query"], base_rank.get((r["need"], r["query"])), r["rank"])
             for r in withl if base_rank.get((r["need"], r["query"])) != r["rank"]]
    if moves:
        print("rank moves (baseline → with index):")
        for need, q, b, a in moves:
            arrow = f"{b or '—'} → {a or '—'}"
            print(f"  {need:<15} {arrow:<10} {q[:42]}")
    else:
        print("no rank moves (index changed nothing on this set)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="labeled cases JSON")
    ap.add_argument("--k", type=int, default=8, help="hits to request per query")
    ap.add_argument("--recall-k", type=int, default=3, help="cutoff for recall@k")
    # SMOKE FLOORS, not a quality gate. The default cases file is a 31-phrasing hand
    # set, and 31 samples cannot carry a floor tuned finer than one sample: ONE query
    # crossing rank 3 moves recall by 3.2%. Every floor here was once tuned to ~0.01
    # (recall 0.9 -> 0.83 -> 0.80, MRR 0.75 -> 0.72 -> 0.70 -> 0.71), which is three
    # times finer than the instrument resolves — so each "re-baseline to track reality"
    # was fitting noise, and the last one then encoded that noise as a named regression
    # signature ("summary-only scores 0.705/0.774, so 0.71 catches a silent revert").
    #
    # What that cost, measured: `asks` is worth +0.0795 MRR / +0.0826 recall@3 on the
    # n=1876 gold set (2026-08-06, varying only summary_labels) — 160 queries INTO
    # top-3 against 5 out. On THIS set its isolated lift is +0.000/+0.000, because only
    # 2 of 31 phrasings are affected at all and they cancel (vocab-gap 4->3 in,
    # quarantine 3->4 out). Same rate of influence as the large set (6.5% vs 8.8%),
    # ~2 effective samples, direction a coin flip. The 0.71 floor was reading that
    # coin flip.
    #
    # So these are set where they only catch GROSS breakage — an empty store, a
    # misconfigured project, retrieval returning nothing useful — and the per-need
    # table below is what this set is actually good for: eyeballing which phrasings
    # are weak. Do NOT re-tune these by ±0.01; a change smaller than 3.2% on n=31
    # means nothing.
    #
    # THE REAL GATE is the large set, which has the power to resolve these effects:
    #
    #     python scripts/eval_retrieval.py --cases scripts/eval_data/notes_gold_large.json \
    #         --bar-mrr 0.69 --bar-recall 0.75
    #
    # Baseline there (n=1876, 180 needs, ~13 min on the daemon):
    #     2026-08-06   MRR 0.7165   recall@3 0.7830
    #     2026-08-15   MRR 0.711    recall@3 0.779
    # Unchanged: SE on recall at n=1876, p~0.78 is ~0.010, so both deltas sit inside
    # one standard error. Floors of 0.69 / 0.75 sit ~2 SE below — far enough not to
    # trip on sampling, close enough to catch a real loss (dropping `asks` costs
    # -0.0795 / -0.0826, which is 8 SE and cannot hide). Re-tune THOSE only on a move
    # bigger than ~0.02, and record the measurement when you do.
    ap.add_argument("--bar-mrr", type=float, default=0.60, help="fail under this MRR")
    ap.add_argument("--bar-recall", type=float, default=0.60, help="fail under this recall@k")
    ap.add_argument("--crib", default="crib", help="crib executable")
    ap.add_argument("--no-daemon", action="store_true",
                    help="run each crib call in-process (fresh code, bypasses the warm daemon)")
    ap.add_argument("--keywords", default=None,
                    help="keyword_index labels to fold into BM25 for every query "
                         "('' = none); overrides config for this run")
    ap.add_argument("--summaries", default=None,
                    help="summary_index labels to fold in as dense aliases "
                         "('' = none); overrides config for this run")
    ap.add_argument("--lift", default=None, metavar="LABELS",
                    help="measure lift: baseline (no index) vs these keyword_index "
                         "labels, printing the delta and rank moves")
    ap.add_argument("--lift-summaries", default=None, metavar="LABELS",
                    dest="lift_summaries",
                    help="measure lift of these summary_index labels (dense aliases)")
    ap.add_argument("--elab-weight", type=float, default=None, dest="elab_weight",
                    help="BM25 weight of keyword_index tokens for --lift / --keywords "
                         "(overrides config; e.g. 0.3 damps generic terms)")
    ap.add_argument("--summary-weight", type=float, default=None, dest="summary_weight",
                    help="RRF fusion weight of summary aliases for --lift-summaries "
                         "(overrides config; e.g. 0.15 damps broad summaries)")
    args = ap.parse_args(argv)

    if shutil.which(args.crib) is None:
        print(f"error: `{args.crib}` not on PATH — cannot run the eval", file=sys.stderr)
        return 2

    spec = json.loads(args.cases.read_text())

    try:
        why = seeded_check(spec, args.k, args.crib, args.no_daemon)
    except RuntimeError as e:
        why = str(e)
    if why:
        print(f"error: {why}", file=sys.stderr)
        return 2

    if args.lift is not None or args.lift_summaries is not None:
        return _run_lift(spec, args)

    try:
        rows = evaluate(spec, args.k, args.recall_k, args.crib, args.no_daemon,
                        args.keywords, args.elab_weight, args.summaries)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if all(r["n_hits"] == 0 for r in rows):
        print("error: every query returned 0 hits — is the project seeded? (`crib import`)", file=sys.stderr)
        return 2

    mrr, recall = report(rows, args.recall_k)

    failures = []
    if mrr < args.bar_mrr:
        failures.append(f"MRR {mrr:.3f} < bar {args.bar_mrr}")
    if recall < args.bar_recall:
        failures.append(f"recall@{args.recall_k} {recall:.3f} < bar {args.bar_recall}")
    if failures:
        print("FAIL: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: all quality bars met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
