"""Ingestion of the non-subway sources: service alerts, traffic speed, Citi Bike.

Each source returns quality records (one per fetch) plus rows for the bucketed
tables and upserts for the reference tables. Fetching goes through an injectable
``UrlFetcher`` so tests run offline.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import quality as q
from .alerts import AlertsParseError, is_active, parse_alerts
from .bikes import (
    GbfsError,
    choose_version,
    parse_discovery,
    parse_station_information,
    parse_station_status,
    parse_versions,
)
from .config import Settings
from .feeds import FetchResult, fetch_url, make_session, mta_headers
from .state import PipelineState
from .storage import bucket_of, epoch_to_utc, read_reference
from .traffic import SOQL_LATEST, TrafficParseError, parse_latest, parse_speeds, soql_since

log = logging.getLogger(__name__)

# (source, url, params, headers) -> FetchResult
UrlFetcher = Callable[[str, str, dict | None, dict | None], FetchResult]


class HttpUrlFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = make_session(settings)

    def __call__(self, source, url, params=None, headers=None) -> FetchResult:
        return fetch_url(
            source, url, self.session, self.settings.http_timeout_s, headers=headers, params=params
        )


@dataclass
class SourceOutput:
    records: list[q.SourceRecord] = field(default_factory=list)
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    # name -> (frame, key column) to upsert into reference/<name>.parquet
    references: dict[str, tuple[pd.DataFrame, str]] = field(default_factory=dict)
    # Set when a source rewrote a reference table itself (the spatial join is redone).
    references_changed: bool = False


def _record(run_id: str, source: str, res: FetchResult) -> q.SourceRecord:
    return q.SourceRecord(
        run_id=run_id,
        source=source,
        url=res.url,
        fetched_ts=res.fetched_at,
        http_status=res.http_status,
        fetch_error=res.error,
        fetch_elapsed_s=round(res.elapsed_s, 3) if res.elapsed_s is not None else None,
        n_bytes=len(res.content or b""),
    )


# --------------------------------------------------------------------------------------
# Service alerts
# --------------------------------------------------------------------------------------


class AlertsSource:
    name = "alerts"

    def __init__(
        self, settings: Settings, fetch: UrlFetcher, clock: Callable[[], float] = time.time
    ):
        self.settings = settings
        self.fetch = fetch
        self.clock = clock

    def run(self, run_id: str, state: PipelineState, data_dir=None) -> SourceOutput:
        s = self.settings
        st = state.source(self.name)
        res = self.fetch(self.name, s.alerts_url, None, mta_headers(s))
        rec = _record(run_id, self.name, res)
        rec.gap_min = q.gap_minutes(st.get("last_ok_ts"), res.fetched_at, s.source_gap_min)
        out = SourceOutput(records=[rec])
        if not res.ok:
            rec.status, rec.detail = q.STATUS_OUTAGE, res.error
            return out
        try:
            parsed = parse_alerts(res.content)
        except AlertsParseError as exc:
            rec.status, rec.detail = q.STATUS_INVALID, str(exc)
            return out

        alerts = parsed.alerts
        rec.n_records = len(alerts)
        rec.observed_ts = parsed.header_ts
        if not parsed.header_ts:
            rec.status, rec.detail = q.STATUS_INVALID, "header timestamp missing"
            return out
        rec.feed_age_s = round(res.fetched_at - parsed.header_ts, 1)
        if rec.feed_age_s > s.alerts_stale_after_s:
            rec.status = q.STATUS_STALE
            rec.detail = f"header age {rec.feed_age_s:.0f}s > {s.alerts_stale_after_s}s"
            return out
        # Presence is confirmed at fetch time, even when nothing changed.
        active = [is_active(p, res.fetched_at) for p in alerts["active_periods"]]
        alerts = alerts.assign(is_active=active)
        seen: dict = st.get("seen", {})
        changed = alerts["updated_ts"].astype("float").fillna(-1).to_numpy() != np.array(
            [float(seen.get(a, -2)) for a in alerts["alert_id"]]
        )
        new_versions = alerts[changed]
        rec.n_valid = int(alerts["is_active"].sum())
        rec.n_new = len(new_versions)
        rec.notes = {
            "n_active": rec.n_valid,
            "n_active_unplanned": int((alerts["is_active"] & ~alerts["is_planned"]).sum()),
            "top_types": dict(Counter(alerts["alert_type"].dropna()).most_common(5)),
        }

        bucket = bucket_of(pd.Series([res.fetched_at])).iloc[0]
        observed = epoch_to_utc([parsed.header_ts]).iloc[0]
        fetched = epoch_to_utc([res.fetched_at]).iloc[0]
        if len(new_versions):
            out.tables["alerts"] = pd.DataFrame(
                {
                    "bucket_utc": bucket,
                    "observed_utc": observed,
                    "fetched_utc": fetched,
                    "run_id": run_id,
                    "alert_id": new_versions["alert_id"].to_numpy(),
                    "alert_type": new_versions["alert_type"].to_numpy(),
                    "is_planned": new_versions["is_planned"].to_numpy(),
                    "created_utc": epoch_to_utc(new_versions["created_ts"]).to_numpy(),
                    "updated_utc": epoch_to_utc(new_versions["updated_ts"]).to_numpy(),
                    "active_start_utc": epoch_to_utc(new_versions["active_start_ts"]).to_numpy(),
                    "active_end_utc": epoch_to_utc(new_versions["active_end_ts"]).to_numpy(),
                    "active_periods": [
                        [{"start_utc": _ts(a), "end_utc": _ts(b)} for a, b in periods]
                        for periods in new_versions["active_periods"]
                    ],
                    "route_ids": list(new_versions["route_ids"]),
                    "station_ids": list(new_versions["station_ids"]),
                    "header_text": new_versions["header_text"].to_numpy(),
                    "description_text": new_versions["description_text"].to_numpy(),
                }
            )
        out.tables["alert_presence"] = pd.DataFrame(
            {
                "bucket_utc": bucket,
                "observed_utc": observed,
                "run_id": run_id,
                "alert_id": alerts["alert_id"].to_numpy(),
                "updated_utc": epoch_to_utc(alerts["updated_ts"]).to_numpy(),
                "is_active": alerts["is_active"].to_numpy(),
            }
        )
        st["seen"] = {
            a: (int(u) if pd.notna(u) else None)
            for a, u in zip(alerts["alert_id"], alerts["updated_ts"], strict=True)
        }
        st["last_header_ts"] = parsed.header_ts
        st["last_ok_ts"] = res.fetched_at
        return out


def _ts(epoch) -> pd.Timestamp | None:
    return None if epoch is None else pd.Timestamp(epoch, unit="s")


# --------------------------------------------------------------------------------------
# Traffic speed
# --------------------------------------------------------------------------------------


class TrafficSource:
    name = "traffic"
    # Rows asked for when (re)building link geometry without recent observations.
    GEOMETRY_ROWS = 2000

    def __init__(
        self, settings: Settings, fetch: UrlFetcher, clock: Callable[[], float] = time.time
    ):
        self.settings = settings
        self.fetch = fetch
        self.clock = clock

    def _headers(self) -> dict:
        token = self.settings.socrata_app_token
        return {"X-App-Token": token} if token else {}

    def run(self, run_id: str, state: PipelineState, data_dir) -> SourceOutput:
        s = self.settings
        st = state.source(self.name)
        last_obs: dict[str, int] = {k: int(v) for k, v in st.get("last_obs", {}).items()}
        now = self.clock()
        since = max(now - s.traffic_lookback_min * 60, float(st.get("max_observed_ts", 0)))
        res = self.fetch(self.name, s.traffic_url, soql_since(since), self._headers())
        rec = _record(run_id, self.name, res)
        rec.gap_min = q.gap_minutes(st.get("last_ok_ts"), res.fetched_at, s.source_gap_min)
        out = SourceOutput(records=[rec])
        if not res.ok:
            rec.status, rec.detail = q.STATUS_OUTAGE, res.error
            return out
        try:
            parsed = parse_speeds(res.content, res.fetched_at)
        except TrafficParseError as exc:
            rec.status, rec.detail = q.STATUS_INVALID, str(exc)
            return out

        obs = parsed.observations
        rec.n_records = parsed.n_rows
        prev = obs["link_id"].map(last_obs).astype("Float64").fillna(-1)
        new = obs[obs["observed_ts"].astype("Float64") > prev]
        rec.n_new = len(new)
        rec.n_valid = int(new["is_valid"].sum())
        rec.n_invalid = int((~new["is_valid"]).sum()) + parsed.n_bad_rows

        newest = int(obs["observed_ts"].max()) if len(obs) else None
        if newest is None:
            latest = self.fetch(self.name, s.traffic_url, SOQL_LATEST, self._headers())
            if latest.ok:
                try:
                    newest = parse_latest(latest.content, latest.fetched_at)
                except TrafficParseError:
                    newest = None
        newest = max(filter(None, [newest, st.get("max_observed_ts")]), default=None)
        if newest is None:
            rec.status, rec.detail = q.STATUS_EMPTY, "no traffic speed observations"
            return out
        rec.observed_ts = newest
        rec.feed_age_s = round(res.fetched_at - newest, 1)

        for link, ts in new.groupby("link_id")["observed_ts"].max().items():
            last_obs[link] = int(ts)
        links_ref = read_reference(data_dir, "traffic_links")
        known = set(links_ref["link_id"]) | set(last_obs)
        fresh = {k for k, ts in last_obs.items() if res.fetched_at - ts <= s.traffic_stale_after_s}
        rec.n_stale = len(known - fresh)
        rec.coverage = len(fresh) / len(known) if known else None
        rec.notes = {"n_links_known": len(known), "n_links_fresh": len(fresh)}

        if rec.feed_age_s > s.traffic_stale_after_s:
            rec.status = q.STATUS_STALE
            rec.detail = (
                f"newest observation is {rec.feed_age_s / 60:.0f} min old "
                f"(> {s.traffic_stale_after_s // 60} min)"
            )
        else:
            st["last_ok_ts"] = res.fetched_at

        if len(new):
            out.tables["traffic_minute"] = pd.DataFrame(
                {
                    "bucket_utc": bucket_of(new["observed_ts"]).to_numpy(),
                    "observed_utc": epoch_to_utc(new["observed_ts"]).to_numpy(),
                    "fetched_utc": epoch_to_utc([res.fetched_at] * len(new)).to_numpy(),
                    "run_id": run_id,
                    "link_id": new["link_id"].to_numpy(),
                    "speed_mph": new["speed_mph"].to_numpy(),
                    "travel_time_s": new["travel_time_s"].to_numpy(),
                    "dot_status": new["dot_status"].astype("Int32").to_numpy(),
                    "is_valid": new["is_valid"].to_numpy(),
                    "age_s": (res.fetched_at - new["observed_ts"].astype(float))
                    .round(1)
                    .to_numpy(),
                }
            )

        links = parsed.links
        if links_ref.empty and links.empty:
            links = self._bootstrap_geometry(res.fetched_at)
        if not links.empty:
            out.references["traffic_links"] = (
                links.assign(last_seen_utc=epoch_to_utc([res.fetched_at] * len(links)).to_numpy()),
                "link_id",
            )
        st["last_obs"] = last_obs
        st["max_observed_ts"] = newest
        return out

    def _bootstrap_geometry(self, now: float) -> pd.DataFrame:
        """Link geometry from the newest rows, even if they are old (stale feed)."""
        params = {"$order": "data_as_of DESC", "$limit": str(self.GEOMETRY_ROWS)}
        res = self.fetch(self.name, self.settings.traffic_url, params, self._headers())
        if not res.ok:
            return pd.DataFrame()
        try:
            return parse_speeds(res.content, now).links
        except TrafficParseError:
            return pd.DataFrame()


# --------------------------------------------------------------------------------------
# Citi Bike (GBFS)
# --------------------------------------------------------------------------------------


class BikesSource:
    name = "bike_status"

    def __init__(
        self, settings: Settings, fetch: UrlFetcher, clock: Callable[[], float] = time.time
    ):
        self.settings = settings
        self.fetch = fetch
        self.clock = clock

    def _discover(self, st: dict, now: float, out: SourceOutput, run_id: str) -> dict | None:
        """GBFS auto-discovery, cached for ``bike_info_max_age_hours``."""
        s = self.settings
        cached = st.get("discovery")
        if cached and now - cached.get("at", 0) < s.bike_info_max_age_hours * 3600:
            return cached
        res = self.fetch("bike_discovery", s.gbfs_url, None, None)
        rec = _record(run_id, "bike_discovery", res)
        out.records.append(rec)
        try:
            if not res.ok:
                raise GbfsError(res.error or "fetch failed")
            disc = parse_discovery(res.content, s.gbfs_language)
            version = disc.version
            if "gbfs_versions" in disc.feeds:
                vres = self.fetch("bike_discovery", disc.feeds["gbfs_versions"], None, None)
                if vres.ok:
                    chosen = choose_version(parse_versions(vres.content))
                    if chosen and chosen[0] != str(disc.version).lstrip("vV"):
                        dres = self.fetch("bike_discovery", chosen[1], None, None)
                        if dres.ok:
                            disc = parse_discovery(dres.content, s.gbfs_language)
                            version = chosen[0]
            missing = {"station_information", "station_status"} - set(disc.feeds)
            if missing:
                raise GbfsError(f"discovery lacks feeds: {sorted(missing)}")
        except GbfsError as exc:
            rec.status = q.STATUS_OUTAGE if not res.ok else q.STATUS_INVALID
            rec.detail = str(exc)
            if cached:
                rec.notes = {"using_cached_discovery": True}
            return cached
        result = {
            "at": now,
            "version": version,
            "station_information": disc.feeds["station_information"],
            "station_status": disc.feeds["station_status"],
        }
        rec.n_records = len(disc.feeds)
        rec.notes = {"version": version}
        st["discovery"] = result
        return result

    def run(self, run_id: str, state: PipelineState, data_dir) -> SourceOutput:
        s = self.settings
        st = state.source(self.name)
        out = SourceOutput()
        now = self.clock()
        disc = self._discover(st, now, out, run_id)
        if disc is None:
            rec = q.SourceRecord(run_id, self.name, s.gbfs_url, now)
            rec.status, rec.detail = q.STATUS_OUTAGE, "GBFS discovery failed"
            out.records.append(rec)
            return out

        stations_ref = read_reference(data_dir, "bike_stations")
        info_age_s = now - st.get("info_ts", 0)
        if stations_ref.empty or info_age_s > s.bike_info_max_age_hours * 3600:
            self._station_information(disc, st, out, run_id)

        res = self.fetch(self.name, disc["station_status"], None, None)
        rec = _record(run_id, self.name, res)
        rec.gap_min = q.gap_minutes(st.get("last_ok_ts"), res.fetched_at, s.source_gap_min)
        out.records.append(rec)
        if not res.ok:
            rec.status, rec.detail = q.STATUS_OUTAGE, res.error
            return out
        try:
            status = parse_station_status(res.content)
        except GbfsError as exc:
            rec.status, rec.detail = q.STATUS_INVALID, str(exc)
            return out

        df = status.stations
        rec.n_records = len(df)
        rec.observed_ts = status.last_updated
        if not status.last_updated:
            rec.status, rec.detail = q.STATUS_INVALID, "last_updated missing"
            return out
        rec.feed_age_s = round(res.fetched_at - status.last_updated, 1)
        if df.empty:
            rec.status, rec.detail = q.STATUS_EMPTY, "no stations in station_status"
            return out
        if rec.feed_age_s > s.bike_feed_stale_after_s:
            rec.status = q.STATUS_STALE
            rec.detail = f"last_updated {rec.feed_age_s:.0f}s ago > {s.bike_feed_stale_after_s}s"
            return out
        if st.get("last_updated") == status.last_updated:
            rec.status, rec.detail = q.STATUS_DUPLICATE, "last_updated unchanged"
            return out

        report_age = status.last_updated - df["last_reported_ts"].astype("Float64")
        stale = (report_age > s.bike_station_stale_after_s).fillna(True).astype(bool)
        installed = df["is_installed"].fillna(False).astype(bool)
        renting = df["is_renting"].fillna(False).astype(bool)
        valid = installed & renting & ~stale
        rec.n_valid = int(valid.sum())
        rec.n_stale = int((installed & stale).sum())
        rec.n_invalid = status.n_invalid_reported
        rec.n_new = len(df)
        rec.coverage = float(valid.sum()) / len(df)
        rec.notes = {
            "bikes_available_fresh": int(df.loc[valid, "bikes_available"].sum()),
            "docks_available_fresh": int(df.loc[valid, "docks_available"].sum()),
            "n_not_installed": int((~installed).sum()),
            "n_not_renting": int((installed & ~renting).sum()),
            "ttl_s": status.ttl,
        }
        out.tables["bike_minute"] = pd.DataFrame(
            {
                "bucket_utc": bucket_of(pd.Series([status.last_updated] * len(df))).to_numpy(),
                "observed_utc": epoch_to_utc([status.last_updated] * len(df)).to_numpy(),
                "fetched_utc": epoch_to_utc([res.fetched_at] * len(df)).to_numpy(),
                "run_id": run_id,
                "station_id": df["station_id"].to_numpy(),
                "bikes_available": df["bikes_available"].astype("Int32").to_numpy(),
                "ebikes_available": df["ebikes_available"].astype("Int32").to_numpy(),
                "docks_available": df["docks_available"].astype("Int32").to_numpy(),
                "bikes_disabled": df["bikes_disabled"].astype("Int32").to_numpy(),
                "docks_disabled": df["docks_disabled"].astype("Int32").to_numpy(),
                "is_installed": df["is_installed"].to_numpy(),
                "is_renting": df["is_renting"].to_numpy(),
                "is_returning": df["is_returning"].to_numpy(),
                "last_reported_utc": epoch_to_utc(df["last_reported_ts"]).to_numpy(),
                "report_age_s": report_age.astype(float).to_numpy(),
            }
        )
        st["last_updated"] = status.last_updated
        st["last_ok_ts"] = res.fetched_at
        return out

    def _station_information(self, disc: dict, st: dict, out: SourceOutput, run_id: str) -> None:
        res = self.fetch("bike_info", disc["station_information"], None, None)
        rec = _record(run_id, "bike_info", res)
        out.records.append(rec)
        if not res.ok:
            rec.status, rec.detail = q.STATUS_OUTAGE, res.error
            return
        try:
            info = parse_station_information(res.content, self.settings.gbfs_language)
        except GbfsError as exc:
            rec.status, rec.detail = q.STATUS_INVALID, str(exc)
            return
        rec.n_records = rec.n_valid = rec.n_new = len(info.stations)
        rec.observed_ts = info.last_updated
        if info.last_updated:
            rec.feed_age_s = round(res.fetched_at - info.last_updated, 1)
        if info.stations.empty:
            rec.status, rec.detail = q.STATUS_EMPTY, "no stations in station_information"
            return
        stations = info.stations.assign(
            capacity=info.stations["capacity"].astype("Int32"),
            last_seen_utc=epoch_to_utc([res.fetched_at] * len(info.stations)).to_numpy(),
        )
        out.references["bike_stations"] = (stations, "station_id")
        st["info_ts"] = res.fetched_at


# --------------------------------------------------------------------------------------
# Buses (MTA Bus Time SIRI, M5)
# --------------------------------------------------------------------------------------


class BusSource:
    """Bus positions from Bus Time SIRI VehicleMonitoring -> segment speeds.

    ``poll()`` takes one snapshot of all buses and is called by the pipeline during
    each subway poll cycle (about a minute apart), never more often than
    ``bus_min_request_interval_s``; ``run()`` turns consecutive snapshots into
    speeds. Without ``MTA_BUS_API_KEY``, or when the key is rejected, nothing is
    requested again in the run and the source is reported ``unavailable``.
    """

    name = "bus"

    def __init__(
        self, settings: Settings, fetch: UrlFetcher, clock: Callable[[], float] = time.time
    ):
        self.settings = settings
        self.fetch = fetch
        self.clock = clock
        self.snapshots: list[FetchResult] = []
        self._last_request: float | None = None
        self._auth_error: str | None = None

    def poll(self) -> None:
        s = self.settings
        if not s.mta_bus_api_key or self._auth_error:
            return
        now = self.clock()
        if (
            self._last_request is not None
            and now - self._last_request < s.bus_min_request_interval_s
        ):
            return
        self._last_request = now
        # SIRI version 1 (the default): version 2 responses omit Extensions.Distances
        # (CallDistanceAlongRoute), without which no speed can be computed.
        params = {"key": s.mta_bus_api_key, "VehicleMonitoringDetailLevel": "normal"}
        res = self.fetch(self.name, s.bus_siri_url, params, None)
        if res.http_status in (401, 403):
            self._auth_error = f"API key rejected (HTTP {res.http_status})"
        self.snapshots.append(res)

    def run(self, run_id: str, state: PipelineState, data_dir) -> SourceOutput:
        from .bus import (
            SiriAuthError,
            SiriError,
            bus_minute_rows,
            parse_vehicle_monitoring,
            refresh_bus_static,
            speed_samples,
        )

        s = self.settings
        st = state.source(self.name)
        out = SourceOutput()
        try:  # stop locations are public; refreshed weekly even without a key
            out.references_changed = refresh_bus_static(s)
        except Exception as exc:  # noqa: BLE001
            log.warning("Bus static GTFS refresh failed: %s", exc)
        if not s.mta_bus_api_key:
            rec = q.SourceRecord(run_id, self.name, s.bus_siri_url, self.clock())
            rec.status = q.STATUS_UNAVAILABLE
            rec.detail = "MTA_BUS_API_KEY is not set; bus data skipped"
            log.warning("Bus data skipped: MTA_BUS_API_KEY is not set")
            out.records.append(rec)
            return out
        if not self.snapshots:
            self.poll()
        parsed = []
        for res in self.snapshots:
            rec = _record(run_id, self.name, res)
            rec.gap_min = q.gap_minutes(st.get("last_ok_ts"), res.fetched_at, s.source_gap_min)
            out.records.append(rec)
            if res.http_status in (401, 403):
                rec.status, rec.detail = q.STATUS_UNAVAILABLE, f"API key rejected ({res.error})"
                continue
            if not res.ok:
                rec.status, rec.detail = q.STATUS_OUTAGE, res.error
                continue
            try:
                p = parse_vehicle_monitoring(res.content)
            except SiriAuthError as exc:
                rec.status, rec.detail = q.STATUS_UNAVAILABLE, f"API key rejected: {exc}"
                continue
            except SiriError as exc:
                rec.status, rec.detail = q.STATUS_INVALID, str(exc)
                continue
            v = p.vehicles
            rec.n_records = p.n_activities
            rec.n_invalid = p.n_bad
            rec.n_valid = rec.n_new = len(v)
            newest = float(v["recorded_ts"].max()) if len(v) else None
            rec.observed_ts = newest
            if newest is None:
                rec.status, rec.detail = q.STATUS_EMPTY, "no vehicles in the response"
                continue
            rec.feed_age_s = round(res.fetched_at - newest, 1)
            if rec.feed_age_s > s.bus_stale_after_s:
                rec.status = q.STATUS_STALE
                rec.detail = f"newest vehicle report {rec.feed_age_s:.0f}s old"
                continue
            rec.notes = {"n_routes": int(v["route_id"].nunique())}
            st["last_ok_ts"] = res.fetched_at
            parsed.append((res.fetched_at, v, rec))
        self.snapshots = []
        if len(parsed) < 2:
            return out
        near = _near_bus_stops(data_dir)
        frames = []
        for (_, prev, _), (fetched, cur, rec) in zip(parsed, parsed[1:], strict=False):
            samples = speed_samples(
                prev, cur, s.bus_min_pair_s, s.bus_max_pair_s, s.bus_max_speed_mph
            )
            rec.notes["n_speed_samples"] = len(samples)
            frames.append(bus_minute_rows(samples, run_id, fetched, near))
        rows = (
            pd.concat([f for f in frames if not f.empty], ignore_index=True)
            if any(not f.empty for f in frames)
            else pd.DataFrame()
        )
        if not rows.empty:
            out.tables["bus_minute"] = rows
            parsed[-1][2].coverage = None
            parsed[-1][2].notes["n_segments"] = int(rows["segment_id"].nunique())
        return out


def _near_bus_stops(data_dir) -> set[str] | None:
    """Bus stops within the neighbor radius of a subway station (None: not known yet)."""
    nb = read_reference(data_dir, "station_neighbors")
    if nb.empty or "neighbor_type" not in nb:
        return None
    stops = nb.loc[nb["neighbor_type"] == "bus_stop", "neighbor_id"]
    return set(stops.astype(str)) if len(stops) else None


def build_sources(
    names: list[str],
    settings: Settings,
    fetch: UrlFetcher | None = None,
    clock: Callable[[], float] = time.time,
) -> list:
    fetch = fetch or HttpUrlFetcher(settings)
    registry = {
        "alerts": AlertsSource,
        "traffic": TrafficSource,
        "bikes": BikesSource,
        "bus": BusSource,
    }
    unknown = [n for n in names if n not in registry]
    if unknown:
        raise ValueError(f"Unknown source(s) {unknown}. Known: subway, {', '.join(registry)}")
    return [registry[n](settings, fetch, clock) for n in names]
