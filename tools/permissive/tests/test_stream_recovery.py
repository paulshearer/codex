import asyncio
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from permissive.runtime import Runtime
from permissive.store import Store
from permissive.stream_recovery import (
    MAX_STREAM_RECOVERY_ATTEMPTS, RECOVERY_PROMPT,
    complete_with_stream_recovery, is_recoverable_stream_error,
)


ERROR = {"message": "stream disconnected before completion: Responses completed event has no completed response",
         "codexErrorInfo": "other", "additionalDetails": None}


def failed(ident="failed-turn", error=None):
    return {"id": ident, "status": "failed", "error": copy.deepcopy(ERROR if error is None else error)}


class ClassificationTests(unittest.TestCase):
    def test_exact_owner_error_and_real_premature_responses_terminations(self):
        self.assertTrue(is_recoverable_stream_error(ERROR))
        self.assertTrue(is_recoverable_stream_error(json.dumps(ERROR)))
        for message in ("stream closed before response.completed", "Responses stream ended before a terminal event"):
            for classification in ("other", {"responseStreamDisconnected": {"httpStatusCode": None}},
                                   {"responseStreamConnectionFailed": {"httpStatusCode": 502}}):
                with self.subTest(message=message, classification=classification):
                    self.assertTrue(is_recoverable_stream_error({"message": message, "codexErrorInfo": classification}))

    def test_hard_failures_and_unknown_error_shapes_are_excluded(self):
        for code in (None, "unauthorized", "rateLimitExceeded", "usageLimitExceeded", "contextWindowExceeded",
                     "cyberPolicy", "badRequest", "sandboxError", {"responseStreamDisconnected": {"httpStatusCode": 401}},
                     {"responseStreamDisconnected": {"httpStatusCode": 429}},
                     {"responseStreamDisconnected": {"httpStatusCode": True}}):
            with self.subTest(code=code):
                self.assertFalse(is_recoverable_stream_error({**ERROR, "codexErrorInfo": code}))
        for message in ("Task cancelled", "stream disconnected before completion: Incomplete response returned, reason: content_filter",
                        "Provider transport failed: ValueError", "invalid tool arguments", "quota exceeded",
                        ERROR["message"] + "; unauthorized", "connection reset by peer"):
            with self.subTest(message=message):
                self.assertFalse(is_recoverable_stream_error({**ERROR, "message": message}))
        for error in (None, [], {}, ERROR["message"], "not json", json.dumps(ERROR["message"]), {"message": 5, "codexErrorInfo": "other"}):
            with self.subTest(error=error):
                self.assertFalse(is_recoverable_stream_error(error))


class RecoveryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_bounded_continuations_and_success_events(self):
        turns = iter([failed("one"), failed("two"), {"id": "three", "status": "completed"}])
        prompts, delays, events = [], [], []
        async def wait_turn(): return next(turns)
        async def start(prompt): prompts.append(prompt)
        async def delay(seconds): delays.append(seconds)
        result = await complete_with_stream_recovery(wait_turn, start, delay, events.append)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(prompts, [RECOVERY_PROMPT, RECOVERY_PROMPT])
        self.assertEqual(delays, [1, 2])
        self.assertEqual([event["phase"] for event in events], ["retrying", "retrying", "recovered"])
        self.assertTrue(all(event["maxAttempts"] == MAX_STREAM_RECOVERY_ATTEMPTS for event in events))

    async def test_exhaustion_returns_real_final_error_without_fabricated_success(self):
        turns = iter([failed("one"), failed("two"), failed("three")])
        events, starts = [], []
        async def wait_turn(): return next(turns)
        async def start(prompt): starts.append(prompt)
        async def delay(seconds): pass
        result = await complete_with_stream_recovery(wait_turn, start, delay, events.append)
        self.assertEqual(result, failed("three"))
        self.assertEqual(len(starts), 2)
        self.assertEqual(events[-1]["phase"], "exhausted")
        self.assertEqual(events[-1]["attempt"], 2)

    async def test_other_failures_completed_and_interrupted_turns_do_not_retry(self):
        for turn in (failed(error={"message": "unauthorized", "codexErrorInfo": "unauthorized"}),
                     {"id": "turn", "status": "completed"}, {"id": "turn", "status": "interrupted", "error": ERROR}):
            events = []
            async def wait_turn(): return turn
            async def forbidden(*args): self.fail("This turn must not retry")
            self.assertEqual(await complete_with_stream_recovery(wait_turn, forbidden, forbidden, events.append), turn)
            self.assertEqual(events, [])

    async def test_cancellation_during_backoff_cannot_start_another_turn(self):
        waiting = asyncio.Event()
        starts = []
        async def wait_turn(): return failed()
        async def start(prompt): starts.append(prompt)
        async def delay(seconds):
            waiting.set()
            await asyncio.Event().wait()
        operation = asyncio.create_task(complete_with_stream_recovery(wait_turn, start, delay, lambda event: None))
        await asyncio.wait_for(waiting.wait(), 1)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError): await operation
        self.assertEqual(starts, [])


