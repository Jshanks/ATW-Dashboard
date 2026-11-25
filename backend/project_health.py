"""Persistent Project Health monitoring, baseline management, and failover state.

The current implementation separates durable baseline establishment from rolling
sample quality, includes tracker queue evidence, evaluates automatic failover on
new observations, and preserves the existing policy, role, reset, cooldown, and
auditable decision-log contracts. Network project switches remain in main.py.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get(
    "DATA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"),
)
STATE_FILE = os.path.join(DATA_DIR, "project_health.json")

HISTORY_SECONDS = 48 * 60 * 60
DEFAULT_MINIMUM_BASELINE_HOURS = 6
DEFAULT_FULL_BASELINE_HOURS = 24
MINIMUM_ALLOWED_BASELINE_HOURS = 1
MAXIMUM_ALLOWED_BASELINE_HOURS = 48
RECENT_WINDOW_SECONDS = 60 * 60
SAMPLE_INTERVAL_SECONDS = 60
MINIMUM_BASELINE_COVERAGE = 0.75
MAX_HISTORY_SAMPLES_PER_PROJECT = HISTORY_SECONDS // SAMPLE_INTERVAL_SECONDS + 10
MAX_DECISION_LOG_ENTRIES = 500
DEFAULT_STALLED_EVALUATIONS = 3
MINIMUM_STALLED_EVALUATIONS = 2
MAXIMUM_STALLED_EVALUATIONS = 10
DEFAULT_COOLDOWN_HOURS = 6
MINIMUM_COOLDOWN_HOURS = 1
MAXIMUM_COOLDOWN_HOURS = 48
DEFAULT_RESUME_STABILIZATION_MINUTES = 30
MINIMUM_RESUME_STABILIZATION_MINUTES = 15
MAXIMUM_RESUME_STABILIZATION_MINUTES = 120

DEFAULT_CANARY_PERCENT = 5
MINIMUM_CANARY_COUNT = 3
MAXIMUM_CANARY_COUNT = 10

# Low-volume projects should not be declared stalled from ratios built on noise.
MINIMUM_BASELINE_ITEMS_PER_HOUR = 1.0
MINIMUM_BASELINE_BYTES_PER_HOUR = 1024 * 1024

# Ratios compare the latest complete hour with the rolling configured baseline,
# excluding that recent hour so current activity does not grade itself.
HEALTHY_RATIO = 0.75
REDUCED_RATIO = 0.35
STALLED_RATIO = 0.25
RECOVERY_RATIO = 0.35
FAILBACK_RECOVERY_RATIO = 0.50
FAILBACK_MINIMUM_RUNWAY_MINUTES = 60.0
MAXIMUM_CURRENT_SAMPLE_AGE_SECONDS = 10 * 60
LOW_QUEUE_RUNWAY_MINUTES = 30.0

_lock = threading.RLock()
_project_history: dict[str, deque] = {}
_baseline_established: dict[str, dict[str, float | None]] = {}
_project_queue: dict[str, dict[str, Any]] = {}
_policy: dict[str, Any] = {}
_role_assignments: dict[str, str] = {}
_runtime: dict[str, Any] = {}
_decision_log: deque = deque(maxlen=MAX_DECISION_LOG_ENTRIES)
_last_save_monotonic = 0.0
SAVE_INTERVAL_SECONDS = 60


def _default_policy() -> dict[str, Any]:
    return {
        "primary_project": "",
        "fallback_project": "",
        "canary_percent": DEFAULT_CANARY_PERCENT,
        "minimum_baseline_hours": DEFAULT_MINIMUM_BASELINE_HOURS,
        "full_baseline_hours": DEFAULT_FULL_BASELINE_HOURS,
        "monitoring_enabled": True,
        "automatic_failover_enabled": False,
        "stalled_evaluations_required": DEFAULT_STALLED_EVALUATIONS,
        "cooldown_hours": DEFAULT_COOLDOWN_HOURS,
        "resume_stabilization_minutes": DEFAULT_RESUME_STABILIZATION_MINUTES,
        "updated_at": 0.0,
    }



def _default_runtime() -> dict[str, Any]:
    return {
        "monitoring_paused_at": None,
        "monitoring_resumed_at": None,
        "stabilization_until": None,
        "consecutive_stalled_evaluations": 0,
        "consecutive_recovered_evaluations": 0,
        "last_recovery_evaluated_sample_at": None,
        "cooldown_until": None,
        "automatic_failover_state": "disabled",
        "failover_in_progress": False,
        "last_automatic_failover_at": None,
        "last_automatic_failback_at": None,
        "last_automatic_transition": None,
        "fleet_mode": "primary",
        "last_automatic_result": None,
        "last_evaluated_sample_at": None,
        "last_evaluation_at": None,
        "last_evaluation_reason": None,
    }
def _normalize_project_slug(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:200]


def _normalize_policy(raw: Any) -> dict[str, Any]:
    policy = _default_policy()
    if not isinstance(raw, dict):
        return policy
    policy["primary_project"] = _normalize_project_slug(raw.get("primary_project"))
    policy["fallback_project"] = _normalize_project_slug(raw.get("fallback_project"))
    try:
        canary_percent = int(raw.get("canary_percent", DEFAULT_CANARY_PERCENT))
    except (TypeError, ValueError):
        canary_percent = DEFAULT_CANARY_PERCENT
    policy["canary_percent"] = max(1, min(100, canary_percent))
    try:
        minimum_hours = int(raw.get("minimum_baseline_hours", DEFAULT_MINIMUM_BASELINE_HOURS))
    except (TypeError, ValueError):
        minimum_hours = DEFAULT_MINIMUM_BASELINE_HOURS
    try:
        full_hours = int(raw.get("full_baseline_hours", DEFAULT_FULL_BASELINE_HOURS))
    except (TypeError, ValueError):
        full_hours = DEFAULT_FULL_BASELINE_HOURS
    minimum_hours = max(MINIMUM_ALLOWED_BASELINE_HOURS, min(MAXIMUM_ALLOWED_BASELINE_HOURS, minimum_hours))
    full_hours = max(minimum_hours, min(MAXIMUM_ALLOWED_BASELINE_HOURS, full_hours))
    policy["minimum_baseline_hours"] = minimum_hours
    policy["full_baseline_hours"] = full_hours
    policy["monitoring_enabled"] = bool(raw.get("monitoring_enabled", True))
    policy["automatic_failover_enabled"] = bool(raw.get("automatic_failover_enabled", False))
    try:
        stalled_required = int(raw.get("stalled_evaluations_required", DEFAULT_STALLED_EVALUATIONS))
    except (TypeError, ValueError):
        stalled_required = DEFAULT_STALLED_EVALUATIONS
    policy["stalled_evaluations_required"] = max(MINIMUM_STALLED_EVALUATIONS, min(MAXIMUM_STALLED_EVALUATIONS, stalled_required))
    try:
        cooldown_hours = int(raw.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS))
    except (TypeError, ValueError):
        cooldown_hours = DEFAULT_COOLDOWN_HOURS
    policy["cooldown_hours"] = max(MINIMUM_COOLDOWN_HOURS, min(MAXIMUM_COOLDOWN_HOURS, cooldown_hours))
    try:
        stabilization_minutes = int(raw.get("resume_stabilization_minutes", DEFAULT_RESUME_STABILIZATION_MINUTES))
    except (TypeError, ValueError):
        stabilization_minutes = DEFAULT_RESUME_STABILIZATION_MINUTES
    policy["resume_stabilization_minutes"] = max(MINIMUM_RESUME_STABILIZATION_MINUTES, min(MAXIMUM_RESUME_STABILIZATION_MINUTES, stabilization_minutes))
    try:
        policy["updated_at"] = float(raw.get("updated_at", 0.0))
    except (TypeError, ValueError):
        policy["updated_at"] = 0.0
    return policy


def _atomic_write(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file_handle:
        json.dump(data, file_handle, indent=2, sort_keys=True)
    os.replace(temporary_path, path)


def _serialize_locked() -> dict[str, Any]:
    cutoff = time.time() - HISTORY_SECONDS
    history_data = {}
    for project_slug, samples in _project_history.items():
        history_data[project_slug] = [
            list(sample) for sample in samples if sample[0] >= cutoff
        ]
    return {
        "version": 4,
        "history": history_data,
        "baseline_established": copy.deepcopy(_baseline_established),
        "project_queue": copy.deepcopy(_project_queue),
        "policy": copy.deepcopy(_policy),
        "role_assignments": copy.deepcopy(_role_assignments),
        "baseline_mode": "per_eligible_warrior_rolling_excluding_recent_hour",
        "runtime": copy.deepcopy(_runtime),
        "decision_log": list(_decision_log),
    }


def force_save() -> None:
    """Persist all project-health state immediately."""
    with _lock:
        state = _serialize_locked()
    try:
        _atomic_write(STATE_FILE, state)
    except Exception as error:
        logger.warning("Failed to save project health state: %s", error)


def save() -> None:
    """Persist state, throttled to once per minute."""
    global _last_save_monotonic
    now_monotonic = time.monotonic()
    with _lock:
        if now_monotonic - _last_save_monotonic < SAVE_INTERVAL_SECONDS:
            return
        _last_save_monotonic = now_monotonic
    force_save()


def load() -> None:
    """Load project-health state and discard tracker samples older than 48 hours."""
    global _policy, _role_assignments, _runtime, _decision_log, _baseline_established, _project_queue
    if not os.path.exists(STATE_FILE):
        with _lock:
            _policy = _default_policy()
        logger.info("No project health state found, starting fresh")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file_handle:
            raw_state = json.load(file_handle)
    except Exception as error:
        logger.warning("Failed to load project health state: %s", error)
        return

    cutoff = time.time() - HISTORY_SECONDS
    loaded_samples = 0
    with _lock:
        _project_history.clear()
        raw_history = raw_state.get("history", {})
        if isinstance(raw_history, dict):
            for project_slug, raw_samples in raw_history.items():
                normalized_slug = _normalize_project_slug(project_slug)
                if not normalized_slug or not isinstance(raw_samples, list):
                    continue
                samples = deque(maxlen=MAX_HISTORY_SAMPLES_PER_PROJECT)
                for raw_sample in raw_samples:
                    if not isinstance(raw_sample, (list, tuple)) or len(raw_sample) < 3:
                        continue
                    if len(raw_sample) < 4:
                        continue
                    try:
                        timestamp = float(raw_sample[0])
                        items_delta = max(0, int(raw_sample[1]))
                        bytes_delta = max(0, int(raw_sample[2]))
                        eligible_warriors = max(0, int(raw_sample[3]))
                    except (TypeError, ValueError):
                        continue
                    if timestamp >= cutoff and eligible_warriors > 0:
                        samples.append((timestamp, items_delta, bytes_delta, eligible_warriors))
                        loaded_samples += 1
                if samples:
                    _project_history[normalized_slug] = samples
        _policy = _normalize_policy(raw_state.get("policy"))
        raw_established = raw_state.get("baseline_established", {})
        _baseline_established = {}
        if isinstance(raw_established, dict):
            for project_slug, raw_value in raw_established.items():
                normalized_slug = _normalize_project_slug(project_slug)
                if not normalized_slug or not isinstance(raw_value, dict):
                    continue
                minimum_at = raw_value.get("minimum_established_at")
                full_at = raw_value.get("full_established_at")
                _baseline_established[normalized_slug] = {
                    "minimum_established_at": float(minimum_at) if isinstance(minimum_at, (int, float)) and not isinstance(minimum_at, bool) else None,
                    "full_established_at": float(full_at) if isinstance(full_at, (int, float)) and not isinstance(full_at, bool) else None,
                }
        raw_queue = raw_state.get("project_queue", {})
        _project_queue = {}
        if isinstance(raw_queue, dict):
            for project_slug, raw_value in raw_queue.items():
                normalized_slug = _normalize_project_slug(project_slug)
                if not normalized_slug or not isinstance(raw_value, dict):
                    continue
                todo_items = raw_value.get("todo_items")
                updated_at = raw_value.get("updated_at")
                _project_queue[normalized_slug] = {
                    "todo_items": max(0, int(todo_items)) if isinstance(todo_items, (int, float)) and not isinstance(todo_items, bool) else None,
                    "updated_at": float(updated_at) if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool) else None,
                    "stale": bool(raw_value.get("stale", False)),
                }
        raw_assignments = raw_state.get("role_assignments", {})
        _role_assignments = {
            str(instance_name): role
            for instance_name, role in raw_assignments.items()
            if isinstance(instance_name, str) and role in ("primary_canary", "fallback_fleet")
        } if isinstance(raw_assignments, dict) else {}
        _runtime = _default_runtime()
        raw_runtime = raw_state.get("runtime", {})
        if isinstance(raw_runtime, dict):
            for runtime_key in _runtime:
                if runtime_key in raw_runtime:
                    _runtime[runtime_key] = raw_runtime[runtime_key]
        raw_log = raw_state.get("decision_log", [])
        _decision_log = deque(
            (entry for entry in raw_log if isinstance(entry, dict)),
            maxlen=MAX_DECISION_LOG_ENTRIES,
        )
    logger.info(
        "Loaded project health state: %d project(s), %d tracker samples, %d role assignments",
        len(_project_history),
        loaded_samples,
        len(_role_assignments),
    )


def record_project_activity(
    project_slug: str,
    items_delta: int,
    bytes_delta: int,
    eligible_warriors: int,
    timestamp: float | None = None,
) -> None:
    """Record one per-project tracker sample.

    A sample is retained even when both deltas are zero. Zero-activity samples are
    required to measure coverage and distinguish a quiet project from missing data.
    """
    normalized_slug = _normalize_project_slug(project_slug)
    if not normalized_slug or not get_policy().get("monitoring_enabled", True):
        return
    sample_time = float(timestamp if timestamp is not None else time.time())
    normalized_eligible_warriors = max(0, int(eligible_warriors))
    if normalized_eligible_warriors <= 0:
        return
    sample = (
        sample_time,
        max(0, int(items_delta)),
        max(0, int(bytes_delta)),
        normalized_eligible_warriors,
    )
    cutoff = sample_time - HISTORY_SECONDS
    with _lock:
        samples = _project_history.setdefault(
            normalized_slug,
            deque(maxlen=MAX_HISTORY_SAMPLES_PER_PROJECT),
        )
        samples.append(sample)
        while samples and samples[0][0] < cutoff:
            samples.popleft()
    save()


def update_project_queue(
    project_slug: str,
    todo_items: int | None,
    updated_at: float | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    """Save the latest tracker queue observation for Project Health.

    A missing queue is represented by ``None`` and is never converted to zero.
    The caller owns tracker refresh policy; Project Health records the evidence
    used by the evaluator and exposes it in the status payload and decision log.
    """
    normalized_slug = _normalize_project_slug(project_slug)
    if not normalized_slug:
        raise ValueError("Project slug is required")
    normalized_todo = None
    if todo_items is not None and not isinstance(todo_items, bool):
        normalized_todo = max(0, int(todo_items))
    normalized_updated_at = None
    if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool):
        normalized_updated_at = float(updated_at)
    with _lock:
        _project_queue[normalized_slug] = {
            "todo_items": normalized_todo,
            "updated_at": normalized_updated_at,
            "stale": bool(stale),
        }
    save()
    return get_project_queue(normalized_slug)


def get_project_queue(project_slug: str) -> dict[str, Any]:
    normalized_slug = _normalize_project_slug(project_slug)
    with _lock:
        queue = copy.deepcopy(_project_queue.get(normalized_slug, {}))
    return {
        "todo_items": queue.get("todo_items"),
        "updated_at": queue.get("updated_at"),
        "stale": bool(queue.get("stale", False)),
        "available": queue.get("todo_items") is not None,
    }


def _window_samples(project_slug: str, start_time: float, end_time: float) -> list[tuple]:
    with _lock:
        samples = list(_project_history.get(project_slug, ()))
    return [sample for sample in samples if start_time <= sample[0] <= end_time]


def _coverage(sample_count: int, duration_seconds: int) -> float:
    expected_samples = max(1, duration_seconds // SAMPLE_INTERVAL_SECONDS)
    return min(1.0, sample_count / expected_samples)


def _window_readiness(project_slug: str, window_hours: int, current_time: float) -> dict[str, Any]:
    duration_seconds = window_hours * 60 * 60
    samples = _window_samples(project_slug, current_time - duration_seconds, current_time)
    sample_count = len(samples)
    expected_samples = max(1, duration_seconds // SAMPLE_INTERVAL_SECONDS)
    required_samples = math.ceil(expected_samples * MINIMUM_BASELINE_COVERAGE)
    coverage = _coverage(sample_count, duration_seconds)
    items_total = sum(sample[1] for sample in samples)
    bytes_total = sum(sample[2] for sample in samples)
    warrior_samples = sum(sample[3] for sample in samples)
    normalized_items_total = sum(sample[1] / sample[3] for sample in samples)
    normalized_bytes_total = sum(sample[2] / sample[3] for sample in samples)
    first_sample_at = samples[0][0] if samples else None
    last_sample_at = samples[-1][0] if samples else None
    span_seconds = 0.0
    if first_sample_at is not None and last_sample_at is not None:
        span_seconds = max(0.0, last_sample_at - first_sample_at)
    # Coverage may be imperfect, but baseline establishment still requires a real
    # elapsed history window. One sample interval of tolerance accounts for the
    # endpoints of a one-minute sampling series.
    required_span_seconds = max(0, duration_seconds - SAMPLE_INTERVAL_SECONDS)
    complete = (
        sample_count >= required_samples
        and coverage >= MINIMUM_BASELINE_COVERAGE
        and span_seconds >= required_span_seconds
    )
    sufficient_volume = (
        items_total >= MINIMUM_BASELINE_ITEMS_PER_HOUR * window_hours
        or bytes_total >= MINIMUM_BASELINE_BYTES_PER_HOUR * window_hours
    )
    return {
        "hours": window_hours,
        "complete": complete,
        "coverage": coverage,
        "coverage_percent": round(coverage * 100, 1),
        "sample_count": sample_count,
        "expected_sample_count": expected_samples,
        "required_sample_count": required_samples,
        "missing_sample_count": max(0, expected_samples - sample_count),
        "minimum_coverage_percent": MINIMUM_BASELINE_COVERAGE * 100,
        "first_sample_at": first_sample_at,
        "last_sample_at": last_sample_at,
        "span_seconds": span_seconds,
        "required_span_seconds": required_span_seconds,
        "items_total": items_total,
        "bytes_total": bytes_total,
        "eligible_warrior_samples": warrior_samples,
        "average_eligible_warriors": warrior_samples / sample_count if sample_count else 0.0,
        "items_per_hour": normalized_items_total / window_hours,
        "bytes_per_hour": normalized_bytes_total / window_hours,
        "raw_items_per_hour": items_total / window_hours,
        "raw_bytes_per_hour": bytes_total / window_hours,
        "normalization": "per_eligible_warrior",
        "sufficient_volume": sufficient_volume,
    }

def get_baseline_readiness(project_slug: str, now: float | None = None) -> dict[str, Any]:
    """Return durable establishment plus current-window data quality.

    Once the configured minimum or full baseline is earned, a later rolling
    coverage gap does not erase it. Only ``reset_primary_baseline`` removes the
    saved establishment timestamps.
    """
    current_time = float(now if now is not None else time.time())
    policy = get_policy()
    minimum = _window_readiness(project_slug, policy["minimum_baseline_hours"], current_time)
    full = _window_readiness(project_slug, policy["full_baseline_hours"], current_time)
    changed = False
    with _lock:
        established = _baseline_established.setdefault(
            project_slug,
            {"minimum_established_at": None, "full_established_at": None},
        )
        if minimum["complete"] and minimum["sufficient_volume"] and not established.get("minimum_established_at"):
            established["minimum_established_at"] = current_time
            changed = True
        if full["complete"] and full["sufficient_volume"] and not established.get("full_established_at"):
            established["full_established_at"] = current_time
            changed = True
        minimum_established_at = established.get("minimum_established_at")
        full_established_at = established.get("full_established_at")
    if changed:
        force_save()
    minimum_established = bool(minimum_established_at)
    full_established = bool(full_established_at)
    phase = "building"
    if minimum_established:
        phase = "full" if full_established else "established"
    return {
        "project": project_slug,
        "phase": phase,
        "complete": minimum_established,
        "established": minimum_established,
        "full_complete": full_established,
        "full_established": full_established,
        "minimum_established_at": minimum_established_at,
        "full_established_at": full_established_at,
        "coverage": minimum["coverage"],
        "coverage_percent": minimum["coverage_percent"],
        "current_window_complete": minimum["complete"],
        "minimum": minimum,
        "full": full,
        "sample_count": minimum["sample_count"],
        "expected_sample_count": minimum["expected_sample_count"],
        "required_sample_count": minimum["required_sample_count"],
        "missing_sample_count": minimum["missing_sample_count"],
        "first_sample_at": minimum["first_sample_at"],
        "last_sample_at": minimum["last_sample_at"],
        "span_seconds": minimum["span_seconds"],
        "items_total": minimum["items_total"],
        "bytes_total": minimum["bytes_total"],
        "items_per_hour": minimum["items_per_hour"],
        "bytes_per_hour": minimum["bytes_per_hour"],
        "sufficient_volume": minimum["sufficient_volume"],
    }

def _ratio(current_value: float, baseline_value: float) -> float | None:
    if baseline_value <= 0:
        return None
    return max(0.0, current_value / baseline_value)


def _minimum_ratio(item_ratio: float | None, byte_ratio: float | None) -> float | None:
    available = [ratio for ratio in (item_ratio, byte_ratio) if ratio is not None]
    return min(available) if available else None


def _classify_state(
    readiness: dict[str, Any],
    item_ratio: float | None,
    byte_ratio: float | None,
    previous_state: str | None,
    recent_data_current: bool,
    queue_reason: str | None,
) -> str:
    if not readiness.get("established"):
        return "Building baseline"
    if queue_reason == "queue_exhausted":
        return "Queue exhausted"
    if queue_reason == "queue_low":
        return "Queue nearly exhausted"
    if not recent_data_current:
        return "Monitoring delayed"
    if not readiness.get("sufficient_volume"):
        return "Building baseline"
    available = [ratio for ratio in (item_ratio, byte_ratio) if ratio is not None]
    if not available:
        return "Building baseline"
    # Either productive signal is enough to show that useful work is flowing.
    strongest_ratio = max(available)
    both_stalled = (
        item_ratio is not None
        and byte_ratio is not None
        and item_ratio < STALLED_RATIO
        and byte_ratio < STALLED_RATIO
    )
    if both_stalled:
        return "Likely stalled"
    if previous_state in ("Reduced activity", "Likely stalled") and strongest_ratio >= RECOVERY_RATIO:
        if strongest_ratio < HEALTHY_RATIO:
            return "Recovering"
    if strongest_ratio >= HEALTHY_RATIO:
        return "Healthy"
    return "Reduced activity"

def _operator_state(
    health_state: str,
    runtime: dict[str, Any],
    policy: dict[str, Any],
    now: float,
) -> str:
    """Prioritize transition state for the header and summary card."""
    if health_state == "Building baseline":
        return health_state
    if runtime.get("failover_in_progress"):
        if runtime.get("automatic_failover_state") == "failback_in_progress":
            return "Returning to primary"
        return "Failing over"
    cooldown_until = runtime.get("cooldown_until")
    if isinstance(cooldown_until, (int, float)) and float(cooldown_until) > now:
        return "Failover cooldown"
    required = int(policy.get("stalled_evaluations_required", 3))
    recovery_count = int(runtime.get("consecutive_recovered_evaluations", 0))
    if recovery_count > 0 and runtime.get("fleet_mode") in ("secondary", "mixed"):
        return f"Evaluating primary recovery {min(recovery_count, required)}/{required}"
    stalled_count = int(runtime.get("consecutive_stalled_evaluations", 0))
    if stalled_count > 0:
        return f"Evaluating stalled project {min(stalled_count, required)}/{required}"
    return health_state


def _last_health_state_locked(project_slug: str) -> str | None:
    for entry in reversed(_decision_log):
        if entry.get("event") == "health_state_changed" and entry.get("details", {}).get("project") == project_slug:
            return entry.get("details", {}).get("to_state")
    return None


def evaluate_project_health(project_slug: str, now: float | None = None) -> dict[str, Any]:
    """Evaluate throughput, queue runway, and current sample freshness."""
    current_time = float(now if now is not None else time.time())
    readiness = get_baseline_readiness(project_slug, current_time)
    with _lock:
        previous_state = _last_health_state_locked(project_slug)
    policy = get_policy()
    baseline_hours = (
        policy["full_baseline_hours"]
        if readiness["full_established"]
        else policy["minimum_baseline_hours"]
    )
    baseline_end = current_time - RECENT_WINDOW_SECONDS
    rolling_baseline = _window_readiness(project_slug, baseline_hours, baseline_end)
    baseline_values_available = rolling_baseline["sufficient_volume"] and rolling_baseline["sample_count"] > 0
    recent_samples = _window_samples(project_slug, current_time - RECENT_WINDOW_SECONDS, current_time)
    recent_raw_items = sum(sample[1] for sample in recent_samples)
    recent_raw_bytes = sum(sample[2] for sample in recent_samples)
    recent_items = sum(sample[1] / sample[3] for sample in recent_samples)
    recent_bytes = sum(sample[2] / sample[3] for sample in recent_samples)
    recent_warrior_samples = sum(sample[3] for sample in recent_samples)
    recent_average_eligible_warriors = (
        recent_warrior_samples / len(recent_samples) if recent_samples else 0.0
    )
    recent_coverage = _coverage(len(recent_samples), RECENT_WINDOW_SECONDS)
    latest_sample_at = recent_samples[-1][0] if recent_samples else None
    latest_sample_age_seconds = (
        max(0.0, current_time - latest_sample_at)
        if latest_sample_at is not None
        else None
    )
    recent_data_current = (
        latest_sample_age_seconds is not None
        and latest_sample_age_seconds <= MAXIMUM_CURRENT_SAMPLE_AGE_SECONDS
    )
    item_ratio = _ratio(
        recent_items,
        rolling_baseline["items_per_hour"] if readiness["established"] and baseline_values_available else 0.0,
    )
    byte_ratio = _ratio(
        recent_bytes,
        rolling_baseline["bytes_per_hour"] if readiness["established"] and baseline_values_available else 0.0,
    )
    recent_items_per_minute = recent_items / max(1.0, RECENT_WINDOW_SECONDS / 60.0)
    queue = get_project_queue(project_slug)
    queue_runway_minutes = None
    if queue["available"] and not queue["stale"] and recent_items_per_minute > 0:
        queue_runway_minutes = queue["todo_items"] / recent_items_per_minute
    queue_reason = None
    if queue["available"] and not queue["stale"]:
        if queue["todo_items"] == 0:
            queue_reason = "queue_exhausted"
        elif queue_runway_minutes is not None and queue_runway_minutes < LOW_QUEUE_RUNWAY_MINUTES:
            queue_reason = "queue_low"
    state = _classify_state(
        readiness,
        item_ratio,
        byte_ratio,
        previous_state,
        recent_data_current,
        queue_reason,
    )
    decision_reason = queue_reason
    if decision_reason is None and state == "Likely stalled":
        decision_reason = "throughput_stalled"
    observation_at = latest_sample_at
    if queue_reason and isinstance(queue.get("updated_at"), (int, float)):
        observation_at = max(observation_at or 0.0, float(queue["updated_at"]))
    operator_state = _operator_state(state, get_runtime(), policy, current_time)
    return {
        "project": project_slug,
        "state": operator_state,
        "health_state": state,
        "decision_reason": decision_reason,
        "observation_at": observation_at,
        "baseline": readiness,
        "rolling_baseline": rolling_baseline,
        "baseline_mode": "per_eligible_warrior_rolling_excluding_recent_hour",
        "recent": {
            "window_seconds": RECENT_WINDOW_SECONDS,
            "coverage": recent_coverage,
            "coverage_percent": round(recent_coverage * 100, 1),
            "sample_count": len(recent_samples),
            "items": recent_items,
            "bytes": recent_bytes,
            "raw_items": recent_raw_items,
            "raw_bytes": recent_raw_bytes,
            "average_eligible_warriors": recent_average_eligible_warriors,
            "items_per_minute": recent_items_per_minute,
            "normalization": "per_eligible_warrior",
            "latest_sample_at": latest_sample_at,
            "latest_sample_age_seconds": latest_sample_age_seconds,
            "current": recent_data_current,
            "maximum_sample_age_seconds": MAXIMUM_CURRENT_SAMPLE_AGE_SECONDS,
        },
        "queue": {
            **queue,
            "runway_minutes": queue_runway_minutes,
            "low_runway_threshold_minutes": LOW_QUEUE_RUNWAY_MINUTES,
        },
        "item_activity_ratio": item_ratio,
        "byte_activity_ratio": byte_ratio,
        "limiting_activity_ratio": _minimum_ratio(item_ratio, byte_ratio),
        "strongest_activity_ratio": max(
            [ratio for ratio in (item_ratio, byte_ratio) if ratio is not None],
            default=None,
        ),
    }

def update_health_state_log(project_slug: str) -> dict[str, Any]:
    """Evaluate health and log a state transition when the visible state changes."""
    evaluation = evaluate_project_health(project_slug)
    with _lock:
        previous_state = _last_health_state_locked(project_slug)
        if previous_state != evaluation["state"]:
            _append_decision_locked(
                "health_state_changed",
                "Project health changed from %s to %s" % (
                    previous_state or "uninitialized",
                    evaluation["state"],
                ),
                "monitor",
                {
                    "project": project_slug,
                    "from_state": previous_state,
                    "to_state": evaluation["state"],
                    "item_activity_ratio": evaluation["item_activity_ratio"],
                    "byte_activity_ratio": evaluation["byte_activity_ratio"],
                },
            )
    save()
    return evaluation


def configure_policy(
    primary_project: str,
    fallback_project: str,
    canary_percent: int = DEFAULT_CANARY_PERCENT,
    minimum_baseline_hours: int = DEFAULT_MINIMUM_BASELINE_HOURS,
    full_baseline_hours: int = DEFAULT_FULL_BASELINE_HOURS,
    monitoring_enabled: bool = True,
    automatic_failover_enabled: bool = False,
    stalled_evaluations_required: int = DEFAULT_STALLED_EVALUATIONS,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
    resume_stabilization_minutes: int = DEFAULT_RESUME_STABILIZATION_MINUTES,
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Persist primary/fallback policy. Automation remains disabled."""
    global _policy
    primary = _normalize_project_slug(primary_project)
    fallback = _normalize_project_slug(fallback_project)
    if not primary:
        raise ValueError("Primary project is required")
    if not fallback:
        raise ValueError("Fallback project is required")
    if primary == fallback:
        raise ValueError("Primary and fallback projects must be different")
    try:
        normalized_percent = int(canary_percent)
    except (TypeError, ValueError):
        raise ValueError("Canary allocation must be a whole-number percentage")
    if normalized_percent < 1 or normalized_percent > 100:
        raise ValueError("Canary allocation must be between 1% and 100%")
    try:
        normalized_minimum_hours = int(minimum_baseline_hours)
        normalized_full_hours = int(full_baseline_hours)
    except (TypeError, ValueError):
        raise ValueError("Baseline windows must be whole hours")
    if not MINIMUM_ALLOWED_BASELINE_HOURS <= normalized_minimum_hours <= MAXIMUM_ALLOWED_BASELINE_HOURS:
        raise ValueError("Minimum baseline must be between 1 and 48 hours")
    if not normalized_minimum_hours <= normalized_full_hours <= MAXIMUM_ALLOWED_BASELINE_HOURS:
        raise ValueError("Full baseline must be at least the minimum baseline and no more than 48 hours")
    normalized_stalled = max(MINIMUM_STALLED_EVALUATIONS, min(MAXIMUM_STALLED_EVALUATIONS, int(stalled_evaluations_required)))
    normalized_cooldown = max(MINIMUM_COOLDOWN_HOURS, min(MAXIMUM_COOLDOWN_HOURS, int(cooldown_hours)))
    normalized_stabilization = max(MINIMUM_RESUME_STABILIZATION_MINUTES, min(MAXIMUM_RESUME_STABILIZATION_MINUTES, int(resume_stabilization_minutes)))
    with _lock:
        old_primary = _policy.get("primary_project", "")
        old_monitoring = _policy.get("monitoring_enabled", True)
        _policy = {
            "primary_project": primary,
            "fallback_project": fallback,
            "canary_percent": normalized_percent,
            "minimum_baseline_hours": normalized_minimum_hours,
            "full_baseline_hours": normalized_full_hours,
            "monitoring_enabled": bool(monitoring_enabled),
            "automatic_failover_enabled": bool(automatic_failover_enabled),
            "stalled_evaluations_required": normalized_stalled,
            "cooldown_hours": normalized_cooldown,
            "resume_stabilization_minutes": normalized_stabilization,
            "updated_at": time.time(),
        }
        now = time.time()
        if old_monitoring != bool(monitoring_enabled):
            _runtime["consecutive_stalled_evaluations"] = 0
            if monitoring_enabled:
                _runtime["monitoring_resumed_at"] = now
                _runtime["stabilization_until"] = now + normalized_stabilization * 60
                toggle_event = "monitoring_enabled"
            else:
                _runtime["monitoring_paused_at"] = now
                _runtime["stabilization_until"] = None
                _runtime["automatic_failover_state"] = "disabled"
                toggle_event = "monitoring_disabled"
            _append_decision_locked(toggle_event, "Project Health monitoring " + ("enabled" if monitoring_enabled else "disabled"), actor, {})
        if old_primary != primary:
            _runtime["consecutive_stalled_evaluations"] = 0
        _append_decision_locked(
            "policy_updated",
            "Project health policy updated",
            actor,
            {
                "policy": copy.deepcopy(_policy),
                "primary_changed": old_primary != primary,
            },
        )
    force_save()
    return get_policy()


