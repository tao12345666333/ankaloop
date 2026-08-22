"""Unified LLM client abstraction built on any-llm."""

from __future__ import annotations

import asyncio
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import httpx

from .compaction import (
    estimate_request_tokens,
    get_model_context_window,
    get_model_output_limit,
)
from .config import ChatConfig, ModelConfig

# Type aliases for commonly used complex types
ToolCall = dict[str, Any]
Message = dict[str, Any]


class ProviderErrorKind(StrEnum):
    """Stable categories for failures returned by model providers."""

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER = "server"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_OVERFLOW = "context_overflow"
    PROTOCOL = "protocol"
    UNKNOWN = "unknown"


class ProviderError(RuntimeError):
    """A safe, structured provider failure suitable for transport responses."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
        partial_output: bool = False,
    ):
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable
        self.partial_output = partial_output
        status = f" (HTTP {status_code})" if status_code is not None else ""
        messages = {
            ProviderErrorKind.AUTH: "Model provider authentication failed",
            ProviderErrorKind.RATE_LIMIT: "Model provider rate limit was exceeded",
            ProviderErrorKind.TIMEOUT: "Model provider request timed out",
            ProviderErrorKind.CONNECTION: "Could not connect to the model provider",
            ProviderErrorKind.SERVER: "Model provider is temporarily unavailable",
            ProviderErrorKind.INVALID_REQUEST: "Model provider rejected the request",
            ProviderErrorKind.CONTEXT_OVERFLOW: "Model request exceeds the configured context window",
            ProviderErrorKind.PROTOCOL: "Model provider returned an invalid response",
            ProviderErrorKind.UNKNOWN: "Model provider request failed",
        }
        super().__init__(messages[kind] + status)


class ContextOverflowError(ProviderError):
    """A local, non-retryable failure raised before an oversized model request."""

    def __init__(
        self,
        *,
        input_tokens: int,
        input_limit: int,
        context_window: int,
        output_reserve: int,
    ):
        self.input_tokens = input_tokens
        self.input_limit = input_limit
        self.context_window = context_window
        self.output_reserve = output_reserve
        self.timeline_emitted = False
        super().__init__(ProviderErrorKind.CONTEXT_OVERFLOW)


def _provider_status_code(error: BaseException) -> int | None:
    """Extract an HTTP status code from common provider SDK exception shapes."""
    direct = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    value = direct if direct is not None else getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _provider_retry_after(error: BaseException) -> float | None:
    """Extract a numeric Retry-After value when exposed by an SDK response."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_provider_error(error: BaseException, *, partial_output: bool = False) -> ProviderError:
    """Classify an arbitrary provider SDK exception without exposing its message."""
    if isinstance(error, ProviderError):
        if partial_output and not error.partial_output:
            error.partial_output = True
            error.retryable = False
        return error

    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        nested = getattr(current, "original_exception", None)
        current = nested if isinstance(nested, BaseException) else current.__cause__

    status = next((code for item in chain if (code := _provider_status_code(item)) is not None), None)
    names = " ".join(type(item).__name__.lower() for item in chain)
    text = " ".join(str(item).lower() for item in chain)
    if status in {401, 403} or "authentication" in names or "permissiondenied" in names:
        kind = ProviderErrorKind.AUTH
    elif status == 429 or "ratelimit" in names or "rate limit" in text:
        kind = ProviderErrorKind.RATE_LIMIT
    elif any(isinstance(item, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)) for item in chain) or (
        "timeout" in names
    ):
        kind = ProviderErrorKind.TIMEOUT
    elif any(isinstance(item, (ConnectionError, httpx.NetworkError)) for item in chain) or any(
        marker in names for marker in ("connection", "connecterror", "networkerror")
    ):
        kind = ProviderErrorKind.CONNECTION
    elif status is not None and status >= 500:
        kind = ProviderErrorKind.SERVER
    elif status is not None and 400 <= status < 500:
        kind = ProviderErrorKind.INVALID_REQUEST
    elif any(isinstance(item, (ValueError, TypeError)) for item in chain):
        kind = ProviderErrorKind.PROTOCOL
    else:
        kind = ProviderErrorKind.UNKNOWN

    retryable = kind in {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.CONNECTION,
        ProviderErrorKind.SERVER,
    }
    return ProviderError(
        kind,
        status_code=status,
        retry_after=next(
            (value for item in chain if (value := _provider_retry_after(item)) is not None),
            None,
        ),
        retryable=retryable and not partial_output,
        partial_output=partial_output,
    )


