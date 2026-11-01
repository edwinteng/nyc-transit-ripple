"""Static output: one PNG chart per event and an ``index.html`` listing recent events.

The page is plain HTML/CSS with relative links, ready to be served by GitHub
Pages. Nothing on it triggers computation; all text is generated in CI.
"""

from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")  # headless rendering in CI
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .config import AGENCY_TIMEZONE, Settings  # noqa: E402
from .events import as_list, event_chart_series  # noqa: E402

TZ = ZoneInfo(AGENCY_TIMEZONE)


def _local_index(minutes: pd.Series) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(minutes)).tz_localize("UTC").tz_convert(TZ)


def _empty(ax, message: str) -> None:
    ax.set_yticks([])
    ax.text(0.5, 0.5, message, transform=ax.transAxes, ha="center", va="center", color="grey")


def render_event_chart(event: dict, series: pd.DataFrame, path: Path) -> Path:
    """Three stacked panels: line delay, nearby traffic speed, nearby Citi Bikes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    x = _local_index(series["minute_utc"])
    start = pd.Timestamp(event["start_utc"]).tz_localize("UTC").tz_convert(TZ)
    end = pd.Timestamp(event["end_utc"]).tz_localize("UTC").tz_convert(TZ)

    fig, axes = plt.subplots(3, 1, figsize=(9, 7), sharex=True, constrained_layout=True)
    ax = axes[0]
    value = series["value"].astype(float)
    ax.plot(x, value, color="#d62728", lw=1.6, label="line delay (worst direction)")
    observed = series["fill_status"].eq("observed")
    ax.scatter(
        x[observed.to_numpy()],
        value[observed],
        s=10,
        color="#d62728",
        zorder=3,
        label="observed minute",
    )
    ax.axhline(float(event["threshold_min"]), color="grey", ls="--", lw=1, label="threshold")
    ax.set_ylabel("delay (min)")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[1]
    ax.plot(
        x,
        series["traffic_speed_mph"].astype(float),
        color="#1f77b4",
        lw=1.6,
        label="traffic speed nearby",
    )
    ax.plot(
        x,
        series["traffic_baseline_mph"].astype(float),
        color="#1f77b4",
        lw=1,
        ls="--",
        label="usual (baseline)",
    )
    ax.set_ylabel("speed (mph)")
    ax.legend(loc="upper left", fontsize=8)
    if series["traffic_speed_mph"].isna().all():
        _empty(ax, "no fresh traffic speed data nearby")

    ax = axes[2]
    ax.plot(
        x,
        series["bikes_available"].astype(float),
        color="#2ca02c",
        lw=1.6,
        label="Citi Bikes available nearby",
    )
    ax.plot(
        x,
        series["bikes_baseline"].astype(float),
        color="#2ca02c",
        lw=1,
        ls="--",
        label="usual (baseline, same stations)",
    )
    ax.set_ylabel("bikes")
    ax.legend(loc="upper left", fontsize=8)
    if series["bikes_available"].isna().all():
        _empty(ax, "no fresh Citi Bike data nearby")

    for a in axes:
        a.axvspan(start, end, color="#d62728", alpha=0.08)
        a.grid(alpha=0.3)
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ))
    axes[2].set_xlabel("New York time")
    fig.suptitle(f"{event['route_id']} line delay event {event['event_id']}", fontsize=11)
    fig.savefig(path, dpi=90)
    plt.close(fig)
    return path


def _status_rows(con) -> list[tuple[str, str, str]]:
    """Latest data quality status per source, for the page header."""
    rows = []
    feeds = con.execute(
        "SELECT feed_id, status, feed_age_s FROM quality_feed "
        "QUALIFY row_number() OVER (PARTITION BY feed_id ORDER BY fetched_utc DESC) = 1 "
        "ORDER BY feed_id"
    ).df()
    if len(feeds):
        ok = int((feeds["status"].isin(["ok", "duplicate"])).sum())
        rows.append(("subway feeds", f"{ok}/{len(feeds)} usable", ""))
    src = con.execute(
        "SELECT source, status, feed_age_s, coverage FROM quality_source "
        "WHERE source IN ('alerts', 'traffic', 'bike_status') "
        "QUALIFY row_number() OVER (PARTITION BY source ORDER BY fetched_utc DESC) = 1 "
        "ORDER BY source"
    ).df()
    for r in src.to_dict("records"):
        age = f"data age {r['feed_age_s'] / 60:.0f} min" if pd.notna(r["feed_age_s"]) else ""
        rows.append((r["source"], r["status"], age))
    return rows


CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:980px;margin:2rem auto;
padding:0 1rem;color:#1d1d1f;line-height:1.45}
h1{font-size:1.5rem;margin-bottom:.2rem} .muted{color:#6e6e73;font-size:.9rem}
.event{border:1px solid #d2d2d7;border-radius:8px;padding:1rem;margin:1.2rem 0}
.event h2{font-size:1.1rem;margin:.1rem 0 .5rem} .badge{display:inline-block;padding:0 .45rem;
border-radius:4px;font-size:.8rem;background:#f2f2f7;margin-right:.3rem}
.ongoing{background:#ffe5e5} img{max-width:100%;height:auto;border-radius:4px}
table{border-collapse:collapse;font-size:.9rem} td,th{padding:.15rem .6rem;text-align:left;
border-bottom:1px solid #eee}
"""


