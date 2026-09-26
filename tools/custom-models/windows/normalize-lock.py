"""Repair release-tag workspace versions only in an isolated build source copy."""

import argparse
from pathlib import Path
import re
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    arguments = parser.parse_args()
    workspace = arguments.workspace.resolve()
    if any(
        (directory / ".git").exists() for directory in (workspace, *workspace.parents)
    ):
        raise ValueError("Normalize an isolated source copy, never a Git checkout.")
    manifest = tomllib.loads((workspace / "Cargo.toml").read_text(encoding="utf-8"))
    workspace_version = manifest["workspace"]["package"]["version"]
    # The tag has five implicit members declared as workspace path dependencies.
    # This local-only enumeration matches Cargo metadata's 155 packages exactly,
    # and does not require a prepopulated Cargo registry or Git dependency cache.
    paths = set(manifest["workspace"]["members"])
    paths.update(
        dependency["path"]
        for dependency in manifest["workspace"]["dependencies"].values()
        if isinstance(dependency, dict) and "path" in dependency
    )
    versions = {}
    for member in sorted(paths):
        directory = (workspace / member).resolve()
        if not directory.is_relative_to(workspace):
            raise ValueError("Local package escapes the staged workspace: " + member)
        package = tomllib.loads((directory / "Cargo.toml").read_text(encoding="utf-8"))[
            "package"
        ]
        version = package["version"]
        if isinstance(version, dict):
            if version != {"workspace": True}:
                raise ValueError(
                    "Unsupported inherited package version: " + package["name"]
                )
            version = workspace_version
        if package["name"] in versions:
            raise ValueError("Duplicate local package name: " + package["name"])
        versions[package["name"]] = version
    lock_path = workspace / "Cargo.lock"
    lock = lock_path.read_text(encoding="utf-8")
    changes = []

    def replace(block):
        content = block.group(0)
        package = tomllib.loads(content)["package"][0]
        name = package["name"]
        if (
            name not in versions
            or "source" in package
            or package["version"] == versions[name]
        ):
            return content
        if package["version"] != "0.0.0":
            raise ValueError("Unexpected local package version: " + name)
        changes.append(name)
        return re.sub(
            r'^version = "0\.0\.0"$',
            'version = "' + versions[name] + '"',
            content,
            count=1,
            flags=re.MULTILINE,
        )

    updated = re.sub(r"\[\[package\]\][\s\S]*?(?=\[\[package\]\]|\Z)", replace, lock)
    lock_path.write_text(updated, encoding="utf-8", newline="\n")
    print(
        "Normalized %d local release-lock versions to %s."
        % (len(changes), workspace_version)
    )


if __name__ == "__main__":
    main()
