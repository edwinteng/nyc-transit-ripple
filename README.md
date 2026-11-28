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
