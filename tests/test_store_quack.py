"""The DuckDB store's shared path over Quack: what the Store contract suite can't
express. The literal renderer that stands in for bound parameters, the loopback
bind and token file, the server's sandbox (attacked as a token holder would),
recovery from a killed server, and the sharedserver wiring in `_build_store`.
Store behaviour itself is in tests/test_store_contract.py (backend
`duckdb-quack`), like every other backend's."""

from __future__ import annotations

import math
import os
import socket
import subprocess
import threading

import pytest

pytest.importorskip("duckdb")

from crib import proc as spawn
from crib.errors import CribUserError  # noqa: E402
from crib.store import Record  # noqa: E402
from crib.store_quack import (  # noqa: E402
    QuackStore,
    check_shared_bind,
    ensure_token,
    render,
    spawn_server,
    to_literal,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── literals: the one place a value becomes SQL text ─────────────────────────

ROUND_TRIP = [
    0,
    -7,
    2**62,
    1.5,
    1731097200.123456,
    -0.0,
    1e-300,
    True,
    False,
    "",
    "O'Brien",
    "''",
    "back\\slash\\",
    "'); DROP TABLE crib_chunks; --",
    "naïve — 日本語 🦆",
    "\u0001\u001f\n\t control",
    "$1 ? :name {}",
    "x" * 100_000,
]


@pytest.mark.parametrize("value", ROUND_TRIP, ids=range(len(ROUND_TRIP)))
def test_literal_reads_back_exactly(value):
    import duckdb

    ((got,),) = (
        duckdb.connect(":memory:").execute(f"SELECT {to_literal(value)}").fetchall()
    )
    assert got == value and type(got) is type(value)
    if isinstance(value, float):
        assert math.copysign(1, got) == math.copysign(1, value)


def test_vector_literal_reads_back_as_float32():
    import duckdb

    vec = [0.1, -0.2, 1e-8, 0.333333]
    ((got,),) = (
        duckdb.connect(":memory:")
        .execute(f"SELECT {to_literal(vec)}::FLOAT[4]")
        .fetchall()
    )
    assert list(got) == pytest.approx(vec, rel=1e-6)


def test_vector_literal_rounds_like_a_bound_parameter():
    """The one-string vector literal stores the same float32 bits as the
    embedded path's bound parameter, including -0.0 and denormal-ish values."""
    import random

    import duckdb

    rng = random.Random(7)
    vec = [rng.uniform(-1, 1) for _ in range(384)] + [-0.0, 1e-40, 0.1, 1 / 3]
    n = len(vec)
    con = duckdb.connect(":memory:")
    ((bound,),) = con.execute(f"SELECT ?::FLOAT[{n}]", [vec]).fetchall()
    # pi-lens-ignore: python-sql-injection
    ((literal,),) = con.execute(f"SELECT {to_literal(vec)}::FLOAT[{n}]").fetchall()
    assert [math.copysign(1, x) for x in literal] == [
        math.copysign(1, x) for x in bound
    ]
    assert list(literal) == list(bound)


def test_vector_literal_refuses_non_finite():
    with pytest.raises(CribUserError):
        to_literal([0.5, math.nan])


@pytest.mark.parametrize(
    "bad",
    [math.nan, math.inf, -math.inf, "nul\x00inside", [], {"a": 1}, b"bytes", object()],
    ids=["nan", "inf", "-inf", "nul", "empty-list", "dict", "bytes", "object"],
)
def test_unrenderable_values_are_refused_not_guessed(bad):
    with pytest.raises(CribUserError):
        to_literal(bad)


def test_render_substitutes_in_order_and_checks_the_count():
    assert render("a = ? AND b = ?", ["x'y", 3]) == "a = 'x''y' AND b = 3"
    with pytest.raises(AssertionError):
        render("a = ?", [])
    with pytest.raises(AssertionError):
        render("a = ?", [1, 2])


# ── bind and token ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_is_allowed(host):
    check_shared_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "store.internal", "::"])
