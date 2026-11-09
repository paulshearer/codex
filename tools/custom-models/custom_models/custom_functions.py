"""Represent native freeform tools through function-only Responses providers."""

import copy
import dataclasses
import json


def provider_description(description):
    """Describe the provider's JSON envelope without contradicting native freeform syntax."""
    description = description.replace(
        'Accepts raw JavaScript source text, not JSON, quoted strings, or markdown code fences.',
        'The input field contains JavaScript source text; do not put JSON or Markdown fences inside that string.')
    description = description.replace(
        'This is a FREEFORM tool, so do not wrap the patch in JSON.',
        'The input field contains the complete raw patch text.')
    return ('Call this JSON function with exactly {"input":"<complete raw tool input>"}. '
            'The outer arguments are an object with one string field named input. '
            'For exec, the string is JavaScript; for apply_patch, it is patch text. '
            'Use actual newline characters inside the decoded string. '
            'Nested tools called from JavaScript retain their declared argument types.\n\n' + description).strip()


def wrap_custom_tools(payload, names):
    payload = copy.deepcopy(payload)
    names = {
        name: dataclasses.replace(original, custom_function=original.custom)
        for name, original in names.items()
    }
    for tool in payload.get("tools", []):
        if tool.get("type") == "custom":
            tool.pop("format", None)
            tool.update(
                type="function",
                strict=True,
                parameters={
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "description": "Exact raw tool input with real newline characters; JavaScript for exec, raw patch text for apply_patch.",
                        }
                    },
                    "required": ["input"],
                    "additionalProperties": False,
                },
            )
            tool["description"] = provider_description(tool.get("description", ""))
    for item in payload.get("input", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "custom_tool_call":
            item.update(
                type="function_call", arguments=json.dumps({"input": item.pop("input")})
            )
        elif item.get("type") == "custom_tool_call_output":
            item["type"] = "function_call_output"
    return payload, names
