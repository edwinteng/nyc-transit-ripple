/* NYC Subway Disruption Ripple dashboard. Plain JavaScript, no build step.
 * Data: data/status.json, data/stations.json, data/events.json, data/events/<id>.json
 * (written by `ripple export`). All text from the data is inserted with textContent. */
"use strict";

const TZ = "America/New_York";
const STALE_PAGE_MIN = 20; // the pipeline runs every ~5 min; older data is flagged
const ROUTE_COLORS = {
  "1": "#EE352E", "2": "#EE352E", "3": "#EE352E",
  "4": "#00933C", "5": "#00933C", "6": "#00933C", "6X": "#00933C",
  "7": "#B933AD", "7X": "#B933AD",
  A: "#0039A6", C: "#0039A6", E: "#0039A6",
  B: "#FF6319", D: "#FF6319", F: "#FF6319", FX: "#FF6319", M: "#FF6319",
  G: "#6CBE45", J: "#996633", Z: "#996633", L: "#A7A9AC",
  N: "#FCCC0A", Q: "#FCCC0A", R: "#FCCC0A", W: "#FCCC0A",
  GS: "#808183", FS: "#808183", H: "#808183", SI: "#1D2F6F",
};
const DARK_TEXT_ROUTES = new Set(["N", "Q", "R", "W", "L", "G"]);
const STATUS_TEXT = {
  ok: ["OK", "✓"], degraded: ["Degraded", "!"], stale: ["Stale", "!"],
  missing: ["Missing", "×"], on_demand: ["On demand", "○"], building: ["Building", "○"],
  unavailable: ["Unavailable", "×"],
};
const CONFIDENCE_TEXT = { high: "high confidence", medium: "medium confidence", low: "low confidence", none: "no estimate" };
const OPTION_TEXT = { walk_line: "Other line", bike: "Citi Bike", bus: "Bus", wait: "Wait" };
const IMPACT_TEXT = { severe: "severe", high: "high", moderate: "moderate", low: "low", unknown: "not enough data" };
const SIGNAL_TEXT = { traffic: "Traffic speed", bus: "Bus speed", camera: "Camera counts", bikes: "Citi Bike availability" };
const SIGNAL_STATUS = {
  used: "used", no_data: "no fresh data", no_baseline: "no baseline yet",
  low_confidence: "baseline has too little history", zero_weight: "weight is 0",
};

const state = { events: [], stations: [], byId: new Map(), selected: null, charts: [], layers: {}, replay: null };

// Chart.js plugin: a vertical line at the replay position (chart.$replayIndex).
const replayCursor = {
  id: "replayCursor",
  afterDatasetsDraw(chart) {
    const i = chart.$replayIndex;
    if (i === undefined || i === null) return;
    const x = chart.scales.x.getPixelForValue(i);
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.restore();
  },
};
const DELAY_BANDS = [[20, "severe"], [10, "high"], [5, "moderate"], [-Infinity, "low"]];

// ---------- helpers ----------------------------------------------------------------------

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const fmtTime = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
const fmtDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short", month: "short", day: "numeric" });
function localTime(iso) { return iso ? fmtTime.format(new Date(iso)) : "?"; }
function localDay(iso) { return iso ? fmtDay.format(new Date(iso)) : "?"; }
function ago(iso) {
  if (!iso) return "never";
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 90) return `${min} min ago`;
  if (min < 48 * 60) return `${Math.round(min / 60)} h ago`;
  return `${Math.round(min / 1440)} days ago`;
}
// Leaflet inserts string tooltips as HTML; names come from external APIs, so pass text nodes.
function tip(text) { return el("span", { text }); }
function fmt(v, digits = 1) { return v === null || v === undefined ? "n/a" : Number(v).toFixed(digits); }

function routeBullet(route) {
  const dark = DARK_TEXT_ROUTES.has(route);
  const b = el("span", { class: "route-bullet" + (dark ? " dark-text" : ""), text: route, title: `${route} line` });
  b.style.background = ROUTE_COLORS[route] || "#4b5563";
  return b;
}

