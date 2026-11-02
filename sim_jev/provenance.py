"""Run provenance, kept separate from deterministic scientific summaries."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from .versions import (
    AUDIT_STATE_SCHEMA_VERSION, CHECKPOINT_SCHEMA_VERSION, MODEL_VERSION,
    PROVENANCE_SCHEMA_VERSION, RNG_PROTOCOL_VERSION,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".pyi", ".toml", ".json", ".cfg", ".ini", ".yaml", ".yml", ".ps1", ".sh"}
EXCLUDED_DIRECTORIES = {
    "runs", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".mplconfig", ".venv", "venv", "env", ".conda", "conda-env", "build", "dist",
    ".vscode", ".idea", ".aws", "node_modules", "audit-record",
}
LEGACY_SOURCE_SCOPE = "project-source-and-config-v1"
SOURCE_SCOPE = "project-source-and-config-v2"
# Bundle-level manifests describe a transport artifact, never the model source.
# Keep v1 available for verification of historical reports without rewriting them.
EXCLUDED_PACKAGE_FILES = {"MANIFEST.json", "AUDIT-MANIFEST.json"}
V2_EXCLUDED_DIRECTORIES = {"cache", "caches", ".cache"}


def canonical_sha256(value: object) -> str:
    """Hash strict JSON values with stable key ordering and no whitespace."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def code_fingerprint(root: Path = ROOT, *, scope: str = SOURCE_SCOPE) -> dict:
    """Hash source/configuration bytes, including untracked files, never mtimes.

    The manifest uses relative paths so an unchanged checkout has the same hash
    at another location. Reports, run outputs, environments and caches are not
    source. Symlinks are excluded rather than following files outside the root.
    """
    if scope not in {LEGACY_SOURCE_SCOPE, SOURCE_SCOPE}:
        raise ValueError("Unknown source fingerprint scope")
    root = Path(root).resolve()
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if name not in EXCLUDED_DIRECTORIES
            and not (scope == SOURCE_SCOPE and name in V2_EXCLUDED_DIRECTORIES)
            and not name.endswith(".egg-info")
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(current) / name
            if (scope == SOURCE_SCOPE and path.parent == root
                    and name in EXCLUDED_PACKAGE_FILES):
                continue
            if path.is_symlink() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if name.startswith(".env"):
                continue
            payload = path.read_bytes()
            files.append({"path": path.relative_to(root).as_posix(), "size_bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()})
    files.sort(key=lambda entry: entry["path"])
    result = {"algorithm": "sha256", "scope": scope,
              "sha256": canonical_sha256(files), "files": files}
    if scope == SOURCE_SCOPE:
        result["scope_definition"] = {
            "included_suffixes": sorted(SOURCE_SUFFIXES),
            "excluded_directory_names": sorted(EXCLUDED_DIRECTORIES | V2_EXCLUDED_DIRECTORIES),
            "excluded_root_files": sorted(EXCLUDED_PACKAGE_FILES),
            "excluded_name_prefixes": [".env"],
            "excluded_directory_suffixes": [".egg-info"],
            "symlinks": "excluded", "relative_paths": "posix",
            "digest_input": "canonical JSON of sorted files only",
        }
    return result


def git_state(root: Path = ROOT) -> dict:
    """Identify the actual repository; never attribute a parent's HEAD to a project.

    ``revision``, ``dirty`` and ``status_porcelain`` always describe ``repo_root``.
    A nested unversioned project can therefore report useful parent-repository
    context without claiming that it has its own Git identity. All ownership
    exceptions are per command; no local or global Git configuration is changed.
    """
    root = Path(root).resolve()
    prefix = ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root)]
    location = {"project_root": str(root), "repo_root": None,
                "project_relative_path": None, "relationship": "unavailable",
                "revision_scope": "repository", "project_has_tracked_files": None}

    def unavailable(reason: str) -> dict:
        return {**location, "available": False, "revision": None, "dirty": None,
                "reason": reason}

    try:
        toplevel = subprocess.run([*prefix, "rev-parse", "--show-toplevel"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=10)
        if toplevel.returncode or not toplevel.stdout.strip():
            return unavailable(toplevel.stderr.strip() or "Git show-toplevel command failed")
        repository = Path(toplevel.stdout.strip()).resolve()
        if not root.is_relative_to(repository):
            return unavailable("Git show-toplevel does not contain the project root")
        relative = root.relative_to(repository).as_posix()
        location.update(repo_root=str(repository), project_relative_path=relative,
                        relationship="project_repository" if root == repository else "parent_repository")
        prefix = ["git", "-c", f"safe.directory={repository.as_posix()}", "-C", str(repository)]
        revision = subprocess.run([*prefix, "rev-parse", "HEAD"], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=10)
        status = subprocess.run([*prefix, "status", "--porcelain=v1", "--untracked-files=all"],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=10)
        tracked = subprocess.run([*prefix, "ls-files", "--", relative], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return unavailable(str(error))
    if revision.returncode or status.returncode or tracked.returncode:
        reason = (revision.stderr or status.stderr or tracked.stderr).strip()
        return unavailable(reason or "Git revision/status/tracked-files command failed")
    location["project_has_tracked_files"] = bool(tracked.stdout)
    return {**location, "available": True, "revision": revision.stdout.strip(),
            "dirty": bool(status.stdout), "status_porcelain": status.stdout.splitlines()}


def runtime_metadata(config: dict, *, run_context: dict | None = None,
                     model_version: str = MODEL_VERSION,
                     rng_protocol_version: str = RNG_PROTOCOL_VERSION,
                     checkpoint_schema_version: int = CHECKPOINT_SCHEMA_VERSION,
                     policy_identity: dict | None = None) -> dict:
    """Describe the actual host invocation, effective inputs and source content."""
    command = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv]))
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "model_version": model_version,
        "audit_state_schema_version": AUDIT_STATE_SCHEMA_VERSION,
        "checkpoint_schema_version": checkpoint_schema_version,
        "rng_protocol_version": rng_protocol_version,
        **({"policy_identity": policy_identity} if policy_identity is not None else {}),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "python_implementation": platform.python_implementation(),
        "packages": {name: importlib.metadata.version(name)
                     for name in ("numpy", "pandas", "matplotlib")},
        "actual_command": command,
        "command_line": subprocess.list2cmdline(command),
        "working_directory": str(Path.cwd()),
        "effective_config_sha256": canonical_sha256(config),
        "code_fingerprint": code_fingerprint(),
        "git": git_state(),
        "run_context": dict(run_context or {}),
    }
