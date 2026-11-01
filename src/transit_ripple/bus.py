"""MTA bus data (M5): Bus Time SIRI vehicle positions -> segment speeds.

Bus Time's SIRI VehicleMonitoring call returns every bus in service with its
position, trip and next stop. For each bus the distance travelled along its route
is ``CallDistanceAlongRoute - DistanceFromCall`` (metres). Two snapshots of the
same bus on the same trip give a speed sample; the sample is assigned to the
**segment** that ends at the bus's next stop (``route|direction|stop``).

Stop locations and the routes serving each stop come from the MTA bus static GTFS
(public, no key), refreshed weekly. Only segments ending at a stop within the
neighbor radius of a subway station are stored.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

MPS_TO_MPH = 2.236936
# Buses at a terminal (layover) or still finishing the previous trip do not move
# along the route of the trip they are assigned to.
NOT_MOVING_STATUSES = ("layover", "prevTrip", "spooking")

VEHICLE_COLUMNS = [
    "vehicle_id",
    "trip_id",
    "route_id",
    "direction_id",
    "recorded_ts",
    "lat",
    "lon",
    "next_stop_id",
    "dist_along_m",
    "progress_status",
]


class SiriError(Exception):
    """Undecodable SIRI payload."""


class SiriAuthError(SiriError):
    """The API key is missing, unknown or not authorized."""


@dataclass
class ParsedSiri:
    vehicles: pd.DataFrame
    response_ts: float | None
    n_activities: int
    n_bad: int


def strip_agency(ref) -> str | None:
    """'MTA NYCT_B63' -> 'B63', 'MTA_308209' -> '308209' (GTFS ids)."""
    if ref is None:
        return None
    ref = str(ref)
    return ref.split("_", 1)[1] if "_" in ref else ref


def _first(value):
    """SIRI v2 wraps many strings in one-element lists."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _epoch(value) -> float | None:
    if not value:
        return None
    try:
        return pd.Timestamp(value).timestamp()
    except (ValueError, TypeError):
        return None