function impactBadge(ev) {
  const label = ev.impact_label || "unknown";
  const text = ev.impact_score === null || ev.impact_score === undefined
    ? "Impact: not enough data"
    : `Impact ${Math.round(ev.impact_score)}/100 (${IMPACT_TEXT[label]})`;
  return el("span", { class: `impact-badge impact-${label}`, text });
}

async function getJSON(path) {
  const resp = await fetch(path, { cache: "no-cache" });
  if (!resp.ok) throw new Error(`${path}: HTTP ${resp.status}`);
  return resp.json();
}

// ---------- status bar ---------------------------------------------------------------------

function renderStatus(status) {
  const list = document.getElementById("status-list");
  list.replaceChildren();
  // "Last update" is when the pipeline last collected data, not when this page was built:
  // the page is rebuilt from stored data, so its build time says nothing about freshness.
  const collected = status.last_pipeline_run_utc;
  document.getElementById("last-update").textContent = collected
    ? `Last update: data collected ${localDay(collected)} ${localTime(collected)} ET ` +
      `(${ago(collected)}). Times are New York time.`
    : "No data has been collected yet.";
  for (const s of status.sources) {
    const [word, icon] = STATUS_TEXT[s.status] || [s.status, "?"];
    const age = s.data_age_min === null ? "" : ` · data ${ago(s.data_utc)}`;
    const chip = el("li", { class: `status-chip ${s.status}`, title: s.detail || "" }, [
      el("span", { "aria-hidden": "true", text: icon }),
      el("span", { text: `${s.label}:` }),
      el("span", { class: "state", text: word }),
      el("span", { text: age }),
    ]);
    list.append(chip);
  }
  const banner = document.getElementById("stale-banner");
  const ageMin = collected ? (Date.now() - new Date(collected).getTime()) / 60000 : Infinity;
  if (ageMin > STALE_PAGE_MIN) {
    banner.hidden = false;
    banner.textContent = collected
      ? `Data may be out of date: it was last collected ${ago(collected)}. ` +
        "Values below are not current conditions."
      : "No data has been collected yet.";
  }
}

// ---------- event list ---------------------------------------------------------------------

function eventTitle(ev) {
  const where = ev.stations.length ? ` near ${ev.stations[0].name}` : "";
  return `${ev.route} line delays${where}`;
}

function renderEvents(events) {
  const list = document.getElementById("event-list");
  list.replaceChildren();
  document.getElementById("event-count").textContent = `(${events.length})`;
  if (!events.length) {
    list.append(el("p", { class: "muted", text: "No delay events in the last 30 days of collected data." }));
    return;
  }
  const sorted = [...events].sort((a, b) =>
    (b.status === "ongoing") - (a.status === "ongoing") || (b.start_utc || "").localeCompare(a.start_utc || ""));
  for (const ev of sorted) {
    const when = ev.status === "ongoing"
      ? `Since ${localTime(ev.start_utc)} ET, ${localDay(ev.start_utc)} · ${fmt(ev.duration_min, 0)} min so far`
      : `${localDay(ev.start_utc)}, ${localTime(ev.start_utc)}–${localTime(ev.end_utc)} ET · ${fmt(ev.duration_min, 0)} min`;
    const card = el("button", {
      type: "button", class: `event-card impact-${Object.hasOwn(IMPACT_TEXT, ev.impact_label) ? ev.impact_label : "unknown"}`,
      role: "listitem",
      "aria-pressed": "false", "data-id": ev.id, onclick: () => selectEvent(ev.id, true),
    }, [
      el("div", { class: "card-head" }, [
        routeBullet(ev.route),
        el("span", { class: "card-title", text: eventTitle(ev) }),
        el("span", { class: `status-badge ${ev.status}`, text: ev.status === "ongoing" ? "Ongoing" : "Ended" }),
      ]),
      el("p", { class: "card-times", text: when }),
      el("p", { class: "card-metrics" }, [
        `Peak median delay ${fmt(ev.peak_delay_min)} min · `, impactBadge(ev),
        ev.alerts.length ? ` · ${ev.alerts.length} MTA alert(s)` : "",
      ]),
      el("p", { class: "card-summary", text: ev.sentences.map((s) => s.text).join(" ") }),
      ev.status === "ongoing" && ev.top_recommendation
        ? el("p", { class: "card-reco" }, [el("strong", { text: "Best option now: " }), ev.top_recommendation.text])
        : null,
    ]);
    list.append(card);
  }
}

