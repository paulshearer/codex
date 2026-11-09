import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.exceptions import InvalidSignature
from multidict import CIMultiDict
from permissive.auth import Authenticator, Signer, authorize, client_key_path, generate, initialize_local, registry_path, revoke
from permissive.client import SignedSession
from permissive.service import Service
from signed_client import provision
from test_service import FakeRuntime


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.signer = provision(self.home)
        self.verifier = Authenticator(self.home)

    def tearDown(self):
        self.verifier.close()
        self.directory.cleanup()

    def verify(self, method, target, body, headers):
        return self.verifier.verify(method, target, body, CIMultiDict(headers))

    def test_exact_body_encoded_query_and_control_headers(self):
        body = b'{"prompt":"hello"}'
        route = "/v1/tasks?value=a%2Fb&empty="
        headers = self.signer.headers("POST", route, body, {"Content-Type": "application/json", "Idempotency-Key": "example", "Last-Event-ID": "42"})
        self.assertEqual(self.verify("POST", route, body, headers), self.signer.key_id)

    def test_method_path_query_body_and_headers_cannot_be_changed(self):
        route, body = "/v1/tasks?limit=3", b"payload"
        headers = self.signer.headers("POST", route, body, {"Content-Type": "text/plain", "Idempotency-Key": "one", "Last-Event-ID": "1"})
        variations = [("GET", route, body, headers), ("POST", "/v1/service/shutdown?limit=3", body, headers),
                      ("POST", "/v1/tasks?limit=4", body, headers), ("POST", route, b"other", headers)]
        for field in ("content-type", "idempotency-key", "last-event-id"):
            variations.append(("POST", route, body, {**headers, field: "changed"}))
            variations.append(("POST", route, body, {key: value for key, value in headers.items() if key != field}))
        for args in variations:
            with self.subTest(method=args[0], target=args[1], headers=args[3]), self.assertRaises((ValueError, InvalidSignature)):
                self.verify(*args)

    def test_replay_is_rejected_even_after_verifier_restart(self):
        headers = self.signer.headers("GET", "/v1/health")
        self.verify("GET", "/v1/health", b"", headers)
        with self.assertRaises(ValueError):
            self.verify("GET", "/v1/health", b"", headers)
        self.verifier.close()
        self.verifier = Authenticator(self.home)
        with self.assertRaises(ValueError):
            self.verify("GET", "/v1/health", b"", headers)

    def test_unknown_wrong_revoked_keys_and_scope(self):
        generate(self.home / "unknown.pem", self.home / "unknown.pub")
        unknown = Signer(self.home / "unknown.pem")
        headers = unknown.headers("GET", "/v1/health")
        with self.assertRaises(ValueError): self.verify("GET", "/v1/health", b"", headers)
        altered = self.signer.headers("GET", "/v1/health")
        altered["signature"] = headers["signature"]
        with self.assertRaises(InvalidSignature): self.verify("GET", "/v1/health", b"", altered)
        with self.assertRaises(ValueError): self.verify("POST", "/v1/browser/command", b"", self.signer.headers("POST", "/v1/browser/command"))
        browser = provision(self.home, "browser")
        self.verify("GET", "/v1/browser/extension", b"", browser.headers("GET", "/v1/browser/extension"))
        with self.assertRaises(ValueError): self.verify("GET", "/v1/health", b"", browser.headers("GET", "/v1/health"))
        revoke(self.home, self.signer.key_id)
        with self.assertRaises(ValueError): self.verify("GET", "/v1/health", b"", self.signer.headers("GET", "/v1/health"))

    def test_expired_future_and_excessive_lifetime(self):
        now = int(time.time())
        for options in ({"created": now - 121}, {"created": now + 31}, {"lifetime": 121}, {"lifetime": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.verify("GET", "/v1/health", b"", self.signer.headers("GET", "/v1/health", **options))

    def test_missing_duplicate_and_uncovered_fields(self):
        original = self.signer.headers("GET", "/v1/health")
        for field in ("signature", "signature-input", "content-digest"):
            headers = CIMultiDict(original); headers.add(field, original[field])
            with self.assertRaises(ValueError): self.verifier.verify("GET", "/v1/health", b"", headers)
            headers = {key: value for key, value in original.items() if key != field}
            with self.assertRaises((KeyError, ValueError)): self.verify("GET", "/v1/health", b"", headers)
        with self.assertRaises(ValueError): self.verify("GET", "/v1/health", b"", {**original, "idempotency-key": "injected"})

    def test_corrupt_registry_fails_closed(self):
        for contents in ("[]", '{"version":1,"keys":[{}]}', '{"version":1,"keys":[{"scopes":[],"publicKey":42}]}'):
            registry_path(self.home).write_text(contents)
            with self.assertRaises((ValueError, KeyError)): Authenticator(self.home)

    def test_restart_provisioning_does_not_reauthorize_revoked_clients(self):
        from permissive.auth import initialize_local
        revoke(self.home, self.signer.key_id)
        initialize_local(self.home, ensure=True)
        with self.assertRaises(ValueError): self.verify("GET", "/v1/health", b"", self.signer.headers("GET", "/v1/health"))


@unittest.skipUnless(os.name == "nt", "Windows AppData virtualization")
class ClientLocationTests(unittest.TestCase):
    def test_migration_preserves_identity_across_appdata_views_and_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "instance"
            with patch.dict(os.environ, {"USERPROFILE": str(root / "user"), "LOCALAPPDATA": str(root / "redirected")}):
                key = client_key_path(home, "browser")
                virtual_home = root / "package-cache/instance"
                old_instance = hashlib.sha256(os.path.normcase(str(virtual_home)).encode()).hexdigest()[:16]
                legacy = root / "redirected/CodexPermissiveClients" / old_instance / "browser/private-key.dpapi"
                generate(legacy, legacy.with_name("public-key.pub"))
                original = Signer(legacy)
                authorize(home, legacy.with_name("public-key.pub").read_bytes(), "Browser", ("browser",))
                with patch("permissive.auth.Path.resolve", return_value=virtual_home):
                    self.assertEqual(client_key_path(home, "browser"), key)
                    initialize_local(home, ensure=True)
                self.assertEqual(key.read_bytes(), legacy.read_bytes())
                self.assertEqual(Signer(key).key_id, original.key_id)
                self.assertTrue(legacy.exists())
                with patch.dict(os.environ, {"LOCALAPPDATA": str(root / "real")}):
                    self.assertEqual(client_key_path(home, "browser"), key)
                    verifier = Authenticator(home)
                    try:
                        headers = Signer(client_key_path(home, "browser")).headers("GET", "/v1/browser/extension")
                        self.assertEqual(verifier.verify("GET", "/v1/browser/extension", b"", CIMultiDict(headers)), original.key_id)
                        revoke(home, original.key_id)
                        initialize_local(home, ensure=True)
                        with self.assertRaises(ValueError):
                            verifier.verify("GET", "/v1/browser/extension", b"", CIMultiDict(Signer(key).headers("GET", "/v1/browser/extension")))
                    finally:
                        verifier.close()


class AuthHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_routes_require_signatures_and_signed_sdk_works(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); provision(home)
            service = Service(home, FakeRuntime())
            async with TestServer(service.app()) as server, ClientSession() as unsigned, SignedSession(home=home, private_key=home / "test-clients/api.pem") as signed:
                for path in ("/v1/health", "/v1/workspaces", "/v1/tasks", "/v1/openapi.json", "/v1/browser/extension"):
                    for headers in ({}, {"Authorization": "Bearer obsolete-token"}, {"Signature": (home / "test-clients/api.pub").read_text().strip()}):
                        async with unsigned.get(server.make_url(path), headers=headers) as response:
                            self.assertEqual(response.status, 401)
                async with signed.get(server.make_url("/v1/health")) as response: self.assertEqual(response.status, 200)
                async with signed.post(server.make_url("/v1/workspaces"), json={"name": "Signed", "workingDirectory": str(home)}) as response:
                    self.assertEqual(response.status, 201, await response.text())
                async with signed.post(server.make_url("/v1/service/shutdown")) as response: self.assertEqual(response.status, 200)

    async def test_expiration_does_not_terminate_an_admitted_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); signer = provision(home)
            service = Service(home, FakeRuntime())
            async def slow(request):
                stream = web.StreamResponse(); await stream.prepare(request)
                await stream.write(b"started\n")
                await asyncio.sleep(2.1)
                await stream.write(b"finished\n"); await stream.write_eof()
                return stream
            app = service.app(); app.router.add_get("/slow", slow)
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/slow", headers=signer.headers("GET", "/slow", lifetime=1))
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.text(), "started\nfinished\n")
