"""Verify live installed code, patch, browser, and image tools in one conversation."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import uuid

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
from permissive.credentials import default_home
from permissive.client import SignedSession


async def main():
    marker = "PERMISSIVE-COMBINED-" + uuid.uuid4().hex[:12]
    directory = Path("C:/codex-build/permissive-acceptance") / marker
    directory.mkdir(parents=True)
    observed = []
    fixture = web.Application()

    async def page(request):
        return web.Response(
            content_type="text/html",
            text="""<html><body><h1>Permissive tool verification</h1>
<form action='/verify' method='post'><input id='value' name='value'>
<button id='save'>Save</button></form></body></html>""",
        )

    async def verify(request):
        value = (await request.post())["value"]
        observed.append(value)
        return web.Response(
            content_type="text/html",
            text="<html><body><p>" + value + "</p></body></html>",
        )

    fixture.router.add_get("/", page)
    fixture.router.add_post("/verify", verify)
    async with (
        TestServer(fixture) as site,
        SignedSession(
            timeout=ClientTimeout(total=None, connect=15),
            home=default_home(),
        ) as client,
    ):

        async def request(method, route, body=None):
            async with client.request(
                method, "http://127.0.0.1:8789" + route, json=body
            ) as response:
                assert response.status < 400, await response.text()
                return await response.json()

        task = await request(
            "POST",
            "/v1/tasks",
            {
                "workingDirectory": str(directory),
                "browserMode": "dedicated",
                "prompt": (
                    "Run this tool acceptance case using real tools, in order. "
                    "1. Use code execution to create evidence.txt containing SEED followed by a newline. "
                    f"2. Use the dedicated apply_patch tool to replace SEED with {marker}; retain the newline. "
                    "In the JavaScript execution tool use this exact native call: "
                    + "text(await tools.apply_patch("
                    + json.dumps(
                        f"*** Begin Patch\n*** Update File: evidence.txt\n@@\n-SEED\n+{marker}\n*** End Patch\n"
                    )
                    + "));. Do not run apply_patch or --codex-run-as-apply-patch in the terminal. "
                    + "3. Use code execution to read the file and verify the replacement. "
                    f"4. Use the browser tool to open {site.make_url('/')}, type the verified marker "
                    "into #value, click #save, and inspect the resulting page. Save a screenshot artifact. "
                    "Use actual browser controls, without using an HTTP client to submit the form. "
                    "5. Use the native view_image tool on the saved screenshot's local path. "
                    "6. Report the marker and concise confirmation that all five steps succeeded."
                ),
            },
        )
        print("Combined live task:", task["id"], flush=True)
        while task["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
            await asyncio.sleep(1)
            task = await request("GET", "/v1/tasks/" + task["id"])
        assert task["status"] == "completed", task
        assert (directory / "evidence.txt").read_text() == marker + "\n"
        assert observed == [marker], observed

        # Native events independently identify the tools; the result text alone
        # cannot prove execution. Read the live database without opening Store,
        # whose startup intentionally interrupts unfinished jobs after a crash.
        with sqlite3.connect(
            f"file:{(default_home() / 'tasks.sqlite').as_posix()}?mode=ro", uri=True
        ) as db:
            events = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT data FROM events WHERE task_id=? AND kind='agent' ORDER BY id",
                    (task["id"],),
                )
            ]
        items = [
            event["params"]["item"]
            for event in events
            if event.get("method") == "item/completed"
        ]
        commands = [item for item in items if item["type"] == "commandExecution"]
        patches = [item for item in items if item["type"] == "fileChange"]
        images = [item for item in items if item["type"] == "imageView"]
        browsers = [
            item
            for item in items
            if item["type"] == "mcpToolCall" and item["server"] == "browser"
        ]
        assert commands and all(item["exitCode"] == 0 for item in commands), commands
        shell = (
            (
                Path(os.environ["LOCALAPPDATA"])
                / "Programs/CodexPermissive/1.0.0/powershell/pwsh.exe"
            )
            .as_posix()
            .lower()
        )
        assert all(
            shell in item["command"].replace("\\", "/").replace("//", "/").lower()
            for item in commands
        ), "Execution did not use the standalone package's PowerShell"
        assert patches and all(item["status"] == "completed" for item in patches), (
            patches
        )
        assert images, "Native image-view tool was not invoked"
        operations = {
            item["arguments"].get("operation")
            for item in browsers
            if item["status"] == "completed" and not item.get("error")
        }
        assert {"open", "type", "click", "inspect", "screenshot"} <= operations, (
            browsers
        )
        screenshots = [
            item for item in task["artifacts"] if item["id"].endswith(".png")
        ]
        assert screenshots, "Screenshot artifact was not exported"
        async with client.get(
            "http://127.0.0.1:8789/v1/tasks/"
            + task["id"]
            + "/artifacts/"
            + screenshots[0]["id"]
        ) as response:
            assert response.status == 200
            assert (await response.read()).startswith(b"\x89PNG\r\n\x1a\n")
        report = {
            "taskId": task["id"],
            "status": task["status"],
            "marker": marker,
            "workingDirectory": str(directory),
            "commandExecutions": len(commands),
            "standaloneShell": shell,
            "nativePatches": len(patches),
            "imageViews": len(images),
            "browserOperations": sorted(operations),
            "observedBrowserSubmissions": observed,
            "screenshotArtifacts": screenshots,
        }
        (directory / "verification.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
