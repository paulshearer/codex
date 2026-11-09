import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import shutil

from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.service import Service
from permissive.store import Store
from permissive.workspaces import compose_instructions, current_instructions, workspace_request, skill_catalog
from test_service import FakeRuntime


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.runtime = FakeRuntime()
        self.runtime.release.set()
        self.signer = provision(self.home)
        self.service = Service(self.home, self.runtime)
        self.client = SignedTestClient(TestServer(self.service.app()), self.signer)
        await self.client.start_server()
        self.headers = {"X-Test-Sign": "1"}
        self.skill = self.home / ".agents/skills/fixture/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text('---\nname: fixture\ndescription: Selected test skill\n---\nASSIGNED-SKILL-CONTENT', encoding="utf-8")

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def api(self, method, route, body=None, expected=200, extra=None):
        response = await self.client.request(method, route, json=body, headers={**self.headers, **(extra or {})})
        self.assertEqual(response.status, expected, await response.text())
        return await response.json()

    async def workspace(self, **settings):
        return await self.api("POST", "/v1/workspaces", {"name": "Fixture", "workingDirectory": str(self.home), **settings}, 201)

    async def test_defaults_filtering_and_context_snapshot(self):
        first = await self.workspace(systemPrompt="WORKSPACE-A", skills=[str(self.skill)], browserMode="attached")
        second = await self.workspace(name="Other", systemPrompt="WORKSPACE-B")
        task = await self.api("POST", "/v1/tasks", {"prompt": "perform work", "workspaceId": first["id"]}, 202)
        other = await self.api("POST", "/v1/tasks", {"prompt": "other work", "workspaceId": second["id"]}, 202)
        self.assertEqual(task["request"]["workingDirectory"], str(self.home))
        self.assertEqual(task["request"]["browserMode"], "attached")
        await asyncio.sleep(.05)
        context = self.service.store.task_context(task["id"])
        self.assertIn("WORKSPACE-A", compose_instructions(context))
        self.assertIn("ASSIGNED-SKILL-CONTENT", compose_instructions(context))
        self.assertNotIn("WORKSPACE-B", compose_instructions(context))
        self.skill.write_text("CHANGED-SKILL-CONTENT", encoding="utf-8")
        await self.api("PATCH", "/v1/workspaces/" + first["id"], {"systemPrompt": "WORKSPACE-A-UPDATED"})
        continued = await self.api("POST", "/v1/tasks/" + task["id"] + "/continue", {"prompt": "follow up"}, 202)
        self.assertEqual(continued["workspaceId"], task["workspaceId"])
        self.assertEqual(continued["threadId"], self.service.store.get(task["id"])["threadId"])
        self.assertEqual(continued["workspaceRevision"], 2)
        self.assertEqual(self.service.store.task_context(task["id"]), context)
        self.assertIn("CHANGED-SKILL-CONTENT", compose_instructions(self.service.store.task_context(continued["id"])))
        listed = await self.api("GET", "/v1/tasks?workspaceId=" + first["id"] + "&limit=1")
        self.assertEqual([item["id"] for item in listed["data"]], [continued["id"]])
        page = await self.api("GET", "/v1/tasks?workspaceId=" + first["id"] + "&cursor=" + listed["nextCursor"])
        self.assertEqual([item["id"] for item in page["data"]], [task["id"]])
        self.assertNotIn(other["id"], [item["id"] for item in page["data"]])

    async def test_invalid_and_cross_workspace_requests(self):
        first, second = await self.workspace(), await self.workspace(name="Second")
        original = await self.api("POST", "/v1/tasks", {"prompt": "one", "workspaceId": first["id"]}, 202)
        await asyncio.sleep(.05)
        response = await self.client.post("/v1/tasks", json={"prompt": "two", "workspaceId": second["id"], "resumeTaskId": original["id"]}, headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.client.patch("/v1/workspaces/" + first["id"], json={"skills": [str(self.home / "missing/SKILL.md")]}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.service.store.workspace(first["id"])["revision"], 1)
        response = await self.client.post("/v1/tasks", json={"prompt": "x", "workspaceId": "missing"}, headers=self.headers)
        self.assertEqual(response.status, 404)
        response = await self.client.post("/v1/workspaces", json={"name": "bad", "workingDirectory": str(self.home), "swarm": {}}, headers=self.headers)
        self.assertEqual(response.status, 400)

    async def test_assigned_skills_and_missing_document(self):
        workspace = await self.workspace(skills=[str(self.skill)])
        catalog = await self.api("GET", "/v1/skills?workspaceId=" + workspace["id"])
        selected = next(item for item in catalog["data"] if item["path"] == str(self.skill))
        self.assertEqual(selected["name"], "fixture")
        self.assertNotIn("content", selected)
        self.skill.unlink()
        response = await self.client.post("/v1/tasks", json={"prompt": "x", "workspaceId": workspace["id"]}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.service.store.workspace(workspace["id"])["taskCount"], 0)

    async def test_three_skills_save_and_duplicates_do_not_inflate_the_limit(self):
        paths = []
        for name in ("outlook-navigation", "outlook-email", "outlook-calendar"):
            path = self.home / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {name}\n---\nInstructions", encoding="utf-8")
            paths.append(str(path))
        workspace = await self.workspace(skills=paths * 12)
        self.assertEqual(workspace["skills"], paths)
        self.assertEqual(len(self.service.store.workspace(workspace["id"])["skills"]), 3)
        response = await self.client.patch("/v1/workspaces/" + workspace["id"], json={"skills": paths[0]}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertIn("must be a list", await response.text())

    async def test_skill_limit_reports_actual_count_without_saving(self):
        paths = []
        for index in range(33):
            path = self.home / "skills" / str(index) / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("Instructions", encoding="utf-8")
            paths.append(str(path))
        workspace = await self.workspace(skills=paths[:32])
        response = await self.client.patch("/v1/workspaces/" + workspace["id"], json={"skills": paths}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertIn("received 33 selected skills", await response.text())
        self.assertEqual(self.service.store.workspace(workspace["id"])["skills"], paths[:32])

    async def test_legacy_submissions_are_owned_and_idempotent(self):
        body = {"prompt": "legacy", "workingDirectory": str(self.home)}
        first = await self.api("POST", "/v1/tasks", body, 202, {"Idempotency-Key": "legacy"})
        again = await self.api("POST", "/v1/tasks", body, 200, {"Idempotency-Key": "legacy"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(first["workspaceId"])
        self.assertEqual(first["workspaceId"], self.service.store.workspace_for_directory(str(self.home))["id"])

    async def test_retry_reuses_captured_defaults_after_workspace_edit(self):
        workspace = await self.workspace(skills=[str(self.skill)])
        body = {"workspaceId": workspace["id"], "prompt": "do this once"}
        first = await self.api("POST", "/v1/tasks", body, 202, {"Idempotency-Key": "workspace-retry"})
        changed_directory = self.home / "changed"; changed_directory.mkdir()
        await self.api("PATCH", "/v1/workspaces/" + workspace["id"], {"workingDirectory": str(changed_directory), "browserMode": "attached"})
        self.skill.unlink()
        repeated = await self.api("POST", "/v1/tasks", body, 200, {"Idempotency-Key": "workspace-retry"})
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(repeated["request"]["workingDirectory"], str(self.home))
        self.assertEqual(repeated["request"]["browserMode"], "dedicated")

    async def test_paused_context_survives_settings_changes_and_restart(self):
        isolated = self.home / "persist"
        store = Store(isolated)
        workspace = store.save_workspace({"name": "Persistent", "workingDirectory": str(self.home), "systemPrompt": "ORIGINAL", "skills": [str(self.skill)]})
        task, _ = store.submit({"prompt": "resume me", "workspaceId": workspace["id"]})
        store.update(task["id"], status="paused", thread_id="saved-chat")
        context = store.task_context(task["id"])
        store.save_workspace({"systemPrompt": "UPDATED", "skills": []}, workspace["id"])
        store.close()
        self.skill.unlink()
        restored = Store(isolated)
        try:
            self.assertEqual(restored.get(task["id"])["status"], "paused")
            self.assertEqual(restored.get(task["id"])["workspaceId"], workspace["id"])
            self.assertEqual(restored.task_context(task["id"]), context)
            self.assertEqual(restored.workspace(workspace["id"])["systemPrompt"], "UPDATED")
        finally:
            restored.close()


class SkillCatalogCopiesTests(unittest.TestCase):
    def test_identical_packages_collapse_but_selected_paths_and_different_references_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            personal = root / "personal/.codex/skills/outlook"
            personal.mkdir(parents=True)
            (personal / "SKILL.md").write_text("---\nname: outlook\n---\nRead references/help.txt", encoding="utf-8")
            (personal / "references").mkdir()
            (personal / "references/help.txt").write_text("Identical supporting files", encoding="utf-8")
            imported = root / "app/skills/outlook"
            shutil.copytree(personal, imported)
            original, copy = str(personal / "SKILL.md"), str(imported / "SKILL.md")
            with patch("permissive.workspaces.Path.home", return_value=root / "personal"):
                self.assertEqual(len(skill_catalog(root / "app")["data"]), 1)
                selected = skill_catalog(root / "app", selected=[copy])["data"]
                self.assertEqual([item["path"] for item in selected], [copy])
                self.assertEqual(len(skill_catalog(root / "app", selected=[original, copy])["data"]), 2)
                (imported / "references/help.txt").write_text("Different instructions", encoding="utf-8")
                self.assertEqual(len(skill_catalog(root / "app")["data"]), 2)


class WorkspaceMigrationTests(unittest.TestCase):
    def test_current_context_replaces_stale_native_developer_bundle(self):
        old = {"systemPrompt": "OUTDATED", "skills": [], "revision": 1}
        current = {"systemPrompt": "CURRENT", "skills": [], "revision": 2}
        body = {"input": [{"role": "developer", "content": [{"type": "input_text", "text": "KEEP-NATIVE-PERMISSIONS\n" + current_instructions(old)}]},
                          {"role": "developer", "content": "KEEP-OTHER-CONTEXT\nOUTDATED"},
                          {"role": "user", "content": "Quote OUTDATED as historical task text"},
                          {"type": "function_call_output", "call_id": "command", "output": "OUTDATED"}]}
        refreshed = workspace_request(body, current, [compose_instructions(old)])
        self.assertIn("KEEP-NATIVE-PERMISSIONS", refreshed["input"][0]["content"][0]["text"])
        self.assertEqual(refreshed["input"][1]["content"], "KEEP-OTHER-CONTEXT")
        self.assertEqual(refreshed["input"][2:4], body["input"][2:4])
        self.assertIn("CURRENT", refreshed["input"][-1]["content"][0]["text"])
        self.assertNotIn("OUTDATED", refreshed["input"][0]["content"][0]["text"])
        self.assertIn("OUTDATED", body["input"][0]["content"][0]["text"])

    def test_existing_chats_are_grouped_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            db = sqlite3.connect(home / "tasks.sqlite")
            db.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, thread_id TEXT, result TEXT, error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, idempotency_key TEXT UNIQUE); CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, created_at INTEGER NOT NULL);")
            for ident, cwd in (("one", home / "first"), ("two", home / "first"), ("three", home / "second")):
                db.execute("INSERT INTO tasks VALUES (?,?, 'completed', ?, 'preserved', NULL, 1, 1, NULL)", (ident, json.dumps({"prompt": ident, "workingDirectory": str(cwd)}), "chat-" + ident))
            db.execute("INSERT INTO events(task_id,kind,data,created_at) VALUES ('one','agent','{}',1)")
            db.commit(); db.close()
            store = Store(home)
            try:
                ids = [store.get(ident)["workspaceId"] for ident in ("one", "two", "three")]
                self.assertEqual(ids[0], ids[1]); self.assertNotEqual(ids[0], ids[2])
                self.assertEqual(store.get("one")["result"], "preserved")
                self.assertEqual(store.get("one")["threadId"], "chat-one")
                self.assertEqual(len(store.events("one")), 1)
                self.assertEqual(len(store.workspaces()["data"]), 2)
                store.close(); store = Store(home)
                self.assertEqual(store.get("one")["workspaceId"], ids[0])
                self.assertEqual(len(store.workspaces()["data"]), 2)
            finally:
                store.close()
