import json
import unittest

from custom_models.chat import CompatibilityError, ToolName, restore_tool_names
from custom_models.custom_functions import wrap_custom_tools


class CustomFunctionTests(unittest.TestCase):
    def test_custom_history_and_images_round_trip_without_mutating_native_input(self):
        native = {
            "tools": [
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "description": "Edit files",
                    "format": {"type": "grammar"},
                }
            ],
            "input": [
                {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "patch-1",
                    "input": "*** Begin Patch\n*** End Patch",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "patch-1",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,example",
                        }
                    ],
                },
            ],
        }
        before = json.dumps(native)
        payload, names = wrap_custom_tools(
            native, {"apply_patch": ToolName("", "apply_patch", custom=True)}
        )
        self.assertEqual(json.dumps(native), before)
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertTrue(payload["tools"][0]["strict"])
        self.assertFalse(payload["tools"][0]["parameters"]["additionalProperties"])
        self.assertIn('{"input":"<complete raw tool input>"}', payload["tools"][0]["description"])
        self.assertEqual(
            payload["input"][1], {**native["input"][1], "type": "function_call_output"}
        )
        self.assertEqual(
            restore_tool_names(payload["input"][0], names), native["input"][0]
        )

    def test_wrapped_call_preserves_namespace_and_real_newlines(self):
        _, names = wrap_custom_tools(
            {}, {"functions__exec": ToolName("functions", "exec", custom=True)}
        )
        call = {
            "id": "one",
            "type": "function_call",
            "name": "functions__exec",
            "call_id": "call-one",
            "status": "completed",
            "arguments": json.dumps({"input": "text(1);\ntext(2);"}),
        }
        self.assertEqual(
            restore_tool_names(call, names),
            {
                "id": "one",
                "type": "custom_tool_call",
                "name": "exec",
                "namespace": "functions",
                "call_id": "call-one",
                "status": "completed",
                "input": "text(1);\ntext(2);",
            },
        )

    def test_malformed_wrappers_cannot_be_dispatched(self):
        _, names = wrap_custom_tools({}, {"exec": ToolName("", "exec", custom=True)})
        for arguments in (
            '{"input":1}',
            '{"input":"code","extra":true}',
            '{"input":"first","input":"second"}',
            '{"code":"text(1)"}',
            '{}',
            'null',
            "[]",
            "invalid",
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(CompatibilityError),
            ):
                restore_tool_names(
                    {
                        "type": "function_call",
                        "name": "exec",
                        "call_id": "one",
                        "arguments": arguments,
                    },
                    names,
                )

    def test_json_string_custom_input_is_lossless(self):
        _, names = wrap_custom_tools(
            {}, {"functions__exec": ToolName("functions", "exec", custom=True)}
        )
        for code in ('text("hello");\ntext(2);', '', '{"input":"nested literal"}'):
            with self.subTest(code=code):
                call = {"type": "function_call", "name": "functions__exec", "call_id": "one",
                        "arguments": json.dumps(code)}
                before = json.dumps(call)
                restored = restore_tool_names(call, names)
                self.assertEqual(restored["type"], "custom_tool_call")
                self.assertEqual(restored["input"], code)
                self.assertEqual(restored["name"], "exec")
                self.assertEqual(restored["namespace"], "functions")
                self.assertEqual(json.dumps(call), before)

    def test_custom_input_errors_identify_tool_without_echoing_arguments(self):
        _, names = wrap_custom_tools(
            {}, {"functions__exec": ToolName("functions", "exec", custom=True)}
        )
        with self.assertRaises(CompatibilityError) as caught:
            restore_tool_names({"type": "function_call", "name": "functions__exec", "call_id": "one",
                                "arguments": '{"code":"private-content"}'}, names)
        self.assertIn("functions.exec", str(caught.exception))
        self.assertIn('{"input":"<raw tool input>"}', str(caught.exception))
        self.assertNotIn("private-content", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
