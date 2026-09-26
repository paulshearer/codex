import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from custom_models.credential_guard import assert_no_openai_secrets
from custom_models.handoff import HandoffResolver, MAX_HANDOFF_BYTES, checkpoints


def history(home, text="Remember: orchid marker 8721.", image=False):
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    content = [{"type": "input_text", "text": text}]
    if image:
        content.append(
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        )
    item = {"type": "compaction", "encrypted_content": "encrypted-test-checkpoint"}
    rows = [
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": content},
        },
        {"type": "compacted", "payload": {"replacement_history": [item]}},
    ]
    path = sessions / "rollout-thread-123.jsonl"
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    path.write_bytes(raw)
    return path, raw, item


class HandoffTests(unittest.TestCase):
    def test_checkpoint_handoff_recall_and_original_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path, raw, item = history(home, image=True)
            found = list(checkpoints(path))
            self.assertEqual(found[0]["source_sha256"], hashlib.sha256(raw).hexdigest())
            resolver = HandoffResolver(home, home / "handoffs")
            result = resolver.resolve(item)
            self.assertIn("orchid marker 8721", result[0]["content"][0]["text"])
            self.assertEqual(result[1]["content"][1]["type"], "input_image")
            self.assertEqual(path.read_bytes(), raw)
            path.write_bytes(raw + b'{"incomplete":')
            self.assertEqual(list(checkpoints(path)), found)
            self.assertEqual(
                resolver.prepare_thread("thread-123")[0]["method"],
                "readable-transcript",
            )

    def test_long_handoff_uses_bounded_provider_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path, raw, item = history(home, "Remember orchid 8721. " * 12000)
            sizes = []

            def summary(text, model=None):
                sizes.append(len(text))
                return "orchid 8721; " + text[: min(200, len(text) // 4)]

            result = HandoffResolver(home, home / "handoffs", summary).resolve(item)
            self.assertIn("orchid 8721", result[0]["content"][0]["text"])
            self.assertLessEqual(max(sizes), 9000)
            self.assertLessEqual(
                len(result[0]["content"][0]["text"].encode()), MAX_HANDOFF_BYTES + 130
            )
            self.assertEqual(path.read_bytes(), raw)

    def test_incomplete_unknown_and_corrupt_history_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path, raw, item = history(home)
            resolver = HandoffResolver(home, home / "handoffs")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                resolver.resolve({**item, "encrypted_content": "missing"})
            path.write_bytes(raw + b"bad complete row\n")
            with self.assertRaisesRegex(ValueError, "invalid completed"):
                resolver.resolve(item)

    def test_changed_source_and_tampered_cache_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path, raw, item = history(home)
            resolver = HandoffResolver(home, home / "handoffs")
            resolver.resolve(item)
            cached = next(resolver.cache.glob("*.json"))
            record = json.loads(cached.read_text())
            record["summary"] = "tampered"
            cached.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                resolver.resolve(item)
            record["summary"] = "restored"
            record["summary_sha256"] = hashlib.sha256(b"restored").hexdigest()
            cached.write_text(json.dumps(record), encoding="utf-8")
            path.write_bytes(raw.replace(b"8721", b"8722"))
            with self.assertRaisesRegex(ValueError, "source changed"):
                resolver.resolve(item)

    def test_duplicate_checkpoint_sources_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path, raw, item = history(home)
            (path.parent / "rollout-duplicate.jsonl").write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                HandoffResolver(home, home / "handoffs").resolve(item)

    def test_known_credentials_cannot_be_sent_in_handoffs(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            home.joinpath("auth.json").write_text(
                json.dumps({"tokens": {"access_token": "sensitive-token"}})
            )
            with self.assertRaisesRegex(ValueError, "request was not sent"):
                assert_no_openai_secrets(b"hello sensitive-token", home)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "env-secret"}):
                with self.assertRaises(ValueError):
                    assert_no_openai_secrets("env-secret", home)
            history(home, "sensitive-token")
            with self.assertRaises(ValueError):
                HandoffResolver(home, home / "handoffs").prepare_thread("thread-123")


if __name__ == "__main__":
    unittest.main()
