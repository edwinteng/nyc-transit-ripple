"""Camera commands: ``python -m transit_ripple.cameras <command>`` (or ``ripple-cameras``)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from .config import CameraSettings

log = logging.getLogger("transit_ripple.cameras")


def _settings(args) -> CameraSettings:
    s = CameraSettings.from_env()
    return s.with_overrides(
        data_dir=Path(args.data_dir) if getattr(args, "data_dir", None) else None,
        model=getattr(args, "model", None),
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _client(settings):
    from .client import PoliteClient

    return PoliteClient(settings)


def _inventory(settings, client, force=False):
    from .inventory import refresh_inventory

    result = refresh_inventory(settings, client, _now(), force=force)
    if result.error:
        log.warning("Inventory refresh failed (%s); using stored inventory", result.error)
    return result


def _print(df: pd.DataFrame) -> None:
    with pd.option_context(
        "display.max_rows", 200, "display.width", 200, "display.max_columns", 30
    ):
        print(df.to_string(index=False))


def cmd_inventory(args) -> int:
    settings = _settings(args)
    result = _inventory(settings, _client(settings), force=args.force)
    cams = result.cameras
    state = "refreshed" if result.refreshed else f"stored (age {result.age_days:.1f} days)"
    print(f"{len(cams)} cameras, {state}; invalid rows dropped: {result.n_invalid}")
    print(cams.groupby("area", dropna=False).size().rename("cameras").to_string())
    return 0


def cmd_map(args) -> int:
    from ..config import Settings as PipelineSettings
    from .mapping import build_mapping, load_stops

    settings = _settings(args)
    cams = _inventory(settings, _client(settings)).cameras
    m1 = PipelineSettings.from_env().with_overrides(data_dir=settings.data_dir)
    mapping = build_mapping(settings.data_dir, load_stops(m1), cams, settings.radius_m)
    n_stations = mapping["station_id"].nunique()
    print(
        f"{len(mapping)} station-camera pairs within {settings.radius_m:g} m; "
        f"{n_stations} stations have at least one camera; "
        f"{mapping['camera_id'].nunique()} cameras are near a station"
    )
    return 0


def _select_cameras(args, settings) -> pd.DataFrame:
    from .storage import read_reference

    cams = read_reference(settings.data_dir, "cameras")
    mapping = read_reference(settings.data_dir, "camera_station_map")
    if args.camera:
        chosen = cams[cams["camera_id"].isin(args.camera)]
    else:
        if mapping.empty:
            raise ValueError("No camera/station mapping; run the `map` command first.")
        m = mapping[mapping["station_id"].isin(args.station)] if args.station else mapping
        nearest = m.sort_values("distance_m").drop_duplicates("camera_id")
        chosen = cams.merge(nearest[["camera_id", "distance_m"]], on="camera_id").sort_values(
            ["distance_m", "camera_id"]
        )
    return chosen.head(args.max_cameras)


def cmd_count(args) -> int:
    from .client import PoliteClient
    from .counting import CameraCounter, save_report
    from .detector import load_detector
    from .model import get_spec
    from .storage import write_counts

    settings = _settings(args)
    get_spec(settings.model)  # fail fast, before any network request
    client = PoliteClient(settings)
    _inventory(settings, client)
    cameras = _select_cameras(args, settings)
    counter = CameraCounter(settings, client, load_detector(settings))
    rows, report = counter.count_cameras(cameras)
    if len(rows):
        write_counts(settings.data_dir, rows, report.run_id)
    path = save_report(settings.data_dir, report)
    md = report.to_markdown()
    if args.report_md:
        Path(args.report_md).write_text(md)
    print(md)
    print(f"Report: {path}")
    return 0 if report.overall_status() != "failed" else 3


def cmd_near(args) -> int:
    from .interface import count_near_station, summarize_station_counts

    settings = _settings(args)
    at = dt.datetime.fromisoformat(args.at) if args.at else None
    df = count_near_station(args.station_id, at, settings=settings)
    _print(df)
    print(json.dumps(summarize_station_counts(df)))
    return 0


def cmd_benchmark(args) -> int:
    from .benchmark import benchmark_markdown, run_benchmark
    from .client import CameraApiError
    from .detector import load_detector

    settings = _settings(args)
    detector = load_detector(settings)
    live_frames, live_fetch = [], []
    if args.live:
        client = _client(settings)
        cams = _inventory(settings, client).cameras.head(args.live)
        for url in cams["image_url"]:
            try:
                live_frames.append(client.get_bytes(url))
            except CameraApiError:
                continue
            live_fetch.append(client.last_elapsed_ms)  # excludes the politeness wait
    result = run_benchmark(detector, settings, args.frames, args.budget_s, live_frames, live_fetch)
    del live_frames  # frames only lived in memory for timing
    md = benchmark_markdown(result)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
    if args.report_md:
        Path(args.report_md).write_text(md)
    print(md)
    return 0


def cmd_sample_frames(args) -> int:
    from .evaluation import sample_frames

    settings = _settings(args)
    client = _client(settings)
    cams = _inventory(settings, client).cameras
    out = Path(args.out) if args.out else settings.data_dir / "labeling"
    labels = sample_frames(client, cams, args.n, out, seed=args.seed)
    n = len(pd.read_csv(labels))
    print(f"Saved {n} frames and {labels} (local only; do not commit; delete after labeling).")
    return 0


def cmd_evaluate(args) -> int:
    from .detector import load_detector
    from .evaluation import evaluate, evaluation_markdown

    settings = _settings(args)
    models = [m.strip() for m in (args.models or settings.model).split(",") if m.strip()]
    detectors = {m: load_detector(settings.with_overrides(model=m)) for m in models}
    results, preds = evaluate(Path(args.labels), detectors)
    out = Path(args.labels).parent
    (out / "evaluation.json").write_text(json.dumps(results, indent=2))
    preds.to_csv(out / "predictions.csv", index=False)
    print(evaluation_markdown(results))
    print(f"Wrote {out / 'evaluation.json'} and {out / 'predictions.csv'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ripple-cameras", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, model=False):
        sp.add_argument("--data-dir", help="data directory (env RIPPLE_DATA_DIR, default ./data)")
        if model:
            sp.add_argument("--model", help="yolox_s (default), yolox_tiny or yolox_nano")

    s = sub.add_parser("inventory", help="refresh the camera list (weekly unless --force)")
    common(s)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_inventory)

    s = sub.add_parser("map", help="map cameras to subway stations within the radius")
    common(s)
    s.set_defaults(func=cmd_map)

    s = sub.add_parser("count", help="count cars/buses/trucks/people at cameras")
    common(s, model=True)
    s.add_argument("--station", nargs="*", default=[], help="station ids (default: all mapped)")
    s.add_argument("--camera", nargs="*", default=[], help="explicit camera ids")
    s.add_argument("--max-cameras", type=int, default=60)
    s.add_argument("--report-md", help="also write the Markdown report here")
    s.set_defaults(func=cmd_count)

    s = sub.add_parser("near", help="count_near_station(station_id, timestamp)")
    common(s, model=True)
    s.add_argument("station_id")
    s.add_argument("--at", help="ISO timestamp (default: now = live counting)")
    s.set_defaults(func=cmd_near)

    s = sub.add_parser("benchmark", help="measure inference time per frame")
    common(s, model=True)
    s.add_argument("--frames", type=int, default=50, help="synthetic frames")
    s.add_argument("--live", type=int, default=0, help="also time this many live frames")
    s.add_argument("--budget-s", type=float, default=240.0, help="run budget for the estimate")
    s.add_argument("--json", help="write the result as JSON")
    s.add_argument("--report-md", help="write the Markdown summary")
    s.set_defaults(func=cmd_benchmark)

    s = sub.add_parser("sample-frames", help="save frames + labels.csv for manual labeling")
    common(s)
    s.add_argument("--n", type=int, default=50)
    s.add_argument("--out", help="output folder (default <data-dir>/labeling, gitignored)")
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(func=cmd_sample_frames)

    s = sub.add_parser("evaluate", help="compare model counts with labels.csv (MAE per class)")
    common(s)
    s.add_argument("--labels", required=True)
    s.add_argument("--models", help="comma separated (default: the configured model)")
    s.set_defaults(func=cmd_evaluate)
    return p


def main(argv: list[str] | None = None) -> int:
    from ..config import load_dotenv

    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except ValueError as exc:
        log.error("%s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 2


if __name__ == "__main__":
    sys.exit(main())
