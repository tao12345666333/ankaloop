"""Capability-safe tool-call normalization and execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from .config import AnkaloopConfig
from .mcp_client import call_mcp_tool
from .mcp_naming import is_mcp_tool_name
from .task import TaskManager, TaskTool
from .tools import ToolRegistry, ToolResult
from .tracing import current_span, set_attributes
from .tracing import semconv as sc

logger = logging.getLogger(__name__)


class _BoundedDaemonExecutor:
    """Small daemon worker pool for synchronous tools that cannot be cancelled."""

    def __init__(self, max_workers: int = 8, max_pending: int = 64) -> None:
        self._jobs: queue.Queue[
            tuple[
                asyncio.AbstractEventLoop,
                asyncio.Future[ToolResult],
                Callable[[], ToolResult],
            ]
        ] = queue.Queue(maxsize=max_pending)
        for index in range(max_workers):
            threading.Thread(
                target=self._worker,
                name=f"ankaloop-sync-tool-{index}",
                daemon=True,
            ).start()

    def submit(self, operation: Callable[[], ToolResult]) -> asyncio.Future[ToolResult]:
        """Queue an operation without blocking the event loop."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolResult] = loop.create_future()
        try:
            self._jobs.put_nowait((loop, future, operation))
        except queue.Full:
            future.set_result(
                ToolResult(
                    success=False,
                    content="",
                    error="Synchronous tool executor is saturated",
                )
            )
        return future

    def _worker(self) -> None:
        while True:
            loop, future, operation = self._jobs.get()
            try:
                if future.done():
                    continue
                result = operation()
            except BaseException as exc:
                self._notify(loop, future, error=exc)
            else:
                self._notify(loop, future, result=result)
            finally:
                self._jobs.task_done()

    @staticmethod
    def _notify(
        loop: asyncio.AbstractEventLoop,
        future: asyncio.Future[ToolResult],
        *,
        result: ToolResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        def complete() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                assert result is not None
                future.set_result(result)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(complete)


_sync_executor: _BoundedDaemonExecutor | None = None
_sync_executor_lock = threading.Lock()


def _get_sync_executor() -> _BoundedDaemonExecutor:
    """Lazily create the bounded executor shared by synchronous tools."""
    global _sync_executor
    if _sync_executor is None:
        with _sync_executor_lock:
            if _sync_executor is None:
                _sync_executor = _BoundedDaemonExecutor()
    return _sync_executor


class ToolCallProtocolError(Exception):
    """Raised for provider tool-call structures that cannot be repaired."""

    def __init__(self, message: str, tool_calls: Any | None = None) -> None:
        super().__init__(message)
        self.tool_calls = tool_calls


class ToolPermissionError(Exception):
    """Raised when a tool call is outside the effective capability."""


class WorkspaceBoundaryError(Exception):
    """Raised when a tool attempts to escape its runtime workspace."""


@dataclass(frozen=True)
class ToolExecutionContext:
    """Trusted runtime context unavailable for model modification."""

    session_id: str
    workspace_root: Path
    turn_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", self.workspace_root.expanduser().resolve())

    def resolve_path(self, value: str) -> Path:
        """Resolve a model path and reject workspace escapes."""
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.workspace_root):
            raise WorkspaceBoundaryError(f"Path is outside workspace: {value}")
        return resolved


@dataclass(frozen=True)
class ToolCapability:
    """Effective allowlist shared by tool display and execution."""

    configured_tools: frozenset[str] | None
    excluded_tools: frozenset[str]
    can_delegate: bool

    @classmethod
    def from_spec(
        cls,
        tools: list[str] | None,
        exclude_tools: list[str] | None,
        can_delegate: bool,
    ) -> ToolCapability:
        """Build a capability from resolved agent configuration."""
        return cls(
            configured_tools=frozenset(tools) if tools is not None else None,
            excluded_tools=frozenset(exclude_tools or []),
            can_delegate=can_delegate,
        )

    def allows(self, tool_name: str) -> bool:
        """Return whether this capability allows a named tool."""
        if tool_name in self.excluded_tools:
            return False
        if tool_name == "task" and not self.can_delegate:
            return False
        return self.configured_tools is None or tool_name in self.configured_tools


