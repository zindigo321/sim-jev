"""Independent origin-aware accounting of a conditional planning branch.

No simulation, summary, policy, random sampling or event scheduling is invoked.
The ledger starts at the supplied public origin, never at an invented time zero.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def close(actual, expected, path):
    require(type(actual) in (int, float) and math.isfinite(actual)
            and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-10),
            f"{path}: {actual!r} != {expected!r}")


def same(actual, expected, path):
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and actual.keys() == expected.keys(), f"{path}: fields differ")
        for key in expected:
            same(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), f"{path}: list shape differs")
        for index, value in enumerate(expected):
            same(actual[index], value, f"{path}[{index}]")
    elif type(expected) in (int, float):
        require(type(actual) in (int, float), f"{path}: expected a number")
        require(actual == expected, f"{path}: {actual!r} != {expected!r}")
    else:
        require(type(actual) is type(expected) and actual == expected, f"{path}: value differs")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def audit_planning_branch(result, request=None):
    """Check one full evaluate() artifact; optional request binds external input."""
    state = result["final_state"]
    origin = state["origin"]
    public = origin["public_state"]
    context = origin["rule_context"]
    parameters = origin["public_parameters"]
    reconstructed_request = {"protocol_version": origin["decision_protocol_version"],
                             "observation": public, "context": context, "parameters": parameters,
                             "request_id": origin["request_id"]}
    if request is not None:
        expected_request = request.to_dict() if hasattr(request, "to_dict") else request
        same(reconstructed_request, expected_request, "origin.request")
    content = {key: value for key, value in reconstructed_request.items() if key != "request_id"}
    require(digest(content) == origin["request_id"], "Origin request identity mismatch")
    require(digest(reconstructed_request) == origin["legal_input_hash"] == result["legal_input_hash"],
            "Origin legal-input hash mismatch")
    start, end = context["window_start"], context["window_end"]
    require(public["time"] == origin["as_of"] == result["origin"] == start, "Origin time mismatch")
    require(state["time"] == result["window_end"] == origin["window_end"] == end, "Branch endpoint mismatch")
    require(result["duration_hours"] == end - start, "Actual simulated duration mismatch")
    require(state["domain"] == "planning" and state["random_manifest"]["domain"] == "planning",
            "Execution domain in planning artifact")
    require(result["scenario_id"] == origin["scenario_id"] == state["random_manifest"]["scenario_id"],
            "Scenario identity mismatch")
    require(state["random_manifest"]["seed"] == origin["planning_seed"], "Planning seed mismatch")
    states = {row["id"]: row["state"] for row in public["aircraft"]}
    active = {row["id"]: row["mission_id"] for row in public["aircraft"] if row["state"] == "flying"}
    missions = {row["id"]: deepcopy(row) for row in public["missions"]}
    repairs = {row["id"]: deepcopy(row) for row in public["repairs"]}
    orders = {row["id"]: deepcopy(row) for row in public["orders"]}
    accepted_before = {row["id"] for row in public["commitments"] if row["accepted"]}
    offered = [row["id"] for row in public["missions"] if start <= row["start_time"] < end]
    required = list(context["active_rule"]["mandatory_ids"])
    plane_hours = {row["id"]: row["flight_hours"] for row in public["aircraft"]}
    mission_hours = {mid: (0. if row["actual_start"] is None else
                          ((start if row["actual_end"] is None else row["actual_end"]) - row["actual_start"])
                          * len(row["assigned_aircraft"])) for mid, row in missions.items()}
    metrics = {name: 0. for name in ("flight_hours", "available_aircraft_hours", "idle_aircraft_hours",
                                    "spare_wait_hours", "bay_wait_hours")}
    counts = Counter()
    on_hand = public["inventory"]["on_hand"]
    prior_ordered = sum(row["quantity"] for row in orders.values())
    prior_arrived = sum(row["quantity"] for row in orders.values() if row["status"] == "arrived")
    offsets = {**dict.fromkeys(metrics, 0.), "flight_hours": math.fsum(plane_hours.values()),
               "failures": len(repairs), "repairs_started": sum(row["started_at"] is not None for row in repairs.values()),
               "repairs_completed": sum(row["status"] == "completed" for row in repairs.values()),
               "ordered_quantity": prior_ordered, "arrived_quantity": prior_arrived}
    same(origin["metric_offsets"], offsets, "origin.metric_offsets")
    require(origin["initial_spares_conservation_offset"] == on_hand + offsets["repairs_started"] - prior_arrived,
            "Origin inventory conservation offset mismatch")
    expected_transit = sum(row["quantity"] for row in orders.values() if row["status"] == "in_transit")
    require(expected_transit == public["inventory"]["in_transit"], "Origin transit mismatch")
    require(sum(row["status"] == "waiting" for row in repairs.values()) == public["inventory"]["unmet_demand"],
            "Origin waiting demand mismatch")
    previous = start
    accepted = None
    command = None
    days, checks, daily_commands = [], [], []
    physical_snapshots = set()
    history_fields = ("time", "idle", "flying", "waiting_repair", "repairing", "on_hand", "in_transit", "unmet_demand")

    def physical_snapshot(time):
        frequencies = Counter(states.values())
        row = {"time": time, **{name: frequencies[name] for name in
                               ("idle", "flying", "waiting_repair", "repairing")},
               "on_hand": on_hand,
               "in_transit": sum(row["quantity"] for row in orders.values() if row["status"] == "in_transit"),
               "unmet_demand": sum(row["status"] == "waiting" for row in repairs.values())}
        physical_snapshots.add(tuple(row[name] for name in history_fields))

    def integrate(target):
        nonlocal previous
        require(start <= previous <= target <= end, "Branch log time regressed/outside interval")
        duration = target - previous
        frequencies = Counter(states.values())
        waiting = sorted((row for row in repairs.values() if row["status"] == "waiting"),
                         key=lambda row: (row["requested_at"], row["id"]))
        covered = min(on_hand, len(waiting))
        metrics["flight_hours"] += frequencies["flying"] * duration
        metrics["available_aircraft_hours"] += (frequencies["idle"] + frequencies["flying"]) * duration
        metrics["idle_aircraft_hours"] += frequencies["idle"] * duration
        metrics["spare_wait_hours"] += (len(waiting) - covered) * duration
        metrics["bay_wait_hours"] += covered * duration
        for aid, mid in active.items():
            plane_hours[aid] += duration
            mission_hours[mid] += duration
        for index, row in enumerate(waiting):
            field = "bay_wait_hours" if index < covered else "spare_wait_hours"
            row[field] += duration
        previous = target

    physical_snapshot(start)
    for index, event in enumerate(state["events"]):
        require(event["event_id"] == f"E{index + 1:07d}", "Branch event ID discontinuity")
        integrate(event["time"])
        kind, entity, time = event["event_type"], event["entity_id"], event["time"]
        details = json.loads(event["details"])
        counts[kind] += 1
        if kind == "weekly_command":
            require(command is None and time == start, "Branch must commit its candidate once at origin")
            command = details["command"]
            same(command, result["command"], "branch.command")
            accepted = command["accepted_ids"]
            require(len(set(accepted)) == len(accepted) and set(accepted) <= set(offered), "Illegal accepted IDs")
            require(set(command["mandatory_ids"]) == set(required), "Command changed public obligations")
            for mid in offered:
                if mid not in accepted:
                    missions[mid]["status"] = "rejected"
        elif kind == "daily_command":
            require(time < end and time not in days, "Duplicate/endpoint daily command")
            days.append(time)
            daily_commands.append(details["command"])
        elif kind == "reorder_check":
            require(time not in checks and time in days, "Replenishment lacks unique daily command")
            checks.append(time)
            position = on_hand + sum(row["quantity"] for row in orders.values() if row["status"] == "in_transit") \
                - sum(row["status"] == "waiting" for row in repairs.values())
            require(details["inventory_position"] == position, "Replenishment position mismatch")
            require(details["quantity"] == max(0, parameters["reorder_target"] - position), "Replenishment quantity mismatch")
        elif kind == "mission_start":
            require(command is not None and entity in (set(accepted) | accepted_before), "Uncommitted mission start")
            mission = missions[entity]
            require(mission["status"] == "pending" and time == mission["start_time"], "Invalid mission launch")
            selected = sorted(aid for aid, value in states.items() if value == "idle")[:mission["aircraft_required"]]
            require(len(selected) == mission["aircraft_required"] and selected == details["aircraft"], "Dispatch selection mismatch")
            mission.update(status="running", actual_start=time, assigned_aircraft=list(details["aircraft"]))
        elif kind == "aircraft_dispatch":
            require(states[entity] == "idle" and entity in missions[details["mission"]]["assigned_aircraft"], "Unavailable dispatch")
            states[entity], active[entity] = "flying", details["mission"]
        elif kind == "aircraft_release":
            require(states[entity] == "flying", "Release of nonflying aircraft")
            states[entity] = event["new_state"]
            active.pop(entity)
        elif kind in {"mission_success", "mission_failed", "mission_not_executed"}:
            mission = missions[entity]
            expected_before = "pending" if kind == "mission_not_executed" else "running"
            require(mission["status"] == expected_before, "Duplicate/invalid mission outcome")
            if kind == "mission_success":
                require(time == mission["start_time"] + mission["duration_hours"], "Mission completion time mismatch")
            elif kind == "mission_not_executed":
                require(time == mission["start_time"], "Not-executed time mismatch")
                require(sum(value == "idle" for value in states.values()) < mission["aircraft_required"], "Dispatch falsely rejected")
            mission.update(status={"mission_success": "success", "mission_failed": "failed",
                                   "mission_not_executed": "not_executed"}[kind], actual_end=time)
        elif kind == "mission_rejected_at_start":
            require(missions[entity]["status"] == "rejected" and time == missions[entity]["start_time"], "False rejection log")
        elif kind == "failure":
            repair_id = details["repair_id"]
            require(states[entity] == "waiting_repair" and repair_id not in repairs, "Invalid repair request")
            repairs[repair_id] = {"id": repair_id, "aircraft_id": entity, "requested_at": time, "status": "waiting",
                                  "started_at": None, "completed_at": None, "spare_wait_hours": 0., "bay_wait_hours": 0.}
        elif kind == "repair_start":
            waiting = sorted((row for row in repairs.values() if row["status"] == "waiting"),
                             key=lambda row: (row["requested_at"], row["id"]))
            require(waiting and waiting[0]["id"] == entity and on_hand > 0, "Non-FIFO/unfunded repair start")
            row = repairs[entity]
            require(row["aircraft_id"] == details["aircraft"], "Repair aircraft mismatch")
            row.update(status="repairing", started_at=time)
            on_hand -= 1
            states[details["aircraft"]] = "repairing"
            require(details["on_hand"] == on_hand, "Repair stock mismatch")
        elif kind == "repair_complete":
            row = repairs[entity]
            require(row["status"] == "repairing" and row["aircraft_id"] == details["aircraft"], "Invalid repair completion")
            row.update(status="completed", completed_at=time)
            states[details["aircraft"]] = "idle"
        elif kind == "order_placed":
            require(entity not in orders, "Duplicate order")
            orders[entity] = {"id": entity, "quantity": details["quantity"], "ordered_at": time,
                              "expected_arrival": details["expected_arrival"], "status": "in_transit", "arrived_at": None}
        elif kind == "order_arrival":
            row = orders[entity]
            require(row["status"] == "in_transit" and row["quantity"] == details["quantity"], "Duplicate/changed order arrival")
            require(time == row["expected_arrival"], "Arrival violated public ETA")
            row.update(status="arrived", arrived_at=time)
            on_hand += row["quantity"]
            require(details["on_hand"] == on_hand, "Arrival stock mismatch")
        elif kind == "terminal_boundary":
            require(time == end == 720 and counts[kind] == 1, "Invalid terminal boundary")
            expected_ids = [row["event_id"] for row in state["events"][:index] if row["time"] == 720]
            same(details, {"time": 720., "event_ids": expected_ids}, "terminal_boundary")
        elif kind != "stale_event":
            raise AssertionError(f"Unexpected planning event {kind}")
        require(on_hand >= 0 and sum(value == "repairing" for value in states.values()) <= parameters["repair_bays"],
                "Physical resource bound violation")
        physical_snapshot(time)
    integrate(end)
    physical_snapshot(end)
    require(command is not None, "Missing planning command")
    require(days == checks == [float(t) for t in range(0, 720, 24) if start <= t < end], "Daily cadence mismatch")
    metrics.update(failures=counts["failure"], repairs_started=counts["repair_start"],
                   repairs_completed=counts["repair_complete"],
                   ordered_quantity=sum(row["quantity"] for row in orders.values()) - prior_ordered,
                   arrived_quantity=sum(row["quantity"] for row in orders.values() if row["status"] == "arrived") - prior_arrived)
    require(result["metrics_delta"].keys() == metrics.keys(), "Metric delta fields mismatch")
    for name, value in metrics.items():
        close(result["metrics_delta"][name], value, f"metrics_delta.{name}")
        close(state["metrics"][name] - offsets[name], value, f"final.metrics.{name}")
    require(state["on_hand"] == on_hand, "Final inventory mismatch")
    for plane in state["aircraft"]:
        aid = plane["id"]
        require(plane["state"] == states[aid] and plane["mission_id"] == active.get(aid), "Final aircraft state mismatch")
        close(plane["flight_hours"], plane_hours[aid], f"aircraft.{aid}.flight_hours")
    require({row["id"] for row in state["aircraft"]} == set(states), "Final aircraft ID mismatch")
    require({row["spec"]["id"] for row in state["missions"]} == set(missions), "Final mission ID mismatch")
    for final in state["missions"]:
        mid = final["spec"]["id"]
        row = missions[mid]
        same(final["spec"], {name: row[name] for name in final["spec"]}, f"mission.{mid}.spec")
        for name in ("status", "actual_start", "actual_end", "assigned_aircraft"):
            same(final[name], row[name], f"mission.{mid}.{name}")
        close(final["flight_hours"], mission_hours[mid], f"mission.{mid}.flight_hours")
    require({row["id"] for row in state["repairs"]} == set(repairs), "Final repair IDs mismatch")
    for final in state["repairs"]:
        for name, value in repairs[final["id"]].items():
            if name.endswith("wait_hours"):
                close(final[name], value, f"repair.{final['id']}.{name}")
            else:
                same(final[name], value, f"repair.{final['id']}.{name}")
    same(state["orders"], list(orders.values()), "final.orders")
    for row in state["history"]:
        require(set(row) == set(history_fields) and tuple(row[name] for name in history_fields) in physical_snapshots,
                "History snapshot unsupported by origin/event ledger")
    successful = [mid for mid in accepted if missions[mid]["status"] == "success"]
    cohort = {"offered": len(offered), "accepted": len(accepted), "rejected": len(offered) - len(accepted),
              **{name: sum(missions[mid]["status"] == status for mid in accepted)
                 for name, status in (("success", "success"), ("failed", "failed"),
                                      ("accepted_not_executed", "not_executed"),
                                      ("pending_execution", "pending"), ("running", "running"))},
              "offered_value": sum(missions[mid]["value"] for mid in offered),
              "accepted_value": sum(missions[mid]["value"] for mid in accepted),
              "earned_value": sum(missions[mid]["value"] for mid in successful),
              "mandatory_shortfall_as_of": sum(missions[mid]["status"] != "success" for mid in required)}
    cohort["unfinished"] = cohort["pending_execution"] + cohort["running"]
    cohort["launched"] = cohort["success"] + cohort["failed"] + cohort["running"]
    cohort["mandatory_shortfall_terminal"] = cohort["mandatory_shortfall_as_of"] if end == 720 else None
    validity = {}
    for name, numerator, denominator in (("DSR", "success", "offered"), ("RCR", "earned_value", "offered_value"),
                                         ("ASR", "success", "accepted"), ("acceptance_rate", "accepted", "offered")):
        validity[name] = cohort[denominator] != 0
        cohort[name] = cohort[numerator] / cohort[denominator] if validity[name] else None
    cohort["validity"] = validity
    same(result["counts"], cohort, "result.counts")
    same(result["validity"], {name: validity[name] for name in ("DSR", "RCR")}, "result.validity")
    score_components = {name: cohort[name] if validity[name] else 0. for name in ("DSR", "RCR")}
    same(result["score_components"], score_components, "result.score_components")
    same(result["score"], sum(score_components.values()), "result.score")
    require(result["unresolved"] == cohort["unfinished"], "Unresolved count mismatch")
    weekly = state["weekly_state"]
    same(weekly["weekly_commands"], [command], "weekly_commands")
    same(weekly["daily_commands"], daily_commands, "daily_commands")
    for window, totals in weekly["window_metrics"].items():
        for name, value in totals.items():
            close(value, metrics[name] if float(window) == start else 0., f"window_metrics.{window}.{name}")
    require(set(weekly["cohorts"]) == {str(int(start))}, "Extra branch cohort")
    frozen = weekly["cohorts"][str(int(start))]
    same(frozen["command"], command, "cohort.command")
    require(frozen["window_start"] == start and frozen["window_end"] == end, "Frozen cohort window mismatch")
    same(frozen["offered_ids"], offered, "cohort.offered_ids")
    same(frozen["accepted_ids"], accepted, "cohort.accepted_ids")
    same(frozen["rejected_ids"], [mid for mid in offered if mid not in accepted], "cohort.rejected_ids")
    same(frozen["mandatory_ids"], required, "cohort.mandatory_ids")
    for name in ("offered_value", "accepted_value"):
        same(frozen[name], cohort[name], f"cohort.{name}")
    same(weekly["closed_cohort_snapshots"], [{"window_start": start, "as_of": end, "counts": cohort}], "closed_cohort_snapshots")
    require(counts["terminal_boundary"] == int(end == 720), "Terminal ledger count mismatch")
    terminal = ({"time": 720., "event_ids": [row["event_id"] for row in state["events"]
                                            if row["time"] == 720 and row["event_type"] != "terminal_boundary"]}
                if end == 720 else None)
    same(weekly["terminal_boundary"], terminal, "weekly.terminal_boundary")
    return {"verified": True, "scenario_id": result["scenario_id"], "legal_input_hash": result["legal_input_hash"],
            "origin": start, "end": end, "counts": cohort, "metrics_delta": metrics,
            "score": sum(score_components.values()), "events_checked": len(state["events"]),
            "request_externally_bound": request is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch", type=Path)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    result = json.loads(args.branch.read_text(encoding="utf-8"))
    request = json.loads(args.request.read_text(encoding="utf-8")) if args.request else None
    print(json.dumps(audit_planning_branch(result, request), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
