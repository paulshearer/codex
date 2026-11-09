import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from permissive.store import Store
from permissive.telemetry import UsageTracker


def usage(total, last):
    return {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": total, "last": last}}}


class TelemetryTests(unittest.TestCase):
    def test_usage_excludes_previous_chat_and_accumulates_requests(self):
        tracker = UsageTracker()
        old = {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110}
        new = {"inputTokens": 30, "cachedInputTokens": 20, "outputTokens": 5, "reasoningOutputTokens": 2, "totalTokens": 35}
        self.assertIsNone(tracker.observe(usage(old, old)))
        tracker.observe({"method": "turn/started"})
        total = {key: old.get(key, 0) + value for key, value in new.items()}
        self.assertEqual(tracker.observe(usage(total, new)), new)
        self.assertEqual(tracker.observe(usage(total, new)), new)
        tracker.observe({"method": "turn/started"})
        total = {key: total.get(key, 0) + value for key, value in new.items()}
        self.assertEqual(tracker.observe(usage(total, new)), {key: value * 2 for key, value in new.items()})

    def test_completion_timestamp_and_usage_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with patch('permissive.store.time.time', return_value=100):
                task = store.submit({"prompt": "Fixture", "workingDirectory": directory})[0]
            with patch('permissive.store.time.time', return_value=160):
                store.update(task['id'], status='completed')
            with patch('permissive.store.time.time', return_value=200):
                store.update(task['id'], thread_id='saved')
            counts = {"inputTokens": 100, "outputTokens": 10}
            store.event(task['id'], 'usage', counts)
            store.close()
            store = Store(Path(directory))
            self.assertEqual(store.get(task['id'])['completedAt'], 160)
            self.assertEqual(store.get(task['id'])['tokenUsage'], counts)
            store.close()

    def test_legacy_migration_recovers_saved_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(Store(Path(directory))) as store:
                # Legacy installations kept event payloads in the central DB.
                # Prevent new-chat segmentation while constructing that layout.
                with patch.object(store.segments, 'migrate'):
                    task = store.submit({"prompt": "Fixture", "workingDirectory": directory})[0]
                self.assertFalse(store.segments.ready(task['conversationId']))
                store.event(task['id'], 'agent', {"method": "turn/started"})
                counts = {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110}
                store.event(task['id'], 'agent', usage(counts, counts))
                store.update(task['id'], status='completed')
                updated = store.get(task['id'])['updatedAt']
                with store.db:
                    store.db.execute('ALTER TABLE tasks DROP COLUMN completed_at')
                    store.db.execute('ALTER TABLE tasks DROP COLUMN token_usage')
            with closing(Store(Path(directory))) as store:
                self.assertEqual(store.get(task['id'])['completedAt'], updated)
                self.assertEqual(store.get(task['id'])['tokenUsage']['totalTokens'], 110)
