"""Verify Windows argument quoting, Unicode, stdio and exit propagation."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


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
            "import json,os,sys\nprint(json.dumps({'arguments':sys.argv[1:],'input':sys.stdin.read(),'path_head':os.environ['PATH'].split(os.pathsep)[0]},ensure_ascii=True))\nsys.exit(7)\n",
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
        result = subprocess.run(
            [str(root / "codex-custom.exe"), *arguments],
            env=environment,
            input="stdio preserved 日本\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if result.returncode != 7:
            raise RuntimeError(
                "Exit propagation failed: " + repr((result.returncode, result.stderr))
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
                *arguments,
            ],
            "input": "stdio preserved 日本\n",
            "path_head": str(root / "native"),
        }
        if actual != expected:
            raise RuntimeError("Argument/stdio preservation failed: " + repr(actual))
        print("Windows argument quoting, Unicode, stdio and exit propagation passed.")


if __name__ == "__main__":
    main()
