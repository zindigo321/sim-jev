"""Fail-closed checkpoint input and deep branch isolation through real runs."""

from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import unittest

from sim_jev import Config, MissionSpec, Simulation


class CheckpointValidationTests(unittest.TestCase):
    def make_sim(self):
        config = replace(Config(), horizon_hours=30, fleet_size=3,
                         mission_rate_per_hour=0, initial_spares=1, reorder_target=2,
                         repair_bays=1, repair_duration_min_hours=4,
                         repair_duration_max_hours=4, lead_time_min_hours=8,
                         lead_time_max_hours=8)
        sim = Simulation(config, [MissionSpec("failing", 1, 5, 2, 10),
                                  MissionSpec("later", 12, 2, 1, 20)],
                         initial_lifetimes=[1, 1, math.inf])
        sim.run(3)
        self.assertEqual({r.status for r in sim.repairs.values()}, {"waiting", "repairing"})
        self.assertTrue(sim.in_transit)
        return sim

    def reject(self, mutation, fragment=None):
        data = self.make_sim().to_checkpoint()
        mutation(data)
        with self.assertRaisesRegex(ValueError, fragment or "checkpoint"):
            Simulation.from_checkpoint(data)

    def test_complete_roundtrip_and_strict_json(self):
        sim = self.make_sim()
        data = sim.to_checkpoint()
        restored = Simulation.from_checkpoint(json.loads(json.dumps(data, allow_nan=False)))
        self.assertEqual(data, restored.to_checkpoint())
        restored.run()
        sim.run()
        self.assertEqual(restored.events, sim.events)
        self.assertEqual(restored.summary(), sim.summary())
        self.assertEqual(restored.to_checkpoint(), sim.to_checkpoint())

    def test_requires_stable_run_boundary_after_constructor_and_reset(self):
        sim = Simulation(Config())
        with self.assertRaisesRegex(ValueError, "stable boundary"):
            sim.to_checkpoint()
        sim.run(0)
        sim.to_checkpoint()
        sim.reset()
        with self.assertRaisesRegex(ValueError, "stable boundary"):
            sim.clone()

    def test_handler_cannot_checkpoint_and_failed_handler_cannot_resume(self):
        class InspectingSimulation(Simulation):
            def _reorder(self, event):
                with self.test.assertRaisesRegex(ValueError, "stable boundary"):
                    self.to_checkpoint()
                super()._reorder(event)

        sim = InspectingSimulation(replace(Config(), mission_rate_per_hour=0))
        sim.test = self
        sim.run(0)
        sim.to_checkpoint()

        class FailingSimulation(Simulation):
            def _reorder(self, event):
                super()._reorder(event)
                raise RuntimeError("handler interrupted after mutation")

        broken = FailingSimulation(Config())
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            broken.run(0)
        with self.assertRaisesRegex(ValueError, "stable boundary"):
            broken.to_checkpoint()
        with self.assertRaisesRegex(RuntimeError, "failed event run"):
            broken.run(0)

    def test_schema_and_protocol_versions_are_explicit(self):
        for field in ("schema_version", "model_version", "rng_protocol_version",
                      "infinity_encoding", "checkpoint_type"):
            with self.subTest(field=field):
                self.reject(lambda d, f=field: d.__setitem__(f, 999), field)
        self.reject(lambda d: d.__setitem__("schema_version", True), "schema_version")

    def test_every_root_field_is_required(self):
        data = self.make_sim().to_checkpoint()
        for field in data:
            with self.subTest(field=field):
                value = deepcopy(data)
                del value[field]
                with self.assertRaisesRegex(ValueError, "missing fields.*" + field):
                    Simulation.from_checkpoint(value)

    def test_nested_required_fields_and_unknown_fields(self):
        for table, field in (("aircraft", "version"), ("missions", "actual_start"),
                             ("repairs", "bay_wait_hours"), ("orders", "arrived_at"),
                             ("event_queue", "sequence"), ("events", "details"),
                             ("history", "idle")):
            with self.subTest(table=table):
                self.reject(lambda d, t=table, f=field: d[t][0].pop(f), table)
        self.reject(lambda d: d["config"].pop("seed"), "config")
        self.reject(lambda d: d["metrics"].pop("failures"), "metrics")
        self.reject(lambda d: d.__setitem__("unknown", 1), "unknown fields")

    def test_nonfinite_values_rejected_at_every_non_lifetime_boundary(self):
        for value in (math.nan, math.inf, -math.inf):
            for table, field in (("aircraft", "flight_hours"), ("missions", "flight_hours"),
                                 ("repairs", "spare_wait_hours"), ("orders", "expected_arrival"),
                                 ("event_queue", "time"), ("events", "time"), ("history", "time")):
                with self.subTest(value=value, table=table):
                    self.reject(lambda d, t=table, f=field, v=value: d[t][0].__setitem__(f, v), "nonfinite")
        self.reject(lambda d: d["aircraft"][0].__setitem__("remaining_life_hours", math.nan), "nonfinite")
        self.reject(lambda d: d["initial_lifetimes"].__setitem__(0, math.inf), "nonfinite")

    def test_infinity_is_explicit_reversible_and_field_limited(self):
        sim = self.make_sim()
        data = sim.to_checkpoint()
        self.assertEqual(data["aircraft"][2]["remaining_life_hours"], {"$float": "+inf"})
        self.assertEqual(data["initial_lifetimes"][2], {"$float": "+inf"})
        restored = Simulation.from_checkpoint(data)
        self.assertEqual(restored.aircraft["A003"].remaining_life_hours, math.inf)
        for invalid in (None, {"$float": "NaN"}, {"$float": "-inf"}, {"$float": "+inf", "extra": 1}):
            with self.subTest(invalid=invalid):
                self.reject(lambda d, v=invalid: d["aircraft"][2].__setitem__("remaining_life_hours", v), "remaining_life_hours")
        self.reject(lambda d: d.__setitem__("time", {"$float": "+inf"}), "time")

    def test_enums_quantities_and_references_fail_with_location(self):
        for table, field in (("aircraft", "state"), ("missions", "status"),
                             ("repairs", "status"), ("orders", "status")):
            for value in ("broken", [], None):
                with self.subTest(table=table, value=value):
                    self.reject(lambda d, t=table, f=field, v=value: d[t][0].__setitem__(f, v), table)
        self.reject(lambda d: d["orders"][0].__setitem__("quantity", True), "quantity")
        self.reject(lambda d: d["orders"][0].__setitem__("quantity", -1), "quantity")
        self.reject(lambda d: d["repairs"][0].__setitem__("aircraft_id", "missing"), "aircraft_id")
        self.reject(lambda d: d["missions"][0]["assigned_aircraft"].__setitem__(0, "missing"), "assigned_aircraft")
        self.reject(lambda d: d["event_queue"][0].__setitem__("entity_id", "missing"), "entity_id")

    def test_inventory_capacity_and_ledger_conservation(self):
        self.reject(lambda d: d["config"].__setitem__("fleet_size", 10**100), "aircraft")
        self.reject(lambda d: d.__setitem__("on_hand", d["on_hand"] + 1), "checkpoint")
        self.reject(lambda d: d["config"].__setitem__("repair_bays", 0), "capacity")
        for key in ("failures", "repairs_started", "repairs_completed", "ordered_quantity", "arrived_quantity"):
            with self.subTest(key=key):
                self.reject(lambda d, k=key: d["metrics"].__setitem__(k, d["metrics"][k] + 1), key)
        self.reject(lambda d: d["metrics"].__setitem__("flight_hours", d["metrics"]["flight_hours"] + 1), "flight_hours")
        self.reject(lambda d: d["repairs"][0].__setitem__("bay_wait_hours", 1), "wait_hours")

    def test_pending_event_presence_priorities_sequences_and_stable_times(self):
        self.reject(lambda d: d["event_queue"].pop(0), "event")
        self.reject(lambda d: d["event_queue"][0].__setitem__("priority", 99), "priority")
        self.reject(lambda d: d["event_queue"][0].__setitem__("time", d["time"]), "stable boundary")
        self.reject(lambda d: d["event_queue"][0].__setitem__("sequence", d["event_sequence"] + 1), "sequence")
        self.reject(lambda d: d["event_queue"][1].__setitem__("sequence", d["event_queue"][0]["sequence"]), "sequence")
        self.reject(lambda d: d["event_queue"][0].__setitem__("time", d["event_queue"][0]["time"] + 0.1), "event")

    def test_failed_mission_retains_future_stale_completion(self):
        def remove_failed_end(data):
            data["event_queue"] = [e for e in data["event_queue"]
                                   if not (e["kind"] == "mission_end" and e["entity_id"] == "failing")]
        self.reject(remove_failed_end, "stale failed-task ends")

    def test_log_detail_references_and_stale_entities_are_validated(self):
        for kind, field in (("failure", "repair_id"), ("aircraft_dispatch", "mission"),
                            ("repair_start", "aircraft")):
            def corrupt(data, event_kind=kind, detail_field=field):
                row = next(e for e in data["events"] if e["event_type"] == event_kind)
                detail = json.loads(row["details"])
                detail[detail_field] = "DOES_NOT_EXIST"
                row["details"] = json.dumps(detail)
            with self.subTest(kind=kind):
                self.reject(corrupt, "details." + field)
        def corrupt_stale(data):
            row = next(e for e in data["events"] if e["event_type"] == "stale_event")
            row["entity_id"] = "DOES_NOT_EXIST"
        self.reject(corrupt_stale, "stale entity reference")

    def test_rng_structure_ranges_and_protocol_rejected(self):
        self.reject(lambda d: d["rng_states"].pop("supply"), "rng_states")
        self.reject(lambda d: d["rng_states"]["tasks"].__setitem__("bit_generator", "MT19937"), "bit_generator")
        self.reject(lambda d: d["rng_states"]["tasks"]["state"].__setitem__("inc", 2), "inc")
        self.reject(lambda d: d["rng_states"]["tasks"]["state"].__setitem__("state", 2**128), "state")
        self.reject(lambda d: d["rng_states"]["tasks"].__setitem__("has_uint32", 3), "has_uint32")
        self.reject(lambda d: d["rng_states"]["tasks"].__setitem__("uinteger", -1), "uinteger")

    def test_logs_history_and_reset_input_corruption_rejected(self):
        self.reject(lambda d: d["events"][0].__setitem__("event_id", "E0"), "event_id")
        self.reject(lambda d: d["events"][0].__setitem__("details", '{"x": NaN}'), "details")
        self.reject(lambda d: d["events"].pop(0), "log_sequence")
        self.reject(lambda d: d["history"][-1].__setitem__("on_hand", 100), "history")
        self.reject(lambda d: d["history"][0].__setitem__("time", 0.5), "history")
        self.reject(lambda d: d["input_missions"][0].__setitem__("value", 999), "input_missions")
        self.reject(lambda d: d["initial_lifetimes"].pop(), "initial_lifetimes")

    def test_clone_isolates_every_mutable_category_and_rng(self):
        sim = self.make_sim()
        original = sim.to_checkpoint()
        clone = sim.clone()
        self.assertIsNot(clone.config, sim.config)
        clone.aircraft["A003"].flight_hours += 1
        clone.missions["later"].assigned_aircraft.append("A003")
        clone.repairs["R000001"].spare_wait_hours += 1
        clone.orders["O000001"].quantity += 1
        clone._queue[0].time += 1
        clone.events[0]["details"] = "{}"
        clone.history[0]["idle"] = 0
        clone.metrics.failures += 1
        for name in clone._rngs:
            self.assertIsNot(clone._rngs[name], sim._rngs[name])
            clone._rngs[name].random(20)
        self.assertEqual(original, sim.to_checkpoint())

    def test_export_and_import_do_not_share_input_data(self):
        sim = self.make_sim()
        original = sim.to_checkpoint()
        exported = sim.to_checkpoint()
        restored = Simulation.from_checkpoint(exported)
        exported["aircraft"][0]["state"] = "broken"
        exported["events"][0]["details"] = "{}"
        exported["rng_states"]["supply"]["state"]["state"] = 0
        self.assertEqual(sim.to_checkpoint(), original)
        self.assertEqual(restored.to_checkpoint(), original)
        restored.run()
        self.assertEqual(sim.to_checkpoint(), original)

    def test_file_roundtrip_no_overwrite_and_invalid_json(self):
        sim = self.make_sim()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "checkpoint.json"
            sim.save_checkpoint(path)
            saved = path.read_bytes()
            loaded = Simulation.load_checkpoint(path)
            self.assertEqual(loaded.to_checkpoint(), sim.to_checkpoint())
            with self.assertRaises(FileExistsError):
                sim.save_checkpoint(path)
            self.assertEqual(path.read_bytes(), saved)
            bad = Path(folder) / "bad.json"
            for text in ('{"time": NaN}', '{"time": 1, "time": 2}', '{'):
                bad.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "checkpoint file"):
                    Simulation.load_checkpoint(bad)

    def test_export_rejects_nan_in_mutated_instance(self):
        sim = self.make_sim()
        sim.aircraft["A003"].remaining_life_hours = math.nan
        with self.assertRaisesRegex(ValueError, "remaining_life_hours"):
            sim.to_checkpoint()


if __name__ == "__main__":
    unittest.main()
