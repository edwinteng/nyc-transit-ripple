"""Surface impact score (0-100) from nearby traffic, camera counts and Citi Bikes.

Each signal is compared with its same-weekday / same-time-of-day baseline:

- ``traffic``: drop of the average nearby traffic speed (slower = more impact);
- ``camera``: increase of vehicles + people counted by nearby cameras;
- ``bikes``: drop of Citi Bikes available nearby (riders switching to bikes).

A signal's sub-score is its change scaled linearly so that the configured
"full" change (e.g. a 40% speed drop) gives 100, clipped to 0-100. Changes in
the "good" direction give 0. The score is the weighted mean of the sub-scores of
the signals that are usable; weights are renormalized over those signals, so a
missing signal never counts as "no impact". A signal is unusable when it has no
observation, no baseline, or a baseline below the minimum confidence. With no
usable signal the score is undefined (None), never 0.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .config import Settings

SIGNALS = ("traffic", "camera", "bikes")
CONFIDENCE_RANK = {"none": 0, "low": 1, "ok": 2}

STATUS_USED = "used"
STATUS_NO_DATA = "no_data"
STATUS_NO_BASELINE = "no_baseline"
STATUS_LOW_CONFIDENCE = "low_confidence"
STATUS_ZERO_WEIGHT = "zero_weight"


@dataclass
class SignalInput:
    observed: float | None
    baseline: float | None
    confidence: str = "none"


@dataclass
class SignalResult:
    status: str
    observed: float | None
    baseline: float | None
    confidence: str
    change_pct: float | None = None
    sub_score: float | None = None
    weight: float = 0.0
    effective_weight: float | None = None


@dataclass
class ImpactResult:
    score: float | None
    used: list[str]
    signals: dict[str, SignalResult]

    def detail(self) -> dict:
        return {name: asdict(r) for name, r in self.signals.items()}


def weights(settings: Settings) -> dict[str, float]:
    return {
        "traffic": settings.impact_weight_traffic,
        "camera": settings.impact_weight_camera,
        "bikes": settings.impact_weight_bikes,
    }


def _present(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def change_pct(observed: float, baseline: float) -> float:
    return (observed - baseline) / baseline * 100.0


def sub_score(signal: str, pct: float, settings: Settings) -> float:
    """0-100 sub-score of a percentage change vs. baseline."""
    if signal == "traffic":
        raw = -pct / settings.impact_traffic_full_drop_pct
    elif signal == "camera":
        raw = pct / settings.impact_camera_full_increase_pct
    elif signal == "bikes":
        raw = -pct / settings.impact_bikes_full_drop_pct
    else:
        raise ValueError(f"unknown signal {signal!r}")
    return float(min(100.0, max(0.0, raw * 100.0)))


def evaluate_signal(
    signal: str, inp: SignalInput, weight: float, settings: Settings
) -> SignalResult:
    res = SignalResult(STATUS_USED, inp.observed, inp.baseline, inp.confidence, weight=weight)
    min_rank = CONFIDENCE_RANK.get(settings.impact_min_confidence, 2)
    if not _present(inp.observed):
        res.status = STATUS_NO_DATA
    elif not _present(inp.baseline) or inp.baseline <= 0:
        res.status = STATUS_NO_BASELINE
    elif CONFIDENCE_RANK.get(inp.confidence, 0) < min_rank:
        res.status = STATUS_LOW_CONFIDENCE
    elif weight <= 0:
        res.status = STATUS_ZERO_WEIGHT
    if res.status in (STATUS_USED, STATUS_LOW_CONFIDENCE, STATUS_ZERO_WEIGHT):
        res.change_pct = round(change_pct(inp.observed, inp.baseline), 1)
        res.sub_score = round(sub_score(signal, res.change_pct, settings), 1)
    return res


def impact_score(inputs: dict[str, SignalInput], settings: Settings) -> ImpactResult:
    """Weighted, renormalized 0-100 score over the usable signals."""
    w = weights(settings)
    results = {
        name: evaluate_signal(name, inputs.get(name, SignalInput(None, None)), w[name], settings)
        for name in SIGNALS
    }
    used = [n for n, r in results.items() if r.status == STATUS_USED]
    total = sum(results[n].weight for n in used)
    if not used or total <= 0:
        return ImpactResult(None, [], results)
    score = 0.0
    for n in used:
        results[n].effective_weight = round(results[n].weight / total, 4)
        score += results[n].sub_score * results[n].weight / total
    return ImpactResult(round(score, 1), used, results)


# Per-minute columns of the surface series: (observed, baseline) per signal.
SERIES_COLUMNS = {
    "traffic": ("traffic_speed_cmp", "traffic_baseline_mph"),
    "camera": ("camera_objects_cmp", "camera_objects_baseline"),
    "bikes": ("bikes_cmp", "bikes_baseline"),
}


def impact_series(series: pd.DataFrame, used: list[str], settings: Settings) -> pd.Series:
    """Per-minute score over the signals used for the event (renormalized per minute)."""
    w = weights(settings)
    num = pd.Series(0.0, index=series.index)
    den = pd.Series(0.0, index=series.index)
    for name in used:
        obs_col, base_col = SERIES_COLUMNS[name]
        if obs_col not in series or base_col not in series:
            continue
        obs = series[obs_col].astype(float)
        base = series[base_col].astype(float)
        ok = obs.notna() & base.notna() & (base > 0)
        pct = (obs - base) / base * 100.0
        sub = pct.map(lambda p, n=name: sub_score(n, p, settings) if pd.notna(p) else np.nan)
        num = num + (sub * w[name]).where(ok, 0.0)
        den = den + pd.Series(np.where(ok, w[name], 0.0), index=series.index)
    out = (num / den).where(den > 0)
    return out.round(1)


def surface_recovery(
    minutes: pd.Series,
    scores: pd.Series,
    after: pd.Timestamp,
    settings: Settings,
) -> pd.Timestamp | None:
    """First minute at or after ``after`` that starts a run of ``impact_recovery_minutes``
    minutes with a known score below the recovery threshold."""
    k = settings.impact_recovery_minutes
    thr = settings.impact_recovery_threshold
    df = pd.DataFrame({"t": pd.to_datetime(minutes).to_numpy(), "s": scores.to_numpy()})
    df = df[df["t"] >= pd.Timestamp(after)].reset_index(drop=True)
    run_start, run = None, 0
    for row in df.itertuples(index=False):
        if pd.notna(row.s) and row.s < thr:
            if run == 0:
                run_start = row.t
            run += 1
            if run >= k:
                return pd.Timestamp(run_start)
        else:
            run, run_start = 0, None
    return None
