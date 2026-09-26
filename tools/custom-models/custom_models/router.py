"""Stdio app-server routing without changing native action permissions."""

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from dataclasses import dataclass, field


_PASSIVE = {
    "thread/read",
    "thread/turns/list",
    "thread/items/list",
    "thread/timeline/list",
    "turn/interrupt",
}
_RESUME_KEYS = {
    "cwd",
    "approvalPolicy",
    "approvalsReviewer",
    "permissions",
    "sandbox",
    "runtimeWorkspaceRoots",
    "baseInstructions",
    "developerInstructions",
    "personality",
}
_MODEL_KEYS = {"model", "fromModel", "toModel", "fasterModel"}
_CONTENT_KEYS = {"input", "content", "output", "arguments", "tools", "text", "config"}


def _toml_value(value):
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                json.dumps(key) + " = " + _toml_value(item)
                for key, item in value.items()
            )
            + " }"
        )
    return json.dumps(value)


class RpcError(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__(error.get("message", "Native backend RPC failed"))


class Backend:
    def __init__(self, router, label, model=None):
        self.router, self.label, self.model = router, label, model
        self.pending = {}
        self.sequence = 0
        self.proc = None
        self.reader = None
        self.ready = False
        self.closed = False

    async def start(self):
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.router.home)
        environment.pop("CODEX_CLI_PATH", None)
        environment.pop("CODEX_THREAD_ID", None)
        arguments = [str(self.router.native_cli)]
        forwarded = list(self.router.arguments)
        generated = []
        if self.model:
            provider = self.router.registry.provider(self.model.provider)
            for key in {
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_ORG_ID",
                "OPENAI_ORGANIZATION",
                "OPENAI_PROJECT_ID",
                provider.env_key,
                *provider.env_http_headers.values(),
            }:
                if key:
                    environment.pop(key, None)
            for key, value in self.router.overrides(self.model).items():
                generated += ["-c", key + "=" + _toml_value(value)]
        insertion = forwarded.index("--") if "--" in forwarded else len(forwarded)
        arguments += forwarded[:insertion] + generated + forwarded[insertion:]
        self.lines = asyncio.Queue()
        self.write_lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        self.proc = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        def pump():
            try:
                for line in self.proc.stdout:
                    loop.call_soon_threadsafe(self.lines.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(self.lines.put_nowait, None)

        threading.Thread(target=pump, daemon=True).start()
        self.reader = asyncio.create_task(self.read())
        result = await self.call("initialize", self.router.initialize_params)
        await self.send({"method": "initialized"})
        self.ready = True
        return result

    async def send(self, message):
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        async with self.write_lock:
            await asyncio.to_thread(self._write, encoded)

    def _write(self, encoded):
        self.proc.stdin.write(encoded)
        self.proc.stdin.flush()

    async def call(self, method, params):
        self.sequence += 1
        ident = f"companion:{self.label}:{self.sequence}"
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"id": ident, "method": method, "params": params})
            response = await asyncio.wait_for(future, 90)
        finally:
            self.pending.pop(ident, None)
        if "error" in response:
            raise RpcError(response["error"])
        return response["result"]

    async def read(self):
        try:
            while (line := await self.lines.get()) is not None:
                message = json.loads(line)
                if "method" not in message:
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        future.set_result(message)
                elif "id" in message:
                    ident = self.router.track_callback(
                        self, message["id"], message.get("params", {}).get("threadId")
                    )
                    self.router.emit(
                        self.router.external({**message, "id": ident}, self.model)
                    )
                elif self.ready and not self.closed:
                    self.router.observe(self, message)
                    params = message.get("params", {})
                    if message["method"] == "serverRequest/resolved":
                        params["requestId"] = self.router.resolve_callback(
                            self, params.get("requestId")
                        )
                    if not (
                        message["method"] in {"thread/archived", "thread/unarchived"}
                        and params.get("threadId") in self.router.suppress_archive
                    ):
                        self.router.emit(self.router.external(message, self.model))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Native backend exited"))

    async def close(self):
        self.closed = True
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                await asyncio.wait_for(asyncio.to_thread(self.proc.wait), 10)
            except asyncio.TimeoutError:
                self.proc.terminate()
                await asyncio.wait_for(asyncio.to_thread(self.proc.wait), 10)
            if self.reader:
                await self.reader


@dataclass
class ThreadState:
    tid: str
    model: object = None
    child: object = None
    active: bool = False
    turn_id: str = None
    resolved: bool = False
    resume_options: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    lock: object = field(default_factory=asyncio.Lock)
    compact_waiter: object = None


