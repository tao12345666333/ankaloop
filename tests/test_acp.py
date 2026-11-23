"""Tests for the ACP (Agent Client Protocol) server adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ResourceLink,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
)
from rich.console import Console

from ankaloop import ui
from ankaloop.acp import server as acp_server
from ankaloop.acp.server import (
    ACPSessionAgent,
    _load_session_meta,
    _prompt_text,
    _save_session_meta,
    _SessionRecord,
    _title_from,
    _tool_kind,
    _tool_title,
)
from ankaloop.application_services import ApplicationServices
from ankaloop.client.embedded import EmbeddedClient
from ankaloop.runtime import TurnEvent, TurnStatus
from ankaloop.session_store import SessionStore


class FakeConn:
    """Records the session updates the agent sends to its client."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    def kinds(self) -> list[str]:
        return [update.session_update for _, update in self.updates]


class FakeTurn:
    """Stand-in turn whose outcome the test controls."""

    def __init__(self, events: list[TurnEvent], *, error: BaseException | None = None) -> None:
        self.id = "turn-1"
        self.events = events
        self.error = error
        self.cancelled = False
        self._queues: list[Any] = []

    def subscribe(self, *, max_bytes: int) -> Any:
        queue = asyncio.Queue()
        for event in self.events:
            queue.put_nowait(event)
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: Any) -> None:
        pass

    async def wait(self) -> str:
        # Yield once so the relay starts before the outcome lands.
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return "done"

    async def cancel(self) -> bool:
        self.cancelled = True
        return True


