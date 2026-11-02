"""One bounded, public-information candidate pool for every phase-2C selector.

No simulation runs, failure predictions, or learned labels enter generation.
Candidate IDs describe command content; the independent generation seed only
determines which additional feasible combinations enter the bounded pool.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools
import json
import math
from typing import TYPE_CHECKING

from .config import require_integer
from .provenance import canonical_sha256
from .weekly_policy import PolicySpec, WeeklyCommand, choose_command, validate_command

if TYPE_CHECKING:
    from .decision import DecisionRequest


CANDIDATE_PROTOCOL_VERSION = "public-feasible-pool-v1"
CANDIDATE_SUMMARY_VERSION = "public-portfolio-summary-v1"
HEURISTIC_VERSION = "planned-completions-dsr-rcr-afh-v1"
GENERATION_RNG_VERSION = "sha256-request-search-v1"
MISSION_FIELDS = ("id", "start_time", "duration_hours", "aircraft_required", "value")
UNITS = {"time": "hours", "duration": "hours", "aircraft": "aircraft",
         "aircraft_hours": "aircraft-hours", "value": "synthetic-value-points",
         "inventory": "single-LRU-units", "ratio": "dimensionless"}


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


@dataclass(frozen=True)
class Candidate:
    id: str
    command: WeeklyCommand
    summary: dict

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("candidate ID must be nonempty text")
        if not isinstance(self.command, WeeklyCommand) or not isinstance(self.summary, dict):
            raise ValueError("candidate requires a WeeklyCommand and public summary")
        object.__setattr__(self, "summary", _copy(self.summary))

    def to_dict(self) -> dict:
        return {"id": self.id, "command": self.command.to_dict(), "summary": _copy(self.summary)}


def _offered(request) -> dict[str, dict]:
    context = request.context
    return {mission["id"]: {name: mission[name] for name in MISSION_FIELDS}
            for mission in request.observation.missions
            if context.window_start <= mission["start_time"] < context.window_end}


def _candidate(request, command: WeeklyCommand, offered: dict) -> Candidate:
    command = validate_command(command, request.observation, request.context)
    context, observation = request.context, request.observation
    identity = {"protocol": CANDIDATE_PROTOCOL_VERSION,
                "window_start": context.window_start, "accepted_ids": sorted(command.accepted_ids)}
    candidate_id = "C-" + canonical_sha256(identity)[:24]
    missions = [offered[mid] for mid in command.accepted_ids]
    ending = [mission for mission in missions
              if mission["start_time"] + mission["duration_hours"] <= context.window_end]
    offered_value = math.fsum(mission["value"] for mission in offered.values())
    ending_value = math.fsum(mission["value"] for mission in ending)
    proxy_dsr = len(ending) / len(offered) if offered else 0.0
    proxy_rcr = ending_value / offered_value if offered_value else 0.0
    rule, usage = context.active_rule, command.plan_usage
    summary = {
        "schema_version": CANDIDATE_SUMMARY_VERSION,
        "generator_version": CANDIDATE_PROTOCOL_VERSION, "units": UNITS,
        "window_start": context.window_start, "window_end": context.window_end,
        "accepted_ids": list(command.accepted_ids), "missions": missions,
        "accepted_count": len(missions), "accepted_value": math.fsum(m["value"] for m in missions),
        "offered_count": len(offered), "offered_value": offered_value,
        "plan_usage": usage,
        "carry_missions": [{name: mission[name] for name in MISSION_FIELDS}
                           for mission in context.carry_missions],
        "mandatory_ids": list(command.mandatory_ids),
        "missing_mandatory": list(command.missing_mandatory),
        "fallback": command.fallback, "fallback_reasons": list(command.reasons),
        "active_rule": rule.to_dict(), "known_rules": observation.rules,
        "rule_satisfaction": {
            "mandatory_included": not command.missing_mandatory,
            "nominal_reserve": usage["minimum_reserve"] >= rule.min_reserve,
            "planned_aircraft_hours": (rule.max_aircraft_hours is None
                                       or usage["aircraft_hours"] <= rule.max_aircraft_hours),
        },
        "resources": {"fleet_size": observation.fleet_size, "aircraft": observation.aircraft,
                      "repairs": observation.repairs, "orders": observation.orders,
                      "inventory": observation.inventory},
        "heuristic": {"version": HEURISTIC_VERSION,
                      "interpretation": "nominal planned completion proxy; not a success probability or realized income",
                      "planned_completions_by_end": len(ending),
                      "planned_value_by_end": ending_value,
                      "DSR_proxy": proxy_dsr, "RCR_proxy": proxy_rcr,
                      "DSR_valid": bool(offered), "RCR_valid": bool(offered_value),
                      "score": proxy_dsr + proxy_rcr},
    }
    return Candidate(candidate_id, command, summary)


def heuristic_ranking(candidates: list[Candidate]) -> list[Candidate]:
    """Frozen deterministic plan proxy, then lower planned AFH, then stable ID.

    A mission crossing the endpoint contributes zero planned completion/value.
    Actual failures, dispatch shortages and terminal value are not forecast here.
    """
    if any(not isinstance(candidate, Candidate) for candidate in candidates):
        raise ValueError("ranking requires Candidate records")
    if len({candidate.id for candidate in candidates}) != len(candidates):
        raise ValueError("candidate IDs must be unique")
    return sorted(candidates, key=lambda candidate: (
        -candidate.summary["heuristic"]["score"],
        candidate.command.plan_usage["aircraft_hours"], candidate.id))


def generate_candidates(request: DecisionRequest, target_size: int = 16,
                        planning_seed: int = 20261102) -> list[Candidate]:
    """Generate at most target_size distinct feasible commands, with finite work.

    Reference first; value/AFH/duration/order greedy plans; local removals,
    additions and exchanges; independent hashed random subset/greedy plans.
    Empty offers produce the one legal empty command. Explicit target_size=0
    produces an empty disabled pool. An infeasible mandatory/carry context has
    exactly the same deterministic fallback for every selector.
    """
    from .decision import DecisionRequest
    if not isinstance(request, DecisionRequest):
        raise ValueError("candidate generation requires a legal DecisionRequest")
    # Recheck the content identity and whitelist at the boundary: dataclass
    # freezing does not make nested public collections immutable.
    request = DecisionRequest.from_dict(request.to_dict())
    require_integer("target_size", target_size)
    require_integer("planning_seed", planning_seed)
    if target_size > 256:
        raise ValueError("target_size exceeds the bounded candidate protocol maximum 256")
    if not target_size:
        return []
    offered = _offered(request)
    reference = choose_command(request.observation, request.context, PolicySpec())
    reference_candidate = _candidate(request, reference, offered)
    pool = {reference_candidate.id: reference_candidate}
    if target_size == 1 or reference.fallback or not offered:
        return list(pool.values())
    mandatory = set(reference.mandatory_ids)
    optional = sorted(set(offered) - mandatory)
    if not optional:
        return list(pool.values())
    # The bound includes deterministic feasibility checks; invalid attempts
    # consume budget too. There is no retry-until-full loop.
    maximum_checks = min(8192, max(256, 128 * target_size))
    checks = 0

    def command_for(ids):
        nonlocal checks
        if checks >= maximum_checks:
            return None
        checks += 1
        try:
            return validate_command(WeeklyCommand(request.context.window_start, tuple(sorted(ids))),
                                    request.observation, request.context)
        except ValueError:
            return None

    def add(ids, limit=target_size):
        if len(pool) >= limit:
            return
        command = command_for(ids)
        if command is not None:
            candidate = _candidate(request, command, offered)
            pool.setdefault(candidate.id, candidate)

    def greedy(order, include=None):
        accepted = set(mandatory)
        for mid in order:
            if include is not None and mid not in include:
                continue
            command = command_for(accepted | {mid})
            if command is not None:
                accepted.add(mid)
            if checks >= maximum_checks:
                break
        return accepted

    def afh(mid):
        mission = offered[mid]
        return mission["duration_hours"] * mission["aircraft_required"]

    add(mandatory)
    orders = [
        sorted(optional, key=lambda mid: (-offered[mid]["value"], offered[mid]["start_time"], mid)),
        sorted(optional, key=lambda mid: (-offered[mid]["value"] / afh(mid), afh(mid), mid)),
        sorted(optional, key=lambda mid: (afh(mid), -offered[mid]["value"], mid)),
        sorted(optional, key=lambda mid: (offered[mid]["start_time"] + offered[mid]["duration_hours"], mid)),
    ]
    for order in orders:
        add(greedy(order))
    local_limit = min(target_size, max(len(pool), (2 * target_size + 2) // 3))
    seeds = [set(candidate.command.accepted_ids) for candidate in list(pool.values())]
    for accepted in seeds:
        if len(pool) >= local_limit or checks >= maximum_checks:
            break
        removable, absent = sorted(accepted - mandatory), sorted(set(optional) - accepted)
        for mid in removable:
            add(accepted - {mid}, local_limit)
        for mid in absent:
            add(accepted | {mid}, local_limit)
        for old, new in itertools.product(removable, absent):
            if len(pool) >= local_limit or checks >= maximum_checks:
                break
            add((accepted - {old}) | {new}, local_limit)

    if len(optional) <= 4:
        for count in range(len(optional) + 1):
            for subset in itertools.combinations(optional, count):
                add(mandatory | set(subset))
        return list(pool.values())

    # No candidate ID, evaluation order, algorithm name, or execution seed is
    # present in these generation-only keys. These are not DES noise keys.
    def draw(attempt, mid, purpose):
        payload = {"protocol": GENERATION_RNG_VERSION, "planning_seed": planning_seed,
                   "request_id": request.request_id, "attempt": attempt,
                   "mission_id": mid, "purpose": purpose}
        return int.from_bytes(hashlib.sha256(json.dumps(payload, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")).digest(), "big")

    for attempt in range(64 * target_size):
        if len(pool) >= target_size or checks >= maximum_checks:
            break
        order = sorted(optional, key=lambda mid: (draw(attempt, mid, "order"), mid))
        included = {mid for mid in optional if draw(attempt, mid, "include") & 1}
        add(greedy(order, included))
    # Exhaustion is explicit: no duplicate padding, no hidden DES, and no claim
    # that keeping the reference in the pool makes the final selector superior.
    return list(pool.values())
