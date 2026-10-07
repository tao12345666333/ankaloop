"""Session manager for AnkaLoop Server.

Manages multiple agent sessions for concurrent client connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agent import Agent
from ..agent_spec import get_default_agent_spec
from ..application_services import ApplicationServices
from ..application_session import ApplicationSessionService
from ..runtime import TurnEvent, TurnStatus
from .config import ServerConfig, get_server_config
from .models import Session, SessionStatus, TokenUsage


class SessionNotFoundError(Exception):
    """Raised when a session is not found."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")


class SessionBusyError(Exception):
    """Raised when a session is busy."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session is busy: {session_id}")


class SessionAlreadyExistsError(Exception):
    """Raised when a live owner already exists for a session ID."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session already exists: {session_id}")


class MaxSessionsReachedError(Exception):
    """Raised when maximum sessions limit is reached."""

    def __init__(self, max_sessions: int):
        self.max_sessions = max_sessions
        super().__init__(f"Maximum sessions limit reached: {max_sessions}")


class ManagedSession:
    """A managed session with agent and metadata."""

    def __init__(
        self,
        session_id: str,
        agent: Agent,
        cwd: str,
        agent_name: str,
        session_service: ApplicationSessionService,
    ):
        self.id = session_id
        self.agent = agent
        self.cwd = cwd
        self.agent_name = agent_name
        self.session_service = session_service
        self.created_at = datetime.now()
        self.metrics = session_service.metrics_for(agent)
        self.status = SessionStatus.IDLE

    def to_session(self) -> Session:
        """Convert to Session model."""
        runtime_status = self.agent.get_queue_status()["status"]
        self.status = SessionStatus(runtime_status)
        metrics = self.session_service.metrics_for(self.agent)
        return Session(
            id=self.id,
            created_at=self.created_at,
            updated_at=metrics.updated_at or self.created_at,
            cwd=self.cwd,
            agent_name=self.agent_name,
            status=self.status,
            message_count=metrics.message_count,
            token_usage=TokenUsage(
                prompt_tokens=metrics.prompt_tokens,
                completion_tokens=metrics.completion_tokens,
                total_tokens=metrics.total_tokens,
            ),
            queued_count=self.agent.queued_count(),
        )

    def update_status(self, status: SessionStatus) -> None:
        """Update session status."""
        self.status = status
        self.metrics.status = status.value
        self.metrics.updated_at = datetime.now()

    def add_token_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Add token usage."""
        self.metrics.prompt_tokens += prompt_tokens
        self.metrics.completion_tokens += completion_tokens
        self.metrics.total_tokens = self.metrics.prompt_tokens + self.metrics.completion_tokens
        self.metrics.updated_at = datetime.now()

    @property
    def updated_at(self) -> datetime:
        """Return the application-managed update time."""
        return self.metrics.updated_at or self.created_at

    @property
    def message_count(self) -> int:
        """Return the number of successfully completed turns."""
        return self.metrics.message_count

    @message_count.setter
    def message_count(self, value: int) -> None:
        self.metrics.message_count = value

    @property
    def token_usage(self) -> TokenUsage:
        """Return current token usage in the server response model."""
        return TokenUsage(
            prompt_tokens=self.metrics.prompt_tokens,
            completion_tokens=self.metrics.completion_tokens,
            total_tokens=self.metrics.total_tokens,
        )


class SessionManager:
    """Manages multiple agent sessions.

    Thread-safe manager for creating, accessing, and destroying sessions.
    Each session maintains its own Agent instance and conversation history.
    """

    def __init__(
        self,
        config: ServerConfig | None = None,
        services: ApplicationServices | None = None,
    ):
        self.config = config or get_server_config()
        self.services = services or ApplicationServices.default()
        self.session_service = self.services.session_service
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = asyncio.Lock()
        self._event_listeners: list[Callable[[str, Any], None]] = []

    @property
    def session_count(self) -> int:
        """Get the number of active sessions."""
        return len(self._sessions)

    async def create_session(
        self,
        cwd: str | None = None,
        agent_name: str | None = None,
        session_id: str | None = None,
    ) -> ManagedSession:
        """Create a new session.

        Args:
            cwd: Working directory for the session. Defaults to config work_dir or cwd.
            agent_name: Name of the agent to use. Defaults to config default_agent.
            session_id: Optional session ID. Auto-generated if not provided.

        Returns:
            The created ManagedSession.

        Raises:
            MaxSessionsReachedError: If maximum sessions limit is reached.
        """
        async with self._lock:
            # Generate session ID
            if session_id is None:
                session_id = self._generate_session_id()
            elif session_id in self._sessions:
                raise SessionAlreadyExistsError(session_id)

            if len(self._sessions) >= self.config.max_sessions:
                raise MaxSessionsReachedError(self.config.max_sessions)

            # Resolve working directory
            if cwd is None:
                cwd = str(self.config.work_dir or Path.cwd())

            # Resolve agent
            agent_name = agent_name or self.config.default_agent
            agent_spec = self._resolve_agent_spec(agent_name)

            # Create agent instance
            agent = Agent(
                agent_spec=agent_spec,
                session_id=session_id,
                services=self.services,
            )
            agent.execution_context["source"] = "server"

            # Create managed session
            session = ManagedSession(
                session_id=session_id,
                agent=agent,
                cwd=cwd,
                agent_name=agent_name,
                session_service=self.session_service,
            )

            # Connect Agent events to EventBridge
            try:
                from .event_bridge import emit_tool_event

                def agent_event_handler(event_type: str, data: dict[str, Any]):
                    # Run async emission in background (fire and forget)
                    import asyncio

                    try:
                        loop = asyncio.get_running_loop()
                        if event_type == "tool.call_start":
                            loop.create_task(
                                emit_tool_event(
                                    session_id=data["session_id"],
                                    event_type="start",
                                    tool_name=data["tool_name"],
                                    arguments=data.get("arguments", {}),
                                )
                            )
                        elif event_type == "tool.call_complete":
                            loop.create_task(
                                emit_tool_event(
                                    session_id=data["session_id"],
                                    event_type="complete",
                                    tool_name=data["tool_name"],
                                    result="[Result omitted]",  # Result might be large
                                    duration_ms=data.get("duration_ms"),
                                )
                            )
                        elif event_type == "tool.call_error":
                            loop.create_task(
                                emit_tool_event(
                                    session_id=data["session_id"],
                                    event_type="error",
                                    tool_name=data["tool_name"],
                                    error=data.get("error", "Unknown error"),
                                    duration_ms=data.get("duration_ms"),
                                )
                            )
                        elif event_type == "message.chunk":
                            from .event_bridge import get_event_bridge

                            bridge = get_event_bridge()
                            loop.create_task(
                                bridge.emit_message_chunk(
                                    session_id=data["session_id"],
                                    content=data["content"],
                                )
                            )
                    except RuntimeError:
                        pass

                agent.add_event_callback(agent_event_handler)
            except Exception:
                pass  # Don't fail session creation if event hook fails

            self._sessions[session_id] = session
            self._emit_event("session.created", {"session_id": session_id})

            return session

    async def get_session(self, session_id: str) -> ManagedSession:
        """Get a session by ID.

        Args:
            session_id: The session ID.

        Returns:
            The ManagedSession.

        Raises:
            SessionNotFoundError: If session is not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    async def list_sessions(self) -> list[Session]:
        """List all sessions.

        Returns:
            List of Session models.
        """
        return [s.to_session() for s in self._sessions.values()]

    async def delete_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session ID.

        Raises:
            SessionNotFoundError: If session is not found.
        """
        async with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFoundError(session_id)

            session = self._sessions.pop(session_id)
            await session.agent.close()
            session.agent.delete_persisted_session()
            self.session_service.forget(session.agent)
            self._emit_event("session.deleted", {"session_id": session_id})

    async def submit_prompt(
        self,
        session_id: str,
        content: str,
        work_dir: Path | None = None,
        stream: bool = True,
        priority: str = "normal",
        *,
        reject_if_busy: bool = False,
    ):
        """Submit a prompt through the session's sole runtime owner."""
        session = await self.get_session(session_id)
        return await self.session_service.submit(
            session.agent,
            content,
            work_dir=work_dir or Path(session.cwd),
            stream=stream,
            show_progress=False,
            priority=priority,
            reject_if_busy=reject_if_busy,
            on_event=lambda event: self._on_turn_event(session, event),
        )

    def _on_turn_event(self, session: ManagedSession, event: TurnEvent) -> None:
        """Project canonical turn lifecycle into server session events."""
        if event.status == TurnStatus.RUNNING:
            status = SessionStatus.BUSY
        elif event.status in {
            TurnStatus.COMPLETED,
            TurnStatus.CANCELLED,
            TurnStatus.FAILED,
        }:
            handle = session.agent.get_turn(event.turn_id)
            if session.agent.has_pending_work(excluding=handle):
                status = SessionStatus.BUSY
            elif event.status == TurnStatus.COMPLETED:
                status = SessionStatus.IDLE
            elif event.status == TurnStatus.CANCELLED:
                status = SessionStatus.CANCELLED
            else:
                status = SessionStatus.ERROR
        else:
            return
        session.update_status(status)
        self._emit_event(
            "session.status_changed",
            {
                "session_id": session.id,
                "status": status.value,
                "turn_id": event.turn_id,
            },
        )

    async def prompt_session(
        self,
        session_id: str,
        content: str,
        work_dir: Path | None = None,
        stream: bool = True,
        priority: str = "normal",
    ):
        """Send a prompt to a session.

        Args:
            session_id: The session ID.
            content: The prompt content.
            work_dir: Optional working directory override.
            stream: Whether to stream the response.
            priority: Message priority.

        Yields:
            Response chunks from the agent.

        Raises:
            SessionNotFoundError: If session is not found.
        """
        handle = await self.submit_prompt(
            session_id,
            content,
            work_dir=work_dir,
            stream=stream,
            priority=priority,
        )
        try:
            yield await handle.wait()
        except Exception as e:
            self._emit_event("session.error", {"session_id": session_id, "error": str(e)})
            raise

    async def cancel_session(self, session_id: str, force: bool = False) -> None:
        """Cancel the current operation in a session.

        Args:
            session_id: The session ID.
            force: Whether to force cancellation.

        Raises:
            SessionNotFoundError: If session is not found.
        """
        session = await self.get_session(session_id)
        await session.agent.cancel(clear_queue=force)
        session.update_status(SessionStatus.CANCELLED)

        self._emit_event(
            "session.cancelled",
            {"session_id": session_id, "force": force},
        )

    def add_event_listener(self, listener: Callable[[str, Any], None]) -> None:
        """Add an event listener.

        Args:
            listener: Callback function receiving (event_type, payload).
        """
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[str, Any], None]) -> None:
        """Remove an event listener.

        Args:
            listener: The listener to remove.
        """
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit an event to all listeners and broadcast to connected clients.

        Args:
            event_type: Type of event.
            payload: Event payload.
        """
        # Emit to local listeners
        for listener in self._event_listeners:
            with contextlib.suppress(Exception):
                listener(event_type, payload)

        # Also broadcast to WebSocket/SSE clients asynchronously
        try:
            import asyncio

            from .event_bridge import get_event_bridge

            bridge = get_event_bridge()
            session_id = payload.get("session_id")

            # Create async task to broadcast (non-blocking)
            async def broadcast():
                if event_type == "session.status_changed":
                    await bridge.emit_session_status_changed(
                        session_id,
                        payload.get("status", "unknown"),
                        payload,
                    )
                elif event_type == "session.created":
                    await bridge.emit_session_status_changed(
                        session_id,
                        "created",
                        payload,
                    )
                elif event_type == "session.deleted":
                    await bridge.emit_session_status_changed(
                        session_id,
                        "deleted",
                        payload,
                    )
                elif event_type == "session.error":
                    await bridge.emit_session_status_changed(
                        session_id,
                        "error",
                        payload,
                    )

            # Schedule the broadcast
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(broadcast())
            except RuntimeError:
                # No running loop, skip async broadcast
                pass
        except ImportError:
            pass  # EventBridge not available

    def _generate_session_id(self) -> str:
        """Generate a unique session ID."""
        return f"session-{uuid.uuid4().hex[:12]}"

    def _resolve_agent_spec(self, agent_name: str):
        """Resolve agent specification by name.

        Args:
            agent_name: Name of the agent.

        Returns:
            Resolved agent specification.
        """
        # Try to get from registry
        registry = self.services.agent_registry
        agent_config = registry.get(agent_name)

        if agent_config:
            # Create a ResolvedAgentSpec from AgentConfig
            from ..agent_spec import AgentMode as SpecAgentMode
            from ..agent_spec import ResolvedAgentSpec

            # Map multi_agent AgentMode to agent_spec AgentMode
            mode_map = {
                "primary": SpecAgentMode.PRIMARY,
                "subagent": SpecAgentMode.SUBAGENT,
            }
            spec_mode = mode_map.get(agent_config.mode.value, SpecAgentMode.PRIMARY)

            return ResolvedAgentSpec(
                name=agent_config.name,
                description=agent_config.description,
                mode=spec_mode,
                system_prompt=agent_config.system_prompt,
                max_steps=agent_config.max_steps,
                tools=agent_config.tools,
                exclude_tools=agent_config.excluded_tools,
                model="",
                base_url="",
                can_delegate=agent_config.can_delegate,
            )

        # Fall back to default
        return get_default_agent_spec()


# Global session manager instance
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Get the global session manager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


def set_session_manager(manager: SessionManager) -> None:
    """Set the global session manager."""
    global _session_manager
    _session_manager = manager


def reset_session_manager() -> None:
    """Reset the global session manager."""
    global _session_manager
    _session_manager = None
