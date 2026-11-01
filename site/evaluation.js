/* Evaluation page: renders data/evaluation.json (written by `ripple evaluate`).
 * All text from the data is inserted with textContent. */
"use strict";

const STATUS_LABEL = { ok: "Results", insufficient_data: "Not enough data yet", skipped: "Skipped" };

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function fmtValue(v, unit) {
  if (v === null || v === undefined) return "n/a";
  if (unit === "share") return `${Math.round(v * 100)}%`;
  const s = Number.isInteger(v) || Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1);
  return unit && unit !== "share" ? `${s} ${unit}` : s;
}

function metricTable(metrics) {
  const rows = metrics.map((m) => el("tr", {}, [
    el("th", { scope: "row", text: m.label }),
    el("td", { text: fmtValue(m.value, m.unit) }),
    el("td", { text: m.ci_low === null ? "" : `${fmtValue(m.ci_low, m.unit)} to ${fmtValue(m.ci_high, m.unit)}` }),
    el("td", { text: String(m.n) }),
    el("td", {}, [m.preliminary ? el("span", { class: "prelim-badge", text: "preliminary" }) : "",
      m.note ? ` ${m.note}` : ""]),
  ]));
  return el("table", { class: "signals eval-table" }, [
    el("thead", {}, el("tr", {}, ["Metric", "Value", "95% interval", "n", "Notes"].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows),
  ]);
}

function chart(title, description, config) {
  const canvas = el("canvas", { role: "img", "aria-label": `${title}. ${description}` });
  const box = el("div", { class: "chart-box" }, [el("h4", { text: title }), el("div", { class: "chart-wrap" }, [canvas])]);
  // eslint-disable-next-line no-new
  new Chart(canvas, { ...config, options: { animation: false, responsive: true, maintainAspectRatio: false, ...config.options } });
  return box;
}

function sectionCharts(s) {
  const out = [];
  if (s.key === "detection" && (s.tables.per_line || []).length) {
    const rows = s.tables.per_line;
    out.push(chart("Precision and recall per line", "Share of events matching an alert and of alerts matched, per line.", {
      type: "bar",
      data: { labels: rows.map((r) => r.line), datasets: [
        { label: "Precision", data: rows.map((r) => r.precision), backgroundColor: "#1d4ed8" },
        { label: "Recall", data: rows.map((r) => r.recall), backgroundColor: "#b45309" },
      ] },
      options: { scales: { y: { min: 0, max: 1, ticks: { callback: (v) => `${v * 100}%` } } } },
    }));
  }
  if (s.key === "placebo") {
    const ms = s.metrics.filter((m) => m.n > 0);
    if (ms.length) {
      out.push(chart("Change during events (mean and 95% interval)", "Percent difference between event and placebo conditions; bars show the 95% bootstrap interval.", {
        type: "bar",
        data: { labels: ms.map((m) => m.label.replace(": change during events", "")), datasets: [
          { label: "95% interval", data: ms.map((m) => (m.ci_low === null ? [m.value, m.value] : [m.ci_low, m.ci_high])), backgroundColor: "#cbd5e1" },
          { label: "Mean", type: "line", showLine: false, pointRadius: 6, data: ms.map((m) => m.value), backgroundColor: "#111", borderColor: "#111" },
        ] },
        options: { indexAxis: "y", scales: { x: { title: { display: true, text: "% difference" } } } },
      }));
    }
  }
  if (s.key === "reliability" && (s.tables.sources || []).length) {
    const rows = s.tables.sources;
    out.push(chart("Uptime per data source", "Share of fetches with usable data, per source.", {
      type: "bar",
      data: { labels: rows.map((r) => r.source), datasets: [{ label: "Uptime", data: rows.map((r) => r.uptime), backgroundColor: "#15803d" }] },
      options: { indexAxis: "y", scales: { x: { min: 0, max: 1, ticks: { callback: (v) => `${v * 100}%` } } } },
    }));
  }
  if (s.key === "recommendations") {
    const ms = s.metrics.filter((m) => m.key.startsWith("mae_") && m.value !== null);
    if (ms.length) {
      out.push(chart("Estimate errors (MAE)", "Mean absolute error of the time or speed estimate per option type.", {
        type: "bar",
        data: { labels: ms.map((m) => `${m.label} (${m.unit})`), datasets: [{ label: "MAE", data: ms.map((m) => m.value), backgroundColor: "#7c3aed" }] },
      }));
    }
  }
  return out;
}

function renderSection(s) {
  const status = STATUS_LABEL[s.status] || s.status;
  const period = s.date_from ? `${s.date_from} to ${s.date_to}` : "not tied to a period";
  const sec = el("section", { class: "panel eval-section", "aria-labelledby": `sec-${s.key}` }, [
    el("h2", { id: `sec-${s.key}` }, [s.title, " ", el("span", { class: `status-chip ${s.status === "ok" ? "ok" : "building"}`, text: status })]),
    el("p", { class: "muted small", text: `Data: ${period}.` }),
    el("p", { class: "eval-summary", text: s.summary }),
  ]);
  if (s.metrics.length) sec.append(metricTable(s.metrics));
  const charts = sectionCharts(s);
  if (charts.length) sec.append(el("div", { class: "charts" }, charts));
  if (s.key === "detection" && (s.tables.unmatched_events || []).length) {
    sec.append(el("details", {}, [
      el("summary", { text: `Unmatched events for manual review (${s.tables.unmatched_events.length})` }),
      el("ul", {}, s.tables.unmatched_events.slice(0, 20).map((e) => el("li", {
        text: `${e.line} line, ${e.start_utc} UTC, ${e.duration_min === null ? "?" : Math.round(e.duration_min)} min, peak ${e.peak_delay_min === null ? "?" : e.peak_delay_min.toFixed(1)} min` }))),
    ]));
  }
  if (s.notes.length) {
    sec.append(el("details", {}, [el("summary", { text: "Method and caveats" }), el("ul", {}, s.notes.map((n) => el("li", { text: n })))]));
  }
  return sec;
}

async function main() {
  const period = document.getElementById("eval-period");
  try {
    const resp = await fetch("data/evaluation.json", { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const report = await resp.json();
    period.textContent = `Period: ${report.date_from} to ${report.date_to} (last ${report.window_days} days), generated ${report.generated_utc.replace("T", " ").replace("Z", " UTC")}.`;
    document.getElementById("min-sample").textContent = String(report.min_sample);
    const box = document.getElementById("eval-sections");
    for (const s of report.sections) box.append(renderSection(s));
  } catch (err) {
    period.textContent = `No evaluation report has been published yet (${err.message}). It is generated weekly.`;
  }
}

document.addEventListener("DOMContentLoaded", main);
