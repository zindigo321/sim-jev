"""Versioned internal checkpoints, with strict JSON and fail-closed restoration.

This module serializes execution truth. It is deliberately separate from the
observable policy interface and the legacy (non-resumable) audit export.
"""

from __future__ import annotations

from dataclasses import asdict, fields
import heapq
import json
import math
from pathlib import Path

import numpy as np

from .config import Config, require_integer, require_number
from .metrics import Metrics
from .models import Aircraft, Event, Mission, MissionSpec, Order, Repair
from .versions import CHECKPOINT_SCHEMA_VERSION, MODEL_VERSION, RNG_PROTOCOL_VERSION


INFINITY_ENCODING = "positive-infinity-tag-v1"
INFINITY_TAG = {"$float": "+inf"}
STREAMS = ("tasks", "failures", "repairs", "supply")
ROOT_FIELDS = {
    "checkpoint_type", "schema_version", "model_version", "rng_protocol_version",
    "infinity_encoding", "time", "config", "on_hand", "aircraft", "missions",
    "repairs", "orders", "event_queue", "event_sequence", "log_sequence", "metrics",
    "rng_states", "input_missions", "initial_lifetimes", "events", "history",
}
HISTORY_FIELDS = {"time", "idle", "flying", "waiting_repair", "repairing", "on_hand",
                  "in_transit", "unmet_demand"}
LOG_FIELDS = {"event_id", "time", "event_type", "entity_id", "old_state", "new_state", "details"}


def _check(condition, path, message):
    if not condition:
        raise ValueError(f"checkpoint.{path}: {message}")


def _shape(value, expected, path):
    _check(isinstance(value, dict), path, "must be an object")
    missing, extra = set(expected) - value.keys(), value.keys() - set(expected)
    _check(not missing, path, f"missing fields: {', '.join(sorted(missing))}")
    _check(not extra, path, f"unknown fields: {', '.join(sorted(extra))}")


def _list(value, path):
    _check(isinstance(value, list), path, "must be an array")
    return value


def _text(value, path):
    _check(isinstance(value, str) and bool(value.strip()), path, "must be nonempty text")


def _number(value, path, minimum=0, maximum=None):
    require_number(f"checkpoint.{path}", value, minimum)
    if maximum is not None:
        _check(value <= maximum, path, f"must be <= {maximum}")


def _integer(value, path, minimum=0, maximum=None):
    require_integer(f"checkpoint.{path}", value, minimum)
    if maximum is not None:
        _check(value <= maximum, path, f"must be <= {maximum}")


def _optional_time(value, path, maximum=None):
    if value is not None:
        _number(value, path, maximum=maximum)


def _close(actual, expected, path):
    _check(math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-10),
           path, f"conservation mismatch: {actual!r} != {expected!r}")


def _duration_support(started, due, low, high, path):
    """Check configured support using the engine's timestamp addition.

    Subtracting timestamps can round outside a valid duration interval, while
    an isclose tolerance would admit a distinct event time. In particular a
    fixed duration has exactly one representable due timestamp. The inclusive
    upper bound also admits uniform draws rounded to the configured endpoint.
    """
    lower, upper = started + low, started + high
    _check(lower <= due <= upper, path,
           f"outside configured duration support [{lower!r}, {upper!r}]")


def _json_copy(value, path="root"):
    """Reject non-JSON Python objects and nonfinite values, with field paths."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        _check(math.isfinite(value), path, "nonfinite numbers are forbidden")
        return value
    if isinstance(value, list):
        return [_json_copy(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        _check(all(isinstance(k, str) for k in value), path, "object keys must be strings")
        return {k: _json_copy(v, f"{path}.{k}") for k, v in value.items()}
    raise ValueError(f"checkpoint.{path}: unsupported JSON type {type(value).__name__}")


def _encode_life(value, path):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == math.inf:
        return dict(INFINITY_TAG)
    _number(value, path)
    return value


def _decode_life(value, path):
    if isinstance(value, dict):
        _check(value == INFINITY_TAG, path, "invalid positive-infinity tag")
        return math.inf
    _number(value, path)
    return value


def _record_list(data, name, cls, key="id"):
    result = {}
    for i, record in enumerate(_list(data[name], name)):
        path = f"{name}[{i}]"
        _shape(record, {f.name for f in fields(cls)}, path)
        _text(record[key], f"{path}.{key}")
        _check(record[key] not in result, path, f"duplicate {key}")
        result[record[key]] = record
    return result


def _spec(value, path):
    _shape(value, {f.name for f in fields(MissionSpec)}, path)
    try:
        return MissionSpec(**value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"checkpoint.{path}: {exc}") from exc


def _reject_constant(value):
    raise ValueError(f"checkpoint JSON: nonfinite token {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"checkpoint JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def _loads(text):
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _profile(name):
    """Select an explicit contract without rewriting a snapshot's identity."""
    if name == "legacy":
        return MODEL_VERSION, RNG_PROTOCOL_VERSION, CHECKPOINT_SCHEMA_VERSION, ROOT_FIELDS, STREAMS
    if name == "weekly":
        from .weekly_checkpoint import MODEL_VERSION as weekly_model, RNG_PROTOCOL_VERSION as weekly_rng
        from .weekly_checkpoint import CHECKPOINT_SCHEMA_VERSION as weekly_schema
        return weekly_model, weekly_rng, weekly_schema, ROOT_FIELDS | {"weekly_state"}, ()
    raise ValueError(f"checkpoint: unknown model profile {name!r}")


