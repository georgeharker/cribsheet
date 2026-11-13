"""`crib generate setup` / `crib generate test`: get LLM generation configured.

crib generates code-symbol descriptions and keywords (`elaborate`), note
summaries (`summarize`) and note rewrites (`distill`) through an llmkit
providers file, `~/.config/crib/models.toml`. Writing that file by hand needs
knowledge a new user doesn't have (adapters, key variables, profile keys,
provider quirks), so `setup`:

1. detects what this machine has (API keys in the environment, a local
   OpenAI-compatible server, the `claude` CLI, providers already defined in
   zsh-ai's models.toml);
2. lets you pick one, and a model;
3. shows the changes to models.toml and config.toml as a diff, backs both up,
   and writes them on confirmation (known quirks built in);
4. offers `test`: one small call per purpose, so a bad key or a provider that
   ignores structured output shows at setup, not as blank descriptions.

CLI only. Nothing here runs unless a person asks for it.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# What crib asks its providers for (`[profiles.<name>].<purpose>`).
PURPOSES = ("elaborate", "summarize", "distill")
PROFILE = "crib"

# A local OpenAI-compatible server on the usual ports.
LOCAL_SERVERS = (
    ("local-ollama", "http://localhost:11434/v1", "Ollama on this machine"),
    ("local-llama-swap", "http://localhost:11435/v1", "llama-swap on this machine"),
)


@dataclass
class Candidate:
    """A provider setup can write: its name in models.toml, how it is shown,
    its `[providers.<name>]` fields, and the models to offer (empty: ask)."""

    name: str
    label: str
    fields: dict[str, Any]
    models: list[str] = field(default_factory=list)
    default_model: str = ""
    source: str = "detected"


def _openai_models(
    endpoint: str, key: str | None, timeout: float = 4.0
) -> list[str] | None:
    """The model ids an OpenAI-compatible endpoint lists, or None when it
    doesn't answer."""
    req = urllib.request.Request(endpoint.rstrip("/") + "/models")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed hosts
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — not there, or not that kind of server
        return None
    return sorted(str(m.get("id")) for m in data.get("data", []) if m.get("id"))


def detect(
    env: dict[str, str] | None = None,
    list_models: Callable[[str, str | None], list[str] | None] = _openai_models,
    which: Callable[[str], str | None] = shutil.which,
    zsh_ai: Path | None = None,
) -> list[Candidate]:
    """Every provider this machine could use, best first; the Claude CLI last
    (it runs on your Claude subscription)."""
    env = dict(os.environ if env is None else env)
    out: list[Candidate] = []
    if env.get("OLLAMA_API_KEY"):
        models = list_models("https://ollama.com/v1", env["OLLAMA_API_KEY"]) or []
        out.append(
            Candidate(
                "ollama-cloud",
                "Ollama Cloud (OLLAMA_API_KEY is set)",
                {
                    "adapter": "openai-compatible",
                    "endpoint": "https://ollama.com/v1",
                    "api_key_env": "OLLAMA_API_KEY",
                    "max_tokens": 8192,
                    # Ollama Cloud ignores response_format: structured output rides
                    # a forced tool call (llmkit >= 0.2.2)
                    "structured": "tool",
                },
                models,
                "glm-5.3-flash",
            )
        )
    if env.get("OPENCODE_API_KEY"):
        models = (
            list_models("https://opencode.ai/zen/v1/", env["OPENCODE_API_KEY"]) or []
        )
        out.append(
            Candidate(
                "opencode-zen",
                "opencode zen (OPENCODE_API_KEY is set)",
                {
                    "adapter": "openai-compatible",
                    "endpoint": "https://opencode.ai/zen/v1/",
                    "api_key_env": "OPENCODE_API_KEY",
                    "max_tokens": 8192,
                },
                models,
                "qwen3.5-plus",
            )
        )
    if env.get("ANTHROPIC_API_KEY"):
        out.append(
            Candidate(
                "anthropic",
                "Anthropic API (ANTHROPIC_API_KEY is set)",
                {
                    "adapter": "anthropic",
                    "api_key_env": "ANTHROPIC_API_KEY",
                    "max_tokens": 8192,
                },
                [],
                "claude-haiku-5-5",
            )
        )
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if env.get(var):
            out.append(
                Candidate(
                    "google",
                    f"Google Gemini ({var} is set)",
                    {"adapter": "google", "api_key_env": var, "max_tokens": 8192},
                    [],
                )
            )
            break
    for name, endpoint, label in LOCAL_SERVERS:
        models = list_models(endpoint, None)
        if models is not None:
            out.append(
                Candidate(
                    name,
                    f"{label} ({endpoint})",
                    {
                        "adapter": "openai-compatible",
                        "endpoint": endpoint,
                        "api_key": "local",
                        "max_tokens": 8192,
                    },
                    models,
                )
            )
    claude = (
        Candidate(
            "claude",
            "Claude CLI via the Agent SDK (uses your Claude subscription; "
            "needs llmkit's claude extra)",
            {"adapter": "claude_code"},
            [],
            "claude-haiku-5-5",
        )
        if which("claude")
        else None
    )
    taken = {c.name for c in out} | ({claude.name} if claude else set())
    out += _from_zsh_ai(
        zsh_ai
        if zsh_ai is not None
        else Path.home() / ".config" / "zsh-ai" / "models.toml",
        taken,
    )
    if claude:
        out.append(claude)  # last: it spends your subscription
    return out


