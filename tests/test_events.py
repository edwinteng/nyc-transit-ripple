import datetime as dt
import json

import pandas as pd

from transit_ripple.config import Settings
from transit_ripple.events import (
    END_DATA_GAP,
    END_RECOVERED,
    STATUS_ENDED,
    STATUS_ONGOING,
    affected_stations,
    detect_events,
    link_alerts,
    make_event_id,
    merge_with_stored,
    process_events,
    route_series,
    summarize_surface,
    surface_series,
)
from transit_ripple.explain import uncited_numbers
from transit_ripple.storage import connect, write_reference, write_table

T0 = pd.Timestamp("2026-11-30 14:00")  # Monday 09:00 EST
S = Settings(
    event_threshold_min=5.0,
    event_min_minutes=5,
    event_min_observed=2,
    event_max_gap_min=10,
    event_min_trips=2,
)


def series(values, route="A", status=None, start=T0):
    """Route series from a list of values (None = missing minute)."""
    rows = []
    for i, v in enumerate(values):
        st = (status[i] if status else "observed") if v is not None else "missing"
        rows.append(
            {
                "minute_utc": start + pd.Timedelta(minutes=i),
                "route_id": route,
                "value": v,
                "direction": "S",
                "fill_status": st,
                "n_trips": 4.0,
                "max_trip_delay_min": (v or 0) + 3,
                "directions_above": ["S"] if v and v >= 5 else [],
            }
        )
    return pd.DataFrame(rows)


# --- state machine -----------------------------------------------------------------------


def test_event_opens_after_n_minutes_and_ends_when_delay_recovers():
    ev = detect_events(series([1, 6, 7, 8, 9, 10, 12, 8, 2, 1]), S)
    assert len(ev) == 1
    e = ev[0]
    assert e.start == T0 + pd.Timedelta(minutes=1)
    assert e.end == T0 + pd.Timedelta(minutes=8)  # exclusive: minute after the last above
    assert (e.status, e.end_reason) == (STATUS_ENDED, END_RECOVERED)
    st = e.stats()
    assert st["peak_delay_min"] == 12 and st["peak_utc"] == T0 + pd.Timedelta(minutes=6)
    assert st["n_minutes_above"] == 7 and st["directions"] == ["S"]
    assert st["max_trip_delay_min"] == 15


def test_short_spikes_do_not_open_events():
    assert detect_events(series([6, 7, 8, 9, 1, 6, 7, 8, 9]), S) == []


def test_forward_filled_minutes_count_but_need_enough_observations():
    one_obs = ["observed", "filled", "filled", "filled", "filled", "filled"]
    assert detect_events(series([8] * 6, status=one_obs), S) == []
    two_obs = ["observed", "filled", "filled", "observed", "filled", "filled"]
    ev = detect_events(series([8] * 6, status=two_obs), S)
    assert len(ev) == 1 and ev[0].start == T0
    assert ev[0].stats()["n_minutes_filled"] == 4


def test_missing_minutes_break_pending_runs():
    assert detect_events(series([8, 8, 8, None, 8, 8, 8]), S) == []


def test_short_data_gap_is_tolerated_long_gap_ends_event():
    ev = detect_events(series([8] * 5 + [None] * 4 + [9, 9, 1]), S)
    assert len(ev) == 1 and ev[0].end == T0 + pd.Timedelta(minutes=11)
    assert ev[0].end_reason == END_RECOVERED
    ev = detect_events(series([8] * 5 + [None] * 12 + [9] * 5), S)
    assert ev[0].end_reason == END_DATA_GAP
    assert ev[0].end == T0 + pd.Timedelta(minutes=5)
    assert len(ev) == 2  # the delay after the gap is a new event


def test_event_still_above_at_the_end_is_ongoing_and_routes_are_separate():
    df = pd.concat([series([8] * 6, route="A"), series([1] * 6, route="C")])
    ev = detect_events(df, S)
    assert [(e.route_id, e.status) for e in ev] == [("A", STATUS_ONGOING)]
    assert make_event_id("A", ev[0].start) == "A-20261130T1400Z"


# --- storage-backed pieces -----------------------------------------------------------------


