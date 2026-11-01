"""GTFS static schedule: download, Parquet cache, service calendars, trip matching."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import shutil
import tempfile
import time
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import requests

from .config import AGENCY_TIMEZONE, Settings
from .realtime import parse_trip_id

log = logging.getLogger(__name__)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TABLES = ("trips", "stop_times", "stops", "routes", "calendar", "calendar_dates")

# Match methods, in order of preference.
MATCH_EXACT = "exact"
MATCH_BASE = "base_trip"
MATCH_EXACT_FALLBACK = "exact_weekday_fallback"
MATCH_BASE_FALLBACK = "base_trip_weekday_fallback"
MATCH_NONE = "unmatched"
MATCH_NO_DATE = "no_start_date"
MATCHED_METHODS = (MATCH_EXACT, MATCH_BASE, MATCH_EXACT_FALLBACK, MATCH_BASE_FALLBACK)


def gtfs_time_to_seconds(value) -> int | None:
    """Convert GTFS ``HH:MM:SS`` (hours may exceed 23) to seconds after service-day start."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def service_day_epoch(service_date: str, tz: str = AGENCY_TIMEZONE) -> int:
    """Epoch seconds of "noon minus 12h" on a service date (YYYYMMDD).

    GTFS measures stop times from this instant, which equals local midnight
    except on daylight-saving transition days.
    """
    day = dt.datetime.strptime(service_date, "%Y%m%d")
    noon = dt.datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(tz))
    return int(noon.timestamp()) - 12 * 3600


# --------------------------------------------------------------------------------------
# Download and Parquet conversion
# --------------------------------------------------------------------------------------


@dataclass
class StaticInfo:
    source_url: str
    static_dir: str
    sha256: str | None = None
    downloaded_at: float | None = None
    feed_version: str | None = None
    feed_start_date: str | None = None
    feed_end_date: str | None = None
    refreshed: bool = False
    warning: str | None = None

    def age_hours(self, now: float | None = None) -> float | None:
        if self.downloaded_at is None:
            return None
        return ((now or time.time()) - self.downloaded_at) / 3600.0

    def to_report(self) -> dict:
        d = asdict(self)
        age = self.age_hours()
        d["age_hours"] = round(age, 2) if age is not None else None
        return d


_CONVERT_SQL = {
    "trips": """
        SELECT route_id, trip_id, service_id, trip_headsign,
               TRY_CAST(direction_id AS INTEGER) AS direction_id, shape_id
        FROM read_csv('{src}', header=true, all_varchar=true)
    """,
    "stop_times": """
        SELECT trip_id, stop_id,
               TRY_CAST(split_part(arrival_time, ':', 1) AS INTEGER) * 3600
                 + TRY_CAST(split_part(arrival_time, ':', 2) AS INTEGER) * 60
                 + TRY_CAST(split_part(arrival_time, ':', 3) AS INTEGER) AS arrival_s,
               TRY_CAST(split_part(departure_time, ':', 1) AS INTEGER) * 3600
                 + TRY_CAST(split_part(departure_time, ':', 2) AS INTEGER) * 60
                 + TRY_CAST(split_part(departure_time, ':', 3) AS INTEGER) AS departure_s,
               TRY_CAST(stop_sequence AS INTEGER) AS stop_sequence
        FROM read_csv('{src}', header=true, all_varchar=true)
        ORDER BY trip_id, stop_sequence
    """,
    "stops": """
        SELECT stop_id, stop_name, TRY_CAST(stop_lat AS DOUBLE) AS stop_lat,
               TRY_CAST(stop_lon AS DOUBLE) AS stop_lon,
               TRY_CAST(location_type AS INTEGER) AS location_type, parent_station
        FROM read_csv('{src}', header=true, all_varchar=true)
    """,
    "routes": "SELECT * FROM read_csv('{src}', header=true, all_varchar=true)",
    "calendar": "SELECT * FROM read_csv('{src}', header=true, all_varchar=true)",
    "calendar_dates": "SELECT * FROM read_csv('{src}', header=true, all_varchar=true)",
}


