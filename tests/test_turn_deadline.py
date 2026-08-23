"""Turn wall-clock deadline + Telegram bot API resilience tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.agent import Agent
from ankaloop.application_services import ApplicationServices
from ankaloop.config import ChatConfig
from ankaloop.multi_agent import AgentRegistry
from ankaloop.tools import ToolRegistry
from ankaloop.turn_service import TurnDeadlineExceededError, TurnService


def _services() -> ApplicationServices:
    return ApplicationServices(
        tool_registry=ToolRegistry(),
        agent_registry=AgentRegistry(),
        skill_manager=MagicMock(),
        transcript_store=MagicMock(),
        memory_manager_factory=MagicMock(),
    )


def _agent(tmp_path, chat: ChatConfig | None = None) -> Agent:
    services = _services()
    with patch("ankaloop.agent.Path.home", return_value=tmp_path):
        agent = Agent(services=services)
    if chat is not None:
        cfg = MagicMock()
        cfg.chat = chat
        agent._resolve_turn_config = MagicMock(return_value=cfg)  # type: ignore[method-assign]
    return agent


@pytest.mark.asyncio
async def test_turn_deadline_cancels_hung_turn(tmp_path):
    """A turn stuck past its deadline raises TurnDeadlineExceededError."""
    chat = ChatConfig()
    chat.turn_deadline_seconds = 0.2
    agent = _agent(tmp_path, chat)

    events: list[str] = []
    agent._emit_event = lambda t, d: events.append(t)  # type: ignore[method-assign]

    async def hang(*args, **kwargs):
        await asyncio.sleep(30)
        return "never"

    agent.turn_service._process_message_inner = hang  # type: ignore[method-assign]

    with pytest.raises(TurnDeadlineExceededError):
        await agent.turn_service.process_message("hello", None, False, False)

    assert "turn.deadline_exceeded" in events


@pytest.mark.asyncio
async def test_turn_deadline_zero_disables_backstop(tmp_path):
    """turn_deadline_seconds=0 opts out of the backstop entirely."""
    chat = ChatConfig()
    chat.turn_deadline_seconds = 0
    agent = _agent(tmp_path, chat)

    async def slow_but_fine(*args, **kwargs):
        await asyncio.sleep(0.1)
        return "ok"

    agent.turn_service._process_message_inner = slow_but_fine  # type: ignore[method-assign]
    assert await agent.turn_service.process_message("hi", None, False, False) == "ok"


@pytest.mark.asyncio
async def test_turn_deadline_not_hit_under_normal_turn(tmp_path):
    chat = ChatConfig()
    chat.turn_deadline_seconds = 5
    agent = _agent(tmp_path, chat)
    agent.turn_service._process_message_inner = AsyncMock(return_value="done")  # type: ignore[method-assign]
    assert await agent.turn_service.process_message("hi", None, False, False) == "done"


@pytest.mark.asyncio
async def test_turn_deadline_defaults_when_config_missing(tmp_path):
    agent = _agent(tmp_path, None)
    agent._resolve_turn_config = MagicMock(side_effect=RuntimeError("no config"))  # type: ignore[method-assign]
    assert agent.turn_service._turn_deadline_seconds() == ChatConfig().turn_deadline_seconds


def test_chat_config_deadline_decode():
    from ankaloop.config import _decode_chat, _encode_chat

    chat = _decode_chat({"turn_deadline_seconds": 300.0})
    assert chat is not None
    assert chat.turn_deadline_seconds == 300.0

    encoded = _encode_chat(chat)
    assert encoded is not None
    assert encoded["turn_deadline_seconds"] == 300.0

    # default stays out of the encoded output
    default_chat = _encode_chat(ChatConfig())
    assert default_chat is not None
    assert "turn_deadline_seconds" not in default_chat


def test_chat_config_deadline_default():
    assert ChatConfig().turn_deadline_seconds == 900.0
