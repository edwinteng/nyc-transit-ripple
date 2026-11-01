"""Feed definitions and runtime settings.

All settings can be overridden with environment variables (prefix ``RIPPLE_``).
Secrets are read from environment variables only and are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

MTA_FEED_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F"

# "Supplemented" static GTFS: the regular schedule plus planned service changes
# for roughly the past two days and the next seven days. It matches realtime
# trip ids far better than the plain schedule, so it is the default.
DEFAULT_STATIC_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip"

AGENCY_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class FeedSpec:
    """One NYCT subway GTFS-Realtime endpoint and the routes it is expected to carry."""

    feed_id: str
    path: str
    routes: tuple[str, ...]

    @property
    def url(self) -> str:
        return MTA_FEED_BASE + self.path


# All subway feeds. No API key is required for these endpoints.
# Route lists are used to emit explicit "missing" rows when a feed is down.
SUBWAY_FEEDS: dict[str, FeedSpec] = {
    spec.feed_id: spec
    for spec in (
        FeedSpec("numbered", "gtfs", ("1", "2", "3", "4", "5", "6", "6X", "7", "7X", "GS")),
        FeedSpec("ace", "gtfs-ace", ("A", "C", "E", "H")),
        FeedSpec("bdfm", "gtfs-bdfm", ("B", "D", "F", "FX", "M", "FS")),
        FeedSpec("g", "gtfs-g", ("G",)),
        FeedSpec("jz", "gtfs-jz", ("J", "Z")),
        FeedSpec("nqrw", "gtfs-nqrw", ("N", "Q", "R", "W")),
        FeedSpec("l", "gtfs-l", ("L",)),
        FeedSpec("si", "gtfs-si", ("SI",)),
    )
}


def resolve_feeds(selection: str | None) -> list[FeedSpec]:
    """Turn a comma separated selection (or ``all``/``None``) into feed specs."""
    if not selection or selection.strip().lower() == "all":
        return list(SUBWAY_FEEDS.values())
    specs = []
    for raw in selection.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in SUBWAY_FEEDS:
            known = ", ".join(SUBWAY_FEEDS)
            raise ValueError(f"Unknown feed '{raw}'. Known feeds: {known}")
        specs.append(SUBWAY_FEEDS[key])
    return specs


def _env(name: str, default, cast):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for {name}: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Thresholds drive the data quality checks."""

    data_dir: Path = Path("data")
    static_dir: Path | None = None
    static_url: str = DEFAULT_STATIC_URL
    static_max_age_hours: float = 6.0

    # Feed freshness: a snapshot older than this (fetch time - header time) is stale.
    stale_after_s: int = 180
    # Header timestamps further than this in the future are rejected (clock jump).
    future_tolerance_s: int = 60
    # Stop events outside [header - max_lag, header + max_lead] are rejected.
    max_lag_s: int = 2 * 3600
    max_lead_s: int = 4 * 3600
    # Delays outside [min_delay_min, max_delay_min] are treated as bad schedule matches.
    min_delay_min: float = -30.0
    max_delay_min: float = 240.0
    # A trip's delay may change by at most (elapsed minutes + tolerance) between snapshots.
    delay_jump_tolerance_min: float = 15.0
    # Successive stop times inside a trip may go backwards by at most this much.
    non_monotonic_tolerance_s: int = 60
    # A downstream stop (not the current one) whose predicted time is already this far
    # in the past is a stale prediction (frozen "ghost" or held train data).
    stale_prediction_tolerance_s: int = 120
    # Vehicle positions older than this (relative to the header) are counted as stale.
    vehicle_stale_after_s: int = 600
    # Trips at least this late count as "delayed" in line aggregates.
    delayed_threshold_min: float = 5.0

    http_timeout_s: float = 20.0
    user_agent: str = "nyc-transit-ripple/0.1 (+https://github.com/edwinteng/nyc-transit-ripple)"

    # Optional secret. The subway feeds do not need a key; if one is set it is sent
    # as the x-api-key header. Read from the environment only, never logged.
    mta_api_key: str | None = field(default=None, repr=False)

    @property
    def resolved_static_dir(self) -> Path:
        return self.static_dir if self.static_dir is not None else self.data_dir / "static"

    def with_overrides(self, **overrides) -> Settings:
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)

    def thresholds(self) -> dict[str, float | int]:
        """Quality thresholds, recorded in every report for reproducibility."""
        names = (
            "stale_after_s",
            "future_tolerance_s",
            "max_lag_s",
            "max_lead_s",
            "min_delay_min",
            "max_delay_min",
            "delay_jump_tolerance_min",
            "non_monotonic_tolerance_s",
            "stale_prediction_tolerance_s",
            "vehicle_stale_after_s",
            "delayed_threshold_min",
        )
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name in names}

    @classmethod
    def from_env(cls) -> Settings:
        d = cls()
        static_dir = os.environ.get("RIPPLE_STATIC_DIR")
        return cls(
            data_dir=Path(_env("RIPPLE_DATA_DIR", str(d.data_dir), str)),
            static_dir=Path(static_dir) if static_dir else None,
            static_url=_env("RIPPLE_STATIC_URL", d.static_url, str),
            static_max_age_hours=_env("RIPPLE_STATIC_MAX_AGE_HOURS", d.static_max_age_hours, float),
            stale_after_s=_env("RIPPLE_STALE_AFTER_S", d.stale_after_s, int),
            future_tolerance_s=_env("RIPPLE_FUTURE_TOLERANCE_S", d.future_tolerance_s, int),
            max_lag_s=_env("RIPPLE_MAX_LAG_S", d.max_lag_s, int),
            max_lead_s=_env("RIPPLE_MAX_LEAD_S", d.max_lead_s, int),
            min_delay_min=_env("RIPPLE_MIN_DELAY_MIN", d.min_delay_min, float),
            max_delay_min=_env("RIPPLE_MAX_DELAY_MIN", d.max_delay_min, float),
            delay_jump_tolerance_min=_env(
                "RIPPLE_DELAY_JUMP_TOLERANCE_MIN", d.delay_jump_tolerance_min, float
            ),
            non_monotonic_tolerance_s=_env(
                "RIPPLE_NON_MONOTONIC_TOLERANCE_S", d.non_monotonic_tolerance_s, int
            ),
            stale_prediction_tolerance_s=_env(
                "RIPPLE_STALE_PREDICTION_TOLERANCE_S", d.stale_prediction_tolerance_s, int
            ),
            vehicle_stale_after_s=_env(
                "RIPPLE_VEHICLE_STALE_AFTER_S", d.vehicle_stale_after_s, int
            ),
            delayed_threshold_min=_env(
                "RIPPLE_DELAYED_THRESHOLD_MIN", d.delayed_threshold_min, float
            ),
            http_timeout_s=_env("RIPPLE_HTTP_TIMEOUT_S", d.http_timeout_s, float),
            user_agent=_env("RIPPLE_USER_AGENT", d.user_agent, str),
            mta_api_key=os.environ.get("MTA_API_KEY") or None,
        )


def load_dotenv(path: str | os.PathLike = ".env") -> list[str]:
    """Load ``KEY=VALUE`` lines from a local .env file into ``os.environ``.

    Existing environment variables win, so CI secrets are never overridden.
    Returns the names of the variables that were set (values are not returned).
    """
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