def test_off_box_bind_is_refused(host):
    with pytest.raises(CribUserError, match="loopback"):
        check_shared_bind(host)


def test_token_file_is_created_once_with_0600(tmp_path):
    path = tmp_path / "sub" / "quack.token"
    first = ensure_token(path)
    assert len(first) >= 32
    assert (path.stat().st_mode & 0o777) == 0o600
    assert ensure_token(path) == first  # stable across restarts


def test_concurrent_token_creation_agrees(tmp_path):
    path = tmp_path / "quack.token"
    got: list[str] = []
    threads = [
        threading.Thread(target=lambda: got.append(ensure_token(path)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 1 and path.read_text().strip() == got[0]


# ── a live server ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("quack")
    try:
        srv = spawn_server(
            root / "crib.duckdb", _free_port(), root / "token", timeout=180
        )
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    yield srv
    srv.stop()


@pytest.fixture()
def remote(server):
    """A raw server-routed cursor: what any holder of the token can send."""
    import duckdb

    from crib.store_quack import _connect_config

    conn = duckdb.connect(":memory:", config=_connect_config(None))  # crib's dirs
    conn.execute("LOAD quack")
    conn.execute(f"ATTACH '{server.uri}' AS r (TOKEN '{server.token}')")
    cur = conn.cursor()
    cur.execute("CONNECT r")
    yield cur
    conn.close()


def test_wrong_token_is_refused(server):
    with pytest.raises(Exception, match="Authentication failed"):
        QuackStore.connect(server.uri, "not-the-token")


@pytest.mark.parametrize(
    "attack",
    [
        "SELECT count(*) FROM read_csv('/etc/hosts')",
        "SELECT * FROM read_text('/etc/passwd')",
        "COPY (SELECT 42) TO '{tmp}/leak.csv'",
        "ATTACH '{tmp}/other.duckdb' AS other",
        "INSTALL httpfs",
        "SET enable_external_access = true",
        "SET lock_configuration = false",
    ],
    ids=[
        "read_csv",
        "read_text",
        "copy-to",
        "attach",
        "install",
        "unlock-files",
        "unlock-config",
    ],
)
def test_token_holder_cannot_leave_the_store(remote, tmp_path, attack):
    """A token holder reaches only crib's database: no file reads or writes, no
    other databases, no extensions, and the lockdown can't be SET back."""
    with pytest.raises(Exception, match="disabled by configuration|locked"):
        # The attack SQL IS the test input: it must be refused by the server.
        # pi-lens-ignore: python-sql-injection
        remote.execute(attack.format(tmp=tmp_path)).fetchall()
    assert not list(tmp_path.iterdir())  # nothing was written


def test_client_survives_a_server_restart(tmp_path):
    """kill -9 the server mid-session: the next operation relaunches it (here the
    hook plays sharedserver's part), opens a fresh session and succeeds, and no
    committed write was lost."""
    port, db, tok = _free_port(), tmp_path / "crib.duckdb", tmp_path / "token"
    servers = [spawn_server(db, port, tok, timeout=180)]

    def relaunch() -> None:
        servers.append(spawn_server(db, port, tok, timeout=180))

    store = QuackStore.connect(servers[0].uri, servers[0].token, relaunch=relaunch)
    try:
        store.upsert([Record("a", [1.0, 0.0], "doc a", {"project": "p"})])
        servers[0].proc.kill()
        servers[0].proc.wait(30)
        store.upsert([Record("b", [0.0, 1.0], "doc b", {"project": "p"})])
        assert len(servers) == 2  # relaunched exactly once
        assert set(store.get_meta({"project": "p"})) == {"a", "b"}
        assert [h.id for h in store.query([1.0, 0.0], 1)] == ["a"]
    finally:
        store.close()
        for s in servers:
            s.stop()


def test_client_recovers_after_a_reconnect_that_failed(tmp_path):
    """The server is still down when the client first tries to reconnect, so
    that reconnect fails. The client must not wedge on its closed connection:
    once the server is back, the next operation reconnects and succeeds."""
    port, db, tok = _free_port(), tmp_path / "crib.duckdb", tmp_path / "token"
    first = spawn_server(db, port, tok, timeout=180)
    store = QuackStore.connect(first.uri, first.token)
    second = None
    try:
        store.upsert([Record("a", [1.0, 0.0], "doc a", {"project": "p"})])
        first.stop()
        with pytest.raises(Exception):
            store.current_dim()  # server down: the reconnect fails too
        second = spawn_server(db, port, tok, timeout=180)
        assert store.current_dim() == 2
        assert set(store.get_meta({"project": "p"})) == {"a"}
    finally:
        store.close()
        if second is not None:
            second.stop()


def test_threads_that_lose_one_session_relaunch_once(tmp_path):
    """Several threads hit the same dead session: one of them relaunches and
    reconnects, the rest reuse that new session instead of relaunching too."""
    port, db, tok = _free_port(), tmp_path / "crib.duckdb", tmp_path / "token"
    servers = [spawn_server(db, port, tok, timeout=180)]

    def relaunch() -> None:
        servers.append(spawn_server(db, port, tok, timeout=180))

    store = QuackStore.connect(servers[0].uri, servers[0].token, relaunch=relaunch)
    try:
        store.upsert([Record("a", [1.0, 0.0], "doc a", {"project": "p"})])
        servers[0].proc.kill()
        servers[0].proc.wait(30)
        errors: list[BaseException] = []

        def read() -> None:
            try:
                assert set(store.get_meta({"project": "p"})) == {"a"}
            except BaseException as e:  # noqa: BLE001 — reported below
                errors.append(e)

        threads = [threading.Thread(target=read) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(240)
        assert errors == []
        assert len(servers) == 2  # one relaunch for one lost session
    finally:
        store.close()
        for s in servers:
            s.stop()


def test_real_errors_are_not_retried(server):
    """Only a lost session triggers a reconnect; a genuine error surfaces as is."""
    calls = []
    store = QuackStore.connect(
        server.uri, server.token, relaunch=lambda: calls.append(1)
    )
    try:
        store.recreate()
        store.upsert([Record("a", [1.0, 0.0], "a", {"project": "p"})])
        with pytest.raises(CribUserError, match="dimension mismatch"):
            store.query([1.0, 0.0, 0.0], 1)
        assert calls == []
    finally:
        store.close()


# ── _build_store over a stand-in sharedserver ────────────────────────────────


class FakeSharedServer:
    """Plays `sharedserver use/unuse`: starts the given command on first use,
    counts references, stops it when the count returns to zero."""

    def __init__(self, logs) -> None:
        self.refs = 0
        self.launches = 0
        self.proc: subprocess.Popen | None = None
        self.logs = logs  # one file per launch, shown when a test fails

    def use(self, name, command, grace_period=None) -> None:
        self.refs += 1
        if self.proc is None or self.proc.poll() is not None:
            self.launches += 1
            log = open(self.logs / f"server{self.launches}.log", "w")  # noqa: SIM115
            # Spawn, don't fork, as crib.sharedserver does (see crib.proc): forking
            # this Quack-client-holding process crashes the child on macOS.
            self.proc = spawn.popen(command, stdout=log, stderr=subprocess.STDOUT)

    def output(self) -> str:
        return "\n".join(
            f"--- {f.name}\n{f.read_text()[-1500:]}"
            for f in sorted(self.logs.glob("*.log"))
        )

    def unuse(self, name) -> None:
        self.refs -= 1
        if self.refs == 0 and self.proc is not None:
            self.proc.terminate()
            self.proc.wait(30)


@pytest.fixture()
def shared_env(tmp_path, monkeypatch):
    from crib import sharedserver
    from crib.config import Config
    from crib.paths import Paths

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    logs = tmp_path / "server-logs"
    logs.mkdir()
    fake = FakeSharedServer(logs)
    monkeypatch.setattr(sharedserver, "use", fake.use)
    monkeypatch.setattr(sharedserver, "unuse", fake.unuse)
    cfg = Config()
    cfg.store.backend = "duckdb"
    cfg.duckdb.mode = "shared"
    cfg.duckdb.port = _free_port()
    yield Paths.resolve().ensure(), cfg, fake
    if fake.proc is not None and fake.proc.poll() is None:
        fake.proc.kill()


def test_build_store_launches_connects_and_releases(shared_env):
    from crib.app import _build_store

    paths, cfg, fake = shared_env
    store, close = _build_store(paths, cfg)
    assert close is not None
    try:
        assert isinstance(store, QuackStore)
        assert fake.launches == 1 and fake.refs == 1
        assert (paths.duckdb_token_file.stat().st_mode & 0o777) == 0o600
        store.upsert([Record("a", [1.0, 0.0], "doc", {"project": "p"})])
        assert [h.id for h in store.query([1.0, 0.0], 1)] == ["a"]
        # a second crib process attaches to the SAME server
        other, close_other = _build_store(paths, cfg)
        assert close_other is not None
        assert fake.launches == 1 and fake.refs == 2
        assert set(other.get_meta({"project": "p"})) == {"a"}
        close_other()
        assert fake.refs == 1
    finally:
        close()
    assert fake.refs == 0  # every use paired with an unuse
    assert paths.duckdb_extension_dir.is_dir()  # crib's own, not ~/.duckdb


def test_build_store_relaunches_a_dead_server_and_balances_refs(shared_env):
    from crib.app import _build_store

    paths, cfg, fake = shared_env
    store, close = _build_store(paths, cfg)
    assert close is not None
    try:
        store.upsert([Record("a", [1.0, 0.0], "doc", {"project": "p"})])
        assert fake.proc is not None
        fake.proc.kill()
        fake.proc.wait(30)
        try:
            assert set(store.get_meta({"project": "p"})) == {"a"}  # relaunched, kept
        except Exception:
            print(fake.output())
            raise
        assert fake.launches == 2
    finally:
        close()
    assert fake.refs == 0


def test_off_box_bind_is_refused_before_anything_launches(shared_env):
    from crib.app import _build_store

    paths, cfg, fake = shared_env
    cfg.duckdb.host = "0.0.0.0"
    with pytest.raises(CribUserError, match="loopback"):
        _build_store(paths, cfg)
    assert fake.launches == 0 and fake.refs == 0


def test_unknown_mode_is_a_clear_error(shared_env):
    from crib.app import _build_store

    paths, cfg, _ = shared_env
    cfg.duckdb.mode = "sharded"
    with pytest.raises(CribUserError, match="embedded \\| shared"):
        _build_store(paths, cfg)


def test_server_refuses_to_start_off_box(tmp_path):
    with pytest.raises(RuntimeError, match="loopback"):
        spawn_server(
            tmp_path / "c.duckdb", _free_port(), tmp_path / "t", host="0.0.0.0"
        )
    assert os.listdir(tmp_path) == [] or not (tmp_path / "c.duckdb").exists()


# ── externally supervised server ([duckdb] launch = false) ──────────────────


def _bare_server(env_dir, port):
    """`python -m crib.store_quack` with NO arguments, as an external supervisor
    (a combiner sharedServers entry) runs it: everything from crib's config."""
    import sys

    cfg_dir = env_dir / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.toml").write_text(f"[duckdb]\nport = {port}\n")
    env = {
        **os.environ,
        "CRIB_CONFIG_DIR": str(cfg_dir),
        "CRIB_DATA_DIR": str(env_dir / "data"),
        "CRIB_INDEX_DIR": str(env_dir / "index"),
    }
    p = spawn.popen(
        [sys.executable, "-m", "crib.store_quack"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert p.stdout is not None
    line = p.stdout.readline()
    assert line.startswith("crib-duckdb ready"), line
    return p


def test_external_server_attach_only_never_launches(shared_env):
    from crib.app import _build_store

    paths, cfg, fake = shared_env
    cfg.duckdb.launch = False
    srv = _bare_server(paths.config_dir.parent, cfg.duckdb.port)
    try:
        store, close = _build_store(paths, cfg)
        assert close is not None
        try:
            assert isinstance(store, QuackStore)
            assert (fake.launches, fake.refs) == (0, 0)  # crib started nothing
            # the bare server used crib's own paths: same token file, same db
            assert (paths.duckdb_token_file.stat().st_mode & 0o777) == 0o600
            store.upsert([Record("a", [1.0, 0.0], "doc", {"project": "p"})])
            assert [h.id for h in store.query([1.0, 0.0], 1)] == ["a"]
        finally:
            close()
        assert (fake.launches, fake.refs) == (0, 0)
    finally:
        srv.terminate()
        srv.wait(30)
    assert paths.duckdb_file.exists()  # it served the configured store file


def test_external_server_absent_is_a_clear_error(shared_env):
    from crib.app import _build_store

    paths, cfg, fake = shared_env
    cfg.duckdb.launch = False
    cfg.duckdb.attach_timeout = 1.0
    with pytest.raises(CribUserError, match="externally managed"):
        _build_store(paths, cfg)
    assert fake.launches == 0


def test_duckdb_state_stays_in_crib_dirs(tmp_path):
    """Extensions AND DuckDB's secrets directory (quack_serve reads it) live in
    crib's own dir, never the user's ~/.duckdb."""
    from crib.store_quack import _connect_config

    ext = tmp_path / "duckdb" / "extensions"
    cfg = _connect_config(ext)
    assert cfg["extension_directory"] == str(ext)
    assert cfg["secret_directory"] == str(tmp_path / "duckdb" / "secrets")
    assert (tmp_path / "duckdb" / "secrets").is_dir()
    assert not any(
        str(v).startswith(os.path.expanduser("~/.duckdb")) for v in cfg.values()
    )


def test_crib_never_touches_home_dot_duckdb(tmp_path):
    """Every crib DuckDB path (embedded store, the Quack server, a client, a
    migration) keeps DuckDB's state in crib's own dirs. Proved by running them
    under a fresh $HOME and checking ~/.duckdb never appears. (Quack is fetched
    once into crib's extension dir under that $HOME.)"""
    import sys
    import textwrap

    home = tmp_path / "home"
    home.mkdir()
    script = textwrap.dedent(
        """
        import socket
        from crib.paths import Paths
        from crib.store import Record
        from crib.store_duckdb import DuckDBStore
        from crib.store_quack import QuackStore, spawn_server

        p = Paths.resolve().ensure()
        emb = DuckDBStore.embedded(p.duckdb_file)
        emb.upsert([Record("a", [1.0, 0.0], "doc", {"project": "p"})])
        emb.close()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = spawn_server(p.index_dir / "q.duckdb", port, p.duckdb_token_file, timeout=180)
        try:
            q = QuackStore.connect(srv.uri, srv.token)
            q.upsert([Record("b", [0.0, 1.0], "doc", {"project": "p"})])
            assert [h.id for h in q.query([0.0, 1.0], 1)] == ["b"]
            q.close()
        finally:
            srv.stop()
        print("ok")
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CRIB_", "XDG_"))}
    env["HOME"] = str(home)
    r = spawn.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0 and "extensions.duckdb.org" in (r.stdout + r.stderr):
        pytest.skip("no network to fetch the quack extension")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert not (home / ".duckdb").exists(), sorted(
        str(p) for p in (home / ".duckdb").rglob("*")
    )
    assert (home / ".cache" / "crib" / "duckdb" / "extensions").is_dir()


def test_install_extensions_fills_crib_s_own_dir(tmp_path):
    from crib.store_quack import SHARED_EXTENSIONS, install_extensions

    ext = tmp_path / "extensions"
    try:
        assert install_extensions(ext) == list(SHARED_EXTENSIONS)
    except Exception as e:  # noqa: BLE001 — offline box: nothing to check
        pytest.skip(f"extensions.duckdb.org unreachable: {e}")
    found = {p.name.split(".")[0] for p in ext.rglob("*.duckdb_extension")}
    assert set(SHARED_EXTENSIONS) <= found
