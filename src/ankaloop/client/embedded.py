"""Embedded client for AnkaLoop.

Runs agents locally without requiring a server.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from rich.console import Console

from ..application_services import ApplicationServices
from .base import BaseClient, ResponseChunk
from .exceptions import SessionError, SessionNotFoundError


class EmbeddedSession:
    """Embedded session data."""

    def __init__(
        self,
        session_id: str,
        agent: Any,  # Agent type
        cwd: str,
        agent_name: str,
        services: ApplicationServices,
    ):
        """Initialize the embedded session.

        Args:
            session_id: Session ID.
            agent: Agent instance.
            cwd: Working directory.
            agent_name: Agent name.
        """
        self.id = session_id
        self.agent = agent
        self.cwd = cwd
        self.agent_name = agent_name
        self.created_at = datetime.now()
        self.services = services

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        status = self.agent.get_queue_status()["status"]
        metrics = self.services.session_service.metrics_for(self.agent)
        return {
            "id": self.id,
            "cwd": self.cwd,
            "agent_name": self.agent_name,
            "created_at": self.created_at.isoformat(),
            "updated_at": (metrics.updated_at or self.created_at).isoformat(),
            "status": status,
            "message_count": metrics.message_count,
        }


class EmbeddedClient(BaseClient):
    """Embedded client that runs agents locally.

    This provides the same interface as remote clients but runs
    agents directly without a server.

    Example:
        async with EmbeddedClient() as client:
            session = await client.create_session()
            async for chunk in await client.prompt(session["id"], "Hello"):
                print(chunk.content)
    """

    def __init__(
        self,
        services: ApplicationServices | None = None,
        *,
        console: Console | None = None,
    ):
        """Initialize the embedded client.

        Args:
            services: Shared application services; defaults to the global set.
            console: Console for agent diagnostics. Pass a stderr console when
                stdout carries a protocol stream (for example ACP).
        """
        self.services = services or ApplicationServices.default()
        self._console = console
        self._sessions: dict[str, EmbeddedSession] = {}
        self._closing_sessions: set[str] = set()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if connected (always True after connect())."""
        return bool(self._connected)

    async def connect(self) -> None:
        """Initialize the embedded client."""
        self._connected = True

    async def close(self) -> None:
        """Cleanup resources."""
        for session in self._sessions.values():
            await session.agent.close()
            self.services.session_service.forget(session.agent)
        self._sessions.clear()
        self._connected = False

    # =========================================================================
    # Health & Info
    # =========================================================================

    async def health(self) -> dict[str, Any]:
        """Return health status.

        Returns:
            Health status dictionary.
        """
        from .. import __version__

        return {
            "healthy": True,
            "version": __version__,
            "mode": "embedded",
            "uptime_seconds": 0,
        }

    async def info(self) -> dict[str, Any]:
        """Get client information.

        Returns:
            Info dictionary.
        """
        from .. import __version__

        registry = self.services.agent_registry
        return {
            "name": "ankaloop-embedded",
            "version": __version__,
            "mode": "embedded",
            "capabilities": ["sessions", "streaming", "tools", "agents"],
            "agents": registry.list_agents(),
        }

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
            session_id: Optional specific session ID.

        Returns:
            Session data dictionary.
        """
        from ..agent import Agent, create_agent_by_name
        from ..agent_spec import get_default_agent_spec

        # Generate session ID
        sid = session_id or str(uuid.uuid4())[:8]
        if sid in self._sessions or sid in self._closing_sessions:
            raise SessionError(f"Session already exists: {sid}")

        # Resolve working directory
        work_dir = cwd or os.getcwd()

        # Create agent
        if agent_name:
            registry = self.services.agent_registry
            if agent_name in registry.list_agents():
                agent = create_agent_by_name(
                    agent_name,
                    session_id=sid,
                    services=self.services,
                    console=self._console,
                )
                resolved_name = agent_name
            else:
                # Try as agent spec file
                from pathlib import Path

                from ..agent_spec import load_agent_spec

                try:
                    spec = load_agent_spec(Path(agent_name))
                    agent = Agent(
                        spec,
                        session_id=sid,
                        services=self.services,
                        console=self._console,
                    )
                    resolved_name = spec.name
                except Exception:
                    # Fall back to default
                    spec = get_default_agent_spec()
                    agent = Agent(
                        spec,
                        session_id=sid,
                        services=self.services,
                        console=self._console,
                    )
                    resolved_name = spec.name
        else:
            spec = get_default_agent_spec()
            agent = Agent(
                spec,
                session_id=sid,
                services=self.services,
                console=self._console,
            )
            resolved_name = spec.name

        # Create session
        session = EmbeddedSession(
            session_id=sid,
            agent=agent,
            cwd=work_dir,
            agent_name=resolved_name,
            services=self.services,
        )
        self._sessions[sid] = session

        return session.to_dict()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Get session information.

        Args:
            session_id: The session ID.

        Returns:
            Session data dictionary.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        if session_id not in self._sessions:
            raise SessionNotFoundError(session_id)
        return self._sessions[session_id].to_dict()

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions.

        Returns:
            List of session data dictionaries.
        """
        return [session.to_dict() for session in self._sessions.values()]

    async def delete_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session ID.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        if session_id not in self._sessions:
            raise SessionNotFoundError(session_id)
        if session_id in self._closing_sessions:
            raise SessionError(f"Session is already closing: {session_id}")
        session = self._sessions[session_id]
        self._closing_sessions.add(session_id)
        try:
            await session.agent.close()
        finally:
            self._sessions.pop(session_id, None)
            self._closing_sessions.discard(session_id)
            self.services.session_service.forget(session.agent)

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
            priority: Message priority (ignored in embedded mode).

        Returns:
            If stream=False, returns the complete response string.
            If stream=True, returns an async iterator of ResponseChunks.
        """
        if session_id not in self._sessions:
            raise SessionNotFoundError(session_id)

        session = self._sessions[session_id]

        if stream:
            return self._stream_prompt(session, content)
        else:
            return await self._non_stream_prompt(session, content)

    async def _non_stream_prompt(
        self,
        session: EmbeddedSession,
        content: str,
    ) -> str:
        """Send a non-streaming prompt.

        Args:
            session: The session.
            content: The prompt content.

        Returns:
            Complete response string.
        """
        from pathlib import Path

        try:
            work_dir = Path(session.cwd) if session.cwd else None
            response = await self.services.session_service.run(
                session.agent,
                content,
                work_dir=work_dir,
                stream=False,
                show_progress=False,
            )
            return response if isinstance(response, str) else ""
        except Exception as e:
            raise SessionError(f"Prompt failed: {e}", session_id=session.id) from e

    async def _stream_prompt(
        self,
        session: EmbeddedSession,
        content: str,
    ) -> AsyncIterator[ResponseChunk]:
        """Stream a prompt response.

        Args:
            session: The session.
            content: The prompt content.

        Yields:
            ResponseChunk objects.
        """
        from pathlib import Path

        try:
            work_dir = Path(session.cwd) if session.cwd else None

            response = await self.services.session_service.run(
                session.agent,
                content,
                work_dir=work_dir,
                stream=True,
                show_progress=False,
            )
            yield ResponseChunk(
                content=response,
                chunk_type="text",
                done=False,
            )

            # Final completion chunk
            yield ResponseChunk(
                content="",
                chunk_type="complete",
                done=True,
            )

        except Exception as e:
            yield ResponseChunk(
                content=str(e),
                chunk_type="error",
                done=True,
            )

    async def cancel(self, session_id: str, *, force: bool = False) -> None:
        """Cancel the current operation.

        Args:
            session_id: The session ID.
            force: Whether to force cancellation.

        Raises:
            SessionNotFoundError: If session doesn't exist.
        """
        if session_id not in self._sessions:
            raise SessionNotFoundError(session_id)
        await self._sessions[session_id].agent.cancel(clear_queue=force)

    # =========================================================================
    # Tools & Agents
    # =========================================================================

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools.

        Returns:
            List of tool info dictionaries.
        """
        from ..tools import (
            ApplyPatchTool,
            BaseTool,
            BashTool,
            GrepTool,
            ReadFileTool,
            ThinkTool,
            TodoTool,
            WriteFileTool,
        )

        # Instantiate tools to get their specs
        tool_classes: list[type[BaseTool]] = [
            ReadFileTool,
            WriteFileTool,
            BashTool,
            GrepTool,
            ApplyPatchTool,
            ThinkTool,
            TodoTool,
        ]

        tools: list[dict[str, Any]] = []
        for tool_cls in tool_classes:
            try:
                tool = tool_cls()
                tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description[:100] if tool.description else "",
                        "source": "builtin",
                    }
                )
            except Exception:
                continue
        return tools

    async def list_agents(self) -> list[dict[str, Any]]:
        """List available agents.

        Returns:
            List of agent info dictionaries.
        """
        from ..multi_agent import get_agent_registry

        registry = get_agent_registry()
        agents = []
        for name in registry.list_agents():
            config = registry.get(name)
            if config:
                agents.append(
                    {
                        "name": config.name,
                        "description": config.description,
                        "mode": config.mode.value,
                    }
                )
        return agents
