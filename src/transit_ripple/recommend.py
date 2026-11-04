"""Recommendation engine (M5): go / switch / wait for riders at affected stations.

For every ongoing delay event and each of its most affected stations, four kinds
of options are estimated and ranked by estimated time:

- ``walk_line``: walk to a nearby station served by a line that is not in any
  ongoing event (straight-line distance x detour factor / walking speed), then the
  next train from the latest GTFS-Realtime arrival predictions;
- ``bike``: walk to the nearest Citi Bike dock that has bikes, and whether nearby
  availability is dropping fast;
- ``bus``: walk to the nearest stop of each nearby bus route, the current speed of
  buses on segments nearby vs. their usual speed;
- ``wait``: the expected remaining delay from the historical recovery model.

There is no destination yet (destination-aware routing is future work), so every
option is compared on the time to *leave the station area*: access (walk) + wait
+ riding a reference distance (``rec_reference_trip_km``) at the mode's speed.

**Avoid the crowd.** The "obvious" option is the one that would be fastest under
normal conditions (usual waits and speeds), i.e. where most riders will go. If it
shows a crowding signal -- buses on that route slower than usual by at least
``rec_crowd_bus_slowdown_pct``, nearby bikes down by at least
``rec_crowd_bike_depletion_pct`` within ``rec_crowd_bike_window_min`` minutes, or
cameras near it counting at least ``rec_crowd_camera_increase_pct`` more people
than usual -- ``rec_crowd_penalty_min`` minutes are added to its estimate and it
is flagged, so a less crowded second-best option can rank first.

Every option of every run is logged to the ``recommendations`` table with its
inputs, so M6 can replay events and compare estimates with what happened.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd

from .baselines import lookup
from .config import Settings
from .events import STATUS_ONGOING, as_list
from .explain import _num, _Sentence
from .geo import haversine_m
from .recovery import RecoveryModel, cause_category

MPH_TO_MPS = 0.44704
DIRECTION_TEXT = {"N": "northbound", "S": "southbound"}
# GTFS route ids that riders know by another name.
ROUTE_TEXT = {
    "GS": "42 St Shuttle",
    "FS": "Franklin Av Shuttle",
    "H": "Rockaway Park Shuttle",
    "SI": "Staten Island Railway",
    "6X": "6 express",
    "7X": "7 express",
    "FX": "F express",
}


def routes_text(routes: list[str]) -> str:
    """'B/D/F/M'; shuttles and express variants by name ('6 and 6 express')."""
    plain = [r for r in routes if r not in ROUTE_TEXT]
    named = [ROUTE_TEXT[r] for r in routes if r in ROUTE_TEXT]
    parts = (["/".join(plain)] if plain else []) + named
    return " and ".join(parts)


OPTION_TYPES = ("walk_line", "bike", "bus", "wait")


@dataclass
class Option:
    option_type: str
    option_id: str
    target_id: str | None
    target_name: str | None
    routes: list[str]
    access_min: float | None
    wait_min: float | None
    ride_min: float | None
    normal_total_min: float | None
    confidence: str
    facts: dict = field(default_factory=dict)  # values used by the text and crowd check
    inputs: dict = field(default_factory=dict)  # provenance (data ages, sources)
    penalty_min: float = 0.0
    crowded: bool = False
    crowd_reasons: list[str] = field(default_factory=list)
    is_obvious: bool = False

    @property
    def total_min(self) -> float | None:
        parts = (self.access_min, self.wait_min, self.ride_min)
        if any(p is None for p in parts):
            return None
        return round(sum(parts) + self.penalty_min, 1)


def walk_minutes(distance_m: float, settings: Settings) -> float:
    return distance_m * settings.walk_detour_factor / settings.walk_speed_m_per_s / 60.0


def ride_minutes(speed_mph: float | None, settings: Settings) -> float | None:
    if not speed_mph or speed_mph <= 0:
        return None
    return settings.rec_reference_trip_km * 1000.0 / (speed_mph * MPH_TO_MPS) / 60.0


# --------------------------------------------------------------------------------------
# Data access (all reads are bounded to the last few minutes of fresh data)
# --------------------------------------------------------------------------------------


def ongoing_events(con: duckdb.DuckDBPyConnection, now: pd.Timestamp, max_age_min: float):
    """Latest version of every event that is ongoing and was computed recently."""
    return con.execute(
        "SELECT * FROM events WHERE status = $s AND computed_utc >= $since "
        "ORDER BY peak_delay_min DESC",
        {"s": STATUS_ONGOING, "since": now - pd.Timedelta(minutes=max_age_min)},
    ).df()


def next_arrivals(con, station_ids: list[str], routes: list[str], now, horizon_min=60):
    """Latest predicted arrival per trip at the stations (from ``stop_delays``)."""
    if not station_ids or not routes:
        return pd.DataFrame(columns=["station_id", "route_id", "direction", "arrival_utc"])
    return con.execute(
        """
        SELECT station_id, route_id, direction, coalesce(arrival_utc, departure_utc) AS arrival_utc,
               observed_utc
        FROM stop_delays
        WHERE list_contains($stations, station_id) AND list_contains($routes, route_id)
          AND observed_utc >= $since
          AND coalesce(arrival_utc, departure_utc) BETWEEN $now AND $until
        QUALIFY row_number() OVER (
            PARTITION BY trip_id, start_date, stop_id ORDER BY observed_utc DESC) = 1
        """,
        {
            "stations": [str(s) for s in station_ids],
            "routes": [str(r) for r in routes],
            "since": now - pd.Timedelta(minutes=10),
            "now": now,
            "until": now + pd.Timedelta(minutes=horizon_min),
        },
    ).df()


def neighbors_of(con, station_id: str, kind: str) -> pd.DataFrame:
    return con.execute(
        "SELECT neighbor_id, neighbor_name, distance_m FROM station_neighbors "
        "WHERE station_id = $s AND neighbor_type = $k ORDER BY distance_m",
        {"s": str(station_id), "k": kind},
    ).df()


def bikes_at(con, dock_ids: list[str], at, max_age_min: float, settings: Settings) -> pd.DataFrame:
    """Latest valid bike count per dock at or before ``at`` (no older than max_age)."""
    if not dock_ids:
        return pd.DataFrame(columns=["station_id", "bikes_available", "bucket_utc"])
    stale = float(settings.bike_station_stale_after_s)
    return con.execute(
        f"""
        SELECT station_id, bikes_available, bucket_utc FROM bike_minute
        WHERE list_contains($ids, station_id) AND bucket_utc BETWEEN $lo AND $at
          AND is_installed AND is_renting AND report_age_s <= {stale}
        QUALIFY row_number() OVER (PARTITION BY station_id ORDER BY bucket_utc DESC) = 1
        """,
        {"ids": dock_ids, "lo": at - pd.Timedelta(minutes=max_age_min), "at": at},
    ).df()


def bus_speeds(con, settings, baselines, route: str, stop_ids: list[str], now) -> dict:
    """Recent speed of a route's buses on segments ending at the stops, and usual speed."""
    if not stop_ids:
        return {}
    df = con.execute(
        """
        SELECT segment_id, bucket_utc AS minute_utc, segment_id AS entity_id,
               distance_m, duration_s
        FROM bus_minute
        WHERE route_id = $r AND list_contains($stops, stop_id) AND bucket_utc >= $since
          AND n_samples > 0
        """,
        {
            "r": route,
            "stops": [str(s) for s in stop_ids],
            "since": now - pd.Timedelta(minutes=settings.rec_data_max_age_min),
        },
    ).df()
    if df.empty:
        return {}
    speed = df["distance_m"].sum() / df["duration_s"].sum() / MPH_TO_MPS
    looked = lookup(baselines, "bus_speed_mph", df, settings.baseline_slot_min)
    out = {"speed_mph": round(float(speed), 1), "n_segments": int(df["segment_id"].nunique())}
    if looked["baseline"].notna().any():
        out["baseline_mph"] = round(float(looked["baseline"].mean()), 1)
        out["baseline_confidence"] = (
            "ok" if (looked["baseline_confidence"] == "ok").all() else "low"
        )
    return out


