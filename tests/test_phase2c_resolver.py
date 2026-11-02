"""End-to-end candidate decisions, frozen budgets and fresh-process recovery."""
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.candidates import Candidate, generate_candidates
from sim_jev.decision import DecisionPolicySpec
from sim_jev.decision_resolver import (DecisionRuntime, logical_decision_record,
                                      select_from_pool)
from sim_jev.jev import ChoiceAdapter, JevConfig
from sim_jev.weekly_policy import RuleSpec
from scripts.weekly_verify import compare_values, audit_business_log, normalized_history

ROOT = Path(__file__).resolve().parents[1]


def fixture(method="heuristic-des", **policy_options):
    config = replace(Config(), fleet_size=2, mission_rate_per_hour=0,
                     failure_rate_per_flight_hour=.02, initial_spares=1, reorder_target=2)
    missions = [MissionSpec(f"W{int(t)}-{i}", t + 1 + 3 * (i % 3), 4, 1, 10 + i)
                for t in (0., 168., 336., 504., 672.) for i in range(8)]
    return WeeklySimulation(config, missions, policy=DecisionPolicySpec(method=method, **policy_options))


def logical_checkpoint(sim):
    state = sim.to_checkpoint()
    state["history"] = normalized_history(sim)
    state["weekly_state"]["decision_records"] = [logical_decision_record(row)
        for row in state["weekly_state"]["decision_records"]]
    return state