def _extract_think_tags(content: str) -> tuple[str | None, str]:
    """Extract content from <think> tags and return (thinking, remaining_content)."""
    pattern = r"<think>(.*?)</think>"
    matches = re.findall(pattern, content, re.DOTALL)
    if matches:
        thinking = "\n".join(m.strip() for m in matches)
        remaining = re.sub(pattern, "", content, flags=re.DOTALL).strip()
        return thinking, remaining
    return None, content


def _response_field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from SDK objects or dictionaries."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _largest_output_limit(values: tuple[Any, ...]) -> int | None:
    """Return the largest explicitly requested output-token limit."""
    limits = [max(0, int(value)) for value in values if value is not None]
    return max(limits) if limits else None


def _first_chat_choice(response: Any) -> Any:
    """Return the first chat-completions choice or raise a clear provider error."""
    choices = _response_field(response, "choices")
    if not choices:
        raise ValueError(
            "Provider returned a chat completion response without choices. "
            "Check that api_type, base_url, and model match the provider's supported API."
        )
    return choices[0]


def _split_response_content(
    content: str | None,
    reasoning_content: str | None,
    *,
    allow_reasoning_as_content: bool,
) -> tuple[str | None, str | None]:
    """Split provider content into user-visible content and hidden thinking."""
    thinking_from_content = None
    if content:
        thinking_from_content, content = _extract_think_tags(content)

    if reasoning_content and not content and allow_reasoning_as_content:
        return thinking_from_content, reasoning_content

    if reasoning_content:
        if thinking_from_content:
            reasoning_content = "\n".join([reasoning_content, thinking_from_content])
        return reasoning_content, content

    return thinking_from_content, content


def _reasoning_content(value: Any) -> str | None:
    """Read normalized any-llm reasoning, with legacy provider fallback."""
    reasoning = _response_field(value, "reasoning")
    if isinstance(reasoning, str):
        return reasoning
    if reasoning is not None:
        content = _response_field(reasoning, "content")
        if content:
            return str(content)
    legacy = _response_field(value, "reasoning_content")
    return str(legacy) if legacy else None


def _extra_content(value: Any) -> dict[str, Any] | None:
    """Extract provider extra_content (e.g., Gemini thought_signature) if present."""
    extra = _response_field(value, "extra_content")
    if isinstance(extra, dict) and extra:
        return extra
    return None


@dataclass
class TokenUsage:
    """Normalized token usage returned by an LLM provider."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Return all tokens occupying the input context."""
        return self.input_tokens + self.cached_input_tokens + self.cache_write_input_tokens


@dataclass
class LLMResponse:
    """Unified response from LLM."""

    content: str | None
    tool_calls: list[ToolCall] | None = None
    stop_reason: str | None = None
    thinking: str | None = None  # Reasoning/thinking content from LLM
    usage: TokenUsage | None = None


def _usage_value(usage: Any, name: str) -> int:
    """Read an integer usage field from SDK objects or dictionaries."""
    if usage is None:
        return 0
    value = usage.get(name, 0) if isinstance(usage, dict) else getattr(usage, name, 0)
    return int(value or 0)


def _usage_details_value(usage: Any, details_name: str, value_name: str) -> int:
    """Read a nested usage detail from SDK objects or dictionaries."""
    if usage is None:
        return 0
    details = usage.get(details_name) if isinstance(usage, dict) else getattr(usage, details_name, None)
    return _usage_value(details, value_name)


def _openai_chat_usage(usage: Any) -> TokenUsage | None:
    """Normalize Chat Completions usage."""
    if usage is None:
        return None
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    output_tokens = _usage_value(usage, "completion_tokens")
    cache_read = _usage_details_value(usage, "prompt_tokens_details", "cached_tokens")
    cache_write = _usage_details_value(usage, "prompt_tokens_details", "cache_write_tokens")
    input_tokens = max(0, prompt_tokens - cache_read - cache_write)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=_usage_value(usage, "total_tokens") or prompt_tokens + output_tokens,
        cached_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
    )


