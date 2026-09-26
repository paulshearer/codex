"""Exercise native MCP discovery and native auto compaction with local fixtures."""

import argparse
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import urllib.request


MARKER = "orchid-8721"
FILLER = "x "
HISTORY_WORDS = 215_000
SUMMARY = f"Verified handoff: remember {MARKER}; the large history was accepted and no work is pending."


def mcp_server(log):
    """A real line-delimited MCP stdio server; expose one local read-only tool."""
    for line in sys.stdin:
        request = json.loads(line)
        with Path(log).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(request) + "\n")
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "acceptance-echo", "version": "1"},
                "instructions": "Echo a local acceptance marker; no external access or side effects.",
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo a local acceptance marker for verification.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                        "annotations": {
                            "readOnlyHint": True,
                            "destructiveHint": False,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            assert request["params"]["name"] == "echo", (
                "Only the local echo fixture can be called"
            )
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "ACTUAL-MCP-ECHO:"
                        + request["params"]["arguments"]["value"],
                    }
                ],
                "isError": False,
            }
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "ping":
            result = {}
        elif "id" not in request:
            continue
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": "Unknown fixture method"},
                    }
                ),
                flush=True,
            )
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
            flush=True,
        )


class ProviderService:
    def __init__(self):
        self.records, self.errors = [], []
        self.compactions = 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    body = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    owner.records.append(body)
                    assert self.path.endswith("/chat/completions"), self.path
                    self.chat(body)
                except Exception as error:
                    owner.errors.append(str(error) or type(error).__name__)
                    self.send_response(500)
                    self.end_headers()

            def event(self, value):
                value = value if isinstance(value, str) else json.dumps(value)
                self.wfile.write(("data: " + value + "\n\n").encode())
                self.wfile.flush()

            def chat(self, body):
                messages = body["messages"]
                last_user = max(
                    index
                    for index, item in enumerate(messages)
                    if item["role"] == "user"
                )
                prompt = messages[last_user].get("content", "")
                transcript = json.dumps(messages)
                tools = body.get("tools", [])
                outputs = [
                    item for item in messages[last_user + 1 :] if item["role"] == "tool"
                ]
                call, text, tokens = None, "ADVANCED-OK", 100
                if "CONTEXT CHECKPOINT COMPACTION" in prompt:
                    assert MARKER in transcript, (
                        "Native compaction lost the source marker"
                    )
                    owner.compactions += 1
                    text = SUMMARY
                elif "TRANSCRIPT PORTION:" in prompt:
                    assert MARKER in transcript, (
                        "Handoff summary portion lacks its original marker"
                    )
                    text = SUMMARY
                elif "MCP-CASE" in prompt:
                    if not outputs:
                        assert not any(
                            "acceptance_echo" in tool["function"]["name"]
                            for tool in tools
                        ), "MCP fixture was not deferred"
                        search = next(
                            tool["function"]
                            for tool in tools
                            if "tool_search" in tool["function"]["name"]
                        )
                        call = (
                            search["name"],
                            "native-search-call",
                            {"query": "acceptance echo local marker", "limit": 1},
                        )
                    elif not any(
                        item.get("tool_call_id") == "native-mcp-call"
                        for item in outputs
                    ):
                        echo = next(
                            tool["function"]
                            for tool in tools
                            if "acceptance_echo" in tool["function"]["name"]
                            and "echo" in tool["function"]["name"]
                        )
                        assert "native-search-call" in transcript, (
                            "Native discovery output was not retained"
                        )
                        call = (echo["name"], "native-mcp-call", {"value": MARKER})
                    else:
                        assert "ACTUAL-MCP-ECHO:" + MARKER in transcript, (
                            "Actual MCP output did not reach the model"
                        )
                        text = "MCP-OK " + MARKER
                elif "LONG-HISTORY-CASE" in prompt:
                    assert prompt.count(FILLER) == HISTORY_WORDS, (
                        "Full large fixture input was lost before inference"
                    )
                    tokens = 214_248
                    text = "LARGE-HISTORY-OK " + MARKER
                elif "RECALL-CASE" in prompt:
                    assert SUMMARY in transcript, (
                        "Native follow-up lacks the verified plain handoff"
                    )
                    assert transcript.count(FILLER) < HISTORY_WORDS // 4, (
                        "Full large history remains in active context"
                    )
                    text = "RECALL " + MARKER
                identifier = "advanced-" + str(len(owner.records))
                if not body.get("stream"):
                    assert MARKER in transcript, (
                        "Handoff summarization lacks its source marker"
                    )
                    result = {
                        "id": identifier,
                        "object": "chat.completion",
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                        },
                    }
                    raw = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if call:
                    name, call_id, arguments = call
                    delta, reason = (
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ]
                        },
                        "tool_calls",
                    )
                else:
                    delta, reason = {"content": text}, "stop"
                self.event(
                    {
                        "id": identifier,
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": reason}
                        ],
                    }
                )
                self.event(
                    {
                        "id": identifier,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": tokens,
                            "completion_tokens": 20,
                            "total_tokens": tokens + 20,
                        },
                    }
                )
                self.event("[DONE]")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()


