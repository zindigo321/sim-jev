"""Independent branch accounting accepts real paths and rejects altered output."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_phase2c_planning as fixtures
from scripts.planning_verify import audit_planning_branch
from sim_jev import MissionSpec
from sim_jev.weekly import WeeklySimulation


class PlanningLogAuditTests(unittest.TestCase):
    def complex_branch(self):
        _, request = fixtures.LegalPlanningTests().complex_request()
        return request, fixtures.branch(request, ("NOW",))

    def test_origin_aware_inventory_carry_wait_and_repair_match_without_summary(self):
        request, result = self.complex_branch()
        with patch.object(WeeklySimulation, "summary", side_effect=AssertionError("self-verification")), \
             patch.object(WeeklySimulation, "_cohort_summary", side_effect=AssertionError("self-verification")):
            audit = audit_planning_branch(result, request)
        self.assertTrue(audit["verified"])
        self.assertTrue(audit["request_externally_bound"])
        self.assertEqual(audit["counts"]["earned_value"], 10)
        self.assertEqual(audit["metrics_delta"]["flight_hours"], 4)
        self.assertEqual(audit["metrics_delta"]["spare_wait_hours"], 24)
        self.assertEqual(audit["metrics_delta"]["repairs_completed"], 2)

    def test_real_random_failure_and_zero_duration_physical_paths(self):
        for seed in range(5):
            request = fixtures.capture(WeeklySimulation(fixtures.config(
                seed=seed, failure_rate_per_flight_hour=.2, repair_duration_min_hours=0,
                repair_duration_max_hours=0, lead_time_min_hours=0, lead_time_max_hours=0),
                [MissionSpec("A", 0, 20, 1, 10), MissionSpec("B", 4, 30, 1, 20)]), 0)
            for accepted in ((), ("A",), ("A", "B")):
                with self.subTest(seed=seed, accepted=accepted):
                    result = fixtures.branch(request, accepted, seed=seed)
                    self.assertTrue(audit_planning_branch(result, request)["verified"])

    def test_terminal_and_unresolved_cohorts_have_exact_as_of_accounting(self):
        for duration, success in ((1, 1), (2, 0)):
            request = fixtures.capture(WeeklySimulation(fixtures.config(),
                                                        [MissionSpec("END", 719, duration, 1, 10)]), 672)
            result = fixtures.branch(request, ("END",))
            audit = audit_planning_branch(result, request)
            self.assertEqual(audit["counts"]["success"], success)
            self.assertEqual(audit["counts"]["unfinished"], 1 - success)
            self.assertEqual(audit["end"] - audit["origin"], 48)

    def test_each_cohort_field_score_and_closed_snapshot_corruption_is_rejected(self):
        request, original = self.complex_branch()
        for name, value in original["counts"].items():
            if name == "validity":
                continue
            with self.subTest(field=name):
                result = deepcopy(original)
                result["counts"][name] = 999 if value is None else value + 123
                with self.assertRaises(AssertionError):
                    audit_planning_branch(result, request)
        mutations = [lambda r: r.__setitem__("score", -1),
                     lambda r: r.__setitem__("unresolved", 999),
                     lambda r: r["validity"].__setitem__("DSR", False),
                     lambda r: r["score_components"].__setitem__("RCR", 999),
                     lambda r: r["counts"]["validity"].__setitem__("DSR", False),
                     lambda r: r["final_state"]["weekly_state"]["closed_cohort_snapshots"][0]["counts"].__setitem__("success", 999),
                     lambda r: r["final_state"]["weekly_state"]["closed_cohort_snapshots"][0].__setitem__("as_of", 335)]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                result = deepcopy(original)
                mutate(result)
                with self.assertRaises(AssertionError):
                    audit_planning_branch(result, request)

    def test_physical_metrics_inventory_eta_and_event_corruption_is_rejected(self):
        request, original = self.complex_branch()
        for field in original["metrics_delta"]:
            with self.subTest(metric=field):
                result = deepcopy(original)
                result["metrics_delta"][field] += 1
                with self.assertRaises(AssertionError):
                    audit_planning_branch(result, request)
        mutations = [lambda r: r["final_state"].__setitem__("on_hand", 999),
                     lambda r: r["final_state"]["aircraft"][0].__setitem__("flight_hours", 999),
                     lambda r: r["final_state"]["orders"][0].__setitem__("expected_arrival", 999),
                     lambda r: r["final_state"]["history"][0].__setitem__("on_hand", 999),
                     lambda r: r["final_state"]["events"].pop(2),
                     lambda r: r["final_state"]["repairs"][1].__setitem__("spare_wait_hours", 999)]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                result = deepcopy(original)
                mutate(result)
                with self.assertRaises(AssertionError):
                    audit_planning_branch(result, request)

    def test_origin_changes_require_correct_legal_input_and_external_binding(self):
        request, result = self.complex_branch()
        corrupted = deepcopy(result)
        corrupted["final_state"]["origin"]["public_state"]["inventory"]["on_hand"] += 1
        with self.assertRaises(AssertionError):
            audit_planning_branch(corrupted)
        with self.assertRaises(AssertionError):
            audit_planning_branch(corrupted, request)
        self.assertFalse(audit_planning_branch(result)["request_externally_bound"])


if __name__ == "__main__":
    unittest.main()