def get_policy() -> dict[str, Any]:
    with _lock:
        return copy.deepcopy(_policy or _default_policy())


def calculate_canary_count(fleet_size: int, percentage: int | None = None) -> int:
    """Calculate canaries using the configured percentage and 3..10 guardrails."""
    if fleet_size <= 0:
        return 0
    policy = get_policy()
    allocation = percentage if percentage is not None else policy["canary_percent"]
    requested_count = math.ceil(fleet_size * max(1, min(100, int(allocation))) / 100.0)
    guarded_count = max(MINIMUM_CANARY_COUNT, requested_count)
    guarded_count = min(MAXIMUM_CANARY_COUNT, guarded_count)
    return min(fleet_size, guarded_count)


def _canonical_host(host: str) -> str:
    normalized_host = str(host or "").strip().lower()
    try:
        return str(ipaddress.ip_address(normalized_host))
    except ValueError:
        return normalized_host


def recommend_canaries(instances: Iterable[Any], percentage: int | None = None) -> dict[str, Any]:
    """Recommend a representative canary set across unique host IPs.

    The first pass selects one online primary-project Warrior from each host. A
    second pass fills remaining slots from those hosts in round-robin order.
    Offline, paused, restarting, and non-primary Warriors are excluded.
    """
    policy = get_policy()
    primary_project = policy.get("primary_project", "")
    candidates: list[dict[str, Any]] = []
    for raw_instance in instances:
        instance = raw_instance.model_dump() if hasattr(raw_instance, "model_dump") else dict(raw_instance)
        if instance.get("connection_state") not in ("online", None):
            continue
        if instance.get("restarting"):
            continue
        actual_project = instance.get("project_slug") or instance.get("selected_project") or ""
        if primary_project and actual_project != primary_project:
            continue
        name = str(instance.get("name", ""))
        host = _canonical_host(instance.get("host", ""))
        if not name or not host:
            continue
        candidates.append({"name": name, "host": host, "project": actual_project})
    candidates.sort(key=lambda candidate: (candidate["host"], candidate["name"].lower()))
    desired_count = calculate_canary_count(len(candidates), percentage)
    by_host: dict[str, deque] = defaultdict(deque)
    for candidate in candidates:
        by_host[candidate["host"]].append(candidate)
    selected: list[dict[str, Any]] = []
    host_order = sorted(by_host)
    while len(selected) < desired_count:
        added_this_round = False
        for host in host_order:
            if by_host[host] and len(selected) < desired_count:
                selected.append(by_host[host].popleft())
                added_this_round = True
        if not added_this_round:
            break
    selected_names = {candidate["name"] for candidate in selected}
    fallback_fleet = [
        candidate for candidate in candidates if candidate["name"] not in selected_names
    ]
    return {
        "recommended_percent": DEFAULT_CANARY_PERCENT,
        "configured_percent": percentage if percentage is not None else policy["canary_percent"],
        "minimum_count": MINIMUM_CANARY_COUNT,
        "maximum_count": MAXIMUM_CANARY_COUNT,
        "eligible_fleet_size": len(candidates),
        "unique_host_count": len(by_host),
        "canary_count": len(selected),
        "primary_canaries": selected,
        "fallback_fleet": fallback_fleet,
    }


