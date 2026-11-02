"""Deterministic acceptance scenarios against the real event engine."""

import json
import math
import unittest
from dataclasses import replace

from sim_jev.config import Config
from sim_jev.engine import Simulation
from sim_jev.models import MissionSpec


def scenario(**changes):
    defaults = dict(
        horizon_hours=12,
        fleet_size=2,
        mission_rate_per_hour=0,
        failure_rate_per_flight_hour=0,
        repair_bays=1,
        repair_duration_min_hours=2,
        repair_duration_max_hours=2,
        initial_spares=0,
        reorder_target=0,
        reorder_interval_hours=24,
        lead_time_min_hours=3,
        lead_time_max_hours=3,
    )
    defaults.update(changes)
    return replace(Config(), **defaults)


def mission(name="M1", start=0, duration=5, required=1, value=100):
    return MissionSpec(name, start, duration, required, value)


class MissionAndFailureTests(unittest.TestCase):
    def test_no_failure_completes_entire_mission(self):
        sim = Simulation(scenario(), [mission(required=2)])
        result = sim.run()
        self.assertEqual(sim.missions["M1"].status, "success")
        self.assertEqual(sim.missions["M1"].assigned_aircraft, ["A001", "A002"])
        self.assertEqual(result["missions_success"], 1)
        self.assertEqual(result["missions_failed"], 0)
        self.assertEqual(result["mission_value_earned"], 100)
        self.assertAlmostEqual(result["flight_hours"], 10)
        self.assertTrue(all(a.state == "idle" for a in sim.aircraft.values()))
        self.assertAlmostEqual(result["availability"], 1)

    def test_insufficient_aircraft_does_not_partially_allocate(self):
        sim = Simulation(scenario(), [mission(required=3)])
        result = sim.run()
        self.assertEqual(sim.missions["M1"].status, "not_executed")
        self.assertEqual(sim.missions["M1"].assigned_aircraft, [])
        self.assertEqual(result["missions_not_executed"], 1)
        self.assertEqual(result["flight_hours"], 0)
        self.assertTrue(all(a.state == "idle" for a in sim.aircraft.values()))

    def test_same_time_missions_use_stable_id_order(self):
        tasks = [mission("Z", required=2), mission("A", required=2)]
        sim = Simulation(scenario(), tasks)
        sim.run()
        self.assertEqual(sim.missions["A"].status, "success")
        self.assertEqual(sim.missions["Z"].status, "not_executed")

    def test_fault_aborts_once_and_releases_healthy_aircraft(self):
        sim = Simulation(scenario(), [mission(duration=10, required=2)], [2, 5])
        sim.run(until=2)
        self.assertEqual(sim.missions["M1"].status, "failed")
        self.assertEqual(sim.missions["M1"].actual_end, 2)
        self.assertEqual(sim.aircraft["A001"].state, "waiting_repair")
        self.assertEqual(sim.aircraft["A002"].state, "idle")
        self.assertIsNone(sim.aircraft["A002"].mission_id)
        result = sim.run()
        self.assertEqual(result["missions_failed"], 1)
        self.assertEqual(result["missions_success"], 0)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["mission_value_earned"], 0)
        self.assertAlmostEqual(result["flight_hours"], 4)
        self.assertAlmostEqual(sim.aircraft["A002"].remaining_life_hours, 3)
        self.assertAlmostEqual(sim.aircraft["A001"].remaining_life_hours, 0)

    def test_stale_fault_does_not_interrupt_new_mission(self):
        tasks = [mission("M1", duration=10, required=2), mission("M2", 3, 8)]
        sim = Simulation(scenario(), tasks, [2, 5])
        sim.run(until=5.5)
        self.assertEqual(sim.missions["M2"].status, "running")
        self.assertEqual(sim.summary()["failures"], 1)
        self.assertEqual(sim.aircraft["A002"].state, "flying")
        sim.run()
        self.assertEqual(sim.missions["M2"].actual_end, 6)
        self.assertEqual(sim.summary()["missions_failed"], 2)
        self.assertEqual(sim.summary()["missions_success"], 0)
        self.assertEqual(sim.summary()["failures"], 2)
        self.assertAlmostEqual(sim.summary()["flight_hours"], 7)
        self.assertTrue(all(a.state == "waiting_repair" for a in sim.aircraft.values()))

    def test_ground_time_does_not_consume_component_life(self):
        tasks = [mission("M1", 2, 2), mission("M2", 8, 2)]
        sim = Simulation(scenario(fleet_size=1), tasks, [5])
        sim.run(until=7)
        self.assertAlmostEqual(sim.aircraft["A001"].remaining_life_hours, 3)
        sim.run()
        self.assertEqual(sim.summary()["missions_success"], 2)
        self.assertAlmostEqual(sim.aircraft["A001"].remaining_life_hours, 1)
        self.assertAlmostEqual(sim.summary()["flight_hours"], 4)

    def test_fault_at_completion_time_has_priority(self):
        sim = Simulation(scenario(), [mission(duration=3)], [3, math.inf])
        result = sim.run()
        self.assertEqual(result["missions_failed"], 1)
        self.assertEqual(result["missions_success"], 0)
        self.assertEqual(sim.missions["M1"].actual_end, 3)

    def test_simultaneous_faults_fail_mission_once_but_both_aircraft_fail(self):
        sim = Simulation(scenario(), [mission(required=2)], [2, 2])
        result = sim.run()
        self.assertEqual(result["missions_failed"], 1)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(len(sim.repairs), 2)
        self.assertEqual(set(sim.missions["M1"].failed_aircraft), {"A001", "A002"})
        self.assertTrue(all(a.state == "waiting_repair" for a in sim.aircraft.values()))

    def test_zero_initial_life_faults_without_flying(self):
        sim = Simulation(scenario(fleet_size=1), [mission()], [0])
        result = sim.run()
        self.assertEqual(result["missions_failed"], 1)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["flight_hours"], 0)
        self.assertEqual(sim.missions["M1"].actual_end, 0)


