import copy
import unittest

from permissive.runtime import browser_instructions, browser_request
from permissive.workspaces import workspace_request


class RuntimeBrowserContextTests(unittest.TestCase):
    def test_followup_refreshes_browser_selection_without_changing_task_evidence(self):
        previous = {"browserMode": "attached", "browser": "chrome", "browserProfile": "old-profile"}
        current = {"browserMode": "attached", "browser": "msedge", "browserProfile": "local-" + "a" * 24}
        history = {"input": [
            {"role": "developer", "content": "Other instructions\n" + browser_instructions(previous)},
            {"role": "developer", "content": [{"type": "input_text", "text": browser_instructions(previous)}]},
            {"role": "user", "content": "Find the message in old-profile"},
            {"type": "function_call_output", "call_id": "browser-list", "output": "old-profile disconnected"},
        ]}
        original = copy.deepcopy(history)
        refreshed = browser_request(workspace_request(history, {}), current)
        developer = "\n".join(item["content"] if isinstance(item.get("content"), str) else "\n".join(
            part.get("text", "") for part in item.get("content", []))
            for item in refreshed["input"] if item.get("role") == "developer")
        self.assertIn('"browser": "msedge"', developer)
        self.assertIn(current["browserProfile"], developer)
        self.assertEqual(developer.count("<permissive_browser_context>"), 1)
        self.assertNotIn("old-profile", developer)
        self.assertIn("Other instructions", developer)
        self.assertEqual(refreshed["input"][2:4], original["input"][2:4])
        self.assertEqual(history, original)

    def test_browser_selection_is_restored_after_compaction(self):
        request = {"browserMode": "dedicated"}
        refreshed = browser_request(workspace_request({"input": "Continue the task"}, {}), request)
        selected = refreshed["input"][-1]["content"][0]["text"]
        self.assertIn('"mode": "dedicated"', selected)
        self.assertIn('"browser": "chromium"', selected)
        self.assertIn('"profileId": ""', selected)
        self.assertIn("routes to this selection automatically", selected)
