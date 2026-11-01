"""Parsers and ingestors for service alerts, NYC DOT traffic speed and Citi Bike GBFS."""

import json
import time

import pandas as pd
import pytest

from builders import RtAlert, ScriptedFetcher, build_alerts_feed
from transit_ripple import quality as q
from transit_ripple.alerts import AlertsParseError, is_active, overlaps, parse_alerts, station_of
from transit_ripple.bikes import (
    GbfsError,
    choose_version,
    parse_discovery,
    parse_station_information,
    parse_station_status,
    parse_versions,
)
from transit_ripple.config import Settings
from transit_ripple.sources import AlertsSource, BikesSource, TrafficSource, build_sources
from transit_ripple.state import PipelineState
from transit_ripple.storage import merge_reference
from transit_ripple.traffic import (
    decode_polyline,
    epoch_to_local_text,
    link_geometry,
    local_to_epoch,
    parse_link_points,
    parse_speeds,
    soql_since,
)

NOW = 1793609600.0  # 2026-11-02 08:53:20 UTC = 03:53:20 EST


# --- alerts ---------------------------------------------------------------------------


def test_station_of_strips_direction_suffix():
    assert station_of("A32N") == "A32"
    assert station_of("101S") == "101"
    assert station_of("A32") == "A32"
    assert station_of("S01") == "S01"


def test_parse_alerts_reads_mercury_extension_routes_stations_and_text():
    feed = build_alerts_feed(
        1000,
        [
            RtAlert(
                "a1",
                "Delays",
                ("2", "3"),
                ("235N", "235S", "236N"),
                ((900, None),),
                created=800,
                updated=950,
            ),
            RtAlert("a2", "Planned - Part Suspended", ("A",), (), ((2000, 3000), (5000, 6000))),
        ],
    )
    p = parse_alerts(feed)
    assert p.header_ts == 1000
    a1, a2 = p.alerts.to_dict("records")
    assert a1["alert_type"] == "Delays" and not a1["is_planned"]
    assert (a1["created_ts"], a1["updated_ts"]) == (800, 950)
    assert a1["route_ids"] == ["2", "3"]
    assert a1["station_ids"] == ["235", "236"]
    assert a1["header_text"] == "Northbound [1] trains are delayed"  # not the en-html variant
    assert a1["active_end_ts"] is pd.NA or pd.isna(a1["active_end_ts"])  # open-ended
    assert a2["is_planned"]
    assert (a2["active_start_ts"], a2["active_end_ts"]) == (2000, 6000)


def test_parse_alerts_rejects_garbage():
    with pytest.raises(AlertsParseError):
        parse_alerts(b"\x00\x01garbage")


def test_alert_activity_and_overlap_rules():
    assert is_active([], 5)  # no period = always active
    assert is_active([(None, 10)], 5) and not is_active([(None, 10)], 11)
    assert is_active([(1, 2), (8, None)], 100)
    assert overlaps([(10, 20)], 15, 30) and not overlaps([(10, 20)], 21, 30)
    assert overlaps([(10, None)], 50, 60)


def test_alerts_source_stores_versions_once_and_presence_every_fetch(tmp_path):
    s = Settings(data_dir=tmp_path)
    state = PipelineState()
    alerts = [RtAlert("a1", periods=((int(NOW) - 600, None),), updated=int(NOW) - 300)]
    fetch = ScriptedFetcher({"subway-alerts": build_alerts_feed(int(NOW) - 300, alerts)}, NOW)
    src = AlertsSource(s, fetch, clock=lambda: NOW)

    out1 = src.run("r1", state, tmp_path)
    rec = out1.records[0]
    assert rec.status == q.STATUS_OK and rec.n_new == 1 and rec.n_valid == 1
    assert len(out1.tables["alerts"]) == 1 and len(out1.tables["alert_presence"]) == 1
    row = out1.tables["alerts"].iloc[0]
    assert list(row["route_ids"]) == ["1"]
    assert row["active_periods"][0]["end_utc"] is None

    # Same feed again: no new version, but presence is still recorded.
    out2 = src.run("r2", state, tmp_path)
    assert out2.records[0].n_new == 0
    assert "alerts" not in out2.tables and len(out2.tables["alert_presence"]) == 1


