"""Conditional planning worlds built exclusively from a legal decision request.

The physical event handlers and daily executor are inherited from the execution
engine. An origin ledger records public carry-in facts; no old events/history are
invented, and these worlds never pass through execution checkpoint restoration.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math

from .config import Config, require_integer
from .decision import DecisionRequest
from .keyed_rng import KeyedRNG
from .metrics import Metrics
from .models import Aircraft, Mission, MissionSpec, Order, Repair
from .weekly import DAY_POINTS, INTEGRALS, WEEK_POINTS, WeeklySimulation
from .weekly_policy import PolicySpec, WeeklyCommand, validate_command


PLANNING_MODEL_VERSION = "conditional-weekly-single-lru-v1"
CONDITIONAL_DISTRIBUTION_VERSION = "exponential-memoryless-uniform-survival-v1"
PLANNING_INPUT_VERSION = "legal-decision-planning-input-v1"
BRANCH_SCORE_VERSION = "current-offer-realized-dsr-plus-rcr-v1"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _finite(value):
    if isinstance(value, float) and math.isinf(value) and value > 0:
        return {"$float": "+inf"}
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


@dataclass(frozen=True)
class PlanningInput:
    request: DecisionRequest
    planning_seed: int
    scenario_index: int

    def __post_init__(self):
        if not isinstance(self.request, DecisionRequest):
            raise ValueError("PlanningInput requires a legal DecisionRequest")
        require_integer("planning_seed", self.planning_seed)
        require_integer("scenario_index", self.scenario_index)
        # Freeze by value: later changes to a caller's nested public lists must
        # not mutate an already constructed scenario.
        object.__setattr__(self, "request", DecisionRequest.from_dict(self.request.to_dict()))

    def to_dict(self):
        return {"version": PLANNING_INPUT_VERSION, "request": self.request.to_dict(),
                "planning_seed": self.planning_seed, "scenario_index": self.scenario_index}


def conditional_repair_due(rng, *, aircraft_id, generation, started_at, as_of, low, high):
    """U(low,high) total service conditioned on still being active at as_of.

    Timestamp addition is checked exactly, including fixed-duration services.
    A draw which cannot represent a strictly future timestamp fails explicitly;
    it is never repaired by reading an execution-world due time.
    """
    if started_at is None or started_at > as_of:
        raise ValueError("conditional repair requires a public past started_at")
    if low < 0 or high < low:
        raise ValueError("invalid public repair duration support")
    if started_at + high <= as_of:
        raise ValueError("empty conditional repair support: service should already be completed")
    if low == high:
        return started_at + low
    lower = max(low, as_of - started_at)
    if lower >= high:
        raise ValueError("empty conditional repair support at floating-point resolution")
    total = rng.uniform(lower, high, entity_type="component",
                        entity_id=f"{aircraft_id}/LRU0", generation=generation,
                        purpose="conditional_repair_total")
    due = started_at + total
    if not due > as_of:
        raise ValueError("conditional repair draw is below future timestamp resolution")
    return due


class PlanningSimulation(WeeklySimulation):
    model_version = PLANNING_MODEL_VERSION

    def _publish_and_commit(self):
        raise RuntimeError("planning interval cannot publish or commit another window")

    def to_checkpoint(self):
        raise ValueError("planning worlds use planning artifacts, never execution checkpoints")

    def clone(self):
        # This clone is permitted only because construction used a legal input.
        return deepcopy(self)

    def reset(self, seed=None):
        raise ValueError("construct a new PlanningScenario from its legal PlanningInput")

    def summary(self):
        result = super().summary()
        result["parameter_basis"] = "synthetic conditional planning branch; realized short-window score; no terminal value"
        result["origin"] = deepcopy(self.origin)
        result["planning_metrics_delta"] = {
            key: value - self.origin["metric_offsets"][key]
            for key, value in asdict(self.metrics).items()}
        return result

    def planning_state(self):
        return _finite({
            "domain": "planning", "model_version": PLANNING_MODEL_VERSION,
            "conditional_distribution_version": CONDITIONAL_DISTRIBUTION_VERSION,
            "origin": deepcopy(self.origin), "time": self.now,
            "aircraft": [asdict(item) for item in self.aircraft.values()],
            "missions": [asdict(item) for item in self.missions.values()],
            "repairs": [asdict(item) for item in self.repairs.values()],
            "orders": [asdict(item) for item in self.orders.values()],
            "on_hand": self.on_hand, "metrics": asdict(self.metrics),
            "event_queue": [asdict(event) for event in sorted(self._queue)],
            "weekly_state": deepcopy(self.weekly_state),
            "events": deepcopy(self.events), "history": deepcopy(self.history),
            "random_manifest": self.keyed_rng.to_dict(),
        })


def _construct(inputs, legal_input_hash, scenario_id):
    request = inputs.request
    observation, context = request.observation, request.context
    now = observation.time
    sim = PlanningSimulation.__new__(PlanningSimulation)
    parameters = request.parameters.to_dict()
    allowed = {field.name for field in fields(Config)} - {"seed", "initial_spares"}
    public_config = {key: value for key, value in parameters.items() if key in allowed}
    started = sum(row["started_at"] is not None for row in observation.repairs)
    completed = sum(row["status"] == "completed" for row in observation.repairs)
    ordered = sum(row["quantity"] for row in observation.orders)
    arrived = sum(row["quantity"] for row in observation.orders if row["status"] == "arrived")
    # This offset is derived from public inventory/accounting facts and is only
    # needed by the inherited physical engine's conservation assertion.
    initial_spares_offset = observation.inventory["on_hand"] + started - arrived
    if initial_spares_offset < 0:
        raise ValueError("public inventory/repair/order ledger is inconsistent")
    public_config.update(seed=inputs.planning_seed, initial_spares=initial_spares_offset,
                         mission_rate_per_hour=0)
    sim.config = Config(**public_config)
    sim._input_missions = None
    sim._initial_lifetimes = None
    sim._rngs = {}
    sim.keyed_rng = KeyedRNG(inputs.planning_seed, scenario_id, domain="planning")
    sim.now = float(now)
    sim._queue, sim._sequence = [], 0
    sim.events, sim.history = [], []
    sim._running = sim._run_failed = sim._checkpoint_ready = False
    sim.on_hand = observation.inventory["on_hand"]
    sim.aircraft, sim.repairs, sim.orders, sim.missions = {}, {}, {}, {}
    generations = {row["id"]: sum(repair["aircraft_id"] == row["id"] and repair["status"] == "completed"
                                      for repair in observation.repairs)
                   for row in observation.aircraft}
    for row in observation.missions:
        spec = MissionSpec(**{name: row[name] for name in
                              ("id", "start_time", "duration_hours", "aircraft_required", "value")})
        actual_start, actual_end = row["actual_start"], row["actual_end"]
        flown = (0. if actual_start is None else
                 ((now if actual_end is None else actual_end) - actual_start) * len(row["assigned_aircraft"]))
        sim.missions[spec.id] = Mission(spec, row["status"], list(row["assigned_aircraft"]),
                                        actual_start, actual_end, flight_hours=flown)
    for row in observation.aircraft:
        state, aid = row["state"], row["id"]
        life = (sim.keyed_rng.exponential(
            sim.config.failure_rate_per_flight_hour, entity_type="component",
            entity_id=f"{aid}/LRU0", generation=generations[aid], purpose="conditional_remaining_life")
                if state in {"idle", "flying"} else 0.)
        aircraft = Aircraft(aid, state, life, row["flight_hours"], row["mission_id"])
        if state == "flying" and math.isfinite(life):
            aircraft.failure_due_at = now + life
            sim._schedule(aircraft.failure_due_at, "failure", aid, aircraft.version)
        sim.aircraft[aid] = aircraft
    if len(sim.aircraft) != sim.config.fleet_size:
        raise ValueError("public aircraft records disagree with fleet_size")
    for row in observation.repairs:
        repair = Repair(**deepcopy(row))
        if repair.status == "repairing":
            repair.due_at = conditional_repair_due(
                sim.keyed_rng, aircraft_id=repair.aircraft_id,
                generation=generations[repair.aircraft_id], started_at=repair.started_at,
                as_of=now, low=sim.config.repair_duration_min_hours,
                high=sim.config.repair_duration_max_hours)
            sim._schedule(repair.due_at, "repair_complete", repair.id)
        elif repair.status == "completed":
            repair.due_at = repair.completed_at  # already observed completion fact
        sim.repairs[repair.id] = repair
    for row in observation.orders:
        order = Order(**deepcopy(row))
        if order.status == "in_transit":
            if order.expected_arrival <= now:
                raise ValueError("public in-transit order should have arrived before decision")
            sim._schedule(order.expected_arrival, "order_arrival", order.id)
        sim.orders[order.id] = order
    if (sim.in_transit, sim.unmet_demand) != (observation.inventory["in_transit"],
                                            observation.inventory["unmet_demand"]):
        raise ValueError("public inventory totals disagree with orders/repair queue")
    for mission in sim.missions.values():
        if mission.status == "running":
            planned_end = mission.spec.start_time + mission.spec.duration_hours
            if planned_end <= now:
                raise ValueError("running public mission has already reached its planned end")
            sim._schedule(planned_end, "mission_end", mission.spec.id)
        elif mission.status == "pending" and now <= mission.spec.start_time < context.window_end:
            sim._schedule(mission.spec.start_time, "mission_start", mission.spec.id)
    commitments = {row["id"]: {"accepted": row["accepted"], "window_start": row["window_start"]}
                   for row in observation.commitments}
    sim.weekly_state = {
        "scenario_id": scenario_id, "policy_spec": PolicySpec().to_dict(),
        "rules_input": deepcopy(observation.rules),
        "announced_rule_versions": [rule["version"] for rule in observation.rules],
        "cohorts": {}, "mission_cohort": {
            row["id"]: str(int(math.floor(row["start_time"] / 168) * 168)) for row in observation.missions},
        "commitments": commitments, "weekly_commands": [], "daily_commands": [],
        # Scheduling cursors, not fabricated historical command records.
        "processed_week_points": [point for point in WEEK_POINTS if point < now],
        "processed_day_points": [point for point in DAY_POINTS if point < now],
        "component_generations": generations,
        "window_metrics": {str(int(point)): {key: 0. for key in INTEGRALS} for point in WEEK_POINTS},
        "closed_cohort_snapshots": [], "terminal_boundary": None,
    }
    sim.metrics = Metrics(flight_hours=math.fsum(row["flight_hours"] for row in observation.aircraft),
                          failures=len(observation.repairs), repairs_started=started,
                          repairs_completed=completed, ordered_quantity=ordered, arrived_quantity=arrived)
    sim.origin = {
        "as_of": now, "window_end": context.window_end, "legal_input_hash": legal_input_hash,
        "request_id": request.request_id, "scenario_id": scenario_id,
        "decision_protocol_version": request.protocol_version,
        "rule_context": context.to_dict(), "public_parameters": request.parameters.to_dict(),
        "planning_seed": inputs.planning_seed, "scenario_index": inputs.scenario_index,
        "metric_offsets": asdict(sim.metrics), "inventory_origin": deepcopy(observation.inventory),
        "initial_spares_conservation_offset": initial_spares_offset,
        "history_semantics": "one origin snapshot then simulated events; no fabricated past",
        "public_state": observation.to_dict(),
    }
    sim._snapshot()
    sim.assert_invariants()
    return sim


class PlanningScenario:
    def __init__(self, inputs: PlanningInput):
        if not isinstance(inputs, PlanningInput):
            raise ValueError("PlanningScenario requires PlanningInput, never Simulation/checkpoint")
        self.inputs = PlanningInput(inputs.request, inputs.planning_seed, inputs.scenario_index)
        self.legal_input_hash = _hash(self.inputs.request.to_dict())
        self.scenario_id = "planning-" + _hash({"legal_input_hash": self.legal_input_hash,
                                               "planning_seed": self.inputs.planning_seed,
                                               "scenario_index": self.inputs.scenario_index})
        self._world = _construct(self.inputs, self.legal_input_hash, self.scenario_id)

    def to_dict(self):
        return {"input_version": PLANNING_INPUT_VERSION, "model_version": PLANNING_MODEL_VERSION,
                "conditional_distribution_version": CONDITIONAL_DISTRIBUTION_VERSION,
                "scenario_id": self.scenario_id, "legal_input_hash": self.legal_input_hash,
                "planning_seed": self.inputs.planning_seed, "scenario_index": self.inputs.scenario_index,
                "initial_state": self._world.planning_state()}

    def evaluate(self, command: WeeklyCommand):
        request = self.inputs.request
        canonical = validate_command(command, request.observation, request.context)
        branch = self._world.clone()
        offered = [row["id"] for row in request.observation.missions
                   if request.context.window_start <= row["start_time"] < request.context.window_end]
        branch._commit_command(canonical, request.context, offered)
        branch.run_window(request.context.window_end)
        cohort = branch.weekly_state["cohorts"][str(int(request.context.window_start))]
        counts = branch._cohort_summary(cohort)
        score = sum(counts[name] if counts["validity"][name] else 0. for name in ("DSR", "RCR"))
        return {
            "score_version": BRANCH_SCORE_VERSION, "score": score,
            "score_components": {name: counts[name] if counts["validity"][name] else 0.
                                 for name in ("DSR", "RCR")},
            "validity": {name: counts["validity"][name] for name in ("DSR", "RCR")},
            "counts": counts, "unresolved": counts["unfinished"],
            "scenario_id": self.scenario_id, "scenario_index": self.inputs.scenario_index,
            "legal_input_hash": self.legal_input_hash,
            "origin": request.context.window_start, "window_end": request.context.window_end,
            "duration_hours": branch.now - request.context.window_start,
            "command": canonical.to_dict(), "model_version": PLANNING_MODEL_VERSION,
            "conditional_distribution_version": CONDITIONAL_DISTRIBUTION_VERSION,
            "metrics_delta": branch.summary()["planning_metrics_delta"],
            "final_state": branch.planning_state(),
        }
