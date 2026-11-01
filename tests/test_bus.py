"""M5 bus data: SIRI parsing, speed samples, minute rows, the bus source and stops."""

import json
import zipfile

import pandas as pd
import pytest

from transit_ripple import quality as q
from transit_ripple.bus import (
    SiriAuthError,
    SiriError,
    bus_minute_rows,
    bus_stops_from_zips,
    parse_vehicle_monitoring,
    segment_id,
    speed_samples,
    strip_agency,
)
from transit_ripple.config import Settings
from transit_ripple.feeds import FetchResult
from transit_ripple.sources import BusSource
from transit_ripple.state import PipelineState

T = pd.Timestamp("2026-11-30 14:00:00", tz="UTC")


def activity(vehicle, along, call_dist, t, stop="308209", trip="T1", route="B63", status=None):
    """One SIRI v2 VehicleActivity (strings wrapped in lists, as in version 2)."""
    journey = {
        "LineRef": f"MTA NYCT_{route}",
        "DirectionRef": "0",
        "FramedVehicleJourneyRef": {"DataFrameRef": "2026-11-30", "DatedVehicleJourneyRef": trip},
        "PublishedLineName": [route],
        "VehicleLocation": {"Longitude": -73.98, "Latitude": 40.68},
        "VehicleRef": f"MTA NYCT_{vehicle}",
        "MonitoredCall": {
            "StopPointRef": f"MTA_{stop}",
            "StopPointName": ["5 AV/UNION ST"],
            "Extensions": {
                "Distances": {
                    "CallDistanceAlongRoute": call_dist,
                    "DistanceFromCall": call_dist - along,
                }
            },
        },
    }
    if status:
        journey["ProgressStatus"] = [status]
    return {"MonitoredVehicleJourney": journey, "RecordedAtTime": t.isoformat()}


def siri(activities, error=None) -> bytes:
    vm = {"ResponseTimestamp": T.isoformat(), "VehicleActivity": activities}
    if error:
        vm = {"ResponseTimestamp": T.isoformat(), "ErrorCondition": {"Description": error}}
    return json.dumps({"Siri": {"ServiceDelivery": {"VehicleMonitoringDelivery": [vm]}}}).encode()


def test_parse_vehicle_monitoring_v2_and_ids():
    bad = {"MonitoredVehicleJourney": {"LineRef": "MTA NYCT_B63"}, "RecordedAtTime": None}
    parsed = parse_vehicle_monitoring(siri([activity("7582", 1000.0, 1200.0, T), bad]))
    assert parsed.n_activities == 2 and parsed.n_bad == 1
    v = parsed.vehicles.iloc[0]
    assert (v.route_id, v.next_stop_id, v.vehicle_id) == ("B63", "308209", "MTA NYCT_7582")
    assert v.dist_along_m == 1000.0 and v.direction_id == 0
    assert v.recorded_ts == T.timestamp()
    assert strip_agency("MTA_308209") == "308209" and strip_agency("B63") == "B63"


def test_parse_errors_distinguish_a_rejected_key():
    with pytest.raises(SiriAuthError):
        parse_vehicle_monitoring(siri([], error="API key is not authorized."))
    with pytest.raises(SiriError):
        parse_vehicle_monitoring(siri([], error="Service temporarily unavailable"))
    with pytest.raises(SiriError):
        parse_vehicle_monitoring(b"<html>oops</html>")


def frame(rows):
    return parse_vehicle_monitoring(siri(rows)).vehicles


def test_speed_samples_pairs_same_trip_and_filters_bad_pairs():
    t1 = T + pd.Timedelta(seconds=60)
    prev = frame(
        [
            activity("1", 1000.0, 1500.0, T),
            activity("2", 1000.0, 1500.0, T),  # will move backwards: new trip / bad data
            activity("3", 1000.0, 1500.0, T, status="layover"),
            activity("4", 1000.0, 1500.0, T),  # will be impossibly fast
            activity("5", 1000.0, 1500.0, T, trip="OLD"),  # different trip later
        ]
    )
    cur = frame(
        [
            activity("1", 1268.2, 1500.0, t1, stop="400001"),  # 268.2 m in 60 s = 10 mph
            activity("2", 900.0, 1500.0, t1),
            activity("3", 1100.0, 1500.0, t1),
            activity("4", 3000.0, 3500.0, t1),
            activity("5", 1100.0, 1500.0, t1, trip="NEW"),
        ]
    )
    s = speed_samples(prev, cur, 20, 300, 60)
    assert s["vehicle_id"].tolist() == ["MTA NYCT_1"]
    row = s.iloc[0]
    assert row.stop_id == "400001"  # the segment ending at the bus's current next stop
    assert row.speed_mph == pytest.approx(10.0, abs=0.01)
    too_close = speed_samples(
        prev, frame([activity("1", 1010.0, 1500.0, T + pd.Timedelta(seconds=5))]), 20, 300, 60
    )
    assert too_close.empty


