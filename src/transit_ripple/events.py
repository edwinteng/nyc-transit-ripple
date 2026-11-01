"""Subway delay events: detection, affected stations, alert linking, surface context.

Rule (all parameters configurable, see ``Settings.event_rule``): a line has a
delay event when its per-minute delay metric (worst direction) is at or above
``event_threshold_min`` for ``event_min_minutes`` consecutive minutes, of which
at least ``event_min_observed`` are real observations rather than forward-filled
minutes. Minutes with too few measured trains or without fresh data are unknown:
they do not count towards an event and do not end one, unless the data gap lasts
longer than ``event_max_gap_min``.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd

from .baselines import CONFIDENCE_LOW, CONFIDENCE_NONE, CONFIDENCE_OK, lookup
from .config import Settings
from .storage import entity_filled, line_minute_filled

MINUTE = pd.Timedelta(minutes=1)


def as_list(value) -> list:
    """List columns come back from DuckDB/pandas as arrays, lists or NULL."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    return list(value)


STATUS_ONGOING = "ongoing"
STATUS_ENDED = "ended"
END_RECOVERED = "recovered"
END_DATA_GAP = "data_gap"
END_ONGOING = "ongoing"
MAX_LINKED_ALERTS = 5


# --------------------------------------------------------------------------------------
# Route series
# --------------------------------------------------------------------------------------


def route_series(
    con: duckdb.DuckDBPyConnection, settings: Settings, start: dt.datetime, end: dt.datetime
) -> pd.DataFrame:
    """Per route x minute: the worst-direction delay metric and its freshness.

    ``fill_status`` is ``observed``/``filled`` when at least one direction has a
    usable value, otherwise ``missing``. ``directions_above`` lists directions at
    or above the threshold in that minute.
    """
    cols = [
        "minute_utc",
        "route_id",
        "value",
        "direction",
        "fill_status",
        "n_trips",
        "max_trip_delay_min",
        "directions_above",
    ]
    grid = line_minute_filled(con, start, end, max_age_min=settings.event_fill_max_age_min)
    if grid.empty:
        return pd.DataFrame(columns=cols)
    metric = settings.event_metric
    if metric not in grid.columns:
        raise ValueError(f"Unknown event metric {metric!r}")
    usable = grid[
        grid["fill_status"].isin(["observed", "filled"])
        & (grid["n_trips_measured"].astype(float) >= settings.event_min_trips)
        & grid[metric].notna()
    ].copy()
    keys = grid[["minute_utc", "route_id"]].drop_duplicates()
    if usable.empty:
        out = keys.assign(
            value=np.nan,
            direction=None,
            fill_status="missing",
            n_trips=np.nan,
            max_trip_delay_min=np.nan,
            directions_above=[[]] * len(keys),
        )
        return out[cols].sort_values(["route_id", "minute_utc"]).reset_index(drop=True)

    usable[metric] = usable[metric].astype(float)
    g = usable.groupby(["minute_utc", "route_id"])
    best = usable.loc[g[metric].idxmax()].set_index(["minute_utc", "route_id"])
    agg = pd.DataFrame(
        {
            "value": best[metric],
            "direction": best["direction"],
            "n_trips": best["n_trips_measured"].astype(float),
            "fill_status": g["fill_status"].agg(
                lambda s: "observed" if (s == "observed").any() else "filled"
            ),
            "max_trip_delay_min": g["delay_max_min"].max().astype(float),
            "directions_above": g.apply(
                lambda d: sorted(
                    d.loc[d[metric] >= settings.event_threshold_min, "direction"].dropna()
                ),
                include_groups=False,
            ),
        }
    ).reset_index()
    out = keys.merge(agg, on=["minute_utc", "route_id"], how="left")
    out["fill_status"] = out["fill_status"].fillna("missing")
    out["directions_above"] = out["directions_above"].map(
        lambda v: v if isinstance(v, list) else []
    )
    return out[cols].sort_values(["route_id", "minute_utc"]).reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------------------


