"""Translate Responses conversations to Chat Completions without executing tools."""

import base64
import copy
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass


class CompatibilityError(ValueError):
    """A request or completion cannot be represented losslessly by the adapter."""


class ProviderError(CompatibilityError):
    def __init__(self, error):
        self.error = error
        super().__init__(str(error.get("message", "Provider returned an error")))


@dataclass(frozen=True)
class ToolName:
    namespace: str
    name: str
    custom: bool = False
    search: bool = False


REASONING_PREFIX = "custom-models-chat-reasoning-v1:"
CALL_TYPES = {"function_call", "custom_tool_call", "tool_search_call"}


def _wire_name(namespace, name):
    raw = f"{namespace}__{name}" if namespace else name
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
        return raw
    return "tool_" + hashlib.sha256(raw.encode()).hexdigest()[:48]


def flatten_responses(body, chat=False):
    """Copy and flatten registered namespace tools and historical tool names."""
    result = copy.deepcopy(body)
    names = {}
    tools = []
    registered = []
    for entry in result.get("tools", []):
        namespace = entry.get("name", "") if entry.get("type") == "namespace" else ""
        children = (
            entry.get("tools", []) if entry.get("type") == "namespace" else [entry]
        )
        for tool in children:
            search = tool.get("type") == "tool_search" and chat
            if search:
                if namespace or tool.get("execution") != "client":
                    raise CompatibilityError(
                        "Chat supports only client-executed tool search"
                    )
                tool = {**tool, "type": "function", "name": "tool_search"}
            if tool.get("type") not in {"function", "custom"}:
                if namespace:
                    raise CompatibilityError("Unsupported namespaced tool type")
                tools.append(tool)
                continue
            name = tool.get("name") or tool.get("function", {}).get("name")
            if not isinstance(name, str) or not name:
                raise CompatibilityError("Tool name must be a nonempty string")
            if "function" in tool:
                tool = {"type": "function", **tool["function"]}
            registered.append((namespace, name, tool, search))
    candidates = [_wire_name(namespace, name) for namespace, name, _, _ in registered]
    reserved = set(candidates)
    original_names = set()
    reverse = {}
    for (namespace, name, tool, search), candidate in zip(registered, candidates):
        original = (namespace, name)
        if original in original_names:
            raise CompatibilityError("Duplicate registered tool name")
        original_names.add(original)
        alias = candidate
        if candidates.count(candidate) > 1:
            attempt = 0
            while True:
                alias = (
                    "tool_"
                    + hashlib.sha256(
                        json.dumps([namespace, name, attempt]).encode()
                    ).hexdigest()[:48]
                )
                if alias not in reserved and alias not in names:
                    break
                attempt += 1
        names[alias] = ToolName(namespace, name, tool["type"] == "custom", search)
        reverse[original] = alias
        tools.append({**tool, "name": alias})
    if "tools" in result:
        result["tools"] = tools
    for item in (
        result.get("input", []) if isinstance(result.get("input"), list) else []
    ):
        if item.get("type") in CALL_TYPES | {
            "function_call_output",
            "custom_tool_call_output",
        } and item.get("name"):
            namespace = item.pop("namespace", "")
            item["name"] = reverse.get(
                (namespace, item["name"]), _wire_name(namespace, item["name"])
            )
    choice = result.get("tool_choice")
    if isinstance(choice, dict) and choice.get("name"):
        namespace = choice.pop("namespace", "")
        choice["name"] = reverse.get(
            (namespace, choice["name"]), _wire_name(namespace, choice["name"])
        )
    return result, names


def restore_tool_names(value, names):
    """Restore original namespace/name without modifying the caller's object."""
    result = copy.deepcopy(value)

    def visit(item):
        if isinstance(item, dict):
            if item.get("type") in CALL_TYPES and item.get("name") in names:
                original = names[item["name"]]
                item["name"] = original.name
                if original.namespace:
                    item["namespace"] = original.namespace
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(result)
    return result


