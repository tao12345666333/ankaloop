"""
Smart Context Compaction for AnkaLoop.

This module provides intelligent context compaction that dynamically adjusts
thresholds based on the model's context window size. It supports multiple
compaction strategies and integrates with the event bus for monitoring.

Key Features:
- Dynamic threshold based on model context windows
- Multiple compaction strategies (summary, truncate, sliding window)
- Token estimation with tiktoken support
- Preservation of important context (recent messages, tool results)
- Event emission for monitoring compaction events
- Configurable safety margins

Built-in Fallback Models (when models.dev unavailable):
- OpenAI: GPT-5 family (400K), GPT-5.4+ (1.05M)
- Anthropic: Claude 4.5 (200K), Claude 4.6+ / 5.x (1M)
- Google: Gemini (1M), Gemma 4 (262K)
- DeepSeek: DeepSeek-V4 (1M)
- ZAI/GLM: GLM-4.6/4.7/5/5.1 (200K), GLM-5.2+ (1M)
- Alibaba/Qwen: Qwen3-Max/Coder (262K), Qwen3.5+ (1M)
- Moonshot: Kimi K3 (1M), MiniMax: M3 (1M)

Example:
    from ankaloop.compaction import SmartCompactor, get_model_context_window

    # Get context window for model
    ctx_window = get_model_context_window("gpt-5.5")  # 1050000

    # Create compactor
    compactor = SmartCompactor(client, model="gpt-5.5")

    # Check if compaction needed
    if compactor.should_compact(messages):
        compacted = await compactor.compact_async(messages)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import ModelConfig

logger = logging.getLogger(__name__)


# Model context window sizes (in tokens) - Built-in fallback models.
#
# Used when the models.dev cache is unavailable (offline first run, failed
# fetch, `models_db` import error). Values mirror models.dev so an offline
# lookup resolves to the same window as an online one; a per-model
# ``model_config.context_window`` override remains the escape hatch for
# gateways that serve a narrower window than the upstream API.
#
# Keys are matched as lowercase substrings, so provider prefixes
# ("openai/gpt-5.5") and dated snapshots ("gpt-5.5-2026-05-13") resolve too.
# Longer keys are tried first, which keeps "gpt-5.4-mini" from being answered
# by "gpt-5.4".
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 400_000,
    "gpt-5.4-pro": 1_050_000,
    "gpt-5.5-pro": 1_050_000,
    "gpt-5.3-codex": 400_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.6": 1_050_000,
    "gpt-5.1": 400_000,
    "gpt-5.2": 400_000,
    "gpt-5": 400_000,
    "gpt-oss": 131_072,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    # Anthropic
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-5": 1_000_000,
    # Google
    "gemini-4-argon": 1_048_576,
    "gemini-3.1-pro": 1_048_576,
    "gemini-3.7-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemma-4": 262_144,
    # DeepSeek
    "deepseek-v4-flash": 1_048_576,
    "deepseek-v4-pro": 1_048_576,
    "deepseek-v4": 1_048_576,
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    # ZAI/GLM
    "glm-5.2": 1_000_000,
    "glm-5.3": 1_000_000,
    "glm-5.1": 200_000,
    "glm-5": 204_800,
    "glm-4.6": 204_800,
    "glm-4.7": 204_800,
    # Alibaba/Qwen
    "qwen3-coder": 262_144,
    "qwen3-max": 262_144,
    "qwen3.5-plus": 1_000_000,
    "qwen3.5-flash": 1_000_000,
    "qwen3.6-plus": 1_000_000,
    "qwen3.6-flash": 1_000_000,
    "qwen3.7-max": 1_000_000,
    "qwen3.7-plus": 1_000_000,
    "qwen3.7-flash": 1_000_000,
    "qwen3.8-max": 1_000_000,
    "qwen3.8-plus": 1_000_000,
    "qwen3.8-flash": 1_000_000,
    "qwen3.5": 262_144,
    "qwen3.6": 262_144,
    "qwen-max": 32_768,
    # Moonshot/Kimi
    "kimi-k3": 1_048_576,
    "kimi-k2": 262_144,
    # MiniMax
    "minimax-m3": 1_000_000,
    "minimax-m2": 204_800,
    # xAI
    "grok-4.20": 1_000_000,
    "grok-4.3": 1_000_000,
    "grok-4.5": 500_000,
    "grok-4.6": 500_000,
    "grok-4.7": 500_000,
    "grok-4": 256_000,
    # Mistral
    "mistral-large-4": 1_048_576,
    "mistral-large": 262_144,
    "mistral-medium": 262_144,
    "mistral-small-2603": 262_144,
    "mistral-small": 128_000,
    "codestral": 256_000,
    "devstral": 262_144,
    "ministral-3b": 131_072,
    "ministral": 262_144,
}

# Longest key first so a specific entry always wins over a shorter prefix
# ("gpt-5.4-mini" before "gpt-5.4", "glm-5.1" before "glm-5").
_MODEL_KEYS_BY_LENGTH: tuple[str, ...] = tuple(sorted(MODEL_CONTEXT_WINDOWS, key=len, reverse=True))

# Default context window for unknown models
DEFAULT_CONTEXT_WINDOW = 200_000
# Fallback output-token limit for unknown models. 32K matches the current
# model landscape (models.dev: ~80% of models allow >=32K output, median 128K);
# the previous 8192 under-reserved output space in request-size preflight now
# that unknown models send no explicit cap. Single source of truth; models_db
# re-exports it.
DEFAULT_OUTPUT_LIMIT = 32768


def match_builtin_context_window(model: str) -> int | None:
    """Resolve a context window from the built-in table, or ``None``.

    Unlike :func:`get_model_context_window` this never falls back to a family
    heuristic or the default, so callers can tell "the table knows this model"
    apart from "nothing matched".
    """
    model_lower = model.lower()
    for known_model in _MODEL_KEYS_BY_LENGTH:
        if known_model in model_lower:
            return MODEL_CONTEXT_WINDOWS[known_model]
    return None


class CompactionStrategy(Enum):
    """Strategies for context compaction."""

    SUMMARY = "summary"  # Use LLM to summarize old messages
    TRUNCATE = "truncate"  # Simple truncation of old messages
    SLIDING_WINDOW = "sliding_window"  # Keep a sliding window of recent messages
    HYBRID = "hybrid"  # Combination of summary and sliding window


@dataclass
class CompactionConfig:
    """Configuration for context compaction.

    Attributes:
        strategy: Compaction strategy to use
        threshold_ratio: Trigger compaction when usage exceeds this ratio of context window
        target_ratio: Aim to reduce context to this ratio after compaction
        preserve_last: Number of recent user/assistant messages to preserve
        preserve_tool_results: Whether to preserve recent tool results
        max_tool_results: Maximum number of tool results to preserve
        min_tokens_to_compact: Minimum tokens before considering compaction
        safety_margin: Extra margin to leave for response generation
    """

    strategy: CompactionStrategy = CompactionStrategy.SUMMARY
    threshold_ratio: float = 0.7  # Compact when > 70% of context used
    target_ratio: float = 0.3  # Aim for 30% after compaction
    preserve_last: int = 6  # Keep last 6 user/assistant messages
    preserve_tool_results: bool = True
    max_tool_results: int = 10
    min_tokens_to_compact: int = 5000  # Don't compact tiny contexts
    safety_margin: float = 0.1  # 10% margin for response


@dataclass
class CompactionResult:
    """Result of a compaction operation.

    Attributes:
        original_tokens: Estimated tokens before compaction
        compacted_tokens: Estimated tokens after compaction
        messages_removed: Number of messages removed/summarized
        messages_preserved: Number of messages preserved
        strategy_used: Strategy that was applied
        summary: Generated summary (if summary strategy used)
    """

    original_tokens: int
    compacted_tokens: int
    messages_removed: int
    messages_preserved: int
    strategy_used: CompactionStrategy
    summary: str | None = None


COMPACT_PROMPT = """You are tasked with compacting a coding conversation context. This is critical for maintaining an effective working memory.