class MaintenanceAndInventoryTests(unittest.TestCase):
    def test_spare_available_but_no_free_bay_queues_then_starts(self):
        config = scenario(initial_spares=2, repair_duration_min_hours=3,
                          repair_duration_max_hours=3)
        sim = Simulation(config, [mission(required=2)], [1, 1])
        sim.run(until=1)
        self.assertEqual(sim.aircraft["A001"].state, "repairing")
        self.assertEqual(sim.aircraft["A002"].state, "waiting_repair")
        self.assertEqual(sim.on_hand, 1)
        sim.run(until=4)
        self.assertEqual(sim.aircraft["A001"].state, "idle")
        self.assertEqual(sim.aircraft["A002"].state, "repairing")
        sim.run()
        repairs = sorted(sim.repairs.values(), key=lambda item: item.aircraft_id)
        self.assertEqual([r.started_at for r in repairs], [1, 4])
        self.assertEqual([r.completed_at for r in repairs], [4, 7])
        self.assertAlmostEqual(sim.summary()["bay_wait_hours"], 3)
        self.assertAlmostEqual(sim.summary()["spare_wait_hours"], 0)
        self.assertEqual(sim.summary()["repairs_completed"], 2)
        self.assertTrue(all(math.isinf(a.remaining_life_hours) for a in sim.aircraft.values()))

    def test_no_spare_does_not_occupy_bay_and_arrival_unblocks(self):
        config = scenario(fleet_size=1, reorder_interval_hours=2)
        sim = Simulation(config, [mission()], [1])
        sim.run(until=4)
        self.assertEqual(sim.aircraft["A001"].state, "waiting_repair")
        self.assertFalse(any(a.state == "repairing" for a in sim.aircraft.values()))
        self.assertEqual(sim.on_hand, 0)
        self.assertEqual(sim.in_transit, 1)
        sim.run(until=5)
        self.assertEqual(sim.aircraft["A001"].state, "repairing")
        repair = next(iter(sim.repairs.values()))
        self.assertEqual(repair.started_at, 5)
        sim.run()
        self.assertEqual(repair.completed_at, 7)
        self.assertAlmostEqual(sim.summary()["spare_wait_hours"], 4)
        self.assertAlmostEqual(sim.summary()["bay_wait_hours"], 0)
        self.assertEqual(sim.summary()["arrived_quantity"], 1)

    def test_repeated_checks_do_not_duplicate_pipeline_order(self):
        config = scenario(horizon_hours=9, initial_spares=0, reorder_target=3,
                          reorder_interval_hours=1, lead_time_min_hours=10,
                          lead_time_max_hours=10)
        sim = Simulation(config, [])
        result = sim.run()
        self.assertEqual(len(sim.orders), 1)
        self.assertEqual(result["ordered_quantity"], 3)
        self.assertEqual(result["arrived_quantity"], 0)
        self.assertEqual(result["in_transit"], 3)

    def test_waiting_demand_is_in_inventory_position_without_duplicate_orders(self):
        config = scenario(horizon_hours=5, fleet_size=1, reorder_interval_hours=1,
                          lead_time_min_hours=10, lead_time_max_hours=10)
        sim = Simulation(config, [mission()], [1])
        result = sim.run()
        self.assertEqual(len(sim.orders), 1)
        self.assertEqual(result["ordered_quantity"], 1)
        self.assertEqual(sim.unmet_demand, 1)
        self.assertEqual(sim.in_transit, 1)

    def test_each_order_arrives_once(self):
        config = scenario(initial_spares=0, reorder_target=2,
                          reorder_interval_hours=1)
        sim = Simulation(config, [])
        sim.run(until=3)
        self.assertEqual(sim.on_hand, 2)
        sim.run()
        self.assertEqual(sim.on_hand, 2)
        self.assertEqual(sim.summary()["ordered_quantity"], 2)
        self.assertEqual(sim.summary()["arrived_quantity"], 2)
        self.assertEqual(len(sim.orders), 1)
        self.assertEqual(next(iter(sim.orders.values())).status, "arrived")

    def test_duplicate_arrival_event_is_ignored(self):
        config = scenario(reorder_target=2)
        sim = Simulation(config, [])
        sim.run(until=0)
        order = next(iter(sim.orders.values()))
        sim._schedule(order.expected_arrival, "order_arrival", order.id)
        sim.run()
        self.assertEqual(sim.on_hand, 2)
        self.assertEqual(sim.summary()["arrived_quantity"], 2)
        self.assertEqual(sim.in_transit, 0)

    def test_duplicate_completion_event_does_not_repair_twice(self):
        sim = Simulation(scenario(fleet_size=1, initial_spares=1), [mission()], [1])
        sim.run(until=1)
        repair = next(iter(sim.repairs.values()))
        sim._schedule(repair.due_at, "repair_complete", repair.id)
        sim.run()
        self.assertEqual(sim.summary()["repairs_completed"], 1)
        self.assertEqual(sim.on_hand, 0)
        self.assertEqual(sim.aircraft["A001"].state, "idle")

    def test_wait_categories_are_disjoint_with_zero_bays(self):
        sim = Simulation(scenario(horizon_hours=6, repair_bays=0, initial_spares=1),
                         [mission(required=2)], [1, 1])
        result = sim.run()
        self.assertEqual(sim.on_hand, 1)
        self.assertEqual(result["repairs_completed"], 0)
        self.assertAlmostEqual(result["bay_wait_hours"], 5)
        self.assertAlmostEqual(result["spare_wait_hours"], 5)
        self.assertTrue(all(a.state == "waiting_repair" for a in sim.aircraft.values()))

    def test_repair_completion_precedes_mission_at_same_time(self):
        sim = Simulation(scenario(fleet_size=1, initial_spares=1),
                         [mission("M1"), mission("M2", 3, 2)], [1])
        sim.run()
        self.assertEqual(sim.missions["M1"].status, "failed")
        self.assertEqual(sim.missions["M2"].status, "success")
        self.assertEqual(sim.summary()["repairs_completed"], 1)
        self.assertEqual(sim.summary()["flight_hours"], 3)

    def test_zero_lead_and_repair_duration_recover_at_same_time(self):
        config = scenario(fleet_size=1, reorder_interval_hours=1,
                          repair_duration_min_hours=0, repair_duration_max_hours=0,
                          lead_time_min_hours=0, lead_time_max_hours=0)
        sim = Simulation(config, [mission("M1"), mission("M2", 1, 2)], [1])
        sim.run()
        self.assertEqual(sim.missions["M2"].status, "success")
        repair = next(iter(sim.repairs.values()))
        self.assertEqual(repair.requested_at, 1)
        self.assertEqual(repair.started_at, 1)
        self.assertEqual(repair.completed_at, 1)
        self.assertEqual(sim.summary()["repairs_completed"], 1)


