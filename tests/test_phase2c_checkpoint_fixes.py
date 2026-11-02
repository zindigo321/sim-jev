"""Saved commands must satisfy the public rules at their own decision time."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from sim_jev import Config, MissionSpec
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand


def quiet(**changes):
    values = dict(fleet_size=1, mission_rate_per_hour=0, failure_rate_per_flight_hour=0)
    values.update(changes)
    return replace(Config(), **values)


def sync_rule(data, index=0):
    rule = data["weekly_state"]["rules_input"][index]
    for row in data["events"]:
        if row["event_type"] == "rule_announced" and row["entity_id"] == rule["version"]:
            row["details"] = json.dumps({"rule": rule}, sort_keys=True)


def sync_command(data, point=0):
    command = data["weekly_state"]["cohorts"][str(point)]["command"]
    for index, old in enumerate(data["weekly_state"]["weekly_commands"]):
        if old["window_start"] == point:
            data["weekly_state"]["weekly_commands"][index] = deepcopy(command)
    for row in data["events"]:
        if row["event_type"] == "weekly_command" and row["entity_id"] == str(point):
            row["details"] = json.dumps({"command": command}, sort_keys=True)


class HistoricalCommandValidationTests(unittest.TestCase):
    def ordinary(self, *, rejected=False):
        policy = (PolicySpec("scripted", "reject", (WeeklyCommand(0, ()),))
                  if rejected else PolicySpec())
        sim = WeeklySimulation(quiet(), [MissionSpec("A", 1, 2, 1, 10)], policy=policy)
        sim.run(0)
        return sim.to_checkpoint()

    def test_changed_reserve_rule_and_matching_announcement_are_rejected(self):
        data = self.ordinary()
        data["weekly_state"]["rules_input"][0]["min_reserve"] = 1
        sync_rule(data)
        with self.assertRaisesRegex(ValueError, "historical command is illegal"):
            WeeklySimulation.from_checkpoint(data)

    def test_changed_budget_and_matching_announcement_are_rejected(self):
        data = self.ordinary()
        data["weekly_state"]["rules_input"][0]["max_aircraft_hours"] = 1
        sync_rule(data)
        with self.assertRaisesRegex(ValueError, "historical command is illegal"):
            WeeklySimulation.from_checkpoint(data)

    def test_missing_feasible_mandatory_even_with_consistent_diagnostics_is_rejected(self):
        data = self.ordinary(rejected=True)
        state = data["weekly_state"]
        state["rules_input"][0]["mandatory_ids"] = ["A"]
        state["cohorts"]["0"]["mandatory_ids"] = ["A"]
        state["cohorts"]["0"]["command"]["mandatory_ids"] = ["A"]
        state["cohorts"]["0"]["command"]["missing_mandatory"] = ["A"]
        sync_rule(data)
        sync_command(data)
        with self.assertRaisesRegex(ValueError, "omits feasible mandatory"):
            WeeklySimulation.from_checkpoint(data)

    def test_untriggered_fallback_and_false_reasons_are_rejected(self):
        for mutation in (lambda c: c.__setitem__("fallback", True),
                         lambda c: c.__setitem__("reasons", [{"code": "nominal_reserve",
                                                              "required": 1, "actual": 0, "shortfall": 1}])):
            with self.subTest(mutation=mutation):
                data = self.ordinary()
                mutation(data["weekly_state"]["cohorts"]["0"]["command"])
                sync_command(data)
                with self.assertRaisesRegex(ValueError, "deterministic validation/fallback"):
                    WeeklySimulation.from_checkpoint(data)

    def fallback(self):
        sim = WeeklySimulation(quiet(), [MissionSpec("A", 1, 2, 1, 10),
                                         MissionSpec("B", 1, 2, 1, 20)],
                               rules=[RuleSpec(0, 168, 0, 0, "must", ("A", "B"))])
        sim.run(0)
        return sim.to_checkpoint()

    def test_triggered_fallback_reason_and_declaration_cannot_be_tampered(self):
        for mutation in (lambda c: c.__setitem__("fallback", False),
                         lambda c: c.__setitem__("reasons", []),
                         lambda c: c["reasons"][0].__setitem__("shortfall", 99),
                         lambda c: c["reasons"][0].__setitem__("shortfall", True)):
            with self.subTest(mutation=mutation):
                data = self.fallback()
                mutation(data["weekly_state"]["cohorts"]["0"]["command"])
                sync_command(data)
                with self.assertRaises(ValueError):
                    WeeklySimulation.from_checkpoint(data)

    def test_alternative_feasible_subset_is_not_the_deterministic_fallback(self):
        data = self.fallback()
        state = data["weekly_state"]
        cohort = state["cohorts"]["0"]
        cohort.update(accepted_ids=["B"], rejected_ids=["A"], accepted_value=20)
        cohort["command"].update(accepted_ids=["B"], missing_mandatory=["A"])
        state["commitments"]["A"]["accepted"] = False
        state["commitments"]["B"]["accepted"] = True
        for mission in data["missions"]:
            mission["status"] = "rejected" if mission["spec"]["id"] == "A" else "pending"
        sync_command(data)
        with self.assertRaisesRegex(ValueError, "deterministic fallback"):
            WeeklySimulation.from_checkpoint(data)

    def test_valid_scripted_choice_is_not_replaced_by_baseline_policy(self):
        data = self.ordinary(rejected=True)
        with patch("sim_jev.weekly_policy.choose_command", side_effect=AssertionError("policy replay")), \
             patch("sim_jev.weekly.resolve_decision", side_effect=AssertionError("policy replay")):
            restored = WeeklySimulation.from_checkpoint(data)
        self.assertEqual(restored.to_checkpoint(), data)

    def test_cross_week_carry_over_new_reserve_and_budget_is_legal_declared_fallback(self):
        missions = [MissionSpec("CROSS", 167, 4, 1, 10), MissionSpec("NEXT", 169, 1, 1, 5)]
        rule = RuleSpec(168, 336, 168, 168, "tight", ("NEXT",), 1, 0)
        full = WeeklySimulation(quiet(), missions, rules=[rule])
        full.run()
        part = WeeklySimulation(quiet(), missions, rules=[rule])
        part.run(168)
        command = part.weekly_state["cohorts"]["168"]["command"]
        self.assertTrue(command["fallback"])
        self.assertEqual(command["accepted_ids"], [])
        self.assertEqual(command["plan_usage"]["aircraft_hours"], 3)
        data = part.to_checkpoint()
        restored = WeeklySimulation.from_checkpoint(data)
        self.assertEqual(restored.to_checkpoint(), data)
        restored.run()
        self.assertEqual(restored.to_checkpoint(), full.to_checkpoint())
        # At the final restore CROSS is already successful, but must still be
        # reconstructed as running for the 168 decision's validation.
        self.assertEqual(WeeklySimulation.from_checkpoint(full.to_checkpoint()).to_checkpoint(),
                         full.to_checkpoint())

    def test_completion_at_boundary_is_not_carry(self):
        rule = RuleSpec(168, 336, 168, 168, "boundary", ("NEXT",))
        sim = WeeklySimulation(quiet(), [MissionSpec("BEFORE", 167, 1, 1, 10),
                                        MissionSpec("NEXT", 168, 1, 1, 5)], rules=[rule])
        sim.run(169)
        data = sim.to_checkpoint()
        self.assertEqual(WeeklySimulation.from_checkpoint(data).to_checkpoint(), data)
        self.assertFalse(data["weekly_state"]["cohorts"]["168"]["command"]["fallback"])


if __name__ == "__main__":
    unittest.main()
