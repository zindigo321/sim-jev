"""Regression cases from the independent phase-2A audit, using real handlers."""

from copy import deepcopy
from dataclasses import replace
import json
import math
import unittest

from sim_jev import Config, MissionSpec, Simulation


def config(**changes):
    values = dict(horizon_hours=120, fleet_size=1, mission_rate_per_hour=0,
                  failure_rate_per_flight_hour=0, repair_bays=1, initial_spares=1,
                  reorder_target=0, reorder_interval_hours=24,
                  repair_duration_min_hours=4, repair_duration_max_hours=4,
                  lead_time_min_hours=8, lead_time_max_hours=8)
    values.update(changes)
    return replace(Config(), **values)


def replace_event_time(data, kind, entity, time):
    event = next(e for e in data["event_queue"]
                 if e["kind"] == kind and e["entity_id"] == entity)
    event["time"] = time
    data["event_queue"].sort(key=lambda e: (e["time"], e["priority"], e["sequence"]))


def replace_order_eta(data, time):
    order = data["orders"][0]
    order["expected_arrival"] = time
    replace_event_time(data, "order_arrival", order["id"], time)
    event = next(e for e in data["events"]
                 if e["event_type"] == "order_placed" and e["entity_id"] == order["id"])
    details = json.loads(event["details"])
    details["expected_arrival"] = time
    event["details"] = json.dumps(details, sort_keys=True)


