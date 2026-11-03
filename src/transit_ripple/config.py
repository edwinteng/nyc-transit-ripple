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


# Other realtime sources (M2). None of them needs a paid key.
DEFAULT_ALERTS_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts"
# NYC DOT Real-Time Traffic Speed on NYC Open Data (Socrata dataset i4gi-tjb9).
DEFAULT_TRAFFIC_URL = "https://data.cityofnewyork.us/resource/i4gi-tjb9.json"
# Official Citi Bike GBFS auto-discovery endpoint.
DEFAULT_GBFS_URL = "https://gbfs.citibikenyc.com/gbfs/gbfs.json"
# MTA Bus Time SIRI VehicleMonitoring (M5); needs the free MTA_BUS_API_KEY.
DEFAULT_BUS_SIRI_URL = "https://bustime.mta.info/api/siri/vehicle-monitoring.json"
# MTA bus static GTFS, one file per borough plus MTA Bus Company (no key).
DEFAULT_BUS_STATIC_URLS = ",".join(
    f"https://rrgtfsfeeds.s3.amazonaws.com/gtfs_{b}.zip"
    for b in ("b", "bx", "m", "q", "si", "busco")
)

# Secrets: environment variable name for each secret field. Never logged.
SECRET_ENV = {
    "mta_api_key": "MTA_API_KEY",
    "socrata_app_token": "SOCRATA_APP_TOKEN",
    "mta_bus_api_key": "MTA_BUS_API_KEY",
}

# Settings recorded in every quality report for reproducibility.
THRESHOLD_FIELDS = (
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
    "alerts_stale_after_s",
    "traffic_stale_after_s",
    "bike_feed_stale_after_s",
    "bike_station_stale_after_s",
    "source_gap_min",
    "bus_min_request_interval_s",
    "bus_min_pair_s",
    "bus_max_pair_s",
    "bus_max_speed_mph",
)


