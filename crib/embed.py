"""Embedders (DESIGN §10). Pluggable behind a protocol.

`HashEmbedder` is a deterministic, dependency-free embedder so the whole index
loop runs and tests without torch. `SentenceTransformerEmbedder` is the real
local default; it lazy-imports sentence-transformers.
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

from .config import EmbedConfig


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class HashEmbedder:
    """Deterministic bag-of-hashed-tokens vector, L2-normalized.

    Not semantically smart, but stable and free — it makes the store/index/lookup
    loop exercisable end to end. Real retrieval quality comes from the ST backend.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in text.lower().split():
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            v[idx] += sign
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class FastEmbedEmbedder:
    """ONNX-based embeddings via fastembed — no torch, no CUDA/nvidia.

    The recommended local backend on hardware without a GPU (e.g. a Pi): reuses
    the onnxruntime that chromadb already pulls in, and runs models like
    `BAAI/bge-small-en-v1.5` at a fraction of the install weight of torch.
    """

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding  # lazy

        # Pin CPU provider: no GPU here, and it silences onnxruntime's noisy
        # device-discovery warnings.
        try:
            self._model = TextEmbedding(
                model_name=model_name, providers=["CPUExecutionProvider"])
        except TypeError:  # older fastembed without a providers kwarg
            self._model = TextEmbedding(model_name=model_name)
        probe = next(iter(self._model.embed(["dimension probe"])))
        self.dim = len(probe)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for v in self._model.embed(list(texts)):
            vec = [float(x) for x in v]
            n = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / n for x in vec])   # L2-normalize for cosine
        return out


def _auto_device() -> str:
    """Pick the best available torch device: CUDA → MPS (Apple) → CPU.

    Matches the policy "accelerator if the box has one, else CPU": nvidia/CUDA on
    CUDA systems, MPS on Apple Silicon, plain CPU on a Pi or unaccelerated Linux.
    """
    import torch  # lazy

    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


class SentenceTransformerEmbedder:
    """torch-based embedder. Heavier than fastembed, but often higher quality.

    Auto-selects the device (CUDA/MPS/CPU). On non-nvidia hardware install the
    CPU-only torch wheel so no CUDA libs are pulled:
        uv pip install torch --index-url https://download.pytorch.org/whl/cpu
    """

    def __init__(self, model_name: str, device: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # lazy

        self.device = device or _auto_device()
        self._model = SentenceTransformer(model_name, device=self.device)
        # method renamed in sentence-transformers 5.x; fall back for older
        get_dim = getattr(self._model, "get_embedding_dimension", None) \
            or self._model.get_sentence_embedding_dimension
        self.dim = get_dim()

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True
        )
        return [v.tolist() for v in vecs]


def build_embedder(cfg: EmbedConfig) -> Embedder:
    """Resolve `cfg.model` to an embedder.

      hash                  -> HashEmbedder (dependency-free)
      fe:<model> / bare     -> FastEmbedEmbedder (ONNX, recommended)
      st:<model>            -> SentenceTransformerEmbedder (heavy, torch)

    Falls back to the hash embedder (with a warning) if the chosen backend isn't
    installed, so a config naming a real model can't crash the server — it
    degrades, the same way the store falls back to JSON without chromadb.
    """
    model = cfg.model
    if model == "hash":
        return HashEmbedder(dim=cfg.dim)
    backend, sep, name = model.partition(":")
    device = None if cfg.device == "auto" else cfg.device
    try:
        if backend == "st":
            return SentenceTransformerEmbedder(name, device=device)
        if backend in ("fe", "fastembed"):
            return FastEmbedEmbedder(name)
        return FastEmbedEmbedder(model)   # bare model name -> fastembed
    except ImportError:
        import sys
        print(f"[crib] embedding backend for {cfg.model!r} not installed; "
              f"falling back to the hash embedder. Install the recommended "
              f"ONNX backend with: pip install 'cribsheet[embed]'", file=sys.stderr)
        return HashEmbedder(dim=cfg.dim)
