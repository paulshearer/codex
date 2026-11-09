import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from permissive.skill_library import export_bundle, import_bundle
from permissive.store import Store
import test_workspaces


class SkillLibraryApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_workspaces.WorkspaceTests.asyncSetUp
    asyncTearDown = test_workspaces.WorkspaceTests.asyncTearDown
    api = test_workspaces.WorkspaceTests.api
    workspace = test_workspaces.WorkspaceTests.workspace

    async def test_categories_preview_and_portable_round_trip(self):
        resources = self.skill.parent / "references" / "example.txt"
        resources.parent.mkdir()
        resources.write_text("portable supporting evidence", encoding="utf-8")
        await self.api("POST", "/v1/skills/register", {"paths": [str(self.skill)]})
        await self.api("PATCH", "/v1/skills/categories", {"paths": [str(self.skill)], "category": "Development"})
        catalog = await self.api("GET", "/v1/skills")
        self.assertEqual(next(item for item in catalog["data"] if item["path"] == str(self.skill))["category"], "Development")
        from urllib.parse import urlencode
        document = await self.api("GET", "/v1/skills/document?" + urlencode({"path": str(self.skill)}))
        self.assertIn("ASSIGNED-SKILL-CONTENT", document["content"])
        response = await self.client.post("/v1/skills/export", json={"paths": [str(self.skill)]}, headers=self.headers)
        self.assertEqual(response.status, 200)
        bundle = self.home / "export.zip"
        bundle.write_bytes(await response.read())
        imported = (await self.api("POST", "/v1/skills/import", {"file": str(bundle)}, 201))["data"][0]
        path = Path(imported["path"])
        self.assertEqual(path.read_bytes(), self.skill.read_bytes())
        self.assertEqual((path.parent / "references/example.txt").read_bytes(), resources.read_bytes())
        self.assertEqual(imported["category"], "Development")
        repeated = (await self.api("POST", "/v1/skills/import", {"file": str(bundle)}, 201))["data"][0]
        self.assertNotEqual(repeated["path"], imported["path"])
        workspace = await self.workspace(skills=[imported["path"]])
        task = await self.api("POST", "/v1/tasks", {"prompt": "use imported skill", "workspaceId": workspace["id"]}, 202)
        self.assertEqual(self.service.store.task_context(task["id"])["skills"][0]["path"], imported["path"])

    async def test_browser_preferences_are_captured_and_continued(self):
        workspace = await self.workspace(browser="msedge", browserProfile="work")
        task = await self.api("POST", "/v1/tasks", {"prompt": "browse", "workspaceId": workspace["id"]}, 202)
        self.assertEqual(task["request"]["browser"], "msedge")
        self.assertEqual(task["request"]["browserProfile"], "work")
        import asyncio
        await asyncio.sleep(.05)
        await self.api("PATCH", "/v1/workspaces/" + workspace["id"], {"browser": "chrome", "browserProfile": "other"})
        followup = await self.api("POST", "/v1/tasks/" + task["id"] + "/continue", {"prompt": "continue browsing"}, 202)
        self.assertEqual(followup["request"]["browser"], "msedge")
        self.assertEqual(followup["request"]["browserProfile"], "work")
        response = await self.client.patch("/v1/workspaces/" + workspace["id"], json={"browserProfile": "../../escape"}, headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post("/v1/tasks", json={"prompt": "browse", "workspaceId": workspace["id"], "browser": "unknown"}, headers=self.headers)
        self.assertEqual(response.status, 400)
        catalog = await self.api("GET", "/v1/browsers")
        self.assertIn("other", next(item for item in catalog["data"] if item["id"] == "chrome")["profiles"])

    async def test_library_validation_is_atomic(self):
        response = await self.client.patch("/v1/skills/categories", json={"paths": [str(self.skill), str(self.home / "missing/SKILL.md")], "category": "Invalid group"}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.service.skill_categories(), {})
        for route in ("/v1/skills/register", "/v1/skills/import", "/v1/skills/export"):
            response = await self.client.post(route, json=[], headers=self.headers)
            self.assertEqual(response.status, 400)


class BrowserSettingsPersistenceTests(unittest.TestCase):
    def test_workspace_settings_and_categories_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = Store(home)
            workspace = store.save_workspace({"name": "Browser workspace", "workingDirectory": directory, "browser": "chrome", "browserProfile": "office"})
            with store.db:
                store.db.execute("INSERT INTO skill_library VALUES (?,?)", ("example", "Operations"))
            store.close()
            store = Store(home)
            try:
                self.assertEqual(store.workspace(workspace["id"])["browser"], "chrome")
                self.assertEqual(store.workspace(workspace["id"])["browserProfile"], "office")
                self.assertEqual(store.db.execute("SELECT category FROM skill_library WHERE path='example'").fetchone()[0], "Operations")
            finally:
                store.close()


class SkillBundleValidationTests(unittest.TestCase):
    def test_rejects_oversized_expansion_and_unsupported_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            file = home / "large.zip"
            with zipfile.ZipFile(file, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("skills/0/large.txt", "x" * 4096)
            with patch("permissive.skill_library.MAX_BUNDLE_BYTES", 2048), self.assertRaises(ValueError):
                import_bundle(home, str(file))
            for manifest in ([], {"format": "other", "version": 1}, {"format": "codex-permissive-skills", "version": 1, "skills": ["invalid"]}):
                with zipfile.ZipFile(file, "w") as archive:
                    archive.writestr("manifest.json", json.dumps(manifest))
                with self.assertRaises(ValueError):
                    import_bundle(home, str(file))
                self.assertEqual(list((home / "skills").iterdir()), [])

    def test_rejects_unsafe_paths_links_duplicates_and_missing_skills_without_partial_import(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for unsafe in ["../escape", "skills/0/C:/escape", "skills/0/CON.txt", "skills/0/../escape", "skills\\0\\escape", "skills/0/file."]:
                bundle = home / "unsafe.zip"
                with zipfile.ZipFile(bundle, "w") as archive:
                    archive.writestr(unsafe, "bad")
                with self.assertRaises(ValueError):
                    import_bundle(home, str(bundle))
                self.assertEqual(list((home / "skills").iterdir()), [])
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("manifest.json", json.dumps({"format": "codex-permissive-skills", "version": 1, "skills": [{"folder": "skills/0"}, {"folder": "skills/1"}]}))
                archive.writestr("skills/0/SKILL.md", "valid skill")
            with self.assertRaises(ValueError):
                import_bundle(home, str(bundle))
            self.assertEqual(list((home / "skills").iterdir()), [])
            info = zipfile.ZipInfo("skills/0/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(info, "../../escape")
            with self.assertRaises(ValueError):
                import_bundle(home, str(bundle))
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("skills/0/file", "one")
                archive.writestr("skills/0/FILE", "two")
            with self.assertRaises(ValueError):
                import_bundle(home, str(bundle))

    def test_export_preserves_duplicate_names_in_separate_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = []
            for name in ("first", "second"):
                path = home / name / "SKILL.md"
                path.parent.mkdir()
                path.write_text("---\nname: same-name\n---\n" + name, encoding="utf-8")
                paths.append(str(path))
            content = export_bundle(paths, {paths[0]: "Operations"})
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                self.assertIn("first", archive.read("skills/0/SKILL.md").decode())
                self.assertIn("second", archive.read("skills/1/SKILL.md").decode())
