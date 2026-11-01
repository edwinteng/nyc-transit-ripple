"""Compact JSON export for the static dashboard (no Parquet reaches the browser).

Files (written under ``out_dir``, normally ``site/data``):

- ``status.json``: last update and freshness of every data source;
- ``stations.json``: subway stations with their routes and, if they are part of
  an active or recent event, that event's id, status and impact;
- ``events.json``: events of the last ``export_window_days`` days (summary fields,
  linked alerts, impact score, explanation sentences);
- ``events/<event_id>.json``: per-event chart series (downsampled to at most
  ``export_max_points`` points) and the nearby stations, cameras, Citi Bike
  stations and road links for the map.

All timestamps are UTC ISO 8601 strings ending in ``Z``; missing values are null.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .config import SUBWAY_FEEDS, Settings
from .events import as_list, route_series, surface_impact
from .explain import sentences_of

SCHEMA_VERSION = 1
IMPACT_BANDS = ((75, "severe"), (50, "high"), (20, "moderate"), (0, "low"))
SERIES_FIELDS = {
    "delay_min": "value",
    "traffic_mph": "traffic_speed_mph",
    "traffic_base_mph": "traffic_baseline_mph",
    "bus_mph": "bus_speed_mph",
    "bus_base_mph": "bus_baseline_mph",
    "camera_vehicles": "camera_vehicles",
    "camera_vehicles_base": "camera_vehicles_baseline",
    "camera_persons": "camera_persons",
    "camera_persons_base": "camera_persons_baseline",
    "bikes": "bikes_available",
    "bikes_base": "bikes_baseline",
    "impact": "impact_score",
}


def iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        if pd.isna(ts):
            return None
    except (TypeError, ValueError):
        pass
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def num(value, digits: int = 1):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, digits)


def impact_label(score) -> str:
    s = num(score)
    if s is None:
        return "unknown"
    return next(label for bound, label in IMPACT_BANDS if s >= bound)


def _write(path: Path, payload) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False)
    path.write_text(text, encoding="utf-8")
    return len(text.encode())


# --------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------

SOURCE_LABELS = {
    "subway": "Subway feeds",
    "alerts": "MTA alerts",
    "traffic": "Traffic speed",
    "bike_status": "Citi Bike",
    "bus": "MTA buses",
    "cameras": "Cameras",
    "baselines": "Baselines",
}


def build_status(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    now: pd.Timestamp,
    baselines: pd.DataFrame | None = None,
) -> dict:
    """Freshness per source. A source is ``ok`` only if its latest fetch was usable
    and its data is younger than the source's stale threshold."""
    sources = []
    feeds = con.execute(
        "SELECT feed_id, status, fetched_utc, header_utc FROM quality_feed "
        "QUALIFY row_number() OVER (PARTITION BY feed_id ORDER BY fetched_utc DESC) = 1"
    ).df()
    if feeds.empty:
        sources.append(_source("subway", "missing", None, None, now, "no subway data yet"))
    else:
        usable = feeds["status"].isin(["ok", "duplicate"])
        newest = feeds.loc[usable, "header_utc"].max() if usable.any() else None
        fetched = feeds["fetched_utc"].max()
        status = _classify(newest, now, settings.stale_after_s + 600) if usable.any() else "missing"
        if status == "ok" and not usable.all():
            status = "degraded"
        detail = f"{int(usable.sum())} of {len(SUBWAY_FEEDS)} feeds usable"
        sources.append(_source("subway", status, newest, fetched, now, detail))

    thresholds = {
        "alerts": settings.alerts_stale_after_s,
        "traffic": settings.traffic_stale_after_s,
        "bike_status": settings.bike_feed_stale_after_s + 600,
        "bus": settings.bus_stale_after_s + 600,
    }
    latest = con.execute(
        "SELECT source, status, detail, fetched_utc, observed_utc, coverage FROM quality_source "
        "QUALIFY row_number() OVER (PARTITION BY source ORDER BY fetched_utc DESC) = 1"
    ).df()
    by_source = {r["source"]: r for r in latest.to_dict("records")}
    for name, threshold in thresholds.items():
        r = by_source.get(name)
        if r is None:
            sources.append(_source(name, "missing", None, None, now, "no data yet"))
            continue
        if r["status"] == "unavailable":
            status = "unavailable"  # e.g. no API key: never shown as normal
        elif r["status"] in ("outage", "invalid", "empty"):
            status = "missing"
        elif r["status"] == "stale":
            status = "stale"
        else:
            status = _classify(r["observed_utc"], now, threshold)
        detail = r["detail"] if isinstance(r["detail"], str) else None
        if num(r["coverage"]) is not None:
            cov = f"coverage {num(r['coverage'] * 100, 0):.0f}%"
            detail = f"{detail}; {cov}" if detail else cov
        sources.append(_source(name, status, r["observed_utc"], r["fetched_utc"], now, detail))

    cams = (
        con.execute(
            "SELECT max(observed_utc) AS last, count(*) FILTER (WHERE status = 'ok') AS ok, "
            "count(*) AS n FROM camera_counts WHERE observed_utc >= $since",
            {"since": now - pd.Timedelta(days=1)},
        )
        .df()
        .iloc[0]
    )
    cam_last = con.execute("SELECT max(observed_utc) FROM camera_counts").fetchone()[0]
    sources.append(
        _source(
            "cameras",
            "on_demand" if cam_last is not None else "missing",
            cam_last,
            cam_last,
            now,
            f"counted only during events; {int(cams['ok'])} of {int(cams['n'])} counts ok "
            "in the last 24 h",
        )
    )
    sources.append(_baseline_source(settings, baselines, now))

    last_run = max(
        [
            pd.Timestamp(t)
            for t in (
                feeds["fetched_utc"].max() if not feeds.empty else None,
                latest["fetched_utc"].max() if not latest.empty else None,
            )
            if t is not None and not pd.isna(t)
        ],
        default=None,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": iso(now),
        "last_pipeline_run_utc": iso(last_run),
        "sources": sources,
    }