def _with_quirks(fields: dict[str, Any]) -> dict[str, Any]:
    """What a provider needs to serve crib's bulk structured generation, added
    to fields written elsewhere: Ollama Cloud ignores response_format (so the
    structured channel is a forced tool call), and a cap under 8192 tokens
    truncates a batch of descriptions mid-JSON."""
    out = dict(fields)
    if "ollama.com" in str(out.get("endpoint", "")):
        out.setdefault("structured", "tool")
    if isinstance(out.get("max_tokens"), int) and out["max_tokens"] < 8192:
        out["max_tokens"] = 8192
    return out


def _from_zsh_ai(path: Path, taken: set[str]) -> list[Candidate]:
    """zsh-ai's own providers, offered for import as they are written there."""
    import tomllib

    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []
    for name, fields in sorted((data.get("providers") or {}).items()):
        if not isinstance(fields, dict) or name in taken or "fim" in name:
            continue  # taken, or fill-in-the-middle only (not a chat model)
        model = str(fields.get("model") or "")
        out.append(
            Candidate(
                name,
                f"{name} from zsh-ai's models.toml ({fields.get('adapter', '?')}"
                + (f", {model}" if model else "")
                + ")",
                _with_quirks({k: v for k, v in fields.items() if k != "model"}),
                [],
                model,
                "zsh-ai",
            )
        )
    return out


# ── writing the files ──────────────────────────────────────────────────────


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dict):
        return (
            "{ "
            + ", ".join(f"{json.dumps(k)} = {_toml_value(x)}" for k, x in v.items())
            + " }"
        )
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))  # a TOML basic string is JSON-escaped


def set_keys(text: str, section: str, values: dict[str, Any], comment: str = "") -> str:
    """`text` with `values` set inside `[section]`: an existing `key = …` line is
    replaced in place, a missing key added after the header, and a missing
    section appended. Everything else (comments, other keys) is left as is."""
    lines = text.splitlines()
    header = re.compile(r"^\s*\[" + re.escape(section) + r"\]\s*(#.*)?$")
    start = next((i for i, ln in enumerate(lines) if header.match(ln)), None)
    if start is None:
        block = (
            ([""] if text.strip() else [])
            + ([f"# {comment}"] if comment else [])
            + [f"[{section}]"]
            + [f"{k} = {_toml_value(v)}" for k, v in values.items()]
        )
        return "\n".join(lines + block) + "\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])),
        len(lines),
    )
    pending = dict(values)
    for i in range(start + 1, end):
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", lines[i])
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)} = {_toml_value(pending.pop(m.group(1)))}"
    lines[start + 1 : start + 1] = [
        f"{k} = {_toml_value(v)}" for k, v in pending.items()
    ]
    return "\n".join(lines) + "\n"


@dataclass
class Change:
    path: Path
    before: str
    after: str

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"{self.path} (now)",
                tofile=f"{self.path} (after setup)",
            )
        )


def plan(
    config_file: Path, models_file: Path, cand: Candidate, model: str, mode: str | None
) -> list[Change]:
    """The edits that make `cand` crib's provider for every purpose."""
    before_models = models_file.read_text() if models_file.exists() else ""
    models = before_models or (
        "# crib's LLM providers (llmkit format), written by `crib generate setup`.\n"
        "# crib asks for three purposes: elaborate (code descriptions and\n"
        "# keywords), summarize (note summaries) and distill (note rewrites).\n"
    )
    fields = {**cand.fields, **({"model": model} if model else {})}
    models = set_keys(models, f"providers.{cand.name}", fields, comment=cand.label)
    models = set_keys(
        models,
        f"profiles.{PROFILE}",
        {p: cand.name for p in PURPOSES},
        comment="which provider crib uses for each purpose",
    )
    before_cfg = config_file.read_text() if config_file.exists() else ""
    gen: dict[str, Any] = {"config": _home_relative(models_file), "profile": PROFILE}
    if mode:
        gen["mode"] = mode
    cfg = set_keys(before_cfg, "generate", gen)
    return [
        c
        for c in (
            Change(models_file, before_models, models),
            Change(config_file, before_cfg, cfg),
        )
        if c.before != c.after
    ]