class FakeFacade:
    def __init__(self, *args, **kwargs):
        self.base_url = "http://127.0.0.1:1/v1"
        self.token = "unused-test-token"
    def start(self, **kwargs): pass
    def stop(self): pass


class FakeProcess:
    pid = 100
    def __init__(self): self.exited = threading.Event()
    def wait(self):
        self.exited.wait()
        return 0
    def poll(self): return 0 if self.exited.is_set() else None
    def terminate(self): self.exited.set()


class RuntimeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.store = Store(self.home)
        self.task = self.store.submit({"prompt": "Original instruction", "workingDirectory": str(self.home),
                                      "browserMode": "dedicated", "attachments": ["original-attachment"]})[0]
        self.store.update(self.task["id"], status="running")
        self.runtime = Runtime(self.home, self.home / "native.exe", "http://127.0.0.1:1/v1", ["unused"])
        self.backends, self.native_inputs = [], []
        self.turns = [failed("turn-1"), {"id": "turn-2", "status": "completed"}]
        self.first_failure = None
        self.duplicate_old_completion = False
        self.tool_effects = 0
        owner = self

        class FakeBackend:
            def __init__(self, router, label, model):
                self.router, self.label, self.model = router, label, model
                self.proc = FakeProcess()
                self.calls, self.futures, self.finished, self.closed = [], [], 0, False
                owner.backends.append(self)
            async def start(self):
                self.hook = self.router.overrides(self.model)["hooks.PreToolUse"][0]["hooks"][0]["command"]
            async def call(self, method, params):
                self.calls.append((method, copy.deepcopy(params)))
                if method == "hooks/list":
                    return {"data": [{"hooks": [{"source": "sessionFlags", "command": self.hook,
                        "eventName": name, "key": name, "currentHash": "fixture"} for name in ("preToolUse", "postToolUse")]}]}
                if method in {"thread/start", "thread/resume"}:
                    return {"thread": {"id": "saved-native-thread"}}
                if method == "thread/goal/get": return {"goal": {"status": "complete"}}
                if method == "turn/start":
                    self.finished += 1
                    self.futures.append(owner.runtime.workers[self.label][2])
                    if self.finished > len(owner.turns): raise AssertionError("Unbounded native retry")
                    turn = copy.deepcopy(owner.turns[self.finished - 1])
                    def emit(name, value):
                        message = {"method": name, "params": {"threadId": "saved-native-thread", **value}}
                        self.router.observe(self, message)
                        self.router.emit(message)
                    if self.finished > 1 and owner.duplicate_old_completion:
                        emit("turn/completed", {"turn": copy.deepcopy(owner.turns[self.finished - 2])})
                    emit("turn/started", {"turn": {"id": turn["id"], "status": "inProgress"}})
                    if self.finished == 1:
                        owner.tool_effects += 1
                        emit("item/completed", {"item": {"type": "commandExecution", "id": "effect-once", "status": "completed"}})
                        emit("item/completed", {"item": {"type": "agentMessage", "text": "Recorded partial progress"}})
                    elif turn["status"] == "completed":
                        emit("item/completed", {"item": {"type": "agentMessage", "text": "Finished remaining work"}})
                    emit("turn/completed", {"turn": turn})
                    if self.finished == 1 and owner.first_failure:
                        owner.first_failure()
                    return {"turn": turn}
                return {}
            async def close(self):
                self.closed = True
                self.proc.exited.set()

        def native_input(home, prompt, attachments):
            owner.native_inputs.append((prompt, list(attachments)))
            return [{"type": "text", "text": prompt}]
        self.patches = [patch("permissive.runtime.Backend", FakeBackend),
                        patch("permissive.runtime.Facade", FakeFacade),
                        patch("permissive.runtime.ProcessTree"),
                        patch("permissive.runtime.native_input", native_input),
                        patch("permissive.stream_recovery.STREAM_RECOVERY_DELAYS", (0, 0))]
        for active_patch in self.patches: active_patch.start()

    async def asyncTearDown(self):
        for active_patch in reversed(self.patches): active_patch.stop()
        self.store.close()
        self.temporary.cleanup()

    def recovery_events(self):
        return [event["data"] for event in self.store.events(self.task["id"]) if event["kind"] == "recovery"]

    async def test_same_task_backend_thread_and_single_completed_tool_effect(self):
        await asyncio.wait_for(self.runtime.run(self.store.get(self.task["id"]), self.store), 2)
        task = self.store.get(self.task["id"])
        self.assertEqual(task["id"], self.task["id"])
        self.assertEqual(task["threadId"], "saved-native-thread")
        self.assertEqual(task["status"], "completed")
        self.assertIsNone(task["error"])
        self.assertEqual(self.tool_effects, 1)
        self.assertEqual(len(self.backends), 1)
        calls = self.backends[0].calls
        self.assertEqual([name for name, params in calls].count("thread/start"), 1)
        self.assertNotIn("thread/resume", [name for name, params in calls])
        self.assertEqual([params["threadId"] for name, params in calls if name == "turn/start"], ["saved-native-thread"] * 2)
        self.assertEqual(self.native_inputs, [("Original instruction", ["original-attachment"]), (RECOVERY_PROMPT, [])])
        self.assertIn("Recorded partial progress", task["result"])
        self.assertIn("Finished remaining work", task["result"])
        self.assertEqual([event["phase"] for event in self.recovery_events()], ["retrying", "recovered"])
        self.assertTrue(self.backends[0].closed)
        self.assertEqual(self.runtime.workers, {})

    async def test_retry_replaces_worker_future_and_ignores_duplicate_old_completion(self):
        self.duplicate_old_completion = True
        await asyncio.wait_for(self.runtime.run(self.store.get(self.task["id"]), self.store), 2)
        task = self.store.get(self.task["id"])
        self.assertEqual(task["status"], "completed")
        first, replacement = self.backends[0].futures
        self.assertIsNot(first, replacement)
        self.assertEqual(first.result()["id"], "turn-1")
        self.assertEqual(replacement.result()["id"], "turn-2")
        self.assertEqual(self.backends[0].finished, 2)
        self.assertEqual(self.tool_effects, 1)

    async def test_runtime_hard_error_is_not_retried(self):
        hard_error = {"message": "Unauthorized", "codexErrorInfo": "unauthorized"}
        self.turns = [failed("hard-error", hard_error)]
        await asyncio.wait_for(self.runtime.run(self.store.get(self.task["id"]), self.store), 2)
        task = self.store.get(self.task["id"])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(json.loads(task["error"]), hard_error)
        self.assertEqual(self.backends[0].finished, 1)
        self.assertEqual(self.recovery_events(), [])

    async def test_runtime_bounds_failures_and_preserves_real_error(self):
        self.turns = [failed("one"), failed("two"), failed("three")]
        await asyncio.wait_for(self.runtime.run(self.store.get(self.task["id"]), self.store), 2)
        task = self.store.get(self.task["id"])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(json.loads(task["error"]), ERROR)
        self.assertEqual(self.backends[0].finished, 3)
        self.assertEqual(self.tool_effects, 1)
        self.assertEqual(self.recovery_events()[-1]["phase"], "exhausted")
        failures = [event for event in self.store.events(task["id"]) if event["kind"] == "agent"
                    and event["data"].get("method") == "turn/completed"]
        self.assertEqual(len(failures), 3)

    async def test_manual_retry_uses_saved_thread_and_omits_original_attachments(self):
        self.store.update(self.task["id"], thread_id="saved-native-thread", result="Earlier attempt evidence")
        task = {**self.store.get(self.task["id"]), "resumePrompt": RECOVERY_PROMPT, "streamRecoveryRequested": True}
        self.turns = [{"id": "manual-retry", "status": "completed"}]
        await asyncio.wait_for(self.runtime.run(task, self.store), 2)
        self.assertEqual(self.native_inputs, [(RECOVERY_PROMPT, [])])
        self.assertEqual([name for name, params in self.backends[0].calls].count("thread/resume"), 1)
        self.assertIn("Earlier attempt evidence", self.store.get(task["id"])["result"])

    async def test_pause_during_recovery_blocks_new_turn_until_resumed(self):
        ident = self.task["id"]
        def pause_at_failure():
            self.store.update(ident, status="pausing")
            self.runtime.controls[ident].pause()
        self.first_failure = pause_at_failure
        operation = asyncio.create_task(self.runtime.run(self.store.get(ident), self.store))
        for _ in range(100):
            if self.store.get(ident)["status"] == "paused": break
            await asyncio.sleep(.01)
        self.assertEqual(self.store.get(ident)["status"], "paused")
        self.assertEqual(self.backends[0].finished, 1)
        await self.runtime.resume(ident, self.store)
        self.store.update(ident, status="running")
        await asyncio.wait_for(operation, 2)
        self.assertEqual(self.store.get(ident)["status"], "completed")

    async def test_cancel_during_backoff_does_not_retry_or_repeat_tool_effect(self):
        ident = self.task["id"]
        with patch("permissive.stream_recovery.STREAM_RECOVERY_DELAYS", (30, 30)):
            operation = asyncio.create_task(self.runtime.run(self.store.get(ident), self.store))
            for _ in range(100):
                if self.recovery_events(): break
                await asyncio.sleep(.01)
            self.assertEqual(self.recovery_events()[0]["phase"], "retrying")
            self.store.update(ident, status="cancelling")
            await self.runtime.interrupt(ident, self.store)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError): await operation
        self.assertEqual(self.backends[0].finished, 1)
        self.assertEqual(self.tool_effects, 1)
        self.assertTrue(self.backends[0].closed)
        self.assertEqual(self.runtime.workers, {})


if __name__ == "__main__":
    unittest.main()
