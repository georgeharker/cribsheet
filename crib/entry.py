"""The `crib` command's entry point.

Some global flags must take effect BEFORE crib's modules load: the facet
registry (and what is built from it) is resolved at import, from the config
dir, so `--root` (confined mode) applied later in `cli.main` would leave the
registry read from the default config dir, outside the root. This reads the
GLOBAL flags that set the environment (`--root`, `--store`, `--writable`) off
the front of the command line, sets their variables, and only then imports the
CLI. It stops at the first non-flag token (the command): a subcommand's own
options (`project create --store RELPATH`) are not global ones.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# global options that take a value (so the next token is theirs, not the command)
_VALUED = {"--tools", "--host", "--port", "--root", "--store", "--writable", "--color"}


def _apply_env_flags(argv: list[str]) -> None:
    writable: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-") or tok == "--":
            break  # the command: everything after it is the command's own
        name, eq, inline = tok.partition("=")
        value = inline if eq else (argv[i + 1] if name in _VALUED and i + 1 < len(argv)
                                   else None)
        if name in _VALUED and not eq:
            i += 1
        i += 1
        if value is None:
            continue
        if name in ("--root", "--store") and not value.strip():
            raise SystemExit(f"crib: {name} needs a directory")
        if name == "--root":
            os.environ["CRIB_ROOT"] = str(Path(value).expanduser().resolve())
        elif name == "--store":
            os.environ["CRIB_STORE"] = str(Path(value).expanduser().resolve())
        elif name == "--writable":
            writable.append(value)
    if writable:
        os.environ["CRIB_WRITABLE"] = ",".join(writable)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _apply_env_flags(args)
    from .cli import main as cli_main

    return cli_main(args)
