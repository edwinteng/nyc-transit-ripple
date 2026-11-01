"""Rule-based plain-English event explanations (no language model).

Each event gets one or two sentences:

1. the subway disruption: line, how late, how long, when, worst station;
2. the surface: how nearby traffic, camera counts and Citi Bikes compared with
   the usual level for that weekday and time, the impact score, and when the
   surface recovered (or why no comparison is possible yet).

Every sentence is returned with the metrics it uses, exactly as printed, so a
check can prove that no sentence contains a number that is not in the data.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from .config import AGENCY_TIMEZONE

# Stand-alone numbers and clock times (digits inside words such as "p90" are not numbers).
NUMBER_RE = re.compile(r"(?<![\w.])(?:\d{1,2}:\d{2}|\d+(?:\.\d+)?)(?![\w])")
QUOTED_RE = re.compile(r'"[^"]*"')

SIGNAL_ORDER = ("traffic", "camera", "bikes")


def _local(ts) -> pd.Timestamp:
    return pd.Timestamp(ts).tz_localize("UTC").tz_convert(ZoneInfo(AGENCY_TIMEZONE))


def _num(value, digits: int = 1) -> str:
    # Round half away from zero (as the dashboard does), not to even: 62.5 -> 63.
    q = Decimal(1).scaleb(-digits)
    d = Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP)
    return str(abs(d) if d == 0 else d)  # never "-0.0"


def _present(value) -> bool:
    if value is None:
        return False
    try:
        return not pd.isna(value)
    except (TypeError, ValueError):
        return True


def _listify(value) -> list:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return list(value)


class _Sentence:
    def __init__(self):
        self.metrics: dict[str, str] = {}

    def cite(self, name: str, text: str) -> str:
        self.metrics[name] = text
        return text


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def subway_sentence(event: dict) -> dict:
    s = _Sentence()
    route = s.cite("route_id", str(event["route_id"]))
    start = _local(event["start_utc"])
    end = _local(event["end_utc"])
    date = s.cite("date_local", f"{start:%a %b} {start.day}")
    start_txt = s.cite("start_local", f"{start:%H:%M}")
    duration = s.cite("duration_min", _num(event["duration_min"], 0))
    if _present(event.get("peak_delay_min")):
        peak = s.cite("peak_delay_min", _num(event["peak_delay_min"]))
        how = f"a median of up to {peak} min late"
    else:
        how = f"at least {s.cite('threshold_min', _num(event['threshold_min']))} min late"
    if event.get("status") == "ongoing":
        text = (
            f"{route} trains have been running {how} for {duration} min, "
            f"since {start_txt} ET on {date}"
        )
    elif event.get("end_reason") == "data_gap":
        text = (
            f"{route} trains ran {how} for at least {duration} min from {start_txt} ET on {date} "
            "(the end time is uncertain because subway data stopped)"
        )
    else:
        end_txt = s.cite("end_local", f"{end:%H:%M}")
        text = (
            f"{route} trains ran {how} for {duration} min, "
            f"from {start_txt} to {end_txt} ET on {date}"
        )
    names = _listify(event.get("affected_station_names"))
    delays = _listify(event.get("affected_station_max_delay_min"))
    if names:
        name = s.cite("station_0_name", str(names[0]))
        if delays and _present(delays[0]):
            worst = s.cite("station_0_max_delay_min", _num(delays[0]))
            text += f", worst at {name} ({worst} min)"
        else:
            text += f", worst at {name}"
    return {"text": text + ".", "metrics": s.metrics}


def _signal_clause(name: str, pct: float, s: _Sentence) -> str:
    amount = s.cite(f"{name}_change_pct_abs", _num(abs(pct), 0))
    if name == "traffic":
        return f"nearby traffic was {amount}% {'slower' if pct < 0 else 'faster'}"
    if name == "bikes":
        return f"Citi Bike availability was {amount}% {'lower' if pct < 0 else 'higher'}"
    return f"cameras counted {amount}% {'more' if pct >= 0 else 'fewer'} vehicles and people"


def surface_sentence(event: dict) -> dict:
    s = _Sentence()
    radius = s.cite("radius_m", _num(event.get("neighbor_radius_m") or 500, 0))
    used = [n for n in SIGNAL_ORDER if n in _listify(event.get("impact_signals_used"))]
    pct = {
        "traffic": event.get("traffic_change_pct"),
        "camera": event.get("camera_change_pct"),
        "bikes": event.get("bikes_change_pct"),
    }
    clauses = [_signal_clause(n, pct[n], s) for n in used if _present(pct[n])]
    score = event.get("impact_score")
    if clauses and _present(score):
        score_txt = s.cite("impact_score", _num(score, 0))
        scale = s.cite("impact_scale", "100")
        text = (
            f"Within {radius} m of the worst-hit stations, {_join(clauses)} than usual for this "
            f"weekday and time (surface impact {score_txt}/{scale})"
        )
        status = event.get("surface_recovery_status")
        if status == "recovered" and _present(event.get("surface_recovered_utc")):
            when = s.cite(
                "surface_recovered_local", f"{_local(event['surface_recovered_utc']):%H:%M}"
            )
            text += f", and the surface had recovered by {when} ET"
        elif status == "not_yet" and _present(event.get("surface_checked_until_utc")):
            when = s.cite(
                "surface_checked_until_local",
                f"{_local(event['surface_checked_until_utc']):%H:%M}",
            )
            text += f", and it had not recovered by {when} ET"
        elif status == "no_impact":
            text += ", so surface conditions stayed close to normal"
        return {"text": text + ".", "metrics": s.metrics}

    observed = []
    if _present(event.get("traffic_speed_mph")) and int(event.get("traffic_n_links") or 0):
        speed = s.cite("traffic_speed_mph", _num(event["traffic_speed_mph"]))
        observed.append(f"traffic averaged {speed} mph")
    if _present(event.get("camera_objects")) and int(event.get("camera_n_cameras") or 0):
        n_cams = s.cite("camera_n_cameras", str(int(event["camera_n_cameras"])))
        objects = s.cite("camera_objects", _num(event["camera_objects"], 0))
        observed.append(f"{n_cams} camera(s) counted about {objects} vehicles and people")
    if _present(event.get("bikes_available")) and int(event.get("bikes_n_stations") or 0):
        bikes = s.cite("bikes_available", _num(event["bikes_available"], 0))
        observed.append(f"{bikes} Citi Bikes were available")
    if observed:
        text = (
            f"Within {radius} m, {_join(observed)}, but there is not enough baseline history yet "
            "to compare them with usual conditions for this weekday and time."
        )
    else:
        text = f"No fresh traffic, camera or Citi Bike data was available within {radius} m."
    return {"text": text, "metrics": s.metrics}


def explain_sentences(event: dict) -> list[dict]:
    """One or two sentences, each with the metrics it cites."""
    return [subway_sentence(event), surface_sentence(event)]


def explain(event: dict) -> tuple[str, dict]:
    """Return (summary, cited_metrics) for one event record."""
    sentences = explain_sentences(event)
    cited: dict = {}
    for s in sentences:
        cited.update(s["metrics"])
    return " ".join(s["text"] for s in sentences), cited


def sentences_of(event: dict) -> list[dict]:
    """Stored sentences of an event (JSON column), or freshly generated ones."""
    raw = event.get("explanation")
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return explain_sentences(event)


def uncited_numbers(text: str, cited: dict) -> list[str]:
    """Numbers in a text (outside quoted alert text) that are not cited."""
    allowed = {tok for value in cited.values() for tok in NUMBER_RE.findall(str(value))}
    plain = QUOTED_RE.sub("", text)
    return [tok for tok in NUMBER_RE.findall(plain) if tok not in allowed]
