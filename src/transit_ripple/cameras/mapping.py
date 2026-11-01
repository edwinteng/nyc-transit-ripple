"""Cameras within a radius (500 m by default) of every subway station.

Stations are the parent stations (``location_type = 1``) of the GTFS static
schedule loaded by M1. The result is stored as ``reference/camera_station_map``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .storage import write_reference

EARTH_RADIUS_M = 6_371_008.8
MAP_COLUMNS = [
    "station_id",
    "station_name",
    "station_lat",
    "station_lon",
    "camera_id",
    "camera_name",
    "distance_m",
]


def haversine_m(lat1, lon1, lat2, lon2) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def parent_stations(stops: pd.DataFrame) -> pd.DataFrame:
    """Subway stations (GTFS parent stations) with coordinates."""
    cols = ["station_id", "station_name", "lat", "lon"]
    if not {"stop_id", "stop_name", "stop_lat", "stop_lon"} <= set(stops.columns):
        return pd.DataFrame(columns=cols)
    df = stops
    if "location_type" in df.columns:
        parents = df[pd.to_numeric(df["location_type"], errors="coerce") == 1]
        if not parents.empty:
            df = parents
    df = df.dropna(subset=["stop_lat", "stop_lon"])
    out = df.rename(
        columns={
            "stop_id": "station_id",
            "stop_name": "station_name",
            "stop_lat": "lat",
            "stop_lon": "lon",
        }
    )[cols]
    return out.astype({"lat": float, "lon": float}).reset_index(drop=True)


def map_cameras(
    stations: pd.DataFrame, cameras: pd.DataFrame, radius_m: float = 500.0
) -> pd.DataFrame:
    """All (station, camera) pairs closer than ``radius_m``, nearest camera first."""
    if stations.empty or cameras.empty:
        return pd.DataFrame(columns=MAP_COLUMNS)
    d = haversine_m(
        stations["lat"].to_numpy()[:, None],
        stations["lon"].to_numpy()[:, None],
        cameras["lat"].to_numpy()[None, :],
        cameras["lon"].to_numpy()[None, :],
    )
    si, ci = np.nonzero(d <= radius_m)
    out = pd.DataFrame(
        {
            "station_id": stations["station_id"].to_numpy()[si],
            "station_name": stations["station_name"].to_numpy()[si],
            "station_lat": stations["lat"].to_numpy()[si],
            "station_lon": stations["lon"].to_numpy()[si],
            "camera_id": cameras["camera_id"].to_numpy()[ci],
            "camera_name": cameras["name"].to_numpy()[ci],
            "distance_m": d[si, ci].round(1),
        },
        columns=MAP_COLUMNS,
    )
    return out.sort_values(["station_id", "distance_m", "camera_id"]).reset_index(drop=True)


def build_mapping(
    data_dir, stops: pd.DataFrame, cameras: pd.DataFrame, radius_m: float
) -> pd.DataFrame:
    mapping = map_cameras(parent_stations(stops), cameras, radius_m)
    write_reference(data_dir, "camera_station_map", mapping)
    return mapping


def load_stops(m1_settings) -> pd.DataFrame:
    """GTFS stops from the M1 static schedule cache (downloaded if needed)."""
    from ..static_gtfs import ensure_static

    ensure_static(m1_settings)
    return pd.read_parquet(m1_settings.resolved_static_dir / "parquet" / "stops.parquet")
