"""M5: historical recovery model and the go / switch / wait recommendation engine."""

import json

import pandas as pd
import pytest

from transit_ripple.config import Settings
from transit_ripple.explain import uncited_numbers
from transit_ripple.recommend import recommend, ride_minutes, walk_minutes
from transit_ripple.recovery import RecoveryModel, cause_category, history_frame, time_band
from transit_ripple.storage import connect, write_reference, write_table

NOW = pd.Timestamp("2026-11-30 14:30")  # Monday 09:30 New York time
S = Settings(rec_reference_trip_km=4.0, recovery_min_events=5)


# --------------------------------------------------------------------------------------
# Recovery model
# --------------------------------------------------------------------------------------


def test_cause_categories_and_time_bands():
    alerts = [
        {"is_planned": True, "header_text": "Planned work: no trains"},
        {"is_planned": False, "alert_type": "Delays", "header_text": "due to a signal problem"},
    ]
    assert cause_category(alerts) == "signal"  # planned work is ignored
    assert cause_category([{"is_planned": False, "header_text": "someone on the tracks"}]) == (
        "person_on_tracks"
    )
    assert cause_category([{"is_planned": False, "header_text": "Delays"}]) == "other"
    assert cause_category([]) is None
    assert time_band(pd.Timestamp("2026-11-30 13:00")) == "am_peak"  # 08:00 ET Monday
    assert time_band(pd.Timestamp("2026-11-30 22:00")) == "pm_peak"  # 17:00 ET
    assert time_band(pd.Timestamp("2026-11-29 13:00")) == "weekend"  # Sunday


def history(rows):
    """rows: (route, band, cause, duration, clearing)."""
    return pd.DataFrame(
        rows, columns=["route_id", "band", "cause", "duration_min", "clearing_min"]
    ).assign(event_id=[f"e{i}" for i in range(len(rows))])


def test_recovery_uses_the_most_specific_group_with_enough_events():
    rows = [("A", "am_peak", "signal", d, 10.0) for d in (10, 20, 30, 40, 50)]
    rows += [("A", "pm_peak", None, 90.0, None)] * 5
    model = RecoveryModel(history(rows), S)
    est = model.estimate("A", NOW, "signal", elapsed_min=0)
    d = est.duration
    assert (d.level, d.n_events, d.confidence) == ("line_band_cause", 5, "ok")
    assert (d.median_min, d.p75_min) == (30.0, 40.0)
    assert est.clearing.median_min == 10.0
    # Elapsed 25 min: only the events that lasted longer count (30, 40, 50 -> 5, 15, 25).
    later = model.estimate("A", NOW, "signal", elapsed_min=25)
    assert (later.remaining_median_min, later.remaining_p75_min) == (15.0, 20.0)


def test_recovery_falls_back_to_broader_groups_with_low_confidence():
    rows = [("A", "am_peak", "signal", 30.0, None)] * 2 + [("C", "midday", None, 60.0, None)] * 4
    model = RecoveryModel(history(rows), S)
    est = model.estimate("A", NOW, "signal", elapsed_min=0)
    # 2 events for A: not enough at any level; the "all lines" group (6) is used.
    assert (est.duration.level, est.duration.n_events, est.duration.confidence) == (
        "all",
        6,
        "low",
    )
    assert est.clearing.level == "none" and est.clearing.median_min is None
    empty = RecoveryModel(history([]), S).estimate("A", NOW, None, 5)
    assert empty.duration.confidence == "none" and empty.remaining_median_min is None


def test_history_frame_from_events():
    ev = pd.DataFrame(
        {
            "event_id": ["x"],
            "route_id": ["A"],
            "start_utc": [pd.Timestamp("2026-11-30 13:00")],
            "end_utc": [pd.Timestamp("2026-11-30 13:40")],
            "duration_min": [40.0],
            "linked_alerts": [[{"is_planned": False, "header_text": "signal problems"}]],
            "surface_recovery_status": ["recovered"],
            "surface_recovered_utc": [pd.Timestamp("2026-11-30 13:52")],
        }
    )
    h = history_frame(ev).iloc[0]
    assert (h.band, h.cause, h.duration_min, h.clearing_min) == ("am_peak", "signal", 40.0, 12.0)


