"""Code symbol index — structural facet (docs/code-symbol-index.md).

Extract a repo's symbols + call graph from a live LSP server and persist them as a
content-addressed, git-communicable store (`<project>/symbol_index/<symbol_hash>.toml`)
— separate from notes, exactly as `keyword_index`/`summary_index` are. The LSP is a
*generator/refresher*, not a serving dependency: once written, callers/callees are
queryable with no server running (docs §3, the two-tier model).

Step-1 scope: Python via pyright, live (no warm session / watcher yet). The client is
raw JSON-RPC over stdio — lsprotocol adoption + the warm-session manager are the next
steps. `symbol_hash` is the hash of the symbol BODY (window-invariant identity, the
`section_hash` pattern) so a description/edge regenerates iff the symbol's own code
changed.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any


# ── LSP server specs — Claude Code `.lsp.json` schema (docs §3.3) ─────────────
# A map of label → {command, args, extensionToLanguage, transport?, env?,
# initializationOptions?, settings?}, identical to the schema Claude Code's
# `.lsp.json` / plugin `lspServers` use — so existing spec files drop in unchanged.
# Selection is by file EXTENSION via `extensionToLanguage`; its value is the LSP
# `languageId` sent in `didOpen`. `command` is resolved per-machine (`$ENV`
# expansion + `which`), so the spec stays portable — the machine-specific binary
# path is never baked in. Override/extend in `~/.config/crib/lsp.json` (same schema;
# user labels take precedence).
#
# For the CALL GRAPH we need `textDocument/callHierarchy`. basedpyright, pyright,
# rust-analyzer, gopls, clangd implement it; pylsp+jedi does NOT (callers only, via
# references). basedpyright is preferred with pyright as the fallback for `.py`.
DEFAULT_LSP_SPECS: dict[str, dict[str, Any]] = {
    # Python: ty (Astral, Rust) FIRST — full LSP (documentSymbol + callHierarchy +
    # references, all verified) and much faster than pyright; falls through to
    # basedpyright/pyright when ty isn't installed.
    "ty": {"command": "ty", "args": ["server"],
           "extensionToLanguage": {".py": "python", ".pyi": "python"}},
    "basedpyright": {"command": "basedpyright-langserver", "args": ["--stdio"],
                     "extensionToLanguage": {".py": "python", ".pyi": "python"}},
    "pyright": {"command": "pyright-langserver", "args": ["--stdio"],
                "extensionToLanguage": {".py": "python", ".pyi": "python"}},
    "rust-analyzer": {"command": "rust-analyzer", "args": [],
                      "extensionToLanguage": {".rs": "rust"}},
    "gopls": {"command": "gopls", "args": [],
              "extensionToLanguage": {".go": "go"}},
    "clangd": {"command": "clangd", "args": [],
               "extensionToLanguage": {".c": "c", ".h": "c", ".cc": "cpp",
                                       ".cpp": "cpp", ".hpp": "cpp", ".hh": "cpp"}},
    # TypeScript/JavaScript: typescript-language-server (tsserver over LSP —
    # documentSymbol + references + callHierarchy, all verified) first; vtsls
    # (the VSCode-flavoured tsserver wrapper, same capabilities) as the fallback.
    "typescript-language-server": {
        "command": "typescript-language-server", "args": ["--stdio"],
        "extensionToLanguage": {".ts": "typescript", ".tsx": "typescriptreact",
                                ".mts": "typescript", ".cts": "typescript",
                                ".js": "javascript", ".jsx": "javascriptreact",
                                ".mjs": "javascript", ".cjs": "javascript"}},
    "vtsls": {
        "command": "vtsls", "args": ["--stdio"],
        "extensionToLanguage": {".ts": "typescript", ".tsx": "typescriptreact",
                                ".mts": "typescript", ".cts": "typescript",
                                ".js": "javascript", ".jsx": "javascriptreact",
                                ".mjs": "javascript", ".cjs": "javascript"}},
    # Lua: emmylua_ls (Rust rewrite, aims for full LSP incl. call hierarchy) first,
    # lua-language-server (LuaLS — references/definition but NO call hierarchy) as
    # the fallback. Even without call hierarchy, descriptions + callers-via-references
    # work; callees fall back to call-site→definition (§3.4).
    "emmylua_ls": {"command": "emmylua_ls", "args": [],
                   "extensionToLanguage": {".lua": "lua"}},
    "lua-language-server": {"command": "lua-language-server", "args": [],
                            "extensionToLanguage": {".lua": "lua"}},
    # Shell/zsh: shuck (Rust shell checker; `shuck server` speaks LSP over stdio).
    # documentSymbol + definition + references, but NO call hierarchy — symbols
    # index fine, calls/called_by stay empty (§3.4). shuck ignores the languageId
    # (it keys on content/extension), so "zsh" is just the stored `lang` label.
    # pinWorkspace: shuck's own discovery misses what crib enumerates by grammar
    # (extensionless autoload files, dotfiles) — the sweep didOpen-pins the full
    # doc set so cross-file references cover them (LSP membership via open docs).
    "shuck": {"command": "shuck", "args": ["server"],
              "extensionToLanguage": {".zsh": "zsh"},
              "pinWorkspace": True},
}


def _config_dir() -> Path:
    return Path(os.environ.get("CRIB_CONFIG_DIR",
                               os.path.expanduser("~/.config/crib")))


def load_specs() -> dict[str, dict[str, Any]]:
    """Merged LSP specs: `~/.config/crib/lsp.json` (user, iterated FIRST so it wins
    selection) ⊕ the shipped defaults. Same `.lsp.json` schema as Claude Code."""
    merged: dict[str, dict[str, Any]] = {}
    f = _config_dir() / "lsp.json"
    if f.exists():
        try:
            user = json.loads(f.read_text())
            if isinstance(user, dict):
                merged.update(user)
        except (ValueError, OSError):
            pass
    for label, spec in DEFAULT_LSP_SPECS.items():
        merged.setdefault(label, dict(spec))   # defaults fill gaps, never override
    return merged


def resolve_command(spec: dict) -> list[str] | None:
    """Portable `command` → argv, or None if the binary isn't on this machine.
    Expands `${ENV}` (incl. `${CLAUDE_PLUGIN_ROOT}`); a bare name is `which`-resolved."""
    cmd = os.path.expandvars(spec.get("command", ""))
    args = list(spec.get("args", []))
    if not cmd:
        return None
    if os.sep in cmd:                       # explicit / expanded path
        return [cmd, *args] if Path(cmd).exists() else None
    resolved = shutil.which(cmd)
    return [resolved, *args] if resolved else None


# Grammar map — how a file's language is inferred BEYOND its extension, so
# extensionless sources (shell autoload functions, dotfiles) get indexed like any
# other. Configurable via ~/.config/crib/grammar.json (merged over these defaults,
# user keys win — same pattern as lsp.json). `extensionToLanguage` on each LSP spec
# stays the primary, fast path; these rules cover files the extension can't.
DEFAULT_GRAMMAR: dict[str, dict[str, str]] = {
    # `#!` interpreter basename (version suffix stripped, `env` unwrapped) → language
    "shebangs": {
        "zsh": "zsh", "bash": "bash", "sh": "sh", "dash": "sh",
        "ksh": "ksh", "mksh": "ksh", "python": "python", "node": "javascript",
        "nodejs": "javascript", "ruby": "ruby", "perl": "perl", "lua": "lua",
    },
    # exact bare filename → language (shells key rc files by name, no extension)
    "filenames": {
        ".zshrc": "zsh", ".zshenv": "zsh", ".zprofile": "zsh", ".zlogin": "zsh",
        ".zlogout": "zsh", ".bashrc": "bash", ".bash_profile": "bash",
        ".bash_login": "bash", ".profile": "sh",
    },
    # first-line tag (letters directly after `#`, NO space) → language. zsh
    # autoload/completion files begin `#compdef`/`#autoload` instead of a shebang;
    # matches compinit + shuck (prose `# compdef` with a space is NOT swept in).
    "firstLineMarkers": {"compdef": "zsh", "autoload": "zsh"},
}


def load_grammar() -> dict[str, dict[str, str]]:
    """Merged grammar map: `~/.config/crib/grammar.json` (user) ⊕ DEFAULT_GRAMMAR.
    Per-category dict merge — a user category overlays the default (user keys win),
    other categories keep their defaults."""
    merged = {k: dict(v) for k, v in DEFAULT_GRAMMAR.items()}
    f = _config_dir() / "grammar.json"
    if f.exists():
        try:
            user = json.loads(f.read_text())
            if isinstance(user, dict):
                for cat, rules in user.items():
                    if isinstance(rules, dict):
                        merged.setdefault(cat, {}).update(rules)
        except (ValueError, OSError):
            pass
    return merged


def _shebang_lang(abspath: Path, grammar: dict | None = None) -> str | None:
    """languageId from a `#!` line (`#!/usr/bin/env zsh` → zsh). Handles `env` and a
    version suffix; None without a shebang or for an unknown interpreter."""
    g = (grammar or load_grammar()).get("shebangs", {})
    try:
        with abspath.open("rb") as fh:
            first = fh.readline(256).decode("utf-8", "replace")
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    toks = first[2:].split()
    if not toks:
        return None
    exe = Path(toks[0]).name
    if exe == "env":                            # `#!/usr/bin/env [-S] [VAR=v] zsh -f`
        rest = [t for t in toks[1:] if not t.startswith("-") and "=" not in t]
        if not rest:                            # only flags/assignments → no interpreter
            return None
        exe = Path(rest[0]).name
    exe = re.sub(r"[0-9.]+$", "", exe)          # python3.11 → python
    return g.get(exe)


def content_lang(abspath: Path, grammar: dict | None = None) -> str | None:
    """Language for an extensionless/unknown-suffix file from (in order) its exact
    NAME, its `#!` shebang, or a first-line `#compdef`/`#autoload` marker. None if
    nothing matches. This is what lets discovery + routing reach files the
    extension map can't."""
    grammar = grammar if grammar is not None else load_grammar()
    if (lang := grammar.get("filenames", {}).get(abspath.name)):
        return lang
    if (lang := _shebang_lang(abspath, grammar)):
        return lang
    try:
        with abspath.open("rb") as fh:
            first = fh.readline(256).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.match(r"#([A-Za-z_]+)", first)        # tag directly after # (no space)
    if m:
        return grammar.get("firstLineMarkers", {}).get(m.group(1))
    return None


