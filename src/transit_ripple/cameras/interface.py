"""Integration point for event detection: counts from cameras near a station.

``count_near_station(station_id, timestamp)`` returns one row per camera within
the configured radius of a subway station:

- if ``timestamp`` is within ``live_window_min`` of now (or omitted), the cameras
  are counted live (politely) and the result is also stored, so it becomes
  history;
- otherwise the stored count closest to ``timestamp`` within
  ``match_tolerance_min`` is returned (frames are never stored, so the past can
  only be answered from counts made at that time).

Cameras without a usable count are returned with ``status = 'missing'`` (or the
failure status) and NULL counts, never as zero.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import pandas as pd

from .config import CameraSettings
from .detector import COUNT_NAMES
from .storage import connect, read_reference, write_counts

RESULT_COLUMNS = [
    "station_id",
    "camera_id",
    "camera_name",
    "distance_m",
    "source",
    "status",
    "observed_utc",
    "age_s",
    *COUNT_NAMES,
]


def _naive_utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_convert("UTC").tz_localize(None) if t.tzinfo else t


def cameras_near_stations(settings: CameraSettings, station_ids: list[str]) -> pd.DataFrame:
    """(station, camera) pairs within the radius, nearest first, with image URLs."""
    mapping = read_reference(settings.data_dir, "camera_station_map")
    if mapping.empty:
        raise ValueError(
            "No camera/station mapping found; run `python -m transit_ripple.cameras map` first."
        )
    ids = [str(s) for s in station_ids]
    near = mapping[mapping["station_id"].isin(ids) & (mapping["distance_m"] <= settings.radius_m)]
    cams = read_reference(settings.data_dir, "cameras")[["camera_id", "image_url"]]
    return near.merge(cams, on="camera_id", how="left")


def cameras_near_station(settings: CameraSettings, station_id: str) -> pd.DataFrame:
    return cameras_near_stations(settings, [station_id])


def select_cameras(
    near: pd.DataFrame, station_ids: list[str], max_cameras: int | None
) -> pd.DataFrame:
    """Distinct cameras to count: round-robin over the stations (in the given order,
    nearest camera first at each), so a cap still covers every station."""
    order = {str(s): i for i, s in enumerate(station_ids)}
    ranked = near.assign(
        _station_order=near["station_id"].map(order),
        _rank=near.groupby("station_id")["distance_m"].rank(method="first"),
    ).sort_values(["_rank", "_station_order", "distance_m"])
    distinct = ranked.drop_duplicates("camera_id")
    if max_cameras is not None:
        distinct = distinct.head(max(0, int(max_cameras)))
    return distinct[["camera_id", "image_url"]]


def count_near_stations(
    station_ids: list[str],
    timestamp: dt.datetime | pd.Timestamp | None = None,
    *,
    settings: CameraSettings | None = None,
    counter_factory: Callable[[], object] | None = None,
    now: dt.datetime | None = None,
    store: bool = True,
    max_cameras: int | None = None,
) -> pd.DataFrame:
    """Like ``count_near_station`` for several stations at once.

    A camera near several of the stations is counted once (and reported for each
    station). ``max_cameras`` caps how many cameras are counted live; cameras over
    the cap are returned with ``status = 'skipped_cap'`` and NULL counts.
    """
    settings = settings or CameraSettings.from_env()
    now_ts = _naive_utc(now or dt.datetime.now(dt.UTC))
    at = _naive_utc(timestamp) if timestamp is not None else now_ts
    near = cameras_near_stations(settings, station_ids)
    if near.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    live = abs((now_ts - at).total_seconds()) <= settings.live_window_min * 60
    if live:
        chosen = select_cameras(near, station_ids, max_cameras)
        if len(chosen):
            counter = (counter_factory or _default_counter)(settings)
            rows, _report = counter.count_cameras(chosen)
            if store and len(rows):
                write_counts(settings.data_dir, rows, rows["run_id"].iloc[0])
            found = rows.assign(source="live")
        else:
            found = pd.DataFrame(columns=["camera_id", "status", "source", "observed_utc"])
    else:
        con = connect(settings.data_dir)
        tol = pd.Timedelta(minutes=settings.match_tolerance_min)
        found = (
            con.execute(
                """
            SELECT * FROM camera_counts
            WHERE list_contains($ids, camera_id)
              AND observed_utc BETWEEN $lo AND $hi
            QUALIFY row_number() OVER (
                PARTITION BY camera_id ORDER BY abs(epoch(observed_utc) - epoch($at::TIMESTAMP))
            ) = 1
            """,
                {
                    "ids": near["camera_id"].unique().tolist(),
                    "lo": at - tol,
                    "hi": at + tol,
                    "at": at,
                },
            )
            .df()
            .assign(source="stored")
        )

    out = near[["station_id", "camera_id", "camera_name", "distance_m"]].merge(
        found.drop(columns=[c for c in ("station_id",) if c in found.columns]),
        on="camera_id",
        how="left",
    )
    for name in COUNT_NAMES:
        if name not in out:
            out[name] = None
    capped = live and max_cameras is not None
    out["source"] = out["source"].fillna("none")
    out["status"] = out["status"].fillna("skipped_cap" if capped else "missing")
    out["observed_utc"] = pd.to_datetime(out.get("observed_utc"))
    # Seconds between the requested time and the observation (negative: observed after
    # it). Live counts are taken now, so their age is 0.
    out["age_s"] = (at - out["observed_utc"]).dt.total_seconds()
    out.loc[out["source"] == "live", "age_s"] = 0.0
    for name in COUNT_NAMES:
        out[name] = (
            pd.to_numeric(out[name], errors="coerce").where(out["status"] == "ok").astype("Int64")
        )
    return (
        out[RESULT_COLUMNS]
        .sort_values(["station_id", "distance_m", "camera_id"])
        .reset_index(drop=True)
    )


def count_near_station(
    station_id: str,
    timestamp: dt.datetime | pd.Timestamp | None = None,
    *,
    settings: CameraSettings | None = None,
    counter_factory: Callable[[], object] | None = None,
    now: dt.datetime | None = None,
    store: bool = True,
    max_cameras: int | None = None,
) -> pd.DataFrame:
    """Counts of cars, buses, trucks and people at cameras near ``station_id``.

    ``counter_factory`` returns a ``CameraCounter``; it is only called for live
    counting (the default builds one with the configured model and polite client).
    """
    out = count_near_stations(
        [station_id],
        timestamp,
        settings=settings,
        counter_factory=counter_factory,
        now=now,
        store=store,
        max_cameras=max_cameras,
    )
    return out.sort_values(["distance_m", "camera_id"]).reset_index(drop=True)


def summarize_station_counts(df: pd.DataFrame) -> dict:
    """Totals over cameras with valid counts (and how many cameras that was)."""
    ok = df[df["status"] == "ok"]
    totals = {name: (int(ok[name].sum()) if len(ok) else None) for name in COUNT_NAMES}
    return {"n_cameras": len(df), "n_cameras_counted": len(ok), **totals}


def _default_counter(settings: CameraSettings):
    from .client import PoliteClient
    from .counting import CameraCounter
    from .detector import load_detector

    return CameraCounter(settings, PoliteClient(settings), load_detector(settings))