# --------------------------------------------------------------------------------------
# Recommendation engine
# --------------------------------------------------------------------------------------


def scenario(tmp_path, *, bike_then=10, bus_speed_mph=4.0, past_events=True):
    """Jay St-MetroTech (A41) with an ongoing A line event, a station 240 m away,
    Citi Bike docks, two bus routes and live arrivals."""
    write_reference(
        tmp_path,
        "subway_stations",
        pd.DataFrame(
            {
                "station_id": ["A41", "232", "FAR", "NODATA"],
                "station_name": ["Jay St-MetroTech", "Borough Hall", "Far Rockaway", "Hoyt St"],
                "lat": [40.6923, 40.69305, 40.60, 40.6905],
                "lon": [-73.9873, -73.98998, -73.75, -73.9850],
                "routes": [["A", "C", "F"], ["2", "3", "4", "5"], ["A"], ["G"]],
            }
        ),
    )
    nb = [
        ("bike_station", "b1", "Jay St & Willoughby", 120.0),
        ("bike_station", "b2", "Tillary St", 300.0),
        ("bus_stop", "s1", "JAY ST/MYRTLE AV", 80.0),
        ("bus_stop", "s2", "ADAMS ST/TILLARY ST", 200.0),
    ]
    write_reference(
        tmp_path,
        "station_neighbors",
        pd.DataFrame(
            [
                {
                    "station_id": "A41",
                    "station_name": "Jay St-MetroTech",
                    "neighbor_type": t,
                    "neighbor_id": i,
                    "neighbor_name": n,
                    "distance_m": d,
                }
                for t, i, n, d in nb
            ]
        ),
    )
    write_reference(
        tmp_path,
        "bus_stops",
        pd.DataFrame(
            {
                "stop_id": ["s1", "s2"],
                "stop_name": ["JAY ST/MYRTLE AV", "ADAMS ST/TILLARY ST"],
                "lat": [40.693, 40.695],
                "lon": [-73.987, -73.989],
                "routes": [["B67"], ["B25", "B67", "BM5"]],  # BM5: express, never suggested
            }
        ),
    )

    # Live arrivals, observed one minute ago.
    def arrival(station, route, direction, minutes, trip):
        return {
            "bucket_utc": NOW - pd.Timedelta(minutes=1),
            "observed_utc": NOW - pd.Timedelta(minutes=1),
            "run_id": "r",
            "feed_id": "x",
            "route_id": route,
            "direction": direction,
            "trip_id": trip,
            "start_date": "20261130",
            "stop_id": station + direction,
            "station_id": station,
            "arrival_utc": NOW + pd.Timedelta(minutes=minutes),
        }

    write_table(
        tmp_path,
        "stop_delays",
        pd.DataFrame(
            [
                arrival("A41", "C", "N", 6, "c1"),
                arrival("A41", "F", "S", 4, "f1"),
                arrival("A41", "A", "S", 1, "a1"),  # the affected line: never suggested
                arrival("232", "4", "N", 9, "41"),
                arrival("232", "2", "S", 12, "21"),
            ]
        ),
        "r",
    )

    def bikes(station, n, minutes_ago):
        t = NOW - pd.Timedelta(minutes=minutes_ago)
        return {
            "bucket_utc": t,
            "observed_utc": t,
            "fetched_utc": t,
            "run_id": "r",
            "station_id": station,
            "bikes_available": n,
            "docks_available": 10,
            "is_installed": True,
            "is_renting": True,
            "is_returning": True,
            "report_age_s": 30.0,
        }

    write_table(
        tmp_path,
        "bike_minute",
        pd.DataFrame(
            [bikes("b1", 5, 1), bikes("b2", 0, 1), bikes("b1", bike_then, 15), bikes("b2", 0, 15)]
        ),
        "r",
    )
    seconds = 60.0
    write_table(
        tmp_path,
        "bus_minute",
        pd.DataFrame(
            {
                "bucket_utc": [NOW - pd.Timedelta(minutes=2)],
                "observed_utc": [NOW - pd.Timedelta(minutes=2)],
                "run_id": ["r"],
                "segment_id": ["B67|0|s1"],
                "route_id": ["B67"],
                "direction_id": [0],
                "stop_id": ["s1"],
                "n_samples": [2],
                "n_vehicles": [2],
                "distance_m": [bus_speed_mph * 0.44704 * seconds],
                "duration_s": [seconds],
                "speed_mph": [bus_speed_mph],
            }
        ),
        "r",
    )
    baselines = pd.DataFrame(
        [
            {
                "metric": "bus_speed_mph",
                "entity_id": "B67|0|s1",
                "weekday": 0,
                "slot_start_min": 540,  # 09:28 ET falls in the 09:00 slot
                "n_days": 4,
                "n_obs": 40,
                "mean": 8.0,
                "median": 8.0,
                "std": 1.0,
                "confidence": "ok",
            }
        ]
    )
    events = [
        {
            "bucket_utc": NOW - pd.Timedelta(minutes=12),
            "computed_utc": NOW,
            "run_id": "r",
            "event_id": "A-1",
            "route_id": "A",
            "status": "ongoing",
            "start_utc": NOW - pd.Timedelta(minutes=12),
            "end_utc": NOW,
            "peak_delay_min": 14.0,
            "focal_station_ids": ["A41"],
            "linked_alerts": [{"alert_id": "x", "is_planned": False, "header_text": "signal"}],
        }
    ]
    if past_events:
        for i, d in enumerate((10, 20, 30, 40, 50)):
            start = NOW - pd.Timedelta(days=7 + i)
            events.append(
                {
                    "bucket_utc": start,
                    "computed_utc": start + pd.Timedelta(hours=1),
                    "run_id": "r",
                    "event_id": f"A-past{i}",
                    "route_id": "A",
                    "status": "ended",
                    "end_reason": "recovered",
                    "start_utc": start,
                    "end_utc": start + pd.Timedelta(minutes=d),
                    "duration_min": float(d),
                    "linked_alerts": [
                        {"alert_id": "y", "is_planned": False, "header_text": "signal"}
                    ],
                }
            )
    write_table(tmp_path, "events", pd.DataFrame(events), "r")
    return baselines


