"""Index-integrity gates: a symbol record is correct or visibly dirty, never
plausibly wrong.

Three failure families, all of which used to corrupt the store silently:

- **session vs file faults** — an unreadable source file is not a wedged LSP
  server, and treating it as one cold-started the language server per file;
- **deletion on bad evidence** — a partial `documentSymbol` answer used to delete
  live symbols, so deletion is now gated on the file's bytes having changed;
- **serialization** — an embedded newline truncated a record and the line-oriented
  reader then misread the rest of the file as bogus keys.

The LSP-facing tests drive a fake stdio server (same shape as the one in
`test_codeindex.py`, plus knobs for garbage frames and a silent method); the
deletion-gate tests fake `extract_file` outright — the gate is pure index logic.
"""

from __future__ import annotations

import asyncio
import random
import time
import tomllib

import pytest

from crib import codeindex as ci
from crib import tomlrec
from crib.app import Crib
from crib.codeindex import SymbolIndex
from crib.config import Config
from crib.paths import Paths
from crib.section_index import SectionIndex
from crib.store import InMemoryStore

# A minimal stdio LSP server. FAKE_LSP_GARBAGE selects when documentSymbol answers
# with an undecodable frame instead of a result — "first" only on the first spawn
# (so the retry lands on a healthy server), "always" every time. `test/silent` is
# never answered, which is how the timeout path is exercised.
_FAKE_LSP = r'''
import json, os, sys

def send(msg):
    data = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()

log = os.environ["FAKE_LSP_LOG"]
first_spawn = "spawn" not in open(log).read()
with open(log, "a") as fh:
    fh.write("spawn\n")
mode = os.environ.get("FAKE_LSP_GARBAGE", "")
garbage = mode == "always" or (mode == "first" and first_spawn)

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
    with open(log, "a") as fh:
        fh.write((m or "?") + "\n")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"capabilities": {"documentSymbolProvider": True}}})
    elif m == "textDocument/documentSymbol":
        if garbage:
            body = b"{not json at all"
            sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body))
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.flush()
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": SYM})
    elif m == "test/silent":
        pass                       # deliberately no reply — the timeout path
    elif m == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif m == "exit":
        sys.exit(0)
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
'''


def _fake_lsp(tmp_path, monkeypatch, garbage: str = ""):
    import sys as _sys
    server = tmp_path / "fake_lsp.py"
    server.write_text(_FAKE_LSP)
    log = tmp_path / "spawns.log"
    log.write_text("")
    monkeypatch.setenv("FAKE_LSP_LOG", str(log))
    monkeypatch.setenv("FAKE_LSP_GARBAGE", garbage)
    argv = [_sys.executable, str(server)]
    monkeypatch.setattr(ci, "server_for",
                        lambda rel, specs=None, abspath=None:
                        ("fake", argv, "python", {}))
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    pass\n")
    (root / "b.py").write_text("def f():\n    pass\n")
    return root, argv, log


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def _sym(fq: str, file_hash: str, *, file: str = "m.py") -> dict:
    return {"fqname": fq, "name": fq.split(".")[-1], "kind": "function",
            "lang": "python", "module": "m", "parent": "",
            "content_hash": f"h_{fq}", "file": file, "file_hash": file_hash,
            "line": 1, "mtime": 1, "signature": "def _():", "description": "d",
            "keywords": [], "container": [], "calls": [], "called_by": [],
            "references": [], "name_terms": [fq.split(".")[-1]],
            "_body": "def _(): pass"}


# ── 2.2 an unreadable FILE is not a wedged SESSION ────────────────────────────
def test_undecodable_source_indexes_lossily_on_the_same_session(tmp_path, monkeypatch):
    """A latin-1 source is indexable, just lossily: `errors="replace"` on the read,
    and the server is didOpen'd with the SAME replaced text so positions agree. The
    warm session must be untouched — it used to be discarded (and rust-analyzer
    cold-started, minutes) on every encounter with one such file."""
    root, argv, log = _fake_lsp(tmp_path, monkeypatch)
    (root / "latin1.py").write_bytes(b"# caf\xe9 comment\ndef f():\n    pass\n")
    pool = ci.LspSessionPool()
    try:
        ci.extract_file(root, "a.py", settle=0, pool=pool)
        before, _fresh = pool.acquire(root, "fake", argv, {})
        entries = ci.extract_file(root, "latin1.py", settle=0, pool=pool)
        after, _fresh = pool.acquire(root, "fake", argv, {})
    finally:
        pool.close_all()
    assert [e["name"] for e in entries] == ["f"]     # indexed, not skipped
    assert after is before                           # same session object…
    assert log.read_text().count("spawn") == 1       # …and no respawn


