"""Dynamic provider credentials for AnkaLoop.

Some gateways issue short-lived tokens through an external CLI instead of a
static API key. A provider profile can declare ``api_key_command`` and
AnkaLoop will execute it lazily when an LLM client is created, cache the
result, and refresh it before expiry.

The companion ``auth_header`` setting formats the resolved key into an
explicit HTTP header (e.g. ``Authorization: Bearer {api_key}``) for gateways
that do not accept the provider SDK's default credential header.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import time

logger = logging.getLogger(__name__)

# Default cache lifetime for non-JWT tokens. JWTs are cached until their
# ``exp`` claim instead (minus ``EXPIRY_MARGIN_SECONDS``).
DEFAULT_COMMAND_TTL_SECONDS = 300.0
# Refresh a cached JWT this long before its actual expiry so an in-flight
# request never carries a token that expires mid-flight.
EXPIRY_MARGIN_SECONDS = 60.0
# Upper bound for a single credential command execution.
COMMAND_TIMEOUT_SECONDS = 15.0

# command -> (token, refresh_after_epoch)
# Keyed by the raw command string, so two providers configured with the same
# command deliberately share one cached token (one CLI call, one credential).
# The cache is not cleared on config reload: entries for removed/renamed
# commands linger until the process exits, and a changed TTL only takes effect
# once the existing entry expires. Use invalidate_api_key_cache (e.g. on a 401)
# to force a refresh sooner.
_command_cache: dict[str, tuple[str, float]] = {}


class ApiKeyCommandError(RuntimeError):
    """Raised when an ``api_key_command`` fails to produce a usable token."""


def _jwt_expiry(token: str) -> float | None:
    """Return the ``exp`` claim of a JWT without verifying its signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _run_command(command: str) -> str:
    """Execute the credential command and return its stripped stdout.

    The command runs through the shell, so pipes, ``&&``, and environment
    expansion work — but the value must be quoted as a shell command line.
    It comes from the user's own config file, the same trust boundary as the
    ``bash`` tool, so this is not an injection surface.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ApiKeyCommandError(
            f"api_key_command timed out after {COMMAND_TIMEOUT_SECONDS:.0f}s: {command!r}"
        ) from exc
    except OSError as exc:
        raise ApiKeyCommandError(f"api_key_command could not start: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = f": {stderr[0][:200]}" if stderr else ""
        raise ApiKeyCommandError(f"api_key_command exited with status {result.returncode}{detail}")

    token = (result.stdout or "").strip()
    if not token:
        raise ApiKeyCommandError("api_key_command produced an empty token")
    return token


def resolve_api_key_command(command: str, ttl_seconds: float | None = None) -> str:
    """Resolve an API key by running ``command``, caching the result.

    JWT tokens are cached until ``exp - EXPIRY_MARGIN_SECONDS``; other tokens
    are cached for ``ttl_seconds`` (default ``DEFAULT_COMMAND_TTL_SECONDS``).
    An explicit positive ``ttl_seconds`` caps a JWT's cache lifetime as well,
    so the token is refreshed at whichever comes first.
    """
    now = time.time()
    cached = _command_cache.get(command)
    if cached is not None and now < cached[1]:
        return cached[0]

    token = _run_command(command)
    jwt_expiry = _jwt_expiry(token)
    if jwt_expiry is not None:
        refresh_after = jwt_expiry - EXPIRY_MARGIN_SECONDS
    else:
        ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else DEFAULT_COMMAND_TTL_SECONDS
        refresh_after = now + ttl
    if ttl_seconds is not None and ttl_seconds > 0:
        refresh_after = min(refresh_after, now + ttl_seconds)
    _command_cache[command] = (token, refresh_after)
    return token


def invalidate_api_key_cache(command: str) -> None:
    """Drop the cached token for ``command`` so the next resolve re-runs it."""
    _command_cache.pop(command, None)


def clear_api_key_caches() -> None:
    """Drop all cached tokens. Intended for tests."""
    _command_cache.clear()


def format_auth_header(template: str, api_key: str) -> tuple[str, str]:
    """Render an ``auth_header`` template into a (header name, value) pair.

    The template must look like ``"Authorization: Bearer {api_key}"``; the
    ``{api_key}`` placeholder is replaced with the resolved credential.
    """
    name, separator, value_template = template.partition(":")
    name = name.strip()
    if not separator or not name:
        raise ValueError(f"auth_header must be formatted as 'Header-Name: ... {{api_key}}', got {template!r}")
    if "{api_key}" not in value_template:
        raise ValueError(f"auth_header must contain an '{{api_key}}' placeholder, got {template!r}")
    if not api_key:
        raise ValueError("auth_header is configured but no API key could be resolved")
    return name, value_template.replace("{api_key}", api_key).strip()
