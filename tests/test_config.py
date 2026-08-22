import ankaloop.config as config_module


def test_config_dir_default():
    assert config_module.CONFIG_DIR.name == "ankaloop"
    assert "config" in str(config_module.CONFIG_DIR).lower()


def test_config_dir_name_override(monkeypatch, tmp_path):
    """Legacy deployments can keep the old config dir via ANKA_CONFIG_DIR_NAME."""
    import importlib

    import ankaloop.config as cfg_mod
    import ankaloop.constants as constants_mod

    monkeypatch.setenv("ANKA_CONFIG_DIR_NAME", "amcp")
    importlib.reload(constants_mod)
    importlib.reload(cfg_mod)
    assert cfg_mod.CONFIG_DIR.name == "amcp"

    monkeypatch.setenv("ANKA_CONFIG_DIR_NAME", "ankaloop")
    importlib.reload(constants_mod)
    importlib.reload(cfg_mod)
    assert cfg_mod.CONFIG_DIR.name == "ankaloop"


def test_load_config():
    config = config_module.load_config()
    assert isinstance(config, config_module.AnkaloopConfig)
    assert isinstance(config.servers, dict)
    assert config.context is None or isinstance(config.context, config_module.ContextConfig)


def test_decode_encode_context_roundtrip():
    raw = {
        "progressive_tools": True,
        "progressive_skills": False,
        "response_ratio": 0.25,
        "min_prompt_budget": 1500,
        "base_prompt_max_tokens": 1200,
        "tool_budget_ratio": 0.5,
        "skill_budget_ratio": 0.2,
        "memory_budget_ratio": 0.2,
        "rules_budget_ratio": 0.1,
        "tool_relevance_threshold": 0.2,
        "skill_relevance_threshold": 0.3,
        "tool_tiers": {"task": "hidden"},
    }

    cfg = config_module._decode_context(raw)
    assert cfg is not None
    assert cfg.progressive_skills is False
    assert cfg.tool_tiers["task"] == "hidden"

    encoded = config_module._encode_context(cfg)
    assert encoded is not None
    assert encoded["min_prompt_budget"] == 1500
    assert encoded["tool_tiers"]["task"] == "hidden"


def test_decode_encode_chat_tool_limits_roundtrip():
    raw = {
        "tool_loop_limit": 300,
        "bash_tool_limit": 100,
        "default_max_lines": 400,
        "sync_tool_settle_timeout_seconds": 0.75,
    }

    cfg = config_module._decode_chat(raw)
    assert cfg is not None
    assert cfg.tool_loop_limit == 300
    assert cfg.bash_tool_limit == 100
    assert cfg.sync_tool_settle_timeout_seconds == 0.75

    encoded = config_module._encode_chat(cfg)
    assert encoded is not None
    assert encoded["tool_loop_limit"] == 300
    assert encoded["bash_tool_limit"] == 100
    assert encoded["sync_tool_settle_timeout_seconds"] == 0.75


def test_decode_encode_provider_reliability_policy_roundtrip():
    raw = {
        "request_timeout_seconds": 45.5,
        "max_retries": 4,
        "retry_base_delay_seconds": 0.25,
        "active_provider": "backup",
        "providers": {
            "backup": {
                "api_type": "anthropic",
                "model": "backup-model",
                "request_timeout_seconds": 30,
                "max_retries": 1,
                "retry_base_delay_seconds": 0.1,
            }
        },
    }

    cfg = config_module._decode_chat(raw)

    assert cfg is not None
    assert cfg.request_timeout_seconds == 30
    assert cfg.max_retries == 1
    assert cfg.retry_base_delay_seconds == 0.1
    encoded = config_module._encode_chat(cfg)
    assert encoded is not None
    assert encoded["providers"]["backup"]["request_timeout_seconds"] == 30.0
    assert encoded["providers"]["backup"]["max_retries"] == 1
    assert encoded["providers"]["backup"]["retry_base_delay_seconds"] == 0.1


