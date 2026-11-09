"""Verify Windows argument quoting, Unicode, stdio and exit propagation."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    package = parser.parse_args().package.resolve()
    with tempfile.TemporaryDirectory(prefix="codex launcher ") as directory:
        root = Path(directory)
        shutil.copy2(package / "codex-custom.exe", root)
        shutil.copytree(package / "python", root / "python")
        module = root / "lib" / "custom_models"
        module.mkdir(parents=True)
        (module / "__init__.py").write_text("", encoding="utf-8")
        (module / "__main__.py").write_text(
            "import json,os,sys\nif '--exit-early' in sys.argv: print('early exit',flush=True); sys.exit(7)\nprint(json.dumps({'arguments':sys.argv[1:],'input':sys.stdin.read(),'path_head':os.environ['PATH'].split(os.pathsep)[0]},ensure_ascii=False))\nprint('stderr 日本',file=sys.stderr,flush=True)\nsys.exit(7)\n",
            encoding="utf-8",
        )
        registry = str(root / "registry with spaces.json")
        home = str(root / "profile with spaces")
        native = str(root / "native with spaces" / "codex.exe")
        arguments = [
            "space separated",
            'embedded"quote',
            "",
            "C:\\ending\\",
            'slashes\\\\"quote',
            "日本",
        ]
        environment = dict(
            os.environ,
            CUSTOM_CODEX_REGISTRY=registry,
            CUSTOM_CODEX_HOME=home,
            CUSTOM_CODEX_NATIVE=native,
        )
        for forwarded in (arguments, ["app-server", *arguments]):
            result = subprocess.run(
                [str(root / "codex-custom.exe"), *forwarded],
                env=environment,
                input="stdio preserved 日本\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            if result.returncode != 7 or result.stderr != "stderr 日本\n":
                raise RuntimeError(
                    "Exit/stderr propagation failed: "
                    + repr((result.returncode, result.stderr))
                )
            actual = json.loads(result.stdout)
            expected = {
                "arguments": [
                    "--registry",
                    registry,
                    "--home",
                    home,
                    "--native",
                    native,
                    *forwarded,
                ],
                "input": "stdio preserved 日本\n",
                "path_head": str(root / "native"),
            }
            if actual != expected:
                raise RuntimeError(
                    "Argument/stdio preservation failed: " + repr(actual)
                )
        process = subprocess.Popen(
            [str(root / "codex-custom.exe"), "app-server", "--exit-early"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            if process.wait(timeout=10) != 7:
                raise RuntimeError(
                    "Hidden child exit was not propagated while caller stdin remained open."
                )
        finally:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        (module / "__main__.py").write_text(
            "import subprocess,sys\n"
            "if '--inherited-pipes' in sys.argv:\n"
            " subprocess.Popen([sys.executable,'-c','import time; time.sleep(8)'],"
            " stdout=sys.stdout,stderr=sys.stderr,close_fds=False)\n"
            " print('parent exited',flush=True);sys.exit(7)\n",
            encoding="utf-8",
        )
        inherited = subprocess.Popen(
            [str(root / "codex-custom.exe"), "app-server", "--inherited-pipes"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            inherited.stdin.close()
            if inherited.wait(timeout=6) != 7:
                raise RuntimeError("Inherited helper pipes delayed app-server exit")
        finally:
            inherited.stdout.close()
            inherited.stderr.close()
            # Let the deliberate inheritor release Windows file handles
            # before TemporaryDirectory removes its executable.
            time.sleep(8)
        print(
            "Windows CLI/hidden app-server quoting, Unicode, stdin/stdout/stderr, EOF and early exit passed."
        )
        shutil.copy2(package / "Codex-Custom-Desktop.exe", root)
        windows = root / "windows"
        windows.mkdir()
        (windows / "launch-desktop.ps1").write_text(
            "$hash = Get-Command Get-FileHash -ErrorAction SilentlyContinue\n"
            "$appx = Get-Command Get-AppxPackage -ErrorAction SilentlyContinue\n"
            "[IO.File]::WriteAllText([IO.Path]::Combine($PSScriptRoot, 'module-result.txt'), [string]([bool]$hash -and [bool]$appx))\n"
            "exit 0\n",
            encoding="utf-8",
        )
        desktop_environment = dict(
            environment, PSModulePath=str(root / "absent modules")
        )
        desktop = subprocess.run(
            [str(root / "Codex-Custom-Desktop.exe")],
            env=desktop_environment,
            timeout=30,
        )
        if (
            desktop.returncode != 0
            or (windows / "module-result.txt").read_text() != "True"
        ):
            raise RuntimeError(
                "Desktop child could not discover Windows PowerShell inbox modules."
            )
        print(
            "Desktop PowerShell modules work with an incompatible inherited PSModulePath."
        )


if __name__ == "__main__":
    main()
