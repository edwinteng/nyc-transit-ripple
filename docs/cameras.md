# Traffic cameras and vision counts (M3, used by events since M4)

Self-contained module: `src/transit_ripple/cameras/` (tests in `tests/cameras/`).
It turns public NYC traffic camera frames into **numbers only**: counts of cars,
buses, trucks and people near subway stations.

## Privacy and data handling

- Frames are downloaded into memory, counted and discarded. They are never written
  to disk, uploaded as artifacts or committed.
- Only per-class counts and frame metadata (size, timings) are stored. To detect
  frozen cameras, the SHA-256 of the last frame per camera is kept in
  `camera_state.json`; the image itself is not.
- The detector outputs generic COCO object classes. There is **no face or license
  plate recognition**, no tracking and no re-identification; bounding boxes are
  used only to remove duplicate detections and are discarded.
- The one exception is the accuracy-evaluation helper `sample-frames`, which saves
  a small sample (e.g. 50 frames) into a **local, gitignored** folder so a person
  can label them. Those files must not be committed or shared and should be
  deleted after labeling.

## Data sources

| Source | Endpoint | Notes |
|---|---|---|
| NYCTMC webcams | `https://webcams.nyctmc.org/api/cameras` | Public, no key. 972 cameras (id, name, borough, lat/lon, online flag, image URL). |
| Camera frame | `https://webcams.nyctmc.org/api/cameras/<id>/image` | 352x240 JPEG, ~10-16 KB, `Cache-Control: no-store`. No timestamp metadata. |
| Subway stations | M1 static GTFS (`stops.txt`, parent stations) | 496 stations. |

## Model and license

| Model | License (code and weights) | COCO AP | Input | Size | Used |
|---|---|---|---|---|---|
| **YOLOX-S** (Megvii) | **Apache-2.0** | 40.5 | 640x640 | 35.9 MB | default |
| YOLOX-Tiny | Apache-2.0 | 32.8 | 416x416 | 20.2 MB | option |
| YOLOX-Nano | Apache-2.0 | 25.8 | 416x416 | 3.7 MB | option |
| Ultralytics YOLOv8n / YOLO11n | AGPL-3.0 | 37.3 / 39.5 | 640x640 | | not used |

Ultralytics YOLO models are AGPL-3.0, which would impose copyleft obligations on
the whole service. YOLOX-S is permissively licensed and at least as accurate on
COCO as the nano Ultralytics models, so it is the default. It runs through ONNX
Runtime (MIT) on CPU with the official ONNX export from the YOLOX
[0.1.1rc0 release](https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0).
Weights are downloaded at runtime, verified against a pinned SHA-256 and cached
(`RIPPLE_MODEL_CACHE_DIR`, default `~/.cache/nyc-transit-ripple/models`); they are
never committed.

Counted COCO classes: `car` (2), `bus` (5), `truck` (7), `person` (0). Detections
need a score of at least 0.35 (`RIPPLE_CAMERA_SCORE_THRESHOLD`); duplicates are
removed with class-aware non-maximum suppression (IoU 0.45).

## Tables

| Table | Content |
|---|---|
| `reference/cameras.parquet` | Camera inventory: `camera_id`, `name`, `area`, `lat`, `lon`, `is_online`, `image_url`, `fetched_utc`. Refreshed at most weekly (`RIPPLE_CAMERA_INVENTORY_MAX_AGE_DAYS`). |
| `reference/camera_station_map.parquet` | Every (station, camera) pair within 500 m: `station_id`, `station_name`, `camera_id`, `camera_name`, `distance_m`. |
| `camera_counts/date=YYYY-MM-DD/<run_id>.parquet` | One row per camera and fetch: `bucket_utc` (1-minute UTC bucket of the fetch time), `observed_utc`, `status`, `car`, `bus`, `truck`, `person`, `repeat_frame`, `model`, `score_threshold`, image size, `fetch_ms` (network only), `inference_ms`. |
| `reports/cameras/date=YYYY-MM-DD/<run_id>.json` | Quality report per counting run. |

`status` is `ok`, `fetch_error` (after retries), `invalid_image` (not a decodable
image) or `frozen` (identical frame for 10+ minutes). Counts are NULL unless
`status = 'ok'`: a camera without data is never reported as zero traffic.
`transit_ripple.cameras.storage.connect(data_dir)` (or `register_views`) exposes
`camera_counts`, `camera_counts_all`, `cameras` and `camera_station_map` in DuckDB.