def validate_call(item):
    if item.get("type") == "tool_search_call":
        arguments = item.get("arguments")
        if (
            not item.get("call_id")
            or item.get("execution") != "client"
            or not isinstance(arguments, dict)
            or item.get("status", "completed") != "completed"
        ):
            raise CompatibilityError("Completed client tool search call is malformed")
        return
    if not item.get("name") or not item.get("call_id"):
        raise CompatibilityError("Completed tool call lacks its name or call ID")
    if item.get("status", "completed") != "completed":
        raise CompatibilityError("Tool call is not completed")
    if item.get("type") == "function_call":
        try:
            arguments = json.loads(item.get("arguments", ""))
        except (ValueError, TypeError):
            raise CompatibilityError(
                "Completed tool call contains invalid JSON arguments"
            ) from None
        if not isinstance(arguments, dict):
            raise CompatibilityError("Function arguments must be a JSON object")
    elif not isinstance(item.get("input"), str):
        raise CompatibilityError("Completed custom tool call lacks string input")


def _content(value, images=False):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if not isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    parts = []
    for part in value:
        if not isinstance(part, dict):
            raise CompatibilityError("Malformed message content")
        kind = part.get("type")
        if kind in {"input_text", "output_text", "text"}:
            parts.append({"type": "text", "text": part.get("text", "")})
        elif kind == "input_image" and images:
            url = part.get("image_url")
            if not isinstance(url, str):
                raise CompatibilityError("Chat image input requires an image URL")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url, "detail": part.get("detail", "auto")},
                }
            )
        else:
            raise CompatibilityError(
                f"Unsupported Chat Completions content type: {kind}"
            )
    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts


def _reasoning(item, model):
    encoded = item.get("encrypted_content", "")
    if encoded is None:
        encoded = ""
    if not isinstance(encoded, str):
        raise CompatibilityError("Reasoning encrypted_content must be a string")
    if encoded.startswith(REASONING_PREFIX):
        if len(encoded) > 4 * 1024 * 1024:
            raise CompatibilityError("Reasoning metadata exceeds the size limit")
        try:
            record = json.loads(
                base64.b64decode(encoded[len(REASONING_PREFIX) :], validate=True)
            )
        except (ValueError, TypeError):
            raise CompatibilityError("Invalid adapter reasoning metadata") from None
        if not isinstance(record, dict) or not isinstance(record.get("text"), str):
            raise CompatibilityError("Invalid adapter reasoning metadata")
        if record.get("model") == model:
            return record["text"]
    return ""


