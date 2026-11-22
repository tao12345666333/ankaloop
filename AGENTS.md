# AnkaLoop Project Rules

## Project Overview

AnkaLoop is a Python coding-agent runtime with a CLI, HTTP/WebSocket server, Telegram integration,
MCP tools, multi-agent delegation, skills, hooks, memory, and
progressive context management.

## Code Style

- Python 3.11+ with modern type hints.
- Follow PEP 8 conventions.
- Maximum line length is 100 characters, enforced by ruff.
- Use dataclasses for configuration and lightweight data structures.
- Keep public functions and classes documented with docstrings.
- Use the `logging` module for runtime diagnostics, not `print`.

## Architecture Patterns

- Tools are classes inheriting from `BaseTool` in `src/ankaloop/tools.py`.
- Agents are configured through `AgentSpec` and `ResolvedAgentSpec`.
- Built-in agent types live in `src/ankaloop/multi_agent.py`.
- Configuration is TOML and is loaded through `src/ankaloop/config.py`.
- Project rules are loaded from `AGENTS.md` through `src/ankaloop/project_rules.py`.
- HTTP/WebSocket server code lives under `src/ankaloop/server/`.
- Client transports live under `src/ankaloop/client/`; protocol conversion code lives under
  `src/ankaloop/protocol/`.
- The ACP server for editor clients lives in `src/ankaloop/acp/` and is a thin adapter over the
  embedded client; it must not keep its own conversation state.
- Telegram command handling is split between `src/ankaloop/telegram/bot.py` and
  `src/ankaloop/telegram/handlers.py`.
- Keep shared slash-command behavior in `src/ankaloop/interaction.py` when possible so CLI,
  server, and Telegram stay consistent.

## Agent Behavior

- Conversation history is persisted per session.
- Per-request tool counters should reset for each user prompt; session-level history may still
  be used for diagnostics and caps.
- `read_file` has conversation and session limits.
- `bash` runs from the request working directory when available, has a per-request cap, and
  truncates large output.
- Telegram `/new` should create a fresh session, abandon old active work, and clear old queued
  messages.
- Model provider failures currently bubble up through `AgentExecutionError`; do not assume an
  AnkaLoop application-level retry exists.

## Testing

- Use pytest for tests.
- Prefer focused unit tests with mocks for external integrations.
- For behavior shared across transports, test the shared interaction layer and at least one
  transport-specific adapter.
- For Telegram session behavior, include tests for task cancellation, queue clearing, and stale
  response suppression.

## Important Files

```text
src/ankaloop/
├── agent.py              # Main Agent class and tool loop
├── agent_spec.py         # Agent configuration specs
├── tools.py              # Built-in tool definitions and registry
├── cli.py                # Typer CLI commands
├── config.py             # TOML configuration handling
├── compaction.py         # Smart context compaction
├── hooks.py              # Extensibility hooks
├── interaction.py        # Shared slash-command routing
├── memory.py             # Memory orchestration
├── models_db.py          # Model metadata lookup
├── project_rules.py      # AGENTS.md loading
├── acp/                  # ACP server for editor clients (optional extra)
├── client/               # Embedded, HTTP, and WebSocket clients
├── protocol/             # HTTP/WebSocket protocol adapters and converters
├── server/               # HTTP/WebSocket server
├── telegram/             # Telegram integration
└── progressive/          # Progressive context views
```

## Dependencies

- `typer` for CLI.
- `rich` for terminal UI.
- `pydantic` for data validation.
- `httpx` for HTTP clients.
- `fastapi` and `uvicorn` for the server.
- `openai` for OpenAI-compatible providers.
- `fastmcp` for MCP integration; `agent-client-protocol` (the `acp` extra) for editor clients.
- Optional extras provide Anthropic, Telegram, and development dependencies. Install the
  `dev` and `telegram` extras when running the complete test suite locally.

## GitHub Safety

- Do not use `gh` or another GitHub tool to create or edit PRs, issues, or comments unless the
  user explicitly confirms that action.
- Before preparing PR text, check for `.github/PULL_REQUEST_TEMPLATE.md` or files under
  `.github/PULL_REQUEST_TEMPLATE/` and follow the template when present.
- When a GitHub comment or PR body is explicitly requested, draft it in a temporary Markdown file
  and preview the exact body before posting.

## Pre-Commit Checks

Before committing code, run the checks that match the change. For normal code changes, prefer:

```bash
ruff format src tests
ruff check src tests
mypy src/ankaloop --ignore-missing-imports
python -m pytest -q
```

Tests that make live model calls are marked `llm`; exclude them for the normal local/CI-equivalent
suite with `python -m pytest -q -m "not llm"`.

For docs-only changes, run at least a targeted grep/lint sanity check and inspect the diff.

## Release Workflow

When releasing a new version, update all version references in this order:

| File | Location | Format |
|------|----------|--------|
| `pyproject.toml` | project metadata | `version = "X.Y.Z"` |
| `src/ankaloop/_version.py` | module constant | `__version__ = "X.Y.Z"` |

Verify the version update with:

```bash
grep -rn "X.Y.Z" pyproject.toml src/ankaloop/_version.py
```

Then run:

```bash
ruff format src tests
ruff check src tests
python -m pytest -q
```

Only when the user explicitly requests a release, commit and create an annotated tag:

```bash
git add -A
git commit -m "chore: bump version to X.Y.Z

Changes in this release:
- <notable changes>"
git tag -a vX.Y.Z -m "Release vX.Y.Z"
```

Pushing commits or tags and publishing packages are separate shared actions and require explicit
user approval. After publishing, verify the installed version reports `X.Y.Z`.

### Version Numbering

- Patch: bug fixes and documentation updates.
- Minor: new features and non-breaking behavior changes.
- Major: breaking changes.
