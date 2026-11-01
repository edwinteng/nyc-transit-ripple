"""Camera counting accuracy from manual labels (M6, part A3).

The M3 labeling workflow (``ripple-cameras sample-frames`` then ``evaluate``)
produces a labels CSV (true counts typed by a person) and a predictions CSV (model
counts per frame). Both contain numbers only, so they can be committed to
``docs/evaluation/`` while the frames themselves are deleted. This module reports
MAE and bias per class and model from those two files; without labels it says so
and skips.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..cameras.detector import COUNT_NAMES
from ..cameras.evaluation import error_metrics, load_labels
from ..config import Settings
from .common import STATUS_OK, STATUS_SKIPPED, Metric, Section, day


def evaluate_cameras(settings: Settings, root: Path | None = None) -> Section:
    root = Path(root or ".")
    labels_path = root / settings.eval_camera_labels
    preds_path = root / settings.eval_camera_predictions
    section = Section(
        "cameras",
        "Camera counting accuracy",
        STATUS_SKIPPED,
        None,
        None,
        "",
        notes=[
            "True counts come from a person labeling sampled frames "
            "(ripple-cameras sample-frames, then ripple-cameras evaluate); frames are "
            "deleted afterwards and only the numbers are kept.",
            "Bias = mean of (model - true): negative means the model under-counts.",
        ],
    )
    if not labels_path.exists():
        section.summary = (
            f"No manual labels yet ({settings.eval_camera_labels} does not exist); "
            "camera accuracy is not evaluated."
        )
        return section
    labels = load_labels(labels_path)
    if labels.empty:
        section.summary = "The labels file has no completed rows yet; skipped."
        return section
    if not preds_path.exists():
        section.summary = (
            f"Labels exist but no model counts ({settings.eval_camera_predictions}); run "
            "ripple-cameras evaluate and copy predictions.csv next to the labels."
        )
        return section
    preds = pd.read_csv(preds_path, dtype={"sample_id": str})
    section.status = STATUS_OK
    if "fetched_utc" in labels:
        t = pd.to_datetime(labels["fetched_utc"], errors="coerce")
        section.date_from, section.date_to = day(t.min()), day(t.max())
    rows = []
    for model, p in preds.groupby("model"):
        merged = labels.merge(p, on="sample_id", suffixes=("", "_pred"))
        if merged.empty:
            continue
        true = merged[list(COUNT_NAMES)]
        pred = merged[[f"{c}_pred" for c in COUNT_NAMES]].set_axis(list(COUNT_NAMES), axis=1)
        for r in error_metrics(true, pred).to_dict("records"):
            rows.append({"model": model, **r})
            section.metrics.append(
                Metric(
                    f"{model}_{r['class']}_mae",
                    f"{model} {r['class']}: MAE",
                    r["mae"],
                    r["n"],
                    "objects per frame",
                )
            )
            section.metrics.append(
                Metric(
                    f"{model}_{r['class']}_bias",
                    f"{model} {r['class']}: bias",
                    r["bias"],
                    r["n"],
                    "objects per frame",
                )
            )
    section.tables["per_class"] = rows
    n = len(labels)
    section.summary = f"{n} labeled frame(s); MAE and bias per class and model are listed below."
    return section