def run(tmp_path, settings=S, **kw):
    baselines = scenario(tmp_path, **kw)
    s = settings.with_overrides(data_dir=tmp_path)
    recs = recommend(connect(tmp_path), s, baselines, NOW.to_pydatetime(), "run1")
    return recs.sort_values("rank").reset_index(drop=True)


def test_options_are_ranked_with_live_arrivals_and_cited_text(tmp_path):
    recs = run(tmp_path)
    assert recs["rank"].tolist() == list(range(1, len(recs) + 1))
    assert recs["option_type"].tolist()[0] == "walk_line"
    first = recs.iloc[0]
    # Same station: 2 min to change platforms; next C (N) in 6 -> 4, next F (S) in 4 -> 2.
    assert first["text"].startswith(
        "Switch to the C/F at this station (next trains in 4 min northbound, 2 min southbound)"
    )
    ride = ride_minutes(S.rec_subway_speed_mph, S)
    assert first["est_total_min"] == pytest.approx(2 + 2 + ride, abs=0.1)
    assert first["is_obvious"] and not first["crowded"]
    walk = recs[recs["option_id"] == "walk_line:232"].iloc[0]
    # Only lines with trains predicted are named (the 3 and 5 have none in the data).
    assert walk["text"].startswith("Walk 4 min to Borough Hall for the 2/4 (next trains in")
    # A nearby station without any predicted train (line not running) is not suggested.
    assert "walk_line:NODATA" not in set(recs["option_id"])
    assert walk["confidence"] == "high"
    # Every number in every text is one of its cited metrics.
    for r in recs.itertuples():
        assert uncited_numbers(r.text, json.loads(r.metrics)) == [], r.text
    assert set(recs["option_type"]) == {"walk_line", "bike", "bus", "wait"}
    assert "bus:BM5" not in set(recs["option_id"])


