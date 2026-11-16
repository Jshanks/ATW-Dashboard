"""Persistent Project Health monitoring, baseline management, and failover state.

v4.6.0 adds an independent monitoring master switch, persistent automatic-failover
policy and runtime state, and destructive primary-baseline reset. Network project
switches remain in main.py; this module owns evaluation, prerequisites, counters,
cooldowns, persistence, and the auditable decision log.
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
MINIMUM_BASELINE_COVERAGE = 0.90
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
STALLED_RATIO = 0.10
RECOVERY_RATIO = 0.35

_lock = threading.RLock()
_project_history: dict[str, deque] = {}
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
        "cooldown_until": None,
        "automatic_failover_state": "disabled",
        "failover_in_progress": False,
        "last_automatic_failover_at": None,
        "last_automatic_result": None,
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
        "version": 2,
        "history": history_data,
        "policy": copy.deepcopy(_policy),
        "role_assignments": copy.deepcopy(_role_assignments),
        "baseline_mode": "rolling_excluding_recent_hour",
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
    global _policy, _role_assignments, _runtime, _decision_log
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
                    try:
                        timestamp = float(raw_sample[0])
                        items_delta = max(0, int(raw_sample[1]))
                        bytes_delta = max(0, int(raw_sample[2]))
                    except (TypeError, ValueError):
                        continue
                    if timestamp >= cutoff:
                        samples.append((timestamp, items_delta, bytes_delta))
                        loaded_samples += 1
                if samples:
                    _project_history[normalized_slug] = samples
        _policy = _normalize_policy(raw_state.get("policy"))
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
    sample = (sample_time, max(0, int(items_delta)), max(0, int(bytes_delta)))
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
    first_sample_at = samples[0][0] if samples else None
    last_sample_at = samples[-1][0] if samples else None
    span_seconds = 0.0
    if first_sample_at is not None and last_sample_at is not None:
        span_seconds = max(0.0, last_sample_at - first_sample_at)
    complete = (
        sample_count >= required_samples
        and coverage >= MINIMUM_BASELINE_COVERAGE
        and span_seconds >= duration_seconds * MINIMUM_BASELINE_COVERAGE
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
        "required_sample_count": required_samples,
        "first_sample_at": first_sample_at,
        "last_sample_at": last_sample_at,
        "span_seconds": span_seconds,
        "items_total": items_total,
        "bytes_total": bytes_total,
        "items_per_hour": items_total / window_hours,
        "bytes_per_hour": bytes_total / window_hours,
        "sufficient_volume": sufficient_volume,
    }


def get_baseline_readiness(project_slug: str, now: float | None = None) -> dict[str, Any]:
    """Return minimum and full baseline readiness using the saved policy windows."""
    current_time = float(now if now is not None else time.time())
    policy = get_policy()
    minimum = _window_readiness(project_slug, policy["minimum_baseline_hours"], current_time)
    full = _window_readiness(project_slug, policy["full_baseline_hours"], current_time)
    phase = "building"
    if minimum["complete"]:
        phase = "full" if full["complete"] else "provisional"
    return {
        "project": project_slug,
        "phase": phase,
        "complete": minimum["complete"],
        "full_complete": full["complete"],
        "coverage": minimum["coverage"],
        "coverage_percent": minimum["coverage_percent"],
        "minimum": minimum,
        "full": full,
        # Compatibility fields retained for the v4.5.0 UI and classifier.
        "sample_count": minimum["sample_count"],
        "required_sample_count": minimum["required_sample_count"],
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
) -> str:
    if not readiness.get("complete"):
        return "Building baseline"
    if not readiness.get("sufficient_volume"):
        return "Insufficient volume"
    limiting_ratio = _minimum_ratio(item_ratio, byte_ratio)
    if limiting_ratio is None:
        return "Insufficient volume"
    if previous_state in ("Reduced activity", "Likely stalled") and limiting_ratio >= RECOVERY_RATIO:
        if limiting_ratio < HEALTHY_RATIO:
            return "Recovering"
    if limiting_ratio >= HEALTHY_RATIO:
        return "Healthy"
    if limiting_ratio >= REDUCED_RATIO:
        return "Reduced activity"
    if limiting_ratio < STALLED_RATIO:
        return "Likely stalled"
    return "Reduced activity"


def _last_health_state_locked(project_slug: str) -> str | None:
    for entry in reversed(_decision_log):
        if entry.get("event") == "health_state_changed" and entry.get("details", {}).get("project") == project_slug:
            return entry.get("details", {}).get("to_state")
    return None


def evaluate_project_health(project_slug: str, now: float | None = None) -> dict[str, Any]:
    """Evaluate current activity against a rolling baseline excluding the latest hour."""
    current_time = float(now if now is not None else time.time())
    readiness = get_baseline_readiness(project_slug, current_time)
    with _lock:
        previous_state = _last_health_state_locked(project_slug)

    policy = get_policy()
    baseline_hours = (
        policy["full_baseline_hours"]
        if readiness["full_complete"]
        else policy["minimum_baseline_hours"]
    )
    baseline_end = current_time - RECENT_WINDOW_SECONDS
    rolling_baseline = _window_readiness(project_slug, baseline_hours, baseline_end)
    baseline_ready = readiness["complete"] and rolling_baseline["complete"]
    comparison_readiness = copy.deepcopy(readiness)
    comparison_readiness["complete"] = baseline_ready
    comparison_readiness["sufficient_volume"] = rolling_baseline["sufficient_volume"]

    recent_samples = _window_samples(
        project_slug,
        current_time - RECENT_WINDOW_SECONDS,
        current_time,
    )
    recent_items = sum(sample[1] for sample in recent_samples)
    recent_bytes = sum(sample[2] for sample in recent_samples)
    recent_coverage = _coverage(len(recent_samples), RECENT_WINDOW_SECONDS)
    item_ratio = _ratio(
        recent_items,
        rolling_baseline["items_per_hour"] if baseline_ready else 0.0,
    )
    byte_ratio = _ratio(
        recent_bytes,
        rolling_baseline["bytes_per_hour"] if baseline_ready else 0.0,
    )
    state = _classify_state(comparison_readiness, item_ratio, byte_ratio, previous_state)
    return {
        "project": project_slug,
        "state": state,
        "baseline": readiness,
        "rolling_baseline": rolling_baseline,
        "baseline_mode": "rolling_excluding_recent_hour",
        "recent": {
            "window_seconds": RECENT_WINDOW_SECONDS,
            "coverage": recent_coverage,
            "coverage_percent": round(recent_coverage * 100, 1),
            "items": recent_items,
            "bytes": recent_bytes,
        },
        "item_activity_ratio": item_ratio,
        "byte_activity_ratio": byte_ratio,
        "limiting_activity_ratio": _minimum_ratio(item_ratio, byte_ratio),
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
            "Complete the configured %d-hour minimum primary-project baseline with at least 90%% coverage"
            % policy["minimum_baseline_hours"]
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
    """Persist one evaluator result and state whether main.py should switch the fleet."""
    current_time = float(now if now is not None else time.time())
    policy = get_policy()
    prerequisites = automatic_failover_prerequisites(current_time)
    item_ratio = evaluation.get("item_activity_ratio")
    byte_ratio = evaluation.get("byte_activity_ratio")
    both_stalled = (
        evaluation.get("state") == "Likely stalled"
        and item_ratio is not None and byte_ratio is not None
        and item_ratio < STALLED_RATIO and byte_ratio < STALLED_RATIO
    )
    with _lock:
        if not prerequisites["available"]:
            _runtime["consecutive_stalled_evaluations"] = 0
            _runtime["automatic_failover_state"] = "blocked"
        elif both_stalled:
            _runtime["consecutive_stalled_evaluations"] = int(_runtime.get("consecutive_stalled_evaluations", 0)) + 1
            _runtime["automatic_failover_state"] = "confirming_stall"
        else:
            _runtime["consecutive_stalled_evaluations"] = 0
            _runtime["automatic_failover_state"] = "armed"
        trigger = (
            prerequisites["available"] and both_stalled
            and _runtime["consecutive_stalled_evaluations"] >= policy["stalled_evaluations_required"]
        )
        if trigger:
            _runtime["failover_in_progress"] = True
            _runtime["automatic_failover_state"] = "failover_in_progress"
            _append_decision_locked("automatic_failover_triggered", "Automatic failover threshold reached", "automation", {"evaluation": copy.deepcopy(evaluation)})
    force_save()
    return {"trigger": trigger, "both_ratios_stalled": both_stalled, "runtime": get_runtime(), "prerequisites": prerequisites}


def complete_automatic_failover(switched: int, failed: int, results: dict[str, Any]) -> None:
    with _lock:
        now = time.time()
        _runtime["failover_in_progress"] = False
        _runtime["last_automatic_failover_at"] = now
        _runtime["cooldown_until"] = now + get_policy()["cooldown_hours"] * 3600
        _runtime["consecutive_stalled_evaluations"] = 0
        _runtime["automatic_failover_state"] = "cooldown"
        _runtime["last_automatic_result"] = {"switched": switched, "failed": failed}
        _append_decision_locked("automatic_failover_completed" if failed == 0 else "automatic_failover_partial", "Automatic failover finished", "automation", {"switched": switched, "failed": failed, "results": copy.deepcopy(results)})
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
        _runtime["consecutive_stalled_evaluations"] = 0
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
                {"t": sample[0], "items": sample[1], "bytes": sample[2]}
                for sample in samples
                if sample[0] >= cutoff
            ]
    return {
        "retention_hours": 48,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "projects": projects_data,
    }


def get_status(instances: Iterable[Any] = ()) -> dict[str, Any]:
    """Return the complete Project Health modal payload."""
    policy = get_policy()
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
        "recommendation": recommend_canaries(instances),
        "decision_log": get_decision_log(100),
        "thresholds": {
            "minimum_baseline_hours": policy["minimum_baseline_hours"],
            "full_baseline_hours": policy["full_baseline_hours"],
            "history_hours": 48,
            "minimum_coverage_percent": MINIMUM_BASELINE_COVERAGE * 100,
            "healthy_ratio": HEALTHY_RATIO,
            "reduced_ratio": REDUCED_RATIO,
            "stalled_ratio": STALLED_RATIO,
            "recovery_ratio": RECOVERY_RATIO,
            "recommended_canary_percent": DEFAULT_CANARY_PERCENT,
            "minimum_canaries": MINIMUM_CANARY_COUNT,
            "maximum_canaries": MAXIMUM_CANARY_COUNT,
        },
    }
