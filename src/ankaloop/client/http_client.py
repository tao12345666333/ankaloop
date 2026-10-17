"""HTTP REST client for AnkaLoop.

Provides HTTP-based communication with AnkaLoop servers.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import BaseClient, ResponseChunk
from .exceptions import (
    ConnectionError,
    PromptError,
    ServerError,
    SessionNotFoundError,
    StreamTimeoutError,
    TimeoutError,
)


class HTTPClient(BaseClient):
    """HTTP REST client for AnkaLoop servers.

    Uses httpx for async HTTP requests with streaming support.

    Example:
        async with HTTPClient("http://localhost:8080") as client:
            session = await client.create_session()
            async for chunk in await client.prompt(session["id"], "Hello"):
                print(chunk.content)
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        stream_timeout: float | httpx.Timeout | None = 300.0,
        prompt_timeout: float | httpx.Timeout | None = 300.0,
        retry_attempts: int = 3,
        headers: dict[str, str] | None = None,
        api_key: str | None = None,
    ):
        """Initialize the HTTP client.

        Args:
            url: Server URL.
            timeout: Default timeout in seconds.
            stream_timeout: Timeout for streaming prompt requests: seconds, an
                ``httpx.Timeout`` for fine-grained control (e.g. an unbounded
                ``read`` timeout with bounded ``connect``/``write``), or
                ``None`` to wait indefinitely. This is the read timeout — the
                maximum gap between stream frames.
            prompt_timeout: Timeout for non-streaming prompt requests, bounding
                the entire wait until the turn completes. Accepts the same
                values as ``stream_timeout``.
            retry_attempts: Number of retry attempts for failed requests.
            headers: Optional custom headers.
            api_key: Optional API key sent as a Bearer token.
        """
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._stream_timeout = stream_timeout
        self._prompt_timeout = prompt_timeout
        self._retry_attempts = retry_attempts
        self._headers = dict(headers or {})
        if api_key is not None and not any(key.lower() == "authorization" for key in self._headers):
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        """Get the base API URL."""
        return f"{self._url}/api/v1"

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._client is not None

    async def connect(self) -> None:
        """Connect to the server.

        Raises:
            ConnectionError: If connection fails.
        """
        try:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers=self._headers,
            )
            # Verify connection with health check
            await self.health()
        except httpx.ConnectError as e:
            await self.close()
            raise ConnectionError(f"Failed to connect to {self._url}: {e}") from e
        except Exception as e:
            await self.close()
            raise ConnectionError(f"Connection failed: {e}") from e

    async def close(self) -> None:
        """Close the connection."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_connected(self) -> httpx.AsyncClient:
        """Ensure the client is connected.

        Returns:
            The httpx client.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._client:
            raise ConnectionError("Client is not connected")
        return self._client

    @staticmethod
    def _read_timeout_seconds(timeout: float | httpx.Timeout | None) -> float | None:
        """Return the read timeout of a configured prompt timeout, if known."""
        if isinstance(timeout, httpx.Timeout):
            return timeout.read
        return timeout

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_data: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request.

        Args:
            method: HTTP method.
            endpoint: API endpoint (without base URL).
            json_data: Optional JSON body.
            params: Optional query parameters.
            timeout: Optional custom timeout.

        Returns:
            Response JSON data.

        Raises:
            ConnectionError: If not connected.
            TimeoutError: If request times out.
            ServerError: If server returns an error.
        """
        client = self._ensure_connected()
        url = f"{self.base_url}{endpoint}"

        for attempt in range(self._retry_attempts):
            try:
                response = await client.request(
                    method,
                    url,
                    json=json_data,
                    params=params,
                    timeout=timeout or self._timeout,
                )

                if response.status_code == 404:
                    raise SessionNotFoundError(endpoint.split("/")[-1])

                if response.status_code >= 400:
                    error_data = response.json() if response.content else {}
                    raise ServerError(
                        message=error_data.get("error", f"HTTP {response.status_code}"),
                        status_code=response.status_code,
                        details=error_data,
                    )

                return dict(response.json())

            except httpx.TimeoutException as e:
                if attempt == self._retry_attempts - 1:
                    raise TimeoutError(
                        f"Request timed out after {timeout or self._timeout}s",
                        timeout=timeout or self._timeout,
                    ) from e
            except (SessionNotFoundError, ServerError):
                raise
            except httpx.ConnectError as e:
                if attempt == self._retry_attempts - 1:
                    raise ConnectionError(f"Connection failed: {e}") from e
            except Exception as e:
                if attempt == self._retry_attempts - 1:
                    raise ServerError(f"Request failed: {e}") from e

        raise ServerError("Max retries exceeded")

    # =========================================================================
    # Health & Info
    # =========================================================================

    async def health(self) -> dict[str, Any]:
        """Check server health.

        Returns:
            Health status dictionary.
        """
        return await self._request("GET", "/health")

    async def info(self) -> dict[str, Any]:
        """Get server information.

        Returns:
            Server info dictionary.
        """
        return await self._request("GET", "/info")

    # =========================================================================
    # Session Management
    # =========================================================================

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
            session_id: Optional requested session ID.

        Returns:
            Session data dictionary.
        """
        json_data = {}
        if cwd:
            json_data["cwd"] = cwd
        if agent_name:
            json_data["agent_name"] = agent_name
        if session_id:
            json_data["session_id"] = session_id

        return await self._request("POST", "/sessions", json_data=json_data)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Get session information.

        Args:
            session_id: The session ID.

        Returns:
            Session data dictionary.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        return await self._request("GET", f"/sessions/{session_id}")

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions.

        Returns:
            List of session data dictionaries.
        """
        response = await self._request("GET", "/sessions")
        return list(response.get("sessions", []))

    async def delete_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session ID.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        await self._request("DELETE", f"/sessions/{session_id}")

    # =========================================================================
    # Prompt Operations
    # =========================================================================

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
        """
        if stream:
            return self._stream_prompt(session_id, content, priority)
        else:
            return await self._non_stream_prompt(session_id, content, priority)

    async def _non_stream_prompt(
        self,
        session_id: str,
        content: str,
        priority: str,
    ) -> str:
        """Send a non-streaming prompt.

        Args:
            session_id: The session ID.
            content: The prompt content.
            priority: Message priority.

        Returns:
            Complete response string.
        """
        client = self._ensure_connected()
        url = f"{self.base_url}/sessions/{session_id}/prompt"

        try:
            response = await client.post(
                url,
                json={"content": content, "priority": priority, "stream": False},
                timeout=self._prompt_timeout,
            )

            if response.status_code == 404:
                raise SessionNotFoundError(session_id)

            if response.status_code >= 400:
                error_data = response.json() if response.content else {}
                raise PromptError(
                    message=error_data.get("error", f"HTTP {response.status_code}"),
                    session_id=session_id,
                    details=error_data,
                )

            result = response.json()
            return str(result.get("response", ""))

        except (SessionNotFoundError, PromptError):
            raise
        except httpx.TimeoutException as e:
            raise StreamTimeoutError(
                f"Prompt timed out ({type(e).__name__}); the turn may still be running server-side",
                session_id=session_id,
                timeout=self._read_timeout_seconds(self._prompt_timeout),
            ) from e
        except Exception as e:
            detail = str(e) or type(e).__name__
            raise PromptError(f"Prompt failed: {detail}", session_id=session_id) from e

    async def _stream_prompt(
        self,
        session_id: str,
        content: str,
        priority: str,
    ) -> AsyncIterator[ResponseChunk]:
        """Stream a prompt response.

        Args:
            session_id: The session ID.
            content: The prompt content.
            priority: Message priority.

        Yields:
            ResponseChunk objects.
        """
        client = self._ensure_connected()
        url = f"{self.base_url}/sessions/{session_id}/prompt/stream"

        try:
            async with client.stream(
                "POST",
                url,
                json={"content": content, "priority": priority, "stream": True},
                timeout=self._stream_timeout,
            ) as response:
                if response.status_code == 404:
                    raise SessionNotFoundError(session_id)

                if response.status_code >= 400:
                    # Read error body
                    await response.aread()
                    try:
                        error_data = response.json()
                    except Exception:
                        error_data = {}
                    raise PromptError(
                        message=error_data.get("error", f"HTTP {response.status_code}"),
                        session_id=session_id,
                    )

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                        chunk_type = data.get("type", "chunk")

                        if chunk_type == "chunk":
                            yield ResponseChunk(
                                content=data.get("content", ""),
                                chunk_type="text",
                                done=False,
                                metadata=data,
                            )
                        elif chunk_type == "tool":
                            tool_event = data.get("event", "")
                            yield ResponseChunk(
                                content=data.get("tool_name", ""),
                                chunk_type="tool_call" if tool_event == "call_start" else "tool_result",
                                done=False,
                                metadata=data,
                            )
                        elif chunk_type == "tool_call":
                            yield ResponseChunk(
                                content=data.get("tool_name", ""),
                                chunk_type="tool_call",
                                done=False,
                                metadata=data,
                            )
                        elif chunk_type == "tool_result":
                            yield ResponseChunk(
                                content=data.get("result", ""),
                                chunk_type="tool_result",
                                done=False,
                                metadata=data,
                            )
                        elif chunk_type == "error":
                            yield ResponseChunk(
                                content=data.get("error", "Unknown error"),
                                chunk_type="error",
                                done=True,
                                metadata=data,
                            )
                            return
                        elif chunk_type == "complete":
                            yield ResponseChunk(
                                content="",
                                chunk_type="complete",
                                done=True,
                                metadata=data,
                            )
                            return
                    except json.JSONDecodeError:
                        continue

        except (SessionNotFoundError, PromptError):
            raise
        except httpx.TimeoutException as e:
            raise StreamTimeoutError(
                f"Stream timed out ({type(e).__name__}); the turn may still be running server-side",
                session_id=session_id,
                timeout=self._read_timeout_seconds(self._stream_timeout),
            ) from e
        except Exception as e:
            detail = str(e) or type(e).__name__
            raise PromptError(f"Stream failed: {detail}", session_id=session_id) from e

    async def cancel(self, session_id: str, *, force: bool = False) -> None:
        """Cancel the current operation in a session.

        Args:
            session_id: The session ID.
            force: Whether to force cancellation.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        await self._request(
            "POST",
            f"/sessions/{session_id}/cancel",
            json_data={"force": force},
        )

    # =========================================================================
    # Tools & Agents
    # =========================================================================

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools.

        Returns:
            List of tool info dictionaries.
        """
        response = await self._request("GET", "/tools")
        return list(response.get("tools", []))

    async def list_agents(self) -> list[dict[str, Any]]:
        """List available agents.

        Returns:
            List of agent info dictionaries.
        """
        response = await self._request("GET", "/agents")
        return list(response.get("agents", []))
