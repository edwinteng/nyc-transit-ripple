"""Recommendation replay (M6, part A4).

For every logged recommendation, look at what happened afterwards:

- ``wait``: actual remaining delay = final event end - recommendation time (events
  that ended because service recovered only), vs. the estimated remaining delay;
- ``walk_line``: actual wait = first train *observed* at the target station on the
  suggested lines after the rider could have reached the platform, vs. the
  estimated wait (observations come from polls about a minute apart, so the actual
  time is known to about a minute);
- ``bus``: realized speed = bus speed on the route's segments near the station in
  the next 30 minutes, vs. the speed used in the estimate.

A realized total time is rebuilt for each option (estimated walk + actual wait +
ride at the realized or assumed speed; the crowd penalty is not part of it). Then:

- MAE of the time estimates per option type;
- how often the top-ranked option beat the "obvious option" baseline in realized
  time, when they differ;
- the avoid-the-crowd check, separately: did obvious options flagged as crowded
  actually degrade (bus slower than usual, bikes ran out, train wait longer than
  estimated), compared with obvious options that were not flagged?
"""

from __future__ import annotations

import json

import pandas as pd

from ..config import Settings
from ..events import as_list
from .common import (
    STATUS_INSUFFICIENT,
    STATUS_OK,
    Metric,
    Section,
    bootstrap_mean_ci,
    day,
    set_span,
)

MPH_TO_MPS = 0.44704


def load_recommendations(con, start, end) -> pd.DataFrame:
    return con.execute(
        "SELECT * FROM recommendations WHERE computed_utc >= $s AND computed_utc < $e "
        "ORDER BY computed_utc, event_id, station_id, rank",
        {"s": start, "e": end},
    ).df()


def final_events(con) -> pd.DataFrame:
    return con.execute("SELECT event_id, status, end_reason, end_utc FROM events").df()


def _facts(rec) -> dict:
    try:
        return json.loads(rec["inputs"]).get("facts", {})
    except (TypeError, ValueError):
        return {}


