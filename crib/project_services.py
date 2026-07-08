"""The project-layer surface the code indexer depends on — narrow, explicit deps.

The indexing pipeline needs a handful of project-layer operations: resolve a project
name, the cross-project ref attribution context + ref list, code-file enumeration, and
source-root registration. `ProjectServices` bundles exactly those, depending on the
collaborators that own each — `Refs` (ref context + list) and `CodeStore` — plus two
injected callables the composition root provides (`enumerate`/`register`: the code
enumeration and watcher concerns that don't have their own object). It holds NO
reference to the Crib god object; the dependency flows down to collaborators, not up.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .config import resolve_project as _resolve_project

if TYPE_CHECKING:
    from .codeindex import RefProjects
    from .codestore import CodeStore
    from .config import Config
    from .paths import Paths
    from .refs import Refs


class ProjectServices:
    def __init__(self, refs: Refs, code: CodeStore, paths: Paths, config: Config,
                 enumerate_code_files: Callable[[Path, list[str]], list[Path]],
                 register_code_root: Callable[[str, str | Path], None]) -> None:
        self.refs = refs
        self.code = code
        self.paths = paths
        self.config = config
        self._enumerate = enumerate_code_files
        self._register = register_code_root

    def resolve_project(self, project: str | None, cwd: Path | None = None) -> str:
        return _resolve_project(self.config, project, cwd)

    def ref_edge_ctx(self, proj: str, root: Path | None = None) -> RefProjects:
        return self.refs.ref_edge_ctx(proj, root)

    def project_refs(self, proj: str) -> list[dict[str, Any]]:
        return self.refs.project_refs(proj)

    def enumerate_code_files(self, root: Path, globs: list[str]) -> list[Path]:
        return self._enumerate(root, globs)

    def register_code_root(self, proj: str, root: str | Path) -> None:
        self._register(proj, root)
