"""Controller invariants tested with a guest transport, never a real VM."""
import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from aiohttp.test_utils import TestServer

from permissive.attachments import Attachments
from permissive.service import Service
from permissive.store import Store
from permissive.vm_runtime import HyperVTransport, VMRuntime
from signed_client import SignedTestClient, provision


class GuestTransport:
    def __init__(self):
        self.calls = []
        self.lifecycle_calls = []
        self.workspaces = {}
        self.attachments = {}
        self.tasks = {}
        self.admissions = {}
        self.fail_lifecycle = set()
        self.artifact = b"guest-generated\x00evidence"
        self.artifacts_enabled = False
        self.rpc_calls = []
        self.staged = {}
        self.lose_next_admission_response = False
        self.fail_checkpoint_after = None

    async def lifecycle(self, operation):
        self.lifecycle_calls.append(operation)
        if operation == "checkpoint" and self.fail_checkpoint_after is not None and self.lifecycle_calls.count("checkpoint") >= self.fail_checkpoint_after:
            raise RuntimeError("Checkpoint maintenance failed")
        if operation in self.fail_lifecycle:
            raise RuntimeError("Hyper-V did not confirm " + operation)
        return {"ok": True, "State": "Paused" if operation == "pause" else "Off" if operation == "stop" else "Running"}

    async def rpc(self, request):
        self.rpc_calls.append(dict(request))
        if request["operation"] == "prepare_directory":
            return {"ready": True, "path": request["path"]}
        if request["operation"] == "stage_file":
            data = base64.b64decode(request["dataBase64"], validate=True)
            existing = self.staged.setdefault(request["path"], bytearray())
            self.assert_chunk(existing, request["offset"], data)
            return {"nextOffset": request["offset"] + len(data), "complete": request["complete"]}
        if request["operation"] == "runner_restart":
            return {"triggered": True, "ready": False}
        raise AssertionError("Unexpected guest control RPC")

    @staticmethod
    def assert_chunk(existing, offset, data):
        if offset != len(existing):
            raise AssertionError("Unexpected staged chunk offset")
        existing.extend(data)

    async def request(self, method, path, body=None, **options):
        self.calls.append((method, path, body, options))
        if path == "/v1/health":
            return {"ok": True, "model": "/models/deepseek-v4-flash", "executionRole": "guest"}
        if path == "/v1/workspaces" and method == "POST":
            ident = "workspace-" + str(len(self.workspaces) + 1)
            self.workspaces[ident] = dict(body)
            return {"id": ident, **body}
        if path.startswith("/v1/workspaces/") and method == "PATCH":
            ident = path.rsplit("/", 1)[1]
            self.workspaces[ident].update(body)
            return {"id": ident, **self.workspaces[ident]}
        if path == "/v1/attachments" and method == "POST":
            ident = "attachment-" + str(len(self.attachments) + 1)
            self.attachments[ident] = {**body, "bytes": base64.b64decode(body["data"], validate=True)}
            return {"id": ident}
        if path == "/v1/tasks" and method == "POST":
            admission = body.get("submissionId") or options.get("idempotencyKey")
            if admission in self.admissions:
                return dict(self.tasks[self.admissions[admission]])
            for ident in body.get("attachments", []):
                if self.attachments[ident]["workspaceId"] != body["workspaceId"]:
                    raise RuntimeError("Attachment belongs to a different workspace")
            if body.get("resumeTaskId"):
                if self.tasks[body["resumeTaskId"]]["workspaceId"] != body["workspaceId"]:
                    raise RuntimeError("Continuation changed guest workspace")
            ident = "guest-task-" + str(len(self.tasks) + 1)
            task = {"id": ident, "status": "completed", "workspaceId": body["workspaceId"],
                    "threadId": "native-guest-thread", "result": "GUEST-VERIFIED", "error": None,
                    "artifacts": [{"id": "evidence.bin", "size": len(self.artifact)}] if self.artifacts_enabled else []}
            self.tasks[ident] = task
            if admission:
                self.admissions[admission] = ident
            if self.lose_next_admission_response:
                self.lose_next_admission_response = False
                raise TimeoutError("Lost guest admission response")
            return dict(task)
        if path.endswith("/events"):
            events = [{"id": 11, "kind": "agent", "data": {"method": "item/completed", "params": {
                "item": {"type": "commandExecution", "id": "guest-command", "command": "guest-only-command", "exitCode": 0}}}, "createdAt": 100}]
            after = options.get("after", 0)
            return {"data": [event for event in events if event["id"] > after], "nextCursor": 11, "hasMore": False}
        if "/artifacts/" in path:
            offset = options.get("offset", 0)
            chunk = self.artifact[offset:offset + 3]
            end = offset + len(chunk)
            return {"data": base64.b64encode(chunk).decode(), "offset": offset, "length": len(chunk),
                    "size": len(self.artifact), "eof": end >= len(self.artifact),
                    "nextOffset": None if end >= len(self.artifact) else end}
        if path.startswith("/v1/tasks/"):
            ident = path.split("/")[3]
            if method == "GET":
                return dict(self.tasks[ident])
            return {"id": ident, "status": "paused" if path.endswith("/pause") else "running"}
        if path == "/v1/browsers":
            return {"data": [{"id": "guest-browser", "profiles": ["guest-profile"]}]}
        if path in {"/v1/browsers/connect", "/v1/browsers/setup"}:
            return {"guest": True, **body}
        raise AssertionError("Unexpected guest RPC: " + method + " " + path)


