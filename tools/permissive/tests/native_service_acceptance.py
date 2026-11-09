"""Exercise the real native app-server through the service using a deterministic provider."""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestServer
from permissive.runtime import Runtime, MODEL
from permissive.store import Store


async def main(native):
    requests = []

    async def provider(request):
        body = await request.json()
        requests.append(body)
        await asyncio.sleep(0.2)
        message = {
            "id": "message-fixture",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "SERVICE-NATIVE-OK", "annotations": []}
            ],
        }
        if not any(
            item.get("type") in {"function_call_output", "custom_tool_call_output"}
            for item in body["input"]
        ):
            execution = next(
                tool
                for tool in body["tools"]
                if tool["type"] in {"custom", "function"}
                and tool["name"].endswith("exec")
            )
            message = {
                "id": "exec-fixture",
                "type": "custom_tool_call",
                "call_id": "exec-fixture",
                "status": "completed",
                "name": execution["name"],
                "input": 'text(JSON.stringify(await tools.exec_command({cmd:"git --version"})));',
            }
            if execution["type"] == "function":
                message.update(
                    type="function_call",
                    arguments=json.dumps({"input": message.pop("input")}),
                )
        result = {
            "id": "response-fixture",
            "model": MODEL,
            "object": "response",
            "status": "completed",
            "output": [message],
            "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        }
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        for value in [
            {
                "type": "response.created",
                "response": {**result, "status": "in_progress", "output": []},
            },
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": result},
        ]:
            await stream.write(("data: " + json.dumps(value) + "\n\n").encode())
        return stream

    app = web.Application()
    app.router.add_post("/v1/responses", provider)
    async with TestServer(app) as server:
        with tempfile.TemporaryDirectory(
            prefix="permissive-native-", dir="C:/codex-build"
        ) as directory:
            home = Path(directory)
            rules = home / "native-home/rules"
            rules.mkdir(parents=True)
            (rules / "default.rules").write_text(
                'prefix_rule(pattern=["git"], decision="forbidden")\n'
            )
            runtime = Runtime(
                home,
                native,
                str(server.make_url("/v1")),
                ["node", str(Path(__file__).parents[1] / "browser/worker.mjs")],
            )
            store = Store(home)
            tasks = [
                store.submit(
                    {
                        "prompt": "Say service native OK",
                        "workingDirectory": str(home),
                        "browserMode": "dedicated",
                    }
                )[0]
                for _ in range(2)
            ]
            for task in tasks:
                store.update(task["id"], status="running")
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(runtime.run(store.get(task["id"]), store) for task in tasks)
                    ),
                    120,
                )
                for task in tasks:
                    actual = store.get(task["id"])
                    if (actual["status"], actual["result"]) != (
                        "completed",
                        "SERVICE-NATIVE-OK",
                    ):
                        raise AssertionError(actual)
                assert all(body["model"] == MODEL for body in requests)
                assert (
                    len(requests) == 4
                    and sum(
                        "git version" in json.dumps(body["input"]) for body in requests
                    )
                    == 2
                )
                print(
                    "Concurrent native startup, code-mode execution, policy bypass, quiet streaming, and result persistence passed."
                )
            finally:
                await runtime.close()
                store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", required=True)
    asyncio.run(main(parser.parse_args().native))
