#!/usr/bin/env bash
# Manage the data branch used by the ingestion workflow.
#
#   scripts/data_branch.sh prepare <dir>            check out the data branch into <dir>
#   scripts/data_branch.sh publish <dir> <message>  replace the data branch with <dir>
#
# The data branch holds only small aggregates (line_minute/, quality_feed/,
# state.json). It is published as a single snapshot commit (force-pushed with a
# lease) so its history does not grow by one commit every five minutes.
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
*.tmp
EOF
    cat >README.md <<'EOF'
# Data branch (generated)

Written by the `Ingest subway feeds` workflow. Do not commit here by hand:
each run replaces this branch with a single snapshot commit.

- `line_minute/date=YYYY-MM-DD/*.parquet`: per route x direction x 1-minute delay aggregates
- `quality_feed/date=YYYY-MM-DD/*.parquet`: per feed x poll data quality counters
- `state.json`: dedup / time-jump state carried between runs

Query with DuckDB, e.g. `ripple query --data-dir . "SELECT * FROM line_minute LIMIT 10"`.
EOF
    git checkout --quiet --orphan "ripple-snapshot-$$"
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
    # (an empty lease means the branch must not exist yet).
    git push --quiet --force-with-lease="refs/heads/$branch:$base" origin "HEAD:refs/heads/$branch"
    echo "Published data snapshot to '$branch'."
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    exit 2
    ;;
esac
