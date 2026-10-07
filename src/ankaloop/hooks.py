"""
Hooks System for AnkaLoop.

This module provides a flexible hooks system inspired by Claude Code's hooks,
allowing users to extend and customize agent behavior through external commands
or Python scripts.

Configuration:
- Project-level: .ankaloop/hooks.toml
- User-level: ~/.config/ankaloop/hooks.toml

Hook Events:
- PreToolUse: Before tool execution (can modify input or deny execution)
- PostToolUse: After tool execution (can modify output or provide feedback)
- UserPromptSubmit: Before user prompt is processed
- SessionStart: When a new session begins
- SessionEnd: When a session ends
- Stop: When agent is about to stop
- PreCompact: Before context compaction

Example Configuration (hooks.toml):
    [hooks.PreToolUse]
    [[hooks.PreToolUse.handlers]]
    matcher = "bash|write_file"
    type = "command"
    command = "./scripts/validate-tool.sh"
    timeout = 30

    [[hooks.PreToolUse.handlers]]
    matcher = "*"
    type = "python"
    script = "./scripts/log_all_tools.py"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .constants import CONFIG_DIR_NAME, PROJECT_DIR_NAME
from .tracing import hook_env_vars, hook_span, set_attributes
from .tracing import semconv as sc

logger = logging.getLogger(__name__)


class HookEvent(Enum):
    """Types of hook events in the system."""

    PRE_TOOL_USE = "PreToolUse"
    """Triggered before a tool is executed. Can modify input or deny execution."""

    POST_TOOL_USE = "PostToolUse"
    """Triggered after a tool is executed. Can modify output or provide feedback."""

    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    """Triggered when user submits a prompt, before processing."""

    SESSION_START = "SessionStart"
    """Triggered when a new session begins."""

    SESSION_END = "SessionEnd"
    """Triggered when a session ends."""

    STOP = "Stop"
    """Triggered when agent is about to stop."""

    PRE_COMPACT = "PreCompact"
    """Triggered before context compaction."""

    NOTIFICATION = "Notification"
    """Triggered for system notifications."""


class HookDecision(Enum):
    """Decisions that hooks can make for tool execution."""

    ALLOW = "allow"
    """Allow the tool to execute (bypass permission checks)."""

    DENY = "deny"
    """Deny the tool execution."""

    CONTINUE = "continue"
    """Continue with normal processing (default)."""


@dataclass
class HookInput:
    """Input data passed to hooks.

    Attributes:
        session_id: Current session ID
        hook_event_name: Name of the hook event
        cwd: Current working directory
        tool_name: Name of the tool (for tool-related events)
        tool_input: Tool input parameters (for PreToolUse)
        tool_response: Tool response (for PostToolUse)
        prompt: User prompt (for UserPromptSubmit)
        message: Notification message (for Notification events)
        metadata: Additional metadata
    """

    session_id: str
    hook_event_name: str
    cwd: str
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_response: dict[str, Any] | None = None
    tool_use_id: str | None = None
    prompt: str | None = None
    message: str | None = None
    notification_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Convert to JSON for passing to external commands."""
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "hook_event_name": self.hook_event_name,
            "cwd": self.cwd,
        }
        if self.tool_name:
            data["tool_name"] = self.tool_name
        if self.tool_input:
            data["tool_input"] = self.tool_input
        if self.tool_response:
            data["tool_response"] = self.tool_response
        if self.tool_use_id:
            data["tool_use_id"] = self.tool_use_id
        if self.prompt:
            data["prompt"] = self.prompt
        if self.message:
            data["message"] = self.message
        if self.notification_type:
            data["notification_type"] = self.notification_type
        if self.metadata:
            data["metadata"] = self.metadata
        return json.dumps(data, ensure_ascii=False)


