import pandas as pd
import pytest

from builders import WEEKDAY_1N, RtTrip, at, build_feed, line_1_trip, make_schedule, static_frames
from transit_ripple.delays import compute_stop_delays, line_snapshot, trip_current_delays
from transit_ripple.realtime import parse_feed
from transit_ripple.static_gtfs import StaticSchedule, service_day_epoch


def delays_for(trips, header_ts, schedule=None, stale_tolerance_s=120):
    su = parse_feed(build_feed(header_ts, trips), "numbered").stop_updates
    return compute_stop_delays(su, schedule or make_schedule(), stale_tolerance_s)


def test_late_train_delay_in_minutes_at_every_stop():
    sd = delays_for([line_1_trip(delay_s=150)], at("10:01:00"))
    assert sd["static_trip_id"].unique().tolist() == [WEEKDAY_1N]
    assert sd["match_method"].unique().tolist() == ["exact"]
    assert sd["delay_min"].tolist() == pytest.approx([2.5, 2.5, 2.5])
    assert sd["arrival_delay_min"].tolist() == pytest.approx([2.5, 2.5, 2.5])
    assert sd["scheduled_ts"].tolist() == [at("10:00:00"), at("10:02:00"), at("10:05:00")]
    assert sd["event"].unique().tolist() == ["arrival"]


def test_early_train_has_negative_delay():
    sd = delays_for([line_1_trip(delay_s=-60)], at("09:55:00"))
    assert sd["delay_min"].tolist() == pytest.approx([-1.0, -1.0, -1.0])


def test_time_kind_distinguishes_actual_from_predicted():
    # At 10:03 the train (2.5 min late) has already arrived at 103N (10:02:30).
    sd = delays_for([line_1_trip(delay_s=150)], at("10:03:00"))
    assert sd["time_kind"].tolist() == ["actual", "predicted", "predicted"]
    assert sd["lead_s"].tolist() == [-30, 90, 270]


def test_downstream_times_in_the_past_are_stale_predictions():
    # At 10:10 the feed still lists the on-time 10:00 train at 103N, with 104N
    # (10:02) and 106N (10:05) predicted in the past: frozen data, not arrivals.
    sd = delays_for([line_1_trip(delay_s=0)], at("10:10:00"))
    assert sd["time_kind"].tolist() == ["actual", "stale", "stale"]
    # Small lag (within the 120 s tolerance) is still a prediction.
    sd = delays_for([line_1_trip(delay_s=0)], at("10:03:00"))
    assert sd["time_kind"].tolist() == ["actual", "predicted", "predicted"]
    sd = delays_for([line_1_trip(delay_s=0)], at("10:10:00"), stale_tolerance_s=900)
    assert sd["time_kind"].tolist() == ["actual", "predicted", "predicted"]


def test_departure_time_used_when_arrival_missing():
    trip = RtTrip("060000_1..N03R", stops=[("103N", None, at("10:00:45"))])
    sd = delays_for([trip], at("09:59:00"))
    row = sd.iloc[0]
    assert row["event"] == "departure"
    assert row["delay_min"] == pytest.approx(0.75)
    assert pd.isna(row["arrival_delay_min"])


def test_after_midnight_trip_uses_service_date_not_calendar_date():
    # Scheduled 24:00 on 2026-11-02 = 00:00 on 2026-11-03; train is 3 minutes late.
    trip = RtTrip("144000_1..N03R", stops=[("103N", at("24:03:00"), at("24:03:00"))])
    sd = delays_for([trip], at("23:58:00"))
    assert sd.iloc[0]["scheduled_ts"] == service_day_epoch("20261102") + 24 * 3600
    assert sd.iloc[0]["delay_min"] == pytest.approx(3.0)


def test_unmatched_trip_and_unscheduled_stop_have_no_delay():
    trips = [
        RtTrip("999900_1..N03R", stops=[("103N", at("10:00:00"), None)]),
        RtTrip(
            "060000_1..N03R", stops=[("999N", at("10:00:00"), None), ("104N", at("10:03:00"), None)]
        ),
    ]
    sd = delays_for(trips, at("09:50:00")).set_index(["trip_id", "stop_id"])
    assert sd.loc[("999900_1..N03R", "103N"), "match_method"] == "unmatched"
    assert pd.isna(sd.loc[("999900_1..N03R", "103N"), "delay_min"])
    assert sd.loc[("060000_1..N03R", "999N"), "match_method"] == "exact"
    assert pd.isna(sd.loc[("060000_1..N03R", "999N"), "delay_min"])
    assert sd.loc[("060000_1..N03R", "104N"), "delay_min"] == pytest.approx(1.0)


