"""In-memory ring buffer for 24h history per instance + tracker items."""

import time
import threading
from collections import deque

SAMPLE_INTERVAL = 30
MAX_SAMPLES = 2880
TRACKER_SAMPLE_INTERVAL = 60

_lock = threading.Lock()

# Per-instance bytes history
_history = {}
_last_sample = {}

# Global tracker items history (single timeline)
_tracker_history = deque(maxlen=MAX_SAMPLES)
_tracker_last_sample = 0


def record(instance_name, total_bytes):
    """Record a bytes sample for an instance."""
    now = time.time()
    mono = time.monotonic()

    with _lock:
        last = _last_sample.get(instance_name, 0)
        if mono - last < SAMPLE_INTERVAL:
            return

        if instance_name not in _history:
            _history[instance_name] = deque(maxlen=MAX_SAMPLES)

        _history[instance_name].append((now, total_bytes))
        _last_sample[instance_name] = mono


def record_tracker(items_done):
    """Record a tracker items snapshot (called ~every 60s from broadcast loop)."""
    global _tracker_last_sample
    now = time.time()
    mono = time.monotonic()

    with _lock:
        if mono - _tracker_last_sample < TRACKER_SAMPLE_INTERVAL:
            return
        _tracker_history.append((now, items_done))
        _tracker_last_sample = mono


def get_bucketed():
    """Return deltas bucketed by auto-selected interval."""
    with _lock:
        now = time.time()
        cutoff = now - 86400

        # Find earliest sample
        earliest = now
        for samples in _history.values():
            for s in samples:
                if s[0] >= cutoff and s[0] < earliest:
                    earliest = s[0]
                    break
        for s in _tracker_history:
            if s[0] >= cutoff and s[0] < earliest:
                earliest = s[0]
                break

        # Auto-select interval
        span_hours = (now - earliest) / 3600
        if span_hours <= 1:
            interval_minutes = 5
        elif span_hours <= 4:
            interval_minutes = 15
        elif span_hours <= 12:
            interval_minutes = 30
        else:
            interval_minutes = 60

        interval_secs = interval_minutes * 60

        # Pre-fill buckets
        buckets = {}
        bucket_start = int(cutoff // interval_secs) * interval_secs
        t = bucket_start
        while t <= now:
            buckets[t] = [0, 0]  # [data_bytes_delta, items_delta]
            t += interval_secs

        # Per-instance byte deltas
        for name, samples in _history.items():
            prev = None
            for sample in samples:
                ts, total_bytes = sample
                if ts < cutoff:
                    prev = sample
                    continue
                if prev is not None:
                    delta_bytes = max(0, total_bytes - prev[1])
                    bucket_key = int(ts // interval_secs) * interval_secs
                    if bucket_key in buckets:
                        buckets[bucket_key][0] += delta_bytes
                prev = sample

        # Tracker items deltas
        prev = None
        for sample in _tracker_history:
            ts, items_done = sample
            if ts < cutoff:
                prev = sample
                continue
            if prev is not None:
                delta_items = max(0, items_done - prev[1])
                bucket_key = int(ts // interval_secs) * interval_secs
                if bucket_key in buckets:
                    buckets[bucket_key][1] += delta_items
            prev = sample

        # Convert to sorted list
        result = []
        for ts in sorted(buckets.keys()):
            result.append({
                "t": ts,
                "bytes": buckets[ts][0],
                "items": buckets[ts][1],
            })

        return {
            "interval_minutes": interval_minutes,
            "buckets": result,
        }


def remove(instance_name):
    """Remove history for a deleted instance."""
    with _lock:
        _history.pop(instance_name, None)
        _last_sample.pop(instance_name, None)