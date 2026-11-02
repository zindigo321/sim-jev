"""Corrupted decision evidence must not survive checkpoint restoration."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.decision import DecisionPolicySpec
from sim_jev.decision_audit import historical_observation, validate_record
from sim_jev.provenance import canonical_sha256
from sim_jev.weekly_policy import RuleSpec


def fixture(method="jev-des", *, until=168, jev_mode="stub"):
    c = Config(fleet_size=3, mission_rate_per_hour=0, failure_rate_per_flight_hour=0,
               initial_spares=1, reorder_target=2, repair_bays=1,
               repair_duration_min_hours=200, repair_duration_max_hours=200,
               lead_time_min_hours=200, lead_time_max_hours=200)
    missions = [MissionSpec("A", 0, 2, 1, 10), MissionSpec("B", 3, 1, 1, 5),
                MissionSpec("cross", 167, 3, 1, 15), MissionSpec("new", 168, 2, 1, 5),
                MissionSpec("later", 180, 2, 1, 8)]
    policy = DecisionPolicySpec(method=method, target_size=4, top_k=2, scenarios_per_candidate=2, jev_mode=jev_mode)
    simulation = WeeklySimulation(c, missions, initial_lifetimes=[1, 1, 1000], policy=policy,
                                  rules=[RuleSpec(0, 168, 0, 0, "mandatory", ("A", "B", "cross"))])
    simulation.run(until)
    return simulation, policy


class DecisionAuditTests(unittest.TestCase):
    def test_legal_all_methods_and_historical_repair_carry_shortage_facts(self):
        for method in ("heuristic-des", "random-des", "direct-des", "jev-direct", "jev-des"):
            with self.subTest(method=method):
                simulation, policy = fixture(method, until=720)
                for record in simulation.weekly_state["decision_records"]:
                    validate_record(record, policy, simulation)
                observation, carry = historical_observation(simulation, 168)
                self.assertEqual([row["state"] for row in observation["aircraft"]],
                                 ["repairing", "waiting_repair", "flying"])
                self.assertEqual([row["id"] for row in carry], ["cross"])
                self.assertGreater(observation["inventory"]["in_transit"], 0)
                self.assertEqual(observation["inventory"]["on_hand"], 0)
                current = next(row for row in observation["missions"] if row["id"] == "new")
                self.assertEqual(current["status"], "pending")
                self.assertIsNone(current["actual_start"])
                WeeklySimulation.from_checkpoint(simulation.to_checkpoint())

    def test_old_review_branch_score_probability_and_scenario_corruptions_rejected(self):
        simulation, policy = fixture()
        for name, mutate in (
            ("score", lambda r: r["branches"][0].update(score=-999)),
            ("probability", lambda r: r["jev"]["probabilities"].update({next(iter(r["jev"]["probabilities"])): -1})),
            ("scenario", lambda r: r["branches"][0].update(scenario_id="false-scenario")),
        ):
            with self.subTest(name=name):
                checkpoint = simulation.to_checkpoint()
                record = checkpoint["weekly_state"]["decision_records"][1]
                mutate(record)
                with self.assertRaises(ValueError):
                    validate_record(record, policy, simulation)
                with self.assertRaises(ValueError):
                    WeeklySimulation.from_checkpoint(checkpoint)

    def test_jev_protocol_and_response_fields_and_fallback_corruption_rejected(self):
        simulation, policy = fixture()
        original = simulation.weekly_state["decision_records"][1]
        mutations = [
            lambda r: r["jev"].update(request_sha256="0" * 64),
            lambda r: r["jev"].update(model="latest"),
            lambda r: r["jev"].update(template_version="future"),
            lambda r: r["jev"].update(candidate_mapping_sha256="0" * 64),
            lambda r: r["jev"].update(choice="unknown"),
            lambda r: r["jev"].update(valid_jev=True),
            lambda r: r["jev"].update(confidence=-1),
            lambda r: r["jev"].update(mode="live"),
            lambda r: r["jev"]["request"]["state"].update(template_version="changed"),
            lambda r: r["jev"]["response"].update(model="latest"),
            lambda r: r.update(fallback=True),
            lambda r: r["jev"].update(fallback_required=True),
            lambda r: r.update(ranking_ids=list(reversed(r["ranking_ids"]))),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = deepcopy(original)
                mutate(changed)
                with self.assertRaises(ValueError):
                    validate_record(changed, policy, simulation)

    def test_branch_allocation_counts_denominators_ratios_scores_and_origin_rejected(self):
        simulation, policy = fixture()
        original = simulation.weekly_state["decision_records"][1]
        mutations = [
            lambda r: r["branches"][0].update(scenario_index=99),
            lambda r: r["branches"][0].update(duration_hours=1),
            lambda r: r["branches"][0].update(window_end=720),
            lambda r: r["branches"][0].update(legal_input_hash="0" * 64),
            lambda r: r["branches"][0]["counts"].update(offered_value=999),
            lambda r: r["branches"][0]["counts"].update(accepted=999),
            lambda r: r["branches"][0]["counts"].update(success=999),
            lambda r: r["branches"][0]["counts"].update(RCR=-1),
            lambda r: r["branches"][0]["counts"]["validity"].update(DSR=False),
            lambda r: r["branches"][0]["score_components"].update(DSR=-1),
            lambda r: r["branches"][0].update(unresolved=999),
            lambda r: r["scenarios"][0].update(scenario_id="incorrect"),
            lambda r: r["scenarios"][0]["initial_state"]["origin"].update(as_of=0),
            lambda r: r["scenarios"][0]["initial_state"]["random_manifest"].update(domain="execution"),
            lambda r: r["budget"].update(B=99),
            lambda r: r.update(branches=list(reversed(r["branches"]))),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = deepcopy(original)
                mutate(changed)
                with self.assertRaises(ValueError):
                    validate_record(changed, policy, simulation)

    def test_historical_public_fields_are_bound_even_after_rehashing_request(self):
        simulation, policy = fixture(until=720)
        original = simulation.weekly_state["decision_records"][1]
        mutations = [
            lambda o: o["aircraft"][0].update(state="idle"),
            lambda o: o["aircraft"][2].update(flight_hours=999),
            lambda o: o["missions"][0].update(value=999),
            lambda o: o["missions"][-2].update(status="running", actual_start=168),
            lambda o: o["missions"][2].update(assigned_aircraft=["A001"]),
            lambda o: o["repairs"][0].update(started_at=2),
            lambda o: o["repairs"][1].update(spare_wait_hours=999),
            lambda o: o["orders"][0].update(expected_arrival=999),
            lambda o: o["inventory"].update(on_hand=999),
            lambda o: o["commitments"][0].update(accepted=False),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = deepcopy(original)
                request = changed["request"]
                mutate(request["observation"])
                request["request_id"] = canonical_sha256({k: v for k, v in request.items() if k != "request_id"})
                with self.assertRaises(ValueError):
                    validate_record(changed, policy, simulation)

    def test_real_replay_missing_runtime_fallback_is_auditable(self):
        simulation, policy = fixture(jev_mode="real_replay")
        for record in simulation.weekly_state["decision_records"]:
            validate_record(record, policy, simulation)
        record = simulation.weekly_state["decision_records"][1]
        self.assertTrue(record["fallback"])
        self.assertEqual(record["jev"]["error_code"], "runtime_adapter_required")

    def test_audit_never_reruns_simulation_or_past_model_calls(self):
        simulation, policy = fixture()
        with patch("sim_jev.planning.PlanningScenario.evaluate", side_effect=AssertionError("DES must not run")), \
             patch("sim_jev.jev.ChoiceAdapter.rank", side_effect=AssertionError("API must not run")):
            for record in simulation.weekly_state["decision_records"]:
                validate_record(record, policy, simulation)


if __name__ == "__main__":
    unittest.main()
