"""One-shot, structured relay from PowerShell Direct to the guest's local API.

The host invokes this module with fixed executable/module arguments and supplies
JSON as stdin or --request-base64. No request field becomes a shell command or an
API origin. Native commands and desktop input execute in the separately started
interactive guest worker, never in this noninteractive relay process.
"""

import argparse
import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import ClientError, ClientTimeout

from .auth import client_key_path
from .client import SignedSession
from .credentials import default_home

PROTOCOL = "permissive-guest-bridge/v1"
MAX_REQUEST_BYTES = 24 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_BINARY_BYTES = 4 * 1024 * 1024
DEFAULT_BINARY_BYTES = 1024 * 1024
SEGMENT = r"[A-Za-z0-9_.-]{1,128}"
ROUTES = {
    "GET": [r"/v1/(?:health|openapi\.json|workspaces|skills|skills/document|browsers|tasks|chats)",
            r"/v1/credentials",
            rf"/v1/workspaces/{SEGMENT}", rf"/v1/attachments/{SEGMENT}(?:/content)?",
            rf"/v1/tasks/{SEGMENT}(?:/events|/artifacts/{SEGMENT})?",
            rf"/v1/chats/{SEGMENT}(?:/events)?", r"/v1/browser/extension"],
    "POST": [r"/v1/(?:workspaces|tasks|attachments|skills/register|skills/export|skills/import|browsers/connect|browsers/setup|browser/command|service/shutdown)",
             r"/v1/credentials(?:/prepare)?",
             rf"/v1/tasks/{SEGMENT}/(?:cancel|pause|resume|continue|steer|retry)",
             rf"/v1/(?:workspaces|chats)/{SEGMENT}/secure-delete",
             rf"/v1/chats/{SEGMENT}/followups(?:/resume)?"],
    "PATCH": [rf"/v1/workspaces/{SEGMENT}", r"/v1/skills/categories"],
    "DELETE": [rf"/v1/attachments/{SEGMENT}", rf"/v1/chats/{SEGMENT}/followups/{SEGMENT}", rf"/v1/credentials(?:/{SEGMENT})?"],
}


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        raise BridgeError("invalid_request", f"Invalid {name}")
    return value


def segment(value):
    if not isinstance(value, str) or not re.fullmatch(SEGMENT, value) or value in {".", ".."}:
        raise BridgeError("invalid_request", "Invalid API identifier")
    return value


