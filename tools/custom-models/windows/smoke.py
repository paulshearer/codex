"""Exercise the packaged native transport without contacting a model service."""

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    options = parser.parse_args()
    package = options.package.resolve()
    with tempfile.TemporaryDirectory(prefix="codex-custom-smoke-") as home:
        environment = os.environ.copy()
        environment.update(
            CUSTOM_CODEX_REGISTRY=str(options.registry.resolve()),
            CUSTOM_CODEX_HOME=home,
            CUSTOM_CODEX_NATIVE=str(package / "native" / "codex.exe"),
        )
        ripgrep = subprocess.run(
            [str(package / "native" / "rg.exe"), "--version"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=True,
        )
        if not ripgrep.stdout.startswith("ripgrep 15.2.0"):
            raise RuntimeError("Unexpected bundled ripgrep version: " + ripgrep.stdout)
        version = subprocess.run(
            [str(package / "codex-custom.exe"), "--version"],
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=True,
        )
        if "0.157.1" not in version.stdout:
            raise RuntimeError("Unexpected native version: " + version.stdout)
        process = subprocess.Popen(
            [str(package / "codex-custom.exe"), "app-server", "--listen", "stdio://"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        messages = queue.Queue()
        diagnostics = []

        def read_output():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    messages.put(RuntimeError("Non-JSON stdout: " + line))
            messages.put(RuntimeError("App-server stdout closed."))

        def read_errors():
            for line in process.stderr:
                diagnostics.append(line)

        threading.Thread(target=read_output, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()

        def request(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" not in message:
                return None
            while True:
                response = messages.get(timeout=60)
                if isinstance(response, Exception):
                    raise response
                if response.get("id") == message["id"]:
                    if "error" in response:
                        raise RuntimeError(json.dumps(response["error"]))
                    return response["result"]

        try:
            request(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "codex-custom-smoke", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            request({"method": "initialized", "params": {}})
            configuration = request(
                {"id": 3, "method": "config/read", "params": {"includeLayers": False}}
            )
            configured_catalog = configuration["config"].get("model_catalog_json")
            if not configured_catalog or not Path(configured_catalog).is_file():
                raise RuntimeError("Desktop custom catalog discovery is missing")
            catalog = request(
                {"id": 2, "method": "model/list", "params": {"limit": 100}}
            )
            registry = json.loads(options.registry.read_text(encoding="utf-8-sig"))
            expected = {
                model["provider"] + "::" + model["id"] for model in registry["models"]
            }
            actual = {model["id"] for model in catalog["data"]}
            if not expected <= actual:
                raise RuntimeError(
                    "Custom aliases missing from model/list: " + repr(expected - actual)
                )
            print(
                "Native version and initialize/model/list passed: "
                + ", ".join(sorted(expected))
            )
        except Exception as error:
            raise RuntimeError(
                str(error) + "\n" + "".join(diagnostics[-20:])
            ) from error
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired as error:
                # Preserve the process for diagnosis rather than terminating a runtime.
                raise RuntimeError(
                    "App-server did not stop after stdin closed (PID %s)." % process.pid
                ) from error


if __name__ == "__main__":
    main()
