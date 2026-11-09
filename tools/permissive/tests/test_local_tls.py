import contextlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import tempfile
import time
import unittest
from urllib.request import Request, urlopen

from custom_models.facade import Facade
from custom_models.registry import Provider
from permissive.local_tls import LocalTLS
from test_facade import MODEL, MockProvider, complete_response, sse


class LocalTLSTests(unittest.TestCase):
    @contextlib.contextmanager
    def facade(self, callback=None):
        callback = callback or (lambda _: (200, "application/json", [
            (0, json.dumps(complete_response()).encode())]))
        upstream = MockProvider(callback)
        with tempfile.TemporaryDirectory() as home:
            tls = LocalTLS(home)
            facade = Facade(Provider("deepseek", "DeepSeek", "responses", upstream.url), [MODEL], home)
            facade.start(ssl_context=tls.context)
            try:
                yield tls, facade, upstream
            finally:
                facade.stop()
                tls.close()
                upstream.stop()

    def test_verified_https_forwards_and_plaintext_is_rejected(self):
        with self.facade() as (tls, facade, upstream):
            self.assertTrue(facade.base_url.startswith("https://"))
            self.assertFalse((tls.certificate.parent / "private-key.pem").exists())
            context = ssl.create_default_context(cafile=str(tls.certificate))
            request = Request(facade.base_url + "/responses", json.dumps({
                "model": MODEL.upstream_model, "input": "hello", "stream": False,
            }).encode(), {"Authorization": "Bearer " + facade.token, "Content-Type": "application/json"})
            with urlopen(request, context=context, timeout=3) as response:
                self.assertEqual(json.load(response)["status"], "completed")
            self.assertEqual(len(upstream.records), 1)
            with self.assertRaises(OSError):
                urlopen(Request(facade.base_url + "/models", headers={"Authorization": "Bearer " + facade.token}), timeout=3)
            plaintext = http.client.HTTPConnection("127.0.0.1", facade.server.server_port, timeout=3)
            try:
                plaintext.request("GET", "/v1/models")
                with self.assertRaises((OSError, http.client.HTTPException)):
                    plaintext.getresponse()
            finally:
                plaintext.close()
            with urlopen(Request(facade.base_url + "/models", headers={"Authorization": "Bearer " + facade.token}),
                         context=context, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(len(upstream.records), 1)

    def test_tls_client_disconnect_cancels_upstream(self):
        callback = lambda _: (200, "text/event-stream", [
            (0, sse({"type": "response.created", "response": {"id": "resp1", "status": "in_progress"}})),
            (1, sse({"type": "response.completed", "response": complete_response()})),
        ])
        with self.facade(callback) as (tls, facade, upstream):
            context = ssl.create_default_context(cafile=str(tls.certificate))
            connection = context.wrap_socket(socket.create_connection(
                ("127.0.0.1", facade.server.server_port), timeout=3), server_hostname="127.0.0.1")
            body = json.dumps({"model": MODEL.upstream_model, "input": "hello", "stream": True})
            connection.sendall(("POST /v1/responses HTTP/1.0\r\nHost: localhost\r\n"
                + "Authorization: Bearer " + facade.token + "\r\nContent-Type: application/json\r\n"
                + "Content-Length: " + str(len(body)) + "\r\n\r\n" + body).encode())
            received = b""
            while b"response.created" not in received:
                received += connection.recv(4096)
            connection.shutdown(socket.SHUT_RDWR)
            connection.close()
            deadline = time.monotonic() + 2
            while not facade.records and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(facade.records)
            self.assertEqual(facade.records[-1]["failure"], "client_disconnected")

    def test_native_trust_is_scoped_and_certificate_is_removed(self):
        with tempfile.TemporaryDirectory() as home:
            tls = LocalTLS(home)
            certificate = tls.certificate
            environment = {"CODEX_CA_CERTIFICATE": "old.pem", "SSL_CERT_FILE": "upstream.pem"}
            trusted = tls.environment(environment)
            self.assertEqual(trusted["CODEX_CA_CERTIFICATE"], str(certificate))
            self.assertEqual(trusted["SSL_CERT_FILE"], "upstream.pem")
            self.assertEqual(environment["CODEX_CA_CERTIFICATE"], "old.pem")
            tls.close()
            self.assertFalse(certificate.exists())


if __name__ == "__main__":
    unittest.main()
