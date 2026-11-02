"""Legal conditional worlds use the real physical handlers and strict endpoints."""

from copy import deepcopy
from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from sim_jev import Config, MissionSpec
from sim_jev.decision import DecisionRequest, DecisionResult, ModelParameters
from sim_jev.keyed_rng import KeyedRNG
from sim_jev.planning import PlanningInput, PlanningScenario, conditional_repair_due
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicyObservation, PolicySpec, RuleSpec, WeeklyCommand, choose_command


def config(**changes):
    values = dict(fleet_size=2, mission_rate_per_hour=0, failure_rate_per_flight_hour=0,
                  repair_bays=1, initial_spares=1, reorder_target=1,
                  repair_duration_min_hours=48, repair_duration_max_hours=48,
                  lead_time_min_hours=48, lead_time_max_hours=48)
    values.update(changes)
    return replace(Config(), **values)


def capture(sim, until):
    requests = []

    def resolver(request, policy, runtime=None):
        requests.append(request)
        return DecisionResult(choose_command(request.observation, request.context, policy))

    with patch("sim_jev.weekly.resolve_decision", side_effect=resolver):
        sim.run(until)
    return requests[-1]


def branch(request, accepted, seed=19, scenario=0):
    return PlanningScenario(PlanningInput(request, seed, scenario)).evaluate(
        WeeklyCommand(request.context.window_start, tuple(accepted)))


