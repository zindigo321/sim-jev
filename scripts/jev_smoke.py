"""Explicit, bounded real HTTP smoke through the complete weekly decision path."""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import sys
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev import Config, MissionSpec, WeeklySimulation
from sim_jev.decision import DecisionPolicySpec
from sim_jev.decision_resolver import DecisionRuntime
from sim_jev.jev import ChoiceAdapter, JevConfig, JevError
from sim_jev.weekly_reporting import write_weekly_run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--max-http-requests", type=int, default=3)
    parser.add_argument("--total-timeout-seconds", type=float, default=120.)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.allow_live:
        parser.error("real smoke requires explicit --allow-live")
    if not 1 <= args.max_http_requests <= 3:
        parser.error("smoke requires 1..3 total HTTP attempts")
    if not math.isfinite(args.total_timeout_seconds) or not 0 < args.total_timeout_seconds <= 120:
        parser.error("smoke requires a positive total deadline no greater than 120 seconds")
    if args.output.exists():
        parser.error("smoke output must be a new directory")
    args.output.mkdir(parents=True)
    record = {"schema": "jev-live-smoke-acceptance-v2", "engine_offline_verified": False,
              "jev_live_smoke_verified": False, "jev_live_pilot_completed": False,
              "stage": "smoke", "model": "jev-1.14.0",
              "mode": "live", "actual_http_requests": 0, "usage": {},
              "actual_command": sys.orig_argv, "max_http_requests": args.max_http_requests,
              "total_timeout_seconds": args.total_timeout_seconds}
    started = time.perf_counter()
    adapter = None
    try:
        adapter = ChoiceAdapter(JevConfig(mode="live", cache_dir=str(args.output / "response-store"),
            allow_live=True, max_http_requests=args.max_http_requests,
            total_timeout_seconds=args.total_timeout_seconds))
        adapter.set_context(phase="smoke", run="smoke", scenario="smoke", seed=42, method="jev-des")
        if not os.environ.get("TYPESAFE_API_KEY"):
            record.update(status="not_run", reason="TYPESAFE_API_KEY is absent", exit_code=2)
            return 2
        config = replace(Config(), fleet_size=2, mission_rate_per_hour=0)
        missions = [MissionSpec(f"SMOKE-{i}", 1 + 3 * (i % 3), 4, 1, 10 + i) for i in range(8)]
        from scripts.planning_verify import audit_planning_branch
        simulation = WeeklySimulation(config, missions,
            policy=DecisionPolicySpec(method="jev-des", jev_mode="live", strict_jev=True),
            decision_runtime=DecisionRuntime(adapter, audit_planning_branch))
        simulation.run(0)
        write_weekly_run(simulation, args.output / "run")
        checkpoint = simulation.to_checkpoint()
        restored = WeeklySimulation.from_checkpoint(checkpoint)
        restored._decision_runtime = DecisionRuntime(adapter)
        before = adapter.http_requests
        restored.run(0)
        if adapter.http_requests != before:
            raise RuntimeError("persisted decision unexpectedly called HTTP again")
        decision = simulation.weekly_state["decision_records"][0]
        if not (decision["jev"]["valid_jev"] and decision["jev"]["valid_response"]
                and decision["jev"].get("transport_kind") == "urllib_http"
                and adapter.http_requests > 0 and decision["budget"]["actual_des"] > 0
                and len(simulation.weekly_state["weekly_commands"]) == 1):
            raise RuntimeError("smoke requires real eligible Choice, DES and one committed command")
        command = [sys.executable, str(ROOT / "scripts/phase2c_live.py"), "resume",
                   "--checkpoint", str((args.output / "run/checkpoint.json").resolve()),
                   "--expected-checkpoint", str((args.output / "run/checkpoint.json").resolve()),
                   "--response-store", str((args.output / "response-store").resolve()),
                   "--max-http-requests", str(args.max_http_requests),
                   "--total-timeout-seconds", str(args.total_timeout_seconds), "--until", "0",
                   "--output", str((args.output / "fresh-process-resume").resolve())]
        environment = dict(os.environ)
        environment.pop("TYPESAFE_API_KEY", None)
        child = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120)
        (args.output / "fresh-process-resume.log").write_text(child.stdout + child.stderr, encoding="utf-8")
        record["fresh_process_resume"] = {"command": command, "exit_code": child.returncode}
        if child.returncode:
            raise RuntimeError("cache-only fresh process restore failed")
        record.update(status="passed", exit_code=0, jev_live_smoke_verified=True,
                      restored_command_did_not_recall=True, des_budget=decision["budget"],
                      usage=decision["jev"]["actual_usage"],
                      presentation_sensitivity="canonical order fixed; independent order experiment not performed")
        return 0
    except JevError as exc:
        record.update(status="failed", exit_code=1, reason=exc.code, adapter_audit=exc.audit)
        return 1
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record.update(status="failed", exit_code=1, reason=type(exc).__name__)
        return 1
    finally:
        record["end_to_end_seconds"] = time.perf_counter() - started
        if adapter is not None:
            record["actual_http_requests"] = adapter.http_requests
            record["http_budget"] = adapter.ledger
            record["adapter_audits"] = adapter.audit_log
            record["usage_accounting"] = adapter.usage_summary()
            from scripts.phase2c_live import response_evidence, write_json
            evidence = response_evidence(adapter)
            if record.get("status") == "not_run":
                evidence.update(evidence_kind="not_run", reason=record["reason"])
            write_json(args.output / "response-evidence.json", evidence)
        (args.output / "smoke.json").write_text(json.dumps(record, ensure_ascii=False, indent=2,
                                                         allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in record.items()
                          if key not in {"adapter_audits", "http_budget", "adapter_audit"}}, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
