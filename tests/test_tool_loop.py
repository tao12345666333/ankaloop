"""Tests for ToolLoop turn orchestration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.config import AnkaloopConfig, ChatConfig
from ankaloop.llm import LLMResponse
from ankaloop.tool_loop import ANSWER_CONTINUATION_PROMPT, MAX_ANSWER_CONTINUATIONS, ToolLoop


def _run_turn(agent: MagicMock, client: MagicMock):
    with patch("ankaloop.llm.create_llm_client", return_value=client):
        loop = ToolLoop(agent)
        return asyncio.run(loop.run([], [], {}, False, MagicMock()))


class TestToolLoopClientCleanup:
    """ToolLoop owns the per-turn LLM client and must close it on the serving loop.

    The interactive CLI runs each prompt in its own ``asyncio.run`` loop. If the
    client is left for GC, the provider SDK's ``__del__`` schedules transport
    cleanup on a later loop and raises ``RuntimeError: Event loop is closed``.
    """

    def test_closes_llm_client_after_successful_turn(self):
        client = MagicMock()
        client.aclose = AsyncMock()
        agent = MagicMock()
        agent._enhanced_chat_with_tools = AsyncMock(return_value=("answer", []))

        result = _run_turn(agent, client)

        assert result == ("answer", [])
        client.aclose.assert_awaited_once()

    def test_closes_llm_client_after_failed_turn(self):
        client = MagicMock()
        client.aclose = AsyncMock()
        agent = MagicMock()
        agent._enhanced_chat_with_tools = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            _run_turn(agent, client)

        client.aclose.assert_awaited_once()


def _make_agent(responses: list[LLMResponse]) -> MagicMock:
    """Mock agent with just enough surface for the real loop to run."""
    agent = MagicMock()
    agent.max_steps = 10
    agent.name = "tester"
    agent.session_id = "s1"
    agent.execution_context = {}
    agent.tool_calls_history = []
    agent.current_conversation_tool_calls = []
    agent.agent_spec.tools = None
    agent.agent_spec.exclude_tools = None
    agent.agent_spec.can_delegate = False
    agent.services.tool_registry = {}
    agent._task_manager = None
    agent._resolve_model_name.return_value = "test-model"
    agent._fit_tool_context.side_effect = lambda messages, tools, budget: messages
    agent._call_llm = AsyncMock(side_effect=responses)
    agent._should_limit_tool_calls.return_value = False
    return agent


def _run_loop(agent: MagicMock):
    cfg = AnkaloopConfig(servers={}, chat=ChatConfig())
    with patch("ankaloop.tool_loop.get_model_context_window", return_value=200_000):
        loop = ToolLoop(agent)
        return asyncio.run(
            loop.enhanced_chat_with_tools(
                llm_client=MagicMock(model="test-model"),
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                tool_registry={},
                stream=False,
                status=MagicMock(),
                return_message_delta=True,
                cfg=cfg,
            )
        )


class TestFinalAnswerCompletion:
    """The loop must not report truncated provider output as a clean success."""

    def test_plain_completion_returns_content(self):
        agent = _make_agent([LLMResponse(content="done", stop_reason="stop")])

        text, delta = _run_loop(agent)

        assert text == "done"
        assert delta[-1] == {"role": "assistant", "content": "done"}
        assert agent._call_llm.await_count == 1
        truncated = [c for c in agent._emit_event.call_args_list if c.args[0] == "message.truncated"]
        assert truncated == []

    def test_length_stop_continues_and_concatenates(self):
        agent = _make_agent(
            [
                LLMResponse(content="Hello wor", stop_reason="length"),
                LLMResponse(content="ld!", stop_reason="stop"),
            ]
        )

        text, delta = _run_loop(agent)

        assert text == "Hello world!"
        assert agent._call_llm.await_count == 2
        # The continuation round replays the partial answer plus the prompt
        # (positional: the mock holds the live list, which grows afterwards).
        second_call_messages = agent._call_llm.call_args.kwargs["messages"]
        assert second_call_messages[1] == {"role": "assistant", "content": "Hello wor"}
        assert second_call_messages[2] == {"role": "user", "content": ANSWER_CONTINUATION_PROMPT}
        # History commits only the concatenated message, without the exchange.
        assert delta[-1] == {"role": "assistant", "content": "Hello world!"}

    def test_truncation_marked_after_continuation_budget_exhausted(self):
        parts = [LLMResponse(content=chunk, stop_reason="length") for chunk in ("a", "b", "c", "d")]
        agent = _make_agent(parts)

        text, delta = _run_loop(agent)

        assert agent._call_llm.await_count == 1 + MAX_ANSWER_CONTINUATIONS
        assert text.startswith("abcd")
        assert "Response truncated" in text
        assert "finish_reason=length" in text
        # The committed history message stays clean; only the returned text is marked.
        assert delta[-1] == {"role": "assistant", "content": "abcd"}
        truncated = [c for c in agent._emit_event.call_args_list if c.args[0] == "message.truncated"]
        assert len(truncated) == 1
        assert truncated[0].args[1]["stop_reason"] == "length"
        assert truncated[0].args[1]["continuations"] == MAX_ANSWER_CONTINUATIONS

    def test_continued_answer_with_tool_calls_keeps_partial_text(self):
        """A continuation that produces tool calls must not lose earlier chunks."""
        agent = _make_agent(
            [
                LLMResponse(content="part1 ", stop_reason="length"),
                LLMResponse(
                    content="part2 ",
                    tool_calls=[{"id": "call_1", "name": "unknown-tool", "arguments": "{}"}],
                    stop_reason="stop",
                ),
                LLMResponse(content="done", stop_reason="stop"),
            ]
        )

        text, delta = _run_loop(agent)

        assert text == "done"
        tool_call_messages = [m for m in delta if m.get("tool_calls")]
        assert len(tool_call_messages) == 1
        assert tool_call_messages[0]["content"] == "part1 part2 "
