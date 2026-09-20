# AnkaLoop Protocol Compatibility Guide

This document describes how AnkaLoop handles different protocols and ensures consistent behavior across:

- **HTTP REST API** - Primary programmatic interface
- **WebSocket** - Real-time streaming communication
- **SSE (Server-Sent Events)** - One-way event streaming

## Protocol Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        AnkaLoop Protocol Layer                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   ┌────────────────┐                                                    │
│   │ ProtocolAdapter│  ← Unified conversion layer                        │
│   └───────┬────────┘                                                    │
│           │                                                              │
│     ┌─────┴─────┬─────────────┐                                        │
│     ▼           ▼             ▼                                        │
│ ┌──────┐   ┌─────────┐   ┌─────────┐                                   │
│ │ HTTP │   │WebSocket│   │   SSE   │                                   │
│ │ REST │   │         │   │         │                                   │
│ └──────┘   └─────────┘   └─────────┘                                   │
│                                                                          │
│   Request/     Bidirectional   Server→Client                            │
│   Response     Streaming       Events                                  │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Unified Event Model

All protocols use a unified event model defined in `ankaloop.server.models.EventType`:

| Event Type | HTTP | WebSocket | SSE |
|------------|------|-----------|-----|
| `session.created` | POST /sessions | ✓ | ✓ |
| `session.deleted` | DELETE /sessions/{id} | ✓ | ✓ |
| `message.chunk` | (streaming response) | ✓ | ✓ |
| `message.complete` | (response end) | ✓ | ✓ |
| `tool.call_start` | - | ✓ | ✓ |
| `tool.call_complete` | - | ✓ | ✓ |

## ProtocolAdapter

The `ProtocolAdapter` class provides unified conversion between protocols:

```python
from ankaloop.protocol import ProtocolAdapter, get_protocol_adapter

adapter = get_protocol_adapter()

# Create a unified event
server_event = adapter.create_message_event(session_id, "Hello")

# Convert to WebSocket format
ws_message = adapter.to_ws_message(server_event, message_id)

# Convert to SSE format
sse_data = adapter.to_sse_data(server_event)
```

## Error Code Mapping

AnkaLoop uses a unified error code system that maps to appropriate responses in each protocol:

### HTTP Status Mapping

| Error Code | HTTP Status | Description |
|------------|-------------|-------------|
| `BAD_REQUEST` | 400 | Invalid request format |
| `VALIDATION_ERROR` | 400 | Request validation failed |
| `UNAUTHORIZED` | 401 | Authentication required |
| `FORBIDDEN` | 403 | Access denied |
| `SESSION_NOT_FOUND` | 404 | Session not found |
| `TOOL_NOT_FOUND` | 404 | Tool not found |
| `SESSION_BUSY` | 409 | Session is busy |
| `INTERNAL_ERROR` | 500 | Internal server error |
| `TIMEOUT` | 504 | Operation timed out |

### Usage

```python
from ankaloop.protocol import ProtocolError, ErrorCode, SessionNotFoundError

# Raise unified error
raise SessionNotFoundError("session-123")

# Or manually
raise ProtocolError(
    code=ErrorCode.SESSION_BUSY,
    message="Session is processing another request",
    details={"session_id": "session-123"}
)
```

## WebSocket Message Format

### Request Messages

```json
{
  "type": "request",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:00Z",
  "payload": {
    "action": "prompt",
    "content": "Help me refactor",
    "session_id": "session-abc"
  }
}
```

### Response Messages

```json
{
  "type": "response",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:01Z",
  "payload": {
    "kind": "text",
    "content": "I'll help you...",
    "done": false,
    "session_id": "session-abc"
  }
}
```

### Event Messages

```json
{
  "type": "event",
  "timestamp": "2026-01-08T12:00:02Z",
  "payload": {
    "kind": "tool_call",
    "tool_name": "read_file",
    "arguments": {"path": "/src/main.py"},
    "session_id": "session-abc"
  }
}
```

## SSE Event Format

```
event: message.chunk
data: {"type":"message.chunk","session_id":"abc","payload":{"content":"Hello"}}

event: tool.call_start
data: {"type":"tool.call_start","session_id":"abc","payload":{"tool_name":"read_file"}}
```

## Best Practices

### 1. Use the ProtocolAdapter for conversions

Always use `ProtocolAdapter` instead of manual conversions to ensure consistency:

```python
from ankaloop.protocol import get_protocol_adapter

adapter = get_protocol_adapter()

# Good
ws_msg = adapter.to_ws_message(event)

# Bad - manual conversion
ws_msg = {"type": "event", "payload": event.payload}  # May miss fields
```

### 2. Use ProtocolError for errors

Unified error handling across protocols:

```python
from ankaloop.protocol import ProtocolError, ErrorCode

# Works correctly in HTTP, WebSocket, and SSE contexts
raise ProtocolError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
```

### 3. Event factory methods

Use adapter factory methods for creating events:

```python
# Create message event
event = adapter.create_message_event(
    session_id="abc",
    content="Hello",
    done=False
)

# Create tool event
event = adapter.create_tool_start_event(
    session_id="abc",
    tool_name="read_file",
    arguments={"path": "/src/main.py"}
)
```

## TypeScript Types

Generate TypeScript types for web clients:

```bash
# From running server
python scripts/generate_types.py --server http://localhost:8080

# Manual generation
python scripts/generate_types.py --manual
```

This generates `types/ankaloop-api.d.ts` with all API types.
