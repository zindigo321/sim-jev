"""Weekly audit artifacts; all internal truth is explicitly kept out of observation."""
from dataclasses import asdict

from .provenance import canonical_sha256, runtime_metadata
from .reporting import (check_output_directory, _write_json, _write_csv, _mission_rows,
                        EVENT_COLUMNS, MISSION_COLUMNS, STATE_COLUMNS, REPAIR_COLUMNS, ORDER_COLUMNS)
from .versions import WEEKLY_POLICY_VERSION, DAILY_POLICY_VERSION
from .decision import policy_version
from .branch_audit_store import save_checkpoint


def write_weekly_run(sim, output_dir, *, run_context=None):
    destination = check_output_directory(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    state = sim.weekly_state
    _write_json(destination / "config.json", sim.config.to_dict())
    _write_json(destination / "summary.json", sim.summary())
    _write_json(destination / "weekly-ledger.json", state)
    _write_json(destination / "policy-observation.json", sim.get_observation().to_dict())
    _write_json(destination / "random-manifest.json", sim.keyed_rng.to_dict())
    save_checkpoint(destination / "checkpoint.json", sim.to_checkpoint())
    _write_json(destination / "runtime.json", runtime_metadata(
        sim.config.to_dict(), run_context=run_context, model_version=sim.model_version,
        rng_protocol_version=sim.rng_protocol_version,
        checkpoint_schema_version=sim.checkpoint_schema_version,
        policy_identity={"weekly_version": policy_version(state["policy_spec"]), "daily_version": DAILY_POLICY_VERSION,
                         "kind": state["policy_spec"]["kind"], "name": state["policy_spec"]["name"],
                         "input_sha256": canonical_sha256(state["policy_spec"])}))
    _write_csv(destination / "events.csv", sim.events, EVENT_COLUMNS)
    _write_csv(destination / "missions.csv", _mission_rows(sim), MISSION_COLUMNS)
    _write_csv(destination / "states.csv", sim.history, STATE_COLUMNS)
    _write_csv(destination / "repairs.csv", [asdict(v) for v in sim.repairs.values()], REPAIR_COLUMNS)
    _write_csv(destination / "orders.csv", [asdict(v) for v in sim.orders.values()], ORDER_COLUMNS)
    return destination
