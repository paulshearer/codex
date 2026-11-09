import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from multidict import CIMultiDict
from yarl import URL

from permissive.auth import Authenticator, client_key_path, digest, revoke
from permissive.client import SignedSession
from permissive.mobile import MobileGateway, allowed, local_status, main, provision_backend, serve, sidecar_lock
from permissive.mobile_auth import BASE_FIELDS, OPTIONAL_FIELDS, MobileState, origin, public_key, signature_base, upstream_origin


ORIGIN = "https://test.tailunit.ts.net:9444"
AUTHORITY = "test.tailunit.ts.net:9444"


class Phone:
    """Exercise the JCA-compatible DER -> RFC9421 raw ECDSA conversion."""
    def __init__(self, installation):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.id = hashlib.sha256(self.public).hexdigest()
        self.installation = installation

    def headers(self, method, path, body=b"", fields=None, *, created=None, lifetime=60, nonce=None, authority=AUTHORITY):
        headers = {key.lower(): value for key, value in (fields or {}).items()}
        headers.update({"host": authority, "permissive-instance-id": self.installation, "content-digest": digest(body)})
        covered = [*BASE_FIELDS, *(field for field in OPTIONAL_FIELDS if field in headers)]
        created = int(time.time()) if created is None else created
        nonce = nonce or secrets.token_urlsafe(24)
        parameters = '(' + ' '.join('"' + field + '"' for field in covered) + f');created={created};expires={created+lifetime};nonce="{nonce}";keyid="{self.id}";alg="ecdsa-p256-sha256"'
        encoded = self.key.sign(signature_base(method, authority, path, headers, covered, parameters), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(encoded)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        headers["signature-input"] = "permissive-mobile=" + parameters
        headers["signature"] = "permissive-mobile=:" + base64.b64encode(raw).decode() + ":"
        return headers

    def enrollment(self, invitation):
        value = {"invitationId": invitation["invitationId"], "secret": invitation["secret"],
                 "publicKey": base64.b64encode(self.public).decode(), "deviceName": "Test phone"}
        body = json.dumps(value, separators=(",", ":")).encode()
        return value, body, self.headers("POST", "/mobile/v1/enroll", body, {"Content-Type": "application/json"})


class MobileAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.state = MobileState(self.home)
        self.state.configure(ORIGIN, "http://127.0.0.1:8789")
        self.phone = Phone(self.state.settings()["installationId"])
        self.invitation = self.state.invite()
        self.enroll()

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def enroll(self, phone=None, invitation=None):
        value, body, headers = (phone or self.phone).enrollment(invitation or self.invitation)
        return self.state.enroll(value, "POST", AUTHORITY, "/mobile/v1/enroll", body, CIMultiDict(headers))

    def verify(self, method, target, body, headers):
        return self.state.verify(method, headers["host"], target, body, CIMultiDict(headers))

    def test_invite_contains_only_bootstrap_and_persists_no_secret(self):
        self.assertEqual(set(self.invitation), {"version", "origin", "installationId", "invitationId", "secret", "expiresAt"})
        self.assertEqual(self.invitation["version"], 1)
        self.assertEqual(len(base64.urlsafe_b64decode(self.invitation["secret"] + "=")), 32)
        self.assertGreaterEqual(self.invitation["expiresAt"], int(time.time()) + 298)
        row = self.state.db.execute("SELECT * FROM invitations").fetchone()
        self.assertEqual(row["secret_hash"], hashlib.sha256(self.invitation["secret"].encode()).digest())
        for file in self.state.root.iterdir():
            if file.is_file():
                self.assertNotIn(self.invitation["secret"].encode(), file.read_bytes())

    def test_same_key_retry_and_other_key_rejected(self):
        identity, created = self.enroll()
        self.assertFalse(created)
        self.assertEqual(identity, self.state.identity(self.phone.id))
        with self.assertRaises(ValueError):
            self.enroll(Phone(self.phone.installation))

    def test_concurrent_invitation_can_enroll_only_one_public_key(self):
        invitation = self.state.invite()
        phones = [Phone(self.phone.installation), Phone(self.phone.installation)]
        gate = threading.Barrier(2)
        def enroll(phone):
            state = MobileState(self.home)
            try:
                value, body, headers = phone.enrollment(invitation)
                gate.wait(timeout=5)
                try:
                    state.enroll(value, "POST", AUTHORITY, "/mobile/v1/enroll", body, CIMultiDict(headers))
                    return True
                except ValueError:
                    return False
            finally:
                state.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enroll, phones))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(sum(self.state.active(phone.id) for phone in phones), 1)

    def test_replay_persists_across_restart(self):
        headers = self.phone.headers("GET", "/mobile/v1/identity")
        self.assertEqual(self.verify("GET", "/mobile/v1/identity", b"", headers), self.phone.id)
        self.state.close()
        self.state = MobileState(self.home)
        with self.assertRaises(ValueError):
            self.verify("GET", "/mobile/v1/identity", b"", headers)
        self.assertEqual(self.state.settings()["installationId"], self.phone.installation)

    def test_revoked_device_and_consumed_invite_never_restore(self):
        self.state.revoke(self.phone.id)
        with self.assertRaises(ValueError):
            self.verify("GET", "/v1/workspaces", b"", self.phone.headers("GET", "/v1/workspaces"))
        with self.assertRaises(ValueError):
            self.enroll()
        self.assertTrue(self.state.devices()[0]["revoked"])
        # A new, explicitly issued local invitation is the only re-enrollment path.
        self.enroll(invitation=self.state.invite())
        self.assertTrue(self.state.active(self.phone.id))

    def test_expired_wrong_and_replaced_invites_fail(self):
        for kind in ("expired", "secret", "replacement"):
            invitation = self.state.invite()
            if kind == "expired":
                with self.state.db:
                    self.state.db.execute("UPDATE invitations SET expires=? WHERE id=?", (int(time.time()) - 1, invitation["invitationId"]))
            elif kind == "secret":
                invitation["secret"] = secrets.token_urlsafe(32)
            else:
                self.state.invite()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.enroll(Phone(self.phone.installation), invitation)

    def test_bound_method_target_exact_body_and_controls(self):
        route, body = "/v1/tasks?value=a%2Fb&empty=", b'{"prompt":"A"}'
        fields = {"Content-Type": "application/json", "Idempotency-Key": "stable", "Last-Event-ID": "14"}
        headers = self.phone.headers("POST", route, body, fields)
        mutations = [("GET", route, body, headers), ("POST", "/v1/tasks?value=a/b&empty=", body, headers),
                     ("POST", "/v1/chats?value=a%2Fb&empty=", body, headers), ("POST", route, body + b" ", headers)]
        for field in ("content-type", "idempotency-key", "last-event-id", "permissive-instance-id", "host"):
            mutations.append(("POST", route, body, {**headers, field: "altered"}))
        for args in mutations:
            with self.subTest(method=args[0], target=args[1], fields=args[3]), self.assertRaises((ValueError, InvalidSignature)):
                self.verify(*args)
        self.assertEqual(self.verify("POST", route, body, headers), self.phone.id)

    def test_missing_duplicate_and_algorithm_downgrade_rejected(self):
        headers = self.phone.headers("GET", "/v1/workspaces")
        for field in ("signature", "signature-input", "content-digest", "permissive-instance-id"):
            duplicate = CIMultiDict(headers); duplicate.add(field, headers[field])
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.state.verify("GET", AUTHORITY, "/v1/workspaces", b"", duplicate)
            with self.assertRaises((ValueError, KeyError)):
                self.verify("GET", "/v1/workspaces", b"", {key: value for key, value in headers.items() if key != field})
        with self.assertRaises(ValueError):
            self.verify("GET", "/v1/workspaces", b"", {**headers, "signature-input": headers["signature-input"].replace("ecdsa-p256-sha256", "ed25519")})

    def test_signature_lifetime_and_raw64_encoding(self):
        for options in ({"created": int(time.time()) - 121}, {"created": int(time.time()) + 31}, {"lifetime": 121}, {"lifetime": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.verify("GET", "/v1/workspaces", b"", self.phone.headers("GET", "/v1/workspaces", **options))
        for payload in (b"invalid", b"\0" * 64, b"\xff" * 64):
            headers = self.phone.headers("GET", "/v1/workspaces")
            headers["signature"] = "permissive-mobile=:" + base64.b64encode(payload).decode() + ":"
            with self.assertRaises((ValueError, InvalidSignature)):
                self.verify("GET", "/v1/workspaces", b"", headers)

    def test_public_keys_are_p256_canonical_spki_only(self):
        for value in (None, "not base64", base64.b64encode(ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()):
            with self.assertRaises(ValueError): public_key(value)
        self.assertEqual(public_key(base64.b64encode(self.phone.public).decode())[2], self.phone.id)

    def test_origin_and_upstream_limits(self):
        for invalid in ("http://test.tailunit.ts.net:9444", "https://example.com:9444", "https://test.tailunit.ts.net",
                        ORIGIN + "/path", "https://user:pass@test.tailunit.ts.net:9444", ORIGIN + "?query=1"):
            with self.subTest(value=invalid), self.assertRaises(ValueError): origin(invalid)
        for invalid in ("http://localhost:8789", "https://127.0.0.1:8789", "http://192.168.0.1:8789", "http://127.0.0.1:8789/path"):
            with self.assertRaises(ValueError): upstream_origin(invalid)

    def test_disabled_installation_rejects_auth_and_invites(self):
        self.state.disable()
        with self.assertRaises(ValueError): self.state.invite()
        with self.assertRaises(ValueError): self.verify("GET", "/v1/workspaces", b"", self.phone.headers("GET", "/v1/workspaces"))

    def test_existing_service_is_explicit_persistent_and_fixed_loopback_only(self):
        self.assertIs(self.state.settings()["existingService"], False)
        self.state.configure(ORIGIN, "http://127.0.0.1:8789", existing_service=True)
        self.state.close()
        self.state = MobileState(self.home)
        self.assertIs(self.state.settings()["existingService"], True)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.state.configure(ORIGIN, "http://127.0.0.1:8789", existing_service=value)
        with self.assertRaises(ValueError):
            self.state.configure(ORIGIN, "http://127.0.0.1:8791", existing_service=True)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.state.db:
                self.state.db.execute("UPDATE settings SET existing_service=2")
        self.state.configure(ORIGIN, "http://127.0.0.1:8789")
        self.assertIs(self.state.settings()["existingService"], False)

    def test_prior_companion_settings_migrate_with_legacy_access_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "companion"
            root.mkdir()
            with sqlite3.connect(root / "state.sqlite") as database:
                database.execute("CREATE TABLE settings(id INTEGER PRIMARY KEY CHECK(id=1), installation_id TEXT NOT NULL, origin TEXT, upstream TEXT, port INTEGER NOT NULL DEFAULT 8790, enabled INTEGER NOT NULL DEFAULT 0)")
                database.execute("INSERT INTO settings VALUES(1,?,?,?,?,?)", (self.phone.installation, ORIGIN, "http://127.0.0.1:8789", 8790, 1))
            database.close()
            state = MobileState(temporary)
            try:
                self.assertEqual(state.settings()["installationId"], self.phone.installation)
                self.assertTrue(state.settings()["enabled"])
                self.assertIs(state.settings()["existingService"], False)
            finally:
                state.close()

    def test_lock_and_readiness_marker(self):
        self.assertFalse(local_status(self.state)["running"])
        (self.state.root / "ready.json").write_text(json.dumps({key: self.state.settings()[key] for key in ("installationId", "origin", "upstream", "port", "existingService")}))
        self.assertFalse(local_status(self.state)["running"])
        with sidecar_lock(self.state.root):
            self.assertTrue(local_status(self.state)["running"])
            with self.assertRaises(ValueError):
                with sidecar_lock(self.state.root): pass
        self.assertFalse(local_status(self.state)["running"])

    def test_changing_existing_service_invalidates_readiness(self):
        (self.state.root / "ready.json").write_text(json.dumps(self.state.settings()))
        with sidecar_lock(self.state.root):
            self.assertTrue(local_status(self.state)["running"])
            self.state.configure(ORIGIN, "http://127.0.0.1:8789", existing_service=True)
            self.assertFalse(local_status(self.state)["running"])


class ControllerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_requires_controller_unless_legacy_explicitly_enabled(self):
        class Response:
            def __init__(self, payload, status): self.payload, self.status = payload, status
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return self.payload
        class Session:
            def __init__(self, payload, status): self.payload, self.status, self.targets = payload, status, []
            def get(self, target, **options):
                self.targets.append(target)
                return Response(self.payload, self.status)
        cases = [
            (False, "http://127.0.0.1:8789", {"ok": True}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": True}, 200, True),
            (True, "http://127.0.0.1:8791", {"ok": True}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": True, "executionRole": "guest"}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": True, "executionRole": "controller"}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": True, "executionRole": None}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": True, "executionRole": "other"}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": False}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": "true"}, 200, False),
            (True, "http://127.0.0.1:8789", {"ok": True}, 401, False),
            (True, "http://127.0.0.1:8789", [], 200, False),
            (False, "http://127.0.0.1:8791", {"ok": True, "executionRole": "controller"}, 200, True),
            (False, "http://127.0.0.1:8789", {"ok": False, "executionRole": "controller"}, 200, False),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            state = MobileState(temporary)
            state.configure(ORIGIN, "http://127.0.0.1:8789")
            state.close()
            gateway = MobileGateway(temporary)
            try:
                for opt_in, upstream, payload, status, accepted in cases:
                    with self.subTest(opt_in=opt_in, upstream=upstream, payload=payload, status=status):
                        gateway.configuration["existingService"] = opt_in
                        gateway.upstream = upstream
                        gateway.session = Session(payload, status)
                        if accepted:
                            self.assertEqual(await gateway.controller(), payload)
                        else:
                            with self.assertRaises(ValueError): await gateway.controller()
                        self.assertEqual(gateway.session.targets, [upstream + "/v1/health"])
            finally:
                gateway.state.close()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {"USERPROFILE": str(self.home / "user"), "LOCALAPPDATA": str(self.home / "appdata")})
        self.env.start()
        provision_backend(self.home)
        self.auth = Authenticator(self.home)
        self.role = "controller"
        self.health = None
        self.received = []
        self.responses = {}
        self.stream_gate = asyncio.Event()
        self.first_event = asyncio.Event()

        @web.middleware
        async def authenticate(request, handler):
            try:
                self.auth.verify(request.method, request.raw_path, await request.read(), request.headers)
            except (ValueError, InvalidSignature):
                return web.Response(status=401)
            return await handler(request)

        async def backend(request):
            self.received.append((request.method, request.raw_path, await request.read(), dict(request.headers)))
            if request.path == "/v1/health":
                return web.json_response(self.health if self.health is not None else {"ok": True, "executionRole": self.role})
            if request.path in self.responses:
                status, value = self.responses[request.path]
                return web.json_response(value, status=status)
            if request.path == "/v1/workspaces/workspace":
                return web.json_response({"id": "workspace", "workingDirectory": "C:/existing"})
            if request.path.startswith("/v1/workspaces/"):
                return web.Response(status=404)
            if request.path.endswith("/events") and request.query.get("format") != "json":
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b"id: 1\ndata: first\n\n")
                self.first_event.set()
                await self.stream_gate.wait()
                with contextlib.suppress(ConnectionError):
                    await response.write(b"id: 2\ndata: second\n\n")
                return response
            return web.json_response({"id": "task", "accepted": True}, status=202 if request.method == "POST" else 200)

        backend_app = web.Application(middlewares=[authenticate])
        backend_app.router.add_route("*", "/{path:.*}", backend)
        self.backend = TestServer(backend_app)
        await self.backend.start_server()
        self.state = MobileState(self.home)
        self.state.configure(ORIGIN, str(self.backend.make_url("")).rstrip("/"))
        self.phone = Phone(self.state.settings()["installationId"])
        self.invitation = self.state.invite()
        self.gateway = MobileGateway(self.home, stream_check_seconds=.05)
        self.front = TestServer(self.gateway.app())
        await self.front.start_server()
        self.client = ClientSession()
        response = await self.request("POST", "/mobile/v1/enroll", enrollment=True)
        self.assertEqual(response.status, 201, await response.text())
        await response.release()

    async def asyncTearDown(self):
        self.stream_gate.set()
        await self.client.close()
        await self.front.close()
        await self.backend.close()
        self.state.close()
        self.auth.close()
        self.env.stop()
        self.temporary.cleanup()

    async def request(self, method, path, body=None, *, headers=None, enrollment=False, **sign_options):
        if enrollment:
            _, data, signed = self.phone.enrollment(self.invitation)
        else:
            data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
            fields = {"Content-Type": "application/json"} if body is not None else {}
            fields.update(headers or {})
            # aiohttp automatically adds this to empty POST requests too.
            if method == "POST" and body is None: fields.setdefault("Content-Type", "application/octet-stream")
            signed = self.phone.headers(method, path, data, fields, **sign_options)
        target = URL(str(self.front.make_url("")).rstrip("/") + path, encoded=True)
        return await self.client.request(method, target, data=data or None, headers=signed)

    async def test_enrollment_identity_and_same_key_retry(self):
        response = await self.request("GET", "/mobile/v1/identity")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["deviceId"], self.phone.id)
        response = await self.request("POST", "/mobile/v1/enroll", enrollment=True)
        self.assertEqual(response.status, 200)

    async def test_no_owner_header_needed_and_unsigned_rejected(self):
        response = await self.request("GET", "/v1/workspaces")
        self.assertEqual(response.status, 200)
        async with self.client.get(self.front.make_url("/v1/workspaces"), headers={"Host": AUTHORITY, "Tailscale-User-Login": "pshearer@hotmail.com"}) as response:
            self.assertEqual(response.status, 401)

    async def test_signed_chat_rename_forwards_exact_body_and_owner_signature(self):
        body = {'title': 'Renamed on Android 📝'}
        self.responses['/v1/chats/chat'] = (200, {'id': 'chat', **body})
        response = await self.request('PATCH', '/v1/chats/chat', body)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.json(), {'id': 'chat', **body})
        received = self.received[-1]
        self.assertEqual(received[:2], ('PATCH', '/v1/chats/chat'))
        self.assertEqual(json.loads(received[2]), body)
        self.assertFalse(allowed('PATCH', '/v1/chats/chat/events'))
        self.assertFalse(allowed('PATCH', '/v1/workspaces/workspace'))

    async def test_management_routes_and_directory_overrides_forbidden(self):
        blocked = [("POST", "/v1/workspaces"), ("PATCH", "/v1/workspaces/workspace"), ("GET", "/v1/environment"),
                   ("POST", "/v1/service/shutdown"), ("POST", "/v1/browser/command"), ("GET", "/mobile/v1/devices"),
                   ("POST", "/v1/browsers/connect"), ("POST", "/v1/browsers/setup"),
                   ("POST", "/v1/browsers"), ("GET", "/v1/browser/extension"), ("GET", "/v1/tasks/../auth/keys")]
        for method, path in blocked:
            response = await self.request(method, path)
            self.assertEqual(response.status, 404, (method, path, await response.text()))
        for body in ({"prompt": "private"}, {"prompt": "private", "workspaceId": "missing"},
                     {"prompt": "private", "workspaceId": "workspace", "workingDirectory": "C:/other"}):
            response = await self.request("POST", "/v1/tasks", body)
            self.assertEqual(response.status, 400)
        self.assertFalse(any(row[0] == "POST" for row in self.received))

    async def test_sealed_credentials_forward_unchanged_but_browser_redemption_is_blocked(self):
        from permissive.temporary_credentials import TemporaryCredentials
        from test_temporary_credentials import seal
        workspace = '11111111-1111-4111-8111-111111111111'
        envelope = seal(TemporaryCredentials().prepare(workspace), workspace)
        operations = [('POST', '/v1/credentials/prepare', {'workspaceId': workspace}),
                      ('POST', '/v1/credentials', envelope),
                      ('GET', '/v1/credentials?workspaceId=' + workspace, None),
                      ('DELETE', '/v1/credentials/' + envelope['uploadId'] + '?workspaceId=' + workspace, None),
                      ('DELETE', '/v1/credentials?workspaceId=' + workspace, None)]
        for method, target, body in operations:
            response = await self.request(method, target, body)
            self.assertIn(response.status, (200, 202), await response.text())
            method_seen, path_seen, raw, _ = self.received[-1]
            self.assertEqual((method_seen, path_seen), (method, target))
            self.assertEqual(json.loads(raw) if raw else None, body)
            self.assertNotIn(b'fixture-password', raw)
        before = len(self.received)
        response = await self.request('POST', '/v1/browser/credentials', {'operation': 'fill'})
        self.assertEqual(response.status, 404)
        self.assertEqual(len(self.received), before)

    async def test_browser_catalog_preserves_remote_profiles_and_connections(self):
        catalog = {"data": [{"id": "msedge", "name": "Microsoft Edge", "installed": True,
                    "profiles": ["managed"], "installedProfiles": [{"id": "local-" + "a" * 24,
                        "directory": "Profile 1", "name": "Work", "account": "work@example.com",
                        "extensionInstalled": True, "connected": True}]}],
                   "connections": [{"connectionId": "connection", "browser": "msedge",
                       "profileId": "extension-profile", "profileName": "Work", "localProfileId": "local-" + "a" * 24}]}
        self.responses["/v1/browsers"] = (200, catalog)
        response = await self.request("GET", "/v1/browsers")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.json(), catalog)
        received = self.received[-1]
        self.assertEqual(received[:3], ("GET", "/v1/browsers", b""))
        self.assertIn('alg="ed25519"', CIMultiDict(received[3])["Signature-Input"])

    async def test_secure_delete_forwards_exact_target_body_and_service_errors(self):
        for resource, ident in (("chats", "chat-id"), ("workspaces", "workspace")):
            path = f"/v1/{resource}/{ident}/secure-delete"
            # The decoded ID confirms the target; transport retains the signed encoding.
            target = path.replace("chat-id", "chat%2Did")
            body = {"confirm": ident, "passes": 7}
            for status in (200, 404, 409, 503):
                payload = {"deleted": True, "passes": 7} if status == 200 else {"error": "service rejection"}
                self.responses[path] = (status, payload)
                with self.subTest(resource=resource, status=status):
                    response = await self.request("POST", target, body)
                    self.assertEqual(response.status, status, await response.text())
                    self.assertEqual(await response.json(), payload)
                    received = self.received[-1]
                    self.assertEqual(received[:3], ("POST", target, json.dumps(body, separators=(",", ":")).encode()))
                    headers = CIMultiDict(received[3])
                    self.assertIn('alg="ed25519"', headers["Signature-Input"])
                    self.assertNotIn("Permissive-Instance-Id", headers)

    async def test_history_actions_forward_the_point_and_confirmation_unchanged(self):
        for action in ('fork', 'delete-from', 'delete-message', 'regenerate', 'edit-message'):
            path = '/v1/chats/selected/' + action
            body = {'taskId': 'task', 'messageId': '123', 'submissionId': str(uuid.uuid4())}
            if action in {'delete-from', 'delete-message', 'regenerate'}:
                body['confirm'] = 'selected'
            elif action == 'edit-message':
                body['text'] = 'Revised reply 📝'
            self.responses[path] = (200, {'id': 'selected'})
            response = await self.request('POST', path, body)
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(self.received[-1][:3], ('POST', path, json.dumps(body, separators=(',', ':')).encode()))

    async def test_secure_delete_requires_exact_confirmation_and_integer_seven_passes(self):
        for resource in ("chats", "workspaces"):
            path = f"/v1/{resource}/selected/secure-delete"
            invalid = [None, {}, {"confirm": "selected"}, {"passes": 7},
                       {"confirm": "other", "passes": 7}, {"confirm": "DELETE", "passes": 7},
                       {"confirm": ["selected"], "passes": 7},
                       *({"confirm": "selected", "passes": passes} for passes in (None, 6, 8, "7", 7.0, True)),
                       {"confirm": "selected", "passes": 7, "extra": True}]
            for body in invalid:
                with self.subTest(resource=resource, body=body):
                    response = await self.request("POST", path, body)
                    self.assertEqual(response.status, 400, await response.text())
                    self.assertEqual((await response.json())["error"]["code"], "invalid_request")
            response = await self.request("POST", path, {"confirm": "selected", "passes": 7},
                                          headers={"Content-Type": "text/plain"})
            self.assertEqual(response.status, 400)
        self.assertFalse(any(row[0] == "POST" for row in self.received))

    async def test_new_operations_require_paired_signatures(self):
        for method, path, body in (("GET", "/v1/browsers", None),
                                  ("POST", "/v1/chats/chat/secure-delete", {"confirm": "chat", "passes": 7}),
                                  ("POST", "/v1/workspaces/workspace/secure-delete", {"confirm": "workspace", "passes": 7})):
            async with self.client.request(method, self.front.make_url(path), json=body,
                                           headers={"Host": AUTHORITY}) as response:
                self.assertEqual(response.status, 401, await response.text())
        self.assertFalse(any(row[1] != "/v1/health" for row in self.received))

    async def test_secure_delete_route_bounds(self):
        for resource in ("chats", "workspaces"):
            for ident in ("selected", "a" * 128):
                self.assertTrue(allowed("POST", f"/v1/{resource}/{ident}/secure-delete"))
            for ident in (".", "..", "a" * 129, "a/b", "a\\b", ""):
                self.assertFalse(allowed("POST", f"/v1/{resource}/{ident}/secure-delete"))
            for method, suffix in (("GET", ""), ("DELETE", ""), ("POST", "/extra")):
                path = f"/v1/{resource}/selected/secure-delete{suffix}"
                response = await self.request(method, path)
                self.assertEqual(response.status, 404, await response.text())
            for ident in ("%2E%2E", "a%2Fb", "a%5Cb", "a%00b"):
                path = f"/v1/{resource}/{ident}/secure-delete"
                response = await self.request("POST", path, {"confirm": ident, "passes": 7})
                self.assertEqual(response.status, 404, await response.text())
        self.assertFalse(any(row[0] == "POST" for row in self.received))

    async def test_exact_body_control_fields_and_encoded_query_resigned(self):
        body = {"prompt": "private ☃", "workspaceId": "workspace"}
        path = "/v1/tasks?value=a%2Fb&empty="
        response = await self.request("POST", path, body, headers={"Idempotency-Key": "stable-submission", "Last-Event-ID": "17"})
        self.assertEqual(response.status, 202, await response.text())
        received = self.received[-1]
        upstream_headers = CIMultiDict(received[3])
        self.assertEqual(received[1], path)
        self.assertEqual(received[2], json.dumps(body, separators=(",", ":")).encode())
        self.assertEqual(upstream_headers["Idempotency-Key"], "stable-submission")
        self.assertEqual(upstream_headers["Last-Event-ID"], "17")
        self.assertIn('alg="ed25519"', upstream_headers["Signature-Input"])
        self.assertNotIn("Permissive-Instance-Id", upstream_headers)

    async def test_artifact_unicode_basename_is_forwarded_with_exact_encoding(self):
        path = "/v1/tasks/task/artifacts/Report%20%E2%98%83.pdf"
        response = await self.request("GET", path)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.received[-1][1], path)
        for invalid in ("/v1/tasks/task/artifacts/../secret", "/v1/tasks/task/artifacts/a/b.pdf",
                        "/v1/tasks/task/artifacts/a\\b.pdf", "/v1/tasks/task/artifacts/.."):
            self.assertFalse(allowed("GET", invalid))

    async def test_origin_funnel_and_wrong_host_rejected(self):
        for headers in ({"Origin": "https://malicious.example"}, {"Origin": ""}, {"Tailscale-Funnel-Request": "?1"}):
            response = await self.request("GET", "/v1/workspaces", headers=headers)
            self.assertEqual(response.status, 403)
        response = await self.request("GET", "/v1/workspaces", authority="other.tailunit.ts.net:9444")
        self.assertEqual(response.status, 401)

    async def test_role_change_prevents_forwarding(self):
        self.role = "guest"
        response = await self.request("GET", "/v1/workspaces")
        self.assertEqual(response.status, 503)
        self.assertFalse(any(item[1] == "/v1/workspaces" for item in self.received))

    async def test_startup_rejects_guest_controller_and_revoked_backend_key(self):
        self.role = "guest"
        gateway = MobileGateway(self.home)
        try:
            with self.assertRaises(ValueError): await gateway.start(None)
            self.assertIsNone(gateway.session)
        finally:
            gateway.state.close()
        self.role = "controller"
        from permissive.auth import Signer
        revoke(self.home, Signer(client_key_path(self.home, "companion")).key_id)
        gateway = MobileGateway(self.home)
        try:
            with self.assertRaises(ValueError): await gateway.start(None)
            self.assertIsNone(gateway.session)
        finally:
            gateway.state.close()

    async def test_legacy_opt_in_identity_and_signed_workspace_forwarding(self):
        # Route only this test's signed loopback transport to its isolated server;
        # never connect to or bind the user's real service on port 8789.
        await self.front.close()
        self.health = {"ok": True, "activeTasks": 0}
        gateway = MobileGateway(self.home)
        try:
            with self.assertRaises(ValueError): await gateway.start(None)
            self.assertIsNone(gateway.session)
        finally:
            gateway.state.close()
        self.state.configure(ORIGIN, "http://127.0.0.1:8789", existing_service=True)
        backend_origin = str(self.backend.make_url("")).rstrip("/")
        targets = []
        class IsolatedSignedSession(SignedSession):
            def request(self, method, target, **options):
                target = URL(target)
                if str(target.origin()) != "http://127.0.0.1:8789":
                    raise AssertionError("Unexpected legacy upstream")
                targets.append(str(target))
                return super().request(method, URL(backend_origin + target.raw_path_qs, encoded=True), **options)
        self.gateway = MobileGateway(self.home)
        self.front = TestServer(self.gateway.app())
        with patch("permissive.mobile.SignedSession", IsolatedSignedSession):
            await self.front.start_server()
        response = await self.request("GET", "/mobile/v1/identity")
        self.assertEqual(response.status, 200, await response.text())
        identity = await response.json()
        self.assertEqual(identity["installationId"], self.phone.installation)
        self.assertEqual(identity["deviceId"], self.phone.id)
        self.assertIn("existing-workspaces", identity["capabilities"])
        response = await self.request("GET", "/v1/workspaces/workspace")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["id"], "workspace")
        self.assertIn("http://127.0.0.1:8789/v1/health", targets)
        self.assertIn("http://127.0.0.1:8789/v1/workspaces/workspace", targets)
        # The fake service's real Ed25519 Authenticator checked every request.
        self.assertTrue(all("signature" in {key.lower() for key in item[3]} for item in self.received))
        self.health = {"ok": True, "executionRole": "guest"}
        response = await self.request("GET", "/v1/workspaces/workspace")
        self.assertEqual(response.status, 503)

    async def test_local_serve_marks_readiness_and_exits_when_disabled(self):
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.state.configure(ORIGIN, str(self.backend.make_url("")).rstrip("/"), port)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            task = asyncio.create_task(serve(self.home))
            try:
                for _ in range(100):
                    if local_status(self.state)["running"]: break
                    if task.done(): await task
                    await asyncio.sleep(.02)
                self.assertTrue(local_status(self.state)["running"])
                self.state.disable()
                await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await task
        self.assertTrue(json.loads(output.getvalue())["listening"])
        self.assertFalse(local_status(self.state)["running"])
        self.assertFalse((self.state.root / "ready.json").exists())

    async def test_stream_retry_forwards_signed_empty_request_and_service_conflict(self):
        path = "/v1/tasks/task/retry"
        response = await self.request("POST", path)
        self.assertEqual(response.status, 202, await response.text())
        forwarded = next(item for item in self.received if item[0] == "POST" and item[1] == path)
        self.assertEqual(forwarded[2], b"")
        self.assertIn("signature", {name.lower() for name in forwarded[3]})
        self.responses[path] = (409, {"message": "This task is no longer recoverable"})
        response = await self.request("POST", path)
        self.assertEqual(response.status, 409)
        await response.read()
        self.assertFalse(any(method == "POST" and route == "/v1/tasks" for method, route, _, _ in self.received))

    async def test_revocation_closes_idle_stream_and_prevents_reconnect(self):
        response = await self.request("GET", "/v1/chats/chat/events", headers={"Last-Event-ID": "0"})
        self.assertEqual(response.status, 200)
        self.assertIn(b"first", await asyncio.wait_for(response.content.readuntil(b"\n\n"), 1))
        self.state.revoke(self.phone.id)
        self.assertEqual(await asyncio.wait_for(response.content.read(), 1), b"")
        response = await self.request("GET", "/v1/chats/chat/events", headers={"Last-Event-ID": "1"})
        self.assertEqual(response.status, 401)

    async def test_admitted_stream_survives_signature_expiry(self):
        response = await self.request("GET", "/v1/chats/chat/events", lifetime=1)
        self.assertEqual(response.status, 200)
        first = await response.content.readuntil(b"\n\n")
        await asyncio.sleep(2)
        self.stream_gate.set()
        rest = await asyncio.wait_for(response.content.read(), 1)
        self.assertIn(b"first", first)
        self.assertIn(b"second", rest)

    async def test_disable_blocks_requests_and_closes_stream(self):
        response = await self.request("GET", "/v1/chats/chat/events")
        await response.content.readuntil(b"\n\n")
        self.state.disable()
        self.assertEqual(await asyncio.wait_for(response.content.read(), 1), b"")
        response = await self.request("GET", "/v1/workspaces")
        self.assertEqual(response.status, 503)

    async def test_corrupt_registry_and_json_errors_are_content_free(self):
        response = await self.request("POST", "/v1/tasks", {"prompt": "SENSITIVE", "workingDirectory": "SENSITIVE"})
        self.assertEqual(response.status, 400)
        self.assertNotIn("SENSITIVE", await response.text())
        self.state.db.execute("DROP TABLE devices"); self.state.db.commit()
        response = await self.request("GET", "/v1/workspaces")
        self.assertEqual(response.status, 503)
        self.assertNotIn("sqlite", await response.text())


