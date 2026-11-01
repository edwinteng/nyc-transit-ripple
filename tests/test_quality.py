import pandas as pd
import pytest

from builders import RtTrip, at, build_feed, line_1_trip, make_schedule
from transit_ripple import quality as q
from transit_ripple.config import Settings
from transit_ripple.delays import compute_stop_delays, trip_current_delays
from transit_ripple.realtime import parse_feed
from transit_ripple.state import PipelineState

S = Settings()


def stop_updates(trips, header_ts):
    return parse_feed(build_feed(header_ts, trips), "numbered").stop_updates


# --- stale feed / outage / time-jump classification ---------------------------------


@pytest.mark.parametrize(
    ("header", "fetched", "last", "n_trips", "expected"),
    [
        (1000, 1010, None, 5, q.STATUS_OK),
        (1000, 1010, 990, 5, q.STATUS_OK),
        (1000, 1000 + 181, None, 5, q.STATUS_STALE),  # older than stale_after_s=180
        (1000, 1010, 1000, 5, q.STATUS_DUPLICATE),  # header unchanged, still fresh
        (1000, 1000 + 400, 1000, 5, q.STATUS_STALE),  # unchanged AND old -> stale, not dup
        (1000, 1010, 1100, 5, q.STATUS_BACKWARD),  # header went back in time
        (1000, 900, None, 5, q.STATUS_FUTURE),  # header 100 s ahead of the wall clock
        (1000, 1010, None, 0, q.STATUS_EMPTY),
        (None, 1010, None, 5, q.STATUS_INVALID),
    ],
)
def test_classify_snapshot(header, fetched, last, n_trips, expected):
    status, detail = q.classify_snapshot(header, fetched, last, n_trips, S)
    assert status == expected
    assert (detail is None) == (expected == q.STATUS_OK)


def test_missing_statuses_never_include_ok_or_duplicate():
    assert q.STATUS_OK not in q.MISSING_STATUSES
    assert q.STATUS_DUPLICATE not in q.MISSING_STATUSES
    assert {q.STATUS_STALE, q.STATUS_OUTAGE} <= q.MISSING_STATUSES


# --- deduplication ---------------------------------------------------------------------


def test_dedupe_trip_entities_keeps_newest_vehicle_report():
    old = line_1_trip(delay_s=0, vehicle_ts=at("09:58:00"), entity_id="e1", train_id="01 A")
    new = line_1_trip(delay_s=120, vehicle_ts=at("09:59:30"), entity_id="e2", train_id="01 B")
    df = stop_updates([old, new], at("10:00:00"))
    assert len(df) == 6

    out, dropped = q.dedupe_trip_entities(df)
    assert dropped == 1
    assert out["entity_id"].unique().tolist() == ["e2"]
    assert len(out) == 3


def test_dedupe_trip_entities_noop_without_duplicates():
    df = stop_updates([line_1_trip()], at("10:00:00"))
    out, dropped = q.dedupe_trip_entities(df)
    assert dropped == 0 and len(out) == len(df)


def test_dedupe_stop_updates_drops_identical_repeats_only():
    t = at("10:00:00")
    trip = RtTrip("060000_1..N03R", stops=[("103N", t, t), ("103N", t, t), ("104N", t + 60, None)])
    out, dropped = q.dedupe_stop_updates(stop_updates([trip], t - 60))
    assert dropped == 1
    assert out["stop_id"].tolist() == ["103N", "104N"]


def test_dedupe_latest_keeps_newest_row_per_key():
    df = pd.DataFrame({"k": ["a", "a", "b"], "ts": [2, 5, 1], "v": [10, 50, 1]})
    out, dropped = q.dedupe_latest(df, keys=["k"], order=["ts"])
    assert dropped == 1
    assert sorted(out["v"]) == [1, 50]


# --- time sanity filters -----------------------------------------------------------------


def test_filter_time_window_drops_missing_and_far_times():
    h = at("10:00:00")
    trip = RtTrip(
        "060000_1..N03R",
        stops=[
            ("103N", h - 3 * 3600, None),  # 3 h in the past (ghost train)
            ("104N", None, None),  # no time at all
            ("106N", h + 120, None),  # fine
            ("107N", h + 5 * 3600, None),  # 5 h ahead
        ],
    )
    out, counts = q.filter_time_window(stop_updates([trip], h), S)
    assert out["stop_id"].tolist() == ["106N"]
    assert counts == {"no_time": 1, "out_of_window": 2}


def _monotonic(offsets, h=None):
    h = h or at("10:00:00")
    stops = []
    for i, o in enumerate(offsets):
        arr, dep = o if isinstance(o, tuple) else (o, o)
        stops.append((f"{101 + i}N", h + arr, h + dep))
    out, dropped = q.filter_non_monotonic(
        stop_updates([RtTrip("060000_1..N03R", stops=stops)], h), S
    )
    return [int(x) - 101 for x in out["stop_id"].str[:3]], dropped


def test_filter_non_monotonic_drops_a_stop_that_goes_backwards():
    assert _monotonic([60, 120, 30, 180, 240]) == ([0, 1, 3, 4], 1)


def test_filter_non_monotonic_drops_a_forward_outlier_without_poisoning_later_stops():
    # A cumulative-max rule would drop the three stops after the outlier.
    assert _monotonic([60, 7200, 120, 180, 240]) == ([0, 2, 3, 4], 1)


