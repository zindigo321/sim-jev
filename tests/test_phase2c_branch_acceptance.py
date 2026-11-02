"""Persisted metric corruption and explicit physical replay acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.phase2c_experiment import fixed_state_request, policy_for
from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.branch_replay import (SAMPLING_RULE, replay_decision_branches,
                                   verify_branch_artifact)
from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionPolicySpec, DecisionRequest
from sim_jev.decision_audit import validate_metrics
from sim_jev.decision_resolver import select_from_pool
from sim_jev.planning import PlanningInput, PlanningScenario
from sim_jev.provenance import canonical_sha256
from sim_jev.weekly_policy import WeeklyCommand


ROOT = Path(__file__).resolve().parents[1]


def simulation_fixture():
    sim = WeeklySimulation(
        Config(fleet_size=2, mission_rate_per_hour=0, failure_rate_per_flight_hour=.1),
        [MissionSpec("one", 1, 2, 1, 10), MissionSpec("two", 5, 3, 1, 11)],
        policy=DecisionPolicySpec(method="heuristic-des", target_size=4, top_k=2,
                                  scenarios_per_candidate=2))
    sim.run(0)
    return sim


class BranchAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.simulation = simulation_fixture()
        cls.checkpoint = cls.simulation.to_checkpoint()
        request = fixed_state_request()
        cls.fixed_record = select_from_pool(
            request, policy_for("heuristic-des", target_size=4, top_k=2, scenarios=2),
            generate_candidates(request, 4, 20261102)).record

    def test_complete_checkpoint_rejects_negative_nonfinite_and_noninteger_metrics(self):
        mutations = [
            ("flight_hours", -1234), ("failures", -1), ("failures", 1.0), ("failures", True),
            ("repairs_started", -1), ("repairs_completed", -1),
            ("ordered_quantity", -1), ("arrived_quantity", -1),
            ("available_aircraft_hours", float("inf")), ("idle_aircraft_hours", float("nan")),
            ("spare_wait_hours", -1), ("bay_wait_hours", True),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                changed = deepcopy(self.checkpoint)
                changed["weekly_state"]["decision_records"][0]["branches"][0]["metrics_delta"][field] = value
                with self.assertRaises(ValueError):
                    WeeklySimulation.from_checkpoint(changed)

    def test_complete_checkpoint_requires_full_metric_schema_and_capacity_bounds(self):
        for mutate in (
            lambda metrics: metrics.pop("failures"),
            lambda metrics: metrics.update(unrecognized=0),
            lambda metrics: metrics.update(flight_hours=337, available_aircraft_hours=337, idle_aircraft_hours=0),
            lambda metrics: metrics.update(available_aircraft_hours=0, idle_aircraft_hours=2, flight_hours=0),
            lambda metrics: metrics.update(available_aircraft_hours=336, idle_aircraft_hours=336,
                                           flight_hours=0, spare_wait_hours=1, bay_wait_hours=0),
        ):
            changed = deepcopy(self.checkpoint)
            mutate(changed["weekly_state"]["decision_records"][0]["branches"][0]["metrics_delta"])
            with self.assertRaises(ValueError):
                WeeklySimulation.from_checkpoint(changed)

    def test_complete_checkpoint_binds_initial_metrics_and_offsets_to_public_origin(self):
        for container in ("initial", "offset"):
            for value in (-1, 999):
                with self.subTest(container=container, value=value):
                    changed = deepcopy(self.checkpoint)
                    initial = changed["weekly_state"]["decision_records"][0]["scenarios"][0]["initial_state"]
                    metrics = initial["metrics"] if container == "initial" else initial["origin"]["metric_offsets"]
                    metrics["failures"] = value
                    with self.assertRaises(ValueError):
                        WeeklySimulation.from_checkpoint(changed)

    def test_legacy_records_restore_without_protocol_migration_or_des(self):
        with patch("sim_jev.planning.PlanningScenario.evaluate", side_effect=AssertionError("no implicit DES")), \
             patch("sim_jev.jev.ChoiceAdapter.rank", side_effect=AssertionError("no HTTP")):
            restored = WeeklySimulation.from_checkpoint(deepcopy(self.checkpoint))
        self.assertEqual(restored.weekly_state["decision_records"], self.simulation.weekly_state["decision_records"])

    def test_metric_domain_does_not_impose_one_failure_per_aircraft(self):
        metrics = deepcopy(self.fixed_record["branches"][0]["metrics_delta"])
        metrics.update(failures=100, repairs_started=100, repairs_completed=100)
        validate_metrics(metrics, capacity_hours=168 * 4)

    def test_repeated_failures_in_one_aircraft_branch_are_legal_checkpoint_evidence(self):
        sim = WeeklySimulation(
            Config(fleet_size=1, mission_rate_per_hour=0, failure_rate_per_flight_hour=1,
                   initial_spares=100, repair_duration_min_hours=.01, repair_duration_max_hours=.01),
            [MissionSpec(f"repeat-{index}", 1 + index * 3, 1, 1, 10) for index in range(30)],
            policy=DecisionPolicySpec(method="heuristic-des", target_size=4, top_k=2,
                                      scenarios_per_candidate=2))
        sim.run(0)
        self.assertGreater(max(row["metrics_delta"]["failures"]
                               for row in sim.weekly_state["decision_records"][0]["branches"]), 1)
        WeeklySimulation.from_checkpoint(sim.to_checkpoint())

    def test_fixed_state_full_replay_preserves_decision_and_saves_complete_artifacts(self):
        original = deepcopy(self.fixed_record)
        with tempfile.TemporaryDirectory() as temporary, \
             patch("sim_jev.jev.ChoiceAdapter.rank", side_effect=AssertionError("HTTP forbidden")):
            folder = Path(temporary) / "physical"
            report = replay_decision_branches(original, artifact_dir=folder)
            self.assertEqual(report["audit_replay_des"], 4)
            self.assertEqual(report["audit_replay_simulated_hours"], 672)
            self.assertEqual(report["actual_http_requests"], 0)
            self.assertFalse(report["affects_selection"])
            self.assertEqual(report["decision_des_added"], 0)
            self.assertFalse(report["external_source_authentication"])
            for row in report["branches"]:
                artifact = json.loads((folder / row["artifact"]).read_text(encoding="utf-8"))
                self.assertEqual(canonical_sha256(artifact["final_state"]), row["physical_artifact_sha256"])
        self.assertEqual(original, self.fixed_record)

    def test_prespecified_closed_loop_sampling_and_zero_branch_case(self):
        report = replay_decision_branches(self.fixed_record, selection="first-per-decision")
        self.assertEqual(report["sampling_rule"], SAMPLING_RULE)
        self.assertEqual(report["audit_replay_des"], 1)
        self.assertEqual(report["branches"][0]["branch_index"], 0)
        request = DecisionRequest.from_dict(self.fixed_record["request"])
        direct = select_from_pool(request, policy_for("jev-direct"), generate_candidates(request)).record
        self.assertEqual(replay_decision_branches(direct)["audit_replay_des"], 0)

    def test_replay_rejects_digest_summary_and_hidden_origin_mismatch(self):
        for mutate in (
            lambda r: r["branches"][0].update(physical_artifact_sha256="0" * 64),
            lambda r: r["branches"][0].update(physical_artifact_sha256="f" * 64),
            lambda r: r["branches"][0]["metrics_delta"].update(failures=999),
            lambda r: r["branches"][0]["counts"].update(offered=float(r["branches"][0]["counts"]["offered"])),
            lambda r: r["scenarios"][0]["initial_state"]["aircraft"][0].update(state="idle"),
        ):
            changed = deepcopy(self.fixed_record)
            mutate(changed)
            with self.assertRaises(ValueError):
                replay_decision_branches(changed)

    def test_checkpoint_digest_format_is_not_claimed_as_physical_verification(self):
        checkpoint = deepcopy(self.checkpoint)
        checkpoint["weekly_state"]["decision_records"][0]["branches"][0]["physical_artifact_sha256"] = "f" * 64
        restored = WeeklySimulation.from_checkpoint(checkpoint)
        with self.assertRaisesRegex(ValueError, "physical artifact digest mismatch"):
            replay_decision_branches(restored.weekly_state["decision_records"][0])

    def test_full_artifact_rejects_physical_tampering_even_when_rehashed(self):
        record = self.fixed_record
        request = DecisionRequest.from_dict(record["request"])
        row = deepcopy(record["branches"][0])
        scenario = next(s for s in record["scenarios"] if s["scenario_index"] == row["scenario_index"])
        artifact = PlanningScenario(PlanningInput(request, scenario["planning_seed"], row["scenario_index"])).evaluate(
            WeeklyCommand.from_dict(row["command"]))
        artifact["final_state"]["on_hand"] += 1
        row["physical_artifact_sha256"] = canonical_sha256(artifact["final_state"])
        with self.assertRaisesRegex(ValueError, "independent physical accounting"):
            verify_branch_artifact(row, artifact, request)

    def test_new_process_checkpoint_replay_reports_separate_audit_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "checkpoint.json"
            checkpoint.write_text(json.dumps(self.checkpoint), encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/phase2c_branch_replay.py"),
                                     "--input", str(checkpoint), "--selection", "first-per-decision",
                                     "--output", str(root / "replay")], cwd=ROOT,
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((root / "replay/audit-replay.json").read_text(encoding="utf-8"))
            self.assertEqual(report["actual_http_requests"], 0)
            self.assertEqual(report["audit_replay_des"], 1)
            self.assertEqual(report["records"][0]["decision_des_added"], 0)


if __name__ == "__main__":
    unittest.main()