@dataclass
class DetectedEvent:
    route_id: str
    start: pd.Timestamp
    last_above: pd.Timestamp
    status: str = STATUS_ONGOING
    end_reason: str = END_ONGOING
    minutes: list[dict] = field(default_factory=list)  # above-threshold minutes

    @property
    def end(self) -> pd.Timestamp:
        """Exclusive end: the minute after the last minute at or above the threshold."""
        return self.last_above + MINUTE

    def stats(self) -> dict:
        m = pd.DataFrame(self.minutes)
        peak = m.loc[m["value"].idxmax()]
        dirs = sorted({d for ds in m["directions_above"] for d in ds})
        return {
            "peak_delay_min": round(float(peak["value"]), 2),
            "peak_utc": peak["minute_utc"],
            "peak_direction": peak["direction"],
            "mean_delay_min": round(float(m["value"].mean()), 2),
            "max_trip_delay_min": (
                round(float(m["max_trip_delay_min"].max()), 2)
                if m["max_trip_delay_min"].notna().any()
                else None
            ),
            "n_minutes_above": len(m),
            "n_minutes_observed": int((m["fill_status"] == "observed").sum()),
            "n_minutes_filled": int((m["fill_status"] == "filled").sum()),
            "directions": dirs,
        }


def detect_events(series: pd.DataFrame, settings: Settings) -> list[DetectedEvent]:
    """Run the event state machine over each route's continuous minute grid."""
    events: list[DetectedEvent] = []
    thr = settings.event_threshold_min
    for route_id, rows in series.groupby("route_id", sort=True):
        run: list[dict] = []
        current: DetectedEvent | None = None
        gap = 0
        for row in rows.sort_values("minute_utc").to_dict("records"):
            known = row["fill_status"] in ("observed", "filled") and pd.notna(row["value"])
            above = known and row["value"] >= thr
            if current is None:
                if above:
                    run.append(row)
                    n_obs = sum(r["fill_status"] == "observed" for r in run)
                    if (
                        len(run) >= settings.event_min_minutes
                        and n_obs >= settings.event_min_observed
                    ):
                        current = DetectedEvent(
                            route_id, run[0]["minute_utc"], row["minute_utc"], minutes=list(run)
                        )
                        run, gap = [], 0
                else:
                    run = []
                continue
            if above:
                current.minutes.append(row)
                current.last_above = row["minute_utc"]
                gap = 0
            elif known:
                current.status, current.end_reason = STATUS_ENDED, END_RECOVERED
                events.append(current)
                current, gap = None, 0
            else:
                gap += 1
                if gap > settings.event_max_gap_min:
                    current.status, current.end_reason = STATUS_ENDED, END_DATA_GAP
                    events.append(current)
                    current, gap = None, 0
        if current is not None:
            events.append(current)  # still ongoing at the end of the data
    return events


def make_event_id(route_id: str, start: pd.Timestamp) -> str:
    return f"{route_id}-{pd.Timestamp(start):%Y%m%dT%H%MZ}"


def merge_with_stored(
    detected: list[DetectedEvent],
    stored: pd.DataFrame,
    settings: Settings,
    now: pd.Timestamp,
) -> tuple[list[tuple[str, pd.Timestamp, DetectedEvent]], list[dict]]:
    """Keep event ids stable across runs.

    A detected event that overlaps a stored event of the same line (for example
    because the detection window cut its beginning) reuses the stored id and
    start. Stored ongoing events that no detection covers any more and that have
    not been updated for longer than the gap tolerance are closed as ``data_gap``.
    Returns (event_id, start, detected) tuples and stored rows to close.
    """
    tol = pd.Timedelta(minutes=settings.event_max_gap_min)
    used: set[str] = set()
    merged = []
    for d in detected:
        event_id, start = make_event_id(d.route_id, d.start), d.start
        if not stored.empty:
            same = stored[stored["route_id"] == d.route_id]
            ov = same[(same["start_utc"] <= d.end + tol) & (same["end_utc"] >= d.start - tol)]
            if not ov.empty:
                first = ov.sort_values("start_utc").iloc[0]
                event_id = first["event_id"]
                start = min(pd.Timestamp(first["start_utc"]), d.start)
        used.add(event_id)
        merged.append((event_id, start, d))
    to_close = []
    if not stored.empty:
        stale = stored[
            (stored["status"] == STATUS_ONGOING)
            & ~stored["event_id"].isin(used)
            & (pd.to_datetime(stored["end_utc"]) < now - tol)
        ]
        to_close = stale.to_dict("records")
    return merged, to_close


