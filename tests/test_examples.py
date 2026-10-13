"""Tests that keep shipped examples executable and in sync with runtime schemas."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.agent import Agent
from ankaloop.agent_spec import load_agent_spec
from ankaloop.commands import CommandManager
from ankaloop.config import AnkaloopConfig, ChatConfig, ContextConfig, ServerConfig, _decode_server_config
from ankaloop.hooks import HookOutput, HooksManager
from ankaloop.llm import LLMResponse
from ankaloop.tools import create_default_tool_registry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_all_example_agent_specs_load() -> None:
    """Every example YAML agent spec should parse as a real AgentSpec."""
    agent_specs = sorted(EXAMPLES.rglob("agents/*.yaml")) + sorted((EXAMPLES / "agents").glob("*.yaml"))
    assert agent_specs

    for spec_path in agent_specs:
        spec = load_agent_spec(spec_path)
        assert spec.name
        assert spec.system_prompt
        assert spec.exclude_tools is not None


def test_exclude_tools_is_valid_agent_spec_field() -> None:
    """Document the audit finding: examples use exclude_tools, which is the correct field."""
    spec = load_agent_spec(EXAMPLES / "agents" / "security-auditor.yaml")
    assert "write_file" in spec.exclude_tools


def test_example_hook_toml_configs_load(tmp_path: Path) -> None:
    """Every example TOML hook config should load through HooksManager."""
    hook_files = sorted((EXAMPLES / "hooks").glob("*.toml"))
    assert hook_files

    for hook_file in hook_files:
        project = tmp_path / hook_file.stem
        hooks_dir = project / ".ankaloop"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.toml").write_text(hook_file.read_text(), encoding="utf-8")

        manager = HooksManager(project_dir=project)
        manager.load_config()

        assert manager.hooks, f"no hooks loaded from {hook_file}"


def test_example_command_toml_configs_load(tmp_path: Path) -> None:
    """Every example TOML command should load through CommandManager."""
    commands_dir = tmp_path / ".ankaloop" / "commands"
    commands_dir.mkdir(parents=True)
    for command_file in (EXAMPLES / "commands").rglob("*.toml"):
        destination = commands_dir / command_file.relative_to(EXAMPLES / "commands")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(command_file.read_text(), encoding="utf-8")

    manager = CommandManager()
    manager.discover_commands(project_root=tmp_path)

    for command_file in (EXAMPLES / "commands").rglob("*.toml"):
        expected_name = ":".join(command_file.relative_to(EXAMPLES / "commands").with_suffix("").parts)
        command = manager.get_command(expected_name)
        assert command is not None, f"missing command {expected_name}"
        result = manager.execute_command(command, "example", project_root=tmp_path)
        assert result.type == "submit_prompt"
        assert result.content


def test_readme_commands_smoke_parse() -> None:
    """README command examples should reference real CLI commands and existing example files."""
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")

    expected_files = [
        "examples/agents/web-developer.yaml",
        "examples/hooks/automated-testing.toml",
        "examples/commands",
        "examples/skills",
    ]
    for path in expected_files:
        assert (ROOT / path).exists(), f"README references missing example path: {path}"

    assert "anka init" in readme
    assert "anka --agent web-developer" in readme
    assert "anka --agent examples/agents/web-developer.yaml" in readme
    assert "/delegate" not in readme


def test_workflows_do_not_reference_removed_delegate_command() -> None:
    """Workflow examples should describe the task tool, not a removed command."""
    for workflow in (EXAMPLES / "workflows").glob("*.md"):
        assert "/delegate" not in workflow.read_text(encoding="utf-8")


def test_example_server_toml_config_decodes() -> None:
    """The documented TOML server shape should match the real config decoder."""
    config = _decode_server_config(
        {
            "host": "127.0.0.1",
            "port": 8080,
            "cors": {
                "enabled": True,
                "allow_origins": [
                    "http://localhost:*",
                    "http://127.0.0.1:*",
                    "tauri://localhost",
                ],
                "allow_methods": ["*"],
                "allow_headers": ["*"],
            },
            "auth": {
                "enabled": True,
                "api_key": "amcp_test",
            },
        }
    )

    assert config is not None
    assert config.host == "127.0.0.1"
    assert config.port == 8080
    assert config.auth.enabled is True
    assert config.auth.api_key == "amcp_test"


@pytest.mark.asyncio
async def test_web_development_workflow_task_tool_runs_with_mock_provider(tmp_path: Path) -> None:
    """Run one multi-agent workflow path with a mock provider and the real task tool."""

    class ParentMockLLM:
        model = "mock-model"

        def __init__(self) -> None:
            self.calls = 0

        async def achat(self, messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="Delegating security review.",
                    tool_calls=[
                        {
                            "id": "call_task_1",
                            "name": "task",
                            "arguments": json.dumps(
                                {
                                    "action": "create",
                                    "description": "Review the authentication system for security issues.",
                                    "agent_type": "explorer",
                                }
                            ),
                        }
                    ],
                )
            if self.calls == 2:
                tool_messages = [message for message in messages if message.get("role") == "tool"]
                assert tool_messages
                assert "Task created successfully" in tool_messages[-1]["content"]
                return LLMResponse(content="Security review delegated successfully.")
            raise AssertionError("unexpected parent mock LLM call")

    async def fake_subagent_run(self, user_input, work_dir=None, **_kwargs):
        assert "authentication system" in user_input
        assert work_dir == tmp_path
        return "Mock security review complete."

    registry = create_default_tool_registry(enable_task=True)
    task_spec = registry.get_tool_spec("task")
    assert task_spec is not None

    agent_spec = load_agent_spec(EXAMPLES / "agents" / "web-developer.yaml")
    cfg = AnkaloopConfig(
        servers={},
        chat=ChatConfig(model="mock-model", request_timeout_seconds=5, max_retries=0),
        context=ContextConfig(progressive_tools=False, progressive_skills=False),
        server=ServerConfig(),
    )

    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=cfg),
        patch("ankaloop.agent.run_pre_tool_use_hooks", new=AsyncMock(return_value=HookOutput())),
        patch("ankaloop.agent.run_post_tool_use_hooks", new=AsyncMock(return_value=HookOutput())),
        patch("ankaloop.agent.Agent._maybe_run_periodic_memory_review", new=AsyncMock(return_value=None)),
        patch("ankaloop.agent.Agent._run_memory_review", new=AsyncMock(return_value=False)),
        patch("ankaloop.agent.Agent.run", new=fake_subagent_run),
    ):
        agent = Agent(agent_spec=agent_spec, session_id="example-session")
        result = await agent._enhanced_chat_with_tools(
            llm_client=ParentMockLLM(),
            messages=[{"role": "user", "content": "Create a secure authentication system."}],
            tools=[task_spec],
            tool_registry={},
            stream=False,
            status=MagicMock(),
            work_dir=tmp_path,
            cfg=cfg,
        )
        await agent.close()

    assert result == "Security review delegated successfully."