def _responses_usage(usage: Any) -> TokenUsage | None:
    """Normalize OpenAI Responses API usage."""
    if usage is None:
        return None
    provider_input_tokens = _usage_value(usage, "input_tokens")
    output_tokens = _usage_value(usage, "output_tokens")
    cache_read = _usage_details_value(usage, "input_tokens_details", "cached_tokens")
    input_tokens = max(0, provider_input_tokens - cache_read)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=_usage_value(usage, "total_tokens") or provider_input_tokens + output_tokens,
        cached_input_tokens=cache_read,
    )


class BaseLLMClient(ABC):
    """Base class for LLM clients."""

    model: str  # Model name/identifier

    def _configure_request_limits(
        self,
        *,
        model_config: ModelConfig | None = None,
        provider_id: str | None = None,
    ) -> None:
        """Configure model metadata used by the final request-size guard."""
        self._model_config = model_config
        self._provider_id = provider_id
        self._request_limit_cache: dict[str, tuple[int, int]] = {}
        self._context_overflow_callback: Any | None = None

    def set_context_overflow_callback(self, callback: Any | None) -> None:
        """Set a callback invoked when local request validation detects overflow."""
        self._context_overflow_callback = callback

    def _validate_request_size(
        self,
        messages: list[Message],
        tools: list[Message] | None,
        *,
        model: str,
        max_tokens: int | None = None,
    ) -> None:
        """Reject a known-oversized request before invoking a provider SDK."""
        model_config = getattr(self, "_model_config", None)
        configured_model = getattr(self, "model", None)
        model_config_id = getattr(model_config, "model_id", None)
        if (model_config_id and model_config_id != model) or (model != configured_model and model_config_id != model):
            model_config = None
        provider_id = getattr(self, "_provider_id", None)
        limit_cache = getattr(self, "_request_limit_cache", {})
        limits = limit_cache.get(model)
        if limits is None:
            limits = (
                get_model_context_window(
                    model,
                    provider_id=provider_id,
                    model_config=model_config,
                ),
                get_model_output_limit(
                    model,
                    provider_id=provider_id,
                    model_config=model_config,
                ),
            )
            limit_cache[model] = limits
            self._request_limit_cache = limit_cache
        context_window, configured_output = limits
        requested_output = configured_output if max_tokens is None else max(0, int(max_tokens))
        output_reserve = min(requested_output, context_window)
        input_limit = max(context_window - output_reserve, 0)
        input_tokens = estimate_request_tokens(messages, tools)
        if input_tokens > input_limit:
            error = ContextOverflowError(
                input_tokens=input_tokens,
                input_limit=input_limit,
                context_window=context_window,
                output_reserve=output_reserve,
            )
            callback = getattr(self, "_context_overflow_callback", None)
            if callable(callback):
                callback(error)
            raise error

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[Message] | None = None,
        stream_callback: Any | None = None,  # Callable[[str], None]
        **kwargs,
    ) -> LLMResponse:
        """Send chat request and return response."""
        pass

    async def achat(
        self,
        messages: list[Message],
        tools: list[Message] | None = None,
        stream_callback: Any | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send chat asynchronously, adapting legacy synchronous clients."""
        return await asyncio.to_thread(
            self.chat,
            messages,
            tools,
            stream_callback,
            **kwargs,
        )


class AnyLLMClient(BaseLLMClient):
    """Completion client for any provider supported by any-llm."""

    def __init__(
        self,
        provider: str,
        base_url: str | None,
        api_key: str | None,
        model: str,
        model_config: ModelConfig | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        from any_llm import AnyLLM

        client_options: dict[str, Any] = {"max_retries": 0} if provider in {"anthropic", "gmi", "openai"} else {}
        if extra_headers:
            client_options["default_headers"] = dict(extra_headers)
        self.client = AnyLLM.create(
            provider,
            api_key=api_key,
            api_base=base_url,
            **client_options,
        )
        self.provider = provider
        self.model = model
        self._configure_request_limits(model_config=model_config, provider_id=provider)

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream_callback: Any | None = None,
        **kwargs,
    ) -> LLMResponse:
        model = kwargs.pop("model", self.model)
        output_limits = (
            kwargs.get("max_tokens"),
            kwargs.get("max_completion_tokens"),
            kwargs.get("max_output_tokens"),
        )
        self._validate_request_size(
            messages,
            tools,
            model=model,
            max_tokens=_largest_output_limit(output_limits),
        )
        callback = stream_callback
        stream = callback is not None
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "allow_running_loop": True,
            **kwargs,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if stream:
            assert callback is not None
            # Streaming mode
            accumulated_content = []
            accumulated_reasoning = []
            tool_calls_chunks: dict[int, dict[str, Any]] = {}  # index -> accumulated chunk
            finish_reason = None
            usage = None

            response = self.client.completion(**params)

            for chunk in response:
                chunk_usage = _response_field(chunk, "usage")
                if chunk_usage is not None:
                    usage = _openai_chat_usage(chunk_usage)
                # Some APIs (e.g., DeepSeek) may return chunks with empty choices
                choices = _response_field(chunk, "choices")
                if not choices:
                    continue
                delta = _response_field(choices[0], "delta")
                finish_reason = _response_field(choices[0], "finish_reason")

                # Handle content
                delta_content = _response_field(delta, "content")
                if delta_content:
                    callback(delta_content)
                    accumulated_content.append(delta_content)

                # Handle thinking (if present in specific fields)
                delta_reasoning = _reasoning_content(delta)
                if delta_reasoning:
                    accumulated_reasoning.append(delta_reasoning)

                # Handle tool calls (accumulate them)
                delta_tool_calls = _response_field(delta, "tool_calls")
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        idx = _response_field(tc, "index", 0)
                        if idx not in tool_calls_chunks:
                            tool_calls_chunks[idx] = {"id": "", "name": "", "arguments": "", "extra_content": None}

                        tool_call_id = _response_field(tc, "id")
                        if tool_call_id:
                            tool_calls_chunks[idx]["id"] += tool_call_id
                        function = _response_field(tc, "function")
                        if function:
                            name = _response_field(function, "name")
                            arguments = _response_field(function, "arguments")
                            if name:
                                tool_calls_chunks[idx]["name"] += name
                            if arguments:
                                tool_calls_chunks[idx]["arguments"] += arguments
                        extra_content = _extra_content(tc)
                        if extra_content is not None:
                            tool_calls_chunks[idx]["extra_content"] = extra_content

            # Reconstruct full response
            content = "".join(accumulated_content) if accumulated_content else None
            reasoning_text = "".join(accumulated_reasoning) if accumulated_reasoning else None

            tool_calls = []
            if tool_calls_chunks:
                for idx in sorted(tool_calls_chunks.keys()):
                    tc = tool_calls_chunks[idx]
                    tool_call: dict[str, Any] = {"id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]}
                    if tc.get("extra_content"):
                        tool_call["extra_content"] = tc["extra_content"]
                    tool_calls.append(tool_call)

            if reasoning_text and not content and not tool_calls:
                callback(reasoning_text)

            thinking, content = _split_response_content(
                content,
                reasoning_text,
                allow_reasoning_as_content=not tool_calls,
            )

            return LLMResponse(
                content=content,
                tool_calls=tool_calls if tool_calls else None,
                stop_reason=finish_reason,
                thinking=thinking,
                usage=usage,
            )

        else:
            resp = self.client.completion(**params)
            first_choice = _first_chat_choice(resp)
            msg = _response_field(first_choice, "message")
            if msg is None:
                raise ValueError("Provider returned a chat completion choice without a message.")

            tool_calls = None
            message_tool_calls = _response_field(msg, "tool_calls")
            if message_tool_calls:
                tool_calls = [
                    (
                        {
                            "id": _response_field(tc, "id"),
                            "name": _response_field(_response_field(tc, "function"), "name"),
                            "arguments": _response_field(_response_field(tc, "function"), "arguments", "{}"),
                        }
                        | ({"extra_content": extra} if (extra := _extra_content(tc)) else {})
                    )
                    for tc in message_tool_calls
                ]

            # Extract thinking content
            thinking, content = _split_response_content(
                _response_field(msg, "content"),
                _reasoning_content(msg),
                allow_reasoning_as_content=tool_calls is None,
            )

            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                stop_reason=_response_field(first_choice, "finish_reason"),
                thinking=thinking,
                usage=_openai_chat_usage(_response_field(resp, "usage")),
            )

    async def achat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream_callback: Any | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a cancellable asynchronous completion request."""
        model = kwargs.pop("model", self.model)
        output_limits = (
            kwargs.get("max_tokens"),
            kwargs.get("max_completion_tokens"),
            kwargs.get("max_output_tokens"),
        )
        self._validate_request_size(
            messages,
            tools,
            model=model,
            max_tokens=_largest_output_limit(output_limits),
        )
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream_callback is not None,
            **kwargs,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if stream_callback is None:
            response = await self.client.acompletion(**params)
            first_choice = _first_chat_choice(response)
            message = _response_field(first_choice, "message")
            if message is None:
                raise ValueError("Provider returned a chat completion choice without a message.")
            message_tool_calls = _response_field(message, "tool_calls")
            tool_calls = None
            if message_tool_calls:
                tool_calls = [
                    (
                        {
                            "id": _response_field(call, "id"),
                            "name": _response_field(_response_field(call, "function"), "name"),
                            "arguments": _response_field(
                                _response_field(call, "function"),
                                "arguments",
                                "{}",
                            ),
                        }
                        | ({"extra_content": extra} if (extra := _extra_content(call)) else {})
                    )
                    for call in message_tool_calls
                ]
            thinking, content = _split_response_content(
                _response_field(message, "content"),
                _reasoning_content(message),
                allow_reasoning_as_content=tool_calls is None,
            )
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                stop_reason=_response_field(first_choice, "finish_reason"),
                thinking=thinking,
                usage=_openai_chat_usage(_response_field(response, "usage")),
            )

        accumulated_content: list[str] = []
        accumulated_reasoning: list[str] = []
        tool_call_chunks: dict[int, dict[str, Any]] = {}
        finish_reason = None
        usage = None
        response_stream = await self.client.acompletion(**params)
        async for chunk in response_stream:
            chunk_usage = _response_field(chunk, "usage")
            if chunk_usage is not None:
                usage = _openai_chat_usage(chunk_usage)
            choices = _response_field(chunk, "choices")
            if not choices:
                continue
            delta = _response_field(choices[0], "delta")
            finish_reason = _response_field(choices[0], "finish_reason")
            content = _response_field(delta, "content")
            if content:
                stream_callback(content)
                accumulated_content.append(content)
            reasoning = _reasoning_content(delta)
            if reasoning:
                accumulated_reasoning.append(reasoning)
            for call in _response_field(delta, "tool_calls", []) or []:
                index = _response_field(call, "index", 0)
                current = tool_call_chunks.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": "", "extra_content": None},
                )
                current["id"] += _response_field(call, "id", "") or ""
                function = _response_field(call, "function")
                if function:
                    current["name"] += _response_field(function, "name", "") or ""
                    current["arguments"] += _response_field(function, "arguments", "") or ""
                extra = _extra_content(call)
                if extra is not None:
                    current["extra_content"] = extra

        content_text = "".join(accumulated_content) or None
        reasoning_text = "".join(accumulated_reasoning) or None
        tool_calls = [
            ({k: v for k, v in chunk.items() if k != "extra_content" or v})
            for chunk in (tool_call_chunks[index] for index in sorted(tool_call_chunks))
        ] or None
        if reasoning_text and not content_text and not tool_calls:
            stream_callback(reasoning_text)
        thinking, content_text = _split_response_content(
            content_text,
            reasoning_text,
            allow_reasoning_as_content=tool_calls is None,
        )
        return LLMResponse(
            content=content_text,
            tool_calls=tool_calls,
            stop_reason=finish_reason,
            thinking=thinking,
            usage=usage,
        )


