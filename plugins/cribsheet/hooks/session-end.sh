#!/usr/bin/env bash
# SessionEnd: detach this session from the shared crib backend. sharedserver is
# refcounted, so crib keeps running while other sessions hold it and stops (after
# the grace period) once the last client leaves. No-op when crib is externally
# managed (CRIB_EXTERNAL) — we never started it, so we don't stop it.
set -euo pipefail
[[ -n "${CRIB_EXTERNAL:-}" ]] && exit 0
ss="${SHAREDSERVER_BIN:-$(command -v sharedserver || true)}"
[[ -n "$ss" ]] && "$ss" unuse cribsheet --pid "$PPID" >/dev/null 2>&1 || true
exit 0
