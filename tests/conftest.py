import os

import pytest


@pytest.fixture(autouse=True)
def _clear_otel_env(monkeypatch):
    """Keep tests from inheriting a developer's or CI's OTEL export setup."""
    for name in tuple(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def sample_config(tmp_path):
    """Create a sample config file."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[chat]
base_url = "https://api.example.com/v1"
model = "test-model"
api_key = "test-key"

[servers.test]
url = "https://test.mcp.server"
""")
    return config_file
