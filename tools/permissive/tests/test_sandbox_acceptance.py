"""Acceptance proof validation without a VM, model call or desktop input."""
import json
import os
import socket
import subprocess
import unittest
import uuid

from tests.sandbox_acceptance import AcceptanceFailure, MODEL, PROBE, desktop_evidence, verify_binding, verify_controller, verify_execution, verify_probe


def binding():
    return {'schema': 'codex-permissive-sandbox-binding/v1', 'role': 'controller', 'vmId': str(uuid.uuid4()),
            'sandboxId': str(uuid.uuid4()), 'guestCredentialPath': 'guest.dpapi', 'packagePath': 'C:/package'}


def tool_event(operation, result, image=False):
    content = [{'type': 'text', 'text': json.dumps({'ok': True, **result})}]
    if image:
        content.append({'type': 'image', 'mimeType': 'image/png', 'data': 'fixture'})
    return {'kind': 'agent', 'data': {'method': 'item/completed', 'params': {'item': {
        'type': 'mcpToolCall', 'server': 'desktop', 'tool': 'desktop', 'status': 'completed',
        'arguments': {'operation': operation}, 'result': {'content': content}}}}}


class SandboxAcceptanceTests(unittest.TestCase):
    def test_controller_and_execution_bindings_reject_local_fallback(self):
        bound = binding()
        verify_binding(bound)
        environment = {'vmId': bound['vmId'], 'environmentId': bound['sandboxId'], 'phase': 'ready'}
        verify_controller({'executionRole': 'controller', 'maxConcurrentModelRequests': 1}, environment, bound)
        with self.assertRaises(AcceptanceFailure):
            verify_controller({'executionRole': 'guest', 'maxConcurrentModelRequests': 1}, environment, bound)
        with self.assertRaises(AcceptanceFailure):
            verify_execution({'execution': {'location': 'local'}}, bound)
        execution = {'location': 'hyperv', 'vmId': bound['vmId'], 'environmentId': bound['sandboxId'], 'attemptId': str(uuid.uuid4()), 'generation': 1}
        self.assertEqual(verify_execution({'execution': execution}, bound), execution)
        with self.assertRaises(AcceptanceFailure):
            verify_execution({'execution': {**execution, 'vmId': str(uuid.uuid4())}}, bound)

    def events(self, display='Display is 703'):
        result = [tool_event('status', {'session': {'canInject': True, 'locked': False, 'sessionId': 1, 'integrity': 0x3000}}),
                  tool_event('windows', {}), tool_event('launch', {}),
                  tool_event('inspect', {'window': {'title': 'Calculator'}, 'elements': [{'name': display}]}),
                  tool_event('action', {'method': 'uia.Invoke'}), tool_event('capture', {}, image=True)]
        result.append({'kind': 'agent', 'data': {'method': 'item/completed', 'params': {'item': {
            'type': 'commandExecution', 'status': 'completed', 'exitCode': 0}}}})
        return result

    def test_tool_proof_requires_actual_calculator_display_and_vision_block(self):
        proof = desktop_evidence(self.events())
        self.assertEqual(proof['calculatorDisplays'][0]['text'], 'Display is 703')
        with self.assertRaises(AcceptanceFailure):
            desktop_evidence(self.events('Display is 0'))
        events = self.events()
        events[5]['data']['params']['item']['result']['content'] = [{'type': 'text', 'text': '{"ok":true}'}]
        with self.assertRaises(AcceptanceFailure):
            desktop_evidence(events)

    def test_independent_native_model_proof_rejects_another_provider_or_host(self):
        native = str(uuid.uuid4())
        guest = 'acceptance-guest' if socket.gethostname().lower() != 'acceptance-guest' else 'another-guest'
        proof = {'hostname': guest, 'nativeThreadId': native, 'sessions': [{'id': native, 'modelProvider': 'deepseek'}],
                 'contexts': [{'model': MODEL}], 'rollouts': [{'name': 'rollout-' + native + '.jsonl'}],
                 'osBuild': '26300', 'osCaption': 'Microsoft Windows 11 Pro', 'osArchitecture': '64-bit', 'osVersion': '10.0.26300'}
        self.assertEqual(verify_probe(proof, binding(), native, MODEL)['models'], [MODEL])
        with self.assertRaises(AcceptanceFailure):
            verify_probe({**proof, 'sessions': [{'id': native, 'modelProvider': 'openai'}]}, binding(), native, MODEL)
        with self.assertRaises(AcceptanceFailure):
            verify_probe({**proof, 'hostname': socket.gethostname()}, binding(), native, MODEL)

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell parser')
    def test_fixed_read_only_probe_parses_in_windows_powershell(self):
        command = "$source=[Console]::In.ReadToEnd(); $tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors) | Out-Null; if ($errors.Count) { $errors | ForEach-Object {$_.Message}; exit 1 }; 'PARSE-OK'"
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command],
                                input=PROBE, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PARSE-OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
