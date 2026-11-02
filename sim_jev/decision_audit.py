"""Restore-time checks of recorded decisions, without rerunning DES or HTTP.

Historical public facts are reconstructed from the business-log prefix ending
at the offer publication barrier. No policy is asked to select a new command.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
import json
import math

from .candidates import Candidate
from .decision import DecisionRequest
from .jev import (MODEL, TEMPLATE_VERSION, INSTRUCTIONS, build_choice_request,
                  validate_response)
from .planning import (PLANNING_INPUT_VERSION, PLANNING_MODEL_VERSION,
                       CONDITIONAL_DISTRIBUTION_VERSION, BRANCH_SCORE_VERSION)
from .provenance import canonical_sha256
from .weekly_policy import WeeklyCommand


METRIC_INTEGRALS = frozenset({"available_aircraft_hours", "idle_aircraft_hours", "flight_hours",
                              "spare_wait_hours", "bay_wait_hours"})
METRIC_COUNTS = frozenset({"failures", "repairs_started", "repairs_completed",
                         "ordered_quantity", "arrived_quantity"})


def validate_metrics(metrics, *, capacity_hours=None, path="metrics"):
    """Check the frozen Metrics schema without treating repeated failures as unique aircraft.

    Capacity bounds apply to interval deltas, not to cumulative public carry-in
    flight hours. Small roundoff from subtracting carry-in integrals is allowed
    only in upper-bound/equality comparisons; negative values are never accepted.
    """
    _check(type(metrics) is dict and metrics.keys() == METRIC_INTEGRALS | METRIC_COUNTS,
           path + " fields differ")
    for key in METRIC_INTEGRALS:
        value = metrics[key]
        _check(type(value) in (int, float) and math.isfinite(value) and value >= 0,
               path + "." + key + " must be finite and nonnegative")
    for key in METRIC_COUNTS:
        _check(type(metrics[key]) is int and metrics[key] >= 0,
               path + "." + key + " must be a nonnegative integer")
    if capacity_hours is not None:
        def within(value, upper):
            return value <= upper or math.isclose(value, upper, rel_tol=1e-12, abs_tol=1e-10)

        for key in METRIC_INTEGRALS:
            _check(within(metrics[key], capacity_hours), path + "." + key + " exceeds window capacity")
        _check(math.isclose(metrics["available_aircraft_hours"],
                            metrics["idle_aircraft_hours"] + metrics["flight_hours"],
                            rel_tol=1e-12, abs_tol=1e-10), path + " available exposure differs")
        occupied = math.fsum(metrics[key] for key in
                             ("available_aircraft_hours", "spare_wait_hours", "bay_wait_hours"))
        _check(within(occupied, capacity_hours), path + " combined exposure exceeds window capacity")


def _public_metric_offsets(request):
    observation = request.observation
    return {"available_aircraft_hours": 0., "idle_aircraft_hours": 0.,
            "flight_hours": math.fsum(row["flight_hours"] for row in observation.aircraft),
            "spare_wait_hours": 0., "bay_wait_hours": 0.,
            "failures": len(observation.repairs),
            "repairs_started": sum(row["started_at"] is not None for row in observation.repairs),
            "repairs_completed": sum(row["status"] == "completed" for row in observation.repairs),
            "ordered_quantity": sum(row["quantity"] for row in observation.orders),
            "arrived_quantity": sum(row["quantity"] for row in observation.orders if row["status"] == "arrived")}


def _check(condition, message):
    if not condition:
        raise ValueError("decision audit: " + message)


def _same_public(actual, expected, path="public"):
    if type(expected) is dict:
        _check(type(actual) is dict and actual.keys() == expected.keys(), path + " fields differ")
        for key in expected:
            _same_public(actual[key], expected[key], path + "." + key)
    elif type(expected) is list:
        _check(type(actual) is list and len(actual) == len(expected), path + " array differs")
        for index, (left, right) in enumerate(zip(actual, expected)):
            _same_public(left, right, f"{path}[{index}]")
    elif path.rsplit(".", 1)[-1] in {"flight_hours", "spare_wait_hours", "bay_wait_hours"}:
        _check(type(actual) in (int, float) and math.isfinite(actual) and
               math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-10), path + " integral differs")
    elif type(expected) in (int, float):
        _check(type(actual) in (int, float) and actual == expected, path + " differs")
    else:
        _check(type(actual) is type(expected) and actual == expected, path + " differs")


def historical_observation(sim, point):
    """Rebuild only recorded public facts before the point's weekly command."""
    aircraft = {f"A{i + 1:03d}": {"id": f"A{i + 1:03d}", "state": "idle",
                 "mission_id": None, "flight_hours": 0.} for i in range(sim.config.fleet_size)}
    missions, repairs, orders, commitments, announced = {}, {}, {}, {}, {}
    on_hand, previous, found = sim.config.initial_spares, 0., False
    for event in sim.events:
        timestamp = event["time"]
        _check(previous <= timestamp <= sim.now, "business-log time invalid")
        dt = timestamp - previous
        waiting = sorted((row for row in repairs.values() if row["status"] == "waiting"),
                         key=lambda row: (row["requested_at"], row["id"]))
        for index, row in enumerate(waiting):
            row["bay_wait_hours" if index < min(on_hand, len(waiting)) else "spare_wait_hours"] += dt
        for row in aircraft.values():
            if row["state"] == "flying":
                row["flight_hours"] += dt
        previous = timestamp
        kind, entity, details = event["event_type"], event["entity_id"], json.loads(event["details"])
        if kind == "rule_announced":
            announced[entity] = deepcopy(details["rule"])
        elif kind == "weekly_offer":
            for mid in details["offered_ids"]:
                missions[mid] = {**asdict(sim.missions[mid].spec), "status": "pending",
                                 "assigned_aircraft": [], "actual_start": None, "actual_end": None}
            if timestamp == point:
                found = True
                break
        elif kind == "weekly_command":
            accepted = details["command"]["accepted_ids"]
            for mid, row in missions.items():
                if timestamp <= row["start_time"] < min(timestamp + 168, 720):
                    commitments[mid] = {"accepted": mid in accepted, "window_start": timestamp}
                    if mid not in accepted:
                        row["status"] = "rejected"
        elif kind == "mission_start":
            missions[entity].update(status="running", assigned_aircraft=deepcopy(details["aircraft"]),
                                    actual_start=timestamp)
        elif kind in {"mission_success", "mission_failed", "mission_not_executed"}:
            missions[entity].update(status=event["new_state"], actual_end=timestamp)
        elif kind == "aircraft_dispatch":
            aircraft[entity].update(state="flying", mission_id=details["mission"])
        elif kind == "aircraft_release":
            aircraft[entity].update(state=event["new_state"], mission_id=None)
        elif kind == "failure":
            rid = details["repair_id"]
            repairs[rid] = {"id": rid, "aircraft_id": entity, "status": "waiting",
                            "requested_at": timestamp, "started_at": None, "completed_at": None,
                            "spare_wait_hours": 0., "bay_wait_hours": 0.}
        elif kind == "repair_start":
            repairs[entity].update(status="repairing", started_at=timestamp)
            aircraft[details["aircraft"]]["state"] = "repairing"
            on_hand -= 1
        elif kind == "repair_complete":
            repairs[entity].update(status="completed", completed_at=timestamp)
            aircraft[details["aircraft"]]["state"] = "idle"
        elif kind == "order_placed":
            orders[entity] = {"id": entity, "quantity": details["quantity"], "ordered_at": timestamp,
                              "expected_arrival": details["expected_arrival"],
                              "status": "in_transit", "arrived_at": None}
        elif kind == "order_arrival":
            orders[entity].update(status="arrived", arrived_at=timestamp)
            on_hand += details["quantity"]
    _check(found, "request has no offer-publication log barrier")
    specs = {mid: asdict(sim.missions[mid].spec) for mid in missions}
    observation = {"time": point, "fleet_size": sim.config.fleet_size,
                   "aircraft": list(aircraft.values()), "missions": list(missions.values()),
                   "repairs": list(repairs.values()), "orders": list(orders.values()),
                   "inventory": {"on_hand": on_hand,
                                 "in_transit": sum(o["quantity"] for o in orders.values() if o["status"] == "in_transit"),
                                 "unmet_demand": sum(r["status"] == "waiting" for r in repairs.values())},
                   "rules": [r for r in sim.weekly_state["rules_input"] if r["version"] in announced],
                   "commitments": [{**specs[mid], "status": missions[mid]["status"], **row}
                                   for mid, row in commitments.items()]}
    carry = [{**specs[mid], "status": missions[mid]["status"]} for mid, row in commitments.items()
             if row["accepted"] and missions[mid]["status"] in {"pending", "running"}
             and specs[mid]["start_time"] < point]
    return observation, carry


