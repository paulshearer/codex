"""Native Windows acceptance against controlled Responses/Chat model services."""

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time


class ModelService:
    def __init__(self):
        self.records = []
        self.cancel_started = threading.Event()
        self.cancel_closed = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.records.append(
                    {"path": self.path, "body": body, "headers": dict(self.headers)}
                )
                if self.path.endswith("/chat/completions"):
                    self.chat(body)
                else:
                    self.responses_stream(body)

            def event(self, value):
                raw = value if isinstance(value, str) else json.dumps(value)
                self.wfile.write(("data: " + raw + "\n\n").encode())
                self.wfile.flush()

            def begin(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

            def chat(self, body):
                messages = body["messages"]
                last_user = max(
                    (
                        index
                        for index, item in enumerate(messages)
                        if item["role"] == "user"
                    ),
                    default=0,
                )
                prompt = messages[last_user].get("content", "")
                outputs = [
                    item for item in messages[last_user + 1 :] if item["role"] == "tool"
                ]
                self.begin()
                identifier = "chat-acceptance-" + str(len(owner.records))

                def chunk(delta, reason=None):
                    self.event(
                        {
                            "id": identifier,
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": reason}
                            ],
                        }
                    )

                if "CANCEL-CASE" in prompt:
                    chunk({"content": "Waiting for cancellation."})
                    owner.cancel_started.set()
                    self.connection.settimeout(10)
                    try:
                        while not owner.cancel_closed.is_set():
                            if select.select([self.connection], [], [], 1)[
                                0
                            ] and not self.connection.recv(1, socket.MSG_PEEK):
                                owner.cancel_closed.set()
                    except (OSError, TimeoutError):
                        owner.cancel_closed.set()
                    return
                if "INCOMPLETE-CASE" in prompt:
                    chunk({"content": "This incomplete text must not succeed."})
                    return
                if "PATCH-CASE" in prompt and not outputs:
                    tools = body.get("tools", [])
                    function = next(
                        tool["function"]
                        for tool in tools
                        if "apply_patch" in tool["function"]["name"]
                    )
                    arguments = json.dumps(
                        {
                            "input": "*** Begin Patch\n*** Add File: proof.txt\n+custom-models verified\n*** End Patch"
                        }
                    )
                    chunk(
                        {
                            "reasoning_content": "Use the patch tool in the workspace.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "patch-call",
                                    "type": "function",
                                    "function": {
                                        "name": function["name"],
                                        "arguments": arguments[:25],
                                    },
                                }
                            ],
                        }
                    )
                    chunk(
                        {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": arguments[25:]}}
                            ]
                        },
                        "tool_calls",
                    )
                elif "CALLBACK-CASE" in prompt and len(outputs) < 2:
                    description = (
                        "First acceptance callback"
                        if not outputs
                        else "Second acceptance callback"
                    )
                    function = next(
                        tool["function"]
                        for tool in body["tools"]
                        if tool["function"].get("description") == description
                    )
                    chunk(
                        {
                            "reasoning_content": "Use a namespaced callback.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "callback-" + str(len(outputs)),
                                    "type": "function",
                                    "function": {
                                        "name": function["name"],
                                        "arguments": '{"value":"verified"}',
                                    },
                                }
                            ],
                        },
                        "tool_calls",
                    )
                else:
                    text = (
                        "PATCH-OK"
                        if "PATCH-CASE" in prompt
                        else "CALLBACK-OK"
                        if "CALLBACK-CASE" in prompt
                        else "RECALL orchid-8721"
                        if "RECALL-CASE" in prompt
                        else "CHAT-OK"
                    )
                    chunk({"content": text}, "stop")
                self.event(
                    {
                        "id": identifier,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                        },
                    }
                )
                self.event("[DONE]")

            def responses_stream(self, body):
                text = "RESPONSES-OK"
                prompt = json.dumps(body.get("input", ""))
                if "RECALL-CASE" in prompt:
                    if "orchid-8721" not in prompt:
                        raise RuntimeError(
                            "Readable checkpoint context was not forwarded"
                        )
                    text = "RECALL orchid-8721"
                message = {
                    "id": "msg_acceptance",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
                result = {
                    "id": "resp_acceptance",
                    "object": "response",
                    "model": body["model"],
                    "status": "completed",
                    "output": [message],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                    },
                }
                if not body.get("stream"):
                    result["output"][0]["content"][0]["text"] = (
                        "Remember orchid-8721. Native verification is pending."
                    )
                    raw = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.begin()
                self.event(
                    {
                        "type": "response.created",
                        "response": {**result, "status": "in_progress", "output": []},
                    }
                )
                self.event(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": message,
                    }
                )
                self.event({"type": "response.completed", "response": result})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = "http://127.0.0.1:" + str(self.server.server_address[1]) + "/v1"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args):
        self.cancel_closed.set()
        self.server.shutdown()
        self.server.server_close()