def test_filter_non_monotonic_keeps_jitter_within_tolerance():
    assert _monotonic([60, 120, 100, 180]) == ([0, 1, 2, 3], 0)


def test_filter_non_monotonic_drops_departure_before_arrival():
    assert _monotonic([60, (300, 200), 400]) == ([0, 2], 1)


def test_filter_non_monotonic_is_per_trip():
    h = at("10:00:00")
    a = RtTrip("060000_1..N03R", stops=[("103N", h + 600, None)])
    b = RtTrip("061200_1..N03R", stops=[("103N", h + 60, None)])  # earlier, but other trip
    out, dropped = q.filter_non_monotonic(stop_updates([a, b], h), S)
    assert dropped == 0 and len(out) == 2


def test_filter_stale_predictions():
    df = pd.DataFrame({"time_kind": ["actual", "stale", "predicted", "stale"]})
    out, dropped = q.filter_stale_predictions(df)
    assert dropped == 2
    assert out["time_kind"].tolist() == ["actual", "predicted"]


def test_filter_implausible_delays():
    df = pd.DataFrame({"delay_min": [0.0, -45.0, 300.0, None, 239.0]})
    out, dropped = q.filter_implausible_delays(df, S)
    assert dropped == 2
    assert out["delay_min"].isna().sum() == 1
    assert sorted(out["delay_min"].dropna()) == [0.0, 239.0]


def _trip_delays(delay_s, header_ts, **kw):
    su = stop_updates([line_1_trip(delay_s=delay_s, **kw)], header_ts)
    sd = compute_stop_delays(su, make_schedule())
    return sd, trip_current_delays(sd)


def test_filter_delay_jumps_uses_elapsed_time_plus_tolerance():
    state = PipelineState()
    h0 = at("09:50:00")

    sd, td, jumps = q.filter_delay_jumps(*_trip_delays(60, h0), state, S)  # first: 1 min late
    assert not jumps and len(td) == 1
    key = state.trip_key("numbered", "20261102", "060000_1..N03R")
    assert state.trip_delays[key] == [h0, 1.0]

    # 5 min later the delay grew to 6 min: change 5 <= 5 + 15 -> plausible.
    sd, td, jumps = q.filter_delay_jumps(*_trip_delays(360, h0 + 300), state, S)
    assert not jumps and len(sd) == 3

    # 1 min later the delay is 40 min: change 34 > 1 + 15 -> abnormal jump, trip dropped.
    sd, td, jumps = q.filter_delay_jumps(*_trip_delays(2400, h0 + 360), state, S)
    assert len(jumps) == 1 and sd.empty and td.empty
    assert jumps[0].previous_delay_min == pytest.approx(6.0)
    assert jumps[0].delay_min == pytest.approx(40.0)

    # The new level is remembered, so a persisting step change is filtered only once.
    sd, td, jumps = q.filter_delay_jumps(*_trip_delays(2430, h0 + 420), state, S)
    assert not jumps and len(sd) == 3


def test_filter_delay_jumps_ignores_unassigned_trips():
    state = PipelineState()
    q.filter_delay_jumps(*_trip_delays(0, at("09:50:00"), assigned=False), state, S)
    assert state.trip_delays == {}


def test_state_round_trip_and_prune(tmp_path):
    st = PipelineState(last_header_ts={"l": 100}, trip_delays={"a": [100, 1.0], "b": [20000, 2.0]})
    path = tmp_path / "state.json"
    st.save(path)
    loaded = PipelineState.load(path)
    assert loaded.last_header_ts == {"l": 100}
    loaded.prune(now_ts=20000)
    assert list(loaded.trip_delays) == ["b"]


def test_state_load_survives_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert PipelineState.load(path).last_header_ts == {}


# --- report ------------------------------------------------------------------------------


def _record(feed, status, **kw):
    return q.FeedPollRecord(
        run_id="r", poll_index=0, feed_id=feed, url="u", fetched_ts=0.0, status=status, **kw
    )


def test_report_status_issues_and_markdown():
    report = q.RunReport(run_id="r", started_ts=0.0, polls_requested=1, thresholds={}, static={})
    report.records = [
        _record("ace", q.STATUS_OK, n_trips=10, n_trips_matched=9, n_rows=100),
        _record("l", q.STATUS_STALE, detail="header age 400s > 180s"),
        _record("g", q.STATUS_OK, n_trips=10, n_trips_matched=2),
    ]
    assert report.overall_status() == "degraded"
    issues = report.issues()
    assert any("l poll 0: stale" in i and "marked missing" in i for i in issues)
    assert any("low schedule match rate 20%" in i for i in issues)

    md = report.to_markdown()
    assert "| ace | okx1 |" in md and "| l | stalex1 |" in md
    d = report.to_dict()
    assert d["totals"]["status_counts"] == {"ok": 2, "stale": 1}
    assert d["feeds"][0]["match_rate"] == pytest.approx(0.9)


def test_report_failed_when_every_feed_missing():
    report = q.RunReport(run_id="r", started_ts=0.0, polls_requested=1, thresholds={}, static={})
    report.records = [_record("ace", q.STATUS_OUTAGE), _record("l", q.STATUS_STALE)]
    assert report.overall_status() == "failed"
    report.records = []
    assert report.overall_status() == "failed"
