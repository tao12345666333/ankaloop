"""Span helpers that encode AnkaLoop's span tree and attribute conventions.

Every helper is cheap when tracing is disabled: the global tracer then returns
non-recording spans, and attribute coercion is skipped for them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from ankaloop.tracing import content as content_mod
from ankaloop.tracing import semconv as sc
from ankaloop.tracing.provider import get_settings, get_tracer

AttributeMapping = Mapping[str, Any]


# --- attribute coercion ----------------------------------------------------


def coerce_attribute(value: Any) -> Any:
    """Coerce *value* into something OTel accepts (scalar or homogeneous list)."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        if all(isinstance(item, str) for item in value):
            return list(value)
        if all(isinstance(item, bool) for item in value):
            return list(value)
        if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            return list(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def set_attributes(span: Span, attributes: AttributeMapping) -> None:
    """Set attributes on *span*, dropping ``None`` values and coercing the rest."""
    if not span.is_recording():
        return
    use_openinference = "openinference" in get_settings().compat
    for key, raw in attributes.items():
        value = coerce_attribute(raw)
        if value is None:
            continue
        span.set_attribute(key, value)
        if use_openinference:
            alias = sc.OPENINFERENCE_ALIASES.get(key)
            if alias:
                span.set_attribute(alias, value)


def _span_kind_attributes(operation: str) -> dict[str, Any]:
    attrs: dict[str, Any] = {sc.GEN_AI_OPERATION_NAME: operation}
    if "openinference" in get_settings().compat:
        attrs[sc.OPENINFERENCE_SPAN_KIND] = sc.OPENINFERENCE_SPAN_KINDS.get(operation, "CHAIN")
    return attrs


# --- status helpers --------------------------------------------------------


def mark_cancelled(span: Span) -> None:
    """Record cooperative cancellation without flagging the span as an error."""
    if span.is_recording():
        span.set_attribute(sc.ANKALOOP_CANCELLED, True)
        span.add_event("cancelled")


def record_error(span: Span, exc: BaseException) -> None:
    """Record *exc* on *span* and set ERROR status."""
    if not span.is_recording():
        return
    # ``ProviderError`` carries a stable category (rate_limit, timeout, ...) which is
    # far more useful for dashboards than the class name.
    kind = getattr(exc, "kind", None)
    span.set_attribute(sc.ERROR_TYPE, str(kind) if isinstance(kind, str) else type(exc).__qualname__)
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)[:256]))


