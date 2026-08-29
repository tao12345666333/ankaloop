import asyncio
import importlib
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ankaloop.runtime import CancellationResult
from ankaloop.telegram.auth import AuthMiddleware
from ankaloop.telegram.config import (
    TelegramConfig,
    TelegramGroupConfig,
    TelegramPairingConfig,
    TelegramStreamingConfig,
    TelegramTopicConfig,
)
from ankaloop.telegram.formatter import TelegramFormatter
from ankaloop.telegram.handlers import (
    RateLimiter,
    SessionManager,
    TelegramHandlers,
    TelegramQueuedMessage,
    _build_enriched_prompt,
    _exclude_none,
    _extract_reply_metadata,
)


class _FakeAgent:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.execution_context: dict[str, str] = {}
        self.flush_calls: list[dict] = []
        self.conversation_history: list[dict] = []
        self._busy = False
        self._queued = 0
        self.closed = False

    async def flush_memory(self, **kwargs):
        self.flush_calls.append(kwargs)
        return True

    def is_busy(self) -> bool:
        return self._busy

    def queued_count(self) -> int:
        return self._queued

    async def submit(self, prompt, **kwargs):
        handle = MagicMock(id=f"turn-{self.session_id}")
        handle.wait = AsyncMock(return_value="ok")
        return handle

    async def cancel(self, **kwargs):
        result = CancellationResult(self._busy, self._queued if kwargs.get("clear_queue") else 0)
        self._busy = False
        self._queued = 0
        return result

    async def reset_session(self):
        self.conversation_history = []

    async def close(self):
        self.closed = True
        await self.cancel(clear_queue=True)


class _FakeBot:
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self.pairing_requests: list[tuple[int, int, str | None]] = []
        self.sent_texts: list[tuple[int, str]] = []
        self.prompts: list[tuple[int, int, str]] = []
        self.model_provider_requests: list[str] = []
        self.flushed_sessions: list[tuple[int, str]] = []
        self.cancelled_turns: list[tuple[int, str]] = []
        self.session_manager: SessionManager | None = None

    def create_pairing_request(self, user_id: int, chat_id: int, username: str | None = None):
        self.pairing_requests.append((user_id, chat_id, username))
        return "PAIRCODE", True

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent_texts.append((chat_id, text))

    async def handle_prompt(self, chat_id: int, user_id: int, text: str) -> None:
        self.prompts.append((chat_id, user_id, text))

    async def cancel_session(self, chat_id: int):
        return CancellationResult(False, 0)

    async def cancel_turn(self, chat_id: int, turn_id: str):
        self.cancelled_turns.append((chat_id, turn_id))
        return True

    async def flush_session_memory(self, chat_id: int, session):
        self.flushed_sessions.append((chat_id, session.session_id))
        return await session.agent.flush_memory()

    async def create_new_session(self, chat_id: int):
        assert self.session_manager is not None
        old_session = self.session_manager.get_current_session(chat_id)
        if old_session is not None:
            await self.session_manager.abandon_current_session(chat_id)
            await self.flush_session_memory(chat_id, old_session)
        return self.session_manager.create_session(chat_id)

    async def switch_session(self, chat_id: int, session_id: str):
        assert self.session_manager is not None
        return await self.session_manager.switch_session(chat_id, session_id)

    async def reset_session(self, chat_id: int):
        assert self.session_manager is not None
        session = await self.session_manager.get_or_create_session(chat_id)
        await session.agent.reset_session()
        return session

    def models_summary(self) -> str:
        return "Configured providers:\n* test"

    def use_model_provider(self, name: str):
        self.model_provider_requests.append(name)
        return True, f"Switched provider to {name}."


def test_auth_middleware():
    auth = AuthMiddleware(allowed_users={1, 2}, admin_users={2})
    assert auth.is_authorized(1)
    assert not auth.is_authorized(3)
    assert auth.is_admin(2)
    assert not auth.is_admin(1)


def test_formatter_converts_standard_markdown():
    formatter = TelegramFormatter(max_length=4096)
    chunks = formatter.format_response("**bold** and [link](https://example.com)")
    assert chunks == ["*bold* and [link](https://example.com)"]


def test_formatter_converts_markdown_list():
    formatter = TelegramFormatter(max_length=4096)
    chunks = formatter.format_response("- first\n- second")
    assert chunks == ["⦁ first\n⦁ second\n"]


def test_formatter_handles_code_block():
    formatter = TelegramFormatter(max_length=4096)
    chunks = formatter.format_response("```python\nprint('hi')\n```")
    assert chunks[0].startswith("```python")
    assert chunks[0].endswith("```")


def test_formatter_splits_long_text():
    formatter = TelegramFormatter(max_length=10)
    chunks = formatter.format_response("1234567890 123")
    assert chunks == ["1234567890", " 123"]


def test_formatter_splits_markdown_without_breaking_entities():
    formatter = TelegramFormatter(max_length=20)
    chunks = formatter.format_response("**" + " ".join(["word"] * 20) + "**")

    assert len(chunks) > 1
    assert all(chunk.startswith("*") and chunk.endswith("*") for chunk in chunks)
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 20 for chunk in chunks)


def test_formatter_uses_utf16_length_for_emoji():
    formatter = TelegramFormatter(max_length=6)
    chunks = formatter.format_response("😀" * 10)
    assert chunks == ["😀😀😀", "😀😀😀", "😀😀😀", "😀"]


def test_split_plain_text_splits_on_utf16_limit():
    from ankaloop.telegram.bot import _split_plain_text

    # Each emoji is 2 UTF-16 units; limit 6 units = 3 emoji per chunk.
    assert _split_plain_text("a" * 3, 10) == ["aaa"]
    chunks = _split_plain_text("😀" * 7, 6)
    assert chunks == ["😀😀😀", "😀😀😀", "😀"]
    # Prefer splitting on newlines.
    text = "\n".join(["line" + str(i) for i in range(200)])
    parts = _split_plain_text(text, 100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts) == text or "\n".join(parts).replace("\n\n", "\n") == text


def test_send_text_chunks_oversized_command_output(capsys):
    from ankaloop.telegram.bot import _split_plain_text, _utf16_len

    assert _utf16_len("abc") == 3
    assert _utf16_len("😀") == 2
    long_output = "skill-🔴😀\n" * 800
    parts = _split_plain_text(long_output, 4096)
    assert all(_utf16_len(p) <= 4096 for p in parts)
    # Content preserved across chunks (modulo stripped blank lines).
    rejoined = "".join(p + "\n" for p in parts).replace("\n\n", "\n")
    assert rejoined.startswith("skill-🔴😀\n")


