# Evaluation report

Generated 2026-11-01T13:30:00Z for **2026-10-02 to 2026-11-01** (30 days). Metrics based on fewer than 30 observations are marked *preliminary*.

Generated automatically by `ripple evaluate` (weekly GitHub Actions workflow); do not edit by hand.

## Event detection vs. official MTA alerts

*Status: ok; data: 2026-11-01 to 2026-11-01.*

4 of 13 detected events matched an unplanned MTA alert (precision 31%), and 4 of 5 alert-lines had a matching event (recall 80%). Median time to detect: +4.1 min.

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Precision (events matching an alert) | 31% |  | 13 | preliminary |
| Recall (alerts with a matching event) | 80% |  | 5 | preliminary |
| Median time to detect (event start - alert time) | 4.2 min |  | 4 | negative: the event was detected before the alert appeared; preliminary |
| Mean time to detect | 4.3 min |  | 4 | preliminary |

**Per line**

| line | events | alerts | matched | precision | recall | ttd_median_min |
|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 1 | 1.0 | 1.0 | 8.6 |
| 3 | 1 | 0 | 0 | 0 |  |  |
| 6 | 0 | 1 | 0 |  | 0 |  |
| A | 2 | 1 | 1 | 0.5 | 1.0 | 9.8 |
| C | 1 | 0 | 0 | 0 |  |  |
| E | 2 | 0 | 0 | 0 |  |  |
| F | 1 | 0 | 0 | 0 |  |  |
| G | 1 | 0 | 0 | 0 |  |  |
| L | 1 | 0 | 0 | 0 |  |  |
| N | 2 | 2 | 2 | 1.0 | 1.0 | -0.55 |
| R | 1 | 0 | 0 | 0 |  |  |

**Unmatched events**

| event_id | line | start_utc | duration_min | peak_delay_min |
|---|---|---|---|---|
| F-20261101T0344Z | F | 2026-11-01 03:44 | 7.0 | 5.7 |
| R-20261101T0344Z | R | 2026-11-01 03:44 | 7.0 | 6.2 |
| E-20261101T0343Z | E | 2026-11-01 03:43 | 8.0 | 12.6 |
| L-20261101T0342Z | L | 2026-11-01 03:42 | 8.0 | 8.8 |
| G-20261101T0342Z | G | 2026-11-01 03:42 | 8.0 | 6.7 |
| 3-20261101T0248Z | 3 | 2026-11-01 02:48 | 8.0 | 6.3 |
| A-20261101T0248Z | A | 2026-11-01 02:48 | 8.0 | 13.4 |
| E-20261101T0248Z | E | 2026-11-01 02:48 | 8.0 | 9.2 |
| C-20261101T0248Z | C | 2026-11-01 02:48 | 8.0 | 19.2 |

- MTA alerts are an imperfect ground truth: planned work is sometimes posted as delays, short delays often get no alert, alerts can be posted late or kept after service recovered, and they name lines that were only indirectly affected.
- An alert matches an event of the same line if it appeared between 15 min before the event start and 15 min after its end; one to one.
- Unmatched events are listed for manual review: they may be real delays without an alert, or false detections.
- 50 alert-line(s) were left out of recall because no subway data was collected for that line in the 30 minutes after the alert (pipeline not running).

## Surface impact validity (placebo test)

*Status: insufficient data; data: 2026-10-02 to 2026-11-01.*

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

*Status: insufficient data; data: 2026-10-02 to 2026-11-01.*

No recommendations were logged in this period (they are only made while a disruption is ongoing).

- Each logged recommendation is replayed against what happened next: the real recovery time of the event, the first train observed at the suggested station, and the bus speed measured in the next 30 minutes.
- Trains are observed at poll times (about a minute apart, with gaps when GitHub delays runs), so realized waits are approximate.
- There is no destination, so 'beating the obvious option' compares the time to get 4 km away.

## Explanation consistency

*Status: ok; data: 2026-10-02 to 2026-11-01.*

0 violation(s) in 26 event sentence(s) and 0 recommendation text(s).

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Sentences with a number not in their metrics | 0 sentences |  | 26 | preliminary |

- Every stored event sentence and recommendation text is re-checked: each number in the text must equal one of the metrics stored with it (quoted alert text excepted). Any violation fails the evaluation workflow; the test suite runs the same check over every template variant.

