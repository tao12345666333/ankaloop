"""Tests for the HTTP memory backend."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import ankaloop.config as config_module
from ankaloop.config import AnkaloopConfig, MemoryConfig
from ankaloop.memory import MemoryManager, get_memory_manager, reset_memory_manager
from ankaloop.memory_http import HttpMemoryManager

# --- Fixtures ---


@pytest.fixture(autouse=True)
def _reset_manager():
    """Reset global memory manager before each test."""
    reset_memory_manager()
    yield
    reset_memory_manager()


def make_manager(
    handler: httpx.MockTransport | object,
    tmp_path: Path,
    **kwargs,
) -> HttpMemoryManager:
    """Build an HttpMemoryManager wired to a mock transport and temp outbox."""
    return HttpMemoryManager(
        base_url="https://memory.test",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        outbox_path=tmp_path / "outbox.jsonl",
        **kwargs,
    )


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def down_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


# --- Tests: method -> HTTP mapping ---


class TestRequestMapping:
    def test_read_long_term(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"content": "project notes"})

        manager = make_manager(handler, tmp_path)
        assert manager.read_long_term(scope="project") == "project notes"
        request = seen[0]
        assert request.method == "GET"
        assert request.url.path == "/v1/long-term"
        assert request.url.params["scope"] == "project"

    def test_write_long_term(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        manager = make_manager(handler, tmp_path)
        assert manager.write_long_term("# Facts", scope="user") is True
        request = seen[0]
        assert request.method == "PUT"
        assert request.url.path == "/v1/long-term"
        body = json.loads(request.content)
        assert body == {"content": "# Facts", "scope": "user"}

    def test_append_history(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        manager = make_manager(handler, tmp_path)
        manager.append_history("did a thing", session_id="s1", tags=["a", "b"], scope="project")
        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/v1/history"
        body = json.loads(request.content)
        assert body["content"] == "did a thing"
        assert body["session_id"] == "s1"
        assert body["tags"] == ["a", "b"]
        assert body["scope"] == "project"

    def test_search(self, tmp_path: Path):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/search"
            assert request.url.params["q"] == "auth"
            return httpx.Response(
                200,
                json={"results": [{"line_number": 3, "content": "auth module", "source": "history"}]},
            )

        manager = make_manager(handler, tmp_path)
        results = manager.search("auth", max_results=5)
        assert len(results) == 1
        assert results[0].line_number == 3
        assert results[0].content == "auth module"
        assert results[0].source == "history"

    def test_context_endpoints(self, tmp_path: Path):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json={"context": f"ctx for {request.url.path}"})

        manager = make_manager(handler, tmp_path)
        assert manager.get_memory_context() == "ctx for /v1/context/memory"
        assert manager.get_persona_context() == "ctx for /v1/context/persona"
        assert paths == ["/v1/context/memory", "/v1/context/persona"]

    def test_persona(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"content": "soul text", "ok": True})

        manager = make_manager(handler, tmp_path)
        assert manager.read_soul(include_default=True) == "soul text"
        assert seen[-1].url.params["include_default"] == "true"
        manager.write_soul("new soul")
        assert seen[-1].method == "PUT"
        assert seen[-1].url.path == "/v1/soul"
        assert manager.read_identity() == "soul text"
        manager.write_identity("new identity")
        assert seen[-1].method == "PUT"
        assert seen[-1].url.path == "/v1/identity"

    def test_persona_project_scope_rejected(self, tmp_path: Path):
        manager = make_manager(ok_handler, tmp_path)
        with pytest.raises(ValueError):
            manager.read_soul(scope="project")
        with pytest.raises(ValueError):
            manager.write_identity("x", scope="project")

    def test_facts_crud(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "GET" and request.url.path == "/v1/facts/python_version":
                return httpx.Response(200, json={"fact": {"key": "python_version", "value": "3.12"}})
            if request.method == "GET" and request.url.path == "/v1/facts":
                return httpx.Response(200, json={"facts": [{"key": "a", "value": "1"}]})
            if request.method == "GET" and request.url.path == "/v1/facts/search":
                return httpx.Response(200, json={"facts": [{"key": "b", "value": "2"}]})
            return httpx.Response(200, json={"ok": True, "deleted": True})

        manager = make_manager(handler, tmp_path)
        manager.upsert_fact("python_version", "3.12", category="config", scope="project")
        request = seen[-1]
        assert request.method == "PUT"
        assert request.url.path == "/v1/facts/python_version"
        body = json.loads(request.content)
        assert body["value"] == "3.12"
        assert body["category"] == "config"
        assert body["scope"] == "project"
        assert "key" not in body

        assert manager.get_fact("python_version", scope="project") == {"key": "python_version", "value": "3.12"}
        assert seen[-1].url.params["scope"] == "project"

        assert manager.list_facts(category="config", limit=10) == [{"key": "a", "value": "1"}]
        assert seen[-1].url.params["category"] == "config"
        assert seen[-1].url.params["limit"] == "10"

        assert manager.search_facts("python") == [{"key": "b", "value": "2"}]
        assert seen[-1].url.params["q"] == "python"

        assert manager.delete_fact("python_version") is True
        assert seen[-1].method == "DELETE"

    def test_fact_key_url_quoted(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"fact": None, "ok": True})

        manager = make_manager(handler, tmp_path)
        manager.get_fact("a/b c")
        assert seen[-1].url.raw_path.startswith(b"/v1/facts/a%2Fb%20c")
        manager.upsert_fact("a/b c", "v")
        assert seen[-1].url.raw_path.startswith(b"/v1/facts/a%2Fb%20c")

    def test_get_stats(self, tmp_path: Path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"user": {"has_long_term": True}, "project": {}})

        manager = make_manager(handler, tmp_path)
        stats = manager.get_stats()
        assert stats["user"] == {"has_long_term": True}
        assert stats["project"] == {}

    def test_project_root_passed_through(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True, "content": ""})

        manager = make_manager(handler, tmp_path, project_root=Path("/repo"))
        manager.read_long_term(scope="project")
        assert seen[-1].url.params["project_root"] == "/repo"
        manager.write_long_term("x", scope="project")
        assert json.loads(seen[-1].content)["project_root"] == "/repo"

    def test_auth_header(self, tmp_path: Path):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"content": ""})

        manager = make_manager(handler, tmp_path, token="secret-token")
        manager.read_long_term()
        assert seen[0].headers["Authorization"] == "Bearer secret-token"


# --- Tests: unreachable remote ---


class TestDegradation:
    def test_reads_degrade_to_empty(self, tmp_path: Path):
        manager = make_manager(down_handler, tmp_path)
        assert manager.read_long_term() == ""
        assert manager.get_memory_context() == ""
        assert manager.get_persona_context() == ""
        assert manager.read_soul() == ""
        assert manager.read_identity() == ""
        assert manager.search("anything") == []
        assert manager.get_fact("k") is None
        assert manager.list_facts() == []
        assert manager.search_facts("q") == []
        assert manager.get_stats() == {"user": {}, "project": {}}

    def test_http_error_status_degrades(self, tmp_path: Path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        manager = make_manager(handler, tmp_path)
        assert manager.read_long_term() == ""
        assert manager.write_long_term("x") is False

    def test_failed_write_is_staged(self, tmp_path: Path):
        manager = make_manager(down_handler, tmp_path)
        assert manager.write_long_term("v1", scope="project") is False
        outbox = tmp_path / "outbox.jsonl"
        entries = [json.loads(line) for line in outbox.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["op"] == "write_long_term"
        assert entries[0]["args"] == {"content": "v1", "scope": "project"}
        assert "queued_at" in entries[0]

    def test_staged_writes_flush_in_order_on_recovery(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ankaloop.memory_http._FLUSH_COOLDOWN_SECONDS", 0)
        state = {"up": False}
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not state["up"]:
                raise httpx.ConnectError("connection refused", request=request)
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        manager = make_manager(handler, tmp_path)
        assert manager.write_long_term("v1") is False
        manager.append_history("event-1", session_id="s1")
        assert seen == []

        state["up"] = True
        assert manager.write_long_term("v2") is True
        # The two staged writes replayed (in order) before the new write.
        assert [b.get("content") for b in seen] == ["v1", "event-1", "v2"]
        assert not (tmp_path / "outbox.jsonl").exists()

    def test_replay_stops_when_remote_still_down(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ankaloop.memory_http._FLUSH_COOLDOWN_SECONDS", 0)
        manager = make_manager(down_handler, tmp_path)
        assert manager.write_long_term("v1") is False
        assert manager.write_long_term("v2") is False
        entries = [json.loads(line) for line in (tmp_path / "outbox.jsonl").read_text().splitlines()]
        assert [e["args"]["content"] for e in entries] == ["v1", "v2"]

    def test_new_write_staged_while_older_writes_stuck(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Selective remote failure must not reorder writes.

        The remote keeps rejecting the staged PUT while it would accept the new
        POST; the new write must queue behind the stuck one, not jump ahead.
        """
        monkeypatch.setattr("ankaloop.memory_http._FLUSH_COOLDOWN_SECONDS", 0)
        state = {"long_term_up": False}
        posted: list[dict] = []
        put: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                if not state["long_term_up"]:
                    return httpx.Response(500, json={"error": "long-term store down"})
                put.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True})
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        manager = make_manager(handler, tmp_path)
        assert manager.write_long_term("old") is False
        manager.append_history("new-event", session_id="s1")
        # The POST was never dispatched: it queued behind the stuck PUT.
        assert posted == []
        entries = [json.loads(line) for line in (tmp_path / "outbox.jsonl").read_text().splitlines()]
        assert [e["op"] for e in entries] == ["write_long_term", "append_history"]

        state["long_term_up"] = True
        manager.append_history("later-event", session_id="s1")
        # Recovery replays everything in original order.
        assert [b["content"] for b in put] == ["old"]
        assert [b["content"] for b in posted] == ["new-event", "later-event"]
        assert not (tmp_path / "outbox.jsonl").exists()

    def test_outbox_capped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ankaloop.memory_http.MAX_OUTBOX_ENTRIES", 5)
        monkeypatch.setattr("ankaloop.memory_http._FLUSH_COOLDOWN_SECONDS", 10**9)
        manager = make_manager(down_handler, tmp_path)
        for i in range(8):
            manager.write_long_term(f"v{i}")
        entries = [json.loads(line) for line in (tmp_path / "outbox.jsonl").read_text().splitlines()]
        assert len(entries) == 5
        assert entries[0]["args"]["content"] == "v3"


