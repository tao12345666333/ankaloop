"""Base client interface for AnkaLoop.

Defines the abstract interface that all client implementations must follow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any


class ClientMode(StrEnum):
    """Client mode enumeration."""

    REMOTE = "remote"  # Connect to remote server
    EMBEDDED = "embedded"  # Run agent locally


class ResponseChunk:
    """Represents a chunk of streaming response."""

    def __init__(
        self,
        content: str,
        *,
        chunk_type: str = "text",
        done: bool = False,
        metadata: dict[str, Any] | None = None,
    ):
        """Initialize the response chunk.

        Args:
            content: The text content of the chunk.
            chunk_type: Type of chunk (text, reasoning, tool_call, tool_result, error).
            done: Whether this is the final chunk.
            metadata: Optional metadata for the chunk.
        """
        self.content = content
        self.chunk_type = chunk_type
        self.done = done
        self.metadata = metadata or {}

    def __repr__(self) -> str:
        """Return string representation."""
        done_str = ", done=True" if self.done else ""
        return f"ResponseChunk({self.chunk_type!r}, {self.content[:50]!r}...{done_str})"


class BaseClient(ABC):
    """Abstract base class for AnkaLoop clients.

    All client implementations (HTTP, WebSocket, Embedded) must implement
    this interface.
    """

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the server or initialize the client.

        Raises:
            ConnectionError: If connection fails.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the connection and cleanup resources."""
        ...

    async def __aenter__(self) -> BaseClient:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    # =========================================================================
    # Health & Info
    # =========================================================================

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        """Check server health.

        Returns:
            Health status dictionary.

        Raises:
            ConnectionError: If not connected.
        """
        ...

    @abstractmethod
    async def info(self) -> dict[str, Any]:
        """Get server information.

        Returns:
            Server info dictionary.

        Raises:
            ConnectionError: If not connected.
        """
        ...

    # =========================================================================
    # Session Management
    # =========================================================================

    @abstractmethod
    async def create_session(
        self,
        *,
        cwd: str | None = None,
        agent_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new session.

        Args:
            cwd: Working directory for the session.
            agent_name: Agent to use for this session.
            session_id: Optional specific session ID.

        Returns:
            Session data dictionary.

        Raises:
            ConnectionError: If not connected.
            SessionError: If session creation fails.
        """
        ...

    @abstractmethod
    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Get session information.

        Args:
            session_id: The session ID.

        Returns:
            Session data dictionary.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        ...

    @abstractmethod
    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions.

        Returns:
            List of session data dictionaries.
        """
        ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session ID.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        ...

    # =========================================================================
    # Prompt Operations
    # =========================================================================

    @abstractmethod
    async def prompt(
        self,
        session_id: str,
        content: str,
        *,
        stream: bool = True,
        priority: str = "normal",
    ) -> str | AsyncIterator[ResponseChunk]:
        """Send a prompt to a session.

        Args:
            session_id: The session ID.
            content: The prompt content.
            stream: Whether to stream the response.
            priority: Message priority.

        Returns:
            If stream=False, returns the complete response string.
            If stream=True, returns an async iterator of ResponseChunks.

        Raises:
            SessionNotFoundError: If session doesn't exist.
            PromptError: If prompt fails.
        """
        ...

    @abstractmethod
    async def cancel(self, session_id: str, *, force: bool = False) -> None:
        """Cancel the current operation in a session.

        Args:
            session_id: The session ID.
            force: Whether to force cancellation.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        ...

    # =========================================================================
    # Tools & Agents
    # =========================================================================

    @abstractmethod
    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools.

        Returns:
            List of tool info dictionaries.
        """
        ...

    @abstractmethod
    async def list_agents(self) -> list[dict[str, Any]]:
        """List available agents.

        Returns:
            List of agent info dictionaries.
        """
        ...