def test_alerts_source_header_is_last_change_time_so_only_very_old_is_stale(tmp_path):
    s = Settings(data_dir=tmp_path)
    quiet = build_alerts_feed(int(NOW) - 3600, [RtAlert("a1")])  # 1 h without changes
    out = AlertsSource(s, ScriptedFetcher({"subway-alerts": quiet}, NOW)).run(
        "r", PipelineState(), tmp_path
    )
    assert out.records[0].status == q.STATUS_OK
    frozen = build_alerts_feed(int(NOW) - 7 * 3600, [RtAlert("a1")])
    out = AlertsSource(s, ScriptedFetcher({"subway-alerts": frozen}, NOW)).run(
        "r", PipelineState(), tmp_path
    )
    assert out.records[0].status == q.STATUS_STALE and out.records[0].is_missing


def test_alerts_source_outage_and_gap(tmp_path):
    s = Settings(data_dir=tmp_path)
    state = PipelineState(sources={"alerts": {"last_ok_ts": NOW - 3600}})
    out = AlertsSource(s, ScriptedFetcher({"subway-alerts": None}, NOW)).run("r", state, tmp_path)
    rec = out.records[0]
    assert rec.status == q.STATUS_OUTAGE and rec.gap_min == pytest.approx(60.0)


# --- traffic ----------------------------------------------------------------------------


def test_decode_polyline_reference_example():
    # Example from Google's polyline algorithm documentation.
    pts = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert pts == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq") is None  # truncated


def _encode(points):
    """Minimal polyline encoder for tests."""
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        for v, prev in ((round(lat * 1e5), plat), (round(lon * 1e5), plon)):
            d = v - prev
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(lat * 1e5), round(lon * 1e5)
    return "".join(out)


def test_link_geometry_validates_against_link_points_and_repairs_backslashes():
    pts = [(40.75, -73.99), (40.76, -73.98), (40.77, -73.97)]
    enc = _encode(pts)
    lp = " ".join(f"{a},{b}" for a, b in pts)
    got, source = link_geometry(enc, lp)
    assert source == "polyline" and got == pts

    # Doubled backslashes inserted by the publisher break the raw decoding.
    broken = enc[:3] + "\\\\" + enc[3:]
    got, source = link_geometry(broken, lp)
    assert source == "polyline_fixed" and got == pts

    # No usable polyline: fall back to the (possibly truncated) link_points.
    got, source = link_geometry("not a polyline!", lp)
    assert source == "link_points" and got == pts
    assert link_geometry(None, None) == ([], "none")


def test_parse_link_points_drops_partial_last_pair_when_truncated():
    text = " ".join(["40.75,-73.99"] * 21) + " 40.7"
    assert len(text) >= 250
    assert len(parse_link_points(text)) == 21
    assert parse_link_points("40.1,-73.2 40.3,-73.4") == [(40.1, -73.2), (40.3, -73.4)]


def test_local_to_epoch_handles_dst_fall_back():
    # 2026-11-01 01:30 New York happens twice (EDT 05:30Z, then EST 06:30Z).
    edt = 1793511000  # 05:30Z
    est = edt + 3600
    assert local_to_epoch("2026-11-01T01:30:00.000", reference_ts=edt + 60) == edt
    assert local_to_epoch("2026-11-01T01:30:00.000", reference_ts=est + 60) == est
    assert local_to_epoch("2026-11-02T03:53:20", NOW) == int(NOW)
    assert epoch_to_local_text(edt) == "2026-11-01T01:30:00"