def test_session_manager_creates_sessions():
    async def _run():
        manager = SessionManager(agent_factory=_FakeAgent, session_timeout=60)
        session = await manager.get_or_create_session(123)
        assert session.session_id.startswith("telegram-123-")
        session2 = manager.create_session(123)
        assert await manager.switch_session(123, session2.session_id)
        assert manager.get_current_session_id(123) == session2.session_id

    asyncio.run(_run())


def test_session_manager_abandons_current_session_when_creating_replacement():
    async def _run():
        manager = SessionManager(agent_factory=_FakeAgent, session_timeout=60)
        old_session = manager.create_session(123)
        old_session.agent._busy = True
        old_session.agent._queued = 1

        result = await manager.abandon_current_session(123)
        new_session = manager.create_session(123)

        assert new_session.session_id != old_session.session_id
        assert manager.get_current_session_id(123) == new_session.session_id
        assert old_session.generation == 1
        assert old_session.agent._busy is False
        assert old_session.agent._queued == 0
        assert result.active_cancelled is True
        assert result.queued_cancelled == 1

    asyncio.run(_run())


def test_session_abandon_invalidates_delivery_before_waiting_for_cancel():
    async def _run():
        cancel_started = asyncio.Event()
        cancel_release = asyncio.Event()

        class _BlockingAgent(_FakeAgent):
            async def cancel(self, **kwargs):
                cancel_started.set()
                await cancel_release.wait()
                return await super().cancel(**kwargs)

        manager = SessionManager(agent_factory=_BlockingAgent, session_timeout=60)
        session = manager.create_session(123)
        session.agent._busy = True
        task = asyncio.create_task(manager.abandon_current_session(123))

        await cancel_started.wait()
        assert session.generation == 1
        assert manager.get_current_session_id(123) is None
        assert not task.done()

        cancel_release.set()
        result = await task
        assert result.active_cancelled is True

    asyncio.run(_run())


def test_expired_sessions_are_closed():
    async def _run():
        manager = SessionManager(agent_factory=_FakeAgent, session_timeout=1)
        session = manager.create_session(123)
        session.last_used = datetime.now() - timedelta(seconds=2)

        await manager.prune_expired()

        assert session.agent.closed is True
        assert manager.get_current_session(123) is None

    asyncio.run(_run())


def test_handle_new_uses_shared_session_command():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        message = _make_message(text="/new", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )

        await handlers.handle_new(update, SimpleNamespace(args=[]))

        assert fake_bot.sent_texts
        assert fake_bot.sent_texts[-1][1].startswith("Created session: telegram-123-")

    asyncio.run(_run())


def test_handle_new_flushes_old_session_before_replacement():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        old_session = handlers._session_manager.create_session(123)
        old_session.agent._queued = 1
        message = _make_message(text="/new", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )

        await handlers.handle_new(update, SimpleNamespace(args=[]))
        await asyncio.sleep(0)

        new_session_id = handlers._session_manager.get_current_session_id(123)
        assert new_session_id != old_session.session_id
        assert old_session.generation == 1
        assert old_session.agent.queued_count() == 0
        assert fake_bot.flushed_sessions == [(123, old_session.session_id)]
        assert old_session.agent.flush_calls == [{}]

    asyncio.run(_run())


def test_stop_callback_cancels_exact_turn_idempotently():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        query = SimpleNamespace(
            data="cancel:turn-abc123",
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123, type="private"),
            effective_user=SimpleNamespace(id=42),
            message=None,
            callback_query=query,
        )

        await handlers.handle_cancel_callback(update, SimpleNamespace())
        await handlers.handle_cancel_callback(update, SimpleNamespace())

        assert fake_bot.cancelled_turns == [
            (123, "turn-abc123"),
            (123, "turn-abc123"),
        ]
        assert query.answer.await_count == 2

    asyncio.run(_run())


def test_handle_session_switch_uses_shared_session_command():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        message = _make_message(text="/session", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )
        session = handlers._session_manager.create_session(123)

        await handlers.handle_session(update, SimpleNamespace(args=["switch", session.session_id]))

        assert fake_bot.sent_texts[-1] == (123, f"Switched to session: {session.session_id}")

    asyncio.run(_run())


def test_handle_models_lists_configured_providers():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        message = _make_message(text="/models", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )

        await handlers.handle_models(update, SimpleNamespace(args=[]))

        assert fake_bot.sent_texts[-1] == (123, "Configured providers:\n* test")

    asyncio.run(_run())


def test_handle_status_shows_context_and_token_usage():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        message = _make_message(text="/status", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )
        session = handlers._session_manager.create_session(123)
        session.agent.get_token_usage_summary = lambda: {
            "context_tokens": 16_000,
            "context_window": 64_000,
            "context_usage_ratio": 0.25,
            "last_output_tokens": 500,
            "last_usage_from_api": True,
            "total_input_tokens": 30_000,
            "total_output_tokens": 2_000,
            "total_tokens": 32_000,
            "total_cached_input_tokens": 4_000,
            "total_cache_write_input_tokens": 1_000,
            "usage_reported_llm_calls": 3,
            "estimated_input_llm_calls": 0,
            "total_llm_calls": 3,
        }

        await handlers.handle_status(update, SimpleNamespace(args=[]))

        status = fake_bot.sent_texts[-1][1]
        assert "Context: 16,000 / 64,000 (25.0%, API)" in status
        assert "Last output: 500 tokens" in status
        assert "Chat-loop tokens: 30,000 input + 2,000 output = 32,000" in status
        assert "Cache: 13.3% hit · 4,000 read, 1,000 write" in status

    asyncio.run(_run())


def test_handle_model_use_switches_provider_for_admin():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42}, admin_users={42})
        message = _make_message(text="/model use backup", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )

        await handlers.handle_model(update, SimpleNamespace(args=["use", "backup"]))

        assert fake_bot.model_provider_requests == ["backup"]
        assert fake_bot.sent_texts[-1] == (123, "Switched provider to backup.")

    asyncio.run(_run())


# --- Metadata injection tests ---


def test_exclude_none_removes_none():
    result = _exclude_none({"a": 1, "b": None, "c": "hello", "d": None})
    assert result == {"a": 1, "c": "hello"}


def test_exclude_none_keeps_falsy():
    result = _exclude_none({"a": 0, "b": "", "c": False, "d": None})
    assert result == {"a": 0, "b": "", "c": False}