def test_default_chat_uses_gmi_without_api_key():
    cfg = config_module._decode_chat(config_module._DEFAULT["chat"])

    assert cfg is not None
    assert cfg.active_provider == "gmi"
    assert cfg.base_url == "https://api.gmi-serving.com/v1"
    assert cfg.model == "openai/gpt-5.5"
    assert cfg.api_type == "gmi"
    assert cfg.api_key is None
    assert "gmi" in cfg.providers
    assert cfg.providers["gmi"].api_key is None
    assert cfg.model_config is not None
    assert cfg.model_config.provider_id == "gmi"
    assert cfg.model_config.context_window == 1_050_000


def test_default_config_has_no_user_visible_mcp_servers():
    assert config_module._DEFAULT["servers"] == {}


def test_decode_chat_active_provider_applies_profile():
    raw = {
        "base_url": "https://primary.example/v1",
        "model": "primary-model",
        "active_provider": "backup",
        "providers": {
            "primary": {
                "base_url": "https://primary.example/v1",
                "model": "primary-model",
                "api_type": "openai",
            },
            "backup": {
                "base_url": "https://backup.example/v1",
                "model": "backup-model",
                "api_type": "anthropic",
                "model_config": {
                    "provider_id": "anthropic",
                    "model_id": "backup-model",
                    "context_window": 200000,
                },
            },
        },
    }

    cfg = config_module._decode_chat(raw)
    assert cfg is not None
    assert cfg.active_provider == "backup"
    assert cfg.base_url == "https://backup.example/v1"
    assert cfg.model == "backup-model"
    assert cfg.api_type == "anthropic"
    assert cfg.model_config is not None
    assert cfg.model_config.provider_id == "anthropic"

    encoded = config_module._encode_chat(cfg)
    assert encoded is not None
    assert encoded["active_provider"] == "backup"
    assert encoded["providers"]["backup"]["model"] == "backup-model"
    assert "base_url" not in encoded
    assert "model" not in encoded
    assert "api_type" not in encoded
    assert "model_config" not in encoded


def test_active_provider_reuses_key_for_same_base_url():
    cfg = config_module._decode_chat(
        {
            "active_provider": "second-model",
            "providers": {
                "credential-source": {
                    "base_url": "https://provider.example/v1",
                    "model": "first-model",
                    "api_key": "shared-key",
                },
                "second-model": {
                    "base_url": "https://provider.example/v1",
                    "model": "second-model",
                },
            },
        }
    )

    assert cfg is not None
    assert cfg.api_key == "shared-key"


def test_decode_encode_server_config_roundtrip():
    raw = {
        "host": "0.0.0.0",
        "port": 8080,
        "auth": {"enabled": True, "api_key": "test-key"},
        "cors": {
            "enabled": True,
            "allow_origins": ["https://example.com"],
            "allow_methods": ["GET"],
            "allow_headers": ["Authorization"],
            "allow_credentials": False,
        },
        "session_timeout_minutes": 30,
        "max_sessions": 10,
        "work_dir": "/tmp/ankaloop",
        "default_agent": "coder",
    }

    cfg = config_module._decode_server_config(raw)
    assert cfg is not None
    assert cfg.host == "0.0.0.0"
    assert cfg.auth.api_key == "test-key"
    assert cfg.cors.allow_origins == ["https://example.com"]
    assert cfg.cors.allow_credentials is False

    encoded = config_module._encode_server_config(cfg)
    assert encoded == raw


def test_decode_encode_automation_roundtrip():
    raw = {
        "enabled": True,
        "default_timeout": 180,
        "jobs": [
            {
                "name": "ci-check",
                "command": "Check CI status",
                "skill": "ci-checker",
                "enabled": True,
                "notify": True,
                "timeout": 90,
                "tags": ["ci"],
                "schedule": "*/15 * * * *",
            }
        ],
    }

    cfg = config_module._decode_automation(raw)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.default_timeout == 180
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "ci-check"
    assert cfg.jobs[0].timeout == 90

    encoded = config_module._encode_automation(cfg)
    assert encoded is not None
    assert encoded["default_timeout"] == 180
    assert encoded["jobs"][0]["name"] == "ci-check"


