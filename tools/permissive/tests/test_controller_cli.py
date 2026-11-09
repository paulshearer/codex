"""CLI role and package boundaries without starting agents, services or VMs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid
import asyncio
from types import SimpleNamespace

from permissive import __main__ as cli
from permissive.privacy import validate_inference_endpoint


class ReachedGuestRuntime(Exception):
    pass


class ControllerCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_missing_deployment_cannot_fall_back_to_local_agent(self):
        with mock.patch.dict(os.environ, {'PERMISSIVE_PACKAGE': str(self.root)}, clear=False), \
             mock.patch.object(cli, 'package_root', return_value=self.root), \
             mock.patch('permissive.runtime.Runtime') as native, \
             mock.patch('permissive.vm_runtime.VMRuntime') as controller, \
             mock.patch('sys.argv', ['codex-permissive', '--home', str(self.root / 'home'), 'serve']), \
             mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code, 2)
            native.assert_not_called()
            controller.assert_not_called()

    def test_role_mismatch_cannot_construct_either_runtime(self):
        binding = self.root / 'controller.json'
        binding.write_text(json.dumps({'role': 'controller', 'vmId': str(uuid.uuid4()), 'guestCredentialPath': 'guest.dpapi'}))
        with mock.patch('permissive.runtime.Runtime') as native, \
             mock.patch('permissive.vm_runtime.VMRuntime') as controller, \
             mock.patch('sys.argv', ['codex-permissive', '--home', str(self.root / 'home'), 'serve', '--role', 'guest', '--deployment', str(binding)]), \
             mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code, 2)
            native.assert_not_called()
            controller.assert_not_called()

    def test_package_root_resolves_source_and_installed_layout(self):
        for directory, module_path in [
            (self.root / 'source/tools/permissive', self.root / 'source/tools/permissive/permissive/__main__.py'),
            (self.root / 'package', self.root / 'package/lib/permissive/__main__.py'),
        ]:
            (directory / 'windows').mkdir(parents=True)
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(cli, '__file__', str(module_path)):
                self.assertEqual(cli.package_root(), directory)

    def test_installed_guest_browser_worker_uses_package_not_lib(self):
        package = self.root / 'package'
        (package / 'windows').mkdir(parents=True)
        (package / 'browser').mkdir()
        binding = self.root / 'guest.json'
        binding.write_text(json.dumps({'role': 'guest', 'packagePath': str(package),
                                       'inferenceEndpoint': 'https://172.31.254.1:18445/v1'}))
        with mock.patch.dict(os.environ, {'PERMISSIVE_PACKAGE': str(package)}, clear=False), \
             mock.patch.object(cli, '__file__', str(package / 'lib/permissive/__main__.py')), \
             mock.patch('permissive.runtime.Runtime', side_effect=ReachedGuestRuntime) as native, \
             mock.patch('sys.argv', ['codex-permissive', '--home', str(self.root / 'home'), 'serve', '--role', 'guest', '--deployment', str(binding)]):
            with self.assertRaises(ReachedGuestRuntime):
                cli.main()
            command = native.call_args.args[3]
            self.assertEqual(Path(command[1]), package / 'browser/worker.mjs')
            self.assertEqual(native.call_args.kwargs['gateway'], 'https://172.31.254.1:18445/v1')

    def test_guest_gateway_requires_tls_even_when_explicitly_configured(self):
        gateway = 'https://172.31.254.1:18445/v1'
        self.assertEqual(validate_inference_endpoint(gateway, gateway=gateway), gateway)
        with self.assertRaises(ValueError):
            validate_inference_endpoint('http://172.31.254.1:18445/v1', gateway='http://172.31.254.1:18445/v1')
        with self.assertRaises(ValueError):
            validate_inference_endpoint('https://unconfigured.internal/v1', gateway=gateway)

    def test_cli_refuses_legacy_host_service_before_submitting_work(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return {'ok': True, 'model': '/models/deepseek-v4-flash'}
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, target): return Response()
            request = mock.Mock()
        client = Client()
        args = SimpleNamespace(home=self.root, private_key=None, url='http://127.0.0.1:8789')
        with mock.patch.object(cli, 'SignedSession', return_value=client), mock.patch.object(cli, 'package_root', return_value=self.root), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'another execution role'):
                asyncio.run(cli.request(args, 'POST', '/v1/tasks', {'prompt': 'must not execute'}))
        client.request.assert_not_called()

    def test_cli_default_directory_is_inside_guest(self):
        args = SimpleNamespace(workspace=None, cwd=None, browser=None, browser_type=None, browser_profile=None)
        self.assertEqual(cli.submission_options(args)['workingDirectory'], r'C:\workspaces')
        args.cwd = r'\\host\share'
        with self.assertRaisesRegex(ValueError, 'guest path'):
            cli.submission_options(args)


if __name__ == '__main__':
    unittest.main()
