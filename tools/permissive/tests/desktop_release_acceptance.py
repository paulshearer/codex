"""Check the production GUI, signed service startup, and absence of a debugger."""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, ProxyHandler, build_opener

from permissive.auth import Signer, client_key_path

SAFE_REPORT = {}


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def visible_window(pid):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    handles = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(handle, _data):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(handle):
            handles.append(handle)
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(callback, 0)
    return handles[0] if handles else None


def check(package):
    package = package.resolve(strict=True)
    report = SAFE_REPORT
    report.update({"passed": False, "headlessStartup": False, "productionWindow": False, "signedServiceReady": False,
              "rawServiceLogsDisabled": False,
              "debuggerOverridesRejected": False, "serviceSurvivesWindowClose": False})
    with tempfile.TemporaryDirectory(prefix="permissive-rust-release-") as directory:
        home = Path(directory)
        # TemporaryDirectory cleanup is constrained to the known temporary root.
        assert home.resolve().parent == Path(tempfile.gettempdir()).resolve()
        api_port, debug_port = free_port(), free_port()
        env = {**os.environ, "PERMISSIVE_PACKAGE": str(package), "PERMISSIVE_HOME": str(home),
               "PERMISSIVE_PYTHON": str(package / "python/python.exe"),
               "PERMISSIVE_NATIVE": str(package / "native/codex-app-server.exe"),
               "PERMISSIVE_SOURCE_PATH": str(package / "lib"),
               "PERMISSIVE_API_URL": f"http://127.0.0.1:{api_port}",
               "PERMISSIVE_E2E_DEBUG_PORT": str(debug_port),
               "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={debug_port}"}
        child = None
        signer = None
        opener = build_opener(ProxyHandler({}))

        def request(method, route):
            headers = signer.headers(method, route)
            with opener.open(Request(f"http://127.0.0.1:{api_port}{route}", method=method, headers=headers), timeout=3) as response:
                return json.load(response)

        try:
            startup = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                      "-File", str(package / "windows/start-service.ps1"),
                                      "-PackageDirectory", str(package)], cwd=package, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            assert startup.returncode == 0
            report["headlessStartup"] = True
            child = subprocess.Popen([str(package / "CodexPermissive.exe")], cwd=package, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert child.poll() is None
                log = home / "desktop.log"
                if log.exists() and log.read_text(encoding="utf-8").count("service.ready") >= 2:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("Production service startup did not finish")
            signer = Signer(client_key_path(home))
            assert request("GET", "/v1/health")["ok"]
            report["signedServiceReady"] = True
            assert not (home / "service.log").exists()
            assert not (home / "service-errors.log").exists()
            report["rawServiceLogsDisabled"] = True
            handle = visible_window(child.pid)
            assert handle
            report["productionWindow"] = True
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(("127.0.0.1", debug_port)) != 0
            report["debuggerOverridesRejected"] = True
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            assert user.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE, only our window.
            child.wait(timeout=10)
            assert request("GET", "/v1/health")["ok"]
            report["serviceSurvivesWindowClose"] = True
            report["passed"] = True
        finally:
            report['authenticationRegistryCreated'] = (home / 'auth/authorized-keys.json').is_file()
            report['serviceDatabaseCreated'] = (home / 'tasks.sqlite').is_file()
            lifecycle_file = home / 'desktop.log'
            if lifecycle_file.is_file():
                report['serviceReadyRecorded'] = 'service.ready' in lifecycle_file.read_text(encoding='utf-8')
                report['serviceFailureRecorded'] = 'service.failed' in lifecycle_file.read_text(encoding='utf-8')
            # This isolated home/port belongs solely to this check; no live tasks exist.
            if signer is None and client_key_path(home).is_file():
                signer = Signer(client_key_path(home))
            if signer:
                try:
                    request("POST", "/v1/service/shutdown")
                    for _ in range(50):
                        with socket.socket() as probe:
                            if probe.connect_ex(("127.0.0.1", api_port)) != 0:
                                break
                        time.sleep(0.1)
                except (OSError, ValueError):
                    pass
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    try:
        result = check(parser.parse_args().package)
    except Exception as error:
        # A production check must not print exception bodies or private data.
        print(json.dumps({**SAFE_REPORT, "passed": False, "failureType": type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(result))
