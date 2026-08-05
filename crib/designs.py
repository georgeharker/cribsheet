"""Design decisions and plan items — notes with a dependency graph over them.

Two facets, one module (docs/plans/design-plan-tracking.md):

- **design** — durable decisions under `notes/design/`, each declaring the
  decisions it builds on. Editing or deleting one *taints* its dependents so a
  human re-checks them; the point is to make design drift visible instead of
  surprising.
- **plan** — resumable work items under `notes/plans/`, with a status, the same
  dependency edges, and a lexorank ordering that survives insert-between.

Both are ordinary NOTES with structured frontmatter, so they inherit search,
versioning, git sync and the merge driver for free (`type: design` / `type: plan`
reaches chunk metadata, so `note_lookup(tags=["design"])` filters them). The only
new machinery is here: the graph load, cycle detection, the body-hash taint, ref
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

The FACET IS THE INTERFACE: `design_read`/`design_edit`/`design_append`/
`design_lookup` are the way in, not `note_read`/`note_edit` on a path under
`design/`. Notes-in-a-directory is the backend; every facet verb is a chance to
speak the edges, which is what the raw note verbs cannot do.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import notes
from .notes import Note
from .util import sha1_hex

if TYPE_CHECKING:
    from .notestore import NoteStore
    from .paths import Paths

DESIGN_DIR = "design"
PLANS_DIR = "plans"
_DIRS = {"design": DESIGN_DIR, "plan": PLANS_DIR}

DESIGN_STATUSES = ("active", "superseded")
# `blocked` is DERIVED (any dep not done/verified), never stored — decision 6.
PLAN_STATUSES = ("todo", "in-progress", "done", "verified")
DONE_STATUSES = ("done", "verified")

_ALPHA = "abcdefghijklmnopqrstuvwxyz"
_N = len(_ALPHA)

# WHAT happened to a dep, per tainted entry — coarse on purpose, because taint
# means "a dep moved", not "this is wrong". The kind tells the reader how much
# re-reading the reaffirm actually needs.
CHANGE_KINDS = ("dep-edited", "dep-superseded", "dep-deleted", "new-unverified-edge")

# How `plan_list` presents the plan: the working set first, the graph second.
# The order of this tuple IS the rendering order.
_GROUPS = ("in-progress", "ready", "blocked", "done")


def _group(node: Node, blocked: bool) -> str:
    """Which working-set band an item belongs to (see `_GROUPS`)."""
    if node.status in DONE_STATUSES:
        return "done"
    if node.status == "in-progress":
        return "in-progress"        # claimed: shown first even when blocked
    return "blocked" if blocked else "ready"


def _today() -> str:
    return datetime.date.today().isoformat()


def _body_hash(body: str) -> str:
    """Identity of a node's PROSE — frontmatter excluded on purpose: `updated`,
    a status flip or a rank change must not taint dependents, only the decision
    text itself."""
    return sha1_hex(body.strip())


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
                hi = ""                 # already strictly below: hi stops binding
            elif i + 1 == len(hi):      # matched hi exactly; anything deeper is >
                raise ValueError(
                    f"no rank fits between {a!r} and {b!r} — they are adjacent "
                    f"(a rank ending in 'a' leaves no gap below it)")
        i += 1


@dataclass
class Node:
    """One design/plan note, loaded from its frontmatter + body."""
    id: str
    kind: str                                   # "design" | "plan"
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

    def brief(self) -> dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "relpath": self.relpath,
               "title": self.title, "status": self.status, "deps": list(self.deps),
               "updated": self.updated}
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
                continue                        # dangling: a warning, not a cycle
            if colour[dep] == GREY:
                cyc = stack[stack.index(dep):] + [dep]
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
    def __init__(self, paths: Paths, notestore: NoteStore) -> None:
        self.paths = paths
        self.notestore = notestore

    # ── loading ───────────────────────────────────────────────────────────────

    def _load_graph(self, proj: str) -> Graph:
        """Scan `notes/design/` + `notes/plans/` into a graph. Frontmatter-only
        parsing (no chunking, no embedding) — cheap enough to run on every verb,
        which is what lets taint be computed live instead of stored."""
        graph = Graph()
        root = self.notestore.notes_root(proj)
        for kind, sub in _DIRS.items():
            d = root / sub
            if not d.exists():
                continue
            for path in sorted(d.glob("*.md")):
                try:
                    fm, body = notes.parse(path.read_text(), path)
                except (OSError, UnicodeDecodeError, notes.NoteParseError):
                    continue            # one broken note must not blind the graph
                nid = str(fm.get("id") or "")
                if not nid:
                    continue            # not indexed yet (no id stamped)
                deps = [str(x) for x in (fm.get("deps") or [])]
                checked = {str(k): str(v) for k, v in (fm.get("checked") or {}).items()}
                graph.nodes[nid] = Node(
                    id=nid, kind=kind, relpath=f"{sub}/{path.name}",
                    title=str(fm.get("title") or path.stem),
                    status=str(fm.get("status")
                               or ("active" if kind == "design" else "todo")),
                    deps=deps, checked=checked, rank=str(fm.get("rank") or ""),
                    body_hash=_body_hash(body), frontmatter=fm,
                    updated=str(fm.get("updated") or ""))
        for node in graph.nodes.values():
            for dep in node.deps:
                graph.dependents.setdefault(dep, []).append(node.id)
        return graph

    def _note(self, proj: str, node: Node) -> Note:
        return notes.load(self.notestore.abspath(proj, node.relpath))

    # ── refs ──────────────────────────────────────────────────────────────────

    def _resolve_ref(self, graph: Graph, ref: str,
                     kind: str | None = None) -> Node:
        """Resolve a user-supplied reference to exactly one node: a full ULID, a
        unique ULID prefix, a relpath (`design/x.md`, `x.md` or bare `x`), or the
        title / its slug. Ambiguity lists the candidates rather than guessing —
        a wrong node is worse than a second call."""
        from .app import _slug
        ref = (ref or "").strip()
        if not ref:
            raise ValueError("empty ref — pass an id, relpath, or title")
        pool = [n for n in graph.nodes.values() if kind is None or n.kind == kind]
        if not pool:
            raise ValueError(
                f"no {kind or 'design/plan'} notes yet — "
                f"`{kind or 'design'}_add` creates the first one")
        want, slug = ref.lower(), _slug(ref)
        exact = [n for n in pool if n.id == ref.upper()]
        if len(exact) == 1:
            return exact[0]
        matches = [n for n in pool
                   if n.relpath.lower() in (want, f"{_DIRS[n.kind]}/{want}",
                                            f"{_DIRS[n.kind]}/{want}.md")
                   or n.relpath.rsplit("/", 1)[1].lower() in (want, f"{want}.md")
                   or n.title.lower() == want
                   or _slug(n.title) == slug]
        if not matches:
            matches = [n for n in pool if n.id.startswith(ref.upper())]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                f"no {kind or 'design/plan'} note matches {ref!r} — "
                f"reference it by id, relpath or title "
                f"(`{kind or 'design'}_check` / `plan_list` show what exists)")
        listing = ", ".join(f"{n.id[:8]}… {n.relpath}" for n in matches[:8])
        raise ValueError(f"ambiguous ref {ref!r} — {len(matches)} matches: {listing}")

    # ── taint ─────────────────────────────────────────────────────────────────

    def _direct_taint(self, graph: Graph) -> dict[str, list[dict[str, Any]]]:
        """Per node, why its OWN deps are out of date (decision 3), as structured
        causes: `{dep, dep_title, dep_updated, change_kind, reason}`.

        Four kinds, and no others — a dep that is gone (`dep-deleted`), one
        declared but never reaffirmed here (`new-unverified-edge`), one whose body
        moved (`dep-edited`), and the special case of that where the dep is now
        marked superseded (`dep-superseded`). The kind and the dep's `updated`
        date travel with the entry so the reader can size the re-read before
        opening anything."""
        out: dict[str, list[dict[str, Any]]] = {}
        for node in graph.nodes.values():
            for dep_id in node.deps:
                dep = graph.nodes.get(dep_id)
                if dep is None:
                    cause = {"dep": dep_id, "dep_title": dep_id, "dep_updated": None,
                             "change_kind": "dep-deleted",
                             "reason": f"dep {dep_id} is missing "
                                       f"(deleted, or never existed)"}
                elif dep_id not in node.checked:
                    cause = {"dep": dep_id, "dep_title": dep.title,
                             "dep_updated": dep.updated or None,
                             "change_kind": "new-unverified-edge",
                             "reason": f"{dep.title!r} was added as a dep but never "
                                       f"verified here"}
                elif node.checked[dep_id] != dep.body_hash:
                    superseded = dep.status == "superseded"
                    when = f" (dep updated {dep.updated})" if dep.updated else ""
                    cause = {
                        "dep": dep_id, "dep_title": dep.title,
                        "dep_updated": dep.updated or None,
                        "change_kind": "dep-superseded" if superseded else "dep-edited",
                        "reason": f"{dep.title!r} "
                                  f"{'was superseded' if superseded else 'changed'} "
                                  f"since this was last verified{when}"}
                else:
                    continue
                out.setdefault(node.id, []).append(cause)
        return out

    def _taint(self, graph: Graph) -> dict[str, dict[str, Any]]:
        """{id: {reasons, causes, paths}} for every tainted node — direct causes
        plus transitive reachability over tainted edges. `paths` spells out the
        chain (`X → Y → Z`, Z being what actually changed) so `check` can say
        *why*, and each path carries the change kind + the dep's `updated` date of
        the cause at its far end.

        EVERY edge is walked here; there is no edge kind that informs without
        checking (see the module docstring)."""
        direct = self._direct_taint(graph)
        tainted: set[str] = set(direct)
        changed = True
        while changed:                          # reachability over tainted edges
            changed = False
            for node in graph.nodes.values():
                if node.id in tainted:
                    continue
                if any(d in tainted for d in node.deps):
                    tainted.add(node.id)
                    changed = True

        def chains(nid: str, stack: list[str]) -> list[dict[str, Any]]:
            node = graph.nodes[nid]
            out = [{"chain": [graph.nodes[s].title for s in stack] + [node.title],
                    "cause": c["reason"], "change_kind": c["change_kind"],
                    "dep": c["dep"], "dep_title": c["dep_title"],
                    "dep_updated": c["dep_updated"]}
                   for c in direct.get(nid, [])]
            for dep_id in node.deps:
                if dep_id in tainted and dep_id in graph.nodes \
                        and dep_id not in stack and dep_id != nid:
                    out += chains(dep_id, [*stack, nid])
            return out

        result: dict[str, dict[str, Any]] = {}
        for nid in tainted:
            paths = chains(nid, [])
            causes = direct.get(nid, [])
            reasons = [c["reason"] for c in causes] or sorted(
                {f"depends on {p['chain'][1]!r}, which is tainted"
                 for p in paths if len(p["chain"]) > 1})
            result[nid] = {"reasons": reasons, "causes": causes, "paths": paths}
        return result

    def _annotate(self, graph: Graph, tainted: dict[str, Any],
                  dep_id: str) -> dict[str, Any]:
        """One edge target, rendered for a dossier: enough to decide whether to
        open it without opening it. A dangling id says so rather than vanishing."""
        node = graph.nodes.get(dep_id)
        if node is None:
            return {"id": dep_id, "title": f"{dep_id} (missing)", "missing": True,
                    "tainted": True}
        return {"id": node.id, "title": node.title, "relpath": node.relpath,
                "status": node.status, "updated": node.updated,
                "tainted": node.id in tainted}

    def _next(self, node: Node) -> str:
        """The prescribed follow-up for a tainted decision — the one string every
        taint-bearing result ends with, so a reader is never left holding a flag
        with no verb attached. Taint is COARSE ("a dep moved"), so reaffirm is the
        normal, cheap outcome; supersede is the exception."""
        return (f"reconsider {node.title!r} against what changed, then "
                f"`design_reaffirm {node.relpath}` (the usual case — taint means a "
                f"dep moved, not that this is wrong); if it no longer holds, "
                f"`design_supersede {node.relpath} <replacement>`")

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
        return {n.relpath for n in graph.nodes.values()
                if n.kind == "design" and n.id in tainted}

    def annotate_hits(self, proj: str, hits: list[dict[str, Any]]
                      ) -> list[dict[str, Any]]:
        """Stamp facet state onto retrieval hits that land on a design/plan note:
        `status`, `tainted`, and dep/dependent counts. The agent reasoning FROM a
        stale decision is told so at the moment it reads it, which is the only
        moment the warning can change what it does."""
        try:
            graph = self._load_graph(proj)
            tainted = self._taint(graph)
        except Exception:  # noqa: BLE001 — as `tainted_designs`: never break a read
            return hits
        by_relpath = {n.relpath: n for n in graph.nodes.values()}
        for hit in hits:
            node = by_relpath.get(str(hit.get("relpath") or ""))
            if node is None:
                continue
            hit.update(kind=node.kind, status=node.status,
                       tainted=node.id in tainted, deps=len(node.deps),
                       dependents=len(graph.dependents.get(node.id, [])))
        return hits

    # ── writes ────────────────────────────────────────────────────────────────

    def _unique_relpath(self, proj: str, sub: str, slug: str) -> str:
        """`<dir>/<slug>.md`, numeric suffix only on collision (DESIGN §15.1)."""
        base = self.notestore.notes_root(proj) / sub
        if not (base / f"{slug}.md").exists():
            return f"{sub}/{slug}.md"
        i = 2
        while (base / f"{slug}-{i}.md").exists():
            i += 1
        return f"{sub}/{slug}-{i}.md"

    async def _save(self, proj: str, node: Node, fm: dict[str, Any],
                    body: str | None = None) -> dict[str, Any]:
        """Rewrite a node's frontmatter (body untouched unless given), stamp
        `updated`, and funnel through the locked index_file write path."""
        path = self.notestore.abspath(proj, node.relpath)
        note = notes.load(path)
        fm = {**note.frontmatter, **fm, "updated": _today()}
        note.frontmatter = fm
        if body is not None:
            note.body = body
        res = await self.notestore.write(proj, node.relpath, note)
        return {"project": proj, "id": node.id, "relpath": node.relpath,
                "title": node.title, "indexed": res.upserted}

    async def _add(self, proj: str, kind: str, title: str, content: str,
                   deps: list[str] | None, extra: dict[str, Any]) -> dict[str, Any]:
        from .app import _slug
        if not (title or "").strip():
            raise ValueError("a design/plan note needs a title")
        if kind == "design" and not (content or "").strip():
            raise ValueError(
                "a design decision needs a body — the choice, why, and what was "
                "rejected. (A plan item may be title-only; a decision may not: "
                "the rationale is the thing a future reader comes back for.)")
        graph = self._load_graph(proj)
        dep_nodes = [self._resolve_ref(graph, r) for r in (deps or [])]
        relpath = self._unique_relpath(proj, _DIRS[kind], _slug(title))
        # A new decision is born VERIFIED: it was written against the deps as they
        # read right now, so seeding `checked` says exactly that (a fresh note
        # showing up already tainted would be noise, not signal).
        checked = ({"checked": {n.id: n.body_hash for n in dep_nodes}}
                   if kind == "design" else {})
        fm: dict[str, Any] = {
            "title": title.strip(), "type": kind,
            "status": extra.pop("status", "active" if kind == "design" else "todo"),
            "deps": [n.id for n in dep_nodes], "links": [], **checked,
            **extra, "created": _today(), "updated": _today()}
        note = Note(path=self.notestore.abspath(proj, relpath), frontmatter=fm,
                    body=(content or "").strip() + "\n")
        res = await self.notestore.write(proj, relpath, note)
        return {"project": proj, "id": note.id, "relpath": relpath,
                "title": fm["title"], "deps": fm["deps"], "indexed": res.upserted}

    async def _dep_add(self, proj: str, kind: str, ref: str,
                       dep_ref: str) -> dict[str, Any]:
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        dep = self._resolve_ref(graph, dep_ref)
        if dep.id == node.id:
            raise ValueError(f"{node.title!r} cannot depend on itself")
        if dep.id in node.deps:
            return {"project": proj, "id": node.id, "relpath": node.relpath,
                    "title": node.title, "dep": dep.id, "already": True,
                    "deps": list(node.deps)}
        probe = {nid: Node(**{**vars(n), "deps": list(n.deps)})
                 for nid, n in graph.nodes.items()}
        probe[node.id].deps.append(dep.id)
        for cyc in _cycles(probe):
            if node.id in cyc:
                path = " → ".join(probe[c].title for c in cyc)
                raise ValueError(
                    f"that dep would create a cycle: {path}. Dependencies must be "
                    f"a DAG — drop the opposite edge first")
        deps = [*node.deps, dep.id]
        # Deliberately does NOT seed `checked`: a newly declared dep starts
        # UNVERIFIED, so the node shows up in `check` — the nudge to actually
        # reconsider it against what it now depends on.
        out = await self._save(proj, node, {"deps": deps})
        return {**out, "dep": dep.id, "dep_title": dep.title, "deps": deps}

    async def _dep_remove(self, proj: str, kind: str, ref: str,
                          dep_ref: str) -> dict[str, Any]:
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        try:
            dep_id = self._resolve_ref(graph, dep_ref).id
        except ValueError:
            dep_id = dep_ref.strip().upper()    # dangling dep: remove by raw id
        if dep_id not in node.deps:
            raise ValueError(f"{node.title!r} does not depend on {dep_ref!r}")
        deps = [d for d in node.deps if d != dep_id]
        checked = {k: v for k, v in node.checked.items() if k != dep_id}
        fm: dict[str, Any] = {"deps": deps}
        if node.kind == "design":
            fm["checked"] = checked
        out = await self._save(proj, node, fm)
        return {**out, "dep": dep_id, "deps": deps}

    async def _forget(self, proj: str, kind: str, ref: str,
                      force: bool) -> dict[str, Any]:
        """Delete blocks on dependents (decision 4) — `force` deletes anyway and
        leaves them tainted (their `checked` now points at a missing id)."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, kind)
        dependents = [graph.nodes[d].brief() for d in graph.dependents.get(node.id, [])]
        if dependents and not force:
            listing = ", ".join(f"{d['title']!r} ({d['relpath']})" for d in dependents)
            raise ValueError(
                f"{node.title!r} still has {len(dependents)} dependent(s): {listing}. "
                f"Drop those edges ({kind}_dep_remove) or pass force=True — forcing "
                f"leaves them tainted, pointing at a missing dep")
        res = await self.notestore.delete(proj, node.relpath)
        return {**res, "id": node.id, "title": node.title,
                "dependents": dependents, "forced": bool(dependents)}

    # ── design verbs ──────────────────────────────────────────────────────────

    async def design_add(self, proj: str, title: str, content: str,
                         deps: list[str] | None = None) -> dict[str, Any]:
        """Record a design decision under `notes/design/`, `checked` seeded from
        the current dep hashes (so a new decision is born verified)."""
        return await self._add(proj, "design", title, content, deps, {})

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
        out = {"project": proj, **node.brief(), "body": note.body.strip(),
               "deps": [self._annotate(graph, tainted, d) for d in node.deps],
               "dependents": [self._annotate(graph, tainted, d)
                              for d in graph.dependents.get(node.id, [])],
               "tainted": node.id in tainted,
               "reasons": info.get("reasons", []), "causes": info.get("causes", []),
               "paths": info.get("paths", [])}
        if node.id in tainted:
            out["next"] = self._next(node)
        return out

    async def _write_body(self, proj: str, ref: str,
                          rewrite: Any) -> dict[str, Any]:
        """The edge-aware write path shared by `design_edit`/`design_append`:
        snapshot the taint state, write the new body through the locked index
        path, then diff — so the answer to "I changed this" is "…and here is what
        that just put out of date", computed against the PRE-edit state.

        Hash-taint remains the safety net for a raw file edit; this is the
        encouraged path because only it can name the consequences in the same
        breath as the change."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "design")
        before = set(self._taint(graph))
        note = self._note(proj, node)
        out = await self._save(proj, node, {}, body=rewrite(note.body))
        after_graph = self._load_graph(proj)
        after = self._taint(after_graph)
        newly = []
        for nid in sorted(set(after) - before):
            hit = after_graph.nodes[nid]
            newly.append({"id": nid, "title": hit.title, "relpath": hit.relpath,
                          "kind": hit.kind,
                          "via": [" → ".join(p["chain"]) for p in after[nid]["paths"]],
                          "next": self._next(hit) if hit.kind == "design" else None})
        res = {**out, "newly_tainted": newly}
        if newly:
            res["next"] = (
                f"{len(newly)} dependent(s) now read as out of date with this — "
                f"`design_read <ref>` each, then `design_reaffirm <ref>` where it "
                f"still holds")
        return res

    async def design_edit(self, proj: str, ref: str,
                          new_content: str) -> dict[str, Any]:
        """Replace a decision's body through the facet, answering with the
        dependents the change just tainted."""
        return await self._write_body(proj, ref,
                                      lambda _: (new_content or "").strip() + "\n")

    async def design_append(self, proj: str, ref: str,
                            content: str) -> dict[str, Any]:
        """Extend a decision's body through the facet, answering with the
        dependents the change just tainted."""
        return await self._write_body(
            proj, ref,
            lambda body: body.rstrip() + "\n\n" + (content or "").strip() + "\n")

    def design_list(self, proj: str, tainted: bool = False) -> dict[str, Any]:
        """Every decision as a flat table — title, ref, status, taint flag, edge
        counts. The inventory read; `design_tree` is the shape read."""
        graph = self._load_graph(proj)
        stale = self._taint(graph)
        rows = [{"id": n.id, "title": n.title, "relpath": n.relpath,
                 "status": n.status, "updated": n.updated,
                 "tainted": n.id in stale,
                 "deps": len(n.deps),
                 "dependents": len(graph.dependents.get(n.id, []))}
                for n in sorted(graph.of_kind("design"), key=lambda n: n.title)]
        total, n_tainted = len(rows), sum(1 for r in rows if r["tainted"])
        if tainted:
            rows = [r for r in rows if r["tainted"]]
        return {"project": proj, "designs": rows, "total": total,
                "tainted": n_tainted, "filtered": bool(tainted),
                "cycles": [[graph.nodes[c].title for c in cyc]
                           for cyc in _cycles(graph.nodes)]}

    async def design_dep_add(self, proj: str, ref: str, dep_ref: str) -> dict[str, Any]:
        return await self._dep_add(proj, "design", ref, dep_ref)

    async def design_dep_remove(self, proj: str, ref: str,
                                dep_ref: str) -> dict[str, Any]:
        return await self._dep_remove(proj, "design", ref, dep_ref)

    async def design_forget(self, proj: str, ref: str,
                            force: bool = False) -> dict[str, Any]:
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
            rows.append({**node.brief(), **info, "next": self._next(node)})
        rows.sort(key=lambda r: r["title"])
        cycles = [[graph.nodes[c].title for c in cyc] for cyc in _cycles(graph.nodes)]
        designs = graph.of_kind("design")
        return {"project": proj, "designs": len(designs), "tainted": rows,
                "clean": not rows, "cycles": cycles}

    async def design_reaffirm(self, proj: str, ref: str) -> dict[str, Any]:
        """Re-record a decision's dep hashes after a human re-read it — the ONLY
        thing that clears taint (short of editing the decision itself).

        Named like `learning_reaffirm` and for the same reason: this is a
        re-blessing against drift, not a proof. Taint is coarse — a dep moved —
        so the common outcome of reading a tainted decision is that it still
        holds and this is a one-line, cheap confirmation, NOT error recovery."""
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "design")
        checked, missing = {}, []
        for dep_id in node.deps:
            dep = graph.nodes.get(dep_id)
            if dep is None:
                missing.append(dep_id)
            else:
                checked[dep_id] = dep.body_hash
        out = await self._save(proj, node, {"checked": checked})
        return {**out, "verified": sorted(checked), "missing": missing}

    def design_tree(self, proj: str, ref: str | None = None,
                    direction: str = "deps", depth: int = 6) -> dict[str, Any]:
        """The dependency tree around a decision — `deps` (what it builds on) or
        `dependents` (what builds on it) — every node taint-flagged."""
        if direction not in ("deps", "dependents"):
            raise ValueError(
                f"unknown direction {direction!r}: use 'deps' (what this builds on) "
                f"or 'dependents' (what builds on this)")
        graph = self._load_graph(proj)
        tainted = self._taint(graph)
        # Without a ref: render every root of the chosen direction — the nodes
        # nothing points at *along the edges being followed*, so walking down from
        # them reaches the whole forest exactly once.
        roots = ([self._resolve_ref(graph, ref, "design")] if ref else
                 sorted((n for n in graph.of_kind("design")
                         if not (graph.dependents.get(n.id) if direction == "deps"
                                 else n.deps)),
                        key=lambda n: n.title))

        def build(node: Node, seen: frozenset[str], level: int) -> dict[str, Any]:
            edges = (node.deps if direction == "deps"
                     else graph.dependents.get(node.id, []))
            out: dict[str, Any] = {**node.brief(),
                                   "tainted": node.id in tainted, "children": []}
            if node.id in seen:                 # DAG: shown already, don't re-expand
                out["repeat"] = True
                return out
            if level >= depth:
                return out
            for eid in edges:
                child = graph.nodes.get(eid)
                if child is None:               # dangling id: a warning, not a crash
                    out["children"].append({"id": eid, "title": f"{eid} (missing)",
                                            "missing": True, "children": []})
                    continue
                out["children"].append(build(child, seen | {node.id}, level + 1))
            return out

        return {"project": proj, "direction": direction,
                "roots": [build(r, frozenset(), 0) for r in roots]}

    async def design_supersede(self, proj: str, ref: str,
                               by_ref: str | None = None) -> dict[str, Any]:
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
            raise ValueError("a decision cannot supersede itself")
        note = self._note(proj, node)
        marker = (f"\n> **Superseded** {_today()}"
                  + (f" by {by.title!r} ({by.relpath})." if by else ".")
                  + " Dependents are tainted until re-verified.\n")
        fm: dict[str, Any] = {"status": "superseded"}
        if by:
            fm["superseded_by"] = by.id
        out = await self._save(proj, node, fm, body=note.body.rstrip() + "\n" + marker)
        dependents = [graph.nodes[d].brief() for d in graph.dependents.get(node.id, [])]
        return {**out, "status": "superseded",
                "superseded_by": by.id if by else None,
                "tainted_dependents": dependents}

    # ── plan verbs ────────────────────────────────────────────────────────────

    def _rank_for(self, graph: Graph, after: Node | None, before: Node | None,
                  exclude: str | None = None) -> str:
        """The rank for an item placed after/before given neighbours (either or
        both may be None — none at all means the end of the list)."""
        items = sorted((n for n in graph.of_kind("plan") if n.id != exclude),
                       key=lambda n: (n.rank, n.title))
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

    async def plan_add(self, proj: str, title: str | None = None, content: str = "",
                       deps: list[str] | None = None, after: str | None = None,
                       before: str | None = None,
                       items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Add plan items (default: at the end; `after`/`before` place them).

        Takes ONE item (`title`/`content`/`deps`) or a BATCH (`items`), because
        planning happens in batches: writing five items is one thought, and five
        calls is five chances to lose the thread. Batch items land contiguously in
        the order given, and may depend on each other by 1-based batch position
        (`"#1"`) as well as by any existing ref — the ordering constraint you
        actually mean is usually to the item you just wrote, which has no id yet.

        A plan item's BODY IS OPTIONAL: a title is a legitimate whole item
        ("wire up the emitter"). A design decision's body is not optional — there
        the rationale IS the artifact."""
        batch = (items if items is not None
                 else [{"title": title, "content": content, "deps": deps}])
        if not batch:
            raise ValueError("plan_add needs a title, or items=[…]")
        rows: list[dict[str, Any]] = []
        by_index: dict[str, str] = {}       # "#1" → the id it created
        prev: str | None = None
        for i, item in enumerate(batch, 1):
            if not isinstance(item, dict):  # a bare string is the obvious shorthand
                item = {"title": str(item)}
            raw_deps = item.get("deps") if item.get("deps") is not None else []
            # "#n" resolves against THIS batch; anything else is an ordinary ref
            item_deps = [by_index[d] if d in by_index else d for d in raw_deps]
            unknown = [d for d in raw_deps
                       if d.startswith("#") and d not in by_index]
            if unknown:
                raise ValueError(
                    f"item {i} depends on {unknown[0]!r}, which is not an EARLIER "
                    f"item in this batch (use #1…#{i - 1}, or an existing ref) — "
                    f"a batch dep can only point backwards")
            graph = self._load_graph(proj)
            a = (self._resolve_ref(graph, prev, "plan") if prev
                 else self._resolve_ref(graph, after, "plan") if after else None)
            b = (self._resolve_ref(graph, before, "plan")
                 if before and not prev else None)
            rank = self._rank_for(graph, a, b)
            out = await self._add(proj, "plan", str(item.get("title") or ""),
                                  str(item.get("content") or ""), item_deps,
                                  {"rank": rank})
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
        are the plan's opinion, not its jailer."""
        if status not in PLAN_STATUSES:
            raise ValueError(
                f"unknown status {status!r}: use one of {', '.join(PLAN_STATUSES)} "
                f"('blocked' is derived from deps, never set)")
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "plan")
        warnings = []
        if status in DONE_STATUSES:
            open_deps = [graph.nodes[d].title for d in node.deps
                         if d in graph.nodes and graph.nodes[d].kind == "plan"
                         and graph.nodes[d].status not in DONE_STATUSES]
            if open_deps:
                warnings.append(
                    f"marked {status} while {len(open_deps)} dep(s) are unfinished: "
                    + ", ".join(repr(t) for t in open_deps))
        # what was blocked BEFORE — so `unblocked` names only what this call freed,
        # not everything that happens to be ready now
        was_blocked = {r["id"] for r in self._rows(proj, graph, graph.of_kind("plan"))
                       if r["blocked"]}
        out = await self._save(proj, node, {"status": status})
        unblocked = []
        if status in DONE_STATUSES:
            after = self._load_graph(proj)
            for row in self._rows(proj, after, after.of_kind("plan")):
                if (row["id"] in was_blocked and not row["blocked"]
                        and row["status"] not in DONE_STATUSES):
                    unblocked.append({"id": row["id"], "ref": row["relpath"],
                                      "title": row["title"],
                                      "status": row["status"]})
        return {**out, "status": status, "warnings": warnings,
                "unblocked": unblocked}

    async def plan_dep_add(self, proj: str, ref: str, dep_ref: str) -> dict[str, Any]:
        return await self._dep_add(proj, "plan", ref, dep_ref)

    async def plan_dep_remove(self, proj: str, ref: str, dep_ref: str) -> dict[str, Any]:
        return await self._dep_remove(proj, "plan", ref, dep_ref)

    async def plan_forget(self, proj: str, ref: str,
                          force: bool = False) -> dict[str, Any]:
        return await self._forget(proj, "plan", ref, force)

    async def plan_move(self, proj: str, ref: str, after: str | None = None,
                        before: str | None = None) -> dict[str, Any]:
        """Re-rank an item. Deps are NOT touched: order is preference, deps are
        correctness (decision 5), so moving can never break the plan."""
        if not (after or before):
            raise ValueError("plan_move needs after=<ref> and/or before=<ref>")
        graph = self._load_graph(proj)
        node = self._resolve_ref(graph, ref, "plan")
        a = self._resolve_ref(graph, after, "plan") if after else None
        b = self._resolve_ref(graph, before, "plan") if before else None
        if node.id in {n.id for n in (a, b) if n}:
            raise ValueError(f"{node.title!r} cannot be placed relative to itself")
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
            ready = sorted((plans[nid] for nid, d in pending.items() if not d),
                           key=lambda n: (n.rank, n.title))
            if not ready:
                break
            node = ready[0]
            order.append(node)
            pending.pop(node.id)
            for rest in pending.values():
                rest.discard(node.id)
        cycles = [[graph.nodes[c].title for c in cyc] for cyc in _cycles(graph.nodes)]
        if pending:                             # cycle survivors, deterministic order
            order += sorted((plans[nid] for nid in pending),
                            key=lambda n: (n.rank, n.title))
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
        in `_row`). Resolves the three dep kinds once for the whole listing."""
        tainted = self._taint(graph)
        unresolved = {d for n in nodes for d in n.deps if d not in graph.nodes}
        note_ids = self._note_dep_ids(proj, unresolved) if unresolved else set()
        return [self._row(graph, n, tainted, note_ids) for n in nodes]

    def _row(self, graph: Graph, node: Node, tainted: dict[str, Any],
             note_ids: set[str]) -> dict[str, Any]:
        """One plan item + its derived state, under the MIXED-DEP RULE:

        - a **plan** dep blocks until it is `done`/`verified` — it is work that
          must happen first;
        - a **design** dep blocks while it is TAINTED and only then — an untainted
          decision is stable ground to build on, a tainted one means the ground
          moved and the item would be built against a decision nobody has
          re-read;
        - a plain **note** dep NEVER blocks. It is a reference, not a gate.

        A dep id that resolves to nothing at all is neither: it is reported as
        `missing_deps` (visible, not blocking) — a dangling id must not silently
        wedge the plan."""
        blockers, note_deps, missing = [], [], []
        for dep_id in node.deps:
            dep = graph.nodes.get(dep_id)
            if dep is None:
                (note_deps if dep_id in note_ids else missing).append(dep_id)
            elif dep.kind == "plan":
                if dep.status not in DONE_STATUSES:
                    blockers.append({"id": dep.id, "ref": dep.relpath,
                                     "title": dep.title, "kind": "plan",
                                     "status": dep.status})
            elif dep.id in tainted:
                blockers.append({"id": dep.id, "ref": dep.relpath,
                                 "title": dep.title, "kind": "design",
                                 "status": "stale — `design_read` it"})
        return {**node.brief(), "blocked": bool(blockers), "blocked_by": blockers,
                "note_deps": note_deps, "missing_deps": missing,
                "group": _group(node, bool(blockers))}

    def plan_list(self, proj: str, all: bool = False) -> dict[str, Any]:
        """The plan as a WORKING SET, not a graph dump: in-progress first, then
        ready, then blocked (each naming what it waits on), with finished work
        hidden unless `all`.

        Topological + rank order still holds WITHIN each group — the grouping only
        answers the question actually being asked ("what am I on, what can I pick
        up, what can't I") ahead of the question the raw graph answers."""
        graph = self._load_graph(proj)
        order, cycles = self._ordered(graph)
        rows = self._rows(proj, graph,
                          [n for n in order if all or n.status not in DONE_STATUSES])
        rows.sort(key=lambda r: _GROUPS.index(r["group"]))   # stable: topo+rank kept
        groups: dict[str, int] = {}
        for row in rows:
            groups[row["group"]] = groups.get(row["group"], 0) + 1
        return {"project": proj, "items": rows, "total": len(order),
                "groups": groups, "hidden": len(order) - len(rows),
                "cycles": cycles}

    def plan_next(self, proj: str, k: int = 5) -> dict[str, Any]:
        """What is actually actionable now: `todo` items nothing blocks, in rank
        order. The 'what do I do next' read at the start of a session.

        Blocking is the mixed-dep rule in `_row`: plan deps until done, design
        deps while tainted, note deps never.

        IN-PROGRESS ITEMS ARE EXCLUDED, deliberately: `in-progress` means CLAIMED,
        and several agents may read this plan at once. Taking an item means
        marking it, which is what makes the claim visible to everyone else."""
        graph = self._load_graph(proj)
        order, _ = self._ordered(graph)
        rows = [r for r in self._rows(proj, graph,
                                      [n for n in order if n.status == "todo"])
                if not r["blocked"]]
        for row in rows:
            row["next"] = (
                f"starting it? `plan_status {row['relpath']} in-progress` (that is "
                f"the claim other agents read); finished? `plan_status "
                f"{row['relpath']} done` — the result names what you unblocked")
        return {"project": proj, "items": rows[:max(1, k)], "ready": len(rows),
                "claimed": sum(1 for n in graph.of_kind("plan")
                               if n.status == "in-progress")}
