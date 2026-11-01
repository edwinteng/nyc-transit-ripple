"""Historical baselines by weekday and time of day (New York local time).

For every entity (traffic link, Citi Bike station) and every (weekday, slot)
the baseline holds the mean/median/std of past valid observations. A baseline
built from fewer than ``baseline_min_days`` distinct days is flagged ``low``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from .config import AGENCY_TIMEZONE, Settings
from .storage import read_reference, write_reference

METRICS = {
    # metric: (table, entity column, time column, value column, validity filter)
    "traffic_speed_mph": ("traffic_minute", "link_id", "observed_utc", "speed_mph", "is_valid"),
    "bikes_available": (
        "bike_minute",
        "station_id",
        "bucket_utc",
        "bikes_available",
        "is_installed AND is_renting AND report_age_s <= {bike_stale}",
    ),
    "docks_available": (
        "bike_minute",
        "station_id",
        "bucket_utc",
        "docks_available",
        "is_installed AND is_returning AND report_age_s <= {bike_stale}",
    ),
}

CONFIDENCE_OK = "ok"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"


def local_slot(ts_utc: pd.Series, slot_min: int, tz: str = AGENCY_TIMEZONE) -> pd.DataFrame:
    """Weekday (Mon=0) and slot start (minutes after local midnight) of UTC times."""
    local = pd.to_datetime(ts_utc).dt.tz_localize("UTC").dt.tz_convert(ZoneInfo(tz))
    minutes = local.dt.hour * 60 + local.dt.minute
    return pd.DataFrame(
        {
            "weekday": local.dt.day_of_week.astype("int8"),
            "slot_start_min": (minutes // slot_min) * slot_min,
        },
        index=ts_utc.index,
    )


def compute_baselines(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    now: dt.datetime,
) -> pd.DataFrame:
    """Baselines over the last ``baseline_days`` days for every metric in METRICS."""
    computed = now.replace(tzinfo=None) if now.tzinfo else now
    end = computed - dt.timedelta(hours=settings.baseline_exclude_recent_hours)
    start = computed - dt.timedelta(days=settings.baseline_days)
    slot = int(settings.baseline_slot_min)
    frames = []
    for metric, (table, id_col, time_col, value_col, where) in METRICS.items():
        cond = where.format(bike_stale=float(settings.bike_station_stale_after_s))
        sql = f"""
        WITH obs AS (
            SELECT {id_col} AS entity_id, {value_col}::DOUBLE AS v,
                   timezone('{AGENCY_TIMEZONE}', {time_col}::TIMESTAMPTZ) AS lt
            FROM {table}
            WHERE {time_col} >= $start AND {time_col} < $end AND {value_col} IS NOT NULL
              AND ({cond})
        )
        SELECT '{metric}' AS metric, entity_id,
               (isodow(lt) - 1)::TINYINT AS weekday,
               ((hour(lt) * 60 + minute(lt)) // {slot} * {slot})::SMALLINT AS slot_start_min,
               count(DISTINCT lt::DATE)::INTEGER AS n_days,
               count(*)::INTEGER AS n_obs,
               avg(v) AS mean, median(v) AS median, stddev_samp(v) AS std
        FROM obs GROUP BY ALL
        """
        frames.append(con.execute(sql, {"start": start, "end": end}).df())
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return read_reference_schema()
    out["confidence"] = (out["n_days"] >= settings.baseline_min_days).map(
        {True: CONFIDENCE_OK, False: CONFIDENCE_LOW}
    )
    for col in ("mean", "median", "std"):
        out[col] = out[col].round(3)
    out["window_start_utc"] = pd.Timestamp(start)
    out["window_end_utc"] = pd.Timestamp(end)
    out["computed_utc"] = pd.Timestamp(computed)
    return out.sort_values(["metric", "entity_id", "weekday", "slot_start_min"]).reset_index(
        drop=True
    )


def read_reference_schema() -> pd.DataFrame:
    from .storage import REFERENCE_SCHEMAS

    return REFERENCE_SCHEMAS["baselines"].empty_table().to_pandas()


def _meta_path(data_dir) -> Path:
    return Path(data_dir) / "reference" / "baselines_meta.json"


def baselines_age_hours(data_dir, now: dt.datetime) -> float | None:
    """Hours since baselines were last computed (even if they came out empty)."""
    meta = _meta_path(data_dir)
    if not meta.exists():
        return None
    computed = pd.Timestamp(json.loads(meta.read_text())["computed_utc"])
    end = now.replace(tzinfo=None) if now.tzinfo else now
    return (pd.Timestamp(end) - computed).total_seconds() / 3600.0


def refresh_baselines(
    con, settings: Settings, now: dt.datetime, force: bool = False
) -> tuple[bool, pd.DataFrame]:
    """Recompute baselines if they are older than ``baseline_max_age_hours``."""
    age = baselines_age_hours(settings.data_dir, now)
    if not force and age is not None and age < settings.baseline_max_age_hours:
        return False, read_reference(settings.data_dir, "baselines")
    b = compute_baselines(con, settings, now)
    write_reference(settings.data_dir, "baselines", b)
    computed = now.replace(tzinfo=None) if now.tzinfo else now
    meta = {
        "computed_utc": pd.Timestamp(computed).isoformat(),
        "rows": len(b),
        "baseline_days": settings.baseline_days,
        "slot_min": settings.baseline_slot_min,
        "min_days_for_ok": settings.baseline_min_days,
        "exclude_recent_hours": settings.baseline_exclude_recent_hours,
    }
    _meta_path(settings.data_dir).write_text(json.dumps(meta, indent=2))
    return True, b


def lookup(
    baselines: pd.DataFrame,
    metric: str,
    grid: pd.DataFrame,
    slot_min: int,
) -> pd.DataFrame:
    """Attach ``baseline``, ``baseline_n_days`` and ``baseline_confidence`` to a grid.

    ``grid`` has ``minute_utc`` and ``entity_id`` columns. Missing baselines give
    NaN and confidence ``none``.
    """
    out = grid.copy()
    if out.empty:
        return out.assign(
            baseline=pd.Series(dtype=float),
            baseline_n_days=pd.Series(dtype=float),
            baseline_confidence=pd.Series(dtype=object),
        )
    slots = local_slot(out["minute_utc"], slot_min)
    out = pd.concat([out, slots], axis=1)
    b = baselines[baselines["metric"] == metric][
        ["entity_id", "weekday", "slot_start_min", "mean", "n_days", "confidence"]
    ].rename(
        columns={
            "mean": "baseline",
            "n_days": "baseline_n_days",
            "confidence": "baseline_confidence",
        }
    )
    b = b.astype({"weekday": "int8", "slot_start_min": "int64"})
    out["slot_start_min"] = out["slot_start_min"].astype("int64")
    out = out.merge(b, on=["entity_id", "weekday", "slot_start_min"], how="left")
    out["baseline_confidence"] = out["baseline_confidence"].fillna(CONFIDENCE_NONE)
    return out