def server_for(relpath: str, specs: dict | None = None,
               abspath: Path | None = None) -> tuple[str, list[str], str, dict] | None:
    """Pick a server for `relpath`. FIRST by extension: iterate specs IN ORDER (user
    ~/.config/crib/lsp.json first, then shipped defaults backfilling missing labels)
    and take the FIRST that BOTH claims the extension (`extensionToLanguage`) AND has
    an installed binary (`resolve_command`) — a missing binary falls through to the
    next candidate (basedpyright→pyright for `.py`). Order = precedence. If the
    extension maps to NO server at all (extension-less scripts, unknown suffix) and
    `abspath` is given, fall back to the `#!` shebang: read its interpreter → language,
    then take the first installed spec serving that language. → (label, argv,
    languageId, spec), or None (that file is then silently skipped)."""
    specs = specs if specs is not None else load_specs()
    ext = Path(relpath).suffix.lower()
    for label, spec in specs.items():
        if not isinstance(spec, dict):   # skip "__doc__"/comment keys
            continue
        lang = (spec.get("extensionToLanguage") or {}).get(ext)
        if not lang:
            continue
        argv = resolve_command(spec)
        if argv:
            return label, argv, lang, spec
    # content fallback (shebang / bare name / #compdef|#autoload marker) — only
    # when the extension is claimed by no spec at all (extensionless scripts, etc.)
    ext_known = any(ext in (sp.get("extensionToLanguage") or {})
                    for sp in specs.values() if isinstance(sp, dict))
    if abspath is not None and not ext_known:
        lang = content_lang(abspath)
        if lang:
            for label, spec in specs.items():
                if not isinstance(spec, dict):
                    continue
                if lang in (spec.get("extensionToLanguage") or {}).values():
                    argv = resolve_command(spec)
                    if argv:
                        return label, argv, lang, spec
    return None


def derive_mtime(root: Path, relpath: str) -> int:
    """A portable index timestamp for a source file, in ns. For a COMMITTED, clean
    file: its git commit date (identical on every machine for the same commit → the
    tracked toml doesn't churn on sync). For a locally-MODIFIED/untracked file (no
    stable commit date): the on-disk `st_mtime_ns`. Outside a git repo / on any git
    error: on-disk mtime. Precedence: disk-if-modified, else git, else disk."""
    abspath = root / relpath
    try:
        disk = abspath.stat().st_mtime_ns
    except OSError:
        disk = 0
    try:
        st = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", relpath],
                            capture_output=True, text=True, timeout=5)
        if st.returncode != 0:
            return disk                          # not a git repo (or error) → disk
        if st.stdout.strip():
            return disk                          # modified/untracked → local disk mtime
        log = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ct", "--", relpath],
                             capture_output=True, text=True, timeout=5)
        ct = log.stdout.strip()
        if log.returncode == 0 and ct.isdigit():
            return int(ct) * 1_000_000_000       # commit seconds → ns (same unit as disk)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return disk


def find_root(path: Path) -> Path:
    """The TOP-LEVEL repo root for `path` — the LSP workspace root.

    A git submodule's `.git` is a FILE (a gitlink); a real repo's `.git` is a
    DIRECTORY. So we resolve to the nearest ancestor with a `.git` *directory*,
    walking straight past submodule boundaries — a vendored/submodule file is thus
    indexed as part of the enclosing top-level repo (e.g. `vendor/llmkit/…` under
    `cribsheet`), NOT as its own root. That keeps a project's `source_root` single
    and consistent; without it, per-file roots flip-flop into a submodule and
    `_revalidate` evicts the real symbols (they resolve under the wrong root)."""
    path = path.resolve()
    for d in [path, *path.parents]:
        if (d / ".git").is_dir():           # top-level repo (submodule .git is a file)
            return d
    # No enclosing git repo → fall back to a package marker (nearest pyproject/setup).
    for d in [path, *path.parents]:
        if (d / "pyproject.toml").exists() or (d / "setup.py").exists():
            return d
    return path.parent

# documentSymbol kinds we index as callables/containers (LSP SymbolKind numbers).
_FUNC_KINDS = {6, 12}          # Method, Function
# Type definitions across languages: Class(5, Py/JS/TS), Enum(10), Interface(11,
# Go iface / Rust trait / TS interface), Struct(23, Rust/Go/C). Indexed as symbols
# AND descended into — their methods/fields live inside.
_TYPE_KINDS = {5, 10, 11, 23}
# Descend to find nested items: types, callables (nested/closures), impl blocks
# (Object 19 — where Rust methods actually live), and modules/namespaces (2/3 —
# Rust inline `mod`, C++/TS namespaces). Without Object(19), every Rust `impl`
# method was silently dropped.
_CONTAINER_KINDS = _FUNC_KINDS | _TYPE_KINDS | {2, 3, 19}
# Data declarations — globals/constants and class/struct fields. Indexed ONLY at
# module/type scope (a var nested under a function is a local → noise): the scope
# guard in extract_file drops any whose ancestry includes a function/method.
_DATA_KINDS = {13, 14, 8, 7}   # Variable, Constant, Field, Property
_INDEX_KINDS = _FUNC_KINDS | _TYPE_KINDS | _DATA_KINDS


