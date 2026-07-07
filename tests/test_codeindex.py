"""Code symbol index — deterministic structural checks (no LLM).

The naming/qualification/store logic is language-specific and pure, so it gets fast
unit tests; the LSP extraction + call graph gets one integration test that skips when
pyright isn't available (like the retrieval gate). Descriptions (the LLM facet) are
NOT tested here — this gate is the structural, deterministic contract.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from crib import codeindex as ci

REPO = Path(__file__).resolve().parent.parent


# --- fqn normalization (per-language qualify / module / local name) ----------
def test_module_of_python_strips_roots_and_init():
    assert ci._module_of("crib/retrieve.py", "python") == "crib.retrieve"
    assert ci._module_of("src/pkg/mod.py", "python") == "pkg.mod"
    assert ci._module_of("lua/mcp_companion/init.lua", "lua") == "mcp_companion"


def test_module_of_rust_uses_colons():
    assert ci._module_of("src/foo/bar.rs", "rust") == "foo::bar"


def test_local_name_strips_lua_table_prefix():
    assert ci._local_name("M.setup", "lua") == "setup"
    assert ci._local_name("T:method", "lua") == "method"
    assert ci._local_name("plain", "python") == "plain"


def test_local_name_reduces_rust_impl_to_type():
    # rust-analyzer names impl symbols `impl Type` / `impl Trait for Type` — reduce to
    # the TYPE so its methods qualify as Type::method, not "impl Type"::method.
    assert ci._local_name("impl ServerState", "rust") == "ServerState"
    assert ci._local_name("impl Display for ServerState", "rust") == "ServerState"
    assert ci._local_name("impl<T> Foo<T>", "rust") == "Foo"
    assert ci._local_name("impl Iterator for Foo<Bar>", "rust") == "Foo"
    # a plain fn named like a keyword-prefix must NOT be munged (word boundary)
    assert ci._local_name("implement", "rust") == "implement"
    assert ci._local_name("ServerState", "rust") == "ServerState"


def test_symbol_index_uses_legible_slug_filenames(tmp_path):
    si = ci.SymbolIndex(tmp_path)
    # clean dotted fqn → verbatim; lossy (rust ::) → munged + hash suffix
    assert si._relname("crib.retrieve.LexicalCache.get") == "crib.retrieve.LexicalCache.get.toml"
    p = si.write({"fqname": "a::b::C", "name": "C", "kind": "struct",
                  "content_hash": "h", "file": "a.rs", "line": 1, "mtime": 1,
                  "signature": "", "description": "d", "container": [], "calls": [],
                  "called_by": [], "references": [], "name_terms": ["C"]})
    assert p.name.startswith("a-b-C-") and p.suffix == ".toml"     # munged + hash
    assert si.by_fqname("a::b::C") and si.delete("a::b::C")        # write/delete round-trip
    assert not si.by_fqname("a::b::C")


def test_derive_mtime_git_date_for_committed_disk_for_modified(tmp_path):
    import subprocess
    r = tmp_path / "repo"; (r / "sub").mkdir(parents=True)
    def git(*a): subprocess.run(["git", "-C", str(r), *a], check=True,
                                capture_output=True)
    git("init"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    f = r / "sub" / "mod.py"; f.write_text("x = 1\n")
    git("add", "-A")
    import os
    env = {**os.environ, "GIT_COMMITTER_DATE": "1700000000 +0000",
           "GIT_AUTHOR_DATE": "1700000000 +0000"}
    subprocess.run(["git", "-C", str(r), "commit", "-m", "c"], check=True,
                   capture_output=True, env=env)
    # committed + clean → git commit date (seconds → ns)
    assert ci.derive_mtime(r, "sub/mod.py") == 1700000000 * 1_000_000_000
    # now modify it → on-disk mtime (a plausibly-large ns value, not the commit date)
    f.write_text("x = 2\n")
    dm = ci.derive_mtime(r, "sub/mod.py")
    assert dm != 1700000000 * 1_000_000_000 and dm == f.stat().st_mtime_ns
    # outside a git repo → on-disk mtime, no crash
    g = tmp_path / "bare.py"; g.write_text("y=1\n")
    assert ci.derive_mtime(tmp_path, "bare.py") == g.stat().st_mtime_ns


def test_type_kinds_indexed_and_descended():
    # Struct(23)/Enum(10)/Interface(11) are indexed AND descended (Rust/Go/TS types);
    # Object(19)=impl and Module(2)/Namespace(3) are descended so nested methods land.
    for k in (5, 10, 11, 23):
        assert k in ci._INDEX_KINDS and k in ci._CONTAINER_KINDS
    for k in (2, 3, 19):
        assert k in ci._CONTAINER_KINDS
    assert ci._KIND[23] == "struct" and ci._KIND[10] == "enum"
    assert ci._KIND_LABEL_OVERRIDE[("rust", 11)] == "trait"


def test_content_lang_shebang_marker_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "cfg"))
    cases = {
        "#!/usr/bin/env zsh\n": "zsh",
        "#!/usr/bin/env -S zsh\n": "zsh",          # -S split-string flag
        "#!/usr/bin/env VAR=1 zsh\n": "zsh",       # env var assignment
        "#!/usr/bin/env python3.11\n": "python",   # version suffix stripped
        "#!/bin/zsh -f\n": "zsh",                  # args after interpreter
        "#compdef foo\n": "zsh",                   # completion marker
        "#autoload\n": "zsh",                      # autoload marker
        "# compdef spaced\n": None,                # prose (space) must NOT match
        "#!/usr/bin/env\n": None,                  # env with no interpreter
        "plain text\n": None,
    }
    for content, exp in cases.items():
        f = tmp_path / "probe"
        f.write_text(content)
        assert ci.content_lang(f) == exp, content
    # bare filename rule
    rc = tmp_path / ".zshrc"; rc.write_text("alias x=y\n")
    assert ci.content_lang(rc) == "zsh"


def test_load_grammar_merges_user_over_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"; cfg.mkdir()
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(cfg))
    (cfg / "grammar.json").write_text(
        '{"shebangs": {"fish": "fish"}, "firstLineMarkers": {"funcdef": "fish"}}')
    g = ci.load_grammar()
    assert g["shebangs"]["fish"] == "fish"          # user rule added
    assert g["shebangs"]["zsh"] == "zsh"            # defaults preserved
    assert g["firstLineMarkers"]["funcdef"] == "fish"
    assert g["firstLineMarkers"]["compdef"] == "zsh"


def test_qualify_is_language_idiomatic():
    assert ci._qualify("python", "crib.retrieve", ("BM25",), "scores") \
        == "crib.retrieve.BM25.scores"
    assert ci._qualify("rust", "foo::bar", ("Type",), "method") \
        == "foo::bar::Type::method"
    # Lua: the `M` table var is dropped from the container, module comes from path
    assert ci._qualify("lua", "mcp_companion", ("M",), "setup") \
        == "mcp_companion.setup"


def test_name_terms_split_compound_identifiers():
    terms = ci._name_terms("SharedServerManager", "mod.SharedServerManager")
    assert {"shared", "server", "manager"} <= set(terms)          # subtokens
    assert "SharedServerManager" in terms                          # unqualified name


# --- shebang routing for extension-less scripts ------------------------------
def test_shebang_lang_maps_interpreters(tmp_path):
    cases = {
        "#!/usr/bin/env zsh\n": "zsh",
        "#!/bin/zsh -f\n": "zsh",                       # trailing interpreter args
        "#!/usr/bin/python3\n": "python",
        "#!/usr/bin/env python3.11\n": "python",        # version suffix stripped
        "#!/usr/bin/node\n": "javascript",
        "plain file, no shebang\n": None,
        "#!/opt/weird/thing\n": None,                   # unknown interpreter
    }
    for src, exp in cases.items():
        p = tmp_path / "s"
        p.write_text(src)
        assert ci._shebang_lang(p) == exp, src


def test_server_for_shebang_fallback(tmp_path):
    # a spec whose binary always resolves ("sh" is everywhere), claiming .zsh→zsh
    specs = {"fakezsh": {"command": "sh", "args": ["-c", ":"],
                         "extensionToLanguage": {".zsh": "zsh"}}}
    # extension-less file with a zsh shebang → routed by shebang to the zsh server
    f = tmp_path / "myscript"
    f.write_text("#!/usr/bin/env zsh\nfoo() { :; }\n")
    sel = ci.server_for(f.name, specs=specs, abspath=f)
    assert sel and sel[0] == "fakezsh" and sel[2] == "zsh"
    # no shebang → no server (never guess)
    g = tmp_path / "plain"
    g.write_text("hello\n")
    assert ci.server_for(g.name, specs=specs, abspath=g) is None
    # a KNOWN extension wins over the shebang (extension precedence)
    h = tmp_path / "real.zsh"
    h.write_text("#!/usr/bin/env python3\n")
    sel2 = ci.server_for(h.name, specs=specs, abspath=h)
    assert sel2 and sel2[2] == "zsh"                    # by extension, not the py shebang


# --- learning_slug: fqn → filesystem/git-safe basename ----------------------
def test_learning_slug_clean_fqn_verbatim():
    # a pure dotted fqn is already filesystem-clean → passes through unchanged
    assert ci.learning_slug("crib.retrieve.LexicalCache.get") == \
        "crib.retrieve.LexicalCache.get"


def test_learning_slug_munges_unsafe_and_disambiguates():
    # anything outside [A-Za-z0-9._-] collapses to '-'; a lossy munge appends a
    # short fqn hash so distinct symbols can't collide on disk
    a = ci.learning_slug("core::cache::Store::get")
    b = ci.learning_slug("core-cache-Store-get")        # would collide sans hash
    assert a.startswith("core-cache-Store-get-")
    assert a != b and "::" not in a and "/" not in a
    # Go import paths, C++ generics/operators/dtors — all land valid and unique
    for fq in ("pkg/foo.Bar", "Vec<T>::push", "operator+", "ns::~Dtor"):
        s = ci.learning_slug(fq)
        assert set(s) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
        assert s and not s.startswith("-") and not s.endswith("-")


# --- describe response shape tolerance (GLM wraps, qwen returns a bare array) -----
def test_describe_rows_accepts_both_shapes():
    rows = [{"name": "f", "description": "does f"}]
    assert ci._describe_rows({"symbols": rows}) == rows      # GLM: wrapped object
    assert ci._describe_rows(rows) == rows                    # qwen: bare array
    assert ci._describe_rows({}) == []                        # empty object
    assert ci._describe_rows(None) == []                      # nothing
    assert ci._describe_rows("nope") == []                    # junk


# --- find_root resolves to the TOP-LEVEL repo, past submodule gitlinks -----------
def test_find_root_walks_past_submodule_to_top_level(tmp_path):
    top = tmp_path / "repo"
    (top / ".git").mkdir(parents=True)               # real repo: .git is a DIRECTORY
    sub = top / "vendor" / "dep"
    (sub / "src").mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/dep")  # submodule: .git is a FILE
    (sub / "pyproject.toml").write_text("[project]\nname='dep'\n")  # would trap a naive finder
    f = sub / "src" / "mod.py"
    f.write_text("x = 1\n")
    # a normal file → the top-level repo
    assert ci.find_root((top / "a.py")) == top
    # a vendored/submodule file → STILL the top-level repo (not the submodule)
    assert ci.find_root(f) == top


# --- mtime survives the TOML render/parse round-trip (the staleness gate) --------
def test_mtime_round_trips_as_bare_int():
    e = {"fqname": "m.f", "name": "f", "kind": "function", "line": 3,
         "mtime": 1730000000123456789, "container": [], "calls": [],
         "called_by": [], "references": [], "name_terms": ["f"]}
    got = ci._parse(ci._render(e))
    assert got["mtime"] == 1730000000123456789 and isinstance(got["mtime"], int)


# --- documentSymbol flattening (descend classes into methods) ----------------
def test_walk_descends_containers():
    tree = [{"name": "C", "kind": 5, "children": [
        {"name": "m", "kind": 6, "children": []}]},
        {"name": "f", "kind": 12}]
    flat = ci._walk(tree)
    names = {(s["name"], parents) for s, parents, _kinds in flat}
    assert ("C", ()) in names
    assert ("m", ("C",)) in names          # method carries its class as container
    assert ("f", ()) in names


def test_walk_tracks_container_kinds_for_scope_guard():
    # a Variable under a class body vs one under a method — the scope guard uses
    # container KINDS to tell a class attribute from a function-local
    tree = [{"name": "C", "kind": 5, "children": [
        {"name": "attr", "kind": 13, "children": []},                      # class attr
        {"name": "meth", "kind": 6, "children": [
            {"name": "local", "kind": 13, "children": []}]}]},             # local
        {"name": "G", "kind": 13}]                                          # module global
    by_name = {s["name"]: kinds for s, _p, kinds in ci._walk(tree)}
    is_local = lambda name: any(k in ci._FUNC_KINDS for k in by_name[name])
    assert not is_local("attr")            # class-scoped → kept
    assert not is_local("G")               # module-scoped → kept
    assert is_local("local")               # under a method → dropped as noise


# --- TOML store render/parse round-trip (incl. the empty-array case) ---------
def test_render_parse_roundtrip_including_empty_arrays():
    e = {"fqname": "a.B.c", "name": "c", "kind": "method", "lang": "python",
         "module": "a", "parent": "a.B", "content_hash": "deadbeef",
         "file": "a.py", "line": 12, "signature": "def c(self):",
         "description": 'has "quotes" and, commas', "container": ["B"],
         "calls": ["x [a.py]"], "called_by": [], "name_terms": ["c", "a.B.c"]}
    got = ci._parse(ci._render(e))
    assert got["fqname"] == "a.B.c" and got["line"] == 12
    assert got["called_by"] == []          # empty array parses as [], not "[]"
    assert got["calls"] == ["x [a.py]"]
    assert got["parent"] == "a.B"
    assert 'quotes' in got["description"]


# --- integration: real LSP extraction + call graph (skips without pyright) ---
def test_extract_call_graph_anchor_edges():
    if ci.server_for("crib/retrieve.py") is None:
        pytest.skip("no Python LSP server available (pyright/basedpyright)")
    entries = {e["fqname"]: e for e in ci.extract_file(REPO, "crib/retrieve.py")}
    # fqn normalization: module-qualified, class-nested
    assert "crib.retrieve.LexicalCache.get" in entries
    get = entries["crib.retrieve.LexicalCache.get"]
    assert get["name"] == "get" and get["parent"] == "crib.retrieve.LexicalCache"
    # deterministic call edges (semantic, cross-file) — the LSP-sync contract
    callees = {c.split(" [")[0] for c in get["calls"]}
    assert {"BM25", "_lexical_tf", "get_docs"} <= callees
    callers = {c.split(" [")[0] for c in get["called_by"]}
    assert "_retrieve" in callers


# --- warm LSP session pool: one spawn across files, crash respawn ------------
# A minimal stdio LSP server: answers initialize + documentSymbol (one function
# "f"), logs each SPAWN to $FAKE_LSP_LOG so tests can count real processes.
_FAKE_LSP = r'''
import json, os, sys

def send(msg):
    data = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()

with open(os.environ["FAKE_LSP_LOG"], "a") as fh:
    fh.write("spawn\n")

SYM = [{"name": "f", "kind": 12,
        "range": {"start": {"line": 0, "character": 0},
                  "end": {"line": 1, "character": 0}},
        "selectionRange": {"start": {"line": 0, "character": 4},
                           "end": {"line": 0, "character": 5}}}]
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        t = line.decode().strip()
        if not t:
            break
        k, _, v = t.partition(":")
        headers[k.strip().lower()] = v.strip()
    msg = json.loads(sys.stdin.buffer.read(int(headers.get("content-length", 0))))
    m, mid = msg.get("method"), msg.get("id")
    with open(os.environ["FAKE_LSP_LOG"], "a") as fh:
        fh.write((m or "?") + "\n")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"capabilities": {"documentSymbolProvider": True}}})
    elif m == "textDocument/documentSymbol":
        send({"jsonrpc": "2.0", "id": mid, "result": SYM})
    elif m == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif m == "exit":
        sys.exit(0)
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
'''


def _fake_lsp(tmp_path, monkeypatch):
    import sys as _sys
    server = tmp_path / "fake_lsp.py"
    server.write_text(_FAKE_LSP)
    log = tmp_path / "spawns.log"
    log.write_text("")
    monkeypatch.setenv("FAKE_LSP_LOG", str(log))
    argv = [_sys.executable, str(server)]
    monkeypatch.setattr(ci, "server_for",
                        lambda rel, specs=None, abspath=None:
                        ("fake", argv, "python", {}))
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    pass\n")
    (root / "b.py").write_text("def f():\n    pass\n")
    return root, argv, log


def test_session_pool_reuses_one_server_across_files(tmp_path, monkeypatch):
    root, _argv, log = _fake_lsp(tmp_path, monkeypatch)
    pool = ci.LspSessionPool()
    try:
        e1 = ci.extract_file(root, "a.py", settle=0, pool=pool)
        e2 = ci.extract_file(root, "b.py", settle=0, pool=pool)
    finally:
        pool.close_all()
    assert [e["name"] for e in e1] == ["f"] == [e["name"] for e in e2]
    assert log.read_text().count("spawn") == 1     # ONE warm server served both files


def test_session_pool_pumps_watched_file_changes(tmp_path, monkeypatch):
    root, _argv, log = _fake_lsp(tmp_path, monkeypatch)
    pool = ci.LspSessionPool()
    try:
        ci.extract_file(root, "a.py", settle=0, pool=pool)      # warm session up
        pool.notify_changes(root, [("b.py", 2), ("gone.py", 3)])
        pool.notify_changes(tmp_path / "other", [("x.py", 2)])  # no session → no-op
    finally:
        pool.close_all()
    assert log.read_text().count("workspace/didChangeWatchedFiles") == 1


def test_session_pool_respawns_after_crash(tmp_path, monkeypatch):
    root, argv, log = _fake_lsp(tmp_path, monkeypatch)
    pool = ci.LspSessionPool()
    try:
        ci.extract_file(root, "a.py", settle=0, pool=pool)
        sess, fresh = pool.acquire(root, "fake", argv, {})   # the warm session…
        assert not fresh
        sess.client.proc.kill()                              # …dies out from under us
        sess.client.proc.wait()
        e2 = ci.extract_file(root, "b.py", settle=0, pool=pool)  # poll() → respawn
    finally:
        pool.close_all()
    assert [e["name"] for e in e2] == ["f"]
    assert log.read_text().count("spawn") == 2


def test_session_pool_pins_docs_across_calls(tmp_path, monkeypatch):
    """Pinned docs (LSP membership: didOpen'd so the server's analysis set covers
    files its own discovery would miss) survive an extraction's teardown and
    close only on unpin."""
    root, argv, log = _fake_lsp(tmp_path, monkeypatch)
    pool = ci.LspSessionPool()
    try:
        n = pool.pin_docs(root, "fake", argv, {},
                          [(root / "a.py", "python"), (root / "b.py", "python")])
        assert n == 2
        ci.extract_file(root, "a.py", settle=0, pool=pool)   # a.py already pinned
        txt = log.read_text()
        assert txt.count("textDocument/didOpen") == 2   # the pins; no re-open
        # open doc => client truth: extracting a pinned uri syncs via didChange
        assert txt.count("textDocument/didChange") == 1
        assert txt.count("textDocument/didClose") == 0  # teardown keeps pins open
        pool.unpin(root)
        deadline = time.monotonic() + 2.0     # notifications have no reply barrier
        while time.monotonic() < deadline \
                and log.read_text().count("textDocument/didClose") < 2:
            time.sleep(0.02)
        assert log.read_text().count("textDocument/didClose") == 2
    finally:
        pool.close_all()
