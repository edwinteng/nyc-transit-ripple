#!/usr/bin/env bash
# Manage the data branch used by the ingestion workflow.
#
#   scripts/data_branch.sh prepare <dir>            check out the data branch into <dir>
#   scripts/data_branch.sh publish <dir> <message>  replace the data branch with <dir>
#
# The data branch holds aggregates, short-retention Citi Bike minutes, events,
# camera counts (numbers only), reference tables and state files. The dashboard
# JSON is not stored here: the Pages workflow exports it from these tables. It is published as a single
# snapshot commit (force-pushed with a lease) so its history does not grow by
# one commit every five minutes.
# Detailed per-stop data and reports are excluded via the branch's .gitignore
# and are uploaded as short-lived workflow artifacts instead.
set -euo pipefail

cmd="${1:?usage: data_branch.sh prepare|publish <dir> [message]}"
dir="${2:?missing directory}"
branch="${DATA_BRANCH:-data}"
default_branch="${DEFAULT_BRANCH:-main}"

case "$branch" in
  "" | main | master | "$default_branch")
    echo "Refusing to use '$branch' as the data branch." >&2
    exit 1
    ;;
esac

base_file="$(git rev-parse --absolute-git-dir)/ripple-data-base"

case "$cmd" in
  prepare)
    git worktree remove --force "$dir" 2>/dev/null || rm -rf "$dir"
    if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
      git fetch --quiet --depth 1 origin "+refs/heads/$branch:refs/remotes/origin/$branch"
      git worktree add --quiet --detach "$dir" "refs/remotes/origin/$branch"
      git rev-parse "refs/remotes/origin/$branch" >"$base_file"
      echo "Checked out existing data branch '$branch' at $(cat "$base_file")"
    else
      git worktree add --quiet --detach "$dir"
      git -C "$dir" checkout --quiet --orphan "ripple-data-new"
      git -C "$dir" rm -rfq --cached --ignore-unmatch . >/dev/null
      git -C "$dir" clean -fdxq
      : >"$base_file"
      echo "Data branch '$branch' does not exist yet; starting empty."
    fi
    ;;

  publish)
    message="${3:-Update data snapshot}"
    cd "$dir"
    cat >.gitignore <<'EOF'
# Only aggregates are versioned here; details go to workflow artifacts.
stop_delays/
reports/
raw/
static/
site/
*.tmp
EOF
    cat >README.md <<'EOF'
# Data branch (generated)

Written by the `Ingest and analyse` workflow. Do not commit here by hand:
each run replaces this branch with a single snapshot commit.

- `line_minute/`, `station_minute/`: subway delays per line / station and minute
- `alerts/`, `alert_presence/`: MTA service alert versions and when they were in the feed
- `traffic_minute/`: NYC DOT traffic speed observations per road link
- `bike_minute/`: Citi Bike bikes and docks per station and minute (kept 35 days)
- `bus_minute/`: bus speed per route segment near subway stations and minute (kept 35 days)
- `recommendations/`: every ranked option for riders, per run (for replay evaluation)
- `events/`: subway delay events with camera counts, surface impact score and
  rule-based explanations (one row per version)
- `camera_counts/`: cars, buses, trucks and people per camera fetch (numbers only,
  counted only near ongoing events)
- `quality_feed/`, `quality_source/`: data quality counters per run
- `reference/`: stations, road links, Citi Bike stations, bus stops, cameras, 500 m
  neighbors, camera-station map, baselines
- `state.json`, `camera_state.json`: dedup / freshness state carried between runs

Query with DuckDB, e.g. `ripple query --data-dir . "SELECT * FROM events LIMIT 10"`.
EOF
    # A fresh orphan branch per publish (a long run publishes many times).
    git checkout --quiet --orphan "ripple-snapshot-$$-$(date +%s%N)"
    git add -A
    base="$(cat "$base_file")"
    if [ -n "$base" ] && [ "$(git write-tree)" = "$(git rev-parse "$base^{tree}")" ]; then
      echo "No changes to publish."
      exit 0
    fi
    git -c user.name="github-actions[bot]" \
        -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        -c commit.gpgsign=false \
        commit --quiet -m "$message"
    # Lease: only overwrite the branch if nobody pushed since we fetched it
    # (an empty lease means the branch must not exist yet). GitHub sometimes answers
    # a push with a transient server error (500, "fatal error in commit_refs"), so
    # retry with backoff; if a failed attempt in fact updated the branch, it now
    # points at our commit and that counts as published.
    head="$(git rev-parse HEAD)"
    pushed=0
    for wait in 2 4 8 16 0; do
      if git push --quiet --force-with-lease="refs/heads/$branch:$base" origin "HEAD:refs/heads/$branch"; then
        pushed=1
        break
      fi
      if [ "$(git ls-remote origin "refs/heads/$branch" | cut -f1)" = "$head" ]; then
        pushed=1
        break
      fi
      [ "$wait" -gt 0 ] || break
      echo "Push failed; retrying in ${wait}s."
      sleep "$wait"
    done
    if [ "$pushed" != 1 ]; then
      echo "Publishing to '$branch' failed after retries." >&2
      exit 1
    fi
    # The next publish of the same run leases against what was just pushed.
    echo "$head" >"$base_file"
    echo "Published data snapshot to '$branch'."
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    exit 2
    ;;
esac
