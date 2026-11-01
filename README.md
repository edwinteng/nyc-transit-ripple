# nyc-transit-ripple

When the NYC subway is disrupted, does the street network break down too? This
project builds a near-zero-cost system to answer that question: it puts subway,
traffic, bus, Citi Bike, camera and weather data on one timeline, detects subway
disruptions, measures their surface "ripple", and recommends whether a rider
should go, switch modes or wait. See the full [design document](docs/design.md).

> **Traffic cameras (M3, in progress):** camera frames are turned into counts of
> cars, buses, trucks and people (numbers only; frames are never stored and there
> is no face or license plate recognition). The detector is **YOLOX-S** by Megvii,
> whose code and pretrained weights are **Apache-2.0**, run with ONNX Runtime (MIT).
> Ultralytics YOLO models are **not** used because they are AGPL-3.0. Weights are
> downloaded at runtime and cached, never committed. See
> [docs/cameras.md](docs/cameras.md).

**Status: Milestone M2** — subway delay events with linked MTA alerts, NYC DOT
traffic speed and Citi Bike ingestion, 500 m spatial join, weekday/time-of-day
baselines, rule-based event summaries and a static event page. (M1: subway
ingestion and data quality checks.)

## What it does

### M2: events and surface context

- **Delay events.** A line has an event when its worst-direction median train
  delay is at least 5 min for 5 consecutive minutes (threshold, N and the metric
  are configurable). Each event stores start/end, affected stations, peak delay
  and linked MTA service alerts.
- **Service alerts.** The MTA subway alerts feed (GTFS-Realtime + the MTA
  "Mercury" extension) is ingested every run; alert versions and their presence
  over time are stored, and alerts that match an event's line and time are linked.
- **NYC DOT traffic speed** (NYC Open Data / Socrata, no key; optional free app
  token) and **Citi Bike GBFS** (official auto-discovery, station information and
  status; bikes and docks per station per minute) are normalized to the shared
  schema with freshness recorded.
- **Spatial join.** For every subway station: DOT road links and Citi Bike
  stations within 500 m.
- **Baselines** by weekday and 30-minute time of day (New York time) from the last
  28 days, flagged `low` confidence with fewer than 3 days of history.
- **Rule-based explanations** (no language model): a short plain-English summary
  per event that only uses numbers present in the data, stored with the list of
  cited numbers.
- **Static output:** a PNG chart per event (delay vs. nearby traffic speed and
  Citi Bike availability over time, with baselines) and an `index.html` listing
  recent events, ready for GitHub Pages.
- **Data quality** for the new sources (status, staleness, coverage, gaps) is part
  of every run's report.

### M1: subway ingestion and quality

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

```bash
uv run ripple baselines && uv run ripple events && uv run ripple site
```