class LspClient:
    """Minimal synchronous JSON-RPC LSP client over a server's stdio."""

    def __init__(self, cmd: list[str], root: Path,
                 init_options: dict | None = None,
                 settings: dict | None = None,
                 extra_folders: list[Path] | None = None) -> None:
        self.root = root
        self.extra_folders = extra_folders or []   # ref roots (multi-root xref)
        self.init_options = init_options or {}
        self.settings = settings or {}
        self._progress: set[Any] = set()   # active $/progress tokens (busy signal)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert self.proc.stdin and self.proc.stdout
        self.w: IO[bytes] = self.proc.stdin
        self.r: IO[bytes] = self.proc.stdout
        self._id = 0
        self._resp: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._wlock = threading.Lock()   # frames must not interleave: the watcher
        threading.Thread(target=self._reader, daemon=True).start()  # pump notifies
        # concurrently with an in-flight extraction's requests (SessionPool).

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg).encode()
        with self._wlock:
            self.w.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            self.w.flush()

    def _reader(self) -> None:
        while True:
            headers: dict[str, str] = {}
            while True:
                line = self.r.readline()
                if not line:
                    return
                t = line.decode().strip()
                if not t:
                    break
                k, _, v = t.partition(":")
                headers[k.strip().lower()] = v.strip()
            body = self.r.read(int(headers.get("content-length", 0)))
            msg = json.loads(body)
            if "id" in msg and "method" in msg:
                self._answer(msg)               # server → client request
            elif "id" in msg:
                with self._lock:
                    self._resp[msg["id"]] = msg
            elif msg.get("method") == "$/progress":
                # workDoneProgress: the server's OWN "busy indexing" signal —
                # `wait_quiescent` waits on this instead of guessing with sleeps
                params = msg.get("params") or {}
                kind = (params.get("value") or {}).get("kind")
                with self._lock:
                    if kind == "begin":
                        self._progress.add(params.get("token"))
                    elif kind == "end":
                        self._progress.discard(params.get("token"))

    def _answer(self, msg: dict) -> None:
        m = msg["method"]
        result: Any = None
        if m == "workspace/configuration":
            items = msg["params"].get("items") or [{}]
            result = [self._section(it.get("section")) for it in items]
        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def _section(self, section: str | None) -> Any:
        """Dig the requested dotted `section` out of the spec's `settings`."""
        s: Any = self.settings
        if not section:
            return s
        for part in section.split("."):
            if isinstance(s, dict) and part in s:
                s = s[part]
            else:
                return {}
        return s

    def request(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if rid in self._resp:
                    return self._resp.pop(rid).get("result")
            time.sleep(0.02)
        raise TimeoutError(method)

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self) -> None:
        res = self.request("initialize", {
            "processId": os.getpid(), "rootUri": self.root.as_uri(),
            "initializationOptions": self.init_options,
            "capabilities": {"textDocument": {
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "callHierarchy": {"dynamicRegistration": True},
                "references": {"dynamicRegistration": True}},
                # workDoneProgress: ask the server to REPORT its indexing, so
                # readiness is its signal, not our sleep (`wait_quiescent`).
                "window": {"workDoneProgress": True},
                # didChangeWatchedFiles: servers that don't self-watch the fs
                # register watchers (client/registerCapability → null-acked in
                # `_answer`) and rely on the pool's `notify_changes` pump.
                "workspace": {"configuration": True,
                              "didChangeWatchedFiles": {"dynamicRegistration": True}}},
            # multi-root: the ref projects' local roots ride along, so
            # references/incomingCalls INTO ref code resolve (cross-project xref)
            "workspaceFolders": [
                {"uri": self.root.as_uri(), "name": self.root.name},
                *({"uri": f.as_uri(), "name": f.name} for f in self.extra_folders),
            ],
        })
        self.capabilities: dict = (res or {}).get("capabilities", {})
        self.notify("initialized", {})

    def wait_quiescent(self, initial: float, timeout: float) -> None:
        """Wait until the server reports no active background work: give it
        `initial` to START reporting (or to just settle, for servers that never
        send `$/progress`), then wait for every active token to end, bounded by
        `timeout`. The principled replacement for a fixed-length sleep."""
        time.sleep(initial)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                busy = bool(self._progress)
            if not busy:
                return
            time.sleep(0.05)

    def close(self) -> None:
        try:
            self.request("shutdown", {}, timeout=5)
            self.notify("exit", {})
        except Exception:  # noqa: BLE001
            pass
        self.proc.terminate()


class _Session:
    """One warm server + its reuse state. All use is serialized by `lock` —
    request/response over a single stdio pipe is inherently one-at-a-time."""

    def __init__(self, client: LspClient) -> None:
        self.client = client
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        # Sweep-scoped MEMBERSHIP pins: uri → doc version, didOpen'd to tell the
        # server these documents matter (its own discovery may miss them —
        # extensionless autoloads, no compile db). Held open across extractions;
        # released by `LspSessionPool.unpin`. An OPEN doc's truth is the CLIENT's
        # (the server ignores disk and watched-file events for it), so extracting
        # a pinned uri must didChange the current text — never analyze pin-time
        # text against disk-time hashes.
        self.pinned: dict[str, int] = {}