## Pipeline reliability

*Status: ok; data: 2026-11-01 to 2026-11-01.*

13 source(s) monitored; lowest uptime: traffic at 0% of 3 fetches. Median 157.6 min between ingest runs (3 runs).

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Uptime: alerts | 100% |  | 3 | preliminary |
| Uptime: bike_discovery | 100% |  | 1 | preliminary |
| Uptime: bike_info | 100% |  | 1 | preliminary |
| Uptime: bike_status | 100% |  | 3 | preliminary |
| Uptime: subway:ace | 100% |  | 9 | preliminary |
| Uptime: subway:bdfm | 100% |  | 9 | preliminary |
| Uptime: subway:g | 100% |  | 9 | preliminary |
| Uptime: subway:jz | 100% |  | 9 | preliminary |
| Uptime: subway:l | 100% |  | 9 | preliminary |
| Uptime: subway:nqrw | 100% |  | 9 | preliminary |
| Uptime: subway:numbered | 100% |  | 9 | preliminary |
| Uptime: subway:si | 100% |  | 9 | preliminary |
| Uptime: traffic | 0% |  | 3 | preliminary |
| Median minutes between ingest runs | 157.6 min |  | 3 | preliminary |
| Run success rate: CI | 100% |  | 17 | average 0.5 min per run; preliminary |
| Run success rate: Cameras | 100% |  | 4 | average 0.6 min per run; preliminary |
| Run success rate: Check MTA Bus Time key | 0% |  | 3 | average 0.2 min per run; preliminary |
| Run success rate: Deploy dashboard | 100% |  | 4 | average 0.8 min per run; preliminary |
| Run success rate: Ingest and analyse | 100% |  | 3 | average 2.6 min per run; preliminary |

**Sources**

| source | fetches | uptime | age_median_s | age_p95_s | gaps | gap_minutes | first_utc | last_utc |
|---|---|---|---|---|---|---|---|---|
| alerts | 3 | 1.0 | 102.2 | 441.7 | 2 | 315.2 | 2026-11-01 02:50 | 2026-11-01 08:05 |
| bike_discovery | 1 | 1.0 |  |  | 0 | 0 | 2026-11-01 02:50 | 2026-11-01 02:50 |
| bike_info | 1 | 1.0 | 81.4 | 81.4 | 0 | 0 | 2026-11-01 02:50 | 2026-11-01 02:50 |
| bike_status | 3 | 1.0 | 55.2 | 77.0 | 2 | 315.4 | 2026-11-01 02:50 | 2026-11-01 08:05 |
| subway:ace | 9 | 1.0 | 6.2 | 11.0 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| subway:bdfm | 9 | 1.0 | 2.8 | 9.2 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| subway:g | 9 | 1.0 | 5.9 | 13.2 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| subway:jz | 9 | 1.0 | 4.8 | 8.2 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| subway:l | 9 | 1.0 | 5.8 | 8.0 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| subway:nqrw | 9 | 1.0 | 7.2 | 8.7 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| subway:numbered | 9 | 1.0 | 4.8 | 5.4 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| subway:si | 9 | 1.0 | 5.2 | 8.0 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 08:05 |
| traffic | 3 | 0 |  |  | 0 | 0 | 2026-11-01 02:50 | 2026-11-01 08:05 |

**Cadence**

| runs | median_minutes_between_runs | p95_minutes_between_runs | longest_gap_minutes |
|---|---|---|---|
| 3 | 157.6 | 250.3 | 260.6 |

**Workflows**

| workflow | runs | success_rate | cancelled_or_skipped | avg_minutes |
|---|---|---|---|---|
| CI | 17 | 1.0 | 0 | 0.459 |
| Cameras | 4 | 1.0 | 0 | 0.6 |
| Check MTA Bus Time key | 3 | 0 | 0 | 0.239 |
| Deploy dashboard | 4 | 1.0 | 0 | 0.758 |
| Ingest and analyse | 3 | 1.0 | 0 | 2.6 |

- Uptime is the share of fetches whose data was usable (status ok or duplicate); stale, missing, invalid and unavailable fetches count as down.
- GitHub runs scheduled workflows on a best-effort basis, so the time between runs is often longer than the 5-minute schedule.
