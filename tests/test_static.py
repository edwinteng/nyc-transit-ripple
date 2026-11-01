import datetime as dt

import pandas as pd
import pytest

from builders import (
    SATURDAY_1N,
    SUPPLEMENT_1N,
    WEEKDAY_1N,
    WEEKDAY_GS,
    make_schedule,
    static_frames,
    write_static_zip,
)
from transit_ripple.config import Settings
from transit_ripple.static_gtfs import (
    MATCH_BASE,
    MATCH_EXACT,
    MATCH_EXACT_FALLBACK,
    MATCH_NO_DATE,
    MATCH_NONE,
    StaticSchedule,
    ensure_static,
    gtfs_time_to_seconds,
    service_day_epoch,
)


def utc(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%MZ")


def test_gtfs_time_to_seconds_handles_service_day_overflow():
    assert gtfs_time_to_seconds("00:00:00") == 0
    assert gtfs_time_to_seconds("10:02:30") == 36150
    assert gtfs_time_to_seconds("25:10:05") == 90605
    assert gtfs_time_to_seconds("") is None
    assert gtfs_time_to_seconds(None) is None


def test_service_day_epoch_is_noon_minus_12h_local_time():
    assert utc(service_day_epoch("20261031")) == "2026-10-31T04:00Z"  # EDT midnight
    # DST ends on 2026-11-01: noon EST minus 12 h is 05:00Z, as the GTFS spec defines.
    assert utc(service_day_epoch("20261101")) == "2026-11-01T05:00Z"
    assert utc(service_day_epoch("20261102")) == "2026-11-02T05:00Z"  # EST midnight


def test_active_services_apply_calendar_and_exceptions():
    s = make_schedule()
    assert s.active_services("20261102") == {"Weekday", "L0S1"}  # Monday + supplement
    assert s.active_services("20261103") == {"Weekday"}
    assert s.active_services("20261107") == {"Saturday"}
    # Thanksgiving: weekday removed, Sunday schedule added.
    assert s.active_services("20261126") == {"Sunday"}


def test_weekday_pattern_services_cover_dates_outside_calendar_range():
    s = make_schedule()
    # 2026-10-31 is a Saturday before the schedule's start date.
    assert s.active_services("20261031") == frozenset()
    assert s.weekday_pattern_services("20261031") == {"Saturday"}
    # In range, the fallback adds nothing new.
    assert s.weekday_pattern_services("20261107") == frozenset()


def test_match_exact_trip_on_active_service():
    m = make_schedule().match("060000_1..N03R", "20261102")
    assert (m.static_trip_id, m.method, m.n_candidates) == (WEEKDAY_1N, MATCH_EXACT, 1)
    assert m.matched


def test_match_uses_service_date_to_choose_between_schedules():
    s = make_schedule()
    assert s.match("060000_1..N03R", "20261107").static_trip_id == SATURDAY_1N
    assert s.match("061200_1..N03R", "20261102").static_trip_id == SUPPLEMENT_1N
    assert s.match("061200_1..N03R", "20261103").method == MATCH_NONE


def test_match_base_trip_when_realtime_path_is_missing_or_different():
    s = make_schedule()
    m = s.match("060000_1..N", "20261102")
    assert (m.static_trip_id, m.method) == (WEEKDAY_1N, MATCH_BASE)
    m = s.match("060000_1..N99X", "20261102")
    assert (m.static_trip_id, m.method) == (WEEKDAY_1N, MATCH_BASE)
    m = s.match("036000_GS.N01R", "20261102")
    assert (m.static_trip_id, m.method) == (WEEKDAY_GS, MATCH_EXACT)


def test_match_falls_back_to_weekday_pattern_around_schedule_changes():
    m = make_schedule().match("060000_1..N03R", "20261031")
    assert (m.static_trip_id, m.method) == (SATURDAY_1N, MATCH_EXACT_FALLBACK)


def test_match_unmatched_and_missing_start_date():
    s = make_schedule()
    assert s.match("999900_1..N03R", "20261102").method == MATCH_NONE
    assert s.match("060000_1..N03R", None).method == MATCH_NO_DATE
    assert s.match("060000_1..N03R", "2026-11-02").method == MATCH_NO_DATE
    assert not s.match("garbage", "20261102").matched


def test_match_prefers_supplement_when_two_active_services_share_a_trip():
    f = static_frames()
    extra = pd.DataFrame(
        [("1", "L0S1-1-1095-S02_060000_1..N03R", "L0S1", "x", 0, "1..N03R")],
        columns=f["trips"].columns,
    )
    trips = pd.concat([f["trips"], extra], ignore_index=True)
    s = StaticSchedule.from_frames(trips, f["stop_times"], f["calendar"], f["calendar_dates"])
    m = s.match("060000_1..N03R", "20261102")
    assert m.static_trip_id == "L0S1-1-1095-S02_060000_1..N03R"
    assert m.n_candidates == 2


def test_scheduled_stop_times_in_seconds():
    st = make_schedule().scheduled_stop_times([WEEKDAY_1N])
    assert list(st["stop_id"]) == ["103N", "104N", "106N"]
    assert list(st["arrival_s"]) == [36000, 36120, 36300]


def test_ensure_static_converts_zip_and_reuses_cache(tmp_path):
    zip_path = write_static_zip(tmp_path / "gtfs.zip")
    settings = Settings(data_dir=tmp_path / "data", static_url=str(zip_path))

    info = ensure_static(settings)
    assert info.refreshed
    assert info.feed_version == "test-version"
    parquet = settings.resolved_static_dir / "parquet"
    assert {p.name for p in parquet.iterdir()} >= {"trips.parquet", "stop_times.parquet"}

    again = ensure_static(settings)
    assert not again.refreshed and again.sha256 == info.sha256

    schedule = StaticSchedule.from_dir(settings.resolved_static_dir)
    assert schedule.match("060000_1..N03R", "20261102").static_trip_id == WEEKDAY_1N
    st = schedule.scheduled_stop_times([WEEKDAY_1N])
    assert sorted(st["arrival_s"]) == [36000, 36120, 36300]
    assert schedule.stop_names["104N"] == "Cathedral Pkwy (110 St)"


def test_ensure_static_falls_back_to_cache_when_refresh_fails(tmp_path):
    zip_path = write_static_zip(tmp_path / "gtfs.zip")
    settings = Settings(data_dir=tmp_path / "data", static_url=str(zip_path))
    ensure_static(settings)

    zip_path.unlink()
    info = ensure_static(settings, force=True)
    assert info.warning and "using cached copy" in info.warning
    assert not info.refreshed


def test_ensure_static_without_cache_raises(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", static_url=str(tmp_path / "missing.zip"))
    with pytest.raises(FileNotFoundError):
        ensure_static(settings)
