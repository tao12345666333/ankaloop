"""Tests for the smart compaction module."""

from unittest.mock import MagicMock, patch

import pytest

from ankaloop.compaction import (
    COMPACT_PROMPT,
    DEFAULT_OUTPUT_LIMIT,
    CompactionConfig,
    CompactionResult,
    CompactionStrategy,
    SmartCompactor,
    estimate_request_tokens,
    estimate_tokens,
    get_model_context_window,
    get_model_output_limit,
    resolve_model_output_limit,
)


class TestGetModelContextWindow:
    """Tests for get_model_context_window function.

    Note: These tests are designed to be flexible since values can come
    from either the models.dev cache or built-in fallbacks.
    """

    def test_gpt5_family(self):
        """Test GPT-5 family models have large context windows."""
        gpt55 = get_model_context_window("gpt-5.5")
        assert gpt55 >= 200_000

        gpt52 = get_model_context_window("gpt-5.2")
        assert gpt52 >= 200_000

    def test_claude_family(self):
        """Test Claude models have large context windows."""
        claude = get_model_context_window("claude-sonnet-4-5")
        assert claude >= 200_000

        # Current-generation Claude models carry a 1M window.
        claude_opus = get_model_context_window("claude-opus-5-5")
        assert claude_opus >= 1_000_000

    def test_gemini_family(self):
        """Test Gemini models have large context windows."""
        gemini = get_model_context_window("gemini-2.5-pro")
        assert gemini >= 1_000_000

    def test_partial_match_gpt55(self):
        """Test partial matching for GPT-5.5 models."""
        result = get_model_context_window("gpt-5.5-2026-05-13")
        assert result >= 1_000_000

    def test_provider_prefixed_model_name(self):
        """Gateway-style ids such as ``openai/gpt-5.5`` resolve to the family entry."""
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            assert get_model_context_window("openai/gpt-5.5") == 1_050_000

    def test_deepseek_model(self):
        """Test DeepSeek model detection."""
        # Current DeepSeek generation ships a 1M window.
        assert get_model_context_window("deepseek-v4-flash") >= 1_000_000
        # Unknown DeepSeek variants fall back to the family heuristic.
        assert get_model_context_window("deepseek-chat") >= 128_000

    def test_qwen_model(self):
        """Test Qwen model detection."""
        qwen = get_model_context_window("qwen3.7-max")
        assert qwen >= 1_000_000
        # Unlisted generations keep a 128K floor instead of the old 32K.
        assert get_model_context_window("qwen-2.5") >= 128_000

    def test_glm_model(self):
        """Test GLM model detection."""
        glm46 = get_model_context_window("glm-4.6")
        assert glm46 >= 200_000

        # GLM-5.2 and newer widened the window to 1M.
        assert get_model_context_window("glm-5.3") >= 1_000_000

    def test_offline_table_matches_no_cache_lookup(self):
        """The built-in table and the offline DB lookup agree for known models."""
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            assert get_model_context_window("gpt-5.5") == 1_050_000
            assert get_model_context_window("claude-sonnet-4-5") == 200_000
            assert get_model_context_window("gemini-2.5-pro") == 1_048_576
            assert get_model_context_window("glm-4.6") == 204_800
            assert get_model_context_window("minimax-m2.1") == 204_800

    def test_unknown_model_returns_default(self):
        """Test unknown model returns default window."""
        result = get_model_context_window("some-unknown-model-xyz")
        assert result == 200_000  # DEFAULT_CONTEXT_WINDOW

    def test_model_config_context_window_override(self):
        """Test that model_config.context_window takes highest priority."""
        from ankaloop.config import ModelConfig

        mc = ModelConfig(context_window=1048576)
        result = get_model_context_window("some-unknown-model-xyz", model_config=mc)
        assert result == 1048576

    def test_model_config_provider_id_forwarded(self):
        """Test that model_config.provider_id is forwarded to DB lookup."""
        from ankaloop.config import ModelConfig

        mc = ModelConfig(provider_id="test-provider")
        # Even with provider_id, unknown model should still return default
        # (this test just verifies provider_id is accepted without error)
        result = get_model_context_window("some-unknown-model-xyz", model_config=mc)
        assert result > 0


