"""Prevent known OpenAI login material appearing in external-provider request bodies."""

import json
import os
from pathlib import Path


def assert_no_openai_secrets(data, home):
    secrets = []
    if os.environ.get("OPENAI_API_KEY"):
        secrets.append(os.environ["OPENAI_API_KEY"])
    path = Path(home) / "auth.json"
    if path.exists():
        try:
            auth = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError(
                "Cannot verify OpenAI credential isolation; request was not sent"
            ) from None

        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in (
                        "access_token",
                        "refresh_token",
                        "id_token",
                        "openai_api_key",
                    ):
                        if isinstance(item, str) and item:
                            secrets.append(item)
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(auth)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if any(secret.encode("utf-8") in raw for secret in secrets):
        raise ValueError(
            "OpenAI login material was found in the request body; request was not sent"
        )
