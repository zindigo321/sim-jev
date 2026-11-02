"""Create and re-read an audit ZIP, then accept its extracted source without Git.

The transport manifest lives beside ``project/``, never inside the source root.
No existing ZIP, extraction directory, or verification report is overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim_jev.provenance import code_fingerprint


EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 ".mplconfig", ".venv", "venv", "env", ".conda", "conda-env",
                 "node_modules", ".aws", ".idea", ".vscode", "build", "dist",
                 "cache", "caches", ".cache", "response-store"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pem", ".key"}


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or "\\" in value
            or ":" in value or path.as_posix() != value):
        raise ValueError(f"Unsafe archive path: {value!r}")
    return path


def _selected_files(root: Path, evidence: list[str]) -> list[Path]:
    roots = []
    for item in evidence:
        relative = _safe_relative(item)
        if relative.parts[0] != "runs":
            raise ValueError("Evidence paths must be explicit paths below runs/")
        source = root.joinpath(*relative.parts)
        if not source.exists() or not source.resolve().is_relative_to(root):
            raise ValueError(f"Missing/outside evidence path: {item}")
        roots.append(source)
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        folder = Path(current)
        directories[:] = sorted(name for name in directories
                                if name not in EXCLUDED_DIRS and not name.endswith(".egg-info")
                                and not (folder / name).is_symlink()
                                and (name != "runs" or folder != root or roots))
        if folder == root / "runs" or folder.is_relative_to(root / "runs"):
            directories[:] = [name for name in directories if any(
                (folder / name).is_relative_to(selected) or selected.is_relative_to(folder / name)
                for selected in roots)]
        for name in sorted(names):
            path = folder / name
            if (path.is_symlink() or path.suffix.lower() in EXCLUDED_SUFFIXES
                    or name.startswith(".env") or name.lower() in {"credentials", "credentials.json",
                                                                  "id_rsa", "id_ed25519"}):
                continue
            if folder == root and name in {"MANIFEST.json", "AUDIT-MANIFEST.json"}:
                continue
            if path.is_relative_to(root / "runs") and not any(
                    path == selected or path.is_relative_to(selected) for selected in roots):
                continue
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def create_package(source_root: Path, archive: Path, *, evidence: list[str] | None = None) -> dict:
    root, archive = source_root.resolve(), archive.resolve()
    if archive.is_relative_to(root):
        raise ValueError("Audit ZIP must be outside the source root")
    if archive.exists() or archive.with_suffix(archive.suffix + ".sha256").exists():
        raise ValueError("Refusing to overwrite audit archive/checksum")
    files = _selected_files(root, evidence or [])
    fingerprint = code_fingerprint(root)
    file_records = []
    secret = os.environ.get("TYPESAFE_API_KEY", "").encode("utf-8")
    archive.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects historical packages even if callers race.
    with archive.open("xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            payload = path.read_bytes()
            if secret and secret in payload:
                raise ValueError(f"Credential bytes detected in {path.relative_to(root)}")
            relative = path.relative_to(root).as_posix()
            bundle.writestr(f"project/{relative}", payload)
            file_records.append({"path": f"project/{relative}", "size_bytes": len(payload),
                                 "sha256": _sha(payload)})
        # Guard against code changes while packaging, rather than blessing mixed revisions.
        if fingerprint != code_fingerprint(root):
            raise ValueError("Source changed during packaging; use a new output path after edits finish")
        manifest = {
            "schema": "sim-jev-audit-bundle-v1", "project_directory": "project",
            "source_fingerprint": fingerprint, "files": file_records,
            "manifest_scope": "all archived files except this external transport manifest",
            "evidence_paths": evidence or [],
            "exclusions": {"directories": sorted(EXCLUDED_DIRS),
                           "suffixes": sorted(EXCLUDED_SUFFIXES),
                           "symlinks": True, "unselected_runs": True,
                           "environment_files": True, "credential_files": True},
        }
        bundle.writestr("MANIFEST.json", json.dumps(manifest, indent=2, ensure_ascii=False,
                                                   allow_nan=False) + "\n")
    digest = _sha(archive.read_bytes())
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii")
    return {"archive": str(archive), "sha256": digest, "file_count": len(file_records),
            "source_fingerprint": fingerprint}


def verify_package(archive: Path, destination: Path, *, python: str = sys.executable,
                   acceptance: str = "full", timeout: float = 1800) -> dict:
    archive, destination = archive.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("Verification requires a brand-new extraction directory")
    if acceptance not in {"full", "unittest", "identity"}:
        raise ValueError("Unknown acceptance level")
    destination.mkdir(parents=True, exist_ok=False)
    report = {"schema": "sim-jev-audit-self-acceptance-v1", "archive": str(archive),
              "sha256": _sha(archive.read_bytes()), "destination": str(destination),
              "acceptance": acceptance, "commands": [], "status": "running"}
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Archive has duplicate entries")
            for name in names:
                _safe_relative(name)
            manifest = json.loads(bundle.read("MANIFEST.json"))
            if manifest.get("schema") != "sim-jev-audit-bundle-v1":
                raise ValueError("Unknown bundle manifest schema")
            entries = manifest["files"]
            expected = {row["path"] for row in entries}
            if len(expected) != len(entries) or set(names) != expected | {"MANIFEST.json"}:
                raise ValueError("Archive entries do not exactly match manifest")
            for row in entries:
                relative = _safe_relative(row["path"])
                if relative.parts[0] != "project" or ".git" in relative.parts:
                    raise ValueError("Archive contains outside-project/Git entry")
                payload = bundle.read(row["path"])
                if len(payload) != row["size_bytes"] or _sha(payload) != row["sha256"]:
                    raise ValueError(f"Bundle content mismatch: {row['path']}")
                output = destination.joinpath(*relative.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(payload)
            _json(destination / "MANIFEST.json", manifest)
        project = destination / "project"
        report["verified_file_count"] = len(entries)
        # Make an enclosing repository irrelevant too. Git inside fixture tests
        # remains available; the extracted project itself has no repository.
        environment = dict(os.environ)
        environment["GIT_CEILING_DIRECTORIES"] = str(destination)
        environment["MPLCONFIGDIR"] = str(destination / "matplotlib-cache")
        environment.pop("PYTHONPATH", None)
        environment.pop("TYPESAFE_API_KEY", None)

        def run(arguments: list[str], log_name: str) -> subprocess.CompletedProcess:
            command = [python, *arguments]
            start = time.monotonic()
            result = subprocess.run(command, cwd=project, env=environment,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout)
            (destination / log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
            report["commands"].append({"command": command, "cwd": str(project),
                                       "exit_code": result.returncode,
                                       "elapsed_seconds": time.monotonic() - start,
                                       "log": log_name})
            if result.returncode:
                raise ValueError(f"Acceptance command failed: {command}; see {log_name}")
            return result

        identity = run(["-c", "import json; from sim_jev.provenance import code_fingerprint, git_state; "
                        "print(json.dumps({'fingerprint':code_fingerprint(), 'git':git_state()}))"],
                       "source-identity.txt")
        actual = json.loads(identity.stdout)
        if actual["fingerprint"] != manifest["source_fingerprint"]:
            raise ValueError("Extracted source fingerprint differs from acceptance source")
        if actual["git"]["available"]:
            raise ValueError("Extracted source unexpectedly acquired Git identity")
        report.update(source_identity_verified=True, extracted_git=actual["git"],
                      source_fingerprint=actual["fingerprint"])
        if acceptance in {"unittest", "full"}:
            result = run(["-m", "unittest", "discover", "-s", "tests", "-v"], "unittest.txt")
            match = re.search(r"Ran (\d+) tests?", result.stdout + result.stderr)
            if match is None:
                raise ValueError("Cannot find actual unittest count")
            report["test_count"] = int(match.group(1))
        if acceptance == "full":
            for script, name in (("validate.py", "legacy"), ("checkpoint_verify.py", "checkpoint"),
                                 ("weekly_verify.py", "weekly")):
                run([f"scripts/{script}", "--output", f"runs/package-selfcheck-{name}"],
                    f"{name}-acceptance.txt")
            if (project / "scripts/phase2c_experiment.py").exists():
                run(["scripts/phase2c_experiment.py", "--output", "runs/package-selfcheck-phase2c"],
                    "phase2c-acceptance.txt")
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        _json(destination / "SELF-ACCEPTANCE.json", report)
        raise
    _json(destination / "SELF-ACCEPTANCE.json", report)
    return report


def main() -> int:
    if sys.flags.optimize:
        print("Audit acceptance refuses optimized Python", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--evidence", action="append", default=[], help="Explicit runs/... path to include")
    parser.add_argument("--verify-into", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--acceptance", choices=["full", "unittest", "identity"], default="full")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args()
    try:
        if not args.verify_existing:
            create_package(args.source_root, args.archive, evidence=args.evidence)
        result = verify_package(args.archive, args.verify_into, python=args.python,
                                acceptance=args.acceptance, timeout=args.timeout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Audit packaging/acceptance failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({key: result[key] for key in ("status", "archive", "sha256", "destination",
                                                  "source_identity_verified")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