class TestResolveModelOutputLimit:
    """Tests for resolve_model_output_limit (distinguishes unknown models)."""

    def test_model_config_override_wins(self):
        from ankaloop.config import ModelConfig

        mc = ModelConfig(output_limit=4096)
        assert resolve_model_output_limit("some-model", model_config=mc) == 4096

    def test_unknown_model_returns_none(self):
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            assert resolve_model_output_limit("totally-unknown-model-xyz") is None

    def test_database_hit_returns_limit(self):
        from ankaloop.models_db import ModelInfo, ModelsDatabase, ProviderInfo

        model = ModelInfo(id="custom-model", name="Custom", provider_id="custom", output_limit=12345)
        provider = ProviderInfo(id="custom", name="Custom", models={"custom-model": model})
        db = ModelsDatabase(providers={"custom": provider})

        with patch("ankaloop.models_db.load_models_cache", return_value=db):
            assert resolve_model_output_limit("custom-model") == 12345
            assert resolve_model_output_limit("custom-model", provider_id="custom") == 12345

    def test_get_model_output_limit_falls_back_for_unknown(self):
        """The legacy getter keeps the DEFAULT_OUTPUT_LIMIT fallback contract."""
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            assert get_model_output_limit("totally-unknown-model-xyz") == DEFAULT_OUTPUT_LIMIT


class TestEstimateTokens:
    """Tests for estimate_tokens function."""

    def test_empty_messages(self):
        """Test with empty message list."""
        assert estimate_tokens([]) == 0

    def test_simple_message(self):
        """Test with a simple text message."""
        messages = [{"role": "user", "content": "Hello, world!"}]
        tokens = estimate_tokens(messages)
        assert tokens > 0
        assert tokens < 100  # Should be small

    def test_multiple_messages(self):
        """Test with multiple messages."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there! How can I help you?"},
            {"role": "user", "content": "Tell me about Python"},
        ]
        tokens = estimate_tokens(messages)
        assert tokens > 0

    def test_long_content(self):
        """Test with long content."""
        long_text = "word " * 1000  # ~5000 characters
        messages = [{"role": "user", "content": long_text}]
        tokens = estimate_tokens(messages)
        # Should estimate roughly 1000+ tokens (4 chars per token)
        assert tokens > 500

    def test_message_with_tool_calls(self):
        """Test message with tool calls."""
        messages = [
            {
                "role": "assistant",
                "content": "Let me read that file.",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "name": "read_file",
                        "arguments": '{"path": "/test.py"}',
                    }
                ],
            }
        ]
        tokens = estimate_tokens(messages)
        assert tokens > 0


class TestCompactionConfig:
    """Tests for CompactionConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = CompactionConfig()
        assert config.strategy == CompactionStrategy.SUMMARY
        assert config.threshold_ratio == 0.7
        assert config.target_ratio == 0.3
        assert config.preserve_last == 6
        assert config.preserve_tool_results is True
        assert config.safety_margin == 0.1

    def test_custom_values(self):
        """Test custom configuration values."""
        config = CompactionConfig(
            strategy=CompactionStrategy.HYBRID,
            threshold_ratio=0.8,
            target_ratio=0.4,
            preserve_last=10,
        )
        assert config.strategy == CompactionStrategy.HYBRID
        assert config.threshold_ratio == 0.8
        assert config.target_ratio == 0.4
        assert config.preserve_last == 10


class TestCompactionStrategy:
    """Tests for CompactionStrategy enum."""

    def test_strategies_exist(self):
        """Test all strategies are defined."""
        assert CompactionStrategy.SUMMARY.value == "summary"
        assert CompactionStrategy.TRUNCATE.value == "truncate"
        assert CompactionStrategy.SLIDING_WINDOW.value == "sliding_window"
        assert CompactionStrategy.HYBRID.value == "hybrid"


