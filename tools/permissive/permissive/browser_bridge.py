"""Task-owned browser attachment broker and Chromium native messaging host."""

import asyncio
import json
import os
import re
import struct
import sys
import threading
import uuid

from aiohttp import ClientSession, WSMsgType, web
from .credentials import default_home
from .client import SignedSession


class BrowserBridge:
    def __init__(self):
        self.metadata = {}
        self.connections, self.pending, self.owners, self.pending_connections = (
            {},
            {},
            {},
            {},
        )

    async def extension(self, request):
        socket = web.WebSocketResponse(heartbeat=15)
        await socket.prepare(request)
        ident = str(uuid.uuid4())
        self.connections[ident] = socket
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    value = json.loads(message.data)
                    if value.get("operation") == "hello":
                        browser, profile = value.get("browser"), value.get("profileId")
                        if browser in {"chrome", "msedge"} and isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
                            self.metadata[ident] = {"browser": browser, "profileId": profile,
                                                    "profileName": str(value.get("profileName", "Browser profile"))[:120]}
                            local_profile = value.get("localProfileId", "")
                            if isinstance(local_profile, str) and re.fullmatch(r"local-[a-f0-9]{24}", local_profile):
                                self.metadata[ident]["localProfileId"] = local_profile
                        continue
                    response_id = value.get("id")
                    if self.pending_connections.get(response_id) == ident and (
                        future := self.pending.get(response_id)
                    ):
                        if not future.done():
                            future.set_result(value)
        finally:
            self.connections.pop(ident, None)
            self.metadata.pop(ident, None)
            for request_id, future in self.pending.items():
                if self.pending_connections[request_id] == ident and not future.done():
                    future.set_exception(RuntimeError("Browser extension disconnected"))
            self.owners = {
                tab: task for tab, task in self.owners.items() if tab[0] != ident
            }
        return socket

    def catalog(self):
        return [{"connectionId": ident, **self.metadata.get(ident, {})} for ident in self.connections]

    def capabilities(self, request):
        """Read the selected connection inventory without opening or changing a tab."""
        if request.get('browserMode', 'dedicated') == 'dedicated':
            return {'status': 'launch_on_demand', 'reason': 'Dedicated browser launches on the first browser tool call.'}
        browser, profile = request.get('browser', 'chromium'), request.get('browserProfile', '')
        matches = [item for item in self.catalog()
                   if (browser == 'chromium' or item.get('browser') == browser)
                   and (not profile or profile in {item.get('profileId'), item.get('localProfileId')})
                   and not self.connections[item['connectionId']].closed]
        if len(matches) == 1:
            return {'status': 'connected'}
        if len(matches) > 1:
            return {'status': 'unavailable', 'reason': 'More than one connection matches; select a specific browser profile.'}
        return {'status': 'disconnected', 'reason': 'Selected browser profile extension is not connected. Use Enable and connect in chat options.'}

    async def command(self, request):
        body = await request.json()
        task = body["taskId"]
        connection = body.get("connectionId")
        browser, profile = body.get("browser"), body.get("profileId")
        if browser or profile:
            candidates = [item["connectionId"] for item in self.catalog()
                          if (not browser or item.get("browser") == browser) and (not profile or profile in {item.get("profileId"), item.get("localProfileId")})]
            if connection and connection not in candidates:
                raise web.HTTPConflict(text="Connection does not match the selected browser profile")
            if not connection:
                if not candidates:
                    raise web.HTTPConflict(text="The selected browser profile is not connected. Open Browser and profile in the chat options and use Enable and connect for that profile. Opening a browser window alone does not connect its extension.")
                if len(candidates) > 1:
                    raise web.HTTPConflict(text="Multiple connections match the selected browser profile. Select a specific connected profile in Browser and profile, or reconnect it so only one matching connection remains.")
                connection = candidates[0]
        if not connection and body["operation"] == "list" and len(self.connections) > 1:
            offset = body.get("offset", 0)
            connections = list(self.connections)
            return web.json_response(
                {
                    "connections": [
                        {"connectionId": value, **self.metadata.get(value, {})}
                        for value in connections[offset : offset + 10]
                    ],
                    "nextOffset": offset + 10
                    if offset + 10 < len(connections)
                    else None,
                }
            )
        if not connection and len(self.connections) == 1:
            connection = next(iter(self.connections))
        socket = self.connections.get(connection)
        if socket is None:
            raise web.HTTPConflict(
                text="Connect the Codex Permissive extension; select connectionId if multiple browsers are connected"
            )
        operation, tab = body["operation"], body.get("tabId")
        key = (connection, tab)
        creating_tab = operation == "open" and tab is None
        if operation != "list" and not creating_tab:
            if not isinstance(tab, int):
                raise ValueError("tabId is required")
            if self.owners.get(key, task) != task:
                raise web.HTTPConflict(text="Tab is owned by another task")
            self.owners[key] = task
        ident = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        self.pending_connections[ident] = connection
        try:
            await socket.send_json({**body, "id": ident})
            try:
                response = await asyncio.wait_for(future, 120)
            except (RuntimeError, ConnectionError):
                raise web.HTTPConflict(text="Browser extension disconnected; retry after the selected profile reconnects") from None
            except asyncio.TimeoutError:
                raise web.HTTPGatewayTimeout(text="The selected browser did not respond; check its connection and retry") from None
            if creating_tab and not response.get("error"):
                created_tab = response.get("result", {}).get("tabId")
                if not isinstance(created_tab, int):
                    raise ValueError("Browser did not return the created tab ID")
                self.owners[(connection, created_tab)] = task
            if operation == "release":
                self.owners.pop(key, None)
            return web.json_response({**response, "connectionId": connection})
        finally:
            self.pending.pop(ident, None)
            self.pending_connections.pop(ident, None)

    async def release_task(self, task):
        for (connection, tab), owner in list(self.owners.items()):
            if owner == task:
                socket = self.connections.get(connection)
                if socket and not socket.closed:
                    try:
                        await socket.send_json(
                            {
                                "id": str(uuid.uuid4()),
                                "operation": "release",
                                "tabId": tab,
                            }
                        )
                    except ConnectionError:
                        pass
                self.owners.pop((connection, tab), None)