def test_stop_names_and_parent_station_are_attached():
    sd = delays_for([line_1_trip()], at("09:50:00"))
    assert sd.iloc[1]["stop_name"] == "Cathedral Pkwy (110 St)"
    assert sd.iloc[1]["station_id"] == "104"


def test_loop_trip_matches_the_closest_visit_to_a_stop():
    f = static_frames()
    trips = pd.DataFrame(
        [("L", "X-Weekday_050000_L..N01R", "Weekday", "loop", 0, "L..N01R")],
        columns=f["trips"].columns,
    )
    st = pd.DataFrame(
        [
            ("X-Weekday_050000_L..N01R", "A1N", "10:00:00", "10:00:00", 1),
            ("X-Weekday_050000_L..N01R", "B1N", "10:05:00", "10:05:00", 2),
            ("X-Weekday_050000_L..N01R", "A1N", "10:10:00", "10:10:00", 3),
        ],
        columns=f["stop_times"].columns,
    )
    schedule = StaticSchedule.from_frames(trips, st, f["calendar"])
    trip = RtTrip("050000_L..N01R", route_id="L", stops=[("A1N", at("10:11:00"), None)])
    sd = delays_for([trip], at("10:08:00"), schedule)
    assert len(sd) == 1
    assert sd.iloc[0]["delay_min"] == pytest.approx(1.0)


def test_trip_current_delay_is_first_stop_with_a_delay():
    trip = RtTrip(
        "060000_1..N03R",
        stops=[
            ("999N", at("10:00:00"), None),
            ("104N", at("10:04:00"), None),
            ("106N", at("10:09:00"), None),
        ],
    )
    td = trip_current_delays(delays_for([trip], at("09:59:00")))
    assert len(td) == 1
    assert td.iloc[0]["stop_id"] == "104N"
    assert td.iloc[0]["delay_min"] == pytest.approx(2.0)


def test_line_snapshot_excludes_unassigned_trips_from_delay_stats():
    trips = [
        line_1_trip(delay_s=360),  # assigned, 6 min late
        RtTrip(  # assigned, on time (supplement trip at 10:12)
            "061200_1..N03R", stops=[("103N", at("10:12:00"), None), ("104N", at("10:14:00"), None)]
        ),
        RtTrip(  # unassigned, 10 min "late": schedule noise that must be ignored
            "144000_1..N03R", stops=[("103N", at("24:10:00"), None)], assigned=False
        ),
        RtTrip("999900_1..N03R", stops=[("103N", at("10:20:00"), None)]),  # assigned, unmatched
    ]
    sd = delays_for(trips, at("09:58:00"))
    td = trip_current_delays(sd)
    lines = line_snapshot(sd, td, delayed_threshold_min=5.0)

    assert len(lines) == 1
    row = lines.iloc[0]
    assert (row["route_id"], row["direction"]) == ("1", "N")
    assert row["n_trips"] == 4
    assert row["n_trips_unassigned"] == 1
    assert row["n_trips_unmatched"] == 1  # assigned but not matched to the schedule
    assert row["n_trips_measured"] == 2
    assert row["delay_mean_min"] == pytest.approx(3.0)
    assert row["delay_median_min"] == pytest.approx(3.0)
    assert row["delay_max_min"] == pytest.approx(6.0)
    assert row["share_trips_delayed"] == pytest.approx(0.5)
    assert row["match_rate"] == pytest.approx(0.75)
    assert row["n_stop_updates"] == 7


def test_line_snapshot_groups_by_route_and_direction():
    trips = [
        line_1_trip(delay_s=60),
        RtTrip("036000_GS.N01R", route_id="GS", stops=[("902N", at("06:00:00"), None)]),
    ]
    lines = line_snapshot(*_with_trips(delays_for(trips, at("05:59:00"))), delayed_threshold_min=5)
    assert sorted(zip(lines["route_id"], lines["direction"], strict=True)) == [
        ("1", "N"),
        ("GS", "N"),
    ]


def _with_trips(sd):
    return sd, trip_current_delays(sd)
