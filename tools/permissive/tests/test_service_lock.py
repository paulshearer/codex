import asyncio
import os
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest

from aiohttp import ClientSession
from signed_client import provision
from permissive.client import SignedSession


class ServiceLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_launcher_cannot_reopen_active_service_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            provision(home)
            configured = home / 'guest-deployment.json'
            configured.write_text(json.dumps({'role': 'guest'}))
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            command = [
                sys.executable,
                "-m",
                "permissive",
                "--home",
                str(home),
                "serve",
                '--role', 'guest', '--deployment', str(configured),
                "--native",
                "unused-fixture.exe",
                "--port",
                str(port),
            ]
            environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
            first = await asyncio.create_subprocess_exec(
                *command,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            url = f"http://127.0.0.1:{port}"
            async with SignedSession(home=home, private_key=home / "test-clients/api.pem") as client:
                try:
                    for _ in range(100):
                        try:
                            response = await client.get(url + "/v1/health")
                            if response.status == 200:
                                await response.read()
                                break
                        except OSError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("First service did not start")
                    second = await asyncio.create_subprocess_exec(
                        *command,
                        env=environment,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(second.communicate(), 10)
                    self.assertNotEqual(second.returncode, 0)
                    self.assertEqual((await client.get(url + "/v1/health")).status, 200)
                finally:
                    await client.post(url + "/v1/service/shutdown")
                    await asyncio.wait_for(first.communicate(), 10)
                    self.assertEqual(first.returncode, 0)