def _download(url: str, target: Path, settings: Settings) -> None:
    if url.startswith(("http://", "https://")):
        headers = {"User-Agent": settings.user_agent}
        with requests.get(url, headers=headers, timeout=120, stream=True) as resp:
            resp.raise_for_status()
            with open(target, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
    else:
        shutil.copyfile(Path(url.removeprefix("file://")), target)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def convert_zip_to_parquet(zip_path: Path, out_dir: Path) -> dict:
    """Extract the GTFS tables we need and write them as Parquet. Returns feed_info."""
    out_dir.mkdir(parents=True, exist_ok=True)
    feed_info: dict = {}
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        missing = [t for t in TABLES if f"{t}.txt" not in names]
        # calendar_dates is optional in GTFS; everything else is required here.
        if [t for t in missing if t != "calendar_dates"]:
            raise ValueError(f"Static GTFS is missing tables: {missing}")
        con = duckdb.connect()
        try:
            for table in TABLES:
                dest = out_dir / f"{table}.parquet"
                if table in missing:
                    empty = pd.DataFrame(
                        {
                            "service_id": pd.Series(dtype=str),
                            "date": pd.Series(dtype=str),
                            "exception_type": pd.Series(dtype=str),
                        }
                    )
                    empty.to_parquet(dest, index=False)
                    continue
                src = Path(zf.extract(f"{table}.txt", tmp))
                sql = _CONVERT_SQL[table].format(src=src.as_posix())
                con.execute(f"COPY ({sql}) TO '{dest.as_posix()}' (FORMAT parquet)")
            if "feed_info.txt" in names:
                src = Path(zf.extract("feed_info.txt", tmp))
                df = con.execute(
                    f"SELECT * FROM read_csv('{src.as_posix()}', header=true, all_varchar=true)"
                ).df()
                if len(df):
                    feed_info = {k: v for k, v in df.iloc[0].to_dict().items() if pd.notna(v)}
        finally:
            con.close()
    return feed_info


def ensure_static(settings: Settings, force: bool = False) -> StaticInfo:
    """Make sure a converted static GTFS exists and is not older than the max age.

    If a refresh fails but an older cache exists, the cache is used and a warning
    is attached (it ends up in the quality report).
    """
    static_dir = settings.resolved_static_dir
    parquet_dir = static_dir / "parquet"
    meta_path = static_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    have_cache = meta is not None and all((parquet_dir / f"{t}.parquet").exists() for t in TABLES)

    def info_from(m: dict, refreshed: bool, warning: str | None = None) -> StaticInfo:
        fi = m.get("feed_info", {})
        return StaticInfo(
            source_url=m.get("source_url", settings.static_url),
            static_dir=str(static_dir),
            sha256=m.get("sha256"),
            downloaded_at=m.get("downloaded_at"),
            feed_version=fi.get("feed_version"),
            feed_start_date=fi.get("feed_start_date"),
            feed_end_date=fi.get("feed_end_date"),
            refreshed=refreshed,
            warning=warning,
        )

    if have_cache and not force and meta.get("source_url") == settings.static_url:
        age_h = (time.time() - meta["downloaded_at"]) / 3600.0
        if age_h < settings.static_max_age_hours:
            return info_from(meta, refreshed=False)

    static_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = static_dir / "download.tmp"
    try:
        log.info("Downloading static GTFS from %s", settings.static_url)
        _download(settings.static_url, tmp_zip, settings)
        digest = _sha256(tmp_zip)
        if have_cache and meta.get("sha256") == digest:
            meta["downloaded_at"] = time.time()
            meta["source_url"] = settings.static_url
        else:
            staging = static_dir / "parquet.staging"
            shutil.rmtree(staging, ignore_errors=True)
            feed_info = convert_zip_to_parquet(tmp_zip, staging)
            shutil.rmtree(parquet_dir, ignore_errors=True)
            staging.rename(parquet_dir)
            meta = {
                "source_url": settings.static_url,
                "sha256": digest,
                "downloaded_at": time.time(),
                "feed_info": feed_info,
            }
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        return info_from(meta, refreshed=True)
    except Exception as exc:  # noqa: BLE001 - fall back to cache on any refresh error
        if have_cache:
            warning = f"static refresh failed ({type(exc).__name__}: {exc}); using cached copy"
            log.warning(warning)
            return info_from(meta, refreshed=False, warning=warning)
        raise
    finally:
        tmp_zip.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Schedule lookups
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TripMatch:
    static_trip_id: str | None
    method: str
    n_candidates: int = 0

    @property
    def matched(self) -> bool:
        return self.static_trip_id is not None


@dataclass(frozen=True)
class _Candidate:
    trip_id: str
    service_id: str
    path: str


class StaticSchedule:
    """In-memory view of the static schedule used for delay calculation."""

    def __init__(
        self,
        trips: pd.DataFrame,
        calendar: pd.DataFrame,
        calendar_dates: pd.DataFrame,
        stops: pd.DataFrame,
        stop_times: pd.DataFrame | None = None,
        stop_times_path: Path | None = None,
        tz: str = AGENCY_TIMEZONE,
    ):
        if stop_times is None and stop_times_path is None:
            raise ValueError("Provide stop_times or stop_times_path")
        self.tz = tz
        self.trips = trips
        self.calendar = calendar.astype(str)
        self.calendar_dates = calendar_dates.astype(str)
        self.stops = stops
        self._stop_times = stop_times
        self._stop_times_path = stop_times_path
        self._service_cache: dict[str, tuple[frozenset, frozenset]] = {}
        self._match_cache: dict[tuple[str, str | None], TripMatch] = {}
        self._added_services = frozenset(
            self.calendar_dates.loc[self.calendar_dates["exception_type"] == "1", "service_id"]
        )
        self.stop_names = dict(zip(stops["stop_id"], stops["stop_name"], strict=False))
        self.parent_station = {
            sid: (parent if isinstance(parent, str) and parent else sid)
            for sid, parent in zip(stops["stop_id"], stops["parent_station"], strict=False)
        }

        self._by_suffix: dict[str, list[_Candidate]] = defaultdict(list)
        self._by_base: dict[str, list[_Candidate]] = defaultdict(list)
        for trip_id, service_id in zip(trips["trip_id"], trips["service_id"], strict=False):
            suffix = trip_id.split("_", 1)[1] if "_" in trip_id else trip_id
            parts = parse_trip_id(suffix)
            cand = _Candidate(trip_id, service_id, parts.path if parts else "")
            self._by_suffix[suffix].append(cand)
            if parts is not None:
                self._by_base[parts.base].append(cand)

    # -- construction ------------------------------------------------------------------

    @classmethod
    def from_dir(cls, static_dir: Path) -> StaticSchedule:
        pq = Path(static_dir) / "parquet"
        return cls(
            trips=pd.read_parquet(pq / "trips.parquet"),
            calendar=pd.read_parquet(pq / "calendar.parquet"),
            calendar_dates=pd.read_parquet(pq / "calendar_dates.parquet"),
            stops=pd.read_parquet(pq / "stops.parquet"),
            stop_times_path=pq / "stop_times.parquet",
        )

    @classmethod
    def from_frames(
        cls,
        trips: pd.DataFrame,
        stop_times: pd.DataFrame,
        calendar: pd.DataFrame,
        calendar_dates: pd.DataFrame | None = None,
        stops: pd.DataFrame | None = None,
    ) -> StaticSchedule:
        """Build from GTFS-like frames (times may be ``HH:MM:SS`` strings)."""
        st = stop_times.copy()
        for col in ("arrival", "departure"):
            if f"{col}_s" not in st.columns:
                st[f"{col}_s"] = st[f"{col}_time"].map(gtfs_time_to_seconds)
        st = st[["trip_id", "stop_id", "arrival_s", "departure_s", "stop_sequence"]]
        if calendar_dates is None:
            calendar_dates = pd.DataFrame(columns=["service_id", "date", "exception_type"])
        if stops is None:
            ids = sorted(st["stop_id"].unique())
            stops = pd.DataFrame({"stop_id": ids, "stop_name": ids, "parent_station": None})
        return cls(trips, calendar, calendar_dates, stops, stop_times=st)

    # -- calendars ---------------------------------------------------------------------

    def _services(self, date: str) -> tuple[frozenset, frozenset]:
        if date not in self._service_cache:
            day = dt.datetime.strptime(date, "%Y%m%d").date()
            wd = WEEKDAYS[day.weekday()]
            cal = self.calendar
            runs_on_weekday = cal[cal[wd] == "1"]
            in_range = runs_on_weekday[
                (runs_on_weekday["start_date"] <= date) & (runs_on_weekday["end_date"] >= date)
            ]
            cd = self.calendar_dates[self.calendar_dates["date"] == date]
            added = set(cd.loc[cd["exception_type"] == "1", "service_id"])
            removed = set(cd.loc[cd["exception_type"] == "2", "service_id"])
            active = (set(in_range["service_id"]) | added) - removed
            pattern = set(runs_on_weekday["service_id"]) - removed - active
            self._service_cache[date] = (frozenset(active), frozenset(pattern))
        return self._service_cache[date]

    def active_services(self, date: str) -> frozenset:
        """Service ids running on a date per calendar.txt + calendar_dates.txt."""
        return self._services(date)[0]

    def weekday_pattern_services(self, date: str) -> frozenset:
        """Services for the same weekday whose date range does not cover the date.

        Used as a lower-confidence fallback around schedule changes ("picks"),
        when realtime trips still follow a schedule the static feed no longer covers.
        """
        return self._services(date)[1]

    # -- matching ----------------------------------------------------------------------

    def _pick(self, cands: list[_Candidate], rt_path: str) -> _Candidate:
        def rank(c: _Candidate):
            return (
                0 if c.path == rt_path else (1 if rt_path and c.path.startswith(rt_path) else 2),
                0 if c.service_id in self._added_services else 1,
                c.trip_id,
            )

        return min(cands, key=rank)

    def match(self, trip_id: str, start_date: str | None) -> TripMatch:
        """Find the static trip for a realtime trip id on a service date."""
        key = (trip_id, start_date)
        if key in self._match_cache:
            return self._match_cache[key]
        result = self._match_uncached(trip_id, start_date)
        self._match_cache[key] = result
        return result

    def _match_uncached(self, trip_id: str, start_date: str | None) -> TripMatch:
        if not start_date or len(start_date) != 8 or not start_date.isdigit():
            return TripMatch(None, MATCH_NO_DATE)
        parts = parse_trip_id(trip_id)
        rt_path = parts.path if parts else ""
        exact = self._by_suffix.get(trip_id, [])
        base = self._by_base.get(parts.base, []) if parts else []
        active = self.active_services(start_date)
        pattern = self.weekday_pattern_services(start_date)
        for method, cands, services in (
            (MATCH_EXACT, exact, active),
            (MATCH_BASE, base, active),
            (MATCH_EXACT_FALLBACK, exact, pattern),
            (MATCH_BASE_FALLBACK, base, pattern),
        ):
            ok = [c for c in cands if c.service_id in services]
            if ok:
                return TripMatch(self._pick(ok, rt_path).trip_id, method, len(ok))
        return TripMatch(None, MATCH_NONE)

    # -- stop times --------------------------------------------------------------------

    def scheduled_stop_times(self, static_trip_ids) -> pd.DataFrame:
        """Scheduled arrival/departure seconds for the given static trips."""
        ids = sorted(set(static_trip_ids))
        cols = ["trip_id", "stop_id", "arrival_s", "departure_s", "stop_sequence"]
        if not ids:
            return pd.DataFrame(columns=cols)
        if self._stop_times is not None:
            return self._stop_times[self._stop_times["trip_id"].isin(ids)][cols].copy()
        con = duckdb.connect()
        try:
            con.register("wanted", pd.DataFrame({"trip_id": ids}))
            return con.execute(
                "SELECT st.trip_id, st.stop_id, st.arrival_s, st.departure_s, st.stop_sequence "
                "FROM read_parquet(?) st JOIN wanted USING (trip_id)",
                [str(self._stop_times_path)],
            ).df()
        finally:
            con.close()


def load_schedule(
    settings: Settings, force_refresh: bool = False
) -> tuple[StaticSchedule, StaticInfo]:
    info = ensure_static(settings, force=force_refresh)
    return StaticSchedule.from_dir(settings.resolved_static_dir), info