# --- Tests: backend selection ---


def _config_with_memory(memory: MemoryConfig | None) -> AnkaloopConfig:
    return AnkaloopConfig(servers={}, memory=memory)


class TestBackendSelection:
    def test_http_backend_selected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda: _config_with_memory(MemoryConfig(backend="http", url="https://memory.test")),
        )
        manager = get_memory_manager(tmp_path)
        assert isinstance(manager, HttpMemoryManager)
        assert manager.base_url == "https://memory.test"

    def test_local_backend_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(config_module, "load_config", lambda: _config_with_memory(None))
        manager = get_memory_manager(tmp_path)
        assert type(manager) is MemoryManager

    def test_http_without_url_falls_back_to_local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda: _config_with_memory(MemoryConfig(backend="http", url=None)),
        )
        manager = get_memory_manager(tmp_path)
        assert type(manager) is MemoryManager

    def test_unknown_backend_falls_back_to_local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda: _config_with_memory(MemoryConfig(backend="carrier-pigeon")),
        )
        manager = get_memory_manager(tmp_path)
        assert type(manager) is MemoryManager

    def test_token_from_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda: _config_with_memory(MemoryConfig(backend="http", url="https://memory.test")),
        )
        monkeypatch.setenv("ANKA_MEMORY_TOKEN", "env-token")
        manager = get_memory_manager(tmp_path)
        assert isinstance(manager, HttpMemoryManager)
        assert manager._client.headers["Authorization"] == "Bearer env-token"

    def test_config_token_beats_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda: _config_with_memory(MemoryConfig(backend="http", url="https://memory.test", token="toml-token")),
        )
        monkeypatch.setenv("ANKA_MEMORY_TOKEN", "env-token")
        manager = get_memory_manager(tmp_path)
        assert isinstance(manager, HttpMemoryManager)
        assert manager._client.headers["Authorization"] == "Bearer toml-token"
