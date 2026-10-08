# AnkaLoop Phase 11: Tracing (OpenTelemetry)

Status: design, not implemented.

## Conclusion

OpenTelemetry is sufficient for agent tracing in AnkaLoop. The OpenTelemetry GenAI
semantic conventions (repository `open-telemetry/semantic-conventions-genai`) already
define the three span types this runtime needs:

| AnkaLoop concept | GenAI span | `gen_ai.operation.name` |
|------------------|-----------|-------------------------|
| One conversation turn (`Agent._process_turn_request`) | `invoke_agent {agent}` | `invoke_agent` |
| One provider call (`Agent._call_llm`) | `chat {model}` | `chat` |
| One tool call (`ToolLoop`, built-in or MCP) | `execute_tool {tool}` | `execute_tool` |
| Delegated subagent (`TaskManager._execute_task`) | nested `invoke_agent {agent}` | `invoke_agent` |

Everything AnkaLoop-specific (queueing, compaction, memory review, hooks, tool journal,
recovery) maps onto span attributes and span events under an `ankaloop.*` namespace.
No custom protocol is needed: any OTLP backend works (Jaeger, Tempo/Grafana, Langfuse,
Phoenix, Datadog, Honeycomb).

Two caveats shape the design:

1. The `gen_ai.*` conventions are still **Development** status. All attribute names live in
   one module (`tracing/semconv.py`) so a rename is a one-file change.
2. Prompt/response/tool-argument content is **opt-in** in the conventions and is sensitive.
   It is off by default and goes through one redaction/truncation path.

## What exists today and why it is not enough

- `Agent._emit_event` fans out ~25 event types (`turn.*`, `tool.*`, `provider.*`, `llm.*`,
  `context.*`, `memory.*`) to callbacks and to `SessionTimelineStore` (JSONL per session).
- `event_bus.py` has a second, process-wide bus (`EventType.TASK_*`, `SUBAGENT_*`).
- `ToolJournal` records T1/T2 durability boundaries per tool call.
- `docs/design/phase10-anchor.md` proposed a step recorder; it was never implemented.

These are flat, per-session event streams. They have no causal tree (which LLM call
produced which tool call, which tool call spawned which subagent), no cross-process
identity (HTTP request → turn → hook subprocess → MCP server), and no exporter. Tracing
adds the tree and the export; it does not replace the timeline, which remains the durable
local record.

## Span model

```diagram
[HTTP/WS request span, if the caller sent traceparent]            (optional parent)
└─ invoke_agent default                                            Agent._process_turn_request
   │  gen_ai.conversation.id = session_id   ankaloop.turn.id       ankaloop.source=cli|server|telegram
   │  ankaloop.turn.queue_wait_ms           ankaloop.turn.status   ankaloop.context.*
   ├─ ankaloop.prepare_context                                     system prompt + tool build
   │  ├─ [event] context.tools_filtered / skills_selected            (existing events)
   │  ├─ invoke_agent memory_review  →  chat {model}                 pre-compaction flush
   │  └─ ankaloop.compact_context    →  chat {model}                 SmartCompactor
   ├─ chat gpt-5.5                                                  Agent._call_llm (1 span per logical call)
   │     gen_ai.usage.*  gen_ai.response.finish_reasons
   │     [event] provider.retry (attempt, delay)   [event] context.overflow
   ├─ execute_tool bash                                             ToolLoop
   │     gen_ai.tool.call.id  gen_ai.tool.type=function
   │     ankaloop.tool.recovery_mode  ankaloop.tool.operation_id
   │     [event] tool.intent  [event] tool.settled  [event] hook.decision
   │     ├─ ankaloop.hook PreToolUse                                   each handler batch
   │     └─ ankaloop.hook PostToolUse
   ├─ execute_tool task                                             delegation
   │     ankaloop.task.id  ankaloop.task.agent_type
   │     └─ invoke_agent focused_coder                              subagent, same trace
   │          ├─ chat ...
   │          └─ execute_tool ...
   ├─ execute_tool mcp__github__search                              gen_ai.tool.type=extension
   │     ankaloop.mcp.server=github
   └─ chat gpt-5.5                                                  final answer
```

Rules:

- One `chat` span per logical call, not per retry attempt. Retries become `provider.retry`
  span events; the span records the final outcome. This keeps token accounting one-to-one
  with `Agent._record_llm_usage`.
- Streaming chunks (`message.chunk`) are never recorded on spans.
- The subagent `invoke_agent` span is a child of the `execute_tool task` span that created
  it, not of the parent turn, so delegation depth is visible in the tree. Because
  `TaskTool` returns before the task finishes, the parent span usually ends before the
  child; that is valid in OpenTelemetry.
