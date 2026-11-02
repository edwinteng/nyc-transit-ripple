# Evaluation report

Generated 2026-11-02T17:08:00Z for **2026-10-03 to 2026-11-02** (30 days). Metrics based on fewer than 30 observations are marked *preliminary*.

Generated automatically by `ripple evaluate` (weekly GitHub Actions workflow); do not edit by hand.

## Event detection vs. official MTA alerts

*Status: ok; data: 2026-11-01 to 2026-11-02.*

9 of 39 detected events matched an unplanned MTA alert (precision 23%), and 9 of 30 alert-lines had a matching event (recall 30%). Median time to detect: +4.8 min.

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Precision (events matching an alert) | 23% |  | 39 |  |
| Recall (alerts with a matching event) | 30% |  | 30 |  |
| Median time to detect (event start - alert time) | 4.8 min |  | 9 | negative: the event was detected before the alert appeared; preliminary |
| Mean time to detect | 6.3 min |  | 9 | preliminary |

**Per line**

| line | events | alerts | matched | precision | recall | ttd_median_min |
|---|---|---|---|---|---|---|
| 1 | 2 | 4 | 1 | 0.5 | 0.25 | 8.6 |
| 2 | 0 | 2 | 0 |  | 0 |  |
| 3 | 2 | 3 | 0 | 0 | 0 |  |
| 4 | 1 | 0 | 0 | 0 |  |  |
| 5 | 1 | 0 | 0 | 0 |  |  |
| 6 | 0 | 4 | 0 |  | 0 |  |
| 7 | 0 | 1 | 0 |  | 0 |  |
| A | 7 | 2 | 2 | 0.286 | 1.0 | 12.2 |
| C | 6 | 1 | 1 | 0.167 | 1.0 | 14.6 |
| D | 0 | 2 | 0 |  | 0 |  |
| E | 3 | 0 | 0 | 0 |  |  |
| F | 7 | 1 | 1 | 0.143 | 1.0 | 1.3 |
| G | 2 | 1 | 0 | 0 | 0 |  |
| J | 1 | 0 | 0 | 0 |  |  |
| L | 1 | 0 | 0 | 0 |  |  |
| N | 4 | 5 | 3 | 0.75 | 0.6 | -0.3 |
| Q | 1 | 2 | 1 | 1.0 | 0.5 | 4.3 |
| R | 1 | 2 | 0 | 0 | 0 |  |

**Unmatched events**

| event_id | line | start_utc | duration_min | peak_delay_min |
|---|---|---|---|---|
| N-20261102T0221Z | N | 2026-11-02 02:21 | 8.0 | 5.7 |
| A-20261101T2300Z | A | 2026-11-01 23:00 | 8.0 | 10.8 |
| C-20261101T2300Z | C | 2026-11-01 23:00 | 8.0 | 12.8 |
| 3-20261101T2240Z | 3 | 2026-11-01 22:40 | 7.0 | 6.0 |
| F-20261101T2239Z | F | 2026-11-01 22:39 | 8.0 | 12.1 |
| A-20261101T2239Z | A | 2026-11-01 22:39 | 8.0 | 12.8 |
| C-20261101T2239Z | C | 2026-11-01 22:39 | 8.0 | 10.5 |
| F-20261101T2151Z | F | 2026-11-01 21:51 | 8.0 | 10.7 |
| A-20261101T2151Z | A | 2026-11-01 21:51 | 8.0 | 9.7 |
| 4-20261101T2151Z | 4 | 2026-11-01 21:51 | 8.0 | 6.6 |
| C-20261101T2151Z | C | 2026-11-01 21:51 | 8.0 | 8.3 |
| 5-20261101T1931Z | 5 | 2026-11-01 19:31 | 8.0 | 7.4 |
| C-20261101T1931Z | C | 2026-11-01 19:31 | 8.0 | 7.8 |
| A-20261101T1931Z | A | 2026-11-01 19:31 | 8.0 | 13.8 |
| F-20261101T1931Z | F | 2026-11-01 19:31 | 8.0 | 10.3 |
| F-20261101T1500Z | F | 2026-11-01 15:00 | 8.0 | 11.8 |
| G-20261101T1500Z | G | 2026-11-01 15:00 | 8.0 | 6.5 |
| E-20261101T0803Z | E | 2026-11-01 08:03 | 8.0 | 8.6 |
| F-20261101T0803Z | F | 2026-11-01 08:03 | 8.0 | 5.9 |
| 1-20261101T0803Z | 1 | 2026-11-01 08:03 | 8.0 | 8.4 |
| J-20261101T0803Z | J | 2026-11-01 08:03 | 8.0 | 6.3 |
| F-20261101T0344Z | F | 2026-11-01 03:44 | 7.0 | 5.7 |
| R-20261101T0344Z | R | 2026-11-01 03:44 | 7.0 | 6.2 |
| E-20261101T0343Z | E | 2026-11-01 03:43 | 8.0 | 12.6 |
| G-20261101T0342Z | G | 2026-11-01 03:42 | 8.0 | 6.7 |
| L-20261101T0342Z | L | 2026-11-01 03:42 | 8.0 | 8.8 |
| C-20261101T0248Z | C | 2026-11-01 02:48 | 8.0 | 19.2 |
| 3-20261101T0248Z | 3 | 2026-11-01 02:48 | 8.0 | 6.3 |
| A-20261101T0248Z | A | 2026-11-01 02:48 | 8.0 | 13.4 |
| E-20261101T0248Z | E | 2026-11-01 02:48 | 8.0 | 9.2 |

