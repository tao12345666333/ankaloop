"""
Event Bus - Publish/Subscribe Event System for AnkaLoop.

This module provides a centralized event system for agent communication
and extensibility, inspired by OpenCode's event bus pattern.

Key Features:
- Type-safe events with dataclass support
- Async and sync event handlers
- Priority-based handler ordering
- Event filtering and pattern matching
- Weak reference support to prevent memory leaks
- Session-scoped and global events

Event Categories:
- Agent events: agent_started, agent_completed, agent_error
- Tool events: tool_started, tool_completed, tool_error
- Session events: session_created, session_destroyed
- Message events: message_received, message_queued
- System events: config_changed, shutdown

Example:
    from ankaloop.event_bus import get_event_bus, Event, EventType

    bus = get_event_bus()

    # Subscribe to events
    @bus.on(EventType.TOOL_COMPLETED)
    async def on_tool_completed(event: Event):
        print(f"Tool {event.data['tool_name']} completed")

    # Publish events
    await bus.emit(Event(
        type=EventType.TOOL_COMPLETED,
        data={"tool_name": "read_file", "result": "..."}
    ))
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .constants import CONFIG_DIR

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of events in the system."""

    # Agent lifecycle events
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_ERROR = "agent.error"
    AGENT_STEP = "agent.step"

    # Tool events
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_ERROR = "tool.error"
    TOOL_PERMISSION_REQUESTED = "tool.permission_requested"
    TOOL_PERMISSION_GRANTED = "tool.permission_granted"
    TOOL_PERMISSION_DENIED = "tool.permission_denied"

    # Session events
    SESSION_CREATED = "session.created"
    SESSION_DESTROYED = "session.destroyed"
    SESSION_BUSY = "session.busy"
    SESSION_IDLE = "session.idle"

    # Message events
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_QUEUED = "message.queued"
    MESSAGE_DEQUEUED = "message.dequeued"
    MESSAGE_PROCESSED = "message.processed"

    # Task events (for parallel sub-agent execution)
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"

    # Subagent events
    SUBAGENT_SPAWNED = "subagent.spawned"
    SUBAGENT_COMPLETED = "subagent.completed"
    SUBAGENT_ERROR = "subagent.error"

    # Context events
    CONTEXT_COMPACTED = "context.compacted"
    CONTEXT_OVERFLOW = "context.overflow"

    # System events
    CONFIG_CHANGED = "system.config_changed"
    SHUTDOWN = "system.shutdown"
    ERROR = "system.error"

    # Heartbeat event
    HEARTBEAT = "heartbeat"

    # Custom events
    CUSTOM = "custom"


class EventPriority(Enum):
    """Priority levels for event handlers."""

    LOW = 0
    NORMAL = 50
    HIGH = 100
    CRITICAL = 200