# --------------------------------------------------------------------------------------
# Affected stations and alerts
# --------------------------------------------------------------------------------------


def affected_stations(
    con: duckdb.DuckDBPyConnection, route_id: str, start, end, threshold: float
) -> pd.DataFrame:
    """Stations where trains of the line were observed at least ``threshold`` late."""
    return con.execute(
        """
        SELECT station_id, any_value(stop_name) AS stop_name,
               max(delay_max_min) AS max_delay_min, count(DISTINCT bucket_utc) AS n_minutes
        FROM station_minute
        WHERE route_id = $route AND bucket_utc >= $start AND bucket_utc < $end
          AND delay_max_min >= $thr
        GROUP BY station_id
        ORDER BY max_delay_min DESC, station_id
        """,
        {"route": route_id, "start": start, "end": end, "thr": threshold},
    ).df()


def link_alerts(
    con: duckdb.DuckDBPyConnection, route_id: str, start, end, station_ids: list[str]
) -> list[dict]:
    """MTA alerts for the line whose active time overlaps the event.

    An alert's time is its active periods, bounded by when it was actually seen in
    the feed (open-ended alerts end when they disappear). Unplanned alerts rank
    first, then alerts naming more of the affected stations.
    """
    df = con.execute(
        """
        WITH latest AS (
            SELECT * FROM alerts
            QUALIFY row_number() OVER (PARTITION BY alert_id ORDER BY updated_utc DESC) = 1
        ),
        seen AS (
            SELECT alert_id, min(bucket_utc) AS first_seen, max(bucket_utc) AS last_seen
            FROM alert_presence GROUP BY alert_id
        )
        SELECT l.alert_id, l.alert_type, l.is_planned, l.header_text, l.updated_utc,
               l.active_periods, l.station_ids, s.first_seen, s.last_seen
        FROM latest l LEFT JOIN seen s USING (alert_id)
        WHERE list_contains(l.route_ids, $route)
        """,
        {"route": route_id},
    ).df()
    if df.empty:
        return []
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    affected = set(station_ids)
    for r in df.to_dict("records"):
        lo = pd.Timestamp(r["first_seen"]) if pd.notna(r["first_seen"]) else None
        hi = (
            pd.Timestamp(r["last_seen"]) + pd.Timedelta(minutes=5)
            if pd.notna(r["last_seen"])
            else None
        )
        if lo is not None and (hi < start or lo > end):
            continue
        periods = list(r["active_periods"]) if r["active_periods"] is not None else []
        if periods:

            def overlaps(p):
                ps = pd.Timestamp(p["start_utc"]) if p["start_utc"] is not None else None
                pe = pd.Timestamp(p["end_utc"]) if p["end_utc"] is not None else None
                return (ps is None or ps <= end) and (pe is None or pe >= start)

            if not any(overlaps(p) for p in periods):
                continue
        stations = set(r["station_ids"]) if r["station_ids"] is not None else set()
        out.append(
            {
                "alert_id": r["alert_id"],
                "alert_type": r["alert_type"],
                "is_planned": bool(r["is_planned"]),
                "header_text": r["header_text"],
                "n_matching_stations": len(stations & affected),
                "_updated": r["updated_utc"],
            }
        )
    out.sort(
        key=lambda a: (
            a["is_planned"],
            -a["n_matching_stations"],
            -pd.Timestamp(a["_updated"]).value,
        )
    )
    for a in out:
        a.pop("_updated")
    return out[:MAX_LINKED_ALERTS]


