"""Inference speed benchmark and "how many cameras per run" estimate.

Synthetic frames (same size as NYCTMC frames, 352x240) measure the CPU cost of
decoding, preprocessing and running the detector without calling the camera API.
Optionally, a few live frames measure the network cost too.

Per camera, sequential polite counting takes about
``max(request_interval_s, fetch_s + processing_s)``, so a run with a time budget
of ``budget_s`` can count ``floor(budget_s / that)`` cameras.
"""

from __future__ import annotations

import io
import math
import os
import platform
import time

import numpy as np

FRAME_SIZE = (240, 352)  # height, width of NYCTMC camera frames


def synthetic_frames(n: int, size: tuple[int, int] = FRAME_SIZE, seed: int = 0) -> list[bytes]:
    """JPEG frames with a road-like gradient and random boxes (no real imagery)."""
    from PIL import Image, ImageDraw

    rng = np.random.default_rng(seed)
    frames = []
    h, w = size
    for _ in range(n):
        base = np.linspace(40, 160, h, dtype=np.uint8)[:, None, None].repeat(w, 1).repeat(3, 2)
        noise = rng.integers(0, 25, (h, w, 3), dtype=np.uint8)
        im = Image.fromarray(base + noise)
        draw = ImageDraw.Draw(im)
        for _ in range(int(rng.integers(3, 12))):
            x, y = int(rng.integers(0, w - 40)), int(rng.integers(h // 3, h - 20))
            color = tuple(int(c) for c in rng.integers(0, 255, 3))
            draw.rectangle(
                [x, y, x + int(rng.integers(15, 40)), y + int(rng.integers(10, 20))], fill=color
            )
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        frames.append(buf.getvalue())
    return frames


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    arr = np.asarray(values, dtype=float)
    return {
        "n": int(arr.size),
        "mean": round(float(arr.mean()), 1),
        "p50": round(float(np.percentile(arr, 50)), 1),
        "p95": round(float(np.percentile(arr, 95)), 1),
        "max": round(float(arr.max()), 1),
    }


def time_frames(detector, frames: list[bytes], warmup: int = 2) -> dict:
    """End-to-end (decode + preprocess + model + postprocess) and model-only times in ms."""
    for data in frames[:warmup]:
        detector.count(data)
    total, model = [], []
    for data in frames:
        start = time.perf_counter()
        result = detector.count(data)
        total.append((time.perf_counter() - start) * 1000.0)
        model.append(result.inference_ms)
    return {"end_to_end_ms": _stats(total), "model_ms": _stats(model)}


def cameras_per_run(
    budget_s: float, request_interval_s: float, fetch_ms: float, processing_ms: float
) -> int:
    per_camera_s = max(request_interval_s, (fetch_ms + processing_ms) / 1000.0)
    return int(math.floor(budget_s / per_camera_s)) if per_camera_s > 0 else 0


def environment() -> dict:
    try:
        import onnxruntime as ort

        ort_version = ort.__version__
    except ImportError:  # pragma: no cover - only without the vision extra
        ort_version = None
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "processor": platform.processor() or platform.machine(),
        "onnxruntime": ort_version,
        "github_runner": os.environ.get("RUNNER_NAME"),
        "runner_os": os.environ.get("RUNNER_OS"),
    }


def run_benchmark(
    detector,
    settings,
    n_frames: int = 50,
    budget_s: float = 240.0,
    live_frames: list[bytes] | None = None,
    live_fetch_ms: list[float] | None = None,
) -> dict:
    synthetic = time_frames(detector, synthetic_frames(n_frames))
    result = {
        "model": detector.model_name,
        "input_size": list(detector.input_size),
        "environment": environment(),
        "synthetic": synthetic,
        "request_interval_s": settings.request_interval_s,
        "budget_s": budget_s,
    }
    processing = synthetic["end_to_end_ms"]["p50"]
    fetch_ms = None
    if live_frames:
        result["live"] = time_frames(detector, live_frames, warmup=0)
        result["live_fetch_ms"] = _stats(live_fetch_ms or [])
        fetch_ms = result["live_fetch_ms"].get("p50")
    assumed_fetch = fetch_ms if fetch_ms is not None else 500.0
    result["assumed_fetch_ms"] = assumed_fetch
    result["cameras_per_run_polite"] = cameras_per_run(
        budget_s, settings.request_interval_s, assumed_fetch, processing
    )
    result["cameras_per_run_cpu_only"] = int(budget_s * 1000.0 // processing) if processing else 0
    return result


def benchmark_markdown(r: dict) -> str:
    env = r["environment"]
    syn = r["synthetic"]
    lines = [
        f"## Camera detector benchmark: `{r['model']}` "
        f"(input {r['input_size'][0]}x{r['input_size'][1]})",
        "",
        f"- Machine: {env['platform']}, {env['cpu_count']} CPUs, Python {env['python']}, "
        f"ONNX Runtime {env['onnxruntime']}",
        f"- Synthetic 352x240 frames: end-to-end p50 **{syn['end_to_end_ms']['p50']} ms** "
        f"(p95 {syn['end_to_end_ms']['p95']} ms); model only p50 {syn['model_ms']['p50']} ms "
        f"(n = {syn['end_to_end_ms']['n']})",
    ]
    if "live" in r:
        lines.append(
            f"- Live frames: fetch p50 {r['live_fetch_ms'].get('p50')} ms, end-to-end p50 "
            f"{r['live']['end_to_end_ms']['p50']} ms (n = {r['live']['end_to_end_ms']['n']})"
        )
    lines += [
        f"- Cameras per {r['budget_s']:.0f} s run at one request per {r['request_interval_s']:g} s "
        f"(fetch assumed {r['assumed_fetch_ms']} ms): **{r['cameras_per_run_polite']}**",
        f"- CPU-only bound (no network, no rate limit): {r['cameras_per_run_cpu_only']} frames",
    ]
    return "\n".join(lines) + "\n"
