"""The decision barrier and versioned command-line entries use real events."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand
from sim_jev.versions import WEEKLY_MODEL_VERSION, KEYED_RNG_PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[1]


class WeeklyBarrierTests(unittest.TestCase):
    def test_malformed_policy_and_rng_domain_raise_value_error(self):
        from sim_jev.keyed_rng import KeyedRNG
        with self.assertRaises(ValueError):
            PolicySpec.from_dict({"kind": [], "name": "bad", "commands": []})
        with self.assertRaises(ValueError):
            KeyedRNG(42, "bad-domain", domain=[])

    def test_infeasible_mandatory_does_not_hide_illegal_script(self):
        config = replace(Config(), fleet_size=1, failure_rate_per_flight_hour=0)
        missions = [MissionSpec("A", 1, 2, 1, 10), MissionSpec("B", 1, 2, 1, 20)]
        rule = RuleSpec(0, 168, 0, 0, "infeasible", mandatory_ids=("A", "B"))
        for commands in ((), (WeeklyCommand(0, ("NOT_OFFERED",)),)):
            sim = WeeklySimulation(config, missions, rules=[rule],
                                   policy=PolicySpec(kind="scripted", commands=commands))
            with self.assertRaises(ValueError):
                sim.run(0)

    def test_predecision_failure_supply_zero_repair_closure(self):
        config = replace(Config(), fleet_size=1, failure_rate_per_flight_hour=0,
                         initial_spares=0, reorder_target=1, repair_bays=1,
                         repair_duration_min_hours=0, repair_duration_max_hours=0,
                         lead_time_min_hours=168, lead_time_max_hours=168)
        sim = WeeklySimulation(config, [MissionSpec("first", 0, 168, 1, 10),
                                         MissionSpec("second", 168, 1, 1, 20)], [168])
        sim.run(168)
        kinds = [r["event_type"] for r in sim.events if r["time"] == 168]
        self.assertLess(kinds.index("failure"), kinds.index("order_arrival"))
        self.assertLess(kinds.index("repair_complete"), kinds.index("weekly_offer"))
        self.assertLess(kinds.index("weekly_offer"), kinds.index("weekly_command"))
        self.assertLess(kinds.index("weekly_command"), kinds.index("daily_command"))
        self.assertLess(kinds.index("daily_command"), kinds.index("mission_start"))
        self.assertEqual(sim.missions["first"].status, "failed")
        self.assertEqual(sim.missions["second"].status, "running")
        restored = WeeklySimulation.from_checkpoint(sim.to_checkpoint())
        restored.run(169)
        self.assertEqual(restored.missions["second"].status, "success")

    def test_terminal_failure_zero_repair_is_once_and_no_new_command(self):
        config = replace(Config(), fleet_size=1, failure_rate_per_flight_hour=0,
                         repair_duration_min_hours=0, repair_duration_max_hours=0,
                         lead_time_min_hours=0, lead_time_max_hours=0)
        sim = WeeklySimulation(config, [MissionSpec("last", 719, 1, 1, 10),
                                         MissionSpec("outside", 720, 1, 1, 20)], [1])
        sim.run()
        self.assertEqual(sim.missions["last"].status, "failed")
        self.assertEqual(sim.missions["outside"].status, "pending")
        at_end = [row for row in sim.events if row["time"] == 720]
        self.assertTrue(any(row["event_type"] == "repair_complete" for row in at_end))
        self.assertFalse(any(row["event_type"] in {"weekly_offer", "weekly_command", "daily_command", "reorder_check"}
                             for row in at_end))
        initial = sim.to_checkpoint()
        sim.run()
        self.assertEqual(initial, sim.to_checkpoint())

    def test_actual_weekly_cli_resume_and_model_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="weekly-cli-") as directory:
            folder = Path(directory)
            config = replace(Config(), mission_rate_per_hour=.01)
            source = folder / "input.json"
            source.write_text(json.dumps({"model_version": WEEKLY_MODEL_VERSION,
                                          "scenario_id": "cli-test", "config": config.to_dict(),
                                          "rules": [], "policy": PolicySpec().to_dict()}), encoding="utf-8")
            checkpoint = folder / "pause.json"
            for args in (
                ["--mode", "weekly", "--config", str(source), "--until", "168",
                 "--checkpoint-out", str(checkpoint), "--output", str(folder / "pause")],
                ["--resume", str(checkpoint), "--output", str(folder / "resumed")],
                ["--mode", "weekly", "--config", str(source), "--output", str(folder / "whole")],
            ):
                result = subprocess.run([sys.executable, "-m", "sim_jev", *args], cwd=ROOT,
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("events.csv", "summary.json", "weekly-ledger.json", "random-manifest.json"):
                self.assertEqual((folder / "whole" / name).read_bytes(),
                                 (folder / "resumed" / name).read_bytes(), name)
            runtime = json.loads((folder / "resumed/runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["model_version"], WEEKLY_MODEL_VERSION)
            self.assertEqual(runtime["rng_protocol_version"], KEYED_RNG_PROTOCOL_VERSION)
            self.assertEqual(runtime["checkpoint_schema_version"], 1)
            self.assertEqual(runtime["policy_identity"]["kind"], "mandatory-first")
            result = subprocess.run([sys.executable, "-m", "sim_jev", "--mode", "legacy", "--resume",
                                     str(checkpoint), "--output", str(folder / "mismatch")], cwd=ROOT,
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertFalse((folder / "mismatch").exists())
