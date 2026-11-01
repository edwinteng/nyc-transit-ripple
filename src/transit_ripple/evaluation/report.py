"""Evaluation report (M6, part A7): JSON for the site and Markdown for docs/evaluation/.

Every metric carries its sample size and every section its date range; metrics
with fewer than ``eval_min_sample`` observations are marked *preliminary*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ..config import Settings
from .cameras import evaluate_cameras
from .common import Section
from .detection import evaluate_detection
from .explanations import evaluate_explanations
from .placebo import evaluate_placebo
from .reliability import evaluate_reliability
from .replay import evaluate_recommendations

SCHEMA_VERSION = 1


def build_report(con, settings: Settings, now, root: Path | None = None, github: bool = True):
    """Run every evaluation over the last ``eval_window_days`` days."""
    end = pd.Timestamp(now).floor("min")
    start = end - pd.Timedelta(days=settings.eval_window_days)
    sections: list[Section] = []
    for fn in (
        lambda: evaluate_detection(con, settings, start, end),
        lambda: evaluate_placebo(con, settings, start, end),
        lambda: evaluate_cameras(settings, root),
        lambda: evaluate_recommendations(con, settings, start, end),
        lambda: evaluate_explanations(con, settings, start, end),
        lambda: evaluate_reliability(con, settings, start, end, github=github),
    ):
        sections.append(fn())
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": (end - pd.Timedelta(seconds=1)).strftime("%Y-%m-%d"),
        "window_days": settings.eval_window_days,
        "min_sample": settings.eval_min_sample,
        "sections": [s.to_dict(settings.eval_min_sample) for s in sections],
    }


def explanation_violations(report: dict) -> int:
    for s in report["sections"]:
        if s["key"] == "explanations":
            return int(next(m["value"] for m in s["metrics"] if m["key"] == "violations") or 0)
    return 0


def _fmt(v, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "share":
        return f"{v:.0%}"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def to_markdown(report: dict) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"Generated {report['generated_utc']} for **{report['date_from']} to "
        f"{report['date_to']}** ({report['window_days']} days). Metrics based on fewer than "
        f"{report['min_sample']} observations are marked *preliminary*.",
        "",
        "Generated automatically by `ripple evaluate` (weekly GitHub Actions workflow); do "
        "not edit by hand.",
        "",
    ]
    for s in report["sections"]:
        lines += [f"## {s['title']}", ""]
        period = f"{s['date_from']} to {s['date_to']}" if s["date_from"] else "not time-bound"
        lines += [f"*Status: {s['status'].replace('_', ' ')}; data: {period}.*", ""]
        lines += [s["summary"], ""]
        if s["metrics"]:
            lines += ["| Metric | Value | 95% interval | n | Note |", "|---|---|---|---|---|"]
            for m in s["metrics"]:
                ci = (
                    f"{_fmt(m['ci_low'], m['unit'])} to {_fmt(m['ci_high'], m['unit'])}"
                    if m["ci_low"] is not None
                    else ""
                )
                unit = "" if m["unit"] in ("share", "") else f" {m['unit']}"
                note = "; ".join(
                    x for x in (m["note"], "preliminary" if m["preliminary"] else None) if x
                )
                value = _fmt(m["value"], m["unit"]) + (unit if m["value"] is not None else "")
                lines.append(f"| {m['label']} | {value} | {ci} | {m['n']} | {note} |")
            lines.append("")
        for name, rows in s["tables"].items():
            if not rows:
                continue
            cols = list(rows[0].keys())
            lines += [f"**{name.replace('_', ' ').capitalize()}**", ""]
            lines += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            for r in rows[:30]:
                lines.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
            if len(rows) > 30:
                lines.append(f"\n({len(rows) - 30} more rows in latest.json)")
            lines.append("")
        if s["notes"]:
            lines += [*(f"- {n}" for n in s["notes"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1 else f"{v:.1f}"
    return str(v).replace("|", "/").replace("\n", " ")


def write_report(report: dict, out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = to_markdown(report)
    paths = [out_dir / "latest.md", out_dir / "latest.json", out_dir / f"{report['date_to']}.md"]
    paths[0].write_text(md, encoding="utf-8")
    paths[1].write_text(
        json.dumps(report, indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    paths[2].write_text(md, encoding="utf-8")
    return paths
