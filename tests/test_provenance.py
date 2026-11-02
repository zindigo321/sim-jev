"""Exercise content-based provenance and actual CLI checkpoint invocations."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sim_jev.config import Config
from sim_jev.provenance import canonical_sha256, code_fingerprint, git_state, runtime_metadata
from sim_jev.versions import CHECKPOINT_SCHEMA_VERSION, MODEL_VERSION, RNG_PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[1]


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runs").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="test-provenance-", dir=ROOT / "runs")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def test_fingerprint_includes_untracked_source_but_excludes_runs_and_mtimes(self):
        source = self.folder / "sim_jev"
        source.mkdir()
        module = source / "untracked.py"
        module.write_text("x = 1\n", encoding="utf-8")
        initial = code_fingerprint(self.folder)
        self.assertEqual([item["path"] for item in initial["files"]], ["sim_jev/untracked.py"])
        os.utime(module, (10, 10))
        for excluded in ("runs", "__pycache__", ".git", ".venv"):
            folder = self.folder / excluded
            folder.mkdir()
            (folder / "ignored.py").write_text("change me", encoding="utf-8")
        self.assertEqual(initial, code_fingerprint(self.folder))
        module.write_text("x = 2\n", encoding="utf-8")
        self.assertNotEqual(initial["sha256"], code_fingerprint(self.folder)["sha256"])

    def test_fingerprint_is_location_independent(self):
        for name in ("first", "second"):
            directory = self.folder / name
            directory.mkdir()
            (directory / "module.py").write_bytes(b"content\n")
        self.assertEqual(code_fingerprint(self.folder / "first"), code_fingerprint(self.folder / "second"))

    def test_effective_config_hash_and_versions_are_explicit(self):
        config = replace(Config(), seed=987).to_dict()
        metadata = runtime_metadata(config)
        self.assertEqual(metadata["effective_config_sha256"], canonical_sha256(config))
        self.assertNotEqual(metadata["effective_config_sha256"], canonical_sha256(Config().to_dict()))
        self.assertEqual(metadata["model_version"], MODEL_VERSION)
        self.assertEqual(metadata["rng_protocol_version"], RNG_PROTOCOL_VERSION)
        self.assertEqual(metadata["checkpoint_schema_version"], CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(metadata["actual_command"], sys.orig_argv)
        self.assertEqual(metadata["python_executable"], sys.executable)
        json.dumps(metadata, allow_nan=False)

    def test_unavailable_git_is_explicit(self):
        state = git_state(Path(tempfile.gettempdir()))
        self.assertFalse(state["available"])
        self.assertIsNone(state["revision"])
        self.assertIsNone(state["dirty"])
        self.assertTrue(state["reason"])

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "sim_jev", *map(str, arguments)],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_cli_resume_in_fresh_process_and_scientific_outputs(self):
        config = replace(Config(), horizon_hours=20, mission_rate_per_hour=0.5)
        config_path = self.folder / "scenario.json"
        config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
        checkpoint = self.folder / "paused.json"
        paused, resumed, whole = [self.folder / name for name in ("paused", "resumed", "whole")]
        commands = [
            ("--config", config_path, "--until", "7.125", "--checkpoint-out", checkpoint, "--output", paused),
            ("--resume", checkpoint, "--output", resumed),
            ("--config", config_path, "--output", whole),
        ]
        for arguments in commands:
            result = self.run_cli(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((resumed / "events.csv").read_bytes(), (whole / "events.csv").read_bytes())
        self.assertEqual((resumed / "orders.csv").read_bytes(), (whole / "orders.csv").read_bytes())
        first = json.loads((resumed / "summary.json").read_text(encoding="utf-8"))
        second = json.loads((whole / "summary.json").read_text(encoding="utf-8"))
        import math
        for key in first:
            if type(first[key]) is float:
                self.assertTrue(math.isclose(first[key], second[key], rel_tol=1e-12, abs_tol=1e-10), key)
            else:
                self.assertEqual(first[key], second[key], key)
        runtime = json.loads((resumed / "runtime.json").read_text(encoding="utf-8"))
        self.assertIn("--resume", runtime["actual_command"])
        self.assertEqual(runtime["run_context"]["resumed_at"], 7.125)
        self.assertEqual(runtime["run_context"]["checkpoint_source_sha256"],
                         hashlib.sha256(checkpoint.read_bytes()).hexdigest())
        from scripts.validate import check_artifacts
        self.assertEqual(check_artifacts(resumed)["metadata_status"], "provenance-v1")

    def test_cli_rejects_conflicting_resume_inputs(self):
        for option, value in (("--seed", "42"), ("--config", "configs/default.json")):
            result = self.run_cli("--resume", "does-not-exist.json", option, value,
                                  "--output", self.folder / "must-not-exist")
            self.assertEqual(result.returncode, 2)
            self.assertIn("cannot be combined", result.stderr)
            self.assertFalse((self.folder / "must-not-exist").exists())

    def test_checkpoint_path_cannot_replace_report_artifact(self):
        destination = self.folder / "output"
        result = self.run_cli("--until", "0", "--output", destination,
                              "--checkpoint-out", destination / "summary.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("report artifact", result.stderr)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