@dataclass
class HookOutput:
    """Output from a hook execution.

    Attributes:
        success: Whether the hook executed successfully
        continue_execution: Whether to continue with normal processing
        stop_reason: Reason for stopping if continue_execution is False
        decision: Decision for tool execution (PreToolUse only)
        decision_reason: Reason for the decision
        updated_input: Modified tool input (PreToolUse only)
        updated_response: Modified tool response (PostToolUse only)
        feedback: Feedback message to show to the model
        system_message: System message to show to the user
        suppress_output: Whether to suppress output in transcript
        exit_code: Exit code from command execution
        stdout: Standard output from command
        stderr: Standard error from command
    """

    success: bool = True
    continue_execution: bool = True
    stop_reason: str | None = None
    decision: HookDecision = HookDecision.CONTINUE
    decision_reason: str | None = None
    updated_input: dict[str, Any] | None = None
    updated_response: dict[str, Any] | None = None
    feedback: str | None = None
    system_message: str | None = None
    suppress_output: bool = False
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    @classmethod
    def from_exit_code(cls, exit_code: int, stdout: str, stderr: str) -> HookOutput:
        """Create HookOutput from command exit code.

        Exit code behaviors:
        - 0: Success, stdout is processed for JSON or used as feedback
        - 2: Blocking error, stderr is used as error message
        - Other: Non-blocking error, execution continues
        """
        output = cls(exit_code=exit_code, stdout=stdout, stderr=stderr)

        if exit_code == 0:
            output.success = True
            output.continue_execution = True
            # Try to parse JSON from stdout
            if stdout.strip():
                try:
                    data = json.loads(stdout)
                    output._apply_json_output(data)
                except json.JSONDecodeError:
                    # Not JSON, use as feedback
                    output.feedback = stdout.strip()
        elif exit_code == 2:
            # Blocking error
            output.success = False
            output.continue_execution = True  # Continue but with error feedback
            output.feedback = stderr.strip() if stderr else "Hook returned blocking error"
            output.decision = HookDecision.DENY
            output.decision_reason = stderr.strip() if stderr else "Hook denied"
        else:
            # Non-blocking error
            output.success = False
            output.continue_execution = True
            if stderr:
                logger.warning(f"Hook failed with non-blocking status code: {stderr}")

        return output

    def _apply_json_output(self, data: dict[str, Any]) -> None:
        """Apply JSON output from hook to this HookOutput."""
        # Common fields
        if "continue" in data:
            self.continue_execution = data["continue"]
        if "stopReason" in data:
            self.stop_reason = data["stopReason"]
        if "suppressOutput" in data:
            self.suppress_output = data["suppressOutput"]
        if "systemMessage" in data:
            self.system_message = data["systemMessage"]
        if "feedback" in data:
            self.feedback = data["feedback"]

        # Hook-specific output
        hook_output = data.get("hookSpecificOutput", {})
        if hook_output:
            hook_event = hook_output.get("hookEventName", "")

            if hook_event == "PreToolUse":
                decision_str = hook_output.get("permissionDecision", "continue")
                self.decision_reason = hook_output.get("permissionDecisionReason")
                normalized_decision = str(decision_str).lower()
                if normalized_decision == "ask":
                    self.decision = HookDecision.DENY
                    self.decision_reason = (
                        self.decision_reason or "Hook approval requests are unsupported; the tool was denied"
                    )
                else:
                    try:
                        self.decision = HookDecision(normalized_decision)
                    except ValueError:
                        self.decision = HookDecision.DENY
                        self.decision_reason = (
                            self.decision_reason or f"Unknown hook permission decision: {decision_str}"
                        )
                if "updatedInput" in hook_output:
                    self.updated_input = hook_output["updatedInput"]

            elif hook_event == "PostToolUse":
                if hook_output.get("decision") == "block":
                    self.decision = HookDecision.DENY
                    self.decision_reason = hook_output.get("reason")
                if "updatedResponse" in hook_output:
                    self.updated_response = hook_output["updatedResponse"]

            elif hook_event == "Stop":
                if hook_output.get("decision") == "block":
                    self.continue_execution = True  # Override stop


@dataclass
class HookHandler:
    """A configured hook handler.

    Attributes:
        matcher: Pattern to match tool names (regex or simple string)
        type: Handler type - "command", "python", or "markdown"
        command: Shell command to execute (for command type)
        script: Python script path (for python type)
        function: Python function path (for python type, e.g., "module.function")
        timeout: Timeout in seconds for hook execution
        enabled: Whether this handler is enabled
        failure_mode: Whether execution failures are allowed ("open") or block ("closed")
        action: Action for markdown hooks - "warn" or "block"
        pattern: Regex pattern for markdown hooks
        message: Message to display (for markdown hooks)
        conditions: Advanced conditions for matching (for markdown hooks)
        name: Unique name for the handler (for markdown hooks)
    """

    matcher: str = "*"
    type: str = "command"
    command: str | None = None
    script: str | None = None
    function: str | None = None
    timeout: int = 30
    enabled: bool = True
    failure_mode: str = "open"
    # Markdown hook specific fields
    action: str = "warn"  # "warn" or "block"
    pattern: str | None = None
    message: str | None = None
    conditions: list[dict[str, str]] | None = None
    name: str | None = None

    def matches(self, tool_name: str | None) -> bool:
        """Check if this handler matches the given tool name.

        Args:
            tool_name: Tool name to match against

        Returns:
            True if the handler should process this tool
        """
        if not tool_name:
            return self.matcher in ("*", "")

        if self.matcher in ("*", ""):
            return True

        # Try regex match
        try:
            pattern = re.compile(f"^({self.matcher})$")
            return pattern.match(tool_name) is not None
        except re.error:
            # Fall back to simple string match
            return self.matcher == tool_name


