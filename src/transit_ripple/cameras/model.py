"""Object detector models: registry, license metadata and cached download.

Weights are never committed. They are downloaded at runtime from the official
release, verified against a pinned SHA-256 and cached (``RIPPLE_MODEL_CACHE_DIR``,
default ``~/.cache/nyc-transit-ripple/models``).

YOLOX (Megvii) is used because both its code and its pretrained COCO weights are
Apache-2.0. Ultralytics YOLO (v5/v8/11) is AGPL-3.0 and is deliberately not used.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

YOLOX_RELEASE = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    url: str
    sha256: str
    input_size: tuple[int, int]  # (height, width) of the exported ONNX graph
    size_mb: float
    coco_ap: float  # COCO val2017 AP reported by the authors
    license: str = "Apache-2.0"
    source: str = "Megvii-BaseDetection/YOLOX release 0.1.1rc0"

    @property
    def filename(self) -> str:
        return f"{self.name}-{self.sha256[:12]}.onnx"


MODELS: dict[str, ModelSpec] = {
    spec.name: spec
    for spec in (
        ModelSpec(
            "yolox_s",
            f"{YOLOX_RELEASE}/yolox_s.onnx",
            "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
            (640, 640),
            35.9,
            40.5,
        ),
        ModelSpec(
            "yolox_tiny",
            f"{YOLOX_RELEASE}/yolox_tiny.onnx",
            "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
            (416, 416),
            20.2,
            32.8,
        ),
        ModelSpec(
            "yolox_nano",
            f"{YOLOX_RELEASE}/yolox_nano.onnx",
            "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
            (416, 416),
            3.7,
            25.8,
        ),
    )
}


class ModelError(RuntimeError):
    pass


def get_spec(name: str) -> ModelSpec:
    if name not in MODELS:
        raise ValueError(f"Unknown model {name!r}. Available: {', '.join(MODELS)}")
    return MODELS[name]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, target: Path) -> None:
    with requests.get(url, timeout=120, stream=True) as resp:
        resp.raise_for_status()
        with open(target, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)


def ensure_model(
    spec: ModelSpec,
    cache_dir: Path,
    download: Callable[[str, Path], None] = _download,
) -> Path:
    """Path to verified weights, downloading them into the cache if needed."""
    cache_dir = Path(cache_dir)
    path = cache_dir / spec.filename
    if path.exists() and _sha256(path) == spec.sha256:
        return path
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".download")
    log.info("Downloading %s (%.1f MB, %s)", spec.name, spec.size_mb, spec.license)
    try:
        download(spec.url, tmp)
        digest = _sha256(tmp)
        if digest != spec.sha256:
            raise ModelError(f"checksum mismatch for {spec.name}: {digest}")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path