class Router:
    def __init__(
        self,
        native_cli,
        registry,
        home,
        facade_urls,
        facade_tokens=None,
        arguments=None,
    ):
        self.native_cli, self.registry, self.home = (
            Path(native_cli),
            registry,
            Path(home),
        )
        self.facade_urls, self.facade_tokens = (
            dict(facade_urls),
            dict(facade_tokens or {}),
        )
        self.arguments = list(arguments or ["app-server", "--listen", "stdio://"])
        if "app-server" not in self.arguments:
            raise ValueError("Router requires app-server arguments")
        listener = (
            self.arguments.index("--listen") if "--listen" in self.arguments else None
        )
        if listener is not None and (
            listener + 1 >= len(self.arguments)
            or self.arguments[listener + 1] != "stdio://"
        ):
            raise ValueError("Custom model router supports stdio:// only")
        if any(
            argument.startswith("--listen=") and argument != "--listen=stdio://"
            for argument in self.arguments
        ):
            raise ValueError("Custom model router supports stdio:// only")
        self.main = None
        self.initialize_params = {}
        self.threads, self.callbacks, self.callback_routes, self.callback_threads = (
            {},
            {},
            {},
            {},
        )
        self.callback_sequence = 0
        self.suppress_archive = set()
        self.selection_path = self.home / "custom-models" / "selections.json"
        self.selections = self.registry.load_selections(self.home)
        self.backend_factory = Backend

    def state(self, tid):
        return self.threads.setdefault(tid, ThreadState(tid))

    def emit(self, message):
        print(json.dumps(message, separators=(",", ":")), flush=True)

    def external(self, value, model):
        if isinstance(value, list):
            return [self.external(item, model) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        provider = value.get("modelProvider", model.provider if model else None)
        for key, item in value.items():
            if key in _MODEL_KEYS and isinstance(item, str):
                matches = [
                    candidate
                    for candidate in self.registry.models
                    if candidate.provider == provider
                    and candidate.upstream_model == item
                ]
                result[key] = matches[0].alias if len(matches) == 1 else item
            else:
                result[key] = (
                    copy.deepcopy(item)
                    if key in _CONTENT_KEYS
                    else self.external(item, model)
                )
        return result

    def overrides(self, model):
        result = self.registry.native_config(
            model,
            self.facade_urls[model.provider],
            self.facade_tokens.get(model.provider),
        )
        catalog = self.home / "catalogs" / f"{model.provider}-{model.id}.json"
        encoded = json.dumps(self.registry.native_catalog(model))
        if not catalog.exists() or catalog.read_text(encoding="utf-8") != encoded:
            catalog.parent.mkdir(parents=True, exist_ok=True)
            temporary = catalog.with_suffix(".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(catalog)
        result["model_catalog_json"] = str(catalog)
        return result

    def native_params(self, params, model):
        result = copy.deepcopy(params)
        config = result.get("config") if isinstance(result.get("config"), dict) else {}
        provider_key = f"model_providers.{model.provider}"
        prefix = provider_key + "."
        entry = {}
        for key, value in self.overrides(model).items():
            if key == provider_key:
                entry = copy.deepcopy(value)
            else:
                config[key] = value
        providers = copy.deepcopy(config.get("model_providers", {}))
        providers[model.provider] = entry
        config["model_providers"] = providers
        for key in list(config):
            if key == provider_key or key.startswith(prefix):
                del config[key]
        result["config"] = config
        if "model" in result:
            result["model"] = model.upstream_model
        if "modelProvider" in result:
            result["modelProvider"] = model.provider
        if (
            result.get("effort") is not None
            and result["effort"] not in model.reasoning_efforts
        ):
            result["effort"] = model.default_reasoning_effort
        mode = result.get("collaborationMode")
        if isinstance(mode, dict):
            mode.setdefault("settings", {})["model"] = model.upstream_model
            effort = mode["settings"].get("reasoning_effort")
            if effort not in model.reasoning_efforts:
                mode["settings"]["reasoning_effort"] = model.default_reasoning_effort
        return result

    def default_params(self, params):
        result = copy.deepcopy(params)
        config = result.get("config")
        if isinstance(config, dict):
            if config.get("model_provider") in self.registry.providers:
                config.pop("model_provider")
                if any(
                    config.get("model") in {model.alias, model.upstream_model}
                    for model in self.registry.models
                ):
                    config.pop("model", None)
            providers = config.get("model_providers")
            if isinstance(providers, dict):
                config["model_providers"] = {
                    key: value
                    for key, value in providers.items()
                    if key not in self.registry.providers
                }
            for key in list(config):
                if any(
                    key.startswith(f"model_providers.{provider}.")
                    for provider in self.registry.providers
                ):
                    del config[key]
        return result

    @staticmethod
    def adopt_settings(st, result):
        for key in (
            "model",
            "modelProvider",
            "cwd",
            "approvalPolicy",
            "approvalsReviewer",
            "activePermissionProfile",
            "serviceTier",
            "personality",
            "disabledPluginIds",
        ):
            if key in result:
                st.settings[key] = copy.deepcopy(result[key])
        if "sandbox" in result:
            st.settings["sandboxPolicy"] = copy.deepcopy(result["sandbox"])
        if "reasoningEffort" in result:
            st.settings["effort"] = result["reasoningEffort"]

    def track_callback(self, child, original, thread_id=None):
        self.callback_sequence += 1
        ident = f"custom-callback:{self.callback_sequence}"
        self.callbacks[ident] = (child, original)
        self.callback_routes[ident] = (child, original)
        self.callback_threads[ident] = thread_id
        return ident

    def resolve_callback(self, child, original):
        for ident, route in list(self.callback_routes.items()):
            if route == (child, original):
                self.callbacks.pop(ident, None)
                self.callback_routes.pop(ident, None)
                self.callback_threads.pop(ident, None)
                return ident
        return original

    def observe(self, child, message):
        params = message.get("params", {})
        st = self.threads.get(params.get("threadId"))
        if st is None or child is not (st.child or self.main):
            return
        if message["method"] == "turn/started":
            st.active = True
            st.turn_id = params.get("turn", {}).get("id")
        elif message["method"] == "turn/completed":
            st.active = False
            st.turn_id = None
            if st.compact_waiter is not None and not st.compact_waiter.done():
                st.compact_waiter.set_result(params["turn"])
        elif message["method"] == "thread/settings/updated":
            st.settings = copy.deepcopy(params["threadSettings"])

    def remember(self, st):
        self.selections = self.registry.save_selection(
            self.home, st.tid, st.model.alias if st.model else None
        )

    def guard_switch(self, st):
        owner = st.child or self.main
        pending = any(
            child is owner and self.callback_threads.get(ident) in {None, st.tid}
            for ident, (child, _) in self.callbacks.items()
        )
        if st.active or pending:
            raise RuntimeError(
                "Finish or cancel the active turn and resolve pending tool requests before switching models"
            )

    async def saved_model(self, st):
        if not st.resolved:
            self.selections = self.registry.load_selections(self.home)
            alias = self.selections.get(st.tid)
            if alias:
                st.model = self.registry.model(alias)
            else:
                saved = await self.main.call(
                    "thread/read", {"threadId": st.tid, "includeTurns": False}
                )
                thread = saved.get("thread", {})
                matches = [
                    model
                    for model in self.registry.models
                    if model.provider == thread.get("modelProvider")
                    and (
                        thread.get("model") is None
                        or model.upstream_model == thread.get("model")
                    )
                ]
                if len(matches) == 1:
                    st.model = matches[0]
            st.resolved = True
        return st.model

    def resume_options(self, st):
        options = copy.deepcopy(st.resume_options)
        for key in ("cwd", "approvalPolicy", "approvalsReviewer", "personality"):
            if key in st.settings:
                options[key] = st.settings[key]
        permission = st.settings.get("activePermissionProfile")
        if permission:
            options["permissions"] = permission["id"]
            options.pop("sandbox", None)
        elif "activePermissionProfile" in st.settings:
            options.pop("permissions", None)
            if "sandboxPolicy" in st.settings:
                options.pop("sandbox", None)
        options["threadId"] = st.tid
        return options

    async def preserve_sandbox(self, child, st, response):
        policy = st.settings.get("sandboxPolicy")
        if policy is not None and not st.settings.get("activePermissionProfile"):
            await child.call(
                "thread/settings/update",
                {"threadId": st.tid, "sandboxPolicy": copy.deepcopy(policy)},
            )
            response["sandbox"], response["activePermissionProfile"] = (
                copy.deepcopy(policy),
                None,
            )

    async def switch(self, st, model, native_model=None):
        if st.child is not None and model == st.model:
            return
        self.guard_switch(st)
        owner = st.child or self.main
        options = self.resume_options(st)
        previous = st.model
        if st.child:
            st.compact_waiter = asyncio.get_running_loop().create_future()
            st.active = True
            try:
                await owner.call("thread/compact/start", {"threadId": st.tid})
                limit = (
                    self.registry.provider(st.model.provider).stream_idle_timeout_ms
                    / 1000
                    + 60
                )
                turn = await asyncio.wait_for(st.compact_waiter, limit)
                if turn.get("status") != "completed":
                    raise RuntimeError(
                        "Compaction failed; model selection was retained"
                    )
            finally:
                st.compact_waiter = None
                st.active = False
            await owner.close()
            st.child = None
        elif model:
            saved = await owner.call(
                "thread/read", {"threadId": st.tid, "includeTurns": False}
            )
            thread = saved.get("thread", {})
            path = thread.get("path")
            if thread.get("ephemeral") or not path or not Path(path).is_file():
                raise RuntimeError(
                    "This thread has no saved history; start a new thread with the selected model"
                )
            self.suppress_archive.add(st.tid)
            try:
                await owner.call("thread/archive", {"threadId": st.tid})
                await owner.call("thread/unarchive", {"threadId": st.tid})
            finally:
                self.suppress_archive.discard(st.tid)
        child = self.backend_factory(self, st.tid, model) if model else self.main
        try:
            if model:
                await child.start()
                options = self.native_params(options, model)
                options.update(model=model.upstream_model, modelProvider=model.provider)
            else:
                options = self.default_params(options)
                options.update(model=native_model, modelProvider="openai")
            response = await child.call("thread/resume", options)
            if response.get("thread", {}).get("id") != st.tid:
                raise RuntimeError("Native backend resumed a different thread")
            await self.preserve_sandbox(child, st, response)
        except BaseException as error:
            if model:
                await child.close()
            st.model = previous
            # Reattach the prior provider after a failed handoff; do not generate a turn.
            restore = (
                self.backend_factory(self, st.tid + "-restore", previous)
                if previous
                else self.main
            )
            try:
                if previous:
                    await restore.start()
                    options = self.native_params(self.resume_options(st), previous)
                    options.update(
                        model=previous.upstream_model, modelProvider=previous.provider
                    )
                else:
                    options = self.default_params(self.resume_options(st))
                restored = await restore.call("thread/resume", options)
                await self.preserve_sandbox(restore, st, restored)
                st.child = restore if previous else None
            except Exception:
                if previous:
                    await restore.close()
                st.child, st.resolved = None, False
                raise RuntimeError(
                    "Model selection failed and the previous backend could not be reattached; resume this thread again"
                ) from error
            raise
        st.child, st.model, st.resolved = child if model else None, model, True
        self.adopt_settings(st, response)
        self.remember(st)

    async def dispatch(self, method, params):
        if method == "initialize":
            self.initialize_params = copy.deepcopy(params)
            self.main = self.backend_factory(self, "main")
            return await self.main.start()
        if self.main is None:
            raise RuntimeError("initialize must be called first")
        if method == "account/read":
            result = await self.main.call(method, params)
            if (
                result.get("account") is not None
                or result.get("requiresOpenaiAuth") is not True
            ):
                return result
            # A fresh profile can use its registered custom provider without
            # an OpenAI account. Ask a native backend for that provider's
            # account metadata; retain the main backend for actual login.
            account_backend = self.backend_factory(
                self, "account", self.registry.model()
            )
            try:
                await account_backend.start()
                return await account_backend.call(method, params)
            finally:
                await account_backend.close()
        if method == "model/list":
            limit = params.get("limit") or 100
            if type(limit) is not int or limit <= 0:
                raise ValueError("Model list limit must be a positive integer")
            cursor = params.get("cursor") or ""
            if cursor.startswith("custom-models:"):
                offset = int(cursor.removeprefix("custom-models:"))
                if not 0 <= offset <= len(self.registry.models):
                    raise ValueError("Invalid custom model list cursor")
                result = {"data": [], "nextCursor": None}
            else:
                result = copy.deepcopy(await self.main.call(method, params))
                offset = 0
            for item in result["data"]:
                item["isDefault"] = False
            if not result.get("nextCursor"):
                count = max(0, limit - len(result["data"]))
                result["data"] += [
                    model.picker_entry(model.alias == self.registry.default_model)
                    for model in self.registry.models[offset : offset + count]
                ]
                offset += count
                if offset < len(self.registry.models):
                    result["nextCursor"] = f"custom-models:{offset}"
            return result
        selected = ((params.get("collaborationMode") or {}).get("settings") or {}).get(
            "model"
        ) or params.get("model")
        model = (
            self.registry.model(selected)
            if isinstance(selected, str) and "::" in selected
            else None
        )
        provider = params.get("modelProvider") or (params.get("config") or {}).get(
            "model_provider"
        )
        if (
            model is None
            and selected is not None
            and provider in self.registry.providers
        ):
            matches = [
                candidate
                for candidate in self.registry.models
                if candidate.provider == provider
                and selected in {candidate.id, candidate.upstream_model}
            ]
            if len(matches) != 1:
                raise ValueError(
                    "The selected upstream model is not uniquely configured for this provider"
                )
            model = matches[0]
        if method == "thread/start":
            if selected is None:
                candidates = [
                    candidate
                    for candidate in self.registry.models
                    if candidate.provider == provider
                ]
                model = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.alias == self.registry.default_model
                    ),
                    candidates[0] if candidates else None,
                )
                if provider is None:
                    model = self.registry.model()
            if model:
                child = self.backend_factory(self, "new", model)
                try:
                    await child.start()
                    request = self.native_params(params, model)
                    request.update(
                        model=model.upstream_model, modelProvider=model.provider
                    )
                    result = await child.call(method, request)
                except BaseException:
                    await child.close()
                    raise
                st = self.state(result["thread"]["id"])
                st.model, st.child, st.resolved = model, child, True
                st.resume_options = {
                    key: copy.deepcopy(value)
                    for key, value in params.items()
                    if key in _RESUME_KEYS or key == "config"
                }
                self.adopt_settings(st, result)
                self.remember(st)
                return self.external(result, model)
            result = await self.main.call(method, self.default_params(params))
            st = self.state(result["thread"]["id"])
            st.resolved = True
            st.resume_options = {
                key: copy.deepcopy(value)
                for key, value in params.items()
                if key in _RESUME_KEYS or key == "config"
            }
            self.adopt_settings(st, result)
            return result
        tid = params.get("threadId") or params.get("conversationId")
        if not tid:
            return self.external(await self.main.call(method, params), None)
        st = self.state(tid)
        async with st.lock:
            if method == "thread/resume":
                st.resume_options.update(
                    {
                        key: copy.deepcopy(value)
                        for key, value in params.items()
                        if key in _RESUME_KEYS or key == "config"
                    }
                )
            if selected is None and method not in _PASSIVE:
                model = await self.saved_model(st)
            if model:
                await self.switch(st, model)
            elif selected is not None and st.child:
                await self.switch(st, None, selected)
            child = st.child or self.main
            request = self.native_params(params, st.model) if st.child else params
            result = await child.call(method, request)
            if method == "thread/fork" and st.child:
                fork = self.state(result["thread"]["id"])
                fork.resume_options = copy.deepcopy(st.resume_options)
                fork.model, fork.resolved = st.model, True
                self.suppress_archive.add(fork.tid)
                try:
                    await child.call("thread/archive", {"threadId": fork.tid})
                    await child.call("thread/unarchive", {"threadId": fork.tid})
                finally:
                    self.suppress_archive.discard(fork.tid)
                fork.child = self.backend_factory(self, fork.tid, fork.model)
                try:
                    await fork.child.start()
                    options = self.native_params(self.resume_options(fork), fork.model)
                    options.update(
                        model=fork.model.upstream_model,
                        modelProvider=fork.model.provider,
                    )
                    await fork.child.call("thread/resume", options)
                except BaseException:
                    await fork.child.close()
                    fork.child = None
                    raise
                self.remember(fork)
            if method == "turn/start":
                st.active = result.get("turn", {}).get("status") == "inProgress"
                st.turn_id = result.get("turn", {}).get("id") if st.active else None
            return self.external(result, st.model if st.child else None)

    async def handle(self, message):
        if "method" not in message:
            callback = self.callbacks.pop(message.get("id"), None)
            if callback:
                child, original = callback
                await child.send({**message, "id": original})
            return
        if "id" not in message:
            return  # Each child is initialized independently.
        try:
            result = await self.dispatch(message["method"], message.get("params") or {})
            self.emit({"id": message["id"], "result": result})
        except RpcError as error:
            self.emit({"id": message["id"], "error": error.error})
        except Exception as error:
            self.emit(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32000,
                        "message": str(error) or type(error).__name__,
                    },
                }
            )

    async def run(self):
        tasks = set()
        try:
            while line := await asyncio.to_thread(sys.stdin.buffer.readline):
                message = json.loads(line)
                if message.get("method") == "initialize":
                    await self.handle(message)
                else:
                    task = asyncio.create_task(self.handle(message))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            interruptions = [
                (st.child or self.main).call(
                    "turn/interrupt", {"threadId": st.tid, "turnId": st.turn_id}
                )
                for st in self.threads.values()
                if st.active and st.turn_id
            ]
            if interruptions:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*interruptions, return_exceptions=True), 5
                    )
                except asyncio.TimeoutError:
                    pass
            children = {
                st.child for st in self.threads.values() if st.child is not None
            }
            if self.main:
                children.add(self.main)
            await asyncio.gather(*(child.close() for child in children))
