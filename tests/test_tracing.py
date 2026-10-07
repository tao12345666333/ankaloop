"""OpenTelemetry tracing: settings, span tree, propagation, content capture."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from ankaloop import tracing
from ankaloop.agent import Agent
from ankaloop.application_services import ApplicationServices
from ankaloop.config import TracingConfig, _decode_tracing, _encode_tracing
from ankaloop.hooks import HookDecision, HookOutput
from ankaloop.llm import LLMResponse, TokenUsage
from ankaloop.multi_agent import AgentRegistry
from ankaloop.runtime import TurnCancelledError
from ankaloop.tools import ToolRegistry, create_default_tool_registry
from ankaloop.tracing import content, provider
from ankaloop.tracing import semconv as sc

# --- fixtures --------------------------------------------------------------


@pytest.fixture(scope="session")
def exporter() -> InMemorySpanExporter:
    """Install one SDK provider for the whole session (OTel allows a single install)."""
    memory = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(memory))
    trace.set_tracer_provider(tracer_provider)
    return memory


@pytest.fixture
def spans(exporter: InMemorySpanExporter):
    exporter.clear()
    tracing.configure_tracing(TracingConfig(enabled=True))
    yield exporter
    exporter.clear()
    tracing.configure_tracing(TracingConfig())


def _services() -> ApplicationServices:
    return ApplicationServices(
        tool_registry=ToolRegistry(),
        agent_registry=AgentRegistry(),
        skill_manager=MagicMock(),
        transcript_store=MagicMock(),
        memory_manager_factory=MagicMock(),
    )


@pytest.fixture
def agent(tmp_path) -> Agent:
    with patch("ankaloop.agent.Path.home", return_value=tmp_path), patch("ankaloop.agent.load_config") as load:
        load.return_value = MagicMock()
        return Agent(session_id="trace-session", services=_services())


def _by_name(exporter: InMemorySpanExporter, prefix: str):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


class ToolThenAnswerLLM:
    """First call requests a bash tool, second call answers."""

    model = "fake-model"
    provider = "fake-provider"

    def __init__(self, command: str = "echo hi"):
        self.calls = 0
        self.command = command

    def chat(self, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                content="",
                tool_calls=[{"id": "call_1", "name": "bash", "arguments": json.dumps({"command": self.command})}],
                stop_reason=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cached_input_tokens=4),
            )
        return SimpleNamespace(content="done", tool_calls=None, stop_reason="stop", usage=None)


async def _run_tool_loop(agent: Agent, llm, tmp_path, prompt: str = "run echo"):
    agent.execution_context["turn_id"] = "turn-1"
    return await agent._enhanced_chat_with_tools(
        llm_client=llm,
        messages=[{"role": "user", "content": prompt}],
        tools=[create_default_tool_registry(enable_task=False).get_tool("bash").get_spec()],
        tool_registry={},
        stream=False,
        status=MagicMock(),
        work_dir=tmp_path,
    )


# --- settings --------------------------------------------------------------


class TestSettings:
    def test_disabled_by_default(self, monkeypatch):
        for name in (
            "OTEL_SDK_DISABLED",
            "OTEL_TRACES_EXPORTER",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        ):
            monkeypatch.delenv(name, raising=False)
        assert provider.resolve_settings(None).enabled is False
        assert provider.resolve_settings(TracingConfig()).enabled is False

    def test_env_endpoint_enables(self, monkeypatch):
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        assert provider.resolve_settings(TracingConfig()).enabled is True

    def test_sdk_disabled_wins_over_toml(self, monkeypatch):
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        assert provider.resolve_settings(TracingConfig(enabled=True)).enabled is False

    def test_capture_content_env_alias(self, monkeypatch):
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
        assert provider.resolve_settings(TracingConfig()).capture_content is True

    def test_build_provider_prefers_otel_service_name(self, monkeypatch):
        monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
        monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
        built = provider.build_tracer_provider(TracingConfig(service_name="from-toml", exporter="none"))
        assert built.resource.attributes["service.name"] == "from-env"

        monkeypatch.delenv("OTEL_SERVICE_NAME")
        built = provider.build_tracer_provider(TracingConfig(service_name="from-toml", exporter="none"))
        assert built.resource.attributes["service.name"] == "from-toml"
        assert built.resource.attributes["service.version"]

    def test_build_provider_console_exporter_has_processor(self, monkeypatch):
        monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
        built = provider.build_tracer_provider(TracingConfig(exporter="console"))
        assert built._active_span_processor._span_processors

    def test_config_round_trip(self):
        cfg = TracingConfig(
            enabled=True,
            exporter="console",
            endpoint="http://localhost:4318",
            capture_content=True,
            compat=["openinference"],
            headers={"x-api-key": "k"},
        )
        decoded = _decode_tracing(_encode_tracing(cfg))
        # Compare by value: other tests reload ``ankaloop.config`` so class identity may differ.
        assert decoded is not None
        assert dataclasses.asdict(decoded) == dataclasses.asdict(cfg)
        assert _decode_tracing(None) is None


# --- span tree -------------------------------------------------------------


class TestSpanTree:
    @pytest.mark.asyncio
    async def test_tool_loop_emits_chat_and_tool_spans(self, agent, spans, tmp_path):
        result = await _run_tool_loop(agent, ToolThenAnswerLLM(), tmp_path)
        assert result == "done"

        chats = _by_name(spans, "chat ")
        tools = _by_name(spans, "execute_tool ")
        assert [s.name for s in chats] == ["chat fake-model", "chat fake-model"]
        assert [s.name for s in tools] == ["execute_tool bash"]

        first, second = chats
        assert first.kind is SpanKind.CLIENT
        assert first.attributes[sc.GEN_AI_PROVIDER_NAME] == "fake-provider"
        assert first.attributes[sc.GEN_AI_OPERATION_NAME] == "chat"
        # prompt_tokens = input + cached + cache_write
        assert first.attributes[sc.GEN_AI_USAGE_INPUT_TOKENS] == 14
        assert first.attributes[sc.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
        assert first.attributes[sc.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS] == 4
        assert first.attributes[sc.ANKALOOP_CONTEXT_USAGE_ESTIMATED] is False
        assert list(first.attributes[sc.GEN_AI_RESPONSE_FINISH_REASONS]) == ["tool_calls"]
        assert list(first.attributes[sc.ANKALOOP_TOOLS_EXPOSED]) == ["bash"]
        # No usage from provider: estimated and flagged as such.
        assert second.attributes[sc.ANKALOOP_CONTEXT_USAGE_ESTIMATED] is True
        assert second.attributes[sc.GEN_AI_USAGE_INPUT_TOKENS] > 0
        assert list(second.attributes[sc.GEN_AI_RESPONSE_FINISH_REASONS]) == ["stop"]
        # Content capture is off by default.
        assert sc.GEN_AI_INPUT_MESSAGES not in first.attributes
        assert sc.GEN_AI_OUTPUT_MESSAGES not in first.attributes

        tool = tools[0]
        assert tool.attributes[sc.GEN_AI_TOOL_NAME] == "bash"
        assert tool.attributes[sc.GEN_AI_TOOL_CALL_ID] == "call_1"
        assert tool.attributes[sc.GEN_AI_TOOL_TYPE] == "function"
        assert tool.attributes[sc.ANKALOOP_TOOL_OPERATION_ID] == "turn-1:call_1"
        assert tool.attributes[sc.ANKALOOP_TOOL_RECOVERY_MODE] == "never_auto_retry"
        assert tool.attributes[sc.ANKALOOP_TOOL_SUCCESS] is True
        assert tool.attributes[sc.ANKALOOP_TOOL_RESULT_LENGTH] > 0
        assert sc.GEN_AI_TOOL_CALL_ARGUMENTS not in tool.attributes
        assert tool.status.status_code is StatusCode.UNSET
        # Timeline events are mirrored, chunks/usage are not.
        names = [e.name for e in tool.events]
        assert "tool.call_start" in names and "tool.intent" in names and "tool.call_complete" in names
        assert "llm.usage" not in names
        # Tool span ends after the first chat span ends and before the second starts.
        assert first.end_time <= tool.start_time <= tool.end_time <= second.start_time

    @pytest.mark.asyncio
    async def test_failed_tool_marks_span_error(self, agent, spans, tmp_path):
        await _run_tool_loop(agent, ToolThenAnswerLLM(command="exit 3"), tmp_path)
        (tool,) = _by_name(spans, "execute_tool ")
        assert tool.attributes[sc.ANKALOOP_TOOL_SUCCESS] is False
        assert tool.status.status_code is StatusCode.ERROR

    @pytest.mark.asyncio
    async def test_hook_denied_tool_records_denied_by(self, agent, spans, tmp_path):
        deny = HookOutput(decision=HookDecision.DENY, decision_reason="nope")
        with patch("ankaloop.agent.run_pre_tool_use_hooks", new=AsyncMock(return_value=deny)):
            await _run_tool_loop(agent, ToolThenAnswerLLM(), tmp_path)
        (tool,) = _by_name(spans, "execute_tool ")
        assert tool.attributes[sc.ANKALOOP_TOOL_DENIED_BY] == "hook"
        assert tool.attributes[sc.ANKALOOP_TOOL_SUCCESS] is False
        assert sc.ANKALOOP_TOOL_OPERATION_ID not in tool.attributes
        denied = [e for e in tool.events if e.name == "tool.denied"]
        assert denied and denied[0].attributes["reason"] == "nope"

    @pytest.mark.asyncio
    async def test_unknown_tool_denied_by_capability(self, agent, spans, tmp_path):
        from ankaloop.agent import MaxStepsReached

        class AlwaysUnknown:
            def chat(self, messages, **_kw):
                return SimpleNamespace(
                    content="",
                    tool_calls=[{"id": "c", "name": "nope_tool", "arguments": "{}"}],
                    stop_reason=None,
                    usage=None,
                )

        with pytest.raises(MaxStepsReached):
            await _run_tool_loop(agent, AlwaysUnknown(), tmp_path)
        tools = _by_name(spans, "execute_tool ")
        assert tools
        assert {t.attributes[sc.ANKALOOP_TOOL_DENIED_BY] for t in tools} == {"capability"}

    @pytest.mark.asyncio
    async def test_retry_keeps_one_chat_span_with_retry_event(self, agent, spans):
        from ankaloop.config import ChatConfig
        from ankaloop.llm import ProviderError, ProviderErrorKind

        class FlakyLLM:
            model = "flaky"

            def __init__(self):
                self.calls = 0

            def chat(self, **_kw):
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError(ProviderErrorKind.RATE_LIMIT, retryable=True)
                return LLMResponse(content="ok", stop_reason="stop")

        cfg = ChatConfig()
        cfg.max_retries = 1
        cfg.retry_base_delay_seconds = 0
        with patch("ankaloop.agent.asyncio.sleep", new=AsyncMock()):
            resp = await agent._call_llm(FlakyLLM(), messages=[{"role": "user", "content": "x"}], cfg=cfg)
        assert resp.content == "ok"

        chats = _by_name(spans, "chat ")
        assert len(chats) == 1
        assert [e.name for e in chats[0].events] == ["provider.retry"]
        assert chats[0].status.status_code is StatusCode.UNSET

    @pytest.mark.asyncio
    async def test_fatal_provider_error_sets_error_status(self, agent, spans):
        from ankaloop.config import ChatConfig
        from ankaloop.llm import ProviderError, ProviderErrorKind

        class BrokenLLM:
            model = "broken"

            def chat(self, **_kw):
                raise ProviderError(ProviderErrorKind.AUTH, retryable=False)

        cfg = ChatConfig()
        cfg.max_retries = 0
        with pytest.raises(ProviderError):
            await agent._call_llm(BrokenLLM(), messages=[{"role": "user", "content": "x"}], cfg=cfg)

        (chat,) = _by_name(spans, "chat ")
        assert chat.status.status_code is StatusCode.ERROR
        # ProviderError exposes its stable category, not the class name.
        assert chat.attributes[sc.ERROR_TYPE] == "auth"
        assert [e.name for e in chat.events] == ["provider.error", "exception"]


# --- turn span and propagation ---------------------------------------------


class TestTurnSpan:
    @pytest.mark.asyncio
    async def test_turn_span_is_root_and_exposes_trace_id(self, agent, spans):
        agent.turn_service.process_message = AsyncMock(return_value="done")  # type: ignore[method-assign]
        events: list[tuple[str, dict]] = []
        agent.add_event_callback(lambda name, data: events.append((name, data)))

        handle = await agent.submit("hello", stream=False)
        assert await handle.wait() == "done"

        (turn,) = _by_name(spans, "invoke_agent ")
        assert turn.parent is None
        assert turn.kind is SpanKind.SERVER
        assert turn.attributes[sc.GEN_AI_AGENT_NAME] == agent.name
        assert turn.attributes[sc.GEN_AI_CONVERSATION_ID] == "trace-session"
        assert turn.attributes[sc.ANKALOOP_TURN_ID] == handle.id
        assert turn.attributes[sc.ANKALOOP_SOURCE] == "agent"
        assert turn.attributes[sc.ANKALOOP_TURN_PRIORITY] == "normal"
        assert turn.attributes[sc.ANKALOOP_TURN_QUEUE_WAIT_MS] >= 0
        assert turn.attributes[sc.ANKALOOP_TURN_STEPS] == 0

        trace_id = format(turn.context.trace_id, "032x")
        assert handle.trace_id == trace_id
        completed = [d for name, d in events if name == "turn.completed"]
        assert completed and completed[0]["trace_id"] == trace_id

    @pytest.mark.asyncio
    async def test_submit_inside_span_parents_turn_under_caller(self, agent, spans):
        agent.turn_service.process_message = AsyncMock(return_value="done")  # type: ignore[method-assign]

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("caller") as caller:
            first = await agent.submit("one", stream=False)
            second = await agent.submit("two", stream=False)
            await first.wait()
            await second.wait()

        turns = _by_name(spans, "invoke_agent ")
        assert len(turns) == 2
        for turn in turns:
            assert turn.parent is not None
            assert turn.parent.span_id == caller.get_span_context().span_id
        assert first.trace_id == second.trace_id == format(caller.get_span_context().trace_id, "032x")

    @pytest.mark.asyncio
    async def test_cancelled_turn_is_not_an_error(self, agent, spans):
        import asyncio

        started = asyncio.Event()

        async def hang(*_a, **_k):
            started.set()
            await asyncio.Event().wait()

        agent.turn_service.process_message = hang  # type: ignore[method-assign]
        handle = await agent.submit("hello", stream=False)
        await started.wait()
        await agent._runtime.cancel_active()
        with pytest.raises(TurnCancelledError):
            await handle.wait()

        (turn,) = _by_name(spans, "invoke_agent ")
        assert turn.attributes[sc.ANKALOOP_CANCELLED] is True
        assert turn.status.status_code is StatusCode.UNSET

    @pytest.mark.asyncio
    async def test_failed_turn_records_error(self, agent, spans):
        agent.turn_service.process_message = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        handle = await agent.submit("hello", stream=False)
        with pytest.raises(RuntimeError):
            await handle.wait()
        (turn,) = _by_name(spans, "invoke_agent ")
        assert turn.status.status_code is StatusCode.ERROR
        assert turn.attributes[sc.ERROR_TYPE] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_task_attributes_flow_into_turn_span(self, agent, spans):
        agent.turn_service.process_message = AsyncMock(return_value="done")  # type: ignore[method-assign]
        agent.execution_context["source"] = "task"
        agent.execution_context["trace_attributes"] = {sc.ANKALOOP_TASK_ID: "task-9"}
        handle = await agent.submit("hello", stream=False)
        await handle.wait()
        (turn,) = _by_name(spans, "invoke_agent ")
        assert turn.attributes[sc.ANKALOOP_SOURCE] == "task"
        assert turn.attributes[sc.ANKALOOP_TASK_ID] == "task-9"


class TestPropagation:
    def test_hook_env_carries_traceparent(self, spans):
        assert tracing.hook_env_vars() == {}
        with tracing.turn_span("a", session_id="s", source="cli") as span:
            env = tracing.hook_env_vars()
        ctx = span.get_span_context()
        assert env["TRACEPARENT"].startswith(f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-")

    def test_hooks_manager_env_includes_traceparent(self, spans, tmp_path):
        from ankaloop.hooks import HookInput, HooksManager

        manager = HooksManager(project_dir=tmp_path)
        manager._loaded = True
        hook_input = HookInput(session_id="s", hook_event_name="PreToolUse", cwd=str(tmp_path))
        with tracing.turn_span("a", session_id="s", source="cli"):
            env = manager._build_hook_env(hook_input)
        assert env["TRACEPARENT"].startswith("00-")

    def test_carrier_from_headers_is_case_insensitive(self):
        headers = {"Traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01", "X-Other": "x"}
        assert tracing.carrier_from_headers(headers) == {"traceparent": headers["Traceparent"]}
        assert tracing.carrier_from_headers(None) == {}

    def test_attach_carrier_restores_context(self, spans):
        carrier = {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
        assert tracing.current_trace_id() is None
        with tracing.attach_carrier(carrier):
            assert tracing.current_trace_id() == "a" * 32
        assert tracing.current_trace_id() is None

    @pytest.mark.asyncio
    async def test_hook_span_records_decision(self, spans, tmp_path):
        from ankaloop.hooks import HookConfig, HookEvent, HookHandler, HookInput, HooksManager

        manager = HooksManager(project_dir=tmp_path)
        manager._loaded = True
        manager.hooks[HookEvent.PRE_TOOL_USE] = HookConfig(
            handlers=[HookHandler(type="python", function="tests.test_tracing._deny_hook")]
        )
        output = await manager.execute_hooks(
            HookEvent.PRE_TOOL_USE,
            HookInput(session_id="s", hook_event_name="PreToolUse", cwd=str(tmp_path)),
            tool_name="bash",
        )
        assert output.decision is HookDecision.DENY
        (span,) = _by_name(spans, "ankaloop.hook ")
        assert span.name == "ankaloop.hook PreToolUse"
        assert span.attributes[sc.ANKALOOP_HOOK_DECISION] == "deny"
        assert span.attributes[sc.ANKALOOP_HOOK_HANDLER_COUNT] == 1

    def test_http_prompt_joins_inbound_traceparent_and_returns_trace_id(self, spans, tmp_path):
        """An HTTP caller's traceparent must reach the turn and come back as ``trace_id``."""
        import asyncio

        from fastapi.testclient import TestClient

        from ankaloop.server import ServerConfig, create_app
        from ankaloop.server.session_manager import get_session_manager

        client = TestClient(create_app(ServerConfig(host="127.0.0.1", port=8080, max_sessions=2)))
        session_id = client.post("/api/v1/sessions", json={"cwd": str(tmp_path)}).json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))
        managed.agent.turn_service.process_message = AsyncMock(return_value="ok")
        inbound_trace = "c" * 32
        response = client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"content": "hello", "stream": False},
            headers={"traceparent": f"00-{inbound_trace}-{'d' * 16}-01"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["trace_id"] == inbound_trace
        (turn,) = _by_name(spans, "invoke_agent ")
        assert format(turn.context.trace_id, "032x") == inbound_trace
        assert turn.attributes[sc.ANKALOOP_SOURCE] == "server"
        assert managed.agent.execution_context["source"] == "server"


def _deny_hook(_hook_input):
    return HookOutput(decision=HookDecision.DENY, decision_reason="blocked")


# --- content capture -------------------------------------------------------


class TestContent:
    def test_redact_nested_sensitive_keys(self):
        value = {"api_key": "x", "nested": {"Authorization": "y", "ok": [1, {"password": "z"}]}}
        assert content.redact(value) == {
            "api_key": "[REDACTED]",
            "nested": {"Authorization": "[REDACTED]", "ok": [1, {"password": "[REDACTED]"}]},
        }

    def test_truncate_reports_omitted_length(self):
        assert content.truncate("abcdef", 4) == "abcd…[truncated 2 chars]"
        assert content.truncate("abc", 0) == "abc"
        assert content.truncate("abc", 3) == "abc"

    def test_messages_to_genai_shapes(self):
        msgs = [
            {"role": "system", "content": "s"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
        ]
        out = content.messages_to_genai(msgs)
        assert out[0] == {"role": "system", "parts": [{"type": "text", "content": "s"}]}
        assert out[1]["parts"][-1] == {"type": "tool_call", "id": "c1", "name": "bash", "arguments": "{}"}
        assert out[2] == {"role": "tool", "parts": [{"type": "tool_call_response", "id": "c1", "result": "out"}]}

    @pytest.mark.asyncio
    async def test_capture_content_records_messages_and_tool_io(self, agent, exporter, tmp_path):
        exporter.clear()
        tracing.configure_tracing(TracingConfig(enabled=True, capture_content=True, content_max_chars=120))
        try:
            await _run_tool_loop(agent, ToolThenAnswerLLM(), tmp_path, prompt="run echo " + "x" * 300)
        finally:
            tracing.configure_tracing(TracingConfig())

        first, _ = _by_name(exporter, "chat ")
        inputs = first.attributes[sc.GEN_AI_INPUT_MESSAGES]
        assert '"run echo' in inputs
        assert len(inputs) < 160 and inputs.endswith("chars]")  # truncated at 120 chars
        assert '"name": "bash"' in first.attributes[sc.GEN_AI_OUTPUT_MESSAGES]

        (tool,) = _by_name(exporter, "execute_tool ")
        assert json.loads(tool.attributes[sc.GEN_AI_TOOL_CALL_ARGUMENTS]) == {"command": "echo hi"}
        assert tool.attributes[sc.GEN_AI_TOOL_CALL_RESULT].startswith("hi")

    def test_openinference_compat_dual_writes(self, exporter):
        exporter.clear()
        tracing.configure_tracing(TracingConfig(enabled=True, compat=["openinference"]))
        try:
            with tracing.chat_span("m", provider="p"):
                pass
        finally:
            tracing.configure_tracing(TracingConfig())
        (span,) = exporter.get_finished_spans()
        assert span.attributes["llm.model_name"] == "m"
        assert span.attributes["llm.provider"] == "p"
        assert span.attributes["openinference.span.kind"] == "LLM"


# --- disabled path ---------------------------------------------------------


class TestDisabled:
    def test_helpers_are_noops_without_recording(self):
        tracer = trace.NoOpTracer()
        with tracer.start_as_current_span("x") as span:
            tracing.set_attributes(span, {"a": {"not": "coerced"}})
            tracing.record_error(span, RuntimeError("x"))
            tracing.mark_cancelled(span)
            tracing.mirror_event("tool.call_start", {"tool_name": "bash"})
            assert tracing.current_trace_id() is None
            assert tracing.capture_carrier() == {}

    def test_coerce_attribute(self):
        from ankaloop.tracing.spans import coerce_attribute

        assert coerce_attribute(["a", "b"]) == ["a", "b"]
        assert coerce_attribute([1, 2.5]) == [1, 2.5]
        assert coerce_attribute({"k": 1}) == '{"k": 1}'
        assert coerce_attribute([1, "x"]) == '[1, "x"]'
        assert coerce_attribute(None) is None
        assert coerce_attribute(True) is True
