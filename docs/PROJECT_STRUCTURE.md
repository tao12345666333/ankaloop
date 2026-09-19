# Project Structure

## Overview

AMCP follows Python best practices with a clear separation of concerns:

```
AMCP/
├── src/ankaloop/              # Main package source code
│   ├── __init__.py        # Package initialization
│   ├── __main__.py        # Entry point for python -m ankaloop
│   ├── cli.py             # CLI interface (Typer)
│   ├── agent.py           # Agent orchestration logic
│   ├── agent_spec.py      # Agent specification handling
│   ├── tools.py           # Built-in tools (read, grep, bash, etc.)
│   ├── config.py          # Configuration management
│   ├── mcp_client.py      # MCP server integration
│   └── readfile.py        # File reading utilities
│
├── tests/                 # Test suite
│   ├── __init__.py
│   ├── conftest.py        # Pytest fixtures
│   ├── test_agent_spec.py
│   ├── test_config.py
│   └── test_tools.py
│
├── .github/
│   └── workflows/
│       └── ci.yml         # GitHub Actions CI/CD
│
├── docs/                  # Documentation
│   ├── QUICK_START.md     # Getting started guide
│   ├── apply-patch.md     # Patch tool reference
│   ├── hooks.md           # Hooks system reference
│   ├── prompts.md         # Prompt system reference
│   ├── skills-and-commands.md  # Skills and slash commands
│   ├── PROJECT_STRUCTURE.md    # This file
│   ├── api/               # API documentation
│   ├── architecture/      # Architecture documents
│   └── design/            # Internal design docs (development phases, feature specs)
├── examples/              # Example agents, commands, hooks, skills, and plugins
│
├── pyproject.toml         # Project metadata & dependencies
├── pytest.ini             # Pytest configuration
├── Makefile               # Common development tasks
├── .pre-commit-config.yaml # Pre-commit hooks
├── .ruff.toml             # Ruff linter configuration
├── .gitignore
├── README.md
├── CONTRIBUTING.md
├── CHANGELOG.md
└── Dockerfile
```

## Key Design Decisions

### 1. Source Layout (`src/` layout)
- Prevents accidental imports of uninstalled code
- Clear separation between source and tests
- Recommended by PyPA

### 2. Testing
- Uses pytest for testing framework
- Fixtures in `conftest.py` for reusability
- Coverage reporting with pytest-cov
- Target: >80% code coverage

### 3. Code Quality
- Ruff for linting and formatting
- Type hints encouraged (mypy for type checking)
- Pre-commit hooks for automated checks

### 4. CI/CD
- GitHub Actions for automated testing
- Matrix testing across Python 3.11, 3.12, 3.13
- Automated coverage reporting

### 5. Configuration
- pyproject.toml as single source of truth
- Tool configurations centralized
- Optional dependencies for development

## Development Workflow

1. **Setup**: `make install` or `pip install -e ".[dev]"`
2. **Test**: `make test` or `pytest`
3. **Lint**: `make lint` or `ruff check src/ tests/`
4. **Format**: `make format` or `ruff format src/ tests/`
5. **Coverage**: `make test-cov`

## Module Responsibilities

- **cli.py**: Command-line interface, argument parsing
- **agent.py**: Core agent logic, tool orchestration
- **tools.py**: Built-in tool implementations
- **config.py**: Configuration loading/saving
- **mcp_client.py**: MCP protocol communication