def parse_vehicle_monitoring(content: bytes) -> ParsedSiri:
    """Vehicles from a SIRI VehicleMonitoring JSON response (version 1 or 2)."""
    try:
        doc = json.loads(content)
        delivery = doc["Siri"]["ServiceDelivery"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SiriError(f"not a SIRI ServiceDelivery: {exc}") from exc
    vm = (delivery.get("VehicleMonitoringDelivery") or [{}])[0]
    err = vm.get("ErrorCondition")
    if err:
        text = err.get("Description") or json.dumps(err)[:200]
        if "key" in text.lower() or "authoriz" in text.lower():
            raise SiriAuthError(text)
        raise SiriError(text)
    rows, bad = [], 0
    activities = vm.get("VehicleActivity") or []
    for act in activities:
        j = act.get("MonitoredVehicleJourney") or {}
        call = j.get("MonitoredCall") or {}
        dist = (call.get("Extensions") or {}).get("Distances") or {}
        loc = j.get("VehicleLocation") or {}
        try:
            along = float(dist["CallDistanceAlongRoute"]) - float(dist["DistanceFromCall"])
        except (KeyError, TypeError, ValueError):
            along = np.nan
        frame = j.get("FramedVehicleJourneyRef") or {}
        row = {
            "vehicle_id": j.get("VehicleRef"),
            "trip_id": frame.get("DatedVehicleJourneyRef"),
            "route_id": strip_agency(j.get("LineRef")),
            "direction_id": _int(j.get("DirectionRef")),
            "recorded_ts": _epoch(act.get("RecordedAtTime")),
            "lat": _float(loc.get("Latitude")),
            "lon": _float(loc.get("Longitude")),
            "next_stop_id": strip_agency(call.get("StopPointRef")),
            "dist_along_m": along,
            "progress_status": _first(j.get("ProgressStatus")),
        }
        if not row["vehicle_id"] or row["recorded_ts"] is None or not row["route_id"]:
            bad += 1
            continue
        rows.append(row)
    vehicles = pd.DataFrame(rows, columns=VEHICLE_COLUMNS)
    response_ts = _epoch(vm.get("ResponseTimestamp") or delivery.get("ResponseTimestamp"))
    return ParsedSiri(vehicles, response_ts, len(activities), bad)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def speed_samples(
    prev: pd.DataFrame,
    cur: pd.DataFrame,
    min_pair_s: float,
    max_pair_s: float,
    max_speed_mph: float,
) -> pd.DataFrame:
    """Speed of each bus between two snapshots, assigned to its current segment."""
    cols = [
        "observed_ts",
        "route_id",
        "direction_id",
        "stop_id",
        "vehicle_id",
        "speed_mph",
        "distance_m",
        "duration_s",
    ]
    if prev.empty or cur.empty:
        return pd.DataFrame(columns=cols)
    m = cur.merge(prev, on=["vehicle_id", "trip_id"], suffixes=("", "_prev"))
    moving = ~m["progress_status"].isin(NOT_MOVING_STATUSES) & ~m["progress_status_prev"].isin(
        NOT_MOVING_STATUSES
    )
    m = m[moving & m["dist_along_m"].notna() & m["dist_along_m_prev"].notna()]
    m = m.assign(
        duration_s=m["recorded_ts"] - m["recorded_ts_prev"],
        distance_m=m["dist_along_m"] - m["dist_along_m_prev"],
    )
    m = m[(m["duration_s"] >= min_pair_s) & (m["duration_s"] <= max_pair_s)]
    m = m[m["distance_m"] >= 0]  # a negative distance means a new trip or bad data
    m = m.assign(speed_mph=m["distance_m"] / m["duration_s"] * MPS_TO_MPH)
    m = m[(m["speed_mph"] <= max_speed_mph) & m["next_stop_id"].notna()]
    return pd.DataFrame(
        {
            "observed_ts": m["recorded_ts"].to_numpy(),
            "route_id": m["route_id"].to_numpy(),
            "direction_id": m["direction_id"].to_numpy(),
            "stop_id": m["next_stop_id"].to_numpy(),
            "vehicle_id": m["vehicle_id"].to_numpy(),
            "speed_mph": m["speed_mph"].to_numpy(),
            "distance_m": m["distance_m"].to_numpy(),
            "duration_s": m["duration_s"].to_numpy(),
        },
        columns=cols,
    )


def segment_id(route_id, direction_id, stop_id) -> str:
    d = "" if direction_id is None or pd.isna(direction_id) else int(direction_id)
    return f"{route_id}|{d}|{stop_id}"


def bus_minute_rows(
    samples: pd.DataFrame, run_id: str, fetched_ts: float, near_stops: set[str] | None
) -> pd.DataFrame:
    """1-minute segment speeds: total distance / total time of the samples (so a
    bus that crawled for longer weighs more than one that passed quickly)."""
    from .storage import bucket_of, epoch_to_utc

    if samples.empty:
        return pd.DataFrame()
    s = samples if near_stops is None else samples[samples["stop_id"].isin(near_stops)]
    if s.empty:
        return pd.DataFrame()
    s = s.assign(bucket_utc=bucket_of(s["observed_ts"]).to_numpy())
    g = (
        s.groupby(["bucket_utc", "route_id", "direction_id", "stop_id"], dropna=False)
        .agg(
            observed_ts=("observed_ts", "max"),
            n_samples=("speed_mph", "size"),
            n_vehicles=("vehicle_id", "nunique"),
            distance_m=("distance_m", "sum"),
            duration_s=("duration_s", "sum"),
        )
        .reset_index()
    )
    g["speed_mph"] = (g["distance_m"] / g["duration_s"] * MPS_TO_MPH).round(2)
    g["observed_utc"] = epoch_to_utc(g["observed_ts"]).to_numpy()
    g["fetched_utc"] = epoch_to_utc([fetched_ts] * len(g)).to_numpy()
    g["run_id"] = run_id
    g["segment_id"] = [
        segment_id(r, d, st)
        for r, d, st in zip(g["route_id"], g["direction_id"], g["stop_id"], strict=True)
    ]
    g["direction_id"] = g["direction_id"].astype("Int32")
    return g.drop(columns=["observed_ts"])


# --------------------------------------------------------------------------------------
# Static GTFS: stop locations and the routes at each stop
# --------------------------------------------------------------------------------------


def bus_stops_from_zips(zip_paths: list[Path]) -> pd.DataFrame:
    """``stop_id, stop_name, lat, lon, routes`` from MTA bus static GTFS zips."""
    frames = []
    con = duckdb.connect()
    try:
        for zp in zip_paths:
            with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zp) as zf:
                names = set(zf.namelist())
                need = {"stops.txt", "trips.txt", "stop_times.txt"}
                if not need <= names:
                    log.warning("%s lacks %s; skipped", zp.name, sorted(need - names))
                    continue
                paths = {n: Path(zf.extract(n, tmp)).as_posix() for n in need}
                df = con.execute(
                    f"""
                    WITH st AS (
                        SELECT DISTINCT trip_id, stop_id
                        FROM read_csv('{paths["stop_times.txt"]}', header=true, all_varchar=true)
                    ),
                    rs AS (
                        SELECT st.stop_id, list(DISTINCT t.route_id ORDER BY t.route_id) AS routes
                        FROM st JOIN read_csv('{paths["trips.txt"]}', header=true,
                                              all_varchar=true) t USING (trip_id)
                        GROUP BY st.stop_id
                    )
                    SELECT s.stop_id, s.stop_name, TRY_CAST(s.stop_lat AS DOUBLE) AS lat,
                           TRY_CAST(s.stop_lon AS DOUBLE) AS lon, rs.routes
                    FROM read_csv('{paths["stops.txt"]}', header=true, all_varchar=true) s
                    JOIN rs USING (stop_id)
                    """
                ).df()
                frames.append(df)
    finally:
        con.close()
    if not frames:
        return pd.DataFrame(columns=["stop_id", "stop_name", "lat", "lon", "routes"])
    all_stops = pd.concat(frames, ignore_index=True)
    # A stop shared by two feeds (e.g. NYCT and MTA Bus Company) keeps all its routes.
    merged = all_stops.groupby("stop_id", as_index=False).agg(
        stop_name=("stop_name", "first"),
        lat=("lat", "first"),
        lon=("lon", "first"),
        routes=("routes", lambda r: sorted({x for routes in r for x in routes})),
    )
    return merged.dropna(subset=["lat", "lon"]).reset_index(drop=True)