Then open `data/site/index.html`. Or with plain pip:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e .
```

```bash
ripple ingest
```

The first run downloads the static schedule (~20 MB zip, converted in a few
seconds). Output goes to `./data` (gitignored), and the quality report is
printed, for example:

```
| Source | Status | Age (s) | Records | Valid | New | Stale | Coverage | Gap (min) |
|---|---|---|---|---|---|---|---|---|
| alerts | ok | 297 | 191 | 16 | 191 | 0 | - | - |
| traffic | stale | 4702 | 256 | 177 | 256 | 125 | 0% | - |
| bike_status | ok | 77 | 2518 | 2375 | 2518 | 32 | 94% | - |
```

Events need some history: with the default rule a line must be late for 5
consecutive minutes, so run `ripple ingest --polls 6 --interval 60` (or several
runs) before `ripple events`. Comparisons with "usual" conditions need baselines
from earlier weeks; until then summaries say that no baseline exists yet.

### Commands

| Command | Purpose |
|---|---|
| `ripple ingest [--sources subway,alerts,traffic,bikes] [--polls N --interval S]` | Fetch sources, compute delays, run checks, write Parquet + report |
| `ripple ingest --replay data/raw/*/*.pb.gz` | Re-process saved subway payloads offline (no network) |
| `ripple baselines [--force]` | Weekday x time-of-day baselines (skipped if computed in the last 24 h) |
| `ripple events [--now ISO]` | Detect delay events, link alerts, add surface context and summaries |
| `ripple site [--out DIR]` | Render event charts and `index.html` (default `data/site`) |
| `ripple neighbors` | Recompute the 500 m spatial join |
| `ripple prune --table bike_minute [--keep-days N]` | Retention for large tables |
| `ripple query "SQL"` | Run SQL against the DuckDB views (`--format table/csv/json`) |
| `ripple lines [--start ISO --end ISO --route A,L --max-age 10]` | 1-minute line delays, forward-filled with freshness |
| `ripple compact [--before YYYY-MM-DD]` | Merge per-run Parquet files of past days into one file per day |
| `ripple static [--force]` | Download/convert the static GTFS only |

All commands accept `--data-dir` (default `./data` or `RIPPLE_DATA_DIR`).

### Example queries

Recent delay events and their summaries:

```bash
uv run ripple query "
  SELECT event_id, status, round(peak_delay_min, 1) AS peak, duration_min, summary
  FROM events ORDER BY start_utc DESC LIMIT 5"
```

Citi Bikes near a station right now:

```bash
uv run ripple query "
  SELECT n.station_name, n.neighbor_name, round(n.distance_m) AS m, b.bikes_available
  FROM station_neighbors n JOIN bike_minute b ON b.station_id = n.neighbor_id
  WHERE n.station_name = 'Jay St-MetroTech' AND n.neighbor_type = 'bike_station'
    AND b.bucket_utc = (SELECT max(bucket_utc) FROM bike_minute)
  ORDER BY m"
```

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
  station_minute/...                             observed delays per station x minute
  quality_source/...                             quality counters per other source
  alerts/..., alert_presence/...                 service alert versions and presence
  traffic_minute/...                             DOT traffic speed per link observation
  bike_minute/...                                Citi Bike bikes/docks per station x minute
  events/...                                     delay event versions with summaries
  reference/                                     stations, links, bike stations, neighbors,
                                                 baselines (snapshot Parquet files)
  site/                                          index.html + charts/<event_id>.png
  reports/date=YYYY-MM-DD/<run_id>.json          full quality report per run
  state.json                                     dedup / freshness state between runs
  raw/                                           raw payloads (only with --save-raw)
  static/                                        static GTFS cache (Parquet)
```

Large raw data is never committed: `data/`, `*.parquet`, `*.pb`, `*.zip` are gitignored.

## Configuration and secrets

Settings come from environment variables (prefix `RIPPLE_`), optionally loaded
from a local `.env` file (gitignored; existing environment variables win). See
[`.env.example`](.env.example) for every option, including all quality thresholds.

No paid keys are needed. Secrets are only ever read from environment variables
(GitHub Secrets in CI, `.env` locally), are never logged, and are sent only to
their own host:

- `SOCRATA_APP_TOKEN` (optional, free NYC Open Data app token for higher rate
  limits): sent as `X-App-Token` to `data.cityofnewyork.us` only.
- `MTA_API_KEY` (optional, not needed today): sent as `x-api-key` to MTA only.

Key settings (all `RIPPLE_<NAME>`): `EVENT_THRESHOLD_MIN` (5), `EVENT_MIN_MINUTES`
(N = 5), `EVENT_METRIC` (`delay_median_min`), `NEIGHBOR_RADIUS_M` (500),
`BASELINE_DAYS` (28), `BASELINE_MIN_DAYS` (3), `BIKE_RETENTION_DAYS` (35).

## Automation (GitHub Actions)

- [`ingest.yml`](.github/workflows/ingest.yml) runs every 5 minutes (and on
  manual dispatch): 3 subway snapshots 60 seconds apart, then alerts, traffic
  speed and Citi Bike status once, baselines, events, summaries and the site.
  Slower cadences are handled inside the run instead of separate schedules
  (a second scheduled workflow sharing the data-branch concurrency group could be
  starved): GBFS discovery and station information daily, static GTFS every 6
  hours, baselines daily, compaction and retention once a day has rolled over.
  Each run then:
  - publishes aggregates, events, reference tables, `site/` and `state.json` to the
    **`data` branch**, replaced by a single snapshot commit each run (force-push
    with lease). Publishing happens whenever ingestion succeeded, even if an
    analysis step failed (the job still fails visibly);
  - uploads per-stop delays and the JSON report as a workflow artifact kept for
    7 days ("raw data short-term, aggregates long-term");
  - adds the Markdown quality report to the job summary.
- [`ci.yml`](.github/workflows/ci.yml) runs lint and tests on Python 3.11–3.13.

Workflow security: the default token is read-only (`contents: read`); only the
ingestion job gets `contents: write`, to push the data branch. Every action is
pinned to a commit SHA. `SOCRATA_APP_TOKEN` is only used by the scheduled /
manually dispatched ingestion workflow, which never runs for pull requests, so it
cannot reach forks; CI needs no secrets, checks out without persisted credentials,
and `pull_request_target` is not used.

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

The event page is `site/index.html` on the data branch. To serve it later with
GitHub Pages, deploy that folder (for example with `actions/deploy-pages` from a
workflow that checks out the data branch).

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Tests use synthetic static schedules and GTFS-Realtime payloads (built with the
same protobuf classes, including the NYCT extension), so they run offline.

## Known limitations

### M2

- **Event timing depends on sampling.** With the default rule an event needs 5
  consecutive minutes, 2 of them observed; GitHub cron delays longer than the
  10-minute gap tolerance split or end events (`end_reason = data_gap`). Events
  that were already under way when data collection started appear to start then.
- **Disruption onsets can be filtered.** The M1 delay-jump filter drops a trip
  whose prediction jumps by more than elapsed time + 15 min, which happens when the
  MTA re-times trains at the start of a disruption; such trips count again from
  the next observation.
- **Affected stations** come from observed arrivals only (`time_kind = 'actual'`);
  stations a delayed train has not reached yet are not listed. The surface
  context uses the 3 most affected stations.
- **Traffic speed coverage and freshness.** The DOT network covers mostly
  highways and arterials (123 of 496 stations have a link within 500 m), and the
  feed can lag for hours (3.5 h was observed while building M2); stale data is
  marked missing, never used as current.
- **Citi Bike** availability includes e-bikes; rebalancing trucks and station
  outages also move the numbers. 367 of 496 stations have docks within 500 m.
- **Baselines need weeks of history.** Same weekday/time slots only fill up after
  3 weeks (`low` confidence before), the most recent 24 hours are excluded, and
  past disruptions are not removed from the baseline yet.
- **Alert linking** matches line and time (station overlap only ranks alerts);
  planned-work notices are numerous and listed separately. The alerts feed header
  is a last-change time, so staleness is only flagged after 6 hours.
- **Comparisons are descriptive.** The summaries report differences from the
  baseline during the event; they do not claim causation or measure after-effects
  (M3 adds an impact score).
- **Data branch size.** `bike_minute` is ~2.3 MB/day compacted and kept 35 days
  (~80 MB), plus the current day's per-run files; the branch is fetched in full by
  every run. Moving long-term storage to Releases/object storage remains planned.
- **GitHub Pages** is not configured yet; the site is generated into `site/` on
  the data branch.

### M1

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
- Public repositories: GitHub disables scheduled workflows after 60 days without
  repository activity; re-enable from the Actions tab if that happens.

## Roadmap

M3 cameras + YOLO, surface impact score · M4 static website (GitHub Pages),
richer rule-based explanations · M5 bus data, recommendation engine · M6
evaluation framework, demo. Explanations are always rule-based and generated in
GitHub Actions (no local model). See [docs/design.md](docs/design.md).
See [docs/design.md](docs/design.md).
