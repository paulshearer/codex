import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestServer
from permissive.service import Service
from permissive.stream_recovery import RECOVERY_PROMPT
from signed_client import SignedTestClient, provision


ERROR = {"message": "stream disconnected before completion: Responses completed event has no completed response",
         "codexErrorInfo": "other", "additionalDetails": None, "misalignment": None}


class RecoveryRuntime:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def run(self, task, store):
        self.calls.append(task)
        await self.release.wait()
        store.update(task["id"], status="completed", result="Recovered answer", error=None)

    async def close(self):
        pass


class StreamRetryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.runtime = RecoveryRuntime()
        self.service = Service(self.home, self.runtime)
        self.client = SignedTestClient(TestServer(self.service.app()), provision(self.home))
        await self.client.start_server()
        self.headers = {"X-Test-Sign": "1"}

    async def asyncTearDown(self):
        self.runtime.release.set()
        await self.client.close()
        self.directory.cleanup()

    def failed_task(self, error=None, **values):
        task = self.service.store.submit({"workingDirectory": str(self.home), "prompt": "ORIGINAL REQUEST", **values})[0]
        self.service.store.update(task["id"], status="failed", thread_id="original-thread", result="Recorded partial progress",
                                  error=json.dumps(ERROR if error is None else error))
        return self.service.store.get(task["id"])

    async def test_signed_retry_keeps_same_task_thread_and_request_and_deduplicates_active_retry(self):
        task = self.failed_task()
        self.assertTrue(task["canRetry"])
        route = "/v1/tasks/" + task["id"] + "/retry"
        response = await self.client.post(route)
        self.assertEqual(response.status, 401)
        response = await self.client.post(route, headers=self.headers)
        self.assertEqual(response.status, 202, await response.text())
        for _ in range(20):
            if self.runtime.calls:
                break
            await asyncio.sleep(.01)
        retried = self.runtime.calls[0]
        self.assertEqual(retried["id"], task["id"])
        self.assertEqual(retried["threadId"], "original-thread")
        self.assertEqual(retried["request"], task["request"])
        self.assertEqual(retried["resumePrompt"], RECOVERY_PROMPT)
        self.assertTrue(retried["streamRecoveryRequested"])
        self.assertNotIn("ORIGINAL REQUEST", retried["resumePrompt"])
        response = await self.client.post(route, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.runtime.calls), 1)
        self.runtime.release.set()
        await asyncio.gather(*self.service.jobs.values())
        saved = self.service.store.get(task["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertFalse(saved["canRetry"])
        self.assertEqual(len(self.service.store.conversation_tasks(task["conversationId"])), 1)
        evidence = [event for event in self.service.store.events(task["id"]) if event["kind"] == "recovery"]
        self.assertEqual(json.loads(evidence[0]["data"]["error"]), ERROR)

    async def test_nonstream_auth_policy_cancelled_and_interrupted_failures_are_not_retryable(self):
        for error in ({"message": ERROR["message"], "codexErrorInfo": "unauthorized"},
                      {"message": "Quota exhausted", "codexErrorInfo": "other"},
                      {"message": "content_filter", "codexErrorInfo": "other"}):
            task = self.failed_task(error)
            self.assertFalse(task["canRetry"])
            response = await self.client.post("/v1/tasks/" + task["id"] + "/retry", headers=self.headers)
            self.assertEqual(response.status, 409)
        for status in ("cancelled", "interrupted", "completed"):
            task = self.failed_task()
            self.service.store.update(task["id"], status=status)
            response = await self.client.post("/v1/tasks/" + task["id"] + "/retry", headers=self.headers)
            self.assertEqual(response.status, 409)
        self.assertEqual(self.runtime.calls, [])

    async def test_retry_rejects_new_input_old_chat_turn_and_busy_native_thread(self):
        task = self.failed_task()
        route = "/v1/tasks/" + task["id"] + "/retry"
        response = await self.client.post(route, json={"prompt": "Changed request"}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.service.thread_owners["original-thread"] = "another-task"
        response = await self.client.post(route, headers=self.headers)
        self.assertEqual(response.status, 409)
        self.service.thread_owners.clear()
        self.service.store.submit({"workingDirectory": str(self.home), "prompt": "Newer task", "resumeTaskId": task["id"]})
        response = await self.client.post(route, headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.runtime.calls, [])

    async def test_held_followup_queue_is_not_released_by_recovery(self):
        task = self.failed_task()
        self.service.store.hold_queue(task["conversationId"])
        response = await self.client.post("/v1/tasks/" + task["id"] + "/retry", headers=self.headers)
        self.assertEqual(response.status, 202)
        self.runtime.release.set()
        await asyncio.gather(*self.service.jobs.values())
        self.assertTrue(self.service.store.conversation(task["conversationId"])["queueHeld"])

    async def test_late_completion_cannot_clear_the_recovery_turn_for_steering(self):
        task = self.failed_task()
        self.service.store.event(task["id"], "agent", {"method": "turn/started", "params": {"turn": {"id": "new-turn"}}})
        self.service.store.event(task["id"], "agent", {"method": "turn/completed", "params": {"turn": {"id": "old-turn"}}})
        self.assertEqual(self.service.store.get(task["id"])["activeTurnId"], "new-turn")
        self.service.store.event(task["id"], "agent", {"method": "turn/completed", "params": {"turn": {"id": "new-turn"}}})
        self.assertIsNone(self.service.store.get(task["id"])["activeTurnId"])
