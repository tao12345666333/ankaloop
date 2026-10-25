"""Tests for the AnkaLoop hooks system."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ankaloop.hooks import (
    HookConfig,
    HookDecision,
    HookEvent,
    HookHandler,
    HookInput,
    HookOutput,
    HooksManager,
    get_hooks_manager,
    reset_hooks_manager,
    run_post_tool_use_hooks,
    run_pre_tool_use_hooks,
    run_user_prompt_hooks,
)


class TestHookHandler:
    """Tests for HookHandler matching."""

    def test_matches_star_pattern(self):
        """Test that * pattern matches all tools."""
        handler = HookHandler(matcher="*")
        assert handler.matches("read_file") is True
        assert handler.matches("write_file") is True
        assert handler.matches("bash") is True
        assert handler.matches(None) is True

    def test_matches_empty_pattern(self):
        """Test that empty pattern matches all tools."""
        handler = HookHandler(matcher="")
        assert handler.matches("read_file") is True
        assert handler.matches(None) is True

    def test_matches_exact_pattern(self):
        """Test exact string matching."""
        handler = HookHandler(matcher="read_file")
        assert handler.matches("read_file") is True
        assert handler.matches("write_file") is False
        assert handler.matches("read_file_extra") is False

    def test_matches_regex_pattern(self):
        """Test regex pattern matching."""
        handler = HookHandler(matcher="read_file|write_file")
        assert handler.matches("read_file") is True
        assert handler.matches("write_file") is True
        assert handler.matches("bash") is False

    def test_matches_regex_wildcard(self):
        """Test regex wildcard patterns."""
        handler = HookHandler(matcher="mcp__.*")
        assert handler.matches("mcp__exa__search") is True
        assert handler.matches("mcp__github__create_issue") is True
        assert handler.matches("read_file") is False


class TestHookInput:
    """Tests for HookInput serialization."""

    def test_to_json_basic(self):
        """Test basic JSON serialization."""
        hook_input = HookInput(
            session_id="test-session",
            hook_event_name="PreToolUse",
            cwd="/home/test",
        )
        data = json.loads(hook_input.to_json())
        assert data["session_id"] == "test-session"
        assert data["hook_event_name"] == "PreToolUse"
        assert data["cwd"] == "/home/test"

    def test_to_json_with_tool_info(self):
        """Test JSON serialization with tool information."""
        hook_input = HookInput(
            session_id="test-session",
            hook_event_name="PreToolUse",
            cwd="/home/test",
            tool_name="read_file",
            tool_input={"path": "/test/file.py"},
            tool_use_id="tool-123",
        )
        data = json.loads(hook_input.to_json())
        assert data["tool_name"] == "read_file"
        assert data["tool_input"]["path"] == "/test/file.py"
        assert data["tool_use_id"] == "tool-123"


class TestHookOutput:
    """Tests for HookOutput from exit codes."""

    def test_from_exit_code_success(self):
        """Test HookOutput from exit code 0."""
        output = HookOutput.from_exit_code(0, "success message", "")
        assert output.success is True
        assert output.continue_execution is True
        assert output.feedback == "success message"

    def test_from_exit_code_blocking_error(self):
        """Test HookOutput from exit code 2 (blocking error)."""
        output = HookOutput.from_exit_code(2, "", "access denied")
        assert output.success is False
        assert output.continue_execution is True  # Continues but with error
        assert output.decision == HookDecision.DENY
        assert output.decision_reason == "access denied"

    def test_from_exit_code_non_blocking_error(self):
        """Test HookOutput from other exit codes."""
        output = HookOutput.from_exit_code(1, "", "warning")
        assert output.success is False
        assert output.continue_execution is True

    def test_from_exit_code_json_output(self):
        """Test HookOutput parsing JSON from stdout."""
        json_output = json.dumps(
            {
                "continue": True,
                "feedback": "All checks passed",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "Approved by policy",
                },
            }
        )
        output = HookOutput.from_exit_code(0, json_output, "")
        assert output.success is True
        assert output.decision == HookDecision.ALLOW
        assert output.decision_reason == "Approved by policy"

    def test_from_exit_code_deny_decision(self):
        """Test HookOutput with deny decision."""
        json_output = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "File is in blocklist",
                }
            }
        )
        output = HookOutput.from_exit_code(0, json_output, "")
        assert output.decision == HookDecision.DENY
        assert output.decision_reason == "File is in blocklist"

    def test_legacy_ask_decision_is_denied(self):
        json_output = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                }
            }
        )
        output = HookOutput.from_exit_code(0, json_output, "")
        assert output.decision == HookDecision.DENY
        assert "unsupported" in output.decision_reason


class TestHooksManager:
    """Tests for HooksManager."""

    def setup_method(self):
        """Reset hooks manager before each test."""
        reset_hooks_manager()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure_mode", "continues"),
        [("open", True), ("closed", False)],
    )
    async def test_hook_failure_mode_is_explicit(self, tmp_path, failure_mode, continues):
        manager = HooksManager(tmp_path)
        manager._loaded = True
        manager.hooks[HookEvent.PRE_TOOL_USE] = HookConfig(
            handlers=[
                HookHandler(
                    type="python",
                    function="missing.module",
                    failure_mode=failure_mode,
                )
            ]
        )

        output = await manager.execute_hooks(
            HookEvent.PRE_TOOL_USE,
            HookInput(
                session_id="test",
                hook_event_name="PreToolUse",
                cwd=str(tmp_path),
                tool_name="bash",
                tool_input={"command": "true"},
            ),
            "bash",
        )

        assert output.continue_execution is continues
        if failure_mode == "closed":
            assert output.decision == HookDecision.DENY

    def test_load_toml_config(self):
        """Test loading hooks from TOML config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            amcp_dir = project_dir / ".ankaloop"
            amcp_dir.mkdir()

            # Create hooks.toml
            hooks_config = """
[hooks.PreToolUse]
[[hooks.PreToolUse.handlers]]
matcher = "write_file"
type = "command"
command = "echo test"
timeout = 10
enabled = true
"""
            (amcp_dir / "hooks.toml").write_text(hooks_config)

            manager = HooksManager(project_dir)
            handlers = manager.get_handlers(HookEvent.PRE_TOOL_USE, "write_file")

            assert len(handlers) == 1
            assert handlers[0].matcher == "write_file"
            assert handlers[0].command == "echo test"
            assert handlers[0].timeout == 10

    def test_load_json_config(self):
        """Test loading hooks from JSON config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            amcp_dir = project_dir / ".ankaloop"
            amcp_dir.mkdir()

            # Create hooks.json
            hooks_config = {
                "hooks": {
                    "PostToolUse": {
                        "handlers": [
                            {
                                "matcher": "*",
                                "type": "python",
                                "script": "./test.py",
                                "timeout": 5,
                                "enabled": True,
                            }
                        ]
                    }
                }
            }
            (amcp_dir / "hooks.json").write_text(json.dumps(hooks_config))

            manager = HooksManager(project_dir)
            handlers = manager.get_handlers(HookEvent.POST_TOOL_USE, "any_tool")

            assert len(handlers) == 1
            assert handlers[0].type == "python"

    def test_disabled_handler_not_returned(self):
        """Test that disabled handlers are not returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            amcp_dir = project_dir / ".ankaloop"
            amcp_dir.mkdir()

            hooks_config = """
[hooks.PreToolUse]
[[hooks.PreToolUse.handlers]]
matcher = "write_file"
type = "command"
command = "echo test"
enabled = false
"""
            (amcp_dir / "hooks.toml").write_text(hooks_config)

            manager = HooksManager(project_dir)
            handlers = manager.get_handlers(HookEvent.PRE_TOOL_USE, "write_file")

            assert len(handlers) == 0

    def test_user_hooks_come_from_config_dir(self, tmp_path, monkeypatch):
        """User-level hooks resolve through the shared CONFIG_DIR constant.

        ``hooks.py`` used to reassemble the user config dir from
        ``XDG_CONFIG_HOME`` at load time instead of importing
        ``constants.CONFIG_DIR`` like the other modules. Anything that
        rebinds the config dir the way ``constants.CONFIG_DIR`` consumers do
        (memory, sessions, event bus, …) would silently keep its hooks — or
        lose them — because hooks kept reading the default XDG location.
        """
        rebound = tmp_path / "rebound" / "ankaloop"
        rebound.mkdir(parents=True)
        (rebound / "hooks.toml").write_text(
            """
[hooks.PreToolUse]
[[hooks.PreToolUse.handlers]]
matcher = "write_file"
type = "command"
command = "echo rebound-toml"
enabled = true
"""
        )
        hooks_dir = rebound / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "warn-bash.md").write_text(
            "---\nname: warn-bash\nenabled: true\nevent: bash\naction: warn\n---\n\nCareful with bash.\n"
        )
        monkeypatch.setattr("ankaloop.hooks.CONFIG_DIR", rebound)

        manager = HooksManager(tmp_path)

        toml_handlers = manager.get_handlers(HookEvent.PRE_TOOL_USE, "write_file")
        assert [h.command for h in toml_handlers] == ["echo rebound-toml"]
        markdown_handlers = manager.get_handlers(HookEvent.PRE_TOOL_USE, "bash")
        assert any(h.name == "warn-bash" for h in markdown_handlers)


