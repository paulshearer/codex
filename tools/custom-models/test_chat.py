import json
import unittest

from custom_models.chat import CompatibilityError, ProviderError, ChatStream
from custom_models.chat import (
    chat_to_response,
    flatten_responses,
    responses_to_chat,
    restore_tool_names,
)
from custom_models.registry import Model


MODEL = Model(
    "flash",
    "deepseek",
    "/models/flash",
    "Flash",
    128000,
    16000,
    96000,
    ("medium",),
    "medium",
    True,
)
TOOLS = [
    {
        "type": "namespace",
        "name": "functions",
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                },
            },
            {"type": "custom", "name": "apply_patch", "description": "Edit files"},
        ],
    },
    {
        "type": "namespace",
        "name": "mcp__calendar",
        "tools": [
            {"type": "function", "name": "list", "parameters": {"type": "object"}}
        ],
    },
]


class ChatTests(unittest.TestCase):
    def test_output_budgets_are_checked_before_accumulation(self):
        for field, limit in (
            ("reasoning_content", 2 * 1024 * 1024),
            ("content", 8 * 1024 * 1024),
        ):
            stream = ChatStream(MODEL, {})
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(CompatibilityError, "size limit"),
            ):
                stream.feed(
                    {"choices": [{"index": 0, "delta": {field: "x" * (limit + 1)}}]}
                )
            self.assertEqual(stream.reasoning, "")
            self.assertEqual(stream.text, "")

    def test_request_keeps_parallel_history_and_custom_input(self):
        patch = "*** Begin Patch\n*** Add File: file.py\n+print('hi')\n*** End Patch"
        body = {
            "instructions": "Be concise",
            "stream": True,
            "tools": TOOLS,
            "reasoning": {"effort": "medium"},
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect"}],
                },
                {
                    "type": "function_call",
                    "namespace": "functions",
                    "name": "exec_command",
                    "call_id": "c1",
                    "arguments": '{"cmd":"pwd"}',
                },
                {
                    "type": "custom_tool_call",
                    "namespace": "functions",
                    "name": "apply_patch",
                    "call_id": "c2",
                    "input": patch,
                },
                {"type": "custom_tool_call_output", "call_id": "c2", "output": "Done"},
            ],
        }
        converted, names = responses_to_chat(body, MODEL)
        self.assertEqual(converted["model"], MODEL.upstream_model)
        self.assertEqual(converted["messages"][1]["content"], "inspect")
        self.assertEqual(
            converted["messages"][2]["tool_calls"][1]["function"],
            {
                "name": "functions__apply_patch",
                "arguments": json.dumps({"input": patch}),
            },
        )
        self.assertEqual(
            converted["messages"][3],
            {"role": "tool", "tool_call_id": "c2", "content": "Done"},
        )
        self.assertTrue(names["functions__apply_patch"].custom)
        self.assertEqual(body["input"][2]["type"], "custom_tool_call")

    def test_namespace_collisions_and_long_names_restore(self):
        collision = {
            "tools": [
                {"type": "function", "name": "a__b"},
                {
                    "type": "namespace",
                    "name": "a",
                    "tools": [{"type": "function", "name": "b"}],
                },
            ],
            "input": [
                {
                    "type": "function_call",
                    "namespace": "a",
                    "name": "b",
                    "call_id": "c",
                    "arguments": "{}",
                }
            ],
        }
        flat, aliases = flatten_responses(collision)
        self.assertEqual(len(set(tool["name"] for tool in flat["tools"])), 2)
        self.assertEqual(restore_tool_names(flat["input"], aliases), collision["input"])
        _, names = flatten_responses(
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__" + "x" * 70,
                        "tools": [{"type": "function", "name": "read"}],
                    }
                ]
            }
        )
        wire = next(iter(names))
        restored = restore_tool_names({"type": "function_call", "name": wire}, names)
        self.assertEqual(
            restored,
            {"type": "function_call", "name": "read", "namespace": "mcp__" + "x" * 70},
        )

    def test_split_tool_arguments_wait_for_terminal_and_restore_custom_patch(self):
        _, names = responses_to_chat({"tools": TOOLS}, MODEL)
        stream = ChatStream(MODEL, names)
        earlier = stream.feed(
            {
                "id": "chat-1",
                "choices": [
                    {
                        "delta": {
                            "content": "Hello ",
                            "reasoning_content": "Inspect files",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call1",
                                    "function": {
                                        "name": "functions__exec_command",
                                        "arguments": '{"cmd":',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call2",
                                    "function": {
                                        "name": "functions__apply_patch",
                                        "arguments": '{"input":"*** Begin Patch\\n',
                                    },
                                },
                            ],
                        }
                    }
                ],
            }
        )
        earlier += stream.feed(
            {
                "choices": [
                    {
                        "delta": {
                            "content": "世界",
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"pwd"}'}},
                                {
                                    "index": 1,
                                    "function": {"arguments": '*** End Patch"}'},
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        self.assertFalse(
            any(event["type"] == "response.output_item.done" for event in earlier)
        )
        terminal = stream.finish()
        calls = [
            event["item"]
            for event in terminal
            if event["type"] == "response.output_item.done"
            and event["item"]["type"] in {"function_call", "custom_tool_call"}
        ]
        self.assertEqual(calls[0]["namespace"], "functions")
        self.assertEqual(calls[1]["input"], "*** Begin Patch\n*** End Patch")
        self.assertEqual(calls[1]["type"], "custom_tool_call")
        self.assertEqual(
            stream.response["output"][0]["content"][0]["text"], "Hello 世界"
        )
        repeated, _ = responses_to_chat(
            {"tools": TOOLS, "input": stream.response["output"]}, MODEL
        )
        self.assertEqual(repeated["messages"][-1]["reasoning_content"], "Inspect files")
        self.assertEqual(terminal[-1]["type"], "response.completed")
        self.assertEqual(
            [
                e["output_index"]
                for e in terminal
                if e["type"] == "response.output_item.done"
            ],
            list(range(len(stream.response["output"]))),
        )

    def test_truncated_invalid_or_incomplete_calls_never_complete(self):
        _, names = responses_to_chat({"tools": TOOLS}, MODEL)
        stream = ChatStream(MODEL, names)
        stream.feed(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "function": {
                                        "name": "functions__exec_command",
                                        "arguments": '{"cmd":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        with self.assertRaisesRegex(CompatibilityError, "finish reason"):
            stream.finish()
        stream.feed({"choices": [{"delta": {}, "finish_reason": "length"}]})
        self.assertFalse(
            any(
                e.get("item", {}).get("type") == "function_call"
                for e in stream.finish()
            )
        )
        broken = ChatStream(MODEL, names)
        broken.feed(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "function": {
                                        "name": "functions__exec_command",
                                        "arguments": "{",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        with self.assertRaisesRegex(CompatibilityError, "invalid JSON"):
            broken.finish()

    def test_provider_errors_and_refusals_remain_failures(self):
        stream = ChatStream(MODEL, {})
        error = {"code": "cyber_policy", "message": "Supplier policy decision"}
        with self.assertRaises(ProviderError) as caught:
            stream.feed({"error": error})
        self.assertEqual(caught.exception.error, error)
        with self.assertRaises(ProviderError) as caught:
            chat_to_response(
                {
                    "choices": [
                        {
                            "message": {"refusal": "Cannot provide this"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                MODEL,
                {},
            )
        self.assertEqual(caught.exception.error["message"], "Cannot provide this")

    def test_nonstream_reasoning_usage_and_json_contract(self):
        body, names = responses_to_chat(
            {
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "schema": {"type": "object"},
                    }
                }
            },
            MODEL,
        )
        self.assertEqual(body["response_format"]["json_schema"]["name"], "answer")
        result = chat_to_response(
            {
                "id": "chat1",
                "choices": [
                    {
                        "message": {"content": "done", "reasoning_content": "thought"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
            },
            MODEL,
            names,
        )
        self.assertEqual(result["usage"]["total_tokens"], 6)
        self.assertEqual(
            result["usage"]["output_tokens_details"]["reasoning_tokens"], 1
        )
        with self.assertRaises(CompatibilityError):
            responses_to_chat(
                {"input": [{"role": "user", "content": [{"type": "input_file"}]}]},
                MODEL,
            )
        with self.assertRaises(CompatibilityError):
            responses_to_chat(
                {
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64,YQ==",
                                }
                            ],
                        }
                    ]
                },
                MODEL,
            )

    def test_client_tool_discovery_roundtrip_keeps_deferred_mcp_tools(self):
        search = {
            "type": "tool_search",
            "execution": "client",
            "description": "Find tools",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }
        chat, names = responses_to_chat({"tools": [search]}, MODEL)
        self.assertEqual(chat["tools"][0]["function"]["name"], "tool_search")
        response = chat_to_response(
            {
                "id": "s",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "search1",
                                    "function": {
                                        "name": "tool_search",
                                        "arguments": '{"query":"calendar"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            MODEL,
            names,
        )
        call = next(
            item for item in response["output"] if item["type"] == "tool_search_call"
        )
        self.assertEqual(call["arguments"], {"query": "calendar"})
        self.assertEqual(call["execution"], "client")
        history = [
            call,
            {
                "type": "tool_search_output",
                "call_id": "search1",
                "execution": "client",
                "status": "completed",
                "tools": [TOOLS[1]],
            },
        ]
        translated, aliases = responses_to_chat(
            {"tools": [search], "input": history}, MODEL
        )
        self.assertIn("mcp__calendar__list", aliases)
        self.assertEqual(
            translated["messages"][0]["tool_calls"][0]["function"]["arguments"],
            '{"query": "calendar"}',
        )
        self.assertEqual(translated["messages"][1]["tool_call_id"], "search1")
        self.assertIn("mcp__calendar__list", translated["messages"][1]["content"])
        with self.assertRaisesRegex(CompatibilityError, "client-executed"):
            responses_to_chat({"tools": [{**search, "execution": "server"}]}, MODEL)


if __name__ == "__main__":
    unittest.main()
