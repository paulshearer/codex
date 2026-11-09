import json
import tempfile
import time
import unittest
from urllib.request import Request, urlopen

from custom_models.facade import Facade
from custom_models.registry import Provider
from custom_models.chat import CompatibilityError
from test_facade import MockProvider, MODEL, complete_response, decoded_events, sse


class GenerationProgressTests(unittest.TestCase):
    def run_stream(self, chunks):
        upstream = MockProvider(lambda _: (200, 'text/event-stream', chunks))
        with tempfile.TemporaryDirectory() as home:
            provider = Provider('deepseek', 'DeepSeek', 'responses', upstream.url,
                                stream_idle_timeout_ms=0, upstream_timeout_seconds=None)
            facade = Facade(provider, [MODEL], home, generation_idle_timeout_seconds=.25)
            facade.start()
            try:
                request = Request(facade.base_url+'/responses', json.dumps({
                    'model': MODEL.upstream_model, 'input': 'hello', 'stream': True}).encode(),
                    {'Content-Type': 'application/json', 'Authorization': 'Bearer '+facade.token})
                began = time.monotonic()
                with urlopen(request, timeout=4) as response:
                    events = decoded_events(response.read())
                return events, list(facade.records), time.monotonic()-began
            finally:
                facade.stop(); upstream.stop()

    def test_long_prefill_before_first_delta_is_allowed(self):
        response = complete_response()
        events, records, _ = self.run_stream([
            (0, sse({'type': 'response.created', 'response': {**response, 'output': [], 'status': 'in_progress'}})),
            (.65, sse({'type': 'response.output_text.delta', 'item_id': 'msg1', 'output_index': 0, 'content_index': 0, 'delta': 'done'})),
            (0, sse({'type': 'response.completed', 'response': response}))])
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertNotEqual(records[-1].get('failure'), 'generation_no_progress')

    def test_heartbeats_do_not_hide_stalled_generation(self):
        chunks = [(0, sse({'type': 'response.output_text.delta', 'item_id': 'msg1', 'output_index': 0, 'content_index': 0, 'delta': 'started'}))]
        chunks += [(.08, b': heartbeat\n\n') for _ in range(15)]
        events, records, elapsed = self.run_stream(chunks)
        self.assertEqual(events[-1]['type'], 'response.failed')
        self.assertEqual(events[-1]['response']['error']['code'], 'adapter_no_progress')
        self.assertEqual(records[-1]['failure'], 'generation_no_progress')
        self.assertLess(elapsed, 1)

    def test_watchdog_configuration_rejects_ambiguous_values(self):
        provider = Provider('deepseek', 'DeepSeek', 'responses', 'http://127.0.0.1:1/v1')
        for value in (True, -1, float('nan'), float('inf'), '1'):
            with self.assertRaises(CompatibilityError):
                Facade(provider, [MODEL], '.', generation_idle_timeout_seconds=value)
