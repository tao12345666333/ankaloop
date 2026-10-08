# AnkaLoop Tracing (OpenTelemetry)

AnkaLoop can emit OpenTelemetry traces for every conversation turn. Spans follow the
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/),
so any OTLP-compatible backend works out of the box (Jaeger, Grafana Tempo, Langfuse,
Phoenix, Honeycomb, Datadog, ...).

Tracing is **off by default** and costs nothing when disabled: only the lightweight
`opentelemetry-api` package is a core dependency, and the SDK / exporters are imported
lazily when tracing is enabled.

## Installation

```bash
pip install "ankaloop[tracing]"      # adds opentelemetry-sdk + OTLP HTTP exporter
```

For gRPC export also install `opentelemetry-exporter-otlp-proto-grpc`.

## Configuration

Add a `[tracing]` section to `~/.config/ankaloop/config.toml` (or the project config):

```toml
[tracing]
enabled = true
exporter = "otlp"                    # "otlp" | "console" | "none"
endpoint = "http://localhost:4318"   # OTLP base URL; "/v1/traces" is appended for HTTP
protocol = "http/protobuf"           # "http/protobuf" | "grpc"
service_name = "ankaloop"
sample_ratio = 1.0
capture_content = false              # record prompts / completions / tool args+results
content_max_chars = 4000             # truncate captured content
compat = []                          # e.g. ["openinference"] to dual-write Phoenix/Arize attrs

[tracing.headers]                    # optional exporter headers
Authorization = "Bearer ..."
```

The CLI, the HTTP/WebSocket server, and the Telegram bot all configure tracing once at
process start and flush spans on exit.

### Environment variables

Standard `OTEL_*` variables take precedence over the TOML values, so the same binary can
be pointed at a collector without editing config:

| Variable | Effect |
|----------|--------|
| `OTEL_SDK_DISABLED=true` | Always disables tracing. |
| `OTEL_TRACES_EXPORTER` | `otlp`, `console`, or `none`. Setting it enables tracing. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Collector endpoint. Setting it enables tracing. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` / `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `http/protobuf` or `grpc`. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Exporter headers (overrides `[tracing.headers]`). |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | Resource attributes. |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | Sampler (overrides `sample_ratio`). |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` | Turns on content capture. |

Quick local check without a collector:

```bash
OTEL_TRACES_EXPORTER=console anka --once "list the files in this directory"
```

If another library already installed a global `TracerProvider` in the same process,
AnkaLoop reuses it instead of replacing it.

## Span model

Each user turn produces one trace:

```text
invoke_agent default                         one per turn (root unless caller sent traceparent)
├─ ankaloop.prepare_context                  system prompt, skills, tool selection
├─ ankaloop.compact_context                  only when compaction runs
├─ chat gpt-4o                               one per logical LLM call (retries = events)
├─ execute_tool bash                         one per tool call
│  ├─ ankaloop.hook PreToolUse               hook handlers, if configured
│  └─ ankaloop.hook PostToolUse
├─ execute_tool task
│  └─ invoke_agent focused_coder             subagent turn, same trace
└─ chat gpt-4o
```

Key attributes:

| Span | Attributes |
|------|------------|
| `invoke_agent` | `gen_ai.agent.name`, `gen_ai.conversation.id` (session id), `ankaloop.turn.id`, `ankaloop.source` (`cli`/`server`/`telegram`/`task`), `ankaloop.turn.priority`, `ankaloop.turn.queue_wait_ms`, `ankaloop.turn.steps`, `ankaloop.turn.llm_calls`, `ankaloop.turn.tool_calls`, `ankaloop.context.tokens`, `ankaloop.context.window`, `ankaloop.task.*` for subagents |
| `chat` | `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `ankaloop.context.usage_estimated` (true when the provider returned no usage) |
| `execute_tool` | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type` (`function` or `extension` for MCP), `ankaloop.mcp.server`, `ankaloop.tool.step`, `ankaloop.tool.operation_id`, `ankaloop.tool.success`, `ankaloop.tool.result_length`, `ankaloop.tool.denied_by` (`arguments`/`capability`/`hook`) |
| `ankaloop.hook` | `ankaloop.hook.event`, `ankaloop.hook.handler_count`, `ankaloop.hook.decision`, `ankaloop.hook.success` |

Existing runtime events (`provider.retry`, `context.compacted`, `tool.intent`,
`tool.settled`, `memory.review_*`, ...) are mirrored as span events on the active span.

Status rules:

- A tool failure marks its `execute_tool` span `ERROR` but not the turn.
- A provider failure after retries marks the `chat` span and the turn `ERROR` with
  `error.type`.
- A cancelled turn (`/stop`, Telegram `/new`, WebSocket cancel) sets
  `ankaloop.cancelled = true` and leaves the status unset; it is not an error.

## Content capture

By default spans contain **no** prompt text, model output, tool arguments, or tool
results — only counts, names, and ids. Set `capture_content = true` (or the
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` env var) to record them as
`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`,
`gen_ai.tool.call.arguments`, and `gen_ai.tool.call.result`.

Captured content is passed through a redactor that replaces the values of
sensitive-looking keys in tool arguments and results (`api_key`, `token`,
`authorization`, `password`, `secret`, `cookie`, ...) with `[REDACTED]`, then truncated
to `content_max_chars`. Free-form text is not scanned, so review your backend's
retention policy before enabling this in production.

## Context propagation

- **Inbound HTTP/WebSocket**: `traceparent` / `tracestate` headers are honoured, so the
  `invoke_agent` span becomes a child of the caller's span. The `POST
  /sessions/{id}/prompt` response and every `turn.*` event include `trace_id`.
- **Hooks**: command and Python hook subprocesses receive `TRACEPARENT` and `TRACESTATE`
  in their environment and can continue the trace with any OTel SDK.
- **Subagents**: `task` delegation runs in the same trace; the subagent's
  `invoke_agent` span is a child of the parent's `execute_tool task` span.
- **CLI / Telegram**: each prompt starts a new trace.

## Troubleshooting

- No spans? Check `anka` logs at DEBUG level for `Tracing: ...` messages; the most
  common cause is `enabled = false` with no `OTEL_*` variable set.
- `RuntimeError: OTLP exporter requested but ... not installed`: install
  `ankaloop[tracing]`.
- Spans appear but have no parent from your HTTP client: make sure the client injects
  W3C `traceparent` headers on the prompt request (or WebSocket handshake).
