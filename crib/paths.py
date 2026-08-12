"""XDG + CRIB_* path resolution (DESIGN §2), plus the containment rules that keep
every derived path inside its store.

Three lifecycles, three roots:
  config  -> CRIB_CONFIG_DIR | $XDG_CONFIG_HOME/crib | ~/.config/crib
  data    -> CRIB_DATA_DIR   | $XDG_DATA_HOME/crib   | ~/.local/share/crib
  index   -> CRIB_INDEX_DIR  | $XDG_CACHE_HOME/crib  | ~/.cache/crib
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import CribUserError

if TYPE_CHECKING:
    from .config import Config

_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def check_project_name(project: str) -> str:
    """Validate a project name — ONE path-safe segment, never a path.

    A project name is used verbatim as a directory name under `projects/`, and it
    reaches us from tool arguments and `.crib` files, so `../x` (or an absolute
    path) would otherwise plant a whole namespace outside the data dir. Returns
    the name so it can be used inline."""
    if project in (".", "..") or not _PROJECT_RE.match(project or ""):
        raise CribUserError(
            f"invalid project name {project!r}: use letters, digits, '.', '_' or "
            "'-' only — a project is a name, not a path")
    return project


def check_relpath(relpath: str, base: Path | str = "the store") -> str:
    """Reject a relpath that would step outside `base` — absolute, or with a `..`
    segment. Absolute is the sharp one: `base / "/etc/passwd"` silently discards
    the base entirely. Rejecting both by NAME (rather than only after joining)
    gives an error that names the offending value, and doesn't depend on where the
    store's symlinks happen to point. Returns the relpath so it can be used
    inline."""
    if not relpath:
        raise CribUserError(f"empty path: expected a path relative to {base}")
    p = Path(relpath)
    if p.is_absolute():
        raise CribUserError(
            f"absolute path not allowed: {relpath!r} — pass a path relative to {base}")
    if ".." in p.parts:
        raise CribUserError(f"path escapes {base}: {relpath!r} — no '..' segments")
    return relpath


def confine(base: Path, *parts: str) -> Path:
    """`base` joined with `parts`, refusing anything that lands outside `base`.

    Every relpath crib joins onto a store directory arrives as a tool argument, so
    containment is checked, not assumed: the parts are screened by name
    (`check_relpath`) and the joined result is re-checked once resolved, which
    also catches an escape through a symlink planted inside the tree."""
    for part in parts:
        check_relpath(part, base)
    out = base.joinpath(*parts)
    if not out.resolve().is_relative_to(base.resolve()):
        raise CribUserError(f"path escapes {base}: {'/'.join(parts)!r}")
    return out


def _resolve(env_override: str, xdg_var: str, xdg_default: str) -> Path:
    if v := os.environ.get(env_override):
        return Path(v).expanduser()
    if v := os.environ.get(xdg_var):
        return Path(v).expanduser() / "crib"
    return Path.home() / xdg_default / "crib"


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    index_dir: Path

    @classmethod
    def resolve(cls) -> Paths:
        return cls(
            config_dir=_resolve("CRIB_CONFIG_DIR", "XDG_CONFIG_HOME", ".config"),
            data_dir=_resolve("CRIB_DATA_DIR", "XDG_DATA_HOME", ".local/share"),
            index_dir=_resolve("CRIB_INDEX_DIR", "XDG_CACHE_HOME", ".cache"),
        )

    # --- derived locations -------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def versions_dir(self) -> Path:
        return self.data_dir / ".versions"

    @property
    def chroma_dir(self) -> Path:
        return self.index_dir / "chroma"

    def project_dir(self, project: str) -> Path:
        """The project's GLOBAL dir under `projects/`.

        Always machine-local, even for a project whose DATA tier lives in a repo
        (`store:` in `.crib`): the derived index tiers (`symbol_index/`,
        `keyword_index/`, `summary_index/`, `doc-sources.json`) and the
        `.cribproject` stub stay here by design, so `rm -rf $CRIB_INDEX_DIR` +
        reindex stays the universal recovery path and embeddings can never be
        committed to someone's repo. The DATA tier (notes + their version ring)
        is the part that may move, and it must be resolved through
        `resolve_project_paths` rather than derived from here."""
        return self.projects_dir / check_project_name(project)

    def notes_dir(self, project: str) -> Path:
        """The GLOBAL notes dir for a project. Callers that must honour an
        adopted in-repo store go through `resolve_project_paths` (or
        `ProjectPathResolver`) instead — this is the global-layout half of what
        that resolves to."""
        return self.project_dir(project) / "notes"

    def ensure(self) -> Paths:
        for d in (self.config_dir, self.data_dir, self.projects_dir,
                  self.versions_dir, self.index_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


# ── Where a project's DATA tier actually lives (docs/plans/repo-local-storage) ──
# A project's notes are EITHER global (`projects/<name>/notes`, ring shared at
# `data_dir/.versions`) OR in a repo subdir declared by that repo's `.crib`
# `store:` and recorded machine-locally in the global stub's `.cribproject`
# `store_root` as a portable `$LOCATION` token. Never both — adoption is a move,
# not an overlay. Only this pair of tiers moves; see `Paths.project_dir`.

@dataclass(frozen=True)
class ProjectPaths:
    """One project's resolved storage locations."""
    project: str
    project_dir: Path                   # global `projects/<name>` (stub or full)
    notes_dir: Path
    versions_dir: Path
    data_root: Path                     # dir holding the pillar stores (notes/,
                                        # design/, plans/, learnings/): the global
                                        # project dir, or the adopted store root
    store_root: Path | None = None      # the in-repo store dir, when adopted
    store_token: str | None = None      # its portable `$LOCATION/...` spelling
    available: bool = True              # False ⇒ store_root isn't on this machine

    @property
    def in_repo(self) -> bool:
        return self.store_root is not None

    def pillar_dir(self, segment: str) -> Path:
        """A pillar store's directory: `data_root/<segment>`. Every pillar —
        notes included (`pillar_dir("notes") == notes_dir`) — is a sibling under
        the data root, in both layouts."""
        return self.data_root / segment

    @property
    def config_file(self) -> Path:
        """The `.cribproject` — always the global one, stub or not."""
        return self.project_dir / ".cribproject"

    def require(self) -> "ProjectPaths":
        """Self, or an actionable error when the store isn't on this machine.

        Every read/write verb funnels through here, so a project whose repo isn't
        cloned (or whose `[locations]` name this machine doesn't know) answers
        with the three things that fix it rather than a bare FileNotFoundError
        from somewhere deep in the note path."""
        if self.available:
            return self
        raise CribUserError(
            f"project {self.project!r} keeps its notes in a repo at "
            f"{self.store_token} ({self.store_root}), which is not on this "
            f"machine: clone the repo there, add a [locations] entry mapping "
            f"that $NAME to a local dir, or run `crib project release "
            f"{self.project}` to move the notes back into the global store")