class HostHelperBudgetTests(unittest.IsolatedAsyncioTestCase):
    class Stream:
        def __init__(self, payload=b""):
            self.payload = payload

        async def read(self, count):
            result, self.payload = self.payload, b""
            return result

    class Process:
        pid = 12345

        def __init__(self, output=b'{"ok":true}'):
            self.stdout = HostHelperBudgetTests.Stream(output)
            self.stderr = HostHelperBudgetTests.Stream()
            self.returncode = None
            self.killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    async def test_host_helper_has_memory_job_before_reading_and_closes_it(self):
        process = self.Process()
        tree = Mock()
        transport = HyperVTransport.__new__(HyperVTransport)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch("permissive.vm_runtime.ProcessTree", return_value=tree) as owner:
            self.assertEqual(await transport._process("-File", "fixed.ps1"), {"ok": True})
        owner.assert_called_once_with(process.pid, memory_limit=1024**3)
        tree.close.assert_called_once_with()

    async def test_failed_host_memory_job_assignment_kills_the_helper(self):
        process = self.Process()
        transport = HyperVTransport.__new__(HyperVTransport)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch("permissive.vm_runtime.ProcessTree", side_effect=OSError("job assignment denied")):
            with self.assertRaises(OSError):
                await transport._process("-File", "fixed.ps1")
        self.assertTrue(process.killed)

    async def test_oversized_host_helper_output_closes_memory_job(self):
        class Oversized(bytes):
            def __len__(self):
                return 64*1024**2 + 1
        process = self.Process(Oversized(b"x"))
        tree = Mock()
        transport = HyperVTransport.__new__(HyperVTransport)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch("permissive.vm_runtime.ProcessTree", return_value=tree):
            with self.assertRaisesRegex(RuntimeError, "transport budget"):
                await transport._process("-File", "fixed.ps1")
        tree.close.assert_called_once_with()

    async def test_cancelled_host_helper_is_killed_and_job_is_closed(self):
        process = self.Process()
        waiting = asyncio.Event()
        forever = asyncio.Event()
        async def wait():
            waiting.set()
            if not process.killed:
                await forever.wait()
            return process.returncode
        process.wait = wait
        tree = Mock()
        transport = HyperVTransport.__new__(HyperVTransport)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch("permissive.vm_runtime.ProcessTree", return_value=tree):
            task = asyncio.create_task(transport._process("-File", "fixed.ps1"))
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        tree.close.assert_called_once_with()


class VMRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disk_usage = patch('permissive.vm_runtime.shutil.disk_usage', return_value=Mock(free=2 * 1024**4))
        disk_usage.start()
        self.addCleanup(disk_usage.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.binding = self.home / "deployment.json"
        self.binding.write_text(json.dumps({"role": "controller", "vmId": str(uuid.uuid4()),
                                            "sandboxId": str(uuid.uuid4()), "guestCredentialPath": "guest-secret.dpapi"}))
        self.transport = GuestTransport()
        self.runtime = VMRuntime(self.home, self.binding, transport=self.transport)
        self.store = Store(self.home, guest_paths=True)
        self.runtime.attach(self.store)
        self.workspace = self.store.save_workspace({"name": "Guest", "workingDirectory": r"Z:\guest-only\workspace", "skills": []})

    async def asyncTearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def task(self, **values):
        body = {"workspaceId": self.workspace["id"], "workingDirectory": self.workspace["workingDirectory"],
                "browserMode": "dedicated", "browser": "chromium", "prompt": "Complete the guest objective", **values}
        return self.store.submit(body)[0]

    async def seed(self, task):
        self.store.update(task["id"], status="running")
        attempt = await self.runtime._new_attempt(task)
        self.runtime.workers[task["id"]] = True
        self.runtime.controls[task["id"]] = asyncio.Event()
        self.runtime.controls[task["id"]].set()
        return attempt

    async def test_execution_uses_only_guest_transport_and_keeps_host_task_id(self):
        task = self.task()
        self.store.update(task["id"], status="running")
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Model execution reached host process launch")):
            await self.runtime.run(self.store.get(task["id"]), self.store)
        actual = self.store.get(task["id"])
        self.assertEqual(actual["id"], task["id"])
        self.assertEqual(actual["status"], "completed")
        self.assertEqual(actual["result"], "GUEST-VERIFIED")
        self.assertNotEqual(self.runtime._attempt(task["id"])["guest_task_id"], task["id"])
        self.assertEqual(actual["threadId"], "native-guest-thread")
        self.assertEqual(self.runtime.max_concurrent_tasks, 1)

    async def test_history_snapshot_import_uploads_retained_files_and_does_not_resume_source(self):
        parent = self.task()
        await self.runtime._new_attempt(parent)
        self.store.update(parent['id'], status='completed', thread_id='source-native')
        files = Attachments(self.store)
        attachment = files.upload({'workspaceId': self.workspace['id'], 'name': 'old.txt', 'data': base64.b64encode(b'old attachment').decode()})['id']
        files.claim([attachment], parent['id'])
        self.store.set_history_seed(parent['id'], [{'role': 'user', 'text': 'Retained prompt', 'attachments': [attachment]}, {'role': 'assistant', 'text': 'Retained answer', 'attachments': []}])
        task = self.task(prompt='Continue fork', resumeTaskId=parent['id'])
        await self.runtime._new_attempt(task)
        submitted = [call[2] for call in self.transport.calls if call[:2] == ('POST', '/v1/tasks')][-1]
        self.assertNotIn('resumeTaskId', submitted)
        self.assertEqual([message['text'] for message in submitted['historyImport']], ['Retained prompt', 'Retained answer'])
        copied = submitted['historyImport'][0]['attachments'][0]
        self.assertNotEqual(copied, attachment)
        self.assertEqual(self.transport.attachments[copied]['data'], base64.b64encode(b'old attachment').decode())
        await self.runtime.run(self.store.get(task['id']), self.store)
        self.assertIsNone(self.store.history_seed(task['id']))

    async def test_history_deletion_erases_guest_chat_and_retries_missing_copies(self):
        task = self.task()
        await self.runtime._new_attempt(task)
        original = self.transport.request
        erased = []
        async def request(method, path, body=None, **options):
            if path == '/v1/tasks' and method == 'GET':
                return {'data': [{**task, 'conversationId': 'guest-chat'} for task in self.transport.tasks.values()], 'nextCursor': None}
            if path == '/v1/chats/guest-chat/secure-delete':
                erased.append(body)
                self.transport.tasks.clear()
                return {'deleted': True, 'passes': 7}
            return await original(method, path, body, **options)
        with patch.object(self.transport, 'request', new=request):
            await self.runtime.erase_history(task['conversationId'], self.store)
            await self.runtime.erase_history(task['conversationId'], self.store)
        self.assertEqual(erased, [{'confirm': 'guest-chat', 'passes': 7}])

    async def test_guest_cleanup_journal_survives_a_failure_after_guest_records_disappear(self):
        task = self.task()
        await self.runtime._new_attempt(task)
        original = self.transport.request
        calls = []
        async def request(method, path, body=None, **options):
            if path == '/v1/tasks' and method == 'GET':
                return {'data': [{**task, 'conversationId': 'guest-chat'} for task in self.transport.tasks.values()], 'nextCursor': None}
            if path == '/v1/chats/guest-chat/secure-delete':
                calls.append(body)
                self.transport.tasks.clear()
                if len(calls) == 1:
                    raise RuntimeError('guest_api_error: Guest API HTTP 503: fixture cleanup failure')
                return {'deleted': True}
            return await original(method, path, body, **options)
        with patch.object(self.transport, 'request', new=request):
            with self.assertRaises(RuntimeError):
                await self.runtime.erase_history(task['conversationId'], self.store)
            self.assertTrue(list((self.home / 'guest-history-deletions').glob('*.json')))
            await self.runtime.erase_history(task['conversationId'], self.store)
        self.assertEqual(len(calls), 2)
        self.assertFalse(list((self.home / 'guest-history-deletions').glob('*.json')))

    async def test_stream_retry_reuses_existing_guest_task_and_native_conversation(self):
        task = self.task()
        attempt = await self.runtime._new_attempt(task)
        self.runtime._update(task["id"], phase="failed")
        self.store.update(task["id"], status="running", thread_id="native-guest-thread")
        recovering = {**self.store.get(task["id"]), "streamRecoveryRequested": True}
        await self.runtime.run(recovering, self.store)
        self.assertEqual(len(self.transport.tasks), 1)
        self.assertEqual(self.runtime._attempt(task["id"])["guest_task_id"], attempt["guest_task_id"])
        retries = [call for call in self.transport.calls if call[1].endswith("/retry")]
        self.assertEqual([(call[0], call[1], call[2]) for call in retries],
                         [("POST", "/v1/tasks/" + attempt["guest_task_id"] + "/retry", None)])
        self.assertEqual(self.store.get(task["id"])["threadId"], "native-guest-thread")

    async def test_generation_fencing_and_event_deduplication(self):
        task = self.task()
        attempt = await self.runtime._new_attempt(task)
        event = {"id": 7, "kind": "agent", "data": {"method": "item/completed", "params": {"item": {"type": "commandExecution", "exitCode": 0}}}, "createdAt": 42}
        self.assertEqual(self.runtime.ingest(task["id"], attempt, [event]), 1)
        self.assertEqual(self.runtime.ingest(task["id"], attempt, [event]), 0)
        self.assertEqual(self.runtime._attempt(task["id"])["cursor"], 7)
        self.assertEqual(self.runtime.ingest(task["id"], {**attempt, "attempt_id": "stale-attempt"}, [{**event, "id": 8}]), 0)
        self.runtime.state["generation"] += 1
        self.assertEqual(self.runtime.ingest(task["id"], attempt, [{**event, "id": 9}]), 0)
        count = self.store.db.execute("SELECT count(*) FROM events WHERE task_id=? AND kind='agent'", (task["id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_guest_journal_rejects_invalid_batches_atomically(self):
        task = self.task()
        attempt = await self.runtime._new_attempt(task)
        event = {"id": 7, "kind": "agent", "data": {"method": "fixture"}}
        initial = self.store.db.execute("SELECT count(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0]
        for invalid in [True, "8", -1, 2**63, None]:
            with self.subTest(event_id=invalid), self.assertRaisesRegex(RuntimeError, "[Ee]vent|[Jj]ournal|[Cc]ursor"):
                self.runtime.ingest(task["id"], attempt, [event, {**event, "id": invalid}])
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0], initial)
            self.assertEqual(self.runtime._attempt(task["id"])["cursor"], 0)
        with self.assertRaisesRegex(RuntimeError, "[Ee]vent|[Jj]ournal|[Pp]age"):
            self.runtime.ingest(task["id"], attempt, [{**event, "id": ident} for ident in range(1, 202)])

    async def test_guest_journal_cannot_add_unseen_events_behind_accepted_cursor(self):
        task = self.task()
        attempt = await self.runtime._new_attempt(task)
        event = {"id": 10, "kind": "agent", "data": {"method": "fixture"}}
        self.assertEqual(self.runtime.ingest(task["id"], attempt, [event]), 1)
        self.assertEqual(self.runtime.ingest(task["id"], attempt, [event]), 0)
        with self.assertRaisesRegex(RuntimeError, "[Cc]ursor|[Ee]vent"):
            self.runtime.ingest(task["id"], attempt, [{**event, "id": 9}])

    async def test_host_journal_byte_budget_rejects_without_partial_persistence(self):
        task = self.task()
        attempt = await self.runtime._new_attempt(task)
        self.runtime.binding["maxJournalBytes"] = 1024
        event = {"id": 7, "kind": "agent", "data": {"text": "x" * 2048}}
        initial = self.store.db.execute("SELECT count(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "[Jj]ournal|[Bb]udget"):
            self.runtime.ingest(task["id"], attempt, [event])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0], initial)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM vm_event_receipts").fetchone()[0], 0)

    async def test_host_event_budget_survives_runtime_restart_and_duplicate_delivery(self):
        task = self.task()
        attempt = await self.runtime._new_attempt(task)
        initial = self.store.db.execute("SELECT count(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0]
        self.runtime.binding["maxEvents"] = initial + 1
        event = {"id": 7, "kind": "agent", "data": {"method": "fixture"}}
        self.assertEqual(self.runtime.ingest(task["id"], attempt, [event]), 1)
        restarted = VMRuntime(self.home, self.binding, transport=self.transport)
        restarted.binding["maxEvents"] = initial + 1
        restarted.attach(self.store)
        self.assertEqual(restarted.ingest(task["id"], attempt, [event]), 0)
        with self.assertRaisesRegex(RuntimeError, "[Jj]ournal|[Bb]udget|[Ee]vent"):
            restarted.ingest(task["id"], attempt, [{**event, "id": 8}])
        self.assertEqual(restarted._attempt(task["id"])["cursor"], 7)

    async def test_guest_terminal_result_is_within_the_host_journal_budget(self):
        task = self.task()
        self.runtime.binding["maxJournalBytes"] = 1024
        original = self.transport.request
        async def request(method, path, body=None, **options):
            result = await original(method, path, body, **options)
            if method == "GET" and path.startswith("/v1/tasks/") and not path.endswith("/events"):
                result["result"] = "x" * 2048
            return result
        self.transport.request = request
        with self.assertRaisesRegex(RuntimeError, "[Jj]ournal|[Bb]udget"):
            await asyncio.wait_for(self.runtime.run(task, self.store), 1)
        self.assertNotEqual(self.store.get(task["id"])["status"], "completed")
        self.assertEqual(self.transport.lifecycle_calls[-3:], ["stop", "restore", "stop"])

    async def test_guest_terminal_event_drain_requires_real_progress(self):
        task = self.task()
        original = self.transport.request
        async def request(method, path, body=None, **options):
            if path.endswith("/events"):
                return {"data": [], "nextCursor": options.get("after", 0), "hasMore": True}
            return await original(method, path, body, **options)
        self.transport.request = request
        with self.assertRaisesRegex(RuntimeError, "[Pp]rogress|[Cc]ursor|[Ee]vent"):
            await asyncio.wait_for(self.runtime.run(task, self.store), 1)
        self.assertNotEqual(self.store.get(task["id"])["status"], "completed")
        self.assertEqual(self.transport.lifecycle_calls[-3:], ["stop", "restore", "stop"])

    async def test_host_boundary_failure_quarantines_if_guest_stop_is_unconfirmed(self):
        self.transport.fail_lifecycle.add("stop")
        self.runtime.binding["maxJournalBytes"] = 1
        task = self.task()
        with self.assertRaisesRegex(RuntimeError, "[Jj]ournal|[Bb]udget|[Ss]top|[Qq]uarantin"):
            await asyncio.wait_for(self.runtime.run(task, self.store), 1)
        self.assertEqual(self.runtime.state["phase"], "quarantined")
        self.assertEqual(json.loads(self.runtime.state_path.read_text())["phase"], "quarantined")
        previous_calls = list(self.transport.lifecycle_calls)
        with self.assertRaisesRegex(RuntimeError, "[Qq]uarantin|fenced"):
            await self.runtime.run(self.task(prompt="Must not reach the failed VM"), self.store)
        self.assertEqual(self.transport.lifecycle_calls, previous_calls)

    async def test_explicit_reset_clears_quarantine_only_after_verified_restore_and_stop(self):
        self.runtime.state["phase"] = "quarantined"
        self.runtime._save()
        self.transport.fail_lifecycle.add("stop")
        with self.assertRaisesRegex(RuntimeError, "[Ss]top|[Qq]uarantin"):
            await self.runtime.environment("restore")
        self.assertEqual(self.runtime.state["phase"], "quarantined")
        self.transport.fail_lifecycle.clear()
        self.transport.lifecycle_calls.clear()
        await self.runtime.environment("restore")
        self.assertEqual(self.runtime.state["phase"], "ready")
        self.assertEqual(self.transport.lifecycle_calls[-3:], ["stop", "restore", "stop"])

    async def test_guest_artifact_aliases_are_rejected_before_fetching_bytes(self):
        task = self.task()
        for name in ["NUL", "con.txt", "COM1.log", "LPT9", "COM¹", "CONIN$", "file.", "file ", "nul\x00.txt", "a?b", "a|b", "a<b"]:
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "[Aa]rtifact|[Ii]dentity"):
                await self.runtime._artifacts(task["id"], {"id": "guest", "artifacts": [{"id": name, "size": 0}]})
        self.assertFalse(any("/artifacts/" in call[1] for call in self.transport.calls))

    async def test_guest_artifact_inventory_rejects_case_aliases(self):
        task = self.task()
        with self.assertRaisesRegex(RuntimeError, "[Aa]rtifact|[Ii]dentity"):
            await self.runtime._artifacts(task["id"], {"id": "guest", "artifacts": [
                {"id": "evidence.txt", "size": 0}, {"id": "EVIDENCE.TXT", "size": 0}]})

    async def test_guest_artifact_budget_omits_large_files_and_does_not_fetch_them(self):
        task = self.task()
        self.runtime.binding.update(maxArtifactBytes=4, maxTaskArtifactBytes=4)
        await self.runtime._artifacts(task["id"], {"id": "guest", "artifacts": [{"id": "large.bin", "size": 5}]})
        self.assertFalse(any("/artifacts/" in call[1] for call in self.transport.calls))
        self.assertFalse((self.home / "artifacts" / task["id"] / "large.bin").exists())
        omitted = next(event for event in self.store.conversation_events_all(task['conversationId']) if event['kind'] == 'artifact_omitted')
        self.assertEqual(omitted['data']["reason"], "host_export_budget")

    async def test_guest_artifact_budget_counts_files_from_previous_attempts(self):
        task = self.task()
        self.runtime.binding.update(maxArtifactBytes=5, maxTaskArtifactBytes=5)
        self.transport.artifact = b"abc"
        await self.runtime._artifacts(task["id"], {"id": "guest", "artifacts": [{"id": "first.bin", "size": 3}]})
        await self.runtime._artifacts(task["id"], {"id": "guest", "artifacts": [{"id": "second.bin", "size": 3}]})
        root = self.home / "artifacts" / task["id"]
        self.assertEqual((root / "first.bin").read_bytes(), b"abc")
        self.assertFalse((root / "second.bin").exists())
        self.assertFalse(list(root.glob(".*")))

    async def test_lost_admission_response_retries_same_uuid_without_second_guest_task(self):
        task = self.task()
        self.transport.lose_next_admission_response = True
        await self.runtime._new_attempt(task)
        self.assertEqual(len(self.transport.tasks), 1)
        submissions = [call for call in self.transport.calls if call[0:2] == ("POST", "/v1/tasks")]
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0][2]["submissionId"], submissions[1][2]["submissionId"])

    def lose_both_admission_responses(self):
        original = self.transport.request
        async def request(method, path, body=None, **options):
            result = await original(method, path, body, **options)
            if method == "POST" and path == "/v1/tasks":
                raise TimeoutError("Both admission receipts were lost")
            return result
        self.transport.request = request

    async def test_lost_both_admission_receipts_hold_slot_until_guest_shutdown(self):
        task = self.task()
        self.lose_both_admission_responses()
        stopping, release = asyncio.Event(), asyncio.Event()
        original = self.transport.lifecycle
        async def lifecycle(operation):
            if operation == "stop" and not stopping.is_set():
                stopping.set()
                await release.wait()
            return await original(operation)
        self.transport.lifecycle = lifecycle
        job = asyncio.create_task(self.runtime.run(task, self.store))
        try:
            await asyncio.wait_for(stopping.wait(), 1)
            self.assertFalse(job.done())
            self.assertIn(task["id"], self.runtime.workers)
            self.assertEqual(len(self.transport.tasks), 1)
            submissions = [call for call in self.transport.calls if call[:2] == ("POST", "/v1/tasks")]
            self.assertEqual(len(submissions), 2)
            self.assertEqual(submissions[0][2]["submissionId"], submissions[1][2]["submissionId"])
        finally:
            release.set()
        with self.assertRaisesRegex(TimeoutError, "admission receipts"):
            await job
        self.assertEqual(self.transport.lifecycle_calls[-3:], ["stop", "restore", "stop"])
        self.assertEqual(self.runtime.state["phase"], "ready")
        self.assertNotIn(task["id"], self.runtime.workers)

    async def test_lost_both_admission_receipts_quarantine_when_shutdown_fails(self):
        self.lose_both_admission_responses()
        self.transport.fail_lifecycle.add("stop")
        task = self.task()
        generation = self.runtime.state["generation"]
        with self.assertRaisesRegex(TimeoutError, "admission receipts"):
            await self.runtime.run(task, self.store)
        self.assertEqual(self.runtime.state["phase"], "quarantined")
        self.assertEqual(self.runtime.state["generation"], generation)
        self.assertNotIn("restore", self.transport.lifecycle_calls)
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            await self.runtime.run(self.task(prompt="Cannot share an uncontrolled guest"), self.store)

    async def test_export_fence_while_waiting_for_start_does_not_mutate_vm(self):
        await self.runtime.lifecycle_lock.acquire()
        job = asyncio.create_task(self.runtime.run(self.task(), self.store))
        await asyncio.sleep(0)
        self.runtime.state["phase"] = "exported"
        self.runtime._save()
        self.runtime.lifecycle_lock.release()
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            await job
        self.assertEqual(self.transport.lifecycle_calls, [])
        self.assertEqual(self.runtime.state["phase"], "exported")

    async def test_concurrent_export_during_failed_admission_cleanup_remains_fenced(self):
        self.lose_both_admission_responses()
        original = self.transport.lifecycle
        async def lifecycle(operation):
            result = await original(operation)
            if operation == "stop":
                self.runtime.state["phase"] = "exported"
                self.runtime._save()
            return result
        self.transport.lifecycle = lifecycle
        with self.assertRaisesRegex(TimeoutError, "admission receipts"):
            await self.runtime.run(self.task(), self.store)
        self.assertEqual(self.transport.lifecycle_calls[-1], "stop")
        self.assertNotIn("restore", self.transport.lifecycle_calls)
        self.assertEqual(self.runtime.state["phase"], "exported")
        self.assertEqual(json.loads(self.runtime.state_path.read_text())["phase"], "exported")

    async def test_recovery_uses_new_admission_and_reconciliation_context(self):
        task = self.task()
        first = await self.runtime._new_attempt(task)
        self.runtime.state["generation"] += 1
        await self.runtime._new_attempt(task, recovery=True)
        second = self.runtime._attempt(task["id"])
        self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        self.assertNotEqual(second["guest_task_id"], first["guest_task_id"])
        request = [call[2] for call in self.transport.calls if call[0:2] == ("POST", "/v1/tasks")][-1]
        self.assertNotIn("resumeTaskId", request)
        self.assertIn("Internet effects were not rolled back", request["prompt"])
        self.assertIn("verify uncertain outcomes", request["prompt"])

    async def test_selected_skill_support_files_are_staged_inside_guest(self):
        skill = self.home / "selected-skill" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: selected\ndescription: selected fixture\n---\nRead references/example.txt.\n")
        support = skill.parent / "references" / "example.txt"
        support.parent.mkdir()
        support.write_text("SUPPORT-FILE-EVIDENCE")
        self.workspace = self.store.save_workspace({"skills": [str(skill)]}, self.workspace["id"])
        task = self.task()
        await self.runtime._new_attempt(task)
        self.assertTrue(any(path.endswith("SKILL.md") for path in self.transport.staged))
        self.assertTrue(any(bytes(data) == b"SUPPORT-FILE-EVIDENCE" for data in self.transport.staged.values()))
        workspace = next(iter(self.transport.workspaces.values()))
        self.assertEqual(len(workspace["skills"]), 1)
        self.assertTrue(workspace["skills"][0].startswith(r"C:\ProgramData\CodexPermissiveGuest\imported-skills"))
        self.assertNotIn(str(skill), workspace["skills"])

    async def test_guest_workspace_keys_are_lexical_and_do_not_follow_host_aliases(self):
        with patch("pathlib.Path.resolve", side_effect=AssertionError("Guest cwd resolved on host")), \
             patch("pathlib.Path.is_dir", side_effect=AssertionError("Guest cwd inspected on host")):
            first = self.store.save_workspace({"name": "Guest alias", "workingDirectory": r"C:\host-looking-link", "skills": []})
            second = self.store.save_workspace({"name": "Guest target", "workingDirectory": r"C:\actual-target", "skills": []})
            found = self.store.workspace_for_directory("c:/HOST-LOOKING-LINK/.")
            updated = self.store.save_workspace({"workingDirectory": r"D:\changed\folder"}, first["id"])
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(found["id"], first["id"])
            self.assertEqual(updated["workingDirectory"], r"D:\changed\folder")
            self.assertEqual(self.store.workspace_for_directory("d:/CHANGED/folder")["id"], first["id"])

    async def test_post_completion_checkpoint_failure_does_not_fail_verified_work(self):
        self.transport.fail_checkpoint_after = 2
        task = self.task()
        await self.runtime.run(task, self.store)
        self.assertEqual(self.store.get(task["id"])["status"], "completed")
        self.assertEqual(self.store.get(task["id"])["result"], "GUEST-VERIFIED")

    async def test_restore_during_artifact_transfer_cannot_publish_stale_completion(self):
        self.transport.artifacts_enabled = True
        original = self.transport.request
        changed = False
        async def request(method, path, body=None, **options):
            nonlocal changed
            result = await original(method, path, body, **options)
            if "/artifacts/" in path and not changed:
                changed = True
                self.runtime.state["generation"] += 1
            return result
        self.transport.request = request
        task = self.task()
        await self.runtime.run(task, self.store)
        self.assertEqual(len(self.transport.tasks), 2)
        self.assertEqual(self.runtime._attempt(task["id"])["generation"], 2)
        self.assertEqual(self.store.get(task["id"])["status"], "completed")

    async def test_attachments_and_artifacts_cross_only_as_guest_data(self):
        attachments = Attachments(self.store)
        attached = attachments.upload({"workspaceId": self.workspace["id"], "name": "input.txt", "data": base64.b64encode(b"user-selected-input").decode()})
        self.transport.artifacts_enabled = True
        task = self.task(attachments=[attached["id"]])
        await self.runtime.run(task, self.store)
        self.assertEqual(next(iter(self.transport.attachments.values()))["bytes"], b"user-selected-input")
        self.assertEqual((self.home / "artifacts" / task["id"] / "evidence.bin").read_bytes(), self.transport.artifact)
        self.assertFalse(list((self.home / "artifacts" / task["id"]).glob(".*")))

    async def test_continuation_attachments_use_predecessor_guest_workspace(self):
        parent = self.task()
        await self.runtime.run(parent, self.store)
        attachments = Attachments(self.store)
        attached = attachments.upload({"workspaceId": self.workspace["id"], "name": "followup.txt", "data": base64.b64encode(b"followup").decode()})
        child = self.task(prompt="Continue in guest", resumeTaskId=parent["id"], attachments=[attached["id"]])
        await self.runtime._new_attempt(child)
        self.assertEqual(self.runtime._attempt(child["id"])["guest_workspace_id"], self.runtime._attempt(parent["id"])["guest_workspace_id"])

    async def test_changed_guest_generation_restores_context_only_when_native_reuse_is_impossible(self):
        parent = self.task(prompt='Keep this context')
        await self.runtime.run(parent, self.store)
        child = self.task(prompt='Continue after guest reset', resumeTaskId=parent['id'])
        self.assertIsNone(self.store.history_seed(child['id']))
        self.runtime.state['generation'] += 1
        await self.runtime._new_attempt(child)
        submitted = [call[2] for call in self.transport.calls if call[:2] == ('POST', '/v1/tasks')][-1]
        self.assertNotIn('resumeTaskId', submitted)
        self.assertIn('Keep this context', [point['text'] for point in submitted['historyImport']])
        self.assertIn('GUEST-VERIFIED', [point['text'] for point in submitted['historyImport']])

    async def test_pause_and_cancel_require_hypervisor_success(self):
        task = self.task()
        await self.seed(task)
        await self.runtime.pause(task["id"], self.store)
        self.assertEqual(self.store.get(task["id"])["status"], "paused")
        self.assertFalse(self.runtime.controls[task["id"]].is_set())
        self.assertIn("pause", self.transport.lifecycle_calls)
        await self.runtime.resume(task["id"], self.store)
        self.assertTrue(self.runtime.controls[task["id"]].is_set())
        generation = self.runtime.state["generation"]
        await self.runtime.interrupt(task["id"], self.store)
        self.assertEqual(self.transport.lifecycle_calls[-3:], ["stop", "restore", "stop"])
        self.assertEqual(self.runtime.state["generation"], generation + 1)
        self.assertEqual(self.runtime._attempt(task["id"])["phase"], "cancelled")

    async def test_failed_hypervisor_pause_does_not_claim_paused(self):
        task = self.task()
        await self.seed(task)
        self.store.update(task["id"], status="pausing")
        self.transport.fail_lifecycle.add("pause")
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            await self.runtime.pause(task["id"], self.store)
        self.assertEqual(self.store.get(task["id"])["status"], "pausing")

    async def test_failed_hypervisor_stop_does_not_advance_generation(self):
        task = self.task()
        await self.seed(task)
        self.store.update(task["id"], status="cancelling")
        generation = self.runtime.state["generation"]
        self.transport.fail_lifecycle.add("stop")
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            await self.runtime.interrupt(task["id"], self.store)
        self.assertEqual(self.runtime.state["generation"], generation)
        self.assertEqual(self.store.get(task["id"])["status"], "cancelling")

    async def test_exported_source_cannot_execute(self):
        self.runtime.state["phase"] = "exported"
        with self.assertRaisesRegex(RuntimeError, "fenced"):
            await self.runtime.run(self.task(), self.store)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.transport.lifecycle_calls, [])


