"""Serve AnkaLoop as an Agent Client Protocol (ACP) agent.

The ACP surface is a thin translation layer over :class:`EmbeddedClient`:
every ACP session maps to one embedded session, and every turn event the
runtime already publishes is forwarded to the client as a ``session/update``.
No conversation state lives here; the session store remains the source of
truth, which keeps ``session/load`` and live turns on the same replay path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp import (
    Agent,
    Client,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    RequestError,
    run_agent,
)
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    EmbeddedResource,
    ImageContentBlock,
    Implementation,
    ListSessionsResponse,
    PromptCapabilities,
    ResourceLink,
    SessionCapabilities,
    SessionInfo,
    SessionListCapabilities,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolKind,
    Usage,
    UsageUpdate,
    UserMessageChunk,
)

from .. import __version__
from ..client.embedded import EmbeddedClient
from ..constants import CONFIG_DIR
from ..runtime import TurnEvent
from ..session_store import SessionStore

if TYPE_CHECKING:
    from acp.schema import (
        AudioContentBlock,
        EmbeddedResourceContentBlock,
        ImageContentBlock,
        ResourceContentBlock,
    )
    from acp.schema import TextContentBlock as TextBlock

    from ..runtime import TurnHandle

    ContentBlock = (
        TextBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
    )

logger = logging.getLogger(__name__)


def _session_meta_path() -> Path:
    """ACP-only metadata (title, cwd, last use) for ``session/list``."""
    return CONFIG_DIR / "acp-sessions.json"


_EVENT_QUEUE_BYTES = 4 * 1024 * 1024
"""Per-turn event buffer; output is coalesced before it reaches the client."""

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

_FLUSH_INTERVAL = 0.1
"""Seconds between streaming flushes of buffered model output."""


@dataclass(slots=True)
class _SessionRecord:
    """One ACP session and the embedded session backing it."""

    cwd: str
    title: str | None = None
    updated_at: str | None = None


class _TurnStream:
    """Translate one turn's runtime events into ACP session updates."""

    def __init__(self, agent: ACPSessionAgent, session_id: str) -> None:
        self._agent = agent
        self._session_id = session_id
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._input_tokens = 0
        self._output_tokens = 0
        self._context_window = 0
        self._reported_usage: tuple[int, int] | None = None
        self._last_flush = 0.0

    async def run(self, handle: TurnHandle) -> None:
        """Publish updates until the turn reaches a terminal state."""
        queue = handle.subscribe(max_bytes=_EVENT_QUEUE_BYTES)
        try:
            while True:
                event = await queue.get()
                await self.publish(event)
                if event.status is not None and event.status.value in _TERMINAL_STATUSES:
                    return
        finally:
            handle.unsubscribe(queue)
            with contextlib.suppress(Exception):
                await self.flush()

    async def publish(self, event: TurnEvent) -> None:
        """Translate one runtime event into ACP session updates."""
        if event.type == "message.chunk":
            self._text.append(str(event.data.get("content", "")))
            await self._flush_if_due()
        elif event.type == "message.reasoning":
            self._reasoning.append(str(event.data.get("content", "")))
            await self._flush_if_due()
        elif event.type.startswith("turn.") and event.status is not None:
            pass  # The final flush and the prompt response carry the outcome.
        elif event.type == "tool.call_start":
            await self.flush()
            name = str(event.data.get("tool_name", "tool"))
            await self._agent.send_update(
                self._session_id,
                ToolCallStart(
                    tool_call_id=_tool_call_id(event),
                    title=_tool_title(name, event.data.get("arguments")),
                    name=name,
                    kind=_tool_kind(name),
                    status="in_progress",
                    raw_input=event.data.get("arguments"),
                ),
            )
        elif event.type.startswith("tool.call_"):
            await self.flush()
            name = str(event.data.get("tool_name", "tool"))
            completed = event.type == "tool.call_complete"
            await self._agent.send_update(
                self._session_id,
                ToolCallProgress(
                    tool_call_id=_tool_call_id(event),
                    title=name,
                    name=name,
                    kind=_tool_kind(name),
                    status="completed" if completed else "failed",
                    raw_output=event.data.get("error") if not completed else None,
                ),
            )
        elif event.type == "llm.usage":
            await self._publish_usage(event.data)

    async def _flush_if_due(self) -> None:
        """Stream buffered text at a steady pace instead of one chunk per delta."""
        if monotonic() - self._last_flush >= _FLUSH_INTERVAL:
            await self.flush()

    async def flush(self, *, text: bool = True, reasoning: bool = True) -> None:
        """Send buffered model output before any non-delta update."""
        # Reasoning first: the model thought before it answered.
        if reasoning and self._reasoning:
            content = "".join(self._reasoning)
            self._reasoning.clear()
            await self._agent.send_update(self._session_id, AgentThoughtChunk(content=TextContentBlock(text=content)))
        self._last_flush = monotonic()
        if text and self._text:
            content = "".join(self._text)
            self._text.clear()
            await self._agent.send_update(self._session_id, AgentMessageChunk(content=TextContentBlock(text=content)))

    async def _publish_usage(self, data: dict[str, Any]) -> None:
        """Report context occupancy: the latest prompt plus its output."""
        input_tokens = data.get("input_tokens")
        output_tokens = data.get("output_tokens")
        if isinstance(input_tokens, int):
            self._input_tokens = input_tokens
        if isinstance(output_tokens, int):
            self._output_tokens = output_tokens
        window = data.get("context_window")
        if isinstance(window, int):
            self._context_window = window
        used = self._input_tokens + self._output_tokens
        size = max(self._context_window, used)
        if used == 0 or (used, size) == self._reported_usage:
            return
        self._reported_usage = (used, size)
        await self._agent.send_update(self._session_id, UsageUpdate(used=used, size=size))

    @property
    def usage(self) -> Usage:
        """Token usage of the turn's latest model call."""
        return Usage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            total_tokens=self._input_tokens + self._output_tokens,
        )