def _make_message(
    text="hello",
    chat_id=123,
    message_id=10,
    reply_to=None,
    *,
    chat_type="private",
    username="tester",
    user_id=42,
    entities=None,
    caption=None,
    caption_entities=None,
    thread_id=None,
    bot_username="amcp_bot",
    bot_id=700,
):
    user = SimpleNamespace(
        id=user_id,
        username=username,
        full_name="Test User",
        first_name="Test",
        last_name="User",
        is_bot=False,
    )
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    bot = SimpleNamespace(id=bot_id, username=bot_username)
    return SimpleNamespace(
        chat_id=chat_id,
        chat=chat,
        message_id=message_id,
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=caption_entities or [],
        from_user=user,
        reply_to_message=reply_to,
        message_thread_id=thread_id,
        date=None,
        get_bot=lambda: bot,
    )


def test_build_enriched_prompt_includes_separator():
    msg = _make_message(text="hello world")
    result = _build_enriched_prompt("hello world", msg)
    assert "\u2014\u2014\u2014\u2014\u2014\u2014\u2014" in result
    content, meta_json = result.split("\n\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n", 1)
    assert content == "hello world"
    data = json.loads(meta_json)
    assert data["channel"] == "telegram"
    assert data["chat_id"] == "123"
    assert data["message_id"] == 10
    assert data["type"] == "text"
    assert data["sender_id"] == "42"
    assert data["sender_is_bot"] is False
    assert data["username"] == "tester"
    assert data["chat_type"] == "private"


def test_build_enriched_prompt_assistant_mode_adds_web_guidance():
    msg = _make_message(text="latest FastAPI changes")
    result = _build_enriched_prompt("latest FastAPI changes", msg, assistant_mode=True)
    content, meta_json = result.split("\n\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n", 1)
    data = json.loads(meta_json)

    assert "[Telegram assistant mode]" in content
    assert "web_search" in content
    assert "web_fetch" in content
    assert data["assistant_mode"] is True
    assert data["network_tools"] == ["web_search", "web_fetch"]


def test_build_enriched_prompt_with_media_metadata():
    msg = _make_message(text="[Photo]")
    media_meta = {"file_id": "abc123", "width": 800, "height": 600}
    result = _build_enriched_prompt("[Photo]", msg, "photo", media_meta)
    _, meta_json = result.split("\n\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n", 1)
    data = json.loads(meta_json)
    assert data["type"] == "photo"
    assert data["media"]["file_id"] == "abc123"


def test_extract_reply_metadata_none_when_no_reply():
    msg = _make_message()
    assert _extract_reply_metadata(msg) is None


def test_extract_reply_metadata_with_reply():
    reply_user = SimpleNamespace(id=99, username="bot_user", is_bot=True)
    reply_msg = SimpleNamespace(
        message_id=5,
        text="original message",
        from_user=reply_user,
    )
    msg = _make_message(reply_to=reply_msg)
    result = _extract_reply_metadata(msg)
    assert result is not None
    assert result["message_id"] == 5
    assert result["from_user_id"] == 99
    assert result["from_username"] == "bot_user"
    assert result["from_is_bot"] is True
    assert result["text"] == "original message"


def test_build_enriched_prompt_with_reply():
    reply_user = SimpleNamespace(id=99, username="bot_user", is_bot=True)
    reply_msg = SimpleNamespace(
        message_id=5,
        text="original",
        from_user=reply_user,
    )
    msg = _make_message(reply_to=reply_msg)
    result = _build_enriched_prompt("replying", msg)
    _, meta_json = result.split("\n\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n", 1)
    data = json.loads(meta_json)
    assert "reply_to_message" in data
    reply = data["reply_to_message"]
    assert reply["message_id"] == 5
    assert reply["from_is_bot"] is True


def _make_handlers(
    config: TelegramConfig,
    allowed_users: set[int] | None = None,
    admin_users: set[int] | None = None,
):
    fake_bot = _FakeBot(config)
    manager = SessionManager(agent_factory=_FakeAgent, session_timeout=60)
    fake_bot.session_manager = manager
    handlers = TelegramHandlers(
        bot=fake_bot,
        session_manager=manager,
        auth=AuthMiddleware(allowed_users=allowed_users or set(), admin_users=admin_users or set()),
        rate_limiter=RateLimiter(limit=100),
    )
    return handlers, fake_bot


def test_message_access_dm_pairing_generates_code():
    handlers, fake_bot = _make_handlers(
        TelegramConfig(dm_policy="pairing", pairing=TelegramPairingConfig(enabled=True)),
        allowed_users=set(),
    )
    message = _make_message(chat_type="private", user_id=222, username="new_user")

    decision = handlers._evaluate_message_access(message, 222)

    assert decision.allowed is False
    assert decision.pairing_code == "PAIRCODE"
    assert fake_bot.pairing_requests == [(222, 123, "new_user")]


def test_message_access_group_mention_policy_requires_mention():
    handlers, _ = _make_handlers(TelegramConfig(group_policy="mention"), allowed_users=set())

    plain_message = _make_message(chat_type="supergroup", chat_id=-1001, text="hello")
    decision_plain = handlers._evaluate_message_access(plain_message, 42)
    assert decision_plain.allowed is False

    mention_message = _make_message(chat_type="supergroup", chat_id=-1001, text="@amcp_bot hello")
    decision_mention = handlers._evaluate_message_access(mention_message, 42)
    assert decision_mention.allowed is True
    assert decision_mention.was_mentioned is True


def test_message_access_group_reply_to_bot_counts_as_mention():
    handlers, _ = _make_handlers(TelegramConfig(group_policy="mention"), allowed_users=set())
    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=700, is_bot=True), text="prev")
    message = _make_message(chat_type="group", chat_id=-1001, text="reply", reply_to=reply_to)

    decision = handlers._evaluate_message_access(message, 42)

    assert decision.allowed is True
    assert decision.was_mentioned is True


def test_message_access_topic_override_precedence():
    group_cfg = TelegramGroupConfig(
        group_policy="allowlist",
        allow_users=[10],
        topics={"42": TelegramTopicConfig(group_policy="open", require_mention=False)},
    )
    cfg = TelegramConfig(group_policy="mention", groups={"-1001": group_cfg})
    handlers, _ = _make_handlers(cfg, allowed_users=set())

    topic_message = _make_message(chat_type="supergroup", chat_id=-1001, thread_id=42, user_id=99)
    non_topic_message = _make_message(chat_type="supergroup", chat_id=-1001, thread_id=43, user_id=99)

    topic_decision = handlers._evaluate_message_access(topic_message, 99)
    non_topic_decision = handlers._evaluate_message_access(non_topic_message, 99)

    assert topic_decision.allowed is True
    assert non_topic_decision.allowed is False


