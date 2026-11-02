"""Run or resume the versioned synthetic fleet simulation."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from .config import load_config
from .engine import Simulation
from .reporting import check_output_directory, write_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic fleet mission/failure/repair/spares simulation (hours)."
    )
    parser.add_argument("--config", type=Path, help="JSON scenario configuration (default: configs/default.json)")
    parser.add_argument("--seed", type=int, help="Override the configured random seed")
    parser.add_argument("--mode", choices=("legacy", "weekly"),
                        help="Model entry (fresh default: legacy; resume: checkpoint identity)")
    parser.add_argument("--output", type=Path, default=Path("runs/demo"), help="New or empty output directory")
    parser.add_argument("--until", type=float, help="Pause after all events at this hour; default: configured horizon")
    parser.add_argument("--checkpoint-out", type=Path, help="Save a resumable internal JSON checkpoint to a new file")
    parser.add_argument("--resume", type=Path, help="Resume an internal checkpoint; incompatible with --config/--seed")
    parser.add_argument("--jev-store", type=Path, help="Runtime-only durable live/replay response store")
    parser.add_argument("--allow-live", action="store_true", help="Explicitly enable bounded real TypeSafe HTTP")
    parser.add_argument("--max-http-requests", type=int, help="Explicit total HTTP attempt cap, including retries")
    parser.add_argument("--http-total-seconds", type=float, help="Explicit total live run time limit")
    arguments = parser.parse_args(argv)
    if arguments.resume is not None and (arguments.config is not None or arguments.seed is not None):
        parser.error("--resume cannot be combined with --config or --seed; checkpoint inputs are authoritative")
    try:
        destination = check_output_directory(arguments.output)
        if arguments.checkpoint_out is not None and arguments.checkpoint_out.exists():
            raise ValueError(f"Checkpoint output already exists: {arguments.checkpoint_out}")
        if arguments.checkpoint_out is not None:
            checkpoint_path = arguments.checkpoint_out.resolve()
            reserved = {destination.resolve(), *(destination.resolve() / name for name in (
                "config.json", "events.csv", "missions.csv", "summary.json", "states.csv",
                "repairs.csv", "orders.csv", "state.json", "runtime.json", "overview.png",
                "checkpoint.json", "weekly-ledger.json", "policy-observation.json", "random-manifest.json",
            ))}
            if checkpoint_path in reserved:
                raise ValueError("--checkpoint-out must not replace the output directory or a report artifact")
        context = {"requested_until": arguments.until}
        if arguments.resume is not None:
            from .checkpoint import _loads
            from .versions import MODEL_VERSION, WEEKLY_MODEL_VERSION
            payload = _loads(arguments.resume.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("checkpoint root must be an object")
            version = payload.get("model_version")
            if version == WEEKLY_MODEL_VERSION:
                from .weekly import WeeklySimulation
                mode, simulation = "weekly", WeeklySimulation.from_checkpoint(payload)
            elif version == MODEL_VERSION:
                mode, simulation = "legacy", Simulation.from_checkpoint(payload)
            else:
                raise ValueError("unsupported checkpoint model identity")
            if arguments.mode is not None and arguments.mode != mode:
                raise ValueError("--mode disagrees with checkpoint model identity")
            context.update(mode="resumed", checkpoint_source=str(arguments.resume.resolve()),
                           checkpoint_source_sha256=hashlib.sha256(arguments.resume.read_bytes()).hexdigest(),
                           resumed_at=simulation.now)
        else:
            mode = arguments.mode or "legacy"
            if mode == "weekly":
                from .weekly_config import load_weekly_simulation
                simulation = load_weekly_simulation(
                    arguments.config or Path("configs/weekly_default.json"), arguments.seed)
            else:
                config = load_config(arguments.config or Path("configs/default.json"))
                if arguments.seed is not None:
                    config = replace(config, seed=arguments.seed)
                simulation = Simulation(config)
            context["mode"] = "fresh"
        if arguments.allow_live and (arguments.max_http_requests is None or arguments.http_total_seconds is None):
            raise ValueError("live runs require explicit --max-http-requests and --http-total-seconds")
        if arguments.jev_store is not None:
            from .decision_resolver import DecisionRuntime
            from .jev import ChoiceAdapter, JevConfig
            policy = getattr(simulation, "weekly_state", {}).get("policy_spec", {})
            if policy.get("kind") != "candidate-des" or not policy.get("method", "").startswith("jev"):
                raise ValueError("--jev-store requires a Jev candidate policy")
            simulation._decision_runtime = DecisionRuntime(ChoiceAdapter(JevConfig(
                mode=policy["jev_mode"], cache_dir=str(arguments.jev_store), allow_live=arguments.allow_live,
                max_http_requests=arguments.max_http_requests if arguments.max_http_requests is not None else 10,
                total_timeout_seconds=arguments.http_total_seconds if arguments.http_total_seconds is not None else 120)))
        elif arguments.allow_live:
            raise ValueError("--allow-live requires --jev-store for durable attempts and responses")
        summary = simulation.run(until=arguments.until)
        context["finished_at"] = simulation.now
        if mode == "weekly":
            from .weekly_reporting import write_weekly_run
            write_weekly_run(simulation, destination, run_context=context)
        else:
            write_run(simulation, destination, run_context=context)
        if arguments.checkpoint_out is not None:
            simulation.save_checkpoint(arguments.checkpoint_out)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"output": str(destination.resolve()), "summary": summary}, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