def client_type():
    path = Path(__file__).with_name("native-acceptance.py")
    spec = importlib.util.spec_from_file_location("custom_native_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Client


def encrypted_handoff(root, registry, service):
    from custom_models.facade import Facade
    from custom_models.handoff import HandoffResolver, MAX_HANDOFF_BYTES
    from custom_models.registry import Registry

    # This separate synthetic profile never edits a native thread's rollout.
    home = root / "synthetic encrypted profile"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    text = (
        f"Historical task record: remember {MARKER}; local tests remain pending.\n"
        * 400
    )
    assert len(text.encode()) > MAX_HANDOFF_BYTES
    item = {"type": "compaction", "encrypted_content": "synthetic-acceptance-cipher"}
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        {"type": "compacted", "payload": {"replacement_history": [item]}},
    ]
    source = sessions / "rollout-synthetic-checkpoint.jsonl"
    original = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    source.write_bytes(original)
    settings = Registry.load(registry)
    model = settings.model()
    resolver = HandoffResolver(home, home / "handoffs")
    facade = Facade(settings.provider(model.provider), [model], home, resolver.resolve)
    resolver.set_summarizer(facade.summarize)
    url = facade.start()
    before = len(service.records)
    try:
        body = {
            "model": model.upstream_model,
            "stream": False,
            "input": [
                item,
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "RECALL-CASE: State the source marker.",
                        }
                    ],
                },
            ],
        }
        request = urllib.request.Request(
            url + "/responses",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + facade.token,
            },
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
        assert result["status"] == "completed" and "RECALL " + MARKER in json.dumps(
            result
        ), result
        cached = list(resolver.cache.glob("*.json"))
        assert len(cached) == 1, cached
        record = json.loads(cached[0].read_text(encoding="utf-8"))
        assert record["method"] == "verified-readable-summary", record["method"]
        assert record["source_sha256"] == hashlib.sha256(original).hexdigest()
        assert (
            record["summary_sha256"]
            == hashlib.sha256(record["summary"].encode()).hexdigest()
        )
        assert (
            MARKER in record["summary"]
            and len(record["summary"].encode()) <= MAX_HANDOFF_BYTES
        )
        assert source.read_bytes() == original, (
            "Source history was changed during handoff"
        )
        calls = service.records[before:]
        assert len(calls) >= 3, (
            "Large checkpoint was not summarized in bounded provider calls"
        )
        assert all(
            "synthetic-acceptance-cipher" not in json.dumps(call) for call in calls
        )
        print(
            "PASS: integrated encrypted checkpoint handoff summarized verified source, recalled marker, and preserved source/cache hashes",
            flush=True,
        )
    finally:
        facade.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--launcher", type=Path)
    options = parser.parse_args()
    Client = client_type()
    with (
        tempfile.TemporaryDirectory(
            prefix="codex advanced acceptance with spaces ", ignore_cleanup_errors=True
        ) as directory,
        ProviderService() as service,
    ):
        root, workspace = Path(directory), Path(directory) / "workspace"
        workspace.mkdir()
        home, log = root / "profile", root / "mcp-proof.jsonl"
        registry = root / "registry.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default_model": "fixture::test",
                    "providers": {
                        "fixture": {
                            "api_format": "chat_completions",
                            "base_url": service.base_url,
                            "stream_idle_timeout_ms": 30_000,
                            "upstream_timeout_seconds": 45,
                        }
                    },
                    "models": [
                        {
                            "id": "test",
                            "provider": "fixture",
                            "upstream_model": "fixture-test",
                            "display_name": "Advanced fixture",
                            "context_window": 262_144,
                            "max_output_tokens": 16_384,
                            "auto_compact_token_limit": 196_608,
                            "reasoning_efforts": ["medium"],
                            "default_reasoning_effort": "medium",
                            "supports_tool_search": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).parent)
        prefix = (
            [str(options.launcher.resolve())]
            if options.launcher
            else [sys.executable, "-m", "custom_models"]
        )
        command = prefix + [
            "--registry",
            str(registry),
            "--home",
            str(home),
            "--native",
            str(options.native.resolve()),
            "app-server",
            "--listen",
            "stdio://",
        ]
        client = Client(command, environment)
        try:
            client.initialize()
            controls = {
                "model": "fixture::test",
                "cwd": str(workspace),
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": "workspace-write",
            }
            started = client.request(
                "thread/start",
                {
                    **controls,
                    "config": {
                        "mcp_servers": {
                            "acceptance_echo": {
                                "command": sys.executable,
                                "args": [
                                    str(Path(__file__).resolve()),
                                    "--mcp-server",
                                    str(log),
                                ],
                                "startup_timeout_sec": 30,
                                "tool_timeout_sec": 15,
                                "enabled_tools": ["echo"],
                            }
                        }
                    },
                },
            )
            print("Started native MCP discovery thread", flush=True)
            client.turn(
                started["thread"]["id"],
                "MCP-CASE: Discover and invoke the local acceptance echo tool.",
            )
            calls = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]
            assert any(call.get("method") == "initialize" for call in calls)
            assert any(call.get("method") == "tools/list" for call in calls)
            actual_calls = [
                call for call in calls if call.get("method") == "tools/call"
            ]
            assert len(actual_calls) == 1 and actual_calls[0]["params"][
                "arguments"
            ] == {"value": MARKER}, actual_calls
            print(
                "PASS: deferred tool_search discovery invoked the actual native MCP stdio server",
                flush=True,
            )
            long_thread = client.request("thread/start", controls)
            tid = long_thread["thread"]["id"]
            large = (
                "LONG-HISTORY-CASE: Remember " + MARKER + ".\n" + FILLER * HISTORY_WORDS
            )
            client.turn(tid, large)
            print(
                "Large native turn completed; reported usage214248 exceeds configured threshold196608",
                flush=True,
            )
            client.turn(
                tid, "RECALL-CASE: State the remembered marker from the compacted task."
            )
            paths = [
                path
                for path in (home / "sessions").rglob("*.jsonl")
                if tid in path.name
            ]
            assert len(paths) == 1, paths
            rows = [
                json.loads(line)
                for line in paths[0].read_text(encoding="utf-8").splitlines()
            ]
            compacted = [
                row["payload"] for row in rows if row.get("type") == "compacted"
            ]
            assert compacted and service.compactions > 0, (
                "Native automatic compaction did not persist a checkpoint"
            )
            replacement = json.dumps(compacted[-1]["replacement_history"])
            assert SUMMARY in replacement and MARKER in replacement, (
                "Saved replacement history lacks plain verified handoff"
            )
            assert replacement.count(FILLER) < HISTORY_WORDS // 4, (
                "Checkpoint retained the full large context"
            )
            recall = next(
                body
                for body in reversed(service.records)
                if "RECALL-CASE" in json.dumps(body["messages"][-1])
            )
            retained_words = json.dumps(recall["messages"]).count(FILLER)
            assert retained_words < HISTORY_WORDS // 4
            print(
                "PASS: native automatic compaction persisted a plain handoff, removed bulk active history, and recalled marker",
                flush=True,
            )
            print(
                f"Bulk fixture words: {HISTORY_WORDS} before compaction, {retained_words} after",
                flush=True,
            )
            encrypted_handoff(root, registry, service)
            assert not service.errors, service.errors
        finally:
            client.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mcp-server"]:
        mcp_server(sys.argv[2])
    else:
        main()
