import json

import numpy as np
import pandas as pd
import pytest
from fakes import FakeSession, jpeg_bytes

from transit_ripple.cameras.client import CameraApiError
from transit_ripple.cameras.counting import (
    STATUS_FETCH_ERROR,
    STATUS_FROZEN,
    STATUS_INVALID,
    STATUS_OK,
    CameraCounter,
    CameraState,
    save_report,
)
from transit_ripple.cameras.detector import (
    COUNT_NAMES,
    Detector,
    InvalidImage,
    count_detections,
    decode_image,
    decode_outputs,
    letterbox,
    nms,
)
from transit_ripple.cameras.storage import connect, write_counts

# --- detector internals ----------------------------------------------------------------


def test_letterbox_keeps_aspect_ratio_and_pads():
    rgb = np.zeros((240, 352, 3), dtype=np.uint8)
    rgb[..., 0] = 200  # red in RGB
    tensor, ratio = letterbox(rgb, (64, 64))
    assert tensor.shape == (1, 3, 64, 64) and tensor.dtype == np.float32
    assert ratio == pytest.approx(64 / 352)
    nh = int(240 * ratio)
    assert tensor[0, 2, 0, 0] == 200 and tensor[0, 0, 0, 0] == 0  # BGR channel order
    assert (tensor[0, :, nh:, :] == 114).all()  # bottom padding


def test_decode_outputs_applies_grid_and_stride():
    size = (64, 64)
    raw = np.zeros((1, 84, 85), dtype=np.float32)
    raw[0, 9, :4] = [0.5, 0.5, 0.0, np.log(2.0)]  # anchor 9 = stride 8, cell (x=1, y=1)
    pred = decode_outputs(raw, size)
    assert pred[9, :4].tolist() == pytest.approx([12.0, 12.0, 8.0, 16.0])
    raw[0, 64, :2] = [0, 0]  # first stride-16 cell
    assert decode_outputs(raw, size)[64, 2] == pytest.approx(16.0)
    with pytest.raises(ValueError):
        decode_outputs(np.zeros((1, 10, 85)), size)


def test_nms_suppresses_overlaps_only():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=float)
    assert nms(boxes, np.array([0.9, 0.8, 0.7]), 0.45) == [0, 2]


def test_count_detections_is_class_aware_and_thresholded():
    def det(x, cls, score):
        row = np.zeros(85)
        row[:4] = [x, 10, 10, 10]
        row[4] = 1.0
        row[5 + cls] = score
        return row

    pred = np.stack(
        [
            det(10, 2, 0.9),
            det(11, 2, 0.8),  # two overlapping cars -> 1
            det(40, 2, 0.9),  # another car
            det(10, 7, 0.9),  # truck at the same place as a car: other class, kept
            det(70, 0, 0.2),  # person below the threshold
            det(90, 16, 0.95),  # dog: not counted
        ]
    )
    counts = count_detections(pred, 0.35, 0.45)
    assert counts == {"car": 2, "bus": 0, "truck": 1, "person": 0}


def test_decode_image_rejects_non_images():
    assert decode_image(jpeg_bytes()).shape == (240, 352, 3)
    with pytest.raises(InvalidImage):
        decode_image(b"<html>camera offline</html>")


def test_detector_outputs_four_integer_counts_only():
    session = FakeSession(64, objects=[(0, 2), (40, 2), (70, 5), (80, 0)])
    result = Detector(session, model_name="fake").count(jpeg_bytes())
    assert list(result.counts) == list(COUNT_NAMES)
    assert result.counts == {"car": 2, "bus": 1, "truck": 0, "person": 1}
    assert all(isinstance(v, int) for v in result.counts.values())
    assert (result.width, result.height) == (352, 240) and result.inference_ms >= 0
    assert not hasattr(result, "image") and not hasattr(result, "boxes")