# --- Typing lifecycle tests ---


def _make_bot_for_typing(
    *,
    typing_indicator: bool = True,
    typing_interval: int = 1,
    agent_factory=None,
):
    """Build a TelegramBot with mocked internals for typing tests."""
    config = TelegramConfig(
        enabled=True,
        bot_token="fake-token",
        typing_indicator=typing_indicator,
        typing_interval_seconds=typing_interval,
    )
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.bot.send_chat_action = AsyncMock()
    message_ids = iter(range(1, 1000))
    mock_app.bot.send_message = AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(message_id=next(message_ids)))
    mock_app.bot.edit_message_text = AsyncMock()
    importlib.import_module("ankaloop.telegram.bot")

    with (
        patch("ankaloop.telegram.bot.ApplicationBuilder") as builder_cls,
        patch("ankaloop.telegram.bot.CommandHandler", MagicMock()),
        patch("ankaloop.telegram.bot.MessageHandler", MagicMock()),
        patch("ankaloop.telegram.bot.filters", MagicMock()),
    ):
        builder_cls.return_value.token.return_value.post_init.return_value.build.return_value = mock_app

        from ankaloop.telegram.bot import TelegramBot

        bot = TelegramBot(
            token="fake-token",
            allowed_users={1},
            admin_users={1},
            config=config,
            agent_factory=agent_factory,
        )
    return bot


def test_post_init_registers_user_facing_bot_commands():
    async def _run():
        bot = _make_bot_for_typing()
        bot._application.bot.set_my_commands = AsyncMock()

        with patch("ankaloop.telegram.bot.BotCommand") as mock_bot_command:
            mock_bot_command.side_effect = lambda cmd, desc: SimpleNamespace(command=cmd, description=desc)
            await bot._post_init(bot._application)

        bot._application.bot.set_my_commands.assert_called_once()
        commands = bot._application.bot.set_my_commands.call_args[0][0]
        names = [c.command for c in commands]

        # user-facing commands registered in the menu
        for expected in ["start", "help", "status", "new", "session", "skills", "models"]:
            assert expected in names
        # admin and helper commands intentionally omitted
        for omitted in [
            "cancel",
            "ask",
            "activate",
            "memory",
            "model",
            "config",
            "users",
            "pair",
            "logs",
            "schedule",
            "shutdown",
        ]:
            assert omitted not in names

    asyncio.run(_run())


def test_get_user_bot_commands_descriptions():
    bot = _make_bot_for_typing()

    with patch("ankaloop.telegram.bot.BotCommand") as mock_bot_command:
        mock_bot_command.side_effect = lambda cmd, desc: SimpleNamespace(command=cmd, description=desc)
        commands = bot._get_user_bot_commands()

    assert [c.command for c in commands] == [
        "start",
        "help",
        "status",
        "recovery",
        "new",
        "session",
        "skills",
        "models",
    ]
    for command in commands:
        assert command.description  # non-empty


def test_persist_config_does_not_write_environment_bot_token(monkeypatch, tmp_path):
    async def _run():
        bot = _make_bot_for_typing()
        loaded_config = SimpleNamespace(telegram=None)

        monkeypatch.setenv("ANKA_TELEGRAM_BOT_TOKEN", "runtime-secret")
        with (
            patch("ankaloop.telegram.bot.load_config", return_value=loaded_config),
            patch("ankaloop.telegram.bot.save_config", return_value=tmp_path / "config.toml") as save_config,
        ):
            await bot.persist_config()

        saved_config = save_config.call_args.args[0]
        assert saved_config.telegram.bot_token is None
        assert bot.config.bot_token == "fake-token"

    asyncio.run(_run())


def test_typing_start_creates_task():
    async def _run():
        bot = _make_bot_for_typing()
        await bot._start_typing(100)

        assert 100 in bot._typing_tasks
        task = bot._typing_tasks[100]
        assert not task.done()

        await bot._stop_typing(100)
        assert 100 not in bot._typing_tasks

    asyncio.run(_run())


def test_create_new_session_flushes_and_replaces_under_boundary_lock():
    async def _run():
        bot = _make_bot_for_typing(agent_factory=_FakeAgent)
        old_session = bot._session_manager.create_session(800)
        old_session.agent._queued = 1
        old_session.agent._busy = True

        flushed: list[str] = []

        async def _flush(chat_id: int, session, **_kwargs) -> bool:
            flushed.append(f"{chat_id}:{session.session_id}:{session.generation}:{session.agent.queued_count()}")
            return True

        with patch.object(bot, "flush_session_memory", side_effect=_flush):
            new_session = await bot.create_new_session(800)

        await asyncio.sleep(0)
        assert new_session.session_id != old_session.session_id
        assert bot._session_manager.get_current_session_id(800) == new_session.session_id
        assert old_session.generation == 1
        assert old_session.agent.queued_count() == 0
        assert flushed == [f"800:{old_session.session_id}:1:0"]
        assert new_session.agent.execution_context["memory_project_root"] == str(bot.memory_project_root(800))

    asyncio.run(_run())


def test_create_new_session_waits_for_old_turn_before_publishing():
    async def _run():
        cancel_started = asyncio.Event()
        cancel_release = asyncio.Event()

        class _BlockingAgent(_FakeAgent):
            async def cancel(self, **kwargs):
                cancel_started.set()
                await cancel_release.wait()
                return await super().cancel(**kwargs)

        bot = _make_bot_for_typing(agent_factory=_BlockingAgent)
        old_session = bot._session_manager.create_session(802)
        old_session.agent._busy = True

        with patch.object(bot, "flush_session_memory", new_callable=AsyncMock, return_value=True):
            task = asyncio.create_task(bot.create_new_session(802))
            await cancel_started.wait()
            assert old_session.generation == 1
            assert bot._session_manager.get_current_session(802) is None
            assert not task.done()
            cancel_release.set()
            new_session = await task

        assert bot._session_manager.get_current_session(802) is new_session

    asyncio.run(_run())


def test_cancel_session_waits_and_returns_actual_counts():
    async def _run():
        cancel_started = asyncio.Event()
        cancel_release = asyncio.Event()

        class _BlockingAgent(_FakeAgent):
            async def cancel(self, **kwargs):
                cancel_started.set()
                await cancel_release.wait()
                return await super().cancel(**kwargs)

        bot = _make_bot_for_typing(agent_factory=_BlockingAgent)
        session = bot._session_manager.create_session(803)
        session.agent._busy = True
        session.agent._queued = 2

        task = asyncio.create_task(bot.cancel_session(803))
        await cancel_started.wait()
        assert not task.done()
        cancel_release.set()
        result = await task

        assert result == CancellationResult(True, 2)

    asyncio.run(_run())


