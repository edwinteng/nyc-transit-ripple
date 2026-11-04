# Data model, quality rules and analysis (M1-M6)

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

`baselines` holds, per metric (`traffic_speed_mph`, `bus_speed_mph`,
`bikes_available`, `docks_available`, `camera_vehicles`, `camera_persons`), entity
and 30-minute local time slot, two kinds of rows:

- **weekday** rows (`weekday` = 0 for Monday ... 6 for Sunday): the same weekday
  and half-hour, e.g. "Mondays 08:00-08:30";
- **pooled** rows (`weekday` = -1 for Monday-Friday, -2 for Saturday-Sunday): the
  same half-hour over all weekdays, or both weekend days.

Each row has `n_days`, `n_obs`, `mean`, `median`, `std` and `confidence`.

- Window: the last 28 days, excluding the most recent 3 hours, so a baseline never
  contains the disruption it is compared with.
- Only valid observations count (valid traffic readings; installed, renting
  stations with a report younger than 30 minutes; bus segments with samples).
- `confidence = low` when fewer than 3 distinct days contributed; `none` when no
  baseline exists.
- **Lookup:** the weekday row is used when it is `ok`; otherwise the pooled row if
  that is `ok`; otherwise whichever exists, with `low` confidence (which the impact
  score does not use). `baseline_level` records `weekday` or `pooled`. A weekday
  slot needs 3 weeks of history, a pooled weekday slot only 3 days, so comparisons
  become available after a few days of continuous collection, and switch to the
  more specific weekday baseline once it has enough history.
- Recomputed at most every 6 hours (`reference/baselines_meta.json` records when).

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

## Cameras during events (M4)

`ripple events` passes a camera hook to event processing. For each **ongoing**
event that has affected stations, most severe (highest peak delay) first, it calls
`cameras.count_near_stations(focal_station_ids, max_cameras=<cameras left>)`:

- cameras within 500 m of the event's 3 most affected stations are taken from the
  M3 camera-station map, round-robin over the stations (nearest camera first), so
  a cap still covers every station; a camera near several stations is counted once;
- every count (cars, buses, trucks, people; numbers only) is written to
  `camera_counts` like any M3 count, and cameras over the cap are reported as
  `skipped_cap` without a request;
- one detector and one HTTP client are reused for the whole run, and the cap
  (`RIPPLE_EVENT_CAMERA_MAX_PER_RUN`, default 30) is shared by all events of the
  run. Ended events never trigger requests, and nothing is fetched when no event is
  ongoing;
- failures (no mapping yet, the `vision` extra missing, a model download error)
  are logged in the run's events report and never stop event processing.

The event row then stores `camera_ids` (cameras within the radius),
`camera_counts` (latest valid count per camera between the event start and 2
minutes after its end), and the averages used for the score: `camera_vehicles`
(car + bus + truck), `camera_persons`, `camera_objects` (their sum),
`camera_objects_baseline`, `camera_change_pct` and `camera_baseline_confidence`.
Per-run hook results (cameras counted, statuses, errors) are written to
`reports/events/date=YYYY-MM-DD/<run_id>.json`.

Camera baselines (`camera_vehicles`, `camera_persons`) are computed like the
other baselines from `camera_counts` with status `ok`. Because cameras are only
counted during events, they fill slowly and describe event-time conditions; the
minimum-confidence rule below keeps them out of the score until a slot has enough
history.

## Surface impact score

For each event the surface series (minute by minute, over the event and up to
`impact_recovery_window_min` = 120 minutes after it) is compared with the
baselines for the same weekday and 30-minute slot. Over the event window the
average observed value and average baseline give one change per signal:

| Signal | Observed vs. baseline | Sub-score = 100 at | Setting |
|---|---|---|---|
| `traffic` | average speed of DOT links within 500 m | 40% slower | `IMPACT_TRAFFIC_FULL_DROP_PCT` |
| `bus` (M5) | average speed of buses on segments ending within 500 m | 40% slower | `IMPACT_BUS_FULL_DROP_PCT` |
| `camera` | vehicles + people counted per camera within 500 m | 100% more | `IMPACT_CAMERA_FULL_INCREASE_PCT` |
| `bikes` | Citi Bikes available within 500 m | 30% fewer | `IMPACT_BIKES_FULL_DROP_PCT` |

