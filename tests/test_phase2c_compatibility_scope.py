"""Historical selection must not substitute nested copies for delivery paths."""

import unittest

from scripts.phase2c_compatibility import select_members


def members():
    base = "sim-jev/runs/phase2b-20261102-170159/"
    files = ["sim-jev/sim_jev/engine.py"]
    for scenario in ("default", "stress"):
        for stop in ("0", "0.125", "35.375", "168", "336", "504", "672", "720"):
            files.append(f"{base}01-2a-fixed/archived-examples/{scenario}-0-{stop}.checkpoint.json")
            files.append(f"{base}04-final-weekly/{scenario}-0/pause-{stop}.checkpoint.json")
        for filename in ("config.json", "events.csv", "missions.csv", "states.csv", "repairs.csv",
                         "orders.csv", "summary.json", "state.json", "overview.png"):
            files.append(f"{base}04-final-validation/{scenario}_seed42/{filename}")
    files += [f"{base}04-final-weekly/stress-0/{kind}.checkpoint.json" for kind in ("in_repair", "in_transit")]
    return files


class CompatibilityScopeTests(unittest.TestCase):
    def test_nested_copies_do_not_duplicate_or_replace_exact_delivery_members(self):
        original = members()
        nested = ["sim-jev/runs/copied-source/" + name for name in original]
        selected = select_members(original + nested)
        self.assertEqual(tuple(map(len, selected)), (16, 18, 18))
        for removed in (original[0], original[1], original[-1]):
            with self.subTest(removed=removed):
                with self.assertRaises(AssertionError):
                    select_members([name for name in original if name != removed] + nested)

    def test_missing_scientific_file_cannot_be_replaced_by_prior_validation_run(self):
        original = members()
        target = next(name for name in original if name.endswith("default_seed42/summary.json"))
        prior = target.replace("04-final-validation", "01-2a-fixed/validation")
        with self.assertRaises(AssertionError):
            select_members([name for name in original if name != target] + [prior])


if __name__ == "__main__":
    unittest.main()
