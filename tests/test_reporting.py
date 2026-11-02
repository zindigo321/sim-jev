"""Exercise real serialization, rendering, and the public command-line entry."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pandas as pd

from sim_jev.config import Config
from sim_jev.engine import Simulation
from sim_jev.models import MissionSpec
from sim_jev.reporting import write_run


ROOT = Path(__file__).resolve().parents[1]


class ReportingTests(unittest.TestCase):
    def setUp(self):
        runs = ROOT / "runs"
        runs.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="test-reporting-", dir=runs)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def run_cli(self, config: Path, output: Path, *extra: str):
        return subprocess.run(
            [sys.executable, "-m", "sim_jev", "--config", str(config), "--output", str(output), *extra],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def test_zero_mission_outputs_have_headers_and_readable_plot(self):
        config = replace(Config(), horizon_hours=10, mission_rate_per_hour=0, initial_spares=0, reorder_target=0)
        simulation = Simulation(config, missions=[])
        simulation.run()
        destination = write_run(simulation, self.folder / "empty")
        expected = {
            "config.json", "events.csv", "missions.csv", "summary.json", "states.csv",
            "repairs.csv", "orders.csv", "state.json", "runtime.json", "overview.png",
        }
        self.assertEqual({path.name for path in destination.iterdir()}, expected)
        for name in ("missions", "repairs", "orders"):
            frame = pd.read_csv(destination / f"{name}.csv")
            self.assertEqual(len(frame), 0)
            self.assertIn("id", frame.columns)
        summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary, simulation.summary())
        state = json.loads((destination / "state.json").read_text(encoding="utf-8"))
        self.assertIsInstance(state, dict)
        from matplotlib import image

        pixels = image.imread(destination / "overview.png")
        self.assertGreater(pixels.shape[0], 500)
        self.assertGreater(pixels.shape[1], 500)
        self.assertGreater(float(pixels.std()), 0.01)

    def test_existing_output_is_preserved(self):
        destination = self.folder / "existing"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("user result", encoding="utf-8")
        simulation = Simulation(replace(Config(), mission_rate_per_hour=0), missions=[])
        with self.assertRaisesRegex(ValueError, "not empty"):
            write_run(simulation, destination)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "user result")
        self.assertEqual(list(destination.iterdir()), [sentinel])

    def test_mission_csv_keeps_censored_result_and_actual_exposure(self):
        simulation = Simulation(
            replace(
                Config(), horizon_hours=3, fleet_size=2,
                failure_rate_per_flight_hour=0, initial_spares=0, reorder_target=0,
            ),
            missions=[
                MissionSpec("finished", 0, 1, 1, 100),
                MissionSpec("ongoing", 2, 4, 1, 200),
            ],
        )
        simulation.run()
        destination = write_run(simulation, self.folder / "censored")
        missions = pd.read_csv(destination / "missions.csv").set_index("id")
        self.assertEqual(missions.loc["finished", "status"], "success")
        self.assertEqual(missions.loc["finished", "earned_value"], 100)
        self.assertEqual(missions.loc["ongoing", "status"], "running")
        self.assertTrue(pd.isna(missions.loc["ongoing", "actual_end"]))
        self.assertEqual(missions.loc["ongoing", "actual_duration_hours"], 1)
        self.assertEqual(missions.loc["ongoing", "flight_hours"], 1)
        self.assertEqual(missions.loc["ongoing", "earned_value"], 0)
        self.assertEqual(json.loads(missions.loc["ongoing", "assigned_aircraft"]), ["A001"])
        states = pd.read_csv(destination / "states.csv")
        self.assertEqual(states.iloc[-1]["time"], 3)
        self.assertEqual(states.iloc[-1]["flying"], 1)

    def test_cli_seed_override_and_reproducible_scientific_outputs(self):
        config = replace(Config(), horizon_hours=20, mission_rate_per_hour=1)
        config_path = self.folder / "scenario.json"
        config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
        destinations = [self.folder / "first", self.folder / "second"]
        for destination in destinations:
            result = self.run_cli(config_path, destination, "--seed", "123")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("summary", json.loads(result.stdout))
        for name in ("config.json", "events.csv", "missions.csv", "summary.json", "states.csv", "repairs.csv", "orders.csv", "state.json"):
            self.assertEqual((destinations[0] / name).read_bytes(), (destinations[1] / name).read_bytes(), name)
        actual = json.loads((destinations[0] / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(actual["seed"], 123)

    def test_cli_rejects_invalid_config_and_nonempty_output(self):
        invalid_path = self.folder / "invalid.json"
        invalid_path.write_text('{"initial_spares": -1}', encoding="utf-8")
        result = self.run_cli(invalid_path, self.folder / "invalid-output")
        self.assertEqual(result.returncode, 2)
        self.assertIn("initial_spares", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.folder / "invalid-output").exists())

        valid_path = self.folder / "valid.json"
        valid_path.write_text('{"mission_rate_per_hour": 0}', encoding="utf-8")
        sentinel = self.folder / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        result = self.run_cli(valid_path, self.folder)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not empty", result.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
