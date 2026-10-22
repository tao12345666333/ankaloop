"""Tests for the models database module."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ankaloop.models_db import (
    BUILTIN_MODELS,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_OUTPUT_LIMIT,
    ModelInfo,
    ModelsDatabase,
    ProviderInfo,
    get_context_window_from_database,
    get_output_limit_from_database,
    load_models_cache,
    lookup_output_limit_from_database,
    save_models_cache,
)


class TestModelInfo:
    """Tests for ModelInfo dataclass."""

    def test_default_values(self):
        """Test default values."""
        model = ModelInfo(id="test-model", name="Test Model")
        assert model.id == "test-model"
        assert model.name == "Test Model"
        assert model.context_window == DEFAULT_CONTEXT_WINDOW
        assert model.tool_call is False

    def test_from_api_data(self):
        """Test creating from API data."""
        api_data = {
            "name": "GPT-5.5",
            "family": "GPT-5",
            "limit": {"context": 128000, "output": 4096},
            "tool_call": True,
            "reasoning": False,
            "cost": {"input": 10.0, "output": 30.0},
        }
        model = ModelInfo.from_api_data("gpt-5.5", api_data, "openai")

        assert model.id == "gpt-5.5"
        assert model.name == "GPT-5.5"
        assert model.context_window == 128000
        assert model.output_limit == 4096
        assert model.tool_call is True
        assert model.provider_id == "openai"


class TestProviderInfo:
    """Tests for ProviderInfo dataclass."""

    def test_default_values(self):
        """Test default values."""
        provider = ProviderInfo(id="test", name="Test Provider")
        assert provider.id == "test"
        assert provider.name == "Test Provider"
        assert provider.models == {}

    def test_from_api_data(self):
        """Test creating from API data."""
        api_data = {
            "name": "OpenAI",
            "api": "https://api.openai.com/v1",
            "env": ["OPENAI_API_KEY"],
            "models": {
                "gpt-5.5": {
                    "name": "GPT-5.5",
                    "limit": {"context": 128000},
                }
            },
        }
        provider = ProviderInfo.from_api_data("openai", api_data)

        assert provider.id == "openai"
        assert provider.name == "OpenAI"
        assert provider.api_url == "https://api.openai.com/v1"
        assert "OPENAI_API_KEY" in provider.env_vars
        assert "gpt-5.5" in provider.models


class TestModelsDatabase:
    """Tests for ModelsDatabase class."""

    def test_empty_database(self):
        """Test empty database."""
        db = ModelsDatabase()
        assert db.providers == {}
        assert db.list_providers() == []

    def test_get_provider(self):
        """Test getting a provider."""
        provider = ProviderInfo(id="test", name="Test")
        db = ModelsDatabase(providers={"test": provider})

        assert db.get_provider("test") == provider
        assert db.get_provider("nonexistent") is None

    def test_get_model(self):
        """Test getting a model."""
        model = ModelInfo(id="test-model", name="Test Model")
        provider = ProviderInfo(id="test", name="Test", models={"test-model": model})
        db = ModelsDatabase(providers={"test": provider})

        assert db.get_model("test", "test-model") == model
        assert db.get_model("test", "nonexistent") is None
        assert db.get_model("nonexistent", "test-model") is None

    def test_find_model_by_name(self):
        """Test finding a model by name."""
        model = ModelInfo(id="gpt-5.5", name="GPT-5.5")
        provider = ProviderInfo(id="openai", name="OpenAI", models={"gpt-5.5": model})
        db = ModelsDatabase(providers={"openai": provider})

        found_model, found_provider = db.find_model_by_name("gpt-5.5")
        assert found_model == model
        assert found_provider == provider

        # Test partial matching
        found_model, found_provider = db.find_model_by_name("gpt-5.5")
        assert found_model == model

    def test_to_dict_and_from_dict(self):
        """Test serialization round-trip."""
        model = ModelInfo(id="test-model", name="Test", context_window=100000)
        provider = ProviderInfo(id="test", name="Test Provider", models={"test-model": model})
        db = ModelsDatabase(providers={"test": provider}, fetched_at="2024-01-01T00:00:00")

        data = db.to_dict()
        restored = ModelsDatabase.from_dict(data)

        assert restored.fetched_at == "2024-01-01T00:00:00"
        assert "test" in restored.providers
        assert "test-model" in restored.providers["test"].models
        assert restored.providers["test"].models["test-model"].context_window == 100000


class TestBuiltinModels:
    """Tests for built-in model fallbacks."""

    def test_builtin_models_exist(self):
        """Test that built-in models are defined."""
        # Check that some models are defined (specific models may change)
        assert len(BUILTIN_MODELS) > 0
        # Check that known categories are represented
        has_openai = any("gpt" in m.lower() for m in BUILTIN_MODELS)
        has_anthropic = any("claude" in m.lower() for m in BUILTIN_MODELS)
        has_glm = any("glm" in m.lower() for m in BUILTIN_MODELS)
        assert has_openai or has_anthropic or has_glm, "Should have at least one major provider"

    def test_builtin_values_reasonable(self):
        """Test that built-in values are reasonable."""
        for model, ctx in BUILTIN_MODELS.items():
            assert ctx >= 8_000, f"{model} context too small"
            assert ctx <= 2_000_000, f"{model} context too large"


class TestGetContextWindowFromDatabase:
    """Tests for get_context_window_from_database function."""

    def test_returns_builtin_when_no_cache(self):
        """Test fallback to built-in values."""
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            # Use a model from BUILTIN_MODELS
            first_model = next(iter(BUILTIN_MODELS.keys()))
            result = get_context_window_from_database(first_model)
            assert result == BUILTIN_MODELS[first_model]

    def test_returns_default_for_unknown(self):
        """Test default for unknown models."""
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            result = get_context_window_from_database("unknown-model-xyz")
            assert result == DEFAULT_CONTEXT_WINDOW

    def test_uses_database_when_available(self):
        """Test using database values."""
        model = ModelInfo(id="custom-model", name="Custom", context_window=999999)
        provider = ProviderInfo(id="custom", name="Custom", models={"custom-model": model})
        db = ModelsDatabase(providers={"custom": provider})

        with patch("ankaloop.models_db.load_models_cache", return_value=db):
            result = get_context_window_from_database("custom-model")
            assert result == 999999


class TestLookupOutputLimitFromDatabase:
    """Tests for lookup_output_limit_from_database (no silent fallback)."""

    def test_returns_none_when_no_cache(self):
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            assert lookup_output_limit_from_database("unknown-model-xyz") is None

    def test_returns_none_for_unknown_model(self):
        model = ModelInfo(id="custom-model", name="Custom", output_limit=4096)
        provider = ProviderInfo(id="custom", name="Custom", models={"custom-model": model})
        db = ModelsDatabase(providers={"custom": provider})

        with patch("ankaloop.models_db.load_models_cache", return_value=db):
            assert lookup_output_limit_from_database("unknown-model-xyz") is None

    def test_returns_limit_by_name_and_provider(self):
        model = ModelInfo(id="custom-model", name="Custom", provider_id="custom", output_limit=4096)
        provider = ProviderInfo(id="custom", name="Custom", models={"custom-model": model})
        db = ModelsDatabase(providers={"custom": provider})

        with patch("ankaloop.models_db.load_models_cache", return_value=db):
            assert lookup_output_limit_from_database("custom-model") == 4096
            assert lookup_output_limit_from_database("custom-model", provider_id="custom") == 4096

    def test_get_output_limit_falls_back_to_default(self):
        with patch("ankaloop.models_db.load_models_cache", return_value=None):
            assert get_output_limit_from_database("unknown-model-xyz") == DEFAULT_OUTPUT_LIMIT


class TestCacheFunctions:
    """Tests for cache save/load functions."""

    def test_save_and_load_cache(self):
        """Test saving and loading cache."""
        model = ModelInfo(id="test", name="Test", context_window=50000)
        provider = ProviderInfo(id="provider", name="Provider", models={"test": model})
        db = ModelsDatabase(providers={"provider": provider}, fetched_at="2024-01-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "models.json"

            with (
                patch("ankaloop.models_db.MODELS_CACHE_FILE", cache_file),
                patch("ankaloop.models_db.CACHE_DIR", Path(tmpdir)),
            ):
                # Save
                save_models_cache(db)

                # Verify file exists
                assert cache_file.exists()

                # Load
                loaded = load_models_cache()
                assert loaded is not None
                assert loaded.fetched_at == "2024-01-01"
                assert "provider" in loaded.providers

    def test_load_returns_none_when_no_cache(self):
        """Test load returns None when no cache file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "nonexistent.json"

            with patch("ankaloop.models_db.MODELS_CACHE_FILE", cache_file):
                result = load_models_cache()
                assert result is None
