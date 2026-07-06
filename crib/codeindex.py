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

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
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
    "shuck": {"command": "shuck", "args": ["server"],
              "extensionToLanguage": {".zsh": "zsh"}},
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


# Interpreter basename → languageId, for shebang routing of extension-less scripts.
# Version suffixes are stripped first (python3.11 → python).
_INTERP_LANG = {
    "zsh": "zsh", "bash": "bash", "sh": "sh", "ksh": "ksh",
    "python": "python", "node": "javascript", "nodejs": "javascript",
    "ruby": "ruby", "perl": "perl", "lua": "lua",
}


def _shebang_lang(abspath: Path) -> str | None:
    """languageId from a `#!` line, for a file whose extension maps to no server
    (`#!/usr/bin/env zsh` → zsh, `#!/usr/bin/python3` → python). Handles `env` and a
    version suffix; None if there's no shebang or the interpreter isn't known."""
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
    if exe == "env" and len(toks) > 1:          # `#!/usr/bin/env python3`
        exe = Path(toks[1]).name
    exe = re.sub(r"[0-9.]+$", "", exe)          # python3.11 → python
    return _INTERP_LANG.get(exe)


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
    # shebang fallback — only when the extension is claimed by no spec at all
    ext_known = any(ext in (sp.get("extensionToLanguage") or {})
                    for sp in specs.values() if isinstance(sp, dict))
    if abspath is not None and not ext_known:
        lang = _shebang_lang(abspath)
        if lang:
            for label, spec in specs.items():
                if not isinstance(spec, dict):
                    continue
                if lang in (spec.get("extensionToLanguage") or {}).values():
                    argv = resolve_command(spec)
                    if argv:
                        return label, argv, lang, spec
    return None


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
_CONTAINER_KINDS = {5, 6, 12}  # Class, Method, Function (descend into these)
# Data declarations — globals/constants and class attributes/fields. Indexed ONLY
# at module or class scope (a var nested under a function is a local → noise): the
# scope guard in extract_file drops any whose ancestry includes a function/method.
_DATA_KINDS = {13, 14, 8, 7}   # Variable, Constant, Field, Property
_INDEX_KINDS = _FUNC_KINDS | {5} | _DATA_KINDS   # Class(5) + callables + data


class LspClient:
    """Minimal synchronous JSON-RPC LSP client over a server's stdio."""

    def __init__(self, cmd: list[str], root: Path,
                 init_options: dict | None = None,
                 settings: dict | None = None) -> None:
        self.root = root
        self.init_options = init_options or {}
        self.settings = settings or {}
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert self.proc.stdin and self.proc.stdout
        self.w: IO[bytes] = self.proc.stdin
        self.r: IO[bytes] = self.proc.stdout
        self._id = 0
        self._resp: dict[int, dict] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg).encode()
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
                "workspace": {"configuration": True}},
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
        })
        self.capabilities: dict = (res or {}).get("capabilities", {})
        self.notify("initialized", {})

    def close(self) -> None:
        try:
            self.request("shutdown", {}, timeout=5)
            self.notify("exit", {})
        except Exception:  # noqa: BLE001
            pass
        self.proc.terminate()


def _in_workspace(uri: str, root: Path) -> bool:
    return uri.startswith(root.as_uri()) and not uri.endswith(".pyi")


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


