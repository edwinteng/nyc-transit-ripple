import datetime as dt
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from fakes import FakeSession, jpeg_bytes

from transit_ripple.cameras import count_near_station, summarize_station_counts
from transit_ripple.cameras.benchmark import (
    benchmark_markdown,
    cameras_per_run,
    run_benchmark,
    synthetic_frames,
)
from transit_ripple.cameras.cli import main
from transit_ripple.cameras.client import CameraApiError
from transit_ripple.cameras.config import CameraSettings
from transit_ripple.cameras.counting import CameraCounter
from transit_ripple.cameras.detector import Detector, decode_image
from transit_ripple.cameras.evaluation import (
    LABEL_COLUMNS,
    choose_sample,
    error_metrics,
    evaluate,
    load_labels,
    sample_frames,
)
from transit_ripple.cameras.model import ModelError, ModelSpec, ensure_model
from transit_ripple.cameras.storage import write_counts, write_reference

NOW = dt.datetime(2026, 11, 1, 2, 30)


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.n_requests = self.n_retries = 0
        self.last_elapsed_ms = 50.0

    def get_bytes(self, url):
        self.n_requests += 1
        value = self.payloads.get(url, CameraApiError("HTTP 404"))
        if isinstance(value, Exception):
            raise value
        return value


def setup_station(settings, cameras):
    write_reference(settings.data_dir, "cameras", cameras)
    write_reference(
        settings.data_dir,
        "camera_station_map",
        pd.DataFrame(
            {
                "station_id": ["A41", "A41"],
                "station_name": ["Jay St-MetroTech"] * 2,
                "station_lat": [40.69] * 2,
                "station_lon": [-73.99] * 2,
                "camera_id": ["c1", "c2"],
                "camera_name": ["Adams St", "Tillary St"],
                "distance_m": [58.3, 450.0],
            }
        ),
    )


# --- count_near_station -----------------------------------------------------------------------


def test_count_near_station_live_counts_and_stores(settings, cameras):
    setup_station(settings, cameras)
    payloads = {
        "https://cams/c1/image": jpeg_bytes(seed=1),
        "https://cams/c2/image": CameraApiError("HTTP 503 after 4 attempts"),
    }
    t = NOW.replace(tzinfo=dt.UTC).timestamp()

    def factory(s):
        return CameraCounter(
            s, FakeClient(payloads), Detector(FakeSession(64, [(0, 2), (5, 0)])), clock=lambda: t
        )

    df = count_near_station("A41", settings=settings, counter_factory=factory, now=NOW)
    assert df["camera_id"].tolist() == ["c1", "c2"]  # nearest first
    c1, c2 = df.to_dict("records")
    assert (c1["source"], c1["status"], c1["car"], c1["person"], c1["age_s"]) == (
        "live",
        "ok",
        1,
        1,
        0.0,
    )
    assert c2["status"] == "fetch_error" and pd.isna(c2["car"])  # failures are NULL, never 0
    assert summarize_station_counts(df) == {
        "n_cameras": 2,
        "n_cameras_counted": 1,
        "car": 1,
        "bus": 0,
        "truck": 0,
        "person": 1,
    }

    # Ten minutes later, asking about that moment is answered from storage.
    past = count_near_station(
        "A41",
        NOW,
        settings=settings,
        now=NOW + dt.timedelta(minutes=10),
        counter_factory=lambda s: pytest.fail("must not count live"),
    )
    assert past["source"].tolist() == ["stored", "stored"]
    assert past.loc[0, "car"] == 1 and past.loc[1, "status"] == "fetch_error"


def test_count_near_station_without_data_reports_missing(settings, cameras):
    setup_station(settings, cameras)
    df = count_near_station("A41", NOW - dt.timedelta(days=1), settings=settings, now=NOW)
    assert df["status"].tolist() == ["missing", "missing"] and df["car"].isna().all()
    assert summarize_station_counts(df)["car"] is None
    assert count_near_station("G05", NOW - dt.timedelta(days=1), settings=settings, now=NOW).empty


def test_counter_report_when_every_fetch_fails(settings, cameras):
    counter = CameraCounter(
        settings, FakeClient({}), Detector(FakeSession(64)), clock=lambda: 1.8e9
    )
    rows, report = counter.count_cameras(cameras)
    assert set(rows["status"]) == {"fetch_error"} and report.overall_status() == "failed"
    assert report.to_dict()["fetch_ms_p50"] is None


def test_count_near_station_needs_a_mapping(settings):
    with pytest.raises(ValueError, match="run .*map"):
        count_near_station("A41", settings=settings, now=NOW)


# --- labeling and evaluation --------------------------------------------------------------------


def test_choose_sample_is_stratified_and_deterministic(cameras):
    many = pd.concat([cameras.assign(camera_id=cameras["camera_id"] + f"_{i}") for i in range(10)])
    a = choose_sample(many, 6, seed=3)
    assert a["camera_id"].tolist() == choose_sample(many, 6, seed=3)["camera_id"].tolist()
    assert len(a) == 6 and set(a["area"]) == {"Brooklyn", "Queens"}


