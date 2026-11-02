"""Regression probes for the independent phase-2A provenance audit findings."""

from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sim_jev.config import Config
from sim_jev.provenance import canonical_sha256, code_fingerprint, git_state, runtime_metadata
from scripts.validate import check_runtime, check_runtime_details


ROOT = Path(__file__).resolve().parents[1]


class ProvenanceAuditFixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sim-jev-provenance-audit-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.config = Config().to_dict()

    def git(self, folder: Path, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={folder.as_posix()}",
             "-c", "user.name=Provenance Test", "-c", "user.email=provenance@example.invalid",
             "-C", str(folder), *args], capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def create_repository(self):
        self.git(self.folder, "init")
        (self.folder / ".gitignore").write_text("project/\n", encoding="utf-8")
        (self.folder / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        self.git(self.folder, "add", ".gitignore", "tracked.py")
        self.git(self.folder, "commit", "-m", "provenance fixture")

    def test_temporary_project_root_is_its_own_repository(self):
        self.create_repository()
        result = git_state(self.folder)
        self.assertTrue(result["available"])
        self.assertEqual(Path(result["repo_root"]), self.folder)
        self.assertEqual(result["project_relative_path"], ".")
        self.assertEqual(result["relationship"], "project_repository")
        self.assertEqual(result["revision_scope"], "repository")

    def test_plain_directory_has_no_git_identity(self):
        result = git_state(self.folder)
        self.assertFalse(result["available"])
        self.assertIsNone(result["repo_root"])
        self.assertEqual(Path(result["project_root"]), self.folder)
        self.assertEqual(result["relationship"], "unavailable")
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["dirty"])
        self.assertTrue(result["reason"])

    def test_parent_repository_is_not_claimed_as_project_identity(self):
        self.create_repository()
        project = self.folder / "project"
        project.mkdir()
        (project / "module.py").write_text("answer = 42\n", encoding="utf-8")
        result = git_state(project)
        self.assertTrue(result["available"])
        self.assertEqual(Path(result["repo_root"]), self.folder)
        self.assertEqual(Path(result["project_root"]), project)
        self.assertEqual(result["relationship"], "parent_repository")
        self.assertEqual(result["project_relative_path"], "project")
        self.assertEqual(result["revision"], self.git(self.folder, "rev-parse", "HEAD").stdout.strip())
        self.assertFalse(result["dirty"])
        self.assertFalse(result["project_has_tracked_files"])
        self.assertEqual(result["revision_scope"], "repository")
        own = git_state(self.folder)
        self.assertEqual(own["relationship"], "project_repository")
        self.assertTrue(own["project_has_tracked_files"])

    def test_bool_is_not_a_runtime_schema_integer(self):
        original = runtime_metadata(self.config)
        for name in ("schema_version", "checkpoint_schema_version", "audit_state_schema_version"):
            with self.subTest(field=name):
                damaged = copy.deepcopy(original)
                damaged[name] = True
                with self.assertRaisesRegex(AssertionError, name):
                    check_runtime(damaged, self.config, require_provenance=True)

    def test_manifest_self_consistency_does_not_claim_source_identity(self):
        original = runtime_metadata(self.config)
        damaged = copy.deepcopy(original)
        damaged["code_fingerprint"]["files"][0]["sha256"] = "0" * 64
        damaged["code_fingerprint"]["sha256"] = canonical_sha256(damaged["code_fingerprint"]["files"])
        result = check_runtime_details(damaged, self.config, require_provenance=True)
        self.assertTrue(result["metadata_valid"])
        self.assertTrue(result["manifest_self_consistent"])
        self.assertFalse(result["source_identity_verified"])
        with self.assertRaisesRegex(AssertionError, "Expected source fingerprint mismatch"):
            check_runtime_details(damaged, self.config, require_provenance=True,
                                  expected_fingerprint=original["code_fingerprint"]["sha256"])
        with self.assertRaisesRegex(AssertionError, "Source-root manifest mismatch"):
            check_runtime_details(damaged, self.config, require_provenance=True, source_root=ROOT)

    def test_explicit_source_or_expected_fingerprint_verifies_identity(self):
        original = runtime_metadata(self.config)
        for expectation in ({"expected_fingerprint": original["code_fingerprint"]},
                            {"expected_fingerprint": original["code_fingerprint"]["sha256"]},
                            {"source_root": ROOT}):
            with self.subTest(expectation=next(iter(expectation))):
                result = check_runtime_details(original, self.config, require_provenance=True, **expectation)
                self.assertTrue(result["source_identity_verified"])
                self.assertEqual(result["metadata_status"], "provenance-v1")

    def test_historical_metadata_can_be_verified_against_its_original_source(self):
        (self.folder / "old_model.py").write_text("old_model = 1\n", encoding="utf-8")
        original = runtime_metadata(self.config)
        original["code_fingerprint"] = code_fingerprint(self.folder)
        result = check_runtime_details(original, self.config, require_provenance=True)
        self.assertFalse(result["source_identity_verified"])
        verified = check_runtime_details(original, self.config, require_provenance=True,
                                         source_root=self.folder)
        self.assertTrue(verified["source_identity_verified"])
        with self.assertRaisesRegex(AssertionError, "Source-root manifest mismatch"):
            check_runtime_details(original, self.config, require_provenance=True, source_root=ROOT)

    def test_historical_schema_one_git_shape_remains_accepted(self):
        original = runtime_metadata(self.config)
        original["git"] = {"available": True, "revision": "a" * 40, "dirty": True,
                           "status_porcelain": ["?? historical_fixture.py"]}
        self.assertEqual(check_runtime(original, self.config, require_provenance=True), "provenance-v1")

    def test_repository_relationship_is_cross_checked(self):
        self.create_repository()
        original = runtime_metadata(self.config)
        original["git"] = git_state(self.folder)
        original["git"]["relationship"] = "parent_repository"
        with self.assertRaisesRegex(AssertionError, "relationship mismatch"):
            check_runtime_details(original, self.config, require_provenance=True)

    def test_legacy_report_does_not_claim_manifest_or_identity_verification(self):
        legacy = {"python_version": "3.11.0", "packages": {"numpy": "2.0.0"}}
        details = check_runtime_details(legacy, self.config, require_provenance=False)
        self.assertTrue(details["metadata_valid"])
        self.assertFalse(details["manifest_self_consistent"])
        self.assertFalse(details["source_identity_verified"])
        with self.assertRaisesRegex(AssertionError, "no source fingerprint"):
            check_runtime_details(legacy, self.config, require_provenance=False,
                                  expected_fingerprint="0" * 64)

    def test_validation_refuses_python_optimization_before_writing_outputs(self):
        output = self.folder / "optimized-must-not-exist"
        result = subprocess.run([sys.executable, "-O", "scripts/validate.py", "--output", str(output)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("refuses optimized Python", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
