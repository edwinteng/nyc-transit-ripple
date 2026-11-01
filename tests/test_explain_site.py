import json

import pandas as pd
import pytest

from transit_ripple.config import Settings
from transit_ripple.explain import (
    _num,
    explain,
    explain_sentences,
    sentences_of,
    uncited_numbers,
)
from transit_ripple.pipeline import station_minute
from transit_ripple.site import build_site, render_event_chart
from transit_ripple.storage import connect, write_table

T0 = pd.Timestamp("2026-11-30 14:00")  # Monday 09:00 EST


def event(**overrides):
    base = {
        "event_id": "2-20261130T1400Z",
        "route_id": "2",
        "status": "ended",
        "end_reason": "recovered",
        "start_utc": T0,
        "end_utc": T0 + pd.Timedelta(minutes=35),
        "computed_utc": T0 + pd.Timedelta(minutes=40),
        "duration_min": 35.0,
        "metric": "delay_median_min",
        "threshold_min": 5.0,
        "peak_delay_min": 14.87,
        "peak_utc": T0 + pd.Timedelta(minutes=12),
        "peak_direction": "N",
        "affected_station_names": [
            "Nevins St",
            "Atlantic Av-Barclays Ctr",
            "149 St-Grand Concourse",
        ],
        "affected_station_max_delay_min": [25.5, 19.2, 7.04],
        "focal_station_ids": ["234", "235"],
        "traffic_n_links": 2,
        "traffic_speed_mph": 18.2,
        "traffic_baseline_mph": 23.4,
        "traffic_change_pct": -22.2,
        "traffic_baseline_confidence": "ok",
        "bikes_n_stations": 20,
        "bikes_available": 412.0,
        "bikes_baseline": 485.0,
        "bikes_change": -73.0,
        "bikes_change_pct": -15.1,
        "bikes_baseline_confidence": "low",
        "linked_alerts": [
            {
                "alert_id": "x",
                "alert_type": "Delays",
                "is_planned": False,
                "header_text": (
                    'Northbound [2][3] trains are delayed while\n"we" address a signal problem '
                    "at 96 St."
                ),
                "n_matching_stations": 1,
            },
            {
                "alert_id": "y",
                "alert_type": "Planned - Reroute",
                "is_planned": True,
                "header_text": "Planned work",
                "n_matching_stations": 0,
            },
        ],
        "neighbor_radius_m": 500.0,
        "camera_n_cameras": 0,
        "camera_objects": None,
        "camera_change_pct": None,
        "impact_score": 55.5,
        "impact_signals_used": ["traffic", "bikes"],
        "surface_recovery_status": "recovered",
        "surface_recovered_utc": T0 + pd.Timedelta(minutes=50),
        "surface_checked_until_utc": T0 + pd.Timedelta(minutes=80),
    }
    base.update(overrides)
    return base


def check_sentences(ev: dict) -> list[dict]:
    """Every sentence cites exactly the numbers it prints, and every cited value is used."""
    sentences = explain_sentences(ev)
    assert 1 <= len(sentences) <= 2
    for sentence in sentences:
        assert uncited_numbers(sentence["text"], sentence["metrics"]) == []
        for value in sentence["metrics"].values():
            assert value in sentence["text"]
    return sentences


def test_two_sentences_with_the_metrics_they_use():
    sentences = check_sentences(event())
    assert sentences[0]["text"] == (
        "2 trains ran a median of up to 14.9 min late for 35 min, from 09:00 to 09:35 ET "
        "on Mon Nov 30, worst at Nevins St (25.5 min)."
    )
    assert sentences[0]["metrics"] == {
        "route_id": "2",
        "date_local": "Mon Nov 30",
        "start_local": "09:00",
        "duration_min": "35",
        "peak_delay_min": "14.9",
        "end_local": "09:35",
        "station_0_name": "Nevins St",
        "station_0_max_delay_min": "25.5",
    }
    assert sentences[1]["text"] == (
        "Within 500 m of the worst-hit stations, nearby traffic was 22% slower and Citi Bike "
        "availability was 15% lower than usual for this weekday and time (surface impact "
        "56/100), and the surface had recovered by 09:50 ET."
    )
    assert sentences[1]["metrics"] == {
        "radius_m": "500",
        "traffic_change_pct_abs": "22",
        "bikes_change_pct_abs": "15",
        "impact_score": "56",
        "impact_scale": "100",
        "surface_recovered_local": "09:50",
    }
    summary, cited = explain(event())
    assert summary == " ".join(s["text"] for s in sentences)
    assert uncited_numbers(summary, cited) == []


def test_uncited_numbers_catches_invented_values():
    summary, cited = explain(event())
    assert uncited_numbers(summary + " Delays will last 45 min.", cited) == ["45"]
    assert uncited_numbers(summary + ' "quoted 99"', cited) == []  # quoted text is exempt


