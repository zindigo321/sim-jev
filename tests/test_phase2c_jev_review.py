"""Independent local-TLS and persisted-budget review counterexamples."""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from sim_jev import jev
from sim_jev.jev import ChoiceAdapter, JevError
from test_phase2c_jev import Clock, live_config, payload, response


@contextmanager
def server(handler, *, context=None):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    instance.daemon_threads = True
    if context is not None:
        instance.socket = context.wrap_socket(instance.socket, server_side=True)
    worker = Thread(target=lambda: instance.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        worker.join(1)


class IndependentJevReviewTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "local TLS test certificate generation requires openssl")
    def test_real_local_tls_redirect_and_error_never_leak_credentials_to_http_target_or_audit(self):
        # The certificate/private key are disposable localhost test data. No
        # real credential, certificate store or external network is consulted.
        fake = "fixture-local-tls-token"
        origin_requests, target_requests = [], []

        class Target(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                target_requests.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
            do_POST = do_GET

        class Origin(BaseHTTPRequestHandler):
            location = ""
            status = 302
            def log_message(self, *args):
                pass
            def do_POST(self):
                origin_requests.append((self.command, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({"error": fake, "usage": {"input_tokens": 7, "output_tokens": 1}}).encode()
                self.send_response(self.status)
                self.send_header("Location", self.location + "?echo=" + fake)
                self.send_header("X-Server-Debug", fake)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with TemporaryDirectory(prefix="sim-jev-local-tls-") as temporary:
            folder = Path(temporary)
            certificate, private_key = folder / "localhost.crt", folder / "localhost.key"
            configuration = folder / "openssl-test.cnf"
            configuration.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
            generation = subprocess.run([
                shutil.which("openssl"), "req", "-config", str(configuration),
                "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(generation.returncode, 0, "local test certificate generation failed")
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certificate, private_key)
            client_context = ssl.create_default_context(cafile=str(certificate))
            with server(Target) as target, server(Origin, context=server_context) as origin:
                Origin.location = f"http://127.0.0.1:{target.server_port}/sink"
                for status in (301, 302, 303, 307, 308, 401):
                    with self.subTest(status=status):
                        Origin.status = status
                        store = folder / f"attempt-{status}"
                        with patch.object(jev, "ENDPOINT", f"https://127.0.0.1:{origin.server_port}/choice"), \
                             patch.object(ssl, "_create_default_https_context", return_value=client_context), \
                             patch.dict(os.environ, {"TYPESAFE_API_KEY": fake}):
                            adapter = ChoiceAdapter(live_config(str(store), max_retries=0))
                            with self.assertRaises(JevError) as caught:
                                adapter.rank(payload())
                        self.assertEqual(caught.exception.code,
                                         "authentication_error" if status == 401 else "redirect_rejected")
                        self.assertEqual(adapter.http_requests, 1)
                        self.assertEqual(adapter.usage_summary()["unknown_usage_attempts"], 1)
                        self.assertFalse(adapter.usage_summary()["usage_complete"])
                        self.assertNotIn(fake, json.dumps(caught.exception.audit))
                        self.assertNotIn(fake, (store / "http-budget.json").read_text(encoding="utf-8"))
                        self.assertEqual(list(store.glob("*.live.json")), [])
            self.assertEqual(origin_requests, [("POST", "Bearer " + fake)] * 6)
            self.assertEqual(target_requests, [])

    def test_corrupted_attempt_identity_or_metering_cannot_be_restored_and_recharged(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-token"}):
            config = live_config(temporary, max_retries=0)
            adapter = ChoiceAdapter(config, transport=lambda *args: (200, {}, response()))
            adapter.rank(payload())
            original = deepcopy(adapter.ledger)
            for field, value in (("attempt_id", "different"), ("known_usage", {"input_tokens": True, "output_tokens": 1}),
                                 ("known_usage", {"input_tokens": 1, "output_tokens": -1}),
                                 ("usage_complete", False), ("number", True)):
                with self.subTest(field=field, value=value):
                    ledger = deepcopy(original)
                    ledger["attempts"][0][field] = value
                    (Path(temporary) / "http-budget.json").write_text(json.dumps(ledger), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        ChoiceAdapter(config, transport=lambda *args: self.fail("corrupt ledger must never send"))

    def test_wall_clock_rollback_does_not_extend_process_deadline_but_completed_cache_is_readable(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-token"}):
            wall, monotonic = Clock(), Clock()
            calls = []
            def transport(*args):
                calls.append(True)
                return 200, {}, response()
            adapter = ChoiceAdapter(live_config(temporary), transport=transport,
                                    clock=wall, monotonic_clock=monotonic)
            adapter.rank(payload())
            wall.now -= 1000
            monotonic.now += 121
            changed = payload()
            changed["state"]["decision"]["observation"]["time"] += 1
            with self.assertRaises(JevError) as caught:
                adapter.rank(changed)
            self.assertEqual(caught.exception.code, "total_timeout")
            cached = adapter.rank(payload())
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(cached["actual_http_requests"], 0)
            self.assertEqual(cached["actual_usage"], {"input_tokens": 0, "output_tokens": 0})
            self.assertEqual(len(calls), 1)
            self.assertEqual(adapter.ledger["started_at_epoch"], 1000.)


if __name__ == "__main__":
    unittest.main()
