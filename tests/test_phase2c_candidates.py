"""Public-only, bounded and common candidate generation for all selectors."""

from dataclasses import asdict, FrozenInstanceError
import json
import random
import unittest
from unittest.mock import patch

from sim_jev.candidates import (CANDIDATE_SUMMARY_VERSION, generate_candidates,
                                heuristic_ranking)
from sim_jev.config import Config
from sim_jev.decision import DecisionRequest, ModelParameters
from sim_jev.models import MissionSpec
from sim_jev.weekly_policy import (PolicyObservation, PolicySpec, RuleContext, RuleSpec,
                                   choose_command, validate_command)


def task(mid, start=10, duration=8, aircraft=1, value=10):
    return {**asdict(MissionSpec(mid, start, duration, aircraft, value)),
            "status": "pending", "assigned_aircraft": [], "actual_start": None, "actual_end": None}


def request(tasks, *, fleet=2, mandatory=(), reserve=0, hours=None, carry=(), window=0):
    rule = RuleSpec(window, min(window + 168, 720), window, window, "candidate-test-v1",
                    mandatory, reserve, hours)
    observation = PolicyObservation(
        window, fleet,
        [{"id": f"A{i:03d}", "state": "idle", "mission_id": None, "flight_hours": 0}
         for i in range(fleet)], list(tasks), [], [],
        {"on_hand": 1, "in_transit": 0, "unmet_demand": 0}, [rule.to_dict()], [])
    context = RuleContext(window, min(window + 168, 720), fleet, rule, carry)
    return DecisionRequest.create(observation, context, ModelParameters.from_config(Config(fleet_size=fleet)))


