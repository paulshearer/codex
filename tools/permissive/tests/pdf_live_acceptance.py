"""Prove the installed harness reads digital and scanned PDFs with real DeepSeek."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import uuid

from aiohttp.test_utils import TestServer
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from permissive.privacy import INFERENCE_ENDPOINT
from permissive.runtime import Runtime
from permissive.service import Service
from signed_client import SignedTestClient, provision
from test_pdf_worker import text_pdf


async def main(package, output):
    output.mkdir(parents=True, exist_ok=True)
    home = output / ('live-' + uuid.uuid4().hex[:12]); home.mkdir()
    # Deliberately include zero/O ambiguity: vision must correct imperfect OCR.
    digital_marker, scan_marker = 'DOC-' + uuid.uuid4().hex[:8].upper(), 'VISION-00B3' + uuid.uuid4().hex[:4].upper()
    digital = home / 'digital.pdf'; scan = home / 'scan.pdf'
    text_pdf(digital, [f'Document identifier: {digital_marker}. This is the first page of a PDF reading verification.'])
    image = Image.new('RGB', (1000, 500), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((50, 80), 'Page 2: Scanned verification sheet', fill='black', font_size=42)
    draw.text((50, 200), f'Verification code: {scan_marker}', fill='black', font_size=42)
    image.save(scan, 'PDF')
    document = home / 'digital and scanned PDF.pdf'
    writer = PdfWriter(); writer.append(digital); writer.append(scan); writer.write(document)
    runtime = Runtime(home, package / 'native/codex-app-server.exe', INFERENCE_ENDPOINT,
                      [str(package / 'node/node.exe'), str(package / 'browser/worker.mjs')])
    service = Service(home, runtime)
    async with SignedTestClient(TestServer(service.app()), provision(home)) as client, asyncio.timeout(600):
        async def post(route, body):
            response = await client.post(route, json=body, headers={'X-Test-Sign': '1'})
            assert response.status < 300, (response.status, await response.text())
            return await response.json()
        workspace = service.store.workspaces()['data'][0]['id']
        attached = await post('/v1/attachments', {'workspaceId': workspace, 'name': document.name, 'data': base64.b64encode(document.read_bytes()).decode()})
        task = await post('/v1/tasks', {'workspaceId': workspace, 'workingDirectory': str(home), 'attachments': [attached['id']],
                                      'prompt': 'Read the attached two-page PDF. Report the document identifier on page 1 and the exact verification code on page 2. Render page 2 once to verify any ambiguous OCR characters against the actual image, including zeros versus letter O. Cite the page for each. Do not guess missing text.'})
        print(json.dumps({'live_task': task['id'], 'home': str(home)}), flush=True)
        while service.jobs:
            await asyncio.sleep(1)
        saved = service.store.get(task['id'])
        result = saved.get('result') or ''
        events = [json.loads(row[0]) for row in service.store.db.execute("SELECT data FROM events WHERE task_id=? AND kind='agent'", (task['id'],))]
        tools = [event.get('params', {}).get('item', {}) for event in events if event.get('method') == 'item/completed']
        pdf_calls = [item for item in tools if item.get('type') == 'mcpToolCall' and item.get('server') == 'pdf']
        used = {item.get('tool') for item in pdf_calls}
        validation = {'task_id': task['id'], 'status': saved['status'], 'digital_text_correct': digital_marker in result,
                      'scanned_vision_correct': scan_marker in result, 'pdf_tools_used': sorted(used),
                      'png_artifacts': len(list((home / 'artifacts' / task['id']).glob('pdf-page-*.png'))), 'result': result}
        validation['completed_shell_commands'] = sum(item.get('type') == 'commandExecution' for item in tools)
        (output / 'live-validation.json').write_text(json.dumps(validation, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in validation.items() if key != 'result'}), flush=True)
        assert saved['status'] == 'completed', saved['error']
        assert digital_marker in result and scan_marker in result, 'Live model did not correctly read both PDF pages'
        assert {'read_pdf', 'render_pdf_page'} <= used, 'Model did not use both dedicated PDF tools'
        assert validation['png_artifacts'] > 0
        assert validation['completed_shell_commands'] == 0, 'The model fell back to ad hoc shell/PDF/OCR tooling'
        print('PASS: real DeepSeek read digital text and recognized an undisclosed random code from a scanned PDF page.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--package', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.package, args.output))
