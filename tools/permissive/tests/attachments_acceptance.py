"""Verify native vision input and an actual tool reading an attached document."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import struct
import tempfile
import zlib

from aiohttp import web
from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.runtime import Runtime, MODEL
from permissive.service import Service


def fixture_png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 32, 32, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x40\x80\xff' * 32) * 32)) + chunk(b'IEND', b'')


async def main(native):
    requests, document_path = [], None
    async def provider(request):
        body = await request.json(); requests.append(body)
        message = {'id': 'attachment-answer', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                   'content': [{'type': 'output_text', 'text': 'ATTACHMENTS-NATIVE-OK', 'annotations': []}]}
        if len(requests) == 1:
            execution = next(tool for tool in body['tools'] if tool['type'] in {'function', 'custom'} and tool['name'].endswith('exec'))
            command = 'Get-Content -LiteralPath ' + "'" + str(document_path).replace("'", "''") + "'"
            code = 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'
            message = {'id': 'attachment-read', 'type': 'custom_tool_call', 'call_id': 'attachment-read', 'status': 'completed', 'name': execution['name'], 'input': code}
            if execution['type'] == 'function':
                message.update(type='function_call', arguments=json.dumps({'input': message.pop('input')}))
        result = {'id': 'attachment-response-' + str(len(requests)), 'model': MODEL, 'object': 'response', 'status': 'completed', 'output': [message], 'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}
        stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'}); await stream.prepare(request)
        for event in [{'type': 'response.created', 'response': {**result, 'status': 'in_progress', 'output': []}},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': message}, {'type': 'response.completed', 'response': result}]:
            await stream.write(('data: ' + json.dumps(event) + '\n\n').encode())
        return stream

    upstream = web.Application(); upstream.router.add_post('/v1/responses', provider)
    async with TestServer(upstream) as server, asyncio.timeout(90):
        with tempfile.TemporaryDirectory(prefix='permissive-attachments-native-', dir='C:/codex-build') as directory:
            home = Path(directory)
            runtime = Runtime(home, native, str(server.make_url('/v1')), ['node', str(Path(__file__).parents[1] / 'browser/worker.mjs')])
            service = Service(home, runtime)
            async with SignedTestClient(TestServer(service.app()), provision(home)) as client:
                async def post(path, body):
                    response = await client.post(path, json=body, headers={'X-Test-Sign': '1'})
                    assert response.status < 300, (response.status, await response.text())
                    return await response.json()
                workspace = service.store.workspaces()['data'][0]['id']
                ids = []
                for name, data in [('screenshot.png', fixture_png()), ('notes.txt', b'ATTACHED-DOCUMENT-CONTENT')]:
                    item = await post('/v1/attachments', {'name': name, 'data': base64.b64encode(data).decode(), 'workspaceId': workspace})
                    ids.append(item['id'])
                document_path = service.attachments.path(ids[1])
                task = await post('/v1/tasks', {'prompt': 'Inspect the attached image and read the attached document with tools.', 'workingDirectory': directory, 'workspaceId': workspace, 'attachments': ids})
                while service.jobs: await asyncio.sleep(.05)
                saved = service.store.get(task['id'])
                assert saved['status'] == 'completed', saved
                assert saved['result'] == 'ATTACHMENTS-NATIVE-OK', saved
                images = [part for item in requests[0]['input'] if item.get('role') == 'user' for part in item.get('content', []) if part.get('type') == 'input_image']
                assert images and images[0]['image_url'].startswith('data:image/'), requests[0]['input']
                outputs = [item for body in requests[1:] for item in body['input'] if item.get('type') in {'function_call_output', 'custom_tool_call_output'}]
                assert any('ATTACHED-DOCUMENT-CONTENT' in json.dumps(item) for item in outputs), outputs
                print('Native image input, managed document path, actual file read, and response passed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--native', required=True)
    asyncio.run(main(parser.parse_args().native))