def test_bot_stop_closes_all_session_agents():
    async def _run():
        bot = _make_bot_for_typing(agent_factory=_FakeAgent)
        first = bot._session_manager.create_session(804)
        second = bot._session_manager.create_session(805)
        bot._stop_event = asyncio.Event()

        await bot.stop()

        assert first.agent.closed is True
        assert second.agent.closed is True

    asyncio.run(_run())


def test_expiry_cleanup_does_not_deadlock_delivery_boundary():
    async def _run():
        started = asyncio.Event()

        class _Agent(_FakeAgent):
            async def run(self, **kwargs):
                started.set()
                await asyncio.sleep(60)
                return "late"

        bot = _make_bot_for_typing(agent_factory=_Agent)
        bot._session_manager.update_session_timeout(1)
        session = bot._session_manager.create_session(806)
        session.last_used = datetime.now() - timedelta(seconds=120)
        message = TelegramQueuedMessage(chat_id=806, user_id=1, text="hi")
        delivery = asyncio.create_task(bot._process_message(session, message))
        session.delivery_tasks.add(delivery)
        delivery.add_done_callback(session.delivery_tasks.discard)
        await started.wait()

        async with bot._get_session_boundary_lock(806):
            await asyncio.wait_for(bot._session_manager.prune_expired(806), timeout=1)

        assert session.agent.closed is True
        assert delivery.done()

    asyncio.run(_run())


def test_memory_dream_once_runs_for_active_chat():
    async def _run():
        bot = _make_bot_for_typing(agent_factory=_FakeAgent)
        bot._session_manager.create_session(801)
        run_once = MagicMock(return_value=SimpleNamespace(ran=True, updated=False, reason="no_reply"))
        dreamer_cls = MagicMock(return_value=SimpleNamespace(run_once=run_once))

        with patch("ankaloop.telegram.bot.MemoryDreamer", dreamer_cls):
            await bot._run_memory_dream_once()

        dreamer_cls.assert_called_once_with(bot.memory_project_root(801))
        run_once.assert_called_once()

    asyncio.run(_run())


def test_memory_dream_loop_start_stop():
    async def _run():
        bot = _make_bot_for_typing(agent_factory=_FakeAgent)
        bot._memory_dream_interval_seconds = 60
        bot._start_memory_dream_loop()

        assert bot._memory_dream_task is not None
        assert not bot._memory_dream_task.done()

        await bot._stop_memory_dream_loop()

        assert bot._memory_dream_task is None

    asyncio.run(_run())


def test_typing_start_sends_immediate_chat_action():
    async def _run():
        bot = _make_bot_for_typing()

        await bot._start_typing(150)

        bot._application.bot.send_chat_action.assert_any_call(chat_id=150, action="typing")
        await bot._stop_typing(150)

    asyncio.run(_run())


def test_typing_stop_cancels_task():
    async def _run():
        bot = _make_bot_for_typing()
        await bot._start_typing(200)
        task = bot._typing_tasks[200]

        await bot._stop_typing(200)

        assert 200 not in bot._typing_tasks
        assert task.cancelled() or task.done()

    asyncio.run(_run())


def test_typing_stays_active_until_all_requests_finish():
    async def _run():
        bot = _make_bot_for_typing()
        await bot._start_typing(250)
        task = bot._typing_tasks[250]

        await bot._start_typing(250)

        assert bot._typing_tasks[250] is task
        assert bot._typing_counts[250] == 2

        await bot._stop_typing(250)

        assert bot._typing_tasks[250] is task
        assert bot._typing_counts[250] == 1
        assert not task.done()

        await bot._stop_typing(250)

        assert 250 not in bot._typing_tasks
        assert 250 not in bot._typing_counts
        assert task.cancelled() or task.done()

    asyncio.run(_run())


def test_typing_start_cancellation_rolls_back_request_count():
    async def _run():
        bot = _make_bot_for_typing()
        started = asyncio.Event()

        async def _send_chat_action(**kwargs):
            started.set()
            await asyncio.Future()

        bot._application.bot.send_chat_action.side_effect = _send_chat_action
        task = asyncio.create_task(bot._start_typing(275))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert 275 not in bot._typing_tasks
        assert 275 not in bot._typing_counts

    asyncio.run(_run())


def test_typing_disabled_skips():
    async def _run():
        bot = _make_bot_for_typing(typing_indicator=False)
        await bot._start_typing(300)

        assert 300 not in bot._typing_tasks

    asyncio.run(_run())


def test_typing_loop_sends_chat_action():
    async def _run():
        bot = _make_bot_for_typing(typing_interval=1)
        await bot._start_typing(400)

        # Let the loop fire at least once
        await asyncio.sleep(0.05)

        bot._application.bot.send_chat_action.assert_called_with(chat_id=400, action="typing")

        await bot._stop_typing(400)

    asyncio.run(_run())


def test_typing_stop_all():
    async def _run():
        bot = _make_bot_for_typing()
        await bot._start_typing(501)
        await bot._start_typing(502)
        assert len(bot._typing_tasks) == 2

        await bot._stop_all_typing()

        assert len(bot._typing_tasks) == 0
        assert len(bot._typing_counts) == 0

    asyncio.run(_run())


def test_handle_prompt_starts_background_worker_and_returns():
    async def _run():
        started = asyncio.Event()
        release = asyncio.Event()

        class _SlowAgent:
            def __init__(self, session_id: str) -> None:
                self.session_id = session_id
                self.execution_context = {}
                self._busy = False

            def is_busy(self) -> bool:
                return self._busy

            def queued_count(self) -> int:
                return 0

            def add_event_callback(self, callback) -> None:
                pass

            def remove_event_callback(self, callback) -> None:
                pass

            async def submit(self, prompt, **kwargs):
                self._busy = True
                handle = MagicMock(id="turn-slow")

                async def _wait():
                    started.set()
                    await release.wait()
                    self._busy = False
                    return "done"

                handle.wait = _wait
                return handle

            async def cancel(self, **kwargs):
                self._busy = False

        bot = _make_bot_for_typing(agent_factory=_SlowAgent)

        await bot.handle_prompt(900, 1, "hi")
        await asyncio.wait_for(started.wait(), timeout=1)

        session = bot._session_manager.get_current_session(900)
        assert session is not None
        assert len(session.delivery_tasks) == 1
        assert not next(iter(session.delivery_tasks)).done()
        assert (
            bot._application.bot.send_message.call_args_list[0].kwargs["text"] == "Started processing your request..."
        )
        markup = bot._application.bot.send_message.call_args_list[0].kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == "cancel:turn-slow"

        release.set()
        if session.delivery_tasks:
            await asyncio.gather(*session.delivery_tasks, return_exceptions=True)
        await asyncio.sleep(0.05)

        bot._application.bot.edit_message_text.assert_any_call(
            chat_id=900,
            message_id=1,
            text="done",
            reply_markup=None,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )

    asyncio.run(_run())


