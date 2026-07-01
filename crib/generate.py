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
import tempfile
from pathlib import Path

from .config import GenerateConfig


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
        name = cfg.provider or (conf.select(cfg.profile, purpose) if cfg.profile else None)
        if not name:
            raise GenerationError(
                f"no generation provider for purpose {purpose!r}: set "
                f"[generate].provider, or a [profiles.{cfg.profile}].{purpose} "
                f"entry in {cfg.config}")
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


def generate(cfg: GenerateConfig, system: str, user: str,
             purpose: str = "distill") -> str:
    """Run one chat turn; return the content stream as a stripped string.

    `purpose` selects the provider from the config's profile. Thinking is dropped
    (``thinking="none"``) — callers want the answer, not the reasoning."""
    from llmkit.bridge import ChatRequest, chat

    provider = resolve_provider(cfg, purpose)
    request = ChatRequest(user=user, system=system)
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as tf:
        out_path = tf.name
    try:
        try:
            code = chat(provider, request, content=out_path, thinking="none")
        except Exception as e:  # noqa: BLE001 — adapter import / call failures
            raise GenerationError(
                f"llmkit generation failed for adapter {provider.adapter!r}: {e}. "
                f"Is the provider configured and its extra installed "
                f"(llmkit[bridge]/[anthropic]/[google]/[claude])?"
            ) from e
        text = Path(out_path).read_text()
    finally:
        try:
            Path(out_path).unlink()
        except OSError:
            pass
    if code != 0:
        raise GenerationError(
            f"llmkit generation exited {code} for adapter {provider.adapter!r}; "
            f"check the provider endpoint/key and that its extra is installed.")
    return text.strip()


async def agenerate(cfg: GenerateConfig, system: str, user: str,
                    purpose: str = "distill", timeout: float | None = None) -> str:
    """Async wrapper — runs the sync bridge in a worker thread, with an optional
    per-call wall-clock cap. On timeout the coroutine is abandoned (the worker
    thread is left to the SDK's own timeout) and TimeoutError propagates, so a
    hung endpoint can't stall a batch."""
    coro = asyncio.to_thread(generate, cfg, system, user, purpose)
    if timeout:
        return await asyncio.wait_for(coro, timeout)
    return await coro