def test_soql_since_uses_local_floating_time():
    params = soql_since(1793595600)  # 2026-11-02T05:00Z = 00:00 EST
    assert params["$where"] == "data_as_of > '2026-11-02T00:00:00'"
    assert params["$order"] == "data_as_of ASC"


def speed_row(link_id, local_time, speed="25.5", status="0", pts=None):
    pts = pts or [(40.75, -73.99), (40.76, -73.98)]
    return {
        "id": link_id,
        "speed": speed,
        "travel_time": "120",
        "status": status,
        "data_as_of": local_time,
        "link_id": "x",
        "link_points": " ".join(f"{a},{b}" for a, b in pts),
        "encoded_poly_line": _encode(pts),
        "owner": "NYC-DOT",
        "borough": "Manhattan",
        "link_name": f"Link {link_id}",
    }


def test_parse_speeds_normalizes_rows_and_marks_no_data():
    rows = [
        speed_row("1", "2026-11-02T03:50:00.000"),
        speed_row("1", "2026-11-02T03:50:00.000"),  # duplicate observation
        speed_row("2", "2026-11-02T03:51:00.000", speed="0", status="-101"),
        {"id": "3"},  # malformed
    ]
    p = parse_speeds(json.dumps(rows).encode(), NOW)
    assert (p.n_rows, p.n_bad_rows) == (4, 1)
    obs = p.observations.set_index("link_id")
    assert len(obs) == 2
    assert obs.loc["1", "is_valid"] and not obs.loc["2", "is_valid"]
    assert obs.loc["1", "observed_ts"] == int(NOW) - 200  # 03:50 EST = 08:50Z
    assert set(p.links["geometry_source"]) == {"polyline"}


def test_traffic_source_dedups_across_runs_and_tracks_freshness(tmp_path):
    s = Settings(data_dir=tmp_path)
    state = PipelineState()
    fresh = json.dumps(
        [speed_row("1", "2026-11-02T03:50:00"), speed_row("2", "2026-11-02T03:52:00")]
    )
    fetch = ScriptedFetcher({"i4gi-tjb9": fresh.encode()}, NOW)
    src = TrafficSource(s, fetch, clock=lambda: NOW)

    out = src.run("r1", state, tmp_path)
    rec = out.records[0]
    assert rec.status == q.STATUS_OK and rec.n_new == 2 and rec.coverage == 1.0
    assert out.tables["traffic_minute"]["age_s"].tolist() == [200.0, 80.0]
    assert "traffic_links" in out.references
    assert fetch.calls[0][2]["$where"].startswith("data_as_of > '2026-11-02T02:23")  # 90 min back

    out2 = src.run("r2", state, tmp_path)  # same rows again
    assert out2.records[0].n_new == 0 and "traffic_minute" not in out2.tables
    assert state.sources["traffic"]["max_observed_ts"] == NOW - 80


def test_traffic_source_stale_feed_probes_latest_and_bootstraps_geometry(tmp_path):
    s = Settings(data_dir=tmp_path)
    old_rows = json.dumps([speed_row("7", "2026-11-01T23:00:00")]).encode()
    fetch = ScriptedFetcher(
        {
            "i4gi-tjb9": [
                b"[]",
                json.dumps([{"latest": "2026-11-01T23:00:00.000"}]).encode(),
                old_rows,
            ]
        },
        NOW,
    )
    out = TrafficSource(s, fetch, clock=lambda: NOW).run("r", PipelineState(), tmp_path)
    rec = out.records[0]
    assert rec.status == q.STATUS_STALE and rec.is_missing
    assert rec.feed_age_s == pytest.approx(NOW - 1793592000)  # Nov 1 23:00 EST = Nov 2 04:00Z
    assert "min old" in rec.detail
    links, key = out.references["traffic_links"]
    assert key == "link_id" and links["link_id"].tolist() == ["7"]
    assert fetch.calls[-1][2] == {"$order": "data_as_of DESC", "$limit": "2000"}


