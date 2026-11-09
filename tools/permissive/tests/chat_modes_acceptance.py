"""Verify real native Plan mode and multi-turn goal completion with a local provider."""

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
    goal_requests = 0

    async def provider(request):
        nonlocal goal_requests
        body = await request.json()
        requests.append(body)
        developer = json.dumps([item for item in body["input"] if item.get("role") == "developer"])
        plan = "The user selected Plan for this chat" in developer
        text = "NATIVE-PLAN-OK" if plan else "NATIVE-GOAL-OK"
        message = {"id": "answer", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}
        if plan:
            assert "Do not edit files" in developer
        else:
            goal_requests += 1
            assert "The user selected Goal for this chat" in developer
            if goal_requests == 2:
                execution = next(tool for tool in body["tools"] if tool["type"] in {"custom", "function"} and tool["name"].endswith("exec"))
                code = 'const goal = ALL_TOOLS.find(tool => tool.name.endsWith("update_goal")); if (!goal) throw new Error("Goal tool missing"); text(await tools[goal.name]({status:"complete"}));'
                message = {"id": "complete-goal", "type": "function_call", "call_id": "complete-goal", "status": "completed", "name": execution["name"], "arguments": json.dumps({"input": code})}
                if execution["type"] == "custom":
                    message.update(type="custom_tool_call", input=code)
                    message.pop("arguments")
        result = {"id": f"response-{len(requests)}", "model": MODEL, "object": "response", "status": "completed", "output": [message], "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "input_tokens_details": {"cached_tokens": 20}, "output_tokens_details": {"reasoning_tokens": 4}}}
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        for value in [{"type": "response.created", "response": {**result, "status": "in_progress", "output": []}}, {"type": "response.output_item.done", "output_index": 0, "item": message}, {"type": "response.completed", "response": result}]:
            await stream.write(("data: " + json.dumps(value) + "\n\n").encode())
        return stream

    upstream = web.Application()
    upstream.router.add_post("/v1/responses", provider)
    async with TestServer(upstream) as server, asyncio.timeout(45):
        with tempfile.TemporaryDirectory(prefix="permissive-chat-modes-", dir="C:/codex-build") as directory:
            home = Path(directory)
            store = Store(home)
            runtime = Runtime(home, native, str(server.make_url("/v1")), ["node", str(Path(__file__).parents[1] / "browser/worker.mjs")])
            try:
                for mode in ("plan", "goal"):
                    task = store.submit({"prompt": "Verify chat " + mode, "workingDirectory": directory, "browserMode": "dedicated", "mode": mode, "goal": "Finish the native goal fixture" if mode == "goal" else ""})[0]
                    store.update(task["id"], status="running")
                    await runtime.run(store.get(task["id"]), store)
                    saved = store.get(task["id"])
                    assert saved["status"] == "completed", saved
                    assert saved["completedAt"] is not None
                    assert saved["tokenUsage"]["inputTokens"] == (100 if mode == "plan" else 300), saved["tokenUsage"]
                    assert saved["tokenUsage"]["cachedInputTokens"] == (20 if mode == "plan" else 60), saved["tokenUsage"]
                    assert saved["tokenUsage"]["reasoningOutputTokens"] == (4 if mode == "plan" else 12), saved["tokenUsage"]
                    if mode == "plan":
                        followup = store.submit({"prompt": "Verify follow-up counters", "workingDirectory": directory, "browserMode": "dedicated", "mode": "plan"})[0]
                        store.update(followup["id"], thread_id=saved["threadId"], status="running")
                        await runtime.run(store.get(followup["id"]), store)
                        usage = store.get(followup["id"])["tokenUsage"]
                        assert usage["inputTokens"] == 100 and usage["totalTokens"] == 110, usage
                    if mode == "goal":
                        assert saved["goal"]["status"] == "complete", saved
                        completed = [event for event in store.events(task["id"]) if event["kind"] == "agent" and event["data"].get("method") == "turn/completed"]
                        assert len(completed) >= 2, completed
                assert goal_requests == 3, goal_requests
                print("Native Plan mode and automatic multi-turn Goal completion passed.")
            finally:
                await runtime.close()
                store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", required=True)
    asyncio.run(main(parser.parse_args().native))
