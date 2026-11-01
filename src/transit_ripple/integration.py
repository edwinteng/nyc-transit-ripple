"""Glue between event detection (M2) and the camera module (M3).

``EventCameraHook`` is passed to ``events.process_events``. For each *ongoing*
event (most severe first) it counts the cameras within the radius of the event's
most affected stations through ``cameras.count_near_stations``, which stores the
counts in ``camera_counts``. A hard cap limits the number of cameras counted per
run (``RIPPLE_EVENT_CAMERA_MAX_PER_RUN``), so the step stays within the time
budget of a scheduled GitHub Actions run (about one camera per second). Ended
events never trigger camera requests.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


class EventCameraHook:
    def __init__(
        self,
        settings: Settings,
        now: dt.datetime | None = None,
        counter_factory: Callable | None = None,
    ):
        from .cameras.config import CameraSettings

        self.settings = settings
        self.camera_settings = CameraSettings.from_env().with_overrides(data_dir=settings.data_dir)
        self.now = now
        self.cap = settings.event_camera_max_per_run
        self.left = self.cap
        self._factory = counter_factory
        self._counter = None
        self.runs: list[dict] = []
        self.errors: list[str] = []

    def _counter_factory(self, camera_settings):
        # Build the detector and polite client once per run and reuse them.
        if self._counter is None:
            if self._factory is not None:
                self._counter = self._factory(camera_settings)
            else:
                from .cameras.interface import _default_counter

                self._counter = _default_counter(camera_settings)
        return self._counter

    def __call__(self, event_id: str, station_ids: list[str]) -> dict | None:
        if self.left <= 0:
            self.runs.append({"event_id": event_id, "skipped": "camera cap reached"})
            return None
        from .cameras.interface import count_near_stations

        try:
            df = count_near_stations(
                list(station_ids),
                None,
                settings=self.camera_settings,
                counter_factory=self._counter_factory,
                now=self.now or dt.datetime.now(dt.UTC),
                max_cameras=self.left,
            )
        except (ValueError, ImportError, OSError, RuntimeError) as exc:
            # No mapping yet, vision extra missing, model download failed, ...
            msg = f"{event_id}: cameras skipped ({type(exc).__name__}: {exc})"
            log.warning(msg)
            self.errors.append(msg)
            return None
        live = df[df["source"] == "live"].drop_duplicates("camera_id")
        self.left -= len(live)
        run = {
            "event_id": event_id,
            "stations": list(station_ids),
            "cameras_near": int(df["camera_id"].nunique()),
            "cameras_counted": len(live),
            "statuses": dict(Counter(live["status"])),
        }
        self.runs.append(run)
        return run

    def report(self) -> dict:
        counted = sum(r.get("cameras_counted", 0) for r in self.runs)
        statuses: Counter = Counter()
        for r in self.runs:
            statuses.update(r.get("statuses", {}))
        return {
            "cap": self.cap,
            "cameras_counted": counted,
            "status_counts": dict(statuses),
            "events": self.runs,
            "errors": self.errors,
        }


def save_events_report(data_dir: Path, run_id: str, now: dt.datetime, report: dict) -> Path:
    path = Path(data_dir) / "reports" / "events" / f"date={now:%Y-%m-%d}" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str))
    return path
