"""Count cars, buses, trucks and people in a camera frame with a YOLOX ONNX model.

Privacy by construction: frames are decoded in memory, passed through the
detector and discarded; only per-class counts leave this module. The detector
outputs generic COCO object classes and boxes, there is no face or license plate
recognition, and boxes are used only to suppress duplicate detections.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass

import numpy as np

# COCO class ids of the counted categories.
COCO_CLASSES = {2: "car", 5: "bus", 7: "truck", 0: "person"}
COUNT_NAMES = ("car", "bus", "truck", "person")
STRIDES = (8, 16, 32)
PAD_VALUE = 114  # YOLOX letterbox padding


class InvalidImage(ValueError):
    """The payload could not be decoded as an image."""


@dataclass
class FrameCounts:
    counts: dict[str, int]
    width: int
    height: int
    inference_ms: float


def decode_image(data: bytes) -> np.ndarray:
    """JPEG/PNG bytes -> RGB uint8 array (in memory only)."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as im:
            return np.asarray(im.convert("RGB"))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImage(f"cannot decode image: {exc}") from exc


def letterbox(rgb: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float]:
    """YOLOX preprocessing: keep aspect ratio, pad bottom/right with 114, BGR, NCHW float32."""
    from PIL import Image

    h, w = size
    ratio = min(h / rgb.shape[0], w / rgb.shape[1])
    nh, nw = int(rgb.shape[0] * ratio), int(rgb.shape[1] * ratio)
    resized = np.asarray(Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR))
    canvas = np.full((h, w, 3), PAD_VALUE, dtype=np.uint8)
    canvas[:nh, :nw] = resized[:, :, ::-1]  # RGB -> BGR, as in YOLOX training
    return np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32), ratio


def decode_outputs(raw: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Turn raw YOLOX head outputs (1, N, 85) into boxes in input-pixel units."""
    grids, strides = [], []
    for stride in STRIDES:
        hs, ws = size[0] // stride, size[1] // stride
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
        grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
        grids.append(grid)
        strides.append(np.full((*grid.shape[:2], 1), stride))
    grid = np.concatenate(grids, 1)
    stride = np.concatenate(strides, 1)
    if raw.shape[1] != grid.shape[1]:
        raise ValueError(f"unexpected output shape {raw.shape} for input size {size}")
    out = raw.astype(np.float32).copy()
    out[..., :2] = (out[..., :2] + grid) * stride
    out[..., 2:4] = np.exp(out[..., 2:4]) * stride
    return out[0]


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy non-maximum suppression; boxes are (x1, y1, x2, y2)."""
    order = scores.argsort()[::-1]
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    keep = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        iw = np.maximum(0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        ih = np.maximum(0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = iw * ih
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


def count_detections(
    pred: np.ndarray, score_threshold: float, nms_threshold: float
) -> dict[str, int]:
    """Per-class counts after score filtering and class-aware NMS."""
    scores = pred[:, 4:5] * pred[:, 5:]
    cls = scores.argmax(1)
    conf = scores.max(1)
    counts = dict.fromkeys(COUNT_NAMES, 0)
    for class_id, name in COCO_CLASSES.items():
        m = (cls == class_id) & (conf >= score_threshold)
        if not m.any():
            continue
        b = pred[m, :4]
        xyxy = np.stack(
            [
                b[:, 0] - b[:, 2] / 2,
                b[:, 1] - b[:, 3] / 2,
                b[:, 0] + b[:, 2] / 2,
                b[:, 1] + b[:, 3] / 2,
            ],
            axis=1,
        )
        counts[name] = len(nms(xyxy, conf[m], nms_threshold))
    return counts


class Detector:
    """Wraps an ONNX Runtime session (or any object with the same interface)."""

    def __init__(
        self,
        session,
        score_threshold: float = 0.35,
        nms_threshold: float = 0.45,
        model_name: str = "custom",
    ):
        self.session = session
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.model_name = model_name
        inp = session.get_inputs()[0]
        self.input_name = inp.name
        self.input_size = (int(inp.shape[2]), int(inp.shape[3]))

    def count(self, data: bytes) -> FrameCounts:
        """Counts for one frame. The image exists only inside this call."""
        rgb = decode_image(data)
        height, width = rgb.shape[:2]
        tensor, _ = letterbox(rgb, self.input_size)
        del rgb
        start = time.perf_counter()
        raw = self.session.run(None, {self.input_name: tensor})[0]
        pred = decode_outputs(raw, self.input_size)
        counts = count_detections(pred, self.score_threshold, self.nms_threshold)
        elapsed = (time.perf_counter() - start) * 1000.0
        return FrameCounts(counts, width, height, round(elapsed, 2))


def load_detector(settings) -> Detector:
    """Download (if needed) and load the configured model with ONNX Runtime on CPU."""
    import onnxruntime as ort

    from .model import ensure_model, get_spec

    spec = get_spec(settings.model)
    path = ensure_model(spec, settings.resolved_model_cache_dir)
    opts = ort.SessionOptions()
    if settings.threads:
        opts.intra_op_num_threads = settings.threads
    session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    return Detector(session, settings.score_threshold, settings.nms_threshold, spec.name)
