"""Fixtures for camera tests (fakes live in fakes.py)."""

from __future__ import annotations

import pandas as pd
import pytest

from transit_ripple.cameras.config import CameraSettings


@pytest.fixture
def settings(tmp_path):
    return CameraSettings(data_dir=tmp_path, request_interval_s=1.0, backoff_s=2.0, max_retries=3)


@pytest.fixture
def cameras():
    return pd.DataFrame(
        {
            "camera_id": ["c1", "c2", "c3"],
            "name": ["Adams St @ Willoughby St", "Tillary St @ Jay St", "Far Away Rd"],
            "area": ["Brooklyn", "Brooklyn", "Queens"],
            "lat": [40.6925, 40.6960, 40.7500],
            "lon": [-73.9880, -73.9870, -73.8000],
            "is_online": [True, True, True],
            "image_url": [
                "https://cams/c1/image",
                "https://cams/c2/image",
                "https://cams/c3/image",
            ],
            "fetched_utc": pd.Timestamp("2026-11-01 02:00"),
        }
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Camera tests must never reach the camera API or download weights."""
    import socket

    def refuse(*args, **kwargs):
        raise RuntimeError("network access is disabled in camera tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
