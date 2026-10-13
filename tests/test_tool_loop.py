"""Tests for ToolLoop turn orchestration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.tool_loop import ToolLoop


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
