"""Launch the native Codex CLI or its desktop stdio router with custom providers."""

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from .registry import Registry


def toml_value(value):
    """Encode configuration overrides as TOML, including quoted provider keys."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{json.dumps(key)} = {toml_value(item)}"
                for key, item in value.items()
                if item is not None
            )
            + " }"
        )
    raise ValueError(f"Unsupported configuration value: {type(value).__name__}")


_VALUE_FLAGS = {
    "-m",
    "--model",
    "-C",
    "--cd",
    "-c",
    "--config",
    "-i",
    "--image",
    "-p",
    "--profile",
    "--enable",
    "--disable",
    "--output-schema",
    "-o",
    "--output-last-message",
    "--color",
    "--remote",
    "--remote-url",
}


def native_command(arguments):
    """Locate the native command without interpreting option values or prompts."""
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == "--":
            return None, None
        if item in _VALUE_FLAGS:
            index += 2
        elif item.startswith("-"):
            index += 1
        else:
            return item, index
    return None, None


def resume_request(arguments):
    command, index = native_command(arguments)
    execution = command in {"exec", "e"}
    if execution:
        command, offset = native_command(arguments[index + 1 :])
        index = index + 1 + offset if offset is not None else None
    if command != "resume":
        return None
    target = None
    target_index = None
    cwd = Path.cwd()
    option_positions = []
    position = 0
    while position < len(arguments):
        value = arguments[position]
        if value == "--":
            break
        option_positions.append((position, value))
        position += 2 if value in _VALUE_FLAGS else 1
    for position, value in option_positions:
        if value in {"-C", "--cd"} and position + 1 < len(arguments):
            cwd = Path(arguments[position + 1]).expanduser().resolve()
        elif value.startswith("--cd="):
            cwd = Path(value.split("=", 1)[1]).expanduser().resolve()
    following = index + 1
    literal = False
    while following < len(arguments):
        item = arguments[following]
        if item == "--":
            literal = True
        elif not literal and item in _VALUE_FLAGS:
            following += 1
        elif literal or not item.startswith("-"):
            target = item
            target_index = following
            break
        following += 1
    flags = {
        value: position for position, value in option_positions if position > index
    }
    return {
        "index": index,
        "target": None if "--last" in flags else target,
        "last": "--last" in flags,
        "last_index": flags.get("--last"),
        "all": "--all" in flags,
        "execution": execution,
        "include_non_interactive": "--include-non-interactive" in flags,
        "cwd": cwd,
        "target_index": target_index,
    }


def select_model(arguments, registry, requested=None, saved=None):
    """Resolve picker aliases without consuming native command options."""
    result = list(arguments)
    native_explicit = None
    for index, value in enumerate(result):
        if value == "--":
            break
        if value in ("--model", "-m"):
            if index + 1 >= len(result):
                raise ValueError(f"{value} needs a model identifier")
            native_explicit = result[index + 1]
        elif value.startswith("--model="):
            native_explicit = value.split("=", 1)[1]
    explicit = requested or native_explicit
    alias = explicit or saved or registry.default_model
    if "::" not in alias:
        if explicit or saved:
            return None, result
        raise ValueError("The default custom model must use a provider::model alias")
    model = registry.model(alias)
    for index, value in enumerate(result):
        if value == "--":
            break
        if value in ("--model", "-m"):
            result[index + 1] = model.upstream_model
        elif value.startswith("--model="):
            result[index] = "--model=" + model.upstream_model
    return model, result


def _rollout_metadata(path):
    result = {
        "path": path,
        "provider": None,
        "model": None,
        "id": None,
        "cwd": None,
        "turn_cwd": None,
        "source": "cli",
        "settings": False,
        "updated": path.stat().st_mtime_ns / 1_000_000,
    }
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                continue
            payload = row.get("payload", {})
            if row.get("type") == "session_meta":
                result.update(
                    provider=payload.get("model_provider"),
                    id=payload.get("id"),
                    cwd=payload.get("cwd"),
                    source=payload.get("source", "cli"),
                )
            elif row.get("type") == "turn_context":
                result["model"] = payload.get("model", result["model"])
                result["turn_cwd"] = payload.get("cwd", result["turn_cwd"])
            elif (
                row.get("type") == "event_msg"
                and payload.get("type") == "thread_settings_applied"
            ):
                if (
                    payload.get("thread_id") is not None
                    and payload["thread_id"] != result["id"]
                ):
                    continue
                settings = payload.get("thread_settings", {})
                if isinstance(settings, dict):
                    result.update(
                        provider=settings.get("model_provider_id", result["provider"]),
                        model=settings.get("model", result["model"]),
                        cwd=settings.get("cwd", result["cwd"]),
                        settings=True,
                    )
    return result


def _saved_records(home):
    sessions = (home / "sessions").resolve()
    records = {}
    for path in sessions.rglob("*.jsonl"):
        if not path.resolve().is_relative_to(sessions):
            continue
        try:
            record = _rollout_metadata(path)
        except FileNotFoundError:
            continue
        records[str(path.resolve())] = record
    # Native --last ordering comes from its own profile's current SQLite
    # projection. Read only; a row is usable only with its verified rollout ID.
    for database in sorted(
        home.glob("state_*.sqlite"),
        key=lambda path: (
            int(path.stem.split("_")[-1]) if path.stem.split("_")[-1].isdigit() else -1
        ),
        reverse=True,
    ):
        try:
            with contextlib.closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM threads WHERE archived = 0 ORDER BY updated_at DESC, id DESC LIMIT 10000"
                )
                for row in rows:
                    record = records.get(str(Path(row["rollout_path"]).resolve()))
                    if not record or record["id"] != row["id"]:
                        continue
                    keys = row.keys()
                    timestamp = (
                        row["updated_at_ms"]
                        if "updated_at_ms" in keys and row["updated_at_ms"] is not None
                        else row["updated_at"] * 1000
                    )
                    record.update(
                        updated=timestamp,
                        db=True,
                        name=row["name"] if "name" in keys else None,
                    )
                    if not record["settings"]:
                        record.update(
                            provider=row["model_provider"],
                            model=row["model"] if "model" in keys else record["model"],
                            cwd=row["cwd"],
                        )
            break
        except sqlite3.Error:
            continue
    index = home / "session_index.jsonl"
    if index.exists():
        names = {}
        with index.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    names[row["id"]] = row["thread_name"]
                except (ValueError, KeyError, TypeError):
                    continue
        for record in records.values():
            if not record.get("name"):
                record["name"] = names.get(record["id"])
    return list(records.values())


def cli_resume(arguments, home, registry, explicit=None):
    """Resolve a known CLI resume target before selecting its loopback provider."""
    request = resume_request(arguments)
    if request is None:
        return None, None, list(arguments)
    if not request["target"] and not request["last"]:
        if len(registry.providers) > 1 and not explicit:
            raise ValueError(
                "Resume the selected task with resume THREAD_ID, or choose --custom-model for the interactive picker"
            )
        return None, None, list(arguments)
    selections = registry.load_selections(home)
    chosen = explicit or cli_selection(home, registry) or registry.default_model
    selected_provider = registry.model(chosen).provider if "::" in chosen else "openai"
    records = _saved_records(home)
    for record in records:
        if (
            not record["settings"]
            and not record.get("db")
            and record["id"] in selections
        ):
            model = registry.model(selections[record["id"]])
            record.update(provider=model.provider, model=model.upstream_model)
    if request["last"]:
        records = [
            record for record in records if record["provider"] == selected_provider
        ]
        if not request["all"]:

            def same_cwd(record):
                cwd = record["turn_cwd"] if request["execution"] else record["cwd"]
                return cwd is None or Path(cwd).expanduser().resolve() == request["cwd"]

            records = [record for record in records if same_cwd(record)]
        if not request["execution"] and not request["include_non_interactive"]:
            records = [
                record
                for record in records
                if isinstance(record["source"], str)
                and record["source"] in {"cli", "vscode"}
            ]
        projected = [record for record in records if record.get("db")]
        if projected:
            records = projected
        records.sort(
            key=lambda record: (record["updated"], record["id"] or ""), reverse=True
        )
    else:
        target = request["target"]
        exact = [
            record
            for record in records
            if record["id"] == target or record["path"].stem.endswith("-" + target)
        ]
        records = exact or [
            record for record in records if record.get("name") == target
        ]
        if len(records) > 1:
            raise ValueError(
                "Saved task name is ambiguous; resume with its exact THREAD_ID"
            )
    if not records:
        if not explicit and len(registry.providers) > 1:
            raise ValueError(
                "Saved provider could not be resolved; choose --custom-model explicitly"
            )
        return None, None, list(arguments)
    record = records[0]
    matches = [
        model
        for model in registry.models
        if model.provider == record["provider"]
        and model.upstream_model == record["model"]
    ]
    saved = (
        matches[0].alias
        if len(matches) == 1
        else record["model"]
        if record["provider"] not in registry.providers
        else None
    )
    if saved is None and not explicit:
        raise ValueError(
            "Saved model is not configured; choose --custom-model explicitly"
        )
    rewritten = list(arguments)
    if request["last"] and record["id"]:
        rewritten.pop(request["last_index"])
        rewritten.insert(request["index"] + 1, record["id"])
    elif record["id"] and request["target"] != record["id"]:
        rewritten[request["target_index"]] = record["id"]
    return saved, record["id"], rewritten


def saved_cli_model(arguments, home, registry):
    request = resume_request(arguments)
    if request is None or not request["target"] and not request["last"]:
        return None
    try:
        return cli_resume(arguments, home, registry)[0]
    except ValueError as error:
        if "could not be resolved" in str(error):
            return None
        raise


def cli_selection(home, registry, model=None):
    path = home / "custom-models" / "cli-selection.json"
    if model is None:
        if not path.exists():
            return None
        alias = json.loads(path.read_text(encoding="utf-8"))["model"]
        return registry.model(alias).alias
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as stream:
        json.dump({"model": model.alias}, stream)
        temporary = Path(stream.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def native_arguments(arguments, overrides):
    # Native -c flags are global, but must precede a literal prompt delimiter.
    index = arguments.index("--") if "--" in arguments else len(arguments)
    return [*arguments[:index], *overrides, *arguments[index:]]


def configure_catalog(registry, model, home):
    destination = home / "catalogs" / (model.provider + "-" + model.id + ".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(registry.native_catalog(model), ensure_ascii=False, indent=2)
    if not destination.exists() or destination.read_text(encoding="utf-8") != content:
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    return destination


def runtime_root():
    return Path(__file__).resolve().parent.parent


def parse_options(argv):
    root = runtime_root()
    registry_default = next(
        (
            root / name
            for name in (
                "registry.local.json",
                "registry.json",
                "registry.example.json",
            )
            if (root / name).exists()
        ),
        root / "registry.json",
    )
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--registry",
        default=os.environ.get("CUSTOM_CODEX_REGISTRY", str(registry_default)),
    )
    profile = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "CodexCustom"
        / "profiles"
        / "default"
    )
    parser.add_argument(
        "--home", default=os.environ.get("CUSTOM_CODEX_HOME", str(profile))
    )
    native_default = root / "native" / "codex.exe"
    parser.add_argument(
        "--native", default=os.environ.get("CUSTOM_CODEX_NATIVE", str(native_default))
    )
    parser.add_argument("--custom-model")
    parser.add_argument("--list-custom-models", action="store_true")
    parser.add_argument("--prepare-handoff", metavar="THREAD_ID")
    if "--" in argv:
        separator = argv.index("--")
        options, unknown = parser.parse_known_args(argv[:separator])
        # Once native arguments have begun, its prompt delimiter belongs to
        # native Codex. A delimiter before the native command separates the
        # companion options instead.
        arguments = (
            unknown
            + argv[
                separator if native_command(unknown)[0] is not None else separator + 1 :
            ]
        )
    else:
        options, arguments = parser.parse_known_args(argv)
    return options, arguments


def main(argv=None):
    options, arguments = parse_options(sys.argv[1:] if argv is None else argv)
    native = Path(options.native).expanduser().resolve()
    home = Path(options.home).expanduser().resolve()
    if arguments in (["--version"], ["-V"], ["--help"], ["-h"]):
        if not native.is_file():
            raise ValueError(
                f"Native CLI not found: {native}. Build or configure --native first."
            )
        return subprocess.call([str(native), *arguments])
    registry = Registry.load(Path(options.registry).expanduser().resolve())
    if options.list_custom_models:
        print(
            json.dumps(
                [
                    {
                        "id": model.alias,
                        "name": model.display_name,
                        "upstream_model": model.upstream_model,
                        "api_format": registry.provider(model.provider).api_format,
                    }
                    for model in registry.models
                ],
                indent=2,
            )
        )
        return 0
    if not native.is_file():
        raise ValueError(
            f"Native CLI not found: {native}. Build or configure --native first."
        )
    home.mkdir(parents=True, exist_ok=True)
    from .facade import Facade
    from .handoff import HandoffResolver

    environment = dict(os.environ, CODEX_HOME=str(home))
    is_server = native_command(arguments)[0] == "app-server"
    explicit_model = options.custom_model
    if explicit_model is None:
        for index, value in enumerate(arguments):
            if value == "--":
                break
            if value in {"-m", "--model"} and index + 1 < len(arguments):
                explicit_model = arguments[index + 1]
            elif value.startswith("--model="):
                explicit_model = value.split("=", 1)[1]
    saved, resumed_id = None, None
    if not is_server:
        saved, resumed_id, arguments = cli_resume(
            arguments, home, registry, explicit_model
        )
    model, arguments = select_model(
        arguments,
        registry,
        options.custom_model,
        None if is_server else (saved or cli_selection(home, registry)),
    )
    providers = (
        registry.providers.values()
        if is_server
        else [registry.provider(model.provider)]
        if model
        else []
    )
    with contextlib.ExitStack() as resources:
        facades, resolvers = {}, {}
        for provider in providers:
            provider_models = [
                entry for entry in registry.models if entry.provider == provider.id
            ]
            resolver = HandoffResolver(home, home / "handoffs" / provider.id)
            facade = Facade(provider, provider_models, home, resolver=resolver.resolve)
            facade.start()
            resources.callback(facade.stop)
            facades[provider.id] = facade
            resolvers[provider.id] = resolver
            resolver.set_summarizer(facade.summarize)
        if options.prepare_handoff:
            if model is None:
                raise ValueError(
                    "Prepare a handoff with a configured --custom-model alias"
                )
            resolver = resolvers[model.provider]
            print(
                json.dumps(
                    resolver.prepare_thread(options.prepare_handoff, model), indent=2
                )
            )
            return 0
        if is_server:
            from .router import Router

            router = Router(
                native,
                registry,
                home,
                {key: facade.base_url for key, facade in facades.items()},
                facade_tokens={key: facade.token for key, facade in facades.items()},
                arguments=arguments,
            )
            return asyncio.run(router.run()) or 0
        configuration = {}
        if model:
            facade = facades[model.provider]
            configuration = registry.native_config(model, facade.base_url, facade.token)
            configuration["model_catalog_json"] = str(
                configure_catalog(registry, model, home)
            )
            provider = registry.provider(model.provider)
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
        overrides = [
            part
            for key, value in configuration.items()
            for part in ("-c", f"{key}={toml_value(value)}")
            if value is not None
        ]
        child = subprocess.Popen(
            [str(native), *native_arguments(arguments, overrides)], env=environment
        )
        cancelled = False
        try:
            if model:
                cli_selection(home, registry, model)
            try:
                code = child.wait()
            except KeyboardInterrupt:
                cancelled = True
                try:
                    code = child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    code = child.wait(timeout=10)
            if code == 0 and not cancelled and resumed_id:
                registry.save_selection(
                    home, resumed_id, model.alias if model else None
                )
            return code
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"Custom Codex: {error}", file=sys.stderr)
        raise SystemExit(2) from None
