"""Tests for LLM client abstraction."""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from any_llm.types.completion import Reasoning

from ankaloop.config import ChatConfig, ModelConfig
from ankaloop.llm import (
    AnthropicClient,
    AnyLLMClient,
    ContextOverflowError,
    LLMResponse,
    OpenAIClient,
    OpenAIResponsesClient,
    ProviderErrorKind,
    classify_provider_error,
    create_llm_client,
)


class TestLLMResponse:
    """Tests for LLMResponse dataclass."""

    def test_basic_response(self):
        resp = LLMResponse(content="Hello, world!")
        assert resp.content == "Hello, world!"
        assert resp.tool_calls is None

    def test_response_with_tool_calls(self):
        tool_calls = [{"id": "1", "name": "test", "arguments": "{}"}]
        resp = LLMResponse(content=None, tool_calls=tool_calls, stop_reason="tool_use")
        assert resp.tool_calls == tool_calls


class TestProviderErrors:
    """Tests for safe provider exception classification."""

    @pytest.mark.parametrize(
        ("status", "kind", "retryable"),
        [
            (401, ProviderErrorKind.AUTH, False),
            (400, ProviderErrorKind.INVALID_REQUEST, False),
            (429, ProviderErrorKind.RATE_LIMIT, True),
            (503, ProviderErrorKind.SERVER, True),
        ],
    )
    def test_classifies_http_statuses(self, status, kind, retryable):
        error = RuntimeError("secret provider response")
        error.status_code = status

        classified = classify_provider_error(error)

        assert classified.kind == kind
        assert classified.retryable is retryable
        assert "secret provider response" not in str(classified)

    def test_partial_stream_output_disables_retry(self):
        error = classify_provider_error(TimeoutError(), partial_output=True)

        assert error.kind == ProviderErrorKind.TIMEOUT
        assert error.retryable is False
        assert error.partial_output is True

    @pytest.mark.parametrize(
        ("wrapped", "kind"),
        [
            (httpx.ReadError("connection reset"), ProviderErrorKind.CONNECTION),
            (httpx.ReadTimeout("slow provider"), ProviderErrorKind.TIMEOUT),
        ],
    )
    def test_classifies_httpx_transport_errors(self, wrapped, kind):
        outer = RuntimeError("provider wrapper")
        outer.original_exception = wrapped

        classified = classify_provider_error(outer)

        assert classified.kind == kind
        assert classified.retryable is True