class ControllerAPItests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        disk_usage = patch('permissive.vm_runtime.shutil.disk_usage', return_value=Mock(free=2 * 1024**4))
        disk_usage.start()
        self.addCleanup(disk_usage.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        binding = self.home / "deployment.json"
        binding.write_text(json.dumps({"role": "controller", "vmId": str(uuid.uuid4()), "guestCredentialPath": "guest.dpapi"}))
        self.transport = GuestTransport()
        self.runtime = VMRuntime(self.home, binding, transport=self.transport)
        self.service = Service(self.home, self.runtime)
        self.client = SignedTestClient(TestServer(self.service.app()), provision(self.home))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    async def api(self, method, path, body=None, status=200):
        response = await self.client.request(method, path, json=body, headers={"X-Test-Sign": "1"})
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def test_browser_routes_are_guest_only(self):
        with patch("permissive.service.browser_catalog", side_effect=AssertionError("Host browser catalog used")), \
             patch("permissive.service.open_installed_profile", side_effect=AssertionError("Host browser opened")), \
             patch("permissive.service.setup_installed_profile", side_effect=AssertionError("Host browser setup used")):
            result = await self.api("GET", "/v1/browsers")
            self.assertEqual(result["data"][0]["id"], "guest-browser")
            result = await self.api("POST", "/v1/browsers/connect", {"browser": "msedge", "profileId": "guest-profile"})
            self.assertTrue(result["guest"])
            result = await self.api("POST", "/v1/browsers/setup", {"browser": "msedge", "profileId": "guest-profile", "confirmed": True})
            self.assertTrue(result["guest"])

    async def test_controller_rejects_local_host_browser_broker(self):
        original_signer = self.client.signer
        self.client.signer = provision(self.home, "browser")
        try:
            response = await self.client.get("/v1/browser/extension", headers={"X-Test-Sign": "1"})
            self.assertEqual(response.status, 403)
            response = await self.client.post("/v1/browser/command", json={"operation": "list", "taskId": "host-broker-fixture"}, headers={"X-Test-Sign": "1"})
            self.assertEqual(response.status, 403)
            self.assertEqual(self.service.browser.connections, {})
        finally:
            self.client.signer = original_signer

    async def test_guest_workspace_does_not_require_a_host_directory(self):
        with patch("pathlib.Path.is_dir", side_effect=AssertionError("Guest directory inspected on host")), \
             patch("pathlib.Path.resolve", side_effect=AssertionError("Guest directory resolved on host")):
            workspace = await self.api("POST", "/v1/workspaces", {"name": "Guest path", "workingDirectory": r"Z:\guest-only\new-folder"}, status=201)
        self.assertEqual(workspace["workingDirectory"], r"Z:\guest-only\new-folder")

    async def test_guest_idempotency_uses_lexical_windows_paths(self):
        body = {"prompt": "Guest lexical admission", "workingDirectory": "Z:/guest-only/lexical", "submissionId": str(uuid.uuid4())}
        resolve, is_dir = Path.resolve, Path.is_dir
        def managed_only(method, path, *args, **kwargs):
            if str(path).lower().startswith('z:'):
                raise AssertionError('Guest admission inspected a guest path on the host')
            return method(path, *args, **kwargs)
        with patch('pathlib.Path.resolve', autospec=True, side_effect=lambda path, *args, **kwargs: managed_only(resolve, path, *args, **kwargs)), \
             patch('pathlib.Path.is_dir', autospec=True, side_effect=lambda path: managed_only(is_dir, path)):
            first, created = self.service.submit(body)
            second, repeated = self.service.submit({**body, "workingDirectory": r"z:\GUEST-ONLY\lexical\."})
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["request"]["workingDirectory"], r"Z:\guest-only\lexical")

    async def test_guest_unc_device_and_parent_paths_are_rejected_before_rpc(self):
        for directory in [r"\\host\share\data", r"\\?\C:\data", r"\\.\C:\data", r"C:\data\..\other"]:
            with self.subTest(directory=directory):
                response = await self.client.post("/v1/tasks", json={"prompt": "Must remain a guest-local cwd", "workingDirectory": directory}, headers={"X-Test-Sign": "1"})
                self.assertEqual(response.status, 400)
                response = await self.client.post("/v1/workspaces", json={"name": "Invalid guest cwd", "workingDirectory": directory}, headers={"X-Test-Sign": "1"})
                self.assertEqual(response.status, 400)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.transport.rpc_calls, [])

    async def test_controller_skill_catalog_never_scans_guest_project_on_host(self):
        workspace = self.service.store.save_workspace({"name": "Guest catalog", "workingDirectory": r"Z:\guest-only\skills", "skills": []})
        with patch("permissive.service.skill_catalog", return_value={"data": []}) as catalog:
            await self.api("GET", "/v1/skills?workspaceId=" + workspace["id"] + "&workingDirectory=Z%3A%5Cguest-only%5Coverride")
        catalog.assert_called_once_with(self.home, None, set(), [])

    async def test_quarantined_environment_rejects_public_submission_before_admission(self):
        self.runtime.state["phase"] = "quarantined"
        workspace = self.service.store.workspaces()["data"][0]
        before = self.service.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        response = await self.client.post("/v1/tasks", json={"workspaceId": workspace["id"], "prompt": "Must not enter a quarantined VM"}, headers={"X-Test-Sign": "1"})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.service.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0], before)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.transport.lifecycle_calls, [])

    async def test_failed_cancel_returns_503_and_remains_cancelling(self):
        workspace = self.service.store.workspaces()["data"][0]
        task = self.service.store.submit({"workspaceId": workspace["id"], "workingDirectory": workspace["workingDirectory"], "browserMode": "dedicated", "prompt": "Guest task"})[0]
        self.service.store.update(task["id"], status="running")
        self.runtime.workers[task["id"]] = True
        self.runtime.controls[task["id"]] = asyncio.Event()
        job = asyncio.create_task(asyncio.Event().wait())
        self.service.jobs[task["id"]] = job
        self.transport.fail_lifecycle.add("stop")
        try:
            response = await self.client.post("/v1/tasks/" + task["id"] + "/cancel", headers={"X-Test-Sign": "1"})
            self.assertEqual(response.status, 503)
            self.assertEqual(self.service.store.get(task["id"])["status"], "cancelling")
            self.assertFalse(job.done())
        finally:
            self.transport.fail_lifecycle.clear()
            self.service.jobs.pop(task["id"], None)
            self.runtime.workers.pop(task["id"], None)
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def test_failed_resume_keeps_task_paused(self):
        workspace = self.service.store.workspaces()["data"][0]
        task = self.service.store.submit({"workspaceId": workspace["id"], "workingDirectory": workspace["workingDirectory"], "browserMode": "dedicated", "prompt": "Guest task"})[0]
        self.service.store.update(task["id"], status="paused", thread_id="guest-thread")
        self.runtime.workers[task["id"]] = True
        self.runtime.controls[task["id"]] = asyncio.Event()
        job = asyncio.create_task(asyncio.Event().wait())
        self.service.jobs[task["id"]] = job
        self.transport.fail_lifecycle.add("resume")
        try:
            response = await self.client.post("/v1/tasks/" + task["id"] + "/resume", headers={"X-Test-Sign": "1"})
            self.assertIn(response.status, {500, 503})
            self.assertEqual(self.service.store.get(task["id"])["status"], "paused")
            self.assertFalse(self.runtime.controls[task["id"]].is_set())
        finally:
            self.transport.fail_lifecycle.clear()
            self.service.jobs.pop(task["id"], None)
            self.runtime.workers.pop(task["id"], None)
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def test_public_submissions_serialize_one_active_guest_task(self):
        original = self.transport.request
        admitted = asyncio.Event()
        release = asyncio.Event()
        submissions = 0
        async def request(method, path, body=None, **options):
            nonlocal submissions
            if method == "POST" and path == "/v1/tasks":
                submissions += 1
                if submissions == 1:
                    admitted.set()
                    await release.wait()
            return await original(method, path, body, **options)
        self.transport.request = request
        workspace = self.service.store.workspaces()["data"][0]
        try:
            await self.api("POST", "/v1/tasks", {"workspaceId": workspace["id"], "prompt": "first guest task"}, status=202)
            await asyncio.wait_for(admitted.wait(), 3)
            await self.api("POST", "/v1/tasks", {"workspaceId": workspace["id"], "prompt": "second guest task"}, status=202)
            await asyncio.sleep(.02)
            self.assertEqual(submissions, 1)
            release.set()
            async with asyncio.timeout(5):
                while self.service.jobs:
                    await asyncio.sleep(.01)
            self.assertEqual(submissions, 2)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