def _heuristic_ids(pool):
    return [c.id for c in sorted(pool, key=lambda c: (-c.summary["heuristic"]["score"],
                                                     c.command.plan_usage["aircraft_hours"], c.id))]


def _validate_record(record, policy, sim):
    request = DecisionRequest.from_dict(record["request"])
    point, end = request.context.window_start, request.context.window_end
    historical, carry = historical_observation(sim, point)
    _same_public(request.observation.to_dict(), historical)
    _same_public(list(request.context.carry_missions), carry, "public.carry")
    pool = [Candidate(row["id"], WeeklyCommand.from_dict(row["command"]), row["summary"])
            for row in record["pool"]]
    by_id = {c.id: c for c in pool}
    ids = sorted(by_id)
    _check(len(ids) == len(pool), "duplicate candidate")
    jev = record["jev"]
    _check(type(record["fallback"]) is bool, "fallback must be boolean")
    if policy.method in {"jev-direct", "jev-des"}:
        _check(type(jev) is dict, "Jev method has no adapter record")
        _check(all(type(jev.get(key)) is bool for key in
                   ("valid_response", "valid_jev", "fallback_required", "uncertain")) and
               type(jev.get("actual_http_requests")) is int and jev["actual_http_requests"] >= 0,
               "Jev audit flag/call-count types differ")
        expected = build_choice_request(request, pool)
        _check(jev["request"] == expected and jev["request_sha256"] == canonical_sha256(expected),
               "Jev full request/hash differs")
        _check(jev["mode"] == policy.jev_mode, "Jev mode differs from serialized policy")
        expected_identity = {"model": MODEL, "template_version": TEMPLATE_VERSION,
                             "template_sha256": canonical_sha256(INSTRUCTIONS),
                             "candidate_mapping_sha256": canonical_sha256(expected["questions"]["portfolio"]["criteria"])}
        if jev.get("valid_response"):
            _check(all(jev.get(key) == value for key, value in expected_identity.items()), "Jev protocol identity differs")
            _check(record["fallback"] is False and jev.get("fallback_required") is False,
                   "successful response marked as fallback")
            if len(pool) < 2:
                _check(jev["api_skipped_reason"] == ("singleton_pool" if ids else "empty_pool") and
                       jev["ranked_ids"] == ids and jev["choice"] == (ids[0] if ids else None) and
                       jev["probabilities"] == {key: 1. for key in ids} and
                       jev["actual_http_requests"] == 0 and jev["valid_jev"] is False,
                       "deterministic skip record differs")
            else:
                response = validate_response(jev["response"], ids)
                answer = response["answers"]["portfolio"]
                for key in ("choice", "probabilities", "confidence"):
                    _check(jev[key] == answer[key], "Jev response/ranking fields differ")
                ranking = sorted(ids, key=lambda key: (-answer["probabilities"][key], key))
                _check(jev["ranked_ids"] == ranking and jev["api_skipped_reason"] is None,
                       "Jev probability ranking differs")
                expected_valid = policy.jev_mode != "stub" and jev.get("transport_kind") != "injected_test_transport"
                _check(jev["valid_jev"] is expected_valid, "Jev validity/source-mode disagreement")
            _check(record["ranking_ids"] == jev["ranked_ids"], "selector ranking differs from Jev ranking")
        else:
            _check(record["fallback"] is True and jev.get("valid_jev") is False and
                   jev.get("fallback_required") is True and bool(jev.get("error_code")) and
                   jev.get("fallback_policy") == "fixed-heuristic-v1", "invalid Jev fallback declaration")
            _check(record["ranking_ids"] == _heuristic_ids(pool), "fallback ranking differs from frozen heuristic")
    else:
        _check(jev is None and record["fallback"] is False, "non-Jev method has Jev/fallback record")
        if policy.method == "random-des":
            expected_ranking = sorted(ids, key=lambda key: (canonical_sha256({"protocol": "random-screen-v1",
                "planning_seed": policy.planning_seed, "request_id": request.request_id, "candidate_id": key}), key))
        else:
            expected_ranking = _heuristic_ids(pool)
        _check(record["ranking_ids"] == expected_ranking, "frozen screening order differs")

    total = 0 if len(pool) < 2 or policy.method == "jev-direct" else min(policy.top_k, len(pool)) * policy.scenarios_per_candidate
    if policy.method == "direct-des":
        allocation = [(ids[i % len(ids)], i // len(ids)) for i in range(total)]
    else:
        allocation = [(key, index) for key in record["ranking_ids"][:policy.top_k]
                      for index in range(policy.scenarios_per_candidate)] if total else []
    branches = record["branches"]
    _check([(row["candidate_id"], row["scenario_index"]) for row in branches] == allocation,
           "branch candidate/scenario allocation differs from frozen budget")
    budget = record["budget"]
    _check(all(type(budget[key]) is int for key in ("configured_M", "configured_K", "B")),
           "budget settings must be integers")
    _check((budget["configured_M"], budget["configured_K"], budget["B"]) ==
           (policy.target_size, policy.top_k, policy.scenarios_per_candidate), "serialized budget settings differ")
    expected_indices = sorted({index for _, index in allocation})
    scenarios = record["scenarios"]
    _check([row["scenario_index"] for row in scenarios] == expected_indices, "scenario pool differs from branch allocation")
    legal_hash = canonical_sha256(request.to_dict())
    scenario_ids = {}
    for scenario in scenarios:
        index = scenario["scenario_index"]
        _check(type(index) is int and index >= 0, "scenario index must be an integer")
        sid = "planning-" + canonical_sha256({"legal_input_hash": legal_hash,
                                              "planning_seed": policy.planning_seed, "scenario_index": index})
        scenario_ids[index] = sid
        _check(scenario["input_version"] == PLANNING_INPUT_VERSION and
               scenario["model_version"] == PLANNING_MODEL_VERSION and
               scenario["conditional_distribution_version"] == CONDITIONAL_DISTRIBUTION_VERSION and
               scenario["legal_input_hash"] == legal_hash and scenario["scenario_id"] == sid and
               scenario["planning_seed"] == policy.planning_seed, "scenario identity/protocol differs")
        initial = scenario["initial_state"]
        _check(initial["domain"] == "planning" and initial["model_version"] == PLANNING_MODEL_VERSION and
               initial["time"] == point and initial["events"] == [], "scenario origin/history differs")
        origin = initial["origin"]
        validate_metrics(initial["metrics"], path="scenario.initial.metrics")
        validate_metrics(origin["metric_offsets"], path="scenario.origin.metric_offsets")
        offsets = _public_metric_offsets(request)
        _same_public(initial["metrics"], offsets, "scenario.initial.metrics")
        _same_public(origin["metric_offsets"], offsets, "scenario.origin.metric_offsets")
        _check(origin["scenario_id"] == sid and origin["scenario_index"] == index and
               origin["planning_seed"] == policy.planning_seed and origin["legal_input_hash"] == legal_hash and
               origin["request_id"] == request.request_id and origin["as_of"] == point and
               origin["window_end"] == end and origin["public_state"] == request.observation.to_dict() and
               origin["rule_context"] == request.context.to_dict() and
               origin["public_parameters"] == request.parameters.to_dict(), "scenario legal origin differs")
        from .keyed_rng import KeyedRNG
        rng = KeyedRNG.from_dict(initial["random_manifest"])
        _check(rng.domain == "planning" and rng.scenario_id == sid and rng.seed == policy.planning_seed,
               "scenario random protocol identity differs")

    offered = {row["id"]: row for row in request.observation.missions if point <= row["start_time"] < end}
    scores = defaultdict(list)
    for row in branches:
        candidate = by_id[row["candidate_id"]]
        _check(row["scenario_id"] == scenario_ids[row["scenario_index"]] and row["legal_input_hash"] == legal_hash,
               "branch scenario/legal-input identity differs")
        _check(row["origin"] == point and row["window_end"] == end and row["duration_hours"] == end - point,
               "branch endpoint differs")
        validate_metrics(row["metrics_delta"], capacity_hours=(end - point) * request.parameters.fleet_size,
                         path="branch.metrics_delta")
        _check(row["model_version"] == PLANNING_MODEL_VERSION and row["score_version"] == BRANCH_SCORE_VERSION and
               row["conditional_distribution_version"] == CONDITIONAL_DISTRIBUTION_VERSION and
               row["command"] == candidate.command.to_dict(), "branch command/protocol differs")
        digest = row["physical_artifact_sha256"]
        _check(type(digest) is str and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
               "physical artifact digest invalid")
        counts, accepted = row["counts"], candidate.command.accepted_ids
        for key in ("offered", "accepted", "rejected", "success", "failed", "accepted_not_executed",
                    "pending_execution", "running", "unfinished", "launched", "mandatory_shortfall_as_of"):
            _check(type(counts[key]) is int and counts[key] >= 0, "branch count type/domain differs")
        _check(all(type(counts[key]) in (int, float) and math.isfinite(counts[key]) and counts[key] >= 0
                   for key in ("offered_value", "accepted_value", "earned_value")),
               "branch value type/domain differs")
        _check(counts["offered"] == len(offered) and counts["accepted"] == len(accepted) and
               counts["rejected"] == len(offered) - len(accepted) and
               counts["offered_value"] == sum(m["value"] for m in offered.values()) and
               counts["accepted_value"] == sum(offered[mid]["value"] for mid in accepted),
               "branch frozen offer/acceptance denominators differ")
        _check(counts["success"] + counts["failed"] + counts["accepted_not_executed"] + counts["unfinished"] == len(accepted)
               and counts["pending_execution"] + counts["running"] == counts["unfinished"] and
               counts["launched"] == counts["success"] + counts["failed"] + counts["running"] and
               row["unresolved"] == counts["unfinished"], "branch outcome conservation differs")
        earned = counts["earned_value"]
        _check(type(earned) in (int, float) and math.isfinite(earned) and 0 <= earned <= counts["accepted_value"],
               "branch earned value outside accepted value")
        mandatory = len(candidate.command.mandatory_ids)
        _check(max(0, mandatory - counts["success"]) <= counts["mandatory_shortfall_as_of"] <= mandatory and
               counts["mandatory_shortfall_terminal"] == (counts["mandatory_shortfall_as_of"] if end == 720 else None)
               and (type(counts["mandatory_shortfall_terminal"]) is int if end == 720 else
                    counts["mandatory_shortfall_terminal"] is None),
               "branch mandatory shortfall differs")
        for name, numerator, denominator in (("DSR", "success", "offered"), ("RCR", "earned_value", "offered_value"),
                                              ("ASR", "success", "accepted"), ("acceptance_rate", "accepted", "offered")):
            valid = counts[denominator] != 0
            ratio = counts[numerator] / counts[denominator] if valid else None
            _check(counts["validity"][name] is valid and type(counts[name]) is type(ratio) and counts[name] == ratio,
                   "branch ratio/validity differs")
            if name in {"DSR", "RCR"}:
                _check(row["validity"][name] is valid and type(row["score_components"][name]) in (int, float)
                       and row["score_components"][name] == (ratio if valid else 0.),
                       "branch score component differs")
        expected_score = sum(row["score_components"][key] for key in ("DSR", "RCR"))
        _check(type(row["score"]) in (int, float) and row["score"] == expected_score, "branch score differs")
        scores[candidate.id].append(row["score"])
    if scores:
        means = {key: math.fsum(values) / len(values) for key, values in scores.items()}
        winner = min(means, key=lambda key: (-means[key], key))
    elif policy.method == "jev-direct" and pool and jev and jev.get("valid_response"):
        winner = jev["choice"]
    else:
        winner = record["ranking_ids"][0] if pool else None
    _check(record["selected_candidate_id"] == winner, "selected candidate differs from recorded decision rule")


def validate_record(record, policy, sim):
    try:
        _validate_record(record, policy, sim)
    except (KeyError, TypeError, IndexError, OverflowError) as exc:
        raise ValueError("decision audit: malformed recorded decision") from exc