class HorizonAndStateTests(unittest.TestCase):
    def test_final_interval_counts_and_running_mission_remains_running(self):
        sim = Simulation(scenario(horizon_hours=5), [mission(start=1, duration=10)])
        result = sim.run()
        self.assertEqual(sim.now, 5)
        self.assertEqual(sim.missions["M1"].status, "running")
        self.assertIsNone(sim.missions["M1"].actual_end)
        self.assertEqual(result["missions_unfinished"], 1)
        self.assertEqual(result["missions_failed"], 0)
        self.assertAlmostEqual(result["flight_hours"], 4)
        self.assertAlmostEqual(result["available_aircraft_hours"], 10)
        self.assertAlmostEqual(result["idle_aircraft_hours"], 6)
        self.assertAlmostEqual(result["availability"], 1)

    def test_cutoff_events_are_processed_and_finished_aircraft_can_be_reused(self):
        sim = Simulation(scenario(horizon_hours=5, fleet_size=1),
                         [mission("M1", duration=5), mission("M2", 5, 2)])
        result = sim.run()
        self.assertEqual(sim.missions["M1"].status, "success")
        self.assertEqual(sim.missions["M2"].status, "running")
        self.assertEqual(result["missions_offered"], 2)
        self.assertEqual(result["missions_unfinished"], 1)
        self.assertEqual(result["flight_hours"], 5)

    def test_cutoff_preserves_active_repair(self):
        config = scenario(horizon_hours=5, initial_spares=1,
                          repair_duration_min_hours=10, repair_duration_max_hours=10)
        sim = Simulation(config, [mission()], [1, math.inf])
        result = sim.run()
        self.assertEqual(sim.aircraft["A001"].state, "repairing")
        repair = next(iter(sim.repairs.values()))
        self.assertIsNone(repair.completed_at)
        self.assertEqual(repair.due_at, 11)
        self.assertEqual(result["repairs_completed"], 0)
        self.assertAlmostEqual(result["availability"], 0.6)
        self.assertAlmostEqual(result["available_aircraft_hours"], 6)

    def test_cutoff_preserves_waiting_and_transit_with_wait_integral(self):
        config = scenario(horizon_hours=5, fleet_size=1, reorder_interval_hours=1,
                          lead_time_min_hours=10, lead_time_max_hours=10)
        sim = Simulation(config, [mission()], [1])
        result = sim.run()
        self.assertEqual(sim.aircraft["A001"].state, "waiting_repair")
        self.assertEqual(next(iter(sim.orders.values())).status, "in_transit")
        self.assertAlmostEqual(result["spare_wait_hours"], 4)
        self.assertAlmostEqual(result["availability"], 0.2)
        self.assertEqual(result["in_transit"], 1)

    def test_zero_tasks_inventory_and_failure_rate_are_legal(self):
        sim = Simulation(scenario(), [])
        result = sim.run()
        self.assertEqual(result["missions_offered"], 0)
        self.assertEqual(result["flight_hours"], 0)
        self.assertEqual(result["failures"], 0)
        self.assertEqual(result["ordered_quantity"], 0)
        self.assertEqual(result["availability"], 1)
        self.assertEqual(sim.on_hand, 0)
        self.assertTrue(all(math.isinf(a.remaining_life_hours) for a in sim.aircraft.values()))

    def test_observation_does_not_leak_hidden_state_or_future_tasks(self):
        sim = Simulation(scenario(), [mission("VISIBLE", 0), mission("FUTURE", 8)],
                         [2, 7])
        sim.run(until=0)
        observation = sim.get_observation()
        encoded = json.dumps(observation, allow_nan=False)
        self.assertIn("VISIBLE", encoded)
        self.assertNotIn("FUTURE", encoded)
        for hidden in ("remaining_life", "failure_due_at", "bit_generator", "rng",
                       "event_queue", "initial_lifetimes"):
            self.assertNotIn(hidden, encoded)
        observation["time"] = -100
        self.assertEqual(sim.now, 0)
        self.assertNotEqual(sim.get_observation().get("time"), -100)

    def test_state_export_is_json_serializable_even_for_infinite_life(self):
        sim = Simulation(scenario(), [])
        sim.run()
        state = sim.export_state()
        encoded = json.dumps(state, allow_nan=False)
        self.assertIn("A001", encoded)

    def test_invalid_run_targets_are_rejected(self):
        sim = Simulation(scenario(), [])
        sim.run(until=3)
        for target in (-1, 2, 13, math.inf, math.nan, True):
            with self.subTest(target=target), self.assertRaises(ValueError):
                sim.run(until=target)