def to_checkpoint(sim, *, profile="legacy"):
    if sim._running or not sim._checkpoint_ready or any(e.time <= sim.now for e in sim._queue):
        raise ValueError("checkpoint: requires a successful run(until=t) at a stable boundary; call run(0) after reset")
    aircraft = [asdict(a) for a in sim.aircraft.values()]
    for i, record in enumerate(aircraft):
        record["remaining_life_hours"] = _encode_life(record["remaining_life_hours"], f"aircraft[{i}].remaining_life_hours")
    model_version, rng_protocol, schema_version, _, _ = _profile(profile)
    data = {
        "checkpoint_type": "InternalCheckpoint",
        "schema_version": schema_version, "model_version": model_version,
        "rng_protocol_version": rng_protocol, "infinity_encoding": INFINITY_ENCODING,
        "time": sim.now, "config": sim.config.to_dict(), "on_hand": sim.on_hand,
        "aircraft": aircraft, "missions": [asdict(m) for m in sim.missions.values()],
        "repairs": [asdict(r) for r in sim.repairs.values()],
        "orders": [asdict(o) for o in sim.orders.values()],
        "event_queue": [asdict(e) for e in sorted(sim._queue)],
        "event_sequence": sim._sequence, "log_sequence": len(sim.events),
        "metrics": asdict(sim.metrics),
        "rng_states": {name: rng.bit_generator.state for name, rng in sim._rngs.items()},
        "input_missions": None if sim._input_missions is None else [asdict(m) for m in sim._input_missions],
        "initial_lifetimes": None if sim._initial_lifetimes is None else [
            _encode_life(v, f"initial_lifetimes[{i}]") for i, v in enumerate(sim._initial_lifetimes)],
        "events": sim.events, "history": sim.history,
    }
    if profile == "weekly":
        from .weekly_checkpoint import export_weekly_state
        data["weekly_state"] = export_weekly_state(sim)
    data = _json_copy(data)
    # The same validator guards both inbound corruption and accidental mutation
    # of an instance before export. No random variates are drawn in restoration.
    from_checkpoint(data, type(sim), profile=profile)
    return data


