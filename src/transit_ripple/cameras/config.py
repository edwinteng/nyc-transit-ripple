"""Settings for the camera module.

Every field can be set with ``RIPPLE_CAMERA_<FIELD_NAME>``; the data directory is
shared with the rest of the pipeline (``RIPPLE_DATA_DIR``) and the model cache
uses ``RIPPLE_MODEL_CACHE_DIR``. The camera API needs no key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

DEFAULT_API_BASE = "https://webcams.nyctmc.org/api"


def default_model_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "nyc-transit-ripple" / "models"


@dataclass(frozen=True)
class CameraSettings:
    data_dir: Path = Path("data")
    api_base: str = DEFAULT_API_BASE
    # The camera list changes rarely: refresh it at most this often.
    inventory_max_age_days: float = 7.0
    radius_m: float = 500.0

    # Politeness towards the camera API: at most one request per interval, and
    # retries with exponential backoff (backoff_s, 2x, 4x, ...) on 429/5xx/network errors.
    request_interval_s: float = 1.0
    max_retries: int = 3
    backoff_s: float = 2.0
    max_backoff_s: float = 60.0
    timeout_s: float = 15.0

    # Detector (see model.py for the available models and their licenses).
    model: str = "yolox_s"
    model_cache_dir: Path | None = None
    score_threshold: float = 0.35
    nms_threshold: float = 0.45
    threads: int = 0  # ONNX Runtime intra-op threads; 0 = library default

    # A camera returning the identical frame for this long is frozen (counts are NULL).
    frozen_after_min: float = 10.0
    # count_near_station: timestamps this close to "now" are counted live...
    live_window_min: float = 5.0
    # ...older ones are answered from stored counts within this tolerance.
    match_tolerance_min: float = 5.0

    user_agent: str = (
        "nyc-transit-ripple/0.3 cameras (+https://github.com/edwinteng/nyc-transit-ripple)"
    )

    @property
    def resolved_model_cache_dir(self) -> Path:
        return self.model_cache_dir or default_model_cache_dir()

    def with_overrides(self, **overrides) -> CameraSettings:
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})

    @classmethod
    def from_env(cls) -> CameraSettings:
        defaults = cls()
        values = {}
        for f in fields(cls):
            if f.name == "data_dir":
                raw = os.environ.get("RIPPLE_DATA_DIR")
            elif f.name == "model_cache_dir":
                raw = os.environ.get("RIPPLE_MODEL_CACHE_DIR")
            else:
                raw = os.environ.get(f"RIPPLE_CAMERA_{f.name.upper()}")
            if raw is None or raw.strip() == "":
                continue
            default = getattr(defaults, f.name)
            try:
                if f.name in ("data_dir", "model_cache_dir"):
                    values[f.name] = Path(raw)
                elif isinstance(default, bool):
                    values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int):
                    values[f.name] = int(raw)
                elif isinstance(default, float):
                    values[f.name] = float(raw)
                else:
                    values[f.name] = raw
            except ValueError as exc:
                raise ValueError(f"Invalid value for {f.name}: {raw!r}") from exc
        return replace(defaults, **values)
