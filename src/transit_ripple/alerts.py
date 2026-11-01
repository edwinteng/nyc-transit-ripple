"""MTA subway service alerts (GTFS-Realtime alerts feed with the Mercury extension)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from google.protobuf.message import DecodeError

from .proto import schema

ALERT_COLUMNS = [
    "alert_id",
    "alert_type",
    "is_planned",
    "created_ts",
    "updated_ts",
    "active_periods",
    "active_start_ts",
    "active_end_ts",
    "route_ids",
    "station_ids",
    "header_text",
    "description_text",
]


class AlertsParseError(ValueError):
    """Raised when the alerts payload is not a valid GTFS-Realtime FeedMessage."""


@dataclass
class ParsedAlerts:
    header_ts: int | None
    n_entities: int = 0
    alerts: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=ALERT_COLUMNS))


def station_of(stop_id: str) -> str:
    """Parent station id of an NYCT stop id: ``A32N`` -> ``A32``, ``A32`` -> ``A32``.

    NYCT station ids end in a digit; a trailing N/S is the platform direction.
    """
    if len(stop_id) >= 3 and stop_id[-1] in "NS" and stop_id[-2].isdigit():
        return stop_id[:-1]
    return stop_id


def _text(translated, language: str = "en") -> str | None:
    """Pick the plain-text translation (MTA also ships an ``en-html`` variant)."""
    plain = [t for t in translated.translation if t.language in (language, "")]
    chosen = plain[0] if plain else None
    if chosen is None:
        non_html = [t for t in translated.translation if "html" not in t.language]
        chosen = non_html[0] if non_html else None
    return chosen.text.strip() if chosen is not None and chosen.text else None


def parse_alerts(content: bytes, language: str = "en") -> ParsedAlerts:
    """One row per alert: type, times, active periods, routes, stations and text."""
    s = schema()
    try:
        msg = s.FeedMessage.FromString(content)
    except (DecodeError, RuntimeError, ValueError) as exc:
        raise AlertsParseError(f"cannot decode alerts payload: {exc}") from exc
    if not msg.header.gtfs_realtime_version:
        raise AlertsParseError("alerts payload has no GTFS-Realtime header")

    rows = []
    for ent in msg.entity:
        if not ent.HasField("alert"):
            continue
        a = ent.alert
        alert_type = created = updated = None
        if a.HasExtension(s.mercury_alert):
            ext = a.Extensions[s.mercury_alert]
            alert_type = ext.alert_type or None
            created = int(ext.created_at) if ext.created_at else None
            updated = int(ext.updated_at) if ext.updated_at else None
        periods = [
            (int(p.start) if p.start else None, int(p.end) if p.end else None)
            for p in a.active_period
        ]
        starts = [p[0] for p in periods if p[0] is not None]
        open_ended = any(p[1] is None for p in periods) or not periods
        ends = [p[1] for p in periods if p[1] is not None]
        routes = sorted({ie.route_id for ie in a.informed_entity if ie.route_id})
        stations = sorted({station_of(ie.stop_id) for ie in a.informed_entity if ie.stop_id})
        rows.append(
            {
                "alert_id": ent.id,
                "alert_type": alert_type,
                "is_planned": bool(alert_type and alert_type.lower().startswith("planned")),
                "created_ts": created,
                "updated_ts": updated if updated is not None else created,
                "active_periods": periods,
                "active_start_ts": min(starts) if starts else None,
                "active_end_ts": None if open_ended else max(ends),
                "route_ids": routes,
                "station_ids": stations,
                "header_text": _text(a.header_text, language),
                "description_text": _text(a.description_text, language),
            }
        )
    df = pd.DataFrame(rows, columns=ALERT_COLUMNS)
    for col in ("created_ts", "updated_ts", "active_start_ts", "active_end_ts"):
        df[col] = df[col].astype("Int64")
    return ParsedAlerts(
        header_ts=int(msg.header.timestamp) if msg.header.timestamp else None,
        n_entities=len(msg.entity),
        alerts=df,
    )


def is_active(periods: list[tuple[int | None, int | None]], ts: float) -> bool:
    """GTFS-RT rule: no period means always active; open bounds are unbounded."""
    if not periods:
        return True
    return any(
        (start is None or start <= ts) and (end is None or ts <= end) for start, end in periods
    )


def overlaps(
    periods: list[tuple[int | None, int | None]],
    window_start: float,
    window_end: float,
) -> bool:
    """Whether any active period intersects ``[window_start, window_end]``."""
    if not periods:
        return True
    return any(
        (start is None or start <= window_end) and (end is None or end >= window_start)
        for start, end in periods
    )
