"""Single-owner session runtime with per-turn results and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import uuid
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from .tracing import capture_carrier

logger = logging.getLogger(__name__)


class MessagePriority(IntEnum):
    """Priority used by the canonical session runtime."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


class TurnStatus(StrEnum):
    """Lifecycle states for one submitted turn."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionRuntimeStatus(StrEnum):
    """Execution states owned by a session runtime."""

    IDLE = "idle"
    BUSY = "busy"
    CANCELLED = "cancelled"
    ERROR = "error"
    CLOSED = "closed"


class RuntimeClosedError(RuntimeError):
    """Raised when work is submitted to a closed session runtime."""


class TurnCancelledError(asyncio.CancelledError):
    """Raised when awaiting a cancelled turn."""


@dataclass(frozen=True)
class CancellationResult:
    """Summary of work cancelled by one runtime operation."""

    active_cancelled: bool
    queued_cancelled: int


@dataclass(frozen=True)
class TurnRequest:
    """Immutable request submitted to a session runtime."""

    id: str
    session_id: str
    prompt: str
    work_dir: Path | None
    stream: bool
    show_progress: bool
    priority: MessagePriority
    created_at: datetime = field(default_factory=datetime.now)
    trace_context: Mapping[str, str] = field(default_factory=dict)
    """W3C trace-context carrier captured at submit time (empty when untraced)."""


@dataclass(frozen=True)
class ErrorEnvelope:
    """Transport-neutral description of a turn failure."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_exception(cls, error: BaseException) -> ErrorEnvelope:
        """Build a stable error envelope without depending on provider modules."""
        kind = getattr(error, "kind", None)
        kind_value = getattr(kind, "value", None)
        code = f"PROVIDER_{str(kind_value).upper()}" if kind_value else "AGENT_ERROR"
        details: dict[str, Any] = {"exception_type": type(error).__name__}
        status_code = getattr(error, "status_code", None)
        if status_code is not None:
            details["status_code"] = status_code
        return cls(
            code=code,
            message=str(error),
            retryable=bool(getattr(error, "retryable", False)),
            details=details,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TurnResult:
    """Terminal result stored for every turn."""

    status: TurnStatus
    value: str | None = None
    error: BaseException | None = None
    error_envelope: ErrorEnvelope | None = None

    @classmethod
    def failed(cls, error: BaseException) -> TurnResult:
        """Create a failed result with its stable public error representation."""
        return cls(
            status=TurnStatus.FAILED,
            error=error,
            error_envelope=ErrorEnvelope.from_exception(error),
        )


@dataclass(frozen=True, init=False)
class TurnEvent:
    """One transport-neutral lifecycle or output event."""

    turn_id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    status: TurnStatus | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    def __init__(
        self,
        turn_id: str,
        event_type: str | TurnStatus | None = None,
        value: dict[str, Any] | datetime | None = None,
        *,
        data: dict[str, Any] | None = None,
        status: TurnStatus | None = None,
        timestamp: datetime | None = None,
        type: str | TurnStatus | None = None,
    ) -> None:
        """Accept both legacy lifecycle and stream event constructor shapes."""
        if event_type is None:
            event_type = type
        if event_type is None:
            raise TypeError("TurnEvent requires an event type or status")
        if isinstance(event_type, TurnStatus):
            status = event_type
            resolved_type = f"turn.{event_type.value}"
            if isinstance(value, datetime):
                timestamp = value
        else:
            resolved_type = event_type
            if isinstance(value, dict):
                data = value
            elif isinstance(value, datetime):
                timestamp = value
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "type", resolved_type)
        object.__setattr__(self, "data", dict(data or {}))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "timestamp", timestamp or datetime.now())

    @classmethod
    def transition(cls, turn_id: str, status: TurnStatus) -> TurnEvent:
        """Create a canonical lifecycle transition event."""
        return cls(
            turn_id=turn_id,
            type=f"turn.{status.value}",
            status=status,
            data={"turn_status": status.value},
        )

    @classmethod
    def output(
        cls,
        turn_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> TurnEvent:
        """Create a canonical output event."""
        return cls(turn_id=turn_id, type=event_type, data=data or {})


# Compatibility alias for integrations that imported the previous stream-only name.
TurnStreamEvent = TurnEvent


class TurnHandle:
    """Track and await one turn independently from other queued turns."""

    def __init__(self, runtime: SessionRuntime, request: TurnRequest):
        self._runtime = runtime
        self.request = request
        self.status = TurnStatus.QUEUED
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.trace_id: str | None = None
        """Hex trace id of the turn's ``invoke_agent`` span, set once it starts."""
        self._completion: asyncio.Future[TurnResult] = asyncio.get_running_loop().create_future()
        self.events: asyncio.Queue[TurnEvent] = asyncio.Queue()
        self.events.put_nowait(TurnEvent.transition(self.id, TurnStatus.QUEUED))
        self._stream_subscribers: set[asyncio.Queue[TurnEvent]] = set()
        self._done_callbacks: list[Callable[[TurnHandle], None]] = []
        self._event_callbacks: list[Callable[[TurnEvent], None]] = []

    @property
    def id(self) -> str:
        """Return the turn ID."""
        return self.request.id

    @property
    def done(self) -> bool:
        """Return whether the turn reached a terminal state."""
        return self._completion.done()

    @property
    def outcome(self) -> TurnResult | None:
        """Return the terminal outcome without blocking."""
        return self._completion.result() if self._completion.done() else None

    async def wait(self) -> str:
        """Wait for this turn and return its result or raise its terminal error."""
        result = await asyncio.shield(self._completion)
        if result.status == TurnStatus.CANCELLED:
            raise TurnCancelledError(f"Turn {self.id} was cancelled")
        if result.status == TurnStatus.FAILED:
            assert result.error is not None
            raise result.error
        return result.value or ""

    async def cancel(self) -> bool:
        """Cancel this queued or active turn."""
        return await self._runtime.cancel_turn(self.id)

    def add_done_callback(self, callback: Callable[[TurnHandle], None]) -> None:
        """Register a callback invoked once after the terminal result is stored."""
        if self.done:
            callback(self)
            return
        self._done_callbacks.append(callback)

    def add_event_callback(self, callback: Callable[[TurnEvent], None]) -> None:
        """Observe future lifecycle transitions without consuming the event queue."""
        self._event_callbacks.append(callback)

    def subscribe(self, *, max_queue_size: int = 256) -> asyncio.Queue[TurnEvent]:
        """Subscribe to output produced after this call."""
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        queue: asyncio.Queue[TurnEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._stream_subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[TurnEvent]) -> None:
        """Stop an output subscription without affecting turn execution."""
        self._stream_subscribers.discard(queue)

    def _publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        event = TurnEvent.output(self.id, event_type, data)
        for queue in tuple(self._stream_subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._stream_subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    TurnEvent.output(
                        self.id,
                        "stream.overflow",
                        {"error": "Turn stream consumer is too slow"},
                    )
                )

    def _finish(self, result: TurnResult) -> None:
        if self.done:
            return
        self._transition(result.status)
        self.finished_at = datetime.now()
        self._completion.set_result(result)
        callbacks, self._done_callbacks = self._done_callbacks, []
        for callback in callbacks:
            with contextlib.suppress(Exception):
                callback(self)

    def _transition(self, status: TurnStatus) -> None:
        self.status = status
        event = TurnEvent.transition(self.id, status)
        self.events.put_nowait(event)
        for callback in tuple(self._event_callbacks):
            with contextlib.suppress(Exception):
                callback(event)


RuntimeProcessor = Callable[[TurnRequest], Coroutine[Any, Any, str]]
RuntimeEventCallback = Callable[[str, TurnHandle], None]


class SessionRuntime:
    """Own a session queue, active task, turn states, and cancellation.

    A runtime executes at most one turn at a time. Every accepted turn reaches
    a terminal state, queued turns cleared by cancellation become cancelled,
    and active cancellation is awaited before the operation returns. Closing
    is permanent and idempotent.
    """

    def __init__(
        self,
        session_id: str,
        processor: RuntimeProcessor,
        event_callback: RuntimeEventCallback | None = None,
        *,
        terminal_handle_retention: int = 200,
    ):
        if terminal_handle_retention < 0:
            raise ValueError("terminal_handle_retention must be non-negative")
        self.session_id = session_id
        self._processor = processor
        self._event_callback = event_callback
        self._terminal_handle_retention = terminal_handle_retention
        self._queue: list[tuple[int, int, TurnHandle]] = []
        self._turns: dict[str, TurnHandle] = {}
        self._terminal_turn_ids: deque[str] = deque()
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._active_handle: TurnHandle | None = None
        self._active_task: asyncio.Task[str] | None = None
        self._closed = False
        self.status = SessionRuntimeStatus.IDLE

    @property
    def is_busy(self) -> bool:
        """Return whether a turn is active or queued."""
        return self._active_handle is not None or bool(self._queue)

    @property
    def queued_count(self) -> int:
        """Return the number of queued, not active, turns."""
        return len(self._queue)

    @property
    def active_turn(self) -> TurnHandle | None:
        """Return the currently running turn."""
        return self._active_handle

    @property
    def is_closed(self) -> bool:
        """Return whether this runtime permanently stopped accepting work."""
        return self._closed

    def has_pending_work(self, *, excluding: TurnHandle | None = None) -> bool:
        """Return whether active or queued work remains besides one handle."""
        active_pending = (
            self._active_handle is not None and self._active_handle is not excluding and not self._active_handle.done
        )
        queued_pending = any(handle is not excluding and not handle.done for _, _, handle in self._queue)
        return active_pending or queued_pending

    def get_turn(self, turn_id: str) -> TurnHandle | None:
        """Return a previously submitted turn."""
        return self._turns.get(turn_id)

    def queued_prompts(self) -> list[str]:
        """Return queued prompts in execution order."""
        return [item[2].request.prompt for item in sorted(self._queue)]

    async def submit(
        self,
        prompt: str,
        *,
        work_dir: Path | None = None,
        stream: bool = True,
        show_progress: bool = True,
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> TurnHandle:
        """Submit a turn and return its independent handle."""
        request = TurnRequest(
            id=f"turn-{uuid.uuid4().hex[:12]}",
            session_id=self.session_id,
            prompt=prompt,
            work_dir=work_dir.resolve() if work_dir else None,
            stream=stream,
            show_progress=show_progress,
            priority=priority,
            trace_context=capture_carrier(),
        )
        handle = TurnHandle(self, request)
        async with self._lock:
            if self._closed:
                raise RuntimeClosedError(f"Session runtime {self.session_id} is closed")
            self._sequence += 1
            heapq.heappush(self._queue, (-priority.value, self._sequence, handle))
            self._turns[handle.id] = handle
            self._emit("turn.queued", handle)
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(
                    self._run_queue(),
                    name=f"ankaloop-session-{self.session_id}",
                )
        return handle

    async def cancel_active(self, *, clear_queue: bool = False) -> bool:
        """Cancel the active turn and optionally cancel all queued turns."""
        result = await self._cancel(clear_queue=clear_queue)
        return result.active_cancelled

    async def cancel_all(self) -> CancellationResult:
        """Cancel and await the active turn, and cancel every queued turn."""
        return await self._cancel(clear_queue=True)

    async def _cancel(self, *, clear_queue: bool) -> CancellationResult:
        async with self._lock:
            task = self._active_task
            cancelled = task is not None and not task.done()
            if cancelled and task is not None:
                task.cancel()
            queued_cancelled = self._cancel_queued_locked() if clear_queue else 0
            active_handle = self._active_handle
        if task is not None and cancelled:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if active_handle is not None and not active_handle.done:
                await asyncio.shield(active_handle._completion)
        return CancellationResult(
            active_cancelled=cancelled,
            queued_cancelled=queued_cancelled,
        )

    async def cancel_turn(self, turn_id: str) -> bool:
        """Cancel a queued or active turn by ID."""
        task: asyncio.Task[str] | None = None
        async with self._lock:
            handle = self._turns.get(turn_id)
            if handle is None or handle.done:
                return False
            if self._active_handle is handle:
                task = self._active_task
                if task is not None:
                    task.cancel()
            else:
                self._queue = [item for item in self._queue if item[2] is not handle]
                heapq.heapify(self._queue)
                handle._finish(TurnResult(TurnStatus.CANCELLED))
                self._emit("turn.cancelled", handle)
                self._record_terminal_turn_locked(handle)
                return True
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if not handle.done:
                await asyncio.shield(handle._completion)
        return True

    async def clear_queue(self) -> int:
        """Cancel all queued turns while preserving the active turn."""
        async with self._lock:
            return self._cancel_queued_locked()

    async def close(self) -> None:
        """Permanently stop accepting work and await all runtime termination."""
        async with self._close_lock:
            async with self._lock:
                if self.status == SessionRuntimeStatus.CLOSED:
                    return
                self._closed = True
            await self._cancel(clear_queue=True)
            worker = self._worker
            if worker is not None and not worker.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

            async with self._lock:
                self._worker = None
                self.status = SessionRuntimeStatus.CLOSED

    def _cancel_queued_locked(self) -> int:
        count = len(self._queue)
        while self._queue:
            _, _, handle = heapq.heappop(self._queue)
            handle._finish(TurnResult(TurnStatus.CANCELLED))
            self._emit("turn.cancelled", handle)
            self._record_terminal_turn_locked(handle)
        return count

    async def _run_queue(self) -> None:
        while True:
            async with self._lock:
                if not self._queue:
                    self._worker = None
                    if self._closed:
                        self.status = SessionRuntimeStatus.CLOSED
                    elif self.status not in {
                        SessionRuntimeStatus.CANCELLED,
                        SessionRuntimeStatus.ERROR,
                    }:
                        self.status = SessionRuntimeStatus.IDLE
                    return
                _, _, handle = heapq.heappop(self._queue)
                if handle.done:
                    continue
                self._active_handle = handle
                handle._transition(TurnStatus.RUNNING)
                handle.started_at = datetime.now()
                self.status = SessionRuntimeStatus.BUSY
                self._emit("turn.running", handle)
                task: asyncio.Task[str] = asyncio.create_task(
                    self._processor(handle.request),
                    name=f"ankaloop-turn-{handle.id}",
                )
                self._active_task = task

            try:
                value = await task
            except asyncio.CancelledError:
                handle._finish(TurnResult(TurnStatus.CANCELLED))
                self.status = SessionRuntimeStatus.CANCELLED
                self._emit("turn.cancelled", handle)
            except BaseException as exc:
                handle._finish(TurnResult.failed(exc))
                self.status = SessionRuntimeStatus.ERROR
                self._emit("turn.failed", handle)
            else:
                handle._finish(TurnResult(TurnStatus.COMPLETED, value=value))
                self.status = SessionRuntimeStatus.IDLE
                self._emit("turn.completed", handle)
            finally:
                async with self._lock:
                    if self._active_handle is handle:
                        self._active_handle = None
                        self._active_task = None
                    self._record_terminal_turn_locked(handle)

    def _record_terminal_turn_locked(self, handle: TurnHandle) -> None:
        self._terminal_turn_ids.append(handle.id)
        while len(self._terminal_turn_ids) > self._terminal_handle_retention:
            self._turns.pop(self._terminal_turn_ids.popleft(), None)

    def _emit(self, event: str, handle: TurnHandle) -> None:
        data: dict[str, Any] = {"turn_status": handle.status.value}
        if handle.trace_id is not None:
            data["trace_id"] = handle.trace_id
        outcome = handle.outcome
        if outcome is not None:
            data["response"] = outcome.value
            if outcome.error is not None:
                data["error"] = str(outcome.error)
            if outcome.error_envelope is not None:
                data["error_envelope"] = outcome.error_envelope.to_dict()
        handle._publish(event, data)
        if self._event_callback is not None:
            try:
                self._event_callback(event, handle)
            except Exception:
                logger.exception(
                    "Session runtime observer failed for %s on turn %s",
                    event,
                    handle.id,
                )

    def publish_turn_event(self, turn_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Publish agent output to subscribers of the active turn."""
        handle = self._turns.get(turn_id)
        if handle is not None and not handle.done:
            handle._publish(event_type, data)