class RandomnessAndInvariantTests(unittest.TestCase):
    def test_same_seed_identical_events_and_scientific_metrics(self):
        config = replace(Config(), horizon_hours=100, seed=23)
        first, second = Simulation(config), Simulation(config)
        self.assertEqual(first.run(), second.run())
        self.assertEqual(first.events, second.events)
        self.assertEqual(first.history, second.history)

    def test_reset_replays_original_inputs_and_seed(self):
        config = replace(Config(), horizon_hours=100, seed=23)
        sim = Simulation(config)
        result = sim.run()
        events = list(sim.events)
        sim.reset()
        self.assertEqual(sim.now, 0)
        self.assertEqual(result, sim.run())
        self.assertEqual(events, sim.events)
        fixed = Simulation(scenario(), [mission()], [1, 10])
        fixed_result = fixed.run()
        fixed.reset()
        self.assertEqual(fixed_result, fixed.run())

    def test_incremental_run_matches_one_pass(self):
        config = replace(Config(), horizon_hours=100, seed=5)
        single, incremental = Simulation(config), Simulation(config)
        expected = single.run()
        for time in (0, 1, 6.5, 24, 70, 100):
            incremental.run(until=time)
            incremental.assert_invariants()
        actual = incremental.summary()
        self.assertEqual(single.events, incremental.events)
        self.assertEqual(set(expected), set(actual))
        for key, value in expected.items():
            with self.subTest(metric=key):
                if isinstance(value, float):
                    self.assertAlmostEqual(value, actual[key], places=10)
                else:
                    self.assertEqual(value, actual[key])

    def test_fractional_pause_times_cannot_move_failure_past_cutoff(self):
        config = scenario(horizon_hours=0.6, fleet_size=1,
                          initial_spares=0, reorder_target=0)
        tasks = [MissionSpec("M1", 0, 0.3, 1, 1), MissionSpec("M2", 0.3, 0.3, 1, 1)]
        single = Simulation(config, tasks, [0.6])
        paused = Simulation(config, tasks, [0.6])
        single.run()
        paused.run(until=0.1)
        paused.run(until=0.2)
        paused.run()
        self.assertEqual(single.missions["M2"].status, "failed")
        self.assertEqual(paused.missions["M2"].status, "failed")
        self.assertEqual(single.events, paused.events)
        self.assertEqual(single.summary()["failures"], paused.summary()["failures"])

    def test_task_stream_is_independent_of_failure_and_support_parameters(self):
        config = replace(Config(), horizon_hours=100, seed=6)
        alternate = replace(config, failure_rate_per_flight_hour=0.5,
                            repair_duration_min_hours=1, repair_duration_max_hours=2,
                            lead_time_min_hours=1, lead_time_max_hours=2)
        first, second = Simulation(config), Simulation(alternate)
        first.run()
        second.run()
        self.assertEqual([m.spec for m in first.missions.values()],
                         [m.spec for m in second.missions.values()])

    def test_failure_stream_is_independent_of_task_generation(self):
        config = replace(Config(), horizon_hours=100, seed=6)
        first, second = Simulation(config), Simulation(config, [])
        self.assertEqual([a.remaining_life_hours for a in first.aircraft.values()],
                         [a.remaining_life_hours for a in second.aircraft.values()])

    def test_repair_stream_is_independent_of_supply_randomness(self):
        config = scenario(seed=7, initial_spares=2, reorder_target=3,
                          repair_duration_min_hours=2, repair_duration_max_hours=4,
                          lead_time_min_hours=1, lead_time_max_hours=2)
        alternate = replace(config, lead_time_min_hours=10, lead_time_max_hours=20)
        tasks = [mission("M1"), mission("M2")]
        first, second = Simulation(config, tasks, [1, 2]), Simulation(alternate, tasks, [1, 2])
        first.run()
        second.run()
        first_durations = [r.due_at - r.started_at for r in first.repairs.values()]
        second_durations = [r.due_at - r.started_at for r in second.repairs.values()]
        self.assertEqual(len(first_durations), 2)
        self.assertEqual(first_durations, second_durations)

    def test_supply_stream_is_independent_of_task_randomness(self):
        config = replace(Config(), horizon_hours=12, initial_spares=0, reorder_target=2,
                         lead_time_min_hours=2, lead_time_max_hours=6)
        first, second = Simulation(config), Simulation(config, [])
        first.run(until=0)
        second.run(until=0)
        self.assertEqual(next(iter(first.orders.values())).expected_arrival,
                         next(iter(second.orders.values())).expected_arrival)

    def test_stress_invariants_and_inventory_conservation_across_time(self):
        config = replace(Config(), horizon_hours=72, seed=9, fleet_size=4,
                         mission_rate_per_hour=1, failure_rate_per_flight_hour=0.3,
                         initial_spares=0, reorder_target=1, repair_bays=1,
                         repair_duration_min_hours=2, repair_duration_max_hours=4,
                         lead_time_min_hours=5, lead_time_max_hours=10,
                         reorder_interval_hours=2)
        sim = Simulation(config)
        for time in range(73):
            sim.run(until=time)
            sim.assert_invariants()
            self.assertGreaterEqual(sim.on_hand, 0)
            self.assertLessEqual(sum(a.state == "repairing" for a in sim.aircraft.values()), 1)
            running_ids = [aid for m in sim.missions.values() if m.status == "running"
                           for aid in m.assigned_aircraft]
            self.assertEqual(len(running_ids), len(set(running_ids)))
            result = sim.summary()
            started = sum(r.started_at is not None for r in sim.repairs.values())
            self.assertEqual(sim.on_hand, config.initial_spares + result["arrived_quantity"] - started)
            self.assertEqual(result["ordered_quantity"], result["arrived_quantity"] + sim.in_transit)
        self.assertGreater(sim.summary()["failures"], 0)
        self.assertGreater(sim.summary()["spare_wait_hours"], 0)


if __name__ == "__main__":
    unittest.main()
