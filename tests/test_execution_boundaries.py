"""Acceptance tests for execution boundaries."""

import asyncio
import json
import shutil
import threading
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop import tool_execution as tool_execution_module
from ankaloop.agent import Agent
from ankaloop.agent_spec import ResolvedAgentSpec
from ankaloop.config import AnkaloopConfig, ChatConfig, ContextConfig, Server
from ankaloop.multi_agent import AgentMode
from ankaloop.task import TaskManager
from ankaloop.tool_execution import (
    ToolCallProtocolError,
    ToolCapability,
    ToolExecutionContext,
    ToolExecutor,
    WorkspaceBoundaryError,
    _BoundedDaemonExecutor,
    normalize_tool_calls,
)
from ankaloop.tools import create_default_tool_registry


def _spec(*, tools=None, exclude_tools=None, can_delegate=True):
    return ResolvedAgentSpec(
        name="boundary",
        description="",
        mode=AgentMode.PRIMARY,
        system_prompt="",
        tools=tools,
        exclude_tools=exclude_tools or [],
        max_steps=5,
        model="",
        base_url="",
        can_delegate=can_delegate,
    )


def _executor(tmp_path, *, tools=None, exposed=None):
    return ToolExecutor(
        context=ToolExecutionContext("session", tmp_path, "turn"),
        capability=ToolCapability.from_spec(tools, [], True),
        exposed_tools=exposed or {"read_file", "write_file", "apply_patch", "bash"},
        registry=create_default_tool_registry(enable_task=False),
        mcp_registry={},
        config=AnkaloopConfig(servers={}, chat=None),
    )


def test_capability_distinguishes_inherit_deny_all_exclude_and_delegation():
    assert ToolCapability.from_spec(None, [], True).allows("bash")
    assert not ToolCapability.from_spec([], [], True).allows("bash")
    assert not ToolCapability.from_spec(None, ["bash"], True).allows("bash")
    assert not ToolCapability.from_spec(None, [], False).allows("task")


@pytest.mark.parametrize(
    "tool_calls, message",
    [
        ([{"name": "bash", "arguments": "{}"}], "missing a valid ID"),
        ([{"id": "x", "arguments": "{}"}], "missing a valid name"),
        (
            [
                {"id": "x", "name": "bash", "arguments": "{}"},
                {"id": "x", "name": "bash", "arguments": "{}"},
            ],
            "Duplicate",
        ),
    ],
)
def test_provider_protocol_errors_are_rejected(tool_calls, message):
    with pytest.raises(ToolCallProtocolError, match=message):
        normalize_tool_calls(tool_calls)


@pytest.mark.parametrize("arguments", ['{"x":', "[]", '"value"'])
def test_repairable_argument_errors_are_normalized(arguments):
    call = normalize_tool_calls([{"id": "x", "name": "bash", "arguments": arguments}])[0]
    assert call.arguments is None
    assert call.argument_error