class TestCompactPrompt:
    def test_prompt_contains_structured_handoff_sections(self):
        expected_sections = [
            "<active_task>",
            "<goal>",
            "<constraints_and_preferences>",
            "<completed_actions>",
            "<active_state>",
            "<in_progress>",
            "<blocked>",
            "<key_decisions>",
            "<pending_asks>",
            "<relevant_files>",
            "<remaining_work>",
            "<critical_context>",
        ]

        for section in expected_sections:
            assert section in COMPACT_PROMPT
        assert "Mark stale" in COMPACT_PROMPT


class TestSmartCompactor:
    """Tests for SmartCompactor class."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock AnkaLoop LLM client."""
        client = MagicMock()
        # Mock successful completion response
        mock_response = MagicMock()
        mock_response.content = "Compacted summary of the conversation."
        client.chat.return_value = mock_response
        return client

    def test_init_calculates_thresholds(self, mock_client):
        """Test that init calculates thresholds based on model."""
        compactor = SmartCompactor(mock_client, model="gpt-5.5")

        # Value may come from models.dev cache or built-in fallback (400K)
        assert compactor.context_window >= 400_000
        # threshold = context_window * 0.9 * 0.7
        assert compactor.threshold_tokens > compactor.context_window * 0.5
        assert compactor.threshold_tokens < compactor.context_window
        # target = context_window * 0.9 * 0.3
        assert compactor.target_tokens < compactor.threshold_tokens

    def test_init_with_small_model(self, mock_client):
        """Test thresholds for smaller context window."""
        compactor = SmartCompactor(mock_client, model="deepseek-chat")

        # Value may come from models.dev cache or built-in fallback (64K)
        assert compactor.context_window >= 64_000
        assert compactor.threshold_tokens < compactor.context_window
        assert compactor.target_tokens < compactor.threshold_tokens

    def test_should_compact_false_for_small_context(self, mock_client):
        """Test should_compact returns False for small contexts."""
        compactor = SmartCompactor(mock_client, model="gpt-5.5")

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        assert compactor.should_compact(messages) is False

    def test_should_compact_false_below_minimum(self, mock_client):
        """Test should_compact returns False below minimum threshold."""
        config = CompactionConfig(min_tokens_to_compact=10000)
        compactor = SmartCompactor(mock_client, model="gpt-5.5", config=config)

        # Create content that's ~500 tokens
        messages = [{"role": "user", "content": "x" * 2000}]
        assert compactor.should_compact(messages) is False

    def test_request_estimate_includes_tool_schemas(self):
        """Complete request estimates include tool definitions sent to the provider."""
        messages = [{"role": "user", "content": "Read this page"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": "Fetch a web page " * 100,
                    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
                },
            }
        ]

        assert estimate_request_tokens(messages, tools) > estimate_tokens(messages)

    def test_get_token_usage(self, mock_client):
        """Test get_token_usage returns correct structure."""
        compactor = SmartCompactor(mock_client, model="gpt-5.5")

        messages = [{"role": "user", "content": "Hello world"}]
        usage = compactor.get_token_usage(messages)

        assert "current_tokens" in usage
        assert "context_window" in usage
        assert "threshold_tokens" in usage
        assert "target_tokens" in usage
        assert "usage_ratio" in usage
        assert "should_compact" in usage
        assert "headroom_tokens" in usage

        assert usage["context_window"] >= 400_000
        assert isinstance(usage["usage_ratio"], float)

    def test_compact_empty_messages(self, mock_client):
        """Test compact with empty messages."""
        compactor = SmartCompactor(mock_client, model="gpt-5.5")

        result_msgs, result = compactor.compact([])

        assert result_msgs == []
        assert result.original_tokens == 0
        assert result.compacted_tokens == 0
        assert result.messages_removed == 0

    def test_compact_preserves_recent(self, mock_client):
        """Test compact preserves recent messages."""
        config = CompactionConfig(preserve_last=2)
        compactor = SmartCompactor(mock_client, model="gpt-5.5", config=config)

        messages = [
            {"role": "user", "content": "Old message 1"},
            {"role": "assistant", "content": "Old response 1"},
            {"role": "user", "content": "Old message 2"},
            {"role": "assistant", "content": "Old response 2"},
            {"role": "user", "content": "Recent message"},
            {"role": "assistant", "content": "Recent response"},
        ]

        result_msgs, result = compactor.compact(messages)

        # Should have summary + preserved messages
        assert len(result_msgs) >= 2
        assert result.messages_preserved >= 2

    def test_compact_with_truncate_strategy(self, mock_client):
        """Test compact with truncate strategy."""
        config = CompactionConfig(strategy=CompactionStrategy.TRUNCATE, preserve_last=2)
        compactor = SmartCompactor(mock_client, model="gpt-5.5", config=config)

        messages = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Message 3"},
            {"role": "assistant", "content": "Response 3"},
        ]

        result_msgs, result = compactor.compact(messages)

        assert result.strategy_used == CompactionStrategy.TRUNCATE
        assert len(result_msgs) > 0

    def test_checkpoint_compaction_never_splits_tool_protocol(self, mock_client):
        config = CompactionConfig(
            strategy=CompactionStrategy.TRUNCATE,
            preserve_last=2,
        )
        compactor = SmartCompactor(mock_client, model="gpt-5.5", config=config)
        messages = [
            {"role": "user", "content": "Inspect"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "read_file",
                "content": "contents",
            },
            {"role": "assistant", "content": "Done"},
            {"role": "user", "content": "Next"},
            {"role": "assistant", "content": "Answer"},
        ]

        checkpoint, _ = compactor.compact_checkpoint(messages)

        assert len(checkpoint) == 1
        assert checkpoint[0]["role"] == "assistant"
        assert "tool_calls" not in checkpoint[0]


