"""Independent weekly semantics checks through actual engine/policy paths."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sim_jev.config import Config
from sim_jev.models import MissionSpec
from sim_jev.provenance import canonical_sha256, code_fingerprint, runtime_metadata
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand
from sim_jev.versions import WEEKLY_POLICY_VERSION, DAILY_POLICY_VERSION
from scripts.weekly_verify import audit_business_log, check_weekly_runtime, compare_simulations


ROOT = Path(__file__).resolve().parents[1]
WEEKS = (0, 168, 336, 504, 672)


def config(**overrides):
    base = Config(fleet_size=2, mission_rate_per_hour=0, failure_rate_per_flight_hour=0,
                  initial_spares=0, reorder_target=0, repair_bays=1,
                  repair_duration_min_hours=4, repair_duration_max_hours=4,
                  lead_time_min_hours=8, lead_time_max_hours=8)
    return replace(base, **overrides)


def script(accepted=None):
    accepted = {} if accepted is None else accepted
    return PolicySpec("scripted", "deterministic-test-script",
                      tuple(WeeklyCommand(point, tuple(accepted.get(point, ()))) for point in WEEKS))


class WeeklySemanticTests(unittest.TestCase):
    def assert_conservation(self, simulation):
        rows = [simulation.summary(), *simulation.summary()["cohorts"]]
        for row in rows:
            self.assertEqual(row["accepted"] + row["rejected"], row["offered"])
            self.assertEqual(row["success"] + row["failed"] + row["accepted_not_executed"]
                             + row["unfinished"], row["accepted"])
            self.assertEqual(row["pending_execution"] + row["running"], row["unfinished"])

    def test_zero_time_commitment_precedes_dispatch_and_zero_life_causal_chain(self):
        simulation = WeeklySimulation(config(fleet_size=1, initial_spares=1),
                                      [MissionSpec("zero", 0, 2, 1, 10)], initial_lifetimes=[0])
        simulation.run(0)
        kinds = [row["event_type"] for row in simulation.events]
        self.assertLess(kinds.index("weekly_offer"), kinds.index("weekly_command"))
        self.assertLess(kinds.index("weekly_command"), kinds.index("daily_command"))
        self.assertLess(kinds.index("daily_command"), kinds.index("mission_start"))
        self.assertLess(kinds.index("mission_start"), kinds.index("failure"))
        self.assertEqual(simulation.missions["zero"].status, "failed")
        self.assertEqual(simulation.summary()["failed"], 1)
        self.assertTrue(simulation.to_checkpoint())
        self.assert_conservation(simulation)

    def test_week_boundary_drains_new_zero_repair_consequences_before_command(self):
        simulation = WeeklySimulation(config(fleet_size=1, reorder_target=1,
                                              repair_duration_min_hours=0, repair_duration_max_hours=0,
                                              lead_time_min_hours=0, lead_time_max_hours=0),
                                      [MissionSpec("old", 167, 1, 1, 1),
                                       MissionSpec("new", 168, 1, 1, 1)], initial_lifetimes=[1])
        simulation.run(168)
        kinds = [row["event_type"] for row in simulation.events if row["time"] == 168]
        self.assertLess(kinds.index("failure"), kinds.index("repair_start"))
        self.assertLess(kinds.index("repair_start"), kinds.index("repair_complete"))
        self.assertLess(kinds.index("repair_complete"), kinds.index("weekly_command"))
        self.assertLess(kinds.index("weekly_command"), kinds.index("daily_command"))
        self.assertLess(kinds.index("daily_command"), kinds.index("mission_start"))
        self.assertEqual(simulation.missions["old"].status, "failed")
        self.assertEqual(simulation.missions["new"].status, "running")
        self.assertEqual(simulation.metrics.repairs_completed, 1)
        self.assertTrue(audit_business_log(simulation)["passed"])

    def test_float_adjacent_boundaries_and_terminal_task_are_not_merged(self):
        before, after = math.nextafter(168.0, -math.inf), math.nextafter(168.0, math.inf)
        missions = [MissionSpec("before", before, 1, 1, 1), MissionSpec("exact", 168, 1, 1, 1),
                    MissionSpec("after", after, 1, 1, 1), MissionSpec("last", 672, 1, 1, 1),
                    MissionSpec("ends-at-terminal", 719, 1, 1, 1), MissionSpec("terminal", 720, 1, 1, 1)]
        simulation = WeeklySimulation(config(fleet_size=4), missions)
        simulation.run(0)
        self.assertEqual(simulation.weekly_state["cohorts"]["0"]["offered_ids"], ["before"])
        simulation.run(before)
        self.assertEqual(simulation.missions["before"].status, "running")
        self.assertNotIn("exact", simulation.weekly_state["mission_cohort"])
        simulation.run(168)
        self.assertEqual(simulation.weekly_state["mission_cohort"]["before"], "0")
        self.assertEqual(simulation.weekly_state["mission_cohort"]["exact"], "168")
        self.assertEqual(simulation.weekly_state["mission_cohort"]["after"], "168")
        self.assertEqual(simulation.missions["after"].status, "pending")
        simulation.run(after)
        self.assertEqual(simulation.missions["after"].status, "running")
        simulation.run(720)
        self.assertEqual(simulation.weekly_state["mission_cohort"]["last"], "672")
        self.assertNotIn("terminal", simulation.weekly_state["mission_cohort"])
        self.assertEqual(simulation.missions["terminal"].status, "pending")
        self.assertEqual(simulation.missions["ends-at-terminal"].status, "success")
        self.assertEqual(simulation.summary()["offered"], 5)
        self.assertEqual(simulation.summary()["success"], 5)
        self.assertNotIn(720, simulation.weekly_state["processed_day_points"])
        self.assertNotIn(720, simulation.weekly_state["processed_week_points"])
        self.assertEqual(sum(row["event_type"] == "terminal_boundary" for row in simulation.events), 1)
        count = len(simulation.events)
        simulation.run(720)
        self.assertEqual(len(simulation.events), count)
        self.assertTrue(audit_business_log(simulation)["passed"])

    def test_cross_week_success_updates_original_cohort_only_at_actual_completion(self):
        simulation = WeeklySimulation(config(fleet_size=1), [MissionSpec("cross", 167, 2, 1, 7)])
        simulation.run(168)
        first = simulation.summary()["cohorts"][0]
        self.assertEqual((first["success"], first["unfinished"]), (0, 1))
        self.assertEqual(simulation.weekly_state["closed_cohort_snapshots"][0]["counts"]["success"], 0)
        simulation.run(169)
        first = simulation.summary()["cohorts"][0]
        self.assertEqual((first["success"], first["unfinished"]), (1, 0))
        self.assertEqual(simulation.summary()["cohorts"][1]["success"], 0)
        self.assertEqual(simulation.weekly_state["closed_cohort_snapshots"][0]["counts"]["success"], 0)
        simulation.run()
        self.assertEqual(simulation.summary()["success"], 1)
        self.assertEqual(simulation.summary()["earned_value"], 7)
        self.assertEqual(simulation.summary()["window_metrics"]["0"]["flight_hours"], 1)
        self.assertEqual(simulation.summary()["window_metrics"]["168"]["flight_hours"], 1)
        self.assert_conservation(simulation)

    def test_cross_week_running_commitment_consumes_next_window_capacity(self):
        simulation = WeeklySimulation(config(fleet_size=1),
                                      [MissionSpec("cross", 167, 3, 1, 1),
                                       MissionSpec("blocked", 168, 1, 1, 100)])
        simulation.run(168)
        self.assertEqual(simulation.weekly_state["cohorts"]["0"]["accepted_ids"], ["cross"])
        self.assertEqual(simulation.weekly_state["cohorts"]["168"]["accepted_ids"], [])
        self.assertEqual(simulation.weekly_state["cohorts"]["168"]["rejected_ids"], ["blocked"])
        self.assertEqual(simulation.missions["cross"].status, "running")
        self.assertEqual(simulation.weekly_state["weekly_commands"][1]["plan_usage"]["aircraft_hours"], 2)
        self.assert_conservation(simulation)

    def test_reject_all_keeps_offered_denominators_and_no_aircraft_exposure(self):
        simulation = WeeklySimulation(config(), [MissionSpec("A", 1, 2, 1, 10)], policy=script())
        simulation.run()
        summary = simulation.summary()
        self.assertEqual((summary["offered"], summary["accepted"], summary["rejected"]), (1, 0, 1))
        self.assertEqual(summary["DSR"], 0)
        self.assertEqual(summary["RCR"], 0)
        self.assertIsNone(summary["ASR"])
        self.assertFalse(summary["validity"]["ASR"])
        self.assertEqual(summary["flight_hours"], 0)
        self.assertTrue(audit_business_log(simulation)["passed"])

    def test_zero_tasks_and_zero_value_have_explicit_null_validity(self):
        empty = WeeklySimulation(config(), [])
        empty.run()
        for name in ("DSR", "RCR", "ASR", "acceptance_rate"):
            self.assertIsNone(empty.summary()[name])
            self.assertFalse(empty.summary()["validity"][name])
        zero = WeeklySimulation(config(), [MissionSpec("zero", 1, 2, 1, 0)])
        zero.run()
        self.assertIsNone(zero.summary()["RCR"])
        self.assertFalse(zero.summary()["validity"]["RCR"])
        self.assertEqual(zero.summary()["DSR"], 1)
        self.assertEqual(zero.summary()["ASR"], 1)

    def test_nominal_acceptance_can_fail_to_dispatch_during_real_repair(self):
        simulation = WeeklySimulation(config(fleet_size=1, initial_spares=1,
                                              repair_duration_min_hours=12, repair_duration_max_hours=12),
                                      [MissionSpec("fails", 0, 2, 1, 1),
                                       MissionSpec("no-aircraft", 7, 1, 1, 2)], initial_lifetimes=[1])
        simulation.run(8)
        summary = simulation.summary()
        self.assertEqual(summary["accepted"], 2)
        self.assertEqual((summary["failed"], summary["accepted_not_executed"]), (1, 1))
        self.assertEqual(simulation.weekly_state["cohorts"]["0"]["accepted_ids"], ["fails", "no-aircraft"])
        self.assert_conservation(simulation)

    def test_same_day_disjoint_tasks_reuse_aircraft_and_exact_failure_end_tie_fails(self):
        missions = [MissionSpec("A", 1, 2, 1, 1), MissionSpec("B", 3, 2, 1, 1)]
        simulation = WeeklySimulation(config(fleet_size=1), missions)
        simulation.run(5)
        self.assertEqual(simulation.summary()["success"], 2)
        self.assertEqual(simulation.missions["A"].assigned_aircraft, ["A001"])
        self.assertEqual(simulation.missions["B"].assigned_aircraft, ["A001"])
        self.assertEqual(simulation.summary()["flight_hours"], 4)
        tied = WeeklySimulation(config(fleet_size=1), [MissionSpec("tie", 718, 2, 1, 3)], initial_lifetimes=[2])
        tied.run()
        self.assertEqual(tied.missions["tie"].status, "failed")
        self.assertEqual(tied.summary()["success"], 0)
        self.assertEqual(tied.summary()["failed"], 1)
        self.assertTrue(any(row["event_type"] == "failure" and row["time"] == 720 for row in tied.events))

    def test_daytime_arrival_starts_fifo_repair_and_only_one_daily_replenishment(self):
        simulation = WeeklySimulation(config(fleet_size=2, reorder_target=1),
                                      [MissionSpec("A", 0, 2, 1, 1), MissionSpec("B", 0, 2, 1, 1)],
                                      initial_lifetimes=[1, 1])
        simulation.run(8)
        self.assertEqual(simulation.repairs["R000001"].started_at, 8)
        self.assertIsNone(simulation.repairs["R000002"].started_at)
        self.assertEqual(simulation.repairs["R000001"].aircraft_id, "A001")
        self.assertEqual(simulation.repairs["R000002"].aircraft_id, "A002")
        at_eight = [row["event_type"] for row in simulation.events if row["time"] == 8]
        self.assertLess(at_eight.index("order_arrival"), at_eight.index("repair_start"))
        simulation.run()
        checks = [row["time"] for row in simulation.events if row["event_type"] == "reorder_check"]
        self.assertEqual(checks, [float(point) for point in range(0, 720, 24)])
        placed = [row["time"] for row in simulation.events if row["event_type"] == "order_placed"]
        self.assertEqual(len(placed), len(set(placed)))
        self.assertEqual(simulation.on_hand, simulation.config.initial_spares
                         + simulation.metrics.arrived_quantity - simulation.metrics.repairs_started)
        self.assertTrue(audit_business_log(simulation)["passed"])

    def test_mandatory_infeasibility_keeps_obligations_and_reports_fallback_shortfall(self):
        rule = RuleSpec(0, 168, 0, 0, "mandatory", ("A", "B"))
        simulation = WeeklySimulation(config(fleet_size=1),
                                      [MissionSpec("A", 10, 2, 1, 1), MissionSpec("B", 10, 2, 1, 2)],
                                      rules=[rule])
        simulation.run(0)
        command = simulation.weekly_state["weekly_commands"][0]
        self.assertTrue(command["fallback"])
        self.assertEqual(command["mandatory_ids"], ["A", "B"])
        self.assertEqual(command["accepted_ids"], ["A"])
        self.assertEqual(command["missing_mandatory"], ["B"])
        self.assertEqual(command["reasons"][0]["code"], "nominal_reserve")
        self.assertEqual(simulation.summary()["mandatory_shortfall_as_of"], 2)
        self.assertIsNone(simulation.summary()["mandatory_shortfall_terminal"])
        simulation.run()
        self.assertEqual(simulation.summary()["mandatory_shortfall_terminal"], 1)
        self.assertEqual(simulation.summary()["offered"], 2)
        self.assertTrue(audit_business_log(simulation)["passed"])

    def test_reserve_and_hours_budget_use_interval_boundaries(self):
        missions = [MissionSpec("A", 1, 2, 1, 3), MissionSpec("B", 3, 2, 1, 2),
                    MissionSpec("C", 2.5, 0.25, 1, 1)]
        rule = RuleSpec(0, 168, 0, 0, "boundary", (), 1, 4)
        simulation = WeeklySimulation(config(fleet_size=2), missions, rules=[rule])
        simulation.run(0)
        command = simulation.weekly_state["weekly_commands"][0]
        self.assertEqual(command["accepted_ids"], ["A", "B"])
        self.assertEqual(command["plan_usage"], {"peak_aircraft": 1, "minimum_reserve": 1, "aircraft_hours": 4})
        tighter = WeeklySimulation(config(fleet_size=2), missions,
                                   rules=[replace(rule, max_aircraft_hours=math.nextafter(4.0, -math.inf))])
        tighter.run(0)
        self.assertEqual(tighter.weekly_state["weekly_commands"][0]["accepted_ids"], ["A"])

    def test_json_pause_and_week_boundary_recovery_does_not_repeat_commands(self):
        missions = [MissionSpec("cross", 167, 2, 1, 5), MissionSpec("late", 673, 2, 1, 10)]
        whole = WeeklySimulation(config(fleet_size=1), missions)
        whole.run()
        resumed = WeeklySimulation(config(fleet_size=1), missions)
        for stop in (0, 0.125, 168, 672, 720):
            resumed.run(stop)
            resumed = WeeklySimulation.from_checkpoint(json.loads(json.dumps(resumed.to_checkpoint(), allow_nan=False)))
        compare_simulations(whole, resumed)
        self.assertEqual(len(resumed.weekly_state["weekly_commands"]), 5)
        self.assertEqual(len(resumed.weekly_state["daily_commands"]), 30)
        self.assertTrue(audit_business_log(resumed)["passed"])

    def test_optimized_acceptance_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory(prefix="weekly-optimized-") as folder:
            output = Path(folder) / "must-not-exist"
            result = subprocess.run([sys.executable, "-O", "scripts/weekly_verify.py", "--output", str(output)],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("refuses optimized Python", result.stderr)
            self.assertFalse(output.exists())

    def test_weekly_runtime_requires_actual_versions_policy_and_expected_source(self):
        simulation = WeeklySimulation(config(), [])
        simulation.run(0)
        policy = simulation.weekly_state["policy_spec"]
        expected = code_fingerprint(ROOT)
        runtime = runtime_metadata(
            simulation.config.to_dict(), model_version=simulation.model_version,
            rng_protocol_version=simulation.rng_protocol_version,
            checkpoint_schema_version=simulation.checkpoint_schema_version,
            policy_identity={"weekly_version": WEEKLY_POLICY_VERSION, "daily_version": DAILY_POLICY_VERSION,
                             "kind": policy["kind"], "name": policy["name"],
                             "input_sha256": canonical_sha256(policy)})
        result = check_weekly_runtime(runtime, simulation, expected)
        self.assertTrue(result["metadata_valid"])
        self.assertTrue(result["manifest_self_consistent"])
        self.assertTrue(result["source_identity_verified"])
        for field, value in (("schema_version", True), ("model_version", "synthetic-single-lru-v1")):
            damaged = json.loads(json.dumps(runtime))
            damaged[field] = value
            with self.subTest(field=field), self.assertRaises(AssertionError):
                check_weekly_runtime(damaged, simulation, expected)
        damaged = json.loads(json.dumps(runtime))
        damaged["code_fingerprint"]["files"][0]["sha256"] = "0" * 64
        damaged["code_fingerprint"]["sha256"] = canonical_sha256(damaged["code_fingerprint"]["files"])
        with self.assertRaisesRegex(AssertionError, "Source changed"):
            check_weekly_runtime(damaged, simulation, expected)
        self.assertFalse(check_weekly_runtime(damaged, simulation, expected,
                                             require_identity=False)["source_identity_verified"])


if __name__ == "__main__":
    unittest.main()
