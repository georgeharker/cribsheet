"""LLM generation over llmkit's bridge (knowledge-capture §2).

Thin wrapper: build an llmkit ``Provider`` from :class:`GenerateConfig`, run one
chat turn, and return the content as a string. The bridge streams content to a
*sink* and returns an exit code, so we capture by pointing it at a temp file and
reading it back. (Upstreaming a ``TextIO`` / ``chat_to_str`` sink to llmkit would
drop the temp file — a clean fast-follow; the fifo/pipe path already works too.)

The core is synchronous (``llmkit.chat`` is sync); :func:`agenerate` wraps it in
a thread so the daemon's async tools never block the event loop.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
import threading
from pathlib import Path
from typing import Any

from .config import GenerateConfig

# Live generation observability (the ops view's queue numbers): the shared
# limiter is invisible from outside the process, so counters live here —
# thread-safe because describe calls run on worker threads.
_stats_lock = threading.Lock()
_gen_stats: dict[str, int] = {"inflight": 0, "waiting": 0, "rate_limited": 0}


def generation_stats() -> dict[str, Any]:
    """Snapshot for the ops surface: what generation is doing RIGHT NOW —
    in-flight calls, callers waiting on the shared limiter, rate-limit hits,
    and seconds remaining on the shared rate-limit cooldown (0 = clear)."""
    with _stats_lock:
        stats: dict[str, Any] = dict(_gen_stats)
    with _cooldown_lock:
        stats["cooldown_s"] = round(max(_cooldown_until - time.monotonic(), 0.0), 1)
    return stats


# Rate-limit retry (2026-09-14, after a zai coding-plan sweep 429'd most of its
# describes): llmkit returns only (code, None) — the HTTP detail lives in the
# raised error's TEXT — so detection is signature matching on the message.
# Only transient throttling retries; schema/auth errors fail fast.
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "rate limited",
    "too many requests",
    "overloaded",
)
_RATE_LIMIT_ATTEMPTS = 4  # one try + three retries
_RATE_LIMIT_BACKOFF_S = 2.0  # doubled per retry: ~2s, 4s, 8s (+ jitter)

# SHARED cooldown (2026-09-14): per-call backoff alone stampedes — N parallel
# callers all back off on the same cadence and all retry into the same window
# (observed: a coding-plan sweep burning hundreds of rejected requests). A 429
# cools the WHOLE engine down; every later call waits it out before its first
# attempt.
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0


def _rate_limit_wait(attempt: int) -> float:
    """Backoff for retry `attempt` (0-based): doubling + jitter, extended to the
    shared cooldown so parallel callers don't retry into the same window — and
    a final per-caller stagger ON TOP of the cooldown, so the N workers that all
    waited out the same gate don't wake in lockstep and re-burst together."""
    delay = _RATE_LIMIT_BACKOFF_S * (2**attempt) * (0.5 + random.random())
    global _cooldown_until
    with _cooldown_lock:
        until = max(_cooldown_until, time.monotonic() + delay)
        _cooldown_until = until
        return max(until - time.monotonic(), 0.0) + random.uniform(0.0, 2.0)


def _is_rate_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _RATE_LIMIT_MARKERS)


class GenerationError(RuntimeError):
    """Raised when the bridge can't produce output — misconfigured provider, a
    missing adapter extra (e.g. `anthropic` SDK), or a non-zero exit."""


def resolve_provider(cfg: GenerateConfig, purpose: str):
    """The llmkit ``Provider`` for a purpose (``distill`` / ``elaborate``).

    With a providers/profiles ``config`` file: an explicit ``provider`` wins,
    else the ``profile``'s widget key for ``purpose`` selects one. Without a
    file: the inline single-provider fields."""
    from llmkit.bridge import Provider

    if cfg.config:
        from llmkit.bridge import load

        conf = load(str(Path(cfg.config).expanduser()))
        name = cfg.provider or (
            conf.select(cfg.profile, purpose) if cfg.profile else None
        )
        if not name:
            raise GenerationError(
                f"no generation provider for purpose {purpose!r}: set "
                f"[generate].provider, or a [profiles.{cfg.profile}].{purpose} "
                f"entry in {cfg.config}"
            )
        return conf.resolve(name)

    return Provider(
        model=cfg.model,
        adapter=cfg.adapter,
        endpoint=cfg.endpoint,
        api_key=cfg.api_key,
        api_key_env=cfg.api_key_env,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
    )


def generate(
    cfg: GenerateConfig, system: str, user: str, purpose: str = "distill"
) -> str:
    """Run one chat turn; return the content stream as a stripped string.

    `purpose` selects the provider from the config's profile. Thinking is dropped
    (``thinking="none"``) — callers want the answer, not the reasoning. Captured
    in-process via llmkit's string sink (no temp file)."""
    from llmkit.bridge import ChatRequest, chat_to_str

    provider = resolve_provider(cfg, purpose)
    request = ChatRequest(user=user, system=system)
    try:
        code, text = chat_to_str(provider, request, thinking="none")
    # SystemExit too: a misconfigured adapter must never escape as a BaseException
    # and tear down the daemon's event loop — see agenerate_structured's callers.
    except (Exception, SystemExit) as e:  # noqa: BLE001 — adapter import / call failures
        raise GenerationError(
            f"llmkit generation failed for adapter {provider.adapter!r}: {e}. "
            f"Is the provider configured and its extra installed "
            f"(llmkit[bridge]/[anthropic]/[google]/[claude])?"
        ) from e
    if code != 0:
        raise GenerationError(
            f"llmkit generation exited {code} for adapter {provider.adapter!r}; "
            f"check the provider endpoint/key and that its extra is installed."
        )
    return text.strip()


