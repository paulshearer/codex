"""Verify explicit cancellation terminates a real delegated Windows command."""

import asyncio
import ctypes
from ctypes import wintypes
from pathlib import Path
import uuid

from aiohttp import ClientSession, ClientTimeout
from permissive.credentials import default_home
from permissive.client import SignedSession


async def main():
    directory = Path("C:/codex-build/permissive-acceptance")
    name = "cancel-pid-" + str(uuid.uuid4()) + ".txt"
    pid_file = directory / name
    async with SignedSession(
        timeout=ClientTimeout(total=None, connect=15),
        home=default_home(),
    ) as client:
        response = await client.post(
            "http://127.0.0.1:8789/v1/tasks",
            json={
                "prompt": f"Use code execution to run this PowerShell command with yield_time_ms 1000: [IO.File]::WriteAllText('{name}', $PID.ToString()); Start-Sleep -Seconds 120; Write-Output PERMISSIVE-CANCEL-OK. This is a cancellation verification; run the actual command, then keep waiting for it.",
                "workingDirectory": str(directory),
                "browserMode": "dedicated",
            },
        )
        assert response.status == 202
        task = await response.json()
        print("Cancellation task:", task["id"], flush=True)
        for _ in range(120):
            if pid_file.exists():
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("The delegated command did not start")
        pid = int(pid_file.read_text(encoding="utf-8-sig"))
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        assert handle, "The test command exited before cancellation"
        try:
            response = await client.post(
                "http://127.0.0.1:8789/v1/tasks/" + task["id"] + "/cancel"
            )
            result = await response.json()
            assert result["status"] == "cancelled", result
            assert kernel.WaitForSingleObject(handle, 10000) == 0, (
                "The delegated child process survived cancellation"
            )
            print(
                "Explicit API cancellation stopped the live native command and recorded cancelled status."
            )
        finally:
            kernel.CloseHandle(handle)
            pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
