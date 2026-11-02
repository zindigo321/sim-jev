"""Real weekly execution/restoration, including fresh interpreter processes."""

from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.checkpoint_verify import compare_simulations, compare_values
from sim_jev import Config, MissionSpec, Simulation
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand


ROOT = Path(__file__).resolve().parents[1]


def quiet(**changes):
    values = dict(fleet_size=2, mission_rate_per_hour=0, failure_rate_per_flight_hour=0,
                  initial_spares=1, reorder_target=1, repair_bays=1,
                  repair_duration_min_hours=4, repair_duration_max_hours=4,
                  lead_time_min_hours=8, lead_time_max_hours=8)
    values.update(changes)
    return replace(Config(), **values)


def compare_weekly(expected, actual):
    differences = compare_simulations(expected, actual)
    differences.extend(compare_values(expected.weekly_state, actual.weekly_state, "weekly_state"))
    compare_values(expected.keyed_rng.to_dict(), actual.keyed_rng.to_dict(), "keyed_rng")
    return differences


def roundtrip(sim):
    data = sim.to_checkpoint()
    restored = WeeklySimulation.from_checkpoint(json.loads(json.dumps(data, allow_nan=False)))
    if restored.to_checkpoint() != data:
        raise AssertionError("restore changed saved commands, manifests, logs or execution state")
    return restored