class CandidateTests(unittest.TestCase):
    def test_sixteen_distinct_feasible_candidates_retain_reference(self):
        legal = request([task(f"M{i}", value=10 + i) for i in range(8)], hours=16)
        pool = generate_candidates(legal)
        self.assertEqual(len(pool), 16)
        self.assertEqual(len({candidate.id for candidate in pool}), 16)
        self.assertEqual(len({candidate.command.accepted_ids for candidate in pool}), 16)
        self.assertEqual(pool[0].command, choose_command(legal.observation, legal.context, PolicySpec()))
        for candidate in pool:
            self.assertEqual(candidate.command,
                             validate_command(candidate.command, legal.observation, legal.context))

    def test_every_candidate_contains_feasible_mandatory(self):
        legal = request([task("M"), *(task(f"O{i}", value=i) for i in range(7))], mandatory=("M",))
        pool = generate_candidates(legal)
        self.assertEqual(len(pool), 8)  # M alone or M plus one of seven optionals.
        self.assertTrue(all("M" in candidate.command.accepted_ids for candidate in pool))

    def test_infeasible_mandatory_has_one_common_fallback(self):
        legal = request([task("A"), task("B"), task("optional", start=100)],
                        fleet=1, mandatory=("A", "B"))
        pool = generate_candidates(legal)
        self.assertEqual(len(pool), 1)
        self.assertTrue(pool[0].command.fallback)
        self.assertEqual(pool[0].command.accepted_ids, ("A",))
        self.assertEqual(pool[0].summary["missing_mandatory"], ["B"])

    def test_carry_over_limit_keeps_legal_fallback(self):
        carry = ({**asdict(MissionSpec("carry", 167, 10, 2, 999)), "status": "running"},)
        legal = request([task("new", start=170)], fleet=2, carry=carry, window=168, reserve=1)
        pool = generate_candidates(legal)
        self.assertEqual(len(pool), 1)
        self.assertTrue(pool[0].command.fallback)
        self.assertEqual(pool[0].command.accepted_ids, ())
        self.assertFalse(pool[0].summary["rule_satisfaction"]["nominal_reserve"])
        self.assertEqual(pool[0].summary["offered_value"], 10)

    def test_empty_single_and_small_pool_return_without_padding(self):
        empty = generate_candidates(request([]))
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].command.accepted_ids, ())
        self.assertFalse(empty[0].summary["heuristic"]["DSR_valid"])
        self.assertFalse(empty[0].summary["heuristic"]["RCR_valid"])
        self.assertEqual(generate_candidates(request([]), target_size=0), [])
        single = generate_candidates(request([task("M")], mandatory=("M",)))
        self.assertEqual(len(single), 1)
        optional = generate_candidates(request([task("O")]))
        self.assertEqual({item.command.accepted_ids for item in optional}, {(), ("O",)})
        self.assertEqual(heuristic_ranking([]), [])

    def test_fixed_legal_request_is_independent_of_global_random_and_execution_seed(self):
        legal = request([task(f"M{i}", value=i + 1) for i in range(9)])
        random.seed(1)
        first = [candidate.to_dict() for candidate in generate_candidates(legal, planning_seed=99)]
        random.seed(999999)
        second = [candidate.to_dict() for candidate in generate_candidates(legal, planning_seed=99)]
        self.assertEqual(first, second)
        self.assertNotIn("seed", legal.parameters.to_dict())
        common = {candidate.id: candidate.command.accepted_ids
                  for candidate in generate_candidates(legal, planning_seed=101)}
        for candidate in first:
            if candidate["id"] in common:
                self.assertEqual(tuple(candidate["command"]["accepted_ids"]), common[candidate["id"]])

    def test_heuristic_counts_current_offer_only_and_no_future_completion(self):
        legal = request([task("short", start=169, duration=1, value=10),
                         task("cross", start=335, duration=4, value=100)], window=168)
        pool = generate_candidates(legal)
        both = next(item for item in pool if len(item.command.accepted_ids) == 2)
        cross = next(item for item in pool if item.command.accepted_ids == ("cross",))
        self.assertEqual(both.summary["heuristic"]["planned_completions_by_end"], 1)
        self.assertEqual(both.summary["heuristic"]["planned_value_by_end"], 10)
        self.assertEqual(cross.summary["heuristic"]["score"], 0)
        ranking = heuristic_ranking(pool)
        self.assertEqual(ranking[0].command.accepted_ids, ("short",))
        self.assertEqual([item.id for item in ranking],
                         [item.id for item in heuristic_ranking(list(reversed(pool)))])

    def test_candidate_export_is_independent_and_versioned_finite_json(self):
        legal = request([task("A")])
        before = legal.to_dict()
        candidate = generate_candidates(legal)[0]
        exported = candidate.to_dict()
        json.dumps(exported, allow_nan=False)
        self.assertEqual(exported["summary"]["schema_version"], CANDIDATE_SUMMARY_VERSION)
        exported["summary"]["resources"]["inventory"]["on_hand"] = 999
        exported["command"]["accepted_ids"].append("fake")
        self.assertEqual(legal.to_dict(), before)
        self.assertEqual(candidate.summary["resources"]["inventory"]["on_hand"], 1)
        with self.assertRaises(FrozenInstanceError):
            candidate.id = "changed"
        for forbidden in ("remaining_life", "failure_due_at", "reference_Q", "root_seed",
                          "initial_lifetimes", "event_queue", "execution_rng"):
            self.assertNotIn(forbidden, json.dumps(candidate.to_dict()))

    def test_generation_performs_no_des_or_api_calls(self):
        legal = request([task(f"M{i}", value=i) for i in range(8)])
        with patch("sim_jev.weekly.WeeklySimulation.run", side_effect=AssertionError("DES forbidden")):
            self.assertEqual(len(generate_candidates(legal)), 16)

    def test_invalid_bounds_and_non_request_are_rejected(self):
        legal = request([])
        for size in (True, -1, 257, 1.5):
            with self.assertRaises(ValueError):
                generate_candidates(legal, target_size=size)
        with self.assertRaises(ValueError):
            generate_candidates(legal.to_dict())
        with self.assertRaises(ValueError):
            generate_candidates(legal, planning_seed=True)

    def test_post_construction_request_mutation_is_not_accepted_as_legal(self):
        legal = request([task("A")])
        legal.observation.inventory["on_hand"] = 999
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            generate_candidates(legal)


if __name__ == "__main__":
    unittest.main()