# --------------------------------------------------------------------------------------
# Surface context
# --------------------------------------------------------------------------------------


def surface_series(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    baselines: pd.DataFrame,
    focal_station_ids: list[str],
    start,
    end,
) -> pd.DataFrame:
    """Per minute: traffic speed and bikes near the focal stations, with baselines.

    Comparisons use only entities that have both a fresh value and a baseline in
    that minute (``*_cmp`` columns), so observed and usual values describe the
    same links/stations. Unmatched totals are kept for display.
    """
    minutes = pd.date_range(
        pd.Timestamp(start).floor("min"), pd.Timestamp(end).floor("min"), freq="min"
    )
    out = pd.DataFrame({"minute_utc": minutes})
    nb = con.execute(
        "SELECT neighbor_type, neighbor_id FROM station_neighbors "
        "WHERE list_contains($ids, station_id)",
        {"ids": list(focal_station_ids)},
    ).df()
    links = sorted(nb.loc[nb["neighbor_type"] == "traffic_link", "neighbor_id"].unique())
    bikes = sorted(nb.loc[nb["neighbor_type"] == "bike_station", "neighbor_id"].unique())
    age = settings.surface_fill_max_age_min

    t = entity_filled(
        con,
        "traffic_minute",
        "link_id",
        "observed_utc",
        ["speed_mph"],
        links,
        start,
        end,
        age,
        where="is_valid",
    )
    t = lookup(baselines, "traffic_speed_mph", t, settings.baseline_slot_min)
    t["has_value"] = t["speed_mph"].notna()
    t["cmp"] = t["has_value"] & t["baseline"].notna()
    t["ok"] = t["cmp"] & (t["baseline_confidence"] == CONFIDENCE_OK)
    t["speed_cmp"] = t["speed_mph"].where(t["cmp"])
    t["base_cmp"] = t["baseline"].where(t["cmp"])
    tg = (
        t.groupby("minute_utc").agg(
            traffic_speed_mph=("speed_mph", "mean"),
            traffic_n_links=("has_value", "sum"),
            traffic_speed_cmp=("speed_cmp", "mean"),
            traffic_baseline_mph=("base_cmp", "mean"),
            traffic_n_cmp=("cmp", "sum"),
            traffic_n_ok=("ok", "sum"),
        )
        if len(t)
        else pd.DataFrame()
    )

    stale_s = float(settings.bike_station_stale_after_s)
    bike_where = f"is_installed AND is_renting AND report_age_s <= {stale_s}"
    b = entity_filled(
        con,
        "bike_minute",
        "station_id",
        "bucket_utc",
        ["bikes_available", "docks_available"],
        bikes,
        start,
        end,
        min(age, 15),
        where=bike_where,
    )
    b = lookup(baselines, "bikes_available", b, settings.baseline_slot_min)
    b["has_value"] = b["bikes_available"].notna()
    b["cmp"] = b["has_value"] & b["baseline"].notna()
    b["ok"] = b["cmp"] & (b["baseline_confidence"] == CONFIDENCE_OK)
    b["bikes_cmp"] = b["bikes_available"].astype(float).where(b["cmp"])
    b["base_cmp"] = b["baseline"].where(b["cmp"])
    b["bikes_val"] = b["bikes_available"].astype(float)
    bg = (
        b.groupby("minute_utc").agg(
            bikes_available=("bikes_val", lambda s: s.sum(min_count=1)),
            bikes_n_stations=("has_value", "sum"),
            bikes_cmp=("bikes_cmp", lambda s: s.sum(min_count=1)),
            bikes_baseline=("base_cmp", lambda s: s.sum(min_count=1)),
            bikes_n_cmp=("cmp", "sum"),
            bikes_n_ok=("ok", "sum"),
        )
        if len(b)
        else pd.DataFrame()
    )

    for frame in (tg, bg):
        if not frame.empty:
            out = out.merge(frame.reset_index(), on="minute_utc", how="left")
    for col in (
        "traffic_speed_mph",
        "traffic_n_links",
        "traffic_speed_cmp",
        "traffic_baseline_mph",
        "traffic_n_cmp",
        "traffic_n_ok",
        "bikes_available",
        "bikes_n_stations",
        "bikes_cmp",
        "bikes_baseline",
        "bikes_n_cmp",
        "bikes_n_ok",
    ):
        if col not in out.columns:
            out[col] = np.nan
    out.attrs["n_links_nearby"] = len(links)
    out.attrs["n_bike_stations_nearby"] = len(bikes)
    return out