def line_rows(route, direction, values, trips=4, start=T0, run="r"):
    """line_minute rows; later runs carry a later fetch time, as in production."""
    fetched_delay = pd.Timedelta(seconds=30 if run == "r" else 300)
    rows = []
    for i, v in enumerate(values):
        if v is None:
            continue
        t = start + pd.Timedelta(minutes=i)
        rows.append(
            {
                "bucket_utc": t,
                "observed_utc": t,
                "fetched_utc": t + fetched_delay,
                "run_id": run,
                "feed_id": "ace",
                "route_id": route,
                "direction": direction,
                "status": "ok",
                "n_trips_measured": trips,
                "delay_median_min": float(v),
                "delay_mean_min": float(v),
                "delay_max_min": float(v) + 3,
            }
        )
    return rows


def test_route_series_takes_worst_direction_and_requires_enough_trips(tmp_path):
    rows = (
        line_rows("A", "N", [2, 2, 9, 9])
        + line_rows("A", "S", [6, 7, 1, 1])
        + line_rows("C", "N", [20, 20, 20, 20], trips=1)
    )
    write_table(tmp_path, "line_minute", pd.DataFrame(rows), "r")
    rs = route_series(
        connect(tmp_path), S, T0.to_pydatetime(), (T0 + pd.Timedelta(minutes=3)).to_pydatetime()
    )
    a = rs[rs["route_id"] == "A"]
    assert a["value"].tolist() == [6, 7, 9, 9]
    assert a["direction"].tolist() == ["S", "S", "N", "N"]
    assert a["directions_above"].tolist() == [["S"], ["S"], ["N"], ["N"]]
    c = rs[rs["route_id"] == "C"]
    assert set(c["fill_status"]) == {"missing"}  # 1 measured train is not enough


def test_route_series_forward_fills_sampling_gaps(tmp_path):
    vals = [8, 8, 8, None, None, 8, 8, 8, None, None]  # cron-like: 3 of 5 minutes observed
    write_table(tmp_path, "line_minute", pd.DataFrame(line_rows("A", "S", vals)), "r")
    rs = route_series(
        connect(tmp_path), S, T0.to_pydatetime(), (T0 + pd.Timedelta(minutes=9)).to_pydatetime()
    )
    assert rs["fill_status"].tolist()[:6] == ["observed"] * 3 + ["filled"] * 2 + ["observed"]
    ev = detect_events(rs, S)
    assert len(ev) == 1 and ev[0].status == STATUS_ONGOING


def test_merge_reuses_stored_ids_and_closes_abandoned_events():
    stored = pd.DataFrame(
        [
            {
                "event_id": "A-20261130T1330Z",
                "route_id": "A",
                "status": STATUS_ONGOING,
                "start_utc": T0 - pd.Timedelta(minutes=30),
                "end_utc": T0 + pd.Timedelta(minutes=2),
            },
            {
                "event_id": "C-20261130T1200Z",
                "route_id": "C",
                "status": STATUS_ONGOING,
                "start_utc": T0 - pd.Timedelta(hours=2),
                "end_utc": T0 - pd.Timedelta(hours=1),
            },
        ]
    )
    detected = detect_events(series([8] * 6), S)  # A, starting at T0 (window cut)
    merged, to_close = merge_with_stored(detected, stored, S, T0 + pd.Timedelta(minutes=6))
    event_id, start, _ = merged[0]
    assert event_id == "A-20261130T1330Z" and start == T0 - pd.Timedelta(minutes=30)
    assert [r["event_id"] for r in to_close] == ["C-20261130T1200Z"]
    merged, _ = merge_with_stored(detected, pd.DataFrame(), S, T0)
    assert merged[0][0] == "A-20261130T1400Z"