- Span status is `ERROR` with `error.type` set only when the span's own operation failed.
  A tool returning `success=False` is `ERROR` on the `execute_tool` span but the turn
  still ends `OK` if the model recovered.
- `asyncio.CancelledError` is not an error: the span gets `ankaloop.cancelled = true` and
  status `UNSET`. Telegram `/new` and `cancel_active` cancel turns routinely; recording
  them as exceptions would make error dashboards useless.
- Each `chat` span records the names of the tools exposed in that request
  (`ankaloop.tools.exposed`, a string list) so the effect of the progressive tool view is
  visible per call. Full schemas (`gen_ai.tool.definitions`) are opt-in with content.

### Attributes

GenAI conventions used (names verified against `model/gen-ai/registry.yaml`):

| Attribute | Source in AnkaLoop |
|-----------|--------------------|
| `gen_ai.operation.name` | fixed per span type |
| `gen_ai.provider.name` | `ChatConfig.api_type` (`openai_responses` → `openai`) |
| `gen_ai.request.model` | `llm_client.model` |
| `gen_ai.response.model`, `gen_ai.response.id` | provider response, when exposed |
| `gen_ai.response.finish_reasons` | `LLMResponse.stop_reason` as a one-element list |
| `gen_ai.usage.input_tokens` | `TokenUsage.prompt_tokens` (includes cached) |
| `gen_ai.usage.output_tokens` | `TokenUsage.output_tokens` |
| `gen_ai.usage.cache_read.input_tokens` | `TokenUsage.cached_input_tokens` |
| `gen_ai.usage.cache_write.input_tokens` | `TokenUsage.cache_write_input_tokens` |
| `gen_ai.conversation.id` | `Agent.session_id` (a real session id, as required) |
| `gen_ai.agent.name` | `Agent.name` |
| `gen_ai.agent.description` | `ResolvedAgentSpec.description` |
| `gen_ai.tool.name`, `gen_ai.tool.call.id` | `ToolCall.name`, `ToolCall.id` |
| `gen_ai.tool.type` | `function` for built-in tools, `extension` for MCP |
| `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions` | opt-in only |
| `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` | opt-in only |
| `error.type` | `ProviderError.kind` for provider failures, exception class name otherwise |

AnkaLoop-specific attributes (`ankaloop.*`):

| Attribute | Meaning |
|-----------|---------|
| `ankaloop.source` | `cli`, `server`, `telegram`, `task` |
| `ankaloop.turn.id` | `TurnRequest.id` |
| `ankaloop.turn.priority` | `MessagePriority.name` |
| `ankaloop.turn.queue_wait_ms` | `started_at - created_at` from `TurnHandle` |
| `ankaloop.turn.steps` | final `Agent.step_count` |
| `ankaloop.context.tokens`, `ankaloop.context.window` | from `_record_llm_usage` |
| `ankaloop.context.usage_estimated` | `True` when usage came from estimation, not the API |
| `ankaloop.tool.step` | loop step index |
| `ankaloop.tool.operation_id` | `{turn_id}:{tool_call_id}` (joins with `ToolJournal`) |
| `ankaloop.tool.recovery_mode` | `classify_recovery_mode(tool_name)` |
| `ankaloop.tool.denied_by` | `capability`, `hook`, `limit` |
| `ankaloop.mcp.server` | MCP server name for `mcp__*` tools |
| `ankaloop.hook.event`, `ankaloop.hook.handler_count`, `ankaloop.hook.decision` | hook batches |
| `ankaloop.task.id`, `ankaloop.task.agent_type`, `ankaloop.task.parent_session_id` | delegation |
| `ankaloop.compaction.strategy`, `.input_tokens`, `.output_tokens`, `.generation` | compaction |
| `ankaloop.telegram.chat_id` | present only when `capture_content` is on (it is PII) |

Existing `_emit_event` events are mirrored as **span events** on the current span. This
gives coverage of `provider.retry`, `context.overflow`, `context.compacted`,
`tool.intent`, `tool.settled`, `memory.review_*`, `turn.recovery_detected`,
`turn.deadline_exceeded` with no changes at the call sites.

## Context propagation: the project-specific problems

1. **`SessionRuntime` breaks the asyncio context chain.** `submit()` enqueues a
   `TurnRequest`; a long-lived worker task created by the *first* submit runs every later
   turn, so an HTTP request span is not an ancestor of the turn span by default.
   Fix: `TurnRequest` gains `trace_context: Mapping[str, str]` (W3C carrier, captured with
   `propagate.inject` in `submit()`), and `Agent._process_turn_request` attaches it and
   opens the `invoke_agent` span before calling `TurnService`. Creating the root span here
   (rather than inside `TurnService`) lets it set `TurnHandle.trace_id` as soon as the turn
   starts, so `turn.running` already carries the id; `TurnService` only adds child spans.
   The carrier is a plain dict, so it is also safe to log.