def test_traffic_source_sends_app_token_only_to_socrata(tmp_path):
    s = Settings(data_dir=tmp_path, socrata_app_token="tok", mta_api_key="mta")
    fetch = ScriptedFetcher({"i4gi-tjb9": b"[]", "subway-alerts": None}, NOW)
    TrafficSource(s, fetch, clock=lambda: NOW).run("r", PipelineState(), tmp_path)
    AlertsSource(s, fetch, clock=lambda: NOW).run("r", PipelineState(), tmp_path)
    by_source = {c[0]: c[3] for c in fetch.calls}
    assert by_source["traffic"] == {"X-App-Token": "tok"}
    assert by_source["alerts"] == {"x-api-key": "mta"}


# --- Citi Bike -------------------------------------------------------------------------------


def gbfs_doc(feeds, version="2.3"):
    return json.dumps(
        {
            "last_updated": int(NOW),
            "ttl": 60,
            "version": version,
            "data": {"en": {"feeds": [{"name": n, "url": u} for n, u in feeds.items()]}},
        }
    ).encode()


def status_doc(last_updated, stations):
    return json.dumps(
        {"last_updated": last_updated, "ttl": 60, "version": "2.3", "data": {"stations": stations}}
    ).encode()


def info_doc():
    return json.dumps(
        {
            "last_updated": int(NOW),
            "ttl": 60,
            "data": {
                "stations": [
                    {
                        "station_id": "s1",
                        "name": "Broadway & W 41 St",
                        "lat": 40.755,
                        "lon": -73.986,
                        "capacity": 55,
                    },
                    {"station_id": "s2", "name": "Jay St & Tech Pl", "lat": 40.695, "lon": -73.987},
                    {"station_id": "bad"},
                ]
            },
        }
    ).encode()


def st(station_id, bikes, docks, reported, installed=1, renting=1):
    return {
        "station_id": station_id,
        "num_bikes_available": bikes,
        "num_docks_available": docks,
        "num_ebikes_available": 1,
        "is_installed": installed,
        "is_renting": renting,
        "is_returning": 1,
        "last_reported": reported,
    }


def test_gbfs_discovery_versions_and_v3_layout():
    d = parse_discovery(gbfs_doc({"station_status": "u1", "gbfs_versions": "v"}, "1.1"))
    assert d.version == "1.1" and d.feeds["station_status"] == "u1"
    v3 = json.dumps({"version": "3.0", "data": {"feeds": [{"name": "station_status", "url": "x"}]}})
    assert parse_discovery(v3.encode()).feeds == {"station_status": "x"}
    versions = parse_versions(
        json.dumps(
            {
                "data": {
                    "versions": [
                        {"version": "v1.1", "url": "a"},
                        {"version": "v2.3", "url": "b"},
                        {"version": "3.0", "url": "c"},
                    ]
                }
            }
        ).encode()
    )
    assert choose_version(versions) == ("2.3", "b")
    assert choose_version(versions, max_major=3) == ("3.0", "c")
    with pytest.raises(GbfsError):
        parse_discovery(b'{"data": {"en": {"feeds": []}}}')


def test_station_information_and_status_parsing():
    info = parse_station_information(info_doc())
    assert info.stations["station_id"].tolist() == ["s1", "s2"]  # malformed row skipped
    assert pd.isna(info.stations.loc[1, "capacity"])
    status = parse_station_status(
        status_doc(int(NOW), [st("s1", 5, 50, int(NOW) - 30), st("s2", 0, 20, 86400)])
    )
    df = status.stations.set_index("station_id")
    assert status.n_invalid_reported == 1 and pd.isna(df.loc["s2", "last_reported_ts"])
    assert df.loc["s1", "bikes_available"] == 5 and bool(df.loc["s1", "is_installed"])
    v3 = parse_station_status(
        json.dumps(
            {
                "last_updated": "2026-11-02T08:53:20Z",
                "data": {
                    "stations": [
                        {
                            "station_id": "x",
                            "num_vehicles_available": 3,
                            "num_docks_available": 4,
                            "last_reported": "2026-11-02T08:53:00Z",
                        }
                    ]
                },
            }
        ).encode()
    )
    assert v3.last_updated == int(NOW) and v3.stations.loc[0, "bikes_available"] == 3


