"""AnkaLoop Client SDK.

Provides client interfaces for connecting to AnkaLoop servers remotely
or running agents in embedded mode locally.

Example usage:
    # Remote mode - connect to a server
    async with AnkaloopClient("http://localhost:8080") as client:
        session = await client.create_session(cwd="/my/project")
        async for chunk in session.prompt("Help me refactor this"):
            print(chunk.content, end="")

    # Embedded mode - run agent locally
    async with AnkaloopClient.embedded() as client:
        session = await client.create_session()
        response = await session.prompt("Hello", stream=False)
        print(response)

    # Auto mode - uses embedded if no server specified
    client = AnkaloopClient.auto()
"""

from __future__ import annotations

import httpx

from .base import BaseClient, ClientMode
from .exceptions import (
    AnkaloopClientError,
    ConnectionError,
    SessionError,
    SessionNotFoundError,
    StreamTimeoutError,
    TimeoutError,
)
from .http_client import HTTPClient
from .session import ClientSession
from .ws_client import WebSocketClient

__all__ = [
    # Main client class
    "AnkaloopClient",
    # Base classes
    "BaseClient",
    "ClientMode",
    # Session
    "ClientSession",
    # Specific clients
    "HTTPClient",
    "WebSocketClient",
    # Exceptions
    "AnkaloopClientError",
    "ConnectionError",
    "SessionError",
    "SessionNotFoundError",
    "StreamTimeoutError",
    "TimeoutError",
]


