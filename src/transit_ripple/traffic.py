"""NYC DOT Real-Time Traffic Speed (NYC Open Data / Socrata dataset ``i4gi-tjb9``).

Each row is one observation of one road link: average speed, travel time and the
time of the observation (``data_as_of``, a floating timestamp in New York local
time). Link geometry comes with every row as a Google encoded polyline.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd

from .config import AGENCY_TIMEZONE

# Generous bounding box around New York City for geometry sanity checks.
NYC_BBOX = (40.40, 41.10, -74.40, -73.60)  # lat_min, lat_max, lon_min, lon_max
# link_points is cut at 255 characters by the publisher; its last pair may be partial.
LINK_POINTS_MAX_LEN = 250

OBS_COLUMNS = [
    "link_id",
    "observed_ts",
    "speed_mph",
    "travel_time_s",
    "dot_status",
    "is_valid",
]
LINK_COLUMNS = [
    "link_id",
    "link_name",
    "borough",
    "owner",
    "lats",
    "lons",
    "geometry_source",
]


class TrafficParseError(ValueError):
    """Raised when the Socrata response is not the expected JSON array."""


@dataclass
class ParsedSpeeds:
    observations: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=OBS_COLUMNS))
    links: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=LINK_COLUMNS))
    n_rows: int = 0
    n_bad_rows: int = 0


# --------------------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------------------


def local_to_epoch(text: str, reference_ts: float, tz: str = AGENCY_TIMEZONE) -> int:
    """Convert a naive local ``YYYY-MM-DDTHH:MM:SS[.fff]`` to epoch seconds.

    In the repeated hour when daylight saving time ends, the interpretation
    that is not in the future relative to ``reference_ts`` (fetch time) wins.
    """
    naive = dt.datetime.fromisoformat(text.rstrip("Z"))
    zone = ZoneInfo(tz)
    candidates = sorted({int(naive.replace(tzinfo=zone, fold=fold).timestamp()) for fold in (0, 1)})
    not_future = [c for c in candidates if c <= reference_ts + 60]
    return not_future[-1] if not_future else candidates[0]


def epoch_to_local_text(ts: float, tz: str = AGENCY_TIMEZONE) -> str:
    """Epoch seconds -> naive local timestamp text for SoQL comparisons."""
    return dt.datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M:%S")


def soql_since(since_ts: float, limit: int = 50000) -> dict[str, str]:
    """Query parameters for observations newer than ``since_ts`` (oldest first)."""
    return {
        "$where": f"data_as_of > '{epoch_to_local_text(since_ts)}'",
        "$order": "data_as_of ASC",
        "$limit": str(limit),
    }


SOQL_LATEST = {"$select": "max(data_as_of) AS latest"}


def parse_latest(content: bytes, reference_ts: float) -> int | None:
    """Parse the ``max(data_as_of)`` probe used when no recent rows are returned."""
    try:
        rows = json.loads(content)
        text = rows[0].get("latest") if rows else None
    except (ValueError, AttributeError, IndexError) as exc:
        raise TrafficParseError(f"cannot parse latest-timestamp response: {exc}") from exc
    return local_to_epoch(text, reference_ts) if text else None


# --------------------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------------------


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]] | None:
    """Decode a Google encoded polyline. Returns None if the string is malformed."""
    points: list[tuple[float, float]] = []
    index = lat = lon = 0
    factor = 10**precision
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= len(encoded):
                    return None
                b = ord(encoded[index]) - 63
                index += 1
                if b < 0:
                    return None
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        points.append((lat / factor, lon / factor))
    return points


def parse_link_points(text: str | None) -> list[tuple[float, float]]:
    """Parse ``"lat,lon lat,lon ..."``; drops the last pair when the text was cut."""
    if not text:
        return []
    pairs = text.split()
    if len(text) >= LINK_POINTS_MAX_LEN and pairs:
        pairs = pairs[:-1]
    points = []
    for pair in pairs:
        parts = pair.split(",")
        try:
            points.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError):
            continue
    return points


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    k = math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((a[0] - b[0]) * 111_320.0, (a[1] - b[1]) * 111_320.0 * k)


def _in_nyc(points: list[tuple[float, float]]) -> bool:
    lat0, lat1, lon0, lon1 = NYC_BBOX
    return bool(points) and all(lat0 <= la <= lat1 and lon0 <= lo <= lon1 for la, lo in points)


def link_geometry(
    encoded: str | None, link_points: str | None, tolerance_m: float = 100.0
) -> tuple[list[tuple[float, float]], str]:
    """Best available geometry for a link and where it came from.

    The published polylines sometimes contain doubled backslashes that are not
    part of the encoding. Candidates are validated against the (truncated)
    ``link_points`` prefix; if none matches, the truncated points are used.
    """
    reference = parse_link_points(link_points)
    if encoded:
        for variant, label in (
            (encoded, "polyline"),
            (encoded.replace("\\\\", ""), "polyline_fixed"),
        ):
            pts = decode_polyline(variant)
            if not pts or not _in_nyc(pts):
                continue
            n = min(len(pts), len(reference))
            if all(_distance_m(pts[i], reference[i]) <= tolerance_m for i in range(n)):
                return pts, label
    if reference and _in_nyc(reference):
        return reference, "link_points"
    return [], "none"


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


def parse_speeds(content: bytes, fetched_ts: float) -> ParsedSpeeds:
    """Normalize Socrata rows into observations and link reference records.

    An observation is valid when the DOT status is 0 and the speed is positive;
    status -101 with speed 0 is how the feed marks "no data".
    """
    try:
        rows = json.loads(content)
    except ValueError as exc:
        raise TrafficParseError(f"traffic response is not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise TrafficParseError("traffic response is not a JSON array")

    obs, links, bad = [], {}, 0
    for r in rows:
        try:
            link_id = str(r["id"])
            observed = local_to_epoch(r["data_as_of"], fetched_ts)
            speed = float(r["speed"]) if r.get("speed") not in (None, "") else None
            travel = float(r["travel_time"]) if r.get("travel_time") not in (None, "") else None
            status = int(r["status"]) if r.get("status") not in (None, "") else None
        except (KeyError, TypeError, ValueError):
            bad += 1
            continue
        obs.append(
            {
                "link_id": link_id,
                "observed_ts": observed,
                "speed_mph": speed,
                "travel_time_s": travel,
                "dot_status": status,
                "is_valid": status == 0 and speed is not None and speed > 0,
            }
        )
        prev = links.get(link_id)
        if prev is None or observed >= prev[0]:
            links[link_id] = (observed, r)

    link_rows = []
    for link_id, (_, r) in links.items():
        pts, source = link_geometry(r.get("encoded_poly_line"), r.get("link_points"))
        link_rows.append(
            {
                "link_id": link_id,
                "link_name": r.get("link_name"),
                "borough": r.get("borough"),
                "owner": r.get("owner"),
                "lats": [p[0] for p in pts],
                "lons": [p[1] for p in pts],
                "geometry_source": source,
            }
        )
    observations = pd.DataFrame(obs, columns=OBS_COLUMNS)
    observations["observed_ts"] = observations["observed_ts"].astype("Int64")
    observations = observations.drop_duplicates(["link_id", "observed_ts"], keep="last")
    return ParsedSpeeds(
        observations=observations.reset_index(drop=True),
        links=pd.DataFrame(link_rows, columns=LINK_COLUMNS),
        n_rows=len(rows),
        n_bad_rows=bad,
    )
