"""Cross-project references (`.crib` `refs:`), extracted from Crib.

`Refs` resolves a project's `refs:` to their local checkouts, builds the
cross-project edge-attribution context the indexer uses, and resolves a symbol
against refs on a local miss. It depends only on `paths` + two injected Crib
callables it can't own: `resident` (a ref project's resident cache, which carries
the pipeline-coupled revalidate hook) and `nested_roots` (boundary detection, shared
with code enumeration). Crib keeps thin delegators so query methods, learnings, and
the ProjectServices seam call refs unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .config import CribLink

if TYPE_CHECKING:
    from .codestore import _ResidentCode
    from .codeindex import RefProjects
    from .paths import Paths


class Refs:
    def __init__(self, paths: Paths,
                 resident: Callable[[str], _ResidentCode],
                 nested_roots: Callable[[Path], list[Path]]) -> None:
        self.paths = paths
        self._resident = resident            # (proj) → _ResidentCode  (Crib-owned)
        self._nested_roots = nested_roots    # (root) → [nested .crib dirs]  (Crib-owned)

    def project_refs(self, proj: str) -> list[dict[str, Any]]:
        """The projects `proj`'s `.crib` names in `refs:` — cross-project xref
        targets. Each ref resolves to its own LOCAL checkout via that project's
        `.source_root` (machine-local, gitignored), so the committed `.crib`
        carries NAMES only and survives differently-located clones. → [{project,
        root (None when not locally present), indexed}]."""
        from .codeindex import SymbolIndex
        my_root = SymbolIndex(self.paths.project_dir(proj)).source_root()
        link = (CribLink.find(my_root)
                if my_root is not None and my_root.exists() else None)
        out: list[dict[str, Any]] = []
        for name in (link.refs if link else []):
            if name == proj:
                continue                     # self-reference is meaningless
            si = SymbolIndex(self.paths.project_dir(name))
            root = si.source_root()
            out.append({"project": name,
                        "root": root if root is not None and root.exists() else None,
                        "indexed": si.is_populated()})
        return out

    def ref_edge_ctx(self, proj: str, root: Path | None = None) -> RefProjects:
        """`extract_file`'s cross-project attribution context: each `.crib` ref's
        locally-resolved root (pre-resolved) + its indexed file set (for the
        site-packages suffix match). When `root` is given, an IN-TREE checkout of
        a ref'd project (nested `.crib` naming it — e.g. `vendor/llmkit`) is added
        as an extra attribution root for that ref: same repo, same relative paths,
        so edges into the vendored copy resolve to the ref project. Reads the
        refs' resident caches — cheap after first touch."""
        out: RefProjects = []
        nested_by_proj: dict[str, Path] = {}
        if root is not None:
            for nd in self._nested_roots(root):
                link = CribLink.find(nd)
                if link is not None:
                    nested_by_proj[link.project] = nd.resolve()
            root = root.resolve()
        for ref in self.project_refs(proj):
            files: frozenset[str] = frozenset()
            if ref["indexed"]:
                try:
                    files = frozenset(
                        e.get("file", "")
                        for e in self._resident(ref["project"]).entries)
                except Exception:  # noqa: BLE001 — a broken ref never fails indexing
                    pass
            rroot = ref["root"].resolve() if ref["root"] else None
            out.append((ref["project"], rroot, files))
            nested = nested_by_proj.get(ref["project"])
            if nested is not None and nested != rroot:
                out.append((ref["project"], nested, files))
        return out

    def resolve_symbol(self, proj: str, symbol: str,
                       rc: _ResidentCode | None = None) -> dict[str, Any]:
        """Resolve a user-supplied symbol to exactly one indexed entry. Exact fqn
        wins; a bare/partial name resolves only if unique — else raise, listing
        candidates (never silently pick, so a learning can't land on the wrong one).
        Resolves against a resident cache when one is passed (avoids a disk read)."""
        from .codeindex import SymbolIndex
        matches = (rc.by_fqname(symbol) if rc is not None
                   else SymbolIndex(self.paths.project_dir(proj)).by_fqname(symbol))
        if not matches:
            raise ValueError(f"unknown symbol {symbol!r} in project {proj!r} — "
                             f"code_lookup it, or code_index the file first")
        exact = [m for m in matches if m.get("fqname") == symbol]
        cands = exact or matches
        if len(cands) > 1:
            names = ", ".join(sorted(m.get("fqname", "") for m in cands)[:8])
            raise ValueError(f"ambiguous symbol {symbol!r} → {names}; pass a full fqname")
        return cands[0]

    def resolve_symbol_or_ref(self, proj: str, symbol: str,
                              rc: _ResidentCode | None = None,
                              ) -> tuple[str, dict[str, Any]]:
        """Resolve locally first; on a LOCAL MISS, try the project's `.crib`
        `refs:` (cross-project xref) → (owning_project, entry). Exactly one ref
        matching wins; several → ambiguous error naming the projects; none →
        the local unknown-symbol error. A local AMBIGUITY still raises (refs
        never paper over it)."""
        try:
            return proj, self.resolve_symbol(proj, symbol, rc)
        except ValueError as local_err:
            if "unknown symbol" not in str(local_err):
                raise
            found: list[tuple[str, dict[str, Any]]] = []
            for ref in self.project_refs(proj):
                if not ref["indexed"]:
                    continue
                try:
                    found.append((ref["project"],
                                  self.resolve_symbol(ref["project"], symbol)))
                except ValueError:
                    continue
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                names = ", ".join(f"{p}:{e['fqname']}" for p, e in found)
                raise ValueError(
                    f"ambiguous symbol {symbol!r} across refs → {names}; "
                    f"pass project= to disambiguate") from None
            raise local_err
