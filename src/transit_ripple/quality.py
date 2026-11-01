"""Data quality checks: deduplication, stale/outage detection, time-jump filters.

Every check returns the cleaned data plus a count of what it removed, so the
pipeline can record all of it in the per-run quality report.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import Counter
from dataclasses import asdict, dataclass, field

import pandas as pd

from .config import Settings
from .state import PipelineState

# Snapshot statuses ----------------------------------------------------------------------
STATUS_OK = "ok"
STATUS_DUPLICATE = "duplicate"  # same header timestamp as the last accepted snapshot
STATUS_STALE = "stale"  # header timestamp too old relative to fetch time
STATUS_OUTAGE = "outage"  # fetch failed (network error / HTTP error)
STATUS_INVALID = "invalid"  # undecodable payload or missing header timestamp
STATUS_EMPTY = "empty"  # decodable but contains no trip updates
STATUS_FUTURE = "time_jump_future"  # header timestamp ahead of the wall clock
STATUS_BACKWARD = "time_jump_backward"  # header older than an already accepted snapshot
# A source that cannot be used at all, e.g. its API key is missing or rejected (M5).
STATUS_UNAVAILABLE = "unavailable"

# Statuses for which the feed's lines are recorded as missing (never as normal).
MISSING_STATUSES = frozenset(
    {STATUS_STALE, STATUS_OUTAGE, STATUS_INVALID, STATUS_EMPTY, STATUS_FUTURE, STATUS_BACKWARD}
)

TRIP_KEY = ["trip_id", "start_date"]


def classify_snapshot(
    header_ts: int | None,
    fetched_at: float,
    last_header_ts: int | None,
    n_trip_updates: int,
    settings: Settings,
) -> tuple[str, str | None]:
    """Decide whether a decoded snapshot can be used. Returns (status, detail)."""
    if not header_ts:
        return STATUS_INVALID, "header timestamp missing"
    age = fetched_at - header_ts
    if age < -settings.future_tolerance_s:
        return STATUS_FUTURE, f"header is {-age:.0f}s ahead of fetch time"
    if last_header_ts is not None and header_ts < last_header_ts:
        return STATUS_BACKWARD, f"header went back {last_header_ts - header_ts}s"
    if age > settings.stale_after_s:
        return STATUS_STALE, f"header age {age:.0f}s > {settings.stale_after_s}s"
    if last_header_ts is not None and header_ts == last_header_ts:
        return STATUS_DUPLICATE, "header timestamp unchanged since last accepted snapshot"
    if n_trip_updates == 0:
        return STATUS_EMPTY, "no trip updates in feed"
    return STATUS_OK, None


# Deduplication ---------------------------------------------------------------------------


def dedupe_trip_entities(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Keep one entity per (trip_id, start_date).

    Preference: newest vehicle timestamp, then most stop updates, then feed order.
    Returns the frame and the number of dropped entities.
    """
    if df.empty:
        return df, 0
    ent = (
        df.groupby(TRIP_KEY + ["entity_id"], dropna=False, sort=False)
        .agg(vehicle_ts=("vehicle_ts", "max"), n=("stop_id", "size"))
        .reset_index()
    )
    ent["_order"] = range(len(ent))
    ent["_vts"] = ent["vehicle_ts"].astype("Float64").fillna(-1)
    best = ent.sort_values(["_vts", "n", "_order"], ascending=[False, False, True]).drop_duplicates(
        TRIP_KEY
    )
    dropped = len(ent) - len(best)
    if dropped == 0:
        return df, 0
    keep = df.merge(best[TRIP_KEY + ["entity_id"]], on=TRIP_KEY + ["entity_id"], how="inner")
    return keep, dropped


