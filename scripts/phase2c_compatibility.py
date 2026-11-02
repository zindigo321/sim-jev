"""Read-only historical 2A/2B compatibility and 18-file scientific regression.

The historical ZIP is read once into memory. Every member selection is anchored
to the original sim-jev/ project root; nested run-source copies are never used
as current source. All generated evidence goes into a new --output directory.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import traceback
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.checkpoint_verify import compare_simulations as compare_legacy, compare_values
from scripts.weekly_verify import compare_simulations as compare_weekly
from sim_jev import Config, Simulation
from sim_jev.provenance import code_fingerprint
from sim_jev.reporting import write_run
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_policy import PolicySpec, RuleSpec


ARCHIVE = Path(r"G:\sim-jev-audit\sim-jev-phase2b-audit-20261102-38b099f3-final.zip")
PROJECT_PREFIX = "sim-jev/"
HISTORY_PREFIX = PROJECT_PREFIX + "runs/phase2b-20261102-170159/"
LEGACY_PREFIX = HISTORY_PREFIX + "01-2a-fixed/archived-examples/"
WEEKLY_PREFIX = HISTORY_PREFIX + "04-final-weekly/"
REPORT_PREFIX = HISTORY_PREFIX + "04-final-validation/"
SCIENCE_FILES = ("config.json", "events.csv", "missions.csv", "states.csv", "repairs.csv",
                 "orders.csv", "summary.json", "state.json", "overview.png")
STOPS = ("0", "0.125", "35.375", "168", "336", "504", "672", "720")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def select_members(names):
    """Exact source/delivery scopes; never match a suffix across nested copies."""
    names = set(names)
    require(PROJECT_PREFIX + "sim_jev/engine.py" in names, "Expected exact sim-jev/ source root is absent")
    legacy = sorted(name for name in names if name.startswith(LEGACY_PREFIX)
                    and PurePosixPath(name).parent.as_posix() + "/" == LEGACY_PREFIX
                    and name.endswith(".checkpoint.json"))
    require(len(legacy) == 16, f"Expected the 16 archived 2A examples, found {len(legacy)}")
    expected_legacy = {f"{LEGACY_PREFIX}{scenario}-0-{stop}.checkpoint.json"
                       for scenario in ("default", "stress") for stop in STOPS}
    require(set(legacy) == expected_legacy, "Archived 2A example names disagree with frozen stops")
    weekly = [f"{WEEKLY_PREFIX}{scenario}-0/pause-{stop}.checkpoint.json"
              for scenario in ("default", "stress") for stop in STOPS]
    weekly += [f"{WEEKLY_PREFIX}stress-0/{kind}.checkpoint.json" for kind in ("in_repair", "in_transit")]
    require(set(weekly) <= names, "Missing representative final 2B checkpoints")
    reports = [f"{REPORT_PREFIX}{scenario}_seed42/{name}"
               for scenario in ("default", "stress") for name in SCIENCE_FILES]
    require(set(reports) <= names, "Missing final historical scientific artifacts")
    return legacy, weekly, reports


def _fresh_from_restored(restored, weekly):
    if weekly:
        state = restored.weekly_state
        return WeeklySimulation(restored.config, restored._input_missions, restored._initial_lifetimes,
                                scenario_id=state["scenario_id"],
                                rules=[RuleSpec.from_dict(rule) for rule in state["rules_input"]],
                                policy=PolicySpec.from_dict(state["policy_spec"]))
    return Simulation(restored.config, restored._input_missions, restored._initial_lifetimes)


def verify_snapshot(raw, member, weekly):
    data = json.loads(raw)
    expected_model = "synthetic-weekly-single-lru-v1" if weekly else "synthetic-single-lru-v1"
    require(data["model_version"] == expected_model, "Historical checkpoint model mismatch")
    restored = (WeeklySimulation if weekly else Simulation).from_checkpoint(data)
    # A historical schema remains itself; no silent normalization/migration.
    require(restored.to_checkpoint() == data, "Restoration changed historical saved state")
    fresh = _fresh_from_restored(restored, weekly)
    compare_values(asdict(fresh.config), asdict(restored.config), "config")
    for name in ("_input_missions", "_initial_lifetimes"):
        left, right = getattr(fresh, name), getattr(restored, name)
        if name == "_input_missions":
            left = None if left is None else [asdict(item) for item in left]
            right = None if right is None else [asdict(item) for item in right]
        compare_values(left, right, name)
    require(restored.config.horizon_hours == 720, "Historical example horizon is not 720")
    fresh.run(720)
    restored.run(720)
    differences = (compare_weekly if weekly else compare_legacy)(fresh, restored)
    return {"member": member, "sha256": sha(raw), "bytes": len(raw), "model_version": expected_model,
            "pause_time": data["time"], "continued_until": 720, "exit_code": 0,
            "operation": "from_checkpoint; to_checkpoint equality; fresh constructor from config/reset inputs; run(720); compare_simulations",
            "complete_business_events_equal": fresh.events == restored.events,
            "configuration_and_reset_inputs_equal": True,
            "events_compared": len(fresh.events), "different_accumulations": differences,
            "summary": restored.summary()}


def verify(output, archive=ARCHIVE):
    if not __debug__:
        raise RuntimeError("Compatibility verification requires assertions; -O is unsupported")
    output, archive = Path(output), Path(archive)
    output.mkdir(parents=True, exist_ok=False)
    initial_source = code_fingerprint(ROOT)
    archive_stat = archive.stat()
    archive_bytes = archive.read_bytes()  # Exactly one filesystem read of the large archive.
    archive_hash = sha(archive_bytes)
    evidence = {"passed": False, "archive": str(archive.resolve()), "archive_sha256": archive_hash,
                "archive_bytes": len(archive_bytes), "archive_filesystem_reads": 1,
                "source_fingerprint_before": initial_source, "source_scope": "current project root; historical source copies excluded",
                "snapshots": [], "science_files": [], "commands": [], "errors": []}
    retained_inputs = output / "archived-inputs"
    retained_inputs.mkdir()
    try:
        with ZipFile(BytesIO(archive_bytes), "r") as zipped:
            names = zipped.namelist()
            require(len(names) == len(set(names)), "Historical archive contains duplicate member names")
            legacy, weekly, reports = select_members(names)
            candidates = sorted(name for name in names if name.startswith(HISTORY_PREFIX)
                                and name.endswith(".checkpoint.json"))
            counts = Counter(name[len(HISTORY_PREFIX):].split("/", 1)[0] for name in candidates)
            inventory = {"archive_members": len(names), "historical_checkpoint_members": len(candidates),
                         "checkpoint_counts_by_historical_directory": dict(sorted(counts.items())),
                         "historical_checkpoint_member_names": candidates,
                         "selected_2a": legacy, "selected_2b": weekly, "selected_scientific_files": reports}
            write_json(output / "archive-inventory.json", inventory)
            evidence["available_selected_2a"] = len(legacy)
            evidence["available_selected_2b"] = len(weekly)
            for is_weekly, selected in ((False, legacy), (True, weekly)):
                model_dir = retained_inputs / ("phase2b" if is_weekly else "phase2a")
                model_dir.mkdir()
                for index, member in enumerate(selected):
                    raw = zipped.read(member)
                    copy_name = f"{index:02d}-{PurePosixPath(member).name}"
                    (model_dir / copy_name).write_bytes(raw)
                    try:
                        record = verify_snapshot(raw, member, is_weekly)
                    except Exception as exc:
                        record = {"member": member, "sha256": sha(raw), "bytes": len(raw), "exit_code": 1,
                                  "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
                        evidence["errors"].append(record["error"])
                    record["retained_input"] = (model_dir / copy_name).relative_to(output).as_posix()
                    evidence["snapshots"].append(record)
                    write_json(output / "compatibility.json", evidence)
            for scenario in ("default", "stress"):
                config_member = PROJECT_PREFIX + f"configs/{scenario}.json"
                config_raw = zipped.read(config_member)
                config = Config.from_dict(json.loads(config_raw))
                require(config.seed == 42, "Historical reference scenario no longer uses seed 42")
                baseline_config = zipped.read(f"{REPORT_PREFIX}{scenario}_seed42/config.json")
                require(json.loads(baseline_config) == config.to_dict(), "Archive root config differs from final scientific reference")
                sim = Simulation(config)
                sim.run(720)
                destination = output / f"{scenario}_seed42"
                write_run(sim, destination, run_context={"kind": "historical-compatibility", "archive_sha256": archive_hash})
                evidence["commands"].append({"operation": "Simulation(archive root config).run(720); write_run", "scenario": scenario,
                                             "config_member": config_member, "config_sha256": sha(config_raw), "exit_code": 0,
                                             "output": destination.relative_to(output).as_posix()})
                for name in SCIENCE_FILES:
                    member = f"{REPORT_PREFIX}{scenario}_seed42/{name}"
                    original = baseline_config if name == "config.json" else zipped.read(member)
                    current = (destination / name).read_bytes()
                    local_original = ROOT / member[len(PROJECT_PREFIX):]
                    local_hash = sha(local_original.read_bytes()) if local_original.is_file() else None
                    record = {"member": member, "archive_sha256": sha(original), "generated_sha256": sha(current),
                              "bytes_equal": original == current, "exit_code": int(original != current),
                              "existing_original_path": str(local_original), "existing_original_present": local_hash is not None,
                              "existing_original_sha256": local_hash,
                              "existing_original_matches_archive": local_hash == sha(original) if local_hash is not None else None}
                    evidence["science_files"].append(record)
                    if not record["bytes_equal"] or record["existing_original_matches_archive"] is False:
                        evidence["errors"].append(f"Scientific byte regression: {member}")
            argv = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_checkpoint_audit_fixes.py", "-v"]
            process = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
            (output / "phase2a-counterexamples.txt").write_text(process.stdout + process.stderr, encoding="utf-8")
            evidence["commands"].append({"argv": argv, "exit_code": process.returncode, "log": "phase2a-counterexamples.txt",
                                         "counterexample_classes": ["initial mission start identity/order", "repair duration support", "order ETA support"]})
            if process.returncode:
                evidence["errors"].append("Historical 2A counterexample regression failed")
        final_stat = archive.stat()
        evidence["archive_stat_unchanged"] = (archive_stat.st_size, archive_stat.st_mtime_ns) == (final_stat.st_size, final_stat.st_mtime_ns)
        evidence["source_fingerprint_after"] = code_fingerprint(ROOT)
        evidence["source_unchanged_during_validation"] = initial_source == evidence["source_fingerprint_after"]
        if not evidence["archive_stat_unchanged"]:
            evidence["errors"].append("Historical archive stat changed during validation")
        if not evidence["source_unchanged_during_validation"]:
            evidence["errors"].append("Current source changed during compatibility validation; rerun with frozen source")
        evidence["snapshots_passed"] = sum(record["exit_code"] == 0 for record in evidence["snapshots"])
        evidence["scientific_files_byte_identical"] = sum(record["bytes_equal"] for record in evidence["science_files"])
        evidence["passed"] = not evidence["errors"] and evidence["snapshots_passed"] == 34 and evidence["scientific_files_byte_identical"] == 18
    except Exception as exc:
        evidence["errors"].append(f"{type(exc).__name__}: {exc}")
        evidence["traceback"] = traceback.format_exc()
    write_json(output / "compatibility.json", evidence)
    print(json.dumps({"passed": evidence["passed"], "output": str(output),
                      "snapshots_passed": evidence.get("snapshots_passed", 0),
                      "scientific_files_byte_identical": evidence.get("scientific_files_byte_identical", 0),
                      "errors": evidence["errors"]}, ensure_ascii=False, indent=2))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    raise SystemExit(verify(options.output, options.archive))