class LspSessionPool:
    """Warm LSP sessions, one per (workspace root, server label) — docs §3.1.

    An LSP server is Chroma's twin (docs §3): warm, stateful, and expensive to
    cold-start — `initialize` pays the whole workspace index (seconds on
    pyright, minutes on rust-analyzer). The pool keeps each initialized client
    alive across `extract_file` calls, so a sweep or revalidation pays the
    spin-up ONCE per (root, server), not per file. A dead server is detected
    (`proc.poll()`) and respawned on next use; sessions idle past `grace` are
    reaped opportunistically on each acquire (the daemon would otherwise hold
    basedpyright + rust-analyzer + gopls at once — real memory weight).

    Docs are NOT kept open across calls: each extraction didOpens what it needs
    and didCloses it after, so every call reads fresh disk and the server's
    per-doc memory stays bounded. What persists is the expensive part — the
    process and its workspace index — kept in sync with disk two ways: the big
    servers watch the fs themselves, AND crib's code watcher pumps
    `workspace/didChangeWatchedFiles` into every warm session for the changed
    root (`notify_changes`, docs §3.2) for the servers that don't."""

    def __init__(self, grace: float = 900.0) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], _Session] = {}
        self.grace = grace

    def acquire(self, root: Path, label: str, argv: list[str], spec: dict,
                extra_roots: list[Path] | None = None) -> tuple[_Session, bool]:
        """The warm session for (root, label), spawning + initializing on first
        use → (session, fresh). Callers hold `session.lock` while using it.
        Spawn/initialize happens under the pool lock: a concurrent cold start of
        another language briefly serializes — rare, and far cheaper than the
        races it removes. `extra_roots` (ref projects' local checkouts) become
        additional workspaceFolders AT CREATION — an existing session keeps the
        folders it was born with (refs changed → bounce the daemon)."""
        key = (str(root.resolve()), label)
        self._reap()
        with self._lock:
            sess = self._sessions.get(key)
            if sess is not None and sess.client.proc.poll() is not None:
                self._sessions.pop(key)          # died since last use
                sess = None
            if sess is not None:
                return sess, False
            client = LspClient(argv, root,
                               init_options=spec.get("initializationOptions"),
                               settings=spec.get("settings"),
                               extra_folders=extra_roots)
            try:
                client.initialize()
            except Exception:
                client.close()
                raise
            sess = self._sessions[key] = _Session(client)
            return sess, True

    def discard(self, root: Path, label: str) -> None:
        """Drop (and close) a session after a wedged/failed extraction, so the
        next acquire respawns clean."""
        with self._lock:
            sess = self._sessions.pop((str(root.resolve()), label), None)
        if sess is not None:
            sess.client.close()

    def notify_changes(self, root: Path, changes: list[tuple[str, int]]) -> None:
        """Pump `workspace/didChangeWatchedFiles` into every warm session for
        `root` — (relpath, type) with LSP FileChangeType 1=created 2=changed
        3=deleted — so a server that doesn't watch the fs itself still
        invalidates its workspace index for files we never didOpen (docs §3.2).
        Best-effort and lock-free (a notification is one atomic framed write;
        a dead session surfaces at its next acquire)."""
        key_root = str(root.resolve())
        with self._lock:
            sessions = [s for (r, _label), s in self._sessions.items()
                        if r == key_root]
        if not sessions:
            return
        events = [{"uri": (Path(key_root) / rel).as_uri(), "type": t}
                  for rel, t in changes]
        for sess in sessions:
            try:
                sess.client.notify("workspace/didChangeWatchedFiles",
                                   {"changes": events})
            except Exception:  # noqa: BLE001 — dead pipe → respawned on next use
                pass

    def _reap(self) -> None:
        now = time.monotonic()
        with self._lock:
            for key, sess in list(self._sessions.items()):
                if now - sess.last_used < self.grace:
                    continue
                if not sess.lock.acquire(blocking=False):
                    continue                     # in use → not actually idle
                try:
                    self._sessions.pop(key, None)
                    sess.client.close()
                finally:
                    sess.lock.release()

    def pin_docs(self, root: Path, label: str, argv: list[str], spec: dict,
                 docs: list[tuple[Path, str]],
                 extra_roots: list[Path] | None = None) -> int:
        """didOpen every (path, languageId) and HOLD them open — the protocol's
        membership signal: an open document is part of the server's analysis set
        even when its own discovery (include config, compile db, module graph)
        would never find it. Without this, cross-file edges for undiscovered
        files are silently incomplete. Sweep-scoped: `unpin(root)` releases.
        → count newly pinned."""
        sess, _fresh = self.acquire(root, label, argv, spec, extra_roots)
        n = 0
        with sess.lock:
            for pd, lang in docs:
                puri = pd.resolve().as_uri()
                if puri in sess.pinned:
                    continue
                try:
                    text = pd.read_text()
                except OSError:
                    continue
                sess.client.notify("textDocument/didOpen", {"textDocument": {
                    "uri": puri, "languageId": lang, "version": 1, "text": text}})
                sess.pinned[puri] = 1
                n += 1
            sess.last_used = time.monotonic()
        return n

    def unpin(self, root: Path) -> None:
        """Release every pinned doc for `root`'s sessions (sweep teardown)."""
        key_root = str(root.resolve())
        with self._lock:
            sessions = [s for (r, _l), s in self._sessions.items() if r == key_root]
        for sess in sessions:
            with sess.lock:
                for u in sess.pinned:
                    try:
                        sess.client.notify("textDocument/didClose",
                                           {"textDocument": {"uri": u}})
                    except Exception:  # noqa: BLE001 — dead pipe → respawn later
                        pass
                sess.pinned.clear()

    def stats(self) -> list[dict[str, Any]]:
        """Live sessions for `status`: which servers are attached where, whether
        each is alive/busy, and how long it's been idle."""
        now = time.monotonic()
        with self._lock:
            items = list(self._sessions.items())
        return [{"root": root, "server": label,
                 "pid": sess.client.proc.pid,
                 "alive": sess.client.proc.poll() is None,
                 "busy": sess.lock.locked(),
                 "idle_s": round(now - sess.last_used, 1)}
                for (root, label), sess in items]

    def close_all(self) -> None:
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for sess in sessions:
            sess.client.close()


_POOL = LspSessionPool()
atexit.register(_POOL.close_all)


def _in_workspace(uri: str, root: Path) -> bool:
    return uri.startswith(root.as_uri()) and not uri.endswith(".pyi")


# (project, locally-resolved root or None, the ref index's file set)
RefProjects = list[tuple[str, "Path | None", frozenset[str]]]


def _locate(uri: str, root: Path, ref_projects: RefProjects | None) -> str | None:
    """Location tag for an edge target: `rel` for an in-workspace uri, `proj:rel`
    when the uri attributes to a REF'D project (cross-project xref), None when
    it's neither (dropped, as before). Attribution strategies:
      (a) path under the ref's local checkout (submodule / editable install) —
          roots arrive pre-resolved;
      (b) site-packages suffix match against the ref's indexed files (git/wheel
          install: …/site-packages/llmkit/bridge/cli.py matches the ref index's
          src/llmkit/bridge/cli.py by trailing segments).
    Qualified edges key by (name, file-rel-to-the-ref-root), which sidesteps the
    path-derived fqname mismatch between checkouts. Ref roots are checked BEFORE
    the workspace: an IN-TREE checkout of a ref'd project (a vendored submodule
    with its own .crib) attributes to the ref, not to the parent — same repo,
    so the relative path matches the ref's own index either way."""
    if not ref_projects:
        return _rel(uri, root) if _in_workspace(uri, root) else None
    if not uri.startswith("file://") or uri.endswith(".pyi"):
        return None
    try:
        p = Path(uri[len("file://"):]).resolve()
    except OSError:
        return None
    for proj, rroot, _files in ref_projects:
        if rroot is not None:
            try:
                return f"{proj}:{p.relative_to(rroot)}"
            except ValueError:
                continue
    if _in_workspace(uri, root):
        return _rel(uri, root)
    m = re.search(r"/site-packages/(.+)$", str(p))
    if m:
        tail = m.group(1)
        for proj, _rroot, files in ref_projects:
            for f in files:
                if f and (f.endswith(tail) or tail.endswith(f)):
                    return f"{proj}:{f}"
    return None


def _rel(uri: str, root: Path) -> str:
    p = uri[len("file://"):]
    try:
        return str(Path(p).resolve().relative_to(root))
    except ValueError:
        return Path(p).name


def _walk(syms: list, parents: tuple[str, ...] = (),
          kinds: tuple[int, ...] = ()) -> list[tuple[dict, tuple, tuple]]:
    """Flatten documentSymbol tree → [(symbol, container_names, container_kinds)],
    descending containers. `container_kinds` lets a caller tell a module/class-level
    declaration from a function-local (a Variable nested under a function)."""
    out = []
    for s in syms or []:
        out.append((s, parents, kinds))
        if s.get("kind") in _CONTAINER_KINDS and s.get("children"):
            out.extend(_walk(s["children"], parents + (s.get("name", ""),),
                             kinds + (s.get("kind") or 0,)))
    return out


_KIND = {5: "class", 6: "method", 12: "function", 10: "enum", 11: "interface",
         23: "struct", 13: "variable", 14: "constant", 8: "field", 7: "property"}
# Per-language label niceties: same SymbolKind, different idiom (a trait IS an
# Interface(11) to the LSP, but reads better as "trait"). Naming quirk, not a kind
# distinction — so it lives here, not in the universal _INDEX_KINDS sets.
_KIND_LABEL_OVERRIDE = {("rust", 11): "trait"}
_REF_CAP = 80  # references resolved per symbol — bounds worst-case on hot helpers
# Lua binds modules to a local table var (`M.setup`); those aren't real qualifiers.
_LUA_TABLE_VARS = {"M", "_M", "self", "Module", "mod"}


