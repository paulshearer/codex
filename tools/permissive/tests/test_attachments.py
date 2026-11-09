import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
import test_conversations as conversations
from permissive.service import Service
from permissive.attachments import native_input

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQ1cAAAAASUVORK5CYII=')


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    post = conversations.ConversationTests.post
    get = conversations.ConversationTests.get
    started = conversations.ConversationTests.started
    finish = conversations.ConversationTests.finish

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.runtime = conversations.ControlledRuntime()
        self.service = Service(self.home, self.runtime)
        self.signer = provision(self.home)
        self.client = SignedTestClient(TestServer(self.service.app()), self.signer)
        await self.client.start_server()
        self.workspace = self.service.store.workspaces()['data'][0]['id']

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def upload(self, name='screenshot.png', data=PNG):
        return await self.post('/v1/attachments', {'name': name, 'data': base64.b64encode(data).decode(), 'workspaceId': self.workspace})

    async def test_images_and_documents_reach_native_input_and_retry_keeps_one_message(self):
        image, document = await self.upload(), await self.upload('report.txt', b'PRIVATE attachment fixture')
        body = {'prompt': 'Read both', 'workingDirectory': str(self.home), 'workspaceId': self.workspace,
                'attachments': [image['id'], document['id']], 'submissionId': str(uuid.uuid4())}
        task = await self.started(await self.post('/v1/tasks', body))
        retry = await self.post('/v1/tasks', body)
        self.assertEqual(task['id'], retry['id'])
        items = native_input(self.home, body['prompt'], body['attachments'])
        self.assertEqual(items[1]['type'], 'localImage')
        self.assertEqual(Path(items[1]['path']).read_bytes(), PNG)
        files = json.loads(items[0]['text'].split('\n')[-1])
        self.assertEqual(Path(files[1]['path']).suffix, '.txt')
        self.assertEqual(Path(files[1]['path']).read_bytes(), b'PRIVATE attachment fixture')
        info = await self.get('/v1/attachments/' + image['id'])
        self.assertEqual(base64.b64decode(info['preview'].split(',')[1]), PNG)
        download = await self.client.get('/v1/attachments/' + document['id'] + '/content', headers={'X-Test-Sign': '1'})
        self.assertEqual(await download.read(), b'PRIVATE attachment fixture')
        await self.finish(task)
        next_task = await self.started(await self.post('/v1/tasks/' + task['id'] + '/continue', {'prompt': 'Next'}))
        self.assertNotIn('attachments', next_task['request'])
        self.assertEqual(len(native_input(self.home, 'Next', [])), 1)
        await self.finish(next_task)

    async def test_steering_and_queued_files_survive_dispatch_and_restart(self):
        task = await self.started(await self.post('/v1/tasks', {'prompt': 'First', 'workingDirectory': str(self.home), 'workspaceId': self.workspace}))
        image, document = await self.upload(), await self.upload('queued.txt', b'queued content')
        body = {'prompt': 'Look at this', 'attachments': [image['id']], 'expectedTurnId': task['activeTurnId'], 'submissionId': str(uuid.uuid4())}
        await self.post('/v1/tasks/' + task['id'] + '/steer', body)
        await self.post('/v1/tasks/' + task['id'] + '/steer', body)
        self.assertEqual(len(self.runtime.deliveries), 1)
        self.assertEqual(self.runtime.deliveries[0][1]['attachments'], [image['id']])
        queued = {'prompt': 'Read queued', 'attachments': [document['id']], 'submissionId': str(uuid.uuid4())}
        await self.post('/v1/chats/' + task['conversationId'] + '/followups', queued)
        await self.post('/v1/chats/' + task['conversationId'] + '/followups', queued)
        await self.finish(task)
        successor = await self.started(self.service.store.conversation_tasks(task['conversationId'])[-1])
        self.assertEqual(successor['request']['attachments'], [document['id']])
        await self.finish(successor)
        self.assertEqual(self.service.attachments.get(document['id'])['owner_task_id'], task['id'])
        await self.client.close()
        self.service = Service(self.home, conversations.ControlledRuntime())
        self.client = SignedTestClient(TestServer(self.service.app()), self.signer)
        await self.client.start_server()
        self.assertEqual((await self.get('/v1/attachments/' + document['id']))['name'], 'queued.txt')

    async def test_validation_authentication_and_private_paths(self):
        body = {'name': 'image.png', 'data': base64.b64encode(PNG).decode(), 'workspaceId': self.workspace}
        response = await self.client.post('/v1/attachments', json=body)
        self.assertEqual(response.status, 401)
        for invalid in ({**body, 'name': '../outside.png'}, {**body, 'name': 'a\\b'}, {**body, 'data': 'invalid!'}, {**body, 'data': ''}, {**body, 'data': 'A' * (23 * 1024 * 1024)}):
            response = await self.client.post('/v1/attachments', json=invalid, headers={'X-Test-Sign': '1'})
            self.assertIn(response.status, (400, 413))
        image = await self.upload()
        base = {'prompt': 'test', 'workspaceId': self.workspace, 'workingDirectory': str(self.home)}
        for ids in ([image['id']] * 2, ['../../outside'], 'invalid', [str(uuid.uuid4())] * 11):
            response = await self.client.post('/v1/tasks', json={**base, 'attachments': ids}, headers={'X-Test-Sign': '1'})
            self.assertEqual(response.status, 400)
        task = await self.started(await self.post('/v1/tasks', {**base, 'attachments': [image['id']]}))
        response = await self.client.post('/v1/tasks', json={**base, 'attachments': [image['id']]}, headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 400)
        response = await self.client.delete('/v1/attachments/' + image['id'], headers={'X-Test-Sign': '1'})
        self.assertEqual(response.status, 400)
        await self.finish(task)

    async def test_removal_and_chat_erasure_overwrite_managed_copies_only(self):
        original = self.home / 'original.txt'
        original.write_bytes(b'original fixture')
        document = await self.upload('original.txt', original.read_bytes())
        draft = await self.upload('draft.txt', b'draft fixture')
        draft_path = self.service.attachments.path(draft['id'])
        with patch('permissive.secure_delete.os.fsync') as flushed:
            response = await self.client.delete('/v1/attachments/' + draft['id'], headers={'X-Test-Sign': '1'})
            self.assertEqual(response.status, 200)
            self.assertEqual(flushed.call_count, 7)
        self.assertFalse(draft_path.exists())
        task = await self.started(await self.post('/v1/tasks', {'prompt': 'Read', 'workspaceId': self.workspace, 'workingDirectory': str(self.home), 'attachments': [document['id']]}))
        await self.finish(task)
        managed = self.service.attachments.path(document['id'])
        survivor = await self.upload('survivor.txt', b'surviving draft')
        await self.post('/v1/chats/' + task['conversationId'] + '/secure-delete', {'confirm': task['conversationId'], 'passes': 7})
        self.assertFalse(managed.exists())
        self.assertEqual(original.read_bytes(), b'original fixture')
        self.assertEqual(self.service.attachments.get(survivor['id'])['name'], 'survivor.txt')
        await self.post('/v1/workspaces/' + self.workspace + '/secure-delete', {'confirm': self.workspace, 'passes': 7})
        self.assertFalse((self.home / 'attachments' / survivor['id']).exists())
