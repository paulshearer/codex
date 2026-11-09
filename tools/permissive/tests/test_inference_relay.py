import asyncio
import gzip
import json
import unittest
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from permissive.inference_relay import application, validate_response_request


class Content:
    async def iter_any(self):
        yield b'data: {"model":"deepseek"}\n\n'


class Response:
    def __init__(self, status=200, content=None):
        self.status = status
        self.headers = {'Content-Type': 'text/event-stream'}
        self.content = content or Content()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass


class Upstream:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.content_factory = Content
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return Response(self.status, self.content_factory())


class InferenceRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = Upstream()
        self.binding = {'guestIp': '127.0.0.1', 'upstreamEndpoint': 'https://sparky1.taileef59.ts.net:8445/v1'}
        self.client = TestClient(TestServer(application(self.binding, session_factory=lambda **kwargs: self.upstream)))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close()
    async def test_streaming_fixed_route_does_not_forward_guest_credentials(self):
        response = await self.client.post('/v1/responses', json={'model': '/models/deepseek-v4-flash'}, headers={'Authorization': 'Bearer guest-selected'})
        self.assertEqual(response.status, 200)
        self.assertIn('deepseek', await response.text())
        args, options = self.upstream.calls[0]
        self.assertEqual(args[1], self.binding['upstreamEndpoint'] + '/responses')
        self.assertNotIn('Authorization', options['headers'])
        self.assertFalse(options['allow_redirects'])
    async def test_arbitrary_routes_queries_and_connect_never_reach_upstream(self):
        for path in ('/v1/responses?upstream=http://192.168.1.1', '/admin', '/v1/responses/else'):
            response = await self.client.post(path)
            self.assertEqual(response.status, 404)
        self.assertFalse(self.upstream.calls)
    async def test_other_guest_and_redirects_are_rejected(self):
        self.binding['guestIp'] = '172.31.254.2'
        response = await self.client.post('/v1/responses')
        self.assertEqual(response.status, 403)
        self.assertFalse(self.upstream.calls)
        self.binding['guestIp'] = '127.0.0.1'
        self.upstream.status = 302
        response = await self.client.post('/v1/responses', json={})
        self.assertEqual(response.status, 502)

    async def test_remote_media_never_reaches_upstream(self):
        values = ['http://169.254.169.254/latest', 'https://192.168.1.1/private',
                  'https://public.example/image.png', 'file:///etc/passwd', '//100.65.1.2/private',
                  r'C:\private\image.png', ' data:image/png;base64,YQ==']
        for field, kind in [('image_url', 'input_image'), ('file_url', 'input_file'),
                            ('audio_url', 'input_audio'), ('video_url', 'input_video')]:
            for value in values:
                with self.subTest(field=field, value=value):
                    response = await self.client.post('/v1/responses', json={
                        'input': [{'role': 'user', 'content': [{'type': kind, field: value}]}]})
                    self.assertEqual(response.status, 400)
                    self.assertNotIn(value, await response.text())
        self.assertFalse(self.upstream.calls)

    async def test_inline_native_images_audio_files_and_file_ids_preserve_body(self):
        payload = {'model': '/models/deepseek-v4-flash', 'stream': True, 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Inspect this local capture'},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,YQ=='},
                {'type': 'input_audio', 'audio_url': 'data:audio/wav;base64,YQ=='},
                {'type': 'input_file', 'file_data': 'data:application/pdf;base64,YQ==', 'filename': 'file.pdf'},
                {'type': 'input_file', 'file_data': 'YQ=='},
                {'type': 'input_image', 'file_id': 'file_123'},
                {'type': 'input_file', 'file_id': 'file-aBC9_123'}]},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': [
                {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,Yg=='}]}]}
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        response = await self.client.post('/v1/responses', data=body, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status, 200)
        self.assertIn('deepseek', await response.text())
        self.assertEqual(self.upstream.calls[0][1]['data'], body)

    async def test_media_in_tool_outputs_and_chat_wrappers_is_validated(self):
        cases = [
            {'input': [{'type': 'function_call_output', 'output': [
                {'type': 'input_image', 'image_url': 'http://192.168.1.1'}]}]},
            {'messages': [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'http://169.254.169.254'}}]}]},
            {'input': [{'content': [{'type': 'file', 'file': {'file_data': 'http://100.64.1.1'}}]}]},
            {'input': [{'content': [{'type': 'input_file', 'file_id': 'https://example.com/file'}]}]},
            {'input': [{'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,YQ==',
                                     'file_url': 'http://private.example'}]}]},
        ]
        for payload in cases:
            response = await self.client.post('/v1/responses', json=payload)
            self.assertEqual(response.status, 400)
        self.assertFalse(self.upstream.calls)

    async def test_urls_in_text_tool_arguments_and_schemas_remain_opaque(self):
        payload = {'input': [
            {'role': 'user', 'content': [{'type': 'input_text',
                'text': 'Read https://192.168.1.1 and discuss {"image_url":"http://private"}'}]},
            {'type': 'function_call', 'name': 'inspect', 'arguments': '{"image_url":"http://private"}'},
            {'type': 'function_call_output', 'output': '{"type":"input_image","image_url":"http://private"}'}],
            'tools': [{'type': 'function', 'name': 'get_image', 'parameters': {
                'properties': {'image_url': {'type': 'string', 'default': 'http://private'}}}}]}
        response = await self.client.post('/v1/responses', json=payload)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.upstream.calls), 1)

    async def test_malformed_json_duplicate_keys_and_non_json_types_are_rejected(self):
        for body in [b'{', b'[]', b'{"input":NaN}', b'{"input":1,"input":2}',
                     '{"input":[]}'.encode('utf-16')]:
            response = await self.client.post('/v1/responses', data=body, headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status, 400)
        response = await self.client.post('/v1/responses', data='{"input":[]}', headers={'Content-Type': 'text/plain'})
        self.assertEqual(response.status, 415)
        self.assertFalse(self.upstream.calls)

    async def test_compressed_uploads_and_slow_uploads_do_not_reach_upstream(self):
        compressed = gzip.compress(b'{"input":"' + b'x' * 1024 * 1024 + b'"}')
        response = await self.client.post('/v1/responses', data=compressed,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})
        self.assertEqual(response.status, 415)

        async def slow_body():
            yield b'{"input":"'
            await asyncio.sleep(1)
            yield b'late"}'

        with patch('permissive.inference_relay.REQUEST_UPLOAD_TIMEOUT', 0.05):
            response = await self.client.post('/v1/responses', data=slow_body(),
                headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status, 408)
        self.assertFalse(self.upstream.calls)

    async def test_oversized_body_is_rejected_before_upstream(self):
        with patch('permissive.inference_relay.MAX_REQUEST_BYTES', 32):
            response = await self.client.post('/v1/responses', json={'input': 'x' * 64})
            self.assertEqual(response.status, 413)
        self.assertFalse(self.upstream.calls)

    async def test_streaming_is_not_buffered_and_concurrent_stream_capacity_is_bounded(self):
        gate = asyncio.Event()

        class SlowContent:
            async def iter_any(self):
                yield b'data: first\n\n'
                await gate.wait()
                yield b'data: last\n\n'

        self.upstream.content_factory = SlowContent
        responses = []
        try:
            for _ in range(4):
                response = await self.client.post('/v1/responses', json={'input': 'stream'})
                responses.append(response)
                self.assertEqual(await asyncio.wait_for(response.content.readline(), 1), b'data: first\n')
            busy = await self.client.post('/v1/responses', json={'input': 'capacity'})
            self.assertEqual(busy.status, 429)
            self.assertEqual(len(self.upstream.calls), 4)
        finally:
            gate.set()
            for response in responses:
                self.assertIn(b'last', await response.read())
        again = await self.client.post('/v1/responses', json={'input': 'available'})
        self.assertEqual(again.status, 200)


