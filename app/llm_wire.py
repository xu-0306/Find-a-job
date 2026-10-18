"""Pure request/stream codecs for the three supported LLM wire formats."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping


_PROTOCOLS = {"responses", "chat_completions", "anthropic_messages"}
_ROLES = {"system", "user", "assistant", "tool"}
_INVALID = "invalid LLM wire data"
_UNSUPPORTED = "unsupported LLM protocol"


def _invalid() -> ValueError:
    return ValueError(_INVALID)


def _protocol(protocol: object) -> str:
    if not isinstance(protocol, str) or protocol not in _PROTOCOLS:
        raise ValueError(_UNSUPPORTED)
    return protocol


def _string(value: object, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _invalid()
    return value


def _mapping(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise _invalid()
    return dict(value)


def _tool_specs(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        raise _invalid()
    normalized = []
    for raw in tools:
        tool = _mapping(raw)
        name = _string(tool.get("name"), allow_empty=False)
        parameters = tool.get("parameters")
        if not isinstance(parameters, Mapping):
            raise _invalid()
        description = tool.get("description")
        if description is not None:
            description = _string(description)
        normalized.append({
            "name": name,
            "description": description,
            "parameters": copy.deepcopy(dict(parameters)),
        })
    return normalized


def _tool_calls(raw: object) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid()
    calls = []
    for value in raw:
        call = _mapping(value)
        calls.append({
            "id": _string(call.get("id"), allow_empty=False),
            "name": _string(call.get("name"), allow_empty=False),
            "arguments": _string(call.get("arguments")),
        })
    return calls


def _messages(messages: object) -> list[dict]:
    if not isinstance(messages, list):
        raise _invalid()
    normalized = []
    for raw in messages:
        message = _mapping(raw)
        role = message.get("role")
        if not isinstance(role, str) or role not in _ROLES:
            raise _invalid()
        if "content" not in message:
            raise _invalid()
        content = message["content"]
        # Providers commonly use null for assistant content when only a tool call is present.
        if content is None and role == "assistant":
            content = ""
        if isinstance(content, list) and role == 'user' and content:
            blocks = []
            for raw_block in content:
                block = _mapping(raw_block)
                if block.get('type') == 'text':
                    blocks.append({'type':'text', 'text':_string(block.get('text'))})
                elif block.get('type') == 'image' and block.get('media_type') in ('image/png','image/jpeg','image/webp'):
                    blocks.append({'type':'image', 'media_type':block['media_type'], 'data':_string(block.get('data'),allow_empty=False)})
                else:
                    raise _invalid()
            content = blocks
        else:
            content = _string(content)
        calls = _tool_calls(message.get("tool_calls")) if role == "assistant" else []
        if role != "assistant" and message.get("tool_calls") is not None:
            raise _invalid()
        tool_call_id = message.get("tool_call_id")
        if role == "tool":
            tool_call_id = _string(tool_call_id, allow_empty=False)
        elif tool_call_id is not None:
            raise _invalid()
        normalized.append({
            "role": role,
            "content": content,
            "tool_calls": calls,
            "tool_call_id": tool_call_id,
        })
    return normalized


def _with_description(target: dict, description: str | None) -> None:
    if description is not None:
        target["description"] = description


def _responses_tools(tools: list[dict]) -> list[dict]:
    result = []
    for tool in tools:
        item = {
            "type": "function",
            "name": tool["name"],
            "parameters": copy.deepcopy(tool["parameters"]),
        }
        _with_description(item, tool["description"])
        result.append(item)
    return result


def _chat_tools(tools: list[dict]) -> list[dict]:
    result = []
    for tool in tools:
        function = {
            "name": tool["name"],
            "parameters": copy.deepcopy(tool["parameters"]),
        }
        _with_description(function, tool["description"])
        result.append({"type": "function", "function": function})
    return result


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    result = []
    for tool in tools:
        item = {
            "name": tool["name"],
            "input_schema": copy.deepcopy(tool["parameters"]),
        }
        _with_description(item, tool["description"])
        result.append(item)
    return result


def _json_argument(value: str) -> object:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise _invalid() from None


def _wire_content(protocol: str, content: str | list[dict]) -> str | list[dict]:
    if isinstance(content, str):
        return content
    blocks = []
    for block in content:
        if block['type'] == 'text':
            blocks.append({'type':'input_text' if protocol == 'responses' else 'text', 'text':block['text']})
        elif protocol == 'anthropic_messages':
            blocks.append({'type':'image', 'source':{'type':'base64', 'media_type':block['media_type'], 'data':block['data']}})
        else:
            url = 'data:'+block['media_type']+';base64,'+block['data']
            blocks.append({'type':'input_image','image_url':url} if protocol == 'responses'
                          else {'type':'image_url','image_url':{'url':url}})
    return blocks


def _responses_input(messages: list[dict]) -> tuple[str | None, list[dict]]:
    instructions = []
    items = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if role == "system":
            instructions.append(content)
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message["tool_call_id"],
                "output": content,
            })
            continue
        if content or not message["tool_calls"]:
            items.append({"role": role, "content": _wire_content('responses', content)})
        for call in message["tool_calls"]:
            items.append({
                "type": "function_call",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": call["arguments"],
            })
    return ("\n\n".join(instructions) if instructions else None), items


def _anthropic_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    system = []
    result = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if role == "system":
            system.append(content)
            continue
        if role == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": content,
                }],
            })
            continue
        calls = message["tool_calls"]
        if role == "assistant" and calls:
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in calls:
                blocks.append({
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": _json_argument(call["arguments"]),
                })
            result.append({"role": "assistant", "content": blocks})
        else:
            result.append({"role": role, "content": _wire_content('anthropic_messages', content)})
    return ("\n\n".join(system) if system else None), result


def build_payload(protocol: str, model: str, messages: list[dict], tools: list[dict]) -> dict:
    """Build a streaming request body without mutating caller-owned values."""

    protocol = _protocol(protocol)
    model = _string(model, allow_empty=False)
    normalized_messages = _messages(messages)
    normalized_tools = _tool_specs(tools)

    if protocol == "responses":
        instructions, input_items = _responses_input(normalized_messages)
        payload = {
            "model": model,
            "input": input_items,
            "tools": _responses_tools(normalized_tools),
            "stream": True,
        }
        if instructions is not None:
            payload["instructions"] = instructions
        return payload

    if protocol == "chat_completions":
        chat_messages = []
        for message in normalized_messages:
            item = {"role": message["role"], "content": _wire_content('chat_completions', message["content"])}
            if message["role"] == "assistant" and message["tool_calls"]:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in message["tool_calls"]
                ]
            if message["role"] == "tool":
                item["tool_call_id"] = message["tool_call_id"]
            chat_messages.append(item)
        return {
            "model": model,
            "messages": chat_messages,
            "tools": _chat_tools(normalized_tools),
            "stream": True,
        }

    system, anthropic_messages = _anthropic_messages(normalized_messages)
    payload = {
        "model": model,
        "messages": anthropic_messages,
        "tools": _anthropic_tools(normalized_tools),
        "max_tokens": 4096,
        "stream": True,
    }
    if system is not None:
        payload["system"] = system
    return payload


def _state(state: object) -> dict:
    if not isinstance(state, dict):
        raise _invalid()
    ctx = state.setdefault("_llm_wire", {})
    if not isinstance(ctx, dict):
        raise _invalid()
    ctx.setdefault("done", False)
    ctx.setdefault("failed", False)
    ctx.setdefault("calls", {})
    ctx.setdefault("emitted_calls", set())
    if not isinstance(ctx["calls"], dict) or not isinstance(ctx["emitted_calls"], set):
        raise _invalid()
    return ctx


def _text(ctx: dict, value: object) -> list[dict]:
    return [{"kind": "text_delta", "text": _string(value)}]


def _error(ctx: dict, code: str, message: str) -> list[dict]:
    if ctx["done"] or ctx["failed"]:
        return []
    ctx["failed"] = True
    return [{"kind": "error", "code": code, "message": message}]


def _done(ctx: dict) -> list[dict]:
    if ctx["done"] or ctx["failed"]:
        return []
    ctx["done"] = True
    return [{"kind": "done"}]


def _index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid()
    return value


def _call_event(ctx: dict, key: object, call_id: object, name: object, arguments: object) -> list[dict]:
    if ctx["done"] or ctx["failed"] or key in ctx["emitted_calls"]:
        return []
    call_id = _string(call_id, allow_empty=False)
    name = _string(name, allow_empty=False)
    arguments = _string(arguments)
    ctx["emitted_calls"].add(key)
    return [{"kind": "tool_call", "id": call_id, "name": name, "arguments": arguments}]


def _provider_error(ctx: dict, event: object = None) -> list[dict]:
    if _is_overflow(event):
        return _error(ctx, "context_overflow", "對話內容超過模型可處理的長度。")
    return _error(ctx, "provider_error", "模型服務發生錯誤，請稍後再試。")


def _is_overflow(event: object) -> bool:
    # Only the structured OpenAI-style code is trusted inside a stream; error text is not parsed here.
    if not isinstance(event, Mapping):
        return False
    response = event.get("response")
    candidates = [event, event.get("error"), response.get("error") if isinstance(response, Mapping) else None]
    return any(isinstance(item, Mapping) and item.get("code") == "context_length_exceeded" for item in candidates)


def _response_call(
    ctx: dict,
    key: object,
    *,
    item_id: object | None = None,
    arguments: object | None = None,
    item: object | None = None,
) -> list[dict]:
    entry = ctx["calls"].get(key)
    if entry is None:
        raise _invalid()
    if item is not None:
        call = _mapping(item)
        if call.get("type") != "function_call":
            raise _invalid()
        item_id = call.get("id")
        arguments = call.get("arguments")
        call_id = call.get("call_id")
        name = call.get("name")
        if call_id is not None and call_id != entry.get("call_id"):
            raise _invalid()
        if name is not None and name != entry.get("name"):
            raise _invalid()
    item_id = _string(item_id, allow_empty=False)
    if item_id != entry.get("id"):
        raise _invalid()
    arguments = _string(arguments)
    # Responses has an item id and a logical call_id.  The latter is the id
    # needed by function_call_output and is the unified tool-call id.
    return _call_event(ctx, key, entry.get("call_id"), entry.get("name"), arguments)


def _decode_responses(event: object, ctx: dict) -> list[dict]:
    if not isinstance(event, Mapping):
        raise _invalid()
    event = dict(event)
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise _invalid()

    if event_type == "response.output_text.delta":
        return _text(ctx, event.get("delta"))
    if event_type == "response.output_item.added":
        output_index = _index(event.get("output_index"))
        item = _mapping(event.get("item"))
        item_type = item.get("type")
        if not isinstance(item_type, str):
            raise _invalid()
        if item_type == "function_call":
            ctx["calls"][output_index] = {
                "id": _string(item.get("id"), allow_empty=False),
                "call_id": _string(item.get("call_id"), allow_empty=False),
                "name": _string(item.get("name"), allow_empty=False),
                "arguments": _string(item.get("arguments", "")),
            }
        return []
    if event_type == "response.function_call_arguments.delta":
        output_index = _index(event.get("output_index"))
        item_id = _string(event.get("item_id"), allow_empty=False)
        delta = _string(event.get("delta"))
        entry = ctx["calls"].get(output_index)
        if entry is None or entry.get("id") != item_id:
            raise _invalid()
        entry["arguments"] += delta
        return []
    if event_type == "response.function_call_arguments.done":
        output_index = _index(event.get("output_index"))
        return _response_call(
            ctx,
            output_index,
            item_id=event.get("item_id"),
            arguments=event.get("arguments"),
        )
    if event_type == "response.output_item.done":
        output_index = _index(event.get("output_index"))
        item = _mapping(event.get("item"))
        item_type = item.get("type")
        if not isinstance(item_type, str):
            raise _invalid()
        if item_type != "function_call":
            return []
        return _response_call(ctx, output_index, item=item)
    if event_type == "response.failed" and _is_overflow(event):
        return _provider_error(ctx, event)
    if event_type in {"response.incomplete", "response.failed", "response.cancelled"}:
        return _error(ctx, "incomplete", "模型未完成回覆。")
    if event_type in {"response.refusal.delta", "response.refusal.done"}:
        return _error(ctx, "refusal", "模型拒絕此請求。")
    if event_type == "response.completed":
        response = event.get("response")
        if isinstance(response, Mapping) and response.get("status") in {"incomplete", "failed", "cancelled"}:
            return _error(ctx, "incomplete", "模型未完成回覆。")
        return _done(ctx)
    if event_type == "error":
        return _provider_error(ctx, event)
    # Lifecycle, usage and content bookkeeping events are optional for this
    # codec; only text and completed function calls cross the seam.
    return []


def _chat_call(ctx: dict, key: tuple[int, int], value: object) -> None:
    call = _mapping(value)
    index = _index(call.get("index"))
    function = _mapping(call.get("function"))
    entry = ctx["calls"].setdefault(key, {"id": None, "name": None, "arguments": ""})
    call_id = call.get("id")
    if call_id is not None:
        call_id = _string(call_id, allow_empty=False)
        if entry["id"] is not None and entry["id"] != call_id:
            raise _invalid()
        entry["id"] = call_id
    name = function.get("name")
    if name is not None:
        name = _string(name, allow_empty=False)
        if entry["name"] is not None and entry["name"] != name:
            raise _invalid()
        entry["name"] = name
    arguments = function.get("arguments")
    if arguments is not None:
        entry["arguments"] += _string(arguments)


def _decode_chat(event: object, ctx: dict) -> list[dict]:
    if isinstance(event, str):
        if event == "[DONE]":
            return _done(ctx)
        raise _invalid()
    if not isinstance(event, Mapping):
        raise _invalid()
    event = dict(event)
    if "error" in event or event.get("type") == "error":
        return _provider_error(ctx, event)
    choices = event.get("choices")
    if choices is None:
        return []
    if not isinstance(choices, list):
        raise _invalid()
    output = []
    for choice_index, raw_choice in enumerate(choices):
        choice = _mapping(raw_choice)
        delta = choice.get("delta", {})
        if not isinstance(delta, Mapping):
            raise _invalid()
        delta = dict(delta)
        if "content" in delta and delta["content"] is not None:
            output.extend(_text(ctx, delta["content"]))
        refusal = delta.get("refusal")
        if refusal is not None:
            _string(refusal)
            output.extend(_error(ctx, "refusal", "模型拒絕此請求。"))
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise _invalid()
            for raw_call in tool_calls:
                call = _mapping(raw_call)
                index = _index(call.get("index"))
                _chat_call(ctx, (choice_index, index), call)
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            finish_reason = _string(finish_reason)
            if finish_reason == "tool_calls":
                for key, entry in ctx["calls"].items():
                    if key[0] == choice_index:
                        output.extend(_call_event(ctx, key, entry.get("id"), entry.get("name"), entry.get("arguments", "")))
            elif finish_reason in {"length", "content_filter", "function_call"}:
                output.extend(_error(ctx, "incomplete", "模型未完成回覆。"))
    return output


def _anthropic_call(ctx: dict, index: int) -> list[dict]:
    entry = ctx["calls"].get(index)
    if entry is None:
        raise _invalid()
    arguments = entry["arguments"]
    if arguments == "":
        arguments = json.dumps(entry.get("input", {}), ensure_ascii=False, separators=(",", ":"))
    return _call_event(ctx, index, entry.get("id"), entry.get("name"), arguments)


def _decode_anthropic(event: object, ctx: dict) -> list[dict]:
    if not isinstance(event, Mapping):
        raise _invalid()
    event = dict(event)
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise _invalid()
    if event_type == "content_block_start":
        index = _index(event.get("index"))
        block = _mapping(event.get("content_block"))
        block_type = block.get("type")
        if not isinstance(block_type, str):
            raise _invalid()
        if block_type == "tool_use":
            ctx["calls"][index] = {
                "id": _string(block.get("id"), allow_empty=False),
                "name": _string(block.get("name"), allow_empty=False),
                "input": block.get("input", {}),
                "arguments": "",
            }
            if not isinstance(ctx["calls"][index]["input"], Mapping):
                raise _invalid()
            ctx["calls"][index]["input"] = dict(ctx["calls"][index]["input"])
        return []
    if event_type == "content_block_delta":
        index = _index(event.get("index"))
        delta = _mapping(event.get("delta"))
        delta_type = delta.get("type")
        if not isinstance(delta_type, str):
            raise _invalid()
        if delta_type == "text_delta":
            return _text(ctx, delta.get("text"))
        if delta_type == "input_json_delta":
            entry = ctx["calls"].get(index)
            if entry is None:
                raise _invalid()
            entry["arguments"] += _string(delta.get("partial_json"))
        elif delta_type in {"thinking_delta", "signature_delta"}:
            return []
        return []
    if event_type == "content_block_stop":
        index = _index(event.get("index"))
        if index in ctx["calls"]:
            return _anthropic_call(ctx, index)
        return []
    if event_type == "message_delta":
        delta = _mapping(event.get("delta"))
        stop_reason = delta.get("stop_reason")
        if stop_reason is not None:
            stop_reason = _string(stop_reason)
            if stop_reason == "refusal":
                return _error(ctx, "refusal", "模型拒絕此請求。")
            if stop_reason == "model_context_window_exceeded":
                return _error(ctx, "context_overflow", "對話內容超過模型可處理的長度。")
            if stop_reason in {"max_tokens"}:
                return _error(ctx, "incomplete", "模型未完成回覆。")
        return []
    if event_type == "message_stop":
        return _done(ctx)
    if event_type == "error":
        return _provider_error(ctx)
    # message_start, ping, usage and other bookkeeping events are optional.
    return []


def decode_event(protocol: str, event: object, state: dict) -> list[dict]:
    """Decode one already-parsed stream event into normalized public events."""

    protocol = _protocol(protocol)
    ctx = _state(state)
    if ctx["done"] or ctx["failed"]:
        return []
    if protocol == "responses":
        return _decode_responses(event, ctx)
    if protocol == "chat_completions":
        return _decode_chat(event, ctx)
    return _decode_anthropic(event, ctx)


__all__ = ["build_payload", "decode_event"]
