"""Builders for synthetic GTFS static schedules and GTFS-Realtime payloads."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from transit_ripple.proto import schema
from transit_ripple.static_gtfs import StaticSchedule, service_day_epoch

# Monday 2026-11-02 (EST, the day after the DST change).
SERVICE_DATE = "20261102"
DAY0 = service_day_epoch(SERVICE_DATE)  # 2026-11-02T05:00:00Z


def at(hhmmss: str, date: str = SERVICE_DATE) -> int:
    """Epoch seconds for a GTFS time on a service date."""
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return service_day_epoch(date) + h * 3600 + m * 60 + s


# ------------------------------------------------------------------------------------
# Static schedule
# ------------------------------------------------------------------------------------

WEEKDAY_1N = "AFA26GEN-1038-Weekday-00_060000_1..N03R"  # departs 10:00
WEEKDAY_1N_LATE = "AFA26GEN-1038-Weekday-00_144000_1..N03R"  # departs 24:00 (after midnight)
SATURDAY_1N = "AFA26GEN-1038-Saturday-00_060000_1..N03R"
SUPPLEMENT_1N = "L0S1-1-1095-S02_061200_1..N03R"  # planned-work trip (10:12), only on 20261102
WEEKDAY_GS = "AFA26GEN-GS01-Weekday-00_036000_GS.N01R"  # shuttle: single-dot route padding


def static_frames() -> dict[str, pd.DataFrame]:
    trips = pd.DataFrame(
        [
            ("1", WEEKDAY_1N, "Weekday", "Van Cortlandt Park", 0, "1..N03R"),
            ("1", WEEKDAY_1N_LATE, "Weekday", "Van Cortlandt Park", 0, "1..N03R"),
            ("1", SATURDAY_1N, "Saturday", "Van Cortlandt Park", 0, "1..N03R"),
            ("1", SUPPLEMENT_1N, "L0S1", "Van Cortlandt Park", 0, "1..N03R"),
            ("GS", WEEKDAY_GS, "Weekday", "Times Sq", 0, "GS.N01R"),
        ],
        columns=["route_id", "trip_id", "service_id", "trip_headsign", "direction_id", "shape_id"],
    )
    rows = []
    for trip_id, start in (
        (WEEKDAY_1N, 10 * 60),
        (WEEKDAY_1N_LATE, 24 * 60),
        (SATURDAY_1N, 10 * 60),
        (SUPPLEMENT_1N, 10 * 60 + 12),
    ):
        for seq, (stop, offset) in enumerate((("103N", 0), ("104N", 2), ("106N", 5)), start=1):
            minutes = start + offset
            t = f"{minutes // 60:02d}:{minutes % 60:02d}:00"
            rows.append((trip_id, stop, t, t, seq))
    rows += [
        (WEEKDAY_GS, "902N", "06:00:00", "06:00:30", 1),
        (WEEKDAY_GS, "901N", "06:02:00", "06:02:00", 2),
    ]
    stop_times = pd.DataFrame(
        rows, columns=["trip_id", "stop_id", "arrival_time", "departure_time", "stop_sequence"]
    )
    calendar = pd.DataFrame(
        [
            ("Weekday", "1", "1", "1", "1", "1", "0", "0", "20261101", "20270515"),
            ("Saturday", "0", "0", "0", "0", "0", "1", "0", "20261101", "20270515"),
            ("Sunday", "0", "0", "0", "0", "0", "0", "1", "20261101", "20270515"),
        ],
        columns=[
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ],
    )
    calendar_dates = pd.DataFrame(
        [
            ("L0S1", "20261102", "1"),  # supplement runs on Monday 11/02
            ("Weekday", "20261126", "2"),  # Thanksgiving: no weekday service
            ("Sunday", "20261126", "1"),  # ... Sunday schedule instead
        ],
        columns=["service_id", "date", "exception_type"],
    )
    stops = pd.DataFrame(
        [
            ("103", "103 St", None),
            ("103N", "103 St", "103"),
            ("104N", "Cathedral Pkwy (110 St)", "104"),
            ("106N", "125 St", "106"),
            ("902N", "Times Sq-42 St", "902"),
            ("901N", "Grand Central-42 St", "901"),
        ],
        columns=["stop_id", "stop_name", "parent_station"],
    )
    return {
        "trips": trips,
        "stop_times": stop_times,
        "calendar": calendar,
        "calendar_dates": calendar_dates,
        "stops": stops,
    }


def make_schedule() -> StaticSchedule:
    f = static_frames()
    return StaticSchedule.from_frames(
        f["trips"], f["stop_times"], f["calendar"], f["calendar_dates"], f["stops"]
    )


def write_static_zip(path: Path) -> Path:
    """Write the synthetic schedule as a GTFS zip (plus routes/feed_info)."""
    f = static_frames()
    f["stops"] = f["stops"].assign(stop_lat=40.8, stop_lon=-73.96, location_type="")
    f["routes"] = pd.DataFrame(
        [("1", "MTA NYCT", "1", "Broadway - 7 Avenue Local", "1", "EE352E")],
        columns=[
            "route_id",
            "agency_id",
            "route_short_name",
            "route_long_name",
            "route_type",
            "route_color",
        ],
    )
    f["feed_info"] = pd.DataFrame(
        [("MTA New York City Transit", "test-version", "20261101", "20270515")],
        columns=["feed_publisher_name", "feed_version", "feed_start_date", "feed_end_date"],
    )
    with zipfile.ZipFile(path, "w") as zf:
        for name, df in f.items():
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            zf.writestr(f"{name}.txt", buf.getvalue())
    return path


# ------------------------------------------------------------------------------------
# Realtime payloads
# ------------------------------------------------------------------------------------


@dataclass
class RtTrip:
    trip_id: str
    start_date: str = SERVICE_DATE
    route_id: str = "1"
    stops: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    assigned: bool | None = True  # None: omit the NYCT extension entirely
    train_id: str | None = "01 1000 SFT/242"
    vehicle_ts: int | None = None
    entity_id: str | None = None
    tracks: tuple[str, str] | None = None


def build_feed(header_ts: int | None, trips: list[RtTrip], n_alerts: int = 0) -> bytes:
    """Serialize a FeedMessage with NYCT extensions, like the MTA feeds."""
    s = schema()
    msg = s.FeedMessage()
    msg.header.gtfs_realtime_version = "1.0"
    if header_ts is not None:
        msg.header.timestamp = header_ts
    for i, trip in enumerate(trips):
        ent = msg.entity.add()
        ent.id = trip.entity_id or f"{i:06d}"
        td = ent.trip_update.trip
        td.trip_id = trip.trip_id
        td.start_date = trip.start_date
        td.route_id = trip.route_id
        if trip.assigned is not None:
            ext = td.Extensions[s.nyct_trip]
            if trip.train_id:
                ext.train_id = trip.train_id
            if trip.assigned:  # the MTA omits the field when false
                ext.is_assigned = True
        for stop_id, arr, dep in trip.stops:
            stu = ent.trip_update.stop_time_update.add()
            stu.stop_id = stop_id
            if arr is not None:
                stu.arrival.time = arr
            if dep is not None:
                stu.departure.time = dep
            if trip.tracks:
                stu.Extensions[s.nyct_stop].scheduled_track = trip.tracks[0]
                stu.Extensions[s.nyct_stop].actual_track = trip.tracks[1]
        if trip.vehicle_ts is not None:
            v = msg.entity.add()
            v.id = f"v{i:05d}"
            v.vehicle.trip.trip_id = trip.trip_id
            v.vehicle.trip.start_date = trip.start_date
            if trip.train_id:
                v.vehicle.trip.Extensions[s.nyct_trip].train_id = trip.train_id
            v.vehicle.timestamp = trip.vehicle_ts
            v.vehicle.current_status = 1  # STOPPED_AT
            if trip.stops:
                v.vehicle.stop_id = trip.stops[0][0]
    for j in range(n_alerts):
        a = msg.entity.add()
        a.id = f"a{j:05d}"
        a.alert.header_text.translation.add().text = "Train delayed"
    return msg.SerializeToString()


def line_1_trip(delay_s: int = 0, **kwargs) -> RtTrip:
    """The 10:00 northbound 1 train, running ``delay_s`` late at every stop."""
    stops = [
        ("103N", at("10:00:00") + delay_s, at("10:00:00") + delay_s),
        ("104N", at("10:02:00") + delay_s, at("10:02:00") + delay_s),
        ("106N", at("10:05:00") + delay_s, at("10:05:00") + delay_s),
    ]
    return RtTrip("060000_1..N03R", stops=stops, **kwargs)


# ------------------------------------------------------------------------------------
# M2 sources
# ------------------------------------------------------------------------------------


@dataclass
class RtAlert:
    alert_id: str
    alert_type: str = "Delays"
    routes: tuple[str, ...] = ("1",)
    stops: tuple[str, ...] = ()
    periods: tuple[tuple[int | None, int | None], ...] = ()
    header: str = "Northbound [1] trains are delayed"
    created: int = 0
    updated: int = 0


def build_alerts_feed(header_ts: int, alerts: list[RtAlert]) -> bytes:
    """Serialize an MTA-style alerts FeedMessage with the Mercury extension."""
    s = schema()
    msg = s.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = header_ts
    for a in alerts:
        ent = msg.entity.add()
        ent.id = a.alert_id
        al = ent.alert
        for start, end in a.periods:
            p = al.active_period.add()
            if start is not None:
                p.start = start
            if end is not None:
                p.end = end
        for r in a.routes:
            ie = al.informed_entity.add()
            ie.agency_id = "MTASBWY"
            ie.route_id = r
        for st in a.stops:
            ie = al.informed_entity.add()
            ie.agency_id = "MTASBWY"
            ie.stop_id = st
        en = al.header_text.translation.add()
        en.text, en.language = a.header, "en"
        html_t = al.header_text.translation.add()
        html_t.text, html_t.language = f"<p>{a.header}</p>", "en-html"
        ext = al.Extensions[s.mercury_alert]
        ext.alert_type = a.alert_type
        ext.created_at = a.created or header_ts
        ext.updated_at = a.updated or a.created or header_ts
    return msg.SerializeToString()


class ScriptedFetcher:
    """UrlFetcher returning scripted responses keyed by (source, url-substring)."""

    def __init__(self, responses: dict, now: float):
        self.responses = responses  # key -> bytes | None | list of those (consumed in order)
        self.now = now
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    def __call__(self, source, url, params=None, headers=None):
        from transit_ripple.feeds import FetchResult

        self.calls.append((source, url, params, headers))
        for key, value in self.responses.items():
            if key in url or key == source:
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                if value is None:
                    return FetchResult(source, url, self.now, None, error="ConnectionError")
                return FetchResult(source, url, self.now, value, http_status=200, elapsed_s=0.01)
        return FetchResult(source, url, self.now, None, http_status=404, error="HTTP 404")