def write_context(tmp_path):
    """Station observations, alerts, neighbors, traffic, bikes and baselines."""
    sm = []
    for i, (station, name, delay) in enumerate(
        [
            ("A41", "Jay St-MetroTech", 12.0),
            ("A42", "Hoyt-Schermerhorn Sts", 7.0),
            ("A43", "Lafayette Av", 3.0),
        ]
    ):
        t = T0 + pd.Timedelta(minutes=2 + i)
        sm.append(
            {
                "bucket_utc": t,
                "observed_utc": t,
                "run_id": "r",
                "feed_id": "ace",
                "route_id": "A",
                "direction": "S",
                "station_id": station,
                "stop_name": name,
                "n_trains": 1,
                "delay_mean_min": delay,
                "delay_max_min": delay,
            }
        )
    write_table(tmp_path, "station_minute", pd.DataFrame(sm), "r")

    def alert(aid, atype, planned, stations, periods, routes=("A",)):
        return {
            "bucket_utc": T0,
            "observed_utc": T0,
            "fetched_utc": T0,
            "run_id": "r",
            "alert_id": aid,
            "alert_type": atype,
            "is_planned": planned,
            "created_utc": T0,
            "updated_utc": T0,
            "active_start_utc": None,
            "active_end_utc": None,
            "active_periods": [{"start_utc": s, "end_utc": e} for s, e in periods],
            "route_ids": list(routes),
            "station_ids": list(stations),
            "header_text": f"{atype} on the A",
            "description_text": None,
        }

    alerts = [
        alert("unplanned", "Delays", False, ["A41"], [(T0 - pd.Timedelta(minutes=5), None)]),
        alert("planned", "Planned - Part Suspended", True, [], [(T0 - pd.Timedelta(days=1), None)]),
        alert("other_line", "Delays", False, ["A41"], [(T0, None)], routes=("G",)),
        alert(
            "expired",
            "Delays",
            False,
            ["A41"],
            [(T0 - pd.Timedelta(hours=5), T0 - pd.Timedelta(hours=4))],
        ),
        alert("gone", "Delays", False, [], [(T0 - pd.Timedelta(hours=5), None)]),
    ]
    write_table(tmp_path, "alerts", pd.DataFrame(alerts), "r")
    presence = [
        {
            "bucket_utc": T0 + pd.Timedelta(minutes=m),
            "observed_utc": T0,
            "run_id": "r",
            "alert_id": a,
            "updated_utc": T0,
            "is_active": True,
        }
        for m in range(0, 10, 5)
        for a in ("unplanned", "planned", "other_line", "expired")
    ]
    presence.append(
        {
            "bucket_utc": T0 - pd.Timedelta(hours=3),
            "observed_utc": T0,
            "run_id": "r",
            "alert_id": "gone",
            "updated_utc": T0,
            "is_active": True,
        }
    )
    write_table(tmp_path, "alert_presence", pd.DataFrame(presence), "r")

    write_reference(
        tmp_path,
        "station_neighbors",
        pd.DataFrame(
            [
                {
                    "station_id": "A41",
                    "station_name": "Jay St-MetroTech",
                    "station_lat": 40.69,
                    "station_lon": -73.98,
                    "neighbor_type": nt,
                    "neighbor_id": nid,
                    "neighbor_name": nid,
                    "distance_m": 100.0,
                }
                for nt, nid in [
                    ("traffic_link", "L1"),
                    ("bike_station", "b1"),
                    ("bike_station", "b2"),
                ]
            ]
        ),
    )
    traffic, bikes = [], []
    for m in range(0, 12, 5):
        t = T0 + pd.Timedelta(minutes=m)
        traffic.append(
            {
                "bucket_utc": t,
                "observed_utc": t,
                "fetched_utc": t,
                "run_id": "r",
                "link_id": "L1",
                "speed_mph": 15.0,
                "is_valid": True,
                "age_s": 30.0,
            }
        )
        for sid, n in (("b1", 4), ("b2", 6)):
            bikes.append(
                {
                    "bucket_utc": t,
                    "observed_utc": t,
                    "fetched_utc": t,
                    "run_id": "r",
                    "station_id": sid,
                    "bikes_available": n,
                    "docks_available": 10,
                    "is_installed": True,
                    "is_renting": True,
                    "is_returning": True,
                    "report_age_s": 30.0,
                }
            )
    write_table(tmp_path, "traffic_minute", pd.DataFrame(traffic), "r")
    write_table(tmp_path, "bike_minute", pd.DataFrame(bikes), "r")
    baselines = pd.DataFrame(
        [
            {
                "metric": "traffic_speed_mph",
                "entity_id": "L1",
                "weekday": 0,
                "slot_start_min": 540,
                "n_days": 4,
                "n_obs": 40,
                "mean": 20.0,
                "median": 20.0,
                "std": 2.0,
                "confidence": "ok",
            },
            {
                "metric": "bikes_available",
                "entity_id": "b1",
                "weekday": 0,
                "slot_start_min": 540,
                "n_days": 1,
                "n_obs": 10,
                "mean": 8.0,
                "median": 8.0,
                "std": 1.0,
                "confidence": "low",
            },
            {
                "metric": "bikes_available",
                "entity_id": "b2",
                "weekday": 0,
                "slot_start_min": 540,
                "n_days": 1,
                "n_obs": 10,
                "mean": 12.0,
                "median": 12.0,
                "std": 1.0,
                "confidence": "low",
            },
        ]
    )
    write_reference(tmp_path, "baselines", baselines)
    return baselines


