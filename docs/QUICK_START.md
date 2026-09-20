# Quick Start Guide

## For Users

### Prerequisites

- Python 3.11 or newer
- Credentials for a supported model provider, or access to an OpenAI-compatible endpoint

### Install and run

```bash
python -m pip install ankaloop
anka init
anka

# Run one task and exit
anka --once "summarize this repository"
```

`ankaloop` is the package name. Installation provides the `anka` command (recommended) and
the `ankaloop` command. The initialization wizard stores configuration in
`~/.config/ankaloop/config.toml`. Alternatively, run without installing via `uvx ankaloop init`
and `uvx ankaloop`.

Telegram support is optional: `python -m pip install "ankaloop[telegram]"`. Provider support,
including Anthropic, is included in the base package; do not install an `anthropic` extra.

## For Contributors

### 1. Clone and Setup
```bash
git clone https://github.com/tao12345666333/ankaloop.git
cd ankaloop

# Install with development dependencies
pip install -e ".[dev]"

# Or using uv (recommended)
uv sync --extra dev --extra telegram
source .venv/bin/activate
```

### 2. Install Pre-commit Hooks
```bash
pre-commit install
```

### 3. Run Tests
```bash
# Quick test
make test

# With coverage report
make test-cov

# Specific test file
pytest tests/test_tools.py -v
```

### 4. Code Quality Checks
```bash
# Lint code
make lint

# Auto-format code
make format

# Type check
make type-check

# Run all checks
make lint && make format && make test
```

### 5. Development Workflow
```bash
# 1. Create a feature branch
git checkout -b feature/my-feature

# 2. Make changes and test
make test

# 3. Format and lint
make format
make lint

# 4. Commit (pre-commit hooks will run automatically)
git add .
git commit -m "feat: add new feature"

# 5. Push and create PR
git push origin feature/my-feature
```

## Common Tasks

### Adding a New Test
```python
# tests/test_myfeature.py
import pytest
from ankaloop.myfeature import MyClass

def test_my_feature():
    obj = MyClass()
    result = obj.do_something()
    assert result == expected_value
```

### Adding a New Tool
```python
# src/ankaloop/tools.py
class MyNewTool(BaseTool):
    @property
    def name(self) -> str:
        return "my_tool"
    
    @property
    def description(self) -> str:
        return "Description of what this tool does"
    
    def execute(self, **kwargs) -> ToolResult:
        # Implementation
        return ToolResult(success=True, content="result")
```

### Running CI Locally
```bash
# Run the same checks as CI
make lint
make type-check
make test-cov
```

## Troubleshooting

### Tests Failing
```bash
# Run with verbose output
pytest -vv

# Run specific test
pytest tests/test_tools.py::test_read_file_tool -v

# Show print statements
pytest -s
```

### Import Errors
```bash
# Reinstall in editable mode
pip install -e .
```

### Pre-commit Issues
```bash
# Update hooks
pre-commit autoupdate

# Run manually
pre-commit run --all-files
```