@dataclass
class HookConfig:
    """Configuration for a hook event.

    Attributes:
        handlers: List of handlers for this event
    """

    handlers: list[HookHandler] = field(default_factory=list)


class HooksManager:
    """Manager for loading and executing hooks.

    This class handles:
    - Loading hook configurations from files
    - Matching hooks to tool calls
    - Executing hooks and processing their output
    """

    def __init__(self, project_dir: Path | None = None):
        """Initialize the hooks manager.

        Args:
            project_dir: Project directory for project-level hooks
        """
        self.project_dir = project_dir or Path.cwd()
        self.hooks: dict[HookEvent, HookConfig] = {}
        self._loaded = False

    def load_config(self) -> None:
        """Load hook configurations from all sources."""
        if self._loaded:
            return

        # Load user-level config
        user_config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / CONFIG_DIR_NAME
        user_hooks_file = user_config_dir / "hooks.toml"
        if user_hooks_file.exists():
            self._load_config_file(user_hooks_file)

        # Load user-level markdown hooks
        user_hooks_dir = user_config_dir / "hooks"
        if user_hooks_dir.exists():
            self._load_markdown_hooks_dir(user_hooks_dir)

        # Load project-level config (overrides user config)
        project_hooks_file = self.project_dir / PROJECT_DIR_NAME / "hooks.toml"
        if project_hooks_file.exists():
            self._load_config_file(project_hooks_file)

        # Also check for hooks.json for flexibility
        project_hooks_json = self.project_dir / PROJECT_DIR_NAME / "hooks.json"
        if project_hooks_json.exists():
            self._load_json_config_file(project_hooks_json)

        # Load project-level markdown hooks
        project_hooks_dir = self.project_dir / PROJECT_DIR_NAME / "hooks"
        if project_hooks_dir.exists():
            self._load_markdown_hooks_dir(project_hooks_dir)

        # Load hooks from plugins directories
        self._load_plugin_hooks()

        self._loaded = True

    def _load_config_file(self, config_file: Path) -> None:
        """Load hooks from a TOML config file."""
        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib  # type: ignore

            with open(config_file, "rb") as f:
                data = tomllib.load(f)

            hooks_data = data.get("hooks", {})
            self._parse_hooks_data(hooks_data, config_file.parent)

        except Exception as e:
            logger.warning(f"Failed to load hooks config from {config_file}: {e}")

    def _load_json_config_file(self, config_file: Path) -> None:
        """Load hooks from a JSON config file."""
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)

            hooks_data = data.get("hooks", {})
            self._parse_hooks_data(hooks_data, config_file.parent)

        except Exception as e:
            logger.warning(f"Failed to load hooks config from {config_file}: {e}")

    def _parse_hooks_data(self, hooks_data: dict[str, Any], base_dir: Path) -> None:
        """Parse hooks data from config."""
        for event_name, event_config in hooks_data.items():
            try:
                event = HookEvent(event_name)
            except ValueError:
                logger.warning(f"Unknown hook event: {event_name}")
                continue

            if event not in self.hooks:
                self.hooks[event] = HookConfig()

            handlers_data = event_config.get("handlers", [])
            if isinstance(handlers_data, list):
                for handler_data in handlers_data:
                    handler = HookHandler(
                        matcher=handler_data.get("matcher", "*"),
                        type=handler_data.get("type", "command"),
                        command=handler_data.get("command"),
                        script=handler_data.get("script"),
                        function=handler_data.get("function"),
                        timeout=handler_data.get("timeout", 30),
                        enabled=handler_data.get("enabled", True),
                        failure_mode=self._normalize_failure_mode(
                            handler_data.get("failure_mode", "open"),
                            event,
                        ),
                    )
                    # Resolve relative paths
                    if handler.script and not Path(handler.script).is_absolute():
                        handler.script = str(base_dir / handler.script)
                    if handler.command:
                        # Replace $ANKA_PROJECT_DIR with the actual path
                        handler.command = handler.command.replace("$ANKA_PROJECT_DIR", str(self.project_dir))

                    self.hooks[event].handlers.append(handler)

    @staticmethod
    def _normalize_failure_mode(value: object, event: HookEvent) -> str:
        """Validate a configured hook failure mode."""
        failure_mode = str(value).strip().lower()
        if failure_mode not in {"open", "closed"}:
            logger.warning(
                "Invalid failure_mode %r for %s; defaulting to open",
                value,
                event.value,
            )
            return "open"
        return failure_mode

    def _load_markdown_hooks_dir(self, hooks_dir: Path) -> None:
        """Load Markdown hook files from a directory.

        Markdown hooks use YAML frontmatter for configuration and the body
        as the message to display.
        """
        try:
            for hook_file in hooks_dir.glob("*.md"):
                self._load_markdown_hook_file(hook_file)
        except Exception as e:
            logger.warning(f"Failed to load markdown hooks from {hooks_dir}: {e}")

    def _load_markdown_hook_file(self, hook_file: Path) -> None:
        """Load a single Markdown hook file.

        Format:
        ---
        name: hook-name
        enabled: true
        event: bash|file|stop|session_start|session_end|prompt
        action: warn|block
        pattern: regex_pattern
        conditions:
          - field: file_path
            operator: regex_match
            pattern: pattern
        ---

        Message body to display when hook triggers.
        """
        try:
            import yaml

            content = hook_file.read_text(encoding="utf-8")

            # Parse YAML frontmatter
            if not content.startswith("---"):
                return

            parts = content.split("---", 2)
            if len(parts) < 3:
                return

            frontmatter = yaml.safe_load(parts[1])
            message = parts[2].strip()

            if not frontmatter:
                return

            # Check if enabled
            if not frontmatter.get("enabled", True):
                return

            # Map event type
            event_map = {
                "bash": HookEvent.PRE_TOOL_USE,
                "file": HookEvent.PRE_TOOL_USE,
                "stop": HookEvent.STOP,
                "session_start": HookEvent.SESSION_START,
                "session_end": HookEvent.SESSION_END,
                "prompt": HookEvent.USER_PROMPT_SUBMIT,
                "pre_tool_use": HookEvent.PRE_TOOL_USE,
                "post_tool_use": HookEvent.POST_TOOL_USE,
            }

            event_str = frontmatter.get("event", "pre_tool_use").lower()
            event = event_map.get(event_str)
            if not event:
                logger.warning(f"Unknown event type in {hook_file}: {event_str}")
                return

            # Determine matcher based on event type
            if event_str == "bash":
                matcher = "bash"
            elif event_str == "file":
                matcher = "write_file|apply_patch"
            else:
                matcher = "*"

            # Create handler
            handler = HookHandler(
                name=frontmatter.get("name", hook_file.stem),
                matcher=matcher,
                type="markdown",
                enabled=frontmatter.get("enabled", True),
                action=frontmatter.get("action", "warn"),
                pattern=frontmatter.get("pattern"),
                message=message,
                conditions=frontmatter.get("conditions"),
                timeout=frontmatter.get("timeout", 30),
            )

            if event not in self.hooks:
                self.hooks[event] = HookConfig()

            self.hooks[event].handlers.append(handler)
            logger.debug(f"Loaded markdown hook: {handler.name} from {hook_file}")

        except Exception as e:
            logger.warning(f"Failed to load markdown hook from {hook_file}: {e}")

    def _load_plugin_hooks(self) -> None:
        """Load hooks from plugin directories."""
        plugin_dirs = [
            self.project_dir / PROJECT_DIR_NAME / "plugins",
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / CONFIG_DIR_NAME / "plugins",
        ]

        for plugin_base in plugin_dirs:
            if not plugin_base.exists():
                continue

            for plugin_dir in plugin_base.iterdir():
                if not plugin_dir.is_dir():
                    continue

                # Check for hooks directory in plugin
                hooks_dir = plugin_dir / "hooks"
                if hooks_dir.exists():
                    self._load_markdown_hooks_dir(hooks_dir)

                # Also check for hooks.toml in plugin
                hooks_toml = plugin_dir / "hooks.toml"
                if hooks_toml.exists():
                    self._load_config_file(hooks_toml)

    def get_handlers(self, event: HookEvent, tool_name: str | None = None) -> list[HookHandler]:
        """Get matching handlers for an event and tool.

        Args:
            event: The hook event type
            tool_name: Tool name for matching (optional)

        Returns:
            List of matching handlers
        """
        self.load_config()

        if event not in self.hooks:
            return []

        handlers = []
        for handler in self.hooks[event].handlers:
            if handler.enabled and handler.matches(tool_name):
                handlers.append(handler)

        return handlers

    async def execute_hooks(
        self,
        event: HookEvent,
        hook_input: HookInput,
        tool_name: str | None = None,
    ) -> HookOutput:
        """Execute all matching hooks for an event.

        Args:
            event: The hook event type
            hook_input: Input data for the hooks
            tool_name: Tool name for matching (for tool-related events)

        Returns:
            Combined output from all hooks
        """
        handlers = self.get_handlers(event, tool_name)
        if not handlers:
            return HookOutput()

        with hook_span(event.value, handler_count=len(handlers)) as span:
            combined_output = await self._run_handlers(handlers, hook_input)
            set_attributes(
                span,
                {
                    sc.ANKALOOP_HOOK_DECISION: combined_output.decision.value,
                    sc.ANKALOOP_HOOK_SUCCESS: combined_output.success,
                },
            )
            return combined_output

    async def _run_handlers(self, handlers: list[HookHandler], hook_input: HookInput) -> HookOutput:
        combined_output = HookOutput()

        for handler in handlers:
            try:
                if handler.type == "command":
                    if handler.command:
                        output = await self._execute_command_hook(handler, hook_input)
                    else:
                        output = HookOutput(
                            success=False,
                            feedback="Command hook is missing its command",
                        )
                elif handler.type == "python":
                    if handler.script or handler.function:
                        output = await self._execute_python_hook(handler, hook_input)
                    else:
                        output = HookOutput(
                            success=False,
                            feedback="Python hook requires a script or function",
                        )
                elif handler.type == "markdown":
                    output = self._execute_markdown_hook(handler, hook_input)
                else:
                    output = HookOutput(
                        success=False,
                        feedback=f"Unknown hook type: {handler.type}",
                    )

                if not output.success and handler.failure_mode == "closed":
                    reason = output.feedback or "Hook execution failed"
                    output.continue_execution = False
                    output.stop_reason = reason
                    output.decision = HookDecision.DENY
                    output.decision_reason = reason

                # Merge outputs (later hooks can override earlier ones)
                self._merge_outputs(combined_output, output)

                # Stop processing if continue_execution is False
                if not output.continue_execution:
                    combined_output.continue_execution = False
                    combined_output.stop_reason = output.stop_reason
                    break

                # Stop processing if tool is denied
                if output.decision == HookDecision.DENY:
                    combined_output.decision = HookDecision.DENY
                    combined_output.decision_reason = output.decision_reason
                    break

            except TimeoutError:
                logger.warning(f"Hook timed out after {handler.timeout}s")
                reason = f"Hook timed out after {handler.timeout}s"
                combined_output.success = False
                combined_output.feedback = reason
                if handler.failure_mode == "closed":
                    combined_output.continue_execution = False
                    combined_output.stop_reason = reason
                    combined_output.decision = HookDecision.DENY
                    combined_output.decision_reason = reason
                    break
            except Exception as e:
                logger.warning(f"Hook execution failed: {e}")
                reason = f"Hook execution failed: {e}"
                combined_output.success = False
                combined_output.feedback = reason
                if handler.failure_mode == "closed":
                    combined_output.continue_execution = False
                    combined_output.stop_reason = reason
                    combined_output.decision = HookDecision.DENY
                    combined_output.decision_reason = reason
                    break

        return combined_output

    async def _execute_command_hook(self, handler: HookHandler, hook_input: HookInput) -> HookOutput:
        """Execute a command-type hook."""
        if not handler.command:
            return HookOutput()

        env = self._build_hook_env(hook_input)

        # Pass input via stdin
        input_json = hook_input.to_json()

        try:
            # Run the command with timeout
            process = await asyncio.create_subprocess_shell(
                handler.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.project_dir),
                env=env,
            )
            return await self._run_hook_process(process, input_json=input_json, timeout=handler.timeout)

        except Exception as e:
            logger.error(f"Failed to execute hook command: {e}")
            return HookOutput(success=False, feedback=f"Hook command failed: {e}")

    async def _execute_python_hook(self, handler: HookHandler, hook_input: HookInput) -> HookOutput:
        """Execute a Python-type hook."""
        if handler.script:
            return await self._execute_python_script(handler, hook_input)
        elif handler.function:
            return await self._execute_python_function(handler, hook_input)
        else:
            return HookOutput()

    async def _execute_python_script(self, handler: HookHandler, hook_input: HookInput) -> HookOutput:
        """Execute a Python script hook."""
        if not handler.script:
            return HookOutput()

        script_path = Path(handler.script)
        if not script_path.exists():
            logger.warning(f"Hook script not found: {script_path}")
            return HookOutput(success=False, feedback=f"Hook script not found: {script_path}")

        # Run the script with the input as an argument
        env = self._build_hook_env(hook_input)

        input_json = hook_input.to_json()

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.project_dir),
                env=env,
            )
            return await self._run_hook_process(process, input_json=input_json, timeout=handler.timeout)

        except Exception as e:
            logger.error(f"Failed to execute hook script: {e}")
            return HookOutput(success=False, feedback=f"Hook script failed: {e}")

    def _build_hook_env(self, hook_input: HookInput) -> dict[str, str]:
        """Build environment variables for hook subprocesses."""
        env = os.environ.copy()
        env["ANKA_PROJECT_DIR"] = str(self.project_dir)
        env["ANKA_SESSION_ID"] = hook_input.session_id
        env["ANKA_HOOK_EVENT"] = hook_input.hook_event_name
        if hook_input.tool_name:
            env["ANKA_TOOL_NAME"] = hook_input.tool_name
        env.update(hook_env_vars())
        return env

    async def _run_hook_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        input_json: str,
        timeout: int,
    ) -> HookOutput:
        """Run a hook subprocess and normalize output handling."""
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_json.encode()),
                timeout=timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")
        exit_code = process.returncode or 0
        return HookOutput.from_exit_code(exit_code, stdout_str, stderr_str)

    async def _execute_python_function(self, handler: HookHandler, hook_input: HookInput) -> HookOutput:
        """Execute a Python function hook."""
        if not handler.function:
            return HookOutput()

        try:
            # Parse module and function
            parts = handler.function.rsplit(".", 1)
            if len(parts) != 2:
                logger.warning(f"Invalid function path: {handler.function}")
                return HookOutput(success=False, feedback=f"Invalid function path: {handler.function}")

            module_name, func_name = parts

            # Import the module
            import importlib

            module = importlib.import_module(module_name)
            func = getattr(module, func_name)

            # Call the function
            input_data = json.loads(hook_input.to_json())

            if asyncio.iscoroutinefunction(func):
                result = await asyncio.wait_for(
                    func(input_data),
                    timeout=handler.timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, func, input_data),
                    timeout=handler.timeout,
                )

            # Process result
            if isinstance(result, dict):
                output = HookOutput()
                output._apply_json_output(result)
                return output
            elif isinstance(result, HookOutput):
                return result
            else:
                return HookOutput(feedback=str(result) if result else None)

        except Exception as e:
            logger.error(f"Failed to execute hook function: {e}")
            return HookOutput(success=False, feedback=f"Hook function failed: {e}")

    def _execute_markdown_hook(self, handler: HookHandler, hook_input: HookInput) -> HookOutput:
        """Execute a markdown-type hook.

        Markdown hooks match patterns against tool input and return decisions
        based on the configured action (warn/block).
        """
        # Determine what content to match against
        content_to_match = ""

        if hook_input.tool_name == "bash":
            # For bash, match against the command
            content_to_match = hook_input.tool_input.get("command", "") if hook_input.tool_input else ""
        elif hook_input.tool_name in ("write_file", "apply_patch"):
            # For file operations, match against content and path
            if hook_input.tool_input:
                file_path = hook_input.tool_input.get("path", hook_input.tool_input.get("file_path", ""))
                content = hook_input.tool_input.get("content", hook_input.tool_input.get("patch", ""))
                content_to_match = f"{file_path}\n{content}"
        elif hook_input.prompt:
            # For prompt hooks
            content_to_match = hook_input.prompt

        # Check if pattern matches
        if handler.pattern:
            try:
                if not re.search(handler.pattern, content_to_match, re.IGNORECASE | re.MULTILINE):
                    # Pattern doesn't match, skip this hook
                    return HookOutput()
            except re.error as e:
                logger.warning(f"Invalid regex pattern in hook {handler.name}: {e}")
                return HookOutput()

        if handler.conditions and not self._check_conditions(handler.conditions, hook_input):
            return HookOutput()

        # Pattern matched or no pattern - execute the hook action
        output = HookOutput(success=True)
        output.feedback = handler.message

        if handler.action == "block":
            output.decision = HookDecision.DENY
            output.decision_reason = f"Blocked by hook: {handler.name}"
        else:  # warn
            output.decision = HookDecision.CONTINUE
            # Feedback is already set

        return output

    def _check_conditions(self, conditions: list[dict[str, str]], hook_input: HookInput) -> bool:
        """Check if all conditions match.

        Args:
            conditions: List of condition dicts with field, operator, pattern
            hook_input: The hook input to check against

        Returns:
            True if all conditions match
        """
        for condition in conditions:
            field = condition.get("field", "")
            operator = condition.get("operator", "contains")
            pattern = condition.get("pattern", "")

            # Get the field value
            value = ""
            if field == "file_path":
                value = hook_input.tool_input.get("path", "") if hook_input.tool_input else ""
            elif field == "content" or field == "new_text":
                value = hook_input.tool_input.get("content", "") if hook_input.tool_input else ""
            elif field == "command":
                value = hook_input.tool_input.get("command", "") if hook_input.tool_input else ""
            elif field == "prompt":
                value = hook_input.prompt or ""
            elif field == "tool_name":
                value = hook_input.tool_name or ""

            # Apply operator
            try:
                if operator == "contains":
                    if pattern.lower() not in value.lower():
                        return False
                elif operator == "not_contains":
                    if pattern.lower() in value.lower():
                        return False
                elif operator == "regex_match":
                    if not re.search(pattern, value, re.IGNORECASE):
                        return False
                elif operator == "regex_not_match":
                    if re.search(pattern, value, re.IGNORECASE):
                        return False
                elif operator == "equals":
                    if value != pattern:
                        return False
                elif operator == "not_equals" and value == pattern:
                    return False
            except re.error:
                logger.warning(f"Invalid regex pattern in condition: {pattern}")
                return False

        return True

    def _merge_outputs(self, target: HookOutput, source: HookOutput) -> None:
        """Merge source output into target output."""
        if not source.success:
            target.success = False
        if source.feedback:
            target.feedback = (target.feedback or "") + "\n" + source.feedback
        if not source.continue_execution:
            target.continue_execution = False
            target.stop_reason = source.stop_reason
        if source.system_message:
            target.system_message = source.system_message
        if source.updated_input:
            target.updated_input = source.updated_input
        if source.updated_response:
            target.updated_response = source.updated_response
        if source.decision != HookDecision.CONTINUE:
            target.decision = source.decision
            target.decision_reason = source.decision_reason
        if source.suppress_output:
            target.suppress_output = True


