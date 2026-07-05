#!/usr/bin/env bash
# SessionStart: bring up the shared crib backend (via sharedserver, respecting a
# globally-installed `crib`), then inject the reach-for-crib directive.
#
# The `mcpServers` entry in plugin.json points at http://127.0.0.1:7732/mcp; this
# hook ensures a warm crib is serving there, refcounted so many sessions share ONE
# process (it survives restarts within the grace period, and stops when the last
# client leaves). Set CRIB_EXTERNAL=1 to skip the backend management entirely —
# use that when crib is already served for you (e.g. by mcp-combiner/mcp-companion,
# or your own external service); the instructions-only plugin is usually the better
# fit in that case.
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${CRIB_EXTERNAL:-}" ]]; then
  ss="${SHAREDSERVER_BIN:-$(command -v sharedserver || true)}"
  if [[ -n "$ss" ]] && command -v crib >/dev/null 2>&1; then
    "$ss" use cribsheet --pid "$PPID" --grace-period 1h -- \
      crib --mcp --http --host 127.0.0.1 --port 7732 >/dev/null 2>&1 || true
  elif [[ -z "$ss" ]]; then
    echo '{"systemMessage":"cribsheet plugin: `sharedserver` not on PATH — the crib MCP backend will not start. Install it (cargo install sharedserver), run crib externally, or use the cribsheet-instructions plugin."}'
  fi
fi

txt="$dir/instructions.txt"
if [[ -f "$txt" ]] && command -v jq >/dev/null 2>&1; then
  jq -Rs '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:.}}' <"$txt"
elif [[ -f "$txt" ]]; then
  cat "$txt"
fi
