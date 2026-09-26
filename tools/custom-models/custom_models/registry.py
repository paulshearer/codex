"""Validated custom providers and Codex-native model metadata."""

import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_RESERVED = {"openai", "ollama", "lmstudio", "amazon-bedrock", "amazon-bedrock-runtime"}


@contextmanager
def _selection_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "selections.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _text(value, label):
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(c in value for c in "\r\n\0")
    ):
        raise ValueError(f"{label} must be a nonempty single-line string")
    return value.strip()


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _mapping(value, label, environment=False):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result = {_text(key, label): _text(item, label) for key, item in value.items()}
    if environment and any(not _ENV.fullmatch(item) for item in result.values()):
        raise ValueError(f"{label} contains an invalid environment variable name")
    return result


def _unknown(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) - allowed:
        raise ValueError(f"{label} contains unsupported fields")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Registry contains a duplicate JSON object key")
        result[key] = value
    return result


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    api_format: str
    base_url: str
    env_key: str | None = None
    auth: dict | None = None
    http_headers: dict = field(default_factory=dict)
    env_http_headers: dict = field(default_factory=dict)
    query_params: dict = field(default_factory=dict)
    stream_idle_timeout_ms: int = 600_000
    upstream_timeout_seconds: float = 660


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    upstream_model: str
    display_name: str
    context_window: int
    max_output_tokens: int
    auto_compact_token_limit: int
    reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str = "none"
    supports_images: bool = False
    supports_tool_search: bool = False

    @property
    def alias(self):
        return f"{self.provider}::{self.id}"

    def picker_entry(self, default=False):
        return {
            "id": self.alias,
            "model": self.alias,
            "displayName": self.display_name,
            "description": f"{self.provider}; {self.context_window:,}-token context",
            "hidden": False,
            "isDefault": default,
            "defaultReasoningEffort": self.default_reasoning_effort,
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort, "description": effort.title()}
                for effort in self.reasoning_efforts
            ],
            "inputModalities": ["text", "image"] if self.supports_images else ["text"],
            "supportsPersonality": False,
            "serviceTiers": [],
            "additionalSpeedTiers": [],
            "defaultServiceTier": None,
            "availableAccessPrograms": None,
            "upgrade": None,
            "upgradeInfo": None,
            "availabilityNux": None,
            "modelSpecialty": None,
            "multiAgentVersion": None,
        }


