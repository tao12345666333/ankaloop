"""Model database module for fetching and caching model information from models.dev."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

# Re-exported so existing ``models_db.DEFAULT_OUTPUT_LIMIT`` consumers keep
# working; compaction owns the single definition.
from .compaction import DEFAULT_OUTPUT_LIMIT
from .constants import CONFIG_DIR

logger = logging.getLogger(__name__)

# Default cache directory
CACHE_DIR = CONFIG_DIR / "cache"
MODELS_CACHE_FILE = CACHE_DIR / "models.json"
MODELS_DEV_API_URL = "https://models.dev/api.json"

# Cache TTL (7 days)
DEFAULT_CACHE_TTL_DAYS = 7

# Default context window for unknown models
DEFAULT_CONTEXT_WINDOW = 200_000


@dataclass
class ModelInfo:
    """Information about a single model."""

    id: str
    name: str
    family: str = ""
    provider_id: str = ""
    context_window: int = DEFAULT_CONTEXT_WINDOW
    output_limit: int = DEFAULT_OUTPUT_LIMIT
    tool_call: bool = False
    reasoning: bool = False
    attachment: bool = False
    knowledge: str = ""
    release_date: str = ""
    cost_input: float = 0.0
    cost_output: float = 0.0
    modalities_input: list[str] = field(default_factory=list)
    modalities_output: list[str] = field(default_factory=list)

    @classmethod
    def from_api_data(cls, model_id: str, data: dict[str, Any], provider_id: str) -> ModelInfo:
        """Create ModelInfo from models.dev API data."""
        limit = data.get("limit", {})
        cost = data.get("cost", {})
        modalities = data.get("modalities", {})

        return cls(
            id=model_id,
            name=data.get("name", model_id),
            family=data.get("family", ""),
            provider_id=provider_id,
            context_window=limit.get("context", DEFAULT_CONTEXT_WINDOW),
            output_limit=limit.get("output", DEFAULT_OUTPUT_LIMIT),
            tool_call=data.get("tool_call", False),
            reasoning=data.get("reasoning", False),
            attachment=data.get("attachment", False),
            knowledge=data.get("knowledge", ""),
            release_date=data.get("release_date", ""),
            cost_input=cost.get("input", 0.0),
            cost_output=cost.get("output", 0.0),
            modalities_input=modalities.get("input", []),
            modalities_output=modalities.get("output", []),
        )


@dataclass
class ProviderInfo:
    """Information about a model provider."""

    id: str
    name: str
    api_url: str = ""
    env_vars: list[str] = field(default_factory=list)
    doc_url: str = ""
    models: dict[str, ModelInfo] = field(default_factory=dict)

    @classmethod
    def from_api_data(cls, provider_id: str, data: dict[str, Any]) -> ProviderInfo:
        """Create ProviderInfo from models.dev API data."""
        models = {}
        for model_id, model_data in data.get("models", {}).items():
            models[model_id] = ModelInfo.from_api_data(model_id, model_data, provider_id)

        return cls(
            id=provider_id,
            name=data.get("name", provider_id),
            api_url=data.get("api", ""),
            env_vars=data.get("env", []),
            doc_url=data.get("doc", ""),
            models=models,
        )


@dataclass
class ModelsDatabase:
    """Database of models from models.dev."""

    providers: dict[str, ProviderInfo] = field(default_factory=dict)
    fetched_at: str = ""

    @classmethod
    def from_api_data(cls, data: dict[str, Any]) -> ModelsDatabase:
        """Create ModelsDatabase from models.dev API data."""
        providers = {}
        for provider_id, provider_data in data.items():
            providers[provider_id] = ProviderInfo.from_api_data(provider_id, provider_data)

        return cls(
            providers=providers,
            fetched_at=datetime.utcnow().isoformat(),
        )

    def get_provider(self, provider_id: str) -> ProviderInfo | None:
        """Get a provider by ID."""
        return self.providers.get(provider_id)

    def get_model(self, provider_id: str, model_id: str) -> ModelInfo | None:
        """Get a model by provider and model ID."""
        provider = self.get_provider(provider_id)
        if provider:
            return provider.models.get(model_id)
        return None

    def get_context_window(self, provider_id: str, model_id: str) -> int:
        """Get the context window for a model."""
        model = self.get_model(provider_id, model_id)
        if model:
            return model.context_window
        return DEFAULT_CONTEXT_WINDOW

    def find_model_by_name(self, model_name: str) -> tuple[ModelInfo | None, ProviderInfo | None]:
        """Find a model by name or ID across all providers.

        Returns (model_info, provider_info) or (None, None) if not found.
        """
        model_lower = model_name.lower()

        for provider in self.providers.values():
            for model in provider.models.values():
                # Exact match
                if model.id == model_name or model.id.lower() == model_lower:
                    return model, provider
                # Name match
                if model.name.lower() == model_lower:
                    return model, provider

        # Try partial matching
        for provider in self.providers.values():
            for model in provider.models.values():
                if model_lower in model.id.lower() or model_lower in model.name.lower():
                    return model, provider

        return None, None

    def list_providers(self) -> list[str]:
        """List all provider IDs."""
        return sorted(self.providers.keys())

    def list_models(self, provider_id: str) -> list[str]:
        """List all model IDs for a provider."""
        provider = self.get_provider(provider_id)
        if provider:
            return sorted(provider.models.keys())
        return []

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        providers_dict = {}
        for pid, provider in self.providers.items():
            models_dict = {}
            for mid, model in provider.models.items():
                models_dict[mid] = {
                    "id": model.id,
                    "name": model.name,
                    "family": model.family,
                    "provider_id": model.provider_id,
                    "context_window": model.context_window,
                    "output_limit": model.output_limit,
                    "tool_call": model.tool_call,
                    "reasoning": model.reasoning,
                    "attachment": model.attachment,
                    "knowledge": model.knowledge,
                    "release_date": model.release_date,
                    "cost_input": model.cost_input,
                    "cost_output": model.cost_output,
                    "modalities_input": model.modalities_input,
                    "modalities_output": model.modalities_output,
                }
            providers_dict[pid] = {
                "id": provider.id,
                "name": provider.name,
                "api_url": provider.api_url,
                "env_vars": provider.env_vars,
                "doc_url": provider.doc_url,
                "models": models_dict,
            }
        return {
            "providers": providers_dict,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelsDatabase:
        """Create from dictionary (loaded from cache)."""
        providers = {}
        for pid, pdata in data.get("providers", {}).items():
            models = {}
            for mid, mdata in pdata.get("models", {}).items():
                models[mid] = ModelInfo(
                    id=mdata.get("id", mid),
                    name=mdata.get("name", mid),
                    family=mdata.get("family", ""),
                    provider_id=mdata.get("provider_id", pid),
                    context_window=mdata.get("context_window", DEFAULT_CONTEXT_WINDOW),
                    output_limit=mdata.get("output_limit", DEFAULT_OUTPUT_LIMIT),
                    tool_call=mdata.get("tool_call", False),
                    reasoning=mdata.get("reasoning", False),
                    attachment=mdata.get("attachment", False),
                    knowledge=mdata.get("knowledge", ""),
                    release_date=mdata.get("release_date", ""),
                    cost_input=mdata.get("cost_input", 0.0),
                    cost_output=mdata.get("cost_output", 0.0),
                    modalities_input=mdata.get("modalities_input", []),
                    modalities_output=mdata.get("modalities_output", []),
                )
            providers[pid] = ProviderInfo(
                id=pdata.get("id", pid),
                name=pdata.get("name", pid),
                api_url=pdata.get("api_url", ""),
                env_vars=pdata.get("env_vars", []),
                doc_url=pdata.get("doc_url", ""),
                models=models,
            )
        return cls(
            providers=providers,
            fetched_at=data.get("fetched_at", ""),
        )


def fetch_models_from_api(timeout: float = 30.0) -> ModelsDatabase:
    """Fetch models data from models.dev API.

    Args:
        timeout: Request timeout in seconds

    Returns:
        ModelsDatabase with all providers and models

    Raises:
        httpx.HTTPError: If the request fails
    """
    logger.info(f"Fetching models from {MODELS_DEV_API_URL}")

    with httpx.Client(timeout=timeout) as client:
        response = client.get(MODELS_DEV_API_URL)
        response.raise_for_status()
        data = response.json()

    db = ModelsDatabase.from_api_data(data)
    logger.info(f"Fetched {len(db.providers)} providers with models")

    return db


def save_models_cache(db: ModelsDatabase) -> Path:
    """Save models database to cache file.

    Args:
        db: ModelsDatabase to save

    Returns:
        Path to the cache file
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    with open(MODELS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(db.to_dict(), f, indent=2)

    logger.info(f"Saved models cache to {MODELS_CACHE_FILE}")
    return MODELS_CACHE_FILE


def load_models_cache() -> ModelsDatabase | None:
    """Load models database from cache file.

    Returns:
        ModelsDatabase if cache exists and is valid, None otherwise
    """
    if not MODELS_CACHE_FILE.exists():
        logger.debug("Models cache file does not exist")
        return None

    try:
        with open(MODELS_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)

        db = ModelsDatabase.from_dict(data)
        logger.debug(f"Loaded models cache from {MODELS_CACHE_FILE}")
        return db
    except Exception as e:
        logger.warning(f"Failed to load models cache: {e}")
        return None


def is_cache_valid(ttl_days: int = DEFAULT_CACHE_TTL_DAYS) -> bool:
    """Check if the cache is still valid.

    Args:
        ttl_days: Cache time-to-live in days

    Returns:
        True if cache exists and is not expired
    """
    db = load_models_cache()
    if not db or not db.fetched_at:
        return False

    try:
        fetched_at = datetime.fromisoformat(db.fetched_at)
        expiry = fetched_at + timedelta(days=ttl_days)
        return datetime.utcnow() < expiry
    except Exception:
        return False


def get_models_database(
    force_refresh: bool = False,
    ttl_days: int = DEFAULT_CACHE_TTL_DAYS,
) -> ModelsDatabase | None:
    """Get the models database, using cache if available.

    Args:
        force_refresh: Force fetching from API even if cache is valid
        ttl_days: Cache time-to-live in days

    Returns:
        ModelsDatabase if available, None if fetch fails and no cache
    """
    # Try to use cache if valid
    if not force_refresh and is_cache_valid(ttl_days):
        db = load_models_cache()
        if db:
            return db

    # Try to fetch from API
    try:
        db = fetch_models_from_api()
        save_models_cache(db)
        return db
    except Exception as e:
        logger.warning(f"Failed to fetch models from API: {e}")

        # Fall back to cache (even if expired)
        db = load_models_cache()
        if db:
            logger.info("Using expired cache as fallback")
            return db

        return None


def has_models_cache() -> bool:
    """Check if models cache exists."""
    return MODELS_CACHE_FILE.exists()


def delete_models_cache() -> bool:
    """Delete the models cache file.

    Returns:
        True if cache was deleted, False if it didn't exist
    """
    if MODELS_CACHE_FILE.exists():
        MODELS_CACHE_FILE.unlink()
        logger.info(f"Deleted models cache: {MODELS_CACHE_FILE}")
        return True
    return False


# Built-in fallback models (used when no cache available)
BUILTIN_MODELS: dict[str, int] = {
    # OpenAI
    "gpt-5.1-codex": 400_000,
    "gpt-5.2": 400_000,
    "gpt-5.5": 400_000,
    # Anthropic
    "claude-4.5-sonnet": 200_000,
    "claude-4.5-opus": 200_000,
    # Google
    "gemini-3-pro": 1_048_576,
    # ZAI/GLM
    "glm-4.6": 204_800,
    "glm-4.7": 204_800,
    # MiniMax
    "minimax-m2.1": 204_800,
}


def get_context_window_from_database(
    model_name: str,
    provider_id: str | None = None,
) -> int:
    """Get context window for a model, checking database and fallbacks.

    Args:
        model_name: Model name or ID
        provider_id: Optional provider ID for more accurate lookup

    Returns:
        Context window size in tokens
    """
    # Try to load from database
    db = load_models_cache()

    if db:
        if provider_id:
            model = db.get_model(provider_id, model_name)
            if model:
                return model.context_window

        # Try to find by name
        model, _ = db.find_model_by_name(model_name)
        if model:
            return model.context_window

    # Check built-in fallbacks
    if model_name in BUILTIN_MODELS:
        return BUILTIN_MODELS[model_name]

    # Try partial matching on built-in
    model_lower = model_name.lower()
    for known_model, window in BUILTIN_MODELS.items():
        if known_model in model_lower or model_lower.startswith(known_model):
            return window

    # Default
    logger.debug(f"Unknown model '{model_name}', using default context window")
    return DEFAULT_CONTEXT_WINDOW


def lookup_output_limit_from_database(
    model_name: str,
    provider_id: str | None = None,
) -> int | None:
    """Look up the maximum output size for a model without applying a fallback.

    Returns ``None`` when the cached database does not know the model, so
    callers can distinguish "no data" from a real limit.
    """
    db = load_models_cache()

    if db:
        if provider_id:
            model = db.get_model(provider_id, model_name)
            if model and model.output_limit > 0:
                return model.output_limit

        model, _ = db.find_model_by_name(model_name)
        if model and model.output_limit > 0:
            return model.output_limit

    return None


def get_output_limit_from_database(
    model_name: str,
    provider_id: str | None = None,
) -> int:
    """Get the maximum output size for a model from the cached model database."""
    limit = lookup_output_limit_from_database(model_name, provider_id)
    if limit is not None:
        return limit
    return DEFAULT_OUTPUT_LIMIT
