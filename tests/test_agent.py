"""Tests for agent module."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.agent import Agent, AgentExecutionError, BusyError, MaxStepsReached, _is_tool_call_pairing_error
from ankaloop.agent_spec import ResolvedAgentSpec
from ankaloop.compaction import estimate_tokens
from ankaloop.config import AnkaloopConfig, ChatConfig, ContextConfig, ModelConfig
from ankaloop.hooks import HookDecision, HookOutput
from ankaloop.llm import ContextOverflowError, LLMResponse, ProviderError, ProviderErrorKind, TokenUsage
from ankaloop.memory import MemoryManager, MemoryStore
from ankaloop.multi_agent import AgentMode
from ankaloop.progressive.context_budget import ContextBudget, estimate_text_tokens
from ankaloop.runtime import RuntimeClosedError, TurnStatus
from ankaloop.session_state import CompactionCheckpoint, SessionState
from ankaloop.session_store import SessionConflictError, SessionLoadError, SessionSaveError
from ankaloop.tool_execution import ToolCapability, ToolExecutionContext, ToolExecutor
from ankaloop.tools import create_default_tool_registry


class TestAgentInit:
    def test_default_init(self):
        with patch("ankaloop.agent.load_config") as mock_load:
            mock_load.return_value = MagicMock()
            agent = Agent()
            assert agent.name == "default"
            assert agent.session_id is not None
            assert agent.conversation_history == []

    def test_custom_session_id(self):
        with patch("ankaloop.agent.load_config") as mock_load:
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")
            assert agent.session_id == "test-session"

    def test_custom_agent_spec(self):
        with patch("ankaloop.agent.load_config") as mock_load:
            mock_load.return_value = MagicMock()
            spec = ResolvedAgentSpec(
                name="custom",
                description="",
                mode=AgentMode.PRIMARY,
                system_prompt="",
                tools=[],
                exclude_tools=[],
                max_steps=10,
                model="",
                base_url="",
            )
            agent = Agent(agent_spec=spec)
            assert agent.name == "custom"
            assert agent.max_steps == 10

    def test_turn_config_follows_provider_switch_unless_agent_is_pinned(self, tmp_path):
        first = AnkaloopConfig(
            servers={},
            chat=ChatConfig(model="model-a", base_url="https://a.example/v1"),
        )
        second = AnkaloopConfig(
            servers={},
            chat=ChatConfig(model="model-b", base_url="https://b.example/v1"),
        )
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", side_effect=[first, second]),
        ):
            agent = Agent(session_id="provider-switch")
            assert agent._resolve_turn_config().chat.base_url == "https://a.example/v1"
            switched = agent._resolve_turn_config()

        assert switched.chat.model == "model-b"
        assert switched.chat.base_url == "https://b.example/v1"

        pinned_spec = ResolvedAgentSpec(
            name="pinned",
            description="",
            mode=AgentMode.PRIMARY,
            system_prompt="",
            tools=[],
            exclude_tools=[],
            max_steps=10,
            model="pinned-model",
            base_url="https://pinned.example/v1",
        )
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=second),
        ):
            pinned = Agent(pinned_spec, session_id="provider-pinned")._resolve_turn_config()

        assert pinned.chat.model == "pinned-model"
        assert pinned.chat.base_url == "https://pinned.example/v1"

    def test_loads_existing_history(self, tmp_path):
        sessions_dir = tmp_path / ".config" / "ankaloop" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_file = sessions_dir / "test-session.json"
        state = SessionState(session_id="test-session", agent_name="default")
        state.commit_turn(
            "turn-1",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        state.usage.total_llm_calls = 1
        data = {**state.to_snapshot(), "schema_version": 2}
        session_file.write_text(json.dumps(data))

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")
            assert len(agent.conversation_history) == 2
            assert agent.total_llm_calls == 1

    def test_load_history_handles_corrupted_file(self, tmp_path):
        session_file = tmp_path / ".config" / "ankaloop" / "sessions" / "test-session.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text("not json")

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            with pytest.raises(SessionLoadError, match="Could not load session"):
                Agent(session_id="test-session")


class TestAgentToolLimits:
    @pytest.mark.asyncio
    async def test_process_message_resets_per_request_tool_counts(self, tmp_path):
        """Per-request tool counts should not leak across Telegram messages."""

        async def deny_prompt(**_kwargs):
            return HookOutput(continue_execution=False, stop_reason="blocked")

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
            patch("ankaloop.agent.run_user_prompt_hooks", side_effect=deny_prompt),
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")
            agent.current_conversation_tool_calls = [{"tool": "read_file"} for _ in range(100)]

            result = await agent._process_message("hello", tmp_path, stream=False, show_progress=False)

        assert result == "blocked"
        assert agent.current_conversation_tool_calls == []

    def test_read_file_session_limit_still_applies(self, tmp_path):
        """Resetting per-request counts must not remove the session-level cap."""
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        agent.tool_calls_history = [{"tool": "read_file"} for _ in range(600)]
        agent.current_conversation_tool_calls = []

        assert agent._should_limit_tool_calls("read_file") is True

    def test_bash_per_request_limit_applies(self, tmp_path):
        """Bash calls are capped per request to avoid oversized tool context."""
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

            agent.current_conversation_tool_calls = [{"tool": "bash"} for _ in range(100)]

            assert agent._should_limit_tool_calls("bash") is True

    def test_bash_per_request_limit_is_configurable(self, tmp_path):
        """Config can tune the bash cap for long-running Telegram agents."""
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = AnkaloopConfig(servers={}, chat=ChatConfig(bash_tool_limit=20))
            agent = Agent(session_id="test-session")

            agent.current_conversation_tool_calls = [{"tool": "bash"} for _ in range(19)]
            assert agent._should_limit_tool_calls("bash") is False

            agent.current_conversation_tool_calls = [{"tool": "bash"} for _ in range(20)]
            assert agent._should_limit_tool_calls("bash") is True

    def test_bash_per_request_limit_can_be_disabled(self, tmp_path):
        """Non-positive bash_tool_limit disables only the bash-specific cap."""
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = AnkaloopConfig(servers={}, chat=ChatConfig(bash_tool_limit=0))
            agent = Agent(session_id="test-session")

            agent.current_conversation_tool_calls = [{"tool": "bash"} for _ in range(500)]

            assert agent._should_limit_tool_calls("bash") is False

    def test_bash_limit_resets_for_new_request(self, tmp_path):
        """A new user request can use bash again after per-request reset."""
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        agent.current_conversation_tool_calls = []

        assert agent._should_limit_tool_calls("bash") is False

    @pytest.mark.asyncio
    async def test_bash_tool_receives_work_dir(self, tmp_path):
        """Agent should run bash tool calls from the request work_dir."""

        class FakeLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        content="",
                        tool_calls=[
                            {
                                "id": "call_1",
                                "name": "bash",
                                "arguments": json.dumps({"command": "pwd"}),
                            }
                        ],
                    )

                tool_messages = [m for m in messages if m.get("role") == "tool"]
                assert tool_messages
                assert str(tmp_path.resolve()) in tool_messages[-1]["content"]
                return SimpleNamespace(content="done", tool_calls=None)

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
            agent = Agent(session_id="test-session")

        result = await agent._enhanced_chat_with_tools(
            llm_client=FakeLLM(),
            messages=[{"role": "user", "content": "pwd"}],
            tools=[create_default_tool_registry(enable_task=False).get_tool("bash").get_spec()],
            tool_registry={},
            stream=False,
            status=MagicMock(),
            work_dir=tmp_path,
        )

        assert result == "done"

    @pytest.mark.asyncio
    async def test_retries_with_repaired_pairing_after_provider_pairing_error(self, tmp_path):
        """A Gemini-style pairing 400 triggers one retry with synthesized tool results."""

        pairing_error = RuntimeError(
            "Error code: 400 - {'error': {'message': 'Please ensure that the number of "
            "function response parts is equal to the number of function call parts of the "
            "function call turn.', 'status': 'INVALID_ARGUMENT'}}"
        )

        class FakeLLM:
            def __init__(self):
                self.calls = 0
                self.seen_messages = None

            def chat(self, messages, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise pairing_error
                self.seen_messages = messages
                return SimpleNamespace(content="recovered", tool_calls=None)

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
            agent = Agent(session_id="test-session")

        llm = FakeLLM()
        result = await agent._enhanced_chat_with_tools(
            llm_client=llm,
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "missing",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "user", "content": "continue"},
            ],
            tools=[],
            tool_registry={},
            stream=False,
            status=MagicMock(),
            work_dir=tmp_path,
        )

        assert result == "recovered"
        assert llm.calls == 2
        retried_roles = [message["role"] for message in llm.seen_messages]
        assert retried_roles == ["assistant", "tool", "user"]
        synthesized = llm.seen_messages[1]
        assert synthesized["tool_call_id"] == "missing"
        assert synthesized["name"] == "read_file"

    @pytest.mark.asyncio
    async def test_retries_transient_provider_failure_and_records_timeline(self, tmp_path):
        class TransientError(RuntimeError):
            status_code = 503

        class FakeLLM:
            def __init__(self):
                self.calls = 0

            async def achat(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TransientError("upstream detail with secret")
                return SimpleNamespace(content="recovered", tool_calls=None, usage=None)

        with patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"):
            agent = Agent(session_id="provider-retry")

        llm = FakeLLM()
        response = await agent._call_llm(
            llm,
            messages=[{"role": "user", "content": "hello"}],
            cfg=ChatConfig(max_retries=1, retry_base_delay_seconds=0),
        )

        assert response.content == "recovered"
        assert llm.calls == 2
        events = agent.get_timeline(limit=10)
        assert events[-1]["type"] == "provider.retry"
        assert events[-1]["data"]["error_kind"] == "server"

    @pytest.mark.asyncio
    async def test_does_not_retry_after_streaming_output(self, tmp_path):
        class FakeLLM:
            def __init__(self):
                self.calls = 0

            async def achat(self, stream_callback=None, **_kwargs):
                self.calls += 1
                stream_callback("partial")
                raise TimeoutError("late timeout")

        with patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"):
            agent = Agent(session_id="provider-stream")

        llm = FakeLLM()
        with pytest.raises(ProviderError) as error:
            await agent._call_llm(
                llm,
                messages=[{"role": "user", "content": "hello"}],
                stream_callback=lambda _chunk: None,
                cfg=ChatConfig(max_retries=3, retry_base_delay_seconds=0),
            )

        assert error.value.kind == ProviderErrorKind.TIMEOUT
        assert error.value.partial_output is True
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_pairing_repair_does_not_retry_after_streaming_output(self, tmp_path):
        class FakeLLM:
            def __init__(self):
                self.calls = 0

            async def achat(self, stream_callback=None, **_kwargs):
                self.calls += 1
                stream_callback("partial")
                error = RuntimeError("INVALID_ARGUMENT: function response parts must equal function call parts")
                error.status_code = 400
                raise error

        with patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"):
            agent = Agent(session_id="pairing-stream")

        llm = FakeLLM()
        with pytest.raises(ProviderError) as error:
            await agent._enhanced_chat_with_tools(
                llm_client=llm,
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                tool_registry={},
                stream=True,
                status=MagicMock(),
                work_dir=tmp_path,
                cfg=AnkaloopConfig(
                    servers={},
                    chat=ChatConfig(max_retries=2, retry_base_delay_seconds=0),
                    context=ContextConfig(),
                ),
            )

        assert error.value.partial_output is True
        assert llm.calls == 1
        assert agent.current_request_llm_calls == 1

    @pytest.mark.asyncio
    async def test_grep_path_is_canonicalized_before_pre_tool_hooks(self, tmp_path):
        """Hooks inspect grep's canonical paths argument rather than its alias."""

        class FakeLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        content="",
                        tool_calls=[
                            {
                                "id": "call_1",
                                "name": "grep",
                                "arguments": json.dumps({"pattern": "needle", "path": "src"}),
                            }
                        ],
                    )
                assert any(message.get("role") == "tool" for message in messages)
                return SimpleNamespace(content="done", tool_calls=None)

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
            agent = Agent(session_id="test-session")

        denied = HookOutput(decision=HookDecision.DENY, decision_reason="test")
        with patch(
            "ankaloop.agent.run_pre_tool_use_hooks",
            new_callable=AsyncMock,
            return_value=denied,
        ) as pre_hook:
            result = await agent._enhanced_chat_with_tools(
                llm_client=FakeLLM(),
                messages=[{"role": "user", "content": "search"}],
                tools=[create_default_tool_registry(enable_task=False).get_tool("grep").get_spec()],
                tool_registry={},
                stream=False,
                status=MagicMock(),
                work_dir=tmp_path,
            )

        assert result == "done"
        assert pre_hook.await_args.kwargs["tool_input"] == {
            "pattern": "needle",
            "paths": ["src"],
        }


