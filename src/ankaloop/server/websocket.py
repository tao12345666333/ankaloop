"""WebSocket handler for AnkaLoop Server.

Provides real-time bidirectional communication for:
- Streaming responses
- Tool execution events
- Session status updates
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..runtime import ErrorEnvelope
from ..tracing import attach_carrier, carrier_from_headers
from .interaction import apply_interaction_result, route_server_interaction
from .models import EventType
from .session_manager import SessionNotFoundError, get_session_manager
from .turn_stream import turn_frames

router = APIRouter()


@dataclass
class _ConnectionState:
    """Bounded outbound state owned by one WebSocket connection."""

    queue: asyncio.Queue[tuple[dict[str, Any], asyncio.Future[None]]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=64)
    )
    writer: asyncio.Task[None] | None = None
    closed: bool = False


class ConnectionManager:
    """Manages WebSocket connections."""

    def __init__(self):
        # session_id -> list of websockets
        self._connections: dict[str, list[WebSocket]] = {}
        # Global connections (not tied to a session)
        self._global_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()
        self._states: dict[WebSocket, _ConnectionState] = {}

    async def connect(self, websocket: WebSocket, session_id: str | None = None) -> None:
        """Accept a new WebSocket connection.

        Args:
            websocket: The WebSocket connection.
            session_id: Optional session ID to associate with.
        """
        await websocket.accept()

        async with self._lock:
            state = _ConnectionState()
            self._states[websocket] = state
            state.writer = asyncio.create_task(
                self._write_messages(websocket, state),
                name=f"ankaloop-ws-writer-{id(websocket)}",
            )
            if session_id:
                if session_id not in self._connections:
                    self._connections[session_id] = []
                self._connections[session_id].append(websocket)
            else:
                self._global_connections.append(websocket)

        # Send connected event
        await self._send_message(
            websocket,
            {
                "type": "event",
                "payload": {
                    "kind": EventType.CONNECTED.value,
                    "session_id": session_id,
                },
            },
        )

    async def disconnect(self, websocket: WebSocket, session_id: str | None = None) -> None:
        """Remove a WebSocket connection.

        Args:
            websocket: The WebSocket connection.
            session_id: Optional session ID.
        """
        state = None
        async with self._lock:
            empty_sessions = []
            for subscribed_session, connections in self._connections.items():
                if websocket in connections:
                    connections.remove(websocket)
                if not connections:
                    empty_sessions.append(subscribed_session)
            for subscribed_session in empty_sessions:
                del self._connections[subscribed_session]
            if websocket in self._global_connections:
                self._global_connections.remove(websocket)
            state = self._states.pop(websocket, None)
        if state is not None:
            await self._shutdown_connection(websocket, state)

    async def send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """Queue one bounded write and wait until the writer sends it."""
        state = self._states.get(websocket)
        if state is None or state.closed:
            raise WebSocketDisconnect()
        if "timestamp" not in message:
            message["timestamp"] = datetime.now().isoformat()
        sent = asyncio.get_running_loop().create_future()
        try:
            state.queue.put_nowait((message, sent))
        except asyncio.QueueFull:
            await self._shutdown_connection(websocket, state, code=1013, reason="Slow consumer")
            raise WebSocketDisconnect(code=1013, reason="Slow consumer") from None
        await sent

    async def _write_messages(self, websocket: WebSocket, state: _ConnectionState) -> None:
        """Run the sole socket writer for a connection."""
        current: asyncio.Future[None] | None = None
        try:
            while True:
                message, current = await state.queue.get()
                await websocket.send_json(message)
                if not current.done():
                    current.set_result(None)
                current = None
        except asyncio.CancelledError:
            if current is not None and not current.done():
                current.cancel()
            raise
        except Exception as exc:
            if current is not None and not current.done():
                current.set_exception(exc)
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="WebSocket write failed")
        finally:
            state.closed = True
            while not state.queue.empty():
                _, pending = state.queue.get_nowait()
                if not pending.done():
                    pending.set_exception(WebSocketDisconnect())

    async def _shutdown_connection(
        self,
        websocket: WebSocket,
        state: _ConnectionState,
        *,
        code: int = 1000,
        reason: str | None = None,
    ) -> None:
        """Stop one writer and close its transport without affecting turns."""
        state.closed = True
        writer = state.writer
        if writer is not None and writer is not asyncio.current_task() and not writer.done():
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        while not state.queue.empty():
            _, pending = state.queue.get_nowait()
            if not pending.done():
                pending.set_exception(WebSocketDisconnect(code=code, reason=reason))
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=reason)

    async def send_to_session(self, session_id: str, message: dict[str, Any]) -> None:
        """Send a message to all connections for a session.

        Args:
            session_id: The session ID.
            message: The message to send.
        """
        connections = self._connections.get(session_id, [])
        for conn in connections:
            await self._send_message(conn, message)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Broadcast a message to all connections.

        Args:
            message: The message to broadcast.
        """
        # Send to global connections
        for conn in self._global_connections:
            await self._send_message(conn, message)

        # Send to all session connections
        for connections in self._connections.values():
            for conn in connections:
                await self._send_message(conn, message)

    async def _send_message(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """Send a message to a WebSocket.

        Args:
            websocket: The WebSocket connection.
            message: The message to send.
        """
        with contextlib.suppress(Exception):
            await self.send(websocket, message)

    def get_connection_stats(self) -> dict[str, Any]:
        """Get connection statistics.

        Returns:
            Dictionary with connection counts per session and global.
        """
        session_counts = {session_id: len(connections) for session_id, connections in self._connections.items()}
        return {
            "global_connections": len(self._global_connections),
            "session_connections": session_counts,
            "total_sessions_with_clients": len(self._connections),
            "total_connections": len(self._global_connections) + sum(len(c) for c in self._connections.values()),
        }

    def get_session_connection_count(self, session_id: str) -> int:
        """Get the number of connections for a specific session.

        Args:
            session_id: The session ID.

        Returns:
            Number of WebSocket connections for the session.
        """
        return len(self._connections.get(session_id, []))


# Global connection manager
connection_manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str | None = Query(default=None),
):
    """WebSocket endpoint for real-time communication.

    Query parameters:
        session_id: Optional session ID to bind this connection to.

    Message format:
        {
            "type": "request|response|event|error",
            "id": "message-id",
            "timestamp": "ISO timestamp",
            "payload": { ... }
        }

    Supported request actions:
        - prompt: Send a prompt to a session
        - cancel: Cancel current operation
        - subscribe: Subscribe to session events
        - ping: Keep-alive ping
    """
    auth = websocket.scope["app"].state.server_config.auth
    if auth.enabled:
        authorization = websocket.headers.get("Authorization", "")
        scheme, _, bearer_key = authorization.partition(" ")
        header_key = bearer_key if scheme.lower() == "bearer" and bearer_key else None
        supplied_key = header_key or websocket.headers.get("X-API-Key")
        expected_key = auth.api_key or ""
        if supplied_key is None or not secrets.compare_digest(supplied_key, expected_key):
            await websocket.close(code=1008, reason="Invalid or missing API key")
            return

    await connection_manager.connect(websocket, session_id)
    prompt_tasks: set[asyncio.Task[None]] = set()
    # Handshake headers may carry a W3C traceparent; prompt tasks join it.
    trace_carrier = carrier_from_headers(websocket.headers)

    def prompt_finished(task: asyncio.Task[None]) -> None:
        prompt_tasks.discard(task)
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()

    try:
        while True:
            # Receive message
            data = await websocket.receive_json()

            # Parse message
            msg_id = data.get("id", str(uuid.uuid4()))
            payload = data.get("payload", {})

            # Handle different actions
            action = payload.get("action", "")

            if action == "ping":
                # Respond with pong
                await connection_manager.send(
                    websocket,
                    {
                        "type": "response",
                        "id": msg_id,
                        "payload": {"action": "pong"},
                    },
                )

            elif action == "prompt":
                with attach_carrier(trace_carrier):
                    task = asyncio.create_task(handle_prompt(websocket, msg_id, payload))
                prompt_tasks.add(task)
                task.add_done_callback(prompt_finished)

            elif action == "cancel":
                # Handle cancel
                await handle_cancel(websocket, msg_id, payload)

            elif action == "subscribe":
                # Subscribe to a session
                target_session = payload.get("session_id")
                if target_session:
                    async with connection_manager._lock:
                        if target_session not in connection_manager._connections:
                            connection_manager._connections[target_session] = []
                        if websocket not in connection_manager._connections[target_session]:
                            connection_manager._connections[target_session].append(websocket)

                    await connection_manager.send(
                        websocket,
                        {
                            "type": "response",
                            "id": msg_id,
                            "payload": {
                                "action": "subscribed",
                                "session_id": target_session,
                            },
                        },
                    )

            else:
                # Unknown action
                await connection_manager.send(
                    websocket,
                    {
                        "type": "error",
                        "id": msg_id,
                        "payload": {
                            "error": f"Unknown action: {action}",
                            "code": "UNKNOWN_ACTION",
                        },
                    },
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        raise
    finally:
        for task in tuple(prompt_tasks):
            task.cancel()
        if prompt_tasks:
            await asyncio.gather(*prompt_tasks, return_exceptions=True)
        await connection_manager.disconnect(websocket, session_id)


async def handle_prompt(websocket: WebSocket, msg_id: str, payload: dict[str, Any]) -> None:
    """Handle a prompt request via WebSocket.

    Args:
        websocket: The WebSocket connection.
        msg_id: The message ID.
        payload: The request payload.
    """
    session_id = payload.get("session_id")
    content = payload.get("content", "")

    if not session_id:
        await connection_manager.send(
            websocket,
            {
                "type": "error",
                "id": msg_id,
                "payload": {"error": "session_id is required", "code": "MISSING_SESSION_ID"},
            },
        )
        return

    session_manager = get_session_manager()

    try:
        routed, _ = await route_server_interaction(session_manager, session_id, content)

        # Stream response or command side effects
        if routed.action == "prompt":
            handle = await session_manager.submit_prompt(
                session_id=session_id,
                content=routed.content,
                stream=True,
                priority=payload.get("priority", "normal"),
            )
            async for frame in turn_frames(handle, session_id):
                await connection_manager.send(
                    websocket,
                    {"type": "response", "id": msg_id, "payload": frame},
                )
        else:
            await connection_manager.send(
                websocket,
                {
                    "type": "response",
                    "id": msg_id,
                    "payload": {
                        "type": "start",
                        "turn_id": msg_id,
                        "session_id": session_id,
                        "status": "running",
                    },
                },
            )
            async for event in apply_interaction_result(session_manager, session_id, routed):
                await connection_manager.send(
                    websocket,
                    {
                        "type": "response",
                        "id": msg_id,
                        "payload": event,
                    },
                )
            await connection_manager.send(
                websocket,
                {
                    "type": "response",
                    "id": msg_id,
                    "payload": {"type": "complete", "turn_id": msg_id},
                },
            )

    except SessionNotFoundError:
        await connection_manager.send(
            websocket,
            {
                "type": "error",
                "id": msg_id,
                "payload": {
                    "error": f"Session not found: {session_id}",
                    "code": "SESSION_NOT_FOUND",
                },
            },
        )
    except (WebSocketDisconnect, RuntimeError):
        return
    except Exception as e:
        envelope = ErrorEnvelope.from_exception(e)
        with contextlib.suppress(Exception):
            await connection_manager.send(
                websocket,
                {
                    "type": "error",
                    "id": msg_id,
                    "payload": {
                        "error": envelope.message,
                        "code": envelope.code,
                        "retryable": envelope.retryable,
                        "details": envelope.details,
                    },
                },
            )


async def handle_cancel(websocket: WebSocket, msg_id: str, payload: dict[str, Any]) -> None:
    """Handle a cancel request via WebSocket.

    Args:
        websocket: The WebSocket connection.
        msg_id: The message ID.
        payload: The request payload.
    """
    session_id = payload.get("session_id")
    force = payload.get("force", False)

    if not session_id:
        await connection_manager.send(
            websocket,
            {
                "type": "error",
                "id": msg_id,
                "payload": {"error": "session_id is required", "code": "MISSING_SESSION_ID"},
            },
        )
        return

    session_manager = get_session_manager()

    try:
        await session_manager.cancel_session(session_id, force=force)

        await connection_manager.send(
            websocket,
            {
                "type": "response",
                "id": msg_id,
                "payload": {
                    "action": "cancelled",
                    "session_id": session_id,
                },
            },
        )

    except SessionNotFoundError:
        await connection_manager.send(
            websocket,
            {
                "type": "error",
                "id": msg_id,
                "payload": {
                    "error": f"Session not found: {session_id}",
                    "code": "SESSION_NOT_FOUND",
                },
            },
        )


def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager."""
    return connection_manager
