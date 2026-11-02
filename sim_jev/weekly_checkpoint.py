"""The weekly model's internal checkpoint contract.

Physical entities use the shared parser under an explicit weekly profile. No
legacy identity is substituted, and restoration never calls reset or a policy.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path

from . import checkpoint as common
from .keyed_rng import KeyedRNG
from .versions import DAILY_POLICY_VERSION, WEEKLY_POLICY_VERSION
from .decision import DECISION_PROTOCOL_VERSION, policy_from_dict, policy_version


MODEL_VERSION = "synthetic-weekly-single-lru-v1"
RNG_PROTOCOL_VERSION = "sha256-entity-counter-v1"
CHECKPOINT_SCHEMA_VERSION = 1
WEEKLY_STATE_FIELDS = {
    "scenario_id", "policy_spec", "rules_input", "announced_rule_versions", "cohorts",
    "mission_cohort", "commitments", "weekly_commands", "daily_commands",
    "processed_week_points", "processed_day_points", "component_generations",
    "window_metrics", "closed_cohort_snapshots", "terminal_boundary", "keyed_rng", "policy_versions",
}
COHORT_FIELDS = {
    "window_start", "window_end", "offered_ids", "accepted_ids", "rejected_ids",
    "offered_value", "accepted_value", "mandatory_ids", "command",
}


def export_weekly_state(sim):
    state = common._json_copy(sim.weekly_state, "weekly_state")
    common._check("keyed_rng" not in state, "weekly_state", "live state must not duplicate RNG state")
    state["keyed_rng"] = sim.keyed_rng.to_dict()
    state["policy_versions"] = {"weekly": policy_version(state["policy_spec"]),
                                "daily": DAILY_POLICY_VERSION}
    return state


def _ids(values, path, known):
    common._list(values, path)
    common._check(all(isinstance(value, str) and value in known for value in values),
                  path, "unknown entity reference")
    common._check(len(values) == len(set(values)), path, "duplicate entity reference")
    return values


def _points(values, expected, path):
    common._list(values, path)
    for index, value in enumerate(values):
        common._number(value, f"{path}[{index}]")
    common._check(values == expected, path, "processed points disagree with stable time")


def restore_weekly_state(sim, source):
    from .weekly_policy import (PolicyObservation, PolicySpec, RuleContext, RuleSpec,
                                WeeklyCommand, validate_command)

    state = common._json_copy(source, "weekly_state")
    new_policy = isinstance(state.get("policy_spec"), dict) and state["policy_spec"].get("kind") == "candidate-des"
    fields = WEEKLY_STATE_FIELDS | ({"decision_protocol_version", "decision_records"} if new_policy else set())
    common._shape(state, fields, "weekly_state")
    common._check(state.pop("policy_versions") ==
                  {"weekly": policy_version(state["policy_spec"]), "daily": DAILY_POLICY_VERSION},
                  "weekly_state.policy_versions", "unsupported policy identity")
    common._text(state["scenario_id"], "weekly_state.scenario_id")
    common._check(sim.config.horizon_hours == 720, "config.horizon_hours", "weekly horizon must be 720")
    common._check(sim.config.reorder_interval_hours == 24, "config.reorder_interval_hours",
                  "weekly executor requires daily replenishment")
    policy_from_dict(state["policy_spec"])
    rules = [RuleSpec.from_dict(item) for item in common._list(state["rules_input"], "weekly_state.rules_input")]
    versions = [rule.version for rule in rules]
    common._check(len(versions) == len(set(versions)), "weekly_state.rules_input", "duplicate rule version")
    common._check(len(rules) == 5 and [rule.window_start for rule in rules] == [0, 168, 336, 504, 672],
                  "weekly_state.rules_input", "one canonically ordered rule per window is required")
    for rule in rules:
        common._check(rule.min_reserve <= sim.config.fleet_size, "weekly_state.rules_input",
                      "nominal reserve exceeds fleet size")
        common._check(all(mid in sim.missions and rule.window_start <= sim.missions[mid].spec.start_time < rule.window_end
                          for mid in rule.mandatory_ids), "weekly_state.rules_input",
                      "mandatory obligation is outside its offered window")
    announced = state["announced_rule_versions"]
    common._list(announced, "weekly_state.announced_rule_versions")
    common._check(len(announced) == len(set(announced))
                  and set(announced) == {rule.version for rule in rules if rule.announced_at <= sim.now},
                  "weekly_state.announced_rule_versions", "rule announcement state disagrees with time")
    weeks = [float(point) for point in range(0, 720, 168) if point <= sim.now]
    days = [float(point) for point in range(0, 720, 24) if point <= sim.now]
    _points(state["processed_week_points"], weeks, "weekly_state.processed_week_points")
    _points(state["processed_day_points"], days, "weekly_state.processed_day_points")

    cohorts = state["cohorts"]
    common._shape(cohorts, {str(int(point)) for point in weeks}, "weekly_state.cohorts")
    ownership, commitments = {}, {}
    for point in weeks:
        key = str(int(point))
        cohort, path = cohorts[key], f"weekly_state.cohorts[{key}]"
        common._shape(cohort, COHORT_FIELDS, path)
        end = min(point + 168, 720)
        common._number(cohort["window_start"], path + ".window_start")
        common._number(cohort["window_end"], path + ".window_end")
        common._check((cohort["window_start"], cohort["window_end"]) == (point, end),
                      path, "incorrect cohort window")
        offered = _ids(cohort["offered_ids"], path + ".offered_ids", sim.missions)
        accepted = _ids(cohort["accepted_ids"], path + ".accepted_ids", sim.missions)
        rejected = _ids(cohort["rejected_ids"], path + ".rejected_ids", sim.missions)
        _ids(cohort["mandatory_ids"], path + ".mandatory_ids", sim.missions)
        expected = [mid for mid, mission in sim.missions.items() if point <= mission.spec.start_time < end]
        common._check(offered == expected, path + ".offered_ids", "frozen offer set disagrees with window tasks")
        common._check(set(accepted).isdisjoint(rejected) and set(accepted) | set(rejected) == set(offered),
                      path, "accepted/rejected must partition the frozen offered set")
        for field, ids in (("offered_value", offered), ("accepted_value", accepted)):
            common._number(cohort[field], path + "." + field)
            common._check(cohort[field] == sum(sim.missions[mid].spec.value for mid in ids),
                          path + "." + field, "frozen value disagrees with mission values")
        command = WeeklyCommand.from_dict(cohort["command"])
        common._check(command.window_start == point and set(command.accepted_ids) == set(accepted),
                      path + ".command", "command disagrees with frozen commitment")
        active_rule = next(rule for rule in rules if rule.window_start == point)
        mandatory = sorted(active_rule.mandatory_ids,
                           key=lambda mid: (sim.missions[mid].spec.start_time, mid))
        common._check(cohort["mandatory_ids"] == list(active_rule.mandatory_ids)
                      and list(command.mandatory_ids) == mandatory
                      and set(command.missing_mandatory) == set(mandatory) - set(accepted),
                      path + ".mandatory_ids", "mandatory obligations/shortfall disagree with rule")
        from .weekly_policy import plan_usage
        carry = [asdict(sim.missions[mid].spec) for mid, previous in commitments.items()
                 if previous["accepted"] and _status_as_of(sim.missions[mid], point) in {"pending", "running"}]
        usage = plan_usage([*carry, *(asdict(sim.missions[mid].spec) for mid in accepted)],
                           point, end, sim.config.fleet_size)
        common._shape(command.plan_usage, usage, path + ".command.plan_usage")
        for field in ("peak_aircraft", "minimum_reserve"):
            common._integer(command.plan_usage[field], path + ".command.plan_usage." + field,
                            minimum=-sim.config.fleet_size if field == "minimum_reserve" else 0)
            common._check(command.plan_usage[field] == usage[field], path + ".command.plan_usage." + field,
                          "plan capacity disagrees with committed intervals")
        common._number(command.plan_usage["aircraft_hours"], path + ".command.plan_usage.aircraft_hours")
        common._check(command.plan_usage["aircraft_hours"] == usage["aircraft_hours"],
                      path + ".command.plan_usage.aircraft_hours", "plan exposure disagrees with intervals")
        # Reconstruct only the historical public facts needed for deterministic
        # command validation. Later outcomes determine whether an earlier
        # commitment was still carrying at this boundary; they must not replace
        # that as-of status. This is a validation projection, not a replay of the
        # historical policy or a reconstructed physical world.
        validation_observation = PolicyObservation(
            time=point, fleet_size=sim.config.fleet_size, aircraft=[],
            missions=[asdict(sim.missions[mid].spec) for mid in (*ownership, *offered)],
            repairs=[], orders=[], inventory={},
            rules=[rule.to_dict() for rule in rules if rule.announced_at <= point],
            commitments=[])
        context = RuleContext(point, end, sim.config.fleet_size, active_rule, tuple(carry))
        try:
            canonical = validate_command(command, validation_observation, context)
        except ValueError as exc:
            raise ValueError(f"checkpoint.{path}.command: historical command is illegal: {exc}") from exc
        # validate_command derives mandatory/fallback diagnostics. Never accept
        # its normalized result in place of damaged saved data. In particular a
        # carry-only violation is a valid declared fallback, not grounds to
        # retroactively cancel the earlier commitment.
        common._check(command.to_dict() == canonical.to_dict(), path + ".command",
                      "saved command disagrees with deterministic validation/fallback")
        for index, reason in enumerate(command.reasons):
            for field, value in reason.items():
                if field != "code":
                    common._number(value, f"{path}.command.reasons[{index}].{field}",
                                   minimum=-math.inf)
        for mid in offered:
            ownership[mid] = key
            commitments[mid] = {"accepted": mid in accepted, "window_start": point}
            common._check((sim.missions[mid].status == "rejected") == (mid in rejected),
                          f"missions[{mid}].status", "mission disagrees with commitment")
    common._shape(state["mission_cohort"], ownership, "weekly_state.mission_cohort")
    common._check(state["mission_cohort"] == ownership, "weekly_state.mission_cohort", "cohort ownership disagreement")
    common._shape(state["commitments"], commitments, "weekly_state.commitments")
    for mid, commitment in state["commitments"].items():
        path = f"weekly_state.commitments[{mid}]"
        common._shape(commitment, {"accepted", "window_start"}, path)
        common._check(type(commitment["accepted"]) is bool, path + ".accepted", "must be bool")
        common._number(commitment["window_start"], path + ".window_start")
        common._check(commitment == commitments[mid], path, "commitment disagrees with cohort")
    for mid, mission in sim.missions.items():
        common._check(mid in ownership or mission.status == "pending", f"missions[{mid}]",
                      "an unoffered mission cannot have an execution outcome")

    commands = common._list(state["weekly_commands"], "weekly_state.weekly_commands")
    common._check(commands == [cohorts[str(int(point))]["command"] for point in weeks],
                  "weekly_state.weekly_commands", "weekly command history disagrees with cohorts")
    common._list(state["daily_commands"], "weekly_state.daily_commands")
    common._check(len(state["daily_commands"]) == len(days), "weekly_state.daily_commands",
                  "one daily command is required per processed day")
    for index, (command, point) in enumerate(zip(state["daily_commands"], days)):
        expected = {"time": point, "opportunity_index": index, "policy_version": DAILY_POLICY_VERSION,
                    "dispatch": "aircraft-id", "repairs": "request-time-id-fifo",
                    "replenishment": "daily-inventory-position"}
        common._shape(command, expected, f"weekly_state.daily_commands[{index}]")
        common._number(command["time"], f"weekly_state.daily_commands[{index}].time")
        common._integer(command["opportunity_index"], f"weekly_state.daily_commands[{index}].opportunity_index")
        common._check(command == expected, f"weekly_state.daily_commands[{index}]", "invalid fixed daily command")
    generations = state["component_generations"]
    common._shape(generations, sim.aircraft, "weekly_state.component_generations")
    for aid, generation in generations.items():
        common._integer(generation, f"weekly_state.component_generations[{aid}]")
        completed = sum(repair.aircraft_id == aid and repair.status == "completed"
                        for repair in sim.repairs.values())
        common._check(generation == completed, f"weekly_state.component_generations[{aid}]",
                      "component generation disagrees with completed replacements")
    rng = KeyedRNG.from_dict(state.pop("keyed_rng"))
    common._check((rng.seed, rng.scenario_id, rng.domain) ==
                  (sim.config.seed, state["scenario_id"], "execution"), "weekly_state.keyed_rng",
                  "RNG identity disagrees with execution inputs")
    terminal = state["terminal_boundary"]
    if sim.now < 720:
        common._check(terminal is None, "weekly_state.terminal_boundary", "terminal boundary has not occurred")
    else:
        common._shape(terminal, {"time", "event_ids"}, "weekly_state.terminal_boundary")
        common._check(terminal["time"] == 720 and type(terminal["time"]) in (int, float),
                      "weekly_state.terminal_boundary.time", "must equal horizon")
        _ids(terminal["event_ids"], "weekly_state.terminal_boundary.event_ids",
             {row["event_id"] for row in sim.events if row["time"] == 720})
    sim.weekly_state, sim.keyed_rng = state, rng
    sim._decision_runtime = None
    if new_policy:
        common._check(state["decision_protocol_version"] == DECISION_PROTOCOL_VERSION,
                      "weekly_state.decision_protocol_version", "unsupported decision protocol")
        from .decision_resolver import validate_decision_records
        validate_decision_records(sim)
    _validate_manifest(sim)
    _validate_window_metrics(sim)
    _validate_closed_cohorts(sim)


def _validate_manifest(sim):
    """Check saved draws against directly knowable entity/creation contracts.

    This consumes no variates and does not rerun the engine or past policies.
    The saved samples must cover exactly the draws required by this world's
    input mode, replacement generations, started services and placed orders.
    It checks model consistency, not cryptographic origin from a claimed seed.
    """
    entries = {}
    for entry in sim.keyed_rng.manifest:
        key = entry["key"]
        identity = (key["entity_type"], key["entity_id"], key["generation"],
                    key["purpose"], key["draw_index"])
        common._check(identity not in entries, "weekly_state.keyed_rng.manifest",
                      "duplicate semantic sample identity")
        entries[identity] = entry
    required = set()

    def sample(entity_type, entity_id, generation, purpose, distribution):
        identity = (entity_type, entity_id, generation, purpose, 0)
        path = f"weekly_state.keyed_rng.manifest[{identity!r}]"
        common._check(identity in entries, path, "missing required entity sample")
        common._check(identity not in required, path, "entity ledger requests a duplicate sample identity")
        required.add(identity)
        entry = entries[identity]
        common._check(entry["distribution"] == distribution, path + ".distribution",
                      "sample distribution disagrees with configured mechanism")
        value = entry["value"]
        return math.inf if value == common.INFINITY_TAG else value

    config = sim.config
    life_distribution = {"kind": "exponential", "rate": config.failure_rate_per_flight_hour}
    repair_distribution = {"kind": "uniform", "low": config.repair_duration_min_hours,
                           "high": config.repair_duration_max_hours}
    current_life = {}
    generation_start = dict.fromkeys(sim.aircraft, 0.)
    generations = dict.fromkeys(sim.aircraft, 0)
    for index, aid in enumerate(sim.aircraft):
        current_life[aid] = (sample("component", f"{aid}/LRU0", 0, "lifetime", life_distribution)
                             if sim._initial_lifetimes is None else sim._initial_lifetimes[index])
    for repair in sim.repairs.values():
        aid, generation = repair.aircraft_id, generations[repair.aircraft_id]
        if repair.started_at is not None:
            duration = sample("component", f"{aid}/LRU0", generation,
                              "repair_duration", repair_distribution)
            common._check(repair.due_at == repair.started_at + duration,
                          f"repairs[{repair.id}].due_at",
                          "repair due time disagrees with saved entity sample")
        if repair.status == "completed":
            generations[aid] += 1
            current_life[aid] = sample("component", f"{aid}/LRU0", generations[aid],
                                       "lifetime", life_distribution)
            generation_start[aid] = repair.completed_at
    common._check(generations == sim.weekly_state["component_generations"],
                  "weekly_state.component_generations", "manifest generations disagree with replacements")
    for aid, aircraft in sim.aircraft.items():
        # No subtraction/accumulation inference is needed if this generation
        # has never consumed flight exposure. In a failed generation the
        # sampled life has already been consumed, even when its duration is 0.
        exposed = any(aid in mission.assigned_aircraft and mission.actual_start is not None
                      and mission.actual_start >= generation_start[aid]
                      and (mission.actual_end if mission.actual_end is not None else sim.now) > mission.actual_start
                      for mission in sim.missions.values())
        if aircraft.state not in {"waiting_repair", "repairing"} and not exposed:
            common._check(aircraft.remaining_life_hours == current_life[aid],
                          f"aircraft[{aid}].remaining_life_hours",
                          "unconsumed component lifetime disagrees with saved sample/input")

    supply_distribution = {"kind": "uniform", "low": config.lead_time_min_hours,
                           "high": config.lead_time_max_hours}
    for order in sim.orders.values():
        common._check(order.ordered_at % 24 == 0 and 0 <= order.ordered_at < 720,
                      f"orders[{order.id}].ordered_at", "order must use a fixed daily opportunity")
        lead = sample("supplier", "single/LRU0", int(order.ordered_at / 24),
                      "lead_time", supply_distribution)
        common._check(order.expected_arrival == order.ordered_at + lead,
                      f"orders[{order.id}].expected_arrival",
                      "public ETA disagrees with saved opportunity sample")

    if sim._input_missions is None:
        if config.mission_rate_per_hour == 0:
            common._check(not sim.missions, "missions", "zero-rate generated stream must be empty")
        else:
            time = 0.
            arrival_distribution = {"kind": "exponential", "rate": config.mission_rate_per_hour}
            attributes = (
                ("duration", "duration_hours", {"kind": "uniform", "low": config.mission_duration_min_hours,
                                                "high": config.mission_duration_max_hours}),
                ("aircraft_required", "aircraft_required", {"kind": "integer", "low": config.mission_aircraft_min,
                                                             "high": config.mission_aircraft_max}),
                ("value", "value", {"kind": "uniform", "low": config.mission_value_min,
                                      "high": config.mission_value_max}),
            )
            for index, mission in enumerate(sim.missions.values()):
                interval = sample("arrival", str(index), 0, "interval", arrival_distribution)
                next_time = time + interval
                common._check(next_time > time and next_time < 720
                              and mission.spec.id == f"M{index + 1:06d}"
                              and mission.spec.start_time == next_time,
                              f"missions[{mission.spec.id}].spec",
                              "generated mission identity/time disagrees with saved arrival sample")
                time = next_time
                for purpose, field, distribution in attributes:
                    value = sample("arrival", str(index), 0, purpose, distribution)
                    common._check(getattr(mission.spec, field) == value,
                                  f"missions[{mission.spec.id}].spec.{field}",
                                  "generated attribute disagrees with saved sample")
            interval = sample("arrival", str(len(sim.missions)), 0, "interval", arrival_distribution)
            common._check(time + interval >= 720 and time + interval > time,
                          "weekly_state.keyed_rng.manifest.terminal_arrival",
                          "last arrival interval must terminate generation at/beyond 720")
    common._check(set(entries) == required, "weekly_state.keyed_rng.manifest",
                  "unexpected sample not supported by inputs/entities/opportunities")


def _status_as_of(mission, time):
    if mission.status == "rejected":
        return "rejected"
    if mission.actual_start is None:
        return "not_executed" if mission.actual_end is not None and mission.actual_end <= time else "pending"
    if mission.actual_start > time:
        return "pending"
    if mission.actual_end is None or mission.actual_end > time:
        return "running"
    return mission.status


def _validate_window_metrics(sim):
    names = ("flight_hours", "available_aircraft_hours", "idle_aircraft_hours", "spare_wait_hours", "bay_wait_hours")
    windows = sim.weekly_state["window_metrics"]
    common._shape(windows, {str(t) for t in (0, 168, 336, 504, 672)}, "weekly_state.window_metrics")
    for key, totals in windows.items():
        path = f"weekly_state.window_metrics[{key}]"
        common._shape(totals, names, path)
        expected = dict.fromkeys(names, 0.)
        start, end = float(key), min(float(key) + 168, 720)
        for row, following in zip(sim.history, sim.history[1:]):
            common._shape(row, common.HISTORY_FIELDS, "history")
            common._shape(following, common.HISTORY_FIELDS, "history")
            dt = max(0., min(following["time"], end) - max(row["time"], start))
            covered = min(row["on_hand"], row["unmet_demand"])
            expected["flight_hours"] += dt * row["flying"]
            expected["available_aircraft_hours"] += dt * (row["flying"] + row["idle"])
            expected["idle_aircraft_hours"] += dt * row["idle"]
            expected["spare_wait_hours"] += dt * (row["unmet_demand"] - covered)
            expected["bay_wait_hours"] += dt * covered
        for name, value in totals.items():
            common._number(value, path + "." + name)
            common._close(value, expected[name], path + "." + name)


def _validate_closed_cohorts(sim):
    snapshots = common._list(sim.weekly_state["closed_cohort_snapshots"], "weekly_state.closed_cohort_snapshots")
    expected_cohorts = [cohort for cohort in sim.weekly_state["cohorts"].values() if cohort["window_end"] <= sim.now]
    common._check(len(snapshots) == len(expected_cohorts), "weekly_state.closed_cohort_snapshots",
                  "one snapshot is required for every closed cohort")
    for index, (snapshot, cohort) in enumerate(zip(snapshots, expected_cohorts)):
        path = f"weekly_state.closed_cohort_snapshots[{index}]"
        common._shape(snapshot, {"window_start", "as_of", "counts"}, path)
        common._number(snapshot["window_start"], path + ".window_start")
        common._number(snapshot["as_of"], path + ".as_of")
        common._check((snapshot["window_start"], snapshot["as_of"]) ==
                      (cohort["window_start"], cohort["window_end"]), path, "incorrect cohort closing time")
        time = snapshot["as_of"]
        accepted = cohort["accepted_ids"]
        states = {mid: _status_as_of(sim.missions[mid], time) for mid in cohort["offered_ids"]}
        expected = {"offered": len(cohort["offered_ids"]), "accepted": len(accepted),
                    "rejected": len(cohort["rejected_ids"])}
        for field, status in (("success", "success"), ("failed", "failed"),
                              ("accepted_not_executed", "not_executed"),
                              ("pending_execution", "pending"), ("running", "running")):
            expected[field] = sum(states[mid] == status for mid in accepted)
        expected["unfinished"] = expected["pending_execution"] + expected["running"]
        expected["launched"] = expected["success"] + expected["failed"] + expected["running"]
        expected["offered_value"] = cohort["offered_value"]
        expected["accepted_value"] = cohort["accepted_value"]
        expected["earned_value"] = sum(sim.missions[mid].spec.value for mid in accepted if states[mid] == "success")
        expected["mandatory_shortfall_as_of"] = sum(states[mid] != "success" for mid in cohort["mandatory_ids"])
        expected["mandatory_shortfall_terminal"] = expected["mandatory_shortfall_as_of"] if time == 720 else None
        sim._ratios(expected)
        common._check(snapshot["counts"] == expected, path + ".counts", "closed snapshot uses incorrect as-of results")


def log_contract(sim):
    state = sim.weekly_state
    refs = {
        "rule_announced": {rule["version"]: rule for rule in state["rules_input"]},
        "weekly_offer": state["cohorts"], "weekly_command": state["cohorts"],
        "daily_command": {str(command["opportunity_index"]): command for command in state["daily_commands"]},
        "mission_rejected_at_start": sim.missions, "terminal_boundary": {"720": None},
    }
    fields = {"rule_announced": {"rule"}, "weekly_offer": {"offered_ids", "window_end"},
              "weekly_command": {"command"}, "daily_command": {"command"},
              "mission_rejected_at_start": set(), "terminal_boundary": {"time", "event_ids"}}
    return refs, fields, {kind: {("", "")} for kind in refs}


def validate_weekly_log(sim, row, detail, path):
    state, kind, entity, time = sim.weekly_state, row["event_type"], row["entity_id"], row["time"]
    if kind == "rule_announced":
        expected = next(rule for rule in state["rules_input"] if rule["version"] == entity)
        common._check(time == expected["announced_at"] and detail["rule"] == expected,
                      path, "rule announcement disagrees with public rule")
    elif kind in {"weekly_offer", "weekly_command"}:
        cohort = state["cohorts"][entity]
        common._check(time == cohort["window_start"], path + ".time", "incorrect weekly decision time")
        expected = ({"offered_ids": cohort["offered_ids"], "window_end": cohort["window_end"]}
                    if kind == "weekly_offer" else {"command": cohort["command"]})
        common._check(detail == expected, path + ".details", "weekly log disagrees with frozen ledger")
    elif kind == "daily_command":
        command = state["daily_commands"][int(entity)]
        common._check(time == command["time"] and detail["command"] == command,
                      path, "daily log disagrees with command history")
    elif kind == "mission_rejected_at_start":
        mission = sim.missions[entity]
        common._check(mission.status == "rejected" and time == mission.spec.start_time,
                      path, "rejected-start log disagrees with mission")
    else:
        common._check(time == 720 and detail == state["terminal_boundary"], path,
                      "terminal boundary log disagrees with saved boundary")


def validate_weekly_log_totals(sim, counts):
    state = sim.weekly_state
    expected = {"weekly_offer": len(state["processed_week_points"]),
                "weekly_command": len(state["processed_week_points"]),
                "daily_command": len(state["processed_day_points"]),
                "reorder_check": len(state["processed_day_points"]),
                "rule_announced": len(state["announced_rule_versions"]),
                "mission_rejected_at_start": sum(m.status == "rejected" and m.spec.start_time <= sim.now
                                                  for m in sim.missions.values()),
                "terminal_boundary": int(sim.now == 720)}
    for kind, total in expected.items():
        rows = [row for row in sim.events if row["event_type"] == kind]
        common._check(counts.get(kind, 0) == total, "events." + kind, "weekly ledger count disagreement")
        identity = [(row["event_type"], row["time"], row["entity_id"]) for row in rows]
        common._check(len(identity) == len(set(identity)), "events." + kind, "duplicate weekly action")
    checks = [row for row in sim.events if row["event_type"] == "reorder_check"]
    common._check([row["time"] for row in checks] == state["processed_day_points"],
                  "events.reorder_check", "one inventory review required per daily opportunity")
    order_times = [order.ordered_at for order in sim.orders.values()]
    common._check(len(order_times) == len(set(order_times)) and set(order_times) <= set(state["processed_day_points"]),
                  "orders", "orders must use unique daily opportunities")
    if sim.now == 720:
        expected_ids = [row["event_id"] for row in sim.events
                        if row["time"] == 720 and row["event_type"] != "terminal_boundary"]
        common._check(state["terminal_boundary"]["event_ids"] == expected_ids,
                      "weekly_state.terminal_boundary.event_ids", "terminal physical outcomes must be recorded once")


def to_checkpoint(sim):
    return common.to_checkpoint(sim, profile="weekly")


def from_checkpoint(data, cls=None):
    if cls is None:
        from .weekly import WeeklySimulation
        cls = WeeklySimulation
    return common.from_checkpoint(data, cls, profile="weekly")


def save_checkpoint(sim, path):
    value = json.dumps(to_checkpoint(sim), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def load_checkpoint(path, cls=None):
    try:
        data = common._loads(Path(path).read_text(encoding="utf-8-sig"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"checkpoint file {path}: {exc}") from exc
    return from_checkpoint(data, cls)
