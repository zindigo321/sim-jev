"""Frozen candidate screening and equal-DES-budget short-window decisions."""
from dataclasses import dataclass
from copy import deepcopy
import math
import time

from .decision import (DecisionRequest, DecisionResult, DecisionPolicySpec, ModelParameters,
                       DECISION_PROTOCOL_VERSION, DECISION_POLICY_VERSION)
from .weekly_policy import PolicySpec, WeeklyCommand, choose_command, validate_command
from .provenance import canonical_sha256

BUDGET_VERSION = "min-k-m-times-b-round-robin-direct-v1"


@dataclass
class DecisionRuntime:
    """Optional external services. Never exported or pickled with a simulation."""
    jev_adapter: object = None
    branch_auditor: object = None


def _adapter(policy, runtime):
    from .jev import ChoiceAdapter, JevConfig, JevError
    adapter = getattr(runtime, "jev_adapter", None)
    if adapter is not None:
        if adapter.config.mode != policy.jev_mode:
            raise ValueError("runtime adapter mode differs from serialized policy")
        return adapter
    if policy.jev_mode == "stub":
        return ChoiceAdapter(JevConfig(mode="stub"))
    raise JevError("runtime_adapter_required", {
        "mode": policy.jev_mode, "valid_jev": False, "valid_response": False,
        "actual_http_requests": 0, "actual_usage": {"input_tokens": 0, "output_tokens": 0},
        "uncertain": False, "fallback_required": True,
        "error_code": "runtime_adapter_required"})


def resolve_decision(request, policy, runtime=None):
    if isinstance(policy, PolicySpec):
        return DecisionResult(choose_command(request.observation, request.context, policy))
    return resolve_candidates(request, policy, runtime)


def resolve_candidates(request, policy, runtime=None):
    from .candidates import generate_candidates
    pool = generate_candidates(request, policy.target_size, policy.planning_seed)
    return select_from_pool(request, policy, pool, runtime)


