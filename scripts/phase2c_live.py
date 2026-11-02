"""Strict bounded Jev acceptance, exact response replay, and cache-only recovery.

The live CLI freezes the original M16/K4/B8 cases and exposes one persistent
HTTP budget per stage. Fixture transports remain fixture evidence throughout.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.phase2c_experiment import (METHODS, SCENARIOS, aggregate_rows, audited_runtime,
    fixed_comparison, run_pilot, write_csv, write_json)
from scripts.weekly_verify import audit_business_log, compare_values
from sim_jev.branch_replay import replay_decision_branches
from sim_jev.decision_resolver import logical_decision_record
from sim_jev.jev import (ChoiceAdapter, JevConfig, JevError, MODEL, TEMPLATE_VERSION,
                         validate_response)
from sim_jev.provenance import canonical_sha256, code_fingerprint
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_reporting import write_weekly_run

VERSION = "phase2c-strict-live-acceptance-v1"
STAGE_LIMITS = {"fixed-state": (3, 120.), "pilot": (24, 1800.)}
IDENTITY_FIELDS = ("request_sha256", "model", "template_version", "template_sha256",
                   "candidate_mapping_sha256")
IO_PROJECTION = {
    "version": "phase2c-live-replay-logic-v1",
    "separated_fields": ["decision.execution_audit", "Jev HTTP/timing/cache/attempt/usage accounting",
                         "decision.jev.mode", "weekly_state.policy_spec.jev_mode"],
    "retained": ["full physical checkpoint and configuration", "planning seed", "candidate rules and pool",
                 "method and strict policy", "legal request and response content including original usage",
                 "command", "all branch results and hashes", "events", "cohort obligations and integrals"],
    "note": "Original artifacts are retained unchanged. I/O facts are compared separately, never rewritten.",
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _new_directory(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=False)
    return path


def _record_projection(record):
    value = logical_decision_record(record)
    if value.get("jev") is not None:
        value["jev"].pop("mode", None)
    return value


def checkpoint_projection(checkpoint):
    value = deepcopy(checkpoint)
    state = value["weekly_state"]
    if state["policy_spec"].get("kind") == "candidate-des":
        state["policy_spec"].pop("jev_mode", None)
        state["decision_records"] = [_record_projection(row) for row in state["decision_records"]]
    return value


def compare_checkpoints(expected, actual):
    differences = compare_values(checkpoint_projection(expected), checkpoint_projection(actual), "checkpoint")
    return {"logical_checkpoint_equal": True, "accumulation_roundoff": differences,
            "projection": IO_PROJECTION}


def response_evidence(adapter):
    """Extract only validated request/response and provenance, not a cache tree."""
    records = {}
    for audit in adapter.audit_log:
        if not audit.get("valid_response") or audit.get("api_skipped_reason"):
            continue
        identity = {key: audit[key] for key in IDENTITY_FIELDS}
        key = identity["request_sha256"]
        row = {"source_mode": "live" if audit.get("valid_jev") else "transport_fixture",
               "identity": identity, "request": audit["request"], "response": audit["response"],
               "live_origin": audit.get("live_origin")}
        if key in records and records[key] != row:
            raise ValueError("One request has conflicting captured response provenance")
        records[key] = row
    return {"version": VERSION, "model": MODEL, "template_version": TEMPLATE_VERSION,
            "records": list(records.values()), "http_budget": deepcopy(adapter.ledger),
            "evidence_kind": "fixture" if adapter._transport_is_fixture else "captured_live",
            "authentication_claim": "Internal consistency evidence, not an external origin signature"}


def import_response_evidence(evidence, destination):
    """Rebuild a disposable real-replay store from explicit sanitized evidence."""
    destination = _new_directory(destination)
    if evidence.get("version") != VERSION or evidence.get("evidence_kind") != "captured_live":
        raise ValueError("Real replay requires captured live evidence; fixtures are excluded")
    seen = set()
    ledger = evidence["http_budget"]
    attempts = {row["attempt_id"]: row for row in ledger["attempts"]}
    if len(attempts) != len(ledger["attempts"]):
        raise ValueError("Duplicate attempt ID in captured budget")
    for row in evidence["records"]:
        if row.get("source_mode") != "live":
            raise ValueError("Fixture evidence cannot become a live cache")
        payload, identity = row["request"], row["identity"]
        question = payload["questions"]["portfolio"]
        expected = {"request_sha256": canonical_sha256(payload), "model": MODEL,
            "template_version": TEMPLATE_VERSION, "template_sha256": canonical_sha256(question["instructions"]),
            "candidate_mapping_sha256": canonical_sha256(question["criteria"])}
        if identity != expected or identity["request_sha256"] in seen:
            raise ValueError("Captured request identity mismatch or duplicate")
        validate_response(row["response"], question["criteria"])
        origin = row.get("live_origin") or {}
        attempt = attempts.get(origin.get("attempt_id"))
        if (attempt is None or attempt["request_sha256"] != identity["request_sha256"]
                or attempt["status"] != "success" or attempt.get("uncertain")):
            raise ValueError("Captured response does not match a completed attempt")
        seen.add(identity["request_sha256"])
        write_json(destination / (identity["request_sha256"] + ".live.json"), row)
    write_json(destination / "http-budget.json", ledger)
    return destination


def _request_accounting(adapter, root):
    """Global ledger is authoritative; audit slices must refer to each attempt once."""
    ids = [attempt["attempt_id"] for audit in adapter.audit_log for attempt in audit["attempts"]]
    ledger_ids = [attempt["attempt_id"] for attempt in adapter.ledger["attempts"]]
    if len(ids) != len(set(ids)) or set(ids) - set(ledger_ids):
        raise ValueError("Request audit duplicates or invents persistent attempts")
    summary = adapter.usage_summary()
    summary.update(ledger_attempt_count=len(ledger_ids), current_process_attempt_count=len(ids),
                   actual_new_http_requests=len(ids),
                   unique_attempt_ids_verified=len(ledger_ids) == len(set(ledger_ids)),
                   cache_hits=sum(bool(row.get("cache_hit")) for row in adapter.audit_log),
                   eligible_requests=sum(not row.get("api_skipped_reason") for row in adapter.audit_log),
                   valid_jev_resolutions=sum(bool(row.get("valid_jev")) for row in adapter.audit_log),
                   api_skipped_count=sum(bool(row.get("api_skipped_reason")) for row in adapter.audit_log))
    summary["valid_jev_resolution_rate"] = (summary["valid_jev_resolutions"] / summary["eligible_requests"]
                                            if summary["eligible_requests"] else None)
    summary["rate_scope"] = "Eligible logical resolutions; cache hits are not new service calls"
    write_json(root / "attempt-ledger.json", adapter.ledger)
    write_json(root / "per-request.json", adapter.audit_log)
    write_json(root / "usage-accounting.json", summary)
    write_json(root / "response-evidence.json", response_evidence(adapter))
    return summary


def _audit_physics(root, stage):
    reports = []
    if stage == "fixed-state":
        selected = [(method, read_json(root / "runs" / f"{method}.json"), "all")
                    for method in METHODS[1:]]
    else:
        selected = []
        for scenario in SCENARIOS:
            for method in METHODS:
                path = root / "runs" / scenario / "seed-0" / method / "decisions.json"
                for index, record in enumerate(read_json(path)["records"]):
                    selected.append((f"{scenario}/{method}/decision-{index}", record, "first-per-decision"))
    for label, record, selection in selected:
        report = replay_decision_branches(record, selection=selection,
                                          artifact_dir=root / "branch-replay" / label)
        reports.append({"run_decision": label, **report})
    result = {"selection_fixed_before_execution": "all fixed-state branches; branch index 0 of every pilot decision",
              "audit_replay_des": sum(row["audit_replay_des"] for row in reports),
              "audit_replay_simulated_hours": math.fsum(row["audit_replay_simulated_hours"] for row in reports),
              "audit_replay_wall_seconds": math.fsum(row["audit_replay_wall_seconds"] for row in reports),
              "actual_http_requests": 0, "decision_des_added": 0, "reports": reports}
    write_json(root / "branch-replay-summary.json", result)
    return {key: value for key, value in result.items() if key != "reports"}


def _enrich_pilot(root, result, *, fixture=False):
    """Use existing experiment rows and summaries, adding only I/O accounting."""
    rows, decisions = result["rows"], []
    for row in rows:
        folder = root / row["scenario"] / f"seed-{row['seed']}" / row["method"]
        records = read_json(folder / "decisions.json")
        by_window = {item["request"]["context"]["window_start"]: item for item in records["records"]}
        for decision in records["summary"]:
            jev = (by_window.get(decision["window_start"], {}).get("jev") or {})
            decision.update(cache_hit=jev.get("cache_hit", False), retry_count=jev.get("retry_count", 0),
                uncertain_attempts=jev.get("uncertain_attempts", 0),
                unknown_usage_attempts=jev.get("unknown_usage_attempts", 0),
                usage_complete=jev.get("usage_complete", True), source_usage=jev.get("source_usage"),
                response_source=jev.get("response_source"),
                attempt_ids=[item["attempt_id"] for item in jev.get("attempts", [])],
                cost_estimate_usd=jev.get("cost_estimate_usd", 0))
            decisions.append(decision)
        audits = read_json(folder / "api-audit.json")
        row.update(cache_hits=sum(bool(item.get("cache_hit")) for item in audits),
            retry_count=sum(item.get("retry_count", 0) for item in audits),
            uncertain_attempts=sum(item.get("uncertain_attempts", 0) for item in audits),
            unknown_usage_attempts=sum(item.get("unknown_usage_attempts", 0) for item in audits),
            usage_complete=all(item.get("usage_complete", True) for item in audits),
            evidence_role="fixture_protocol_acceptance" if fixture else "strict_live_engineering_acceptance")
        write_json(folder / "pilot-result.json", row)
    write_csv(root / "per-seed.csv", rows)
    write_csv(root / "per-decision.csv", decisions)
    aggregate = aggregate_rows(rows)
    for group in aggregate:
        members = [row for row in rows if (row["scenario"], row["method"]) ==
                   (group["scenario"], group["method"])]
        for key in ("cache_hits", "retry_count", "uncertain_attempts", "unknown_usage_attempts",
                    "cost_estimate_usd", "api_skipped_count", "valid_response_count"):
            group[key] = sum(row[key] for row in members)
        group["usage_complete"] = all(row["usage_complete"] for row in members)
    write_json(root / "aggregate.json", aggregate)
    write_csv(root / "aggregate.csv", aggregate)


def _validate_stage_budget(stage, max_http_requests, total_timeout_seconds):
    max_calls, max_seconds = STAGE_LIMITS[stage]
    if type(max_http_requests) is not int or not 0 < max_http_requests <= max_calls:
        raise ValueError(f"{stage} requires 1..{max_calls} total HTTP attempts")
    if (type(total_timeout_seconds) not in (int, float) or not math.isfinite(total_timeout_seconds)
            or not 0 < total_timeout_seconds <= max_seconds):
        raise ValueError(f"{stage} total timeout must be positive and at most {max_seconds:g} seconds")


def run_stage(stage, output, *, allow_live, max_http_requests, total_timeout_seconds,
              transport=None, clock=time.time, sleeper=time.sleep, verify_replay=True):
    _validate_stage_budget(stage, max_http_requests, total_timeout_seconds)
    if not allow_live:
        raise ValueError("Strict live acceptance requires explicit --allow-live")
    output = _new_directory(output)
    started = time.perf_counter()
    record = {"version": VERSION, "stage": stage, "status": "not_run", "exit_code": 2,
        "model": MODEL, "template_version": TEMPLATE_VERSION, "actual_http_requests": 0,
        "max_http_requests": max_http_requests, "total_timeout_seconds": total_timeout_seconds,
        "configured_budget": {"M": 16, "K": 4, "B": 8}, "planning_seed": 20261102,
        "source_before": code_fingerprint(ROOT), "engine_offline_verified": False,
        "jev_live_smoke_verified": False, "jev_live_pilot_completed": False,
        "jev_live_fixed_verified": False, "command": list(getattr(sys, "orig_argv", sys.argv)),
        "evidence_kind": "fixture" if transport else "live_pending",
        "presentation_sensitivity": "not tested; only canonical client ordering is fixed",
        "interpretation": "Engineering acceptance of fixed synthetic cases; no significance, generalization, or superiority claim"}
    adapter = None
    try:
        if not os.environ.get("TYPESAFE_API_KEY"):
            record["reason"] = "TYPESAFE_API_KEY is absent"
            write_json(output / "attempt-ledger.json", {"stage": stage, "attempts": [],
                "status": "not_run", "max_http_requests": max_http_requests,
                "total_timeout_seconds": total_timeout_seconds, "started_at_epoch": None})
            write_json(output / "response-evidence.json", {"version": VERSION, "records": [],
                "evidence_kind": "not_run", "reason": record["reason"]})
            write_json(output / "usage-accounting.json", {"actual_http_requests": 0,
                "actual_new_http_requests": 0, "known_usage": {"input_tokens": 0, "output_tokens": 0},
                "unknown_usage_attempts": 0, "usage_complete": True, "uncertain_attempts": 0,
                "cost_estimate_usd": 0, "status": "not_run"})
            return record
        adapter = ChoiceAdapter(JevConfig(mode="live", cache_dir=str(output / "response-store"),
            allow_live=True, max_http_requests=max_http_requests,
            total_timeout_seconds=total_timeout_seconds), transport=transport, clock=clock, sleeper=sleeper)
        runtime, counter = audited_runtime("live", adapter=adapter)
        if stage == "fixed-state":
            result = fixed_comparison(output / "runs", mode="live", runtime=runtime, counter=counter,
                                      strict_jev=True, phase=stage)
            record.update(run_count=6, decision_count=6, actual_des=result["actual_des"],
                          simulated_hours=result["simulated_hours"])
        else:
            result = run_pilot(output / "runs", seeds=(0,), scenarios=SCENARIOS, methods=METHODS,
                mode="live", runtime=runtime, counter=counter, strict_jev=True, phase=stage,
                save_resume=True, include_fixed=False)
            _enrich_pilot(output / "runs", result, fixture=transport is not None)
            record.update(run_count=result["run_count"], decision_count=result["decision_count"],
                actual_des=result["actual_des"], simulated_hours=result["simulated_hours"],
                paired_exogenous_inputs_verified=result["paired_exogenous_inputs_verified"])
            if record["run_count"] != 12 or record["decision_count"] != 60:
                raise ValueError("Pilot did not complete the frozen 12-run, 60-decision design")
        record["branch_replay"] = _audit_physics(output, stage)
        accounting = _request_accounting(adapter, output)
        if accounting["actual_new_http_requests"] != adapter.http_requests:
            raise ValueError("New stage request accounting differs from durable attempts")
        if result["actual_http_requests"] != adapter.http_requests:
            raise ValueError("Per-run HTTP accounting differs from campaign ledger")
        eligible = [row for row in adapter.audit_log if not row.get("api_skipped_reason")]
        qualified = bool(eligible) and all(row.get("valid_jev") for row in eligible) and transport is None
        record.update(status="passed" if qualified else "fixture_passed", exit_code=0,
                      engine_offline_verified=True, evidence_kind="captured_live" if qualified else "fixture")
        record["jev_live_fixed_verified" if stage == "fixed-state" else "jev_live_pilot_completed"] = qualified
        # Write the completed capture before replay reads it; the final block
        # records replay failure and revokes the completion flag if necessary.
        write_json(output / "stage.json", record)
        if qualified and verify_replay:
            record["replay_verification"] = replay_stage(output, output / "replay-verification")
        return record
    except (JevError, ValueError, AssertionError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        record.update(status="failed", exit_code=1, reason=getattr(error, "code", type(error).__name__),
                      jev_live_fixed_verified=False, jev_live_pilot_completed=False)
        partial = output / "runs" / "experiment.json"
        if stage == "pilot" and partial.is_file():
            progress = read_json(partial)
            record.update(run_count=progress.get("run_count", 0),
                          decision_count=progress.get("decision_count", 0),
                          completed_runs=[{key: row[key] for key in ("scenario", "seed", "method")}
                                          for row in progress.get("rows", [])])
        return record
    finally:
        if adapter is not None:
            record["actual_http_requests"] = adapter.http_requests
            try:
                record["accounting"] = _request_accounting(adapter, output)
            except (ValueError, KeyError, TypeError, OSError) as error:
                record.update(status="failed", exit_code=1, accounting_error=type(error).__name__,
                              jev_live_fixed_verified=False, jev_live_pilot_completed=False)
                write_json(output / "attempt-ledger.json", adapter.ledger)
        record["source_after"] = code_fingerprint(ROOT)
        record["source_stable"] = record["source_before"] == record["source_after"]
        if not record["source_stable"] and record["exit_code"] == 0:
            record.update(status="failed", exit_code=1, reason="source_changed_during_stage",
                          jev_live_fixed_verified=False, jev_live_pilot_completed=False)
        record["end_to_end_seconds"] = time.perf_counter() - started
        write_json(output / "stage.json", record)


def resume_cached(checkpoint, expected_checkpoint, response_store, output, *, max_http_requests,
                  total_timeout_seconds, until=None, transport=None):
    output = _new_directory(output)
    result = {"version": VERSION, "status": "running", "actual_http_requests": 0,
              "network_policy": "cache_only", "projection": IO_PROJECTION}
    adapter = None
    try:
        config = JevConfig(mode="live", network_policy="cache_only", cache_dir=str(response_store),
                           max_http_requests=max_http_requests, total_timeout_seconds=total_timeout_seconds)
        adapter = ChoiceAdapter(config, transport=transport)
        ledger_before = deepcopy(adapter.ledger)
        runtime, counter = audited_runtime("live", adapter=adapter)
        sim = WeeklySimulation.from_checkpoint(read_json(checkpoint))
        policy = sim.weekly_state["policy_spec"]
        if policy.get("kind") != "candidate-des" or policy.get("jev_mode") != "live" or not policy["strict_jev"]:
            raise ValueError("Recovery requires unchanged strict live policy identity")
        sim._decision_runtime = runtime
        sim.run(until)
        if adapter.ledger != ledger_before:
            raise ValueError("Cache-only recovery changed the persistent HTTP budget")
        expected = read_json(expected_checkpoint)
        result.update(compare_checkpoints(expected, sim.to_checkpoint()))
        result["business_audit"] = audit_business_log(sim)
        write_weekly_run(sim, output / "run")
        result.update(status="passed", exit_code=0, original_budget_unchanged=True,
                      historical_policy_mode="live", new_decisions_audited=counter["verified_branches"],
                      persistent_http_requests=adapter.http_requests,
                      cache_hits=sum(bool(row.get("cache_hit")) for row in adapter.audit_log))
        return result
    except (JevError, ValueError, AssertionError, RuntimeError, OSError) as error:
        result.update(status="failed", exit_code=1, reason=getattr(error, "code", type(error).__name__))
        return result
    finally:
        if adapter is not None:
            write_json(output / "new-request-audit.json", adapter.audit_log)
        write_json(output / "resume.json", result)


def replay_stage(source, output):
    source, output = Path(source).resolve(), _new_directory(output)
    captured = read_json(source / "stage.json")
    if captured.get("status") != "passed" or captured.get("evidence_kind") != "captured_live":
        raise ValueError("Exact real replay requires a completed live capture")
    cache = import_response_evidence(read_json(source / "response-evidence.json"), output / "response-store")
    adapter = ChoiceAdapter(JevConfig(mode="real_replay", cache_dir=str(cache), max_http_requests=0))
    runtime, counter = audited_runtime("real_replay", adapter=adapter)
    report = {"version": VERSION, "status": "running", "actual_http_requests": 0,
              "source": str(source), "projection": IO_PROJECTION, "comparisons": []}
    try:
        if captured["stage"] == "fixed-state":
            fixed_comparison(output / "runs", mode="real_replay", runtime=runtime, counter=counter,
                             strict_jev=True, phase="fixed-state-replay")
            for method in METHODS[1:]:
                left = _record_projection(read_json(source / "runs" / f"{method}.json"))
                right = _record_projection(read_json(output / "runs" / f"{method}.json"))
                compare_values(left, right, method)
                report["comparisons"].append({"method": method, "logical_record_equal": True})
        else:
            run_pilot(output / "runs", seeds=(0,), scenarios=SCENARIOS, methods=METHODS,
                mode="real_replay", runtime=runtime, counter=counter, strict_jev=True,
                phase="pilot-replay", include_fixed=False)
            for scenario in SCENARIOS:
                for method in METHODS:
                    relative = Path("runs") / scenario / "seed-0" / method / "checkpoint.json"
                    comparison = compare_checkpoints(read_json(source / relative), read_json(output / relative))
                    report["comparisons"].append({"scenario": scenario, "method": method,
                                                   "logical_checkpoint_equal": comparison["logical_checkpoint_equal"]})
            budget = read_json(cache / "http-budget.json")
            source_run = source / "runs" / "balanced" / "seed-0" / "jev-des"
            command = [sys.executable, str(ROOT / "scripts" / "phase2c_live.py"), "resume",
                "--checkpoint", str(source_run / "resume-point.json"),
                "--expected-checkpoint", str(source_run / "checkpoint.json"),
                "--response-store", str(cache), "--max-http-requests", str(budget["max_http_requests"]),
                "--total-timeout-seconds", str(budget["total_timeout_seconds"]),
                "--output", str(output / "new-process-resume")]
            environment = dict(os.environ)
            environment.pop("TYPESAFE_API_KEY", None)
            process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=1800)
            write_json(output / "resume-command.json", {"command": command, "exit_code": process.returncode,
                                                        "credential_removed": True})
            (output / "resume-process.log").write_text(process.stdout + process.stderr, encoding="utf-8")
            if process.returncode:
                raise ValueError("New-process cache-only recovery failed")
            report["new_process_resume"] = read_json(output / "new-process-resume" / "resume.json")
        if adapter.http_requests or any(row["actual_http_requests"] for row in adapter.audit_log):
            raise ValueError("Exact replay performed HTTP")
        write_json(output / "per-request.json", adapter.audit_log)
        report.update(status="passed", exit_code=0, decision_des=counter["verified_branches"],
                      network_attempts_zero=True)
        return report
    except (JevError, ValueError, AssertionError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        report.update(status="failed", exit_code=1, reason=getattr(error, "code", type(error).__name__))
        raise
    finally:
        write_json(output / "replay.json", report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="stage", required=True)
    for stage in STAGE_LIMITS:
        command = commands.add_parser(stage)
        command.add_argument("--allow-live", action="store_true")
        command.add_argument("--max-http-requests", type=int, required=True)
        command.add_argument("--total-timeout-seconds", type=float, required=True)
        command.add_argument("--output", type=Path, required=True)
        if stage == "pilot":
            command.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
            command.add_argument("--seeds", nargs="+", type=int, default=[0])
    replay = commands.add_parser("replay")
    replay.add_argument("--source", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    resume = commands.add_parser("resume")
    for name in ("checkpoint", "expected-checkpoint", "response-store", "output"):
        resume.add_argument("--" + name, type=Path, required=True)
    resume.add_argument("--max-http-requests", type=int, required=True)
    resume.add_argument("--total-timeout-seconds", type=float, required=True)
    resume.add_argument("--until", type=float)
    resume.add_argument("--fixture-cache-only", action="store_true",
                        help="Read fixture cache only; never qualifies as live or permits network")
    args = parser.parse_args(argv)
    try:
        if args.stage in STAGE_LIMITS:
            if args.stage == "pilot" and (args.scenarios != list(SCENARIOS) or args.seeds != [0]):
                parser.error("First live pilot is frozen to --scenarios balanced scarce --seeds 0")
            _validate_stage_budget(args.stage, args.max_http_requests, args.total_timeout_seconds)
            if not args.allow_live:
                parser.error("Live requires --allow-live")
            result = run_stage(args.stage, args.output, allow_live=args.allow_live,
                max_http_requests=args.max_http_requests, total_timeout_seconds=args.total_timeout_seconds)
        elif args.stage == "replay":
            result = replay_stage(args.source, args.output)
        else:
            def forbidden_transport(*unused):
                raise AssertionError("Cache-only recovery must never call a transport")
            result = resume_cached(args.checkpoint, args.expected_checkpoint, args.response_store, args.output,
                max_http_requests=args.max_http_requests, total_timeout_seconds=args.total_timeout_seconds,
                until=args.until, transport=forbidden_transport if args.fixture_cache_only else None)
        print(json.dumps({key: result.get(key) for key in ("status", "exit_code", "reason",
              "actual_http_requests", "jev_live_fixed_verified", "jev_live_pilot_completed")}, ensure_ascii=False))
        return result["exit_code"]
    except (ValueError, OSError, AssertionError, JevError) as error:
        print(json.dumps({"status": "failed", "reason": getattr(error, "code", type(error).__name__)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