On a live run: 307 of 496 subway stations have at least one camera within 500 m
(median 3, max 32); 544 of the 972 cameras are near a station.

## Politeness towards the camera API

- At most one request per second (`RIPPLE_CAMERA_REQUEST_INTERVAL_S`).
- Retries on network errors, HTTP 429 and 5xx with exponential backoff (2 s, 4 s,
  8 s; `Retry-After` honored, capped at 60 s); other errors are not retried.
- 15 s timeout per request and an identifying `User-Agent`.
- The camera list is fetched at most weekly; camera tasks run only on demand.

## Interface for event detection

```python
from transit_ripple.cameras import count_near_station, summarize_station_counts

df = count_near_station("A41", event_start_utc)  # station id, timestamp
summary = summarize_station_counts(df)  # totals over cameras with valid counts
```

One row per camera within 500 m (nearest first) with `source` (`live`, `stored`
or `none`), `status`, `observed_utc`, `age_s` and the four counts. If the
timestamp is within 5 minutes of now (or omitted), the cameras are counted live
and the counts are stored; otherwise the stored count nearest to the timestamp
(within 5 minutes) is returned. Frames are never stored, so the past can only be
answered from counts made at that time.

`count_near_stations(station_ids, max_cameras=N)` does the same for several
stations at once: each camera is counted once, cameras are chosen round-robin over
the stations (nearest first), and cameras over the cap get `status = skipped_cap`
without a request. Since M4, `ripple events` uses it for every ongoing delay event
(most severe first) with a shared cap per run; see "Cameras during events" in
[data-model.md](data-model.md#cameras-during-events-m4).

## Commands

```bash
uv sync --extra vision
```

| Command | Purpose |
|---|---|
| `ripple-cameras inventory [--force]` | Refresh the camera list (weekly unless forced) |
| `ripple-cameras map` | Map cameras to subway stations within 500 m |
| `ripple-cameras count [--station A41 ...] [--max-cameras 60] [--model yolox_s]` | Count and store; prints the quality report |
| `ripple-cameras near A41 [--at ISO]` | `count_near_station` from the command line |
| `ripple-cameras benchmark [--frames 50] [--live 10] [--model ...]` | Inference speed and cameras-per-run estimate |
| `ripple-cameras sample-frames --n 50 [--out data/labeling]` | Save frames + `labels.csv` locally for manual labeling |
| `ripple-cameras evaluate --labels data/labeling/labels.csv [--models yolox_s,yolox_tiny]` | MAE per class vs. the labels |

(`python -m transit_ripple.cameras <command>` is equivalent.)

## Accuracy evaluation

1. `ripple-cameras sample-frames --n 50` picks 50 online cameras, stratified by
   borough, saves their current frames to `data/labeling/frames/` (gitignored) and
   writes `data/labeling/labels.csv` with empty `car`, `bus`, `truck`, `person`
   columns. Model counts are deliberately not in that file, to avoid anchoring.
2. A person fills in the true counts (rows left empty are skipped).
3. `ripple-cameras evaluate --labels data/labeling/labels.csv --models yolox_s,yolox_tiny,yolox_nano`
   writes `evaluation.json` and `predictions.csv` next to the labels and prints,
   per model and class: MAE, bias (mean of predicted - true; negative means the
   model under-counts), RMSE, the share of frames counted exactly, and totals.
4. Delete the frames afterwards.

## Performance

Measured with `ripple-cameras benchmark` on a GitHub Actions `ubuntu-latest` runner
(4 CPUs, Python 3.12, ONNX Runtime 1.31.1), 100 synthetic 352x240 frames each;
end-to-end = JPEG decode + preprocessing + model + post-processing:

| Model | Input | End-to-end p50 | p95 | Model only p50 |
|---|---|---|---|---|
| YOLOX-S (default) | 640x640 | 64.4 ms | 65.4 ms | 61.3 ms |
| YOLOX-Tiny | 416x416 | 18.7 ms | 19.1 ms | 17.0 ms |
| YOLOX-Nano | 416x416 | 7.1 ms | 7.2 ms | 5.3 ms |

The camera workflow's `benchmark` job reproduces these numbers (it also runs on
pull requests that change this module).

Per camera, polite sequential counting takes about
`max(request interval, fetch + processing)`. With one request per second and
frames that download in well under a second, the rate limit, not the CPU, bounds
a run: about 240 cameras in a 4-minute run, i.e. every camera near the busiest
stations, or the cameras of all affected stations of a few events.