@contextmanager
def _managed_span(
    name: str,
    *,
    kind: SpanKind,
    attributes: AttributeMapping,
) -> Iterator[Span]:
    tracer = get_tracer()
    with tracer.start_as_current_span(
        name,
        kind=kind,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        set_attributes(span, attributes)
        try:
            yield span
        except asyncio.CancelledError:
            mark_cancelled(span)
            raise
        except Exception as exc:
            record_error(span, exc)
            raise


# --- span constructors -----------------------------------------------------


@contextmanager
def turn_span(
    agent_name: str,
    *,
    session_id: str | None,
    source: str,
    turn_id: str | None = None,
    attributes: AttributeMapping | None = None,
) -> Iterator[Span]:
    """Root ``invoke_agent`` span for one user turn (or one delegated task)."""
    attrs: dict[str, Any] = {
        **_span_kind_attributes(sc.OPERATION_INVOKE_AGENT),
        sc.GEN_AI_AGENT_NAME: agent_name,
        sc.GEN_AI_CONVERSATION_ID: session_id,
        sc.ANKALOOP_SOURCE: source,
        sc.ANKALOOP_TURN_ID: turn_id,
    }
    if attributes:
        attrs.update(attributes)
    with _managed_span(
        f"{sc.OPERATION_INVOKE_AGENT} {agent_name}",
        kind=SpanKind.SERVER,
        attributes=attrs,
    ) as span:
        yield span


@contextmanager
def chat_span(
    model: str | None,
    *,
    provider: str | None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    attributes: AttributeMapping | None = None,
) -> Iterator[Span]:
    """One ``chat`` span per logical model call (retries stay inside it)."""
    attrs: dict[str, Any] = {
        **_span_kind_attributes(sc.OPERATION_CHAT),
        sc.GEN_AI_REQUEST_MODEL: model,
        sc.GEN_AI_PROVIDER_NAME: provider,
    }
    if tools:
        attrs[sc.ANKALOOP_TOOLS_EXPOSED] = tool_spec_names(tools)
    if attributes:
        attrs.update(attributes)
    settings = get_settings()
    if settings.capture_content and messages:
        attrs[sc.GEN_AI_INPUT_MESSAGES] = content_mod.serialize(
            content_mod.messages_to_genai(messages), settings.content_max_chars
        )
    name = f"{sc.OPERATION_CHAT} {model}" if model else sc.OPERATION_CHAT
    with _managed_span(name, kind=SpanKind.CLIENT, attributes=attrs) as span:
        yield span


def tool_spec_names(tools: list[Any]) -> list[str]:
    """Names of the tools in a provider tool-spec list."""
    return [_tool_spec_name(spec) for spec in tools]


def _tool_spec_name(spec: Any) -> str:
    if isinstance(spec, dict):
        function = spec.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        if spec.get("name"):
            return str(spec["name"])
    return str(getattr(spec, "name", spec))


def record_chat_response(
    span: Span,
    response: Any,
    *,
    estimated_input_tokens: int | None = None,
    context_window: int | None = None,
) -> None:
    """Attach usage, finish reason, and (optionally) output content to a chat span."""
    if not span.is_recording():
        return
    attrs: dict[str, Any] = {}
    usage = getattr(response, "usage", None)
    if usage is not None:
        attrs[sc.GEN_AI_USAGE_INPUT_TOKENS] = getattr(usage, "prompt_tokens", None)
        attrs[sc.GEN_AI_USAGE_OUTPUT_TOKENS] = getattr(usage, "output_tokens", None)
        cached = getattr(usage, "cached_input_tokens", 0)
        if cached:
            attrs[sc.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS] = cached
        cache_write = getattr(usage, "cache_write_input_tokens", 0)
        if cache_write:
            attrs[sc.GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS] = cache_write
        attrs[sc.ANKALOOP_CONTEXT_USAGE_ESTIMATED] = False
    elif estimated_input_tokens is not None:
        attrs[sc.GEN_AI_USAGE_INPUT_TOKENS] = estimated_input_tokens
        attrs[sc.ANKALOOP_CONTEXT_USAGE_ESTIMATED] = True
    if context_window:
        attrs[sc.ANKALOOP_CONTEXT_WINDOW] = context_window

    stop_reason = getattr(response, "stop_reason", None)
    tool_calls = getattr(response, "tool_calls", None)
    if stop_reason:
        attrs[sc.GEN_AI_RESPONSE_FINISH_REASONS] = [str(stop_reason)]
    elif tool_calls:
        attrs[sc.GEN_AI_RESPONSE_FINISH_REASONS] = ["tool_calls"]
    response_model = getattr(response, "model", None)
    if response_model:
        attrs[sc.GEN_AI_RESPONSE_MODEL] = response_model

    settings = get_settings()
    if settings.capture_content:
        parts: list[dict[str, Any]] = []
        text = getattr(response, "content", None)
        if text:
            parts.append({"type": "text", "content": text})
        for call in tool_calls or []:
            parts.append(_tool_call_part(call))
        if parts:
            attrs[sc.GEN_AI_OUTPUT_MESSAGES] = content_mod.serialize(
                [{"role": "assistant", "parts": parts}], settings.content_max_chars
            )
    set_attributes(span, attrs)


def _tool_call_part(call: Any) -> dict[str, Any]:
    """Normalise a provider tool call (dict or object) into a gen_ai message part."""
    if isinstance(call, dict):
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
        return {
            "type": "tool_call",
            "id": call.get("id"),
            "name": function.get("name") or call.get("name"),
            "arguments": function.get("arguments", call.get("arguments")),
        }
    return {
        "type": "tool_call",
        "id": getattr(call, "id", None),
        "name": getattr(call, "name", None),
        "arguments": getattr(call, "raw_arguments", getattr(call, "arguments", None)),
    }


@contextmanager
def tool_span(
    tool_name: str,
    *,
    tool_call_id: str | None,
    step: int | None = None,
    arguments: Any | None = None,
    attributes: AttributeMapping | None = None,
) -> Iterator[Span]:
    """``execute_tool`` span around one tool call, including its hooks."""
    attrs: dict[str, Any] = {
        **_span_kind_attributes(sc.OPERATION_EXECUTE_TOOL),
        sc.GEN_AI_TOOL_NAME: tool_name,
        sc.GEN_AI_TOOL_CALL_ID: tool_call_id,
        sc.GEN_AI_TOOL_TYPE: sc.TOOL_TYPE_FUNCTION,
        sc.ANKALOOP_TOOL_STEP: step,
    }
    if attributes:
        attrs.update(attributes)
    settings = get_settings()
    if settings.capture_content and arguments is not None:
        attrs[sc.GEN_AI_TOOL_CALL_ARGUMENTS] = content_mod.serialize(arguments, settings.content_max_chars)
    with _managed_span(
        f"{sc.OPERATION_EXECUTE_TOOL} {tool_name}",
        kind=SpanKind.INTERNAL,
        attributes=attrs,
    ) as span:
        yield span


def record_tool_result(span: Span, *, success: bool, result: str | None = None) -> None:
    """Record outcome of a tool call on its span."""
    if not span.is_recording():
        return
    attrs: dict[str, Any] = {sc.ANKALOOP_TOOL_SUCCESS: success}
    if result is not None:
        attrs[sc.ANKALOOP_TOOL_RESULT_LENGTH] = len(result)
        settings = get_settings()
        if settings.capture_content:
            attrs[sc.GEN_AI_TOOL_CALL_RESULT] = content_mod.serialize(result, settings.content_max_chars)
    set_attributes(span, attrs)
    if not success:
        span.set_status(Status(StatusCode.ERROR, "tool reported failure"))


def record_tool_denied(span: Span, denied_by: str, reason: str | None = None) -> None:
    """Record that a tool call was refused before execution."""
    if not span.is_recording():
        return
    set_attributes(span, {sc.ANKALOOP_TOOL_DENIED_BY: denied_by, sc.ANKALOOP_TOOL_SUCCESS: False})
    span.add_event("tool.denied", {"denied_by": denied_by, "reason": reason or ""})


@contextmanager
def hook_span(
    event_name: str,
    *,
    handler_count: int,
    attributes: AttributeMapping | None = None,
) -> Iterator[Span]:
    """Span around one hook event's handler execution."""
    attrs: dict[str, Any] = {
        sc.ANKALOOP_HOOK_EVENT: event_name,
        sc.ANKALOOP_HOOK_HANDLER_COUNT: handler_count,
    }
    if attributes:
        attrs.update(attributes)
    with _managed_span(
        f"{sc.SPAN_HOOK_PREFIX} {event_name}",
        kind=SpanKind.INTERNAL,
        attributes=attrs,
    ) as span:
        yield span


@contextmanager
def internal_span(name: str, attributes: AttributeMapping | None = None) -> Iterator[Span]:
    """Generic INTERNAL span (context preparation, compaction, ...)."""
    with _managed_span(name, kind=SpanKind.INTERNAL, attributes=attributes or {}) as span:
        yield span


# --- events and ids --------------------------------------------------------


def mirror_event(event_type: str, data: Mapping[str, Any] | None) -> None:
    """Add an agent timeline event to the current span when it is interesting."""
    if event_type in sc.UNMIRRORED_EVENTS:
        return
    if not event_type.startswith(sc.MIRRORED_EVENT_PREFIXES):
        return
    span = trace.get_current_span()
    if not span.is_recording():
        return
    attributes: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key in {"arguments", "messages", "content"}:
            continue
        coerced = coerce_attribute(value)
        if coerced is not None:
            attributes[key] = coerced
    span.add_event(event_type, attributes)


def current_trace_id() -> str | None:
    """Hex trace id of the current span, or ``None`` when not in a trace."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def current_span() -> Span:
    return trace.get_current_span()