- MTA alerts are an imperfect ground truth: planned work is sometimes posted as delays, short delays often get no alert, alerts can be posted late or kept after service recovered, and they name lines that were only indirectly affected.
- An alert matches an event of the same line if it appeared between 15 min before the event start and 15 min after its end; one to one.
- Unmatched events are listed for manual review: they may be real delays without an alert, or false detections.
- 53 alert-line(s) were left out of recall because no subway data was collected for that line in the 30 minutes after the alert (pipeline not running).

## Surface impact validity (placebo test)

*Status: insufficient data; data: 2026-10-03 to 2026-11-02.*

Not enough ended events with matched non-event data yet (at least 2 per signal are needed); no conclusion about the surface impact can be drawn.

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Traffic speed: change during events vs. same stations at matched times | n/a |  | 0 | too few events; preliminary |
| Traffic speed: change during events vs. control stations | n/a |  | 0 | too few events; preliminary |
| Bus speed: change during events vs. same stations at matched times | n/a |  | 0 | too few events; preliminary |
| Bus speed: change during events vs. control stations | n/a |  | 0 | too few events; preliminary |
| Citi Bikes available: change during events vs. same stations at matched times | n/a |  | 0 | too few events; preliminary |
| Citi Bikes available: change during events vs. control stations | n/a |  | 0 | too few events; preliminary |

- (a) compares the stations near an event with the same stations at the same time of day on earlier days without an event there; (b) also subtracts the change at control stations at least 2000 m from any affected station (difference in differences).
- Negative differences mean slower traffic or buses, or fewer bikes, during events. 95% intervals are percentile bootstraps over events.
- Cameras are not tested: they are only counted during events, so there are no non-event camera counts to compare with.

## Camera counting accuracy

*Status: skipped; data: not time-bound.*

No manual labels yet (docs/evaluation/camera_labels.csv does not exist); camera accuracy is not evaluated.

- True counts come from a person labeling sampled frames (ripple-cameras sample-frames, then ripple-cameras evaluate); frames are deleted afterwards and only the numbers are kept.
- Bias = mean of (model - true): negative means the model under-counts.

## Recommendation replay

*Status: insufficient data; data: 2026-10-03 to 2026-11-02.*

No recommendations were logged in this period (they are only made while a disruption is ongoing).

- Each logged recommendation is replayed against what happened next: the real recovery time of the event, the first train observed at the suggested station, and the bus speed measured in the next 30 minutes.
- Trains are observed at poll times (about a minute apart, with gaps when GitHub delays runs), so realized waits are approximate.
- There is no destination, so 'beating the obvious option' compares the time to get 4 km away.

## Explanation consistency

*Status: ok; data: 2026-10-03 to 2026-11-02.*

0 violation(s) in 78 event sentence(s) and 0 recommendation text(s).

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Sentences with a number not in their metrics | 0 sentences |  | 78 |  |

- Every stored event sentence and recommendation text is re-checked: each number in the text must equal one of the metrics stored with it (quoted alert text excepted). Any violation fails the evaluation workflow; the test suite runs the same check over every template variant.

## Pipeline reliability

*Status: ok; data: 2026-11-01 to 2026-11-02.*

