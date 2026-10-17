"""Tests for AnkaLoop Client SDK."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ankaloop.client import (
    AnkaloopClient,
    BaseClient,
    ClientMode,
    ClientSession,
    HTTPClient,
    WebSocketClient,
)
from ankaloop.client.base import ResponseChunk
from ankaloop.client.embedded import EmbeddedClient
from ankaloop.client.exceptions import (
    AnkaloopClientError,
    ConnectionError,
    PromptError,
    SessionError,
    SessionNotFoundError,
    StreamTimeoutError,
    TimeoutError,
)
from ankaloop.telegram.client import TelegramClient

# ============================================================================
# Test Exceptions
# ============================================================================


class TestExceptions:
    """Test exception classes."""

    def test_amcp_client_error(self):
        """Test base exception."""
        err = AnkaloopClientError("test error", code="TEST_CODE", details={"key": "value"})
        assert str(err) == "test error"
        assert err.message == "test error"
        assert err.code == "TEST_CODE"
        assert err.details == {"key": "value"}

    def test_connection_error(self):
        """Test connection error."""
        err = ConnectionError("failed to connect")
        assert "failed to connect" in str(err)
        assert err.code == "CONNECTION_ERROR"

    def test_session_not_found_error(self):
        """Test session not found error."""
        err = SessionNotFoundError("session-123")
        assert "session-123" in str(err)
        assert err.session_id == "session-123"
        assert err.code == "SESSION_NOT_FOUND"

    def test_timeout_error(self):
        """Test timeout error."""
        err = TimeoutError("timeout", timeout=30.0)
        assert err.timeout == 30.0
        assert err.details.get("timeout") == 30.0

    def test_prompt_error(self):
        """Test prompt error."""
        err = PromptError("failed", session_id="sess-1")
        assert err.session_id == "sess-1"
        assert err.code == "PROMPT_ERROR"

    def test_stream_timeout_error(self):
        """Test stream timeout error."""
        err = StreamTimeoutError("timed out", session_id="sess-1", timeout=300.0)
        assert isinstance(err, PromptError)
        assert err.code == "STREAM_TIMEOUT"
        assert err.session_id == "sess-1"
        assert err.timeout == 300.0
        assert err.details["timeout"] == 300.0


# ============================================================================
# Test ResponseChunk
# ============================================================================


class TestResponseChunk:
    """Test ResponseChunk class."""

    def test_basic_chunk(self):
        """Test basic chunk creation."""
        chunk = ResponseChunk(content="hello", chunk_type="text")
        assert chunk.content == "hello"
        assert chunk.chunk_type == "text"
        assert chunk.done is False
        assert chunk.metadata == {}

    def test_done_chunk(self):
        """Test done chunk."""
        chunk = ResponseChunk(content="", chunk_type="complete", done=True)
        assert chunk.done is True

    def test_chunk_with_metadata(self):
        """Test chunk with metadata."""
        meta = {"tool_name": "read_file", "call_id": "123"}
        chunk = ResponseChunk(content="result", chunk_type="tool_result", metadata=meta)
        assert chunk.metadata == meta

    def test_chunk_repr(self):
        """Test chunk representation."""
        chunk = ResponseChunk(content="hello world", done=True)
        repr_str = repr(chunk)
        assert "ResponseChunk" in repr_str
        assert "done=True" in repr_str


# ============================================================================
# Test AnkaloopClient
# ============================================================================


class TestAnkaloopClient:
    """Test AnkaloopClient class."""

    def test_remote_mode_creation(self):
        """Test creating client in remote mode."""
        client = AnkaloopClient.remote("http://localhost:8080")
        assert client.mode == ClientMode.REMOTE
        assert client.url == "http://localhost:8080"
        assert client.is_connected is False

    def test_embedded_mode_creation(self):
        """Test creating client in embedded mode."""
        client = AnkaloopClient.embedded()
        assert client.mode == ClientMode.EMBEDDED
        assert client.url is None

    def test_auto_mode_with_url(self):
        """Test auto mode with URL defaults to remote."""
        client = AnkaloopClient.auto("http://localhost:8080")
        assert client.mode == ClientMode.REMOTE

    def test_auto_mode_without_url(self):
        """Test auto mode without URL defaults to embedded."""
        client = AnkaloopClient.auto()
        assert client.mode == ClientMode.EMBEDDED

    def test_url_based_mode_detection(self):
        """Test that URL presence determines mode."""
        with_url = AnkaloopClient("http://localhost:8080")
        assert with_url.mode == ClientMode.REMOTE

        without_url = AnkaloopClient()
        assert without_url.mode == ClientMode.EMBEDDED

    def test_telegram_client_implements_base_client_contract(self):
        client = TelegramClient(AsyncMock())

        assert isinstance(client, BaseClient)


# ============================================================================
# Test HTTPClient
# ============================================================================


class TestHTTPClient:
    """Test HTTPClient class."""

    def test_client_creation(self):
        """Test client creation."""
        client = HTTPClient("http://localhost:8080")
        assert client._url == "http://localhost:8080"
        assert client.base_url == "http://localhost:8080/api/v1"
        assert client.is_connected is False

    def test_url_trailing_slash(self):
        """Test URL normalization."""
        client = HTTPClient("http://localhost:8080/")
        assert client._url == "http://localhost:8080"

    def test_ensure_connected_raises(self):
        """Test that ensure_connected raises when not connected."""
        client = HTTPClient("http://localhost:8080")
        with pytest.raises(ConnectionError):
            client._ensure_connected()

    def test_api_key_adds_bearer_header_without_overriding_headers(self):
        client = HTTPClient(
            "http://localhost:8080",
            headers={"X-Custom": "value"},
            api_key="secret",
        )
        assert client._headers == {
            "X-Custom": "value",
            "Authorization": "Bearer secret",
        }

        custom = HTTPClient(
            "http://localhost:8080",
            headers={"authorization": "Custom credential"},
            api_key="secret",
        )
        assert custom._headers["authorization"] == "Custom credential"


class _FakeStreamContext:
    """Mimic the async context manager returned by httpx.AsyncClient.stream."""

    def __init__(self, lines: list[str] | None = None, error: Exception | None = None):
        self._lines = lines or []
        self._error = error
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aiter_lines(self):
        if self._error is not None:
            raise self._error
        for line in self._lines:
            yield line


class TestHTTPClientStreaming:
    """Test HTTPClient prompt streaming timeouts and error fidelity."""

    def test_prompt_timeout_configuration(self):
        """stream_timeout and prompt_timeout accept seconds, httpx.Timeout, or None."""
        client = HTTPClient("http://localhost:8080")
        assert client._stream_timeout == 300.0
        assert client._prompt_timeout == 300.0

        unlimited = HTTPClient("http://localhost:8080", stream_timeout=None, prompt_timeout=None)
        assert unlimited._stream_timeout is None
        assert unlimited._prompt_timeout is None
        assert HTTPClient._read_timeout_seconds(None) is None

        custom = httpx.Timeout(connect=5.0, read=42.0, write=5.0, pool=5.0)
        assert HTTPClient._read_timeout_seconds(custom) == 42.0

    @pytest.mark.asyncio
    async def test_stream_prompt_passes_configured_timeout(self):
        """The streaming request uses stream_timeout instead of a hardcoded value."""
        client = HTTPClient("http://localhost:8080", stream_timeout=None)
        client._client = MagicMock()
        client._client.stream.return_value = _FakeStreamContext(
            ['{"type": "complete", "turn_id": "t1", "status": "completed"}']
        )

        chunks = [chunk async for chunk in await client.prompt("sess-1", "hi")]

        assert chunks[-1].done is True
        assert client._client.stream.call_args.kwargs["timeout"] is None

    @pytest.mark.asyncio
    async def test_stream_prompt_ignores_keepalive_ping_frames(self):
        """Server keepalive frames are skipped without ending the stream."""
        client = HTTPClient("http://localhost:8080")
        client._client = MagicMock()
        client._client.stream.return_value = _FakeStreamContext(
            [
                '{"type": "start", "turn_id": "t1", "session_id": "sess-1", "status": "running"}',
                '{"type": "ping", "turn_id": "t1"}',
                '{"type": "chunk", "turn_id": "t1", "content": "hello"}',
                '{"type": "ping", "turn_id": "t1"}',
                '{"type": "complete", "turn_id": "t1", "status": "completed"}',
            ]
        )

        chunks = [chunk async for chunk in await client.prompt("sess-1", "hi")]

        assert [chunk.chunk_type for chunk in chunks] == ["text", "complete"]
        assert chunks[0].content == "hello"

    @pytest.mark.asyncio
    async def test_stream_prompt_read_timeout_raises_stream_timeout_error(self):
        """httpx timeouts surface as StreamTimeoutError naming the exception type."""
        client = HTTPClient("http://localhost:8080", stream_timeout=300.0)
        client._client = MagicMock()
        client._client.stream.return_value = _FakeStreamContext(error=httpx.ReadTimeout(""))

        with pytest.raises(StreamTimeoutError) as exc_info:
            async for _ in await client.prompt("sess-1", "hi"):
                pass

        err = exc_info.value
        assert isinstance(err, PromptError)
        assert "ReadTimeout" in str(err)
        assert err.code == "STREAM_TIMEOUT"
        assert err.session_id == "sess-1"
        assert err.timeout == 300.0

    @pytest.mark.asyncio
    async def test_stream_prompt_error_names_exception_type_when_message_empty(self):
        """Exceptions with an empty str() still produce an identifiable message."""
        client = HTTPClient("http://localhost:8080")
        client._client = MagicMock()
        client._client.stream.return_value = _FakeStreamContext(error=httpx.ReadError(""))

        with pytest.raises(PromptError, match="ReadError"):
            async for _ in await client.prompt("sess-1", "hi"):
                pass

    @pytest.mark.asyncio
    async def test_non_stream_prompt_uses_prompt_timeout(self):
        """Non-streaming prompts use prompt_timeout, not stream_timeout."""
        client = HTTPClient("http://localhost:8080", stream_timeout=5.0, prompt_timeout=60.0)
        client._client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"response": "done"}
        client._client.post = AsyncMock(return_value=response)

        assert await client.prompt("sess-1", "hi", stream=False) == "done"
        assert client._client.post.call_args.kwargs["timeout"] == 60.0

    @pytest.mark.asyncio
    async def test_non_stream_prompt_timeout_raises_stream_timeout_error(self):
        """Non-streaming prompt timeouts surface as StreamTimeoutError."""
        client = HTTPClient("http://localhost:8080")
        client._client = MagicMock()
        client._client.post = AsyncMock(side_effect=httpx.ReadTimeout(""))

        with pytest.raises(StreamTimeoutError, match="ReadTimeout"):
            await client.prompt("sess-1", "hi", stream=False)


class TestWebSocketClient:
    """Test WebSocket authentication and URL construction."""

    def test_api_key_and_session_query(self):
        client = WebSocketClient(
            "https://example.test/",
            session_id="session with spaces",
            headers={"X-Custom": "value"},
            api_key="secret",
        )
        assert client.ws_url == "wss://example.test/ws?session_id=session+with+spaces"
        assert client._headers == {
            "X-Custom": "value",
            "Authorization": "Bearer secret",
        }

    @pytest.mark.asyncio
    async def test_concurrent_prompt_streams_use_independent_queues(self):
        client = WebSocketClient("https://example.test", session_id="session")
        sent = []

        async def capture_send(message):
            sent.append(message)

        client._send = AsyncMock(side_effect=capture_send)
        first_stream = client.prompt_stream("first")
        second_stream = client.prompt_stream("second")
        first_chunk_task = asyncio.create_task(anext(first_stream))
        second_chunk_task = asyncio.create_task(anext(second_stream))
        await asyncio.sleep(0)

        first_id, second_id = (message["id"] for message in sent)
        await client._dispatch_message(
            {
                "type": "response",
                "id": second_id,
                "payload": {"type": "chunk", "content": "second response"},
            }
        )
        await client._dispatch_message(
            {
                "type": "response",
                "id": first_id,
                "payload": {"type": "chunk", "content": "first response"},
            }
        )

        assert (await first_chunk_task).content == "first response"
        assert (await second_chunk_task).content == "second response"

        first_done_task = asyncio.create_task(anext(first_stream))
        second_done_task = asyncio.create_task(anext(second_stream))
        await client._dispatch_message({"type": "response", "id": first_id, "payload": {"type": "complete"}})
        await client._dispatch_message({"type": "response", "id": second_id, "payload": {"type": "complete"}})
        assert (await first_done_task).done is True
        assert (await second_done_task).done is True

    @pytest.mark.asyncio
    async def test_failed_prompt_send_cleans_up_stream_queue(self):
        client = WebSocketClient("https://example.test", session_id="session")
        client._send = AsyncMock(side_effect=ConnectionError("send failed"))

        with pytest.raises(ConnectionError, match="send failed"):
            await anext(client.prompt_stream("hello"))

        assert client._stream_queues == {}

    @pytest.mark.asyncio
    async def test_slow_prompt_stream_gets_error_without_blocking_dispatch(self):
        client = WebSocketClient("https://example.test", session_id="session")
        queue = asyncio.Queue(maxsize=1)
        client._stream_queues["request"] = queue

        await client._dispatch_message({"type": "response", "id": "request", "payload": {"type": "chunk"}})
        await client._dispatch_message({"type": "response", "id": "request", "payload": {"type": "chunk"}})

        error = await queue.get()
        assert error["payload"]["code"] == "SLOW_CONSUMER"
        assert "request" in client._overflowed_streams

    @pytest.mark.asyncio
    async def test_prompt_stream_timeout_raises_stream_timeout_error(self):
        """An idle stream raises StreamTimeoutError naming the idle limit."""
        client = WebSocketClient("https://example.test", session_id="session", stream_timeout=0.01)
        client._send = AsyncMock()

        with pytest.raises(StreamTimeoutError) as exc_info:
            await anext(client.prompt_stream("hello"))

        err = exc_info.value
        assert isinstance(err, PromptError)
        assert err.code == "STREAM_TIMEOUT"
        assert err.session_id == "session"
        assert err.timeout == 0.01
        assert "0.01" in str(err)
        assert client._stream_queues == {}

    @pytest.mark.asyncio
    async def test_prompt_stream_ignores_keepalive_ping_frames(self):
        """Server keepalive frames do not end or corrupt the stream."""
        client = WebSocketClient("https://example.test", session_id="session")
        sent = []

        async def capture_send(message):
            sent.append(message)

        client._send = AsyncMock(side_effect=capture_send)
        stream = client.prompt_stream("hello")
        chunk_task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        msg_id = sent[0]["id"]

        await client._dispatch_message({"type": "response", "id": msg_id, "payload": {"type": "ping"}})
        await client._dispatch_message(
            {"type": "response", "id": msg_id, "payload": {"type": "chunk", "content": "hello"}}
        )
        assert (await chunk_task).content == "hello"

        done_task = asyncio.create_task(anext(stream))
        await client._dispatch_message({"type": "response", "id": msg_id, "payload": {"type": "ping"}})
        await client._dispatch_message({"type": "response", "id": msg_id, "payload": {"type": "complete"}})
        assert (await done_task).done is True

    @pytest.mark.asyncio
    async def test_amcp_client_passes_auth_to_http_client(self, monkeypatch):
        async def connect_without_network(client):
            client._client = object()

        monkeypatch.setattr(HTTPClient, "connect", connect_without_network)
        client = AnkaloopClient.remote(
            "https://example.test",
            headers={"X-Custom": "value"},
            api_key="secret",
        )
        await client.connect()
        assert isinstance(client._client, HTTPClient)
        assert client._client._headers["Authorization"] == "Bearer secret"
        assert client._client._headers["X-Custom"] == "value"

    @pytest.mark.asyncio
    async def test_client_passes_prompt_timeouts_to_http_client(self, monkeypatch):
        async def connect_without_network(client):
            client._client = object()

        monkeypatch.setattr(HTTPClient, "connect", connect_without_network)
        client = AnkaloopClient.remote(
            "https://example.test",
            stream_timeout=None,
            prompt_timeout=12.0,
        )
        await client.connect()
        assert isinstance(client._client, HTTPClient)
        assert client._client._stream_timeout is None
        assert client._client._prompt_timeout == 12.0


# ============================================================================
# Test EmbeddedClient
# ============================================================================


class TestEmbeddedClient:
    """Test EmbeddedClient class."""

    @pytest.mark.asyncio
    async def test_connect_and_close(self):
        """Test connect and close."""
        client = EmbeddedClient()
        assert client.is_connected is False

        await client.connect()
        assert client.is_connected is True

        await client.close()
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_close_forgets_session_metrics(self):
        """Closing a reusable client releases projections for all removed sessions."""
        client = EmbeddedClient()
        await client.connect()
        await client.create_session(session_id="close-metrics")

        assert len(client.services.session_service._metrics) == 1
        await client.close()

        assert len(client.services.session_service._metrics) == 0

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager."""
        async with EmbeddedClient() as client:
            assert client.is_connected is True
        # After exit, would be closed

    @pytest.mark.asyncio
    async def test_health(self):
        """Test health check."""
        async with EmbeddedClient() as client:
            health = await client.health()
            assert health["healthy"] is True
            assert "version" in health
            assert health["mode"] == "embedded"

    @pytest.mark.asyncio
    async def test_info(self):
        """Test info endpoint."""
        async with EmbeddedClient() as client:
            info = await client.info()
            assert info["name"] == "ankaloop-embedded"
            assert info["mode"] == "embedded"
            assert "capabilities" in info

    @pytest.mark.asyncio
    async def test_create_session(self):
        """Test session creation."""
        async with EmbeddedClient() as client:
            session = await client.create_session()
            assert "id" in session
            assert "cwd" in session
            assert "agent_name" in session

    @pytest.mark.asyncio
    async def test_create_session_with_cwd(self):
        """Test session creation with working directory."""
        async with EmbeddedClient() as client:
            session = await client.create_session(cwd="/tmp")
            assert session["cwd"] == "/tmp"

    @pytest.mark.asyncio
    async def test_get_session(self):
        """Test getting session."""
        async with EmbeddedClient() as client:
            created = await client.create_session()
            fetched = await client.get_session(created["id"])
            assert fetched["id"] == created["id"]

    @pytest.mark.asyncio
    async def test_get_session_not_found(self):
        """Test getting non-existent session."""
        async with EmbeddedClient() as client:
            with pytest.raises(SessionNotFoundError):
                await client.get_session("non-existent")

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        """Test listing sessions."""
        async with EmbeddedClient() as client:
            # Start with empty
            sessions = await client.list_sessions()
            assert len(sessions) == 0

            # Create one
            await client.create_session()
            sessions = await client.list_sessions()
            assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_delete_session(self):
        """Test deleting session."""
        async with EmbeddedClient() as client:
            session = await client.create_session()
            await client.delete_session(session["id"])

            sessions = await client.list_sessions()
            assert len(sessions) == 0

    @pytest.mark.asyncio
    async def test_delete_session_not_found(self):
        """Test deleting non-existent session."""
        async with EmbeddedClient() as client:
            with pytest.raises(SessionNotFoundError):
                await client.delete_session("non-existent")

    @pytest.mark.asyncio
    async def test_list_tools(self):
        """Test listing tools."""
        async with EmbeddedClient() as client:
            tools = await client.list_tools()
            assert isinstance(tools, list)
            # Should have at least some default tools
            assert len(tools) > 0
            # Each tool should have a name
            for tool in tools:
                assert "name" in tool

    @pytest.mark.asyncio
    async def test_list_agents(self):
        """Test listing agents."""
        async with EmbeddedClient() as client:
            agents = await client.list_agents()
            assert isinstance(agents, list)
            # Should have at least some agents
            assert len(agents) > 0
            for agent in agents:
                assert "name" in agent


