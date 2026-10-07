"""OpenTelemetry tracing for AnkaLoop.

Call :func:`configure_tracing` once at process start (the CLI and server do
this). Everything else is a thin helper around the OpenTelemetry API that
degrades to no-ops when tracing is disabled.
"""

from __future__ import annotations

from ankaloop.tracing.propagation import (
    attach_carrier,
    capture_carrier,
    carrier_from_headers,
    hook_env_vars,
)
from ankaloop.tracing.provider import (
    TracingSettings,
    build_tracer_provider,
    configure_tracing,
    configure_tracing_from_config,
    get_settings,
    get_tracer,
    resolve_settings,
    shutdown_tracing,
)
from ankaloop.tracing.spans import (
    chat_span,
    current_span,
    current_trace_id,
    hook_span,
    internal_span,
    mark_cancelled,
    mirror_event,
    record_chat_response,
    record_error,
    record_tool_denied,
    record_tool_result,
    set_attributes,
    tool_span,
    tool_spec_names,
    turn_span,
)

__all__ = [
    "TracingSettings",
    "attach_carrier",
    "build_tracer_provider",
    "capture_carrier",
    "carrier_from_headers",
    "chat_span",
    "configure_tracing",
    "configure_tracing_from_config",
    "current_span",
    "current_trace_id",
    "get_settings",
    "get_tracer",
    "hook_env_vars",
    "hook_span",
    "internal_span",
    "mark_cancelled",
    "mirror_event",
    "record_chat_response",
    "record_error",
    "record_tool_denied",
    "record_tool_result",
    "resolve_settings",
    "set_attributes",
    "shutdown_tracing",
    "tool_span",
    "tool_spec_names",
    "turn_span",
]
