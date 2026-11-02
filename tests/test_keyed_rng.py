"""Semantic entity-key pairing, strict manifests and real process portability."""

import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest

from sim_jev.keyed_rng import KeyedRNG, canonical_key_bytes


def component(aid, generation=0, purpose="lifetime"):
    return dict(entity_type="component", entity_id=f"{aid}/LRU0",
                generation=generation, purpose=purpose)


class KeyedRNGTests(unittest.TestCase):
    def test_other_aircraft_extra_repairs_do_not_move_keys_or_values(self):
        first, second = KeyedRNG(17, "paired"), KeyedRNG(17, "paired")
        first.exponential(0.1, **component("A001"))
        for generation in range(4):
            second.exponential(0.1, **component("A001", generation))
            second.uniform(1, 4, **component("A001", generation, "repair_duration"))
        for sample in (first, second):
            sample.exponential(0.1, **component("A002", 2))
            sample.uniform(1, 4, **component("A002", 2, "repair_duration"))
        self.assertEqual(first.manifest[-2:], second.manifest[-2:])

    def test_order_independence_repeat_and_distribution_reuse_rejection(self):
        one, two = KeyedRNG(1, "s"), KeyedRNG(1, "s")
        left = one.uniform(0, 1, **component("A001"))
        right = one.uniform(0, 1, **component("A002"))
        self.assertEqual(two.uniform(0, 1, **component("A002")), right)
        self.assertEqual(two.uniform(0, 1, **component("A001")), left)
        self.assertEqual(one.uniform(0, 1, **component("A001")), left)
        self.assertEqual(len(one.manifest), 2)
        with self.assertRaisesRegex(ValueError, "different distributions"):
            one.uniform(0, 2, **component("A001"))

    def test_domains_scenarios_and_generation_are_separate(self):
        values = [KeyedRNG(2, scenario, domain).uniform(0, 1, **component("A001", generation))
                  for domain, scenario, generation in (
                      ("execution", "s", 0), ("planning", "s", 0),
                      ("execution", "other", 0), ("execution", "s", 1))]
        self.assertEqual(len(set(values)), 4)

    def test_json_restore_appends_without_replaying_historical_draws(self):
        first = KeyedRNG(3, "s")
        first.exponential(0, **component("A001"))
        first.uniform(0.3, 0.3, **component("A002"))
        state = json.loads(json.dumps(first.to_dict(), allow_nan=False))
        restored = KeyedRNG.from_dict(state)
        self.assertEqual(restored.to_dict(), state)
        self.assertEqual(restored.exponential(0, **component("A001")), math.inf)
        self.assertEqual(restored.to_dict(), state)
        self.assertEqual(restored.integer(2, 4, **component("A003")),
                         first.integer(2, 4, **component("A003")))
        self.assertEqual(restored.to_dict(), first.to_dict())
        state["manifest"].clear()
        self.assertEqual(len(restored.manifest), 3)

    def test_strict_state_rejects_corrupt_identity_and_boolean_counter(self):
        rng = KeyedRNG(2, "s")
        rng.uniform(0, 1, **component("A001"))
        pristine = rng.to_dict()
        for mutate in (
            lambda x: x.update(seed=True),
            lambda x: x.update(unexpected=1),
            lambda x: x["manifest"][0]["key"].update(generation=True),
            lambda x: x["manifest"][0].update(key_sha256="0" * 64),
            lambda x: x["manifest"].append(copy.deepcopy(x["manifest"][0])),
            lambda x: x["manifest"][0].update(transform_version="unknown"),
        ):
            broken = copy.deepcopy(pristine)
            mutate(broken)
            with self.assertRaises(ValueError):
                KeyedRNG.from_dict(broken)

    def test_keys_are_unambiguous_and_not_python_hash_dependent(self):
        first, second = KeyedRNG(4, "a/b"), KeyedRNG(4, "a")
        first.uniform(0, 1, entity_type="x", entity_id="c", purpose="d")
        second.uniform(0, 1, entity_type="b/x", entity_id="c", purpose="d")
        self.assertNotEqual(canonical_key_bytes(first.manifest[0]["key"]),
                            canonical_key_bytes(second.manifest[0]["key"]))
        code = ("from sim_jev.keyed_rng import KeyedRNG; import json; "
                "r=KeyedRNG(4,'a/b'); r.uniform(0,1,entity_type='x',entity_id='c',purpose='d'); "
                "print(json.dumps(r.to_dict(),sort_keys=True))")
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), first.to_dict())

    def test_daily_supply_opportunity_identity_does_not_use_order_position(self):
        first, second = KeyedRNG(42, "s"), KeyedRNG(42, "s")
        first.uniform(8, 12, entity_type="supplier", entity_id="single/LRU0",
                      generation=0, purpose="lead_time")
        for rng in (first, second):
            rng.uniform(8, 12, entity_type="supplier", entity_id="single/LRU0",
                        generation=3, purpose="lead_time")
        self.assertEqual(first.manifest[-1], second.manifest[-1])


if __name__ == "__main__":
    unittest.main()
