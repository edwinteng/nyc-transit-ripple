"""Parquet storage in 1-minute UTC buckets, plus DuckDB views over it.

Layout (all under the data directory)::

    stop_delays/date=YYYY-MM-DD/<run_id>.parquet     per trip x stop observations
    line_minute/date=YYYY-MM-DD/<run_id>.parquet     per route x direction x minute
    station_minute/date=YYYY-MM-DD/<run_id>.parquet  observed train delays per station x minute
    quality_feed/date=YYYY-MM-DD/<run_id>.parquet    per subway feed x poll quality counters
    quality_source/date=YYYY-MM-DD/<run_id>.parquet  per other source x run quality counters
    alerts/...                                       service alert versions
    alert_presence/...                               alerts present in the feed per minute
    traffic_minute/...                               traffic speed per link observation
    bike_minute/...                                  bikes/docks per Citi Bike station x minute
    events/...                                       delay event versions
    reference/<name>.parquet                         snapshot tables (stations, links,
                                                     neighbors, baselines)
    reports/date=YYYY-MM-DD/<run_id>.json            full quality report per run

All bucketed tables share the core columns ``bucket_utc`` (1-minute UTC bucket of
the observation time), ``observed_utc`` (source timestamp) and ``run_id``; most
also carry ``fetched_utc`` and an age in seconds for freshness. All timestamps
are naive UTC (column names end in ``_utc``).
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

TS = pa.timestamp("us")

SCHEMAS: dict[str, pa.Schema] = {
    "stop_delays": pa.schema(
        [
            ("bucket_utc", TS),
            ("observed_utc", TS),
            ("run_id", pa.string()),
            ("feed_id", pa.string()),
            ("route_id", pa.string()),
            ("direction", pa.string()),
            ("trip_id", pa.string()),
            ("start_date", pa.string()),
            ("train_id", pa.string()),
            ("is_assigned", pa.bool_()),
            ("trip_relationship", pa.string()),
            ("static_trip_id", pa.string()),
            ("match_method", pa.string()),
            ("stop_index", pa.int32()),
            ("stop_id", pa.string()),
            ("station_id", pa.string()),
            ("stop_name", pa.string()),
            ("stop_relationship", pa.string()),
            ("arrival_utc", TS),
            ("departure_utc", TS),
            ("scheduled_arrival_utc", TS),
            ("scheduled_departure_utc", TS),
            ("event", pa.string()),
            ("event_utc", TS),
            ("scheduled_utc", TS),
            ("delay_min", pa.float64()),
            ("arrival_delay_min", pa.float64()),
            ("departure_delay_min", pa.float64()),
            ("lead_s", pa.int64()),
            ("time_kind", pa.string()),
            ("scheduled_track", pa.string()),
            ("actual_track", pa.string()),
            ("vehicle_status", pa.string()),
            ("vehicle_age_s", pa.int64()),
        ]
    ),
    "line_minute": pa.schema(
        [
            ("bucket_utc", TS),
            ("observed_utc", TS),
            ("fetched_utc", TS),
            ("run_id", pa.string()),
            ("feed_id", pa.string()),
            ("route_id", pa.string()),
            ("direction", pa.string()),
            ("status", pa.string()),
            ("feed_age_s", pa.float64()),
            ("n_trips", pa.int32()),
            ("n_trips_unassigned", pa.int32()),
            ("n_trips_unmatched", pa.int32()),
            ("n_trips_measured", pa.int32()),
            ("n_stop_updates", pa.int32()),
            ("delay_mean_min", pa.float64()),
            ("delay_median_min", pa.float64()),
            ("delay_p90_min", pa.float64()),
            ("delay_max_min", pa.float64()),
            ("share_trips_delayed", pa.float64()),
            ("match_rate", pa.float64()),
        ]
    ),
    "quality_feed": pa.schema(
        [
            ("bucket_utc", TS),
            ("fetched_utc", TS),
            ("header_utc", TS),
            ("run_id", pa.string()),
            ("poll_index", pa.int32()),
            ("feed_id", pa.string()),
            ("status", pa.string()),
            ("detail", pa.string()),
            ("http_status", pa.int32()),
            ("fetch_error", pa.string()),
            ("fetch_elapsed_s", pa.float64()),
            ("n_bytes", pa.int64()),
            ("feed_age_s", pa.float64()),
            ("n_entities", pa.int32()),
            ("n_trip_updates", pa.int32()),
            ("n_vehicles", pa.int32()),
            ("n_alerts", pa.int32()),
            ("n_trips", pa.int32()),
            ("n_trips_unassigned", pa.int32()),
            ("n_trips_matched", pa.int32()),
            ("n_trips_unmatched", pa.int32()),
            ("n_stale_vehicles", pa.int32()),
            ("n_stop_updates", pa.int32()),
            ("n_with_schedule", pa.int32()),
            ("dup_trip_entities", pa.int32()),
            ("dup_stop_updates", pa.int32()),
            ("dropped_no_time", pa.int32()),
            ("dropped_out_of_window", pa.int32()),
            ("dropped_non_monotonic", pa.int32()),
            ("dropped_stale_prediction", pa.int32()),
            ("dropped_implausible_delay", pa.int32()),
            ("dropped_delay_jump_trips", pa.int32()),
            ("n_rows", pa.int32()),
            ("match_rate", pa.float64()),
            ("match_methods", pa.string()),
            ("routes_seen", pa.string()),
        ]
    ),
    # Observed (time_kind = actual) train delays aggregated per station and minute.
    "station_minute": pa.schema(
        [
            ("bucket_utc", TS),
            ("observed_utc", TS),
            ("run_id", pa.string()),
            ("feed_id", pa.string()),
            ("route_id", pa.string()),
            ("direction", pa.string()),
            ("station_id", pa.string()),
            ("stop_name", pa.string()),
            ("n_trains", pa.int32()),
            ("delay_mean_min", pa.float64()),
            ("delay_max_min", pa.float64()),
        ]
    ),
    "quality_source": pa.schema(
        [
            ("bucket_utc", TS),
            ("fetched_utc", TS),
            ("observed_utc", TS),
            ("run_id", pa.string()),
            ("source", pa.string()),
            ("status", pa.string()),
            ("detail", pa.string()),
            ("http_status", pa.int32()),
            ("fetch_error", pa.string()),
            ("fetch_elapsed_s", pa.float64()),
            ("n_bytes", pa.int64()),
            ("feed_age_s", pa.float64()),
            ("n_records", pa.int32()),
            ("n_valid", pa.int32()),
            ("n_new", pa.int32()),
            ("n_stale", pa.int32()),
            ("n_invalid", pa.int32()),
            ("coverage", pa.float64()),
            ("gap_min", pa.float64()),
            ("notes", pa.string()),
        ]
    ),
    # One row per new or changed alert version (alerts are stored once per update).
    "alerts": pa.schema(
        [
            ("bucket_utc", TS),
            ("observed_utc", TS),
            ("fetched_utc", TS),
            ("run_id", pa.string()),
            ("alert_id", pa.string()),
            ("alert_type", pa.string()),
            ("is_planned", pa.bool_()),
            ("created_utc", TS),
            ("updated_utc", TS),
            ("active_start_utc", TS),
            ("active_end_utc", TS),
            (
                "active_periods",
                pa.list_(pa.struct([("start_utc", TS), ("end_utc", TS)])),
            ),
            ("route_ids", pa.list_(pa.string())),
            ("station_ids", pa.list_(pa.string())),
            ("header_text", pa.string()),
            ("description_text", pa.string()),
        ]
    ),
    # Which alerts were in the feed at each fetch (bounds open-ended alerts).
    "alert_presence": pa.schema(
        [
            ("bucket_utc", TS),
            ("observed_utc", TS),
            ("run_id", pa.string()),
            ("alert_id", pa.string()),
            ("updated_utc", TS),
            ("is_active", pa.bool_()),
        ]
    ),
    "traffic_minute": pa.schema(
        [
            ("bucket_utc", TS),
            ("observed_utc", TS),
            ("fetched_utc", TS),
            ("run_id", pa.string()),
            ("link_id", pa.string()),
            ("speed_mph", pa.float64()),
            ("travel_time_s", pa.float64()),
            ("dot_status", pa.int32()),
            ("is_valid", pa.bool_()),
            ("age_s", pa.float64()),
        ]
    ),
    "bike_minute": pa.schema(
        [
            ("bucket_utc", TS),
            ("observed_utc", TS),
            ("fetched_utc", TS),
            ("run_id", pa.string()),
            ("station_id", pa.string()),
            ("bikes_available", pa.int32()),
            ("ebikes_available", pa.int32()),
            ("docks_available", pa.int32()),
            ("bikes_disabled", pa.int32()),
            ("docks_disabled", pa.int32()),
            ("is_installed", pa.bool_()),
            ("is_renting", pa.bool_()),
            ("is_returning", pa.bool_()),
            ("last_reported_utc", TS),
            ("report_age_s", pa.float64()),
        ]
    ),
    # One row per computation of an event; the "events" view keeps the newest.
    "events": pa.schema(
        [
            ("bucket_utc", TS),
            ("computed_utc", TS),
            ("run_id", pa.string()),
            ("event_id", pa.string()),
            ("route_id", pa.string()),
            ("status", pa.string()),
            ("end_reason", pa.string()),
            ("start_utc", TS),
            ("end_utc", TS),
            ("duration_min", pa.float64()),
            ("directions", pa.list_(pa.string())),
            ("metric", pa.string()),
            ("threshold_min", pa.float64()),
            ("min_minutes", pa.int32()),
            ("peak_delay_min", pa.float64()),
            ("peak_utc", TS),
            ("peak_direction", pa.string()),
            ("mean_delay_min", pa.float64()),
            ("max_trip_delay_min", pa.float64()),
            ("n_minutes_above", pa.int32()),
            ("n_minutes_observed", pa.int32()),
            ("n_minutes_filled", pa.int32()),
            ("affected_station_ids", pa.list_(pa.string())),
            ("affected_station_names", pa.list_(pa.string())),
            ("affected_station_max_delay_min", pa.list_(pa.float64())),
            ("focal_station_ids", pa.list_(pa.string())),
            (
                "linked_alerts",
                pa.list_(
                    pa.struct(
                        [
                            ("alert_id", pa.string()),
                            ("alert_type", pa.string()),
                            ("is_planned", pa.bool_()),
                            ("header_text", pa.string()),
                            ("n_matching_stations", pa.int32()),
                        ]
                    )
                ),
            ),
            ("n_linked_alerts", pa.int32()),
            ("traffic_n_links", pa.int32()),
            ("traffic_speed_mph", pa.float64()),
            ("traffic_baseline_mph", pa.float64()),
            ("traffic_change_pct", pa.float64()),
            ("traffic_baseline_confidence", pa.string()),
            ("bikes_n_stations", pa.int32()),
            ("bikes_available", pa.float64()),
            ("bikes_baseline", pa.float64()),
            ("bikes_change", pa.float64()),
            ("bikes_change_pct", pa.float64()),
            ("bikes_baseline_confidence", pa.string()),
            ("summary", pa.string()),
            ("cited_metrics", pa.string()),
            # M4: cameras, impact score, surface recovery, sentence-level explanation.
            ("camera_ids", pa.list_(pa.string())),
            (
                "camera_counts",
                pa.list_(
                    pa.struct(
                        [
                            ("camera_id", pa.string()),
                            ("observed_utc", TS),
                            ("car", pa.int32()),
                            ("bus", pa.int32()),
                            ("truck", pa.int32()),
                            ("person", pa.int32()),
                        ]
                    )
                ),
            ),
            ("camera_n_cameras", pa.int32()),
            ("camera_vehicles", pa.float64()),
            ("camera_persons", pa.float64()),
            ("camera_objects", pa.float64()),
            ("camera_objects_baseline", pa.float64()),
            ("camera_change_pct", pa.float64()),
            ("camera_baseline_confidence", pa.string()),
            ("impact_score", pa.float64()),
            ("impact_signals_used", pa.list_(pa.string())),
            ("impact_detail", pa.string()),
            ("impact_peak_score", pa.float64()),
            ("impact_peak_utc", TS),
            ("surface_recovered_utc", TS),
            ("surface_recovery_status", pa.string()),
            ("surface_checked_until_utc", TS),
            ("explanation", pa.string()),
        ]
    ),
}

# Snapshot tables, rewritten as a whole: reference/<name>.parquet
REFERENCE_SCHEMAS: dict[str, pa.Schema] = {
    "subway_stations": pa.schema(
        [
            ("station_id", pa.string()),
            ("station_name", pa.string()),
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("routes", pa.list_(pa.string())),
        ]
    ),
    "traffic_links": pa.schema(
        [
            ("link_id", pa.string()),
            ("link_name", pa.string()),
            ("borough", pa.string()),
            ("owner", pa.string()),
            ("lats", pa.list_(pa.float64())),
            ("lons", pa.list_(pa.float64())),
            ("geometry_source", pa.string()),
            ("last_seen_utc", TS),
        ]
    ),
    "bike_stations": pa.schema(
        [
            ("station_id", pa.string()),
            ("name", pa.string()),
            ("short_name", pa.string()),
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("capacity", pa.int32()),
            ("region_id", pa.string()),
            ("last_seen_utc", TS),
        ]
    ),
    "station_neighbors": pa.schema(
        [
            ("station_id", pa.string()),
            ("station_name", pa.string()),
            ("station_lat", pa.float64()),
            ("station_lon", pa.float64()),
            ("neighbor_type", pa.string()),
            ("neighbor_id", pa.string()),
            ("neighbor_name", pa.string()),
            ("distance_m", pa.float64()),
        ]
    ),
    "baselines": pa.schema(
        [
            ("metric", pa.string()),
            ("entity_id", pa.string()),
            ("weekday", pa.int8()),
            ("slot_start_min", pa.int16()),
            ("n_days", pa.int32()),
            ("n_obs", pa.int32()),
            ("mean", pa.float64()),
            ("median", pa.float64()),
            ("std", pa.float64()),
            ("confidence", pa.string()),
            ("window_start_utc", TS),
            ("window_end_utc", TS),
            ("computed_utc", TS),
        ]
    ),
}

# Keys that identify one logical row inside a 1-minute bucket, and the ordering
# used to pick the row to keep (last wins).
DEDUP_KEYS: dict[str, tuple[list[str], str]] = {
    "stop_delays": (
        ["bucket_utc", "feed_id", "start_date", "trip_id", "stop_id", "scheduled_utc"],
        "observed_utc, run_id",
    ),
    # Within a minute, a valid observation beats a "missing" marker; then newest wins.
    "line_minute": (
        ["bucket_utc", "feed_id", "route_id", "direction"],
        "(status = 'ok'), fetched_utc, run_id",
    ),
    "quality_feed": (["run_id", "poll_index", "feed_id"], "fetched_utc"),
    "station_minute": (
        ["bucket_utc", "route_id", "direction", "station_id"],
        "observed_utc, run_id",
    ),
    "quality_source": (["run_id", "source"], "fetched_utc"),
    "alerts": (["alert_id", "updated_utc"], "fetched_utc, run_id"),
    "alert_presence": (["bucket_utc", "alert_id"], "observed_utc, run_id"),
    "traffic_minute": (["link_id", "observed_utc"], "fetched_utc, run_id"),
    "bike_minute": (["bucket_utc", "station_id"], "fetched_utc, run_id"),
    "events": (["event_id"], "computed_utc, run_id"),
}

# Row order inside compacted files; entity-first ordering compresses much better.
SORT_KEYS: dict[str, list[str]] = {
    "bike_minute": ["station_id", "bucket_utc"],
    "traffic_minute": ["link_id", "observed_utc"],
    "station_minute": ["route_id", "station_id", "bucket_utc"],
}

TABLES = tuple(SCHEMAS)
REFERENCE_TABLES = tuple(REFERENCE_SCHEMAS)


def epoch_to_utc(values) -> pd.Series:
    """Epoch seconds (nullable) -> naive UTC datetimes."""
    s = pd.Series(values)
    return pd.to_datetime(s.astype("Float64"), unit="s").astype("datetime64[us]")


def bucket_of(epoch_s: pd.Series) -> pd.Series:
    """Floor epoch seconds to the start of their UTC minute."""
    return epoch_to_utc(epoch_s).dt.floor("min")


def date_partition(epoch_s: float) -> str:
    return "date=" + dt.datetime.fromtimestamp(epoch_s, dt.UTC).strftime("%Y-%m-%d")


def to_arrow(df: pd.DataFrame, table: str) -> pa.Table:
    schema = SCHEMAS.get(table) or REFERENCE_SCHEMAS[table]
    data = {}
    for f in schema:
        if f.name in df.columns:
            data[f.name] = df[f.name]
        else:
            data[f.name] = pd.Series([None] * len(df), dtype="object")
    frame = pd.DataFrame(data, index=df.index)
    return pa.Table.from_pandas(frame, schema=schema, preserve_index=False)


def write_table(data_dir: Path, table: str, df: pd.DataFrame, run_id: str) -> list[Path]:
    """Write rows into date partitions (by ``bucket_utc``). Returns written files."""
    if df.empty:
        return []
    written = []
    dates = df["bucket_utc"].dt.strftime("%Y-%m-%d")
    for date, part in df.groupby(dates):
        target = Path(data_dir) / table / f"date={date}" / f"{run_id}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".parquet.tmp")
        pq.write_table(to_arrow(part, table), tmp, compression="zstd")
        os.replace(tmp, target)
        written.append(target)
    return written


def _glob(data_dir: Path, table: str) -> str:
    return (Path(data_dir) / table / "*" / "*.parquet").as_posix()


def _has_files(data_dir: Path, table: str) -> bool:
    return any((Path(data_dir) / table).glob("*/*.parquet"))


def connect(data_dir: Path, database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    """Open DuckDB with views over the Parquet tables.

    ``<table>_all`` exposes every stored row; ``<table>`` keeps one row per
    bucket key (deduplicated), which is what analyses should use.
    """
    con = duckdb.connect(database)
    # Stored timestamps are naive UTC; make now()/casts agree with them regardless
    # of the machine's local time zone.
    con.execute("SET TimeZone = 'UTC'")
    for table, schema in SCHEMAS.items():
        if _has_files(data_dir, table):
            con.execute(
                f"CREATE OR REPLACE VIEW {table}_all AS SELECT * FROM read_parquet("
                f"'{_glob(data_dir, table)}', hive_partitioning = true, union_by_name = true)"
            )
        else:
            con.register(f"_{table}_empty", schema.empty_table())
            con.execute(f"CREATE OR REPLACE VIEW {table}_all AS SELECT * FROM _{table}_empty")
        keys, order = DEDUP_KEYS[table]
        order_desc = ", ".join(f"{part.strip()} DESC" for part in order.split(","))
        con.execute(
            f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM {table}_all "
            f"QUALIFY row_number() OVER (PARTITION BY {', '.join(keys)} ORDER BY {order_desc}) = 1"
        )
    for name, schema in REFERENCE_SCHEMAS.items():
        path = reference_path(data_dir, name)
        if path.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{path.as_posix()}')"
            )
        else:
            con.register(f"_{name}_empty", schema.empty_table())
            con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM _{name}_empty")
    register_camera_views(con, data_dir)
    return con


def reference_path(data_dir: Path, name: str) -> Path:
    return Path(data_dir) / "reference" / f"{name}.parquet"


def read_reference(data_dir: Path, name: str) -> pd.DataFrame:
    path = reference_path(data_dir, name)
    if not path.exists():
        return REFERENCE_SCHEMAS[name].empty_table().to_pandas()
    return pd.read_parquet(path)


def write_reference(data_dir: Path, name: str, df: pd.DataFrame) -> Path:
    """Atomically replace a snapshot table."""
    path = reference_path(data_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(to_arrow(df.reset_index(drop=True), name), tmp, compression="zstd")
    os.replace(tmp, path)
    return path


def merge_reference(data_dir: Path, name: str, df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Upsert rows into a snapshot table by ``key`` (new rows win) and save it."""
    old = read_reference(data_dir, name)
    merged = pd.concat([old[~old[key].isin(df[key])], df], ignore_index=True) if len(old) else df
    merged = merged.sort_values(key).reset_index(drop=True)
    write_reference(data_dir, name, merged)
    return merged


