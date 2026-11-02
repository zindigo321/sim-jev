"""Smoke authorization, metering evidence and strict genuine-response gate."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.jev_smoke import main
from scripts.package_audit import create_package
from sim_jev.jev import ChoiceAdapter, MODEL


class SmokeAcceptanceTests(unittest.TestCase):
    def test_missing_key_records_not_run_and_zero_durable_attempts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            output = Path(tmp) / "smoke"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--allow-live", "--output", str(output)])
            self.assertEqual(code, 2)
            result = json.loads((output / "smoke.json").read_text())
            ledger = json.loads((output / "response-store/http-budget.json").read_text())
            self.assertEqual(result["status"], "not_run")
            self.assertFalse(result["jev_live_smoke_verified"])
            self.assertEqual(result["actual_http_requests"], 0)
            self.assertEqual(ledger["attempts"], [])
            self.assertEqual(ledger["max_http_requests"], 3)

    def test_invalid_or_enlarged_smoke_budgets_are_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "invalid"
            for arguments in ([], ["--allow-live", "--max-http-requests", "4"],
                              ["--allow-live", "--max-http-requests", "0"],
                              ["--allow-live", "--total-timeout-seconds", "121"],
                              ["--allow-live", "--total-timeout-seconds", "nan"]):
                with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        main([*arguments, "--output", str(output)])
                    self.assertEqual(stopped.exception.code, 2)
                    self.assertFalse(output.exists())

    def test_injected_success_cannot_verify_real_smoke(self):
        def fixture(payload, credential, timeout):
            ids = sorted(payload["questions"]["portfolio"]["criteria"])
            return 200, {}, {"model": MODEL, "answers": {"portfolio": {
                "choice": ids[0], "probabilities": {key: 1 / len(ids) for key in ids},
                "confidence": .5}}, "usage": {"input_tokens": 7, "output_tokens": 1}}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fixture-only"}):
            output = Path(tmp) / "fixture"
            with patch("scripts.jev_smoke.ChoiceAdapter", side_effect=lambda config: ChoiceAdapter(config, transport=fixture)):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--allow-live", "--output", str(output)])
            result = json.loads((output / "smoke.json").read_text())
            self.assertEqual(code, 1)
            self.assertFalse(result["jev_live_smoke_verified"])
            self.assertEqual(result["actual_http_requests"], 1)
            self.assertTrue(result["adapter_audits"][0]["valid_response"])
            self.assertFalse(result["adapter_audits"][0]["valid_jev"])
            checkpoint = json.loads((output / "run/checkpoint.json").read_text())
            state = checkpoint["weekly_state"]
            self.assertEqual(len(state["weekly_commands"]), 1)
            self.assertEqual(state["decision_records"][0]["budget"]["actual_des"], 32)
            self.assertEqual(len(list((output / "response-store").glob("*.live.json"))), 0)

    def test_package_excludes_runtime_store_but_keeps_explicit_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            store = root / "runs/current/response-store"
            store.mkdir(parents=True)
            (store / "runtime.live.json").write_text("{}")
            (store.parent / "request-response-evidence.json").write_text("{}")
            archive = Path(tmp) / "new.zip"
            create_package(root, archive, evidence=["runs/current"])
            import zipfile
            with zipfile.ZipFile(archive) as bundle:
                self.assertFalse(any("response-store" in name for name in bundle.namelist()))
                self.assertIn("project/runs/current/request-response-evidence.json", bundle.namelist())


if __name__ == "__main__":
    unittest.main()
