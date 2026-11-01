"""M6 evaluation framework: event matching, placebo comparison, recommendation replay,
explanation consistency and the report."""

import json
import random

import pandas as pd
import pytest

from transit_ripple.cli import main
from transit_ripple.config import Settings
from transit_ripple.evaluation import build_report, explanation_violations, to_markdown
from transit_ripple.evaluation.common import bootstrap_mean_ci
from transit_ripple.evaluation.detection import evaluate_detection, match_events_to_alerts
from transit_ripple.evaluation.explanations import check_sentences, find_violations
from transit_ripple.evaluation.placebo import compare, evaluate_placebo
from transit_ripple.evaluation.replay import realized_outcomes, top_vs_obvious
from transit_ripple.explain import explain_sentences
from transit_ripple.recommend import Option, option_text
from transit_ripple.storage import connect, write_reference, write_table

T0 = pd.Timestamp("2026-11-30 14:00")  # Monday 09:00 New York time
S = Settings(eval_bootstrap_samples=500, eval_min_sample=30)


# --------------------------------------------------------------------------------------
# Event detection vs. MTA alerts
# --------------------------------------------------------------------------------------


def ev(eid, route, start_min, end_min):
    return {
        "bucket_utc": T0 + pd.Timedelta(minutes=start_min),
        "event_id": eid,
        "route_id": route,
        "start_utc": T0 + pd.Timedelta(minutes=start_min),
        "end_utc": T0 + pd.Timedelta(minutes=end_min),
    }


def al(aid, route, minute):
    return {"alert_id": aid, "route_id": route, "alert_utc": T0 + pd.Timedelta(minutes=minute)}


def test_matching_is_one_to_one_within_the_tolerance():
    events = pd.DataFrame(
        [
            ev("A-1", "A", 0, 30),  # alert at -5: detected 5 min after the alert
            ev("A-2", "A", 40, 50),  # the same alert is already used; a later one fits
            ev("C-1", "C", 0, 10),  # no alert for C
            ev("2-1", "2", 100, 110),  # alert 2 h before: outside the tolerance
        ]
    )
    alerts = pd.DataFrame([al("x", "A", -5), al("y", "A", 52), al("z", "2", -20), al("w", "Q", 0)])
    m = match_events_to_alerts(events, alerts, tolerance_min=15).set_index("event_id")
    assert m.loc["A-1", "alert_id"] == "x" and m.loc["A-1", "time_to_detect_min"] == 5.0
    assert m.loc["A-2", "alert_id"] == "y" and m.loc["A-2", "time_to_detect_min"] == -12.0
    assert set(m.index) == {"A-1", "A-2"}
    assert match_events_to_alerts(events.head(0), alerts, 15).empty


def alert_store(tmp_path):
    def alert(aid, routes, minute, planned=False):
        t = T0 + pd.Timedelta(minutes=minute)
        return {
            "bucket_utc": t,
            "observed_utc": t,
            "fetched_utc": t,
            "run_id": "r",
            "alert_id": aid,
            "alert_type": "Delays",
            "is_planned": planned,
            "created_utc": t,
            "updated_utc": t,
            "route_ids": routes,
            "station_ids": [],
            "header_text": "Delays",
        }

    write_table(
        tmp_path,
        "alerts",
        pd.DataFrame(
            [
                alert("x", ["A", "C"], -5),  # A matches; C has no event -> a recall miss
                alert("p", ["A"], 1, planned=True),  # planned work: not ground truth
                alert("old", ["F"], 60),  # F not observed after the alert: excluded
            ]
        ),
        "r",
    )
    lm = [
        {
            "bucket_utc": T0 + pd.Timedelta(minutes=i),
            "fetched_utc": T0,
            "run_id": "r",
            "feed_id": "ace",
            "route_id": r,
            "direction": "S",
            "status": "ok",
        }
        for i in range(-10, 40)
        for r in ("A", "C")
    ]
    write_table(tmp_path, "line_minute", pd.DataFrame(lm), "r")
    events = [
        {**ev("A-1", "A", 0, 30), "computed_utc": T0, "run_id": "r", "status": "ended"},
        {**ev("E-1", "E", 0, 8), "computed_utc": T0, "run_id": "r", "status": "ended"},
    ]
    write_table(tmp_path, "events", pd.DataFrame(events), "r")


