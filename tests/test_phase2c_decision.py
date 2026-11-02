"""Actual resolver boundary and exact physical endpoint regressions."""
from dataclasses import replace
import json
import math
from unittest.mock import patch
import unittest

from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.decision import DecisionRequest, DecisionResult, ModelParameters
from sim_jev.weekly_policy import PolicySpec, choose_command


class DecisionBoundaryTests(unittest.TestCase):
    def config(self, **kwargs):
        return replace(Config(), fleet_size=1, mission_rate_per_hour=0,
                       failure_rate_per_flight_hour=0, **kwargs)

    def test_resolver_gets_only_public_versioned_request_before_commit(self):
        captured = []
        sim = WeeklySimulation(self.config(), [MissionSpec("A", 1, 2, 1, 10)])
        def resolver(request, policy, runtime):
            self.assertIsInstance(request, DecisionRequest)
            self.assertEqual(sim.weekly_state["weekly_commands"], [])
            self.assertEqual(sim.missions["A"].status, "pending")
            text = json.dumps(request.to_dict())
            for name in ("seed", "remaining_life", "due_at", "event_queue", "manifest", "initial_lifetimes"):
                self.assertNotIn(name, text)
            self.assertEqual(DecisionRequest.from_dict(request.to_dict()), request)
            captured.append(request)
            return DecisionResult(choose_command(request.observation, request.context, policy))
        with patch("sim_jev.weekly.resolve_decision", resolver):
            sim.run(0)
        self.assertEqual(len(captured), 1)
        sim.run(0)
        self.assertEqual(len(sim.weekly_state["weekly_commands"]), 1)

    def test_public_parameter_whitelist_rejects_execution_seed_and_unknown_nested_fields(self):
        params = ModelParameters.from_config(self.config()).to_dict()
        params["seed"] = 10
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            ModelParameters.from_dict(params)
        sim = WeeklySimulation(self.config(), [])
        request, _ = sim._publish_request()
        damaged = request.to_dict()
        damaged["observation"]["aircraft"][0]["hidden_future"] = 42
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            DecisionRequest.from_dict(damaged)

    def test_endpoint_physical_completion_before_next_offer_or_day(self):
        sim = WeeklySimulation(self.config(), [MissionSpec("A", 166, 2, 1, 10),
                                               MissionSpec("B", 168, 1, 1, 99)])
        sim.run(0)
        sim.run_window(168)
        self.assertEqual(sim.missions["A"].status, "success")
        self.assertEqual(sim.missions["B"].status, "pending")
        self.assertEqual(sim.weekly_state["processed_week_points"], [0.])
        self.assertNotIn(168, sim.weekly_state["processed_day_points"])
        self.assertEqual(sim.weekly_state["closed_cohort_snapshots"][0]["counts"]["success"], 1)
        with self.assertRaises(ValueError):
            sim.to_checkpoint()
        sim.run(168)
        self.assertEqual(sim.weekly_state["processed_week_points"], [0., 168.])
        self.assertEqual(sim.missions["B"].status, "running")

    def test_neighbor_time_not_merged_and_terminal_once(self):
        after = math.nextafter(168., math.inf)
        sim = WeeklySimulation(self.config(), [MissionSpec("A", 0, after, 1, 10)])
        sim.run_window(168)
        self.assertEqual(sim.missions["A"].status, "running")
        sim.run(after)
        self.assertEqual(sim.missions["A"].status, "success")
        sim.run(672)
        sim.run_window(720)
        sim.run(720)
        self.assertEqual(sum(r["event_type"] == "terminal_boundary" for r in sim.events), 1)


if __name__ == "__main__":
    unittest.main()
