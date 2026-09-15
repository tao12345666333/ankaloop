# Contributing to AnkaLoop

Thank you for helping improve AnkaLoop. Keep changes focused, add tests for behavior changes, and
update user-facing documentation when commands or configuration change.

## Development Setup

AnkaLoop requires Python 3.11 or newer. Clone the repository, then install the project in editable
mode with the development and Telegram extras so the full test suite can be collected.

```bash
git clone https://github.com/tao12345666333/ankaloop.git
cd ankaloop

# Recommended: create and sync a uv-managed environment
uv sync --extra dev --extra telegram
source .venv/bin/activate

# Alternatively, use pip in an activated virtual environment
python -m pip install -e ".[dev,telegram]"
```

## Running Tests

```bash
# Run the complete local suite
make test

# Run with coverage
make test-cov

# Run the CI-equivalent suite without live model tests
python -m pytest -q -m "not llm"

# Run one file or one test
python -m pytest tests/test_config.py
python -m pytest tests/test_config.py::test_name
```

Tests that make live provider calls must use the `llm` marker. Unit tests should mock external
services and must not require credentials or network access.

## Code Quality

```bash
# Format first, then lint
make format
make lint

# Type check
make type-check
```

Before opening a pull request, run the checks relevant to your change. For normal Python changes,
the standard sequence is:

```bash
ruff format src tests
ruff check src tests
mypy src/ankaloop --ignore-missing-imports
python -m pytest -q -m "not llm"
```

## Pre-commit Hooks

```bash
python -m pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## Development Guidelines

- Target Python 3.11+ and use modern type hints.
- Follow PEP 8; Ruff enforces formatting and a 100-character line length.
- Add focused pytest coverage for fixes and behavior changes.
- Use `logging` rather than `print` for runtime diagnostics.
- Keep shared slash-command behavior in `src/ankaloop/interaction.py` so CLI, server, and Telegram
  remain consistent.
- Define built-in tools as `BaseTool` subclasses in `src/ankaloop/tools.py`.
- Do not include secrets, local configuration, generated coverage files, or build artifacts in
  commits.

## Project Structure

```text
ankaloop/
├── src/ankaloop/
│   ├── agent.py         # Main agent and tool loop
│   ├── agent_spec.py    # Agent configuration specs
│   ├── cli.py           # Typer CLI commands
│   ├── config.py        # TOML configuration
│   ├── interaction.py   # Shared slash-command behavior
│   ├── tools.py         # Built-in tools and registry
│   ├── client/          # Embedded, HTTP, and WebSocket clients
│   ├── progressive/     # Progressive context management
│   ├── protocol/        # HTTP/WebSocket adapters and converters
│   ├── server/          # HTTP/WebSocket server
│   └── telegram/        # Telegram integration
├── tests/               # Pytest suite
├── docs/                # User-facing docs (api/, architecture/, design/ for internal specs)
├── examples/            # Example agents, commands, hooks, skills, and plugins
├── .github/workflows/   # CI and release workflows
└── pyproject.toml       # Package metadata and tool configuration
```

## Pull Requests

1. Create a focused branch and keep commits scoped to one change.
2. Add or update tests and documentation as needed.
3. Run the relevant formatting, lint, type-check, and test commands.
4. Complete `.github/PULL_REQUEST_TEMPLATE.md`, including the verification commands and any
   related issues.
5. Confirm CI passes on Python 3.11, 3.12, and 3.13.

Avoid unrelated refactors in the same pull request. If a check cannot be run locally, state that
clearly in the pull request's Testing section.
