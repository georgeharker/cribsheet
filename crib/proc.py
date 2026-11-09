"""Start child processes WITHOUT fork().

Why: on macOS, once a process has used Apple's Network.framework (DuckDB's Quack
client does, through its HTTP stack), the framework's fork handler
(`nw_settings_child_has_forked`) crashes the child of every later fork() before
it can exec. crib's daemon hosts that client in shared DuckDB mode and starts
git, language servers and sharedserver, so it must never fork. posix_spawn runs
no fork handlers.

CPython takes the posix_spawn path only when the program is an explicit path,
there is no `cwd`, and file descriptors are inherited (macOS has no
posix_spawn closefrom). This module arranges all three:

- the program is resolved to an absolute path (a missing one raises
  FileNotFoundError, exactly as `subprocess` would);
- `close_fds=False`: safe, because Python's own fds are non-inheritable
  (PEP 446) and DuckDB/Quack mark theirs close-on-exec;
- a `cwd` is applied by `/bin/sh` (`cd`, then `exec` the program), since
  CPython won't posix_spawn with one. Prefer the program's own flag where it has
  one (`git -C <dir>`).

The same code runs on Linux, where posix_spawn is equally correct, so CI
exercises the path every macOS user takes.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_SH = "/bin/sh"
# $1 is the directory; the rest is the program and its arguments.
_CD_EXEC = 'cd -- "$1" || exit 127; shift; exec "$@"'
# Options that force CPython back onto fork(), with the default that doesn't: any
# other value is refused rather than silently forked.
_FORK_FREE_DEFAULTS: dict[str, Any] = {
    "preexec_fn": None,
    "start_new_session": False,
    "pass_fds": (),
    "process_group": None,
    "user": None,
    "group": None,
    "extra_groups": None,
    "umask": -1,
    "executable": None,
}


def _argv(
    cmd: Sequence[str | os.PathLike[str]], cwd: str | os.PathLike[str] | None
) -> list[str]:
    if not cmd:
        raise ValueError("empty command")
    args = [os.fspath(a) for a in cmd]
    prog = args[0]
    exe = prog if os.path.dirname(prog) else shutil.which(prog)
    if exe is None:
        raise FileNotFoundError(
            errno.ENOENT, f"No such file or directory: {prog!r}", prog
        )
    exe = os.path.abspath(exe)
    if cwd is None:
        return [exe, *args[1:]]
    d = os.fspath(cwd)
    if not os.path.isdir(d):
        raise FileNotFoundError(errno.ENOENT, f"No such directory: {d!r}", d)
    return [_SH, "-c", _CD_EXEC, "crib-proc", d, exe, *args[1:]]


def _check(kwargs: dict[str, Any]) -> None:
    forking = sorted(
        k
        for k, default in _FORK_FREE_DEFAULTS.items()
        if k in kwargs and kwargs[k] != default
    )
    if forking:
        raise ValueError(f"crib.proc refuses options that force fork(): {forking}")
    if kwargs.get("close_fds"):
        raise ValueError(
            "crib.proc needs close_fds=False (posix_spawn); see the module docstring"
        )


def run(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | Path | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """`subprocess.run`, spawned without fork()."""
    _check(kwargs)
    return subprocess.run(_argv(cmd, cwd), close_fds=False, **kwargs)


def popen(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | Path | None = None,
    **kwargs: Any,
) -> subprocess.Popen[Any]:
    """`subprocess.Popen`, spawned without fork()."""
    _check(kwargs)
    return subprocess.Popen(_argv(cmd, cwd), close_fds=False, **kwargs)