class TestCreateLLMClient:
    """Tests for create_llm_client factory."""

    def test_default_creates_openai_client(self):
        cfg = ChatConfig(model="gpt-5.5", api_key="test-key")
        client = create_llm_client(cfg)
        assert isinstance(client, OpenAIClient)

    def test_openai_type_creates_openai_client(self):
        cfg = ChatConfig(api_type="openai", model="gpt-5.5", api_key="test-key")
        client = create_llm_client(cfg)
        assert isinstance(client, OpenAIClient)

    @pytest.mark.parametrize("api_type", ["openai", "anthropic", "gmi"])
    def test_known_providers_disable_sdk_retries(self, api_type):
        cfg = ChatConfig(api_type=api_type, model="test-model", api_key="test-key")

        with patch("any_llm.AnyLLM.create") as create:
            create_llm_client(cfg)

        assert create.call_args.kwargs["max_retries"] == 0

    def test_openai_responses_type(self):
        cfg = ChatConfig(api_type="openai_responses", model="gpt-5.5", api_key="test-key")
        client = create_llm_client(cfg)
        assert isinstance(client, OpenAIResponsesClient)

    def test_anthropic_type(self):
        cfg = ChatConfig(api_type="anthropic", model="claude-sonnet-4-20250514", api_key="test-key")
        client = create_llm_client(cfg)
        assert isinstance(client, AnthropicClient)

    def test_any_llm_provider_type(self):
        cfg = ChatConfig(api_type="gmi", model="zai-org/GLM-5.2-FP8", api_key="test-key")
        client = create_llm_client(cfg)
        assert isinstance(client, AnyLLMClient)
        assert client.provider == "gmi"

    @pytest.mark.parametrize("api_type", ["openai", "openai_responses"])
    def test_openai_preserves_configured_base_url(self, api_type):
        base_url = "https://api-gateway.example.com/v1/openai"
        cfg = ChatConfig(
            api_type=api_type,
            base_url=base_url,
            model="test-model",
            api_key="test-key",
        )

        with patch("any_llm.AnyLLM.create") as create:
            create_llm_client(cfg)

        assert create.call_args.kwargs["api_base"] == base_url

    def test_none_config_uses_defaults(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.delenv("ANKA_API_TYPE", raising=False)

        client = create_llm_client(None)
        assert isinstance(client, OpenAIClient)


class TestOpenAIClient:
    def test_client_creation(self):
        client = OpenAIClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        assert client.model == "gpt-5.5"

    def test_rejects_oversized_request_before_provider_dispatch(self):
        client = OpenAIClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="tiny-model",
            model_config=ModelConfig(context_window=100, output_limit=20),
        )
        provider_called = False

        def completion(**_kwargs):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("provider must not be called")

        client.client.completion = completion

        with pytest.raises(ContextOverflowError) as error:
            client.chat([{"role": "user", "content": "oversized " * 200}])

        assert error.value.kind == ProviderErrorKind.CONTEXT_OVERFLOW
        assert error.value.input_limit == 80
        assert error.value.output_reserve == 20
        assert error.value.retryable is False
        assert provider_called is False

    def test_request_guard_includes_tool_schemas(self):
        client = OpenAIClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="tiny-model",
            model_config=ModelConfig(context_window=200, output_limit=20),
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "schema " * 500,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        with pytest.raises(ContextOverflowError):
            client.chat([{"role": "user", "content": "hello"}], tools=tools)

    @pytest.mark.parametrize("output_parameter", ["max_completion_tokens", "max_output_tokens"])
    def test_request_guard_reserves_native_output_limit_aliases(self, output_parameter):
        client = OpenAIClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="tiny-model",
            model_config=ModelConfig(context_window=100, output_limit=10),
        )
        client.client.completion = lambda **_kwargs: pytest.fail("provider must not be called")

        with pytest.raises(ContextOverflowError) as error:
            client.chat(
                [{"role": "user", "content": "input " * 30}],
                **{output_parameter: 90},
            )

        assert error.value.output_reserve == 90

    def test_request_model_override_does_not_reuse_default_model_limits(self):
        client = OpenAIClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="large-model",
            model_config=ModelConfig(
                model_id="large-model",
                context_window=1_000_000,
                output_limit=10,
            ),
        )
        client.client.completion = lambda **_kwargs: pytest.fail("provider must not be called")

        with (
            patch("ankaloop.llm.get_model_context_window", return_value=100),
            patch("ankaloop.llm.get_model_output_limit", return_value=20),
            pytest.raises(ContextOverflowError) as error,
        ):
            client.chat(
                [{"role": "user", "content": "oversized " * 200}],
                model="small-model",
            )

        assert error.value.context_window == 100
        assert error.value.output_reserve == 20

    def test_chat_preserves_tool_call_extra_content(self):
        client = OpenAIClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        extra = {"google": {"thought_signature": "sig-abc"}}
        client.client.completion = lambda **_kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="call_1",
                                function=SimpleNamespace(name="bash", arguments='{"command": "pwd"}'),
                                extra_content=extra,
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )

        response = client.chat([{"role": "user", "content": "run pwd"}])

        assert response.tool_calls == [
            {
                "id": "call_1",
                "name": "bash",
                "arguments": '{"command": "pwd"}',
                "extra_content": extra,
            }
        ]

    def test_chat_captures_provider_usage(self):
        client = OpenAIClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        client.client.completion = lambda **_kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=30,
                total_tokens=150,
                prompt_tokens_details=SimpleNamespace(cached_tokens=40),
            ),
        )

        response = client.chat([{"role": "user", "content": "hello"}])

        assert response.usage is not None
        assert response.usage.input_tokens == 80
        assert response.usage.prompt_tokens == 120
        assert response.usage.output_tokens == 30
        assert response.usage.total_tokens == 150
        assert response.usage.cached_input_tokens == 40

    def test_chat_uses_reasoning_as_content_when_content_missing(self):
        client = OpenAIClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        client.client.completion = lambda **_kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        reasoning=Reasoning(content="DeepSeek final answer"),
                        tool_calls=None,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        response = client.chat([{"role": "user", "content": "hello"}])

        assert response.content == "DeepSeek final answer"
        assert response.thinking is None

    def test_chat_keeps_reasoning_hidden_when_content_present(self):
        client = OpenAIClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        client.client.completion = lambda **_kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Visible answer",
                        reasoning=Reasoning(content="Hidden reasoning"),
                        tool_calls=None,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        response = client.chat([{"role": "user", "content": "hello"}])

        assert response.content == "Visible answer"
        assert response.thinking == "Hidden reasoning"

    def test_streaming_chat_uses_reasoning_as_content_when_content_missing(self):
        client = OpenAIClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        client.client.completion = lambda **_kwargs: iter(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                reasoning=Reasoning(content="Deep"),
                                tool_calls=None,
                            ),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                reasoning=Reasoning(content="Seek"),
                                tool_calls=None,
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                ),
            ]
        )
        streamed_chunks = []

        response = client.chat(
            [{"role": "user", "content": "hello"}],
            stream_callback=streamed_chunks.append,
        )

        assert streamed_chunks == ["DeepSeek"]
        assert response.content == "DeepSeek"
        assert response.thinking is None

    def test_chat_raises_clear_error_when_choices_missing(self):
        client = OpenAIClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        client.client.completion = lambda **_kwargs: SimpleNamespace(
            choices=None,
            usage=None,
        )

        with pytest.raises(ValueError, match="without choices"):
            client.chat([{"role": "user", "content": "hello"}])