def camera_persons_change(con, settings, baselines, station_id: str, now) -> dict:
    """People counted by cameras near a station in the last minutes vs. usual."""
    df = con.execute(
        """
        SELECT c.camera_id AS entity_id, c.observed_utc AS minute_utc, c.persons
        FROM camera_counts_ok c JOIN camera_station_map m USING (camera_id)
        WHERE m.station_id = $s AND m.distance_m <= $r AND c.observed_utc >= $since
        """,
        {
            "s": str(station_id),
            "r": float(settings.neighbor_radius_m),
            "since": now - pd.Timedelta(minutes=settings.rec_data_max_age_min),
        },
    ).df()
    if df.empty:
        return {}
    looked = lookup(baselines, "camera_persons", df, settings.baseline_slot_min)
    looked = looked[looked["baseline"].notna() & (looked["baseline"] > 0)]
    if looked.empty:
        return {"persons": round(float(df["persons"].mean()), 1)}
    persons, base = float(looked["persons"].mean()), float(looked["baseline"].mean())
    return {
        "persons": round(persons, 1),
        "persons_baseline": round(base, 1),
        "persons_change_pct": round((persons - base) / base * 100, 1),
    }


# --------------------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------------------


def walk_line_options(con, settings, station, stations, affected_routes, now) -> list[Option]:
    lat, lon = station["lat"], station["lon"]
    d = haversine_m(lat, lon, stations["lat"].to_numpy(), stations["lon"].to_numpy())
    cand = stations.assign(distance_m=d)
    cand = cand[cand["distance_m"] <= settings.rec_walk_max_m].copy()
    cand["open_routes"] = cand["routes"].map(
        lambda r: sorted(set(as_list(r)) - affected_routes, key=_route_order)
    )
    cand = cand[cand["open_routes"].map(len) > 0]
    if cand.empty:
        return []
    arrivals = next_arrivals(
        con,
        cand["station_id"].tolist(),
        sorted({r for rs in cand["open_routes"] for r in rs}),
        now,
    )
    ride = ride_minutes(settings.rec_subway_speed_mph, settings)
    options = []
    for c in cand.itertuples(index=False):
        same = c.station_id == station["station_id"]
        walk = settings.rec_transfer_min if same else max(1.0, walk_minutes(c.distance_m, settings))
        at_platform = now + pd.Timedelta(minutes=walk)
        a = arrivals[
            (arrivals["station_id"] == c.station_id)
            & arrivals["route_id"].isin(c.open_routes)
            & (arrivals["arrival_utc"] >= at_platform)
        ]
        if a.empty:
            # No train predicted within the hour: the line may not be running now
            # (night or weekend service), so it is not suggested.
            continue
        by_dir = a.groupby("direction")["arrival_utc"].min().sort_index().to_dict()
        waits = {k: (v - at_platform).total_seconds() / 60.0 for k, v in by_dir.items()}
        running = sorted(set(a["route_id"]), key=_route_order)
        live = True
        wait = min(waits.values())
        options.append(
            Option(
                "walk_line",
                f"walk_line:{c.station_id}",
                c.station_id,
                c.station_name,
                running,
                round(walk, 1),
                round(wait, 1),
                round(ride, 1),
                round(walk + settings.rec_default_train_wait_min + ride, 1),
                "high" if live else "low",
                facts={
                    "same_station": same,
                    "distance_m": round(float(c.distance_m)),
                    "waits_by_direction": {k: round(v, 1) for k, v in waits.items()},
                    "live_arrivals": live,
                },
                inputs={
                    "arrivals_observed_utc": str(a["observed_utc"].max()) if live else None,
                    "n_predicted_arrivals": len(a),
                },
            )
        )
    options.sort(key=lambda o: o.total_min)
    return options[: settings.rec_max_walk_options]


