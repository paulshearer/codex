"""Console and stdio MCP entry point; every task goes through the same API."""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path, PureWindowsPath
import sys

from aiohttp import ClientSession, ClientTimeout, web
from .credentials import default_home
from .client import SignedSession
from .privacy import INFERENCE_ENDPOINT, http_server_options


def package_root():
    if os.environ.get('PERMISSIVE_PACKAGE'):
        return Path(os.environ['PERMISSIVE_PACKAGE'])
    for candidate in (Path(__file__).parents[2], Path(__file__).parents[1]):
        if (candidate / 'windows').is_dir():
            return candidate
    return Path(__file__).parents[2]


async def request(args, method, path, body=None):
    async with SignedSession(home=args.home, private_key=args.private_key) as client:
        # A pre-VM installation may still own this port. Verify the execution
        # role before sending a mutation; never hand it new host-execution work.
        if path not in {'/v1/health', '/v1/service/shutdown'}:
            binding_path = Path(os.environ.get('PERMISSIVE_DEPLOYMENT', package_root() / 'deployment.json'))
            binding = json.loads(binding_path.read_text(encoding='utf-8-sig')) if binding_path.is_file() else {}
            expected_role = binding.get('role', 'controller')
            async with client.get(args.url + '/v1/health') as response:
                if response.status >= 400:
                    raise RuntimeError('Cannot verify the execution service before dispatch')
                health = await response.json()
            if health.get('executionRole') != expected_role:
                raise RuntimeError('The API belongs to another execution role; stop the previous service and start the bound controller')
            if expected_role == 'controller' and binding.get('vmId') and health.get('vmId') != binding['vmId']:
                raise RuntimeError('The running controller belongs to another VM deployment')
        async with client.request(method, args.url + path, json=body) as response:
            if response.status >= 400:
                raise RuntimeError(await response.text())
            return await response.json()


