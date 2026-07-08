"""The project-layer surface the code indexer depends on — one narrow seam.

The indexing pipeline needs a handful of things from the project layer: how to
resolve a project name, the cross-project ref attribution context, the ref list, the
code-file enumeration, and source-root registration. `ProjectServices` is exactly
that surface. It DEFERS to the project *loader* (Crib) for the implementations for
now, so the pipeline can be extracted into a `CodeIndexer` that talks to THIS
interface instead of reaching into the Crib god object — and later the refs /
enumeration logic can move behind this seam without touching the indexer.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import Crib
    from .codeindex import RefProjects
    from .codestore import CodeStore
    from .config import Config
    from .paths import Paths


class ProjectServices:
    """Project-layer services for the code indexer, backed by the loader (Crib)."""

    def __init__(self, loader: Crib) -> None:
        self._loader = loader

    # shared dependencies the indexer also needs, surfaced so it holds only `services`
    @property
    def paths(self) -> Paths:
        return self._loader.paths

    @property
    def config(self) -> Config:
        return self._loader.config

    @property
    def code(self) -> CodeStore:
        return self._loader.code

    # project-layer operations (defer to the loader; move behind this seam later)
    def resolve_project(self, project: str | None, cwd: Path | None = None) -> str:
        return self._loader.resolve_project(project, cwd)

    def ref_edge_ctx(self, proj: str, root: Path | None = None) -> RefProjects:
        return self._loader._ref_edge_ctx(proj, root)

    def project_refs(self, proj: str) -> list[dict[str, Any]]:
        return self._loader._project_refs(proj)

    def enumerate_code_files(self, root: Path, globs: list[str]) -> list[Path]:
        return self._loader._enumerate_code_files(root, globs)

    def register_code_root(self, proj: str, root: str | Path) -> None:
        self._loader._register_code_root(proj, root)
