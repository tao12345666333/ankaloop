"""WebSocket client for AnkaLoop.

Provides WebSocket-based real-time communication with AnkaLoop servers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .base import ResponseChunk
from .exceptions import ConnectionError, PromptError, SessionNotFoundError, StreamTimeoutError


class WebSocketClient:
    """WebSocket client for real-time AnkaLoop communication.

    Provides bidirectional streaming with the server.

    Example:
        async with WebSocketClient("http://localhost:8080", session_id="my-session") as ws:
            await ws.send_prompt("Hello")
            async for message in ws:
                if message.done:
                    break
                print(message.content)
    """

    def __init__(
        self,
        url: str,
        session_id: str | None = None,
        *,
        timeout: float = 30.0,
        stream_timeout: float | None = 300.0,
        headers: dict[str, str] | None = None,
        api_key: str | None = None,
    ):
        """Initialize the WebSocket client.

        Args:
            url: Server URL (HTTP URL will be converted to WS).
            session_id: Optional session ID to bind to.
            timeout: Connection timeout in seconds.
            stream_timeout: Maximum seconds to wait between stream frames in
                ``prompt_stream``, or ``None`` to wait indefinitely.
            headers: Optional WebSocket handshake headers.
            api_key: Optional API key sent as a Bearer token.
        """
        self._url = url.rstrip("/")
        self._session_id = session_id
        self._timeout = timeout
        self._stream_timeout = stream_timeout
        self._headers = dict(headers or {})
        if api_key is not None and not any(key.lower() == "authorization" for key in self._headers):
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._message_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._receive_task: asyncio.Task | None = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._stream_queues: dict[str, asyncio.Queue[dict]] = {}
        self._overflowed_streams: set[str] = set()

    @property
    def ws_url(self) -> str:
        """Get the WebSocket URL."""
        # Convert http(s) to ws(s)
        url = self._url
        if url.startswith("http://"):
            url = "ws://" + url[7:]
        elif url.startswith("https://"):
            url = "wss://" + url[8:]

        ws_url = f"{url}/ws"
        if self._session_id:
            ws_url += "?" + urlencode({"session_id": self._session_id})
        return ws_url

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return bool(self._ws is not None and self._ws.open)

    async def connect(self) -> None:
        """Connect to the WebSocket server.

        Raises:
            ConnectionError: If connection fails.
        """
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.ws_url, additional_headers=self._headers),
                timeout=self._timeout,
            )

            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Wait for connected event
            msg = await asyncio.wait_for(
                self._message_queue.get(),
                timeout=self._timeout,
            )

            if msg.get("type") != "event" or msg.get("payload", {}).get("kind") != "connected":
                raise ConnectionError(f"Unexpected connection response: {msg}")

        except TimeoutError as e:
            raise ConnectionError(f"WebSocket connection timeout: {self._timeout}s") from e
        except WebSocketException as e:
            raise ConnectionError(f"WebSocket error: {e}") from e
        except Exception as e:
            raise ConnectionError(f"Failed to connect: {e}") from e

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._receive_task:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        # Cancel pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        self._fail_streams("WebSocket connection closed")

    async def __aenter__(self) -> WebSocketClient:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    async def _receive_loop(self) -> None:
        """Background task to receive messages."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)
                    await self._dispatch_message(data)

                except json.JSONDecodeError:
                    continue

        except ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            self._fail_streams("WebSocket connection closed")

    async def _dispatch_message(self, data: dict[str, Any]) -> None:
        """Route one frame to its request-scoped consumer."""
        msg_id = data.get("id")
        if msg_id and msg_id in self._pending_requests:
            future = self._pending_requests[msg_id]
            if not future.done():
                future.set_result(data)
        elif msg_id and msg_id in self._stream_queues:
            if msg_id in self._overflowed_streams:
                return
            queue = self._stream_queues[msg_id]
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                self._overflowed_streams.add(msg_id)
                self._replace_queue_with_error(queue, msg_id, "WebSocket stream consumer is too slow")
        else:
            if self._message_queue.full():
                self._message_queue.get_nowait()
            self._message_queue.put_nowait(data)

    def _fail_streams(self, error: str) -> None:
        for msg_id, queue in tuple(self._stream_queues.items()):
            self._replace_queue_with_error(queue, msg_id, error, code="CONNECTION_CLOSED")

    @staticmethod
    def _replace_queue_with_error(
        queue: asyncio.Queue[dict],
        msg_id: str,
        error: str,
        *,
        code: str = "SLOW_CONSUMER",
    ) -> None:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(
            {
                "type": "error",
                "id": msg_id,
                "payload": {"error": error, "code": code},
            }
        )

    async def _send(self, message: dict) -> None:
        """Send a message.

        Args:
            message: Message dictionary.

        Raises:
            ConnectionError: If not connected.
        """
        if not self._ws:
            raise ConnectionError("WebSocket is not connected")

        await self._ws.send(json.dumps(message))

    async def _request(
        self,
        action: str,
        payload: dict,
        *,
        timeout: float | None = None,
    ) -> dict:
        """Send a request and wait for response.

        Args:
            action: Action name.
            payload: Request payload.
            timeout: Optional timeout.

        Returns:
            Response data.
        """
        msg_id = str(uuid.uuid4())
        future: asyncio.Future[dict] = asyncio.Future()
        self._pending_requests[msg_id] = future

        try:
            await self._send(
                {
                    "type": "request",
                    "id": msg_id,
                    "timestamp": datetime.now().isoformat(),
                    "payload": {"action": action, **payload},
                }
            )

            return await asyncio.wait_for(
                future,
                timeout=timeout or self._timeout,
            )

        finally:
            self._pending_requests.pop(msg_id, None)

    # =========================================================================
    # Public API
    # =========================================================================

    async def ping(self) -> bool:
        """Send a ping and wait for pong.

        Returns:
            True if pong received.
        """
        try:
            response = await self._request("ping", {})
            return bool(response.get("payload", {}).get("action") == "pong")
        except Exception:
            return False

    async def subscribe(self, session_id: str) -> None:
        """Subscribe to a session's events.

        Args:
            session_id: The session ID.
        """
        await self._request("subscribe", {"session_id": session_id})

    async def send_prompt(
        self,
        content: str,
        session_id: str | None = None,
        *,
        priority: str = "normal",
    ) -> str:
        """Send a prompt via WebSocket.

        This starts the prompt but returns immediately. Use the iterator
        to receive streaming responses.

        Args:
            content: The prompt content.
            session_id: The session ID (uses bound session if not provided).
            priority: Message priority.

        Returns:
            Message ID for tracking.

        Raises:
            ConnectionError: If not connected.
            ValueError: If no session_id provided and not bound to one.
        """
        sid = session_id or self._session_id
        if not sid:
            raise ValueError("session_id is required")

        msg_id = str(uuid.uuid4())
        await self._send(
            {
                "type": "request",
                "id": msg_id,
                "timestamp": datetime.now().isoformat(),
                "payload": {
                    "action": "prompt",
                    "session_id": sid,
                    "content": content,
                    "priority": priority,
                },
            }
        )
        return msg_id

    async def send_cancel(
        self,
        session_id: str | None = None,
        *,
        force: bool = False,
    ) -> None:
        """Send a cancel request.

        Args:
            session_id: The session ID (uses bound session if not provided).
            force: Whether to force cancellation.

        Raises:
            ValueError: If no session_id provided and not bound to one.
        """
        sid = session_id or self._session_id
        if not sid:
            raise ValueError("session_id is required")

        await self._request("cancel", {"session_id": sid, "force": force})

    async def prompt_stream(
        self,
        content: str,
        session_id: str | None = None,
        *,
        priority: str = "normal",
    ) -> AsyncIterator[ResponseChunk]:
        """Send a prompt and stream the response.

        Args:
            content: The prompt content.
            session_id: The session ID.
            priority: Message priority.

        Yields:
            ResponseChunk objects.
        """
        sid = session_id or self._session_id
        if not sid:
            raise ValueError("session_id is required")

        msg_id = str(uuid.uuid4())
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._stream_queues[msg_id] = queue
        try:
            await self._send(
                {
                    "type": "request",
                    "id": msg_id,
                    "timestamp": datetime.now().isoformat(),
                    "payload": {
                        "action": "prompt",
                        "session_id": sid,
                        "content": content,
                        "priority": priority,
                    },
                }
            )

            # Receive responses with this message ID
            while True:
                data = await asyncio.wait_for(
                    queue.get(),
                    timeout=self._stream_timeout,
                )

                msg_type = data.get("type")
                payload = data.get("payload", {})

                if msg_type == "error":
                    error = payload.get("error", "Unknown error")
                    code = payload.get("code", "")
                    if code == "SESSION_NOT_FOUND":
                        raise SessionNotFoundError(sid)
                    raise PromptError(error, session_id=sid)

                if msg_type == "response":
                    kind = payload.get("type", payload.get("kind", ""))
                    content_chunk = payload.get("content", "")

                    if kind == "chunk":
                        yield ResponseChunk(
                            content=content_chunk,
                            chunk_type="text",
                            done=False,
                        )
                    elif kind == "tool":
                        tool_event = payload.get("event", "")
                        is_start = tool_event == "call_start"
                        yield ResponseChunk(
                            content=payload.get("tool_name", ""),
                            chunk_type="tool_call" if is_start else "tool_result",
                            done=False,
                            metadata=payload,
                        )
                    elif kind == "error":
                        raise PromptError(payload.get("error", "Unknown error"), session_id=sid)
                    elif kind == "complete":
                        yield ResponseChunk(
                            content="",
                            chunk_type="complete",
                            done=True,
                        )
                        return

                elif msg_type == "event":
                    kind = payload.get("kind", "")
                    if kind == "tool.call_start":
                        yield ResponseChunk(
                            content=payload.get("tool_name", ""),
                            chunk_type="tool_call",
                            done=False,
                            metadata=payload,
                        )
                    elif kind == "tool.call_complete":
                        yield ResponseChunk(
                            content=payload.get("result", ""),
                            chunk_type="tool_result",
                            done=False,
                            metadata=payload,
                        )

        except TimeoutError as e:
            raise StreamTimeoutError(
                f"Stream timed out after {self._stream_timeout}s without frames; "
                "the turn may still be running server-side",
                session_id=sid,
                timeout=self._stream_timeout,
            ) from e
        finally:
            self._stream_queues.pop(msg_id, None)
            self._overflowed_streams.discard(msg_id)

    def __aiter__(self) -> AsyncIterator[dict]:
        """Iterate over received messages."""
        return self._iterate_messages()

    async def _iterate_messages(self) -> AsyncIterator[dict]:
        """Iterate over messages from the queue."""
        while True:
            try:
                message = await self._message_queue.get()
                yield message
            except Exception:
                break