def prune(data_dir: Path, table: str, keep_days: int, today: dt.date) -> list[str]:
    """Delete date partitions older than ``keep_days`` (retention for large tables)."""
    root = Path(data_dir) / table
    if not root.exists():
        return []
    cutoff = today - dt.timedelta(days=keep_days)
    removed = []
    for part_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("date=")):
        try:
            day = dt.date.fromisoformat(part_dir.name.removeprefix("date="))
        except ValueError:
            continue
        if day < cutoff:
            for f in part_dir.iterdir():
                f.unlink()
            part_dir.rmdir()
            removed.append(part_dir.name)
    return removed


def register_camera_views(con: duckdb.DuckDBPyConnection, data_dir: Path) -> None:
    """Camera tables (M3) live in the same data directory. Call again after new
    counts were written to a directory that had none (the view binds to the files
    that exist when it is created)."""
    from .cameras.storage import register_views

    register_views(con, data_dir)
    con.execute(
        "CREATE OR REPLACE VIEW camera_counts_ok AS SELECT *, "
        "(car + bus + truck)::DOUBLE AS vehicles, person::DOUBLE AS persons "
        "FROM camera_counts WHERE status = 'ok'"
    )


def compact(data_dir: Path, table: str, before: dt.date) -> list[str]:
    """Merge per-run files of each date partition older than ``before`` into one file.

    Rows are deduplicated with the same rule as the ``<table>`` view.
    Returns the partitions that were compacted.
    """
    root = Path(data_dir) / table
    if not root.exists():
        return []
    if table == "camera_counts":  # camera module table (M3), same dedup rule as its view
        from .cameras.storage import COUNTS_SCHEMA

        schema = COUNTS_SCHEMA
        keys, order = ["bucket_utc", "camera_id"], "observed_utc, run_id"
    else:
        schema = SCHEMAS[table]
        keys, order = DEDUP_KEYS[table]
    order_desc = ", ".join(f"{part.strip()} DESC" for part in order.split(","))
    done = []
    for part_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("date=")):
        try:
            day = dt.date.fromisoformat(part_dir.name.removeprefix("date="))
        except ValueError:
            continue
        files = sorted(part_dir.glob("*.parquet"))
        if day >= before or len(files) <= 1:
            continue
        target = part_dir / "compacted.parquet"
        tmp = part_dir / "compacted.parquet.tmp"
        cols = ", ".join(schema.names)
        src = ", ".join(f"'{f.as_posix()}'" for f in files)
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT {cols} FROM read_parquet([{src}], union_by_name = true) "
                f"QUALIFY row_number() OVER (PARTITION BY {', '.join(keys)} "
                f"ORDER BY {order_desc}) = 1 ORDER BY {', '.join(SORT_KEYS.get(table, keys[:1]))}) "
                f"TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)"
            )
        finally:
            con.close()
        os.replace(tmp, target)
        for f in files:
            if f != target:
                f.unlink()
        done.append(part_dir.name)
    return done