**Compression Rules:**
- MUST KEEP: Error messages, working solutions, current task state, file paths
- MERGE: Similar discussions into single summary
- REMOVE: Redundant explanations, failed attempts (keep lessons learned)
- CONDENSE: Long code/file content → keep key parts, signatures, and structure only
- PRESERVE: Any information that would be needed to continue the current task

**Input Context ({token_count} tokens):**

{context}

**Output a concise summary (aim for {target_tokens} tokens) in this exact structure:**

<active_task>
[Most recent unfulfilled user request, verbatim when possible. If none, write "None."]
</active_task>

<goal>
[The user-visible goal and desired outcome.]
</goal>

<constraints_and_preferences>
- [Durable instructions, constraints, style preferences, environment assumptions.]
</constraints_and_preferences>

<completed_actions>
- [Past-tense, dated when possible: action taken, tool/file target, outcome.]
</completed_actions>

<active_state>
[Current known state of the workspace, session, services, tests, or UI.]
</active_state>

<in_progress>
- [Work started but not completed. Include exact files/commands if relevant.]
</in_progress>

<blocked>
- [Blocker, cause, attempted fixes, and what is needed to unblock.]
</blocked>

<key_decisions>
- [Decision and rationale.]
</key_decisions>

<pending_asks>
- [Questions asked of the user that remain unanswered. Mark stale if they may no longer apply.]
</pending_asks>

