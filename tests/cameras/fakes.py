"""Fakes for camera tests: no network, no model weights."""

from __future__ import annotations

import io

import numpy as np


def jpeg_bytes(width: int = 352, height: int = 240, seed: int = 0) -> bytes:
    from PIL import Image

    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 255, (height, width, 3), dtype=np.uint8)).save(buf, "JPEG")
    return buf.getvalue()


class FakeInput:
    def __init__(self, size):
        self.name = "images"
        self.shape = [1, 3, size, size]


class FakeSession:
    """ONNX-Runtime-like session returning crafted raw YOLOX outputs.

    ``objects`` is a list of (anchor_index, coco_class_id); each becomes one
    confident detection the size of its grid cell.
    """

    def __init__(self, size: int = 64, objects=(), score: float = 0.9):
        self.size = size
        self.objects = list(objects)
        self.score = score
        self.calls = 0
        n = sum((size // s) ** 2 for s in (8, 16, 32))
        self.n_anchors = n

    def get_inputs(self):
        return [FakeInput(self.size)]

    def run(self, _outputs, feeds):
        (tensor,) = feeds.values()
        assert tensor.shape == (1, 3, self.size, self.size) and tensor.dtype == np.float32
        self.calls += 1
        raw = np.zeros((1, self.n_anchors, 85), dtype=np.float32)
        for anchor, class_id in self.objects:
            raw[0, anchor, 4] = 1.0
            raw[0, anchor, 5 + class_id] = self.score
        return [raw]


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None, json_data=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeHttp:
    """requests.Session stand-in: returns scripted responses or raises errors."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    """Deterministic monotonic clock whose sleep advances time."""

    def __init__(self, t=1000.0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s
