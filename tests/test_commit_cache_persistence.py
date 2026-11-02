"""Crash consistency of response persistence; all HTTP and credentials are fake."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev import jev
from sim_jev.jev import ChoiceAdapter, JevConfig, JevError, MODEL


def payload():
    return {"model": MODEL, "state": {"case": "cache-persistence-fixture"},
            "questions": {"portfolio": {"type": "choice", "instructions": "fixture",
                                        "criteria": {"A": {}, "B": {}}}}}


def response():
    return {"model": MODEL, "answers": {"portfolio": {
        "choice": "A", "probabilities": {"A": .8, "B": .2}, "confidence": .5}},
        "usage": {"input_tokens": 11, "output_tokens": 2}}


def config(folder):
    return JevConfig(mode="live", cache_dir=str(folder), allow_live=True,
                     max_http_requests=1, total_timeout_seconds=120, max_retries=0)


def crash_worker(folder, when):
    """Exit without finally/exception handling at the actual cache-write seam."""
    os.environ["TYPESAFE_API_KEY"] = "LOCAL_FAKE_CACHE_TEST_TOKEN"
    original = jev._atomic_json

    def interrupted_write(path, value):
        if path.name.endswith(".fixture.json"):
            if when == "before":
                os._exit(73)
            original(path, value)
            os._exit(74)
        return original(path, value)

    jev._atomic_json = interrupted_write
    ChoiceAdapter(config(folder), transport=lambda *args: (200, {}, response())).rank(payload())
    raise AssertionError("crash worker did not exit")


class CachePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jev-cache-crash-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        environment = patch.dict(os.environ, {"TYPESAFE_API_KEY": "LOCAL_FAKE_CACHE_TEST_TOKEN"})
        environment.start()
        self.addCleanup(environment.stop)

    def budget(self, folder=None):
        return json.loads(((folder or self.folder) / "http-budget.json").read_text(encoding="utf-8"))

    def crash(self, when):
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                  "--crash-worker", str(self.folder), when],
                                 cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 73 if when == "before" else 74,
                         process.stdout + process.stderr)
        row = self.budget()["attempts"][0]
        self.assertEqual(row["status"], "response_pending")
        self.assertTrue(row["uncertain"])
        self.assertEqual(row["known_usage"], response()["usage"])
        return self.budget()

    def test_success_is_not_persisted_until_the_cache_is_durable(self):
        original, inspected = jev._atomic_json, []

        def inspect_write(path, value):
            if path.name.endswith(".fixture.json"):
                row = self.budget()["attempts"][0]
                inspected.append(row["status"])
                self.assertTrue(row["uncertain"])
                self.assertEqual(row["known_usage"], response()["usage"])
            return original(path, value)

        with patch.object(jev, "_atomic_json", inspect_write):
            adapter = ChoiceAdapter(config(self.folder), transport=lambda *args: (200, {}, response()))
            result = adapter.rank(payload())
        self.assertEqual(inspected, ["response_pending"])
        self.assertEqual(result["actual_usage"], response()["usage"])
        self.assertEqual(self.budget()["attempts"][0]["status"], "success")
        self.assertFalse(self.budget()["attempts"][0]["uncertain"])

    def test_process_death_before_cache_keeps_quota_usage_and_uncertainty(self):
        original = self.crash("before")
        self.assertEqual(list(self.folder.glob("*.fixture.json")), [])
        calls = []
        adapter = ChoiceAdapter(replace(config(self.folder), network_policy="cache_only", allow_live=False),
                                transport=lambda *args: calls.append(args))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(JevError) as caught:
            adapter.rank(payload())
        self.assertEqual(caught.exception.code, "cache_only_miss")
        self.assertEqual(caught.exception.audit["prior_uncertain_attempts"], [1])
        self.assertEqual(calls, [])
        self.assertEqual(adapter.http_requests, 1)
        self.assertEqual(adapter.ledger["started_at_epoch"], original["started_at_epoch"])
        self.assertEqual(adapter.usage_summary()["actual_usage"], response()["usage"])
        self.assertEqual(adapter.ledger["attempts"][0]["status"], "response_not_persisted")
        restarted = ChoiceAdapter(config(self.folder), transport=lambda *args: calls.append(args))
        with self.assertRaises(JevError) as caught:
            restarted.rank(payload())
        self.assertEqual(caught.exception.code, "http_budget_exhausted")
        self.assertEqual(calls, [])

    def test_process_death_after_cache_is_recovered_without_http(self):
        original = self.crash("after")
        self.assertEqual(len(list(self.folder.glob("*.fixture.json"))), 1)
        calls = []
        adapter = ChoiceAdapter(replace(config(self.folder), network_policy="cache_only", allow_live=False),
                                transport=lambda *args: calls.append(args))
        with patch.dict(os.environ, {}, clear=True):
            result = adapter.rank(payload())
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["actual_http_requests"], 0)
        self.assertEqual(result["prior_uncertain_attempts"], [])
        self.assertEqual(adapter.http_requests, 1)
        self.assertEqual(adapter.ledger["started_at_epoch"], original["started_at_epoch"])
        self.assertEqual(adapter.usage_summary()["actual_usage"], response()["usage"])
        self.assertEqual(adapter.ledger["attempts"][0]["status"], "success")
        self.assertFalse(adapter.ledger["attempts"][0]["uncertain"])
        self.assertEqual(calls, [])

    def test_old_success_without_valid_bound_response_becomes_uncertain(self):
        for damage in ("missing", "invalid_response", "wrong_attempt"):
            with self.subTest(damage=damage):
                folder = self.folder / damage
                adapter = ChoiceAdapter(config(folder), transport=lambda *args: (200, {}, response()))
                adapter.rank(payload())
                original = deepcopy(adapter.ledger)
                path = next(folder.glob("*.fixture.json"))
                if damage == "missing":
                    path.unlink()
                else:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                    if damage == "invalid_response":
                        cached["response"]["answers"]["portfolio"]["choice"] = "B"
                    else:
                        cached["live_origin"]["attempt_id"] = "another-budget:1"
                    path.write_text(json.dumps(cached), encoding="utf-8")
                restored = ChoiceAdapter(replace(config(folder), network_policy="cache_only", allow_live=False),
                                         transport=lambda *args: self.fail("cache-only sent HTTP"))
                row = restored.ledger["attempts"][0]
                self.assertEqual(row["status"], "response_not_persisted")
                self.assertTrue(row["uncertain"])
                self.assertEqual(row["known_usage"], original["attempts"][0]["known_usage"])
                self.assertEqual(row["attempt_id"], original["attempts"][0]["attempt_id"])
                self.assertEqual(restored.http_requests, 1)
                self.assertEqual(restored.ledger["started_at_epoch"], original["started_at_epoch"])


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--crash-worker":
        crash_worker(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