def test_status_send_failure_cancels_submitted_turn():
    async def _run():
        cancelled: list[str] = []

        class _Agent:
            def __init__(self, session_id: str) -> None:
                self.session_id = session_id
                self.execution_context = {}

            def is_busy(self) -> bool:
                return False

            def queued_count(self) -> int:
                return 0

            async def submit(self, prompt, **kwargs):
                return SimpleNamespace(id="turn-orphan")

            async def cancel_turn(self, turn_id: str):
                cancelled.append(turn_id)
                return True

        bot = _make_bot_for_typing(agent_factory=_Agent)
        bot._application.bot.send_message.side_effect = RuntimeError("Telegram unavailable")

        with pytest.raises(RuntimeError, match="Telegram unavailable"):
            await bot.handle_prompt(906, 1, "hi")

        assert cancelled == ["turn-orphan"]

    asyncio.run(_run())


def test_handle_prompt_queues_follow_up_for_same_session():
    async def _run():
        first_started = asyncio.Event()
        first_release = asyncio.Event()
        calls: list[str] = []

        class _QueuedAgent:
            def __init__(self, session_id: str) -> None:
                self.session_id = session_id
                self.execution_context = {}
                self._busy = False
                self._queued = 0

            def is_busy(self) -> bool:
                return self._busy

            def queued_count(self) -> int:
                return self._queued

            def add_event_callback(self, callback) -> None:
                pass

            def remove_event_callback(self, callback) -> None:
                pass

            async def submit(self, prompt, **kwargs):
                calls.append(prompt)
                idx = len(calls)
                if idx == 1:
                    self._busy = True
                    handle = MagicMock(id="turn-first")

                    async def _wait1():
                        first_started.set()
                        await first_release.wait()
                        self._busy = False
                        self._queued = max(0, self._queued - 1)
                        return "reply 1"

                    handle.wait = _wait1
                    return handle
                else:
                    self._queued += 1
                    handle = MagicMock(id="turn-second")

                    async def _wait2():
                        self._busy = True
                        self._queued = max(0, self._queued - 1)
                        return "reply 2"

                    handle.wait = _wait2
                    return handle

            async def cancel(self, **kwargs):
                self._busy = False
                self._queued = 0

        bot = _make_bot_for_typing(agent_factory=_QueuedAgent)

        await bot.handle_prompt(901, 1, "first")
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await bot.handle_prompt(901, 1, "second")

        session = bot._session_manager.get_current_session(901)
        assert session is not None
        assert session.agent.queued_count() == 1
        assert bot._application.bot.send_message.call_args_list[1].kwargs["text"] == (
            "Session busy. Queued at position 1."
        )

        first_release.set()
        if session.delivery_tasks:
            await asyncio.gather(*session.delivery_tasks, return_exceptions=True)
        await asyncio.sleep(0.05)

        assert calls == ["first", "second"]
        bot._application.bot.edit_message_text.assert_any_call(
            chat_id=901,
            message_id=2,
            text="reply 2",
            reply_markup=None,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )

    asyncio.run(_run())


def test_process_message_starts_and_stops_typing():
    async def _run():
        bot = _make_bot_for_typing()

        class _MockAgent:
            session_id = "test"

            async def run(self, **kwargs):
                return "ok"

        session = SimpleNamespace(
            session_id="test",
            agent=_MockAgent(),
            lock=asyncio.Lock(),
            current_task=None,
        )
        msg = TelegramQueuedMessage(chat_id=600, user_id=1, text="hi")

        with (
            patch.object(bot, "_start_typing", new_callable=AsyncMock) as start,
            patch.object(bot, "_stop_typing", new_callable=AsyncMock) as stop,
        ):
            await bot._process_message(session, msg)

        start.assert_called_once_with(600)
        stop.assert_called_once_with(600)

    asyncio.run(_run())


def test_process_message_stops_typing_after_response_delivery():
    async def _run():
        bot = _make_bot_for_typing()
        events: list[str] = []

        class _MockAgent:
            session_id = "test"

            async def run(self, **kwargs):
                return "ok"

        session = SimpleNamespace(
            session_id="test",
            agent=_MockAgent(),
            lock=asyncio.Lock(),
            current_task=None,
        )
        msg = TelegramQueuedMessage(chat_id=650, user_id=1, text="hi")

        async def _start(chat_id: int) -> None:
            events.append(f"start:{chat_id}")

        async def _stop(chat_id: int) -> None:
            events.append(f"stop:{chat_id}")

        async def _send_markdown(chat_id: int, text: str) -> None:
            events.append(f"send:{chat_id}:{text}")

        with (
            patch.object(bot, "_start_typing", side_effect=_start),
            patch.object(bot, "_stop_typing", side_effect=_stop),
            patch.object(bot, "send_markdown", side_effect=_send_markdown),
        ):
            await bot._process_message(session, msg)

        assert events == ["start:650", "send:650:ok", "stop:650"]

    asyncio.run(_run())


def test_process_message_stops_typing_on_error():
    async def _run():
        bot = _make_bot_for_typing()

        class _ErrorAgent:
            session_id = "test"

            async def run(self, **kwargs):
                raise RuntimeError("boom")

        session = SimpleNamespace(
            session_id="test",
            agent=_ErrorAgent(),
            lock=asyncio.Lock(),
            current_task=None,
        )
        msg = TelegramQueuedMessage(chat_id=700, user_id=1, text="hi")

        with (
            patch.object(bot, "_start_typing", new_callable=AsyncMock) as start,
            patch.object(bot, "_stop_typing", new_callable=AsyncMock) as stop,
        ):
            await bot._process_message(session, msg)

        start.assert_called_once_with(700)
        stop.assert_called_once_with(700)

    asyncio.run(_run())