def bike_option(con, settings, station, now) -> Option | None:
    docks = neighbors_of(con, station["station_id"], "bike_station")
    if docks.empty:
        return None
    ids = docks["neighbor_id"].astype(str).tolist()
    current = bikes_at(con, ids, now, settings.rec_data_max_age_min, settings)
    if current.empty:
        return None
    m = docks.merge(current, left_on="neighbor_id", right_on="station_id")
    usable = m[m["bikes_available"] >= settings.rec_min_bikes]
    if usable.empty:
        return None
    dock = usable.iloc[0]  # nearest first
    then = bikes_at(
        con,
        ids,
        now - pd.Timedelta(minutes=settings.rec_crowd_bike_window_min),
        10,
        settings,
    )
    total_now = int(m["bikes_available"].sum())
    common = set(then["station_id"]) & set(m["station_id"])
    total_then = int(then[then["station_id"].isin(common)]["bikes_available"].sum())
    total_now_common = int(m[m["station_id"].isin(common)]["bikes_available"].sum())
    drop = (
        round((total_then - total_now_common) / total_then * 100, 1)
        if common and total_then > 0
        else None
    )
    walk = max(1.0, walk_minutes(float(dock["distance_m"]), settings))
    access = walk + settings.rec_bike_unlock_min
    ride = ride_minutes(settings.rec_bike_speed_mph, settings)
    return Option(
        "bike",
        f"bike:{dock['neighbor_id']}",
        str(dock["neighbor_id"]),
        str(dock["neighbor_name"]),
        [],
        round(access, 1),
        0.0,
        round(ride, 1),
        round(access + ride, 1),
        "high",
        facts={
            "walk_min": round(walk, 1),
            "dock_bikes": int(dock["bikes_available"]),
            "bikes_nearby": total_now,
            "n_docks": len(m),
            "bikes_drop_pct": drop,
            "window_min": settings.rec_crowd_bike_window_min,
        },
        inputs={"bike_data_utc": str(m["bucket_utc"].max())},
    )


