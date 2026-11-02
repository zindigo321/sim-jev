"""Paired synthetic phase-2C pipeline pilot and frozen-state comparison.

Default Jev rows are explicitly artificial stub plumbing tests. This CLI never
sends live HTTP; bounded live smoke has its own explicit opt-in entry point.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
import csv
import json
import math
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev import Config, MissionSpec
from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionPolicySpec, DecisionResult
from sim_jev.decision_resolver import DecisionRuntime, select_from_pool
from sim_jev.jev import ChoiceAdapter, JevConfig, build_choice_request
from sim_jev.provenance import canonical_sha256, code_fingerprint, git_state
from sim_jev.reporting import check_output_directory
from sim_jev.weekly import WEEK_POINTS, WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec, choose_command
from sim_jev.weekly_reporting import write_weekly_run
from scripts.planning_verify import audit_planning_branch
from scripts.weekly_verify import audit_business_log

METHODS = ("mandatory-first", "random-des", "heuristic-des", "jev-direct", "jev-des", "direct-des")
SCENARIOS = ("balanced", "scarce")
EXPERIMENT_VERSION = "phase2c-paired-pipeline-v1"


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                          encoding="utf-8")


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                             if isinstance(value, (list, dict)) else value for key, value in row.items()})


def case_definition(name):
    if name not in SCENARIOS:
        raise ValueError("Unknown frozen phase-2C scenario")
    definition = json.loads((ROOT / "configs" / f"phase2c_{name}.json").read_text(encoding="utf-8"))
    if definition["schema_version"] != "phase2c-paired-case-v1":
        raise ValueError("Unsupported paired scenario schema")
    return definition


def build_case(name, seed, policy, runtime=None):
    """Exact paired offers/rules; environment seed enters execution physics only."""
    definition = case_definition(name)
    config = Config(**{**definition["config"], "seed": seed})
    missions, rules = [], []
    for index, start in enumerate(WEEK_POINTS):
        end = min(start + 168, 720)
        for template in definition["mission_templates"]:
            at = (start + template["offset"] if "offset" in template
                  else end + template["offset_from_end"])
            if not start <= at < end:
                raise ValueError("Frozen mission template is outside its offer window")
            missions.append(MissionSpec(f"W{index}_{template['label']}", at,
                                        template["duration_hours"], template["aircraft_required"],
                                        template["value"]))
        rule = definition["rule"]
        rules.append(RuleSpec(start, end, 0 if index == 1 else start, start,
                              f"{definition['scenario_id']}-rule-{int(start)}",
                              tuple(f"W{index}_{label}" for label in rule["mandatory_labels"]),
                              rule["min_reserve"],
                              rule["final_window_afh"] if end == 720 else rule["full_window_afh"]))
    return WeeklySimulation(config, missions, scenario_id=definition["scenario_id"],
                            rules=rules, policy=policy, decision_runtime=runtime)


def policy_for(method, *, mode="stub", planning_seed=20261102, target_size=16, top_k=4, scenarios=8,
               strict_jev=False):
    if method not in METHODS:
        raise ValueError("Unknown pilot method")
    if method == "mandatory-first":
        return PolicySpec()
    return DecisionPolicySpec(method=method, planning_seed=planning_seed, target_size=target_size,
                              top_k=top_k, scenarios_per_candidate=scenarios, jev_mode=mode,
                              name=method, strict_jev=strict_jev)


def audited_runtime(mode, cache_dir=None, *, adapter=None):
    if adapter is None and mode not in {"stub", "real_replay"}:
        raise ValueError("Pilot accepts offline stub or exact real replay only; use explicit live smoke separately")
    if adapter is None:
        adapter = ChoiceAdapter(JevConfig(mode=mode, cache_dir=str(cache_dir) if cache_dir else None,
                                          max_http_requests=0))
    if adapter.config.mode != mode:
        raise ValueError("Shared adapter mode differs from experiment mode")
    counter = {"verified_branches": 0}

    def audit(result, request):
        checked = audit_planning_branch(result, request)
        counter["verified_branches"] += 1
        return checked

    return DecisionRuntime(jev_adapter=adapter, branch_auditor=audit), counter


def decision_rows(sim, name, seed, method):
    by_window = {record["command"]["window_start"]: record
                 for record in sim.weekly_state.get("decision_records", [])}
    cohorts = {row["window_start"]: row for row in sim.summary()["cohorts"]}
    rows = []
    for command in sim.weekly_state["weekly_commands"]:
        record = by_window.get(command["window_start"], {})
        budget, jev = record.get("budget", {}), record.get("jev") or {}
        cohort = cohorts[command["window_start"]]
        selected = [branch["score"] for branch in record.get("branches", [])
                    if branch["candidate_id"] == record.get("selected_candidate_id")]
        row = {"scenario": name, "seed": seed, "method": method,
               "window_start": command["window_start"], "window_end": cohort["window_end"],
               "request_id": record.get("request", {}).get("request_id"),
               "pool_sha256": record.get("pool_sha256"), "actual_M": budget.get("actual_M", 0),
               "ranking_ids": record.get("ranking_ids", []),
               "selected_candidate_id": record.get("selected_candidate_id"),
               "accepted_ids": command["accepted_ids"], "nominal_fallback": command["fallback"],
               "missing_mandatory": command["missing_mandatory"],
               "planned_afh": command["plan_usage"]["aircraft_hours"],
               "actual_des": budget.get("actual_des", 0),
               "simulated_hours": budget.get("simulated_hours", 0),
               "selected_branch_mean": math.fsum(selected) / len(selected) if selected else None,
               "jev_mode": jev.get("mode"), "valid_jev": jev.get("valid_jev", False),
               "valid_response": jev.get("valid_response", False),
               "heuristic_fallback": record.get("fallback", False),
               "api_skipped_reason": jev.get("api_skipped_reason"),
               "actual_http_requests": jev.get("actual_http_requests", 0),
               "actual_usage": jev.get("actual_usage", {"input_tokens": 0, "output_tokens": 0})}
        row.update({f"actual_{key}": cohort[key] for key in (
            "offered", "accepted", "success", "failed", "accepted_not_executed", "unfinished",
            "offered_value", "earned_value", "DSR", "RCR", "ASR", "mandatory_shortfall_terminal")})
        rows.append(row)
    return rows


def run_case(name, seed, method, folder, *, mode="stub", cache_dir=None,
             planning_seed=20261102, target_size=16, top_k=4, scenarios=8,
             runtime=None, counter=None, strict_jev=False, phase=None, save_resume=False):
    start = time.perf_counter()
    folder = check_output_directory(folder)
    if runtime is None:
        runtime, counter = audited_runtime(mode, cache_dir)
    if counter is None:
        raise ValueError("An injected runtime requires its branch audit counter")
    audit_start = len(runtime.jev_adapter.audit_log)
    branch_start = counter["verified_branches"]
    if phase is not None:
        runtime.jev_adapter.set_context(phase=phase, run=f"{name}/seed-{seed}/{method}",
                                       scenario=name, seed=seed, method=method)
    policy = policy_for(method, mode=mode, planning_seed=planning_seed, target_size=target_size,
                        top_k=top_k, scenarios=scenarios, strict_jev=strict_jev)
    sim = build_case(name, seed, policy, runtime)
    exogenous = {"scenario_id": sim.weekly_state["scenario_id"],
                 "missions": [asdict(item) for item in sim._input_missions],
                 "rules": sim.weekly_state["rules_input"]}
    paired_hash = canonical_sha256(exogenous)
    resume_point = None
    try:
        if save_resume:
            sim.run(0)
            resume_point = sim.to_checkpoint()
        sim.run()
    except Exception as error:
        if phase is not None:
            folder.mkdir(parents=True, exist_ok=True)
            write_json(folder / "failed-run.json", {"phase": phase, "scenario": name, "seed": seed,
                "method": method, "status": "failed", "error_code": getattr(error, "code", type(error).__name__),
                "paired_task_rule_sha256": paired_hash, "committed_weekly_commands":
                sim.weekly_state["weekly_commands"], "completed_decisions":
                sim.weekly_state.get("decision_records", []), "strict_jev": strict_jev})
            write_json(folder / "api-audit.json", runtime.jev_adapter.audit_log[audit_start:])
        raise
    accounting = audit_business_log(sim)
    write_weekly_run(sim, folder, run_context={"experiment_version": EXPERIMENT_VERSION,
                     "evidence_role": "pipeline_only_stub" if method.startswith("jev") and mode == "stub"
                                      else "small_synthetic_pipeline_pilot",
                     "paired_task_rule_sha256": paired_hash, "method": method, "jev_mode": mode})
    write_json(folder / "exogenous-inputs.json", exogenous)
    write_json(folder / "independent-business-audit.json", accounting)
    decisions = decision_rows(sim, name, seed, method)
    records = sim.weekly_state.get("decision_records", [])
    write_json(folder / "decisions.json", {"records": records, "summary": decisions,
                 "reference_commands": sim.weekly_state["weekly_commands"] if not records else []})
    write_csv(folder / "decisions.csv", decisions)
    write_json(folder / "api-audit.json", runtime.jev_adapter.audit_log[audit_start:])
    if resume_point is not None:
        write_json(folder / "resume-point.json", resume_point)
    metrics = sim.summary()
    api = [record["jev"] for record in records if record.get("jev")]
    eligible = sum(not item.get("api_skipped_reason") for item in api)
    valid = sum(bool(item.get("valid_jev")) for item in api)
    row = {"scenario": name, "seed": seed, "method": method, "jev_mode": mode if method.startswith("jev") else None,
           "evidence_role": "pipeline_only_stub" if method.startswith("jev") and mode == "stub"
                            else "small_synthetic_pipeline_pilot",
           "paired_task_rule_sha256": paired_hash,
           **{key: metrics[key] for key in ("DSR", "RCR", "ASR", "offered", "accepted", "success",
                                            "offered_value", "earned_value", "mandatory_shortfall_terminal",
                                            "availability", "spare_wait_hours", "bay_wait_hours",
                                            "available_aircraft_hours", "on_hand", "in_transit")},
           "actual_aircraft_hours": metrics["flight_hours"],
           "fleet_horizon_hours": sim.config.fleet_size * metrics["elapsed_hours"],
           "total_wait_hours": metrics["spare_wait_hours"] + metrics["bay_wait_hours"],
           "decision_count": len(decisions), "jev_eligible_requests": eligible,
           "valid_jev_decisions": valid, "api_valid_rate": valid / eligible if eligible else None,
           "valid_response_count": sum(bool(item.get("valid_response")) for item in api),
           "api_skipped_count": sum(bool(item.get("api_skipped_reason")) for item in api),
           "heuristic_fallback_count": sum(bool(record.get("fallback")) for record in records),
           "nominal_fallback_count": sum(bool(command["fallback"]) for command in sim.weekly_state["weekly_commands"]),
           "api_error_count": sum(bool(item.get("error_code")) for item in api),
           "actual_http_requests": sum(item.get("actual_http_requests", 0) for item in api),
           "actual_input_tokens": sum(item.get("actual_usage", {}).get("input_tokens", 0) for item in api),
           "actual_output_tokens": sum(item.get("actual_usage", {}).get("output_tokens", 0) for item in api),
           "recorded_response_input_tokens": sum(item.get("usage", {}).get("input_tokens", 0) for item in api),
           "cost_estimate_usd": math.fsum(item.get("cost_estimate_usd", 0) for item in api),
           "actual_des": sum(row["actual_des"] for row in decisions),
           "simulated_hours": math.fsum(row["simulated_hours"] for row in decisions),
           "independent_branch_audits": counter["verified_branches"] - branch_start,
           "end_to_end_seconds": time.perf_counter() - start, "output": str(folder)}
    if row["independent_branch_audits"] != row["actual_des"]:
        raise AssertionError("Every DES branch must pass independent origin-aware accounting")
    write_json(folder / "pilot-result.json", row)
    return row, decisions


def aggregate_rows(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["scenario"], row["method"]].append(row)
    result = []
    for (scenario, method), group in sorted(grouped.items()):
        totals = {key: math.fsum(row[key] for row in group) for key in (
            "offered", "accepted", "success", "offered_value", "earned_value",
            "mandatory_shortfall_terminal", "actual_aircraft_hours", "available_aircraft_hours",
            "fleet_horizon_hours", "total_wait_hours", "actual_des", "simulated_hours",
            "actual_http_requests", "valid_jev_decisions", "jev_eligible_requests",
            "heuristic_fallback_count", "actual_input_tokens", "actual_output_tokens", "end_to_end_seconds")}
        ratios = {key: totals[num] / totals[den] if totals[den] else None for key, num, den in (
            ("DSR", "success", "offered"), ("RCR", "earned_value", "offered_value"),
            ("ASR", "success", "accepted"), ("availability", "available_aircraft_hours", "fleet_horizon_hours"),
            ("api_valid_rate", "valid_jev_decisions", "jev_eligible_requests"))}
        result.append({"scenario": scenario, "method": method, "runs": len(group),
                       "seeds": [row["seed"] for row in group], "evidence_role": group[0]["evidence_role"],
                       "ratio_basis": "sum of full-horizon numerators / sum of full-horizon denominators",
                       **totals, **ratios})
    return result


def fixed_state_request():
    """One untuned public state contains all four required difficult conditions."""
    config = Config(fleet_size=4, mission_rate_per_hour=0, failure_rate_per_flight_hour=.02,
                    repair_bays=1, initial_spares=1, reorder_target=1,
                    repair_duration_min_hours=48, repair_duration_max_hours=48,
                    lead_time_min_hours=48, lead_time_max_hours=48)
    missions = [MissionSpec("FA", 143, 10, 1, 1), MissionSpec("FB", 145, 10, 1, 1),
                MissionSpec("CARRY", 167, 6, 1, 999), MissionSpec("MAND", 170, 8, 2, 10),
                MissionSpec("O1", 180, 10, 2, 30), MissionSpec("O2", 182, 6, 1, 20),
                MissionSpec("O3", 185, 8, 2, 35), MissionSpec("O4", 205, 5, 1, 25),
                MissionSpec("O5", 207, 7, 2, 40), MissionSpec("O6", 331, 10, 1, 30),
                MissionSpec("FUT", 400, 1, 1, 10)]
    sim = WeeklySimulation(config, missions, [1, 1, math.inf, math.inf], scenario_id="fixed-public-stress-v1",
                           rules=[RuleSpec(168, 336, 168, 168, "fixed-mandatory-edge", ("MAND",), 1, 55),
                                  RuleSpec(336, 504, 0, 336, "known-future-id", ("FUT",))])
    requests = []

    def capture(request, policy, runtime=None):
        requests.append(request)
        return DecisionResult(choose_command(request.observation, request.context, policy))

    with patch("sim_jev.weekly.resolve_decision", side_effect=capture):
        sim.run(168)
    request = requests[-1]
    states = {aircraft["state"] for aircraft in request.observation.aircraft}
    if not {"repairing", "waiting_repair", "flying"} <= states or request.observation.inventory["on_hand"] != 0:
        raise AssertionError("Fixed comparison no longer contains the required public resource conditions")
    return request


def fixed_comparison(folder, *, mode="stub", cache_dir=None, planning_seed=20261102,
                     target_size=16, top_k=4, scenarios=8, runtime=None, counter=None,
                     strict_jev=False, phase=None):
    folder.mkdir(parents=True, exist_ok=False)
    request = fixed_state_request()
    pool = generate_candidates(request, target_size, planning_seed)
    frozen = [item.to_dict() for item in pool]
    if runtime is None:
        runtime, counter = audited_runtime(mode, cache_dir)
    if counter is None:
        raise ValueError("An injected runtime requires its branch audit counter")
    branch_start = counter["verified_branches"]
    records = []
    write_json(folder / "request.json", request.to_dict())
    write_json(folder / "candidate-pool.json", frozen)
    for method in METHODS[1:]:
        if phase is not None:
            runtime.jev_adapter.set_context(phase=phase, run=f"fixed-state/{method}",
                                           scenario="fixed-public-stress-v1", method=method)
        policy = policy_for(method, mode=mode, planning_seed=planning_seed, target_size=target_size,
                            top_k=top_k, scenarios=scenarios, strict_jev=strict_jev)
        result = select_from_pool(request, policy, pool, runtime)
        write_json(folder / f"{method}.json", result.record)
        records.append(result.record)
        if [item.to_dict() for item in pool] != frozen:
            raise AssertionError("Frozen comparison pool was mutated")
    client_order_stable = build_choice_request(request, pool) == build_choice_request(request, list(reversed(pool)))
    summary = {"fixture_features": ["active repair", "cross-window occupancy", "stock shortage with waiting repair",
                                     "mandatory plus carry exactly fills nominal reserve boundary"],
               "request_id": request.request_id, "actual_M": len(pool),
               "common_pool_sha256": canonical_sha256(frozen),
               "reference_command": choose_command(request.observation, request.context, PolicySpec()).to_dict(),
               "reference_des_budget": 0,
               "actual_des": sum(record["budget"]["actual_des"] for record in records),
               "simulated_hours": math.fsum(record["budget"]["simulated_hours"] for record in records),
               "actual_http_requests": sum((record.get("jev") or {}).get("actual_http_requests", 0)
                                           for record in records),
               "independent_branch_audits": counter["verified_branches"] - branch_start,
               "presentation_sensitivity": {"client_reversed_input_payload_identical": client_order_stable,
                   "presentation": "stable candidate ID order", "real_model_sensitivity": "not tested by this offline CLI"},
               "interpretation": "behavior and pipeline only; no high-budget quality labels or Regret@K",
               "jev_evidence": "artificial stub, excluded from Jev effectiveness evidence" if mode == "stub" else "exact live-response replay or explicit heuristic fallback",
               "methods": [{"method": record["method"], "ranking_ids": record["ranking_ids"],
                             "selected_candidate_id": record["selected_candidate_id"],
                             "budget": record["budget"], "fallback": record["fallback"]} for record in records]}
    if summary["actual_des"] != summary["independent_branch_audits"]:
        raise AssertionError("Fixed-state branch audit budget mismatch")
    if phase is not None:
        summary["strict_jev"] = strict_jev
        summary["jev_evidence"] = ("injected fixture; excluded from real Jev evidence"
            if runtime.jev_adapter._transport_is_fixture else "strict captured live responses or exact real replay")
        summary["presentation_sensitivity"]["real_model_sensitivity"] = "not tested"
    write_json(folder / "comparison.json", summary)
    return summary


def run_pilot(destination, *, seeds=tuple(range(5)), scenarios=SCENARIOS, methods=METHODS,
              mode="stub", cache_dir=None, planning_seed=20261102,
              target_size=16, top_k=4, branch_scenarios=8, include_fixed=True,
              runtime=None, counter=None, strict_jev=False, phase=None, save_resume=False):
    destination = check_output_directory(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source = code_fingerprint(ROOT)
    start = time.perf_counter()
    record = {"version": EXPERIMENT_VERSION, "status": "running", "mode": mode,
              "command": list(getattr(sys, "orig_argv", [sys.executable, *sys.argv])),
              "python_executable": sys.executable, "seeds": list(seeds), "scenarios": list(scenarios),
              "methods": list(methods), "configured_budget": {"M": target_size, "K": top_k, "B": branch_scenarios},
              "planning_seed": planning_seed, "source_before": source, "git": git_state(ROOT),
              "rows": [], "engine_offline_verified": False, "jev_live_smoke_verified": False,
              "jev_live_pilot_completed": False,
              "limitations": ["synthetic pipeline size only, no significance or generalization claim",
                  "no PHM, PPO, learned terminal value, paper cost, or J720",
                  "stub never counts as scientific Jev evidence"]}
    write_json(destination / "experiment.json", record)
    try:
        rows, decisions, pairings = [], [], {}
        for scenario in scenarios:
            for seed in seeds:
                for method in methods:
                    print(f"Running {scenario} seed={seed} method={method} mode={mode}", flush=True)
                    row, detail = run_case(scenario, seed, method, destination / scenario / f"seed-{seed}" / method,
                                          mode=mode, cache_dir=cache_dir, planning_seed=planning_seed,
                                          target_size=target_size, top_k=top_k, scenarios=branch_scenarios,
                                          runtime=runtime, counter=counter, strict_jev=strict_jev,
                                          phase=phase, save_resume=save_resume)
                    key = scenario, seed
                    previous = pairings.setdefault(key, row["paired_task_rule_sha256"])
                    if previous != row["paired_task_rule_sha256"]:
                        raise AssertionError("Methods did not receive identical paired task/rule inputs")
                    rows.append(row)
                    decisions.extend(detail)
                    write_csv(destination / "per-seed.csv", rows)
                    write_csv(destination / "per-decision.csv", decisions)
                    if phase is not None:
                        record.update(rows=rows, run_count=len(rows), decision_count=len(decisions))
                        write_json(destination / "experiment.json", record)
        if include_fixed:
            record["fixed_comparison"] = fixed_comparison(destination / "fixed-state", mode=mode,
                cache_dir=cache_dir, planning_seed=planning_seed, target_size=target_size, top_k=top_k,
                scenarios=branch_scenarios)
        aggregate = aggregate_rows(rows)
        write_json(destination / "aggregate.json", aggregate)
        write_csv(destination / "aggregate.csv", aggregate)
        record.update(rows=rows, run_count=len(rows), decision_count=len(decisions),
                      paired_exogenous_inputs_verified=True, source_after=code_fingerprint(ROOT),
                      end_to_end_seconds=time.perf_counter() - start,
                      closed_loop_des=sum(row["actual_des"] for row in rows),
                      closed_loop_simulated_hours=math.fsum(row["simulated_hours"] for row in rows),
                      closed_loop_http_requests=sum(row["actual_http_requests"] for row in rows))
        fixed = record.get("fixed_comparison", {})
        record.update(actual_des=record["closed_loop_des"] + fixed.get("actual_des", 0),
                      simulated_hours=record["closed_loop_simulated_hours"] + fixed.get("simulated_hours", 0),
                      actual_http_requests=record["closed_loop_http_requests"] + fixed.get("actual_http_requests", 0))
        record["source_stable"] = record["source_before"] == record["source_after"]
        if not record["source_stable"]:
            raise AssertionError("Source changed during pilot; keep this development evidence and rerun after freezing")
        record.update(status="passed", exit_code=0, engine_offline_verified=True)
    except Exception as error:
        record.update(status="failed", exit_code=1,
                      error=getattr(error, "code", type(error).__name__) if phase else str(error))
        write_json(destination / "experiment.json", record)
        raise
    write_json(destination / "experiment.json", record)
    return record


def main():
    if sys.flags.optimize:
        print("Pilot acceptance refuses optimized Python", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(range(5)))
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=list(METHODS))
    parser.add_argument("--jev-mode", choices=["stub", "real_replay"], default="stub")
    parser.add_argument("--response-cache", type=Path)
    parser.add_argument("--planning-seed", type=int, default=20261102)
    parser.add_argument("--target-size", type=int, default=16)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--branch-scenarios", type=int, default=8)
    parser.add_argument("--skip-fixed-state", action="store_true")
    args = parser.parse_args()
    if any(seed < 0 for seed in args.seeds) or len(args.seeds) != len(set(args.seeds)):
        parser.error("Seeds must be distinct nonnegative integers")
    if args.jev_mode == "real_replay" and args.response_cache is None:
        parser.error("real_replay requires --response-cache; misses are explicit heuristic fallbacks")
    try:
        result = run_pilot(args.output, seeds=args.seeds, scenarios=args.scenarios, methods=args.methods,
                           mode=args.jev_mode, cache_dir=args.response_cache, planning_seed=args.planning_seed,
                           target_size=args.target_size, top_k=args.top_k, branch_scenarios=args.branch_scenarios,
                           include_fixed=not args.skip_fixed_state)
    except (ValueError, AssertionError, RuntimeError, OSError) as error:
        print(f"Phase-2C pilot failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({key: result[key] for key in ("status", "run_count", "decision_count", "actual_des",
                                                  "actual_http_requests", "engine_offline_verified")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
