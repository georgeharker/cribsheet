#!/usr/bin/env bash
# SessionStart: converge crib's MCP registration to match the environment, warm the
# backend when we own it, then inject the reach-for-crib directive.
#
# See docs/plugin-mcp-registration.md. The switch is a GLOBAL toggle — set once in
# zshenv, never varied per session (the user-scope MCP registry it drives is global,
# so two sessions disagreeing would thrash each other):
#
#   MCP_COMBINER=1                     a combiner serves my MCPs -> don't register
#   MCP_COMBINER_SERVES_CRIBSHEET=0/1  per-backend override (wins)
#   (nothing set)                      standalone -> register + warm crib
#
# This is a CONVERGENCE step, not a one-way disable: both branches mutate, so setting
# the switch flips to combiner and unsetting it flips back, with no manual `claude
# mcp` either way. The env is the source of truth, not the registry.
#
# Registration changes land in the NEXT session (Claude Code fixes the MCP set at
# startup), and any write forces an MCP config reload — so steady state must perform
# no writes at all. Hence the guards below.
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NAME=cribsheet
URL=http://127.0.0.1:7732/mcp

_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    ''|0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}

# Does a combiner serve $1? The per-backend override wins over the global switch.
combiner_serves() {
  local name per per_set
  name=$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')
  eval "per=\${MCP_COMBINER_SERVES_$name-}"
  eval "per_set=\${MCP_COMBINER_SERVES_$name+set}"
  if [ -n "$per_set" ]; then _truthy "$per"; return; fi
  _truthy "${MCP_COMBINER-}"
}

# Is $NAME already in the user-scope MCP config?
#
# The fast path reads the config JSON directly (~35ms) because this runs on EVERY
# session start; `claude mcp get` is authoritative but costs ~1.7s, which in steady
# state is pure waste. This is only the CHECK — every mutation still goes through the
# supported CLI. Exit 2 ("can't tell": file moved, unparseable, no python) falls back
# to the slow-but-correct probe rather than guessing, because guessing "absent" would
# re-add and reload MCP on every single session.
_registered() {
  local rc=0
  python3 -c '
import json, os, sys
try:
    cands = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        cands.append(os.path.join(os.path.expanduser(cfg), ".claude.json"))
    cands += [os.path.expanduser("~/.claude.json"),
              os.path.expanduser("~/.config/claude/.claude.json")]
    for p in cands:
        if os.path.exists(p):
            with open(p) as fh:
                d = json.load(fh)
            sys.exit(0 if sys.argv[1] in (d.get("mcpServers") or {}) else 1)
    sys.exit(2)
except Exception:
    sys.exit(2)
' "$NAME" || rc=$?
  if [ "$rc" -le 1 ]; then return "$rc"; fi
  claude mcp get "$NAME" >/dev/null 2>&1
}

# All `claude`/sharedserver output is silenced: this hook's stdout IS the
# SessionStart JSON payload, and a stray line would corrupt it.
if combiner_serves "$NAME"; then
  # The combiner is the MCP. Ensure we are not registered alongside it.
  if _registered; then
    claude mcp remove "$NAME" --scope user >/dev/null 2>&1 || true
  fi
else
  # Standalone: ensure we are registered, and keep one warm crib behind it.
  if ! _registered; then
    claude mcp add --transport http "$NAME" "$URL" --scope user >/dev/null 2>&1 || true
  fi
  ss="${SHAREDSERVER_BIN:-$(command -v sharedserver || true)}"
  if [[ -n "$ss" ]] && command -v crib >/dev/null 2>&1; then
    "$ss" use "$NAME" --pid "$PPID" --grace-period 1h -- \
      crib --mcp --http --host 127.0.0.1 --port 7732 >/dev/null 2>&1 || true
  elif [[ -z "$ss" ]]; then
    echo '{"systemMessage":"cribsheet: `sharedserver` not on PATH — the crib MCP backend will not start. Install it (cargo install sharedserver), serve crib yourself, or set MCP_COMBINER=1 if a combiner already serves it."}'
  fi
fi

txt="$dir/instructions.txt"
if [[ -f "$txt" ]] && command -v jq >/dev/null 2>&1; then
  jq -Rs '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:.}}' <"$txt"
elif [[ -f "$txt" ]]; then
  cat "$txt"
fi