async def mcp(args):
    tools = [
        {
            "name": "submit",
            "description": "Delegate work to DeepSeek inside the bound Windows VM. Working directories are guest paths. Returns a task ID immediately.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "workingDirectory": {"type": "string", "description": "Absolute local Windows path inside the guest; host directories are never mounted."},
                    "workspaceId": {"type": "string"},
                    "browserMode": {"enum": ["dedicated", "attached"]},
                    "browser": {"enum": ["chromium", "chrome", "msedge"]},
                    "browserProfile": {"type": "string"},
                },
                "required": ["prompt"],
                "anyOf": [{"required": ["workspaceId"]}, {"required": ["workingDirectory"]}],
                "additionalProperties": False,
            },
        },
        *[
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "taskId": {"type": "string"},
                        **(
                            {"prompt": {"type": "string"}} if name == "continue" else {}
                        ),
                        **(
                            {"offset": {"type": "integer", "minimum": 0}}
                            if name == "result"
                            else {}
                        ),
                    },
                    "required": ["taskId", *(["prompt"] if name == "continue" else [])],
                    "additionalProperties": False,
                },
            }
            for name, description in [
                ("status", "Read delegated task status"),
                ("result", "Read result and artifact references"),
                ("cancel", "Cancel delegated work"),
                ("pause", "Finish the current activity, then pause delegated work"),
                ("unpause", "Resume a paused delegated task"),
                ("continue", "Continue a saved delegated conversation"),
            ]
        ],
        {"name": "workspaces", "description": "List workspace IDs and defaults for delegation. Use workspaceId when submitting work.",
         "inputSchema": {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
        {"name": "save_workspace", "description": "Create or update workspace instructions and assigned SKILL.md files. Changes apply to subsequent tasks.",
         "inputSchema": {"type": "object", "properties": {"workspaceId": {"type": "string"}, "name": {"type": "string"},
             "workingDirectory": {"type": "string"}, "systemPrompt": {"type": "string"}, "skills": {"type": "array", "items": {"type": "string"}},
             "browserMode": {"enum": ["dedicated", "attached"]}, "browser": {"enum": ["chromium", "chrome", "msedge"]},
             "browserProfile": {"type": "string"}}, "additionalProperties": False}},
    ]
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        message = json.loads(line)
        if "id" not in message:
            continue
        try:
            method, params = message.get("method"), message.get("params", {})
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codex-permissive", "version": "1.0.0"},
                }
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "ping":
                result = {}
            elif method == "tools/call":
                name, value = params["name"], params.get("arguments", {})
                if name == "submit":
                    result = await request(args, "POST", "/v1/tasks", value)
                elif name == "workspaces":
                    result = await request(args, "GET", "/v1/workspaces")
                elif name == "save_workspace":
                    workspace_id = value.get("workspaceId")
                    result = await request(args, "PATCH" if workspace_id else "POST", "/v1/workspaces" + ("/" + workspace_id if workspace_id else ""), {key: item for key, item in value.items() if key != "workspaceId"})
                elif name in {"status", "result", "cancel", "pause", "unpause", "continue"}:
                    suffix = "/resume" if name == "unpause" else "/" + name if name in {"cancel", "pause", "continue"} else ""
                    result = await request(
                        args,
                        "POST" if suffix else "GET",
                        "/v1/tasks/" + value["taskId"] + suffix,
                        {"prompt": value["prompt"]} if name == "continue" else None,
                    )
                else:
                    raise ValueError("Unknown tool")
                offset = value.get("offset", 0)
                if type(offset) is not int or offset < 0:
                    raise ValueError("offset must be a nonnegative integer")
                if name in {"workspaces", "save_workspace"}:
                    def workspace_summary(item):
                        return {"workspaceId": item["id"], "name": item["name"], "workingDirectory": item["workingDirectory"], "skillCount": len(item["skills"]), "hasSystemPrompt": bool(item["systemPrompt"]), "revision": item["revision"]}
                    if name == "workspaces":
                        rows = [workspace_summary(item) for item in result["data"][offset:offset + 10]]
                        projected = {"data": rows, "nextOffset": None}
                        while len(json.dumps(projected, ensure_ascii=False).encode()) > 850 and len(rows) > 1:
                            rows.pop()
                        projected["nextOffset"] = offset + len(rows) if offset + len(rows) < len(result["data"]) else None
                    else:
                        projected = workspace_summary(result)
                    encoded = json.dumps(projected, ensure_ascii=False)
                    output = {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": encoded}]}}
                    print(json.dumps(output, ensure_ascii=False), flush=True)
                    continue
                text = result.get("result") or result.get("error") or ""
                page = text[offset : offset + 600]
                projected = {
                    "taskId": result["id"],
                    "status": result["status"],
                    "workspaceId": result["workspaceId"],
                    "chatId": result["threadId"],
                    "text": page,
                    "nextOffset": offset + len(page)
                    if offset + len(page) < len(text)
                    else None,
                    "artifactCount": len(result.get("artifacts", [])),
                }
                if name == "result":
                    projected["artifacts"] = [
                        {
                            "id": item["id"],
                            "path": "/v1/tasks/"
                            + result["id"]
                            + "/artifacts/"
                            + item["id"],
                        }
                        for item in result.get("artifacts", [])
                        if re.fullmatch(r"[\w.-]{1,128}", item["id"], flags=re.ASCII)
                    ][:1]
                encoded = json.dumps(projected, ensure_ascii=False)
                while len(encoded.encode()) > 900:
                    if not page:
                        raise ValueError('Task metadata exceeds the MCP output budget; retrieve it through the signed task API')
                    page = page[:-20]
                    projected.update(text=page, nextOffset=offset + len(page))
                    encoded = json.dumps(projected, ensure_ascii=False)
                result = {"content": [{"type": "text", "text": encoded}]}
            else:
                raise ValueError("Unknown method")
            output = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except Exception as error:
            output = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32000, "message": str(error)[:200]},
            }
        print(json.dumps(output, ensure_ascii=False), flush=True)


