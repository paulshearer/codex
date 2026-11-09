"""Validated chat overrides and native collaboration-mode parameters."""

from .workspaces import normalize_skills

CHAT_FIELDS = {"browserMode", "browser", "browserProfile", "skills", "mode", "goal"}


def chat_settings(body):
    result = {}
    if "skills" in body:
        result["skills"] = normalize_skills(body["skills"], "chat")
    mode = body.get("mode", "default")
    if mode not in {"default", "plan", "goal"}:
        raise ValueError("mode must be default, plan, or goal")
    goal = body.get("goal", "")
    if not isinstance(goal, str) or "\0" in goal or len(goal.encode()) > 8192:
        raise ValueError("Goal objective must be text of at most 8 KiB")
    result.update(mode=mode, goal=goal.strip() if mode == "goal" else "")
    return result


def collaboration_mode(request, model):
    return {"mode": "plan" if request.get("mode") == "plan" else "default",
            "settings": {"model": model, "reasoning_effort": "high", "developer_instructions": None}}