def test_detection_section_reports_precision_recall_and_unmatched(tmp_path):
    alert_store(tmp_path)
    sec = evaluate_detection(
        connect(tmp_path), S, T0 - pd.Timedelta(days=1), T0 + pd.Timedelta(days=1)
    )
    m = {x.key: x for x in sec.metrics}
    assert (m["precision"].value, m["precision"].n) == (0.5, 2)
    assert (m["recall"].value, m["recall"].n) == (0.5, 2)  # A and C; F was not observable
    assert m["ttd_median"].value == 5.0
    assert [r["event_id"] for r in sec.tables["unmatched_events"]] == ["E-1"]
    assert any("imperfect ground truth" in n for n in sec.notes)
    assert any("left out of recall" in n for n in sec.notes)
    d = sec.to_dict(min_sample=30)
    assert all(x["preliminary"] for x in d["metrics"])  # tiny samples are flagged


# --------------------------------------------------------------------------------------
# Placebo comparison
# --------------------------------------------------------------------------------------


def test_bootstrap_and_placebo_comparison_report_significance_honestly():
    mean, lo, hi = bootstrap_mean_ci([-20.0, -18.0, -25.0, -22.0, -19.0], 500, 1)
    assert mean == pytest.approx(-20.8) and lo < mean < hi < 0
    assert compare([-20.0, -18.0, -25.0, -22.0, -19.0], S)["significant"]
    noisy = compare([-10.0, 12.0, -3.0, 8.0, -5.0, 4.0], S)
    assert noisy["ci_low"] < 0 < noisy["ci_high"] and not noisy["significant"]
    one = compare([-30.0], S)
    assert one["n"] == 1 and not one["significant"] and one["ci_low"] is None


def placebo_store(tmp_path, n_events=3):
    """Traffic near station A41 is 30% slower during each event than on the same
    weekday a week earlier; the control station far away does not change."""
    write_reference(
        tmp_path,
        "subway_stations",
        pd.DataFrame(
            {
                "station_id": ["A41", "FAR"],
                "station_name": ["Jay St", "Far Away"],
                "lat": [40.69, 40.85],
                "lon": [-73.99, -73.80],
                "routes": [["A"], ["7"]],
            }
        ),
    )
    write_reference(
        tmp_path,
        "station_neighbors",
        pd.DataFrame(
            {
                "station_id": ["A41", "FAR"],
                "neighbor_type": ["traffic_link", "traffic_link"],
                "neighbor_id": ["L1", "L9"],
                "distance_m": [100.0, 100.0],
            }
        ),
    )
    rows, events = [], []
    for k in range(n_events):
        start = T0 + pd.Timedelta(days=k + 1)  # Tuesday to Thursday
        events.append(
            {
                **ev(f"A-{k}", "A", 0, 20),
                "bucket_utc": start,
                "start_utc": start,
                "end_utc": start + pd.Timedelta(minutes=20),
                "computed_utc": start,
                "run_id": "r",
                "status": "ended",
                "focal_station_ids": ["A41"],
            }
        )
        for day_offset, speed in ((0, 14.0), (7, 20.0)):  # event day, a week earlier
            for m in range(0, 20, 2):
                t = start - pd.Timedelta(days=day_offset) + pd.Timedelta(minutes=m)
                for link, v in (("L1", speed), ("L9", 30.0)):
                    rows.append(
                        {
                            "bucket_utc": t,
                            "observed_utc": t,
                            "fetched_utc": t,
                            "run_id": "r",
                            "link_id": link,
                            "speed_mph": v,
                            "is_valid": True,
                        }
                    )
    write_table(tmp_path, "traffic_minute", pd.DataFrame(rows), "r")
    write_table(tmp_path, "events", pd.DataFrame(events), "r")


