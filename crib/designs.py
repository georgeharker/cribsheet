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

Staleness is COMPUTED ON READ and RECORDED ON VERIFY: each node keeps
`checked: {dep_id: <body hash at last verify>}`, and `*_check` compares that
against the deps' current hashes. Nothing propagates on write — which is exactly
why a design edited by a plain `note_edit` (or by hand, or by a git pull) is
caught all the same. `Designs` takes an explicit resolved `project`, like
`Learnings`; Crib keeps resolve_project + delegate.
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

    def brief(self) -> dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "relpath": self.relpath,
               "title": self.title, "status": self.status, "deps": list(self.deps)}
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
                    body_hash=_body_hash(body), frontmatter=fm)
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

    def _direct_taint(self, graph: Graph) -> dict[str, list[str]]:
        """Per node, why its OWN deps are out of date (decision 3): a dep that is
        gone, one never verified since it was added, or one whose body changed."""
        out: dict[str, list[str]] = {}
        for node in graph.nodes.values():
            for dep_id in node.deps:
                dep = graph.nodes.get(dep_id)
                if dep is None:
                    out.setdefault(node.id, []).append(
                        f"dep {dep_id} is missing (deleted, or never existed)")
                elif dep_id not in node.checked:
                    out.setdefault(node.id, []).append(
                        f"{dep.title!r} was added as a dep but never verified here")
                elif node.checked[dep_id] != dep.body_hash:
                    what = ("was superseded" if dep.status == "superseded"
                            else "changed")
                    out.setdefault(node.id, []).append(
                        f"{dep.title!r} {what} since this was last verified")
        return out

    def _taint(self, graph: Graph) -> dict[str, dict[str, Any]]:
        """{id: {reasons, paths}} for every tainted node — direct causes plus
        transitive reachability over tainted edges. `paths` spells out the chain
        (`X → Y → Z`, Z being what actually changed) so `check` can say *why*."""
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
                    "cause": reason}
                   for reason in direct.get(nid, [])]
            for dep_id in node.deps:
                if dep_id in tainted and dep_id in graph.nodes \
                        and dep_id not in stack and dep_id != nid:
                    out += chains(dep_id, [*stack, nid])
            return out

        result: dict[str, dict[str, Any]] = {}
        for nid in tainted:
            paths = chains(nid, [])
            reasons = direct.get(nid) or sorted(
                {f"depends on {p['chain'][1]!r}, which is tainted"
                 for p in paths if len(p["chain"]) > 1})
            result[nid] = {"reasons": reasons, "paths": paths}
        return result

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
        the current dep hashes."""
        return await self._add(proj, "design", title, content, deps, {})

    async def design_dep_add(self, proj: str, ref: str, dep_ref: str) -> dict[str, Any]:
        return await self._dep_add(proj, "design", ref, dep_ref)

    async def design_dep_remove(self, proj: str, ref: str,
                                dep_ref: str) -> dict[str, Any]:
        return await self._dep_remove(proj, "design", ref, dep_ref)

    async def design_forget(self, proj: str, ref: str,
                            force: bool = False) -> dict[str, Any]:
        return await self._forget(proj, "design", ref, force)

    def design_check(self, proj: str, ref: str | None = None) -> dict[str, Any]:
        """Which decisions are out of date with what they build on, and why."""
        graph = self._load_graph(proj)
        tainted = self._taint(graph)
        only = self._resolve_ref(graph, ref, "design").id if ref else None
        rows = []
        for nid, info in tainted.items():
            node = graph.nodes[nid]
            if node.kind != "design" or (only and nid != only):
                continue
            rows.append({**node.brief(), **info})
        rows.sort(key=lambda r: r["title"])
        cycles = [[graph.nodes[c].title for c in cyc] for cyc in _cycles(graph.nodes)]
        designs = graph.of_kind("design")
        return {"project": proj, "designs": len(designs), "tainted": rows,
                "clean": not rows, "cycles": cycles}

    async def design_verify(self, proj: str, ref: str) -> dict[str, Any]:
        """Re-record a decision's dep hashes after a human re-read it — the ONLY
        thing that clears taint (short of editing the decision itself)."""
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

    async def plan_add(self, proj: str, title: str, content: str,
                       deps: list[str] | None = None, after: str | None = None,
                       before: str | None = None) -> dict[str, Any]:
        """Add a plan item (default: at the end; `after`/`before` place it)."""
        graph = self._load_graph(proj)
        rank = self._rank_for(graph,
                              self._resolve_ref(graph, after, "plan") if after else None,
                              self._resolve_ref(graph, before, "plan") if before else None)
        out = await self._add(proj, "plan", title, content, deps, {"rank": rank})
        return {**out, "rank": rank}

    async def plan_status(self, proj: str, ref: str, status: str) -> dict[str, Any]:
        """Set an item's status. Marking `done` with unfinished deps WARNS rather
        than blocks — the deps are the plan's opinion, not its jailer."""
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
        out = await self._save(proj, node, {"status": status})
        return {**out, "status": status, "warnings": warnings}

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

    def _row(self, graph: Graph, node: Node) -> dict[str, Any]:
        blockers = [graph.nodes[d].title for d in node.deps
                    if d in graph.nodes and graph.nodes[d].kind == "plan"
                    and graph.nodes[d].status not in DONE_STATUSES]
        missing = [d for d in node.deps if d not in graph.nodes]
        return {**node.brief(), "blocked": bool(blockers), "blocked_by": blockers,
                "missing_deps": missing}

    def plan_list(self, proj: str, all: bool = False) -> dict[str, Any]:
        """The plan in execution order (topological, rank-tie-broken), each item
        carrying its DERIVED `blocked`. Hides finished items unless `all`."""
        graph = self._load_graph(proj)
        order, cycles = self._ordered(graph)
        rows = [self._row(graph, n) for n in order
                if all or n.status not in DONE_STATUSES]
        return {"project": proj, "items": rows, "total": len(order),
                "hidden": len(order) - len(rows), "cycles": cycles}

    def plan_next(self, proj: str, k: int = 5) -> dict[str, Any]:
        """What is actually actionable now: `todo` items whose deps are all done,
        in rank order. The 'what do I do next' read."""
        graph = self._load_graph(proj)
        order, _ = self._ordered(graph)
        rows = [r for r in (self._row(graph, n) for n in order
                            if n.status == "todo") if not r["blocked"]]
        return {"project": proj, "items": rows[:max(1, k)], "ready": len(rows)}
