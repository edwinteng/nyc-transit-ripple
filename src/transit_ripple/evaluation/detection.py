"""Event detection vs. official MTA service alerts (M6, part A1).

Ground truth: unplanned MTA subway alerts. An alert counts once per line it names
(an alert for the 2 and 3 is two "alert-lines"). The alert time is when it was
created (or first seen in the feed if creation is unknown).

A detected event matches an alert-line of the same line whose alert time lies
within ``[event start - tolerance, event end + tolerance]``. Matching is one to
one, closest alert time to the event start first.

- precision = matched events / events;
- recall = matched alert-lines / alert-lines;
- time to detect = event start - alert time (negative: detected before the alert).

Alerts are an imperfect ground truth: they include planned work announced as
delays, are sometimes missing for short delays, are often posted late or kept
long after service recovered, and name lines that were only indirectly affected.
Unmatched events are listed for manual review.
"""

from __future__ import annotations

import pandas as pd

from ..config import SUBWAY_FEEDS, Settings
from .common import STATUS_INSUFFICIENT, STATUS_OK, Metric, Section, day, ratio, set_span

SUBWAY_ROUTES = {r for spec in SUBWAY_FEEDS.values() for r in spec.routes}


def load_events(con, start, end) -> pd.DataFrame:
    return con.execute(
        """
        SELECT event_id, route_id, status, end_reason, start_utc, end_utc, duration_min,
               peak_delay_min, impact_score
        FROM events WHERE start_utc >= $start AND start_utc < $end ORDER BY start_utc
        """,
        {"start": start, "end": end},
    ).df()


def load_alert_lines(con, start, end) -> pd.DataFrame:
    """One row per (unplanned alert, subway line) with the alert time."""
    df = con.execute(
        """
        WITH first_seen AS (
            SELECT alert_id, min(bucket_utc) AS first_seen_utc FROM alert_presence
            GROUP BY alert_id
        ),
        latest AS (
            SELECT * FROM alerts
            QUALIFY row_number() OVER (PARTITION BY alert_id ORDER BY updated_utc DESC) = 1
        )
        SELECT a.alert_id, a.alert_type, a.header_text, a.route_ids, a.is_planned,
               coalesce(a.created_utc, f.first_seen_utc) AS alert_utc
        FROM latest a LEFT JOIN first_seen f USING (alert_id)
        WHERE NOT coalesce(a.is_planned, false)
        """
    ).df()
    rows = []
    for r in df.to_dict("records"):
        t = r["alert_utc"]
        if t is None or pd.isna(t) or not (pd.Timestamp(start) <= t < pd.Timestamp(end)):
            continue
        for route in sorted(set(r["route_ids"] if r["route_ids"] is not None else [])):
            if route in SUBWAY_ROUTES:
                rows.append(
                    {
                        "alert_id": r["alert_id"],
                        "route_id": route,
                        "alert_utc": pd.Timestamp(t),
                        "alert_type": r["alert_type"],
                        "header_text": r["header_text"],
                    }
                )
    return pd.DataFrame(
        rows, columns=["alert_id", "route_id", "alert_utc", "alert_type", "header_text"]
    )


def observable(con, alerts: pd.DataFrame, window_min: float = 30.0) -> pd.Series:
    """True for alert-lines whose line had usable subway data within ``window_min``
    minutes after the alert: only those could have been detected."""
    if alerts.empty:
        return pd.Series(dtype=bool)
    con.register("_alert_lines", alerts.reset_index(drop=True).reset_index())
    df = con.execute(
        f"""
        SELECT a.index, EXISTS (
            SELECT 1 FROM line_minute l
            WHERE l.route_id = a.route_id AND l.status = 'ok'
              AND l.bucket_utc BETWEEN a.alert_utc
                                   AND a.alert_utc + INTERVAL {int(window_min)} MINUTE
        ) AS ok
        FROM _alert_lines a ORDER BY a.index
        """
    ).df()
    return pd.Series(df["ok"].to_numpy(), index=alerts.index)


def match_events_to_alerts(
    events: pd.DataFrame, alerts: pd.DataFrame, tolerance_min: float
) -> pd.DataFrame:
    """One-to-one matches (event_id, alert_id, route_id, time_to_detect_min)."""
    cols = ["event_id", "alert_id", "route_id", "time_to_detect_min"]
    if events.empty or alerts.empty:
        return pd.DataFrame(columns=cols)
    tol = pd.Timedelta(minutes=tolerance_min)
    pairs = events.merge(alerts, on="route_id")
    ok = (pairs["alert_utc"] >= pairs["start_utc"] - tol) & (
        pairs["alert_utc"] <= pairs["end_utc"] + tol
    )
    pairs = pairs[ok].copy()
    if pairs.empty:
        return pd.DataFrame(columns=cols)
    pairs["ttd"] = (pairs["start_utc"] - pairs["alert_utc"]).dt.total_seconds() / 60.0
    pairs["dist"] = pairs["ttd"].abs()
    used_events, used_alerts, out = set(), set(), []
    for r in pairs.sort_values(["dist", "event_id", "alert_id"]).itertuples(index=False):
        key = (r.alert_id, r.route_id)
        if r.event_id in used_events or key in used_alerts:
            continue
        used_events.add(r.event_id)
        used_alerts.add(key)
        out.append(
            {
                "event_id": r.event_id,
                "alert_id": r.alert_id,
                "route_id": r.route_id,
                "time_to_detect_min": round(float(r.ttd), 1),
            }
        )
    return pd.DataFrame(out, columns=cols)