def test_placebo_detects_a_consistent_slowdown(tmp_path):
    placebo_store(tmp_path)
    settings = S.with_overrides(eval_placebo_days_back=7, eval_control_min_distance_m=1000)
    sec = evaluate_placebo(connect(tmp_path), settings, T0, T0 + pd.Timedelta(days=30))
    m = {x.key: x for x in sec.metrics}
    same = m["traffic_speed_diff_same_station_pct"]
    assert same.n == 3 and same.value == pytest.approx(-30.0)
    assert same.note.startswith("significant") is False or same.ci_low <= -30.0
    ctrl = m["traffic_speed_diff_vs_controls_pct"]
    assert ctrl.n == 3 and ctrl.value == pytest.approx(-30.0)  # controls: ratio 1.0
    assert m["bus_speed_diff_same_station_pct"].n == 0  # no bus data: no claim
    rows = sec.tables["per_event"]
    assert rows[0]["event_value"] == 14.0 and rows[0]["matched_value"] == 20.0


def test_placebo_without_history_is_insufficient(tmp_path):
    placebo_store(tmp_path, n_events=1)
    sec = evaluate_placebo(connect(tmp_path), S, T0, T0 + pd.Timedelta(days=30))
    assert sec.status == "insufficient_data"
    assert "no conclusion" in sec.summary


# --------------------------------------------------------------------------------------
# Recommendation replay
# --------------------------------------------------------------------------------------


def rec(rank, option_type, option_id, total, **kw):
    base = {
        "run_id": "r1",
        "event_id": "A-1",
        "station_id": "A41",
        "computed_utc": T0,
        "rank": rank,
        "option_type": option_type,
        "option_id": option_id,
        "target_id": None,
        "routes": [],
        "est_total_min": total,
        "est_access_min": 2.0,
        "est_wait_min": 3.0,
        "est_ride_min": 8.8,
        "penalty_min": 0.0,
        "normal_total_min": total,
        "is_obvious": False,
        "crowded": False,
        "inputs": json.dumps({"facts": {}}),
    }
    base.update(kw)
    return base


def replay_store(tmp_path):
    write_table(
        tmp_path,
        "events",
        pd.DataFrame(
            [
                {
                    **ev("A-1", "A", -10, 25),
                    "computed_utc": T0 + pd.Timedelta(minutes=30),
                    "run_id": "r",
                    "status": "ended",
                    "end_reason": "recovered",
                }
            ]
        ),
        "r",
    )
    # The C arrives at A41 7 min after the recommendation (5 min after the 2 min walk).
    write_table(
        tmp_path,
        "station_minute",
        pd.DataFrame(
            {
                "bucket_utc": [T0 + pd.Timedelta(minutes=7)],
                "observed_utc": [T0 + pd.Timedelta(minutes=7)],
                "run_id": ["r"],
                "route_id": ["C"],
                "direction": ["N"],
                "station_id": ["A41"],
                "n_trains": [1],
            }
        ),
        "r",
    )
    write_reference(
        tmp_path,
        "station_neighbors",
        pd.DataFrame(
            {
                "station_id": ["A41"],
                "neighbor_type": ["bus_stop"],
                "neighbor_id": ["s1"],
                "distance_m": [80.0],
            }
        ),
    )
    # Buses at 4 mph during the following half hour (usual 8 mph).
    write_table(
        tmp_path,
        "bus_minute",
        pd.DataFrame(
            {
                "bucket_utc": [T0 + pd.Timedelta(minutes=5)],
                "run_id": ["r"],
                "segment_id": ["B67|0|s1"],
                "route_id": ["B67"],
                "stop_id": ["s1"],
                "n_samples": [2],
                "distance_m": [4 * 0.44704 * 120],
                "duration_s": [120.0],
                "speed_mph": [4.0],
            }
        ),
        "r",
    )


def test_replay_computes_realized_outcomes_per_option_type(tmp_path):
    replay_store(tmp_path)
    recs = pd.DataFrame(
        [
            rec(1, "walk_line", "walk_line:A41", 13.8, target_id="A41", routes=["C"]),
            rec(
                2,
                "bus",
                "bus:B67",
                26.0,
                routes=["B67"],
                est_wait_min=6.0,
                est_ride_min=4000 / (8 * 0.44704) / 60,  # assumed 8 mph
                is_obvious=True,
                crowded=True,
                inputs=json.dumps({"facts": {"baseline_mph": 8.0}}),
            ),
            rec(3, "wait", "wait:A", 30.0, est_access_min=0.0, est_wait_min=20.0),
        ]
    )
    out = realized_outcomes(connect(tmp_path), S, recs).set_index("option_type")
    assert out.loc["walk_line", "realized_wait_min"] == pytest.approx(5.0)
    assert out.loc["walk_line", "realized_total_min"] == pytest.approx(2 + 5 + 8.8)
    assert out.loc["wait", "realized_wait_min"] == pytest.approx(25.0)  # ended at +25 min
    assert out.loc["bus", "realized_speed_mph"] == pytest.approx(4.0)
    assert out.loc["bus", "estimated_speed_mph"] == pytest.approx(8.0)
    assert bool(out.loc["bus", "degraded"])  # 50% below usual: the crowd flag was right
    tvo = top_vs_obvious(out.reset_index())
    assert tvo == {"beat": 1, "lost": 0, "same_option": 0, "unknown": 0}


