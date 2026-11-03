import datetime as dt

import numpy as np
import pandas as pd
import pytest

from transit_ripple.baselines import (
    CONFIDENCE_LOW,
    CONFIDENCE_NONE,
    CONFIDENCE_OK,
    POOLED_WEEKDAYS,
    POOLED_WEEKEND,
    compute_baselines,
    local_slot,
    lookup,
    refresh_baselines,
)
from transit_ripple.config import Settings
from transit_ripple.geo import (
    haversine_m,
    point_to_polyline_m,
    project,
    station_neighbors,
    subway_stations,
)
from transit_ripple.storage import connect, read_reference, write_table

# --- spatial join ------------------------------------------------------------------------


def test_haversine_and_projection_agree_at_city_scale():
    assert haversine_m(40.0, -74.0, 41.0, -74.0) == pytest.approx(111_195, rel=1e-3)
    x1, y1 = project(40.75, -73.99)
    x2, y2 = project(40.76, -73.98)
    planar = float(np.hypot(x2 - x1, y2 - y1))
    assert planar == pytest.approx(float(haversine_m(40.75, -73.99, 40.76, -73.98)), rel=0.005)


def test_point_to_polyline_distance():
    xs, ys = [0.0, 200.0, 200.0], [0.0, 0.0, 300.0]
    d = point_to_polyline_m([100, -50, 260, 200], [100, 0, 150, 400], xs, ys)
    assert d.tolist() == pytest.approx([100.0, 50.0, 60.0, 100.0])
    assert point_to_polyline_m([3], [4], [0.0], [0.0]).tolist() == [5.0]


def test_subway_stations_uses_parent_stations():
    stops = pd.DataFrame(
        {
            "stop_id": ["101", "101N", "101S", "A32"],
            "stop_name": ["Van Cortlandt", "Van Cortlandt", "Van Cortlandt", "W 4 St"],
            "stop_lat": [40.889, 40.889, 40.889, 40.732],
            "stop_lon": [-73.898, -73.898, -73.898, -74.000],
            "location_type": [1, None, None, 1],
            "parent_station": [None, "101", "101", None],
        }
    )
    assert subway_stations(stops)["station_id"].tolist() == ["101", "A32"]
    assert subway_stations(stops.drop(columns=["stop_lat"])).empty


def test_station_neighbors_within_radius_only():
    stations = pd.DataFrame(
        {"station_id": ["A32"], "station_name": ["W 4 St"], "lat": [40.7320], "lon": [-74.0000]}
    )
    # ~111 m and ~890 m north of the station.
    bikes = pd.DataFrame(
        {
            "station_id": ["near", "far"],
            "name": ["Near", "Far"],
            "lat": [40.7330, 40.7400],
            "lon": [-74.0, -74.0],
        }
    )
    # East-west link passing ~222 m south, and one ~2 km away.
    links = pd.DataFrame(
        {
            "link_id": ["L1", "L2", "L3"],
            "link_name": ["Close", "Distant", "No geometry"],
            "lats": [[40.7300, 40.7300], [40.7500, 40.7500], []],
            "lons": [[-74.01, -73.99], [-74.01, -73.99], []],
        }
    )
    nb = station_neighbors(stations, links, bikes, radius_m=500)
    got = {(r.neighbor_type, r.neighbor_id): r.distance_m for r in nb.itertuples()}
    assert set(got) == {("bike_station", "near"), ("traffic_link", "L1")}
    assert got[("bike_station", "near")] == pytest.approx(111, abs=2)
    assert got[("traffic_link", "L1")] == pytest.approx(222, abs=3)


# --- baselines ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 11, 30, 15, 0)  # Monday 10:00 EST


def _traffic_rows(link, local_days, speed, hour=9, minute=10):
    rows = []
    for day in local_days:
        # 09:10 EST = 14:10 UTC in November.
        t = pd.Timestamp(f"2026-11-{day:02d} {hour + 5:02d}:{minute:02d}:00")
        rows.append(
            {
                "bucket_utc": t,
                "observed_utc": t,
                "fetched_utc": t,
                "run_id": "r",
                "link_id": link,
                "speed_mph": speed,
                "is_valid": True,
                "age_s": 60.0,
            }
        )
    return rows


def test_local_slot_uses_new_york_time():
    s = local_slot(pd.Series(pd.to_datetime(["2026-11-30 14:10", "2026-11-02 04:59"])), 30)
    assert s["weekday"].tolist() == [0, 6]  # Monday 09:10 EST, Sunday 23:59 EST
    assert s["slot_start_min"].tolist() == [540, 1410]


