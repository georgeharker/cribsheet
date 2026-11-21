"""Files whose language server returned NO symbols although they hold code.

A server that can't load its language's runtime (typescript-language-server
with no usable tsserver, a zsh server mid-init) answers documentSymbol with
an empty list: no error, fast. crib used to accept that silently: a new file
got no records, a previously indexed one kept its old records as if current.
This is the durable record of such files, per project, kept in the INDEX tier
(it describes this machine's servers, not the code): written by the indexer
when an extract comes back empty, cleared when the file next yields symbols,
read by the sweep's report, `crib project doctor` and `code_dossier`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _file(index_dir: Path, project: str) -> Path:
    return index_dir / "code" / f"{project}.empty-extracts.json"


def load(index_dir: Path, project: str) -> dict[str, dict[str, Any]]:
    """`{relpath: {"server": label, "kept_prior": n}}` for `project` ({} if none)."""
    try:
        data = json.loads(_file(index_dir, project).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(
    index_dir: Path, project: str, relpath: str, server: str | None, kept_prior: int
) -> None:
    """Note that `relpath` extracted to nothing (with `kept_prior` old records)."""
    _update(index_dir, project, relpath, {"server": server, "kept_prior": kept_prior})


def clear(index_dir: Path, project: str, relpath: str) -> None:
    """`relpath` extracted symbols again: forget its empty record."""
    _update(index_dir, project, relpath, None)


def _update(index_dir: Path, project: str, relpath: str, value: dict | None) -> None:
    with _LOCK:
        cur = load(index_dir, project)
        if (value is None and relpath not in cur) or cur.get(relpath) == value:
            return
        if value is None:
            cur.pop(relpath)
        else:
            cur[relpath] = value
        f = _file(index_dir, project)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(cur, indent=1, sort_keys=True))
            tmp.replace(f)
        except OSError:
            pass  # a diagnostic record must never fail an index
