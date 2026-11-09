import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from permissive.pause_gate import PauseGate, tool_boundary


class PauseGateTests(unittest.TestCase):
    def test_tool_payload_has_no_action_policy_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in ["Get-Process python | Stop-Process", "Stop-Process -Name python", "taskkill /IM node.exe", "killall pwsh"]:
                with self.subTest(command=command):
                    output = io.StringIO()
                    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "exec_command", "tool_input": {"cmd": command}})
                    with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(output):
                        tool_boundary(directory)
                    self.assertEqual(json.loads(output.getvalue()), {})

    def test_native_hook_waits_at_boundary_then_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = PauseGate(Path(directory))
            gate.pause()
            process = subprocess.Popen([sys.executable, "-m", "permissive.pause_gate", directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                process.stdin.write('{"hook_event_name":"PostToolUse"}')
                process.stdin.close()
                process.stdin = None
                deadline = time.monotonic() + 5
                while not gate.reached.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(gate.reached.exists())
                self.assertIsNone(process.poll())
                gate.resume()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout), {})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_cancel_releases_an_inference_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = PauseGate(Path(directory))
            gate.pause()
            errors = []
            def wait():
                try:
                    gate.wait()
                except RuntimeError as error:
                    errors.append(str(error))
            worker = threading.Thread(target=wait)
            worker.start()
            deadline = time.monotonic() + 2
            while not gate.reached.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            gate.close()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["Task cancelled"])


if __name__ == "__main__":
    unittest.main()
