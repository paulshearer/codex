import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from permissive.sparky_gateway import application, MODEL


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_prefill_heartbeats_and_disconnect_cancels(self):
        closed = asyncio.Event()

        async def inference(request):
            stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await stream.prepare(request)
            try:
                await asyncio.sleep(10)
            finally:
                closed.set()
            return stream

        backend = web.Application()
        backend.router.add_post("/v1/responses", inference)
        async with TestServer(backend, handler_cancellation=True) as server:
            with tempfile.TemporaryDirectory() as directory:
                client = TestClient(
                    TestServer(
                        application(
                            str(server.make_url("")).rstrip("/"),
                            "private-test-credential",
                            Path(directory) / "maintenance",
                            heartbeat=0.02,
                        ),
                        handler_cancellation=True,
                    )
                )
                async with client:
                    response = await client.post(
                        "/v1/responses", json={"model": MODEL, "stream": True}
                    )
                    self.assertEqual(
                        await response.content.readline(), b": processing\n"
                    )
                    response.close()
                    await asyncio.wait_for(closed.wait(), 2)
