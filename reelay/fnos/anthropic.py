"""Small Anthropic Messages adapter for Reelay's internal chat interface.

No provider credentials are sent to a URL supplied by a model or source message.
JSON-schema requests remain validated by Reelay after generation.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from types import SimpleNamespace as NS


class ProviderError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"Anthropic provider HTTP {status}")
        self.status_code = status


def translate(params: dict) -> dict:
    system, messages = [], []
    for message in params.get("messages", []):
        role = message.get("role")
        if role == "system":
            system.append(str(message.get("content") or ""))
        elif role == "tool":
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": str(message.get("content") or "")}]})
        else:
            content = []
            if message.get("content"):
                content.append({"type": "text", "text": str(message["content"])})
            for call in message.get("tool_calls") or []:
                content.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])})
            messages.append({"role": role, "content": content or [{"type": "text", "text": "Continue."}]})
    # Anthropic expects consecutive user tool results in the same content array.
    merged = []
    for item in messages:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["content"].extend(item["content"])
        else:
            merged.append(item)
    fmt = params.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        system.append("Return only JSON conforming to this schema: " + json.dumps(fmt["json_schema"]["schema"], ensure_ascii=False))
    elif fmt.get("type") == "json_object":
        system.append("Return a JSON object only, without markdown fences.")
    result = {"model": params["model"], "max_tokens": min(int(params.get("max_tokens", 4096)), 8192), "system": "\n\n".join(system), "messages": merged}
    if "temperature" in params:
        result["temperature"] = params["temperature"]
    if params.get("tools"):
        result["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"]["parameters"]} for t in params["tools"]]
    return result


class AnthropicClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url, self.key = base_url.rstrip("/"), api_key
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **params):
        payload = translate(params)
        timeout = min(float(params.get("timeout", 60)), 120)

        def send():
            request = urllib.request.Request(self.base_url + "/v1/messages", data=json.dumps(payload).encode(), headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        raise RuntimeError("Provider response too large")
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                raise ProviderError(exc.code) from None
            except urllib.error.URLError:
                raise ConnectionError("Anthropic provider connection failed") from None

        data = await asyncio.to_thread(send)
        text, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(NS(id=block["id"], function=NS(name=block["name"], arguments=json.dumps(block["input"]))))
        usage = data.get("usage") or {}
        return NS(choices=[NS(message=NS(content="\n".join(text), tool_calls=calls, refusal=None))], usage=NS(prompt_tokens=usage.get("input_tokens", 0), completion_tokens=usage.get("output_tokens", 0), total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0)))
