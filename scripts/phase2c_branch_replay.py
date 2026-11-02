"""Rebuild persisted branch evidence from legal inputs without HTTP or selection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev.branch_replay import replay_decision_branches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="Single decision, decisions.json, or complete weekly checkpoint")
    parser.add_argument("--selection", choices=["all", "first-per-decision"], default="all")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("replay output must be a new directory")
    args.output.mkdir(parents=True)
    report = {"status": "running", "actual_http_requests": 0, "command": sys.orig_argv, "records": []}
    code = 1
    try:
        value = json.loads(args.input.read_text(encoding="utf-8"))
        if "weekly_state" in value:
            from sim_jev import WeeklySimulation
            value = WeeklySimulation.from_checkpoint(value).weekly_state["decision_records"]
        elif "records" in value:
            value = value["records"]
        elif isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            raise ValueError("input must contain recorded decisions")
        for index, record in enumerate(value):
            report["records"].append(replay_decision_branches(
                record, selection=args.selection, artifact_dir=args.output / f"decision-{index:03d}"))
        report.update(status="passed", audit_replay_des=sum(row["audit_replay_des"] for row in report["records"]),
                      audit_replay_simulated_hours=sum(row["audit_replay_simulated_hours"] for row in report["records"]),
                      audit_replay_wall_seconds=sum(row["audit_replay_wall_seconds"] for row in report["records"]))
        code = 0
    except (ValueError, KeyError, TypeError, OSError, AssertionError) as exc:
        report.update(status="failed", error=str(exc))
    report["exit_code"] = code
    (args.output / "audit-replay.json").write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                            allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "records"}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
