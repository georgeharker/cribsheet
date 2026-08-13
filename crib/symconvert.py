"""In-place conversion of stored symbol entries to the current shape.

Conversion applies only when the new fields are pure functions of fields the store
ALREADY holds — `symbol_ref`, `fqn` and `scope` need `file`, `lang`, `container` and
`name`, which every entry ever written carries. When a change needs information the
store does not have, the answer is a reindex, said out loud.

The mechanism is the store\'s own: `SymbolIndex.write` normalizes identity on every
write and files the record under its canonical name, so CONVERTING AN ENTRY IS
READING IT AND WRITING IT BACK with the schema stamped. This module holds the parts
of that worth stating separately — what makes an entry convertible, and the per-entry
proof that conversion touched nothing it does not own.

RESUMABLE WITHOUT A LOCK OR A SENTINEL. Each entry records the schema it was written
at and its filename is derived from its own key, so `done` is answerable per record
and the work-list is derived from the data. A half-converted store is the ordinary
state; the converter\'s own filter is what resumes it. Combined with the store\'s
atomic per-entry write, the worst a crash can leave is a record not yet converted —
exactly the input the next run expects — or a leftover file under a prior name,
which the next write of that entry unlinks.
"""

from __future__ import annotations

from typing import Any

# Fields conversion is allowed to write, and legacy fields it is allowed to REMOVE
# (their content survives: `fqname` becomes `symbol_was[0]`; `module` and `parent`
# are derivable). Everything else must come through byte-identical — that is the
# property that makes this safe over LLM-priced data, and `preserved` asserts it per
# entry rather than trusting these lists.
DERIVABLE = ("symbol_ref", "fqn", "scope", "symbol_was", "schema")
REMOVED = ("fqname", "module", "parent")


def convertible(entry: dict[str, Any]) -> bool:
    """Whether this entry carries the inputs the derived fields need. `file` and
    `name` are the hard requirements; `lang` and `container` have meaningful empty
    defaults. An entry missing `file` cannot be given a reference at all, and must
    be reindexed."""
    return bool(entry.get("file")) and bool(entry.get("name"))


def convert_entry(entry: dict[str, Any], target: int) -> dict[str, Any]:
    """One entry, old shape → current. Pure: no I/O, no LSP, no model.
    Returns a NEW dict; the input is untouched so a caller can diff the two."""
    from .codeindex import SymbolIndex
    out = SymbolIndex.normalize_identity(entry)
    out["schema"] = target
    return out


def preserved(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Fields that changed and were not conversion\'s to change. Empty is the pass.

    Checked per entry, because the whole claim of conversion-over-reindex is that
    the expensive facets — `description`, `keywords`, `calls`, `called_by`,
    `references` — come through untouched."""
    return sorted(k for k in set(before) | set(after)
                  if k not in DERIVABLE and k not in REMOVED
                  and before.get(k) != after.get(k))