def _symbol_ranges(c: LspClient, uri: str, language_id: str,
                   cache: dict[str, list], opened: set[str]) -> list[tuple[str, int, int]]:
    """(local_name, start_line, end_line) for every symbol in `uri` via documentSymbol
    (opening the file first if needed). Cached per session — used to map a reference
    location back to the symbol that encloses it (the referrer)."""
    if uri in cache:
        return cache[uri]
    if uri not in opened:
        opened.add(uri)
        path = uri[len("file://"):] if uri.startswith("file://") else ""
        try:
            text = Path(path).read_text() if path else ""
        except OSError:
            text = ""
        if text:
            c.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language_id, "version": 1, "text": text}})
            time.sleep(0.15)
    out: list[tuple[str, int, int]] = []
    for s, _p, _k in _walk(c.request("textDocument/documentSymbol",
                                     {"textDocument": {"uri": uri}}) or []):
        rng = s.get("range", {})
        out.append((_local_name(s.get("name", ""), language_id),
                    rng.get("start", {}).get("line", 0),
                    rng.get("end", {}).get("line", 0)))
    cache[uri] = out
    return out


def _enclosing_symbol(c: LspClient, uri: str, line: int, language_id: str,
                      cache: dict[str, list], opened: set[str]) -> str | None:
    """Innermost symbol in `uri` whose range contains `line` — the referrer symbol."""
    best: tuple[str, int] | None = None
    for name, s0, e0 in _symbol_ranges(c, uri, language_id, cache, opened):
        if s0 <= line <= e0 and (best is None or (e0 - s0) < best[1]):
            best = (name, e0 - s0)
    return best[0] if best else None


def _module_of(relpath: str, lang: str) -> str:
    """Module/namespace from the FILE PATH (language-specific). The container parts
    come from documentSymbol; the module part can only come from the path."""
    p = Path(relpath)
    parts = list(p.with_suffix("").parts)
    # drop common source-root prefixes so the module reads naturally
    while parts and parts[0] in ("src", "lua", "lib"):
        parts = parts[1:]
    # the index-file whose name is the *directory's* module is language-specific:
    # Rust mod.rs, Lua init.lua, Python __init__.py — but a Python `mod.py` is a
    # real module and must NOT be dropped.
    index_file = {"rust": "mod", "lua": "init"}.get(lang, "__init__")
    if parts and parts[-1] == index_file:
        parts = parts[:-1]
    sep = "::" if lang == "rust" else "."
    return sep.join(parts)


def _local_name(raw: str, lang: str) -> str:
    """The bare symbol name — strip a Lua module-table prefix (`M.setup`→`setup`,
    `T:method`→`method`) that documentSymbol folds into the name, and reduce a Rust
    `impl` block's name to the TYPE it's for, so its methods qualify as `Type::method`
    (rust-analyzer names impl symbols `impl Type` / `impl Trait for Type`)."""
    if lang == "lua":
        return raw.replace(":", ".").split(".")[-1]
    if lang == "rust" and re.match(r"impl\b", raw):
        body = re.sub(r"^impl\s*<[^>]*>", "impl", raw)[4:].strip()  # drop impl-generics
        if " for " in body:                       # `Trait for Type` → the Type
            body = body.split(" for ")[-1].strip()
        base = re.sub(r"<.*$", "", body).strip().split("::")[-1]    # strip type args/path
        return base or raw
    return raw


def _qualify(lang: str, module: str, container: tuple[str, ...], name: str) -> str:
    """Render a language-idiomatic qualified name from the parts. Parts are canonical;
    this is derived display + a lookup alias."""
    cont = [c for c in container if _local_name(c, lang) not in _LUA_TABLE_VARS]
    cont = [_local_name(c, lang) for c in cont]
    sep = "::" if lang == "rust" else "."
    pieces = ([module] if module else []) + cont + [name]
    return sep.join(p for p in pieces if p)


_SUBTOK = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def _name_terms(name: str, qualified: str) -> list[str]:
    """BM25/keyword field: the names + compound-identifier subtoken splits, so a
    bare/partial name query matches (e.g. `SharedServerManager` → shared server
    manager). The unqualified name is what lets the short query hit."""
    toks = {name, qualified}
    for s in (name, qualified):
        for w in re.findall(r"[A-Za-z0-9]+", s):
            for t in _SUBTOK.findall(w) or [w]:
                toks.add(t.lower())
    return sorted(t for t in toks if t)


class NoServer(RuntimeError):
    """No LSP server for a file's extension is available on this machine."""


# Post-didOpen settle on a WARM session: the workspace index already exists, only
# this one file's analysis is pending — a fraction of the fresh-session settle.
_REUSE_SETTLE = 0.3


def _hierarchy_edges(c: LspClient, method: str, item: dict, key: str,
                     root: Path, ref_projects: RefProjects | None) -> list[str]:
    """One callHierarchy direction → located `name [loc]` edge strings (`key` is
    the LSP result field naming the counterpart: `to` outgoing, `from` incoming)."""
    out = []
    for e in c.request(method, {"item": item}) or []:
        t = e.get(key, {})
        loc = _locate(t.get("uri", ""), root, ref_projects)
        if loc:
            out.append(f"{t.get('name')} [{loc}]")
    return out


def extract_file(root: Path, relpath: str, settle: float = 1.5,
                 pool: LspSessionPool | None = None,
                 ref_projects: RefProjects | None = None) -> list[dict]:
    """Symbols + workspace-resolved call edges for one file, via the LSP server the
    specs select for its extension (docs §3.3). Raises NoServer if none resolves.
    The session comes WARM from the pool (docs §3.1) — spawn + `initialize` (the
    whole-workspace index, the expensive part) is paid once per (root, server),
    not per file. A wedged/dead server is discarded and the extraction retried
    once on a fresh one. Edges resolving OUTSIDE the root attribute to
    `ref_projects` (the `.crib` `refs:`) as qualified `name [proj:rel]` edges
    instead of being dropped."""
    path = (root / relpath).resolve()
    sel = server_for(relpath, abspath=path)
    if sel is None:
        raise NoServer(f"no LSP server for {Path(relpath).suffix or '(no ext)'} "
                       f"(configure ~/.config/crib/lsp.json)")
    label, argv, language_id, spec = sel
    pool = pool or _POOL
    try:
        return _extract(pool, root, relpath, path, settle, label, argv,
                        language_id, spec, ref_projects)
    except (TimeoutError, OSError, ValueError):
        pool.discard(root, label)        # crash supervision: respawn once and retry
        return _extract(pool, root, relpath, path, settle, label, argv,
                        language_id, spec, ref_projects)


@dataclass(frozen=True)
class _ExtractCtx:
    """Per-file invariants shared by every `_symbol_entry` call for one extraction —
    the warm client + the file's identity/location context. `sym_cache` and `opened`
    are mutated in place across the loop (encloser-range cache; the didOpen'd-doc set
    the teardown closes), so they're the SAME objects `_extract` reads afterwards."""
    c: LspClient
    uri: str
    root: Path
    ref_projects: RefProjects | None
    module: str
    language_id: str
    relpath: str
    lines: list[str]
    mtime: Any
    has_refs: bool
    sym_cache: dict[str, list]
    opened: set[str]


def _reference_edges(ctx: _ExtractCtx, pos: dict, local: str) -> list[str]:
    """`textDocument/references` for the symbol at `pos` → located `enc [loc]` edges.
    A FIRST-CLASS relation (everywhere the symbol is mentioned), for any server with
    referencesProvider — deliberately SEPARATE from `called_by` (call-hierarchy only):
    a reference is broader than a call (reads/mentions too), the distinction left to
    the consumer/LLM. This is the only caller signal for symbols-only servers (shuck).
    Each hit is attributed to its enclosing symbol; self-references are dropped."""
    refs = ctx.c.request("textDocument/references", {
        "textDocument": {"uri": ctx.uri}, "position": pos,
        "context": {"includeDeclaration": False}}) or []
    out: list[str] = []
    for loc in refs[:_REF_CAP]:
        ruri = loc.get("uri", "")
        rloc = _locate(ruri, ctx.root, ctx.ref_projects)
        if rloc is None:
            continue
        rline = loc.get("range", {}).get("start", {}).get("line", 0)
        enc = _enclosing_symbol(ctx.c, ruri, rline, ctx.language_id,
                                ctx.sym_cache, ctx.opened)
        if not enc or (enc == local and rloc == ctx.relpath):
            continue                                # skip self-references
        out.append(f"{enc} [{rloc}]")
    return out


