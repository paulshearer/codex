import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.service import Service
from test_service import FakeRuntime


class BrowserBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_selected_profile_explains_setup_without_routing_to_another_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            signer = provision(Path(directory), "browser")
            service = Service(Path(directory), FakeRuntime())
            async with SignedTestClient(TestServer(service.app()), signer) as client:
                headers = {'X-Test-Sign': '1'}
                other = await client.ws_connect('/v1/browser/extension', headers=headers)
                try:
                    await other.send_json({'operation': 'hello', 'browser': 'msedge', 'profileId': 'other'})
                    await asyncio.sleep(.02)
                    response = await client.post('/v1/browser/command', json={
                        'taskId': 'one', 'operation': 'list', 'browser': 'msedge', 'profileId': 'selected'}, headers=headers)
                    self.assertEqual(response.status, 409)
                    message = await response.text()
                    self.assertIn('selected browser profile is not connected', message)
                    self.assertIn('Enable and connect', message)
                    self.assertFalse(service.browser.pending)
                finally:
                    await other.close()

    async def test_ambiguous_browser_selection_is_distinguished_from_disconnection(self):
        with tempfile.TemporaryDirectory() as directory:
            signer = provision(Path(directory), "browser")
            service = Service(Path(directory), FakeRuntime())
            async with SignedTestClient(TestServer(service.app()), signer) as client:
                headers = {'X-Test-Sign': '1'}
                sockets = [await client.ws_connect('/v1/browser/extension', headers=headers) for _ in range(2)]
                try:
                    for index, socket in enumerate(sockets):
                        await socket.send_json({'operation': 'hello', 'browser': 'msedge', 'profileId': f'profile-{index}'})
                    await asyncio.sleep(.02)
                    response = await client.post('/v1/browser/command', json={
                        'taskId': 'one', 'operation': 'list', 'browser': 'msedge'}, headers=headers)
                    self.assertEqual(response.status, 409)
                    self.assertIn('Multiple connections match', await response.text())
                    self.assertFalse(service.browser.pending)
                finally:
                    for socket in sockets:
                        await socket.close()

    async def test_disconnect_during_command_returns_a_clear_connection_error(self):
        with tempfile.TemporaryDirectory() as directory:
            signer = provision(Path(directory), "browser")
            service = Service(Path(directory), FakeRuntime())
            async with SignedTestClient(TestServer(service.app()), signer) as client:
                headers = {'X-Test-Sign': '1'}
                socket = await client.ws_connect('/v1/browser/extension', headers=headers)
                request = asyncio.create_task(client.post('/v1/browser/command', json={'taskId': 'one', 'operation': 'inspect', 'tabId': 72}, headers=headers))
                await socket.receive_json()
                await socket.close()
                response = await request
                self.assertEqual(response.status, 409)
                self.assertIn('disconnected', await response.text())

    async def test_open_creates_owned_tab_without_requiring_an_existing_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            signer = provision(Path(directory), "browser")
            service = Service(Path(directory), FakeRuntime())
            async with SignedTestClient(TestServer(service.app()), signer) as client:
                headers = {"X-Test-Sign": "1"}
                socket = await client.ws_connect('/v1/browser/extension', headers=headers)
                async def extension():
                    async for message in socket:
                        value = message.json()
                        await socket.send_json({'id': value['id'], 'result': {'tabId': 72, 'url': 'https://example.com'}})
                worker = asyncio.create_task(extension())
                try:
                    body = {'taskId': 'one', 'operation': 'open', 'url': 'https://example.com'}
                    response = await client.post('/v1/browser/command', json=body, headers=headers)
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())['result']['tabId'], 72)
                    response = await client.post('/v1/browser/command', json={'taskId': 'two', 'operation': 'inspect', 'tabId': 72}, headers=headers)
                    self.assertEqual(response.status, 409)
                    await service.browser.release_task('one')
                    response = await client.post('/v1/browser/command', json={'taskId': 'two', 'operation': 'inspect', 'tabId': 72}, headers=headers)
                    self.assertEqual(response.status, 200)
                finally:
                    await socket.close()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

    async def test_profile_selection_reconnects_by_identity_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            signer = provision(Path(directory), "browser")
            service = Service(Path(directory), FakeRuntime())
            async with SignedTestClient(TestServer(service.app()), signer) as client:
                headers = {"X-Test-Sign": "1"}
                chrome = await client.ws_connect("/v1/browser/extension", headers=headers)
                edge = await client.ws_connect("/v1/browser/extension", headers=headers)
                await chrome.send_json({"operation": "hello", "browser": "chrome", "profileId": "personal", "profileName": "Personal Chrome"})
                local_profile = 'local-' + 'a' * 24
                await edge.send_json({"operation": "hello", "browser": "msedge", "profileId": "work", "localProfileId": local_profile, "profileName": "Work Edge"})
                await asyncio.sleep(.02)
                self.assertEqual({item["profileName"] for item in service.browser.catalog()}, {"Personal Chrome", "Work Edge"})

                async def reply(socket):
                    async for message in socket:
                        value = message.json()
                        await socket.send_json({"id": value["id"], "result": {"profile": "work"}})

                worker = asyncio.create_task(reply(edge))
                body = {"taskId": "one", "operation": "list", "browser": "msedge", "profileId": local_profile}
                try:
                    response = await client.post("/v1/browser/command", json=body, headers=headers)
                    self.assertEqual(response.status, 200)
                    old_connection = (await response.json())["connectionId"]
                    response = await client.post("/v1/browser/command", json={**body, "connectionId": next(item["connectionId"] for item in service.browser.catalog() if item["browser"] == "chrome")}, headers=headers)
                    self.assertEqual(response.status, 409)
                    await edge.close()
                    await worker
                    response = await client.post("/v1/browser/command", json=body, headers=headers)
                    self.assertEqual(response.status, 409)
                    edge = await client.ws_connect("/v1/browser/extension", headers=headers)
                    await edge.send_json({"operation": "hello", "browser": "msedge", "profileId": "work", "localProfileId": local_profile, "profileName": "Work Edge"})
                    await asyncio.sleep(.02)
                    worker = asyncio.create_task(reply(edge))
                    response = await client.post("/v1/browser/command", json=body, headers=headers)
                    self.assertEqual(response.status, 200)
                    self.assertNotEqual((await response.json())["connectionId"], old_connection)
                finally:
                    await chrome.close()
                    await edge.close()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

    async def test_attached_tab_ownership_release_and_connection_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            signer = provision(Path(directory), "browser")
            service = Service(Path(directory), FakeRuntime())
            async with SignedTestClient(TestServer(service.app()), signer) as client:
                headers = {"X-Test-Sign": "1"}
                socket = await client.ws_connect(
                    "/v1/browser/extension", headers=headers
                )

                async def extension():
                    async for message in socket:
                        value = message.json()
                        await socket.send_json(
                            {"id": value["id"], "result": {"ok": True}}
                        )

                worker = asyncio.create_task(extension())
                try:
                    body = {"taskId": "one", "operation": "inspect", "tabId": 17}
                    response = await client.post(
                        "/v1/browser/command", json=body, headers=headers
                    )
                    self.assertEqual(response.status, 200)
                    response = await client.post(
                        "/v1/browser/command",
                        json={**body, "taskId": "two"},
                        headers=headers,
                    )
                    self.assertEqual(response.status, 409)
                    await service.browser.release_task("one")
                    response = await client.post(
                        "/v1/browser/command",
                        json={**body, "taskId": "two"},
                        headers=headers,
                    )
                    self.assertEqual(response.status, 200)
                    response = await client.get("/v1/health", headers=headers)
                    self.assertEqual(response.status, 401)
                finally:
                    await socket.close()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
