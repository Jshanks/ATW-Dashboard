"""In-memory ring buffer for 24h bandwidth history per instance."""

import time
import threading
from collections import deque

# 30-second sample interval, 24h = 2880 samples per instance
SAMPLE_INTERVAL = 30
MAX_SAMPLES = 2880

_lock = threading.Lock()
_history: dict[str, deque] = {}  # instance_name -> deque of (timestamp, bw_down, bw_up)
_last_sample: dict[str, float] = {}  # instance_name -> last sample monotonic time


def record(instance_name: str, bw_down: float, bw_up: float):
    """Record a bandwidth sample if enough time has passed since the last one."""
    now = time.time()
    mono = time.monotonic()

    with _lock:
        last = _last_sample.get(instance_name, 0)
        if mono - last < SAMPLE_INTERVAL:
            return

        if instance_name not in _history:
            _history[instance_name] = deque(maxlen=MAX_SAMPLES)

        _history[instance_name].append((now, bw_down, bw_up))
        _last_sample[instance_name] = mono


def get_all() -> dict[str, list]:
    """Return all history as {instance_name: [[timestamp, bw_down, bw_up], ...]}."""
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