"""cribsheet — markdown memory with a Chroma embedding index.

The package *imports* cleanly with only PyYAML present: heavy backends
(chromadb, sentence-transformers, fastmcp, watchdog) are imported lazily by the
modules that need them, so the core indexing loop stays usable — and testable —
without them. That's a code property, not a packaging claim — the base install
ships chromadb (a pyproject dependency), and normal use wants `[full]`.
"""

__version__ = "0.1.0"