def api_origin(value):
    parsed = urlsplit(value)
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (parsed.scheme != "http" or not loopback or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise BridgeError("invalid_configuration", "Guest API must be an HTTP loopback origin")
    try:
        parsed.port
    except ValueError as error:
        raise BridgeError("invalid_configuration", "Invalid guest API port") from error
    return value.rstrip("/")


def route(request):
    if not isinstance(request, dict):
        raise BridgeError("invalid_request", "Request must be a JSON object")
    operation = request.get("operation", request.get("op", request.get("action", "request")))
    body = request.get("body")
    if body is not None and not isinstance(body, dict):
        raise BridgeError("invalid_request", "body must be a JSON object")
    method, path = request.get("method", "GET"), request.get("path")
    if operation != "request":
        simple = {"health": "/v1/health", "workspaces": "/v1/workspaces", "browsers": "/v1/browsers",
                  "chats": "/v1/chats", "skills": "/v1/skills"}
        if operation in simple:
            method, path = "GET", simple[operation]
        elif operation == "workspace":
            method, path = "GET", "/v1/workspaces/" + segment(request.get("workspaceId"))
        elif operation == "save_workspace":
            ident = request.get("workspaceId")
            method, path = ("PATCH", "/v1/workspaces/" + segment(ident)) if ident else ("POST", "/v1/workspaces")
        elif operation in {"submit", "upload_attachment"}:
            method, path = "POST", "/v1/tasks" if operation == "submit" else "/v1/attachments"
        elif operation in {"status", "result", "events", "pause", "resume", "cancel", "continue", "steer", "retry", "artifact"}:
            path = "/v1/tasks/" + segment(request.get("taskId"))
            method = "GET" if operation in {"status", "result", "events", "artifact"} else "POST"
            if operation == "artifact":
                path += "/artifacts/" + segment(request.get("artifactId"))
            elif operation not in {"status", "result"}:
                path += "/" + operation
        elif operation in {"attachment", "attachment_content", "delete_attachment"}:
            path = "/v1/attachments/" + segment(request.get("attachmentId"))
            if operation == "attachment_content":
                path += "/content"
            method = "DELETE" if operation == "delete_attachment" else "GET"
        elif operation in {"chat", "chat_events"}:
            method, path = "GET", "/v1/chats/" + segment(request.get("conversationId"))
            if operation == "chat_events":
                path += "/events?format=json&" + urlencode({"after": request.get("after", 0)})
        else:
            raise BridgeError("invalid_request", "Unknown guest bridge operation")
    if not isinstance(method, str) or not isinstance(path, str):
        raise BridgeError("invalid_request", "Expected method and API path")
    method = method.upper()
    parsed = urlsplit(path)
    if (parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/v1/")
            or any(ord(char) < 32 for char in path) or "\\" in path
            or not any(re.fullmatch(pattern, parsed.path) for pattern in ROUTES.get(method, []))):
        raise BridgeError("invalid_request", "Unsupported guest API route")
    return method, path, body


async def read_limited(content, maximum):
    chunks, total = [], 0
    while True:
        chunk = await content.read(min(65536, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum:
            raise BridgeError("response_too_large", "Guest response exceeds the relay limit")
        chunks.append(chunk)


class GuestBridge:
    def __init__(self, home=None, api_url="http://127.0.0.1:8789"):
        self.home = Path(home) if home is not None else default_home()
        self.api_url = api_origin(api_url)

    async def execute(self, request):
        operation = request.get("operation", request.get("op", request.get("action"))) if isinstance(request, dict) else None
        if operation == "runner_restart":
            if request.keys() - {"operation", "op", "action"}:
                raise BridgeError("invalid_request", "runner_restart takes no command arguments")
            return await restart_runner()
        if operation == "prepare_directory":
            if request.keys() - {"operation", "op", "action", "path"}:
                raise BridgeError("invalid_request", "prepare_directory takes only a guest path")
            path = guest_path(request.get("path"))
            created = not path.exists()
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path), "created": created, "ready": True}
        if operation == "stage_file":
            return await asyncio.to_thread(stage_file, request)
        method, path, body = route(request)
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        scope = "browser" if parsed.path.startswith("/v1/browser/") else "api"
        headers = {}
        key = request.get("idempotencyKey")
        if key is not None:
            if not isinstance(key, str) or not key or len(key) > 256 or any(ord(char) < 32 for char in key):
                raise BridgeError("invalid_request", "Invalid idempotencyKey")
            headers["Idempotency-Key"] = key
        is_events = method == "GET" and parsed.path.endswith("/events") and query.get("format") != ["json"]
        offset = integer(request.get("offset", 0), "offset")
        length = integer(request.get("length", DEFAULT_BINARY_BYTES), "length", 1, MAX_BINARY_BYTES)
        binary = method == "GET" and ("/artifacts/" in parsed.path or parsed.path.endswith("/content"))
        if binary:
            headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        try:
            after = request.get("after", int(query.get("after", [0])[0])) if is_events else 0
        except (TypeError, ValueError) as error:
            raise BridgeError("invalid_request", "Invalid event cursor") from error
        if is_events:
            after = integer(after, "after")
            headers["Last-Event-ID"] = str(after)
        # A guest-local identity is explicit: environment private-key overrides
        # must not accidentally select a controller or other client identity.
        async with SignedSession(home=self.home, private_key=client_key_path(self.home, scope),
                                 timeout=ClientTimeout(total=30, connect=5), read_bufsize=MAX_EVENT_BYTES) as client:
            async with client.request(method, self.api_url + path, json=body, headers=headers) as response:
                if binary and response.status == 416 and offset == 0 and response.headers.get("Content-Range") == "bytes */0":
                    return {"data": "", "offset": 0, "length": 0, "nextOffset": None, "eof": True, "size": 0,
                            "contentType": response.headers.get("Content-Type", "application/octet-stream")}
                if response.status >= 400:
                    details = (await read_limited(response.content, 65536)).decode("utf-8", "replace")
                    raise BridgeError("guest_api_error", f"Guest API HTTP {response.status}: {details[:500]}")
                if is_events:
                    return await self.events(response, request, after)
                if binary or "json" not in response.headers.get("Content-Type", "").lower():
                    return await self.binary(response, offset, length)
                raw = await read_limited(response.content, MAX_JSON_BYTES)
                try:
                    return json.loads(raw)
                except (UnicodeError, ValueError) as error:
                    raise BridgeError("invalid_guest_response", "Guest API returned invalid JSON") from error

    async def events(self, response, request, after):
        limit = integer(request.get("limit", 200), "limit", 1, 200)
        wait_ms = integer(request.get("waitMs", 200), "waitMs", 1, 2000)
        rows, used, more, fields = [], 0, False, []
        try:
            async with asyncio.timeout(wait_ms / 1000):
                while True:
                    line = await response.content.readline()
                    if not line:
                        break
                    if line.startswith(b"data:"):
                        fields.append(line[5:].strip())
                    elif line.strip() == b"" and fields:
                        raw = b"\n".join(fields)
                        fields = []
                        if len(raw) > MAX_EVENT_BYTES or used + len(raw) > MAX_EVENT_BYTES:
                            if not rows:
                                raise BridgeError("response_too_large", "Guest event exceeds the relay limit")
                            more = True
                            break
                        try:
                            event = json.loads(raw)
                        except (UnicodeError, ValueError) as error:
                            raise BridgeError("invalid_guest_response", "Invalid guest event JSON") from error
                        if not isinstance(event, dict) or type(event.get("id")) is not int or event["id"] <= after:
                            raise BridgeError("invalid_guest_response", "Invalid guest event cursor")
                        rows.append(event)
                        after = event["id"]
                        used += len(raw)
                        if len(rows) >= limit:
                            more = True
                            break
        except TimeoutError:
            pass
        return {"data": rows, "nextCursor": after, "hasMore": more}

    async def binary(self, response, offset, length):
        content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
        if content_range:
            if int(content_range[1]) != offset:
                raise BridgeError("invalid_guest_response", "Guest returned the wrong byte range")
            size = int(content_range[3])
        else:
            raw_size = response.headers.get("Content-Length", "")
            size = int(raw_size) if raw_size.isdigit() else None
            # Handle local API responses that ignore Range without buffering the file.
            skipped = 0
            while skipped < offset:
                chunk = await response.content.read(min(65536, offset - skipped))
                if not chunk:
                    raise BridgeError("invalid_request", "Byte offset exceeds guest content size")
                skipped += len(chunk)
        data = bytearray()
        while len(data) <= length:
            chunk = await response.content.read(min(65536, length + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        eof = offset + min(len(data), length) >= size if size is not None else len(data) <= length
        data = bytes(data[:length])
        return {"data": base64.b64encode(data).decode("ascii"), "offset": offset, "length": len(data),
                "nextOffset": None if eof else offset + len(data), "eof": eof, "size": size,
                "contentType": response.headers.get("Content-Type", "application/octet-stream")}


async def restart_runner():
    """Trigger the provisioned interactive guest runner without using its API."""
    executable = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    options = {"creationflags": 0x08000000} if os.name == "nt" else {}
    process = await asyncio.create_subprocess_exec(
        str(executable), "-NoProfile", "-NonInteractive", "-Command",
        "$ErrorActionPreference = 'Stop'; Start-ScheduledTask -TaskName 'CodexPermissiveGuest'",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
    )
    try:
        await asyncio.wait_for(process.communicate(), 10)
    except TimeoutError as error:
        process.kill()
        await process.communicate()
        raise BridgeError("guest_runner_timeout", "Guest runner trigger did not finish") from error
    if process.returncode:
        raise BridgeError("guest_runner_failed", "Could not trigger the provisioned guest runner")
    return {"triggered": True, "taskName": "CodexPermissiveGuest", "ready": False}


def guest_path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise BridgeError("invalid_request", "Expected an absolute guest Windows path")
    path = PureWindowsPath(value)
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive) or ".." in path.parts:
        raise BridgeError("invalid_request", "Expected an absolute local guest Windows path")
    if os.name != "nt":
        raise BridgeError("invalid_configuration", "Guest file staging requires Windows")
    return Path(path)


def stage_file(request):
    """Stage explicit controller-supplied bytes on the guest with retry safety."""
    if request.keys() - {"operation", "op", "action", "path", "dataBase64", "offset", "complete"}:
        raise BridgeError("invalid_request", "Unexpected stage_file field")
    path = guest_path(request.get("path"))
    if not path.name:
        raise BridgeError("invalid_request", "Expected a guest destination file")
    offset = integer(request.get("offset", 0), "offset")
    complete = request.get("complete", False)
    if type(complete) is not bool:
        raise BridgeError("invalid_request", "complete must be a boolean")
    encoded = request.get("dataBase64")
    if not isinstance(encoded, str) or len(encoded) > (MAX_BINARY_BYTES + 2) // 3 * 4:
        raise BridgeError("invalid_request", "Invalid staging chunk")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BridgeError("invalid_request", "Invalid staging base64") from error
    if len(data) > MAX_BINARY_BYTES:
        raise BridgeError("invalid_request", "Staging chunk exceeds the relay limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(str(path).casefold().encode("utf-8")).hexdigest()[:24]
    partial = path.parent / (".permissive-stage-" + identity + ".partial")
    end = offset + len(data)
    # A final response may have been lost after atomic replacement. The exact
    # already-committed bytes can acknowledge an identical retry without writing.
    if not partial.exists() and path.is_file() and (offset > 0 or complete):
        size = path.stat().st_size
        with path.open("rb") as existing:
            existing.seek(offset)
            same = existing.read(len(data)) == data and end <= size
        if same and (not complete or end == size):
            return {"path": str(path), "offset": offset, "written": len(data), "nextOffset": end,
                    "complete": complete, "size": size}
    if not partial.exists():
        if offset:
            raise BridgeError("staging_gap", "Staging chunk precedes its missing earlier bytes")
        with partial.open("xb"):
            pass
    with partial.open("r+b") as output:
        output.seek(0, 2)
        size = output.tell()
        if offset > size:
            raise BridgeError("staging_gap", "Staging chunk leaves a gap")
        overlap = min(len(data), size - offset)
        output.seek(offset)
        if output.read(overlap) != data[:overlap]:
            raise BridgeError("staging_conflict", "Staging retry differs from previously written bytes")
        if end > size:
            output.seek(size)
            output.write(data[overlap:])
            size = end
        if complete and size != end:
            raise BridgeError("staging_conflict", "Final staging size differs from existing bytes")
        output.flush()
        os.fsync(output.fileno())
    if complete:
        os.replace(partial, path)
    return {"path": str(path), "offset": offset, "written": len(data), "nextOffset": end,
            "complete": complete, "size": size}


def decode_request(encoded=None, stream=None):
    if encoded is not None:
        if len(encoded) > (MAX_REQUEST_BYTES + 2) // 3 * 4:
            raise BridgeError("invalid_request", "Request exceeds the relay limit")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise BridgeError("invalid_request", "Invalid base64 request") from error
    else:
        raw = (stream or sys.stdin.buffer).read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise BridgeError("invalid_request", "Request exceeds the relay limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise BridgeError("invalid_request", "Expected UTF-8 JSON request") from error
    if not isinstance(value, dict):
        raise BridgeError("invalid_request", "Request must be a JSON object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-base64")
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("--api-url", default="http://127.0.0.1:8789")
    args = parser.parse_args(argv)
    try:
        request = decode_request(args.request_base64)
        result = asyncio.run(GuestBridge(args.home, args.api_url).execute(request))
        response = {"protocol": PROTOCOL, "ok": True, "result": result}
    except BridgeError as error:
        response = {"protocol": PROTOCOL, "ok": False, "error": {"code": error.code, "message": str(error)}}
    except (ClientError, OSError, TimeoutError):
        response = {"protocol": PROTOCOL, "ok": False, "error": {"code": "guest_unavailable", "message": "Guest local API is unavailable"}}
    except Exception:
        response = {"protocol": PROTOCOL, "ok": False, "error": {"code": "bridge_failed", "message": "Guest relay could not complete the request"}}
    # ASCII JSON survives Windows PowerShell's native-output code page while
    # preserving Unicode through JSON escapes. Keep the complete frame bounded.
    encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
    if len(encoded) > MAX_JSON_BYTES:
        encoded = json.dumps({"protocol": PROTOCOL, "ok": False, "error": {
            "code": "response_too_large", "message": "Guest result exceeds the relay frame limit"}}, separators=(",", ":"))
    sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
