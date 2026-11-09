"""Store helpers that aren't part of the Store contract (that lives in
tests/test_store_contract.py, run against every backend)."""

from __future__ import annotations

import pytest


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
        ("store.internal", False),  # names are never resolved: not loopback
    ],
)
def test_is_loopback_host(host, loopback):
    """The shared DuckDB server's bind check (store_quack.check_shared_bind)
    rests on this: only an address reachable from this machine alone passes."""
    from crib.store import is_loopback_host

    assert is_loopback_host(host) is loopback
