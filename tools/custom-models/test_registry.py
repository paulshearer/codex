import copy
import json
from pathlib import Path
import tempfile
import unittest

from custom_models.registry import Registry


def sample_registry():
    return {
        "version": 1,
        "default_model": "deepseek::flash",
        "providers": {
            "deepseek": {
                "name": "DeepSeek",
                "api_format": "responses",
                "base_url": "http://localhost:8000/v1",
                "env_key": "DEEPSEEK_API_KEY",
            }
        },
        "models": [
            {
                "id": "flash",
                "provider": "deepseek",
                "upstream_model": "/models/deepseek-v4-flash",
                "display_name": "DeepSeek 4.1 Flash",
                "context_window": 262144,
                "max_output_tokens": 16384,
                "auto_compact_token_limit": 196608,
                "reasoning_efforts": ["medium"],
                "default_reasoning_effort": "medium",
                "supports_images": True,
            }
        ],
    }


class RegistryTests(unittest.TestCase):
    def test_aliases_distinguish_identical_upstream_models(self):
        data = sample_registry()
        data["providers"]["other"] = {
            "name": "Other",
            "base_url": "https://example.test/v1",
            "api_format": "chat_completions",
        }
        data["models"].append(
            {**data["models"][0], "provider": "other", "supports_images": False}
        )
        registry = Registry.from_dict(data)
        self.assertEqual(
            [model.alias for model in registry.models],
            ["deepseek::flash", "other::flash"],
        )
        self.assertEqual(
            registry.model("other::flash").upstream_model,
            registry.model().upstream_model,
        )

    def test_provider_rejects_ambiguous_upstream_identity(self):
        data = sample_registry()
        data["models"].append({**data["models"][0], "id": "another-alias"})
        with self.assertRaisesRegex(ValueError, "unique upstream"):
            Registry.from_dict(data)

    def test_native_loopback_configuration_does_not_contain_upstream_credentials(self):
        data = sample_registry()
        data["providers"]["deepseek"].update(
            http_headers={"Authorization": "private-upstream-key"},
            env_http_headers={"X-Upstream-Secret": "PRIVATE_HEADER"},
            query_params={"key": "private-query"},
        )
        registry = Registry.from_dict(data)
        actual = registry.native_config(
            registry.model(), "http://127.0.0.1:9/v1", "private-loopback-token"
        )
        encoded = json.dumps(actual)
        self.assertNotIn("private-upstream-key", encoded)
        self.assertNotIn("private-query", encoded)
        self.assertNotIn("DEEPSEEK_API_KEY", encoded)
        self.assertEqual(
            actual["model_providers.deepseek"]["http_headers"]["Authorization"],
            "Bearer private-loopback-token",
        )
        self.assertFalse(any("approval" in key or "sandbox" in key for key in actual))

    def test_limits_and_capabilities_enter_native_catalog(self):
        registry = Registry.from_dict(sample_registry())
        metadata = registry.native_catalog(
            registry.model(), "unchanged harness instructions"
        )["models"][0]
        self.assertEqual(
            {
                key: metadata[key]
                for key in (
                    "slug",
                    "context_window",
                    "max_context_window",
                    "auto_compact_token_limit",
                    "input_modalities",
                )
            },
            {
                "slug": "/models/deepseek-v4-flash",
                "context_window": 262144,
                "max_context_window": 262144,
                "auto_compact_token_limit": 196608,
                "input_modalities": ["text", "image"],
            },
        )
        self.assertEqual(
            metadata["model_messages"],
            {"instructions_template": "unchanged harness instructions"},
        )

    def test_tool_search_is_an_explicit_native_capability(self):
        data = sample_registry()
        registry = Registry.from_dict(data)
        self.assertFalse(
            registry.native_catalog(registry.model(), "instructions")["models"][0][
                "supports_search_tool"
            ]
        )
        data["models"][0]["supports_tool_search"] = True
        registry = Registry.from_dict(data)
        self.assertTrue(
            registry.native_catalog(registry.model(), "instructions")["models"][0][
                "supports_search_tool"
            ]
        )
        data["models"][0]["supports_tool_search"] = "true"
        with self.assertRaisesRegex(ValueError, "boolean"):
            Registry.from_dict(data)

    def test_invalid_configuration_is_rejected_before_network_use(self):
        changes = [
            lambda data: data["providers"]["deepseek"].update(
                base_url="https://user:secret@example.test/v1"
            ),
            lambda data: data["models"][0].update(auto_compact_token_limit=262143),
            lambda data: data["models"][0].update(default_reasoning_effort="high"),
            lambda data: data["models"].append(copy.deepcopy(data["models"][0])),
            lambda data: data.update(default_model="absent::model"),
            lambda data: data["providers"]["deepseek"].update(
                auth={"command": "helper"}
            ),
            lambda data: data["providers"]["deepseek"].update(
                auth={"type": "bearer"}, env_key=None
            ),
        ]
        for change in changes:
            with self.subTest(change=change):
                data = sample_registry()
                change(data)
                with self.assertRaises(ValueError):
                    Registry.from_dict(data)

    def test_duplicate_json_keys_are_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text('{"version":1,"version":1}')
            with self.assertRaisesRegex(ValueError, "duplicate"):
                Registry.load(path)

    def test_command_auth_matches_native_schema(self):
        data = sample_registry()
        del data["providers"]["deepseek"]["env_key"]
        data["providers"]["deepseek"]["auth"] = {
            "command": "token-helper",
            "args": ["--audience", "custom"],
            "timeout_ms": 2000,
            "refresh_interval_ms": 0,
        }
        provider = Registry.from_dict(data).provider("deepseek")
        self.assertEqual(provider.auth, data["providers"]["deepseek"]["auth"])
        with tempfile.TemporaryDirectory() as directory:
            data["providers"]["deepseek"]["auth"]["cwd"] = directory
            self.assertEqual(
                Registry.from_dict(data).provider("deepseek").auth["cwd"], directory
            )
        data["providers"]["deepseek"]["auth"]["cwd"] = "relative-folder"
        with self.assertRaisesRegex(ValueError, "absolute"):
            Registry.from_dict(data)

    def test_text_only_chat_metadata_cannot_advertise_images(self):
        data = sample_registry()
        data["providers"]["deepseek"]["api_format"] = "chat_completions"
        with self.assertRaisesRegex(ValueError, "text only"):
            Registry.from_dict(data)
        data["models"][0]["supports_images"] = False
        registry = Registry.from_dict(data)
        self.assertEqual(
            registry.native_config(registry.model(), "http://127.0.0.1:9/v1")[
                "web_search"
            ],
            "disabled",
        )

    def test_selection_save_preserves_other_threads_and_rejects_unknown_alias(self):
        registry = Registry.from_dict(sample_registry())
        with tempfile.TemporaryDirectory() as directory:
            registry.save_selection(directory, "one", "deepseek::flash")
            registry.save_selection(directory, "two", "deepseek::flash")
            with self.assertRaises(ValueError):
                registry.save_selection(directory, "one", "missing::model")
            self.assertEqual(
                registry.load_selections(directory),
                {"one": "deepseek::flash", "two": "deepseek::flash"},
            )
            registry.save_selection(directory, "one")
            self.assertEqual(
                registry.load_selections(directory), {"two": "deepseek::flash"}
            )


if __name__ == "__main__":
    unittest.main()