class TestHookExecution:
    """Tests for hook execution."""

    def setup_method(self):
        """Reset hooks manager before each test."""
        reset_hooks_manager()

    @pytest.mark.asyncio
    async def test_execute_command_hook_success(self):
        """Test executing a command hook that succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            amcp_dir = project_dir / ".ankaloop"
            amcp_dir.mkdir()

            # Use JSON config for complex commands with embedded JSON output
            hooks_config = {
                "hooks": {
                    "PreToolUse": {
                        "handlers": [
                            {
                                "matcher": "*",
                                "type": "command",
                                "command": 'echo {"feedback": "Hook executed"}',
                                "timeout": 5,
                                "enabled": True,
                            }
                        ]
                    }
                }
            }
            (amcp_dir / "hooks.json").write_text(json.dumps(hooks_config))

            output = await run_pre_tool_use_hooks(
                session_id="test-session",
                tool_name="read_file",
                tool_input={"path": "/test"},
                project_dir=project_dir,
            )

            assert output.feedback is not None
            assert "Hook executed" in output.feedback

    @pytest.mark.asyncio
    async def test_execute_hook_deny(self):
        """Test executing a hook that denies tool execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            amcp_dir = project_dir / ".ankaloop"
            amcp_dir.mkdir()

            # Create a hook that denies writes
            script_content = """#!/usr/bin/env python3
import json
import sys
input_data = json.load(sys.stdin)
if input_data.get("tool_name") == "write_file":
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Writes are blocked"
        }
    }
    print(json.dumps(output))
"""
            script_path = amcp_dir / "deny_writes.py"
            script_path.write_text(script_content)
            script_path.chmod(0o755)

            hooks_config = f"""
[hooks.PreToolUse]
[[hooks.PreToolUse.handlers]]
matcher = "write_file"
type = "python"
script = "{script_path}"
timeout = 5
enabled = true
"""
            (amcp_dir / "hooks.toml").write_text(hooks_config)

            output = await run_pre_tool_use_hooks(
                session_id="test-session",
                tool_name="write_file",
                tool_input={"path": "/test", "content": "test"},
                project_dir=project_dir,
            )

            assert output.decision == HookDecision.DENY
            assert "blocked" in output.decision_reason.lower()

    @pytest.mark.asyncio
    async def test_no_hooks_returns_default(self):
        """Test that no hooks returns default output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            output = await run_pre_tool_use_hooks(
                session_id="test-session",
                tool_name="read_file",
                tool_input={"path": "/test"},
                project_dir=project_dir,
            )

            assert output.success is True
            assert output.continue_execution is True
            assert output.decision == HookDecision.CONTINUE

    @pytest.mark.asyncio
    async def test_user_prompt_hooks(self):
        """Test UserPromptSubmit hooks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            amcp_dir = project_dir / ".ankaloop"
            amcp_dir.mkdir()

            # Use JSON config for complex commands with embedded JSON output
            hooks_config = {
                "hooks": {
                    "UserPromptSubmit": {
                        "handlers": [
                            {
                                "matcher": "",
                                "type": "command",
                                "command": 'echo {"feedback": "Prompt received"}',
                                "timeout": 5,
                                "enabled": True,
                            }
                        ]
                    }
                }
            }
            (amcp_dir / "hooks.json").write_text(json.dumps(hooks_config))

            output = await run_user_prompt_hooks(
                session_id="test-session",
                prompt="Hello, agent!",
                project_dir=project_dir,
            )

            assert output.feedback is not None


class TestEnvironmentVariables:
    """Tests for environment variable substitution."""

    def setup_method(self):
        """Reset hooks manager before each test."""
        reset_hooks_manager()

    def test_amcp_project_dir_substitution(self):
        """Test $ANKA_PROJECT_DIR substitution in commands."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            amcp_dir = project_dir / ".ankaloop"
            amcp_dir.mkdir()

            hooks_config = """
[hooks.PreToolUse]
[[hooks.PreToolUse.handlers]]
matcher = "*"
type = "command"
command = "echo $ANKA_PROJECT_DIR"
timeout = 5
enabled = true
"""
            (amcp_dir / "hooks.toml").write_text(hooks_config)

            manager = HooksManager(project_dir)
            handlers = manager.get_handlers(HookEvent.PRE_TOOL_USE, "any")

            # The $ANKA_PROJECT_DIR should be available in the command's environment
            assert len(handlers) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