def test_bus_minute_rows_weight_by_time_and_keep_stops_near_subway():
    ts = T.timestamp()
    samples = pd.DataFrame(
        {
            "observed_ts": [ts, ts + 10, ts + 20],
            "route_id": ["B63", "B63", "B63"],
            "direction_id": [0, 0, 0],
            "stop_id": ["S1", "S1", "S9"],
            "vehicle_id": ["a", "b", "c"],
            "speed_mph": [10.0, 2.0, 5.0],
            "distance_m": [268.2, 53.6, 100.0],
            "duration_s": [60.0, 60.0, 60.0],
        }
    )
    rows = bus_minute_rows(samples, "r1", ts + 30, near_stops={"S1"})
    assert len(rows) == 1
    r = rows.iloc[0]
    assert r.segment_id == segment_id("B63", 0, "S1") == "B63|0|S1"
    assert (r.n_samples, r.n_vehicles) == (2, 2)
    assert r.speed_mph == pytest.approx((268.2 + 53.6) / 120 * 2.236936, abs=0.01)
    assert bus_minute_rows(samples, "r1", ts, near_stops={"nope"}).empty


class ScriptedFetch:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, source, url, params=None, headers=None):
        self.calls.append(params)
        status, body = self.responses.pop(0)
        if status != 200:
            return FetchResult(source, url, 1.8e9, None, http_status=status, error=f"HTTP {status}")
        return FetchResult(source, url, self.now, body, http_status=200)


def bus_settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path, bus_static_urls="", **kw)


def test_bus_source_without_key_is_unavailable_and_requests_nothing(tmp_path):
    fetch = ScriptedFetch([])
    src = BusSource(bus_settings(tmp_path), fetch, clock=lambda: 1.8e9)
    src.poll()
    out = src.run("r1", PipelineState(), tmp_path)
    (rec,) = out.records
    assert rec.status == q.STATUS_UNAVAILABLE and "MTA_BUS_API_KEY" in rec.detail
    assert rec.is_missing and fetch.calls == [] and not out.tables


def test_rejected_key_stops_polling_and_is_unavailable(tmp_path):
    fetch = ScriptedFetch([(403, b"")])
    clock = iter([1.8e9, 1.8e9 + 60, 1.8e9 + 120, 1.8e9 + 180])
    src = BusSource(
        bus_settings(tmp_path, mta_bus_api_key="secret"), fetch, clock=lambda: next(clock)
    )
    src.poll()
    src.poll()  # not requested again after a 403
    out = src.run("r1", PipelineState(), tmp_path)
    assert len(fetch.calls) == 1
    assert out.records[0].status == q.STATUS_UNAVAILABLE
    assert "secret" not in json.dumps([r.to_dict() for r in out.records])  # never reported


def test_two_snapshots_make_segment_speeds_and_respect_the_rate_limit(tmp_path):
    t1 = T + pd.Timedelta(seconds=60)
    fetch = ScriptedFetch(
        [
            (200, siri([activity("1", 1000.0, 1500.0, T)])),
            (200, siri([activity("1", 1268.2, 1500.0, t1)])),
        ]
    )
    times = iter([T.timestamp(), T.timestamp() + 10, T.timestamp() + 60, T.timestamp() + 70])
    src = BusSource(bus_settings(tmp_path, mta_bus_api_key="k"), fetch, clock=lambda: next(times))
    fetch.now = T.timestamp() + 1
    src.poll()
    src.poll()  # 10 s later: skipped (Bus Time asks for >= 30 s between requests)
    fetch.now = t1.timestamp() + 1
    src.poll()
    out = src.run("r1", PipelineState(), tmp_path)
    assert len(fetch.calls) == 2 and fetch.calls[0]["key"] == "k"
    assert [r.status for r in out.records] == ["ok", "ok"]
    bm = out.tables["bus_minute"]
    assert bm["segment_id"].tolist() == ["B63|0|308209"]
    assert bm["speed_mph"].iloc[0] == pytest.approx(10.0, abs=0.01)


def test_bus_stops_from_static_gtfs(tmp_path):
    zp = tmp_path / "gtfs_b.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n1,A ST,40.1,-73.9\n2,B ST,40.2,-73.8\n",
        )
        zf.writestr(
            "trips.txt", "route_id,service_id,trip_id,direction_id\nB63,s,t1,0\nB25,s,t2,1\n"
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,08:00:00,08:00:00,1,1\nt1,08:05:00,08:05:00,2,2\nt2,09:00:00,09:00:00,2,1\n",
        )
    stops = bus_stops_from_zips([zp]).set_index("stop_id")
    assert list(stops.loc["1", "routes"]) == ["B63"]
    assert list(stops.loc["2", "routes"]) == ["B25", "B63"]
    assert stops.loc["2", "lat"] == 40.2


def test_version_2_records_without_route_distances_give_no_speed():
    """Bus Time's SIRI v2 omits Extensions.Distances; such records cannot be paired."""

    def v2(t, from_stop):
        a = activity("1", 1000.0, 1500.0, t)
        call = a["MonitoredVehicleJourney"]["MonitoredCall"]
        call.pop("Extensions")
        call["DistanceFromStop"] = from_stop
        return a

    prev = frame([v2(T, 500.0)])
    cur = frame([v2(T + pd.Timedelta(seconds=60), 200.0)])
    assert prev["dist_along_m"].isna().all()
    assert speed_samples(prev, cur, 20, 300, 60).empty


def test_bus_source_requests_siri_version_1(tmp_path):
    fetch = ScriptedFetch([(200, siri([activity("1", 1000.0, 1500.0, T)]))])
    fetch.now = T.timestamp() + 1
    src = BusSource(bus_settings(tmp_path, mta_bus_api_key="k"), fetch, clock=lambda: T.timestamp())
    src.poll()
    assert "version" not in fetch.calls[0]  # v1 is the default and carries the distances