def test_replay_section_reports_mae_and_crowd_check(tmp_path):
    replay_store(tmp_path)
    recs = [
        {
            **rec(1, "walk_line", "walk_line:A41", 13.8, target_id="A41", routes=["C"]),
            "bucket_utc": T0,
            "text": "x",
            "metrics": "{}",
        },
        {
            **rec(2, "wait", "wait:A", 30.0, est_wait_min=20.0, is_obvious=True),
            "bucket_utc": T0,
            "text": "y",
        },
    ]
    write_table(tmp_path, "recommendations", pd.DataFrame(recs), "r1")
    report = build_report(
        connect(tmp_path), S, T0 + pd.Timedelta(days=1), root=tmp_path, github=False
    )
    sec = next(s for s in report["sections"] if s["key"] == "recommendations")
    m = {x["key"]: x for x in sec["metrics"]}
    assert m["mae_walk_line"]["value"] == pytest.approx(2.0) and m["mae_walk_line"]["n"] == 1
    assert m["mae_wait"]["value"] == pytest.approx(5.0)
    assert m["top_beats_obvious"]["n"] == 1 and m["top_beats_obvious"]["preliminary"]


# --------------------------------------------------------------------------------------
# Explanation consistency checker
# --------------------------------------------------------------------------------------


def test_checker_flags_numbers_not_in_the_metrics(tmp_path):
    ok = {"text": "A trains ran 12.0 min late.", "metrics": {"route": "A", "delay": "12.0"}}
    bad = {"text": "A trains ran 12.0 min late for 45 min.", "metrics": {"delay": "12.0"}}
    assert check_sentences([ok]) == []
    assert check_sentences([ok, bad])[0]["uncited"] == ["45"]
    write_table(
        tmp_path,
        "recommendations",
        pd.DataFrame(
            [
                {
                    **rec(1, "bike", "bike:b1", 20.0),
                    "bucket_utc": T0,
                    "text": "Take a bike (3 min walk).",
                    "metrics": json.dumps({"walk_min": "2"}),
                }
            ]
        ),
        "r",
    )
    violations, n_sent, n_rec = find_violations(connect(tmp_path), T0, T0 + pd.Timedelta(days=1))
    assert (len(violations), n_sent, n_rec) == (1, 0, 1)
    assert violations[0]["uncited"] == ["3"]
    code = main(
        [
            "evaluate",
            "--data-dir",
            str(tmp_path),
            "--now",
            (T0 + pd.Timedelta(days=1)).isoformat(),
            "--no-github",
            "--check-explanations",
        ]
    )
    assert code == 1


