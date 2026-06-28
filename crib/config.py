"""Config loading: global config.toml, .cribproject, .crib (DESIGN §6, §10)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EmbedConfig:
    # "hash" = dependency-free dev embedder; "fe:<model>" = fastembed (ONNX);
    # "st:<model>" = sentence-transformers (torch).
    model: str = "hash"
    dim: int = 256  # only used by the hash embedder
    # torch device for the `st:` backend: "auto" (cuda→mps→cpu), or a forced
    # "cuda" / "mps" / "cpu". Ignored by the hash and fastembed backends.
    device: str = "auto"


@dataclass
class ChromaConfig:
    mode: str = "embedded"          # "embedded" | "shared"
    server_name: str = "crib-chroma"
    grace_period: str = "1h"
    host: str = "127.0.0.1"
    # 7733 keeps shared chroma in crib's own 773x band (MCP server is 7732),
    # away from chromadb's default 8000 — the most collision-prone port on the box.
    port: int = 7733


@dataclass
class Config:
    default_project: str = "default"
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    chroma: ChromaConfig = field(default_factory=ChromaConfig)
    versions_keep: int = 20
    watch: bool = True

    @classmethod
    def load(cls, config_file: Path) -> "Config":
        cfg = cls()
        if not config_file.exists():
            return cfg
        data = tomllib.loads(config_file.read_text())
        if "default_project" in data:
            cfg.default_project = data["default_project"]
        if "versions_keep" in data:
            cfg.versions_keep = int(data["versions_keep"])
        if "watch" in data:
            cfg.watch = bool(data["watch"])
        if e := data.get("embed"):
            cfg.embed = EmbedConfig(**{**vars(cfg.embed), **e})
        if c := data.get("chroma"):
            cfg.chroma = ChromaConfig(**{**vars(cfg.chroma), **c})
        return cfg


@dataclass
class ProjectConfig:
    """`.cribproject` — per-project config living in the project's data dir."""
    name: str
    embed_model: str | None = None
    distill_prompt: str | None = None
    versions_keep: int | None = None

    @classmethod
    def load(cls, path: Path, fallback_name: str) -> "ProjectConfig":
        if not path.exists():
            return cls(name=fallback_name)
        data = yaml.safe_load(path.read_text()) or {}
        return cls(
            name=data.get("name", fallback_name),
            embed_model=data.get("embed_model"),
            distill_prompt=data.get("distill_prompt"),
            versions_keep=data.get("versions_keep"),
        )


@dataclass
class CribLink:
    """`.crib` — found at a code repo root; ties the repo to a crib project."""
    project: str
    paths: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    import_into: str | None = None
    root: Path | None = None  # dir the .crib was found in

    @classmethod
    def find(cls, start: Path) -> "CribLink | None":
        """Walk up from `start` looking for a `.crib` file."""
        start = start.resolve()
        for d in (start, *start.parents):
            f = d / ".crib"
            if f.is_file():
                data = yaml.safe_load(f.read_text()) or {}
                return cls(
                    project=data["project"],
                    paths=data.get("paths", []),
                    imports=data.get("import", []),
                    import_into=data.get("import_into"),
                    root=d,
                )
        return None


def resolve_project(
    cfg: Config,
    explicit: str | None,
    cwd: Path | None = None,
) -> str:
    """Project precedence (DESIGN §6): explicit arg -> .crib -> default."""
    if explicit:
        return explicit
    if cwd is not None and (link := CribLink.find(cwd)):
        return link.project
    return cfg.default_project