# Per-language comment syntax for the leading-doc capture (docs code-symbol-index §3).
# The LSP `documentSymbol` range starts at the def/decorator, so the human-authored
# comment ABOVE a symbol — often its richest intent — is outside the body we index. We
# recover it two ways (§): the LSP `hover` documentation where the server carries it
# (doc-comment languages), and a config-driven reverse upward-scan for everything else
# (notably Python `#`, which hover omits). `line`: line-comment prefixes, longest-first
# so `///` wins over `//`. `block`: (open, close) delimiter pairs, multi-line. `skip`:
# lines between comment and symbol that still attach it (decorators/attributes) — walked
# over so a comment above them is reached. Prefix/delimiter matching, no tree-sitter.
_COMMENT_SYNTAX: dict[str, dict] = {
    "python": {"line": ("#",),               "block": (),                "skip": ("@",)},
    "zsh":    {"line": ("#",),               "block": (),                "skip": ()},
    "rust":   {"line": ("///", "//!", "//"), "block": (("/*", "*/"),),   "skip": ("#[", "#![")},
    "go":     {"line": ("//",),              "block": (("/*", "*/"),),   "skip": ()},
    "c":      {"line": ("//",),              "block": (("/*", "*/"),),   "skip": ()},
    "cpp":    {"line": ("//",),              "block": (("/*", "*/"),),   "skip": ()},
    "lua":    {"line": ("--",),              "block": (("--[[", "]]"),), "skip": ()},
}
# Servers whose `hover` documentation carries the doc-comment above a decl (so it's worth
# the extra round-trip). Python/zsh omitted: pyright hover returns only the in-body
# docstring, never the `#` block — the reverse-scan covers those with no LSP cost.
_HOVER_DOC_LANGS = frozenset({"rust", "go", "c", "cpp", "lua"})


def _strip_comment_markers(text: str, syn: dict) -> str:
    """Strip line-comment prefixes and block delimiters (plus decorative leading `*`)
    from a collected comment block, leaving the prose. Blank result → ""."""
    out: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        for op, cl in syn["block"]:
            if s.startswith(op):
                s = s[len(op):].strip()
            if s.endswith(cl):
                s = s[: len(s) - len(cl)].strip()
        pfx = next((p for p in syn["line"] if s.startswith(p)), None)
        if pfx is not None:
            s = s[len(pfx):].strip()
        elif s.startswith("*"):                 # `/* * */`-style continuation bullets
            s = s[1:].strip()
        out.append(s)
    return "\n".join(out).strip()


def _leading_comment(lines: list[str], start: int, lang: str) -> str:
    """The contiguous comment block immediately above the symbol at `start` (0-based),
    reverse-scanned per the language's comment syntax. Walks over decorator/attribute
    lines (`skip`) and a single blank-line gap, so a comment above a decorator still
    attaches; >1 blank line severs it. Handles line comments and multi-line block
    comments. Returns the prose (markers stripped) or ""."""
    syn = _COMMENT_SYNTAX.get(lang)
    if syn is None or start <= 0:
        return ""
    collected: list[str] = []          # bottom-up; reversed at the end
    i = start - 1
    blanks = 0
    while i >= 0:
        s = lines[i].strip()
        if not s:
            blanks += 1
            if blanks > 1:
                break
            i -= 1
            continue
        if syn["skip"] and s.startswith(syn["skip"]):   # decorator/attribute — step over
            blanks = 0
            i -= 1
            continue
        # multi-line block comment: we meet the CLOSE first going up — gather to the OPEN.
        blk = next((b for b in syn["block"] if s.endswith(b[1])), None)
        if blk is not None:
            op = blk[0]
            while i >= 0:
                collected.append(lines[i])
                if lines[i].strip().startswith(op) or op in lines[i]:
                    break
                i -= 1
            blanks = 0
            i -= 1
            continue
        if any(s.startswith(p) for p in syn["line"]):
            collected.append(lines[i])
            blanks = 0
            i -= 1
            continue
        break                          # real code — stop
    if not collected:
        return ""
    return _strip_comment_markers("\n".join(reversed(collected)), syn)


def _hover_doc(ctx: "_ExtractCtx", pos: dict) -> str:
    """LSP `hover` documentation for the symbol at `pos`, prose only (fenced signature
    code stripped). "" when unsupported/empty. Used only for `_HOVER_DOC_LANGS` — the
    servers whose hover actually carries the doc-comment above a decl."""
    if not ctx.c.capabilities.get("hoverProvider"):
        return ""
    try:
        res = ctx.c.request("textDocument/hover",
                            {"textDocument": {"uri": ctx.uri}, "position": pos})
    except (TimeoutError, OSError, ValueError):
        return ""
    if not res:
        return ""
    contents = res.get("contents")
    parts: list[str] = []
    for c in (contents if isinstance(contents, list) else [contents]):
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict) and "value" in c:
            parts.append(c["value"])
    text = "\n".join(parts)
    # drop fenced code blocks (the rendered signature); keep the prose doc-comment.
    prose, fenced = [], False
    for ln in text.splitlines():
        if ln.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            prose.append(ln)
    return "\n".join(prose).strip()


def _symbol_entry(ctx: _ExtractCtx, s: dict, parents: tuple[str, ...],
                  pkinds: tuple[int, ...]) -> dict | None:
    """Assemble one indexed-symbol record from a documentSymbol node (+ its call/
    reference edges), or None when the node is filtered out (uninteresting kind, or a
    data decl nested in a function → a local, noise)."""
    kind = s.get("kind")
    if kind not in _INDEX_KINDS:
        return None
    # scope guard: a data declaration (global/const/field) is indexed only at module
    # or class scope — one nested under a function is a LOCAL (noise).
    if kind in _DATA_KINDS and any(pk in _FUNC_KINDS for pk in pkinds):
        return None
    rng = s.get("range", {})
    start = rng.get("start", {}).get("line", 0)
    body = "\n".join(ctx.lines[start:rng.get("end", {}).get("line", 0) + 1])
    # Leading-doc capture (§3): the LSP range starts at the def, so the comment ABOVE the
    # symbol is excluded — fold it in (reverse-scan, plus hover for doc-comment servers)
    # so its authored intent gates description regen (content_hash) and enriches search.
    pos = (s.get("selectionRange") or rng)["start"]
    lead = _leading_comment(ctx.lines, start, ctx.language_id)
    if ctx.language_id in _HOVER_DOC_LANGS:
        hov = _hover_doc(ctx, pos)
        if hov and hov not in lead and hov not in body:
            lead = (lead + "\n\n" + hov).strip() if lead else hov
    if lead and lead not in body:
        body = lead + "\n\n" + body
    container = tuple(parents)
    local = _local_name(s.get("name", ""), ctx.language_id)
    # IDENTITY = the language-normalized qualified name (stable across body edits;
    # changes only on rename/move). content_hash is a SEPARATE field that gates
    # description regeneration — the two jobs, split (docs §2.1).
    fqname = _qualify(ctx.language_id, ctx.module, container, local)
    parent = (_qualify(ctx.language_id, ctx.module, container[:-1],
                       _local_name(container[-1], ctx.language_id))
              if container else "")
    content_hash = hashlib.sha1(body.encode()).hexdigest()[:16]
    sig = ctx.lines[start].strip()[:120]
    calls: list[str] = []
    called_by: list[str] = []
    if kind in _FUNC_KINDS and ctx.c.capabilities.get("callHierarchyProvider"):
        prep = ctx.c.request("textDocument/prepareCallHierarchy",
                             {"textDocument": {"uri": ctx.uri}, "position": pos})
        if prep:
            item = prep[0]
            calls = _hierarchy_edges(ctx.c, "callHierarchy/outgoingCalls",
                                     item, "to", ctx.root, ctx.ref_projects)
            called_by = _hierarchy_edges(ctx.c, "callHierarchy/incomingCalls",
                                         item, "from", ctx.root, ctx.ref_projects)
    references = _reference_edges(ctx, pos, local) if ctx.has_refs else []
    # module-level variables read more naturally as "global" than "variable"
    kind_label = ("global" if kind == 13 and not container
                  else _KIND_LABEL_OVERRIDE.get((ctx.language_id, kind or 0))
                  or _KIND.get(kind or 0, "?"))
    return {
        "fqname": fqname, "name": local,
        "kind": kind_label,
        "lang": ctx.language_id, "module": ctx.module, "container": list(container),
        "parent": parent, "content_hash": content_hash,
        "file": ctx.relpath, "line": start + 1, "mtime": ctx.mtime, "signature": sig,
        "calls": sorted(set(calls)), "called_by": sorted(set(called_by)),
        "references": sorted(set(references)),
        "name_terms": _name_terms(local, fqname),
        "_body": body,   # transient: for the description mop-up; not persisted
    }


