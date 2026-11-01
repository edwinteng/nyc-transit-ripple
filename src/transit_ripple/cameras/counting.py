"""Fetch camera frames politely and turn them into counts (numbers only).

Per camera and fetch the status is one of:

- ``ok``: counts are valid;
- ``fetch_error``: the frame could not be downloaded (after retries);
- ``invalid_image``: the payload was not a decodable image;
- ``frozen``: the camera has returned the identical frame for at least
  ``frozen_after_min`` minutes, so its counts are NULL (never reported as current).

Counts are NULL for every status except ``ok``. Frames are never written to disk;
only a SHA-256 of the last frame per camera is kept (in ``camera_state.json``) to
detect frozen cameras.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .client import CameraApiError, PoliteClient
from .config import CameraSettings
from .detector import COUNT_NAMES, Detector, InvalidImage

STATUS_OK = "ok"
STATUS_FETCH_ERROR = "fetch_error"
STATUS_INVALID = "invalid_image"
STATUS_FROZEN = "frozen"


def make_run_id(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y%m%dT%H%M%SZ")


class CameraState:
    """Last frame hash per camera, persisted between runs (no image content)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.frames: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.frames = json.loads(self.path.read_text()).get("frames", {})
            except (OSError, ValueError):
                self.frames = {}

    def observe(self, camera_id: str, digest: str, now: float) -> tuple[bool, float]:
        """Record a frame hash. Returns (same_as_before, seconds the frame has not changed)."""
        prev = self.frames.get(camera_id)
        if prev and prev.get("sha256") == digest:
            return True, now - float(prev["since_ts"])
        self.frames[camera_id] = {"sha256": digest, "since_ts": now}
        return False, 0.0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "frames": self.frames}, separators=(",", ":")))
        os.replace(tmp, self.path)


@dataclass
class CameraRunReport:
    run_id: str
    started_ts: float
    model: str
    score_threshold: float
    request_interval_s: float
    finished_ts: float | None = None
    n_cameras: int = 0
    status_counts: dict = field(default_factory=dict)
    fetch_ms: list = field(default_factory=list)
    inference_ms: list = field(default_factory=list)
    n_requests: int = 0
    n_retries: int = 0
    issues: list = field(default_factory=list)

    @staticmethod
    def _pct(values: list, q: float) -> float | None:
        return round(float(np.percentile(values, q)), 1) if values else None

    def overall_status(self) -> str:
        ok = self.status_counts.get(STATUS_OK, 0)
        if self.n_cameras == 0 or ok == 0:
            return "failed"
        return "ok" if ok == self.n_cameras else "degraded"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("fetch_ms")
        d.pop("inference_ms")
        d.update(
            overall_status=self.overall_status(),
            coverage=round(self.status_counts.get(STATUS_OK, 0) / self.n_cameras, 4)
            if self.n_cameras
            else None,
            fetch_ms_p50=self._pct(self.fetch_ms, 50),
            fetch_ms_p95=self._pct(self.fetch_ms, 95),
            inference_ms_p50=self._pct(self.inference_ms, 50),
            inference_ms_p95=self._pct(self.inference_ms, 95),
            duration_s=round(self.finished_ts - self.started_ts, 1) if self.finished_ts else None,
        )
        return d

    def to_markdown(self) -> str:
        d = self.to_dict()
        statuses = ", ".join(f"{k}: {v}" for k, v in sorted(self.status_counts.items())) or "-"
        lines = [
            f"## Camera counting report `{self.run_id}`",
            "",
            f"- Overall status: **{d['overall_status']}**",
            f"- Cameras: {self.n_cameras} ({statuses}); coverage {d['coverage']}",
            f"- Model: `{self.model}` (score threshold {self.score_threshold})",
            f"- Fetch ms p50/p95: {d['fetch_ms_p50']} / {d['fetch_ms_p95']}; "
            f"inference ms p50/p95: {d['inference_ms_p50']} / {d['inference_ms_p95']}",
            f"- Requests: {self.n_requests} (retries {self.n_retries}), at most one per "
            f"{self.request_interval_s:g}s; duration {d['duration_s']} s",
            "",
            "### Issues",
            "",
        ]
        lines += [f"- {i}" for i in self.issues[:20]] or ["- none"]
        if len(self.issues) > 20:
            lines.append(f"- ... and {len(self.issues) - 20} more")
        return "\n".join(lines) + "\n"


