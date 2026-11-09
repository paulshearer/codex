"""Real native pause boundaries and cancellation, using a deterministic provider."""

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import tempfile
import time

from aiohttp import web
from aiohttp.test_utils import TestServer
from signed_client import SignedTestClient, provision
from permissive.runtime import Runtime, MODEL
from permissive.service import Service


async def until(predicate, timeout=30):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


async def main(native):
    requests = []
    prefill = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="permissive-controls-", dir="C:/codex-build") as directory:
        home = Path(directory)
        selected_skill = home / "assigned/SKILL.md"
        selected_skill.parent.mkdir()
        selected_skill.write_text("---\nname: assigned\ndescription: Assigned workspace fixture\n---\nWORKSPACE-ASSIGNED-SKILL", encoding="utf-8")
        unselected_skill = home / ".agents/skills/unselected/SKILL.md"
        unselected_skill.parent.mkdir(parents=True)
        unselected_skill.write_text("---\nname: unselected\ndescription: WORKSPACE-UNSELECTED-SKILL\n---\nDo not inject this skill.", encoding="utf-8")

        async def provider(request):
            body = await request.json()
            effective = json.dumps(body)
            serialized = json.dumps(body["input"])
            scenario = "UPDATE" if "CONTROL-UPDATE" in serialized else next(name for name in ("TOOL", "PREFILL", "CANCEL", "PAUSED-CANCEL") if "CONTROL-" + name in serialized)
            if scenario == "UPDATE":
                developer = json.dumps([item for item in body["input"] if item.get("role") == "developer"])
                assert "WORKSPACE-UPDATED-PROMPT" in developer
                assert "WORKSPACE-SYSTEM-PROMPT" not in developer and "WORKSPACE-ASSIGNED-SKILL" not in developer, "Stale workspace context remained on continuation"
            else:
                assert "WORKSPACE-SYSTEM-PROMPT" in effective, "Workspace prompt was not sent to inference"
                assert "WORKSPACE-ASSIGNED-SKILL" in effective, "Selected skill was not sent to inference"
            assert "WORKSPACE-UNSELECTED-SKILL" not in effective, "Unassigned skill leaked into inference"
            outputs = [item for item in body["input"] if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
            requests.append((scenario, len(outputs)))
            if scenario == "PREFILL" and not outputs:
                await prefill.wait()
            message = {"id": "answer", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "CONTROL-DONE", "annotations": []}]}
            if len(outputs) < 2 and scenario != "UPDATE":
                trace = (home / (scenario + ".txt")).as_posix()
                if not outputs:
                    started = (home / (scenario + ".started")).as_posix()
                    duration = 120 if scenario == "CANCEL" else 2
                    command = f"[IO.File]::WriteAllText('{started}', $PID.ToString()); Start-Sleep -Seconds {duration}; [IO.File]::AppendAllText('{trace}', 'A')"
                    if scenario == "CANCEL":
                        descendant = (home / "CANCEL.descendant").as_posix()
                        command = f"$ownedChild = Start-Process -FilePath (Join-Path $PSHOME 'pwsh.exe') -ArgumentList '-NoProfile -Command Start-Sleep -Seconds 120' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText('{descendant}', $ownedChild.Id.ToString()); " + command
                else:
                    command = f"[IO.File]::AppendAllText('{trace}', 'B')"
                execution = next(tool for tool in body["tools"] if tool["type"] in {"custom", "function"} and tool["name"].endswith("exec"))
                code = 'text(JSON.stringify(await tools.exec_command(' + json.dumps({"cmd": command, "yield_time_ms": 10000}) + ')));'
                message = {"id": f"exec-{len(outputs)}", "type": "function_call", "call_id": f"exec-{len(outputs)}",
                           "status": "completed", "name": execution["name"], "arguments": json.dumps({"input": code})}
                if execution["type"] == "custom":
                    message.update(type="custom_tool_call", input=code)
                    message.pop("arguments")
            result = {"id": f"response-{len(requests)}", "model": MODEL, "object": "response", "status": "completed",
                      "output": [message], "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}
            stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await stream.prepare(request)
            for event in [{"type": "response.created", "response": {**result, "status": "in_progress", "output": []}},
                          {"type": "response.output_item.done", "output_index": 0, "item": message},
                          {"type": "response.completed", "response": result}]:
                await stream.write(("data: " + json.dumps(event) + "\n\n").encode())
            return stream

        upstream = web.Application()
        upstream.router.add_post("/v1/responses", provider)
        async with TestServer(upstream) as server:
            runtime = Runtime(home, native, str(server.make_url("/v1")), ["node", str(Path(__file__).parents[1] / "browser/worker.mjs")])
            signer = provision(home)
            service = Service(home, runtime)
            workspace = service.store.save_workspace({"name": "Native workspace", "workingDirectory": str(home), "systemPrompt": "WORKSPACE-SYSTEM-PROMPT", "skills": [str(selected_skill)]})
            async with SignedTestClient(TestServer(service.app()), signer) as client:
                headers = {"X-Test-Sign": "1"}
                async def post(route, body=None):
                    response = await client.post(route, json=body, headers=headers)
                    assert response.status in {200, 202}, await response.text()
                    return await response.json()
                async def submit(scenario):
                    return await post("/v1/tasks", {"prompt": "CONTROL-" + scenario, "workingDirectory": str(home)})
                def started(task, scenario):
                    current = service.store.get(task["id"])
                    assert current["status"] != "failed", current["error"]
                    return (home / (scenario + ".started")).exists()

                tool = await submit("TOOL")
                await until(lambda: started(tool, "TOOL"))
                route = "/v1/tasks/" + tool["id"]
                assert (await post(route + "/pause"))["status"] == "pausing"
                await until(lambda: service.store.get(tool["id"])["status"] == "paused")
                thread = service.store.get(tool["id"])["threadId"]
                assert (home / "TOOL.txt").read_text() == "A"
                await asyncio.sleep(0.3)
                assert [count for name, count in requests if name == "TOOL"] == [0]
                await post(route + "/resume")
                await until(lambda: service.store.get(tool["id"])["status"] == "completed")
                assert (home / "TOOL.txt").read_text() == "AB"
                assert service.store.get(tool["id"])["threadId"] == thread

                service.store.save_workspace({"systemPrompt": "WORKSPACE-UPDATED-PROMPT", "skills": []}, workspace["id"])
                followup = await post(route + "/continue", {"prompt": "CONTROL-UPDATE"})
                await until(lambda: service.store.get(followup["id"])["status"] == "completed")
                assert service.store.get(followup["id"])["threadId"] == thread
                assert any(name == "UPDATE" for name, _ in requests)
                service.store.save_workspace({"systemPrompt": "WORKSPACE-SYSTEM-PROMPT", "skills": [str(selected_skill)]}, workspace["id"])

                inference = await submit("PREFILL")
                await until(lambda: ("PREFILL", 0) in requests)
                route = "/v1/tasks/" + inference["id"]
                await post(route + "/pause")
                prefill.set()
                await until(lambda: service.store.get(inference["id"])["status"] == "paused")
                assert not (home / "PREFILL.started").exists(), "A new command started while paused"
                await post(route + "/resume")
                await until(lambda: service.store.get(inference["id"])["status"] == "completed")
                assert (home / "PREFILL.txt").read_text() == "AB"

                cancelled = await submit("CANCEL")
                await until(lambda: (home / "CANCEL.started").exists())
                await until(lambda: (home / "CANCEL.started").stat().st_size > 0)
                pid = int((home / "CANCEL.started").read_text())
                descendant_pid = int((home / "CANCEL.descendant").read_text())
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                process = kernel.OpenProcess(0x100000, False, pid)
                descendant_process = kernel.OpenProcess(0x100000, False, descendant_pid)
                assert process, "The command exited before cancellation"
                assert descendant_process, "The grandchild exited before cancellation"
                try:
                    began = time.monotonic()
                    result = await post("/v1/tasks/" + cancelled["id"] + "/cancel")
                    elapsed = time.monotonic() - began
                    assert result["status"] == "cancelled"
                    assert kernel.WaitForSingleObject(process, 5000) == 0, "Command survived cancellation"
                    assert kernel.WaitForSingleObject(descendant_process, 5000) == 0, "Grandchild survived cancellation"
                    assert elapsed < 6, elapsed
                finally:
                    kernel.CloseHandle(process)
                    kernel.CloseHandle(descendant_process)
                assert not (home / "CANCEL.txt").exists()

                parked = await submit("PAUSED-CANCEL")
                await until(lambda: (home / "PAUSED-CANCEL.started").exists())
                route = "/v1/tasks/" + parked["id"]
                await post(route + "/pause")
                await until(lambda: service.store.get(parked["id"])["status"] == "paused")
                assert (await post(route + "/cancel"))["status"] == "cancelled"
                assert (home / "PAUSED-CANCEL.txt").read_text() == "A"
                assert parked["id"] not in runtime.workers and parked["id"] not in runtime.controls
                evidence = {"toolPauseFinishesCurrentCommand": True, "resumeExecutesNextCommandOnce": True,
                            "inferencePauseBlocksNextTool": True, "cancelTerminatesRealCommandPid": pid,
                            "cancelSeconds": round(elapsed, 3), "pausedTaskCancellation": True, "sameThreadOnResume": True}
                evidence["cancelTerminatesGrandchildPid"] = descendant_pid
                evidence["workspaceId"] = workspace["id"]
                evidence["workspacePromptAndAssignedSkillsReachedInference"] = True
                evidence["unselectedSkillsExcluded"] = True
                evidence["updatedContextReplacesStaleNativeContinuationInstructions"] = True
                Path("C:/codex-build/permissive-task-controls-native-verification.json").write_text(json.dumps(evidence, indent=2))
                print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", required=True)
    asyncio.run(main(parser.parse_args().native))