def refresh_bus_static(settings, now: dt.datetime | None = None, force: bool = False) -> bool:
    """Download the bus static GTFS (weekly) and rewrite the ``bus_stops`` reference.

    Returns True if the reference was rewritten. Failures keep the old reference.
    """
    from .static_gtfs import _download
    from .storage import read_reference, write_reference

    meta_path = Path(settings.data_dir) / "reference" / "bus_stops_meta.json"
    now = now or dt.datetime.now(dt.UTC)
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    if (
        not force
        and meta_path.exists()
        and not read_reference(settings.data_dir, "bus_stops").empty
    ):
        computed = pd.Timestamp(json.loads(meta_path.read_text())["refreshed_utc"])
        if (
            pd.Timestamp(now_naive) - computed
        ).total_seconds() < settings.bus_static_max_age_hours * 3600:
            return False
    target_dir = settings.resolved_static_dir / "bus"
    target_dir.mkdir(parents=True, exist_ok=True)
    zips = []
    for url in [u.strip() for u in settings.bus_static_urls.split(",") if u.strip()]:
        dest = target_dir / Path(url).name
        try:
            _download(url, dest, settings)
            zips.append(dest)
        except Exception as exc:  # noqa: BLE001 - one borough must not block the others
            log.warning("Bus static GTFS download failed for %s: %s", url, exc)
            if dest.exists():
                zips.append(dest)  # keep using the previous copy
    if not zips:
        return False
    stops = bus_stops_from_zips(zips)
    if stops.empty:
        return False
    write_reference(settings.data_dir, "bus_stops", stops)
    meta_path.write_text(
        json.dumps(
            {"refreshed_utc": pd.Timestamp(now_naive).isoformat(), "n_stops": len(stops)},
            indent=2,
        )
    )
    return True
