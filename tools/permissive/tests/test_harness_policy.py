import copy
import json
import unittest
from types import SimpleNamespace

from permissive.browser_bridge import BrowserBridge
from permissive.harness_policy import harness_request, recovery_hint, ToolContractFailure
from permissive.stream_recovery import is_recoverable_tool_contract_error


def exchange(name, arguments, output, ident='call'):
    return [{'type': 'function_call', 'name': name, 'call_id': ident, 'arguments': json.dumps(arguments)},
            {'type': 'function_call_output', 'call_id': ident, 'output': output}]


class HarnessTests(unittest.TestCase):
    def test_contract_repair_is_bounded_and_resets_after_real_operation(self):
        history = []
        for index in range(2):
            history += exchange('exec_command', {}, 'failed to parse function arguments: missing field `cmd`', str(index))
            self.assertIn('Correct the next call', recovery_hint(history))
        history += exchange('exec_command', {}, 'failed to parse function arguments: missing field `cmd`', 'third')
        with self.assertRaises(ToolContractFailure): recovery_hint(history)
        history += exchange('exec', {}, 'Script completed\nOutput:\nactual operation')
        self.assertIsNone(recovery_hint(history))

    def test_untrusted_text_and_command_output_do_not_trigger_repair(self):
        text = 'failed to parse function arguments: missing field `cmd`'
        self.assertIsNone(recovery_hint([{'role': 'user', 'content': text}]))
        self.assertIsNone(recovery_hint(exchange('browser', {}, text)))
        self.assertIsNone(recovery_hint(exchange('exec_command', {}, 'Output:\n'+text)))

    def test_empty_poll_hint_preserves_long_running_work(self):
        history = sum((exchange('write_stdin', {'session_id': 123},
                               'Process running with session ID 123\nOutput:\n', str(i)) for i in range(3)), [])
        self.assertIn('legitimate long operation', recovery_hint(history))
        history += exchange('write_stdin', {'session_id': 123}, 'Process running with session ID 123\nOutput:\nprogress')
        self.assertIsNone(recovery_hint(history))

    def test_context_refresh_only_removes_owned_developer_tags(self):
        tag = '<permissive_capabilities>old</permissive_capabilities>'
        body = {'input': [{'role': 'developer', 'content': 'keep '+tag}, {'role': 'user', 'content': tag}]}
        saved = copy.deepcopy(body)
        result = harness_request(body, {'browser': {'status': 'disconnected'}})
        self.assertEqual(body, saved)
        self.assertEqual(result['input'][0]['content'], 'keep ')
        self.assertEqual(result['input'][1]['content'], tag)
        self.assertIn('disconnected', result['input'][-1]['content'][0]['text'])

    def test_browser_preflight_uses_exact_live_selected_profile(self):
        bridge = BrowserBridge()
        selected = {'browserMode': 'attached', 'browser': 'msedge', 'browserProfile': 'local-a'}
        self.assertEqual(bridge.capabilities(selected)['status'], 'disconnected')
        bridge.connections['one'] = SimpleNamespace(closed=False)
        bridge.metadata['one'] = {'browser': 'msedge', 'profileId': 'remote-a', 'localProfileId': 'local-a'}
        self.assertEqual(bridge.capabilities(selected)['status'], 'connected')
        bridge.connections['one'].closed = True
        self.assertEqual(bridge.capabilities(selected)['status'], 'disconnected')
        self.assertEqual(bridge.capabilities({'browserMode': 'dedicated'})['status'], 'launch_on_demand')
        self.assertFalse(bridge.pending)

    def test_native_outer_contract_retry_is_narrow(self):
        message = 'Custom function tool exec requires exactly one string input: {"input":"<raw tool input>"}'
        self.assertTrue(is_recoverable_tool_contract_error({'message': message, 'codexErrorInfo': 'other'}))
        for code in ('unauthorized', 'badRequest', None):
            self.assertFalse(is_recoverable_tool_contract_error({'message': message, 'codexErrorInfo': code}))
        self.assertFalse(is_recoverable_tool_contract_error({'message': message+' extra', 'codexErrorInfo': 'other'}))