def test_sample_frames_writes_local_frames_and_empty_label_csv(tmp_path, cameras):
    client = FakeClient(
        {
            "https://cams/c1/image": jpeg_bytes(seed=1),
            "https://cams/c2/image": b"not an image",
            "https://cams/c3/image": jpeg_bytes(seed=3),
        }
    )
    labels = sample_frames(client, cameras, 3, tmp_path / "labeling", seed=0, now=lambda: NOW)
    df = pd.read_csv(labels)
    assert list(df.columns) == LABEL_COLUMNS
    assert len(df) == 2  # the invalid payload is skipped
    assert df[["car", "bus", "truck", "person"]].isna().all().all()  # left for a person to fill
    for f in df["file"]:
        assert decode_image((tmp_path / "labeling" / f).read_bytes()).shape == (240, 352, 3)
    assert "do not commit" in (tmp_path / "labeling" / "README.txt").read_text().lower()


def test_load_labels_and_error_metrics(tmp_path):
    p = tmp_path / "labels.csv"
    pd.DataFrame(
        [
            {
                "sample_id": "s001",
                "file": "frames/s001.jpg",
                "car": 3,
                "bus": 0,
                "truck": 1,
                "person": 2,
            },
            {
                "sample_id": "s002",
                "file": "frames/s002.jpg",
                "car": 5,
                "bus": 1,
                "truck": 0,
                "person": 0,
            },
            {
                "sample_id": "s003",
                "file": "frames/s003.jpg",
                "car": None,
                "bus": 0,
                "truck": 0,
                "person": 0,
            },
        ]
    ).to_csv(p, index=False)
    labels = load_labels(p)
    assert labels["sample_id"].tolist() == ["s001", "s002"]  # incomplete row skipped
    pred = pd.DataFrame({"car": [2, 7], "bus": [0, 1], "truck": [1, 0], "person": [0, 0]})
    m = error_metrics(labels, pred).set_index("class")
    assert m.loc["car", "mae"] == 1.5 and m.loc["car", "bias"] == 0.5
    assert m.loc["car", "rmse"] == pytest.approx(1.581, abs=1e-3)
    assert m.loc["bus", "exact_share"] == 1.0
    assert m.loc["person", "bias"] == -1.0 and m.loc["person", "true_total"] == 2

    bad = tmp_path / "bad.csv"
    pd.DataFrame(
        [{"sample_id": "x", "file": "f", "car": -1, "bus": 0, "truck": 0, "person": 0}]
    ).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="non-negative integers"):
        load_labels(bad)


def test_evaluate_scores_each_model(tmp_path):
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "s001.jpg").write_bytes(jpeg_bytes())
    pd.DataFrame(
        [
            {
                "sample_id": "s001",
                "file": "frames/s001.jpg",
                "car": 2,
                "bus": 0,
                "truck": 0,
                "person": 1,
            }
        ]
    ).to_csv(tmp_path / "labels.csv", index=False)
    detectors = {
        "good": Detector(FakeSession(64, [(0, 2), (40, 2), (60, 0)])),
        "blind": Detector(FakeSession(64, [])),
    }
    results, preds = evaluate(tmp_path / "labels.csv", detectors)
    assert results["good"]["mae_mean"] == 0.0
    assert results["blind"]["mae_mean"] == pytest.approx(0.75)
    assert set(preds["model"]) == {"good", "blind"}


# --- model cache, benchmark, settings, CLI -------------------------------------------------------


def test_ensure_model_verifies_checksum_and_caches(tmp_path):
    payload = b"fake onnx weights"
    spec = ModelSpec(
        "tiny_test",
        "https://example/x.onnx",
        hashlib.sha256(payload).hexdigest(),
        (64, 64),
        0.1,
        1.0,
    )
    calls = []

    def download(url, target):
        calls.append(url)
        Path(target).write_bytes(payload)

    path = ensure_model(spec, tmp_path, download)
    assert path.read_bytes() == payload and path.name.startswith("tiny_test-")
    assert ensure_model(spec, tmp_path, download) == path and len(calls) == 1  # cached
    bad = ModelSpec("bad", "u", "0" * 64, (64, 64), 0.1, 1.0)
    with pytest.raises(ModelError, match="checksum"):
        ensure_model(bad, tmp_path, download)
    assert not list(tmp_path.glob("*.download"))


def test_cameras_per_run_is_bounded_by_the_polite_rate():
    assert cameras_per_run(240, 1.0, fetch_ms=300, processing_ms=80) == 240
    assert cameras_per_run(240, 1.0, fetch_ms=1500, processing_ms=500) == 120
    assert cameras_per_run(240, 0.0, fetch_ms=0, processing_ms=0) == 0