class CheckpointAuditFixTests(unittest.TestCase):
    def repair_sim(self, **changes):
        sim = Simulation(config(**changes), [MissionSpec("A", 0, 2, 1, 10),
                                             MissionSpec("B", 7, 2, 1, 100)], [1])
        sim.run(1)
        return sim

    def order_sim(self, **changes):
        sim = Simulation(config(initial_spares=0, reorder_target=1, **changes), [])
        sim.run(0)
        return sim

    def test_unique_sorted_start_sequence_swap_is_rejected(self):
        sim = Simulation(config(), [MissionSpec("B", 1, 2, 1, 100),
                                    MissionSpec("A", 1, 2, 1, 10)])
        sim.run(0)
        data = sim.to_checkpoint()
        starts = [e for e in data["event_queue"] if e["kind"] == "mission_start"]
        starts[0]["sequence"], starts[1]["sequence"] = starts[1]["sequence"], starts[0]["sequence"]
        data["event_queue"].sort(key=lambda e: (e["time"], e["priority"], e["sequence"]))
        self.assertEqual(len({e["sequence"] for e in data["event_queue"]}), len(data["event_queue"]))
        with self.assertRaisesRegex(ValueError, "initialization identity"):
            Simulation.from_checkpoint(data)
        sim.run()
        self.assertEqual(sim.summary()["mission_value_earned"], 10)

    def test_start_sequence_uses_complete_table_after_consumed_starts(self):
        sim = Simulation(config(), [MissionSpec("early", 0, 1, 1, 10),
                                    MissionSpec("late", 5, 1, 1, 20)])
        sim.run(2)
        data = sim.to_checkpoint()
        start = next(e for e in data["event_queue"] if e["kind"] == "mission_start")
        self.assertEqual(start["sequence"], 2)
        self.assertEqual(Simulation.from_checkpoint(data).to_checkpoint(), data)
        start["sequence"] = 1
        with self.assertRaisesRegex(ValueError, "initialization identity"):
            Simulation.from_checkpoint(data)

    def test_synchronized_fixed_repair_delay_is_rejected(self):
        sim = self.repair_sim()
        data = sim.to_checkpoint()
        repair = data["repairs"][0]
        self.assertEqual(repair["due_at"], 5)
        repair["due_at"] = 100
        replace_event_time(data, "repair_complete", repair["id"], 100)
        with self.assertRaisesRegex(ValueError, r"due_at.*configured duration support"):
            Simulation.from_checkpoint(data)
        sim.run()
        self.assertEqual(sim.missions["B"].status, "success")

    def test_synchronized_fixed_order_delay_with_public_log_is_rejected(self):
        sim = self.order_sim(horizon_hours=20)
        data = sim.to_checkpoint()
        self.assertEqual(data["orders"][0]["expected_arrival"], 8)
        replace_order_eta(data, 100)
        with self.assertRaisesRegex(ValueError, r"expected_arrival.*configured duration support"):
            Simulation.from_checkpoint(data)
        sim.run()
        self.assertEqual(sim.on_hand, 1)

    def test_fixed_duration_rejects_adjacent_float_timestamps(self):
        for fixture, field in ((self.repair_sim, "due_at"), (self.order_sim, "expected_arrival")):
            sim = fixture()
            original = sim.to_checkpoint()
            table = "repairs" if field == "due_at" else "orders"
            due = original[table][0][field]
            for target in (math.nextafter(due, -math.inf), math.nextafter(due, math.inf)):
                with self.subTest(field=field, target=target):
                    data = deepcopy(original)
                    if table == "repairs":
                        data[table][0][field] = target
                        replace_event_time(data, "repair_complete", data[table][0]["id"], target)
                    else:
                        replace_order_eta(data, target)
                    with self.assertRaisesRegex(ValueError, "configured duration support"):
                        Simulation.from_checkpoint(data)

    def test_interval_endpoints_use_added_timestamp_boundaries(self):
        for fixture, table, field, limits in (
            (lambda: self.repair_sim(repair_duration_min_hours=4, repair_duration_max_hours=6),
             "repairs", "due_at", (5., 7.)),
            (lambda: self.order_sim(lead_time_min_hours=8, lead_time_max_hours=10),
             "orders", "expected_arrival", (8., 10.)),
        ):
            original = fixture().to_checkpoint()
            for target in (*limits, math.nextafter(limits[0], -math.inf),
                           math.nextafter(limits[1], math.inf)):
                with self.subTest(table=table, target=target):
                    data = deepcopy(original)
                    if table == "repairs":
                        data[table][0][field] = target
                        replace_event_time(data, "repair_complete", data[table][0]["id"], target)
                    else:
                        replace_order_eta(data, target)
                    if target in limits:
                        self.assertEqual(Simulation.from_checkpoint(data).to_checkpoint(), data)
                    else:
                        with self.assertRaisesRegex(ValueError, "configured duration support"):
                            Simulation.from_checkpoint(data)

    def test_completed_entities_also_validate_support(self):
        for sim, table, field, completed_field in (
            (self.repair_sim(), "repairs", "due_at", "completed_at"),
            (self.order_sim(), "orders", "expected_arrival", "arrived_at"),
        ):
            sim.run(20)
            original = sim.to_checkpoint()
            self.assertEqual(Simulation.from_checkpoint(original).to_checkpoint(), original)
            data = deepcopy(original)
            target = math.nextafter(data[table][0][field], math.inf)
            data[table][0][field] = data[table][0][completed_field] = target
            with self.assertRaisesRegex(ValueError, "configured duration support"):
                Simulation.from_checkpoint(data)

    def test_zero_duration_completed_repair_and_order_restore(self):
        sim = Simulation(config(initial_spares=0, reorder_interval_hours=1,
                                repair_duration_min_hours=0, repair_duration_max_hours=0,
                                lead_time_min_hours=0, lead_time_max_hours=0),
                         [MissionSpec("A", 0, 2, 1, 10)], [1])
        sim.run(1)
        self.assertEqual(sim.repairs["R000001"].completed_at, 1)
        self.assertEqual(sim.orders["O000001"].arrived_at, 1)
        data = sim.to_checkpoint()
        self.assertEqual(Simulation.from_checkpoint(data).to_checkpoint(), data)

    def test_fractional_accumulation_active_completed_and_adjacent_stops(self):
        cfg = config(initial_spares=1, reorder_target=1, reorder_interval_hours=.1,
                     repair_duration_min_hours=.1, repair_duration_max_hours=.1,
                     lead_time_min_hours=.1, lead_time_max_hours=.1)
        factory = lambda: Simulation(cfg, [MissionSpec("fraction", .1, 1, 1, 1)], [.2])
        start = .1 + .2
        due = start + .1
        self.assertNotEqual(due - start, .1)  # A subtraction-based test would reject this state.
        for stop in (start, math.nextafter(due, -math.inf), due,
                     math.nextafter(due, math.inf), .7):
            with self.subTest(stop=stop):
                sim = factory()
                sim.run(stop)
                self.assertEqual(sim.repairs["R000001"].started_at, start)
                self.assertEqual(sim.repairs["R000001"].due_at, due)
                self.assertEqual(sim.orders["O000001"].expected_arrival, due)
                data = sim.to_checkpoint()
                restored = Simulation.from_checkpoint(json.loads(json.dumps(data, allow_nan=False)))
                self.assertEqual(restored.to_checkpoint(), data)


if __name__ == "__main__":
    unittest.main()
