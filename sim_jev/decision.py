"""Versioned legal decision boundary, separate from execution identity and truth."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from copy import deepcopy

from .config import Config, require_integer
from .provenance import canonical_sha256
from .versions import DAILY_POLICY_VERSION, WEEKLY_POLICY_VERSION
from .weekly_policy import PolicyObservation, PolicySpec, RuleContext, WeeklyCommand

DECISION_PROTOCOL_VERSION = "legal-weekly-request-v1"
DECISION_PROTOCOL_VERSION_V2 = "legal-weekly-request-v2"
DECISION_POLICY_VERSION = "candidate-choice-des-v1"


@dataclass(frozen=True)
class ModelParameters:
    fleet_size: int
    repair_bays: int
    failure_rate_per_flight_hour: float
    repair_duration_min_hours: float
    repair_duration_max_hours: float
    lead_time_min_hours: float
    lead_time_max_hours: float
    reorder_target: int
    reorder_interval_hours: float = 24.
    horizon_hours: float = 720.
    daily_executor_version: str = DAILY_POLICY_VERSION

    def __post_init__(self):
        # Reuse public parameter domain validation, never retain a whole Config.
        values = self.to_dict()
        values.pop("daily_executor_version")
        Config(**values)
        if (self.horizon_hours != 720 or self.reorder_interval_hours != 24
                or self.daily_executor_version != DAILY_POLICY_VERSION):
            raise ValueError("unsupported public execution model")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_config(cls, config):
        # Explicit whitelist: execution seed, task generator, overrides and notes
        # cannot enter the decision object by adding a new Config field.
        return cls(fleet_size=config.fleet_size, repair_bays=config.repair_bays,
                   failure_rate_per_flight_hour=config.failure_rate_per_flight_hour,
                   repair_duration_min_hours=config.repair_duration_min_hours,
                   repair_duration_max_hours=config.repair_duration_max_hours,
                   lead_time_min_hours=config.lead_time_min_hours,
                   lead_time_max_hours=config.lead_time_max_hours,
                   reorder_target=config.reorder_target,
                   reorder_interval_hours=config.reorder_interval_hours,
                   horizon_hours=config.horizon_hours)

    @classmethod
    def from_dict(cls, data):
        _shape(data, {f.name for f in fields(cls)}, "public model parameters")
        return cls(**data)


def _shape(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{label}: unexpected fields")


_MISSION = {"id", "start_time", "duration_hours", "aircraft_required", "value"}
_OBS_FIELDS = {
    "aircraft": {"id", "state", "mission_id", "flight_hours"},
    "missions": _MISSION | {"status", "assigned_aircraft", "actual_start", "actual_end"},
    "repairs": {"id", "aircraft_id", "status", "requested_at", "started_at", "completed_at",
                "spare_wait_hours", "bay_wait_hours"},
    "orders": {"id", "quantity", "ordered_at", "expected_arrival", "status", "arrived_at"},
    "commitments": _MISSION | {"status", "accepted", "window_start"},
    "rules": {"window_start", "window_end", "announced_at", "effective_at", "version",
              "mandatory_ids", "min_reserve", "max_aircraft_hours"},
}


@dataclass(frozen=True)
class DecisionRequest:
    observation: PolicyObservation
    context: RuleContext
    parameters: ModelParameters
    request_id: str
    protocol_version: str = DECISION_PROTOCOL_VERSION

    def __post_init__(self):
        if self.protocol_version not in {DECISION_PROTOCOL_VERSION, DECISION_PROTOCOL_VERSION_V2}:
            raise ValueError("unsupported decision request version")
        if not isinstance(self.observation, PolicyObservation) or not isinstance(self.context, RuleContext):
            raise ValueError("decision requires public observation and context")
        if not isinstance(self.parameters, ModelParameters):
            raise ValueError("decision requires whitelisted ModelParameters")
        obs = self.observation.to_dict()
        for name, keys in _OBS_FIELDS.items():
            for record in obs[name]:
                _shape(record, keys, "public " + name)
        _shape(obs["inventory"], {"on_hand", "in_transit", "unmet_demand"}, "public inventory")
        for record in self.context.carry_missions:
            _shape(record, _MISSION | {"status"}, "public carry")
        if (self.observation.time != self.context.window_start
                or self.observation.fleet_size != self.parameters.fleet_size
                or self.context.fleet_size != self.parameters.fleet_size):
            raise ValueError("decision identity disagrees with public context")
        if any(r["announced_at"] > self.observation.time for r in obs["rules"]):
            raise ValueError("unannounced rule in decision request")
        if any(m["start_time"] >= self.context.window_end for m in obs["missions"]):
            raise ValueError("unannounced future task in decision request")
        if self.request_id != canonical_sha256(self._content()):
            raise ValueError("legal request identity mismatch")
        # Dataclasses are frozen; nested collections must also be isolated from
        # the caller so resolver mutations never reach a simulation.
        object.__setattr__(self, "observation", PolicyObservation.from_dict(obs))
        object.__setattr__(self, "context", RuleContext.from_dict(self.context.to_dict()))

    def _content(self):
        return {"protocol_version": self.protocol_version,
                "observation": self.observation.to_dict(), "context": self.context.to_dict(),
                "parameters": self.parameters.to_dict()}

    def to_dict(self):
        return {**self._content(), "request_id": self.request_id}

    @classmethod
    def create(cls, observation, context, parameters, *, protocol_version=DECISION_PROTOCOL_VERSION):
        content = {"protocol_version": protocol_version,
                   "observation": observation.to_dict(), "context": context.to_dict(),
                   "parameters": parameters.to_dict()}
        return cls(observation, context, parameters, canonical_sha256(content), protocol_version)

    @classmethod
    def from_dict(cls, data):
        _shape(data, {f.name for f in fields(cls)}, "decision request")
        return cls(PolicyObservation.from_dict(data["observation"]),
                   RuleContext.from_dict(data["context"]), ModelParameters.from_dict(data["parameters"]),
                   data["request_id"], data["protocol_version"])


@dataclass(frozen=True)
class DecisionPolicySpec:
    method: str = "heuristic-des"
    planning_seed: int = 20261102
    target_size: int = 16
    top_k: int = 4
    scenarios_per_candidate: int = 8
    jev_mode: str = "stub"
    strict_jev: bool = False
    kind: str = "candidate-des"
    name: str = "phase2c"
    version: str = DECISION_POLICY_VERSION

    def __post_init__(self):
        if self.kind != "candidate-des" or self.version != DECISION_POLICY_VERSION:
            raise ValueError("unsupported decision policy version")
        if self.method not in {"random-des", "heuristic-des", "jev-direct", "jev-des", "direct-des"}:
            raise ValueError("unknown decision method")
        if self.jev_mode not in {"stub", "real_replay", "live"}:
            raise ValueError("unknown Jev mode")
        for name in ("planning_seed", "target_size", "top_k", "scenarios_per_candidate"):
            require_integer(name, getattr(self, name), 0 if name == "planning_seed" else 1)
        if self.target_size > 16 or self.top_k > self.target_size:
            raise ValueError("2C frozen candidate budget requires 1 <= K <= M <= 16")
        if type(self.strict_jev) is not bool or not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("invalid decision policy descriptor")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        _shape(data, {f.name for f in fields(cls)}, "decision policy")
        return cls(**deepcopy(data))


def policy_from_dict(data):
    if data.get("kind") == "local-candidate-des":
        from .local_policy import LocalDecisionPolicySpec
        return LocalDecisionPolicySpec.from_dict(data)
    return (DecisionPolicySpec.from_dict(data) if data.get("kind") == "candidate-des"
            else PolicySpec.from_dict(data))


def policy_version(data):
    if data.get("kind") == "local-candidate-des":
        from .local_policy import LocalDecisionPolicySpec
        return LocalDecisionPolicySpec.from_dict(data).version
    return DECISION_POLICY_VERSION if data.get("kind") == "candidate-des" else WEEKLY_POLICY_VERSION


@dataclass(frozen=True)
class DecisionResult:
    command: WeeklyCommand
    record: dict | None = None
