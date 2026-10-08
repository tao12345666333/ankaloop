"""Session management endpoints."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator, Coroutine
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ...runtime import ErrorEnvelope
from ..interaction import apply_interaction_result, route_server_interaction
from ..models import (
    CancelRequest,
    ConflictStrategy,
    CreateSessionRequest,
    PromptRequest,
    PromptResponse,
    Session,
    SessionListResponse,
    SessionStatus,
)
from ..session_manager import (
    MaxSessionsReachedError,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    get_session_manager,
)
from ..turn_stream import turn_frames

router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _safe_emit(coro: Coroutine[Any, Any, None]) -> None:
    """Safely emit an event, suppressing any exceptions."""
    with contextlib.suppress(Exception):
        await coro


async def _enqueue_agent_prompt(
    session_manager: Any,
    session: Any,
    content: str,
    request: PromptRequest,
):
    """Submit a queued prompt and return its owned turn handle and position."""
    position = session.agent.queued_count() + 1
    handle = await session_manager.submit_prompt(
        session_id=session.id,
        content=content,
        work_dir=Path(session.cwd),
        stream=request.stream,
        priority=request.priority.value,
    )
    return handle, position


@router.post("", response_model=Session)
async def create_session(request: CreateSessionRequest | None = None) -> Session:
    """Create a new session.

    Creates a new agent session with optional working directory
    and agent specification.
    """
    session_manager = get_session_manager()

    try:
        req = request or CreateSessionRequest(cwd=None, agent_name=None, session_id=None)
        managed_session = await session_manager.create_session(
            cwd=req.cwd,
            agent_name=req.agent_name,
            session_id=req.session_id,
        )
        return managed_session.to_session()
    except MaxSessionsReachedError as e:
        raise HTTPException(
            status_code=429,
            detail={"error": str(e), "code": "MAX_SESSIONS_REACHED"},
        ) from None
    except SessionAlreadyExistsError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": str(e), "code": "SESSION_ALREADY_EXISTS"},
        ) from None


@router.get("", response_model=SessionListResponse)
async def list_sessions() -> SessionListResponse:
    """List all active sessions."""
    session_manager = get_session_manager()
    sessions = await session_manager.list_sessions()
    return SessionListResponse(sessions=sessions, total=len(sessions))


@router.get("/{session_id}", response_model=Session)
async def get_session(session_id: str) -> Session:
    """Get session details by ID."""
    session_manager = get_session_manager()

    try:
        managed_session = await session_manager.get_session(session_id)
        return managed_session.to_session()
    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Delete a session."""
    session_manager = get_session_manager()

    try:
        await session_manager.delete_session(session_id)
        return {"status": "deleted", "session_id": session_id}
    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None


@router.post("/{session_id}/prompt", response_model=PromptResponse)
async def send_prompt(session_id: str, request: PromptRequest) -> PromptResponse:
    """Send a prompt to a session.

    Runs the prompt and returns the complete response. For incremental output,
    use the /sessions/{id}/prompt/stream endpoint.

    The `conflict_strategy` parameter controls behavior when the session is busy:
    - `queue`: Add the prompt to the queue (default)
    - `reject`: Reject the prompt with an error
    """
    session_manager = get_session_manager()

    try:
        session = await session_manager.get_session(session_id)
        message_id = f"msg-{uuid.uuid4().hex[:12]}"
        routed, _ = await route_server_interaction(session_manager, session_id, request.content)

        if routed.action != "prompt":
            response = PromptResponse(
                session_id=session_id,
                message_id=message_id,
                status="handled",
            )
            async for event in apply_interaction_result(session_manager, session_id, routed):
                if event["type"] in {"session_created", "session_switched"}:
                    response.session_id = event["session_id"]
                    if event["type"] == "session_created":
                        response.new_session_id = event["session_id"]
                    response.response = event.get("content")
                    response.command = event["type"]
                elif event["type"] == "chunk":
                    response.response = event.get("content")
            return response

        # Get event bridge for collaboration events
        from ..event_bridge import get_event_bridge

        bridge = get_event_bridge()

        # Emit collaboration event: notify other clients about incoming prompt
        await _safe_emit(
            bridge.emit_prompt_received(
                session_id=session_id,
                content=request.content,
                priority=request.priority.value,
            )
        )

        # Check if busy and handle based on conflict strategy
        is_busy = session.status == SessionStatus.BUSY or session.agent.is_busy()
        if is_busy and request.conflict_strategy == ConflictStrategy.REJECT:
            # Notify about rejection
            await _safe_emit(
                bridge.emit_prompt_rejected(
                    session_id=session_id,
                    reason="Session is busy",
                    conflict_strategy="reject",
                )
            )

            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Session is busy, prompt rejected",
                    "code": "SESSION_BUSY",
                    "session_id": session_id,
                },
            )

        if is_busy:
            # Default: queue the message
            handle, position = await _enqueue_agent_prompt(
                session_manager,
                session,
                routed.content,
                request,
            )

            # Notify about queuing
            await _safe_emit(
                bridge.emit_prompt_queued(
                    session_id=session_id,
                    message_id=handle.id,
                    position=position,
                )
            )

            return PromptResponse(
                session_id=session_id,
                message_id=handle.id,
                status="queued",
                position=position,
            )

        # Notify that processing started
        await _safe_emit(
            bridge.emit_prompt_started(
                session_id=session_id,
                message_id=message_id,
            )
        )

        handle = await session_manager.submit_prompt(
            session_id=session_id,
            content=routed.content,
            stream=False,
            priority=request.priority.value,
        )
        response_text = await handle.wait()

        return PromptResponse(
            session_id=session_id,
            message_id=handle.id,
            status="complete",
            response=response_text,
            trace_id=handle.trace_id,
        )

    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None


