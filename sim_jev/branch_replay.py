"""Explicit offline physical replay of persisted decision-branch evidence.

This is a post-decision consistency check. It never changes the candidate pool,
ranking, scores, committed command or decision-time Monte Carlo budget. A hash
and a deterministic replay are consistency evidence, not a source signature.
"""
from copy import deepcopy
import json
from pathlib import Path
import time

from .candidates import _candidate, _offered
from .decision import DecisionRequest
from .decision_audit import validate_metrics
from .planning import PlanningInput, PlanningScenario
from .provenance import canonical_sha256
from .weekly_policy import WeeklyCommand, validate_command


BRANCH_REPLAY_VERSION = "legal-input-physical-branch-replay-v1"
SAMPLING_RULE = "first-recorded-branch-of-every-decision-v1"


def _require(condition, message):
    if not condition:
        raise ValueError("branch replay: " + message)


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def verify_branch_artifact(row, artifact, request):
    """Bind the full physical artifact to the persisted summary and legal request."""
    from scripts.planning_verify import audit_planning_branch

    request = request if isinstance(request, DecisionRequest) else DecisionRequest.from_dict(request)
    _require(type(row) is dict and type(artifact) is dict, "branch/artifact must be objects")
    expected_fields = (set(artifact) - {"final_state"}) | {"candidate_id", "physical_artifact_sha256"}
    _require(set(row) == expected_fields, "persisted branch schema differs")
    _require(row["physical_artifact_sha256"] == canonical_sha256(artifact["final_state"]),
             "physical artifact digest mismatch")
    for key in artifact.keys() - {"final_state"}:
        _require(canonical_sha256(row[key]) == canonical_sha256(artifact[key]),
                 "persisted branch summary differs: " + key)
    capacity = (request.context.window_end - request.context.window_start) * request.parameters.fleet_size
    validate_metrics(row["metrics_delta"], capacity_hours=capacity, path="replayed.metrics_delta")
    validate_metrics(artifact["final_state"]["metrics"], path="replayed.final.metrics")
    try:
        accounting = audit_planning_branch(artifact, request)
    except (AssertionError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("branch replay: independent physical accounting failed") from exc
    return accounting


def replay_decision_branches(record, *, selection="all", artifact_dir=None):
    """Rebuild all branches or the prespecified first branch of one decision.

    Only legal request/scenario seed/index/command enter the new planning world.
    Recorded hidden scenario state is compared afterwards, never used as input.
    ``artifact_dir`` must be a new directory; complete replay artifacts are saved
    there for independent re-audit. No HTTP or policy selection is performed.
    """
    if selection not in {"all", "first-per-decision"}:
        raise ValueError("branch replay: unknown selection rule")
    started = time.perf_counter()
    record = deepcopy(record)
    request = DecisionRequest.from_dict(record["request"])
    pool = record["pool"]
    _require(canonical_sha256(pool) == record["pool_sha256"], "candidate pool digest mismatch")
    candidates = {}
    for candidate in pool:
        command = WeeklyCommand.from_dict(candidate["command"])
        _require(validate_command(command, request.observation, request.context) == command,
                 "candidate command is not canonical")
        _require(_candidate(request, command, _offered(request)).to_dict() == candidate,
                 "candidate identity/summary mismatch")
        _require(candidate["id"] not in candidates, "duplicate candidate identity")
        candidates[candidate["id"]] = candidate
    rows = record["branches"]
    selected = list(enumerate(rows if selection == "all" else rows[:1]))
    scenario_records = {row["scenario_index"]: row for row in record["scenarios"]}
    _require(len(scenario_records) == len(record["scenarios"]), "duplicate scenario index")
    seeds = {row["planning_seed"] for row in scenario_records.values()}
    _require(len(seeds) <= 1, "planning seed differs across recorded scenarios")
    output = Path(artifact_dir) if artifact_dir is not None else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=False)
    reports, worlds = [], {}
    for index, row in selected:
        candidate = candidates.get(row["candidate_id"])
        _require(candidate is not None and row["command"] == candidate["command"],
                 "branch does not belong to recorded candidate")
        scenario = scenario_records.get(row["scenario_index"])
        _require(scenario is not None, "branch scenario missing")
        key = row["scenario_index"]
        if key not in worlds:
            worlds[key] = PlanningScenario(PlanningInput(request, scenario["planning_seed"], key))
            _require(canonical_sha256(worlds[key].to_dict()) == canonical_sha256(scenario),
                     "reconstructed scenario differs from persisted origin")
        artifact = worlds[key].evaluate(WeeklyCommand.from_dict(row["command"]))
        verify_branch_artifact(row, artifact, request)
        report = {"branch_index": index, "candidate_id": row["candidate_id"],
                  "scenario_id": row["scenario_id"], "scenario_index": row["scenario_index"],
                  "planning_seed": scenario["planning_seed"], "duration_hours": artifact["duration_hours"],
                  "physical_artifact_sha256": canonical_sha256(artifact["final_state"]),
                  "summary_verified": True, "independent_physical_accounting_verified": True}
        if output is not None:
            name = f"branch-{index:04d}.json"
            _write_json(output / name, artifact)
            report["artifact"] = name
        reports.append(report)
    result = {"version": BRANCH_REPLAY_VERSION, "status": "passed",
              "request_id": request.request_id, "legal_input_hash": canonical_sha256(request.to_dict()),
              "method": record["method"], "selection": selection,
              "sampling_rule": "all-recorded-branches" if selection == "all" else SAMPLING_RULE,
              "recorded_branch_count": len(rows), "audit_replay_des": len(reports),
              "audit_replay_simulated_hours": sum(row["duration_hours"] for row in reports),
              "audit_replay_wall_seconds": time.perf_counter() - started,
              "actual_http_requests": 0, "decision_des_added": 0,
              "affects_selection": False, "external_source_authentication": False,
              "branches": reports}
    if output is not None:
        _write_json(output / "replay-report.json", result)
    return result