class WeeklyCheckpointTests(unittest.TestCase):
    def factory(self):
        return WeeklySimulation(quiet(), [MissionSpec("FAIL", 0, 2, 1, 1),
                                         MissionSpec("CROSS", 167, 2, 1, 9),
                                         MissionSpec("NEXT", 168, 3, 1, 5),
                                         MissionSpec("LATE", 672, 60, 1, 7),
                                         MissionSpec("UNOFFered", 720, 1, 1, 100)],
                                [1, math.inf], scenario_id="checkpoint-real-paths")

    def test_profiles_have_distinct_identities_and_reject_mixed_entrypoints(self):
        old = Simulation(quiet(), [])
        new = WeeklySimulation(quiet(), [])
        old.run(0)
        new.run(0)
        legacy, weekly = old.to_checkpoint(), new.to_checkpoint()
        self.assertEqual(legacy["model_version"], "synthetic-single-lru-v1")
        self.assertEqual(weekly["model_version"], "synthetic-weekly-single-lru-v1")
        self.assertEqual(weekly["rng_protocol_version"], "sha256-entity-counter-v1")
        self.assertEqual(weekly["rng_states"], {})
        self.assertIn("policy_versions", weekly["weekly_state"])
        for factory, checkpoint in ((Simulation.from_checkpoint, weekly),
                                    (WeeklySimulation.from_checkpoint, legacy)):
            with self.assertRaises(ValueError):
                factory(checkpoint)
        self.assertEqual(Simulation.from_checkpoint(legacy).to_checkpoint(), legacy)

    def test_stable_boundary_and_repeated_weekly_run_do_not_repeat_actions(self):
        sim = self.factory()
        with self.assertRaisesRegex(ValueError, "stable boundary"):
            sim.to_checkpoint()
        for point in (0, 168, 672, 720):
            sim.run(point)
            checkpoint = sim.to_checkpoint()
            restored = roundtrip(sim)
            restored.run(point)
            self.assertEqual(restored.to_checkpoint(), checkpoint)

    def test_whole_vs_sequential_json_restores_and_cross_cohort_accounting(self):
        full = self.factory()
        full.run()
        part = self.factory()
        for point in (0, .125, math.nextafter(168, -math.inf), 168,
                      math.nextafter(168, math.inf), 169, 672, 720):
            part.run(point)
            part = roundtrip(part)
            if point == 168:
                first = part.weekly_state["closed_cohort_snapshots"][0]
                self.assertEqual(first["counts"]["success"], 0)
                self.assertEqual(first["counts"]["unfinished"], 1)
            if point == 169:
                self.assertEqual(part.summary()["cohorts"][0]["success"], 1)
        compare_weekly(full, part)
        self.assertEqual(part.missions["UNOFFered"].status, "pending")
        self.assertNotIn("UNOFFered", part.weekly_state["mission_cohort"])
        self.assertFalse(any(e.kind == "mission_start" and e.entity_id == "UNOFFered" for e in part._queue))

    def test_real_fresh_process_resume_at_week_repair_and_transit(self):
        full = self.factory()
        full.run()
        code = ("from sim_jev.weekly import WeeklySimulation; import sys; "
                "s=WeeklySimulation.load_checkpoint(sys.argv[1]); s.run(); "
                "s.save_checkpoint(sys.argv[2])")
        with tempfile.TemporaryDirectory(prefix="weekly-process-") as folder:
            for point in (1, 24, 168):
                with self.subTest(point=point):
                    part = self.factory()
                    part.run(point)
                    if point == 1:
                        self.assertEqual(part.repairs["R000001"].status, "repairing")
                    if point == 24:
                        self.assertEqual(part.orders["O000001"].status, "in_transit")
                    source, target = Path(folder) / f"pause-{point}.json", Path(folder) / f"final-{point}.json"
                    part.save_checkpoint(source)
                    completed = subprocess.run([sys.executable, "-c", code, str(source), str(target)],
                                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    compare_weekly(full, WeeklySimulation.load_checkpoint(target))

    def test_clone_and_export_isolate_commands_rules_rng_and_every_ledger(self):
        sim = self.factory()
        sim.run(168)
        original = sim.to_checkpoint()
        clone = sim.clone()
        clone.weekly_state["cohorts"]["0"]["offered_ids"].append("mutation")
        clone.weekly_state["policy_spec"]["name"] = "mutation"
        clone.weekly_state["rules_input"][0]["mandatory_ids"].append("mutation")
        clone.weekly_state["daily_commands"][0]["dispatch"] = "mutation"
        clone.weekly_state["component_generations"]["A001"] += 1
        clone.weekly_state["closed_cohort_snapshots"][0]["counts"]["success"] += 1
        clone.keyed_rng.manifest[0]["key"]["purpose"] = "mutation"
        exported = sim.to_checkpoint()
        exported["weekly_state"]["cohorts"].clear()
        self.assertEqual(sim.to_checkpoint(), original)

    def test_restore_preserves_reset_inputs_and_policy_script(self):
        commands = tuple(WeeklyCommand(point, ()) for point in (0, 168, 336, 504, 672))
        policy = PolicySpec("scripted", "reject-all", commands)
        inputs = [None, [], [MissionSpec("M", 0, 1, 1, 3)]]
        for missions in inputs:
            with self.subTest(missions=missions):
                cfg = quiet(mission_rate_per_hour=.01)
                sim = WeeklySimulation(cfg, missions, [math.inf, math.inf], policy=policy,
                                       scenario_id="reset-inputs")
                sim.run(168)
                restored = roundtrip(sim)
                self.assertEqual(restored._input_missions, sim._input_missions)
                self.assertEqual(restored._initial_lifetimes, sim._initial_lifetimes)
                for item in (sim, restored):
                    item.reset(seed=79)
                    item.run()
                compare_weekly(sim, restored)

    def test_every_weekly_state_field_required_and_bool_schema_rejected(self):
        sim = self.factory()
        sim.run(168)
        original = sim.to_checkpoint()
        for key in original["weekly_state"]:
            with self.subTest(key=key):
                data = deepcopy(original)
                del data["weekly_state"][key]
                with self.assertRaisesRegex(ValueError, "missing fields"):
                    WeeklySimulation.from_checkpoint(data)
        data = deepcopy(original)
        data["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "schema_version"):
            WeeklySimulation.from_checkpoint(data)

    def test_weekly_corruption_of_points_denominators_ownership_generation_and_snapshot_rejected(self):
        sim = self.factory()
        sim.run(168)
        original = sim.to_checkpoint()
        mutations = [
            lambda s: s["processed_week_points"].append(168),
            lambda s: s["processed_day_points"].pop(),
            lambda s: s["daily_commands"][0].__setitem__("opportunity_index", True),
            lambda s: s["cohorts"]["0"].__setitem__("offered_value", 0),
            lambda s: s["cohorts"]["0"]["offered_ids"].pop(),
            lambda s: s["mission_cohort"].__setitem__("CROSS", "168"),
            lambda s: s["commitments"]["CROSS"].__setitem__("accepted", False),
            lambda s: s["component_generations"].__setitem__("A001", 99),
            lambda s: s["announced_rule_versions"].pop(),
            lambda s: s["window_metrics"]["0"].__setitem__("flight_hours", 100),
            lambda s: s["closed_cohort_snapshots"][0]["counts"].__setitem__("success", 1),
            lambda s: s["keyed_rng"].__setitem__("scenario_id", "different-world"),
            lambda s: s["policy_versions"].__setitem__("weekly", "unknown-version"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                data = deepcopy(original)
                mutate(data["weekly_state"])
                with self.assertRaises(ValueError):
                    WeeklySimulation.from_checkpoint(data)

    def test_rejected_pending_start_survives_restore_then_is_logged_once(self):
        missions = [MissionSpec("NO", 10, 1, 1, 8), MissionSpec("END", 720, 1, 1, 9)]
        policy = PolicySpec("scripted", "reject-all", tuple(
            WeeklyCommand(point, ()) for point in (0, 168, 336, 504, 672)))
        sim = WeeklySimulation(quiet(), missions, policy=policy)
        sim.run(0)
        self.assertEqual(sim.missions["NO"].status, "rejected")
        sim = roundtrip(sim)
        sim.run(10)
        sim = roundtrip(sim)
        sim.run()
        self.assertEqual(sum(row["event_type"] == "mission_rejected_at_start" for row in sim.events), 1)
        self.assertEqual(sim.summary()["offered"], 1)
        self.assertEqual(sim.summary()["accepted"], 0)

    def test_real_random_default_trajectory_at_every_required_pause(self):
        cfg = quiet(mission_rate_per_hour=.04, failure_rate_per_flight_hour=.1)
        full = WeeklySimulation(cfg, scenario_id="random-restore")
        full.run()
        part = WeeklySimulation(cfg, scenario_id="random-restore")
        for point in (0, .125, 168, 672, 720):
            part.run(point)
            part = roundtrip(part)
        compare_weekly(full, part)


if __name__ == "__main__":
    unittest.main()
