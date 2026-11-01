import json

import pytest

from builders import RtTrip, at, build_feed, line_1_trip, make_schedule
from transit_ripple import quality as q
from transit_ripple.config import SUBWAY_FEEDS, Settings
from transit_ripple.feeds import FetchResult, load_replay_batches
from transit_ripple.pipeline import Pipeline
from transit_ripple.state import PipelineState
from transit_ripple.static_gtfs import StaticInfo
from transit_ripple.storage import connect

FEEDS = [SUBWAY_FEEDS["numbered"], SUBWAY_FEEDS["l"]]


class FakeFetcher:
    """Returns scripted payloads: one dict {feed_id: (bytes|None, fetched_at)} per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, specs):
        batch = self.script[self.calls]
        self.calls += 1
        out = []
        for spec in specs:
            content, fetched = batch[spec.feed_id]
            error = None if content is not None else "ConnectionError"
            out.append(
                FetchResult(
                    spec.feed_id,
                    spec.url,
                    fetched,
                    content,
                    http_status=200 if content else None,
                    error=error,
                    elapsed_s=0.1,
                )
            )
        return out


class Clock:
    def __init__(self, t):
        self.t = float(t)
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def l_trip(delay_s=0):
    # The L feed has no matching schedule in the fixture -> unmatched trips.
    return RtTrip("060000_L..N01R", route_id="L", stops=[("L01N", at("10:00:00") + delay_s, None)])


def make_pipeline(tmp_path, script, start_ts, **settings_kw):
    settings = Settings(data_dir=tmp_path, **settings_kw)
    clock = Clock(start_ts)
    pipe = Pipeline(
        settings,
        make_schedule(),
        StaticInfo(
            source_url="test",
            static_dir=str(tmp_path),
            feed_version="test-version",
            downloaded_at=start_ts,
        ),
        FEEDS,
        fetcher=FakeFetcher(script),
        clock=clock,
        sleep=clock.sleep,
    )
    return pipe, clock


def test_run_writes_delays_lines_quality_and_marks_outages_missing(tmp_path):
    h = at("09:59:00")
    script = [{"numbered": (build_feed(h, [line_1_trip(delay_s=120)]), h + 5), "l": (None, h + 5)}]
    pipe, _ = make_pipeline(tmp_path, script, h + 5)

    result = pipe.run(polls=1)

    report = result.report.to_dict()
    assert report["overall_status"] == "degraded"
    statuses = {f["feed_id"]: f["status"] for f in report["feeds"]}
    assert statuses == {"numbered": q.STATUS_OK, "l": q.STATUS_OUTAGE}
    assert any("l poll 0: outage" in i for i in report["issues"])
    assert json.loads(result.report_path.read_text())["run_id"] == result.report.run_id

    con = connect(tmp_path)
    delays = con.execute(
        "SELECT stop_id, delay_min, match_method FROM stop_delays ORDER BY stop_id"
    ).fetchall()
    assert delays == [("103N", 2.0, "exact"), ("104N", 2.0, "exact"), ("106N", 2.0, "exact")]

    lines = con.execute(
        "SELECT route_id, direction, status, n_trips_measured, delay_mean_min FROM line_minute "
        "ORDER BY route_id, direction"
    ).fetchall()
    assert lines[0] == ("1", "N", "ok", 1, 2.0)
    # Outage: explicit missing rows for every L line with NULL metrics, never "normal".
    assert lines[1:] == [("L", "N", "outage", None, None), ("L", "S", "outage", None, None)]

    qf = con.execute(
        "SELECT feed_id, status, n_trips, n_rows FROM quality_feed ORDER BY 1"
    ).fetchall()
    assert qf == [("l", "outage", 0, 0), ("numbered", "ok", 1, 3)]
    assert PipelineState.load(tmp_path / "state.json").last_header_ts == {"numbered": h}


def test_duplicate_snapshot_is_skipped_and_backward_jump_is_missing(tmp_path):
    h = at("09:59:00")
    feed = build_feed(h, [line_1_trip()])
    older = build_feed(h - 60, [line_1_trip()])
    l_feed = build_feed(h, [l_trip()])
    pipe, clock = make_pipeline(
        tmp_path,
        [
            {"numbered": (feed, h + 5), "l": (l_feed, h + 5)},
            {"numbered": (feed, h + 35), "l": (l_feed, h + 35)},
            {"numbered": (older, h + 65), "l": (l_feed, h + 65)},
        ],
        h + 5,
    )

    r1 = pipe.run(polls=1)
    clock.t = h + 35
    r2 = pipe.run(polls=1)
    clock.t = h + 65
    r3 = pipe.run(polls=1)

    assert r1.report.rows_written["stop_delays"] == 4
    assert {r.status for r in r2.report.records} == {q.STATUS_DUPLICATE}
    assert r2.report.rows_written["stop_delays"] == 0
    assert r2.report.overall_status() == "ok"  # duplicates are not data loss
    by_feed = {r.feed_id: r.status for r in r3.report.records}
    assert by_feed["numbered"] == q.STATUS_BACKWARD
    assert set(r3.line_minute["status"]) == {q.STATUS_BACKWARD}


def test_stale_feed_is_marked_missing(tmp_path):
    h = at("09:59:00")
    script = [
        {
            "numbered": (build_feed(h, [line_1_trip()]), h + 600),
            "l": (build_feed(h + 590, [l_trip()]), h + 600),
        }
    ]
    pipe, _ = make_pipeline(tmp_path, script, h + 600)
    result = pipe.run(polls=1)
    by_feed = {r.feed_id: r for r in result.report.records}
    assert by_feed["numbered"].status == q.STATUS_STALE
    assert by_feed["numbered"].feed_age_s == pytest.approx(600)
    assert by_feed["l"].status == q.STATUS_OK
    stale_lines = result.line_minute[result.line_minute["feed_id"] == "numbered"]
    assert set(stale_lines["status"]) == {q.STATUS_STALE}
    assert stale_lines["delay_mean_min"].isna().all()
    assert len(stale_lines) == 2 * len(SUBWAY_FEEDS["numbered"].routes)
    assert (result.stop_delays["feed_id"] == "numbered").sum() == 0


def test_stale_predictions_are_dropped_and_counted(tmp_path):
    h = at("10:10:00")  # the 10:00 train is still listed, downstream times are past
    script = [{"numbered": (build_feed(h, [line_1_trip()]), h + 5), "l": (None, h + 5)}]
    pipe, _ = make_pipeline(tmp_path, script, h + 5)
    result = pipe.run(polls=1)
    rec = next(r for r in result.report.records if r.feed_id == "numbered")
    assert rec.dropped_stale_prediction == 2
    assert result.stop_delays["stop_id"].tolist() == ["103N"]
    assert result.stop_delays["time_kind"].tolist() == ["actual"]


def test_invalid_payload_is_reported(tmp_path):
    h = at("09:59:00")
    script = [{"numbered": (b"garbage", h + 5), "l": (build_feed(h, [l_trip()]), h + 5)}]
    pipe, _ = make_pipeline(tmp_path, script, h + 5)
    result = pipe.run(polls=1)
    rec = next(r for r in result.report.records if r.feed_id == "numbered")
    assert rec.status == q.STATUS_INVALID


def test_multiple_polls_sleep_and_dedupe_within_minute_bucket(tmp_path):
    h = at("09:59:00")
    script = [
        {
            "numbered": (build_feed(h, [line_1_trip(delay_s=60)]), h + 2),
            "l": (build_feed(h, [l_trip()]), h + 2),
        },
        {
            "numbered": (build_feed(h + 30, [line_1_trip(delay_s=90)]), h + 32),
            "l": (build_feed(h + 30, [l_trip()]), h + 32),
        },
    ]
    pipe, clock = make_pipeline(tmp_path, script, h + 2)
    result = pipe.run(polls=2, interval_s=30)

    assert clock.sleeps == [30]
    assert result.report.polls_completed == 2
    # Both polls fall into the 09:59 bucket: the newer observation wins.
    assert result.report.bucket_duplicates_dropped["stop_delays"] == 4
    numbered = result.stop_delays[result.stop_delays["feed_id"] == "numbered"]
    assert numbered["delay_min"].tolist() == [1.5, 1.5, 1.5]
    assert result.report.bucket_duplicates_dropped["line_minute"] == 2


def test_delay_jump_across_runs_is_filtered(tmp_path):
    h = at("09:50:00")
    script = [
        {
            "numbered": (build_feed(h, [line_1_trip(delay_s=60)]), h + 2),
            "l": (build_feed(h, [l_trip()]), h + 2),
        },
        {
            "numbered": (build_feed(h + 120, [line_1_trip(delay_s=3000)]), h + 122),
            "l": (build_feed(h + 120, [l_trip()]), h + 122),
        },
    ]
    pipe, clock = make_pipeline(tmp_path, script, h + 2)
    pipe.run(polls=1)
    clock.t = h + 122
    result = pipe.run(polls=1)
    rec = next(r for r in result.report.records if r.feed_id == "numbered")
    assert rec.dropped_delay_jump_trips == 1
    assert rec.n_rows == 0
    assert any("delay jumped" in i for i in result.report.issues())


def test_save_raw_and_replay_reproduce_the_same_rows(tmp_path):
    h = at("09:59:00")
    script = [
        {
            "numbered": (build_feed(h, [line_1_trip(delay_s=30)]), h + 3),
            "l": (build_feed(h, [l_trip()]), h + 3),
        }
    ]
    live_dir = tmp_path / "live"
    pipe, _ = make_pipeline(live_dir, script, h + 3)
    pipe.save_raw = True
    live = pipe.run(polls=1)

    raw_files = sorted((live_dir / "raw").rglob("*.pb.gz"))
    assert [p.name for p in raw_files] == [f"l_{h}.pb.gz", f"numbered_{h}.pb.gz"]
    batches = load_replay_batches(raw_files)
    assert len(batches) == 1 and {r.feed_id for r in batches[0]} == {"l", "numbered"}

    replay_pipe, _ = make_pipeline(tmp_path / "replay", [], h + 3)
    replay = replay_pipe.run(batches=batches)
    cols = ["feed_id", "trip_id", "stop_id", "delay_min"]
    a = live.stop_delays[cols].sort_values(cols).reset_index(drop=True)
    b = replay.stop_delays[cols].sort_values(cols).reset_index(drop=True)
    assert a.equals(b)