class AnkaloopClient:
    """Main AnkaLoop client class.

    Provides a unified interface for connecting to AnkaLoop servers
    or running agents locally.

    Examples:
        # Connect to remote server
        async with AnkaloopClient("http://localhost:8080") as client:
            session = await client.create_session()
            async for chunk in session.prompt("Hello"):
                print(chunk)

        # Use embedded mode (local agent)
        async with AnkaloopClient.embedded() as client:
            session = await client.create_session()
            response = await session.prompt("Hello", stream=False)

        # Auto-detect mode
        client = AnkaloopClient.auto()
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float = 30.0,
        stream_timeout: float | httpx.Timeout | None = 300.0,
        retry_attempts: int = 3,
        mode: ClientMode | None = None,
        headers: dict[str, str] | None = None,
        api_key: str | None = None,
    ):
        """Initialize the AnkaLoop client.

        Args:
            url: Server URL for remote mode. If None, uses embedded mode.
            timeout: Default timeout in seconds.
            stream_timeout: Timeout for prompt requests: seconds, an
                ``httpx.Timeout`` for fine-grained control, or ``None`` to
                wait indefinitely. For streaming prompts this bounds the gap
                between stream frames.
            retry_attempts: Number of retry attempts for failed requests.
            mode: Explicit mode selection. If None, auto-detects.
            headers: Optional custom remote request headers.
            api_key: Optional remote server API key.
        """
        self._url = url
        self._timeout = timeout
        self._stream_timeout = stream_timeout
        self._retry_attempts = retry_attempts
        self._mode = mode or (ClientMode.REMOTE if url else ClientMode.EMBEDDED)
        self._headers = headers
        self._api_key = api_key
        self._client: BaseClient | None = None
        self._ws_client: WebSocketClient | None = None

    @classmethod
    def remote(cls, url: str, **kwargs) -> AnkaloopClient:
        """Create a client in remote mode.

        Args:
            url: Server URL.
            **kwargs: Additional arguments for the client.

        Returns:
            AnkaloopClient configured for remote mode.
        """
        return cls(url=url, mode=ClientMode.REMOTE, **kwargs)

    @classmethod
    def embedded(cls, **kwargs) -> AnkaloopClient:
        """Create a client in embedded mode.

        Args:
            **kwargs: Additional arguments for the client.

        Returns:
            AnkaloopClient configured for embedded mode.
        """
        return cls(url=None, mode=ClientMode.EMBEDDED, **kwargs)

    @classmethod
    def auto(cls, url: str | None = None, **kwargs) -> AnkaloopClient:
        """Auto-detect client mode.

        Uses embedded mode if no URL is provided, otherwise remote.

        Args:
            url: Optional server URL.
            **kwargs: Additional arguments for the client.

        Returns:
            AnkaloopClient with auto-detected mode.
        """
        return cls(url=url, **kwargs)

    @property
    def mode(self) -> ClientMode:
        """Get the client mode."""
        return self._mode

    @property
    def url(self) -> str | None:
        """Get the server URL (None in embedded mode)."""
        return self._url

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        """Connect to the server or initialize embedded agent.

        Raises:
            ConnectionError: If connection fails.
        """
        if self._mode == ClientMode.REMOTE:
            if not self._url:
                raise ConnectionError("URL is required for remote mode")
            self._client = HTTPClient(
                url=self._url,
                timeout=self._timeout,
                stream_timeout=self._stream_timeout,
                retry_attempts=self._retry_attempts,
                headers=self._headers,
                api_key=self._api_key,
            )
        else:
            # Embedded mode - import here to avoid circular imports
            from .embedded import EmbeddedClient

            self._client = EmbeddedClient()

        await self._client.connect()

    async def close(self) -> None:
        """Close the connection."""
        if self._ws_client:
            await self._ws_client.close()
            self._ws_client = None
        if self._client:
            await self._client.close()
            self._client = None

    async def __aenter__(self) -> AnkaloopClient:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    # =========================================================================
    # Session Management
    # =========================================================================

    async def create_session(
        self,
        *,
        cwd: str | None = None,
        agent_name: str | None = None,
        session_id: str | None = None,
    ) -> ClientSession:
        """Create a new session.

        Args:
            cwd: Working directory for the session.
            agent_name: Agent to use for this session.
            session_id: Optional specific session ID.

        Returns:
            ClientSession for interacting with the session.

        Raises:
            ConnectionError: If not connected.
            SessionError: If session creation fails.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        session_data = await self._client.create_session(
            cwd=cwd,
            agent_name=agent_name,
            session_id=session_id,
        )

        return ClientSession(
            client=self._client,
            session_id=session_data["id"],
            cwd=session_data.get("cwd"),
            agent_name=session_data.get("agent_name"),
        )

    async def get_session(self, session_id: str) -> ClientSession:
        """Get an existing session.

        Args:
            session_id: The session ID.

        Returns:
            ClientSession for the existing session.

        Raises:
            ConnectionError: If not connected.
            SessionNotFoundError: If session doesn't exist.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        session_data = await self._client.get_session(session_id)

        return ClientSession(
            client=self._client,
            session_id=session_data["id"],
            cwd=session_data.get("cwd"),
            agent_name=session_data.get("agent_name"),
        )

    async def list_sessions(self) -> list[dict]:
        """List all sessions.

        Returns:
            List of session data dictionaries.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        return await self._client.list_sessions()

    async def delete_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session ID.

        Raises:
            ConnectionError: If not connected.
            SessionNotFoundError: If session doesn't exist.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        await self._client.delete_session(session_id)

    # =========================================================================
    # WebSocket Support
    # =========================================================================

    async def websocket_session(
        self,
        session_id: str,
    ) -> WebSocketClient:
        """Get a WebSocket client for a session.

        Args:
            session_id: The session ID.

        Returns:
            WebSocketClient connected to the session.

        Raises:
            ConnectionError: If not connected or mode is embedded.
        """
        if self._mode != ClientMode.REMOTE:
            raise ConnectionError("WebSocket is only available in remote mode")
        if not self._url:
            raise ConnectionError("URL is required for WebSocket")

        stream_timeout = self._stream_timeout
        if isinstance(stream_timeout, httpx.Timeout):
            stream_timeout = stream_timeout.read
        ws_client = WebSocketClient(
            url=self._url,
            session_id=session_id,
            stream_timeout=stream_timeout,
            headers=self._headers,
            api_key=self._api_key,
        )
        await ws_client.connect()
        return ws_client

    # =========================================================================
    # Server Info
    # =========================================================================

    async def health(self) -> dict:
        """Check server health.

        Returns:
            Health status dictionary.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        return await self._client.health()

    async def info(self) -> dict:
        """Get server information.

        Returns:
            Server info dictionary.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        return await self._client.info()

    async def list_tools(self) -> list[dict]:
        """List available tools.

        Returns:
            List of tool info dictionaries.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        return await self._client.list_tools()

    async def list_agents(self) -> list[dict]:
        """List available agents.

        Returns:
            List of agent info dictionaries.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")

        return await self._client.list_agents()