def from_checkpoint(data, cls, *, profile="legacy"):
    from .engine import EVENT_PRIORITY

    model_version, rng_protocol, schema_version, root_fields, streams = _profile(profile)
    weekly = profile == "weekly"
    data = _json_copy(data)
    _shape(data, root_fields, "root")
    _integer(data["schema_version"], "schema_version", 1)
    for key, expected in (("checkpoint_type", "InternalCheckpoint"),
                          ("schema_version", schema_version),
                          ("model_version", model_version),
                          ("rng_protocol_version", rng_protocol),
                          ("infinity_encoding", INFINITY_ENCODING)):
        _check(data[key] == expected, key, f"unsupported value {data[key]!r}; expected {expected!r}")
    _shape(data["config"], {f.name for f in fields(Config)}, "config")
    try:
        config = Config.from_dict(data["config"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"checkpoint.config: {exc}") from exc
    now = data["time"]
    _number(now, "time", maximum=config.horizon_hours)
    _integer(data["on_hand"], "on_hand")
    _integer(data["event_sequence"], "event_sequence")
    _integer(data["log_sequence"], "log_sequence")

    aircraft_data = _record_list(data, "aircraft", Aircraft)
    _check(len(aircraft_data) == config.fleet_size
           and all(aid == f"A{i + 1:03d}" for i, aid in enumerate(aircraft_data)),
           "aircraft", "IDs/order must match configured fleet")
    aircraft = {}
    for aid, record in aircraft_data.items():
        path = f"aircraft[{aid}]"
        _text(record["state"], path + ".state")
        _check(record["state"] in {"idle", "flying", "waiting_repair", "repairing"}, path + ".state", "invalid enum")
        record["remaining_life_hours"] = _decode_life(record["remaining_life_hours"], path + ".remaining_life_hours")
        _number(record["flight_hours"], path + ".flight_hours")
        _integer(record["version"], path + ".version")
        _optional_time(record["failure_due_at"], path + ".failure_due_at")
        if record["mission_id"] is not None:
            _text(record["mission_id"], path + ".mission_id")
        aircraft[aid] = Aircraft(**record)

    missions = {}
    for i, record in enumerate(_list(data["missions"], "missions")):
        path = f"missions[{i}]"
        _shape(record, {f.name for f in fields(Mission)}, path)
        spec = _spec(record["spec"], path + ".spec")
        _check(spec.id not in missions, path, "duplicate mission ID")
        status = record["status"]
        _text(status, path + ".status")
        statuses = {"pending", "running", "success", "failed", "not_executed"}
        if weekly:
            statuses.add("rejected")
        _check(status in statuses, path + ".status", "invalid enum")
        assigned = _list(record["assigned_aircraft"], path + ".assigned_aircraft")
        failed = _list(record["failed_aircraft"], path + ".failed_aircraft")
        for field, values in (("assigned_aircraft", assigned), ("failed_aircraft", failed)):
            _check(all(isinstance(v, str) and v in aircraft for v in values), path + "." + field, "unknown aircraft reference")
            _check(len(set(values)) == len(values), path + "." + field, "duplicate aircraft reference")
        _check(set(failed) <= set(assigned), path + ".failed_aircraft", "not a subset of assigned aircraft")
        _number(record["flight_hours"], path + ".flight_hours")
        start, end = record["actual_start"], record["actual_end"]
        _optional_time(start, path + ".actual_start", now)
        _optional_time(end, path + ".actual_end", now)
        if status == "pending":
            future = spec.start_time > now or (weekly and spec.start_time >= config.horizon_hours)
            _check(future and start is None and end is None and not assigned and not failed
                   and record["flight_hours"] == 0, path, "invalid pending mission")
        elif status == "rejected":
            _check(weekly and spec.start_time < config.horizon_hours and start is None and end is None
                   and not assigned and not failed and record["flight_hours"] == 0,
                   path, "invalid rejected mission")
        elif status == "not_executed":
            _check(start is None and end == spec.start_time and not assigned and not failed
                   and record["flight_hours"] == 0, path, "invalid unexecuted mission")
        else:
            _check(start == spec.start_time and len(assigned) == spec.aircraft_required, path, "invalid dispatch")
            if status == "running":
                _check(end is None and not failed and start + spec.duration_hours > now, path, "invalid running mission")
                elapsed = now - start
            else:
                _check(end is not None and start <= end <= start + spec.duration_hours, path, "invalid terminal mission time")
                _check(bool(failed) == (status == "failed"), path, "failure list disagrees with status")
                if status == "success":
                    _check(end == start + spec.duration_hours, path, "success must be at scheduled end")
                elapsed = end - start
            _close(record["flight_hours"], elapsed * len(assigned), path + ".flight_hours")
        missions[spec.id] = Mission(**{**record, "spec": spec})
    _check(list(missions) == sorted(missions, key=lambda mid: (missions[mid].spec.start_time, mid)),
           "missions", "records must preserve canonical start/ID order")

    repairs = {}
    for i, (rid, record) in enumerate(_record_list(data, "repairs", Repair).items(), 1):
        path = f"repairs[{rid}]"
        _check(rid == f"R{i:06d}", path + ".id", "repair IDs must be sequential")
        _check(isinstance(record["aircraft_id"], str) and record["aircraft_id"] in aircraft, path + ".aircraft_id", "unknown aircraft reference")
        _text(record["status"], path + ".status")
        _check(record["status"] in {"waiting", "repairing", "completed"}, path + ".status", "invalid enum")
        _number(record["requested_at"], path + ".requested_at", maximum=now)
        _optional_time(record["started_at"], path + ".started_at", now)
        _optional_time(record["completed_at"], path + ".completed_at", now)
        _optional_time(record["due_at"], path + ".due_at")
        _number(record["spare_wait_hours"], path + ".spare_wait_hours")
        _number(record["bay_wait_hours"], path + ".bay_wait_hours")
        requested, started, due, completed = (record[k] for k in ("requested_at", "started_at", "due_at", "completed_at"))
        if record["status"] == "waiting":
            _check(started is None and due is None and completed is None, path, "waiting repair has service timestamps")
            wait = now - requested
        else:
            _check(started is not None and due is not None and requested <= started <= due, path, "invalid service times")
            _duration_support(started, due, config.repair_duration_min_hours,
                              config.repair_duration_max_hours, path + ".due_at")
            if record["status"] == "repairing":
                _check(completed is None and due > now, path, "repair due event already elapsed")
            else:
                _check(completed == due, path, "completed repair must end at due time")
            wait = started - requested
        _close(record["spare_wait_hours"] + record["bay_wait_hours"], wait, path + ".wait_hours")
        repairs[rid] = Repair(**record)

    orders = {}
    for i, (oid, record) in enumerate(_record_list(data, "orders", Order).items(), 1):
        path = f"orders[{oid}]"
        _check(oid == f"O{i:06d}", path + ".id", "order IDs must be sequential")
        _integer(record["quantity"], path + ".quantity", 1)
        _number(record["ordered_at"], path + ".ordered_at", maximum=now)
        _number(record["expected_arrival"], path + ".expected_arrival", minimum=record["ordered_at"])
        _duration_support(record["ordered_at"], record["expected_arrival"],
                          config.lead_time_min_hours, config.lead_time_max_hours,
                          path + ".expected_arrival")
        _optional_time(record["arrived_at"], path + ".arrived_at", now)
        _text(record["status"], path + ".status")
        _check(record["status"] in {"in_transit", "arrived"}, path + ".status", "invalid enum")
        if record["status"] == "in_transit":
            _check(record["arrived_at"] is None and record["expected_arrival"] > now, path, "invalid in-transit order")
        else:
            _check(record["arrived_at"] == record["expected_arrival"], path, "arrival must match public commitment")
        orders[oid] = Order(**record)

    # Each profile's reset contract is explicit. Legacy queues every start and
    # an initial replenishment; weekly queues only starts before the horizon.
    initial_starts = [mid for mid, m in missions.items()
                      if not weekly or m.spec.start_time < config.horizon_hours]
    initial_start_sequences = {mid: i for i, mid in enumerate(initial_starts, 1)}
    minimum_sequence = len(initial_starts) + (0 if weekly else 1)
    _check(data["event_sequence"] >= minimum_sequence, "event_sequence",
           "precedes model initialization sequence")
    queue, sequence_ids, logical_events = [], set(), set()
    for i, record in enumerate(_list(data["event_queue"], "event_queue")):
        path = f"event_queue[{i}]"
        _shape(record, {f.name for f in fields(Event)}, path)
        _number(record["time"], path + ".time")
        _check(record["time"] > now, path + ".time", "event must be after the stable boundary")
        _integer(record["sequence"], path + ".sequence", 1, data["event_sequence"])
        _check(record["sequence"] not in sequence_ids, path + ".sequence", "duplicate sequence")
        sequence_ids.add(record["sequence"])
        _integer(record["version"], path + ".version")
        _integer(record["priority"], path + ".priority")
        kind, entity = record["kind"], record["entity_id"]
        _text(kind, path + ".kind")
        _text(entity, path + ".entity_id")
        _check(kind in EVENT_PRIORITY, path + ".kind", "unknown event kind")
        _check(not weekly or kind != "reorder_check", path + ".kind",
               "weekly replenishment belongs exclusively to daily commands")
        _check(record["priority"] == EVENT_PRIORITY[kind], path + ".priority", "wrong event priority")
        refs = {"failure": aircraft, "mission_start": missions, "mission_end": missions,
                "repair_complete": repairs, "order_arrival": orders,
                "reorder_check": {"inventory": None}, "try_repairs": {"maintenance": None}}
        _check(entity in refs[kind], path + ".entity_id", "unknown entity reference")
        identity = (kind, entity, record["version"])
        _check(identity not in logical_events, path, "duplicate logical event")
        logical_events.add(identity)
        if kind == "failure":
            _check(record["version"] <= aircraft[entity].version, path + ".version", "future aircraft generation")
            if record["version"] == aircraft[entity].version:
                _check(aircraft[entity].state == "flying" and record["time"] == aircraft[entity].failure_due_at,
                       path, "current failure event disagrees with flight anchor")
        else:
            _check(record["version"] == 0, path + ".version", "only failures have generation versions")
        if kind == "mission_start":
            _check(missions[entity].status in ({"pending", "rejected"} if weekly else {"pending"})
                   and record["time"] == missions[entity].spec.start_time,
                   path, "start event disagrees with pending mission")
            _check(entity in initial_start_sequences
                   and record["sequence"] == initial_start_sequences[entity], path + ".sequence",
                   "start sequence disagrees with model initialization identity")
        elif kind == "mission_end":
            mission = missions[entity]
            _check(mission.status in {"running", "failed"} and
                   record["time"] == mission.actual_start + mission.spec.duration_hours,
                   path, "end event disagrees with dispatched mission")
        elif kind == "repair_complete":
            _check(repairs[entity].status == "repairing" and record["time"] == repairs[entity].due_at,
                   path, "completion event disagrees with repair")
        elif kind == "order_arrival":
            _check(orders[entity].status == "in_transit" and record["time"] == orders[entity].expected_arrival,
                   path, "arrival event disagrees with order")
        _check(kind != "try_repairs", path, "repair attempts must be drained at their causal time")
        if kind != "mission_start":
            _check(record["sequence"] > len(initial_starts), path + ".sequence",
                   "physical/reorder event sequence overlaps model initialization starts")
        queue.append(Event(**record))
    _check(queue == sorted(queue), "event_queue", "must be in canonical event order")

    def event_count(kind, entity, time, version=0):
        return sum(e.kind == kind and e.entity_id == entity and e.time == time
                   and e.version == version for e in queue)

    for m in missions.values():
        if m.status in ({"pending", "rejected"} if weekly else {"pending"}):
            expected_start = 1
            if weekly and (m.spec.start_time >= config.horizon_hours or m.spec.start_time <= now):
                expected_start = 0
            _check(event_count("mission_start", m.spec.id, m.spec.start_time) == expected_start,
                   f"missions[{m.spec.id}]", "missing/duplicate pending start event")
        elif m.status == "running" or (m.status == "failed" and m.actual_start + m.spec.duration_hours > now):
            _check(event_count("mission_end", m.spec.id, m.actual_start + m.spec.duration_hours) == 1,
                   f"missions[{m.spec.id}]", "missing/duplicate dispatched completion event (including stale failed-task ends)")
    for a in aircraft.values():
        if a.state == "flying":
            _check(a.mission_id in missions, f"aircraft[{a.id}].mission_id", "unknown mission reference")
            if math.isfinite(a.remaining_life_hours):
                _check(a.failure_due_at is not None and a.failure_due_at > now,
                       f"aircraft[{a.id}].failure_due_at", "missing future failure anchor")
                _close(a.remaining_life_hours, a.failure_due_at - now, f"aircraft[{a.id}].remaining_life_hours")
                _check(event_count("failure", a.id, a.failure_due_at, a.version) == 1,
                       f"aircraft[{a.id}]", "missing/duplicate current failure event")
            else:
                _check(a.failure_due_at is None, f"aircraft[{a.id}].failure_due_at", "infinite life cannot have a failure event")
    for r in repairs.values():
        if r.status == "repairing":
            _check(event_count("repair_complete", r.id, r.due_at) == 1,
                   f"repairs[{r.id}]", "missing/duplicate completion event")
    for o in orders.values():
        if o.status == "in_transit":
            _check(event_count("order_arrival", o.id, o.expected_arrival) == 1,
                   f"orders[{o.id}]", "missing/duplicate arrival event")

    _shape(data["metrics"], {f.name for f in fields(Metrics)}, "metrics")
    for key, value in data["metrics"].items():
        (_number if key.endswith("hours") else _integer)(value, "metrics." + key)
    metrics = Metrics(**data["metrics"])
    expected_counts = {"failures": len(repairs),
                       "repairs_started": sum(r.status != "waiting" for r in repairs.values()),
                       "repairs_completed": sum(r.status == "completed" for r in repairs.values()),
                       "ordered_quantity": sum(o.quantity for o in orders.values()),
                       "arrived_quantity": sum(o.quantity for o in orders.values() if o.status == "arrived")}
    for key, expected in expected_counts.items():
        _check(getattr(metrics, key) == expected, "metrics." + key, "ledger count mismatch")
    for name in ("spare_wait_hours", "bay_wait_hours"):
        _close(getattr(metrics, name), sum(getattr(r, name) for r in repairs.values()), "metrics." + name)
    _close(metrics.flight_hours, sum(a.flight_hours for a in aircraft.values()), "metrics.flight_hours/aircraft")
    _close(metrics.flight_hours, sum(m.flight_hours for m in missions.values()), "metrics.flight_hours/missions")
    _close(metrics.available_aircraft_hours, metrics.idle_aircraft_hours + metrics.flight_hours,
           "metrics.available_aircraft_hours")

    inputs = data["input_missions"]
    if inputs is not None:
        inputs = tuple(_spec(v, f"input_missions[{i}]") for i, v in enumerate(_list(inputs, "input_missions")))
        _check(len({m.id for m in inputs}) == len(inputs), "input_missions", "duplicate IDs")
        _check({m.id: m for m in inputs} == {mid: m.spec for mid, m in missions.items()},
               "input_missions", "reset task specs disagree with simulation tasks")
    lifetimes = data["initial_lifetimes"]
    if lifetimes is not None:
        lifetimes = tuple(_decode_life(v, f"initial_lifetimes[{i}]") for i, v in enumerate(_list(lifetimes, "initial_lifetimes")))
        _check(len(lifetimes) == config.fleet_size, "initial_lifetimes", "must contain one value per aircraft")

    _shape(data["rng_states"], streams, "rng_states")
    rngs = {}
    for name in streams:
        state, path = data["rng_states"][name], f"rng_states.{name}"
        _shape(state, {"bit_generator", "state", "has_uint32", "uinteger"}, path)
        _check(state["bit_generator"] == "PCG64", path + ".bit_generator", "only PCG64 is supported by this protocol")
        _shape(state["state"], {"state", "inc"}, path + ".state")
        _integer(state["state"]["state"], path + ".state.state", maximum=2**128 - 1)
        _integer(state["state"]["inc"], path + ".state.inc", 1, 2**128 - 1)
        _check(state["state"]["inc"] % 2 == 1, path + ".state.inc", "PCG increment must be odd")
        _integer(state["has_uint32"], path + ".has_uint32", maximum=1)
        _integer(state["uinteger"], path + ".uinteger", maximum=2**32 - 1)
        bit_generator = np.random.PCG64(0)
        bit_generator.state = state
        _check(bit_generator.state == state, path, "RNG state cannot round trip exactly")
        rngs[name] = np.random.Generator(bit_generator)

    sim = cls.__new__(cls)
    sim.config, sim.now, sim.on_hand = config, now, data["on_hand"]
    sim.aircraft, sim.missions, sim.repairs, sim.orders = aircraft, missions, repairs, orders
    sim._queue, sim._sequence, sim.metrics = queue, data["event_sequence"], metrics
    heapq.heapify(sim._queue)
    sim._rngs, sim._input_missions, sim._initial_lifetimes = rngs, inputs, lifetimes
    sim.events, sim.history = data["events"], data["history"]
    sim._running, sim._checkpoint_ready = False, True
    sim._run_failed = False
    if weekly:
        from .weekly_checkpoint import restore_weekly_state
        restore_weekly_state(sim, data["weekly_state"])
    _validate_logs(sim, data["log_sequence"], profile=profile)
    _validate_history(sim)
    # Existing physical invariants remain unchanged; wrap their errors with the
    # checkpoint boundary to make malformed input distinguishable from reset.
    try:
        sim.assert_invariants()
    except (KeyError, RuntimeError, TypeError) as exc:
        raise ValueError(f"checkpoint.entities: {exc}") from exc
    return sim


def _validate_logs(sim, log_sequence, *, profile="legacy"):
    logs = _list(sim.events, "events")
    _check(log_sequence == len(logs), "log_sequence", "does not match event ledger length")
    refs = {"mission_start": sim.missions, "mission_failed": sim.missions,
            "mission_not_executed": sim.missions, "mission_success": sim.missions,
            "aircraft_dispatch": sim.aircraft, "aircraft_release": sim.aircraft,
            "failure": sim.aircraft, "repair_start": sim.repairs, "repair_complete": sim.repairs,
            "order_placed": sim.orders, "order_arrival": sim.orders, "reorder_check": {"inventory": None}}
    detail_fields = {
        "mission_start": {"aircraft"}, "mission_failed": {"aircraft"},
        "mission_not_executed": {"reason"}, "mission_success": set(),
        "aircraft_dispatch": {"mission"}, "aircraft_release": set(),
        "failure": {"repair_id"}, "repair_start": {"aircraft", "on_hand"},
        "repair_complete": {"aircraft"}, "order_placed": {"quantity", "expected_arrival"},
        "order_arrival": {"quantity", "on_hand"}, "reorder_check": {"quantity", "inventory_position"},
        "stale_event": {"ignored_kind", "sequence"},
    }
    transitions = {
        "mission_start": {("pending", "running")}, "mission_failed": {("running", "failed")},
        "mission_not_executed": {("pending", "not_executed")}, "mission_success": {("running", "success")},
        "aircraft_dispatch": {("idle", "flying")}, "aircraft_release": {("flying", "idle"), ("flying", "waiting_repair")},
        "failure": {("flying", "waiting_repair")}, "repair_start": {("waiting", "repairing")},
        "repair_complete": {("repairing", "completed")}, "order_placed": {("", "in_transit")},
        "order_arrival": {("in_transit", "arrived")}, "reorder_check": {("", "")}, "stale_event": {("", "")},
    }
    weekly_log_kinds = set()
    if profile == "weekly":
        from .weekly_checkpoint import log_contract, validate_weekly_log, validate_weekly_log_totals
        extra_refs, extra_fields, extra_transitions = log_contract(sim)
        weekly_log_kinds = set(extra_refs)
        refs.update(extra_refs)
        detail_fields.update(extra_fields)
        transitions.update(extra_transitions)
    previous, counts, last_check = 0.0, {}, None
    for i, row in enumerate(logs):
        path = f"events[{i}]"
        _shape(row, LOG_FIELDS, path)
        _check(row["event_id"] == f"E{i + 1:07d}", path + ".event_id", "log IDs must be continuous")
        _number(row["time"], path + ".time", previous, sim.now)
        previous = row["time"]
        kind = row["event_type"]
        _text(kind, path + ".event_type")
        _text(row["entity_id"], path + ".entity_id")
        _check(kind in refs or kind == "stale_event", path + ".event_type", "invalid event enum")
        if kind != "stale_event":
            _check(row["entity_id"] in refs[kind], path + ".entity_id", "unknown entity reference")
        for key in ("old_state", "new_state", "details"):
            _check(isinstance(row[key], str), path + "." + key, "must be text")
        try:
            detail = _loads(row["details"])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"checkpoint.{path}.details: {exc}") from exc
        _check(isinstance(detail, dict), path + ".details", "must encode an object")
        _json_copy(detail, path + ".details")
        _shape(detail, detail_fields[kind], path + ".details")
        _check((row["old_state"], row["new_state"]) in transitions[kind], path, "invalid logged state transition")
        entity, time = row["entity_id"], row["time"]
        if kind in weekly_log_kinds:
            validate_weekly_log(sim, row, detail, path)
        elif kind.startswith("mission_"):
            mission = sim.missions[entity]
            expected_time = mission.actual_start if kind == "mission_start" else mission.actual_end
            _check(time == expected_time, path + ".time", "mission timestamp disagrees with ledger")
            if kind in {"mission_start", "mission_failed"}:
                expected = mission.assigned_aircraft if kind == "mission_start" else mission.failed_aircraft
                _check(detail["aircraft"] == expected, path + ".details.aircraft", "mission aircraft list disagreement")
            if kind == "mission_not_executed":
                _check(detail["reason"] == "insufficient_idle_aircraft", path + ".details.reason", "unknown nonexecution reason")
        elif kind == "aircraft_dispatch":
            _text(detail["mission"], path + ".details.mission")
            _check(detail["mission"] in sim.missions, path + ".details.mission", "unknown mission reference")
            mission = sim.missions[detail["mission"]]
            _check(entity in mission.assigned_aircraft and time == mission.actual_start,
                   path + ".details.mission", "dispatch ownership/time disagreement")
        elif kind == "failure":
            _text(detail["repair_id"], path + ".details.repair_id")
            _check(detail["repair_id"] in sim.repairs, path + ".details.repair_id", "unknown repair reference")
            repair = sim.repairs[detail["repair_id"]]
            _check(repair.aircraft_id == entity and repair.requested_at == time,
                   path + ".details.repair_id", "failure repair ownership/time disagreement")
        elif kind in {"repair_start", "repair_complete"}:
            repair = sim.repairs[entity]
            _check(detail["aircraft"] == repair.aircraft_id, path + ".details.aircraft", "repair aircraft reference disagreement")
            expected_time = repair.started_at if kind == "repair_start" else repair.completed_at
            _check(time == expected_time, path + ".time", "repair timestamp disagrees with ledger")
        elif kind in {"order_placed", "order_arrival"}:
            order = sim.orders[entity]
            _integer(detail["quantity"], path + ".details.quantity", 1)
            _check(detail["quantity"] == order.quantity, path + ".details.quantity", "order quantity disagreement")
            expected_time = order.ordered_at if kind == "order_placed" else order.arrived_at
            _check(time == expected_time, path + ".time", "order timestamp disagrees with ledger")
            if kind == "order_placed":
                _number(detail["expected_arrival"], path + ".details.expected_arrival")
                _check(detail["expected_arrival"] == order.expected_arrival,
                       path + ".details.expected_arrival", "order commitment disagreement")
        elif kind == "stale_event":
            ignored = detail["ignored_kind"]
            _text(ignored, path + ".details.ignored_kind")
            stale_refs = {"failure": sim.aircraft, "mission_start": sim.missions, "mission_end": sim.missions,
                          "repair_complete": sim.repairs, "order_arrival": sim.orders}
            _check(ignored in stale_refs, path + ".details.ignored_kind", "unknown stale event kind")
            _check(entity in stale_refs[ignored], path + ".entity_id", "unknown stale entity reference")
            _integer(detail["sequence"], path + ".details.sequence", 1, sim._sequence)
        if "on_hand" in detail:
            _integer(detail["on_hand"], path + ".details.on_hand")
        if kind == "reorder_check":
            _integer(detail["quantity"], path + ".details.quantity")
            _integer(detail["inventory_position"], path + ".details.inventory_position", -sim.config.fleet_size)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "reorder_check":
            last_check = row["time"]
    for kind, expected in (("failure", sim.metrics.failures), ("repair_start", sim.metrics.repairs_started),
                           ("repair_complete", sim.metrics.repairs_completed), ("order_placed", len(sim.orders)),
                           ("order_arrival", sum(o.status == "arrived" for o in sim.orders.values())),
                           ("mission_start", sum(m.status in {"running", "failed", "success"} for m in sim.missions.values())),
                           ("mission_failed", sum(m.status == "failed" for m in sim.missions.values())),
                           ("mission_success", sum(m.status == "success" for m in sim.missions.values())),
                           ("mission_not_executed", sum(m.status == "not_executed" for m in sim.missions.values()))):
        _check(counts.get(kind, 0) == expected, "events." + kind, "ledger count mismatch")
    _check(last_check is not None, "events.reorder_check", "missing initial inventory review; call run(0)")
    if profile == "weekly":
        validate_weekly_log_totals(sim, counts)
        _check(not any(e.kind == "reorder_check" for e in sim._queue), "event_queue.reorder_check",
               "weekly replenishment must have no independent queued review")
        return
    next_check = last_check + sim.config.reorder_interval_hours
    pending = [e for e in sim._queue if e.kind == "reorder_check"]
    if next_check <= sim.config.horizon_hours:
        _check(len(pending) == 1 and pending[0].time == next_check,
               "event_queue.reorder_check", "missing/incorrect next inventory review")
    else:
        _check(not pending, "event_queue.reorder_check", "inventory review beyond configured horizon")


