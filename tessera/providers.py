"""Text/function-tool wire adapters; the local harness owns execution and history."""

from abc import ABC, abstractmethod
from collections import OrderedDict
from copy import deepcopy
import json
import threading


def arguments_object(value):
    if isinstance(value, str):
        value = json.loads(value or "{}")
    if not isinstance(value, dict):
        raise ValueError("Expected tool argument object")
    return value


class ProviderAdapter(ABC):
    path = ""

    def __init__(self, replay_cache_size=64):
        if type(replay_cache_size) is not int or replay_cache_size <= 0:
            raise ValueError("Provider replay cache size must be a positive integer")
        self._cache_size = replay_cache_size
        self._replays = OrderedDict()
        self._lock = threading.RLock()

    def headers(self, key, options):
        return {"Authorization": "Bearer " + key} if key else {}

    @abstractmethod
    def encode(self, model, messages, tools, options):
        pass

    @abstractmethod
    def decode(self, data):
        pass

    def _remember(self, calls, native):
        if not calls:
            return
        key = tuple(call["id"] for call in calls)
        with self._lock:
            self._replays[key] = deepcopy(native)
            self._replays.move_to_end(key)
            while len(self._replays) > self._cache_size:
                self._replays.popitem(last=False)

    def _replay(self, calls):
        key = tuple(call["id"] for call in calls)
        with self._lock:
            return deepcopy(self._replays.get(key))