def _baseline_source(settings: Settings, baselines: pd.DataFrame | None, now) -> dict:
    """Baselines are ``building`` until at least one slot has enough history."""
    meta = Path(settings.data_dir) / "reference" / "baselines_meta.json"
    if not meta.exists():
        return _source("baselines", "missing", None, None, now, "not computed yet")
    m = json.loads(meta.read_text())
    computed = pd.Timestamp(m["computed_utc"])
    if baselines is None or "confidence" not in baselines:
        n_rows, n_ok = int(m.get("rows", 0)), None
    else:
        n_rows, n_ok = len(baselines), int((baselines["confidence"] == "ok").sum())
    max_age_s = 2 * settings.baseline_max_age_hours * 3600
    if (now - computed).total_seconds() > max_age_s:
        status = "stale"
    elif n_ok == 0:
        status = "building"
    else:
        status = "ok"
    ok_txt = "" if n_ok is None else f", {n_ok} with enough history"
    detail = (
        f"{n_rows} weekday/time slots{ok_txt}; a slot needs "
        f"{m.get('min_days_for_ok')} days of history"
    )
    return _source("baselines", status, computed, computed, now, detail)


def _classify(observed, now: pd.Timestamp, threshold_s: float) -> str:
    if observed is None or pd.isna(observed):
        return "missing"
    return "ok" if (now - pd.Timestamp(observed)).total_seconds() <= threshold_s else "stale"


def _source(name, status, observed, fetched, now, detail) -> dict:
    age = None
    if observed is not None and not pd.isna(observed):
        age = num((now - pd.Timestamp(observed)).total_seconds() / 60.0, 0)
    return {
        "source": name,
        "label": SOURCE_LABELS.get(name, name),
        "status": status,
        "data_utc": iso(observed),
        "checked_utc": iso(fetched),
        "data_age_min": age,
        "detail": detail,
    }


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


def recent_events(
    con: duckdb.DuckDBPyConnection, settings: Settings, now: pd.Timestamp
) -> pd.DataFrame:
    since = now - pd.Timedelta(days=settings.export_window_days)
    return con.execute(
        "SELECT * FROM events WHERE start_utc >= $since ORDER BY start_utc DESC",
        {"since": since},
    ).df()


def _signals(detail_json) -> dict:
    if not isinstance(detail_json, str) or not detail_json:
        return {}
    signals = json.loads(detail_json).get("signals", {})
    return {
        name: {
            "status": d.get("status"),
            "change_pct": num(d.get("change_pct")),
            "sub_score": num(d.get("sub_score")),
            "weight": num(d.get("effective_weight"), 3),
        }
        for name, d in signals.items()
    }