<relevant_files>
- [path]: [why it matters, key symbols, current state]
</relevant_files>

<remaining_work>
- [Next steps. Mark stale if inferred from older context rather than the latest user turn.]
</remaining_work>

<critical_context>
[Errors, exact command outputs, IDs, line numbers, or caveats required to continue safely.]
</critical_context>
"""


def get_model_context_window(
    model: str,
    provider_id: str | None = None,
    model_config: ModelConfig | None = None,
) -> int:
    """Get the context window size for a model.

    This function checks multiple sources in order:
    1. User-provided ``model_config.context_window`` override (highest priority)
    2. Models database cache (from models.dev), using ``model_config.provider_id``
       when available for more accurate lookup
    3. Built-in ``MODEL_CONTEXT_WINDOWS`` dictionary (substring match)
    4. Pattern-based family heuristics
    5. Default fallback

    Args:
        model: Model name or identifier
        provider_id: Optional provider ID for more accurate lookup
        model_config: Optional ModelConfig with explicit user overrides

    Returns:
        Context window size in tokens
    """
    # Highest priority: explicit user override from config
    if model_config and model_config.context_window:
        return model_config.context_window

    # Use provider_id from model_config if not explicitly provided
    effective_provider_id = provider_id or (model_config.provider_id if model_config else None)

    # Try to get from models database
    try:
        from .models_db import get_context_window_from_database

        db_result = get_context_window_from_database(model, effective_provider_id)
        if db_result != DEFAULT_CONTEXT_WINDOW:
            return db_result
    except ImportError:
        pass  # models_db not available
    except Exception as e:
        logger.debug(f"Failed to get context window from database: {e}")

    # Built-in table, with substring matching for provider prefixes and
    # dated snapshots (e.g. "gpt-5.5-2026-05-13" -> "gpt-5.5").
    builtin = match_builtin_context_window(model)
    if builtin is not None:
        return builtin

    # Last-resort family heuristics for models newer than the built-in table.
    # These track the current generation of each family rather than the
    # window it had when the heuristic was written; an over-estimate makes
    # compaction fire late, so they stay on the conservative side of each
    # family's most common value.
    model_lower = model.lower()
    for family, window in (
        ("gemini", 1_000_000),
        ("claude", 200_000),
        ("gpt-5", 400_000),
        ("gpt-4.1", 1_000_000),
        ("grok", 256_000),
        ("kimi", 262_144),
        ("minimax", 204_800),
        ("deepseek", 128_000),
        ("glm", 200_000),
        ("qwen", 131_072),
        ("llama", 128_000),
        ("mistral", 128_000),
        ("mixtral", 128_000),
    ):
        if family in model_lower:
            return window

    logger.warning(f"Unknown model '{model}', using default context window of {DEFAULT_CONTEXT_WINDOW}")
    return DEFAULT_CONTEXT_WINDOW


def resolve_model_output_limit(
    model: str,
    provider_id: str | None = None,
    model_config: ModelConfig | None = None,
) -> int | None:
    """Resolve the model output limit from config or the model database.

    Returns ``None`` when no source knows the model, so callers can choose
    between omitting the request cap and applying a fallback.
    """
    if model_config and model_config.output_limit and model_config.output_limit > 0:
        return model_config.output_limit

    effective_provider_id = provider_id or (model_config.provider_id if model_config else None)
    try:
        from .models_db import lookup_output_limit_from_database

        return lookup_output_limit_from_database(model, effective_provider_id)
    except ImportError:
        return None
    except Exception as e:
        logger.debug(f"Failed to resolve output limit from database: {e}")
        return None


def get_model_output_limit(
    model: str,
    provider_id: str | None = None,
    model_config: ModelConfig | None = None,
) -> int:
    """Get the model output limit, falling back to ``DEFAULT_OUTPUT_LIMIT``.

    Prefer :func:`resolve_model_output_limit` when an unknown model should not
    silently inherit the fallback.
    """
    resolved = resolve_model_output_limit(model, provider_id, model_config)
    if resolved is not None:
        return resolved
    return DEFAULT_OUTPUT_LIMIT


def estimate_tokens(messages: list[dict[str, Any]], use_tiktoken: bool = True) -> int:
    """Estimate token count for messages.

    Args:
        messages: List of message dictionaries
        use_tiktoken: Try to use tiktoken for more accurate estimation

    Returns:
        Estimated token count
    """
    if use_tiktoken:
        try:
            import tiktoken

            # Use cl100k_base encoding (OpenAI/Claude compatible)
            enc = tiktoken.get_encoding("cl100k_base")
            total = 0
            for msg in messages:
                # Count role token overhead
                total += 4  # <|im_start|>role<|im_sep|>...<|im_end|>

                content = msg.get("content", "")
                if isinstance(content, str):
                    total += len(enc.encode(content))
                elif isinstance(content, list):
                    # Handle multi-modal content
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            total += len(enc.encode(item["text"]))

                # Count tool calls
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        if isinstance(tc, dict):
                            name = tc.get("name", "") or tc.get("function", {}).get("name", "")
                            args = tc.get("arguments", "") or tc.get("function", {}).get("arguments", "")
                            total += len(enc.encode(name)) + len(enc.encode(args))

            return total
        except ImportError:
            pass

    # Fallback: rough estimation (4 chars ≈ 1 token)
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    total += len(item["text"]) // 4

        # Estimate tool calls
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict):
                    total += 50  # Overhead per tool call
                    args = tc.get("arguments", "") or tc.get("function", {}).get("arguments", "")
                    total += len(str(args)) // 4

    return total


def estimate_request_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    use_tiktoken: bool = True,
) -> int:
    """Estimate tokens for a complete model request, including tool schemas."""
    total = estimate_tokens(messages, use_tiktoken=use_tiktoken)
    if not tools:
        return total

    serialized_tools = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return total + estimate_tokens(
        [{"role": "system", "content": serialized_tools}],
        use_tiktoken=use_tiktoken,
    )


def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    """Convert messages to text for compaction."""
    parts = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if isinstance(content, str) and content.strip():
            parts.append(f"## Message {i + 1} ({role})\n{content}")

        # Include tool calls
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                name = tc.get("name", "") or tc.get("function", {}).get("name", "")
                args = tc.get("arguments", "") or tc.get("function", {}).get("arguments", "")
                parts.append(f"[Tool call: {name}]\nArgs: {args[:500]}...")

    return "\n\n".join(parts)


class SmartCompactor:
    """Smart context compaction with dynamic thresholds.

    This compactor automatically adjusts its behavior based on the model's
    context window size, ensuring optimal use of available context while
    preventing overflow.

    Example:
        compactor = SmartCompactor(client, model="gpt-5.5")

        # Automatic threshold calculation
        print(f"Using threshold: {compactor.threshold_tokens} tokens")

        if compactor.should_compact(messages):
            result = compactor.compact(messages)
            print(f"Reduced from {result.original_tokens} to {result.compacted_tokens}")
    """

    def __init__(
        self,
        client: Any,
        model: str,
        config: CompactionConfig | None = None,
        model_config: ModelConfig | None = None,
    ):
        """Initialize the smart compactor.

        Args:
            client: AnkaLoop LLM client for generating summaries
            model: Model name for context window detection
            config: Optional compaction configuration
            model_config: Optional ModelConfig with explicit user overrides
        """
        self.client = client
        self.model = model
        self.config = config or CompactionConfig()

        # Calculate dynamic thresholds
        self.context_window = get_model_context_window(model, model_config=model_config)
        self._update_thresholds()

        logger.info(
            f"SmartCompactor initialized for {model}: "
            f"context={self.context_window}, threshold={self.threshold_tokens}, "
            f"target={self.target_tokens}"
        )

    def _update_thresholds(self) -> None:
        """Update threshold values based on context window and config."""
        # Calculate available tokens (minus safety margin for response)
        available = int(self.context_window * (1 - self.config.safety_margin))

        # Threshold: when to trigger compaction
        self.threshold_tokens = int(available * self.config.threshold_ratio)

        # Target: what to aim for after compaction
        self.target_tokens = int(available * self.config.target_ratio)

        # Ensure target is reasonable
        self.target_tokens = max(self.target_tokens, self.config.min_tokens_to_compact)

    def should_compact(self, messages: list[dict[str, Any]]) -> bool:
        """Check if compaction is needed.

        Args:
            messages: Current message history

        Returns:
            True if compaction should be triggered
        """
        current_tokens = estimate_tokens(messages)

        # Don't compact tiny contexts
        if current_tokens < self.config.min_tokens_to_compact:
            return False

        return current_tokens > self.threshold_tokens

    def get_token_usage(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Get detailed token usage information.

        Args:
            messages: Current message history

        Returns:
            Dictionary with usage statistics
        """
        current = estimate_tokens(messages)
        return {
            "current_tokens": current,
            "context_window": self.context_window,
            "threshold_tokens": self.threshold_tokens,
            "target_tokens": self.target_tokens,
            "usage_ratio": current / self.context_window,
            "should_compact": current > self.threshold_tokens,
            "headroom_tokens": self.context_window - current,
        }

    def compact(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], CompactionResult]:
        """Compact messages using the configured strategy.

        Args:
            messages: Messages to compact

        Returns:
            Tuple of (compacted_messages, result)
        """
        if not messages:
            return messages, CompactionResult(
                original_tokens=0,
                compacted_tokens=0,
                messages_removed=0,
                messages_preserved=0,
                strategy_used=self.config.strategy,
            )

        original_tokens = estimate_tokens(messages)

        # Find split point for preserved messages
        preserve_idx, to_compact, to_preserve = self._split_messages(messages)

        if not to_compact:
            return messages, CompactionResult(
                original_tokens=original_tokens,
                compacted_tokens=original_tokens,
                messages_removed=0,
                messages_preserved=len(messages),
                strategy_used=self.config.strategy,
            )

        # Apply compaction strategy
        strategy = self.config.strategy
        if strategy == CompactionStrategy.TRUNCATE:
            compacted, summary = self._truncate(to_compact)
        elif strategy == CompactionStrategy.SLIDING_WINDOW:
            compacted, summary = self._sliding_window(to_compact)
        elif strategy == CompactionStrategy.HYBRID:
            compacted, summary = self._hybrid(to_compact)
        else:  # SUMMARY (default)
            compacted, summary = self._summarize(to_compact)

        # Build final message list
        result_messages = compacted + to_preserve
        compacted_tokens = estimate_tokens(result_messages)

        # Emit event
        self._emit_compaction_event(
            original_tokens=original_tokens,
            compacted_tokens=compacted_tokens,
            messages_removed=len(to_compact),
            strategy=strategy,
        )

        result = CompactionResult(
            original_tokens=original_tokens,
            compacted_tokens=compacted_tokens,
            messages_removed=len(to_compact),
            messages_preserved=len(to_preserve) + len(compacted),
            strategy_used=strategy,
            summary=summary,
        )

        logger.info(
            f"Compacted context: {original_tokens} -> {compacted_tokens} tokens "
            f"({len(to_compact)} messages summarized, {len(to_preserve)} preserved)"
        )

        return result_messages, result

    def compact_checkpoint(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], CompactionResult]:
        """Compact a committed prefix into a protocol-safe checkpoint message."""
        compacted, result = self.compact(messages)
        if not compacted:
            return compacted, result

        if len(compacted) == 1 and compacted[0].get("role") == "assistant" and not compacted[0].get("tool_calls"):
            checkpoint_context = compacted
        else:
            checkpoint_context = [
                {
                    "role": "assistant",
                    "content": (f"[Previous committed turns compacted]\n\n{_messages_to_text(compacted)}"),
                }
            ]
        result.compacted_tokens = estimate_tokens(checkpoint_context)
        return checkpoint_context, result

    def _split_messages(self, messages: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
        """Split messages into to_compact and to_preserve.

        Args:
            messages: All messages

        Returns:
            Tuple of (split_index, to_compact, to_preserve)
        """
        # Find split point - preserve last N user/assistant messages
        preserve_idx = len(messages)
        user_assistant_count = 0

        for i in range(len(messages) - 1, -1, -1):
            role = messages[i].get("role")
            if role in ("user", "assistant"):
                user_assistant_count += 1
                if user_assistant_count >= self.config.preserve_last:
                    preserve_idx = i
                    break

        # Also preserve recent tool results if configured
        if self.config.preserve_tool_results:
            tool_count = 0
            for i in range(preserve_idx - 1, -1, -1):
                if messages[i].get("role") == "tool":
                    tool_count += 1
                    if tool_count <= self.config.max_tool_results:
                        preserve_idx = i
                    else:
                        break

        to_compact = messages[:preserve_idx]
        to_preserve = messages[preserve_idx:]

        return preserve_idx, to_compact, to_preserve

    def _summarize(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """Summarize old messages using LLM.

        Args:
            messages: Messages to summarize

        Returns:
            Tuple of (compacted_messages, summary_text)
        """
        context_text = _messages_to_text(messages)
        token_count = estimate_tokens(messages)

        prompt = COMPACT_PROMPT.format(
            token_count=token_count,
            context=context_text,
            target_tokens=self.target_tokens,
        )

        try:
            resp = self.client.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful assistant that compacts conversation context for coding tasks.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=min(4000, self.target_tokens),
                temperature=0.3,  # Lower temperature for consistent summaries
            )
            summary = resp.content or ""
        except Exception as e:
            logger.warning(f"LLM summarization failed: {e}, falling back to truncation")
            return self._truncate(messages)

        compacted = [
            {
                "role": "assistant",
                "content": f"[Previous context compacted - {len(messages)} messages summarized]\n\n{summary}",
            }
        ]
        return compacted, summary

    def _truncate(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """Simple truncation strategy.

        Args:
            messages: Messages to truncate

        Returns:
            Tuple of (truncated_messages, summary_text)
        """
        # Keep first and last few messages
        if len(messages) <= 4:
            return messages, ""

        first = messages[:2]
        last = messages[-2:]
        removed_count = len(messages) - 4

        summary = f"[... {removed_count} messages truncated ...]"
        result = first + [{"role": "assistant", "content": summary}] + last

        return result, summary

    def _sliding_window(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """Sliding window strategy - keep most recent messages.

        Args:
            messages: Messages to process

        Returns:
            Tuple of (windowed_messages, summary_text)
        """
        # Calculate how many messages to keep
        target = self.target_tokens
        kept: list[dict[str, Any]] = []
        total_tokens = 0
        message_tokens = self._estimate_message_tokens(messages)

        for msg, msg_tokens in zip(reversed(messages), reversed(message_tokens), strict=False):
            if total_tokens + msg_tokens > target:
                break
            kept.insert(0, msg)
            total_tokens += msg_tokens

        removed_count = len(messages) - len(kept)
        if removed_count > 0:
            summary = f"[... {removed_count} older messages removed ...]"
            kept.insert(0, {"role": "assistant", "content": summary})
        else:
            summary = ""

        return kept, summary

    def _hybrid(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """Hybrid strategy - sliding window with summary of removed content.

        Args:
            messages: Messages to process

        Returns:
            Tuple of (compacted_messages, summary_text)
        """
        # First apply sliding window to get recent messages
        target = self.target_tokens // 2  # Leave room for summary
        kept: list[dict[str, Any]] = []
        total_tokens = 0
        message_tokens = self._estimate_message_tokens(messages)

        for msg, msg_tokens in zip(reversed(messages), reversed(message_tokens), strict=False):
            if total_tokens + msg_tokens > target:
                break
            kept.insert(0, msg)
            total_tokens += msg_tokens

        removed = messages[: len(messages) - len(kept)]

        if not removed:
            return kept, ""

        # Summarize removed messages
        try:
            context = _messages_to_text(removed)
            resp = self.client.chat(
                messages=[
                    {"role": "system", "content": "Summarize this conversation context in 2-3 paragraphs."},
                    {"role": "user", "content": context[:10000]},  # Limit input
                ],
                max_tokens=500,
                temperature=0.3,
            )
            summary = resp.content or ""
        except Exception:
            summary = f"[{len(removed)} older messages summarized]"

        result = [{"role": "assistant", "content": f"[Earlier context summary]\n{summary}"}] + kept
        return result, summary

    def _estimate_message_tokens(self, messages: list[dict[str, Any]]) -> list[int]:
        """Estimate token count for each message once."""
        return [estimate_tokens([msg]) for msg in messages]

    def _emit_compaction_event(
        self,
        original_tokens: int,
        compacted_tokens: int,
        messages_removed: int,
        strategy: CompactionStrategy,
    ) -> None:
        """Emit compaction event to event bus."""
        try:
            from .event_bus import Event, EventType, get_event_bus

            event = Event(
                type=EventType.CONTEXT_COMPACTED,
                source="compactor",
                data={
                    "original_tokens": original_tokens,
                    "compacted_tokens": compacted_tokens,
                    "messages_removed": messages_removed,
                    "strategy": strategy.value,
                    "model": self.model,
                    "context_window": self.context_window,
                    "threshold_tokens": self.threshold_tokens,
                },
            )
            get_event_bus().emit_sync(event)
        except Exception:
            pass  # Don't fail compaction if event emission fails


# Backward compatibility alias
Compactor = SmartCompactor


def create_compactor(
    client: Any,
    model: str,
    strategy: str = "summary",
    threshold_ratio: float = 0.7,
    target_ratio: float = 0.3,
    model_config: ModelConfig | None = None,
) -> SmartCompactor:
    """Create a smart compactor with custom settings.

    Args:
        client: AnkaLoop LLM client
        model: Model name
        strategy: Compaction strategy ("summary", "truncate", "sliding_window", "hybrid")
        threshold_ratio: When to trigger compaction (ratio of context window)
        target_ratio: Target size after compaction
        model_config: Optional ModelConfig with explicit user overrides

    Returns:
        Configured SmartCompactor instance
    """
    strategy_enum = {
        "summary": CompactionStrategy.SUMMARY,
        "truncate": CompactionStrategy.TRUNCATE,
        "sliding_window": CompactionStrategy.SLIDING_WINDOW,
        "hybrid": CompactionStrategy.HYBRID,
    }.get(strategy, CompactionStrategy.SUMMARY)

    config = CompactionConfig(
        strategy=strategy_enum,
        threshold_ratio=threshold_ratio,
        target_ratio=target_ratio,
    )

    return SmartCompactor(client, model, config, model_config=model_config)