class ChatCompletionsAdapter(ProviderAdapter):
    path = "/chat/completions"

    def encode(self, model, messages, tools, options):
        payload = {"model": model, "messages": deepcopy(messages)}
        payload.update(options)
        payload["stream"] = False
        if tools:
            payload["tools"] = deepcopy(tools)
        return payload

    def decode(self, data):
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise ValueError("Expected text content")
        calls = []
        for call in message.get("tool_calls") or []:
            function = call["function"]
            calls.append({"id": call["id"], "name": function["name"],
                          "arguments": arguments_object(function.get("arguments") or "{}")})
        result = {"content": content, "tool_calls": calls, "usage": data.get("usage", {}),
                  "finish_reason": choice.get("finish_reason")}
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            result["reasoning"] = reasoning
        return result

    @staticmethod
    def assemble(lines, on_delta):
        """Fold a chat-completions SSE stream into one non-streamed response, reporting deltas live."""
        content, reasoning, calls, usage, finish = [], [], {}, {}, None
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
            if line.startswith(":"):
                if "router waiting for serving endpoint" in line:
                    on_delta("status", "Router connected, waiting for upstream output")
                elif line[1:].strip():
                    on_delta("heartbeat", "Provider connection alive")
                continue
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            chunk = json.loads(body)
            if chunk.get("error"):
                # An SSE error is a failed response, never an empty answer.
                raise ValueError("Provider reported a stream error")
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                finish = choice.get("finish_reason") or finish
                thought = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(thought, str) and thought:
                    reasoning.append(thought)
                    on_delta("thought", thought)
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content.append(text)
                    on_delta("text", text)
                for part in delta.get("tool_calls") or []:
                    slot = calls.setdefault(part.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    function = part.get("function") or {}
                    slot["id"] = part.get("id") or slot["id"]
                    if function.get("name") and not slot["name"]:
                        slot["name"] = function["name"]
                        on_delta("tool", slot["name"])
                    slot["arguments"] += function.get("arguments") or ""
        message = {"content": "".join(content), "reasoning_content": "".join(reasoning),
                   "tool_calls": [{"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                  for _, c in sorted(calls.items())]}
        return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}


class ResponsesAdapter(ProviderAdapter):
    path = "/responses"

    def encode(self, model, messages, tools, options):
        payload = dict(options)
        if "previous_response_id" in payload or "conversation" in payload:
            raise ValueError("Provider continuation cannot replace local history")
        if "max_tokens" in payload:
            payload.setdefault("max_output_tokens", payload.pop("max_tokens"))
        items = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                              "output": message["content"]})
                continue
            calls = message.get("tool_calls") or []
            replay = self._replay(calls) if calls else None
            if replay is not None:
                items.extend(replay)
                continue
            if message.get("content") or not calls:
                items.append({"role": role, "content": deepcopy(message.get("content", ""))})
            for call in calls:
                function = call["function"]
                arguments = arguments_object(function.get("arguments") or "{}")
                items.append({"type": "function_call", "call_id": call["id"], "name": function["name"],
                              "arguments": json.dumps(arguments)})
        payload.update(model=model, input=items, stream=False)
        effort = payload.pop('reasoning_effort', None)
        if effort:
            payload['reasoning'] = {'effort': effort}
        payload.setdefault("store", False)
        payload.setdefault("include", ["reasoning.encrypted_content"])
        if tools:
            payload["tools"] = []
            for tool in tools:
                definition = {"type": "function", **deepcopy(tool["function"])}
                # Preserve optional arguments instead of server-side strict normalization.
                definition.setdefault("strict", False)
                payload["tools"].append(definition)
        return payload

    def assemble(self, lines, on_delta):
        """Fold a Responses event stream into the object a plain call returns.

        The ChatGPT Codex backend refuses a non-streamed request with 400
        "Stream must be set to true", so this path is the only way to reach it.
        The final `response.completed` event carries the whole response, which
        is what decode() already knows how to read.
        """
        final = None
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body:
                # A keepalive frame carries no payload. Parsing it as JSON was
                # an empty-body error, not a provider fault.
                continue
            if body == "[DONE]":
                break
            chunk = json.loads(body)
            kind = chunk.get("type") or ""
            if kind == "error":
                detail = chunk.get("error") or {}
                raise ValueError(detail.get("message") or "Provider reported a stream error")
            if kind in ("response.completed", "response.incomplete"):
                final = chunk.get("response") or chunk
                continue
            if kind == "response.output_text.delta":
                text = chunk.get("delta")
                if isinstance(text, str) and text:
                    on_delta("text", text)
                continue
            if kind in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
                thought = chunk.get("delta")
                if isinstance(thought, str) and thought:
                    on_delta("thought", thought)
                continue
            if kind == "response.output_item.added":
                item = chunk.get("item") or {}
                if item.get("type") == "function_call" and item.get("name"):
                    on_delta("tool", item["name"])
        if final is None:
            raise ValueError("Provider stream ended without a response")
        return final

    def decode(self, data):
        status = data["status"]
        if status not in ("completed", "incomplete"):
            raise ValueError("Provider response did not complete")
        text = []
        calls = []
        refused = False
        for item in data["output"]:
            if item["type"] == "function_call":
                calls.append({"id": item["call_id"], "name": item["name"],
                              "arguments": arguments_object(item["arguments"])})
            elif item["type"] == "message":
                for block in item["content"]:
                    if block["type"] == "output_text":
                        text.append(block["text"])
                    elif block["type"] == "refusal":
                        refused = True
                        text.append(block.get("refusal", ""))
            elif item["type"] != "reasoning":
                raise ValueError("Unsupported provider output item")
        finish = "tool_calls" if calls else "stop"
        if status == "incomplete":
            if calls:
                raise ValueError("Incomplete provider tool response")
            reason = data.get("incomplete_details", {}).get("reason")
            finish = "length" if reason == "max_output_tokens" else "content_filter"
        if refused:
            finish = "content_filter"
            if calls:
                raise ValueError("Refused provider tool response")
        if status == "completed" and not calls and not any(text):
            raise ValueError("Provider completed without text or tool calls")
        self._remember(calls, data["output"])
        return {"content": "\n".join(text), "tool_calls": calls,
                "usage": data.get("usage", {}), "finish_reason": finish}


def text_blocks(content):
    if isinstance(content, list):
        return deepcopy(content)
    if not isinstance(content, str):
        raise ValueError("Expected provider text or content blocks")
    return [{"type": "text", "text": content}] if content else []


class AnthropicMessagesAdapter(ProviderAdapter):
    path = "/messages"

    def headers(self, key, options):
        version = options.get("api_version")
        if not isinstance(version, str) or not version:
            raise ValueError("anthropic_messages requires api_version")
        headers = {"anthropic-version": version}
        if key:
            headers["x-api-key"] = key
        return headers

    def encode(self, model, messages, tools, options):
        payload = dict(options)
        if type(payload.get("max_tokens")) is not int or payload["max_tokens"] <= 0:
            raise ValueError("anthropic_messages requires positive max_tokens")
        system = []
        turns = []
        for message in messages:
            role = message["role"]
            if role in ("system", "developer"):
                system.extend(text_blocks(message["content"]))
                continue
            calls = message.get("tool_calls") or []
            if role == "tool":
                role = "user"
                blocks = [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                           "content": deepcopy(message["content"])}]
            else:
                blocks = self._replay(calls) if calls else None
                if blocks is None:
                    blocks = text_blocks(message.get("content", ""))
                    for call in calls:
                        function = call["function"]
                        blocks.append({"type": "tool_use", "id": call["id"], "name": function["name"],
                                       "input": arguments_object(function.get("arguments") or "{}")})
            if role not in ("user", "assistant"):
                raise ValueError("Unsupported provider message role")
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"].extend(blocks)
            else:
                turns.append({"role": role, "content": blocks})
        payload.update(model=model, messages=turns, stream=False)
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = []
            for tool in tools:
                function = tool["function"]
                definition = {"name": function["name"], "input_schema": deepcopy(function["parameters"])}
                for key in ("description", "cache_control"):
                    if key in function:
                        definition[key] = deepcopy(function[key])
                payload["tools"].append(definition)
        return payload

    def decode(self, data):
        if data.get("type") == "error":
            raise ValueError("Provider returned an error")
        text = []
        calls = []
        for block in data["content"]:
            if block["type"] == "text":
                text.append(block["text"])
            elif block["type"] == "tool_use":
                calls.append({"id": block["id"], "name": block["name"],
                              "arguments": arguments_object(block["input"])})
            elif block["type"] not in ("thinking", "redacted_thinking"):
                raise ValueError("Unsupported provider content block")
        reason = data["stop_reason"]
        finish = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
                  "max_tokens": "length"}.get(reason, "content_filter")
        if calls and finish in ("length", "content_filter"):
            raise ValueError("Incomplete provider tool response")
        if not calls and not any(text):
            raise ValueError("Provider completed without text or tool calls")
        self._remember(calls, data["content"])
        return {"content": "\n".join(text), "tool_calls": calls,
                "usage": data.get("usage", {}), "finish_reason": finish}


ADAPTERS = {"openai_chat": ChatCompletionsAdapter, "openai_responses": ResponsesAdapter,
            "anthropic_messages": AnthropicMessagesAdapter}


def create_adapter(kind, replay_cache_size=64):
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        raise ValueError("Unknown provider kind")
    return adapter(replay_cache_size)