def bus_options(con, settings, baselines, station, now) -> list[Option]:
    stops = neighbors_of(con, station["station_id"], "bus_stop")
    if stops.empty:
        return []
    ref = con.execute(
        "SELECT stop_id, routes FROM bus_stops WHERE list_contains($ids, stop_id)",
        {"ids": stops["neighbor_id"].astype(str).tolist()},
    ).df()
    stops = stops.merge(ref, left_on="neighbor_id", right_on="stop_id")
    excluded = tuple(
        p.strip().upper() for p in settings.rec_exclude_bus_route_prefixes.split(",") if p.strip()
    )
    nearest: dict[str, dict] = {}
    for s in stops.itertuples(index=False):  # nearest first
        for r in as_list(s.routes):
            if excluded and str(r).upper().startswith(excluded):
                continue
            nearest.setdefault(
                r, {"stop_id": s.stop_id, "name": s.neighbor_name, "d": s.distance_m}
            )
    options = []
    for route, stop in nearest.items():
        route_stops = [x.stop_id for x in stops.itertuples() if route in as_list(x.routes)]
        sp = bus_speeds(con, settings, baselines, route, route_stops, now)
        live = "speed_mph" in sp
        speed = sp.get("speed_mph") or sp.get("baseline_mph") or settings.rec_bus_default_speed_mph
        usual = sp.get("baseline_mph") or settings.rec_bus_default_speed_mph
        walk = max(1.0, walk_minutes(float(stop["d"]), settings))
        wait = settings.rec_default_bus_wait_min
        ride = ride_minutes(speed, settings)
        change = (
            round((sp["speed_mph"] - sp["baseline_mph"]) / sp["baseline_mph"] * 100, 1)
            if live and sp.get("baseline_mph")
            else None
        )
        options.append(
            Option(
                "bus",
                f"bus:{route}",
                str(stop["stop_id"]),
                str(stop["name"]),
                [route],
                round(walk, 1),
                wait,
                round(ride, 1),
                round(walk + wait + ride_minutes(usual, settings), 1),
                "medium" if live else "low",
                facts={
                    "walk_min": round(walk, 1),
                    "speed_mph": sp.get("speed_mph"),
                    "baseline_mph": sp.get("baseline_mph"),
                    "speed_change_pct": change,
                    "live_speed": live,
                },
                inputs={
                    "speed_source": "live" if live else "assumed",
                    "baseline_confidence": sp.get("baseline_confidence"),
                    "n_segments": sp.get("n_segments", 0),
                },
            )
        )
    options.sort(key=lambda o: o.total_min)
    return options[: settings.rec_max_bus_options]


def wait_option(settings, event: dict, model: RecoveryModel, now) -> Option:
    start = pd.Timestamp(event["start_utc"])
    elapsed = max(0.0, (now - start).total_seconds() / 60.0)
    cause = cause_category([dict(a) for a in as_list(event.get("linked_alerts"))])
    est = model.estimate(event["route_id"], start, cause, elapsed)
    ride = ride_minutes(settings.rec_subway_speed_mph, settings)
    return Option(
        "wait",
        f"wait:{event['route_id']}",
        None,
        None,
        [event["route_id"]],
        0.0,
        est.remaining_median_min,
        round(ride, 1),
        round(ride, 1),
        {"ok": "medium", "low": "low"}.get(est.duration.confidence, "none"),
        facts={"estimate": est.to_dict(), "cause": cause},
        inputs={"recovery_level": est.duration.level, "recovery_n": est.duration.n_events},
    )


