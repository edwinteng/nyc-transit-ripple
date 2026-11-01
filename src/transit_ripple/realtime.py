"""Parse NYCT GTFS-Realtime payloads into flat stop-level tables."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
from google.protobuf.message import DecodeError

from .proto import schema

# NYCT trip ids look like "118700_1..N03R": origin time in hundredths of a minute
# after midnight, route id padded with dots, direction (N/S) and an optional path id.
TRIP_ID_RE = re.compile(r"^(?P<origin>\d{6})_(?P<route>[A-Za-z0-9]+)\.+(?P<dir>[NS])(?P<path>.*)$")

_NYCT_DIRECTION = {1: "N", 2: "E", 3: "S", 4: "W"}
_VEHICLE_STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}
_TRIP_REL = {0: "SCHEDULED", 1: "ADDED", 2: "UNSCHEDULED", 3: "CANCELED", 5: "REPLACEMENT"}
_STOP_REL = {0: "SCHEDULED", 1: "SKIPPED", 2: "NO_DATA", 3: "UNSCHEDULED"}

STOP_UPDATE_COLUMNS = [
    "feed_id",
    "header_ts",
    "entity_id",
    "trip_id",
    "start_date",
    "route_id",
    "direction",
    "train_id",
    "is_assigned",
    "trip_relationship",
    "stop_index",
    "stop_id",
    "stop_relationship",
    "arrival_ts",
    "departure_ts",
    "scheduled_track",
    "actual_track",
    "vehicle_ts",
    "vehicle_status",
    "vehicle_stop_id",
]


class FeedParseError(ValueError):
    """Raised when a payload is not a valid GTFS-Realtime FeedMessage."""


@dataclass(frozen=True)
class TripIdParts:
    origin_code: str
    route: str
    direction: str
    path: str

    @property
    def origin_minutes(self) -> float:
        """Scheduled departure from origin, in minutes after service-day midnight."""
        return int(self.origin_code) / 100.0

    @property
    def base(self) -> str:
        """Trip id without the path id, e.g. ``118700_1..N``."""
        return f"{self.origin_code}_{self.route}..{self.direction}"


def parse_trip_id(trip_id: str) -> TripIdParts | None:
    m = TRIP_ID_RE.match(trip_id or "")
    if not m:
        return None
    return TripIdParts(m.group("origin"), m.group("route"), m.group("dir"), m.group("path"))


@dataclass
class ParsedFeed:
    feed_id: str
    header_ts: int | None
    gtfs_realtime_version: str
    incrementality: int
    n_entities: int = 0
    n_trip_updates: int = 0
    n_vehicles: int = 0
    n_alerts: int = 0
    n_trip_updates_without_stops: int = 0
    stop_updates: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=STOP_UPDATE_COLUMNS)
    )


def _opt_time(event) -> int | None:
    # A missing StopTimeEvent or time==0 both mean "no time".
    return int(event.time) if event.time else None


def parse_feed(content: bytes, feed_id: str) -> ParsedFeed:
    """Decode a FeedMessage and flatten trip updates into one row per stop update.

    Vehicle positions are joined onto their trips (timestamp, status, stop).
    """
    s = schema()
    try:
        msg = s.FeedMessage.FromString(content)
    except (DecodeError, RuntimeError, ValueError) as exc:
        raise FeedParseError(f"{feed_id}: cannot decode GTFS-Realtime payload: {exc}") from exc

    header = msg.header
    if not header.gtfs_realtime_version:
        raise FeedParseError(f"{feed_id}: payload has no GTFS-Realtime header")
    parsed = ParsedFeed(
        feed_id=feed_id,
        header_ts=int(header.timestamp) if header.timestamp else None,
        gtfs_realtime_version=header.gtfs_realtime_version,
        incrementality=int(header.incrementality),
        n_entities=len(msg.entity),
    )

    # Vehicles are keyed by trip and train id, so duplicated trip ids served by
    # two different trains can still be told apart.
    vehicles: dict[tuple[str, str, str | None], tuple[int | None, str | None, str | None]] = {}
    trip_updates = []
    for ent in msg.entity:
        if ent.HasField("trip_update"):
            trip_updates.append(ent)
        if ent.HasField("vehicle"):
            parsed.n_vehicles += 1
            v = ent.vehicle
            if v.HasField("trip") and v.trip.trip_id:
                info = (
                    int(v.timestamp) if v.timestamp else None,
                    _VEHICLE_STATUS.get(v.current_status) if v.HasField("current_status") else None,
                    v.stop_id or None,
                )
                vtrain = None
                if v.trip.HasExtension(s.nyct_trip):
                    vtrain = v.trip.Extensions[s.nyct_trip].train_id or None
                vehicles[(v.trip.trip_id, v.trip.start_date, vtrain)] = info
                vehicles.setdefault((v.trip.trip_id, v.trip.start_date, None), info)
        if ent.HasField("alert"):
            parsed.n_alerts += 1
    parsed.n_trip_updates = len(trip_updates)

    rows = []
    for ent in trip_updates:
        tu = ent.trip_update
        td = tu.trip
        train_id = None
        is_assigned = None
        direction = None
        if td.HasExtension(s.nyct_trip):
            ext = td.Extensions[s.nyct_trip]
            train_id = ext.train_id or None
            # The MTA omits is_assigned when it is false; absence inside the
            # extension therefore means "not assigned to a train yet".
            is_assigned = bool(ext.is_assigned)
            if ext.HasField("direction"):
                direction = _NYCT_DIRECTION.get(ext.direction)
        parts = parse_trip_id(td.trip_id)
        if parts is not None:
            direction = parts.direction
        trip_rel = _TRIP_REL.get(td.schedule_relationship, str(td.schedule_relationship))
        vehicle_ts, vehicle_status, vehicle_stop = vehicles.get(
            (td.trip_id, td.start_date, train_id),
            vehicles.get((td.trip_id, td.start_date, None), (None, None, None)),
        )
        if len(tu.stop_time_update) == 0:
            parsed.n_trip_updates_without_stops += 1
        for i, stu in enumerate(tu.stop_time_update):
            sched_track = actual_track = None
            if stu.HasExtension(s.nyct_stop):
                sext = stu.Extensions[s.nyct_stop]
                sched_track = sext.scheduled_track or None
                actual_track = sext.actual_track or None
            rows.append(
                (
                    feed_id,
                    parsed.header_ts,
                    ent.id,
                    td.trip_id,
                    td.start_date or None,
                    td.route_id or (parts.route if parts else None),
                    direction,
                    train_id,
                    is_assigned,
                    trip_rel,
                    i,
                    stu.stop_id,
                    _STOP_REL.get(stu.schedule_relationship, str(stu.schedule_relationship)),
                    _opt_time(stu.arrival) if stu.HasField("arrival") else None,
                    _opt_time(stu.departure) if stu.HasField("departure") else None,
                    sched_track,
                    actual_track,
                    vehicle_ts,
                    vehicle_status,
                    vehicle_stop,
                )
            )

    df = pd.DataFrame.from_records(rows, columns=STOP_UPDATE_COLUMNS)
    for col in ("header_ts", "arrival_ts", "departure_ts", "vehicle_ts", "stop_index"):
        df[col] = df[col].astype("Int64")
    df["is_assigned"] = df["is_assigned"].astype("boolean")
    parsed.stop_updates = df
    return parsed