def _validate_history(sim):
    history = _list(sim.history, "history")
    _check(bool(history), "history", "must retain initial snapshot")
    previous = 0.0
    integrals = {key: 0.0 for key in ("available_aircraft_hours", "idle_aircraft_hours", "flight_hours",
                                     "spare_wait_hours", "bay_wait_hours")}
    for i, row in enumerate(history):
        path = f"history[{i}]"
        _shape(row, HISTORY_FIELDS, path)
        _number(row["time"], path + ".time", previous, sim.now)
        previous = row["time"]
        for key in HISTORY_FIELDS - {"time"}:
            _integer(row[key], path + "." + key)
        _check(sum(row[k] for k in ("idle", "flying", "waiting_repair", "repairing")) == sim.config.fleet_size,
               path, "aircraft count mismatch")
        _check(row["repairing"] <= sim.config.repair_bays and row["unmet_demand"] == row["waiting_repair"],
               path, "repair capacity/demand mismatch")
        if i + 1 < len(history):
            next_row = history[i + 1]
            _shape(next_row, HISTORY_FIELDS, f"history[{i + 1}]")
            _number(next_row["time"], f"history[{i + 1}].time", row["time"], sim.now)
            dt = next_row["time"] - row["time"]
            covered = min(row["on_hand"], row["unmet_demand"])
            integrals["available_aircraft_hours"] += dt * (row["idle"] + row["flying"])
            integrals["idle_aircraft_hours"] += dt * row["idle"]
            integrals["flight_hours"] += dt * row["flying"]
            integrals["spare_wait_hours"] += dt * (row["unmet_demand"] - covered)
            integrals["bay_wait_hours"] += dt * covered
    initial = {"time": 0.0, "idle": sim.config.fleet_size, "flying": 0, "waiting_repair": 0,
               "repairing": 0, "on_hand": sim.config.initial_spares, "in_transit": 0, "unmet_demand": 0}
    _check(history[0] == initial, "history[0]", "initial snapshot disagreement")
    expected = {"time": sim.now, **{state: sum(a.state == state for a in sim.aircraft.values())
                                   for state in ("idle", "flying", "waiting_repair", "repairing")},
                "on_hand": sim.on_hand, "in_transit": sim.in_transit, "unmet_demand": sim.unmet_demand}
    _check(history[-1] == expected, "history[-1]", "current snapshot disagreement")
    for key, value in integrals.items():
        _close(getattr(sim.metrics, key), value, "history.integral." + key)


def save_checkpoint(sim, path):
    text = json.dumps(to_checkpoint(sim), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def load_checkpoint(path, cls):
    try:
        data = _loads(Path(path).read_text(encoding="utf-8-sig"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"checkpoint file {path}: {exc}") from exc
    return from_checkpoint(data, cls)
