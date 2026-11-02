"""Bounded TypeSafe Choice adapter; credentials never enter serialized state.

The artificial stub is an interface fixture, not model effectiveness evidence.
Only successful live responses may populate the real-replay response store.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import tempfile
from threading import Event, Thread
import time
from typing import Callable
from urllib import error, request
from uuid import uuid4

from .provenance import canonical_sha256

MODEL = "jev-1.14.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
PROTOCOL_VERSION = "typesafe-choice-adapter-v1"
ACCOUNTING_VERSION = "http-attempt-accounting-v2"
TEMPLATE_VERSION = "weekly-portfolio-choice-v1"
STUB_VERSION = "artificial-stable-id-triangular-v1"
PROBABILITY_SUM_TOLERANCE = 1e-6
MAX_TOKEN_COUNT = 2**63 - 1
INPUT_USD_PER_MILLION_TOKENS = 0.042
INSTRUCTIONS = (
    "Choose the weekly mission portfolio with the strongest current-offer realized "
    "DSR plus RCR by the end of this decision window. All candidates already satisfy "
    "the same deterministic nominal rules, or share the declared mandatory fallback. "
    "Use only the supplied public state and candidate summaries. Consider current "
    "resource commitments, repair and replenishment constraints, mission value and "
    "aircraft-hour demand. Rejected offers remain in both offered denominators. "
    "There is no learned terminal value or guarantee of mission success. "
    "Return a Choice over the supplied candidate IDs."
)


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _public(value):
    forbidden = {"seed", "root_seed", "remaining_life_hours", "failure_due_at", "due_at",
                 "event_queue", "rng_states", "execution_rng", "initial_lifetimes",
                 "authorization", "typesafe_api_key", "api_key"}
    if isinstance(value, dict):
        if any(str(key).lower() in forbidden for key in value):
            raise ValueError("Choice input contains nonpublic or credential fields")
        for item in value.values():
            _public(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _public(item)


def build_choice_request(decision_request, candidates) -> dict:
    """Freeze the same legal request and candidate mapping for every method."""
    legal = decision_request.to_dict()
    criteria = {}
    for candidate in sorted(candidates, key=lambda item: item.id):
        if candidate.id in criteria:
            raise ValueError("Duplicate candidate ID")
        criteria[candidate.id] = _copy(candidate.summary)
    state = {"decision": legal, "candidate_summary_version": "public-portfolio-summary-v1",
             "units": {"time": "hours", "aircraft_hours": "aircraft-hours",
                       "value": "synthetic whole-mission value points"},
             "template_version": TEMPLATE_VERSION}
    _public(state)
    _public(criteria)
    return _copy({"model": MODEL, "state": state,
                  "questions": {"portfolio": {"type": "choice", "instructions": INSTRUCTIONS,
                                                "criteria": criteria}}})


@dataclass(frozen=True)
class JevConfig:
    mode: str = "stub"
    model: str = MODEL
    cache_dir: str | None = None
    max_http_requests: int = 10
    total_timeout_seconds: float = 120.0
    request_timeout_seconds: float = 30.0
    max_retries: int = 2
    backoff_initial_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    allow_live: bool = False
    network_policy: str = "allow"

    def __post_init__(self):
        if self.mode not in {"stub", "live", "real_replay"} or self.model != MODEL:
            raise ValueError("Unsupported Jev mode or pinned model")
        if type(self.allow_live) is not bool:
            raise ValueError("allow_live must be a boolean")
        if self.network_policy not in {"allow", "cache_only"}:
            raise ValueError("Unsupported network policy")
        for name in ("max_http_requests", "max_retries"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("total_timeout_seconds", "request_timeout_seconds", "backoff_initial_seconds",
                     "backoff_max_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.backoff_initial_seconds > self.backoff_max_seconds:
            raise ValueError("Initial backoff exceeds its cap")
        if self.cache_dir is not None and (type(self.cache_dir) is not str or not self.cache_dir.strip()):
            raise ValueError("cache_dir must be a nonempty path string")
        if self.mode in {"live", "real_replay"} and self.cache_dir is None:
            raise ValueError("Live/replay require a durable response and budget directory")

    def to_dict(self):
        return asdict(self)


class JevError(RuntimeError):
    """Sanitized categorical failure; callers may apply the frozen heuristic."""
    def __init__(self, code: str, audit: dict):
        self.code, self.audit = code, _copy(audit)
        super().__init__(f"Jev Choice failed: {code}")


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".jev-write-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class _RejectRedirect(request.HTTPRedirectHandler):
    """Never forward an authenticated request, even to the same origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_transport(payload, credential, timeout):
    """Only this transient stack frame handles the authorization header."""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    req = request.Request(ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + credential})
    try:
        with request.build_opener(_RejectRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                return response.status, {}, None
            try:
                parsed = json.loads(raw)
            except (ValueError, UnicodeError):
                parsed = None
            return response.status, dict(response.headers), parsed
    except error.HTTPError as exc:
        # Never log server error bodies: they can echo request credentials.
        try:
            return exc.code, dict(exc.headers), None
        finally:
            exc.close()


def _transport_with_deadline(transport, payload, credential, timeout):
    """Bound caller wall time even if the server continuously drips response bytes.

    Socket inactivity timeouts alone do not bound a whole HTTP attempt. Only the
    caller can validate/persist the result. A late worker result is discarded;
    the daemon never receives the adapter, ledger, cache, or any write callback.
    There can be at most one such worker per reserved HTTP attempt in this run.
    The external request may still complete after timeout: that is uncertain,
    not an exactly-once or server-side cancellation guarantee.
    """
    if timeout <= 0:
        raise TimeoutError("HTTP deadline elapsed before transport")
    deadline = time.monotonic() + timeout
    mailbox, abandoned = Queue(maxsize=1), Event()

    def invoke():
        try:
            outcome = (True, transport(payload, credential, timeout))
        except BaseException as exc:
            outcome = (False, exc)
        if not abandoned.is_set():
            mailbox.put_nowait(outcome)

    Thread(target=invoke, name="jev-http-attempt", daemon=True).start()
    try:
        succeeded, outcome = mailbox.get(timeout=max(0., deadline - time.monotonic()))
        if time.monotonic() > deadline:
            raise TimeoutError("HTTP attempt absolute deadline exceeded")
        if not succeeded:
            raise outcome
        return outcome
    except Empty:
        raise TimeoutError("HTTP attempt absolute deadline exceeded") from None
    finally:
        abandoned.set()


def validate_response(response, candidate_ids, model=MODEL):
    """Accept complete probability coverage only; never infer omitted zeros."""
    if type(response) is not dict or response.get("model") != model:
        raise ValueError("model_mismatch")
    answers = response.get("answers")
    if type(answers) is not dict or type(answers.get("portfolio")) is not dict:
        raise ValueError("answer_schema")
    answer = answers["portfolio"]
    if "type" in answer and answer["type"] != "choice":
        raise ValueError("answer_type")
    ids = set(candidate_ids)
    probabilities = answer.get("probabilities")
    if type(probabilities) is not dict or set(probabilities) != ids:
        raise ValueError("probability_coverage")
    for probability in probabilities.values():
        if type(probability) not in (int, float) or not 0 <= probability <= 1 or not math.isfinite(probability):
            raise ValueError("probability_value")
    if not math.isclose(math.fsum(probabilities.values()), 1., rel_tol=0,
                        abs_tol=PROBABILITY_SUM_TOLERANCE):
        raise ValueError("probability_sum")
    choice, confidence = answer.get("choice"), answer.get("confidence")
    if type(choice) is not str or choice not in ids:
        raise ValueError("choice_id")
    if probabilities[choice] < max(probabilities.values()):
        raise ValueError("choice_not_argmax")
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1 or not math.isfinite(confidence):
        raise ValueError("confidence_value")
    usage = response.get("usage")
    if type(usage) is not dict or not {"input_tokens", "output_tokens"} <= usage.keys():
        raise ValueError("usage_schema")
    if any(type(usage[key]) is not int or not 0 <= usage[key] <= MAX_TOKEN_COUNT
           for key in ("input_tokens", "output_tokens")):
        raise ValueError("usage_value")
    return {"model": model, "answers": {"portfolio": {
        "choice": choice, "probabilities": _copy(probabilities), "confidence": confidence}},
        "usage": {key: usage[key] for key in ("input_tokens", "output_tokens")}}


def _known_usage(response):
    """Extract only fully validated metering, never arbitrary response fields."""
    usage = response.get("usage") if type(response) is dict else None
    if (type(usage) is dict and all(type(usage.get(key)) is int and
                                  0 <= usage[key] <= MAX_TOKEN_COUNT
                                  for key in ("input_tokens", "output_tokens"))):
        return {key: usage[key] for key in ("input_tokens", "output_tokens")}
    return None


def _summarize_attempts(attempts):
    """Only this invocation's HTTP attempts incur new usage; cache reads do not."""
    known = {"input_tokens": 0, "output_tokens": 0}
    unknown, uncertain, retries = 0, 0, 0
    seen = set()
    for row in attempts:
        attempt_id = row["attempt_id"]
        if attempt_id in seen:
            raise ValueError("Duplicate HTTP attempt identity")
        seen.add(attempt_id)
        usage = _known_usage({"usage": row.get("known_usage")})
        if usage is None:
            unknown += 1
        else:
            for key in known:
                known[key] += usage[key]
        uncertain += bool(row.get("uncertain"))
        retries += row.get("retry_index", 0) > 0
    return {"accounting_version": ACCOUNTING_VERSION, "actual_http_requests": len(seen),
            "actual_usage": known, "known_usage": dict(known),
            "unknown_usage_attempts": unknown, "usage_complete": unknown == 0,
            "uncertain_attempts": uncertain, "retry_count": retries,
            "cost_estimate_usd": known["input_tokens"] * INPUT_USD_PER_MILLION_TOKENS / 1_000_000,
            "cost_estimate_is_lower_bound": unknown > 0,
            "cost_estimate_scope": "known metering only; not a service invoice"}


class ChoiceAdapter:
    """One bounded run, with an optional injected transport for protocol tests.

    The response directory is single-writer. It persists reservations before
    sending, so process death cannot refund an attempted request. An unfinished
    reservation remains uncertain and counts against the next process budget.
    """
    def __init__(self, config: JevConfig | None = None, *, transport: Callable | None = None,
                 clock: Callable = time.time, sleeper: Callable = time.sleep,
                 stub_response: dict | None = None, monotonic_clock: Callable | None = None):
        self.config = config or JevConfig()
        if not isinstance(self.config, JevConfig):
            raise ValueError("ChoiceAdapter requires JevConfig")
        self.transport, self.clock, self.sleeper = transport or _http_transport, clock, sleeper
        # A supplied test clock controls virtual elapsed time unless a separate
        # monotonic fixture is supplied. Production always uses monotonic time.
        self._monotonic_clock = monotonic_clock or (time.monotonic if clock is time.time else clock)
        self._transport_is_fixture = transport is not None
        if self._transport_is_fixture and self.config.mode != "live":
            raise ValueError("Transport injection is restricted to live-protocol unit tests")
        self.stub_response = _copy(stub_response) if stub_response is not None else None
        self.audit_log = []
        self._context = {}
        self.cache_dir = Path(self.config.cache_dir).resolve() if self.config.cache_dir else None
        self.ledger = {"protocol_version": PROTOCOL_VERSION, "started_at_epoch": clock(),
                       "max_http_requests": self.config.max_http_requests,
                       "total_timeout_seconds": self.config.total_timeout_seconds, "attempts": [],
                       "accounting_version": ACCOUNTING_VERSION, "budget_id": uuid4().hex}
        if self.config.mode == "live":
            path = self.cache_dir / "http-budget.json"
            if path.exists():
                stored = json.loads(path.read_text(encoding="utf-8"))
                if (type(stored) is not dict or stored.get("protocol_version") != PROTOCOL_VERSION or
                        stored.get("max_http_requests") != self.config.max_http_requests or
                        stored.get("total_timeout_seconds") != self.config.total_timeout_seconds or
                        type(stored.get("attempts")) is not list):
                    raise ValueError("Persistent HTTP budget identity mismatch")
                self.ledger = stored
                self._validate_and_migrate_ledger()
            elif self.config.network_policy == "cache_only":
                raise ValueError("Live cache-only requires the original durable HTTP budget")
            self._save_budget()
        # Epoch time carries the deadline across processes. A second monotonic
        # bound prevents a wall-clock rollback from extending this process.
        remaining = self.config.total_timeout_seconds - max(
            0., self.clock() - self.ledger["started_at_epoch"])
        self._process_deadline = self._monotonic_clock() + max(0., remaining)

    def _validate_and_migrate_ledger(self):
        """Old ledgers retain quota/time and unknown metering, never invented zeros."""
        ledger = self.ledger
        started = ledger.get("started_at_epoch")
        if type(started) not in (int, float) or not math.isfinite(started) or started < 0:
            raise ValueError("Invalid persistent budget start time")
        if ledger.get("accounting_version") not in {None, ACCOUNTING_VERSION}:
            raise ValueError("Unknown persistent accounting version")
        legacy = "accounting_version" not in ledger
        if legacy:
            ledger["budget_id"] = canonical_sha256({"legacy_budget": ledger,
                                                    "directory": str(self.cache_dir)})
            ledger["accounting_version"] = ACCOUNTING_VERSION
            ledger["migrated_from"] = PROTOCOL_VERSION
        if type(ledger.get("budget_id")) is not str or not ledger["budget_id"]:
            raise ValueError("Invalid persistent budget ID")
        if len(ledger["attempts"]) > self.config.max_http_requests:
            raise ValueError("Persistent attempts exceed authorized quota")
        for number, row in enumerate(ledger["attempts"], 1):
            if (type(row) is not dict or type(row.get("number")) is not int or row["number"] != number
                    or type(row.get("request_sha256")) is not str
                    or len(row["request_sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in row["request_sha256"])
                    or type(row.get("uncertain")) is not bool
                    or type(row.get("retry_index")) is not int or row["retry_index"] < 0):
                raise ValueError("Invalid persistent HTTP attempt")
            expected_id = f"{ledger['budget_id']}:{number}"
            if legacy:
                row.update(attempt_id=expected_id, known_usage=None, usage_complete=False,
                           context={}, metering_status="unknown_legacy")
            if row.get("attempt_id") != expected_id:
                raise ValueError("Persistent HTTP attempt identity mismatch")
            usage = row.get("known_usage")
            if usage is not None and _known_usage({"usage": usage}) != usage:
                raise ValueError("Invalid persistent attempt metering")
            if type(row.get("usage_complete")) is not bool or row["usage_complete"] != (usage is not None):
                raise ValueError("Persistent attempt metering completeness mismatch")
            if row.get("status") in {"success", "response_pending", "response_not_persisted"}:
                persisted = self._has_persisted_response(row)
                row.update(status="success" if persisted else "response_not_persisted",
                           uncertain=not persisted)

    def _has_persisted_response(self, attempt):
        """Reconcile a completed HTTP attempt with its durable, bound response.

        A crash may occur on either side of the cache rename. Reconciliation
        changes neither reservations nor known metering and never sends HTTP.
        """
        fixture = attempt.get("transport_kind") == "injected_test_transport"
        suffix, source = (".fixture.json", "transport_fixture") if fixture else (".live.json", "live")
        path = self.cache_dir / (attempt["request_sha256"] + suffix)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            payload = cached["request"]
            question = payload["questions"]["portfolio"]
            identity = {"request_sha256": canonical_sha256(payload), "model": self.config.model,
                        "template_version": TEMPLATE_VERSION,
                        "template_sha256": canonical_sha256(question["instructions"]),
                        "candidate_mapping_sha256": canonical_sha256(question["criteria"])}
            if (cached["source_mode"] != source or cached["identity"] != identity
                    or payload["model"] != self.config.model
                    or identity["request_sha256"] != attempt["request_sha256"]):
                return False
            response = validate_response(cached["response"], question["criteria"], self.config.model)
            origin = cached["live_origin"]
            if origin.get("attempt_id") != attempt["attempt_id"]:
                # Pre-accounting caches used the persisted attempt number.
                if not (attempt.get("metering_status") == "unknown_legacy"
                        and origin.get("attempt_id") is None
                        and origin.get("http_attempt_number") == attempt["number"]):
                    return False
            return (attempt["known_usage"] is None
                    or attempt["known_usage"] == response["usage"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def set_context(self, **context):
        """Attach nonsecret experiment labels; decision identity comes from input."""
        allowed = {"phase", "run", "scenario", "seed", "method", "decision", "window_start"}
        if set(context) - allowed or any(type(value) not in (str, int, float, type(None))
                                        for value in context.values()):
            raise ValueError("Invalid HTTP audit context")
        self._context = _copy(context)

    def usage_summary(self):
        return _summarize_attempts(self.ledger["attempts"])

    def _meter_audit(self, audit):
        if self.config.mode != "stub":
            audit.update(_summarize_attempts(audit["attempts"]))

    @property
    def http_requests(self):
        return len(self.ledger["attempts"])

    def _save_budget(self):
        if self.config.mode == "live":
            _atomic_json(self.cache_dir / "http-budget.json", self.ledger)

    def _remaining(self):
        return min(self.config.total_timeout_seconds - max(0., self.clock() - self.ledger["started_at_epoch"]),
                   self._process_deadline - self._monotonic_clock())

    def _fail(self, code, audit, *, uncertain=False):
        self._meter_audit(audit)
        audit.update(valid_jev=False, valid_response=False, error_code=code,
                     uncertain=uncertain or audit["uncertain"], actual_http_requests=len(audit["attempts"]),
                     total_http_requests=self.http_requests,
                     latency_seconds=max(0., self.clock() - audit.pop("_started")),
                     fallback_required=True,
                     cost_estimate_usd=audit["actual_usage"]["input_tokens"] * INPUT_USD_PER_MILLION_TOKENS / 1_000_000,
                     cost_basis={"input_usd_per_million_tokens": INPUT_USD_PER_MILLION_TOKENS,
                                 "output_usd_per_million_tokens": 0., "verified_on": "2026-11-02",
                                 "source": "https://docs.typesafe.ai/models"})
        self.audit_log.append(_copy(audit))
        raise JevError(code, audit)

    def _finish(self, response, audit, *, cache_hit=False, skipped=None):
        answer = response["answers"]["portfolio"]
        ranked = sorted(answer["probabilities"], key=lambda key: (-answer["probabilities"][key], key))
        self._meter_audit(audit)
        actual_usage = audit["actual_usage"]
        audit.update(ranked_ids=ranked, choice=answer["choice"], probabilities=answer["probabilities"],
                     confidence=answer["confidence"], response=response, usage=response["usage"],
                     actual_usage=actual_usage, valid_response=True,
                     valid_jev=self.config.mode in {"live", "real_replay"} and skipped is None
                               and not self._transport_is_fixture,
                     api_skipped_reason=skipped, cache_hit=cache_hit, fallback_required=False,
                     actual_http_requests=len(audit["attempts"]), total_http_requests=self.http_requests,
                     latency_seconds=max(0., self.clock() - audit.pop("_started")),
                     cost_estimate_usd=actual_usage["input_tokens"] * INPUT_USD_PER_MILLION_TOKENS / 1_000_000,
                     cost_basis={"input_usd_per_million_tokens": INPUT_USD_PER_MILLION_TOKENS,
                                 "output_usd_per_million_tokens": 0., "verified_on": "2026-11-02",
                                 "source": "https://docs.typesafe.ai/models"})
        if self.config.mode != "stub":
            audit["response_usage"] = _copy(response["usage"])
            audit["source_usage"] = _copy(response["usage"]) if skipped is None else None
        self.audit_log.append(_copy(audit))
        return _copy(audit)

    def rank(self, payload: dict) -> dict:
        # Canonical presentation is also the byte-order used by HTTP; a caller
        # cannot accidentally reorder criteria while retaining the cache hash.
        payload = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
        if (type(payload) is not dict or set(payload) != {"model", "state", "questions"}
                or payload["model"] != self.config.model):
            raise ValueError("Choice request model or top-level schema mismatch")
        questions = payload["questions"]
        if type(questions) is not dict or set(questions) != {"portfolio"}:
            raise ValueError("Only one portfolio Choice is supported")
        question = questions["portfolio"]
        if type(question) is not dict or set(question) != {"type", "instructions", "criteria"} or question["type"] != "choice":
            raise ValueError("Invalid portfolio Choice schema")
        criteria = question["criteria"]
        if type(criteria) is not dict or any(type(key) is not str or not key for key in criteria):
            raise ValueError("Candidate IDs must be nonempty strings")
        _public(payload)
        ids = sorted(criteria)
        request_hash = canonical_sha256(payload)
        identity = {"request_sha256": request_hash, "model": self.config.model,
                    "template_version": TEMPLATE_VERSION,
                    "template_sha256": canonical_sha256(question["instructions"]),
                    "candidate_mapping_sha256": canonical_sha256(criteria)}
        prior_uncertain = [row["number"] for row in self.ledger["attempts"]
                           if row["request_sha256"] == request_hash and row["uncertain"]]
        audit = {**identity, "protocol_version": PROTOCOL_VERSION, "mode": self.config.mode,
                 "request": payload, "attempts": [], "errors": [], "uncertain": bool(prior_uncertain),
                 "prior_uncertain_attempts": prior_uncertain,
                 "transport_kind": "injected_test_transport" if self._transport_is_fixture else "urllib_http",
                 "usage": {"input_tokens": 0, "output_tokens": 0},
                 "actual_usage": {"input_tokens": 0, "output_tokens": 0},
                 "_started": self.clock(), "stub_fixture_version": STUB_VERSION if self.config.mode == "stub" else None}
        if self.config.mode != "stub":
            decision = payload.get("state", {}).get("decision", {})
            context = dict(self._context)
            if type(decision) is dict:
                context["decision"] = decision.get("request_id")
                context["window_start"] = decision.get("context", {}).get(
                    "window_start", decision.get("observation", {}).get("time"))
            audit.update(context=context, network_policy=self.config.network_policy,
                         response_source=None, source_usage=None)
        if len(ids) < 2:
            response = {"model": MODEL, "answers": {"portfolio": {
                "choice": ids[0] if ids else None, "probabilities": {key: 1. for key in ids},
                "confidence": None}}, "usage": {"input_tokens": 0, "output_tokens": 0}}
            return self._finish(response, audit, skipped="singleton_pool" if ids else "empty_pool")
        if self.config.mode == "stub":
            denominator = len(ids) * (len(ids) + 1) / 2
            response = self.stub_response if self.stub_response is not None else {
                "model": MODEL, "answers": {"portfolio": {"choice": ids[0],
                    "probabilities": {key: (len(ids) - i) / denominator for i, key in enumerate(ids)},
                    "confidence": 0.5}}, "usage": {"input_tokens": 0, "output_tokens": 0}}
            try:
                response = validate_response(response, ids)
            except ValueError as exc:
                self._fail("invalid_response:" + str(exc), audit)
            return self._finish(response, audit)
        cache_source = "transport_fixture" if self._transport_is_fixture else "live"
        cache_path = self.cache_dir / (request_hash + (".fixture.json" if self._transport_is_fixture else ".live.json"))
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (cached.get("source_mode") != cache_source or cached.get("identity") != identity or
                        cached.get("request") != payload):
                    raise ValueError("cache_identity")
                response = validate_response(cached["response"], ids)
            except (ValueError, KeyError, TypeError):
                self._fail("cache_invalid", audit)
            audit["live_origin"] = cached.get("live_origin")
            audit["response_source"] = "fixture_cache" if self._transport_is_fixture else "live_cache"
            return self._finish(response, audit, cache_hit=True)
        if self.config.mode == "real_replay":
            self._fail("replay_cache_miss", audit)
        if self.config.network_policy == "cache_only":
            self._fail("cache_only_miss", audit)
        if not self.config.allow_live:
            self._fail("live_disabled", audit)
        credential = os.environ.get("TYPESAFE_API_KEY")
        if not credential:
            self._fail("missing_credential", audit)
        for retry in range(self.config.max_retries + 1):
            if self.http_requests >= self.config.max_http_requests:
                self._fail("http_budget_exhausted", audit, uncertain=audit["uncertain"])
            if self._remaining() <= 0:
                self._fail("total_timeout", audit, uncertain=audit["uncertain"])
            attempt = {"number": self.http_requests + 1, "request_sha256": request_hash,
                       "retry_index": retry, "started_at_epoch": self.clock(),
                       "status": "uncertain", "uncertain": True,
                       "attempt_id": f"{self.ledger['budget_id']}:{self.http_requests + 1}",
                       "context": _copy(audit["context"]), "known_usage": None,
                       "usage_complete": False,
                       "transport_kind": audit["transport_kind"]}
            self.ledger["attempts"].append(attempt)
            audit["attempts"].append(attempt)
            self._save_budget()  # reservation survives a process crash during HTTP
            timeout = min(self.config.request_timeout_seconds, self._remaining())
            retryable, code, headers, response = False, None, {}, None
            try:
                status, headers, response = _transport_with_deadline(self.transport, payload, credential, timeout)
                attempt.update(http_status=status, uncertain=False)
                known_usage = _known_usage(response)
                attempt.update(known_usage=known_usage, usage_complete=known_usage is not None)
                if status == 200:
                    try:
                        response = validate_response(response, ids)
                    except ValueError as exc:
                        code = "invalid_response:" + str(exc)
                else:
                    code = ("redirect_rejected" if type(status) is int and 300 <= status < 400 else
                            {400: "request_error", 401: "authentication_error", 422: "schema_error",
                             429: "rate_limited", 529: "overloaded"}.get(status, "http_error"))
                    retryable = status in {429, 529}
            except (TimeoutError, error.URLError, OSError):
                code, retryable = "transport_timeout_or_error", True
                audit["uncertain"] = True
            except Exception:
                # Deliberately omit exception text: third-party transports may
                # echo headers, URLs or credentials in their exception strings.
                code = "transport_error"
                audit["uncertain"] = True
            attempt["latency_seconds"] = max(0., self.clock() - attempt["started_at_epoch"])
            if code is None and self._remaining() < 0:
                code = "total_timeout"
            if code is None:
                # Keep this reservation uncertain until the response is durable.
                # A process death must not leave a successful, unreplayable row.
                attempt.update(status="response_pending", uncertain=True)
            else:
                attempt["status"] = code
            self._save_budget()
            if code is None:
                cached = {"source_mode": cache_source, "identity": identity, "request": payload,
                          "response": response, "live_origin": {
                              "http_attempt_number": attempt["number"],
                              "attempt_id": attempt["attempt_id"],
                              "accounting_version": ACCOUNTING_VERSION,
                              "received_at_epoch": self.clock(), "latency_seconds": attempt["latency_seconds"]}}
                try:
                    _atomic_json(cache_path, cached)
                except OSError:
                    attempt.update(status="response_not_persisted", uncertain=True)
                    audit["uncertain"] = True
                    self._save_budget()
                    self._fail("response_not_persisted", audit, uncertain=True)
                attempt.update(status="success", uncertain=False)
                self._save_budget()
                audit["live_origin"] = cached["live_origin"]
                audit["response_source"] = "injected_fixture" if self._transport_is_fixture else "live_http"
                return self._finish(response, audit)
            audit["errors"].append({"code": code, "attempt_number": attempt["number"],
                                    "retryable": retryable, "uncertain": attempt["uncertain"]})
            if not retryable or retry >= self.config.max_retries:
                self._fail(code, audit, uncertain=audit["uncertain"])
            delay = min(self.config.backoff_max_seconds, self.config.backoff_initial_seconds * 2 ** retry)
            lower_headers = {str(key).lower(): value for key, value in headers.items()}
            server_delay = "retry-after-ms" in lower_headers or "retry-after" in lower_headers
            try:
                if "retry-after-ms" in lower_headers:
                    requested = float(lower_headers["retry-after-ms"]) / 1000
                else:
                    requested = float(lower_headers.get("retry-after", delay))
            except (TypeError, ValueError):
                try:
                    requested = parsedate_to_datetime(lower_headers["retry-after"]).timestamp() - self.clock()
                except (KeyError, TypeError, ValueError, OverflowError):
                    requested = delay
            if server_delay and requested >= 0 and (
                    not math.isfinite(requested) or requested > self.config.backoff_max_seconds
                    or requested >= self._remaining()):
                # Respect the server minimum without exceeding this experiment's
                # waiting budget. Clamping it and sending early is not allowed.
                attempt["retry_requested_seconds"] = requested if math.isfinite(requested) else None
                attempt["retry_stopped_reason"] = "retry_after_exceeds_budget"
                self._save_budget()
                self._fail("retry_after_exceeds_budget", audit, uncertain=audit["uncertain"])
            if math.isfinite(requested) and requested >= 0:
                delay = max(delay, requested)
            attempt["retry_delay_seconds"] = delay
            self._save_budget()
            if delay >= self._remaining():
                self._fail("total_timeout", audit, uncertain=audit["uncertain"])
            self.sleeper(delay)
        raise AssertionError("Bounded retry loop must return or raise")
