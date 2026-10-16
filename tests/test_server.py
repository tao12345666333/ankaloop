"""Tests for AnkaLoop Server module."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ankaloop.server import ServerConfig, create_app
from ankaloop.server.config import AuthConfig, get_server_config, is_loopback_host
from ankaloop.server.models import SessionStatus
from ankaloop.server.session_manager import (
    ManagedSession,
    MaxSessionsReachedError,
    SessionAlreadyExistsError,
    SessionManager,
    SessionNotFoundError,
    get_session_manager,
)
from ankaloop.session_store import SessionTimelineStore


@pytest.fixture
def server_config():
    """Create a test server configuration."""
    return ServerConfig(
        host="127.0.0.1",
        port=8080,
        max_sessions=5,
    )


@pytest.fixture
def app(server_config):
    """Create a test FastAPI application."""
    return create_app(server_config)


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def session_manager(server_config):
    """Create a fresh session manager for testing."""
    return SessionManager(server_config)


class TestHealthEndpoints:
    """Test health and info endpoints."""

    def test_root_endpoint(self, client):
        """Test root endpoint returns API info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "ankaloop-server"
        assert "version" in data
        assert data["api"] == "/api/v1"

    def test_health_endpoint(self, client):
        """Test health endpoint returns healthy status."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["healthy"] is True
        assert "version" in data
        assert "uptime_seconds" in data

    def test_info_endpoint(self, client):
        """Test info endpoint returns server info."""
        response = client.get("/api/v1/info")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "ankaloop-server"
        assert "capabilities" in data
        assert "sessions" in data["capabilities"]


class TestServerAuthentication:
    """Test unified HTTP and WebSocket API-key authentication."""

    @pytest.fixture
    def authenticated_client(self):
        config = ServerConfig(auth=AuthConfig(enabled=True, api_key="test-secret"))
        return TestClient(create_app(config))

    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"}],
    )
    def test_http_rejects_missing_or_wrong_key(self, authenticated_client, headers):
        response = authenticated_client.get("/api/v1/info", headers=headers)
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Bearer test-secret"},
            {"X-API-Key": "test-secret"},
        ],
    )
    def test_http_accepts_supported_key_headers(self, authenticated_client, headers):
        response = authenticated_client.get("/api/v1/info", headers=headers)
        assert response.status_code == 200

    def test_health_and_root_are_public(self, authenticated_client):
        assert authenticated_client.get("/api/v1/health").status_code == 200
        assert authenticated_client.get("/").status_code == 200

    @pytest.mark.parametrize(
        ("url", "headers"),
        [
            ("/ws?api_key=test-secret", {}),
            ("/ws?api_key=wrong", {}),
            ("/ws", {"Authorization": "Bearer wrong"}),
        ],
    )
    def test_websocket_rejects_missing_or_wrong_key(self, authenticated_client, url, headers):
        with pytest.raises(WebSocketDisconnect), authenticated_client.websocket_connect(url, headers=headers):
            pass

    @pytest.mark.parametrize(
        ("url", "headers"),
        [
            ("/ws", {"Authorization": "Bearer test-secret"}),
            ("/ws", {"X-API-Key": "test-secret"}),
        ],
    )
    def test_websocket_accepts_supported_headers(self, authenticated_client, url, headers):
        with authenticated_client.websocket_connect(url, headers=headers) as websocket:
            message = websocket.receive_json()
            assert message["payload"]["kind"] == "connected"

    def test_websocket_auth_is_scoped_to_its_app(self):
        authenticated_app = create_app(ServerConfig(auth=AuthConfig(enabled=True, api_key="app-secret")))
        create_app(ServerConfig())

        with (
            TestClient(authenticated_app) as client,
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws"),
        ):
            pass

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.internal"])
    def test_non_loopback_without_auth_is_rejected(self, host):
        with pytest.raises(ValueError, match="non-loopback"):
            create_app(ServerConfig(host=host))

    @pytest.mark.parametrize("host", ["localhost", "localhost.", "127.0.0.1", "::1"])
    def test_loopback_hosts_are_recognized(self, host):
        assert is_loopback_host(host)

    @pytest.mark.parametrize("api_key", [None, "", "   "])
    def test_enabled_auth_requires_nonempty_key(self, api_key):
        with pytest.raises(ValueError, match="api_key is empty"):
            create_app(ServerConfig(auth=AuthConfig(enabled=True, api_key=api_key)))


class TestSessionEndpoints:
    """Test session management endpoints."""

    def test_create_session(self, client):
        """Test creating a new session."""
        response = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["cwd"] == "/tmp"
        assert data["status"] == "idle"

    def test_list_sessions(self, client):
        """Test listing sessions."""
        # Create a session first
        client.post("/api/v1/sessions", json={"cwd": "/tmp"})

        response = client.get("/api/v1/sessions")
        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data
        assert "total" in data
        assert data["total"] >= 1

    def test_get_session(self, client):
        """Test getting a specific session."""
        # Create a session
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]

        # Get the session
        response = client.get(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == session_id

    def test_get_nonexistent_session(self, client):
        """Test getting a nonexistent session returns 404."""
        response = client.get("/api/v1/sessions/nonexistent-id")
        assert response.status_code == 404

    def test_get_durable_sanitized_timeline(self, client, tmp_path):
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))
        managed.agent._timeline_store = SessionTimelineStore(tmp_path, session_id)
        managed.agent._emit_event(
            "tool.call_start",
            {
                "tool_name": "bash",
                "tool_id": "call-1",
                "arguments": {"command": "echo secret"},
            },
        )

        response = client.get(f"/api/v1/sessions/{session_id}/timeline")

        assert response.status_code == 200
        events = response.json()["events"]
        assert events[-1]["type"] == "tool.call_start"
        assert events[-1]["data"] == {"tool_name": "bash", "tool_id": "call-1"}
        assert "secret" not in managed.agent._timeline_store.path.read_text(encoding="utf-8")

    def test_delete_session(self, client):
        """Test deleting a session."""
        # Create a session
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))
        managed.agent._emit_event("turn.completed", {"turn_id": "turn-delete"})
        timeline_path = managed.agent._timeline_store.path
        managed.agent._save_conversation_history()
        snapshot_path = managed.agent.session_file
        assert timeline_path.exists()
        assert snapshot_path.exists()

        # Delete the session
        response = client.delete(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 200

        # Verify it's deleted
        get_resp = client.get(f"/api/v1/sessions/{session_id}")
        assert get_resp.status_code == 404
        assert not timeline_path.exists()
        assert not snapshot_path.exists()

    def test_prompt_stream_new_session_command(self, client):
        """Test /new through the server prompt stream creates a session."""
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]

        with client.stream(
            "POST",
            f"/api/v1/sessions/{session_id}/prompt/stream",
            json={"content": "/new", "stream": True},
        ) as response:
            assert response.status_code == 200
            events = [json.loads(line) for line in response.iter_lines() if line]

        created = next(event for event in events if event["type"] == "session_created")
        assert created["previous_session_id"] == session_id
        assert created["session_id"] != session_id
        assert any(event["type"] == "complete" for event in events)

    def test_prompt_stream_relays_incremental_turn_frames(self, client):
        """HTTP and runtime share request-scoped incremental frames."""
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))

        async def process(_request):
            managed.agent._emit_event("message.chunk", {"content": "first"})
            await asyncio.sleep(0)
            managed.agent._emit_event("message.chunk", {"content": " second"})
            return "first second"

        managed.agent._runtime._processor = process
        with client.stream(
            "POST",
            f"/api/v1/sessions/{session_id}/prompt/stream",
            json={"content": "hello", "stream": True},
        ) as response:
            frames = [json.loads(line) for line in response.iter_lines() if line]

        assert [frame["type"] for frame in frames] == ["start", "chunk", "chunk", "complete"]
        assert [frame.get("content") for frame in frames[1:3]] == ["first", " second"]
        assert len({frame["turn_id"] for frame in frames}) == 1

        turn_id = frames[0]["turn_id"]
        turn_response = client.get(f"/api/v1/sessions/{session_id}/turns/{turn_id}")
        assert turn_response.json()["status"] == "completed"

    def test_prompt_stream_emits_keepalive_ping_for_idle_turn(self, client, monkeypatch):
        """Idle turns emit ping frames so consumers can tell working from dead."""
        monkeypatch.setattr(get_server_config(), "stream_keepalive_seconds", 0.05)
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))

        async def process(_request):
            await asyncio.sleep(0.2)
            return "slow done"

        managed.agent._runtime._processor = process
        with client.stream(
            "POST",
            f"/api/v1/sessions/{session_id}/prompt/stream",
            json={"content": "hello", "stream": True},
        ) as response:
            frames = [json.loads(line) for line in response.iter_lines() if line]

        types = [frame["type"] for frame in frames]
        assert types[0] == "start"
        assert types[-1] == "complete"
        assert "ping" in types
        assert set(types) <= {"start", "ping", "chunk", "complete"}

    def test_prompt_new_session_command_non_stream(self, client):
        """Test /new through the non-stream endpoint returns new session metadata."""
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]

        response = client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"content": "/new", "stream": False},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "handled"
        assert data["new_session_id"]
        assert data["new_session_id"] != session_id

    def test_prompt_non_stream_executes_agent_and_returns_response(self, client):
        """Test non-stream prompt endpoint runs the agent and returns its response."""
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))
        managed.agent._runtime._processor = AsyncMock(return_value="server response")

        response = client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"content": "hello", "stream": False},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert data["response"] == "server response"
        managed.agent._runtime._processor.assert_awaited_once()
        request = managed.agent._runtime._processor.await_args.args[0]
        assert request.prompt == "hello"
        assert request.stream is False

    def test_prompt_non_stream_busy_queue_actually_enqueues(self, client):
        """Test busy non-stream prompt endpoint enqueues the prompt before returning queued."""
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))
        handle = MagicMock(id="turn-queued")
        managed.agent.is_busy = MagicMock(return_value=True)
        managed.agent.queued_count = MagicMock(return_value=0)
        managed.agent._submit_turn = AsyncMock(return_value=handle)

        response = client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"content": "queued prompt", "stream": False, "conflict_strategy": "queue"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["position"] == 1
        assert data["message_id"] == "turn-queued"
        managed.agent._submit_turn.assert_awaited_once()


class TestToolEndpoints:
    """Test tool management endpoints."""

    def test_list_tools(self, client):
        """Test listing available tools."""
        response = client.get("/api/v1/tools")
        assert response.status_code == 200
        data = response.json()
        assert "tools" in data
        assert "total" in data
        assert len(data["tools"]) > 0

    def test_get_tool(self, client):
        """Test getting a specific tool."""
        response = client.get("/api/v1/tools/read_file")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "read_file"

    def test_get_nonexistent_tool(self, client):
        """Test getting a nonexistent tool returns 404."""
        response = client.get("/api/v1/tools/nonexistent_tool")
        assert response.status_code == 404

    def test_execute_tool_endpoint_removed(self, client):
        """Test that the direct execute endpoint is no longer available."""
        response = client.post("/api/v1/tools/read_file/execute", json={"arguments": {}})
        assert response.status_code == 404


class TestAgentEndpoints:
    """Test agent management endpoints."""

    def test_list_agents(self, client):
        """Test listing available agents."""
        response = client.get("/api/v1/agents")
        assert response.status_code == 200
        data = response.json()
        assert "agents" in data
        assert "total" in data
        assert len(data["agents"]) > 0

    def test_get_agent(self, client):
        """Test getting a specific agent."""
        response = client.get("/api/v1/agents/coder")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "coder"
        assert data["mode"] == "primary"

    def test_get_nonexistent_agent(self, client):
        """Test getting a nonexistent agent returns 404."""
        response = client.get("/api/v1/agents/nonexistent_agent")
        assert response.status_code == 404


class TestSessionManager:
    """Test session manager functionality."""

    @pytest.mark.asyncio
    async def test_create_session(self, session_manager):
        """Test creating a session via manager."""
        session = await session_manager.create_session(cwd="/tmp")
        assert session.id.startswith("session-")
        assert session.cwd == "/tmp"
        assert session_manager.session_count == 1

    @pytest.mark.asyncio
    async def test_get_session(self, session_manager):
        """Test getting a session by ID."""
        created = await session_manager.create_session(cwd="/tmp")
        retrieved = await session_manager.get_session(created.id)
        assert retrieved.id == created.id

    @pytest.mark.asyncio
    async def test_duplicate_live_session_owner_is_rejected(self, session_manager):
        first = await session_manager.create_session(cwd="/tmp", session_id="shared")

        with pytest.raises(SessionAlreadyExistsError, match="already exists"):
            await session_manager.create_session(cwd="/var", session_id="shared")

        assert await session_manager.get_session("shared") is first

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self, session_manager):
        """Test getting a nonexistent session raises error."""
        with pytest.raises(SessionNotFoundError):
            await session_manager.get_session("nonexistent-id")

    @pytest.mark.asyncio
    async def test_delete_session(self, session_manager):
        """Test deleting a session."""
        session = await session_manager.create_session(cwd="/tmp")
        await session_manager.delete_session(session.id)
        assert session_manager.session_count == 0

    @pytest.mark.asyncio
    async def test_max_sessions_limit(self, session_manager):
        """Test max sessions limit is enforced."""
        # Create max sessions
        for _ in range(5):
            await session_manager.create_session(cwd="/tmp")

        # Try to create one more
        with pytest.raises(MaxSessionsReachedError):
            await session_manager.create_session(cwd="/tmp")

    @pytest.mark.asyncio
    async def test_list_sessions(self, session_manager):
        """Test listing all sessions."""
        await session_manager.create_session(cwd="/tmp")
        await session_manager.create_session(cwd="/var")

        sessions = await session_manager.list_sessions()
        assert len(sessions) == 2


class TestConnectionStatus:
    """Test connection status endpoints (Phase 2)."""

    def test_status_endpoint_includes_connections(self, client):
        """Test status endpoint includes connection info."""
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        data = response.json()
        assert "connections" in data
        assert "total_connections" in data["connections"]

    def test_connections_endpoint(self, client):
        """Test dedicated connections endpoint."""
        response = client.get("/api/v1/connections")
        assert response.status_code == 200
        data = response.json()
        assert "total_connections" in data
        assert "global_connections" in data
        assert "session_connections" in data


class TestEventBridge:
    """Test event bridge functionality (Phase 2)."""

    @pytest.mark.asyncio
    async def test_event_bridge_creation(self):
        """Test event bridge can be created."""
        from ankaloop.server.event_bridge import EventBridge, get_event_bridge

        bridge = get_event_bridge()
        assert bridge is not None
        assert isinstance(bridge, EventBridge)

    @pytest.mark.asyncio
    async def test_emit_tool_event(self):
        """Test emitting tool events."""
        from ankaloop.server.event_bridge import emit_tool_event

        # Should not raise
        await emit_tool_event(
            session_id="test-session",
            event_type="start",
            tool_name="test_tool",
            arguments={"arg": "value"},
        )

        await emit_tool_event(
            session_id="test-session",
            event_type="complete",
            tool_name="test_tool",
            result="success",
            duration_ms=100.0,
        )

        await emit_tool_event(
            session_id="test-session",
            event_type="error",
            tool_name="test_tool",
            error="Something went wrong",
        )


class TestConnectionManager:
    """Test WebSocket connection manager (Phase 2)."""

    def test_connection_stats_initial(self):
        """Test initial connection stats are empty."""
        from ankaloop.server.websocket import ConnectionManager

        manager = ConnectionManager()
        stats = manager.get_connection_stats()

        assert stats["global_connections"] == 0
        assert stats["total_connections"] == 0
        assert stats["total_sessions_with_clients"] == 0
        assert stats["session_connections"] == {}

    def test_session_connection_count(self):
        """Test getting connection count for specific session."""
        from ankaloop.server.websocket import ConnectionManager

        manager = ConnectionManager()
        count = manager.get_session_connection_count("nonexistent-session")
        assert count == 0

    @pytest.mark.asyncio
    async def test_writes_to_one_connection_are_serialized(self):
        from ankaloop.server.websocket import ConnectionManager

        class FakeWebSocket:
            def __init__(self):
                self.sending = False
                self.messages = []
                self.closed = False

            async def accept(self):
                pass

            async def close(self, code=1000, reason=None):
                self.closed = True

            async def send_json(self, message):
                assert self.sending is False
                self.sending = True
                await asyncio.sleep(0)
                self.messages.append(message)
                self.sending = False

        manager = ConnectionManager()
        websocket = FakeWebSocket()
        await manager.connect(websocket)

        await asyncio.gather(
            manager.send(websocket, {"type": "response", "id": "one"}),
            manager.send(websocket, {"type": "response", "id": "two"}),
        )

        assert [message.get("id") for message in websocket.messages] == [None, "one", "two"]
        await manager.disconnect(websocket)
        assert websocket.closed is True

    @pytest.mark.asyncio
    async def test_slow_connection_closes_when_outbound_queue_is_full(self):
        from ankaloop.server.websocket import ConnectionManager

        class SlowWebSocket:
            def __init__(self):
                self.block = asyncio.Event()
                self.closed_code = None

            async def accept(self):
                pass

            async def close(self, code=1000, reason=None):
                self.closed_code = code
                self.block.set()

            async def send_json(self, _message):
                await self.block.wait()

        manager = ConnectionManager()
        websocket = SlowWebSocket()
        websocket.block.set()
        await manager.connect(websocket)
        websocket.block.clear()

        sends = [
            asyncio.create_task(manager.send(websocket, {"type": "event", "id": str(index)})) for index in range(66)
        ]
        await asyncio.gather(*sends, return_exceptions=True)

        state = manager._states[websocket]
        assert state.queue.qsize() <= state.queue.maxsize
        assert state.closed is True
        assert websocket.closed_code == 1013
        await manager.disconnect(websocket)


class TestCollaborationEvents:
    """Test real-time collaboration events (Phase 2 enhancement)."""

    @pytest.mark.asyncio
    async def test_emit_prompt_received(self):
        """Test emitting prompt received event."""
        from ankaloop.server.event_bridge import get_event_bridge

        bridge = get_event_bridge()

        # Should not raise
        await bridge.emit_prompt_received(
            session_id="test-session",
            content="Test prompt",
            sender_client_id="client-123",
            priority="normal",
        )

    @pytest.mark.asyncio
    async def test_emit_prompt_received_truncates_long_content(self):
        """Test that long content is truncated in prompt received event."""
        from ankaloop.server.event_bridge import get_event_bridge

        bridge = get_event_bridge()

        long_content = "a" * 200
        # Should not raise and should truncate internally
        await bridge.emit_prompt_received(
            session_id="test-session",
            content=long_content,
        )

    @pytest.mark.asyncio
    async def test_emit_prompt_started(self):
        """Test emitting prompt started event."""
        from ankaloop.server.event_bridge import get_event_bridge

        bridge = get_event_bridge()

        await bridge.emit_prompt_started(
            session_id="test-session",
            message_id="msg-123",
        )

    @pytest.mark.asyncio
    async def test_emit_prompt_queued(self):
        """Test emitting prompt queued event."""
        from ankaloop.server.event_bridge import get_event_bridge

        bridge = get_event_bridge()

        await bridge.emit_prompt_queued(
            session_id="test-session",
            message_id="msg-123",
            position=2,
        )

    @pytest.mark.asyncio
    async def test_emit_prompt_rejected(self):
        """Test emitting prompt rejected event."""
        from ankaloop.server.event_bridge import get_event_bridge

        bridge = get_event_bridge()

        await bridge.emit_prompt_rejected(
            session_id="test-session",
            reason="Session is busy",
            conflict_strategy="reject",
        )


class TestConflictStrategy:
    """Test conflict handling strategies for concurrent prompts (Phase 2 enhancement)."""

    def test_conflict_strategy_enum(self):
        """Test ConflictStrategy enum values."""
        from ankaloop.server.models import ConflictStrategy

        assert ConflictStrategy.QUEUE.value == "queue"
        assert ConflictStrategy.REJECT.value == "reject"

    def test_prompt_request_has_conflict_strategy(self):
        """Test PromptRequest includes conflict_strategy field."""
        from ankaloop.server.models import ConflictStrategy, PromptRequest

        # Default is QUEUE
        req = PromptRequest(content="test")
        assert req.conflict_strategy == ConflictStrategy.QUEUE

        # Can specify REJECT
        req_reject = PromptRequest(content="test", conflict_strategy=ConflictStrategy.REJECT)
        assert req_reject.conflict_strategy == ConflictStrategy.REJECT

    def test_new_event_types_exist(self):
        """Test that new collaboration event types exist."""
        from ankaloop.server.models import EventType

        # Verify new event types are available
        assert EventType.PROMPT_RECEIVED.value == "prompt.received"
        assert EventType.PROMPT_STARTED.value == "prompt.started"
        assert EventType.PROMPT_QUEUED.value == "prompt.queued"
        assert EventType.PROMPT_REJECTED.value == "prompt.rejected"

    def test_prompt_with_queue_strategy(self, client):
        """Test prompt with queue strategy (default behavior)."""
        # Create a session
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))
        managed.agent._runtime._processor = AsyncMock(return_value="queued strategy response")

        # Send prompt with queue strategy (default)
        response = client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"content": "test prompt", "conflict_strategy": "queue"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == session_id
        assert data["status"] == "complete"
        assert data["response"] == "queued strategy response"

    def test_prompt_with_reject_strategy_on_idle_session(self, client):
        """Test prompt with reject strategy on idle session succeeds."""
        # Create a session
        create_resp = client.post("/api/v1/sessions", json={"cwd": "/tmp"})
        session_id = create_resp.json()["id"]
        managed = asyncio.run(get_session_manager().get_session(session_id))
        managed.agent._runtime._processor = AsyncMock(return_value="reject strategy response")

        # Send prompt with reject strategy - should succeed since session is idle
        response = client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"content": "test prompt", "conflict_strategy": "reject"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert data["response"] == "reject strategy response"


class TestServeEnvVars:
    """Test serve command env-var and flag handling for host/port/api-key."""

    def test_env_vars_recognized_in_help(self):
        """Typer exposes ANKA_HOST/ANKA_PORT/ANKA_API_KEY as envvar options."""
        from typer.testing import CliRunner

        from ankaloop.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["serve", "--help"])
        assert result.exit_code == 0
        assert "ANKA_HOST" in result.output
        assert "ANKA_PORT" in result.output
        assert "ANKA_API_KEY" in result.output

    def test_api_key_flag_enables_auth(self):
        """--api-key CLI flag builds ServerConfig with auth enabled."""
        from ankaloop.cli import serve_command
        from ankaloop.server.config import AuthConfig

        original_run = None

        captured: dict = {}

        def fake_run_server(**kwargs):
            captured.update(kwargs)

        import ankaloop.server

        original_run = ankaloop.server.run_server
        ankaloop.server.run_server = fake_run_server
        try:
            serve_command(
                host="0.0.0.0",
                port=8080,
                api_key="flag-secret",
                work_dir=None,
                reload=False,
                telegram_enabled=False,
            )
        finally:
            ankaloop.server.run_server = original_run

        auth = captured.get("auth")
        assert auth is not None
        assert auth.enabled is True
        assert auth.api_key == "flag-secret"
