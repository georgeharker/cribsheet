#!/usr/bin/env bash
# Wait for release.yml's test jobs on this commit and exit with their verdict.
#
# One release tag (vX.Y.Z) starts three workflows: release.yml (PyPI), and the
# OpenCode and Pi plugin releases. The suite runs ONCE, in release.yml (it calls
# ci.yml); each plugin workflow waits here for those test jobs instead of running
# its own copy, and publishes only if they passed. The plugins keep their own
# workflow files (npm trusted publishing is configured per workflow filename).
#
# Env: GH_TOKEN (actions: read), REPO (owner/name), SHA (the tagged commit).
set -euo pipefail
: "${GH_TOKEN:?}" "${REPO:?}" "${SHA:?}"
deadline=$(( $(date +%s) + ${WAIT_MINUTES:-75} * 60 ))
while :; do
  run=$(gh api "repos/$REPO/actions/workflows/release.yml/runs?head_sha=$SHA&event=push&per_page=1" \
        --jq '.workflow_runs[0].id // empty')
  if [[ -n "$run" ]]; then
    # the called ci.yml's job(s), named "tests / tests"
    jobs=$(gh api "repos/$REPO/actions/runs/$run/jobs?per_page=100" \
           --jq '[.jobs[] | select(.name | startswith("tests"))]')
    total=$(jq 'length' <<<"$jobs")
    bad=$(jq '[.[] | select(.conclusion != null and .conclusion != "success")] | length' <<<"$jobs")
    good=$(jq '[.[] | select(.conclusion == "success")] | length' <<<"$jobs")
    if (( bad > 0 )); then
      echo "release.yml's tests failed on $SHA (run $run): not publishing"
      jq -r '.[] | "  \(.name): \(.conclusion // .status)"' <<<"$jobs"
      exit 1
    fi
    if (( total > 0 && good == total )); then
      echo "release.yml's tests passed on $SHA (run $run)"
      exit 0
    fi
    echo "waiting: $good of $total test job(s) done (run $run)"
  else
    echo "waiting for release.yml to start on $SHA"
  fi
  if (( $(date +%s) > deadline )); then
    echo "gave up waiting for release.yml's tests on $SHA"
    exit 1
  fi
  sleep 30
done
