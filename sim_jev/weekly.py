"""Versioned weekly commitments over the shared single-LRU physical engine.

This is a synthetic software baseline, with no PHM observations or lookahead.
All decisions execute inside run's timestamp barrier, never after run returns.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import heapq
import math

from .config import Config, require_number
from .engine import Simulation
from .keyed_rng import KeyedRNG
from .models import MissionSpec
from .weekly_policy import (PolicyObservation, PolicySpec, RuleContext, RuleSpec,
                            validate_command)
from .decision import (DecisionPolicySpec, DecisionRequest, ModelParameters,
                       DECISION_PROTOCOL_VERSION, policy_from_dict)
from .decision_resolver import resolve_decision
from .versions import (WEEKLY_MODEL_VERSION, KEYED_RNG_PROTOCOL_VERSION,
                       WEEKLY_CHECKPOINT_SCHEMA_VERSION, DAILY_POLICY_VERSION)

WEEK_POINTS = (0., 168., 336., 504., 672.)
DAY_POINTS = tuple(float(t) for t in range(0, 720, 24))
INTEGRALS = ("flight_hours", "available_aircraft_hours", "idle_aircraft_hours",
             "spare_wait_hours", "bay_wait_hours")


class WeeklySimulation(Simulation):
    model_version = WEEKLY_MODEL_VERSION
    rng_protocol_version = KEYED_RNG_PROTOCOL_VERSION
    checkpoint_schema_version = WEEKLY_CHECKPOINT_SCHEMA_VERSION

    def __init__(self, config: Config, missions=None, initial_lifetimes=None, *,
                 scenario_id="weekly-synthetic", rules=None, policy=None, decision_runtime=None):
        if config.horizon_hours != 720 or config.reorder_interval_hours != 24:
            raise ValueError("weekly mode requires horizon_hours=720 and reorder_interval_hours=24")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise ValueError("scenario_id must be nonempty text")
        policy = PolicySpec() if policy is None else policy
        if not isinstance(policy, (PolicySpec, DecisionPolicySpec)):
            raise ValueError("policy must be a serializable PolicySpec, not a callback")
        self._decision_runtime = decision_runtime
        supplied = [] if rules is None else list(rules)
        if any(not isinstance(rule, RuleSpec) for rule in supplied):
            raise ValueError("rules must contain RuleSpec records")
        covered = {r.window_start for r in supplied}
        supplied += [RuleSpec(t, min(t + 168., 720.), t, t, f"default-{int(t)}")
                     for t in WEEK_POINTS if t not in covered]
        if len({r.version for r in supplied}) != len(supplied):
            raise ValueError("rule versions must be unique")
        # Only one applicable rule per commitment window in this limited model.
        if len(supplied) != len(WEEK_POINTS) or {r.window_start for r in supplied} != set(WEEK_POINTS):
            raise ValueError("exactly one rule per weekly window is supported")
        self.weekly_state = {"scenario_id": scenario_id,
                             "policy_spec": policy.to_dict(),
                             "rules_input": [r.to_dict() for r in sorted(supplied, key=lambda r: r.window_start)]}
        super().__init__(config, missions, initial_lifetimes)
        ids = set(self.missions)
        for rule in supplied:
            if not set(rule.mandatory_ids) <= ids:
                raise ValueError("mandatory rule references unknown missions")
            if any(not rule.window_start <= self.missions[mid].spec.start_time < rule.window_end
                   for mid in rule.mandatory_ids):
                raise ValueError("mandatory obligations must belong to their start window")

    def reset(self, seed=None):
        inputs = {key: deepcopy(self.weekly_state[key]) for key in
                  ("scenario_id", "policy_spec", "rules_input")}
        self.weekly_state = {
            **inputs, "announced_rule_versions": [], "cohorts": {}, "mission_cohort": {},
            "commitments": {}, "weekly_commands": [], "daily_commands": [],
            "processed_week_points": [], "processed_day_points": [],
            "component_generations": {f"A{i + 1:03d}": 0 for i in range(self.config.fleet_size)},
            "window_metrics": {str(int(t)): {key: 0. for key in INTEGRALS} for t in WEEK_POINTS},
            "closed_cohort_snapshots": [], "terminal_boundary": None,
        }
        if inputs["policy_spec"]["kind"] == "candidate-des":
            self.weekly_state.update(decision_protocol_version=DECISION_PROTOCOL_VERSION,
                                     decision_records=[])
        return super().reset(seed)

    def _reset_randomness(self):
        self._rngs = {}
        self.keyed_rng = KeyedRNG(self.config.seed, self.weekly_state["scenario_id"])

    def _schedule_initial_events(self):
        for mission in self.missions.values():
            if mission.spec.start_time < 720:
                self._schedule(mission.spec.start_time, "mission_start", mission.spec.id)

    def _initial_lifetime(self, aircraft_id):
        return self.keyed_rng.exponential(
            self.config.failure_rate_per_flight_hour, entity_type="component",
            entity_id=f"{aircraft_id}/LRU0", generation=0, purpose="lifetime")

    def _replacement_lifetime(self, aircraft):
        generations = self.weekly_state["component_generations"]
        generations[aircraft.id] += 1
        return self.keyed_rng.exponential(
            self.config.failure_rate_per_flight_hour, entity_type="component",
            entity_id=f"{aircraft.id}/LRU0", generation=generations[aircraft.id], purpose="lifetime")

    def _repair_duration(self, repair):
        return self.keyed_rng.uniform(
            self.config.repair_duration_min_hours, self.config.repair_duration_max_hours,
            entity_type="component", entity_id=f"{repair.aircraft_id}/LRU0",
            generation=self.weekly_state["component_generations"][repair.aircraft_id],
            purpose="repair_duration")

    def _supply_lead_time(self):
        return self.keyed_rng.uniform(
            self.config.lead_time_min_hours, self.config.lead_time_max_hours,
            entity_type="supplier", entity_id="single/LRU0", generation=int(self.now / 24),
            purpose="lead_time")

    def _generate_missions(self):
        c, rng = self.config, self.keyed_rng
        if c.mission_rate_per_hour == 0:
            return []
        result, time, index = [], 0., 0
        while True:
            key = dict(entity_type="arrival", entity_id=str(index), generation=0)
            next_time = time + rng.exponential(c.mission_rate_per_hour, purpose="interval", **key)
            if next_time >= 720:
                return result
            if next_time <= time:
                raise ValueError("mission interval is below floating-point time resolution")
            time = next_time
            result.append(MissionSpec(
                f"M{index + 1:06d}", time,
                rng.uniform(c.mission_duration_min_hours, c.mission_duration_max_hours,
                            purpose="duration", **key),
                rng.integer(c.mission_aircraft_min, c.mission_aircraft_max,
                            purpose="aircraft_required", **key),
                rng.uniform(c.mission_value_min, c.mission_value_max, purpose="value", **key)))
            index += 1

    def _advance(self, target):
        before = {key: getattr(self.metrics, key) for key in INTEGRALS}
        window = str(int(min(672, math.floor(self.now / 168) * 168)))
        super()._advance(target)
        for key in INTEGRALS:
            self.weekly_state["window_metrics"][window][key] += getattr(self.metrics, key) - before[key]

    def _rule_context(self, point):
        rules = [RuleSpec.from_dict(r) for r in self.weekly_state["rules_input"]
                 if r["window_start"] == point and r["announced_at"] <= self.now
                 and r["effective_at"] <= self.now]
        if len(rules) != 1:
            raise ValueError("weekly commitment requires one announced, effective rule")
        carry = tuple({**asdict(self.missions[mid].spec), "status": self.missions[mid].status}
                      for mid, item in self.weekly_state["commitments"].items()
                      if item["accepted"] and self.missions[mid].status in {"pending", "running"}
                      and self.missions[mid].spec.start_time < point)
        return RuleContext(point, min(point + 168., 720.), self.config.fleet_size, rules[0], carry)

    def _publish_request(self):
        state = self.weekly_state
        for rule in state["rules_input"]:
            if rule["announced_at"] <= self.now and rule["version"] not in state["announced_rule_versions"]:
                state["announced_rule_versions"].append(rule["version"])
                self._log("rule_announced", rule["version"], rule=deepcopy(rule))
        point, end = self.now, min(self.now + 168., 720.)
        key = str(int(point))
        offered = [m.spec.id for m in self.missions.values() if point <= m.spec.start_time < end]
        for mid in offered:
            state["mission_cohort"][mid] = key
        self._log("weekly_offer", key, offered_ids=offered, window_end=end)
        context = self._rule_context(point)
        request = DecisionRequest.create(self.get_observation(), context,
                                         ModelParameters.from_config(self.config))
        return request, offered

    def _publish_and_commit(self):
        request, offered = self._publish_request()
        result = resolve_decision(request, policy_from_dict(self.weekly_state["policy_spec"]),
                                  getattr(self, "_decision_runtime", None))
        command = validate_command(result.command, request.observation, request.context)
        if result.record is not None:
            self.weekly_state["decision_records"].append(deepcopy(result.record))
        self._commit_command(command, request.context, offered)

    def _commit_command(self, command, context, offered):
        state = self.weekly_state
        point, end = context.window_start, context.window_end
        if point != self.now or point in state["processed_week_points"]:
            raise ValueError("weekly command must be committed once at its decision point")
        # All routes, including planning branches, share this final gate.
        command = validate_command(command, self.get_observation(), context)
        key = str(int(point))
        accepted = list(command.accepted_ids)
        rejected = [mid for mid in offered if mid not in accepted]
        record = {"window_start": point, "window_end": end, "offered_ids": offered,
                  "accepted_ids": accepted, "rejected_ids": rejected,
                  "offered_value": sum(self.missions[mid].spec.value for mid in offered),
                  "accepted_value": sum(self.missions[mid].spec.value for mid in accepted),
                  "mandatory_ids": list(context.active_rule.mandatory_ids), "command": command.to_dict()}
        state["cohorts"][key] = record
        for mid in offered:
            state["commitments"][mid] = {"accepted": mid in accepted, "window_start": point}
            if mid not in accepted:
                self.missions[mid].status = "rejected"
        state["weekly_commands"].append(command.to_dict())
        state["processed_week_points"].append(point)
        self._log("weekly_command", key, command=command.to_dict())

    def _daily_command(self):
        command = {"time": self.now, "opportunity_index": int(self.now / 24),
                   "policy_version": DAILY_POLICY_VERSION, "dispatch": "aircraft-id",
                   "repairs": "request-time-id-fifo", "replenishment": "daily-inventory-position"}
        self.weekly_state["daily_commands"].append(command)
        self.weekly_state["processed_day_points"].append(self.now)
        self._log("daily_command", str(command["opportunity_index"]), command=command)
        self._reorder_once()

    def _start_mission(self, event):
        mission = self.missions[event.entity_id]
        if mission.status == "rejected":
            self._log("mission_rejected_at_start", mission.spec.id)
            return
        if not self.weekly_state["commitments"].get(mission.spec.id, {}).get("accepted"):
            raise RuntimeError("mission reached dispatch without a weekly commitment")
        super()._start_mission(event)

    def _close_cohorts(self):
        existing = {row["window_start"] for row in self.weekly_state["closed_cohort_snapshots"]}
        for cohort in self.weekly_state["cohorts"].values():
            if cohort["window_end"] <= self.now and cohort["window_start"] not in existing:
                snapshot = {"window_start": cohort["window_start"], "as_of": self.now,
                            "counts": self._cohort_summary(cohort)}
                self.weekly_state["closed_cohort_snapshots"].append(snapshot)

    def run(self, until=None):
        return self._run_interval(until)

    def run_window(self, window_end):
        """Drain endpoint physical closure, stop before its public/control phase.

        This boundary is intended for PlanningSimulation branches. It is not an
        execution checkpoint barrier: a subsequent ordinary run may publish the
        next window. Exact adjacent binary64 times remain distinct.
        """
        if window_end not in (168., 336., 504., 672., 720.):
            raise ValueError("window_end must be a weekly physical boundary")
        return self._run_interval(window_end, stop_before_control=True)

    def _run_interval(self, until=None, *, stop_before_control=False):
        target = 720. if until is None else until
        require_number("until", target)
        if not self.now <= target <= 720:
            raise ValueError("until must be between current time and 720")
        if self._running or self._run_failed:
            raise RuntimeError("run requires a non-running, successful stable instance")
        self._checkpoint_ready, self._running = False, True
        handlers = {"mission_start": self._start_mission, "mission_end": self._end_mission,
                    "failure": self._fail, "repair_complete": self._complete_repair,
                    "order_arrival": self._arrive_order, "try_repairs": self._try_repairs}

        def drain(*, physical_only=False):
            # Physical priorities precede starts; every newly caused physical
            # event is drained before the next start, including zero durations.
            while self._queue and self._queue[0].time == self.now:
                if physical_only and self._queue[0].kind == "mission_start":
                    break
                event = heapq.heappop(self._queue)
                handlers[event.kind](event)
                self.assert_invariants()
                self._snapshot()

        try:
            while True:
                weeks = [p for p in WEEK_POINTS if p not in self.weekly_state["processed_week_points"]]
                days = [p for p in DAY_POINTS if p not in self.weekly_state["processed_day_points"]]
                candidates = ([self._queue[0].time] if self._queue else []) + weeks[:1] + days[:1]
                if not candidates or min(candidates) > target:
                    break
                time = min(candidates)
                self._advance(time)
                drain(physical_only=True)
                if stop_before_control and time == target:
                    self._close_cohorts()
                    self._snapshot()
                    break
                if time in weeks:
                    self._close_cohorts()
                    self._publish_and_commit()
                    self._snapshot()
                if time in days:
                    self._daily_command()
                    self._snapshot()
                drain()
            if self.now < target:
                self._advance(float(target))
                self._snapshot()
            if self.now == 720 and self.weekly_state["terminal_boundary"] is None:
                boundary = {"time": 720., "event_ids": [r["event_id"] for r in self.events if r["time"] == 720]}
                self.weekly_state["terminal_boundary"] = boundary
                self._close_cohorts()
                self._log("terminal_boundary", "720", **boundary)
            self.assert_invariants()
            self._checkpoint_ready = not stop_before_control or self.now == 720
        except BaseException:
            self._run_failed = True
            raise
        finally:
            self._running = False
        return self.summary()

    def get_observation(self):
        visible = set(self.weekly_state["mission_cohort"])
        return PolicyObservation(
            time=self.now, fleet_size=self.config.fleet_size,
            aircraft=[{"id": a.id, "state": a.state, "mission_id": a.mission_id,
                       "flight_hours": a.flight_hours} for a in self.aircraft.values()],
            missions=[{**asdict(m.spec), "status": m.status, "assigned_aircraft": list(m.assigned_aircraft),
                       "actual_start": m.actual_start, "actual_end": m.actual_end}
                      for mid, m in self.missions.items() if mid in visible],
            repairs=[{"id": r.id, "aircraft_id": r.aircraft_id, "status": r.status,
                      "requested_at": r.requested_at, "started_at": r.started_at,
                      "completed_at": r.completed_at, "spare_wait_hours": r.spare_wait_hours,
                      "bay_wait_hours": r.bay_wait_hours} for r in self.repairs.values()],
            orders=[asdict(o) for o in self.orders.values()],
            inventory={"on_hand": self.on_hand, "in_transit": self.in_transit,
                       "unmet_demand": self.unmet_demand},
            rules=deepcopy([r for r in self.weekly_state["rules_input"] if r["announced_at"] <= self.now]),
            commitments=[{**asdict(self.missions[mid].spec), "status": self.missions[mid].status,
                          **deepcopy(record)} for mid, record in self.weekly_state["commitments"].items()])

    def _cohort_summary(self, cohort):
        offered = cohort["offered_ids"]
        accepted = cohort["accepted_ids"]
        counts = {"offered": len(offered), "accepted": len(accepted),
                  "rejected": len(cohort["rejected_ids"])}
        for output, status in (("success", "success"), ("failed", "failed"),
                               ("accepted_not_executed", "not_executed"),
                               ("pending_execution", "pending"), ("running", "running")):
            counts[output] = sum(self.missions[mid].status == status for mid in accepted)
        counts["unfinished"] = counts["pending_execution"] + counts["running"]
        counts["launched"] = counts["success"] + counts["failed"] + counts["running"]
        counts["offered_value"] = cohort["offered_value"]
        counts["accepted_value"] = cohort["accepted_value"]
        counts["earned_value"] = sum(self.missions[mid].spec.value for mid in accepted
                                      if self.missions[mid].status == "success")
        counts["mandatory_shortfall_as_of"] = sum(self.missions[mid].status != "success"
                                                   for mid in cohort["mandatory_ids"])
        counts["mandatory_shortfall_terminal"] = counts["mandatory_shortfall_as_of"] if self.now == 720 else None
        self._ratios(counts)
        return counts

    @staticmethod
    def _ratios(counts):
        valid = {}
        for name, numerator, denominator in (("DSR", "success", "offered"),
                                              ("RCR", "earned_value", "offered_value"),
                                              ("ASR", "success", "accepted"),
                                              ("acceptance_rate", "accepted", "offered")):
            valid[name] = counts[denominator] != 0
            counts[name] = counts[numerator] / counts[denominator] if valid[name] else None
        counts["validity"] = valid

    def summary(self):
        cohorts = [{"window_start": c["window_start"], "window_end": c["window_end"],
                    **self._cohort_summary(c)} for c in self.weekly_state["cohorts"].values()]
        keys = ("offered", "accepted", "rejected", "success", "failed", "accepted_not_executed",
                "pending_execution", "running", "unfinished", "launched", "offered_value",
                "accepted_value", "earned_value", "mandatory_shortfall_as_of")
        result = {key: sum(c[key] for c in cohorts) for key in keys}
        result["mandatory_shortfall_terminal"] = result["mandatory_shortfall_as_of"] if self.now == 720 else None
        self._ratios(result)
        denominator = self.config.fleet_size * self.now
        result.update(model_version=self.model_version, rng_protocol_version=self.rng_protocol_version,
                      parameter_basis="synthetic software baseline; no PHM prediction or paper replication",
                      seed=self.config.seed, elapsed_hours=self.now, configured_horizon_hours=720.,
                      cohorts=cohorts, **asdict(self.metrics), on_hand=self.on_hand,
                      in_transit=self.in_transit, unmet_demand=self.unmet_demand,
                      inventory_position=self.on_hand + self.in_transit - self.unmet_demand,
                      availability=self.metrics.available_aircraft_hours / denominator if denominator else None,
                      idle_fraction=self.metrics.idle_aircraft_hours / denominator if denominator else None,
                      actual_idle=sum(a.state == "idle" for a in self.aircraft.values()),
                      actual_available=sum(a.state in {"idle", "flying"} for a in self.aircraft.values()),
                      planned_aircraft_hours=sum(c["command"]["plan_usage"]["aircraft_hours"]
                                                for c in self.weekly_state["cohorts"].values()),
                      window_metrics=deepcopy(self.weekly_state["window_metrics"]),
                      terminal_boundary=deepcopy(self.weekly_state["terminal_boundary"]))
        return result

    def assert_invariants(self):
        super().assert_invariants()
        for cohort in self.weekly_state["cohorts"].values():
            row = self._cohort_summary(cohort)
            if row["accepted"] + row["rejected"] != row["offered"] or sum(
                row[k] for k in ("success", "failed", "accepted_not_executed", "unfinished")) != row["accepted"]:
                raise RuntimeError("weekly cohort conservation violated")

    def to_checkpoint(self):
        from .weekly_checkpoint import to_checkpoint
        return to_checkpoint(self)

    @classmethod
    def from_checkpoint(cls, data):
        from .weekly_checkpoint import from_checkpoint
        return from_checkpoint(data, cls)

    def save_checkpoint(self, path):
        from .weekly_checkpoint import save_checkpoint
        save_checkpoint(self, path)

    @classmethod
    def load_checkpoint(cls, path):
        from .weekly_checkpoint import load_checkpoint
        return load_checkpoint(path, cls)

    def export_state(self):
        return self.to_checkpoint()
