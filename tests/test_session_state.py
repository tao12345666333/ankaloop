"""Tests for canonical session turns, commits, and restart behavior."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.agent import Agent, AgentExecutionError
from ankaloop.compaction import CompactionResult, CompactionStrategy
from ankaloop.config import AnkaloopConfig, ChatConfig, ContextConfig
from ankaloop.hooks import HookOutput
from ankaloop.runtime import TurnStatus
from ankaloop.session_state import SessionState
from ankaloop.session_store import SessionSaveError
from ankaloop.tools import create_default_tool_registry


@pytest.fixture(autouse=True)
def _disable_best_effort_projections():
    with (
        patch("ankaloop.agent.get_memory_manager"),
        patch("ankaloop.agent.get_transcript_store"),
    ):
        yield


def _config() -> AnkaloopConfig:
    return AnkaloopConfig(
        servers={},
        chat=ChatConfig(model="glm-4.7"),
        context=ContextConfig(),
    )


def _tool_delta() -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "think", "arguments": '{"thought":"one"}'},
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "think", "arguments": '{"thought":"two"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "think",
            "content": "one",
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "name": "think",
            "content": "two",
        },
    ]


@pytest.mark.asyncio
async def test_tool_loop_returns_ordered_canonical_message_delta(tmp_path):
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
                            "id": "call-1",
                            "name": "think",
                            "arguments": json.dumps({"thought": "one"}),
                        },
                        {
                            "id": "call-2",
                            "name": "think",
                            "arguments": json.dumps({"thought": "two"}),
                        },
                    ],
                    usage=None,
                )
            return SimpleNamespace(content="done", tool_calls=None, usage=None)

    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
    ):
        agent = Agent(session_id="tool-delta")
        think = create_default_tool_registry(enable_task=False).get_tool("think")
        result = await agent._enhanced_chat_with_tools(
            llm_client=FakeLLM(),
            messages=[{"role": "user", "content": "think twice"}],
            tools=[think.get_spec()],
            tool_registry={},
            stream=False,
            status=MagicMock(),
            work_dir=tmp_path,
            return_message_delta=True,
        )

    assert isinstance(result, tuple)
    final, delta = result
    assert final == "done"
    assert [message["role"] for message in delta] == ["assistant", "tool", "tool", "assistant"]
    assert [message["tool_call_id"] for message in delta[1:3]] == [
        "call-1",
        "call-2",
    ]
    assert delta[-1]["content"] == "done"
    assert "reasoning_content" not in delta[-1]


@pytest.mark.asyncio
async def test_completed_turn_persists_full_tool_evidence_and_restarts(tmp_path):
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        agent = Agent(session_id="canonical")
        with (
            patch.object(
                agent,
                "_build_tools_and_registry",
                new=AsyncMock(return_value=([], {})),
            ),
            patch.object(
                agent,
                "_run_with_tools",
                new=AsyncMock(return_value=("done", _tool_delta())),
            ),
        ):
            result = await agent._process_message("inspect", tmp_path, stream=False, show_progress=False)

        restarted = Agent(session_id="canonical")

    assert result == "done"
    assert [message["role"] for message in agent.conversation_history] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert restarted.conversation_history == agent.conversation_history
    assert restarted._session_state.model_context() == agent.conversation_history


def _echo_config() -> AnkaloopConfig:
    return AnkaloopConfig(
        servers={},
        chat=ChatConfig(model="glm-4.7", requires_reasoning_echo=True),
        context=ContextConfig(),
    )


class _ThinkingFakeLLM:
    """Fake LLM whose responses carry a reasoning trace on every step."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "think",
                        "arguments": json.dumps({"thought": "one"}),
                    }
                ],
                thinking="trace-tool",
                usage=None,
            )
        return SimpleNamespace(content="done", tool_calls=None, thinking="trace-final", usage=None)

    async def aclose(self):
        return None


async def _run_thinking_turn(tmp_path, config: AnkaloopConfig) -> Agent:
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=config),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=_ThinkingFakeLLM()),
    ):
        agent = Agent(session_id="reasoning")
        think = create_default_tool_registry(enable_task=False).get_tool("think")
        with patch.object(
            agent,
            "_build_tools_and_registry",
            new=AsyncMock(return_value=([think.get_spec()], {})),
        ):
            result = await agent._process_message("think", tmp_path, stream=False, show_progress=False)
    assert result == "done"
    return agent


@pytest.mark.asyncio
async def test_completed_turn_replays_reasoning_content_in_history(tmp_path):
    """Echo-enabled providers get reasoning_content replayed from history.

    Reasoning-mandatory providers (e.g. Reflection) reject a request unless
    every earlier assistant message carries the reasoning_content it was
    returned with, so the tool loop stores it on both the tool-calling and
    the final assistant message.
    """
    agent = await _run_thinking_turn(tmp_path, _echo_config())

    history = agent.conversation_history
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[1]["reasoning_content"] == "trace-tool"
    assert history[1]["tool_calls"][0]["id"] == "call-1"
    assert history[3]["reasoning_content"] == "trace-final"
    assert agent._session_state.model_context() == history


