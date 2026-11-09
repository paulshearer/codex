import asyncio
import unittest

import test_workspaces
from permissive.chat_settings import collaboration_mode
from permissive.workspaces import compose_instructions, workspace_request


class ChatSettingsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_workspaces.WorkspaceTests.asyncSetUp
    asyncTearDown = test_workspaces.WorkspaceTests.asyncTearDown
    api = test_workspaces.WorkspaceTests.api
    workspace = test_workspaces.WorkspaceTests.workspace

    async def test_chat_overrides_survive_followups_without_changing_workspace(self):
        workspace = await self.workspace(skills=[str(self.skill)], browser="chromium")
        first = await self.api("POST", "/v1/tasks", {"workspaceId": workspace["id"], "prompt": "plan this", "browser": "msedge", "browserProfile": "chat-only", "skills": [], "mode": "plan"}, 202)
        context = self.service.store.task_context(first["id"])
        self.assertEqual(context["skills"], [])
        self.assertIn("Do not edit files", compose_instructions(context))
        await asyncio.sleep(.05)
        second = await self.api("POST", "/v1/tasks/" + first["id"] + "/continue", {"prompt": "implement", "mode": "goal", "goal": "Implement and verify", "skills": [str(self.skill)], "browser": "chrome", "browserProfile": "office"}, 202)
        self.assertEqual(second["threadId"], self.service.store.get(first["id"])["threadId"])
        self.assertEqual(second["request"]["browser"], "chrome")
        current = self.service.store.task_context(second["id"])
        self.assertEqual(current["skills"][0]["path"], str(self.skill))
        self.assertIn("explicitly selected", compose_instructions(current))
        self.assertIn("persisted native goal", compose_instructions(current))
        self.assertEqual(context, self.service.store.task_context(first["id"]))
        await asyncio.sleep(.05)
        third = await self.api("POST", "/v1/tasks/" + second["id"] + "/continue", {"prompt": "continue"}, 202)
        for field in ("browser", "browserProfile", "skills", "mode", "goal"):
            self.assertEqual(third["request"][field], second["request"][field])
        saved = self.service.store.workspace(workspace["id"])
        self.assertEqual(saved["browser"], "chromium")
        self.assertEqual(saved["skills"], [str(self.skill)])
        independent = await self.api("POST", "/v1/tasks", {"workspaceId": workspace["id"], "prompt": "other chat"}, 202)
        self.assertEqual(independent["request"]["browser"], "chromium")
        self.assertEqual(independent["request"]["mode"], "default")
        developer = workspace_request({"input": [{"role": "developer", "content": compose_instructions(context)}]}, current, [compose_instructions(context)])
        self.assertNotIn("Do not edit files", developer["input"][0]["content"])

    async def test_validation_idempotency_and_reset(self):
        workspace = await self.workspace()
        body = {"workspaceId": workspace["id"], "prompt": "chat", "mode": "plan", "skills": [str(self.skill)]}
        first = await self.api("POST", "/v1/tasks", body, 202, {"Idempotency-Key": "chat-overrides"})
        await asyncio.sleep(.05)
        repeated = await self.api("POST", "/v1/tasks", body, 200, {"Idempotency-Key": "chat-overrides"})
        self.assertEqual(repeated["id"], first["id"])
        for values in ({"mode": "bad"}, {"skills": "not-a-list"}, {"goal": "x" * 8193}, {"browserProfile": "../escape"}):
            response = await self.client.post("/v1/tasks/" + first["id"] + "/continue", json={"prompt": "invalid", **values}, headers=self.headers)
            self.assertEqual(response.status, 400)
        reset = await self.api("POST", "/v1/tasks/" + first["id"] + "/continue", {"prompt": "default", "mode": "default", "goal": "old objective", "skills": []}, 202)
        self.assertEqual(reset["request"]["goal"], "")
        self.assertEqual(self.service.store.task_context(reset["id"])["skills"], [])
        self.assertEqual(collaboration_mode(first["request"], "fixture")["mode"], "plan")
        self.assertEqual(collaboration_mode(reset["request"], "fixture")["mode"], "default")
