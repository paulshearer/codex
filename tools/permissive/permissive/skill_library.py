"""Portable skill bundles, including relative supporting files and categories."""

import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import uuid
import zipfile

from .workspaces import skill_document

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_FILES = 2048


def category_name(value):
    if not isinstance(value, str) or len(value.strip()) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError("Category must be text of at most 120 characters")
    return value.strip() or "Uncategorized"


def export_bundle(paths, categories):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
        raise ValueError("Select between 1 and 32 skills to export")
    output, manifest, total, count = io.BytesIO(), [], 0, 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, value in enumerate(dict.fromkeys(paths)):
            document = skill_document(value)
            root = Path(document["path"]).parent
            folder = f"skills/{index}"
            manifest.append({"folder": folder, "category": categories.get(document["path"], "Uncategorized")})
            for file in sorted(root.rglob("*")):
                if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"Skill bundles cannot include symbolic links: {file}")
                if not file.is_file():
                    continue
                total += file.stat().st_size
                count += 1
                if total > MAX_BUNDLE_BYTES or count > MAX_BUNDLE_FILES:
                    raise ValueError("Skill bundle exceeds 32 MiB or 2048 files")
                archive.write(file, f"{folder}/{file.relative_to(root).as_posix()}")
        encoded = json.dumps({"format": "codex-permissive-skills", "version": 1, "skills": manifest}).encode("utf-8")
        if total + len(encoded) > MAX_BUNDLE_BYTES:
            raise ValueError("Skill bundle exceeds 32 MiB including its manifest")
        archive.writestr("manifest.json", encoded)
    if output.tell() > MAX_BUNDLE_BYTES:
        raise ValueError("Compressed skill bundle exceeds 32 MiB")
    return output.getvalue()


def import_bundle(home, filename):
    source = Path(filename)
    if not source.is_absolute() or not source.is_file() or source.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Import requires an existing absolute ZIP path of at most 32 MiB")
    target_root = home / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(dir=target_root) as staging:
            entries = archive.infolist()
            if len(entries) > MAX_BUNDLE_FILES + 1 or sum(item.file_size for item in entries) > MAX_BUNDLE_BYTES:
                raise ValueError("Skill bundle exceeds 32 MiB or 2048 files")
            names = set()
            for entry in entries:
                parts = PurePosixPath(entry.filename).parts
                # Windows names, alternate streams, traversal and links are never extracted.
                if (not parts or entry.filename.startswith("/") or "\\" in entry.filename
                        or any(ord(c) < 32 for c in entry.filename)
                        or entry.filename.rstrip("/") != PurePosixPath(entry.filename).as_posix()
                        or any(part in {".", ".."} or ":" in part or part.endswith((".", " "))
                               or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
                               for part in parts)
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.filename.casefold() in names):
                    raise ValueError("Invalid or duplicate path in skill bundle")
                names.add(entry.filename.casefold())
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != "codex-permissive-skills" or manifest.get("version") != 1:
                raise ValueError("Unsupported skill bundle format")
            skills = manifest.get("skills")
            if not isinstance(skills, list) or not 1 <= len(skills) <= 32:
                raise ValueError("A bundle must contain between 1 and 32 skills")
            folders = []
            for skill in skills:
                if not isinstance(skill, dict):
                    raise ValueError("Invalid skill in manifest")
                folder = skill.get("folder")
                if not isinstance(folder, str) or not re.fullmatch(r"skills/[0-9]+", folder) or folder in folders:
                    raise ValueError("Invalid skill folder in manifest")
                skill["category"] = category_name(skill.get("category", "Uncategorized"))
                folders.append(folder)
            for entry in entries:
                if entry.filename == "manifest.json" or entry.is_dir():
                    continue
                if not any(entry.filename.startswith(folder + "/") for folder in folders):
                    raise ValueError("File is outside a manifest skill folder")
                destination = Path(staging).joinpath(*PurePosixPath(entry.filename).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(entry))
            for folder in folders:
                skill_document(str(Path(staging) / folder / "SKILL.md"))
            # Validate everything before publishing; each import gets fresh directories.
            published = []
            try:
                for skill in skills:
                    destination = target_root / ("imported-" + uuid.uuid4().hex)
                    shutil.move(str(Path(staging) / skill["folder"]), destination)
                    published.append(destination)
                return [{"path": str(folder / "SKILL.md"), "category": skill["category"]}
                        for folder, skill in zip(published, skills)]
            except Exception:
                for folder in published:
                    shutil.rmtree(folder)
                raise
    except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError, OSError, RuntimeError) as error:
        raise ValueError(f"Cannot import skill bundle: {error}") from error