def test_unreadable_file_raises_file_read_error_and_keeps_the_session(
        tmp_path, monkeypatch):
    root, argv, log = _fake_lsp(tmp_path, monkeypatch)
    (root / "d.py").mkdir()                          # read_text → IsADirectoryError
    pool = ci.LspSessionPool()
    try:
        ci.extract_file(root, "a.py", settle=0, pool=pool)
        before, _fresh = pool.acquire(root, "fake", argv, {})
        with pytest.raises(ci.FileReadError):
            ci.extract_file(root, "d.py", settle=0, pool=pool)
        after, _fresh = pool.acquire(root, "fake", argv, {})
    finally:
        pool.close_all()
    assert after is before and log.read_text().count("spawn") == 1


def test_file_read_error_reports_the_file_skipped(crib, tmp_path, monkeypatch):
    """The pipeline turns it into a per-file skip (kind `unreadable`, which the
    project sweep collects into `skipped` and warns about once) — never an aborted
    file, never a lost session."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text("def a(): pass\n")

    def boom(*a, **k):
        raise ci.FileReadError("m.py: 'utf-8' codec is unusable")

    monkeypatch.setattr(ci, "extract_file", boom)
    out = crib._index_code_file_tracked(root, "m.py", "p", patch_edges=False)
    assert out["symbols"] == 0 and out["skipped_kind"] == "unreadable"


# ── 2.3 the caller's settle is honored ────────────────────────────────────────
def test_settle_policy_defaults_but_an_explicit_value_wins(tmp_path, monkeypatch):
    """`settle=None` is policy (full wait cold, short wait warm); an explicit float
    is obeyed exactly. The confirm pass asks for 3s and used to get 0.3s — so it
    re-read the same partial listing it existed to disprove."""
    root, _argv, _log = _fake_lsp(tmp_path, monkeypatch)
    seen: list[float] = []
    monkeypatch.setattr(ci.LspClient, "wait_quiescent",
                        lambda self, initial, timeout: seen.append(initial))
    pool = ci.LspSessionPool()
    try:
        ci.extract_file(root, "a.py", pool=pool)                 # cold  → policy
        ci.extract_file(root, "b.py", pool=pool)                 # warm  → policy
        ci.extract_file(root, "b.py", settle=3.0, pool=pool)     # warm  → explicit
        ci.extract_file(root, "b.py", settle=0, pool=pool)       # warm  → explicit 0
    finally:
        pool.close_all()
    assert seen == [ci._FRESH_SETTLE, ci._REUSE_SETTLE, 3.0, 0]


# ── 2.3 the hash-gated deletion rule ──────────────────────────────────────────
def _seed_two_symbols(crib, root, file_hash):
    store = SymbolIndex(crib.paths.project_dir("p"))
    for fq in ("m.a", "m.b"):
        store.write(_sym(fq, file_hash))
    store.set_source_root(root)
    return store


def test_unchanged_file_hash_withholds_deletion_and_marks_dirty(
        crib, tmp_path, monkeypatch):
    """Identical bytes cannot have lost a symbol. So an extract that comes back
    short for an UNCHANGED file is an extraction anomaly, not an edit: keep every
    symbol, blank the vanished ones' `content_hash` (the store's merge-dirty
    marker, which `revalidate`/reconcile already sweep) and say so in the result."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text("def a(): pass\n\ndef b(): pass\n")
    store = _seed_two_symbols(crib, root, "HASH1")

    monkeypatch.setattr(ci, "extract_file",
                        lambda r, rel, settle=None, pool=None, ref_projects=None:
                        [_sym("m.a", "HASH1")])          # b vanished; same bytes
    monkeypatch.setattr(ci, "describe_file", lambda *a, **k: {})
    monkeypatch.setattr(ci, "describe_symbols", lambda *a, **k: {})
    out = crib._index_code_file_tracked(root, "m.py", "p", patch_edges=False)

    assert {e["name"] for e in store.all()} == {"a", "b"}         # nothing deleted
    assert store.read("m.py#b")["content_hash"] == ""               # …but visibly dirty
    assert out["deletions_withheld"] == ["m.py#b"]