# Global hooks manager singleton
_hooks_manager: HooksManager | None = None


def get_hooks_manager(project_dir: Path | None = None) -> HooksManager:
    """Get the global hooks manager.

    Args:
        project_dir: Project directory (uses cwd if not specified)

    Returns:
        Global HooksManager instance
    """
    global _hooks_manager
    if _hooks_manager is None or (project_dir and _hooks_manager.project_dir != project_dir):
        _hooks_manager = HooksManager(project_dir)
    return _hooks_manager


def reset_hooks_manager() -> None:
    """Reset the global hooks manager (for testing)."""
    global _hooks_manager
    _hooks_manager = None


# Convenience functions for hook execution


async def run_pre_tool_use_hooks(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str | None = None,
    project_dir: Path | None = None,
) -> HookOutput:
    """Run PreToolUse hooks.

    Args:
        session_id: Current session ID
        tool_name: Name of the tool being executed
        tool_input: Tool input parameters
        tool_use_id: Tool use ID
        project_dir: Project directory

    Returns:
        HookOutput with decision and possibly modified input
    """
    manager = get_hooks_manager(project_dir)
    hook_input = HookInput(
        session_id=session_id,
        hook_event_name=HookEvent.PRE_TOOL_USE.value,
        cwd=str(project_dir or Path.cwd()),
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
    )
    return await manager.execute_hooks(HookEvent.PRE_TOOL_USE, hook_input, tool_name)


