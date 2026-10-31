# Dynamic Model Database Integration

This document describes the dynamic model database integration in AMCP, which allows automatic fetching and caching of model specifications from [models.dev](https://models.dev).

## Overview

AMCP now supports dynamic model configuration through an external data source, eliminating the need to manually update hardcoded model specifications when new models are released.

### Data Source

- **Primary Source**: [models.dev API](https://models.dev/api.json)
- **Cache Location**: `~/.config/amcp/cache/models.json`
- **Cache TTL**: 7 days (configurable)
- **Fallback**: Built-in model list for offline operation

## Interactive Initialization

When you run `amcp init`, an interactive wizard guides you through configuration:

```bash
amcp init
```

### Wizard Steps

1. **Download Model Database**
   - Option to fetch latest model data from models.dev
   - Downloads provider and model information including context windows

2. **Select Provider**
   - Choose from popular providers (OpenAI, Anthropic, Google, DeepSeek, etc.)
   - Or configure a custom provider not in the database

3. **Select Model**
   - Browse available models for the selected provider
   - See context window sizes, features (tool calling, reasoning, etc.)

4. **Configure API Key**
   - Enter API key or use environment variables
   - Environment variable hints based on provider

### Quick Init (Legacy)

For users who prefer the old behavior:

```bash
amcp init --quick
```

This skips the wizard and creates a default configuration file.

## Configuration File

The configuration file now includes model metadata:

```toml
[chat]
base_url = "https://api.openai.com/v1"
model = "gpt-4o"
# api_key = "..."  # Optional, can use env var

[chat.model_config]
provider_id = "openai"
model_id = "gpt-4o"
context_window = 128000
# output_limit = 16384
# is_custom = false
```

### Custom Providers

For providers not in the models.dev database:

```toml
[chat]
base_url = "https://api.custom-provider.com/v1"
model = "custom-model"

[chat.model_config]
provider_id = "custom-provider"
model_id = "custom-model"
context_window = 32000  # Must specify manually
is_custom = true
```

When `is_custom = true`, AMCP uses the specified `context_window` directly instead of looking it up in the database.

## Context Window Resolution

AnkaLoop resolves context window sizes in the following priority order:

1. **User-specified override** in config (`context_window` in `[chat.model_config]`)
2. **Models database cache** (from models.dev)
3. **Built-in fallback table** (`MODEL_CONTEXT_WINDOWS` in `compaction.py`, mirrored on models.dev)
4. **Pattern-based family heuristics** (e.g., "claude" → 200K)
5. **Default fallback**: 200,000 tokens

## Programmatic Usage

### Fetching Models

```python
from amcp.models_db import get_models_database, fetch_models_from_api

# Get database (uses cache if valid)
db = get_models_database()

# Force refresh from API
db = get_models_database(force_refresh=True)

# List providers
for provider_id in db.list_providers():
    provider = db.get_provider(provider_id)
    print(f"{provider.name}: {len(provider.models)} models")
```

### Getting Context Windows

```python
from amcp import get_context_window_from_database, get_model_context_window

# From database (specific provider)
ctx = get_context_window_from_database("gpt-4o", provider_id="openai")

# Automatic resolution (database → built-in → pattern → default)
ctx = get_model_context_window("gpt-4o")
```

### Model Information

```python
from amcp.models_db import ModelsDatabase, load_models_cache

db = load_models_cache()
if db:
    model, provider = db.find_model_by_name("claude-3.5-sonnet")
    if model:
        print(f"Model: {model.name}")
        print(f"Context: {model.context_window:,}")
        print(f"Tool Call: {model.tool_call}")
        print(f"Provider: {provider.name}")
```

## Data Structure

### Provider Information

| Field | Description |
|-------|-------------|
| `id` | Provider identifier (e.g., "openai") |
| `name` | Display name (e.g., "OpenAI") |
| `api_url` | Base API URL |
| `env_vars` | Environment variables for API key |
| `doc_url` | Documentation URL |
| `models` | Dictionary of models |

### Model Information

| Field | Description |
|-------|-------------|
| `id` | Model identifier |
| `name` | Display name |
| `family` | Model family (e.g., "GPT-4") |
| `context_window` | Maximum context size in tokens |
| `output_limit` | Maximum output tokens |
| `tool_call` | Supports tool/function calling |
| `reasoning` | Has reasoning capabilities (o1-style) |
| `attachment` | Supports file attachments |
| `cost_input` | Cost per million input tokens |
| `cost_output` | Cost per million output tokens |

## Cache Management

### View Cache Status

```bash
ls -la ~/.config/amcp/cache/
```

### Clear Cache

```python
from amcp.models_db import delete_models_cache

delete_models_cache()  # Forces re-fetch on next run
```

### Check Cache Validity

```python
from amcp.models_db import is_cache_valid

if not is_cache_valid(ttl_days=7):
    print("Cache expired or missing")
```

## Offline Operation

AMCP works fully offline using:

1. **Cached database** (if previously fetched)
2. **Built-in model table** (current generation of every major family)
3. **Pattern matching** for common model families
4. **200K default** for unknown models

## Best Practices

1. **Run `amcp init` periodically** to update the model database
2. **Specify context_window for custom models** to ensure optimal context management
3. **Use provider_id for accurate lookups** when programmatically accessing model data
4. **Check for new models** when upgrading to new AI providers

## Error Handling

- **Network failures**: Falls back to cached data or built-in values
- **Invalid cache**: Automatically re-fetches from API
- **Unknown models**: Uses pattern matching and finally defaults to 200K
- **API rate limits**: Cache reduces API calls to ~once per week
