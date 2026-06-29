"""Small dependency-free helpers: hashing and ULID generation."""

from __future__ import annotations

import hashlib
import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def sha1_hex(*parts: str) -> str:
    """Stable SHA-1 hex over the concatenation of the given parts."""
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")  # delimiter so (a,b) != (ab,)
    return h.hexdigest()


def short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def new_ulid() -> str:
    """A ULID: 48-bit ms timestamp + 80 bits of randomness, Crockford base32.

    Lexicographically sortable and collision-resistant. (Wall-clock time is fine
    here — this is runtime code, not a replayable workflow script.)
    """
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")  # 80 bits
    value = (ts << 80) | rnd
    return _b32(value)


def derived_ulid(*parts: str) -> str:
    """A deterministic, ULID-shaped id from a stable key (Crockford base32).

    Same key → same id on every machine, so *derived* notes (imported docs,
    mirrored memory) don't fork identity across a git sync: both machines stamp
    the byte-identical id instead of two independent random ULIDs that collide on
    the same path (DESIGN §14). Not time-ordered — that property is irrelevant for
    content whose identity is its source, not its creation moment.
    """
    digest = hashlib.sha1("\x00".join(parts).encode("utf-8")).digest()
    return _b32(int.from_bytes(digest[:16], "big"))  # 128 bits


def _b32(value: int) -> str:
    """Low 130 bits of `value` as 26 Crockford base32 chars (ULID encoding)."""
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
