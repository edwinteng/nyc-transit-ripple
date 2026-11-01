"""HTTP fetching of realtime feeds (and replay of saved payloads)."""

from __future__ import annotations

import gzip
import logging
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import FeedSpec, Settings

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    feed_id: str
    url: str
    fetched_at: float  # epoch seconds, wall clock when the response arrived
    content: bytes | None
    http_status: int | None = None
    error: str | None = None
    elapsed_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.content is not None and self.error is None


Fetcher = Callable[[list[FeedSpec]], list[FetchResult]]


def make_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = settings.user_agent
    if settings.mta_api_key:
        # Not required for subway feeds; supported for forward compatibility.
        session.headers["x-api-key"] = settings.mta_api_key
    return session


def fetch_one(spec: FeedSpec, session: requests.Session, timeout: float) -> FetchResult:
    start = time.time()
    try:
        resp = session.get(spec.url, timeout=timeout)
        now = time.time()
        if resp.status_code != 200:
            return FetchResult(
                spec.feed_id,
                spec.url,
                now,
                None,
                http_status=resp.status_code,
                error=f"HTTP {resp.status_code}",
                elapsed_s=now - start,
            )
        return FetchResult(
            spec.feed_id, spec.url, now, resp.content, http_status=200, elapsed_s=now - start
        )
    except requests.RequestException as exc:
        now = time.time()
        # Keep the message short and free of request headers.
        return FetchResult(
            spec.feed_id,
            spec.url,
            now,
            None,
            error=f"{type(exc).__name__}",
            elapsed_s=now - start,
        )


class HttpFetcher:
    """Fetch all feeds concurrently so snapshots are close together in time."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = make_session(settings)

    def __call__(self, specs: list[FeedSpec]) -> list[FetchResult]:
        with ThreadPoolExecutor(max_workers=max(1, len(specs))) as pool:
            futures = [
                pool.submit(fetch_one, spec, self.session, self.settings.http_timeout_s)
                for spec in specs
            ]
            return [f.result() for f in futures]


RAW_NAME_RE = re.compile(r"^(?P<feed>[a-z0-9]+)_(?P<ts>\d{9,11})\.pb(\.gz)?$")


def raw_file_name(feed_id: str, header_ts: int) -> str:
    return f"{feed_id}_{header_ts}.pb.gz"


def save_raw(raw_dir: Path, result: FetchResult, header_ts: int) -> Path:
    """Store a raw payload (gzip) for short-term debugging and replay."""
    from .storage import date_partition  # local import avoids a cycle

    target = raw_dir / date_partition(header_ts) / raw_file_name(result.feed_id, header_ts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() and result.content is not None:
        target.write_bytes(gzip.compress(result.content))
    return target


def load_replay_batches(paths: Iterable[Path]) -> list[list[FetchResult]]:
    """Group saved payloads into poll batches ordered by header timestamp.

    Files must be named ``<feed_id>_<header_ts>.pb[.gz]``. Each batch holds at most
    one payload per feed; ``fetched_at`` is set to the header timestamp so replayed
    data is judged fresh.
    """
    by_feed: dict[str, list[tuple[int, Path]]] = {}
    for p in paths:
        m = RAW_NAME_RE.match(p.name)
        if not m:
            log.warning("Skipping file with unexpected name: %s", p)
            continue
        by_feed.setdefault(m.group("feed"), []).append((int(m.group("ts")), p))
    for items in by_feed.values():
        items.sort()
    n_batches = max((len(v) for v in by_feed.values()), default=0)
    batches: list[list[FetchResult]] = []
    for i in range(n_batches):
        batch = []
        for feed_id, items in sorted(by_feed.items()):
            if i >= len(items):
                continue
            ts, path = items[i]
            data = path.read_bytes()
            if path.suffix == ".gz":
                data = gzip.decompress(data)
            batch.append(
                FetchResult(feed_id, str(path), float(ts), data, http_status=200, elapsed_s=0.0)
            )
        batches.append(batch)
    return batches
