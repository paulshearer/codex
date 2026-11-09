"""Workspace settings and explicitly assigned local skill documents."""

import os
import ntpath
import copy
import hashlib
from pathlib import Path, PureWindowsPath
import re
import stat
from .browser_settings import browser_settings

MAX_SKILL_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_SKILLS = 32


def normalize_skills(value, owner="workspace"):
    if not isinstance(value, list):
        raise ValueError(f"{owner.capitalize()} skills must be a list of SKILL.md paths")
    skills = {}
    for path in value:
        document = skill_document(path)
        skills.setdefault(directory_key(document["path"]), document["path"])
    if len(skills) > MAX_SKILLS:
        raise ValueError(f"A {owner} may select up to {MAX_SKILLS} skill documents; received {len(skills)} selected skills")
    return list(skills.values())


def normalize_guest_directory(directory):
    """Normalize guest data lexically without consulting the host filesystem."""
    if not isinstance(directory, str) or "\0" in directory:
        raise ValueError("workingDirectory must name an absolute local guest Windows directory")
    path = PureWindowsPath(directory)
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive) or ".." in path.parts:
        raise ValueError("workingDirectory must name an absolute local guest Windows directory without parent traversal")
    return str(PureWindowsPath(ntpath.normpath(directory)))


def directory_key(directory, guest_paths=False):
    if guest_paths:
        return ntpath.normcase(normalize_guest_directory(directory))
    return os.path.normcase(str(Path(directory).resolve()))


def skill_document(value):
    if not isinstance(value, str):
        raise ValueError("Skills must be absolute SKILL.md paths")
    path = Path(value)
    if not path.is_absolute() or path.name.lower() != "skill.md" or not path.is_file():
        raise ValueError(f"Skill must name an existing absolute SKILL.md file: {value}")
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError(f"Skill exceeds the 256 KiB limit: {path}")
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Cannot read skill: {path}") from error
    header = content.split("---", 2)[1] if content.startswith("---") and content.count("---") >= 2 else ""
    def field(name, fallback):
        match = re.search(rf"^{name}:\s*(.+)$", header, re.MULTILINE)
        return match.group(1).strip().strip("\"'") if match else fallback
    return {"path": str(path.resolve()), "name": field("name", path.parent.name),
            "description": field("description", ""), "content": content}


def normalize_workspace(body, current=None, guest_paths=False):
    allowed = {"name", "workingDirectory", "systemPrompt", "skills", "browserMode", "browser", "browserProfile", "executionDetail", "diagnosticLogLevel"}
    if not isinstance(body, dict) or body.keys() - allowed:
        raise ValueError("Expected workspace name, workingDirectory, systemPrompt, skills, browserMode, browser, and browserProfile")
    value = {"systemPrompt": "", "skills": [], "browserMode": "dedicated", **(current or {}), **body}
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise ValueError("Workspace name must be between 1 and 120 characters")
    directory = value.get("workingDirectory")
    if guest_paths:
        directory = normalize_guest_directory(directory)
    elif not isinstance(directory, str) or not Path(directory).is_absolute() or not Path(directory).is_dir():
        raise ValueError("Workspace workingDirectory must be an existing absolute directory")
    prompt = value["systemPrompt"]
    if not isinstance(prompt, str) or "\0" in prompt or len(prompt.encode()) > 128 * 1024:
        raise ValueError("Workspace systemPrompt must be text of at most 128 KiB")
    if value["browserMode"] not in {"dedicated", "attached"}:
        raise ValueError("browserMode must be dedicated or attached")
    skills = normalize_skills(value["skills"])
    detail = value.get("executionDetail", "standard")
    level = value.get("diagnosticLogLevel", "off")
    if detail not in {"minimal", "standard", "full"}:
        raise ValueError("executionDetail must be minimal, standard, or full")
    if level not in {"off", "errors", "info", "debug"}:
        raise ValueError("diagnosticLogLevel must be off, errors, info, or debug")
    normalized = {"name": name.strip(), "workingDirectory": directory if guest_paths else str(Path(directory).resolve()),
                  "systemPrompt": prompt, "skills": skills, "browserMode": value["browserMode"],
                  "executionDetail": detail, "diagnosticLogLevel": level, **browser_settings(value)}
    workspace_context(normalized)
    return normalized


