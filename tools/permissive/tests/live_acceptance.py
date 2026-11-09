"""Manual acceptance against the live service and its configured local model."""

import asyncio
import json
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
from permissive.credentials import default_home
from permissive.client import SignedSession


async def main():
    directory = Path("C:/codex-build/permissive-acceptance")
    directory.mkdir(parents=True, exist_ok=True)
    app = web.Application()

    async def fixture(request):
        return web.Response(
            text='<html><body><input id="name"><button id="go" onclick="document.querySelector(\'p\').textContent=document.querySelector(\'input\').value">Save</button><p>before</p></body></html>',
            content_type="text/html",
        )

    app.router.add_get("/", fixture)
    async with (
        TestServer(app) as site,
        SignedSession(
            timeout=ClientTimeout(total=None, connect=15),
            home=default_home(),
        ) as client,
    ):

        async def request(method, route, body=None):
            async with client.request(
                method, "http://127.0.0.1:8789" + route, json=body
            ) as response:
                if response.status >= 400:
                    raise AssertionError(await response.text())
                return await response.json()

        async def wait(task):
            while task["status"] not in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }:
                await asyncio.sleep(1)
                task = await request("GET", "/v1/tasks/" + task["id"])
            if task["status"] != "completed":
                raise AssertionError(task)
            return task

        code = await request(
            "POST",
            "/v1/tasks",
            {
                "prompt": "Use code execution to write code-ok.txt with the exact text PERMISSIVE-CODE-OK in the working directory. Run a command to read it back and verify the text. Report concise confirmation.",
                "workingDirectory": str(directory),
                "browserMode": "dedicated",
            },
        )
        browser = await request(
            "POST",
            "/v1/tasks",
            {
                "prompt": f"Use the browser tool to open {site.make_url('/')}. Type PERMISSIVE-BROWSER-OK into #name, click #go, inspect the resulting page and verify that text, then save a screenshot artifact. Use actual browser controls for this verification. Report concise confirmation.",
                "workingDirectory": str(directory),
                "browserMode": "dedicated",
            },
        )
        print("Live tasks:", code["id"], browser["id"], flush=True)
        code, browser = await asyncio.gather(wait(code), wait(browser))
        assert (directory / "code-ok.txt").read_text().strip() == "PERMISSIVE-CODE-OK"
        assert browser["artifacts"] and any(
            item["id"].endswith(".png") for item in browser["artifacts"]
        )
        assert "PERMISSIVE-BROWSER-OK" in browser["result"]
        continued = await request(
            "POST",
            "/v1/tasks/" + code["id"] + "/continue",
            {
                "prompt": "Run a command to read code-ok.txt again. Report its exact contents."
            },
        )
        continued = await wait(continued)
        assert (
            continued["threadId"] == code["threadId"]
            and "PERMISSIVE-CODE-OK" in continued["result"]
        )
        print(
            json.dumps(
                {
                    "codeTask": code["id"],
                    "browserTask": browser["id"],
                    "continuedTask": continued["id"],
                    "codeExecution": True,
                    "browserExecution": True,
                    "continuation": True,
                    "artifacts": browser["artifacts"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
