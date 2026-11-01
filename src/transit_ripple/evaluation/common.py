"""Shared pieces of the evaluation report (M6): metrics with sample sizes, sections,
bootstrap confidence intervals."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_SKIPPED = "skipped"


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) or math.isinf(v) else round(float(v), 3)
    return v


@dataclass
class Metric:
    """One number with its sample size; ``ci`` is a 95% bootstrap interval if any."""

    key: str
    label: str
    value: float | None
    n: int
    unit: str = ""
    ci_low: float | None = None
    ci_high: float | None = None
    note: str | None = None

    def to_dict(self, min_sample: int) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": _clean(self.value),
            "n": int(self.n),
            "unit": self.unit,
            "ci_low": _clean(self.ci_low),
            "ci_high": _clean(self.ci_high),
            "note": self.note,
            "preliminary": int(self.n) < min_sample,
        }


@dataclass
class Section:
    key: str
    title: str
    status: str
    date_from: str | None
    date_to: str | None
    summary: str
    metrics: list[Metric] = field(default_factory=list)
    tables: dict[str, list[dict]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self, min_sample: int) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "summary": self.summary,
            "metrics": [m.to_dict(min_sample) for m in self.metrics],
            "tables": {
                k: [{c: _clean(v) for c, v in row.items()} for row in rows]
                for k, rows in self.tables.items()
            },
            "notes": self.notes,
        }


def day(ts) -> str | None:
    if ts is None or (isinstance(ts, float) and math.isnan(ts)) or pd.isna(ts):
        return None
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def bootstrap_mean_ci(
    values, n_boot: int = 2000, seed: int = 7, alpha: float = 0.05
) -> tuple[float | None, float | None, float | None]:
    """Mean and percentile bootstrap interval of the mean (None if fewer than 2 values)."""
    x = np.asarray([v for v in values if v is not None and not math.isnan(v)], dtype=float)
    if len(x) == 0:
        return None, None, None
    mean = float(x.mean())
    if len(x) < 2:
        return mean, None, None
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n_boot, len(x)), replace=True).mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return mean, float(lo), float(hi)


def set_span(section: Section, *series) -> None:
    """Set the section's date range to the span of the data actually used."""
    parts = [pd.to_datetime(pd.Series(x)) for x in series if len(x)]
    if not parts:
        return
    values = pd.concat(parts, ignore_index=True).dropna()
    if len(values):
        section.date_from, section.date_to = day(values.min()), day(values.max())


def ratio(num: int, den: int) -> float | None:
    return num / den if den else None
