"""Drive real weekly decisions and repairs to test information and RNG pairing."""

import copy
from dataclasses import asdict, replace
import json
import unittest

from sim_jev.config import Config
from sim_jev.models import MissionSpec
from sim_jev.keyed_rng import KeyedRNG
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand


WEEKS = (0, 168, 336, 504, 672)


def scripted(accepted=(), name="script"):
    return PolicySpec("scripted", name,
                      tuple(WeeklyCommand(point, tuple(accepted) if point == 0 else ())
                            for point in WEEKS))


class WeeklyInformationRNGTests(unittest.TestCase):
    def _manifest_checkpoint(self):
        config = replace(Config(), seed=12, fleet_size=2, mission_rate_per_hour=0.1,
                         mission_aircraft_min=1, mission_aircraft_max=1,
                         mission_duration_min_hours=2, mission_duration_max_hours=4,
                         failure_rate_per_flight_hour=0.4, repair_bays=2,
                         repair_duration_min_hours=2, repair_duration_max_hours=3,
                         initial_spares=0, reorder_target=3,
                         lead_time_min_hours=4, lead_time_max_hours=8)
        simulation = WeeklySimulation(config, scenario_id="manifest-contract")
        simulation.run()
        self.assertTrue(simulation.repairs)
        self.assertTrue(simulation.orders)
        return simulation.to_checkpoint()

    def test_manifest_requires_every_historical_semantic_draw(self):
        state = self._manifest_checkpoint()
        manifest = state["weekly_state"]["keyed_rng"]["manifest"]
        selectors = (
            lambda e: e["key"]["purpose"] == "lifetime" and e["key"]["generation"] == 0,
            lambda e: e["key"]["purpose"] == "lifetime" and e["key"]["generation"] > 0,
            lambda e: e["key"]["purpose"] == "repair_duration",
            lambda e: e["key"]["purpose"] == "lead_time",
            lambda e: e["key"]["purpose"] == "duration",
            lambda e: e["key"]["purpose"] == "interval"
                      and e["key"]["entity_id"] == str(len(state["missions"])),
        )
        for selector in selectors:
            index = next(i for i, entry in enumerate(manifest) if selector(entry))
            corrupted = copy.deepcopy(state)
            del corrupted["weekly_state"]["keyed_rng"]["manifest"][index]
            with self.assertRaisesRegex(ValueError, "missing required entity sample"):
                WeeklySimulation.from_checkpoint(corrupted)
        restored = WeeklySimulation.from_checkpoint(json.loads(json.dumps(state, allow_nan=False)))
        self.assertEqual(restored.to_checkpoint(), state)

    def test_manifest_rejects_samples_without_matching_entity_or_opportunity(self):
        state = self._manifest_checkpoint()
        rng = KeyedRNG(state["config"]["seed"], "manifest-contract")
        rng.exponential(state["config"]["failure_rate_per_flight_hour"],
                        entity_type="component", entity_id="A999/LRU0", purpose="lifetime")
        state["weekly_state"]["keyed_rng"]["manifest"].append(rng.manifest[0])
        with self.assertRaisesRegex(ValueError, "unexpected sample"):
            WeeklySimulation.from_checkpoint(state)

    def test_manifest_values_match_actual_repairs_public_eta_and_task_attributes(self):
        state = self._manifest_checkpoint()
        for purpose in ("repair_duration", "lead_time", "duration", "value"):
            corrupted = copy.deepcopy(state)
            entry = next(item for item in corrupted["weekly_state"]["keyed_rng"]["manifest"]
                         if item["key"]["purpose"] == purpose)
            low, high = entry["distribution"]["low"], entry["distribution"]["high"]
            entry["value"] = low if entry["value"] != low else high
            with self.assertRaisesRegex(ValueError, "saved.*sample"):
                WeeklySimulation.from_checkpoint(corrupted)
        corrupted = copy.deepcopy(state)
        entry = next(item for item in corrupted["weekly_state"]["keyed_rng"]["manifest"]
                     if item["key"]["purpose"] == "repair_duration")
        entry["distribution"]["high"] += 1
        with self.assertRaisesRegex(ValueError, "configured mechanism"):
            WeeklySimulation.from_checkpoint(corrupted)
        corrupted = copy.deepcopy(state)
        terminal = next(item for item in corrupted["weekly_state"]["keyed_rng"]["manifest"]
                        if item["key"]["purpose"] == "interval"
                        and item["key"]["entity_id"] == str(len(state["missions"])))
        terminal["value"] = 0
        with self.assertRaisesRegex(ValueError, "last arrival interval"):
            WeeklySimulation.from_checkpoint(corrupted)

    def test_override_and_empty_generated_stream_have_no_invented_samples(self):
        config = replace(Config(), mission_rate_per_hour=0, reorder_target=0)
        explicit = WeeklySimulation(config, [], [100] * config.fleet_size)
        explicit.run(0)
        self.assertEqual(explicit.keyed_rng.manifest, [])
        WeeklySimulation.from_checkpoint(explicit.to_checkpoint())
        generated = WeeklySimulation(config)
        generated.run(0)
        self.assertEqual(len(generated.keyed_rng.manifest), config.fleet_size)
        WeeklySimulation.from_checkpoint(generated.to_checkpoint())

    def test_two_real_policies_receive_same_generated_exogenous_missions(self):
        config = replace(Config(), seed=52, mission_rate_per_hour=0.08)
        accept = WeeklySimulation(config, scenario_id="paired-demand")
        reject = WeeklySimulation(config, scenario_id="paired-demand", policy=scripted(name="reject-all"))
        accept.run()
        reject.run()
        self.assertEqual([asdict(m.spec) for m in accept.missions.values()],
                         [asdict(m.spec) for m in reject.missions.values()])
        first = [entry for entry in accept.keyed_rng.manifest if entry["key"]["entity_type"] == "arrival"]
        second = [entry for entry in reject.keyed_rng.manifest if entry["key"]["entity_type"] == "arrival"]
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertGreater(accept.summary()["launched"], 0)
        self.assertEqual(reject.summary()["launched"], 0)

    def test_real_extra_a001_repair_does_not_move_a002_samples(self):
        config = replace(Config(), seed=12, fleet_size=2, mission_rate_per_hour=0,
                         failure_rate_per_flight_hour=0.0001, repair_bays=2,
                         repair_duration_min_hours=2, repair_duration_max_hours=3,
                         initial_spares=10, reorder_target=0)
        tasks = [MissionSpec("extra", 0, 2, 1, 1), MissionSpec("common", 5, 2, 2, 10)]
        extra = WeeklySimulation(config, tasks, [1.5, 1], scenario_id="entity-pair",
                                 policy=scripted(("extra", "common"), "extra-a001"))
        fewer = WeeklySimulation(config, tasks, [1.5, 1], scenario_id="entity-pair",
                                 policy=scripted(("common",), "no-extra"))
        extra.run(10)
        fewer.run(10)
        self.assertEqual(extra.missions["extra"].status, "failed")
        self.assertEqual(fewer.missions["extra"].status, "rejected")
        first_repair = next(r for r in extra.repairs.values() if r.aircraft_id == "A002")
        second_repair = next(r for r in fewer.repairs.values() if r.aircraft_id == "A002")
        self.assertEqual(first_repair.id, "R000002")
        self.assertEqual(second_repair.id, "R000001")
        self.assertEqual(first_repair.requested_at, 6)
        self.assertEqual(second_repair.requested_at, 6)
        self.assertEqual(first_repair.status, "completed")
        self.assertEqual(second_repair.status, "completed")
        paired = []
        for simulation in (extra, fewer):
            samples = [entry for entry in simulation.keyed_rng.manifest
                       if entry["key"]["entity_id"] == "A002/LRU0"]
            self.assertEqual({(entry["key"]["generation"], entry["key"]["purpose"])
                              for entry in samples}, {(0, "repair_duration"), (1, "lifetime")})
            paired.append(samples)
        self.assertEqual(*paired)
        self.assertEqual(first_repair.due_at, second_repair.due_at)
        self.assertEqual(extra.aircraft["A002"].remaining_life_hours,
                         fewer.aircraft["A002"].remaining_life_hours)

    def test_policy_label_never_changes_real_samples_or_commands(self):
        config = replace(Config(), seed=7, fleet_size=1, mission_rate_per_hour=0,
                         initial_spares=2, reorder_target=0,
                         repair_duration_min_hours=2, repair_duration_max_hours=4)
        tasks = [MissionSpec("A", 0, 2, 1, 10)]
        left = WeeklySimulation(config, tasks, [1], scenario_id="label-pair",
                                policy=scripted(("A",), "policy-left"))
        right = WeeklySimulation(config, tasks, [1], scenario_id="label-pair",
                                 policy=scripted(("A",), "policy-right"))
        left.run()
        right.run()
        self.assertEqual(left.keyed_rng.manifest, right.keyed_rng.manifest)
        self.assertEqual(left.weekly_state["weekly_commands"], right.weekly_state["weekly_commands"])
        self.assertEqual(left.events, right.events)

    def test_hidden_lifetime_future_task_and_repair_realization_do_not_change_inputs(self):
        base = replace(Config(), seed=1, fleet_size=2, mission_rate_per_hour=0,
                       initial_spares=2, reorder_target=0, repair_bays=1,
                       repair_duration_min_hours=2, repair_duration_max_hours=3)
        visible = [MissionSpec("cause", 167, 2, 1, 1), MissionSpec("next", 170, 1, 1, 10)]
        # Only hidden quantities differ at the real 168 h decision: aircraft 2
        # never flew, 400 h demand is not announced, and repair completion is
        # not a public commitment. There are no orders with conflicting ETAs.
        left = WeeklySimulation(base, [*visible, MissionSpec("future", 400, 1, 1, 1)], [1, 99],
                                scenario_id="hidden-pair")
        right = WeeklySimulation(replace(base, seed=2),
                                 [*visible, MissionSpec("future", 401, 2, 1, 999)], [1, 199],
                                 scenario_id="hidden-pair")
        left.run(168)
        right.run(168)
        self.assertNotEqual(left.repairs["R000001"].due_at, right.repairs["R000001"].due_at)
        self.assertEqual(left.repairs["R000001"].status, "repairing")
        self.assertEqual(right.repairs["R000001"].status, "repairing")
        self.assertNotEqual(left.aircraft["A002"].remaining_life_hours,
                            right.aircraft["A002"].remaining_life_hours)
        self.assertNotEqual(left.missions["future"].spec, right.missions["future"].spec)
        self.assertEqual(left.get_observation().to_dict(), right.get_observation().to_dict())
        self.assertEqual(left._rule_context(168).to_dict(), right._rule_context(168).to_dict())
        self.assertEqual(left.weekly_state["weekly_commands"], right.weekly_state["weekly_commands"])
        self.assertEqual(left.weekly_state["weekly_commands"][-1]["accepted_ids"], ["next"])
        self.assertFalse(left.orders)
        self.assertFalse(right.orders)

    def test_future_announced_rule_is_visible_but_not_applied_early(self):
        config = replace(Config(), fleet_size=2, mission_rate_per_hour=0,
                         failure_rate_per_flight_hour=0, reorder_target=0)
        rules = [RuleSpec(168, 336, 0, 168, "public-future", min_reserve=2),
                 RuleSpec(336, 504, 336, 336, "hidden-future", min_reserve=2)]
        tasks = [MissionSpec("now", 1, 1, 1, 1), MissionSpec("later", 169, 1, 1, 1)]
        simulation = WeeklySimulation(config, tasks, rules=rules)
        simulation.run(0)
        public = {rule["version"] for rule in simulation.get_observation().rules}
        self.assertIn("public-future", public)
        self.assertNotIn("hidden-future", public)
        self.assertEqual(simulation.weekly_state["weekly_commands"][0]["accepted_ids"], ["now"])
        simulation.run(168)
        self.assertEqual(simulation.weekly_state["weekly_commands"][1]["accepted_ids"], [])
        self.assertNotIn("hidden-future", {r["version"] for r in simulation.get_observation().rules})
        simulation.run(336)
        self.assertIn("hidden-future", {r["version"] for r in simulation.get_observation().rules})

    def test_observation_is_independent_and_order_eta_remains_public_truth(self):
        config = replace(Config(), fleet_size=1, mission_rate_per_hour=0,
                         initial_spares=0, reorder_target=1,
                         lead_time_min_hours=8, lead_time_max_hours=8)
        simulation = WeeklySimulation(config, [MissionSpec("A", 1, 1, 1, 1)], [100])
        simulation.run(0)
        before = simulation.get_observation().to_dict()
        self.assertEqual(before["orders"][0]["expected_arrival"], simulation.orders["O000001"].expected_arrival)
        self.assertEqual(before["orders"][0]["expected_arrival"], 8)
        observation = simulation.get_observation()
        observation.aircraft[0]["state"] = "corrupted"
        observation.missions[0]["value"] = 999
        observation.inventory["on_hand"] = 999
        observation.rules[0]["mandatory_ids"].append("unknown")
        observation.commitments[0]["accepted"] = False
        observation.orders[0]["expected_arrival"] = 999
        self.assertEqual(before, simulation.get_observation().to_dict())
        simulation.run(8)
        self.assertEqual(simulation.orders["O000001"].arrived_at, 8)


if __name__ == "__main__":
    unittest.main()