async def command(args):
    if args.command == "chat":
        task = None
        print("Codex Permissive · DeepSeek V4.1 Flash · Sparky. Type /quit to exit.")
        while True:
            try:
                prompt = await asyncio.to_thread(input, "You> ")
            except EOFError:
                return
            if prompt.strip() == "/quit":
                return
            if not prompt.strip():
                continue
            path = (
                "/v1/tasks/" + task["id"] + "/continue"
                if task and task.get("threadId")
                else "/v1/tasks"
            )
            body = (
                {"prompt": prompt}
                if path.endswith("/continue")
                else {
                    "prompt": prompt,
                    **submission_options(args),
                }
            )
            task = await wait_task(args, await request(args, "POST", path, body))
        return
    if args.command == "stop":
        print(json.dumps(await request(args, "POST", "/v1/service/shutdown")))
        return
    if args.command == "workspaces":
        print(json.dumps(await request(args, "GET", "/v1/workspaces"), indent=2))
        return
    if args.command == "mcp":
        await mcp(args)
        return
    if args.command == "status":
        print(
            json.dumps(
                await request(args, "GET", "/v1/tasks/" + args.task_id), indent=2
            )
        )
        return
    if args.command in {"cancel", "pause", "unpause"}:
        operation = "resume" if args.command == "unpause" else args.command
        print(
            json.dumps(
                await request(args, "POST", "/v1/tasks/" + args.task_id + "/" + operation)
            )
        )
        return
    body = {"prompt": args.prompt}
    path = (
        "/v1/tasks/" + args.task_id + "/continue"
        if args.command == "resume"
        else "/v1/tasks"
    )
    if args.command == "exec":
        body.update(submission_options(args))
    task = await request(args, "POST", path, body)
    task = await wait_task(args, task)
    if task["status"] != "completed":
        raise SystemExit(1)