class ResolverTests(unittest.TestCase):
    def test_same_frozen_pool_equal_actual_des_budget_and_short_final_window(self):
        sim = fixture()
        request, _ = sim._publish_request()
        pool = generate_candidates(request)
        self.assertEqual(len(pool), 16)
        observed_pool = None
        for method in ("random-des", "heuristic-des", "jev-des", "direct-des", "jev-direct"):
            with self.subTest(method=method):
                result = select_from_pool(request, DecisionPolicySpec(method=method), pool)
                row = result.record
                if observed_pool is None:
                    observed_pool = row["pool_sha256"]
                self.assertEqual(row["pool_sha256"], observed_pool)
                expected = 0 if method == "jev-direct" else 32
                self.assertEqual(row["budget"]["actual_des"], expected)
                self.assertEqual(row["budget"]["simulated_hours"], expected * 168)
                self.assertFalse(row["fallback"])
                if method.startswith("jev"):
                    self.assertFalse(row["jev"]["valid_jev"])
                    self.assertEqual(row["jev"]["mode"], "stub")
        actual = fixture("direct-des")
        actual.run(672)
        self.assertEqual(actual.weekly_state["decision_records"][-1]["budget"]["simulated_hours"], 32 * 48)
        self.assertEqual(len(actual.weekly_state["weekly_commands"]), 5)

    def test_pool_reordering_repeated_selection_same_branch_results(self):
        sim = fixture()
        request, _ = sim._publish_request()
        pool = generate_candidates(request)
        first = select_from_pool(request, DecisionPolicySpec(), pool)
        second = select_from_pool(request, DecisionPolicySpec(), list(reversed(pool)))
        self.assertEqual(logical_decision_record(first.record), logical_decision_record(second.record))

    def test_empty_singleton_and_short_pools_have_explicit_actual_budgets(self):
        sim = fixture()
        request, _ = sim._publish_request()
        pool = generate_candidates(request)
        for size, expected in ((0, 0), (1, 0), (2, 16), (3, 24)):
            for method in ("random-des", "heuristic-des", "jev-des", "direct-des"):
                with self.subTest(size=size, method=method):
                    row = select_from_pool(request, DecisionPolicySpec(method=method), pool[:size]).record
                    self.assertEqual(row["budget"]["actual_des"], expected)
                    self.assertEqual(row["budget"]["actual_M"], size)
                    if size < 2:
                        self.assertIsNotNone(row["deterministic_reason"])
                        if method == "jev-des":
                            self.assertIsNotNone(row["jev"]["api_skipped_reason"])

    def test_invalid_jev_response_is_explicit_frozen_heuristic_fallback(self):
        sim = fixture()
        request, _ = sim._publish_request()
        pool = generate_candidates(request)
        bad = ChoiceAdapter(JevConfig(), stub_response={"model": "incorrect"})
        actual = select_from_pool(request, DecisionPolicySpec(method="jev-des"), pool, DecisionRuntime(bad))
        expected = select_from_pool(request, DecisionPolicySpec(method="heuristic-des"), pool)
        self.assertEqual(actual.command, expected.command)
        self.assertTrue(actual.record["fallback"])
        self.assertFalse(actual.record["jev"]["valid_jev"])
        self.assertIn("invalid_response", actual.record["jev"]["error_code"])
        with self.assertRaises(RuntimeError):
            select_from_pool(request, DecisionPolicySpec(method="jev-des", strict_jev=True),
                             pool, DecisionRuntime(bad))

    def test_stale_public_summary_rejected_before_external_request(self):
        sim = fixture()
        request, _ = sim._publish_request()
        pool = generate_candidates(request)
        altered = deepcopy(pool[0].summary)
        altered["resources"]["inventory"]["on_hand"] += 7
        pool[0] = Candidate(pool[0].id, pool[0].command, altered)
        with self.assertRaisesRegex(ValueError, "summary"):
            select_from_pool(request, DecisionPolicySpec(method="jev-direct"), pool)

    def test_singleton_live_needs_no_runtime_or_credential(self):
        sim = fixture()
        request, _ = sim._publish_request()
        pool = generate_candidates(request)[:1]
        row = select_from_pool(request, DecisionPolicySpec(method="jev-des", jev_mode="live"), pool).record
        self.assertFalse(row["fallback"])
        self.assertEqual(row["jev"]["api_skipped_reason"], "singleton_pool")
        self.assertEqual(row["jev"]["actual_http_requests"], 0)

    def test_infeasible_mandatory_same_singleton_fallback_every_method(self):
        sim = WeeklySimulation(replace(Config(), fleet_size=1, mission_rate_per_hour=0),
            [MissionSpec("A", 1, 3, 1, 10), MissionSpec("B", 1, 3, 1, 20)],
            rules=[RuleSpec(0, 168, 0, 0, "obligations", ("A", "B"))])
        request, _ = sim._publish_request()
        pool = generate_candidates(request)
        self.assertEqual(len(pool), 1)
        for method in ("random-des", "heuristic-des", "jev-direct", "jev-des", "direct-des"):
            result = select_from_pool(request, DecisionPolicySpec(method=method), pool)
            self.assertTrue(result.command.fallback)
            self.assertEqual(result.record["budget"]["actual_des"], 0)

    def test_committed_restoration_does_not_resolve_again_and_rejects_corrupt_ledger(self):
        sim = fixture("jev-des")
        sim.run(168)
        checkpoint = sim.to_checkpoint()
        with patch("sim_jev.weekly.resolve_decision", side_effect=AssertionError("reexecuted")):
            restored = WeeklySimulation.from_checkpoint(checkpoint)
            restored.run(168)
        self.assertEqual(restored.weekly_state, sim.weekly_state)
        for mutate in (
            lambda row: row["budget"].update(actual_des=999),
            lambda row: row.update(selected_candidate_id="unknown"),
            lambda row: row["pool"][0]["summary"].update(accepted_value=999),
        ):
            corrupt = deepcopy(checkpoint)
            mutate(corrupt["weekly_state"]["decision_records"][0])
            with self.assertRaises(ValueError):
                WeeklySimulation.from_checkpoint(corrupt)

    def test_new_policy_fresh_process_resume_full_log_inputs_and_model_state(self):
        for method in ("heuristic-des", "jev-des", "jev-direct", "direct-des", "random-des"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as folder:
                whole, partial = fixture(method), fixture(method)
                whole.run()
                partial.run(180.25)
                source, output = Path(folder)/"paused.json", Path(folder)/"resumed.json"
                partial.save_checkpoint(source)
                code = ("import sys; from sim_jev import WeeklySimulation; "
                        "s=WeeklySimulation.load_checkpoint(sys.argv[1]); s.run(); s.save_checkpoint(sys.argv[2])")
                result = subprocess.run([sys.executable, "-c", code, str(source), str(output)],
                                        cwd=ROOT, text=True, capture_output=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                resumed = WeeklySimulation.load_checkpoint(output)
                self.assertEqual(whole.events, resumed.events)
                compare_values(logical_checkpoint(whole), logical_checkpoint(resumed), "checkpoint")
                self.assertEqual(whole.config, resumed.config)
                self.assertEqual(whole._input_missions, resumed._input_missions)
                self.assertEqual(whole._initial_lifetimes, resumed._initial_lifetimes)
                audit_business_log(resumed)
                # Actual timings remain recorded; no false byte-equality claim.
                self.assertTrue(all(row["execution_audit"]["end_to_end_seconds"] >= 0
                                    for row in resumed.weekly_state["decision_records"]))


if __name__ == "__main__":
    unittest.main()
