"""In-memory ring buffer for 24h history per instance."""

import time
import threading
from collections import deque

SAMPLE_INTERVAL = 30
MAX_SAMPLES = 2880

_lock = threading.Lock()
_history = {}
_last_sample = {}


def record(instance_name, total_bytes, completed_items):
    """Record a data/items sample if enough time has passed."""
    now = time.time()
    mono = time.monotonic()

    with _lock:
        last = _last_sample.get(instance_name, 0)
        if mono - last < SAMPLE_INTERVAL:
            return

        if instance_name not in _history:
            _history[instance_name] = deque(maxlen=MAX_SAMPLES)

        _history[instance_name].append((now, total_bytes, completed_items))
        _last_sample[instance_name] = mono


def get_all():
    """Return raw history."""
    with _lock:
        result = {}
        for name, samples in _history.items():
            result[name] = list(samples)
        return result


def get_bucketed():
    """Return deltas aggregated across all instances, bucketed by auto-selected interval."""
    with _lock:
        now = time.time()
        cutoff = now - 86400

        # Find earliest sample within 24h window
        earliest = now
        for samples in _history.values():
            for s in samples:
                if s[0] >= cutoff and s[0] < earliest:
                    earliest = s[0]
                    break

        # Auto-select interval based on data span
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

        # Pre-fill buckets from cutoff to now
        buckets = {}
        bucket_start = int(cutoff // interval_secs) * interval_secs
        t = bucket_start
        while t <= now:
            buckets[t] = [0, 0]  # [data_bytes, items_done]
            t += interval_secs

        # For each instance, compute deltas and assign to buckets
        for name, samples in _history.items():
            prev = None
            for sample in samples:
                ts, total_bytes, completed = sample
                if ts < cutoff:
                    prev = sample
                    continue
                if prev is not None:
                    delta_bytes = max(0, total_bytes - prev[1])
                    delta_items = max(0, completed - prev[2])

                    bucket_key = int(ts // interval_secs) * interval_secs
                    if bucket_key in buckets:
                        buckets[bucket_key][0] += delta_bytes
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