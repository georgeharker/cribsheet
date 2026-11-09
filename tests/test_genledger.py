"""The generation ledger (crib.genledger): back off from inputs an LLM already
failed on, and forget them on success or when the input or provider changes.

The policy is tested against an injected clock; the storage against every
backend that can hold it (memory fallback, DuckDB embedded, DuckDB over Quack).
"""

from __future__ import annotations

import socket

import pytest

from crib.genledger import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    GenLedger,
    MemoryLedgerStorage,
    backoff_s,
    for_store,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


# ── storage backends ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def quack_server(tmp_path_factory):
    from crib.store_quack import spawn_server

    root = tmp_path_factory.mktemp("ledger-quack")
    try:
        srv = spawn_server(
            root / "crib.duckdb", _free_port(), root / "token", timeout=180
        )
    except RuntimeError as e:
        pytest.skip(f"crib DuckDB server did not start: {e}")
    yield srv
    srv.stop()


@pytest.fixture(params=["memory", "duckdb-embedded", "duckdb-quack"])
def storage(request, tmp_path):
    if request.param == "memory":
        return MemoryLedgerStorage()
    if request.param == "duckdb-embedded":
        from crib.store_duckdb import DuckDBStore

        s = DuckDBStore.embedded(tmp_path / "crib.duckdb")
        request.addfinalizer(s.close)
        return s
    from crib.store_quack import QuackStore

    srv = request.getfixturevalue("quack_server")
    s = QuackStore.connect(srv.uri, srv.token)
    request.addfinalizer(s.close)
    s.ledger_delete(
        "describe", [f"p:{n}" for n in "abcdefgh"]
    )  # shared server: start clean
    s.ledger_delete("enrich", [f"p:{n}" for n in "abcdefgh"])
    return s


@pytest.fixture
def ledger(storage):
    clock = Clock()
    return GenLedger(storage, clock), clock


# ── policy ───────────────────────────────────────────────────────────────────


def test_backoff_schedule():
    assert backoff_s(1) == BACKOFF_BASE_S
    assert backoff_s(2) == 4 * BACKOFF_BASE_S
    assert backoff_s(3) == 16 * BACKOFF_BASE_S
    assert backoff_s(50) == BACKOFF_CAP_S  # capped, never unbounded


def test_unknown_targets_are_due(ledger):
    led, _ = ledger
    ready, waiting = led.due("describe", {"p:a": "h1", "p:b": "h2"}, "glm")
    assert sorted(ready) == ["p:a", "p:b"] and waiting == {}


def test_a_failure_backs_off_until_its_retry_time(ledger):
    led, clock = ledger
    led.failed("describe", {"p:a": "h1"}, "glm", "structured output: missing 'symbols'")
    ready, waiting = led.due("describe", {"p:a": "h1"}, "glm")
    assert ready == [] and waiting["p:a"].attempts == 1
    assert "missing 'symbols'" in waiting["p:a"].last_error
    clock.t += BACKOFF_BASE_S - 1
    assert led.due("describe", {"p:a": "h1"}, "glm")[0] == []
    clock.t += 2
    assert led.due("describe", {"p:a": "h1"}, "glm")[0] == ["p:a"]


def test_consecutive_failures_back_off_longer(ledger):
    led, clock = ledger
    for _ in range(3):
        led.failed("describe", {"p:a": "h1"}, "glm", "boom")
    e = led.due("describe", {"p:a": "h1"}, "glm")[1]["p:a"]
    assert e.attempts == 3
    assert e.next_retry == pytest.approx(clock.t + 16 * BACKOFF_BASE_S)


def test_changed_input_or_provider_is_due_at_once(ledger):
    led, _ = ledger
    led.failed("describe", {"p:a": "h1"}, "glm", "boom")
    assert led.due("describe", {"p:a": "h2"}, "glm")[0] == ["p:a"]  # the file changed
    assert led.due("describe", {"p:a": "h1"}, "claude")[0] == ["p:a"]  # new provider
    led.failed(
        "describe", {"p:a": "h2"}, "glm", "boom"
    )  # a new input restarts the count
    assert led.due("describe", {"p:a": "h2"}, "glm")[1]["p:a"].attempts == 1


def test_success_forgets(ledger):
    led, _ = ledger
    led.failed("describe", {"p:a": "h1", "p:b": "h1"}, "glm", "boom")
    led.succeeded("describe", ["p:a"])
    ready, waiting = led.due("describe", {"p:a": "h1", "p:b": "h1"}, "glm")
    assert ready == ["p:a"] and list(waiting) == ["p:b"]


def test_purposes_are_independent(ledger):
    led, _ = ledger
    led.failed("describe", {"p:a": "h1"}, "glm", "boom")
    assert led.due("enrich", {"p:a": "h1"}, "glm")[0] == ["p:a"]


def test_counts(ledger):
    led, clock = ledger
    led.failed("describe", {"p:a": "h", "p:b": "h"}, "glm", "boom")
    led.failed("enrich", {"p:c": "h"}, "glm", "boom")
    c = led.counts()
    assert c["describe"] == {"failing": 2, "backing_off": 2}
    assert c["enrich"]["failing"] == 1


def test_hostile_text_round_trips(ledger):
    """The error text and targets go through the Quack literal path too."""
    led, _ = ledger
    nasty = "'); DROP TABLE crib_gen_ledger; -- naïve 🦆 \\ \" ?"
    led.failed("describe", {"p:a": nasty}, nasty, nasty)
    e = led.due("describe", {"p:a": nasty}, nasty)[1]["p:a"]
    assert (e.input_hash, e.provider, e.last_error) == (nasty, nasty, nasty)


# ── persistence and store integration ────────────────────────────────────────


def test_duckdb_ledger_survives_reopen_and_a_vector_rebuild(tmp_path):
    from crib.store import Record
    from crib.store_duckdb import DuckDBStore

    path = tmp_path / "crib.duckdb"
    s = DuckDBStore.embedded(path)
    s.upsert([Record("c", [1.0, 0.0], "doc", {"project": "p"})])
    GenLedger(s).failed("describe", {"p:a": "h1"}, "glm", "boom")
    s.recreate()  # drops the vectors only
    s.close()
    s = DuckDBStore.embedded(path)
    try:
        assert (
            GenLedger(s).due("describe", {"p:a": "h1"}, "glm")[1]["p:a"].attempts == 1
        )
        assert s.current_dim() is None  # the rebuild really emptied the chunks
    finally:
        s.close()


def test_for_store_picks_the_store_or_memory(tmp_path):
    from crib.store import InMemoryStore
    from crib.store_duckdb import DuckDBStore

    assert isinstance(for_store(InMemoryStore())._s, MemoryLedgerStorage)
    s = DuckDBStore.embedded(tmp_path / "crib.duckdb")
    try:
        assert for_store(s)._s is s
    finally:
        s.close()