def evaluate_detection(con, settings: Settings, start, end) -> Section:
    events = load_events(con, start, end)
    all_alerts = load_alert_lines(con, start, end)
    alerts = all_alerts[observable(con, all_alerts)] if len(all_alerts) else all_alerts
    n_unobserved = len(all_alerts) - len(alerts)
    matches = match_events_to_alerts(events, alerts, settings.eval_alert_tolerance_min)
    section = Section(
        "detection",
        "Event detection vs. official MTA alerts",
        STATUS_OK,
        day(start),
        day(end - pd.Timedelta(seconds=1)),
        "",
        notes=[
            "MTA alerts are an imperfect ground truth: planned work is sometimes posted as "
            "delays, short delays often get no alert, alerts can be posted late or kept "
            "after service recovered, and they name lines that were only indirectly affected.",
            f"An alert matches an event of the same line if it appeared between "
            f"{settings.eval_alert_tolerance_min:g} min before the event start and "
            f"{settings.eval_alert_tolerance_min:g} min after its end; one to one.",
            "Unmatched events are listed for manual review: they may be real delays without "
            "an alert, or false detections.",
        ],
    )
    if n_unobserved:
        section.notes.append(
            f"{n_unobserved} alert-line(s) were left out of recall because no subway data was "
            "collected for that line in the 30 minutes after the alert (pipeline not running)."
        )
    if events.empty and alerts.empty:
        section.status = STATUS_INSUFFICIENT
        section.summary = "No detected events and no unplanned alerts in this period."
        return section
    set_span(section, events["start_utc"], alerts["alert_utc"])
    n_ev, n_al, n_m = len(events), len(alerts), len(matches)
    precision, recall = ratio(n_m, n_ev), ratio(n_m, n_al)
    ttd = matches["time_to_detect_min"].astype(float)
    section.metrics = [
        Metric("precision", "Precision (events matching an alert)", precision, n_ev, "share"),
        Metric("recall", "Recall (alerts with a matching event)", recall, n_al, "share"),
        Metric(
            "ttd_median",
            "Median time to detect (event start - alert time)",
            float(ttd.median()) if n_m else None,
            n_m,
            "min",
            note="negative: the event was detected before the alert appeared",
        ),
        Metric("ttd_mean", "Mean time to detect", float(ttd.mean()) if n_m else None, n_m, "min"),
    ]
    per_line = []
    for route in sorted(set(events["route_id"]) | set(alerts["route_id"]), key=_order):
        e = int((events["route_id"] == route).sum())
        a = int((alerts["route_id"] == route).sum())
        m = matches[matches["route_id"] == route]
        per_line.append(
            {
                "line": route,
                "events": e,
                "alerts": a,
                "matched": len(m),
                "precision": ratio(len(m), e),
                "recall": ratio(len(m), a),
                "ttd_median_min": float(m["time_to_detect_min"].median()) if len(m) else None,
            }
        )
    section.tables["per_line"] = per_line
    unmatched = events[~events["event_id"].isin(matches["event_id"])]
    section.tables["unmatched_events"] = [
        {
            "event_id": r["event_id"],
            "line": r["route_id"],
            "start_utc": pd.Timestamp(r["start_utc"]).strftime("%Y-%m-%d %H:%M"),
            "duration_min": r["duration_min"],
            "peak_delay_min": r["peak_delay_min"],
        }
        for r in unmatched.sort_values("start_utc", ascending=False).head(50).to_dict("records")
    ]
    if precision is None or recall is None:
        section.summary = (
            f"{n_ev} detected event(s) and {n_al} unplanned alert-line(s) in this period; "
            "precision or recall is undefined."
        )
    else:
        section.summary = (
            f"{n_m} of {n_ev} detected events matched an unplanned MTA alert (precision "
            f"{precision:.0%}), and {n_m} of {n_al} alert-lines had a matching event "
            f"(recall {recall:.0%})."
        )
        if n_m:
            section.summary += f" Median time to detect: {float(ttd.median()):+.1f} min."
    return section


def _order(r: str):
    return (0, int(r)) if r.isdigit() else (1, r)


__all__ = ["evaluate_detection", "match_events_to_alerts", "load_alert_lines"]
