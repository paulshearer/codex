import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestServer
from signed_client import provision
from permissive.service import Service
from test_service import FakeRuntime


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_delegation_and_bounded_result(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runtime = FakeRuntime()
            runtime.role = 'controller'
            runtime.binding = {'vmId': '6d206188-1ebf-4e1b-ae79-a9b23f4e6597'}
            runtime.state = {'environmentId': 'mcp-fixture', 'phase': 'ready'}
            runtime.start_monitor = AsyncMock()
            runtime.release.set()
            provision(home)
            service = Service(home, runtime)
            service.store.db.execute('CREATE TABLE vm_attempts (task_id TEXT, phase TEXT)')
            async with TestServer(service.app()) as server:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "permissive",
                    "--home",
                    str(home),
                    "--private-key", str(home / "test-clients/api.pem"),
                    "--url",
                    str(server.make_url("")).rstrip("/"),
                    "mcp",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
                )

                async def call(ident, method, params):
                    process.stdin.write(
                        (
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": ident,
                                    "method": method,
                                    "params": params,
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await process.stdin.drain()
                    response = json.loads(
                        await asyncio.wait_for(process.stdout.readline(), 10)
                    )
                    self.assertNotIn("error", response)
                    return response["result"]

                try:
                    initialized = await call(
                        1, "initialize", {"protocolVersion": "2024-11-05"}
                    )
                    self.assertEqual(
                        initialized["serverInfo"]["name"], "codex-permissive"
                    )
                    tools = await call(2, "tools/list", {})
                    self.assertEqual(
                        [tool["name"] for tool in tools["tools"]],
                        ["submit", "status", "result", "cancel", "pause", "unpause", "continue", "workspaces", "save_workspace"],
                    )
                    saved = await call(20, "tools/call", {"name": "save_workspace", "arguments": {"name": "MCP workspace", "workingDirectory": str(home), "systemPrompt": "MCP workspace context"}})
                    workspace = json.loads(saved["content"][0]["text"])
                    listed = await call(21, "tools/call", {"name": "workspaces", "arguments": {}})
                    self.assertIn(workspace["workspaceId"], [item["workspaceId"] for item in json.loads(listed["content"][0]["text"])["data"]])
                    submitted = await call(
                        3,
                        "tools/call",
                        {
                            "name": "submit",
                            "arguments": {
                                "prompt": "MCP fixture",
                                "workspaceId": workspace["workspaceId"],
                            },
                        },
                    )
                    task = json.loads(submitted["content"][0]["text"])
                    self.assertEqual(task["workspaceId"], workspace["workspaceId"])
                    await asyncio.sleep(0.05)
                    service.store.update(task["taskId"], result="large result " * 20000)
                    result = await call(
                        4,
                        "tools/call",
                        {"name": "result", "arguments": {"taskId": task["taskId"]}},
                    )
                    text = result["content"][0]["text"]
                    self.assertLessEqual(len(text.encode()), 900)
                    self.assertEqual(json.loads(text)["status"], "completed")
                    self.assertGreater(json.loads(text)["nextOffset"], 0)
                    # Guest-controlled metadata must not trap the bounded MCP
                    # projection in a loop once the text page is already empty.
                    service.store.update(task['taskId'], thread_id='\U0001f600' * 256)
                    process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {'taskId': task['taskId']}}}) + '\n').encode())
                    await process.stdin.drain()
                    oversized = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
                    self.assertIn('output budget', oversized['error']['message'])
                    self.assertLess(len(json.dumps(oversized).encode()), 500)
                finally:
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), 10)
