"""Compare the frozen 60-run offline experiment with a read-only historical ZIP.

Restore every old 2C checkpoint without rewriting its records. Scientific bytes
are compared directly; runtime metadata is reported separately, never normalized
to disguise a changed physical model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev.weekly import WeeklySimulation
from sim_jev.provenance import code_fingerprint
from scripts.weekly_verify import audit_business_log

SCIENCE_FILES = ("config.json", "events.csv", "missions.csv", "orders.csv", "repairs.csv",
                 "states.csv", "summary.json", "policy-observation.json", "random-manifest.json")
PREFIX = "project/runs/phase2c-20261102/pilot-final/"


def verify(archive: Path, experiment: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    source = code_fingerprint()
    before = archive.stat()
    result = {"schema": "phase2c-science-regression-v1", "status": "running",
              "archive": str(archive.resolve()), "experiment": str(experiment.resolve()),
              "source_fingerprint": source, "science_files": [], "checkpoints": [],
              "actual_http_requests": 0, "errors": []}
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = sorted(name for name in bundle.namelist()
                           if name.startswith(PREFIX) and name.endswith("/checkpoint.json"))
            if len(names) != 60:
                raise ValueError("Expected exactly 60 historical experiment checkpoints")
            for member in names:
                relative = member[len(PREFIX):]
                original = json.loads(bundle.read(member))
                restored = WeeklySimulation.from_checkpoint(original)
                if restored.to_checkpoint() != original:
                    raise ValueError(f"Historical checkpoint changed on restore: {relative}")
                restored.run(720)
                audit_business_log(restored)
                current = json.loads((experiment / relative).read_text(encoding="utf-8"))
                if restored.summary() != WeeklySimulation.from_checkpoint(current).summary():
                    raise ValueError(f"Historical and current metrics differ: {relative}")
                retained = output / "historical-2c-checkpoints" / relative
                retained.parent.mkdir(parents=True, exist_ok=True)
                retained.write_bytes(bundle.read(member))
                result["checkpoints"].append({"member": member, "unchanged_roundtrip": True,
                                               "continued_summary_equal": True,
                                               "sha256": hashlib.sha256(bundle.read(member)).hexdigest()})
                directory = Path(relative).parent
                for filename in SCIENCE_FILES:
                    path = directory / filename
                    old = bundle.read(PREFIX + path.as_posix())
                    new = (experiment / path).read_bytes()
                    equal = old == new
                    result["science_files"].append({"path": path.as_posix(), "bytes_equal": equal,
                        "old_sha256": hashlib.sha256(old).hexdigest(),
                        "new_sha256": hashlib.sha256(new).hexdigest()})
                    if not equal:
                        result["errors"].append("Scientific bytes changed: " + path.as_posix())
        after = archive.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            result["errors"].append("Historical archive stat changed")
        if source != code_fingerprint():
            result["errors"].append("Source changed during verification")
    except (ValueError, AssertionError, OSError, KeyError, zipfile.BadZipFile) as error:
        result["errors"].append(str(error))
    result.update(status="passed" if not result["errors"] else "failed",
                  restored_checkpoints=len(result["checkpoints"]),
                  identical_science_files=sum(row["bytes_equal"] for row in result["science_files"]))
    (output / "comparison.json").write_text(json.dumps(result, indent=2, ensure_ascii=False,
                                                       allow_nan=False) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.archive, args.experiment, args.output)
    print(json.dumps({key: result[key] for key in ("status", "restored_checkpoints",
                                                  "identical_science_files", "errors")}, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