def test_compute_baselines_by_weekday_and_slot_with_confidence(tmp_path):
    rows = (
        _traffic_rows("L1", [9, 16, 23], 20.0)  # three Mondays -> ok
        + _traffic_rows("L1", [16], 30.0, minute=20)  # same slot, second observation
        + _traffic_rows("L2", [23], 12.0)  # one Monday -> low confidence
        + _traffic_rows("L2", [24, 25], 18.0)  # Tue + Wed: pooled weekdays reach 3 days
        + _traffic_rows("L1", [30], 5.0)  # 50 min ago: excluded (most recent 3 h)
    )
    rows.append({**rows[0], "link_id": "L3", "is_valid": False, "speed_mph": 0.0})
    write_table(tmp_path, "traffic_minute", pd.DataFrame(rows), "r")
    settings = Settings(data_dir=tmp_path)
    all_b = compute_baselines(connect(tmp_path), settings, NOW)
    b = all_b[all_b["weekday"] >= 0].set_index(["metric", "entity_id", "weekday"])

    l1 = b.loc[("traffic_speed_mph", "L1", 0)]
    assert l1["slot_start_min"] == 540
    assert (l1["n_days"], l1["n_obs"]) == (3, 4)
    assert l1["mean"] == pytest.approx(22.5)
    assert l1["confidence"] == CONFIDENCE_OK
    assert b.loc[("traffic_speed_mph", "L2", 0), "confidence"] == CONFIDENCE_LOW
    assert "L3" not in set(all_b["entity_id"])  # invalid observations are ignored
    # Pooled: the same half-hour over all weekdays (code -1) gets L2 to 3 days.
    pooled = all_b[all_b["weekday"] == POOLED_WEEKDAYS].set_index("entity_id")
    assert (pooled.loc["L2", "n_days"], pooled.loc["L2", "confidence"]) == (3, CONFIDENCE_OK)
    assert pooled.loc["L2", "mean"] == pytest.approx(16.0)
    assert not (all_b["weekday"] == POOLED_WEEKEND).any()  # no weekend data


def test_refresh_baselines_skips_when_fresh_even_if_empty(tmp_path):
    settings = Settings(data_dir=tmp_path)
    changed, b = refresh_baselines(connect(tmp_path), settings, NOW)
    assert changed and b.empty
    changed, _ = refresh_baselines(connect(tmp_path), settings, NOW + dt.timedelta(hours=1))
    assert not changed


def test_refresh_baselines_skips_when_fresh(tmp_path):
    write_table(tmp_path, "traffic_minute", pd.DataFrame(_traffic_rows("L1", [9], 20.0)), "r")
    settings = Settings(data_dir=tmp_path)
    con = connect(tmp_path)
    changed, b = refresh_baselines(con, settings, NOW)
    assert changed and len(b) == 2  # the Monday slot and the pooled weekday slot
    changed, _ = refresh_baselines(con, settings, NOW + dt.timedelta(hours=2))
    assert not changed
    changed, _ = refresh_baselines(con, settings, NOW + dt.timedelta(hours=2), force=True)
    assert changed
    assert len(read_reference(tmp_path, "baselines")) == 2


def test_lookup_attaches_baseline_or_none():
    baselines = pd.DataFrame(
        {
            "metric": ["bikes_available"],
            "entity_id": ["s1"],
            "weekday": [0],
            "slot_start_min": [540],
            "mean": [12.0],
            "n_days": [4],
            "confidence": [CONFIDENCE_OK],
        }
    )
    grid = pd.DataFrame(
        {
            "minute_utc": pd.to_datetime(
                ["2026-11-30 14:15", "2026-11-30 14:15", "2026-11-30 15:00"]
            ),
            "entity_id": ["s1", "s2", "s1"],
        }
    )
    out = lookup(baselines, "bikes_available", grid, 30)
    assert out["baseline"].tolist()[0] == 12.0
    assert (
        pd.isna(out["baseline"].iloc[1]) and out["baseline_confidence"].iloc[1] == CONFIDENCE_NONE
    )
    assert pd.isna(out["baseline"].iloc[2])  # 10:00 slot has no baseline
    assert out["baseline_level"].tolist()[0] == "weekday"


def test_lookup_falls_back_to_pooled_baselines_with_enough_history():
    def row(weekday, mean, n_days, conf):
        return {
            "metric": "bikes_available",
            "entity_id": "s1",
            "weekday": weekday,
            "slot_start_min": 540,
            "mean": mean,
            "n_days": n_days,
            "confidence": conf,
        }

    grid = pd.DataFrame(
        {"minute_utc": pd.to_datetime(["2026-11-30 14:15"]), "entity_id": ["s1"]}
    )  # Monday 09:15
    low_exact = pd.DataFrame(
        [row(0, 12.0, 1, CONFIDENCE_LOW), row(POOLED_WEEKDAYS, 15.0, 4, CONFIDENCE_OK)]
    )
    out = lookup(low_exact, "bikes_available", grid, 30).iloc[0]
    assert (out["baseline"], out["baseline_level"], out["baseline_confidence"]) == (
        15.0,
        "pooled",
        CONFIDENCE_OK,
    )
    ok_exact = pd.DataFrame(
        [row(0, 12.0, 3, CONFIDENCE_OK), row(POOLED_WEEKDAYS, 15.0, 9, CONFIDENCE_OK)]
    )
    assert lookup(ok_exact, "bikes_available", grid, 30).iloc[0]["baseline"] == 12.0
    only_pooled_low = pd.DataFrame([row(POOLED_WEEKDAYS, 15.0, 2, CONFIDENCE_LOW)])
    out = lookup(only_pooled_low, "bikes_available", grid, 30).iloc[0]
    assert (out["baseline"], out["baseline_confidence"]) == (15.0, CONFIDENCE_LOW)
    weekend = pd.DataFrame([row(POOLED_WEEKEND, 9.0, 5, CONFIDENCE_OK)])
    assert pd.isna(lookup(weekend, "bikes_available", grid, 30).iloc[0]["baseline"])