# ============================================================================
# Test ClientSession
# ============================================================================


class TestClientSession:
    """Test ClientSession class."""

    @pytest.mark.asyncio
    async def test_session_properties(self):
        """Test session properties."""
        async with EmbeddedClient() as client:
            session_data = await client.create_session()
            session = ClientSession(
                client=client,
                session_id=session_data["id"],
                cwd=session_data.get("cwd"),
                agent_name=session_data.get("agent_name"),
            )

            assert session.session_id == session_data["id"]
            assert session.id == session_data["id"]  # Alias
            assert session.cwd == session_data.get("cwd")

    @pytest.mark.asyncio
    async def test_session_info(self):
        """Test getting session info."""
        async with EmbeddedClient() as client:
            session_data = await client.create_session()
            session = ClientSession(
                client=client,
                session_id=session_data["id"],
            )

            info = await session.info()
            assert info["id"] == session_data["id"]

    @pytest.mark.asyncio
    async def test_session_delete(self):
        """Test session deletion."""
        async with EmbeddedClient() as client:
            session_data = await client.create_session()
            session = ClientSession(
                client=client,
                session_id=session_data["id"],
            )

            await session.delete()

            # Should be deleted
            with pytest.raises(SessionNotFoundError):
                await client.get_session(session_data["id"])

    def test_session_repr(self):
        """Test session representation."""
        # Mock client - we just need the repr
        session = ClientSession(
            client=None,  # type: ignore
            session_id="test-123",
            agent_name="coder",
        )
        repr_str = repr(session)
        assert "ClientSession" in repr_str
        assert "test-123" in repr_str
        assert "coder" in repr_str


# ============================================================================
# Integration-style tests (mocked)
# ============================================================================


class TestClientIntegration:
    """Integration-style tests with EmbeddedClient."""

    @pytest.mark.asyncio
    async def test_full_workflow_embedded(self):
        """Test full workflow with embedded client."""
        client = AnkaloopClient.embedded()

        async with client:
            # Check health
            # Note: For embedded, we'd need to adapt this
            # health = await client.health()
            # assert health["healthy"]

            # Create session
            session = await client.create_session(cwd="/tmp")
            assert session.session_id is not None

            # Get session info
            info = await session.info()
            assert info["id"] == session.session_id

            # List sessions
            sessions = await client.list_sessions()
            assert len(sessions) >= 1

            # Delete session
            await session.delete()

            # Verify deletion
            remaining = await client.list_sessions()
            assert all(s["id"] != session.session_id for s in remaining)