@router.post("/{session_id}/prompt/stream")
async def send_prompt_stream(session_id: str, request: PromptRequest) -> StreamingResponse:
    """Send a prompt and stream the response.

    Returns a streaming response with chunks from the agent.
    Each chunk is a JSON object followed by a newline.

    The `conflict_strategy` parameter controls behavior when the session is busy:
    - `queue`: Wait for the queue (default)
    - `reject`: Reject the prompt with an error
    """
    session_manager = get_session_manager()

    try:
        session = await session_manager.get_session(session_id)
        routed, _ = await route_server_interaction(session_manager, session_id, request.content)

        # Get event bridge for collaboration events
        from ..event_bridge import get_event_bridge

        bridge = get_event_bridge()

        # Emit collaboration event: notify other clients about incoming prompt
        await _safe_emit(
            bridge.emit_prompt_received(
                session_id=session_id,
                content=request.content,
                priority=request.priority.value,
            )
        )

        if routed.action == "prompt":
            # Check for conflict and handle based on strategy
            is_busy = session.status == SessionStatus.BUSY or session.agent.is_busy()
            if is_busy and request.conflict_strategy == ConflictStrategy.REJECT:
                # Notify about rejection
                await _safe_emit(
                    bridge.emit_prompt_rejected(
                        session_id=session_id,
                        reason="Session is busy",
                        conflict_strategy="reject",
                    )
                )

                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "Session is busy, prompt rejected",
                        "code": "SESSION_BUSY",
                        "session_id": session_id,
                    },
                )
            # For QUEUE strategy, continue - the agent will queue internally

    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None

    async def generate() -> AsyncGenerator[str, None]:
        import json

        message_id = f"msg-{uuid.uuid4().hex[:12]}"
        try:
            if routed.action == "prompt":
                handle = await session_manager.submit_prompt(
                    session_id=session_id,
                    content=routed.content,
                    stream=True,
                    priority=request.priority.value,
                )
                message_id = handle.id
                async for frame in turn_frames(handle, session_id):
                    yield json.dumps(frame) + "\n"
            else:
                yield (
                    json.dumps(
                        {
                            "type": "start",
                            "turn_id": message_id,
                            "session_id": session_id,
                            "status": "running",
                        }
                    )
                    + "\n"
                )
                async for event in apply_interaction_result(session_manager, session_id, routed):
                    yield json.dumps(event) + "\n"
                yield json.dumps({"type": "complete", "turn_id": message_id}) + "\n"

        except Exception as e:
            envelope = ErrorEnvelope.from_exception(e)
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "turn_id": message_id,
                        "error": envelope.message,
                        "code": envelope.code,
                        "retryable": envelope.retryable,
                        "details": envelope.details,
                    }
                )
                + "\n"
            )

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-ID": session_id,
        },
    )


@router.post("/{session_id}/cancel")
async def cancel_session(session_id: str, request: CancelRequest | None = None) -> dict:
    """Cancel the current operation in a session."""
    session_manager = get_session_manager()

    try:
        req = request or CancelRequest()
        await session_manager.cancel_session(session_id, force=req.force)
        return {"status": "cancelled", "session_id": session_id}
    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None


@router.get("/{session_id}/turns/{turn_id}")
async def get_turn(session_id: str, turn_id: str) -> dict[str, Any]:
    """Return the current or terminal state of one submitted turn."""
    session_manager = get_session_manager()
    try:
        session = await session_manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "SESSION_NOT_FOUND"}) from None
    handle = session.agent.get_turn(turn_id)
    if handle is None:
        raise HTTPException(status_code=404, detail={"code": "TURN_NOT_FOUND"})
    outcome = handle.outcome
    envelope = outcome.error_envelope if outcome else None
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "status": handle.status.value,
        "response": outcome.value if outcome else None,
        "error": str(outcome.error) if outcome and outcome.error else None,
        "error_envelope": envelope.to_dict() if envelope else None,
    }


@router.get("/{session_id}/history")
async def get_session_history(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Get conversation history for a session."""
    session_manager = get_session_manager()

    try:
        session = await session_manager.get_session(session_id)
        history = session.agent.conversation_history[-limit:]

        return {
            "session_id": session_id,
            "messages": history,
            "total": len(session.agent.conversation_history),
            "returned": len(history),
        }
    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None


@router.get("/{session_id}/timeline")
async def get_session_timeline(
    session_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    """Get recent durable, metadata-only execution events for a session."""
    session_manager = get_session_manager()
    try:
        session = await session_manager.get_session(session_id)
        events = session.agent.get_timeline(limit=limit)
        return {
            "session_id": session_id,
            "events": events,
            "returned": len(events),
        }
    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None


@router.delete("/{session_id}/history")
async def clear_session_history(session_id: str) -> dict:
    """Clear conversation history for a session."""
    session_manager = get_session_manager()

    try:
        session = await session_manager.get_session(session_id)
        await session.agent.clear_conversation_history()

        return {"status": "cleared", "session_id": session_id}
    except SessionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Session not found: {session_id}", "code": "SESSION_NOT_FOUND"},
        ) from None