def _retry_delay(e: BaseException, attempt: int) -> float | None:
    """Retry backoff for a transient rate-limit error, else None (fail fast).

    The 429 hit is counted here — the ops view's `rate_limited` number — so
    the except handler stays free of boolean operators (the ast-grep rule
    `no-boolean-in-except` scans handler bodies, not just the clause)."""
    if not _is_rate_limit(e):
        return None
    with _stats_lock:
        _gen_stats["rate_limited"] += 1
    if attempt + 1 >= _RATE_LIMIT_ATTEMPTS:
        return None
    return _rate_limit_wait(attempt)


def generate_structured(
    cfg: GenerateConfig,
    system: str,
    user: str,
    schema: dict,
    purpose: str = "elaborate",
    schema_name: str = "emit",
    schema_description: str = "",
) -> object:
    """Run one chat turn constrained to ``schema`` (JSON Schema); return the parsed
    object (or ``None`` on a non-zero exit / unparseable output — the caller falls
    back). Adapters enforce the schema natively (anthropic forced tool-use,
    openai-compatible ``response_format``), so the model can't wander off-format."""
    from llmkit.bridge import ChatRequest, chat_structured

    provider = resolve_provider(cfg, purpose)
    request = ChatRequest(
        user=user,
        system=system,
        schema=schema,
        schema_name=schema_name,
        schema_description=schema_description,
    )
    code = 1
    data = None
    with _stats_lock:
        _gen_stats["waiting"] += 1
    try:
        for attempt in range(_RATE_LIMIT_ATTEMPTS):
            try:
                with _stats_lock:
                    _gen_stats["waiting"] -= 1
                    _gen_stats["inflight"] += 1
                try:
                    code, data = chat_structured(provider, request)
                    break
                finally:
                    with _stats_lock:
                        _gen_stats["inflight"] -= 1
            # SystemExit too: keep a misbehaving adapter from crashing the daemon (a
            # stray SystemExit is a BaseException that escapes a bare except Exception).
            # The retry decision lives in _retry_delay so this handler body stays
            # boolean-free (the no-boolean-in-except rule greps handler bodies).
            except (Exception, SystemExit) as e:  # noqa: BLE001 — adapter call failures
                delay = _retry_delay(e, attempt)
                if delay is None:
                    raise GenerationError(
                        f"llmkit structured generation failed for adapter "
                        f"{provider.adapter!r}: {e}."
                    ) from e
                # stderr not logging: the daemon's stderr is where llmkit's own
                # error text already lands — one stream, one story.
                print(
                    f"[crib] rate-limited (attempt {attempt + 1}/"
                    f"{_RATE_LIMIT_ATTEMPTS}), retrying in {delay:.0f}s: "
                    f"{str(e)[:120]}",
                    file=sys.stderr,
                )
                with _stats_lock:
                    _gen_stats["waiting"] += 1
                time.sleep(delay)
                continue
                raise GenerationError(
                    f"llmkit structured generation failed for adapter "
                    f"{provider.adapter!r}: {e}."
                ) from e
        if code != 0:
            raise GenerationError(
                f"llmkit structured generation exited {code} for adapter "
                f"{provider.adapter!r}."
            )
        return data
    finally:
        with _stats_lock:
            _gen_stats["waiting"] = max(_gen_stats["waiting"] - 1, 0)


async def agenerate(
    cfg: GenerateConfig,
    system: str,
    user: str,
    purpose: str = "distill",
    timeout: float | None = None,
) -> str:
    """Async wrapper — runs the sync bridge in a worker thread, with an optional
    per-call wall-clock cap. On timeout the coroutine is abandoned (the worker
    thread is left to the SDK's own timeout) and TimeoutError propagates, so a
    hung endpoint can't stall a batch."""
    coro = asyncio.to_thread(generate, cfg, system, user, purpose)
    if timeout:
        return await asyncio.wait_for(coro, timeout)
    return await coro


async def agenerate_structured(
    cfg: GenerateConfig,
    system: str,
    user: str,
    schema: dict,
    purpose: str = "elaborate",
    schema_name: str = "emit",
    schema_description: str = "",
    timeout: float | None = None,
) -> object:
    """Async wrapper for :func:`generate_structured` (worker thread + optional
    wall-clock cap), mirroring :func:`agenerate`."""
    coro = asyncio.to_thread(
        generate_structured,
        cfg,
        system,
        user,
        schema,
        purpose,
        schema_name,
        schema_description,
    )
    if timeout:
        return await asyncio.wait_for(coro, timeout)
    return await coro
