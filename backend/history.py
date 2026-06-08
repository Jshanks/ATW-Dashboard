"""In-memory ring buffer for 24h history per instance + tracker items, with file persistence."""

import json
import logging
import os
import time
import threading
from collections import deque

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL = 30
MAX_SAMPLES = 2880
TRACKER_SAMPLE_INTERVAL = 60
SAVE_INTERVAL = 60

MAX_DELTA_GAP = 180

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

_lock = threading.Lock()

_history = {}
_last_sample = {}

_tracker_history = deque(maxlen=MAX_SAMPLES)
_tracker_last_sample = 0

_last_save = 0
_bucketed_cache = None
_bucketed_dirty = True


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
        global _bucketed_dirty
        _bucketed_dirty = True


def record_tracker(items_delta, bytes_delta=0):
    """Record tracker deltas (items and bytes) for the current poll cycle."""
    global _tracker_last_sample
    now = time.time()
    mono = time.monotonic()

    with _lock:
        if mono - _tracker_last_sample < TRACKER_SAMPLE_INTERVAL:
            return
        _tracker_history.append((now, items_delta, bytes_delta))
        _tracker_last_sample = mono
        global _bucketed_dirty
        _bucketed_dirty = True


def get_bucketed():
    """Return deltas bucketed by auto-selected interval."""
    global _bucketed_cache, _bucketed_dirty
    if not _bucketed_dirty and _bucketed_cache is not None:
        return _bucketed_cache
    with _lock:
        _bucketed_dirty = False
        now = time.time()
        cutoff = now - 86400

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

        buckets = {}
        bucket_start = int(cutoff // interval_secs) * interval_secs
        t = bucket_start
        while t <= now:
            buckets[t] = [0, 0, 0]  # [instance_bytes, tracker_items, tracker_bytes]
            t += interval_secs

        for name, samples in _history.items():
            prev = None
            for sample in samples:
                ts, total_bytes = sample
                if ts < cutoff:
                    prev = sample
                    continue
                if prev is not None:
                    gap = ts - prev[0]
                    if gap <= MAX_DELTA_GAP:
                        delta_bytes = max(0, total_bytes - prev[1])
                        bucket_key = int(ts // interval_secs) * interval_secs
                        if bucket_key in buckets:
                            buckets[bucket_key][0] += delta_bytes
                prev = sample

        for sample in _tracker_history:
            ts = sample[0]
            items_delta = sample[1]
            bytes_delta = sample[2] if len(sample) > 2 else 0
            if ts < cutoff:
                continue
            bucket_key = int(ts // interval_secs) * interval_secs
            if bucket_key in buckets:
                buckets[bucket_key][1] += max(0, items_delta)
                buckets[bucket_key][2] += max(0, bytes_delta)

        result = []
        for ts in sorted(buckets.keys()):
            result.append({
                "t": ts,
                "bytes": buckets[ts][0],
                "items": buckets[ts][1],
                "tracker_bytes": buckets[ts][2],
            })

        _bucketed_cache = {
            "interval_minutes": interval_minutes,
            "buckets": result,
        }
        return _bucketed_cache


def remove(instance_name):
    """Remove history for a deleted instance."""
    with _lock:
        global _bucketed_dirty
    with _lock:
        _history.pop(instance_name, None)
        _last_sample.pop(instance_name, None)
        _bucketed_dirty = True


def save():
    """Save history to disk, throttled to once per SAVE_INTERVAL."""
    global _last_save
    mono = time.monotonic()
    if mono - _last_save < SAVE_INTERVAL:
        return
    force_save()
    _last_save = mono


def force_save():
    """Save history to disk immediately."""
    with _lock:
        now = time.time()
        cutoff = now - 86400

        data = {
            "instances": {},
            "tracker": [],
        }

        for name, samples in _history.items():
            data["instances"][name] = [
                s for s in samples if s[0] >= cutoff
            ]

        data["tracker"] = [
            s for s in _tracker_history if s[0] >= cutoff
        ]

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, HISTORY_FILE)
        logger.debug("History saved (%d instances, %d tracker samples)",
                      len(data["instances"]), len(data["tracker"]))
    except Exception as e:
        logger.warning("Failed to save history: %s", e)


def load():
    """Load history from disk on startup."""
    global _tracker_last_sample

    if not os.path.exists(HISTORY_FILE):
        logger.info("No history file found, starting fresh")
        return

    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Failed to load history file: %s", e)
        return

    now = time.time()
    cutoff = now - 86400
    loaded_instances = 0
    loaded_samples = 0

    with _lock:
        instances = data.get("instances", {})
        for name, samples in instances.items():
            dq = deque(maxlen=MAX_SAMPLES)
            for s in samples:
                if isinstance(s, (list, tuple)) and len(s) >= 2 and s[0] >= cutoff:
                    dq.append(tuple(s))
                    loaded_samples += 1
            if dq:
                _history[name] = dq
                loaded_instances += 1

        tracker_samples = data.get("tracker", [])
        for s in tracker_samples:
            if isinstance(s, (list, tuple)) and len(s) >= 2 and s[0] >= cutoff:
                # Normalize old 2-element tuples to 3-element
                if len(s) == 2:
                    _tracker_history.append((s[0], s[1], 0))
                else:
                    _tracker_history.append(tuple(s))

    logger.info("History loaded: %d instances, %d samples, %d tracker points",
                loaded_instances, loaded_samples, len(_tracker_history))