def dedupe_stop_updates(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop repeated identical stop updates within a trip."""
    if df.empty:
        return df, 0
    out = df.drop_duplicates(TRIP_KEY + ["stop_id", "arrival_ts", "departure_ts"])
    return out, len(df) - len(out)


def dedupe_latest(df: pd.DataFrame, keys: list[str], order: list[str]) -> tuple[pd.DataFrame, int]:
    """Keep the last row per key after sorting by ``order`` (e.g. newest observation)."""
    if df.empty:
        return df, 0
    out = df.sort_values(order, kind="stable").drop_duplicates(keys, keep="last")
    return out.sort_index(), len(df) - len(out)


# Time sanity filters ---------------------------------------------------------------------


def _event_time(df: pd.DataFrame) -> pd.Series:
    return df["arrival_ts"].fillna(df["departure_ts"])


def filter_time_window(df: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, dict]:
    """Drop stop updates with no time or a time far from the feed header time."""
    if df.empty:
        return df, {"no_time": 0, "out_of_window": 0}
    t = _event_time(df)
    no_time = t.isna()
    lo = df["header_ts"] - settings.max_lag_s
    hi = df["header_ts"] + settings.max_lead_s
    out_of_window = ~no_time & ((t < lo) | (t > hi)).fillna(False)
    keep = ~(no_time | out_of_window)
    return df[keep], {"no_time": int(no_time.sum()), "out_of_window": int(out_of_window.sum())}


def _monotonic_keep(times: list[float], tol: float) -> list[bool]:
    """Keep the largest time-ordered subset of a trip's stops, plus jitter within ``tol``.

    The longest non-decreasing subsequence is the smallest set of removals that
    makes the trip monotonic, so a single bad stop (too early *or* too late) costs
    one row instead of invalidating every later stop. Stops outside it are kept
    when they fit between their kept neighbours within the tolerance.
    """
    n = len(times)
    tails: list[float] = []
    tails_idx: list[int] = []
    prev = [-1] * n
    for i, t in enumerate(times):
        j = bisect.bisect_right(tails, t)
        if j == len(tails):
            tails.append(t)
            tails_idx.append(i)
        else:
            tails[j] = t
            tails_idx[j] = i
        prev[i] = tails_idx[j - 1] if j > 0 else -1
    in_seq = [False] * n
    k = tails_idx[-1] if tails_idx else -1
    while k != -1:
        in_seq[k] = True
        k = prev[k]

    before_max = [float("-inf")] * n
    running = float("-inf")
    for i in range(n):
        before_max[i] = running
        if in_seq[i]:
            running = max(running, times[i])
    after_min = [float("inf")] * n
    running = float("inf")
    for i in range(n - 1, -1, -1):
        after_min[i] = running
        if in_seq[i]:
            running = min(running, times[i])
    return [
        in_seq[i] or (times[i] >= before_max[i] - tol and times[i] <= after_min[i] + tol)
        for i in range(n)
    ]


def filter_non_monotonic(df: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, int]:
    """Drop stop updates that break the time order of a trip.

    Drops stops whose departure precedes their arrival (beyond the tolerance), and
    the minimum number of stops needed so the remaining times never go backwards
    by more than the tolerance along the trip.
    """
    if df.empty:
        return df, 0
    tol = settings.non_monotonic_tolerance_s
    d = df.sort_values(TRIP_KEY + ["stop_index"], kind="stable")
    dep_before_arr = (d["departure_ts"] < d["arrival_ts"] - tol).fillna(False)
    candidates = d[~dep_before_arr]
    t = _event_time(candidates).astype("Float64")

    keep = pd.Series(True, index=candidates.index)
    keys = [candidates[k].fillna("") for k in TRIP_KEY]
    for _, idx in t.groupby(keys).groups.items():
        times = t.loc[idx]
        valid = times.notna()
        if valid.sum() < 2:
            continue
        sub = times[valid]
        mask = _monotonic_keep([float(x) for x in sub], tol)
        keep.loc[sub.index] = mask

    kept = candidates[keep]
    dropped = len(d) - len(kept)
    return kept.sort_index(), int(dropped)


def filter_stale_predictions(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop downstream predictions that are already in the past (see ``time_kind``)."""
    if df.empty:
        return df, 0
    stale = df["time_kind"].eq("stale")
    return df[~stale], int(stale.sum())


def filter_implausible_delays(df: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, int]:
    """Drop rows whose delay is outside the plausible range (likely a bad schedule match)."""
    if df.empty:
        return df, 0
    d = df["delay_min"].astype("Float64")
    bad = ((d < settings.min_delay_min) | (d > settings.max_delay_min)).fillna(False)
    return df[~bad], int(bad.sum())


def is_false(value) -> bool:
    """True only for an explicit False (None/NA mean "unknown")."""
    return value is not None and not pd.isna(value) and not bool(value)


@dataclass
class DelayJump:
    feed_id: str
    trip_id: str
    start_date: str | None
    previous_delay_min: float
    delay_min: float
    elapsed_min: float


def filter_delay_jumps(
    stop_delays: pd.DataFrame,
    trip_delays: pd.DataFrame,
    state: PipelineState,
    settings: Settings,
) -> tuple[pd.DataFrame, pd.DataFrame, list[DelayJump]]:
    """Drop trips whose delay changed faster than physically plausible.

    Between two observations a train's delay can change by roughly the elapsed
    time at most; anything beyond ``elapsed + tolerance`` is treated as an
    abnormal time jump. The state is always updated with the newest value so a
    genuine step change is only filtered once.
    """
    jumps: list[DelayJump] = []
    if trip_delays.empty:
        return stop_delays, trip_delays, jumps
    jumped_keys = set()
    for row in trip_delays.itertuples(index=False):
        if is_false(row.is_assigned) or pd.isna(row.delay_min) or pd.isna(row.header_ts):
            continue
        key = state.trip_key(row.feed_id, row.start_date, row.trip_id)
        prev = state.trip_delays.get(key)
        ts = int(row.header_ts)
        delay = float(row.delay_min)
        if prev is not None and ts > prev[0]:
            elapsed = (ts - prev[0]) / 60.0
            if abs(delay - prev[1]) > elapsed + settings.delay_jump_tolerance_min:
                jumps.append(
                    DelayJump(row.feed_id, row.trip_id, row.start_date, prev[1], delay, elapsed)
                )
                jumped_keys.add((row.feed_id, row.trip_id, row.start_date))
        if prev is None or ts > prev[0]:
            state.trip_delays[key] = [ts, delay]
    if not jumped_keys:
        return stop_delays, trip_delays, jumps

    def keep_mask(df: pd.DataFrame) -> pd.Series:
        keys = list(zip(df["feed_id"], df["trip_id"], df["start_date"], strict=True))
        return pd.Series([k not in jumped_keys for k in keys], index=df.index)

    return stop_delays[keep_mask(stop_delays)], trip_delays[keep_mask(trip_delays)], jumps


# Reporting -------------------------------------------------------------------------------


@dataclass
class FeedPollRecord:
    """Quality counters for one feed in one poll."""

    run_id: str
    poll_index: int
    feed_id: str
    url: str
    fetched_ts: float
    status: str = STATUS_OK
    detail: str | None = None
    http_status: int | None = None
    fetch_error: str | None = None
    fetch_elapsed_s: float | None = None
    n_bytes: int = 0
    header_ts: int | None = None
    feed_age_s: float | None = None
    n_entities: int = 0
    n_trip_updates: int = 0
    n_vehicles: int = 0
    n_alerts: int = 0
    n_trips: int = 0
    n_trips_unassigned: int = 0
    n_trips_matched: int = 0
    n_trips_unmatched: int = 0
    n_stale_vehicles: int = 0
    n_stop_updates: int = 0
    n_with_schedule: int = 0
    dup_trip_entities: int = 0
    dup_stop_updates: int = 0
    dropped_no_time: int = 0
    dropped_out_of_window: int = 0
    dropped_non_monotonic: int = 0
    dropped_stale_prediction: int = 0
    dropped_implausible_delay: int = 0
    dropped_delay_jump_trips: int = 0
    n_rows: int = 0
    match_methods: dict = field(default_factory=dict)
    routes_seen: list = field(default_factory=list)

    @property
    def is_missing(self) -> bool:
        return self.status in MISSING_STATUSES

    @property
    def match_rate(self) -> float | None:
        return self.n_trips_matched / self.n_trips if self.n_trips else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["match_rate"] = round(self.match_rate, 4) if self.match_rate is not None else None
        return d


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Statuses of non-subway sources reuse the snapshot vocabulary above.
SOURCE_MISSING_STATUSES = frozenset(
    {STATUS_STALE, STATUS_OUTAGE, STATUS_INVALID, STATUS_EMPTY, STATUS_UNAVAILABLE}
)


@dataclass
class SourceRecord:
    """Quality counters for one fetch of a non-subway source (alerts, traffic, bikes).

    ``coverage`` is the share of known entities (links, stations) with fresh data;
    ``gap_min`` is set when the previous successful fetch is older than the gap
    threshold, i.e. the time series has a hole.
    """

    run_id: str
    source: str
    url: str
    fetched_ts: float
    status: str = STATUS_OK
    detail: str | None = None
    http_status: int | None = None
    fetch_error: str | None = None
    fetch_elapsed_s: float | None = None
    n_bytes: int = 0
    observed_ts: float | None = None
    feed_age_s: float | None = None
    n_records: int = 0
    n_valid: int = 0
    n_new: int = 0
    n_stale: int = 0
    n_invalid: int = 0
    coverage: float | None = None
    gap_min: float | None = None
    notes: dict = field(default_factory=dict)

    @property
    def is_missing(self) -> bool:
        return self.status in SOURCE_MISSING_STATUSES

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.coverage is not None:
            d["coverage"] = round(self.coverage, 4)
        return d


def gap_minutes(last_ok_ts: float | None, now_ts: float, threshold_min: float) -> float | None:
    """Minutes since the previous successful fetch, if that exceeds the threshold."""
    if last_ok_ts is None:
        return None
    gap = (now_ts - last_ok_ts) / 60.0
    return round(gap, 1) if gap > threshold_min else None


@dataclass
class RunReport:
    run_id: str
    started_ts: float
    polls_requested: int
    thresholds: dict
    static: dict
    finished_ts: float | None = None
    polls_completed: int = 0
    records: list[FeedPollRecord] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    delay_jumps: list[DelayJump] = field(default_factory=list)
    bucket_duplicates_dropped: dict = field(default_factory=dict)
    rows_written: dict = field(default_factory=dict)
    files_written: list = field(default_factory=list)

    MIN_MATCH_RATE = 0.5

    def issues(self) -> list[str]:
        out = []
        if self.static.get("warning"):
            out.append(f"static GTFS: {self.static['warning']}")
        for r in self.records:
            if r.is_missing:
                why = r.detail or r.fetch_error or ""
                out.append(f"{r.feed_id} poll {r.poll_index}: {r.status} ({why}); marked missing")
            elif (
                r.status == STATUS_OK
                and r.match_rate is not None
                and r.match_rate < self.MIN_MATCH_RATE
            ):
                out.append(
                    f"{r.feed_id} poll {r.poll_index}: low schedule match rate "
                    f"{r.match_rate:.0%} ({r.n_trips_matched}/{r.n_trips} trips)"
                )
        for j in self.delay_jumps[:10]:
            out.append(
                f"{j.feed_id} trip {j.trip_id}: delay jumped {j.previous_delay_min:+.1f} -> "
                f"{j.delay_min:+.1f} min in {j.elapsed_min:.1f} min; filtered"
            )
        if len(self.delay_jumps) > 10:
            out.append(f"... and {len(self.delay_jumps) - 10} more delay jumps")
        for r in self.sources:
            if r.is_missing:
                why = r.detail or r.fetch_error or ""
                out.append(f"{r.source}: {r.status} ({why}); marked missing")
            if r.gap_min is not None:
                out.append(f"{r.source}: gap of {r.gap_min:.0f} min since the previous good fetch")
            if r.coverage is not None and r.coverage < self.MIN_COVERAGE and not r.is_missing:
                out.append(f"{r.source}: low coverage {r.coverage:.0%} of known entities fresh")
        return out

    MIN_COVERAGE = 0.5

    def overall_status(self) -> str:
        units = [*self.records, *self.sources]
        if not units:
            return "failed"
        missing = sum(u.is_missing for u in units)
        if missing == len(units):
            return "failed"
        gaps = any(r.gap_min is not None for r in self.sources)
        if missing or gaps or self.static.get("warning"):
            return "degraded"
        return "ok"

    def totals(self) -> dict:
        keys = (
            "n_trips",
            "n_trips_matched",
            "n_stop_updates",
            "dup_trip_entities",
            "dup_stop_updates",
            "dropped_no_time",
            "dropped_out_of_window",
            "dropped_non_monotonic",
            "dropped_stale_prediction",
            "dropped_implausible_delay",
            "dropped_delay_jump_trips",
            "n_rows",
        )
        t = {k: sum(getattr(r, k) for r in self.records) for k in keys}
        t["status_counts"] = dict(Counter(r.status for r in self.records))
        return t

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": _iso(self.started_ts),
            "finished_at": _iso(self.finished_ts),
            "overall_status": self.overall_status(),
            "polls_requested": self.polls_requested,
            "polls_completed": self.polls_completed,
            "thresholds": self.thresholds,
            "static": self.static,
            "totals": self.totals(),
            "bucket_duplicates_dropped": self.bucket_duplicates_dropped,
            "rows_written": self.rows_written,
            "files_written": self.files_written,
            "issues": self.issues(),
            "delay_jumps": [asdict(j) for j in self.delay_jumps],
            "feeds": [r.to_dict() for r in self.records],
            "sources": [r.to_dict() for r in self.sources],
        }

    def to_markdown(self) -> str:
        lines = [
            f"## Ingestion quality report `{self.run_id}`",
            "",
            f"- Overall status: **{self.overall_status()}**",
            f"- Polls: {self.polls_completed}/{self.polls_requested}",
            f"- Static GTFS: `{self.static.get('feed_version')}` "
            f"(age {self.static.get('age_hours')} h)",
            f"- Rows written: {self.rows_written}",
            "",
            "| Feed | Statuses | Last age (s) | Trips | Matched | Unassigned | Rows | "
            "Dedup (trip/stop) | Dropped (window/order/stale/delay/jump) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        by_feed: dict[str, list[FeedPollRecord]] = {}
        for r in self.records:
            by_feed.setdefault(r.feed_id, []).append(r)
        for feed_id, recs in by_feed.items():
            statuses = ", ".join(f"{k}x{v}" for k, v in Counter(r.status for r in recs).items())
            last = recs[-1]
            age = f"{last.feed_age_s:.0f}" if last.feed_age_s is not None else "-"
            trips = sum(r.n_trips for r in recs)
            matched = sum(r.n_trips_matched for r in recs)
            rate = f"{matched / trips:.0%}" if trips else "-"

            def total(*attrs, recs=recs):
                return sum(getattr(r, a) for r in recs for a in attrs)

            lines.append(
                f"| {feed_id} | {statuses} | {age} | {trips} | {rate} | "
                f"{total('n_trips_unassigned')} | {total('n_rows')} | "
                f"{total('dup_trip_entities')}/{total('dup_stop_updates')} | "
                f"{total('dropped_out_of_window', 'dropped_no_time')}/"
                f"{total('dropped_non_monotonic')}/{total('dropped_stale_prediction')}/"
                f"{total('dropped_implausible_delay')}/"
                f"{total('dropped_delay_jump_trips')} |"
            )
        if self.sources:
            lines += [
                "",
                "| Source | Status | Age (s) | Records | Valid | New | Stale | Coverage "
                "| Gap (min) |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for r in self.sources:
                age = f"{r.feed_age_s:.0f}" if r.feed_age_s is not None else "-"
                cov = f"{r.coverage:.0%}" if r.coverage is not None else "-"
                gap = f"{r.gap_min:.0f}" if r.gap_min is not None else "-"
                lines.append(
                    f"| {r.source} | {r.status} | {age} | {r.n_records} | {r.n_valid} | "
                    f"{r.n_new} | {r.n_stale} | {cov} | {gap} |"
                )
        issues = self.issues()
        lines += ["", "### Issues", ""]
        lines += [f"- {i}" for i in issues] if issues else ["- none"]
        return "\n".join(lines) + "\n"
