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

_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def check_project_name(project: str) -> str:
    """Validate a project name — ONE path-safe segment, never a path.

    A project name is used verbatim as a directory name under `projects/`, and it
    reaches us from tool arguments and `.crib` files, so `../x` (or an absolute
    path) would otherwise plant a whole namespace outside the data dir. Returns
    the name so it can be used inline."""
    if project in (".", "..") or not _PROJECT_RE.match(project or ""):
        raise ValueError(
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
        raise ValueError(f"empty path: expected a path relative to {base}")
    p = Path(relpath)
    if p.is_absolute():
        raise ValueError(
            f"absolute path not allowed: {relpath!r} — pass a path relative to {base}")
    if ".." in p.parts:
        raise ValueError(f"path escapes {base}: {relpath!r} — no '..' segments")
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
        raise ValueError(f"path escapes {base}: {'/'.join(parts)!r}")
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
        return self.projects_dir / check_project_name(project)

    def notes_dir(self, project: str) -> Path:
        return self.project_dir(project) / "notes"

    def ensure(self) -> Paths:
        for d in (self.config_dir, self.data_dir, self.projects_dir,
                  self.versions_dir, self.index_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self
