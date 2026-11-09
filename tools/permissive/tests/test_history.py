import base64
import asyncio
import json
import threading
import unittest
import uuid
from unittest.mock import patch

import test_conversations as conversations
from permissive.attachments import Attachments
from permissive.history import all_events, native_history, restored_history, points, bounded_seed, execution_evidence, recover_failed_import, upgrade_unprojected_history, EVIDENCE_PREFIX, EVIDENCE_BYTES
from permissive.store import Store


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = conversations.ConversationTests.asyncSetUp
    asyncTearDown = conversations.ConversationTests.asyncTearDown
    post = conversations.ConversationTests.post
    get = conversations.ConversationTests.get
    started = conversations.ConversationTests.started
    finish = conversations.ConversationTests.finish

    def fixture(self, attachments=False):
        store = self.service.store
        workspace = store.workspaces()['data'][0]['id']
        files = []
        if attachments:
            files = [self.service.attachments.upload({'workspaceId': workspace, 'name': 'test.txt', 'data': base64.b64encode(b'retained file').decode()})['id']]
        first = store.submit({'prompt': 'First prompt', 'workspaceId': workspace, 'workingDirectory': str(self.home), 'browserMode': 'dedicated', 'attachments': files})[0]
        self.service.attachments.claim(files, first['id'])
        store.update(first['id'], thread_id=str(uuid.uuid4()))
        self.agent(first, 'turn/started', {'turn': {'id': 'turn1'}})
        self.agent(first, 'item/reasoning/textDelta', {'itemId': 'reasoning', 'delta': 'DELETED-REASONING'})
        self.agent(first, 'item/agentMessage/delta', {'itemId': 'a', 'delta': 'First'})
        self.agent(first, 'item/completed', {'item': {'id': 'a', 'type': 'agentMessage', 'text': 'First answer'}})
        store.update(first['id'], status='completed', result='First answer')
        second = store.submit({'prompt': 'Later secret prompt', 'workspaceId': workspace, 'workingDirectory': str(self.home), 'browserMode': 'dedicated', 'resumeTaskId': first['id']})[0]
        store.update(second['id'], thread_id=store.get(first['id'])['threadId'])
        self.agent(second, 'turn/started', {'turn': {'id': 'turn2'}})
        self.agent(second, 'item/completed', {'item': {'id': 'a', 'type': 'agentMessage', 'text': 'Later secret answer'}})
        store.update(second['id'], status='completed', result='Later secret answer')
        return store.get(first['id']), store.get(second['id'])

    def agent(self, task, method, params):
        self.service.store.event(task['id'], 'agent', {'method': method, 'params': params})

    def messages(self, chat):
        return points(chat['tasks'], all_events(self.service.store, chat['id']))

    async def edit(self, chat, point, delete=False, submission=None):
        return await self.post('/v1/chats/' + chat + ('/delete-from' if delete else '/fork'),
            {'taskId': point['taskId'], 'messageId': point['messageId'], 'submissionId': submission or str(uuid.uuid4()),
             **({'confirm': chat} if delete else {})})

    async def cleanup_finished(self):
        for _ in range(300):
            self.service.history_wakeup.set()
            if not self.service.store.db.execute('SELECT 1 FROM history_deletions').fetchone():
                return
            await asyncio.sleep(.01)
        self.fail('Background cleanup did not finish')

    async def test_slow_copy_keeps_health_responsive_and_concurrent_retries_start_once(self):
        from permissive.reply_versions import copy_history
        first, _ = self.fixture(True)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow_copy(*args):
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Fixture copy timed out')
            return copy_history(*args)
        body = dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()), confirm=first['conversationId'])
        path = '/v1/chats/' + first['conversationId'] + '/regenerate'
        try:
            with patch('permissive.reply_versions.copy_history', side_effect=slow_copy):
                request = asyncio.create_task(self.post(path, body))
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                retry = asyncio.create_task(self.post(path, body))
                self.assertTrue((await asyncio.wait_for(self.get('/v1/health'), .5))['ok'])
                rejected = await self.client.post('/v1/tasks', json={'prompt': 'Race with copy', 'resumeTaskId': first['id']}, headers={'X-Test-Sign': '1'})
                self.assertEqual(rejected.status, 409)
                release.set()
                result, repeated = await asyncio.gather(request, retry)
                self.assertEqual(result['id'], repeated['id'])
                self.assertEqual(len(calls), 1)
                current = result['tasks'][-1]
                await self.started(current)
                self.assertEqual(len(self.service.jobs), 1)
                await self.finish(current)
        finally:
            release.set()

    async def test_disconnected_request_does_not_abandon_regeneration(self):
        from permissive.history import build_replacement
        first, _ = self.fixture()
        entered, release = threading.Event(), threading.Event()
        def slow_copy(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('Fixture copy timed out')
            return build_replacement(*args)
        body = dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()), confirm=first['conversationId'], _operation='regenerate')
        from permissive.history import operation_key
        key = operation_key(first['conversationId'], body, True)
        try:
            with patch('permissive.history.build_replacement', side_effect=slow_copy):
                caller = asyncio.create_task(self.service.copy_history(first['conversationId'], body, 'regenerate', key))
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError): await caller
                operation = self.service.history_operations[key]
                release.set()
                replacement = await operation
                current = self.service.store.conversation_tasks(replacement)[-1]
                await self.started(current)
                await self.finish(current)
        finally:
            release.set()

    def test_internal_history_scan_uses_one_query_for_large_conversations(self):
        first, _ = self.fixture()
        with self.service.store.db:
            self.service.store.db.executemany('INSERT INTO events(task_id,kind,data,created_at) VALUES (?,?,?,?)',
                ((first['id'], 'inference', '{"phase":"output"}', 1) for _ in range(1200)))
        queries = []
        self.service.store.db.set_trace_callback(queries.append)
        try:
            events = all_events(self.service.store, first['conversationId'])
        finally:
            self.service.store.db.set_trace_callback(None)
        self.assertGreater(len(events), 1200)
        self.assertEqual(len([query for query in queries if 'FROM events' in query]), 1)

    async def test_fork_at_user_and_assistant_is_independent_persistent_and_retryable(self):
        first, second = self.fixture(True)
        original = self.service.store.conversation(first['conversationId'])
        artifact = self.home / 'artifacts' / first['id'] / 'retained.txt'
        artifact.parent.mkdir(parents=True)
        artifact.write_text('retained artifact', encoding='utf-8')
        messages = self.messages(original)
        submission = str(uuid.uuid4())
        fork = await self.edit(original['id'], messages[1], submission=submission)
        self.assertEqual((await self.edit(original['id'], messages[1], submission=submission))['id'], fork['id'])
        self.assertEqual([point['text'] for point in self.messages(fork)], ['First prompt', 'First answer'])
        self.assertTrue(fork['title'].endswith(' — Fork'))
        self.assertEqual(len(self.service.store.conversation_tasks(original['id'])), 2)
        self.assertNotEqual(fork['tasks'][0]['threadId'], first['threadId'])
        self.assertIsNone(fork['tasks'][0]['threadId'])
        copied_artifact = self.home / 'artifacts' / fork['tasks'][0]['id'] / 'retained.txt'
        self.assertEqual(copied_artifact.read_text(encoding='utf-8'), 'retained artifact')
        old_attachment = first['request']['attachments'][0]
        new_attachment = fork['tasks'][0]['request']['attachments'][0]
        self.assertNotEqual(new_attachment, old_attachment)
        self.assertEqual(self.service.attachments.path(new_attachment).read_bytes(), b'retained file')
        await self.post('/v1/chats/' + original['id'] + '/secure-delete', {'confirm': original['id'], 'passes': 7})
        self.assertTrue(self.service.attachments.path(new_attachment).is_file())
        self.assertTrue(copied_artifact.is_file())
        reopened = Store(self.home)
        try:
            seed = reopened.history_seed(fork['tasks'][-1]['id'])
            self.assertEqual([point['text'] for point in seed], ['First prompt', 'First answer'])
            self.assertNotIn('Later secret', json.dumps(native_history(self.home, seed)))
        finally:
            reopened.close()
        prompt_fork = await self.edit(fork['id'], self.messages(fork)[0])
        self.assertEqual([point['text'] for point in self.messages(prompt_fork)], ['First prompt'])

    async def test_delete_response_preserves_prompt_then_continues_with_only_retained_context(self):
        first, second = self.fixture(True)
        chat = self.service.store.conversation(first['conversationId'])
        target, submission = self.messages(chat)[1], str(uuid.uuid4())
        old_mobile = await self.get('/v1/chats/' + chat['id'] + '/events?format=json&view=mobile')
        self.assertIn('Later secret',json.dumps(old_mobile))
        retained = await self.edit(chat['id'], target, True, submission)
        mobile = await self.get('/v1/chats/' + retained['id'] + '/events?format=json&view=mobile')
        self.assertNotIn('Later secret',json.dumps(mobile))
        repeated = await self.edit(chat['id'], target, True, submission)
        self.assertEqual(repeated['tasks'], retained['tasks'])
        self.assertNotEqual(retained['id'], chat['id'])
        self.assertEqual([point['text'] for point in self.messages(retained)], ['First prompt'])
        self.assertEqual(retained['followups'], [])
        await self.cleanup_finished()
        self.assertNotIn('Later secret', '\n'.join(self.service.store.db.iterdump()))
        self.assertNotIn('DELETED-REASONING', '\n'.join(self.service.store.db.iterdump()))
        latest = retained['tasks'][-1]
        continuation = await self.post('/v1/tasks/' + latest['id'] + '/continue', {'prompt': 'New branch', 'submissionId': str(uuid.uuid4())})
        self.assertEqual([message['text'] for message in self.service.store.history_seed(continuation['id'])], ['First prompt'])
        await self.started(continuation)
        await self.finish(continuation)

    async def test_edit_preserves_later_messages_files_ids_and_future_context(self):
        first, second = self.fixture(True)
        chat = self.service.store.conversation(first['conversationId'])
        target = self.messages(chat)[1]
        mobile_path = '/v1/chats/' + chat['id'] + '/events?format=json&view=mobile&through=' + str(chat['eventCursor'])
        await self.get(mobile_path)
        body = dict(taskId=target['taskId'], messageId=target['messageId'], submissionId=str(uuid.uuid4()), text='Revised answer 📝')
        result = await self.post('/v1/chats/' + chat['id'] + '/edit-message', body)
        expected = ['First prompt', 'Revised answer 📝', 'Later secret prompt', 'Later secret answer']
        self.assertEqual([point['text'] for point in self.messages(result)], expected)
        self.assertEqual([task['id'] for task in result['tasks']], [first['id'], second['id']])
        self.assertEqual(result['tasks'][0]['request']['attachments'], first['request']['attachments'])
        self.assertEqual(result['tasks'][0]['threadId'], first['threadId'])
        self.assertEqual(result['tasks'][0]['result'], 'Revised answer 📝')
        self.assertIn('Revised answer', json.dumps(await self.get(mobile_path)))
        repeated = await self.post('/v1/chats/' + chat['id'] + '/edit-message', body)
        self.assertEqual(repeated['eventCursor'], result['eventCursor'])
        seed = self.service.store.history_seed(second['id'])
        self.assertEqual([message['text'] for message in seed], expected)
        reopened = Store(self.home)
        try:
            self.assertEqual(reopened.history_seed(second['id']), seed)
        finally:
            reopened.close()
        continuation = await self.post('/v1/tasks/' + second['id'] + '/continue', {'prompt': 'Use the revision'})
        self.assertEqual(self.service.store.history_seed(continuation['id']), seed)
        await self.started(continuation)
        await self.finish(continuation)

    async def test_edit_prompt_and_native_steering(self):
        first, second = self.fixture()
        submission = str(uuid.uuid4())
        self.agent(second, 'item/completed', {'item': {'id': 'native-steer', 'clientId': submission, 'type': 'userMessage'}})
        self.service.store.event(second['id'], 'steering', {'submissionId': submission, 'prompt': 'Original steer', 'attachments': []})
        chat = self.service.store.conversation(first['conversationId'])
        for target, text in [(self.messages(chat)[0], 'Revised prompt'), (self.messages(chat)[-1], 'Revised steer')]:
            chat = await self.post('/v1/chats/' + chat['id'] + '/edit-message', dict(taskId=target['taskId'], messageId=target['messageId'], submissionId=str(uuid.uuid4()), text=text))
        self.assertEqual(chat['tasks'][0]['request']['prompt'], 'Revised prompt')
        self.assertEqual([point['text'] for point in self.messages(chat)], ['Revised prompt', 'First answer', 'Later secret prompt', 'Later secret answer', 'Revised steer'])
        self.assertNotIn('Original steer', json.dumps(all_events(self.service.store, chat['id'])))

    async def test_edit_legacy_result_preserves_tool_evidence_and_mobile_replay(self):
        first, second = self.fixture()
        for event in all_events(self.service.store, first['conversationId']):
            if event['taskId'] == second['id'] and event['kind'] == 'agent':
                self.service.store.segments.replace_event(event['id'], None)
        self.service.store.db.execute("DELETE FROM events WHERE task_id=? AND kind='agent'", (second['id'],))
        self.service.store.db.commit()
        self.agent(second, 'item/completed', {'item': {'id': 'tool', 'type': 'commandExecution', 'command': 'read file', 'aggregatedOutput': 'Retained evidence'}})
        await self.get('/v1/chats/' + first['conversationId'] + '/events?format=json&view=mobile')
        chat = await self.post('/v1/chats/' + first['conversationId'] + '/edit-message', dict(taskId=second['id'], messageId='result', submissionId=str(uuid.uuid4()), text='Revised legacy reply'))
        self.assertEqual(self.messages(chat)[-1]['text'], 'Revised legacy reply')
        seed = self.service.store.history_seed(second['id'])
        self.assertIn('Retained evidence', seed[-1]['text'])
        self.assertIn('Revised legacy reply', json.dumps(seed))
        self.assertEqual(chat['tasks'][-1]['result'], 'Revised legacy reply')

    async def test_edit_allows_another_chat_to_keep_running(self):
        first, _ = self.fixture()
        other = await self.post('/v1/tasks', {'prompt': 'Another active chat', 'workingDirectory': str(self.home)})
        await self.started(other)
        result = await self.post('/v1/chats/' + first['conversationId'] + '/edit-message', dict(taskId=first['id'], messageId='prompt', submissionId=str(uuid.uuid4()), text='Revised prompt'))
        self.assertEqual(result['tasks'][0]['request']['prompt'], 'Revised prompt')
        self.assertEqual(self.service.store.get(other['id'])['status'], 'running')
        await self.finish(other)

    async def test_edit_rejects_empty_foreign_busy_and_reused_submission(self):
        first, second = self.fixture()
        chat = first['conversationId']
        body = dict(taskId=first['id'], messageId='prompt', submissionId=str(uuid.uuid4()), text='Revised')
        for changes in ({'text': '  '}, {'taskId': 'foreign'}, {'messageId': 'missing'}):
            response = await self.client.post('/v1/chats/' + chat + '/edit-message', json={**body, **changes}, headers={'X-Test-Sign': '1'})
            self.assertEqual(response.status, 400)
        self.service.store.update(second['id'], status='paused')
        response = await self.client.post('/v1/chats/' + chat + '/edit-message', json=body, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 400)
        self.service.store.update(second['id'], status='completed')
        await self.post('/v1/chats/' + chat + '/edit-message', body)
        response = await self.client.post('/v1/chats/' + chat + '/edit-message', json={**body, 'text': 'Different'}, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 400)
        await self.post('/v1/chats/' + chat + '/secure-delete', {'confirm': chat, 'passes': 7})
        self.assertEqual(self.service.store.db.execute('SELECT COUNT(*) FROM message_edits').fetchone()[0], 0)

    async def test_delete_one_message_retains_later_turns_and_is_retryable(self):
        first, second = self.fixture(True)
        original = self.service.store.conversation(first['conversationId'])
        target = self.messages(original)[1]
        body = dict(taskId=first['id'], messageId=target['messageId'], submissionId=str(uuid.uuid4()), confirm=original['id'])
        artifact = self.home / 'artifacts' / second['id'] / 'retained.txt'
        artifact.parent.mkdir(parents=True)
        artifact.write_text('Retained output', encoding='utf-8')
        result = await self.post('/v1/chats/' + original['id'] + '/delete-message', body)
        self.assertEqual([point['text'] for point in self.messages(result)], ['First prompt', 'Later secret prompt', 'Later secret answer'])
        repeated = await self.post('/v1/chats/' + original['id'] + '/delete-message', body)
        self.assertEqual(repeated['tasks'], result['tasks'])
        await self.cleanup_finished()
        self.assertNotIn('First answer', '\n'.join(self.service.store.db.iterdump()))
        saved = self.service.store.segments.connection(self.service.store.segments.info(result['id'])['file_id'])
        self.assertNotIn('First answer', '\n'.join(saved.iterdump()))
        self.assertEqual((self.home / 'artifacts' / result['tasks'][1]['id'] / 'retained.txt').read_text(), 'Retained output')
        self.assertEqual(self.service.attachments.path(result['tasks'][0]['request']['attachments'][0]).read_bytes(), b'retained file')
        self.assertEqual([message['text'] for message in self.service.store.history_seed(result['tasks'][-1]['id'])], ['First prompt', 'Later secret prompt', 'Later secret answer'])

    async def test_prompt_edit_and_delete_update_native_input_copies(self):
        first, second = self.fixture()
        for method in ('item/started', 'item/completed'):
            self.agent(first, method, {'item': {'id': 'original-input', 'type': 'userMessage', 'content': [{'type': 'text', 'text': 'First prompt'}]}})
        edited = await self.post('/v1/chats/' + first['conversationId'] + '/edit-message', dict(taskId=first['id'], messageId='prompt', submissionId=str(uuid.uuid4()), text='Revised private prompt'))
        events = all_events(self.service.store, edited['id'])
        inputs = [event['data']['params']['item'] for event in events if event['kind'] == 'agent' and event['data'].get('params', {}).get('item', {}).get('type') == 'userMessage']
        self.assertTrue(inputs)
        self.assertTrue(all(item['content'][0]['text'] == 'Revised private prompt' for item in inputs))
        removed = await self.post('/v1/chats/' + edited['id'] + '/delete-message', dict(taskId=first['id'], messageId='prompt', submissionId=str(uuid.uuid4()), confirm=edited['id']))
        database = self.service.store.segments.connection(self.service.store.segments.info(removed['id'])['file_id'])
        self.assertNotIn('Revised private prompt', '\n'.join(database.iterdump()))
        self.assertIn('Later secret answer', json.dumps(removed))

    async def test_delete_only_prompt_removes_its_attachment_but_keeps_reply(self):
        first, second = self.fixture(True)
        old_file = self.service.attachments.path(first['request']['attachments'][0])
        result = await self.post('/v1/chats/' + first['conversationId'] + '/delete-message', dict(taskId=first['id'], messageId='prompt', submissionId=str(uuid.uuid4()), confirm=first['conversationId']))
        self.assertEqual([point['text'] for point in self.messages(result)], ['First answer', 'Later secret prompt', 'Later secret answer'])
        self.assertEqual(result['tasks'][0]['request']['attachments'], [])
        await self.cleanup_finished()
        self.assertFalse(old_file.exists())

    async def test_saved_response_alias_edits_combined_reply_and_deletes_it_independently(self):
        first, second = self.fixture()
        self.agent(first, 'item/completed', {'item': {'id': 'other', 'type': 'agentMessage', 'text': 'Final part'}})
        self.service.store.update(first['id'], result='First answer\n\nFinal part')
        result = await self.post('/v1/chats/' + first['conversationId'] + '/edit-message', dict(taskId=first['id'], messageId='response', text='Edited whole saved response', submissionId=str(uuid.uuid4())))
        self.assertEqual([point['text'] for point in self.messages(result)], ['First prompt', 'Edited whole saved response', 'Later secret prompt', 'Later secret answer'])
        result = await self.post('/v1/chats/' + first['conversationId'] + '/delete-message', dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()), confirm=first['conversationId']))
        self.assertEqual([point['text'] for point in self.messages(result)], ['First prompt', 'Later secret prompt', 'Later secret answer'])

    async def test_regenerate_replaces_response_from_original_prompt_without_duplicate_submission(self):
        first, second = self.fixture(True)
        body = dict(taskId=second['id'], messageId='response', submissionId=str(uuid.uuid4()), confirm=first['conversationId'])
        result = await self.post('/v1/chats/' + first['conversationId'] + '/regenerate', body)
        current = result['tasks'][-1]
        await self.started(current)
        self.assertEqual(current['request']['prompt'], 'Later secret prompt')
        self.assertEqual([message['text'] for message in self.service.store.history_seed(current['id'])], ['First prompt', 'First answer'])
        retry = await self.post('/v1/chats/' + first['conversationId'] + '/regenerate', body)
        self.assertEqual(retry['tasks'][-1]['id'], current['id'])
        self.assertEqual(len(self.service.jobs), 1)
        await self.finish(current)
        final = self.service.store.conversation(result['id'])
        self.assertFalse(final['title'].endswith(' — Fork'))
        self.assertEqual([point['text'] for point in self.messages(final)], ['First prompt', 'First answer', 'Later secret prompt', 'Later secret prompt'])

    async def test_regenerate_steered_response_retains_earlier_exchanges_in_same_task(self):
        first, _ = self.fixture(True)
        submission = str(uuid.uuid4())
        self.service.store.event(first['id'], 'steering', {'submissionId': submission, 'prompt': 'Rerun this later input', 'attachments': []})
        self.agent(first, 'item/completed', {'item': {'id': 'native-steer', 'clientId': submission, 'type': 'userMessage'}})
        self.agent(first, 'item/completed', {'turnId': 'turn1', 'item': {'id': 'steered-answer', 'type': 'agentMessage', 'text': 'OLD STEERED ANSWER'}})
        self.service.store.update(first['id'], result='OLD STEERED ANSWER')
        chat = self.service.store.conversation(first['conversationId'])
        target = next(point for point in self.messages(chat) if point['text'] == 'OLD STEERED ANSWER')
        body = dict(taskId=first['id'], messageId=target['messageId'], submissionId=str(uuid.uuid4()), confirm=chat['id'])
        result = await self.post('/v1/chats/' + chat['id'] + '/regenerate', body)
        current = result['tasks'][-1]
        await self.started(current)
        self.assertEqual(current['request']['prompt'], 'Rerun this later input')
        self.assertEqual([message['text'] for message in self.service.store.history_seed(current['id'])], ['First prompt', 'First answer'])
        self.assertEqual([point['text'] for point in self.messages(result)], ['First prompt', 'First answer', 'Rerun this later input'])
        self.assertEqual(len(result['tasks']), 2)
        self.assertEqual((await self.post('/v1/chats/' + chat['id'] + '/regenerate', body))['id'], result['id'])
        await self.finish(current)
        self.assertEqual([point['text'] for point in self.messages(self.service.store.conversation(result['id']))],
                         ['First prompt', 'First answer', 'Rerun this later input', 'Rerun this later input'])

    async def test_regenerate_saved_reply_uses_latest_user_input_and_retains_its_attachment(self):
        first, _ = self.fixture(True)
        file = self.service.attachments.upload({'workspaceId': first['workspaceId'], 'name': 'steer.txt', 'data': base64.b64encode(b'steered attachment').decode()})['id']
        self.service.attachments.claim([file], first['id'])
        self.service.store.event(first['id'], 'steering', {'submissionId': str(uuid.uuid4()), 'prompt': 'Latest user input', 'attachments': [file]})
        self.agent(first, 'item/completed', {'item': {'id': 'latest-answer', 'type': 'agentMessage', 'text': 'Old latest answer'}})
        self.service.store.update(first['id'], result='First answer\n\nOld latest answer')
        result = await self.post('/v1/chats/' + first['conversationId'] + '/regenerate',
            dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()), confirm=first['conversationId']))
        current = result['tasks'][-1]
        await self.started(current)
        self.assertEqual(current['request']['prompt'], 'Latest user input')
        self.assertEqual([message['text'] for message in self.service.store.history_seed(current['id'])], ['First prompt', 'First answer'])
        self.assertEqual(self.service.attachments.path(current['request']['attachments'][0]).read_bytes(), b'steered attachment')
        self.assertEqual(current['request']['attachments'][0], file)
        await self.finish(current)

    async def test_regenerate_starts_once_without_waiting_for_unavailable_cleanup(self):
        first, _ = self.fixture(True)
        body = dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()), confirm=first['conversationId'])
        async def unavailable(*args):
            raise RuntimeError('Storage temporarily unavailable')
        self.runtime.erase_history = unavailable
        response = await self.client.post('/v1/chats/' + first['conversationId'] + '/regenerate', json=body, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 200)
        result = await response.json()
        current = result['tasks'][-1]
        await self.started(current)
        retry = await self.post('/v1/chats/' + first['conversationId'] + '/regenerate', body)
        self.assertEqual(retry['id'], result['id'])
        self.assertEqual(len(self.service.jobs), 1)
        self.assertFalse(self.service.store.db.execute('SELECT 1 FROM history_deletions').fetchone())
        self.assertEqual(self.service.store.conversation(first['conversationId'])['id'], first['conversationId'])
        self.assertFalse(self.service.pending_history)
        self.assertEqual([message['text'] for message in self.service.store.history_seed(current['id'])], [])
        self.assertEqual(self.service.attachments.path(current['request']['attachments'][0]).read_bytes(), b'retained file')
        await self.finish(current)
        async def ready(*args):
            pass
        self.runtime.erase_history = ready
        await self.cleanup_finished()
        self.assertEqual((await self.post('/v1/chats/' + first['conversationId'] + '/regenerate', body))['id'], result['id'])

    async def test_delete_first_prompt_leaves_an_empty_continuable_chat(self):
        first, _ = self.fixture()
        chat = self.service.store.conversation(first['conversationId'])
        result = await self.edit(chat['id'], self.messages(chat)[0], True)
        self.assertEqual(self.messages(result), [])
        self.assertEqual(self.service.store.history_seed(result['tasks'][-1]['id']), [])
        task = await self.post('/v1/tasks/' + result['tasks'][-1]['id'] + '/continue', {'prompt': 'Start again'})
        await self.started(task)
        await self.finish(task)

    async def test_steering_native_echo_keeps_text_and_files_at_its_cutoff(self):
        first, _ = self.fixture()
        submission = str(uuid.uuid4())
        self.agent(first, 'item/completed', {'item': {'id': 'native-steer', 'clientId': submission, 'type': 'userMessage'}})
        self.service.store.event(first['id'], 'steering', {'submissionId': submission, 'prompt': 'Steer here', 'attachments': []})
        chat = self.service.store.conversation(first['conversationId'])
        target = next(point for point in self.messages(chat) if point['text'] == 'Steer here')
        fork = await self.edit(chat['id'], target)
        self.assertEqual([point['text'] for point in self.messages(fork)], ['First prompt', 'First answer', 'Steer here'])

    async def test_invalid_foreign_and_incomplete_points_and_busy_chat_rejected(self):
        first, _ = self.fixture()
        chat = self.service.store.conversation(first['conversationId'])
        target = self.messages(chat)[1]
        body = {'taskId': target['taskId'], 'messageId': target['messageId'], 'submissionId': str(uuid.uuid4())}
        for changed in ({'taskId': 'foreign'}, {'messageId': '999999'}):
            response = await self.client.post('/v1/chats/' + chat['id'] + '/fork', json={**body, **changed}, headers={'X-Test-Sign': '1'})
            self.assertEqual(response.status, 400)
        self.service.store.update(first['id'], status='paused')
        response = await self.client.post('/v1/chats/' + chat['id'] + '/fork', json=body, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 400)

    async def test_interrupted_delete_can_be_retried_without_erasing_the_replacement(self):
        first, _ = self.fixture(True)
        chat = self.service.store.conversation(first['conversationId'])
        target = self.messages(chat)[1]
        body = {'taskId': target['taskId'], 'messageId': target['messageId'], 'submissionId': str(uuid.uuid4()), 'confirm': chat['id']}
        attempted = asyncio.Event()
        async def unavailable(*args):
            attempted.set()
            raise RuntimeError('fixture unavailable')
        self.runtime.erase_history = unavailable
        result = await self.post('/v1/chats/' + chat['id'] + '/delete-from', body)
        await asyncio.wait_for(attempted.wait(), 2)
        self.assertEqual([point['text'] for point in self.messages(result)], ['First prompt'])
        self.assertEqual((await self.post('/v1/chats/' + chat['id'] + '/delete-from', body))['id'], result['id'])
        listed = await self.get('/v1/chats')
        self.assertEqual([item['conversationId'] for item in listed['data']], [result['id']])
        self.assertEqual((await self.get('/v1/chats/' + result['id']))['tasks'], result['tasks'])
        response = await self.client.get('/v1/tasks/' + first['id'], headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 410)
        self.assertEqual((await self.get('/v1/health'))['pendingHistoryDeletions'], 1)
        async def ready(*args):
            pass
        self.runtime.erase_history = ready
        await self.cleanup_finished()
        self.assertEqual(self.service.store.conversation(result['id'])['tasks'], result['tasks'])

    async def test_seven_pass_worker_does_not_block_api_or_new_work(self):
        first, _ = self.fixture(True)
        entered, release = threading.Event(), threading.Event()
        from permissive.secure_delete import overwrite_file
        def slow_overwrite(path, root, **kwargs):
            if path.parent.name == 'secure-erase-files':
                entered.set()
                if not release.wait(5):
                    raise OSError('test did not release worker')
            return overwrite_file(path, root, **kwargs)
        try:
            with patch('permissive.secure_delete.overwrite_file', side_effect=slow_overwrite):
                chat = self.service.store.conversation(first['conversationId'])
                result = await self.edit(chat['id'], self.messages(chat)[1], True)
                self.assertNotEqual(result['id'], chat['id'])
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertTrue((await asyncio.wait_for(self.get('/v1/health'), .5))['ok'])
                task = await asyncio.wait_for(self.post('/v1/tasks/' + result['tasks'][-1]['id'] + '/continue', {'prompt': 'Continue while cleanup runs'}), .5)
                await self.started(task)
                self.assertTrue(self.service.store.db.execute('SELECT 1 FROM history_deletions').fetchone())
                release.set()
                await self.finish(task)
                await self.cleanup_finished()
        finally:
            release.set()

    async def test_pending_cleanup_survives_restart_without_recreating_chat(self):
        first, _ = self.fixture(True)
        attempted = asyncio.Event()
        async def unavailable(*args):
            attempted.set()
            raise RuntimeError('Offline guest')
        self.runtime.erase_history = unavailable
        body = dict(taskId=first['id'], messageId='response', submissionId=str(uuid.uuid4()), confirm=first['conversationId'])
        result = await self.post('/v1/chats/' + first['conversationId'] + '/delete-message', body)
        await asyncio.wait_for(attempted.wait(), 2)
        signer = self.client.signer
        await self.client.close()
        self.runtime = conversations.ControlledRuntime()
        self.service = conversations.Service(self.home, self.runtime)
        self.client = conversations.SignedTestClient(conversations.TestServer(self.service.app()), signer)
        await self.client.start_server()
        await self.cleanup_finished()
        self.assertEqual((await self.get('/v1/chats'))['data'][0]['conversationId'], result['id'])
        self.assertEqual((await self.post('/v1/chats/' + first['conversationId'] + '/delete-message', body))['id'], result['id'])
        self.assertEqual(self.service.store.conversation(result['id'])['tasks'], result['tasks'])

    async def test_upgrade_publishes_an_old_interrupted_replacement_without_waiting(self):
        from permissive.history import copy_prefix, edit_path, save_edit
        first, _ = self.fixture(True)
        chat = self.service.store.conversation(first['conversationId'])
        target = self.messages(chat)[1]
        body = dict(taskId=first['id'], messageId=target['messageId'], submissionId=str(uuid.uuid4()), confirm=chat['id'])
        replacement = copy_prefix(self.service.store, chat['id'], body, False, 'history-replacement:' + chat['id'] + ':' + body['submissionId'])
        manifest = edit_path(self.service.store, chat['id'])
        manifest.parent.mkdir(exist_ok=True)
        save_edit(manifest, {'replacement': replacement, 'body': body})
        signer = self.client.signer
        await self.client.close()
        self.runtime = conversations.ControlledRuntime()
        self.service = conversations.Service(self.home, self.runtime)
        self.client = conversations.SignedTestClient(conversations.TestServer(self.service.app()), signer)
        await self.client.start_server()
        result = await self.post('/v1/chats/' + chat['id'] + '/delete-from', body)
        self.assertEqual(result['id'], replacement)
        self.assertFalse(self.service.pending_history)
        self.assertEqual([point['text'] for point in self.messages(result)], ['First prompt'])
        await self.cleanup_finished()

    async def test_delete_opens_replacement_while_another_chat_is_running(self):
        first, _ = self.fixture(True)
        other = await self.post('/v1/tasks', {'prompt': 'Unrelated work', 'workingDirectory': str(self.home)})
        await self.started(other)
        chat = self.service.store.conversation(first['conversationId'])
        result = await self.edit(chat['id'], self.messages(chat)[1], True)
        self.assertNotEqual(result['id'], chat['id'])
        self.assertEqual(self.service.store.get(other['id'])['status'], 'running')
        self.assertTrue(self.service.store.conversation(result['id'])['cleanupPending'])
        await self.finish(other)
        await self.cleanup_finished()

    async def test_deleted_controller_tasks_leave_no_dispatch_or_receipt_records(self):
        first, _ = self.fixture()
        db = self.service.store.db
        db.executescript('CREATE TABLE vm_attempts (task_id TEXT, attempt_id TEXT); CREATE TABLE vm_event_receipts (attempt_id TEXT); CREATE TABLE vm_journal_usage (task_id TEXT);')
        with db:
            db.execute('INSERT INTO vm_attempts VALUES (?,?)', (first['id'], 'attempt'))
            db.execute('INSERT INTO vm_event_receipts VALUES (?)', ('attempt',))
            db.execute('INSERT INTO vm_journal_usage VALUES (?)', (first['id'],))
        chat = self.service.store.conversation(first['conversationId'])
        await self.edit(chat['id'], self.messages(chat)[1], True)
        await self.cleanup_finished()
        for table in ('vm_attempts', 'vm_event_receipts', 'vm_journal_usage'):
            self.assertEqual(self.service.store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    async def test_large_tool_evidence_is_bounded_but_full_timeline_is_preserved(self):
        first, _ = self.fixture()
        output = 'VERY_LARGE_RETAINED_TOOL_OUTPUT\n' * 45000
        item = {'id': 'large-tool', 'type': 'commandExecution', 'command': 'safe fixture', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': output}
        self.agent(first, 'item/completed', {'item': item})
        self.agent(first, 'item/completed', {'item': {'id': 'after-tool', 'type': 'agentMessage', 'text': 'Tool finished'}})
        chat = self.service.store.conversation(first['conversationId'])
        target = next(point for point in self.messages(chat) if point['text'] == 'Tool finished')
        fork = await self.edit(chat['id'], target)
        seed = self.service.store.history_seed(fork['tasks'][-1]['id'])
        self.assertLessEqual(len(seed[-1]['text'].encode()), EVIDENCE_BYTES)
        self.assertIn('safe fixture', seed[-1]['text'])
        self.assertIn('full retained record', seed[-1]['text'])
        copied = next(event for event in all_events(self.service.store, fork['id']) if event['kind'] == 'agent' and event['data'].get('params', {}).get('item', {}).get('id') == 'large-tool')
        self.assertEqual(copied['data']['params']['item']['aggregatedOutput'], output)
        legacy = [{'role': 'assistant', 'text': EVIDENCE_PREFIX + json.dumps([item]), 'attachments': []}]
        self.assertLess(len(json.dumps(native_history(self.home, legacy)).encode()), EVIDENCE_BYTES)

    async def test_legacy_failed_import_rebuild_requires_no_executed_work(self):
        first, _ = self.fixture()
        store = self.service.store
        item = {'type': 'commandExecution', 'aggregatedOutput': 'x' * 1500000}
        seed = [{'role': 'user', 'text': 'Retained codeword CEDAR-47', 'attachments': []},
                {'role': 'assistant', 'text': EVIDENCE_PREFIX + json.dumps([item]), 'attachments': []}]
        # Give the independent snapshot only its own retained task.
        chat = store.conversation(first['conversationId'])
        fork = await self.edit(chat['id'], self.messages(chat)[1])
        parent = fork['tasks'][-1]
        store.set_history_seed(parent['id'], seed)
        failed = store.submit({'prompt': 'Previous attempt', 'resumeTaskId': parent['id'], 'workingDirectory': str(self.home)})[0]
        store.set_history_seed(failed['id'], None)
        store.update(failed['id'], status='failed', thread_id=str(uuid.uuid4()), result='', error='stream disconnected before completion: response.failed event received')
        task = store.submit({'prompt': 'Continue now', 'resumeTaskId': failed['id'], 'workingDirectory': str(self.home)})[0]
        recovered = recover_failed_import(store, task)
        self.assertEqual(recovered[0]['text'], 'Retained codeword CEDAR-47')
        self.assertEqual(recovered[-1]['text'], 'Previous attempt')
        self.assertLess(len(recovered[1]['text'].encode()), EVIDENCE_BYTES)
        self.agent(failed, 'item/started', {'item': {'type': 'commandExecution', 'id': 'may-have-run'}})
        self.assertIsNone(recover_failed_import(store, task))

    def test_evidence_budget_preserves_recent_records_and_unicode(self):
        items = [{'id': str(i), 'output': '\U0001f332' * 10000} for i in range(40)]
        text = execution_evidence(items)
        self.assertLessEqual(len(text.encode()), EVIDENCE_BYTES)
        self.assertIn('"id": "39"', text)
        self.assertIn('earlier tool records omitted', text)
        self.assertEqual(bounded_seed([{'role': 'assistant', 'text': text}])[0]['text'], text)

    async def test_large_restore_keeps_complete_retained_archive_and_erases_it_with_chat(self):
        first, _ = self.fixture()
        long_text = 'OLD RETAINED DETAIL\n' + 'historical detail ' * 10000 + ' EXACT-RETAINED-END'
        self.agent(first, 'item/completed', {'item': {'id': 'long', 'type': 'agentMessage', 'text': long_text}})
        self.agent(first, 'item/completed', {'item': {'id': 'last', 'type': 'agentMessage', 'text': 'Recent answer CEDAR-47'}})
        chat = self.service.store.conversation(first['conversationId'])
        fork = await self.edit(chat['id'], next(point for point in self.messages(chat) if point['text'] == 'Recent answer CEDAR-47'))
        ident = fork['tasks'][-1]['id']
        seed = self.service.store.history_seed(ident)
        imported, compact = restored_history(self.home, ident, seed)
        self.assertTrue(compact)
        self.assertLess(len(json.dumps(imported).encode()), 80 * 1024)
        self.assertIn('Recent answer CEDAR-47', json.dumps(imported))
        self.assertNotIn('Later secret', json.dumps(imported))
        archive = self.home / 'history-contexts' / ident / 'retained-messages.jsonl'
        records = [json.loads(line) for line in archive.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([dict((key,value) for key,value in record.items() if key != 'messageNumber') for record in records[1:]], seed)
        self.assertNotIn('Later secret', archive.read_text())
        self.assertEqual(self.service.store.history_seed(ident), seed)
        self.assertIn('Do not load the entire archive', imported[0]['content'][0]['text'])
        await self.post('/v1/chats/' + fork['id'] + '/secure-delete', {'confirm': fork['id'], 'passes': 7})
        self.assertFalse(archive.exists())
        self.assertEqual(self.service.store.get(first['id'])['request']['prompt'], 'First prompt')

    def test_short_restored_history_stays_complete_without_an_archive(self):
        seed = [{'role':'user','text':'Keep all of this exact text.','attachments':[]},
                {'role':'assistant','text':'Exact reply.','attachments':[]}]
        imported, compact = restored_history(self.home, str(uuid.uuid4()), seed)
        self.assertFalse(compact)
        self.assertEqual(imported, native_history(self.home, seed))
        self.assertFalse((self.home / 'history-contexts').exists())

    async def test_existing_large_import_upgrades_with_completed_and_uncertain_actions(self):
        first, _ = self.fixture()
        store = self.service.store
        long_text = 'Long retained reply. ' * 10000
        self.agent(first, 'item/completed', {'item': {'id':'long', 'type':'agentMessage','text':long_text}})
        chat = store.conversation(first['conversationId'])
        fork = await self.edit(chat['id'], next(point for point in self.messages(chat) if point['text'] == long_text))
        parent = fork['tasks'][-1]
        old = store.submit({'prompt':'Previous follow-up', 'resumeTaskId':parent['id'], 'workingDirectory':str(self.home)})[0]
        store.set_history_seed(old['id'],None)
        thread = str(uuid.uuid4())
        store.update(old['id'],status='cancelled',thread_id=thread)
        self.agent(old,'item/completed',{'item':{'id':'done','type':'commandExecution','status':'completed','aggregatedOutput':'ALREADY-PERFORMED'}})
        self.agent(old,'item/started',{'item':{'id':'uncertain','type':'commandExecution','status':'inProgress','command':'inspect before repeating'}})
        task = store.submit({'prompt':'New follow-up', 'resumeTaskId':old['id'], 'workingDirectory':str(self.home)})[0]
        store.update(task['id'],thread_id=thread)
        seed = upgrade_unprojected_history(store,store.get(task['id']))
        self.assertIn('Previous follow-up',json.dumps(seed))
        self.assertIn('ALREADY-PERFORMED',json.dumps(seed))
        self.assertIn('outcomeUnknown',json.dumps(seed))
        self.assertNotIn('Later secret',json.dumps(seed))
        self.assertNotIn('New follow-up',json.dumps(seed))
        folder = self.home / 'history-contexts' / old['id']
        folder.mkdir(parents=True)
        (folder/'retained-messages.jsonl').write_text('already upgraded')
        self.assertIsNone(upgrade_unprojected_history(store,store.get(task['id'])))


if __name__ == '__main__':
    unittest.main()
