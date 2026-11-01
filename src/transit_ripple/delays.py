"""Actual/predicted vs. scheduled time differences, per trip and stop."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .static_gtfs import MATCHED_METHODS, StaticSchedule, service_day_epoch

DELAY_COLUMNS = [
    "static_trip_id",
    "match_method",
    "station_id",
    "stop_name",
    "scheduled_arrival_ts",
    "scheduled_departure_ts",
    "arrival_delay_min",
    "departure_delay_min",
    "event",
    "event_ts",
    "scheduled_ts",
    "delay_min",
    "lead_s",
    "time_kind",
]


def compute_stop_delays(
    stop_updates: pd.DataFrame,
    schedule: StaticSchedule,
    stale_tolerance_s: int = 120,
) -> pd.DataFrame:
    """Attach schedule information and delays to stop updates.

    ``delay_min`` = realtime time - scheduled time, in minutes (positive = late).
    The arrival time is used when present, otherwise the departure time.
    ``time_kind``:

    - ``actual``: the current stop (first in the trip update) and its time is not
      after the feed header time, i.e. the train already arrived there;
    - ``stale``: a later stop whose predicted time is already more than
      ``stale_tolerance_s`` in the past (frozen predictions, e.g. ghost trains);
    - ``predicted``: everything else.
    """
    df = stop_updates.copy().reset_index(drop=True)
    if df.empty:
        for col in DELAY_COLUMNS:
            df[col] = pd.Series(dtype="object")
        return df

    # 1. Match each realtime trip to a static trip.
    trips = df[["trip_id", "start_date"]].drop_duplicates()
    matches = [
        schedule.match(t, d) for t, d in zip(trips["trip_id"], trips["start_date"], strict=True)
    ]
    trips = trips.assign(
        static_trip_id=[m.static_trip_id for m in matches],
        match_method=[m.method for m in matches],
    )
    df = df.merge(trips, on=["trip_id", "start_date"], how="left")

    # 2. Look up scheduled times for the matched trips and stops.
    st = schedule.scheduled_stop_times(df["static_trip_id"].dropna().unique())
    st = st.rename(
        columns={"trip_id": "static_trip_id", "arrival_s": "_arr_s", "departure_s": "_dep_s"}
    )
    df["_row"] = np.arange(len(df))
    merged = df.merge(
        st[["static_trip_id", "stop_id", "_arr_s", "_dep_s"]],
        on=["static_trip_id", "stop_id"],
        how="left",
    )

    base = merged["start_date"].map(_service_base_cache())
    merged["scheduled_arrival_ts"] = (base + merged["_arr_s"]).astype("Int64")
    merged["scheduled_departure_ts"] = (base + merged["_dep_s"]).astype("Int64")

    # A trip can visit a stop twice (loops); keep the visit closest in time.
    rt_time = merged["arrival_ts"].fillna(merged["departure_ts"]).astype("Float64")
    sched_time = (
        merged["scheduled_arrival_ts"].fillna(merged["scheduled_departure_ts"]).astype("Float64")
    )
    merged["_gap"] = (rt_time - sched_time).abs().fillna(np.inf)
    merged = (
        merged.sort_values(["_row", "_gap"], kind="stable")
        .drop_duplicates("_row", keep="first")
        .sort_values("_row")
        .reset_index(drop=True)
    )

    # 3. Delays.
    arr_delay = (merged["arrival_ts"] - merged["scheduled_arrival_ts"]).astype("Float64") / 60.0
    dep_delay = (merged["departure_ts"] - merged["scheduled_departure_ts"]).astype("Float64") / 60.0
    skipped = merged["stop_relationship"].eq("SKIPPED")
    merged["arrival_delay_min"] = arr_delay.mask(skipped)
    merged["departure_delay_min"] = dep_delay.mask(skipped)

    use_arrival = merged["arrival_ts"].notna()
    merged["event"] = np.where(use_arrival, "arrival", "departure")
    merged["event_ts"] = (
        merged["arrival_ts"].where(use_arrival, merged["departure_ts"]).astype("Int64")
    )
    merged["scheduled_ts"] = (
        merged["scheduled_arrival_ts"]
        .where(use_arrival, merged["scheduled_departure_ts"])
        .astype("Int64")
    )
    merged["delay_min"] = merged["arrival_delay_min"].where(
        use_arrival, merged["departure_delay_min"]
    )
    merged["lead_s"] = (merged["event_ts"] - merged["header_ts"]).astype("Int64")
    lead = merged["lead_s"].astype("Float64")
    first = merged["stop_index"].eq(0).fillna(False).astype(bool)
    past = (lead <= 0).fillna(False).astype(bool)
    stale = (~first & (lead < -stale_tolerance_s)).fillna(False).astype(bool)
    merged["time_kind"] = np.select([first & past, stale], ["actual", "stale"], default="predicted")

    merged["station_id"] = merged["stop_id"].map(schedule.parent_station)
    merged["stop_name"] = merged["stop_id"].map(schedule.stop_names)
    return merged.drop(columns=["_row", "_gap", "_arr_s", "_dep_s"])


def _service_base_cache():
    cache: dict[str, float] = {}

    def lookup(date):
        if not isinstance(date, str):
            return np.nan
        if date not in cache:
            try:
                cache[date] = float(service_day_epoch(date))
            except ValueError:
                cache[date] = np.nan
        return cache[date]

    return lookup


TRIP_KEY = ["feed_id", "trip_id", "start_date"]


def trip_current_delays(stop_delays: pd.DataFrame) -> pd.DataFrame:
    """One row per trip: the delay at its first stop update that has a delay.

    For an assigned train this is the current or next station, i.e. the
    closest-to-observed estimate of how late the train is right now.
    """
    cols = TRIP_KEY + [
        "header_ts",
        "route_id",
        "direction",
        "is_assigned",
        "match_method",
        "stop_id",
        "delay_min",
        "time_kind",
    ]
    if stop_delays.empty:
        return pd.DataFrame(columns=cols)
    with_delay = stop_delays[stop_delays["delay_min"].notna()]
    first = with_delay.sort_values(TRIP_KEY + ["stop_index"]).drop_duplicates(TRIP_KEY)
    return first[cols].reset_index(drop=True)


LINE_GROUP = ["feed_id", "route_id", "direction"]


def line_snapshot(
    stop_delays: pd.DataFrame,
    trip_delays: pd.DataFrame,
    delayed_threshold_min: float,
) -> pd.DataFrame:
    """Aggregate one feed snapshot into per (route, direction) line metrics.

    Trips flagged as not assigned to a train are excluded from delay statistics,
    because their times are just the schedule.
    """
    cols = LINE_GROUP + [
        "n_trips",
        "n_trips_unassigned",
        "n_trips_unmatched",
        "n_trips_measured",
        "n_stop_updates",
        "delay_mean_min",
        "delay_median_min",
        "delay_p90_min",
        "delay_max_min",
        "share_trips_delayed",
        "match_rate",
    ]
    if stop_delays.empty:
        return pd.DataFrame(columns=cols)

    trips = stop_delays.drop_duplicates(TRIP_KEY)[
        LINE_GROUP + ["trip_id", "start_date", "is_assigned", "match_method"]
    ]
    trips = trips.assign(
        unassigned=trips["is_assigned"].eq(False).fillna(False).astype(bool),
        matched=trips["match_method"].isin(MATCHED_METHODS),
    )
    counts = trips.groupby(LINE_GROUP, dropna=False).agg(
        n_trips=("trip_id", "size"),
        n_trips_unassigned=("unassigned", "sum"),
        n_matched=("matched", "sum"),
    )
    unmatched_assigned = (
        trips[~trips["unassigned"] & ~trips["matched"]]
        .groupby(LINE_GROUP, dropna=False)
        .size()
        .rename("n_trips_unmatched")
    )
    n_updates = stop_delays.groupby(LINE_GROUP, dropna=False).size().rename("n_stop_updates")

    measured = trip_delays[~trip_delays["is_assigned"].eq(False).fillna(False).astype(bool)]
    d = measured.assign(
        delay=measured["delay_min"].astype(float),
        late=(measured["delay_min"].astype(float) >= delayed_threshold_min).astype(float),
    )
    stats = d.groupby(LINE_GROUP, dropna=False).agg(
        n_trips_measured=("delay", "size"),
        delay_mean_min=("delay", "mean"),
        delay_median_min=("delay", "median"),
        delay_p90_min=("delay", lambda s: float(np.quantile(s, 0.9)) if len(s) else np.nan),
        delay_max_min=("delay", "max"),
        share_trips_delayed=("late", "mean"),
    )

    out = counts.join([unmatched_assigned, n_updates, stats], how="left").reset_index()
    out["n_trips_unmatched"] = out["n_trips_unmatched"].fillna(0).astype(int)
    out["n_trips_measured"] = out["n_trips_measured"].fillna(0).astype(int)
    out["n_trips_unassigned"] = out["n_trips_unassigned"].astype(int)
    out["match_rate"] = out["n_matched"] / out["n_trips"]
    # Long-term aggregates: 0.01 min (0.6 s) is finer than the 1 s input resolution
    # matters for, and rounding makes the stored Parquet about 3x smaller.
    for col in ("delay_mean_min", "delay_median_min", "delay_p90_min", "delay_max_min"):
        out[col] = out[col].astype(float).round(2)
    for col in ("share_trips_delayed", "match_rate"):
        out[col] = out[col].astype(float).round(3)
    return out[cols]