def test_wait_option_uses_the_recovery_history(tmp_path):
    recs = run(tmp_path)
    wait = recs[recs["option_type"] == "wait"].iloc[0]
    # Past durations 10..50, elapsed 12 min -> remaining of 20, 30, 40, 50 = 8, 18, 28, 38.
    assert wait["est_wait_min"] == 23.0
    assert "about 23 more min expected" in wait["text"]
    assert "5 past events, A line" in wait["text"]
    facts = json.loads(wait["inputs"])["facts"]
    assert facts["cause"] == "signal"


def test_bus_slowdown_and_bike_depletion_are_reported(tmp_path):
    recs = run(tmp_path)
    b67 = recs[recs["option_id"] == "bus:B67"].iloc[0]
    assert "buses nearby are running 50% slower than usual" in b67["text"]
    assert b67["confidence"] == "medium"
    b25 = recs[recs["option_id"] == "bus:B25"].iloc[0]
    assert "no live bus speed data" in b25["text"] and b25["confidence"] == "low"
    bike = recs[recs["option_type"] == "bike"].iloc[0]
    assert bike["target_name"] == "Jay St & Willoughby"  # nearest dock with bikes
    assert "bikes nearby are down 50% in the last 15 min" in bike["text"]


def test_crowded_obvious_option_is_penalized_and_flagged(tmp_path):
    # No other line within reach: the bike is the obvious option under normal
    # conditions, and nearby bikes dropped 50% in 15 minutes (threshold 30%).
    settings = S.with_overrides(rec_walk_max_m=-1.0, rec_crowd_penalty_min=10.0)
    recs = run(tmp_path, settings=settings)
    bike = recs[recs["option_type"] == "bike"].iloc[0]
    assert bike["is_obvious"] and bike["crowded"]
    assert list(bike["crowd_reasons"]) == ["bike_depletion"]
    assert bike["penalty_min"] == 10.0
    assert bike["est_total_min"] == pytest.approx(bike["normal_total_min"] + 10.0, abs=0.1)
    assert "Likely crowded: nearby bikes are running out" in bike["text"]
    # Without the penalty the bike would rank first; now the B25 bus beats it.
    assert recs.iloc[0]["option_id"] == "bus:B25"

    calm = run(tmp_path / "calm", settings=settings, bike_then=5)
    calm_bike = calm[calm["option_type"] == "bike"].iloc[0]
    assert calm_bike["is_obvious"] and not calm_bike["crowded"] and calm_bike["rank"] == 1


def test_no_history_and_no_ongoing_events(tmp_path):
    recs = run(tmp_path, past_events=False)
    wait = recs[recs["option_type"] == "wait"].iloc[0]
    assert pd.isna(wait["est_total_min"]) and wait["rank"] == len(recs)  # unknown: last
    assert "no past events yet" in wait["text"]
    assert wait["confidence"] == "none"
    later = NOW + pd.Timedelta(hours=2)  # the event version is too old to act on
    empty = recommend(
        connect(tmp_path), S.with_overrides(data_dir=tmp_path), pd.DataFrame(), later, "r"
    )
    assert empty.empty


def test_recommendations_are_logged_with_their_inputs(tmp_path):
    recs = run(tmp_path)
    write_table(tmp_path, "recommendations", recs, "run1")
    stored = connect(tmp_path).execute("SELECT * FROM recommendations ORDER BY rank").df()
    assert len(stored) == len(recs)
    inputs = json.loads(stored.iloc[0]["inputs"])
    assert set(inputs) == {"facts", "data", "rule"}
    assert inputs["rule"]["rec_reference_trip_km"] == 4.0
    assert stored["computed_utc"].iloc[0] == NOW


def test_walking_and_riding_times():
    assert walk_minutes(600, S) == pytest.approx(600 * 1.3 / 1.3 / 60)
    assert ride_minutes(8.0, S) == pytest.approx(4000 / (8 * 0.44704) / 60)
    assert ride_minutes(0, S) is None
