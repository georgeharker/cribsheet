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


def server_for(relpath: str, specs: dict | None = None
               ) -> tuple[str, list[str], str, dict] | None:
    """Pick a server for `relpath` by extension. Iterates specs IN ORDER (user
    ~/.config/crib/lsp.json first, then shipped defaults backfilling missing labels)
    and returns the FIRST that BOTH claims the extension (`extensionToLanguage`) AND
    has an installed binary (`resolve_command`) — so a missing binary falls through
    to the next candidate (e.g. basedpyright→pyright for `.py`). Order = precedence.
    → (label, argv, languageId, spec), or None if nothing matches/resolves (that
    language is then silently skipped)."""
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
    return None


def find_root(path: Path) -> Path:
    """Nearest ancestor with a project marker — the LSP workspace root."""
    path = path.resolve()
    for d in [path, *path.parents]:
        if (d / "pyproject.toml").exists() or (d / ".git").exists() \
                or (d / "setup.py").exists():
            return d
    return path.parent

# documentSymbol kinds we index as callables/containers (LSP SymbolKind numbers).
_FUNC_KINDS = {6, 12}          # Method, Function
_CONTAINER_KINDS = {5, 6, 12}  # Class, Method, Function (descend into these)


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
        self.request("initialize", {
            "processId": os.getpid(), "rootUri": self.root.as_uri(),
            "initializationOptions": self.init_options,
            "capabilities": {"textDocument": {
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "callHierarchy": {"dynamicRegistration": True}},
                "workspace": {"configuration": True}},
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
        })
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


def _walk(syms: list, parents: tuple[str, ...] = ()) -> list[tuple[dict, tuple]]:
    """Flatten documentSymbol tree → [(symbol, container_path)], descending classes."""
    out = []
    for s in syms or []:
        out.append((s, parents))
        if s.get("kind") in _CONTAINER_KINDS and s.get("children"):
            out.extend(_walk(s["children"], parents + (s.get("name", ""),)))
    return out


_KIND = {5: "class", 6: "method", 12: "function"}
# Lua binds modules to a local table var (`M.setup`); those aren't real qualifiers.
_LUA_TABLE_VARS = {"M", "_M", "self", "Module", "mod"}


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
    sel = server_for(relpath)
    if sel is None:
        raise NoServer(f"no LSP server for {Path(relpath).suffix} "
                       f"(configure ~/.config/crib/lsp.json)")
    _label, argv, language_id, spec = sel
    path = (root / relpath).resolve()
    lines = path.read_text().splitlines()
    c = LspClient(argv, root, init_options=spec.get("initializationOptions"),
                  settings=spec.get("settings"))
    entries: list[dict] = []
    try:
        c.initialize()
        uri = path.as_uri()
        c.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language_id, "version": 1,
            "text": "\n".join(lines)}})
        time.sleep(settle)
        module = _module_of(relpath, language_id)
        syms = c.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        for s, parents in _walk(syms):
            if s.get("kind") not in _FUNC_KINDS | {5}:
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
            calls: list[str] = []
            called_by: list[str] = []
            if s.get("kind") in _FUNC_KINDS:
                pos = (s.get("selectionRange") or rng)["start"]
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
            entries.append({
                "fqname": fqname, "name": local,
                "kind": _KIND.get(s.get("kind") or 0, "?"),
                "lang": language_id, "module": module, "container": list(container),
                "parent": parent, "content_hash": content_hash,
                "file": relpath, "line": start + 1, "signature": sig,
                "calls": sorted(set(calls)), "called_by": sorted(set(called_by)),
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
    "type represents or manages. Return every definition.")


def describe_file(gen_cfg: Any, root: Path, relpath: str) -> dict[str, str]:
    """LLM description per symbol (name → one-line intent). Independent of the LSP —
    just the file text — so the semantic facet works even where no server exists.
    Reuses the bulk structured generation built for the keyword/summary indexes."""
    from .generate import generate_structured
    src = (root / relpath).read_text()
    data = generate_structured(gen_cfg, DESCRIBE_SYSTEM, src, DESCRIBE_SCHEMA,
                               purpose="elaborate", schema_name="describe_symbols")
    out: dict[str, str] = {}
    for s in (data or {}).get("symbols", []) if isinstance(data, dict) else []:
        if isinstance(s, dict) and s.get("name") and s.get("description"):
            out[s["name"]] = s["description"]
    return out


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
            "every one.")
    data = generate_structured(gen_cfg, sysp, blob, DESCRIBE_SCHEMA,
                               purpose="elaborate", schema_name="describe_symbols")
    out: dict[str, str] = {}
    for s in (data or {}).get("symbols", []) if isinstance(data, dict) else []:
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
_ARRAYS = ("container", "calls", "called_by", "name_terms")


def _render(e: dict) -> str:
    lines = [f'{k} = "{_esc(str(e.get(k, "")))}"' for k in _SCALARS]
    lines.append(f'line = {e.get("line", 0)}')
    for key in _ARRAYS:
        vals = e.get(key) or []
        if vals:
            lines.append(f"{key} = [")
            lines += [f'  "{_esc(str(v))}",' for v in vals]
            lines.append("]")
        else:
            lines.append(f"{key} = []")
    return "\n".join(lines) + "\n"


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
