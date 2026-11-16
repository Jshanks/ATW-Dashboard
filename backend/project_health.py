"""Persistent project-health monitoring and manual failover planning for ATW Dashboard.

v4.5.0 is intentionally monitor-only. This module records per-project tracker
activity, evaluates project health, recommends representative canaries, persists
the configured policy and role assignments, and keeps an auditable decision log.
It never changes a Warrior project on its own.
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
BASELINE_SECONDS = 24 * 60 * 60
RECENT_WINDOW_SECONDS = 60 * 60
SAMPLE_INTERVAL_SECONDS = 60
EXPECTED_BASELINE_SAMPLES = BASELINE_SECONDS // SAMPLE_INTERVAL_SECONDS
MINIMUM_BASELINE_COVERAGE = 0.90
MINIMUM_VALID_BASELINE_SAMPLES = math.ceil(
    EXPECTED_BASELINE_SAMPLES * MINIMUM_BASELINE_COVERAGE
)
MAX_HISTORY_SAMPLES_PER_PROJECT = HISTORY_SECONDS // SAMPLE_INTERVAL_SECONDS + 10
MAX_DECISION_LOG_ENTRIES = 500

DEFAULT_CANARY_PERCENT = 5
MINIMUM_CANARY_COUNT = 3
MAXIMUM_CANARY_COUNT = 10

# Low-volume projects should not be declared stalled from ratios built on noise.
MINIMUM_BASELINE_ITEMS_PER_HOUR = 1.0
MINIMUM_BASELINE_BYTES_PER_HOUR = 1024 * 1024

# Ratios compare the latest complete hour with the frozen 24-hour baseline's
# average hourly rate. Both item and byte ratios participate in classification.
HEALTHY_RATIO = 0.75
REDUCED_RATIO = 0.35
STALLED_RATIO = 0.10
RECOVERY_RATIO = 0.35

_lock = threading.RLock()
_project_history: dict[str, deque] = {}
_policy: dict[str, Any] = {}
_role_assignments: dict[str, str] = {}
_frozen_baseline: dict[str, Any] | None = None
_decision_log: deque = deque(maxlen=MAX_DECISION_LOG_ENTRIES)
_last_save_monotonic = 0.0
SAVE_INTERVAL_SECONDS = 60


def _default_policy() -> dict[str, Any]:
    return {
        "primary_project": "",
        "fallback_project": "",
        "canary_percent": DEFAULT_CANARY_PERCENT,
        "monitor_only": True,
        "manual_failover_only": True,
        "updated_at": 0.0,
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
    policy["monitor_only"] = True
    policy["manual_failover_only"] = True
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
        "version": 1,
        "history": history_data,
        "policy": copy.deepcopy(_policy),
        "role_assignments": copy.deepcopy(_role_assignments),
        "frozen_baseline": copy.deepcopy(_frozen_baseline),
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
    global _policy, _role_assignments, _frozen_baseline, _decision_log
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
        raw_baseline = raw_state.get("frozen_baseline")
        _frozen_baseline = raw_baseline if isinstance(raw_baseline, dict) else None
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
    if not normalized_slug:
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


def get_baseline_readiness(project_slug: str, now: float | None = None) -> dict[str, Any]:
    """Return primary-project baseline readiness for the most recent 24 hours."""
    current_time = float(now if now is not None else time.time())
    start_time = current_time - BASELINE_SECONDS
    samples = _window_samples(project_slug, start_time, current_time)
    sample_count = len(samples)
    coverage = _coverage(sample_count, BASELINE_SECONDS)
    items_total = sum(sample[1] for sample in samples)
    bytes_total = sum(sample[2] for sample in samples)
    first_sample_at = samples[0][0] if samples else None
    last_sample_at = samples[-1][0] if samples else None
    span_seconds = 0.0
    if first_sample_at is not None and last_sample_at is not None:
        span_seconds = max(0.0, last_sample_at - first_sample_at)
    complete = (
        sample_count >= MINIMUM_VALID_BASELINE_SAMPLES
        and coverage >= MINIMUM_BASELINE_COVERAGE
        and span_seconds >= BASELINE_SECONDS * MINIMUM_BASELINE_COVERAGE
    )
    sufficient_volume = (
        items_total >= MINIMUM_BASELINE_ITEMS_PER_HOUR * 24
        or bytes_total >= MINIMUM_BASELINE_BYTES_PER_HOUR * 24
    )
    return {
        "project": project_slug,
        "complete": complete,
        "coverage": coverage,
        "coverage_percent": round(coverage * 100, 1),
        "sample_count": sample_count,
        "required_sample_count": MINIMUM_VALID_BASELINE_SAMPLES,
        "first_sample_at": first_sample_at,
        "last_sample_at": last_sample_at,
        "span_seconds": span_seconds,
        "items_total": items_total,
        "bytes_total": bytes_total,
        "sufficient_volume": sufficient_volume,
    }


def freeze_primary_baseline(actor: str = "dashboard") -> dict[str, Any]:
    """Freeze the current complete primary-project baseline for later comparisons."""
    global _frozen_baseline
    with _lock:
        primary_project = _policy.get("primary_project", "")
    readiness = get_baseline_readiness(primary_project) if primary_project else {
        "complete": False,
        "coverage_percent": 0.0,
    }
    if not primary_project:
        raise ValueError("A primary project must be configured before freezing a baseline")
    if not readiness.get("complete"):
        raise ValueError(
            "A complete 24-hour primary-project baseline with at least 90% coverage is required"
        )
    frozen_at = time.time()
    baseline = {
        "project": primary_project,
        "frozen_at": frozen_at,
        "coverage": readiness["coverage"],
        "coverage_percent": readiness["coverage_percent"],
        "sample_count": readiness["sample_count"],
        "items_total": readiness["items_total"],
        "bytes_total": readiness["bytes_total"],
        "items_per_hour": readiness["items_total"] / 24.0,
        "bytes_per_hour": readiness["bytes_total"] / 24.0,
        "sufficient_volume": readiness["sufficient_volume"],
    }
    with _lock:
        _frozen_baseline = baseline
        _append_decision_locked(
            "baseline_frozen",
            "Primary-project baseline frozen for health comparisons",
            actor,
            {"baseline": copy.deepcopy(baseline)},
        )
    force_save()
    return copy.deepcopy(baseline)


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
    """Evaluate a project's current state against the frozen primary baseline."""
    current_time = float(now if now is not None else time.time())
    readiness = get_baseline_readiness(project_slug, current_time)
    with _lock:
        frozen_baseline = copy.deepcopy(_frozen_baseline)
        previous_state = _last_health_state_locked(project_slug)
    baseline = frozen_baseline
    if not baseline or baseline.get("project") != project_slug:
        baseline = {
            "items_per_hour": readiness["items_total"] / 24.0 if readiness["complete"] else 0.0,
            "bytes_per_hour": readiness["bytes_total"] / 24.0 if readiness["complete"] else 0.0,
            "sufficient_volume": readiness["sufficient_volume"],
        }
    recent_samples = _window_samples(
        project_slug,
        current_time - RECENT_WINDOW_SECONDS,
        current_time,
    )
    recent_items = sum(sample[1] for sample in recent_samples)
    recent_bytes = sum(sample[2] for sample in recent_samples)
    recent_coverage = _coverage(len(recent_samples), RECENT_WINDOW_SECONDS)
    item_ratio = _ratio(recent_items, float(baseline.get("items_per_hour", 0.0)))
    byte_ratio = _ratio(recent_bytes, float(baseline.get("bytes_per_hour", 0.0)))
    state = _classify_state(readiness, item_ratio, byte_ratio, previous_state)
    return {
        "project": project_slug,
        "state": state,
        "baseline": readiness,
        "frozen_baseline": frozen_baseline,
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
    actor: str = "dashboard",
) -> dict[str, Any]:
    """Persist primary/fallback policy. Automation remains disabled."""
    global _policy, _frozen_baseline
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
    with _lock:
        old_primary = _policy.get("primary_project", "")
        _policy = {
            "primary_project": primary,
            "fallback_project": fallback,
            "canary_percent": normalized_percent,
            "monitor_only": True,
            "manual_failover_only": True,
            "updated_at": time.time(),
        }
        if _frozen_baseline and _frozen_baseline.get("project") != primary:
            _frozen_baseline = None
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
    if not primary_project:
        reasons.append("Configure a primary project")
    if not policy.get("fallback_project"):
        reasons.append("Configure a fallback project")
    if readiness and not readiness.get("complete"):
        reasons.append("Complete a 24-hour primary-project baseline with at least 90% coverage")
    if not get_role_assignments():
        reasons.append("Preview and save Primary canary and Fallback fleet assignments")
    return {
        "available": not reasons,
        "monitor_only": True,
        "manual_only": True,
        "reasons": reasons,
        "primary_baseline": readiness,
        "message": (
            "Manual failover is available. No automatic switching is enabled."
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
        "mode": "monitor_only",
        "automatic_switching_enabled": False,
        "policy": policy,
        "health": evaluations,
        "roles": get_role_assignments(),
        "frozen_baseline": copy.deepcopy(_frozen_baseline),
        "failover": failover_availability(),
        "recommendation": recommend_canaries(instances),
        "decision_log": get_decision_log(100),
        "thresholds": {
            "baseline_hours": 24,
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
