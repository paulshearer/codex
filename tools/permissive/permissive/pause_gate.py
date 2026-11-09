"""Task-local activity boundary shared by inference and native tool hooks."""

from pathlib import Path
import sys
import threading
import time


class PauseGate:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.requested = self.directory / "requested"
        self.reached = self.directory / "reached"
        self.stopped = threading.Event()
        self.resume()

    def pause(self):
        self.requested.touch()

    def resume(self):
        self.requested.unlink(missing_ok=True)
        self.reached.unlink(missing_ok=True)

    def wait(self):
        while self.requested.exists():
            if self.stopped.is_set():
                raise RuntimeError("Task cancelled")
            self.reached.touch()
            self.stopped.wait(0.05)
        if self.stopped.is_set():
            raise RuntimeError("Task cancelled")

    def close(self):
        self.stopped.set()


def tool_boundary(directory):
    # Drain the native payload before waiting. This hook implements user controls;
    # it does not classify or authorize the tool's contents inside the guest VM.
    sys.stdin.read()
    directory = Path(directory)
    while (directory / "requested").exists():
        (directory / "reached").touch()
        time.sleep(0.05)
    print('{}')


if __name__ == "__main__":
    tool_boundary(sys.argv[1])
