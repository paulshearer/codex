import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from permissive.pdf_worker import call_tool


def text_pdf(path, texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        content = DecodedStreamObject()
        escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        content.set_data(f'BT /F1 18 Tf 60 700 Td ({escaped}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(content)
    writer.write(path)


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.path = self.home / "Contract with spaces & apostrophe's.pdf"
        text_pdf(self.path, ['PDF-FIRST-' + 'long contract terms ' * 80, 'PDF-SECOND-PAGE', 'PDF-THIRD-PAGE'])

    def call(self, name, args, **kwargs):
        return call_tool(name, {'path': str(self.path), **args}, self.home, 'test-task', **kwargs)

    def parsed(self, result):
        self.assertNotIn('isError', result, result.get('content'))
        return json.loads(result['content'][0]['text'])

    def test_cursor_preserves_long_page_and_all_later_pages(self):
        args, pieces, seen = {'max_chars': 100, 'ocr': False}, [], set()
        for _ in range(100):
            result = self.parsed(self.call('read_pdf', args))
            self.assertEqual(result['total_pages'], 3)
            for page in result['pages']:
                seen.add(page['page']); pieces.append(page['text'])
            if result['next'] is None:
                break
            args.update(result['next'])
        else:
            self.fail('Cursor made no progress')
        expected = ''.join(page.extract_text() for page in PdfReader(self.path).pages)
        self.assertEqual(''.join(pieces), expected)
        self.assertEqual(seen, {1, 2, 3})

    def test_scanned_pdf_warns_and_renders_actual_image(self):
        image = Image.new('RGB', (800, 400), 'white')
        ImageDraw.Draw(image).text((35, 100), 'SCANNED-VISION-ONLY-9142', fill='black', font_size=30)
        image.save(self.path, 'PDF')
        result = self.parsed(self.call('read_pdf', {'ocr': False}))
        self.assertEqual(result['pages'][0]['text'], '')
        self.assertTrue(result['pages'][0]['warnings'])
        rendered = self.call('render_pdf_page', {'page': 1})
        metadata = self.parsed(rendered)
        self.assertTrue(Path(metadata['image_path']).is_file())
        with Image.open(io.BytesIO(base64.b64decode(rendered['content'][1]['data']))) as page:
            self.assertGreater(page.width, 500)
            self.assertLessEqual(page.width * page.height, 8_000_000)
            self.assertLess(page.convert('L').getextrema()[0], 100)

    def test_scanned_pdf_has_automatic_ocr_and_cleans_scratch(self):
        if not os.environ.get('PERMISSIVE_TESSERACT') and not (Path(__file__).parents[1] / 'ocr/tesseract.exe').exists():
            self.skipTest('Run with the packaged OCR engine or PERMISSIVE_TESSERACT')
        image = Image.new('RGB', (900, 400), 'white')
        ImageDraw.Draw(image).text((40, 100), 'SCANNED-INVOICE-9142', fill='black', font_size=40)
        image.save(self.path, 'PDF')
        value = self.parsed(self.call('read_pdf', {}))
        self.assertIn('SCANNED-INVOICE-9142', value['pages'][0]['text'])
        self.assertEqual(value['pages'][0]['extraction'], 'ocr')
        self.assertTrue(value['pages'][0]['low_text_layer'])
        self.assertTrue(value['pages'][0]['warnings'])
        self.assertFalse(list((self.home / 'artifacts/test-task').glob('.pdf-helper-*')))

    def test_errors_are_bounded_and_next_call_still_works(self):
        for args in [{'start_page': 99}, {'offset': 1000000}, {'page_count': 11}, {'max_chars': 0}, {'page_count': True}, {'extra': 1}]:
            with self.subTest(args=args):
                self.assertTrue(self.call('read_pdf', args)['isError'])
        self.path.write_bytes(b'%PDF-1.7\nnot a valid pdf')
        self.assertTrue(self.call('read_pdf', {})['isError'])
        text_pdf(self.path, ['PDF-RECOVERY-OK'])
        self.assertIn('PDF-RECOVERY-OK', self.parsed(self.call('read_pdf', {}))['pages'][0]['text'])

    def test_photo_page_uses_bounded_inline_image_and_keeps_png_artifact(self):
        image = Image.frombytes('RGB', (1800, 1800), os.urandom(1800 * 1800 * 3))
        image.save(self.path, 'PDF')
        result = self.call('render_pdf_page', {'page': 1})
        metadata = self.parsed(result)
        self.assertTrue(Path(metadata['image_path']).read_bytes().startswith(b'\x89PNG'))
        self.assertEqual(result['content'][1]['mimeType'], 'image/jpeg')
        self.assertLessEqual(len(base64.b64decode(result['content'][1]['data'])), 2 * 1024 * 1024)

    def test_password_protection_reports_error(self):
        writer = PdfWriter(); writer.append(self.path); writer.encrypt('fixture-password'); writer.write(self.path)
        result = self.call('read_pdf', {})
        self.assertTrue(result['isError'])
        self.assertIn('password-protected', result['content'][0]['text'])

    def test_timeout_stops_only_its_helper(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
        try:
            result = self.call('read_pdf', {}, timeout=.001)
            self.assertTrue(result['isError'])
            self.assertIn('Only this PDF helper was stopped', result['content'][0]['text'])
            self.assertIsNone(unrelated.poll())
            self.assertIn('PDF-FIRST', self.parsed(self.call('read_pdf', {}))['pages'][0]['text'])
        finally:
            unrelated.terminate(); unrelated.wait(timeout=10)

    def test_stdio_mcp_text_and_image_results(self):
        process = subprocess.Popen([sys.executable, '-B', '-m', 'permissive.pdf_worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, 'PERMISSIVE_HOME': str(self.home), 'PERMISSIVE_TASK_ID': 'mcp-test'})
        try:
            def rpc(ident, method, params):
                process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}) + '\n').encode())
                process.stdin.flush()
                response = json.loads(process.stdout.readline())
                self.assertNotIn('error', response)
                return response['result']
            self.assertIn('tools', rpc(1, 'initialize', {'protocolVersion': '2024-11-05'})['capabilities'])
            self.assertEqual([t['name'] for t in rpc(2, 'tools/list', {})['tools']], ['read_pdf', 'render_pdf_page'])
            read = rpc(3, 'tools/call', {'name': 'read_pdf', 'arguments': {'path': str(self.path)}})
            self.assertEqual(self.parsed(read)['total_pages'], 3)
            rendered = rpc(4, 'tools/call', {'name': 'render_pdf_page', 'arguments': {'path': str(self.path), 'page': 2}})
            self.assertEqual(rendered['content'][1]['type'], 'image')
            self.assertTrue(base64.b64decode(rendered['content'][1]['data']).startswith(b'\x89PNG'))
        finally:
            process.stdin.close(); process.wait(timeout=10)
            process.stdout.close(); process.stderr.close()


if __name__ == '__main__':
    unittest.main()