# --- counting ------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads  # url -> bytes or Exception
        self.n_requests = self.n_retries = 0
        self.last_elapsed_ms = 42.0

    def get_bytes(self, url):
        self.n_requests += 1
        value = self.payloads[url]
        if isinstance(value, Exception):
            raise value
        return value


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def cams(*ids):
    return pd.DataFrame({"camera_id": list(ids), "image_url": [f"https://cams/{i}" for i in ids]})


def test_counter_rows_statuses_and_report(settings):
    payloads = {
        "https://cams/ok": jpeg_bytes(seed=1),
        "https://cams/down": CameraApiError("HTTP 503 after 4 attempts"),
        "https://cams/html": b"<html>offline</html>",
    }
    clock = Clock(1_793_500_000.0)
    counter = CameraCounter(
        settings, FakeClient(payloads), Detector(FakeSession(64, [(0, 2)])), clock=clock
    )
    rows, report = counter.count_cameras(cams("ok", "down", "html"), run_id="r1")
    by = rows.set_index("camera_id")
    assert by.loc["ok", "status"] == STATUS_OK and by.loc["ok", "car"] == 1
    assert by.loc["ok", "fetch_ms"] == 42.0
    assert by.loc["down", "status"] == STATUS_FETCH_ERROR and pd.isna(by.loc["down", "car"])
    assert by.loc["html", "status"] == STATUS_INVALID and pd.isna(by.loc["html", "person"])
    assert rows["bucket_utc"].iloc[0] == pd.Timestamp("2026-11-01 02:26:00")  # 02:26:40 UTC
    # Numbers only: no column holds image bytes.
    assert not any(rows[c].map(lambda v: isinstance(v, bytes | bytearray)).any() for c in rows)
    d = report.to_dict()
    assert d["overall_status"] == "degraded" and d["coverage"] == pytest.approx(1 / 3, abs=1e-4)
    assert d["status_counts"] == {"ok": 1, "fetch_error": 1, "invalid_image": 1}
    assert "camera down: fetch_error" in report.to_markdown()
    assert json.loads(save_report(settings.data_dir, report).read_text())["run_id"] == "r1"


def test_frozen_camera_gets_null_counts_after_the_threshold(settings):
    frame = jpeg_bytes(seed=2)
    clock = Clock(1_793_500_000.0)
    counter = CameraCounter(
        settings,
        FakeClient({"https://cams/x": frame}),
        Detector(FakeSession(64, [(0, 2)])),
        clock=clock,
    )
    first = counter.count_camera("x", "https://cams/x", "r")
    clock.t += 300
    repeat = counter.count_camera("x", "https://cams/x", "r")
    clock.t += 400  # identical frame for 700 s > 10 min
    frozen = counter.count_camera("x", "https://cams/x", "r")
    assert (first["status"], first["repeat_frame"]) == (STATUS_OK, False)
    assert (repeat["status"], repeat["repeat_frame"]) == (STATUS_OK, True)
    assert frozen["status"] == STATUS_FROZEN and frozen["car"] is None
    counter.state.save()
    saved = json.loads((settings.data_dir / "camera_state.json").read_text())
    assert set(saved["frames"]["x"]) == {"sha256", "since_ts"}  # a hash, never the image
    assert (
        CameraState(settings.data_dir / "camera_state.json").frames["x"]["since_ts"]
        == 1_793_500_000.0
    )


def test_counts_are_stored_and_queryable(settings):
    clock = Clock(1_793_500_000.0)
    counter = CameraCounter(
        settings,
        FakeClient({"https://cams/a": jpeg_bytes()}),
        Detector(FakeSession(64, [(3, 7)])),
        clock=clock,
    )
    rows, report = counter.count_cameras(cams("a"))
    write_counts(settings.data_dir, rows, report.run_id)
    con = connect(settings.data_dir)
    got = con.execute("SELECT camera_id, status, car, truck FROM camera_counts").fetchall()
    assert got == [("a", "ok", 0, 1)]
    assert list((settings.data_dir / "camera_counts").glob("date=2026-11-01/*.parquet"))
