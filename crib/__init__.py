"""cribsheet — markdown memory with a Chroma embedding index.

The package *imports* cleanly with only PyYAML present: heavy backends
(chromadb, sentence-transformers, fastmcp, watchdog) are imported lazily by the
modules that need them, so the core indexing loop stays usable — and testable —
without them. That's a code property, not a packaging claim — the base install
ships chromadb (a pyproject dependency), and normal use wants `[full]`.
"""

# Single-sourced from pyproject: the installed package metadata IS the version,
# so `[project] version` in pyproject.toml (and the matching git tag) is the only
# place it's written. Falls back gracefully when run from an uninstalled tree.
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("cribsheet")
except PackageNotFoundError:          # running from source without an install
    __version__ = "0+unknown"

del PackageNotFoundError, _pkg_version
