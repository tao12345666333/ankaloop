"""Tests for canonical session turns, commits, and restart behavior."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.agent import SESSION_SAVE_MAX_ATTEMPTS, Agent, AgentExecutionError
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


def _commit_simple_turn(agent: Agent, turn_id: str, user: str, assistant: str) -> None:
    """Commit one user/assistant turn through the canonical save path."""
    agent._session_state.commit_turn(
        turn_id,
        [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
    )
    agent._apply_session_state(agent._session_state)
    agent._save_conversation_history()


def _answer_with(side_effect):
    """Mocked tool-building plus a tool loop that delegates to side_effect."""
    return (
        patch(
            "ankaloop.agent.Agent._build_tools_and_registry",
            new=AsyncMock(return_value=([], {})),
        ),
        patch(
            "ankaloop.agent.Agent._run_with_tools",
            new=AsyncMock(side_effect=side_effect),
        ),
    )


@pytest.mark.asyncio
async def test_recreated_process_adopts_persisted_state_before_turn(tmp_path):
    """A stale serving process self-heals at turn start: it adopts the store
    state, so the model sees the full history and the commit applies."""
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        stale = Agent(session_id="recreated")
        _commit_simple_turn(stale, "turn-1", "first", "one")

        replacement = Agent(session_id="recreated")
        _commit_simple_turn(replacement, "turn-2", "second", "two")
        _commit_simple_turn(replacement, "turn-3", "third", "three")
        assert replacement._session_state.revision == 3
        assert stale._session_state.revision == 1

        seen_messages: list[list[dict]] = []

        async def answer(**kwargs):
            seen_messages.append(kwargs["messages"])
            return "four", []

        build_patch, run_patch = _answer_with(answer)
        with build_patch, run_patch:
            result = await stale._process_message("fourth", tmp_path, stream=False, show_progress=False)

        restarted = Agent(session_id="recreated")

    assert result == "four"
    context_contents = [message.get("content") for message in seen_messages[0]]
    assert "second" in context_contents
    assert "third" in context_contents
    assert [turn.turn_id for turn in stale._session_state.turns][:3] == ["turn-1", "turn-2", "turn-3"]
    assert stale._session_state.revision == 4
    assert [message["content"] for message in restarted.conversation_history] == [
        "first",
        "one",
        "second",
        "two",
        "third",
        "three",
        "fourth",
        "four",
    ]


@pytest.mark.asyncio
async def test_commit_rebases_when_store_advances_mid_turn(tmp_path):
    """When the store advances after the turn-start sync, the end-of-turn
    commit rebases its append-only delta onto the persisted turns instead of
    poisoning the session, and usage counters merge instead of resetting."""
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        stale = Agent(session_id="racy")
        _commit_simple_turn(stale, "turn-1", "first", "one")

        async def advance_store_then_answer(**_kwargs):
            other = Agent(session_id="racy")
            _commit_simple_turn(other, "turn-2", "second", "two")
            other._session_state.commit_turn(
                "turn-3",
                [
                    {"role": "user", "content": "third"},
                    {"role": "assistant", "content": "three"},
                ],
            )
            other.total_output_tokens = 7
            other._save_conversation_history()
            stale.total_input_tokens += 100  # this turn's own usage delta
            return "late answer", []

        build_patch, run_patch = _answer_with(advance_store_then_answer)
        with build_patch, run_patch:
            result = await stale._process_message("fourth", tmp_path, stream=False, show_progress=False)

        restarted = Agent(session_id="racy")

    assert result == "late answer"
    assert [turn.turn_id for turn in restarted._session_state.turns][:3] == [
        "turn-1",
        "turn-2",
        "turn-3",
    ]
    assert len(restarted._session_state.turns) == 4
    assert restarted._session_state.revision == 4
    assert [message["content"] for message in restarted.conversation_history] == [
        "first",
        "one",
        "second",
        "two",
        "third",
        "three",
        "fourth",
        "late answer",
    ]
    assert restarted.total_output_tokens == 7
    assert restarted.total_input_tokens == 100


@pytest.mark.asyncio
async def test_commit_conflict_surfaces_when_store_diverges_mid_turn(tmp_path):
    """A store that is not a forward extension of the process's state (here:
    reset by another process) is true divergence — the conflict must surface
    and the stale turn must not be resurrected onto the reset session."""
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        stale = Agent(session_id="diverged")
        _commit_simple_turn(stale, "turn-1", "first", "one")

        async def reset_store_then_answer(**_kwargs):
            other = Agent(session_id="diverged")
            await other.reset_session()
            return "lost", []

        build_patch, run_patch = _answer_with(reset_store_then_answer)
        with (
            build_patch,
            run_patch,
            pytest.raises(AgentExecutionError, match="changed from revision"),
        ):
            await stale._process_message("fourth", tmp_path, stream=False, show_progress=False)

        restarted = Agent(session_id="diverged")

    assert restarted.conversation_history == []
    assert restarted._session_state.revision == 2


@pytest.mark.asyncio
async def test_commit_converges_when_store_advances_repeatedly_mid_turn(tmp_path):
    """The store advances twice — once before the first save and again
    between the first rebase and its save. The bounded retry loop rebases
    the original candidate onto the freshest state and the turn still lands."""
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        stale = Agent(session_id="double-race")
        _commit_simple_turn(stale, "turn-1", "first", "one")

        async def advance_store_then_answer(**_kwargs):
            other = Agent(session_id="double-race")
            _commit_simple_turn(other, "turn-2", "second", "two")
            return "converged", []

        original_rebase = Agent._rebase_session_candidate
        advanced_again = False

        def rebase_then_advance_again(self, candidate, conflict):
            nonlocal advanced_again
            rebased = original_rebase(self, candidate, conflict)
            if not advanced_again:
                advanced_again = True
                late = Agent(session_id="double-race")
                _commit_simple_turn(late, "turn-3", "third", "three")
            return rebased

        build_patch, run_patch = _answer_with(advance_store_then_answer)
        with (
            build_patch,
            run_patch,
            patch.object(Agent, "_rebase_session_candidate", new=rebase_then_advance_again),
        ):
            result = await stale._process_message("fourth", tmp_path, stream=False, show_progress=False)

        restarted = Agent(session_id="double-race")

    assert result == "converged"
    assert [turn.turn_id for turn in restarted._session_state.turns][:3] == [
        "turn-1",
        "turn-2",
        "turn-3",
    ]
    assert len(restarted._session_state.turns) == 4
    assert restarted._session_state.revision == 4
    assert [message["content"] for message in restarted.conversation_history] == [
        "first",
        "one",
        "second",
        "two",
        "third",
        "three",
        "fourth",
        "converged",
    ]


@pytest.mark.asyncio
async def test_commit_gives_up_after_bounded_rebase_attempts(tmp_path):
    """A store that advances on every single attempt never converges, so the
    conflict surfaces after SESSION_SAVE_MAX_ATTEMPTS saves — the retry loop
    must be bounded and must not resurrect the turn on the failed state."""
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
        patch(
            "ankaloop.agent.run_user_prompt_hooks",
            new=AsyncMock(return_value=HookOutput()),
        ),
        patch("ankaloop.llm.create_llm_client", return_value=MagicMock(aclose=AsyncMock())),
    ):
        stale = Agent(session_id="endless-race")
        _commit_simple_turn(stale, "turn-1", "first", "one")

        async def advance_store_then_answer(**_kwargs):
            other = Agent(session_id="endless-race")
            _commit_simple_turn(other, "turn-2", "second", "two")
            return "lost", []

        original_rebase = Agent._rebase_session_candidate
        race_count = 0

        def rebase_then_advance_again(self, candidate, conflict):
            nonlocal race_count
            race_count += 1
            rebased = original_rebase(self, candidate, conflict)
            late = Agent(session_id="endless-race")
            _commit_simple_turn(late, f"racing-{race_count}", f"race {race_count}", f"raced {race_count}")
            return rebased

        original_save = stale._session_store.save
        save_calls = 0

        def counting_save(*args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            return original_save(*args, **kwargs)

        build_patch, run_patch = _answer_with(advance_store_then_answer)
        with (
            build_patch,
            run_patch,
            patch.object(Agent, "_rebase_session_candidate", new=rebase_then_advance_again),
            patch.object(stale._session_store, "save", side_effect=counting_save),
            pytest.raises(AgentExecutionError, match="changed from revision"),
        ):
            await stale._process_message("fourth", tmp_path, stream=False, show_progress=False)

        restarted = Agent(session_id="endless-race")

    assert save_calls == SESSION_SAVE_MAX_ATTEMPTS
    assert [message["content"] for message in restarted.conversation_history] == [
        "first",
        "one",
        "second",
        "two",
        "race 1",
        "raced 1",
        "race 2",
        "raced 2",
    ]
    assert [turn.turn_id for turn in stale._session_state.turns] == ["turn-1"]


def test_sync_adopts_store_state_even_when_store_is_older(tmp_path):
    """A filesystem snapshot restore can roll the store back behind the
    process's committed revision; the store stays the source of truth."""
    with (
        patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
        patch("ankaloop.agent.load_config", return_value=_config()),
    ):
        agent = Agent(session_id="rolled-back")
        _commit_simple_turn(agent, "turn-1", "first", "one")
        _commit_simple_turn(agent, "turn-2", "second", "two")
        assert agent._session_state.revision == 2

        rolled_back = SessionState(session_id="rolled-back", agent_name=agent.name)
        rolled_back.commit_turn(
            "turn-1",
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "one"},
            ],
        )
        agent._session_store.delete()
        agent._session_store.save(rolled_back.to_snapshot())
        assert agent._session_store.read_revision() == 1

        agent._sync_session_state_from_store()

    assert [message["content"] for message in agent.conversation_history] == ["first", "one"]
    assert agent._session_state.revision == 1


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