def test_decode_automation_uses_default_timeout():
    cfg = config_module._decode_automation(
        {
            "default_timeout": 222,
            "jobs": [
                {
                    "name": "job-a",
                    "command": "run it",
                }
            ],
        }
    )
    assert cfg is not None
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].timeout == 222


def test_decode_encode_automation_none():
    assert config_module._decode_automation(None) is None
    assert config_module._encode_automation(None) is None


def test_decode_encode_telegram_roundtrip_with_policies():
    raw = {
        "enabled": True,
        "bot_token": "token",
        "allowed_users": [1],
        "admin_users": [1],
        "dm_policy": "pairing",
        "group_policy": "mention",
        "group_allow_users": [2, 3],
        "typing_indicator": True,
        "typing_interval_seconds": 5,
        "max_queue_size": 25,
        "pairing": {
            "enabled": True,
            "code_ttl_seconds": 900,
            "max_pending": 50,
        },
        "groups": {
            "-100123": {
                "enabled": True,
                "group_policy": "allowlist",
                "require_mention": True,
                "allow_users": [4],
                "topics": {
                    "42": {
                        "enabled": True,
                        "group_policy": "open",
                        "require_mention": False,
                        "allow_users": [5],
                    }
                },
            }
        },
    }

    cfg = config_module._decode_telegram(raw)
    assert cfg is not None
    assert cfg.dm_policy == "pairing"
    assert cfg.group_policy == "mention"
    assert cfg.group_allow_users == [2, 3]
    assert cfg.typing_interval_seconds == 5
    assert cfg.max_queue_size == 25
    assert cfg.pairing.code_ttl_seconds == 900
    assert cfg.groups["-100123"].topics["42"].group_policy == "open"

    encoded = config_module._encode_telegram(cfg)
    assert encoded is not None
    assert encoded["dm_policy"] == "pairing"
    assert encoded["group_policy"] == "mention"
    assert encoded["pairing"]["max_pending"] == 50
    assert encoded["groups"]["-100123"]["topics"]["42"]["group_policy"] == "open"


def test_decode_telegram_invalid_policy_falls_back_to_defaults():
    cfg = config_module._decode_telegram(
        {
            "dm_policy": "invalid",
            "group_policy": "invalid",
            "typing_interval_seconds": 0,
            "max_queue_size": 0,
        }
    )

    assert cfg is not None
    assert cfg.dm_policy == "allowlist"
    assert cfg.group_policy == "mention"
    assert cfg.typing_interval_seconds == 1
    assert cfg.max_queue_size == 1


def test_decode_encode_provider_extra_headers_roundtrip():
    raw = {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "test-model",
        "api_type": "openai",
        "extra_headers": {
            "HTTP-Referer": "https://myapp.example",
            "X-OpenRouter-Title": "My App",
        },
    }
    provider = config_module._decode_chat_provider(raw)
    assert provider.extra_headers == {
        "HTTP-Referer": "https://myapp.example",
        "X-OpenRouter-Title": "My App",
    }
    encoded = config_module._encode_chat_provider(provider)
    assert encoded["extra_headers"] == raw["extra_headers"]


def test_active_provider_applies_extra_headers():
    raw = {
        "active_provider": "openrouter",
        "providers": {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "model": "test-model",
                "api_type": "openai",
                "extra_headers": {"X-Custom": "yes"},
            },
            "other": {
                "base_url": "https://other.example/v1",
                "model": "other-model",
                "api_type": "openai",
            },
        },
    }
    chat = config_module._decode_chat(raw)
    assert chat is not None
    assert chat.extra_headers == {"X-Custom": "yes"}