// ---------- map ----------------------------------------------------------------------------

function initMap(stations) {
  const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([40.73, -73.94], 11);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  state.map = map;
  const base = L.layerGroup().addTo(map);
  const highlighted = L.layerGroup().addTo(map);
  for (const st of stations) {
    if (st.lat === null || st.lon === null) continue;
    const routes = st.routes.length ? ` (${st.routes.join(", ")})` : "";
    if (st.event) {
      // The pin HTML only ever contains a number or "?" and fixed class names.
      const score = Number(st.event.impact_score);
      const label = st.event.impact_score === null || !Number.isFinite(score) ? "?" : String(Math.round(score));
      const band = Object.hasOwn(IMPACT_TEXT, st.event.impact_label) ? st.event.impact_label : "unknown";
      const icon = L.divIcon({
        className: "",
        html: `<span class="badge-pin impact-${band} ${st.event.status === "ongoing" ? "ongoing" : "recent"}">${label}</span>`,
        iconSize: [26, 26], iconAnchor: [13, 13],
      });
      const impact = label === "?" ? "impact: not enough data" : `impact ${label}/100 (${IMPACT_TEXT[band]})`;
      const title = `${st.name}${routes}: ${st.event.route} line event (${st.event.status}), ` +
        `trains up to ${fmt(st.event.max_delay_min)} min late, ${impact}`;
      L.marker([st.lat, st.lon], { icon, title, keyboard: true, riseOnHover: true })
        .bindTooltip(tip(title)).on("click", () => selectEvent(st.event.event_id, false)).addTo(highlighted);
    } else {
      L.circleMarker([st.lat, st.lon], { radius: 3, color: "#52606d", weight: 1, fillColor: "#9aa5b1", fillOpacity: 0.9 })
        .bindTooltip(tip(`${st.name}${routes}`)).addTo(base);
    }
  }
  state.layers.nearby = L.layerGroup().addTo(map);
}

function showNearby(detail) {
  const layer = state.layers.nearby;
  layer.clearLayers();
  const points = [];
  for (const link of detail.nearby.links) {
    if (!link.coords.length) continue;
    L.polyline(link.coords, { color: "#6d28d9", weight: 4, dashArray: "6 6" })
      .bindTooltip(tip(`Traffic speed link: ${link.name || link.id}`)).addTo(layer);
    points.push(...link.coords);
  }
  for (const b of detail.nearby.bikes) {
    if (b.lat === null) continue;
    const icon = L.divIcon({ className: "", html: '<span class="pin-bike">B</span>', iconSize: [18, 18], iconAnchor: [9, 9] });
    L.marker([b.lat, b.lon], { icon, title: `Citi Bike: ${b.name}` }).bindTooltip(tip(`Citi Bike: ${b.name} (${fmt(b.distance_m, 0)} m)`)).addTo(layer);
    points.push([b.lat, b.lon]);
  }
  for (const c of detail.nearby.cameras) {
    if (c.lat === null) continue;
    const counts = c.vehicles === null ? "not counted during this event"
      : `vehicles ${c.vehicles}, people ${c.persons} (counted at ${localTime(c.counted_utc)} ET)`;
    const icon = L.divIcon({ className: "", html: '<span class="pin-camera">C</span>', iconSize: [18, 18], iconAnchor: [9, 9] });
    L.marker([c.lat, c.lon], { icon, title: `Camera: ${c.name}` }).bindTooltip(tip(`Camera: ${c.name} – ${counts}`)).addTo(layer);
    points.push([c.lat, c.lon]);
  }
  for (const s of detail.nearby.stations) {
    if (s.lat !== null) points.push([s.lat, s.lon]);
  }
  if (points.length) state.map.fitBounds(L.latLngBounds(points).pad(0.15), { maxZoom: 16 });
}

