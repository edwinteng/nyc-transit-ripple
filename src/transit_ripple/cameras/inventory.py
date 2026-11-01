"""NYCTMC public traffic camera inventory (``GET {api_base}/cameras``, no key).

Each camera has an id, a name, a borough ("area"), coordinates, an online flag and
an image URL. The list is stored as ``reference/cameras.parquet`` and refreshed
at most every ``inventory_max_age_days`` (weekly by default).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

import pandas as pd

from .client import CameraApiError, PoliteClient
from .config import CameraSettings
from .storage import read_reference, write_reference

# Generous bounding box around New York City.
NYC_BBOX = (40.40, 41.10, -74.40, -73.60)
INVENTORY_COLUMNS = [
    "camera_id",
    "name",
    "area",
    "lat",
    "lon",
    "is_online",
    "image_url",
    "fetched_utc",
]


class InventoryError(ValueError):
    """The camera list could not be parsed."""


@dataclass
class InventoryResult:
    cameras: pd.DataFrame
    refreshed: bool
    n_invalid: int = 0
    error: str | None = None
    age_days: float | None = None


def _flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return None if value is None else bool(value)


def parse_inventory(
    content: bytes, fetched_utc: pd.Timestamp, api_base: str
) -> tuple[pd.DataFrame, int]:
    """Parse the camera list. Rows without an id or with coordinates outside NYC are dropped."""
    try:
        rows = json.loads(content)
    except ValueError as exc:
        raise InventoryError(f"camera list is not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise InventoryError("camera list is not a JSON array")
    lat0, lat1, lon0, lon1 = NYC_BBOX
    out, invalid = [], 0
    for r in rows:
        try:
            camera_id = str(r["id"]).strip()
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if not camera_id or not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1):
            invalid += 1
            continue
        out.append(
            {
                "camera_id": camera_id,
                "name": (r.get("name") or "").strip() or None,
                "area": r.get("area"),
                "lat": lat,
                "lon": lon,
                "is_online": _flag(r.get("isOnline")),
                "image_url": r.get("imageUrl")
                or f"{api_base.rstrip('/')}/cameras/{camera_id}/image",
                "fetched_utc": fetched_utc,
            }
        )
    df = pd.DataFrame(out, columns=INVENTORY_COLUMNS).drop_duplicates("camera_id", keep="last")
    return df.sort_values("camera_id").reset_index(drop=True), invalid


def inventory_age_days(data_dir, now: dt.datetime) -> float | None:
    cams = read_reference(data_dir, "cameras")
    if cams.empty:
        return None
    fetched = pd.to_datetime(cams["fetched_utc"]).max()
    return (pd.Timestamp(now) - fetched).total_seconds() / 86400.0


def refresh_inventory(
    settings: CameraSettings,
    client: PoliteClient,
    now: dt.datetime,
    force: bool = False,
) -> InventoryResult:
    """Return the inventory, downloading a new one if missing, stale or forced.

    If the download fails and a stored inventory exists, the stored one is used.
    """
    age = inventory_age_days(settings.data_dir, now)
    if not force and age is not None and age < settings.inventory_max_age_days:
        return InventoryResult(read_reference(settings.data_dir, "cameras"), False, age_days=age)
    url = f"{settings.api_base.rstrip('/')}/cameras"
    try:
        content = client.get_bytes(url)
        cams, invalid = parse_inventory(content, pd.Timestamp(now), settings.api_base)
        if cams.empty:
            raise InventoryError("camera list is empty")
    except (CameraApiError, InventoryError) as exc:
        stored = read_reference(settings.data_dir, "cameras")
        if stored.empty:
            raise
        return InventoryResult(stored, False, error=str(exc), age_days=age)
    write_reference(settings.data_dir, "cameras", cams)
    return InventoryResult(cams, True, n_invalid=invalid, age_days=0.0)