```
change_pct = (observed - baseline) / baseline * 100
sub_score  = clip(100 * change / full_change, 0, 100)   # change in the "worse" direction
score      = sum(w_i * sub_score_i) / sum(w_i)          # over the usable signals only
```

Weights `IMPACT_WEIGHT_BUS` = 0.5, `IMPACT_WEIGHT_CAMERA` = 0.25,
`IMPACT_WEIGHT_BIKES` = 0.25 and `IMPACT_WEIGHT_TRAFFIC` = 0 (all `RIPPLE_`
settings). **Traffic speed is off by default** (`TRAFFIC_ENABLED=false`): the NYC DOT
feed covers only about 125 highway and arterial links, is often hours behind and
stopped updating entirely on 2026-11-02, while bus speeds measure local streets near
far more stations (over 1,000 segments). Setting `TRAFFIC_ENABLED=true` and a
traffic weight turns it back on. (M4 used traffic 0.4 / cameras 0.3 / bikes 0.3
before buses existed.) A change in the "better"
direction (faster traffic or buses, fewer people, more bikes) gives 0, not a negative
score. A signal is **usable** only when it has an observation, a positive
baseline, a baseline confidence of at least `IMPACT_MIN_CONFIDENCE` (`ok` = at
least 3 days of history) and a weight above 0; otherwise it is reported with
status `no_data`, `no_baseline`, `low_confidence` or `zero_weight` and the
weights are **renormalized** over the remaining signals. With no usable signal
the score is NULL ("not enough data"), never 0.

Stored per event: `impact_score`, `impact_signals_used` (list) and
`impact_detail` (JSON: per signal the status, observed value, baseline,
confidence, change, sub-score, configured and effective weight, plus the rule
settings). Example: traffic 25% slower (sub-score 62.5), Citi Bikes 15% fewer
(50) and no bus or camera data gives (0.3 x 62.5 + 0.2 x 50) / 0.5 = 57.5.

The dashboard labels scores as low (< 20), moderate (20-49), high (50-74) and
severe (75+), always with the number.

### Surface recovery

The same formula, applied minute by minute with the signals used for the event
(renormalized per minute), gives `impact_series`. `impact_peak_score` /
`impact_peak_utc` are its maximum during the event. Then:

- peak below `IMPACT_RECOVERY_THRESHOLD` (20): `surface_recovery_status = no_impact`;
- otherwise `surface_recovered_utc` is the first minute after the peak that starts
  `IMPACT_RECOVERY_MINUTES` (10) consecutive minutes with a known score below 20
  (`recovered`); if that has not happened yet, `not_yet`, with
  `surface_checked_until_utc` = the last minute with a known score;
- no score: `unknown`.

Missing minutes break a recovery run, so data gaps delay recovery rather than
fake it.

## Buses (M5)

**Source.** MTA Bus Time SIRI `VehicleMonitoring` (all buses in service, one
request), with the free key from the environment variable `MTA_BUS_API_KEY`
(sent as the `key` query parameter to `bustime.mta.info` only; reports record the
URL without parameters). The pipeline takes one snapshot per subway poll cycle,
never more often than every 30 seconds (`RIPPLE_BUS_MIN_REQUEST_INTERVAL_S`), as
Bus Time asks. Without a key, or when Bus Time answers 401/403 or "API key is not
authorized", no further request is made in that run and the `bus` source is
recorded with status **`unavailable`** (a missing status: never "normal"); the
dashboard shows "MTA buses: Unavailable".

**Speeds.** For each bus, distance along its route = `CallDistanceAlongRoute -
DistanceFromCall` (metres, from the next stop). Two snapshots of the same vehicle
on the same trip, 20-300 s apart, give a speed sample; samples that move backwards,
exceed 60 mph, or come from buses at a terminal (`layover`, `prevTrip`) are dropped.
A sample belongs to the **segment** that ends at the bus's next stop
(`segment_id = route|direction|stop`).