def test_affected_stations_and_alert_linking(tmp_path):
    write_context(tmp_path)
    con = connect(tmp_path)
    end = T0 + pd.Timedelta(minutes=10)
    st = affected_stations(con, "A", T0, end, 5.0)
    assert st["station_id"].tolist() == ["A41", "A42"]  # Lafayette Av below threshold
    assert st["max_delay_min"].tolist() == [12.0, 7.0]

    alerts = link_alerts(con, "A", T0, end, st["station_id"].tolist())
    assert [a["alert_id"] for a in alerts] == ["unplanned", "planned"]  # unplanned ranks first
    assert alerts[0]["n_matching_stations"] == 1 and alerts[1]["is_planned"]


def test_surface_series_and_summary_compare_with_baselines(tmp_path):
    baselines = write_context(tmp_path)
    con = connect(tmp_path)
    end = T0 + pd.Timedelta(minutes=10)
    s = surface_series(con, S, baselines, ["A41"], T0, end)
    assert s.attrs["n_bike_stations_nearby"] == 2
    row = s.set_index("minute_utc").loc[T0 + pd.Timedelta(minutes=3)]
    assert row["traffic_speed_mph"] == 15.0 and row["traffic_baseline_mph"] == 20.0
    assert row["bikes_available"] == 10 and row["bikes_baseline"] == 20.0
    summary = summarize_surface(s, T0, end)
    assert summary["traffic_change_pct"] == -25.0
    assert summary["traffic_baseline_confidence"] == "ok"
    assert (summary["bikes_available"], summary["bikes_baseline"], summary["bikes_change"]) == (
        10.0,
        20.0,
        -10.0,
    )
    assert summary["bikes_baseline_confidence"] == "low"
    empty = summarize_surface(surface_series(con, S, baselines, ["ZZZ"], T0, end), T0, end)
    assert empty["traffic_speed_mph"] is None and empty["bikes_n_stations"] == 0


def test_process_events_end_to_end(tmp_path):
    write_context(tmp_path)
    rows = line_rows("A", "S", [8, 9, 12, 11, 10, 9, 8, 2, 1])
    write_table(tmp_path, "line_minute", pd.DataFrame(rows), "r")
    # The surface is followed for 2 min after the event, so a later run at
    # now + 5 min sees exactly the same data and must not write a new version.
    settings = Settings(
        data_dir=tmp_path,
        event_min_minutes=5,
        event_lookback_hours=2,
        impact_recovery_window_min=2,
    )
    con = connect(tmp_path)
    from transit_ripple.storage import read_reference

    now = (T0 + pd.Timedelta(minutes=9)).to_pydatetime()
    events = process_events(con, settings, read_reference(tmp_path, "baselines"), now, "run1")
    assert len(events) == 1
    ev = events.iloc[0].to_dict()
    assert ev["event_id"] == "A-20261130T1400Z" and ev["status"] == STATUS_ENDED
    assert ev["duration_min"] == 7.0 and ev["peak_delay_min"] == 12.0
    assert ev["affected_station_names"] == ["Jay St-MetroTech", "Hoyt-Schermerhorn Sts"]
    assert list(ev["focal_station_ids"]) == ["A41", "A42"]
    assert ev["n_linked_alerts"] == 2
    assert ev["traffic_change_pct"] == -25.0
    assert ev["summary"] == (
        "A trains ran a median of up to 12.0 min late for 7 min, from 09:00 to 09:07 ET "
        "on Mon Nov 30, worst at Jay St-MetroTech (12.0 min). Within 500 m of the "
        "worst-hit stations, nearby traffic was 25% slower than usual for this weekday "
        "and time (surface impact 63/100), and it had not recovered by 09:09 ET."
    )
    # Traffic -25% of a 40% full drop -> 62.5; the bike baseline is low-confidence
    # and there are no cameras, so the score is renormalized over traffic alone.
    assert ev["impact_score"] == 62.5
    assert list(ev["impact_signals_used"]) == ["traffic"]
    detail = json.loads(ev["impact_detail"])["signals"]
    assert detail["bikes"]["status"] == "low_confidence"
    assert detail["camera"]["status"] == "no_data"
    assert ev["surface_recovery_status"] == "not_yet"
    sentences = json.loads(ev["explanation"])
    assert len(sentences) == 2
    for sentence in sentences:
        assert uncited_numbers(sentence["text"], sentence["metrics"]) == []
    assert uncited_numbers(ev["summary"], json.loads(ev["cited_metrics"])) == []

    # Stored, then re-detected on the next run: nothing changed, so no new version.
    write_table(tmp_path, "events", events, "run1")
    baselines = read_reference(tmp_path, "baselines")
    later = process_events(
        connect(tmp_path), settings, baselines, now + dt.timedelta(minutes=5), "r2"
    )
    assert later.empty

    # New data extends the event: a new version with the same id.
    more = line_rows("A", "S", [None] * 7 + [15, 15, 15, 1], run="r2")
    write_table(tmp_path, "line_minute", pd.DataFrame(more), "r2")
    later = process_events(
        connect(tmp_path), settings, baselines, now + dt.timedelta(minutes=5), "r2"
    )
    assert later["event_id"].tolist() == ["A-20261130T1400Z"]
    assert later["peak_delay_min"].tolist() == [15.0]


