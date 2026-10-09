"""Tests for dynamic provider credentials (api_key_command / auth_header)."""

import base64
import json
import subprocess

import pytest

from ankaloop import auth
from ankaloop.auth import (
    ApiKeyCommandError,
    clear_api_key_caches,
    format_auth_header,
    invalidate_api_key_cache,
    resolve_api_key_command,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    clear_api_key_caches()
    yield
    clear_api_key_caches()


def _make_jwt(exp: float | None) -> str:
    def encode(payload: dict) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = encode({"alg": "none"})
    claims = encode({"exp": exp} if exp is not None else {"sub": "test"})
    return f"{header}.{claims}.signature"


def _completed(stdout: str = "token-123\n", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestResolveApiKeyCommand:
    def test_runs_command_and_strips_output(self, monkeypatch):
        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: _completed("  token-abc \n"))
        assert resolve_api_key_command("get-token") == "token-abc"

    def test_caches_result_until_ttl(self, monkeypatch):
        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            return _completed(f"token-{calls}")

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        assert resolve_api_key_command("get-token") == "token-1"
        assert resolve_api_key_command("get-token") == "token-1"
        assert calls == 1

    def test_ttl_expiry_triggers_refresh(self, monkeypatch):
        calls = 0
        now = 1000.0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            return _completed(f"token-{calls}")

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        monkeypatch.setattr(auth.time, "time", lambda: now)

        assert resolve_api_key_command("get-token", ttl_seconds=10.0) == "token-1"
        now += 5.0
        assert resolve_api_key_command("get-token", ttl_seconds=10.0) == "token-1"
        now += 6.0
        assert resolve_api_key_command("get-token", ttl_seconds=10.0) == "token-2"
        assert calls == 2

    def test_jwt_cached_until_exp_minus_margin(self, monkeypatch):
        now = 1000.0
        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            return _completed(_make_jwt(exp=now + 600))

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        monkeypatch.setattr(auth.time, "time", lambda: now)

        # Without an explicit TTL the JWT exp wins: cached until exp - margin (60s).
        resolve_api_key_command("get-token")
        now += 500.0
        resolve_api_key_command("get-token")
        assert calls == 1

        now += 41.0
        resolve_api_key_command("get-token")
        assert calls == 2

    def test_explicit_ttl_caps_jwt_cache_lifetime(self, monkeypatch):
        now = 1000.0
        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            return _completed(_make_jwt(exp=now + 600))

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        monkeypatch.setattr(auth.time, "time", lambda: now)

        # An explicit TTL shorter than the JWT lifetime takes precedence.
        resolve_api_key_command("get-token", ttl_seconds=30.0)
        now += 15.0
        resolve_api_key_command("get-token", ttl_seconds=30.0)
        assert calls == 1

        now += 16.0
        resolve_api_key_command("get-token", ttl_seconds=30.0)
        assert calls == 2

    def test_jwt_without_exp_falls_back_to_ttl(self, monkeypatch):
        now = 1000.0
        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            return _completed(_make_jwt(exp=None))

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        monkeypatch.setattr(auth.time, "time", lambda: now)

        resolve_api_key_command("get-token", ttl_seconds=30.0)
        now += 31.0
        resolve_api_key_command("get-token", ttl_seconds=30.0)
        assert calls == 2

    def test_nonzero_exit_raises(self, monkeypatch):
        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: _completed(returncode=1, stderr="not logged in"))
        with pytest.raises(ApiKeyCommandError, match=r"status 1.*not logged in"):
            resolve_api_key_command("get-token")

    def test_empty_token_raises(self, monkeypatch):
        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: _completed("  \n"))
        with pytest.raises(ApiKeyCommandError, match="empty token"):
            resolve_api_key_command("get-token")

    def test_timeout_raises(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="get-token", timeout=15.0)

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        with pytest.raises(ApiKeyCommandError, match="timed out"):
            resolve_api_key_command("get-token")

    def test_failed_refresh_keeps_no_cache(self, monkeypatch):
        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: _completed(returncode=1, stderr="boom"))
        with pytest.raises(ApiKeyCommandError):
            resolve_api_key_command("get-token")
        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: _completed("recovered"))
        assert resolve_api_key_command("get-token") == "recovered"

    def test_invalidate_forces_refresh(self, monkeypatch):
        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            return _completed(f"token-{calls}")

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        assert resolve_api_key_command("get-token") == "token-1"
        invalidate_api_key_cache("get-token")
        assert resolve_api_key_command("get-token") == "token-2"


class TestFormatAuthHeader:
    def test_bearer_template(self):
        assert format_auth_header("Authorization: Bearer {api_key}", "tok") == ("Authorization", "Bearer tok")

    def test_raw_key_template(self):
        assert format_auth_header("x-api-key: {api_key}", "tok") == ("x-api-key", "tok")

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError, match="Header-Name"):
            format_auth_header("Authorization", "tok")

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="Header-Name"):
            format_auth_header(": Bearer {api_key}", "tok")

    def test_missing_placeholder_raises(self):
        with pytest.raises(ValueError, match="placeholder"):
            format_auth_header("Authorization: Bearer static", "tok")

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="no API key"):
            format_auth_header("Authorization: Bearer {api_key}", "")
