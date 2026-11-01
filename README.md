# nyc-transit-ripple

When the NYC subway is disrupted, does the street network break down too? This
project builds a near-zero-cost system to answer that question: it puts subway,
traffic, bus, Citi Bike, camera and weather data on one timeline, detects subway
disruptions, measures their surface "ripple", and recommends whether a rider
should go, switch modes or wait. See the full [design document](docs/design.md).

**Status: Milestone M1** — project skeleton, subway ingestion and data quality checks.

## What M1 does

- Fetches all 8 MTA subway GTFS-Realtime feeds (every line, including the
  shuttles and the Staten Island Railway). No API key is needed.
- Decodes them with `gtfs-realtime-bindings` plus the MTA's NYCT extension
  (train id, whether a train is assigned, scheduled/actual track).
- Loads the MTA "supplemented" static GTFS schedule (regular schedule plus
  planned work) and caches it as Parquet.
- Matches every realtime trip to its scheduled trip and computes, for each trip
  at each station, realtime vs. scheduled time in minutes.
- Runs data quality checks on every snapshot: deduplication, stale-feed and
  outage detection, and filtering of abnormal time jumps. Every run writes a
  quality report.
- Stores results in 1-minute UTC buckets as Parquet, queryable with DuckDB.
- Runs on GitHub Actions every 5 minutes.

Details of the tables and every quality rule: [docs/data-model.md](docs/data-model.md).

## Quick start