@dataclass
class Event:
    """An event that can be published and subscribed to.

    Attributes:
        type: The type of event
        data: Event-specific data payload
        source: Source of the event (e.g., agent name, tool name)
        session_id: Session this event belongs to (None for global events)
        timestamp: When the event was created
        id: Unique event identifier
        metadata: Additional metadata
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    session_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.type, str):
            try:
                self.type = EventType(self.type)
            except ValueError:
                self.type = EventType.CUSTOM
                self.metadata["original_type"] = self.type


@dataclass
class EventHandler:
    """A registered event handler.

    Attributes:
        id: Unique handler identifier
        callback: The handler function
        event_types: Types of events to handle (None = all)
        priority: Handler priority (higher = called first)
        session_filter: Only handle events from this session (None = all)
        is_async: Whether the callback is async
        once: Whether to remove after first call
        weak: Whether to use weak reference
    """

    id: str
    callback: Callable[[Event], Any] | Callable[[Event], Coroutine[Any, Any, Any]]
    event_types: set[EventType] | None = None
    priority: EventPriority = EventPriority.NORMAL
    session_filter: str | None = None
    is_async: bool = False
    once: bool = False
    weak: bool = False
    _weak_ref: weakref.ref | None = field(default=None, repr=False)

    def matches(self, event: Event) -> bool:
        """Check if this handler should handle the given event."""
        # Check event type
        if self.event_types is not None and event.type not in self.event_types:
            return False

        # Check session filter
        return not (self.session_filter is not None and event.session_id != self.session_filter)

    def get_callback(self) -> Callable | None:
        """Get the callback, resolving weak reference if necessary."""
        if self.weak and self._weak_ref is not None:
            return self._weak_ref()
        return self.callback


class EventBus:
    """Central event bus for publish/subscribe communication.

    Thread-safe, async-compatible event bus that supports:
    - Multiple handlers per event type
    - Priority-based handler ordering
    - Session-scoped event filtering
    - Weak references to prevent memory leaks
    - One-time handlers

    Example:
        bus = EventBus()

        # Subscribe with decorator
        @bus.on(EventType.TOOL_COMPLETED)
        async def handler(event):
            print(event.data)

        # Subscribe manually
        bus.subscribe(
            EventType.AGENT_STARTED,
            my_handler,
            priority=EventPriority.HIGH
        )

        # Publish event
        await bus.emit(Event(type=EventType.TOOL_COMPLETED, data={}))

        # Publish synchronously
        bus.emit_sync(Event(type=EventType.TOOL_COMPLETED, data={}))
    """

    def __init__(self):
        """Initialize the event bus."""
        self._handlers: list[EventHandler] = []
        self._lock = asyncio.Lock()
        self._emit_lock = asyncio.Lock()
        self._history: list[Event] = []
        self._max_history = 100

    def subscribe(
        self,
        event_types: EventType | list[EventType] | None = None,
        callback: Callable[[Event], Any] | None = None,
        priority: EventPriority = EventPriority.NORMAL,
        session_filter: str | None = None,
        once: bool = False,
        weak: bool = False,
    ) -> str:
        """Subscribe to events.

        Args:
            event_types: Types to subscribe to (None = all)
            callback: Handler function
            priority: Handler priority
            session_filter: Only receive events from this session
            once: Remove handler after first call
            weak: Use weak reference to callback

        Returns:
            Handler ID for unsubscribing
        """
        if callback is None:
            raise ValueError("callback is required")

        # Normalize event types
        if event_types is None:
            types_set = None
        elif isinstance(event_types, EventType):
            types_set = {event_types}
        else:
            types_set = set(event_types)

        # Check if callback is async
        is_async = asyncio.iscoroutinefunction(callback)

        # Create handler
        handler_id = str(uuid.uuid4())
        handler = EventHandler(
            id=handler_id,
            callback=callback,
            event_types=types_set,
            priority=priority,
            session_filter=session_filter,
            is_async=is_async,
            once=once,
            weak=weak,
        )

        # Set up weak reference if requested
        if weak:
            handler._weak_ref = weakref.ref(callback)
            handler.callback = None  # type: ignore

        # Add handler and sort by priority
        self._handlers.append(handler)
        self._handlers.sort(key=lambda h: h.priority.value, reverse=True)

        logger.debug(f"Subscribed handler {handler_id} to {event_types}")
        return handler_id

    def unsubscribe(self, handler_id: str) -> bool:
        """Unsubscribe a handler by ID.

        Args:
            handler_id: The handler ID returned from subscribe()

        Returns:
            True if handler was found and removed
        """
        for i, handler in enumerate(self._handlers):
            if handler.id == handler_id:
                del self._handlers[i]
                logger.debug(f"Unsubscribed handler {handler_id}")
                return True
        return False

    def on(
        self,
        event_types: EventType | list[EventType] | None = None,
        priority: EventPriority = EventPriority.NORMAL,
        session_filter: str | None = None,
        once: bool = False,
    ):
        """Decorator for subscribing to events.

        Example:
            @bus.on(EventType.TOOL_COMPLETED)
            async def handler(event):
                print(event.data)
        """

        def decorator(func: Callable[[Event], Any]):
            self.subscribe(
                event_types=event_types,
                callback=func,
                priority=priority,
                session_filter=session_filter,
                once=once,
            )
            return func

        return decorator

    async def emit(self, event: Event) -> list[Any]:
        """Emit an event to all matching handlers.

        Args:
            event: The event to emit

        Returns:
            List of results from handlers
        """
        async with self._emit_lock:
            return await self._emit_internal(event)

    async def _emit_internal(self, event: Event) -> list[Any]:
        """Internal emit implementation."""
        self._record_history(event)

        results = []
        handlers_to_remove: list[str] = []
        matched_handlers, dead_handlers = self._collect_matching_handlers(event)
        handlers_to_remove.extend(dead_handlers)

        for handler, callback in matched_handlers:
            try:
                if handler.is_async:
                    result = await callback(event)
                else:
                    result = callback(event)
                results.append(result)
            except Exception as e:
                logger.error(f"Error in event handler {handler.id}: {e}")
                results.append(None)

            if handler.once:
                handlers_to_remove.append(handler.id)

        # Remove one-time and dead handlers
        self._remove_handlers(handlers_to_remove)

        logger.debug(f"Emitted event {event.type.value} to {len(results)} handlers")
        return results

    def emit_sync(self, event: Event) -> None:
        """Emit an event synchronously (fire and forget for async handlers).

        Note: Async handlers will be scheduled but not awaited.
        For full async support, use emit() instead.

        Args:
            event: The event to emit
        """
        self._record_history(event)

        handlers_to_remove: list[str] = []
        matched_handlers, dead_handlers = self._collect_matching_handlers(event)
        handlers_to_remove.extend(dead_handlers)

        for handler, callback in matched_handlers:
            try:
                if handler.is_async:
                    # Schedule async handler without waiting
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(callback(event))
                    except RuntimeError:
                        # No running loop, run in new loop
                        asyncio.run(callback(event))
                else:
                    callback(event)
            except Exception as e:
                logger.error(f"Error in event handler {handler.id}: {e}")

            if handler.once:
                handlers_to_remove.append(handler.id)

        self._remove_handlers(handlers_to_remove)

    def _record_history(self, event: Event) -> None:
        """Store an event in bounded history."""
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

    def _collect_matching_handlers(
        self,
        event: Event,
    ) -> tuple[list[tuple[EventHandler, Callable[[Event], Any]]], list[str]]:
        """Collect handlers matching an event and callbacks that are still alive."""
        matched: list[tuple[EventHandler, Callable[[Event], Any]]] = []
        dead_handlers: list[str] = []

        for handler in self._handlers:
            if not handler.matches(event):
                continue

            callback = handler.get_callback()
            if callback is None:
                dead_handlers.append(handler.id)
                continue

            matched.append((handler, callback))

        return matched, dead_handlers

    def _remove_handlers(self, handler_ids: list[str]) -> None:
        """Remove handlers by id."""
        for handler_id in handler_ids:
            self.unsubscribe(handler_id)

    def clear(self) -> None:
        """Remove all handlers."""
        self._handlers.clear()
        logger.debug("Cleared all event handlers")

    def clear_session(self, session_id: str) -> int:
        """Remove all handlers for a specific session.

        Args:
            session_id: Session to clear handlers for

        Returns:
            Number of handlers removed
        """
        original_count = len(self._handlers)
        self._handlers = [h for h in self._handlers if h.session_filter != session_id]
        removed = original_count - len(self._handlers)
        logger.debug(f"Cleared {removed} handlers for session {session_id}")
        return removed

    def get_history(
        self,
        event_type: EventType | None = None,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Get event history with optional filtering.

        Args:
            event_type: Filter by event type
            session_id: Filter by session
            limit: Maximum number of events to return

        Returns:
            List of matching events (newest first)
        """
        events = list(reversed(self._history))

        if event_type is not None:
            events = [e for e in events if e.type == event_type]

        if session_id is not None:
            events = [e for e in events if e.session_id == session_id]

        if limit is not None:
            events = events[:limit]

        return events

    def handler_count(self, event_type: EventType | None = None) -> int:
        """Get the number of registered handlers.

        Args:
            event_type: Count only handlers for this type

        Returns:
            Number of handlers
        """
        if event_type is None:
            return len(self._handlers)

        return sum(1 for h in self._handlers if h.event_types is None or event_type in h.event_types)

    def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics."""
        return {
            "total_handlers": len(self._handlers),
            "history_size": len(self._history),
            "max_history": self._max_history,
            "handlers_by_priority": {p.name: sum(1 for h in self._handlers if h.priority == p) for p in EventPriority},
        }


# Global event bus singleton
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance.

    Returns:
        Global EventBus singleton
    """
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def reset_event_bus() -> None:
    """Reset the global event bus (mainly for testing)."""
    global _event_bus
    if _event_bus is not None:
        _event_bus.clear()
    _event_bus = None


