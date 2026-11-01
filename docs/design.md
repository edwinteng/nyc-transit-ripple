# Design Document: NYC Subway Disruption Ripple System

## 1. Problem

When the NYC subway is disrupted, riders flood the streets at once — switching to buses, ride-hailing, or Citi Bike — causing secondary surface congestion. Navigation apps give everyone similar rerouting advice, so the most obvious alternative gets overwhelmed.

Core question: When the subway breaks down, does the surface break down too? How big, how long, where? Should a rider go now, switch modes, or wait?

## 2. Goals

- Ingest real-time subway, traffic speed, bus, Citi Bike, traffic camera, and weather data on one timeline.
- On a subway delay, assess nearby surface traffic and crowding of alternatives.
- Build history to estimate disruption recovery time and surface clearing time.
- Generate plain-language explanations and recommendations (go / switch / wait).
- Measure accuracy of explanations and recommendations.
- Near-zero cost: no paid API keys.

Non-goals: full navigation app; mobile app (start with a static web dashboard); storing large volumes of camera images.

## 3. Data sources

- MTA GTFS-Realtime (subway), ~30s: delays, per-trip actual delay.
- MTA Service Alerts: cause and scope.
- MTA GTFS static: schedules, station locations.
- MTA Bus Time (SIRI), ~30s: bus slowdowns (free MTA key via GitHub Secrets; later milestone).
- Citi Bike GBFS, ~1 min: bike depletion near stations.
- NYC DOT Real-Time Traffic Speed, ~5 min: primary congestion signal.
- NYCTMC traffic webcams (no key): side streets and bus-stop queues near stations (supplementary).
- Open-Meteo weather, hourly: control variable.

## 4. Architecture

- GitHub Actions: fetch -> normalize -> quality checks; detect delay events; on event, fetch nearby camera frames -> YOLO counts -> store numbers only; write Parquet/DuckDB.
- GitHub Actions also generates all explanations with rule-based templates, right after event detection; a separate Pages workflow exports compact JSON from the stored data and deploys the static site. No local machine, local model or Ollama is involved, so explanations are never delayed by an offline computer.
- Static website (GitHub Pages): events, map, explanations, history. Visitors cannot trigger any computation; pages are static files.

## 5. Pipeline details

- UTC timestamps, 1-minute buckets; forward-fill low-frequency data and record freshness.
- Deduplicate; mark stale feeds as missing (never "normal"); filter abnormal time jumps; quality report every run.
- Raw data short-term, aggregates long-term; large files in GitHub Releases or free object storage.

## 6. Event detection

- Delay event: a line exceeds a delay threshold for N consecutive minutes, or a service alert appears.
- Surface impact: within 500 m of the station, compare speed, camera counts, bus speed, bike availability vs. a same-weekday, same-time baseline.
- Combine into a 0-100 surface impact score: a weighted mean of per-signal sub-scores (configurable weights), renormalized over the signals that have data and a reliable baseline; the signals used are stored with the score (M4; formula in docs/data-model.md).

## 7. Cameras and cost control

- Analyze only a few cameras near the affected station, only during events.
- Layer 1: open-source YOLO on GitHub Actions (vehicle/pedestrian counts, queue estimates).
- No second, model-based layer: anomalous snapshots kept as evidence can be reviewed manually.
- Do not store images except a few anomaly snapshots as evidence.

## 8. Recommendation engine

Compare estimated total time for: walking to another working line; Citi Bike; bus; waiting (historical recovery time); and avoiding the crowd (if the obvious alternative is overwhelmed, recommend the less crowded second-best option). Output ranked options plus explanation.

## 9. Explanation layer

- Rule-based templates only, executed inside GitHub Actions. No language model (local or hosted) is used.
- Templates may only cite numbers present in the event data; every summary is stored with the list of cited metrics, and tests check that no other number appears.
- When data is missing, stale or the baseline has little history, the summary says so instead of guessing.

## 10. Evaluation

- Event detection vs. official MTA alerts (precision/recall).
- Manually sample camera snapshots to validate YOLO counts.
- Replay past events to check whether recommended options beat the obvious option.
- Check generated explanations for numbers that are not present in the data (automated) and for misleading wording (manual sample).

## 11. Security and privacy

- Secrets only in GitHub Secrets or local .env (gitignored).
- No face or license plate recognition; counts only.

## 12. Milestones

- M1: skeleton, subway ingestion, quality checks.
- M2: delay event detection, traffic speed, Citi Bike.
- M3: cameras + YOLO (the impact score moved to M4).
- M4: static website (GitHub Pages), cameras during events, surface impact score, rule-based explanations.
- M5: richer rule-based explanations, bus data, recommendation engine.
- M6: evaluation framework, README, demo.

## 13. Known risks

- Low-quality, moving cameras limit YOLO accuracy.
- GitHub Actions cron minimum ~5 min and not guaranteed on time.
- Template-based explanations are less flexible than free text; wording must be maintained as metrics are added.
- The "avoid the crowd" benefit must be validated and may not hold.

## 14. Revision history

- M2: Design change: no local model or Ollama. All explanations are generated by rule-based templates inside GitHub Actions (sections 4, 7, 9, 10, 12 and 13 updated).
- M2: Delay events are defined per line as "the worst-direction median train delay is at least a threshold for N consecutive minutes" (threshold, N and the metric are configurable), and are linked to MTA service alerts that overlap them in time and route.
- M4: The camera step runs only for ongoing events, most severe first, with a hard cap on cameras per run (default 30); counts are stored with the event.
- M4: Surface impact score defined (traffic speed drop, camera count increase, Citi Bike drop vs. the same weekday/time baseline; weights 0.4/0.3/0.3, renormalized over usable signals; low-confidence baselines excluded). Bus speed joins in M5.
- M4: Explanations are one or two template sentences per event, each stored with the metrics it quotes.
- M4: Public dashboard: static site (HTML/CSS/JS, Leaflet + OpenStreetMap, Chart.js) on GitHub Pages, fed by compact JSON exported from the data branch at deploy time; no Parquet is served.