def line_minute_filled(
    con: duckdb.DuckDBPyConnection,
    start: dt.datetime,
    end: dt.datetime,
    max_age_min: int = 10,
) -> pd.DataFrame:
    """Continuous 1-minute grid per line with forward-filled values and freshness.

    ``fill_status`` is ``observed`` (fresh value in this minute), ``filled``
    (carried forward, ``age_min`` minutes old) or ``missing`` (no valid data
    within ``max_age_min`` or the feed was stale/down). Missing minutes have NULL
    metrics; they are never reported as normal.
    """
    sql = f"""
    WITH minutes AS (
        SELECT unnest(generate_series(
            date_trunc('minute', $start::TIMESTAMP),
            date_trunc('minute', $end::TIMESTAMP),
            INTERVAL 1 MINUTE)) AS minute_utc
    ),
    keys AS (
        SELECT DISTINCT feed_id, route_id, direction FROM line_minute
        WHERE bucket_utc BETWEEN $start::TIMESTAMP - INTERVAL {int(max_age_min)} MINUTE
                             AND $end::TIMESTAMP
    ),
    grid AS (SELECT * FROM minutes CROSS JOIN keys)
    SELECT g.minute_utc, g.feed_id, g.route_id, g.direction,
           l.bucket_utc AS source_bucket_utc,
           l.status AS source_status,
           date_diff('minute', l.bucket_utc, g.minute_utc) AS age_min,
           CASE
             WHEN l.bucket_utc IS NULL OR l.status <> 'ok'
                  OR date_diff('minute', l.bucket_utc, g.minute_utc) > {int(max_age_min)}
               THEN 'missing'
             WHEN l.bucket_utc = g.minute_utc THEN 'observed'
             ELSE 'filled'
           END AS fill_status,
           l.* EXCLUDE (bucket_utc, feed_id, route_id, direction, status)
    FROM grid g
    ASOF LEFT JOIN line_minute l
      ON g.feed_id = l.feed_id AND g.route_id = l.route_id
     AND g.direction IS NOT DISTINCT FROM l.direction
     AND g.minute_utc >= l.bucket_utc
    ORDER BY g.feed_id, g.route_id, g.direction, g.minute_utc
    """
    df = con.execute(sql, {"start": start, "end": end}).df()
    metric_cols = [
        c
        for c in df.columns
        if c.startswith(("delay_", "n_trips", "share_", "match_rate", "n_stop_updates"))
    ]
    df.loc[df["fill_status"] == "missing", metric_cols] = None
    return df


