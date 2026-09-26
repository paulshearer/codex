import contextlib
import dataclasses
import http.client
import http.server
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from custom_models.facade import Facade
from custom_models.registry import Model, Provider


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
    False,
)
TOOLS = [
    {
        "type": "namespace",
        "name": "mcp__calendar",
        "tools": [
            {"type": "function", "name": "list", "parameters": {"type": "object"}}
        ],
    }
]


def sse(event):
    return (
        "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
    ).encode()


def decoded_events(raw):
    return [
        json.loads(line[6:])
        for line in raw.decode().splitlines()
        if line.startswith("data: ") and line[6:] != "[DONE]"
    ]


class MockProvider:
    def __init__(self, callback):
        self.records = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                record = {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(raw),
                }
                owner.records.append(record)
                status, content_type, chunks = callback(record)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                try:
                    for delay, data in chunks:
                        if delay:
                            time.sleep(delay)
                        self.wfile.write(data)
                        self.wfile.flush()
                except OSError:
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def complete_response(text="done"):
    return {
        "id": "resp1",
        "object": "response",
        "status": "completed",
        "model": MODEL.upstream_model,
        "error": None,
        "output": [
            {
                "id": "msg1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


class FacadeTests(unittest.TestCase):
    @contextlib.contextmanager
    def facade(self, callback, api="responses", resolver=None, **provider_options):
        upstream = MockProvider(callback)
        with tempfile.TemporaryDirectory() as home:
            provider = Provider(
                "deepseek", "DeepSeek", api, upstream.url, **provider_options
            )
            facade = Facade(provider, [MODEL], home, resolver=resolver)
            facade.start()
            try:
                yield facade, upstream, Path(home)
            finally:
                facade.stop()
                upstream.stop()

    def post(self, facade, body=None, token=True, headers=None):
        body = body or {
            "model": MODEL.upstream_model,
            "input": "hello",
            "stream": False,
        }
        request = Request(
            facade.base_url + "/responses",
            json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": "Bearer " + facade.token} if token else {}),
                **(headers or {}),
            },
        )
        with urlopen(request, timeout=3) as response:
            return response.read()

    def test_loopback_auth_and_facade_owned_upstream_credentials(self):
        callback = lambda _: (
            200,
            "application/json",
            [(0, json.dumps(complete_response()).encode())],
        )
        with patch.dict(
            "os.environ",
            {
                "CUSTOM_SUPPLIER_TOKEN": "supplier-only-key",
                "CUSTOM_HEADER": "supplier-header",
                "OPENAI_API_KEY": "original-openai-key",
            },
        ):
            with self.facade(
                callback,
                env_key="CUSTOM_SUPPLIER_TOKEN",
                env_http_headers={"X-Supplier": "CUSTOM_HEADER"},
                http_headers={"X-Version": "v1"},
                query_params={"api-version": "2026-09"},
            ) as (facade, upstream, home):
                (home / "auth.json").write_text(
                    json.dumps({"tokens": {"access_token": "original-chatgpt-key"}})
                )
                with self.assertRaises(HTTPError) as caught:
                    self.post(facade, token=False)
                self.assertEqual(caught.exception.code, 401)
                with self.assertRaises(HTTPError) as caught:
                    self.post(facade, headers={"OpenAI-Project": "project-secret"})
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(upstream.records, [])
                self.post(facade)
                record = upstream.records[0]
                self.assertEqual(
                    record["headers"]["Authorization"], "Bearer supplier-only-key"
                )
                self.assertEqual(record["headers"]["X-Supplier"], "supplier-header")
                self.assertEqual(record["path"], "/v1/responses?api-version=2026-09")
                self.assertNotIn(facade.token, json.dumps(record))
                with self.assertRaises(HTTPError) as caught:
                    self.post(
                        facade,
                        {
                            "model": MODEL.upstream_model,
                            "input": "original-chatgpt-key",
                        },
                    )
                self.assertEqual(caught.exception.code, 400)
                self.assertEqual(len(upstream.records), 1)
                for budget in (0, -1, True, 1.5, MODEL.max_output_tokens + 1):
                    with self.assertRaises(HTTPError) as caught:
                        self.post(
                            facade,
                            {
                                "model": MODEL.upstream_model,
                                "input": "hi",
                                "max_output_tokens": budget,
                            },
                        )
                    self.assertEqual(caught.exception.code, 400)
                self.assertEqual(len(upstream.records), 1)

    def test_command_auth_is_cached_and_stderr_is_not_returned(self):
        callback = lambda _: (
            200,
            "application/json",
            [(0, json.dumps(complete_response()).encode())],
        )
        auth = {
            "command": sys.executable,
            "args": ["-c", "print('supplier-command-token')"],
            "timeout_ms": 3000,
            "refresh_interval_ms": 60000,
        }
        with self.facade(callback, auth=auth) as (facade, upstream, _):
            self.post(facade)
            self.post(facade)
            self.assertEqual(
                [r["headers"]["Authorization"] for r in upstream.records],
                ["Bearer supplier-command-token"] * 2,
            )
            self.assertEqual(facade._cached_auth, "supplier-command-token")

    def test_zero_refresh_command_auth_uses_cwd_and_invalidates_after_401_without_retry(
        self,
    ):
        with tempfile.TemporaryDirectory() as auth_directory:
            counter = Path(auth_directory) / "counter.txt"
            command = "from pathlib import Path; p=Path('counter.txt'); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); print('supplier-'+str(n))"
            auth = {
                "command": sys.executable,
                "args": ["-c", command],
                "cwd": auth_directory,
                "refresh_interval_ms": 0,
            }
            failure = json.dumps(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Original provider auth failure",
                    }
                }
            ).encode()

            def callback(record):
                if record["body"]["input"] == "reject-token":
                    return 401, "application/json", [(0, failure)]
                return (
                    200,
                    "application/json",
                    [(0, json.dumps(complete_response()).encode())],
                )

            with self.facade(callback, auth=auth) as (facade, upstream, _):
                self.post(facade)
                self.post(facade)
                self.assertEqual(counter.read_text(), "1")
                with self.assertRaises(HTTPError) as caught:
                    self.post(
                        facade, {"model": MODEL.upstream_model, "input": "reject-token"}
                    )
                self.assertEqual(caught.exception.code, 401)
                self.assertEqual(caught.exception.read(), failure)
                self.assertEqual(len(upstream.records), 3)
                self.assertIsNone(facade._cached_auth)
                self.post(facade)
                self.assertEqual(counter.read_text(), "2")
                self.assertEqual(
                    upstream.records[-1]["headers"]["Authorization"],
                    "Bearer supplier-2",
                )

    def test_absent_or_empty_environment_headers_are_optional(self):
        callback = lambda _: (
            200,
            "application/json",
            [(0, json.dumps(complete_response()).encode())],
        )
        with patch.dict("os.environ", {"EMPTY_OPTIONAL_HEADER": ""}, clear=True):
            with self.facade(
                callback,
                env_http_headers={
                    "X-Empty": "EMPTY_OPTIONAL_HEADER",
                    "X-Absent": "ABSENT_OPTIONAL_HEADER",
                },
            ) as (facade, upstream, _):
                self.post(facade)
                self.assertNotIn("X-Empty", upstream.records[0]["headers"])
                self.assertNotIn("X-Absent", upstream.records[0]["headers"])

    def test_responses_tool_done_is_buffered_until_completed(self):
        call = {
            "id": "fc1",
            "type": "function_call",
            "status": "completed",
            "name": "mcp__calendar__list",
            "call_id": "call1",
            "arguments": "{}",
        }
        terminal = {
            "id": "resp1",
            "status": "completed",
            "error": None,
            "output": [call],
        }
        chunks = [
            (
                0,
                sse(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": call,
                    }
                ),
            ),
            (0.05, sse({"type": "response.completed", "response": terminal})),
        ]
        with self.facade(lambda _: (200, "text/event-stream", chunks)) as (
            facade,
            _,
            _,
        ):
            raw = self.post(
                facade,
                {
                    "model": MODEL.upstream_model,
                    "input": "hi",
                    "stream": True,
                    "tools": TOOLS,
                },
            )
            events = decoded_events(raw)
            self.assertEqual(
                [event["type"] for event in events],
                ["response.output_item.done", "response.completed"],
            )
            self.assertEqual(
                events[0]["item"],
                {**call, "name": "list", "namespace": "mcp__calendar"},
            )
            self.assertEqual(events[1]["response"]["output"][0], events[0]["item"])

    def test_backend_policy_failure_is_unchanged_and_never_releases_tools(self):
        call = {
            "id": "fc1",
            "type": "function_call",
            "status": "completed",
            "name": "mcp__calendar__list",
            "call_id": "call1",
            "arguments": "{}",
        }
        failure = {
            "id": "resp1",
            "status": "failed",
            "error": {"code": "cyber_policy", "message": "Original provider refusal"},
            "output": [],
        }
        chunks = [
            (
                0,
                sse(
                    {
                        "type": "response.output_item.done",
                        "item": call,
                        "output_index": 0,
                    }
                ),
            ),
            (0, sse({"type": "response.failed", "response": failure})),
        ]
        with self.facade(lambda _: (200, "text/event-stream", chunks)) as (
            facade,
            _,
            _,
        ):
            events = decoded_events(
                self.post(
                    facade,
                    {
                        "model": MODEL.upstream_model,
                        "input": "hi",
                        "stream": True,
                        "tools": TOOLS,
                    },
                )
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["response"], failure)

    def test_truncated_stream_and_invalid_arguments_report_failure(self):
        call = {
            "id": "fc1",
            "type": "function_call",
            "status": "completed",
            "name": "mcp__calendar__list",
            "call_id": "call1",
            "arguments": "{",
        }
        for terminal in (
            [],
            [
                (
                    0,
                    sse(
                        {
                            "type": "response.completed",
                            "response": {"status": "completed", "output": [call]},
                        }
                    ),
                )
            ],
        ):
            with self.subTest(terminal=bool(terminal)):
                chunks = [
                    (
                        0,
                        sse(
                            {
                                "type": "response.output_item.done",
                                "item": call,
                                "output_index": 0,
                            }
                        ),
                    ),
                    *terminal,
                ]
                with self.facade(lambda _: (200, "text/event-stream", chunks)) as (
                    facade,
                    _,
                    _,
                ):
                    events = decoded_events(
                        self.post(
                            facade,
                            {
                                "model": MODEL.upstream_model,
                                "input": "hi",
                                "stream": True,
                                "tools": TOOLS,
                            },
                        )
                    )
                    self.assertEqual(
                        [event["type"] for event in events], ["response.failed"]
                    )

    def test_buffered_completion_requires_the_same_terminal_tool_item(self):
        call = {
            "id": "fc1",
            "type": "function_call",
            "status": "completed",
            "name": "mcp__calendar__list",
            "call_id": "call1",
            "arguments": "{}",
        }
        for final in ([], [{**call, "arguments": '{"changed":true}'}]):
            chunks = [
                (
                    0,
                    sse(
                        {
                            "type": "response.output_item.done",
                            "item": call,
                            "output_index": 0,
                        }
                    ),
                ),
                (
                    0,
                    sse(
                        {
                            "type": "response.completed",
                            "response": {"status": "completed", "output": final},
                        }
                    ),
                ),
            ]
            with (
                self.subTest(final=final),
                self.facade(lambda _: (200, "text/event-stream", chunks)) as (
                    facade,
                    _,
                    _,
                ),
            ):
                events = decoded_events(
                    self.post(
                        facade,
                        {
                            "model": MODEL.upstream_model,
                            "input": "hi",
                            "stream": True,
                            "tools": TOOLS,
                        },
                    )
                )
                self.assertEqual(
                    [event["type"] for event in events], ["response.failed"]
                )

    def test_incoming_buffered_reasoning_refreshes_stream_idle_timer(self):
        chunks = [
            (
                0,
                b'data: {"id":"chat1","choices":[{"index":0,"delta":{"reasoning_content":"start"}}]}\n\n',
            )
        ]
        chunks.extend(
            (
                0.06,
                b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"more"}}]}\n\n',
            )
            for _ in range(4)
        )
        chunks.extend(
            [
                (
                    0,
                    b'data: {"choices":[{"index":0,"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n',
                ),
                (0, b"data: [DONE]\n\n"),
            ]
        )
        with self.facade(
            lambda _: (200, "text/event-stream", chunks),
            api="chat_completions",
            stream_idle_timeout_ms=100,
            upstream_timeout_seconds=2,
        ) as (facade, _, _):
            events = decoded_events(
                self.post(
                    facade,
                    {"model": MODEL.upstream_model, "input": "hi", "stream": True},
                )
            )
            self.assertEqual(events[-1]["type"], "response.completed")
            self.assertEqual(
                events[-1]["response"]["output"][1]["summary"][0]["text"],
                "start" + "more" * 4,
            )

    def test_idle_timeout_and_client_cancellation_cleanup(self):
        chunks = [
            (
                0,
                sse(
                    {
                        "type": "response.created",
                        "response": {"id": "resp1", "status": "in_progress"},
                    }
                ),
            ),
            (1, sse({"type": "response.completed", "response": complete_response()})),
        ]
        with self.facade(
            lambda _: (200, "text/event-stream", chunks),
            stream_idle_timeout_ms=100,
            upstream_timeout_seconds=2,
        ) as (facade, _, _):
            started = time.monotonic()
            events = decoded_events(
                self.post(
                    facade,
                    {"model": MODEL.upstream_model, "input": "hi", "stream": True},
                )
            )
            self.assertLess(time.monotonic() - started, 0.7)
            self.assertEqual(events[-1]["response"]["error"]["code"], "adapter_timeout")
        with self.facade(
            lambda _: (200, "text/event-stream", chunks),
            stream_idle_timeout_ms=2000,
            upstream_timeout_seconds=3,
        ) as (facade, _, _):
            connection = socket.create_connection(
                ("127.0.0.1", facade.server.server_port), timeout=2
            )
            body = json.dumps(
                {"model": MODEL.upstream_model, "input": "hi", "stream": True}
            ).encode()
            headers = f"POST /v1/responses HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {facade.token}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            connection.sendall(headers + body)
            received = b""
            while b"response.created" not in received:
                received += connection.recv(4096)
            connection.shutdown(socket.SHUT_RDWR)
            connection.close()
            deadline = time.monotonic() + 2
            while not facade.records and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(
                facade.records,
                "Cancelled request did not release its upstream connection",
            )
            self.assertEqual(facade.records[-1]["failure"], "client_disconnected")
            self.assertFalse(facade._connections)

    def test_chat_stream_translates_reasoning_tool_and_usage(self):
        chunks = [
            {
                "id": "chat1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "reasoning_content": "Inspect",
                            "content": "Working",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call1",
                                    "function": {
                                        "name": "mcp__calendar__list",
                                        "arguments": "{",
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": "}"}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        ]
        frames = [
            (0, ("data: " + json.dumps(chunk) + "\n\n").encode()) for chunk in chunks
        ] + [(0, b"data: [DONE]\n\n")]
        with self.facade(
            lambda _: (200, "text/event-stream", frames), api="chat_completions"
        ) as (facade, upstream, _):
            events = decoded_events(
                self.post(
                    facade,
                    {
                        "model": MODEL.upstream_model,
                        "input": "hi",
                        "stream": True,
                        "tools": TOOLS,
                    },
                )
            )
            self.assertEqual(upstream.records[0]["path"], "/v1/chat/completions")
            calls = [
                event["item"]
                for event in events
                if event["type"] == "response.output_item.done"
                and event["item"]["type"] == "function_call"
            ]
            self.assertEqual(calls[0]["name"], "list")
            self.assertEqual(calls[0]["namespace"], "mcp__calendar")
            self.assertEqual(events[-1]["response"]["usage"]["total_tokens"], 5)

    def test_checkpoint_resolver_model_binding_and_bounded_summary(self):
        seen = []

        def resolver(item, model=None):
            seen.append((item, model))
            return [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Verified earlier conversation"}
                    ],
                }
            ]

        callback = lambda _: (
            200,
            "application/json",
            [(0, json.dumps(complete_response("Short notes")).encode())],
        )
        with self.facade(callback, resolver=resolver) as (facade, upstream, _):
            self.post(
                facade,
                {
                    "model": MODEL.upstream_model,
                    "input": [
                        {"type": "compaction", "encrypted_content": "opaque-original"}
                    ],
                },
            )
            self.assertEqual(seen[0][1], MODEL)
            self.assertNotIn("opaque-original", json.dumps(upstream.records[0]["body"]))
            self.assertEqual(
                facade.summarize("Earlier transcript", MODEL), "Short notes"
            )
            self.assertEqual(upstream.records[-1]["body"]["max_output_tokens"], 3072)
            before = len(upstream.records)
            with self.assertRaisesRegex(ValueError, "registered"):
                facade.summarize(
                    "Earlier transcript",
                    dataclasses.replace(MODEL, provider="unregistered"),
                )
            self.assertEqual(len(upstream.records), before)
        with self.facade(
            lambda _: (
                200,
                "application/json",
                [(0, json.dumps(complete_response("x" * 7201)).encode())],
            )
        ) as (facade, _, _):
            with self.assertRaisesRegex(ValueError, "bounded"):
                facade.summarize("Earlier transcript")


if __name__ == "__main__":
    unittest.main()
