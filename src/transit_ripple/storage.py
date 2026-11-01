"""Parquet storage in 1-minute UTC buckets, plus DuckDB views over it.

Layout (all under the data directory)::

    stop_delays/date=YYYY-MM-DD/<run_id>.parquet   per trip x stop observations
    line_minute/date=YYYY-MM-DD/<run_id>.parquet   per route x direction x minute
    quality_feed/date=YYYY-MM-DD/<run_id>.parquet  per feed x poll quality counters
    reports/date=YYYY-MM-DD/<run_id>.json          full quality report per run

All timestamps are naive UTC (column names end in ``_utc``).
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
}

TABLES = tuple(SCHEMAS)


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
    schema = SCHEMAS[table]
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
    return con


def compact(data_dir: Path, table: str, before: dt.date) -> list[str]:
    """Merge per-run files of each date partition older than ``before`` into one file.

    Rows are deduplicated with the same rule as the ``<table>`` view.
    Returns the partitions that were compacted.
    """
    root = Path(data_dir) / table
    if not root.exists():
        return []
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
        cols = ", ".join(SCHEMAS[table].names)
        src = ", ".join(f"'{f.as_posix()}'" for f in files)
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT {cols} FROM read_parquet([{src}], union_by_name = true) "
                f"QUALIFY row_number() OVER (PARTITION BY {', '.join(keys)} "
                f"ORDER BY {order_desc}) = 1 ORDER BY {keys[0]}) "
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
