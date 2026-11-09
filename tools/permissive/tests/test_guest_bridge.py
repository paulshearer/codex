import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from permissive.auth import Authenticator, authorize, generate
from permissive.guest_bridge import BridgeError, GuestBridge, PROTOCOL, api_origin, decode_request, route


class RequestTests(unittest.TestCase):
    def test_only_fixed_guest_api_routes_and_loopback_origins(self):
        for path in ["https://example.com/v1/health", "//example.com/v1/health", "/v1/tasks/../health", "/v1/tasks/%2fhealth", "/v1/health#fragment", "/other", "/v1/health\r\nX: y"]:
            with self.subTest(path=path), self.assertRaises(BridgeError):
                route({"operation": "request", "method": "GET", "path": path})
        for url in ["https://example.com", "http://127.0.0.1.evil.test", "http://user@127.0.0.1", "http://127.0.0.1/path"]:
            with self.subTest(url=url), self.assertRaises(BridgeError):
                api_origin(url)
        self.assertEqual(api_origin("http://127.0.0.1:8789/"), "http://127.0.0.1:8789")
        self.assertEqual(route({"op": "request", "method": "POST", "path": "/v1/tasks/t-1/pause"}), ("POST", "/v1/tasks/t-1/pause", None))
        self.assertEqual(route({"operation": "retry", "taskId": "t-1"}), ("POST", "/v1/tasks/t-1/retry", None))

    def test_utf8_stdin_and_base64_are_data_not_code(self):
        value = {"operation": "submit", "body": {"prompt": "$(Get-Process); `x` ' \" 日本語"}}
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.assertEqual(decode_request(base64.b64encode(raw).decode()), value)
        self.assertEqual(decode_request(stream=io.BytesIO(raw)), value)
        for encoded in ["?bad", base64.b64encode(b"[]").decode(), base64.b64encode('{"op":"health"}'.encode("utf-16")).decode()]:
            with self.subTest(encoded=encoded), self.assertRaises(BridgeError):
                decode_request(encoded)

    def test_cli_returns_one_error_frame_without_traceback(self):
        request = base64.b64encode(json.dumps({"path": "https://example.com/v1/health"}).encode()).decode()
        process = subprocess.run([sys.executable, "-m", "permissive.guest_bridge", "--request-base64", request], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertEqual(len(process.stdout.splitlines()), 1)
        frame = json.loads(process.stdout)
        self.assertEqual(frame["protocol"], PROTOCOL)
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"]["code"], "invalid_request")


class GuestBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.keys = {}
        for scope in ("api", "browser"):
            private = self.home / (scope + ".pem")
            public = private.with_suffix(".pub")
            generate(private, public)
            authorize(self.home, public.read_bytes(), "Guest " + scope, (scope,))
            self.keys[scope] = private
        self.auth = Authenticator(self.home)
        self.calls = []
        self.content = b"first\x00second\xffthird"
        self.artifact = self.home / "artifact.bin"
        self.artifact.write_bytes(self.content)

        @web.middleware
        async def authenticate(request, handler):
            try:
                self.auth.verify(request.method, request.raw_path, await request.read(), request.headers)
            except Exception as error:
                return web.json_response({"error": str(error)}, status=401)
            return await handler(request)

        app = web.Application(middlewares=[authenticate])
        app.router.add_route("*", "/v1/{tail:.*}", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.patch = patch("permissive.guest_bridge.client_key_path", lambda home, scope: self.keys[scope])
        self.patch.start()
        self.bridge = GuestBridge(self.home, str(self.server.make_url("/")))

    async def asyncTearDown(self):
        self.patch.stop()
        await self.server.close()
        self.auth.close()
        self.temporary.cleanup()

    async def handle(self, request):
        raw = await request.read()
        body = json.loads(raw) if raw else None
        self.calls.append((request.method, request.raw_path, body, dict(request.headers)))
        if request.path.endswith("/events"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            after = int(request.headers.get("Last-Event-ID", 0))
            if "/empty/" in request.path:
                await response.write(b": heartbeat\n\n")
                await asyncio.sleep(.1)
            else:
                for ident in (3, 4, 5):
                    if ident > after:
                        event = {"id": ident, "kind": "agent", "data": {"method": "item/completed", "params": {"item": {"type": "commandExecution", "exitCode": 0}}}, "createdAt": ident}
                        await response.write(("id: " + str(ident) + "\nevent: agent\ndata: " + json.dumps(event) + "\n\n").encode())
            await response.write_eof()
            return response
        if "/artifacts/" in request.path or request.path.endswith("/content"):
            return web.FileResponse(self.artifact)
        if request.path == "/v1/tasks/missing":
            return web.json_response({"error": "Missing task"}, status=404)
        if request.path == "/v1/health":
            return web.json_response({"ok": True, "model": "/models/deepseek-v4-flash"})
        if request.path == "/v1/tasks":
            return web.json_response({"id": "guest-task", "status": "queued", "request": body})
        return web.json_response({"id": "guest-task", "status": "completed", "threadId": "native-thread", "result": "703", "body": body})

    async def test_signed_local_submit_preserves_structured_payload(self):
        body = {"prompt": "$(malicious shell syntax); `x` ' \" 日本語", "workingDirectory": "C:/guest workspace"}
        with patch.dict(os.environ, {"PERMISSIVE_PRIVATE_KEY": "C:/not-the-guest-key.pem"}):
            result = await self.bridge.execute({"op": "request", "method": "POST", "path": "/v1/tasks", "body": body, "idempotencyKey": "controller-attempt-1"})
        self.assertEqual(result["request"], body)
        self.assertEqual(self.calls[-1][2], body)
        headers = {key.lower(): value for key, value in self.calls[-1][3].items()}
        self.assertEqual(headers["idempotency-key"], "controller-attempt-1")
        self.assertIn("signature", headers)

    async def test_status_and_browser_routes_use_guest_identities(self):
        health = await self.bridge.execute({"operation": "health"})
        self.assertEqual(health["model"], "/models/deepseek-v4-flash")
        status = await self.bridge.execute({"operation": "status", "taskId": "guest-task"})
        self.assertEqual(status["threadId"], "native-thread")
        await self.bridge.execute({"operation": "request", "method": "POST", "path": "/v1/browser/command", "body": {"operation": "list"}})
        self.assertEqual(self.calls[-1][2], {"operation": "list"})

    async def test_event_pages_keep_actual_native_events_and_cursor(self):
        first = await self.bridge.execute({"operation": "events", "taskId": "guest-task", "after": 0, "limit": 2})
        self.assertEqual([event["id"] for event in first["data"]], [3, 4])
        self.assertEqual(first["nextCursor"], 4)
        self.assertTrue(first["hasMore"])
        self.assertEqual(first["data"][0]["data"]["params"]["item"]["type"], "commandExecution")
        second = await self.bridge.execute({"operation": "request", "method": "GET", "path": "/v1/tasks/guest-task/events?after=4"})
        self.assertEqual([event["id"] for event in second["data"]], [5])
        self.assertEqual(second["nextCursor"], 5)
        self.assertFalse(second["hasMore"])

    async def test_empty_live_event_poll_is_bounded(self):
        began = time.monotonic()
        result = await self.bridge.execute({"operation": "events", "taskId": "empty", "after": 9, "waitMs": 10})
        self.assertEqual(result, {"data": [], "nextCursor": 9, "hasMore": False})
        self.assertLess(time.monotonic() - began, 1)

    async def test_binary_artifacts_are_chunked_and_reassemble(self):
        data, offset = bytearray(), 0
        while True:
            result = await self.bridge.execute({"operation": "artifact", "taskId": "guest-task", "artifactId": "evidence.bin", "offset": offset, "length": 4})
            self.assertEqual(result["offset"], offset)
            self.assertEqual(result["size"], len(self.content))
            data.extend(base64.b64decode(result["data"]))
            if result["eof"]:
                self.assertIsNone(result["nextOffset"])
                break
            offset = result["nextOffset"]
        self.assertEqual(bytes(data), self.content)

    async def test_empty_artifact_is_a_complete_zero_byte_frame(self):
        self.artifact.write_bytes(b"")
        result = await self.bridge.execute({"operation": "artifact", "taskId": "guest-task", "artifactId": "empty.bin"})
        self.assertEqual(result["data"], "")
        self.assertEqual(result["size"], 0)
        self.assertTrue(result["eof"])
        self.assertIsNone(result["nextOffset"])

    async def test_guest_error_and_response_limits_are_explicit(self):
        with self.assertRaises(BridgeError) as error:
            await self.bridge.execute({"operation": "status", "taskId": "missing"})
        self.assertEqual(error.exception.code, "guest_api_error")
        with patch("permissive.guest_bridge.MAX_JSON_BYTES", 5), self.assertRaises(BridgeError) as error:
            await self.bridge.execute({"operation": "health"})
        self.assertEqual(error.exception.code, "response_too_large")
        for value in [True, -1, "bad"]:
            with self.subTest(after=value), self.assertRaises(BridgeError):
                await self.bridge.execute({"operation": "events", "taskId": "guest-task", "after": value})

    async def test_runner_trigger_is_fixed_and_does_not_claim_readiness(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        with patch("permissive.guest_bridge.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as launch:
            result = await self.bridge.execute({"op": "runner_restart"})
        self.assertEqual(result, {"triggered": True, "taskName": "CodexPermissiveGuest", "ready": False})
        self.assertIn("Start-ScheduledTask -TaskName 'CodexPermissiveGuest'", launch.call_args.args[-1])
        self.assertEqual(self.calls, [])
        with self.assertRaises(BridgeError):
            await self.bridge.execute({"op": "runner_restart", "command": "other"})

    @unittest.skipUnless(os.name == "nt", "Windows guest path semantics")
    async def test_guest_file_staging_is_atomic_and_identical_retries_are_safe(self):
        directory = self.home / "guest-workspace" / "imported-skill"
        result = await self.bridge.execute({"op": "prepare_directory", "path": str(directory)})
        self.assertTrue(result["created"])
        destination = directory / "SKILL.md"
        def chunk(data, offset=0, complete=False):
            return {"op": "stage_file", "path": str(destination), "dataBase64": base64.b64encode(data).decode(), "offset": offset, "complete": complete}
        first = await self.bridge.execute(chunk(b"first"))
        self.assertFalse(destination.exists())
        self.assertEqual(first["nextOffset"], 5)
        self.assertEqual(await self.bridge.execute(chunk(b"first")), first)
        with self.assertRaises(BridgeError) as error:
            await self.bridge.execute(chunk(b"WRONG"))
        self.assertEqual(error.exception.code, "staging_conflict")
        with self.assertRaises(BridgeError) as error:
            await self.bridge.execute(chunk(b"gap", 20))
        self.assertEqual(error.exception.code, "staging_gap")
        final = await self.bridge.execute(chunk(b" second", 5, True))
        self.assertEqual(destination.read_bytes(), b"first second")
        self.assertTrue(final["complete"])
        self.assertEqual(await self.bridge.execute(chunk(b" second", 5, True)), final)
        self.assertFalse(list(directory.glob("*.partial")))
        self.assertEqual(self.calls, [])

    async def test_staging_rejects_relative_unc_and_parent_paths(self):
        for value in ["relative.txt", r"C:relative.txt", r"\\server\share\file.txt", r"C:\folder\..\file.txt"]:
            with self.subTest(path=value), self.assertRaises(BridgeError):
                await self.bridge.execute({"op": "stage_file", "path": value, "dataBase64": "", "offset": 0, "complete": True})


if __name__ == "__main__":
    unittest.main()
