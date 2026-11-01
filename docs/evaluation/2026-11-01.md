# Evaluation report

Generated 2026-11-01T04:35:00Z for **2026-10-02 to 2026-11-01** (30 days). Metrics based on fewer than 30 observations are marked *preliminary*.

Generated automatically by `ripple evaluate` (weekly GitHub Actions workflow); do not edit by hand.

## Event detection vs. official MTA alerts

*Status: ok; data: 2026-11-01 to 2026-11-01.*

2 of 6 detected events matched an unplanned MTA alert (precision 33%), and 2 of 4 alert-lines had a matching event (recall 50%). Median time to detect: +3.9 min.

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Precision (events matching an alert) | 33% |  | 6 | preliminary |
| Recall (alerts with a matching event) | 50% |  | 4 | preliminary |
| Median time to detect (event start - alert time) | 3.9 min |  | 2 | negative: the event was detected before the alert appeared; preliminary |
| Mean time to detect | 3.9 min |  | 2 | preliminary |

**Per line**

| line | events | alerts | matched | precision | recall | ttd_median_min |
|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 1 | 1.0 | 1.0 | 8.6 |
| 3 | 1 | 0 | 0 | 0 |  |  |
| A | 1 | 1 | 0 | 0 | 0 |  |
| C | 1 | 0 | 0 | 0 |  |  |
| E | 1 | 0 | 0 | 0 |  |  |
| N | 1 | 2 | 1 | 1.0 | 0.5 | -0.8 |

**Unmatched events**

| event_id | line | start_utc | duration_min | peak_delay_min |
|---|---|---|---|---|
| E-20261101T0248Z | E | 2026-11-01 02:48 | 8.0 | 9.2 |
| A-20261101T0248Z | A | 2026-11-01 02:48 | 8.0 | 13.4 |
| C-20261101T0248Z | C | 2026-11-01 02:48 | 8.0 | 19.2 |
| 3-20261101T0248Z | 3 | 2026-11-01 02:48 | 8.0 | 6.3 |

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

0 violation(s) in 12 event sentence(s) and 0 recommendation text(s).

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Sentences with a number not in their metrics | 0 sentences |  | 12 | preliminary |

- Every stored event sentence and recommendation text is re-checked: each number in the text must equal one of the metrics stored with it (quoted alert text excepted). Any violation fails the evaluation workflow; the test suite runs the same check over every template variant.

## Pipeline reliability

*Status: ok; data: 2026-11-01 to 2026-11-01.*

13 source(s) monitored; lowest uptime: traffic at 0% of 2 fetches. Median 54.6 min between ingest runs (2 runs).

| Metric | Value | 95% interval | n | Note |
|---|---|---|---|---|
| Uptime: alerts | 100% |  | 2 | preliminary |
| Uptime: bike_discovery | 100% |  | 1 | preliminary |
| Uptime: bike_info | 100% |  | 1 | preliminary |
| Uptime: bike_status | 100% |  | 2 | preliminary |
| Uptime: subway:ace | 100% |  | 6 | preliminary |
| Uptime: subway:bdfm | 100% |  | 6 | preliminary |
| Uptime: subway:g | 100% |  | 6 | preliminary |
| Uptime: subway:jz | 100% |  | 6 | preliminary |
| Uptime: subway:l | 100% |  | 6 | preliminary |
| Uptime: subway:nqrw | 100% |  | 6 | preliminary |
| Uptime: subway:numbered | 100% |  | 6 | preliminary |
| Uptime: subway:si | 100% |  | 6 | preliminary |
| Uptime: traffic | 0% |  | 2 | preliminary |
| Median minutes between ingest runs | 54.6 min |  | 2 | preliminary |

**Sources**

| source | fetches | uptime | age_median_s | age_p95_s | gaps | gap_minutes | first_utc | last_utc |
|---|---|---|---|---|---|---|---|---|
| alerts | 2 | 1.0 | 73.8 | 99.4 | 1 | 54.6 | 2026-11-01 02:50 | 2026-11-01 03:45 |
| bike_discovery | 1 | 1.0 |  |  | 0 | 0 | 2026-11-01 02:50 | 2026-11-01 02:50 |
| bike_info | 1 | 1.0 | 81.4 | 81.4 | 0 | 0 | 2026-11-01 02:50 | 2026-11-01 02:50 |
| bike_status | 2 | 1.0 | 67.3 | 78.2 | 1 | 54.6 | 2026-11-01 02:50 | 2026-11-01 03:45 |
| subway:ace | 6 | 1.0 | 5.9 | 11.0 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| subway:bdfm | 6 | 1.0 | 2.4 | 2.9 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| subway:g | 6 | 1.0 | 4.3 | 6.0 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| subway:jz | 6 | 1.0 | 2.9 | 4.9 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| subway:l | 6 | 1.0 | 6.9 | 8.0 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| subway:nqrw | 6 | 1.0 | 3.9 | 8.9 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| subway:numbered | 6 | 1.0 | 4.5 | 4.9 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| subway:si | 6 | 1.0 | 4.5 | 8.1 | 0 | 0 | 2026-11-01 02:48 | 2026-11-01 03:45 |
| traffic | 2 | 0 |  |  | 0 | 0 | 2026-11-01 02:50 | 2026-11-01 03:45 |

**Cadence**

| runs | median_minutes_between_runs | p95_minutes_between_runs | longest_gap_minutes |
|---|---|---|---|
| 2 | 54.6 | 54.6 | 54.6 |

- Uptime is the share of fetches whose data was usable (status ok or duplicate); stale, missing, invalid and unavailable fetches count as down.
- GitHub runs scheduled workflows on a best-effort basis, so the time between runs is often longer than the 5-minute schedule.
- Workflow statistics were skipped (no GITHUB_TOKEN / GITHUB_REPOSITORY; they are collected in the evaluation workflow).
