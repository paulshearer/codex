"""Tailnet-only Android companion sidecar and local owner management commands.

The network listener has no management API. It accepts paired P-256 phone
requests and mediates an explicit subset of the existing signed host API.
"""

import argparse
import asyncio
from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from aiohttp import ClientError, ClientTimeout, web
from cryptography.exceptions import InvalidSignature
from yarl import URL

from .auth import Signer, authorize, client_key_path, generate, registry
from .client import SignedSession
from .credentials import default_home
from .mobile_auth import EXISTING_SERVICE_UPSTREAM, MobileState, validate_configuration
from .privacy import http_server_options


SEGMENT = r"(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]{1,128}"
ARTIFACT = r"(?!\.{1,2}(?:/|$))[^/\\\x00-\x1f\x7f]{1,512}"
SECURE_DELETE = rf"/v1/(?:workspaces|chats)/({SEGMENT})/secure-delete"
ROUTES = {
    "PATCH": [rf"/v1/chats/{SEGMENT}"],
    "GET": [r"/v1/(?:health|workspaces|tasks|chats|browsers)", rf"/v1/workspaces/{SEGMENT}",
            r"/v1/credentials",
            rf"/v1/(?:tasks|chats)/{SEGMENT}(?:/events)?", rf"/v1/tasks/{SEGMENT}/artifacts/{ARTIFACT}",
            rf"/v1/attachments/{SEGMENT}(?:/content)?"],
    "POST": [r"/v1/(?:tasks|attachments)", rf"/v1/tasks/{SEGMENT}/(?:continue|steer|pause|resume|cancel|retry)",
             r"/v1/credentials(?:/prepare)?",
             rf"/v1/chats/{SEGMENT}/(?:fork|delete-from|delete-message|regenerate|edit-message)", rf"/v1/chats/{SEGMENT}/followups(?:/resume)?", SECURE_DELETE],
    "DELETE": [rf"/v1/attachments/{SEGMENT}", rf"/v1/chats/{SEGMENT}/followups/{SEGMENT}", rf"/v1/credentials(?:/{SEGMENT})?"],
}
MAX_BODY = 23 * 1024 * 1024
POLL_SECONDS = 1
STREAM_CHECK_SECONDS = 15


def allowed(method, path):
    return any(re.fullmatch(pattern, path) for pattern in ROUTES.get(method, ()))


def provision_backend(home):
    path = client_key_path(home, "companion")
    if not path.exists():
        generate(path, path.with_name("public-key.pub"))
    signer = Signer(path)
    from cryptography.hazmat.primitives import serialization
    public = signer.key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    authorize(home, public, "Local companion API client", ("api",))
    return signer


@contextmanager
def sidecar_lock(root):
    path = Path(root) / "gateway.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("The companion is already running") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def failure(code, message, status):
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def local_status(state):
    result = state.status()
    result["running"] = False
    marker = state.root / "ready.json"
    if not result["enabled"] or not marker.exists():
        return result
    try:
        ready = json.loads(marker.read_text(encoding="utf-8"))
        if any(ready.get(key) != result[key] for key in ("installationId", "origin", "upstream", "port", "existingService")):
            return result
        try:
            with sidecar_lock(state.root):
                return result
        except ValueError:
            result["running"] = True
    except (ValueError, OSError):
        pass
    return result


