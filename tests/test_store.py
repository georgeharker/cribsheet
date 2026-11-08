"""Chroma store contract tests (embedded/shared/json lifecycles)."""

from __future__ import annotations

import pytest


@pytest.fixture()
def ephemeral_http(monkeypatch):
    """chromadb's HttpClient pings the server on construction; a real in-memory
    EphemeralClient (no files, no network) stands in, so the guard is tested
    without a running chroma."""
    import chromadb as chromadb_mod

    monkeypatch.delenv("CRIB_ALLOW_UNPATCHED_CHROMA_REMOTE", raising=False)
    monkeypatch.setattr(
        "chromadb.HttpClient",
        lambda host, port: chromadb_mod.EphemeralClient(),
    )


@pytest.mark.parametrize(
    "host, loopback",
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),  # all interfaces — the dangerous one
        ("192.168.1.5", False),
        ("chroma.internal", False),  # names are never resolved: not loopback
    ],
)
def test_is_loopback_host(host, loopback):
    from crib.store import is_loopback_host

    assert is_loopback_host(host) is loopback


def test_loopback_shared_mode_is_allowed(ephemeral_http):
    """CVE-2026-45829 (GHSA-f4j7-r4q5-qw2c) needs untrusted input on chroma's
    listener. crib launches `chroma run --host <host>`, so a loopback bind takes
    no off-machine input: shared mode on 127.0.0.1 works with no opt-in."""
    from crib.store import ChromaStore

    s = ChromaStore.shared("127.0.0.1", 7733)
    assert s._client.heartbeat() is not None


def test_off_box_shared_mode_refused_until_opted_in(ephemeral_http, monkeypatch):
    """A non-loopback bind is refused by default, naming the CVE and both fixes
    (bind to loopback, or opt in); either opt-in spelling lets it through."""
    from crib.errors import CribUserError
    from crib.store import ChromaStore

    with pytest.raises(CribUserError, match="CVE-2026-45829") as exc:
        ChromaStore.shared("0.0.0.0", 7733)
    msg = str(exc.value)
    assert "127.0.0.1" in msg and "allow_unpatched_remote" in msg

    s = ChromaStore.shared("0.0.0.0", 7733, allow_unpatched=True)
    assert s._client.heartbeat() is not None

    monkeypatch.setenv("CRIB_ALLOW_UNPATCHED_CHROMA_REMOTE", "1")
    s2 = ChromaStore.shared("192.168.1.5", 7733)
    assert s2._client.heartbeat() is not None


def test_off_box_refusal_happens_before_chroma_is_launched(tmp_path, monkeypatch):
    """The guard runs BEFORE `sharedserver use` starts `chroma run`: a refused
    config never puts a listener on the network, even briefly."""
    from crib import app as app_mod
    from crib import sharedserver
    from crib.config import Config
    from crib.errors import CribUserError
    from crib.paths import Paths

    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.delenv("CRIB_ALLOW_UNPATCHED_CHROMA_REMOTE", raising=False)
    launched: list[object] = []
    monkeypatch.setattr(sharedserver, "use", lambda *a, **k: launched.append(a))

    cfg = Config()
    cfg.chroma.mode = "shared"
    cfg.chroma.host = "0.0.0.0"
    with pytest.raises(CribUserError, match="CVE-2026-45829"):
        app_mod._build_shared_chroma(Paths.resolve().ensure(), cfg)
    assert launched == []  # no `chroma run` was ever started
