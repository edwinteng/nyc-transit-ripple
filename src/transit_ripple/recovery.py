"""Historical recovery model for the "wait" option (M5).

From past delay events that ended because service recovered, estimate how long a
disruption lasts (median and 75th percentile, in minutes), grouped by

- line (route),
- time-of-day band of the event start (New York time), and
- cause category of the linked unplanned MTA alert, when there is one.

If a group has fewer than ``recovery_min_events`` past events, broader groups are
tried in this order: line + band + cause, line + band, line, band, all events.
Any fallback, or a final group that is still small, makes the estimate
``low`` confidence. With no past events at all there is no estimate.

For an event that has already lasted ``elapsed`` minutes, the remaining delay is
estimated from the past events that lasted longer than that (their duration minus
``elapsed``), so a long-running disruption is not promised to end "any minute".

Surface clearing time is the time from the subway recovery to the recovery of the
nearby surface signals (``surface_recovered_utc - end_utc``), with the same
grouping and fallbacks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import AGENCY_TIMEZONE, Settings

# First match wins, so the more specific phrases come first.
CAUSE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("person_on_tracks", ("person on the track", "someone on the track", "struck by")),
    ("signal", ("signal",)),
    ("track_or_switch", ("switch", "track", "rail condition", "broken rail")),
    ("power", ("power", "electrical")),
    ("mechanical", ("mechanical", "brake", "door problem", "train with a problem")),
    ("medical", ("sick customer", "medical", "ems ", "injured")),
    ("police", ("police", "nypd", "investigation", "unruly")),
    ("weather", ("weather", "flood", "snow", "heat", "water condition")),
    ("crowding", ("crowd",)),
)
MIN_CONDITIONAL_EVENTS = 3


def cause_category(alerts) -> str | None:
    """Cause of the first linked *unplanned* alert, from keywords in its text."""
    for a in alerts if alerts is not None else []:
        if a.get("is_planned"):
            continue
        text = f" {a.get('alert_type') or ''} {a.get('header_text') or ''} ".lower()
        for cause, words in CAUSE_KEYWORDS:
            if any(w in text for w in words):
                return cause
        return "other"
    return None


def time_band(ts_utc) -> str:
    """Time-of-day band of a UTC time, in New York time; weekends are one band."""
    local = pd.Timestamp(ts_utc).tz_localize("UTC").tz_convert(ZoneInfo(AGENCY_TIMEZONE))
    if local.day_of_week >= 5:
        return "weekend"
    h = local.hour
    if 6 <= h < 10:
        return "am_peak"
    if 10 <= h < 16:
        return "midday"
    if 16 <= h < 20:
        return "pm_peak"
    if 20 <= h < 24:
        return "evening"
    return "night"


BAND_TEXT = {
    "am_peak": "morning rush",
    "midday": "midday",
    "pm_peak": "evening rush",
    "evening": "evening",
    "night": "overnight",
    "weekend": "weekend",
}


@dataclass
class Estimate:
    n_events: int
    level: str  # line_band_cause, line_band, line, band, all, none
    group: str  # human-readable group, e.g. "A line, morning rush"
    confidence: str  # ok, low, none
    median_min: float | None = None
    p75_min: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WaitEstimate:
    duration: Estimate
    elapsed_min: float
    remaining_median_min: float | None
    remaining_p75_min: float | None
    clearing: Estimate

    def to_dict(self) -> dict:
        return {
            "duration": self.duration.to_dict(),
            "elapsed_min": self.elapsed_min,
            "remaining_median_min": self.remaining_median_min,
            "remaining_p75_min": self.remaining_p75_min,
            "clearing": self.clearing.to_dict(),
        }


def _q(values: np.ndarray, q: float) -> float:
    return round(float(np.quantile(values, q)), 1)


class RecoveryModel:
    def __init__(self, history: pd.DataFrame, settings: Settings):
        """``history``: one row per past event with ``route_id``, ``band``, ``cause``,
        ``duration_min`` and ``clearing_min`` (NaN when the surface never recovered)."""
        self.h = history.reset_index(drop=True)
        self.min_events = int(settings.recovery_min_events)

    @classmethod
    def from_store(cls, con, settings: Settings, now) -> RecoveryModel:
        since = pd.Timestamp(now) - pd.Timedelta(days=settings.recovery_history_days)
        ev = con.execute(
            """
            SELECT event_id, route_id, start_utc, end_utc, duration_min, linked_alerts,
                   surface_recovery_status, surface_recovered_utc
            FROM events
            WHERE status = 'ended' AND end_reason = 'recovered' AND start_utc >= $since
            """,
            {"since": since},
        ).df()
        return cls(history_frame(ev), settings)

    def _pick(self, column: str, route: str, band: str, cause: str | None) -> tuple:
        h = self.h[self.h[column].notna()] if column == "clearing_min" else self.h
        levels = []
        if cause:
            same = (h.route_id == route) & (h.band == band) & (h.cause == cause)
            levels.append(("line_band_cause", same))
        levels += [
            ("line_band", (h.route_id == route) & (h.band == band)),
            ("line", h.route_id == route),
            ("band", h.band == band),
            ("all", pd.Series(True, index=h.index)),
        ]
        for i, (level, mask) in enumerate(levels):
            values = h.loc[mask, column].to_numpy(dtype=float)
            if len(values) >= self.min_events or level == "all":
                if len(values) == 0:
                    break
                # Any fallback to a broader group means low confidence.
                confident = i == 0 and len(values) >= self.min_events
                return level, values, "ok" if confident else "low"
        return "none", np.array([]), "none"

    @staticmethod
    def _group_text(level: str, route: str, band: str, cause: str | None) -> str:
        cause_text = cause.replace("_", " ") if cause else ""
        parts = {
            "line_band_cause": f"{route} line, {BAND_TEXT[band]}, {cause_text}",
            "line_band": f"{route} line, {BAND_TEXT[band]}",
            "line": f"{route} line",
            "band": f"all lines, {BAND_TEXT[band]}",
            "all": "all lines",
            "none": "no past events",
        }
        return parts[level]

    def _estimate(self, column, route, band, cause) -> tuple[Estimate, np.ndarray]:
        level, values, confidence = self._pick(column, route, band, cause)
        est = Estimate(len(values), level, self._group_text(level, route, band, cause), confidence)
        if len(values):
            est.median_min, est.p75_min = _q(values, 0.5), _q(values, 0.75)
        return est, values

    def estimate(
        self, route: str, start_utc, cause: str | None, elapsed_min: float
    ) -> WaitEstimate:
        band = time_band(start_utc)
        duration, values = self._estimate("duration_min", route, band, cause)
        rem_med = rem_p75 = None
        if len(values):
            longer = values[values > elapsed_min] - elapsed_min
            if len(longer) >= MIN_CONDITIONAL_EVENTS:
                rem_med, rem_p75 = _q(longer, 0.5), _q(longer, 0.75)
            else:
                # Already longer than (almost) every similar event: no reliable
                # conditional estimate; use what is left of the overall quantiles.
                rem_med = round(max(0.0, duration.median_min - elapsed_min), 1)
                rem_p75 = round(max(0.0, duration.p75_min - elapsed_min), 1)
                duration.confidence = "low" if duration.confidence != "none" else "none"
        clearing, _ = self._estimate("clearing_min", route, band, cause)
        return WaitEstimate(duration, round(float(elapsed_min), 1), rem_med, rem_p75, clearing)


def history_frame(events: pd.DataFrame) -> pd.DataFrame:
    """Features of past events for the recovery model."""
    cols = ["event_id", "route_id", "band", "cause", "duration_min", "clearing_min"]
    if events.empty:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(
        {
            "event_id": events["event_id"],
            "route_id": events["route_id"],
            "band": [time_band(t) for t in events["start_utc"]],
            "cause": [cause_category(_alerts(a)) for a in events["linked_alerts"]],
            "duration_min": events["duration_min"].astype(float),
        }
    )
    rec = pd.to_datetime(events["surface_recovered_utc"])
    clearing = (rec - pd.to_datetime(events["end_utc"])).dt.total_seconds() / 60.0
    ok = events["surface_recovery_status"].eq("recovered") & clearing.notna()
    out["clearing_min"] = clearing.where(ok).clip(lower=0.0)
    return out[cols]


def _alerts(value) -> list[dict]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    return [dict(a) for a in value]
