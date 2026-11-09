"""Exercise real native steering, queued successors, and saved conversation identity."""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from aiohttp import web
from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.runtime import Runtime, MODEL
from permissive.service import Service


async def main(native):
    requests = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        body = await request.json()
        requests.append(body)
        if len(requests) == 1:
            entered.set()
            await release.wait()
        text = 'NATIVE-STEER-OK' if 'NATIVE-STEER-DIRECTION' in json.dumps(body.get('input')) else 'NATIVE-INITIAL-OK'
        message = {'id': 'answer-' + str(len(requests)), 'type': 'message', 'role': 'assistant', 'status': 'completed',
                   'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
        result = {'id': 'response-' + str(len(requests)), 'model': MODEL, 'object': 'response', 'status': 'completed', 'output': [message],
                  'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}
        stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await stream.prepare(request)
        for event in [{'type': 'response.created', 'response': {**result, 'status': 'in_progress', 'output': []}},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                      {'type': 'response.completed', 'response': result}]:
            await stream.write(('data: ' + json.dumps(event) + '\n\n').encode())
        return stream

    upstream = web.Application()
    upstream.router.add_post('/v1/responses', provider)
    async with TestServer(upstream) as server, asyncio.timeout(90):
        with tempfile.TemporaryDirectory(prefix='permissive-steer-', dir='C:/codex-build') as directory:
            home = Path(directory)
            runtime = Runtime(home, native, str(server.make_url('/v1')), ['node', str(Path(__file__).parents[1] / 'browser/worker.mjs')])
            service = Service(home, runtime)
            async with SignedTestClient(TestServer(service.app()), provision(home)) as client:
                async def post(path, body):
                    response = await client.post(path, json=body, headers={'X-Test-Sign': '1'})
                    assert response.status < 300, (response.status, await response.text())
                    return await response.json()

                first = await post('/v1/tasks', {'prompt': 'Start the native steering fixture', 'workingDirectory': directory})
                await entered.wait()
                saved = service.store.get(first['id'])
                assert saved['activeTurnId']
                body = {'prompt': 'NATIVE-STEER-DIRECTION', 'expectedTurnId': saved['activeTurnId'], 'submissionId': str(uuid.uuid4())}
                accepted = await post('/v1/tasks/' + first['id'] + '/steer', body)
                repeated = await post('/v1/tasks/' + first['id'] + '/steer', body)
                assert accepted == repeated and accepted['turnId'] == saved['activeTurnId']
                assert len(service.store.conversation_tasks(first['conversationId'])) == 1
                queue_path = '/v1/chats/' + first['conversationId'] + '/followups'
                await post(queue_path, {'prompt': 'QUEUED-NATIVE-FOLLOWUP', 'submissionId': str(uuid.uuid4())})
                release.set()
                while service.jobs:
                    await asyncio.sleep(.05)
                tasks = service.store.conversation_tasks(first['conversationId'])
                assert len(tasks) == 2 and all(task['status'] == 'completed' for task in tasks), tasks
                assert tasks[0]['threadId'] == tasks[1]['threadId']
                assert 'NATIVE-STEER-OK' in tasks[0]['result'], tasks[0]['result']
                assert any('NATIVE-STEER-DIRECTION' in json.dumps(body.get('input')) for body in requests[1:])
                events = service.store.conversation_events(first['conversationId'])
                native_users = [event['data']['params']['item'] for event in events if event['kind'] == 'agent'
                                and event['data'].get('params', {}).get('item', {}).get('type') == 'userMessage']
                assert any(item.get('clientId') == body['submissionId'] for item in native_users), native_users
                assert len([event for event in events if event['kind'] == 'steering']) == 1
                assert len(service.store.conversations()['data']) == 1
                print('Native steering, retry deduplication, queued continuation, and conversation identity passed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', required=True)
    asyncio.run(main(parser.parse_args().native))
