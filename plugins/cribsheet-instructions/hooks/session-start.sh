#!/usr/bin/env bash
# SessionStart: inject the reach-for-crib directive as additional context.
# Harness-agnostic content; delivered here via Claude Code's SessionStart hook so a
# plugin can carry eager instructions (which CLAUDE.md would otherwise have to).
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
txt="$dir/instructions.txt"
[ -f "$txt" ] || exit 0
if command -v jq >/dev/null 2>&1; then
  jq -Rs '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:.}}' <"$txt"
else
  # jq absent: fall back to a plain context line (Claude Code also accepts stdout text)
  cat "$txt"
fi