def test_process_message_suppresses_error_after_session_is_abandoned():
    async def _run():
        bot = _make_bot_for_typing()
        started = asyncio.Event()
        release = asyncio.Event()

        class _ErrorAgent:
            session_id = "test"

            async def run(self, **kwargs):
                started.set()
                await release.wait()
                raise RuntimeError("boom")

        session = SimpleNamespace(
            session_id="test",
            agent=_ErrorAgent(),
            lock=asyncio.Lock(),
            current_task=None,
            generation=0,
        )
        msg = TelegramQueuedMessage(chat_id=710, user_id=1, text="hi")

        with patch.object(bot, "send_markdown", new_callable=AsyncMock) as send_markdown:
            task = asyncio.create_task(bot._process_message(session, msg))
            await started.wait()
            session.generation += 1
            release.set()
            await task

        send_markdown.assert_not_called()

    asyncio.run(_run())


def test_process_message_suppresses_result_chunks_after_abandon():
    async def _run():
        bot = _make_bot_for_typing()
        started = asyncio.Event()
        release = asyncio.Event()

        class _SlowAgent:
            session_id = "test"

            async def run(self, **kwargs):
                started.set()
                await release.wait()
                return "late result"

        session = SimpleNamespace(
            session_id="test",
            agent=_SlowAgent(),
            lock=asyncio.Lock(),
            generation=0,
        )
        msg = TelegramQueuedMessage(chat_id=711, user_id=1, text="hi")

        with (
            patch.object(bot, "send_markdown", new_callable=AsyncMock) as send_markdown,
            patch.object(bot, "_edit_text", new_callable=AsyncMock) as edit_text,
        ):
            task = asyncio.create_task(bot._process_message(session, msg))
            await started.wait()
            session.generation += 1
            release.set()
            await task

        send_markdown.assert_not_awaited()
        edit_text.assert_not_awaited()

    asyncio.run(_run())


def test_streaming_config_defaults():
    cfg = TelegramStreamingConfig()
    assert cfg.streaming_enabled is True
    assert cfg.streaming_interval_seconds == 1.5
    assert cfg.streaming_min_chars == 50
    assert cfg.streaming_max_chars == 3900


def test_streaming_config_disabled():
    cfg = TelegramStreamingConfig(streaming_enabled=False)
    assert cfg.streaming_enabled is False


def test_streaming_config_validation():
    cfg = TelegramStreamingConfig(streaming_interval_seconds=0.1, streaming_min_chars=1, streaming_max_chars=100)
    assert cfg.streaming_interval_seconds == 0.1
    assert cfg.streaming_min_chars == 1
    assert cfg.streaming_max_chars == 100


def test_telegram_config_has_streaming_field():
    cfg = TelegramConfig()
    assert hasattr(cfg, "streaming")
    assert isinstance(cfg.streaming, TelegramStreamingConfig)
    assert cfg.streaming.streaming_enabled is True


def test_streaming_delivery_manager_accumulates_chunks():
    """Test that _StreamingDeliveryManager accumulates chunks from the event callback."""
    from ankaloop.telegram.bot import _StreamingDeliveryManager

    formatter = TelegramFormatter(max_length=4096)
    cfg = TelegramStreamingConfig(streaming_min_chars=1, streaming_interval_seconds=0.0)

    class _FakeBot:
        def _get_telegram_message_id(self, msg):
            return 42

        async def _edit_text(self, chat_id, message_id, text, markdown=False):
            return True

        async def send_text(self, chat_id, text):
            pass

        async def send_markdown(self, chat_id, text):
            pass

    bot = _FakeBot()
    manager = _StreamingDeliveryManager(
        bot=bot,
        chat_id=123,
        status_message_id=1,
        formatter=formatter,
        cfg=cfg,
        is_current=lambda: True,
    )

    # Simulate chunk events (synchronous, called from agent _emit_event)
    manager.on_chunk("message.chunk", {"content": "Hello "})
    manager.on_chunk("message.chunk", {"content": "world!"})

    # The buffer should have accumulated
    assert manager._buffer == "Hello world!"
    assert manager._needs_flush is True


def test_streaming_delivery_manager_ignores_non_chunk_events():
    """Test that _StreamingDeliveryManager ignores non-chunk events."""
    from ankaloop.telegram.bot import _StreamingDeliveryManager

    formatter = TelegramFormatter(max_length=4096)
    cfg = TelegramStreamingConfig()

    class _FakeBot:
        def _get_telegram_message_id(self, msg):
            return 42

        async def _edit_text(self, chat_id, message_id, text, markdown=False):
            return True

        async def send_text(self, chat_id, text):
            pass

        async def send_markdown(self, chat_id, text):
            pass

    bot = _FakeBot()
    manager = _StreamingDeliveryManager(
        bot=bot,
        chat_id=123,
        status_message_id=1,
        formatter=formatter,
        cfg=cfg,
        is_current=lambda: True,
    )

    # Non-chunk events should not affect buffer
    manager.on_chunk("tool.call_start", {"tool_name": "bash"})
    manager.on_chunk("tool.call_complete", {"tool_name": "bash"})
    assert manager._buffer == ""
    assert manager._needs_flush is False


def test_streaming_delivery_manager_flush_edits_message():
    """Test that maybe_flush edits the status message with accumulated text."""
    from ankaloop.telegram.bot import _StreamingDeliveryManager

    formatter = TelegramFormatter(max_length=4096)
    cfg = TelegramStreamingConfig(streaming_min_chars=1, streaming_interval_seconds=0.0)
    edits: list[str] = []

    class _FakeBot:
        def _get_telegram_message_id(self, msg):
            return 42

        async def _edit_text(self, chat_id, message_id, text, markdown=False):
            edits.append(text)
            return True

        async def send_text(self, chat_id, text):
            pass

        async def send_markdown(self, chat_id, text):
            pass

    bot = _FakeBot()
    manager = _StreamingDeliveryManager(
        bot=bot,
        chat_id=123,
        status_message_id=1,
        formatter=formatter,
        cfg=cfg,
        is_current=lambda: True,
    )

    manager.on_chunk("message.chunk", {"content": "Hello world!"})
    asyncio.run(manager.maybe_flush())
    assert len(edits) == 1
    assert edits[0] == "Hello world!"


def test_streaming_delivery_manager_respects_is_current():
    """Test that _StreamingDeliveryManager stops delivering when session is no longer current."""
    from ankaloop.telegram.bot import _StreamingDeliveryManager

    formatter = TelegramFormatter(max_length=4096)
    cfg = TelegramStreamingConfig(streaming_min_chars=1, streaming_interval_seconds=0.0)

    class _FakeBot:
        def _get_telegram_message_id(self, msg):
            return 42

        async def _edit_text(self, chat_id, message_id, text, markdown=False):
            return True

        async def send_text(self, chat_id, text):
            pass

        async def send_markdown(self, chat_id, text):
            pass

    is_current = [True]
    bot = _FakeBot()
    manager = _StreamingDeliveryManager(
        bot=bot,
        chat_id=123,
        status_message_id=1,
        formatter=formatter,
        cfg=cfg,
        is_current=lambda: is_current[0],
    )

    # When current, chunks accumulate
    manager.on_chunk("message.chunk", {"content": "Hello"})
    assert manager._buffer == "Hello"

    # When not current, maybe_flush should do nothing
    is_current[0] = False
    asyncio.run(manager.maybe_flush())
    # Buffer still has content but no edit was made (would need to check bot._edit_text calls)

    # finalize should also do nothing when not current
    asyncio.run(manager.finalize("Hello world!"))


