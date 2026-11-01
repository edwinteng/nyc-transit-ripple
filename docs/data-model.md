# Data model, quality rules and analysis (M1 + M2)

This document describes what the pipeline stores, which data quality rules it
applies and how delay events, baselines and explanations are computed. All
timestamps are UTC and stored as naive `TIMESTAMP` values whose column names end
in `_utc`. Local New York time is used only for baselines ("weekday and time of
day") and for text shown to people.

Every bucketed table shares the core columns `bucket_utc` (1-minute bucket of
the observation time), `observed_utc` (timestamp from the source) and `run_id`;
most also carry `fetched_utc` and an age in seconds, so freshness is always
recorded next to the value.

## Sources

| Source | Endpoint | Notes |
|---|---|---|
| GTFS-Realtime, 8 subway feeds | `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F<feed>` | No API key. Decoded with `gtfs-realtime-bindings` plus the NYCT extension (train id, assignment flag, tracks). |
| GTFS static, "supplemented" | `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip` | Regular schedule plus planned service changes for about the past 2 and next 7 days. Converted to Parquet and refreshed every 6 hours. |
| MTA subway service alerts (M2) | `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts` | GTFS-Realtime alerts with the MTA "Mercury" extension (alert type, created/updated time). No key. |
| NYC DOT Real-Time Traffic Speed (M2) | `https://data.cityofnewyork.us/resource/i4gi-tjb9.json` (Socrata) | Speed per road link, ~5-15 min per link. No key; optional free app token via `SOCRATA_APP_TOKEN`. |
| Citi Bike GBFS (M2) | `https://gbfs.citibikenyc.com/gbfs/gbfs.json` (auto-discovery) | Discovery -> `gbfs_versions` -> highest supported version (2.3 today) -> `station_information` + `station_status`. No key. |

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


## M2 tables

### `station_minute` (route x station x minute)

Observed train delays (`time_kind = 'actual'` rows of `stop_delays`) aggregated
per station: `n_trains`, `delay_mean_min`, `delay_max_min`. Kept long-term, so the
stations affected by an event can be recomputed even though per-stop detail is
short-lived.

### `alerts` and `alert_presence`

- `alerts`: one row per alert **version** (stored when an alert is new or its
  `updated_utc` changes): `alert_type`, `is_planned` (type starts with
  "Planned"), `active_periods` (list of `start_utc`/`end_utc`, NULL = open),
  `route_ids`, `station_ids` (parent stations), `header_text`, `description_text`.
- `alert_presence`: which alerts were in the feed at each fetch, bucketed by fetch
  time. Open-ended alerts are considered over once they disappear from the feed.
- The feed header timestamp is the time of the latest alert change, not a
  heartbeat, so an unchanged feed is normal; only a header older than 6 hours
  is reported as stale.

### `traffic_minute` (link x observation)

`link_id`, `speed_mph`, `travel_time_s`, `dot_status`, `is_valid` (status 0 and
speed > 0; status -101 with speed 0 is how the feed says "no data"), `age_s`
(fetch time - observation time). `data_as_of` is New York local time without an
offset; it is converted to UTC, and in the repeated hour at the end of daylight
saving time the reading that is not in the future wins. Each run asks only for
observations newer than the last one seen (at most 90 minutes back); repeated
observations are dropped.

### `bike_minute` (Citi Bike station x minute)

`bikes_available` (classic + e-bikes), `ebikes_available`, `docks_available`,
disabled counts, `is_installed`, `is_renting`, `is_returning`,
`last_reported_utc` and `report_age_s`. Bucketed by the feed's `last_updated`.
Placeholder `last_reported` values (the feed uses e.g. 86400) become NULL.
About 2,500 stations x 288 runs/day, so this table is kept for 35 days
(`RIPPLE_BIKE_RETENTION_DAYS`); compacted files are sorted by station, which makes
them about a third smaller.

### `events` (one row per event version)

See "Delay events" below. The `events` view keeps the newest version per
`event_id`; a new version is written only when something about the event changed.

### `quality_source` (source x fetch)

Status (`ok`, `duplicate`, `stale`, `outage`, `invalid`, `empty`), feed age,
record counts (`n_records`, `n_valid`, `n_new`, `n_stale`, `n_invalid`),
`coverage` (share of known links/stations with fresh data) and `gap_min` (set when
the previous successful fetch is older than 15 minutes). Sources:
`alerts`, `traffic`, `bike_status`, `bike_info`, `bike_discovery`.

### Reference tables (`reference/*.parquet`)

Snapshots rewritten as a whole: `subway_stations` (GTFS parent stations),
`traffic_links` (geometry decoded from the published polyline), `bike_stations`,
`station_neighbors` and `baselines`.

## Spatial join

For every subway station (GTFS parent station), `station_neighbors` lists the
NYC DOT road links and Citi Bike stations within 500 m (`RIPPLE_NEIGHBOR_RADIUS_M`):
point-to-polyline distance for links, point-to-point distance for bike stations,
both in a local metric projection. Link geometry comes from `encoded_poly_line`;
the published strings sometimes contain doubled backslashes that are not part of
the encoding, so the decoder tries the raw and the repaired string and keeps the
one that matches the (truncated) `link_points` prefix.

On a live sample: 367 of 496 stations have Citi Bike docks within 500 m and 123
have a DOT traffic link (the DOT network covers mostly highways and arterials).

## Baselines

`baselines` holds, per metric (`traffic_speed_mph`, `bikes_available`,
`docks_available`), entity, local weekday (Mon = 0) and 30-minute local time slot:
`n_days`, `n_obs`, `mean`, `median`, `std` and `confidence`.

- Window: the last 28 days, excluding the most recent 24 hours, so a baseline
  describes earlier weeks and never contains the disruption it is compared with.
- Only valid observations count (valid traffic readings; installed, renting
  stations with a report younger than 30 minutes).
- `confidence = low` when fewer than 3 distinct days contributed (that is, fewer
  than three earlier same weekdays); `none` when no baseline exists.
- Recomputed at most once a day (`reference/baselines_meta.json` records when).

## Delay events

Per line (route), every minute takes the **worst direction** of the line-minute
metric (default: median delay of measured trains), forward-filled for at most 5
minutes. A minute with fewer than 2 measured trains, or without fresh data, is
*unknown*.

| Rule | Default |
|---|---|
| Open: metric >= threshold for N consecutive minutes | 5.0 min, N = 5 |
| ... of which at least this many are observed (not forward-filled) | 2 |
| Close (`recovered`): first known minute below the threshold | |
| Unknown minutes do not count and do not close an event, unless they last longer than | 10 min (`data_gap`) |
| Still above at the latest data | `status = ongoing` |

Each run re-detects events over the last 24 hours; an event that overlaps a stored
event of the same line keeps the stored `event_id` (`<route>-<start>`), so ids are
stable across runs.

Stored per event: `start_utc`, `end_utc` (exclusive), `duration_min`, `status`,
`end_reason`, `directions`, `peak_delay_min`/`peak_utc`/`peak_direction`,
`mean_delay_min`, `max_trip_delay_min`, minute counts (above/observed/filled),
**affected stations** (stations where trains of the line were observed at least
the threshold late, from `station_minute`, worst first, with their maximum delay),
**linked alerts** and the **surface context**.

### Alert linking

An alert is linked when it names the event's line and its active periods,
bounded by when it was actually present in the feed, overlap the event. Unplanned
alerts rank before planned work, then alerts naming more of the affected stations.
Up to 5 are kept.

### Surface context

For the 3 most affected stations, the event compares nearby conditions with their
baselines, minute by minute over the event:

- traffic: average speed of the DOT links within 500 m vs. the average baseline of
  the same links;
- Citi Bike: total bikes available at stations within 500 m vs. the summed
  baselines of the same stations.

Only entities that have both a fresh value and a baseline in a minute are compared,
so "observed" and "usual" always describe the same links/stations. Without
baselines the observed values are still reported, with no comparison.

## Explanations

`summary` is generated by rule-based templates (no language model, design
section 9). Every number in it is taken from the event record and is also stored,
exactly as printed, in `cited_metrics` (JSON); a test checks that a summary never
contains a number that is not cited (quoted MTA alert text excepted). Missing,
stale or low-confidence inputs are stated explicitly.

## Static site

`ripple site` writes `site/index.html` (recent events with summaries, linked
alerts, cited numbers and the latest data status) and `site/charts/<event_id>.png`
(line delay, nearby traffic speed and nearby Citi Bikes over time, with
baselines and the event shaded). Charts are re-rendered only when an event changed.
