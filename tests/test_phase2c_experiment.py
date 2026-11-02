"""Paired inputs, true run artifacts, explicit stub evidence and pilot budgets."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.phase2c_experiment import (METHODS, aggregate_rows, build_case, fixed_comparison,
                                       fixed_state_request, policy_for, run_case)
from sim_jev.candidates import generate_candidates


ROOT = Path(__file__).resolve().parents[1]


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sim-jev-pilot-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_methods_and_environment_seeds_preserve_exact_offers_and_obligations(self):
        for scenario in ("balanced", "scarce"):
            reference = build_case(scenario, 0, policy_for("mandatory-first"))
            self.assertEqual(len(reference._input_missions), 45)
            for seed in (0, 4):
                for method in METHODS:
                    candidate = build_case(scenario, seed, policy_for(method))
                    self.assertEqual(candidate._input_missions, reference._input_missions)
                    self.assertEqual(candidate.weekly_state["rules_input"], reference.weekly_state["rules_input"])
                    self.assertEqual(candidate.keyed_rng.scenario_id, reference.keyed_rng.scenario_id)
                    self.assertEqual(candidate.config.seed, seed)

    def test_real_small_closed_loop_exports_and_independently_audits_every_branch(self):
        row, decisions = run_case("balanced", 0, "heuristic-des", self.folder / "run",
                                  target_size=4, top_k=2, scenarios=1)
        self.assertEqual(row["offered"], 45)
        self.assertEqual(row["decision_count"], 5)
        self.assertEqual(row["actual_des"], 10)
        self.assertEqual(row["independent_branch_audits"], 10)
        self.assertEqual(row["simulated_hours"], 1440)
        self.assertEqual(row["actual_http_requests"], 0)
        self.assertEqual(sum(item["actual_success"] for item in decisions), row["success"])
        self.assertEqual(row["DSR"], row["success"] / row["offered"])
        for name in ("events.csv", "checkpoint.json", "weekly-ledger.json", "decisions.json",
                     "decisions.csv", "independent-business-audit.json", "api-audit.json"):
            self.assertTrue((self.folder / "run" / name).is_file(), name)
        with self.assertRaisesRegex(ValueError, "not empty"):
            run_case("balanced", 0, "heuristic-des", self.folder / "run")
        second = deepcopy(row)
        second.update(seed=1, offered=100, accepted=40, success=1, offered_value=1000, earned_value=2)
        aggregate = aggregate_rows([row, second])[0]
        self.assertEqual(aggregate["DSR"], (row["success"] + 1) / (row["offered"] + 100))
        self.assertEqual(aggregate["RCR"], (row["earned_value"] + 2) / (row["offered_value"] + 1000))

    def test_artificial_direct_choice_is_never_reported_as_valid_jev(self):
        row, _ = run_case("scarce", 1, "jev-direct", self.folder / "stub",
                          target_size=4, top_k=2, scenarios=1)
        self.assertEqual(row["actual_des"], 0)
        self.assertEqual(row["actual_http_requests"], 0)
        self.assertEqual(row["valid_jev_decisions"], 0)
        self.assertEqual(row["api_valid_rate"], 0)
        self.assertEqual(row["evidence_role"], "pipeline_only_stub")
        self.assertGreater(row["valid_response_count"], 0)

    def test_fixed_request_contains_difficult_states_and_shared_frozen_comparison(self):
        request = fixed_state_request()
        states = {row["state"] for row in request.observation.aircraft}
        self.assertTrue({"repairing", "waiting_repair", "flying"} <= states)
        self.assertEqual(request.observation.inventory["on_hand"], 0)
        self.assertEqual(request.context.active_rule.mandatory_ids, ("MAND",))
        pool = generate_candidates(request)
        self.assertEqual(len(pool), 16)
        summary = fixed_comparison(self.folder / "fixed", target_size=4, top_k=2, scenarios=1)
        self.assertEqual(summary["actual_des"], 8)
        self.assertEqual(summary["independent_branch_audits"], 8)
        self.assertTrue(summary["presentation_sensitivity"]["client_reversed_input_payload_identical"])
        identities = []
        for method in METHODS[1:]:
            record = json.loads((self.folder / "fixed" / f"{method}.json").read_text(encoding="utf-8"))
            identities.append((record["request"]["request_id"], record["pool_sha256"]))
        self.assertEqual(len(set(identities)), 1)

    def test_offline_pilot_cli_rejects_live_mode_before_creating_outputs(self):
        output = self.folder / "must-not-exist"
        process = subprocess.run([sys.executable, "scripts/phase2c_experiment.py", "--jev-mode", "live",
                                  "--output", str(output)], cwd=ROOT, capture_output=True,
                                 text=True, encoding="utf-8", timeout=20)
        self.assertEqual(process.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