def assign_roles(primary_canaries: Iterable[str], fallback_fleet: Iterable[str], actor: str = "dashboard") -> dict[str, str]:
    """Persist the previewed manual-failover role assignments."""
    global _role_assignments
    canary_names = [str(name) for name in primary_canaries if str(name)]
    fallback_names = [str(name) for name in fallback_fleet if str(name)]
    overlap = set(canary_names).intersection(fallback_names)
    if overlap:
        raise ValueError("A Warrior cannot be assigned to both failover groups")
    assignments = {name: "primary_canary" for name in canary_names}
    assignments.update({name: "fallback_fleet" for name in fallback_names})
    with _lock:
        _role_assignments = assignments
        _append_decision_locked(
            "roles_assigned",
            "Manual failover role assignments saved",
            actor,
            {
                "primary_canaries": canary_names,
                "fallback_fleet": fallback_names,
            },
        )
    force_save()
    return copy.deepcopy(assignments)


def get_role_assignments() -> dict[str, str]:
    with _lock:
        return copy.deepcopy(_role_assignments)


def failover_availability() -> dict[str, Any]:
    """State whether manual failover may be offered to the user."""
    policy = get_policy()
    primary_project = policy.get("primary_project", "")
    readiness = get_baseline_readiness(primary_project) if primary_project else None
    reasons = []
    if not policy.get("monitoring_enabled", True):
        reasons.append("Turn on Project Health monitoring")
    if not primary_project:
        reasons.append("Configure a primary project")
    if not policy.get("fallback_project"):
        reasons.append("Configure a fallback project")
    if readiness and not readiness.get("complete"):
        reasons.append(
            "Complete the configured %d-hour minimum primary-project baseline with at least %.0f%% coverage"
            % (policy["minimum_baseline_hours"], MINIMUM_BASELINE_COVERAGE * 100)
        )
    if not get_role_assignments():
        reasons.append("Preview and save Primary canary and Fallback fleet assignments")
    return {
        "available": not reasons,
        "monitoring_enabled": policy.get("monitoring_enabled", True),
        "manual_only": False,
        "reasons": reasons,
        "primary_baseline": readiness,
        "message": (
            "Failover prerequisites are satisfied."
            if not reasons
            else "Failover unavailable: " + "; ".join(reasons) + "."
        ),
    }


