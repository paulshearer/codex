"""Check console launch, saved provider switches, and resume with native Codex."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--launcher", type=Path)
    options = parser.parse_args()
    source = Path(__file__).resolve().parent
    specification = importlib.util.spec_from_file_location(
        "native_acceptance", source / "native-acceptance.py"
    )
    acceptance = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(acceptance)
    with (
        tempfile.TemporaryDirectory(
            prefix="codex CLI with spaces ", ignore_cleanup_errors=True
        ) as directory,
        acceptance.ModelService() as service,
    ):
        root = Path(directory)
        workspace, home = root / "workspace", root / "profile"
        workspace.mkdir()
        registry = root / "registry.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default_model": "first::same",
                    "providers": {
                        key: {
                            "api_format": "responses",
                            "base_url": service.base_url,
                            "http_headers": {"X-Provider": key},
                        }
                        for key in ("first", "second")
                    },
                    "models": [
                        {
                            "id": "same",
                            "provider": key,
                            "upstream_model": "same-model",
                            "context_window": 262144,
                            "max_output_tokens": 16384,
                            "auto_compact_token_limit": 196608,
                        }
                        for key in ("first", "second")
                    ],
                }
            ),
            encoding="utf-8",
        )
        environment = dict(os.environ, PYTHONPATH=str(source))
        for key in (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_ORGANIZATION",
            "OPENAI_PROJECT_ID",
        ):
            environment.pop(key, None)
        prefix = (
            [str(options.launcher.resolve())]
            if options.launcher
            else [sys.executable, "-m", "custom_models"]
        )
        prefix += [
            "--registry",
            str(registry),
            "--home",
            str(home),
            "--native",
            str(options.native.resolve()),
        ]

        def run(arguments):
            result = subprocess.run(
                prefix + arguments,
                env=environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=90,
            )
            if result.returncode:
                raise RuntimeError(
                    "Native console request failed:\n"
                    + result.stdout[-2000:]
                    + result.stderr[-3000:]
                )
            rows = [
                json.loads(line) for line in result.stdout.splitlines() if line.strip()
            ]
            assert any(row.get("type") == "turn.completed" for row in rows), rows
            return rows

        common = ["exec", "--json", "--skip-git-repo-check", "-C", str(workspace)]
        rows = run(common + ["Console launcher smoke."])
        tid = next(
            row["thread_id"] for row in rows if row.get("type") == "thread.started"
        )
        assert service.records[-1]["headers"]["X-Provider"] == "first"
        run(
            [
                "--custom-model",
                "second::same",
                *common,
                "resume",
                tid,
                "Switch the provider for this task.",
            ]
        )
        assert service.records[-1]["headers"]["X-Provider"] == "second"
        run(common + ["resume", tid, "Resume the same task using its saved provider."])
        assert service.records[-1]["headers"]["X-Provider"] == "second"
        run(common + ["resume", "--last", "Resume the latest task in this workspace."])
        assert service.records[-1]["headers"]["X-Provider"] == "second"
        assert not (home / "auth.json").exists()
        assert all(
            "Authorization" not in record["headers"] for record in service.records
        )
        print(
            "PASS: native Windows console execution; same-ID provider switch; explicit/latest restart resume; isolated credentials; paths with spaces"
        )


if __name__ == "__main__":
    main()
