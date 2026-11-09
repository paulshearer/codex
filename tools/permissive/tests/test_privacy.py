import asyncio
import io
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch
from unittest.mock import AsyncMock
import subprocess
import os
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from permissive.client import SignedSession
from permissive.privacy import INFERENCE_ENDPOINT, native_environment, validate_inference_endpoint, validate_transport
from permissive.sparky_gateway import application, MODEL
from permissive.service import Service
from permissive.store import Store
from permissive.runtime import Runtime
from signed_client import provision, SignedTestClient
from test_service import FakeRuntime


class PrivacyTests(unittest.TestCase):
    def test_only_private_or_loopback_inference_is_accepted(self):
        for endpoint in [INFERENCE_ENDPOINT, 'http://127.0.0.1:1234/v1', 'http://[::1]:1234/v1', 'http://localhost:1234/v1/']:
            self.assertEqual(validate_inference_endpoint(endpoint), endpoint)
        for endpoint in ['https://api.openai.com/v1', 'https://outside.example/v1', 'http://sparky1.taileef59.ts.net:8445/v1', 'https://sparky1.taileef59.ts.net:8446/v1', INFERENCE_ENDPOINT + '?copy=secret', INFERENCE_ENDPOINT + '#secret', 'https://user:secret@sparky1.taileef59.ts.net:8445/v1']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_inference_endpoint(endpoint)

    def test_remote_plaintext_is_rejected_before_signing(self):
        client = object.__new__(SignedSession)
        client.headers = {}
        client.signer = Mock()
        for target in ['http://outside.example/v1/tasks', 'ws://192.168.1.2/v1/browser/extension']:
            with self.assertRaises(ValueError):
                client._prepare('POST', target, {'json': {'prompt': 'PRIVATE-PROMPT'}})
        client.signer.headers.assert_not_called()
        for target in ['https://pc.example/v1/tasks', 'wss://pc.example/v1/browser/extension', 'http://127.0.0.1/v1/tasks']:
            validate_transport(target)
        for target in ['https://[PRIVATE-KEY', 'https://127.0.0.1:PRIVATE-KEY', 'https://PRIVATE-KEY\uff0f@outside.example']:
            with self.assertRaises(ValueError) as failure:
                validate_transport(target)
            self.assertNotIn('PRIVATE-KEY', str(failure.exception))

    def test_native_environment_drops_verbose_logs_and_exporter_credentials(self):
        environment = {'RUST_LOG': 'trace', 'rust_log': 'debug', 'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://outside.example', 'otel_exporter_otlp_headers': 'secret', 'CODEX_OTEL_SECRET': 'secret', 'CODEX_TRACE_CONFIG': 'secret', 'codex_internal_app_server_remote_control_disabled': '0', 'PATH': 'tool-path'}
        sanitized = native_environment(environment)
        self.assertEqual(sanitized, {'RUST_LOG': 'off', 'PATH': 'tool-path', 'CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED': '1', 'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8'})
        self.assertEqual(environment['RUST_LOG'], 'trace')

    def test_python_helpers_can_print_unicode_chat_text_to_a_pipe(self):
        environment = native_environment({**os.environ, 'PYTHONUTF8':'0', 'pythonioencoding':'ascii'})
        result = subprocess.run([sys.executable, '-c', 'print(chr(0x1F332))'], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertEqual(result.stdout.decode('utf-8').strip(), '\U0001f332')

    def test_gateway_rejects_nonlocal_upstreams(self):
        for upstream in ['http://outside.example', 'https://127.0.0.1', 'http://user:secret@127.0.0.1', 'http://127.0.0.1/another/path', 'http://127.0.0.1?secret']:
            with self.subTest(upstream=upstream), self.assertRaises(ValueError):
                application(upstream, 'test-key', Path('maintenance'))


class PrivacyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_errors_never_disclose_backend_error_content(self):
        async def backend_failure(request):
            return web.Response(status=500, text='PRIVATE-BACKEND-KEY-PROMPT')

        backend = web.Application()
        backend.router.add_post('/v1/responses', backend_failure)
        async with TestServer(backend) as backend_server:
            with tempfile.TemporaryDirectory() as directory:
                gateway = application(str(backend_server.make_url('')).rstrip('/'), 'PRIVATE-CREDENTIAL', Path(directory) / 'maintenance', heartbeat=.01)
                async with TestClient(TestServer(gateway)) as client:
                    for stream in [False, True]:
                        response = await client.post('/v1/responses', json={'model': MODEL, 'stream': stream})
                        content = await response.text()
                        self.assertNotIn('PRIVATE-', content)
                        self.assertIn('Local inference returned HTTP 500', content)

    async def test_runtime_enforces_privacy_for_startup_and_thread_config(self):
        captured = {}

        def backend_factory(router, label, model):
            backend = Mock(proc=None)

            async def start():
                captured['startup'] = router.overrides(model)
                captured['thread'] = router.native_params({}, model)['config']
                captured['environment'] = router.backend_environment({'RUST_LOG': 'trace', 'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://outside.example'})
                captured['stderr'] = router.backend_stderr()
                raise RuntimeError('STOP-BEFORE-NATIVE-EXECUTION')

            backend.start = start
            backend.close = AsyncMock()
            return backend

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = Store(home)
            try:
                task, _ = store.submit({'prompt': 'PRIVATE-PROMPT', 'browserMode': 'dedicated', 'workingDirectory': str(home)})
                runtime = Runtime(home, 'unused-native.exe', INFERENCE_ENDPOINT, ['unused-node'])
                with patch('permissive.runtime.Backend', backend_factory), patch('permissive.runtime.Facade.start'), self.assertRaisesRegex(RuntimeError, 'STOP-BEFORE-NATIVE-EXECUTION'):
                    await runtime.run(task, store)
            finally:
                store.close()
        for name in ['startup', 'thread']:
            for signal in ['exporter', 'trace_exporter', 'metrics_exporter']:
                self.assertEqual(captured[name]['otel.' + signal], 'none')
            for flag in ['analytics.enabled', 'feedback.enabled', 'otel.log_user_prompt', 'otel.log_agent_responses', 'otel.log_guardian_assessments', 'features.image_generation', 'features.realtime_conversation']:
                self.assertIs(captured[name][flag], False)
        certificate = Path(captured['environment'].pop('CODEX_CA_CERTIFICATE'))
        self.assertEqual(certificate.name, 'certificate.pem')
        self.assertFalse(certificate.exists())
        self.assertEqual(captured['environment'], {'RUST_LOG': 'off', 'CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED': '1', 'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8'})
        self.assertEqual(captured['stderr'], subprocess.DEVNULL)

    async def test_gateway_redirects_never_forward_prompts_or_credentials(self):
        received = []

        async def outside(request):
            received.append((await request.read(), dict(request.headers)))
            return web.json_response({'stolen': True})

        external = web.Application()
        external.router.add_route('*', '/capture', outside)
        async with TestServer(external) as external_server:
            async def redirect(request):
                raise web.HTTPTemporaryRedirect(location=str(external_server.make_url('/capture')))

            backend = web.Application()
            backend.router.add_post('/v1/responses', redirect)
            async with TestServer(backend) as backend_server:
                with tempfile.TemporaryDirectory() as directory:
                    gateway = application(str(backend_server.make_url('')).rstrip('/'), 'PRIVATE-CREDENTIAL', Path(directory) / 'maintenance', heartbeat=.01)
                    async with TestClient(TestServer(gateway)) as client:
                        for stream in [False, True]:
                            response = await client.post('/v1/responses', json={'model': MODEL, 'stream': stream, 'input': 'PRIVATE-PROMPT'}, allow_redirects=False)
                            await response.read()
                        await asyncio.sleep(.02)
        self.assertEqual(received, [])

    async def test_http_requests_and_exception_bodies_do_not_enter_logs(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                signer = provision(home)
                service = Service(home, FakeRuntime())
                app = service.app()

                async def fail(request):
                    raise RuntimeError('PRIVATE-PROMPT-EXCEPTION')

                app.router.add_get('/test-error', fail)
                async with SignedTestClient(TestServer(app), signer) as client:
                    unauthorized = await client.get('/v1/tasks?prompt=PRIVATE-QUERY', headers={'User-Agent': 'PRIVATE-HEADER'})
                    self.assertEqual(unauthorized.status, 401)
                    response = await client.get('/test-error?prompt=PRIVATE-QUERY', headers={'X-Test-Sign': '1'})
                    self.assertEqual(response.status, 500)
                    self.assertNotIn('PRIVATE-', await response.text())
            self.assertNotIn('PRIVATE-', output.getvalue())
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
