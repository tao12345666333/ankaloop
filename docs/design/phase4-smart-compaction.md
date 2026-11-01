# AMCP Phase 4: Smart Compaction

This document describes the Smart Compaction feature implemented in AMCP v0.4.0, which provides intelligent context compaction based on model context window sizes.

## Overview

Smart Compaction automatically manages conversation context length by:

1. **Detecting model context windows** - Automatically determines the context window size for 40+ popular models
2. **Dynamic thresholds** - Calculates compaction thresholds based on the model's capacity
3. **Multiple strategies** - Supports summary, truncate, sliding window, and hybrid strategies
4. **Token estimation** - Uses tiktoken when available for accurate token counting
5. **Event integration** - Emits events when compaction occurs for monitoring

## Model Context Windows

AnkaLoop resolves context windows from [models.dev](https://models.dev) when its cache is
available, and falls back to a built-in table otherwise. The table is shared by the compaction
thresholds and the offline model lookup, so both agree on the same model.

The table pins only the current generation of each family, from the oldest release still worth
answering for. Older releases and families without an entry are left to the models.dev cache and
then to a per-family heuristic, rather than being frozen at a number that goes stale:

| Model family | Oldest pinned release | Context window |
|--------------|-----------------------|----------------|
| **OpenAI GPT** | gpt-5.5 (also covers gpt-5.6 and gpt-6) | 1,050,000 |
| **Anthropic Claude** | claude-opus-5, claude-sonnet-5, claude-haiku-5, claude-fable-5 | 1,000,000 |
| **Google Gemini** | gemini-3.6 (also covers 3.7, 3.8 and 4.x) | 1,048,576 |
| **DeepSeek** | deepseek-v4 (also covers v4-flash / v4-pro) | 1,048,576 |
| **ZAI/GLM** | glm-5.2 (also covers glm-5.3) | 1,000,000 |
| **Moonshot Kimi** | kimi-k3 | 1,048,576 |
| **MiniMax** | minimax-m3 (also covers m3.1) | 1,000,000 |

Matching is case-insensitive and substring-based, so any prefix or suffix resolves to the same
entry: `group/deepseek-v4-flash`, `deepseek/deepseek-v4-flash`, `deepseek-v4-flash@group` and
`DeepSeek-V4-Flash` all get 1,048,576. A table entry answers a model only when the model carries
the same version segment as the key, so a current-generation entry is never borrowed by an older
release: `claude-opus-5` answers `anthropic/claude-opus-5-5` but not `claude-opus-4-5`, and
`gemini-4` answers `gemini-4-argon` but not `gemini-2.5-pro`. Newer point releases of a pinned
family inherit its window (`gpt-6.1-sol`, `kimi-k3.1-pro`).

Models missing from both the cache and the table fall back to a per-family heuristic, whose value
is the family's median window on models.dev (Claude → 200K, GPT-5 → 400K, Grok → 500K, Qwen and
Mistral → 262K), and finally to a 200,000-token default. Non-conversational variants (embeddings,
TTS, image generation) get 8,192 rather than their family's window, since they ship a far smaller
one.

Set `context_window` in `[chat.model_config]` when a gateway serves a narrower window than the
upstream API: an under-estimate only compacts early, but an over-estimate makes the request-size
preflight reject a request the provider would have accepted.

## How It Works

### Dynamic Thresholds

The SmartCompactor calculates thresholds based on the model's context window:

```python
from amcp import SmartCompactor

# For claude-sonnet-4-5 (200K context):
compactor = SmartCompactor(client, model="claude-sonnet-4-5")
# threshold = 126,000 tokens (70% of available context)
# target = 54,000 tokens (30% after compaction)

# For gpt-5.5 (1.05M context):
compactor = SmartCompactor(client, model="gpt-5.5")
# threshold = 661,500 tokens
# target = 283,500 tokens
```

### Configuration

Customize compaction behavior with `CompactionConfig`:

```python
from amcp import SmartCompactor, CompactionConfig, CompactionStrategy

config = CompactionConfig(
    strategy=CompactionStrategy.SUMMARY,  # summary, truncate, sliding_window, hybrid
    threshold_ratio=0.7,  # Compact when > 70% of context used
    target_ratio=0.3,     # Aim for 30% after compaction
    preserve_last=6,      # Keep last 6 user/assistant messages
    preserve_tool_results=True,  # Preserve recent tool results
    max_tool_results=10,  # Maximum tool results to preserve
    min_tokens_to_compact=5000,  # Don't compact tiny contexts
    safety_margin=0.1,    # 10% margin for response generation
)

compactor = SmartCompactor(client, model="gpt-5.5", config=config)
```

## Compaction Strategies