### `bus_minute` (segment x minute)

`bucket_utc`, `observed_utc`, `fetched_utc`, `run_id`, `segment_id`, `route_id`,
`direction_id`, `stop_id`, `n_samples`, `n_vehicles`, `distance_m`, `duration_s`
and `speed_mph` = total distance / total time of the samples (a bus that crawled for
longer weighs more). Only segments ending at a stop within 500 m of a subway station
are stored; retention `RIPPLE_BUS_RETENTION_DAYS` (35).

### `reference/bus_stops.parquet`

`stop_id`, `stop_name`, `lat`, `lon`, `routes` from the MTA bus static GTFS (one zip
per borough plus MTA Bus Company, public, no key), refreshed weekly. The spatial join
adds `neighbor_type = 'bus_stop'` rows to `station_neighbors`; a segment is "near" a
station when its end stop is.

**Baseline and impact.** `bus_speed_mph` baselines are computed per segment like the
other metrics. The surface series averages the segments near the focal stations, and
`bus` is a fourth impact signal (speed drop vs. usual; 100 at a 40% drop,
`IMPACT_BUS_FULL_DROP_PCT`; weight `IMPACT_WEIGHT_BUS`). Default weights are bus 0.5,
camera 0.25, bikes 0.25 (traffic speed off), always renormalized over the usable
signals. Events store `bus_n_segments`, `bus_speed_mph`, `bus_baseline_mph`,
`bus_change_pct` and `bus_baseline_confidence`.

## Recovery model (M5)

Past events that ended because service recovered (`end_reason = 'recovered'`, last
`RECOVERY_HISTORY_DAYS` = 180 days) give duration distributions grouped by:

- line;
- time-of-day band of the start in New York time: `am_peak` 06-10, `midday` 10-16,
  `pm_peak` 16-20, `evening` 20-24, `night` 00-06 on weekdays, `weekend`;
- cause category of the first linked unplanned alert, from keywords in its text
  (`person_on_tracks`, `signal`, `track_or_switch`, `power`, `mechanical`,
  `medical`, `police`, `weather`, `crowding`, `other`).

The most specific group with at least `RECOVERY_MIN_EVENTS` (5) events is used:
line + band + cause, then line + band, line, band, all events. Any fallback, or a
final group that is still small, makes the estimate `low` confidence; no past
events gives no estimate. For an event that has lasted `elapsed` minutes, the
remaining delay is the median (and 75th percentile) of `duration - elapsed` over
the similar events that lasted longer (at least 3), otherwise what is left of the
overall quantiles, marked low. **Surface clearing time** (`surface_recovered_utc -
end_utc`) uses the same grouping.

## Recommendations (M5)

`ripple recommend` runs after `ripple events`. For every ongoing event (computed in
the last 15 minutes) and each of its focal stations it builds options and ranks them
by the estimated time to get `REC_REFERENCE_TRIP_KM` (4 km) away from the station,
because no destination is known: **total = access (walk) + wait + ride**, where
ride = reference distance / the mode's speed.

| Option | Access | Wait | Ride speed | Confidence |
|---|---|---|---|---|
| `walk_line` | `distance x 1.3 / 1.3 m/s` (2 min to change platforms at the same station) | next predicted train after arriving at the platform (GTFS-Realtime, last 10 min of predictions) | 17 mph | high |
| `bike` | walk to the nearest dock with at least 2 bikes + 1 min to unlock | 0 | 8 mph | high |
| `bus` | walk to the route's nearest stop | 6 min (assumed; no headway data yet) | current speed on nearby segments, else usual, else 7.5 mph | medium with live speed, else low |
| `wait` | 0 | remaining delay (recovery model median) | 17 mph | medium / low / none |

