"""Transport-neutral streaming frames for runtime-owned turns."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from ..runtime import TurnEvent, TurnHandle, TurnStatus

DEFAULT_KEEPALIVE_INTERVAL = 15.0
"""Default seconds of stream inactivity before a keepalive ``ping`` frame is sent."""


async def _next_event(
    queue: asyncio.Queue[TurnEvent],
    keepalive_interval: float | None,
) -> TurnEvent | None:
    """Wait for the next turn event, returning None when the stream stays idle.

    Cancelling the ``queue.get()`` on timeout can, in a rare race with event
    delivery, drop that one event; the relay tolerates this because terminal
    state is re-derived from the handle once the queue drains.
    """
    if keepalive_interval is None:
        return await queue.get()
    try:
        return await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
    except TimeoutError:
        return None


def _event_frame(event: TurnEvent, handle: TurnHandle) -> dict[str, Any] | None:
    """Convert an internal turn event to the public streaming schema."""
    base = {"turn_id": event.turn_id}
    if event.type == "message.chunk":
        return {"type": "chunk", **base, "content": event.data.get("content", "")}
    if event.type == "message.reasoning":
        return {"type": "reasoning", **base, "content": event.data.get("content", "")}
    if event.type.startswith("tool.call_"):
        return {
            "type": "tool",
            **base,
            "event": event.type.removeprefix("tool."),
            **{key: value for key, value in event.data.items() if key not in {"session_id", "turn_id"}},
        }
    if event.type == "turn.completed":
        return {"type": "complete", **base, "status": "completed"}
    if event.type in {"turn.failed", "turn.cancelled"}:
        cancelled = event.type == "turn.cancelled"
        envelope = handle.outcome.error_envelope if handle.outcome is not None else None
        return {
            "type": "error",
            **base,
            "error": (
                envelope.message
                if envelope
                else event.data.get("error") or ("Turn cancelled" if cancelled else "Turn failed")
            ),
            "code": "TURN_CANCELLED" if cancelled else envelope.code if envelope else "AGENT_ERROR",
            "retryable": envelope.retryable if envelope else False,
            "details": envelope.details if envelope else {},
        }
    if event.type == "stream.overflow":
        return {
            "type": "error",
            **base,
            "error": event.data["error"],
            "code": "SLOW_CONSUMER",
            "retryable": True,
        }
    return None


async def turn_frames(
    handle: TurnHandle,
    session_id: str,
    *,
    keepalive_interval: float | None = DEFAULT_KEEPALIVE_INTERVAL,
) -> AsyncGenerator[dict[str, Any], None]:
    """Relay one turn without making the transport own its execution lifecycle.

    Args:
        handle: The turn to relay.
        session_id: The session the turn belongs to.
        keepalive_interval: Seconds the stream may stay idle before a
            ``{"type": "ping"}`` frame is emitted, keeping proxies and clients
            with read timeouts from dropping the connection during long tool
            executions. ``None`` disables keepalive frames.
    """
    queue = handle.subscribe()
    sent_chunk = False
    sent_reasoning = False
    try:
        yield {
            "type": "start",
            "turn_id": handle.id,
            "session_id": session_id,
            "status": handle.status.value,
        }
        while True:
            if handle.done and queue.empty():
                outcome = handle.outcome
                if outcome is not None and not sent_reasoning and outcome.reasoning_content:
                    yield {"type": "reasoning", "turn_id": handle.id, "content": outcome.reasoning_content}
                if outcome is not None and outcome.value:
                    yield {"type": "chunk", "turn_id": handle.id, "content": outcome.value}
                if handle.status == TurnStatus.CANCELLED:
                    event_type = "turn.cancelled"
                elif handle.status == TurnStatus.COMPLETED:
                    event_type = "turn.completed"
                else:
                    event_type = "turn.failed"
                frame = _event_frame(TurnEvent.output(handle.id, event_type), handle)
                assert frame is not None
                yield frame
                return
            event = await _next_event(queue, keepalive_interval)
            if event is None:
                yield {"type": "ping", "turn_id": handle.id}
                continue
            frame = _event_frame(event, handle)
            if frame is None:
                continue
            if frame["type"] == "complete":
                outcome = handle.outcome
                if not sent_reasoning and outcome is not None and outcome.reasoning_content:
                    yield {"type": "reasoning", "turn_id": handle.id, "content": outcome.reasoning_content}
                if not sent_chunk and outcome is not None and outcome.value:
                    yield {"type": "chunk", "turn_id": handle.id, "content": outcome.value}
            if frame["type"] == "chunk":
                sent_chunk = True
            elif frame["type"] == "reasoning":
                sent_reasoning = True
            yield frame
            if frame["type"] in {"complete", "error"}:
                return
    finally:
        handle.unsubscribe(queue)
