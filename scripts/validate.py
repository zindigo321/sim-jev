"""Repeat the phase-one acceptance run without overwriting existing results.

Run with the project interpreter: python scripts/validate.py --output runs/validation
This checks actual engine runs and exported accounting identities; it does not
implement a second simulation model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from sim_jev.config import load_config
from sim_jev.engine import Simulation
from sim_jev.reporting import check_output_directory
from sim_jev.provenance import canonical_sha256, code_fingerprint
from sim_jev.versions import (
    AUDIT_STATE_SCHEMA_VERSION, CHECKPOINT_SCHEMA_VERSION, MODEL_VERSION,
    PROVENANCE_SCHEMA_VERSION, RNG_PROTOCOL_VERSION,
)


ARTIFACTS = (
    "config.json", "events.csv", "missions.csv", "summary.json", "states.csv",
    "repairs.csv", "orders.csv", "state.json", "runtime.json", "overview.png",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def close(actual: float, expected: float, label: str) -> None:
    require(math.isclose(float(actual), float(expected), rel_tol=1e-10, abs_tol=1e-8),
            f"{label}: {actual} != {expected}")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                               allow_nan=False) + "\n", encoding="utf-8")


def check_runtime_details(runtime: dict, config: dict, *, require_provenance: bool,
                          expected_fingerprint: str | dict | None = None,
                          source_root: Path | None = None) -> dict:
    """Separate metadata/manifest checks from an explicitly requested source check.

    Historical reports are valid against their original source. They are never
    implicitly checked against the current checkout. ``expected_fingerprint``
    can be an independently recorded SHA256 or a complete fingerprint manifest.
    ``source_root`` computes that expectation from an explicit source tree.
    """
    details = {"metadata_status": None, "metadata_valid": False,
               "manifest_self_consistent": False, "source_identity_verified": False}
    require(bool(runtime["python_version"]) and bool(runtime["packages"]), "Missing runtime versions")
    if "schema_version" not in runtime:
        require(not require_provenance, "New output is missing provenance schema_version")
        require(expected_fingerprint is None and source_root is None,
                "Legacy runtime has no source fingerprint to verify")
        details.update(metadata_status="legacy-runtime-only", metadata_valid=True)
        return details
    for name, expected in (
        ("schema_version", PROVENANCE_SCHEMA_VERSION), ("model_version", MODEL_VERSION),
        ("audit_state_schema_version", AUDIT_STATE_SCHEMA_VERSION),
        ("checkpoint_schema_version", CHECKPOINT_SCHEMA_VERSION),
        ("rng_protocol_version", RNG_PROTOCOL_VERSION),
    ):
        require(type(runtime.get(name)) is type(expected) and runtime.get(name) == expected,
                f"Unexpected runtime {name}")
    require(runtime.get("effective_config_sha256") == canonical_sha256(config), "Effective config hash mismatch")
    require(isinstance(runtime.get("actual_command"), list) and bool(runtime["actual_command"])
            and all(isinstance(part, str) for part in runtime["actual_command"]), "Missing actual command")
    require(bool(runtime.get("command_line")) and bool(runtime.get("python_executable")), "Missing command/interpreter")
    fingerprint = runtime.get("code_fingerprint", {})
    require(type(fingerprint) is dict, "Invalid source fingerprint")
    files = fingerprint.get("files", [])
    require(fingerprint.get("algorithm") == "sha256" and type(files) is list and bool(files),
            "Missing source fingerprint")
    require(all(type(item) is dict for item in files), "Invalid source manifest entry")
    require(fingerprint.get("scope") in {"project-source-and-config-v1", "project-source-and-config-v2"},
            "Unknown source fingerprint scope")
    require(fingerprint.get("sha256") == canonical_sha256(files), "Source manifest hash mismatch")
    paths = [item.get("path") for item in files]
    require(all(isinstance(path, str) and path and not Path(path).is_absolute()
                and ".." not in Path(path).parts for path in paths), "Invalid source path")
    require(paths == sorted(set(paths)), "Source paths must be sorted and unique")
    for item in files:
        require(bool(re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))), "Invalid source SHA256")
        require(type(item.get("size_bytes")) is int and item["size_bytes"] >= 0, "Invalid source size")
    git = runtime.get("git", {})
    require(type(git.get("available")) is bool, "Missing Git availability")
    if git["available"]:
        require(bool(re.fullmatch(r"[0-9a-f]{40,64}", git.get("revision", "")))
                and type(git.get("dirty")) is bool, "Missing Git revision/dirty flag")
    else:
        require(git.get("revision") is None and git.get("dirty") is None and bool(git.get("reason")),
                "Unavailable Git must include an explicit reason")
    # Old schema-1 reports did not record a repository root. Accept those as
    # historical metadata, but validate every relationship in reports that do.
    if "repo_root" in git:
        project = git.get("project_root")
        require(isinstance(project, str) and Path(project).is_absolute(), "Invalid Git project root")
        require(git.get("revision_scope") == "repository", "Unclear Git revision scope")
        repository = git["repo_root"]
        if repository is not None:
            require(isinstance(repository, str) and Path(repository).is_absolute(), "Invalid Git repository root")
            project_path, repository_path = Path(project), Path(repository)
            require(project_path.is_relative_to(repository_path), "Git repository does not contain project")
            relative = project_path.relative_to(repository_path).as_posix()
            require(git.get("project_relative_path") == relative, "Git project-relative path mismatch")
            relationship = "project_repository" if project_path == repository_path else "parent_repository"
            require(git.get("relationship") == relationship, "Git repository relationship mismatch")
        else:
            require(not git["available"] and git.get("relationship") == "unavailable"
                    and git.get("project_relative_path") is None, "Missing Git repository identity")
        if git["available"]:
            require(type(git.get("project_has_tracked_files")) is bool, "Missing project tracking status")
    details.update(metadata_status="provenance-v1", metadata_valid=True,
                   manifest_self_consistent=True)
    expectations = []
    if expected_fingerprint is not None:
        expected_hash = (expected_fingerprint.get("sha256") if type(expected_fingerprint) is dict
                         else expected_fingerprint)
        require(isinstance(expected_hash, str) and bool(re.fullmatch(r"[0-9a-f]{64}", expected_hash)),
                "Invalid expected source fingerprint")
        expectations.append(expected_hash)
        if type(expected_fingerprint) is dict:
            require(fingerprint == expected_fingerprint, "Expected source manifest mismatch")
    if source_root is not None:
        expected_source = code_fingerprint(source_root, scope=fingerprint["scope"])
        require(fingerprint == expected_source, "Source-root manifest mismatch")
        expectations.append(expected_source["sha256"])
    for expected_hash in expectations:
        require(fingerprint["sha256"] == expected_hash, "Expected source fingerprint mismatch")
    details["source_identity_verified"] = bool(expectations)
    return details


def check_runtime(runtime: dict, config: dict, *, require_provenance: bool,
                  expected_fingerprint: str | dict | None = None,
                  source_root: Path | None = None) -> str:
    """Compatibility wrapper; detailed evidence is in ``check_runtime_details``."""
    return check_runtime_details(runtime, config, require_provenance=require_provenance,
                                 expected_fingerprint=expected_fingerprint,
                                 source_root=source_root)["metadata_status"]


def check_artifacts(folder: Path, *, require_provenance: bool = True,
                    expected_fingerprint: str | dict | None = None,
                    source_root: Path | None = None) -> dict:
    """Cross-check serialized totals, state integrals, and readable image data."""
    for name in ARTIFACTS:
        require((folder / name).is_file() and (folder / name).stat().st_size > 0,
                f"Missing/empty artifact: {folder / name}")
    summary = read_json(folder / "summary.json")
    config = load_config(folder / "config.json")
    state = read_json(folder / "state.json")
    runtime = read_json(folder / "runtime.json")
    frames = {name: pd.read_csv(folder / f"{name}.csv")
              for name in ("events", "missions", "states", "repairs", "orders")}
    events, missions, states, repairs, orders = (frames[name] for name in frames)
    require(config.seed == summary["seed"], "Saved seed disagrees with summary")
    require(state["time"] == summary["elapsed_hours"], "State cutoff disagrees with summary")
    metadata_checks = check_runtime_details(runtime, config.to_dict(), require_provenance=require_provenance,
                                            expected_fingerprint=expected_fingerprint, source_root=source_root)
    require(len(missions) == summary["missions_offered"] + summary["missions_future"],
            "Mission CSV row count disagrees with summary")
    for status, key in (("success", "missions_success"), ("failed", "missions_failed"),
                        ("not_executed", "missions_not_executed"),
                        ("running", "missions_unfinished"), ("pending", "missions_future")):
        require(int((missions["status"] == status).sum()) == summary[key], key)
    close(missions["flight_hours"].sum(), summary["flight_hours"], "mission exposure")
    close(missions["earned_value"].sum(), summary["mission_value_earned"], "earned value")
    close(missions.loc[missions["status"] != "pending", "value"].sum(),
          summary["mission_value_offered"], "offered value")
    require(events["event_id"].is_unique, "Duplicate log ID")
    require(events["time"].is_monotonic_increasing, "Event log time went backwards")
    for event_type, key in (("failure", "failures"), ("mission_success", "missions_success"),
                            ("mission_failed", "missions_failed"),
                            ("mission_not_executed", "missions_not_executed"),
                            ("repair_start", "repairs_started"),
                            ("repair_complete", "repairs_completed")):
        require(int((events["event_type"] == event_type).sum()) == summary[key], key)
    require(len(repairs) == summary["failures"], "Repair request count")
    require(int(repairs["started_at"].notna().sum()) == summary["repairs_started"], "Repair starts")
    require(int((repairs["status"] == "completed").sum()) == summary["repairs_completed"], "Repair completions")
    for key in ("spare_wait_hours", "bay_wait_hours"):
        close(repairs[key].sum(), summary[key], key)
    require(int(orders["quantity"].sum()) == summary["ordered_quantity"], "Ordered quantity")
    require(int(orders.loc[orders["status"] == "arrived", "quantity"].sum())
            == summary["arrived_quantity"], "Arrived quantity")
    require(int(orders.loc[orders["status"] == "in_transit", "quantity"].sum())
            == summary["in_transit"], "In-transit quantity")
    require(summary["on_hand"] == config.initial_spares + summary["arrived_quantity"]
            - summary["repairs_started"], "Spare inventory conservation")
    require(summary["ordered_quantity"] == summary["arrived_quantity"] + summary["in_transit"],
            "Pipeline conservation")
    require(states["time"].is_monotonic_increasing, "State timeline time went backwards")
    close(states.iloc[0]["time"], 0, "Initial timeline time")
    close(states.iloc[-1]["time"], summary["elapsed_hours"], "Final timeline time")
    counts = states[["idle", "flying", "waiting_repair", "repairing"]]
    require(bool((counts >= 0).all().all()) and bool((counts.sum(axis=1) == config.fleet_size).all()),
            "Aircraft state count conservation")
    require(bool((states["repairing"] <= config.repair_bays).all()), "Repair bay capacity")
    require(bool((states[["on_hand", "in_transit", "unmet_demand"]] >= 0).all().all()),
            "Negative inventory/demand")
    dt = states["time"].shift(-1, fill_value=summary["elapsed_hours"]) - states["time"]
    close(((states["idle"] + states["flying"]) * dt).sum(),
          summary["available_aircraft_hours"], "Available aircraft integral")
    close((states["idle"] * dt).sum(), summary["idle_aircraft_hours"], "Idle aircraft integral")
    close((states["flying"] * dt).sum(), summary["flight_hours"], "Flying aircraft integral")
    close((states["waiting_repair"] * dt).sum(),
          summary["total_repair_wait_hours"], "Waiting aircraft integral")
    if summary["elapsed_hours"]:
        close(summary["available_aircraft_hours"] / (config.fleet_size * summary["elapsed_hours"]),
              summary["availability"], "Availability denominator")
    else:
        require(summary["availability"] is None, "Zero-time availability must be null")
    for column, key in (("idle", "aircraft_idle"), ("flying", "aircraft_flying"),
                        ("waiting_repair", "aircraft_waiting_repair"),
                        ("repairing", "aircraft_repairing"),
                        ("on_hand", "on_hand"), ("in_transit", "in_transit"),
                        ("unmet_demand", "unmet_demand")):
        close(states.iloc[-1][column], summary[key], f"Final {column}")
    from matplotlib import image

    pixels = image.imread(folder / "overview.png")
    require(pixels.ndim == 3 and min(pixels.shape[:2]) >= 500 and float(pixels.std()) > 0.01,
            "PNG failed shape/content check")
    return {
        "path": str(folder.relative_to(ROOT)) if folder.is_relative_to(ROOT) else str(folder),
        "passed": True, "artifact_count": len(ARTIFACTS),
        "metadata_status": metadata_checks["metadata_status"], "metadata_checks": metadata_checks,
        "csv_rows": {name: len(frame) for name, frame in frames.items()},
        "png_shape": list(pixels.shape), "summary": summary,
        "sha256": {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                   for name in ARTIFACTS},
    }


def subprocess_run(arguments: list[str], log: Path) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, *arguments], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=180)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    require(result.returncode == 0, f"Command failed ({result.returncode}); see {log}")
    return result


def validate(destination: Path, record: dict) -> None:
    require(sys.flags.optimize == 0, "Acceptance validation refuses optimized Python (-O/-OO)")
    print("Running full unittest suite...", flush=True)
    tests = subprocess_run(["-m", "unittest", "discover", "-s", "tests", "-v"],
                           destination / "unittest.txt")
    match = re.search(r"Ran (\d+) tests?", tests.stdout + tests.stderr)
    require(match is not None, "Cannot identify unittest count")
    record["unit_tests"] = {"count": int(match.group(1)), "passed": True, "log": "unittest.txt"}
    smoke = []
    for name in ("default", "stress"):
        config = load_config(ROOT / "configs" / f"{name}.json")
        print(f"Running {name} seeds 0-9, twice each...", flush=True)
        for seed in range(10):
            effective = replace(config, seed=seed)
            first, second = Simulation(effective), Simulation(effective)
            first_result, second_result = first.run(), second.run()
            first.assert_invariants()
            second.assert_invariants()
            require(first_result == second_result, f"{name} seed {seed}: metric mismatch")
            require(first.events == second.events, f"{name} seed {seed}: event trace mismatch")
            smoke.append({"scenario": name, **first_result,
                          "event_count": len(first.events), "repeat_match": True})
    pd.DataFrame(smoke).to_csv(destination / "smoke.csv", index=False, lineterminator="\n")
    record["smoke"] = {
        "scenarios": ["default", "stress"], "seeds": list(range(10)),
        "unique_runs": 20, "total_runs_with_repeats": 40, "all_repeats_identical": True,
        "per_event_invariants": True,
        "ranges": {
            name: {key: {"min": min(row[key] for row in smoke if row["scenario"] == name),
                         "max": max(row[key] for row in smoke if row["scenario"] == name)}
                   for key in ("missions_offered", "missions_success", "missions_failed",
                               "missions_not_executed", "failures", "repairs_completed",
                               "spare_wait_hours", "availability")}
            for name in ("default", "stress")
        },
    }
    record["fresh_cli_outputs"] = []
    for name in ("default", "stress"):
        print(f"Running and checking fresh {name} CLI example (seed 42)...", flush=True)
        folder = destination / f"{name}_seed42"
        process = subprocess_run(
            ["-m", "sim_jev", "--config", str(ROOT / "configs" / f"{name}.json"),
             "--seed", "42", "--output", str(folder)], destination / f"{name}_cli.txt",
        )
        checked = check_artifacts(folder, expected_fingerprint=code_fingerprint(ROOT))
        require(json.loads(process.stdout)["summary"] == checked["summary"], "CLI stdout summary mismatch")
        record["fresh_cli_outputs"].append(checked)
    record["existing_outputs_read_only"] = []
    for name in ("demo", "stress"):
        folder = ROOT / "runs" / name
        if folder.exists():
            print(f"Checking existing runs/{name} without modification...", flush=True)
            record["existing_outputs_read_only"].append(check_artifacts(folder, require_provenance=False))
        else:
            record["existing_outputs_read_only"].append({"path": f"runs/{name}", "status": "absent"})


def main() -> int:
    if sys.flags.optimize:
        print("Acceptance validation refuses optimized Python (-O/-OO)", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/validation"),
                        help="New or empty acceptance output directory, relative to project root")
    arguments = parser.parse_args()
    destination = arguments.output if arguments.output.is_absolute() else ROOT / arguments.output
    destination = destination.resolve()
    try:
        check_output_directory(destination)
        destination.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    cache = ROOT / "runs" / ".mplconfig"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    record = {"status": "running", "python_executable": sys.executable,
              "parameter_basis": "Synthetic prototype acceptance, not real-fleet validation"}
    try:
        validate(destination, record)
    except (AssertionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        record.update(status="failed", error=str(error))
        write_json(destination / "validation.json", record)
        print(f"Validation failed: {error}", file=sys.stderr)
        return 1
    record["status"] = "passed"
    write_json(destination / "validation.json", record)
    print(json.dumps({"status": "passed", "unit_tests": record["unit_tests"]["count"],
                      "smoke_runs": 20, "repeated_engine_runs": 40,
                      "output": str(destination)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
