"""Exact planning constraints, public information and fixed fallback rules."""

from dataclasses import asdict
import json
import math
import unittest

from sim_jev.models import MissionSpec
from sim_jev.weekly_policy import (
    PolicyObservation, PolicySpec, RuleContext, RuleSpec, WeeklyCommand,
    choose_command, plan_usage, validate_command,
)


def mission(mid, start, duration=1, aircraft=1, value=1):
    return asdict(MissionSpec(mid, start, duration, aircraft, value))


def inputs(missions, fleet=1, mandatory=(), reserve=0, hours=None, carry=(), window=0):
    rule = RuleSpec(window, min(window + 168, 720), window, window, "v1",
                    mandatory, reserve, hours)
    observation = PolicyObservation(window, fleet, [], list(missions), [], [], {},
                                    [rule.to_dict()], [])
    context = RuleContext(window, min(window + 168, 720), fleet, rule, carry)
    return observation, context


class WeeklyPolicyTests(unittest.TestCase):
    def test_same_day_nonoverlap_reuses_nominal_aircraft(self):
        observations, context = inputs([mission("A", 0, 2), mission("B", 2, 3)])
        command = choose_command(observations, context, PolicySpec())
        self.assertEqual(command.accepted_ids, ("A", "B"))
        self.assertEqual(command.plan_usage, {"peak_aircraft": 1, "minimum_reserve": 0,
                                             "aircraft_hours": 5.0})

    def test_sweep_detects_subday_and_adjacent_float_overlaps(self):
        tasks = [mission("A", 0, 2), mission("B", math.nextafter(2, -math.inf), 1)]
        usage = plan_usage(tasks, 0, 168, 1)
        self.assertEqual(usage["peak_aircraft"], 2)
        observations, context = inputs(tasks)
        self.assertEqual(choose_command(observations, context, PolicySpec()).accepted_ids, ("A",))

    def test_aircraft_hours_use_required_count_and_window_overlap(self):
        usage = plan_usage([mission("carry", 167, 4, 2), mission("new", 170, 2, 3)],
                           168, 336, 6)
        self.assertEqual(usage["aircraft_hours"], 12)
        self.assertEqual(usage["peak_aircraft"], 5)
        self.assertEqual(usage["minimum_reserve"], 1)

    def test_budget_and_reserve_exact_boundary(self):
        tasks = [mission("A", 1, 2, 2, 10), mission("B", 4, 2, 1, 1)]
        observation, context = inputs(tasks, fleet=3, reserve=1, hours=4)
        self.assertEqual(choose_command(observation, context, PolicySpec()).accepted_ids, ("A",))
        observation, context = inputs(tasks, fleet=3, reserve=1, hours=6)
        self.assertEqual(choose_command(observation, context, PolicySpec()).accepted_ids, ("A", "B"))
        observation, context = inputs(tasks, fleet=3, reserve=1, hours=math.nextafter(4, -math.inf))
        self.assertEqual(choose_command(observation, context, PolicySpec()).accepted_ids, ("B",))

    def test_carry_commitments_consume_new_window_capacity(self):
        observation, context = inputs([mission("new", 168, 1)], carry=(mission("carry", 167, 2),), window=168)
        command = choose_command(observation, context, PolicySpec())
        self.assertEqual(command.accepted_ids, ())
        self.assertEqual(command.plan_usage["aircraft_hours"], 1)

    def test_mandatory_infeasible_has_uniform_fallback_and_keeps_obligations(self):
        tasks = [mission("A", 1, 2, value=1), mission("B", 1, 2, value=100), mission("C", 10)]
        observation, context = inputs(tasks, mandatory=("A", "B"))
        first = choose_command(observation, context, PolicySpec())
        second = choose_command(observation, context,
                                PolicySpec("scripted", "different", (WeeklyCommand(0, ()),)))
        self.assertEqual(first, second)
        self.assertTrue(first.fallback)
        self.assertEqual(first.mandatory_ids, ("A", "B"))
        self.assertEqual(first.accepted_ids, ("A",))
        self.assertEqual(first.missing_mandatory, ("B",))
        self.assertEqual(first.reasons[0]["code"], "nominal_reserve")
        self.assertEqual(context.active_rule.mandatory_ids, ("A", "B"))

    def test_mandatory_first_and_optional_value_start_id_ties(self):
        tasks = [mission("B", 1, value=10), mission("A", 1, value=10), mission("M", 1, value=0)]
        observation, context = inputs(tasks, fleet=2, mandatory=("M",))
        self.assertEqual(choose_command(observation, context, PolicySpec()).accepted_ids, ("A", "M"))

    def test_scripted_command_rejects_unknown_missing_mandatory_and_capacity(self):
        observation, context = inputs([mission("A", 1), mission("B", 1)], mandatory=("A",))
        for ids in (("unknown",), (), ("A", "B")):
            with self.assertRaises(ValueError):
                validate_command(WeeklyCommand(0, ids), observation, context)
        with self.assertRaises(ValueError):
            choose_command(observation, context, PolicySpec("scripted", "empty", ()))

    def test_reject_all_and_empty_input_are_explicit(self):
        observation, context = inputs([mission("A", 1, value=0)])
        command = choose_command(observation, context, PolicySpec("scripted", "reject-all", (WeeklyCommand(0, ()),)))
        self.assertEqual(command.accepted_ids, ())
        observation, context = inputs([])
        self.assertEqual(choose_command(observation, context, PolicySpec()).plan_usage["aircraft_hours"], 0)

    def test_records_round_trip_and_observation_is_independent(self):
        mission_data = mission("A", 1)
        observation, context = inputs([mission_data])
        mission_data["value"] = 999
        self.assertEqual(observation.missions[0]["value"], 1)
        policy = PolicySpec("scripted", "fixed", (WeeklyCommand(0, ("A",)),))
        command = choose_command(observation, context, policy)
        for record in (observation, context, context.active_rule, policy, command):
            self.assertEqual(type(record).from_dict(json.loads(json.dumps(record.to_dict()))), record)
        exported = observation.to_dict()
        exported["missions"][0]["value"] = 888
        self.assertEqual(observation.missions[0]["value"], 1)

    def test_hidden_fields_rejected_public_eta_allowed(self):
        observation, _ = inputs([])
        data = observation.to_dict()
        data["orders"] = [{"expected_arrival": 100}]
        PolicyObservation.from_dict(data)
        for key in ("remaining_life_hours", "failure_due_at", "due_at", "root_seed", "event_queue"):
            hidden = observation.to_dict()
            hidden["aircraft"] = [{key: 10}]
            with self.assertRaises(ValueError):
                PolicyObservation.from_dict(hidden)

    def test_future_rule_preannouncement_and_strict_schema(self):
        future = RuleSpec(168, 336, 0, 168, "future")
        self.assertEqual(RuleSpec.from_dict(future.to_dict()), future)
        for parameters in ((168, 336, 1, 168, "bad"), (168, 336, 168, 0, "bad")):
            with self.assertRaises(ValueError):
                RuleSpec(*parameters)
        data = future.to_dict()
        data["min_reserve"] = True
        with self.assertRaises(ValueError):
            RuleSpec.from_dict(data)
        data = future.to_dict()
        data["extra"] = 1
        with self.assertRaises(ValueError):
            RuleSpec.from_dict(data)


if __name__ == "__main__":
    unittest.main()
