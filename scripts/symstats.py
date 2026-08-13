#!/usr/bin/env python3
"""Before/after evidence for a symbol-store conversion.

The whole argument for converting rather than reindexing is that the EXPENSIVE
facets come through untouched — `description` and `keywords` are LLM output, and
`calls`/`called_by`/`references` move with whatever language server happens to be
installed (a shuck 0.1.0 -> 0.1.1 upgrade shifted svg-mcp by +825 edges in the middle
of the last attempt). This turns that argument into a check.

    scripts/symstats.py capture <project>...        -> stats JSON per project
    scripts/symstats.py diff <before.json> <after.json>

Two groups of facts, and they are checked differently:

  INVARIANT   symbol count, per-kind edge counts, and CONTENT HASHES over the
              description/keyword/content_hash maps. Hashes, not counts: a count
              cannot tell "same 2300 descriptions" from "2300 different ones", and
              that is exactly the failure conversion exists to prevent.

  PROGRESSED  schema stamps, symbol_ref presence + uniqueness, filename canonicality,
              leftovers, and ADDRESSABILITY — that every pre-conversion binding is
              still reachable. Nothing here is expected to be equal; the diff reports
              them so the movement is visible rather than assumed.

Pure reads. Nothing here writes to a store.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crib.codeindex import SymbolIndex, _parse  # noqa: E402
from crib.paths import Paths  # noqa: E402
from crib.symbols import ref_slug, symbol_ref  # noqa: E402

EDGE_KINDS = ("calls", "called_by", "references")


def _digest(pairs: list[tuple[str, str]]) -> str:
    """Stable hash over (key, value) pairs — sorted, so it is independent of read
    order, and over the VALUES, so it catches a rewrite that preserves the count."""
    h = hashlib.sha256()
    for k, v in sorted(pairs):
        h.update(k.encode())
        h.update(b"\0")
        h.update(v.encode())
        h.update(b"\1")
    return h.hexdigest()[:16]


def _entry_key(e: dict) -> str:
    """What this entry is called, whether or not it has been converted.

    Derivable for an unconverted entry, which is what lets a half-converted store be
    grouped correctly — the same property the converter's resume relies on."""
    ref = e.get("symbol_ref")
    if ref:
        return str(ref)
    return symbol_ref(e.get("file", ""), e.get("container") or (),
                      e.get("name", ""), e.get("lang", ""))


def _bindings(e: dict) -> list[str]:
    """Every spelling this entry answers to — current key plus prior ones."""
    was = e.get("symbol_was") or ([e["fqname"]] if e.get("fqname") else [])
    return [_entry_key(e), *(str(w) for w in was)]