def test_context_rejects_absolute_parent_and_symlink_escapes(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    context = ToolExecutionContext("session", workspace, "turn")

    with pytest.raises(WorkspaceBoundaryError):
        context.resolve_path("../outside/file.txt")
    with pytest.raises(WorkspaceBoundaryError):
        context.resolve_path(str(outside / "file.txt"))
    with pytest.raises(WorkspaceBoundaryError):
        context.resolve_path("link/file.txt")


@pytest.mark.asyncio
async def test_read_write_patch_and_bash_share_runtime_workspace(tmp_path):
    executor = _executor(tmp_path)
    write = await executor.execute("write_file", {"path": "file.txt", "content": "old"})
    assert write.success
    read = await executor.execute("read_file", {"path": "file.txt"})
    assert read.success and "old" in read.content

    marker = "***"
    patch_text = f"{marker} Begin Patch\n{marker} Update File: file.txt\n@@\n-old\n+new\n{marker} End Patch"
    patched = await executor.execute("apply_patch", {"patch": patch_text})
    assert patched.success
    bash = await executor.execute("bash", {"command": "pwd"})
    assert bash.success
    assert str(tmp_path.resolve()) in bash.content
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "new\n"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
@pytest.mark.asyncio
async def test_grep_singular_path_is_repaired_before_workspace_binding(tmp_path):
    (tmp_path / "sample.py").write_text("needle = True\n", encoding="utf-8")
    executor = _executor(tmp_path, exposed={"grep"})

    result = await executor.execute("grep", {"pattern": "needle", "path": "sample.py"})

    assert result.success
    assert "needle = True" in result.content
    assert result.metadata["paths"] == [str((tmp_path / "sample.py").resolve())]


@pytest.mark.asyncio
async def test_grep_does_not_repair_non_string_path(tmp_path):
    executor = _executor(tmp_path, exposed={"grep"})

    result = await executor.execute("grep", {"pattern": "needle", "path": None})

    assert not result.success
    assert result.error is not None
    assert result.error.startswith("Invalid arguments:")
    assert "does not support parameter 'path'" in result.error


@pytest.mark.asyncio
async def test_mcp_alias_dispatches_to_the_original_tool_name(tmp_path):
    """The sanitized alias exposed to the model maps back to the MCP server tool."""
    alias = "mcp__tavily__tavily_extract"
    executor = ToolExecutor(
        context=ToolExecutionContext("session", tmp_path, "turn"),
        capability=ToolCapability.from_spec(None, [], True),
        exposed_tools={alias},
        registry=create_default_tool_registry(enable_task=False),
        mcp_registry={alias: ("tavily", "tavily.extract")},
        config=AnkaloopConfig(servers={"tavily": Server(url="https://mcp.example.com/mcp")}, chat=None),
    )
    response = {
        "is_error": False,
        "content": [{"type": "text", "text": "extracted"}],
        "metadata": {"request_id": "success-1"},
    }
    call_mcp_tool = AsyncMock(return_value=response)

    with patch("ankaloop.tool_execution.call_mcp_tool", call_mcp_tool):
        result = await executor.execute(alias, {"url": "https://example.com"})

    assert result.success
    assert result.content == "extracted"
    assert result.metadata == {"response": response}
    server, tool_name, arguments = call_mcp_tool.await_args.args
    assert (server.url, tool_name, arguments) == (
        "https://mcp.example.com/mcp",
        "tavily.extract",
        {"url": "https://example.com"},
    )


@pytest.mark.asyncio
async def test_mcp_error_response_returns_failed_result_with_metadata(tmp_path):
    alias = "mcp__tavily__tavily_extract"
    executor = ToolExecutor(
        context=ToolExecutionContext("session", tmp_path, "turn"),
        capability=ToolCapability.from_spec(None, [], True),
        exposed_tools={alias},
        registry=create_default_tool_registry(enable_task=False),
        mcp_registry={alias: ("tavily", "tavily.extract")},
        config=AnkaloopConfig(servers={"tavily": Server(url="https://mcp.example.com/mcp")}, chat=None),
    )
    response = {
        "is_error": True,
        "content": [{"type": "text", "text": "upstream rejected the request"}],
        "metadata": {"request_id": "error-1", "status": 429},
    }

    with patch("ankaloop.tool_execution.call_mcp_tool", AsyncMock(return_value=response)):
        result = await executor.execute(alias, {"url": "https://example.com"})

    assert not result.success
    assert result.content == "upstream rejected the request"
    assert result.error == "upstream rejected the request"
    assert result.metadata == {"response": response}


@pytest.mark.asyncio
async def test_task_tool_inherits_trusted_runtime_workspace(tmp_path):
    executor = _executor(tmp_path, exposed={"task"})
    task_tool = MagicMock()
    task_tool.execute = AsyncMock(return_value="created")

    with patch("ankaloop.tool_execution.TaskTool", return_value=task_tool) as task_tool_class:
        executor.task_manager = TaskManager()
        result = await executor.execute("task", {"action": "create", "description": "inspect"})

    assert result.success
    task_tool_class.assert_called_once_with(
        manager=executor.task_manager,
        session_id="session",
        work_dir=tmp_path.resolve(),
    )


@pytest.mark.asyncio
async def test_two_workspace_executors_are_isolated(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_executor = _executor(first)
    second_executor = _executor(second)
    await asyncio.gather(
        first_executor.execute("write_file", {"path": "same.txt", "content": "first"}),
        second_executor.execute("write_file", {"path": "same.txt", "content": "second"}),
    )
    assert (first / "same.txt").read_text() == "first"
    assert (second / "same.txt").read_text() == "second"


@pytest.mark.asyncio
async def test_todos_are_isolated_by_trusted_runtime_session(tmp_path):
    registry = create_default_tool_registry(enable_task=False)

    def todo_executor(session_id):
        return ToolExecutor(
            context=ToolExecutionContext(session_id, tmp_path, "turn"),
            capability=ToolCapability.from_spec(["todo"], [], False),
            exposed_tools={"todo"},
            registry=registry,
            mcp_registry={},
            config=AnkaloopConfig(servers={}, chat=None),
        )

    first = todo_executor("first")
    second = todo_executor("second")
    written = await first.execute(
        "todo",
        {
            "action": "write",
            "todos": [{"id": "1", "content": "first session only"}],
            "_session_id": "second",
        },
    )

    assert written.success
    assert "first session only" in (await first.execute("todo", {"action": "read"})).content
    assert "No todos" in (await second.execute("todo", {"action": "read"})).content


@pytest.mark.asyncio
async def test_bash_process_is_terminated_on_cancellation(tmp_path):
    executor = _executor(tmp_path)
    task = asyncio.create_task(executor.execute("bash", {"command": "sleep 2; printf escaped > should-not-exist"}))
    await asyncio.sleep(0.1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_thread_backed_tool_settles_before_cancellation_returns(tmp_path):
    started = threading.Event()
    release = threading.Event()
    registry = MagicMock()

    def execute_tool(_name, **_arguments):
        started.set()
        release.wait(timeout=5)
        return SimpleNamespace(success=True, content="settled")

    registry.execute_tool.side_effect = execute_tool
    executor = ToolExecutor(
        context=ToolExecutionContext("session", tmp_path, "turn"),
        capability=ToolCapability.from_spec(None, [], True),
        exposed_tools={"slow_write"},
        registry=registry,
        mcp_registry={},
        config=AnkaloopConfig(servers={}, chat=None),
    )

    task = asyncio.create_task(executor.execute("slow_write", {}))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_thread_backed_tool_cancellation_has_settle_deadline(tmp_path):
    started = threading.Event()
    release = threading.Event()
    registry = MagicMock()

    def execute_tool(_name, **_arguments):
        started.set()
        release.wait(timeout=5)
        return SimpleNamespace(success=True, content="late")

    registry.execute_tool.side_effect = execute_tool
    executor = ToolExecutor(
        context=ToolExecutionContext("session", tmp_path, "deadline"),
        capability=ToolCapability.from_spec(None, [], True),
        exposed_tools={"stuck_tool"},
        registry=registry,
        mcp_registry={},
        config=AnkaloopConfig(
            servers={},
            chat=ChatConfig(sync_tool_settle_timeout_seconds=0.05),
        ),
    )

    task = asyncio.create_task(executor.execute("stuck_tool", {}))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert not release.is_set()
    release.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cancelled_queued_sync_tool_is_not_executed(tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()
    queued_executed = threading.Event()
    registry = MagicMock()

    def execute_tool(name, **_arguments):
        if name == "blocking_tool":
            first_started.set()
            release_first.wait(timeout=5)
        else:
            queued_executed.set()
        return SimpleNamespace(success=True, content=name)

    registry.execute_tool.side_effect = execute_tool
    executor = ToolExecutor(
        context=ToolExecutionContext("session", tmp_path, "queued-cancel"),
        capability=ToolCapability.from_spec(None, [], True),
        exposed_tools={"blocking_tool", "queued_tool"},
        registry=registry,
        mcp_registry={},
        config=AnkaloopConfig(
            servers={},
            chat=ChatConfig(sync_tool_settle_timeout_seconds=0.01),
        ),
    )

    with patch.object(
        tool_execution_module,
        "_sync_executor",
        _BoundedDaemonExecutor(max_workers=1, max_pending=2),
    ):
        blocking = asyncio.create_task(executor.execute("blocking_tool", {}))
        assert await asyncio.to_thread(first_started.wait, 1)
        queued = asyncio.create_task(executor.execute("queued_tool", {}))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued

        release_first.set()
        assert (await blocking).content == "blocking_tool"
        await asyncio.sleep(0.05)

    assert not queued_executed.is_set()


class _ToolCallingLLM:
    def __init__(self, call):
        self.call = call
        self.calls = 0
        self.tool_result = ""

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(content="", tool_calls=[self.call], usage=None)
        self.tool_result = [message for message in messages if message.get("role") == "tool"][-1]["content"]
        return SimpleNamespace(content="done", tool_calls=None, usage=None)


@pytest.mark.asyncio
async def test_hidden_tool_call_is_denied_without_execution(tmp_path):
    with patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"):
        agent = Agent(_spec(tools=None), session_id="hidden")
    llm = _ToolCallingLLM(
        {
            "id": "call-1",
            "name": "write_file",
            "arguments": json.dumps({"path": "forbidden.txt", "content": "no"}),
        }
    )
    think_spec = create_default_tool_registry(enable_task=False).get_tool("think").get_spec()
    result = await agent._enhanced_chat_with_tools(
        llm_client=llm,
        messages=[{"role": "user", "content": "test"}],
        tools=[think_spec],
        tool_registry={},
        stream=False,
        status=MagicMock(),
        work_dir=tmp_path,
    )
    assert result == "done"
    assert "permission denied" in llm.tool_result
    assert not (tmp_path / "forbidden.txt").exists()


@pytest.mark.asyncio
async def test_empty_allowlist_and_disabled_delegation_cannot_execute(tmp_path):
    registry = create_default_tool_registry(enable_task=True)
    cases = [
        (
            _spec(tools=[]),
            registry.get_tool("write_file").get_spec(),
            {
                "id": "write",
                "name": "write_file",
                "arguments": '{"path":"denied.txt","content":"no"}',
            },
        ),
        (
            _spec(tools=None, can_delegate=False),
            registry.get_tool_spec("task"),
            {
                "id": "task",
                "name": "task",
                "arguments": '{"action":"list"}',
            },
        ),
    ]
    for index, (spec, tool_spec, call) in enumerate(cases):
        with patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"):
            agent = Agent(spec, session_id=f"denied-{index}")
        llm = _ToolCallingLLM(call)
        assert (
            await agent._enhanced_chat_with_tools(
                llm_client=llm,
                messages=[{"role": "user", "content": "test"}],
                tools=[tool_spec],
                tool_registry={},
                stream=False,
                status=MagicMock(),
                work_dir=tmp_path,
            )
            == "done"
        )
        assert "permission denied" in llm.tool_result
    assert not (tmp_path / "denied.txt").exists()


@pytest.mark.asyncio
async def test_invalid_arguments_are_repairable_but_missing_id_is_protocol_error(tmp_path):
    cfg = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=cfg),
    ):
        agent = Agent(_spec(tools=None), session_id="malformed")
    bash_spec = create_default_tool_registry(enable_task=False).get_tool("bash").get_spec()
    llm = _ToolCallingLLM({"id": "call-1", "name": "bash", "arguments": "[]"})
    assert (
        await agent._enhanced_chat_with_tools(
            llm_client=llm,
            messages=[{"role": "user", "content": "test"}],
            tools=[bash_spec],
            tool_registry={},
            stream=False,
            status=MagicMock(),
            work_dir=tmp_path,
        )
        == "done"
    )
    assert "JSON object" in llm.tool_result

    bad_llm = _ToolCallingLLM({"name": "bash", "arguments": "{}"})
    with pytest.raises(ToolCallProtocolError):
        await agent._enhanced_chat_with_tools(
            llm_client=bad_llm,
            messages=[{"role": "user", "content": "test"}],
            tools=[bash_spec],
            tool_registry={},
            stream=False,
            status=MagicMock(),
            work_dir=tmp_path,
        )
