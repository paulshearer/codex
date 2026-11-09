"""Verify edited chat context against the real native agent and a local provider."""
import argparse
import base64
import asyncio
import json
import io
from pathlib import Path
import tempfile
import uuid

from aiohttp import web
from aiohttp.test_utils import TestServer
from permissive.history import all_events, copy_prefix, points, native_history
from permissive.runtime import Runtime, MODEL
from permissive.store import Store
from permissive.attachments import Attachments
from PIL import Image


async def main(native):
    requests = []

    async def provider(request):
        body = await request.json()
        requests.append(body)
        item = {'id': 'fixture-' + str(len(requests)), 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'NATIVE-HISTORY-OK', 'annotations': []}]}
        result = {'id': 'response-fixture', 'model': MODEL, 'status': 'completed', 'output': [item],
                  'usage': {'input_tokens': 20, 'output_tokens': 5, 'total_tokens': 25}}
        stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await stream.prepare(request)
        for event in [{'type': 'response.created', 'response': {**result, 'status': 'in_progress', 'output': []}},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': item}, {'type': 'response.completed', 'response': result}]:
            await stream.write(('data: ' + json.dumps(event) + '\n\n').encode())
        return stream

    app = web.Application()
    app.router.add_post('/v1/responses', provider)
    async with TestServer(app) as server:
        with tempfile.TemporaryDirectory(prefix='permissive-history-native-') as directory:
            home = Path(directory)
            runtime = Runtime(home, native, str(server.make_url('/v1')), ['node', str(Path(__file__).parents[1] / 'browser/worker.mjs')])
            store = Store(home)
            try:
                # No source native rollout is available: the fork must be fully
                # self-contained and cannot accidentally resume a later summary.
                source = store.submit({'prompt': 'RETAINED-USER', 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
                files = Attachments(store)
                png = io.BytesIO()
                Image.new('RGB', (24, 24), (20, 80, 160)).save(png, format='PNG')
                pixel = base64.b64encode(png.getvalue()).decode()
                image = files.upload({'workspaceId': source['workspaceId'], 'name': 'retained.png', 'data': pixel})['id']
                files.claim([image], source['id'])
                with store.db:
                    store.db.execute('UPDATE tasks SET request=? WHERE id=?', (json.dumps({**source['request'], 'attachments': [image]}), source['id']))
                store.update(source['id'], thread_id=str(uuid.uuid4()))
                store.event(source['id'], 'agent', {'method': 'item/completed', 'params': {'item': {'id': 'a', 'type': 'agentMessage', 'text': 'RETAINED-ANSWER'}}})
                store.event(source['id'], 'agent', {'method': 'item/completed', 'params': {'item': {'id': 'b', 'type': 'agentMessage', 'text': 'OMITTED-SECRET'}}})
                store.update(source['id'], status='completed', result='OMITTED-SECRET')
                target = points(store.conversation_tasks(source['conversationId']), all_events(store, source['conversationId']))[1]
                fork_id = copy_prefix(store, source['conversationId'], target, True)
                parent = store.conversation_tasks(fork_id)[-1]
                task = store.submit({'prompt': 'CONTINUE-AFTER-FORK', 'resumeTaskId': parent['id'], 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
                store.update(task['id'], thread_id=parent['threadId'], status='running')
                await asyncio.wait_for(runtime.run(store.get(task['id']), store), 60)
                assert store.get(task['id'])['result'] == 'NATIVE-HISTORY-OK', store.get(task['id'])
                payload = json.dumps(requests[-1]['input'])
                assert all(text in payload for text in ['RETAINED-USER', 'RETAINED-ANSWER', 'CONTINUE-AFTER-FORK']), payload
                assert 'OMITTED-SECRET' not in payload, payload
                assert any(part.get('type') == 'input_image' for item in requests[-1]['input'] if isinstance(item.get('content'), list) for part in item['content']), [item for item in requests[-1]['input'] if item.get('role') == 'user']
                assert store.history_seed(task['id']) is None
                assert store.get(task['id'])['threadId'] != source['threadId']
                # A second continuation must resume the new native thread rather
                # than importing the seed again and losing its latest answer.
                next_task = store.submit({'prompt': 'SECOND-CONTINUATION', 'resumeTaskId': task['id'], 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
                store.update(next_task['id'], thread_id=store.get(task['id'])['threadId'], status='running')
                await asyncio.wait_for(runtime.run(store.get(next_task['id']), store), 60)
                payload = json.dumps(requests[-1]['input'])
                assert all(text in payload for text in ['RETAINED-USER', 'RETAINED-ANSWER', 'CONTINUE-AFTER-FORK', 'NATIVE-HISTORY-OK', 'SECOND-CONTINUATION'])
                assert 'OMITTED-SECRET' not in payload
                assert any(part.get('type') == 'input_image' for item in requests[-1]['input'] if isinstance(item.get('content'), list) for part in item['content'])
                empty = store.submit({'prompt': 'START-FRESH-AFTER-DELETION', 'workingDirectory': str(home), 'browserMode': 'dedicated'})[0]
                store.set_history_seed(empty['id'], [])
                store.update(empty['id'], status='running')
                await asyncio.wait_for(runtime.run(store.get(empty['id']), store), 60)
                payload = json.dumps(requests[-1]['input'])
                assert 'START-FRESH-AFTER-DELETION' in payload and 'RETAINED-USER' not in payload and 'OMITTED-SECRET' not in payload
                print('Real native history import, independent fork, omitted-context exclusion and successive continuation passed.')
            finally:
                await runtime.close()
                store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', required=True)
    asyncio.run(main(parser.parse_args().native))