Stations within 1.2 km count for `walk_line` only through lines that are not in any
ongoing event and that have at least one predicted train in the next hour (a line
with no prediction may not be running, e.g. at night). Premium express buses are
excluded (`REC_EXCLUDE_BUS_ROUTE_PREFIXES`). At most 2 stations and 2 bus routes are
kept per affected station. Unknown totals (no recovery history) rank last.

**Avoid the crowd.** The *obvious* option is the non-wait option with the lowest
total under normal conditions (usual train wait 5 min, usual bus speed, bikes as
now). If it shows any crowding signal it gets `REC_CROWD_PENALTY_MIN` (5) extra
minutes and is flagged `crowded`, with `crowd_reasons`:

- `bus_slowdown`: buses on that route at least 25% slower than usual;
- `bike_depletion`: bikes at the docks within 500 m down at least 30% in 15 minutes;
- `camera_people`: cameras near the target station (or near the affected station
  for bike and bus) counting at least 50% more people than usual.

**Text.** Each option has one template sentence, e.g. "Walk 4 min to Borough Hall for
the 2/4 (next trains in 5 min northbound, 8 min southbound). About 18 min to get 4 km
away." Every number is stored in `metrics` exactly as printed (same check as event
explanations).

### `recommendations` (one row per option per station per run)

`bucket_utc`, `computed_utc`, `run_id`, `event_id`, `event_route_id`, `station_id`,
`station_name`, `rank`, `option_type`, `option_id`, `target_id`, `target_name`,
`routes`, `est_total_min`, `est_access_min`, `est_wait_min`, `est_ride_min`,
`penalty_min`, `normal_total_min`, `is_obvious`, `crowded`, `crowd_reasons`,
`confidence`, `text`, `metrics` (JSON) and `inputs` (JSON: the facts behind the
estimate, data timestamps and the full `rec_*` rule), so M6 can replay events and
compare estimates with what happened.

## Explanations

Every event gets one or two sentences from fixed templates (no language model,
design section 9), stored in `explanation` as JSON
`[{"text": ..., "metrics": {name: value as printed}}]`; `summary` joins the
sentences and `cited_metrics` merges their metrics.

1. **Subway:** line, how late (peak median delay, or "at least the threshold"),
   duration, start/end in New York time, worst station and its delay; ongoing and
   data-gap variants.
2. **Surface:** the change of each *used* signal vs. usual for the weekday and
   time (traffic slower/faster, Citi Bike availability lower/higher, camera counts
   more/fewer), the impact score and the recovery time ("had recovered by",
   "had not recovered by", "stayed close to normal"). Without usable baselines it
   gives the observed values and says that there is not enough baseline history
   yet; without any fresh data it says so.

Numbers are rounded half up (as in the browser). Tests check, per sentence, that
every number in the text is one of its cited metrics and that every cited metric
appears in the text.

## Dashboard export

`ripple export --out site/data` writes compact JSON (UTC ISO 8601 strings ending
in `Z`, NaN written as `null`, numbers rounded) for the static dashboard; the
browser never reads Parquet. Files:

