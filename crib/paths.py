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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import CribUserError

if TYPE_CHECKING:
    from .config import Config

# One path-safe segment. A single '@' separates an OVERLAY from its base
# (`toucan@pr-123`: a copy of `toucan` in a bundle); never leading or trailing.
_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]+(@[A-Za-z0-9._-]+)?$")


def check_project_name(project: str) -> str:
    """Validate a project name — ONE path-safe segment, never a path.

    A project name is used verbatim as a directory name under `projects/`, and it
    reaches us from tool arguments and `.crib` files, so `../x` (or an absolute
    path) would otherwise plant a whole namespace outside the data dir. Returns
    the name so it can be used inline."""
    if project in (".", "..") or not _PROJECT_RE.match(project or ""):
        raise CribUserError(
            f"invalid project name {project!r}: use letters, digits, '.', '_' or "
            "'-' only — a project is a name, not a path"
        )
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
            f"absolute path not allowed: {relpath!r} — pass a path relative to {base}"
        )
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


# CONFINED MODE (`--root` / $CRIB_ROOT): everything crib keeps lives under one
# root, and wins over the per-dir overrides: config, data and index are its
# `config/`, `data/` and `index/` (the index dir, i.e. the store, may be put
# elsewhere with --store / $CRIB_STORE: a disposable cache). The rest of the
# mode (no git sync, no
# shared store, watchers only inside the root, the checkout's own .crib never
# read or written, writes only under the root) keys off `confined_root()`.
ROOT_ENV = "CRIB_ROOT"
# `--store DIR`: where the store (index dir) lives. In confined mode it is the
# ONLY way to move the store: an inherited $CRIB_INDEX_DIR could point it at the
# trusted side's own index.
STORE_ENV = "CRIB_STORE"
_ROOT_SUBDIR = {"CRIB_CONFIG_DIR": "config", "CRIB_DATA_DIR": "data",
                "CRIB_INDEX_DIR": "index"}


def confined_root() -> Path | None:
    """The confined-mode root, or None for a normal instance."""
    v = os.environ.get(ROOT_ENV)
    return Path(v).expanduser().resolve() if v else None


def _resolve(env_override: str, xdg_var: str, xdg_default: str) -> Path:
    store = os.environ.get(STORE_ENV) if env_override == "CRIB_INDEX_DIR" else None
    if (root := confined_root()) is not None:
        # the STORE (index dir: the DuckDB file + caches) is the one thing a
        # confined instance may keep elsewhere (`--store`, e.g. pod scratch):
        # a disposable cache, rebuilt from the root
        return Path(store).expanduser() if store else root / _ROOT_SUBDIR[env_override]
    if store:
        return Path(store).expanduser()
    if v := os.environ.get(env_override):
        return Path(v).expanduser()
    if v := os.environ.get(xdg_var):
        return Path(v).expanduser() / "crib"
    return Path.home() / xdg_default / "crib"