class TestOpenAIResponsesClient:
    def test_client_creation(self):
        client = OpenAIResponsesClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        assert client.model == "gpt-5.5"

    def test_rejects_oversized_request_before_responses_dispatch(self):
        client = OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="tiny-model",
            model_config=ModelConfig(context_window=100, output_limit=20),
        )
        provider_called = False

        def responses(**_kwargs):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("provider must not be called")

        client.client.responses = responses

        with pytest.raises(ContextOverflowError):
            client.chat([{"role": "user", "content": "oversized " * 200}])

        assert provider_called is False

    def test_request_guard_reserves_native_max_output_tokens(self):
        client = OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="tiny-model",
            model_config=ModelConfig(context_window=100, output_limit=10),
        )
        client.client.responses = lambda **_kwargs: pytest.fail("provider must not be called")

        with pytest.raises(ContextOverflowError) as error:
            client.chat(
                [{"role": "user", "content": "input " * 30}],
                max_output_tokens=90,
            )

        assert error.value.output_reserve == 90

    def test_responses_captures_provider_usage(self):
        client = OpenAIResponsesClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-5.5")
        client.client.responses = lambda **_kwargs: SimpleNamespace(
            output=[],
            stop_reason="stop",
            usage=SimpleNamespace(
                input_tokens=200,
                output_tokens=50,
                total_tokens=250,
                input_tokens_details=SimpleNamespace(cached_tokens=80),
            ),
        )

        response = client.chat([{"role": "user", "content": "hello"}])

        assert response.usage is not None
        assert response.usage.input_tokens == 120
        assert response.usage.prompt_tokens == 200
        assert response.usage.output_tokens == 50
        assert response.usage.cached_input_tokens == 80

    def test_responses_converts_tool_history(self):
        messages = [
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
        ]

        converted = OpenAIResponsesClient._convert_messages(messages)

        assert converted == [
            {"role": "user", "content": "read it"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "read_file",
                "arguments": '{"path":"a.py"}',
            },
            {"type": "function_call_output", "call_id": "call-1", "output": "contents"},
        ]

    def test_responses_streams_and_forwards_options(self):
        client = OpenAIResponsesClient.__new__(OpenAIResponsesClient)
        client.model = "gpt-5.5"
        captured = {}
        completed = SimpleNamespace(output=[], status="completed", usage=None)

        def responses(**kwargs):
            captured.update(kwargs)
            return iter(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="done"),
                    SimpleNamespace(type="response.completed", response=completed),
                ]
            )

        client.client = SimpleNamespace(responses=responses)
        chunks = []

        response = client.chat(
            [{"role": "user", "content": "hello"}],
            stream_callback=chunks.append,
            model="gpt-5",
            max_tokens=123,
            temperature=0.2,
        )

        assert chunks == ["done"]
        assert response.stop_reason == "completed"
        assert captured["model"] == "gpt-5"
        assert captured["max_output_tokens"] == 123
        assert captured["temperature"] == 0.2
        assert captured["stream"] is True
        assert captured["allow_running_loop"] is True


