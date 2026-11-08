"""Chroma store contract tests (embedded/shared/json lifecycles)."""

from __future__ import annotations

import pytest



def test_shared_mode_refused_until_patched(tmp_path, monkeypatch):
    """CVE-2026-45829 (GHSA-f4j7-r4q5-qw2c): chromadb <= 1.5.9's REMOTE/server
    mode carries pre-auth code injection (trust_remote_code in the server
    collections API; embedded mode has no listener and is unaffected). Until a
    chromadb release ships the fix (chroma-core/chroma#7237), `shared` mode
    refuses by default -- a config flip must not silently open the surface."""
    from crib.errors import CribUserError
    from crib.store import ChromaStore

    with pytest.raises(CribUserError, match="CVE-2026-45829"):
        ChromaStore.shared("127.0.0.1", 7733)
    with pytest.raises(CribUserError, match="allow_unpatched_remote"):
        ChromaStore.shared("127.0.0.1", 7733, allow_unpatched=False)
    # the opt-in is accepted and the client CONSTRUCTS — with chromadb's own
    # HttpClient STUBBED (on CI/CI-like boxes its constructor pings the server;
    # the guard's job is only to check the opt-in, not that a server exists)
    # a REAL functional client (EphemeralClient: in-memory, no files, no
    # network) standing in for the pinging HttpClient the constructor builds
    import chromadb as chromadb_mod

    monkeypatch.setattr(
        "chromadb.HttpClient",
        lambda host, port: chromadb_mod.EphemeralClient(),
    )
    s = ChromaStore.shared("127.0.0.1", 7733, allow_unpatched=True)
    assert s._client.heartbeat() is not None  # constructed and alive
    # the env spelling for the daemon's process env
    monkeypatch.setenv("CRIB_ALLOW_UNPATCHED_CHROMA_REMOTE", "1")
    s2 = ChromaStore.shared("127.0.0.1", 7733)
    assert s2._client.heartbeat() is not None