def _extract(pool: LspSessionPool, root: Path, relpath: str, path: Path,
             settle: float, label: str, argv: list[str], language_id: str,
             spec: dict, ref_projects: RefProjects | None = None) -> list[dict]:
    """Session/doc lifecycle skeleton: acquire a warm session, open (or sync a pinned)
    doc, wait for quiescence, then walk documentSymbol into `_symbol_entry` records —
    closing only what THIS call opened (pins stay) in the finally."""
    lines = path.read_text().splitlines()
    # ref checkouts OUTSIDE the workspace ride along as extra workspaceFolders
    # (multi-root xref); in-tree ones are already inside the root
    extra = list(dict.fromkeys(
        rr for _p, rr, _f in (ref_projects or [])
        if rr is not None and not rr.is_relative_to(root.resolve())))
    sess, fresh = pool.acquire(root, label, argv, spec, extra_roots=extra)
    entries: list[dict] = []
    with sess.lock:
        c = sess.client
        uri = path.as_uri()
        # seeded with the sweep's membership pins: already open, never re-opened
        # here, and never closed by this call's teardown
        opened: set[str] = set(sess.pinned)
        try:
            if uri in sess.pinned:
                # open doc ⇒ client truth: sync the server to the SAME text we
                # hash below (the file may have changed since it was pinned)
                sess.pinned[uri] += 1
                c.notify("textDocument/didChange", {
                    "textDocument": {"uri": uri, "version": sess.pinned[uri]},
                    "contentChanges": [{"text": "\n".join(lines)}]})
            else:
                opened.add(uri)
                c.notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri, "languageId": language_id, "version": 1,
                    "text": "\n".join(lines)}})
            # readiness: honor the server's own $/progress over a blind sleep —
            # fresh sessions may be mid-workspace-index (minutes on big repos)
            c.wait_quiescent(
                initial=settle if fresh else min(settle, _REUSE_SETTLE),
                timeout=60.0 if fresh else 10.0)
            syms = c.request("textDocument/documentSymbol",
                             {"textDocument": {"uri": uri}})
            ctx = _ExtractCtx(
                c=c, uri=uri, root=root, ref_projects=ref_projects,
                module=_module_of(relpath, language_id), language_id=language_id,
                relpath=relpath, lines=lines,
                mtime=derive_mtime(root, relpath),   # portable index timestamp
                has_refs=bool(c.capabilities.get("referencesProvider")),
                sym_cache={}, opened=opened)         # uri → ranges; didOpen'd docs
            for s, parents, pkinds in _walk(syms):
                e = _symbol_entry(ctx, s, parents, pkinds)
                if e is not None:
                    entries.append(e)
        finally:
            for u in opened - sess.pinned.keys():   # close what THIS call opened —
                try:                         # call reads fresh disk; server doc-
                    c.notify("textDocument/didClose",   # memory stays bounded.
                             {"textDocument": {"uri": u}})   # PINNED docs stay
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
            sess.last_used = time.monotonic()
    return entries


# --- semantic facet: per-symbol descriptions (docs §4) -----------------------

DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"symbols": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                       "description": {"type": "string"},
                       "keywords": {"type": "array", "items": {"type": "string"}}},
        "required": ["name", "description"]}}},
    "required": ["symbols"],
}
_KEYWORD_INSTR = (
    "Also output `keywords`: 4-8 SEARCH KEYWORD PHRASES a developer would type to find "
    "this symbol BY INTENT — behaviors, concepts, domain synonyms, the problem it solves. "
    "NOT the identifier spelled out, NOT generic words like 'function'/'helper'; for a "
    "test, describe what it verifies. These expand the symbol's searchable vocabulary. ")
DESCRIBE_SYSTEM = (
    "You are given a source file. For EACH top-level definition — class, function, "
    "or method — IN ORDER, output its qualified name (Class.method), its kind "
    "(class|function|method), and ONE concise sentence describing what it does: the "
    "intent, not a restatement of the signature. A class description says what the "
    "type represents or manages. " + _KEYWORD_INSTR
    + "Return every definition as JSON matching the schema. "
    # NB: the literal word 'json' is required here — Alibaba/qwen rejects a
    # response_format=json_object request whose messages never mention 'json'.
    )


def _rows_to_meta(data: Any) -> dict[str, dict[str, Any]]:
    """Structured describe rows → name → {description, keywords}. ONE LLM pass yields
    both facets (description feeds dense; keywords feed the expanded BM25 field)."""
    out: dict[str, dict[str, Any]] = {}
    for s in _describe_rows(data):
        if isinstance(s, dict) and s.get("name") and s.get("description"):
            out[s["name"]] = {"description": s["description"],
                              "keywords": [str(k) for k in (s.get("keywords") or [])]}
    return out


def describe_file(gen_cfg: Any, root: Path, relpath: str) -> dict[str, dict[str, Any]]:
    """LLM {description, keywords} per symbol (name → intent + search keywords) in ONE
    pass. Independent of the LSP — just the file text — so the semantic facet works even
    where no server exists. Reuses the bulk structured generation."""
    from .generate import generate_structured
    src = (root / relpath).read_text()
    data = generate_structured(gen_cfg, DESCRIBE_SYSTEM, src, DESCRIBE_SCHEMA,
                               purpose="elaborate", schema_name="describe_symbols")
    return _rows_to_meta(data)


def _describe_rows(data: Any) -> list:
    """The symbol rows from a structured describe response — tolerating both shapes:
    a wrapped object `{"symbols": [...]}` (GLM) OR a bare array `[...]` (qwen/Alibaba
    returns the array directly under json_object)."""
    if isinstance(data, dict):
        return data.get("symbols", []) or []
    return data if isinstance(data, list) else []


def describe_symbols(gen_cfg: Any, symbols: list[dict]) -> dict[str, dict[str, Any]]:
    """MOP-UP describe: a focused structured call over ONLY the given symbols (their
    bodies), for ones the whole-file bulk pass missed. Keyed by the symbol's local
    `name`. Returns name → {description, keywords} (both facets, one pass)."""
    if not symbols:
        return {}
    from .generate import generate_structured
    blob = "\n\n".join(f"# {s.get('kind','')} {s.get('name','')}\n{s.get('_body','')}"
                       for s in symbols)
    sysp = ("For EACH `# kind name`-delimited definition below, output its name and "
            "ONE concise sentence on what it does (intent, not the signature). "
            + _KEYWORD_INSTR + "Cover every one, as JSON matching the schema.")  # 'json' for qwen
    data = generate_structured(gen_cfg, sysp, blob, DESCRIBE_SCHEMA,
                               purpose="elaborate", schema_name="describe_symbols")
    return _rows_to_meta(data)


