import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.service import Service
from permissive.store import Store


class FakeRuntime:
    def __init__(self):
        self.active, self.maximum = 0, 0
        self.release = asyncio.Event()
        self.gates, self.paused = {}, set()
        self.finished_activity = set()

    async def run(self, task, store):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            self.gates[task["id"]] = asyncio.Event()
            self.gates[task["id"]].set()
            store.update(
                task["id"], thread_id=task["threadId"] or "thread-" + task["id"]
            )
            await self.release.wait()
            self.finished_activity.add(task["id"])
            if task["id"] in self.paused:
                store.update(task["id"], status="paused")
            await self.gates[task["id"]].wait()
            store.event(
                task["id"],
                "agent",
                {"method": "item/completed", "params": {"text": "done"}},
            )
            store.update(task["id"], status="completed", result="done")
        finally:
            self.active -= 1

    async def interrupt(self, ident, store):
        pass

    async def pause(self, ident, store):
        self.paused.add(ident)
        self.gates[ident].clear()

    async def resume(self, ident, store):
        self.paused.discard(ident)
        self.gates[ident].set()

    async def close(self):
        pass


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_secure_delete_requires_signature_confirmation_and_idle_service(self):
        task = self.service.store.submit({'prompt': 'delete fixture', 'workingDirectory': str(self.home)})[0]
        route = '/v1/chats/' + task['conversationId'] + '/secure-delete'
        body = {'confirm': task['conversationId'], 'passes': 7}
        response = await self.client.post(route, json=body)
        self.assertEqual(response.status, 401)
        response = await self.client.post(route, json={**body, 'passes': 6}, headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post(route, json=body, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.service.store.update(task['id'], status='completed')
        self.service.jobs['other'] = asyncio.create_task(asyncio.sleep(100))
        response = await self.client.post(route, json=body, headers=self.headers)
        self.assertEqual(response.status, 409)
        other = self.service.jobs.pop('other'); other.cancel()
        await asyncio.gather(other, return_exceptions=True)
        response = await self.client.post(route, json=body, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['passes'], 7)
        response = await self.client.get('/v1/chats/' + task['conversationId'], headers=self.headers)
        self.assertEqual(response.status, 404)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.runtime = FakeRuntime()
        self.signer = provision(self.home)
        self.service = Service(self.home, self.runtime)
        self.client = SignedTestClient(TestServer(self.service.app()), self.signer)
        await self.client.start_server()
        self.headers = {"X-Test-Sign": "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def submit(self, **kwargs):
        response = await self.client.post(
            "/v1/tasks",
            json={
                "prompt": "execute fixture",
                "workingDirectory": str(self.home),
                **kwargs,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status, 202, await response.text())
        return await response.json()

    async def test_two_workers_queue_and_cancel_without_replay(self):
        tasks = [await self.submit() for _ in range(3)]
        await asyncio.sleep(0.05)
        self.assertEqual(
            [self.service.store.get(task["id"])["status"] for task in tasks],
            ["running", "running", "queued"],
        )
        response = await self.client.post(
            "/v1/tasks/" + tasks[2]["id"] + "/cancel", headers=self.headers
        )
        self.assertEqual((await response.json())["status"], "cancelled")
        self.runtime.release.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.runtime.maximum, 2)

    async def test_host_storage_stop_records_failure_reason_after_shutdown(self):
        self.runtime.state = {'hostNotice': 'Host storage reserve reached'}
        task = await self.submit()
        await asyncio.sleep(.05)
        job = self.service.jobs[task['id']]
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        saved = self.service.store.get(task['id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['error'], 'Host storage reserve reached')

    async def test_controller_shutdown_does_not_replace_completed_result(self):
        task = await self.submit()
        await asyncio.sleep(.05)
        self.service.store.update(task['id'], status='completed', result='Saved before checkpoint maintenance')
        job = self.service.jobs[task['id']]
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        saved = self.service.store.get(task['id'])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['result'], 'Saved before checkpoint maintenance')

    async def test_authentication_and_browser_origin(self):
        self.assertEqual((await self.client.get("/v1/health")).status, 401)
        self.assertEqual(
            (
                await self.client.get(
                    "/v1/health",
                    headers={**self.headers, "Origin": "https://example.com"},
                )
            ).status,
            403,
        )

    async def test_idempotency_reconnect_and_artifact(self):
        body = {"prompt": "fixture", "workingDirectory": str(self.home)}
        headers = {**self.headers, "Idempotency-Key": "fixture-key"}
        first = await (
            await self.client.post("/v1/tasks", json=body, headers=headers)
        ).json()
        second = await (
            await self.client.post("/v1/tasks", json=body, headers=headers)
        ).json()
        self.assertEqual(first["id"], second["id"])
        changed = await self.client.post(
            "/v1/tasks", json={**body, "prompt": "other"}, headers=headers
        )
        self.assertEqual(changed.status, 400)
        self.runtime.release.set()
        await asyncio.sleep(0.05)
        events = self.service.store.events(first["id"])
        response = await self.client.get(
            "/v1/tasks/" + first["id"] + "/events",
            headers={**self.headers, "Last-Event-ID": str(events[0]["id"])},
        )
        text = await response.text()
        self.assertNotIn("id: " + str(events[0]["id"]) + "\n", text)
        self.assertIn('"status": "completed"', text)
        artifact = self.home / "artifacts" / first["id"] / "fixture.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("artifact-result")
        response = await self.client.get(
            "/v1/tasks/" + first["id"] + "/artifacts/fixture.txt", headers=self.headers
        )
        self.assertEqual(await response.text(), "artifact-result")

    async def test_restart_marks_interrupted(self):
        temporary = self.home / "restart"
        store = Store(temporary)
        task, _ = store.submit({"prompt": "must not replay"})
        store.update(task["id"], status="running", thread_id="saved-thread")
        store.close()
        restored = Store(temporary)
        self.assertEqual(restored.get(task["id"])["status"], "interrupted")
        self.assertEqual(restored.get(task["id"])["threadId"], "saved-thread")
        restored.close()

    async def test_continuation_keeps_thread_and_prevents_concurrent_writers(self):
        self.runtime.release.set()
        original = await self.submit()
        await asyncio.sleep(0.05)
        original = self.service.store.get(original["id"])
        self.runtime.release.clear()
        route = "/v1/tasks/" + original["id"] + "/continue"
        headers = {**self.headers, "Idempotency-Key": "continuation-fixture"}
        response = await self.client.post(
            route, json={"prompt": "next"}, headers=headers
        )
        continued = await response.json()
        self.assertEqual(
            (response.status, continued["threadId"]), (202, original["threadId"])
        )
        repeated = await self.client.post(
            route, json={"prompt": "next"}, headers=headers
        )
        self.assertEqual(
            (repeated.status, (await repeated.json())["id"]), (200, continued["id"])
        )
        collision = await self.client.post(
            route, json={"prompt": "parallel writer"}, headers=self.headers
        )
        self.assertEqual(collision.status, 400)
        cancelled = await self.client.post(
            "/v1/tasks/" + continued["id"] + "/cancel", headers=self.headers
        )
        self.assertEqual((await cancelled.json())["status"], "cancelled")
        retry = await self.client.post(
            route, json={"prompt": "retry after cancellation"}, headers=self.headers
        )
        self.assertEqual(retry.status, 202)

    async def test_model_and_directory_not_overridable(self):
        response = await self.client.post(
            "/v1/tasks",
            json={
                "prompt": "fixture",
                "workingDirectory": str(self.home),
                "model": "other",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/v1/tasks",
            json={"prompt": "fixture", "workingDirectory": "relative"},
            headers=self.headers,
        )
        self.assertEqual(response.status, 400)

    async def test_pause_finishes_activity_and_resume_retains_task_and_thread(self):
        task = await self.submit()
        await asyncio.sleep(0.05)
        task = self.service.store.get(task["id"])
        route = "/v1/tasks/" + task["id"]
        pause = await self.client.post(route + "/pause", headers=self.headers)
        self.assertEqual((await pause.json())["status"], "pausing")
        self.assertNotIn(task["id"], self.runtime.finished_activity)
        self.runtime.release.set()
        await asyncio.sleep(0.05)
        self.assertIn(task["id"], self.runtime.finished_activity)
        self.assertEqual(self.service.store.get(task["id"])["status"], "paused")
        await asyncio.sleep(0.05)
        self.assertEqual(self.service.store.get(task["id"])["status"], "paused")
        conflict = await self.client.post(route + "/continue", json={"prompt": "competing"}, headers=self.headers)
        self.assertEqual(conflict.status, 400)
        resumed = await self.client.post(route + "/resume", headers=self.headers)
        self.assertEqual((await resumed.json())["id"], task["id"])
        await asyncio.sleep(0.05)
        actual = self.service.store.get(task["id"])
        self.assertEqual((actual["status"], actual["threadId"]), ("completed", task["threadId"]))

    async def test_queued_pause_prevents_start_and_can_cancel(self):
        tasks = [await self.submit() for _ in range(3)]
        await asyncio.sleep(0.05)
        route = "/v1/tasks/" + tasks[2]["id"]
        response = await self.client.post(route + "/pause", headers=self.headers)
        self.assertEqual((await response.json())["status"], "paused")
        self.runtime.release.set()
        await asyncio.sleep(0.05)
        self.assertNotIn(tasks[2]["id"], self.runtime.finished_activity)
        response = await self.client.post(route + "/cancel", headers=self.headers)
        self.assertEqual((await response.json())["status"], "cancelled")

    async def test_cancel_survives_failed_interrupt_and_is_idempotent(self):
        task = await self.submit()
        await asyncio.sleep(0.05)
        async def broken_interrupt(ident, store):
            raise RuntimeError("Broken native pipe")
        self.runtime.interrupt = broken_interrupt
        route = "/v1/tasks/" + task["id"] + "/cancel"
        responses = await asyncio.gather(*(self.client.post(route, headers=self.headers) for _ in range(2)))
        self.assertEqual([ (await response.json())["status"] for response in responses], ["cancelled", "cancelled"])
        self.assertNotIn(task["id"], self.service.jobs)

    async def test_client_disconnect_does_not_abandon_cancellation(self):
        task = await self.submit()
        await asyncio.sleep(0.05)
        interrupting, release = asyncio.Event(), asyncio.Event()
        async def slow_interrupt(ident, store):
            interrupting.set()
            await release.wait()
        self.runtime.interrupt = slow_interrupt
        request = asyncio.create_task(self.service.cancel(SimpleNamespace(match_info={"id": task["id"]})))
        await interrupting.wait()
        self.assertEqual(self.service.store.get(task["id"])["status"], "cancelling")
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        release.set()
        await self.service.cancellations[task["id"]]
        self.assertEqual(self.service.store.get(task["id"])["status"], "cancelled")
        self.assertNotIn(task["id"], self.service.jobs)

    async def test_paused_queue_resume_and_restart_do_not_replay_automatically(self):
        tasks = [await self.submit() for _ in range(3)]
        await asyncio.sleep(0.05)
        route = "/v1/tasks/" + tasks[2]["id"]
        await self.client.post(route + "/pause", headers=self.headers)
        self.runtime.release.set()
        await asyncio.sleep(0.05)
        await self.client.post(route + "/resume", headers=self.headers)
        await asyncio.sleep(0.05)
        self.assertEqual(self.service.store.get(tasks[2]["id"])["status"], "completed")
        store = Store(self.home / "paused-restart")
        paused, _ = store.submit({"prompt": "do not auto-replay"})
        store.update(paused["id"], status="paused", thread_id="saved-thread")
        store.close()
        restored = Store(self.home / "paused-restart")
        self.assertEqual(restored.get(paused["id"])["status"], "paused")
        restored.close()
