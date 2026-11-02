"""Real weekly-mode recovery acceptance and independent business-log accounting.

This program runs the production engine. Its ledger checks only reconstruct
already logged facts; they do not sample, schedule or simulate another world.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim_jev.provenance import canonical_sha256, code_fingerprint, git_state, runtime_metadata
from sim_jev.decision import policy_version
from sim_jev.versions import (AUDIT_STATE_SCHEMA_VERSION, PROVENANCE_SCHEMA_VERSION,
                              WEEKLY_MODEL_VERSION, KEYED_RNG_PROTOCOL_VERSION,
                              WEEKLY_CHECKPOINT_SCHEMA_VERSION, WEEKLY_POLICY_VERSION,
                              DAILY_POLICY_VERSION)

REL_TOL = 1e-12
ABS_TOL = 1e-10
STOPS = (0, 0.125, 35.375, 168, 336, 504, 672, 720)
ACCUMULATED = {
    "flight_hours", "spare_wait_hours", "bay_wait_hours", "total_repair_wait_hours",
    "available_aircraft_hours", "idle_aircraft_hours", "availability", "idle_fraction",
    "actual_aircraft_hours", "actual_flight_hours",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def close(actual, expected, path):
    require(math.isclose(actual, expected, rel_tol=REL_TOL, abs_tol=ABS_TOL),
            f"{path}: {actual!r} != {expected!r}")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")


def compare_values(expected, actual, path="root", differences=None):
    """Compare all inputs/identities exactly and only accumulated floats loosely."""
    differences = [] if differences is None else differences
    if isinstance(expected, dict):
        require(type(actual) is dict and expected.keys() == actual.keys(), f"{path}: fields differ")
        for key in expected:
            compare_values(expected[key], actual[key], f"{path}.{key}", differences)
    elif isinstance(expected, list):
        require(type(actual) is list and len(expected) == len(actual), f"{path}: list shape differs")
        for index, (left, right) in enumerate(zip(expected, actual)):
            compare_values(left, right, f"{path}[{index}]", differences)
    elif path.rsplit(".", 1)[-1] in ACCUMULATED and type(expected) in (int, float):
        require(type(actual) in (int, float), f"{path}: numeric type differs")
        close(actual, expected, path)
        if expected != actual:
            differences.append({"path": path, "expected": expected, "actual": actual,
                                "absolute_difference": abs(expected - actual)})
    else:
        require(type(expected) is type(actual) and expected == actual,
                f"{path}: {expected!r} != {actual!r}")
    return differences


def normalized_history(simulation):
    """Drop only unchanged non-business pause samples; preserve every log row."""
    business_times = {row["time"] for row in simulation.events}
    rows = simulation.history
    result = []
    for index, row in enumerate(rows):
        previous = rows[index - 1] if index else None
        pure_sample = (0 < index < len(rows) - 1 and row["time"] not in business_times
                       and previous["time"] != row["time"] != rows[index + 1]["time"]
                       and {key: value for key, value in row.items() if key != "time"}
                       == {key: value for key, value in previous.items() if key != "time"})
        if not pure_sample:
            result.append(row)
    return result


def compare_simulations(expected, actual):
    require(expected.events == actual.events, "Complete ordered business logs differ")
    require(normalized_history(expected) == normalized_history(actual), "State histories differ")
    left, right = expected.to_checkpoint(), actual.to_checkpoint()
    # Checkpoint layout is intentionally compared in full. Only the state
    # history may contain extra sampling rows caused by a non-event pause.
    for checkpoint, simulation in ((left, expected), (right, actual)):
        if "history" in checkpoint:
            checkpoint["history"] = normalized_history(simulation)
        if "physical" in checkpoint and "history" in checkpoint["physical"]:
            checkpoint["physical"]["history"] = normalized_history(simulation)
    differences = compare_values(left, right, "checkpoint")
    differences.extend(compare_values(expected.summary(), actual.summary(), "summary"))
    return differences


def audit_business_log(simulation):
    """Recalculate physical counts, exposure, waits and inventory from full logs."""
    states = {f"A{index + 1:03d}": "idle" for index in range(simulation.config.fleet_size)}
    active_missions = {}
    flight_by_mission = Counter()
    flight_by_aircraft = Counter()
    repairs, orders = {}, {}
    on_hand = simulation.config.initial_spares
    totals = {name: 0.0 for name in (
        "flight_hours", "available_aircraft_hours", "idle_aircraft_hours", "spare_wait_hours", "bay_wait_hours")}
    window_integrals = {str(point): {name: 0.0 for name in totals} for point in (0, 168, 336, 504, 672)}
    counts = Counter()
    mission_outcomes, outcome_times, launch_times = {}, {}, {}
    launched_ids, offered_ids, accepted_ids, mandatory_ids = set(), set(), set(), set()
    offers_by_window, commands_by_window, offer_windows, rules_by_window = {}, {}, {}, {}
    planned_hours = []
    daily_times, replenish_times = [], []
    previous_time = 0.0

    def integrate(target):
        nonlocal previous_time
        require(target >= previous_time, "Business log time regressed")
        duration = target - previous_time
        state_counts = Counter(states.values())
        waiting = sum(item["status"] == "waiting" for item in repairs.values())
        covered = min(on_hand, waiting)
        totals["flight_hours"] += state_counts["flying"] * duration
        totals["available_aircraft_hours"] += (state_counts["idle"] + state_counts["flying"]) * duration
        totals["idle_aircraft_hours"] += state_counts["idle"] * duration
        totals["spare_wait_hours"] += (waiting - covered) * duration
        totals["bay_wait_hours"] += covered * duration
        interval_start = previous_time
        rates = {"flight_hours": state_counts["flying"],
                 "available_aircraft_hours": state_counts["idle"] + state_counts["flying"],
                 "idle_aircraft_hours": state_counts["idle"],
                 "spare_wait_hours": waiting - covered, "bay_wait_hours": covered}
        while interval_start < target:
            window = min(672, int(interval_start // 168) * 168)
            end = min(target, window + 168, 720)
            for name, rate in rates.items():
                window_integrals[str(window)][name] += rate * (end - interval_start)
            interval_start = end
        for aircraft_id, mission_id in active_missions.items():
            flight_by_aircraft[aircraft_id] += duration
            flight_by_mission[mission_id] += duration
        previous_time = target

    for index, event in enumerate(simulation.events):
        require(event["event_id"] == f"E{index + 1:07d}", "Business event ID discontinuity")
        integrate(event["time"])
        kind, entity = event["event_type"], event["entity_id"]
        details = json.loads(event["details"])
        counts[kind] += 1
        if kind == "rule_announced":
            rule = details["rule"]
            window = str(int(rule["window_start"]))
            require(window not in rules_by_window, "Duplicate announced rule window")
            rules_by_window[window] = rule
        elif kind == "weekly_offer":
            require(entity not in offers_by_window, "Duplicate weekly announcement")
            ids = details["offered_ids"]
            require(len(ids) == len(set(ids)) and offered_ids.isdisjoint(ids), "Duplicate mission offer")
            offered_ids.update(ids)
            offers_by_window[entity] = ids
            offer_windows[entity] = (event["time"], details["window_end"])
        elif kind == "weekly_command":
            require(entity in offers_by_window and entity not in commands_by_window,
                    "Weekly command has no unique preceding offer")
            command = details["command"]
            require(entity in rules_by_window and
                    set(command["mandatory_ids"]) == set(rules_by_window[entity]["mandatory_ids"]),
                    "Command changed announced mandatory obligations")
            require(set(command["accepted_ids"]) <= set(offers_by_window[entity]), "Accepted unoffered mission")
            planning_ids = (accepted_ids - set(mission_outcomes)) | set(command["accepted_ids"])
            start, end = event["time"], min(event["time"] + 168, 720)
            points, exposures = {}, []
            for mission_id in planning_ids:
                spec = simulation.missions[mission_id].spec
                left, right = max(start, spec.start_time), min(end, spec.start_time + spec.duration_hours)
                if right > left:
                    points[left] = points.get(left, 0) + spec.aircraft_required
                    points[right] = points.get(right, 0) - spec.aircraft_required
                    exposures.append(spec.aircraft_required * (right - left))
            occupancy = peak = 0
            for timestamp in sorted(points):
                occupancy += points[timestamp]
                peak = max(peak, occupancy)
            usage = command["plan_usage"]
            require(usage["peak_aircraft"] == peak, "Command nominal peak disagrees with interval ledger")
            require(usage["minimum_reserve"] == simulation.config.fleet_size - peak,
                    "Command nominal reserve disagrees with interval ledger")
            close(usage["aircraft_hours"], math.fsum(exposures), "command.planned_aircraft_hours")
            planned_hours.append(math.fsum(exposures))
            accepted_ids.update(command["accepted_ids"])
            mandatory_ids.update(command["mandatory_ids"])
            commands_by_window[entity] = command
        elif kind == "daily_command":
            require(event["time"] not in daily_times, "Duplicate daily command")
            daily_times.append(event["time"])
        elif kind == "reorder_check":
            require(event["time"] not in replenish_times, "Duplicate daily replenishment check")
            replenish_times.append(event["time"])
        elif kind == "mission_start":
            require(entity not in launched_ids, "Duplicate mission launch")
            if hasattr(simulation, "weekly_state"):
                require(entity in accepted_ids, "Dispatch happened before a weekly commitment")
            launched_ids.add(entity)
            launch_times[entity] = event["time"]
        elif kind == "aircraft_dispatch":
            require(states[entity] == "idle", f"Dispatch of unavailable {entity}")
            states[entity] = "flying"
            active_missions[entity] = details["mission"]
        elif kind == "aircraft_release":
            require(states[entity] == "flying", f"Release of nonflying {entity}")
            states[entity] = event["new_state"]
            active_missions.pop(entity)
        elif kind == "failure":
            require(states[entity] == "waiting_repair", "Failure must follow physical release")
            repair_id = details["repair_id"]
            require(repair_id not in repairs, "Duplicate repair request")
            repairs[repair_id] = {"aircraft": entity, "status": "waiting", "requested_at": event["time"]}
        elif kind == "repair_start":
            require(repairs[entity]["status"] == "waiting", "Repair start has no waiting request")
            waiting_ids = sorted((key for key, item in repairs.items() if item["status"] == "waiting"),
                                 key=lambda key: (repairs[key]["requested_at"], key))
            require(entity == waiting_ids[0], "Repair did not follow FIFO")
            require(on_hand > 0, "Repair consumed missing spare")
            on_hand -= 1
            repairs[entity]["status"] = "repairing"
            states[details["aircraft"]] = "repairing"
            require(details["on_hand"] == on_hand, "Logged spare quantity differs after repair start")
        elif kind == "repair_complete":
            require(repairs[entity]["status"] == "repairing", "Completion has no active repair")
            repairs[entity]["status"] = "completed"
            states[details["aircraft"]] = "idle"
        elif kind == "order_placed":
            require(entity not in orders, "Duplicate order")
            orders[entity] = {"quantity": details["quantity"], "arrived": False}
        elif kind == "order_arrival":
            require(entity in orders and not orders[entity]["arrived"], "Duplicate/missing arrival")
            require(orders[entity]["quantity"] == details["quantity"], "Arrival changed order quantity")
            orders[entity]["arrived"] = True
            on_hand += details["quantity"]
            require(details["on_hand"] == on_hand, "Logged stock differs after arrival")
        elif kind in {"mission_success", "mission_failed", "mission_not_executed"}:
            require(entity not in mission_outcomes, "Mission has multiple terminal business outcomes")
            mission_outcomes[entity] = kind
            outcome_times[entity] = event["time"]
        require(on_hand >= 0, "Negative stock")
        require(sum(value == "repairing" for value in states.values()) <= simulation.config.repair_bays,
                "Repair capacity exceeded")
    integrate(simulation.now)
    expected_metrics = asdict(simulation.metrics)
    for name, value in totals.items():
        close(value, expected_metrics[name], f"log-accounting.{name}")
    for kind, name in (("failure", "failures"), ("repair_start", "repairs_started"),
                       ("repair_complete", "repairs_completed")):
        require(counts[kind] == expected_metrics[name], f"log-accounting.{name}")
    ordered = sum(item["quantity"] for item in orders.values())
    arrived = sum(item["quantity"] for item in orders.values() if item["arrived"])
    require(ordered == expected_metrics["ordered_quantity"], "Ordered quantity mismatch")
    require(arrived == expected_metrics["arrived_quantity"], "Arrived quantity mismatch")
    require(on_hand == simulation.on_hand, "Final stock mismatch")
    require(ordered - arrived == simulation.in_transit, "Final in-transit mismatch")
    for aircraft_id, aircraft in simulation.aircraft.items():
        require(states[aircraft_id] == aircraft.state, f"Final state differs: {aircraft_id}")
        close(flight_by_aircraft[aircraft_id], aircraft.flight_hours, f"flight.{aircraft_id}")
    for mission_id, mission in simulation.missions.items():
        close(flight_by_mission[mission_id], mission.flight_hours, f"flight.{mission_id}")
    earned = sum(simulation.missions[key].spec.value for key, outcome in mission_outcomes.items()
                 if outcome == "mission_success")
    ledger = None
    if hasattr(simulation, "weekly_state"):
        expected_days = [float(point) for point in range(0, 720, 24) if point <= simulation.now]
        require(daily_times == expected_days == replenish_times, "Daily command/replenishment schedule differs")
        successful = {mid for mid, outcome in mission_outcomes.items() if outcome == "mission_success"}
        failed = {mid for mid, outcome in mission_outcomes.items() if outcome == "mission_failed"}
        not_executed = {mid for mid, outcome in mission_outcomes.items() if outcome == "mission_not_executed"}
        require(set(mission_outcomes) <= accepted_ids, "Unaccepted mission acquired a terminal outcome")
        ledger = {"offered": len(offered_ids), "accepted": len(accepted_ids),
                  "rejected": len(offered_ids - accepted_ids), "success": len(successful),
                  "failed": len(failed), "accepted_not_executed": len(not_executed),
                  "unfinished": len(accepted_ids - set(mission_outcomes)), "launched": len(launched_ids),
                  "pending_execution": len(accepted_ids - launched_ids - set(mission_outcomes)),
                  "running": len(launched_ids - set(mission_outcomes)),
                  "offered_value": sum(simulation.missions[mid].spec.value for mid in sorted(offered_ids)),
                  "accepted_value": sum(simulation.missions[mid].spec.value for mid in sorted(accepted_ids)),
                  "earned_value": earned, "mandatory_shortfall_as_of": len(mandatory_ids - successful)}
        summary = simulation.summary()
        for name, value in ledger.items():
            if name.endswith("value"):
                close(value, summary[name], f"log-ledger.{name}")
            else:
                require(value == summary[name], f"log-ledger.{name}: {value} != {summary[name]}")
        for name, numerator, denominator in (("DSR", "success", "offered"),
                                              ("RCR", "earned_value", "offered_value"),
                                              ("ASR", "success", "accepted"),
                                              ("acceptance_rate", "accepted", "offered")):
            if ledger[denominator] == 0:
                require(summary[name] is None and summary["validity"][name] is False, f"Undefined ratio {name}")
            else:
                close(summary[name], ledger[numerator] / ledger[denominator], f"log-ratio.{name}")
                require(summary["validity"][name] is True, f"Valid ratio {name} missing validity")
        for window, integrals in window_integrals.items():
            for name, value in integrals.items():
                close(value, summary["window_metrics"][window][name], f"window.{window}.{name}")
        close(summary["planned_aircraft_hours"], sum(planned_hours), "planned_aircraft_hours")
        # Derive each historical view from event timestamps. In particular a
        # later success must not be backdated into an already closed cohort.
        # Neither _cohort_summary nor the engine's ratio helper is used here.
        def cohort_counts(window, as_of):
            offered = offers_by_window[window]
            command = commands_by_window[window]
            accepted = command["accepted_ids"]
            terminal = {mid: mission_outcomes[mid] for mid in accepted
                        if mid in outcome_times and outcome_times[mid] <= as_of}
            started = {mid for mid in accepted
                       if mid in launch_times and launch_times[mid] <= as_of}
            succeeded = {mid for mid, kind in terminal.items() if kind == "mission_success"}
            row = {
                "offered": len(offered), "accepted": len(accepted),
                "rejected": len(set(offered) - set(accepted)),
                "success": len(succeeded),
                "failed": sum(kind == "mission_failed" for kind in terminal.values()),
                "accepted_not_executed": sum(kind == "mission_not_executed" for kind in terminal.values()),
                "pending_execution": len(set(accepted) - started - terminal.keys()),
                "running": len(started - terminal.keys()),
                "unfinished": len(set(accepted) - terminal.keys()),
                "launched": len(started),
                "offered_value": sum(simulation.missions[mid].spec.value for mid in offered),
                "accepted_value": sum(simulation.missions[mid].spec.value for mid in accepted),
                "earned_value": sum(simulation.missions[mid].spec.value for mid in accepted if mid in succeeded),
                "mandatory_shortfall_as_of": len(set(command["mandatory_ids"]) - succeeded),
            }
            row["mandatory_shortfall_terminal"] = row["mandatory_shortfall_as_of"] if as_of == 720 else None
            row["validity"] = {}
            for name, numerator, denominator in (("DSR", "success", "offered"),
                                                  ("RCR", "earned_value", "offered_value"),
                                                  ("ASR", "success", "accepted"),
                                                  ("acceptance_rate", "accepted", "offered")):
                row["validity"][name] = row[denominator] != 0
                row[name] = row[numerator] / row[denominator] if row[denominator] else None
            return row

        def check_cohort_record(actual, expected, path):
            require(type(actual) is dict and actual.keys() == expected.keys(), f"{path}: fields differ")
            for name, value in expected.items():
                observed = actual[name]
                if isinstance(value, dict):
                    check_cohort_record(observed, value, f"{path}.{name}")
                elif name in {"offered_value", "accepted_value", "earned_value", "window_start", "window_end", "as_of"}:
                    require(type(observed) in (int, float) and observed == value,
                            f"{path}.{name}: {observed!r} != {value!r}")
                else:
                    require(type(observed) is type(value) and observed == value,
                            f"{path}.{name}: {observed!r} != {value!r}")

        expected_cohorts, expected_closed = [], []
        require(offers_by_window.keys() == commands_by_window.keys(), "Offer lacks a committed command")
        require(simulation.weekly_state["cohorts"].keys() == offers_by_window.keys(),
                "Persisted cohort windows differ from announced windows")
        for window, (start, end) in offer_windows.items():
            counts_at_now = cohort_counts(window, simulation.now)
            expected_cohorts.append({"window_start": start, "window_end": end, **counts_at_now})
            persisted = simulation.weekly_state["cohorts"][window]
            command = commands_by_window[window]
            accepted = command["accepted_ids"]
            expected_frozen = {
                "window_start": start, "window_end": end,
                "offered_ids": offers_by_window[window], "accepted_ids": accepted,
                "rejected_ids": [mid for mid in offers_by_window[window] if mid not in accepted],
                "offered_value": counts_at_now["offered_value"],
                "accepted_value": counts_at_now["accepted_value"],
                "mandatory_ids": rules_by_window[window]["mandatory_ids"], "command": command,
            }
            require(persisted == expected_frozen, f"cohort.{window}: frozen commitment differs from business logs")
            if end <= simulation.now:
                expected_closed.append({"window_start": start, "as_of": end,
                                        "counts": cohort_counts(window, end)})
        require(type(summary["cohorts"]) is list and len(summary["cohorts"]) == len(expected_cohorts),
                "Summary cohort list differs from announcements")
        for index, (actual, expected) in enumerate(zip(summary["cohorts"], expected_cohorts)):
            check_cohort_record(actual, expected, f"cohorts[{index}]")
        closed = simulation.weekly_state["closed_cohort_snapshots"]
        require(type(closed) is list and len(closed) == len(expected_closed), "Closed cohort snapshot list differs")
        for index, (actual, expected) in enumerate(zip(closed, expected_closed)):
            check_cohort_record(actual, expected, f"closed_cohort_snapshots[{index}]")
        require(summary["mandatory_shortfall_terminal"] ==
                (ledger["mandatory_shortfall_as_of"] if simulation.now == 720 else None),
                "Terminal mandatory shortfall differs")
        require(summary["accepted"] + summary["rejected"] == summary["offered"], "Offer conservation")
        require(sum(summary[name] for name in ("success", "failed", "accepted_not_executed", "unfinished"))
                == summary["accepted"], "Accepted outcome conservation")
        if simulation.now == 720:
            require(counts["terminal_boundary"] == 1, "Terminal boundary must be logged exactly once")
            physical_at_terminal = [row["event_id"] for row in simulation.events
                                    if row["time"] == 720 and row["event_type"] != "terminal_boundary"]
            require(summary["terminal_boundary"]["event_ids"] == physical_at_terminal,
                    "Terminal physical events missing or counted twice")
    return {"passed": True, "event_counts": dict(counts), "integrals": totals,
            "window_integrals": window_integrals, "ledger": ledger,
            "mission_outcomes": mission_outcomes, "earned_value": earned,
            "cohorts": expected_cohorts if ledger is not None else [],
            "closed_cohort_snapshots": expected_closed if ledger is not None else [],
            "ordered_quantity": ordered, "arrived_quantity": arrived, "on_hand": on_hand,
            "in_transit": ordered - arrived, "rel_tol": REL_TOL, "abs_tol": ABS_TOL}


def run_command(argv, destination, name, records):
    result = subprocess.run(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", timeout=300)
    log = destination / f"{name}.log"
    log.write_text(result.stdout, encoding="utf-8")
    records.append({"argv": argv, "exit_code": result.returncode, "log": log.name})
    write_json(destination / "commands.json", records)
    require(result.returncode == 0, f"{name} exited {result.returncode}; see {log}")


def check_weekly_runtime(runtime, simulation, expected_fingerprint, *, require_identity=True):
    """Validate the actual weekly identities without substituting legacy versions."""
    for name, expected in (("schema_version", PROVENANCE_SCHEMA_VERSION),
                           ("audit_state_schema_version", AUDIT_STATE_SCHEMA_VERSION),
                           ("checkpoint_schema_version", WEEKLY_CHECKPOINT_SCHEMA_VERSION),
                           ("model_version", WEEKLY_MODEL_VERSION),
                           ("rng_protocol_version", KEYED_RNG_PROTOCOL_VERSION)):
        require(type(runtime.get(name)) is type(expected) and runtime.get(name) == expected,
                f"Unexpected weekly runtime {name}")
    require(runtime["effective_config_sha256"] == canonical_sha256(asdict(simulation.config)),
            "Weekly runtime effective input hash differs")
    policy = simulation.weekly_state["policy_spec"]
    require(runtime["policy_identity"] == {
        "weekly_version": policy_version(policy), "daily_version": DAILY_POLICY_VERSION,
        "kind": policy["kind"], "name": policy["name"], "input_sha256": canonical_sha256(policy)},
        "Weekly runtime policy identity differs")
    require(bool(runtime["python_version"]) and bool(runtime["python_executable"])
            and type(runtime["packages"]) is dict and bool(runtime["packages"]), "Missing weekly runtime versions")
    require(type(runtime["actual_command"]) is list and bool(runtime["actual_command"])
            and all(type(part) is str for part in runtime["actual_command"]), "Missing weekly runtime command")
    fingerprint = runtime["code_fingerprint"]
    require(fingerprint["algorithm"] == "sha256" and fingerprint["scope"] in {
                "project-source-and-config-v1", "project-source-and-config-v2"},
            "Unexpected weekly source-fingerprint format")
    require(fingerprint["sha256"] == canonical_sha256(fingerprint["files"]), "Weekly source manifest is inconsistent")
    verified = fingerprint == expected_fingerprint
    if require_identity:
        require(verified, "Source changed during weekly acceptance; rerun against a stable checkout")
    return {"metadata_valid": True, "manifest_self_consistent": True,
            "source_identity_verified": verified, "expected_source_sha256": expected_fingerprint["sha256"]}


def dump_evidence(simulation, folder, expected_fingerprint, *, require_identity=True):
    """Preserve complete trace and complete internal checkpoint for audit."""
    folder.mkdir(parents=True, exist_ok=False)
    checkpoint = simulation.to_checkpoint()
    write_json(folder / "final.checkpoint.json", checkpoint)
    write_json(folder / "events.json", simulation.events)
    write_json(folder / "history.json", simulation.history)
    write_json(folder / "summary.json", simulation.summary())
    write_json(folder / "config.json", asdict(simulation.config))
    write_json(folder / "missions.json", [asdict(item) for item in simulation.missions.values()])
    write_json(folder / "weekly-state.json", simulation.weekly_state)
    write_json(folder / "weekly-commands.json", simulation.weekly_state["weekly_commands"])
    write_json(folder / "daily-commands.json", simulation.weekly_state["daily_commands"])
    write_json(folder / "rules.json", simulation.weekly_state["rules_input"])
    write_json(folder / "random-manifest.json", simulation.keyed_rng.to_dict())
    policy = simulation.weekly_state["policy_spec"]
    runtime = runtime_metadata(
        asdict(simulation.config),
        model_version=simulation.model_version, rng_protocol_version=simulation.rng_protocol_version,
        checkpoint_schema_version=simulation.checkpoint_schema_version,
        policy_identity={"weekly_version": policy_version(policy), "daily_version": DAILY_POLICY_VERSION,
                         "kind": policy["kind"], "name": policy["name"], "input_sha256": canonical_sha256(policy)},
        run_context={"acceptance": "weekly-verify", "scenario_id": simulation.weekly_state["scenario_id"],
                     "seed": simulation.config.seed, "expected_source_sha256": expected_fingerprint["sha256"]})
    write_json(folder / "runtime.json", runtime)
    runtime_checks = check_weekly_runtime(runtime, simulation, expected_fingerprint, require_identity=require_identity)
    write_json(folder / "runtime-verification.json", runtime_checks)
    accounting = audit_business_log(simulation)
    write_json(folder / "independent-accounting.json", accounting)
    return accounting


def child_resume(source, destination):
    from sim_jev.weekly import WeeklySimulation

    simulation = WeeklySimulation.load_checkpoint(source)
    before = {"time": simulation.now,
              "repairing": sum(item.status == "repairing" for item in simulation.repairs.values()),
              "in_transit": simulation.in_transit,
              "business_events": len(simulation.events)}
    simulation.run()
    simulation.save_checkpoint(destination)
    print(json.dumps({"passed": True, "resumed_state": before,
                      "final_time": simulation.now, "business_events": len(simulation.events)},
                     ensure_ascii=False, sort_keys=True))


def fresh_process(source, destination, whole, records, name):
    from sim_jev.weekly import WeeklySimulation

    final_path = destination / f"{name}.final.checkpoint.json"
    run_command([sys.executable, str(Path(__file__).resolve()), "--resume-child", str(source),
                 "--final-checkpoint", str(final_path)], destination, name, records)
    restored = WeeklySimulation.load_checkpoint(final_path)
    differences = compare_simulations(whole, restored)
    return {"name": name, "source": str(source), "final_checkpoint": final_path.name,
            "different_accumulations": differences}


def verify(destination, *, allow_source_changes=False):
    from sim_jev.weekly import WeeklySimulation
    from sim_jev.weekly_config import load_weekly_simulation

    require(sys.flags.optimize == 0, "Weekly acceptance refuses optimized Python (-O/-OO)")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    records, outcomes, processes = [], [], []
    initial_fingerprint = code_fingerprint(ROOT)
    write_json(destination / "source-before.json", {"fingerprint": initial_fingerprint, "git": git_state(ROOT)})
    for scenario in ("default", "stress"):
        config_path = ROOT / "configs" / f"weekly_{scenario}.json"
        for seed in range(10):
            print(f"Weekly {scenario} seed {seed}: whole run, JSON round trips and log accounting", flush=True)
            whole = load_weekly_simulation(config_path, seed)
            effective = whole.config
            whole.run()
            folder = destination / f"{scenario}-{seed}"
            accounting = dump_evidence(whole, folder, initial_fingerprint,
                                       require_identity=not allow_source_changes)
            branch = load_weekly_simulation(config_path, seed)
            for stop in STOPS:
                branch.run(until=stop)
                source = folder / f"pause-{stop}.checkpoint.json"
                branch.save_checkpoint(source)
                branch = WeeklySimulation.load_checkpoint(source)
                if seed == 0:
                    processes.append(fresh_process(source, destination, whole, records,
                                                   f"{scenario}-{seed}-{stop}-process"))
            differences = compare_simulations(whole, branch)
            audit_business_log(branch)
            outcomes.append({"scenario": scenario, "seed": seed, "roundtrips": len(STOPS),
                             "different_accumulations": differences, "summary": whole.summary(),
                             "events": len(whole.events), "independent_accounting_passed": accounting["passed"]})
            write_json(folder / "pair-comparison.json", outcomes[-1])
            if scenario == "stress" and seed == 0:
                coverage = {}
                for label, timestamp in (
                    ("in_repair", next((item.started_at for item in whole.repairs.values()
                                        if item.started_at is not None and item.started_at < effective.horizon_hours
                                        and item.due_at > item.started_at), None)),
                    ("in_transit", next((item.ordered_at for item in whole.orders.values()
                                         if item.expected_arrival > item.ordered_at
                                         and item.ordered_at < effective.horizon_hours), None)),
                ):
                    require(timestamp is not None, f"Smoke scenario did not exercise {label}")
                    pause = load_weekly_simulation(config_path, seed)
                    pause.run(until=timestamp)
                    if label == "in_repair":
                        require(any(item.status == "repairing" for item in pause.repairs.values()),
                                "Selected process fixture is not in repair")
                    else:
                        require(pause.in_transit > 0, "Selected process fixture has no in-transit order")
                    source = folder / f"{label}.checkpoint.json"
                    pause.save_checkpoint(source)
                    coverage[label] = {"time": pause.now, "in_transit": pause.in_transit,
                                       "repairing": sum(item.status == "repairing" for item in pause.repairs.values())}
                    processes.append(fresh_process(source, destination, whole, records, f"{scenario}-{label}-process"))
                write_json(destination / "cross-process-state-coverage.json", coverage)
    final_fingerprint = code_fingerprint(ROOT)
    source_stable = final_fingerprint == initial_fingerprint
    write_json(destination / "source-after.json", {"fingerprint": final_fingerprint, "git": git_state(ROOT),
                                                  "source_stable": source_stable})
    if not allow_source_changes:
        require(source_stable, "Source changed during weekly acceptance; final acceptance requires a stable checkout")
    result = {"passed": True, "source_stable": source_stable,
              "source_identity_required": not allow_source_changes,
              "accepted_source_sha256": initial_fingerprint["sha256"] if source_stable else None,
              "rel_tol": REL_TOL, "abs_tol": ABS_TOL,
              "smoke_pairs": len(outcomes), "roundtrips": len(outcomes) * len(STOPS),
              "cross_process_pairs": len(processes), "results": outcomes, "process_results": processes,
              "parameter_basis": "Synthetic software acceptance; no PHM/policy-effectiveness claim"}
    write_json(destination / "verification.json", result)
    table = []
    for row in outcomes:
        flat = {"scenario": row["scenario"], "seed": row["seed"], "roundtrips": row["roundtrips"],
                "events": row["events"], "independent_accounting_passed": row["independent_accounting_passed"],
                **{key: value for key, value in row["summary"].items() if not isinstance(value, (dict, list))},
                "different_accumulations": len(row["different_accumulations"]),
                "maximum_absolute_difference": max((item["absolute_difference"] for item in row["different_accumulations"]),
                                                   default=0.0)}
        table.append(flat)
    with (destination / "per-seed.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    hashes = {path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(destination.rglob("*")) if path.is_file()}
    write_json(destination / "output-sha256.json", hashes)
    print(json.dumps({key: result[key] for key in ("passed", "smoke_pairs", "roundtrips", "cross_process_pairs")},
                     indent=2))


def main():
    if sys.flags.optimize:
        print("Weekly acceptance refuses optimized Python (-O/-OO)", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume-child", type=Path)
    parser.add_argument("--final-checkpoint", type=Path)
    parser.add_argument("--allow-source-changes", action="store_true",
                        help="Development evidence only; do not require one stable source fingerprint")
    arguments = parser.parse_args()
    if arguments.resume_child is not None:
        if arguments.final_checkpoint is None or arguments.output is not None:
            parser.error("--resume-child requires --final-checkpoint and excludes --output")
        child_resume(arguments.resume_child, arguments.final_checkpoint)
    else:
        if arguments.output is None or arguments.final_checkpoint is not None:
            parser.error("Specify --output for an acceptance run")
        verify(arguments.output, allow_source_changes=arguments.allow_source_changes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