# The SDK declares ``Agent`` as a Protocol, so subclassing it to inherit the
# request decorators trips mypy's abstract check; at runtime nothing is abstract.
class ACPSessionAgent(Agent):  # type: ignore[abstract]
    """ACP agent that runs AnkaLoop's embedded client."""

    def __init__(self, *, agent_name: str | None = None, client: EmbeddedClient | None = None):
        self._agent_name = agent_name
        self._client = client or EmbeddedClient()
        self._conn: Client | None = None
        self._sessions: dict[str, _SessionRecord] = _load_session_meta()
        self._active: dict[str, TurnHandle] = {}
        self._cancel_requested: set[str] = set()

    def on_connect(self, conn: Client) -> None:
        """Remember the client connection used for session updates."""
        self._conn = conn

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        """Advertise the capabilities this adapter implements."""
        del client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=Implementation(name="ankaloop", title="AnkaLoop", version=__version__),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(image=True, embedded_context=True),
                session_capabilities=SessionCapabilities(list=SessionListCapabilities()),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        """Create an embedded session in the client's working directory."""
        del additional_directories, mcp_servers, kwargs
        session_id = uuid4().hex
        await self._client.connect()
        await self._client.create_session(cwd=cwd, agent_name=self._agent_name, session_id=session_id)
        self._sessions[session_id] = _SessionRecord(cwd=cwd, updated_at=_now())
        _save_session_meta(self._sessions)
        return NewSessionResponse(session_id=session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Any = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        """Reattach to a stored session and replay its history to the client."""
        del mcp_servers, additional_directories, kwargs
        if not await self._attach(session_id, cwd):
            raise RequestError.resource_not_found(f"session {session_id}")
        for message in self._client._sessions[session_id].agent.conversation_history:
            content = _message_text(message.get("content"))
            if not content:
                continue
            if message.get("role") == "user":
                await self.send_update(session_id, UserMessageChunk(content=TextContentBlock(text=content)))
            elif message.get("role") == "assistant":
                await self.send_update(session_id, AgentMessageChunk(content=TextContentBlock(text=content)))
        self._sessions[session_id].updated_at = _now()
        _save_session_meta(self._sessions)
        return LoadSessionResponse()

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        """List ACP sessions that still have a stored transcript."""
        del cursor, kwargs
        root = _normalize(cwd) if cwd else None
        sessions = []
        for session_id, record in sorted(
            self._sessions.items(), key=lambda item: item[1].updated_at or "", reverse=True
        ):
            if root is not None and _normalize(record.cwd) != root:
                continue
            if not (_session_meta_path().parent / "sessions" / f"{session_id}.json").exists():
                continue
            sessions.append(
                SessionInfo(
                    session_id=session_id,
                    cwd=record.cwd,
                    title=record.title,
                    updated_at=record.updated_at,
                )
            )
        return ListSessionsResponse(sessions=sessions)

    async def prompt(
        self,
        session_id: str,
        prompt: list[ContentBlock],
        **kwargs: Any,
    ) -> PromptResponse:
        """Run one turn and stream it to the client."""
        del kwargs
        record = self._sessions.get(session_id)
        if record is None or not await self._attach(session_id, record.cwd):
            raise RequestError.invalid_params({"sessionId": session_id})
        self._cancel_requested.discard(session_id)
        content = _prompt_text(prompt)
        if record.title is None and content.strip():
            record.title = _title_from(content)
        record.updated_at = _now()
        _save_session_meta(self._sessions)

        handle = await self._client.services.session_service.submit(
            self._client._sessions[session_id].agent,
            content,
            work_dir=Path(record.cwd),
            stream=True,
            show_progress=False,
        )
        self._active[session_id] = handle
        stream = _TurnStream(self, session_id)
        relay = asyncio.create_task(stream.run(handle))
        relay.add_done_callback(_consume_exception)
        try:
            await handle.wait()
        except asyncio.CancelledError:
            # `session/cancel` arrives as a notification; only that turns into
            # a cancelled response, while shutdown still propagates.
            task = asyncio.current_task()
            if not self._cancel_requested or task is None or task.uncancel() > 0:
                raise
            await _finish(relay)
            return PromptResponse(stop_reason="cancelled", usage=stream.usage)
        except Exception as exc:
            logger.info("ACP turn failed for session %s: %s", session_id, exc)
            await _finish(relay)
            await self.send_update(session_id, AgentMessageChunk(content=TextContentBlock(text=f"\nError: {exc}")))
            return PromptResponse(stop_reason="refusal", usage=stream.usage)
        finally:
            self._active.pop(session_id, None)
        await _finish(relay)
        if session_id in self._cancel_requested:
            return PromptResponse(stop_reason="cancelled", usage=stream.usage)
        return PromptResponse(stop_reason="end_turn", usage=stream.usage)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Stop the session's active turn; its prompt answers ``cancelled``."""
        del kwargs
        self._cancel_requested.add(session_id)
        handle = self._active.get(session_id)
        if handle is not None:
            await handle.cancel()

    async def close(self) -> None:
        """Release every embedded session."""
        await self._client.close()

    # =========================================================================
    # Helpers
    # =========================================================================

    async def send_update(self, session_id: str, update: Any) -> None:
        """Send one ``session/update`` notification to the connected client."""
        if self._conn is None:
            return
        await self._conn.session_update(session_id, update)

    async def _attach(self, session_id: str, cwd: str | None) -> bool:
        """Bind an embedded session to a stored transcript, or reuse it."""
        if session_id in self._client._sessions:
            return True
        stored = SessionStore(_session_meta_path().parent / "sessions", session_id).load()
        if stored is None:
            return False
        record = self._sessions.setdefault(session_id, _SessionRecord(cwd=cwd or str(Path.cwd())))
        await self._client.connect()
        await self._client.create_session(cwd=record.cwd, agent_name=self._agent_name, session_id=session_id)
        return True


async def _finish(relay: asyncio.Task[None]) -> None:
    """Wait for the relay to flush, unless it already stopped."""
    if not relay.done():
        relay.cancel()
    await asyncio.gather(relay, return_exceptions=True)


def _consume_exception(task: asyncio.Task[Any]) -> None:
    """Mark a relay's exception as observed; the prompt reports the turn."""
    if not task.cancelled():
        task.exception()


def _prompt_text(prompt: list[ContentBlock]) -> str:
    """Flatten ACP content blocks into the text one turn receives."""
    parts: list[str] = []
    for block in prompt:
        if isinstance(block, TextContentBlock):
            parts.append(block.text)
        elif isinstance(block, ResourceLink):
            parts.append(f"[{block.name}]({block.uri})")
        elif isinstance(block, ImageContentBlock):
            parts.append(f"[image {block.mime_type}]")
        elif isinstance(block, EmbeddedResource):
            resource = block.resource
            text = getattr(resource, "text", None)
            uri = getattr(resource, "uri", "")
            parts.append(text if isinstance(text, str) else f"[resource {uri}]")
        else:
            parts.append(f"[{getattr(block, 'type', 'content')}]")
    return "\n\n".join(part for part in parts if part)


def _message_text(content: Any) -> str:
    """Extract text from a stored conversation message."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _tool_call_id(event: TurnEvent) -> str:
    return str(event.data.get("tool_id") or uuid4().hex)


def _tool_kind(name: str) -> ToolKind:
    """Map an AnkaLoop tool name to the ACP tool kind clients render."""
    lower = name.lower()
    if any(token in lower for token in ("read", "cat", "view")):
        return "read"
    if any(token in lower for token in ("write", "edit", "patch", "apply")):
        return "edit"
    if any(token in lower for token in ("delete", "remove", "rm")):
        return "delete"
    if any(token in lower for token in ("grep", "search", "glob", "find")):
        return "search"
    if any(token in lower for token in ("bash", "shell", "exec", "run", "command")):
        return "execute"
    if "fetch" in lower or "web" in lower:
        return "fetch"
    if "think" in lower:
        return "think"
    return "other"


def _tool_title(name: str, arguments: Any) -> str:
    """Show the most useful argument as the tool call title."""
    if isinstance(arguments, dict):
        for key in ("command", "path", "file_path", "pattern", "query", "description"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return value
    return name


def _title_from(content: str) -> str:
    """Derive a short session title from the first prompt."""
    first = " ".join(content.split())
    return first if len(first) <= 60 else first[:57] + "..."


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _load_session_meta() -> dict[str, _SessionRecord]:
    """Load ACP session metadata, ignoring a corrupt file."""
    try:
        data = json.loads(_session_meta_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records: dict[str, _SessionRecord] = {}
    if not isinstance(data, dict):
        return records
    for session_id, value in data.items():
        if isinstance(value, dict) and isinstance(value.get("cwd"), str):
            records[session_id] = _SessionRecord(
                cwd=value["cwd"],
                title=value.get("title"),
                updated_at=value.get("updated_at"),
            )
    return records


def _save_session_meta(sessions: dict[str, _SessionRecord]) -> None:
    """Persist ACP session metadata atomically."""
    path = _session_meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        session_id: {"cwd": record.cwd, "title": record.title, "updated_at": record.updated_at}
        for session_id, record in sessions.items()
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


async def run_acp_agent(*, agent_name: str | None = None) -> None:
    """Serve ACP over stdio until the client disconnects."""
    agent = ACPSessionAgent(agent_name=agent_name)  # type: ignore[abstract]
    try:
        await run_agent(agent)
    finally:
        await agent.close()