def _event(event_type: str, **data: Any) -> TurnEvent:
    return TurnEvent.output("turn-1", event_type, data)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point session and ACP metadata storage at a temporary directory."""
    monkeypatch.setattr(acp_server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("ankaloop.agent.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("ankaloop.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("ankaloop.config.CONFIG_FILE", tmp_path / "config.toml")
    return tmp_path


@pytest.fixture
def services() -> ApplicationServices:
    """Isolated application services for an embedded client."""
    return ApplicationServices.default()


@pytest.fixture
def agent(services: ApplicationServices, config_dir: Path) -> ACPSessionAgent:
    """An ACP agent wired to a fake client connection and temporary metadata."""
    del config_dir
    acp_agent = ACPSessionAgent(client=EmbeddedClient(services=services))
    acp_agent.on_connect(FakeConn())
    return acp_agent


def test_initialize_advertises_capabilities(agent: ACPSessionAgent) -> None:
    response = asyncio.run(agent.initialize(protocol_version=1))

    assert response.protocol_version == 1
    assert response.agent_info is not None
    assert response.agent_info.name == "ankaloop"
    capabilities = response.agent_capabilities
    assert capabilities is not None
    assert capabilities.load_session is True
    assert capabilities.session_capabilities is not None
    assert capabilities.session_capabilities.list is not None
    # Prompts are flattened to text, so image blocks must not be advertised.
    assert capabilities.prompt_capabilities is not None
    assert capabilities.prompt_capabilities.image is False


def test_new_session_creates_embedded_session(agent: ACPSessionAgent, tmp_path: Path) -> None:
    response = asyncio.run(agent.new_session(cwd=str(tmp_path)))

    assert response.session_id in agent._client._sessions
    assert agent._client._sessions[response.session_id].cwd == str(tmp_path)


def test_new_session_records_metadata(agent: ACPSessionAgent, config_dir: Path, tmp_path: Path) -> None:
    response = asyncio.run(agent.new_session(cwd=str(tmp_path)))

    meta = json.loads((config_dir / "acp-sessions.json").read_text())
    assert meta[response.session_id]["cwd"] == str(tmp_path)


def _write_chat_config(config_dir: Path, *, effort: str = "high", options: list[str] | None = None) -> None:
    lines = [
        "[chat]",
        'active_provider = "test"',
        f'reasoning_effort = "{effort}"',
        "",
        "[chat.providers.test]",
        'base_url = "https://example.test/v1"',
        'model = "test-model"',
        'api_type = "openai"',
        f'reasoning_effort = "{effort}"',
    ]
    if options is not None:
        quoted = ", ".join(f'"{value}"' for value in options)
        lines.append(f"reasoning_effort_options = [{quoted}]")
    (config_dir / "config.toml").write_text("\n".join(lines) + "\n")


def test_new_session_advertises_reasoning_effort(agent: ACPSessionAgent, config_dir: Path, tmp_path: Path) -> None:
    _write_chat_config(config_dir, effort="high", options=["low", "high", "max"])

    response = asyncio.run(agent.new_session(cwd=str(tmp_path)))

    option = response.config_options[0]
    assert option.id == "reasoning_effort"
    assert option.category == "thought_level"
    assert option.current_value == "high"
    assert [item.value for item in option.options] == ["low", "high", "max", "auto"]


def test_new_session_falls_back_to_global_reasoning_effort_levels(
    agent: ACPSessionAgent, config_dir: Path, tmp_path: Path
) -> None:
    _write_chat_config(config_dir, effort="high")

    response = asyncio.run(agent.new_session(cwd=str(tmp_path)))

    values = [item.value for item in response.config_options[0].options]
    assert "medium" in values
    assert values[-1] == "auto"


def test_set_reasoning_effort_is_per_session(agent: ACPSessionAgent, config_dir: Path, tmp_path: Path) -> None:
    _write_chat_config(config_dir, effort="high", options=["low", "high", "max"])
    first = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    second = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id

    response = asyncio.run(agent.set_config_option("reasoning_effort", first, "low"))

    assert response.config_options[0].current_value == "low"
    assert agent._sessions[first].reasoning_effort == "low"
    assert agent._sessions[second].reasoning_effort is None
    assert agent._client._sessions[first].agent.reasoning_effort_override == "low"
    assert agent._client._sessions[second].agent.reasoning_effort_override is None
    assert agent._client._sessions[first].agent._resolve_turn_config().chat.reasoning_effort == "low"
    assert agent._client._sessions[second].agent._resolve_turn_config().chat.reasoning_effort == "high"
    meta = json.loads((config_dir / "acp-sessions.json").read_text())
    assert meta[first]["reasoning_effort"] == "low"


def test_set_reasoning_effort_auto_clears_provider_effort(
    agent: ACPSessionAgent, config_dir: Path, tmp_path: Path
) -> None:
    _write_chat_config(config_dir, effort="high", options=["low", "high", "max"])
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id

    asyncio.run(agent.set_config_option("reasoning_effort", session_id, "auto"))

    embedded = agent._client._sessions[session_id].agent
    assert embedded.reasoning_effort_override == "auto"
    assert embedded._resolve_turn_config().chat.reasoning_effort is None


def test_set_reasoning_effort_rejects_unadvertised_value(
    agent: ACPSessionAgent, config_dir: Path, tmp_path: Path
) -> None:
    _write_chat_config(config_dir, effort="high", options=["low", "high", "max"])
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id

    with pytest.raises(RequestError):
        asyncio.run(agent.set_config_option("reasoning_effort", session_id, "medium"))


def test_title_skips_client_instruction_blocks() -> None:
    assert _title_from("<t3_code_instructions>be nice</t3_code_instructions> Fix calc") == "Fix calc"
    wrapped = (
        "<t3_code_instructions>be nice</t3_code_instructions> "
        "<user_request>Fix calc</user_request> <runtime_info>ignore me</runtime_info>"
    )
    assert _title_from(wrapped) == "Fix calc"


def test_metadata_save_merges_records_from_other_servers(config_dir: Path) -> None:
    first = _SessionRecord(cwd="/one", updated_at="2026-01-01T00:00:00+00:00")
    _save_session_meta({"s1": first})
    stale_snapshot = _load_session_meta()
    second = _SessionRecord(cwd="/two", updated_at="2026-01-01T00:00:02+00:00")
    _save_session_meta({"s2": second})

    updated = _SessionRecord(cwd="/one", title="updated", updated_at="2026-01-01T00:00:03+00:00")
    _save_session_meta({**stale_snapshot, "s1": updated})

    meta = json.loads((config_dir / "acp-sessions.json").read_text())
    assert set(meta) == {"s1", "s2"}
    assert meta["s1"]["title"] == "updated"


def test_list_sessions_sees_metadata_from_other_servers(
    agent: ACPSessionAgent, config_dir: Path, tmp_path: Path
) -> None:
    (config_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (config_dir / "sessions" / "other.json").write_text("{}")
    other = _SessionRecord(cwd=str(tmp_path), title="Other", updated_at="2026-01-01T00:00:00+00:00")
    _save_session_meta({**agent._sessions, "other": other})

    response = asyncio.run(agent.list_sessions(cwd=str(tmp_path)))

    assert "other" in {info.session_id for info in response.sessions}


def test_prompt_streams_text_reasoning_and_tools(
    agent: ACPSessionAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    monkeypatch.setattr(acp_server, "_FLUSH_INTERVAL", 0.0)
    turn = FakeTurn(
        [
            _event("message.reasoning", content="thinking"),
            _event("message.chunk", content="Hello"),
            _event("message.chunk", content=" world"),
            _event("tool.call_start", tool_name="bash", tool_id="t1", arguments={"command": "ls"}),
            _event("tool.call_complete", tool_name="bash", tool_id="t1"),
            _event("llm.usage", input_tokens=10, output_tokens=5, context_window=1000),
            TurnEvent.transition("turn-1", TurnStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(agent._client.services.session_service, "submit", _submit(turn), raising=True)

    response = asyncio.run(agent.prompt(session_id, [TextContentBlock(text="say hi")]))

    conn: FakeConn = agent._conn  # type: ignore[assignment]
    updates = [update for _, update in conn.updates]
    assert response.stop_reason == "end_turn"
    assert response.usage is not None
    assert (response.usage.input_tokens, response.usage.output_tokens) == (10, 5)
    assert isinstance(updates[0], AgentThoughtChunk)
    assert updates[0].content.text == "thinking"  # type: ignore[union-attr]
    assert isinstance(updates[1], AgentMessageChunk)
    assert updates[1].content.text == "Hello"  # type: ignore[union-attr]
    # The tail of the text is flushed when the turn ends.
    assert isinstance(updates[2], AgentMessageChunk)
    assert updates[2].content.text == " world"  # type: ignore[union-attr]
    assert isinstance(updates[3], ToolCallStart)
    assert updates[3].tool_call_id == "t1"
    assert updates[3].title == "ls"
    assert updates[3].kind == "execute"
    assert isinstance(updates[4], ToolCallProgress)
    assert updates[4].status == "completed"
    assert isinstance(updates[5], UsageUpdate)
    assert (updates[5].used, updates[5].size) == (15, 1000)


def test_usage_reports_context_occupancy(
    agent: ACPSessionAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    turn = FakeTurn(
        [
            _event("llm.usage", input_tokens=900, output_tokens=40, context_window=2000),
            _event("llm.usage", input_tokens=1800, output_tokens=30, context_window=2000),
            TurnEvent.transition("turn-1", TurnStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(agent._client.services.session_service, "submit", _submit(turn))

    response = asyncio.run(agent.prompt(session_id, [TextContentBlock(text="hi")]))

    conn: FakeConn = agent._conn  # type: ignore[assignment]
    usage = [(update.used, update.size) for _, update in conn.updates if isinstance(update, UsageUpdate)]
    # Occupancy, not a running total: each report replaces the previous one.
    assert usage == [(940, 2000), (1830, 2000)]
    assert response.usage is not None
    assert response.usage.total_tokens == 1830


def test_failed_tool_reports_failure(agent: ACPSessionAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    turn = FakeTurn(
        [
            _event("tool.call_start", tool_name="read_file", tool_id="t2", arguments={}),
            _event("tool.call_error", tool_name="read_file", tool_id="t2", error="missing"),
            TurnEvent.transition("turn-1", TurnStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(agent._client.services.session_service, "submit", _submit(turn))

    asyncio.run(agent.prompt(session_id, [TextContentBlock(text="read")]))

    conn: FakeConn = agent._conn  # type: ignore[assignment]
    progress = [update for _, update in conn.updates if isinstance(update, ToolCallProgress)]
    assert progress[0].status == "failed"
    assert progress[0].raw_output == "missing"


def test_completed_tool_with_failure_reports_failed(
    agent: ACPSessionAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    turn = FakeTurn(
        [
            _event("tool.call_start", tool_name="read_file", tool_id="t3", arguments={"path": "missing"}),
            _event("tool.call_complete", tool_name="read_file", tool_id="t3", success=False),
            TurnEvent.transition("turn-1", TurnStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(agent._client.services.session_service, "submit", _submit(turn))

    asyncio.run(agent.prompt(session_id, [TextContentBlock(text="read")]))

    conn: FakeConn = agent._conn  # type: ignore[assignment]
    progress = [update for _, update in conn.updates if isinstance(update, ToolCallProgress)]
    assert progress[0].status == "failed"


def test_prompt_failure_returns_refusal(
    agent: ACPSessionAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    turn = FakeTurn([], error=RuntimeError("provider down"))
    monkeypatch.setattr(agent._client.services.session_service, "submit", _submit(turn))

    response = asyncio.run(agent.prompt(session_id, [TextContentBlock(text="go")]))

    conn: FakeConn = agent._conn  # type: ignore[assignment]
    assert response.stop_reason == "refusal"
    last = conn.updates[-1][1]
    assert isinstance(last, AgentMessageChunk)
    assert "provider down" in last.content.text  # type: ignore[union-attr]


def test_cancel_reports_cancelled(agent: ACPSessionAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    turn = FakeTurn([])
    started = asyncio.Event()

    async def run() -> Any:
        async def submit(*args: Any, **kwargs: Any) -> FakeTurn:
            started.set()
            return turn

        monkeypatch.setattr(agent._client.services.session_service, "submit", submit)
        prompt = asyncio.create_task(agent.prompt(session_id, [TextContentBlock(text="long")]))
        await started.wait()
        while session_id not in agent._active:
            await asyncio.sleep(0)
        await agent.cancel(session_id)
        # The runtime ends the waiting prompt with a cancellation.
        prompt.cancel()
        return await prompt

    response = asyncio.run(run())

    assert turn.cancelled is True
    assert response.stop_reason == "cancelled"


def test_unknown_session_is_rejected(agent: ACPSessionAgent, tmp_path: Path) -> None:
    with pytest.raises(RequestError):
        asyncio.run(agent.prompt("missing", [TextContentBlock(text="hi")]))
    with pytest.raises(RequestError):
        asyncio.run(agent.load_session(cwd=str(tmp_path), session_id="missing"))


def test_load_session_replays_history(agent: ACPSessionAgent, config_dir: Path, tmp_path: Path) -> None:
    session_id = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    embedded_agent = agent._client._sessions[session_id].agent
    embedded_agent._session_store = SessionStore(config_dir / "sessions", session_id)
    embedded_agent._session_state.commit_turn(
        "turn-1",
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]},
        ],
    )
    embedded_agent._session_state.commit_turn(
        "turn-2",
        [{"role": "user", "content": "second question"}, {"role": "assistant", "content": "second answer"}],
    )
    embedded_agent._persist_session_state(embedded_agent._session_state)
    assert (config_dir / "sessions" / f"{session_id}.json").exists()
    asyncio.run(agent._client.delete_session(session_id))

    asyncio.run(agent.load_session(cwd=str(tmp_path), session_id=session_id))

    conn: FakeConn = agent._conn  # type: ignore[assignment]
    replayed = [(update.session_update, update.content.text) for _, update in conn.updates]
    assert replayed == [
        ("user_message_chunk", "first question"),
        ("agent_message_chunk", "first answer"),
        ("user_message_chunk", "second question"),
        ("agent_message_chunk", "second answer"),
    ]
    assert session_id in agent._client._sessions


def test_list_sessions_requires_stored_transcript(agent: ACPSessionAgent, config_dir: Path, tmp_path: Path) -> None:
    kept = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    dropped = asyncio.run(agent.new_session(cwd=str(tmp_path))).session_id
    other = tmp_path / "other"
    other.mkdir()
    other_session = asyncio.run(agent.new_session(cwd=str(other))).session_id
    sessions_dir = config_dir / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    for session_id in (kept, other_session):
        (sessions_dir / f"{session_id}.json").write_text("{}")

    listed = asyncio.run(agent.list_sessions(cwd=str(tmp_path)))

    assert [session.session_id for session in listed.sessions] == [kept]
    assert dropped not in [session.session_id for session in listed.sessions]
    assert asyncio.run(agent.list_sessions(cwd=str(other))).sessions[0].session_id == (other_session)


def test_tool_kind_and_title() -> None:
    assert _tool_kind("read_file") == "read"
    assert _tool_kind("apply_patch") == "edit"
    assert _tool_kind("grep") == "search"
    assert _tool_kind("bash") == "execute"
    assert _tool_kind("web_fetch") == "fetch"
    assert _tool_kind("todo_write") == "edit"
    assert _tool_kind("delegate_task") == "other"
    assert _tool_title("bash", {"command": "git status"}) == "git status"
    assert _tool_title("bash", {}) == "bash"


def test_prompt_text_flattens_blocks() -> None:
    text = _prompt_text(
        [
            TextContentBlock(text="look here"),
            ResourceLink(name="README", uri="file:///README.md"),
        ]
    )

    assert text == "look here\n\n[README](file:///README.md)"


def test_default_client_keeps_stdout_for_protocol(config_dir: Path) -> None:
    acp_agent = ACPSessionAgent()

    assert acp_agent._client._console is not None
    assert acp_agent._client._console.stderr is True


def test_embedded_client_passes_console_to_agents(services: ApplicationServices, tmp_path: Path) -> None:
    console = Console(stderr=True)
    client = EmbeddedClient(services=services, console=console)

    asyncio.run(client.create_session(cwd=str(tmp_path), session_id="console-test"))

    assert client._sessions["console-test"].agent.console is console
    asyncio.run(client.close())


def test_run_acp_agent_routes_tool_ui_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyAgent:
        async def close(self) -> None:
            pass

    async def fake_run_agent(agent: Any) -> None:
        del agent

    monkeypatch.setattr(acp_server, "ACPSessionAgent", lambda **kwargs: DummyAgent())
    monkeypatch.setattr(acp_server, "run_agent", fake_run_agent)
    monkeypatch.setattr(ui, "console", ui.console)

    asyncio.run(acp_server.run_acp_agent())

    assert ui.console.stderr is True


def _submit(turn: FakeTurn) -> Any:
    async def submit(*args: Any, **kwargs: Any) -> FakeTurn:
        return turn

    return submit