| File | Content |
|---|---|
| `status.json` | `generated_utc`, `last_pipeline_run_utc`, and per source (`subway`, `alerts`, `traffic`, `bike_status`, `bus`, `cameras`, `baselines`): `status` (`ok`, `degraded`, `stale`, `missing`, `unavailable`, `on_demand`, `building`), `data_utc`, `checked_utc`, `data_age_min`, `detail` |
| `stations.json` | every subway station (`id`, `name`, `lat`, `lon`, `routes`) and, if it is affected by an ongoing event or one that ended in the last 24 h, the most severe such `event` (id, route, status, impact score and label, max delay) |
| `events.json` | events that started in the last `EXPORT_WINDOW_DAYS` (30): times, peak delay, threshold, impact score/label/signals, recovery, affected stations with coordinates, linked alerts, `sentences`, `summary`, `top_recommendation` (best option of the latest run), and the path of the detail file; plus the event and impact rules |
| `events/<event_id>.json` | per-minute series from 30 minutes before the event to its recovery window (`t`, `delay_min`, `traffic_mph`, `traffic_base_mph`, `camera_vehicles(_base)`, `camera_persons(_base)`, `bikes(_base)`, `impact`), averaged into at most `EXPORT_MAX_POINTS` (120) points; and `nearby` stations, cameras (with the event's latest counts), Citi Bike stations and road links (simplified to at most about 40 points); `recommendations`: the latest ranked options per station |

Detail files of events that left the window are deleted. A typical export with a
few events is around 100-150 KB in total. The Pages workflow runs the export from
the data branch at deploy time, so the JSON is never committed.

### Replay and notable events (M6)

Each `events/<event_id>.json` also has:

- `station_series`: for up to 8 affected stations, the worst delay observed at the
  station (any direction of the event's line) on the same time axis as `series`;
  observations are held for at most 10 minutes;
- `recommendation_history`: every recommendation set computed during the event
  (`computed_utc` and the top 3 options per station; at most 48 sets, evenly spaced,
  always including the last).

`events.json` lists `notable` events: ended events of the last
`NOTABLE_HISTORY_DAYS` (180) lasting at least `NOTABLE_MIN_DURATION_MIN` (10) with
affected stations, ranked by impact score (`basis = impact`), then, while few events
have a score, by peak delay x duration (`basis = delay`). Notable events older than
the 30-day window are included with `in_window = false` (they are replayable but not
in the event list).

## Evaluation (M6)

`ripple evaluate` (weekly in the `Evaluate` workflow) writes
`docs/evaluation/latest.md`, `latest.json` (also served as `data/evaluation.json`)
and `<date>.md`. Every section has a status (`ok`, `insufficient_data`, `skipped`),
the date range of the data it used, a summary, and metrics with `value`, `n`, an
optional 95% interval, and `preliminary = n < EVAL_MIN_SAMPLE` (30).

| Section | Method |
|---|---|
| Detection vs. MTA alerts | Ground truth: unplanned alerts, one per line named ("alert-line"), at their creation time (else first seen). An event matches an alert-line of its line created between `EVAL_ALERT_TOLERANCE_MIN` (15) before its start and 15 after its end; one to one, closest first. Precision = matched / events; recall = matched / alert-lines whose line had usable subway data in the 30 minutes after the alert; time to detect = event start - alert time. Unmatched events are listed. |
| Placebo test | Per ended event and signal (traffic speed, bus speed, Citi Bikes): mean near the focal stations during the event (E) vs. the same entities at the same local time on earlier days of the same kind within 14 days, skipping windows with an event at these stations (A); and vs. up to 10 control stations at least 2 km from any station affected by an active event, each relative to its own matched windows. Differences in %: (a) E/A - 1, (b) E/A - mean(C/C_matched). Mean over events with a percentile bootstrap 95% interval; "significant" only if the interval excludes 0. |
| Camera accuracy | `docs/evaluation/camera_labels.csv` (manual counts) and `camera_predictions.csv` (model counts) from the M3 labeling workflow: MAE and bias (model - true) per class and model. Skipped without labels. |
| Recommendation replay | Realized outcomes after each logged option: wait = event end - recommendation time (recovered events only); other line = first train observed (`station_minute`) at the target station after the walk; bus = speed on the route's nearby segments in the next 30 min; bike = bikes at the dock 15 min later. MAE per option type; how often rank 1 beat the obvious option in realized time; whether obvious options flagged as crowded degraded (bus <= usual x (1 - 25%), dock below 2 bikes or nearby bikes down >= 30%, train wait > estimate + 3 min) compared with unflagged ones. |
| Explanation consistency | Every stored event sentence and recommendation text: each number must be one of its cited metrics. Violations fail the workflow (`--check-explanations`); a randomized test over every template variant runs in CI. |
| Reliability | Per source: share of usable fetches (uptime), median and 95th percentile data age, gap count; median and longest time between ingest runs; per workflow (GitHub REST API, read-only token): success rate and average duration. |

## Static site (M2, offline)

`ripple site` writes `site/index.html` and `site/charts/<event_id>.png` for
offline use; the M4 dashboard replaces it for publishing.