class TestAgentHistoryManagement:
    @pytest.mark.asyncio
    async def test_clear_conversation_history(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")
            agent.conversation_history = [{"role": "user", "content": "hi"}]
            agent.tool_calls_history = [{"tool": "test"}]
            agent.total_llm_calls = 5
            await agent.clear_conversation_history()
            assert agent.conversation_history == []
            assert agent.tool_calls_history == []
            assert agent.total_llm_calls == 0

    @pytest.mark.asyncio
    async def test_stale_owner_cannot_clear_newer_session(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=MagicMock()),
        ):
            first = Agent(session_id="shared-clear")
            stale = Agent(session_id="shared-clear")
            first._session_state.commit_turn(
                "saved",
                [
                    {"role": "user", "content": "keep"},
                    {"role": "assistant", "content": "kept"},
                ],
            )
            first._apply_session_state(first._session_state)
            first._save_conversation_history()

            with pytest.raises(SessionConflictError, match="changed from revision"):
                await stale.clear_conversation_history()

            restarted = Agent(session_id="shared-clear")

        assert [turn.turn_id for turn in restarted._session_state.turns] == ["saved"]

    @pytest.mark.asyncio
    async def test_run_cancellation_cancels_its_turn(self, tmp_path):
        started = asyncio.Event()

        async def slow_process(*_args):
            started.set()
            await asyncio.sleep(60)
            return "late"

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        with patch.object(agent, "_process_message", side_effect=slow_process):
            task = asyncio.create_task(agent.run("hello"))
            await started.wait()
            turn_id = str(agent.execution_context["turn_id"])
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        handle = agent.get_turn(turn_id)
        assert handle is not None
        assert handle.status == TurnStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_repeated_run_cancellation_waits_for_sync_tool_to_settle(self, tmp_path):
        started = threading.Event()
        release = threading.Event()
        registry = MagicMock()

        def execute_tool(_name, **_arguments):
            started.set()
            release.wait(timeout=5)
            return SimpleNamespace(success=True, content="settled")

        registry.execute_tool.side_effect = execute_tool
        executor = ToolExecutor(
            context=ToolExecutionContext("run-cancel", tmp_path, "turn"),
            capability=ToolCapability.from_spec(None, [], True),
            exposed_tools={"slow_write"},
            registry=registry,
            mcp_registry={},
            config=AnkaloopConfig(servers={}, chat=None),
        )

        async def process(*_args):
            await executor.execute("slow_write", {})
            return "late"

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=MagicMock()),
        ):
            agent = Agent(session_id="run-cancel")

        with patch.object(agent, "_process_message", side_effect=process):
            run_task = asyncio.create_task(agent.run("write"))
            assert await asyncio.to_thread(started.wait, 1)
            run_task.cancel()
            await asyncio.sleep(0.05)
            run_task.cancel()
            await asyncio.sleep(0.05)
            assert not run_task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await run_task

    @pytest.mark.asyncio
    async def test_cancelled_handle_waiter_does_not_cancel_detached_turn(self, tmp_path):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_process(*_args):
            started.set()
            await release.wait()
            return "done"

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        with patch.object(agent, "_process_message", side_effect=slow_process):
            handle = await agent.submit("hello")
            waiter = asyncio.create_task(handle.wait())
            await started.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            release.set()
            assert await handle.wait() == "done"

    @pytest.mark.asyncio
    async def test_reset_session_publishes_only_after_save(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        agent._session_state.commit_turn(
            "turn-1",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        agent._apply_session_state(agent._session_state)
        old_messages = list(agent.conversation_history)

        with (
            patch.object(
                agent._session_store,
                "save",
                side_effect=SessionSaveError("disk full"),
            ),
            pytest.raises(SessionSaveError, match="disk full"),
        ):
            await agent.reset_session()

        assert agent.conversation_history == old_messages

        await agent.reset_session()
        assert agent.conversation_history == []
        assert agent.session_file.exists()

    @pytest.mark.asyncio
    async def test_reset_session_cancels_active_turn_before_clearing(self, tmp_path):
        started = asyncio.Event()

        async def slow_process(*_args):
            started.set()
            await asyncio.sleep(60)
            return "late"

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        with patch.object(agent, "_process_message", side_effect=slow_process):
            handle = await agent.submit("hello")
            await started.wait()
            await agent.reset_session()

        assert handle.status == TurnStatus.CANCELLED
        assert agent.conversation_history == []

    @pytest.mark.asyncio
    async def test_close_waits_for_owned_tasks_and_rejects_submit(self, tmp_path):
        review_started = asyncio.Event()
        review_cancelled = asyncio.Event()

        async def review():
            review_started.set()
            try:
                await asyncio.sleep(60)
            finally:
                review_cancelled.set()

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        task = asyncio.create_task(review())
        agent._pending_memory_review_tasks.add(task)
        await review_started.wait()
        task_manager = SimpleNamespace(cancel_for_session=AsyncMock(return_value=0))
        agent._task_manager = task_manager
        await agent.close()
        await agent.close()

        assert review_cancelled.is_set()
        task_manager.cancel_for_session.assert_awaited_once_with(agent.session_id)
        with pytest.raises(RuntimeClosedError):
            await agent.submit("late")

    @pytest.mark.asyncio
    async def test_cancel_waits_for_pending_memory_review(self, tmp_path):
        review_started = asyncio.Event()
        review_cancelled = asyncio.Event()

        async def review():
            review_started.set()
            try:
                await asyncio.sleep(60)
            finally:
                review_cancelled.set()

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        task = asyncio.create_task(review())
        agent._pending_memory_review_tasks.add(task)
        await review_started.wait()
        task_manager = SimpleNamespace(cancel_for_session=AsyncMock(return_value=0))
        agent._task_manager = task_manager
        result = await agent.cancel(clear_queue=True)

        assert result.active_cancelled is False
        assert review_cancelled.is_set()

    def test_get_conversation_summary(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")
            agent.conversation_history = [{"role": "user", "content": "hi"}]
            agent.tool_calls_history = [{"tool": "test"}]
            agent.total_llm_calls = 3
            summary = agent.get_conversation_summary()
            assert summary["session_id"] == "test-session"
            assert summary["message_count"] == 1
            assert summary["total_tool_calls"] == 1
            assert summary["total_llm_calls"] == 3

    def test_memory_prompt_context_is_frozen_per_root(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        manager = MagicMock()
        manager.get_persona_context.return_value = "persona"
        manager.get_memory_context.return_value = "memory"

        with patch("ankaloop.agent.get_memory_manager", return_value=manager) as get_manager:
            assert agent._get_memory_prompt_context(tmp_path) == ("persona", "memory")
            manager.get_persona_context.return_value = "changed-persona"
            manager.get_memory_context.return_value = "changed-memory"
            assert agent._get_memory_prompt_context(tmp_path) == ("persona", "memory")

        get_manager.assert_called_once_with(tmp_path.resolve())

    def test_reset_memory_context_snapshot_refreshes_memory(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        first = MagicMock()
        first.get_persona_context.return_value = "first-persona"
        first.get_memory_context.return_value = "first-memory"
        second = MagicMock()
        second.get_persona_context.return_value = "second-persona"
        second.get_memory_context.return_value = "second-memory"

        with patch("ankaloop.agent.get_memory_manager", side_effect=[first, second]):
            assert agent._get_memory_prompt_context(tmp_path) == ("first-persona", "first-memory")
            agent.reset_memory_context_snapshot()
            assert agent._get_memory_prompt_context(tmp_path) == ("second-persona", "second-memory")

    @pytest.mark.asyncio
    async def test_periodic_memory_review_runs_every_ten_user_turns(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        conversation = []
        for idx in range(10):
            conversation.extend(
                [
                    {"role": "user", "content": f"u{idx}"},
                    {"role": "assistant", "content": f"a{idx}"},
                ]
            )

        with patch.object(agent, "_run_isolated_memory_review", new_callable=AsyncMock) as review:
            await agent._maybe_run_periodic_memory_review(
                conversation_snapshot=conversation,
                system_prompt="system",
                work_dir=tmp_path,
                status=MagicMock(),
            )

        tasks = list(agent._pending_memory_review_tasks)
        assert len(tasks) == 1
        await tasks[0]
        review.assert_awaited_once()
        assert agent._last_memory_review_turn_count == 10

    @pytest.mark.asyncio
    async def test_flush_memory_marks_reviewed_turn_count(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")
        agent.conversation_history = [
            {"role": "user", "content": "remember I prefer concise replies"},
            {"role": "assistant", "content": "ok"},
        ]

        with (
            patch.object(agent, "_get_system_prompt", return_value="system"),
            patch.object(agent, "_run_memory_review", new_callable=AsyncMock, return_value=True) as review,
        ):
            saved = await agent.flush_memory(work_dir=tmp_path)

        assert saved is True
        review.assert_awaited_once()
        assert agent._last_memory_review_turn_count == 1

    def test_save_conversation_history(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")
            agent._session_state.commit_turn(
                "turn-1",
                [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
            )
            agent._apply_session_state(agent._session_state)
            agent._save_conversation_history()
            assert agent.session_file.exists()
            data = json.loads(agent.session_file.read_text())
            assert data["schema_version"] == 2
            assert data["conversation"]["messages"] == [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]

    @pytest.mark.asyncio
    async def test_ephemeral_agent_keeps_turn_in_memory_without_durable_projections(self, tmp_path):
        """Ephemeral turns run normally without session, timeline, history, or transcript writes."""
        config = AnkaloopConfig(
            servers={},
            chat=ChatConfig(model="test-model"),
            context=ContextConfig(),
        )
        memory_manager = MagicMock()
        transcript_store = MagicMock()
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=config),
            patch(
                "ankaloop.agent.run_user_prompt_hooks",
                new=AsyncMock(return_value=HookOutput()),
            ),
            patch("ankaloop.llm.create_llm_client"),
            patch("ankaloop.agent.get_memory_manager", return_value=memory_manager),
            patch("ankaloop.agent.get_transcript_store", return_value=transcript_store) as transcript_getter,
        ):
            agent = Agent(session_id="task-child", ephemeral=True)
            with (
                patch.object(agent, "_get_system_prompt", return_value="system"),
                patch.object(
                    agent,
                    "_build_tools_and_registry",
                    new=AsyncMock(return_value=([], {})),
                ),
                patch.object(
                    agent,
                    "_run_with_tools",
                    new=AsyncMock(return_value=("done", [])),
                ),
                patch.object(agent._session_store, "save") as save,
                patch.object(agent._timeline_store, "append") as append_timeline,
                patch.object(agent, "_schedule_periodic_memory_review") as schedule_review,
            ):
                agent._emit_event("turn.started", {"turn_id": "turn-1"})
                result = await agent._process_message(
                    "delegated work",
                    tmp_path,
                    stream=False,
                    show_progress=False,
                )

        assert result == "done"
        assert [message["role"] for message in agent.conversation_history] == ["user", "assistant"]
        save.assert_not_called()
        append_timeline.assert_not_called()
        schedule_review.assert_not_called()
        memory_manager.append_history.assert_not_called()
        transcript_getter.assert_not_called()
        transcript_store.append_turn.assert_not_called()
        assert not agent.session_file.exists()
        assert not agent._timeline_store.path.exists()


class TestAgentContextFit:
    def test_fit_tool_context_synthesizes_missing_tool_results(self):
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "kept",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "lost",
                        "type": "function",
                        "function": {"name": "grep", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "kept", "content": "file contents"},
            {"role": "user", "content": "go on"},
        ]

        fitted = Agent._fit_tool_context(messages, [], 10**9)

        roles = [message["role"] for message in fitted]
        assert roles == ["system", "assistant", "tool", "tool", "user"]
        synthesized = fitted[3]
        assert synthesized["tool_call_id"] == "lost"
        assert synthesized["name"] == "grep"
        assert "synthesized" in synthesized["content"]
        assert messages[2]["role"] == "tool" and len(messages) == 4

    def test_fit_tool_context_drops_orphaned_tool_results(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "tool", "tool_call_id": "orphan", "content": "stale"},
            {"role": "user", "content": "hello"},
        ]

        fitted = Agent._fit_tool_context(messages, [], 10**9)

        assert [message["role"] for message in fitted] == ["system", "user"]

    def test_fit_tool_context_trims_old_result_without_mutating_input(self):
        old_content = "old result " * 3000
        latest_content = "latest result " * 1000
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "old",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "old", "content": old_content},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "latest",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "latest", "content": latest_content},
        ]
        budget = 5000

        fitted = Agent._fit_tool_context(messages, [], budget)

        assert "trimmed for context budget" in fitted[2]["content"]
        assert fitted[4]["content"] == latest_content
        assert messages[2]["content"] == old_content

    @pytest.mark.asyncio
    async def test_context_overflow_is_not_retried_and_records_timeline_event(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        class OversizedClient:
            async def achat(self, **_kwargs):
                raise ContextOverflowError(
                    input_tokens=120,
                    input_limit=80,
                    context_window=100,
                    output_reserve=20,
                )

        events = []
        agent.add_event_callback(lambda event_type, data: events.append((event_type, data)))

        with pytest.raises(ContextOverflowError):
            await agent._call_llm(
                OversizedClient(),
                messages=[{"role": "user", "content": "too large"}],
                cfg=ChatConfig(max_retries=3),
            )

        overflow_events = [data for event_type, data in events if event_type == "context.overflow"]
        assert len(overflow_events) == 1
        assert overflow_events[0]["input_tokens"] == 120
        assert overflow_events[0]["input_limit"] == 80
        assert agent.current_request_llm_calls == 1

    @pytest.mark.asyncio
    async def test_context_overflow_through_call_llm_has_no_self_referencing_cause(self, tmp_path):
        """Verify the real _call_llm path does not produce a self-referencing __cause__.

        classify_provider_error() returns the same ContextOverflowError object,
        so ``raise provider_error from exc`` would set ``__cause__ = self``.
        The bare ``raise`` guard must prevent this.
        """
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        original_overflow = ContextOverflowError(
            input_tokens=120,
            input_limit=80,
            context_window=100,
            output_reserve=20,
        )

        class OversizedClient:
            async def achat(self, **_kwargs):
                raise original_overflow

        with pytest.raises(ContextOverflowError) as caught:
            await agent._call_llm(
                OversizedClient(),
                messages=[{"role": "user", "content": "too large"}],
                cfg=ChatConfig(max_retries=3),
            )

        # The caught exception must be the same object (classify returns it unchanged)
        assert caught.value is original_overflow
        # Must NOT have a self-referencing __cause__
        assert caught.value.__cause__ is not caught.value
        # Must not be misidentified as a pairing error
        assert _is_tool_call_pairing_error(caught.value) is False

    @pytest.mark.asyncio
    async def test_call_llm_preserves_existing_cause_on_non_retryable_provider_error(self, tmp_path):
        """When a non-retryable ProviderError is freshly created by classify_provider_error,
        ``raise provider_error from exc`` must link the original exception as __cause__.
        """
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        class ProtocolErrorClient:
            async def achat(self, **_kwargs):
                raise ValueError("malformed response")

        with pytest.raises(ProviderError) as caught:
            await agent._call_llm(
                ProtocolErrorClient(),
                messages=[{"role": "user", "content": "hello"}],
                cfg=ChatConfig(max_retries=0),
            )

        # classify_provider_error wraps ValueError into a new non-retryable ProviderError
        assert caught.value.kind == ProviderErrorKind.PROTOCOL
        assert caught.value.retryable is False
        # The bare-raise guard must NOT apply here (provider_error is a fresh object),
        # so the original exception is linked as __cause__
        assert caught.value.__cause__ is not None
        assert isinstance(caught.value.__cause__, ValueError)

    def test_context_overflow_observer_records_auxiliary_client_failures(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        client = MagicMock()
        agent._attach_context_overflow_observer(client)
        callback = client.set_context_overflow_callback.call_args.args[0]
        error = ContextOverflowError(
            input_tokens=120,
            input_limit=80,
            context_window=100,
            output_reserve=20,
        )

        callback(error)
        callback(error)

        timeline = agent._timeline_store.read(limit=10)
        overflows = [event for event in timeline if event["type"] == "context.overflow"]
        assert len(overflows) == 1
        assert overflows[0]["data"]["input_limit"] == 80

    def test_agent_model_override_drops_mismatched_model_metadata(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = AnkaloopConfig(
                servers={},
                chat=ChatConfig(
                    model="large-model",
                    model_config=ModelConfig(
                        model_id="large-model",
                        context_window=1_000_000,
                        output_limit=10,
                    ),
                ),
            )
            spec = ResolvedAgentSpec(
                name="test",
                description="",
                mode=AgentMode.PRIMARY,
                system_prompt="",
                tools=[],
                exclude_tools=[],
                max_steps=20,
                model="small-model",
                base_url="",
            )
            agent = Agent(agent_spec=spec)

        resolved = agent._resolve_turn_config()

        assert resolved.chat is not None
        assert resolved.chat.model == "small-model"
        assert resolved.chat.model_config is None

    def test_records_provider_usage_for_status(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        response = LLMResponse(
            content="done",
            usage=TokenUsage(
                input_tokens=10_000,
                output_tokens=500,
                total_tokens=12_500,
                cached_input_tokens=2_000,
            ),
        )
        agent.total_llm_calls = 1

        agent._record_llm_usage(response, estimated_input_tokens=10_000, context_window=64_000)

        usage = agent.get_token_usage_summary()
        assert usage["context_tokens"] == 12_000
        assert usage["context_usage_ratio"] == 0.1875
        assert usage["total_tokens"] == 12_500
        assert usage["total_cached_input_tokens"] == 2_000
        assert usage["total_cache_write_input_tokens"] == 0
        assert usage["last_usage_from_api"] is True

    def test_estimates_input_when_provider_omits_usage(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="test-session")

        agent.total_llm_calls = 1
        agent._record_llm_usage(
            LLMResponse(content="done"),
            estimated_input_tokens=8_000,
            context_window=64_000,
        )

        usage = agent.get_token_usage_summary()
        assert usage["context_tokens"] == 8_000
        assert usage["total_input_tokens"] == 8_000
        assert usage["last_output_tokens"] is None
        assert usage["estimated_input_llm_calls"] == 1
        assert usage["last_usage_from_api"] is False


class TestAgentEventCallbacks:
    def test_add_and_emit_event(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            events = []

            def callback(event_type, data):
                events.append((event_type, data))

            agent.add_event_callback(callback)
            agent._emit_event("test.event", {"key": "value"})
            assert len(events) == 1
            assert events[0][0] == "test.event"
            assert events[0][1]["key"] == "value"

    def test_remove_event_callback(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            events = []

            def callback(event_type, data):
                events.append((event_type, data))

            agent.add_event_callback(callback)
            agent.remove_event_callback(callback)
            agent._emit_event("test.event", {})
            assert len(events) == 0

    def test_emit_event_suppresses_callback_errors(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()

            def bad_callback(event_type, data):
                raise RuntimeError("boom")

            agent.add_event_callback(bad_callback)
            # Should not raise
            agent._emit_event("test.event", {})


class TestAgentContextBudget:
    def test_resolve_model_name_from_spec(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            spec = ResolvedAgentSpec(
                name="test",
                description="",
                mode=AgentMode.PRIMARY,
                system_prompt="",
                tools=[],
                exclude_tools=[],
                max_steps=20,
                model="gpt-5.5",
                base_url="",
            )
            agent = Agent(agent_spec=spec)
            assert agent._resolve_model_name() == "gpt-5.5"

    def test_resolve_model_name_fallback(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_cfg = MagicMock()
            mock_cfg.chat = None
            mock_load.return_value = mock_cfg
            agent = Agent()
            assert agent._resolve_model_name() == "DeepSeek-V3.1-Terminus"

    def test_trim_to_token_budget_empty(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            assert agent._trim_to_token_budget("", 100) == ""

    def test_trim_to_token_budget_within_budget(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            text = "short text"
            assert agent._trim_to_token_budget(text, 1000) == text

    def test_trim_to_token_budget_honors_one_token_limit(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()

        trimmed = agent._trim_to_token_budget("abcdefgh", 1)

        assert estimate_text_tokens(trimmed) <= 1

    def test_is_tool_call_pairing_error_handles_self_referencing_cause(self):
        """Regression: _is_tool_call_pairing_error must not infinite-loop when
        __cause__ points to the same object (self-reference from raise ... from exc).
        """
        from ankaloop.llm import ContextOverflowError

        error = ContextOverflowError(
            input_tokens=100,
            input_limit=80,
            context_window=100,
            output_reserve=20,
        )
        error.__cause__ = error  # simulate the self-reference

        # Must return quickly (not hang)
        assert _is_tool_call_pairing_error(error) is False

    def test_non_progressive_skills_share_one_budget(self, tmp_path):
        cfg = AnkaloopConfig(
            servers={},
            chat=ChatConfig(model="test-model"),
            context=ContextConfig(progressive_skills=False),
        )
        skill_manager = MagicMock()
        skill_manager.get_all_skills.return_value = [object()]
        skill_manager.build_skills_summary.return_value = "S" * 200
        skill_manager.get_active_skills_content.return_value = "C" * 200
        budget = ContextBudget(
            total_available=100,
            prompt_budget=30,
            base_prompt=20,
            tools=0,
            skills=10,
            memory=0,
            rules=0,
            buffer=0,
        )
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=cfg),
            patch("ankaloop.agent.get_skill_manager", return_value=skill_manager),
            patch.object(Agent, "_calculate_context_budget", return_value=budget),
            patch.object(Agent, "_load_project_rules", return_value=""),
            patch.object(Agent, "_get_memory_prompt_context", return_value=("", "")),
        ):
            agent = Agent()
            prompt = agent._get_system_prompt(tmp_path, conversation_tokens=0)

        assert prompt.count("S") + prompt.count("C") <= budget.skills * 4 + 3

    def test_system_prompt_budget_uses_checkpoint_model_context(self, tmp_path):
        cfg = AnkaloopConfig(
            servers={},
            chat=ChatConfig(
                model="small-test-model",
                model_config=ModelConfig(
                    model_id="small-test-model",
                    context_window=1_000,
                    output_limit=100,
                ),
            ),
            context=ContextConfig(base_prompt_max_tokens=300),
        )
        skill_manager = MagicMock()
        skill_manager.get_all_skills.return_value = [object()]
        skill_manager.get_skills.return_value = []
        skill_manager.get_active_skills.return_value = []

        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=cfg),
            patch("ankaloop.agent.get_skill_manager", return_value=skill_manager),
            patch.object(Agent, "_load_project_rules", return_value="CHECKPOINT_RULE_MARKER"),
            patch.object(Agent, "_get_memory_prompt_context", return_value=("", "")),
        ):
            agent = Agent(session_id="checkpoint-prompt-budget")
            for index in range(2):
                agent._session_state.commit_turn(
                    f"large-{index}",
                    [
                        {"role": "user", "content": "u" * 4_000},
                        {"role": "assistant", "content": "a" * 4_000},
                    ],
                )
            agent._session_state.commit_turn(
                "small-suffix",
                [
                    {"role": "user", "content": "recent question"},
                    {"role": "assistant", "content": "recent answer"},
                ],
            )
            agent._session_state.checkpoint = CompactionCheckpoint(
                context=[{"role": "assistant", "content": "Compact summary"}],
                covered_message_count=4,
                covered_turn_count=2,
                generation=1,
                strategy="summary",
                strategy_version=1,
                original_tokens=4_000,
                compacted_tokens=4,
            )
            agent._apply_session_state(agent._session_state)

            model_context = agent._session_state.model_context([{"role": "user", "content": "current question"}])
            checkpoint_prompt = agent._get_system_prompt(
                tmp_path,
                conversation_tokens=estimate_tokens(model_context),
                cfg=cfg,
            )

            # Without a checkpoint, the actual model view is the large canonical
            # history and retains the existing zero-budget behavior.
            agent._session_state.checkpoint = None
            canonical_context = agent._session_state.model_context([{"role": "user", "content": "current question"}])
            canonical_prompt = agent._get_system_prompt(
                tmp_path,
                conversation_tokens=estimate_tokens(canonical_context),
                cfg=cfg,
            )

        assert checkpoint_prompt
        assert "memory_guidance" in checkpoint_prompt
        assert "CHECKPOINT_RULE_MARKER" in checkpoint_prompt
        assert canonical_prompt == ""

    def test_system_prompt_includes_persona_and_memory(self, tmp_path):
        """System prompt includes durable soul, identity, and memory."""
        manager = MemoryManager(project_root=tmp_path / "project")
        manager.user_store = MemoryStore(tmp_path / "user-memory")
        manager.write_soul("Soul marker: careful continuity", scope="user")
        manager.write_identity("Identity marker: AnkaLoop Atlas", scope="user")
        manager.write_long_term("Memory marker: user prefers concise replies", scope="user")

        cfg = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=cfg),
            patch("ankaloop.agent.get_memory_manager", return_value=manager),
        ):
            agent = Agent()
            prompt = agent._get_system_prompt(tmp_path / "project", conversation_tokens=0)

        assert "Soul marker: careful continuity" in prompt
        assert "Identity marker: AnkaLoop Atlas" in prompt
        assert "Memory marker: user prefers concise replies" in prompt


class TestAgentToolRegistry:
    def test_tool_registry_initialized(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            assert agent.tool_registry is not None


class TestAgentStepTracking:
    def test_step_count_starts_at_zero(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            assert agent.step_count == 0

    def test_request_counters_reset(self, tmp_path):
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            agent.current_request_tool_calls = 5
            agent.current_request_llm_calls = 3
            agent.current_request_start_time = 12345.0
            agent.current_request_tool_calls = 0
            agent.current_request_llm_calls = 0
            agent.current_request_start_time = None
            assert agent.current_request_tool_calls == 0
            assert agent.current_request_llm_calls == 0


class TestAgentMemoryReview:
    """Tests for pre-compaction memory flush."""

    def test_run_memory_review_exists(self, tmp_path):
        """Agent has _run_memory_review method."""
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            agent = Agent()
            assert hasattr(agent, "_run_memory_review")

    def test_system_prompt_includes_memory_guidance(self, tmp_path):
        """System prompt includes MEMORY_GUIDANCE text."""
        manager = MemoryManager(project_root=tmp_path / "project")
        manager.user_store = MemoryStore(tmp_path / "user-memory")

        cfg = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=cfg),
            patch("ankaloop.agent.get_memory_manager", return_value=manager),
        ):
            agent = Agent()
            prompt = agent._get_system_prompt(tmp_path / "project", conversation_tokens=0)

        assert "memory_guidance" in prompt
        assert "write_soul" in prompt
        assert "upsert_fact" in prompt
        assert "declarative facts" in prompt


def test_process_message_wraps_markup_like_exceptions(tmp_path):
    async def _run():
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock(chat=None)
            agent = Agent(session_id="test-session")

        prompt_hook_output = SimpleNamespace(
            continue_execution=True,
            feedback=None,
            stop_reason=None,
        )
        status = SimpleNamespace(update=lambda *args, **kwargs: None)
        markup_error = "closing tag '[/llms.txt]' at position 48 doesn't match any open tag"

        with (
            patch("ankaloop.agent.run_user_prompt_hooks", return_value=prompt_hook_output),
            patch.object(agent, "_create_progress_context") as mock_progress,
            patch.object(agent, "_get_system_prompt", return_value="system"),
            patch.object(agent, "_build_tools_and_registry") as mock_build_tools,
            patch.object(agent, "_run_with_tools", side_effect=ValueError(markup_error)),
        ):
            mock_progress.return_value.__enter__.return_value = status
            mock_progress.return_value.__exit__.return_value = False
            mock_build_tools.return_value = ([], {})

            with pytest.raises(AgentExecutionError, match="Agent execution failed"):
                await agent._process_message("search e2b persistence", tmp_path, stream=False, show_progress=False)

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_process_message_preserves_max_steps_exception(tmp_path):
    cfg = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=cfg),
    ):
        agent = Agent(session_id="max-steps")
    prompt_hook_output = SimpleNamespace(
        continue_execution=True,
        feedback=None,
        stop_reason=None,
    )
    status = SimpleNamespace(update=lambda *args, **kwargs: None)
    with (
        patch("ankaloop.agent.run_user_prompt_hooks", return_value=prompt_hook_output),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock()),
        patch.object(agent, "_create_progress_context") as progress,
        patch.object(agent, "_get_system_prompt", return_value="system"),
        patch.object(agent, "_build_tools_and_registry", return_value=([], {})),
        patch.object(agent, "_run_with_tools", side_effect=MaxStepsReached(1)),
    ):
        progress.return_value.__enter__.return_value = status
        progress.return_value.__exit__.return_value = False
        with pytest.raises(MaxStepsReached):
            await agent._process_message("test", tmp_path, stream=False, show_progress=False)