def test_changed_file_hash_lets_a_real_removal_through(crib, tmp_path, monkeypatch):
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text("def a(): pass\n")
    store = _seed_two_symbols(crib, root, "HASH1")

    monkeypatch.setattr(ci, "extract_file",
                        lambda r, rel, settle=None, pool=None, ref_projects=None:
                        [_sym("m.a", "HASH2")])          # the file really changed
    monkeypatch.setattr(ci, "describe_file", lambda *a, **k: {})
    monkeypatch.setattr(ci, "describe_symbols", lambda *a, **k: {})
    out = crib._index_code_file_tracked(root, "m.py", "p", patch_edges=False)

    assert {e["name"] for e in store.all()} == {"a"}              # b really removed
    assert "deletions_withheld" not in out


def test_deletion_gate_stays_permissive_for_a_pre_file_hash_index(
        crib, tmp_path, monkeypatch):
    """An index written before `file_hash` existed carries no evidence either way —
    fall back to the old behavior rather than freezing stale symbols forever."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text("def a(): pass\n")
    store = SymbolIndex(crib.paths.project_dir("p"))
    for fq in ("m.a", "m.b"):
        legacy = _sym(fq, "")
        legacy.pop("file_hash")
        store.write(legacy)
    store.set_source_root(root)

    monkeypatch.setattr(ci, "extract_file",
                        lambda r, rel, settle=None, pool=None, ref_projects=None:
                        [_sym("m.a", "HASH1")])
    monkeypatch.setattr(ci, "describe_file", lambda *a, **k: {})
    monkeypatch.setattr(ci, "describe_symbols", lambda *a, **k: {})
    crib._index_code_file_tracked(root, "m.py", "p", patch_edges=False)
    assert {e["name"] for e in store.all()} == {"a"}


# ── 2.4 reader-thread death fails fast ────────────────────────────────────────
def test_garbage_frame_kills_the_reader_and_the_session_is_replaced_once(
        tmp_path, monkeypatch):
    """One malformed frame used to kill the reader silently, after which every
    request burned its full 30s timeout. Now the session is marked dead, the
    in-flight request raises `SessionError`, and the existing discard/retry path
    replaces the server exactly once — the extraction still succeeds."""
    root, _argv, log = _fake_lsp(tmp_path, monkeypatch, garbage="first")
    pool = ci.LspSessionPool()
    t0 = time.monotonic()
    try:
        entries = ci.extract_file(root, "a.py", settle=0, pool=pool)
    finally:
        pool.close_all()
    assert [e["name"] for e in entries] == ["f"]      # the retry got a real answer
    assert log.read_text().count("spawn") == 2       # replaced exactly once
    assert time.monotonic() - t0 < 15.0              # not two 30s timeouts


def test_dead_session_raises_immediately_and_leaks_no_response_slot(
        tmp_path, monkeypatch):
    root, argv, _log = _fake_lsp(tmp_path, monkeypatch, garbage="always")
    pool = ci.LspSessionPool()
    try:
        sess, _fresh = pool.acquire(root, "fake", argv, {})
        c = sess.client
        params = {"textDocument": {"uri": (root / "a.py").as_uri()}}
        with pytest.raises(ci.SessionError):
            c.request("textDocument/documentSymbol", params)
        assert c._dead                                # cause recorded for diagnosis
        t0 = time.monotonic()
        with pytest.raises(ci.SessionError):
            c.request("textDocument/documentSymbol", params)
        assert time.monotonic() - t0 < 1.0            # fails fast, no 30s wait
        assert c._resp == {}
    finally:
        pool.close_all()


def test_timed_out_request_pops_its_own_response_slot(tmp_path, monkeypatch):
    """A request that times out must not leave its id behind: the answer, if it
    ever arrives, has no waiter and would otherwise accumulate for the session's
    whole life."""
    root, argv, _log = _fake_lsp(tmp_path, monkeypatch)
    pool = ci.LspSessionPool()
    try:
        sess, _fresh = pool.acquire(root, "fake", argv, {})
        with pytest.raises(ci.SessionError):
            sess.client.request("test/silent", {}, timeout=0.2)
        assert sess.client._resp == {}
    finally:
        pool.close_all()


# ── 2.5 escaping is a real codec ──────────────────────────────────────────────
def test_escape_round_trips_and_always_yields_valid_toml():
    """The contract of the shared codec: `unesc(esc(s)) == s` for ANY string, and
    `"{esc(s)}"` always parses as the same TOML basic string. Newlines, quotes,
    backslashes, tabs, control characters and non-BMP text included."""
    alphabet = "ab\\\"'\n\r\t\x00\x0b\x1b\x7f é漢🙂 =[],#"
    rnd = random.Random(20260805)
    samples = ["", "\\", '"', "\n", "\\n", "\\\\n", 'a\\"b', "\t\r\n"]
    samples += ["".join(rnd.choice(alphabet) for _ in range(rnd.randrange(1, 24)))
                for _ in range(400)]
    for s in samples:
        enc = tomlrec.esc(s)
        assert tomlrec.unesc(enc) == s, repr(s)
        assert tomllib.loads(f'v = "{enc}"')["v"] == s, repr(s)


def test_record_round_trips_a_multiline_description():
    """The 2.5 defect: `_esc` left `\\n` alone, `_render` emitted a one-line scalar,
    and the line-oriented reader truncated the value and misread the rest of the
    record. Re-rendering must also be byte-stable (no escape doubling)."""
    e = {"fqname": "m.C.f", "name": "f", "kind": "method", "lang": "python",
         "module": "m", "parent": "m.C", "content_hash": "abc", "file": "m.py",
         "file_hash": "fh", "line": 1, "mtime": 7,
         "signature": r'def f(self) -> "C":  # re.match(r"\d+", s)',
         "description": 'first line\nsecond "line"\twith a tab\\and a backslash',
         "container": ["C"], "calls": ["g [x.py]"], "called_by": [],
         "references": ["mentions\nnewline [t.py]"], "name_terms": ["f"]}
    text = ci._render(e)
    got = ci._parse(text)
    assert got["description"] == e["description"]
    assert got["signature"] == e["signature"]
    assert got["references"] == e["references"]
    assert ci._render(got) == text                    # stable across read-modify-write


def test_broken_record_is_marked_dirty_not_partially_parsed(tmp_path):
    """A truncated record is never half-believed: identity is salvaged so the file
    can be re-extracted, `content_hash` goes blank (the merge-dirty marker the
    revalidate/reconcile sweeps already act on), and the payload is dropped so the
    symbol re-enters the describe backlog."""
    si = SymbolIndex(tmp_path)
    p = si.write({"fqname": "m.f", "name": "f", "kind": "function", "lang": "python",
                  "module": "m", "parent": "", "content_hash": "abc", "file": "m.py",
                  "file_hash": "fh", "line": 1, "mtime": 7, "signature": "def f():",
                  "description": "does a thing", "container": [], "calls": [],
                  "called_by": [], "references": [], "name_terms": ["f"],
                  "keywords": ["kw"]})
    text = p.read_text()
    p.write_text(text[:text.index("signature")] + 'signature = "def f(\n')

    e = ci._parse(p.read_text())
    assert e["name"] == "f" and e["file"] == "m.py"        # routable back to its file
    assert e["content_hash"] == ""                         # dirty
    assert "description" not in e and "keywords" not in e  # never guessed at


def test_dirty_record_is_swept_back_into_the_index_and_rewritten_valid(
        crib, tmp_path, monkeypatch):
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def alpha(): pass\n")
    store = SymbolIndex(crib.paths.project_dir("p"))
    p = store.write(_sym("pkg.mod.alpha", "H", file="pkg/mod.py"))
    store.set_source_root(root)
    text = p.read_text()
    p.write_text(text[:text.index("signature")] + 'signature = "def alpha(\n')

    reindexed: list[str] = []
    monkeypatch.setattr(
        crib, "_index_code_file_tracked",
        lambda rt, rel, proj, patch_edges, existing=None: reindexed.append(rel))
    crib._revalidate("p")
    assert reindexed == ["pkg/mod.py"]                 # the backlog picks it up

    store.write(_sym("pkg.mod.alpha", "H", file="pkg/mod.py"))   # what it rebuilds to
    assert tomllib.loads(p.read_text())["content_hash"] == "h_pkg.mod.alpha"


def test_symbol_record_write_is_atomic(tmp_path, monkeypatch):
    """The rename is the only visible step: a failure mid-write leaves the previous
    record whole, and the temp file never matches the store's `*.toml` glob."""
    si = SymbolIndex(tmp_path)
    entry = {"fqname": "m.f", "name": "f", "kind": "function", "content_hash": "h",
             "file": "m.py", "line": 1, "mtime": 1, "description": "original",
             "container": [], "calls": [], "called_by": [], "references": [],
             "name_terms": ["f"]}
    p = si.write(entry)
    before = p.read_text()

    def boom(_src, _dst):
        raise OSError("no space left on device")

    monkeypatch.setattr("crib.tomlrec.os.replace", boom)
    with pytest.raises(OSError):
        si.write({**entry, "description": "clobbered"})
    assert p.read_text() == before                       # untouched, not truncated
    assert [q.name for q in si.root.glob("*.toml")] == [p.name]


