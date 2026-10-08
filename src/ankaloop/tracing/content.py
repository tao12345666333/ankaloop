"""Opt-in content capture helpers: redaction, truncation, serialisation."""

from __future__ import annotations

import json
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "token",
    "password",
    "secret",
    "cookie",
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact(value: Any) -> Any:
    """Return a copy of *value* with sensitive mapping values replaced."""
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}…[truncated {omitted} chars]"


def serialize(value: Any, max_chars: int) -> str:
    """Redact, JSON-encode (strings pass through), and truncate *value*."""
    cleaned = redact(value)
    if isinstance(cleaned, str):
        text = cleaned
    else:
        try:
            text = json.dumps(cleaned, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(cleaned)
    return truncate(text, max_chars)


def _message_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "content": content}]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append({"type": "text", "content": item.get("text", "")})
                else:
                    parts.append({"type": item.get("type", "unknown")})
            else:
                parts.append({"type": "text", "content": str(item)})
        return parts
    if content is None:
        return []
    return [{"type": "text", "content": str(content)}]


def messages_to_genai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert chat messages to the ``gen_ai.input.messages`` structure."""
    output: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        parts = _message_parts(message.get("content"))
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            parts.append(
                {
                    "type": "tool_call",
                    "id": tool_call.get("id"),
                    "name": function.get("name") or tool_call.get("name"),
                    "arguments": function.get("arguments", tool_call.get("arguments")),
                }
            )
        if role == "tool":
            parts = [
                {
                    "type": "tool_call_response",
                    "id": message.get("tool_call_id"),
                    "result": message.get("content"),
                }
            ]
        output.append({"role": role, "parts": parts})
    return output