class TestCompactionResult:
    """Tests for CompactionResult dataclass."""

    def test_result_fields(self):
        """Test CompactionResult has all expected fields."""
        result = CompactionResult(
            original_tokens=10000,
            compacted_tokens=3000,
            messages_removed=20,
            messages_preserved=5,
            strategy_used=CompactionStrategy.SUMMARY,
            summary="Test summary",
        )

        assert result.original_tokens == 10000
        assert result.compacted_tokens == 3000
        assert result.messages_removed == 20
        assert result.messages_preserved == 5
        assert result.strategy_used == CompactionStrategy.SUMMARY
        assert result.summary == "Test summary"

    def test_result_without_summary(self):
        """Test CompactionResult with no summary."""
        result = CompactionResult(
            original_tokens=1000,
            compacted_tokens=1000,
            messages_removed=0,
            messages_preserved=5,
            strategy_used=CompactionStrategy.TRUNCATE,
        )

        assert result.summary is None


class TestCreateCompactor:
    """Tests for create_compactor factory function."""

    def test_create_with_defaults(self):
        """Test creating compactor with defaults."""
        from ankaloop.compaction import create_compactor

        client = MagicMock()
        compactor = create_compactor(client, "gpt-5.5")

        assert isinstance(compactor, SmartCompactor)
        assert compactor.config.strategy == CompactionStrategy.SUMMARY

    def test_create_with_custom_strategy(self):
        """Test creating compactor with custom strategy."""
        from ankaloop.compaction import create_compactor

        client = MagicMock()
        compactor = create_compactor(
            client,
            "gpt-5.5",
            strategy="hybrid",
            threshold_ratio=0.8,
            target_ratio=0.4,
        )

        assert compactor.config.strategy == CompactionStrategy.HYBRID
        assert compactor.config.threshold_ratio == 0.8
        assert compactor.config.target_ratio == 0.4