# Convenience functions for common events


async def emit_agent_started(
    agent_name: str,
    session_id: str,
    **data: Any,
) -> None:
    """Emit an agent started event."""
    await get_event_bus().emit(
        Event(
            type=EventType.AGENT_STARTED,
            source=agent_name,
            session_id=session_id,
            data={"agent_name": agent_name, **data},
        )
    )


async def emit_agent_completed(
    agent_name: str,
    session_id: str,
    result: str,
    **data: Any,
) -> None:
    """Emit an agent completed event."""
    await get_event_bus().emit(
        Event(
            type=EventType.AGENT_COMPLETED,
            source=agent_name,
            session_id=session_id,
            data={"agent_name": agent_name, "result": result, **data},
        )
    )


async def emit_tool_started(
    tool_name: str,
    session_id: str | None = None,
    arguments: dict[str, Any] | None = None,
    **data: Any,
) -> None:
    """Emit a tool started event."""
    await get_event_bus().emit(
        Event(
            type=EventType.TOOL_STARTED,
            source=tool_name,
            session_id=session_id,
            data={"tool_name": tool_name, "arguments": arguments or {}, **data},
        )
    )


async def emit_tool_completed(
    tool_name: str,
    session_id: str | None = None,
    result: Any = None,
    duration_ms: float | None = None,
    **data: Any,
) -> None:
    """Emit a tool completed event."""
    await get_event_bus().emit(
        Event(
            type=EventType.TOOL_COMPLETED,
            source=tool_name,
            session_id=session_id,
            data={
                "tool_name": tool_name,
                "result": result,
                "duration_ms": duration_ms,
                **data,
            },
        )
    )


async def emit_tool_error(
    tool_name: str,
    error: str,
    session_id: str | None = None,
    **data: Any,
) -> None:
    """Emit a tool error event."""
    await get_event_bus().emit(
        Event(
            type=EventType.TOOL_ERROR,
            source=tool_name,
            session_id=session_id,
            data={"tool_name": tool_name, "error": error, **data},
        )
    )


async def emit_task_event(
    event_type: EventType,
    task_id: str,
    task_description: str,
    session_id: str | None = None,
    **data: Any,
) -> None:
    """Emit a task event."""
    event = Event(
        type=event_type,
        source=f"task:{task_id}",
        session_id=session_id,
        data={"task_id": task_id, "description": task_description, **data},
    )
    await get_event_bus().emit(event)
    if session_id:
        try:
            from .session_store import SessionTimelineStore

            SessionTimelineStore(
                CONFIG_DIR / "sessions",
                session_id,
            ).append(event_type.value, event.data, timestamp=event.timestamp.isoformat())
        except Exception as exc:
            logger.debug("Task timeline persistence failed (non-critical): %s", exc)