def _confidence(n_ok: pd.Series, n_cmp: pd.Series) -> str:
    total = float(n_cmp.sum())
    if total == 0:
        return CONFIDENCE_NONE
    return CONFIDENCE_OK if float(n_ok.sum()) / total >= 0.5 else CONFIDENCE_LOW


def summarize_surface(series: pd.DataFrame, start, end) -> dict:
    """Event-window averages of the surface series (NULL when there is no data)."""
    w = series[
        (series["minute_utc"] >= pd.Timestamp(start)) & (series["minute_utc"] < pd.Timestamp(end))
    ]

    def mean(col):
        v = w[col].astype(float)
        return round(float(v.mean()), 1) if v.notna().any() else None

    out: dict = {}
    has_cmp = (w["traffic_n_cmp"].fillna(0) > 0).any()
    out["traffic_n_links"] = int(w["traffic_n_links"].fillna(0).max()) if len(w) else 0
    out["traffic_speed_mph"] = mean("traffic_speed_cmp") if has_cmp else mean("traffic_speed_mph")
    out["traffic_baseline_mph"] = mean("traffic_baseline_mph") if has_cmp else None
    out["traffic_baseline_confidence"] = _confidence(
        w["traffic_n_ok"].fillna(0), w["traffic_n_cmp"].fillna(0)
    )
    if out["traffic_speed_mph"] is not None and out["traffic_baseline_mph"]:
        out["traffic_change_pct"] = round(
            (out["traffic_speed_mph"] - out["traffic_baseline_mph"])
            / out["traffic_baseline_mph"]
            * 100,
            1,
        )
    else:
        out["traffic_change_pct"] = None

    has_bcmp = (w["bikes_n_cmp"].fillna(0) > 0).any()
    out["bikes_n_stations"] = (
        int(w["bikes_n_cmp" if has_bcmp else "bikes_n_stations"].fillna(0).max()) if len(w) else 0
    )
    out["bikes_available"] = mean("bikes_cmp") if has_bcmp else mean("bikes_available")
    out["bikes_baseline"] = mean("bikes_baseline") if has_bcmp else None
    out["bikes_baseline_confidence"] = _confidence(
        w["bikes_n_ok"].fillna(0), w["bikes_n_cmp"].fillna(0)
    )
    if out["bikes_available"] is not None and out["bikes_baseline"] is not None:
        out["bikes_change"] = round(out["bikes_available"] - out["bikes_baseline"], 1)
        out["bikes_change_pct"] = (
            round(out["bikes_change"] / out["bikes_baseline"] * 100, 1)
            if out["bikes_baseline"]
            else None
        )
    else:
        out["bikes_change"] = out["bikes_change_pct"] = None
    return out


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def stored_events(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute("SELECT * FROM events").df()


def process_events(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    baselines: pd.DataFrame,
    now: dt.datetime,
    run_id: str,
) -> pd.DataFrame:
    """Detect events in the lookback window and build one new version per event."""
    from .explain import explain  # local import: explain depends on event fields

    now_ts = pd.Timestamp(now.replace(tzinfo=None) if now.tzinfo else now).floor("min")
    window_start = now_ts - pd.Timedelta(hours=settings.event_lookback_hours)
    series = route_series(con, settings, window_start.to_pydatetime(), now_ts.to_pydatetime())
    detected = detect_events(series, settings)
    stored = stored_events(con)
    merged, to_close = merge_with_stored(detected, stored, settings, now_ts)

    rows = []
    for event_id, start, d in merged:
        st = d.stats()
        end = d.end
        stations = affected_stations(con, d.route_id, start, end, settings.event_threshold_min)
        focal = stations["station_id"].head(settings.event_context_stations).tolist()
        alerts = link_alerts(con, d.route_id, start, end, stations["station_id"].tolist())
        surface = summarize_surface(
            surface_series(con, settings, baselines, focal, start, end), start, end
        )
        row = {
            "bucket_utc": pd.Timestamp(start).floor("min"),
            "computed_utc": now_ts,
            "run_id": run_id,
            "event_id": event_id,
            "route_id": d.route_id,
            "status": d.status,
            "end_reason": d.end_reason,
            "start_utc": pd.Timestamp(start),
            "end_utc": end,
            "duration_min": (end - pd.Timestamp(start)).total_seconds() / 60.0,
            "metric": settings.event_metric,
            "threshold_min": settings.event_threshold_min,
            "min_minutes": settings.event_min_minutes,
            "affected_station_ids": stations["station_id"].tolist(),
            "affected_station_names": stations["stop_name"].tolist(),
            "affected_station_max_delay_min": [
                round(float(x), 2) for x in stations["max_delay_min"]
            ],
            "focal_station_ids": focal,
            "linked_alerts": alerts,
            "n_linked_alerts": len(alerts),
            "neighbor_radius_m": settings.neighbor_radius_m,
            **st,
            **surface,
        }
        row["summary"], cited = explain(row)
        row["cited_metrics"] = json.dumps(cited, sort_keys=True)
        rows.append(row)
    for old in to_close:
        row = dict(old)
        row.update(
            computed_utc=now_ts,
            run_id=run_id,
            status=STATUS_ENDED,
            end_reason=END_DATA_GAP,
        )
        row["summary"], cited = explain(row)
        row["cited_metrics"] = json.dumps(cited, sort_keys=True)
        rows.append(row)
    # Only store a new version when something about the event changed.
    latest = {} if stored.empty else {r["event_id"]: r for r in stored.to_dict("records")}
    rows = [r for r in rows if fingerprint(r) != fingerprint(latest.get(r["event_id"]))]
    return pd.DataFrame(rows)


_VOLATILE = {"computed_utc", "run_id", "bucket_utc", "date"}


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(v) for v in value]
    if value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp | dt.datetime | np.datetime64):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return round(value, 3)
    return value