def config_dir() -> Path:
    """crib's config dir: `$CRIB_CONFIG_DIR`, else `$XDG_CONFIG_HOME/crib`,
    else ~/.config/crib. The one resolver for config.toml and the system facets
    file alike."""
    return _resolve("CRIB_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    index_dir: Path
    # A second, READ-ONLY projects dir (config `context_dir`; a bundle's context
    # projects): a project found there lives there whole (stub, notes, records)
    # and is never written. Set by `with_context`.
    context_dir: Path | None = None

    def with_context(self, cfg: Any) -> "Paths":
        """These paths with the config's `context_dir` (expanded) applied."""
        from dataclasses import replace

        from .config import expand_location

        if not getattr(cfg, "context_dir", None):
            return self
        return replace(self, context_dir=expand_location(cfg.context_dir, cfg.locations))

    def in_context(self, project: str) -> bool:
        """Is `project` one of the read-only context projects?"""
        return (self.context_dir is not None
                and (self.context_dir / check_project_name(project)).is_dir())

    @classmethod
    def resolve(cls) -> Paths:
        return cls(
            config_dir=config_dir(),
            data_dir=_resolve("CRIB_DATA_DIR", "XDG_DATA_HOME", ".local/share"),
            index_dir=_resolve("CRIB_INDEX_DIR", "XDG_CACHE_HOME", ".cache"),
        )

    # --- derived locations -------------------------------------------------
    @property
    def root(self) -> Path | None:
        """The confined-mode root (`--root` / $CRIB_ROOT), or None."""
        return confined_root()

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
    def trash_dir(self) -> Path:
        """Holding area for deleted projects — `project_delete` moves a tree here
        rather than rm-ing it, one-level undo in the spirit of the version ring."""
        return self.data_dir / ".trash"

    @property
    def duckdb_file(self) -> Path:
        """The DuckDB vector store — a derived index tier, so it lives under the
        index dir and a full reindex can always rebuild it."""
        return self.index_dir / "duckdb" / "crib.duckdb"

    @property
    def duckdb_token_file(self) -> Path:
        """The shared DuckDB server's auth token (0600, created once)."""
        return self.duckdb_file.parent / "quack.token"

    @property
    def duckdb_extension_dir(self) -> Path:
        """crib's own DuckDB extension directory (Quack lives here once fetched),
        so crib never depends on, or writes to, the user's ~/.duckdb."""
        return self.duckdb_file.parent / "extensions"

    def project_dir(self, project: str) -> Path:
        """The project's GLOBAL dir under `projects/`.

        Always machine-local, even for a project whose DATA tier lives in a repo
        (`store:` in `.crib`): the derived index tiers (`symbol_index/`,
        `keyword_index/`, `summary_index/`, `doc-sources.json`) and the
        `.cribproject` stub stay here by design, so `rm -rf $CRIB_INDEX_DIR` +
        reindex stays the universal recovery path and embeddings can never be
        committed to someone's repo. The DATA tier (notes + their version ring)
        is the part that may move, and it must be resolved through
        `resolve_project_paths` rather than derived from here.

        A context project (`in_context`) lives whole in the context dir, which
        takes precedence: the writable root can't shadow it."""
        if self.in_context(project):
            assert self.context_dir is not None
            return self.context_dir / check_project_name(project)
        return self.projects_dir / check_project_name(project)

    def notes_dir(self, project: str) -> Path:
        """The GLOBAL notes dir for a project. Callers that must honour an
        adopted in-repo store go through `resolve_project_paths` (or
        `ProjectPathResolver`) instead — this is the global-layout half of what
        that resolves to."""
        return self.project_dir(project) / "notes"

    def ensure(self) -> Paths:
        for d in (
            self.config_dir,
            self.data_dir,
            self.projects_dir,
            self.versions_dir,
            self.index_dir,
            self.trash_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self


# ── Where a project's DATA tier actually lives (docs/plans/repo-local-storage) ──
# A project's notes are EITHER global (`projects/<name>/notes`, ring shared at
# `data_dir/.versions`) OR in a repo subdir declared by that repo's `.crib`
# `store:` and recorded machine-locally in the global stub's `.cribproject`
# `store_root` as a portable `$LOCATION` token. Never both — adoption is a move,
# not an overlay. Only this pair of tiers moves; see `Paths.project_dir`.


@dataclass(frozen=True)
class FacetLocation:
    """Where one facet's items live for a project when its repo maps it (`.crib`
    `facets:`, recorded in the stub): the mapping's root, the glob picking its
    files, whether it is writable, and how ids work (see config.FacetMapping)."""

    root: Path
    token: str  # the root's portable `$LOCATION/...` spelling
    glob: str = "**/*.md"
    read_only: bool = False
    ids: str = "stamp"
    # An ADDITIVE location (docs: `sources/<repo>/`): its files sit beside the
    # facet's own items under this relpath namespace. None: the facet's home.
    prefix: str | None = None

    @property
    def available(self) -> bool:
        return self.root.is_dir()

    def owns(self, relpath: str) -> bool:
        """Is `relpath` (relative to the root) one of this facet's files? Never
        anything under a hidden dir: the facet's own version ring lives inside
        its mapping (`.versions/`), and `.git` is the repo's."""
        from pathlib import PurePosixPath

        rp = PurePosixPath(relpath)
        if any(part.startswith(".") for part in rp.parts[:-1]):
            return False
        return rp.full_match(self.glob)

    @property
    def versions_dir(self) -> Path:
        """The mapped facet's version ring: beside its files, gitignored there."""
        return self.root / ".versions"

    @property
    def suffix(self) -> str:
        """What a new item's filename ends with, so the glob takes it: the
        glob's `*<suffix>` (`**/*.dec.md` -> `.dec.md`). A writable mapping's
        glob always has that shape (config.writable_glob)."""
        last = self.glob.rsplit("/", 1)[-1]
        return last[1:] if last.startswith("*") and last.endswith(".md") else ".md"


@dataclass(frozen=True)
class ProjectPaths:
    """One project's resolved storage locations."""

    project: str
    project_dir: Path  # global `projects/<name>` (stub or full)
    notes_dir: Path
    versions_dir: Path
    data_root: Path  # dir holding the pillar stores (notes/,
    # design/, plans/, learnings/): the global
    # project dir, or the adopted store root
    store_root: Path | None = None  # the in-repo store dir, when adopted
    store_token: str | None = None  # its portable `$LOCATION/...` spelling
    available: bool = True  # False ⇒ store_root isn't on this machine
    # Mapped facets, by STORE segment (`routines`): their items live at the
    # mapping, not under the data root.
    mappings: Mapping[str, FacetLocation] = field(default_factory=dict)
    # a transient project (see ProjectConfig.transient): no version ring, not
    # synced, not reconciled at startup, deleted when `expires` passes
    transient: bool = False
    expires: str | None = None

    @property
    def in_repo(self) -> bool:
        return self.store_root is not None

    def home_dir(self, segment: str) -> Path:
        """A pillar's directory under the data root, mapped or not: where its
        items live unless a mapping moves them (`crib project adopt` moves them
        there, `release` back)."""
        return self.data_root / segment

    def pillar_dir(self, segment: str) -> Path:
        """A pillar store's directory: `data_root/<segment>`. Every pillar —
        notes included (`pillar_dir("notes") == notes_dir`) — is a sibling under
        the data root, in both layouts; a mapped facet's is its mapping's root."""
        if (loc := self.mappings.get(segment)) is not None:
            return loc.root
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
            f"{self.project}` to move the notes back into the global store"
        )


def resolve_project_paths(paths: Paths, cfg: "Config", project: str) -> ProjectPaths:
    """Resolve a project's data-tier locations (global, or an adopted in-repo store).

    The stub `.cribproject` is the authority — NOT the repo's `.crib` — because
    the daemon must find an adopted store with no cwd anywhere near the repo
    (project listing, startup reconcile and watcher roots all scan `projects_dir`
    as before). `.crib` `store:` is what `project adopt` reads to WRITE that stub."""
    from .config import ProjectConfig, expand_location

    name = check_project_name(project)
    pdir = paths.project_dir(name)
    pcfg = ProjectConfig.load(pdir / ".cribproject", name)
    token = pcfg.store_root
    mappings = _mappings(pcfg.mappings, cfg)
    if not token:
        # Global layout: the ring is the SHARED `data_dir/.versions`, keyed by
        # note id across every project (DESIGN §8) — unchanged for global projects.
        return ProjectPaths(
            name, pdir, pdir / "notes", paths.versions_dir, data_root=pdir,
            mappings=mappings, transient=bool(pcfg.transient), expires=pcfg.expires,
        )
    root = expand_location(token, cfg.locations)
    return ProjectPaths(
        name,
        pdir,
        root / "notes",
        root / ".versions",
        data_root=root,
        store_root=root,
        store_token=token,
        available=root.is_dir(),
        mappings=mappings,
        transient=bool(pcfg.transient),
        expires=pcfg.expires,
    )


def _mappings(raw: Any, cfg: "Config") -> dict[str, FacetLocation]:
    """The stub's recorded facet mappings, by store segment (an entry for a facet
    no longer in the registry is ignored: `crib facets sync` deals with it)."""
    from . import facets
    from .config import expand_location

    out: dict[str, FacetLocation] = {}
    for noun, m in (raw or {}).items():
        spec = facets.get(noun)
        if spec is None or not isinstance(m, dict) or not m.get("root"):
            continue
        out[spec.store] = FacetLocation(
            root=expand_location(str(m["root"]), cfg.locations),
            token=str(m["root"]),
            glob=str(m.get("glob") or "**/*.md"),
            read_only=bool(m.get("read_only", False)),
            ids=str(m.get("ids") or "stamp"),
        )
    return out


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