_KIND = {5: "class", 6: "method", 12: "function",
         13: "variable", 14: "constant", 8: "field", 7: "property"}
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
    `T:method`→`method`) that documentSymbol folds into the name."""
    if lang == "lua":
        return raw.replace(":", ".").split(".")[-1]
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


def extract_file(root: Path, relpath: str, settle: float = 1.5) -> list[dict]:
    """Symbols + workspace-resolved call edges for one file, via the LSP server the
    specs select for its extension (docs §3.3). Raises NoServer if none resolves."""
    path = (root / relpath).resolve()
    sel = server_for(relpath, abspath=path)
    if sel is None:
        raise NoServer(f"no LSP server for {Path(relpath).suffix or '(no ext)'} "
                       f"(configure ~/.config/crib/lsp.json)")
    _label, argv, language_id, spec = sel
    lines = path.read_text().splitlines()
    c = LspClient(argv, root, init_options=spec.get("initializationOptions"),
                  settings=spec.get("settings"))
    entries: list[dict] = []
    mtime = path.stat().st_mtime_ns   # staleness gate: reindex when the source moves
    try:
        c.initialize()
        uri = path.as_uri()
        c.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language_id, "version": 1,
            "text": "\n".join(lines)}})
        time.sleep(settle)
        module = _module_of(relpath, language_id)
        syms = c.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        has_refs = bool(c.capabilities.get("referencesProvider"))
        sym_cache: dict[str, list] = {}     # uri → symbol ranges (encloser resolution)
        opened: set[str] = {uri}            # target already open
        for s, parents, pkinds in _walk(syms):
            kind = s.get("kind")
            if kind not in _INDEX_KINDS:
                continue
            # scope guard: a data declaration (global/const/field) is indexed only at
            # module or class scope — one nested under a function is a LOCAL (noise).
            if kind in _DATA_KINDS and any(pk in _FUNC_KINDS for pk in pkinds):
                continue
            rng = s.get("range", {})
            start = rng.get("start", {}).get("line", 0)
            body = "\n".join(lines[start:rng.get("end", {}).get("line", 0) + 1])
            container = tuple(parents)
            local = _local_name(s.get("name", ""), language_id)
            # IDENTITY = the language-normalized qualified name (stable across body
            # edits; changes only on rename/move). content_hash is a SEPARATE field
            # that gates description regeneration — the two jobs, split (docs §2.1).
            fqname = _qualify(language_id, module, container, local)
            parent = (_qualify(language_id, module, container[:-1],
                               _local_name(container[-1], language_id))
                      if container else "")
            content_hash = hashlib.sha1(body.encode()).hexdigest()[:16]
            sig = lines[start].strip()[:120]
            pos = (s.get("selectionRange") or rng)["start"]
            calls: list[str] = []
            called_by: list[str] = []
            if kind in _FUNC_KINDS and c.capabilities.get("callHierarchyProvider"):
                prep = c.request("textDocument/prepareCallHierarchy",
                                 {"textDocument": {"uri": uri}, "position": pos})
                if prep:
                    item = prep[0]
                    for e in c.request("callHierarchy/outgoingCalls", {"item": item}) or []:
                        t = e.get("to", {})
                        if _in_workspace(t.get("uri", ""), root):
                            calls.append(f"{t.get('name')} [{_rel(t.get('uri',''), root)}]")
                    for e in c.request("callHierarchy/incomingCalls", {"item": item}) or []:
                        fr = e.get("from", {})
                        if _in_workspace(fr.get("uri", ""), root):
                            called_by.append(f"{fr.get('name')} [{_rel(fr.get('uri',''), root)}]")
            # references: a FIRST-CLASS relation (everywhere this symbol is mentioned),
            # for any server with referencesProvider — deliberately SEPARATE from
            # called_by (call-hierarchy only). A reference is broader than a call (it
            # includes reads/mentions); the reference-vs-call distinction is left to the
            # consumer/LLM. This is the caller signal for symbols-only servers (shuck).
            references: list[str] = []
            if has_refs:
                refs = c.request("textDocument/references", {
                    "textDocument": {"uri": uri}, "position": pos,
                    "context": {"includeDeclaration": False}}) or []
                for loc in refs[:_REF_CAP]:
                    ruri = loc.get("uri", "")
                    if not _in_workspace(ruri, root):
                        continue
                    rline = loc.get("range", {}).get("start", {}).get("line", 0)
                    enc = _enclosing_symbol(c, ruri, rline, language_id, sym_cache, opened)
                    if not enc or (enc == local and _rel(ruri, root) == relpath):
                        continue                    # skip self-references
                    references.append(f"{enc} [{_rel(ruri, root)}]")
            # module-level variables read more naturally as "global" than "variable"
            kind_label = ("global" if kind == 13 and not container
                          else _KIND.get(kind or 0, "?"))
            entries.append({
                "fqname": fqname, "name": local,
                "kind": kind_label,
                "lang": language_id, "module": module, "container": list(container),
                "parent": parent, "content_hash": content_hash,
                "file": relpath, "line": start + 1, "mtime": mtime, "signature": sig,
                "calls": sorted(set(calls)), "called_by": sorted(set(called_by)),
                "references": sorted(set(references)),
                "name_terms": _name_terms(local, fqname),
                "_body": body,   # transient: for the description mop-up; never persisted
            })
    finally:
        c.close()
    return entries


# --- semantic facet: per-symbol descriptions (docs §4) -----------------------

DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"symbols": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                       "description": {"type": "string"}},
        "required": ["name", "description"]}}},
    "required": ["symbols"],
}
DESCRIBE_SYSTEM = (
    "You are given a source file. For EACH top-level definition — class, function, "
    "or method — IN ORDER, output its qualified name (Class.method), its kind "
    "(class|function|method), and ONE concise sentence describing what it does: the "
    "intent, not a restatement of the signature. A class description says what the "
    "type represents or manages. Return every definition as JSON matching the schema. "
    # NB: the literal word 'json' is required here — Alibaba/qwen rejects a
    # response_format=json_object request whose messages never mention 'json'.
    )


def describe_file(gen_cfg: Any, root: Path, relpath: str) -> dict[str, str]:
    """LLM description per symbol (name → one-line intent). Independent of the LSP —
    just the file text — so the semantic facet works even where no server exists.
    Reuses the bulk structured generation built for the keyword/summary indexes."""
    from .generate import generate_structured
    src = (root / relpath).read_text()
    data = generate_structured(gen_cfg, DESCRIBE_SYSTEM, src, DESCRIBE_SCHEMA,
                               purpose="elaborate", schema_name="describe_symbols")
    out: dict[str, str] = {}
    for s in _describe_rows(data):
        if isinstance(s, dict) and s.get("name") and s.get("description"):
            out[s["name"]] = s["description"]
    return out


def _describe_rows(data: Any) -> list:
    """The symbol rows from a structured describe response — tolerating both shapes:
    a wrapped object `{"symbols": [...]}` (GLM) OR a bare array `[...]` (qwen/Alibaba
    returns the array directly under json_object)."""
    if isinstance(data, dict):
        return data.get("symbols", []) or []
    return data if isinstance(data, list) else []


def describe_symbols(gen_cfg: Any, symbols: list[dict]) -> dict[str, str]:
    """MOP-UP describe: a focused structured call over ONLY the given symbols (their
    bodies), for ones the whole-file bulk pass missed. Keyed by the symbol's local
    `name` (fewer symbols → higher hit rate; content_hash gate makes the retry cheap)."""
    if not symbols:
        return {}
    from .generate import generate_structured
    blob = "\n\n".join(f"# {s.get('kind','')} {s.get('name','')}\n{s.get('_body','')}"
                       for s in symbols)
    sysp = ("For EACH `# kind name`-delimited definition below, output its name and "
            "ONE concise sentence on what it does (intent, not the signature). Cover "
            "every one, as JSON matching the schema.")   # 'json' required for qwen
    data = generate_structured(gen_cfg, sysp, blob, DESCRIBE_SCHEMA,
                               purpose="elaborate", schema_name="describe_symbols")
    out: dict[str, str] = {}
    for s in _describe_rows(data):
        if isinstance(s, dict) and s.get("name") and s.get("description"):
            out[s["name"]] = s["description"]
    return out


def match_description(fqname: str, descs: dict[str, str]) -> str:
    """Attach a description to a structural symbol: exact fqname else last segment."""
    if fqname in descs:
        return descs[fqname]
    tail = fqname.split(".")[-1]
    for name, d in descs.items():
        if name == tail or name.split(".")[-1] == tail:
            return d
    return ""


# --- content-addressed store (separate location, like keyword_index) ----------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


_SCALARS = ("fqname", "name", "kind", "lang", "module", "parent", "content_hash",
            "file", "signature", "description")
_ARRAYS = ("container", "calls", "called_by", "references", "name_terms")


def _render(e: dict) -> str:
    lines = [f'{k} = "{_esc(str(e.get(k, "")))}"' for k in _SCALARS]
    lines.append(f'line = {e.get("line", 0)}')
    lines.append(f'mtime = {e.get("mtime", 0)}')
    for key in _ARRAYS:
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
    """Content-addressed structural store: symbol_index/<symbol_hash>.toml under the
    project data dir — git-communicable, byte-deterministic, merge-conflict-free."""

    def __init__(self, project_dir: Path) -> None:
        self.root = project_dir / "symbol_index"

    def write(self, entry: dict) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        # Filename keyed by the FQN (identity), so a body edit UPDATES the same file
        # (clean git diff) instead of orphaning it — content_hash is a field inside.
        fh = hashlib.sha1(entry["fqname"].encode()).hexdigest()[:16]
        p = self.root / f"{fh}.toml"
        p.write_text(_render(entry))
        return p

    def write_all(self, entries: list[dict]) -> int:
        for e in entries:
            self.write(e)
        return len(entries)

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
        p = self.root / f"{hashlib.sha1(fqname.encode()).hexdigest()[:16]}.toml"
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
                arr.append(s.rstrip(",").strip().strip('"'))
            continue
        if s.endswith("= []"):                 # empty array on one line
            e[s.split(" = ")[0].strip()] = []
        elif s.endswith("= ["):                # multi-line array start
            key = s.split(" = ")[0].strip()
            arr = []
        elif " = " in s:
            k, _, v = s.partition(" = ")
            v = v.strip()
            e[k] = int(v) if v.isdigit() else v.strip('"')
    return e