def event_summary(ev: dict, stations: dict) -> dict:
    ids = as_list(ev.get("affected_station_ids"))
    names = as_list(ev.get("affected_station_names"))
    delays = as_list(ev.get("affected_station_max_delay_min"))
    affected = []
    for i, sid in enumerate(ids):
        st = stations.get(sid, {})
        affected.append(
            {
                "id": sid,
                "name": names[i] if i < len(names) else st.get("name"),
                "max_delay_min": num(delays[i]) if i < len(delays) else None,
                "lat": st.get("lat"),
                "lon": st.get("lon"),
            }
        )
    alerts = [
        {
            "id": a.get("alert_id"),
            "type": a.get("alert_type"),
            "planned": bool(a.get("is_planned")),
            "header": a.get("header_text"),
        }
        for a in as_list(ev.get("linked_alerts"))
    ]
    return {
        "id": ev["event_id"],
        "route": ev["route_id"],
        "status": ev["status"],
        "end_reason": ev.get("end_reason"),
        "start_utc": iso(ev["start_utc"]),
        "end_utc": iso(ev["end_utc"]),
        "duration_min": num(ev.get("duration_min"), 0),
        "peak_delay_min": num(ev.get("peak_delay_min")),
        "peak_utc": iso(ev.get("peak_utc")),
        "peak_direction": ev.get("peak_direction"),
        "threshold_min": num(ev.get("threshold_min")),
        "impact_score": num(ev.get("impact_score")),
        "impact_label": impact_label(ev.get("impact_score")),
        "impact_signals_used": as_list(ev.get("impact_signals_used")),
        "impact_signals": _signals(ev.get("impact_detail")),
        "surface_recovery_status": ev.get("surface_recovery_status"),
        "surface_recovered_utc": iso(ev.get("surface_recovered_utc")),
        "surface_checked_until_utc": iso(ev.get("surface_checked_until_utc")),
        "traffic_change_pct": num(ev.get("traffic_change_pct")),
        "camera_change_pct": num(ev.get("camera_change_pct")),
        "bikes_change_pct": num(ev.get("bikes_change_pct")),
        "bus_change_pct": num(ev.get("bus_change_pct")),
        "stations": affected,
        "focal_station_ids": as_list(ev.get("focal_station_ids")),
        "alerts": alerts,
        "summary": ev.get("summary"),
        "sentences": sentences_of(ev),
        "computed_utc": iso(ev.get("computed_utc")),
        "detail": f"events/{ev['event_id']}.json",
    }


def latest_recommendations(con: duckdb.DuckDBPyConnection, event_ids: list[str]) -> dict:
    """Latest recommendation set per event: {event_id: {computed_utc, stations: [...]}}."""
    if not event_ids:
        return {}
    df = con.execute(
        """
        SELECT * FROM recommendations
        WHERE list_contains($ids, event_id)
        QUALIFY computed_utc = max(computed_utc) OVER (PARTITION BY event_id)
        ORDER BY event_id, station_id, rank
        """,
        {"ids": list(event_ids)},
    ).df()
    out: dict = {}
    for (event_id, station_id), g in df.groupby(["event_id", "station_id"], sort=False):
        entry = out.setdefault(
            event_id, {"computed_utc": iso(g["computed_utc"].iloc[0]), "stations": []}
        )
        entry["stations"].append(
            {
                "station_id": station_id,
                "station_name": g["station_name"].iloc[0],
                "options": [
                    {
                        "rank": int(r["rank"]),
                        "type": r["option_type"],
                        "text": r["text"],
                        "total_min": num(r["est_total_min"]),
                        "normal_total_min": num(r["normal_total_min"]),
                        "confidence": r["confidence"],
                        "is_obvious": bool(r["is_obvious"]),
                        "crowded": bool(r["crowded"]),
                        "crowd_reasons": as_list(r["crowd_reasons"]),
                        "target_name": r["target_name"]
                        if isinstance(r["target_name"], str)
                        else None,
                        "routes": as_list(r["routes"]),
                    }
                    for r in g.sort_values("rank").to_dict("records")
                ],
            }
        )
    return out


