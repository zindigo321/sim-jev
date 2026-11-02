"""Legal observations, public rules and deterministic weekly commitments.

Planning capacity is nominal fleet capacity, including currently unavailable
aircraft. No hidden repair completion or lifetime is used to release capacity.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, fields
import json
import math
from typing import Iterable

from .config import require_integer, require_number
from .models import MissionSpec


WEEK_POINTS = (0, 168, 336, 504, 672)


def _shape(data, cls):
    names = {item.name for item in fields(cls)}
    if not isinstance(data, dict) or set(data) != names:
        raise ValueError(f"{cls.__name__}: expected exactly {sorted(names)}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _ids(value, name):
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} must be an array")
    for item in value:
        _text(item, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must contain unique IDs")
    return tuple(value)


def _week(value, name):
    require_number(name, value)
    if value not in WEEK_POINTS:
        raise ValueError(f"{name} must be a weekly decision point")


def _json_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError("policy data must be strict finite JSON") from error


class _Record:
    def to_dict(self) -> dict:
        return _json_copy(asdict(self))


@dataclass(frozen=True)
class RuleSpec(_Record):
    window_start: float
    window_end: float
    announced_at: float
    effective_at: float
    version: str
    mandatory_ids: tuple[str, ...] = ()
    min_reserve: int = 0
    max_aircraft_hours: float | None = None

    def __post_init__(self):
        _week(self.window_start, "window_start")
        require_number("window_end", self.window_end)
        if self.window_end != min(self.window_start + 168, 720):
            raise ValueError("rule window must be the exact half-open weekly window")
        _week(self.announced_at, "announced_at")
        _week(self.effective_at, "effective_at")
        if not self.announced_at <= self.effective_at <= self.window_start:
            raise ValueError("require announced_at <= effective_at <= window_start")
        _text(self.version, "version")
        object.__setattr__(self, "mandatory_ids", _ids(self.mandatory_ids, "mandatory_ids"))
        require_integer("min_reserve", self.min_reserve)
        if self.max_aircraft_hours is not None:
            require_number("max_aircraft_hours", self.max_aircraft_hours)

    @classmethod
    def from_dict(cls, data):
        _shape(data, cls)
        return cls(**copy.deepcopy(data))


@dataclass(frozen=True)
class RuleContext(_Record):
    window_start: float
    window_end: float
    fleet_size: int
    active_rule: RuleSpec
    carry_missions: tuple[dict, ...] = ()

    def __post_init__(self):
        _week(self.window_start, "window_start")
        require_number("window_end", self.window_end)
        require_integer("fleet_size", self.fleet_size, 1)
        if not isinstance(self.active_rule, RuleSpec):
            raise ValueError("active_rule must be RuleSpec")
        if (self.window_start, self.window_end) != (
                self.active_rule.window_start, self.active_rule.window_end):
            raise ValueError("active rule belongs to a different window")
        if self.active_rule.min_reserve > self.fleet_size:
            raise ValueError("min_reserve exceeds nominal fleet size")
        if not isinstance(self.carry_missions, (tuple, list)):
            raise ValueError("carry_missions must be an array")
        object.__setattr__(self, "carry_missions", tuple(_json_copy(self.carry_missions)))
        _mission_map(self.carry_missions)

    @classmethod
    def from_dict(cls, data):
        _shape(data, cls)
        data = copy.deepcopy(data)
        data["active_rule"] = RuleSpec.from_dict(data["active_rule"])
        return cls(**data)


@dataclass(frozen=True)
class PolicyObservation(_Record):
    time: float
    fleet_size: int
    aircraft: list[dict]
    missions: list[dict]
    repairs: list[dict]
    orders: list[dict]
    inventory: dict
    rules: list[dict]
    commitments: list[dict]

    def __post_init__(self):
        require_number("observation time", self.time)
        require_integer("fleet_size", self.fleet_size, 1)
        for name in ("aircraft", "missions", "repairs", "orders", "rules", "commitments"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError(f"{name} must be an array of objects")
            object.__setattr__(self, name, _json_copy(value))
        if not isinstance(self.inventory, dict):
            raise ValueError("inventory must be an object")
        object.__setattr__(self, "inventory", _json_copy(self.inventory))
        forbidden = {"remaining_life_hours", "failure_due_at", "due_at", "rng_states",
                     "root_seed", "event_queue", "execution_rng", "initial_lifetimes"}

        def check(value):
            if isinstance(value, dict):
                if forbidden & value.keys():
                    raise ValueError("hidden execution information in PolicyObservation")
                for nested in value.values():
                    check(nested)
            elif isinstance(value, list):
                for nested in value:
                    check(nested)
        check(asdict(self))

    @classmethod
    def from_dict(cls, data):
        _shape(data, cls)
        return cls(**copy.deepcopy(data))


@dataclass(frozen=True)
class WeeklyCommand(_Record):
    window_start: float
    accepted_ids: tuple[str, ...]
    mandatory_ids: tuple[str, ...] = ()
    missing_mandatory: tuple[str, ...] = ()
    fallback: bool = False
    reasons: tuple[dict, ...] = ()
    plan_usage: dict = field(default_factory=dict)

    def __post_init__(self):
        _week(self.window_start, "window_start")
        for name in ("accepted_ids", "mandatory_ids", "missing_mandatory"):
            object.__setattr__(self, name, _ids(getattr(self, name), name))
        if type(self.fallback) is not bool:
            raise ValueError("fallback must be a boolean")
        if not isinstance(self.reasons, (tuple, list)) or any(
                not isinstance(item, dict) for item in self.reasons):
            raise ValueError("reasons must be an array of objects")
        object.__setattr__(self, "reasons", tuple(_json_copy(self.reasons)))
        if not isinstance(self.plan_usage, dict):
            raise ValueError("plan_usage must be an object")
        object.__setattr__(self, "plan_usage", _json_copy(self.plan_usage))

    @classmethod
    def from_dict(cls, data):
        _shape(data, cls)
        return cls(**copy.deepcopy(data))


@dataclass(frozen=True)
class PolicySpec(_Record):
    kind: str = "mandatory-first"
    name: str = "mandatory-first"
    commands: tuple[WeeklyCommand, ...] = ()

    def __post_init__(self):
        _text(self.kind, "policy kind")
        if self.kind not in {"mandatory-first", "scripted"}:
            raise ValueError("unknown weekly policy kind")
        _text(self.name, "policy name")
        if not isinstance(self.commands, (tuple, list)) or any(
                not isinstance(item, WeeklyCommand) for item in self.commands):
            raise ValueError("commands must contain WeeklyCommand records")
        object.__setattr__(self, "commands", tuple(self.commands))
        windows = [item.window_start for item in self.commands]
        if len(windows) != len(set(windows)):
            raise ValueError("duplicate scripted command window")
        if self.kind != "scripted" and self.commands:
            raise ValueError("mandatory-first cannot carry scripted commands")

    @classmethod
    def from_dict(cls, data):
        _shape(data, cls)
        data = copy.deepcopy(data)
        if not isinstance(data["commands"], list):
            raise ValueError("commands must be an array")
        data["commands"] = tuple(WeeklyCommand.from_dict(item) for item in data["commands"])
        return cls(**data)


def _mission_map(missions: Iterable[dict]) -> dict[str, dict]:
    result = {}
    for record in missions:
        if not isinstance(record, dict):
            raise ValueError("missions must contain objects")
        names = ("id", "start_time", "duration_hours", "aircraft_required", "value")
        if not all(name in record for name in names):
            raise ValueError("mission record is missing its public specification")
        MissionSpec(**{name: record[name] for name in names})
        if record["id"] in result:
            raise ValueError("duplicate mission ID in planning input")
        result[record["id"]] = record
    return result


def plan_usage(missions: Iterable[dict], window_start: float, window_end: float,
               fleet_size: int) -> dict:
    """Exact interval sweep over [start,end), clipped to this planning window."""
    require_number("window_start", window_start)
    require_number("window_end", window_end, window_start, positive=True)
    require_integer("fleet_size", fleet_size, 1)
    records = _mission_map(missions)
    changes = {}
    exposures = []
    for mission in records.values():
        start = max(window_start, mission["start_time"])
        end = min(window_end, mission["start_time"] + mission["duration_hours"])
        if end <= start:
            continue
        required = mission["aircraft_required"]
        changes[start] = changes.get(start, 0) + required
        changes[end] = changes.get(end, 0) - required
        exposures.append(required * (end - start))
    current = peak = 0
    for time in sorted(changes):
        current += changes[time]
        peak = max(peak, current)
    return {"peak_aircraft": peak, "minimum_reserve": fleet_size - peak,
            "aircraft_hours": math.fsum(exposures)}


def _violations(usage: dict, context: RuleContext) -> list[dict]:
    rule = context.active_rule
    reasons = []
    if usage["minimum_reserve"] < rule.min_reserve:
        reasons.append({"code": "nominal_reserve", "required": rule.min_reserve,
                        "actual": usage["minimum_reserve"],
                        "shortfall": rule.min_reserve - usage["minimum_reserve"]})
    if rule.max_aircraft_hours is not None and usage["aircraft_hours"] > rule.max_aircraft_hours:
        reasons.append({"code": "planned_aircraft_hours", "limit": rule.max_aircraft_hours,
                        "actual": usage["aircraft_hours"],
                        "excess": usage["aircraft_hours"] - rule.max_aircraft_hours})
    return reasons


def _inputs(observation: PolicyObservation, context: RuleContext):
    if not isinstance(observation, PolicyObservation) or not isinstance(context, RuleContext):
        raise ValueError("policy requires PolicyObservation and RuleContext only")
    if observation.time != context.window_start or observation.fleet_size != context.fleet_size:
        raise ValueError("observation/context decision identity mismatch")
    rule = context.active_rule
    if rule.announced_at > observation.time or rule.effective_at > observation.time:
        raise ValueError("active rule is not yet announced/effective")
    visible = _mission_map(observation.missions)
    offered = {mid: m for mid, m in visible.items()
               if context.window_start <= m["start_time"] < context.window_end}
    carry = _mission_map(context.carry_missions)
    if offered.keys() & carry.keys():
        raise ValueError("new offers overlap existing commitments")
    if not set(rule.mandatory_ids) <= offered.keys():
        raise ValueError("mandatory IDs must refer to this window's public offers")
    mandatory = sorted(rule.mandatory_ids, key=lambda mid: (offered[mid]["start_time"], mid))
    return offered, carry, mandatory


def _usage(accepted_ids, offered, carry, context):
    return plan_usage([*carry.values(), *(offered[mid] for mid in accepted_ids)],
                      context.window_start, context.window_end, context.fleet_size)


def _fallback(offered, carry, mandatory, context, reasons):
    accepted = []
    for mid in mandatory:
        usage = _usage([*accepted, mid], offered, carry, context)
        if not _violations(usage, context):
            accepted.append(mid)
    missing = [mid for mid in mandatory if mid not in accepted]
    return WeeklyCommand(context.window_start, tuple(accepted), tuple(mandatory),
                         tuple(missing), True, tuple(reasons),
                         _usage(accepted, offered, carry, context))


def validate_command(command: WeeklyCommand, observation: PolicyObservation,
                     context: RuleContext) -> WeeklyCommand:
    """Reject illegal scripted choices and derive all diagnostics independently."""
    offered, carry, mandatory = _inputs(observation, context)
    if not isinstance(command, WeeklyCommand) or command.window_start != context.window_start:
        raise ValueError("command window does not match the decision")
    if not set(command.accepted_ids) <= offered.keys():
        raise ValueError("command contains unknown or unoffered mission IDs")
    mandatory_usage = _usage(mandatory, offered, carry, context)
    infeasible = _violations(mandatory_usage, context)
    if infeasible:
        expected = _fallback(offered, carry, mandatory, context, infeasible)
        if set(command.accepted_ids) != set(expected.accepted_ids):
            raise ValueError("infeasible mandatory set requires the deterministic fallback")
        return expected
    if not set(mandatory) <= set(command.accepted_ids):
        raise ValueError("command omits feasible mandatory obligations")
    accepted = sorted(command.accepted_ids, key=lambda mid: (offered[mid]["start_time"], mid))
    usage = _usage(accepted, offered, carry, context)
    if _violations(usage, context):
        raise ValueError("command violates nominal planning constraints")
    return WeeklyCommand(context.window_start, tuple(accepted), tuple(mandatory), (), False, (), usage)


def choose_command(observation: PolicyObservation, context: RuleContext,
                   policy: PolicySpec) -> WeeklyCommand:
    """Software baseline only; it neither predicts failures nor claims optimality."""
    if not isinstance(policy, PolicySpec):
        raise ValueError("policy must be a serializable PolicySpec")
    offered, carry, mandatory = _inputs(observation, context)
    scripted = None
    if policy.kind == "scripted":
        matches = [command for command in policy.commands if command.window_start == context.window_start]
        if not matches:
            raise ValueError("missing scripted command for this weekly decision")
        scripted = matches[0]
        # Fallback is a planning response, not a way to accept malformed input.
        if not set(scripted.accepted_ids) <= offered.keys():
            raise ValueError("command contains unknown or unoffered mission IDs")
    infeasible = _violations(_usage(mandatory, offered, carry, context), context)
    if infeasible:
        return _fallback(offered, carry, mandatory, context, infeasible)
    if policy.kind == "scripted":
        return validate_command(scripted, observation, context)
    accepted = list(mandatory)
    optional = sorted((mid for mid in offered if mid not in mandatory),
                      key=lambda mid: (-offered[mid]["value"], offered[mid]["start_time"], mid))
    for mid in optional:
        if not _violations(_usage([*accepted, mid], offered, carry, context), context):
            accepted.append(mid)
    return validate_command(WeeklyCommand(context.window_start, tuple(accepted)), observation, context)
