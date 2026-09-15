.PHONY: install test lint format clean build publish publish-test publish-twine

install:
	pip install -e ".[dev]"

test:
	pytest

test-cov:
	pytest --cov=ankaloop --cov-branch --cov-report=html --cov-report=term

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

type-check:
	mypy src/ankaloop --ignore-missing-imports

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .coverage htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +

# Build distribution packages
build: clean
	uv build

# Publish to PyPI (production) - using uv
publish: build
	uv publish

# Publish to TestPyPI (for testing) - using uv
publish-test: build
	uv publish --publish-url https://test.pypi.org/legacy/

# Alternative: Publish using twine (if uv publish not available)
publish-twine: build
	pip install --upgrade twine
	twine upload dist/*