@pytest.mark.asyncio
async def test_reasoning_content_dropped_when_echo_disabled(tmp_path):
    """Default providers never replay reasoning: storing it would only
    inflate prompt tokens on every later turn."""
    agent = await _run_thinking_turn(tmp_path, _config())

    history = agent.conversation_history
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "assistant"]
    assert all("reasoning_content" not in message for message in history)
    assert history[3]["content"] == "done"


@pytest.mark.asyncio
async def test_failed_and_cancelled_turns_leave_committed_state_unchanged(tmp_path):
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        agent = Agent(session_id="rollback")
        agent._session_state.commit_turn(
            "existing",
            [
                {"role": "user", "content": "before"},
                {"role": "assistant", "content": "saved"},
            ],
        )
        agent._apply_session_state(agent._session_state)
        agent._save_conversation_history()
        original = list(agent.conversation_history)

        with (
            patch.object(
                agent,
                "_build_tools_and_registry",
                new=AsyncMock(return_value=([], {})),
            ),
            patch.object(
                agent,
                "_run_with_tools",
                new=AsyncMock(side_effect=AgentExecutionError("provider failed")),
            ),
            pytest.raises(AgentExecutionError, match="provider failed"),
        ):
            await agent._process_message("fail", tmp_path, stream=False, show_progress=False)
        assert agent.conversation_history == original

        with (
            patch(
                "ankaloop.agent.run_user_prompt_hooks",
                new=AsyncMock(side_effect=RuntimeError("hook failed")),
            ),
            pytest.raises(RuntimeError, match="hook failed"),
        ):
            await agent._process_message("hook", tmp_path, stream=False, show_progress=False)

        assert agent.conversation_history == original

        started = asyncio.Event()

        async def wait_forever(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        with (
            patch.object(
                agent,
                "_build_tools_and_registry",
                new=AsyncMock(return_value=([], {})),
            ),
            patch.object(agent, "_run_with_tools", side_effect=wait_forever),
        ):
            task = asyncio.create_task(agent._process_message("cancel", tmp_path, stream=False, show_progress=False))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert agent.conversation_history == original


@pytest.mark.asyncio
async def test_save_failure_fails_turn_handle_without_advancing_memory(tmp_path):
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        agent = Agent(session_id="commit-failure")
        with (
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
            patch.object(
                agent._session_store,
                "save",
                side_effect=SessionSaveError("disk full"),
            ),
        ):
            handle = await agent.submit("write", work_dir=tmp_path, stream=False, show_progress=False)
            with pytest.raises(AgentExecutionError, match="disk full"):
                await handle.wait()

    assert handle.status == TurnStatus.FAILED
    assert agent.conversation_history == []
    assert agent.total_llm_calls == 0


@pytest.mark.asyncio
async def test_checkpoint_restart_only_compacts_new_committed_prefix(tmp_path):
    compacted_inputs: list[list[dict]] = []

    class FakeCompactor:
        threshold_tokens = 1
        config = SimpleNamespace(min_tokens_to_compact=1, preserve_last=2)

        def __init__(self, *_args, **_kwargs):
            pass

        def compact_checkpoint(self, messages):
            compacted_inputs.append(messages)
            summary = f"summary-{len(compacted_inputs)}"
            return (
                [{"role": "assistant", "content": summary}],
                CompactionResult(
                    original_tokens=100,
                    compacted_tokens=10,
                    messages_removed=len(messages),
                    messages_preserved=1,
                    strategy_used=CompactionStrategy.SUMMARY,
                    summary=summary,
                ),
            )

    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.agent.SmartCompactor", FakeCompactor),
        patch("ankaloop.agent.estimate_request_tokens", return_value=100),
        patch("ankaloop.agent.estimate_tokens", return_value=100),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        agent = Agent(session_id="checkpoint-restart")
        for index in range(4):
            agent._session_state.commit_turn(
                f"seed-{index}",
                [
                    {"role": "user", "content": f"old-user-{index}"},
                    {"role": "assistant", "content": f"old-answer-{index}"},
                ],
            )
        agent._apply_session_state(agent._session_state)
        agent._save_conversation_history()

        with (
            patch.object(
                agent,
                "_build_tools_and_registry",
                new=AsyncMock(return_value=([], {})),
            ),
            patch.object(
                agent,
                "_run_with_tools",
                new=AsyncMock(return_value=("first", [])),
            ),
            patch.object(agent, "_run_memory_review", new=AsyncMock(return_value=False)),
        ):
            await agent._process_message("new-one", tmp_path, stream=False, show_progress=False)

        restarted = Agent(session_id="checkpoint-restart")
        with (
            patch.object(
                restarted,
                "_build_tools_and_registry",
                new=AsyncMock(return_value=([], {})),
            ),
            patch.object(
                restarted,
                "_run_with_tools",
                new=AsyncMock(return_value=("second", [])),
            ),
            patch.object(
                restarted,
                "_run_memory_review",
                new=AsyncMock(return_value=False),
            ),
        ):
            await restarted._process_message("new-two", tmp_path, stream=False, show_progress=False)

    assert len(compacted_inputs) == 2
    assert compacted_inputs[1][0]["content"] == "summary-1"
    second_contents = [message.get("content") for message in compacted_inputs[1]]
    assert "old-user-0" not in second_contents
    assert restarted._session_state.checkpoint.generation == 2
