from pathlib import Path

import pytest

from builders import at, build_feed, line_1_trip, write_static_zip
from transit_ripple.cli import main
from transit_ripple.config import SUBWAY_FEEDS, Settings, load_dotenv, resolve_feeds
from transit_ripple.feeds import make_session, raw_file_name


def test_all_subway_lines_are_covered():
    assert len(SUBWAY_FEEDS) == 8
    routes = {r for spec in SUBWAY_FEEDS.values() for r in spec.routes}
    for r in (
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "A",
        "C",
        "E",
        "B",
        "D",
        "F",
        "M",
        "G",
        "J",
        "Z",
        "L",
        "N",
        "Q",
        "R",
        "W",
        "GS",
        "FS",
        "H",
        "SI",
    ):
        assert r in routes
    assert SUBWAY_FEEDS["ace"].url.endswith("nyct%2Fgtfs-ace")


def test_resolve_feeds():
    assert len(resolve_feeds("all")) == 8
    assert [f.feed_id for f in resolve_feeds("ace, L")] == ["ace", "l"]
    with pytest.raises(ValueError, match="Unknown feed"):
        resolve_feeds("ace,xyz")


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPPLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RIPPLE_STALE_AFTER_S", "90")
    monkeypatch.setenv("RIPPLE_MAX_DELAY_MIN", "120.5")
    monkeypatch.setenv("MTA_API_KEY", "secret-value")
    s = Settings.from_env()
    assert s.data_dir == tmp_path and s.stale_after_s == 90 and s.max_delay_min == 120.5
    assert s.resolved_static_dir == tmp_path / "static"
    assert s.mta_api_key == "secret-value"
    # Secrets never show up in reprs, logs or reports.
    assert "secret-value" not in repr(s)
    assert "secret-value" not in str(s.thresholds())
    assert make_session(s).headers["x-api-key"] == "secret-value"


def test_settings_invalid_env_value(monkeypatch):
    monkeypatch.setenv("RIPPLE_STALE_AFTER_S", "soon")
    with pytest.raises(ValueError, match="RIPPLE_STALE_AFTER_S"):
        Settings.from_env()


def test_no_api_key_header_by_default(monkeypatch):
    monkeypatch.delenv("MTA_API_KEY", raising=False)
    assert "x-api-key" not in make_session(Settings.from_env()).headers


def test_load_dotenv_does_not_override_existing_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nRIPPLE_TEST_A=from_file\nexport RIPPLE_TEST_B='quoted'\nRIPPLE_TEST_C=x\n"
    )
    monkeypatch.setenv("RIPPLE_TEST_C", "from_env")
    monkeypatch.delenv("RIPPLE_TEST_A", raising=False)
    monkeypatch.delenv("RIPPLE_TEST_B", raising=False)
    loaded = load_dotenv(env)
    import os

    assert sorted(loaded) == ["RIPPLE_TEST_A", "RIPPLE_TEST_B"]
    assert os.environ["RIPPLE_TEST_A"] == "from_file"
    assert os.environ["RIPPLE_TEST_B"] == "quoted"
    assert os.environ["RIPPLE_TEST_C"] == "from_env"
    assert load_dotenv(tmp_path / "missing.env") == []


def test_cli_replay_query_lines_and_compact(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env is picked up
    zip_path = write_static_zip(tmp_path / "gtfs.zip")
    h = at("09:59:00")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / raw_file_name("numbered", h).removesuffix(".gz")).write_bytes(
        build_feed(h, [line_1_trip(delay_s=180)])
    )
    data = tmp_path / "data"
    common = ["--data-dir", str(data), "--static-url", str(zip_path)]

    assert (
        main(
            [
                "ingest",
                *common,
                "--feeds",
                "numbered",
                "--replay",
                *map(str, raw.iterdir()),
                "--report-md",
                str(tmp_path / "report.md"),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Overall status: **ok**" in out
    assert Path(tmp_path / "report.md").read_text().startswith("## Subway ingestion quality report")

    assert (
        main(
            [
                "query",
                *common,
                "SELECT round(avg(delay_min), 2) AS d FROM stop_delays",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip().splitlines() == ["d", "3.0"]

    assert (
        main(
            [
                "lines",
                *common,
                "--start",
                "2026-11-02T14:59:00+00:00",
                "--end",
                "2026-11-02T15:01:00+00:00",
                "--route",
                "1",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    lines_csv = capsys.readouterr().out
    assert "observed" in lines_csv and "filled" in lines_csv

    assert main(["compact", *common, "--before", "2026-11-01"]) == 0
    assert "compacted 0 partition(s)" in capsys.readouterr().out

    assert main(["ingest", *common, "--feeds", "nope"]) == 2