def resolve_project_paths(paths: Paths, cfg: "Config", project: str) -> ProjectPaths:
    """Resolve a project's data-tier locations (global, or an adopted in-repo store).

    The stub `.cribproject` is the authority — NOT the repo's `.crib` — because
    the daemon must find an adopted store with no cwd anywhere near the repo
    (project listing, startup reconcile and watcher roots all scan `projects_dir`
    as before). `.crib` `store:` is what `project adopt` reads to WRITE that stub."""
    from .config import ProjectConfig, expand_location
    name = check_project_name(project)
    pdir = paths.projects_dir / name
    token = ProjectConfig.load(pdir / ".cribproject", name).store_root
    if not token:
        # Global layout: the ring is the SHARED `data_dir/.versions`, keyed by
        # note id across every project (DESIGN §8) — unchanged for global projects.
        return ProjectPaths(name, pdir, pdir / "notes", paths.versions_dir,
                            data_root=pdir)
    root = expand_location(token, cfg.locations)
    return ProjectPaths(name, pdir, root / "notes", root / ".versions",
                        data_root=root,
                        store_root=root, store_token=token,
                        available=root.is_dir())


class ProjectPathResolver:
    """`resolve_project_paths` with a per-project cache — it is hit on every note
    read and write, and each miss costs a `.cribproject` parse.

    Adoption/release (and anything else that rewrites a stub) must
    `invalidate()`; nothing else changes a project's layout mid-process."""

    def __init__(self, paths: Paths, config: "Config") -> None:
        self.paths = paths
        self.config = config
        self._cache: dict[str, ProjectPaths] = {}

    def __call__(self, project: str) -> ProjectPaths:
        pp = self._cache.get(project)
        if pp is None:
            pp = resolve_project_paths(self.paths, self.config, project)
            self._cache[project] = pp
        return pp

    def invalidate(self, project: str | None = None) -> None:
        if project is None:
            self._cache.clear()
        else:
            self._cache.pop(project, None)
