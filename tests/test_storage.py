import datetime as dt

import pandas as pd
import pytest

from transit_ripple.storage import (
    bucket_of,
    compact,
    connect,
    date_partition,
    epoch_to_utc,
    line_minute_filled,
    write_table,
)

T0 = int(dt.datetime(2026, 11, 2, 10, 0, tzinfo=dt.UTC).timestamp())


def utc(minute_offset: float) -> pd.Timestamp:
    return pd.Timestamp("2026-11-02 10:00:00") + pd.Timedelta(minutes=minute_offset)


def test_bucket_and_partition_helpers_use_utc_minutes():
    buckets = bucket_of(pd.Series([T0 + 59, T0 + 60, None]))
    assert buckets[0] == utc(0) and buckets[1] == utc(1) and pd.isna(buckets[2])
    assert epoch_to_utc([T0 + 1]).iloc[0] == pd.Timestamp("2026-11-02 10:00:01")
    assert date_partition(T0) == "date=2026-11-02"


def stop_rows(bucket, observed, delay, trip="t1", run="r1"):
    return {
        "bucket_utc": bucket,
        "observed_utc": observed,
        "run_id": run,
        "feed_id": "numbered",
        "route_id": "1",
        "direction": "N",
        "trip_id": trip,
        "start_date": "20261102",
        "stop_id": "103N",
        "scheduled_utc": utc(5),
        "delay_min": delay,
        "is_assigned": True,
    }


def test_write_table_partitions_by_bucket_date(tmp_path):
    df = pd.DataFrame(
        [
            stop_rows(
                pd.Timestamp("2026-11-02 23:59:00"), pd.Timestamp("2026-11-02 23:59:10"), 1.0
            ),
            stop_rows(
                pd.Timestamp("2026-11-03 00:00:00"), pd.Timestamp("2026-11-03 00:00:05"), 2.0
            ),
        ]
    )
    files = write_table(tmp_path, "stop_delays", df, "r1")
    assert sorted(f.parent.name for f in files) == ["date=2026-11-02", "date=2026-11-03"]
    assert write_table(tmp_path, "stop_delays", df.iloc[0:0], "r2") == []