// ---------- detail -------------------------------------------------------------------------

function chartBox(title, description, allDatasets, labels, opts = {}) {
  // Series without any value (for example a baseline that does not exist yet) are left out.
  const datasets = allDatasets.filter((d) => d.data.some((v) => v !== null));
  const hasData = datasets.some((d) => !d.reference);
  const box = el("div", { class: "chart-box" }, [el("h4", { text: title })]);
  if (!hasData) {
    box.append(el("p", { class: "chart-empty", text: opts.empty || "No data for this period." }));
    return box;
  }
  const canvas = el("canvas", { role: "img", "aria-label": `${title}. ${description}` });
  box.append(el("div", { class: "chart-wrap" }, [canvas]));
  const chart = new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, spanGaps: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { boxWidth: 14 } } },
      scales: { x: { ticks: { maxTicksLimit: 6 } }, y: { beginAtZero: opts.beginAtZero ?? false, suggestedMax: opts.suggestedMax } },
    },
    plugins: [replayCursor],
  });
  state.charts.push(chart);
  return box;
}

function line(label, data, color, dashed = false) {
  return { label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, borderDash: dashed ? [6, 4] : [] };
}

function renderDetail(ev, detail) {
  for (const c of state.charts) c.destroy();
  state.charts = [];
  const box = document.getElementById("detail");
  box.replaceChildren();
  box.append(el("h3", {}, [routeBullet(ev.route), " ", eventTitle(ev)]));
  box.append(el("p", { class: "card-metrics" }, [
    ev.status === "ongoing" ? `Ongoing since ${localTime(ev.start_utc)} ET, ${localDay(ev.start_utc)}`
      : `${localDay(ev.start_utc)}, ${localTime(ev.start_utc)}–${localTime(ev.end_utc)} ET`,
    ` · peak median delay ${fmt(ev.peak_delay_min)} min · `, impactBadge(ev),
  ]));

  const ol = el("ol", { class: "sentences" });
  for (const s of ev.sentences) {
    const metrics = el("dl");
    for (const [k, v] of Object.entries(s.metrics)) metrics.append(el("dt", { text: k }), el("dd", { text: v }));
    ol.append(el("li", {}, [el("span", { text: s.text }),
      el("details", { class: "metrics-used" }, [el("summary", { text: "Metrics used in this sentence" }), metrics])]));
  }
  box.append(ol);

  const signals = ev.impact_signals || {};
  const order = Object.keys(SIGNAL_TEXT).filter((n) => n in signals);
  const rows = order.map((name) => [name, signals[name]]).map(([name, s]) => el("tr", {}, [
    el("td", { text: SIGNAL_TEXT[name] || name }),
    el("td", { text: SIGNAL_STATUS[s.status] || s.status }),
    el("td", { text: s.change_pct === null ? "n/a" : `${s.change_pct > 0 ? "+" : ""}${fmt(s.change_pct)}%` }),
    el("td", { text: s.sub_score === null ? "n/a" : fmt(s.sub_score, 0) }),
    el("td", { text: s.weight === null ? "n/a" : `${Math.round(s.weight * 100)}%` }),
  ]));
  box.append(el("table", { class: "signals" }, [
    el("caption", { class: "visually-hidden", text: "Signals of the surface impact score" }),
    el("thead", {}, el("tr", {}, ["Signal", "Status", "Change vs. usual", "Sub-score", "Weight"].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows),
  ]));

  box.append(renderReplayControls(ev, detail));
  const recoBox = el("div", { id: "replay-recos" });
  box.append(recoBox);

  if (ev.alerts.length) {
    box.append(el("h4", { text: "Linked MTA alerts" }));
    box.append(el("ul", { class: "alerts" }, ev.alerts.map((a) =>
      el("li", { text: `${a.type || "Alert"}${a.planned ? " (planned work)" : ""}: ${a.header || ""}` }))));
  }

  const s = detail.series;
  const labels = s.t.map(localTime);
  const thr = s.t.map(() => detail.threshold_min);
  const charts = el("div", { class: "charts" }, [
    chartBox("Subway delay (min)", `Median train delay of the ${ev.route} line, worst direction, with the event threshold.`,
      [line("Median delay", s.delay_min, "#b91c1c"), { ...line("Event threshold", thr, "#6b7280", true), reference: true }],
      labels, { beginAtZero: true }),
    ev.impact_signals && !ev.impact_signals.traffic ? null : chartBox("Nearby traffic speed (mph)", "Average speed of road links within 500 m, compared with the usual speed for this weekday and time.",
      [line("Observed", s.traffic_mph, "#1d4ed8"), line("Usual", s.traffic_base_mph, "#1d4ed8", true)], labels,
      { empty: "No fresh traffic speed data near these stations." }),
    chartBox("Nearby bus speed (mph)", "Average speed of buses on route segments ending within 500 m, compared with the usual speed.",
      [line("Observed", s.bus_mph, "#b45309"), line("Usual", s.bus_base_mph, "#b45309", true)], labels,
      { empty: "No live bus speed data near these stations." }),
    chartBox("Camera counts", "Vehicles and people counted by cameras within 500 m, compared with usual counts.",
      [line("Vehicles", s.camera_vehicles, "#0f766e"), line("Usual vehicles", s.camera_vehicles_base, "#0f766e", true),
       line("People", s.camera_persons, "#a21caf"), line("Usual people", s.camera_persons_base, "#a21caf", true)], labels,
      { beginAtZero: true, empty: "No camera counts near these stations (cameras are counted only while an event is ongoing)." }),
    chartBox("Citi Bikes available nearby", "Bikes available at Citi Bike stations within 500 m, compared with the usual number.",
      [line("Available", s.bikes, "#15803d"), line("Usual", s.bikes_base, "#15803d", true)], labels,
      { empty: "No fresh Citi Bike data near these stations." }),
    chartBox("Surface impact score (0-100)", "Per-minute combination of the signals above that have a reliable baseline.",
      [line("Impact score", s.impact, "#92400e")], labels,
      { beginAtZero: true, suggestedMax: 100, empty: "Not enough baseline history yet to compute the impact score." }),
  ]);
  box.append(charts);
  setupReplay(ev, detail);
}

function renderRecommendations(ev, recs, atReplay = false) {
  const ongoing = ev.status === "ongoing";
  const heading = atReplay ? `Options suggested at ${localTime(recs.computed_utc)} ET`
    : ongoing ? "Options for riders now" : "Last options suggested during this event";
  const wrap = el("section", { class: "recos", "aria-labelledby": "recos-heading" }, [
    el("h4", { id: "recos-heading", text: heading }),
    el("p", { class: "muted small", text:
      `Computed ${localTime(recs.computed_utc)} ET. Ranked by the estimated time to get ` +
      "4 km away from the station (no destination is known). The usual pick is the option most " +
      "riders would take on a normal day; it is penalized when it looks crowded." }),
  ]);
  for (const st of recs.stations) {
    const ol = el("ol", { class: "reco-list" });
    for (const o of st.options) {
      const tags = [el("span", { class: "reco-type", text: OPTION_TEXT[o.type] || o.type }),
        el("span", { class: `reco-conf conf-${o.confidence}`, text: CONFIDENCE_TEXT[o.confidence] || o.confidence })];
      if (o.is_obvious) tags.push(el("span", { class: "reco-tag", text: "usual pick" }));
      if (o.crowded) tags.push(el("span", { class: "reco-tag crowded", text: "likely crowded" }));
      ol.append(el("li", { class: o.crowded ? "crowded" : "" }, [el("div", { class: "reco-tags" }, tags), el("p", { text: o.text })]));
    }
    wrap.append(el("h5", { text: `At ${st.station_name}` }), ol);
  }
  return wrap;
}

// ---------- replay -------------------------------------------------------------------------

function renderReplayControls(ev, detail) {
  const n = detail.series.t.length;
  const wrap = el("div", { class: "replay", role: "group", "aria-labelledby": "replay-heading" }, [
    el("h4", { id: "replay-heading", text: "Replay this event" }),
    el("p", { class: "muted small", text:
      "Move the slider (or press Play) to see the map, the charts and the suggested options " +
      "change over time. Station pins show the worst observed delay in minutes at that moment." }),
  ]);
  if (!n) return wrap;
  const play = el("button", { type: "button", class: "replay-play", id: "replay-play", "aria-pressed": "false",
    text: "▶ Play", onclick: () => toggleReplay() });
  const slider = el("input", { type: "range", id: "replay-slider", min: 0, max: n - 1, value: n - 1, step: 1,
    "aria-label": "Replay time", oninput: (e) => updateReplay(Number(e.target.value)) });
  const time = el("output", { id: "replay-time", for: "replay-slider" });
  wrap.append(el("div", { class: "replay-row" }, [play, slider, time]));
  return wrap;
}

function setupReplay(ev, detail) {
  stopReplay();
  if (state.layers.replay) state.layers.replay.clearLayers();
  else state.layers.replay = L.layerGroup().addTo(state.map);
  state.replay = { ev, detail, timer: null, index: detail.series.t.length - 1 };
  updateReplay(state.replay.index);
}

function delayBand(v) {
  return DELAY_BANDS.find(([min]) => v >= min)[1];
}

function updateReplay(i) {
  const r = state.replay;
  if (!r || !r.detail.series.t.length) return;
  r.index = i;
  const t = r.detail.series.t[i];
  const slider = document.getElementById("replay-slider");
  if (slider && Number(slider.value) !== i) slider.value = i;
  const label = `${localDay(t)} ${localTime(t)} ET`;
  if (slider) slider.setAttribute("aria-valuetext", label);
  const out = document.getElementById("replay-time");
  if (out) out.textContent = label;
  for (const c of state.charts) {
    c.$replayIndex = i;
    c.update("none");
  }
  // Map: worst observed delay per affected station at this moment (number + color band).
  const layer = state.layers.replay;
  layer.clearLayers();
  for (const st of r.ev.stations) {
    const series = (r.detail.station_series || {})[st.id];
    if (!series || st.lat === null) continue;
    const v = series[i];
    const known = v !== null && v !== undefined;
    const txt = known ? String(Math.round(v)) : "–";
    const band = known ? delayBand(v) : "unknown";
    const icon = L.divIcon({ className: "", html: `<span class="badge-pin replay-pin impact-${band}">${txt}</span>`,
      iconSize: [26, 26], iconAnchor: [13, 13] });
    const desc = known ? `trains up to ${fmt(v)} min late` : "no observation";
    L.marker([st.lat, st.lon], { icon, title: `${st.name}: ${desc} at ${localTime(t)} ET`, zIndexOffset: 1000 })
      .bindTooltip(tip(`${st.name}: ${desc} at ${localTime(t)} ET`)).addTo(layer);
  }
  // Recommendations in effect at this moment (latest set computed at or before t).
  const box = document.getElementById("replay-recos");
  if (!box) return;
  const history = r.detail.recommendation_history || [];
  const current = history.filter((h) => h.computed_utc <= t).pop();
  const last = i === r.detail.series.t.length - 1;
  if (current) box.replaceChildren(renderRecommendations(r.ev, current, !last));
  else if (last && r.detail.recommendations) box.replaceChildren(renderRecommendations(r.ev, r.detail.recommendations));
  else box.replaceChildren(el("p", { class: "muted small", text: history.length
    ? "No options had been computed yet at this moment."
    : "No options were computed for this event (they are only made while a disruption is ongoing)." }));
}

function stopReplay() {
  if (state.replay && state.replay.timer) clearInterval(state.replay.timer);
  if (state.replay) state.replay.timer = null;
  const btn = document.getElementById("replay-play");
  if (btn) { btn.setAttribute("aria-pressed", "false"); btn.textContent = "▶ Play"; }
}

function toggleReplay() {
  const r = state.replay;
  if (!r) return;
  if (r.timer) { stopReplay(); return; }
  const n = r.detail.series.t.length;
  if (r.index >= n - 1) updateReplay(0);
  const btn = document.getElementById("replay-play");
  btn.setAttribute("aria-pressed", "true");
  btn.textContent = "❚❚ Pause";
  r.timer = setInterval(() => {
    if (r.index >= n - 1) { stopReplay(); return; }
    updateReplay(r.index + 1);
  }, 400);
}

function renderNotable(notable) {
  const box = document.getElementById("notable-list");
  if (!box) return;
  box.replaceChildren();
  const items = notable.map((n) => [n, state.byId.get(n.id)]).filter(([, ev]) => ev);
  if (!items.length) {
    box.append(el("p", { class: "muted small", text: "No ended events with enough data yet." }));
    return;
  }
  for (const [n, ev] of items) {
    const why = n.basis === "impact" && ev.impact_score !== null
      ? `impact ${Math.round(ev.impact_score)}/100`
      : `peak delay ${fmt(ev.peak_delay_min)} min for ${fmt(ev.duration_min, 0)} min`;
    box.append(el("button", { type: "button", class: "notable-card", "data-id": ev.id,
      onclick: async () => { await selectEvent(ev.id, false); toggleReplay(); } }, [
      routeBullet(ev.route),
      el("span", { class: "notable-text" }, [el("strong", { text: `${localDay(ev.start_utc)} ${localTime(ev.start_utc)} ET` }), ` · ${why}`]),
      el("span", { class: "notable-play", "aria-hidden": "true", text: "▶ Replay" }),
    ]));
  }
}

async function selectEvent(id, fromList) {
  const ev = state.byId.get(id);
  if (!ev) return;
  state.selected = id;
  for (const card of document.querySelectorAll(".event-card")) {
    card.setAttribute("aria-pressed", card.dataset.id === id ? "true" : "false");
  }
  if (!fromList) {
    const card = document.querySelector(`.event-card[data-id="${CSS.escape(id)}"]`);
    if (card) card.scrollIntoView({ block: "nearest" });
  }
  history.replaceState(null, "", `#event=${encodeURIComponent(id)}`);
  stopReplay();
  const box = document.getElementById("detail");
  box.replaceChildren(el("p", { class: "muted", text: "Loading event detail…" }));
  try {
    const detail = await getJSON(`data/${ev.detail}`);
    if (state.selected !== id) return;
    renderDetail(ev, detail);
    showNearby(detail);
  } catch (err) {
    box.replaceChildren(el("p", { class: "muted", text: `Could not load the event detail (${err.message}).` }));
  }
}

// ---------- start --------------------------------------------------------------------------

async function main() {
  try {
    const [status, stations, events] = await Promise.all([
      getJSON("data/status.json"), getJSON("data/stations.json"), getJSON("data/events.json"),
    ]);
    renderStatus(status);
    state.events = events.events;
    state.stations = stations.stations;
    for (const ev of state.events) state.byId.set(ev.id, ev);
    renderEvents(state.events.filter((e) => e.in_window !== false));
    renderNotable(events.notable || []);
    initMap(state.stations);
    const wanted = new URLSearchParams(location.hash.slice(1)).get("event");
    if (wanted && state.byId.has(wanted)) selectEvent(wanted, false);
    else if (state.events.length) {
      const first = document.querySelector(".event-card") || document.querySelector(".notable-card");
      if (first) selectEvent(first.dataset.id, true);
    }
  } catch (err) {
    document.getElementById("last-update").textContent =
      `The dashboard data is not available yet (${err.message}).`;
  }
}

document.addEventListener("DOMContentLoaded", main);
