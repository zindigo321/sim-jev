"""2C acceptance counterexamples; all credentials and transports are fixtures."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib import request
from urllib.response import addinfourl

from sim_jev import jev
from sim_jev.jev import ChoiceAdapter, JevConfig, JevError, validate_response
from test_phase2c_jev import Clock, live_config, payload, response


@contextmanager
def local_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    worker = Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(1)


class JevAcceptanceTests(unittest.TestCase):
    def test_default_transport_refuses_same_origin_cross_port_and_cross_host_redirects(self):
        destination_requests, source_requests = [], []
        class Target(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                destination_requests.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
            do_POST = do_GET

        class Source(BaseHTTPRequestHandler):
            location, status = "", 302
            def log_message(self, *args):
                pass
            def do_POST(self):
                source_requests.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(self.status)
                self.send_header("Location", self.location)
                self.end_headers()
            def do_GET(self):
                destination_requests.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

        with local_server(Target) as target, local_server(Source) as source:
            source_url = f"http://127.0.0.1:{source.server_port}/choice"
            destinations = [f"http://127.0.0.1:{target.server_port}/sink",
                            f"http://localhost:{target.server_port}/sink", "/sink"]
            for code in (301, 302, 303, 307, 308):
                for destination in destinations:
                    with self.subTest(code=code, destination=destination), TemporaryDirectory() as folder:
                        Source.location, Source.status = destination + "?credential=do-not-log", code
                        with patch.object(jev, "ENDPOINT", source_url), \
                             patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
                            adapter = ChoiceAdapter(live_config(folder))
                            with self.assertRaises(JevError) as caught:
                                adapter.rank(payload())
                        self.assertEqual(caught.exception.code, "redirect_rejected")
                        self.assertEqual(adapter.http_requests, 1)
                        self.assertEqual(adapter.usage_summary()["unknown_usage_attempts"], 1)
                        self.assertNotIn("do-not-log", json.dumps(caught.exception.audit))
                        self.assertNotIn("local-fake-token", json.dumps(adapter.ledger))
            self.assertEqual(len(source_requests), 15)
            self.assertEqual(destination_requests, [])

    def test_https_to_http_downgrade_never_reaches_local_target(self):
        # The HTTPS response handler is synthetic; the target is a real local
        # HTTP sentinel. This exercises urllib's complete redirect dispatch.
        destination_requests = []
        class Target(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                destination_requests.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
            do_POST = do_GET

        with local_server(Target) as target:
            def fake_https(handler, req):
                headers = Message()
                headers["Location"] = f"http://127.0.0.1:{target.server_port}/downgrade"
                result = addinfourl(BytesIO(b""), headers, req.full_url, 302)
                result.msg = "Found"
                return result
            with patch.object(request.HTTPSHandler, "https_open", fake_https):
                status, _, body = jev._http_transport(payload(), "local-fake-token", 1)
            self.assertEqual(status, 302)
            self.assertIsNone(body)
            self.assertEqual(destination_requests, [])

    def test_choice_requires_argmax_but_any_exact_tie_is_allowed(self):
        data = response()
        data["answers"]["portfolio"]["probabilities"] = {"C1": .1, "C2": .8, "C3": .1}
        with self.assertRaisesRegex(ValueError, "choice_not_argmax"):
            validate_response(data, ("C1", "C2", "C3"))
        data["answers"]["portfolio"]["choice"] = "C2"
        validate_response(data, ("C1", "C2", "C3"))
        data["answers"]["portfolio"].update(choice="C3", probabilities={"C1": .2, "C2": .4, "C3": .4})
        result = ChoiceAdapter(stub_response=data).rank(payload())
        self.assertEqual(result["ranked_ids"], ["C2", "C3", "C1"])
        self.assertEqual(result["choice"], "C3")

    def test_non_argmax_is_rejected_for_http_fixture_cache_and_real_replay(self):
        data = response()
        data["answers"]["portfolio"]["probabilities"] = {"C1": .1, "C2": .8, "C3": .1}
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
            adapter = ChoiceAdapter(live_config(folder), transport=lambda *a: (200, {}, data))
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "invalid_response:choice_not_argmax")
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
            adapter = ChoiceAdapter(live_config(folder), transport=lambda *a: (200, {}, response()))
            adapter.rank(payload())
            fixture = next(Path(folder).glob("*.fixture.json"))
            cached = json.loads(fixture.read_text())
            cached["response"] = data
            fixture.write_text(json.dumps(cached))
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.code, "cache_invalid")
            # Artificial legacy-format cache tests only the reader, and is
            # never exported as a real response or effectiveness evidence.
            cached["source_mode"] = "live"
            cached["test_fixture_note"] = "Artificial cache-reader test; no live service"
            fixture.with_name(fixture.name.replace(".fixture.json", ".live.json")).write_text(json.dumps(cached))
            replay = ChoiceAdapter(JevConfig(mode="real_replay", cache_dir=folder))
            with self.assertRaises(JevError) as caught:
                replay.rank(payload())
            self.assertEqual(caught.exception.code, "cache_invalid")

    def test_non_argmax_historical_choice_is_rejected_by_complete_checkpoint(self):
        from sim_jev import Config, MissionSpec, WeeklySimulation
        from sim_jev.decision import DecisionPolicySpec
        simulation = WeeklySimulation(
            replace(Config(), mission_rate_per_hour=0),
            [MissionSpec("A", 1, 2, 1, 10), MissionSpec("B", 4, 2, 1, 20)],
            policy=DecisionPolicySpec(method="jev-direct"))
        simulation.run(0)
        checkpoint = simulation.to_checkpoint()
        record = checkpoint["weekly_state"]["decision_records"][0]
        answer = record["jev"]["response"]["answers"]["portfolio"]
        lowest = min(answer["probabilities"], key=answer["probabilities"].get)
        self.assertLess(answer["probabilities"][lowest], max(answer["probabilities"].values()))
        answer["choice"] = record["jev"]["choice"] = lowest
        with self.assertRaisesRegex(ValueError, "choice_not_argmax"):
            WeeklySimulation.from_checkpoint(checkpoint)

    def test_corrupt_durable_attempt_usage_and_identity_are_rejected(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
            config = live_config(folder)
            adapter = ChoiceAdapter(config, transport=lambda *a: (200, {}, response()))
            adapter.rank(payload())
            original = deepcopy(adapter.ledger)
            path = Path(folder) / "http-budget.json"
            for mutation in (lambda r: r.update(attempt_id="duplicate-or-unbound"),
                             lambda r: r["known_usage"].update(input_tokens=-1),
                             lambda r: r["known_usage"].update(output_tokens=True),
                             lambda r: r.update(usage_complete=False)):
                corrupt = deepcopy(original)
                mutation(corrupt["attempts"][0])
                path.write_text(json.dumps(corrupt))
                with self.assertRaises(ValueError):
                    ChoiceAdapter(config)

    def test_all_attempt_usage_is_summed_once_and_retained_through_cache_and_resume(self):
        clock = Clock()
        success = response()
        success["usage"] = {"input_tokens": 11, "output_tokens": 2}
        replies = iter([(429, {}, {"usage": {"input_tokens": 7, "output_tokens": 1}}),
                        (200, {}, success)])
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
            config = live_config(folder)
            adapter = ChoiceAdapter(config, transport=lambda *a: next(replies), clock=clock, sleeper=clock.sleep)
            adapter.set_context(phase="pilot", run="balanced-0-jev-des", scenario="balanced", seed=0, method="jev-des")
            result = adapter.rank(payload())
            self.assertEqual(result["actual_usage"], {"input_tokens": 18, "output_tokens": 3})
            self.assertEqual(result["source_usage"], success["usage"])
            self.assertEqual(result["unknown_usage_attempts"], 0)
            self.assertTrue(result["usage_complete"])
            self.assertEqual([r["known_usage"] for r in result["attempts"]],
                             [{"input_tokens": 7, "output_tokens": 1}, success["usage"]])
            self.assertEqual(result["attempts"][0]["context"]["decision"], "legal-request")
            self.assertEqual(result["attempts"][0]["context"]["window_start"], 168)
            ids = [row["attempt_id"] for row in adapter.ledger["attempts"]]
            self.assertEqual(len(set(ids)), 2)
            clock.now += 500
            restored = ChoiceAdapter(replace(config, network_policy="cache_only", allow_live=False),
                                     transport=lambda *a: self.fail("cache-only must not call"), clock=clock)
            with patch.dict(os.environ, {}, clear=True):
                cached = restored.rank(payload())
            self.assertEqual(cached["actual_usage"], {"input_tokens": 0, "output_tokens": 0})
            self.assertEqual(cached["source_usage"], success["usage"])
            self.assertEqual(restored.usage_summary()["actual_usage"], {"input_tokens": 18, "output_tokens": 3})
            self.assertEqual([row["attempt_id"] for row in restored.ledger["attempts"]], ids)
            self.assertEqual(restored.ledger["started_at_epoch"], 1000.)
            changed = payload()
            changed["state"]["decision"]["observation"]["time"] = 169
            with self.assertRaises(JevError) as caught:
                restored.rank(changed)
            self.assertEqual(caught.exception.code, "cache_only_miss")
            self.assertEqual(restored.http_requests, 2)

    def test_unknown_retry_then_success_and_failed_known_usage_are_explicit(self):
        for missing in (None, {"usage": {"input_tokens": True, "output_tokens": 1}},
                        {"usage": {"input_tokens": -1, "output_tokens": 1}}):
            with self.subTest(missing=missing), TemporaryDirectory() as folder, \
                 patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
                clock = Clock()
                replies = iter([(429, {}, missing), (200, {}, response())])
                adapter = ChoiceAdapter(live_config(folder), transport=lambda *a: next(replies),
                                        clock=clock, sleeper=clock.sleep)
                result = adapter.rank(payload())
                self.assertEqual(result["actual_usage"], response()["usage"])
                self.assertEqual(result["unknown_usage_attempts"], 1)
                self.assertFalse(result["usage_complete"])
                self.assertTrue(result["cost_estimate_is_lower_bound"])
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
            adapter = ChoiceAdapter(live_config(folder), transport=lambda *a:
                                    (422, {}, {"usage": {"input_tokens": 7, "output_tokens": 1},
                                               "error": "local-fake-token"}))
            with self.assertRaises(JevError) as caught:
                adapter.rank(payload())
            self.assertEqual(caught.exception.audit["actual_usage"], {"input_tokens": 7, "output_tokens": 1})
            self.assertTrue(caught.exception.audit["usage_complete"])
            self.assertNotIn("local-fake-token", json.dumps(adapter.ledger))

    def test_timeout_and_crash_reservation_keep_unknown_metering_and_unique_id(self):
        for exception in (TimeoutError, KeyboardInterrupt):
            with self.subTest(exception=exception), TemporaryDirectory() as folder, \
                 patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
                def fail(*args):
                    raise exception("do-not-log")
                config = live_config(folder, max_retries=0, max_http_requests=1)
                adapter = ChoiceAdapter(config, transport=fail)
                with self.assertRaises((JevError, KeyboardInterrupt)):
                    adapter.rank(payload())
                restarted = ChoiceAdapter(config, transport=fail)
                summary = restarted.usage_summary()
                self.assertEqual(summary["unknown_usage_attempts"], 1)
                self.assertEqual(summary["uncertain_attempts"], 1)
                self.assertEqual(summary["actual_http_requests"], 1)
                self.assertEqual(restarted.ledger["attempts"][0]["attempt_id"], adapter.ledger["attempts"][0]["attempt_id"])
                with self.assertRaises(JevError) as caught:
                    restarted.rank(payload())
                self.assertEqual(caught.exception.code, "http_budget_exhausted")
                self.assertEqual(caught.exception.audit["actual_http_requests"], 0)

    def test_legacy_ledger_additive_migration_retains_unknown_and_cannot_enlarge_quota(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
            clock = Clock()
            config = live_config(folder)
            adapter = ChoiceAdapter(config, transport=lambda *a: (401, {}, None), clock=clock)
            with self.assertRaises(JevError):
                adapter.rank(payload())
            legacy = deepcopy(adapter.ledger)
            for key in ("accounting_version", "budget_id"):
                legacy.pop(key)
            for row in legacy["attempts"]:
                for key in ("attempt_id", "known_usage", "usage_complete", "context", "transport_kind"):
                    row.pop(key)
            (Path(folder) / "http-budget.json").write_text(json.dumps(legacy))
            clock.now += 1
            restored = ChoiceAdapter(config, transport=lambda *a: self.fail("no send"), clock=clock)
            self.assertEqual(restored.ledger["started_at_epoch"], 1000.)
            self.assertEqual(restored.usage_summary()["unknown_usage_attempts"], 1)
            again = ChoiceAdapter(config, transport=lambda *a: self.fail("no send"), clock=clock)
            self.assertEqual(again.ledger, restored.ledger)
            for altered in (replace(config, max_http_requests=11), replace(config, total_timeout_seconds=121)):
                with self.assertRaisesRegex(ValueError, "budget identity mismatch"):
                    ChoiceAdapter(altered)

    def test_multiple_runs_share_single_budget_and_context_without_duplicate_usage(self):
        clock = Clock()
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"TYPESAFE_API_KEY": "local-fake-token"}):
            adapter = ChoiceAdapter(live_config(folder, max_http_requests=2),
                                    transport=lambda *a: (200, {}, response()), clock=clock)
            for index in range(2):
                adapter.set_context(phase="pilot", run=f"run-{index}")
                data = payload()
                data["state"]["decision"]["observation"]["time"] += index
                adapter.rank(data)
            self.assertEqual([r["context"]["run"] for r in adapter.ledger["attempts"]], ["run-0", "run-1"])
            self.assertEqual(adapter.usage_summary()["actual_usage"], {"input_tokens": 240, "output_tokens": 24})
            data["state"]["decision"]["observation"]["time"] += 1
            with self.assertRaises(JevError) as caught:
                adapter.rank(data)
            self.assertEqual(caught.exception.code, "http_budget_exhausted")
            self.assertEqual(adapter.http_requests, 2)


if __name__ == "__main__":
    unittest.main()
