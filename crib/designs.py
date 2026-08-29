"""Design decisions and plan items — facet notes with a dependency graph over them.

Two facets, one module (docs/plans/design-plan-tracking.md):

- **design** — durable decisions in the `design/` pillar store, each declaring
  the decisions it builds on. Editing or deleting one *taints* its dependents so
  a human re-checks them; the point is to make design drift visible instead of
  surprising.
- **plan** — resumable work items in the `plans/` pillar store, with a status,
  the same dependency edges, and a lexorank ordering that survives
  insert-between.

Each pillar is its OWN STORE — a sibling dir under the project's data root,
sharing the note-store implementation (versioning, git sync, the merge driver)
but never the notes search scope: chunks carry a `store` axis, retrieval and the
ranking caches are per-pillar, so decisions never surface in (or re-weight)
`note_lookup` and `design_lookup`/`plan_lookup` search pure facet pools.
Relpaths are store-relative (`base.md`); a citation of a facet note from
another store spells it qualified (`design:base.md`). The only machinery here is
the graph layer: the graph load, cycle detection, the body-hash taint, ref
resolution and the rank arithmetic.

Staleness is COMPUTED ON READ and RECORDED ON REAFFIRM: each node keeps
`checked: {dep_id: <body hash at last reaffirm>}`, and `*_check` compares that
against the deps' current hashes. Nothing propagates on write — which is exactly
why a design edited by a plain file write (or by hand, or by a git pull) is
caught all the same. `Designs` takes an explicit resolved `project`, like
`Learnings`; Crib keeps resolve_project + delegate.

EVERY EDGE CHECKS. There is one edge semantics for taint: a dep edge propagates
checking, always. An edge kind that doesn't check ("informed-by") would be the
hole through which an origin changes silently — the exact failure this module
exists to close. If typed edges ever arrive they may vary *gating* (whether
`plan_next` blocks) but never *checking*.

SOURCES ARE THE SECOND EDGE FAMILY, and they check too: `sources:` cites the doc
SECTION an entry was drawn from, recording that section's hash at capture. A
changed or vanished section taints the entry (`source-changed` /
`source-missing`) with the doc + heading named in the chain. Deps are graph
edges, body-hash checked, and GATE `plan_next`; sources are attribution edges,
SECTION-hash checked, and never gate.

A source names a SECTION, never a whole document (the sole exception being a doc
with no headings, whose body is its one section). That is a data-model rule, not
a preference: whole-file hashing of a DESIGN.md would re-check every entry drawn
from it on any edit anywhere in it, and a flag that fires for unrelated reasons
is one readers learn to ignore. `section_hash` re-checks an entry only when *its*
section moved.

PROPOSED IS THE QUARANTINE TIER. An entry the session LLM extracted from a doc
(`design_import`) lands `proposed`: readable, searchable, and inert — it taints
nothing, so nothing downstream inherits authority it hasn't earned.
`design_promote` is the human act that turns it into active ground, and it is the
only way in (hand-authored decisions are already a human judgement, so
`design_add` still lands `active`).

The FACET IS THE INTERFACE: `design_read`/`design_edit`/`design_append`/
`design_lookup` are the way in — the note verbs refuse facet-store paths
outright. The shared store impl is backend; every facet verb is a chance to
speak the edges, which is what a raw file edit (still supported: the watcher
reindexes, hash-taint catches drift) cannot do.
"""

from __future__ import annotations

import contextlib
import datetime
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import notes
from .chunk import chunk_note, section_key
from .errors import CribUserError
from .notes import Note
from .util import sha1_hex

if TYPE_CHECKING:
    from .notestore import NoteStore
    from .paths import Paths

DESIGN_DIR = "design"
PLANS_DIR = "plans"
# kind → the pre-split subdir under notes/ — retained ONLY for legacy spellings:
# ref aliases (`design/x.md` still resolves) and the migration's source dirs.
_DIRS = {"design": DESIGN_DIR, "plan": PLANS_DIR}

# Qualified DOC references: a citation names a doc in another pillar store as
# `design:foo.md` / `plans:foo.md`; unqualified means the notes store (plain
# notes and in-situ `sources/<repo>/…` docs alike).
_DOCREF_STORES = ("design", "plans")


def split_docref(ref: str) -> tuple[str, str]:
    """`"design:foo.md"` → `("design", "foo.md")`; unqualified → `("notes", ref)`."""
    for name in _DOCREF_STORES:
        if ref.startswith(name + ":"):
            return name, ref[len(name) + 1 :]
    return "notes", ref


def format_docref(store: str, relpath: str) -> str:
    return relpath if store == "notes" else f"{store}:{relpath}"


# `proposed` is the IMPORT tier: extracted, not yet blessed (see the module
# docstring). It is a first-class status rather than a tag because it changes
# behaviour — a proposed entry taints nothing until `design_promote`.
DESIGN_STATUSES = ("proposed", "active", "superseded")
# `blocked` is DERIVED (any dep not done/verified), never stored — decision 6.
PLAN_STATUSES = ("todo", "in-progress", "done", "verified")
DONE_STATUSES = ("done", "verified")

_ALPHA = "abcdefghijklmnopqrstuvwxyz"
_N = len(_ALPHA)

# WHAT happened to a dep, per tainted entry — coarse on purpose, because taint
# means "a dep moved", not "this is wrong". The kind tells the reader how much
# re-reading the reaffirm actually needs. The last two are the SOURCE family: the
# cited section moved, or the heading it named is gone.
SOURCE_KINDS = ("source-changed", "source-missing")
CHANGE_KINDS = (
    "dep-edited",
    "dep-superseded",
    "dep-deleted",
    "new-unverified-edge",
    *SOURCE_KINDS,
)

# How `plan_list` presents the plan: the working set first, the graph second.
# The order of this tuple IS the rendering order.
_GROUPS = ("in-progress", "ready", "blocked", "done")

# The EXTRACTION PROCEDURES the import verbs return as their `instruction`. They
# are the payload, not documentation of it: `design_import` runs no model, so the
# only thing that turns a doc into graph entries is the session LLM following
# this — and a procedure that arrives WITH the sections and hashes it refers to
# is one the reader can execute without holding anything in its head.
_DESIGN_IMPORT = """\
Extract the DECISIONS this doc settles into the design graph. You do the reading
and the judgement — this verb ran no model.

1. READ {relpath} in full (`note_read {relpath}`, or open the `path` above). A
   decision is a choice with consequences: what was chosen, why, and what was
   rejected. Prose that only describes how something works is not one.
2. DEDUPE FIRST. `existing` lists what already cites this doc; `design_lookup`
   each candidate before adding it. If the graph already holds it, `design_append`
   the new detail onto that entry (or `design_dep_add` an edge) — a near-duplicate
   decision forks the graph and splits its dependents between two records.
3. ADD THEM IN ONE PASS. Each `design_add` carries:
   - `deps`: the decisions it builds on, including ones you added a moment ago in
     this same pass (they resolve by title);
   - `sources`: the `source` string of the EXACT section(s) it was drawn from,
     COPIED VERBATIM from `sections` below (several if it draws on several), so
     the entry re-checks when that section moves. Never cite the doc as a whole —
     that is refused, and it would re-check this entry on any edit anywhere in the
     file;
   - `proposed=True`: extraction lands in the quarantine tier, because an
     LLM-extracted decision must not carry the authority of a hand-recorded one.
   Each add answers with `similar` — a hit there means step 2 missed one.
4. REPORT the subgraph with `design_tree`, and list what you added. Promotion is
   the human act: `design_promote <ref>` per entry that is confirmed. Until then a
   proposed entry taints nothing and gates any plan item that depends on it.
"""

_PLAN_IMPORT = """\
Turn this doc's ACTIONABLE work into plan items. You do the reading and the
judgement — this verb ran no model.

1. READ {relpath} in full (`note_read {relpath}`, or open the `path` above) and
   pull out what someone would actually DO — each item one discrete piece of work,
   in the order the doc implies.
2. DEDUPE FIRST. `existing` lists items already citing this doc; `plan_lookup`
   each candidate. Work already tracked takes `plan_dep_add`/`plan_status`, not a
   second item for the same thing.
3. ADD THEM AS ONE BATCH — `plan_add(items=[...])`, in doc order. Each item
   carries:
   - `deps`: must-precede constraints, naming EARLIER items of this same batch by
     position ("#1") or any existing ref, plus the design decisions the work rests
     on;
   - `sources`: the `source` string of the EXACT section it came from, COPIED
     VERBATIM from `sections` below (never the doc as a whole — that is refused),
     so a finished item flags `revisit` if the passage it was done against later
     changes.
   A title is a legitimate whole item; write a body only where the detail matters.
4. REPORT the result with `plan_list` (and `plan_next` if you are picking work up
   now).
"""


def _group(node: Node, blocked: bool) -> str:
    """Which working-set band an item belongs to (see `_GROUPS`)."""
    if node.status in DONE_STATUSES:
        return "done"
    if node.status == "in-progress":
        return "in-progress"  # claimed: shown first even when blocked
    return "blocked" if blocked else "ready"


def _today() -> str:
    return datetime.date.today().isoformat()


def _body_hash(body: str) -> str:
    """Identity of a node's PROSE — frontmatter excluded on purpose: `updated`,
    a status flip or a rank change must not taint dependents, only the decision
    text itself."""
    return sha1_hex(body.strip())


def _suffix_match(key: str, suffix: str) -> bool:
    """Does `suffix` name the TAIL of a heading breadcrumb, segment-wise and
    case-insensitively? (`"10.3 Fusion"` and `"10. Stack/10.3 Fusion"` both name
    the section `10. Stack/10.3 Fusion`.) Suffix rather than substring so a
    citation stays short without becoming ambiguous by accident."""
    want = [p.strip().lower() for p in suffix.split("/") if p.strip()]
    have = [p.strip().lower() for p in key.split("/") if p.strip()]
    return bool(want) and len(want) <= len(have) and have[-len(want) :] == want


def _rank_between(a: str | None = None, b: str | None = None) -> str:
    """A lexorank strictly between `a` and `b` (either may be None = unbounded),
    over lowercase a–z — so inserting between two items never renumbers a
    neighbour (decision 5).

    Midpoint of the two strings, descending a character at a time when the
    neighbours are adjacent (`m`/`n` → `mm`). A returned rank NEVER ends in `a`:
    nothing can ever sort between `X` and `Xa`, so emitting one would poison the
    gap below it — when the midpoint would be `a` we take it as a prefix and keep
    descending. Hand-written ranks can still paint the caller into that corner,
    which is a ValueError rather than a silently out-of-order result.
    """
    lo, hi = (a or "").lower(), (b or "").lower()
    if lo and hi and lo >= hi:
        raise ValueError(f"ranks out of order: {a!r} must sort before {b!r}")
    out: list[str] = []
    i = 0
    while True:
        low = _ALPHA.index(lo[i]) if i < len(lo) else -1
        high = _ALPHA.index(hi[i]) if i < len(hi) else _N
        mid = (low + high) // 2
        if high - low > 1 and mid > 0:
            return "".join(out) + _ALPHA[mid]
        c = _ALPHA[low] if low >= 0 else _ALPHA[0]
        out.append(c)
        if i < len(hi):
            if c < hi[i]:
                hi = ""  # already strictly below: hi stops binding
            elif i + 1 == len(hi):  # matched hi exactly; anything deeper is >
                raise CribUserError(
                    f"no rank fits between {a!r} and {b!r} — they are adjacent "
                    f"(a rank ending in 'a' leaves no gap below it)"
                )
        i += 1


