"""Opt-in, workspace-local diagnostics; never enable global native logging."""

import json
import time


class WorkspaceDiagnostics:
    def __init__(self, home, max_bytes=10 * 1024 * 1024):
        self.root = home / "logs" / "workspaces"
        self.max_bytes = max_bytes

    def record(self, task, kind, data):
        level = task["diagnosticLogLevel"]
        if level == "off":
            return
        error = kind == "status" and data.get("status") in {"failed", "interrupted"}
        error = error or kind == "agent" and data.get("method") == "error"
        if level == "errors" and not error:
            return
        if level == "info" and kind not in {"status", "usage", "submission", "steering", "queue"} and not error:
            return
        record = {"timestamp": time.time(), "taskId": task["id"], "conversationId": task["conversationId"],
                  "workspaceId": task["workspaceId"], "kind": kind}
        if level == "debug":
            record["data"] = data
        else:
            # Allowlist operational fields; exception text and user content are excluded.
            record["data"] = {key: data[key] for key in ("status", "method", "submissionId", "turnId") if key in data}
        try:
            directory = self.root / task["workspaceId"]
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "diagnostics.jsonl"
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            if path.exists() and path.stat().st_size + len(line) > self.max_bytes:
                (directory / "diagnostics.5.jsonl").unlink(missing_ok=True)
                for index in range(4, 0, -1):
                    previous = directory / f"diagnostics.{index}.jsonl"
                    if previous.exists():
                        previous.replace(directory / f"diagnostics.{index + 1}.jsonl")
                path.replace(directory / "diagnostics.1.jsonl")
            with path.open("ab") as handle:
                handle.write(line)
        except (OSError, ValueError, TypeError):
            # A logging failure cannot affect task execution or event persistence.
            pass
