import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.service import Service
from permissive.store import Store


class ControlledRuntime:
    def __init__(self):
        self.gates = {}
        self.deliveries = []
        self.failures = set()

    async def run(self, task, store):
        ident = task['id']
        self.gates[ident] = asyncio.Event()
        store.update(ident, thread_id=task['threadId'] or 'native-' + ident)
        store.event(ident, 'agent', {'method': 'turn/started', 'params': {'turn': {'id': 'turn-' + ident}}})
        await self.gates[ident].wait()
        if ident in self.failures:
            raise RuntimeError('Fixture failure')
        store.event(ident, 'agent', {'method': 'item/completed', 'params': {'item': {'id': 'answer', 'type': 'agentMessage', 'text': task['request']['prompt']}}})
        store.update(ident, status='completed', result=task['request']['prompt'])

    async def steer(self, ident, store, body):
        self.deliveries.append((ident, body))
        return {'turnId': store.get(ident)['activeTurnId']}

    async def interrupt(self, ident, store):
        pass

    async def close(self):
        pass


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.runtime = ControlledRuntime()
        self.service = Service(self.home, self.runtime)
        self.client = SignedTestClient(TestServer(self.service.app()), provision(self.home))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def post(self, path, body):
        response = await self.client.post(path, json=body, headers={'X-Test-Sign': '1'})
        self.assertLess(response.status, 300, await response.text())
        return await response.json()

    async def get(self, path):
        response = await self.client.get(path, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def started(self, task):
        for _ in range(100):
            if self.service.store.get(task['id'])['activeTurnId']:
                return self.service.store.get(task['id'])
            await asyncio.sleep(.01)
        self.fail('Task never started')

    async def submit(self, prompt='first', workspace=None):
        return await self.post('/v1/tasks', {'prompt': prompt, 'workingDirectory': str(self.home),
            **({'workspaceId': workspace} if workspace else {})})

    async def finish(self, task):
        self.runtime.gates[task['id']].set()
        for _ in range(100):
            if task['id'] not in self.service.jobs:
                return
            await asyncio.sleep(.01)
        self.fail('Task never finished')

    async def test_continuations_share_one_conversation_and_preserve_all_events(self):
        first = await self.started(await self.submit())
        await self.finish(first)
        body = {'prompt': 'second', 'submissionId': str(uuid.uuid4())}
        second = await self.post('/v1/tasks/' + first['id'] + '/continue', body)
        repeated = await self.post('/v1/tasks/' + first['id'] + '/continue', body)
        self.assertEqual(second['id'], repeated['id'])
        second = await self.started(second)
        self.assertEqual(second['conversationId'], first['conversationId'])
        self.assertEqual(second['threadId'], first['threadId'])
        chat = await self.get('/v1/chats/' + first['conversationId'])
        self.assertEqual([task['request']['prompt'] for task in chat['tasks']], ['first', 'second'])
        listed = await self.get('/v1/chats')
        self.assertEqual(len(listed['data']), 1)
        self.assertEqual(listed['data'][0]['title'], 'first')
        self.assertEqual(self.service.store.workspace(first['workspaceId'])['taskCount'], 1)
        page = await self.get(f"/v1/chats/{first['conversationId']}/events?format=json&after=0&through={chat['eventCursor']}")
        self.assertEqual({event['taskId'] for event in page['data']}, {first['id'], second['id']})
        self.assertEqual(sorted(event['id'] for event in page['data']), [event['id'] for event in page['data']])
        response = await self.client.post('/v1/tasks/' + first['id'] + '/continue', json={'prompt': 'stale'}, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 400)

    async def test_open_event_stream_does_not_delay_service_shutdown(self):
        task = await self.started(await self.submit())
        await self.finish(task)
        chat = await self.get('/v1/chats/' + task['conversationId'])
        response = await self.client.get('/v1/chats/' + chat['id'] + '/events?after=' + str(chat['eventCursor']), headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 200)
        try:
            await asyncio.wait_for(self.client.server.close(), 2)
            self.assertEqual(await asyncio.wait_for(response.content.read(), 1), b'')
        finally:
            response.close()

    async def test_rename_is_shared_by_lists_snapshots_and_continuations_without_activity_changes(self):
        first = await self.started(await self.submit())
        ident = first['conversationId']
        before = self.service.store.get(first['id'])
        cursor = self.service.store.conversation(ident)['eventCursor']
        response = await self.client.patch('/v1/chats/' + ident, json={'title': '  Project notes 📝  '}, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.json(), {'id': ident, 'title': 'Project notes 📝'})
        self.assertEqual(self.service.store.get(first['id']), before)
        self.assertEqual(self.service.store.conversation(ident)['eventCursor'], cursor)
        self.assertEqual((await self.get('/v1/chats'))['data'][0]['title'], 'Project notes 📝')
        self.assertEqual((await self.get('/v1/chats/' + ident))['title'], 'Project notes 📝')
        await self.post('/v1/chats/' + ident + '/followups', {'prompt': 'Queued work', 'submissionId': str(uuid.uuid4())})
        await self.finish(first)
        second = await self.started(self.service.store.conversation_tasks(ident)[-1])
        self.assertEqual((await self.get('/v1/chats/' + ident))['title'], 'Project notes 📝')
        await self.finish(second)
        reopened = Store(self.home)
        try:
            self.assertEqual(reopened.conversation(ident)['title'], 'Project notes 📝')
            self.assertEqual([task['request']['prompt'] for task in reopened.conversation(ident)['tasks']], ['first', 'Queued work'])
        finally:
            reopened.close()

    async def test_rename_validates_input_and_requires_signed_existing_chat(self):
        first = await self.started(await self.submit())
        path = '/v1/chats/' + first['conversationId']
        for body in ({}, [], {'title': None}, {'title': 123}, {'title': '   '}, {'title': 'x' * 121},
                     {'title': 'two\nlines'}, {'title': 'tab\tinside'}, {'title': 'null\0inside'},
                     {'title': 'two\u2028lines'}, {'title': 'ok', 'prompt': 'changed'}):
            response = await self.client.patch(path, json=body, headers={'X-Test-Sign': '1'})
            self.assertEqual(response.status, 400, (body, await response.text()))
        response = await self.client.patch(path, json={'title': 'unsigned'})
        self.assertEqual(response.status, 401)
        response = await self.client.patch('/v1/chats/missing', json={'title': 'Missing'}, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 404)
        for title in ['📝' * 120, 'Last saved name']:
            response = await self.client.patch(path, json={'title': title}, headers={'X-Test-Sign': '1'})
            self.assertEqual(response.status, 200)
        self.assertEqual((await self.get('/v1/chats/' + first['conversationId']))['title'], 'Last saved name')
        await self.finish(first)

    async def test_steering_deduplicates_retries_and_rejects_stale_turns(self):
        task = await self.started(await self.submit())
        body = {'prompt': 'change direction', 'expectedTurnId': task['activeTurnId'], 'submissionId': str(uuid.uuid4())}
        path = '/v1/tasks/' + task['id'] + '/steer'
        first, second = await asyncio.gather(self.post(path, body), self.post(path, body))
        self.assertEqual(first, second)
        self.assertEqual(len(self.runtime.deliveries), 1)
        response = await self.client.post(path, json={**body, 'prompt': 'different'}, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 400)
        await self.finish(task)
        response = await self.client.post(path, json={**body, 'submissionId': str(uuid.uuid4())}, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.runtime.deliveries), 1)
        self.assertEqual(len(self.service.store.conversation_tasks(task['conversationId'])), 1)

    async def test_queue_dispatches_fifo_once_and_can_remove_waiting_items(self):
        first = await self.started(await self.submit())
        path = '/v1/chats/' + first['conversationId'] + '/followups'
        bodies = [{'prompt': prompt, 'submissionId': str(uuid.uuid4())} for prompt in ['second', 'removed', 'third']]
        for body in bodies:
            await self.post(path, body)
        await self.post(path, bodies[0])
        response = await self.client.delete(path + '/' + bodies[1]['submissionId'], headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 200)
        await self.finish(first)
        second = await self.started(self.service.store.conversation_tasks(first['conversationId'])[-1])
        self.assertEqual(second['request']['prompt'], 'second')
        await self.finish(second)
        third = await self.started(self.service.store.conversation_tasks(first['conversationId'])[-1])
        self.assertEqual(third['request']['prompt'], 'third')
        self.assertEqual(third['threadId'], first['threadId'])
        self.assertEqual(len(self.service.store.conversation_tasks(first['conversationId'])), 3)
        self.assertEqual(self.service.store.conversation(first['conversationId'])['followups'], [])

    async def test_cancellation_holds_queue_until_explicit_resume(self):
        first = await self.started(await self.submit())
        path = '/v1/chats/' + first['conversationId'] + '/followups'
        await self.post(path, {'prompt': 'later', 'submissionId': str(uuid.uuid4())})
        await self.post('/v1/tasks/' + first['id'] + '/cancel', {})
        chat = self.service.store.conversation(first['conversationId'])
        self.assertTrue(chat['queueHeld'])
        self.assertEqual(len(chat['tasks']), 1)
        chat = await self.post(path + '/resume', {})
        self.assertFalse(chat['queueHeld'])
        self.assertEqual(len(chat['tasks']), 2)
        await self.started(chat['tasks'][-1])

    async def test_failure_holds_queue_and_retries_use_original_overrides(self):
        task = await self.started(await self.submit())
        path = '/v1/chats/' + task['conversationId'] + '/followups'
        body = {'prompt': 'next', 'mode': 'goal', 'goal': '  objective  ', 'submissionId': str(uuid.uuid4())}
        await self.post(path, body)
        await self.post(path, body)
        self.runtime.failures.add(task['id'])
        await self.finish(task)
        chat = self.service.store.conversation(task['conversationId'])
        self.assertTrue(chat['queueHeld'])
        self.assertEqual(len(chat['tasks']), 1)
        self.assertEqual(len(chat['followups']), 1)
        chat = await self.post(path + '/resume', {})
        self.assertEqual(chat['tasks'][-1]['request']['goal'], 'objective')
        await self.started(chat['tasks'][-1])
        await self.post(path, body)
        self.assertEqual(len(self.service.store.conversation(task['conversationId'])['tasks']), 2)

    async def test_workspace_filter_and_recency_pagination(self):
        general = self.service.store.save_workspace({'name': 'Here', 'workingDirectory': str(self.home)})
        workspace = self.service.store.save_workspace({'name': 'Other', 'workingDirectory': str(self.home)})
        first = await self.started(await self.submit('other', workspace['id']))
        second = await self.started(await self.submit('general', general['id']))
        self.service.store.db.execute('UPDATE tasks SET updated_at=updated_at+10 WHERE id=?', (first['id'],))
        self.service.store.db.commit()
        page = self.service.store.conversations(limit=1)
        self.assertEqual(page['data'][0]['id'], first['id'])
        next_page = self.service.store.conversations(page['nextCursor'], limit=1)
        self.assertEqual(next_page['data'][0]['id'], second['id'])
        filtered = await self.get('/v1/chats?workspaceId=' + workspace['id'])
        self.assertEqual(len(filtered['data']), 1)


