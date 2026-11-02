"""Real checkpoint acceptance, including separate pause/resume CLI processes.

Comparison tolerates only accumulated scientific floats; event times, business
logs, categories, IDs, queue order and integer accounting remain exact.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

if not __debug__:
    raise RuntimeError("checkpoint verification requires assertions; optimized Python (-O) is unsupported")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim_jev import Simulation
from sim_jev.config import load_config

REL_TOL = 1e-12
ABS_TOL = 1e-10
ACCUMULATED = {
    "flight_hours", "spare_wait_hours", "bay_wait_hours", "total_repair_wait_hours",
    "available_aircraft_hours", "idle_aircraft_hours", "availability", "idle_fraction",
}


def compare_values(expected, actual, path="root", differences=None):
    """Strict structure/discrete data, documented tolerance on integrals only."""
    differences = [] if differences is None else differences
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and expected.keys() == actual.keys(), path
        for key in expected:
            compare_values(expected[key], actual[key], f"{path}.{key}", differences)
    elif isinstance(expected, (list, tuple)):
        assert type(actual) is type(expected) and len(expected) == len(actual), path
        for index, (left, right) in enumerate(zip(expected, actual)):
            compare_values(left, right, f"{path}[{index}]", differences)
    elif path.rsplit(".", 1)[-1] in ACCUMULATED and isinstance(expected, (int, float)):
        assert math.isclose(expected, actual, rel_tol=REL_TOL, abs_tol=ABS_TOL), (path, expected, actual)
        if expected != actual:
            differences.append({"path": path, "expected": expected, "actual": actual,
                                "absolute_difference": abs(expected - actual)})
    else:
        assert expected == actual and type(expected) is type(actual), (path, expected, actual)
    return differences


def normalized_history(sim):
    """Remove ONLY extra, unchanged, non-business-time pause samples.

    Preserve initial/final rows, every row at any processed event time (including
    stale events), all state changes, and all same-time rows. Never normalize logs.
    """
    business_times = {row["time"] for row in sim.events}
    rows = sim.history
    result = []
    for index, row in enumerate(rows):
        previous = rows[index - 1] if index else None
        pure_sample = (0 < index < len(rows) - 1 and row["time"] not in business_times
                       and previous["time"] != row["time"] != rows[index + 1]["time"]
                       and {k: v for k, v in row.items() if k != "time"}
                       == {k: v for k, v in previous.items() if k != "time"})
        if not pure_sample:
            result.append(row)
    return result


def compare_simulations(expected, actual):
    # Reset inputs are part of the resumable experiment, even when they have
    # not affected the current trajectory. Keep None distinct from empty input.
    compare_values(asdict(expected.config), asdict(actual.config), "config")
    for name in ("_input_missions", "_initial_lifetimes"):
        left, right = getattr(expected, name), getattr(actual, name)
        if name == "_input_missions":
            left = None if left is None else [asdict(m) for m in left]
            right = None if right is None else [asdict(m) for m in right]
        compare_values(left, right, name)
    assert expected.events == actual.events, "complete ordered events/log IDs differ"
    assert normalized_history(expected) == normalized_history(actual), "state trajectory differs"
    differences = compare_values(expected.summary(), actual.summary(), "summary")
    for name in ("aircraft", "missions", "orders", "repairs"):
        differences.extend(compare_values(
            {key: asdict(value) for key, value in getattr(expected, name).items()},
            {key: asdict(value) for key, value in getattr(actual, name).items()}, name))
    compare_values([asdict(e) for e in sorted(expected._queue)],
                   [asdict(e) for e in sorted(actual._queue)], "event_queue")
    assert expected._sequence == actual._sequence
    assert expected.on_hand == actual.on_hand
    assert expected.now == actual.now
    for name in expected._rngs:
        assert expected._rngs[name].bit_generator.state == actual._rngs[name].bit_generator.state, name
    return differences


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False) + "\n", encoding="utf-8")


def run_command(argv, destination, name, records):
    result = subprocess.run(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    (destination / f"{name}.log").write_text(result.stdout, encoding="utf-8")
    records.append({"argv": argv, "exit_code": result.returncode, "log": f"{name}.log"})
    write_json(destination / "commands.json", records)
    if result.returncode:
        raise RuntimeError(f"{name} exited {result.returncode}: {result.stdout}")


def verify(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    records, outcomes = [], []
    for scenario in ("default", "stress"):
        config_path = ROOT / "configs" / f"{scenario}.json"
        config = load_config(config_path)
        for seed in range(10):
            config = replace(config, seed=seed)
            whole = Simulation(config)
            whole.run()
            branch = Simulation(config)
            for stop in (0, 0.125, 35.375, 168, 336, 504, 672, 720):
                branch.run(until=stop)
                path = destination / f"{scenario}-{seed}-{stop}.checkpoint.json"
                branch.save_checkpoint(path)
                branch = Simulation.load_checkpoint(path)
                if seed == 0:
                    final_path = destination / f"{scenario}-{seed}-{stop}-process-final.json"
                    code = ("from sim_jev import Simulation; import sys; "
                            "s=Simulation.load_checkpoint(sys.argv[1]); s.run(); "
                            "s.save_checkpoint(sys.argv[2]); print(s.summary())")
                    run_command([sys.executable, "-c", code, str(path), str(final_path)],
                                destination, f"{scenario}-{seed}-{stop}-process", records)
                    compare_simulations(whole, Simulation.load_checkpoint(final_path))
            differences = compare_simulations(whole, branch)
            outcomes.append({"scenario": scenario, "seed": seed, "stops": 8,
                             "different_accumulations": differences,
                             "events": len(whole.events), "summary": branch.summary()})
        # These are three genuinely separate interpreter processes. The pause
        # process exits before the resume process is created.
        for mode, extra in (
            ("full", ["--config", str(config_path), "--seed", "42"]),
            ("paused", ["--config", str(config_path), "--seed", "42", "--until", "168"]),
            ("resumed", ["--resume", str(destination / f"{scenario}-paused.checkpoint.json")]),
        ):
            argv = [sys.executable, "-m", "sim_jev", *extra,
                    "--output", str(destination / f"{scenario}-{mode}"),
                    "--checkpoint-out", str(destination / f"{scenario}-{mode}.checkpoint.json")]
            run_command(argv, destination, f"{scenario}-{mode}", records)
        full = Simulation.load_checkpoint(destination / f"{scenario}-full.checkpoint.json")
        resumed = Simulation.load_checkpoint(destination / f"{scenario}-resumed.checkpoint.json")
        differences = compare_simulations(full, resumed)
        # Full CSV logs are byte identical, with continuous IDs; no filtering.
        assert ((destination / f"{scenario}-full/events.csv").read_bytes()
                == (destination / f"{scenario}-resumed/events.csv").read_bytes())
        outcomes.append({"scenario": scenario, "seed": 42, "cross_process": True,
                         "different_accumulations": differences})
    write_json(destination / "verification.json", {
        "passed": True, "rel_tol": REL_TOL, "abs_tol": ABS_TOL,
        "smoke_pairs": 20, "roundtrips": 160, "cross_process_pairs": 18,
        "results": outcomes,
    })
    hashes = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(destination.rglob("*")) if p.is_file()}
    write_json(destination / "output-sha256.json", hashes)
    print(json.dumps({"passed": True, "output": str(destination), "smoke_pairs": 20,
                      "cross_process_pairs": 18}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New verification directory")
    verify(parser.parse_args().output)
