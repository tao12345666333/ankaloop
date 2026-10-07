"""API models for AnkaLoop Server."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# Enums
# ============================================================================


class SessionStatus(StrEnum):
    """Session status."""

    IDLE = "idle"
    BUSY = "busy"
    CANCELLED = "cancelled"
    ERROR = "error"


class EventType(StrEnum):
    """Server event types."""

    # Connection events
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    HEARTBEAT = "heartbeat"

    # Session events
    SESSION_CREATED = "session.created"
    SESSION_DELETED = "session.deleted"
    SESSION_STATUS_CHANGED = "session.status_changed"

    # Message events
    MESSAGE_START = "message.start"
    MESSAGE_CHUNK = "message.chunk"
    MESSAGE_COMPLETE = "message.complete"
    MESSAGE_ERROR = "message.error"

    # Tool events
    TOOL_CALL_START = "tool.call_start"
    TOOL_CALL_COMPLETE = "tool.call_complete"
    TOOL_CALL_ERROR = "tool.call_error"

    # Agent events
    AGENT_THINKING = "agent.thinking"
    AGENT_IDLE = "agent.idle"

    # Collaboration events (for multi-client sync)
    PROMPT_RECEIVED = "prompt.received"  # Another client sent a prompt
    PROMPT_STARTED = "prompt.started"  # Prompt processing started
    PROMPT_QUEUED = "prompt.queued"  # Prompt was queued (busy session)
    PROMPT_REJECTED = "prompt.rejected"  # Prompt was rejected (conflict)


class ConflictStrategy(StrEnum):
    """Strategy for handling concurrent prompts to a busy session."""

    QUEUE = "queue"  # Queue the prompt for later execution
    REJECT = "reject"  # Reject the prompt with an error


class MessagePriority(StrEnum):
    """Message priority levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# ============================================================================
# Request Models
# ============================================================================


class CreateSessionRequest(BaseModel):
    """Request to create a new session."""

    cwd: str | None = Field(None, description="Working directory for the session")
    agent_name: str | None = Field(None, description="Agent to use for this session")
    session_id: str | None = Field(None, description="Optional requested session ID")


class PromptRequest(BaseModel):
    """Request to send a prompt."""

    content: str = Field(..., description="The prompt content")
    channel: str | None = Field(None, description="Origin channel, for example cli or telegram")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional channel metadata")
    priority: MessagePriority = Field(default=MessagePriority.NORMAL, description="Message priority")
    stream: bool = Field(default=True, description="Whether to stream the response")
    conflict_strategy: ConflictStrategy = Field(
        default=ConflictStrategy.QUEUE,
        description="Strategy when session is busy: 'queue' to wait, 'reject' to fail immediately",
    )


class CancelRequest(BaseModel):
    """Request to cancel current operation."""

    force: bool = Field(default=False, description="Force cancellation")


# ============================================================================
# Response Models
# ============================================================================


class TokenUsage(BaseModel):
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Session(BaseModel):
    """Session information."""

    id: str
    created_at: datetime
    updated_at: datetime
    cwd: str
    agent_name: str
    status: SessionStatus
    message_count: int = 0
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    queued_count: int = 0


class SessionListResponse(BaseModel):
    """Response for listing sessions."""

    sessions: list[Session]
    total: int


class PromptResponse(BaseModel):
    """Response after sending a prompt."""

    session_id: str
    message_id: str
    status: str  # "streaming" | "queued" | "complete" | "error"
    position: int | None = None  # Position in queue if queued
    response: str | None = None
    command: str | None = None
    new_session_id: str | None = None
    trace_id: str | None = None  # Hex trace id of the turn when tracing is enabled


class HealthResponse(BaseModel):
    """Health check response."""

    healthy: bool = True
    version: str
    uptime_seconds: float


class ServerInfo(BaseModel):
    """Server information."""

    name: str = "ankaloop-server"
    version: str
    protocol_version: str = "1.0"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "sessions",
            "streaming",
            "websocket",
            "sse",
            "tools",
            "agents",
        ]
    )
    agents: list[str] = Field(default_factory=list)
    tools_count: int = 0


class ToolInfo(BaseModel):
    """Tool information."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    source: str = "builtin"  # "builtin" | "mcp" | "plugin"


class ToolListResponse(BaseModel):
    """Response for listing tools."""

    tools: list[ToolInfo]
    total: int


class AgentInfo(BaseModel):
    """Agent information."""

    name: str
    description: str
    mode: str  # "primary" | "subagent"
    tools_count: int = 0


class AgentListResponse(BaseModel):
    """Response for listing agents."""

    agents: list[AgentInfo]
    total: int


# ============================================================================
# Event Models
# ============================================================================


class ServerEvent(BaseModel):
    """Server-sent event."""

    type: EventType
    session_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    payload: dict[str, Any] = Field(default_factory=dict)


class MessageChunkEvent(BaseModel):
    """Message chunk event for streaming."""

    session_id: str
    message_id: str
    content: str
    done: bool = False


class ToolCallEvent(BaseModel):
    """Tool call event."""

    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    call_id: str


class ToolResultEvent(BaseModel):
    """Tool result event."""

    session_id: str
    tool_name: str
    call_id: str
    result: str
    success: bool
    error: str | None = None


# ============================================================================
# WebSocket Models
# ============================================================================


class WSMessage(BaseModel):
    """WebSocket message format."""

    type: str  # "request" | "response" | "event" | "error"
    id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    payload: dict[str, Any] = Field(default_factory=dict)


class WSPromptPayload(BaseModel):
    """WebSocket prompt payload."""

    action: str = "prompt"
    content: str
    session_id: str
    priority: MessagePriority = MessagePriority.NORMAL


class WSCancelPayload(BaseModel):
    """WebSocket cancel payload."""

    action: str = "cancel"
    session_id: str
    force: bool = False


# ============================================================================
# Error Models
# ============================================================================


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    code: str
    details: dict[str, Any] | None = None


class ValidationErrorResponse(BaseModel):
    """Validation error response."""

    error: str = "Validation Error"
    code: str = "VALIDATION_ERROR"
    details: list[dict[str, Any]]
