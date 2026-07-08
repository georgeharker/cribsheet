"""XDG + CRIB_* path resolution (DESIGN §2).

Three lifecycles, three roots:
  config  -> CRIB_CONFIG_DIR | $XDG_CONFIG_HOME/crib | ~/.config/crib
  data    -> CRIB_DATA_DIR   | $XDG_DATA_HOME/crib   | ~/.local/share/crib
  index   -> CRIB_INDEX_DIR  | $XDG_CACHE_HOME/crib  | ~/.cache/crib
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
        return self.projects_dir / project

    def notes_dir(self, project: str) -> Path:
        return self.project_dir(project) / "notes"

    def ensure(self) -> Paths:
        for d in (self.config_dir, self.data_dir, self.projects_dir,
                  self.versions_dir, self.index_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self