def fingerprint(row: dict | None) -> str | None:
    """Comparable content of an event version (ignores when/where it was computed)."""
    if row is None:
        return None
    from .storage import SCHEMAS

    fields = [f for f in SCHEMAS["events"].names if f not in _VOLATILE]
    return json.dumps({f: _plain(row.get(f)) for f in fields}, sort_keys=True, default=str)


def event_chart_series(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    baselines: pd.DataFrame,
    event: dict,
    pad_min: int,
) -> pd.DataFrame:
    """Minute series around an event for charts: line delay + surface context."""
    start = pd.Timestamp(event["start_utc"]) - pd.Timedelta(minutes=pad_min)
    end = pd.Timestamp(event["end_utc"]) + pd.Timedelta(minutes=pad_min)
    if event.get("computed_utc") is not None and pd.notna(event.get("computed_utc")):
        # Never draw forward-filled values for minutes after the computation time.
        end = min(end, pd.Timestamp(event["computed_utc"]))
    rs = route_series(con, settings, start.to_pydatetime(), end.to_pydatetime())
    rs = rs[rs["route_id"] == event["route_id"]][["minute_utc", "value", "fill_status"]]
    focal = as_list(event.get("focal_station_ids"))
    surf = surface_series(con, settings, baselines, focal, start, end)
    return surf.merge(rs, on="minute_utc", how="left")