def realized_wait_for_train(con, rec, horizon_min: float) -> float | None:
    """Minutes from reaching the platform to the first observed train of the lines."""
    t0 = pd.Timestamp(rec["computed_utc"]) + pd.Timedelta(minutes=float(rec["est_access_min"]))
    row = con.execute(
        """
        SELECT min(bucket_utc) FROM station_minute
        WHERE station_id = $s AND list_contains($routes, route_id) AND n_trains > 0
          AND bucket_utc >= $t0 AND bucket_utc <= $t1
        """,
        {
            "s": str(rec["target_id"]),
            "routes": [str(r) for r in as_list(rec["routes"])],
            "t0": t0.floor("min"),
            "t1": t0 + pd.Timedelta(minutes=horizon_min),
        },
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return max(0.0, (pd.Timestamp(row[0]) - t0).total_seconds() / 60.0)


def realized_bus_speed(con, rec, window_min: float = 30.0) -> float | None:
    t0 = pd.Timestamp(rec["computed_utc"])
    row = con.execute(
        """
        SELECT sum(distance_m), sum(duration_s) FROM bus_minute
        WHERE list_contains($routes, route_id) AND n_samples > 0
          AND bucket_utc >= $t0 AND bucket_utc < $t1
          AND stop_id IN (SELECT neighbor_id FROM station_neighbors
                          WHERE station_id = $s AND neighbor_type = 'bus_stop')
        """,
        {
            "routes": [str(r) for r in as_list(rec["routes"])],
            "t0": t0,
            "t1": t0 + pd.Timedelta(minutes=window_min),
            "s": str(rec["station_id"]),
        },
    ).fetchone()
    if row is None or not row[1]:
        return None
    return row[0] / row[1] / MPH_TO_MPS


def bikes_later(con, rec, settings: Settings) -> tuple[int | None, int | None]:
    """(bikes at the suggested dock, bikes at all nearby docks) 15 minutes later."""
    t = pd.Timestamp(rec["computed_utc"]) + pd.Timedelta(minutes=settings.rec_crowd_bike_window_min)
    df = con.execute(
        """
        SELECT b.station_id, b.bikes_available FROM bike_minute b
        WHERE b.bucket_utc BETWEEN $lo AND $t
          AND b.station_id IN (SELECT neighbor_id FROM station_neighbors
                               WHERE station_id = $s AND neighbor_type = 'bike_station')
        QUALIFY row_number() OVER (PARTITION BY b.station_id ORDER BY b.bucket_utc DESC) = 1
        """,
        {"lo": t - pd.Timedelta(minutes=10), "t": t, "s": str(rec["station_id"])},
    ).df()
    if df.empty:
        return None, None
    dock = df.loc[df["station_id"] == str(rec["target_id"]), "bikes_available"]
    return (int(dock.iloc[0]) if len(dock) else None), int(df["bikes_available"].sum())


def realized_outcomes(con, settings: Settings, recs: pd.DataFrame) -> pd.DataFrame:
    """One row per logged option with its realized wait/speed/total (NaN if unknown)."""
    if recs.empty:
        return recs.assign(realized_wait_min=[], realized_speed_mph=[], realized_total_min=[])
    ev = final_events(con).set_index("event_id")
    km = settings.rec_reference_trip_km
    out = []
    for r in recs.to_dict("records"):
        wait = speed = total = None
        degraded = None
        access = float(r["est_access_min"]) if r["est_access_min"] is not None else None
        ride = r["est_ride_min"]
        if r["option_type"] == "wait":
            e = ev.loc[r["event_id"]] if r["event_id"] in ev.index else None
            if e is not None and e["status"] == "ended" and e["end_reason"] == "recovered":
                wait = max(
                    0.0,
                    (pd.Timestamp(e["end_utc"]) - pd.Timestamp(r["computed_utc"])).total_seconds()
                    / 60.0,
                )
                total = wait + ride
        elif r["option_type"] == "walk_line" and access is not None:
            wait = realized_wait_for_train(con, r, settings.eval_replay_horizon_min)
            if wait is not None:
                total = access + wait + ride
                degraded = wait > float(r["est_wait_min"]) + settings.eval_walk_degrade_min
        elif r["option_type"] == "bus" and access is not None:
            speed = realized_bus_speed(con, r)
            if speed:
                total = access + float(r["est_wait_min"]) + km * 1000 / (speed * MPH_TO_MPS) / 60
                usual = _facts(r).get("baseline_mph")
                if usual:
                    degraded = speed <= usual * (1 - settings.rec_crowd_bus_slowdown_pct / 100)
        elif r["option_type"] == "bike":
            dock, nearby = bikes_later(con, r, settings)
            if dock is not None:
                before = _facts(r).get("bikes_nearby")
                degraded = dock < settings.rec_min_bikes or (
                    before is not None
                    and before > 0
                    and nearby is not None
                    and (before - nearby) / before * 100 >= settings.rec_crowd_bike_depletion_pct
                )
                total = (
                    r["est_total_min"] - (r["penalty_min"] or 0.0)
                    if dock >= settings.rec_min_bikes
                    else None
                )
        est_speed = (
            km * 1000 / (float(ride) * 60) / MPH_TO_MPS
            if r["option_type"] == "bus" and ride
            else None
        )
        out.append(
            {
                **r,
                "realized_wait_min": wait,
                "realized_speed_mph": speed,
                "estimated_speed_mph": est_speed,
                "realized_total_min": total,
                "degraded": degraded,
            }
        )
    return pd.DataFrame(out)


def top_vs_obvious(outcomes: pd.DataFrame) -> dict:
    """Per recommendation set: did rank 1 beat the obvious option in realized time?"""
    beat = lost = same = unknown = 0
    for _, g in outcomes.groupby(["run_id", "event_id", "station_id"]):
        top = g[g["rank"] == 1]
        obvious = g[g["is_obvious"].fillna(False).astype(bool)]
        if top.empty or obvious.empty:
            unknown += 1
            continue
        t, o = top.iloc[0], obvious.iloc[0]
        if t["option_id"] == o["option_id"]:
            same += 1
            continue
        if pd.isna(t["realized_total_min"]) or pd.isna(o["realized_total_min"]):
            unknown += 1
            continue
        if t["realized_total_min"] < o["realized_total_min"]:
            beat += 1
        else:
            lost += 1
    return {"beat": beat, "lost": lost, "same_option": same, "unknown": unknown}


def evaluate_recommendations(con, settings: Settings, start, end) -> Section:
    recs = load_recommendations(con, start, end)
    section = Section(
        "recommendations",
        "Recommendation replay",
        STATUS_OK,
        day(start),
        day(end - pd.Timedelta(seconds=1)),
        "",
        notes=[
            "Each logged recommendation is replayed against what happened next: the real "
            "recovery time of the event, the first train observed at the suggested station, "
            "and the bus speed measured in the next 30 minutes.",
            "Trains are observed at poll times (about a minute apart, with gaps when GitHub "
            "delays runs), so realized waits are approximate.",
            "There is no destination, so 'beating the obvious option' compares the time to "
            f"get {settings.rec_reference_trip_km:g} km away.",
        ],
    )
    if recs.empty:
        section.status = STATUS_INSUFFICIENT
        section.summary = (
            "No recommendations were logged in this period (they are only made while a "
            "disruption is ongoing)."
        )
        return section
    set_span(section, recs["computed_utc"])
    out = realized_outcomes(con, settings, recs)
    n_sets = out.groupby(["run_id", "event_id", "station_id"]).ngroups
    for opt, est_col, real_col, unit, label in (
        ("wait", "est_wait_min", "realized_wait_min", "min", "Wait: remaining delay"),
        ("walk_line", "est_wait_min", "realized_wait_min", "min", "Other line: train wait"),
        ("bus", "estimated_speed_mph", "realized_speed_mph", "mph", "Bus: speed"),
    ):
        g = out[(out["option_type"] == opt) & out[real_col].notna() & out[est_col].notna()]
        err = (g[est_col].astype(float) - g[real_col].astype(float)).abs()
        mae, lo, hi = bootstrap_mean_ci(
            err.tolist(), settings.eval_bootstrap_samples, settings.eval_seed
        )
        bias = float((g[est_col] - g[real_col]).mean()) if len(g) else None
        section.metrics.append(
            Metric(
                f"mae_{opt}",
                f"MAE, {label}",
                mae,
                len(g),
                unit,
                lo,
                hi,
                note=None if bias is None else f"bias {bias:+.1f} {unit} (estimate - actual)",
            )
        )
    tvo = top_vs_obvious(out)
    compared = tvo["beat"] + tvo["lost"]
    section.metrics.append(
        Metric(
            "top_beats_obvious",
            "Top-ranked option faster than the obvious option",
            tvo["beat"] / compared if compared else None,
            compared,
            "share",
            note=f"{tvo['same_option']} set(s) where the top option was the obvious one; "
            f"{tvo['unknown']} without a realized outcome",
        )
    )
    obvious = out[out["is_obvious"].fillna(False).astype(bool) & out["degraded"].notna()]
    flagged = obvious[obvious["crowded"].fillna(False).astype(bool)]
    calm = obvious[~obvious["crowded"].fillna(False).astype(bool)]
    section.metrics += [
        Metric(
            "crowd_flag_degraded",
            "Flagged obvious options that actually degraded",
            float(flagged["degraded"].astype(bool).mean()) if len(flagged) else None,
            len(flagged),
            "share",
        ),
        Metric(
            "crowd_noflag_degraded",
            "Unflagged obvious options that degraded",
            float(calm["degraded"].astype(bool).mean()) if len(calm) else None,
            len(calm),
            "share",
        ),
    ]
    section.tables["by_option_type"] = [
        {
            "option_type": t,
            "logged": int((out["option_type"] == t).sum()),
            "with_outcome": int(
                ((out["option_type"] == t) & out["realized_total_min"].notna()).sum()
            ),
            "ranked_first": int(((out["option_type"] == t) & (out["rank"] == 1)).sum()),
        }
        for t in ("walk_line", "bike", "bus", "wait")
    ]
    section.tables["top_vs_obvious"] = [tvo]
    section.summary = (
        f"{len(out)} options in {n_sets} recommendation set(s) were replayed; "
        f"{int(out['realized_total_min'].notna().sum())} have a realized outcome."
    )
    if compared:
        section.summary += (
            f" The top-ranked option beat the obvious option in {tvo['beat']} of {compared} "
            "comparable cases."
        )
    return section


__all__ = ["evaluate_recommendations", "realized_outcomes", "top_vs_obvious"]
