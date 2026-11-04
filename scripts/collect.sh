#!/usr/bin/env bash
# Collect continuously for a number of minutes: ingest -> baselines -> events ->
# recommendations -> publish, repeated. Used by the "Ingest and analyse" workflow
# because GitHub starts scheduled runs only every few hours: one long run keeps
# the 1-minute data nearly continuous, which events (5 consecutive minutes) and
# baselines (3 days of history per slot) need.
#
#   scripts/collect.sh <minutes>
#
# Environment: POLLS, INTERVAL (subway snapshots per iteration), SAVE_RAW,
# PUBLISH=1 to publish the data branch after every iteration, RIPPLE_DATA_DIR.
# A failing step is reported and the loop goes on; the script exits non-zero at
# the end if any step failed (data is still published after each good ingest).
set -uo pipefail

minutes="${1:?usage: collect.sh <minutes>}"
data_dir="${RIPPLE_DATA_DIR:-datastore}"
polls="${POLLS:-5}"
interval="${INTERVAL:-60}"
end=$(( $(date +%s) + minutes * 60 ))
# NYC DOT traffic speed is off unless RIPPLE_TRAFFIC_ENABLED=true (see config.py).
sources="subway,bus,alerts,bikes"
if [ "${RIPPLE_TRAFFIC_ENABLED:-false}" = "true" ]; then
  sources="${sources},traffic"
fi
i=0
failures=0
last_maintenance=""

step() {
  local name="$1"
  shift
  if ! "$@"; then
    echo "::error::iteration ${i}: ${name} failed"
    failures=$((failures + 1))
    return 1
  fi
}

while :; do
  i=$((i + 1))
  echo "::group::Iteration ${i} ($(date -u +%H:%M:%S) UTC)"
  ingested=0
  step ingest uv run --no-sync ripple ingest \
    --sources "$sources" \
    --polls "$polls" --interval "$interval" ${SAVE_RAW:+--save-raw} \
    --report-md "${RUNNER_TEMP:-/tmp}/quality-report.md" && ingested=1
  step baselines uv run --no-sync ripple baselines
  if [ "$i" -eq 1 ]; then
    # Camera inventory (weekly) and camera-station mapping; events still run without it.
    uv run --no-sync ripple-cameras map || echo "::warning::camera mapping failed"
  fi
  step events uv run --no-sync ripple events
  step recommend uv run --no-sync ripple recommend
  hour="$(date -u +%Y%m%d%H)"
  if [ "$hour" != "$last_maintenance" ]; then
    step compact uv run --no-sync ripple compact
    step prune-bikes uv run --no-sync ripple prune --table bike_minute
    step prune-buses uv run --no-sync ripple prune --table bus_minute
    last_maintenance="$hour"
  fi
  # Per-stop detail is only needed for the last minutes (recommendations) and is
  # uploaded as a short-lived artifact at the end: keep the last hour on disk.
  find "$data_dir/stop_delays" -name '*.parquet' -mmin +60 -delete 2>/dev/null || true
  if [ "$ingested" = 1 ] && [ "${PUBLISH:-0}" = 1 ]; then
    step publish scripts/data_branch.sh publish "$data_dir" \
      "Ingest run ${GITHUB_RUN_ID:-local}, iteration ${i}"
  fi
  echo "::endgroup::"
  if [ "$(date +%s)" -ge "$end" ]; then
    break
  fi
done

echo "Collected for ${minutes} min: ${i} iteration(s), ${failures} failed step(s)."
[ "$failures" -eq 0 ]