def downsample(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    """Average consecutive minutes into buckets so at most ``max_points`` remain."""
    if len(df) <= max_points or max_points <= 0:
        return df
    k = math.ceil(len(df) / max_points)
    groups = np.arange(len(df)) // k
    numeric = df.drop(columns=["minute_utc"]).apply(pd.to_numeric, errors="coerce")
    out = numeric.groupby(groups).mean()
    out.insert(0, "minute_utc", df["minute_utc"].groupby(groups).first().to_numpy())
    return out


def event_detail(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    baselines: pd.DataFrame,
    ev: dict,
    stations: dict,
    now: pd.Timestamp,
) -> dict:
    start, end = pd.Timestamp(ev["start_utc"]), pd.Timestamp(ev["end_utc"])
    focal = as_list(ev.get("focal_station_ids"))
    _, series = surface_impact(con, settings, baselines, focal, start, end, now)
    pad = pd.Timedelta(minutes=settings.site_chart_padding_min)
    lo = start - pad
    hi = min(max(series["minute_utc"].max(), end), now)
    rs = route_series(con, settings, lo.to_pydatetime(), hi.to_pydatetime())
    rs = rs[rs["route_id"] == ev["route_id"]][["minute_utc", "value"]]
    pre = pd.DataFrame({"minute_utc": pd.date_range(lo.floor("min"), hi.floor("min"), freq="min")})
    full = pre.merge(series, on="minute_utc", how="left").merge(rs, on="minute_utc", how="left")
    cols = ["minute_utc", *SERIES_FIELDS.values()]
    for c in cols:
        if c not in full:
            full[c] = np.nan
    ds = downsample(full[cols], settings.export_max_points)
    out_series = {"t": [iso(t) for t in ds["minute_utc"]]}
    for key, col in SERIES_FIELDS.items():
        out_series[key] = [num(v) for v in ds[col]]
    return {
        "schema_version": SCHEMA_VERSION,
        "id": ev["event_id"],
        "route": ev["route_id"],
        "start_utc": iso(start),
        "end_utc": iso(end),
        "threshold_min": num(ev.get("threshold_min")),
        "series": out_series,
        "nearby": nearby(con, settings, focal, ev, stations),
        "recommendations": latest_recommendations(con, [ev["event_id"]]).get(ev["event_id"]),
    }


def nearby(con, settings: Settings, focal: list[str], ev: dict, stations: dict) -> dict:
    """Stations, cameras, Citi Bike stations and road links around the focal stations."""
    out = {
        "stations": [
            {
                "id": s,
                "name": stations.get(s, {}).get("name"),
                "lat": stations.get(s, {}).get("lat"),
                "lon": stations.get(s, {}).get("lon"),
            }
            for s in focal
        ],
        "cameras": [],
        "bikes": [],
        "links": [],
    }
    if not focal:
        return out
    params = {"ids": [str(s) for s in focal], "r": float(settings.neighbor_radius_m)}
    latest = {c["camera_id"]: c for c in as_list(ev.get("camera_counts"))}
    cams = con.execute(
        """
        SELECT m.camera_id, any_value(m.camera_name) AS name, min(m.distance_m) AS distance_m,
               any_value(c.lat) AS lat, any_value(c.lon) AS lon
        FROM camera_station_map m LEFT JOIN cameras c USING (camera_id)
        WHERE list_contains($ids, m.station_id) AND m.distance_m <= $r
        GROUP BY m.camera_id ORDER BY distance_m
        """,
        params,
    ).df()
    for r in cams.to_dict("records"):
        count = latest.get(r["camera_id"])
        out["cameras"].append(
            {
                "id": r["camera_id"],
                "name": r["name"],
                "lat": num(r["lat"], 6),
                "lon": num(r["lon"], 6),
                "distance_m": num(r["distance_m"], 0),
                "vehicles": (count["car"] + count["bus"] + count["truck"]) if count else None,
                "persons": count["person"] if count else None,
                "counted_utc": iso(count["observed_utc"]) if count else None,
            }
        )
    bikes = con.execute(
        """
        SELECT n.neighbor_id AS id, any_value(n.neighbor_name) AS name,
               min(n.distance_m) AS distance_m, any_value(b.lat) AS lat, any_value(b.lon) AS lon
        FROM station_neighbors n LEFT JOIN bike_stations b ON b.station_id = n.neighbor_id
        WHERE n.neighbor_type = 'bike_station' AND list_contains($ids, n.station_id)
          AND n.distance_m <= $r
        GROUP BY n.neighbor_id ORDER BY distance_m
        """,
        params,
    ).df()
    out["bikes"] = [
        {
            "id": r["id"],
            "name": r["name"],
            "lat": num(r["lat"], 6),
            "lon": num(r["lon"], 6),
            "distance_m": num(r["distance_m"], 0),
        }
        for r in bikes.to_dict("records")
    ]
    links = con.execute(
        """
        SELECT l.link_id AS id, any_value(l.link_name) AS name, any_value(l.lats) AS lats,
               any_value(l.lons) AS lons
        FROM station_neighbors n JOIN traffic_links l ON l.link_id = n.neighbor_id
        WHERE n.neighbor_type = 'traffic_link' AND list_contains($ids, n.station_id)
          AND n.distance_m <= $r
        GROUP BY l.link_id ORDER BY l.link_id
        """,
        params,
    ).df()
    for r in links.to_dict("records"):
        lats, lons = list(r["lats"]), list(r["lons"])
        step = max(1, math.ceil(len(lats) / 40))  # simplify long polylines
        coords = [[num(a, 5), num(b, 5)] for a, b in zip(lats[::step], lons[::step], strict=False)]
        if lats and (len(lats) - 1) % step:
            coords.append([num(lats[-1], 5), num(lons[-1], 5)])
        out["links"].append({"id": r["id"], "name": r["name"], "coords": coords})
    return out


# --------------------------------------------------------------------------------------
# Stations and orchestration
# --------------------------------------------------------------------------------------


def station_index(con: duckdb.DuckDBPyConnection) -> dict:
    df = con.execute("SELECT * FROM subway_stations ORDER BY station_id").df()
    return {
        r["station_id"]: {
            "id": r["station_id"],
            "name": r["station_name"],
            "lat": num(r["lat"], 6),
            "lon": num(r["lon"], 6),
            "routes": as_list(r.get("routes")),
        }
        for r in df.to_dict("records")
    }


def build_stations(
    index: dict, events: list[dict], now: pd.Timestamp, recent_hours: float = 24
) -> dict:
    """Stations, each linked to its most severe active or recent event."""
    since = iso(now - pd.Timedelta(hours=recent_hours))
    best: dict[str, dict] = {}
    for ev in events:
        recent = ev["status"] == "ongoing" or (ev["end_utc"] or "") >= since
        if not recent:
            continue
        for st in ev["stations"]:
            current = best.get(st["id"])
            rank = (ev["status"] == "ongoing", ev["impact_score"] or -1, ev["peak_delay_min"] or 0)
            if current is None or rank > current["_rank"]:
                best[st["id"]] = {
                    "_rank": rank,
                    "event_id": ev["id"],
                    "route": ev["route"],
                    "status": ev["status"],
                    "impact_score": ev["impact_score"],
                    "impact_label": ev["impact_label"],
                    "max_delay_min": st["max_delay_min"],
                }
    stations = []
    for sid, st in index.items():
        link = best.get(sid)
        if link:
            link = {k: v for k, v in link.items() if k != "_rank"}
        stations.append({**st, "event": link})
    return {"schema_version": SCHEMA_VERSION, "generated_utc": iso(now), "stations": stations}


def export_site_data(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    baselines: pd.DataFrame,
    out_dir: Path,
    now: dt.datetime,
) -> dict:
    """Write all dashboard JSON files; returns {relative path: bytes}."""
    now_ts = pd.Timestamp(now.replace(tzinfo=None) if now.tzinfo else now).floor("s")
    out_dir = Path(out_dir)
    sizes: dict[str, int] = {}
    index = station_index(con)
    rows = recent_events(con, settings, now_ts).to_dict("records")
    summaries = [event_summary(ev, index) for ev in rows]
    recs = latest_recommendations(con, [ev["id"] for ev in summaries])
    for ev in summaries:
        r = recs.get(ev["id"])
        top = r["stations"][0]["options"][0] if r and r["stations"] else None
        ev["top_recommendation"] = (
            {
                "text": top["text"],
                "station_name": r["stations"][0]["station_name"],
                "computed_utc": r["computed_utc"],
            }
            if top
            else None
        )
    sizes["status.json"] = _write(
        out_dir / "status.json", build_status(con, settings, now_ts, baselines)
    )
    sizes["stations.json"] = _write(
        out_dir / "stations.json", build_stations(index, summaries, now_ts)
    )
    sizes["events.json"] = _write(
        out_dir / "events.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_utc": iso(now_ts),
            "window_days": settings.export_window_days,
            "impact_rule": settings.impact_rule(),
            "event_rule": settings.event_rule(),
            "events": summaries,
        },
    )
    keep = set()
    for ev in rows:
        name = f"events/{ev['event_id']}.json"
        keep.add(Path(name).name)
        sizes[name] = _write(
            out_dir / name, event_detail(con, settings, baselines, ev, index, now_ts)
        )
    events_dir = out_dir / "events"
    if events_dir.exists():
        for old in events_dir.glob("*.json"):
            if old.name not in keep:
                old.unlink()
    return sizes