async def native_host():
    home = default_home()
    async with SignedSession(home=home, scope="browser") as client:
        async with client.ws_connect(
            "http://127.0.0.1:8789/v1/browser/extension", heartbeat=15
        ) as socket:
            loop = asyncio.get_running_loop()
            incoming = asyncio.Queue()

            def pump_browser():
                def exact(length):
                    data = bytearray()
                    while len(data) < length:
                        part = os.read(sys.stdin.fileno(), length - len(data))
                        if not part:
                            if data:
                                raise ValueError("Incomplete native message")
                            return None
                        data.extend(part)
                    return bytes(data)

                try:
                    while True:
                        header = exact(4)
                        if header is None:
                            loop.call_soon_threadsafe(incoming.put_nowait, None)
                            return
                        length = struct.unpack("<I", header)[0]
                        if length > 8 * 1024 * 1024:
                            raise ValueError("Native message exceeds size limit")
                        payload = exact(length)
                        if payload is None:
                            raise ValueError("Incomplete native message")
                        loop.call_soon_threadsafe(incoming.put_nowait, json.loads(payload))
                except Exception as error:
                    try:
                        loop.call_soon_threadsafe(incoming.put_nowait, error)
                    except RuntimeError:
                        pass

            # A browser can keep stdin open after the service closes its socket.
            # A daemon using raw OS reads does not hold asyncio's executor or a
            # buffered stdin lock alive during process shutdown.
            threading.Thread(target=pump_browser, daemon=True).start()

            async def read_browser():
                while (value := await incoming.get()) is not None:
                    if isinstance(value, Exception):
                        await socket.close()
                        raise value
                    await socket.send_json(value)
                await socket.close()

            reader = asyncio.create_task(read_browser())
            try:
                async for message in socket:
                    if message.type == WSMsgType.TEXT:
                        raw = message.data.encode()
                        sys.stdout.buffer.write(struct.pack("<I", len(raw)) + raw)
                        sys.stdout.buffer.flush()
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(native_host())