def _parse_sources(raw: Any) -> list[dict[str, Any]]:
    """`sources:` frontmatter → `[{ref, heading, hash}]`, tolerantly.

    A hand-written entry may be the bare citation string (`docs/DESIGN.md#4.
    Coordination`) rather than the mapping the verbs write — accepted, because a
    source is exactly the kind of thing a human adds to a note by hand. READING is
    deliberately more permissive than writing: `_resolve_source` refuses a
    citation that names no section, but a file already on disk is parsed as it
    stands rather than dropped."""
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            ref, _, heading = item.partition("#")
            item = {"ref": ref, "heading": heading}
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        heading = str(item.get("heading") or "").strip()
        out.append(
            {
                "ref": ref,
                "heading": heading or None,
                "hash": str(item.get("hash") or ""),
            }
        )
    return out


def _source_rows(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The frontmatter form of `sources` — the recorded fields only, so the
    live `current` hash resolved on load never leaks back into the file."""
    return [
        {
            k: v
            for k, v in (
                ("ref", s["ref"]),
                ("heading", s.get("heading")),
                ("hash", s.get("hash") or ""),
            )
            if v
        }
        for s in sources
    ]


def source_label(src: dict[str, Any]) -> str:
    """How a cited source reads in a chain, a cause, or an error: `doc#heading`."""
    return src["ref"] + (f"#{src['heading']}" if src.get("heading") else "")


def _source_cause(src: dict[str, Any]) -> dict[str, Any] | None:
    """The taint cause a citation carries right now, or None while it still reads
    as it did. Same shape as a dep cause (`dep`/`dep_title`/`change_kind`/
    `reason`) so every consumer — check rows, chains, the CLI — handles both
    families without a second code path; `source`/`heading` are the extra keys
    that say which doc moved.

    A citation recorded WITHOUT a hash (hand-written) is left alone: crib cannot
    claim a section changed when it never saw the section it was drawn from."""
    label = source_label(src)
    current, recorded = src.get("current"), src.get("hash") or ""
    if current is None:
        kind = "source-missing"
        reason = (
            f"the cited source {label} is gone — the heading was renamed or "
            f"removed, or the doc no longer resolves"
        )
    elif recorded and recorded != current:
        kind = "source-changed"
        reason = f"{label} changed since this was drawn from it"
    else:
        return None
    return {
        "dep": None,
        "dep_title": label,
        "dep_updated": None,
        "source": src["ref"],
        "heading": src.get("heading"),
        "change_kind": kind,
        "reason": reason,
    }


@dataclass
class Node:
    """One design/plan note, loaded from its frontmatter + body."""

    id: str
    kind: str  # "design" | "plan"
    relpath: str
    title: str
    status: str
    deps: list[str]
    checked: dict[str, str]
    rank: str
    body_hash: str
    frontmatter: dict[str, Any]
    # the note's `updated` stamp — reported per tainted entry so "a dep changed"
    # comes with WHEN, which is most of deciding how hard to re-read it
    updated: str = ""
    # attribution edges: `[{ref, heading, hash, current}]`. `hash` is the cited
    # section's hash AT CAPTURE, `current` what it hashes to now (None = the
    # section is gone) — resolved by `_load_graph`, so `_taint` stays pure.
    sources: list[dict[str, Any]] = field(default_factory=list)

    def brief(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "kind": self.kind,
            "relpath": self.relpath,
            "title": self.title,
            "status": self.status,
            "deps": list(self.deps),
            "updated": self.updated,
        }
        if self.sources:
            out["sources"] = [source_label(s) for s in self.sources]
        if self.kind == "plan":
            out["rank"] = self.rank
        return out


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    dependents: dict[str, list[str]] = field(default_factory=dict)

    def of_kind(self, kind: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]


def _cycles(nodes: dict[str, Node]) -> list[list[str]]:
    """Every dependency cycle, as id lists (empty when the graph is a DAG).

    Reported rather than raised on load: a cycle can arrive from a git merge or a
    hand edit, and refusing to *read* the graph would leave no way to see it. The
    write verbs (`*_dep_add`) validate before writing, so crib never creates one."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(nodes, WHITE)
    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(nid: str, stack: list[str]) -> None:
        colour[nid] = GREY
        stack.append(nid)
        for dep in nodes[nid].deps:
            if dep not in nodes:
                continue  # dangling: a warning, not a cycle
            if colour[dep] == GREY:
                cyc = stack[stack.index(dep) :] + [dep]
                # normalize the rotation so one cycle isn't reported once per entry
                key = tuple(sorted(cyc[:-1]))
                if key not in seen:
                    seen.add(key)
                    found.append(cyc)
            elif colour[dep] == WHITE:
                visit(dep, stack)
        stack.pop()
        colour[nid] = BLACK

    for nid in sorted(nodes):
        if colour[nid] == WHITE:
            visit(nid, [])
    return found


class Designs:
    def __init__(
        self,
        paths: Paths,
        design_store: NoteStore,
        plan_store: NoteStore,
        notestore: NoteStore,
    ) -> None:
        self.paths = paths
        # The two pillar stores this facet OWNS, by kind — plus the notes store,
        # read-only here, for citation resolution (a decision cites plain notes
        # and in-situ docs far more often than other decisions).
        self._stores = {"design": design_store, "plan": plan_store}
        self.notestore = notestore
        # docref store name → NoteStore (note the kind/store spelling difference:
        # kind "plan" lives in the store named "plans")
        self._doc_stores = {
            "notes": notestore,
            "design": design_store,
            "plans": plan_store,
        }

    def _store(self, kind: str) -> NoteStore:
        return self._stores[kind]

    # ── loading ───────────────────────────────────────────────────────────────

    def _load_graph(self, proj: str) -> Graph:
        """Scan the `design/` + `plans/` pillar stores into a graph.
        Frontmatter-only parsing (no chunking, no embedding) — cheap enough to
        run on every verb, which is what lets taint be computed live instead of
        stored.

        The one exception is `sources`: each cited section is hashed as it reads
        NOW, so `_taint` can compare against the recorded hash without doing I/O
        itself. `docs` memoizes per load, so ten decisions citing one DESIGN.md
        split it once."""
        graph = Graph()
        docs: dict[str, dict[str, str]] = {}
        for kind, store in self._stores.items():
            try:
                d = store.root(proj)
            except Exception:  # noqa: BLE001 — an unavailable store blinds no read
                continue
            if not d.exists():
                continue
            for path in sorted(d.glob("*.md")):
                try:
                    fm, body = notes.parse(path.read_text(), path)
                except (OSError, UnicodeDecodeError, notes.NoteParseError):
                    continue  # one broken note must not blind the graph
                nid = str(fm.get("id") or "")
                if not nid:
                    continue  # not indexed yet (no id stamped)
                deps = [str(x) for x in (fm.get("deps") or [])]
                checked = {str(k): str(v) for k, v in (fm.get("checked") or {}).items()}
                sources = _parse_sources(fm.get("sources"))
                for src in sources:
                    src["current"] = self._section_hash(proj, src, docs)
                graph.nodes[nid] = Node(
                    id=nid,
                    kind=kind,
                    relpath=path.name,
                    title=str(fm.get("title") or path.stem),
                    status=str(
                        fm.get("status") or ("active" if kind == "design" else "todo")
                    ),
                    deps=deps,
                    checked=checked,
                    rank=str(fm.get("rank") or ""),
                    body_hash=_body_hash(body),
                    frontmatter=fm,
                    updated=str(fm.get("updated") or ""),
                    sources=sources,
                )
        for node in graph.nodes.values():
            for dep in node.deps:
                graph.dependents.setdefault(dep, []).append(node.id)
        return graph

    def _note(self, proj: str, node: Node) -> Note:
        return notes.load(self._store(node.kind).abspath(proj, node.relpath))

    # ── sources: the doc a decision was drawn from ────────────────────────────
    # Attribution edges live at SECTION granularity, hashed with the very chunker
    # the index uses (`chunk_note` → `section_hash`), so the two agree by
    # construction rather than by a second implementation that can drift.

    def _doc_abspath(self, proj: str, ref: str):
        """A (possibly qualified) doc reference → its on-disk path, routed
        through the owning pillar store."""
        store, rel = split_docref(ref)
        return self._doc_stores[store].abspath(proj, rel)

    def _doc_exists(self, proj: str, relpath: str) -> bool:
        try:
            return self._doc_abspath(proj, relpath).exists()
        except (OSError, ValueError):
            return False  # escaping/unresolvable relpath

    def _known_docs(self, proj: str) -> set[str]:
        """Every doc reference crib can cite in this project: what the index
        holds (including docs indexed in situ) plus each pillar tree on disk.
        Non-notes docs read QUALIFIED (`design:foo.md`) — the docref spelling."""
        out: set[str] = set()
        # PLUGGABLE-BACKEND boundary: the vector store (Chroma/Json/…) sits behind
        # an interface, so we don't own the exception types `get_meta` can raise.
        # An unreachable store here must NARROW the doc set (a degraded citation
        # search), not fail the read — hence the deliberate broad catch, reported.
        try:
            for meta in self.notestore.store.get_meta({"project": proj}).values():
                rp = str(meta.get("relpath") or "")
                if rp:
                    out.add(format_docref(str(meta.get("store") or "notes"), rp))
        except Exception as e:  # noqa: BLE001 — backend boundary, see above
            print(f"[crib] known-docs: store meta unavailable: {e}", file=sys.stderr)
        for name, store in self._doc_stores.items():
            # a missing/unreadable pillar dir just contributes no docs
            with contextlib.suppress(OSError, ValueError):
                root = store.root(proj)
                out |= {
                    format_docref(name, p.relative_to(root).as_posix())
                    for p in root.rglob("*.md")
                }
        return out

    def _resolve_doc(self, proj: str, ref: str) -> str:
        """A user-supplied doc reference → the one relpath it names.

        Exact relpath first; then the repo-relative spelling of a doc indexed in
        situ (`DESIGN.md` → `sources/cribsheet/DESIGN.md`, via the source-root
        registry); then a unique path-suffix match over everything indexed. As
        with `_resolve_ref`, ambiguity lists the candidates rather than guessing."""
        ref = (ref or "").strip().lstrip("./")
        if not ref:
            raise CribUserError(
                "name the doc: a note relpath, a qualified facet ref "
                "(`design:foo.md`), or a repo-relative path to a doc indexed in "
                "situ (`DESIGN.md`, `docs/plans/foo.md`)"
            )
        if self._doc_exists(proj, ref):
            return ref
        # legacy alias: the pre-split spelling `design/x.md` names `design:x.md`
        for name in _DOCREF_STORES:
            if ref.startswith(name + "/"):
                alias = f"{name}:{ref[len(name) + 1 :]}"
                if self._doc_exists(proj, alias):
                    return alias

        def _tail_match(rp: str) -> bool:
            if rp == ref or rp.endswith("/" + ref):
                return True
            store, rel = split_docref(rp)
            if store == "notes":
                return False
            # a facet doc also answers to its store-relative tail, and to the
            # legacy `design/…` spelling of it
            want = ref[len(store) + 1 :] if ref.startswith(store + "/") else ref
            return rel == want or rel.endswith("/" + want)

        matches = {
            prefix + ref
            for prefix in self.notestore.source_roots(proj).all()
            if self._doc_exists(proj, prefix + ref)
        }
        matches |= {rp for rp in self._known_docs(proj) if _tail_match(rp)}
        if len(matches) == 1:
            return matches.pop()
        if not matches:
            raise CribUserError(
                f"no doc matches {ref!r} — pass a note relpath, or a path under a "
                f"repo whose docs are indexed in situ (those read "
                f"`sources/<repo>/<path>`)"
            )
        listing = ", ".join(sorted(matches)[:8])
        raise CribUserError(f"ambiguous doc {ref!r} — {len(matches)} match: {listing}")

    def _indexed_sections(self, proj: str, relpath: str) -> dict[str, str]:
        """`{section_key: section_hash}` as the INDEX records them.

        Preferred over re-hashing the file, because the graph checks against the
        indexed corpus: an edit lands as taint when the reindex lands, so a
        source and a retrieval hit never disagree about what the doc says."""
        store, rel = split_docref(relpath)
        try:
            metas = {
                cid: m
                for cid, m in self.notestore.store.get_meta(
                    {"project": proj, "relpath": rel}
                ).items()
                if (str(m.get("store") or "notes")) == store
            }
        except Exception:  # noqa: BLE001 — fall back to hashing the text ourselves
            return {}
        out: dict[str, str] = {}
        for meta in metas.values():
            sh = str(meta.get("section_hash") or "")
            if sh:
                key = section_key(
                    str(meta.get("heading_path") or ""),
                    int(meta.get("occurrence", 1) or 1),
                )
                out[key] = sh
        return out

    def _doc_sections(self, proj: str, relpath: str) -> list[dict[str, Any]]:
        """A cited doc split into sections, in document order: heading path,
        identity key, current `section_hash`, size and a one-line preview.

        The hash comes from the index when the doc is indexed; an UNINDEXED
        target is split with the same `chunk_note` the indexer runs, which
        produces the identical hash — so a doc can be cited before it is
        indexed and nothing re-hashes when it later is."""
        indexed = self._indexed_sections(proj, relpath)
        try:
            text = self._doc_abspath(proj, relpath).read_text()
        except (OSError, ValueError, UnicodeDecodeError):
            # unreadable (an in-situ doc whose repo isn't on this machine): the
            # index still knows the sections, just not their order
            return [
                {
                    "heading_path": k,
                    "key": k,
                    "section_hash": h,
                    "words": 0,
                    "preview": "",
                    "indexed": True,
                }
                for k, h in sorted(indexed.items())
            ]
        _, body = notes.parse(text, relpath)
        rows: list[dict[str, Any]] = []
        for c in chunk_note(proj, relpath, "", body):
            if c.window_idx:
                continue  # one row per SECTION, not per window
            key = section_key(c.heading_path, c.occurrence)
            words = c.section_text.split()
            rows.append(
                {
                    "heading_path": "/".join(c.heading_path),
                    "key": key,
                    "section_hash": indexed.get(key) or c.section_hash,
                    # `preview` is the section's first words VERBATIM and
                    # `words` its length — locators, not a summary; nothing
                    # here reads the content for meaning
                    "words": len(words),
                    "preview": " ".join(words[:24]),
                    "indexed": key in indexed,
                }
            )
        return rows

    def _doc_index(
        self, proj: str, relpath: str, cache: dict[str, dict[str, str]]
    ) -> dict[str, str]:
        """`{section_key: hash}` for a doc. The `""` key is the section BEFORE the
        first heading — which, in a doc with no headings at all, is the whole
        body: the only case a citation may name a doc rather than a section."""
        if relpath not in cache:
            cache[relpath] = {
                r["key"]: r["section_hash"] for r in self._doc_sections(proj, relpath)
            }
        return cache[relpath]

    def _section_hash(
        self, proj: str, src: dict[str, Any], cache: dict[str, dict[str, str]]
    ) -> str | None:
        """What the cited section hashes to RIGHT NOW — None when it is gone
        (heading renamed or removed, or the doc itself no longer resolves)."""
        try:
            idx = self._doc_index(proj, src["ref"], cache)
        except Exception:  # noqa: BLE001 — a broken citation is taint, never a crash
            return None
        heading = src.get("heading") or ""
        if not heading:
            return idx.get("")
        if heading in idx:
            return idx[heading]
        # a hand-written `heading` may be a suffix of the real breadcrumb
        hits = [k for k in idx if k and _suffix_match(k, heading)]
        return idx[hits[0]] if len(hits) == 1 else None

    def _match_heading(
        self, relpath: str, rows: list[dict[str, Any]], suffix: str
    ) -> dict[str, Any]:
        """The one section whose heading path ends with `suffix` — the resolution
        behind `--source "docs/DESIGN.md#10.3 Retrieval"`. Segment-wise first,
        then a substring match on the last segment; ambiguity at either tier
        lists the candidates rather than picking one."""
        for hits in (
            [r for r in rows if _suffix_match(r["key"], suffix)],
            [
                r
                for r in rows
                if suffix.strip().lower()
                in r["heading_path"].rsplit("/", 1)[-1].lower()
            ],
        ):
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                listing = ", ".join(h["key"] for h in hits[:8])
                raise CribUserError(
                    f"ambiguous source heading {suffix!r} in {relpath} — "
                    f"{len(hits)} sections match: {listing}. Cite more of the "
                    f'heading path (`--source "{relpath}#<one of those>"`)'
                )
        listing = ", ".join(r["key"] for r in rows[:12]) or "(none — no headings)"
        raise CribUserError(
            f"no section of {relpath} matches {suffix!r} — its headings are: {listing}"
        )

    def _resolve_source(self, proj: str, spec: Any) -> dict[str, Any]:
        """One `--source` spec → a recorded citation `{ref, heading, hash}`.

        Takes `"<doc>#<heading suffix>"` (the CLI/agent spelling) or the mapping
        form; the heading is resolved to the FULL breadcrumb so the citation
        stays legible — and re-checkable — after the doc is reorganised.

        A SOURCE CITES A SECTION, not a document. A bare doc reference against a
        doc that has headings is an error listing them, because whole-file
        attribution is the thing section granularity exists to prevent: it would
        re-check every entry drawn from a DESIGN.md on any edit anywhere in it,
        which is noise that trains a reader to ignore the flag. The one exception
        is a doc with no headings at all — there the whole body IS the section the
        chunker hashes, so citing the doc and citing its section are the same
        act."""
        if isinstance(spec, dict):
            doc, suffix = str(spec.get("ref") or ""), str(spec.get("heading") or "")
        else:
            doc, _, suffix = str(spec or "").partition("#")
        relpath = self._resolve_doc(proj, doc)
        rows = self._doc_sections(proj, relpath)
        if suffix.strip():
            row = self._match_heading(relpath, rows, suffix)
            return {
                "ref": relpath,
                "heading": row["heading_path"],
                "hash": row["section_hash"],
            }
        headed = [r for r in rows if r["key"]]
        if headed:
            listing = ", ".join(r["key"] for r in headed[:12])
            raise CribUserError(
                f"{relpath} has headings, so cite the SECTION this was drawn from "
                f"rather than the whole doc — `{relpath}#<heading>`. Its sections "
                f"are: {listing}"
            )
        if not rows:
            raise CribUserError(f"{relpath} has no content to cite")
        return {"ref": relpath, "heading": None, "hash": rows[0]["section_hash"]}

    def _capture_sources(
        self, proj: str, specs: list[Any] | None
    ) -> list[dict[str, Any]]:
        """Resolve every `--source` spec on a write, de-duplicated, in order."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for spec in specs or []:
            src = self._resolve_source(proj, spec)
            label = source_label(src)
            if label not in seen:
                seen.add(label)
                out.append(src)
        return out

    def _recapture(
        self, proj: str, node: Node
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Re-record a node's source hashes (reaffirm/promote): each citation
        re-hashed as the doc reads now. A citation whose section is GONE keeps
        its old hash and is reported — re-blessing must not paper over a source
        that no longer exists."""
        cache: dict[str, dict[str, str]] = {}
        rows, missing = [], []
        for src in node.sources:
            current = self._section_hash(proj, src, cache)
            if current is None:
                missing.append(source_label(src))
            rows.append(
                {
                    "ref": src["ref"],
                    "heading": src.get("heading"),
                    "hash": current or src.get("hash") or "",
                }
            )
        return rows, missing

    def _source_view(self, src: dict[str, Any]) -> dict[str, Any]:
        """One citation, rendered for a dossier: where it points and whether it
        still reads the way it did when this entry was drawn from it."""
        current, recorded = src.get("current"), src.get("hash") or ""
        state = (
            "missing"
            if current is None
            else "unhashed"
            if not recorded
            else "ok"
            if current == recorded
            else "changed"
        )
        return {
            "ref": src["ref"],
            "heading": src.get("heading"),
            "label": source_label(src),
            "state": state,
        }

    # ── refs ──────────────────────────────────────────────────────────────────

    def _resolve_ref(
        self, graph: Graph, ref: str, kind: str | None = None, _cross: bool = False
    ) -> Node:
        """Resolve a user-supplied reference to exactly one node: a full ULID, a
        unique ULID prefix, a relpath (`design/x.md`, `x.md` or bare `x`), or the
        title / its slug. Ambiguity lists the candidates rather than guessing —
        a wrong node is worse than a second call."""
        from .app import _slug

        ref = (ref or "").strip()
        if not ref:
            raise CribUserError("empty ref — pass an id, relpath, or title")
        pool = [n for n in graph.nodes.values() if kind is None or n.kind == kind]

        def _other_facet_hint() -> None:
            """The miss that reads as store-corruption when it is actually the
            wrong FACET: the ref exists, just across the aisle. Say so — a session
            was lost to concluding crib was broken over exactly this."""
            if kind is None or _cross:
                return
            other = "plan" if kind == "design" else "design"
            try:
                hit = self._resolve_ref(graph, ref, other, _cross=True)
            except CribUserError:
                return
            raise CribUserError(
                f"{ref!r} is not a {kind} note — it exists as a "
                f"{other.upper()} item ({hit.relpath}): use the {other}_* verbs"
            )

        if not pool:
            _other_facet_hint()
            raise CribUserError(
                f"no {kind or 'design/plan'} notes yet — "
                f"`{kind or 'design'}_add` creates the first one"
            )
        want, slug = ref.lower(), _slug(ref)
        exact = [n for n in pool if n.id == ref.upper()]
        if len(exact) == 1:
            return exact[0]

        def _rel_match(n: Node) -> bool:
            # store-relative spelling, plus the legacy pre-split `design/x.md`
            w = want
            legacy = f"{_DIRS[n.kind]}/"
            if w.startswith(legacy):
                w = w[len(legacy) :]
            return n.relpath.lower() in (w, f"{w}.md")

        matches = [
            n
            for n in pool
            if _rel_match(n) or n.title.lower() == want or _slug(n.title) == slug
        ]
        if not matches:
            matches = [n for n in pool if n.id.startswith(ref.upper())]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            _other_facet_hint()
            raise CribUserError(
                f"no {kind or 'design/plan'} note matches {ref!r} — "
                f"reference it by id, relpath or title "
                f"(`{kind or 'design'}_check` / `plan_list` show what exists)"
            )
        listing = ", ".join(f"{n.id[:8]}… {n.relpath}" for n in matches[:8])
        raise CribUserError(
            f"ambiguous ref {ref!r} — {len(matches)} matches: {listing}"
        )

    # ── taint ─────────────────────────────────────────────────────────────────

    def _direct_taint(
        self, graph: Graph, sources: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        """Per node, why its OWN edges are out of date (decision 3), as structured
        causes: `{dep, dep_title, dep_updated, change_kind, reason}`.

        Six kinds, and no others — a dep that is gone (`dep-deleted`), one
        declared but never reaffirmed here (`new-unverified-edge`), one whose body
        moved (`dep-edited`), and the special case of that where the dep is now
        marked superseded (`dep-superseded`); then the SOURCE family, where the
        cited section changed (`source-changed`) or is gone (`source-missing`).
        The kind and the dep's `updated` date travel with the entry so the reader
        can size the re-read before opening anything.

        `sources=False` computes the DEP-ONLY view. That is what `plan_next`'s
        gating reads, because attribution edges check but never gate: a decision
        whose source doc was reworded must still be re-read, but it must not stop
        work from being picked up.

        A dep that is `proposed` yields NO cause at all: an extracted entry is
        quarantined until promoted, and quarantine that taints the graph around it
        is not quarantine."""
        out: dict[str, list[dict[str, Any]]] = {}
        for node in graph.nodes.values():
            if sources:
                for src in node.sources:
                    cause = _source_cause(src)
                    if cause:
                        out.setdefault(node.id, []).append(cause)
            for dep_id in node.deps:
                dep = graph.nodes.get(dep_id)
                if dep is not None and dep.status == "proposed":
                    continue
                if dep is None:
                    cause = {
                        "dep": dep_id,
                        "dep_title": dep_id,
                        "dep_updated": None,
                        "change_kind": "dep-deleted",
                        "reason": f"dep {dep_id} is missing "
                        f"(deleted, or never existed)",
                    }
                elif dep_id not in node.checked:
                    cause = {
                        "dep": dep_id,
                        "dep_title": dep.title,
                        "dep_updated": dep.updated or None,
                        "change_kind": "new-unverified-edge",
                        "reason": f"{dep.title!r} was added as a dep but never "
                        f"verified here",
                    }
                elif node.checked[dep_id] != dep.body_hash:
                    superseded = dep.status == "superseded"
                    when = f" (dep updated {dep.updated})" if dep.updated else ""
                    cause = {
                        "dep": dep_id,
                        "dep_title": dep.title,
                        "dep_updated": dep.updated or None,
                        "change_kind": "dep-superseded" if superseded else "dep-edited",
                        "reason": f"{dep.title!r} "
                        f"{'was superseded' if superseded else 'changed'} "
                        f"since this was last verified{when}",
                    }
                else:
                    continue
                out.setdefault(node.id, []).append(cause)
        return out

    def _taint(self, graph: Graph, sources: bool = True) -> dict[str, dict[str, Any]]:
        """{id: {reasons, causes, paths}} for every tainted node — direct causes
        plus transitive reachability over tainted edges. `paths` spells out the
        chain (`X → Y → Z`, Z being what actually changed) so `check` can say
        *why*, and each path carries the change kind + the dep's `updated` date of
        the cause at its far end. A SOURCE cause ends its chain with the citation
        itself (`X → docs/DESIGN.md#4. Coordination`), so the doc and heading that
        moved are named in the explanation, not just in the cause.

        EVERY edge is walked here; there is no edge kind that informs without
        checking (see the module docstring). `sources=False` is not an exception
        to that — it is the GATING view (`plan_next`), which asks the narrower
        question of whether work is safe to pick up.

        Taint does not flow OUT of a `proposed` node: quarantine that taints its
        dependents would spread the very authority it exists to withhold."""
        direct = self._direct_taint(graph, sources)
        tainted: set[str] = set(direct)

        def propagates(dep_id: str) -> bool:
            dep = graph.nodes.get(dep_id)
            return dep_id in tainted and (dep is None or dep.status != "proposed")

        changed = True
        while changed:  # reachability over tainted edges
            changed = False
            for node in graph.nodes.values():
                if node.id in tainted:
                    continue
                if any(propagates(d) for d in node.deps):
                    tainted.add(node.id)
                    changed = True

        def chains(nid: str, stack: list[str]) -> list[dict[str, Any]]:
            node = graph.nodes[nid]
            base = [graph.nodes[s].title for s in stack] + [node.title]
            out = [
                {
                    "chain": base + ([c["dep_title"]] if c.get("source") else []),
                    "cause": c["reason"],
                    "change_kind": c["change_kind"],
                    "dep": c["dep"],
                    "dep_title": c["dep_title"],
                    "dep_updated": c["dep_updated"],
                }
                for c in direct.get(nid, [])
            ]
            for dep_id in node.deps:
                if (
                    propagates(dep_id)
                    and dep_id in graph.nodes
                    and dep_id not in stack
                    and dep_id != nid
                ):
                    out += chains(dep_id, [*stack, nid])
            return out

        result: dict[str, dict[str, Any]] = {}
        for nid in tainted:
            paths = chains(nid, [])
            causes = direct.get(nid, [])
            reasons = [c["reason"] for c in causes] or sorted(
                {
                    f"depends on {p['chain'][1]!r}, which is tainted"
                    for p in paths
                    if len(p["chain"]) > 1
                }
            )
            result[nid] = {"reasons": reasons, "causes": causes, "paths": paths}
        return result

    def _annotate(
        self, graph: Graph, tainted: dict[str, Any], dep_id: str
    ) -> dict[str, Any]:
        """One edge target, rendered for a dossier: enough to decide whether to
        open it without opening it. A dangling id says so rather than vanishing."""
        node = graph.nodes.get(dep_id)
        if node is None:
            return {
                "id": dep_id,
                "title": f"{dep_id} (missing)",
                "missing": True,
                "tainted": True,
            }
        return {
            "id": node.id,
            "title": node.title,
            "relpath": node.relpath,
            "status": node.status,
            "updated": node.updated,
            "tainted": node.id in tainted,
        }

    def _next(self, node: Node, causes: list[dict[str, Any]] | None = None) -> str:
        """The prescribed follow-up for a tainted decision — the one string every
        taint-bearing result ends with, so a reader is never left holding a flag
        with no verb attached. Taint is COARSE ("a dep moved"), so reaffirm is the
        normal, cheap outcome; supersede is the exception.

        A taint caused ONLY by sources gets its own line: re-read the cited
        section, not the whole decision graph — and it says so, because a reader
        told to "reconsider" when a doc's wording moved will do more work than the
        change deserves."""
        srcs = [c for c in causes or [] if c["change_kind"] in SOURCE_KINDS]
        if causes and len(srcs) == len(causes):
            cited = ", ".join(dict.fromkeys(c["dep_title"] for c in srcs))
            return (
                f"re-read {cited} — the source {node.title!r} was drawn from — "
                f"then `design_reaffirm {node.relpath}` to re-record the section "
                f"hash (a source is attribution: it checks, it never gates)"
            )
        return (
            f"reconsider {node.title!r} against what changed, then "
            f"`design_reaffirm {node.relpath}` (the usual case — taint means a "
            f"dep moved, not that this is wrong); if it no longer holds, "
            f"`design_supersede {node.relpath} <replacement>`"
        )

    def tainted_designs(self, proj: str) -> set[str]:
        """Relpaths of this project's tainted design notes — the cheap ambient
        probe behind `status`'s `design_tainted` count and the `tainted: true`
        marker on retrieval hits. Best-effort: an unreadable project reports
        nothing rather than failing the caller's real work."""
        try:
            graph = self._load_graph(proj)
            tainted = self._taint(graph)
        except Exception:  # noqa: BLE001 — an ambient marker must never break a read
            return set()
        return {
            n.relpath
            for n in graph.nodes.values()
            if n.kind == "design" and n.id in tainted
        }

    def annotate_hits(
        self, proj: str, hits: list[dict[str, Any]], kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Stamp facet state onto retrieval hits that land on a design/plan note:
        `status`, `tainted`, and dep/dependent counts. The agent reasoning FROM a
        stale decision is told so at the moment it reads it, which is the only
        moment the warning can change what it does.

        Relpaths are store-relative, so a design and a plan may share one name —
        `kind` (what the facet lookup queried) disambiguates the join."""
        try:
            graph = self._load_graph(proj)
            tainted = self._taint(graph)
        except Exception:  # noqa: BLE001 — as `tainted_designs`: never break a read
            return hits
        by_relpath = {(n.kind, n.relpath): n for n in graph.nodes.values()}
        kinds = (kind,) if kind else ("design", "plan")
        for hit in hits:
            rp = str(hit.get("relpath") or "")
            node = next(
                (by_relpath[k] for k in ((kd, rp) for kd in kinds) if k in by_relpath),
                None,
            )
            if node is None:
                continue
            hit.update(
                kind=node.kind,
                status=node.status,
                tainted=node.id in tainted,
                deps=len(node.deps),
                dependents=len(graph.dependents.get(node.id, [])),
            )
        return hits

    # ── writes ────────────────────────────────────────────────────────────────

    def _unique_relpath(self, proj: str, kind: str, slug: str) -> str:
        """`<slug>.md` in the kind's pillar store, numeric suffix only on
        collision (DESIGN §15.1)."""
        base = self._store(kind).root(proj)
        if not (base / f"{slug}.md").exists():
            return f"{slug}.md"
        i = 2
        while (base / f"{slug}-{i}.md").exists():
            i += 1
        return f"{slug}-{i}.md"

    async def _save(
        self, proj: str, node: Node, fm: dict[str, Any], body: str | None = None
    ) -> dict[str, Any]:
        """Rewrite a node's frontmatter (body untouched unless given), stamp
        `updated`, and funnel through the locked index_file write path."""
        store = self._store(node.kind)
        path = store.abspath(proj, node.relpath)
        note = notes.load(path)
        fm = {**note.frontmatter, **fm, "updated": _today()}
        note.frontmatter = fm
        if body is not None:
            note.body = body
        res = await store.write(proj, node.relpath, note)
        return {
            "project": proj,
            "id": node.id,
            "relpath": node.relpath,
            "title": node.title,
            "indexed": res.upserted,
        }

    async def _add(
        self,
        proj: str,
        kind: str,
        title: str,
        content: str,
        deps: list[str] | None,
        extra: dict[str, Any],
        sources: list[Any] | None = None,
    ) -> dict[str, Any]:
        from .app import _slug

        if not (title or "").strip():
            raise CribUserError("a design/plan note needs a title")
        if kind == "design" and not (content or "").strip():
            raise CribUserError(
                "a design decision needs a body — the choice, why, and what was "
                "rejected. (A plan item may be title-only; a decision may not: "
                "the rationale is the thing a future reader comes back for.)"
            )
        graph = self._load_graph(proj)
        dep_nodes = [self._resolve_ref(graph, r) for r in (deps or [])]
        relpath = self._unique_relpath(proj, kind, _slug(title))
        # A new note is born VERIFIED, decision OR plan item: it was written
        # against the deps as they read right now, so seeding `checked` says
        # exactly that (a fresh note showing up already tainted would be noise, not
        # signal). Seeding plans too is what lets an EDIT's taint diff mean
        # something — otherwise every plan dependent is born tainted and
        # `plan_edit`'s `newly_tainted` could never fire. A dep added LATER
        # (`_dep_add`) still starts unverified on purpose; this is the create path.
        checked = {"checked": {n.id: n.body_hash for n in dep_nodes}}
        # Same story for `sources`: the citation records the section hash AS
        # CAPTURED, so the entry is born current with the doc it was drawn from
        # and starts checking against it from the next edit onward.
        cited = self._capture_sources(proj, sources)
        fm: dict[str, Any] = {
            "title": title.strip(),
            "type": kind,
            "status": extra.pop("status", "active" if kind == "design" else "todo"),
            "deps": [n.id for n in dep_nodes],
            "links": [],
            **checked,
            **({"sources": _source_rows(cited)} if cited else {}),
            **extra,
            "created": _today(),
            "updated": _today(),
        }
        store = self._store(kind)
        note = Note(
            path=store.abspath(proj, relpath),
            frontmatter=fm,
            body=(content or "").strip() + "\n",
        )
        res = await store.write(proj, relpath, note)
        return {
            "project": proj,
            "id": note.id,
            "relpath": relpath,
            "title": fm["title"],
            "deps": fm["deps"],
            "status": fm["status"],
            "sources": [source_label(s) for s in cited],
            "indexed": res.upserted,
        }

    async def _dep_add(
        self, proj: str, kind: str, ref: str, dep_ref: str
    ) -> dict[str, Any]:
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        dep = self._resolve_ref(graph, dep_ref)
        if dep.id == node.id:
            raise CribUserError(f"{node.title!r} cannot depend on itself")
        if dep.id in node.deps:
            return {
                "project": proj,
                "id": node.id,
                "relpath": node.relpath,
                "title": node.title,
                "dep": dep.id,
                "already": True,
                "deps": list(node.deps),
            }
        probe = {
            nid: Node(**{**vars(n), "deps": list(n.deps)})
            for nid, n in graph.nodes.items()
        }
        probe[node.id].deps.append(dep.id)
        for cyc in _cycles(probe):
            if node.id in cyc:
                path = " → ".join(probe[c].title for c in cyc)
                raise CribUserError(
                    f"that dep would create a cycle: {path}. Dependencies must be "
                    f"a DAG — drop the opposite edge first"
                )
        deps = [*node.deps, dep.id]
        # Deliberately does NOT seed `checked`: a newly declared dep starts
        # UNVERIFIED, so the node shows up in `check` — the nudge to actually
        # reconsider it against what it now depends on.
        out = await self._save(proj, node, {"deps": deps})
        return {**out, "dep": dep.id, "dep_title": dep.title, "deps": deps}

    async def _dep_remove(
        self, proj: str, kind: str, ref: str, dep_ref: str
    ) -> dict[str, Any]:
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        try:
            dep_id = self._resolve_ref(graph, dep_ref).id
        except ValueError:
            dep_id = dep_ref.strip().upper()  # dangling dep: remove by raw id
        if dep_id not in node.deps:
            raise CribUserError(f"{node.title!r} does not depend on {dep_ref!r}")
        deps = [d for d in node.deps if d != dep_id]
        checked = {k: v for k, v in node.checked.items() if k != dep_id}
        fm: dict[str, Any] = {"deps": deps}
        if node.kind == "design":
            fm["checked"] = checked
        out = await self._save(proj, node, fm)
        return {**out, "dep": dep_id, "deps": deps}

    async def _forget(
        self, proj: str, kind: str, ref: str, force: bool
    ) -> dict[str, Any]:
        """Delete blocks on dependents (decision 4) — `force` deletes anyway and
        leaves them tainted (their `checked` now points at a missing id)."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        dependents = [graph.nodes[d].brief() for d in graph.dependents.get(node.id, [])]
        if dependents and not force:
            listing = ", ".join(f"{d['title']!r} ({d['relpath']})" for d in dependents)
            raise CribUserError(
                f"{node.title!r} still has {len(dependents)} dependent(s): {listing}. "
                f"Drop those edges ({kind}_dep_remove) or pass force=True — forcing "
                f"leaves them tainted, pointing at a missing dep"
            )
        res = await self._store(node.kind).delete(proj, node.relpath)
        return {
            **res,
            "id": node.id,
            "title": node.title,
            "dependents": dependents,
            "forced": bool(dependents),
        }

    # ── design verbs ──────────────────────────────────────────────────────────

    async def design_add(
        self,
        proj: str,
        title: str,
        content: str,
        deps: list[str] | None = None,
        sources: list[Any] | None = None,
        proposed: bool = False,
    ) -> dict[str, Any]:
        """Record a design decision under `notes/design/`, `checked` seeded from
        the current dep hashes (so a new decision is born verified) and `sources`
        from the cited sections' hashes.

        `proposed=True` is the IMPORT tier and belongs to `design_import`'s
        procedure: hand-authoring is already a human judgement, so it lands
        `active`; only extraction quarantines."""
        extra = {"status": "proposed"} if proposed else {}
        return await self._add(proj, "design", title, content, deps, extra, sources)

    def design_read(self, proj: str, ref: str) -> dict[str, Any]:
        """The one-call orientation on a decision: body + status + every edge
        annotated + this node's own taint state with the chains explaining it.

        The `code_dossier` analog for decisions, and the reason the facet — not
        `note_read` on a path under `design/` — is the way in: the file gives you
        prose, this gives you prose PLUS what it rests on, what rests on it, and
        whether either has moved under you."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "design")
        tainted = self._taint(graph)
        info = tainted.get(node.id, {})
        note = self._note(proj, node)
        out = {
            "project": proj,
            **node.brief(),
            "body": note.body.strip(),
            "deps": [self._annotate(graph, tainted, d) for d in node.deps],
            "dependents": [
                self._annotate(graph, tainted, d)
                for d in graph.dependents.get(node.id, [])
            ],
            # attribution, alongside the graph edges: where this came FROM, and
            # whether that passage still reads the way it did
            "sources": [self._source_view(s) for s in node.sources],
            "tainted": node.id in tainted,
            "reasons": info.get("reasons", []),
            "causes": info.get("causes", []),
            "paths": info.get("paths", []),
        }
        if node.id in tainted:
            out["next"] = self._next(node, info.get("causes"))
        if node.status == "proposed":
            out["next"] = (
                f"{node.title!r} is PROPOSED — extracted, not yet blessed, so it "
                f"taints nothing and nothing should be built on it as settled. "
                f"Confirm it against its sources, then `design_promote "
                f"{node.relpath}`"
            )
        return out

    async def _write_body(
        self,
        proj: str,
        ref: str,
        rewrite: Any,
        sources: list[Any] | None = None,
        precaptured: bool = False,
        kind: str = "design",
    ) -> dict[str, Any]:
        """The edge-aware write path shared by the edit/append verbs of BOTH
        facets (`kind` selects which): snapshot the taint state, write the new
        body through the locked index path, then diff — so the answer to "I
        changed this" is "…and here is what that just put out of date", computed
        against the PRE-edit state.

        Hash-taint remains the safety net for a raw file edit; this is the
        encouraged path because only it can name the consequences in the same
        breath as the change.

        `sources`, when given, REPLACES the recorded citations (re-captured at
        their current hashes) — the decision is being restated, so what it was
        drawn from is restated with it. Omitted, they are left untouched.
        `precaptured` marks rows already in stored form (the append verbs' additive
        merge, where existing citations must KEEP their capture-time hashes)."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        before = set(self._taint(graph))
        note = self._note(proj, node)
        fm: dict[str, Any] = {}
        if sources is not None:
            fm["sources"] = (
                sources
                if precaptured
                else _source_rows(self._capture_sources(proj, sources))
            )
        out = await self._save(proj, node, fm, body=rewrite(note.body))
        after_graph = self._load_graph(proj)
        after = self._taint(after_graph)
        newly = []
        for nid in sorted(set(after) - before):
            hit = after_graph.nodes[nid]
            newly.append(
                {
                    "id": nid,
                    "title": hit.title,
                    "relpath": hit.relpath,
                    "kind": hit.kind,
                    "via": [" → ".join(p["chain"]) for p in after[nid]["paths"]],
                    "next": (
                        self._next(hit, after[nid]["causes"])
                        if hit.kind == "design"
                        else None
                    ),
                }
            )
        res = {**out, "newly_tainted": newly}
        if newly:
            res["next"] = (
                f"{len(newly)} dependent(s) now read as out of date with this — "
                f"`{kind}_read <ref>` each, then `{kind}_reaffirm <ref>` where it "
                f"still holds"
            )
        return res

    async def design_edit(
        self, proj: str, ref: str, new_content: str, sources: list[Any] | None = None
    ) -> dict[str, Any]:
        """Replace a decision's body through the facet, answering with the
        dependents the change just tainted. `sources` replaces its citations."""
        return await self._write_body(
            proj, ref, lambda _: (new_content or "").strip() + "\n", sources
        )

    async def design_append(
        self, proj: str, ref: str, content: str, sources: list[Any] | None = None
    ) -> dict[str, Any]:
        """Extend a decision's body through the facet, answering with the
        dependents the change just tainted.

        `sources` here ADDS citations (deduped by section) rather than replacing
        them — append semantics for the append verb, and the shape of the real
        sequence it exists for: the decision is written first, the doc-of-record
        grows later, and without a post-hoc wire the doc's edits re-check plan
        items but silently miss the decision itself. Existing citations keep the
        hash they were CAPTURED at (their whole meaning); only the new ones are
        hashed as the doc reads now. `design_edit(sources=)` remains the
        replace-everything spelling."""
        merged = self._append_sources(proj, ref, "design", sources)
        return await self._write_body(
            proj,
            ref,
            lambda body: body.rstrip() + "\n\n" + (content or "").strip() + "\n",
            sources=None if merged is None else merged,
            precaptured=merged is not None,
        )

    def _append_sources(
        self, proj: str, ref: str, kind: str, sources: list[Any] | None
    ) -> list[Any] | None:
        """Merge new citations onto a node's existing ones (deduped by section),
        the additive semantics the append verbs share: existing rows KEEP their
        capture-time hashes, only the new ones are hashed as the doc reads now.
        Returns None when there is nothing to add — leave the citations untouched."""
        if not sources:
            return None
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        kept = [{k: v for k, v in s.items() if k != "current"} for s in node.sources]
        have = {(s.get("ref"), s.get("heading")) for s in kept}
        new = [
            r
            for r in _source_rows(self._capture_sources(proj, sources))
            if (r.get("ref"), r.get("heading")) not in have
        ]
        return kept + new

    def plan_read(self, proj: str, ref: str) -> dict[str, Any]:
        """A plan item's dossier: body, status, every edge annotated, its DERIVED
        blocking (the mixed-dep rule `plan_next` gates on), and its own taint.

        The `design_read` of the plan facet, with the one thing a plan item lives
        or dies by that a decision has no analog for — `blocked_by`: what it is
        waiting on right now, and why (an unfinished plan dep, a tainted or
        proposed design dep). A finished item whose cited source moved carries
        `revisit`; the graph reports it, it never re-opens the status."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "plan")
        gating = self._taint(graph, sources=False)
        full = self._taint(graph)
        unresolved = {d for d in node.deps if d not in graph.nodes}
        note_ids = self._note_dep_ids(proj, unresolved) if unresolved else set()
        row = self._row(graph, node, gating, note_ids, full)
        note = self._note(proj, node)
        out = {
            "project": proj,
            **node.brief(),
            "body": note.body.strip(),
            "deps": [self._annotate(graph, full, d) for d in node.deps],
            "dependents": [
                self._annotate(graph, full, d)
                for d in graph.dependents.get(node.id, [])
            ],
            "sources": [self._source_view(s) for s in node.sources],
            "blocked": row["blocked"],
            "blocked_by": row["blocked_by"],
            "note_deps": row["note_deps"],
            "missing_deps": row["missing_deps"],
            "tainted": node.id in full,
        }
        if row.get("revisit"):
            out["revisit"] = row["revisit"]
            out["next"] = row["next"]
        return out

    async def plan_edit(
        self, proj: str, ref: str, new_content: str, sources: list[Any] | None = None
    ) -> dict[str, Any]:
        """Replace a plan item's body through the facet, answering with the
        dependents the change just tainted — the plan-side `design_edit`.
        `sources` replaces its citations."""
        return await self._write_body(
            proj,
            ref,
            lambda _: (new_content or "").strip() + "\n",
            sources,
            kind="plan",
        )

    async def plan_append(
        self, proj: str, ref: str, content: str, sources: list[Any] | None = None
    ) -> dict[str, Any]:
        """Extend a plan item's body through the facet, answering with the
        dependents the change just tainted — the plan-side `design_append`.

        A plan body is OPTIONAL, so an append onto a title-only item just SETS the
        body rather than prepending blank lines. `sources` ADDS citations (deduped
        by section), existing ones keeping their capture-time hashes."""
        merged = self._append_sources(proj, ref, "plan", sources)

        def rewrite(body: str) -> str:
            text = (content or "").strip() + "\n"
            return body.rstrip() + "\n\n" + text if body.strip() else text

        return await self._write_body(
            proj,
            ref,
            rewrite,
            sources=None if merged is None else merged,
            precaptured=merged is not None,
            kind="plan",
        )

    def design_list(self, proj: str, tainted: bool = False) -> dict[str, Any]:
        """Every decision as a flat table — title, ref, status, taint flag, edge
        counts. The inventory read; `design_tree` is the shape read."""
        graph = self._load_graph(proj)
        stale = self._taint(graph)
        rows = [
            {
                "id": n.id,
                "title": n.title,
                "relpath": n.relpath,
                "status": n.status,
                "updated": n.updated,
                "tainted": n.id in stale,
                "deps": len(n.deps),
                "sources": len(n.sources),
                "dependents": len(graph.dependents.get(n.id, [])),
            }
            for n in sorted(graph.of_kind("design"), key=lambda n: n.title)
        ]
        total, n_tainted = len(rows), sum(1 for r in rows if r["tainted"])
        proposed = sum(1 for r in rows if r["status"] == "proposed")
        if tainted:
            rows = [r for r in rows if r["tainted"]]
        return {
            "project": proj,
            "designs": rows,
            "total": total,
            "tainted": n_tainted,
            "proposed": proposed,
            "filtered": bool(tainted),
            "cycles": [
                [graph.nodes[c].title for c in cyc] for cyc in _cycles(graph.nodes)
            ],
        }

    async def design_dep_add(self, proj: str, ref: str, dep_ref: str) -> dict[str, Any]:
        return await self._dep_add(proj, "design", ref, dep_ref)

    async def design_dep_remove(
        self, proj: str, ref: str, dep_ref: str
    ) -> dict[str, Any]:
        return await self._dep_remove(proj, "design", ref, dep_ref)

    async def design_forget(
        self, proj: str, ref: str, force: bool = False
    ) -> dict[str, Any]:
        return await self._forget(proj, "design", ref, force)

    def design_check(self, proj: str, ref: str | None = None) -> dict[str, Any]:
        """Which decisions are out of date with what they build on, why, and what
        to do about each — every tainted row ends with its prescribed follow-up
        (`next`) so the flag is never a dead end."""
        graph = self._load_graph(proj)
        tainted = self._taint(graph)
        only = self._resolve_ref(graph, ref, "design").id if ref else None
        rows = []
        for nid, info in tainted.items():
            node = graph.nodes[nid]
            if node.kind != "design" or (only and nid != only):
                continue
            rows.append(
                {**node.brief(), **info, "next": self._next(node, info.get("causes"))}
            )
        rows.sort(key=lambda r: r["title"])
        cycles = [[graph.nodes[c].title for c in cyc] for cyc in _cycles(graph.nodes)]
        designs = graph.of_kind("design")
        # Proposed entries are their own queue, not part of the stale one: they
        # aren't out of date, they are un-blessed, and the verb that clears them
        # is `design_promote` rather than `design_reaffirm`.
        proposed = [
            {**n.brief(), "next": f"confirm it, then `design_promote {n.relpath}`"}
            for n in sorted(designs, key=lambda n: n.title)
            if n.status == "proposed" and (not only or n.id == only)
        ]
        return {
            "project": proj,
            "designs": len(designs),
            "tainted": rows,
            "proposed": proposed,
            "clean": not rows,
            "cycles": cycles,
        }

    def _recheck(self, graph: Graph, node: Node) -> tuple[dict[str, str], list[str]]:
        """The dep hashes as they read now, plus the ids that resolve to nothing —
        what a re-blessing (`design_reaffirm`, `design_promote`) records."""
        checked, missing = {}, []
        for dep_id in node.deps:
            dep = graph.nodes.get(dep_id)
            if dep is None:
                missing.append(dep_id)
            else:
                checked[dep_id] = dep.body_hash
        return checked, missing

    async def design_reaffirm(self, proj: str, ref: str) -> dict[str, Any]:
        """Re-record a decision's dep AND source hashes after a human re-read it —
        the ONLY thing that clears taint (short of editing the decision itself).

        Named like `learning_reaffirm` and for the same reason: this is a
        re-blessing against drift, not a proof. Taint is coarse — a dep moved —
        so the common outcome of reading a tainted decision is that it still
        holds and this is a one-line, cheap confirmation, NOT error recovery.

        Both edge families are re-recorded together, because both are what "I
        re-read this and it still holds" is a statement about."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "design")
        checked, missing = self._recheck(graph, node)
        sources, missing_sources = self._recapture(proj, node)
        fm: dict[str, Any] = {"checked": checked}
        if node.sources:
            fm["sources"] = sources
        out = await self._save(proj, node, fm)
        return {
            **out,
            "verified": sorted(checked),
            "missing": missing,
            "sources": [source_label(s) for s in sources],
            "missing_sources": missing_sources,
        }

    async def plan_reaffirm(self, proj: str, ref: str) -> dict[str, Any]:
        """Re-record a plan item's dep AND source hashes after a human re-read
        what moved — the plan-side twin of `design_reaffirm`, and the missing
        half of a symmetry: plan items carry design deps that taint when the
        decision moves, but the only way to clear a benign taint used to be
        `plan_dep_remove` + `plan_dep_add` per edge — a workaround wearing a verb
        costume. Semantically this IS reaffirm: "I re-read the moved decision;
        this item still stands against it; re-record the baseline."

        Distinct from `plan_status` on purpose: a status write is a claim about
        the WORK (and re-records source hashes as part of it); this is a claim
        about the item's GROUND, made without pretending the work moved."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "plan")
        checked, missing = self._recheck(graph, node)
        sources, missing_sources = self._recapture(proj, node)
        fm: dict[str, Any] = {"checked": checked}
        if node.sources:
            fm["sources"] = sources
        out = await self._save(proj, node, fm)
        return {
            **out,
            "verified": sorted(checked),
            "missing": missing,
            "sources": [source_label(s) for s in sources],
            "missing_sources": missing_sources,
        }

    async def design_promote(self, proj: str, ref: str) -> dict[str, Any]:
        """proposed → active: the human act that turns an EXTRACTED decision into
        settled ground others may build on.

        Deliberately its own verb rather than a `status` argument: promotion is
        the whole point of the quarantine tier, and it seeds `checked` and the
        source hashes FRESH — the entry becomes authoritative as of the graph and
        the docs as they read at the moment someone confirmed it, not as of
        whatever the extraction saw."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "design")
        if node.status != "proposed":
            raise CribUserError(
                f"{node.title!r} is already {node.status} — promote applies to "
                f"`proposed` entries (what `design_import`'s procedure creates); "
                f"a decision that drifted is `design_reaffirm`, one that was "
                f"replaced is `design_supersede`"
            )
        checked, missing = self._recheck(graph, node)
        sources, missing_sources = self._recapture(proj, node)
        fm: dict[str, Any] = {"status": "active", "checked": checked}
        if node.sources:
            fm["sources"] = sources
        out = await self._save(proj, node, fm)
        dependents = [graph.nodes[d].brief() for d in graph.dependents.get(node.id, [])]
        return {
            **out,
            "status": "active",
            "verified": sorted(checked),
            "missing": missing,
            "sources": [source_label(s) for s in sources],
            "missing_sources": missing_sources,
            # now that it is active, its edges check like any other decision's
            "dependents": dependents,
            "next": (
                f"{node.title!r} is active — it now taints what builds on "
                f"it, and `design_check` covers it like any other decision"
            ),
        }

    def design_tree(
        self, proj: str, ref: str | None = None, direction: str = "deps", depth: int = 6
    ) -> dict[str, Any]:
        """The dependency tree around a decision — `deps` (what it builds on) or
        `dependents` (what builds on it) — every node taint-flagged."""
        if direction not in ("deps", "dependents"):
            raise CribUserError(
                f"unknown direction {direction!r}: use 'deps' (what this builds on) "
                f"or 'dependents' (what builds on this)"
            )
        graph = self._load_graph(proj)
        tainted = self._taint(graph)
        # Without a ref: render every root of the chosen direction — the nodes
        # nothing points at *along the edges being followed*, so walking down from
        # them reaches the whole forest exactly once.
        roots = (
            [self._resolve_ref(graph, ref, "design")]
            if ref
            else sorted(
                (
                    n
                    for n in graph.of_kind("design")
                    if not (
                        graph.dependents.get(n.id) if direction == "deps" else n.deps
                    )
                ),
                key=lambda n: n.title,
            )
        )

        def build(node: Node, seen: frozenset[str], level: int) -> dict[str, Any]:
            edges = (
                node.deps if direction == "deps" else graph.dependents.get(node.id, [])
            )
            out: dict[str, Any] = {
                **node.brief(),
                "tainted": node.id in tainted,
                "children": [],
            }
            if node.id in seen:  # DAG: shown already, don't re-expand
                out["repeat"] = True
                return out
            if level >= depth:
                return out
            for eid in edges:
                child = graph.nodes.get(eid)
                if child is None:  # dangling id: a warning, not a crash
                    out["children"].append(
                        {
                            "id": eid,
                            "title": f"{eid} (missing)",
                            "missing": True,
                            "children": [],
                        }
                    )
                    continue
                out["children"].append(build(child, seen | {node.id}, level + 1))
            return out

        return {
            "project": proj,
            "direction": direction,
            "roots": [build(r, frozenset(), 0) for r in roots],
        }

    def facet_graph(
        self, proj: str, kind: str = "design", sources: bool = False
    ) -> dict[str, Any]:
        """The decision/plan graph as `{nodes, edges}` — the same consumer contract
        as the symbol graph: every node an object carrying `id` and `name`, every
        edge endpoint declared (lean nodes for note-deps and missing targets), no
        string a consumer has to parse. `id` is the pillar-qualified ref
        (`design:x.md`) — the pasteable spelling `sources` citations already use.

        `kind="design"` is the decision map; `kind="plan"` includes the DESIGN
        nodes plan items depend on, because those edges gate (`plan_next` drops an
        item while its design dep is tainted) and a plan graph that hid them would
        misdraw the plan as self-contained. `tainted` is this graph's ※: computed
        live, never stored. `sources=True` adds doc-section attribution nodes and
        `kind="source"` edges — off by default, they double the node count.

        Edge kinds: `dep` (from depends on to), `superseded_by` (old → new)."""
        if kind not in ("design", "plan"):
            raise CribUserError(
                f"unknown kind {kind!r}: use 'design' (the decision map) or "
                f"'plan' (items plus the decisions they rest on)"
            )
        graph = self._load_graph(proj)
        tainted = self._taint(graph)
        kinds = {"design"} if kind == "design" else {"plan", "design"}
        picked = [n for n in graph.nodes.values() if n.kind in kinds]
        if kind == "plan":
            # only design nodes a plan item actually rests on — the whole decision
            # map belongs to kind="design", not to every plan export
            plan_dep_ids = {d for n in picked if n.kind == "plan" for d in n.deps}
            picked = [n for n in picked if n.kind == "plan" or n.id in plan_dep_ids]

        def ref_of(n: Node) -> str:
            return f"{n.kind}:{n.relpath}"

        nodes: dict[str, dict[str, Any]] = {}
        for n in picked:
            node: dict[str, Any] = {
                "id": ref_of(n),
                "ulid": n.id,
                "name": n.title,
                "kind": n.kind,
                "status": n.status,
                "updated": n.updated,
            }
            if n.kind == "design":
                node["tainted"] = n.id in tainted
            if n.kind == "plan" and n.rank:
                node["rank"] = n.rank
            nodes[n.id] = node  # keyed by ulid while wiring edges

        edges: list[dict[str, str]] = []
        for n in picked:
            for dep in n.deps:
                target = graph.nodes.get(dep)
                if target is not None and target.id in nodes:
                    to = nodes[target.id]["id"]
                elif target is not None:
                    # a real node outside the export (a design edge from a
                    # design-only export never lands here; a plan edge to a note
                    # does not either — this is e.g. kind="design" citing a plan)
                    to = f"{target.kind}:{target.relpath}"
                    nodes.setdefault(
                        target.id,
                        {
                            "id": to,
                            "ulid": target.id,
                            "name": target.title,
                            "kind": target.kind,
                            "truncated": True,
                        },
                    )
                else:
                    # a NOTE dep (never blocks, lives outside this graph) or a
                    # genuinely dangling id — declared lean, never a bare string
                    to = f"note:{dep}"
                    nodes.setdefault(
                        dep,
                        {
                            "id": to,
                            "ulid": dep,
                            "name": dep[-8:],
                            "kind": "note",
                            "external": True,
                        },
                    )
                edges.append({"from": nodes[n.id]["id"], "to": to, "kind": "dep"})
            by = str(n.frontmatter.get("superseded_by") or "")
            if by and by in nodes:
                edges.append(
                    {
                        "from": nodes[n.id]["id"],
                        "to": nodes[by]["id"],
                        "kind": "superseded_by",
                    }
                )
            if sources:
                for s in n.sources:
                    sid = f"doc:{source_label(s)}"
                    if sid not in nodes:
                        nodes[sid] = {
                            "id": sid,
                            "name": str(s.get("heading") or s.get("ref") or "").split(
                                "/"
                            )[-1],
                            "kind": "doc",
                            "synthetic": True,
                        }
                    edges.append(
                        {"from": nodes[n.id]["id"], "to": sid, "kind": "source"}
                    )
        uniq: dict[tuple[str, str, str], dict[str, str]] = {}
        for e in edges:
            uniq[(e["from"], e["to"], e["kind"])] = e
        return {
            "project": proj,
            "kind": kind,
            "shape": "edges",
            "nodes": sorted(nodes.values(), key=lambda x: str(x["id"])),
            "edges": sorted(
                uniq.values(), key=lambda x: (x["from"], x["to"], x["kind"])
            ),
        }

    async def design_supersede(
        self, proj: str, ref: str, by_ref: str | None = None
    ) -> dict[str, Any]:
        """Soft-delete a decision: mark it `superseded` (optionally naming its
        replacement) and taint everything that builds on it.

        The taint comes from the same body hash every other edit uses — the
        supersession is APPENDED to the decision's body — so there is still no
        write fan-out onto dependents (decision 3); they simply read as changed
        until a human verifies them against the replacement."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "design")
        by = self._resolve_ref(graph, by_ref, "design") if by_ref else None
        if by and by.id == node.id:
            raise CribUserError("a decision cannot supersede itself")
        note = self._note(proj, node)
        marker = (
            f"\n> **Superseded** {_today()}"
            + (f" by {by.title!r} ({by.relpath})." if by else ".")
            + " Dependents are tainted until re-verified.\n"
        )
        fm: dict[str, Any] = {"status": "superseded"}
        if by:
            fm["superseded_by"] = by.id
        out = await self._save(proj, node, fm, body=note.body.rstrip() + "\n" + marker)
        dependents = [graph.nodes[d].brief() for d in graph.dependents.get(node.id, [])]
        return {
            **out,
            "status": "superseded",
            "superseded_by": by.id if by else None,
            "tainted_dependents": dependents,
        }

    # ── plan verbs ────────────────────────────────────────────────────────────

    def _rank_for(
        self,
        graph: Graph,
        after: Node | None,
        before: Node | None,
        exclude: str | None = None,
    ) -> str:
        """The rank for an item placed after/before given neighbours (either or
        both may be None — none at all means the end of the list)."""
        items = sorted(
            (n for n in graph.of_kind("plan") if n.id != exclude),
            key=lambda n: (n.rank, n.title),
        )
        ranks = [n.rank for n in items if n.rank]
        if after and before:
            return _rank_between(after.rank, before.rank)
        if after:
            nxt = next((r for r in ranks if r > after.rank), None)
            return _rank_between(after.rank, nxt)
        if before:
            prev = None
            for r in ranks:
                if r >= before.rank:
                    break
                prev = r
            return _rank_between(prev, before.rank)
        return _rank_between(ranks[-1] if ranks else None, None)

    async def plan_add(
        self,
        proj: str,
        title: str | None = None,
        content: str = "",
        deps: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        items: list[dict[str, Any]] | None = None,
        sources: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Add plan items (default: at the end; `after`/`before` place them).

        Takes ONE item (`title`/`content`/`deps`/`sources`) or a BATCH (`items`),
        because planning happens in batches: writing five items is one thought, and
        five calls is five chances to lose the thread. Batch items land contiguously
        in the order given, and may depend on each other by 1-based batch position
        (`"#1"`) as well as by any existing ref — the ordering constraint you
        actually mean is usually to the item you just wrote, which has no id yet.
        A batch item may carry its own `sources`, which is what `plan_import`'s
        procedure produces: one item per actionable passage, each citing it.

        A plan item's BODY IS OPTIONAL: a title is a legitimate whole item
        ("wire up the emitter"). A design decision's body is not optional — there
        the rationale IS the artifact."""
        batch = (
            items
            if items is not None
            else [
                {"title": title, "content": content, "deps": deps, "sources": sources}
            ]
        )
        if not batch:
            raise CribUserError("plan_add needs a title, or items=[…]")
        rows: list[dict[str, Any]] = []
        by_index: dict[str, str] = {}  # "#1" → the id it created
        prev: str | None = None
        for i, raw in enumerate(batch, 1):
            # a bare string is the obvious shorthand; normalize to a typed dict so
            # deps/sources below read as their declared types, not `Any | str`
            item: dict[str, Any] = raw if isinstance(raw, dict) else {"title": str(raw)}
            raw_deps: list[str] = item.get("deps") or []
            # "#n" resolves against THIS batch; anything else is an ordinary ref
            item_deps = [by_index[d] if d in by_index else d for d in raw_deps]
            unknown = [d for d in raw_deps if d.startswith("#") and d not in by_index]
            if unknown:
                raise CribUserError(
                    f"item {i} depends on {unknown[0]!r}, which is not an EARLIER "
                    f"item in this batch (use #1…#{i - 1}, or an existing ref) — "
                    f"a batch dep can only point backwards"
                )
            graph = self._load_graph(proj)
            a = (
                self._resolve_ref(graph, prev, "plan")
                if prev
                else self._resolve_ref(graph, after, "plan")
                if after
                else None
            )
            b = (
                self._resolve_ref(graph, before, "plan")
                if before and not prev
                else None
            )
            rank = self._rank_for(graph, a, b)
            out = await self._add(
                proj,
                "plan",
                str(item.get("title") or ""),
                str(item.get("content") or ""),
                item_deps,
                {"rank": rank},
                item.get("sources"),
            )
            by_index[f"#{i}"] = out["id"]
            prev = out["id"]
            rows.append({**out, "rank": rank})
        # A single-item call keeps its historical top-level shape; a batch reads
        # off `items`/`added` (which the single call also carries).
        head = rows[0] if len(rows) == 1 else {"project": proj}
        return {**head, "items": rows, "added": len(rows)}

    async def plan_status(self, proj: str, ref: str, status: str) -> dict[str, Any]:
        """Set an item's status, answering with what that just UNBLOCKED.

        The plan-side mirror of `design_edit`'s `newly_tainted`: finishing work is
        an edge event too, so completing an item names the dependents whose deps
        are now all satisfied — the next actionable step arrives with the
        completion instead of waiting for someone to think to ask `plan_next`.

        Marking `done` with unfinished deps WARNS rather than blocks — the deps
        are the plan's opinion, not its jailer.

        A status write also RE-RECORDS the item's source hashes: setting a status
        is a statement about the work as of now, so it is also a statement about
        the passages it was done against. That is what clears a `revisit` flag —
        re-running `plan_status <ref> done` after re-reading a changed source is
        the plan-side `design_reaffirm`, and it needs no verb of its own."""
        if status not in PLAN_STATUSES:
            raise CribUserError(
                f"unknown status {status!r}: use one of {', '.join(PLAN_STATUSES)} "
                f"('blocked' is derived from deps, never set)"
            )
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "plan")
        warnings = []
        if status in DONE_STATUSES:
            open_deps = [
                graph.nodes[d].title
                for d in node.deps
                if d in graph.nodes
                and graph.nodes[d].kind == "plan"
                and graph.nodes[d].status not in DONE_STATUSES
            ]
            if open_deps:
                warnings.append(
                    f"marked {status} while {len(open_deps)} dep(s) are unfinished: "
                    + ", ".join(repr(t) for t in open_deps)
                )
        # what was blocked BEFORE — so `unblocked` names only what this call freed,
        # not everything that happens to be ready now
        was_blocked = {
            r["id"]
            for r in self._rows(proj, graph, graph.of_kind("plan"))
            if r["blocked"]
        }
        fm: dict[str, Any] = {"status": status}
        missing_sources: list[str] = []
        if node.sources:
            fm["sources"], missing_sources = self._recapture(proj, node)
        out = await self._save(proj, node, fm)
        unblocked = []
        if status in DONE_STATUSES:
            after = self._load_graph(proj)
            for row in self._rows(proj, after, after.of_kind("plan")):
                if (
                    row["id"] in was_blocked
                    and not row["blocked"]
                    and row["status"] not in DONE_STATUSES
                ):
                    unblocked.append(
                        {
                            "id": row["id"],
                            "ref": row["relpath"],
                            "title": row["title"],
                            "status": row["status"],
                        }
                    )
        if missing_sources:
            warnings.append(
                "cited source(s) no longer resolve: " + ", ".join(missing_sources)
            )
        return {
            **out,
            "status": status,
            "warnings": warnings,
            "missing_sources": missing_sources,
            "unblocked": unblocked,
        }

    async def plan_dep_add(self, proj: str, ref: str, dep_ref: str) -> dict[str, Any]:
        return await self._dep_add(proj, "plan", ref, dep_ref)

    async def plan_dep_remove(
        self, proj: str, ref: str, dep_ref: str
    ) -> dict[str, Any]:
        return await self._dep_remove(proj, "plan", ref, dep_ref)

    async def plan_forget(
        self, proj: str, ref: str, force: bool = False
    ) -> dict[str, Any]:
        return await self._forget(proj, "plan", ref, force)

    async def plan_move(
        self, proj: str, ref: str, after: str | None = None, before: str | None = None
    ) -> dict[str, Any]:
        """Re-rank an item. Deps are NOT touched: order is preference, deps are
        correctness (decision 5), so moving can never break the plan."""
        if not (after or before):
            raise CribUserError("plan_move needs after=<ref> and/or before=<ref>")
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "plan")
        a = self._resolve_ref(graph, after, "plan") if after else None
        b = self._resolve_ref(graph, before, "plan") if before else None
        if node.id in {n.id for n in (a, b) if n}:
            raise CribUserError(f"{node.title!r} cannot be placed relative to itself")
        rank = self._rank_for(graph, a, b, exclude=node.id)
        out = await self._save(proj, node, {"rank": rank})
        return {**out, "rank": rank, "deps": list(node.deps)}

    def _ordered(self, graph: Graph) -> tuple[list[Node], list[list[str]]]:
        """Topological order over plan items, rank breaking ties among the ready
        set — deps guarantee correctness, rank expresses preference (decision 5).
        Returns (ordered items, cycles); items caught in a cycle come last."""
        plans = {n.id: n for n in graph.of_kind("plan")}
        pending = {nid: {d for d in n.deps if d in plans} for nid, n in plans.items()}
        order: list[Node] = []
        while True:
            ready = sorted(
                (plans[nid] for nid, d in pending.items() if not d),
                key=lambda n: (n.rank, n.title),
            )
            if not ready:
                break
            node = ready[0]
            order.append(node)
            pending.pop(node.id)
            for rest in pending.values():
                rest.discard(node.id)
        cycles = [[graph.nodes[c].title for c in cyc] for cyc in _cycles(graph.nodes)]
        if pending:  # cycle survivors, deterministic order
            order += sorted(
                (plans[nid] for nid in pending), key=lambda n: (n.rank, n.title)
            )
        return order, cycles

    def _note_dep_ids(self, proj: str, ids: set[str]) -> set[str]:
        """Which of these unresolved dep ids name a PLAIN NOTE in this project.

        A plan item may depend on something that isn't in the graph at all. Two
        very different things look identical from the graph's side, so we look:
        an id that still names a note is a REFERENCE (never a gate), while an id
        that names nothing is a genuinely missing dep. Only walked when there are
        unresolved ids, which is the rare case."""
        found: set[str] = set()
        try:
            root = self.notestore.notes_root(proj)
        except Exception:  # noqa: BLE001 — an unreachable store isn't this read's job
            return found
        for path in root.rglob("*.md"):
            try:
                nid = notes.scan_id(path.read_text())
            except (OSError, UnicodeDecodeError):
                continue
            if nid in ids:
                found.add(nid)
                if len(found) == len(ids):
                    break
        return found

    def _rows(self, proj: str, graph: Graph, nodes: list[Node]) -> list[dict[str, Any]]:
        """Plan rows with their DERIVED blocking, under the mixed-dep rule (below
        in `_row`). Resolves the three dep kinds once for the whole listing.

        TWO taint views, because the two edge families differ in exactly one way:
        `gating` is dep-only and decides what blocks, while the full view supplies
        the `revisit` flag. A decision whose SOURCE doc was reworded must be
        re-read, but it must not stop work being picked up — sources check, they
        never gate."""
        gating = self._taint(graph, sources=False)
        full = self._taint(graph)
        unresolved = {d for n in nodes for d in n.deps if d not in graph.nodes}
        note_ids = self._note_dep_ids(proj, unresolved) if unresolved else set()
        return [self._row(graph, n, gating, note_ids, full) for n in nodes]

    def _row(
        self,
        graph: Graph,
        node: Node,
        tainted: dict[str, Any],
        note_ids: set[str],
        full: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One plan item + its derived state, under the MIXED-DEP RULE:

        - a **plan** dep blocks until it is `done`/`verified` — it is work that
          must happen first;
        - a **design** dep blocks while it is TAINTED and only then — an untainted
          decision is stable ground to build on, a tainted one means the ground
          moved and the item would be built against a decision nobody has
          re-read — and equally while it is PROPOSED: an extracted decision
          nobody has confirmed is unstable ground of a different flavour, so it
          gates until `design_promote`;
        - a plain **note** dep NEVER blocks. It is a reference, not a gate.

        A dep id that resolves to nothing at all is neither: it is reported as
        `missing_deps` (visible, not blocking) — a dangling id must not silently
        wedge the plan.

        Finished items carry `revisit` when a SOURCE they cite has changed: the
        graph reports, it never re-opens a status. Reverting `done` to `todo`
        behind someone's back would be the graph overruling the human who set it;
        a flag lets them decide."""
        blockers, note_deps, missing = [], [], []
        for dep_id in node.deps:
            dep = graph.nodes.get(dep_id)
            if dep is None:
                (note_deps if dep_id in note_ids else missing).append(dep_id)
            elif dep.kind == "plan":
                if dep.status not in DONE_STATUSES:
                    blockers.append(
                        {
                            "id": dep.id,
                            "ref": dep.relpath,
                            "title": dep.title,
                            "kind": "plan",
                            "status": dep.status,
                        }
                    )
            elif dep.status == "proposed":
                blockers.append(
                    {
                        "id": dep.id,
                        "ref": dep.relpath,
                        "title": dep.title,
                        "kind": "design",
                        "status": "proposed — `design_promote` it",
                    }
                )
            elif dep.id in tainted:
                blockers.append(
                    {
                        "id": dep.id,
                        "ref": dep.relpath,
                        "title": dep.title,
                        "kind": "design",
                        "status": "stale — `design_read` it",
                    }
                )
        causes = ((full or {}).get(node.id, {}) or {}).get("causes") or []
        moved = [c for c in causes if c["change_kind"] in SOURCE_KINDS]
        row = {
            **node.brief(),
            "blocked": bool(blockers),
            "blocked_by": blockers,
            "note_deps": note_deps,
            "missing_deps": missing,
            "group": _group(node, bool(blockers)),
        }
        if moved and node.status in DONE_STATUSES:
            row["revisit"] = [c["reason"] for c in moved]
            row["next"] = (
                f"a source {node.title!r} was done against has changed — re-read "
                f"{', '.join(dict.fromkeys(c['dep_title'] for c in moved))} and "
                f"decide: still done, or `plan_status {node.relpath} todo`"
            )
        return row

    def plan_list(self, proj: str, all: bool = False) -> dict[str, Any]:
        """The plan as a WORKING SET, not a graph dump: in-progress first, then
        ready, then blocked (each naming what it waits on), with finished work
        hidden unless `all`.

        Topological + rank order still holds WITHIN each group — the grouping only
        answers the question actually being asked ("what am I on, what can I pick
        up, what can't I") ahead of the question the raw graph answers."""
        graph = self._load_graph(proj)
        order, cycles = self._ordered(graph)
        rows = self._rows(
            proj, graph, [n for n in order if all or n.status not in DONE_STATUSES]
        )
        rows.sort(key=lambda r: _GROUPS.index(r["group"]))  # stable: topo+rank kept
        groups: dict[str, int] = {}
        for row in rows:
            groups[row["group"]] = groups.get(row["group"], 0) + 1
        return {
            "project": proj,
            "items": rows,
            "total": len(order),
            "groups": groups,
            "hidden": len(order) - len(rows),
            "revisit": sum(1 for r in rows if r.get("revisit")),
            "cycles": cycles,
        }

    def plan_next(self, proj: str, k: int = 5) -> dict[str, Any]:
        """What is actually actionable now: `todo` items nothing blocks, in rank
        order. The 'what do I do next' read at the start of a session.

        Blocking is the mixed-dep rule in `_row`: plan deps until done, design
        deps while tainted OR PROPOSED, note deps never. A proposed design dep
        gates for the same reason a tainted one does — unpromoted ground is
        unstable ground, and an item built on an extracted-but-unconfirmed
        decision is work done against something nobody has agreed to yet. It is
        cleared by `design_promote`, not by waiting.

        SOURCES DO NOT GATE. A decision whose cited doc section was reworded is
        tainted for re-reading, but the work built on it stays actionable —
        attribution edges check, they never block.

        IN-PROGRESS ITEMS ARE EXCLUDED, deliberately: `in-progress` means CLAIMED,
        and several agents may read this plan at once. Taking an item means
        marking it, which is what makes the claim visible to everyone else."""
        graph = self._load_graph(proj)
        order, _ = self._ordered(graph)
        rows = [
            r
            for r in self._rows(proj, graph, [n for n in order if n.status == "todo"])
            if not r["blocked"]
        ]
        for row in rows:
            row["next"] = (
                f"starting it? `plan_status {row['relpath']} in-progress` (that is "
                f"the claim other agents read); finished? `plan_status "
                f"{row['relpath']} done` — the result names what you unblocked"
            )
        return {
            "project": proj,
            "items": rows[: max(1, k)],
            "ready": len(rows),
            "claimed": sum(
                1 for n in graph.of_kind("plan") if n.status == "in-progress"
            ),
        }

    # ── import: the description IS the procedure ──────────────────────────────
    # IMPORT IS HOST-LLM DRIVEN; THIS SIDE IS TOOLING AND INSTRUCTION ONLY. These
    # verbs do exactly three MECHANICAL things:
    #   (a) enumerate the doc's sections — heading paths + `section_hash`. Server
    #       side because the hash must be the one taint-checking computes; a
    #       caller cannot reproduce it, and a citation built from a different
    #       hash would never match.
    #   (b) list the entries already citing the doc (dedupe context).
    #   (c) return the extraction procedure as instruction text.
    # They do NOT interpret content: no "looks like a decision" heuristic, no
    # keyword scanning, no summarization, no classification. If any of that ever
    # appears here, delete it — the host LLM reads the sections and exercises ALL
    # judgement through the ordinary add/dep/source verbs, which is the only way
    # the resulting entries are ones somebody stands behind. DESIGN §5's precedent
    # is that a description doubles as its usage instruction; here it IS the
    # payload.

    def _citing(self, proj: str, relpath: str, kind: str) -> list[dict[str, Any]]:
        """Entries that already cite this doc — the dedupe context, so a second
        import extends the graph instead of forking it."""
        graph = self._load_graph(proj)
        out = []
        for node in sorted(graph.of_kind(kind), key=lambda n: n.title):
            cited = [source_label(s) for s in node.sources if s["ref"] == relpath]
            if cited:
                out.append({**node.brief(), "cites": cited})
        return out

    def _import(self, proj: str, kind: str, relpath: str) -> dict[str, Any]:
        doc = self._resolve_doc(proj, relpath)
        sections = [
            {
                "heading_path": r["heading_path"],
                # the citation string, verbatim — this is what goes in
                # `sources`, so nothing has to be re-derived by hand
                "source": f"{doc}#{r['key']}" if r["key"] else doc,
                "section_hash": r["section_hash"],
                "words": r["words"],
                "preview": r["preview"],
                "indexed": r["indexed"],
            }
            for r in self._doc_sections(proj, doc)
        ]
        existing = self._citing(proj, doc, kind)
        path = ""
        # best-effort absolute path; a doc that doesn't resolve just omits it
        with contextlib.suppress(OSError, ValueError):
            path = str(self._doc_abspath(proj, doc))
        return {
            "project": proj,
            "kind": kind,
            "relpath": doc,
            "path": path,
            "sections": sections,
            "existing": existing,
            "instruction": (
                _DESIGN_IMPORT if kind == "design" else _PLAN_IMPORT
            ).format(relpath=doc),
            "next": (
                f"read {doc}, then follow `instruction` — nothing has been "
                f"written yet; this verb only prepared the citations"
            ),
        }

    def design_import(self, proj: str, relpath: str) -> dict[str, Any]:
        """Prepare a doc for extraction into the DESIGN graph: its sections with
        their current hashes, what already cites it, and the procedure to follow.

        Runs no model and interprets nothing — the session LLM does the reading
        and ALL the judgement, which is the point: a parser guessing at which
        paragraphs are decisions would produce entries nobody stands behind. What
        the caller cannot do for itself is compute the section hashes taint
        checking will compare against, so that is what this hands it."""
        return self._import(proj, "design", relpath)

    def plan_import(self, proj: str, relpath: str) -> dict[str, Any]:
        """The same for the PLAN facet: a doc's sections + the procedure for
        turning its actionable passages into plan items that cite them."""
        return self._import(proj, "plan", relpath)