def log_manual_failover_decision(
    action: str,
    actor: str = "dashboard",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a manual failover decision entry without changing any Warrior project."""
    allowed_actions = {"previewed", "requested", "confirmed", "cancelled", "completed", "failed"}
    if action not in allowed_actions:
        raise ValueError("Unsupported manual failover decision action")
    availability = failover_availability()
    if action in {"requested", "confirmed", "completed"} and not availability["available"]:
        raise ValueError(availability["message"])
    with _lock:
        entry = _append_decision_locked(
            "manual_failover_" + action,
            "Manual failover %s" % action,
            actor,
            details or {},
        )
    force_save()
    return copy.deepcopy(entry)


def complete_manual_fleet_switch(
    destination: str,
    instances: Iterable[Any],
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Synchronize automation with observed placement after a manual switch.

    Call only after the manual switch operation finishes, using a fresh fleet
    snapshot. Canary Warriors are excluded: fleet_mode describes the managed
    fallback fleet, not the primary canary population. Unknown or unrelated
    placements prevent a false declaration of complete recovery/failover.
    """
    if destination not in ("primary", "secondary"):
        raise ValueError("Manual switch destination must be primary or secondary")

    classified = [classify_instance_for_project_health(instance) for instance in instances]
    managed = [item for item in classified if item["role"] == "fallback_fleet"]
    if not managed:
        raise ValueError("Cannot synchronize fleet mode without assigned fallback Warriors")

    counts = {"primary": 0, "secondary": 0, "other": 0, "unknown": 0}
    for item in managed:
        counts[item["placement"]] += 1

    if counts["primary"] == len(managed):
        fleet_mode = "primary"
    elif counts["secondary"] == len(managed):
        fleet_mode = "secondary"
    else:
        fleet_mode = "mixed"

    with _lock:
        previous_mode = _runtime.get("fleet_mode", "primary")
        previous_transition_active = bool(_runtime.get("failover_in_progress"))
        previous_automation_state = _runtime.get("automatic_failover_state")
        _runtime["fleet_mode"] = fleet_mode
        # Manual placement is authoritative only after the caller has serialized
        # this operation against automatic transitions. Clear stale transition
        # flags so the operator badge reflects current health rather than an
        # abandoned automatic operation.
        _runtime["failover_in_progress"] = False
        _runtime["consecutive_stalled_evaluations"] = 0
        _runtime["consecutive_recovered_evaluations"] = 0
        _runtime["last_recovery_evaluated_sample_at"] = None
        _runtime["last_evaluated_sample_at"] = None
        # Do not alter cooldown or automatic-transition timestamps.
        _runtime["automatic_failover_state"] = "waiting_for_new_sample"
        _append_decision_locked(
            "manual_fleet_mode_updated",
            "Manual switch synchronized Project Health fleet mode",
            actor,
            {
                "destination": destination,
                "previous_mode": previous_mode,
                "previous_transition_active": previous_transition_active,
                "previous_automation_state": previous_automation_state,
                "fleet_mode": fleet_mode,
                "managed_counts": counts,
            },
        )
    force_save()
    return {"fleet_mode": fleet_mode, "managed_counts": counts}


def get_runtime() -> dict[str, Any]:
    with _lock:
        return copy.deepcopy(_runtime or _default_runtime())


def automatic_failover_prerequisites(now: float | None = None) -> dict[str, Any]:
    current_time = float(now if now is not None else time.time())
    policy = get_policy()
    availability = failover_availability()
    reasons = list(availability["reasons"])
    runtime = get_runtime()
    if not policy.get("automatic_failover_enabled"):
        reasons.append("Automatic failover is turned off")
    stabilization_until = runtime.get("stabilization_until")
    if stabilization_until and current_time < float(stabilization_until):
        reasons.append("Monitoring is stabilizing after resume")
    cooldown_until = runtime.get("cooldown_until")
    if cooldown_until and current_time < float(cooldown_until):
        reasons.append("Automatic failover is in cooldown")
    if runtime.get("failover_in_progress"):
        reasons.append("Automatic failover is already in progress")
    return {"available": not reasons, "reasons": reasons, "runtime": runtime}


def register_automatic_evaluation(evaluation: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Count one new five-minute evaluation and decide whether to fail over."""
    current_time = float(now if now is not None else time.time())
    policy = get_policy()
    prerequisites = automatic_failover_prerequisites(current_time)
    recent = evaluation.get("recent", {})
    latest_sample_at = recent.get("latest_sample_at")
    observation_at = evaluation.get("observation_at", latest_sample_at)
    reason = evaluation.get("decision_reason")
    qualifying_reason = reason in {"queue_exhausted", "queue_low", "throughput_stalled"}
    with _lock:
        previous_sample_at = _runtime.get("last_evaluated_sample_at")
        new_sample = (
            isinstance(observation_at, (int, float))
            and not isinstance(observation_at, bool)
            and (previous_sample_at is None or float(observation_at) > float(previous_sample_at))
        )
        _runtime["last_evaluation_at"] = current_time
        _runtime["last_evaluation_reason"] = reason
        if new_sample:
            _runtime["last_evaluated_sample_at"] = float(observation_at)
        if not prerequisites["available"]:
            _runtime["consecutive_stalled_evaluations"] = 0
            _runtime["automatic_failover_state"] = "blocked"
        elif not new_sample:
            _runtime["automatic_failover_state"] = "waiting_for_new_sample"
        elif qualifying_reason:
            _runtime["consecutive_stalled_evaluations"] = int(_runtime.get("consecutive_stalled_evaluations", 0)) + 1
            _runtime["automatic_failover_state"] = "confirming_" + reason
        else:
            _runtime["consecutive_stalled_evaluations"] = 0
            _runtime["automatic_failover_state"] = "armed"
        trigger = (
            prerequisites["available"]
            and new_sample
            and qualifying_reason
            and _runtime["consecutive_stalled_evaluations"] >= policy["stalled_evaluations_required"]
        )
        if trigger:
            _runtime["failover_in_progress"] = True
            _runtime["automatic_failover_state"] = "failover_in_progress"
            _append_decision_locked(
                "automatic_failover_triggered",
                "Automatic failover threshold reached",
                "automation",
                {"reason": reason, "evaluation": copy.deepcopy(evaluation)},
            )
    force_save()
    return {
        "trigger": trigger,
        "qualifying_reason": reason if qualifying_reason else None,
        "new_sample": new_sample,
        "runtime": get_runtime(),
        "prerequisites": prerequisites,
    }

def complete_automatic_failover(switched: int, failed: int, results: dict[str, Any]) -> None:
    with _lock:
        now = time.time()
        _runtime["failover_in_progress"] = False
        _runtime["last_automatic_failover_at"] = now
        _runtime["last_automatic_transition"] = "failover"
        _runtime["fleet_mode"] = "secondary" if failed == 0 else "mixed"
        _runtime["cooldown_until"] = now + get_policy()["cooldown_hours"] * 3600
        _runtime["consecutive_stalled_evaluations"] = 0
        _runtime["consecutive_recovered_evaluations"] = 0
        _runtime["last_recovery_evaluated_sample_at"] = None
        _runtime["automatic_failover_state"] = "cooldown"
        _runtime["last_automatic_result"] = {
            "direction": "secondary",
            "switched": switched,
            "failed": failed,
        }
        _append_decision_locked(
            "automatic_failover_completed" if failed == 0 else "automatic_failover_partial",
            "Automatic failover finished",
            "automation",
            {"switched": switched, "failed": failed, "results": copy.deepcopy(results)},
        )
    force_save()


def register_automatic_recovery_evaluation(
    evaluation: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    """Confirm that the primary is ready for automatic failback.

    Recovery requires a current, established normalized baseline, at least one
    activity ratio at or above 50%, and at least 60 minutes of fresh queue
    runway. The existing stalled-evaluation count controls the number of
    consecutive five-minute recovery observations required.
    """
    current_time = float(now if now is not None else time.time())
    policy = get_policy()
    runtime = get_runtime()
    recent = evaluation.get("recent", {})
    queue = evaluation.get("queue", {})
    readiness = evaluation.get("baseline", {})
    observation_at = evaluation.get("observation_at", recent.get("latest_sample_at"))
    strongest_ratio = evaluation.get("strongest_activity_ratio")
    runway_minutes = queue.get("runway_minutes")
    cooldown_until = runtime.get("cooldown_until")
    cooldown_complete = not cooldown_until or current_time >= float(cooldown_until)
    fleet_on_secondary = runtime.get("fleet_mode") in ("secondary", "mixed")
    queue_sufficient = (
        queue.get("available")
        and not queue.get("stale")
        and isinstance(runway_minutes, (int, float))
        and not isinstance(runway_minutes, bool)
        and float(runway_minutes) >= FAILBACK_MINIMUM_RUNWAY_MINUTES
    )
    activity_recovered = (
        isinstance(strongest_ratio, (int, float))
        and not isinstance(strongest_ratio, bool)
        and float(strongest_ratio) >= FAILBACK_RECOVERY_RATIO
    )
    qualifying = (
        policy.get("monitoring_enabled")
        and policy.get("automatic_failover_enabled")
        and fleet_on_secondary
        and cooldown_complete
        and readiness.get("established")
        and recent.get("current")
        and activity_recovered
        and queue_sufficient
    )
    with _lock:
        previous_observation = _runtime.get("last_recovery_evaluated_sample_at")
        new_sample = (
            isinstance(observation_at, (int, float))
            and not isinstance(observation_at, bool)
            and (
                previous_observation is None
                or float(observation_at) > float(previous_observation)
            )
        )
        if new_sample:
            _runtime["last_recovery_evaluated_sample_at"] = float(observation_at)
        if not fleet_on_secondary:
            _runtime["consecutive_recovered_evaluations"] = 0
        elif not cooldown_complete:
            _runtime["consecutive_recovered_evaluations"] = 0
            _runtime["automatic_failover_state"] = "cooldown"
        elif new_sample and qualifying:
            _runtime["consecutive_recovered_evaluations"] = int(
                _runtime.get("consecutive_recovered_evaluations", 0)
            ) + 1
            _runtime["automatic_failover_state"] = "confirming_primary_recovery"
        elif new_sample:
            _runtime["consecutive_recovered_evaluations"] = 0
            _runtime["automatic_failover_state"] = "waiting_for_primary_recovery"
        trigger = (
            new_sample
            and qualifying
            and _runtime["consecutive_recovered_evaluations"]
            >= policy["stalled_evaluations_required"]
        )
        if trigger:
            _runtime["failover_in_progress"] = True
            _runtime["automatic_failover_state"] = "failback_in_progress"
            _append_decision_locked(
                "automatic_failback_triggered",
                "Primary recovery threshold reached",
                "automation",
                {
                    "evaluation": copy.deepcopy(evaluation),
                    "recovery_ratio_required": FAILBACK_RECOVERY_RATIO,
                    "minimum_runway_minutes": FAILBACK_MINIMUM_RUNWAY_MINUTES,
                },
            )
    force_save()
    return {
        "trigger": trigger,
        "new_sample": new_sample,
        "qualifying": qualifying,
        "activity_recovered": activity_recovered,
        "queue_sufficient": queue_sufficient,
        "cooldown_complete": cooldown_complete,
        "runtime": get_runtime(),
    }


def cancel_automatic_failback(
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Cancel a triggered failback without recording a completed transition.

    The recovery confirmation sequence must begin again on fresh observations.
    Preserve the fleet mode, cooldown, and last successful transition metadata.
    """
    with _lock:
        _runtime["failover_in_progress"] = False
        _runtime["consecutive_recovered_evaluations"] = 0
        _runtime["automatic_failover_state"] = "waiting_for_primary_recovery"
        _runtime["last_automatic_result"] = {
            "direction": "primary",
            "status": "cancelled",
            "reason": str(reason),
        }
        _append_decision_locked(
            "automatic_failback_cancelled",
            "Automatic failback cancelled: " + str(reason),
            "automation",
            {"reason": str(reason), "evidence": copy.deepcopy(evidence or {})},
        )
    force_save()


def complete_automatic_failback(
    switched: int,
    failed: int,
    results: dict[str, Any],
) -> None:
    """Finish an automatic return to primary and restart the shared cooldown."""
    with _lock:
        now = time.time()
        _runtime["failover_in_progress"] = False
        _runtime["last_automatic_failback_at"] = now
        _runtime["last_automatic_transition"] = "failback"
        _runtime["fleet_mode"] = "primary" if failed == 0 else "mixed"
        _runtime["cooldown_until"] = now + get_policy()["cooldown_hours"] * 3600
        _runtime["consecutive_stalled_evaluations"] = 0
        _runtime["consecutive_recovered_evaluations"] = 0
        _runtime["last_recovery_evaluated_sample_at"] = None
        _runtime["automatic_failover_state"] = "cooldown"
        _runtime["last_automatic_result"] = {
            "direction": "primary",
            "switched": switched,
            "failed": failed,
        }
        _append_decision_locked(
            "automatic_failback_completed" if failed == 0 else "automatic_failback_partial",
            "Automatic failback to primary finished",
            "automation",
            {"switched": switched, "failed": failed, "results": copy.deepcopy(results)},
        )
    force_save()


def reset_primary_baseline(confirmation: str, actor: str = "dashboard") -> dict[str, Any]:
    policy = get_policy()
    primary = policy.get("primary_project", "")
    expected = "RESET " + primary
    if not primary:
        raise ValueError("Configure a primary project before resetting its baseline")
    if str(confirmation).strip() != expected:
        raise ValueError("Type %s to confirm baseline reset" % expected)
    with _lock:
        previous_samples = list(_project_history.get(primary, ()))
        previous_readiness = get_baseline_readiness(primary)
        deleted_samples = len(previous_samples)
        _project_history.pop(primary, None)
        _baseline_established.pop(primary, None)
        _runtime["consecutive_stalled_evaluations"] = 0
        _runtime["last_evaluated_sample_at"] = None
        _runtime["last_evaluation_reason"] = None
        _runtime["automatic_failover_state"] = "waiting_for_baseline"
        _runtime["stabilization_until"] = None
        _append_decision_locked("baseline_reset", "Primary-project baseline history deleted", actor, {"project": primary, "deleted_samples": deleted_samples, "previous_minimum_coverage": previous_readiness["minimum"]["coverage_percent"], "previous_full_coverage": previous_readiness["full"]["coverage_percent"], "automatic_failover_enabled": policy.get("automatic_failover_enabled", False)})
    force_save()
    return {"project": primary, "deleted_samples": deleted_samples, "baseline_phase": "building", "automatic_failover_armed": policy.get("automatic_failover_enabled", False), "automatic_failover_available": False}


def _append_decision_locked(
    event: str,
    message: str,
    actor: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    entry = {
        "timestamp": time.time(),
        "event": event,
        "message": message,
        "actor": str(actor or "dashboard")[:200],
        "details": copy.deepcopy(details),
    }
    _decision_log.append(entry)
    return entry


def get_decision_log(limit: int = 100) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(MAX_DECISION_LOG_ENTRIES, int(limit)))
    with _lock:
        return copy.deepcopy(list(_decision_log)[-normalized_limit:][::-1])


def get_project_history(project_slug: str = "") -> dict[str, Any]:
    """Return persistent 48-hour per-project tracker history."""
    cutoff = time.time() - HISTORY_SECONDS
    with _lock:
        selected_projects = (
            [project_slug]
            if project_slug
            else sorted(_project_history.keys())
        )
        projects_data = {}
        for selected_project in selected_projects:
            samples = _project_history.get(selected_project, ())
            projects_data[selected_project] = [
                {
                    "t": sample[0],
                    "items": sample[1],
                    "bytes": sample[2],
                    "eligible_warriors": sample[3],
                    "items_per_warrior": sample[1] / sample[3],
                    "bytes_per_warrior": sample[2] / sample[3],
                }
                for sample in samples
                if sample[0] >= cutoff
            ]
    return {
        "retention_hours": 48,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "projects": projects_data,
    }


def classify_instance_for_project_health(instance: Any) -> dict[str, Any]:
    raw = instance.model_dump() if hasattr(instance, "model_dump") else dict(instance)
    name = str(raw.get("name") or "")
    role = get_role_assignments().get(name, "unassigned")
    policy = get_policy()
    primary = policy.get("primary_project", "")
    secondary = policy.get("fallback_project", "")
    project = _normalize_project_slug(raw.get("project_slug"))
    connection = raw.get("connection_state")
    if hasattr(connection, "value"):
        connection = connection.value
    online = connection == "online"
    restarting = bool(raw.get("restarting"))
    if not project:
        placement = "unknown"
    elif project == primary:
        placement = "primary"
    elif project == secondary:
        placement = "secondary"
    else:
        placement = "other"
    switchable = (
        role == "fallback_fleet"
        and online
        and not restarting
        and placement in ("primary", "secondary")
    )
    return {
        "name": name,
        "role": role,
        "project": project,
        "placement": placement,
        "online": online,
        "restarting": restarting,
        "eligible_for_sampling": switchable,
        "switchable": switchable,
    }


def get_fleet_placement(instances: Iterable[Any]) -> dict[str, Any]:
    classified = [classify_instance_for_project_health(instance) for instance in instances]
    counts = {"primary": 0, "secondary": 0, "other": 0, "unknown": 0}
    names = {key: [] for key in counts}
    for item in classified:
        counts[item["placement"]] += 1
        names[item["placement"]].append(item["name"])
    return {"counts": counts, "names": names, "instances": classified}


def get_status(instances: Iterable[Any] = ()) -> dict[str, Any]:
    """Return the complete Project Health modal payload."""
    policy = get_policy()
    instance_list = list(instances)
    project_slugs = set(get_project_history()["projects"].keys())
    if policy.get("primary_project"):
        project_slugs.add(policy["primary_project"])
    if policy.get("fallback_project"):
        project_slugs.add(policy["fallback_project"])
    evaluations = {
        project_slug: evaluate_project_health(project_slug)
        for project_slug in sorted(project_slugs)
    }
    return {
        "mode": "automatic_capable",
        "monitoring_enabled": policy.get("monitoring_enabled", True),
        "automatic_switching_enabled": policy.get("automatic_failover_enabled", False),
        "automation": automatic_failover_prerequisites(),
        "runtime": get_runtime(),
        "policy": policy,
        "health": evaluations,
        "roles": get_role_assignments(),
        "failover": failover_availability(),
        "recommendation": recommend_canaries(instance_list),
        "fleet_placement": get_fleet_placement(instance_list),
        "decision_log": get_decision_log(100),
        "thresholds": {
            "minimum_baseline_hours": policy["minimum_baseline_hours"],
            "full_baseline_hours": policy["full_baseline_hours"],
            "history_hours": 48,
            "minimum_coverage_percent": MINIMUM_BASELINE_COVERAGE * 100,
            "evaluation_interval_seconds": 300,
            "maximum_current_sample_age_seconds": MAXIMUM_CURRENT_SAMPLE_AGE_SECONDS,
            "low_queue_runway_minutes": LOW_QUEUE_RUNWAY_MINUTES,
            "healthy_ratio": HEALTHY_RATIO,
            "reduced_ratio": REDUCED_RATIO,
            "stalled_ratio": STALLED_RATIO,
            "recovery_ratio": RECOVERY_RATIO,
            "failback_recovery_ratio": FAILBACK_RECOVERY_RATIO,
            "failback_minimum_runway_minutes": FAILBACK_MINIMUM_RUNWAY_MINUTES,
            "recommended_canary_percent": DEFAULT_CANARY_PERCENT,
            "minimum_canaries": MINIMUM_CANARY_COUNT,
            "maximum_canaries": MAXIMUM_CANARY_COUNT,
        },
    }
