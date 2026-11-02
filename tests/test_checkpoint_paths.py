"""Checkpoint tests drive genuine handlers, serialized restores and subprocesses."""
from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sim_jev import Config, MissionSpec, Simulation
from sim_jev.config import load_config
from scripts.checkpoint_verify import compare_simulations

ROOT = Path(__file__).resolve().parents[1]


def quiet(**updates):
    params = dict(horizon_hours=12, fleet_size=2, mission_rate_per_hour=0,
                  failure_rate_per_flight_hour=0, repair_bays=1, initial_spares=0,
                  reorder_target=0, reorder_interval_hours=24,
                  repair_duration_min_hours=2, repair_duration_max_hours=2,
                  lead_time_min_hours=3, lead_time_max_hours=3)
    params.update(updates)
    return replace(Config(), **params)


def roundtrip(sim):
    return Simulation.from_checkpoint(json.loads(json.dumps(sim.to_checkpoint(), allow_nan=False)))


class CheckpointPathTests(unittest.TestCase):
    def check_stops(self, factory, stops):
        full = factory()
        full.run()
        for stop in stops:
            with self.subTest(stop=stop):
                part = factory()
                part.run(until=stop)
                restored = roundtrip(part)
                self.assertEqual(part.to_checkpoint(), restored.to_checkpoint())
                restored.run()
                compare_simulations(full, restored)
        serial = factory()
        for stop in sorted(set(stops)):
            serial.run(until=stop)
            serial = roundtrip(serial)
        serial.run()
        compare_simulations(full, serial)

    def test_default_and_stress_multiple_stops(self):
        for name in ("default", "stress"):
            config = load_config(ROOT / "configs" / f"{name}.json")
            self.check_stops(lambda: Simulation(config), (0, .125, 35.375, 168, 672, 720))

    def test_flying_waiting_repairing_transit_paths(self):
        config = quiet(fleet_size=3, initial_spares=1, reorder_interval_hours=2)
        specs = [MissionSpec("M", 0, 10, 3, 100)]
        factory = lambda: Simulation(config, specs, [1, 1, 8])
        sim = factory()
        sim.run(until=.5)
        self.assertTrue(all(a.state == "flying" for a in sim.aircraft.values()))
        sim.run(until=2)
        self.assertEqual([a.state for a in sim.aircraft.values()], ["repairing", "waiting_repair", "idle"])
        self.assertGreater(sim.in_transit, 0)
        self.check_stops(factory, (0, .5, 1, 2, 2.5, 3, 5, 12))

    def test_same_time_physical_events_and_adjacent_floats(self):
        config = quiet(fleet_size=4, initial_spares=1, reorder_target=2,
                       repair_duration_min_hours=3, repair_duration_max_hours=3,
                       lead_time_min_hours=4, lead_time_max_hours=4)
        tasks = [MissionSpec("A", 0, 5, 1, 1), MissionSpec("B", 0, 4, 1, 1),
                 MissionSpec("C", 0, 4, 1, 1), MissionSpec("D", 4, 1, 1, 1)]
        factory = lambda: Simulation(config, tasks, [1, 4, math.inf, math.inf])
        before, exact = factory(), factory()
        before.run(until=math.nextafter(4, -math.inf))
        self.assertEqual(before.missions["B"].status, "running")
        self.assertEqual(before.repairs["R000001"].status, "repairing")
        self.assertEqual(before.orders["O000001"].status, "in_transit")
        exact.run(until=4)
        self.assertEqual(exact.missions["B"].status, "failed")
        self.assertEqual(exact.missions["C"].status, "success")
        self.assertEqual(exact.missions["D"].status, "running")
        types = [e["event_type"] for e in exact.events if e["time"] == 4]
        for left, right in zip(("failure", "repair_complete", "order_arrival", "mission_success"),
                               ("repair_complete", "order_arrival", "mission_success", "mission_start")):
            self.assertLess(types.index(left), types.index(right))
        self.check_stops(factory, (0, .25, math.nextafter(4, -math.inf), 4,
                                   math.nextafter(4, math.inf), 12))

    def test_failure_completion_boundary_classification_strict(self):
        for end in (.3, .6, 1., 168., 672., 720.):
            for life in (math.nextafter(end, -math.inf), end, math.nextafter(end, math.inf)):
                with self.subTest(end=end, life=life):
                    config = quiet(horizon_hours=end, fleet_size=1)
                    factory = lambda: Simulation(config, [MissionSpec("M", 0, end, 1, 1)], [life])
                    sim = factory()
                    for stop in (0, end / 7, end / 3, math.nextafter(end, -math.inf), end):
                        sim.run(until=stop)
                        sim = roundtrip(sim)
                    self.assertEqual(sim.missions["M"].status, "failed" if life <= end else "success")
                    full = factory()
                    full.run()
                    compare_simulations(full, sim)

    def test_fractional_previous_flight_and_zero_time_cascade(self):
        config = quiet(horizon_hours=.6, fleet_size=1)
        factory = lambda: Simulation(config, [MissionSpec("A", 0, .3, 1, 1),
                                              MissionSpec("B", .3, .3, 1, 1)], [.6])
        self.check_stops(factory, (0, .1, .2, .3, .5, .6))
        config = quiet(fleet_size=1, reorder_interval_hours=1,
                       repair_duration_min_hours=0, repair_duration_max_hours=0,
                       lead_time_min_hours=0, lead_time_max_hours=0)
        factory = lambda: Simulation(config, [MissionSpec("A", 0, 5, 1, 1),
                                              MissionSpec("B", 1, 2, 1, 1)], [1])
        self.check_stops(factory, (0, math.nextafter(1, -math.inf), 1, math.nextafter(1, math.inf), 12))

    def test_720_physical_events_and_legacy_start_are_preserved(self):
        config = quiet(horizon_hours=720, fleet_size=2, initial_spares=1,
                       reorder_target=2, lead_time_min_hours=720, lead_time_max_hours=720,
                       repair_duration_min_hours=4, repair_duration_max_hours=4)
        specs = [MissionSpec("A", 715, 10, 1, 1), MissionSpec("B", 718, 2, 1, 1),
                 MissionSpec("C", 720, 5, 1, 1), MissionSpec("FUTURE", 721, 1, 1, 1)]
        factory = lambda: Simulation(config, specs, [1, math.inf])
        self.check_stops(factory, (0, 168, 672, 716, math.nextafter(720, -math.inf), 720))
        sim = factory()
        sim.run()
        self.assertEqual(sim.missions["B"].status, "success")
        self.assertEqual(sim.missions["C"].status, "running")
        self.assertEqual(sim.missions["FUTURE"].status, "pending")
        self.assertEqual(sim.repairs["R000001"].completed_at, 720)
        self.assertEqual(sim.orders["O000001"].arrived_at, 720)

    def test_rng_continuation_and_reset_inputs(self):
        config = replace(Config(), horizon_hours=100)
        factories = [lambda: Simulation(config), lambda: Simulation(config, []),
                     lambda: Simulation(config, [MissionSpec("M", 1, 10, 2, 9)],
                                        [1, math.inf] + [3] * (config.fleet_size - 2))]
        for factory in factories:
            sim = factory()
            sim.run(until=35)
            restored = roundtrip(sim)
            for name in sim._rngs:
                self.assertIsNot(sim._rngs[name], restored._rngs[name])
                self.assertEqual(sim._rngs[name].random(32).tolist(), restored._rngs[name].random(32).tolist())
            for seed in (None, 17):
                sim.reset(seed=seed)
                restored.reset(seed=seed)
                sim.run()
                restored.run()
                compare_simulations(sim, restored)

    def test_real_new_process_restore_explicit_inputs(self):
        config = quiet(horizon_hours=720, fleet_size=2, reorder_interval_hours=24)
        specs = [MissionSpec("M", 0, 10, 2, 10), MissionSpec("LATE", 672, 70, 1, 4)]
        full = Simulation(config, specs, [1, math.inf])
        full.run()
        with tempfile.TemporaryDirectory(prefix="checkpoint-process-") as temp:
            source, output = Path(temp) / "pause.json", Path(temp) / "final.json"
            code = ("from sim_jev import Simulation; import sys; "
                    "s=Simulation.load_checkpoint(sys.argv[1]); s.run(); "
                    "s.save_checkpoint(sys.argv[2])")
            for stop in (0, .5, 2, 25, 28, 168, 672, 720):
                with self.subTest(stop=stop):
                    source = Path(temp) / f"pause-{stop}.json"
                    output = Path(temp) / f"final-{stop}.json"
                    part = Simulation(config, specs, [1, math.inf])
                    part.run(until=stop)
                    part.save_checkpoint(source)
                    result = subprocess.run([sys.executable, "-c", code, str(source), str(output)],
                                            cwd=ROOT, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    compare_simulations(full, Simulation.load_checkpoint(output))


if __name__ == "__main__":
    unittest.main()