class TestAnthropicClient:
    def test_client_creation(self):
        client = AnthropicClient(api_key="test-key", model="claude-sonnet-4-20250514")
        assert client.model == "claude-sonnet-4-20250514"

    def test_anthropic_uses_normalized_any_llm_usage(self):
        client = AnthropicClient.__new__(AnthropicClient)
        client.model = "claude-sonnet-4-20250514"
        client.provider = "anthropic"
        client.client = SimpleNamespace(
            completion=lambda **_kwargs: SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="done", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=140,
                    completion_tokens=20,
                    total_tokens=160,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=30),
                ),
            )
        )

        response = client.chat([{"role": "user", "content": "hello"}])

        assert response.usage is not None
        assert response.usage.input_tokens == 110
        assert response.usage.prompt_tokens == 140
        assert response.usage.output_tokens == 20
        assert response.usage.cached_input_tokens == 30


class TestRequestHeaders:
    """Tests for provider request header injection (app attribution)."""

    def test_openrouter_endpoint_injects_attribution_headers(self):
        cfg = ChatConfig(
            api_type="openai",
            base_url="https://openrouter.ai/api/v1",
            model="test-model",
            api_key="test-key",
        )
        client = create_llm_client(cfg)
        headers = client.client.client.default_headers
        assert headers["HTTP-Referer"] == "https://github.com/tao12345666333/ankaloop"
        assert headers["X-OpenRouter-Title"] == "AnkaLoop"
        assert headers["X-OpenRouter-Categories"] == "personal-agent"

    def test_non_openrouter_endpoint_gets_no_attribution_headers(self):
        cfg = ChatConfig(api_type="openai", model="test-model", api_key="test-key")
        client = create_llm_client(cfg)
        headers = client.client.client.default_headers
        assert "HTTP-Referer" not in headers
        assert "X-OpenRouter-Title" not in headers

    def test_explicit_headers_override_attribution_defaults(self):
        cfg = ChatConfig(
            api_type="openai",
            base_url="https://openrouter.ai/api/v1",
            model="test-model",
            api_key="test-key",
            extra_headers={"HTTP-Referer": "https://myapp.example", "X-Custom": "yes"},
        )
        client = create_llm_client(cfg)
        headers = client.client.client.default_headers
        assert headers["HTTP-Referer"] == "https://myapp.example"
        assert headers["X-Custom"] == "yes"
        # title still defaulted since not explicitly set
        assert headers["X-OpenRouter-Title"] == "AnkaLoop"

    def test_env_overrides_attribution_defaults(self, monkeypatch):
        monkeypatch.setenv("ANKA_APP_URL", "https://example.com")
        monkeypatch.setenv("ANKA_APP_NAME", "MyApp")
        monkeypatch.setenv("ANKA_APP_CATEGORIES", "")
        from ankaloop.llm import _build_request_headers

        headers = _build_request_headers(
            base_url="https://openrouter.ai/api/v1",
            api_type=None,
            extra_headers=None,
        )
        assert headers["HTTP-Referer"] == "https://example.com"
        assert headers["X-OpenRouter-Title"] == "MyApp"
        assert "X-OpenRouter-Categories" not in headers
