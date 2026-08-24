from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ankaloop.agent import Agent
from ankaloop.cli import app
from ankaloop.config import AnkaloopConfig, ChatConfig, ContextConfig, Server
from ankaloop.tool_execution import ToolCapability, ToolExecutionContext, ToolExecutor
from ankaloop.tools import create_default_tool_registry

PARALLEL_URL = "https://search.parallel.ai/mcp"
SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"objective": {"type": "string"}, "search_queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["objective", "search_queries"],
}
FETCH_SCHEMA = {
    "type": "object",
    "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
    "required": ["urls"],
}


def test_setup_parallel_is_explicit_and_preserves_existing_servers(monkeypatch, tmp_path):
    cfg = AnkaloopConfig(
        servers={"custom": Server(url="https://mcp.example.com", headers={"X-User": "kept"})}, chat=None
    )
    monkeypatch.setattr("ankaloop.cli.load_config", lambda: cfg)
    with patch("ankaloop.cli.save_config", return_value=tmp_path / "config.toml") as save:
        result = CliRunner().invoke(app, ["mcp", "setup-parallel"])
    assert result.exit_code == 0
    assert cfg.servers["custom"].headers == {"X-User": "kept"}
    assert cfg.servers["parallel-search"] == Server(url=PARALLEL_URL)
    save.assert_called_once_with(cfg)
    assert "search objectives, search queries, and requested URLs" in result.stdout


@pytest.mark.parametrize("name", ["parallel", "parallel-search"])
def test_setup_parallel_refuses_user_owned_name_and_headers(monkeypatch, name):
    original = Server(url="https://user.example/mcp", headers={"Authorization": "Bearer user"})
    cfg = AnkaloopConfig(servers={name: original}, chat=None)
    monkeypatch.setattr("ankaloop.cli.load_config", lambda: cfg)
    with patch("ankaloop.cli.save_config") as save:
        result = CliRunner().invoke(app, ["mcp", "setup-parallel"])
    assert result.exit_code != 0
    assert cfg.servers == {name: original}
    save.assert_not_called()


@pytest.mark.asyncio
async def test_parallel_tools_reach_real_agent_context_and_exact_hosted_calls(monkeypatch, tmp_path):
    cfg = AnkaloopConfig(
        servers={"parallel-search": Server(url=PARALLEL_URL)},
        chat=ChatConfig(model="unknown-model", mcp_tools_enabled=True),
        context=ContextConfig(progressive_tools=False),
    )
    monkeypatch.setattr("ankaloop.agent.load_config", lambda: cfg)
    list_tools = AsyncMock(
        return_value=[
            {"name": "web_search", "description": "Search", "inputSchema": SEARCH_SCHEMA},
            {"name": "web_fetch", "description": "Fetch", "inputSchema": FETCH_SCHEMA},
        ]
    )
    monkeypatch.setattr("ankaloop.agent.list_mcp_tools", list_tools)
    tools, registry = await Agent()._build_tools_and_registry(user_input="research requested page")
    exposed = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
    assert exposed["mcp__parallel-search__web_search"] == SEARCH_SCHEMA
    assert exposed["mcp__parallel-search__web_fetch"] == FETCH_SCHEMA
    assert registry == {
        "mcp__parallel-search__web_search": ("parallel-search", "web_search"),
        "mcp__parallel-search__web_fetch": ("parallel-search", "web_fetch"),
    }
    assert list_tools.await_args.args[0] == Server(url=PARALLEL_URL)
    executor = ToolExecutor(
        context=ToolExecutionContext("session", tmp_path, "turn"),
        capability=ToolCapability.from_spec(None, [], True),
        exposed_tools=set(registry),
        registry=create_default_tool_registry(enable_task=False),
        mcp_registry=registry,
        config=cfg,
    )
    hosted_call = AsyncMock(return_value={"is_error": False, "content": [{"type": "text", "text": "ok"}]})
    search_args = {"objective": "Answer with current sources", "search_queries": ["requested topic primary sources"]}
    fetch_args = {"urls": ["https://example.com/requested"]}
    with patch("ankaloop.tool_execution.call_mcp_tool", hosted_call):
        assert (await executor.execute("mcp__parallel-search__web_search", search_args)).success
        assert (await executor.execute("mcp__parallel-search__web_fetch", fetch_args)).success
    assert hosted_call.await_args_list[0].args == (Server(url=PARALLEL_URL), "web_search", search_args)
    assert hosted_call.await_args_list[1].args == (
        Server(url=PARALLEL_URL),
        "web_fetch",
        {"urls": ["https://example.com/requested"]},
    )
