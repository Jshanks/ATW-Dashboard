"""Deterministic smoke tests for Project Health baseline and failover behavior.

Run from the repository root:

    python -m backend.project_health_smoke

The suite uses an isolated temporary state file and never reads or modifies the
production Project Health data volume.
"""
from __future__ import annotations

import ast
import copy
import os
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import project_health


PROJECT = "telegram"
FALLBACK = "urls"
BASE_TIME = 1_800_000_000.0


class ProjectHealthSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

        self.original_state_file = project_health.STATE_FILE
        project_health.STATE_FILE = str(
            Path(self.temporary_directory.name) / "project_health.json"
        )

        with project_health._lock:
            project_health._project_history = {}
            project_health._baseline_established = {}
            project_health._project_queue = {}
            project_health._policy = project_health._default_policy()
            project_health._policy.update(
                {
                    "monitoring_enabled": True,
                    "automatic_failover_enabled": True,
                    "primary_project": PROJECT,
                    "fallback_project": FALLBACK,
                    "minimum_baseline_hours": 6,
                    "full_baseline_hours": 24,
                    "stalled_evaluations_required": 3,
                }
            )
            project_health._role_assignments = {
                "warrior-01": "primary_canary",
                "warrior-02": "fallback_fleet",
            }
            project_health._runtime = project_health._default_runtime()
            project_health._decision_log = deque(
                maxlen=project_health.MAX_DECISION_LOG_ENTRIES
            )

        self.save_patch = patch.object(project_health, "save", return_value=None)
        self.force_save_patch = patch.object(
            project_health,
            "force_save",
            return_value=None,
        )
        self.save_patch.start()
        self.force_save_patch.start()
        self.addCleanup(self.save_patch.stop)
        self.addCleanup(self.force_save_patch.stop)
        self.addCleanup(self._restore_state_file)

    def _restore_state_file(self) -> None:
        project_health.STATE_FILE = self.original_state_file

    def _record_window(
        self,
        project_slug: str,
        end_time: float,
        hours: int,
        sample_count: int,
        items_per_sample: int = 10,
        bytes_per_sample: int = 1_000_000,
        eligible_warriors: int = 100,
    ) -> None:
        duration_seconds = hours * 60 * 60
        start_time = end_time - duration_seconds
        # Span the complete elapsed window while allowing sparse coverage.
        interval = (duration_seconds - project_health.SAMPLE_INTERVAL_SECONDS) / max(
            1,
            sample_count - 1,
        )
        for sample_index in range(sample_count):
            project_health.record_project_activity(
                project_slug,
                items_per_sample,
                bytes_per_sample,
                eligible_warriors=eligible_warriors,
                timestamp=start_time + sample_index * interval,
            )

    def _establish_minimum_baseline(self, established_at: float = BASE_TIME) -> dict:
        expected_samples = 6 * 60
        required_samples = int(
            expected_samples * project_health.MINIMUM_BASELINE_COVERAGE
        )
        self._record_window(
            PROJECT,
            established_at,
            hours=6,
            sample_count=required_samples,
        )
        readiness = project_health.get_baseline_readiness(
            PROJECT,
            now=established_at,
        )
        self.assertTrue(readiness["established"])
        return readiness

    def test_minimum_baseline_establishes_at_75_percent(self) -> None:
        readiness = self._establish_minimum_baseline()

        self.assertEqual(project_health.MINIMUM_BASELINE_COVERAGE, 0.75)
        self.assertEqual(readiness["phase"], "established")
        self.assertEqual(readiness["coverage_percent"], 75.0)
        self.assertEqual(readiness["sample_count"], 270)
        self.assertEqual(readiness["required_sample_count"], 270)
        self.assertEqual(readiness["missing_sample_count"], 90)
        self.assertIsNotNone(readiness["minimum_established_at"])

    def test_establishment_survives_later_coverage_reduction(self) -> None:
        first = self._establish_minimum_baseline()
        established_at = first["minimum_established_at"]

        later = project_health.get_baseline_readiness(
            PROJECT,
            now=BASE_TIME + 3 * 60 * 60,
        )

        self.assertTrue(later["established"])
        self.assertEqual(later["minimum_established_at"], established_at)
        self.assertLess(later["coverage_percent"], 75.0)

    def test_todo_unavailable_is_neutral(self) -> None:
        queue = project_health.update_project_queue(
            PROJECT,
            None,
            updated_at=BASE_TIME,
            stale=False,
        )

        self.assertFalse(queue["available"])
        self.assertIsNone(queue["todo_items"])

    def test_zero_todo_classifies_queue_exhausted(self) -> None:
        self._establish_minimum_baseline()
        project_health.update_project_queue(
            PROJECT,
            0,
            updated_at=BASE_TIME,
            stale=False,
        )

        evaluation = project_health.evaluate_project_health(
            PROJECT,
            now=BASE_TIME,
        )

        self.assertEqual(evaluation["state"], "Queue exhausted")
        self.assertEqual(evaluation["decision_reason"], "queue_exhausted")
        self.assertEqual(evaluation["queue"]["todo_items"], 0)

    def test_low_runway_uses_recent_completion_rate(self) -> None:
        self._establish_minimum_baseline(established_at=BASE_TIME - 60 * 60)
        for sample_index in range(60):
            project_health.record_project_activity(
                PROJECT,
                1_000,
                100_000_000,
                eligible_warriors=100,
                timestamp=BASE_TIME - (59 - sample_index) * 60,
            )
        # Ten items/minute with 200 remaining gives a 20-minute runway.
        project_health.update_project_queue(
            PROJECT,
            200,
            updated_at=BASE_TIME,
            stale=False,
        )

        evaluation = project_health.evaluate_project_health(
            PROJECT,
            now=BASE_TIME,
        )

        self.assertEqual(evaluation["state"], "Queue nearly exhausted")
        self.assertEqual(evaluation["decision_reason"], "queue_low")
        self.assertAlmostEqual(evaluation["queue"]["runway_minutes"], 20.0)

    def test_duplicate_observation_does_not_increment_confirmation(self) -> None:
        self._establish_minimum_baseline()
        project_health.update_project_queue(
            PROJECT,
            0,
            updated_at=BASE_TIME,
            stale=False,
        )
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)

        with patch.object(
            project_health,
            "automatic_failover_prerequisites",
            return_value={"available": True, "reasons": []},
        ):
            first = project_health.register_automatic_evaluation(
                copy.deepcopy(evaluation),
                now=BASE_TIME,
            )
            duplicate = project_health.register_automatic_evaluation(
                copy.deepcopy(evaluation),
                now=BASE_TIME + 60,
            )

        self.assertTrue(first["new_sample"])
        self.assertFalse(duplicate["new_sample"])
        self.assertEqual(
            duplicate["runtime"]["consecutive_stalled_evaluations"],
            1,
        )

    def test_three_new_five_minute_observations_trigger_failover(self) -> None:
        self._establish_minimum_baseline()

        with patch.object(
            project_health,
            "automatic_failover_prerequisites",
            return_value={"available": True, "reasons": []},
        ):
            decision = None
            for evaluation_number in range(1, 4):
                observation_time = BASE_TIME + evaluation_number * 5 * 60
                project_health.update_project_queue(
                    PROJECT,
                    0,
                    updated_at=observation_time,
                    stale=False,
                )
                evaluation = project_health.evaluate_project_health(
                    PROJECT,
                    now=observation_time,
                )
                decision = project_health.register_automatic_evaluation(
                    evaluation,
                    now=observation_time,
                )
                self.assertEqual(
                    decision["runtime"]["consecutive_stalled_evaluations"],
                    evaluation_number,
                )

        self.assertIsNotNone(decision)
        self.assertTrue(decision["trigger"])
        self.assertEqual(decision["qualifying_reason"], "queue_exhausted")

    def test_status_serializes_new_thresholds_and_evidence(self) -> None:
        self._establish_minimum_baseline()
        project_health.update_project_queue(
            PROJECT,
            40_720_000,
            updated_at=BASE_TIME,
            stale=False,
        )

        with patch.object(project_health.time, "time", return_value=BASE_TIME):
            status = project_health.get_status(())

        self.assertEqual(status["thresholds"]["minimum_coverage_percent"], 75.0)
        self.assertEqual(status["thresholds"]["evaluation_interval_seconds"], 300)
        self.assertEqual(
            status["thresholds"]["maximum_current_sample_age_seconds"],
            600,
        )
        self.assertEqual(status["thresholds"]["low_queue_runway_minutes"], 30.0)
        self.assertIn(PROJECT, status["health"])
        self.assertEqual(
            status["health"][PROJECT]["queue"]["todo_items"],
            40_720_000,
        )


    def test_normalization_preserves_rate_when_fleet_size_halves(self) -> None:
        self._record_window(
            PROJECT,
            BASE_TIME - 60 * 60,
            hours=5,
            sample_count=225,
            items_per_sample=1_000,
            bytes_per_sample=100_000_000,
            eligible_warriors=100,
        )
        self._record_window(
            PROJECT,
            BASE_TIME,
            hours=1,
            sample_count=45,
            items_per_sample=500,
            bytes_per_sample=50_000_000,
            eligible_warriors=50,
        )
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        self.assertAlmostEqual(evaluation["item_activity_ratio"], 1.0, delta=0.25)
        self.assertAlmostEqual(evaluation["byte_activity_ratio"], 1.0, delta=0.25)
        self.assertAlmostEqual(
            evaluation["recent"]["average_eligible_warriors"],
            50.0,
            delta=0.1,
        )
        self.assertEqual(evaluation["recent"]["normalization"], "per_eligible_warrior")

    def test_genuine_per_warrior_decline_remains_stalled(self) -> None:
        self._record_window(
            PROJECT,
            BASE_TIME - 60 * 60,
            hours=5,
            sample_count=225,
            items_per_sample=1_000,
            bytes_per_sample=100_000_000,
            eligible_warriors=100,
        )
        self._record_window(
            PROJECT,
            BASE_TIME,
            hours=1,
            sample_count=45,
            items_per_sample=100,
            bytes_per_sample=10_000_000,
            eligible_warriors=50,
        )
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        self.assertLess(evaluation["item_activity_ratio"], project_health.STALLED_RATIO)
        self.assertLess(evaluation["byte_activity_ratio"], project_health.STALLED_RATIO)
        self.assertEqual(evaluation["decision_reason"], "throughput_stalled")

    def test_legacy_samples_without_denominator_are_not_loaded(self) -> None:
        Path(project_health.STATE_FILE).write_text(
            '{"version":3,"project_history":{"telegram":[[1800000000,10,1000]]}}',
            encoding="utf-8",
        )
        project_health.load()
        history = project_health.get_project_history(PROJECT)
        self.assertEqual(history["projects"][PROJECT], [])

    def test_fleet_placement_uses_actual_project(self) -> None:
        instances = (
            SimpleNamespace(
                name="warrior-02", project_slug=PROJECT,
                connection_state="online", restarting=False,
                model_dump=lambda: {
                    "name": "warrior-02", "project_slug": PROJECT,
                    "connection_state": "online", "restarting": False,
                },
            ),
            SimpleNamespace(
                name="warrior-03", project_slug=FALLBACK,
                connection_state="online", restarting=False,
                model_dump=lambda: {
                    "name": "warrior-03", "project_slug": FALLBACK,
                    "connection_state": "online", "restarting": False,
                },
            ),
            SimpleNamespace(
                name="warrior-04", project_slug="usgovernment",
                connection_state="online", restarting=False,
                model_dump=lambda: {
                    "name": "warrior-04", "project_slug": "usgovernment",
                    "connection_state": "online", "restarting": False,
                },
            ),
            SimpleNamespace(
                name="warrior-05", project_slug="",
                connection_state="online", restarting=False,
                model_dump=lambda: {
                    "name": "warrior-05", "project_slug": "",
                    "connection_state": "online", "restarting": False,
                },
            ),
        )
        with project_health._lock:
            project_health._role_assignments.update({
                "warrior-03": "fallback_fleet",
                "warrior-04": "fallback_fleet",
                "warrior-05": "fallback_fleet",
            })
        placement = project_health.get_fleet_placement(instances)
        self.assertEqual(
            placement["counts"],
            {"primary": 1, "secondary": 1, "other": 1, "unknown": 1},
        )
        classifications = {
            item["name"]: item for item in placement["instances"]
        }
        self.assertTrue(classifications["warrior-02"]["switchable"])
        self.assertTrue(classifications["warrior-03"]["switchable"])
        self.assertFalse(classifications["warrior-04"]["switchable"])
        self.assertFalse(classifications["warrior-05"]["switchable"])


    def test_insufficient_volume_displays_building_baseline(self) -> None:
        self._record_window(
            PROJECT,
            BASE_TIME - 5 * 60 * 60,
            hours=6,
            sample_count=270,
            items_per_sample=0,
            bytes_per_sample=0,
        )
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        self.assertEqual(evaluation["state"], "Building baseline")
        self.assertNotEqual(evaluation.get("health_state"), "Insufficient volume")

    def test_operator_status_prioritizes_failover_cooldown(self) -> None:
        self._establish_minimum_baseline()
        with project_health._lock:
            project_health._runtime["cooldown_until"] = BASE_TIME + 3600
            project_health._runtime["consecutive_stalled_evaluations"] = 2
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        self.assertEqual(evaluation["state"], "Failover cooldown")

    def test_operator_status_shows_stalled_confirmation_progress(self) -> None:
        self._establish_minimum_baseline()
        with project_health._lock:
            project_health._runtime["consecutive_stalled_evaluations"] = 2
            project_health._runtime["cooldown_until"] = None
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        self.assertEqual(evaluation["state"], "Evaluating stalled project 2/3")

    def test_recovery_requires_cooldown_activity_and_queue_runway(self) -> None:
        self._establish_minimum_baseline()
        with project_health._lock:
            project_health._policy["monitoring_enabled"] = True
            project_health._policy["automatic_failover_enabled"] = True
            project_health._runtime["fleet_mode"] = "secondary"
            project_health._runtime["cooldown_until"] = BASE_TIME - 1
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        evaluation["strongest_activity_ratio"] = 0.75
        evaluation["recent"]["current"] = True
        evaluation["baseline"]["established"] = True
        evaluation["queue"] = {
            "available": True,
            "stale": False,
            "runway_minutes": 120.0,
        }
        evaluation["observation_at"] = BASE_TIME
        decision = project_health.register_automatic_recovery_evaluation(
            evaluation,
            now=BASE_TIME,
        )
        self.assertTrue(decision["qualifying"])
        self.assertFalse(decision["trigger"])
        self.assertEqual(
            decision["runtime"]["consecutive_recovered_evaluations"],
            1,
        )

    def _manual_fleet(self, *placements):
        projects = {"primary": PROJECT, "secondary": FALLBACK,
                    "other": "unrelated", "unknown": ""}
        with project_health._lock:
            project_health._role_assignments = {
                "canary": "primary_canary",
                **{f"fallback-{index}": "fallback_fleet"
                   for index in range(len(placements))},
            }
        return [
            {"name": "canary", "project_slug": PROJECT,
             "connection_state": "online"},
            *({"name": f"fallback-{index}",
               "project_slug": projects[placement],
               "connection_state": "online"}
              for index, placement in enumerate(placements)),
        ]

    def test_manual_secondary_switch_updates_mode_without_cooldown(self) -> None:
        with project_health._lock:
            project_health._runtime["cooldown_until"] = BASE_TIME - 100
            project_health._runtime["consecutive_recovered_evaluations"] = 2
            project_health._runtime["last_recovery_evaluated_sample_at"] = BASE_TIME
        result = project_health.complete_manual_fleet_switch(
            "secondary", self._manual_fleet("secondary", "secondary"))
        self.assertEqual(result["fleet_mode"], "secondary")
        runtime = project_health.get_runtime()
        self.assertEqual(runtime["cooldown_until"], BASE_TIME - 100)
        self.assertEqual(runtime["consecutive_recovered_evaluations"], 0)
        self.assertIsNone(runtime["last_recovery_evaluated_sample_at"])
        self.assertEqual(project_health.get_decision_log(1)[0]["event"],
                         "manual_fleet_mode_updated")

    def test_manual_primary_and_partial_switches(self) -> None:
        project_health.complete_manual_fleet_switch(
            "secondary", self._manual_fleet("secondary", "secondary"))
        partial = project_health.complete_manual_fleet_switch(
            "primary", self._manual_fleet("primary", "secondary"))
        self.assertEqual(partial["fleet_mode"], "mixed")
        complete = project_health.complete_manual_fleet_switch(
            "primary", self._manual_fleet("primary", "primary"))
        self.assertEqual(complete["fleet_mode"], "primary")

    def test_manual_switch_preserves_unknown_and_unrelated(self) -> None:
        result = project_health.complete_manual_fleet_switch(
            "primary", self._manual_fleet("primary", "unknown", "other"))
        self.assertEqual(result["fleet_mode"], "mixed")
        self.assertEqual(result["managed_counts"]["unknown"], 1)
        self.assertEqual(result["managed_counts"]["other"], 1)

    def test_manual_switch_enables_recovery_confirmation(self) -> None:
        self._establish_minimum_baseline()
        project_health.complete_manual_fleet_switch(
            "secondary", self._manual_fleet("secondary"))
        latest = None
        for index in range(3):
            now = BASE_TIME + index * 300
            evaluation = project_health.evaluate_project_health(PROJECT, now=now)
            evaluation["strongest_activity_ratio"] = 0.75
            evaluation["recent"]["current"] = True
            evaluation["baseline"]["established"] = True
            evaluation["queue"] = {"available": True, "stale": False,
                                   "runway_minutes": 120.0}
            evaluation["observation_at"] = now
            latest = project_health.register_automatic_recovery_evaluation(
                evaluation, now=now)
        self.assertTrue(latest["trigger"])

    def test_recovery_rejects_short_primary_runway(self) -> None:
        self._establish_minimum_baseline()
        with project_health._lock:
            project_health._policy["monitoring_enabled"] = True
            project_health._policy["automatic_failover_enabled"] = True
            project_health._runtime["fleet_mode"] = "secondary"
            project_health._runtime["cooldown_until"] = BASE_TIME - 1
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        evaluation["strongest_activity_ratio"] = 0.75
        evaluation["recent"]["current"] = True
        evaluation["baseline"]["established"] = True
        evaluation["queue"] = {
            "available": True,
            "stale": False,
            "runway_minutes": 59.0,
        }
        evaluation["observation_at"] = BASE_TIME
        decision = project_health.register_automatic_recovery_evaluation(
            evaluation,
            now=BASE_TIME,
        )
        self.assertFalse(decision["queue_sufficient"])
        self.assertFalse(decision["trigger"])

    def test_three_recovery_observations_trigger_failback(self) -> None:
        self._establish_minimum_baseline()
        with project_health._lock:
            project_health._policy["monitoring_enabled"] = True
            project_health._policy["automatic_failover_enabled"] = True
            project_health._runtime["fleet_mode"] = "secondary"
            project_health._runtime["cooldown_until"] = BASE_TIME - 1
        latest = None
        for index in range(3):
            observation = BASE_TIME + index * 300
            evaluation = project_health.evaluate_project_health(PROJECT, now=observation)
            evaluation["strongest_activity_ratio"] = 0.75
            evaluation["recent"]["current"] = True
            evaluation["baseline"]["established"] = True
            evaluation["queue"] = {
                "available": True,
                "stale": False,
                "runway_minutes": 120.0,
            }
            evaluation["observation_at"] = observation
            latest = project_health.register_automatic_recovery_evaluation(
                evaluation,
                now=observation,
            )
        self.assertIsNotNone(latest)
        self.assertTrue(latest["trigger"])
        self.assertEqual(
            latest["runtime"]["automatic_failover_state"],
            "failback_in_progress",
        )

    def test_cancel_failback_resets_runtime_and_preserves_history(self) -> None:
        with project_health._lock:
            project_health._runtime.update({
                "fleet_mode": "secondary",
                "failover_in_progress": True,
                "consecutive_recovered_evaluations": 3,
                "automatic_failover_state": "failback_in_progress",
                "cooldown_until": BASE_TIME + 3600,
                "last_automatic_failover_at": BASE_TIME - 3600,
                "last_automatic_transition": "failover",
            })
        project_health.cancel_automatic_failback(
            "Final primary queue validation failed",
            {"runway_minutes": 12, "queue_stale": False},
        )
        runtime = project_health.get_runtime()
        self.assertFalse(runtime["failover_in_progress"])
        self.assertEqual(runtime["consecutive_recovered_evaluations"], 0)
        self.assertEqual(runtime["automatic_failover_state"], "waiting_for_primary_recovery")
        self.assertEqual(runtime["fleet_mode"], "secondary")
        self.assertEqual(runtime["cooldown_until"], BASE_TIME + 3600)
        self.assertEqual(runtime["last_automatic_failover_at"], BASE_TIME - 3600)
        self.assertEqual(runtime["last_automatic_transition"], "failover")
        entry = project_health.get_decision_log(1)[0]
        self.assertEqual(entry["event"], "automatic_failback_cancelled")
        self.assertEqual(entry["details"]["evidence"]["runway_minutes"], 12)

    def test_cancel_failback_allows_new_recovery_confirmation(self) -> None:
        self._establish_minimum_baseline()
        with project_health._lock:
            project_health._runtime["fleet_mode"] = "secondary"
            project_health._runtime["failover_in_progress"] = True
            project_health._runtime["consecutive_recovered_evaluations"] = 3
        project_health.cancel_automatic_failback("Queue stale", {"queue_stale": True})
        evaluation = project_health.evaluate_project_health(PROJECT, now=BASE_TIME)
        evaluation["strongest_activity_ratio"] = 0.75
        evaluation["recent"]["current"] = True
        evaluation["baseline"]["established"] = True
        evaluation["queue"] = {"available": True, "stale": False, "runway_minutes": 120}
        evaluation["observation_at"] = BASE_TIME
        decision = project_health.register_automatic_recovery_evaluation(evaluation, now=BASE_TIME)
        self.assertTrue(decision["qualifying"])
        self.assertFalse(decision["trigger"])
        self.assertEqual(decision["runtime"]["consecutive_recovered_evaluations"], 1)

    def test_complete_failback_restarts_shared_cooldown(self) -> None:
        with project_health._lock:
            project_health._policy["cooldown_hours"] = 2.0
        before = time.time()
        project_health.complete_automatic_failback(5, 0, {})
        runtime = project_health.get_runtime()
        self.assertEqual(runtime["fleet_mode"], "primary")
        self.assertEqual(runtime["last_automatic_transition"], "failback")
        self.assertGreaterEqual(runtime["cooldown_until"], before + 7199)