@dataclass(frozen=True)
class NormalizedToolCall:
    """Provider tool call normalized for validation and dispatch."""

    id: str
    name: str
    raw_arguments: str
    arguments: dict[str, Any] | None
    argument_error: str | None = None
    extra_content: dict[str, Any] | None = None


def normalize_tool_calls(tool_calls: Any) -> list[NormalizedToolCall]:
    """Normalize provider tool calls and classify repairable argument errors."""
    if not isinstance(tool_calls, list):
        raise ToolCallProtocolError("Provider tool_calls must be a list", tool_calls=tool_calls)

    normalized: list[NormalizedToolCall] = []
    seen_ids: set[str] = set()
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            raise ToolCallProtocolError(f"Tool call {index} must be an object")
        call_id = call.get("id")
        name = call.get("name")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ToolCallProtocolError(f"Tool call {index} is missing a valid ID")
        if call_id in seen_ids:
            raise ToolCallProtocolError(f"Duplicate tool call ID: {call_id}", tool_calls=tool_calls)
        seen_ids.add(call_id)
        if not isinstance(name, str) or not name.strip():
            raise ToolCallProtocolError(f"Tool call {call_id} is missing a valid name")

        raw = call.get("arguments", "{}")
        if raw is None:
            raw = "{}"
        if isinstance(raw, dict):
            arguments = raw
            raw_arguments = json.dumps(raw, ensure_ascii=False)
            error = None
        elif isinstance(raw, str):
            raw_arguments = raw or "{}"
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                arguments = None
                error = f"Invalid JSON arguments: {exc.msg}"
            else:
                if not isinstance(parsed, dict):
                    arguments = None
                    error = "Tool arguments must be a JSON object"
                else:
                    arguments = parsed
                    error = None
        else:
            raw_arguments = json.dumps(raw, ensure_ascii=False)
            arguments = None
            error = "Tool arguments must be a JSON object"

        normalized.append(
            NormalizedToolCall(
                id=call_id,
                name=name,
                raw_arguments=raw_arguments,
                arguments=arguments,
                argument_error=error,
                extra_content=call.get("extra_content") if isinstance(call.get("extra_content"), dict) else None,
            )
        )
    return normalized