def random_event(rng: random.Random) -> dict:
    def maybe(v):
        return None if rng.random() < 0.2 else v

    start = T0 + pd.Timedelta(minutes=rng.randint(0, 5000))
    dur = rng.uniform(5, 180)
    used = [s for s in ("traffic", "bus", "camera", "bikes") if rng.random() < 0.5]
    return {
        "route_id": rng.choice(["A", "2", "6X", "GS", "L"]),
        "status": rng.choice(["ongoing", "ended"]),
        "end_reason": rng.choice(["recovered", "data_gap", "ongoing"]),
        "start_utc": start,
        "end_utc": start + pd.Timedelta(minutes=dur),
        "duration_min": dur,
        "threshold_min": 5.0,
        "peak_delay_min": maybe(rng.uniform(5, 60)),
        "affected_station_names": rng.choice([[], ["34 St-Penn Station", "14 St"]]),
        "affected_station_max_delay_min": [rng.uniform(0, 90), rng.uniform(0, 90)],
        "neighbor_radius_m": 500.0,
        "traffic_n_links": rng.randint(0, 3),
        "traffic_speed_mph": maybe(rng.uniform(1, 50)),
        "traffic_change_pct": maybe(rng.uniform(-80, 40)),
        "bus_n_segments": rng.randint(0, 3),
        "bus_speed_mph": maybe(rng.uniform(1, 20)),
        "bus_change_pct": maybe(rng.uniform(-80, 40)),
        "camera_n_cameras": rng.randint(0, 3),
        "camera_objects": maybe(rng.uniform(0, 80)),
        "camera_change_pct": maybe(rng.uniform(-50, 200)),
        "bikes_n_stations": rng.randint(0, 5),
        "bikes_available": maybe(rng.uniform(0, 300)),
        "bikes_change_pct": maybe(rng.uniform(-90, 50)),
        "impact_score": maybe(rng.uniform(0, 100)),
        "impact_signals_used": used,
        "surface_recovery_status": rng.choice(["recovered", "not_yet", "no_impact", "unknown"]),
        "surface_recovered_utc": maybe(start + pd.Timedelta(minutes=dur + 15)),
        "surface_checked_until_utc": maybe(start + pd.Timedelta(minutes=dur + 60)),
    }


def random_option(rng: random.Random) -> Option:
    kind = rng.choice(["walk_line", "bike", "bus", "wait"])
    waits = {d: rng.uniform(0, 30) for d in rng.sample(["N", "S"], rng.randint(0, 2))}
    facts = {
        "same_station": rng.random() < 0.3,
        "waits_by_direction": waits,
        "walk_min": rng.uniform(1, 15),
        "dock_bikes": rng.randint(2, 40),
        "bikes_drop_pct": rng.choice([None, rng.uniform(-20, 90)]),
        "window_min": 15.0,
        "speed_mph": rng.uniform(2, 15),
        "speed_change_pct": rng.choice([None, rng.uniform(-70, 30)]),
        "live_speed": rng.random() < 0.5,
        "estimate": {
            "duration": {
                "n_events": rng.randint(0, 40),
                "median_min": rng.uniform(5, 90),
                "group": rng.choice(["A line, morning rush", "all lines", "7 line"]),
            }
        },
    }
    o = Option(
        kind,
        f"{kind}:x",
        "x",
        rng.choice(["34 St-Herald Sq", "Jay St & Willoughby", "5 AV/W 41 ST"]),
        [rng.choice(["B", "D", "6X", "H", "B63"])],
        rng.uniform(0, 15),
        rng.choice([None, rng.uniform(0, 40)]),
        rng.uniform(5, 40),
        rng.uniform(5, 40),
        "low",
        facts=facts,
    )
    if rng.random() < 0.3:
        o.crowded, o.crowd_reasons, o.penalty_min = True, ["bike_depletion"], 5.0
    return o


def test_every_template_variant_cites_every_number():
    """CI guard: random inputs through every explanation and recommendation template."""
    rng = random.Random(20261101)
    violations = []
    for _ in range(400):
        for s in explain_sentences(random_event(rng)):
            violations += check_sentences([s])
        violations += check_sentences([option_text(random_option(rng), S, {})])
    assert violations == []


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


def test_report_on_an_empty_store_says_so_and_marks_small_samples(tmp_path):
    report = build_report(connect(tmp_path), S, T0, root=tmp_path, github=False)
    keys = [s["key"] for s in report["sections"]]
    assert keys == [
        "detection",
        "placebo",
        "cameras",
        "recommendations",
        "explanations",
        "reliability",
    ]
    by = {s["key"]: s for s in report["sections"]}
    assert by["cameras"]["status"] == "skipped" and "No manual labels" in by["cameras"]["summary"]
    assert by["recommendations"]["status"] == "insufficient_data"
    assert report["date_from"] == "2026-10-31" and report["date_to"] == "2026-11-30"
    assert explanation_violations(report) == 0
    json.dumps(report, allow_nan=False)  # strict JSON for the site
    md = to_markdown(report)
    assert "# Evaluation report" in md and "| Metric | Value | 95% interval | n | Note |" in md
    assert "preliminary" in md
