"""Source scope and real no-Git transport regressions, independent of host Git."""

from pathlib import Path
import json
import shutil
import tempfile
import unittest
import zipfile

from scripts.package_audit import create_package, verify_package
from scripts.validate import check_runtime_details
from sim_jev.config import Config
from sim_jev.provenance import code_fingerprint, runtime_metadata


ROOT = Path(__file__).resolve().parents[1]


class AuditPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sim-jev-package-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()

    def test_transport_manifest_does_not_change_source_identity(self):
        (self.root / "module.py").write_text("value = 42\n", encoding="utf-8")
        before = code_fingerprint(self.root)
        (self.root / "MANIFEST.json").write_text('{"transport":true}', encoding="utf-8")
        self.assertEqual(before, code_fingerprint(self.root))
        self.assertEqual(before["scope"], "project-source-and-config-v2")
        self.assertIn("MANIFEST.json", before["scope_definition"]["excluded_root_files"])
        legacy = code_fingerprint(self.root, scope="project-source-and-config-v1")
        self.assertIn("MANIFEST.json", [item["path"] for item in legacy["files"]])
        self.assertNotEqual(before["sha256"], legacy["sha256"])

    def test_historical_v1_source_identity_uses_historical_scope(self):
        (self.root / "model.py").write_text("value=1\n", encoding="utf-8")
        (self.root / "MANIFEST.json").write_text("{}", encoding="utf-8")
        config = Config().to_dict()
        runtime = runtime_metadata(config)
        runtime["code_fingerprint"] = code_fingerprint(self.root, scope="project-source-and-config-v1")
        verified = check_runtime_details(runtime, config, require_provenance=True, source_root=self.root)
        self.assertTrue(verified["source_identity_verified"])
        self.assertNotIn("scope_definition", runtime["code_fingerprint"])

    def fixture_project(self):
        (self.root / "sim_jev").mkdir()
        (self.root / "sim_jev/__init__.py").write_text("", encoding="utf-8")
        for name in ("provenance.py", "versions.py"):
            shutil.copyfile(ROOT / "sim_jev" / name, self.root / "sim_jev" / name)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_fixture.py").write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class FixtureTests(unittest.TestCase):\n"
            "    def test_root_has_no_transport_manifest_or_git(self):\n"
            "        self.assertFalse(Path('MANIFEST.json').exists())\n"
            "        self.assertFalse(Path('.git').exists())\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs/CONTRACT.md").write_text("Frozen fixture contract.\n", encoding="utf-8")
        (self.root / "runs/current").mkdir(parents=True)
        (self.root / "runs/current/evidence.json").write_text('{"genuine_fixture":true}', encoding="utf-8")
        (self.root / "runs/old").mkdir()
        (self.root / "runs/old/excluded.txt").write_text("old evidence", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git/objects").write_text("not a repository", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=fixture", encoding="utf-8")
        (self.root / "cache").mkdir()
        (self.root / "cache/secret.txt").write_text("not packed", encoding="utf-8")

    def test_acceptance_package_extract_identity_and_real_no_git_tests(self):
        self.fixture_project()
        # Freeze identity before packaging, then verify the extracted code with
        # an actual fresh interpreter and actual unittest execution.
        accepted = code_fingerprint(self.root)
        package = self.base / "new-audit.zip"
        created = create_package(self.root, package, evidence=["runs/current"])
        self.assertEqual(created["source_fingerprint"], accepted)
        verified = verify_package(package, self.base / "new-extraction", acceptance="unittest")
        self.assertTrue(verified["source_identity_verified"])
        self.assertFalse(verified["extracted_git"]["available"])
        self.assertEqual(verified["test_count"], 1)
        self.assertTrue(all(item["exit_code"] == 0 for item in verified["commands"]))
        self.assertEqual(verified["source_fingerprint"], accepted)
        with zipfile.ZipFile(package) as bundle:
            names = bundle.namelist()
            self.assertIn("MANIFEST.json", names)
            self.assertIn("project/docs/CONTRACT.md", names)
            self.assertIn("project/runs/current/evidence.json", names)
            for unwanted in ("project/.env", "project/.git/objects", "project/MANIFEST.json",
                             "project/cache/secret.txt", "project/runs/old/excluded.txt"):
                self.assertNotIn(unwanted, names)
        self.assertTrue(package.with_suffix(".zip.sha256").exists())
        with self.assertRaisesRegex(ValueError, "overwrite"):
            create_package(self.root, package)
        with self.assertRaisesRegex(ValueError, "brand-new"):
            verify_package(package, self.base / "new-extraction")

    def test_archive_tamper_is_rejected_before_execution(self):
        self.fixture_project()
        original, damaged = self.base / "original.zip", self.base / "damaged.zip"
        create_package(self.root, original)
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(damaged, "w") as target:
            for name in source.namelist():
                target.writestr(name, b"changed" if name == "project/tests/test_fixture.py" else source.read(name))
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            verify_package(damaged, self.base / "damaged-extraction", acceptance="identity")
        report = json.loads((self.base / "damaged-extraction/SELF-ACCEPTANCE.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["commands"], [])


if __name__ == "__main__":
    unittest.main()
