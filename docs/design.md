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
- Owner's local machine: scheduled script pulls latest events/metrics (text only) -> local Ollama model (Qwen/Llama) writes explanations -> pushes back to repo. Never exposes ports.
- Static website (GitHub Pages): events, map, explanations, history. Visitors cannot trigger AI calls.

## 5. Pipeline details

- UTC timestamps, 1-minute buckets; forward-fill low-frequency data and record freshness.
- Deduplicate; mark stale feeds as missing (never "normal"); filter abnormal time jumps; quality report every run.
- Raw data short-term, aggregates long-term; large files in GitHub Releases or free object storage.

## 6. Event detection

- Delay event: a line exceeds a delay threshold for N consecutive minutes, or a service alert appears.
- Surface impact: within 500 m of the station, compare speed, camera counts, bus speed, bike availability vs. a same-weekday, same-time baseline.
- Combine into a 0-100 surface impact score.

## 7. Cameras and cost control

- Analyze only a few cameras near the affected station, only during events.
- Layer 1: open-source YOLO on GitHub Actions (vehicle/pedestrian counts, queue estimates).
- Layer 2 (optional): local multimodal model on a few anomalous snapshots.
- Do not store images except a few anomaly snapshots as evidence.

## 8. Recommendation engine

Compare estimated total time for: walking to another working line; Citi Bike; bus; waiting (historical recovery time); and avoiding the crowd (if the obvious alternative is overwhelmed, recommend the less crowded second-best option). Output ranked options plus explanation.

## 9. AI explanation layer

- Default: rule-based templates. Advanced: local Ollama model on structured metrics.
- AI may only cite numbers present in its input; output lists cited metrics.

## 10. Evaluation

- Event detection vs. official MTA alerts (precision/recall).
- Manually sample camera snapshots to validate YOLO counts.
- Replay past events to check whether recommended options beat the obvious option.
- Check AI explanations for fabricated or inconsistent numbers.

## 11. Security and privacy

- Secrets only in GitHub Secrets or local .env (gitignored).
- No face or license plate recognition; counts only.

## 12. Milestones

- M1: skeleton, subway ingestion, quality checks.
- M2: delay event detection, traffic speed, Citi Bike.
- M3: cameras + YOLO, surface impact score.
- M4: static website, rule-based explanations.
- M5: local Ollama explanations, bus data, recommendation engine.
- M6: evaluation framework, README, demo.

## 13. Known risks

- Low-quality, moving cameras limit YOLO accuracy.
- GitHub Actions cron minimum ~5 min and not guaranteed on time.
- Explanations are delayed when the local machine is off.
- The "avoid the crowd" benefit must be validated and may not hold.