Requires Python 3.11+. [uv](https://docs.astral.sh/uv/) is recommended:

```bash
uv sync
```

```bash
uv run ripple ingest
```

Or with plain pip:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e .
```

```bash
ripple ingest
```

The first run downloads the static schedule (~20 MB zip, converted in a few
seconds). Output goes to `./data` (gitignored), and the quality report is
printed:

```
| Feed | Statuses | Last age (s) | Trips | Matched | Unassigned | Rows | ...
|---|---|---|---|---|---|---|---
| numbered | okx1 | 4 | 150 | 93% | 49 | 3135 | ...
| ace | okx1 | 8 | 75 | 92% | 39 | 1764 | ...
```

### Commands

| Command | Purpose |
|---|---|
| `ripple ingest [--polls N --interval S] [--feeds ace,l] [--save-raw]` | Fetch feeds, compute delays, run checks, write Parquet + report |
| `ripple ingest --replay data/raw/*/*.pb.gz` | Re-process saved raw payloads offline (no network) |
| `ripple query "SQL"` | Run SQL against the DuckDB views (`--format table/csv/json`) |
| `ripple lines [--start ISO --end ISO --route A,L --max-age 10]` | 1-minute line delays, forward-filled with freshness |
| `ripple compact [--before YYYY-MM-DD]` | Merge per-run Parquet files of past days into one file per day |
| `ripple static [--force]` | Download/convert the static GTFS only |

All commands accept `--data-dir` (default `./data` or `RIPPLE_DATA_DIR`).

### Example queries

Current delay by line:

```bash
uv run ripple query "
  SELECT route_id, direction, n_trips_measured, delay_mean_min, delay_p90_min, share_trips_delayed
  FROM line_minute
  WHERE bucket_utc = (SELECT max(bucket_utc) FROM line_minute WHERE status = 'ok')
  ORDER BY delay_mean_min DESC"
```

Most delayed stations in the last hour (observed arrivals only):

```bash
uv run ripple query "
  SELECT stop_name, route_id, count(*) AS n, round(avg(delay_min), 1) AS avg_delay
  FROM stop_delays
  WHERE time_kind = 'actual' AND bucket_utc > now()::TIMESTAMP - INTERVAL 1 HOUR
  GROUP BY ALL HAVING n >= 3 ORDER BY avg_delay DESC LIMIT 10"
```

Feed health over time:

```bash
uv run ripple query "
  SELECT feed_id, status, count(*) AS polls, round(avg(feed_age_s)) AS avg_age_s
  FROM quality_feed GROUP BY ALL ORDER BY feed_id, status"
```

From Python:

```python
from transit_ripple.storage import connect

con = connect("data")
df = con.execute("SELECT * FROM line_minute LIMIT 5").df()
```

## Data layout

```
data/
  stop_delays/date=YYYY-MM-DD/<run_id>.parquet   trip x stop delays (detail, short-term)
  line_minute/date=YYYY-MM-DD/<run_id>.parquet   route x direction x minute aggregates
  quality_feed/date=YYYY-MM-DD/<run_id>.parquet  quality counters per feed and poll
  reports/date=YYYY-MM-DD/<run_id>.json          full quality report per run
  state.json                                     dedup / time-jump state between runs
  raw/                                           raw payloads (only with --save-raw)
  static/                                        static GTFS cache (Parquet)
```

Large raw data is never committed: `data/`, `*.parquet`, `*.pb`, `*.zip` are gitignored.

## Configuration and secrets

Settings come from environment variables (prefix `RIPPLE_`), optionally loaded
from a local `.env` file (gitignored; existing environment variables win). See
[`.env.example`](.env.example) for every option, including all quality thresholds.

M1 needs no secrets. Secrets are only ever read from environment variables
(GitHub Secrets in CI, `.env` locally) and are never logged. `MTA_API_KEY` is
optional and only sent as the `x-api-key` header if set.

## Automation (GitHub Actions)

- [`ingest.yml`](.github/workflows/ingest.yml) runs every 5 minutes (and on
  manual dispatch). Each run takes 3 snapshots 60 seconds apart, then:
  - publishes the long-term aggregates (`line_minute`, `quality_feed`,
    `state.json`) to the **`data` branch**. That branch is replaced by a single
    snapshot commit each run (force-push with lease), so its history does not
    grow every 5 minutes. Past days are compacted to one file per day;
  - uploads per-stop delays and the JSON report as a workflow artifact kept for
    7 days ("raw data short-term, aggregates long-term");
  - adds the Markdown quality report to the job summary.
- [`ci.yml`](.github/workflows/ci.yml) runs lint and tests on Python 3.11–3.13.

Scheduled workflows only run from the default branch, so ingestion starts once
this is merged to `main`. Optional repository variables:

| Variable | Default | Effect |
|---|---|---|
| `RIPPLE_DATA_BRANCH` | `data` | Branch for aggregates (`main`/`master` are refused) |
| `RIPPLE_PUBLISH_DATA` | (on) | Set to `false` to stop publishing to the data branch |
| `RIPPLE_ARTIFACT_RETENTION_DAYS` | `7` | Retention of per-run artifacts |

To analyse the published history locally:

```bash
git fetch origin data && git worktree add ../ripple-data origin/data
```

```bash
uv run ripple query --data-dir ../ripple-data "SELECT count(*) FROM line_minute"
```

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Tests use synthetic static schedules and GTFS-Realtime payloads (built with the
same protobuf classes, including the NYCT extension), so they run offline.

## Known limitations (M1)

- **Coverage gaps.** GitHub cron runs at most every 5 minutes and is often
  delayed or skipped; with 3 snapshots per run, roughly 3 of every 5 minutes are
  observed at best. Gaps are explicit: `ripple lines` forward-fills for at most
  `--max-age` minutes, then reports `missing`.
- **Predictions vs. observations.** Delays at upcoming stops are MTA predictions.
  Only the current stop of a train (`time_kind = 'actual'`) is an observed
  arrival. Held or "ghost" trains with frozen predictions have their stale
  downstream rows dropped; their current-stop delay can understate the real
  delay (no dwell-based lower bound yet).
- **Schedule matching is incomplete.** Added trips, reroutes and schedule
  changes mean some trips cannot be matched; they get no delay. On a live sample
  match rates were 68–100% per feed (on the day of a schedule change). The
  weekday fallback used around schedule changes is lower confidence and labelled
  in `match_method`.
- **Unassigned trips** (no train yet) are kept in `stop_delays` but excluded
  from line statistics, because their times are just the schedule.
- **Delay-jump filter** compares with the previous observation of the same trip.
  A genuine sudden re-prediction is dropped once (the new level is then accepted).
- **Data branch size.** Measured on a simulated full day, compacted
  `line_minute` data is about 0.5 MB/day, plus the smaller `quality_feed`. That is
  fine for months but should move to GitHub Releases or free object storage
  later (design §5). The data branch keeps no git history by design.
- **Static schedule refresh** every 6 hours: planned work published in between is
  not matched until the next refresh.
- **Daylight-saving days** follow the GTFS "noon minus 12 h" rule; the MTA's own
  behaviour on transition days has not been verified.
- **Not in M1**: service alerts feed (only alert entity counts are recorded),
  delay event detection, and all non-subway sources (M2+).
- Public repositories: GitHub disables scheduled workflows after 60 days without
  repository activity; re-enable from the Actions tab if that happens.

## Roadmap

M2 delay event detection, traffic speed, Citi Bike · M3 cameras + YOLO, surface
impact score · M4 static website, rule-based explanations · M5 local Ollama
explanations, bus data, recommendation engine · M6 evaluation framework, demo.
See [docs/design.md](docs/design.md).
