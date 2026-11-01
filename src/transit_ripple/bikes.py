"""Citi Bike GBFS: auto-discovery, station information and station status.

Supports GBFS 1.x/2.x (``data.<language>.feeds``) and 3.x (``data.feeds``,
localized names, RFC 3339 timestamps).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

import pandas as pd

# last_reported values before this are placeholders (the feed uses e.g. 86400).
MIN_VALID_TS = 1_420_070_400  # 2015-01-01

INFO_COLUMNS = ["station_id", "name", "short_name", "lat", "lon", "capacity", "region_id"]
STATUS_COLUMNS = [
    "station_id",
    "bikes_available",
    "ebikes_available",
    "docks_available",
    "bikes_disabled",
    "docks_disabled",
    "is_installed",
    "is_renting",
    "is_returning",
    "last_reported_ts",
]


class GbfsError(ValueError):
    """Raised for payloads that are not valid GBFS documents."""


@dataclass
class Discovery:
    version: str | None
    feeds: dict[str, str]
    versions: dict[str, str] = field(default_factory=dict)  # version -> discovery URL


@dataclass
class StationStatus:
    last_updated: int | None
    ttl: int | None
    stations: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=STATUS_COLUMNS))
    n_invalid_reported: int = 0


@dataclass
class StationInfo:
    last_updated: int | None
    stations: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=INFO_COLUMNS))


def _load(content: bytes, what: str) -> dict:
    try:
        doc = json.loads(content)
    except ValueError as exc:
        raise GbfsError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
        raise GbfsError(f"{what} has no 'data' object")
    return doc


def _ts(value) -> int | None:
    """GBFS timestamps: POSIX seconds (1.x/2.x) or RFC 3339 strings (3.x)."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _localized(value, language: str) -> str | None:
    if isinstance(value, list):  # GBFS 3.x: [{"text": ..., "language": ...}]
        for item in value:
            if isinstance(item, dict) and item.get("language") == language:
                return item.get("text")
        return value[0].get("text") if value and isinstance(value[0], dict) else None
    return value


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for p in version.lstrip("vV").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def parse_discovery(content: bytes, language: str = "en") -> Discovery:
    """Feed name -> URL from a ``gbfs.json`` auto-discovery document."""
    doc = _load(content, "GBFS discovery")
    data = doc["data"]
    if isinstance(data.get("feeds"), list):  # GBFS 3.x
        feeds = data["feeds"]
    else:
        lang_block = data.get(language) or next(iter(data.values()), {})
        feeds = lang_block.get("feeds", []) if isinstance(lang_block, dict) else []
    mapping = {f["name"]: f["url"] for f in feeds if isinstance(f, dict) and "name" in f}
    if not mapping:
        raise GbfsError("GBFS discovery lists no feeds")
    return Discovery(version=doc.get("version"), feeds=mapping)


def parse_versions(content: bytes) -> dict[str, str]:
    """``gbfs_versions.json``: version -> discovery URL."""
    doc = _load(content, "GBFS versions")
    return {
        str(v["version"]).lstrip("vV"): v["url"]
        for v in doc["data"].get("versions", [])
        if isinstance(v, dict) and "version" in v and "url" in v
    }


def choose_version(versions: dict[str, str], max_major: int = 2) -> tuple[str, str] | None:
    """Highest advertised version whose major number is supported."""
    usable = [(v, url) for v, url in versions.items() if _version_key(v)[0] <= max_major]
    if not usable:
        return None
    return max(usable, key=lambda item: _version_key(item[0]))


def parse_station_information(content: bytes, language: str = "en") -> StationInfo:
    doc = _load(content, "station_information")
    rows = []
    for s in doc["data"].get("stations", []):
        try:
            rows.append(
                {
                    "station_id": str(s["station_id"]),
                    "name": _localized(s.get("name"), language),
                    "short_name": _localized(s.get("short_name"), language),
                    "lat": float(s["lat"]),
                    "lon": float(s["lon"]),
                    "capacity": int(s["capacity"]) if s.get("capacity") is not None else None,
                    "region_id": str(s["region_id"]) if s.get("region_id") is not None else None,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    df = pd.DataFrame(rows, columns=INFO_COLUMNS)
    df["capacity"] = df["capacity"].astype("Int64")
    return StationInfo(last_updated=_ts(doc.get("last_updated")), stations=df)


def _flag(value) -> bool | None:
    if value is None:
        return None
    return bool(value)


def parse_station_status(content: bytes) -> StationStatus:
    """Bikes/docks per station. Invalid ``last_reported`` placeholders become NULL."""
    doc = _load(content, "station_status")
    rows, invalid = [], 0
    for s in doc["data"].get("stations", []):
        if "station_id" not in s:
            continue
        reported = _ts(s.get("last_reported"))
        if reported is not None and reported < MIN_VALID_TS:
            reported = None
            invalid += 1
        bikes = s.get("num_bikes_available", s.get("num_vehicles_available"))
        rows.append(
            {
                "station_id": str(s["station_id"]),
                "bikes_available": bikes,
                "ebikes_available": s.get("num_ebikes_available"),
                "docks_available": s.get("num_docks_available"),
                "bikes_disabled": s.get("num_bikes_disabled", s.get("num_vehicles_disabled")),
                "docks_disabled": s.get("num_docks_disabled"),
                "is_installed": _flag(s.get("is_installed")),
                "is_renting": _flag(s.get("is_renting")),
                "is_returning": _flag(s.get("is_returning")),
                "last_reported_ts": reported,
            }
        )
    df = pd.DataFrame(rows, columns=STATUS_COLUMNS)
    for col in (
        "bikes_available",
        "ebikes_available",
        "docks_available",
        "bikes_disabled",
        "docks_disabled",
        "last_reported_ts",
    ):
        df[col] = df[col].astype("Int64")
    for col in ("is_installed", "is_renting", "is_returning"):
        df[col] = df[col].astype("boolean")
    ttl = doc.get("ttl")
    return StationStatus(
        last_updated=_ts(doc.get("last_updated")),
        ttl=int(ttl) if isinstance(ttl, int | float) else None,
        stations=df,
        n_invalid_reported=invalid,
    )
