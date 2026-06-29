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
    # Asymmetric retrieval: instruction prepended to QUERY text only (passages
    # stay raw) before embedding. None = auto (the canonical instruction for
    # English BGE models, "" for everything else); set "" to disable, or a
    # custom string. Only the query path changes, so no reindex is needed.
    query_prefix: str | None = None


@dataclass
class ChunkConfig:
    """How long sections are split for embedding. A section longer than
    `window_words` is cut into overlapping windows; `overlap_ratio` is the
    fraction of each window re-shared with its neighbour (so a knob set once
    holds steady if the window size changes). Changing either re-chunks notes —
    run `crib reindex` (or bounce the daemon) to apply to existing docs."""
    window_words: int = 320         # keep windows under the model's 512-token cap
    overlap_ratio: float = 0.20     # 0.0–<1.0; 0.20 => 64-word overlap at 320

    @property
    def overlap_words(self) -> int:
        # Clamp below the window so the windowing step can't stall.
        ratio = min(max(self.overlap_ratio, 0.0), 0.9)
        return min(round(self.window_words * ratio), self.window_words - 1)


@dataclass
class MemoryConfig:
    """Mirroring of Claude Code harness memory into crib projects (DESIGN §13).
    `watch` enables the daemon's live mirror over bound repos (opted in via
    `crib import-memory`); the one-shot `import-memory` works regardless."""
    watch: bool = True


@dataclass
class RetrieveConfig:
    """How `lookup`/`apropos` rank. `hybrid` fuses the dense vector ranking with
    a BM25 lexical ranking (reciprocal-rank fusion), which fixes terse keyword
    queries where exact-term sections lose to vaguely-on-topic prose. `rrf_k`
    is the RRF damping constant (60 is the canonical value).

    `rerank` adds a cross-encoder pass over the top `rerank_top_n` fused
    candidates — it reads (query, passage) jointly, fixing vocabulary-divergent
    queries that both dense and BM25 miss. Off by default: it's a model inference
    per candidate, so it costs latency (warm in the daemon, ONNX/CPU)."""
    hybrid: bool = True
    rrf_k: int = 60
    rerank: bool = False
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"  # small ONNX cross-encoder
    rerank_top_n: int = 20                               # candidate pool to rerank


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
class DaemonConfig:
    """The long-lived `crib --mcp --http` process the CLI attaches to.

    It IS the MCP server: one warm process per machine, shared by Claude (over
    MCP) and the `crib` CLI (as an MCP client). sharedserver owns its lifecycle
    — refcount + grace keep it warm between CLI calls; `name`/`host`/`port` must
    match the sharedServer registration so everyone attaches to the same process.
    """
    enabled: bool = True
    name: str = "cribsheet"         # sharedserver name (== the MCP registration)
    host: str = "127.0.0.1"
    port: int = 7732                # crib's MCP band (chroma is 7733)
    grace_period: str = "1h"        # keep warm this long after the last client


@dataclass
class Config:
    default_project: str = "default"
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    retrieve: RetrieveConfig = field(default_factory=RetrieveConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    chroma: ChromaConfig = field(default_factory=ChromaConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    versions_keep: int = 20
    watch: bool = True
    # Named path roots → local absolute dirs, e.g. {"DEV": "~/Development"}.
    # Provenance paths are stored as portable `$NAME/rest` tokens against these
    # (HOME is always implied) so an imported note's `source_repo` reads the same
    # on every machine instead of conflicting on the absolute prefix (DESIGN §14).
    locations: dict[str, str] = field(default_factory=dict)

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
        if ck := data.get("chunk"):
            cfg.chunk = ChunkConfig(**{**vars(cfg.chunk), **ck})
        if r := data.get("retrieve"):
            cfg.retrieve = RetrieveConfig(**{**vars(cfg.retrieve), **r})
        if m := data.get("memory"):
            cfg.memory = MemoryConfig(**{**vars(cfg.memory), **m})
        if c := data.get("chroma"):
            cfg.chroma = ChromaConfig(**{**vars(cfg.chroma), **c})
        if d := data.get("daemon"):
            cfg.daemon = DaemonConfig(**{**vars(cfg.daemon), **d})
        if loc := data.get("locations"):
            cfg.locations = {str(k): str(v) for k, v in loc.items()}
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


def _location_roots(locations: dict[str, str]) -> list[tuple[str, Path]]:
    """Configured roots plus the built-in HOME, longest path first so a nested
    named root (`$DEV` under `$HOME`) wins the greedy match."""
    pairs = [(name, Path(p).expanduser()) for name, p in locations.items()]
    pairs.append(("HOME", Path.home()))
    return sorted(pairs, key=lambda kp: len(str(kp[1])), reverse=True)


def portable_path(p: str | Path, locations: dict[str, str]) -> str:
    """Rewrite an absolute path to a machine-portable `$NAME/rest` token by
    substituting the longest matching configured location (HOME built in). Falls
    back to the plain string when nothing matches — better a stable, if
    non-portable, value than a crash."""
    ap = Path(p).expanduser()
    for name, root in _location_roots(locations):
        try:
            rel = ap.relative_to(root)
        except ValueError:
            continue
        return f"${name}" if rel == Path(".") else f"${name}/{rel.as_posix()}"
    return str(ap)


def expand_location(token: str, locations: dict[str, str]) -> Path:
    """Expand a `$NAME/rest` token back to a local absolute path. The inverse of
    `portable_path`; an unknown name or a non-token is returned verbatim."""
    if not token.startswith("$"):
        return Path(token)
    head, _, rest = token[1:].partition("/")
    roots = {"HOME": Path.home(),
             **{k: Path(v).expanduser() for k, v in locations.items()}}
    root = roots.get(head)
    if root is None:
        return Path(token)
    return root / rest if rest else root


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
