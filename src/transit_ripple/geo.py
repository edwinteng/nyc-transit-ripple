"""Spatial join: traffic links, Citi Bike stations and bus stops near each subway station.

Distances use a local equirectangular projection around New York City, which is
accurate to well under 1% over a few kilometres.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EARTH_M_PER_DEG = 111_320.0
REF_LAT = 40.73  # projection reference latitude (Manhattan)

NEIGHBOR_COLUMNS = [
    "station_id",
    "station_name",
    "station_lat",
    "station_lon",
    "neighbor_type",
    "neighbor_id",
    "neighbor_name",
    "distance_m",
]


def project(lat, lon) -> tuple[np.ndarray, np.ndarray]:
    """Degrees -> metres (x east, y north) in the local projection."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    k = np.cos(np.radians(REF_LAT))
    return lon * EARTH_M_PER_DEG * k, lat * EARTH_M_PER_DEG


def haversine_m(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance in metres (vectorized)."""
    r = 6_371_008.8
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def point_to_polyline_m(px, py, xs, ys) -> np.ndarray:
    """Distance from points (px, py) to one polyline (xs, ys), all in metres.

    Returns one distance per point: the minimum over the polyline's segments.
    A single-vertex polyline is treated as a point.
    """
    px = np.asarray(px, dtype=float)[:, None]
    py = np.asarray(py, dtype=float)[:, None]
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if len(xs) == 1:
        return np.hypot(px[:, 0] - xs[0], py[:, 0] - ys[0])
    ax, ay, bx, by = xs[:-1], ys[:-1], xs[1:], ys[1:]
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t = np.where(seg_len2 > 0, np.clip(t, 0.0, 1.0), 0.0)
    cx, cy = ax + t * dx, ay + t * dy
    return np.min(np.hypot(px - cx, py - cy), axis=1)


def subway_stations(stops: pd.DataFrame) -> pd.DataFrame:
    """Parent stations (location_type 1) with coordinates from GTFS stops."""
    cols = ["station_id", "station_name", "lat", "lon"]
    if not {"stop_id", "stop_name", "stop_lat", "stop_lon"} <= set(stops.columns):
        return pd.DataFrame(columns=cols)
    df = stops.copy()
    if "location_type" in df.columns:
        lt = pd.to_numeric(df["location_type"], errors="coerce")
        parents = df[lt == 1]
        if not parents.empty:
            df = parents
    df = df.dropna(subset=["stop_lat", "stop_lon"])
    return df.rename(
        columns={
            "stop_id": "station_id",
            "stop_name": "station_name",
            "stop_lat": "lat",
            "stop_lon": "lon",
        }
    )[["station_id", "station_name", "lat", "lon"]].reset_index(drop=True)


def bike_neighbors(stations: pd.DataFrame, bikes: pd.DataFrame, radius_m: float) -> pd.DataFrame:
    """Citi Bike stations within ``radius_m`` of each subway station."""
    if stations.empty or bikes.empty:
        return pd.DataFrame(columns=NEIGHBOR_COLUMNS)
    d = haversine_m(
        stations["lat"].to_numpy()[:, None],
        stations["lon"].to_numpy()[:, None],
        bikes["lat"].to_numpy()[None, :],
        bikes["lon"].to_numpy()[None, :],
    )
    si, bi = np.nonzero(d <= radius_m)
    return pd.DataFrame(
        {
            "station_id": stations["station_id"].to_numpy()[si],
            "station_name": stations["station_name"].to_numpy()[si],
            "station_lat": stations["lat"].to_numpy()[si],
            "station_lon": stations["lon"].to_numpy()[si],
            "neighbor_type": "bike_station",
            "neighbor_id": bikes["station_id"].astype(str).to_numpy()[bi],
            "neighbor_name": bikes["name"].to_numpy()[bi],
            "distance_m": d[si, bi].round(1),
        },
        columns=NEIGHBOR_COLUMNS,
    )


def traffic_neighbors(stations: pd.DataFrame, links: pd.DataFrame, radius_m: float) -> pd.DataFrame:
    """Traffic links passing within ``radius_m`` of each subway station."""
    rows = []
    if stations.empty or links.empty:
        return pd.DataFrame(columns=NEIGHBOR_COLUMNS)
    px, py = project(stations["lat"], stations["lon"])
    for link in links.itertuples(index=False):
        lats, lons = list(link.lats), list(link.lons)
        if not lats:
            continue
        xs, ys = project(lats, lons)
        d = point_to_polyline_m(px, py, xs, ys)
        for i in np.nonzero(d <= radius_m)[0]:
            rows.append(
                {
                    "station_id": stations["station_id"].iat[i],
                    "station_name": stations["station_name"].iat[i],
                    "station_lat": stations["lat"].iat[i],
                    "station_lon": stations["lon"].iat[i],
                    "neighbor_type": "traffic_link",
                    "neighbor_id": str(link.link_id),
                    "neighbor_name": link.link_name,
                    "distance_m": round(float(d[i]), 1),
                }
            )
    return pd.DataFrame(rows, columns=NEIGHBOR_COLUMNS)


def bus_stop_neighbors(
    stations: pd.DataFrame, bus_stops: pd.DataFrame | None, radius_m: float
) -> pd.DataFrame:
    """Bus stops within ``radius_m`` of each subway station. Bus speed segments end at
    a stop, so a segment is "near" a station when its end stop is."""
    if bus_stops is None or stations.empty or bus_stops.empty:
        return pd.DataFrame(columns=NEIGHBOR_COLUMNS)
    renamed = bus_stops.rename(columns={"stop_id": "station_id", "stop_name": "name"})
    out = bike_neighbors(stations, renamed[["station_id", "name", "lat", "lon"]], radius_m)
    return out.assign(neighbor_type="bus_stop")


def station_neighbors(
    stations: pd.DataFrame,
    traffic_links: pd.DataFrame,
    bike_stations: pd.DataFrame,
    radius_m: float = 500.0,
    bus_stops: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """All neighbors of all subway stations, nearest first."""
    out = pd.concat(
        [
            traffic_neighbors(stations, traffic_links, radius_m),
            bike_neighbors(stations, bike_stations, radius_m),
            bus_stop_neighbors(stations, bus_stops, radius_m),
        ],
        ignore_index=True,
    )
    if out.empty:
        return pd.DataFrame(columns=NEIGHBOR_COLUMNS)
    return out.sort_values(["station_id", "neighbor_type", "distance_m"]).reset_index(drop=True)
