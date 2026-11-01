"""Command line interface: ``ripple <command>``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from .config import Settings, load_dotenv, resolve_feeds

log = logging.getLogger("transit_ripple")


def _settings(args: argparse.Namespace) -> Settings:
    base = Settings.from_env()
    return base.with_overrides(
        data_dir=Path(args.data_dir) if getattr(args, "data_dir", None) else None,
        static_dir=Path(args.static_dir) if getattr(args, "static_dir", None) else None,
        static_url=getattr(args, "static_url", None),
    )


def cmd_ingest(args: argparse.Namespace) -> int:
    from .feeds import HttpFetcher, load_replay_batches
    from .pipeline import Pipeline
    from .static_gtfs import load_schedule

    settings = _settings(args)
    feeds = resolve_feeds(args.feeds)
    schedule, info = load_schedule(settings, force_refresh=args.refresh_static)
    batches = None
    if args.replay:
        paths = sorted(Path(p) for p in args.replay)
        batches = load_replay_batches(paths)
        known = {f.feed_id for f in feeds}
        batches = [[r for r in b if r.feed_id in known] for b in batches]
    pipeline = Pipeline(
        settings,
        schedule,
        info,
        feeds,
        fetcher=HttpFetcher(settings),
        save_raw=args.save_raw,
    )
    result = pipeline.run(polls=args.polls, interval_s=args.interval, batches=batches)
    md = result.report.to_markdown()
    if args.report_md:
        Path(args.report_md).write_text(md)
    print(md)
    print(f"Report: {result.report_path}")
    status = result.report.overall_status()
    if args.fail_on_outage and status == "failed":
        return 3
    return 0


def cmd_static(args: argparse.Namespace) -> int:
    from .static_gtfs import ensure_static

    info = ensure_static(_settings(args), force=args.force)
    print(json.dumps(info.to_report(), indent=2))
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    from .storage import TABLES, compact

    settings = _settings(args)
    before = dt.date.fromisoformat(args.before) if args.before else dt.datetime.now(dt.UTC).date()
    tables = args.tables.split(",") if args.tables else list(TABLES)
    for table in tables:
        done = compact(settings.data_dir, table, before)
        print(f"{table}: compacted {len(done)} partition(s) {' '.join(done)}".rstrip())
    return 0


def _print_frame(df, fmt: str) -> None:
    if fmt == "csv":
        df.to_csv(sys.stdout, index=False)
    elif fmt == "json":
        print(df.to_json(orient="records", date_format="iso", indent=2))
    else:
        import pandas as pd

        with pd.option_context(
            "display.max_rows", 500, "display.max_columns", 50, "display.width", 200
        ):
            print(df.to_string(index=False))


def cmd_query(args: argparse.Namespace) -> int:
    from .storage import connect

    con = connect(_settings(args).data_dir)
    _print_frame(con.execute(args.sql).df(), args.format)
    return 0


def cmd_lines(args: argparse.Namespace) -> int:
    from .storage import connect, line_minute_filled

    end = dt.datetime.fromisoformat(args.end) if args.end else dt.datetime.now(dt.UTC)
    start = dt.datetime.fromisoformat(args.start) if args.start else end - dt.timedelta(minutes=60)
    # Stored timestamps are naive UTC.
    start = start.astimezone(dt.UTC).replace(tzinfo=None) if start.tzinfo else start
    end = end.astimezone(dt.UTC).replace(tzinfo=None) if end.tzinfo else end
    con = connect(_settings(args).data_dir)
    df = line_minute_filled(con, start, end, max_age_min=args.max_age)
    if args.route:
        df = df[df["route_id"].isin(args.route.split(","))]
    cols = [
        "minute_utc",
        "feed_id",
        "route_id",
        "direction",
        "fill_status",
        "age_min",
        "n_trips_measured",
        "delay_mean_min",
        "delay_p90_min",
        "share_trips_delayed",
    ]
    _print_frame(df[cols] if args.format == "table" else df, args.format)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ripple", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--data-dir", help="output directory (env RIPPLE_DATA_DIR, default ./data)")
        sp.add_argument("--static-dir", help="static GTFS cache (default <data-dir>/static)")
        sp.add_argument("--static-url", help="static GTFS zip URL or local path")

    ing = sub.add_parser("ingest", help="fetch subway feeds and store delays + quality report")
    common(ing)
    ing.add_argument("--feeds", default="all", help="comma separated feed ids or 'all'")
    ing.add_argument("--polls", type=int, default=1, help="snapshots to take in this run")
    ing.add_argument("--interval", type=float, default=30.0, help="seconds between snapshots")
    ing.add_argument("--save-raw", action="store_true", help="keep raw payloads under raw/")
    ing.add_argument(
        "--replay",
        nargs="+",
        metavar="FILE",
        help="process saved <feed>_<ts>.pb[.gz] files instead of fetching",
    )
    ing.add_argument("--report-md", help="also write the Markdown report to this path")
    ing.add_argument("--refresh-static", action="store_true", help="force static GTFS refresh")
    ing.add_argument(
        "--fail-on-outage",
        action="store_true",
        help="exit with code 3 when no feed produced usable data",
    )
    ing.set_defaults(func=cmd_ingest)

    st = sub.add_parser("static", help="download and convert the static GTFS schedule")
    common(st)
    st.add_argument("--force", action="store_true")
    st.set_defaults(func=cmd_static)

    cp = sub.add_parser("compact", help="merge per-run Parquet files of past days")
    common(cp)
    cp.add_argument("--before", help="compact partitions before this date (default: today UTC)")
    cp.add_argument("--tables", help="comma separated tables (default: all)")
    cp.set_defaults(func=cmd_compact)

    qp = sub.add_parser("query", help="run SQL against the DuckDB views")
    common(qp)
    qp.add_argument("sql")
    qp.add_argument("--format", choices=("table", "csv", "json"), default="table")
    qp.set_defaults(func=cmd_query)

    lp = sub.add_parser("lines", help="forward-filled 1-minute line delays with freshness")
    common(lp)
    lp.add_argument("--start", help="ISO start time (default: end - 60 min)")
    lp.add_argument("--end", help="ISO end time (default: now)")
    lp.add_argument("--max-age", type=int, default=10, help="max minutes to forward-fill")
    lp.add_argument("--route", help="comma separated route ids")
    lp.add_argument("--format", choices=("table", "csv", "json"), default="table")
    lp.set_defaults(func=cmd_lines)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except ValueError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
