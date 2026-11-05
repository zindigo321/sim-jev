"""Actual CLI/new-process recovery and conservative journal crash boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sim_jev.branch_audit_store import BranchAuditStore, read_json
from sim_jev.decision_resolver import logical_decision_record
from sim_jev.planning import PlanningScenario
from sim_jev.weekly import WeeklySimulation
from scripts.phase2d_local import run_pilot_case, save
from scripts.planning_verify import audit_planning_branch

ROOT = Path(__file__).resolve().parents[1]


def cli(folder, until):
    p = subprocess.run([sys.executable, str(ROOT / "scripts/phase2d_local.py"),
                        "recovery-run", "--output", str(folder), "--until", str(until)],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if p.returncode:
        raise AssertionError(p.stdout + p.stderr)
    return json.loads(p.stdout)


class ResumeCLIRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.resumed = cls.root / "resumed"
        cls.first = cli(cls.resumed, 168)
        cls.old_audits = read_json(cls.resumed / "branch-audits.json")
        cls.old_attempts = sorted(p.name for p in (cls.resumed / "branch-audit-store/attempts").glob("*.json"))
        cls.middle = cli(cls.resumed, 336)
        cls.final = cli(cls.resumed, 720)
        cls.continuous = cls.root / "continuous"
        cli(cls.continuous, 720)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_actual_writer_new_process_exports_next_checkpoint(self):
        self.assertEqual(self.first["result"]["logical_branches"], 64)
        self.assertEqual(self.middle["result"]["logical_branches"], 96)
        left = read_json(self.continuous / "checkpoint.json")
        right = read_json(self.resumed / "checkpoint.json")
        # Only measured execution wall time differs. Request, pools, scenarios,
        # commands, complete events, counters and all business ledgers are exact.
        for value in (left, right):
            value["weekly_state"]["decision_records"] = [
                logical_decision_record(r) for r in value["weekly_state"]["decision_records"]]
        self.assertEqual(left, right)
        restored = WeeklySimulation.from_checkpoint(read_json(self.resumed / "checkpoint.json"))
        save(self.root / "next-checkpoint.json", restored.to_checkpoint())
        self.assertEqual(list(read_json(self.root / "next-checkpoint.json")["weekly_state"]["commitments"]),
                         list(restored.weekly_state["commitments"]))

    def test_64_to_160_preserves_all_stable_audit_identities(self):
        final = read_json(self.resumed / "branch-audits.json")
        accounting = read_json(self.resumed / "branch-accounting.json")
        self.assertEqual((accounting["logical_branches"], accounting["actual_compute_attempts"],
                          accounting["persistent_audit_identities"]), (160, 160, 160))
        self.assertEqual(len({x["audit_identity"] for x in final}), 160)
        by_key = {x["branch_key"]: x for x in final}
        for old in self.old_audits:
            self.assertEqual(by_key[old["branch_key"]], old)
        self.assertTrue(set(self.old_attempts) <= {p.name for p in
                        (self.resumed / "branch-audit-store/attempts").glob("*.json")})
        # Stable audit identity includes physical output but excludes timing and
        # attempt UUID, so an independent continuous run has the same identities.
        self.assertEqual({a["audit_identity"] for a in final},
                         {a["audit_identity"] for a in read_json(self.continuous / "branch-audits.json")})

    def test_existing_result_and_repeated_resume_do_not_rerun(self):
        before = read_json(self.resumed / "branch-audits.json")
        with patch.object(PlanningScenario, "evaluate", side_effect=AssertionError("no DES repeat")), \
                patch.object(WeeklySimulation, "run", side_effect=AssertionError("no execution repeat")):
            for _ in range(2):
                row, detail = run_pilot_case(self.resumed, "balanced", 30, "heuristic-des", None)
                self.assertEqual(row["actual_des"], 160)
                self.assertEqual(len(detail), 5)
        self.assertEqual(before, read_json(self.resumed / "branch-audits.json"))

    def test_existing_result_cannot_hide_missing_audit(self):
        copied = self.root / "corrupted-evidence-copy"
        shutil.copytree(self.resumed, copied)
        next((copied / "branch-audit-store/audits").glob("*.json")).unlink()
        with self.assertRaisesRegex(ValueError, "pending/uncertain|missing"):
            run_pilot_case(copied, "balanced", 30, "heuristic-des", None)

    def test_retained_artifact_belongs_to_selected_candidate_scenario_zero(self):
        checkpoint = read_json(self.resumed / "checkpoint.json")
        for record in checkpoint["weekly_state"]["decision_records"]:
            selected = next(b for b in record["branches"]
                            if b["candidate_id"] == record["selected_candidate_id"] and b["scenario_index"] == 0)
            artifact = read_json(self.resumed / ("window-" + str(int(record["request"]["context"]["window_start"]))) / "branch-0.json")
            self.assertEqual(artifact["command"], record["command"])
            self.assertEqual(artifact["scenario_id"], selected["scenario_id"])


class CrashBoundaryTests(unittest.TestCase):
    def crash_at(self, folder, point):
        def crash(name):
            if name == point:
                raise RuntimeError("injected crash: " + point)
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            run_pilot_case(folder, "balanced", 30, "heuristic-des", None, until=0, fault_hook=crash)

    def test_before_audit_write_preserves_pending_and_blocks_refund_or_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self.crash_at(folder, "before_audit_write")
            attempts = list((folder / "branch-audit-store/attempts").glob("*.json"))
            self.assertEqual(len(attempts), 1)
            self.assertEqual(read_json(attempts[0])["status"], "pending")
            with patch.object(PlanningScenario, "evaluate", side_effect=AssertionError("must stop before DES")), \
                    self.assertRaisesRegex(ValueError, "pending/uncertain"):
                run_pilot_case(folder, "balanced", 30, "heuristic-des", None, until=0)
            self.assertEqual(read_json(attempts[0])["status"], "pending")

    def test_after_audit_write_is_classified_completed_but_not_reselected(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self.crash_at(folder, "after_audit_write")
            with patch.object(PlanningScenario, "evaluate", side_effect=AssertionError("must not repeat")), \
                    self.assertRaisesRegex(ValueError, "uncommitted audited"):
                run_pilot_case(folder, "balanced", 30, "heuristic-des", None, until=0)
            attempt = read_json(next((folder / "branch-audit-store/attempts").glob("*.json")))
            self.assertEqual(attempt["status"], "completed")
            self.assertTrue(attempt["recovered_from_complete_audit"])

    def test_before_checkpoint_commit_keeps_uncommitted_audits_and_stops(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self.crash_at(folder, "before_checkpoint_write")
            self.assertFalse((folder / "checkpoint.json").exists())
            self.assertEqual(len(list((folder / "branch-audit-store/audits").glob("*.json"))), 32)
            with patch.object(PlanningScenario, "evaluate", side_effect=AssertionError("must not repeat")), \
                    self.assertRaisesRegex(ValueError, "uncommitted audited"):
                run_pilot_case(folder, "balanced", 30, "heuristic-des", None, until=0)

    def test_after_checkpoint_write_recovers_commit_marker_without_recomputation(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            self.crash_at(folder, "after_checkpoint_write")
            with patch.object(PlanningScenario, "evaluate", side_effect=AssertionError("must not repeat")):
                row, detail = run_pilot_case(folder, "balanced", 30, "heuristic-des", None, until=0)
            self.assertEqual(row["logical_branches"], 32)
            self.assertEqual(row["actual_compute_attempts"], 32)
            self.assertEqual(read_json(folder / "branch-audit-store/checkpoint-commit.json")["status"], "committed")

    def test_completed_attempt_cannot_be_reserved_twice(self):
        # Covers the actual resolver hook and never allows a second evaluator
        # invocation after a computation has already been durably attempted.
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            run_pilot_case(folder, "balanced", 30, "heuristic-des", None, until=0)
            checkpoint = read_json(folder / "checkpoint.json")
            record = checkpoint["weekly_state"]["decision_records"][0]
            from sim_jev.decision import DecisionRequest
            from sim_jev.candidates import Candidate
            from sim_jev.weekly_policy import WeeklyCommand
            from sim_jev.planning import PlanningInput
            req = DecisionRequest.from_dict(record["request"])
            branch = record["branches"][0]
            item = next(c for c in record["pool"] if c["id"] == branch["candidate_id"])
            candidate = Candidate(item["id"], WeeklyCommand.from_dict(item["command"]), item["summary"])
            scenario = PlanningScenario(PlanningInput(req, 20261102, branch["scenario_index"]))
            journal = BranchAuditStore(folder, audit_planning_branch)
            with self.assertRaisesRegex(ValueError, "already attempted"):
                journal.before_branch(req, candidate, branch["scenario_index"], scenario)


if __name__ == "__main__":
    unittest.main()