def test_numbers_round_half_up_like_the_dashboard():
    assert _num(62.5, 0) == "63" and _num(14.85, 1) == "14.9" and _num(-0.04, 1) == "0.0"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {"status": "ongoing", "end_reason": "ongoing"},
            "2 trains have been running a median of up to 14.9 min late for 35 min, "
            "since 09:00 ET on Mon Nov 30, worst at Nevins St (25.5 min).",
        ),
        ({"end_reason": "data_gap"}, "(the end time is uncertain because subway data stopped)"),
        ({"peak_delay_min": None}, "2 trains ran at least 5.0 min late for 35 min"),
        (
            {"affected_station_names": [], "affected_station_max_delay_min": []},
            "from 09:00 to 09:35 ET on Mon Nov 30. Within",
        ),
        (
            {"impact_signals_used": ["camera"], "camera_change_pct": 40.0, "impact_score": 40.0},
            "cameras counted 40% more vehicles and people than usual",
        ),
        (
            {
                "impact_signals_used": ["traffic"],
                "traffic_change_pct": 10.0,
                "impact_score": 0.0,
                "surface_recovery_status": "no_impact",
            },
            "nearby traffic was 10% faster than usual for this weekday and time (surface impact "
            "0/100), so surface conditions stayed close to normal.",
        ),
        (
            {
                "surface_recovery_status": "not_yet",
                "surface_recovered_utc": None,
                "surface_checked_until_utc": T0 + pd.Timedelta(minutes=60),
            },
            "and it had not recovered by 10:00 ET.",
        ),
        (
            {"impact_signals_used": [], "impact_score": None},
            "Within 500 m, traffic averaged 18.2 mph and 412 Citi Bikes were available, but "
            "there is not enough baseline history yet to compare them with usual conditions",
        ),
        (
            {
                "impact_signals_used": [],
                "impact_score": None,
                "camera_n_cameras": 3,
                "camera_objects": 41.6,
                "traffic_n_links": 0,
                "bikes_n_stations": 0,
            },
            "Within 500 m, 3 camera(s) counted about 42 vehicles and people, but",
        ),
        (
            {
                "impact_signals_used": [],
                "impact_score": None,
                "traffic_n_links": 0,
                "bikes_n_stations": 0,
            },
            "No fresh traffic, camera or Citi Bike data was available within 500 m.",
        ),
    ],
)
def test_sentence_variants_never_invent_numbers(overrides, expected):
    sentences = check_sentences(event(**overrides))
    assert expected in " ".join(s["text"] for s in sentences)


def test_sentences_of_prefers_the_stored_explanation():
    stored = [{"text": "Stored.", "metrics": {}}]
    assert sentences_of(event(explanation=json.dumps(stored))) == stored
    assert sentences_of(event()) == explain_sentences(event())


def test_station_minute_aggregates_observed_arrivals_only():
    t = T0
    sd = pd.DataFrame(
        {
            "bucket_utc": [t, t, t, t],
            "observed_utc": [t] * 4,
            "feed_id": ["numbered"] * 4,
            "route_id": ["2"] * 4,
            "direction": ["N"] * 4,
            "station_id": ["234", "234", "235", "236"],
            "stop_name": ["Nevins St", "Nevins St", "Atlantic Av", "Bergen St"],
            "trip_id": ["a", "b", "a", "c"],
            "delay_min": [10.0, 4.0, 3.0, 50.0],
            "time_kind": ["actual", "actual", "actual", "predicted"],
        }
    )
    out = station_minute(sd, "r").set_index("station_id")
    assert list(out.index) == ["234", "235"]
    assert (
        out.loc["234", "n_trains"],
        out.loc["234", "delay_mean_min"],
        out.loc["234", "delay_max_min"],
    ) == (2, 7.0, 10.0)
    assert station_minute(pd.DataFrame(), "r").empty


def chart_series():
    minutes = pd.date_range(T0 - pd.Timedelta(minutes=5), T0 + pd.Timedelta(minutes=40), freq="min")
    n = len(minutes)
    return pd.DataFrame(
        {
            "minute_utc": minutes,
            "value": [2.0] * 5 + [9.0] * 35 + [2.0] * (n - 40),
            "fill_status": ["observed", "filled"] * (n // 2) + ["observed"] * (n % 2),
            "traffic_speed_mph": [18.0] * n,
            "traffic_baseline_mph": [23.0] * n,
            "bikes_available": [None] * n,
            "bikes_baseline": [None] * n,
        }
    )


def test_render_event_chart_writes_png(tmp_path):
    path = render_event_chart(event(), chart_series(), tmp_path / "c" / "e.png")
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 10_000


def test_build_site_lists_events_escapes_text_and_prunes_old_charts(tmp_path):
    settings = Settings(data_dir=tmp_path)
    ev = event(
        summary="Delay <b>x</b> & more",
        cited_metrics=json.dumps({"peak": "14.9"}),
        bucket_utc=T0,
        run_id="r",
        n_linked_alerts=2,
        linked_alerts=[
            {
                "alert_id": "x",
                "alert_type": "Delays",
                "is_planned": False,
                "header_text": "<script>alert(1)</script>",
                "n_matching_stations": 0,
            }
        ],
        directions=["N"],
        affected_station_ids=["234"],
    )
    write_table(tmp_path, "events", pd.DataFrame([ev]), "r")
    out = tmp_path / "site"
    (out / "charts").mkdir(parents=True)
    (out / "charts" / "old-event.png").write_bytes(b"old")

    index = build_site(
        connect(tmp_path), settings, pd.DataFrame(columns=["metric"]), out, T0.to_pydatetime()
    )
    page = index.read_text()
    assert "Delay &lt;b&gt;x&lt;/b&gt; &amp; more" in page
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert 'src="charts/2-20261130T1400Z.png"' in page
    assert (out / "charts" / "2-20261130T1400Z.png").exists()
    assert not (out / "charts" / "old-event.png").exists()


def test_build_site_without_events(tmp_path):
    index = build_site(
        connect(tmp_path),
        Settings(data_dir=tmp_path),
        pd.DataFrame(columns=["metric"]),
        tmp_path / "site",
        T0.to_pydatetime(),
    )
    assert "No delay events detected yet." in index.read_text()
