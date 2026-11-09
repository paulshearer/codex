"""Exercise real native PDF tools, vision transport and content-neutral hooks."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image, ImageDraw
from permissive.pdf_worker import call_tool
from permissive.runtime import Runtime, MODEL
from permissive.service import Service
from signed_client import SignedTestClient, provision
from test_pdf_worker import text_pdf


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


async def native_acceptance(native):
    requests, document_path, scan_path = [], None, None

    async def provider(request):
        body = await request.json(); requests.append(body)
        index = len(requests)
        message = {'id': f'pdf-item-{index}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                   'content': [{'type': 'output_text', 'text': 'PDF-NATIVE-OK', 'annotations': []}]}
        if index <= 2:
            execution = next(tool for tool in body['tools'] if tool['type'] in {'function', 'custom'} and tool['name'].endswith('exec'))
            if index == 1:
                code = ('const r = ALL_TOOLS.find(t => t.name.endsWith("__read_pdf"));'
                        'const v = ALL_TOOLS.find(t => t.name.endsWith("__render_pdf_page"));'
                        f'text(await tools[r.name]({{path:{json.dumps(str(document_path))}}}));'
                        f'const scan=await tools[r.name]({{path:{json.dumps(str(scan_path))},ocr:false}});text(scan);'
                        f'const view=await tools[v.name]({{path:{json.dumps(str(scan_path))},page:1}});'
                        'for(const c of view.content){if(c.type === "image")image(c);else if(c.type === "text")text(c.text);}')
            else:
                # -WhatIf does not stop any processes. Verify that command text
                # does not trigger an in-harness action-policy decision.
                marker = str(document_path.parent / 'sweep-executed.txt').replace("'", "''")
                command = "Get-Process python | Stop-Process -WhatIf; Set-Content -LiteralPath '" + marker + "' -Value SWEEP-EXECUTED"
                code = 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'
            message = {'id': f'pdf-call-{index}', 'type': 'custom_tool_call', 'call_id': f'pdf-call-{index}',
                       'status': 'completed', 'name': execution['name'], 'input': code}
            if execution['type'] == 'function':
                message.update(type='function_call', arguments=json.dumps({'input': message.pop('input')}))
        result = {'id': f'pdf-response-{index}', 'model': MODEL, 'object': 'response', 'status': 'completed',
                  'output': [message], 'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}
        stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'}); await stream.prepare(request)
        for event in [{'type': 'response.created', 'response': {**result, 'status': 'in_progress', 'output': []}},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                      {'type': 'response.completed', 'response': result}]:
            await stream.write(('data: ' + json.dumps(event) + '\n\n').encode())
        return stream

    upstream = web.Application(client_max_size=32 * 1024 * 1024)
    upstream.router.add_post('/v1/responses', provider)
    async with TestServer(upstream) as server, asyncio.timeout(120):
        with tempfile.TemporaryDirectory(prefix='permissive-pdf-native-', dir='C:/codex-build') as directory:
            home = Path(directory)
            document_path = home / 'contract.pdf'
            text_pdf(document_path, ['NATIVE-PDF-TEXT-4821 ' + 'Contract terms ' * 10])
            scan = Image.new('RGB', (800, 400), 'white')
            ImageDraw.Draw(scan).text((40, 100), 'SCANNED-PDF-VISION-7634', fill='black', font_size=32)
            scan_source = home / 'scan.pdf'; scan.save(scan_source, 'PDF')
            runtime = Runtime(home, native, str(server.make_url('/v1')), ['node', str(Path(__file__).parents[1] / 'browser/worker.mjs')])
            service = Service(home, runtime)
            async with SignedTestClient(TestServer(service.app()), provision(home)) as client:
                async def post(route, body):
                    response = await client.post(route, json=body, headers={'X-Test-Sign': '1'})
                    assert response.status < 300, (response.status, await response.text())
                    return await response.json()
                workspace = service.store.workspaces()['data'][0]['id']
                attached = await post('/v1/attachments', {'name': 'scanned contract.pdf', 'data': base64.b64encode(scan_source.read_bytes()).decode(), 'workspaceId': workspace})
                scan_path = service.attachments.path(attached['id'])
                task = await post('/v1/tasks', {'prompt': 'Read the contract text and inspect the scanned attachment. Test content-neutral command execution.',
                                              'workspaceId': workspace, 'workingDirectory': directory, 'attachments': [attached['id']]})
                while service.jobs:
                    await asyncio.sleep(.05)
                saved = service.store.get(task['id'])
                assert saved['status'] == 'completed', saved
                outputs = [item for body in requests for item in body['input'] if item.get('type') in {'function_call_output', 'custom_tool_call_output'}]
                serial = json.dumps(outputs)
                assert 'NATIVE-PDF-TEXT-4821' in serial, serial[:5000]
                assert 'Little or no extractable text' in serial, serial[:5000]
                vision_messages = [item for body in requests for item in body['input'] if item.get('role') == 'user']
                assert any(item.get('type') == 'input_image' for item in walk(vision_messages)), 'Rendered PDF image did not reach the backend-supported user vision input'
                assert 'process sweep can stop' not in serial.lower(), 'An action-policy deny remained in the activity boundary'
                assert (home / 'sweep-executed.txt').read_text().strip() == 'SWEEP-EXECUTED', 'Harmless WhatIf command did not execute'
                hook_runs = [json.loads(row[0]).get('params', {}).get('run', {}) for row in service.store.db.execute("SELECT data FROM events WHERE task_id=? AND data LIKE '%hook/completed%'", (task['id'],))]
                assert all(run.get('status') != 'failed' for run in hook_runs), 'A native activity-boundary hook failed'
                health = await client.get('/v1/health', headers={'X-Test-Sign': '1'})
                assert health.status == 200
                assert list((home / 'artifacts' / task['id']).glob('pdf-page-*.png'))
                print('PASS: native PDF text, managed scan attachment, actual vision input, PNG artifact, content-neutral hooks and healthy service.', flush=True)


def contracts_acceptance(directory):
    files = sorted(Path(directory).glob('*.pdf'))
    assert files, 'No PDFs in the acceptance directory'
    with tempfile.TemporaryDirectory(prefix='permissive-pdf-contracts-') as temporary:
        for path in files:
            cursor, pages, chars = {}, set(), 0
            for _ in range(200):
                result = call_tool('read_pdf', {'path': str(path.resolve()), **cursor}, temporary, 'contract-test')
                assert not result.get('isError'), (path.name, result)
                value = json.loads(result['content'][0]['text'])
                pages.update(page['page'] for page in value['pages'])
                chars += sum(len(page['text']) for page in value['pages'])
                if value['next'] is None:
                    break
                cursor = value['next']
            else:
                raise AssertionError('Cursor failed to finish: ' + path.name)
            assert pages == set(range(1, value['total_pages'] + 1)), path.name
            rendered = call_tool('render_pdf_page', {'path': str(path.resolve()), 'page': 1}, temporary, 'contract-test')
            assert not rendered.get('isError'), (path.name, rendered)
            assert rendered['content'][1]['type'] == 'image'
            print(json.dumps({'file': path.name, 'pages': len(pages), 'text_chars': chars, 'rendered': True}), flush=True)
        print(f'PASS: all {len(files)} PDFs read through the final page and rendered.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--native')
    parser.add_argument('--contracts')
    args = parser.parse_args()
    if args.contracts:
        contracts_acceptance(args.contracts)
    if args.native:
        asyncio.run(native_acceptance(args.native))