class OpenAIClient(AnyLLMClient):
    """Backward-compatible OpenAI completion client."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        model_config: ModelConfig | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__("openai", base_url, api_key, model, model_config, extra_headers)


class AnthropicClient(AnyLLMClient):
    """Backward-compatible Anthropic completion client."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        base_url: str | None = None,
        model_config: ModelConfig | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__("anthropic", base_url, api_key, model, model_config, extra_headers)


class OpenAIResponsesClient(BaseLLMClient):
    """OpenAI Responses API client backed by any-llm."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        model_config: ModelConfig | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        from any_llm import AnyLLM

        client_options: dict[str, Any] = {"max_retries": 0}
        if extra_headers:
            client_options["default_headers"] = dict(extra_headers)
        self.client = AnyLLM.create(
            "openai",
            api_key=api_key,
            api_base=base_url,
            **client_options,
        )
        self.model = model
        self._configure_request_limits(model_config=model_config, provider_id="openai")

    def chat(
        self,
        messages: list[Message],
        tools: list[Message] | None = None,
        stream_callback: Any | None = None,
        **kwargs,
    ) -> LLMResponse:
        # Convert tools to Responses API format
        resp_tools = None
        if tools:
            resp_tools = [
                {
                    "type": "function",
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "parameters": t["function"].get("parameters", {}),
                }
                for t in tools
            ]

        model = kwargs.pop("model", self.model)
        max_tokens = kwargs.pop("max_tokens", None)
        output_limit = _largest_output_limit((max_tokens, kwargs.get("max_output_tokens")))
        self._validate_request_size(
            messages,
            tools,
            model=model,
            max_tokens=output_limit,
        )
        params: dict[str, Any] = {
            "model": model,
            "input_data": cast(Any, self._convert_messages(messages)),
            "tools": resp_tools,
            "allow_running_loop": True,
            **kwargs,
        }
        if max_tokens is not None:
            params["max_output_tokens"] = max_tokens

        if stream_callback:
            completed_response = None
            for event in self.client.responses(stream=True, **params):
                event_type = _response_field(event, "type")
                if event_type == "response.output_text.delta":
                    delta = _response_field(event, "delta")
                    if delta:
                        stream_callback(delta)
                elif event_type == "response.completed":
                    completed_response = _response_field(event, "response")
            if completed_response is None:
                raise ValueError("Provider response stream ended without a completed response.")
            return self._parse_response(completed_response)

        resp = self.client.responses(**params)
        return self._parse_response(resp)

    async def achat(
        self,
        messages: list[Message],
        tools: list[Message] | None = None,
        stream_callback: Any | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a cancellable asynchronous Responses API request."""
        response_tools = None
        if tools:
            response_tools = [
                {
                    "type": "function",
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "parameters": tool["function"].get("parameters", {}),
                }
                for tool in tools
            ]
        model = kwargs.pop("model", self.model)
        max_tokens = kwargs.pop("max_tokens", None)
        output_limit = _largest_output_limit((max_tokens, kwargs.get("max_output_tokens")))
        self._validate_request_size(
            messages,
            tools,
            model=model,
            max_tokens=output_limit,
        )
        params: dict[str, Any] = {
            "model": model,
            "input_data": cast(Any, self._convert_messages(messages)),
            "tools": response_tools,
            **kwargs,
        }
        if max_tokens is not None:
            params["max_output_tokens"] = max_tokens

        if stream_callback is None:
            return self._parse_response(await self.client.aresponses(**params))

        completed_response = None
        events = await self.client.aresponses(stream=True, **params)
        async for event in events:
            event_type = _response_field(event, "type")
            if event_type == "response.output_text.delta":
                delta = _response_field(event, "delta")
                if delta:
                    stream_callback(delta)
            elif event_type == "response.completed":
                completed_response = _response_field(event, "response")
        if completed_response is None:
            raise ValueError("Provider response stream ended without a completed response.")
        return self._parse_response(completed_response)

    @staticmethod
    def _convert_messages(messages: list[Message]) -> list[Message]:
        """Convert AnkaLoop Chat Completions history to Responses input items."""
        converted: list[Message] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id"),
                        "output": message.get("content", ""),
                    }
                )
                continue

            message_tool_calls = message.get("tool_calls")
            if role == "assistant" and message_tool_calls:
                if message.get("content"):
                    converted.append({"role": "assistant", "content": message["content"]})
                for tool_call in message_tool_calls:
                    function = tool_call.get("function", {})
                    converted.append(
                        {
                            "type": "function_call",
                            "call_id": tool_call.get("id"),
                            "name": function.get("name"),
                            "arguments": function.get("arguments", "{}"),
                        }
                    )
                continue

            converted.append({"role": role, "content": message.get("content", "")})
        return converted

    @staticmethod
    def _parse_response(resp: Any) -> LLMResponse:
        """Normalize an any-llm Responses result."""

        # Parse response
        content_parts = []
        tool_calls = []

        for item in _response_field(resp, "output", []):
            if _response_field(item, "type") == "message":
                for block in _response_field(item, "content", []):
                    if _response_field(block, "type") == "output_text":
                        content_parts.append(_response_field(block, "text", ""))
            elif _response_field(item, "type") == "function_call":
                tool_calls.append(
                    {
                        "id": _response_field(item, "call_id"),
                        "name": _response_field(item, "name"),
                        "arguments": _response_field(item, "arguments", "{}"),
                    }
                )

        return LLMResponse(
            content="\n".join(content_parts) if content_parts else None,
            tool_calls=tool_calls if tool_calls else None,
            stop_reason=_response_field(resp, "stop_reason") or _response_field(resp, "status"),
            usage=_responses_usage(_response_field(resp, "usage")),
        )


