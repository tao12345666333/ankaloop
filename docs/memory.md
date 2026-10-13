# Long-term Memory

AnkaLoop keeps persistent cross-session memory: curated long-term notes
(`MEMORY.md`), an append-only history log (`HISTORY.md`), durable facts, and a
global persona (`SOUL.md` / `IDENTITY.md`).

## Backends

The `[memory]` section of `~/.config/ankaloop/config.toml` selects where memory
lives:

```toml
[memory]
backend = "local"        # "local" (default) | "http"
url = "https://..."      # required when backend = "http"
token = "..."            # optional bearer token; ANKA_MEMORY_TOKEN env var also works
timeout_seconds = 10.0   # optional per-request timeout
```

- **`local`** (default): file/SQLite storage under `~/.config/ankaloop/memory/`
  (user scope) and `.ankaloop/memory/` (project scope). Behavior is unchanged
  from earlier versions.
- **`http`**: every memory operation is mapped to one HTTP call against a remote
  memory service (`HttpMemoryManager`). Use it when several AnkaLoop processes
  (multiple `anka serve` instances, CLI plus server, ephemeral containers) must
  share one long-term memory with a single writer.

`get_memory_manager()` is the single swap point: the agent's context injection,
the `memory` tool, sub-agents spawned via the task tool, and the Telegram
integration all obtain their manager through it, so configuring `backend =
"http"` covers all of them. Local memory *consolidation* (`memory_dream`) is
file/SQLite-based and stays inactive with the HTTP backend; consolidation is
expected to happen on the server side.

### Failure semantics

- **Reads** (context injection, `read`, `search`, fact lookups, stats) degrade
  to empty results when the remote is unreachable, so the current turn still
  completes.
- **Writes** are staged in a local JSONL outbox
  (`~/.config/ankaloop/memory-outbox.jsonl`, capped at 1000 entries) and
  replayed in order before subsequent writes once the remote recovers. A write
  that was staged instead of applied returns `False` from `write_long_term` /
  `delete_fact`.
- The remote is the single writer; the client performs no merging or caching.

### Scope semantics

`scope="user" | "project"` is passed through unchanged, together with the local
`project_root` when known. The server decides how to key a project scope (e.g.
on a repo URL rather than a local clone path). Persona operations are
global-only, same as the local manager.

## HTTP API contract

The client speaks JSON over HTTP. `Authorization: Bearer <token>` is sent when
a token is configured. `scope` and `project_root` appear as query parameters on
GET/DELETE and as JSON body fields otherwise. Responses are JSON objects;
non-2xx responses count as backend failures.

| Method | Path | Request | Response |
|--------|------|---------|----------|
| GET | `/v1/context/memory` | `?project_root=` | `{"context": "..."}` |
| GET | `/v1/context/persona` | — | `{"context": "..."}` |
| GET | `/v1/long-term` | `?scope=&project_root=` | `{"content": "..."}` |
| PUT | `/v1/long-term` | `{"scope", "content", "project_root?"}` | `{"ok": true}` |
| POST | `/v1/history` | `{"scope", "content", "session_id", "tags", "project_root?"}` | `{"ok": true}` |
| GET | `/v1/search` | `?q=&max_results=&project_root=` | `{"results": [{"line_number", "content", "source"}]}` |
| GET | `/v1/soul` | `?include_default=true|false` | `{"content": "..."}` |
| PUT | `/v1/soul` | `{"content"}` | `{"ok": true}` |
| GET | `/v1/identity` | — | `{"content": "..."}` |
| PUT | `/v1/identity` | `{"content"}` | `{"ok": true}` |
| PUT | `/v1/facts/{key}` | `{"scope", "value", "category", "source", "confidence", "project_root?"}` | `{"ok": true}` |
| GET | `/v1/facts/{key}` | `?scope=&project_root=` | `{"fact": {...} \| null}` |
| GET | `/v1/facts` | `?scope=&category=&limit=&project_root=` | `{"facts": [...]}` |
| GET | `/v1/facts/search` | `?q=&max_results=&scope=&project_root=` | `{"facts": [...]}` |
| DELETE | `/v1/facts/{key}` | `?scope=&project_root=` | `{"deleted": true}` |
| GET | `/v1/stats` | `?project_root=` | `{"user": {...}, "project": {...}}` |

Notes for server implementers:

- `source` on search results is `"memory"` or `"history"`.
- Fact dicts keep the local shape: `key`, `value`, `category`, `source`,
  `confidence`, plus optional timestamps.
- `include_default=true` on `/v1/soul` asks the server to return its built-in
  default persona when no custom soul exists.

## Config directory location

All user-level paths (config, memory, sessions, transcripts, caches, logs)
honor `XDG_CONFIG_HOME`; the default is `~/.config/ankaloop/`. Set
`ANKA_CONFIG_DIR_NAME=amcp` to keep the legacy directory name.