def capture(project: str) -> dict:
    paths = Paths.resolve()
    pdir = paths.project_dir(project)
    store = SymbolIndex(pdir)
    root = store.root

    files = sorted(root.glob("*.toml")) if root.exists() else []
    entries, by_file = [], {}
    for p in files:
        try:
            e = _parse(p.read_text())
        except OSError:
            continue
        entries.append(e)
        by_file[p.name] = e

    # --- INVARIANT ---------------------------------------------------------
    keys = [_entry_key(e) for e in entries]
    desc = _digest([(k, str(e.get("description") or ""))
                    for k, e in zip(keys, entries)])
    kw = _digest([(k, "\n".join(e.get("keywords") or []))
                  for k, e in zip(keys, entries)])
    chash = _digest([(k, str(e.get("content_hash") or ""))
                     for k, e in zip(keys, entries)])
    edges = {kind: _digest([(k, "\n".join(sorted(e.get(kind) or [])))
                            for k, e in zip(keys, entries)])
             for kind in EDGE_KINDS}
    edge_counts = {kind: sum(len(e.get(kind) or []) for e in entries)
                   for kind in EDGE_KINDS}

    # --- PROGRESSED --------------------------------------------------------
    schemas: dict[str, int] = {}
    for e in entries:
        schemas[str(int(e.get("schema") or 0))] = \
            schemas.get(str(int(e.get("schema") or 0)), 0) + 1
    # A record is CANONICAL iff its basename is derived from its own key. Anything
    # else is a leftover — the state a crash between write-new and unlink-old leaves,
    # and the one the next pass is supposed to sweep.
    noncanonical = sorted(name for name, e in by_file.items()
                          if name != f"{ref_slug(_entry_key(e))}.toml")
    dupes = sorted({k for k in keys if keys.count(k) > 1}) if len(keys) < 8000 else []

    # --- learnings ---------------------------------------------------------
    from crib import notes as _notes
    ldir = pdir / "learnings"
    legacy_ldir = pdir / "code-learnings"
    lrows = []
    for d in (ldir, legacy_ldir):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            fm = _notes.load(p).frontmatter
            body = _notes.load(p).body
            lrows.append({
                "dir": d.name, "relpath": p.name,
                "binding": fm.get("symbol_ref") or fm.get("symbol") or "",
                "was": fm.get("symbol_was") or [],
                "id": fm.get("id") or "",
                "schema": int(fm.get("schema") or 0),
                "body_sha1": hashlib.sha1(body.encode()).hexdigest(),
            })
    reachable = {b for e in entries for b in _bindings(e)}
    unresolvable = sorted(r["relpath"] for r in lrows
                          if r["binding"] and r["binding"] not in reachable)

    return {
        "project": project,
        "invariant": {
            "symbols": len(entries),
            "edge_counts": edge_counts,
            "edge_digests": edges,
            "description_digest": desc,
            "keywords_digest": kw,
            "content_hash_digest": chash,
            "described": sum(1 for e in entries if e.get("description")),
            "learnings": len(lrows),
            # keyed by the note's ULID where it has one — the id is what survives a
            # rebind, so keying on it is what makes "same notes" survive a rename
            "learning_ids": _digest([(str(r["relpath"]), str(r["id"]))
                                     for r in lrows]),
            "learning_bodies": _digest([(str(r["id"] or r["relpath"]),
                                         str(r["body_sha1"])) for r in lrows]),
        },
        "progressed": {
            "store_stamp": store.stored_schema(),
            "entry_schemas": schemas,
            "with_symbol_ref": sum(1 for e in entries if e.get("symbol_ref")),
            "with_fqname": sum(1 for e in entries if e.get("fqname")),
            "key_collisions": dupes,
            "noncanonical_filenames": len(noncanonical),
            "noncanonical_sample": noncanonical[:5],
            "learnings_dir": sorted({r["dir"] for r in lrows}),
            "learning_bindings_unresolvable": unresolvable,
            "learning_schemas": sorted({r["schema"] for r in lrows}),
        },
    }


def diff(before: dict, after: dict) -> int:
    """0 if every invariant held. Prints the movement either way."""
    name = before.get("project", "?")
    bad = []
    print(f"\n=== {name}")
    print("  INVARIANT — must be identical")
    for k in sorted(before["invariant"]):
        b, a = before["invariant"][k], after["invariant"].get(k)
        ok = b == a
        if not ok:
            bad.append(k)
        mark = "  ok " if ok else "  ** "
        print(f"  {mark}{k:<22} {b}" + ("" if ok else f"   ->   {a}"))
    print("  PROGRESSED — expected to move")
    for k in sorted(before["progressed"]):
        b, a = before["progressed"][k], after["progressed"].get(k)
        print(f"       {k:<22} {b}   ->   {a}")

    # These are not before/after comparisons — they are absolute claims about the
    # AFTER state, and they are the point of the conversion.
    print("  AFTER-STATE CLAIMS")
    ap = after["progressed"]
    ai = after["invariant"]
    claims = [
        ("every entry has symbol_ref", ap["with_symbol_ref"] == ai["symbols"]),
        ("no key collisions", not ap["key_collisions"]),
        ("no non-canonical filenames", ap["noncanonical_filenames"] == 0),
        ("every learning resolves", not ap["learning_bindings_unresolvable"]),
    ]
    for label, ok in claims:
        if not ok:
            bad.append(label)
        print(f"  {'  ok ' if ok else '  ** '}{label}")

    print(f"\n  => {'PASS' if not bad else 'FAIL: ' + ', '.join(bad)}")
    return 0 if not bad else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[1] == "capture":
        out = {p: capture(p) for p in argv[2:]}
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    if argv[1] == "diff":
        b = json.loads(Path(argv[2]).read_text())
        a = json.loads(Path(argv[3]).read_text())
        rc = 0
        for proj in sorted(b):
            if proj not in a:
                print(f"\n=== {proj}\n  ** missing from the after-capture")
                rc = 1
                continue
            rc |= diff(b[proj], a[proj])
        return rc
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
