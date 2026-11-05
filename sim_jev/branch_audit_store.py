"""Durable DES attempts and audit coverage for resumable local experiments.

The checkpoint is the commit point. Evidence is durable before that commit.
Uncommitted computations are never silently repeated: ambiguous recovery stops.
This is a conservative journal, not an exactly-once execution claim.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from .provenance import canonical_sha256
from .jev import _atomic_json

PROTOCOL = "durable-branch-audit-v1"


def read_json(path):
    from .checkpoint import _loads
    return _loads(Path(path).read_text(encoding="utf-8"))


def checkpoint_bytes(value):
    """Object insertion order is part of legacy commitment observation order."""
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def save_checkpoint(path, value):
    """Atomic, strict JSON checkpoint writer; never sort ordered state maps."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".checkpoint-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(checkpoint_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def branch_key(request, candidate_id, scenario_id):
    value = request.to_dict() if hasattr(request, "to_dict") else request
    return canonical_sha256({"protocol": PROTOCOL, "request_sha256": canonical_sha256(value),
                             "candidate_id": candidate_id, "scenario_id": scenario_id})


class BranchAuditStore:
    """One per run; callable as DecisionRuntime.branch_auditor.

    fault_hook is a test-only crash injector at persistence boundaries. It does
    not alter the saved data, selection, DES implementation or auditor.
    """
    def __init__(self, run_dir, auditor, *, fault_hook=None, read_only=False):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "branch-audit-store"
        self.read_only = read_only
        for subdir in ("attempts", "audits", "artifacts"):
            directory = self.root / subdir
            if read_only:
                if not directory.is_dir():
                    raise ValueError("missing durable branch audit directory: " + str(directory))
            else:
                directory.mkdir(parents=True, exist_ok=True)
        self.auditor = auditor
        self.fault_hook = fault_hook
        self._active = None
        # The experiment runner is sequential and owns one writer per run.
        # Reload and validate at construction/reconciliation, not O(N^2) file
        # reads before N branches. Durable files remain the source of truth.
        initial = self._attempts()
        self._known_attempts = {row["branch_key"]: row for row in initial}
        if len(initial) != len(self._known_attempts):
            raise ValueError("duplicate DES attempt identity")

    def _fault(self, point):
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _attempts(self):
        return [read_json(p) for p in sorted((self.root / "attempts").glob("*.json"))]

    def _audits(self):
        result = {}
        for path in sorted((self.root / "audits").glob("*.json")):
            value = read_json(path)
            core = {k: v for k, v in value.items() if k != "record_sha256"}
            stable = {k: value[k] for k in ("branch_key", "physical_artifact_sha256", "result_scalar_sha256")}
            if (value["record_sha256"] != canonical_sha256(core) or
                    value["audit_identity"] != canonical_sha256(stable) or path.stem != value["branch_key"]):
                raise ValueError("branch audit identity/hash mismatch")
            if value["branch_key"] in result:
                raise ValueError("duplicate durable branch audit")
            result[value["branch_key"]] = value
        return result

    def before_branch(self, request, candidate, index, scenario):
        if self.read_only:
            raise ValueError("read-only branch audit store cannot reserve computation")
        if self._active is not None:
            raise ValueError("pending/uncertain DES attempt: refuse overlapping computation")
        if any(row["status"] != "completed" for row in self._known_attempts.values()):
            raise ValueError("pending/uncertain DES attempt requires checkpoint reconciliation")
        key = branch_key(request, candidate.id, scenario.scenario_id)
        if key in self._known_attempts:
            raise ValueError("DES branch already attempted; refuse uncommitted or duplicate recomputation")
        attempt = {"protocol": PROTOCOL, "attempt_id": uuid.uuid4().hex,
                   "branch_key": key, "request_sha256": canonical_sha256(request.to_dict()),
                   "candidate_id": candidate.id, "scenario_id": scenario.scenario_id,
                   "scenario_index": index, "origin": request.context.window_start,
                   "status": "pending", "reserved_compute_attempt": True}
        _atomic_json(self.root / "attempts" / (attempt["attempt_id"] + ".json"), attempt)
        self._known_attempts[key] = attempt
        self._active = attempt
        self._started = time.perf_counter()
        self._fault("after_attempt_reservation")

    def __call__(self, result, request):
        if self.read_only:
            raise ValueError("read-only branch audit store cannot record computation")
        attempt = self._active
        if attempt is None:
            raise ValueError("DES audit requires a durable pre-computation reservation")
        if (attempt["request_sha256"] != canonical_sha256(request.to_dict()) or
                attempt["scenario_id"] != result["scenario_id"] or
                attempt["scenario_index"] != result["scenario_index"]):
            raise ValueError("DES attempt/result identity mismatch")
        compute_seconds = time.perf_counter() - self._started
        started = time.perf_counter()
        checked = self.auditor(result, request)
        audit_seconds = time.perf_counter() - started
        self._fault("before_audit_write")
        artifact = None
        if result["scenario_index"] == 0:
            artifact = attempt["branch_key"] + ".json"
            _atomic_json(self.root / "artifacts" / artifact, result)
        value = {"protocol": PROTOCOL, **{key: attempt[key] for key in (
            "attempt_id", "branch_key", "request_sha256", "candidate_id", "scenario_id", "scenario_index", "origin")},
            "score": result["score"], "duration_hours": result["duration_hours"],
            "physical_artifact_sha256": canonical_sha256(result["final_state"]),
            "result_scalar_sha256": canonical_sha256({k: v for k, v in result.items() if k != "final_state"}),
            "audit": checked, "compute_seconds": compute_seconds, "audit_seconds": audit_seconds,
            "scenario_zero_artifact": artifact}
        value["audit_identity"] = canonical_sha256({k: value[k] for k in (
            "branch_key", "physical_artifact_sha256", "result_scalar_sha256")})
        value["record_sha256"] = canonical_sha256(value)
        path = self.root / "audits" / (attempt["branch_key"] + ".json")
        if path.exists():
            raise ValueError("refuse overwrite of existing branch audit")
        _atomic_json(path, value)
        self._fault("after_audit_write")
        attempt.update(status="completed", audit_identity=value["audit_identity"],
                       physical_artifact_sha256=value["physical_artifact_sha256"])
        _atomic_json(self.root / "attempts" / (attempt["attempt_id"] + ".json"), attempt)
        self._active = None
        return checked

    def reconcile(self, checkpoint, *, allow_uncommitted=False):
        """Verify all checkpoint branches, attempts and durable identities exactly.

        A pending attempt with a complete immutable audit can be classified as
        completed. A missing audit stays pending/uncertain and blocks recovery.
        """
        audits = self._audits()
        attempts = self._attempts()
        if len({a["branch_key"] for a in attempts}) != len(attempts):
            raise ValueError("duplicate DES attempt identity")
        for attempt in attempts:
            audit = audits.get(attempt["branch_key"])
            if audit is None or audit["attempt_id"] != attempt["attempt_id"]:
                raise ValueError("pending/uncertain DES attempt has no complete durable audit")
            if attempt["status"] != "completed":
                if self.read_only:
                    raise ValueError("pending/uncertain DES attempt needs explicit journal recovery")
                attempt.update(status="completed", recovered_from_complete_audit=True,
                               audit_identity=audit["audit_identity"],
                               physical_artifact_sha256=audit["physical_artifact_sha256"])
                _atomic_json(self.root / "attempts" / (attempt["attempt_id"] + ".json"), attempt)
            if (attempt["audit_identity"] != audit["audit_identity"] or
                    attempt["physical_artifact_sha256"] != audit["physical_artifact_sha256"]):
                raise ValueError("DES attempt/audit binding mismatch")
        expected = {}
        records = checkpoint.get("weekly_state", {}).get("decision_records", [])
        for record in records:
            for row in record["branches"]:
                key = branch_key(record["request"], row["candidate_id"], row["scenario_id"])
                if key in expected:
                    raise ValueError("duplicate committed branch identity")
                expected[key] = row
                audit = audits.get(key)
                if audit is None:
                    raise ValueError("checkpoint references missing durable branch audit")
                for field in ("candidate_id", "scenario_id", "physical_artifact_sha256", "score", "duration_hours"):
                    if audit[field] != row[field]:
                        raise ValueError("checkpoint/audit branch mismatch: " + field)
                scalar = {k: v for k, v in row.items() if k not in ("candidate_id", "physical_artifact_sha256")}
                if audit["result_scalar_sha256"] != canonical_sha256(scalar):
                    raise ValueError("checkpoint/audit scalar hash mismatch")
        if set(audits) != {a["branch_key"] for a in attempts}:
            raise ValueError("audit has no corresponding actual computation attempt")
        self._known_attempts = {row["branch_key"]: row for row in attempts}
        if not allow_uncommitted and set(expected) != set(audits):
            raise ValueError("uncommitted audited DES work; conservative recovery stops without recomputation")
        return {"protocol": PROTOCOL, "logical_branches": len(expected),
                "actual_compute_attempts": len(attempts), "persistent_audit_identities": len(audits),
                "pending_uncertain": 0, "audit_replay_des": 0,
                "compute_seconds": sum(a["compute_seconds"] for a in audits.values()),
                "audit_seconds": sum(a["audit_seconds"] for a in audits.values()),
                "audit_identity_set_sha256": canonical_sha256(sorted(a["audit_identity"] for a in audits.values()))}

    def commit_checkpoint(self, path, checkpoint):
        if self.read_only:
            raise ValueError("read-only branch audit store cannot commit a checkpoint")
        path = Path(path)
        accounting = self.reconcile(checkpoint)
        # Persist selected scenario zero, not the first evaluated candidate.
        audits = self._audits()
        for record in checkpoint.get("weekly_state", {}).get("decision_records", []):
            rows = [r for r in record["branches"] if r["candidate_id"] == record["selected_candidate_id"] and r["scenario_index"] == 0]
            if rows:
                row = rows[0]
                audit = audits[branch_key(record["request"], row["candidate_id"], row["scenario_id"])]
                value = read_json(self.root / "artifacts" / audit["scenario_zero_artifact"])
                if canonical_sha256(value["final_state"]) != audit["physical_artifact_sha256"]:
                    raise ValueError("retained physical artifact differs from audit")
                _atomic_json(self.run_dir / ("window-" + str(int(audit["origin"]))) / "branch-0.json", value)
        intent = {"protocol": PROTOCOL, "status": "pending", "checkpoint_name": path.name,
                  "checkpoint_sha256": hashlib.sha256(checkpoint_bytes(checkpoint)).hexdigest(),
                  "accounting": accounting}
        _atomic_json(self.root / "checkpoint-commit.json", intent)
        self._fault("before_checkpoint_write")
        save_checkpoint(path, checkpoint)
        self._fault("after_checkpoint_write")
        intent["status"] = "committed"
        _atomic_json(self.root / "checkpoint-commit.json", intent)
        self._export(accounting)
        return accounting

    def verify_checkpoint(self, path):
        path = Path(path)
        checkpoint = read_json(path)
        accounting = self.reconcile(checkpoint)
        intent_path = self.root / "checkpoint-commit.json"
        if intent_path.exists():
            intent = read_json(intent_path)
            if (intent["checkpoint_name"] != path.name or
                    intent["checkpoint_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest() or
                    intent["accounting"] != accounting):
                raise ValueError("checkpoint commit intent differs; pending/uncertain recovery stops")
            if intent["status"] != "committed":
                if self.read_only:
                    raise ValueError("checkpoint commit needs explicit journal recovery")
                intent.update(status="committed", recovered_after_checkpoint_write=True)
                _atomic_json(intent_path, intent)
        elif accounting["logical_branches"]:
            raise ValueError("checkpoint lacks durable commit intent")
        if not self.read_only:
            self._export(accounting)
        return checkpoint, accounting

    def _export(self, accounting):
        audits = self._audits()
        _atomic_json(self.run_dir / "branch-audits.json", [audits[k] for k in sorted(audits)])
        _atomic_json(self.run_dir / "branch-accounting.json", accounting)
