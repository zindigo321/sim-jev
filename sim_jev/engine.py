"""Single explicit event engine. Time and exposure are measured in hours."""

from __future__ import annotations

from dataclasses import asdict, replace
import heapq
import json
import math
from collections.abc import Iterable, Sequence

import numpy as np

from .config import Config, require_number
from .metrics import Metrics, summarize
from .models import Aircraft, Event, Mission, MissionSpec, Order, Repair
from .policy import dispatch_aircraft, prioritize_repairs, reorder_quantity
from .versions import ARITHMETIC_PROTOCOL_LEGACY, ARITHMETIC_PROTOCOL_V2


EVENT_PRIORITY = {
    "failure": 0, "repair_complete": 1, "order_arrival": 2,
    "mission_end": 3, "try_repairs": 4, "reorder_check": 5, "mission_start": 6,
}


class Simulation:
    """Resettable simulator with separate audit export and internal checkpoints.

    ``missions=None`` generates a Poisson mission stream; ``[]`` means no missions.
    Optional initial lifetimes are a deterministic experiment/test input, not an
    observable PHM estimate. They do not change the post-replacement lifetime law.
    """

    def __init__(self, config: Config, missions: Iterable[MissionSpec] | None = None,
                 initial_lifetimes: Sequence[float] | None = None, *,
                 arithmetic_protocol: str = ARITHMETIC_PROTOCOL_LEGACY):
        if arithmetic_protocol not in {ARITHMETIC_PROTOCOL_LEGACY, ARITHMETIC_PROTOCOL_V2}:
            raise ValueError("unsupported arithmetic protocol")
        self.arithmetic_protocol = arithmetic_protocol
        config.validate()
        self.config = config
        self._input_missions = None if missions is None else tuple(missions)
        if self._input_missions is not None:
            ids = []
            for spec in self._input_missions:
                if not isinstance(spec, MissionSpec):
                    raise ValueError("missions must contain MissionSpec records")
                spec.validate()
                ids.append(spec.id)
            if len(set(ids)) != len(ids):
                raise ValueError("mission IDs must be unique")
        self._initial_lifetimes = None if initial_lifetimes is None else tuple(initial_lifetimes)
        if self._initial_lifetimes is not None:
            if len(self._initial_lifetimes) != config.fleet_size:
                raise ValueError("initial_lifetimes must contain one value per aircraft")
            for life in self._initial_lifetimes:
                if isinstance(life, bool) or not isinstance(life, (int, float)) or math.isnan(life) or life < 0:
                    raise ValueError("initial lifetimes must be nonnegative numbers (infinity allowed)")
        self.reset()

    def reset(self, seed: int | None = None) -> dict:
        self._checkpoint_ready = False
        self._running = False
        self._run_failed = False
        if seed is not None:
            self.config = replace(self.config, seed=seed)
        self._reset_randomness()
        self.now = 0.0
        self._queue: list[Event] = []
        self._sequence = 0
        self.events: list[dict] = []
        self.history: list[dict] = []
        self.metrics = Metrics()
        self.on_hand = self.config.initial_spares
        self.aircraft: dict[str, Aircraft] = {}
        self.repairs: dict[str, Repair] = {}
        self.orders: dict[str, Order] = {}
        for i in range(self.config.fleet_size):
            aircraft_id = f"A{i + 1:03d}"
            life = (self._initial_lifetimes[i] if self._initial_lifetimes is not None
                    else self._initial_lifetime(aircraft_id))
            self.aircraft[aircraft_id] = Aircraft(aircraft_id, remaining_life_hours=float(life))
        specs = self._generate_missions() if self._input_missions is None else self._input_missions
        self.missions = {spec.id: Mission(spec) for spec in sorted(specs, key=lambda m: (m.start_time, m.id))}
        self._schedule_initial_events()
        self._snapshot()
        self.assert_invariants()
        self._commit_accumulation_anchor()
        return self.get_observation()

    def _reset_randomness(self) -> None:
        streams = np.random.SeedSequence(self.config.seed).spawn(4)
        self._rngs = {name: np.random.default_rng(stream)
                      for name, stream in zip(("tasks", "failures", "repairs", "supply"), streams)}

    def _schedule_initial_events(self) -> None:
        for mission in self.missions.values():
            self._schedule(mission.spec.start_time, "mission_start", mission.spec.id)
        self._schedule(0.0, "reorder_check", "inventory")

    def _initial_lifetime(self, aircraft_id: str) -> float:
        return self._new_lifetime()

    def _replacement_lifetime(self, aircraft: Aircraft) -> float:
        return self._new_lifetime()

    def _repair_duration(self, repair: Repair) -> float:
        return self._uniform("repairs", self.config.repair_duration_min_hours,
                             self.config.repair_duration_max_hours)

    def _supply_lead_time(self) -> float:
        return self._uniform("supply", self.config.lead_time_min_hours,
                             self.config.lead_time_max_hours)

    def _uniform(self, stream: str, low: float, high: float) -> float:
        return float(low if low == high else self._rngs[stream].uniform(low, high))

    def _new_lifetime(self) -> float:
        rate = self.config.failure_rate_per_flight_hour
        return float(self._rngs["failures"].exponential(1.0 / rate)) if rate else math.inf

    def _generate_missions(self) -> list[MissionSpec]:
        c = self.config
        if c.mission_rate_per_hour == 0:
            return []
        result = []
        time = 0.0
        rng = self._rngs["tasks"]
        while True:
            next_time = time + float(rng.exponential(1.0 / c.mission_rate_per_hour))
            if next_time > c.horizon_hours:
                return result
            if next_time <= time:
                raise ValueError("mission arrival interval is below floating-point time resolution")
            time = next_time
            result.append(MissionSpec(
                f"M{len(result) + 1:06d}", time,
                self._uniform("tasks", c.mission_duration_min_hours, c.mission_duration_max_hours),
                int(rng.integers(c.mission_aircraft_min, c.mission_aircraft_max + 1)),
                self._uniform("tasks", c.mission_value_min, c.mission_value_max),
            ))

    @property
    def in_transit(self) -> int:
        return sum(order.quantity for order in self.orders.values() if order.status == "in_transit")

    @property
    def unmet_demand(self) -> int:
        return sum(repair.status == "waiting" for repair in self.repairs.values())

    def _schedule(self, time: float, kind: str, entity_id: str, version: int = 0) -> None:
        if not math.isfinite(time) or time < self.now:
            raise ValueError(f"invalid event time {time} for {kind}")
        self._sequence += 1
        heapq.heappush(self._queue, Event(float(time), EVENT_PRIORITY[kind], self._sequence,
                                         kind, entity_id, version))

    def _log(self, kind: str, entity_id: str, old: str = "", new: str = "", **details) -> None:
        self.events.append({"event_id": f"E{len(self.events) + 1:07d}", "time": self.now,
                            "event_type": kind, "entity_id": entity_id,
                            "old_state": old, "new_state": new,
                            "details": json.dumps(details, sort_keys=True, allow_nan=False)})

    def _stale(self, event: Event) -> None:
        self._log("stale_event", event.entity_id, ignored_kind=event.kind, sequence=event.sequence)

    def _snapshot(self) -> None:
        counts = {state: sum(a.state == state for a in self.aircraft.values())
                  for state in ("idle", "flying", "waiting_repair", "repairing")}
        self.history.append({"time": self.now, **counts, "on_hand": self.on_hand,
                             "in_transit": self.in_transit, "unmet_demand": self.unmet_demand})

    def _commit_accumulation_anchor(self) -> None:
        """Commit integrals only at a physical/control boundary, never a pause."""
        if getattr(self, "arithmetic_protocol", ARITHMETIC_PROTOCOL_LEGACY) != ARITHMETIC_PROTOCOL_V2:
            return
        from copy import deepcopy
        names = ("available_aircraft_hours", "idle_aircraft_hours", "flight_hours",
                 "spare_wait_hours", "bay_wait_hours")
        self._accumulation_anchor = {
            "time": self.now,
            "metrics": {name: getattr(self.metrics, name) for name in names},
            "aircraft": {key: value.flight_hours for key, value in self.aircraft.items()},
            "missions": {key: value.flight_hours for key, value in self.missions.items()},
            "repairs": {key: {name: getattr(value, name) for name in
                              ("spare_wait_hours", "bay_wait_hours")}
                        for key, value in self.repairs.items()},
        }
        if hasattr(self, "weekly_state"):
            self._accumulation_anchor["window_metrics"] = deepcopy(self.weekly_state["window_metrics"])

    def _restore_accumulation_anchor(self) -> None:
        """Discard the previous pause projection before advancing the same span."""
        if getattr(self, "arithmetic_protocol", ARITHMETIC_PROTOCOL_LEGACY) != ARITHMETIC_PROTOCOL_V2:
            return
        from copy import deepcopy
        anchor = self._accumulation_anchor
        for key, value in anchor["metrics"].items():
            setattr(self.metrics, key, value)
        for key, value in anchor["aircraft"].items():
            self.aircraft[key].flight_hours = value
        for key, value in anchor["missions"].items():
            self.missions[key].flight_hours = value
        for key, values in anchor["repairs"].items():
            for name, value in values.items():
                setattr(self.repairs[key], name, value)
        if "window_metrics" in anchor:
            self.weekly_state["window_metrics"] = deepcopy(anchor["window_metrics"])
        self.now = anchor["time"]

    def _advance(self, target: float) -> None:
        self._restore_accumulation_anchor()
        dt = target - self.now
        if dt == 0:
            return
        waiting = prioritize_repairs(self.repairs.values())
        covered = min(self.on_hand, len(waiting))
        flying = [a for a in self.aircraft.values() if a.state == "flying"]
        self.metrics.integrate(dt, sum(a.state == "idle" for a in self.aircraft.values()),
                               len(flying), len(waiting) - covered, covered)
        for i, repair in enumerate(waiting):
            if i < covered:
                repair.bay_wait_hours += dt
            else:
                repair.spare_wait_hours += dt
        for aircraft in flying:
            aircraft.flight_hours += dt
            # Anchor to the flight's fixed event time. Repeated dt subtraction
            # would let observation/run boundaries move a failure past mission end.
            if aircraft.failure_due_at is not None:
                aircraft.remaining_life_hours = max(0.0, aircraft.failure_due_at - target)
            self.missions[aircraft.mission_id].flight_hours += dt
        self.now = target

    def run(self, until: float | None = None) -> dict:
        target = self.config.horizon_hours if until is None else until
        require_number("until", target)
        if target < self.now or target > self.config.horizon_hours:
            raise ValueError("until must be between current time and configured horizon")
        if self._running:
            raise RuntimeError("run cannot be called inside an event handler")
        if self._run_failed:
            raise RuntimeError("a failed event run cannot resume; reset or restore a checkpoint")
        self._checkpoint_ready = False
        self._running = True
        handlers = {"mission_start": self._start_mission, "mission_end": self._end_mission,
                    "failure": self._fail, "repair_complete": self._complete_repair,
                    "order_arrival": self._arrive_order, "reorder_check": self._reorder,
                    "try_repairs": self._try_repairs}
        try:
            while self._queue and self._queue[0].time <= target:
                event = heapq.heappop(self._queue)
                self._advance(event.time)
                handlers[event.kind](event)
                self._commit_accumulation_anchor()
                self.assert_invariants()
                self._snapshot()
            if self.now < target:
                self._advance(float(target))
                self._snapshot()
            self.assert_invariants()
            self._checkpoint_ready = True
        except BaseException:
            self._run_failed = True
            raise
        finally:
            self._running = False
        return self.summary()

    def to_checkpoint(self) -> dict:
        """Return isolated, strict JSON internal state after a successful run().

        Contains hidden truth and execution RNG. Never use as planner input.
        A newly constructed/reset instance must first call ``run(until=0)``.
        """
        from .checkpoint import to_checkpoint
        return to_checkpoint(self)

    @classmethod
    def from_checkpoint(cls, data: dict) -> Simulation:
        """Validate and restore without generating tasks or sampling any RNG."""
        from .checkpoint import from_checkpoint
        return from_checkpoint(data, cls)

    def save_checkpoint(self, path) -> None:
        """Write a new strict JSON file; existing files are never overwritten."""
        from .checkpoint import save_checkpoint
        save_checkpoint(self, path)

    @classmethod
    def load_checkpoint(cls, path) -> Simulation:
        from .checkpoint import load_checkpoint
        return load_checkpoint(path, cls)

    def clone(self) -> Simulation:
        """Deeply isolated same-world replay, not a legal predictive scenario."""
        return type(self).from_checkpoint(self.to_checkpoint())

    def _start_mission(self, event: Event) -> None:
        mission = self.missions[event.entity_id]
        if mission.status != "pending":
            self._stale(event)
            return
        selected = dispatch_aircraft(self.aircraft.values(), mission.spec.aircraft_required)
        if not selected:
            mission.status = "not_executed"
            mission.actual_end = self.now
            self._log("mission_not_executed", mission.spec.id, "pending", mission.status,
                      reason="insufficient_idle_aircraft")
            return
        mission.status = "running"
        mission.actual_start = self.now
        mission.assigned_aircraft = [a.id for a in selected]
        self._log("mission_start", mission.spec.id, "pending", "running", aircraft=mission.assigned_aircraft)
        for aircraft in selected:
            aircraft.state = "flying"
            aircraft.mission_id = mission.spec.id
            aircraft.version += 1
            if math.isfinite(aircraft.remaining_life_hours):
                aircraft.failure_due_at = self.now + aircraft.remaining_life_hours
                self._schedule(aircraft.failure_due_at, "failure", aircraft.id, aircraft.version)
            self._log("aircraft_dispatch", aircraft.id, "idle", "flying", mission=mission.spec.id)
        self._schedule(self.now + mission.spec.duration_hours, "mission_end", mission.spec.id)

    def _release(self, aircraft: Aircraft, state: str) -> None:
        old = aircraft.state
        aircraft.state = state
        aircraft.mission_id = None
        aircraft.version += 1
        aircraft.failure_due_at = None
        self._log("aircraft_release", aircraft.id, old, state)

    def _end_mission(self, event: Event) -> None:
        mission = self.missions[event.entity_id]
        if mission.status != "running":
            self._stale(event)
            return
        mission.status = "success"
        mission.actual_end = self.now
        for aircraft_id in mission.assigned_aircraft:
            self._release(self.aircraft[aircraft_id], "idle")
        self._log("mission_success", mission.spec.id, "running", "success")

    def _fail(self, event: Event) -> None:
        aircraft = self.aircraft[event.entity_id]
        if aircraft.state != "flying" or aircraft.version != event.version:
            self._stale(event)
            return
        mission = self.missions[aircraft.mission_id]
        # Collect exact simultaneous failures before releasing healthy participants.
        failed = [self.aircraft[aid] for aid in mission.assigned_aircraft
                  if self.aircraft[aid].failure_due_at is not None
                  and self.aircraft[aid].failure_due_at <= self.now]
        mission.status = "failed"
        mission.actual_end = self.now
        mission.failed_aircraft = [a.id for a in failed]
        self._log("mission_failed", mission.spec.id, "running", "failed", aircraft=mission.failed_aircraft)
        for aid in mission.assigned_aircraft:
            item = self.aircraft[aid]
            if aid in mission.failed_aircraft:
                item.remaining_life_hours = 0.0
                self.metrics.failures += 1
                self._release(item, "waiting_repair")
                repair_id = f"R{len(self.repairs) + 1:06d}"
                self.repairs[repair_id] = Repair(repair_id, aid, self.now)
                self._log("failure", aid, "flying", "waiting_repair", repair_id=repair_id)
            else:
                self._release(item, "idle")
        self._schedule(self.now, "try_repairs", "maintenance")

    def _try_repairs(self, event: Event) -> None:
        active = sum(r.status == "repairing" for r in self.repairs.values())
        for repair in prioritize_repairs(self.repairs.values()):
            if self.on_hand == 0 or active >= self.config.repair_bays:
                break
            self.on_hand -= 1
            active += 1
            self.metrics.repairs_started += 1
            repair.status = "repairing"
            repair.started_at = self.now
            duration = self._repair_duration(repair)
            repair.due_at = self.now + duration
            aircraft = self.aircraft[repair.aircraft_id]
            aircraft.state = "repairing"
            self._schedule(repair.due_at, "repair_complete", repair.id)
            self._log("repair_start", repair.id, "waiting", "repairing",
                      aircraft=aircraft.id, on_hand=self.on_hand)

    def _complete_repair(self, event: Event) -> None:
        repair = self.repairs[event.entity_id]
        if repair.status != "repairing":
            self._stale(event)
            return
        repair.status = "completed"
        repair.completed_at = self.now
        self.metrics.repairs_completed += 1
        aircraft = self.aircraft[repair.aircraft_id]
        aircraft.remaining_life_hours = self._replacement_lifetime(aircraft)
        aircraft.state = "idle"
        aircraft.version += 1
        self._log("repair_complete", repair.id, "repairing", "completed", aircraft=aircraft.id)
        self._schedule(self.now, "try_repairs", "maintenance")

    def _reorder(self, event: Event) -> None:
        self._reorder_once()
        next_check = self.now + self.config.reorder_interval_hours
        if next_check <= self.now:
            raise ValueError("reorder interval is below floating-point time resolution")
        if next_check <= self.config.horizon_hours:
            self._schedule(next_check, "reorder_check", "inventory")

    def _reorder_once(self) -> None:
        """One inventory opportunity; the model-specific scheduler owns cadence."""
        quantity = reorder_quantity(self.on_hand, self.in_transit, self.unmet_demand,
                                    self.config.reorder_target)
        self._log("reorder_check", "inventory", quantity=quantity,
                  inventory_position=self.on_hand + self.in_transit - self.unmet_demand)
        if quantity:
            order_id = f"O{len(self.orders) + 1:06d}"
            lead = self._supply_lead_time()
            order = Order(order_id, quantity, self.now, self.now + lead)
            self.orders[order_id] = order
            self.metrics.ordered_quantity += quantity
            self._schedule(order.expected_arrival, "order_arrival", order.id)
            self._log("order_placed", order.id, "", "in_transit", quantity=quantity,
                      expected_arrival=order.expected_arrival)

    def _arrive_order(self, event: Event) -> None:
        order = self.orders[event.entity_id]
        if order.status != "in_transit":
            self._stale(event)
            return
        order.status = "arrived"
        order.arrived_at = self.now
        self.on_hand += order.quantity
        self.metrics.arrived_quantity += order.quantity
        self._log("order_arrival", order.id, "in_transit", "arrived", quantity=order.quantity,
                  on_hand=self.on_hand)
        self._schedule(self.now, "try_repairs", "maintenance")

    def summary(self) -> dict:
        return summarize(self)

    def get_observation(self) -> dict:
        """Known information only; task announcement is at its scheduled start."""
        return {
            "time": self.now,
            "aircraft": [{"id": a.id, "state": a.state, "mission_id": a.mission_id,
                          "flight_hours": a.flight_hours} for a in self.aircraft.values()],
            "missions": [{**asdict(m.spec), "status": m.status,
                          "assigned_aircraft": list(m.assigned_aircraft),
                          "actual_start": m.actual_start, "actual_end": m.actual_end}
                         for m in self.missions.values() if m.spec.start_time <= self.now],
            "repairs": [{"id": r.id, "aircraft_id": r.aircraft_id, "status": r.status,
                         "requested_at": r.requested_at, "started_at": r.started_at,
                         "completed_at": r.completed_at,
                         "spare_wait_hours": r.spare_wait_hours, "bay_wait_hours": r.bay_wait_hours}
                        for r in self.repairs.values()],
            "orders": [asdict(o) for o in self.orders.values()],
            "inventory": {"on_hand": self.on_hand, "in_transit": self.in_transit,
                          "unmet_demand": self.unmet_demand,
                          "position": self.on_hand + self.in_transit - self.unmet_demand},
        }

    def export_state(self) -> dict:
        """Internal audit export includes hidden state; never feed this to a policy."""
        data = {"schema_version": 1, "resume_supported": False, "time": self.now,
                "config": self.config.to_dict(), "on_hand": self.on_hand,
                "aircraft": [asdict(a) for a in self.aircraft.values()],
                "missions": [asdict(m) for m in self.missions.values()],
                "repairs": [asdict(r) for r in self.repairs.values()],
                "orders": [asdict(o) for o in self.orders.values()],
                "event_queue": [asdict(e) for e in sorted(self._queue)],
                "event_sequence": self._sequence, "log_sequence": len(self.events),
                "metrics": asdict(self.metrics),
                "rng_states": {name: rng.bit_generator.state for name, rng in self._rngs.items()},
                "input_missions": None if self._input_missions is None else [asdict(m) for m in self._input_missions],
                "initial_lifetimes": self._initial_lifetimes}

        def finite_json(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None  # Infinite disabled-failure lifetime is encoded as null.
            if isinstance(value, dict):
                return {k: finite_json(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [finite_json(v) for v in value]
            return value

        return finite_json(data)

    def assert_invariants(self) -> None:
        """Conservation and mutual exclusion checks run after every queued event."""
        def check(condition: bool, message: str) -> None:
            if not condition:
                raise RuntimeError(f"simulation invariant violated: {message}")

        active = [r for r in self.repairs.values() if r.status != "completed"]
        repairing = [r for r in active if r.status == "repairing"]
        check(0 <= len(repairing) <= self.config.repair_bays, "repair capacity")
        check(self.on_hand >= 0, "negative inventory")
        check(self.on_hand == self.config.initial_spares + self.metrics.arrived_quantity
              - self.metrics.repairs_started, "spare conservation")
        check(self.in_transit == self.metrics.ordered_quantity - self.metrics.arrived_quantity,
              "in-transit conservation")
        check(len({r.aircraft_id for r in active}) == len(active), "duplicate active repair")
        active_by_aircraft = {r.aircraft_id: r for r in active}
        for a in self.aircraft.values():
            check(a.state in {"idle", "flying", "waiting_repair", "repairing"}, "aircraft state")
            check(a.flight_hours >= 0 and a.remaining_life_hours >= 0, "negative exposure/life")
            if a.state == "flying":
                check(a.mission_id in self.missions, "missing flying mission")
                m = self.missions[a.mission_id]
                check(m.status == "running" and a.id in m.assigned_aircraft, "flying ownership")
                check(a.id not in active_by_aircraft, "flying during maintenance")
            else:
                check(a.mission_id is None and a.failure_due_at is None, "ground mission ownership")
            if a.state in {"waiting_repair", "repairing"}:
                check(a.id in active_by_aircraft, "missing repair")
                expected = "waiting" if a.state == "waiting_repair" else "repairing"
                check(active_by_aircraft[a.id].status == expected, "repair state disagreement")
            else:
                check(a.id not in active_by_aircraft, "unexpected repair")
        for m in self.missions.values():
            check(len(set(m.assigned_aircraft)) == len(m.assigned_aircraft), "duplicate mission aircraft")
            if m.status == "running":
                check(len(m.assigned_aircraft) == m.spec.aircraft_required, "partial dispatch")
                check(all(self.aircraft[aid].state == "flying" and self.aircraft[aid].mission_id == m.spec.id
                          for aid in m.assigned_aircraft), "mission ownership")
        check(math.isclose(sum(a.flight_hours for a in self.aircraft.values()), self.metrics.flight_hours,
                           rel_tol=1e-10, abs_tol=1e-9), "aircraft flight-hour conservation")
        check(math.isclose(sum(m.flight_hours for m in self.missions.values()), self.metrics.flight_hours,
                           rel_tol=1e-10, abs_tol=1e-9), "mission flight-hour conservation")
