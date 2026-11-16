#!/usr/bin/env bash
# Wait for release.yml's verdict on this tag and exit with it.
#
# One release tag (vX.Y.Z) starts three workflows: release.yml (PyPI), and the
# OpenCode and Pi plugin releases. The suite runs ONCE, in release.yml (it calls
# ci.yml); each plugin workflow waits here instead of running its own copy, and
# publishes only if it passed. The plugins keep their own workflow files (npm
# trusted publishing is configured per workflow filename).
#
# The verdict: release.yml's `build` job, which `needs: tests`, has started
# (tests passed) -> 0; any test job failed, `build` was skipped, or the run ended
# without it (a startup failure, an early cancel) -> 1. Only a release.yml run for
# THIS tag, created around this run (a re-pushed tag must not read an old one).
#
# Env: GH_TOKEN (actions: read), REPO (owner/name), SHA (the tagged commit),
#      TAG (the tag, github.ref_name); RUN_ID (this run, to date it; optional);
#      WAIT_MINUTES (default 75), WAIT_SLEEP (seconds between polls, default 30).
set -euo pipefail
: "${GH_TOKEN:?}" "${REPO:?}" "${SHA:?}" "${TAG:?}"
deadline=$(( $(date +%s) + ${WAIT_MINUTES:-75} * 60 ))
since=0
if [[ -n "${RUN_ID:-}" ]]; then
  since=$(gh api "repos/$REPO/actions/runs/$RUN_ID" --jq '.created_at | fromdateiso8601 - 600')
fi
while :; do
  run=$(gh api "repos/$REPO/actions/workflows/release.yml/runs?head_sha=$SHA&event=push&branch=$TAG&per_page=10" \
        --jq "[.workflow_runs[] | select((.created_at | fromdateiso8601) >= $since)] | first // empty | \"\\(.id) \\(.status)\"")
  if [[ -n "$run" ]]; then
    id=${run%% *} state=${run#* }
    jobs=$(gh api "repos/$REPO/actions/runs/$id/jobs?per_page=100" --jq '.jobs')
    failed=$(jq '[.[] | select((.name | startswith("tests")) and .conclusion != null
                               and .conclusion != "success")] | length' <<<"$jobs")
    build=$(jq -r '[.[] | select(.name == "build")] | first // {} | .conclusion // .status // "none"' <<<"$jobs")
    if (( failed > 0 )) || [[ $build == skipped || $build == cancelled ]]; then
      echo "release.yml's tests did not pass on $TAG (run $id): not publishing"
      jq -r '.[] | "  \(.name): \(.conclusion // .status)"' <<<"$jobs"
      exit 1
    fi
    if [[ $build != none && $build != skipped ]]; then
      echo "release.yml's tests passed on $TAG (run $id; build: $build)"
      exit 0
    fi
    if [[ $state == completed ]]; then
      echo "release.yml (run $id) ended without reaching its build: not publishing"
      jq -r '.[] | "  \(.name): \(.conclusion // .status)"' <<<"$jobs"
      exit 1
    fi
    echo "waiting: release.yml run $id is $state"
  else
    echo "waiting for release.yml to start on $TAG"
  fi
  if (( $(date +%s) >= deadline )); then
    echo "gave up waiting for release.yml's tests on $TAG"
    exit 1
  fi
  sleep "${WAIT_SLEEP:-30}"
done