class ManualFailoverContractTests(unittest.TestCase):
    """Source-level contract checks that do not require FastAPI or Pydantic."""

    @classmethod
    def setUpClass(cls) -> None:
        backend_directory = Path(__file__).resolve().parent
        cls.models_source = (backend_directory / "models.py").read_text(
            encoding="utf-8"
        )
        cls.main_source = (backend_directory / "main.py").read_text(
            encoding="utf-8"
        )
        cls.models_tree = ast.parse(cls.models_source)
        cls.main_tree = ast.parse(cls.main_source)

    def setUp(self) -> None:
        with project_health._lock:
            project_health._policy = project_health._default_policy()
            project_health._policy.update({
                "primary_project": PROJECT,
                "fallback_project": FALLBACK,
                "monitoring_enabled": True,
                "automatic_failover_enabled": True,
            })
            project_health._runtime = project_health._default_runtime()
            project_health._role_assignments = {}
            project_health._decision_log = deque(
                maxlen=project_health.MAX_DECISION_LOG_ENTRIES)
        save_patch = patch.object(project_health, "force_save", return_value=None)
        save_patch.start()
        self.addCleanup(save_patch.stop)

    def _startup_reconciler(self, placements, persisted_mode="primary"):
        """Execute the actual startup helper with isolated Warrior clients."""
        helper = next(
            node for node in self.main_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_reconcile_project_health_startup_placement"
        )
        from backend.models import ConnectionState
        from types import SimpleNamespace
        from unittest.mock import Mock
        names = [f"fallback-{index}" for index in range(len(placements))]
        with project_health._lock:
            project_health._role_assignments = {
                "canary": "primary_canary",
                **{name: "fallback_fleet" for name in names},
            }
            project_health._runtime["fleet_mode"] = persisted_mode
        project_map = {"primary": PROJECT, "secondary": FALLBACK,
                       "other": "unrelated", "unknown": ""}
        clients = {}
        for name, placement in zip(names, placements):
            if placement == "missing":
                continue
            online = placement != "offline"
            status = SimpleNamespace(
                name=name,
                project_slug=project_map.get(placement, FALLBACK),
                connection_state=(ConnectionState.ONLINE if online
                                  else ConnectionState.OFFLINE),
                restarting=False,
            )
            status.model_dump = lambda status=status: {
                "name": status.name,
                "project_slug": status.project_slug,
                "connection_state": status.connection_state,
                "restarting": status.restarting,
            }
            clients[name] = SimpleNamespace(status=status)
        namespace = {
            "project_health": project_health,
            "clients": clients,
            "ConnectionState": ConnectionState,
            "_manual_failover_lock": SimpleNamespace(locked=lambda: False),
            "logger": Mock(),
        }
        isolated = ast.Module(body=[helper], type_ignores=[])
        exec(compile(isolated, "main.py", "exec"), namespace)
        return namespace["_reconcile_project_health_startup_placement"]()

    def test_startup_reconciles_secondary_fleet_with_primary_canary(self) -> None:
        self.assertTrue(self._startup_reconciler(["secondary"] * 94))
        self.assertEqual(project_health.get_runtime()["fleet_mode"], "secondary")
        self.assertEqual(project_health.get_decision_log(1)[0]["actor"],
                         "startup-reconciliation")

    def test_startup_reconciles_mixed_and_primary_fleets(self) -> None:
        self.assertTrue(self._startup_reconciler(["secondary", "primary"]))
        self.assertEqual(project_health.get_runtime()["fleet_mode"], "mixed")
        self.assertTrue(self._startup_reconciler(["primary", "primary"], "mixed"))
        self.assertEqual(project_health.get_runtime()["fleet_mode"], "primary")

    def test_startup_waits_for_missing_offline_or_unknown(self) -> None:
        for unresolved in ("missing", "offline", "unknown"):
            with self.subTest(unresolved=unresolved):
                self.assertFalse(self._startup_reconciler(
                    ["secondary", unresolved]))
                self.assertEqual(project_health.get_runtime()["fleet_mode"],
                                 "primary")

    def test_startup_noop_does_not_reset_cooldown(self) -> None:
        with project_health._lock:
            project_health._runtime["cooldown_until"] = BASE_TIME + 3600
        self.assertTrue(self._startup_reconciler(["primary", "primary"]))
        self.assertEqual(project_health.get_runtime()["cooldown_until"],
                         BASE_TIME + 3600)
        self.assertFalse(any(entry["event"] == "manual_fleet_mode_updated"
                             for entry in project_health.get_decision_log()))

    def test_startup_reconciliation_precedes_automatic_evaluation(self) -> None:
        loop = next(node for node in self.main_tree.body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_automatic_failover_loop")
        source = ast.get_source_segment(self.main_source, loop)
        self.assertLess(source.index("_reconcile_project_health_startup_placement()"),
                        source.index("register_automatic_recovery_evaluation("))

    def test_manual_failover_model_accepts_only_saved_roles(self) -> None:
        target_class = next(
            node
            for node in self.models_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "ManualFailoverTarget"
        )
        enum_values = {
            statement.targets[0].id: statement.value.value
            for statement in target_class.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Constant)
        }
        self.assertEqual(
            enum_values,
            {"PRIMARY": "primary", "SECONDARY": "secondary"},
        )

        request_class = next(
            node
            for node in self.models_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "ManualFailoverRequest"
        )
        annotated_fields = {
            statement.target.id: ast.unparse(statement.annotation)
            for statement in request_class.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
        }
        self.assertEqual(
            annotated_fields.get("target"),
            "ManualFailoverTarget",
        )
        self.assertNotIn("confirmation", annotated_fields)

    def test_manual_endpoint_resolves_primary_and_secondary_destinations(self) -> None:
        endpoint = next(
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "execute_manual_project_failover"
        )
        endpoint_source = ast.get_source_segment(self.main_source, endpoint) or ""

        required_source = (
            "target_role = request.target.value",
            'if target_role == "primary"',
            'policy.get("primary_project", "")',
            'policy.get("fallback_project", "")',
            '"target": target_role',
            '"target_project": target_project',
            'additional_allowed_projects',
            "summary = _summarize_project_switch_results(results)",
            "**summary",
        )
        for required_text in required_source:
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, endpoint_source)

        self.assertNotIn("request.confirmation", endpoint_source)
        self.assertNotIn(
            "project_health.failover_availability()",
            endpoint_source,
        )

    def test_main_records_eligible_warrior_denominator(self) -> None:
        self.assertIn(
            "def _eligible_warrior_count(project_slug: str) -> int:",
            self.main_source,
        )
        self.assertIn("eligible_warriors=eligible_warriors", self.main_source)

    def test_switch_helper_preserves_third_project_assignments(self) -> None:
        helper = next(
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "_switch_failover_instance"
        )
        helper_source = ast.get_source_segment(self.main_source, helper) or ""
        self.assertIn("additional_allowed_projects", helper_source)
        self.assertIn("current_project not in allowed_projects", helper_source)
        self.assertIn('"skipped_unrelated_project"', helper_source)
        self.assertIn('"skipped_project_unknown"', helper_source)
        self.assertIn('"already_at_target"', helper_source)

    def test_policy_response_exposes_primary_change_metadata(self) -> None:
        endpoint = next(
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "update_project_health_policy"
        )
        endpoint_source = ast.get_source_segment(self.main_source, endpoint) or ""
        self.assertIn('"primary_changed"', endpoint_source)
        self.assertIn('"previous_primary_project"', endpoint_source)
        self.assertIn('"fleet_placement"', endpoint_source)

    def test_primary_change_moves_primary_canaries(self) -> None:
        endpoint = next(
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "execute_manual_project_failover"
        )
        endpoint_source = ast.get_source_segment(self.main_source, endpoint) or ""
        self.assertIn('request.actor == "dashboard-primary-change"', endpoint_source)
        self.assertIn("operation_names.extend(", endpoint_source)
        self.assertIn('"primary_canary_results": canary_results', endpoint_source)
        self.assertIn("additional_allowed_projects", endpoint_source)

    def test_automatic_loop_evaluates_and_executes_failback(self) -> None:
        loop = next(
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "_automatic_failover_loop"
        )
        loop_source = ast.get_source_segment(self.main_source, loop) or ""
        self.assertIn(
            "project_health.register_automatic_recovery_evaluation(",
            loop_source,
        )
        self.assertIn("await _execute_automatic_failback()", loop_source)
        self.assertIn(
            "project_health.FAILBACK_MINIMUM_RUNWAY_MINUTES",
            loop_source,
        )

    def test_final_failback_uses_calculated_queue_runway(self) -> None:
        loop = next(node for node in self.main_tree.body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_automatic_failover_loop")
        source = ast.get_source_segment(self.main_source, loop) or ""
        self.assertIn('final_evaluation = project_health.evaluate_project_health(', source)
        self.assertIn('primary_queue = final_evaluation.get("queue", {})', source)
        self.assertIn('runway_minutes = primary_queue.get("runway_minutes")', source)
        self.assertNotIn('primary_queue = final_queue.get(primary_project)', source)
        self.assertIn('await _execute_automatic_failback()', source)
        self.assertIn('project_health.cancel_automatic_failback(', source)
        self.assertIn('"queue_stale": primary_queue.get("stale")', source)
        self.assertIn('"refresh_error": refresh_error', source)
        self.assertIn('logger.exception(', source)

    def test_automatic_restart_path_preserved(self) -> None:
        for name in ("_execute_automatic_failover", "_execute_automatic_failback"):
            function = next(node for node in self.main_tree.body
                            if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
            source = ast.get_source_segment(self.main_source, function) or ""
            self.assertIn('_restart_switched_project_health_warriors(batch, results)', source)

    def test_automatic_failback_targets_primary_project(self) -> None:
        endpoint = next(
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "_execute_automatic_failback"
        )
        endpoint_source = ast.get_source_segment(self.main_source, endpoint) or ""
        self.assertIn('policy["primary_project"]', endpoint_source)
        self.assertIn("project_health.complete_automatic_failback(", endpoint_source)

    def test_automatic_failover_still_targets_secondary_project(self) -> None:
        automatic_endpoint = next(
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "_execute_automatic_failover"
        )
        automatic_source = (
            ast.get_source_segment(self.main_source, automatic_endpoint) or ""
        )
        self.assertIn(
            '_switch_failover_instance(name, policy["fallback_project"], policy)',
            automatic_source,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
