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
    from .sources import build_sources
    from .static_gtfs import load_schedule

    settings = _settings(args)
    names = [n.strip() for n in args.sources.split(",") if n.strip()]
    if args.replay and names != ["subway"]:
        # Replays are offline: only saved subway payloads are reprocessed.
        log.info("Replay mode: ingesting saved subway payloads only")
        names = ["subway"]
    feeds = resolve_feeds(args.feeds) if "subway" in names else []
    sources = build_sources([n for n in names if n != "subway"], settings)
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
        sources=sources,
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
    tables = args.tables.split(",") if args.tables else [*TABLES, "camera_counts"]
    for table in tables:
        done = compact(settings.data_dir, table, before)
        print(f"{table}: compacted {len(done)} partition(s) {' '.join(done)}".rstrip())
    return 0


def _now(args: argparse.Namespace) -> dt.datetime:
    """Reference time (naive UTC): ``--now`` or the current time."""
    raw = getattr(args, "now", None)
    t = dt.datetime.fromisoformat(raw) if raw else dt.datetime.now(dt.UTC)
    return t.astimezone(dt.UTC).replace(tzinfo=None) if t.tzinfo else t


def cmd_neighbors(args: argparse.Namespace) -> int:
    from .pipeline import refresh_neighbors
    from .static_gtfs import load_schedule

    settings = _settings(args)
    schedule, _ = load_schedule(settings)
    nb = refresh_neighbors(settings.data_dir, schedule.stops, settings.neighbor_radius_m, schedule)
    counts = nb.groupby("neighbor_type")["station_id"].agg(["size", "nunique"])
    print(counts.rename(columns={"size": "pairs", "nunique": "subway_stations"}).to_string())
    return 0


def cmd_baselines(args: argparse.Namespace) -> int:
    from .baselines import refresh_baselines
    from .storage import connect

    settings = _settings(args)
    con = connect(settings.data_dir)
    changed, b = refresh_baselines(con, settings, _now(args), force=args.force)
    print("recomputed" if changed else "baselines are fresh; not recomputed")
    if len(b):
        print(b.groupby(["metric", "confidence"]).size().rename("slots").to_string())
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    from .events import STATUS_ONGOING, process_events
    from .integration import EventCameraHook, save_events_report
    from .pipeline import make_run_id
    from .storage import connect, read_reference, write_table

    settings = _settings(args)
    now = _now(args)
    con = connect(settings.data_dir)
    baselines = read_reference(settings.data_dir, "baselines")
    run_id = make_run_id(now.replace(tzinfo=dt.UTC).timestamp())
    use_cameras = settings.event_cameras_enabled and not args.no_cameras
    hook = EventCameraHook(settings, now=now.replace(tzinfo=dt.UTC)) if use_cameras else None
    events = process_events(con, settings, baselines, now, run_id, camera_hook=hook)
    write_table(settings.data_dir, "events", events, run_id)
    report = {
        "run_id": run_id,
        "now_utc": now.isoformat(),
        "event_versions_written": len(events),
        "ongoing": int((events["status"] == STATUS_ONGOING).sum()) if len(events) else 0,
        "cameras": hook.report() if hook else {"enabled": False},
    }
    path = save_events_report(settings.data_dir, run_id, now, report)
    print(f"{len(events)} event version(s) written; report {path}")
    if hook:
        cams = report["cameras"]
        counted, cap = cams["cameras_counted"], cams["cap"]
        print(f"Cameras: {counted} counted (cap {cap}); {cams['status_counts']}")
        for err in cams["errors"]:
            print(f"  warning: {err}")
    for ev in events.to_dict("records"):
        print(f"- {ev['event_id']} [{ev['status']}] impact {ev['impact_score']}: {ev['summary']}")
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    from .site import build_site
    from .storage import connect, read_reference

    settings = _settings(args)
    out = Path(args.out) if args.out else settings.data_dir / "site"
    con = connect(settings.data_dir)
    index = build_site(
        con, settings, read_reference(settings.data_dir, "baselines"), out, _now(args)
    )
    print(f"Wrote {index}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import export_site_data
    from .storage import connect, read_reference

    settings = _settings(args)
    out = Path(args.out)
    con = connect(settings.data_dir)
    sizes = export_site_data(
        con, settings, read_reference(settings.data_dir, "baselines"), out, _now(args)
    )
    total = sum(sizes.values())
    print(f"Wrote {len(sizes)} JSON file(s) to {out} ({total / 1024:.0f} KB)")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from .storage import prune

    settings = _settings(args)
    keep = args.keep_days if args.keep_days is not None else settings.bike_retention_days
    removed = prune(settings.data_dir, args.table, keep, _now(args).date())
    print(f"{args.table}: removed {len(removed)} partition(s) older than {keep} days")
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
    ing.add_argument(
        "--sources",
        default="subway,alerts,traffic,bikes",
        help="comma separated: subway, alerts, traffic, bikes (default: all)",
    )
    ing.add_argument("--feeds", default="all", help="subway feed ids (comma separated) or 'all'")
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

    nbp = sub.add_parser("neighbors", help="recompute the 500 m spatial join")
    common(nbp)
    nbp.set_defaults(func=cmd_neighbors)

    bp = sub.add_parser("baselines", help="weekday x time-of-day baselines (if stale)")
    common(bp)
    bp.add_argument("--force", action="store_true", help="recompute even if fresh")
    bp.add_argument("--now", help="reference time, ISO (default: now)")
    bp.set_defaults(func=cmd_baselines)

    ep = sub.add_parser("events", help="detect delay events, link alerts, explain")
    common(ep)
    ep.add_argument("--now", help="reference time, ISO (default: now)")
    ep.add_argument(
        "--no-cameras", action="store_true", help="do not count cameras for ongoing events"
    )
    ep.set_defaults(func=cmd_events)

    sp = sub.add_parser("site", help="render event charts and index.html")
    common(sp)
    sp.add_argument("--out", help="output directory (default <data-dir>/site)")
    sp.add_argument("--now", help="reference time, ISO (default: now)")
    sp.set_defaults(func=cmd_site)

    xp = sub.add_parser("export", help="write the dashboard JSON (status, stations, events)")
    common(xp)
    xp.add_argument("--out", default="site/data", help="output folder (default site/data)")
    xp.add_argument("--now", help="reference time, ISO (default: now)")
    xp.set_defaults(func=cmd_export)

    pp = sub.add_parser("prune", help="delete old partitions of a large table")
    common(pp)
    pp.add_argument("--table", default="bike_minute")
    pp.add_argument("--keep-days", type=int, help="default: RIPPLE_BIKE_RETENTION_DAYS")
    pp.add_argument("--now", help="reference time, ISO (default: now)")
    pp.set_defaults(func=cmd_prune)
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
        # Configuration/input errors exit with 2; the traceback helps when it is a bug.
        log.error("%s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 2


if __name__ == "__main__":
    sys.exit(main())
