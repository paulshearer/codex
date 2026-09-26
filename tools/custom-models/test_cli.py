import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from custom_models.__main__ import (
    cli_resume,
    cli_selection,
    configure_catalog,
    main,
    native_arguments,
    native_command,
    parse_options,
    resume_request,
    saved_cli_model,
    select_model,
    toml_value,
)
from custom_models.registry import Registry


def registry():
    return Registry.from_dict(
        {
            "version": 1,
            "default_model": "alpha::same",
            "providers": {
                key: {"base_url": "http://127.0.0.1:8000/v1"}
                for key in ("alpha", "beta")
            },
            "models": [
                {
                    "id": "same",
                    "provider": key,
                    "upstream_model": "same",
                    "context_window": 16000,
                    "max_output_tokens": 2000,
                    "auto_compact_token_limit": 12000,
                }
                for key in ("alpha", "beta")
            ],
        }
    )


class CliTests(unittest.TestCase):
    def test_alias_and_native_flags(self):
        reg = registry()
        model, args = select_model(["exec", "--model=beta::same", "hello"], reg)
        self.assertEqual(
            (model.alias, args), ("beta::same", ["exec", "--model=same", "hello"])
        )
        self.assertEqual(
            select_model(["exec", "-m", "gpt-example"], reg),
            (None, ["exec", "-m", "gpt-example"]),
        )
        with self.assertRaises(ValueError):
            select_model(["exec", "-m", "unknown::same"], reg)
        self.assertEqual(
            select_model(["exec", "--", "--model=literal"], reg)[1],
            ["exec", "--", "--model=literal"],
        )
        self.assertEqual(
            select_model(["exec", "-m", "same"], reg, requested="beta::same")[0].alias,
            "beta::same",
        )

    def test_companion_options_and_native_delimiter(self):
        options, args = parse_options(
            [
                "--registry",
                "path with spaces.json",
                "--custom-model",
                "beta::same",
                "--",
                "exec",
                "--",
                "-literal prompt",
            ]
        )
        self.assertEqual(
            (options.registry, options.custom_model),
            ("path with spaces.json", "beta::same"),
        )
        self.assertEqual(args, ["exec", "--", "-literal prompt"])
        self.assertEqual(
            native_arguments(args, ["-c", "model_provider=beta"]),
            ["exec", "-c", "model_provider=beta", "--", "-literal prompt"],
        )
        self.assertEqual(
            parse_options(["exec", "--", "resume"])[1], ["exec", "--", "resume"]
        )

    def test_toml_quoted_provider_and_headers(self):
        import tomllib

        data = {
            "custom.id": {
                "http_headers": {
                    "Authorization": "Bearer temporary",
                    "X-Test": "line\\tab",
                }
            },
            "flag": False,
            "array": [1, "two"],
        }
        self.assertEqual(tomllib.loads("value=" + toml_value(data))["value"], data)

    def test_default_selection_and_catalog_update(self):
        with tempfile.TemporaryDirectory() as directory:
            home, reg = Path(directory), registry()
            self.assertIsNone(cli_selection(home, reg))
            cli_selection(home, reg, reg.model("beta::same"))
            self.assertEqual(cli_selection(home, reg), "beta::same")
            catalog = configure_catalog(reg, reg.model(), home)
            catalog.write_text("{}", encoding="utf-8")
            configure_catalog(reg, reg.model(), home)
            self.assertEqual(
                json.loads(catalog.read_text(encoding="utf-8"))["models"][0]["slug"],
                "same",
            )

    def test_resume_reloads_provider_with_duplicate_upstream_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            home, reg = Path(directory), registry()
            sessions = home / "sessions"
            sessions.mkdir()
            path = sessions / "rollout-thread-123.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {"type": "session_meta", "payload": {"model_provider": "beta"}},
                        {"type": "turn_context", "payload": {"model": "same"}},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                saved_cli_model(
                    ["exec", "resume", "-c", "x=1", "thread-123", "hello"], home, reg
                ),
                "beta::same",
            )
            cli_selection(home, reg, reg.model("beta::same"))
            self.assertEqual(
                saved_cli_model(["resume", "--last"], home, reg), "beta::same"
            )
            self.assertIsNone(saved_cli_model(["resume", "missing"], home, reg))

    def rollout(
        self, home, tid, provider="alpha", cwd=None, source="cli", settings=None
    ):
        directory = home / "sessions"
        directory.mkdir(exist_ok=True)
        path = directory / ("rollout-" + tid + ".jsonl")
        rows = [
            {
                "type": "session_meta",
                "payload": {
                    "id": tid,
                    "model_provider": provider,
                    "cwd": str(cwd or home),
                    "source": source,
                },
            },
            {
                "type": "turn_context",
                "payload": {"model": "same", "cwd": str(cwd or home)},
            },
        ]
        if settings:
            rows.append(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": settings,
                    },
                }
            )
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
        return path

    def test_commands_do_not_treat_option_values_or_prompt_resume_as_commands(self):
        self.assertEqual(
            native_command(["-c", "setting='app-server'", "app-server"]),
            ("app-server", 2),
        )
        self.assertIsNone(resume_request(["exec", "--", "resume"]))
        self.assertIsNone(resume_request(["exec", "-c", "resume", "A prompt"]))
        self.assertIsNone(resume_request(["A prompt", "resume"]))
        self.assertEqual(
            resume_request(["-c", "x=1", "exec", "--json", "resume", "id", "continue"])[
                "target"
            ],
            "id",
        )
        self.assertFalse(
            resume_request(["exec", "resume", "id", "--", "--last"])["last"]
        )
        self.assertFalse(
            resume_request(["exec", "resume", "-c", "--last", "id"])["last"]
        )

    def test_latest_native_settings_override_original_provider_and_stale_desktop_selection(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            home, reg = Path(directory), registry()
            path = self.rollout(
                home,
                "task",
                settings={
                    "model_provider_id": "beta",
                    "model": "same",
                    "cwd": str(home),
                },
            )
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "thread_settings_applied",
                                "thread_id": "copied-parent",
                                "thread_settings": {
                                    "model_provider_id": "alpha",
                                    "model": "same",
                                    "cwd": str(home),
                                },
                            },
                        }
                    )
                    + "\n"
                )
            reg.save_selection(home, "task", "alpha::same")
            self.assertEqual(
                cli_resume(["exec", "resume", "task", "continue"], home, reg)[0],
                "beta::same",
            )
            self.assertEqual(
                cli_resume(
                    ["exec", "resume", "task"], home, reg, explicit="alpha::same"
                )[1],
                "task",
            )

    def test_last_filters_provider_cwd_and_interactive_sources_and_rewrites_exact_id(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            home, reg = Path(directory), registry()
            paths = [
                self.rollout(home, "target", cwd=home),
                self.rollout(home, "other-provider", provider="beta", cwd=home),
                self.rollout(home, "other-cwd", cwd=home / "elsewhere"),
                self.rollout(home, "exec-task", cwd=home, source="exec"),
            ]
            for index, path in enumerate(paths):
                os.utime(path, ns=(1_000_000_000 * (index + 1),) * 2)
            alias, tid, args = cli_resume(
                ["-C", str(home), "resume", "--last", "continue"], home, reg
            )
            self.assertEqual(
                (alias, tid, args),
                (
                    "alpha::same",
                    "target",
                    ["-C", str(home), "resume", "target", "continue"],
                ),
            )
            self.assertEqual(
                cli_resume(["exec", "-C", str(home), "resume", "--last"], home, reg)[1],
                "exec-task",
            )
            self.assertEqual(
                cli_resume(["-C", str(home), "resume", "--last", "--all"], home, reg)[
                    1
                ],
                "other-cwd",
            )

    def test_bare_multi_provider_picker_requires_explicit_model_and_ids_are_not_substrings(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            home, reg = Path(directory), registry()
            self.rollout(home, "task-long")
            with self.assertRaisesRegex(ValueError, "THREAD_ID"):
                cli_resume(["resume"], home, reg)
            self.assertEqual(
                cli_resume(["resume"], home, reg, explicit="beta::same"),
                (None, None, ["resume"]),
            )
            with self.assertRaisesRegex(ValueError, "could not be resolved"):
                cli_resume(["resume", "task"], home, reg)

    def test_readonly_native_projection_orders_last_and_preserves_builtin_model(self):
        with tempfile.TemporaryDirectory() as directory:
            home, reg = Path(directory), registry()
            first = self.rollout(home, "native-recent", cwd=home)
            self.rollout(home, "filesystem-recent", cwd=home)
            with contextlib.closing(
                sqlite3.connect(home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "CREATE TABLE threads(id TEXT,rollout_path TEXT,updated_at INTEGER,updated_at_ms INTEGER,model_provider TEXT,model TEXT,cwd TEXT,archived INTEGER,name TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "native-recent",
                        str(first),
                        10,
                        10001,
                        "alpha",
                        "same",
                        str(home),
                        0,
                        "named",
                    ),
                )
                connection.commit()
            self.assertEqual(
                cli_resume(["exec", "-C", str(home), "resume", "--last"], home, reg)[1],
                "native-recent",
            )
            self.assertEqual(
                cli_resume(["resume", "named"], home, reg)[1], "native-recent"
            )
            self.rollout(
                home,
                "builtin",
                provider="openai",
                settings={
                    "model_provider_id": "openai",
                    "model": "gpt-example",
                    "cwd": str(home),
                },
            )
            saved = cli_resume(["resume", "builtin"], home, reg)[0]
            self.assertEqual(saved, "gpt-example")
            self.assertEqual(
                select_model(["resume", "builtin"], reg, saved=saved),
                (None, ["resume", "builtin"]),
            )

    def test_task_selection_changes_only_after_successful_uncancelled_resume(self):
        for outcome in ("success", "failure", "cancelled"):
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as directory,
            ):
                home, reg = Path(directory), registry()
                self.rollout(home, "task")
                reg.save_selection(home, "task", "alpha::same")
                native = home / "native.exe"
                native.touch()
                child = MagicMock()
                child.wait.side_effect = (
                    [KeyboardInterrupt(), 0]
                    if outcome == "cancelled"
                    else [0 if outcome == "success" else 1]
                )
                child.poll.return_value = 0 if outcome != "failure" else 1
                facade = MagicMock(base_url="http://127.0.0.1:9/v1", token="ephemeral")
                with (
                    patch("custom_models.__main__.Registry.load", return_value=reg),
                    patch("custom_models.facade.Facade", return_value=facade),
                    patch(
                        "custom_models.__main__.subprocess.Popen", return_value=child
                    ),
                ):
                    code = main(
                        [
                            "--registry",
                            str(home / "registry.json"),
                            "--native",
                            str(native),
                            "--home",
                            str(home),
                            "--custom-model",
                            "beta::same",
                            "exec",
                            "resume",
                            "task",
                            "continue",
                        ]
                    )
                self.assertEqual(code, 1 if outcome == "failure" else 0)
                self.assertEqual(
                    reg.load_selections(home)["task"],
                    "beta::same" if outcome == "success" else "alpha::same",
                )
                facade.stop.assert_called_once()

    def test_child_stops_before_facade_if_postlaunch_selection_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            home, reg = Path(directory), registry()
            native = home / "native.exe"
            native.touch()
            events = []
            child = MagicMock()
            child.poll.return_value = None
            child.terminate.side_effect = lambda: events.append("terminate")
            child.wait.side_effect = lambda **kwargs: events.append("wait") or 0
            facade = MagicMock(base_url="http://127.0.0.1:9/v1", token="ephemeral")
            facade.stop.side_effect = lambda: events.append("stop")
            original = cli_selection

            def selection(home, registry, model=None):
                if model is not None:
                    raise OSError("Selection store unavailable")
                return original(home, registry)

            with (
                patch("custom_models.__main__.Registry.load", return_value=reg),
                patch("custom_models.facade.Facade", return_value=facade),
                patch("custom_models.__main__.subprocess.Popen", return_value=child),
                patch("custom_models.__main__.cli_selection", side_effect=selection),
            ):
                with self.assertRaisesRegex(OSError, "Selection store unavailable"):
                    main(
                        [
                            "--registry",
                            str(home / "registry.json"),
                            "--native",
                            str(native),
                            "--home",
                            str(home),
                            "exec",
                            "hello",
                        ]
                    )
            self.assertEqual(events, ["terminate", "wait", "stop"])


if __name__ == "__main__":
    unittest.main()