def _cast(name: str, raw: str, default):
    try:
        if isinstance(default, bool):
            if raw.strip().lower() in ("1", "true", "yes", "on"):
                return True
            if raw.strip().lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError(raw)
        if isinstance(default, Path) or name in ("data_dir", "static_dir"):
            return Path(raw)
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        return raw
    except ValueError as exc:
        raise ValueError(f"Invalid value for RIPPLE_{name.upper()}: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Every field can be set with ``RIPPLE_<FIELD_NAME>``."""

    data_dir: Path = Path("data")
    static_dir: Path | None = None
    static_url: str = DEFAULT_STATIC_URL
    static_max_age_hours: float = 6.0

    # --- subway feed quality (M1) ---------------------------------------------------
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

    # --- other sources (M2) -----------------------------------------------------------
    alerts_url: str = DEFAULT_ALERTS_URL
    # The alerts header timestamp is the time of the latest alert change, not a
    # heartbeat, so only a very old one indicates a frozen feed.
    alerts_stale_after_s: int = 6 * 3600
    traffic_url: str = DEFAULT_TRAFFIC_URL
    # Each run asks for observations newer than max(last seen, now - lookback).
    traffic_lookback_min: int = 90
    # A link whose newest observation is older than this is stale (DOT updates ~5 min).
    traffic_stale_after_s: int = 1200
    gbfs_url: str = DEFAULT_GBFS_URL
    gbfs_language: str = "en"
    bike_feed_stale_after_s: int = 300
    # A station whose last report is older than this is stale.
    bike_station_stale_after_s: int = 1800
    # Station information changes rarely: refresh at most this often.
    bike_info_max_age_hours: float = 24.0
    # Per-station minute data is large; keep it this many days (baselines need 28).
    bike_retention_days: int = 35
    # Minutes without a successful fetch of a source before it is reported as a gap.
    source_gap_min: int = 15

    # --- buses (M5) ---------------------------------------------------------------------
    bus_siri_url: str = DEFAULT_BUS_SIRI_URL
    # Comma separated static GTFS zips for bus stop locations and the routes at each stop.
    bus_static_urls: str = DEFAULT_BUS_STATIC_URLS
    bus_static_max_age_hours: float = 168.0
    # Bus Time asks clients not to poll more often than every 30 seconds per key.
    bus_min_request_interval_s: float = 30.0
    # A speed sample needs two positions of the same bus on the same trip this far apart.
    bus_min_pair_s: float = 20.0
    bus_max_pair_s: float = 300.0
    # Faster samples are GPS or distance errors and are dropped.
    bus_max_speed_mph: float = 60.0
    # A SIRI response whose newest vehicle report is older than this is stale.
    bus_stale_after_s: int = 300
    bus_retention_days: int = 35

    # --- spatial join and baselines -------------------------------------------------
    neighbor_radius_m: float = 500.0
    baseline_days: int = 28
    baseline_slot_min: int = 30
    baseline_min_days: int = 3
    baseline_max_age_hours: float = 6.0
    # Baselines describe earlier weeks: the most recent hours (which may contain the
    # disruption being analysed) are excluded.
    baseline_exclude_recent_hours: float = 3.0

    # --- delay events ------------------------------------------------------------------
    # Line-minute metric compared with the threshold (worst direction per minute).
    event_metric: str = "delay_median_min"
    event_threshold_min: float = 5.0
    # N: consecutive minutes at or above the threshold to open an event.
    event_min_minutes: int = 5
    # ...of which at least this many must be actual observations (not forward-filled).
    event_min_observed: int = 2
    # Minutes with fewer measured trips than this do not count.
    event_min_trips: int = 2
    # Line values are forward-filled at most this many minutes for detection.
    event_fill_max_age_min: int = 5
    # An event survives a data gap of up to this many minutes.
    event_max_gap_min: int = 10
    event_lookback_hours: float = 24.0
    # Number of most-affected stations used for the surface context.
    event_context_stations: int = 3
    # Surface series are forward-filled at most this many minutes.
    surface_fill_max_age_min: int = 20

    # --- cameras during events (M4) ------------------------------------------------------
    # Count cameras near the affected stations of ongoing events only, and never more
    # than this many cameras per run (the camera API allows about one per second).
    event_cameras_enabled: bool = True
    event_camera_max_per_run: int = 30
    # Camera counts are forward-filled at most this many minutes (one count per run).
    camera_fill_max_age_min: int = 10

    # --- surface impact score (M4, bus signal M5) -------------------------------------------
    # Weights of the signals; they are renormalized over the signals available.
    impact_weight_traffic: float = 0.3
    impact_weight_bus: float = 0.3
    impact_weight_camera: float = 0.2
    impact_weight_bikes: float = 0.2
    # Change vs. baseline that maps to a sub-score of 100 (linear, clipped to 0-100).
    impact_traffic_full_drop_pct: float = 40.0
    impact_bus_full_drop_pct: float = 40.0
    impact_camera_full_increase_pct: float = 100.0
    impact_bikes_full_drop_pct: float = 30.0
    # Lowest baseline confidence a signal may have to be used: "ok" or "low".
    impact_min_confidence: str = "ok"
    # The surface has recovered once the per-minute score stays below this...
    impact_recovery_threshold: float = 20.0
    # ...for this many consecutive minutes after the event.
    impact_recovery_minutes: int = 10
    # Minutes after the event end that are analysed for recovery.
    impact_recovery_window_min: int = 120

    # --- recovery model ("wait" option, M5) --------------------------------------------------
    # Past events used to estimate how long a disruption lasts.
    recovery_history_days: int = 180
    # Fewer similar past events than this: fall back to a broader group (low confidence).
    recovery_min_events: int = 5

    # --- recommendations (M5) -----------------------------------------------------------------
    # No destination is known, so every option is compared on the time to travel a
    # reference distance away from the affected station (destination-aware routing is
    # future work).
    rec_reference_trip_km: float = 4.0
    walk_speed_m_per_s: float = 1.3
    # Street distance / straight-line distance.
    walk_detour_factor: float = 1.3
    # Stations served by unaffected lines are considered up to this distance.
    rec_walk_max_m: float = 1200.0
    # Minimum time to change platforms at the same station.
    rec_transfer_min: float = 2.0
    rec_max_walk_options: int = 2
    rec_max_bus_options: int = 2
    # Premium-fare express commuter buses are not suggested (comma separated prefixes).
    rec_exclude_bus_route_prefixes: str = "BXM,BM,QM,SIM,X"
    # Average speeds for the reference trip (typical NYC values, configurable).
    rec_subway_speed_mph: float = 17.0
    rec_bike_speed_mph: float = 8.0
    rec_bus_default_speed_mph: float = 7.5
    # Waits assumed when no live arrival data exists.
    rec_default_train_wait_min: float = 5.0
    rec_default_bus_wait_min: float = 6.0
    rec_bike_unlock_min: float = 1.0
    rec_min_bikes: int = 2
    # Live data older than this is not used for recommendations.
    rec_data_max_age_min: float = 15.0
    # "Avoid the crowd": crowding signals on the most obvious option.
    rec_crowd_bus_slowdown_pct: float = 25.0
    rec_crowd_bike_depletion_pct: float = 30.0
    rec_crowd_bike_window_min: float = 15.0
    rec_crowd_camera_increase_pct: float = 50.0
    rec_crowd_penalty_min: float = 5.0

    # --- evaluation (M6) ------------------------------------------------------------------
    eval_window_days: int = 30
    # A metric with fewer observations than this is marked preliminary.
    eval_min_sample: int = 30
    # An alert matches an event of its line if it appeared within this many minutes
    # before the event start or before its end.
    eval_alert_tolerance_min: float = 15.0
    eval_bootstrap_samples: int = 2000
    eval_seed: int = 7
    # Placebo test: matched non-event windows are looked for this many days back...
    eval_placebo_days_back: int = 14
    # ...and control stations must be at least this far from every affected station.
    eval_control_min_distance_m: float = 2000.0
    eval_control_stations: int = 10
    # Recommendation replay: how long after a recommendation outcomes are looked for.
    eval_replay_horizon_min: float = 60.0
    # A walk_line option "degraded" if the real wait exceeded the estimate by this much.
    eval_walk_degrade_min: float = 3.0
    # Camera accuracy: manual labels and model counts (numbers only, no images).
    eval_camera_labels: str = "docs/evaluation/camera_labels.csv"
    eval_camera_predictions: str = "docs/evaluation/camera_predictions.csv"
    # Notable events on the home page.
    notable_events: int = 4
    notable_min_duration_min: float = 10.0
    notable_history_days: int = 180

    # --- static site and export ---------------------------------------------------------
    site_max_events: int = 30
    site_chart_padding_min: int = 30
    export_window_days: int = 30
    export_max_points: int = 120

    http_timeout_s: float = 20.0
    user_agent: str = "nyc-transit-ripple/0.2 (+https://github.com/edwinteng/nyc-transit-ripple)"

    # Optional secrets, read from the environment only and never logged or reported.
    # MTA_API_KEY: not needed by the subway feeds; sent as x-api-key if set.
    mta_api_key: str | None = field(default=None, repr=False)
    # SOCRATA_APP_TOKEN: optional free NYC Open Data app token (higher rate limits).
    socrata_app_token: str | None = field(default=None, repr=False)
    # MTA_BUS_API_KEY: free MTA Bus Time key; without it bus data is "unavailable".
    mta_bus_api_key: str | None = field(default=None, repr=False)

    @property
    def resolved_static_dir(self) -> Path:
        return self.static_dir if self.static_dir is not None else self.data_dir / "static"

    def with_overrides(self, **overrides) -> Settings:
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)

    def thresholds(self) -> dict[str, float | int]:
        """Quality thresholds, recorded in every report for reproducibility."""
        return {name: getattr(self, name) for name in THRESHOLD_FIELDS}

    def impact_rule(self) -> dict[str, float | int | str]:
        """The configurable impact score definition, recorded with every event."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name.startswith("impact_")}

    def recommendation_rule(self) -> dict[str, float | int | str]:
        """The configurable recommendation assumptions, stored with every recommendation."""
        prefixes = ("rec_", "walk_", "recovery_")
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name.startswith(prefixes)}

    def event_rule(self) -> dict[str, float | int | str]:
        """The configurable delay event definition, stored with every event."""
        names = (
            "event_metric",
            "event_threshold_min",
            "event_min_minutes",
            "event_min_observed",
            "event_min_trips",
            "event_fill_max_age_min",
            "event_max_gap_min",
        )
        return {name: getattr(self, name) for name in names}

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        values = {}
        for f in fields(cls):
            if f.name in SECRET_ENV:
                values[f.name] = os.environ.get(SECRET_ENV[f.name]) or None
                continue
            raw = os.environ.get(f"RIPPLE_{f.name.upper()}")
            if raw is None or raw.strip() == "":
                continue
            values[f.name] = _cast(f.name, raw, getattr(defaults, f.name))
        return replace(defaults, **values)


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