def test_connect_creates_typed_empty_views_without_data(tmp_path):
    con = connect(tmp_path)
    for table in ("stop_delays", "line_minute", "quality_feed"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    cols = [r[0] for r in con.execute("DESCRIBE stop_delays").fetchall()]
    assert "delay_min" in cols and "bucket_utc" in cols


def test_connect_uses_utc_session_time_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    con = connect(tmp_path)
    assert con.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"
    now_utc = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    got = con.execute("SELECT now()::TIMESTAMP").fetchone()[0]
    assert abs((got - now_utc).total_seconds()) < 60


def test_dedup_view_keeps_latest_observation_per_bucket(tmp_path):
    write_table(tmp_path, "stop_delays", pd.DataFrame([stop_rows(utc(0), utc(0.1), 1.0)]), "r1")
    write_table(
        tmp_path, "stop_delays", pd.DataFrame([stop_rows(utc(0), utc(0.5), 1.5, run="r2")]), "r2"
    )
    write_table(
        tmp_path, "stop_delays", pd.DataFrame([stop_rows(utc(1), utc(1.2), 2.0, run="r3")]), "r3"
    )
    con = connect(tmp_path)
    assert con.execute("SELECT count(*) FROM stop_delays_all").fetchone()[0] == 3
    rows = con.execute("SELECT bucket_utc, delay_min FROM stop_delays ORDER BY 1").fetchall()
    assert [r[1] for r in rows] == [1.5, 2.0]
    # Hive partition column is exposed for pruning.
    assert (
        con.execute("SELECT DISTINCT date::VARCHAR FROM stop_delays").fetchone()[0] == "2026-11-02"
    )


def test_compact_merges_past_partitions_only(tmp_path):
    write_table(tmp_path, "stop_delays", pd.DataFrame([stop_rows(utc(0), utc(0.1), 1.0)]), "r1")
    write_table(
        tmp_path, "stop_delays", pd.DataFrame([stop_rows(utc(0), utc(0.5), 1.5, run="r2")]), "r2"
    )
    today = pd.Timestamp("2026-11-03 08:00:00")
    write_table(
        tmp_path, "stop_delays", pd.DataFrame([stop_rows(today, today, 3.0, run="r3")]), "r3"
    )
    write_table(
        tmp_path, "stop_delays", pd.DataFrame([stop_rows(today, today, 3.0, run="r4")]), "r4"
    )

    done = compact(tmp_path, "stop_delays", before=dt.date(2026, 11, 3))
    assert done == ["date=2026-11-02"]
    past = sorted(p.name for p in (tmp_path / "stop_delays" / "date=2026-11-02").iterdir())
    assert past == ["compacted.parquet"]
    assert len(list((tmp_path / "stop_delays" / "date=2026-11-03").iterdir())) == 2

    con = connect(tmp_path)
    got = con.execute("SELECT delay_min FROM stop_delays_all WHERE date = '2026-11-02'").fetchall()
    assert got == [(1.5,)]  # deduplicated while compacting
    assert compact(tmp_path, "stop_delays", before=dt.date(2026, 11, 3)) == []  # idempotent
    assert compact(tmp_path / "nothing", "line_minute", before=dt.date(2026, 11, 3)) == []


def line_row(minute, status="ok", delay=2.0, route="A", run="r"):
    return {
        "bucket_utc": utc(minute),
        "observed_utc": utc(minute) if status == "ok" else None,
        "fetched_utc": utc(minute),
        "run_id": run,
        "feed_id": "ace",
        "route_id": route,
        "direction": "N",
        "status": status,
        "n_trips_measured": 5 if status == "ok" else None,
        "delay_mean_min": delay if status == "ok" else None,
    }


def test_line_minute_filled_forward_fills_with_freshness_and_never_fakes_normal(tmp_path):
    rows = [
        line_row(0, delay=1.0),
        line_row(3, delay=4.0),
        line_row(6, status="stale"),
        line_row(9, delay=2.0),
    ]
    write_table(tmp_path, "line_minute", pd.DataFrame(rows), "r")
    con = connect(tmp_path)
    df = line_minute_filled(con, utc(0).to_pydatetime(), utc(16).to_pydatetime(), max_age_min=5)
    df = df.set_index("minute_utc")

    def at(m):
        return df.loc[utc(m)]

    assert at(0)["fill_status"] == "observed" and at(0)["delay_mean_min"] == pytest.approx(1.0)
    assert at(2)["fill_status"] == "filled" and at(2)["age_min"] == 2
    assert at(2)["delay_mean_min"] == pytest.approx(1.0)
    assert at(5)["fill_status"] == "filled" and at(5)["delay_mean_min"] == pytest.approx(4.0)
    # The feed was stale at minute 6: missing, not "normal", until fresh data arrives.
    assert at(6)["fill_status"] == "missing" and at(6)["source_status"] == "stale"
    assert pd.isna(at(7)["delay_mean_min"])
    assert at(9)["fill_status"] == "observed"
    assert at(14)["fill_status"] == "filled" and at(14)["age_min"] == 5
    # Older than max_age: missing with NULL metrics.
    assert at(15)["fill_status"] == "missing" and pd.isna(at(15)["delay_mean_min"])
    assert len(df) == 17


def test_prune_removes_only_partitions_older_than_retention(tmp_path):
    from transit_ripple.storage import prune

    for day in ("2026-10-01", "2026-10-20", "2026-11-01"):
        t = pd.Timestamp(f"{day} 12:00")
        write_table(
            tmp_path,
            "bike_minute",
            pd.DataFrame(
                [
                    {
                        "bucket_utc": t,
                        "observed_utc": t,
                        "fetched_utc": t,
                        "run_id": "r",
                        "station_id": "s1",
                        "bikes_available": 3,
                    }
                ]
            ),
            "r",
        )
    removed = prune(tmp_path, "bike_minute", keep_days=20, today=dt.date(2026, 11, 2))
    assert removed == ["date=2026-10-01"]
    left = sorted(p.name for p in (tmp_path / "bike_minute").iterdir())
    assert left == ["date=2026-10-20", "date=2026-11-01"]
    assert prune(tmp_path, "missing_table", 20, dt.date(2026, 11, 2)) == []
