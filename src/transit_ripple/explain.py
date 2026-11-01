"""Rule-based plain-English event summaries (no language model).

Every number in a summary comes from the event record, and is also returned in
``cited_metrics`` exactly as printed, so tests can check that a summary never
contains a number that is not in the data. Alert text is quoted verbatim.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

import pandas as pd

from .config import AGENCY_TIMEZONE

METRIC_LABELS = {
    "delay_median_min": "median",
    "delay_mean_min": "average",
    "delay_p90_min": "90th-percentile",
    "delay_max_min": "maximum",
}
DIRECTION_LABELS = {"N": "northbound", "S": "southbound"}
# Stand-alone numbers and clock times (digits inside words such as "p90" are not numbers).
NUMBER_RE = re.compile(r"(?<![\w.])(?:\d{1,2}:\d{2}|\d+(?:\.\d+)?)(?![\w])")
QUOTED_RE = re.compile(r'"[^"]*"')


def _local(ts) -> pd.Timestamp:
    return pd.Timestamp(ts).tz_localize("UTC").tz_convert(ZoneInfo(AGENCY_TIMEZONE))


def _num(value, digits: int = 1) -> str:
    v = round(float(value), digits)
    return f"{v:.{digits}f}" if digits else f"{int(round(v))}"


def _listify(value) -> list:
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    return list(value)


def _present(value) -> bool:
    return value is not None and not (isinstance(value, float) and pd.isna(value))


def explain(event: dict) -> tuple[str, dict]:
    """Return (summary, cited_metrics) for one event record."""
    cited: dict[str, str] = {}

    def cite(name: str, text: str) -> str:
        cited[name] = text
        return text

    route = cite("route_id", str(event["route_id"]))
    radius = cite("radius_m", _num(event.get("neighbor_radius_m") or 500, 0))
    start = _local(event["start_utc"])
    end = _local(event["end_utc"])
    metric = METRIC_LABELS.get(event.get("metric"), event.get("metric", "delay"))
    threshold = cite("threshold_min", _num(event["threshold_min"]))
    duration = cite("duration_min", _num(event["duration_min"], 0))
    day = cite("date_local", f"{start:%a %b} {start.day}")
    start_txt = cite("start_local", f"{start:%H:%M}")
    parts = []
    if event.get("status") == "ongoing":
        when = f"since {start_txt} ET on {day} ({duration} min so far)"
        head = f"{route} train delays are ongoing"
    else:
        end_txt = cite("end_local", f"{end:%H:%M}")
        when = f"from {start_txt} to {end_txt} ET on {day} ({duration} min)"
        head = f"{route} train delays"
        if event.get("end_reason") == "data_gap":
            head += " (end time uncertain: subway data stopped)"
    sentence = (
        f"{head}: {when}, the {metric} train delay on the {route} line was at least {threshold} min"
    )
    if _present(event.get("peak_delay_min")) and _present(event.get("peak_utc")):
        peak = cite("peak_delay_min", _num(event["peak_delay_min"]))
        peak_at = cite("peak_local", f"{_local(event['peak_utc']):%H:%M}")
        direction = DIRECTION_LABELS.get(event.get("peak_direction") or "", "")
        sentence += f", peaking at {peak} min at {peak_at}" + (
            f" ({direction})" if direction else ""
        )
    parts.append(sentence + ".")

    names = _listify(event.get("affected_station_names"))
    delays = _listify(event.get("affected_station_max_delay_min"))
    if names:
        shown = []
        for i, (name, delay) in enumerate(list(zip(names, delays, strict=False))[:3]):
            name = cite(f"station_{i}_name", str(name))
            shown.append(
                f"{name} (up to {cite(f'station_{i}_max_delay_min', _num(delay))} min late)"
            )
        parts.append("Most affected stations: " + ", ".join(shown) + ".")
    else:
        parts.append("No station-level arrival observations were available for this event.")

    n_links = int(event.get("traffic_n_links") or 0)
    speed = event.get("traffic_speed_mph")
    if n_links and _present(speed):
        links = cite("traffic_n_links", str(n_links))
        speed_txt = cite("traffic_speed_mph", _num(speed))
        base = event.get("traffic_baseline_mph")
        change = event.get("traffic_change_pct")
        if _present(base) and _present(change):
            base_txt = cite("traffic_baseline_mph", _num(base))
            pct = cite("traffic_change_pct_abs", _num(abs(change), 0))
            side = "below" if change < 0 else "above"
            parts.append(
                f"Traffic on {links} DOT road link(s) within {radius} m of the most affected "
                f"stations averaged {speed_txt} mph, {pct}% {side} the usual {base_txt} mph for "
                "this weekday "
                "and time."
            )
        else:
            parts.append(
                f"Traffic on {links} DOT road link(s) within {radius} m averaged {speed_txt} mph; "
                "there is no baseline for this weekday and time yet."
            )
    else:
        parts.append(f"No fresh DOT traffic speed data was available within {radius} m.")

    n_bike = int(event.get("bikes_n_stations") or 0)
    bikes = event.get("bikes_available")
    if n_bike and _present(bikes):
        stations_txt = cite("bikes_n_stations", str(n_bike))
        bikes_txt = cite("bikes_available", _num(bikes, 0))
        base = event.get("bikes_baseline")
        change = event.get("bikes_change")
        if _present(base) and _present(change):
            base_txt = cite("bikes_baseline", _num(base, 0))
            diff = cite("bikes_change_abs", _num(abs(change), 0))
            pct = event.get("bikes_change_pct")
            pct_txt = (
                f" ({cite('bikes_change_pct_abs', _num(abs(pct), 0))}%)" if _present(pct) else ""
            )
            side = "fewer" if change < 0 else "more"
            parts.append(
                f"About {bikes_txt} Citi Bikes were available at {stations_txt} station(s) within "
                f"{radius} m, {diff}{pct_txt} {side} than the usual {base_txt}."
            )
        else:
            parts.append(
                f"About {bikes_txt} Citi Bikes were available at {stations_txt} station(s) within "
                f"{radius} m; there is no baseline for this weekday and time yet."
            )
    else:
        parts.append(f"No fresh Citi Bike availability data was available within {radius} m.")

    low = [
        label
        for label, key in (
            ("traffic", "traffic_baseline_confidence"),
            ("bike", "bikes_baseline_confidence"),
        )
        if event.get(key) == "low"
    ]
    if low:
        parts.append(
            f"The {' and '.join(low)} baseline(s) rest on little history, so these comparisons "
            "are indicative only."
        )

    alerts = _listify(event.get("linked_alerts"))
    unplanned = [a for a in alerts if not a.get("is_planned")]
    planned = [a for a in alerts if a.get("is_planned")]
    if unplanned:
        a = unplanned[0]
        text = " ".join((a.get("header_text") or "").replace('"', "'").split())
        if len(text) > 160:
            text = text[:157].rstrip() + "..."
        parts.append(f'MTA alert ({a.get("alert_type") or "alert"}): "{text}"')
    if planned:
        parts.append(
            f"{cite('n_planned_alerts', str(len(planned)))} planned-work notice(s) also applied to "
            "this line."
        )
    if not alerts:
        parts.append("No MTA service alert matched this line and time.")
    return " ".join(parts), cited


def uncited_numbers(summary: str, cited: dict) -> list[str]:
    """Numbers in the summary (outside quoted alert text) that are not cited."""
    allowed = {tok for value in cited.values() for tok in NUMBER_RE.findall(str(value))}
    text = QUOTED_RE.sub("", summary)
    return [tok for tok in NUMBER_RE.findall(text) if tok not in allowed]
