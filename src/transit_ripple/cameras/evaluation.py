"""Accuracy evaluation scaffolding: sample frames for manual labeling, then score.

1. ``sample_frames`` saves N camera frames into a **local** folder (default
   ``data/labeling``, gitignored) together with ``labels.csv``. The count columns
   are left empty for a person to fill in (true counts). Model counts are not
   written to that file, so labelers are not anchored by them.
2. ``evaluate`` runs one or more models on the labeled frames and reports, per
   class, the mean absolute error (MAE), the mean error (bias: positive means the
   model over-counts), RMSE and the share of frames counted exactly.

These local frames are the only images this project ever writes to disk; they
exist solely for labeling, must not be committed or shared, and should be deleted
after labeling.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from .client import CameraApiError, PoliteClient
from .detector import COUNT_NAMES, InvalidImage, decode_image

LABEL_COLUMNS = [
    "sample_id",
    "file",
    "camera_id",
    "camera_name",
    "area",
    "fetched_utc",
    *COUNT_NAMES,
    "notes",
]
README_TEXT = """Local frames for manual labeling (nyc-transit-ripple, M3).

Fill in labels.csv: for each frame, count the visible cars, buses, trucks and
people (whole or partly visible objects that a person would count). Leave a row
empty to skip it. Then run:

    python -m transit_ripple.cameras evaluate --labels labels.csv

Do not commit or share these images, and delete them once labeling is done.
"""


def choose_sample(cameras: pd.DataFrame, n: int, seed: int = 0) -> pd.DataFrame:
    """Stratified random sample by borough (``area``), proportional to camera counts."""
    cams = (
        cameras[cameras["is_online"].fillna(True).astype(bool)]
        if "is_online" in cameras
        else cameras
    )
    if len(cams) <= n:
        return cams.sample(frac=1, random_state=seed).reset_index(drop=True)
    shares = cams["area"].fillna("unknown").value_counts(normalize=True)
    quotas = {area: max(1, round(share * n)) for area, share in shares.items()}
    parts = []
    for area, k in quotas.items():
        group = cams[cams["area"].fillna("unknown") == area]
        parts.append(group.sample(min(k, len(group)), random_state=seed))
    sample = pd.concat(parts).sample(frac=1, random_state=seed)
    return sample.head(n).reset_index(drop=True)


def sample_frames(
    client: PoliteClient,
    cameras: pd.DataFrame,
    n: int,
    out_dir: Path,
    seed: int = 0,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> Path:
    """Fetch up to ``n`` frames (politely) into ``out_dir`` and write labels.csv."""
    out_dir = Path(out_dir)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "README.txt").write_text(README_TEXT)
    rows = []
    for cam in choose_sample(cameras, n, seed).itertuples(index=False):
        try:
            data = client.get_bytes(cam.image_url)
            decode_image(data)  # only keep frames that are real images
        except (CameraApiError, InvalidImage):
            continue
        sample_id = f"s{len(rows) + 1:03d}"
        path = frames_dir / f"{sample_id}.jpg"
        path.write_bytes(data)
        rows.append(
            {
                "sample_id": sample_id,
                "file": f"frames/{path.name}",
                "camera_id": cam.camera_id,
                "camera_name": cam.name,
                "area": cam.area,
                "fetched_utc": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                **dict.fromkeys(COUNT_NAMES, ""),
                "notes": "",
            }
        )
    labels = out_dir / "labels.csv"
    pd.DataFrame(rows, columns=LABEL_COLUMNS).to_csv(labels, index=False)
    return labels


def load_labels(path: Path) -> pd.DataFrame:
    """Rows whose four counts are all filled in with non-negative integers."""
    df = pd.read_csv(path, dtype={"sample_id": str, "file": str})
    for name in COUNT_NAMES:
        df[name] = pd.to_numeric(df[name], errors="coerce")
    complete = df[list(COUNT_NAMES)].notna().all(axis=1)
    df = df[complete].copy()
    bad = (df[list(COUNT_NAMES)] < 0).any(axis=1) | (df[list(COUNT_NAMES)] % 1 != 0).any(axis=1)
    if bad.any():
        raise ValueError(
            f"labels must be non-negative integers (rows {df.loc[bad, 'sample_id'].tolist()})"
        )
    df[list(COUNT_NAMES)] = df[list(COUNT_NAMES)].astype(int)
    return df.reset_index(drop=True)


def error_metrics(true: pd.DataFrame, pred: pd.DataFrame) -> pd.DataFrame:
    """Per-class MAE, bias (pred - true), RMSE and exact-count share."""
    rows = []
    for name in COUNT_NAMES:
        t = true[name].to_numpy(dtype=float)
        p = pred[name].to_numpy(dtype=float)
        err = p - t
        rows.append(
            {
                "class": name,
                "n": len(t),
                "mae": round(float(np.abs(err).mean()), 3) if len(t) else math.nan,
                "bias": round(float(err.mean()), 3) if len(t) else math.nan,
                "rmse": round(float(np.sqrt((err**2).mean())), 3) if len(t) else math.nan,
                "exact_share": round(float((err == 0).mean()), 3) if len(t) else math.nan,
                "true_total": int(t.sum()),
                "pred_total": int(p.sum()),
            }
        )
    return pd.DataFrame(rows)


def evaluate(labels_path: Path, detectors: dict) -> tuple[dict, pd.DataFrame]:
    """Score each detector on the labeled frames.

    Returns ({model: {"per_class": [...], "mae_mean": x}}, predictions frame).
    """
    labels_path = Path(labels_path)
    labels = load_labels(labels_path)
    if labels.empty:
        raise ValueError("no labeled rows: fill in the count columns of labels.csv first")
    preds, results = [], {}
    for name, det in detectors.items():
        counts = []
        for row in labels.itertuples(index=False):
            data = (labels_path.parent / row.file).read_bytes()
            counts.append(det.count(data).counts)
        pred = pd.DataFrame(counts, columns=list(COUNT_NAMES))
        metrics = error_metrics(labels, pred)
        results[name] = {
            "n_frames": len(labels),
            "mae_mean": round(float(metrics["mae"].mean()), 3),
            "per_class": metrics.to_dict("records"),
        }
        preds.append(pred.assign(model=name, sample_id=labels["sample_id"].to_numpy()))
    return results, pd.concat(preds, ignore_index=True)


def evaluation_markdown(results: dict) -> str:
    lines = ["## Camera count accuracy", ""]
    for model, r in results.items():
        lines += [
            f"### `{model}` ({r['n_frames']} labeled frames, mean MAE {r['mae_mean']})",
            "",
            "| Class | MAE | Bias | RMSE | Exact | True total | Predicted total |",
            "|---|---|---|---|---|---|---|",
        ]
        for m in r["per_class"]:
            lines.append(
                f"| {m['class']} | {m['mae']} | {m['bias']:+} | {m['rmse']} "
                f"| {m['exact_share']:.0%} | {m['true_total']} | {m['pred_total']} |"
            )
        lines.append("")
    return "\n".join(lines)
