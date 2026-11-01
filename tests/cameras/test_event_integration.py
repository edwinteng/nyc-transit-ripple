"""M4: cameras counted near the stations of ongoing delay events, with a hard cap."""

import datetime as dt
import json

import pandas as pd
import pytest
from fakes import FakeSession, jpeg_bytes

from transit_ripple.cameras import count_near_stations
from transit_ripple.cameras.counting import CameraCounter
from transit_ripple.cameras.detector import Detector
from transit_ripple.cameras.interface import select_cameras
from transit_ripple.cameras.storage import write_reference
from transit_ripple.config import Settings
from transit_ripple.integration import EventCameraHook, save_events_report

NOW = dt.datetime(2026, 11, 1, 2, 30)


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.n_requests = self.n_retries = 0
        self.last_elapsed_ms = 50.0
        self.urls = []

    def get_bytes(self, url):
        self.n_requests += 1
        self.urls.append(url)
        return self.payloads[url]


def near_frame():
    """A41 has c1 (60 m) and c2 (450 m); A42 has c2 (100 m) and c3 (300 m)."""
    return pd.DataFrame(
        {
            "station_id": ["A41", "A41", "A42", "A42"],
            "station_name": ["Jay St", "Jay St", "Hoyt St", "Hoyt St"],
            "station_lat": [40.69, 40.69, 40.68, 40.68],
            "station_lon": [-73.99, -73.99, -73.98, -73.98],
            "camera_id": ["c1", "c2", "c2", "c3"],
            "camera_name": ["Adams St", "Tillary St", "Tillary St", "Far Away Rd"],
            "distance_m": [60.0, 450.0, 100.0, 300.0],
        }
    )


def setup_mapping(settings, cameras):
    write_reference(settings.data_dir, "cameras", cameras)
    write_reference(settings.data_dir, "camera_station_map", near_frame())


def make_factory(cameras, calls):
    payloads = {u: jpeg_bytes(seed=i) for i, u in enumerate(cameras["image_url"])}
    t = NOW.replace(tzinfo=dt.UTC).timestamp()

    def factory(s):
        calls.append(s)
        client = FakeClient(payloads)
        return CameraCounter(
            s, client, Detector(FakeSession(64, [(0, 2), (5, 0)])), clock=lambda: t
        )

    return factory


def test_select_cameras_round_robin_dedupes_and_caps(cameras):
    near = near_frame().merge(cameras[["camera_id", "image_url"]], on="camera_id")
    # Round 1: nearest camera of each station (c1 for A41, c2 for A42), then round 2.
    assert select_cameras(near, ["A41", "A42"], None)["camera_id"].tolist() == ["c1", "c2", "c3"]
    assert select_cameras(near, ["A42", "A41"], 2)["camera_id"].tolist() == ["c2", "c1"]
    assert select_cameras(near, ["A41", "A42"], 0).empty


def test_count_near_stations_counts_each_camera_once_and_marks_the_cap(settings, cameras):
    setup_mapping(settings, cameras)
    calls = []
    df = count_near_stations(
        ["A41", "A42"],
        settings=settings,
        counter_factory=make_factory(cameras, calls),
        now=NOW,
        max_cameras=2,
    )
    # One row per station-camera pair; c2 is near both stations but counted once.
    assert len(df) == 4
    by_cam = df.drop_duplicates("camera_id").set_index("camera_id")
    assert by_cam.loc["c1", "status"] == "ok" and by_cam.loc["c2", "status"] == "ok"
    assert by_cam.loc["c3", "status"] == "skipped_cap" and pd.isna(by_cam.loc["c3", "car"])
    stored = pd.concat(
        pd.read_parquet(p) for p in (settings.data_dir / "camera_counts").rglob("*.parquet")
    )
    assert sorted(stored["camera_id"]) == ["c1", "c2"]  # numbers only, each camera once


def test_hook_shares_one_cap_and_one_counter_across_events(tmp_path, settings, cameras):
    setup_mapping(settings, cameras)
    calls = []
    hook = EventCameraHook(
        Settings(data_dir=settings.data_dir, event_camera_max_per_run=2),
        now=NOW.replace(tzinfo=dt.UTC),
        counter_factory=make_factory(cameras, calls),
    )
    first = hook("A-1", ["A41"])  # c1, c2
    second = hook("C-1", ["A42"])  # cap reached: no request at all
    assert first["cameras_counted"] == 2 and first["statuses"] == {"ok": 2}
    assert second is None
    assert len(calls) == 1  # detector and client built once per run
    report = hook.report()
    assert report["cap"] == 2 and report["cameras_counted"] == 2
    assert report["events"][1] == {"event_id": "C-1", "skipped": "camera cap reached"}
    path = save_events_report(tmp_path, "run1", NOW, report)
    assert json.loads(path.read_text())["status_counts"] == {"ok": 2}


def test_hook_failures_never_break_event_processing(settings, cameras):
    setup_mapping(settings, cameras)

    def broken(_settings):
        raise ImportError("onnxruntime is not installed")

    hook = EventCameraHook(
        Settings(data_dir=settings.data_dir), now=NOW.replace(tzinfo=dt.UTC), counter_factory=broken
    )
    assert hook("A-1", ["A41"]) is None
    assert "ImportError" in hook.report()["errors"][0]
    # Stations without a nearby camera: nothing is counted and nothing fails.
    assert hook("G-1", ["G22"])["cameras_counted"] == 0


@pytest.mark.parametrize("cap", [0, -1])
def test_non_positive_cap_disables_requests(settings, cameras, cap):
    setup_mapping(settings, cameras)
    hook = EventCameraHook(
        Settings(data_dir=settings.data_dir, event_camera_max_per_run=cap),
        now=NOW.replace(tzinfo=dt.UTC),
        counter_factory=lambda s: pytest.fail("must not count"),
    )
    assert hook("A-1", ["A41"]) is None
