"""A polite HTTP client for the NYCTMC camera API.

- At most one request per ``request_interval_s`` (process-wide for this client).
- Retries on network errors, HTTP 429 and 5xx, with exponential backoff
  (``backoff_s``, then 2x, 4x, ... capped at ``max_backoff_s``) and respect for
  ``Retry-After``. Other HTTP errors are not retried.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from .config import CameraSettings

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class CameraApiError(RuntimeError):
    """A request failed after all retries (or with a non-retryable status)."""


def _retry_after_s(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form is not worth parsing here


class PoliteClient:
    def __init__(
        self,
        settings: CameraSettings,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", settings.user_agent)
        self.clock = clock
        self.sleep = sleep
        self._last_request: float | None = None
        self.n_requests = 0
        self.n_retries = 0
        # Network time of the last successful request, excluding politeness waits.
        self.last_elapsed_ms: float | None = None

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self.settings.request_interval_s - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()

    def get(self, url: str) -> requests.Response:
        s = self.settings
        error = "unknown error"
        for attempt in range(s.max_retries + 1):
            self._wait_turn()
            self.n_requests += 1
            retry_after = None
            start = time.perf_counter()
            try:
                resp = self.session.get(url, timeout=s.timeout_s)
            except requests.RequestException as exc:
                error = type(exc).__name__
            else:
                if resp.status_code == 200:
                    self.last_elapsed_ms = round((time.perf_counter() - start) * 1000.0, 1)
                    return resp
                error = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRY_STATUS:
                    raise CameraApiError(error)
                retry_after = _retry_after_s(resp)
            if attempt == s.max_retries:
                break
            delay = retry_after if retry_after is not None else s.backoff_s * (2**attempt)
            delay = min(delay, s.max_backoff_s)
            log.info("Camera API %s; retrying in %.1fs", error, delay)
            self.n_retries += 1
            self.sleep(delay)
        raise CameraApiError(f"{error} after {s.max_retries + 1} attempts")

    def get_json(self, url: str):
        resp = self.get(url)
        try:
            return resp.json()
        except ValueError as exc:
            raise CameraApiError(f"response is not JSON: {exc}") from exc

    def get_bytes(self, url: str) -> bytes:
        return self.get(url).content
