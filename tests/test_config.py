"""Validation tests for configuration and deterministic input contracts."""

import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from sim_jev.config import Config, load_config
from sim_jev.engine import Simulation
from sim_jev.models import MissionSpec


class ConfigValidationTests(unittest.TestCase):
    def test_example_configs_load_and_identify_synthetic_parameters(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("default", "stress"):
            with self.subTest(config=name):
                config = load_config(root / "configs" / f"{name}.json")
                config.validate()
                self.assertTrue(config.parameter_note)
                self.assertEqual(config.horizon_hours, 720)
                self.assertEqual(config.fleet_size, 12)

    def test_config_round_trip(self):
        config = Config()
        self.assertEqual(Config.from_dict(config.to_dict()), config)

    def test_illegal_numbers_types_and_ranges_are_rejected(self):
        cases = [
            {"horizon_hours": 0}, {"horizon_hours": -1},
            {"horizon_hours": math.inf}, {"horizon_hours": math.nan},
            {"fleet_size": 0}, {"fleet_size": -1}, {"fleet_size": 1.5},
            {"fleet_size": True}, {"seed": -1}, {"seed": True},
            {"mission_rate_per_hour": -0.1}, {"mission_rate_per_hour": "0.1"},
            {"mission_duration_min_hours": 0},
            {"mission_duration_min_hours": 8, "mission_duration_max_hours": 2},
            {"mission_aircraft_min": 0}, {"mission_aircraft_max": 1.5},
            {"mission_aircraft_min": 4, "mission_aircraft_max": 2},
            {"mission_value_min": -1},
            {"mission_value_min": 200, "mission_value_max": 100},
            {"failure_rate_per_flight_hour": -1},
            {"failure_rate_per_flight_hour": math.nan},
            {"repair_bays": -1}, {"repair_bays": 1.5},
            {"repair_duration_min_hours": -1},
            {"repair_duration_min_hours": 12, "repair_duration_max_hours": 4},
            {"initial_spares": -1}, {"initial_spares": 0.5},
            {"reorder_target": -1}, {"reorder_interval_hours": 0},
            {"lead_time_min_hours": -1},
            {"lead_time_min_hours": 5, "lead_time_max_hours": 2},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(Config(), **change).validate()

    def test_unknown_config_field_is_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_dict({"mission_probability": 1.5})

    def test_zero_support_and_failure_parameters_are_valid(self):
        config = replace(Config(), mission_rate_per_hour=0,
                         failure_rate_per_flight_hour=0, repair_bays=0,
                         initial_spares=0, reorder_target=0,
                         repair_duration_min_hours=0, repair_duration_max_hours=0,
                         lead_time_min_hours=0, lead_time_max_hours=0)
        config.validate()
        result = Simulation(config).run()
        self.assertEqual(result["missions_offered"], 0)
        self.assertEqual(result["failures"], 0)

    def test_invalid_json_root_and_value_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            for data in ([], {"fleet_size": -1}, {"unexpected": 1}):
                with self.subTest(data=data):
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)


class InputValidationTests(unittest.TestCase):
    def test_invalid_mission_fields_are_rejected(self):
        cases = [
            ("", 0, 1, 1, 1), ("M", -1, 1, 1, 1),
            ("M", math.nan, 1, 1, 1), ("M", 0, 0, 1, 1),
            ("M", 0, -1, 1, 1), ("M", 0, math.inf, 1, 1),
            ("M", 0, 1, 0, 1), ("M", 0, 1, 1.5, 1),
            ("M", 0, 1, True, 1), ("M", 0, 1, 1, -1),
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                spec = MissionSpec(*data)
                Simulation(Config(), [spec])

    def test_duplicate_mission_ids_are_rejected(self):
        tasks = [MissionSpec("M", 0, 1, 1, 1), MissionSpec("M", 2, 1, 1, 1)]
        with self.assertRaises(ValueError):
            Simulation(Config(), tasks)

    def test_invalid_lifetime_overrides_are_rejected(self):
        config = replace(Config(), fleet_size=2)
        for values in ([1], [1, 2, 3], [-1, 2], [math.nan, 2], [True, 2]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Simulation(config, [], values)


if __name__ == "__main__":
    unittest.main()
