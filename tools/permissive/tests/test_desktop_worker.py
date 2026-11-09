import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from permissive.desktop_worker import DesktopError, DesktopWorker, TOOLS, cleanup_payload, deadline, helper_path, run_helper


WINDOW = {'hwnd': '123', 'pid': 45, 'processStartTicks': '638000000000000000', 'title': 'Fixture'}
ELEMENT = {'id': 0, 'runtimeId': [42, 123, 9], 'name': 'Save', 'patterns': ['InvokePatternIdentifiers.Pattern']}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=')


class FixtureRunner:
    def __init__(self):
        self.calls = []
        self.action_result = {'ok': True, 'outcome': 'dispatched', 'method': 'fixture'}

    def __call__(self, payload, timeout):
        self.calls.append((payload, timeout))
        operation = payload['operation']
        if operation == 'inspect':
            return {'ok': True, 'outcome': 'observed', 'window': WINDOW, 'elements': [ELEMENT], 'nextOffset': None}
        if operation == 'capture':
            Path(payload['output']).write_bytes(PNG)
            return {'ok': True, 'outcome': 'observed', 'window': payload.get('window'),
                    'capture': {'width': 1, 'height': 1, 'bounds': {'left': 10, 'top': 20, 'width': 1, 'height': 1}, 'method': 'screen'}}
        if operation == 'windows':
            return {'ok': True, 'outcome': 'observed', 'windows': [WINDOW]}
        return dict(self.action_result)


class DesktopWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runner = FixtureRunner()
        self.worker = DesktopWorker(self.temporary.name, 'task', self.runner)

    def result(self, args):
        response = self.worker.call(args)
        return response, json.loads(response['content'][0]['text'])

    def inspect(self):
        return self.result({'operation': 'inspect', 'window': WINDOW})[1]

    def test_schema_contains_generic_operations_without_application_categories(self):
        properties = TOOLS[0]['inputSchema']['properties']
        self.assertEqual(properties['operation']['enum'], ['status', 'windows', 'launch', 'inspect', 'action', 'capture'])
        for action in ['invoke', 'set_value', 'set_range_value', 'scroll', 'drag', 'key', 'clipboard_set', 'close']:
            self.assertIn(action, properties['action']['enum'])
        self.assertNotIn('allowedApplications', properties)

    def test_snapshot_supplies_native_identity_and_is_consumed_after_mutation(self):
        observation = self.inspect()
        response, result = self.result({'operation': 'action', 'snapshotId': observation['snapshotId'], 'elementId': 0, 'action': 'invoke'})
        self.assertNotIn('isError', response)
        self.assertEqual(self.runner.calls[-1][0]['window'], WINDOW)
        self.assertEqual(self.runner.calls[-1][0]['element'], ELEMENT)
        count = len(self.runner.calls)
        response, result = self.result({'operation': 'action', 'snapshotId': observation['snapshotId'], 'elementId': 0, 'action': 'invoke'})
        self.assertTrue(response['isError'])
        self.assertEqual(result['error']['code'], 'stale_snapshot')
        self.assertEqual(len(self.runner.calls), count)

    def test_window_metadata_is_not_part_of_process_identity(self):
        observation = self.inspect()
        identity = {key: WINDOW[key] for key in ['hwnd', 'pid', 'processStartTicks']}
        response, _ = self.result({'operation': 'action', 'window': identity, 'snapshotId': observation['snapshotId'], 'elementId': 0, 'action': 'invoke'})
        self.assertNotIn('isError', response)

    def test_wrong_window_or_unknown_element_fails_before_native_call(self):
        observation = self.inspect()
        for extra, code in [({'window': {**WINDOW, 'pid': 999}}, 'target_mismatch'), ({'elementId': 100}, 'stale_element')]:
            count = len(self.runner.calls)
            response, result = self.result({'operation': 'action', 'snapshotId': observation['snapshotId'], 'elementId': 0, 'action': 'invoke', **extra})
            self.assertTrue(response['isError'])
            self.assertEqual(result['error']['code'], code)
            self.assertEqual(len(self.runner.calls), count)

    def test_unknown_mutation_is_not_retried_or_reusable(self):
        observation = self.inspect()
        self.runner.action_result = {'ok': False, 'outcome': 'unknown', 'error': {'code': 'helper_timeout', 'message': 'Timed out'}}
        count = len(self.runner.calls)
        response, result = self.result({'operation': 'action', 'snapshotId': observation['snapshotId'], 'elementId': 0, 'action': 'invoke'})
        self.assertTrue(response['isError'])
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(len(self.runner.calls), count + 1)
        self.assertEqual(len(self.worker.snapshots), 0)

    def test_noninteractive_result_does_not_disable_semantic_operations(self):
        self.runner.action_result = {'ok': False, 'outcome': 'not_started', 'error': {'code': 'no_interactive_desktop', 'message': 'Locked'}}
        _, result = self.result({'operation': 'action', 'action': 'key', 'key': 'Return'})
        self.assertEqual(result['error']['code'], 'no_interactive_desktop')
        observation = self.inspect()
        self.runner.action_result = {'ok': True, 'outcome': 'dispatched', 'method': 'uia.Invoke'}
        response, result = self.result({'operation': 'action', 'snapshotId': observation['snapshotId'], 'elementId': 0, 'action': 'invoke'})
        self.assertNotIn('isError', response)
        self.assertEqual(result['method'], 'uia.Invoke')

    def test_capture_returns_real_inline_image_artifact_and_coordinate_binding(self):
        response, result = self.result({'operation': 'capture', 'window': WINDOW})
        self.assertEqual(Path(result['path']).read_bytes(), PNG)
        self.assertEqual(base64.b64decode(response['content'][1]['data']), PNG)
        snapshot = result['snapshotId']
        response, _ = self.result({'operation': 'action', 'snapshotId': snapshot, 'action': 'click', 'x': 0, 'y': 0})
        self.assertNotIn('isError', response)
        self.assertEqual(self.runner.calls[-1][0]['window'], WINDOW)
        self.assertEqual(self.runner.calls[-1][0]['observedBounds'], {'left': 10, 'top': 20, 'width': 1, 'height': 1})

    def test_coordinate_input_requires_capture_but_releases_do_not(self):
        response, result = self.result({'operation': 'action', 'action': 'click', 'x': 1, 'y': 2})
        self.assertTrue(response['isError'])
        self.assertEqual(result['error']['code'], 'stale_snapshot')
        for action in ['mouse_up', 'key_up']:
            response, _ = self.result({'operation': 'action', 'action': action, 'key': 'Shift'})
            self.assertNotIn('isError', response)

    def test_validation_rejects_bad_shape_without_mutation(self):
        for args in [{'operation': 'unknown'}, {'operation': 'action', 'action': 'invoke'},
                     {'operation': 'status', 'timeoutMs': True}, {'operation': 'inspect'},
                     {'operation': 'launch'}, {'operation': 'windows', 'extra': True}]:
            response, result = self.result(args)
            self.assertTrue(response['isError'])
            self.assertEqual(result['outcome'], 'not_started')
        self.assertEqual(self.runner.calls, [])

    def test_deadlines_account_for_long_input_and_are_caller_overridable(self):
        self.assertEqual(deadline({'operation': 'status'}), 3)
        self.assertEqual(deadline({'operation': 'launch', 'waitForWindowMs': 30000}), 35)
        self.assertEqual(deadline({'operation': 'action', 'action': 'drag', 'durationMs': 20000, 'holdMs': 1000}), 26)
        self.assertEqual(deadline({'operation': 'inspect', 'timeoutMs': 120000}), 120)
        with self.assertRaises(DesktopError):
            deadline({'operation': 'action', 'timeoutMs': 0})

    def test_input_cleanup_is_limited_to_requested_buttons_and_keys(self):
        self.assertEqual(cleanup_payload({'operation': 'action', 'action': 'drag', 'modifiers': ['Ctrl'], 'button': 'right'}),
                         {'operation': 'cleanup_input', 'keys': ['Ctrl'], 'buttons': ['right']})
        self.assertEqual(cleanup_payload({'operation': 'action', 'action': 'key', 'key': 'Ctrl+a'}),
                         {'operation': 'cleanup_input', 'keys': ['Ctrl', 'a'], 'buttons': []})
        self.assertIsNone(cleanup_payload({'operation': 'action', 'action': 'invoke'}))

    def test_helper_resolution_supports_installed_and_source_layouts(self):
        import permissive.desktop_worker as module
        with mock.patch.dict(os.environ, {'PERMISSIVE_DESKTOP_AGENT': str(Path(self.temporary.name) / 'override.exe')}, clear=True):
            self.assertEqual(helper_path(), Path(self.temporary.name) / 'override.exe')
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(module, '__file__', str(Path(self.temporary.name) / 'lib/permissive/desktop_worker.py')):
            installed = Path(self.temporary.name) / 'native/desktop-agent.exe'
            installed.parent.mkdir()
            installed.touch()
            self.assertEqual(helper_path(), installed)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(module, '__file__', str(Path(self.temporary.name) / 'source/permissive/desktop_worker.py')):
            source = Path(self.temporary.name) / 'source/native/desktop-agent.exe'
            source.parent.mkdir(parents=True)
            source.touch()
            self.assertEqual(helper_path(), source)


