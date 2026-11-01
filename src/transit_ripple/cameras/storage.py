"""Parquet storage for camera data, following the project conventions.

- ``camera_counts/date=YYYY-MM-DD/<run_id>.parquet``: one row per camera and fetch,
  bucketed by the 1-minute UTC bucket of the fetch time. Numbers only, no images.
- ``reference/cameras.parquet``: camera inventory (snapshot).
- ``reference/camera_station_map.parquet``: cameras within the radius of each
  subway station (snapshot).

This module is self-contained so it does not change the shared storage code;
``register_views`` adds the camera tables to any DuckDB connection.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

TS = pa.timestamp("us")

COUNT_CLASSES = ("car", "bus", "truck", "person")

COUNTS_SCHEMA = pa.schema(
    [
        ("bucket_utc", TS),
        ("observed_utc", TS),
        ("run_id", pa.string()),
        ("camera_id", pa.string()),
        ("status", pa.string()),
        ("detail", pa.string()),
        ("car", pa.int32()),
        ("bus", pa.int32()),
        ("truck", pa.int32()),
        ("person", pa.int32()),
        ("repeat_frame", pa.bool_()),
        ("model", pa.string()),
        ("score_threshold", pa.float64()),
        ("image_width", pa.int32()),
        ("image_height", pa.int32()),
        ("fetch_ms", pa.float64()),
        ("inference_ms", pa.float64()),
    ]
)

REFERENCE_SCHEMAS = {
    "cameras": pa.schema(
        [
            ("camera_id", pa.string()),
            ("name", pa.string()),
            ("area", pa.string()),
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("is_online", pa.bool_()),
            ("image_url", pa.string()),
            ("fetched_utc", TS),
        ]
    ),
    "camera_station_map": pa.schema(
        [
            ("station_id", pa.string()),
            ("station_name", pa.string()),
            ("station_lat", pa.float64()),
            ("station_lon", pa.float64()),
            ("camera_id", pa.string()),
            ("camera_name", pa.string()),
            ("distance_m", pa.float64()),
        ]
    ),
}


def _to_arrow(df: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    cols = {
        f.name: (df[f.name] if f.name in df.columns else pd.Series([None] * len(df), dtype=object))
        for f in schema
    }
    return pa.Table.from_pandas(
        pd.DataFrame(cols, index=df.index), schema=schema, preserve_index=False
    )


def reference_path(data_dir: Path, name: str) -> Path:
    return Path(data_dir) / "reference" / f"{name}.parquet"


def write_reference(data_dir: Path, name: str, df: pd.DataFrame) -> Path:
    path = reference_path(data_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(
        _to_arrow(df.reset_index(drop=True), REFERENCE_SCHEMAS[name]), tmp, compression="zstd"
    )
    os.replace(tmp, path)
    return path


def read_reference(data_dir: Path, name: str) -> pd.DataFrame:
    path = reference_path(data_dir, name)
    if not path.exists():
        return REFERENCE_SCHEMAS[name].empty_table().to_pandas()
    return pd.read_parquet(path)


def write_counts(data_dir: Path, df: pd.DataFrame, run_id: str) -> list[Path]:
    """Write count rows into date partitions of ``camera_counts``."""
    if df.empty:
        return []
    written = []
    for date, part in df.groupby(df["bucket_utc"].dt.strftime("%Y-%m-%d")):
        target = Path(data_dir) / "camera_counts" / f"date={date}" / f"{run_id}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".parquet.tmp")
        pq.write_table(_to_arrow(part, COUNTS_SCHEMA), tmp, compression="zstd")
        os.replace(tmp, target)
        written.append(target)
    return written


def register_views(con: duckdb.DuckDBPyConnection, data_dir: Path) -> duckdb.DuckDBPyConnection:
    """Create ``camera_counts`` (+ ``_all``) and reference views on a connection."""
    root = Path(data_dir)
    if any((root / "camera_counts").glob("*/*.parquet")):
        glob = (root / "camera_counts" / "*" / "*.parquet").as_posix()
        con.execute(
            "CREATE OR REPLACE VIEW camera_counts_all AS SELECT * FROM read_parquet("
            f"'{glob}', hive_partitioning = true, union_by_name = true)"
        )
    else:
        con.register("_camera_counts_empty", COUNTS_SCHEMA.empty_table())
        con.execute(
            "CREATE OR REPLACE VIEW camera_counts_all AS SELECT * FROM _camera_counts_empty"
        )
    # One row per camera and minute: the latest fetch wins.
    con.execute(
        "CREATE OR REPLACE VIEW camera_counts AS SELECT * FROM camera_counts_all QUALIFY "
        "row_number() OVER (PARTITION BY bucket_utc, camera_id ORDER BY observed_utc DESC, "
        "run_id DESC) = 1"
    )
    for name, schema in REFERENCE_SCHEMAS.items():
        path = reference_path(root, name)
        if path.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{path.as_posix()}')"
            )
        else:
            con.register(f"_{name}_empty", schema.empty_table())
            con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM _{name}_empty")
    return con


def connect(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """DuckDB connection with the camera views (session time zone UTC)."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return register_views(con, data_dir)
