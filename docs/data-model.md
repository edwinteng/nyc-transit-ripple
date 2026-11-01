# M1 data model and quality rules

This document describes what the M1 ingestion pipeline stores and which data
quality rules it applies. All timestamps are UTC and stored as naive
`TIMESTAMP` values whose column names end in `_utc`.

## Sources

| Source | Endpoint | Notes |
|---|---|---|
| GTFS-Realtime, 8 subway feeds | `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F<feed>` | No API key. Decoded with `gtfs-realtime-bindings` plus the NYCT extension (train id, assignment flag, tracks). |
| GTFS static, "supplemented" | `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip` | Regular schedule plus planned service changes for about the past 2 and next 7 days. Converted to Parquet and refreshed every 6 hours. |

Feed ids used throughout: `numbered` (1-7, S), `ace` (A, C, E, Rockaway shuttle),
`bdfm` (B, D, F, M, Franklin shuttle), `g`, `jz`, `nqrw`, `l`, `si` (Staten Island Railway).

## Delay definition

For each trip update and each listed stop:

```
delay_min = (realtime time - scheduled time) / 60      # positive = late
```

- The realtime arrival time is used when present, otherwise the departure time
  (`event` column). Both `arrival_delay_min` and `departure_delay_min` are stored.
- Scheduled times come from `stop_times.txt` of the matched static trip. GTFS
  times are seconds after "noon minus 12 h" of the service date
  (`start_date`) in `America/New_York`, so trips after midnight (`24:05:00`) and
  daylight-saving days are handled per the GTFS specification.
- If a trip visits a stop twice, the visit closest in time is used.
- `time_kind`:
  - `actual`: the current stop (first in the trip update) with a time not after
    the feed timestamp, i.e. the train already arrived there;
  - `predicted`: an upcoming stop (MTA prediction);
  - `stale`: a downstream stop whose prediction is already more than 120 s in the
    past (frozen "ghost" or held train data). These rows are dropped.
- `lead_s` = event time - feed timestamp (negative = in the past).

### Matching realtime trips to the schedule

Realtime trip ids look like `118700_1..N03R` (origin time in 1/100 min, route,
direction, path). Static trip ids end with the same suffix. Matching tries, in
order (recorded in `match_method`):

1. `exact`: same suffix, service active on `start_date` (calendar + calendar_dates);
2. `base_trip`: same origin time, route and direction, different or missing path;
3. `exact_weekday_fallback` / `base_trip_weekday_fallback`: as above, but with a
   service that runs on that weekday outside its date range. This covers the days
   around a schedule change ("pick"), when realtime still follows the old schedule.
   Lower confidence; filter on `match_method` if needed.

Unmatched trips (`unmatched`) are kept with NULL delays and counted in the report.
When two active services contain the same trip, the planned-work supplement wins.

## Tables

### `stop_delays` (one row per trip x stop x 1-minute bucket)

Short-term detail. In GitHub Actions it is uploaded as a 7-day artifact, not committed.

| Column | Meaning |
|---|---|
| `bucket_utc` | Feed timestamp floored to the minute |
| `observed_utc` | Feed header timestamp |
| `feed_id`, `route_id`, `direction` (`N`/`S`) | Line |
| `trip_id`, `start_date`, `train_id` | Realtime trip identity |
| `is_assigned` | Whether a physical train is assigned (NULL if unknown) |
| `static_trip_id`, `match_method` | Schedule match |
| `stop_index` | Position in the trip update (0 = current/next stop) |
| `stop_id`, `station_id`, `stop_name` | Platform, parent station, name |
| `arrival_utc`, `departure_utc` | Realtime times |
| `scheduled_arrival_utc`, `scheduled_departure_utc` | Scheduled times |
| `event`, `event_utc`, `scheduled_utc` | Time used for `delay_min` |
| `delay_min`, `arrival_delay_min`, `departure_delay_min` | Delays in minutes |
| `lead_s`, `time_kind` | See above |
| `scheduled_track`, `actual_track` | NYCT extension |
| `vehicle_status`, `vehicle_age_s` | Joined vehicle position; age relative to the feed timestamp |

### `line_minute` (one row per route x direction x 1-minute bucket)