def apply_crowd_check(options: list[Option], settings, crowd: dict) -> None:
    """Flag and penalize the obvious option if it shows a crowding signal."""
    candidates = [o for o in options if o.option_type != "wait" and o.normal_total_min is not None]
    if not candidates:
        return
    obvious = min(candidates, key=lambda o: o.normal_total_min)
    obvious.is_obvious = True
    reasons = []
    if obvious.option_type == "bus":
        ch = obvious.facts.get("speed_change_pct")
        if ch is not None and ch <= -settings.rec_crowd_bus_slowdown_pct:
            reasons.append("bus_slowdown")
    if obvious.option_type == "bike":
        drop = obvious.facts.get("bikes_drop_pct")
        if drop is not None and drop >= settings.rec_crowd_bike_depletion_pct:
            reasons.append("bike_depletion")
    place = obvious.target_id if obvious.option_type == "walk_line" else "__station__"
    cam = crowd.get(place) or {}
    if cam.get("persons_change_pct") is not None and (
        cam["persons_change_pct"] >= settings.rec_crowd_camera_increase_pct
    ):
        reasons.append("camera_people")
        obvious.facts["camera"] = cam
    if reasons:
        obvious.crowded = True
        obvious.crowd_reasons = reasons
        obvious.penalty_min = settings.rec_crowd_penalty_min


def rank(options: list[Option]) -> list[Option]:
    known = sorted([o for o in options if o.total_min is not None], key=lambda o: o.total_min)
    return known + [o for o in options if o.total_min is None]


# --------------------------------------------------------------------------------------
# Text (rule-based; every number is cited)
# --------------------------------------------------------------------------------------


def _route_order(r: str):
    return (0, int(r)) if r.isdigit() else (1, r)


