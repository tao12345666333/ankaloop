"""Server configuration for AnkaLoop."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class CORSConfig(BaseModel):
    """CORS configuration."""

    enabled: bool = True
    allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:*",
            "http://127.0.0.1:*",
            "tauri://localhost",
        ]
    )
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])
    allow_credentials: bool = True


class AuthConfig(BaseModel):
    """Authentication configuration."""

    enabled: bool = False
    api_key: str | None = None


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is explicitly local without resolving DNS."""
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class ServerConfig(BaseModel):
    """AnkaLoop Server configuration."""

    host: str = "127.0.0.1"
    port: int = 8080
    cors: CORSConfig = Field(default_factory=CORSConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    # Session settings
    session_timeout_minutes: int = 60 * 24  # 24 hours
    max_sessions: int = 100

    # Streaming settings
    # Seconds a prompt stream may stay idle before a keepalive ping frame is
    # sent; None disables keepalive frames.
    stream_keepalive_seconds: float | None = 15.0

    # Working directory
    work_dir: Path | None = None

    # Agent settings
    default_agent: str = "default"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerConfig:
        """Create config from dictionary."""
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return self.model_dump()

    def validate_security(self) -> None:
        """Reject insecure or incomplete authentication configuration."""
        if self.auth.enabled and not (self.auth.api_key and self.auth.api_key.strip()):
            raise ValueError("server authentication is enabled but api_key is empty")
        if not is_loopback_host(self.host) and not self.auth.enabled:
            raise ValueError(f"refusing to bind to non-loopback host {self.host!r} without authentication")


# Global server config instance
_server_config: ServerConfig | None = None


def get_server_config() -> ServerConfig:
    """Get the global server configuration."""
    global _server_config
    if _server_config is None:
        _server_config = ServerConfig()
    return _server_config


def set_server_config(config: ServerConfig) -> None:
    """Set the global server configuration."""
    global _server_config
    _server_config = config


def reset_server_config() -> None:
    """Reset server configuration to default."""
    global _server_config
    _server_config = None