class InferenceRequestBudgetTests(unittest.TestCase):
    def test_depth_and_item_budgets_precede_json_object_allocation(self):
        with patch('permissive.inference_relay.MAX_JSON_DEPTH', 4), patch('permissive.inference_relay.json.loads') as loads:
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                validate_response_request(b'{"input":[[[[[]]]]]}')
            loads.assert_not_called()
        with patch('permissive.inference_relay.MAX_JSON_ITEMS', 4), patch('permissive.inference_relay.json.loads') as loads:
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                validate_response_request(b'{"input":[1,2,3,4,5]}')
            loads.assert_not_called()

    def test_body_and_media_count_budgets(self):
        with patch('permissive.inference_relay.MAX_REQUEST_BYTES', 10):
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                validate_response_request(b'{"input":"long"}')
        with patch('permissive.inference_relay.MAX_MEDIA_PARTS', 1):
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                validate_response_request(json.dumps({'input': [{'content': [
                    {'type': 'input_image', 'image_url': 'data:image/png;base64,YQ=='},
                    {'type': 'input_image', 'image_url': 'data:image/png;base64,Yg=='}]}]}).encode())

    def test_escaped_quotes_and_structure_in_text_do_not_consume_json_depth(self):
        text = 'Escaped \\" quote \\\\" brace {' * 100
        body = json.dumps({'input': text}).encode()
        with patch('permissive.inference_relay.MAX_JSON_DEPTH', 2):
            validate_response_request(body)


if __name__ == '__main__':
    unittest.main()