def option_text(o: Option, settings, event: dict) -> dict:
    s = _Sentence()
    f = o.facts
    if o.option_type == "walk_line":
        routes = s.cite("routes", routes_text(o.routes))
        name = s.cite("target_name", o.target_name or "")
        if f["same_station"]:
            text = f"Switch to the {routes} at this station"
        else:
            walk = s.cite("walk_min", _num(o.access_min, 0))
            text = f"Walk {walk} min to {name} for the {routes}"
        waits = f["waits_by_direction"]
        if waits:
            parts = []
            for direction, w in sorted(waits.items()):
                label = DIRECTION_TEXT.get(direction, direction)
                if w < 0.5:
                    v = s.cite(f"wait_{direction}_min_under", "1")
                    parts.append(f"less than {v} min {label}")
                else:
                    v = s.cite(f"wait_{direction}_min", _num(w, 0))
                    parts.append(f"{v} min {label}")
            label = "next train in" if len(parts) == 1 else "next trains in"
            text += f" ({label} {', '.join(parts)})"
        elif o.wait_min is not None:
            assumed = s.cite("assumed_wait_min", _num(o.wait_min, 0))
            text += f" (no live arrivals; a {assumed} min wait is assumed)"
        else:
            text += " (no live arrivals)"
    elif o.option_type == "bike":
        walk = s.cite("walk_min", _num(f["walk_min"], 0))
        name = s.cite("target_name", o.target_name or "")
        bikes = s.cite("dock_bikes", str(f["dock_bikes"]))
        text = f"Take a Citi Bike from {name} ({walk} min walk, {bikes} bikes there now"
        drop = f.get("bikes_drop_pct")
        if drop is not None and drop > 0:
            pct = s.cite("bikes_drop_pct", _num(drop, 0))
            window = s.cite("window_min", _num(f["window_min"], 0))
            text += f"; bikes nearby are down {pct}% in the last {window} min"
        text += ")"
    elif o.option_type == "bus":
        route = s.cite("route", o.routes[0])
        walk = s.cite("walk_min", _num(f["walk_min"], 0))
        name = s.cite("target_name", o.target_name or "")
        text = f"Take the {route} bus from {name} ({walk} min walk"
        if f.get("speed_change_pct") is not None:
            pct = s.cite("speed_change_pct_abs", _num(abs(f["speed_change_pct"]), 0))
            word = "slower" if f["speed_change_pct"] < 0 else "faster"
            text += f"; buses nearby are running {pct}% {word} than usual)"
        elif f.get("live_speed"):
            mph = s.cite("speed_mph", _num(f["speed_mph"]))
            text += f"; buses nearby average {mph} mph)"
        else:
            text += "; no live bus speed data)"
    else:  # wait
        line = s.cite("route", o.routes[0])
        est = f["estimate"]
        d = est["duration"]
        if d["n_events"] == 0:
            text = f"Wait for the {line}: there are no past events yet to estimate the recovery"
        elif o.wait_min is None:
            med = s.cite("history_median_min", _num(d["median_min"], 0))
            n = s.cite("history_n_events", str(d["n_events"]))
            group = s.cite("history_group", d["group"])
            text = (
                f"Wait for the {line}: this delay has already lasted longer than most similar "
                f"past delays (median {med} min; {n} past events, {group}), so there is no "
                "reliable estimate of when it ends"
            )
        else:
            rem = s.cite("remaining_median_min", _num(o.wait_min, 0))
            med = s.cite("history_median_min", _num(d["median_min"], 0))
            n = s.cite("history_n_events", str(d["n_events"]))
            group = s.cite("history_group", d["group"])
            text = (
                f"Wait for the {line}: about {rem} more min expected (similar past delays "
                f"lasted a median of {med} min; {n} past events, {group})"
            )
    if o.crowded:
        reasons = {
            "bus_slowdown": "buses there are much slower than usual",
            "bike_depletion": "nearby bikes are running out",
            "camera_people": "cameras count many more people than usual",
        }
        text += f". Likely crowded: {'; '.join(reasons[r] for r in o.crowd_reasons)}"
    total = o.total_min
    if total is not None:
        km = s.cite("reference_km", _num(settings.rec_reference_trip_km, 0))
        t = s.cite("total_min", _num(total, 0))
        text += f". About {t} min to get {km} km away"
    return {"text": text + ".", "metrics": s.metrics}


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def recommend(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    baselines: pd.DataFrame,
    now,
    run_id: str,
    model: RecoveryModel | None = None,
) -> pd.DataFrame:
    """Ranked options for every ongoing event and focal station (one row per option)."""
    now = pd.Timestamp(now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now)
    now = now.floor("s")
    events = ongoing_events(con, now, max_age_min=settings.rec_data_max_age_min)
    if events.empty:
        return pd.DataFrame()
    model = model or RecoveryModel.from_store(con, settings, now)
    stations = con.execute("SELECT * FROM subway_stations").df()
    by_id = {r["station_id"]: r for r in stations.to_dict("records")}
    affected_routes = set(events["route_id"])
    rows = []
    for ev in events.to_dict("records"):
        wait = wait_option(settings, ev, model, now)
        for sid in as_list(ev.get("focal_station_ids")):
            station = by_id.get(sid)
            if station is None or station.get("lat") is None:
                continue
            options = [
                *walk_line_options(con, settings, station, stations, affected_routes, now),
                *bus_options(con, settings, baselines, station, now),
                wait,
            ]
            bike = bike_option(con, settings, station, now)
            if bike:
                options.append(bike)
            crowd = {"__station__": camera_persons_change(con, settings, baselines, sid, now)}
            for o in options:
                if o.option_type == "walk_line" and o.target_id not in crowd:
                    crowd[o.target_id] = camera_persons_change(
                        con, settings, baselines, o.target_id, now
                    )
            # Each station gets its own copy of the shared wait option.
            options = [o if o.option_type != "wait" else _copy(o) for o in options]
            apply_crowd_check(options, settings, crowd)
            for i, o in enumerate(rank(options), start=1):
                txt = option_text(o, settings, ev)
                rows.append(
                    {
                        "bucket_utc": now.floor("min"),
                        "computed_utc": now,
                        "run_id": run_id,
                        "event_id": ev["event_id"],
                        "event_route_id": ev["route_id"],
                        "station_id": sid,
                        "station_name": station["station_name"],
                        "rank": i,
                        "option_type": o.option_type,
                        "option_id": o.option_id,
                        "target_id": o.target_id,
                        "target_name": o.target_name,
                        "routes": o.routes,
                        "est_total_min": o.total_min,
                        "est_access_min": o.access_min,
                        "est_wait_min": o.wait_min,
                        "est_ride_min": o.ride_min,
                        "penalty_min": o.penalty_min,
                        "normal_total_min": o.normal_total_min,
                        "is_obvious": o.is_obvious,
                        "crowded": o.crowded,
                        "crowd_reasons": o.crowd_reasons,
                        "confidence": o.confidence,
                        "text": txt["text"],
                        "metrics": json.dumps(txt["metrics"], sort_keys=True),
                        "inputs": json.dumps(
                            {
                                "facts": o.facts,
                                "data": o.inputs,
                                "rule": settings.recommendation_rule(),
                            },
                            sort_keys=True,
                            default=_json_default,
                        ),
                    }
                )
    return pd.DataFrame(rows)


def _copy(o: Option) -> Option:
    from dataclasses import replace

    return replace(o, facts=dict(o.facts), inputs=dict(o.inputs), crowd_reasons=[])


def _json_default(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if math.isnan(v) else float(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    return str(v)