def select_from_pool(request, policy, pool, runtime=None):
    """Offline comparisons can explicitly freeze one pool for all selectors.

    Direct DES cycles the stable-ID-sorted pool, with scenario index increasing
    after each cycle. Screened methods share indices 0..B-1. Both use exactly
    min(K,actual_M)*B branches; empty and singleton pools use zero branches.
    """
    from .candidates import heuristic_ranking, _candidate, _offered
    from .planning import PlanningInput, PlanningScenario
    from .jev import JevError, build_choice_request
    start = time.perf_counter()
    if not isinstance(request, DecisionRequest) or not isinstance(policy, DecisionPolicySpec):
        raise ValueError("candidate selection requires versioned legal request and policy")
    # Own the request and pool so a caller cannot mutate records while selecting.
    request = DecisionRequest.from_dict(request.to_dict())
    pool = deepcopy(pool)
    if len({c.id for c in pool}) != len(pool):
        raise ValueError("duplicate candidate ID")
    for candidate in pool:
        if validate_command(candidate.command, request.observation, request.context) != candidate.command:
            raise ValueError("candidate command diagnostics differ from canonical command")
        if _candidate(request, candidate.command, _offered(request)).to_dict() != candidate.to_dict():
            raise ValueError("candidate identity/summary does not belong to this legal request")
    stable = sorted(pool, key=lambda item: item.id)
    ranked = heuristic_ranking(pool)
    jev, fallback = None, False
    if policy.method == "random-des":
        ranked = sorted(pool, key=lambda c: (canonical_sha256({"protocol": "random-screen-v1",
            "planning_seed": policy.planning_seed, "request_id": request.request_id, "candidate_id": c.id}), c.id))
    elif policy.method in {"jev-direct", "jev-des"}:
        payload = build_choice_request(request, pool)
        try:
            if len(pool) < 2:
                from .jev import ChoiceAdapter, JevConfig
                jev = ChoiceAdapter(JevConfig()).rank(payload)
                jev.update(mode=policy.jev_mode, stub_fixture_version=None,
                           transport_kind="deterministic_no_http")
            else:
                jev = _adapter(policy, runtime).rank(payload)
            by_id = {c.id: c for c in pool}
            ranked = [by_id[key] for key in jev["ranked_ids"]]
        except JevError as exc:
            if policy.strict_jev:
                raise
            fallback = True
            jev = {**exc.audit, "request": payload, "request_sha256": canonical_sha256(payload),
                   "error_code": exc.code, "fallback_policy": "fixed-heuristic-v1"}
    branches, scenarios = [], {}
    total = min(policy.top_k, len(pool)) * policy.scenarios_per_candidate if len(pool) > 1 else 0
    if policy.method == "jev-direct":
        total = 0
        chosen = (next(c for c in pool if c.id == jev["choice"])
                  if pool and jev and jev.get("valid_response") else (ranked[0] if ranked else None))
        allocation = []
    elif total:
        if policy.method == "direct-des":
            allocation = [(stable[i % len(stable)], i // len(stable)) for i in range(total)]
        else:
            allocation = [(candidate, index) for candidate in ranked[:policy.top_k]
                          for index in range(policy.scenarios_per_candidate)]
        totals = {}
        for candidate, index in allocation:
            if index not in scenarios:
                scenarios[index] = PlanningScenario(PlanningInput(request, policy.planning_seed, index))
            result = scenarios[index].evaluate(candidate.command)
            auditor = getattr(runtime, "branch_auditor", None)
            if auditor is not None:
                auditor(result, request)
            # The full branch artifact is available via PlanningScenario; the
            # recoverable decision ledger stores sufficient score/budget facts
            # and identity hashes without duplicating every physical log.
            row = {key: deepcopy(value) for key, value in result.items() if key != "final_state"}
            row["candidate_id"] = candidate.id
            row["physical_artifact_sha256"] = canonical_sha256(result["final_state"])
            branches.append(row)
            totals.setdefault(candidate.id, []).append(result["score"])
        scores = {key: math.fsum(values) / len(values) for key, values in totals.items()}
        winner = min(scores, key=lambda key: (-scores[key], key))
        chosen = next(candidate for candidate in pool if candidate.id == winner)
    else:
        allocation = []
        chosen = ranked[0] if ranked else None
    command = (chosen.command if chosen is not None else
               choose_command(request.observation, request.context, PolicySpec()))
    record = {
        "protocol_version": DECISION_PROTOCOL_VERSION, "policy_version": DECISION_POLICY_VERSION,
        "request": request.to_dict(), "method": policy.method,
        "pool": [candidate.to_dict() for candidate in stable],
        "pool_sha256": canonical_sha256([candidate.to_dict() for candidate in stable]),
        "ranking_ids": [candidate.id for candidate in ranked],
        "selected_candidate_id": chosen.id if chosen is not None else None,
        "command": command.to_dict(), "scenarios": [scenarios[i].to_dict() for i in sorted(scenarios)],
        "branches": branches, "fallback": fallback,
        "deterministic_reason": "empty_pool_reference" if not pool else "singleton_pool" if len(pool) == 1 else None,
        "budget": {"version": BUDGET_VERSION, "configured_M": policy.target_size,
                   "actual_M": len(pool), "configured_K": policy.top_k,
                   "B": policy.scenarios_per_candidate, "allocated_des": total,
                   "actual_des": len(branches),
                   "simulated_hours": math.fsum(row["duration_hours"] for row in branches)},
        "jev": jev,
        "execution_audit": {"end_to_end_seconds": time.perf_counter() - start},
    }
    if len(branches) != total:
        raise RuntimeError("DES budget mismatch")
    return DecisionResult(command, record)


def logical_decision_record(record):
    """Separate genuine runtime audit from model logic for fixed-response replay.

    Original records remain unchanged in checkpoints and exported audit files.
    Network timing, cache hits and actual calls are not claimed equal.
    """
    value = deepcopy(record)
    value.pop("execution_audit", None)
    if value.get("jev") is not None:
        jev = value["jev"]
        value["jev"] = {key: jev.get(key) for key in (
            "request_sha256", "model", "template_version", "template_sha256",
            "candidate_mapping_sha256", "mode", "request", "response", "choice", "ranked_ids",
            "probabilities", "confidence", "valid_response", "valid_jev", "api_skipped_reason",
            "fallback_required", "fallback_policy", "error_code", "stub_fixture_version")}
    return value


def validate_decision_records(sim):
    """Validate recorded choices without rerunning a past resolver or HTTP call."""
    from .candidates import _candidate, _offered
    state = sim.weekly_state
    records = state["decision_records"]
    if not isinstance(records, list) or len(records) != len(state["weekly_commands"]):
        raise ValueError("decision ledger count differs from committed commands")
    policy = DecisionPolicySpec.from_dict(state["policy_spec"])
    for record, committed in zip(records, state["weekly_commands"]):
        request = DecisionRequest.from_dict(record["request"])
        if (record["protocol_version"] != DECISION_PROTOCOL_VERSION or
                record["policy_version"] != DECISION_POLICY_VERSION or record["method"] != policy.method or
                record["command"] != committed or request.context.window_start != committed["window_start"]):
            raise ValueError("decision ledger identity/command mismatch")
        if request.parameters != ModelParameters.from_config(sim.config):
            raise ValueError("decision public model mismatch")
        rule = next(r for r in state["rules_input"] if r["window_start"] == request.context.window_start)
        if request.context.active_rule.to_dict() != rule:
            raise ValueError("decision request rule mismatch")
        pool = record["pool"]
        if canonical_sha256(pool) != record["pool_sha256"]:
            raise ValueError("decision pool identity mismatch")
        offered = _offered(request)
        for item in pool:
            candidate = _candidate(request, WeeklyCommand.from_dict(item["command"]), offered)
            if candidate.to_dict() != item:
                raise ValueError("decision candidate identity/summary mismatch")
        ids = [row["id"] for row in pool]
        if len(set(ids)) != len(ids) or sorted(record["ranking_ids"]) != sorted(ids):
            raise ValueError("decision ranking does not cover candidate pool")
        selected = record["selected_candidate_id"]
        if ids and (selected not in ids or next(row["command"] for row in pool if row["id"] == selected) != committed):
            raise ValueError("decision selection differs from committed candidate")
        branches, budget = record["branches"], record["budget"]
        allocated = (0 if len(pool) < 2 or policy.method == "jev-direct" else
                     min(policy.top_k, len(pool)) * policy.scenarios_per_candidate)
        if (budget["version"] != BUDGET_VERSION or budget["actual_M"] != len(pool) or
                budget["actual_des"] != len(branches) or budget["allocated_des"] != allocated or
                len(branches) != allocated or budget["simulated_hours"] !=
                math.fsum(row["duration_hours"] for row in branches)):
            raise ValueError("decision budget ledger mismatch")
        for row in branches:
            if row["candidate_id"] not in ids or row["duration_hours"] != request.context.window_end - request.context.window_start:
                raise ValueError("decision branch endpoint/identity mismatch")
        from .decision_audit import validate_record
        validate_record(record, policy, sim)
