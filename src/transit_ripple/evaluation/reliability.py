"""Pipeline reliability (M6, part A6).

Per data source: uptime (share of fetches that were usable), freshness (median and
95th percentile age of the data when fetched) and gaps (fetches that followed more
than ``source_gap_min`` minutes without a good fetch). Per workflow: run success
rate and average duration, from the GitHub Actions REST API with the workflow's
read-only ``GITHUB_TOKEN`` (skipped when no token is available, e.g. locally).
"""

from __future__ import annotations

import logging
import os

import pandas as pd
import requests

from ..config import Settings
from .common import (
    STATUS_INSUFFICIENT,
    STATUS_OK,
    STATUS_SKIPPED,
    Metric,
    Section,
    day,
    set_span,
)

log = logging.getLogger(__name__)
USABLE = ("ok", "duplicate")


def source_reliability(con, start, end) -> list[dict]:
    rows = []
    feeds = con.execute(
        "SELECT feed_id AS source, status, feed_age_s, fetched_utc, run_id FROM quality_feed "
        "WHERE fetched_utc >= $s AND fetched_utc < $e",
        {"s": start, "e": end},
    ).df()
    if len(feeds):
        feeds["source"] = "subway:" + feeds["source"]
    other = con.execute(
        "SELECT source, status, feed_age_s, gap_min, fetched_utc FROM quality_source "
        "WHERE fetched_utc >= $s AND fetched_utc < $e",
        {"s": start, "e": end},
    ).df()
    for name, g in pd.concat([feeds, other], ignore_index=True).groupby("source"):
        usable = g["status"].isin(USABLE)
        age = pd.to_numeric(g.loc[usable, "feed_age_s"], errors="coerce").dropna()
        gaps = g["gap_min"].notna() if "gap_min" in g else pd.Series(False, index=g.index)
        rows.append(
            {
                "source": name,
                "fetches": len(g),
                "uptime": float(usable.mean()),
                "age_median_s": float(age.median()) if len(age) else None,
                "age_p95_s": float(age.quantile(0.95)) if len(age) else None,
                "gaps": int(gaps.sum()),
                "gap_minutes": float(pd.to_numeric(g.get("gap_min"), errors="coerce").sum())
                if "gap_min" in g
                else 0.0,
                "first_utc": str(g["fetched_utc"].min())[:16],
                "last_utc": str(g["fetched_utc"].max())[:16],
            }
        )
    return rows


def pipeline_cadence(con, start, end) -> dict:
    runs = con.execute(
        "SELECT run_id, min(fetched_utc) AS t FROM quality_feed "
        "WHERE fetched_utc >= $s AND fetched_utc < $e GROUP BY run_id ORDER BY t",
        {"s": start, "e": end},
    ).df()
    if len(runs) < 2:
        return {"runs": len(runs)}
    gaps = runs["t"].diff().dt.total_seconds().dropna() / 60.0
    return {
        "runs": len(runs),
        "median_minutes_between_runs": float(gaps.median()),
        "p95_minutes_between_runs": float(gaps.quantile(0.95)),
        "longest_gap_minutes": float(gaps.max()),
    }


def workflow_runs(repo: str, token: str, since, max_pages: int = 10) -> list[dict]:
    """Completed workflow runs created since ``since`` (GitHub REST API, read-only)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    runs, url = [], f"https://api.github.com/repos/{repo}/actions/runs"
    params = {
        "created": f">={pd.Timestamp(since):%Y-%m-%d}",
        "per_page": 100,
        "status": "completed",
    }
    for page in range(1, max_pages + 1):
        resp = requests.get(url, headers=headers, params={**params, "page": page}, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"GitHub API HTTP {resp.status_code}")
        batch = resp.json().get("workflow_runs", [])
        runs += batch
        if len(batch) < 100:
            break
    return runs


def summarize_runs(runs: list[dict]) -> list[dict]:
    if not runs:
        return []
    df = pd.DataFrame(
        {
            "workflow": [r.get("name") for r in runs],
            "conclusion": [r.get("conclusion") for r in runs],
            "started": pd.to_datetime([r.get("run_started_at") for r in runs], utc=True),
            "updated": pd.to_datetime([r.get("updated_at") for r in runs], utc=True),
        }
    )
    df["minutes"] = (df["updated"] - df["started"]).dt.total_seconds() / 60.0
    out = []
    for name, g in df.groupby("workflow"):
        finished = g[g["conclusion"].isin(["success", "failure", "timed_out"])]
        out.append(
            {
                "workflow": name,
                "runs": len(g),
                "success_rate": float((finished["conclusion"] == "success").mean())
                if len(finished)
                else None,
                "cancelled_or_skipped": int((~g.index.isin(finished.index)).sum()),
                "avg_minutes": float(g["minutes"].mean()),
            }
        )
    return out


def evaluate_reliability(con, settings: Settings, start, end, github: bool = True) -> Section:
    sources = source_reliability(con, start, end)
    cadence = pipeline_cadence(con, start, end)
    section = Section(
        "reliability",
        "Pipeline reliability",
        STATUS_OK if sources else STATUS_INSUFFICIENT,
        day(start),
        day(end - pd.Timedelta(seconds=1)),
        "",
        notes=[
            "Uptime is the share of fetches whose data was usable (status ok or duplicate); "
            "stale, missing, invalid and unavailable fetches count as down.",
            "GitHub runs scheduled workflows on a best-effort basis, so the time between "
            "runs is often longer than the 5-minute schedule.",
        ],
    )
    if sources:
        set_span(
            section, pd.Series([s["first_utc"] for s in sources] + [s["last_utc"] for s in sources])
        )
    section.tables["sources"] = sources
    for s in sources:
        section.metrics.append(
            Metric(
                f"uptime_{s['source']}",
                f"Uptime: {s['source']}",
                s["uptime"],
                s["fetches"],
                "share",
            )
        )
    if cadence.get("runs", 0) >= 2:
        section.metrics.append(
            Metric(
                "run_interval_median",
                "Median minutes between ingest runs",
                cadence["median_minutes_between_runs"],
                cadence["runs"],
                "min",
            )
        )
    section.tables["cadence"] = [cadence]
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if github and repo and token:
        try:
            wf = summarize_runs(workflow_runs(repo, token, start))
            section.tables["workflows"] = wf
            for w in wf:
                section.metrics.append(
                    Metric(
                        f"workflow_{w['workflow']}",
                        f"Run success rate: {w['workflow']}",
                        w["success_rate"],
                        w["runs"],
                        "share",
                        note=f"average {w['avg_minutes']:.1f} min per run",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - reliability must not fail the report
            section.notes.append(f"Workflow statistics unavailable: {type(exc).__name__}.")
    else:
        section.notes.append(
            "Workflow statistics were skipped (no GITHUB_TOKEN / GITHUB_REPOSITORY; they are "
            "collected in the evaluation workflow)."
        )
        if not sources:
            section.status = STATUS_SKIPPED
    up = [s for s in sources if s["fetches"]]
    if up:
        worst = min(up, key=lambda s: s["uptime"])
        section.summary = (
            f"{len(up)} source(s) monitored; lowest uptime: {worst['source']} at "
            f"{worst['uptime']:.0%} of {worst['fetches']} fetches."
        )
        if cadence.get("runs", 0) >= 2:
            section.summary += (
                f" Median {cadence['median_minutes_between_runs']:.1f} min between ingest runs "
                f"({cadence['runs']} runs)."
            )
    else:
        section.summary = "No quality records in this period."
    return section
