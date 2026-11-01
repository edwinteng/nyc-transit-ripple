import json

import pandas as pd
import pytest

from transit_ripple.config import Settings
from transit_ripple.explain import explain, uncited_numbers
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
    }
    base.update(overrides)
    return base


def test_summary_states_the_metrics_and_cites_every_number():
    summary, cited = explain(event())
    assert summary.startswith(
        "2 train delays: from 09:00 to 09:35 ET on Mon Nov 30 (35 min), the median train delay "
        "on the 2 line was at least 5.0 min, peaking at 14.9 min at 09:12 (northbound)."
    )
    assert "Nevins St (up to 25.5 min late)" in summary
    assert "averaged 18.2 mph, 22% below the usual 23.4 mph" in summary
    assert (
        "About 412 Citi Bikes were available at 20 station(s) within 500 m, 73 (15%) fewer"
        in summary
    )
    assert "bike baseline(s) rest on little history" in summary
    # Alert text is quoted verbatim (whitespace normalized, inner quotes made single).
    assert "(Delays): \"Northbound [2][3] trains are delayed while 'we' address" in summary
    assert "1 planned-work notice(s)" in summary
    assert cited["traffic_speed_mph"] == "18.2" and cited["bikes_change_abs"] == "73"
    assert uncited_numbers(summary, cited) == []


def test_uncited_numbers_catches_invented_values():
    summary, cited = explain(event())
    assert uncited_numbers(summary + " Delays will last 45 min.", cited) == ["45"]
    assert uncited_numbers(summary + ' "quoted 99"', cited) == []  # alert quotes are exempt


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {"status": "ongoing", "end_reason": "ongoing"},
            "are ongoing: since 09:00 ET on Mon Nov 30 (35 min so far)",
        ),
        ({"end_reason": "data_gap"}, "(end time uncertain: subway data stopped)"),
        ({"traffic_n_links": 0, "traffic_speed_mph": None}, "No fresh DOT traffic speed data"),
        (
            {"traffic_baseline_mph": None, "traffic_change_pct": None},
            "there is no baseline for this weekday",
        ),
        ({"bikes_n_stations": 0, "bikes_available": None}, "No fresh Citi Bike availability data"),
        (
            {"affected_station_names": [], "affected_station_max_delay_min": []},
            "No station-level arrival",
        ),
        ({"linked_alerts": []}, "No MTA service alert matched this line and time."),
    ],
)
def test_summary_variants_never_invent_numbers(overrides, expected):
    summary, cited = explain(event(**overrides))
    assert expected in summary
    assert uncited_numbers(summary, cited) == []


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