2. **Hooks are subprocesses.** `HooksManager._build_hook_env` adds `TRACEPARENT` and
   `TRACESTATE` so command/python-script hooks can continue the trace with any OTel SDK.
3. **MCP tools.** `call_mcp_tool` wraps the call in `execute_tool` with `gen_ai.tool.type=
   extension`. Forwarding `traceparent` to the MCP server via request `_meta` is deferred
   until it is confirmed what `fastmcp` 2.x passes through on the client side.
4. **Subagents.** `TaskManager.start_task` uses `asyncio.create_task`, which copies the
   current contextvars, so the `execute_tool task` span is naturally the parent. No
   change needed beyond adding attributes.
5. **Inbound HTTP/WS.** The sessions route and `websocket.py` extract `traceparent` from
   request headers / an optional `trace_context` field on the WS message and start a
   `SERVER` span around the submit. The `opentelemetry-instrumentation-fastapi` package is
   not required; if a user installs it, its spans become the parent automatically.
6. **Telegram and CLI.** No inbound trace. Each update / prompt starts a new trace at
   the `invoke_agent` span.

Trace ids are surfaced back to the caller: `turn.running` / `turn.completed` event data
and `TurnHandle` carry `trace_id`, the HTTP submit response includes it, and the
timeline JSONL therefore records it. A user can jump from `anka session timeline` to the
trace in their backend.

## Module layout

```text
src/ankaloop/tracing/
├── __init__.py       # configure_tracing(cfg), shutdown_tracing(), get_tracer()
├── semconv.py        # gen_ai.* and ankaloop.* attribute/event name constants (single owner)
├── provider.py       # TracerProvider setup: resource, sampler, exporter (otlp/console/none)
├── spans.py          # turn_span(), chat_span(), tool_span(), hook_span(), subagent attrs,
│                     # record_usage(), record_error(), mirror_event()
├── propagation.py    # capture_carrier(), attach_carrier(), hook_env(), extract_http()
└── content.py        # opt-in message / argument / result capture with redaction + truncation
```

Integration points (one small edit each):

| File | Change |
|------|--------|
| `config.py` | `TracingConfig` dataclass, `[tracing]` decode/encode, `AnkaloopConfig.tracing` |
| `cli.py`, `server/app.py` lifespan, `telegram/bot.py` start | call `configure_tracing(cfg.tracing)` once per process; `shutdown_tracing()` on exit for flush |
| `runtime.py` | `TurnRequest.trace_context`; `_run_queue` records `queue_wait_ms` on handle |
| `agent.py` | `_process_turn_request` attaches carrier and opens `turn_span` (sets `TurnHandle.trace_id`); `_call_llm` wrapped in `chat_span` + `record_chat_response`; `_emit_event` calls `mirror_event` |
| `turn_service.py` | `prepare_context` / `compact_context` child spans |
| `tool_loop.py` | per tool call `tool_span`; denied/limited paths set `ankaloop.tool.denied_by` |
| `tool_execution.py` | `_execute_mcp` sets `gen_ai.tool.type=extension`, `ankaloop.mcp.server` |
| `hooks.py` | `execute_hooks` wrapped in `hook_span`; `_build_hook_env` adds `TRACEPARENT` |
| `task.py` | `_execute_task` adds `ankaloop.task.*` attributes to the subagent's turn via `execution_context` |
| `server/routes/sessions.py`, `server/websocket.py` | extract inbound `traceparent`, return `trace_id` |

`get_tracer()` returns `opentelemetry.trace.get_tracer("ankaloop", __version__)`. When
tracing is disabled or the SDK is not installed, the API's no-op tracer is used: every
`with tracer.start_as_current_span(...)` costs a few hundred nanoseconds and no I/O.

`configure_tracing` never replaces a `TracerProvider` the host application already set
(checked via `isinstance(get_tracer_provider(), ProxyTracerProvider)`). AnkaLoop is
embeddable through `client/embedded.py`; a host that has its own OTel setup keeps it and
AnkaLoop spans simply join its traces.

Attribute values pass through one coercion helper: scalars and homogeneous string lists
are set as-is, everything else is JSON-encoded with `default=str` so a non-serializable
tool result can never raise inside the tracing path.

Optional OpenInference aliases (`tracing.compat = ["openinference"]`): when set, each
`gen_ai.*` attribute is also written under its OpenInference name (`llm.model_name`,
`llm.token_count.prompt`, `tool.name`, `input.value` / `output.value`,
`openinference.span.kind = AGENT|LLM|TOOL`). Arize Phoenix and some other UIs still key
their LLM views off these names. Off by default because it doubles attribute volume.

## Configuration