class ToolExecutor:
    """Execute allowed built-in and MCP tools within a trusted workspace."""

    def __init__(
        self,
        *,
        context: ToolExecutionContext,
        capability: ToolCapability,
        exposed_tools: set[str],
        registry: ToolRegistry,
        mcp_registry: dict[str, tuple[str, str]],
        config: AnkaloopConfig,
        task_manager: TaskManager | None = None,
    ):
        self.context = context
        self.capability = capability
        self.exposed_tools = exposed_tools
        self.registry = registry
        self.mcp_registry = mcp_registry
        self.config = config
        self.task_manager = task_manager

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Validate permission and execute one tool asynchronously."""
        if name not in self.exposed_tools or not self.capability.allows(name):
            raise ToolPermissionError(f"Tool '{name}' is not authorized for this turn")

        arguments = self.prepare_model_arguments(name, arguments)
        args = self._bind_arguments(name, arguments)
        if is_mcp_tool_name(name):
            return await self._execute_mcp(name, args)
        if name == "task":
            if self.task_manager is None:
                return ToolResult(success=False, content="", error="Task manager is not configured")
            content = await TaskTool(
                manager=self.task_manager,
                session_id=self.context.session_id,
                work_dir=self.context.workspace_root,
            ).execute(**args)
            return ToolResult(success=True, content=content)
        if name == "bash":
            return await self._execute_bash(args)
        return await self._execute_sync_tool(name, args)

    async def _execute_sync_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run a thread-backed tool and briefly settle it after cancellation.

        Python cannot stop a thread that has already entered synchronous tool
        code. The settle deadline gives a normal tool time to reach a terminal
        state without allowing one stuck worker to block session cancellation
        forever. A worker that exceeds the deadline is detached and logged.
        """
        task = _get_sync_executor().submit(
            partial(self.registry.execute_tool, name, **arguments),
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            timeout = self.config.chat.sync_tool_settle_timeout_seconds if self.config.chat is not None else 2.0
            deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
            while not task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except asyncio.CancelledError:
                    continue
                except TimeoutError:
                    break
                except Exception:
                    break
            if task.done() and not task.cancelled():
                with contextlib.suppress(Exception):
                    task.result()
            elif not task.done():
                logger.error(
                    "Synchronous tool %s exceeded the %.2fs cancellation settle deadline "
                    "for turn %s; cancelling it before execution when possible",
                    name,
                    max(timeout, 0.0),
                    self.context.turn_id,
                )
                task.cancel()
            raise cancelled

    def prepare_model_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Canonicalize model input before hooks or trusted runtime binding."""
        if is_mcp_tool_name(name):
            return dict(arguments)
        prepared = self.registry.prepare_model_arguments(name, arguments)
        if name == "todo":
            prepared.pop("_session_id", None)
        return prepared

    def _bind_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments)
        if name in {"read_file", "write_file"} and isinstance(args.get("path"), str):
            args["path"] = str(self.context.resolve_path(args["path"]))
        elif name == "grep":
            paths = args.get("paths")
            if paths is None:
                args["paths"] = [str(self.context.workspace_root)]
            elif isinstance(paths, list):
                args["paths"] = [str(self.context.resolve_path(path)) for path in paths]
        elif name == "apply_patch":
            args["_workspace_root"] = self.context.workspace_root
        elif name == "bash":
            args.pop("cwd", None)
        elif name == "memory":
            args["project_root"] = str(self.context.workspace_root)
        elif name == "todo":
            args["_session_id"] = self.context.session_id
        return args

    async def _execute_mcp(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        server_name, inner_name = self.mcp_registry.get(name, (None, None))
        if not server_name or not inner_name:
            return ToolResult(success=False, content="", error=f"Unknown MCP tool {name}")
        server = self.config.servers.get(server_name)
        if server is None:
            return ToolResult(success=False, content="", error=f"Unknown MCP server {server_name}")
        set_attributes(
            current_span(),
            {sc.GEN_AI_TOOL_TYPE: sc.TOOL_TYPE_EXTENSION, sc.ANKALOOP_MCP_SERVER: server_name},
        )
        response = await call_mcp_tool(server, inner_name, arguments)
        parts = [item.get("text", "") for item in response.get("content", []) or [] if item.get("type") == "text"]
        content = "\n\n".join(parts) or json.dumps(response, ensure_ascii=False)
        if response.get("is_error"):
            return ToolResult(
                success=False,
                content=content,
                error=content,
                metadata={"response": response},
            )
        return ToolResult(success=True, content=content, metadata={"response": response})

    async def _execute_bash(self, arguments: dict[str, Any]) -> ToolResult:
        command = arguments.get("command")
        timeout = arguments.get("timeout", 30)
        if not isinstance(command, str):
            return ToolResult(success=False, content="", error="Bash command must be a string")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1 or timeout > 600:
            return ToolResult(success=False, content="", error="Bash timeout must be between 1 and 600")

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.context.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            await self._terminate_process(process)
            return ToolResult(success=False, content="", error=f"Command timed out after {timeout} seconds")
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise

        output = stdout.decode(errors="replace")
        if stderr:
            output += f"\n[stderr]\n{stderr.decode(errors='replace')}"
        output = output or "(no output)"
        max_chars = 6000
        truncated = len(output) > max_chars
        if truncated:
            half = (max_chars - 40) // 2
            output = f"{output[:half]}\n... [output truncated] ...\n{output[-half:]}"
        return ToolResult(
            success=process.returncode == 0,
            content=output,
            metadata={
                "command": command,
                "exit_code": process.returncode,
                "cwd": str(self.context.workspace_root),
                "truncated": truncated,
            },
            error=None if process.returncode == 0 else f"Command exited with code {process.returncode}",
        )

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
