"""Independent cohort accounting must detect plausible report-only corruption."""
from copy import deepcopy
import math
import unittest

from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand
from scripts.weekly_verify import audit_business_log


def fixture(until=169):
    config = Config(fleet_size=1, mission_rate_per_hour=0, failure_rate_per_flight_hour=0,
                    initial_spares=1, reorder_target=0, repair_bays=1,
                    repair_duration_min_hours=100, repair_duration_max_hours=100)
    missions = [MissionSpec("fails", 0, 2, 1, 5), MissionSpec("unavailable", 3, 1, 1, 7),
                MissionSpec("rejected", 4, 1, 1, 9), MissionSpec("cross", 167, 2, 1, 11)]
    policy = PolicySpec("scripted", "audit-fixture", tuple(
        WeeklyCommand(point, ("fails", "unavailable", "cross") if point == 0 else ())
        for point in (0, 168, 336, 504, 672)))
    # Deliberately reverse obligation order: command normalization must not
    # cause the audit to confuse a rule's list order with changed obligations.
    rules = [RuleSpec(0, 168, 0, 0, "fixture", ("cross", "fails"))]
    simulation = WeeklySimulation(config, missions, policy=policy, rules=rules,
                                  initial_lifetimes=[1])
    simulation.run(until)
    return simulation


class CohortAuditTests(unittest.TestCase):
    def test_report_corruption_in_every_cohort_field_is_rejected(self):
        simulation = fixture()
        original = simulation.summary()
        mutations = {
            "offered": 9, "accepted": 9, "rejected": 9, "success": 9,
            "failed": 9, "accepted_not_executed": 9, "pending_execution": 9,
            "running": 9, "unfinished": 9, "launched": 9,
            "offered_value": original["cohorts"][0]["offered_value"] + 123,
            "accepted_value": -1, "earned_value": -1,
            "mandatory_shortfall_as_of": 9, "mandatory_shortfall_terminal": 9,
            "DSR": -1, "RCR": -1, "ASR": -1, "acceptance_rate": -1,
            "window_start": 1, "window_end": math.nextafter(168., math.inf),
            "validity": {"DSR": False, "RCR": True, "ASR": True, "acceptance_rate": True},
        }
        for field, damaged in mutations.items():
            with self.subTest(field=field):
                summary = deepcopy(original)
                summary["cohorts"][0][field] = damaged
                simulation.summary = lambda: summary
                with self.assertRaises(AssertionError):
                    audit_business_log(simulation)

    def test_closed_snapshot_corruption_in_every_field_is_rejected(self):
        simulation = fixture()
        original = deepcopy(simulation.weekly_state["closed_cohort_snapshots"])
        for field, value in original[0]["counts"].items():
            with self.subTest(field=field):
                simulation.weekly_state["closed_cohort_snapshots"] = deepcopy(original)
                damaged = False if isinstance(value, dict) else 999
                simulation.weekly_state["closed_cohort_snapshots"][0]["counts"][field] = damaged
                with self.assertRaises(AssertionError):
                    audit_business_log(simulation)
        for field in ("as_of", "window_start"):
            with self.subTest(field=field):
                simulation.weekly_state["closed_cohort_snapshots"] = deepcopy(original)
                simulation.weekly_state["closed_cohort_snapshots"][0][field] = 169
                with self.assertRaises(AssertionError):
                    audit_business_log(simulation)

    def test_missing_duplicate_or_reordered_cohorts_and_snapshots_are_rejected(self):
        simulation = fixture(720)
        original_summary = simulation.summary()
        original_closed = deepcopy(simulation.weekly_state["closed_cohort_snapshots"])
        for mutate in (lambda rows: rows[:-1], lambda rows: rows + [rows[0]],
                       lambda rows: list(reversed(rows))):
            with self.subTest(kind="summary", mutate=mutate):
                summary = deepcopy(original_summary)
                summary["cohorts"] = mutate(summary["cohorts"])
                simulation.summary = lambda: summary
                with self.assertRaises(AssertionError):
                    audit_business_log(simulation)
            simulation.summary = lambda: original_summary
            with self.subTest(kind="closed", mutate=mutate):
                simulation.weekly_state["closed_cohort_snapshots"] = mutate(deepcopy(original_closed))
                with self.assertRaises(AssertionError):
                    audit_business_log(simulation)
            simulation.weekly_state["closed_cohort_snapshots"] = deepcopy(original_closed)

    def test_valid_as_of_views_are_rebuilt_from_start_and_terminal_logs(self):
        for until in (0, 1, 3, 167, 168, 169, 720):
            with self.subTest(until=until):
                simulation = fixture(until)
                audit = audit_business_log(simulation)
                self.assertTrue(audit["passed"])
                self.assertEqual(audit["cohorts"], simulation.summary()["cohorts"])
                self.assertEqual(audit["closed_cohort_snapshots"],
                                 simulation.weekly_state["closed_cohort_snapshots"])
                if until >= 168:
                    closed = audit["closed_cohort_snapshots"][0]["counts"]
                    self.assertEqual((closed["success"], closed["running"], closed["earned_value"]), (0, 1, 0))
                if until >= 169:
                    self.assertEqual(audit["cohorts"][0]["success"], 1)
                    self.assertEqual(audit["cohorts"][0]["earned_value"], 11)

    def test_exact_boundary_completion_counts_but_next_float_does_not(self):
        config = Config(fleet_size=2, mission_rate_per_hour=0, failure_rate_per_flight_hour=0,
                        initial_spares=0, reorder_target=0)
        simulation = WeeklySimulation(config, [MissionSpec("exact", 167, 1, 1, 3),
                                               MissionSpec("after", 167, math.nextafter(168., math.inf) - 167, 1, 5)])
        simulation.run(169)
        audit = audit_business_log(simulation)
        self.assertEqual(audit["closed_cohort_snapshots"][0]["counts"]["success"], 1)
        self.assertEqual(audit["closed_cohort_snapshots"][0]["counts"]["earned_value"], 3)
        self.assertEqual(audit["cohorts"][0]["success"], 2)

    def test_zero_denominators_and_validity_are_checked_per_cohort(self):
        simulation = WeeklySimulation(Config(mission_rate_per_hour=0, failure_rate_per_flight_hour=0),
                                      [MissionSpec("zero", 1, 1, 1, 0)])
        simulation.run(168)
        self.assertTrue(audit_business_log(simulation)["passed"])
        original = simulation.summary()
        for index, field, value in ((0, "RCR", 0), (1, "DSR", 0), (1, "ASR", 0),
                                    (1, "validity", {"DSR": True, "RCR": False,
                                                     "ASR": False, "acceptance_rate": False})):
            with self.subTest(index=index, field=field):
                summary = deepcopy(original)
                summary["cohorts"][index][field] = value
                simulation.summary = lambda: summary
                with self.assertRaises(AssertionError):
                    audit_business_log(simulation)

    def test_frozen_values_and_terminal_shortfall_are_checked(self):
        simulation = fixture(720)
        original = simulation.summary()
        damaged = deepcopy(original)
        damaged["mandatory_shortfall_terminal"] += 1
        simulation.summary = lambda: damaged
        with self.assertRaises(AssertionError):
            audit_business_log(simulation)
        simulation.summary = lambda: original
        simulation.weekly_state["cohorts"]["0"]["offered_value"] += 123
        with self.assertRaises(AssertionError):
            audit_business_log(simulation)


if __name__ == "__main__":
    unittest.main()
