"""In-memory ring buffer for 24h history per instance."""

import time
import threading
from collections import deque

SAMPLE_INTERVAL = 30
MAX_SAMPLES = 2880

_lock = threading.Lock()
_history: dict[str, deque] = {}  # name -> deque of (timestamp, total_bytes, completed_items)
_last_sample: dict[str, float] = {}


def record(instance_name: str, total_bytes: int, completed_items: int):
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


def get_all() -> dict[str, list]:
    """Return all history as {instance_name: [[timestamp, total_bytes, completed_items], ...]}."""
    with _lock:
        result = {}
        for name, samples in _history.items():
            result[name] = list(samples)
        return result


def remove(instance_name: str):
    """Remove history for a deleted instance."""
    with _lock:
        _history.pop(instance_name, None)
        _last_sample.pop(instance_name, None)