def _home_relative(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home) :] if s.startswith(home + os.sep) else s


def apply(changes: list[Change], backup_dir: Path) -> list[Path]:
    """Write `changes`, each existing file backed up first under `backup_dir`
    (not beside it: the config may live in a dotfiles repo). Symlinks are
    written through, so a linked config is edited where it lives."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = []
    for c in changes:
        if c.before:
            backup_dir.mkdir(parents=True, exist_ok=True)
            b = backup_dir / f"{c.path.name}.{stamp}"
            b.write_text(c.before)
            backups.append(b)
        target = c.path.resolve() if c.path.is_symlink() else c.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(c.after)
    return backups


# ── test ───────────────────────────────────────────────────────────────────

_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


def test_purposes(gen_cfg: Any) -> list[dict[str, Any]]:
    """One small call per purpose: structured for elaborate/summarize (what
    crib's bulk generation uses), plain for distill."""
    from .generate import generate, generate_structured

    rows = []
    for purpose in PURPOSES:
        row: dict[str, Any] = {"purpose": purpose}
        t0 = time.monotonic()
        try:
            if purpose == "distill":
                text = generate(gen_cfg, "Answer in one word.", "Say ok.", purpose)
                row["ok"] = bool(text.strip())
            else:
                data = generate_structured(
                    gen_cfg,
                    "Reply with JSON matching the schema.",
                    'Return {"ok": true}.',
                    _SCHEMA,
                    purpose=purpose,
                    schema_name="check",
                )
                row["ok"] = isinstance(data, dict) and data.get("ok") is True
                if not row["ok"]:
                    row["error"] = f"structured reply did not match: {data!r}"[:200]
        except Exception as e:  # noqa: BLE001 — reported, per purpose
            row["ok"] = False
            row["error"] = str(e)[:300]
        row["seconds"] = round(time.monotonic() - t0, 1)
        rows.append(row)
    return rows


# ── the interactive flow ───────────────────────────────────────────────────


def run_setup(
    paths: Any,
    candidates: list[Candidate],
    ask: Callable[[str], str],
    say: Callable[[str], None],
    assume_yes: bool = False,
    pick: str | None = None,
    model: str | None = None,
    mode: str | None = None,
) -> int:
    """Pick a candidate (and model), show the diff, write on confirmation."""
    if not candidates:
        say(
            "No provider found on this machine: set an API key (OLLAMA_API_KEY, "
            "OPENCODE_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY), run a local "
            "OpenAI-compatible server, or install the claude CLI, then run this again."
        )
        return 1
    if pick:
        chosen = next((c for c in candidates if c.name == pick), None)
        if chosen is None:
            say(f"no candidate {pick!r}: found {[c.name for c in candidates]}")
            return 1
    else:
        say("Generation providers found on this machine:")
        for i, c in enumerate(candidates, 1):
            say(f"  {i}. {c.label}")
        n = ask(f"Use which? [1-{len(candidates)}] ").strip() or "1"
        if not n.isdigit() or not 1 <= int(n) <= len(candidates):
            say("nothing chosen; no changes")
            return 1
        chosen = candidates[int(n) - 1]
    if model is None:
        if chosen.models:
            say(f"Models at {chosen.name}:")
            for i, m in enumerate(chosen.models[:30], 1):
                say(f"  {i}. {m}")
        default = chosen.default_model or (chosen.models[0] if chosen.models else "")
        got = (
            ""
            if assume_yes
            else ask(f"Model{f' [{default}]' if default else ''}: ").strip()
        )
        if got.isdigit() and chosen.models and 1 <= int(got) <= len(chosen.models[:30]):
            got = chosen.models[int(got) - 1]
        model = got or default
    if not model and chosen.fields.get("adapter") != "claude_code":
        say("a model is needed; no changes")
        return 1
    if mode is None and not assume_yes:
        got = (
            ask(
                "Generate in the background as code and notes change (on), or only "
                "when you run a verb that asks (explicit)? [on] "
            )
            .strip()
            .lower()
        )
        mode = got or "on"
    if mode not in (None, "on", "explicit", "off"):
        say(f"unknown mode {mode!r}: on, explicit or off")
        return 1
    models_file = paths.config_dir / "models.toml"
    changes = plan(paths.config_file, models_file, chosen, model, mode)
    if not changes:
        say("Already set up this way; nothing to change.")
        return 0
    for c in changes:
        say(c.diff())
    if not assume_yes and ask("Write these changes? [y/N] ").strip().lower() not in (
        "y",
        "yes",
    ):
        say("no changes written")
        return 1
    backups = apply(changes, paths.index_dir / "config-backups")
    for b in backups:
        say(f"backed up to {b}")
    say(
        "Done. Check it with `crib generate test`; a running daemon picks the "
        "new provider up on its next restart."
    )
    return 0