```toml
[tracing]
enabled = false
exporter = "otlp"                     # otlp | console | none
endpoint = "http://localhost:4318"    # OTLP/HTTP base; OTEL_EXPORTER_OTLP_ENDPOINT wins
protocol = "http/protobuf"            # http/protobuf | grpc
service_name = "ankaloop"             # OTEL_SERVICE_NAME wins
sample_ratio = 1.0                    # parent-based ratio sampler
capture_content = false               # gen_ai.input/output messages, tool args/results
content_max_chars = 4000              # per attribute, after redaction
compat = []                           # ["openinference"] to dual-write Phoenix-style names
headers = {}                          # e.g. Authorization for Langfuse / Honeycomb
```

Standard `OTEL_*` environment variables take precedence over the TOML values, so an
operator can turn tracing on with `OTEL_TRACES_EXPORTER=otlp` and
`OTEL_EXPORTER_OTLP_ENDPOINT=...` without editing config. The conventions' example
variable `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` is honoured as an
alias for `capture_content`.

Redaction in `content.py`: `api_key`, `authorization`, `token`, `password` keys in tool
arguments are replaced with `[REDACTED]`; MCP server `headers`/`env` are never captured;
values are truncated to `content_max_chars`.

## Dependencies

- `opentelemetry-api` becomes a direct core dependency. It is already in `uv.lock` as a
  transitive dependency of `fastmcp` (via `pydocket`), so this changes nothing for
  installs; declaring it just makes the import contract explicit. The API package is
  no-op without an SDK.
- New optional extra `tracing = ["opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http"]`
  (`opentelemetry-sdk` is also already transitively present). gRPC export is an extra
  install the user can add themselves; `provider.py` imports it lazily.
- `dev` extra adds `opentelemetry-sdk` so tests can use `InMemorySpanExporter`.

## Testing

All tests use `opentelemetry-sdk`'s `InMemorySpanExporter` with a `SimpleSpanProcessor`
and a fake LLM client; no network, no live model.

- Shape: a turn with two `chat` calls and one tool call yields exactly
  `invoke_agent` → [`chat`, `execute_tool`, `chat`] with matching
  `gen_ai.conversation.id` and `ankaloop.turn.id` on every span.
- Retry: a provider that fails once with a retryable error produces **one** `chat` span
  with one `provider.retry` event and status `OK`; a non-retryable failure produces one
  `chat` span with `error.type = rate_limit` and the turn span `ERROR`.
- Usage: the `chat` span's `gen_ai.usage.*` equals the fake response's `TokenUsage`;
  when `usage` is `None`, `ankaloop.context.usage_estimated` is `True` and no
  `gen_ai.usage.output_tokens` is set.
- Denied tool: a hook `DENY` yields an `execute_tool` span with
  `ankaloop.tool.denied_by = hook` and no `tool.intent` event (the journal path never ran).
- Runtime propagation: a span is active when `agent.run()` is awaited; the resulting
  `invoke_agent` span must have that span as parent **on the second submit as well**
  (regression test for the long-lived worker task).
- Subagent: `task` tool delegation produces an `invoke_agent focused_coder` span whose
  parent is the `execute_tool task` span and whose `trace_id` equals the parent's.
- Hooks: a command hook sees `TRACEPARENT` in its environment matching the active span.
- Off by default: with `tracing.enabled = false`, `opentelemetry.sdk` is never imported
  and `_call_llm`'s behaviour is byte-for-byte identical (existing tests act as the guard).
- Content: with `capture_content = false` no `gen_ai.input.messages` /
  `gen_ai.tool.call.arguments` attributes exist; with it on, an argument named `api_key`
  is `[REDACTED]` and a 10 000-char result is truncated to `content_max_chars`.

## Rollout

1. **Skeleton** — `tracing/` package, `TracingConfig`, `turn_span` + `chat_span` +
   `tool_span`, `_emit_event` mirroring, `trace_id` on turn events, tests, `docs/tracing.md`.
   Shippable on its own: a CLI user gets a full tree in Jaeger.
2. **Propagation** — `TurnRequest.trace_context`, HTTP/WS extract, hook `TRACEPARENT`,
   subagent and MCP attributes.
3. **Content + metrics** — opt-in content capture with redaction; GenAI metrics
   (`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`) from the same hooks;
   optional local JSONL span exporter into the session directory for offline
   `anka session trace` viewing (replaces the unbuilt Anchor proposal).

## Non-goals

- Replacing the timeline store or tool journal: they are durability/recovery mechanisms;
  tracing is export-only and may be sampled.
- Tracing `message.chunk` streaming output.
- Auto-instrumenting `httpx` / provider SDKs. The `chat` span is the unit of accounting;
  users who want HTTP-level spans can install the corresponding OTel instrumentation
  package and it will nest under `chat` automatically.
