"""Surface impact validity: a placebo test (M6, part A2).

Does the street near an affected station really change during a subway event, or
would we see the same "impact" at any time or place? For every ended event and
each surface signal near its focal stations:

- ``E``: mean during the event window;
- ``A``: mean at the **same stations** in matched non-event windows: the same local
  time of day on earlier days of the same kind (weekday or weekend), up to
  ``eval_placebo_days_back`` days back, skipping windows that overlap an event
  touching these stations;
- **control stations**: up to ``eval_control_stations`` stations at least
  ``eval_control_min_distance_m`` from every station affected by any event active
  in the window, each compared with its own matched non-event windows.

Two per-event differences (percent):

- (a) same stations: ``E / A - 1``;
- (b) vs. controls: ``(E / A) - mean over controls (C / C_matched)`` (a
  difference-in-differences that removes city-wide conditions such as weather).

The report gives the mean difference across events with a 95% bootstrap interval
and says plainly whether the interval excludes zero. Speeds are lower and bikes
fewer under impact, so an effect shows as a negative difference.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Settings
from ..events import as_list
from ..geo import haversine_m
from ..storage import entity_filled
from .common import (
    STATUS_INSUFFICIENT,
    STATUS_OK,
    Metric,
    Section,
    bootstrap_mean_ci,
    day,
    set_span,
)

# signal: (neighbor type or source, table, id column, time column, value, filter, label)
SIGNALS = {
    "traffic_speed": (
        "traffic_link",
        "traffic_minute",
        "link_id",
        "observed_utc",
        "speed_mph",
        "is_valid",
        "Traffic speed",
    ),
    "bus_speed": (
        "bus_stop",
        "bus_minute",
        "stop_id",
        "bucket_utc",
        "speed_mph",
        "n_samples > 0",
        "Bus speed",
    ),
    "bikes_available": (
        "bike_station",
        "bike_minute",
        "station_id",
        "bucket_utc",
        "bikes_available",
        "is_installed AND is_renting",
        "Citi Bikes available",
    ),
}


def compare(diffs, settings: Settings) -> dict:
    """Mean of per-event differences with a bootstrap 95% interval."""
    mean, lo, hi = bootstrap_mean_ci(
        list(diffs), settings.eval_bootstrap_samples, settings.eval_seed
    )
    n = len([d for d in diffs if d is not None and not np.isnan(d)])
    significant = lo is not None and hi is not None and (lo > 0 or hi < 0)
    return {"mean": mean, "ci_low": lo, "ci_high": hi, "n": n, "significant": significant}


class SignalReader:
    """Mean of one signal over a set of entities and a time window."""

    def __init__(self, con, settings: Settings, signal: str):
        self.con = con
        self.settings = settings
        self.kind, self.table, self.id_col, self.time_col, self.value, self.where, _ = SIGNALS[
            signal
        ]
        self._entities: dict[str, list[str]] = {}

    def entities(self, station_ids: list[str]) -> list[str]:
        key = ",".join(sorted(station_ids))
        if key not in self._entities:
            df = self.con.execute(
                "SELECT DISTINCT neighbor_id FROM station_neighbors "
                "WHERE neighbor_type = $k AND list_contains($ids, station_id)",
                {"k": self.kind, "ids": list(station_ids)},
            ).df()
            self._entities[key] = sorted(df["neighbor_id"].astype(str))
        return self._entities[key]

    def mean(self, entity_ids: list[str], start, end) -> float | None:
        if not entity_ids:
            return None
        g = entity_filled(
            self.con,
            self.table,
            self.id_col,
            self.time_col,
            [self.value],
            entity_ids,
            pd.Timestamp(start).to_pydatetime(),
            (pd.Timestamp(end) - pd.Timedelta(minutes=1)).to_pydatetime(),
            min(self.settings.surface_fill_max_age_min, 15),
            where=self.where,
        )
        v = pd.to_numeric(g[self.value], errors="coerce") if len(g) else pd.Series(dtype=float)
        return float(v.mean()) if v.notna().any() else None


def matched_windows(event: dict, busy: pd.DataFrame, settings: Settings) -> list[tuple]:
    """Earlier windows at the same local time on days of the same kind, with no event
    touching these stations."""
    start, end = pd.Timestamp(event["start_utc"]), pd.Timestamp(event["end_utc"])
    weekend = start.tz_localize("UTC").tz_convert("America/New_York").day_of_week >= 5
    focal = set(as_list(event.get("focal_station_ids")))
    out = []
    for d in range(1, settings.eval_placebo_days_back + 1):
        s, e = start - pd.Timedelta(days=d), end - pd.Timedelta(days=d)
        local = s.tz_localize("UTC").tz_convert("America/New_York")
        if (local.day_of_week >= 5) != weekend:
            continue
        overlap = busy[(busy["start_utc"] < e) & (busy["end_utc"] > s)]
        if any(focal & set(as_list(f)) for f in overlap["focal_station_ids"]):
            continue
        out.append((s, e))
    return out


def control_stations(event: dict, events: pd.DataFrame, stations: pd.DataFrame, settings, rng):
    """Stations far from every station affected by any event active during the window."""
    start, end = pd.Timestamp(event["start_utc"]), pd.Timestamp(event["end_utc"])
    active = events[(events["start_utc"] < end) & (events["end_utc"] > start)]
    affected = {s for f in active["focal_station_ids"] for s in as_list(f)}
    aff = stations[stations["station_id"].isin(affected)]
    cand = stations.dropna(subset=["lat", "lon"])
    if len(aff):
        d = haversine_m(
            cand["lat"].to_numpy()[:, None],
            cand["lon"].to_numpy()[:, None],
            aff["lat"].to_numpy()[None, :],
            aff["lon"].to_numpy()[None, :],
        ).min(axis=1)
        cand = cand[d >= settings.eval_control_min_distance_m]
    ids = sorted(cand["station_id"])
    rng.shuffle(ids)
    return ids


def event_differences(con, settings, events, stations, signal, rng) -> list[dict]:
    reader = SignalReader(con, settings, signal)
    rows = []
    ended = events[(events["status"] == "ended") & events["focal_station_ids"].map(len).gt(0)]
    for ev in ended.to_dict("records"):
        ents = reader.entities(as_list(ev["focal_station_ids"]))
        e_val = reader.mean(ents, ev["start_utc"], ev["end_utc"])
        windows = matched_windows(ev, events, settings)
        if e_val is None or not windows:
            continue
        a_vals = [v for s, e in windows if (v := reader.mean(ents, s, e)) is not None]
        if not a_vals or np.mean(a_vals) == 0:
            continue
        a_val = float(np.mean(a_vals))
        rel_event = e_val / a_val
        controls = []
        for sid in control_stations(ev, events, stations, settings, rng):
            if len(controls) >= settings.eval_control_stations:
                break
            c_ents = reader.entities([sid])
            c_now = reader.mean(c_ents, ev["start_utc"], ev["end_utc"])
            if c_now is None:
                continue
            c_then = [v for s, e in windows if (v := reader.mean(c_ents, s, e)) is not None]
            if not c_then or np.mean(c_then) == 0:
                continue
            controls.append(c_now / float(np.mean(c_then)))
        rows.append(
            {
                "event_id": ev["event_id"],
                "signal": signal,
                "start_utc": ev["start_utc"],
                "event_value": round(e_val, 2),
                "matched_value": round(a_val, 2),
                "n_matched_windows": len(a_vals),
                "diff_same_station_pct": (rel_event - 1) * 100,
                "n_controls": len(controls),
                "diff_vs_controls_pct": (
                    (rel_event - float(np.mean(controls))) * 100 if controls else None
                ),
            }
        )
    return rows


def evaluate_placebo(con, settings: Settings, start, end) -> Section:
    events = con.execute(
        "SELECT event_id, route_id, status, start_utc, end_utc, focal_station_ids FROM events "
        "WHERE start_utc >= $s - INTERVAL 30 DAY AND start_utc < $e",
        {"s": start, "e": end},
    ).df()
    events["focal_station_ids"] = events["focal_station_ids"].map(as_list)
    in_window = events[(events["start_utc"] >= pd.Timestamp(start))]
    stations = con.execute("SELECT station_id, lat, lon FROM subway_stations").df()
    section = Section(
        "placebo",
        "Surface impact validity (placebo test)",
        STATUS_OK,
        day(start),
        day(end - pd.Timedelta(seconds=1)),
        "",
        notes=[
            "(a) compares the stations near an event with the same stations at the same "
            "time of day on earlier days without an event there; (b) also subtracts the "
            "change at control stations at least "
            f"{settings.eval_control_min_distance_m:g} m from any affected station "
            "(difference in differences).",
            "Negative differences mean slower traffic or buses, or fewer bikes, during "
            "events. 95% intervals are percentile bootstraps over events.",
            "Cameras are not tested: they are only counted during events, so there are no "
            "non-event camera counts to compare with.",
        ],
    )
    rng = np.random.default_rng(settings.eval_seed)
    all_rows, findings = [], []
    for signal, spec in SIGNALS.items():
        if signal == "traffic_speed" and not settings.traffic_enabled:
            continue
        rows = event_differences(con, settings, events.assign(), stations, signal, rng)
        rows = [r for r in rows if r["event_id"] in set(in_window["event_id"])]
        all_rows += rows
        for key, label in (
            ("diff_same_station_pct", "vs. same stations at matched times"),
            ("diff_vs_controls_pct", "vs. control stations"),
        ):
            res = compare([r[key] for r in rows if r[key] is not None], settings)
            section.metrics.append(
                Metric(
                    f"{signal}_{key}",
                    f"{spec[-1]}: change during events {label}",
                    res["mean"],
                    res["n"],
                    "%",
                    res["ci_low"],
                    res["ci_high"],
                    note=(
                        "significant (95% interval excludes 0)"
                        if res["significant"]
                        else "not significant"
                        if res["n"] >= 2
                        else "too few events"
                    ),
                )
            )
            if res["n"] >= 2:
                findings.append((spec[-1], label, res))
    if all_rows:
        set_span(section, pd.Series([r["start_utc"] for r in all_rows]))
    section.tables["per_event"] = [
        {**r, "start_utc": pd.Timestamp(r["start_utc"]).strftime("%Y-%m-%d %H:%M")}
        for r in all_rows[:200]
    ]
    if not findings:
        section.status = STATUS_INSUFFICIENT
        section.summary = (
            "Not enough ended events with matched non-event data yet (at least 2 per signal "
            "are needed); no conclusion about the surface impact can be drawn."
        )
        return section
    sig = [f for f in findings if f[2]["significant"]]
    if sig:
        parts = [f"{s} {lab}: {r['mean']:+.1f}%" for s, lab, r in sig]
        section.summary = "Significant differences: " + "; ".join(parts) + "."
    else:
        section.summary = (
            "No significant difference was found between event and placebo conditions for "
            "any signal (every 95% interval includes zero). With the current sample this "
            "does not show a surface impact."
        )
    return section