def bike_fetcher(status_bytes):
    return ScriptedFetcher(
        {
            "gbfs.json": gbfs_doc(
                {
                    "station_information": "https://x/station_information.json",
                    "station_status": "https://x/station_status.json",
                }
            ),
            "station_information": info_doc(),
            "station_status": status_bytes,
        },
        NOW,
    )


def test_bikes_source_discovers_once_and_writes_station_minutes(tmp_path):
    s = Settings(data_dir=tmp_path)
    state = PipelineState()
    status = status_doc(
        int(NOW) - 20,
        [
            st("s1", 5, 50, int(NOW) - 30),
            st("s2", 2, 10, int(NOW) - 4000),  # stale report
            st("s3", 0, 0, int(NOW) - 30, installed=0),
        ],
    )
    fetch = bike_fetcher(status)
    out = BikesSource(s, fetch, clock=lambda: NOW).run("r1", state, tmp_path)
    by_source = {r.source: r for r in out.records}
    assert set(by_source) == {"bike_discovery", "bike_info", "bike_status"}
    rec = by_source["bike_status"]
    assert rec.status == q.STATUS_OK
    assert (rec.n_records, rec.n_valid, rec.n_stale) == (3, 1, 1)
    assert rec.coverage == pytest.approx(1 / 3)
    rows = out.tables["bike_minute"].set_index("station_id")
    assert rows.loc["s1", "bikes_available"] == 5 and rows.loc["s2", "report_age_s"] == 3980
    merge_reference(tmp_path, "bike_stations", *out.references["bike_stations"])

    # Second run: discovery and station information are cached; status unchanged = duplicate.
    fetch.calls.clear()
    out2 = BikesSource(s, fetch, clock=lambda: NOW + 30).run("r2", state, tmp_path)
    assert [c[0] for c in fetch.calls] == ["bike_status"]
    assert out2.records[0].status == q.STATUS_DUPLICATE and "bike_minute" not in out2.tables


def test_bikes_source_stale_status_is_missing(tmp_path):
    s = Settings(data_dir=tmp_path)
    status = status_doc(int(NOW) - 900, [st("s1", 5, 50, int(NOW) - 900)])
    out = BikesSource(s, bike_fetcher(status), clock=lambda: NOW).run(
        "r", PipelineState(), tmp_path
    )
    rec = next(r for r in out.records if r.source == "bike_status")
    assert rec.status == q.STATUS_STALE and "bike_minute" not in out.tables


def test_build_sources_rejects_unknown_names():
    with pytest.raises(ValueError, match="Unknown source"):
        build_sources(["alerts", "weather"], Settings())
    assert [
        s.name
        for s in build_sources(["alerts", "traffic", "bikes"], Settings(), fetch=lambda *a: None)
    ] == ["alerts", "traffic", "bike_status"]


def test_source_record_report_integration():
    report = q.RunReport(
        run_id="r", started_ts=time.time(), polls_requested=0, thresholds={}, static={}
    )
    report.sources = [
        q.SourceRecord("r", "traffic", "u", 0.0, status=q.STATUS_STALE, detail="old"),
        q.SourceRecord("r", "bike_status", "u", 0.0, coverage=0.2, gap_min=30.0),
        q.SourceRecord("r", "alerts", "u", 0.0),
    ]
    assert report.overall_status() == "degraded"
    issues = report.issues()
    assert any("traffic: stale (old)" in i for i in issues)
    assert any("bike_status: gap of 30 min" in i for i in issues)
    assert any("bike_status: low coverage 20%" in i for i in issues)
    md = report.to_markdown()
    assert "| traffic | stale |" in md and "| bike_status | ok |" in md
    assert report.to_dict()["sources"][1]["coverage"] == 0.2
    assert q.gap_minutes(None, 100, 15) is None
    assert q.gap_minutes(0, 600, 15) is None and q.gap_minutes(0, 1200, 15) == 20.0