class LocalCommandTests(unittest.TestCase):
    def test_cli_configure_status_invite_and_disable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"USERPROFILE": str(Path(directory) / "user"), "LOCALAPPDATA": str(Path(directory) / "appdata")}):
            def command(*arguments):
                output = io.StringIO()
                with contextlib.redirect_stdout(output): code = main(["--home", directory, *arguments])
                return code, json.loads(output.getvalue())
            code, configured = command("configure", "--origin", ORIGIN)
            self.assertEqual(code, 0)
            self.assertTrue(configured["enabled"])
            self.assertIs(configured["existingService"], False)
            self.assertFalse(configured["running"])
            self.assertTrue(client_key_path(directory, "companion").exists())
            _, invitation = command("invite")
            self.assertEqual(invitation["installationId"], configured["installationId"])
            _, status = command("status")
            self.assertNotIn("secret", json.dumps(status))
            _, disabled = command("disable")
            self.assertFalse(disabled["enabled"])
            code, error = command("invite")
            self.assertEqual(code, 1)
            self.assertEqual(error["error"]["code"], "companion_unavailable")

    def test_existing_service_cli_opt_in_and_invalid_target(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"USERPROFILE": str(Path(directory) / "user"), "LOCALAPPDATA": str(Path(directory) / "appdata")}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--home", directory, "configure", "--origin", ORIGIN, "--upstream", "http://127.0.0.1:8791", "--existing-service"])
            self.assertEqual(code, 1)
            self.assertFalse(client_key_path(directory, "companion").exists())
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "companion_unavailable")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--home", directory, "configure", "--origin", ORIGIN, "--existing-service"])
            self.assertEqual(code, 0)
            self.assertIs(json.loads(output.getvalue())["existingService"], True)

    def test_allowlist_has_no_management_or_encoded_traversal(self):
        for method, path in (("POST", "/v1/workspaces"), ("GET", "/v1/environment"), ("POST", "/v1/service/shutdown"),
                             ("GET", "/v1/tasks/../auth"), ("GET", "/v1/tasks/%2e%2e/events")):
            self.assertFalse(allowed(method, path))
        for method, path in (("GET", "/v1/workspaces/workspace"), ("POST", "/v1/chats/chat/followups"),
                             ("DELETE", "/v1/chats/chat/followups/item"), ("GET", "/v1/tasks/task/artifacts/report.pdf")):
            self.assertTrue(allowed(method, path))

    def test_invalid_cli_arguments_emit_content_free_json(self):
        for arguments in (["unexpected-sensitive-command"], ["configure", "--origin", ORIGIN, "--port", "sensitive"]):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = main(arguments)
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "companion_unavailable")
            self.assertEqual(errors.getvalue(), "")
            self.assertNotIn("sensitive", output.getvalue())