def test_benchmark_on_synthetic_frames():
    frames = synthetic_frames(3)
    assert all(decode_image(f).shape == (240, 352, 3) for f in frames)
    r = run_benchmark(
        Detector(FakeSession(64), model_name="fake"), CameraSettings(), n_frames=4, budget_s=60
    )
    assert r["synthetic"]["end_to_end_ms"]["n"] == 4 and r["cameras_per_run_polite"] == 60
    assert "Cameras per 60 s run" in benchmark_markdown(r)


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPPLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RIPPLE_MODEL_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("RIPPLE_CAMERA_REQUEST_INTERVAL_S", "2.5")
    monkeypatch.setenv("RIPPLE_CAMERA_MODEL", "yolox_nano")
    s = CameraSettings.from_env()
    assert (s.data_dir, s.request_interval_s, s.model) == (tmp_path, 2.5, "yolox_nano")
    assert s.resolved_model_cache_dir == tmp_path / "models"
    monkeypatch.setenv("RIPPLE_CAMERA_MAX_RETRIES", "many")
    with pytest.raises(ValueError, match="max_retries"):
        CameraSettings.from_env()


def test_cli_near_answers_from_stored_counts(tmp_path, capsys, monkeypatch, cameras):
    monkeypatch.chdir(tmp_path)
    settings = CameraSettings(data_dir=tmp_path / "data")
    setup_station(settings, cameras)
    t = pd.Timestamp("2026-10-01 12:30:10")  # far from "now": answered from storage
    write_counts(
        settings.data_dir,
        pd.DataFrame(
            [
                {
                    "bucket_utc": t.floor("min"),
                    "observed_utc": t,
                    "run_id": "r",
                    "camera_id": "c1",
                    "status": "ok",
                    "car": 4,
                    "bus": 0,
                    "truck": 1,
                    "person": 2,
                }
            ]
        ),
        "r",
    )
    code = main(
        ["near", "A41", "--data-dir", str(settings.data_dir), "--at", "2026-10-01T12:31:00+00:00"]
    )
    assert code == 0
    out = capsys.readouterr().out
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary == {
        "n_cameras": 2,
        "n_cameras_counted": 1,
        "car": 4,
        "bus": 0,
        "truck": 1,
        "person": 2,
    }
    assert main(["count", "--data-dir", str(tmp_path / "empty"), "--model", "nope"]) == 2


def test_cli_inventory_map_count_flow_offline(tmp_path, capsys, monkeypatch):
    """The camera workflow's sequence with a fake HTTP client and detector."""
    import transit_ripple.cameras.client as client_mod
    import transit_ripple.cameras.detector as detector_mod
    import transit_ripple.cameras.mapping as mapping_mod

    rows = [
        {
            "id": "c1",
            "name": "Adams St @ Willoughby St",
            "latitude": 40.6925,
            "longitude": -73.9880,
            "area": "Brooklyn",
            "isOnline": "true",
            "imageUrl": "https://cams/c1/image",
        },
        {
            "id": "c2",
            "name": "Far Away Rd",
            "latitude": 40.75,
            "longitude": -73.80,
            "area": "Queens",
            "isOnline": "true",
            "imageUrl": "https://cams/c2/image",
        },
    ]
    payloads = {
        "https://webcams.nyctmc.org/api/cameras": json.dumps(rows).encode(),
        "https://cams/c1/image": jpeg_bytes(seed=4),
    }

    class StubClient(FakeClient):
        def __init__(self, settings):
            super().__init__(payloads)

    monkeypatch.setattr(client_mod, "PoliteClient", StubClient)
    monkeypatch.setattr(
        detector_mod,
        "load_detector",
        lambda s: Detector(FakeSession(64, [(0, 2), (30, 5)]), model_name=s.model),
    )
    monkeypatch.setattr(
        mapping_mod,
        "load_stops",
        lambda m1: pd.DataFrame(
            {
                "stop_id": ["A41"],
                "stop_name": ["Jay St-MetroTech"],
                "stop_lat": [40.692338],
                "stop_lon": [-73.987342],
                "location_type": [1],
            }
        ),
    )
    data = ["--data-dir", str(tmp_path / "data")]

    assert main(["inventory", *data]) == 0
    assert "2 cameras, refreshed" in capsys.readouterr().out
    assert main(["map", *data]) == 0
    assert "1 station-camera pairs within 500 m; 1 stations" in capsys.readouterr().out
    report_md = tmp_path / "report.md"
    assert main(["count", *data, "--report-md", str(report_md)]) == 0
    out = capsys.readouterr().out
    assert "Overall status: **ok**" in out and "Cameras: 1 (ok: 1)" in out
    from transit_ripple.cameras.storage import connect

    got = (
        connect(tmp_path / "data")
        .execute("SELECT camera_id, car, bus FROM camera_counts")
        .fetchall()
    )
    assert got == [("c1", 1, 1)]
    assert report_md.read_text().startswith("## Camera counting report")
