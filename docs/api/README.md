# AnkaLoop API Reference

> **Version**: 1.0.0
> **Base URL**: `http://localhost:8080/api/v1`
> **OpenAPI Spec**: `/openapi.json`

This document provides comprehensive API documentation for the AnkaLoop Server HTTP REST API, WebSocket API, and SSE event streams.

## Table of Contents

- [Authentication](#authentication)
- [HTTP REST API](#http-rest-api)
  - [Health & Info](#health--info)
  - [Sessions](#sessions)
  - [Tools](#tools)
  - [Agents](#agents)
  - [Events](#events)
- [WebSocket API](#websocket-api)
- [Error Handling](#error-handling)

---

## Authentication

**Current**: No authentication required (local use only)

**Planned** (Phase 5):
- API Key authentication via `X-API-Key` header
- JWT token support for web clients

---

## HTTP REST API

### Health & Info

#### GET `/health`

Check server health status.

**Response** `200 OK`:
```json
{
  "healthy": true,
  "version": "0.8.0",
  "uptime_seconds": 3600.5
}
```

#### GET `/info`

Get server information and capabilities.

**Response** `200 OK`:
```json
{
  "name": "ankaloop-server",
  "version": "0.8.0",
  "protocol_version": "1.0",
  "capabilities": ["sessions", "streaming", "websocket", "sse", "tools", "agents"],
  "agents": ["default"],
  "tools_count": 15
}
```

#### GET `/status`

Get detailed server status including connection stats.

**Response** `200 OK`:
```json
{
  "healthy": true,
  "version": "0.8.0",
  "uptime_seconds": 3600.5,
  "sessions": {
    "active": 2,
    "total": 5
  },
  "connections": {
    "websocket": 3,
    "sse": 1
  }
}
```

---

### Sessions

#### POST `/sessions`

Create a new session.

**Request Body**:
```json
{
  "cwd": "/path/to/project",
  "agent_name": "default"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | No | Working directory for the session |
| `agent_name` | string | No | Agent to use (defaults to "default") |

**Response** `201 Created`:
```json
{
  "id": "a1b2c3d4",
  "created_at": "2026-01-08T12:00:00Z",
  "updated_at": "2026-01-08T12:00:00Z",
  "cwd": "/path/to/project",
  "agent_name": "default",
  "status": "idle",
  "message_count": 0,
  "token_usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  },
  "queued_count": 0
}
```

#### GET `/sessions`

List all active sessions.

**Response** `200 OK`:
```json
{
  "sessions": [
    {
      "id": "a1b2c3d4",
      "created_at": "2026-01-08T12:00:00Z",
      "updated_at": "2026-01-08T12:30:00Z",
      "cwd": "/path/to/project",
      "agent_name": "default",
      "status": "idle",
      "message_count": 10,
      "token_usage": {
        "prompt_tokens": 5000,
        "completion_tokens": 2000,
        "total_tokens": 7000
      },
      "queued_count": 0
    }
  ],
  "total": 1
}
```

#### GET `/sessions/{id}`

Get details for a specific session.

**Path Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Session ID |

**Response** `200 OK`: Session object (same as above)

**Response** `404 Not Found`:
```json
{
  "error": "Session not found: a1b2c3d4",
  "code": "SESSION_NOT_FOUND",
  "details": {"session_id": "a1b2c3d4"}
}
```

#### DELETE `/sessions/{id}`

Delete a session.

**Response** `204 No Content`

#### POST `/sessions/{id}/prompt`

Send a prompt to a session and return its status or complete response as JSON.

**Request Body**:
```json
{
  "content": "Help me refactor this code",
  "priority": "normal",
  "stream": true,
  "conflict_strategy": "queue"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | Yes | The prompt content |
| `priority` | string | No | Message priority: `low`, `normal`, `high`, `urgent` |
| `stream` | boolean | No | Whether to stream the response (default: `true`) |
| `conflict_strategy` | string | No | Strategy when session is busy: `queue` (wait for queue, default) or `reject` (fail immediately with 409 Conflict) |

**Response** `200 OK` (`application/json`):
```json
{
  "session_id": "a1b2c3d4",
  "message_id": "turn-123",
  "status": "complete",
  "response": "I'll help you refactor..."
}
```

**Response** `409 Conflict`:
```json
{
  "error": "Session is busy: a1b2c3d4",
  "code": "SESSION_BUSY",
  "details": {"session_id": "a1b2c3d4"}
}
```

#### POST `/sessions/{id}/prompt/stream`

Submit a runtime-owned turn and receive newline-delimited JSON frames as output is produced. HTTP
disconnect only stops the relay; it does not cancel the turn. Use the returned `turn_id` with the
turn status endpoint after disconnecting.

```jsonl
{"type":"start","turn_id":"turn-123","session_id":"a1b2c3d4","status":"running"}
{"type":"chunk","turn_id":"turn-123","content":"I'll help "}
{"type":"tool","turn_id":"turn-123","event":"call_start","tool_name":"read_file"}
{"type":"chunk","turn_id":"turn-123","content":"you refactor..."}
{"type":"complete","turn_id":"turn-123","status":"completed"}
```

The shared HTTP/WebSocket frame types are `start`, `chunk`, `tool`, `error`, and `complete`.
Slow consumers receive an `error` frame with code `SLOW_CONSUMER`; the turn continues running.

#### GET `/sessions/{id}/turns/{turn_id}`

Return the state and terminal result, when available, of a runtime-retained turn.

#### POST `/sessions/{id}/cancel`

Cancel the current operation in a session.

**Request Body** (optional):
```json
{
  "force": false
}
```

**Response** `200 OK`:
```json
{
  "message": "Cancellation requested",
  "session_id": "a1b2c3d4"
}
```

---

### Tools

#### GET `/tools`

List all available tools.

**Response** `200 OK`:
```json
{
  "tools": [
    {
      "name": "read_file",
      "description": "Read the contents of a file",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {"type": "string", "description": "File path to read"}
        },
        "required": ["path"]
      },
      "source": "builtin"
    },
    {
      "name": "mcp__filesystem__read_file",
      "description": "Read file via MCP",
      "parameters": {...},
      "source": "mcp"
    }
  ],
  "total": 15
}
```

---

### Agents

#### GET `/agents`

List available agents.

**Response** `200 OK`:
```json
{
  "agents": [
    {
      "name": "default",
      "description": "Default AnkaLoop agent with full capabilities",
      "mode": "primary",
      "tools_count": 15
    },
    {
      "name": "code-reviewer",
      "description": "Specialized agent for code review",
      "mode": "subagent",
      "tools_count": 5
    }
  ],
  "total": 2
}
```

#### GET `/agents/{name}`

Get details for a specific agent.

**Response** `200 OK`:
```json
{
  "name": "default",
  "description": "Default AnkaLoop agent with full capabilities",
  "mode": "primary",
  "tools_count": 15,
  "system_prompt": "You are a helpful coding assistant...",
  "max_steps": 25,
  "tools": ["read_file", "write_file", "bash", ...]
}
```

---

### Events (SSE)

#### GET `/events`

Global event stream (Server-Sent Events).

**Headers**:
- `Accept: text/event-stream`

**Event Types**:
```
event: session.created
data: {"session_id": "a1b2c3d4", "timestamp": "..."}

event: message.chunk
data: {"session_id": "a1b2c3d4", "content": "Hello", "done": false}

event: tool.call_start
data: {"session_id": "a1b2c3d4", "tool_name": "read_file", "arguments": {...}}

event: tool.call_complete
data: {"session_id": "a1b2c3d4", "tool_name": "read_file", "result": "..."}
```

#### GET `/sessions/{id}/events`

Session-specific event stream.

Same event format as global events, but filtered to the specific session.

---

## WebSocket API

### Connection

```
ws://localhost:8080/ws?session_id={session_id}
```

### Message Format

All WebSocket messages follow this structure:

```typescript
interface WSMessage {
  type: "request" | "response" | "event" | "error";
  id?: string;           // Message ID for correlation
  timestamp: string;     // ISO 8601 timestamp
  payload: object;       // Message-specific payload
}
```

### Client → Server Messages

#### Send Prompt

```json
{
  "type": "request",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:00Z",
  "payload": {
    "action": "prompt",
    "content": "Help me refactor this code",
    "session_id": "a1b2c3d4",
    "priority": "normal"
  }
}
```

#### Cancel Operation

```json
{
  "type": "request",
  "id": "msg-124",
  "timestamp": "2026-01-08T12:00:01Z",
  "payload": {
    "action": "cancel",
    "session_id": "a1b2c3d4",
    "force": false
  }
}
```

### Server → Client Messages

#### Text Response (Streaming)

```json
{
  "type": "response",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:02Z",
  "payload": {
    "type": "chunk",
    "turn_id": "turn-123",
    "content": "I'll help you refactor..."
  }
}
```

#### Tool Call Start

```json
{
  "type": "response",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:03Z",
  "payload": {
    "type": "tool",
    "turn_id": "turn-123",
    "event": "call_start",
    "tool_name": "read_file",
    "tool_id": "call_abc123",
    "arguments": {"path": "/src/main.py"}
  }
}
```

#### Tool Call Complete

```json
{
  "type": "response",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:04Z",
  "payload": {
    "type": "tool",
    "turn_id": "turn-123",
    "event": "call_complete",
    "tool_name": "read_file",
    "tool_id": "call_abc123",
    "success": true
  }
}
```

#### Completion

```json
{
  "type": "response",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:10Z",
  "payload": {
    "type": "complete",
    "turn_id": "turn-123",
    "status": "completed"
  }
}
```

#### Error

```json
{
  "type": "error",
  "id": "msg-123",
  "timestamp": "2026-01-08T12:00:05Z",
  "payload": {
    "error": "Session not found",
    "code": "SESSION_NOT_FOUND",
    "session_id": "invalid-id"
  }
}
```

---

## Error Handling

### Error Response Format

All errors follow this format:

```json
{
  "error": "Human-readable error message",
  "code": "ERROR_CODE",
  "details": {
    // Additional error context
  }
}
```

### Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `BAD_REQUEST` | 400 | Invalid request format |
| `VALIDATION_ERROR` | 400 | Request validation failed |
| `INVALID_JSON` | 400 | Malformed JSON |
| `UNAUTHORIZED` | 401 | Authentication required |
| `FORBIDDEN` | 403 | Access denied |
| `NOT_FOUND` | 404 | Resource not found |
| `SESSION_NOT_FOUND` | 404 | Session not found |
| `TOOL_NOT_FOUND` | 404 | Tool not found |
| `AGENT_NOT_FOUND` | 404 | Agent not found |
| `CONFLICT` | 409 | Resource conflict |
| `SESSION_BUSY` | 409 | Session is busy |
| `RATE_LIMITED` | 429 | Too many requests |
| `INTERNAL_ERROR` | 500 | Internal server error |
| `LLM_ERROR` | 500 | LLM API error |
| `TOOL_ERROR` | 500 | Tool execution error |
| `MCP_ERROR` | 500 | MCP server error |
| `TIMEOUT` | 504 | Operation timed out |

---

## Event Types

| Event Type | Description |
|------------|-------------|
| `session.created` | New session created |
| `session.deleted` | Session deleted |
| `session.status_changed` | Session status changed |
| `message.start` | Message generation started |
| `message.chunk` | Message chunk (streaming) |
| `message.complete` | Message generation complete |
| `message.error` | Error during message generation |
| `tool.call_start` | Tool execution started |
| `tool.call_complete` | Tool execution completed |
| `tool.call_error` | Tool execution failed |
| `agent.thinking` | Agent thinking/planning |
| `agent.idle` | Agent returned to idle |
| `prompt.received` | Prompt received (for multi-client sync) |
| `prompt.started` | Prompt processing started |
| `prompt.queued` | Prompt was queued (session busy) |
| `prompt.rejected` | Prompt was rejected (conflict) |

---

## SDK Usage

### Python SDK

```python
from ankaloop.client import AnkaloopClient

async with AnkaloopClient("http://localhost:8080") as client:
    # Create session
    session = await client.create_session(cwd="/my/project")

    # Stream response
    async for chunk in await session.prompt_stream("Help me refactor"):
        print(chunk.content, end="")

    # Full response
    response = await session.prompt_full("What did you do?")
```

### TypeScript/JavaScript

```typescript
import type { paths, Session } from './types/ankaloop-api';

const response = await fetch('http://localhost:8080/api/v1/sessions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ cwd: '/my/project' })
});

const session: Session = await response.json();
```

### WebSocket (JavaScript)

```javascript
const ws = new WebSocket('ws://localhost:8080/ws?session_id=my-session');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === 'response' && msg.payload.kind === 'text') {
    console.log(msg.payload.content);
  }
};

ws.send(JSON.stringify({
  type: 'request',
  id: 'msg-1',
  payload: {
    action: 'prompt',
    content: 'Hello!',
    session_id: 'my-session'
  }
}));
```

---

## Rate Limiting

**Current**: No rate limiting (local use only)

**Planned** (Phase 5):
- 100 requests/minute per API key
- 10 concurrent sessions per client
- Configurable limits

---

## CORS

Cross-Origin Resource Sharing is enabled by default for local development:

```toml
[server.cors]
enabled = true
allow_origins = ["http://localhost:*", "http://127.0.0.1:*", "tauri://localhost"]
allow_methods = ["*"]
allow_headers = ["*"]
```

To customize, edit `~/.config/ankaloop/config.toml`.