def entity_filled(
    con: duckdb.DuckDBPyConnection,
    table: str,
    id_col: str,
    time_col: str,
    value_cols: list[str],
    entity_ids: list[str],
    start: dt.datetime,
    end: dt.datetime,
    max_age_min: int,
    where: str = "TRUE",
) -> pd.DataFrame:
    """Per-entity 1-minute grid with the newest valid value at or before each minute.

    Values older than ``max_age_min`` are NULL (``age_min`` is still reported),
    so stale data never passes for a current reading.
    """
    cols = ["minute_utc", "entity_id", "age_min", *value_cols]
    if not entity_ids:
        return pd.DataFrame(columns=cols)
    ids = pd.DataFrame({"entity_id": [str(e) for e in entity_ids]})
    con.register("_entity_ids", ids)
    values = ", ".join(f"s.{c}" for c in value_cols)
    sql = f"""
    WITH minutes AS (
        SELECT unnest(generate_series(
            date_trunc('minute', $start::TIMESTAMP),
            date_trunc('minute', $end::TIMESTAMP),
            INTERVAL 1 MINUTE)) AS minute_utc
    ),
    src AS (
        SELECT {id_col} AS entity_id, date_trunc('minute', {time_col}) AS t, {", ".join(value_cols)}
        FROM {table}
        WHERE {id_col} IN (SELECT entity_id FROM _entity_ids)
          AND {time_col} BETWEEN $start::TIMESTAMP - INTERVAL {int(max_age_min)} MINUTE
                             AND $end::TIMESTAMP
          AND ({where})
        QUALIFY row_number() OVER (PARTITION BY {id_col}, date_trunc('minute', {time_col})
                                   ORDER BY {time_col} DESC) = 1
    ),
    grid AS (SELECT * FROM minutes CROSS JOIN _entity_ids)
    SELECT g.minute_utc, g.entity_id, date_diff('minute', s.t, g.minute_utc) AS age_min, {values}
    FROM grid g
    ASOF LEFT JOIN src s ON g.entity_id = s.entity_id AND g.minute_utc >= s.t
    ORDER BY g.entity_id, g.minute_utc
    """
    df = con.execute(sql, {"start": start, "end": end}).df()
    con.unregister("_entity_ids")
    stale = df["age_min"].isna() | (df["age_min"] > max_age_min)
    df.loc[stale, value_cols] = None
    return df[cols]
