"""Protocol fixtures only: injected HTTP is never a real Jev effectiveness run."""
from copy import deepcopy
from dataclasses import replace
import json
import math
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sim_jev.jev import (ChoiceAdapter, JevConfig, JevError, MODEL, TEMPLATE_VERSION,
                         build_choice_request, validate_response)
from sim_jev.provenance import canonical_sha256


def payload(ids=("C2", "C1", "C3")):
    request = SimpleNamespace(to_dict=lambda: {"observation": {"time": 168},
                                               "request_id": "legal-request"})
    return build_choice_request(request, [SimpleNamespace(id=key, summary={"tasks": [key], "value": 10})
                                           for key in ids])


def response(ids=("C1", "C2", "C3")):
    return {"model": MODEL, "answers": {"portfolio": {"choice": ids[0],
            "probabilities": {key: 1 / len(ids) for key in ids}, "confidence": 0.5}},
            "usage": {"input_tokens": 120, "output_tokens": 12}}


class Clock:
    def __init__(self):
        self.now, self.sleeps = 1000., []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def live_config(directory, **kwargs):
    return JevConfig(mode="live", cache_dir=directory, allow_live=True, **kwargs)


class JevProtocolTests(unittest.TestCase):
    def test_typed_configuration_and_pinned_model(self):
        for overrides in ({"model": "latest"}, {"max_http_requests": True}, {"max_retries": -1},
                          {"total_timeout_seconds": float("nan")}, {"request_timeout_seconds": 0},
                          {"allow_live": "yes"}, {"mode": "live"}, {"cache_dir": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                JevConfig(**overrides)
        self.assertEqual(JevConfig().to_dict()["model"], MODEL)

    def test_request_is_public_and_stable_for_reordered_candidates(self):
        self.assertEqual(payload(), payload(("C3", "C2", "C1")))
        hidden = SimpleNamespace(to_dict=lambda: {"remaining_life_hours": 123})
        with self.assertRaisesRegex(ValueError, "nonpublic"):
            build_choice_request(hidden, [])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            payload(("C1", "C1"))

    def test_stub_is_artificial_repeatable_and_not_live_evidence(self):
        adapter = ChoiceAdapter()
        result = adapter.rank(payload())
        self.assertEqual(result["ranked_ids"], ["C1", "C2", "C3"])
        self.assertEqual(result["probabilities"], {"C1": .5, "C2": 1 / 3, "C3": 1 / 6})
        self.assertEqual(result["actual_http_requests"], 0)
        self.assertFalse(result["valid_jev"])
        self.assertTrue(result["valid_response"])
        self.assertIsNotNone(result["stub_fixture_version"])
        self.assertEqual(adapter.rank(payload())["request_sha256"], result["request_sha256"])

    def test_zero_singleton_paths_skip_api_explicitly(self):
        for ids, reason in (((), "empty_pool"), (("C1",), "singleton_pool")):
            result = ChoiceAdapter().rank(payload(ids))
            self.assertEqual(result["api_skipped_reason"], reason)
            self.assertEqual(result["ranked_ids"], list(ids))
            self.assertEqual(result["actual_http_requests"], 0)
            self.assertFalse(result["valid_jev"])

    def test_ties_use_stable_id_and_choice_is_separately_preserved(self):
        fixture = response()
        fixture["answers"]["portfolio"]["choice"] = "C3"
        result = ChoiceAdapter(stub_response=fixture).rank(payload())
        self.assertEqual(result["choice"], "C3")
        self.assertEqual(result["ranked_ids"], ["C1", "C2", "C3"])

    def test_response_rejects_bad_ids_coverage_types_ranges_and_model(self):
        mutations = [
            lambda r: r.update(model="jev-latest"),
            lambda r: r["answers"]["portfolio"].update(type="score"),
            lambda r: r["answers"]["portfolio"].update(type=True),
            lambda r: r["answers"]["portfolio"].update(choice="unknown"),
            lambda r: r["answers"]["portfolio"]["probabilities"].pop("C1"),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(unknown=0),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(C1=True),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(C1=-1),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(C1=1.1),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(C1=float("nan")),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(C1=float("inf")),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(C1=10**999),
            lambda r: r["answers"]["portfolio"]["probabilities"].update(C1=0),
            lambda r: r["answers"]["portfolio"].update(confidence=True),
            lambda r: r["answers"]["portfolio"].update(confidence=-.1),
            lambda r: r["answers"]["portfolio"].update(confidence=float("nan")),
            lambda r: r["usage"].update(input_tokens=True),
            lambda r: r["usage"].update(output_tokens=-1),
            lambda r: r["usage"].update(input_tokens=10**999),
            lambda r: r.pop("usage"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                malformed = response()
                mutate(malformed)
                with self.assertRaises(ValueError):
                    validate_response(malformed, ("C1", "C2", "C3"))

    def test_invalid_response_error_is_categorical_and_fallback_required(self):
        malformed = response()
        malformed["answers"]["portfolio"]["choice"] = "UNKNOWN"
        with self.assertRaises(JevError) as caught:
            ChoiceAdapter(stub_response=malformed).rank(payload())
        self.assertEqual(caught.exception.code, "invalid_response:choice_id")
        self.assertTrue(caught.exception.audit["fallback_required"])
        self.assertFalse(caught.exception.audit["valid_jev"])

    def test_missing_credential_and_explicit_live_switch_do_not_send(self):
        for allow_live, expected in ((False, "live_disabled"), (True, "missing_credential")):
            with TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
                adapter = ChoiceAdapter(replace(live_config(folder), allow_live=allow_live),
                                        transport=lambda *args: self.fail("must not call"))
                with self.assertRaises(JevError) as caught:
                    adapter.rank(payload())
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(adapter.http_requests, 0)

    def test_nonretryable_http_statuses_are_classified_and_never_echo_bodies(self):
        for status, code in ((400, "request_error"), (401, "authentication_error"), (422, "schema_error")):
            with self.subTest(status=status), TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
                adapter = ChoiceAdapter(live_config(folder), transport=lambda *args: (status, {}, {"error": "test-secret"}))
                with self.assertRaises(JevError) as caught:
                    adapter.rank(payload())
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(adapter.http_requests, 1)
                self.assertNotIn("test-secret", json.dumps(caught.exception.audit))
                self.assertNotIn("test-secret", (Path(folder) / "http-budget.json").read_text())

    def test_retry_after_bounded_backoff_and_transport_fixture_separation(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            clock = Clock()
            replies = iter([(429, {"Retry-After": "8"}, None),
                            (529, {"retry-after-ms": "1500"}, None), (200, {}, response())])
            adapter = ChoiceAdapter(live_config(folder), transport=lambda *args: next(replies),
                                    clock=clock, sleeper=clock.sleep)
            result = adapter.rank(payload())
            self.assertEqual(clock.sleeps, [8., 1.5])
            self.assertEqual(result["actual_http_requests"], 3)
            self.assertFalse(result["valid_jev"])
            self.assertEqual(result["transport_kind"], "injected_test_transport")
            self.assertEqual(len(list(Path(folder).glob("*.fixture.json"))), 1)
            self.assertEqual(list(Path(folder).glob("*.live.json")), [])
            self.assertAlmostEqual(result["cost_estimate_usd"], 120 * .042 / 1_000_000)

    def test_timeout_is_uncertain_and_total_request_budget_survives_recreation(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            clock = Clock()
            def timeout(*args):
                raise TimeoutError("contains test-secret which must not be logged")
            config = live_config(folder, max_http_requests=2, max_retries=5)
            adapter = ChoiceAdapter(config, transport=timeout, clock=clock, sleeper=clock.sleep)
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "http_budget_exhausted")
            self.assertTrue(caught.exception.audit["uncertain"])
            self.assertEqual(adapter.http_requests, 2)
            self.assertNotIn("test-secret", json.dumps(caught.exception.audit))
            restored = ChoiceAdapter(config, transport=timeout, clock=clock, sleeper=clock.sleep)
            with self.assertRaises(JevError) as second:
                restored.rank(payload())
            self.assertEqual(second.exception.code, "http_budget_exhausted")
            self.assertEqual(second.exception.audit["actual_http_requests"], 0)

    def test_total_timeout_includes_retry_wait_and_bounds_transport_timeout(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            clock, timeouts = Clock(), []
            def transport(p, key, timeout):
                timeouts.append(timeout)
                return 429, {"Retry-After": "8"}, None
            adapter = ChoiceAdapter(live_config(folder, total_timeout_seconds=2), transport=transport,
                                    clock=clock, sleeper=clock.sleep)
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "retry_after_exceeds_budget")
            self.assertEqual(timeouts, [2])
            self.assertEqual(clock.sleeps, [])

    def test_crash_reservation_persists_as_uncertain_and_is_not_refunded(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            def crash(*args):
                raise KeyboardInterrupt()
            adapter = ChoiceAdapter(live_config(folder, max_http_requests=1), transport=crash)
            with self.assertRaises(KeyboardInterrupt):
                adapter.rank(payload())
            ledger = json.loads((Path(folder) / "http-budget.json").read_text())
            self.assertEqual(ledger["attempts"][0]["status"], "uncertain")
            self.assertTrue(ledger["attempts"][0]["uncertain"])
            restored = ChoiceAdapter(live_config(folder, max_http_requests=1), transport=crash)
            with self.assertRaises(JevError) as caught:
                restored.rank(payload())
            self.assertEqual(caught.exception.code, "http_budget_exhausted")

    def test_successful_fixture_cache_reuse_makes_no_additional_http_request(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            calls = []
            def transport(*args):
                calls.append(1)
                return 200, {}, response()
            config = live_config(folder)
            first = ChoiceAdapter(config, transport=transport).rank(payload())
            restored = ChoiceAdapter(config, transport=transport)
            second = restored.rank(payload())
            self.assertEqual(len(calls), 1)
            self.assertEqual(first["ranked_ids"], second["ranked_ids"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["actual_http_requests"], 0)
            self.assertEqual(second["actual_usage"]["input_tokens"], 0)
            self.assertEqual(restored.http_requests, 1)
            with self.assertRaises(JevError) as caught:
                ChoiceAdapter(JevConfig(mode="real_replay", cache_dir=folder)).rank(payload())
            self.assertEqual(caught.exception.code, "replay_cache_miss")

    def test_real_replay_requires_exact_full_request_template_model_and_mapping(self):
        # Handcrafted protocol fixture is local to this test; it is never
        # exported as an actual live response or experiment effectiveness data.
        with TemporaryDirectory() as folder:
            p = payload()
            identity = {"request_sha256": canonical_sha256(p), "model": MODEL,
                        "template_version": TEMPLATE_VERSION,
                        "template_sha256": canonical_sha256(p["questions"]["portfolio"]["instructions"]),
                        "candidate_mapping_sha256": canonical_sha256(p["questions"]["portfolio"]["criteria"])}
            record = {"source_mode": "live", "identity": identity, "request": p, "response": response(),
                      "test_fixture_note": "Artificial unit-test record; no real HTTP occurred"}
            target = Path(folder) / (canonical_sha256(p) + ".live.json")
            target.write_text(json.dumps(record), encoding="utf-8")
            adapter = ChoiceAdapter(JevConfig(mode="real_replay", cache_dir=folder))
            self.assertEqual(adapter.rank(p)["actual_http_requests"], 0)
            for field in ("source_mode", "request", "identity"):
                with self.subTest(field=field):
                    corrupted = deepcopy(record)
                    corrupted[field] = "mismatch"
                    target.write_text(json.dumps(corrupted), encoding="utf-8")
                    with self.assertRaises(JevError) as caught:
                        adapter.rank(p)
                    self.assertEqual(caught.exception.code, "cache_invalid")
            target.write_text(json.dumps(record), encoding="utf-8")
            different = deepcopy(p)
            different["state"]["decision"]["observation"]["time"] += 1
            with self.assertRaises(JevError) as caught:
                adapter.rank(different)
            self.assertEqual(caught.exception.code, "replay_cache_miss")

    def test_returned_audit_does_not_mutate_persistent_budget(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            adapter = ChoiceAdapter(live_config(folder), transport=lambda *args: (200, {}, response()))
            audit = adapter.rank(payload())
            audit["attempts"][0]["status"] = "modified"
            self.assertEqual(adapter.ledger["attempts"][0]["status"], "success")

    def test_choice_type_when_present_and_http_criteria_are_canonical(self):
        checked = response()
        checked["answers"]["portfolio"]["type"] = "choice"
        self.assertEqual(validate_response(checked, ("C1", "C2", "C3"))["model"], MODEL)
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            captured = []
            def transport(sent, *_):
                captured.append(list(sent["questions"]["portfolio"]["criteria"]))
                return 200, {}, response()
            p = payload()
            p["questions"]["portfolio"]["criteria"] = dict(reversed(list(p["questions"]["portfolio"]["criteria"].items())))
            ChoiceAdapter(live_config(folder), transport=transport).rank(p)
            self.assertEqual(captured, [["C1", "C2", "C3"]])

    def test_invalid_live_response_is_not_cached_or_retried_and_usage_is_preserved(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            bad = response()
            bad["answers"]["portfolio"]["type"] = "score"
            adapter = ChoiceAdapter(live_config(folder), transport=lambda *args: (200, {}, bad))
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "invalid_response:answer_type")
            self.assertEqual(adapter.http_requests, 1)
            self.assertEqual(caught.exception.audit["actual_usage"]["input_tokens"], 120)
            self.assertEqual(list(Path(folder).glob("*.fixture.json")), [])

    def test_retry_limit_and_total_elapsed_during_http(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            clock = Clock()
            def slow(*args):
                clock.now += 3
                return 200, {}, response()
            adapter = ChoiceAdapter(live_config(folder, total_timeout_seconds=2), transport=slow,
                                    clock=clock, sleeper=clock.sleep)
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "total_timeout")
            self.assertEqual(adapter.http_requests, 1)
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            adapter = ChoiceAdapter(live_config(folder, max_retries=0), transport=lambda *args: (529, {}, None))
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "overloaded")
            self.assertEqual(adapter.http_requests, 1)

    def test_response_cache_write_failure_is_uncertain_and_budget_remains(self):
        from sim_jev import jev
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            real_write = jev._atomic_json
            def cannot_persist_response(path, value):
                if path.name.endswith(".fixture.json"):
                    raise OSError("synthetic filesystem error")
                return real_write(path, value)
            adapter = ChoiceAdapter(live_config(folder), transport=lambda *args: (200, {}, response()))
            with patch.object(jev, "_atomic_json", side_effect=cannot_persist_response), self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "response_not_persisted")
            self.assertTrue(caught.exception.audit["uncertain"])
            saved = json.loads((Path(folder) / "http-budget.json").read_text())
            self.assertTrue(saved["attempts"][0]["uncertain"])
            self.assertEqual(len(saved["attempts"]), 1)

    def test_retry_after_over_cap_or_total_budget_stops_without_early_retry(self):
        cases = [({"Retry-After": "200"}, 120), ({"retry-after-ms": "9000"}, 120),
                 ({"Retry-After": "3"}, 2)]
        for headers, budget in cases:
            with self.subTest(headers=headers), TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
                clock = Clock()
                adapter = ChoiceAdapter(live_config(folder, total_timeout_seconds=budget),
                                        transport=lambda *args: (429, headers, None),
                                        clock=clock, sleeper=clock.sleep)
                with self.assertRaises(JevError) as caught:
                    adapter.rank(payload())
                self.assertEqual(caught.exception.code, "retry_after_exceeds_budget")
                self.assertEqual(adapter.http_requests, 1)
                self.assertEqual(clock.sleeps, [])
                self.assertEqual(adapter.ledger["attempts"][0]["retry_stopped_reason"],
                                 "retry_after_exceeds_budget")

    def test_blocking_transport_has_absolute_deadline_and_late_success_cannot_write_cache(self):
        release, completed = Event(), Event()
        def blocking(*args):
            release.wait(2)
            completed.set()
            return 200, {}, response()
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}):
            adapter = ChoiceAdapter(live_config(folder, request_timeout_seconds=.06,
                                                total_timeout_seconds=.12, max_retries=0), transport=blocking)
            start = time.monotonic()
            try:
                with self.assertRaises(JevError) as caught:
                    adapter.rank(payload())
                self.assertLess(time.monotonic() - start, .5)
                self.assertTrue(caught.exception.audit["uncertain"])
                self.assertEqual(adapter.http_requests, 1)
                before = (Path(folder) / "http-budget.json").read_bytes()
            finally:
                release.set()
            self.assertTrue(completed.wait(1))
            time.sleep(.02)
            self.assertEqual(list(Path(folder).glob("*.fixture.json")), [])
            self.assertEqual((Path(folder) / "http-budget.json").read_bytes(), before)
            self.assertNotIn("test-secret", before.decode("utf-8"))

    def test_real_local_http_slow_drip_respects_total_deadline(self):
        from sim_jev import jev
        stop, received = Event(), Event()
        class SlowResponse(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                raw = json.dumps(response()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                received.set()
                try:
                    for byte in raw:
                        if stop.is_set():
                            break
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.015)  # Every byte arrives before socket inactivity timeout.
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowResponse)
        server.daemon_threads = True
        worker = Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        worker.start()
        try:
            with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-test-only"}), \
                 patch.object(jev, "ENDPOINT", f"http://127.0.0.1:{server.server_address[1]}/systemone"):
                adapter = ChoiceAdapter(live_config(folder, request_timeout_seconds=1,
                                                    total_timeout_seconds=.15, max_retries=0))
                start = time.monotonic()
                with self.assertRaises(JevError) as caught:
                    adapter.rank(payload())
                elapsed = time.monotonic() - start
                self.assertTrue(received.is_set())
                self.assertLess(elapsed, .6)
                self.assertTrue(caught.exception.audit["uncertain"])
                self.assertEqual(adapter.http_requests, 1)
                stop.set()
                time.sleep(.05)
                self.assertEqual(list(Path(folder).glob("*.live.json")), [])
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            worker.join(1)


if __name__ == "__main__":
    unittest.main()