class MobileGateway:
    def __init__(self, home, *, stream_check_seconds=STREAM_CHECK_SECONDS):
        self.home = Path(home)
        self.state = MobileState(home)
        self.configuration = self.state.settings()
        self.upstream = self.configuration["upstream"]
        self.session = None
        self.closing = False
        self.stream_check_seconds = min(STREAM_CHECK_SECONDS, max(.01, stream_check_seconds))
        self.streams = set()

    async def start(self, app):
        settings = self.state.settings()
        if not settings["enabled"] or not settings["origin"] or not self.upstream:
            raise ValueError("The companion is disabled")
        # Startup must never silently restore a locally revoked client key.
        signer = Signer(client_key_path(self.home, "companion"))
        entry = next((item for item in registry(self.home)["keys"] if item["id"] == signer.key_id), None)
        if not entry or "api" not in entry["scopes"]:
            raise ValueError("Companion API client is not authorized")
        self.session = SignedSession(private_key=client_key_path(self.home, "companion"), auto_decompress=False)
        try:
            await self.controller()
        except BaseException:
            await self.session.__aexit__(None, None, None)
            self.session = None
            raise

    async def close(self, app):
        await self.shutdown(app)
        if self.session:
            await self.session.__aexit__(None, None, None)
        self.state.close()

    async def shutdown(self, app):
        self.closing = True
        for stream in list(self.streams):
            stream.cancel()
        if self.streams:
            await asyncio.gather(*list(self.streams), return_exceptions=True)

    async def controller(self):
        async with self.session.get(self.upstream + "/v1/health", timeout=ClientTimeout(total=10)) as response:
            if response.status != 200:
                raise ValueError("The host controller is unavailable")
            data = await response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ValueError("The Windows host is unavailable")
            if self.configuration["existingService"]:
                valid_role = self.upstream == EXISTING_SERVICE_UPSTREAM and "executionRole" not in data
            else:
                valid_role = data.get("executionRole") == "controller"
            if not valid_role:
                raise ValueError("The upstream is not a Windows host controller")
            return data

    @web.middleware
    async def guard(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            return failure("invalid_request", "The request is too large", 413)
        except Exception:
            # Neither exceptions nor request objects enter HTTP diagnostics.
            return failure("unavailable", "The companion is unavailable", 503)

    @web.middleware
    async def authenticate(self, request, handler):
        if "Origin" in request.headers or "Tailscale-Funnel-Request" in request.headers:
            return failure("forbidden", "This client cannot access the companion", 403)
        if not self.state.settings()["enabled"] or self.state.settings() != self.configuration:
            return failure("disabled", "The companion is unavailable", 503)
        if len(request.headers.getall("Host", [])) != 1:
            return failure("invalid_signature", "A paired device signature is required", 401)
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            return failure("invalid_request", "Unsupported request encoding", 400)
        if not (request.method == "POST" and request.path == "/mobile/v1/enroll"
                or request.method == "GET" and request.path == "/mobile/v1/identity"
                or allowed(request.method, request.path)):
            return failure("not_found", "This operation is unavailable", 404)
        try:
            body = await request.read()
            if request.path == "/mobile/v1/enroll":
                if len(body) > 4096 or request.content_type != "application/json":
                    return failure("invalid_enrollment", "Pairing could not be completed", 400)
                try:
                    identity, created = self.state.enroll(json.loads(body), request.method,
                        request.headers["Host"], request.raw_path, body, request.headers)
                except (ValueError, KeyError, TypeError, InvalidSignature, UnicodeError):
                    return failure("invalid_enrollment", "Pairing could not be completed", 401)
                return web.json_response(identity, status=201 if created else 200)
            request["mobileDeviceId"] = self.state.verify(request.method, request.headers["Host"], request.raw_path, body, request.headers)
        except (ValueError, KeyError, TypeError, InvalidSignature, UnicodeError):
            return failure("invalid_signature", "A paired device signature is required", 401)
        except web.HTTPRequestEntityTooLarge:
            return failure("invalid_request", "The request is too large", 413)
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError):
            return failure("invalid_request", "The request is invalid", 400)
        except (ClientError, asyncio.TimeoutError, OSError):
            return failure("host_unavailable", "The Windows host is unavailable", 503)
        except sqlite3.Error:
            return failure("unavailable", "The companion is unavailable", 503)

    async def identity(self, request):
        return web.json_response(self.state.identity(request["mobileDeviceId"]))

    async def validate_body(self, request, body):
        if body:
            if request.content_type != "application/json":
                raise ValueError("JSON required")
            value = json.loads(body)
            if not isinstance(value, dict) or value.keys() & {"workingDirectory", "directory", "cwd", "credentialWorkspaceId"}:
                raise ValueError("Directory overrides are unavailable")
        else:
            value = {}
        deletion = re.fullmatch(SECURE_DELETE, request.path) if request.method == "POST" else None
        if deletion and (value.keys() != {"confirm", "passes"} or value.get("confirm") != deletion[1]
                         or type(value.get("passes")) is not int or value["passes"] != 7):
            raise ValueError("Confirm the selected ID and seven overwrite passes")
        if request.method == "POST" and request.path in {"/v1/tasks", "/v1/attachments"}:
            workspace = value.get("workspaceId")
            if not isinstance(workspace, str) or not re.fullmatch(SEGMENT, workspace):
                raise ValueError("An existing workspace is required")
            async with self.session.get(self.upstream + "/v1/workspaces/" + workspace,
                                        timeout=ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise ValueError("An existing workspace is required")
                result = await response.json()
                if not isinstance(result, dict) or result.get("id") != workspace:
                    raise ValueError("An existing workspace is required")

    async def proxy(self, request):
        try:
            await self.controller()
        except (ValueError, ClientError, asyncio.TimeoutError):
            return failure("host_unavailable", "The Windows host controller is unavailable", 503)
        body = await request.read()
        await self.validate_body(request, body)
        # Do not forward any caller-selected authentication, destination, proxy,
        # encoding, or connection headers. The exact body and control fields survive.
        headers = {field: request.headers[field] for field in ("content-type", "idempotency-key", "last-event-id") if field in request.headers}
        if not self.state.active(request["mobileDeviceId"]):
            return failure("invalid_signature", "A paired device signature is required", 401)
        target = URL(self.upstream + request.raw_path, encoded=True)
        async with self.session.request(request.method, target, data=body if body else None, headers=headers) as upstream:
            response_headers = {field: upstream.headers[field] for field in
                ("Content-Type", "Content-Length", "Content-Disposition", "Content-Encoding", "Cache-Control", "ETag", "Last-Modified") if field in upstream.headers}
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            await response.prepare(request)
            task = asyncio.current_task()
            self.streams.add(task)
            reader = None
            try:
                while not self.closing and self.state.active(request["mobileDeviceId"]):
                    if reader is None:
                        reader = asyncio.create_task(upstream.content.read(64 * 1024))
                    done, _ = await asyncio.wait({reader}, timeout=self.stream_check_seconds)
                    if not self.state.active(request["mobileDeviceId"]) or self.closing:
                        break
                    if not done:
                        # The upstream supplies SSE heartbeats. Injecting one here
                        # could split an event between two arriving body chunks.
                        continue
                    chunk = reader.result(); reader = None
                    if not chunk:
                        break
                    await asyncio.wait_for(response.write(chunk), timeout=STREAM_CHECK_SECONDS)
            except (ConnectionError, ClientError, asyncio.CancelledError, asyncio.TimeoutError, sqlite3.Error):
                pass
            finally:
                if reader is not None:
                    reader.cancel()
                    with suppress(asyncio.CancelledError, ClientError, ConnectionError):
                        await reader
                self.streams.discard(task)
            with suppress(ConnectionError):
                await response.write_eof()
            return response

    def app(self):
        app = web.Application(middlewares=[self.guard, self.authenticate], client_max_size=MAX_BODY,
                              handler_args={**http_server_options(), "auto_decompress": False})
        app.router.add_get("/mobile/v1/identity", self.identity)
        app.router.add_route("*", "/{path:.*}", self.proxy)
        app.on_startup.append(self.start)
        app.on_shutdown.append(self.shutdown)
        app.on_cleanup.append(self.close)
        return app


async def serve(home):
    gateway = MobileGateway(home)
    try:
        with sidecar_lock(gateway.state.root):
            runner = web.AppRunner(gateway.app(), access_log=None, shutdown_timeout=2)
            marker = gateway.state.root / "ready.json"
            try:
                marker.unlink(missing_ok=True)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", gateway.configuration["port"])
                await site.start()
                ready = {key: gateway.configuration[key] for key in ("installationId", "origin", "upstream", "port", "existingService")}
                marker.write_text(json.dumps(ready), encoding="utf-8")
                print(json.dumps({**gateway.state.status(), "running": True, "listening": True}), flush=True)
                while gateway.state.settings() == gateway.configuration:
                    await asyncio.sleep(POLL_SECONDS)
            finally:
                marker.unlink(missing_ok=True)
                await runner.cleanup()
    finally:
        if not gateway.closing:
            gateway.state.close()


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid companion command")


def main(argv=None):
    parser = JsonParser(description="Local companion management")
    parser.add_argument("--home", default=str(default_home()))
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--origin", required=True)
    configure.add_argument("--upstream", default="http://127.0.0.1:8789")
    configure.add_argument("--port", type=int, default=8790)
    configure.add_argument("--existing-service", action="store_true")
    for command in ("status", "invite", "devices", "disable", "serve"):
        commands.add_parser(command)
    commands.add_parser("revoke").add_argument("device_id")
    try:
        args = parser.parse_args(argv)
        if args.command == "serve":
            asyncio.run(serve(args.home))
            return 0
        state = MobileState(args.home)
        try:
            if args.command == "configure":
                # Validate all inputs before provisioning authorization.
                validate_configuration(args.origin, args.upstream, args.port, args.existing_service)
                provision_backend(args.home)
                result = state.configure(args.origin, args.upstream, args.port, existing_service=args.existing_service)
            elif args.command == "revoke":
                result = state.revoke(args.device_id)
            elif args.command == "devices":
                result = {"devices": state.devices()}
            elif args.command == "status":
                result = local_status(state)
            else:
                result = getattr(state, args.command)()
            if args.command in {"configure", "disable"}:
                result["running"] = local_status(state)["running"]
            print(json.dumps(result, ensure_ascii=True))
        finally:
            state.close()
        return 0
    except Exception:
        print(json.dumps({"error": {"code": "companion_unavailable", "message": "The companion operation could not be completed"}}))
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
