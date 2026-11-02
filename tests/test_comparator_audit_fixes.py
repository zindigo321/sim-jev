"""Regressions for previously invisible changes to future reset behavior."""
from dataclasses import replace
import subprocess
import sys
import unittest

from sim_jev import Simulation
from sim_jev.config import Config
from sim_jev.models import MissionSpec
from scripts.checkpoint_verify import compare_simulations


class ComparatorAuditTests(unittest.TestCase):
    def test_changed_reset_inputs_are_detected_before_reset(self):
        original = Simulation(replace(Config(), fleet_size=1),
                              [MissionSpec("A", 1, 2, 1, 10)], [100])
        original.run(0)
        for name, value in (("_input_missions", ()), ("_input_missions", None),
                            ("_initial_lifetimes", None), ("_initial_lifetimes", (200,)),
                            ("config", replace(original.config, mission_rate_per_hour=0))):
            with self.subTest(name=name, value=value):
                branch = original.clone()
                setattr(branch, name, value)
                self.assertEqual(original.events, branch.events)
                with self.assertRaises(AssertionError):
                    compare_simulations(original, branch)
        branch = original.clone()
        branch._input_missions = ()
        original.reset()
        branch.reset()
        self.assertEqual(len(original.missions), 1)
        self.assertEqual(len(branch.missions), 0)

    def test_none_and_explicit_infinite_lifetimes_differ(self):
        sim = Simulation(replace(Config(), failure_rate_per_flight_hour=0), [])
        sim.run(0)
        branch = sim.clone()
        branch._initial_lifetimes = (float("inf"),) * sim.config.fleet_size
        with self.assertRaises(AssertionError):
            compare_simulations(sim, branch)

    def test_optimized_verifier_fails_explicitly(self):
        result = subprocess.run([sys.executable, "-O", "scripts/checkpoint_verify.py", "--help"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("optimized Python", result.stderr)