class LegalPlanningTests(unittest.TestCase):
    def test_hidden_execution_identity_lifetimes_and_unpublished_tasks_do_not_enter(self):
        common = [MissionSpec("A", 1, 2, 1, 10), MissionSpec("B", 4, 3, 1, 20)]
        first = WeeklySimulation(config(seed=1, failure_rate_per_flight_hour=.05),
                                 [*common, MissionSpec("FUTURE1", 400, 1, 1, 1)], [1, 2])
        second = WeeklySimulation(config(seed=999, failure_rate_per_flight_hour=.05),
                                  [*common, MissionSpec("FUTURE2", 500, 8, 1, 99)], [100, 200])
        a, b = capture(first, 0), capture(second, 0)
        self.assertEqual(a.to_dict(), b.to_dict())
        before_a, before_b = first.to_checkpoint(), second.to_checkpoint()
        pa, pb = PlanningScenario(PlanningInput(a, 77, 0)), PlanningScenario(PlanningInput(b, 77, 0))
        self.assertEqual(pa.to_dict(), pb.to_dict())
        self.assertEqual(pa.evaluate(WeeklyCommand(0, ("A",))), pb.evaluate(WeeklyCommand(0, ("A",))))
        self.assertEqual(first.to_checkpoint(), before_a)
        self.assertEqual(second.to_checkpoint(), before_b)
        self.assertEqual(pa.to_dict()["initial_state"]["random_manifest"]["domain"], "planning")

    def test_exponential_conditioned_residual_is_independent_and_zero_rate_is_infinite(self):
        request = capture(WeeklySimulation(config(), [MissionSpec("A", 167, 4, 1, 10)]), 168)
        scenario = PlanningScenario(PlanningInput(request, 1, 0))
        self.assertTrue(all(math.isinf(a.remaining_life_hours) for a in scenario._world.aircraft.values()))
        self.assertEqual(scenario._world.aircraft["A001"].flight_hours, 1)
        self.assertEqual(scenario._world.missions["A"].actual_start, 167)
        self.assertEqual(next(e.time for e in scenario._world._queue if e.kind == "mission_end"), 171)

    def test_conditional_repair_distribution_respects_elapsed_service_and_fixed_duration(self):
        draws = [conditional_repair_due(KeyedRNG(99, str(i), domain="planning"),
                                       aircraft_id="A001", generation=0, started_at=150,
                                       as_of=168, low=12, high=24) for i in range(100)]
        self.assertTrue(all(168 < value <= 174 for value in draws))
        self.assertLess(min(draws), 169)
        self.assertGreater(max(draws), 173)
        self.assertEqual(conditional_repair_due(KeyedRNG(1, "fixed", domain="planning"),
                                               aircraft_id="A001", generation=0, started_at=150,
                                               as_of=168, low=24, high=24), 174)
        with self.assertRaisesRegex(ValueError, "empty conditional repair support"):
            conditional_repair_due(KeyedRNG(1, "invalid", domain="planning"),
                                   aircraft_id="A001", generation=0, started_at=144,
                                   as_of=168, low=12, high=24)

    def complex_request(self):
        missions = [MissionSpec("FAIL1", 143, 10, 1, 2), MissionSpec("FAIL2", 145, 10, 1, 3),
                    MissionSpec("CROSS", 167, 4, 1, 1000), MissionSpec("NOW", 170, 1, 1, 10),
                    MissionSpec("FUT", 400, 1, 1, 100)]
        rules = [RuleSpec(168, 336, 168, 168, "current", ("NOW",)),
                 RuleSpec(336, 504, 0, 336, "public-future", ("FUT",))]
        sim = WeeklySimulation(config(fleet_size=4), missions, [1, 1, math.inf, math.inf], rules=rules)
        return sim, capture(sim, 168)

    def test_flying_repair_waiting_transit_and_future_rule_ids_drive_shared_handlers(self):
        source, request = self.complex_request()
        states = {row["state"] for row in request.observation.aircraft}
        self.assertEqual(states, {"flying", "repairing", "waiting_repair", "idle"})
        self.assertEqual(request.observation.inventory["in_transit"], 1)
        scenario = PlanningScenario(PlanningInput(request, 28, 0))
        world = scenario._world
        self.assertNotIn("FUT", world.missions)
        self.assertTrue(any(rule["mandatory_ids"] == ["FUT"] for rule in world.weekly_state["rules_input"]))
        self.assertEqual(world.aircraft["A001"].remaining_life_hours, 0)
        self.assertEqual(world.aircraft["A002"].remaining_life_hours, 0)
        self.assertEqual(world.repairs["R000001"].due_at, 192)
        eta = request.observation.orders[0]["expected_arrival"]
        self.assertEqual(world.orders["O000001"].expected_arrival, eta)
        self.assertEqual(world.events, [])
        self.assertEqual([row["time"] for row in world.history], [168])
        initial = scenario.to_dict()
        result = scenario.evaluate(WeeklyCommand(168, ("NOW",)))
        self.assertEqual(scenario.to_dict(), initial)
        event_types = {row["event_type"] for row in result["final_state"]["events"]}
        self.assertTrue({"repair_complete", "repair_start", "order_arrival", "mission_success"} <= event_types)
        arrivals = [row for row in result["final_state"]["events"]
                    if row["event_type"] == "order_arrival" and row["entity_id"] == "O000001"]
        self.assertEqual(arrivals[0]["time"], eta)
        self.assertEqual(result["counts"]["offered_value"], 10)
        self.assertEqual(result["counts"]["earned_value"], 10)
        self.assertEqual(result["score"], 2)
        self.assertEqual(result["metrics_delta"]["flight_hours"], 4)
        self.assertEqual(source.missions["CROSS"].status, "running")

    def test_hidden_active_repair_realization_does_not_change_legal_request_world(self):
        source, request = self.complex_request()
        before = PlanningScenario(PlanningInput(request, 6, 0)).to_dict()
        source.repairs["R000001"].due_at += 10
        source.aircraft["A003"].remaining_life_hours = .01
        # Only legal inputs are accepted by PlanningScenario; no constructor
        # argument can reference the changed execution simulation.
        same = DecisionRequest.create(request.observation, request.context,
                                      ModelParameters.from_config(source.config))
        self.assertEqual(before, PlanningScenario(PlanningInput(same, 6, 0)).to_dict())
        with self.assertRaises(ValueError):
            PlanningScenario(source)

    def test_invalid_observed_repair_survival_is_reported(self):
        _, request = self.complex_request()
        parameters = replace(request.parameters, repair_duration_min_hours=1,
                             repair_duration_max_hours=2)
        impossible = DecisionRequest.create(request.observation, request.context, parameters)
        with self.assertRaisesRegex(ValueError, "empty conditional repair support"):
            PlanningScenario(PlanningInput(impossible, 2, 0))

    def test_endpoint_physical_completion_current_commit_once_and_no_next_offer(self):
        for point, end in ((0, 168), (168, 336), (672, 720)):
            with self.subTest(point=point):
                sim = WeeklySimulation(config(), [MissionSpec("END", end - 1, 1, 1, 10),
                                                  MissionSpec("NEXT", end, 1, 1, 20)])
                request = capture(sim, point)
                result = branch(request, ("END",))
                self.assertEqual(result["duration_hours"], end - point)
                self.assertEqual(result["counts"]["success"], 1)
                self.assertEqual(len(result["final_state"]["weekly_state"]["weekly_commands"]), 1)
                events = result["final_state"]["events"]
                self.assertEqual(sum(row["event_type"] == "weekly_command" for row in events), 1)
                self.assertFalse(any(row["event_type"] in {"weekly_offer", "rule_announced"} for row in events))
                self.assertFalse(any(row["event_type"] == "daily_command" and row["time"] == end for row in events))
                self.assertEqual(sum(row["event_type"] == "terminal_boundary" for row in events), int(end == 720))

    def test_adjacent_float_end_is_unresolved_and_zero_denominator_score_validity(self):
        duration = math.nextafter(168., math.inf) - 167.
        request = capture(WeeklySimulation(config(), [MissionSpec("AFTER", 167, duration, 1, 0)]), 0)
        result = branch(request, ("AFTER",))
        self.assertEqual(result["counts"]["success"], 0)
        self.assertEqual(result["unresolved"], 1)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["validity"], {"DSR": True, "RCR": False})
        empty = capture(WeeklySimulation(config(), []), 0)
        result = branch(empty, ())
        self.assertEqual(result["score_components"], {"DSR": 0, "RCR": 0})
        self.assertEqual(result["validity"], {"DSR": False, "RCR": False})

    def test_reordering_adding_and_repeating_evaluation_changes_no_branch(self):
        request = capture(WeeklySimulation(config(failure_rate_per_flight_hour=.2),
                                           [MissionSpec("A", 1, 12, 1, 10),
                                            MissionSpec("B", 2, 10, 1, 20)]), 0)
        worlds = [PlanningScenario(PlanningInput(request, 5, index)) for index in range(2)]
        commands = [WeeklyCommand(0, ()), WeeklyCommand(0, ("A",)), WeeklyCommand(0, ("B",))]
        expected = {(s, i): worlds[s].evaluate(command)
                    for s in range(2) for i, command in enumerate(commands)}
        for s in reversed(range(2)):
            worlds[s].evaluate(WeeklyCommand(0, ("A", "B")))
            for i in (2, 0, 1, 1):
                self.assertEqual(worlds[s].evaluate(commands[i]), expected[s, i])

    def test_planning_world_cannot_use_execution_checkpoint_or_reset(self):
        request = capture(WeeklySimulation(config(), []), 0)
        world = PlanningScenario(PlanningInput(request, 0, 0))._world
        with self.assertRaisesRegex(ValueError, "never execution checkpoints"):
            world.to_checkpoint()
        with self.assertRaisesRegex(ValueError, "new PlanningScenario"):
            world.reset()

    def test_terminal_physics_and_ledger_are_not_repeated(self):
        request = capture(WeeklySimulation(config(), [MissionSpec("END", 719, 1, 1, 10)]), 672)
        world = PlanningScenario(PlanningInput(request, 0, 0))._world.clone()
        world._commit_command(WeeklyCommand(672, ("END",)), request.context, ["END"])
        world.run_window(720)
        first = world.planning_state()
        world.run_window(720)
        self.assertEqual(world.planning_state(), first)


if __name__ == "__main__":
    unittest.main()
