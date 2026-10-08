"""TracerProvider construction and global installation.

``opentelemetry-api`` is always importable (core dependency); the SDK and
exporters are only imported when tracing is actually enabled, so a default
install pays nothing beyond no-op API calls.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace

from ankaloop._version import __version__
from ankaloop.config import TracingConfig

logger = logging.getLogger(__name__)

_ENDPOINT_ENV_VARS = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
_CAPTURE_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"


@dataclass
class TracingSettings:
    """Effective tracing settings after merging TOML config and ``OTEL_*`` env."""

    enabled: bool = False
    capture_content: bool = False
    content_max_chars: int = 4000
    compat: tuple[str, ...] = ()
    raw: TracingConfig = field(default_factory=TracingConfig)


_settings = TracingSettings()


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def resolve_settings(config: TracingConfig | None) -> TracingSettings:
    """Merge TOML config with ``OTEL_*`` environment variables.

    Precedence:

    * ``OTEL_SDK_DISABLED=true`` always disables tracing.
    * Tracing is enabled when ``[tracing].enabled`` is true, or when
      ``OTEL_TRACES_EXPORTER`` / an OTLP endpoint variable is set.
    * ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`` turns on
      content capture.
    """
    cfg = config or TracingConfig()
    if _env_truthy("OTEL_SDK_DISABLED"):
        enabled = False
    else:
        enabled = cfg.enabled or _env_set("OTEL_TRACES_EXPORTER") or any(_env_set(name) for name in _ENDPOINT_ENV_VARS)
    capture = cfg.capture_content or _env_truthy(_CAPTURE_CONTENT_ENV)
    return TracingSettings(
        enabled=enabled,
        capture_content=capture,
        content_max_chars=max(0, cfg.content_max_chars),
        compat=tuple(c.strip().lower() for c in cfg.compat if c.strip()),
        raw=cfg,
    )


def get_settings() -> TracingSettings:
    """Return the settings installed by :func:`configure_tracing`."""
    return _settings


def _build_exporter(cfg: TracingConfig) -> Any | None:
    exporter_name = os.environ.get("OTEL_TRACES_EXPORTER", "").strip().lower() or cfg.exporter
    exporter_name = exporter_name.strip().lower()

    if exporter_name in {"none", ""}:
        return None
    if exporter_name == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    if exporter_name not in {"otlp", "otlp_proto_http", "otlp_proto_grpc"}:
        logger.warning("Unknown tracing exporter %r; falling back to OTLP", exporter_name)

    protocol = (
        (
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
            or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
            or ("grpc" if exporter_name == "otlp_proto_grpc" else cfg.protocol)
        )
        .strip()
        .lower()
    )

    kwargs: dict[str, Any] = {}
    env_endpoint = any(_env_set(name) for name in _ENDPOINT_ENV_VARS)
    if cfg.endpoint and not env_endpoint:
        base = cfg.endpoint.rstrip("/")
        kwargs["endpoint"] = base if protocol == "grpc" else f"{base}/v1/traces"
    if cfg.headers and not _env_set("OTEL_EXPORTER_OTLP_HEADERS"):
        kwargs["headers"] = dict(cfg.headers)

    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter as GrpcExporter,
            )
        except ImportError as exc:
            raise RuntimeError(
                "gRPC OTLP exporter requested but opentelemetry-exporter-otlp-proto-grpc is not installed"
            ) from exc
        return GrpcExporter(**kwargs)

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )
    except ImportError as exc:
        raise RuntimeError(
            "OTLP exporter requested but opentelemetry-exporter-otlp-proto-http is not "
            "installed; install ankaloop[tracing]"
        ) from exc
    return HttpExporter(**kwargs)


def build_tracer_provider(cfg: TracingConfig) -> Any:
    """Construct an SDK ``TracerProvider`` from *cfg* without installing it.

    Standard ``OTEL_*`` variables win over the TOML values: ``OTEL_SERVICE_NAME``,
    ``OTEL_RESOURCE_ATTRIBUTES``, ``OTEL_TRACES_SAMPLER``, exporter endpoint and
    protocol variables are all honoured by passing nothing explicit when set.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource_attrs: dict[str, Any] = {"service.version": __version__}
    if not _env_set("OTEL_SERVICE_NAME"):
        resource_attrs["service.name"] = cfg.service_name
    resource = Resource.create(resource_attrs)

    sampler = None
    if not _env_set("OTEL_TRACES_SAMPLER"):
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        ratio = min(1.0, max(0.0, cfg.sample_ratio))
        sampler = ParentBased(TraceIdRatioBased(ratio))

    provider = TracerProvider(resource=resource, sampler=sampler)
    exporter = _build_exporter(cfg)
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def _has_real_global_provider() -> bool:
    current = trace.get_tracer_provider()
    return not isinstance(current, (trace.ProxyTracerProvider, trace.NoOpTracerProvider))


def configure_tracing(config: TracingConfig | None) -> TracingSettings:
    """Resolve settings, install a global ``TracerProvider`` when enabled.

    Safe to call more than once; subsequent calls only refresh content-capture
    settings. An existing non-proxy global provider (e.g. installed by the
    embedding application) is never replaced.
    """
    global _settings
    settings = resolve_settings(config)
    _settings = settings
    if not settings.enabled:
        return settings

    if _has_real_global_provider():
        logger.debug("Tracing: reusing existing global TracerProvider")
        return settings

    try:
        provider = build_tracer_provider(settings.raw)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Tracing disabled: %s", exc)
        _settings = TracingSettings(enabled=False, raw=settings.raw)
        return _settings

    trace.set_tracer_provider(provider)
    logger.info("Tracing enabled (exporter=%s)", settings.raw.exporter)
    return settings


def configure_tracing_from_config() -> TracingSettings:
    """Load ``[tracing]`` from the user config and call :func:`configure_tracing`.

    Config load errors are logged and treated as "no TOML tracing section" so a
    broken config file never prevents the CLI or server from starting.
    """
    from ankaloop.config import load_config

    try:
        tracing_config = load_config().tracing
    except Exception as exc:
        logger.debug("Tracing: could not load config (%s); using environment only", exc)
        tracing_config = None
    return configure_tracing(tracing_config)


def shutdown_tracing() -> None:
    """Flush and shut down the global provider if it is an SDK provider."""
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Tracing shutdown failed", exc_info=True)


def get_tracer() -> trace.Tracer:
    """Return the ``ankaloop`` tracer from the current global provider."""
    return trace.get_tracer("ankaloop", __version__)
