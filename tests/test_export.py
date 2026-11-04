"""Dashboard JSON export: schema, strict JSON (no NaN), size and downsampling."""

import json
import math

import pandas as pd
import pytest

from test_events import T0, line_rows, write_context
from transit_ripple.config import Settings
from transit_ripple.events import process_events
from transit_ripple.export import (
    build_status,
    downsample,
    export_site_data,
    impact_label,
    iso,
    num,
)
from transit_ripple.storage import connect, read_reference, write_reference, write_table

STATUSES = {"ok", "stale", "degraded", "missing", "on_demand", "building", "unavailable"}
ISO_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


def strict_load(path):
    """json.loads that rejects NaN / Infinity (browsers cannot parse them)."""

    def refuse(token):
        raise ValueError(f"non-standard JSON constant {token}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=refuse)


def write_map_references(tmp_path):
    write_reference(
        tmp_path,
        "subway_stations",
        pd.DataFrame(
            {
                "station_id": ["A41", "A42", "A43", "G22"],
                "station_name": [
                    "Jay St-MetroTech",
                    "Hoyt-Schermerhorn Sts",
                    "Lafayette Av",
                    "Court Sq",
                ],
                "lat": [40.6923, 40.6884, 40.6863, 40.7471],
                "lon": [-73.9873, -73.9851, -73.9739, -73.9456],
                "routes": [["A", "C", "F"], ["A", "C", "G"], ["C"], ["G"]],
            }
        ),
    )
    write_reference(
        tmp_path,
        "traffic_links",
        pd.DataFrame(
            {
                "link_id": ["L1"],
                "link_name": ["FLATBUSH AVE EXT"],
                "lats": [[40.690 + i / 10000 for i in range(100)]],
                "lons": [[-73.980 - i / 10000 for i in range(100)]],
            }
        ),
    )
    write_reference(
        tmp_path,
        "bike_stations",
        pd.DataFrame(
            {
                "station_id": ["b1", "b2"],
                "name": ["Jay St", "Willoughby"],
                "lat": [40.69, 40.692],
                "lon": [-73.987, -73.986],
            }
        ),
    )


@pytest.fixture
def exported(tmp_path):
    write_context(tmp_path)
    write_map_references(tmp_path)
    rows = line_rows("A", "S", [8, 9, 12, 11, 10, 9, 8, 2, 1])
    write_table(tmp_path, "line_minute", pd.DataFrame(rows), "r")
    settings = Settings(
        data_dir=tmp_path,
        traffic_enabled=True,
        impact_weight_traffic=0.3,
        event_min_minutes=5,
        event_lookback_hours=2,
    )
    baselines = read_reference(tmp_path, "baselines")
    now = (T0 + pd.Timedelta(minutes=9)).to_pydatetime()
    events = process_events(connect(tmp_path), settings, baselines, now, "run1")
    write_table(tmp_path, "events", events, "run1")
    out = tmp_path / "site" / "data"
    (out / "events").mkdir(parents=True)
    (out / "events" / "old-event.json").write_text("{}")
    later = (T0 + pd.Timedelta(minutes=12)).to_pydatetime()
    sizes = export_site_data(connect(tmp_path), settings, baselines, out, later)
    return out, sizes, settings


def test_export_writes_the_documented_files_as_strict_json(exported):
    out, sizes, _ = exported
    event_file = "events/A-20261130T1400Z.json"
    assert set(sizes) == {"status.json", "stations.json", "events.json", event_file}
    for name in sizes:
        strict_load(out / name)
    assert not (out / "events" / "old-event.json").exists()  # stale details are removed
    assert sum(sizes.values()) < 100_000  # compact: no raw Parquet, rounded numbers


def test_events_json_schema(exported):
    out, _, settings = exported
    payload = strict_load(out / "events.json")
    assert payload["schema_version"] == 1 and payload["window_days"] == 30
    assert payload["impact_rule"]["impact_weight_traffic"] == settings.impact_weight_traffic
    (ev,) = payload["events"]
    expected_types = {
        "id": str,
        "route": str,
        "status": str,
        "start_utc": str,
        "end_utc": str,
        "duration_min": float,
        "peak_delay_min": float,
        "threshold_min": float,
        "impact_score": float,
        "impact_label": str,
        "impact_signals_used": list,
        "impact_signals": dict,
        "surface_recovery_status": str,
        "stations": list,
        "alerts": list,
        "summary": str,
        "sentences": list,
        "detail": str,
    }
    for key, typ in expected_types.items():
        assert isinstance(ev[key], typ), key
    assert ev["start_utc"] == "2026-11-30T14:00:00Z" and ev["end_utc"] == "2026-11-30T14:07:00Z"
    assert (ev["impact_score"], ev["impact_label"]) == (62.5, "high")
    assert ev["impact_signals_used"] == ["traffic"]
    assert set(ev["impact_signals"]) == {"traffic", "bus", "camera", "bikes"}
    assert ev["impact_signals"]["bikes"]["status"] == "low_confidence"
    assert ev["impact_signals"]["traffic"]["weight"] == 1.0
    assert ev["stations"][0] == {
        "id": "A41",
        "name": "Jay St-MetroTech",
        "max_delay_min": 12.0,
        "lat": 40.6923,
        "lon": -73.9873,
    }
    assert [a["id"] for a in ev["alerts"]] == ["unplanned", "planned"]
    assert len(ev["sentences"]) == 2
    for s in ev["sentences"]:
        assert set(s) == {"text", "metrics"} and s["text"].endswith(".")
        assert all(isinstance(v, str) for v in s["metrics"].values())
    assert ev["summary"] == " ".join(s["text"] for s in ev["sentences"])
    assert ev["detail"] == "events/A-20261130T1400Z.json"


def test_event_detail_series_and_nearby(exported):
    out, _, settings = exported
    detail = strict_load(out / "events" / "A-20261130T1400Z.json")
    series = detail["series"]
    keys = {
        "t",
        "delay_min",
        "traffic_mph",
        "traffic_base_mph",
        "bus_mph",
        "bus_base_mph",
        "camera_vehicles",
        "camera_vehicles_base",
        "camera_persons",
        "camera_persons_base",
        "bikes",
        "bikes_base",
        "impact",
    }
    assert set(series) == keys
    n = len(series["t"])
    assert 0 < n <= settings.export_max_points
    assert all(len(v) == n for v in series.values())
    assert all(pd.Series(series["t"]).str.match(ISO_RE))
    assert series["t"] == sorted(series["t"])
    i = series["t"].index("2026-11-30T14:03:00Z")
    assert (series["traffic_mph"][i], series["traffic_base_mph"][i]) == (15.0, 20.0)
    assert series["delay_min"][i] == 11.0
    assert series["camera_vehicles"][i] is None  # no camera: null, never 0
    nearby = detail["nearby"]
    assert [s["id"] for s in nearby["stations"]] == ["A41", "A42"]
    assert {b["id"] for b in nearby["bikes"]} == {"b1", "b2"}
    (link,) = nearby["links"]
    assert link["id"] == "L1" and 2 <= len(link["coords"]) <= 42  # simplified polyline
    assert link["coords"][-1] == [40.6999, -73.9899]  # the end point is kept
    assert nearby["cameras"] == []


def test_stations_json_links_recent_events(exported):
    out, _, _ = exported
    stations = {s["id"]: s for s in strict_load(out / "stations.json")["stations"]}
    assert set(stations) == {"A41", "A42", "A43", "G22"}
    assert stations["A41"]["routes"] == ["A", "C", "F"]
    link = stations["A41"]["event"]
    assert link["event_id"] == "A-20261130T1400Z" and link["impact_label"] == "high"
    assert stations["A43"]["event"] is None  # below the threshold: not affected
    assert stations["G22"]["event"] is None


def test_status_json_marks_every_source(exported, tmp_path):
    out, _, _ = exported
    status = strict_load(out / "status.json")
    assert status["generated_utc"] == "2026-11-30T14:12:00Z"
    by_name = {s["source"]: s for s in status["sources"]}
    assert set(by_name) == {
        "subway",
        "alerts",
        "traffic",
        "bike_status",
        "bus",
        "cameras",
        "baselines",
    }
    assert {s["status"] for s in status["sources"]} <= STATUSES
    for s in status["sources"]:
        assert set(s) == {
            "source",
            "label",
            "status",
            "data_utc",
            "checked_utc",
            "data_age_min",
            "detail",
        }
    # This datastore has no feed quality data and no baseline metadata.
    assert by_name["subway"]["status"] == "missing"
    assert by_name["baselines"]["status"] == "missing"
    assert by_name["cameras"]["status"] == "missing"


def test_baselines_are_building_until_a_slot_has_enough_history(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "baselines_meta.json").write_text(
        json.dumps({"computed_utc": "2026-11-30T13:00:00", "rows": 2, "min_days_for_ok": 3})
    )
    settings = Settings(data_dir=tmp_path)
    now = pd.Timestamp("2026-11-30 14:00")
    low = pd.DataFrame({"metric": ["m", "m"], "confidence": ["low", "low"]})
    ok = pd.DataFrame({"metric": ["m", "m"], "confidence": ["low", "ok"]})

    def baseline_status(baselines, at=now):
        sources = build_status(connect(tmp_path), settings, at, baselines)["sources"]
        return next(s for s in sources if s["source"] == "baselines")

    building = baseline_status(low)
    assert building["status"] == "building" and "0 with enough history" in building["detail"]
    assert baseline_status(ok)["status"] == "ok"
    assert baseline_status(ok, now + pd.Timedelta(days=3))["status"] == "stale"


def test_empty_datastore_exports_valid_files(tmp_path):
    out = tmp_path / "data"
    sizes = export_site_data(
        connect(tmp_path), Settings(data_dir=tmp_path), pd.DataFrame(), out, T0.to_pydatetime()
    )
    assert set(sizes) == {"status.json", "stations.json", "events.json"}
    assert strict_load(out / "events.json")["events"] == []
    assert {s["status"] for s in strict_load(out / "status.json")["sources"]} == {"missing"}


def test_downsample_averages_consecutive_minutes():
    minutes = pd.date_range(T0, periods=300, freq="min")
    df = pd.DataFrame({"minute_utc": minutes, "v": range(300), "w": [None] * 300})
    ds = downsample(df, 120)
    assert len(ds) == 100  # buckets of 3 minutes
    assert ds["minute_utc"].iloc[1] == minutes[3] and ds["v"].iloc[1] == 4.0
    assert ds["w"].isna().all()
    assert downsample(df.head(50), 120) is not None and len(downsample(df.head(50), 120)) == 50


def test_helpers():
    assert iso(pd.Timestamp("2026-11-30 14:00:05.9")) == "2026-11-30T14:00:05Z"
    assert iso(pd.Timestamp("2026-11-30 09:00", tz="America/New_York")) == "2026-11-30T14:00:00Z"
    assert iso(None) is None and iso(pd.NaT) is None
    assert num(float("nan")) is None and num(math.inf) is None and num("x") is None
    assert num(12.345) == 12.3 and num(12.345, 0) == 12.0
    assert [impact_label(x) for x in (None, 0, 19.9, 20, 50, 74.9, 75, 100)] == [
        "unknown",
        "low",
        "low",
        "moderate",
        "high",
        "high",
        "severe",
        "severe",
    ]


def test_recommendations_are_exported_per_station_latest_run_only(tmp_path):
    from transit_ripple.export import latest_recommendations

    def rec(run, minute, station, rank, text, crowded=False):
        t = T0 + pd.Timedelta(minutes=minute)
        return {
            "bucket_utc": t,
            "computed_utc": t,
            "run_id": run,
            "event_id": "A-1",
            "event_route_id": "A",
            "station_id": station,
            "station_name": {"A41": "Jay St-MetroTech", "A42": "Hoyt St"}[station],
            "rank": rank,
            "option_type": "walk_line" if rank == 1 else "wait",
            "option_id": f"opt{rank}",
            "est_total_min": 10.0 * rank,
            "normal_total_min": 9.0,
            "is_obvious": rank == 1,
            "crowded": crowded,
            "crowd_reasons": ["bike_depletion"] if crowded else [],
            "confidence": "high",
            "text": text,
            "routes": ["C"],
        }

    rows = [
        rec("r1", 0, "A41", 1, "old"),
        rec("r2", 5, "A41", 2, "Wait."),
        rec("r2", 5, "A41", 1, "Switch."),
        rec("r2", 5, "A42", 1, "Walk.", crowded=True),
    ]
    write_table(tmp_path, "recommendations", pd.DataFrame(rows), "r")
    out = latest_recommendations(connect(tmp_path), ["A-1", "none"])
    assert set(out) == {"A-1"}
    entry = out["A-1"]
    assert entry["computed_utc"] == "2026-11-30T14:05:00Z"
    a41, a42 = entry["stations"]
    assert [o["text"] for o in a41["options"]] == ["Switch.", "Wait."]  # ranked, latest run
    assert a42["options"][0]["crowded"] and a42["options"][0]["crowd_reasons"] == ["bike_depletion"]
    assert latest_recommendations(connect(tmp_path), []) == {}


def test_replay_data_and_notable_events(tmp_path):
    from transit_ripple.export import notable_events, recommendation_history

    write_context(tmp_path)
    write_map_references(tmp_path)
    rows = line_rows("A", "S", [8, 9, 12, 11, 10, 9, 8, 2, 1])
    write_table(tmp_path, "line_minute", pd.DataFrame(rows), "r")
    settings = Settings(
        data_dir=tmp_path,
        impact_weight_traffic=0.3,
        event_min_minutes=5,
        event_lookback_hours=2,
        notable_min_duration_min=5,
    )
    baselines = read_reference(tmp_path, "baselines")
    now = (T0 + pd.Timedelta(minutes=9)).to_pydatetime()
    write_table(
        tmp_path, "events", process_events(connect(tmp_path), settings, baselines, now, "r1"), "r1"
    )
    recs = []
    for minute in (2, 4):
        for rank in (1, 2, 3, 4):
            t = T0 + pd.Timedelta(minutes=minute)
            recs.append(
                {
                    "bucket_utc": t,
                    "computed_utc": t,
                    "run_id": f"r{minute}",
                    "event_id": "A-20261130T1400Z",
                    "station_id": "A41",
                    "station_name": "Jay St-MetroTech",
                    "rank": rank,
                    "option_type": "wait",
                    "option_id": f"o{rank}",
                    "text": f"Option {rank} at minute {minute}.",
                    "is_obvious": rank == 2,
                    "crowded": False,
                    "confidence": "low",
                }
            )
    write_table(tmp_path, "recommendations", pd.DataFrame(recs), "r")
    con = connect(tmp_path)
    history = recommendation_history(con, "A-20261130T1400Z")
    assert [h["computed_utc"] for h in history] == ["2026-11-30T14:02:00Z", "2026-11-30T14:04:00Z"]
    assert [o["rank"] for o in history[0]["stations"][0]["options"]] == [1, 2, 3]  # top 3 only
    notable = notable_events(con, settings, pd.Timestamp(T0 + pd.Timedelta(hours=1)))
    assert notable["event_id"].tolist() == ["A-20261130T1400Z"]
    assert notable["notable_basis"].tolist() == ["impact"]

    out = tmp_path / "site"
    export_site_data(con, settings, baselines, out, (T0 + pd.Timedelta(hours=1)).to_pydatetime())
    payload = strict_load(out / "events.json")
    assert payload["notable"] == [{"id": "A-20261130T1400Z", "basis": "impact"}]
    assert payload["events"][0]["in_window"] and payload["events"][0]["notable"] == "impact"
    detail = strict_load(out / "events" / "A-20261130T1400Z.json")
    n = len(detail["series"]["t"])
    assert set(detail["station_series"]) == {"A41", "A42"}
    assert all(len(v) == n for v in detail["station_series"].values())
    assert max(v for v in detail["station_series"]["A41"] if v is not None) == 12.0
    assert len(detail["recommendation_history"]) == 2


def test_traffic_speed_is_off_by_default(tmp_path):
    """Traffic speed is not collected, scored or shown unless switched on."""
    settings = Settings(data_dir=tmp_path)
    assert not settings.traffic_enabled and settings.impact_weight_traffic == 0.0
    sources = {
        s["source"] for s in build_status(connect(tmp_path), settings, pd.Timestamp(T0))["sources"]
    }
    assert "traffic" not in sources and "bus" in sources
    from transit_ripple.export import _signals

    detail = json.dumps(
        {"signals": {"traffic": {"status": "zero_weight"}, "bus": {"status": "used"}}}
    )
    assert set(_signals(detail)) == {"bus"}
