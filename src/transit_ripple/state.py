"""Small JSON state carried between runs (dedup and time-jump detection)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

STATE_VERSION = 1
# Per-trip delay memory is only useful for a few hours.
TRIP_MEMORY_S = 3 * 3600


@dataclass
class PipelineState:
    # feed_id -> last accepted header timestamp
    last_header_ts: dict[str, int] = field(default_factory=dict)
    # "feed|start_date|trip_id" -> [observed_ts, delay_min]
    trip_delays: dict[str, list[float]] = field(default_factory=dict)
    # Other sources (M2): free-form JSON state per source, e.g. last good fetch time.
    sources: dict[str, dict] = field(default_factory=dict)

    def source(self, name: str) -> dict:
        return self.sources.setdefault(name, {})

    @staticmethod
    def trip_key(feed_id: str, start_date: str | None, trip_id: str) -> str:
        return f"{feed_id}|{start_date or ''}|{trip_id}"

    @classmethod
    def load(cls, path: Path) -> PipelineState:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must not stop ingestion; start fresh.
            return cls()
        if raw.get("version") != STATE_VERSION:
            return cls()
        return cls(
            last_header_ts={k: int(v) for k, v in raw.get("last_header_ts", {}).items()},
            trip_delays={k: list(v) for k, v in raw.get("trip_delays", {}).items()},
            # Absent in M1 state files; starts empty.
            sources={k: dict(v) for k, v in raw.get("sources", {}).items()},
        )

    def prune(self, now_ts: float) -> None:
        cutoff = now_ts - TRIP_MEMORY_S
        self.trip_delays = {k: v for k, v in self.trip_delays.items() if v[0] >= cutoff}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "last_header_ts": dict(sorted(self.last_header_ts.items())),
            "trip_delays": dict(sorted(self.trip_delays.items())),
            "sources": dict(sorted(self.sources.items())),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, path)