def responses_to_chat(body, model):
    """Return a Chat request and map required to restore its completed calls."""
    body = copy.deepcopy(body)
    # Native discovery returns additional tool definitions in durable history.
    # Register them for the next Chat call while retaining the tool result.
    discovered = []
    for item in body.get("input", []) if isinstance(body.get("input"), list) else []:
        if item.get("type") in {"additional_tools", "tool_search_output"}:
            discovered.extend(item.get("tools", []))
    if discovered:
        existing = body.setdefault("tools", [])
        registered = set()
        for tool in existing:
            namespace = tool.get("name", "") if tool.get("type") == "namespace" else ""
            children = tool.get("tools", []) if namespace else [tool]
            registered.update((namespace, child.get("name")) for child in children)
        for tool in discovered:
            namespace = tool.get("name", "") if tool.get("type") == "namespace" else ""
            children = tool.get("tools", []) if namespace else [tool]
            new_children = []
            for child in children:
                identity = (namespace, child.get("name"))
                if identity not in registered:
                    new_children.append(child)
                    registered.add(identity)
            if new_children:
                existing.append(
                    {**tool, "tools": new_children} if namespace else new_children[0]
                )
    body, names = flatten_responses(body, chat=True)
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    items = body.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    pending_reasoning = ""
    for item in items:
        kind = item.get("type", "message")
        if kind == "reasoning":
            pending_reasoning += _reasoning(item, model.upstream_model)
            continue
        if kind == "tool_search_call":
            if item.get("execution") != "client":
                raise CompatibilityError(
                    "Historical server-executed tool search is unsupported"
                )
            alias = next(
                (wire for wire, original in names.items() if original.search),
                "tool_search",
            )
            item = {
                **item,
                "type": "function_call",
                "name": alias,
                "arguments": json.dumps(item.get("arguments", {})),
            }
            kind = "function_call"
        if kind in CALL_TYPES:
            arguments = (
                item.get("arguments")
                if kind == "function_call"
                else json.dumps({"input": item.get("input", "")})
            )
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            call = {
                "id": item.get("call_id") or item.get("id"),
                "type": "function",
                "function": {"name": item["name"], "arguments": arguments},
            }
            if not call["id"]:
                raise CompatibilityError("Historical tool call lacks a call ID")
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append(
                    {"role": "assistant", "content": None, "tool_calls": [call]}
                )
            if pending_reasoning:
                messages[-1]["reasoning_content"] = pending_reasoning
                pending_reasoning = ""
        elif kind in {
            "function_call_output",
            "custom_tool_call_output",
            "tool_search_output",
        }:
            if not item.get("call_id"):
                raise CompatibilityError("Historical tool output lacks a call ID")
            if kind == "tool_search_output":
                if item.get("execution") != "client":
                    raise CompatibilityError(
                        "Historical server-executed tool search is unsupported"
                    )
                discovered_tools, isolated = flatten_responses(
                    {"tools": item.get("tools", [])}
                )
                discovered_names = []
                for tool in discovered_tools.get("tools", []):
                    # The complete registered set controls collision aliases.
                    original = isolated.get(tool.get("name"))
                    wire = next(
                        (wire for wire, value in names.items() if value == original),
                        tool.get("name"),
                    )
                    discovered_names.append({**tool, "name": wire})
                content = json.dumps({"tools": discovered_names}, ensure_ascii=False)
            else:
                content = _content(item.get("output", ""))
            messages.append(
                {"role": "tool", "tool_call_id": item["call_id"], "content": content}
            )
        elif kind == "message":
            role = item.get("role", "user")
            role = "system" if role == "developer" else role
            if role not in {"system", "user", "assistant"}:
                raise CompatibilityError("Unsupported Chat message role")
            message = {"role": role, "content": _content(item.get("content"), False)}
            if role == "assistant" and pending_reasoning:
                message["reasoning_content"] = pending_reasoning
                pending_reasoning = ""
            messages.append(message)
        elif kind == "additional_tools":
            continue
        else:
            raise CompatibilityError(f"Unsupported historical Responses item: {kind}")
    tools = []
    for tool in body.get("tools", []):
        if tool.get("type") not in {"function", "custom"}:
            raise CompatibilityError(
                "Chat providers require client-executed function tools; unsupported type: "
                + str(tool.get("type"))
            )
        parameters = tool.get("parameters", {"type": "object", "properties": {}})
        if tool["type"] == "custom":
            parameters = {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Exact freeform tool input, including complete apply_patch text.",
                    }
                },
                "required": ["input"],
                "additionalProperties": False,
            }
        function = {"name": tool["name"], "parameters": parameters}
        if tool.get("description"):
            function["description"] = tool["description"]
        if tool.get("strict") is True:
            function["strict"] = True
        tools.append({"type": "function", "function": function})
    result = {
        "model": model.upstream_model,
        "messages": messages,
        "stream": body.get("stream") is True,
        "max_tokens": body.get("max_output_tokens") or model.max_output_tokens,
    }
    effort = (body.get("reasoning") or {}).get("effort") or body.get("reasoning_effort")
    if effort:
        result["reasoning_effort"] = effort
    if tools:
        result["tools"] = tools
        choice = body.get("tool_choice", "auto")
        if isinstance(choice, dict):
            if choice.get("type") not in {"function", "custom"} or not choice.get(
                "name"
            ):
                raise CompatibilityError("Unsupported Chat tool choice")
            choice = {"type": "function", "function": {"name": choice["name"]}}
        result["tool_choice"] = choice
    for key in (
        "temperature",
        "top_p",
        "seed",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "parallel_tool_calls",
    ):
        if key in body:
            result[key] = body[key]
    text_format = (body.get("text") or {}).get("format")
    if text_format and text_format.get("type") == "json_schema":
        result["response_format"] = {
            "type": "json_schema",
            "json_schema": {k: v for k, v in text_format.items() if k != "type"},
        }
    elif text_format and text_format.get("type") == "json_object":
        result["response_format"] = {"type": "json_object"}
    if result["stream"]:
        result["stream_options"] = {"include_usage": True}
    return result, names


def response_usage(usage):
    source = usage or {}
    incoming = source.get("prompt_tokens", source.get("input_tokens", 0))
    outgoing = source.get("completion_tokens", source.get("output_tokens", 0))
    return {
        "input_tokens": incoming,
        "input_tokens_details": {
            "cached_tokens": source.get("prompt_tokens_details", {}).get(
                "cached_tokens", 0
            )
        },
        "output_tokens": outgoing,
        "output_tokens_details": {
            "reasoning_tokens": source.get("completion_tokens_details", {}).get(
                "reasoning_tokens", 0
            )
        },
        "total_tokens": source.get("total_tokens", incoming + outgoing),
    }