### 1. Summary (Default)

Uses the LLM to create an intelligent summary of old context:

```python
config = CompactionConfig(strategy=CompactionStrategy.SUMMARY)
```

**Best for**: Long coding sessions where context from earlier conversations is important.

The summary is structured:
```
<current_task>
What we're working on now
</current_task>

<completed>
- Task 1: Outcome
- Task 2: Outcome
</completed>

<code_state>
Key files and their current state
</code_state>

<important>
Critical context, errors, decisions made
</important>
```

### 2. Truncate

Simple removal of old messages, keeping first and last few:

```python
config = CompactionConfig(strategy=CompactionStrategy.TRUNCATE)
```

**Best for**: Fast compaction when context is less important.

### 3. Sliding Window

Keeps the most recent messages that fit in the target size:

```python
config = CompactionConfig(strategy=CompactionStrategy.SLIDING_WINDOW)
```

**Best for**: Sessions where only recent context matters.

### 4. Hybrid

Combines sliding window with a summary of removed content:

```python
config = CompactionConfig(strategy=CompactionStrategy.HYBRID)
```

**Best for**: Balance between summary quality and speed.

## Usage Examples

### Basic Usage

```python
from amcp import SmartCompactor, get_model_context_window

# Check model's context window
window = get_model_context_window("gpt-5.5")
print(f"Context window: {window:,} tokens")

# Create compactor
compactor = SmartCompactor(client, model="gpt-5.5")

# Check if compaction is needed
if compactor.should_compact(messages):
    compacted_messages, result = compactor.compact(messages)
    print(f"Reduced from {result.original_tokens} to {result.compacted_tokens}")
```

### Token Usage Monitoring

```python
# Get detailed token usage
usage = compactor.get_token_usage(messages)
print(f"Current: {usage['current_tokens']:,} tokens")
print(f"Usage: {usage['usage_ratio']:.1%} of context")
print(f"Headroom: {usage['headroom_tokens']:,} tokens")
print(f"Should compact: {usage['should_compact']}")
```

### Factory Function

```python
from amcp import create_compactor

# Quick creation with custom settings
compactor = create_compactor(
    client,
    model="gpt-5.5",
    strategy="hybrid",
    threshold_ratio=0.8,
    target_ratio=0.4,
)
```

### Event Monitoring

```python
from amcp import get_event_bus, EventType

@get_event_bus().on(EventType.CONTEXT_COMPACTED)
async def on_compaction(event):
    data = event.data
    print(f"Context compacted: {data['original_tokens']} -> {data['compacted_tokens']}")
    print(f"Strategy: {data['strategy']}")
    print(f"Model: {data['model']}")
```

## Token Estimation

AMCP provides accurate token estimation:

```python
from amcp import estimate_tokens

messages = [
    {"role": "user", "content": "Hello, how are you?"},
    {"role": "assistant", "content": "I'm doing well, thank you!"},
]

tokens = estimate_tokens(messages)
print(f"Estimated tokens: {tokens}")
```

Features:
- Uses `tiktoken` library when available (recommended)
- Falls back to character-based estimation (4 chars ≈ 1 token)
- Accounts for message role overhead
- Counts tool calls separately

To get accurate estimation, install tiktoken:
```bash
pip install tiktoken
```

## Compaction Result

The `compact()` method returns a `CompactionResult` with details:

```python
messages, result = compactor.compact(messages)

print(f"Original tokens: {result.original_tokens}")
print(f"Compacted tokens: {result.compacted_tokens}")
print(f"Messages removed: {result.messages_removed}")
print(f"Messages preserved: {result.messages_preserved}")
print(f"Strategy used: {result.strategy_used.value}")
print(f"Summary: {result.summary[:100]}...")  # If summary strategy
```

## Integration with Agent

The SmartCompactor is integrated into the Agent class and is used automatically during long conversations. Configure via `config.toml`:

```toml
[chat]
model = "gpt-5.5"
# Compaction is automatic based on model's context window
```

## Best Practices

1. **Choose the right strategy**:
   - Use `SUMMARY` for important context
   - Use `TRUNCATE` for speed
   - Use `HYBRID` for balance

2. **Adjust thresholds based on use case**:
   - Lower threshold (0.5-0.6) for frequent compaction
   - Higher threshold (0.8-0.9) for maximum context usage

3. **Preserve recent context**:
   - Keep `preserve_last` at 4-8 for conversation continuity
   - Enable `preserve_tool_results` for coding tasks

4. **Monitor compaction events**:
   - Subscribe to `CONTEXT_COMPACTED` events
   - Log compaction statistics for debugging

## Version

Smart Compaction was added in **AMCP v0.4.0**.
