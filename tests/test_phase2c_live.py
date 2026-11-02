"""Runner budgets, strict failures and offline recovery never promote fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.phase2c_experiment import audited_runtime, run_case
from scripts.phase2c_live import (checkpoint_projection, import_response_evidence, main,
    read_json, replay_stage, response_evidence, resume_cached, run_stage, _request_accounting)
from sim_jev.jev import ChoiceAdapter, JevConfig, JevError, MODEL

ROOT = Path(__file__).resolve().parents[1]


def fixture_response(payload, input_tokens=11, output_tokens=2):
    ids = sorted(payload["questions"]["portfolio"]["criteria"])
    return {"model": MODEL, "answers": {"portfolio": {"choice": ids[0],
        "probabilities": {key: 1 / len(ids) for key in ids}, "confidence": .5}},
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


def fixture_transport(payload, credential, timeout):
    return 200, {}, fixture_response(payload)


class LiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sim-jev-live-runner-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-credential"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def adapter(self, *, max_calls=24, timeout=1800, transport=fixture_transport, clock=None):
        options = {"transport": transport}
        if clock is not None:
            options["clock"] = clock
        adapter = ChoiceAdapter(JevConfig(mode="live", cache_dir=str(self.root / "store"),
            allow_live=True, max_http_requests=max_calls, total_timeout_seconds=timeout), **options)
        runtime, counter = audited_runtime("live", adapter=adapter)
        return adapter, runtime, counter

    def case(self, method, folder, runtime, counter, scenario="balanced", **extra):
        return run_case(scenario, 0, method, self.root / folder, mode="live", runtime=runtime,
            counter=counter, strict_jev=True, phase="fixture-pilot", save_resume=True,
            target_size=4, top_k=2, scenarios=1, **extra)

    def test_missing_key_exits_two_and_preserves_empty_stage_evidence(self):
        with patch.dict(os.environ, {}, clear=True):
            code = main(["fixed-state", "--allow-live", "--max-http-requests", "3",
                         "--total-timeout-seconds", "120", "--output", str(self.root / "missing")])
        self.assertEqual(code, 2)
        record = read_json(self.root / "missing" / "stage.json")
        self.assertEqual(record["status"], "not_run")
        self.assertEqual(record["actual_http_requests"], 0)
        self.assertFalse(record["jev_live_fixed_verified"])
        self.assertEqual(read_json(self.root / "missing" / "attempt-ledger.json")["attempts"], [])
        self.assertTrue((self.root / "missing" / "usage-accounting.json").is_file())
        self.assertFalse((self.root / "missing" / "runs").exists())

    def test_live_limits_and_fixed_design_cannot_silently_expand(self):
        for args in (["fixed-state", "--max-http-requests", "0", "--total-timeout-seconds", "120"],
                     ["fixed-state", "--max-http-requests", "4", "--total-timeout-seconds", "120"],
                     ["pilot", "--max-http-requests", "25", "--total-timeout-seconds", "1800"]):
            self.assertEqual(main([*args, "--allow-live", "--output", str(self.root / "bad")]), 1)
        with self.assertRaises(SystemExit) as stopped:
            main(["pilot", "--seeds", "1", "--allow-live", "--max-http-requests", "24",
                  "--total-timeout-seconds", "1800", "--output", str(self.root / "bad")])
        self.assertEqual(stopped.exception.code, 2)
        self.assertFalse((self.root / "bad").exists())

    def test_fixed_stage_one_shared_response_counts_retry_usage_once_and_replays_all_branches(self):
        calls = []
        def transport(payload, credential, timeout):
            calls.append(payload)
            if len(calls) == 1:
                return 429, {}, {"usage": {"input_tokens": 7, "output_tokens": 1}}
            return 200, {}, fixture_response(payload)
        result = run_stage("fixed-state", self.root / "fixed", allow_live=True, max_http_requests=3,
                           total_timeout_seconds=120, transport=transport, sleeper=lambda _: None)
        self.assertEqual(result["status"], "fixture_passed", result)
        self.assertEqual(result["actual_http_requests"], 2)
        self.assertFalse(result["jev_live_fixed_verified"])
        self.assertEqual(result["accounting"]["actual_usage"], {"input_tokens": 18, "output_tokens": 3})
        self.assertEqual(result["accounting"]["cache_hits"], 1)
        self.assertEqual(result["actual_des"], 128)
        self.assertEqual(result["branch_replay"]["audit_replay_des"], 128)
        self.assertEqual(result["branch_replay"]["decision_des_added"], 0)
        self.assertEqual(len(calls), 2)
        with self.assertRaises(ValueError):
            replay_stage(self.root / "fixed", self.root / "must-not-promote")
        with self.assertRaises(ValueError):
            import_response_evidence(read_json(self.root / "fixed" / "response-evidence.json"), self.root / "bad-store")

    def test_shared_adapter_run_audit_slices_and_baseline_do_not_duplicate_http(self):
        adapter, runtime, counter = self.adapter()
        base, _ = self.case("mandatory-first", "baseline", runtime, counter)
        self.assertEqual(adapter.http_requests, 0)
        first, _ = self.case("jev-direct", "first", runtime, counter)
        self.assertEqual(first["actual_http_requests"], 5)
        second, _ = self.case("jev-direct", "second", runtime, counter)
        self.assertEqual(second["actual_http_requests"], 0)
        self.assertEqual(second["actual_input_tokens"], 0)
        self.assertEqual(second["recorded_response_input_tokens"], 55)
        self.assertEqual(len(read_json(self.root / "first" / "api-audit.json")), 5)
        self.assertEqual(len(read_json(self.root / "second" / "api-audit.json")), 5)
        audit_root = self.root / "accounting"
        audit_root.mkdir()
        summary = _request_accounting(adapter, audit_root)
        self.assertEqual(summary["actual_http_requests"], 5)
        self.assertEqual(summary["actual_usage"], {"input_tokens": 55, "output_tokens": 10})
        self.assertEqual(summary["cache_hits"], 5)
        self.assertEqual({row["context"]["run"] for row in adapter.ledger["attempts"]}, {"balanced/seed-0/jev-direct"})
        self.assertEqual(len({row["context"]["decision"] for row in adapter.ledger["attempts"]}), 5)

    def test_shared_budget_exhaustion_stops_second_case_and_persists_failure(self):
        adapter, runtime, counter = self.adapter(max_calls=5)
        self.case("jev-direct", "first", runtime, counter)
        with self.assertRaises(JevError) as error:
            self.case("jev-direct", "second", runtime, counter, scenario="scarce")
        self.assertEqual(error.exception.code, "http_budget_exhausted")
        self.assertEqual(adapter.http_requests, 5)
        self.assertEqual(read_json(self.root / "second" / "failed-run.json")["error_code"], "http_budget_exhausted")

    def test_expired_global_deadline_allows_completed_cache_but_no_new_case(self):
        now = [1000.]
        adapter, runtime, counter = self.adapter(timeout=10, clock=lambda: now[0])
        self.case("jev-direct", "first", runtime, counter)
        now[0] += 11
        repeated, _ = self.case("jev-direct", "cached", runtime, counter)
        self.assertEqual(repeated["actual_http_requests"], 0)
        with self.assertRaises(JevError) as error:
            self.case("jev-direct", "expired", runtime, counter, scenario="scarce")
        self.assertEqual(error.exception.code, "total_timeout")
        self.assertEqual(adapter.http_requests, 5)

    def test_systemic_error_stops_stage_and_never_reports_completed(self):
        result = run_stage("fixed-state", self.root / "unauthorized", allow_live=True,
            max_http_requests=3, total_timeout_seconds=120,
            transport=lambda *args: (401, {}, {"dangerous_error": "do not save arbitrary server text"}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["actual_http_requests"], 1)
        self.assertFalse(result["jev_live_fixed_verified"])
        self.assertEqual(result["accounting"]["unknown_usage_attempts"], 1)
        self.assertNotIn("dangerous_error", json.dumps(result))
        self.assertFalse((self.root / "unauthorized" / "runs" / "jev-des.json").exists())

    def test_cache_only_new_process_preserves_live_identity_and_original_budget(self):
        adapter, runtime, counter = self.adapter()
        self.case("jev-des", "original", runtime, counter)
        original_ledger = read_json(self.root / "store" / "http-budget.json")
        environment = dict(os.environ)
        environment.pop("TYPESAFE_API_KEY", None)
        command = [sys.executable, "scripts/phase2c_live.py", "resume", "--fixture-cache-only",
            "--checkpoint", str(self.root / "original" / "resume-point.json"),
            "--expected-checkpoint", str(self.root / "original" / "checkpoint.json"),
            "--response-store", str(self.root / "store"), "--max-http-requests", "24",
            "--total-timeout-seconds", "1800", "--output", str(self.root / "resumed")]
        process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                 text=True, encoding="utf-8", timeout=60)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = read_json(self.root / "resumed" / "resume.json")
        self.assertEqual(result["actual_http_requests"], 0)
        self.assertEqual(result["cache_hits"], 4)
        self.assertEqual(result["historical_policy_mode"], "live")
        self.assertEqual(original_ledger, read_json(self.root / "store" / "http-budget.json"))
        resumed = read_json(self.root / "resumed" / "run" / "checkpoint.json")
        self.assertEqual(resumed["weekly_state"]["policy_spec"]["jev_mode"], "live")
        self.assertTrue(all(not record["jev"]["valid_jev"] for record in resumed["weekly_state"]["decision_records"]))

    def test_cache_only_missing_future_response_fails_without_transport(self):
        adapter, runtime, counter = self.adapter()
        self.case("jev-des", "original", runtime, counter)
        cached = sorted((self.root / "store").glob("*.fixture.json"))
        # Remove only our temporary test cache, choosing a future request.
        first_hash = read_json(self.root / "original" / "resume-point.json")["weekly_state"]["decision_records"][0]["jev"]["request_sha256"]
        next(path for path in cached if path.stem.split(".")[0] != first_hash).unlink()
        calls = []
        result = resume_cached(self.root / "original" / "resume-point.json",
            self.root / "original" / "checkpoint.json", self.root / "store", self.root / "missing-future",
            max_http_requests=24, total_timeout_seconds=1800, transport=lambda *args: calls.append(args))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "cache_only_miss")
        self.assertEqual(calls, [])

    def test_projection_retains_configuration_seed_request_response_and_obligations(self):
        adapter, runtime, counter = self.adapter()
        self.case("jev-direct", "projection", runtime, counter)
        original = read_json(self.root / "projection" / "checkpoint.json")
        normalized = checkpoint_projection(original)
        self.assertNotIn("jev_mode", normalized["weekly_state"]["policy_spec"])
        self.assertIn("planning_seed", normalized["weekly_state"]["policy_spec"])
        self.assertEqual(normalized["config"], original["config"])
        record = normalized["weekly_state"]["decision_records"][0]
        self.assertEqual(record["jev"]["response"], original["weekly_state"]["decision_records"][0]["jev"]["response"])
        self.assertEqual(normalized["weekly_state"]["cohorts"], original["weekly_state"]["cohorts"])


if __name__ == "__main__":
    unittest.main()