async def run_post_tool_use_hooks(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_response: dict[str, Any],
    tool_use_id: str | None = None,
    project_dir: Path | None = None,
) -> HookOutput:
    """Run PostToolUse hooks.

    Args:
        session_id: Current session ID
        tool_name: Name of the tool that was executed
        tool_input: Tool input parameters
        tool_response: Tool response
        tool_use_id: Tool use ID
        project_dir: Project directory

    Returns:
        HookOutput with possibly modified response
    """
    manager = get_hooks_manager(project_dir)
    hook_input = HookInput(
        session_id=session_id,
        hook_event_name=HookEvent.POST_TOOL_USE.value,
        cwd=str(project_dir or Path.cwd()),
        tool_name=tool_name,
        tool_input=tool_input,
        tool_response=tool_response,
        tool_use_id=tool_use_id,
    )
    return await manager.execute_hooks(HookEvent.POST_TOOL_USE, hook_input, tool_name)


async def run_user_prompt_hooks(
    session_id: str,
    prompt: str,
    project_dir: Path | None = None,
) -> HookOutput:
    """Run UserPromptSubmit hooks.

    Args:
        session_id: Current session ID
        prompt: User's prompt
        project_dir: Project directory

    Returns:
        HookOutput with possible modifications or denial
    """
    manager = get_hooks_manager(project_dir)
    hook_input = HookInput(
        session_id=session_id,
        hook_event_name=HookEvent.USER_PROMPT_SUBMIT.value,
        cwd=str(project_dir or Path.cwd()),
        prompt=prompt,
    )
    return await manager.execute_hooks(HookEvent.USER_PROMPT_SUBMIT, hook_input)


