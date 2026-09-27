import asyncio
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from custom_models.registry import Registry
from custom_models.router import Backend, Router
from test_registry import sample_registry


class FakeBackend:
    def __init__(self, router, label, model=None):
        self.router, self.label, self.model = router, label, model
        self.calls, self.sent = [], []
        self.closed = False
        self.fail_resume = False
        router.created.append(self)

    async def start(self):
        return {"userAgent": "native-test"}

    async def send(self, message):
        self.sent.append(copy.deepcopy(message))

    async def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "config/read":
            return copy.deepcopy(
                getattr(self.router, "native_config_result", {"config": {}})
            )
        if method == "model/list":
            return {
                "data": [{"id": "gpt", "model": "gpt", "isDefault": True}],
                "nextCursor": None,
            }
        if method == "account/read":
            return copy.deepcopy(
                self.router.custom_account if self.model else self.router.main_account
            )
        if method == "thread/start":
            tid = f"thread-{len(self.router.created)}"
            self.router.saved[tid] = {
                "id": tid,
                "model": self.model.upstream_model
                if self.model
                else params.get("model"),
                "modelProvider": self.model.provider if self.model else "openai",
                "path": str(self.router.rollout),
            }
            return {
                "thread": self.router.saved[tid],
                "model": self.router.saved[tid]["model"],
                "modelProvider": self.router.saved[tid]["modelProvider"],
                "approvalPolicy": params.get("approvalPolicy"),
                "approvalsReviewer": params.get("approvalsReviewer"),
            }
        if method == "thread/read":
            return {
                "thread": self.router.saved.get(
                    params["threadId"],
                    {
                        "id": params["threadId"],
                        "modelProvider": "openai",
                        "model": "gpt",
                        "path": str(self.router.rollout),
                    },
                )
            }
        if method == "thread/resume":
            if self.fail_resume:
                raise RuntimeError("Deliberate resume failure")
            return {
                "thread": {"id": params["threadId"]},
                "model": params.get("model"),
                "modelProvider": params.get("modelProvider")
                or (self.model.provider if self.model else "openai"),
            }
        if method == "thread/fork":
            return {
                "thread": {"id": "forked-thread"},
                "model": self.model.upstream_model,
                "modelProvider": self.model.provider,
            }
        if method == "thread/compact/start":
            self.router.observe(
                self,
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"status": "completed"},
                    },
                },
            )
            return {}
        if method == "turn/start":
            return {
                "turn": {"id": "native-turn-id", "status": "inProgress"},
                "model": self.model.upstream_model
                if self.model
                else params.get("model"),
            }
        return {}

    async def close(self):
        self.closed = True


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_close_finishes_when_exited_process_stdout_stays_open(self):
        backend = Backend(self.router, "inherited-pipe")
        backend.proc = SimpleNamespace(stdin=io.BytesIO(), wait=lambda: 0)
        backend.reader = asyncio.create_task(backend.read())
        backend.lines = asyncio.Queue()
        await asyncio.wait_for(backend.close(), 4)
        self.assertTrue(backend.proc.stdin.closed)
        self.assertTrue(backend.reader.cancelled())

    async def test_desktop_catalog_discovery_preserves_native_configuration(self):
        original = {
            "config": {"approval_policy": "on-request"},
            "layers": [],
            "origins": {},
        }
        self.router.native_config_result = copy.deepcopy(original)
        result = await self.router.dispatch("config/read", {"includeLayers": True})
        catalog = Path(result["config"].pop("model_catalog_json"))
        self.assertTrue(catalog.is_file())
        self.assertEqual(result, original)
        self.assertEqual(self.router.native_config_result, original)
        entries = await self.router.dispatch(
            "model/list", {"includeHidden": True, "limit": 100}
        )
        self.assertIn("deepseek::flash", [entry["model"] for entry in entries["data"]])
        original["config"]["model_catalog_json"] = "user-catalog.json"
        self.router.native_config_result = original
        self.assertEqual(await self.router.dispatch("config/read", {}), original)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.registry = Registry.from_dict(sample_registry())
        self.router = self.make_router()
        await self.router.dispatch("initialize", {"clientInfo": {"name": "test"}})

    def make_router(self):
        router = Router(
            "native.exe",
            self.registry,
            self.directory.name,
            {"deepseek": "http://127.0.0.1:9/v1"},
            {"deepseek": "ephemeral-token"},
        )
        router.backend_factory = FakeBackend
        router.created, router.saved = [], {}
        router.main_account = {
            "account": None,
            "requiresOpenaiAuth": True,
            "workspaceRouting": None,
        }
        router.custom_account = {
            "account": None,
            "requiresOpenaiAuth": False,
            "workspaceRouting": None,
        }
        router.rollout = Path(self.directory.name) / "saved-rollout.jsonl"
        router.rollout.touch()
        return router

    async def start_custom(self, **params):
        return await self.router.dispatch(
            "thread/start", {"model": "deepseek::flash", **params}
        )

    async def test_fresh_custom_profile_reads_native_provider_account_metadata(self):
        main = self.router.main
        params = {"refreshToken": False}
        actual = {**self.router.custom_account, "nativeMetadata": "preserved"}
        self.router.custom_account = actual
        result = await self.router.dispatch("account/read", params)
        metadata = self.router.created[-1]
        self.assertEqual(result, actual)
        self.assertEqual(metadata.model, self.registry.model())
        self.assertEqual(main.calls, [("account/read", params)])
        self.assertEqual(metadata.calls, [("account/read", params)])
        self.assertTrue(metadata.closed)
        self.assertIs(self.router.main, main)
        self.assertFalse(main.closed)
        self.assertFalse(self.router.threads)
        self.assertFalse((Path(self.directory.name) / "auth.json").exists())

    async def test_authenticated_main_account_and_login_keep_native_routing(self):
        actual = {
            "account": {
                "type": "chatgpt",
                "email": "test@example.com",
                "planType": "plus",
            },
            "requiresOpenaiAuth": True,
            "workspaceRouting": {
                "chatgptAccountId": "actual-account",
                "backendOrigin": "https://example.com",
            },
        }
        self.router.main_account = actual
        created = len(self.router.created)
        self.assertEqual(
            await self.router.dispatch("account/read", {"refreshToken": True}), actual
        )
        self.assertEqual(len(self.router.created), created)
        await self.router.dispatch("account/login/start", {"type": "chatgpt"})
        self.assertEqual(
            self.router.main.calls[-1], ("account/login/start", {"type": "chatgpt"})
        )
        self.router.main_account = {"account": None, "requiresOpenaiAuth": False}
        self.assertEqual(
            await self.router.dispatch("account/read", {}), self.router.main_account
        )
        self.assertEqual(len(self.router.created), created)

    async def test_custom_account_metadata_backend_closes_on_native_error(self):
        original_factory = self.router.backend_factory

        def factory(*args):
            backend = original_factory(*args)
            backend.call = AsyncMock(
                side_effect=RuntimeError("Native account metadata failed")
            )
            return backend

        self.router.backend_factory = factory
        with self.assertRaisesRegex(RuntimeError, "Native account metadata failed"):
            await self.router.dispatch("account/read", {})
        self.assertTrue(self.router.created[-1].closed)
        self.assertFalse(self.router.main.closed)

    async def test_desktop_provider_only_prewarm_and_default_start(self):
        result = await self.router.dispatch(
            "thread/start", {"model": None, "modelProvider": "deepseek"}
        )
        self.assertEqual(
            (result["model"], result["modelProvider"]), ("deepseek::flash", "deepseek")
        )
        implicit = await self.router.dispatch("thread/start", {})
        self.assertEqual(implicit["model"], "deepseek::flash")

    async def test_desktop_config_merge_keeps_controls_and_restores_provider_metadata(
        self,
    ):
        controls = {
            "approval_policy": "on-request",
            "approvals_reviewer": "guardian",
            "permissions": "restricted",
        }
        incoming = {
            "model": "deepseek::flash",
            "approvalsReviewer": "guardian",
            "approvalPolicy": "on-request",
            "config": {
                **controls,
                "model_context_window": 999999,
                "model_providers": {"deepseek": {"base_url": "wrong"}},
            },
            "collaborationMode": {
                "mode": "default",
                "settings": {"model": "deepseek::flash", "reasoning_effort": "high"},
            },
        }
        original = copy.deepcopy(incoming)
        result = await self.router.dispatch("thread/start", incoming)
        forwarded = self.router.created[-1].calls[-1][1]
        self.assertEqual({key: forwarded["config"][key] for key in controls}, controls)
        self.assertEqual(
            (forwarded["approvalPolicy"], forwarded["approvalsReviewer"]),
            ("on-request", "guardian"),
        )
        self.assertEqual(forwarded["config"]["model_context_window"], 262144)
        self.assertEqual(
            forwarded["config"]["model_providers"]["deepseek"]["http_headers"],
            {"Authorization": "Bearer ephemeral-token"},
        )
        self.assertEqual(
            forwarded["collaborationMode"]["settings"]["model"],
            "/models/deepseek-v4-flash",
        )
        self.assertEqual((result["model"], incoming), ("deepseek::flash", original))

    async def test_null_resume_after_restart_uses_saved_alias_and_fresh_facade(self):
        started = await self.start_custom()
        restarted = self.make_router()
        await restarted.dispatch("initialize", {})
        result = await restarted.dispatch(
            "thread/resume",
            {"threadId": started["thread"]["id"], "model": None, "modelProvider": None},
        )
        self.assertEqual(result["model"], "deepseek::flash")
        self.assertEqual(
            restarted.created[-1].calls[-1][1]["config"]["model_providers"]["deepseek"][
                "base_url"
            ],
            "http://127.0.0.1:9/v1",
        )

    async def test_callback_round_trip_and_switch_guard(self):
        started = await self.start_custom()
        tid = started["thread"]["id"]
        child = self.router.state(tid).child
        translated = self.router.track_callback(child, 7)
        with self.assertRaisesRegex(RuntimeError, "pending"):
            await self.router.dispatch(
                "thread/settings/update", {"threadId": tid, "model": "gpt"}
            )
        await self.router.handle({"id": translated, "result": {"decision": "decline"}})
        self.assertEqual(child.sent, [{"id": 7, "result": {"decision": "decline"}}])
        self.assertEqual(self.router.resolve_callback(child, 7), translated)

    async def test_active_turn_switch_is_rejected_without_changing_selection(self):
        started = await self.start_custom()
        tid = started["thread"]["id"]
        await self.router.dispatch("turn/start", {"threadId": tid, "input": []})
        with self.assertRaisesRegex(RuntimeError, "active turn"):
            await self.router.dispatch(
                "thread/settings/update", {"threadId": tid, "model": "gpt"}
            )
        self.assertEqual(self.router.state(tid).model.alias, "deepseek::flash")

    async def test_frontend_eof_interrupts_owned_turn_before_closing_backend(self):
        started = await self.start_custom()
        tid = started["thread"]["id"]
        await self.router.dispatch("turn/start", {"threadId": tid, "input": []})
        child = self.router.state(tid).child
        with patch(
            "custom_models.router.sys.stdin", SimpleNamespace(buffer=io.BytesIO())
        ):
            await self.router.run()
        self.assertIn(
            ("turn/interrupt", {"threadId": tid, "turnId": "native-turn-id"}),
            child.calls,
        )
        self.assertTrue(child.closed)
        self.assertTrue(self.router.main.closed)

    async def test_custom_to_native_compacts_and_preserves_reviewer(self):
        started = await self.start_custom(
            approvalPolicy="on-request", approvalsReviewer="guardian"
        )
        tid = started["thread"]["id"]
        child = self.router.state(tid).child
        await self.router.dispatch(
            "thread/settings/update", {"threadId": tid, "model": "gpt"}
        )
        self.assertTrue(child.closed)
        self.assertIn(("thread/compact/start", {"threadId": tid}), child.calls)
        resumes = [
            params
            for method, params in self.router.main.calls
            if method == "thread/resume"
        ]
        self.assertEqual(resumes[-1]["approvalsReviewer"], "guardian")
        self.assertNotIn(
            "deepseek", resumes[-1].get("config", {}).get("model_providers", {})
        )

    async def test_external_translation_does_not_rewrite_tool_arguments(self):
        raw = {
            "model": "/models/deepseek-v4-flash",
            "modelProvider": "deepseek",
            "arguments": {"model": "/models/deepseek-v4-flash"},
            "collaborationMode": {"settings": {"model": "/models/deepseek-v4-flash"}},
        }
        actual = self.router.external(raw, self.registry.model())
        self.assertEqual(actual["arguments"], raw["arguments"])
        self.assertEqual(
            actual["collaborationMode"]["settings"]["model"], "deepseek::flash"
        )

    async def test_unloaded_thread_metadata_restores_alias_by_provider(self):
        actual = self.router.external(
            {
                "data": [
                    {"model": "/models/deepseek-v4-flash", "modelProvider": "deepseek"},
                    {"model": "/models/deepseek-v4-flash", "modelProvider": "openai"},
                ]
            },
            None,
        )
        self.assertEqual(
            [thread["model"] for thread in actual["data"]],
            ["deepseek::flash", "/models/deepseek-v4-flash"],
        )

    async def test_picker_aliases_have_one_explicit_default(self):
        result = await self.router.dispatch("model/list", {"includeHidden": True})
        self.assertEqual(
            [item["model"] for item in result["data"] if item["isDefault"]],
            ["deepseek::flash"],
        )

    async def test_model_list_pagination_does_not_drop_custom_entries(self):
        first = await self.router.dispatch("model/list", {"limit": 1})
        self.assertEqual(
            (len(first["data"]), first["nextCursor"]), (1, "custom-models:0")
        )
        second = await self.router.dispatch(
            "model/list", {"limit": 1, "cursor": first["nextCursor"]}
        )
        self.assertEqual(
            ([item["model"] for item in second["data"]], second["nextCursor"]),
            (["deepseek::flash"], None),
        )

    async def test_upstream_model_id_requires_unambiguous_explicit_provider(self):
        result = await self.router.dispatch(
            "thread/start",
            {"model": "/models/deepseek-v4-flash", "modelProvider": "deepseek"},
        )
        self.assertEqual(result["model"], "deepseek::flash")
        with self.assertRaises(ValueError):
            await self.router.dispatch(
                "thread/start", {"model": "unconfigured", "modelProvider": "deepseek"}
            )

    async def test_fork_has_independent_backend_owner_and_keeps_parent(self):
        parent = await self.start_custom(approvalsReviewer="guardian")
        parent_id = parent["thread"]["id"]
        parent_child = self.router.state(parent_id).child
        fork = await self.router.dispatch("thread/fork", {"threadId": parent_id})
        self.assertEqual(fork["model"], "deepseek::flash")
        self.assertIsNot(self.router.state("forked-thread").child, parent_child)
        await self.router.dispatch(
            "thread/settings/update", {"threadId": "forked-thread", "model": "gpt"}
        )
        self.assertFalse(parent_child.closed)
        self.assertEqual(self.router.state(parent_id).model.alias, "deepseek::flash")

    async def test_native_start_controls_survive_custom_switch(self):
        original = await self.router.dispatch(
            "thread/start",
            {
                "model": "gpt",
                "approvalsReviewer": "guardian",
                "permissions": "restricted",
            },
        )
        tid = original["thread"]["id"]
        await self.router.dispatch(
            "thread/settings/update", {"threadId": tid, "model": "deepseek::flash"}
        )
        resume = [
            params
            for method, params in self.router.state(tid).child.calls
            if method == "thread/resume"
        ][-1]
        self.assertEqual(
            (resume["approvalsReviewer"], resume["permissions"]),
            ("guardian", "restricted"),
        )

    async def test_latest_custom_sandbox_survives_provider_handoff(self):
        original = await self.router.dispatch(
            "thread/start", {"model": "gpt", "permissions": "original"}
        )
        tid = original["thread"]["id"]
        policy = {
            "type": "workspaceWrite",
            "writableRoots": [self.directory.name],
            "networkAccess": False,
            "excludeTmpdirEnvVar": True,
            "excludeSlashTmp": True,
        }
        self.router.observe(
            self.router.main,
            {
                "method": "thread/settings/updated",
                "params": {
                    "threadId": tid,
                    "threadSettings": {
                        "sandboxPolicy": policy,
                        "activePermissionProfile": None,
                    },
                },
            },
        )
        await self.router.dispatch(
            "thread/settings/update", {"threadId": tid, "model": "deepseek::flash"}
        )
        calls = self.router.state(tid).child.calls
        resume = next(params for method, params in calls if method == "thread/resume")
        self.assertNotIn("permissions", resume)
        self.assertIn(
            ("thread/settings/update", {"threadId": tid, "sandboxPolicy": policy}),
            calls,
        )
        self.assertEqual(self.router.state(tid).settings["sandboxPolicy"], policy)

    async def test_failed_switch_retains_prior_saved_selection(self):
        original = await self.router.dispatch("thread/start", {"model": "gpt"})
        tid = original["thread"]["id"]

        def failing_factory(router, label, model=None):
            child = FakeBackend(router, label, model)
            child.fail_resume = model is not None
            return child

        self.router.backend_factory = failing_factory
        with self.assertRaisesRegex(RuntimeError, "Deliberate"):
            await self.router.dispatch(
                "thread/settings/update", {"threadId": tid, "model": "deepseek::flash"}
            )
        self.assertIsNone(self.router.state(tid).model)
        self.assertNotIn(tid, self.registry.load_selections(self.directory.name))

    async def test_unmaterialized_native_thread_keeps_owner_on_failed_switch(self):
        original = await self.router.dispatch("thread/start", {"model": "gpt"})
        tid = original["thread"]["id"]
        self.router.saved[tid]["path"] = None
        with self.assertRaisesRegex(RuntimeError, "no saved history"):
            await self.router.dispatch(
                "thread/settings/update", {"threadId": tid, "model": "deepseek::flash"}
            )
        self.assertFalse(
            any(method == "thread/archive" for method, _ in self.router.main.calls)
        )
        self.assertIsNone(self.router.state(tid).child)

    async def test_resume_reads_latest_cli_selection(self):
        tid = "cli-selected-thread"
        self.registry.save_selection(self.directory.name, tid, "deepseek::flash")
        result = await self.router.dispatch(
            "thread/resume", {"threadId": tid, "model": None}
        )
        self.assertEqual(result["model"], "deepseek::flash")

    async def test_generated_facade_overrides_follow_user_config_but_precede_delimiter(
        self,
    ):
        self.router.arguments = [
            "app-server",
            "-c",
            'model_providers.deepseek.base_url="https://wrong.invalid"',
            "--",
            "literal-tail",
        ]
        backend = Backend(self.router, "argv-test", self.registry.model())

        class EmptyProcess:
            stdout = ()

        with patch(
            "custom_models.router.subprocess.Popen", return_value=EmptyProcess()
        ) as launch:
            backend.call = AsyncMock(return_value={})
            backend.send = AsyncMock()
            await backend.start()
            await backend.reader
        arguments = launch.call_args.args[0]
        actual = [
            argument
            for argument in arguments[: arguments.index("--")]
            if argument.startswith("model_providers.deepseek=")
        ]
        import tomllib

        table = tomllib.loads(actual[-1])["model_providers"]["deepseek"]
        self.assertEqual(table["base_url"], "http://127.0.0.1:9/v1")
        self.assertNotIn("env_key", table)
        self.assertEqual(arguments[arguments.index("--") :], ["--", "literal-tail"])

    async def test_catalog_is_regenerated_when_existing_metadata_is_stale(self):
        path = Path(self.directory.name) / "catalogs" / "deepseek-flash.json"
        path.parent.mkdir()
        path.write_text('{"models":[]}')
        self.router.overrides(self.registry.model())
        self.assertIn('"context_window": 262144', path.read_text())


if __name__ == "__main__":
    unittest.main()