async def wait_task(args, task):
    print("Task " + task["id"], file=sys.stderr)
    while task["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
        await asyncio.sleep(1)
        task = await request(args, "GET", "/v1/tasks/" + task["id"])
    print(task.get("result") or task.get("error") or task["status"])
    return task


def submission_options(args):
    value = {}
    if args.workspace:
        value["workspaceId"] = args.workspace
    if args.cwd or not args.workspace:
        directory = PureWindowsPath(args.cwd or r'C:\workspaces')
        if not directory.is_absolute() or not directory.drive or str(directory).startswith('\\\\'):
            raise ValueError('Working directory must be an absolute local Windows guest path')
        value["workingDirectory"] = str(directory)
    if args.browser:
        value["browserMode"] = args.browser
    if args.browser_type:
        value["browser"] = args.browser_type
    if args.browser_profile is not None:
        value["browserProfile"] = args.browser_profile
    return value


def main():
    parser = argparse.ArgumentParser(prog="codex-permissive")
    default_binding = Path(os.environ.get('PERMISSIVE_DEPLOYMENT', package_root() / 'deployment.json'))
    try:
        defaults = json.loads(default_binding.read_text(encoding='utf-8-sig')) if default_binding.is_file() else {}
    except (OSError, ValueError):
        defaults = {}  # The serve/dispatch path reports invalid bindings before execution.
    bound_home = Path(defaults['controllerHome']) if defaults.get('role') == 'controller' and defaults.get('controllerHome') else default_home()
    bound_url = 'http://127.0.0.1:' + str(defaults.get('controllerPort', 8789))
    parser.add_argument(
        "--version",
        action="version",
        version="Codex Permissive 1.0.0 (native 0.162.1; DeepSeek V4.1 Flash)",
    )
    parser.add_argument("--home", type=Path, default=default_home() if os.environ.get('PERMISSIVE_HOME') else bound_home)
    parser.add_argument("--private-key", help="Client Ed25519 private key; defaults to PERMISSIVE_PRIVATE_KEY or the local protected client key")
    parser.add_argument("--url", default=os.environ.get('PERMISSIVE_API_URL', bound_url))
    subs = parser.add_subparsers(dest="command", required=True)
    authentication = subs.add_parser("auth", help="Generate client keys or install/revoke server public keys")
    authentication.add_argument("auth_args", nargs=argparse.REMAINDER)
    serve = subs.add_parser("serve")
    serve.add_argument("--native")
    serve.add_argument('--role', choices=['controller', 'guest'], default='controller')
    serve.add_argument('--deployment', type=Path, default=os.environ.get('PERMISSIVE_DEPLOYMENT'))
    serve.add_argument("--endpoint", default=INFERENCE_ENDPOINT)
    serve.add_argument("--node", default="node")
    serve.add_argument(
        "--browser-worker",
        default=str(package_root() / "browser/worker.mjs"),
    )
    serve.add_argument("--port", type=int, default=8789)
    environment = subs.add_parser('environment', help='Manage the external Hyper-V execution environment')
    environment_subs = environment.add_subparsers(dest='environment_action', required=True)
    for operation in ('status', 'pause', 'resume', 'reset'):
        environment_subs.add_parser(operation)
    export = environment_subs.add_parser('export')
    export.add_argument('--output', required=True)
    export.add_argument('--deployment', type=Path, required=True)
    imported = environment_subs.add_parser('import')
    imported.add_argument('--bundle', required=True)
    imported.add_argument('--root', required=True)
    imported.add_argument('--package', type=Path, default=package_root())
    for name in ["exec", "chat", "resume", "status", "cancel", "pause", "unpause", "mcp", "stop", "workspaces"]:
        sub = subs.add_parser(name)
        if name in {"resume", "status", "cancel", "pause", "unpause"}:
            sub.add_argument("task_id")
        if name in {"exec", "resume"}:
            sub.add_argument("prompt")
        if name in {"exec", "chat"}:
            sub.add_argument("--cwd")
            sub.add_argument("--workspace", help="Workspace ID; uses its directory and browser defaults")
            sub.add_argument(
                "--browser", choices=["dedicated", "attached"]
            )
            sub.add_argument("--browser-type", choices=["chromium", "chrome", "msedge"])
            sub.add_argument("--browser-profile", help="Named dedicated profile or connected extension profile ID")
    args = parser.parse_args()
    if args.command == "auth":
        from .auth import main as authenticate
        authenticate(["--home", str(args.home), *args.auth_args])
        return
    if args.command == 'environment':
        if args.environment_action in {'export', 'import'}:
            import subprocess
            root = package_root()
            if args.environment_action == 'export':
                asyncio.run(request(args, 'POST', '/v1/environment/export'))
            script = root / ('windows/sandbox/' + ('Export-Sandbox.ps1' if args.environment_action == 'export' else 'Import-Sandbox.ps1'))
            parameters = (['-BindingPath', str(args.deployment), '-OutputPath', args.output] if args.environment_action == 'export' else
                          ['-BundlePath', args.bundle, '-RootPath', args.root, '-PackageDirectory', str(args.package)])
            raise SystemExit(subprocess.call(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script), *parameters]))
        async def environment_request():
            action = args.environment_action
            value = await request(args, 'GET' if action == 'status' else 'POST', '/v1/environment' + ('' if action == 'status' else '/' + action))
            print(json.dumps(value, indent=2))
        asyncio.run(environment_request())
        return
    if args.command == "serve":
        from .service import Service
        from .vm_runtime import deployment
        configured = Path(args.deployment) if args.deployment else package_root() / 'deployment.json'
        if not configured.is_file():
            parser.error('An explicit deployment.json is required; local execution is never a controller fallback')
        settings = deployment(configured)
        if args.role != settings['role']:
            parser.error('Requested role does not match deployment.json')

        args.home.mkdir(parents=True, exist_ok=True)
        # An OS-held lock survives neither process exit nor a crash. A second
        # launcher must not open the database and interrupt an existing service.
        with (args.home / "service.lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt

                if lock.seek(0, os.SEEK_END) == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.role == 'controller':
                import ctypes
                if os.name != 'nt' or not ctypes.windll.shell32.IsUserAnAdmin():
                    parser.error('The Hyper-V controller requires its registered elevated host task; start it through the desktop or an elevated Windows PowerShell session')
                from .vm_runtime import VMRuntime
                runtime = VMRuntime(args.home, configured)
            else:
                from .runtime import Runtime
                package = Path(settings.get('packagePath', package_root()))
                gateway = settings.get('inferenceEndpoint')
                if settings.get('relayCertificatePath'):
                    os.environ['SSL_CERT_FILE'] = settings['relayCertificatePath']
                runtime = Runtime(args.home, args.native or package / 'native/codex-app-server.exe',
                    gateway or args.endpoint, [args.node, args.browser_worker], gateway=gateway)

            async def serve():
                service = Service(args.home, runtime)
                runner = web.AppRunner(service.app(), handler_cancellation=True, **http_server_options())
                relay = None
                try:
                    if args.role == 'controller' and settings.get('relayTls'):
                        from .inference_relay import start_relay
                        relay = await start_relay(settings)
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", args.port).start()
                    await service.stopped.wait()
                finally:
                    await runner.cleanup()
                    if relay:
                        await relay.cleanup()

            asyncio.run(serve())
    else:
        asyncio.run(command(args))


if __name__ == "__main__":
    main()