async def run_session_start_hooks(
    session_id: str,
    project_dir: Path | None = None,
) -> HookOutput:
    """Run SessionStart hooks.

    Args:
        session_id: New session ID
        project_dir: Project directory

    Returns:
        HookOutput
    """
    manager = get_hooks_manager(project_dir)
    hook_input = HookInput(
        session_id=session_id,
        hook_event_name=HookEvent.SESSION_START.value,
        cwd=str(project_dir or Path.cwd()),
    )
    return await manager.execute_hooks(HookEvent.SESSION_START, hook_input)


async def run_session_end_hooks(
    session_id: str,
    project_dir: Path | None = None,
) -> HookOutput:
    """Run SessionEnd hooks.

    Args:
        session_id: Session ID that is ending
        project_dir: Project directory

    Returns:
        HookOutput
    """
    manager = get_hooks_manager(project_dir)
    hook_input = HookInput(
        session_id=session_id,
        hook_event_name=HookEvent.SESSION_END.value,
        cwd=str(project_dir or Path.cwd()),
    )
    return await manager.execute_hooks(HookEvent.SESSION_END, hook_input)


async def run_stop_hooks(
    session_id: str,
    project_dir: Path | None = None,
) -> HookOutput:
    """Run Stop hooks.

    Args:
        session_id: Current session ID
        project_dir: Project directory

    Returns:
        HookOutput with possible override to continue
    """
    manager = get_hooks_manager(project_dir)
    hook_input = HookInput(
        session_id=session_id,
        hook_event_name=HookEvent.STOP.value,
        cwd=str(project_dir or Path.cwd()),
    )
    return await manager.execute_hooks(HookEvent.STOP, hook_input)


async def run_pre_compact_hooks(
    session_id: str,
    compact_type: str = "auto",
    project_dir: Path | None = None,
) -> HookOutput:
    """Run PreCompact hooks.

    Args:
        session_id: Current session ID
        compact_type: Type of compaction ("auto" or "manual")
        project_dir: Project directory

    Returns:
        HookOutput
    """
    manager = get_hooks_manager(project_dir)
    hook_input = HookInput(
        session_id=session_id,
        hook_event_name=HookEvent.PRE_COMPACT.value,
        cwd=str(project_dir or Path.cwd()),
        metadata={"compact_type": compact_type},
    )
    return await manager.execute_hooks(HookEvent.PRE_COMPACT, hook_input)