Long-term aggregate, committed to the `data` branch.

| Column | Meaning |
|---|---|
| `bucket_utc` | Minute (feed time for valid rows, fetch time for missing markers) |
| `status` | `ok`, or why the feed is missing: `stale`, `outage`, `invalid`, `empty`, `time_jump_future`, `time_jump_backward` |
| `n_trips`, `n_trips_unassigned`, `n_trips_unmatched`, `n_trips_measured` | Trip counts |
| `delay_mean_min`, `delay_median_min`, `delay_p90_min`, `delay_max_min` | Over measured trips (0.01 min resolution) |
| `share_trips_delayed` | Share of measured trips at least 5 minutes late |
| `match_rate` | Share of trips matched to the schedule |
| `feed_age_s` | Fetch time minus feed timestamp |

The per-trip delay used here is the delay at the trip's first stop update that
has a delay (its current or next station). Trips without an assigned train are
excluded because their times are just the schedule. Missing markers have NULL
metrics: a missing feed is never reported as normal service.

### `quality_feed` (one row per feed x poll)

Status and every quality counter from the report (see below), so data quality
can be queried over time. Committed to the `data` branch.

### Reports

`reports/date=YYYY-MM-DD/<run_id>.json` holds the full report of a run: thresholds,
static GTFS version and age, per-feed counters, delay jumps, rows written and a
list of issues. A Markdown summary is printed and, in Actions, added to the job summary.

## Quality checks

Applied in this order for each feed snapshot:

| Check | Rule (default) | Outcome |
|---|---|---|
| Outage | HTTP error or network failure | `outage`, lines marked missing |
| Invalid payload | Not decodable, or no header timestamp | `invalid`, lines marked missing |
| Future timestamp | Header more than 60 s ahead of fetch time | `time_jump_future`, lines marked missing |
| Backward time jump | Header older than the last accepted header for the feed | `time_jump_backward`, lines marked missing |
| Stale feed | Fetch time - header > 180 s | `stale`, lines marked missing |
| Duplicate snapshot | Header equal to the last accepted header (and not stale) | `duplicate`, skipped (not missing) |
| Empty feed | No trip updates | `empty`, lines marked missing |
| Duplicate trip entities | Same `trip_id` + `start_date` twice | Keep newest vehicle report, then most stops |
| Duplicate stop updates | Identical stop/time repeated in a trip | Drop repeats |
| Time window | Stop time missing, or > 2 h before / > 4 h after the header | Drop row |
| Non-monotonic times | Departure before arrival (> 60 s), or times going backwards along the trip | Drop the fewest stops that make the trip monotonic (longest non-decreasing subsequence; jitter up to 60 s kept) |
| Stale predictions | Downstream stop predicted > 120 s in the past | Drop row |
| Implausible delay | Delay < -30 min or > 240 min (bad schedule match) | Drop row |
| Delay jump | Trip delay changed by more than elapsed minutes + 15 min since the previous observation (state kept across runs) | Drop the trip's rows for this snapshot; the new level is remembered |
| Bucket dedup | Same trip x stop (or line) in the same minute | Keep the newest observation; for lines a valid row beats a missing marker |

Counted but not filtered: unassigned trips, unmatched trips, trips whose vehicle
position is older than 10 minutes (`n_stale_vehicles`), alert entities.

All thresholds can be changed with environment variables (see `.env.example`)
and are recorded in every report.

## Freshness and forward fill

`ripple lines` (or `storage.line_minute_filled`) returns a continuous 1-minute grid
per line. Each minute carries the newest observation at or before it, with
`age_min` and `fill_status`:

- `observed`: data from this minute;
- `filled`: carried forward, at most `--max-age` minutes (default 10);
- `missing`: no valid data within the max age, or the latest information is a
  missing marker (stale/outage). Metrics are NULL.

## DuckDB views

`storage.connect(data_dir)` (used by `ripple query`) creates:

- `stop_delays`, `line_minute`, `quality_feed`: deduplicated per bucket key;
- `stop_delays_all`, `line_minute_all`, `quality_feed_all`: every stored row.

The Hive partition column `date` is available for partition pruning.