class Registry:
    def __init__(self, providers, models, default_model):
        self.providers = dict(providers)
        self.models = tuple(models)
        self.default_model = default_model
        self._models = {model.alias: model for model in self.models}

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.stat().st_size > 512 * 1024:
            raise ValueError("Registry exceeds the 512 KiB size limit")
        return cls.from_dict(
            json.loads(
                path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object
            )
        )

    @classmethod
    def from_dict(cls, data):
        _unknown(data, {"version", "default_model", "providers", "models"}, "Registry")
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Registry version must be 1")
        if not isinstance(data.get("providers"), dict) or not data["providers"]:
            raise ValueError("Registry requires providers")
        providers = {}
        fields = set(Provider.__dataclass_fields__) - {"id"}
        for ident, values in data["providers"].items():
            if (
                not isinstance(ident, str)
                or not _ID.fullmatch(ident)
                or ident in _RESERVED
            ):
                raise ValueError("Custom provider ID is invalid or reserved")
            if ident.casefold() in {key.casefold() for key in providers}:
                raise ValueError("Custom provider IDs must be unique on Windows")
            _unknown(values, fields, "Provider")
            name = _text(values.get("name", ident), "Provider name")
            api = values.get("api_format", "responses")
            if api not in {"responses", "chat_completions"}:
                raise ValueError(
                    "Provider api_format must be responses or chat_completions"
                )
            base = _text(values.get("base_url"), "Provider base_url").rstrip("/")
            parsed = urlsplit(base)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or parsed.query
            ):
                raise ValueError(
                    "Provider base_url must be HTTP(S) without embedded credentials, query or fragment"
                )
            env_key = values.get("env_key")
            if env_key is not None and (
                not isinstance(env_key, str) or not _ENV.fullmatch(env_key)
            ):
                raise ValueError("Provider env_key is invalid")
            auth = values.get("auth")
            if auth is not None:
                _unknown(
                    auth,
                    {"command", "args", "cwd", "timeout_ms", "refresh_interval_ms"},
                    "Provider auth",
                )
                _text(auth.get("command"), "Auth command")
                if (
                    auth.get("cwd") is not None
                    and not Path(_text(auth["cwd"], "Auth cwd")).is_absolute()
                ):
                    raise ValueError("Auth cwd must be an absolute path")
                if env_key:
                    raise ValueError("Provider cannot combine env_key and command auth")
                if not isinstance(auth.get("args", []), list) or any(
                    not isinstance(arg, str) or "\0" in arg
                    for arg in auth.get("args", [])
                ):
                    raise ValueError("Auth args must be a string array")
                _positive(auth.get("timeout_ms", 5000), "Auth timeout_ms")
                refresh = auth.get("refresh_interval_ms", 300000)
                if type(refresh) is not int or refresh < 0:
                    raise ValueError(
                        "Auth refresh_interval_ms must be a nonnegative integer"
                    )
            idle = _positive(
                values.get("stream_idle_timeout_ms", 600000), "Stream idle timeout"
            )
            timeout = values.get("upstream_timeout_seconds", 660)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (float, int))
                or not math.isfinite(timeout)
                or timeout <= 0
                or timeout < idle / 1000
            ):
                raise ValueError(
                    "Upstream timeout must be at least the stream idle timeout"
                )
            providers[ident] = Provider(
                ident,
                name,
                api,
                base,
                env_key,
                auth,
                _mapping(values.get("http_headers", {}), "HTTP headers"),
                _mapping(
                    values.get("env_http_headers", {}), "Environment HTTP headers", True
                ),
                _mapping(values.get("query_params", {}), "Query parameters"),
                idle,
                timeout,
            )
        if (
            not isinstance(data.get("models"), list)
            or not 1 <= len(data["models"]) <= 100
        ):
            raise ValueError("Registry requires 1 to 100 models")
        models = []
        aliases = set()
        upstream_models = set()
        for values in data["models"]:
            _unknown(values, set(Model.__dataclass_fields__), "Model")
            ident = values.get("id")
            provider = values.get("provider")
            if (
                not isinstance(ident, str)
                or not _ID.fullmatch(ident)
                or provider not in providers
            ):
                raise ValueError("Model ID or provider is invalid")
            context = _positive(values.get("context_window"), "Context window")
            output = _positive(values.get("max_output_tokens"), "Output limit")
            compact = _positive(
                values.get("auto_compact_token_limit"), "Compaction threshold"
            )
            if output >= context or compact > context - output:
                raise ValueError("Model limits must reserve room for output")
            efforts = values.get("reasoning_efforts", [])
            if (
                not isinstance(efforts, list)
                or any(
                    not isinstance(item, str) or item not in _EFFORTS
                    for item in efforts
                )
                or len(set(efforts)) != len(efforts)
            ):
                raise ValueError("Model reasoning efforts are invalid")
            default = values.get("default_reasoning_effort", "none")
            if default not in (efforts or ["none"]):
                raise ValueError(
                    "Default reasoning effort must be supported by the model"
                )
            images = values.get("supports_images", False)
            if type(images) is not bool:
                raise ValueError("supports_images must be boolean")
            if images and providers[provider].api_format == "chat_completions":
                raise ValueError(
                    "Chat Completions models support text only in this release"
                )
            search = values.get("supports_tool_search", False)
            if type(search) is not bool:
                raise ValueError("supports_tool_search must be boolean")
            model = Model(
                ident,
                provider,
                _text(values.get("upstream_model"), "Upstream model"),
                _text(values.get("display_name", ident), "Model display name"),
                context,
                output,
                compact,
                tuple(efforts),
                default,
                images,
                search,
            )
            if model.alias.casefold() in {alias.casefold() for alias in aliases}:
                raise ValueError("Registry contains a duplicate model alias")
            upstream = (model.provider, model.upstream_model)
            if upstream in upstream_models:
                raise ValueError(
                    "Each provider must have unique upstream model identifiers"
                )
            upstream_models.add(upstream)
            aliases.add(model.alias)
            models.append(model)
        default = data.get("default_model")
        if default not in aliases:
            raise ValueError(
                "Registry default_model must name a configured model alias"
            )
        return cls(providers, models, default)

    def model(self, alias=None):
        try:
            return self._models[alias or self.default_model]
        except KeyError:
            raise ValueError("Unknown custom model alias") from None

    def provider(self, ident):
        try:
            return self.providers[ident]
        except KeyError:
            raise ValueError("Unknown custom provider") from None

    @staticmethod
    def load_selections(home):
        path = Path(home) / "custom-models" / "selections.json"
        if not path.exists():
            return {}
        data = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in data.items()
        ):
            raise ValueError("Saved custom model selections are invalid")
        return data

    def save_selection(self, home, thread_id, alias=None):
        if alias is not None:
            self.model(alias)
        directory = Path(home) / "custom-models"
        with _selection_lock(directory):
            selections = self.load_selections(home)
            if alias is None:
                selections.pop(thread_id, None)
            else:
                selections[thread_id] = alias
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
            ) as temporary:
                json.dump(selections, temporary)
                path = Path(temporary.name)
            try:
                path.replace(directory / "selections.json")
            finally:
                path.unlink(missing_ok=True)
        return selections

    def native_config(self, model, base_url, loopback_token=None):
        if isinstance(model, str):
            model = self.model(model)
        provider = self.provider(model.provider)
        table = {
            "name": provider.name,
            "base_url": base_url,
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
            "stream_idle_timeout_ms": provider.stream_idle_timeout_ms,
            "request_max_retries": 0,
            "stream_max_retries": 0,
        }
        if loopback_token:
            table["http_headers"] = {"Authorization": "Bearer " + loopback_token}
        result = {
            "model": model.upstream_model,
            "model_provider": provider.id,
            "model_context_window": model.context_window,
            "model_auto_compact_token_limit": model.auto_compact_token_limit,
            "model_reasoning_effort": model.default_reasoning_effort,
            f"model_providers.{provider.id}": table,
        }
        if provider.api_format == "chat_completions":
            result["web_search"] = "disabled"
        return result

    def native_catalog(self, model, instructions=None):
        if isinstance(model, str):
            model = self.model(model)
        if instructions is None:
            instructions = (
                Path(__file__).parent.parent / "assets" / "prompt.md"
            ).read_text(encoding="utf-8")
        return {
            "models": [
                {
                    "slug": model.upstream_model,
                    "display_name": model.display_name,
                    "description": f"Custom model from {model.provider}",
                    "default_reasoning_level": model.default_reasoning_effort,
                    "supported_reasoning_levels": [
                        {"effort": effort, "description": effort.title()}
                        for effort in model.reasoning_efforts
                    ],
                    "shell_type": "unified_exec",
                    "visibility": "list",
                    "supported_in_api": True,
                    "priority": 0,
                    "availability_nux": None,
                    "upgrade": None,
                    "model_messages": {"instructions_template": instructions},
                    "supports_reasoning_summary_parameter": False,
                    "default_reasoning_summary": "none",
                    "support_verbosity": False,
                    "default_verbosity": None,
                    "apply_patch_tool_type": "freeform",
                    "truncation_policy": {"mode": "tokens", "limit": 10000},
                    "context_window": model.context_window,
                    "max_context_window": model.context_window,
                    "auto_compact_token_limit": model.auto_compact_token_limit,
                    "supports_search_tool": model.supports_tool_search,
                    "effective_context_window_percent": 95,
                    "experimental_supported_tools": [],
                    "input_modalities": ["text", "image"]
                    if model.supports_images
                    else ["text"],
                }
            ]
        }