def workspace_context(workspace):
    skills = [skill_document(path) for path in workspace["skills"]]
    context = {"systemPrompt": workspace["systemPrompt"], "skills": skills,
               "revision": workspace.get("revision", 1)}
    if len(compose_instructions(context).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError("The workspace prompt and selected skills exceed the 1 MiB context limit")
    return context


def compose_instructions(context):
    parts = [context.get("systemPrompt", "")]
    if context.get("skills"):
        parts.append("The following skills are assigned to this workspace. Apply them when relevant to the user's task. "
                     "Resolve each skill's relative references against its listed directory; read supporting files as needed.")
    for skill in context.get("skills", []):
        parts.append(f"Assigned skill: {skill['name']}\nSource: {skill['path']}\nDirectory: {Path(skill['path']).parent}\n\n{skill['content']}")
    if context.get("explicitSkills") and context.get("skills"):
        parts.append("The user explicitly selected these skills for this chat. Use their instructions to carry out the current request.")
    if context.get("mode") == "plan":
        parts.append("The user selected Plan for this chat. Investigate using read-only actions and return a concrete implementation plan. Do not edit files, change application data, or execute the proposed implementation. Include unresolved questions in your response instead of using interactive input tools.")
    elif context.get("mode") == "goal":
        parts.append("The user selected Goal for this chat. Pursue the persisted native goal until it is achieved. Use get_goal to inspect it and update_goal to mark it complete only after verification, or blocked according to the tool's rules. Do not create a duplicate goal.")
    return "\n\n".join(part for part in parts if part.strip())


def current_instructions(context):
    return ("<permissive_workspace_context>\n"
            f"Current workspace settings for this task, revision {context.get('revision', 1)}. "
            "These replace earlier workspace prompts and assigned skill documents in this conversation. "
            "Historical assistant messages are not current workspace instructions.\n"
            + (compose_instructions(context) or "Use the application's default instructions. No skills are assigned to this workspace.")
            + ("\nNo skills are currently assigned to this workspace." if not context.get("skills") else "")
            + "\n</permissive_workspace_context>")


def workspace_request(body, context, historical_instructions=()):
    """Refresh workspace developer context without changing user/tool history.

    Native resumed histories retain their initial developer bundle. Replace our
    own section in that bundle, and append the captured current configuration
    on every inference request, including after native compaction.
    """
    body = copy.deepcopy(body)
    items = body.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    for item in items:
        if item.get("role") != "developer":
            continue
        def clean(text):
            text = re.sub(r"<permissive_workspace_context>.*?</permissive_workspace_context>", "", text, flags=re.DOTALL)
            for previous in sorted(set(historical_instructions), key=len, reverse=True):
                if previous:
                    text = re.sub(r"(?m)(?:^|\n)" + re.escape(previous) + r"(?=\n|$)", "", text)
            return text
        if isinstance(item.get("content"), str):
            item["content"] = clean(item["content"])
        elif isinstance(item.get("content"), list):
            for part in item["content"]:
                if part.get("type") == "input_text":
                    part["text"] = clean(part["text"])
    items.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": current_instructions(context)}]})
    body["input"] = items
    return body


def skill_tree_digest(path):
    """Collapse copied packages only when their supporting files also match."""
    root = Path(path).parent
    digest = hashlib.sha256()
    try:
        for file in sorted(root.rglob("*")):
            if file.is_symlink() or getattr(file.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return None
            if file.is_file():
                digest.update(file.relative_to(root).as_posix().encode())
                digest.update(b"\0")
                with file.open("rb") as stream:
                    digest.update(hashlib.file_digest(stream, "sha256").digest())
        return digest.digest()
    except OSError:
        return None


def skill_catalog(home, directory=None, assigned=(), selected=()):
    roots = [home / "skills", Path.home() / ".codex" / "skills"]
    if directory:
        roots.extend(Path(directory) / name / "skills" for name in (".agents", ".codex"))
    paths = set(assigned)
    for root in roots:
        if root.is_dir():
            paths.update(str(path) for path in root.rglob("SKILL.md"))
    values, copies = [], {}
    selected = {directory_key(path) for path in selected}
    # Prefer a workspace's existing selection, then the stable sorted path.
    for path in sorted(paths, key=lambda path: (directory_key(path) not in selected, path)):
        try:
            document = skill_document(path)
        except ValueError:
            continue
        key = (document["name"], document["content"])
        previous = copies.setdefault(key, [])
        if previous:
            fingerprint = skill_tree_digest(document["path"])
            if fingerprint is not None and any(skill_tree_digest(item["path"]) == fingerprint for item in previous):
                if directory_key(document["path"]) not in selected:
                    continue
        previous.append(document)
        values.append({key: value for key, value in document.items() if key != "content"})
    return {"data": sorted(values, key=lambda value: (value["name"].lower(), value["path"]))}