def build_site(
    con, settings: Settings, baselines: pd.DataFrame, out_dir: Path, now: dt.datetime
) -> Path:
    """Write charts and index.html for the most recent events."""
    out_dir.mkdir(parents=True, exist_ok=True)
    charts = out_dir / "charts"
    events = con.execute(
        "SELECT * FROM events ORDER BY start_utc DESC LIMIT $n", {"n": settings.site_max_events}
    ).df()
    keep = set()
    cards = []
    manifest_path = charts / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    new_manifest = {}
    for ev in events.to_dict("records"):
        chart = charts / f"{ev['event_id']}.png"
        version = str(ev.get("computed_utc"))
        # Re-render only events whose stored version changed since the last build.
        if not chart.exists() or manifest.get(ev["event_id"]) != version:
            series = event_chart_series(
                con, settings, baselines, ev, settings.site_chart_padding_min
            )
            render_event_chart(ev, series, chart)
        new_manifest[ev["event_id"]] = version
        keep.add(chart.name)
        start = pd.Timestamp(ev["start_utc"]).tz_localize("UTC").tz_convert(TZ)
        status_cls = "badge ongoing" if ev["status"] == "ongoing" else "badge"
        alerts = as_list(ev["linked_alerts"])
        alert_items = "".join(
            f"<li>{html.escape(a['alert_type'] or 'alert')}"
            f"{' (planned)' if a['is_planned'] else ''}: {html.escape(a['header_text'] or '')}</li>"
            for a in alerts
        )
        cited = json.loads(ev["cited_metrics"]) if ev.get("cited_metrics") else {}
        metrics = "".join(
            f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
            for k, v in sorted(cited.items())
        )
        alt = f"Delay, traffic speed and bike availability for {ev['event_id']}"
        alerts_html = f"<h3>Linked MTA alerts</h3><ul>{alert_items}</ul>" if alert_items else ""
        cards.append(
            "\n".join(
                [
                    f'<section class="event" id="{html.escape(ev["event_id"])}">',
                    f"<h2>{html.escape(ev['route_id'])} line · {start:%a %b %d, %H:%M} ET</h2>",
                    f'<span class="{status_cls}">{html.escape(ev["status"])}</span>',
                    f'<span class="badge">peak {ev["peak_delay_min"]:.1f} min</span>',
                    f'<span class="badge">{ev["duration_min"]:.0f} min</span>',
                    f"<p>{html.escape(ev['summary'] or '')}</p>",
                    f'<img src="charts/{html.escape(chart.name)}" alt="{html.escape(alt)}" '
                    'loading="lazy">',
                    alerts_html,
                    "<details><summary>Numbers cited in the summary</summary>"
                    f"<table>{metrics}</table></details>",
                    "</section>",
                ]
            )
        )
    for old in charts.glob("*.png") if charts.exists() else []:
        if old.name not in keep:
            old.unlink()
    if new_manifest:
        manifest_path.write_text(json.dumps(new_manifest, indent=1, sort_keys=True))

    status = "".join(
        f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td><td>{html.escape(c)}</td></tr>"
        for a, b, c in _status_rows(con)
    )
    rule = settings.event_rule()
    generated = pd.Timestamp(now.replace(tzinfo=None) if now.tzinfo else now)
    body = "\n".join(cards) if cards else "<p>No delay events detected yet.</p>"
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NYC subway disruption ripple: recent events</title><style>{CSS}</style></head>
<body>
<h1>NYC subway disruption ripple</h1>
<p class="muted">Recent subway delay events with nearby traffic speed and Citi Bike availability.
Generated {generated:%Y-%m-%d %H:%M} UTC from public MTA, NYC DOT and Citi Bike data.
Summaries are rule-based and only cite numbers from the data.</p>
<p class="muted">Event rule: {html.escape(rule["event_metric"])} at or above
{rule["event_threshold_min"]} min for {rule["event_min_minutes"]} consecutive minutes
(at least {rule["event_min_observed"]} observed).</p>
<h3>Data status</h3><table><tr><th>Source</th><th>Status</th><th></th></tr>{status}</table>
{body}
</body></html>
"""
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    return index