def test_camera_hook_runs_for_ongoing_events_and_counts_are_stored(tmp_path):
    from transit_ripple.cameras.storage import write_counts
    from transit_ripple.cameras.storage import write_reference as write_camera_reference

    write_context(tmp_path)
    write_camera_reference(
        tmp_path,
        "camera_station_map",
        pd.DataFrame(
            {
                "station_id": ["A41", "A42"],
                "camera_id": ["c1", "c9"],
                "camera_name": ["Adams St", "Far"],
                "distance_m": [60.0, 900.0],  # c9 is outside the 500 m radius
            }
        ),
    )
    settings = Settings(data_dir=tmp_path, event_min_minutes=5, event_lookback_hours=2)
    calls = []

    def hook(event_id, station_ids):
        calls.append((event_id, list(station_ids)))
        t = T0 + pd.Timedelta(minutes=6, seconds=20)
        counts = {"car": 3, "bus": 1, "truck": 0, "person": 4}
        row = {"bucket_utc": t.floor("min"), "observed_utc": t, "run_id": "cam", "status": "ok"}
        write_counts(settings.data_dir, pd.DataFrame([{**row, "camera_id": "c1", **counts}]), "cam")
        return {"cameras_counted": 1}

    # Ended event: no camera request.
    rows = line_rows("A", "S", [8, 9, 12, 11, 10, 9, 8, 2, 1])
    write_table(tmp_path, "line_minute", pd.DataFrame(rows), "r")
    now = (T0 + pd.Timedelta(minutes=9)).to_pydatetime()
    ended = process_events(connect(tmp_path), settings, pd.DataFrame(), now, "r1", camera_hook=hook)
    assert ended["status"].tolist() == [STATUS_ENDED] and calls == []

    # Ongoing event (same data, observed earlier): one request for the focal stations.
    live = tmp_path / "ongoing"
    write_context(live)
    mapping = pd.read_parquet(tmp_path / "reference" / "camera_station_map.parquet")
    write_camera_reference(live, "camera_station_map", mapping)
    rows = line_rows("A", "S", [8, 9, 12, 11, 10, 9, 8])
    write_table(live, "line_minute", pd.DataFrame(rows), "r")
    settings = Settings(data_dir=live, event_min_minutes=5, event_lookback_hours=2)
    now = (T0 + pd.Timedelta(minutes=6)).to_pydatetime()
    events = process_events(connect(live), settings, pd.DataFrame(), now, "r2", camera_hook=hook)
    ev = events.iloc[0].to_dict()
    assert ev["status"] == STATUS_ONGOING
    assert calls == [("A-20261130T1400Z", ["A41", "A42"])]
    assert list(ev["camera_ids"]) == ["c1"]
    (count,) = list(ev["camera_counts"])
    assert (count["camera_id"], count["car"], count["bus"], count["person"]) == ("c1", 3, 1, 4)
    assert ev["camera_n_cameras"] == 1 and ev["camera_objects"] == 8.0
    assert ev["camera_baseline_confidence"] == "none"
