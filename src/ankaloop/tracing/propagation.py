"""W3C trace-context propagation helpers.

These wrap ``opentelemetry.propagate`` so call sites never deal with the
``Context`` API directly. All helpers are no-ops when tracing is off.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace


def capture_carrier() -> dict[str, str]:
    """Serialise the current span context into a ``traceparent`` carrier."""
    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return {}
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def attach_carrier(carrier: Mapping[str, str] | None) -> Iterator[None]:
    """Make the span described by *carrier* current for the duration of the block."""
    if not carrier:
        yield
        return
    ctx = propagate.extract(dict(carrier))
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


def hook_env_vars() -> dict[str, str]:
    """Environment variables for a child process to join the current trace."""
    carrier = capture_carrier()
    env: dict[str, str] = {}
    if "traceparent" in carrier:
        env["TRACEPARENT"] = carrier["traceparent"]
    if "tracestate" in carrier:
        env["TRACESTATE"] = carrier["tracestate"]
    return env


def carrier_from_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Extract the propagation headers (lower-cased) from an HTTP header mapping."""
    if not headers:
        return {}
    carrier: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"traceparent", "tracestate", "baggage"} and value:
            carrier[lowered] = value
    return carrier