14 source(s) monitored; lowest uptime: traffic at 0% of 10 fetches. Median 201.5 min between ingest runs (10 runs).

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Uptime: alerts | 100% |  | 10 | preliminary |
| Uptime: bike_discovery | 100% |  | 2 | preliminary |
| Uptime: bike_info | 100% |  | 2 | preliminary |
| Uptime: bike_status | 100% |  | 10 | preliminary |
| Uptime: bus | 71% |  | 7 | preliminary |
| Uptime: subway:ace | 100% |  | 30 |  |
| Uptime: subway:bdfm | 100% |  | 30 |  |
| Uptime: subway:g | 100% |  | 30 |  |
| Uptime: subway:jz | 100% |  | 30 |  |
| Uptime: subway:l | 100% |  | 30 |  |
| Uptime: subway:nqrw | 100% |  | 30 |  |
| Uptime: subway:numbered | 100% |  | 30 |  |
| Uptime: subway:si | 100% |  | 30 |  |
| Uptime: traffic | 0% |  | 10 | preliminary |
| Median minutes between ingest runs | 201.5 min |  | 10 | preliminary |
| Run success rate: CI | 100% |  | 20 | average 0.5 min per run; preliminary |
| Run success rate: Cameras | 100% |  | 4 | average 0.6 min per run; preliminary |
| Run success rate: Check MTA Bus Time key | 40% |  | 5 | average 0.2 min per run; preliminary |
| Run success rate: Deploy dashboard | 100% |  | 12 | average 0.7 min per run; preliminary |
| Run success rate: Evaluate | 100% |  | 1 | average 0.3 min per run; preliminary |
| Run success rate: Ingest and analyse | 100% |  | 10 | average 2.6 min per run; preliminary |

**Sources**

| source | fetches | uptime | age_median_s | age_p95_s | gaps | gap_minutes | first_utc | last_utc |
|---|---|---|---|---|---|---|---|---|
| alerts | 10 | 1.0 | 332.7 | 931.0 | 9 | 1854.9 | 2026-11-01 02:50 | 2026-11-02 09:45 |
| bike_discovery | 2 | 1.0 |  |  | 0 | 0 | 2026-11-01 02:50 | 2026-11-02 09:45 |
| bike_info | 2 | 1.0 | 72.0 | 80.5 | 0 | 0 | 2026-11-01 02:50 | 2026-11-02 09:45 |
| bike_status | 10 | 1.0 | 57.9 | 78.3 | 9 | 1855.0 | 2026-11-01 02:50 | 2026-11-02 09:45 |
| bus | 7 | 0.714 | 8.5 | 13.9 | 0 | 0 | 2026-11-01 15:00 | 2026-11-02 09:45 |
| subway:ace | 30 | 1.0 | 5.2 | 10.5 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| subway:bdfm | 30 | 1.0 | 4.5 | 12.7 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| subway:g | 30 | 1.0 | 5.0 | 13.7 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| subway:jz | 30 | 1.0 | 4.4 | 12.7 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| subway:l | 30 | 1.0 | 4.5 | 9.3 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| subway:nqrw | 30 | 1.0 | 5.0 | 11.7 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| subway:numbered | 30 | 1.0 | 4.9 | 5.7 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| subway:si | 30 | 1.0 | 7.3 | 13.8 | 0 | 0 | 2026-11-01 02:48 | 2026-11-02 09:45 |
| traffic | 10 | 0 |  |  | 0 | 0 | 2026-11-01 02:50 | 2026-11-02 09:45 |

**Cadence**

| runs | median_minutes_between_runs | p95_minutes_between_runs | longest_gap_minutes |
|---|---|---|---|
| 10 | 201.5 | 431.6 | 441.5 |

**Workflows**

| workflow | runs | success_rate | cancelled_or_skipped | avg_minutes |
|---|---|---|---|---|
| CI | 20 | 1.0 | 0 | 0.475 |
| Cameras | 4 | 1.0 | 0 | 0.6 |
| Check MTA Bus Time key | 5 | 0.4 | 0 | 0.227 |
| Deploy dashboard | 12 | 1.0 | 0 | 0.733 |
| Evaluate | 1 | 1.0 | 0 | 0.283 |
| Ingest and analyse | 10 | 1.0 | 0 | 2.6 |

- Uptime is the share of fetches whose data was usable (status ok or duplicate); stale, missing, invalid and unavailable fetches count as down.
- GitHub runs scheduled workflows on a best-effort basis, so the time between runs is often longer than the 5-minute schedule.
