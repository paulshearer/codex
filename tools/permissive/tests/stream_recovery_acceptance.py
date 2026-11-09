"""Real native recovery after a completed command and malformed provider terminal.

Uses an isolated application home and loopback provider; no external inference,
browser interaction, installed application state or user history is accessed.
"""

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time

from aiohttp import web
from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.runtime import MODEL, Runtime
from permissive.service import Service
from permissive.stream_recovery import RECOVERY_PROMPT


ERROR = "Responses completed event has no completed response"


def user_texts(body):
    return [
        "\n".join(part.get("text", "") for part in item.get("content", []) if isinstance(part, dict))
        if isinstance(item.get("content"), list) else item.get("content", "")
        for item in body.get("input", []) if item.get("role") == "user"
    ]


async def scenario(native, malformed_status, browser_worker):
    began = time.monotonic()
    name = malformed_status or "missing"
    records, provider_failures = [], []
    original = "STREAM-RECOVERY-ORIGINAL-" + name
    attachment_name = "original-attachment-" + name + ".txt"
    with tempfile.TemporaryDirectory(prefix="permissive-stream-recovery-", dir="C:/codex-build") as directory:
        home = Path(directory)
        effect = home / "effect.txt"
        runtime = None

        async def provider(request):
            try:
                body = await request.json()
                outputs = [item for item in body["input"] if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
                users = user_texts(body)
                latest = users[-1] if users else ""
                backend = next(iter(runtime.workers.values()))[0]
                record = {"request": len(records) + 1, "nativePid": backend.proc.pid,
                          "toolOutputs": len(outputs), "originalUserMessages": sum(original in text for text in users),
                          "attachmentUserMessages": sum(attachment_name in text for text in users),
                          "latestIsRecovery": RECOVERY_PROMPT in latest,
                          "latestRepeatsOriginal": original in latest,
                          "latestRepeatsAttachment": attachment_name in latest}
                records.append(record)
                assert record["originalUserMessages"] == 1, record
                assert record["attachmentUserMessages"] == 1, record
                step = len(records)
                assert step <= 3, "Unexpected provider retry or original task replay"
                message = {"id": "answer", "type": "message", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "STREAM-RECOVERY-NATIVE-OK", "annotations": []}]}
                if step == 1:
                    assert not outputs and not record["latestIsRecovery"], record
                    execution = next(tool for tool in body["tools"] if tool["type"] in {"custom", "function"} and tool["name"].endswith("exec"))
                    command = "[IO.File]::AppendAllText('" + effect.as_posix().replace("'", "''") + "', 'EFFECT-ONCE')"
                    code = "text(JSON.stringify(await tools.exec_command(" + json.dumps({"cmd": command, "yield_time_ms": 10000}) + ")));"
                    message = {"id": "effect-once", "type": "function_call", "call_id": "effect-once", "status": "completed",
                               "name": execution["name"], "arguments": json.dumps({"input": code})}
                    if execution["type"] == "custom":
                        message.update(type="custom_tool_call", input=code)
                        message.pop("arguments")
                else:
                    assert effect.read_text() == "EFFECT-ONCE", "Completed command was missing or repeated"
                    assert len(outputs) == 1, record
                    if step == 2:
                        assert not record["latestIsRecovery"], record
                    else:
                        assert record["latestIsRecovery"], record
                        assert not record["latestRepeatsOriginal"] and not record["latestRepeatsAttachment"], record
                result = {"id": "response-" + str(step), "model": MODEL, "object": "response", "status": "completed",
                          "output": [message], "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}
                events = [{"type": "response.created", "response": {**result, "status": "in_progress", "output": []}}]
                if step == 2:
                    result["output"] = []
                    if malformed_status is None:
                        result.pop("status")
                    else:
                        result["status"] = malformed_status
                else:
                    events.append({"type": "response.output_item.done", "output_index": 0, "item": message})
                events.append({"type": "response.completed", "response": result})
                stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await stream.prepare(request)
                for event in events:
                    await stream.write(("data: " + json.dumps(event) + "\n\n").encode())
                return stream
            except Exception as error:
                provider_failures.append(repr(error))
                return web.json_response({"error": {"code": "fixture_assertion", "message": "Deterministic fixture failed"}}, status=500)

        upstream = web.Application()
        upstream.router.add_post("/v1/responses", provider)
        async with TestServer(upstream) as server, asyncio.timeout(120):
            runtime = Runtime(home, native, str(server.make_url("/v1")),
                              ["node", str(browser_worker)])
            service = Service(home, runtime)
            async with SignedTestClient(TestServer(service.app()), provision(home)) as client:
                async def post(path, body):
                    response = await client.post(path, json=body, headers={"X-Test-Sign": "1"})
                    assert response.status < 300, (response.status, await response.text())
                    return await response.json()

                workspace = service.store.workspaces()["data"][0]["id"]
                attachment = await post("/v1/attachments", {"name": attachment_name,
                    "data": base64.b64encode(b"Isolated deterministic acceptance attachment").decode(), "workspaceId": workspace})
                task = await post("/v1/tasks", {"prompt": original, "workingDirectory": directory,
                    "workspaceId": workspace, "attachments": [attachment["id"]]})
                while service.jobs:
                    await asyncio.sleep(.05)
                saved = service.store.get(task["id"])
                events = []
                while page := service.store.conversation_events(task["conversationId"], events[-1]["id"] if events else 0):
                    events.extend(page)
                agent = [event["data"] for event in events if event["kind"] == "agent"]
                turns = [event["params"] for event in agent if event.get("method") == "turn/completed"]
                commands = [event["params"]["item"] for event in agent if event.get("method") == "item/completed"
                            and event.get("params", {}).get("item", {}).get("type") == "commandExecution"]
                phases = [event["data"]["phase"] for event in events if event["kind"] == "recovery"]
                assert not provider_failures, provider_failures
                assert saved["status"] == "completed" and saved["result"] == "STREAM-RECOVERY-NATIVE-OK", saved
                assert len(records) == 3 and len({record["nativePid"] for record in records}) == 1, records
                assert effect.read_text() == "EFFECT-ONCE", "Effect was repeated"
                assert len(commands) == 1 and commands[0]["exitCode"] == 0, commands
                assert phases == ["retrying", "recovered"], phases
                assert len(turns) == 2 and [turn["turn"]["status"] for turn in turns] == ["failed", "completed"], turns
                assert ERROR in turns[0]["turn"]["error"]["message"], turns[0]
                assert {turn["threadId"] for turn in turns} == {saved["threadId"]}, turns
                assert len(service.store.conversation_tasks(task["conversationId"])) == 1
                assert saved["request"]["attachments"] == [attachment["id"]]
                return {"scenario": name, "status": saved["status"], "providerRequests": records,
                        "sameNativeProcess": True, "sameNativeThread": True, "sameTask": True,
                        "commandExecutions": len(commands), "effectOccurrences": effect.read_text().count("EFFECT-ONCE"),
                        "nativeTurnStatuses": [turn["turn"]["status"] for turn in turns],
                        "originalError": turns[0]["turn"]["error"], "recoveryPhases": phases,
                        "originalPromptNotResubmitted": True, "attachmentNotResubmitted": True,
                        "nativeRetriesDisabled": all(runtime.registry.native_config(runtime.registry.model(), "http://127.0.0.1:1/v1")["model_providers.deepseek"][key] == 0 for key in ("request_max_retries", "stream_max_retries")),
                        "seconds": round(time.monotonic() - began, 3)}


async def main(native, output, browser_worker):
    native = Path(native).resolve(strict=True)
    results = [await scenario(native, status, browser_worker) for status in (None, "in_progress")]
    with native.open("rb") as stream:
        native_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    from custom_models import facade
    from permissive import runtime, service, store, stream_recovery
    modules = {}
    for module in (facade, runtime, service, store, stream_recovery):
        path = Path(module.__file__)
        modules[module.__name__] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    evidence = {"status": "passed", "nativeBinary": str(native), "nativeSha256": native_sha256, "modules": modules,
                "isolatedTemporaryHomes": True, "loopbackDeterministicProvider": True, "externalModelRequests": 0,
                "browserActions": 0, "cases": results}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", required=True)
    parser.add_argument("--output", default="C:/codex-build/stream-recovery-patch-20261019/native-acceptance.json")
    parser.add_argument("--browser-worker", default=str(Path(__file__).parents[1] / "browser/worker.mjs"))
    arguments = parser.parse_args()
    asyncio.run(main(arguments.native, arguments.output, arguments.browser_worker))