def test_bikes_discovery_follows_gbfs_versions_to_the_highest_supported_version(tmp_path):
    """Production path: the official endpoint is GBFS 1.1 and lists newer versions."""
    v11 = gbfs_doc(
        {
            "station_information": "https://x/1.1/station_information.json",
            "station_status": "https://x/1.1/station_status.json",
            "gbfs_versions": "https://x/gbfs_versions.json",
        },
        version="1.1",
    )
    versions = json.dumps(
        {
            "data": {
                "versions": [
                    {"version": "v1.1", "url": "https://x/1.1/gbfs.json"},
                    {"version": "v2.3", "url": "https://x/2.3/gbfs.json"},
                ]
            }
        }
    ).encode()
    v23 = gbfs_doc(
        {
            "station_information": "https://x/2.3/station_information.json",
            "station_status": "https://x/2.3/station_status.json",
        }
    )
    status = status_doc(int(NOW) - 10, [st("s1", 5, 50, int(NOW) - 30)])
    fetch = ScriptedFetcher(
        {
            "citibikenyc.com/gbfs/gbfs.json": v11,
            "gbfs_versions": versions,
            "2.3/gbfs.json": v23,
            "2.3/station_information": info_doc(),
            "2.3/station_status": status,
        },
        NOW,
    )
    state = PipelineState()
    out = BikesSource(Settings(data_dir=tmp_path), fetch, clock=lambda: NOW).run(
        "r", state, tmp_path
    )
    disc = state.sources["bike_status"]["discovery"]
    assert disc["version"] == "2.3"
    assert disc["station_status"] == "https://x/2.3/station_status.json"
    assert {r.source: r.status for r in out.records} == {
        "bike_discovery": q.STATUS_OK,
        "bike_info": q.STATUS_OK,
        "bike_status": q.STATUS_OK,
    }
    assert "1.1/station_status" not in " ".join(c[1] for c in fetch.calls)


def test_bikes_discovery_failure_uses_cache_or_reports_outage(tmp_path):
    s = Settings(data_dir=tmp_path, bike_info_max_age_hours=1)
    down = ScriptedFetcher({"gbfs.json": None}, NOW)
    out = BikesSource(s, down, clock=lambda: NOW).run("r", PipelineState(), tmp_path)
    assert {r.source: r.status for r in out.records} == {
        "bike_discovery": q.STATUS_OUTAGE,
        "bike_status": q.STATUS_OUTAGE,
    }

    # An expired cached discovery is still used when re-discovery fails.
    cached = {
        "at": NOW - 7200,
        "version": "2.3",
        "station_information": "https://x/station_information.json",
        "station_status": "https://x/station_status.json",
    }
    state = PipelineState(sources={"bike_status": {"discovery": cached, "info_ts": NOW}})
    merge_reference(
        tmp_path, "bike_stations", parse_station_information(info_doc()).stations, "station_id"
    )
    fetch = ScriptedFetcher(
        {
            "gbfs.json": None,
            "station_status": status_doc(int(NOW) - 10, [st("s1", 5, 50, int(NOW) - 30)]),
        },
        NOW,
    )
    out = BikesSource(s, fetch, clock=lambda: NOW).run("r", state, tmp_path)
    by_source = {r.source: r for r in out.records}
    assert by_source["bike_discovery"].notes == {"using_cached_discovery": True}
    assert by_source["bike_status"].status == q.STATUS_OK
    assert len(out.tables["bike_minute"]) == 1