class CameraCounter:
    def __init__(
        self,
        settings: CameraSettings,
        client: PoliteClient,
        detector: Detector,
        state: CameraState | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.client = client
        self.detector = detector
        self.state = state or CameraState(Path(settings.data_dir) / "camera_state.json")
        self.clock = clock

    def count_camera(self, camera_id: str, image_url: str, run_id: str) -> dict:
        """One row of numbers for one camera. Nothing about the image is kept."""
        row: dict = {
            "camera_id": camera_id,
            "run_id": run_id,
            "model": self.detector.model_name,
            "score_threshold": self.detector.score_threshold,
            **dict.fromkeys(COUNT_NAMES),
        }
        try:
            data = self.client.get_bytes(image_url)
        except CameraApiError as exc:
            row.update(status=STATUS_FETCH_ERROR, detail=str(exc), fetch_ms=None)
            return self._stamp(row)
        row["fetch_ms"] = self.client.last_elapsed_ms  # network time, without rate-limit waits
        now = self.clock()
        same, unchanged_s = self.state.observe(camera_id, hashlib.sha256(data).hexdigest(), now)
        row["repeat_frame"] = same
        if same and unchanged_s >= self.settings.frozen_after_min * 60:
            row.update(
                status=STATUS_FROZEN, detail=f"identical frame for {unchanged_s / 60:.0f} min"
            )
            return self._stamp(row, now)
        try:
            result = self.detector.count(data)
        except InvalidImage as exc:
            row.update(status=STATUS_INVALID, detail=str(exc))
            return self._stamp(row, now)
        finally:
            del data
        row.update(
            status=STATUS_OK,
            detail=None,
            image_width=result.width,
            image_height=result.height,
            inference_ms=result.inference_ms,
            **result.counts,
        )
        return self._stamp(row, now)

    def _stamp(self, row: dict, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        ts = pd.Timestamp(now, unit="s").floor("us")  # Parquet stores microseconds
        row["observed_utc"] = ts
        row["bucket_utc"] = ts.floor("min")
        return row

    def count_cameras(
        self, cameras: pd.DataFrame, run_id: str | None = None
    ) -> tuple[pd.DataFrame, CameraRunReport]:
        """Count every camera in ``cameras`` (columns camera_id, image_url)."""
        started = self.clock()
        run_id = run_id or make_run_id(started)
        report = CameraRunReport(
            run_id=run_id,
            started_ts=started,
            model=self.detector.model_name,
            score_threshold=self.detector.score_threshold,
            request_interval_s=self.settings.request_interval_s,
        )
        requests_before, retries_before = self.client.n_requests, self.client.n_retries
        rows = [
            self.count_camera(str(c.camera_id), str(c.image_url), run_id)
            for c in cameras[["camera_id", "image_url"]].itertuples(index=False)
        ]
        df = pd.DataFrame(rows)
        report.n_cameras = len(df)
        if len(df):
            report.status_counts = dict(Counter(df["status"]))
            for col, target in (
                ("fetch_ms", report.fetch_ms),
                ("inference_ms", report.inference_ms),
            ):
                if col in df:
                    target.extend(df[col].dropna().tolist())
            for r in df[df["status"] != STATUS_OK].itertuples():
                report.issues.append(f"camera {r.camera_id}: {r.status} ({r.detail})")
        report.n_requests = self.client.n_requests - requests_before
        report.n_retries = self.client.n_retries - retries_before
        report.finished_ts = self.clock()
        self.state.save()
        return df, report


def save_report(data_dir: Path, report: CameraRunReport) -> Path:
    date = dt.datetime.fromtimestamp(report.started_ts, dt.UTC).strftime("%Y-%m-%d")
    path = Path(data_dir) / "reports" / "cameras" / f"date={date}" / f"{report.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str))
    return path