def _build_request_headers(
    *,
    base_url: str | None,
    api_type: str | None,
    extra_headers: dict[str, str] | None,
) -> dict[str, str] | None:
    """Combine configured headers with automatic OpenRouter app attribution.

    OpenRouter associates API usage with an app via the HTTP-Referer /
    X-OpenRouter-Title headers (see https://openrouter.ai/docs/app-attribution).
    When the target is an OpenRouter endpoint, inject defaults so AnkaLoop
    usage is attributed to the project; explicit user headers always win.
    """
    headers: dict[str, str] = dict(extra_headers) if extra_headers else {}
    is_openrouter = bool(base_url and "openrouter.ai" in base_url) or api_type == "openrouter"
    if is_openrouter:
        headers.setdefault(
            "HTTP-Referer",
            os.environ.get("ANKA_APP_URL", "https://github.com/tao12345666333/ankaloop"),
        )
        headers.setdefault("X-OpenRouter-Title", os.environ.get("ANKA_APP_NAME", "AnkaLoop"))
        categories = os.environ.get("ANKA_APP_CATEGORIES")
        if categories is None:
            headers.setdefault("X-OpenRouter-Categories", "personal-agent")
        elif categories:
            headers.setdefault("X-OpenRouter-Categories", categories)
    return headers or None


