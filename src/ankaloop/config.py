"""Configuration management for AnkaLoop."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import tomli_w  # type: ignore

from .constants import CONFIG_DIR_NAME
from .telegram.config import (
    TelegramConfig,
    TelegramGroupConfig,
    TelegramNotificationsConfig,
    TelegramPairingConfig,
    TelegramStreamingConfig,
    TelegramTopicConfig,
    apply_env_overrides,
    normalize_dm_policy,
    normalize_group_policy,
    parse_user_ids,
)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / CONFIG_DIR_NAME
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class Server:
    # stdio transport fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http(sse) transport fields
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ModelConfig:
    """Configuration for a specific model."""

    # Model identification
    provider_id: str | None = None  # Provider ID from models.dev (e.g., "openai", "anthropic")
    model_id: str | None = None  # Model ID (e.g., "gpt-5.5", "claude-4.5-sonnet")

    # Custom settings (override database values)
    context_window: int | None = None  # Override context window size
    output_limit: int | None = None  # Override output limit

    # Whether this is a custom model (not from models.dev database)
    is_custom: bool = False


@dataclass
class ChatProviderConfig:
    """Named LLM provider profile for runtime switching."""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_type: str | None = None  # any-llm provider ID, or "openai_responses"
    model_config: ModelConfig | None = None
    request_timeout_seconds: float | None = None
    max_retries: int | None = None
    retry_base_delay_seconds: float | None = None
    extra_headers: dict[str, str] | None = None  # custom HTTP headers (e.g. OpenRouter attribution)


@dataclass
class ChatConfig:
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_type: str | None = None  # any-llm provider ID, or "openai_responses"

    # Model configuration (from models.dev or custom)
    model_config: ModelConfig | None = None

    # Provider reliability policy. Retries are only attempted for transient
    # failures and never after a streaming response emitted content.
    request_timeout_seconds: float = 120.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 0.5

    # Named provider profiles. ``active_provider`` selects which profile is copied
    # into the top-level chat fields at load time so existing call sites keep working.
    active_provider: str | None = None
    providers: dict[str, ChatProviderConfig] = field(default_factory=dict)

    # Custom HTTP headers forwarded to the LLM provider (e.g. OpenRouter app attribution).
    extra_headers: dict[str, str] | None = None

    # Tool calling settings
    tool_loop_limit: int | None = None
    bash_tool_limit: int | None = None
    default_max_lines: int | None = None
    read_roots: list[str] | None = None  # list of allowed root paths for read_file
    # MCP tool exposure
    mcp_tools_enabled: bool | None = None
    mcp_servers: list[str] | None = None
    # which servers' tools to expose; if unset, expose all configured servers
    # Built-in file modification tools
    write_tool_enabled: bool | None = None
    edit_tool_enabled: bool | None = None
    sync_tool_settle_timeout_seconds: float = 2.0
    # Agent settings
    default_agent: str | None = None  # default agent to use: "coder", "explorer", etc.
    # Queue settings
    enable_queue: bool | None = None  # enable message queue (default: True)
    max_queue_size: int | None = None  # max queued messages per session (default: 100)


@dataclass
class ContextConfig:
    """Configuration for progressive context optimization."""

    progressive_tools: bool = True
    progressive_skills: bool = True

    response_ratio: float = 0.30
    min_prompt_budget: int = 2500
    base_prompt_max_tokens: int = 2200

    tool_budget_ratio: float = 0.45
    skill_budget_ratio: float = 0.30
    memory_budget_ratio: float = 0.15
    rules_budget_ratio: float = 0.10

    tool_relevance_threshold: float = 0.12
    skill_relevance_threshold: float = 0.25

    tool_tiers: dict[str, str] = field(default_factory=dict)


@dataclass
class CORSConfig:
    """HTTP server CORS configuration."""

    enabled: bool = True
    allow_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:*",
            "http://127.0.0.1:*",
            "tauri://localhost",
        ]
    )
    allow_methods: list[str] = field(default_factory=lambda: ["*"])
    allow_headers: list[str] = field(default_factory=lambda: ["*"])
    allow_credentials: bool = True


@dataclass
class AuthConfig:
    enabled: bool = False
    api_key: str | None = None


@dataclass
class ServerConfig:
    """Configuration for AnkaLoop Server."""

    host: str = "127.0.0.1"
    port: int = 8080
    auth: AuthConfig = field(default_factory=AuthConfig)
    cors: CORSConfig = field(default_factory=CORSConfig)
    session_timeout_minutes: int = 60 * 24
    max_sessions: int = 100
    work_dir: str | None = None
    default_agent: str = "default"


@dataclass
class AutomationJobConfig:
    """Configuration for a single automation job."""

    name: str = ""
    command: str = ""
    skill: str | None = None
    enabled: bool = True
    notify: bool = True
    timeout: int = 300
    work_dir: str | None = None
    tags: list[str] = field(default_factory=list)
    schedule: str | None = None


@dataclass
class AutomationConfig:
    """Lightweight automation config for external orchestrators."""

    enabled: bool = True
    default_timeout: int = 300
    jobs: list[AutomationJobConfig] = field(default_factory=list)


@dataclass
class AnkaloopConfig:
    servers: dict[str, Server]
    chat: ChatConfig | None = None
    context: ContextConfig | None = None
    server: ServerConfig | None = None
    telegram: TelegramConfig | None = None
    automation: AutomationConfig | None = None


_DEFAULT = {
    "servers": {},
    "server": {
        "host": "127.0.0.1",
        "port": 8080,
        "auth": {
            "enabled": False,
        },
        "cors": {
            "enabled": True,
            "allow_origins": [
                "http://localhost:*",
                "http://127.0.0.1:*",
                "tauri://localhost",
            ],
            "allow_methods": ["*"],
            "allow_headers": ["*"],
            "allow_credentials": True,
        },
        "session_timeout_minutes": 1440,
        "max_sessions": 100,
        "default_agent": "default",
    },
    "chat": {
        "base_url": "https://api.gmi-serving.com/v1",
        "model": "openai/gpt-5.5",
        "active_provider": "gmi",
        "providers": {
            "gmi": {
                "base_url": "https://api.gmi-serving.com/v1",
                "model": "openai/gpt-5.5",
                "api_type": "gmi",
                "model_config": {
                    "provider_id": "gmi",
                    "model_id": "openai/gpt-5.5",
                    "context_window": 1050000,
                    "output_limit": 131072,
                },
            },
        },
        # "api_key": ""  # optional; users can add this if they want config-based auth
        "tool_loop_limit": 300,
        "bash_tool_limit": 100,
        "default_max_lines": 400,
        # "read_roots": ["."]  # optional; defaults to current working directory when unset
        "mcp_tools_enabled": True,
        # "mcp_servers": ["custom"]  # optional; if unset, expose all configured servers
        "write_tool_enabled": True,
        "edit_tool_enabled": True,
        "sync_tool_settle_timeout_seconds": 2.0,
    },
    "context": {
        "progressive_tools": True,
        "progressive_skills": True,
        "response_ratio": 0.30,
        "min_prompt_budget": 2500,
        "base_prompt_max_tokens": 2200,
        "tool_budget_ratio": 0.45,
        "skill_budget_ratio": 0.30,
        "memory_budget_ratio": 0.15,
        "rules_budget_ratio": 0.10,
        "tool_relevance_threshold": 0.12,
        "skill_relevance_threshold": 0.25,
        "tool_tiers": {
            "read_file": "always",
            "grep": "always",
            "think": "always",
            "web_search": "always",
            "web_fetch": "always",
            "apply_patch": "frequent",
            "write_file": "frequent",
            "bash": "always",
            "todo": "frequent",
            "memory": "on_demand",
            "task": "on_demand",
            "mcp__*": "on_demand",
        },
    },
    "telegram": {
        "enabled": False,
        "allowed_users": [],
        "admin_users": [],
        "webhook_mode": False,
        "webhook_url": "",
        "max_message_length": 4096,
        "rate_limit_messages": 20,
        "session_timeout": 3600,
        "dm_policy": "allowlist",
        "group_policy": "mention",
        "group_allow_users": [],
        "typing_indicator": True,
        "typing_interval_seconds": 4,
        "max_queue_size": 20,
        "pairing": {
            "enabled": True,
            "code_ttl_seconds": 1800,
            "max_pending": 200,
        },
        "groups": {},
        "notifications": {
            "ci_failures": True,
            "pr_reviews": True,
            "task_completions": True,
            "error_alerts": True,
        },
    },
    "automation": {
        "enabled": True,
        "default_timeout": 300,
        "jobs": [],
    },
}


def _decode_server(name: str, raw: Mapping[str, object]) -> Server:
    command = raw.get("command")
    command_s = str(command) if command is not None else None
    raw_args = raw.get("args")
    args = [str(x) for x in raw_args] if isinstance(raw_args, list) else []
    raw_env = raw.get("env")
    env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
    url = raw.get("url")
    url_s = str(url) if url is not None else None
    raw_headers = raw.get("headers")
    headers = {str(k): str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
    return Server(command=command_s, args=args, env=env, url=url_s, headers=headers)


def _decode_model_config(raw: Mapping[str, object] | None) -> ModelConfig | None:
    """Decode model_config section from TOML."""
    if not raw:
        return None
    provider_id = raw.get("provider_id")
    model_id = raw.get("model_id")
    context_window = raw.get("context_window")
    output_limit = raw.get("output_limit")
    return ModelConfig(
        provider_id=str(provider_id) if provider_id else None,
        model_id=str(model_id) if model_id else None,
        context_window=int(str(context_window)) if context_window else None,
        output_limit=int(str(output_limit)) if output_limit else None,
        is_custom=bool(raw.get("is_custom", False)),
    )


def _decode_chat_provider(raw: Mapping[str, object]) -> ChatProviderConfig:
    """Decode a named chat provider profile from TOML."""
    base_url = raw.get("base_url")
    model = raw.get("model")
    api_key = raw.get("api_key")
    api_type = raw.get("api_type")
    request_timeout_seconds = raw.get("request_timeout_seconds")
    max_retries = raw.get("max_retries")
    retry_base_delay_seconds = raw.get("retry_base_delay_seconds")
    raw_model_config = raw.get("model_config")
    model_config = _decode_model_config(raw_model_config) if isinstance(raw_model_config, dict) else None
    raw_extra_headers = raw.get("extra_headers")
    extra_headers: dict[str, str] | None = None
    if isinstance(raw_extra_headers, dict):
        extra_headers = {str(k): str(v) for k, v in raw_extra_headers.items()}
    return ChatProviderConfig(
        base_url=str(base_url) if base_url is not None else None,
        model=str(model) if model is not None else None,
        api_key=str(api_key) if api_key is not None else None,
        api_type=str(api_type) if api_type is not None else None,
        model_config=model_config,
        request_timeout_seconds=(float(str(request_timeout_seconds)) if request_timeout_seconds is not None else None),
        max_retries=int(str(max_retries)) if max_retries is not None else None,
        retry_base_delay_seconds=(
            float(str(retry_base_delay_seconds)) if retry_base_delay_seconds is not None else None
        ),
        extra_headers=extra_headers,
    )


def _apply_active_provider(chat: ChatConfig) -> ChatConfig:
    """Copy the selected provider profile into top-level chat fields."""
    if not chat.active_provider:
        return chat
    provider = chat.providers.get(chat.active_provider)
    if provider is None:
        return chat
    chat.base_url = provider.base_url
    chat.model = provider.model
    chat.api_key = provider.api_key
    if chat.api_key is None and provider.base_url:
        chat.api_key = next(
            (
                candidate.api_key
                for candidate in chat.providers.values()
                if candidate.base_url == provider.base_url and candidate.api_key
            ),
            None,
        )
    chat.api_type = provider.api_type
    chat.model_config = provider.model_config
    if provider.request_timeout_seconds is not None:
        chat.request_timeout_seconds = provider.request_timeout_seconds
    if provider.max_retries is not None:
        chat.max_retries = provider.max_retries
    if provider.retry_base_delay_seconds is not None:
        chat.retry_base_delay_seconds = provider.retry_base_delay_seconds
    if provider.extra_headers is not None:
        chat.extra_headers = provider.extra_headers
    return chat


def _decode_chat(raw: Mapping[str, object] | None) -> ChatConfig | None:
    if not raw:
        return None
    base_url = raw.get("base_url")
    model = raw.get("model")
    api_key = raw.get("api_key")
    api_type = raw.get("api_type")
    request_timeout_seconds = raw.get("request_timeout_seconds", 120.0)
    max_retries = raw.get("max_retries", 2)
    retry_base_delay_seconds = raw.get("retry_base_delay_seconds", 0.5)
    tool_loop_limit = raw.get("tool_loop_limit")
    bash_tool_limit = raw.get("bash_tool_limit")
    default_max_lines = raw.get("default_max_lines")
    read_roots = raw.get("read_roots")
    mcp_tools_enabled = raw.get("mcp_tools_enabled")
    mcp_servers = raw.get("mcp_servers")
    write_tool_enabled = raw.get("write_tool_enabled")
    edit_tool_enabled = raw.get("edit_tool_enabled")
    sync_tool_settle_timeout_seconds = raw.get("sync_tool_settle_timeout_seconds", 2.0)
    # Agent settings
    default_agent = raw.get("default_agent")
    # Queue settings
    enable_queue = raw.get("enable_queue")
    max_queue_size = raw.get("max_queue_size")
    # Model config
    raw_model_config = raw.get("model_config")
    model_config = _decode_model_config(raw_model_config) if isinstance(raw_model_config, dict) else None
    raw_extra_headers = raw.get("extra_headers")
    extra_headers: dict[str, str] | None = None
    if isinstance(raw_extra_headers, dict):
        extra_headers = {str(k): str(v) for k, v in raw_extra_headers.items()}

    active_provider = raw.get("active_provider")
    raw_providers = raw.get("providers")
    providers: dict[str, ChatProviderConfig] = {}
    if isinstance(raw_providers, dict):
        for name, provider_raw in raw_providers.items():
            if isinstance(provider_raw, dict):
                providers[str(name)] = _decode_chat_provider(provider_raw)

    chat = ChatConfig(
        base_url=str(base_url) if base_url is not None else None,
        model=str(model) if model is not None else None,
        api_key=str(api_key) if api_key is not None else None,
        api_type=str(api_type) if api_type is not None else None,
        model_config=model_config,
        request_timeout_seconds=float(str(request_timeout_seconds)),
        max_retries=int(str(max_retries)),
        retry_base_delay_seconds=float(str(retry_base_delay_seconds)),
        active_provider=str(active_provider) if active_provider is not None else None,
        providers=providers,
        tool_loop_limit=int(str(tool_loop_limit)) if tool_loop_limit is not None else None,
        bash_tool_limit=int(str(bash_tool_limit)) if bash_tool_limit is not None else None,
        default_max_lines=int(str(default_max_lines)) if default_max_lines is not None else None,
        read_roots=[str(p) for p in read_roots] if isinstance(read_roots, list) else None,
        mcp_tools_enabled=bool(mcp_tools_enabled) if mcp_tools_enabled is not None else None,
        mcp_servers=[str(s) for s in mcp_servers] if isinstance(mcp_servers, list) else None,
        write_tool_enabled=bool(write_tool_enabled) if write_tool_enabled is not None else None,
        edit_tool_enabled=bool(edit_tool_enabled) if edit_tool_enabled is not None else None,
        sync_tool_settle_timeout_seconds=float(str(sync_tool_settle_timeout_seconds)),
        default_agent=str(default_agent) if default_agent is not None else None,
        enable_queue=bool(enable_queue) if enable_queue is not None else None,
        max_queue_size=int(str(max_queue_size)) if max_queue_size is not None else None,
        extra_headers=extra_headers,
    )
    return _apply_active_provider(chat)


def _decode_context(raw: Mapping[str, object] | None) -> ContextConfig | None:
    if not raw:
        return None

    tool_tiers_raw = raw.get("tool_tiers")
    tool_tiers = {str(k): str(v) for k, v in tool_tiers_raw.items()} if isinstance(tool_tiers_raw, dict) else {}

    return ContextConfig(
        progressive_tools=bool(raw.get("progressive_tools", True)),
        progressive_skills=bool(raw.get("progressive_skills", True)),
        response_ratio=float(str(raw.get("response_ratio", 0.30))),
        min_prompt_budget=int(str(raw.get("min_prompt_budget", 2500))),
        base_prompt_max_tokens=int(str(raw.get("base_prompt_max_tokens", 2200))),
        tool_budget_ratio=float(str(raw.get("tool_budget_ratio", 0.45))),
        skill_budget_ratio=float(str(raw.get("skill_budget_ratio", 0.30))),
        memory_budget_ratio=float(str(raw.get("memory_budget_ratio", 0.15))),
        rules_budget_ratio=float(str(raw.get("rules_budget_ratio", 0.10))),
        tool_relevance_threshold=float(str(raw.get("tool_relevance_threshold", 0.12))),
        skill_relevance_threshold=float(str(raw.get("skill_relevance_threshold", 0.25))),
        tool_tiers=tool_tiers,
    )


def _decode_auth(raw: Mapping[str, object] | None) -> AuthConfig:
    """Decode HTTP server auth config from TOML."""
    if not raw:
        return AuthConfig()
    api_key = raw.get("api_key")
    if api_key is None:
        legacy_api_keys = raw.get("api_keys")
        if isinstance(legacy_api_keys, list) and legacy_api_keys:
            api_key = legacy_api_keys[0]
    return AuthConfig(
        enabled=bool(raw.get("enabled", False)),
        api_key=str(api_key) if api_key is not None else None,
    )


def _decode_cors(raw: Mapping[str, object] | None, legacy_origins: object = None) -> CORSConfig:
    """Decode HTTP server CORS config from TOML."""
    if not raw:
        origins = [str(origin) for origin in legacy_origins] if isinstance(legacy_origins, list) else None
        return CORSConfig(allow_origins=origins) if origins is not None else CORSConfig()
    allow_origins = raw.get("allow_origins")
    allow_methods = raw.get("allow_methods")
    allow_headers = raw.get("allow_headers")
    return CORSConfig(
        enabled=bool(raw.get("enabled", True)),
        allow_origins=(
            [str(origin) for origin in allow_origins] if isinstance(allow_origins, list) else CORSConfig().allow_origins
        ),
        allow_methods=[str(method) for method in allow_methods] if isinstance(allow_methods, list) else ["*"],
        allow_headers=[str(header) for header in allow_headers] if isinstance(allow_headers, list) else ["*"],
        allow_credentials=bool(raw.get("allow_credentials", True)),
    )


def _decode_server_config(raw: Mapping[str, object] | None) -> ServerConfig | None:
    """Decode HTTP server config from TOML."""
    if not raw:
        return None
    auth_raw = raw.get("auth")
    cors_raw = raw.get("cors")
    return ServerConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(str(raw.get("port", 8080))),
        auth=_decode_auth(auth_raw if isinstance(auth_raw, dict) else None),
        cors=_decode_cors(cors_raw if isinstance(cors_raw, dict) else None, raw.get("cors_origins")),
        session_timeout_minutes=int(str(raw.get("session_timeout_minutes", 1440))),
        max_sessions=int(str(raw.get("max_sessions", 100))),
        work_dir=str(raw["work_dir"]) if raw.get("work_dir") else None,
        default_agent=str(raw.get("default_agent", "default")),
    )


def _decode_telegram_notifications(raw: Mapping[str, object] | None) -> TelegramNotificationsConfig:
    if not raw:
        return TelegramNotificationsConfig()
    return TelegramNotificationsConfig(
        ci_failures=bool(raw.get("ci_failures", True)),
        pr_reviews=bool(raw.get("pr_reviews", True)),
        task_completions=bool(raw.get("task_completions", True)),
        error_alerts=bool(raw.get("error_alerts", True)),
    )


def _decode_telegram_pairing(raw: Mapping[str, object] | None) -> TelegramPairingConfig:
    if not raw:
        return TelegramPairingConfig()
    return TelegramPairingConfig(
        enabled=bool(raw.get("enabled", True)),
        code_ttl_seconds=max(60, int(str(raw.get("code_ttl_seconds", 1800)))),
        max_pending=max(1, int(str(raw.get("max_pending", 200)))),
    )


def _decode_telegram_streaming(raw: Mapping[str, object] | None) -> TelegramStreamingConfig:
    cfg = TelegramStreamingConfig()
    if not raw:
        return cfg
    if "streaming_enabled" in raw:
        cfg.streaming_enabled = bool(raw.get("streaming_enabled"))
    if "streaming_interval_seconds" in raw and raw.get("streaming_interval_seconds") is not None:
        cfg.streaming_interval_seconds = max(0.3, float(str(raw.get("streaming_interval_seconds"))))
    if "streaming_min_chars" in raw and raw.get("streaming_min_chars") is not None:
        cfg.streaming_min_chars = max(1, int(str(raw.get("streaming_min_chars"))))
    if "streaming_max_chars" in raw and raw.get("streaming_max_chars") is not None:
        cfg.streaming_max_chars = max(100, int(str(raw.get("streaming_max_chars"))))
    return cfg


def _decode_telegram_topic(raw: Mapping[str, object] | None) -> TelegramTopicConfig:
    if not raw:
        return TelegramTopicConfig()
    require_mention_raw = raw.get("require_mention")
    return TelegramTopicConfig(
        enabled=bool(raw.get("enabled", True)),
        group_policy=(
            normalize_group_policy(str(raw.get("group_policy"))) if raw.get("group_policy") is not None else None
        ),
        require_mention=(bool(require_mention_raw) if require_mention_raw is not None else None),
        allow_users=parse_user_ids(raw.get("allow_users")),
    )


def _decode_telegram_group(raw: Mapping[str, object] | None) -> TelegramGroupConfig:
    if not raw:
        return TelegramGroupConfig()
    require_mention_raw = raw.get("require_mention")
    topics: dict[str, TelegramTopicConfig] = {}
    raw_topics = raw.get("topics")
    if isinstance(raw_topics, dict):
        for topic_id, topic_raw in raw_topics.items():
            if isinstance(topic_raw, dict):
                topics[str(topic_id)] = _decode_telegram_topic(topic_raw)
    return TelegramGroupConfig(
        enabled=bool(raw.get("enabled", True)),
        group_policy=(
            normalize_group_policy(str(raw.get("group_policy"))) if raw.get("group_policy") is not None else None
        ),
        require_mention=(bool(require_mention_raw) if require_mention_raw is not None else None),
        allow_users=parse_user_ids(raw.get("allow_users")),
        topics=topics,
    )


def _decode_telegram(raw: Mapping[str, object] | None) -> TelegramConfig | None:
    if not raw:
        return None
    cfg = TelegramConfig()
    if "enabled" in raw:
        cfg.enabled = bool(raw.get("enabled"))
    if "bot_token" in raw and raw.get("bot_token") is not None:
        cfg.bot_token = str(raw.get("bot_token"))
    cfg.allowed_users = parse_user_ids(raw.get("allowed_users"))
    cfg.admin_users = parse_user_ids(raw.get("admin_users"))
    if "webhook_mode" in raw:
        cfg.webhook_mode = bool(raw.get("webhook_mode"))
    if "webhook_url" in raw and raw.get("webhook_url") is not None:
        cfg.webhook_url = str(raw.get("webhook_url"))
    if "max_message_length" in raw and raw.get("max_message_length") is not None:
        cfg.max_message_length = int(str(raw.get("max_message_length")))
    if "rate_limit_messages" in raw and raw.get("rate_limit_messages") is not None:
        cfg.rate_limit_messages = int(str(raw.get("rate_limit_messages")))
    if "session_timeout" in raw and raw.get("session_timeout") is not None:
        cfg.session_timeout = int(str(raw.get("session_timeout")))
    if "dm_policy" in raw and raw.get("dm_policy") is not None:
        cfg.dm_policy = normalize_dm_policy(str(raw.get("dm_policy")))
    if "group_policy" in raw and raw.get("group_policy") is not None:
        cfg.group_policy = normalize_group_policy(str(raw.get("group_policy")))
    cfg.group_allow_users = parse_user_ids(raw.get("group_allow_users"))
    if "typing_indicator" in raw:
        cfg.typing_indicator = bool(raw.get("typing_indicator"))
    if "typing_interval_seconds" in raw and raw.get("typing_interval_seconds") is not None:
        cfg.typing_interval_seconds = max(1, int(str(raw.get("typing_interval_seconds"))))
    if "max_queue_size" in raw and raw.get("max_queue_size") is not None:
        cfg.max_queue_size = max(1, int(str(raw.get("max_queue_size"))))

    pairing = raw.get("pairing")
    cfg.pairing = _decode_telegram_pairing(pairing if isinstance(pairing, dict) else None)

    streaming = raw.get("streaming")
    cfg.streaming = _decode_telegram_streaming(streaming if isinstance(streaming, dict) else None)

    groups: dict[str, TelegramGroupConfig] = {}
    raw_groups = raw.get("groups")
    if isinstance(raw_groups, dict):
        for chat_id, group_raw in raw_groups.items():
            if isinstance(group_raw, dict):
                groups[str(chat_id)] = _decode_telegram_group(group_raw)
    cfg.groups = groups

    notifications = raw.get("notifications")
    cfg.notifications = _decode_telegram_notifications(notifications if isinstance(notifications, dict) else None)
    return cfg


def load_config() -> AnkaloopConfig:
    if CONFIG_FILE.exists():
        data: dict[str, Any] = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    else:
        data = _DEFAULT
    servers_data = data.get("servers", {})
    servers = {name: _decode_server(name, raw) for name, raw in servers_data.items() if isinstance(raw, dict)}
    chat_data = data.get("chat")
    chat = _decode_chat(chat_data) if isinstance(chat_data, dict) else None
    context_data = data.get("context")
    context = _decode_context(context_data) if isinstance(context_data, dict) else None
    server_data = data.get("server")
    server = _decode_server_config(server_data) if isinstance(server_data, dict) else None
    telegram_data = data.get("telegram")
    telegram = _decode_telegram(telegram_data) if isinstance(telegram_data, dict) else None
    telegram = apply_env_overrides(telegram)
    automation_data = data.get("automation")
    automation = _decode_automation(automation_data) if isinstance(automation_data, dict) else None
    return AnkaloopConfig(
        servers=servers,
        chat=chat,
        context=context,
        server=server,
        telegram=telegram,
        automation=automation,
    )


def _encode_server(s: Server) -> dict:
    out: dict = {}
    if s.command:
        out["command"] = s.command
    if s.args:
        out["args"] = list(s.args)
    if s.env:
        out["env"] = dict(s.env)
    if s.url:
        out["url"] = s.url
    if s.headers:
        out["headers"] = dict(s.headers)
    return out


def _encode_model_config(m: ModelConfig | None) -> dict | None:
    """Encode ModelConfig to TOML-compatible dict."""
    if m is None:
        return None
    out: dict = {}
    if m.provider_id:
        out["provider_id"] = m.provider_id
    if m.model_id:
        out["model_id"] = m.model_id
    if m.context_window is not None:
        out["context_window"] = m.context_window
    if m.output_limit is not None:
        out["output_limit"] = m.output_limit
    if m.is_custom:
        out["is_custom"] = True
    return out if out else None


def _encode_chat_provider(p: ChatProviderConfig) -> dict:
    """Encode a named chat provider profile to TOML-compatible dict."""
    out: dict = {}
    if p.base_url:
        out["base_url"] = p.base_url
    if p.model:
        out["model"] = p.model
    if p.api_key:
        out["api_key"] = p.api_key
    if p.api_type:
        out["api_type"] = p.api_type
    if p.request_timeout_seconds is not None:
        out["request_timeout_seconds"] = float(p.request_timeout_seconds)
    if p.max_retries is not None:
        out["max_retries"] = int(p.max_retries)
    if p.retry_base_delay_seconds is not None:
        out["retry_base_delay_seconds"] = float(p.retry_base_delay_seconds)
    model_config_dict = _encode_model_config(p.model_config)
    if model_config_dict:
        out["model_config"] = model_config_dict
    if p.extra_headers:
        out["extra_headers"] = dict(p.extra_headers)
    return out


def _encode_chat(c: ChatConfig | None) -> dict | None:
    if c is None:
        return None
    out: dict = {}
    provider_profiles_enabled = bool(c.providers)
    if c.base_url and not provider_profiles_enabled:
        out["base_url"] = c.base_url
    if c.model and not provider_profiles_enabled:
        out["model"] = c.model
    if c.api_key and not provider_profiles_enabled:
        out["api_key"] = c.api_key
    if c.api_type and not provider_profiles_enabled:
        out["api_type"] = c.api_type
    out["request_timeout_seconds"] = float(c.request_timeout_seconds)
    out["max_retries"] = int(c.max_retries)
    out["retry_base_delay_seconds"] = float(c.retry_base_delay_seconds)
    # Model config
    model_config_dict = _encode_model_config(c.model_config)
    if model_config_dict and not provider_profiles_enabled:
        out["model_config"] = model_config_dict
    if c.active_provider:
        out["active_provider"] = c.active_provider
    if c.providers:
        out["providers"] = {name: _encode_chat_provider(provider) for name, provider in c.providers.items()}
    if c.extra_headers:
        out["extra_headers"] = dict(c.extra_headers)
    if c.tool_loop_limit is not None:
        out["tool_loop_limit"] = int(c.tool_loop_limit)
    if c.bash_tool_limit is not None:
        out["bash_tool_limit"] = int(c.bash_tool_limit)
    if c.default_max_lines is not None:
        out["default_max_lines"] = int(c.default_max_lines)
    if c.read_roots is not None:
        out["read_roots"] = list(c.read_roots)
    if c.mcp_tools_enabled is not None:
        out["mcp_tools_enabled"] = bool(c.mcp_tools_enabled)
    if c.mcp_servers is not None:
        out["mcp_servers"] = list(c.mcp_servers)
    if c.write_tool_enabled is not None:
        out["write_tool_enabled"] = bool(c.write_tool_enabled)
    if c.edit_tool_enabled is not None:
        out["edit_tool_enabled"] = bool(c.edit_tool_enabled)
    out["sync_tool_settle_timeout_seconds"] = float(c.sync_tool_settle_timeout_seconds)
    # Agent settings
    if c.default_agent:
        out["default_agent"] = c.default_agent
    # Queue settings
    if c.enable_queue is not None:
        out["enable_queue"] = bool(c.enable_queue)
    if c.max_queue_size is not None:
        out["max_queue_size"] = int(c.max_queue_size)
    return out


def _encode_context(c: ContextConfig | None) -> dict | None:
    if c is None:
        return None

    return {
        "progressive_tools": bool(c.progressive_tools),
        "progressive_skills": bool(c.progressive_skills),
        "response_ratio": float(c.response_ratio),
        "min_prompt_budget": int(c.min_prompt_budget),
        "base_prompt_max_tokens": int(c.base_prompt_max_tokens),
        "tool_budget_ratio": float(c.tool_budget_ratio),
        "skill_budget_ratio": float(c.skill_budget_ratio),
        "memory_budget_ratio": float(c.memory_budget_ratio),
        "rules_budget_ratio": float(c.rules_budget_ratio),
        "tool_relevance_threshold": float(c.tool_relevance_threshold),
        "skill_relevance_threshold": float(c.skill_relevance_threshold),
        "tool_tiers": dict(c.tool_tiers),
    }


def _encode_auth(cfg: AuthConfig) -> dict:
    out: dict[str, Any] = {
        "enabled": bool(cfg.enabled),
    }
    if cfg.api_key:
        out["api_key"] = cfg.api_key
    return out


def _encode_cors(cfg: CORSConfig) -> dict:
    return {
        "enabled": bool(cfg.enabled),
        "allow_origins": list(cfg.allow_origins),
        "allow_methods": list(cfg.allow_methods),
        "allow_headers": list(cfg.allow_headers),
        "allow_credentials": bool(cfg.allow_credentials),
    }


def _encode_server_config(cfg: ServerConfig | None) -> dict | None:
    if cfg is None:
        return None
    out: dict[str, Any] = {
        "host": cfg.host,
        "port": int(cfg.port),
        "auth": _encode_auth(cfg.auth),
        "cors": _encode_cors(cfg.cors),
        "session_timeout_minutes": int(cfg.session_timeout_minutes),
        "max_sessions": int(cfg.max_sessions),
        "default_agent": cfg.default_agent,
    }
    if cfg.work_dir:
        out["work_dir"] = cfg.work_dir
    return out


def _encode_telegram_notifications(cfg: TelegramNotificationsConfig) -> dict:
    return {
        "ci_failures": bool(cfg.ci_failures),
        "pr_reviews": bool(cfg.pr_reviews),
        "task_completions": bool(cfg.task_completions),
        "error_alerts": bool(cfg.error_alerts),
    }


def _encode_telegram_pairing(cfg: TelegramPairingConfig) -> dict:
    return {
        "enabled": bool(cfg.enabled),
        "code_ttl_seconds": int(cfg.code_ttl_seconds),
        "max_pending": int(cfg.max_pending),
    }


def _encode_telegram_topic(cfg: TelegramTopicConfig) -> dict:
    out: dict[str, Any] = {
        "enabled": bool(cfg.enabled),
    }
    if cfg.group_policy:
        out["group_policy"] = cfg.group_policy
    if cfg.require_mention is not None:
        out["require_mention"] = bool(cfg.require_mention)
    if cfg.allow_users:
        out["allow_users"] = list(cfg.allow_users)
    return out


def _encode_telegram_group(cfg: TelegramGroupConfig) -> dict:
    out: dict[str, Any] = {
        "enabled": bool(cfg.enabled),
    }
    if cfg.group_policy:
        out["group_policy"] = cfg.group_policy
    if cfg.require_mention is not None:
        out["require_mention"] = bool(cfg.require_mention)
    if cfg.allow_users:
        out["allow_users"] = list(cfg.allow_users)
    if cfg.topics:
        out["topics"] = {topic_id: _encode_telegram_topic(topic_cfg) for topic_id, topic_cfg in cfg.topics.items()}
    return out


def _encode_telegram(cfg: TelegramConfig | None) -> dict | None:
    if cfg is None:
        return None
    out: dict[str, Any] = {
        "enabled": bool(cfg.enabled),
        "webhook_mode": bool(cfg.webhook_mode),
        "max_message_length": int(cfg.max_message_length),
        "rate_limit_messages": int(cfg.rate_limit_messages),
        "session_timeout": int(cfg.session_timeout),
        "dm_policy": normalize_dm_policy(cfg.dm_policy),
        "group_policy": normalize_group_policy(cfg.group_policy),
        "typing_indicator": bool(cfg.typing_indicator),
        "typing_interval_seconds": int(cfg.typing_interval_seconds),
        "max_queue_size": int(cfg.max_queue_size),
        "pairing": _encode_telegram_pairing(cfg.pairing),
    }
    if cfg.bot_token:
        out["bot_token"] = cfg.bot_token
    if cfg.allowed_users:
        out["allowed_users"] = list(cfg.allowed_users)
    if cfg.admin_users:
        out["admin_users"] = list(cfg.admin_users)
    if cfg.webhook_url:
        out["webhook_url"] = cfg.webhook_url
    if cfg.group_allow_users:
        out["group_allow_users"] = list(cfg.group_allow_users)
    if cfg.groups:
        out["groups"] = {chat_id: _encode_telegram_group(group_cfg) for chat_id, group_cfg in cfg.groups.items()}
    if cfg.notifications:
        out["notifications"] = _encode_telegram_notifications(cfg.notifications)
    return out if out else None


def save_config(cfg: AnkaloopConfig) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {"servers": {name: _encode_server(s) for name, s in cfg.servers.items()}}
    chat_obj = _encode_chat(cfg.chat)
    if chat_obj is not None:
        data["chat"] = chat_obj
    context_obj = _encode_context(cfg.context)
    if context_obj is not None:
        data["context"] = context_obj
    server_obj = _encode_server_config(cfg.server)
    if server_obj is not None:
        data["server"] = server_obj
    telegram_obj = _encode_telegram(cfg.telegram)
    if telegram_obj is not None:
        data["telegram"] = telegram_obj
    automation_obj = _encode_automation(cfg.automation)
    if automation_obj is not None:
        data["automation"] = automation_obj
    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(data, f)
    return CONFIG_FILE


def save_default_config() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "wb") as f:
            tomli_w.dump(_DEFAULT, f)
    return CONFIG_FILE


# ---------------------------------------------------------------------------
# Automation config decode / encode
# ---------------------------------------------------------------------------


def _decode_automation(raw: Mapping[str, object] | None) -> AutomationConfig | None:
    """Decode [automation] section from TOML."""
    if not raw:
        return None

    cfg = AutomationConfig(
        enabled=bool(raw.get("enabled", True)),
        default_timeout=int(str(raw.get("default_timeout", 300))),
    )

    jobs: list[AutomationJobConfig] = []
    jobs_raw = raw.get("jobs", [])
    for j in jobs_raw if isinstance(jobs_raw, list) else []:
        if not isinstance(j, dict):
            continue
        timeout = int(str(j.get("timeout", cfg.default_timeout)))
        jobs.append(
            AutomationJobConfig(
                name=str(j.get("name", "")),
                command=str(j.get("command", "")),
                skill=str(j["skill"]) if j.get("skill") else None,
                enabled=bool(j.get("enabled", True)),
                notify=bool(j.get("notify", True)),
                timeout=timeout,
                work_dir=str(j["work_dir"]) if j.get("work_dir") else None,
                tags=[str(t) for t in j.get("tags", [])],
                schedule=str(j["schedule"]) if j.get("schedule") else None,
            )
        )

    cfg.jobs = jobs
    return cfg


def _encode_automation(cfg: AutomationConfig | None) -> dict | None:
    """Encode AutomationConfig to TOML-compatible dict."""
    if cfg is None:
        return None

    out: dict[str, Any] = {
        "enabled": cfg.enabled,
        "default_timeout": cfg.default_timeout,
    }

    jobs: list[dict[str, Any]] = []
    for j in cfg.jobs:
        jd: dict[str, Any] = {
            "name": j.name,
            "command": j.command,
            "enabled": j.enabled,
            "notify": j.notify,
            "timeout": j.timeout,
        }
        if j.skill:
            jd["skill"] = j.skill
        if j.work_dir:
            jd["work_dir"] = j.work_dir
        if j.tags:
            jd["tags"] = j.tags
        if j.schedule:
            jd["schedule"] = j.schedule
        jobs.append(jd)

    if jobs:
        out["jobs"] = jobs

    return out