class Client:
    def __init__(self, command, environment):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            encoding="utf-8",
        )
        self.messages = queue.Queue()
        self.pending = []
        self.errors = []
        self.callbacks = []
        self.sequence = 0

        def read():
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.messages.put(
                        RuntimeError("Invalid app-server stdout: " + line)
                    )
            self.messages.put(RuntimeError("App-server closed stdout"))

        def diagnostics():
            for line in self.process.stderr:
                self.errors.append(line)

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=diagnostics, daemon=True).start()

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self, match, seconds=60):
        deadline = time.monotonic() + seconds
        for index, item in enumerate(self.pending):
            if match(item):
                return self.pending.pop(index)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                methods = [
                    item.get("method", "response") for item in self.pending[-15:]
                ]
                raise TimeoutError(
                    "App-server wait expired; recent messages: "
                    + repr(methods)
                    + "\n"
                    + "".join(self.errors[-15:])
                )
            try:
                item = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(
                    "App-server wait expired; pending: "
                    + repr(self.pending[-8:])
                    + "\n"
                    + "".join(self.errors[-15:])
                ) from None
            if isinstance(item, Exception):
                raise RuntimeError(str(item) + "\n" + "".join(self.errors[-15:]))
            if item.get("method") == "item/tool/call" and "id" in item:
                self.callbacks.append(item["params"])
                self.send(
                    {
                        "id": item["id"],
                        "result": {
                            "contentItems": [
                                {"type": "inputText", "text": "callback verified"}
                            ],
                            "success": True,
                        },
                    }
                )
            elif (
                item.get("method") == "item/fileChange/requestApproval" and "id" in item
            ):
                self.send({"id": item["id"], "result": {"decision": "accept"}})
            elif match(item):
                return item
            else:
                self.pending.append(item)

    def request(self, method, params, expect_error=False):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        response = self.receive(lambda item: item.get("id") == self.sequence)
        if expect_error:
            assert "error" in response, response
            return response["error"]
        if "error" in response:
            raise RuntimeError(
                json.dumps(response["error"]) + "\n" + "".join(self.errors[-15:])
            )
        return response["result"]

    def initialize(self):
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "codex-custom-acceptance", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.send({"method": "initialized"})

    def turn(self, tid, text, success=True):
        response = self.request(
            "turn/start", {"threadId": tid, "input": [{"type": "text", "text": text}]}
        )
        turn_id = response["turn"]["id"]
        complete = self.receive(
            lambda item: (
                item.get("method") == "turn/completed"
                and item["params"]["turn"]["id"] == turn_id
            )
        )
        status = complete["params"]["turn"]["status"]
        assert (status == "completed") == success, complete
        return complete

    def close(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=20)
            assert code == 0, (
                "Router failed during cleanup: "
                + str(code)
                + "\n"
                + "".join(self.errors[-15:])
            )
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
            raise RuntimeError(
                "Router/native process failed to stop after stdin closed"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--launcher", type=Path)
    options = parser.parse_args()
    native = options.native.resolve()
    with (
        tempfile.TemporaryDirectory(
            prefix="codex acceptance with spaces ", ignore_cleanup_errors=True
        ) as directory,
        ModelService() as service,
    ):
        root = Path(directory)
        home = root / "profile"
        workspace = root / "workspace"
        workspace.mkdir()
        registry = root / "registry.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default_model": "chat::same",
                    "providers": {
                        key: {
                            "api_format": api,
                            "base_url": service.base_url,
                            "http_headers": {"X-Provider": key},
                            "stream_idle_timeout_ms": 15000,
                            "upstream_timeout_seconds": 30,
                        }
                        for key, api in (
                            ("chat", "chat_completions"),
                            ("response", "responses"),
                        )
                    },
                    "models": [
                        {
                            "id": "same",
                            "provider": key,
                            "upstream_model": "same-model",
                            "display_name": key + " same",
                            "context_window": 262144,
                            "max_output_tokens": 16384,
                            "auto_compact_token_limit": 196608,
                            "reasoning_efforts": ["medium"],
                            "default_reasoning_effort": "medium",
                        }
                        for key in ("chat", "response")
                    ],
                }
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        for key in {
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_ORGANIZATION",
            "OPENAI_PROJECT_ID",
        }:
            environment.pop(key, None)
        environment["PYTHONPATH"] = str(Path(__file__).parent)
        prefix = (
            [str(options.launcher.resolve())]
            if options.launcher
            else [sys.executable, "-m", "custom_models"]
        )
        prefix += [
            "--registry",
            str(registry),
            "--home",
            str(home),
            "--native",
            str(native),
        ]
        command = prefix + ["app-server", "--listen", "stdio://"]
        dynamic = [
            {
                "type": "namespace",
                "name": name,
                "description": name,
                "tools": [
                    {
                        "type": "function",
                        "name": "echo",
                        "description": description,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    }
                ],
            }
            for name, description in (
                ("verificationOne", "First acceptance callback"),
                ("verificationTwo", "Second acceptance callback"),
            )
        ]
        client = Client(command, environment)
        try:
            client.initialize()
            account = client.request("account/read", {"refreshToken": False})
            assert (
                account["account"] is None and account["requiresOpenaiAuth"] is False
            ), account
            assert not (home / "auth.json").exists(), (
                "Fresh custom profile unexpectedly created OpenAI credentials"
            )
            assert not service.records, (
                "Reading account metadata unexpectedly called the supplier"
            )
            print("Fresh custom profile account metadata verified", flush=True)
            catalog = client.request("model/list", {"limit": 100})
            assert {"chat::same", "response::same"} <= {
                entry["id"] for entry in catalog["data"]
            }
            started = client.request(
                "thread/start",
                {
                    "model": "chat::same",
                    "cwd": str(workspace),
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "sandbox": "workspace-write",
                    "dynamicTools": dynamic,
                    "config": {
                        "model_providers": {
                            "chat": {
                                "base_url": "http://invalid.example/v1",
                                "env_key": "NEVER_USE",
                            }
                        }
                    },
                },
            )
            tid = started["thread"]["id"]
            assert started["model"] == "chat::same", started
            print("Started native Chat thread", flush=True)
            client.turn(tid, "PATCH-CASE: Add proof.txt with the patch tool.")
            print("Patch turn completed", flush=True)
            assert (
                workspace / "proof.txt"
            ).read_text().strip() == "custom-models verified"
            patch_followup = next(
                record["body"]
                for record in service.records
                if record["path"].endswith("chat/completions")
                and any(
                    item["role"] == "tool" and item.get("tool_call_id") == "patch-call"
                    for item in record["body"]["messages"]
                )
            )
            assert any(
                item.get("reasoning_content") == "Use the patch tool in the workspace."
                for item in patch_followup["messages"]
            )
            client.turn(tid, "CALLBACK-CASE: Invoke both namespaced echo tools.")
            print("Callback turn completed", flush=True)
            assert {callback["namespace"] for callback in client.callbacks} == {
                "verificationOne",
                "verificationTwo",
            }, client.callbacks
            other = client.request(
                "thread/start",
                {
                    "model": "response::same",
                    "cwd": str(workspace),
                    "approvalPolicy": "on-request",
                    "sandbox": "workspace-write",
                },
            )
            client.turn(other["thread"]["id"], "Responses streaming smoke.")
            client.turn(tid, "INCOMPLETE-CASE", success=False)
            response = client.request(
                "turn/start",
                {"threadId": tid, "input": [{"type": "text", "text": "CANCEL-CASE"}]},
            )
            assert service.cancel_started.wait(15), (
                "Streaming cancellation test never started"
            )
            client.request(
                "turn/start",
                {
                    "threadId": tid,
                    "model": "response::same",
                    "input": [{"type": "text", "text": "Switch"}],
                },
                expect_error=True,
            )
            client.request(
                "turn/interrupt", {"threadId": tid, "turnId": response["turn"]["id"]}
            )
            complete = client.receive(
                lambda item: (
                    item.get("method") == "turn/completed"
                    and item["params"]["turn"]["id"] == response["turn"]["id"]
                )
            )
            assert complete["params"]["turn"]["status"] == "interrupted", complete
            assert service.cancel_closed.wait(10), (
                "Cancelling native turn did not close upstream request"
            )
        finally:
            client.close()
        resumed = Client(command, environment)
        try:
            resumed.initialize()
            result = resumed.request(
                "thread/resume", {"threadId": tid, "model": None, "modelProvider": None}
            )
            assert result["thread"]["id"] == tid and result["model"] == "chat::same", (
                result
            )
            resumed.turn(tid, "Restart/resume smoke.")
        finally:
            resumed.close()
        assert all(
            record["headers"].get("X-Provider") in {"chat", "response"}
            for record in service.records
        )
        assert all(
            not any(
                header.lower()
                in {"authorization", "cookie", "openai-organization", "openai-project"}
                for header in record["headers"]
            )
            for record in service.records
        )
        print(
            "PASS: native Responses + Chat streaming; actual patch; reasoning continuity; namespaces/callbacks; failures; cancellation; restart/null resume; provider override/auth isolation; paths with spaces"
        )


if __name__ == "__main__":
    main()