def create_llm_client(cfg: ChatConfig | None) -> BaseLLMClient:
    """Create an any-llm client based on config.

    api_type options:
    - Any any-llm provider ID, such as "openai", "anthropic", or "gmi"
    - "openai_responses": OpenAI Responses API
    """
    api_type = (cfg.api_type if cfg else None) or os.environ.get("ANKA_API_TYPE", "openai")
    model = (cfg.model if cfg else None) or "gpt-5.5"
    model_config = cfg.model_config if cfg else None
    if model_config and model_config.model_id and model_config.model_id != model:
        model_config = None

    if api_type == "openai_responses":
        responses_base_url = (cfg.base_url if cfg else None) or os.environ.get(
            "ANKA_OPENAI_BASE", "https://api.openai.com/v1"
        )
        api_key = (cfg.api_key if cfg else None) or os.environ.get("OPENAI_API_KEY")
        return OpenAIResponsesClient(
            base_url=responses_base_url,
            api_key=api_key,
            model=model,
            model_config=model_config,
            extra_headers=_build_request_headers(
                base_url=responses_base_url,
                api_type=api_type,
                extra_headers=cfg.extra_headers if cfg else None,
            ),
        )

    base_url: str | None = cfg.base_url if cfg else None
    if api_type == "openai":
        base_url = base_url or os.environ.get("ANKA_OPENAI_BASE", "https://api.openai.com/v1")
    api_key = cfg.api_key if cfg else None
    if api_type == "openai":
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
    elif api_type == "gmi":
        api_key = api_key or os.environ.get("GMI_API_KEY") or os.environ.get("OPENAI_API_KEY")

    if api_type == "openai":
        assert base_url is not None
        return OpenAIClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            model_config=model_config,
            extra_headers=_build_request_headers(
                base_url=base_url,
                api_type=api_type,
                extra_headers=cfg.extra_headers if cfg else None,
            ),
        )
    if api_type == "anthropic":
        return AnthropicClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            model_config=model_config,
            extra_headers=_build_request_headers(
                base_url=base_url,
                api_type=api_type,
                extra_headers=cfg.extra_headers if cfg else None,
            ),
        )
    return AnyLLMClient(
        provider=api_type,
        base_url=base_url,
        api_key=api_key,
        model=model,
        model_config=model_config,
        extra_headers=_build_request_headers(
            base_url=base_url,
            api_type=api_type,
            extra_headers=cfg.extra_headers if cfg else None,
        ),
    )
