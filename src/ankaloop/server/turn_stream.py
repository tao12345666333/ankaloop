"""Transport-neutral streaming frames for runtime-owned turns."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any

from ..runtime import TurnEvent, TurnEventQueue, TurnHandle, TurnStatus

DEFAULT_KEEPALIVE_INTERVAL = 15.0
"""Default seconds of stream inactivity before a keepalive ``ping`` frame is sent."""

DELTA_FLUSH_INTERVAL = 0.016
"""Maximum seconds a buffered delta frame waits before it is written.

A model can emit one ``message.chunk`` or ``message.reasoning`` event per token,
which is far more frames than a transport or a client needs. Delta frames are
therefore coalesced per turn and written at most once per interval. A delta
that ends a quiet period is written immediately, so the first token of a burst
is never delayed.
"""

_DELTA_FRAME_TYPES = frozenset({"chunk", "reasoning"})


class _DeltaBuffer:
    """Collect delta frame content until the next flush.

    Adjacent fragments of the same frame type are joined, and a switch between
    ``chunk`` and ``reasoning`` starts a new segment, so the relay never
    reorders content that arrived in a different order.
    """

    def __init__(self) -> None:
        self._segments: list[tuple[str, list[str]]] = []

    def __bool__(self) -> bool:
        return bool(self._segments)

    def add(self, frame_type: str, content: str) -> None:
        """Buffer one delta fragment."""
        if self._segments and self._segments[-1][0] == frame_type:
            self._segments[-1][1].append(content)
        else:
            self._segments.append((frame_type, [content]))

    def drain(self, turn_id: str) -> list[dict[str, Any]]:
        """Return one frame per buffered segment and clear the buffer."""
        frames = [
            {"type": frame_type, "turn_id": turn_id, "content": "".join(parts)} for frame_type, parts in self._segments
        ]
        self._segments = []
        return frames


async def _next_event(queue: TurnEventQueue, timeout: float | None) -> TurnEvent | None:
    """Wait for the next turn event, returning None when the wait times out.

    A cancelled wait leaves a delivered event queued for the next read, so a
    flush or keepalive timeout cannot lose output.
    """
    if timeout is None:
        return await queue.get()
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
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
    return None


async def turn_frames(
    handle: TurnHandle,
    session_id: str,
    *,
    keepalive_interval: float | None = DEFAULT_KEEPALIVE_INTERVAL,
) -> AsyncGenerator[dict[str, Any], None]:
    """Relay one turn without making the transport own its execution lifecycle.

    Delta frames are coalesced: at most one ``chunk`` or ``reasoning`` frame is
    written per :data:`DELTA_FLUSH_INTERVAL`, and the buffer is always flushed
    before a non-delta frame and before the terminal frame. When a consumer
    falls far enough behind that the turn's event queue discards deltas, the
    relay writes ``stream.dropped`` frames carrying the number of discarded
    delta frames, so a consumer can log the gap instead of inferring one.

    Args:
        handle: The turn to relay.
        session_id: The session the turn belongs to.
        keepalive_interval: Seconds the stream may stay idle before a
            ``{"type": "ping"}`` frame is emitted, keeping proxies and clients
            with read timeouts from dropping the connection during long tool
            executions. ``None`` disables keepalive frames.
    """
    queue = handle.subscribe()
    pending = _DeltaBuffer()
    sent_chunk = False
    sent_reasoning = False
    next_flush_at = 0.0

    def flush() -> list[dict[str, Any]]:
        """Return the buffered deltas, preceded by any unreported drop count."""
        frames = pending.drain(handle.id)
        dropped = queue.take_dropped()
        if dropped:
            frames.append({"type": "stream.dropped", "turn_id": handle.id, "count": dropped})
        return frames

    try:
        yield {
            "type": "start",
            "turn_id": handle.id,
            "session_id": session_id,
            "status": handle.status.value,
        }
        while True:
            if handle.done and queue.empty():
                for buffered in flush():
                    yield buffered
                outcome = handle.outcome
                if outcome is not None and not sent_reasoning and outcome.reasoning_content:
                    yield {"type": "reasoning", "turn_id": handle.id, "content": outcome.reasoning_content}
                if outcome is not None and not sent_chunk and outcome.value:
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

            if pending:
                event = await _next_event(queue, max(0.0, next_flush_at - time.monotonic()))
            else:
                event = await _next_event(queue, keepalive_interval)
            if event is None:
                if pending:
                    next_flush_at = time.monotonic() + DELTA_FLUSH_INTERVAL
                    for buffered in flush():
                        yield buffered
                else:
                    yield {"type": "ping", "turn_id": handle.id}
                continue

            frame = _event_frame(event, handle)
            if frame is None:
                continue
            if frame["type"] in _DELTA_FRAME_TYPES:
                if frame["type"] == "chunk":
                    sent_chunk = True
                else:
                    sent_reasoning = True
                pending.add(frame["type"], frame["content"])
                now = time.monotonic()
                if now >= next_flush_at:
                    next_flush_at = now + DELTA_FLUSH_INTERVAL
                    for buffered in flush():
                        yield buffered
                continue

            # A non-delta frame ends the current delta window: write what is
            # buffered, report any drops, then write the frame itself.
            next_flush_at = 0.0
            for buffered in flush():
                yield buffered
            if frame["type"] == "complete":
                outcome = handle.outcome
                if not sent_reasoning and outcome is not None and outcome.reasoning_content:
                    yield {"type": "reasoning", "turn_id": handle.id, "content": outcome.reasoning_content}
                if not sent_chunk and outcome is not None and outcome.value:
                    yield {"type": "chunk", "turn_id": handle.id, "content": outcome.value}
            yield frame
            if frame["type"] in {"complete", "error"}:
                return
    finally:
        handle.unsubscribe(queue)
