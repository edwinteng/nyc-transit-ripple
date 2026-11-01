"""Traffic cameras (M3): inventory, station mapping and object counts.

Public entry points:

- ``count_near_station(station_id, timestamp)``: counts of cars, buses, trucks and
  people at the cameras within 500 m of a subway station (live or stored).
- ``python -m transit_ripple.cameras <command>``: inventory, map, count, near,
  benchmark, sample-frames, evaluate.

Only numbers are produced: frames are processed in memory and never stored (except
when explicitly sampled into a local, gitignored folder for manual labeling).
"""

from .interface import count_near_station, count_near_stations, summarize_station_counts

__all__ = ["count_near_station", "count_near_stations", "summarize_station_counts"]