@unittest.skipUnless(os.name == 'nt', 'Windows native protocol fixture')
class NativeDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.executable = Path(cls.temporary.name) / 'desktop-protocol-fixture.exe'
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            raise unittest.SkipTest('System framework compiler unavailable')
        result = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64', '/reference:System.Web.Extensions.dll',
                                 '/out:' + str(cls.executable), str(Path(__file__).with_name('DesktopAgentProtocolFixture.cs'))], capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stdout.decode(errors='replace'))
        cls.native = Path(cls.temporary.name) / 'desktop-agent.exe'
        framework = compiler.parent
        references = [framework / 'WPF' / name for name in ['UIAutomationClient.dll', 'UIAutomationTypes.dll', 'WindowsBase.dll']]
        command = [str(compiler), '/nologo', '/target:exe', '/platform:x64', '/out:' + str(cls.native),
                   '/reference:System.Web.Extensions.dll', '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll']
        command.extend('/reference:' + str(reference) for reference in references)
        command.append(str(Path(__file__).resolve().parents[1] / 'windows/DesktopAgent.cs'))
        result = subprocess.run(command, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stdout.decode(errors='replace'))

    def test_real_helper_rejects_invalid_operation_before_ui(self):
        result = run_helper({'operation': 'invalid'}, 3, self.native)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'invalid_arguments')
        self.assertEqual(result['outcome'], 'not_started')

    def test_real_helper_rejects_stale_handle_before_uia_or_input(self):
        result = run_helper({'operation': 'action', 'action': 'key', 'key': 'Return',
                             'window': {'hwnd': '0', 'pid': 0, 'processStartTicks': '0'}}, 3, self.native)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'stale_window')
        self.assertEqual(result['outcome'], 'not_started')

    def test_helper_protocol_has_no_ui_dependency(self):
        result = run_helper({'operation': 'status'}, 3, self.executable)
        self.assertTrue(result['ok'])
        self.assertEqual(result['method'], 'fixture')

    def test_hung_observation_times_out_without_mutation(self):
        start = time.monotonic()
        result = run_helper({'operation': 'hang'}, .25, self.executable)
        self.assertLess(time.monotonic() - start, 4)
        self.assertFalse(result['ok'])
        self.assertEqual(result['outcome'], 'not_started')
        self.assertTrue(result['retrySafe'])

    def test_hung_mutation_has_unknown_outcome_and_no_retry(self):
        start = time.monotonic()
        result = run_helper({'operation': 'hang_after_dispatch'}, .25, self.executable)
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertFalse(result['retrySafe'])

    def test_partial_native_response_after_dispatch_is_unknown(self):
        result = run_helper({'operation': 'malformed_after_dispatch'}, 3, self.executable)
        self.assertEqual(result['error']['code'], 'helper_protocol_error')
        self.assertEqual(result['outcome'], 'unknown')
        self.assertFalse(result['retrySafe'])

    def test_timed_out_input_reports_separate_release_cleanup(self):
        result = run_helper({'operation': 'action', 'action': 'drag', 'modifiers': ['Shift'], 'button': 'left'}, .25, self.executable)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertTrue(result['inputCleanup']['ok'])
        self.assertEqual(result['inputCleanup']['method'], 'fixture')

    def test_native_partial_input_error_also_releases_only_requested_inputs(self):
        result = run_helper({'operation': 'action', 'action': 'click', 'button': 'right', 'finishUnknown': True}, 3, self.executable)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertTrue(result['inputCleanup']['ok'])

    def test_stdio_mcp_discovery_and_call(self):
        environment = {**os.environ, 'PERMISSIVE_HOME': self.temporary.name, 'PERMISSIVE_TASK_ID': 'mcp-test',
                       'PERMISSIVE_DESKTOP_AGENT': str(self.executable)}
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'desktop', 'arguments': {'operation': 'status'}}}]
        result = subprocess.run([sys.executable, '-m', 'permissive.desktop_worker'], input=''.join(json.dumps(message) + '\n' for message in messages),
                                text=True, capture_output=True, env=environment, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line)['result'] for line in result.stdout.splitlines()]
        self.assertEqual(replies[1]['tools'][0]['name'], 'desktop')
        self.assertTrue(json.loads(replies[2]['content'][0]['text'])['ok'])


if __name__ == '__main__':
    unittest.main()
