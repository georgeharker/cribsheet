"""cribsheet — markdown memory with a Chroma embedding index.

The package imports cleanly with only PyYAML installed. Heavy backends
(chromadb, sentence-transformers, fastmcp, watchdog) are imported lazily by the
modules that need them, so the core indexing loop is usable — and testable —
without them.
"""

__version__ = "0.1.0"
