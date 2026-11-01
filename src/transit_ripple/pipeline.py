"""One ingestion run: fetch -> parse -> quality checks -> delays -> Parquet + report.

The subway feeds are polled ``polls`` times; the other sources (alerts, traffic,
bikes) are fetched once per run after that, and the spatial join is refreshed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import quality as q
from .config import FeedSpec, Settings
from .delays import compute_stop_delays, line_snapshot, trip_current_delays
from .feeds import Fetcher, FetchResult, save_raw
from .geo import station_neighbors, subway_stations
from .realtime import FeedParseError, parse_feed
from .state import PipelineState
from .static_gtfs import MATCHED_METHODS, StaticInfo, StaticSchedule
from .storage import (
    bucket_of,
    date_partition,
    epoch_to_utc,
    merge_reference,
    read_reference,
    write_reference,
    write_table,
)

log = logging.getLogger(__name__)

DIRECTIONS = ("N", "S")


def make_run_id(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class RunResult:
    report: q.RunReport
    report_path: Path
    stop_delays: pd.DataFrame
    line_minute: pd.DataFrame
    quality_feed: pd.DataFrame
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass
class _SnapshotOutput:
    record: q.FeedPollRecord
    stop_delays: pd.DataFrame | None = None
    lines: pd.DataFrame | None = None
    jumps: list | None = None


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        schedule: StaticSchedule,
        static_info: StaticInfo,
        feeds: list[FeedSpec],
        fetcher: Fetcher,
        save_raw: bool = False,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        sources: list | None = None,
    ):
        self.settings = settings
        self.schedule = schedule
        self.static_info = static_info
        self.feeds = feeds
        self.fetcher = fetcher
        self.save_raw = save_raw
        self.clock = clock
        self.sleep = sleep
        self.data_dir = Path(settings.data_dir)
        self.state_path = self.data_dir / "state.json"
        self.sources = sources or []

    # ------------------------------------------------------------------------------

    def run(
        self,
        polls: int = 1,
        interval_s: float = 30.0,
        batches: list[list[FetchResult]] | None = None,
    ) -> RunResult:
        """Run ``polls`` fetch cycles (or process pre-fetched ``batches``)."""
        started = self.clock()
        run_id = make_run_id(started)
        state = PipelineState.load(self.state_path)
        report = q.RunReport(
            run_id=run_id,
            started_ts=started,
            polls_requested=len(batches) if batches is not None else polls,
            thresholds=self.settings.thresholds(),
            static=self.static_info.to_report(),
        )
        specs = {s.feed_id: s for s in self.feeds}
        stop_frames, line_frames = [], []

        n_polls = len(batches) if batches is not None else polls
        for poll_index in range(n_polls):
            cycle_start = self.clock()
            results = batches[poll_index] if batches is not None else self.fetcher(self.feeds)
            for res in results:
                spec = specs.get(res.feed_id)
                if spec is None:
                    log.warning("Ignoring result for unknown feed %s", res.feed_id)
                    continue
                out = self._process(res, spec, poll_index, run_id, state)
                report.records.append(out.record)
                if out.stop_delays is not None and not out.stop_delays.empty:
                    stop_frames.append(out.stop_delays)
                if out.lines is not None and not out.lines.empty:
                    line_frames.append(out.lines)
                if out.jumps:
                    report.delay_jumps.extend(out.jumps)
            for source in self.sources:
                # Sources that need several snapshots per run (buses) take one per cycle.
                if hasattr(source, "poll"):
                    try:
                        source.poll()
                    except Exception:  # noqa: BLE001 - one source must not sink the run
                        log.exception("Polling source %s failed", source.name)
            report.polls_completed += 1
            if batches is None and poll_index < n_polls - 1:
                self.sleep(max(0.0, interval_s - (self.clock() - cycle_start)))

        stop_delays = _finalize_stop_delays(stop_frames, run_id)
        stop_delays, report.bucket_duplicates_dropped["stop_delays"] = q.dedupe_latest(
            stop_delays,
            keys=["bucket_utc", "feed_id", "start_date", "trip_id", "stop_id", "scheduled_utc"],
            order=["observed_utc"],
        )
        lines = _finalize_lines(line_frames, run_id)
        lines, report.bucket_duplicates_dropped["line_minute"] = q.dedupe_latest(
            lines,
            keys=["bucket_utc", "feed_id", "route_id", "direction"],
            order=["_is_ok", "fetched_utc"],
        )
        lines = lines.drop(columns=["_is_ok"], errors="ignore")
        quality_feed = _quality_frame(report.records)
        tables: dict[str, pd.DataFrame] = {
            "stop_delays": stop_delays,
            "line_minute": lines,
            "quality_feed": quality_feed,
            "station_minute": station_minute(stop_delays, run_id),
        }

        references_changed = False
        for source in self.sources:
            try:
                out = source.run(run_id, state, self.data_dir)
            except Exception as exc:  # noqa: BLE001 - one source must not sink the run
                log.exception("Source %s failed", source.name)
                rec = q.SourceRecord(run_id, source.name, "", self.clock())
                rec.status, rec.detail = q.STATUS_INVALID, f"{type(exc).__name__}: {exc}"
                report.sources.append(rec)
                continue
            report.sources.extend(out.records)
            for table, frame in out.tables.items():
                tables[table] = pd.concat([tables[table], frame]) if table in tables else frame
            for name, (frame, key) in out.references.items():
                merge_reference(self.data_dir, name, frame, key)
                references_changed = True
            references_changed |= out.references_changed
        if self.sources:
            tables["quality_source"] = _source_quality_frame(report.sources)

        neighbors_path = self.data_dir / "reference" / "station_neighbors.parquet"
        if references_changed or (self.sources and not neighbors_path.exists()):
            refresh_neighbors(
                self.data_dir, self.schedule.stops, self.settings.neighbor_radius_m, self.schedule
            )

        for table, frame in tables.items():
            files = write_table(self.data_dir, table, frame, run_id)
            report.rows_written[table] = len(frame)
            report.files_written += [str(f.relative_to(self.data_dir)) for f in files]

        report.finished_ts = self.clock()
        report_path = self.data_dir / "reports" / date_partition(started) / f"{run_id}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report.to_dict(), indent=2, default=_json_default))

        state.prune(report.finished_ts)
        state.save(self.state_path)
        return RunResult(report, report_path, stop_delays, lines, quality_feed, tables)

    # ------------------------------------------------------------------------------

    def _process(
        self,
        res: FetchResult,
        spec: FeedSpec,
        poll_index: int,
        run_id: str,
        state: PipelineState,
    ) -> _SnapshotOutput:
        s = self.settings
        rec = q.FeedPollRecord(
            run_id=run_id,
            poll_index=poll_index,
            feed_id=spec.feed_id,
            url=spec.url,
            fetched_ts=res.fetched_at,
            http_status=res.http_status,
            fetch_error=res.error,
            fetch_elapsed_s=round(res.elapsed_s, 3) if res.elapsed_s is not None else None,
            n_bytes=len(res.content or b""),
        )
        if not res.ok:
            rec.status, rec.detail = q.STATUS_OUTAGE, res.error
            return _SnapshotOutput(rec, lines=_missing_rows(spec, rec))
        try:
            parsed = parse_feed(res.content, spec.feed_id)
        except FeedParseError as exc:
            rec.status, rec.detail = q.STATUS_INVALID, str(exc)
            return _SnapshotOutput(rec, lines=_missing_rows(spec, rec))

        rec.header_ts = parsed.header_ts
        rec.feed_age_s = round(res.fetched_at - parsed.header_ts, 1) if parsed.header_ts else None
        rec.n_entities = parsed.n_entities
        rec.n_trip_updates = parsed.n_trip_updates
        rec.n_vehicles = parsed.n_vehicles
        rec.n_alerts = parsed.n_alerts

        status, detail = q.classify_snapshot(
            parsed.header_ts,
            res.fetched_at,
            state.last_header_ts.get(spec.feed_id),
            parsed.n_trip_updates,
            s,
        )
        rec.status, rec.detail = status, detail
        if status in q.MISSING_STATUSES:
            return _SnapshotOutput(rec, lines=_missing_rows(spec, rec))
        if status == q.STATUS_DUPLICATE:
            return _SnapshotOutput(rec)

        state.last_header_ts[spec.feed_id] = parsed.header_ts
        if self.save_raw:
            save_raw(self.data_dir / "raw", res, parsed.header_ts)

        su = parsed.stop_updates
        su, rec.dup_trip_entities = q.dedupe_trip_entities(su)
        su, rec.dup_stop_updates = q.dedupe_stop_updates(su)
        su, dropped = q.filter_time_window(su, s)
        rec.dropped_no_time = dropped["no_time"]
        rec.dropped_out_of_window = dropped["out_of_window"]
        su, rec.dropped_non_monotonic = q.filter_non_monotonic(su, s)

        sd = compute_stop_delays(su, self.schedule, s.stale_prediction_tolerance_s)
        trips = sd.drop_duplicates(["trip_id", "start_date"])
        rec.n_trips = len(trips)
        rec.n_trips_unassigned = int(trips["is_assigned"].eq(False).fillna(False).sum())
        rec.n_trips_matched = int(trips["match_method"].isin(MATCHED_METHODS).sum())
        rec.n_trips_unmatched = rec.n_trips - rec.n_trips_matched
        rec.match_methods = dict(Counter(trips["match_method"]))
        rec.routes_seen = sorted(trips["route_id"].dropna().unique().tolist())
        vehicle_age = trips["header_ts"] - trips["vehicle_ts"]
        rec.n_stale_vehicles = int((vehicle_age > s.vehicle_stale_after_s).fillna(False).sum())
        rec.n_stop_updates = len(sd)
        rec.n_with_schedule = int(sd["scheduled_ts"].notna().sum()) if len(sd) else 0

        sd, rec.dropped_stale_prediction = q.filter_stale_predictions(sd)
        sd, rec.dropped_implausible_delay = q.filter_implausible_delays(sd, s)
        td = trip_current_delays(sd)
        sd, td, jumps = q.filter_delay_jumps(sd, td, state, s)
        rec.dropped_delay_jump_trips = len(jumps)
        rec.n_rows = len(sd)

        lines = line_snapshot(sd, td, s.delayed_threshold_min)
        if not lines.empty:
            lines = lines.assign(
                status=q.STATUS_OK,
                header_ts=parsed.header_ts,
                fetched_ts=res.fetched_at,
                feed_age_s=rec.feed_age_s,
            )
        return _SnapshotOutput(rec, stop_delays=sd, lines=lines, jumps=jumps)


def _missing_rows(spec: FeedSpec, rec: q.FeedPollRecord) -> pd.DataFrame:
    """Explicit "missing" markers for every line of a feed that could not be used."""
    rows = [
        {
            "feed_id": spec.feed_id,
            "route_id": route,
            "direction": direction,
            "status": rec.status,
            "header_ts": rec.header_ts,
            "fetched_ts": rec.fetched_ts,
            "feed_age_s": rec.feed_age_s,
        }
        for route in spec.routes
        for direction in DIRECTIONS
    ]
    return pd.DataFrame(rows)


def _finalize_stop_delays(frames: list[pd.DataFrame], run_id: str) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=["bucket_utc", "observed_utc"])
    df = pd.concat(frames, ignore_index=True)
    out = df.assign(
        bucket_utc=bucket_of(df["header_ts"]),
        observed_utc=epoch_to_utc(df["header_ts"]),
        run_id=run_id,
        arrival_utc=epoch_to_utc(df["arrival_ts"]),
        departure_utc=epoch_to_utc(df["departure_ts"]),
        scheduled_arrival_utc=epoch_to_utc(df["scheduled_arrival_ts"]),
        scheduled_departure_utc=epoch_to_utc(df["scheduled_departure_ts"]),
        event_utc=epoch_to_utc(df["event_ts"]),
        scheduled_utc=epoch_to_utc(df["scheduled_ts"]),
        vehicle_age_s=(df["header_ts"] - df["vehicle_ts"]).astype("Int64"),
        stop_index=df["stop_index"].astype("Int32"),
    )
    return out


def _finalize_lines(frames: list[pd.DataFrame], run_id: str) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=["bucket_utc", "fetched_utc", "_is_ok"])
    df = pd.concat(frames, ignore_index=True)
    # Valid observations are bucketed by feed time, missing markers by fetch time.
    bucket_src = df["header_ts"].where(df["status"] == q.STATUS_OK, df["fetched_ts"])
    out = df.assign(
        bucket_utc=bucket_of(bucket_src),
        observed_utc=epoch_to_utc(df["header_ts"]),
        fetched_utc=epoch_to_utc(df["fetched_ts"]),
        run_id=run_id,
        _is_ok=(df["status"] == q.STATUS_OK),
    )
    return out


def _quality_frame(records: list[q.FeedPollRecord]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=["bucket_utc"])
    df = pd.DataFrame([r.to_dict() for r in records])
    df["fetched_utc"] = epoch_to_utc(df["fetched_ts"])
    df["bucket_utc"] = bucket_of(df["fetched_ts"])
    df["header_utc"] = epoch_to_utc(df["header_ts"])
    df["match_methods"] = df["match_methods"].map(lambda d: json.dumps(d, sort_keys=True))
    df["routes_seen"] = df["routes_seen"].map(lambda r: ",".join(r))
    for col in ("http_status", "poll_index"):
        df[col] = df[col].astype("Int32")
    return df


def _json_default(obj):
    if hasattr(obj, "item"):
        return obj.item()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def station_minute(stop_delays: pd.DataFrame, run_id: str) -> pd.DataFrame:
    """Observed train delays (``time_kind = actual``) per route x station x minute.

    This small aggregate is kept long-term, so affected stations of an event can
    be recomputed from history even though per-stop detail is short-lived.
    """
    if stop_delays.empty or "time_kind" not in stop_delays.columns:
        return pd.DataFrame(columns=["bucket_utc"])
    d = stop_delays[
        stop_delays["time_kind"].eq("actual")
        & stop_delays["delay_min"].notna()
        & stop_delays["station_id"].notna()
    ]
    if d.empty:
        return pd.DataFrame(columns=["bucket_utc"])
    g = d.assign(delay=d["delay_min"].astype(float)).groupby(
        ["bucket_utc", "feed_id", "route_id", "direction", "station_id"], dropna=False
    )
    out = g.agg(
        observed_utc=("observed_utc", "max"),
        stop_name=("stop_name", "first"),
        n_trains=("trip_id", "nunique"),
        delay_mean_min=("delay", "mean"),
        delay_max_min=("delay", "max"),
    ).reset_index()
    out["delay_mean_min"] = out["delay_mean_min"].round(2)
    out["delay_max_min"] = out["delay_max_min"].round(2)
    out["run_id"] = run_id
    return out


def station_routes(schedule: StaticSchedule) -> dict[str, list[str]]:
    """Routes scheduled to stop at each parent station (from the static GTFS)."""
    import duckdb

    trips = schedule.trips[["trip_id", "route_id"]]
    if schedule._stop_times is not None:
        stop_routes = schedule._stop_times[["trip_id", "stop_id"]].merge(trips, on="trip_id")
        pairs = stop_routes[["stop_id", "route_id"]].drop_duplicates()
    else:
        con = duckdb.connect()
        try:
            con.register("trips", trips)
            pairs = con.execute(
                "SELECT DISTINCT st.stop_id, t.route_id FROM read_parquet(?) st "
                "JOIN trips t USING (trip_id)",
                [str(schedule._stop_times_path)],
            ).df()
        finally:
            con.close()
    pairs["station_id"] = pairs["stop_id"].map(schedule.parent_station).fillna(pairs["stop_id"])
    grouped = pairs.groupby("station_id")["route_id"].agg(lambda r: sorted(set(r)))
    return grouped.to_dict()


def refresh_neighbors(
    data_dir: Path, stops: pd.DataFrame, radius_m: float, schedule: StaticSchedule | None = None
) -> pd.DataFrame:
    """Recompute the subway station -> traffic link / bike station spatial join."""
    stations = subway_stations(stops)
    if schedule is not None:
        routes = station_routes(schedule)
        stations["routes"] = stations["station_id"].map(lambda s: routes.get(s, []))
    write_reference(data_dir, "subway_stations", stations)
    neighbors = station_neighbors(
        stations,
        read_reference(data_dir, "traffic_links"),
        read_reference(data_dir, "bike_stations"),
        radius_m,
        read_reference(data_dir, "bus_stops"),
    )
    write_reference(data_dir, "station_neighbors", neighbors)
    return neighbors


def _source_quality_frame(records: list[q.SourceRecord]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=["bucket_utc"])
    df = pd.DataFrame([r.to_dict() for r in records])
    df["fetched_utc"] = epoch_to_utc(df["fetched_ts"])
    df["bucket_utc"] = bucket_of(df["fetched_ts"])
    df["observed_utc"] = epoch_to_utc(df["observed_ts"])
    df["notes"] = df["notes"].map(lambda d: json.dumps(d, sort_keys=True, default=str))
    df["http_status"] = df["http_status"].astype("Int32")
    return df