class ConversationStoreTests(unittest.TestCase):
    def test_error_logging_has_no_content_and_settings_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                workspace = store.save_workspace({'name': 'Errors', 'workingDirectory': directory, 'diagnosticLogLevel': 'errors'})
                for invalid in ({'diagnosticLogLevel': 'verbose'}, {'executionDetail': 'invalid'}):
                    with self.assertRaises(ValueError):
                        store.save_workspace(invalid, workspace['id'])
                task = store.submit({'workspaceId': workspace['id'], 'prompt': 'PRIVATE-PROMPT', 'browserMode': 'dedicated'})[0]
                root = Path(directory) / 'logs' / 'workspaces' / workspace['id']
                self.assertFalse(root.exists())
                store.update(task['id'], status='failed', error='PRIVATE-ERROR')
                content = (root / 'diagnostics.jsonl').read_text()
                self.assertIn('failed', content)
                self.assertNotIn('PRIVATE-', content)
            finally:
                store.close()

    def test_restart_recovers_dispatched_queue_receipt_without_duplicate_task(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = Store(home)
            first = store.submit({'prompt': 'one', 'workingDirectory': directory, 'browserMode': 'dedicated'})[0]
            store.update(first['id'], status='completed', thread_id='native')
            submission = str(uuid.uuid4())
            successor = store.submit({'prompt': 'two', 'workingDirectory': directory, 'browserMode': 'dedicated', 'resumeTaskId': first['id']}, 'followup:' + submission)[0]
            store.db.execute("INSERT INTO followups(id,conversation_id,body,created_at) VALUES (?,?,'{}',0)", (submission, first['conversationId']))
            store.db.commit()
            store.close()
            reopened = Store(home)
            try:
                self.assertEqual(len(reopened.conversation_tasks(first['conversationId'])), 2)
                self.assertEqual(reopened.conversation(first['conversationId'])['followups'], [])
                receipt = reopened.db.execute('SELECT status,task_id FROM followups WHERE id=?', (submission,)).fetchone()
                self.assertEqual((receipt['status'], receipt['task_id']), ('dispatched', successor['id']))
            finally:
                reopened.close()

    def test_migration_recovers_parent_and_thread_identity_and_holds_pending_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = Store(home)
            first = store.submit({'prompt': 'one', 'workingDirectory': directory, 'browserMode': 'dedicated'})[0]
            store.update(first['id'], thread_id='same-native', status='completed')
            second = store.submit({'prompt': 'two', 'workingDirectory': directory, 'browserMode': 'dedicated', 'resumeTaskId': first['id']})[0]
            store.update(second['id'], thread_id='same-native', status='completed')
            third = store.submit({'prompt': 'three', 'workingDirectory': directory, 'browserMode': 'dedicated', 'resumeTaskId': second['id']})[0]
            store.db.execute('UPDATE tasks SET conversation_id=NULL')
            store.db.execute("INSERT INTO followups(id,conversation_id,body,created_at) VALUES (?,?,'{}',0)", (str(uuid.uuid4()), first['id']))
            store.db.commit()
            store.close()
            reopened = Store(home)
            try:
                self.assertEqual({reopened.get(task['id'])['conversationId'] for task in [first, second, third]}, {first['id']})
                self.assertEqual(reopened.get(third['id'])['status'], 'interrupted')
                self.assertTrue(reopened.conversation(first['id'])['queueHeld'])
                self.assertEqual(reopened.workspace(first['workspaceId'])['executionDetail'], 'standard')
                self.assertEqual(reopened.workspace(first['workspaceId'])['diagnosticLogLevel'], 'off')
            finally:
                reopened.close()

    def test_event_pages_use_snapshot_boundary_and_global_event_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                task = store.submit({'prompt': 'one', 'workingDirectory': directory, 'browserMode': 'dedicated'})[0]
                for index in range(420):
                    store.event(task['id'], 'agent', {'method': 'example', 'params': {'index': index}})
                cursor = store.conversation(task['conversationId'])['eventCursor']
                store.event(task['id'], 'agent', {'method': 'later'})
                events, after = [], 0
                while after < cursor:
                    page = store.conversation_events(task['conversationId'], after, cursor)
                    self.assertLessEqual(len(page), 200)
                    events.extend(page)
                    after = page[-1]['id']
                self.assertEqual(len(events), 421)
                self.assertEqual(len({event['id'] for event in events}), 421)
                self.assertEqual(len(store.conversation_events(task['conversationId'], cursor)), 1)
            finally:
                store.close()

    def test_logging_levels_capture_per_task_rotate_isolate_and_tolerate_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = Store(home)
            try:
                workspace = store.save_workspace({'name': 'Debug', 'workingDirectory': directory, 'diagnosticLogLevel': 'debug', 'executionDetail': 'full'})
                task = store.submit({'prompt': 'PRIVATE-PROMPT', 'workspaceId': workspace['id'], 'browserMode': 'dedicated'})[0]
                log = home / 'logs' / 'workspaces' / workspace['id'] / 'diagnostics.jsonl'
                self.assertIn('PRIVATE-PROMPT', log.read_text())
                store.save_workspace({'diagnosticLogLevel': 'info'}, workspace['id'])
                store.event(task['id'], 'agent', {'method': 'tool', 'params': {'output': 'PRIVATE-OUTPUT'}})
                self.assertIn('PRIVATE-OUTPUT', log.read_text())
                newer = store.submit({'prompt': 'PRIVATE-NEW', 'workspaceId': workspace['id'], 'browserMode': 'dedicated'})[0]
                store.event(newer['id'], 'agent', {'method': 'error', 'params': {'message': 'PRIVATE-ERROR'}})
                self.assertNotIn('PRIVATE-NEW', log.read_text())
                self.assertNotIn('PRIVATE-ERROR', log.read_text())
                other = store.submit({'prompt': 'PRIVATE-OFF', 'workingDirectory': directory, 'browserMode': 'dedicated'})[0]
                # Directory default may select Debug; use an explicitly separate Off workspace.
                off = store.save_workspace({'name': 'Off', 'workingDirectory': directory})
                store.submit({'prompt': 'OFF', 'workspaceId': off['id'], 'browserMode': 'dedicated'})
                self.assertFalse((home / 'logs' / 'workspaces' / off['id']).exists())
                store.diagnostics.max_bytes = 1
                for index in range(8):
                    store.event(task['id'], 'agent', {'method': 'tool', 'params': {'index': index}})
                self.assertEqual(len(list(log.parent.glob('*.jsonl'))), 6)
                self.assertIn('"index": 7', log.read_text())
                store.diagnostics.root = log  # Cannot create a directory below a file.
                store.event(task['id'], 'agent', {'method': 'tool', 'params': {'value': 'still saved'}})
                self.assertEqual(store.events(task['id'])[-1]['data']['params']['value'], 'still saved')
            finally:
                store.close()
