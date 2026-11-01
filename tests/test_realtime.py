import pytest

from builders import RtTrip, at, build_feed, line_1_trip
from transit_ripple.realtime import FeedParseError, parse_feed, parse_trip_id


@pytest.mark.parametrize(
    ("trip_id", "origin", "route", "direction", "path"),
    [
        ("118700_1..N03R", "118700", "1", "N", "03R"),
        ("036000_GS.N01R", "036000", "GS", "N", "01R"),
        ("119300_1..N", "119300", "1", "N", ""),
        ("082800_J..S12X026", "082800", "J", "S", "12X026"),
    ],
)
def test_parse_trip_id(trip_id, origin, route, direction, path):
    parts = parse_trip_id(trip_id)
    assert parts is not None
    assert (parts.origin_code, parts.route, parts.direction, parts.path) == (
        origin,
        route,
        direction,
        path,
    )
    assert parts.base == f"{origin}_{route}..{direction}"


def test_parse_trip_id_origin_minutes_and_invalid():
    assert parse_trip_id("118700_1..N03R").origin_minutes == pytest.approx(1187.0)
    assert parse_trip_id("not-a-trip") is None
    assert parse_trip_id("") is None


def test_parse_feed_flattens_stop_updates_with_nyct_extension():
    trip = line_1_trip(delay_s=120, vehicle_ts=at("10:01:00"), tracks=("1", "2"))
    payload = build_feed(at("10:01:30"), [trip], n_alerts=1)

    parsed = parse_feed(payload, "numbered")

    assert parsed.header_ts == at("10:01:30")
    assert parsed.gtfs_realtime_version == "1.0"
    assert (parsed.n_trip_updates, parsed.n_vehicles, parsed.n_alerts) == (1, 1, 1)
    df = parsed.stop_updates
    assert list(df["stop_id"]) == ["103N", "104N", "106N"]
    assert list(df["stop_index"]) == [0, 1, 2]
    row = df.iloc[0]
    assert row["trip_id"] == "060000_1..N03R"
    assert row["start_date"] == "20261102"
    assert row["route_id"] == "1"
    assert row["direction"] == "N"
    assert row["train_id"] == "01 1000 SFT/242"
    assert bool(row["is_assigned"]) is True
    assert row["arrival_ts"] == at("10:02:00")
    assert row["scheduled_track"] == "1" and row["actual_track"] == "2"
    assert row["vehicle_ts"] == at("10:01:00")
    assert row["vehicle_status"] == "STOPPED_AT"
    assert row["feed_id"] == "numbered"


def test_parse_feed_unassigned_when_flag_absent_and_unknown_without_extension():
    payload = build_feed(
        at("10:00:00"),
        [
            line_1_trip(assigned=False),
            RtTrip("060000_1..S03R", stops=[("103S", at("10:30:00"), None)], assigned=None),
        ],
    )
    df = parse_feed(payload, "numbered").stop_updates
    by_trip = df.drop_duplicates("trip_id").set_index("trip_id")["is_assigned"]
    assert by_trip["060000_1..N03R"] == False  # noqa: E712 - nullable boolean
    assert by_trip.isna()["060000_1..S03R"]


def test_parse_feed_missing_times_are_null():
    payload = build_feed(
        at("10:00:00"),
        [
            RtTrip(
                "060000_1..N03R",
                stops=[("103N", None, at("10:00:30")), ("104N", at("10:02:00"), None)],
            )
        ],
    )
    df = parse_feed(payload, "numbered").stop_updates
    assert df["arrival_ts"].isna().tolist() == [True, False]
    assert df["departure_ts"].isna().tolist() == [False, True]


def test_parse_feed_counts_trip_updates_without_stops():
    payload = build_feed(at("10:00:00"), [RtTrip("060000_1..N03R", stops=[])])
    parsed = parse_feed(payload, "numbered")
    assert parsed.n_trip_updates == 1
    assert parsed.n_trip_updates_without_stops == 1
    assert parsed.stop_updates.empty


@pytest.mark.parametrize("payload", [b"\xff\xfe not a protobuf", b""])
def test_parse_feed_rejects_invalid_payload(payload):
    with pytest.raises(FeedParseError):
        parse_feed(payload, "numbered")
