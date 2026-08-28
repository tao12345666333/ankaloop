"""Tests for the durable tool-boundary journal and its recovery resolver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ankaloop.tool_journal import (
    JOURNAL_PROTOCOL,
    ToolJournal,
    ToolJournalError,
    canonical_args_hash,
    classify_recovery_mode,
    resolve_journal,
)


@pytest.fixture
def journal(tmp_path: Path) -> ToolJournal:
    return ToolJournal(tmp_path, "sess-journal")


class TestToolJournalStore:
    def test_events_persist_as_jsonl(self, journal: ToolJournal, tmp_path: Path):
        journal.turn_started("t1", "hello")
        journal.tool_intent("t1", "call_1", "bash", {"command": "ls"})
        journal.tool_outcome("t1", "call_1", success=True, duration_ms=12.5)
        journal.turn_committed("t1", 7)

        events = journal.read()
        assert [e["kind"] for e in events] == ["turn_started", "tool_intent", "tool_outcome", "turn_committed"]
        assert events[0]["protocol"] == JOURNAL_PROTOCOL
        assert events[1]["operation_id"] == "t1:call_1"
        assert events[1]["canonical_args_hash"] == canonical_args_hash("bash", {"command": "ls"})
        assert events[3]["revision"] == 7
        # Every line on disk is valid JSON.
        lines = journal.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4
        assert all(isinstance(json.loads(line), dict) for line in lines)

    def test_disabled_journal_is_noop(self, tmp_path: Path):
        journal = ToolJournal(tmp_path, "sess-x", enabled=False)
        journal.turn_started("t1", "hi")
        journal.tool_intent("t1", "c", "bash", {})
        journal.tool_outcome("t1", "c", success=True)
        journal.turn_committed("t1", 1)
        assert journal.read() == []
        assert not journal.path.exists()

    def test_intent_write_failure_raises(self, tmp_path: Path):
        journal = ToolJournal(tmp_path / "missing-root", "sess-y")
        # Point the journal at a location that cannot be a directory.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        journal.root = blocker
        journal.path = blocker / "sess-y.journal.jsonl"
        journal.lock_path = blocker / ".sess-y.journal.lock"
        with pytest.raises(ToolJournalError):
            journal.tool_intent("t1", "c1", "bash", {})

    def test_trim_never_crosses_newest_turn_boundary(self, tmp_path: Path):
        journal = ToolJournal(tmp_path, "sess-trim", max_events=10)
        # Turn A: many settled operations (trimmable).
        journal.turn_started("ta", "old")
        for i in range(12):
            journal.tool_intent("ta", f"c{i}", "grep", {"q": i})
            journal.tool_outcome("ta", f"c{i}", success=True)
        journal.turn_committed("ta", 1)
        # Turn B: one open operation (must survive trimming).
        journal.turn_started("tb", "new")
        journal.tool_intent("tb", "cX", "bash", {"command": "rm -rf /tmp/x"})

        events = journal.read()
        assert len(events) <= 10
        kinds = [e["kind"] for e in events]
        assert "turn_started" in kinds  # newest turn boundary retained
        assert kinds[-1] == "tool_intent"
        # The open operation resolves as a suspected crash, not lost.
        recovery = journal.resolve()
        assert recovery.interrupted_turns[0].turn_id == "tb"
        assert recovery.interrupted_turns[0].open_operations == ("tb:cX",)

    @pytest.mark.parametrize("padding", range(0, 13))
    def test_trim_cut_never_lands_inside_a_turn(self, tmp_path: Path, padding: int):
        """A trim target between turns must shift forward to a boundary.

        The naive ``min(drop_until, last_turn_start)`` clamp cuts at an
        arbitrary offset, which slices an older turn and manufactures a
        false crash or corruption.  Whatever the padding, the retained
        journal must resolve identically to the same turns without any
        trimming: no orphaned outcome, no manufactured indeterminate.
        """
        journal = ToolJournal(tmp_path, f"sess-pad-{padding}", max_events=10)
        # Turn A: 1 + padding * 2 + 1 settled events (trimmable).
        journal.turn_started("ta", "old")
        journal.tool_intent("ta", "cA", "grep", {"q": "a"})
        for i in range(padding):
            journal.tool_intent("ta", f"cp{i}", "grep", {"q": i})
            journal.tool_outcome("ta", f"cp{i}", success=True)
        journal.tool_outcome("ta", "cA", success=True)
        journal.turn_committed("ta", 1)
        # Turn B: committed, would end up partially retained if cut mid-turn.
        journal.turn_started("tb", "middle")
        journal.tool_intent("tb", "cB", "read_file", {"path": "x"})
        journal.tool_outcome("tb", "cB", success=True)
        journal.turn_committed("tb", 2)
        # Turn C: open operation that must survive trimming.
        journal.turn_started("tc", "new")
        journal.tool_intent("tc", "cC", "bash", {"command": "echo"})

        recovery = journal.resolve()
        assert recovery.interrupted_turns[0].turn_id == "tc"
        assert recovery.interrupted_turns[0].open_operations == ("tc:cC",)
        assert not recovery.has_corruption
        statuses = {d.operation_id: d.status for d in recovery.decisions}
        # Every retained settled op stays completed; nothing was manufactured.
        for settled_op in ("ta:cA", "tb:cB"):
            assert statuses.get(settled_op, "completed") == "completed"
        # Any retained turn keeps its full event sequence (no half-turn cut).
        events = journal.read()
        retained_turns = {str(e.get("turn_id")) for e in events if e.get("kind") == "turn_started"}
        # ta: started + intent + padding*2 + outcome + committed.
        expected_events = {"ta": 4 + 2 * padding, "tb": 4, "tc": 2}
        for turn_id in retained_turns:
            count = sum(1 for e in events if str(e.get("turn_id")) == turn_id)
            assert count == expected_events[turn_id]


class TestResolverDecisions:
    def test_settled_operation_is_completed(self):
        recovery = resolve_journal(
            [
                {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
                {
                    "kind": "tool_intent",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "tool_call_id": "c",
                    "tool_name": "bash",
                    "recovery_mode": "never_auto_retry",
                },
                {"kind": "tool_outcome", "turn_id": "t", "operation_id": "t:c", "success": True},
                {"kind": "turn_committed", "turn_id": "t", "revision": 3},
            ]
        )
        assert not recovery.requires_attention
        assert recovery.decisions[0].status == "completed"
        assert not recovery.interrupted_turns

    def test_open_intent_without_terminal_is_crash_suspected(self):
        recovery = resolve_journal(
            [
                {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
                {
                    "kind": "tool_intent",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "tool_call_id": "c",
                    "tool_name": "bash",
                    "recovery_mode": "never_auto_retry",
                },
            ]
        )
        assert recovery.requires_attention
        decision = recovery.decisions[0]
        assert decision.status == "indeterminate"
        assert decision.recovery_mode == "never_auto_retry"
        assert len(recovery.interrupted_turns) == 1
        assert recovery.interrupted_turns[0].turn_id == "t"

    def test_open_intent_with_terminal_is_aborted_unsettled(self):
        recovery = resolve_journal(
            [
                {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
                {
                    "kind": "tool_intent",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "tool_call_id": "c",
                    "tool_name": "write_file",
                    "recovery_mode": "never_auto_retry",
                },
                {"kind": "turn_cancelled", "turn_id": "t"},
            ]
        )
        decision = recovery.decisions[0]
        assert decision.status == "aborted_unsettled"
        assert not recovery.interrupted_turns
        # Cancel with an open op still warrants attention but is not a crash.
        assert any(d.status != "completed" for d in recovery.decisions)

    def test_orphaned_outcome_is_corruption(self):
        recovery = resolve_journal(
            [
                {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
                {"kind": "tool_outcome", "turn_id": "t", "operation_id": "t:ghost", "success": True},
                {"kind": "turn_committed", "turn_id": "t", "revision": 1},
            ]
        )
        assert recovery.has_corruption
        assert any(d.status == "corruption" for d in recovery.decisions)

    def test_duplicate_intent_is_corruption(self):
        recovery = resolve_journal(
            [
                {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
                {
                    "kind": "tool_intent",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "tool_call_id": "c",
                    "tool_name": "bash",
                    "recovery_mode": "never_auto_retry",
                },
                {
                    "kind": "tool_intent",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "tool_call_id": "c",
                    "tool_name": "bash",
                    "recovery_mode": "never_auto_retry",
                },
            ]
        )
        assert recovery.has_corruption

    def test_outcome_hash_mismatch_is_corruption(self):
        recovery = resolve_journal(
            [
                {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
                {
                    "kind": "tool_intent",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "tool_call_id": "c",
                    "tool_name": "bash",
                    "recovery_mode": "never_auto_retry",
                    "canonical_args_hash": "aaa",
                },
                {
                    "kind": "tool_outcome",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "success": True,
                    "canonical_args_hash": "bbb",
                },
                {"kind": "turn_committed", "turn_id": "t", "revision": 1},
            ]
        )
        assert recovery.has_corruption
        assert any(d.reason == "outcome args hash differs from intent" for d in recovery.decisions)

    def test_outcome_hash_match_is_completed(self):
        recovery = resolve_journal(
            [
                {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
                {
                    "kind": "tool_intent",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "tool_call_id": "c",
                    "tool_name": "bash",
                    "recovery_mode": "never_auto_retry",
                    "canonical_args_hash": "aaa",
                },
                {
                    "kind": "tool_outcome",
                    "turn_id": "t",
                    "operation_id": "t:c",
                    "success": True,
                    "canonical_args_hash": "aaa",
                },
                {"kind": "turn_committed", "turn_id": "t", "revision": 1},
            ]
        )
        assert not recovery.has_corruption
        assert recovery.decisions[0].status == "completed"

    def test_empty_and_garbage_events_are_tolerated(self):
        assert resolve_journal([]).requires_attention is False
        recovery = resolve_journal([{"kind": "unknown"}, {"other": 1}])
        assert not recovery.has_corruption

    def test_acknowledged_operations_stop_requiring_attention(self):
        events = [
            {"kind": "turn_started", "turn_id": "t", "protocol": JOURNAL_PROTOCOL},
            {
                "kind": "tool_intent",
                "turn_id": "t",
                "operation_id": "t:c",
                "tool_call_id": "c",
                "tool_name": "bash",
                "recovery_mode": "never_auto_retry",
            },
            {"kind": "recovery_acknowledged", "turns": ["t"], "operations": ["t:c"]},
        ]
        assert resolve_journal(events).requires_attention is False
        decision = resolve_journal(events).decisions[0]
        assert decision.status == "acknowledged"
        # A new open operation after the ack is still a crash suspect.
        events.append(
            {
                "kind": "tool_intent",
                "turn_id": "t2",
                "operation_id": "t2:c2",
                "tool_call_id": "c2",
                "tool_name": "bash",
                "recovery_mode": "never_auto_retry",
            }
        )
        recovery = resolve_journal(events)
        assert recovery.requires_attention is True
        assert any(d.operation_id == "t2:c2" and d.status == "indeterminate" for d in recovery.decisions)


class TestAcknowledgeWorkflow:
    def test_mark_recovery_acknowledged_persists_and_resolves(self, journal: ToolJournal):
        assert journal.mark_recovery_acknowledged() == {"turns": [], "operations": []}
        journal.turn_started("t1", "do work")
        journal.tool_intent("t1", "c1", "bash", {"command": "echo"})

        assert journal.resolve().requires_attention is True
        acked = journal.mark_recovery_acknowledged()
        assert acked == {"turns": ["t1"], "operations": ["t1:c1"]}

        recovery = journal.resolve()
        assert recovery.requires_attention is False
        assert recovery.interrupted_turns[0].acknowledged is True
        assert recovery.decisions[0].status == "acknowledged"
        # The ack is durable across a fresh journal instance.
        reopened = ToolJournal(journal.root, journal.session_id)
        assert reopened.resolve().requires_attention is False

    def test_ack_without_open_operations_appends_nothing(self, journal: ToolJournal):
        journal.turn_started("t1", "hi")
        journal.tool_intent("t1", "c1", "grep", {"q": 1})
        journal.tool_outcome("t1", "c1", success=True)
        journal.turn_committed("t1", 1)
        before = journal.read()
        assert journal.mark_recovery_acknowledged() == {"turns": [], "operations": []}
        assert journal.read() == before


class TestClassification:
    def test_read_only_tools_are_replay_safe(self):
        assert classify_recovery_mode("grep") == "replay_safe"
        assert classify_recovery_mode("read_file") == "replay_safe"
        assert classify_recovery_mode("think") == "replay_safe"

    def test_side_effect_tools_never_auto_retry(self):
        assert classify_recovery_mode("bash") == "never_auto_retry"
        assert classify_recovery_mode("write_file") == "never_auto_retry"
        assert classify_recovery_mode("apply_patch") == "never_auto_retry"
        # Unknown/MCP tools default to the conservative mode.
        assert classify_recovery_mode("mcp__custom__deploy") == "never_auto_retry"

    def test_canonical_hash_is_order_insensitive(self):
        assert canonical_args_hash("bash", {"a": 1, "b": 2}) == canonical_args_hash("bash", {"b": 2, "a": 1})
        assert canonical_args_hash("bash", {"a": 1}) != canonical_args_hash("bash", {"a": 2})


class TestToolLoopWiring:
    """The tool loop must write T1 before execution and T2 after the result."""

    @pytest.fixture
    def agent(self, tmp_path: Path):
        from unittest.mock import MagicMock, patch

        from ankaloop.agent import Agent
        from ankaloop.config import AnkaloopConfig, ContextConfig

        with patch("ankaloop.agent.Path.home") as mock_home, patch("ankaloop.agent.load_config") as mock_load:
            mock_home.return_value = tmp_path
            mock_load.return_value = MagicMock()
            agent = Agent(session_id="journal-wiring")
        return agent

    @pytest.mark.asyncio
    async def test_bash_call_writes_intent_and_outcome(self, agent, tmp_path: Path):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from ankaloop.tools import create_default_tool_registry

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
                                "arguments": json.dumps({"command": "echo hi"}),
                            }
                        ],
                    )
                return SimpleNamespace(content="done", tool_calls=None)

        agent.execution_context["turn_id"] = "turn-abc"
        result = await agent._enhanced_chat_with_tools(
            llm_client=FakeLLM(),
            messages=[{"role": "user", "content": "run echo"}],
            tools=[create_default_tool_registry(enable_task=False).get_tool("bash").get_spec()],
            tool_registry={},
            stream=False,
            status=MagicMock(),
            work_dir=tmp_path,
        )
        assert result == "done"

        events = agent._tool_journal.read()
        kinds = [e["kind"] for e in events]
        # No turn_started here (turn_service owns that), but the tool loop
        # must have journaled the boundary pair for its execution.
        assert kinds == ["tool_intent", "tool_outcome"]
        intent = events[0]
        assert intent["turn_id"] == "turn-abc"
        assert intent["operation_id"] == "turn-abc:call_1"
        assert intent["recovery_mode"] == "never_auto_retry"
        assert events[1]["success"] is True

        recovery = agent._tool_journal.resolve()
        assert [d.status for d in recovery.decisions] == ["completed"]
        # T2 carries the intent's canonical args hash for settlement
        # verification.
        outcome = events[1]
        assert outcome["canonical_args_hash"] == canonical_args_hash("bash", {"command": "echo hi"})

    @pytest.mark.asyncio
    async def test_denied_tool_writes_no_intent(self, agent, tmp_path: Path):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        class FakeLLM:
            def chat(self, messages, **_kwargs):
                return SimpleNamespace(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_d",
                            "name": "not_a_real_tool",
                            "arguments": json.dumps({"x": 1}),
                        }
                    ],
                )

        agent.execution_context["turn_id"] = "turn-deny"
        from ankaloop.agent import MaxStepsReached

        with pytest.raises(MaxStepsReached):
            await agent._enhanced_chat_with_tools(
                llm_client=FakeLLM(),
                messages=[{"role": "user", "content": "denied"}],
                tools=[],
                tool_registry={},
                stream=False,
                status=MagicMock(),
                work_dir=tmp_path,
                max_steps=1,
            )
        # Permission-denied calls cross no boundary: no journal events.
        assert agent._tool_journal.read() == []

    @pytest.mark.asyncio
    async def test_crash_between_boundaries_is_indeterminate(self, agent, tmp_path: Path):
        journal = agent._tool_journal
        journal.turn_started("turn-crash", "do work")
        journal.tool_intent("turn-crash", "call_z", "bash", {"command": "curl -X POST ..."})

        recovery = journal.resolve()
        decision = recovery.decisions[0]
        assert decision.status == "indeterminate"
        assert decision.recovery_mode == "never_auto_retry"
        assert recovery.interrupted_turns[0].turn_id == "turn-crash"
        summary = recovery.summary()
        assert summary["open_operations"][0]["tool_name"] == "bash"


class TestTurnServiceWiring(TestToolLoopWiring):
    @pytest.mark.asyncio
    async def test_turn_lifecycle_events_in_journal(self, agent, tmp_path: Path):
        """Full turn through process_message journals started + committed."""
        from unittest.mock import MagicMock, patch

        from ankaloop.config import AnkaloopConfig, ChatConfig, ContextConfig
        from ankaloop.runtime import TurnRequest

        with patch.object(agent, "_resolve_turn_config") as mock_cfg:
            mock_cfg.return_value = AnkaloopConfig(servers={}, chat=None, context=ContextConfig())
            with patch.object(agent, "_run_user_prompt_hooks") as mock_hooks:
                mock_hooks.return_value = MagicMock(continue_execution=True, stop_reason=None, feedback=None)
                with (
                    patch.object(agent, "_build_tools_and_registry", return_value=([], {})),
                    patch.object(agent, "_get_system_prompt", return_value="sys"),
                    patch("ankaloop.llm.create_llm_client"),
                ):
                    # A provider failure journals turn_failed, not a commit.
                    from ankaloop.llm import ProviderError, ProviderErrorKind

                    with patch.object(
                        agent,
                        "_run_with_tools",
                        side_effect=ProviderError(ProviderErrorKind.TIMEOUT),
                    ):
                        agent.execution_context["turn_id"] = "turn-fail"
                        with pytest.raises(ProviderError):
                            await agent.turn_service.process_message("hi", None, False, False)

        events = agent._tool_journal.read()
        kinds = [e["kind"] for e in events]
        assert kinds[0] == "turn_started"
        assert kinds[-1] == "turn_failed"
        recovery = agent._tool_journal.resolve()
        assert not recovery.interrupted_turns  # turn_failed is a terminal event