class ChatStream:
    """Accumulate Chat deltas and release executable calls only after valid completion."""

    def __init__(self, model, names):
        self.model, self.names = model, names
        self.response = None
        self.text = ""
        self.reasoning = ""
        self.calls = {}
        self.finish_reason = None
        self.usage = None
        self.message_added = False
        self.finished = False
        self.output_bytes = 0
        self.reasoning_bytes = 0

    def _track(self, part, reasoning=False):
        size = len(part.encode("utf-8"))
        if self.output_bytes + size > 8 * 1024 * 1024:
            raise CompatibilityError("Provider output exceeds the adapter size limit")
        if reasoning and self.reasoning_bytes + size > 2 * 1024 * 1024:
            raise CompatibilityError(
                "Provider reasoning exceeds the adapter size limit"
            )
        self.output_bytes += size
        if reasoning:
            self.reasoning_bytes += size

    def _ensure(self, chunk):
        if self.response is not None:
            return []
        identifier = (
            "resp_"
            + re.sub(r"[^A-Za-z0-9_-]", "_", str(chunk.get("id") or uuid.uuid4().hex))[
                :96
            ]
        )
        self.response = {
            "id": identifier,
            "object": "response",
            "created_at": chunk.get("created") or int(time.time()),
            "status": "in_progress",
            "error": None,
            "incomplete_details": None,
            "model": self.model.upstream_model,
            "output": [],
            "usage": None,
        }
        return [
            {"type": "response.created", "response": copy.deepcopy(self.response)},
            {"type": "response.in_progress", "response": copy.deepcopy(self.response)},
        ]

    def feed(self, chunk):
        if self.finished:
            raise CompatibilityError("Provider sent data after stream completion")
        if not isinstance(chunk, dict):
            raise CompatibilityError("Malformed Chat stream event")
        if chunk.get("error"):
            raise ProviderError(chunk["error"])
        events = self._ensure(chunk)
        if chunk.get("usage") is not None:
            self.usage = chunk["usage"]
        choices = chunk.get("choices", [])
        if not choices:
            return events
        if len(choices) != 1 or choices[0].get("index", 0) != 0:
            raise CompatibilityError("Multiple Chat choices are not supported")
        choice = choices[0]
        if choice.get("finish_reason") is not None:
            if self.finish_reason is not None:
                raise CompatibilityError("Provider sent duplicate finish reasons")
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta", {})
        if delta.get("refusal"):
            raise ProviderError(
                {"code": "provider_refusal", "message": delta["refusal"]}
            )
        for key in ("reasoning_content", "reasoning"):
            if isinstance(delta.get(key), str):
                self._track(delta[key], reasoning=True)
                self.reasoning += delta[key]
        text = delta.get("content")
        if text is not None and not isinstance(text, str):
            raise CompatibilityError("Chat stream content must be text")
        if text:
            self._track(text)
            identifier = "msg_" + self.response["id"][5:]
            if not self.message_added:
                item = {
                    "id": identifier,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "", "annotations": []}],
                }
                events.extend(
                    [
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": item,
                        },
                        {
                            "type": "response.content_part.added",
                            "item_id": identifier,
                            "output_index": 0,
                            "content_index": 0,
                            "part": item["content"][0],
                        },
                    ]
                )
                self.message_added = True
            self.text += text
            events.append(
                {
                    "type": "response.output_text.delta",
                    "item_id": identifier,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                }
            )
        for delta_call in delta.get("tool_calls", []):
            index = delta_call.get("index", 0)
            if not isinstance(index, int) or index < 0:
                raise CompatibilityError("Invalid Chat tool index")
            call = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if len(self.calls) > 128:
                raise CompatibilityError(
                    "Provider returned too many parallel tool calls"
                )
            if delta_call.get("id"):
                if call["id"] and call["id"] != delta_call["id"]:
                    raise CompatibilityError("Provider changed a tool call ID")
                call["id"] = delta_call["id"]
            function = delta_call.get("function", {})
            for key in ("name", "arguments"):
                part = function.get(key)
                if part is not None:
                    if not isinstance(part, str):
                        raise CompatibilityError("Chat tool deltas must be strings")
                    self._track(part)
                    call[key] += part
        return events

    def finish(self):
        if self.finished or self.response is None:
            raise CompatibilityError("Provider returned an empty or duplicated stream")
        if self.finish_reason not in {"stop", "tool_calls", "length", "content_filter"}:
            raise CompatibilityError("Chat stream ended without a valid finish reason")
        if self.finish_reason == "tool_calls" and not self.calls:
            raise CompatibilityError(
                "Provider ended with tool_calls but supplied no calls"
            )
        incomplete = self.finish_reason in {"length", "content_filter"}
        output = []
        if self.message_added:
            output.append(
                {
                    "id": "msg_" + self.response["id"][5:],
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": self.text, "annotations": []}
                    ],
                }
            )
        if self.reasoning:
            record = {"model": self.model.upstream_model, "text": self.reasoning}
            output.append(
                {
                    "id": "rs_" + uuid.uuid4().hex,
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": self.reasoning}],
                    "encrypted_content": REASONING_PREFIX
                    + base64.b64encode(json.dumps(record).encode()).decode(),
                }
            )
        if not incomplete:
            for index, call in sorted(self.calls.items()):
                item = {
                    "id": f"fc_{self.response['id'][5:]}_{index}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                }
                validate_call(item)
                original = self.names.get(item["name"])
                if original is None:
                    raise CompatibilityError("Provider returned an unregistered tool")
                if original.custom:
                    wrapper = json.loads(item.pop("arguments"))
                    if not isinstance(wrapper.get("input"), str):
                        raise CompatibilityError(
                            "Custom tool wrapper must contain string input"
                        )
                    item.update(type="custom_tool_call", input=wrapper["input"])
                elif original.search:
                    item.update(
                        type="tool_search_call",
                        execution="client",
                        arguments=json.loads(item["arguments"]),
                    )
                    item.pop("name")
                output.append(restore_tool_names(item, self.names))
        events = []
        for index, item in enumerate(output):
            if item["type"] != "message" or not self.message_added:
                events.append(
                    {
                        "type": "response.output_item.added",
                        "output_index": index,
                        "item": copy.deepcopy(item),
                    }
                )
            if item["type"] in {"function_call", "custom_tool_call"}:
                key, value = (
                    ("input", item["input"])
                    if item["type"] == "custom_tool_call"
                    else ("arguments", item["arguments"])
                )
                event = (
                    "response.custom_tool_call_input.done"
                    if key == "input"
                    else "response.function_call_arguments.done"
                )
                events.append(
                    {
                        "type": event,
                        "item_id": item["id"],
                        "output_index": index,
                        key: value,
                    }
                )
            elif item["type"] == "message":
                events.extend(
                    [
                        {
                            "type": "response.output_text.done",
                            "item_id": item["id"],
                            "output_index": index,
                            "content_index": 0,
                            "text": self.text,
                        },
                        {
                            "type": "response.content_part.done",
                            "item_id": item["id"],
                            "output_index": index,
                            "content_index": 0,
                            "part": item["content"][0],
                        },
                    ]
                )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": copy.deepcopy(item),
                }
            )
        self.response.update(
            status="incomplete" if incomplete else "completed",
            output=output,
            usage=response_usage(self.usage),
            incomplete_details={
                "reason": "max_output_tokens"
                if self.finish_reason == "length"
                else "content_filter"
            }
            if incomplete
            else None,
        )
        events.append(
            {
                "type": "response.incomplete" if incomplete else "response.completed",
                "response": self.response,
            }
        )
        self.finished = True
        return events


def chat_to_response(body, model, names):
    if body.get("error"):
        raise ProviderError(body["error"])
    choices = body.get("choices", [])
    if len(choices) != 1 or not isinstance(choices[0].get("message"), dict):
        raise CompatibilityError("Provider returned no single complete Chat choice")
    choice = choices[0]
    message = choice["message"]
    calls = [
        {**call, "index": index}
        for index, call in enumerate(message.get("tool_calls", []))
    ]
    stream = ChatStream(model, names)
    stream.feed(
        {
            "id": body.get("id"),
            "created": body.get("created"),
            "usage": body.get("usage"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": choice.get("finish_reason"),
                    "delta": {**message, "tool_calls": calls},
                }
            ],
        }
    )
    stream.finish()
    return stream.response