def test_handle_recovery_reports_unsettled_operations():
    from ankaloop.tool_journal import JournalRecovery, ToolOperationDecision

    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        session = handlers._session_manager.create_session(123)
        session.agent.get_recovery_status = lambda: JournalRecovery(
            decisions=(
                ToolOperationDecision(
                    operation_id="t1:tc_1",
                    tool_name="bash",
                    status="indeterminate",
                    reason="no outcome; suspected crash",
                    turn_id="t1",
                    tool_call_id="tc_1",
                    recovery_mode="never_auto_retry",
                ),
            ),
            interrupted_turns=(),
            has_corruption=False,
        )
        message = _make_message(text="/recovery", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )

        await handlers.handle_recovery(update, SimpleNamespace(args=[]))

        text = fake_bot.sent_texts[-1][1]
        assert "bash" in text
        assert "indeterminate" in text
        assert "/recovery ack" in text

    asyncio.run(_run())


def test_handle_recovery_ack_calls_agent_and_reports_counts():
    from ankaloop.tool_journal import ToolJournal

    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        session = handlers._session_manager.create_session(123)
        journal = ToolJournal(Path(tempfile.gettempdir()), session.session_id)
        session.agent.acknowledge_recovery = journal.mark_recovery_acknowledged
        journal.turn_started("t1")
        journal.tool_intent("t1", "tc_1", "bash", {"command": "ls"})
        journal.turn_failed("t1", "boom")
        message = _make_message(text="/recovery ack", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )

        await handlers.handle_recovery(update, SimpleNamespace(args=["ack"]))
        await handlers.handle_recovery(update, SimpleNamespace(args=["ack"]))

        texts = [t[1] for t in fake_bot.sent_texts]
        assert "Acknowledged 0 interrupted turn(s), 1 unsettled operation(s)" in texts[0]
        assert "Nothing to acknowledge" in texts[1]

        journal.delete()

    asyncio.run(_run())


def test_handle_recovery_ack_rejects_busy_session():
    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        session = handlers._session_manager.create_session(123)
        session.agent._busy = True
        session.agent.acknowledge_recovery = MagicMock()
        message = _make_message(text="/recovery ack", user_id=42)
        update = SimpleNamespace(
            effective_chat=message.chat,
            effective_user=message.from_user,
            message=message,
        )

        await handlers.handle_recovery(update, SimpleNamespace(args=["ack"]))

        session.agent.acknowledge_recovery.assert_not_called()
        assert "while the session is processing" in fake_bot.sent_texts[-1][1]

    asyncio.run(_run())


def test_handle_recovery_all_lists_orphan_journals(tmp_path):
    import ankaloop.telegram.handlers as ankaloop_handlers
    from ankaloop.telegram.handlers import TelegramHandlers, _format_recovery_status
    from ankaloop.tool_journal import ToolJournal

    orphan_root = tmp_path / ".config" / "ankaloop" / "sessions"
    orphan_id = "telegram-123-orphan01"
    journal = ToolJournal(orphan_root, orphan_id)
    journal.turn_started("t9")
    journal.tool_intent("t9", "tc_9", "edit_file", {"path": "x"})
    # no terminal event, no outcome -> indeterminate

    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        session = handlers._session_manager.create_session(123)
        from ankaloop.tool_journal import JournalRecovery

        session.agent.get_recovery_status = lambda: JournalRecovery(
            decisions=(), interrupted_turns=(), has_corruption=False
        )
        real_scan = ankaloop_handlers.scan_journal_files

        def _scan(root, prefix=None):
            return real_scan(orphan_root, prefix=prefix)

        with (
            patch("ankaloop.telegram.handlers.Path.home", return_value=tmp_path),
            patch("ankaloop.telegram.handlers.scan_journal_files", _scan),
        ):
            message = _make_message(text="/recovery all", user_id=42)
            update = SimpleNamespace(
                effective_chat=message.chat,
                effective_user=message.from_user,
                message=message,
            )
            await handlers.handle_recovery(update, SimpleNamespace(args=["all"]))

        text = fake_bot.sent_texts[-1][1]
        assert "1 journal(s) requiring attention" in text
        assert orphan_id in text
        assert "edit_file" in text
        assert "suspected crash" in text

    asyncio.run(_run())
    journal.delete()


def test_handle_recovery_all_continues_after_malformed_journal(tmp_path):
    import ankaloop.telegram.handlers as ankaloop_handlers
    from ankaloop.tool_journal import JournalRecovery, ToolJournal

    orphan_root = tmp_path / "sessions"
    malformed_id = "telegram-123-malformed"
    malformed_path = orphan_root / f"{malformed_id}.journal.jsonl"
    orphan_root.mkdir()
    malformed_path.write_text("{not-json}\n", encoding="utf-8")
    valid_id = "telegram-123-valid"
    valid = ToolJournal(orphan_root, valid_id)
    valid.turn_started("t9")
    valid.tool_intent("t9", "tc_9", "bash", {"command": "do-work"})

    async def _run():
        handlers, fake_bot = _make_handlers(TelegramConfig(), allowed_users={42})
        session = handlers._session_manager.create_session(123)
        session.agent.get_recovery_status = lambda: JournalRecovery(
            decisions=(), interrupted_turns=(), has_corruption=False
        )
        real_scan = ankaloop_handlers.scan_journal_files

        with patch(
            "ankaloop.telegram.handlers.scan_journal_files",
            side_effect=lambda root, prefix=None: real_scan(orphan_root, prefix=prefix),
        ):
            message = _make_message(text="/recovery all", user_id=42)
            update = SimpleNamespace(
                effective_chat=message.chat,
                effective_user=message.from_user,
                message=message,
            )
            await handlers.handle_recovery(update, SimpleNamespace(args=["all"]))

        text = fake_bot.sent_texts[-1][1]
        assert "2 journal(s) requiring attention" in text
        assert malformed_id in text
        assert "journal [unreadable]" in text
        assert valid_id in text
        assert "bash" in text

    asyncio.run(_run())