def test_section_index_escapes_newlines_and_writes_atomically(tmp_path):
    si = SectionIndex(tmp_path, "keyword_index")
    terms = ["multi\nline term", 'with a "quote"', "tab\there", r"back\slash"]
    p = si.write("keywords", "sec1", terms, relpath="a.md", heading="H\nJ")
    assert si.read_terms("keywords", "sec1") == terms    # exact round-trip
    assert tomllib.loads(p.read_text())["heading"] == "H\nJ"
    assert not list(p.parent.glob(".*"))                 # no temp left behind


def test_unparseable_symbol_record_is_dropped_from_all(tmp_path):
    """A record broken past even its identity has nothing to key on; `all()` skips
    it rather than handing downstream a dict with no `fqname` to index by."""
    si = SymbolIndex(tmp_path)
    si.write({"fqname": "m.f", "name": "f", "kind": "function", "content_hash": "h",
              "file": "m.py", "line": 1, "mtime": 1, "description": "d",
              "container": [], "calls": [], "called_by": [], "references": [],
              "name_terms": ["f"]})
    (si.root / "junk.toml").write_text('= "no key at all\n')
    assert [e["symbol_ref"] for e in si.all()] == ["m.py#f"]


def run(coro):
    return asyncio.run(coro)


def test_sweep_reports_unreadable_files(crib, tmp_path, monkeypatch):
    """A hole in the index is never invisible: the project sweep collects unreadable
    files into `skipped` (an ordinary non-code file self-skipping stays silent)."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text("def a(): pass\n")
    (root / "n.py").write_text("def b(): pass\n")
    monkeypatch.setattr(crib.indexer.services, "enumerate_code_files",
                        lambda r, globs: [root / "m.py", root / "n.py"])

    def index_one(rt, rel, proj, patch_edges, existing=None, describe_mode="inline", sweep=False):
        if rel == "n.py":
            return {"symbols": 0, "skipped": "n.py: bad codec",
                    "skipped_kind": "unreadable"}
        return {"symbols": 1, "described": 1}

    monkeypatch.setattr(crib.indexer, "_index_code_file_tracked", index_one)
    out = run(crib.indexer._index_project_code("p", root, ["*.py"]))
    assert out["files_indexed"] == 1
    assert [s["error"] for s in out["skipped"]] == ["n.py: bad codec"]
