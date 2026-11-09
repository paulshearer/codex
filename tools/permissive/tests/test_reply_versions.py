import asyncio
import json
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from test_history import HistoryTests
from test_conversations import ControlledRuntime
from permissive.service import Service


class ReplyVersionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = HistoryTests.asyncSetUp
    asyncTearDown = HistoryTests.asyncTearDown
    fixture = HistoryTests.fixture
    agent = HistoryTests.agent
    post = HistoryTests.post
    get = HistoryTests.get
    started = HistoryTests.started
    finish = HistoryTests.finish
    messages = HistoryTests.messages

    async def regenerate(self, task, message='response', submission=None):
        return await self.post('/v1/chats/' + task['conversationId'] + '/regenerate',
            dict(taskId=task['id'], messageId=message, submissionId=submission or str(uuid.uuid4())))

    async def ready(self, chat):
        job = self.service.regeneration_finalizers.get(chat['id'])
        if job:
            await asyncio.wait_for(asyncio.shield(job), 8)
        result = await self.get('/v1/chats/' + chat['id'])
        self.assertFalse(result['branchPreparing'], result.get('branchPreparationError'))
        return result

    async def test_dispatch_precedes_blocked_copy_and_retries_share_one_task(self):
        from permissive.reply_versions import copy_history
        first, _ = self.fixture(True)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocked(store, job):
            calls.append(job['task_id'])
            entered.set()
            if not release.wait(8):
                raise RuntimeError('Fixture timed out')
            return copy_history(store, job)
        submission = str(uuid.uuid4())
        try:
            with patch('permissive.reply_versions.copy_history', side_effect=blocked):
                chat, repeated = await asyncio.gather(self.regenerate(first, submission=submission), self.regenerate(first, submission=submission))
                self.assertEqual(chat['id'], repeated['id'])
                task = await self.started(chat['tasks'][-1])
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertEqual(calls, [task['id']])
                self.assertTrue((await asyncio.wait_for(self.get('/v1/health'), 1))['ok'])
                old = await self.get('/v1/chats/' + first['conversationId'])
                self.assertIn('First answer', [point['text'] for point in self.messages(old)])
                self.assertFalse(self.service.store.db.execute('SELECT 1 FROM history_deletions').fetchone())
                rejected = await self.client.post('/v1/tasks', json={'resumeTaskId': first['id'], 'prompt': 'race'}, headers={'X-Test-Sign': '1'})
                self.assertEqual(rejected.status, 409)
                await self.finish(task)
                release.set()
                final = await self.ready(chat)
                self.assertEqual(final['tasks'][-1]['id'], task['id'])
                self.assertEqual(len(final['replyVersions'][0]['versions']), 2)
        finally:
            release.set()

    async def test_versions_keep_continuations_nested_choices_and_one_sidebar_entry(self):
        first, second = self.fixture()
        chat = await self.regenerate(second)
        await self.started(chat['tasks'][-1]); await self.finish(chat['tasks'][-1])
        chat = await self.ready(chat)
        group = chat['replyVersions'][0]['groupId']
        task = chat['tasks'][-1]
        third = await self.regenerate(task)
        await self.started(third['tasks'][-1]); await self.finish(third['tasks'][-1])
        third = await self.ready(third)
        self.assertEqual(third['replyVersions'][0]['groupId'], group)
        self.assertEqual(len(third['replyVersions'][0]['versions']), 3)
        self.assertEqual(len((await self.get('/v1/chats'))['data']), 1)
        self.assertEqual(self.service.store.workspace(first['workspaceId'])['taskCount'], 1)
        continuation = await self.post('/v1/tasks/' + third['tasks'][-1]['id'] + '/continue', {'prompt': 'new path'})
        await self.started(continuation); await self.finish(continuation)
        original = await self.get('/v1/chats/' + first['conversationId'])
        self.assertNotIn('new path', json.dumps(original))
        old_continuation = await self.post('/v1/tasks/' + second['id'] + '/continue', {'prompt': 'old path'})
        await self.started(old_continuation); await self.finish(old_continuation)
        self.assertNotIn('old path', json.dumps(await self.get('/v1/chats/' + third['id'])))
        nested = await self.regenerate(old_continuation)
        await self.started(nested['tasks'][-1]); await self.finish(nested['tasks'][-1])
        nested = await self.ready(nested)
        self.assertEqual(len(nested['replyVersions']), 2)

    async def test_cancelled_attempt_is_selectable_and_whole_chat_delete_erases_versions(self):
        first, _ = self.fixture(True)
        attachment = first['request']['attachments'][0]
        chat = await self.regenerate(first)
        await self.started(chat['tasks'][-1])
        await self.post('/v1/tasks/' + chat['tasks'][-1]['id'] + '/cancel', {})
        final = await self.ready(chat)
        self.assertEqual(final['tasks'][-1]['status'], 'cancelled')
        self.assertEqual(len(final['replyVersions'][0]['versions']), 2)
        deleted = await self.post('/v1/chats/' + chat['id'] + '/secure-delete', {'confirm': chat['id'], 'passes': 7})
        self.assertEqual(set(deleted['conversationIds']), {first['conversationId'], chat['id']})
        self.assertFalse((self.home / 'attachments' / attachment).exists())
        self.assertEqual((await self.get('/v1/chats'))['data'], [])

    async def test_restart_finalizes_without_reexecuting_the_prompt(self):
        from permissive.reply_versions import prepare
        from permissive.history import operation_key
        first, _ = self.fixture()
        await self.service.ensure_segments(first['conversationId'])
        body = dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()), _operation='regenerate')
        branch, _ = prepare(self.service.store, first['conversationId'], body, operation_key(first['conversationId'], body, True))
        self.service.store.segments.scope = None
        signer = self.client.signer
        await self.client.close()
        self.runtime = ControlledRuntime()
        self.service = Service(self.home, self.runtime)
        self.client = SignedTestClient(TestServer(self.service.app()), signer)
        await self.client.start_server()
        final = await self.ready({'id': branch})
        self.assertEqual(final['tasks'][-1]['status'], 'interrupted')
        self.assertEqual(self.runtime.gates, {})
        retry = await self.regenerate(first, submission=body['submissionId'])
        self.assertEqual(retry['id'], branch)
        self.assertEqual(self.runtime.gates, {})

    async def test_finalizer_failure_is_visible_and_retries_without_losing_original(self):
        from permissive.reply_versions import copy_history
        first, _ = self.fixture()
        attempts = []
        def flaky(store, job):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError('Fixture storage unavailable')
            return copy_history(store, job)
        with patch('permissive.reply_versions.copy_history', side_effect=flaky):
            chat = await self.regenerate(first)
            await self.started(chat['tasks'][-1]); await self.finish(chat['tasks'][-1])
            await self.ready(chat)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertIn('First answer', json.dumps(await self.get('/v1/chats/' + first['conversationId'])))

    async def test_version_snapshots_reuse_message_positions_without_replaying_the_segment(self):
        first, second = self.fixture()
        chat = await self.regenerate(second)
        await self.started(chat['tasks'][-1]); await self.finish(chat['tasks'][-1])
        chat = await self.ready(chat)
        self.service.store.segments.reply_points_cache.clear()
        with patch.object(self.service.store, 'conversation_events_all', side_effect=AssertionError('Full history scan')):
            initial = self.service.store.conversation(chat['id'])
            with patch.object(self.service.store.segments, 'rows', side_effect=AssertionError('Repeated event scan')):
                refreshed = self.service.store.conversation(chat['id'])
        self.assertEqual(initial['replyVersions'], refreshed['replyVersions'])
        self.assertIsNotNone(initial['replyVersions'][0]['messageId'])

    async def test_disconnected_preparation_finishes_and_keeps_health_responsive(self):
        from permissive.reply_versions import prepare
        first, _ = self.fixture()
        entered, release = threading.Event(), threading.Event()
        body = dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()))
        async def request_body():
            return dict(body)
        def blocked(*args):
            entered.set()
            if not release.wait(8):
                raise RuntimeError('Fixture timed out')
            return prepare(*args)
        try:
            with patch('permissive.reply_versions.prepare', side_effect=blocked):
                caller = asyncio.create_task(self.service.regenerate(SimpleNamespace(match_info={'id': first['conversationId']}, json=request_body)))
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertTrue((await asyncio.wait_for(self.get('/v1/health'), 1))['ok'])
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                accepted = list(self.service.history_operations.values())[0]
                release.set()
                branch = await accepted
                chat = self.service.store.conversation(branch)
                await self.started(chat['tasks'][-1])
                repeated = await self.regenerate(first, submission=body['submissionId'])
                self.assertEqual(repeated['id'], branch)
                await self.finish(chat['tasks'][-1]); await self.ready(chat)
        finally:
            release.set()

    async def test_selected_branch_deletion_retains_sibling_and_retry_identity(self):
        first, second = self.fixture(True)
        submission = str(uuid.uuid4())
        chat = await self.regenerate(second, submission=submission)
        await self.started(chat['tasks'][-1]); await self.finish(chat['tasks'][-1])
        chat = await self.ready(chat)
        replacement = await self.post('/v1/chats/' + chat['id'] + '/delete-message', dict(
            taskId=chat['tasks'][-1]['id'], messageId='response', segmentId=chat['segment']['id'],
            submissionId=str(uuid.uuid4()), confirm=chat['id']))
        self.assertEqual(replacement['chatGroupId'], first['conversationId'])
        self.assertEqual(len(replacement['replyVersions'][0]['versions']), 2)
        retry = await self.regenerate(second, submission=submission)
        self.assertEqual(retry['id'], replacement['id'])
        self.assertEqual(self.service.jobs, {})
        original = await self.get('/v1/chats/' + first['conversationId'])
        self.assertIn('Later secret answer', json.dumps(original))
        wrong = await self.client.post('/v1/chats/' + first['conversationId'] + '/regenerate', json=dict(
            taskId=first['id'], messageId='response', submissionId=submission), headers={'X-Test-Sign': '1'})
        self.assertEqual(wrong.status, 400)

    async def test_followup_waits_for_finalization_then_continues_new_branch(self):
        first, _ = self.fixture()
        chat = await self.regenerate(first)
        task = await self.started(chat['tasks'][-1])
        await self.post('/v1/chats/' + chat['id'] + '/followups', {'prompt': 'after regeneration', 'submissionId': str(uuid.uuid4())})
        await self.finish(task)
        final = await self.ready(chat)
        successor = final['tasks'][-1]
        self.assertNotEqual(successor['id'], task['id'])
        self.assertEqual(successor['request']['prompt'], 'after regeneration')
        await self.started(successor); await self.finish(successor)
