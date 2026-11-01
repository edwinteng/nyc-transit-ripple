import datetime as dt
import json

import pandas as pd
import pytest
import requests
from fakes import Clock, FakeHttp, FakeResponse

from transit_ripple.cameras.client import CameraApiError, PoliteClient
from transit_ripple.cameras.config import CameraSettings
from transit_ripple.cameras.inventory import (
    InventoryError,
    inventory_age_days,
    parse_inventory,
    refresh_inventory,
)
from transit_ripple.cameras.mapping import build_mapping, map_cameras, parent_stations
from transit_ripple.cameras.storage import read_reference, write_reference

NOW = dt.datetime(2026, 11, 1, 2, 30)


def client(settings, responses):
    clock = Clock()
    http = FakeHttp(responses)
    return PoliteClient(settings, session=http, clock=clock, sleep=clock.sleep), http, clock


# --- polite client ------------------------------------------------------------------------


def test_client_limits_request_rate(settings):
    c, http, clock = client(settings, [FakeResponse(200, b"x")])
    for _ in range(3):
        assert c.get_bytes("https://cams/x") == b"x"
    assert clock.sleeps == [1.0, 1.0]  # at most one request per second
    assert c.n_requests == 3 and c.n_retries == 0


def test_client_retries_with_exponential_backoff_then_succeeds(settings):
    c, http, clock = client(
        settings,
        [FakeResponse(503), requests.ConnectionError(), FakeResponse(200, b"ok")],
    )
    assert c.get_bytes("https://cams/x") == b"ok"
    # Backoff 2 s then 4 s; the rate limit adds nothing because backoff is longer.
    assert clock.sleeps == [2.0, 4.0]
    assert c.n_retries == 2 and len(http.calls) == 3


def test_client_honors_retry_after_and_caps_backoff(tmp_path):
    s = CameraSettings(data_dir=tmp_path, backoff_s=2.0, max_backoff_s=5.0, max_retries=2)
    c, _, clock = client(
        s, [FakeResponse(429, headers={"Retry-After": "30"}), FakeResponse(200, b"ok")]
    )
    c.get_bytes("https://cams/x")
    assert clock.sleeps == [5.0]  # Retry-After 30 s, capped at max_backoff_s


def test_client_gives_up_after_max_retries_and_does_not_retry_client_errors(settings):
    c, http, _ = client(settings, [FakeResponse(500)])
    with pytest.raises(CameraApiError, match="HTTP 500 after 4 attempts"):
        c.get_bytes("https://cams/x")
    assert len(http.calls) == 4
    c, http, _ = client(settings, [FakeResponse(404)])
    with pytest.raises(CameraApiError, match="HTTP 404"):
        c.get_bytes("https://cams/x")
    assert len(http.calls) == 1


# --- inventory -------------------------------------------------------------------------------


def api_rows():
    return [
        {
            "id": "a",
            "name": "QBB @ Crescent St",
            "latitude": 40.75,
            "longitude": -73.94,
            "area": "Queens",
            "isOnline": "true",
            "imageUrl": "https://webcams.nyctmc.org/api/cameras/a/image",
        },
        {
            "id": "b",
            "name": "  ",
            "latitude": "40.70",
            "longitude": "-73.99",
            "area": "Brooklyn",
            "isOnline": False,
        },
        {"id": "c", "name": "Somewhere else", "latitude": 51.5, "longitude": -0.12},  # not NYC
        {"name": "no id", "latitude": 40.7, "longitude": -73.9},
        {"id": "a", "name": "QBB @ Crescent St (dup)", "latitude": 40.75, "longitude": -73.94},
    ]


def test_parse_inventory_normalizes_and_drops_invalid_rows():
    df, invalid = parse_inventory(json.dumps(api_rows()).encode(), pd.Timestamp(NOW), "https://api")
    assert invalid == 2
    assert df["camera_id"].tolist() == ["a", "b"]
    b = df.set_index("camera_id").loc["b"]
    assert pd.isna(b["name"]) and b["is_online"] is False and b["lat"] == 40.70
    assert b["image_url"] == "https://api/cameras/b/image"  # default URL pattern
    assert df.set_index("camera_id").loc["a", "name"] == "QBB @ Crescent St (dup)"  # last wins
    with pytest.raises(InventoryError):
        parse_inventory(b"{not json", pd.Timestamp(NOW), "https://api")
    with pytest.raises(InventoryError):
        parse_inventory(b'{"cameras": []}', pd.Timestamp(NOW), "https://api")


def test_inventory_refreshes_weekly_and_falls_back_to_stored(settings):
    c, http, _ = client(settings, [FakeResponse(200, json.dumps(api_rows()).encode())])
    first = refresh_inventory(settings, c, NOW)
    assert first.refreshed and len(first.cameras) == 2
    assert read_reference(settings.data_dir, "cameras")["camera_id"].tolist() == ["a", "b"]

    later = refresh_inventory(settings, c, NOW + dt.timedelta(days=3))
    assert not later.refreshed and later.age_days == pytest.approx(3.0)
    assert len(http.calls) == 1  # no request: the stored list is younger than a week

    c2, _, _ = client(settings, [FakeResponse(503)])
    stale = refresh_inventory(settings, c2, NOW + dt.timedelta(days=8))
    assert not stale.refreshed and "HTTP 503" in stale.error and len(stale.cameras) == 2
    assert inventory_age_days(settings.data_dir, NOW + dt.timedelta(days=8)) == pytest.approx(8.0)


def test_inventory_without_stored_copy_raises_on_failure(settings):
    c, _, _ = client(settings, [FakeResponse(503)])
    with pytest.raises(CameraApiError):
        refresh_inventory(settings, c, NOW)


# --- station mapping ---------------------------------------------------------------------------


def stops():
    return pd.DataFrame(
        {
            "stop_id": ["A41", "A41N", "A41S", "G05"],
            "stop_name": [
                "Jay St-MetroTech",
                "Jay St-MetroTech",
                "Jay St-MetroTech",
                "Jamaica Center",
            ],
            "stop_lat": [40.692338, 40.692338, 40.692338, 40.702147],
            "stop_lon": [-73.987342, -73.987342, -73.987342, -73.801109],
            "location_type": [1, None, None, 1],
            "parent_station": [None, "A41", "A41", None],
        }
    )


def test_parent_stations_only():
    st = parent_stations(stops())
    assert st["station_id"].tolist() == ["A41", "G05"]
    assert parent_stations(stops().drop(columns=["stop_lat"])).empty


def test_map_cameras_within_radius_nearest_first(cameras):
    m = map_cameras(parent_stations(stops()), cameras, radius_m=500)
    assert m["station_id"].unique().tolist() == ["A41"]  # Jamaica has no camera nearby
    assert m["camera_id"].tolist() == ["c1", "c2"]  # c3 is several km away
    assert m["distance_m"].iloc[0] < m["distance_m"].iloc[1] < 500
    # 18 m north-south and 55.5 m east-west of the station entrance coordinates.
    assert m["distance_m"].iloc[0] == pytest.approx(58.3, abs=0.5)
    assert map_cameras(parent_stations(stops()), cameras, radius_m=50).empty


def test_build_mapping_stores_reference(settings, cameras):
    write_reference(settings.data_dir, "cameras", cameras)
    m = build_mapping(settings.data_dir, stops(), cameras, 500)
    stored = read_reference(settings.data_dir, "camera_station_map")
    assert stored["camera_id"].tolist() == m["camera_id"].tolist() == ["c1", "c2"]
    assert set(stored.columns) >= {"station_id", "station_name", "camera_id", "distance_m"}