def match_meta(fqname: str, metas: dict[str, Any]) -> tuple[str, list[str]]:
    """(description, keywords) for a structural symbol: exact fqname else last segment.
    Tolerates a bare-string value (legacy description-only rows)."""
    def _split(v: Any) -> tuple[str, list[str]]:
        if isinstance(v, dict):
            return v.get("description", ""), list(v.get("keywords") or [])
        return (v or ""), []
    if fqname in metas:
        return _split(metas[fqname])
    tail = fqname.split(".")[-1]
    for name, v in metas.items():
        if name == tail or name.split(".")[-1] == tail:
            return _split(v)
    return "", []


def match_description(fqname: str, descs: dict[str, Any]) -> str:
    """Back-compat: just the description (see `match_meta` for keywords too)."""
    return match_meta(fqname, descs)[0]


# --- content-addressed store (separate location, like keyword_index) ----------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _unesc(s: str) -> str:
    """Exact inverse of `_esc` — undoes the quote-escape then the backslash-escape (the
    REVERSE order of `_esc`). Without this, `_parse` returned still-escaped strings, so a
    read-modify-write cycle (e.g. `_patch_called_by` rewriting a heavily-called symbol on
    every reindex) re-escaped each time and DOUBLED the backslashes — a signature with a
    quote grew 1→3→7→…→64GB over a session of repeated reindexes."""
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _unquote(v: str) -> str:
    """Strip exactly one pair of delimiter quotes (not `.strip('"')`, which over-eats a
    trailing escaped quote) and un-escape the contents."""
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return _unesc(v)


_SCALARS = ("fqname", "name", "kind", "lang", "module", "parent", "content_hash",
            "file", "signature", "description")
_ARRAYS = ("container", "calls", "called_by", "references", "name_terms", "keywords")


def _render(e: dict) -> str:
    lines = [f'{k} = "{_esc(str(e.get(k, "")))}"' for k in _SCALARS]
    lines.append(f'line = {e.get("line", 0)}')
    # `mtime` is DERIVED (see derive_mtime): the git commit date for committed code
    # (identical across machines → the tracked toml doesn't churn on sync) or the
    # on-disk mtime for locally-modified files. It's a record; the staleness GATE uses
    # the toml file's own mtime (Crib._revalidate), so it never needs git at query time.
    lines.append(f'mtime = {e.get("mtime", 0)}')
    for key in _ARRAYS:
        # `keywords` is OPTIONAL and its presence is meaningful: a rendered
        # `keywords = []` means a describe pass ran and yielded none (don't retry
        # forever); NO line means never attempted (the backfill-stale signal). The
        # structural arrays are always set by extraction, so only keywords skips.
        if key == "keywords" and key not in e:
            continue
        vals = e.get(key) or []
        if vals:
            lines.append(f"{key} = [")
            lines += [f'  "{_esc(str(v))}",' for v in vals]
            lines.append("]")
        else:
            lines.append(f"{key} = []")
    return "\n".join(lines) + "\n"


# ── Durable human learnings attached to a symbol ──────────────────────────────
# A learning is a first-class NOTE (under <project>/code-learnings/), keyed to a
# symbol's fqn — deliberately SEPARATE from the LLM description (a regenerable
# cache): re-indexing never touches it, and it rides the normal watch/index/sync/
# merge. See docs/code-symbol-index.md § Learnings.
LEARNINGS_DIR = "code-learnings"
_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def learning_slug(fqn: str) -> str:
    """fqn → a filesystem- and git-sync-safe basename (no extension). Whitelist
    `[A-Za-z0-9._-]`; everything else (`::` `/` `<>` `*` `&` spaces `~` operators
    …) collapses to `-`. When the munge is lossy, append a short fqn hash so
    distinct symbols can't collide and the exact name is recoverable — the note's
    `symbol:` frontmatter stays authoritative regardless. Clean dotted fqns pass
    through verbatim: `crib.retrieve.LexicalCache.get`."""
    safe = _SLUG_UNSAFE.sub("-", fqn).strip("-")
    if safe != fqn:
        safe = f"{safe}-{hashlib.sha1(fqn.encode()).hexdigest()[:8]}"
    return safe


class SymbolIndex:
    """Structural store: symbol_index/<slug(fqn)>.toml under the project data dir.
    Filename is the LEGIBLE munged fqn (same scheme as learnings) — deterministic
    across machines (same symbol → same path → git 3-way merges it), and a git diff
    names the symbol (`crib.retrieve.LexicalCache.get.toml`) instead of an opaque
    hash. The `merge=cribnote` driver auto-resolves any field divergence on sync."""

    def __init__(self, project_dir: Path) -> None:
        self.root = project_dir / "symbol_index"

    def _relname(self, fqname: str) -> str:
        return f"{learning_slug(fqname)}.toml"

    def write(self, entry: dict) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        # Filename keyed by the FQN (identity), so a body edit UPDATES the same file
        # (clean git diff) instead of orphaning it — content_hash is a field inside.
        p = self.root / self._relname(entry["fqname"])
        p.write_text(_render(entry))
        return p

    def write_all(self, entries: list[dict]) -> int:
        for e in entries:
            self.write(e)
        return len(entries)

    def read(self, fqname: str) -> dict | None:
        """One symbol's entry by EXACT fqname — O(1) (filename is the fqn slug), for
        the deferred-describe clobber guard: re-read at patch time and skip if the
        body changed again since it was queued. None when absent (dropped/renamed)."""
        p = self.root / self._relname(fqname)
        try:
            return _parse(p.read_text())
        except OSError:
            return None

    def by_fqname(self, name: str) -> list[dict]:
        """Read entries whose fqname ends with `name` (bare name or dotted path)."""
        out = []
        for p in self.root.glob("*.toml") if self.root.exists() else []:
            e = _parse(p.read_text())
            fq = e.get("fqname", "")
            if fq == name or fq.endswith("." + name) or fq.split(".")[-1] == name:
                out.append(e)
        return out

    def all(self) -> list[dict]:
        """Every persisted symbol entry (for concept search over descriptions)."""
        return [_parse(p.read_text()) for p in self.root.glob("*.toml")] \
            if self.root.exists() else []

    def is_populated(self) -> bool:
        """Cheap check (no parse) — does this project have any indexed symbols?"""
        return self.root.exists() and any(self.root.glob("*.toml"))

    def delete(self, fqname: str) -> bool:
        """Drop one symbol's entry by exact fqname (rename/removal). Returns hit."""
        p = self.root / self._relname(fqname)
        if p.exists():
            p.unlink()
            return True
        return False

    _ROOT_META = ".source_root"

    def set_source_root(self, root: Path) -> None:
        """Persist the source repo root, so staleness revalidation can stat the source
        files later (at query time) without needing the caller's cwd/.crib."""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / self._ROOT_META).write_text(str(root))

    def source_root(self) -> Path | None:
        f = self.root / self._ROOT_META
        try:
            return Path(f.read_text().strip()) if f.exists() else None
        except OSError:
            return None


def _parse(text: str) -> dict:
    """Tiny reader for the flat TOML we write (no external toml dep on the hot path)."""
    e: dict[str, Any] = {}
    key = None
    arr: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if key:
            if s == "]":
                e[key] = arr
                key, arr = None, []
            elif s:
                arr.append(_unquote(s.rstrip(",").strip()))
            continue
        if s.endswith("= []"):                 # empty array on one line
            e[s.split(" = ")[0].strip()] = []
        elif s.endswith("= ["):                # multi-line array start
            key = s.split(" = ")[0].strip()
            arr = []
        elif " = " in s:
            k, _, v = s.partition(" = ")
            v = v.strip()
            e[k] = int(v) if v.isdigit() else _unquote(v)
    return e
